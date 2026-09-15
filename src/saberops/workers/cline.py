"""Cline worker adapter for local CLI execution via the ClinePass provider.

One Orch attempt corresponds to exactly one controlled Cline CLI dispatch.
Cline is an execution provider ONLY: routing, retry, escalation, review,
gate, and lifecycle authority remain with Orch.  The adapter never uses
Cline team/spawn/subagent facilities and never delegates orchestration
decisions to Cline.

Transport evidence (verified against the installed Cline CLI 3.0.60): in
``--json`` headless mode the CLI rejects a piped-stdin prompt with
``JSON output mode requires a prompt argument`` -- the task MUST travel
through ``argv`` as a single positional argument.  The adapter therefore
reports the orchestrator prompt-argument limit as its deterministic byte
ceiling and preflight-skips oversized prompts instead of launching a
process that cannot receive them.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from typing import Any

from saberops.git import GitManager
from saberops.models import (
    AttemptStatus,
    DispatchOutcome,
    WorkerInterruptionReason,
    WorkerRequest,
    WorkerResult,
)
from saberops.process import (
    ORCH_PROMPT_ARG_LIMIT,
    ProcessResult,
    get_prompt_arg_limit,
    launch_worker_process,
    run_process,
)
from saberops.telemetry import ClineStructuredRun, parse_cline_jsonl
from saberops.workers.base import WorkerAdapter, streaming_process_kwargs

__all__ = ["CLINE_PASS_PROVIDER_ID", "ClineAdapter", "ORCH_PROMPT_ARG_LIMIT"]

# Cline-side provider id: Orch provider "cline" always dispatches through the
# authenticated ClinePass provider (-P), with the model carrying the
# cline-pass/<model-id> identity.  This is a fixed CLI argument, NOT a model
# catalog: any cline-pass/<model-id> accepted by the authenticated account is
# dispatchable.
CLINE_PASS_PROVIDER_ID = "cline-pass"

# Cline reports terminal state in the structured run_result, NOT the exit
# code: an error finish (e.g. unknown provider) still exits 0, and other
# non-completed terminal reasons (aborted, max_iterations, mistake_limit)
# may also return without a failing exit code.  Success is therefore a
# strict positive allowlist: ONLY "completed" on a clean process completion
# with a terminal run_result present is a verified successful completion.
# Any other finish reason -- including unknown future values -- fails closed.
_CLINE_SUCCESS_FINISH_REASONS = frozenset({"completed"})

# Infrastructure failure patterns.  These classify PROVIDER- or account-side
# conditions and must NEVER become capability evidence: a model failing to
# answer because of auth, quota, outage, transport, or runtime problems did
# not demonstrate a model-capability failure.
_AUTH_FAILURE_PATTERNS: tuple[str, ...] = (
    "unknown or disabled provider",
    "not authenticated",
    "unauthenticated",
    "unauthorized",
    "authentication required",
    "invalid api key",
    "api key",
    "login required",
    "sign in",
    "credentials",
)
_QUOTA_EXHAUSTION_PATTERNS: tuple[str, ...] = (
    "quota",
    "usage limit",
    "rate limit",
    "insufficient credit",
    "out of credit",
    "billing",
    "exceeded",
)


def _sink(_line: str) -> None:
    """No-op stdout sink forcing run_process's process-group streaming path."""
    return None


def _classify_provider_error(evidence: str) -> str:
    """Return a human classification prefix for provider-side failure evidence.

    All outcomes remain infrastructure conditions (DispatchOutcome is decided
    by the caller); this only sharpens the error message so auth, quota, and
    service failures are distinguishable from CLI runtime failures.
    """
    lower = evidence.lower()
    if any(pattern in lower for pattern in _AUTH_FAILURE_PATTERNS):
        return "Cline authentication/provider failure"
    if any(pattern in lower for pattern in _QUOTA_EXHAUSTION_PATTERNS):
        return "ClinePass quota/usage exhaustion or provider service failure"
    return "Cline provider/service failure"


class ClineAdapter(WorkerAdapter):
    """Worker adapter dispatching tasks via the authenticated Cline CLI."""

    capability_transport = "DIRECT_CLI"
    factual_capabilities = frozenset(
        {"streaming", "structured_usage", "process_identity", "cancellation"}
    )

    def __init__(
        self,
        runner: Callable[..., ProcessResult] = run_process,
        cline_bin: str | None = None,
    ) -> None:
        self.runner = runner
        self.cline_bin = (
            cline_bin if cline_bin is not None else (os.environ.get("ORCH_CLINE_BIN") or "cline")
        )

    @property
    def provider_name(self) -> str:
        return "cline"

    def is_available(self) -> bool:
        """Check if Cline binary is available on PATH or custom runner is provided."""
        if self.runner is not run_process:
            return True
        return shutil.which(self.cline_bin) is not None

    def max_prompt_bytes(self) -> int:
        """Return Cline's verified argv prompt capability.

        The installed Cline CLI 3.0.60 does not accept a piped-stdin prompt in
        headless ``--json`` mode (verified locally: every stdin variant is
        rejected with ``JSON output mode requires a prompt argument or piped
        stdin`` while the prompt is in fact only read from argv), so the
        prompt must travel as a single positional argument.  The verified
        capability is the orchestrator prompt-argument limit
        (``ORCH_PROMPT_ARG_LIMIT``).  Callers must preflight-skip oversized
        prompts rather than launch a process that cannot receive them.
        """
        return get_prompt_arg_limit()

    def prompt_transport(self) -> str:
        return "argv"

    def run(self, request: WorkerRequest) -> WorkerResult:
        """Dispatch task to the Cline CLI non-interactively in the specified worktree."""
        if not self.is_available():
            return WorkerResult(
                provider=self.provider_name,
                model=request.model,
                status=AttemptStatus.FAILED,
                summary="Cline CLI not found on system",
                stdout="",
                stderr="cline binary not found on PATH",
                changed_paths=[],
                has_changes=False,
                error="cline binary not found on PATH",
                outcome=DispatchOutcome.PROVIDER_OR_RUNTIME_FAILURE,
            )

        prompt_bytes = len(request.task.encode("utf-8"))
        limit = get_prompt_arg_limit()
        if prompt_bytes > limit:
            msg = (
                f"{self.provider_name} prompt size ({prompt_bytes} bytes) exceeds configured "
                f"limit ({limit} bytes); provider adapter has no verified non-argv prompt "
                "transport (Cline --json mode requires the prompt as an argv argument)"
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

        # Explicit per-dispatch model: -m always carries the requested model,
        # so the user's persistent Cline default model is never assumed or
        # mutated.  -c pins execution to the attempt worktree; the adapter
        # never targets the owner's canonical checkout.
        cmd = [
            self.cline_bin,
            "-P",
            CLINE_PASS_PROVIDER_ID,
            "-m",
            request.model,
            "--json",
            "--yolo",
            "-c",
            str(request.worktree_path),
            request.task,
        ]

        kwargs: dict[str, Any] = {
            "cwd": request.worktree_path,
            "timeout": request.timeout_seconds,
            # A sink keeps every Cline dispatch on run_process's streaming
            # path: start_new_session process-group ownership, descendant
            # extinction confirmation, and monitor integration hold even when
            # the caller requested no live streaming.  Without it, a bare
            # dispatch would fall back to the bare subprocess.run path with
            # no group-ownership guarantees.
            "on_stdout": request.on_stdout if request.on_stdout is not None else _sink,
            **streaming_process_kwargs(request),
        }
        if request.env is not None:
            kwargs["env"] = request.env

        # The generic autonomous execution seam: direct launch when no
        # boundary is installed, kernel containment (or a typed
        # fail-closed refusal) when one is.
        res: ProcessResult = launch_worker_process(cmd, runner=self.runner, **kwargs)
        return self._finalize(request, res)

    def _finalize(self, request: WorkerRequest, res: ProcessResult) -> WorkerResult:
        parsed = parse_cline_jsonl(res.stdout)
        changed_paths = GitManager.get_changed_paths(request.worktree_path)
        has_changes = len(changed_paths) > 0
        status, outcome, error, summary = self._classify(request=request, res=res, parsed=parsed)
        display_stdout = parsed.text
        if summary is None:
            out_strip = display_stdout.strip()
            summary = out_strip[:300] if out_strip else "Worker completed successfully"
        interruption_reason: WorkerInterruptionReason | None = None
        if parsed.finish_reason == "context_exhausted":
            interruption_reason = WorkerInterruptionReason.CONTEXT_EXHAUSTED
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
            prompt_bytes=len(request.task.encode("utf-8")),
            prompt_transport_used="argv",
            actual_provider=parsed.actual_provider,
            actual_model=parsed.actual_model,
            outcome=outcome,
            usage=parsed.usage,
            interruption_reason=interruption_reason,
        )

    def _classify(
        self,
        request: WorkerRequest,
        res: ProcessResult,
        parsed: ClineStructuredRun,
    ) -> tuple[AttemptStatus, DispatchOutcome | None, str | None, str | None]:
        """Map process + structured evidence onto the existing Orch taxonomy.

        Infrastructure and provider-side conditions (timeout, termination
        uncertainty, auth, quota, outage, CLI crash, malformed/incomplete
        structured output) map to ``PROVIDER_OR_RUNTIME_FAILURE`` (or
        ``TIMEOUT``), never to capability evidence.  The returned summary is
        non-None only for a requested-vs-actual identity discrepancy, which
        is preserved as visible evidence instead of rewriting either value.
        """
        discrepancy: str | None = None
        if parsed.actual_model is not None and parsed.actual_model != request.model:
            discrepancy = f"[actual: {parsed.actual_provider or '?'}/{parsed.actual_model}]"

        if res.timed_out:
            return (
                AttemptStatus.TIMEOUT,
                DispatchOutcome.TIMEOUT,
                f"Worker timed out after {request.timeout_seconds}s",
                discrepancy,
            )
        if res.termination_incomplete:
            msg = (
                "Worker process group extinction could not be confirmed; "
                "worktree is not safely quiescent"
            )
            return (
                AttemptStatus.FAILED,
                DispatchOutcome.PROVIDER_OR_RUNTIME_FAILURE,
                msg,
                msg,
            )
        if not parsed.saw_run_result:
            # Cline exits 0 even for error finishes, so a missing terminal
            # run_result is a malformed/incomplete structured result: never a
            # success and never a capability signal.
            evidence = "; ".join(parsed.error_messages) or res.stderr.strip()
            classification = (
                _classify_provider_error(evidence) if evidence else "Cline CLI/runtime failure"
            )
            msg = f"{classification}: structured run_result missing (exit code {res.exit_code})"
            return (
                AttemptStatus.FAILED,
                DispatchOutcome.PROVIDER_OR_RUNTIME_FAILURE,
                msg,
                msg,
            )
        if parsed.finish_reason is None:
            msg = (
                "Cline structured result incomplete: terminal run_result present "
                "but finish reason missing"
            )
            return (
                AttemptStatus.FAILED,
                DispatchOutcome.PROVIDER_OR_RUNTIME_FAILURE,
                msg,
                msg,
            )
        if parsed.finish_reason not in _CLINE_SUCCESS_FINISH_REASONS:
            # Fail closed on every non-completed terminal reason ("error",
            # "aborted", "max_iterations", "mistake_limit", and any unknown
            # future value).  These states -- notably "aborted" -- can return
            # with exit code 0, so the structured reason is authoritative.
            # They are infrastructure/termination conditions, never
            # capability evidence.  Bounded evidence preserves both the
            # actual finish reason and any provider-side error message.
            evidence = "; ".join(parsed.error_messages)[:300]
            msg = (
                f"Cline did not complete: terminal finish reason "
                f"'{parsed.finish_reason}' (exit code {res.exit_code})"
            )
            if evidence:
                msg = f"{msg}: {evidence}"
            return (
                AttemptStatus.FAILED,
                DispatchOutcome.PROVIDER_OR_RUNTIME_FAILURE,
                msg,
                msg,
            )
        if not res.passed:
            # Structured finish but unclean process termination: the process
            # contract failed, so the attempt is not safely complete.
            msg = f"Worker exited with code {res.exit_code} after structured finish"
            return (
                AttemptStatus.FAILED,
                DispatchOutcome.PROVIDER_OR_RUNTIME_FAILURE,
                msg,
                msg,
            )
        return AttemptStatus.SUCCESS, None, None, discrepancy
