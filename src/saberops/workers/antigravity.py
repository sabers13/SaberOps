"""Antigravity (AGY) worker adapter for local CLI execution."""

from __future__ import annotations

import json
import math
import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from saberops.git import GitManager
from saberops.models import AttemptStatus, DispatchOutcome, WorkerRequest, WorkerResult
from saberops.process import (
    ORCH_PROMPT_ARG_LIMIT,
    ProcessResult,
    get_prompt_arg_limit,
    launch_worker_process,
    run_process,
)
from saberops.workers.antigravity_events import parse_events
from saberops.workers.base import WorkerAdapter, streaming_process_kwargs

__all__ = ["AntigravityAdapter", "ORCH_PROMPT_ARG_LIMIT"]

_SKIP_PERMISSIONS_OPT_OUT = frozenset({"0", "false", "no", "off"})


class AntigravityAdapter(WorkerAdapter):
    """Worker adapter dispatching tasks via Antigravity (agy) CLI."""

    capability_transport = "DIRECT_CLI"
    factual_capabilities = frozenset(
        {"streaming", "large_prompt_stdin", "process_identity", "cancellation"}
    )

    def __init__(
        self,
        runner: Callable[..., ProcessResult] = run_process,
        agy_bin: str | None = None,
    ) -> None:
        self.runner = runner
        self.agy_bin = agy_bin if agy_bin is not None else (os.environ.get("ORCH_AGY_BIN") or "agy")

    @property
    def provider_name(self) -> str:
        return "antigravity"

    def is_available(self) -> bool:
        """Check if Antigravity binary is available on PATH or custom runner is provided."""
        if self.runner is not run_process:
            return True
        return shutil.which(self.agy_bin) is not None

    def max_prompt_bytes(self) -> int | None:
        """Antigravity has a verified plain-text stdin fallback for large prompts.

        The installed CLI accepts piped text without ``--print`` and emits the
        same stream-json terminal result. Verified with a 42,421-byte prompt,
        an exact response and terminal SUCCESS; ``--print=`` refuses empty input.
        Small prompts keep the existing explicit ``--print`` argv path.
        """
        return None

    def prompt_transport(self) -> str:
        return "stdin"

    def prompt_transport_for(self, prompt_bytes: int) -> str:
        return "stdin" if prompt_bytes > get_prompt_arg_limit() else "argv"

    def run(self, request: WorkerRequest) -> WorkerResult:
        """Dispatch task to Antigravity CLI non-interactively in the specified worktree."""
        if not self.is_available():
            return WorkerResult(
                provider=self.provider_name,
                model=request.model,
                status=AttemptStatus.FAILED,
                summary="Antigravity (agy) CLI not found on system",
                stdout="",
                stderr="agy binary not found on PATH",
                changed_paths=[],
                has_changes=False,
                error="agy binary not found on PATH",
                outcome=DispatchOutcome.PROVIDER_OR_RUNTIME_FAILURE,
            )

        prompt_bytes = len(request.task.encode("utf-8"))
        use_stdin = prompt_bytes > get_prompt_arg_limit()

        # A typed autonomous effort always wins over ambient
        # ``ORCH_AGY_EFFORT``.  The neutral tier maps to the concrete
        # ``agy --effort`` option by lowercasing (LOW->low, MEDIUM->medium,
        # HIGH->high, ULTRA->ultra).  Legacy non-autonomous requests with
        # no typed effort retain the ambient fallback for compatibility.
        typed_effort = getattr(request, "reasoning_effort", None)
        if typed_effort is not None:
            effort = (
                typed_effort.value.lower()
                if hasattr(typed_effort, "value")
                else str(typed_effort).lower()
            )
        else:
            # Explicit AGY model presets already encode their effort. A legacy
            # default of medium conflicts with a selected -high/-low preset.
            # Keep owner-supplied effort authoritative; derive only the default.
            preset_effort = request.model.rsplit("-", 1)[-1]
            effort = os.environ.get("ORCH_AGY_EFFORT") or (
                preset_effort if preset_effort in {"low", "medium", "high"} else "medium"
            )

        cmd = [self.agy_bin]
        if use_stdin:
            cmd.extend(["--input-format", "text"])
        else:
            cmd.extend(["--print", request.task])
        cmd.extend([
            "--model",
            request.model,
            "--effort",
            effort,
            "--add-dir",
            str(request.worktree_path),
            "--mode",
            "plan" if request.read_only else "accept-edits",
            "--output-format",
            "stream-json",
        ])
        if request.native_resume_id:
            cmd.extend(["--conversation", request.native_resume_id])
        if not request.read_only and (
            os.environ.get("ORCH_AGY_SKIP_PERMISSIONS", "").strip().lower()
            not in _SKIP_PERMISSIONS_OPT_OUT
        ):
            cmd.append("--dangerously-skip-permissions")
        # ``agy --print-timeout`` is optional.  Omit it when the owner did not
        # configure a finite hard deadline so the orchestrator's process
        # monitor remains the authoritative lifecycle controller.  In
        # particular, never coerce the no-deadline ``float("inf")`` sentinel.
        if math.isfinite(request.timeout_seconds):
            cmd.extend(["--print-timeout", f"{int(request.timeout_seconds)}s"])

        # Keep the native account route. An API-key configuration is an explicit refusal.
        settings = Path.home() / ".gemini/antigravity-cli/settings.json"
        if settings.is_file():
            try:
                config = json.loads(settings.read_text())
            except (OSError, ValueError):
                config = {}
            if config.get("modelProvider") == "gemini":
                return WorkerResult(provider=self.provider_name, model=request.model,
                    status=AttemptStatus.FAILED, summary="Antigravity subscription login required",
                    stdout="", stderr="API-key mode is disabled for subscription execution",
                    has_changes=False, outcome=DispatchOutcome.ELIGIBILITY_SKIP)
        excluded = set(request.inherit_env_exclude) | {"GEMINI_API_KEY", "GOOGLE_API_KEY"}
        kwargs: dict[str, Any] = {
            "cwd": request.worktree_path,
            "timeout": request.timeout_seconds,
            **streaming_process_kwargs(request),
        }
        kwargs["inherit_env_exclude"] = tuple(sorted(excluded))
        if request.env is not None:
            kwargs["env"] = {k: v for k, v in request.env.items() if k not in excluded}
        if use_stdin:
            kwargs["input_text"] = request.task

        # The generic autonomous execution seam: direct launch when no
        # boundary is installed, kernel containment (or a typed
        # fail-closed refusal) when one is.
        res: ProcessResult = launch_worker_process(cmd, runner=self.runner, **kwargs)

        parsed = parse_events(res.stdout)
        if res.control_interrupted:
            from saberops.models import WorkerInterruptionReason
            return WorkerResult(provider=self.provider_name, model=request.model,
                status=AttemptStatus.FAILED, summary="Interrupted for manual control",
                stdout=parsed.text, stderr=res.stderr, has_changes=False, usage=parsed.usage,
                duration_seconds=res.duration_seconds, provider_exit_code=res.exit_code,
            execution_quiescent=not res.termination_incomplete,
                provider_terminal_status=parsed.terminal_status,
                interruption_reason=WorkerInterruptionReason.ABORTED)
        changed_paths = GitManager.get_changed_paths(request.worktree_path)
        has_changes = len(changed_paths) > 0

        if res.timed_out:
            status = AttemptStatus.TIMEOUT
            outcome = DispatchOutcome.TIMEOUT
            error = f"Worker timed out after {request.timeout_seconds}s"
            summary = "Worker execution timed out"
        elif res.termination_incomplete:
            status = AttemptStatus.FAILED
            outcome = DispatchOutcome.PROVIDER_OR_RUNTIME_FAILURE
            error = (
                "Worker process group extinction could not be confirmed; "
                "worktree is not safely quiescent"
            )
            summary = error
        elif res.passed and parsed.terminal_status == "SUCCESS" and not parsed.denied:
            status = AttemptStatus.SUCCESS
            outcome = None
            error = None
            out_strip = parsed.text.strip()
            summary = out_strip[:300] if out_strip else "Worker completed successfully"
        else:
            status = AttemptStatus.FAILED
            outcome = DispatchOutcome.PROVIDER_OR_RUNTIME_FAILURE
            error = ("Antigravity denied a required tool step" if parsed.denied else
                     "Antigravity did not report terminal SUCCESS" if res.passed else
                     f"Worker exited with code {res.exit_code}")
            summary = (res.stderr.strip() or res.stdout.strip())[:300]

        return WorkerResult(
            provider=self.provider_name,
            model=request.model,
            status=status,
            summary=summary,
            stdout=parsed.text,
            stderr=res.stderr,
            changed_paths=changed_paths,
            has_changes=has_changes,
            error=error,
            duration_seconds=res.duration_seconds,
            outcome=outcome,
            provider_exit_code=res.exit_code,
            execution_quiescent=not res.termination_incomplete,
            provider_terminal_status=parsed.terminal_status,
            usage=parsed.usage,
            prompt_bytes=prompt_bytes,
            prompt_transport_used="stdin" if use_stdin else "argv",
        )
