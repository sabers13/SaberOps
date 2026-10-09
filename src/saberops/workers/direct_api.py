"""Direct OpenAI-compatible API execution. Subscription auth is never used."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from saberops.git import GitManager
from saberops.model_access.connections import AccessProtocol, AccessStore, ProviderConnection
from saberops.models import (
    AttemptStatus,
    DispatchOutcome,
    ProviderUsage,
    WorkerRequest,
    WorkerResult,
)
from saberops.process import launch_worker_process
from saberops.project import owner_state_dir
from saberops.workers.base import WorkerAdapter, streaming_process_kwargs


def api_connections() -> tuple[ProviderConnection, ...]:
    path = os.environ.get("ORCH_ACCESS_STORE", "").strip()
    store = AccessStore(Path(path) if path else owner_state_dir() / AccessStore.FILENAME)
    try:
        return tuple(c for c in store.load().list_connections() if not c.is_account_backed)
    except (OSError, ValueError):
        return ()


def supports_connection(connection: ProviderConnection) -> bool:
    return (
        not connection.is_account_backed
        and connection.enabled
        and connection.is_configured
        and connection.protocol is AccessProtocol.OPENAI_COMPATIBLE
        and (not connection.credential_ref or connection.credential_ref.startswith("env:"))
    )


def connection_for(provider: str, profile_id: str) -> ProviderConnection | None:
    prefix = "discovered-profile:"
    if not profile_id.startswith(prefix):
        return None
    identity = profile_id.removeprefix(prefix)
    return next(
        (
            c
            for c in api_connections()
            if c.connection_id == identity and c.provider == provider and supports_connection(c)
        ),
        None,
    )


def credential_value(connection: ProviderConnection, env: dict[str, str] | None = None) -> str:
    if not connection.credential_ref:
        return ""
    # env: is the currently implemented secret-reference authority. Other
    # handle kinds fail closed; no subscription credential files are opened.
    if not connection.credential_ref.startswith("env:"):
        raise ValueError("API execution currently requires an env: credential reference")
    name = connection.credential_ref[4:]
    value = (env or {}).get(name, os.environ.get(name, ""))
    if not value or any(char in value for char in "\r\n\x00"):
        raise ValueError("API credential reference is unavailable")
    return value


class DirectAPIAdapter(WorkerAdapter):
    capability_transport = "DIRECT_API"
    factual_capabilities = frozenset(
        {
            "large_prompt_stdin",
            "read_only",
            "structured_usage",
            "process_identity",
            "cancellation",
            "tool_restriction",
        }
    )

    def __init__(self, provider: str) -> None:
        self._provider = provider

    @property
    def provider_name(self) -> str:
        return self._provider

    def is_available(self) -> bool:
        return True

    def prompt_transport(self) -> str:
        return "stdin"

    def validate_profile(self, binding: Any, profile: Any) -> None:
        from saberops.control_plane.bindings import BindingResolutionError
        from saberops.control_plane.capabilities import ProviderTransport

        connection = connection_for(binding.provider, profile.profile_id)
        if (
            binding.provider != self.provider_name
            or connection is None
            or binding.transport is not ProviderTransport.DIRECT_API
        ):
            raise BindingResolutionError("UNSUPPORTED_BACKEND")
        expected = (
            f"discovered-binding:{binding.binding_role.value.lower()}:"
            f"{connection.connection_id}:{binding.model}"
        )
        if binding.binding_id != expected:
            raise BindingResolutionError("UNMAPPABLE_PROFILE")
        if profile.account_ref or profile.backend_ref:
            raise BindingResolutionError("UNMAPPABLE_PROFILE")

    def run(self, request: WorkerRequest) -> WorkerResult:
        env = dict(request.env or {})
        connection = connection_for(self.provider_name, env.get("ORCH_EXECUTION_PROFILE_ID", ""))
        # Match the canonical identity marker, not an ambient provider default.
        if env.get("ORCH_EXECUTION_BACKEND") != "DIRECT_API":
            connection = None
        binding_id = env.get("ORCH_EXECUTION_BINDING_ID", "")
        roles = ("worker", "reviewer", "orchestrator")
        role = next(
            (
                r
                for r in roles
                if connection is not None
                and binding_id
                == f"discovered-binding:{r}:{connection.connection_id}:{request.model}"
            ),
            "",
        )
        if not role:
            connection = None
        try:
            if connection is None:
                raise ValueError("Exact API connection is unavailable or disabled")
            credential = credential_value(connection, env)
        except ValueError as exc:
            return WorkerResult(
                provider=self.provider_name,
                model=request.model,
                status=AttemptStatus.FAILED,
                summary=str(exc),
                stdout="",
                stderr="",
                error=str(exc),
                has_changes=False,
                outcome=DispatchOutcome.ELIGIBILITY_SKIP,
            )
        env["SABEROPS_API_CREDENTIAL"] = credential
        payload = {
            "connection": connection.to_dict(),
            "model": request.model,
            "task": request.task,
            "read_only": request.read_only or role in ("reviewer", "orchestrator"),
            "input_images": [str(image) for image in request.input_images],
        }
        kwargs = streaming_process_kwargs(request)
        # Keep the monitored Popen path even when the caller only requests
        # process identity; the blocking capture path cannot expose that PID.
        kwargs.setdefault("on_stdout", lambda line: None)
        kwargs.update(
            cwd=request.worktree_path,
            env=env,
            timeout=request.timeout_seconds,
            input_text=json.dumps(payload),
        )
        result = launch_worker_process(
            [sys.executable, str(Path(__file__).with_name("api_agent.py"))], **kwargs
        )
        output = ""
        usages = []
        actual_model = None
        completed = False
        for line in result.stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "result":
                output = str(event.get("text", ""))
                completed = event.get("success") is True
            if event.get("type") == "usage":
                usages.append(
                    ProviderUsage(
                        input_tokens=event.get("input_tokens"),
                        output_tokens=event.get("output_tokens"),
                        total_tokens=event.get("total_tokens"),
                        cached_tokens=event.get("cached_tokens"),
                        usage_source="direct_api_response",
                    )
                )
                actual_model = event.get("model") or actual_model
        changes = GitManager.get_changed_paths(request.worktree_path)
        status = (
            AttemptStatus.TIMEOUT
            if result.timed_out
            else AttemptStatus.SUCCESS
            if result.passed and completed and not result.termination_incomplete
            else AttemptStatus.FAILED
        )
        error = (
            None if status is AttemptStatus.SUCCESS else (output or "Direct API execution failed")
        )
        return WorkerResult(
            provider=self.provider_name,
            model=request.model,
            status=status,
            summary=error or output[:300],
            stdout=output,
            stderr="",
            error=error,
            changed_paths=changes,
            has_changes=bool(changes),
            usage=usages,
            actual_model=actual_model,
            duration_seconds=result.duration_seconds,
            prompt_bytes=len(request.task.encode()),
            prompt_transport_used="stdin",
            outcome=None
            if status is AttemptStatus.SUCCESS
            else DispatchOutcome.TIMEOUT
            if result.timed_out
            else DispatchOutcome.PROVIDER_OR_RUNTIME_FAILURE,
        )
