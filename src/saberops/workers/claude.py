"""Claude Code native harness adapter using its own subscription login.

Uses the documented Claude Code CLI, not an Anthropic HTTP client, copied OAuth
credentials, custom agent loop, or SDK package. Billing remains provider-owned.
"""
from __future__ import annotations

import base64
import json
import mimetypes
import shutil
from collections.abc import Callable
from typing import Any

from saberops.git import GitManager
from saberops.models import AttemptStatus, DispatchOutcome, WorkerRequest, WorkerResult
from saberops.process import ProcessResult, launch_worker_process, run_process
from saberops.workers.base import WorkerAdapter, streaming_process_kwargs
from saberops.workers.env import hermetic_env
from saberops.workers.exhaustion import (
    ExhaustionVerdict,
    classify_claude,
    reset_to_iso,
)

# CLI configuration is invocation-local. Keep the native agent loop and system
# prompt, but prevent project/user settings from selecting an API credential or
# gateway instead of the explicitly selected subscription connection.
_SETTINGS = json.dumps({"forceLoginMethod": "claudeai", "disableAllHooks": True,
                        "promptSuggestionEnabled": False})


def _native_result(stdout: str) -> tuple[str, bool, str | None]:
    """Read only native completion evidence; an exit code alone is not success."""
    completed = False
    text: list[str] = []
    actual_model: str | None = None
    for line in stdout.splitlines():
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if not isinstance(message, dict):
            continue
        if message.get("type") == "system" and message.get("subtype") == "init":
            model = message.get("model")
            if isinstance(model, str) and model:
                actual_model = model
        if message.get("type") == "assistant":
            body = message.get("message", {})
            if isinstance(body, dict) and isinstance(body.get("content"), list):
                text.extend(item["text"] for item in body["content"]
                            if isinstance(item, dict) and item.get("type") == "text"
                            and isinstance(item.get("text"), str))
        if message.get("type") == "result":
            completed = (message.get("subtype") == "success"
                         and message.get("is_error") is False
                         and not message.get("permission_denials"))
            result = message.get("result")
            if isinstance(result, str) and result:
                text = [result]
    return "\n".join(text), completed, actual_model


class ClaudeCodeAdapter(WorkerAdapter):
    """Launch Claude Code in the admitted worktree with native tools/context."""

    factual_capabilities = frozenset({
        "streaming", "large_prompt_stdin", "read_only", "process_identity", "cancellation",
    })

    def __init__(self, runner: Callable[..., ProcessResult] = run_process,
                 claude_bin: str = "claude") -> None:
        self.runner = runner
        self.claude_bin = claude_bin

    @property
    def provider_name(self) -> str:
        return "anthropic"

    def is_available(self) -> bool:
        return self.runner is not run_process or shutil.which(self.claude_bin) is not None

    def max_prompt_bytes(self) -> int:
        # Documented CLI limit for plain-text piped stdin.
        return 10_000_000

    def prompt_transport(self) -> str:
        return "stdin"

    def run(self, request: WorkerRequest) -> WorkerResult:
        prompt_bytes = len(request.task.encode("utf-8"))
        def refusal(
            detail: str, *, timeout: bool = False, runtime: bool = False
        ) -> WorkerResult:
            return WorkerResult(
                provider=self.provider_name, model=request.model,
                status=AttemptStatus.TIMEOUT if timeout else AttemptStatus.FAILED,
                summary=detail, stdout="", stderr="", error=detail,
                has_changes=False, prompt_bytes=prompt_bytes, prompt_transport_used="stdin",
                outcome=(DispatchOutcome.TIMEOUT if timeout else
                         DispatchOutcome.PROVIDER_OR_RUNTIME_FAILURE if runtime else
                         DispatchOutcome.ELIGIBILITY_SKIP),
            )
        if not self.is_available():
            return refusal("Claude Code CLI not installed")
        if prompt_bytes > self.max_prompt_bytes():
            return refusal("Claude Code stdin prompt exceeds the supported 10 MB limit")

        # Fast mode is separately billed; never enable it for this connection.
        # The child environment is an explicit hermetic allowlist: only safe
        # runtime variables plus the request overlay survive, so API, gateway,
        # and cloud-provider credentials can never switch the billing source.
        overlay = dict(request.env or {})
        overlay["CLAUDE_CODE_DISABLE_FAST_MODE"] = "1"
        env = hermetic_env("claude", overlay)
        common = [self.claude_bin, "--setting-sources=", "--settings", _SETTINGS]
        auth = launch_worker_process(
            [*common, "auth", "status", "--json"], runner=self.runner,
            cwd=request.worktree_path, timeout=min(8.0, request.timeout_seconds),
            env=env, inherit_env=False,
        )
        try:
            login = json.loads(auth.stdout)
        except ValueError:
            login = None
        if auth.termination_incomplete:
            # No later launch is safe while this process group might be alive.
            return refusal("Claude login-check process termination could not be confirmed",
                           runtime=True)
        if auth.timed_out:
            return refusal("Claude subscription login check timed out", timeout=True)
        if (not auth.passed or not isinstance(login, dict)
                or login.get("loggedIn") is not True
                or login.get("authMethod") != "claude.ai"
                or login.get("apiProvider") != "firstParty"):
            return refusal("Claude subscription login required; sign in using claude auth login. "
                           "API, gateway, and cloud-provider fallback is disabled.")
        remaining = request.timeout_seconds - auth.duration_seconds
        if remaining <= 0:
            return refusal("Claude worker deadline elapsed during login check", timeout=True)
        argv = [*common, "--print", "--input-format", "text", "--output-format", "stream-json",
                "--verbose", "--include-partial-messages", "--model", request.model,
                "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']
        if not request.persist_native_session:
            argv.append("--no-session-persistence")
        if request.native_resume_id:
            argv.extend(["--resume", request.native_resume_id])
        if request.read_only:
            # No shell, write, edit, agent, or MCP tools in reviewer mode.
            argv.extend(["--permission-mode", "plan", "--tools", "Read,Glob,Grep"])
        else:
            argv.extend(["--permission-mode", "acceptEdits", "--allowedTools",
                         "Read,Edit,Write,Glob,Grep,Bash"])
        input_text = request.task
        if request.input_images:
            argv[argv.index("--input-format") + 1] = "stream-json"
            content: list[dict[str, Any]] = [{"type": "text", "text": request.task}]
            for image in request.input_images:
                content.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": mimetypes.guess_type(image.name)[0],
                            "data": base64.b64encode(image.read_bytes()).decode("ascii"),
                        },
                    }
                )
            input_text = (
                json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n"
            )
        kwargs: dict[str, Any] = streaming_process_kwargs(request)
        kwargs.update(cwd=request.worktree_path, timeout=remaining, env=env,
                      inherit_env=False, input_text=input_text)
        process = launch_worker_process(argv, runner=self.runner, **kwargs)
        output, completed, actual_model = _native_result(process.stdout)
        if process.control_interrupted:
            from saberops.models import WorkerInterruptionReason
            return WorkerResult(provider=self.provider_name, model=request.model,
                status=AttemptStatus.FAILED, summary="Interrupted for manual control",
                stdout=output, stderr=process.stderr, has_changes=False,
                interruption_reason=WorkerInterruptionReason.ABORTED,
                duration_seconds=process.duration_seconds)
        changes = GitManager.get_changed_paths(request.worktree_path)
        if process.timed_out:
            status, outcome, error = (AttemptStatus.TIMEOUT, DispatchOutcome.TIMEOUT,
                                      "Claude Code worker timed out")
        elif process.termination_incomplete:
            status, outcome, error = (AttemptStatus.FAILED,
                                      DispatchOutcome.PROVIDER_OR_RUNTIME_FAILURE,
                                      "Claude Code process termination could not be confirmed")
        elif process.passed and completed:
            status, outcome, error = AttemptStatus.SUCCESS, None, None
        else:
            status, outcome, error = (AttemptStatus.FAILED,
                                      DispatchOutcome.PROVIDER_OR_RUNTIME_FAILURE,
                                      "Claude Code did not report successful task completion")
        interruption_reason = None
        quota_reset_at = None
        quota_window: str | None = None
        quota_model_scope: str | None = None
        quota_reset_confidence: str | None = None
        if status is not AttemptStatus.SUCCESS:
            # Trap: a plan limit is subtype "success" with is_error true,
            # handled inside classify_claude -- never treat it as success.
            exhaustion = classify_claude(process.stdout, process.stderr)
            if exhaustion.verdict is ExhaustionVerdict.QUOTA_EXHAUSTED:
                # Continuation-after-quota path applies; RATE_LIMITED
                # keeps today's retry behaviour (falls through).
                from saberops.models import WorkerInterruptionReason
                interruption_reason = WorkerInterruptionReason.QUOTA_EXHAUSTED
                quota_reset_at = reset_to_iso(exhaustion.reset_at)
                quota_window = exhaustion.window
                quota_model_scope = exhaustion.model_scope
                quota_reset_confidence = exhaustion.reset_confidence
                reset_note = quota_reset_at or "unknown reset time"
                error = (
                    "Claude plan quota exhausted "
                    f"(retry after {reset_note}) [{exhaustion.rule} {exhaustion.confidence}]"
                )
        return WorkerResult(
            provider=self.provider_name, model=request.model, status=status,
            summary=error or output.strip()[:300] or "Claude Code completed",
            stdout=output, stderr=process.stderr, changed_paths=changes, has_changes=bool(changes),
            error=error, outcome=outcome,
            interruption_reason=interruption_reason, quota_reset_at=quota_reset_at,
            quota_window=quota_window, quota_model_scope=quota_model_scope,
            quota_reset_confidence=quota_reset_confidence,
            provider_exit_code=process.exit_code,
            execution_quiescent=not process.termination_incomplete,
            provider_terminal_status="SUCCESS" if completed else "FAILED",
            duration_seconds=auth.duration_seconds + process.duration_seconds,
            prompt_bytes=prompt_bytes, prompt_transport_used="stdin", actual_model=actual_model,
        )
