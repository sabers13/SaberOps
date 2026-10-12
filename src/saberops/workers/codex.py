"""Codex worker adapter for local CLI execution."""

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
from saberops.telemetry import parse_codex_jsonl
from saberops.workers.base import WorkerAdapter, streaming_process_kwargs
from saberops.workers.env import hermetic_env
from saberops.workers.exhaustion import (
    ExhaustionVerdict,
    classify_codex,
    reset_to_iso,
)

__all__ = ["CodexAdapter", "ORCH_PROMPT_ARG_LIMIT"]


# Billing-relevant ``-c`` pins applied to every Codex invocation.  Each
# key is confirmed to exist in the installed Codex CLI
# (0.162.x; see the adapter docstring for evidence):
# * ``forced_login_method="chatgpt"`` forces ChatGPT login so an API key
#   can never switch the turn to API billing;
# * ``model_provider="openai"`` pins the built-in OpenAI provider so a
#   user config cannot redirect the turn to another provider;
# * ``service_tier="default"`` pins the standard service tier so a user
#   config cannot opt the turn into separately billed Fast mode.
# ``-c`` overrides have the highest config precedence (above project and
# user config files), and project ``.codex/`` layers cannot override
# ``model_provider``/``model_providers`` at all.
# ``--ignore-user-config`` is passed on every invocation (see the adapter
# docstring for evidence): it drops ``$CODEX_HOME/config.toml`` -- and
# with it the ``[projects.*]`` trust table that gates project ``.codex/``
# layers -- while ChatGPT auth still loads from ``CODEX_HOME``.
_IGNORE_USER_CONFIG_FLAG: tuple[str, ...] = ("--ignore-user-config",)

_DIRECT_COVERRIDE_FLAGS: tuple[str, ...] = (
    "-c",
    'forced_login_method="chatgpt"',
    "-c",
    'model_provider="openai"',
    "-c",
    'service_tier="default"',
)


class CodexAdapter(WorkerAdapter):
    """Worker adapter dispatching tasks via Codex CLI.

    Every invocation pins billing-relevant configuration via ``-c``
    overrides (highest config precedence, above user and project
    ``config.toml`` layers), so user or project config files can never
    change the billing of a worker turn.  Key evidence, all verified
    against the installed Codex CLI 0.162.0 plus the official
    configuration reference
    (https://developers.openai.com/codex/config-reference):
    ``forced_login_method`` accepts ``chatgpt | api`` ("Restrict Codex to
    a specific authentication method"); ``model_provider`` is a string
    ("Provider id from model_providers (default: openai)"); the
    ``-c key=value`` flag "override[s] a configuration value that would
    otherwise be loaded from ``~/.codex/config.toml``" (``codex exec
    --help``); ``service_tier="default"`` selects standard speed (the
    installed binary's own app-server JSON schema,
    ``TurnStartParams.serviceTierForTurn``: 'Use "default" for standard
    speed'); and project ``.codex/`` layers load "only when you trust
    the project" and can never override ``model_provider`` /
    ``model_providers``.

    ``--ignore-user-config`` is passed on every invocation so project
    trust can never sneak in through the user config: the installed
    Codex CLI 0.162.0 documents it as "Do not load
    ``$CODEX_HOME/config.toml``; auth still uses ``CODEX_HOME``"
    (``codex exec --help``), which closes the project-config trust gap
    (an unlisted worktree inherits no ``[projects.*]`` trust entry, and
    with the user config ignored there is no trust table at all, so
    project ``.codex/`` layers stay untrusted/unloaded).  Auth still
    loads because the credential material lives outside ``config.toml``:
    ``auth.json`` sits directly under ``CODEX_HOME`` (alongside, not
    inside, ``config.toml``), the binary resolves the "auth.json path
    (file backend/fallback)" plus keyring storage independently of the
    config file, and this host's ``config.toml`` does not set
    ``cli_auth_credentials_store`` (so the compiled-in default applies
    with or without the user config).
    """

    capability_transport = "DIRECT_CLI"
    factual_capabilities = frozenset(
        {
            "streaming",
            "large_prompt_stdin",
            "read_only",
            "structured_usage",
            "process_identity",
            "cancellation",
        }
    )

    def __init__(
        self,
        runner: Callable[..., ProcessResult] = run_process,
        codex_bin: str = "codex",
    ) -> None:
        self.runner = runner
        self.codex_bin = codex_bin

    @property
    def provider_name(self) -> str:
        return "codex"

    def is_available(self) -> bool:
        """Check if Codex binary is available on PATH or custom runner is provided."""
        if self.runner is not run_process:
            return True
        return shutil.which(self.codex_bin) is not None

    def prompt_transport(self) -> str:
        return "stdin"

    def prompt_transport_for(self, prompt_bytes: int) -> str:
        """Planned transport for this exact size: argv below the threshold, else stdin."""
        return "stdin" if prompt_bytes > get_prompt_arg_limit() else "argv"

    def run(self, request: WorkerRequest) -> WorkerResult:
        """Dispatch task to Codex non-interactively in the specified worktree."""
        if not self.is_available():
            return WorkerResult(
                provider=self.provider_name,
                model=request.model,
                status=AttemptStatus.FAILED,
                summary="Codex CLI not found on system",
                stdout="",
                stderr="codex binary not found on PATH",
                changed_paths=[],
                has_changes=False,
                error="Codex binary not found on PATH",
                outcome=DispatchOutcome.PROVIDER_OR_RUNTIME_FAILURE,
            )

        prompt_bytes = len(request.task.encode("utf-8"))
        use_stdin = prompt_bytes > get_prompt_arg_limit()
        prompt_arg = "-" if use_stdin else request.task

        sandbox_mode = "read-only" if request.read_only else "workspace-write"
        cmd = [
            self.codex_bin,
            "exec",
            *_IGNORE_USER_CONFIG_FLAG,
            prompt_arg,
            "--model",
            request.model,
            "--cd",
            str(request.worktree_path),
            "--sandbox",
            sandbox_mode,
            "--json",
        ]
        if not request.persist_native_session:
            cmd.append("--ephemeral")
        if request.native_resume_id:
            cmd = [self.codex_bin, "--cd", str(request.worktree_path),
                    "--sandbox", sandbox_mode, "exec", "resume", request.native_resume_id,
                    *_IGNORE_USER_CONFIG_FLAG,
                    prompt_arg, "--model", request.model, "--json"]
        if request.read_only:
            cmd.append("--skip-git-repo-check")
        for image in request.input_images:
            cmd.extend(["--image", str(image)])

        # Installed Codex 0.147.0 exposes exact per-turn usage on the JSONL
        # turn.completed event. The parser consumes only that native payload.

        kwargs: dict[str, Any] = {
            "cwd": request.worktree_path,
            "timeout": request.timeout_seconds,
            **streaming_process_kwargs(request),
        }
        # Pin billing-relevant configuration for this invocation only.
        # No persistent Codex config mutation.
        cmd.extend(_DIRECT_COVERRIDE_FLAGS)
        # Hermetic child environment: only the allowlist plus the explicit
        # request overlay reach the child.  Ambient CODEX_API_KEY /
        # CODEX_ACCESS_TOKEN / OPENAI_* credentials can never switch
        # Codex to API billing.
        kwargs["env"] = hermetic_env("codex", request.env)
        kwargs["inherit_env"] = False
        if use_stdin:
            kwargs["input_text"] = request.task

        # The generic autonomous execution seam: direct launch when no
        # boundary is installed, kernel containment (or a typed
        # fail-closed refusal) when one is.
        res: ProcessResult = launch_worker_process(cmd, runner=self.runner, **kwargs)
        transport_used = "stdin" if use_stdin else "argv"

        display_stdout, usage = parse_codex_jsonl(res.stdout)
        if res.control_interrupted:
            from saberops.models import WorkerInterruptionReason
            return WorkerResult(provider=self.provider_name, model=request.model,
                status=AttemptStatus.FAILED, summary="Interrupted for manual control",
                stdout=display_stdout, stderr=res.stderr, has_changes=False,
                interruption_reason=WorkerInterruptionReason.ABORTED,
                duration_seconds=res.duration_seconds, usage=usage)
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
            exhaustion = classify_codex(res.stdout, res.stderr)
            if exhaustion.verdict is ExhaustionVerdict.QUOTA_EXHAUSTED:
                # Plan exhaustion: the orchestrator's existing
                # continuation-after-quota path applies (provider is
                # skipped for the rest of the cycle, work continues on
                # the next eligible candidate).  RATE_LIMITED keeps
                # today's retry behaviour and falls through below.
                from saberops.models import WorkerInterruptionReason
                reset_note = reset_to_iso(exhaustion.reset_at) or "unknown reset time"
                quota_error = (
                    "Codex plan quota exhausted "
                    f"(retry after {reset_note}) [{exhaustion.rule} {exhaustion.confidence}]"
                )
                return WorkerResult(
                    provider=self.provider_name,
                    model=request.model,
                    status=AttemptStatus.FAILED,
                    summary=quota_error,
                    stdout=display_stdout,
                    stderr=res.stderr,
                    changed_paths=changed_paths,
                    has_changes=has_changes,
                    error=quota_error,
                    duration_seconds=res.duration_seconds,
                    prompt_bytes=prompt_bytes,
                    prompt_transport_used=transport_used,
                    outcome=DispatchOutcome.PROVIDER_OR_RUNTIME_FAILURE,
                    usage=usage,
                    interruption_reason=WorkerInterruptionReason.QUOTA_EXHAUSTED,
                    quota_reset_at=reset_to_iso(exhaustion.reset_at),
                    quota_window=exhaustion.window,
                    quota_model_scope=exhaustion.model_scope,
                    quota_reset_confidence=exhaustion.reset_confidence,
                    provider_exit_code=res.exit_code,
                    execution_quiescent=not res.termination_incomplete,
                )
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
            provider_exit_code=res.exit_code,
            execution_quiescent=not res.termination_incomplete,
        )
