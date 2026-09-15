"""Antigravity (AGY) worker adapter for local CLI execution."""

from __future__ import annotations

import math
import os
import shutil
from collections.abc import Callable
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
from saberops.workers.base import WorkerAdapter, streaming_process_kwargs

__all__ = ["AntigravityAdapter", "ORCH_PROMPT_ARG_LIMIT"]

_SKIP_PERMISSIONS_OPT_OUT = frozenset({"0", "false", "no", "off"})


class AntigravityAdapter(WorkerAdapter):
    """Worker adapter dispatching tasks via Antigravity (agy) CLI."""

    capability_transport = "DIRECT_CLI"
    factual_capabilities = frozenset({"streaming", "process_identity", "cancellation"})

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

    def max_prompt_bytes(self) -> int:
        """Return Antigravity's verified argv prompt capability.

        Antigravity has no verified non-argv prompt transport: the prompt is
        always passed as a positional ``argv`` argument. Its verified capability
        is the orchestrator prompt-argument limit (``ORCH_PROMPT_ARG_LIMIT``,
        default ``DEFAULT_PROMPT_ARG_LIMIT``). Callers must preflight-skip
        oversized prompts rather than launch a process that cannot receive them.
        """
        return get_prompt_arg_limit()

    def prompt_transport(self) -> str:
        return "argv"

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
        limit = get_prompt_arg_limit()
        if prompt_bytes > limit:
            msg = (
                f"{self.provider_name} prompt size ({prompt_bytes} bytes) exceeds configured "
                f"limit ({limit} bytes); provider adapter has no verified non-argv prompt transport"
            )
            return WorkerResult(
                provider=self.provider_name,
                model=request.model,
                status=AttemptStatus.FAILED,
                summary=msg,
                stdout="",
                stderr=msg,
                changed_paths=[],
                has_changes=False,
                error=msg,
                outcome=DispatchOutcome.ELIGIBILITY_SKIP,
            )

        # C11-D: a typed autonomous effort always wins over ambient
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
            effort = os.environ.get("ORCH_AGY_EFFORT") or "medium"

        cmd = [
            self.agy_bin,
            "--print",
            request.task,
            "--model",
            request.model,
            "--effort",
            effort,
            "--add-dir",
            str(request.worktree_path),
            "--mode",
            "accept-edits",
        ]
        if (
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

        kwargs: dict[str, Any] = {
            "cwd": request.worktree_path,
            "timeout": request.timeout_seconds,
            **streaming_process_kwargs(request),
        }
        if request.env is not None:
            kwargs["env"] = request.env

        # The generic autonomous execution seam: direct launch when no
        # boundary is installed, kernel containment (or a typed
        # fail-closed refusal) when one is.
        res: ProcessResult = launch_worker_process(cmd, runner=self.runner, **kwargs)

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
        elif res.passed:
            status = AttemptStatus.SUCCESS
            outcome = None
            error = None
            out_strip = res.stdout.strip()
            summary = out_strip[:300] if out_strip else "Worker completed successfully"
        else:
            status = AttemptStatus.FAILED
            outcome = DispatchOutcome.PROVIDER_OR_RUNTIME_FAILURE
            error = f"Worker exited with code {res.exit_code}"
            summary = (res.stderr.strip() or res.stdout.strip())[:300]

        return WorkerResult(
            provider=self.provider_name,
            model=request.model,
            status=status,
            summary=summary,
            stdout=res.stdout,
            stderr=res.stderr,
            changed_paths=changed_paths,
            has_changes=has_changes,
            error=error,
            duration_seconds=res.duration_seconds,
            outcome=outcome,
        )
