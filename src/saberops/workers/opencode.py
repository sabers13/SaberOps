"""OpenCode worker adapter for local CLI execution."""

from __future__ import annotations

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
from saberops.telemetry import parse_opencode_jsonl
from saberops.workers.base import WorkerAdapter, streaming_process_kwargs
from saberops.workers.instruction_compat import (
    InstructionCompatibility,
    instruction_compatibility_for_opencode_model,
)

__all__ = ["ORCH_PROMPT_ARG_LIMIT", "OpenCodeAdapter"]


class OpenCodeAdapter(WorkerAdapter):
    """Worker adapter dispatching tasks via OpenCode CLI."""

    capability_transport = "DIRECT_CLI"
    factual_capabilities = frozenset(
        {"streaming", "large_prompt_stdin", "structured_usage", "process_identity", "cancellation"}
    )

    def __init__(
        self,
        runner: Callable[..., ProcessResult] = run_process,
        opencode_bin: str = "opencode",
    ) -> None:
        self.runner = runner
        self.opencode_bin = opencode_bin

    @property
    def provider_name(self) -> str:
        return "opencode"

    def is_available(self) -> bool:
        """Check if OpenCode binary is available on PATH or custom runner is provided."""
        if self.runner is not run_process:
            return True
        return shutil.which(self.opencode_bin) is not None

    def max_prompt_bytes(self) -> int | None:
        """Return ``None``: OpenCode has a verified non-argv prompt transport.

        The installed OpenCode CLI (``opencode run``) reads the ``message``
        positional array, and when process stdin is not a TTY it also reads
        piped stdin text and uses it as (part of) the message -- verified
        directly against the installed ``opencode`` binary: piping text with
        no ``message`` positional argument at all proceeds straight to
        session creation and prompt dispatch (it does not hit the CLI's
        "You must provide a message or a command" guard), so an omitted
        positional plus piped stdin is a safe, verified large-prompt
        transport. There is therefore no byte ceiling the caller must
        preflight-skip against; :meth:`run` chooses argv or stdin per call
        based on the orchestrator prompt-argument safety threshold
        (``ORCH_PROMPT_ARG_LIMIT``).
        """
        return None

    def prompt_transport(self) -> str:
        return "stdin"

    def prompt_transport_for(self, prompt_bytes: int) -> str:
        """Planned transport for this exact size: argv below the threshold, else stdin."""
        return "stdin" if prompt_bytes > get_prompt_arg_limit() else "argv"

    def instruction_compatibility(self, model: str) -> InstructionCompatibility:
        """Report factual project-instruction compatibility for one model id.

        Transport evidence only, never a routing decision: the return
        value describes whether THIS adapter's OpenCode CLI transport can
        faithfully carry the canonical project instruction contract to
        the upstream addressed by ``model`` (see
        :mod:`saberops.workers.instruction_compat` for the
        reproduced General Compute constraint).  The adapter never
        chooses a replacement model or alters the routing chain; Orch
        policy / C07 preflight consumes this evidence and owns
        eligibility.
        """
        return instruction_compatibility_for_opencode_model(model)

    def run(self, request: WorkerRequest) -> WorkerResult:
        """Dispatch task to OpenCode in the specified worktree."""
        if not self.is_available():
            return WorkerResult(
                provider=self.provider_name,
                model=request.model,
                status=AttemptStatus.FAILED,
                summary="OpenCode CLI not found on system",
                stdout="",
                stderr="opencode binary not found on PATH",
                changed_paths=[],
                has_changes=False,
                error="OpenCode binary not found on PATH",
                outcome=DispatchOutcome.PROVIDER_OR_RUNTIME_FAILURE,
            )

        prompt_bytes = len(request.task.encode("utf-8"))
        use_stdin = prompt_bytes > get_prompt_arg_limit()

        # Small prompts keep the efficient legacy argv path unchanged: the
        # task is passed as the ``message`` positional. Large prompts omit
        # the positional entirely and pipe the task on stdin instead --
        # verified against the installed OpenCode CLI, which reads piped
        # stdin as the message when no positional is given (see
        # max_prompt_bytes docstring). This never risks E2BIG for a large
        # prompt: the oversized text never enters argv.
        cmd = [self.opencode_bin, "run"]
        if not use_stdin:
            cmd.append(request.task)
        cmd.extend(
            [
                "--model",
                request.model,
                "--dir",
                str(request.worktree_path),
                "--pure",
                "--format",
                "json",
            ]
        )
        # Installed OpenCode 1.18.25 exposes provider-native usage on JSONL
        # step_finish.part.tokens (including cache read/write). Missing final
        # events remain explicit unknowns; prompt bytes are never estimated.

        kwargs: dict[str, Any] = {
            "cwd": request.worktree_path,
            "timeout": request.timeout_seconds,
            **streaming_process_kwargs(request),
        }
        if request.env is not None:
            kwargs["env"] = request.env
        if use_stdin:
            kwargs["input_text"] = request.task

        # The generic autonomous execution seam: direct launch when no
        # boundary is installed, kernel containment (or a typed
        # fail-closed refusal) when one is.
        res: ProcessResult = launch_worker_process(cmd, runner=self.runner, **kwargs)
        transport_used = "stdin" if use_stdin else "argv"

        display_stdout, usage = parse_opencode_jsonl(res.stdout)
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
            out_strip = display_stdout.strip()
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
            stdout=display_stdout,
            stderr=res.stderr,
            changed_paths=changed_paths,
            has_changes=has_changes,
            error=error,
            duration_seconds=res.duration_seconds,
            prompt_bytes=prompt_bytes,
            prompt_transport_used=transport_used,
            outcome=outcome,
            usage=usage,
        )
