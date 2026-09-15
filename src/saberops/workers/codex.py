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

__all__ = ["CodexAdapter", "ORCH_PROMPT_ARG_LIMIT"]


# Gateway-aware env-var names published by :mod:`saberops.gateway`.
# They MUST match the public constant module used by the gateway dispatch
# seam so the Codex adapter can detect when a real gateway dispatch was
# prepared for it.
_GATEWAY_ENDPOINT_ENV = "ORCH_GATEWAY_ENDPOINT"
_GATEWAY_CONNECTION_ENV = "ORCH_GATEWAY_CONNECTION"
_GATEWAY_AUTHORIZATION_ENV = "ORCH_GATEWAY_AUTHORIZATION_ENV"
_GATEWAY_LOOPBACK_ENV = "ORCH_GATEWAY_LOOPBACK_URL"
_GATEWAY_PROVIDER_ENV = "ORCH_GATEWAY_PROVIDER_ID"

# Sentinel env-var name for the OmniRoute bearer token when the bridge
# is the transport owner. The bridge resolves the value at relay time;
# the Codex CLI never sees the bearer value directly when the bridge
# is in use. The Codex CLI itself resolves this env-var to obtain its
# own ``Authorization`` header on the loopback hop.
_BRIDGE_AUTH_SENTINEL_ENV = "_ORCH_BRIDGE_AUTH_SENTINEL"


class CodexAdapter(WorkerAdapter):
    """Worker adapter dispatching tasks via Codex CLI.

    The adapter is gateway-capable: when ``WorkerRequest.env`` carries
    the standard C10 gateway env vars (set by
    :func:`saberops.gateway.prepare_dispatch_decision`), the
    adapter emits the per-process ``-c model_providers.<id>={...}`` and
    ``-c model_provider="<id>"`` overrides that the Codex CLI
    ``rust-v0.147.0`` accepts to dynamically select a custom provider.

    When the loopback URL of the bounded C10 bridge is also present,
    the adapter uses the bridge as the ``base_url`` so the bridge can
    observe ``X-OmniRoute-Selected-Connection-Id`` from the upstream
    response on the worker's behalf.
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
    gateway_capabilities = frozenset()
    # C10-R3: Codex 0.147.0 has been proven to consume the
    # ORCH_GATEWAY_* env vars emitted by the C10 dispatch seam and
    # route its real HTTP request through the configured OmniRoute
    # transport via ``-c model_providers.<id>={...}`` and
    # ``-c model_provider="<id>"``.  Advertise this ONLY because the
    # mechanism has been implemented and tested.
    c10_gateway_transport_capabilities = frozenset(
        {
            "codex_custom_provider",
            "responses_wire",
            "env_http_headers",
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
            prompt_arg,
            "--model",
            request.model,
            "--cd",
            str(request.worktree_path),
            "--sandbox",
            sandbox_mode,
            "--ephemeral",
            "--json",
        ]
        if request.read_only:
            cmd.append("--skip-git-repo-check")

        # Installed Codex 0.147.0 exposes exact per-turn usage on the JSONL
        # turn.completed event. The parser consumes only that native payload.

        kwargs: dict[str, Any] = {
            "cwd": request.worktree_path,
            "timeout": request.timeout_seconds,
            **streaming_process_kwargs(request),
        }
        base_env = dict(request.env) if request.env is not None else {}
        gateway_overrides = self._build_gateway_overrides(base_env)
        if gateway_overrides is not None:
            cmd.extend(gateway_overrides)
        if base_env:
            kwargs["env"] = base_env
        if use_stdin:
            kwargs["input_text"] = request.task

        # The generic autonomous execution seam: direct launch when no
        # boundary is installed, kernel containment (or a typed
        # fail-closed refusal) when one is.
        res: ProcessResult = launch_worker_process(cmd, runner=self.runner, **kwargs)
        transport_used = "stdin" if use_stdin else "argv"

        display_stdout, usage = parse_codex_jsonl(res.stdout)
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

    def _build_gateway_overrides(self, env: dict[str, str]) -> list[str] | None:
        """Return the ``-c`` flags that select a C10 gateway provider.

        The Codex CLI accepts repeated ``-c key=value`` flags to layer
        configuration on top of ``~/.codex/config.toml`` *for that
        invocation only*.  This adapter uses that mechanism to
        dynamically route the Codex request to the configured gateway
        transport.

        The adapter returns ``None`` when no gateway preparation is
        present in ``env``, so callers may keep the legacy direct
        command unchanged.

        The selection is process-local: no persistent Codex config
        mutation, no provider/model/account authority.  The provider id
        itself is a deterministic, non-secret identifier derived from
        the gateway pinning variables already in the worker request.

        Secret hygiene: the bearer-token env-var *name* is passed to
        Codex; Codex resolves the actual token value at request time.
        When the bounded C10 bridge is the transport owner, the
        adapter hands Codex a sentinel env-var name so the Codex CLI
        never sees the real OmniRoute token.
        """

        if (
            _GATEWAY_ENDPOINT_ENV not in env
            and _GATEWAY_LOOPBACK_ENV not in env
        ):
            return None

        loopback_url = env.get(_GATEWAY_LOOPBACK_ENV)
        endpoint = env.get(_GATEWAY_ENDPOINT_ENV)
        if not endpoint:
            return None
        base_url = loopback_url or endpoint
        provider_id = env.get(_GATEWAY_PROVIDER_ENV) or "orch_omniroute"
        authorization_env_name = env.get(
            _GATEWAY_AUTHORIZATION_ENV, "OMNIROUTE_API_KEY"
        )
        effective_auth_env = (
            _BRIDGE_AUTH_SENTINEL_ENV
            if loopback_url
            else authorization_env_name
        )
        # Codex expects the env-var *name* for ``env_http_headers``
        # mapping -- the CLI resolves the value per request and we
        # never persist the connection id value in command-line args
        # or evidence.
        env_http_headers: dict[str, str] = {}
        if _GATEWAY_CONNECTION_ENV in env:
            env_http_headers["x-omniroute-connection"] = _GATEWAY_CONNECTION_ENV

        provider_table_parts = [
            f'name = "orchestrator-c10-{provider_id}"',
            f'base_url = "{base_url}"',
            f'env_key = "{effective_auth_env}"',
            'wire_api = "responses"',
        ]
        if env_http_headers:
            pairs = ", ".join(
                f'"{name}" = "{value}"'
                for name, value in sorted(env_http_headers.items())
            )
            provider_table_parts.append(f"env_http_headers = {{{pairs}}}")
        provider_table_value = "{" + ", ".join(provider_table_parts) + "}"
        select_value = f'model_provider = "{provider_id}"'
        provider_key = f"model_providers.{provider_id}"
        return [
            "-c",
            f"{select_value}",
            "-c",
            f"{provider_key} = {provider_table_value}",
        ]
