"""Claude Code native harness adapter using its own subscription login.

Uses the documented Claude Code CLI, not an Anthropic HTTP client, copied OAuth
credentials, custom agent loop, or SDK package. Billing remains provider-owned.
"""
from __future__ import annotations

import base64
import json
import mimetypes
import re
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from saberops.git import GitManager
from saberops.models import (
    AttemptStatus,
    DispatchOutcome,
    ProviderUsage,
    WorkerRequest,
    WorkerResult,
)
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

# Exit 0 without a terminal ``result`` frame means the provider stream stopped
# before it could report completion.  Name that condition instead of claiming
# the provider itself failed to complete, which hides a lost stream.
_TRUNCATED_MID_FRAME = (
    "Claude Code provider output was truncated: the stream ended mid-frame "
    "before the terminal result frame"
)
_TRUNCATED_NO_RESULT = (
    "Claude Code provider output was truncated: the stream ended before the "
    "terminal result frame"
)
_RECOVERED_NOTE = (
    "[Provider stream was truncated before the terminal result frame; "
    "completion was recovered from the durable Claude Code session transcript]"
)


def _token_count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _usage_from_payload(payload: object, *, source: str) -> ProviderUsage | None:
    """Read the CLI's own token counters from a ``usage`` object.

    Claude reports the turn's input/output counters and the cache counters as
    separate fields.  No counter is inferred: a field the CLI did not report
    stays ``None``, and ``total`` is only input + output when both are known
    (cache reads/writes are subsets of input, as in the Codex parser).
    """
    if not isinstance(payload, dict):
        return None
    input_tokens = _token_count(payload.get("input_tokens"))
    output_tokens = _token_count(payload.get("output_tokens"))
    cache_read = _token_count(payload.get("cache_read_input_tokens"))
    cache_write = _token_count(payload.get("cache_creation_input_tokens"))
    if all(
        value is None
        for value in (input_tokens, output_tokens, cache_read, cache_write)
    ):
        return None
    total = (
        input_tokens + output_tokens
        if input_tokens is not None and output_tokens is not None
        else None
    )
    return ProviderUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cache_read,
        total_tokens=total,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        reasoning_tokens=None,
        usage_source=source,
        raw_payload=json.dumps({"usage": payload}, sort_keys=True)[:4000],
    )


@dataclass(frozen=True)
class _NativeOutput:
    """Native completion evidence parsed from the CLI's stream-json stdout."""

    text: str
    completed: bool
    actual_model: str | None
    saw_result: bool
    ends_mid_frame: bool
    session_id: str | None
    usage: list[ProviderUsage]


def _native_result(stdout: str) -> _NativeOutput:
    """Read only native completion evidence; an exit code alone is not success."""
    completed = False
    saw_result = False
    text: list[str] = []
    actual_model: str | None = None
    session_id: str | None = None
    usages: list[ProviderUsage] = []
    lines = stdout.splitlines()
    for line in lines:
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if not isinstance(message, dict):
            continue
        frame_session = message.get("session_id")
        if session_id is None and isinstance(frame_session, str) and frame_session:
            session_id = frame_session
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
            saw_result = True
            completed = (message.get("subtype") == "success"
                         and message.get("is_error") is False
                         and not message.get("permission_denials"))
            result = message.get("result")
            if isinstance(result, str) and result:
                text = [result]
            usage = _usage_from_payload(
                message.get("usage"), source="claude_result_json"
            )
            usages.append(
                usage
                if usage is not None
                else ProviderUsage(usage_source="claude_result_without_usage")
            )
    ends_mid_frame = False
    if lines and not stdout.endswith(("\n", "\r")):
        try:
            json.loads(lines[-1])
        except ValueError:
            # The last bytes captured are not a complete JSON frame: the
            # provider stream stopped in the middle of a write.
            ends_mid_frame = True
    return _NativeOutput(
        text="\n".join(text),
        completed=completed,
        actual_model=actual_model,
        saw_result=saw_result,
        ends_mid_frame=ends_mid_frame,
        session_id=session_id,
        usage=usages,
    )


def _entry_epoch(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _session_file(
    worktree: Path, env: dict[str, str], session_id: str | None, since: float
) -> Path | None:
    """Locate the CLI's own durable transcript for this dispatch, if any.

    Claude Code appends every turn to ``<config>/projects/<encoded cwd>/
    <session>.jsonl``.  That file is written by the CLI itself through a
    different path than stdout, so it survives a truncated stdout flush.
    """
    config = env.get("CLAUDE_CONFIG_DIR")
    home = env.get("HOME")
    if config:
        projects = Path(config) / "projects"
    elif home:
        projects = Path(home) / ".claude" / "projects"
    else:
        return None
    encoded = re.sub(r"[^A-Za-z0-9]", "-", str(worktree.resolve()))
    directory = projects / encoded
    if not directory.is_dir():
        return None
    if session_id:
        candidate = directory / f"{session_id}.jsonl"
        return candidate if candidate.is_file() else None
    newest: tuple[float, Path] | None = None
    for path in directory.glob("*.jsonl"):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime + 1.0 < since:
            continue
        if newest is None or mtime > newest[0]:
            newest = (mtime, path)
    return newest[1] if newest is not None else None


@dataclass(frozen=True)
class _RecoveredTurn:
    """The durable transcript's completed turn: its text and token counters."""

    text: str
    usage: ProviderUsage | None


def _completed_turn(path: Path, since: float) -> _RecoveredTurn | None:
    """Return the final assistant turn only when it completed after *since*.

    Completion requires the last conversational entry to be an assistant
    message that ended its turn normally (``end_turn``, no error).  The
    timestamp guard keeps a resumed session's earlier turn from being read as
    this dispatch's completion.  The message's own ``usage`` object carries the
    same counters the lost terminal result frame would have reported, so the
    recovered dispatch still records exact usage instead of ``unknown``.
    """
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    last: tuple[str, dict[str, Any]] | None = None
    for line in raw.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        kind = entry.get("type")
        if kind not in ("assistant", "user"):
            continue
        if isinstance(entry.get("message"), dict):
            last = (kind, entry)
    if last is None or last[0] != "assistant":
        return None
    entry = last[1]
    message = entry["message"]
    if message.get("stop_reason") != "end_turn" or message.get("is_error") is True:
        return None
    stamp = _entry_epoch(entry.get("timestamp"))
    if stamp is None or stamp < since:
        return None
    content = message.get("content")
    if not isinstance(content, list):
        return None
    text = "".join(
        item["text"] for item in content
        if isinstance(item, dict) and item.get("type") == "text"
        and isinstance(item.get("text"), str)
    )
    if not text:
        return None
    usage = _usage_from_payload(
        message.get("usage"), source="claude_transcript_json"
    )
    return _RecoveredTurn(text=text, usage=usage)


def _recover_completion(
    worktree: Path, env: dict[str, str], session_id: str | None, since: float
) -> _RecoveredTurn | None:
    path = _session_file(worktree, env, session_id, since)
    if path is None:
        return None
    return _completed_turn(path, since)


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
        launched_at = time.time()
        process = launch_worker_process(argv, runner=self.runner, **kwargs)
        native = _native_result(process.stdout)
        output = native.text
        # A clean exit without any terminal result frame is the signature of a
        # provider stream whose tail never reached the pipe (Claude Code
        # 2.1.330 exits before flushing its async stdout writes).  The CLI's
        # own durable transcript still records the completed turn, so recover
        # the terminal evidence from there instead of failing the dispatch.
        recovered: _RecoveredTurn | None = None
        if process.passed and not native.saw_result:
            recovered = _recover_completion(
                request.worktree_path, env, native.session_id, launched_at
            )
            if recovered is not None:
                output = recovered.text
        stderr_text = process.stderr
        if recovered is not None:
            stderr_text = (
                f"{stderr_text}\n{_RECOVERED_NOTE}" if stderr_text else _RECOVERED_NOTE
            )
        if process.control_interrupted:
            from saberops.models import WorkerInterruptionReason
            return WorkerResult(provider=self.provider_name, model=request.model,
                status=AttemptStatus.FAILED, summary="Interrupted for manual control",
                stdout=output, stderr=stderr_text, has_changes=False,
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
        elif process.passed and native.completed:
            status, outcome, error = AttemptStatus.SUCCESS, None, None
        elif process.passed and recovered is not None:
            # The model finished the turn and the CLI persisted that turn, but
            # its stdout lost the tail.  Complete on the durable native
            # evidence and keep the truncation visible in stderr.
            status, outcome, error = AttemptStatus.SUCCESS, None, None
        elif process.passed and process.output_incomplete:
            # The child exited cleanly, but not every byte it wrote reached
            # the capture, so native terminal evidence may have been lost.
            # Never read that as success: report the real cause instead of
            # claiming the provider did not complete.
            status, outcome, error = (AttemptStatus.FAILED,
                                      DispatchOutcome.PROVIDER_OR_RUNTIME_FAILURE,
                                      "Claude Code output capture was incomplete; "
                                      "the terminal result frame was not observed")
        elif process.passed and not native.saw_result:
            # The provider stream stopped before its terminal result frame and
            # left no durable completion behind: this is truncated provider
            # output, not a provider that declined to complete the task.
            status, outcome, error = (AttemptStatus.FAILED,
                                      DispatchOutcome.PROVIDER_OR_RUNTIME_FAILURE,
                                      _TRUNCATED_MID_FRAME if native.ends_mid_frame
                                      else _TRUNCATED_NO_RESULT)
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
            stdout=output, stderr=stderr_text, changed_paths=changes, has_changes=bool(changes),
            error=error, outcome=outcome,
            interruption_reason=interruption_reason, quota_reset_at=quota_reset_at,
            quota_window=quota_window, quota_model_scope=quota_model_scope,
            quota_reset_confidence=quota_reset_confidence,
            provider_exit_code=process.exit_code,
            execution_quiescent=not process.termination_incomplete,
            provider_terminal_status=(
                "SUCCESS" if native.completed or recovered is not None else "FAILED"
            ),
            duration_seconds=auth.duration_seconds + process.duration_seconds,
            prompt_bytes=prompt_bytes, prompt_transport_used="stdin",
            actual_model=native.actual_model,
            usage=(
                [recovered.usage]
                if recovered is not None and recovered.usage is not None
                else native.usage
            ),
        )
