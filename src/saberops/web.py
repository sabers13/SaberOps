"""Local web onboarding pages for SaberOps.

This module wires the small FastAPI application served by ``orch ui``. It keeps
only the first-run setup, accounts and questions pages; run monitoring, plan
and wave scoring, the routing editor and the storage cleanup page are gone;
the first-run pages remain.
"""
# ruff: noqa: E501

from __future__ import annotations

import hmac
import json
import os
import platform
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import (
    FastAPI,
    Form,
    Request,
    Response,
)
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from saberops import access_ops, plans_ops
from saberops.away import (
    NEVER_DELEGABLE_KINDS,
    NEVER_DELEGABLE_POLICY,
    DecisionScope,
    StandingDecision,
)
from saberops.control_plane import (
    BindingRole,
    BindingStoreError,
    OwnerBindings,
    OwnerBindingStore,
    capability_profile_for,
    get_binding_store_path,
    materialize_discovered_binding,
    save_owner_bindings,
    upsert_owner_binding,
)
from saberops.control_plane.bindings import (
    BindingResolutionError,
    resolve_execution_binding,
)
from saberops.db import Database
from saberops.model_access import (
    ACCOUNT_BACKENDS,
    AccessStore,
    ConnectionTestReason,
    ExecutionSupport,
    ModelCatalogStore,
    ModelDiscoveryResult,
    ModelListingError,
    ModelSource,
    ProviderConnection,
    ReadinessService,
    ReadinessStore,
    discover_connection_models,
)
from saberops.observability.run_report import (
    saberops_version,
)
from saberops.owner_mode import (
    OwnerModeError,
    clear_owner_mode,
    list_owner_modes,
    parse_owner_mode,
    set_owner_mode,
)
from saberops.owner_mode import (
    mode_view as owner_mode_view,
)
from saberops.project import ProjectRegistry, owner_state_dir
from saberops.questions import (
    AnswerAuthority,
    AskRole,
    OwnerQuestionQueue,
    QuestionError,
    QuestionStatus,
    is_due,
    question_view,
)
from saberops.runtime import ProjectRuntimeManager
from saberops.service import OrchestratorService
from saberops.storage_cleanup import (
    current_disk_floor_bytes,
    disk_ok,
)
from saberops.workers.registry import AdapterRegistry

_PACKAGE_DIR = Path(__file__).resolve().parent
_TEMPLATES_DIR = _PACKAGE_DIR / "templates"
_STATIC_DIR = _PACKAGE_DIR / "static"

_SOURCE_SHA_UNKNOWN = "UNKNOWN"
_SOURCE_TREE_CLEAN = "CLEAN"
_SOURCE_TREE_DIRTY = "DIRTY"
_SOURCE_TREE_NOT_GIT = "NOT_GIT"
_SOURCE_TREE_UNKNOWN = "UNKNOWN"


def _resolve_source_identity(package_dir: Path) -> tuple[str, str]:
    """Resolve fail-closed source identity for the executing package.

    Rules:
      - Installed package outside a Git checkout -> ("UNKNOWN", "NOT_GIT").
      - Inside a checkout and the executing ``src/saberops`` tree is clean
        relative to HEAD -> (exact HEAD SHA, "CLEAN").
      - Inside a checkout but the executing package tree differs from HEAD
        (modified, staged, deleted, or relevant untracked files) ->
        ("UNKNOWN", "DIRTY").

    Only the executing package tree (``src/saberops``) is considered, so a
    dirty unrelated README or planning document does not invalidate the SHA.
    Any git failure degrades to ("UNKNOWN", "UNKNOWN").
    """
    try:
        source_root = package_dir.parents[1]
    except IndexError:
        return (_SOURCE_SHA_UNKNOWN, _SOURCE_TREE_NOT_GIT)
    if package_dir != source_root / "src" / "saberops":
        return (_SOURCE_SHA_UNKNOWN, _SOURCE_TREE_NOT_GIT)
    if not (source_root / ".git").exists():
        return (_SOURCE_SHA_UNKNOWN, _SOURCE_TREE_NOT_GIT)
    try:
        from saberops.process import run_process

        head = run_process(["git", "rev-parse", "HEAD"], cwd=source_root, timeout=5)
        if not head.passed:
            return (_SOURCE_SHA_UNKNOWN, _SOURCE_TREE_UNKNOWN)
        sha = head.stdout.strip()
        if re.fullmatch(r"[0-9a-f]{40}", sha) is None:
            return (_SOURCE_SHA_UNKNOWN, _SOURCE_TREE_UNKNOWN)
        status = run_process(
            ["git", "status", "--porcelain=v1", "--", "src/saberops"],
            cwd=source_root,
            timeout=5,
        )
        if not status.passed:
            return (_SOURCE_SHA_UNKNOWN, _SOURCE_TREE_UNKNOWN)
        if status.stdout.strip():
            return (_SOURCE_SHA_UNKNOWN, _SOURCE_TREE_DIRTY)
        return (sha, _SOURCE_TREE_CLEAN)
    except Exception:
        return (_SOURCE_SHA_UNKNOWN, _SOURCE_TREE_UNKNOWN)

_BIN_ATTR: dict[str, str] = {
    "opencode": "opencode_bin",
    "codex": "codex_bin",
    "antigravity": "agy_bin",
}

#: Owner-action event types that must reach the run-page EventSource even
#: after the run itself is terminal (manual Review / Accept happens on an
#: already COMPLETED run).  Kept as a literal tuple so the client bundle
#: and the stream liveness check share one visible contract.
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))



def format_timestamp(value: str | None) -> str:
    """Render a stored timestamp in canonical format: YYYY-MM-DD HH:MM:SS UTC."""
    if value is None:
        return "N/A"
    raw = value.strip()
    if raw == "":
        return "N/A"
    normalized = raw
    if raw.endswith("Z") or raw.endswith("z"):
        normalized = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return raw
    if parsed.tzinfo is not None and parsed.utcoffset() is not None:
        parsed = parsed.astimezone(UTC)
    return parsed.strftime("%Y-%m-%d %H:%M:%S UTC")








def resolve_owner_argv_prefix() -> list[str]:
    """Resolve the argv prefix for a detached/supervised execution owner.

    Backward-compatible re-export of :func:`saberops.cli_environment.resolve_owner_argv_prefix`
    (the single authority lives in cli_environment so engine modules need
    not import the web console).
    """
    from saberops.cli_environment import resolve_owner_argv_prefix as _resolve

    return _resolve()


def resolve_orch_executable() -> str:
    """Resolve the orchestrator executable identity for display/diagnostics.

    Backward-compatible re-export of :func:`saberops.cli_environment.resolve_orch_executable`.
    """
    from saberops.cli_environment import resolve_orch_executable as _resolve

    return _resolve()




def _access_store() -> AccessStore:
    """Return the user-connection store (overridable for tests).

    Production path is ``<owner_state_dir>/access-connections.json``;
    ``ORCH_ACCESS_STORE`` pins an explicit file (tests, diagnostics).
    Only credential *references* are ever persisted -- never values.
    """
    override = os.environ.get("ORCH_ACCESS_STORE", "").strip()
    if override:
        return AccessStore(Path(override).expanduser())
    return AccessStore(owner_state_dir() / AccessStore.FILENAME)


def _model_catalog_store() -> ModelCatalogStore:
    """Return the discovered-model catalog store (overridable for tests).

    Production path is ``<owner_state_dir>/access-models.json``;
    ``ORCH_ACCESS_MODELS_STORE`` pins an explicit file.  The catalog is
    non-secret evidence and is written only by discovery refresh, never
    by connection save/probe, so a discovery failure can never erase
    durable connection configuration.
    """
    override = os.environ.get("ORCH_ACCESS_MODELS_STORE", "").strip()
    if override:
        return ModelCatalogStore(Path(override).expanduser())
    return ModelCatalogStore(owner_state_dir() / ModelCatalogStore.FILENAME)


def _binding_store() -> OwnerBindingStore:
    """Return the owner binding store (overridable for tests).

    Production path is ``$XDG_CONFIG_HOME/saberops/bindings.json``;
    ``ORCH_BINDING_STORE`` pins an explicit file.  The document is binding
    owner identity only -- never a credential value, never a second routing
    configuration.
    """
    return OwnerBindingStore(get_binding_store_path())


def _load_owner_bindings_safely() -> OwnerBindings:
    """Load owner bindings, degrading to an empty document on corrupt state."""
    try:
        return _binding_store().load()
    except BindingStoreError:
        return OwnerBindings()


#: Bounded wall-clock budget for one backend model-list subprocess.
_BACKEND_LIST_TIMEOUT_SECONDS = 15.0


def _run_backend_listing(
    argv: list[str], *, env: Mapping[str, str] | None = None
) -> str:
    """Run one backend's supported model-list command (bounded, no secrets).

    The backend reads its own account session; Orchestrator passes no
    credential and captures only stdout.  A non-zero exit is classified and
    its stderr is never surfaced (it may carry session material).

    ``env`` replaces the inherited environment when given; callers that pass
    ``None`` keep the previous direct-inheritance behaviour.
    """

    try:
        if env is not None:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=_BACKEND_LIST_TIMEOUT_SECONDS,
                check=False,
                env=dict(env),
            )
        else:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=_BACKEND_LIST_TIMEOUT_SECONDS,
                check=False,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ModelListingError(
            ConnectionTestReason.ENDPOINT_UNREACHABLE,
            f"backend model listing failed: {exc.__class__.__name__}",
        ) from exc
    if completed.returncode != 0:
        raise ModelListingError(
            ConnectionTestReason.ENDPOINT_UNREACHABLE,
            f"backend model listing exited {completed.returncode}",
        )
    return completed.stdout or ""


def _normalized_backend_model_id(raw: str) -> str:
    """Return one backend-enumerated model id verbatim (whitespace-trimmed).

    A backend's enumerated model identity is provider-native and may carry
    an upstream namespace that is part of the identity the adapter must
    invoke.  ``opencode models`` lists the OpenCode-native upstream as
    ``opencode/<model>`` and its sibling upstreams as ``<upstream>/<model>``;
    the SaberOps connection ``provider`` (for example ``opencode``) names the
    CLI that carries the request, not the upstream namespace.  Stripping a
    leading ``<provider>/`` would therefore collapse ``opencode/<model>``
    into a bare name and silently merge it with an unrelated same-named
    upstream (for example ``google/<model>``).  The raw listing is the
    authoritative executable identity, so no prefix is ever removed here.
    """
    return raw.strip()


def _model_ids_from_sequence(items: list[Any]) -> list[str]:
    ids: list[str] = []
    for item in items:
        if isinstance(item, str):
            ids.append(_normalized_backend_model_id(item))
        elif isinstance(item, dict):
            for key in ("id", "model", "slug", "name"):
                candidate = item.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    ids.append(_normalized_backend_model_id(candidate.strip()))
                    break
    return [model_id for model_id in ids if model_id]


def _parse_backend_model_list(stdout: str) -> list[str]:
    """Parse a backend's supported model-list output conservatively.

    Recognises a JSON catalog (``codex debug models``) and the
    ``provider/model`` line format (``opencode models``).  Model ids are
    preserved verbatim so provider-native upstream namespaces survive
    discovery (see :func:`_normalized_backend_model_id`).  Anything that
    cannot be parsed with confidence yields no ids rather than invented ones.
    """
    text = stdout.strip()
    if not text:
        return []
    try:
        payload: Any = json.loads(text)
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        for key in ("models", "data", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return _model_ids_from_sequence(value)
        return []
    if isinstance(payload, list):
        return _model_ids_from_sequence(payload)
    ids: list[str] = []
    for line in text.splitlines():
        cleaned = line.strip()
        if not cleaned or " " in cleaned:
            continue
        ids.append(_normalized_backend_model_id(cleaned))
    return [model_id for model_id in ids if model_id]


def _list_account_models(connection: ProviderConnection) -> list[str]:
    """Enumerate models through a backend's own supported listing command."""
    descriptor = next(
        (
            entry
            for entry in ACCOUNT_BACKENDS
            if entry.connection_id == connection.connection_id
        ),
        None,
    )
    if descriptor is None or not descriptor.has_model_listing:
        raise ModelListingError(
            ConnectionTestReason.CAPABILITY_UNKNOWN,
            "backend exposes no supported model enumeration",
        )
    executable = shutil.which(descriptor.executable)
    if executable is None:
        raise ModelListingError(
            ConnectionTestReason.BACKEND_NOT_INSTALLED,
            f"backend executable '{descriptor.executable}' not found",
        )
    if descriptor.model_list_protocol:
        from saberops.model_access.native_listing import list_native_models

        return list_native_models(executable, descriptor.model_list_protocol)
    if descriptor.backend == "opencode":
        # Config/plugin isolation for the OpenCode listing launch: a private
        # config/home root with project/ancestor discovery disabled (inline
        # deny policies block every plugin, MCP server and skill), reusing
        # the account/session data directory by path only.  Refuse in the
        # existing typed form when isolation cannot be established; other
        # backends keep their previous behaviour exactly.
        from saberops.workers.env import hermetic_env
        from saberops.workers.opencode_isolation import (
            OpenCodeIsolationUnavailable,
            opencode_isolation,
        )

        base_env = hermetic_env("opencode", {}, None)
        try:
            isolation = opencode_isolation(base_env)
        except OpenCodeIsolationUnavailable as exc:
            raise ModelListingError(
                ConnectionTestReason.ENDPOINT_UNREACHABLE, str(exc)
            ) from exc
        # The listing is read-only metadata, so it lists every provider the
        # account can reach (OpenCode Go, Zen, Anthropic via OpenCode, ...);
        # billing stays pinned per run to the model's own provider by the
        # worker. OpenCode v2 serves the CLI from one background service on a
        # fixed port; the isolated config cannot share the owner's running
        # service, so the listing runs its own private server (--standalone).
        with isolation:
            listing_env = isolation.env_for(base_env, config=isolation.config({}))
            argv = [executable, *descriptor.model_list_argv]
            try:
                stdout = _run_backend_listing([*argv, "--standalone"], env=listing_env)
            except ModelListingError:
                # OpenCode before v2 has no --standalone flag.
                stdout = _run_backend_listing(argv, env=listing_env)
    else:
        stdout = _run_backend_listing([executable, *descriptor.model_list_argv])
    if descriptor.backend == "antigravity":
        # Native `agy models` emits exact ID<TAB>display name pairs.
        # Never interpret headers, errors or display names as executable IDs.
        return [
            line.split("\t", 1)[0].strip()
            for line in stdout.splitlines()
            if "\t" in line and line.split("\t", 1)[0].strip()
        ]
    return _parse_backend_model_list(stdout)


def _materialize_discovered_binding(
    connection: ProviderConnection,
    model_id: str,
    registry: AdapterRegistry,
    *,
    owner: OwnerBindings | None = None,
    persist: bool = True,
) -> tuple[OwnerBindings, Any] | None:
    """Translate validated discovery evidence into exact bindings.

    Discovery creates the *role-qualified* binding identities that share
    the same account / profile and connection-scoped quota-pool
    resource:

    * BUILD-role binding: eligible for the worker-routing ladder.
    * CHECK-role binding: eligible for the owner-selected reviewer slot.

    Both share the same ``profile_id`` (the real connection / account)
    and ``quota_pool_id`` (the real connection-scoped scarcity resource);
    only their ``binding_id`` carries the semantic role so /routing/add
    and /reviewer/select each see their own exact binding and quota
    policy remains authoritative over the real quota pool.  Two distinct
    account connections to the same provider register distinct
    ``profile_id`` and distinct ``quota_pool_id`` -- provider identity is
    not quota-resource identity, and semantic role never duplicates
    either resource.

    The exact bindings are persisted only when both prove through the
    real resolver with the real adapter, so a provider/model name can
    never become executable without an exact binding that actually
    resolves.  Returns ``(owner, worker_binding)`` or ``None``.
    """
    if not connection.is_account_backed:
        # Direct API execution has no adapter.
        return None
    adapter = registry.get(connection.provider)
    if adapter is None:
        return None
    from saberops.control_plane.bindings import BindingKind
    profile = capability_profile_for(adapter)
    worker_binding, execution_profile, pool = materialize_discovered_binding(
        connection_id=connection.connection_id,
        provider=connection.provider,
        model=model_id,
        provider_transport=profile.transport,
        harness_capabilities=profile.capabilities,
        binding_role=BindingRole.BUILD,
        backend=BindingKind.DIRECT_CLI,
    )
    reviewer_binding, _reviewer_profile, _reviewer_pool = materialize_discovered_binding(
        connection_id=connection.connection_id,
        provider=connection.provider,
        model=model_id,
        provider_transport=profile.transport,
        harness_capabilities=profile.capabilities,
        binding_role=BindingRole.CHECK,
        backend=BindingKind.DIRECT_CLI,
    )
    owner = owner if owner is not None else _load_owner_bindings_safely()
    candidate = upsert_owner_binding(
        owner, binding=worker_binding, profile=execution_profile, pool=pool
    )
    candidate = upsert_owner_binding(
        candidate, binding=reviewer_binding, profile=execution_profile, pool=pool
    )
    try:
        resolve_execution_binding(
            registry=candidate.registry,
            binding_id=worker_binding.binding_id,
            adapter=adapter,
        )
        resolve_execution_binding(
            registry=candidate.registry,
            binding_id=reviewer_binding.binding_id,
            adapter=adapter,
        )
    except BindingResolutionError:
        return None
    if persist and not _save_owner_bindings(candidate):
        return None
    return candidate, worker_binding


def _save_owner_bindings(owner: OwnerBindings) -> bool:
    """Persist owner bindings; return ``True`` only when durable."""
    try:
        save_owner_bindings(owner)
    except BindingStoreError:
        return False
    return True


def _apply_exact_execution_bindings(
    connection: ProviderConnection,
    result: ModelDiscoveryResult,
    registry: AdapterRegistry,
) -> ModelDiscoveryResult:
    """Replace inferred execution support with exact-binding evidence.

    A successful enumeration on an account-backed connection with a real
    adapter materializes exact role bindings per discovered model and proves them
    through the canonical resolver. Unsupported protocols and models with no
    resolvable exact binding stay ``UNSUPPORTED`` / ``UNKNOWN``. A failed or
    ``UNKNOWN`` refresh never creates a binding.
    """
    if not result.ok:
        return result
    owner = _load_owner_bindings_safely()
    support: dict[tuple[str, str, str, str], ExecutionSupport] = {}
    for model in result.models:
        if model.source is not ModelSource.DISCOVERED or model.is_stale:
            support[model.identity] = model.execution_support
            continue
        if not connection.is_account_backed:
            # Direct API connections are not supported: no execution exists.
            support[model.identity] = ExecutionSupport.UNSUPPORTED
            continue
        if registry.get(connection.provider) is None:
            support[model.identity] = ExecutionSupport.UNSUPPORTED
            continue
        materialized = _materialize_discovered_binding(
            connection, model.model_id, registry, owner=owner, persist=False
        )
        if materialized is None:
            support[model.identity] = ExecutionSupport.UNKNOWN
            continue
        owner, _binding = materialized
        support[model.identity] = ExecutionSupport.SUPPORTED
    if ExecutionSupport.SUPPORTED in support.values() and not _save_owner_bindings(owner):
        support = {
            identity: ExecutionSupport.UNKNOWN if value is ExecutionSupport.SUPPORTED else value
            for identity, value in support.items()
        }
    return replace(
        result,
        models=tuple(
            replace(model, execution_support=support.get(model.identity, model.execution_support))
            for model in result.models
        ),
    )


def _discover_connection(
    connection: ProviderConnection,
    registry: AdapterRegistry,
) -> ModelDiscoveryResult:
    """Run bounded discovery for one connection.

    Account-backed backends use their own supported listing boundary where
    one exists (listing commands or Claude/Copilot native control protocols);
    backends with no supported enumeration stay truthfully ``UNKNOWN`` rather
    than inventing a catalog. A stored direct-API connection enumerates
    nothing: it has no executable transport.

    Execution support is proven *after* enumeration by materializing an exact
    Binding and resolving it with the real adapter -- never inferred
    from the mere presence of a registered adapter.
    """
    discovered_at = datetime.now(UTC).isoformat()
    if not connection.is_account_backed:
        return discover_connection_models(connection, discovered_at=discovered_at)
    descriptor = next(
        (
            entry
            for entry in ACCOUNT_BACKENDS
            if entry.connection_id == connection.connection_id
        ),
        None,
    )
    if descriptor is not None and descriptor.has_model_listing:
        result = discover_connection_models(
            connection,
            list_models=lambda: _list_account_models(connection),
            discovered_at=discovered_at,
        )
    else:
        result = discover_connection_models(
            connection,
            discovered_at=discovered_at,
        )
    return _apply_exact_execution_bindings(connection, result, registry)


def _readiness_store() -> ReadinessStore:
    """Return the readiness-evidence store (overridable for tests).

    Production path is ``<owner_state_dir>/access-readiness.json``;
    ``ORCH_READINESS_STORE`` pins an explicit file.  The store carries
    only non-secret observations; the Verify action writes through this
    same seam while every other read path shows existing evidence only.
    """
    override = os.environ.get("ORCH_READINESS_STORE", "").strip()
    if override:
        return ReadinessStore(Path(override).expanduser())
    return ReadinessStore(owner_state_dir() / ReadinessStore.FILENAME)


def initialize_account_readiness() -> None:
    """Acquire bounded native CLI evidence at UI launch, in the selected profile."""
    ReadinessService(
        registry=_access_store().load(), store=_readiness_store()
    ).refresh_accounts_on_startup()



_LOOPBACK_HOST_NAMES = frozenset({"127.0.0.1", "localhost", "::1"})
_SYNTHETIC_TEST_HOST = "testserver"


def _parse_host_header(raw: str) -> tuple[str, int | None] | None:
    """Split a Host header value into ``(hostname, port)``.

    Returns ``None`` when the value is malformed.  Hostname comparison is
    the caller's job; this helper only enforces Host grammar (RFC 9110
    §7.2 shape: optional ``[v6]`` brackets, at most one numeric port).
    """
    value = raw.strip()
    if not value or "@" in value or " " in value or "\t" in value:
        return None
    if value.startswith("["):
        end = value.find("]")
        if end <= 1:
            return None
        hostname = value[1:end].lower()
        rest = value[end + 1 :]
        if not rest:
            return hostname, None
        if not rest.startswith(":") or len(rest) < 2:
            return None
        port_text = rest[1:]
        if not port_text.isdigit():
            return None
        port = int(port_text)
        if not 1 <= port <= 65535:
            return None
        return hostname, port
    if value.count(":") > 1:
        # Unbracketed IPv6 (e.g. ``::1``) is never a valid Host value.
        return None
    if ":" in value:
        hostname, _, port_text = value.partition(":")
        if not hostname or not port_text or not port_text.isdigit():
            return None
        port = int(port_text)
        if not 1 <= port <= 65535:
            return None
        return hostname.lower(), port
    return value.lower(), None


def _host_header_allowed(request: Request) -> bool:
    """Check the request Host header against the local-only allowlist.

    Trusted Host values name this loopback server directly
    (``127.0.0.1`` / ``localhost`` / ``[::1]``, optional port).  A
    ``testserver`` Host is accepted only inside a synthetic ASGI test
    scope whose server name is itself ``testserver`` (Starlette
    TestClient default); the same Host value arriving at a real
    loopback server is foreign and refused, so there is no production
    bypass.  ``X-Forwarded-Host`` and similar proxy headers are never
    consulted.  When the Host carries an explicit port and the ASGI
    server port is known, the ports must match.
    """
    raws = request.headers.getlist("host")
    if len(raws) != 1:
        return False
    parsed = _parse_host_header(raws[0])
    if parsed is None:
        return False
    hostname, port = parsed
    scope_server = request.scope.get("server")
    server_host: str | None = None
    server_port: int | None = None
    if isinstance(scope_server, (list, tuple)) and len(scope_server) == 2:
        if isinstance(scope_server[0], str):
            server_host = scope_server[0].lower()
        if isinstance(scope_server[1], int):
            server_port = scope_server[1]
    if hostname == _SYNTHETIC_TEST_HOST:
        if server_host != _SYNTHETIC_TEST_HOST:
            return False
    elif hostname not in _LOOPBACK_HOST_NAMES:
        return False
    if port is not None and server_port is not None and port != server_port:
        return False
    return True


def _is_private_web_path(path: str) -> bool:
    """Paths whose responses may carry owner data or mutate state.

    ``/health`` reports non-secret install identity for diagnostics and
    ``/static/*`` serves bundled assets; everything else (setup, accounts
    and questions pages, their POSTs, and API docs) requires a trusted Host
    header.
    """
    if path == "/health" or path == "/static" or path.startswith("/static/"):
        return False
    return True


# ---------------------------------------------------------------------------
# The kept first-run onboarding surface.
#
# Read projections and owner-confirmed mutations for first-run Setup, Accounts
# and Questions: readiness diagnostics, honest vendor logins, connection/model
# discovery, fail-closed posture, the question queue, unattended standing
# decisions, the owner mode and the work gate.  Every projection degrades to
# an honest empty state and never raises into the request path; every mutation
# is gated on a process-local CSRF token plus explicit owner confirmation.
# ---------------------------------------------------------------------------

_CSRF_TOKEN: str = secrets.token_urlsafe(32)

_STATE_OK: frozenset[str] = frozenset({"READY", "OK", "EXPIRING", "TRUE"})
_STATE_FAIL: frozenset[str] = frozenset(
    {"NOT_READY", "UNVERIFIED", "EXPIRED", "FORBIDDEN", "FALSE"}
)


def _csrf_token() -> str:
    """Return the process-local CSRF token for owner-confirmed mutations."""
    return _CSRF_TOKEN


def _csrf_valid(submitted: str) -> bool:
    """Constant-time check of a submitted CSRF token."""
    return bool(submitted) and hmac.compare_digest(submitted, _CSRF_TOKEN)


def _pill_class(state: str) -> str:
    """Map a backend state token to a presentation class (ok/warn/fail)."""
    token = state.strip().upper()
    if token in _STATE_OK:
        return "ok"
    if token in _STATE_FAIL:
        return "fail"
    return "warn"



def _question_queue() -> OwnerQuestionQueue:
    """Return the durable owner-question queue (``ORCH_QUESTIONS_STORE`` override)."""
    return OwnerQuestionQueue()



def _unattended_store_path() -> Path:
    """Resolve the standing-decisions document path (``ORCH_UNATTENDED_STORE``)."""
    override = os.environ.get("ORCH_UNATTENDED_STORE", "").strip()
    if override:
        return Path(override).expanduser()
    return owner_state_dir() / "unattended-decisions.json"


def _onboarding_notice(request: Request) -> str | None:
    """Translate an onboarding success-redirect flag into a human notice."""
    params = request.query_params
    if params.get("mode_saved") == "1":
        return "Saved. The owner mode applies to the next decision."
    if params.get("paused") == "1":
        return "Work paused. Nothing starts or resumes until you confirm a resume."
    if params.get("resumed") == "1":
        return "Resumed. Parked work returns to the scheduler on its next tick."
    if params.get("discovered") == "1":
        connection = params.get("connection") or "the connection"
        return f"Model list refreshed for {connection}."
    if params.get("posture_saved") == "1":
        return "Saved. Billing confirmed until you change it."
    if params.get("confirmed_all") is not None:
        count = params.get("confirmed_all") or "0"
        skipped = params.get("skipped") or "0"
        message = f"Saved. Billing confirmed for {count} account(s) until you change it."
        if skipped != "0":
            message += f" {skipped} still need a terms or pricing link: use Change on each."
        return message
    agent = params.get("agent")
    if agent:
        from saberops.agent_connect import agent_by_slug

        found = agent_by_slug(agent)
        name = found.name if found else agent
        if params.get("agent_action") == "disconnect":
            return f"Saved. SaberOps was removed from {name}."
        return (
            f"Saved. {name} is connected. Start a new {name} session and say "
            "\"turn on saberops\" when you want it."
        )
    if params.get("mcp") in ("on", "off"):
        return (
            "Saved. Agents can use SaberOps again when you turn it on in a session."
            if params.get("mcp") == "on"
            else "Saved. SaberOps is off for every agent; they work as normal agents."
        )
    checked = params.get("checked")
    if checked:
        outcome = params.get("outcome") or ""
        return {
            "ran": "Usage checked just now.",
            "no-executable": "Usage not checked: this account's command-line tool is not installed.",
            "failed": "Usage could not be read. The reason and next step are shown on this account.",
        }.get(outcome, "Usage was not checked this time.")
    if params.get("answered") == "1":
        return "Owner answer recorded verbatim."
    if params.get("deferred") == "1":
        return "Question deferred; it resurfaces on the chosen date."
    return None


def _diagnostics_rows() -> list[dict[str, Any]]:
    """Local-only readiness diagnostics; nothing here contacts the network."""
    rows: list[dict[str, Any]] = []
    py_ok = sys.version_info >= (3, 12)
    rows.append(
        {
            "label": "Python runtime",
            "state": "ok" if py_ok else "fail",
            "detail": f"{platform.python_version()} (requires 3.12+)",
            "fix": ""
            if py_ok
            else "Install Python 3.12 or newer, then recreate the virtual environment.",
        }
    )
    git = shutil.which("git")
    rows.append(
        {
            "label": "Git executable",
            "state": "ok" if git else "fail",
            "detail": git or "not found on PATH",
            "fix": "" if git else "Install git, then reopen the terminal.",
        }
    )
    rows.append(
        {
            "label": "SQLite library",
            "state": "ok",
            "detail": f"{sqlite3.sqlite_version} (bundled with Python)",
            "fix": "",
        }
    )
    try:
        state_dir = owner_state_dir()
        anchor = state_dir
        while not anchor.exists() and anchor != anchor.parent:
            anchor = anchor.parent
        usage = shutil.disk_usage(anchor)
        floor = current_disk_floor_bytes(usage.total)
        ok = disk_ok([state_dir])
        rows.append(
            {
                "label": "Disk free floor",
                "state": "ok" if ok else "fail",
                "detail": (
                    f"{usage.free / float(1024 ** 3):.1f} GB free on {anchor}; "
                    f"floor {floor / float(1024 ** 3):.1f} GB (10 GB or 10%)"
                ),
                "fix": "" if ok else "Free disk space before starting work.",
            }
        )
    except Exception as exc:
        rows.append(
            {
                "label": "Disk free floor",
                "state": "warn",
                "detail": f"unavailable: {exc.__class__.__name__}",
                "fix": "Check that the state directory is readable.",
            }
        )
    return rows


def _doctor_view(db: Database, repo: Path) -> dict[str, Any]:
    """The same findings the CLI and the ``doctor`` MCP tool report."""
    from saberops.doctor import run_doctor

    try:
        report = run_doctor(repo, db.db_path, fix=False)
    except Exception as exc:
        return {
            "error": f"{exc.__class__.__name__}: {exc}",
            "counts": {"ok": 0, "skip": 0, "fixable": 0, "human": 0},
            "findings": [],
            "actions": [],
        }
    return {
        "error": None,
        "counts": report.counts(),
        "findings": [finding.as_dict() for finding in report.findings],
        "actions": [action.as_dict() for action in report.actions],
    }


def _vendor_login_rows() -> list[dict[str, Any]]:
    """Official vendor CLI logins: installed/readiness evidence, never secrets."""
    readiness: Any
    try:
        readiness = access_ops.readiness_store().load()
    except Exception:
        readiness = None
    rows: list[dict[str, Any]] = []
    for descriptor in ACCOUNT_BACKENDS:
        evidence = (
            readiness.try_get(descriptor.connection_id) if readiness is not None else None
        )
        state = evidence.state.value if evidence is not None else "UNKNOWN"
        rows.append(
            {
                "connection_id": descriptor.connection_id,
                "provider": descriptor.provider,
                "backend": descriptor.backend,
                "executable": descriptor.executable,
                "installed": shutil.which(descriptor.executable) is not None,
                "login_hint": f"{descriptor.executable} login",
                "readiness_state": state,
                "readiness_reason": evidence.reason if evidence is not None else "NO_EVIDENCE",
                "observed_at": evidence.observed_at if evidence is not None else None,
                "state_class": _pill_class(state),
            }
        )
    return rows


def _connection_rows() -> list[dict[str, Any]]:
    """Owner model-access connections with readiness and catalog counts."""
    try:
        payload = access_ops.list_access_connections()
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    for row in payload.get("connections", []):
        if not isinstance(row, dict):
            continue
        readiness = str(row.get("readiness_state") or "UNKNOWN")
        rows.append(
            {
                "connection_id": row.get("connection_id"),
                "provider": row.get("provider"),
                "backend": row.get("backend"),
                "protocol": row.get("protocol"),
                "auth_mode": row.get("auth_mode"),
                "credential_display": row.get("credential_display"),
                "enabled": row.get("enabled"),
                "account_backed": row.get("account_backed"),
                "readiness_state": readiness,
                "readiness_reason": row.get("readiness_reason"),
                "observed_at": row.get("observed_at"),
                "catalog_models": row.get("catalog_models"),
                "state_class": _pill_class(readiness),
            }
        )
    return rows


def _model_rows() -> list[dict[str, Any]]:
    """Discovered models with source and exact execution-support evidence."""
    try:
        catalog = access_ops.load_catalog_safely()
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    for model in catalog.list_models():
        support = model.execution_support.value
        rows.append(
            {
                "model_id": model.model_id,
                "display_name": model.display_name,
                "provider": model.provider,
                "backend": model.backend,
                "connection_id": model.connection_id,
                "source": model.source.value,
                "execution_support": support,
                "stale": model.stale,
                "discovered_at": model.discovered_at,
                "auto_routing": model.is_execution_supported and not model.stale,
                "state_class": _pill_class(support),
            }
        )
    return rows


def _posture_rows() -> list[dict[str, Any]]:
    """Per-route billing posture; only verified routes may auto-route."""
    try:
        payload = plans_ops.show_postures(date.today())
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    for row in payload.get("postures", []):
        if not isinstance(row, dict):
            continue
        status = str(row.get("status") or "UNVERIFIED")
        rows.append(
            {
                "selector": row.get("selector"),
                "posture_class": row.get("class"),
                "verified_on": row.get("verified_on"),
                "expires_on": row.get("expires_on"),
                "status": status,
                "auto_routing": status in ("OK", "EXPIRING"),
                "confirmed": status in ("OK", "EXPIRING", "ATTENDED_ONLY", "METERED"),
                "state_class": _pill_class(status),
            }
        )
    return rows






#: Owner-facing names for the upstream providers an aggregator account (OpenCode,
#: a custom API connection) routes to; the prefix is the model id's first segment.





def _planner_view() -> dict[str, Any]:
    """The agent that plans the work: whoever holds the MCP writer lease."""
    from saberops.mcp.lease import OrchestratorLease, pid_is_alive
    from saberops.mcp.switch import mcp_enabled

    try:
        record = OrchestratorLease().read()
    except Exception:
        record = None
    if not mcp_enabled():
        return {"connected": False, "name": "", "since": None, "holder": None, "off": True}
    if record is None or not pid_is_alive(record.pid):
        return {"connected": False, "name": "", "since": None, "holder": None}
    raw = record.client or ""
    name = raw.replace("-", " ").replace("_", " ").strip().title() if raw else "Your agent"
    return {"connected": True, "name": name, "since": record.acquired_at, "holder": record.holder}









def _question_rows() -> dict[str, Any]:
    """Owner question queue: open/due questions first, then the full ledger."""
    today = date.today()
    try:
        questions = _question_queue().questions()
    except Exception as exc:
        return {"error": f"{exc.__class__.__name__}: {exc}", "pending": [], "all": []}
    pending: list[dict[str, Any]] = []
    all_rows: list[dict[str, Any]] = []
    for question in questions:
        row = question_view(question, today)
        row["state_class"] = _pill_class(question.status.value)
        all_rows.append(row)
        if question.status is QuestionStatus.OPEN or is_due(question, today):
            pending.append(row)
    return {"error": None, "pending": pending, "all": all_rows}


def _self_answered_question_rows(limit: int = 20) -> dict[str, Any]:
    """Routine questions the planning session answered, newest last."""
    try:
        decisions = _question_queue().orchestrator_alone_decisions()
    except Exception as exc:
        return {"error": f"{exc.__class__.__name__}: {exc}", "rows": [], "count": 0}
    rows = [decision.to_mapping() for decision in decisions[-limit:]]
    return {"error": None, "rows": rows, "count": len(decisions)}


def _standing_decision_rows() -> dict[str, Any]:
    """Standing decisions with their expiration windows, read from durable state."""
    path = _unattended_store_path()
    view: dict[str, Any] = {
        "path": str(path),
        "present": False,
        "rows": [],
        "error": None,
    }
    if not path.exists():
        return view
    view["present"] = True
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        view["error"] = f"{exc.__class__.__name__}: {exc}"
        return view
    raw = payload.get("standing_decisions") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        view["error"] = "document has no standing_decisions list"
        return view
    now = datetime.now(UTC).isoformat()
    rows: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            scope_raw = entry.get("scope")
            if not isinstance(scope_raw, dict):
                raise ValueError("scope must be a mapping")
            scope = DecisionScope(
                project_id=str(scope_raw.get("project_id") or ""),
                task_id=str(scope_raw.get("task_id") or ""),
                paths=tuple(str(item) for item in scope_raw.get("paths") or ()),
            )
            decision = StandingDecision(
                standing_decision_id=str(entry.get("standing_decision_id") or ""),
                scope=scope,
                decision_key=str(entry.get("decision_key") or ""),
                owner_words=str(entry.get("owner_words") or ""),
                answered_by=str(entry.get("answered_by") or ""),
                granted_at_turn=entry.get("granted_at_turn"),
                ttl_turns=entry.get("ttl_turns"),
                expires_at=entry.get("expires_at"),
            )
        except Exception as exc:
            rows.append(
                {
                    "error": f"{exc.__class__.__name__}: {exc}",
                    "state_class": "fail",
                }
            )
            continue
        live = decision.is_live(now=now, current_turn=0)
        rows.append(
            {
                "standing_decision_id": decision.standing_decision_id,
                "decision_key": decision.decision_key,
                "project_id": decision.scope.project_id,
                "task_id": decision.scope.task_id,
                "owner_words": decision.owner_words,
                "expires_at": decision.expires_at,
                "ttl_turns": decision.ttl_turns,
                "granted_at_turn": decision.granted_at_turn,
                "live": live,
                "state_class": "ok" if live else "warn",
            }
        )
    view["rows"] = rows
    return view


def _never_delegable_rows() -> list[dict[str, Any]]:
    """Decision kinds the engine may never decide without the owner."""
    return [
        {"kind": kind.value, "description": NEVER_DELEGABLE_POLICY[kind]}
        for kind in NEVER_DELEGABLE_KINDS
    ]


def _owner_mode_view() -> dict[str, Any]:
    """The effective owner mode, its choices and each decision it permits."""
    try:
        view = owner_mode_view()
        view["choices"] = list_owner_modes()
        view["error"] = None
        return view
    except Exception as exc:
        return {
            "error": f"{exc.__class__.__name__}: {exc}",
            "mode": "UNKNOWN",
            "declared_mode": "UNKNOWN",
            "label": "unavailable",
            "until": None,
            "expired": False,
            "config_path": None,
            "choices": [],
            "decisions": {},
            "max_jobs": None,
            "away_bounded": False,
        }


def _work_gate_view(db: Database) -> dict[str, Any]:
    """The work gate: paused or running, why, and what is parked."""
    try:
        from saberops.runtime_gate import gate_view

        view = gate_view(db=db)
        view["error"] = None
        return view
    except Exception as exc:  # pragma: no cover - a page must never fail here
        return {
            "error": f"{exc.__class__.__name__}: {exc}",
            "closed": True,
            "label": "unavailable",
            "reason": "gate_unreadable",
            "detail": "",
            "since": None,
            "paused_jobs": [],
        }



OWNER_PAGES: tuple[str, ...] = ("setup", "accounts", "questions")


def _nav_state() -> dict[str, Any]:
    """Sidebar badges for every page: pending questions and setup attention.

    Read-only and never raises; an unreadable store simply shows no badge.
    """
    try:
        pending = len(_question_rows().get("pending") or [])
    except Exception:
        pending = 0
    try:
        attention = any(row.get("state") != "ok" for row in _diagnostics_rows())
    except Exception:
        attention = False
    return {
        "pending_questions": pending,
        "setup_attention": attention,
        "version": saberops_version(),
    }


def _first_run_required(service: OrchestratorService) -> bool:
    """True only when the store holds no runs, bindings, projects or models.

    Account-backed connections are static declarations that always exist, so
    they never signal a configured install; discovered catalog models do.  Any
    unreadable store returns ``False`` so an uncertain state never surprises the
    owner by swapping the dashboard for the onboarding page.
    """
    try:
        if service.list_runs(limit=1):
            return False
    except Exception:
        return False
    try:
        if _load_owner_bindings_safely().registry.list_bindings():
            return False
    except Exception:
        return False
    try:
        if ProjectRegistry().list_projects():
            return False
    except Exception:
        return False
    try:
        if access_ops.load_catalog_safely().list_models():
            return False
    except Exception:
        return False
    return True


def _onboarding_context(
    service: OrchestratorService,
    db: Database,
    request: Request,
    *,
    error: str | None = None,
    notice: str | None = None,
) -> dict[str, Any]:
    """Build the kept owner-page context (setup, accounts, questions)."""
    return {
        "csrf_token": _CSRF_TOKEN,
        "saberops_version": saberops_version(),
        "error": error,
        "notice": notice,
        "diagnostics": _diagnostics_rows(),
        "vendor_logins": _vendor_login_rows(),
        "connections": _connection_rows(),
        "models": _model_rows(),
        "postures": _posture_rows(),
        "planner": _planner_view(),
        "questions": _question_rows(),
        "self_answered_questions": _self_answered_question_rows(),
        "standing_decisions": _standing_decision_rows(),
        "never_delegable": _never_delegable_rows(),
        "owner_mode": _owner_mode_view(),
        "work_gate": _work_gate_view(db),
    }


def create_app(
    db_path: str | Path | None = None,
    repo_path: str | Path | None = None,
    registry: AdapterRegistry | None = None,
    worktree_base: Path | None = None,
    state_root: Path | str | None = None,
    seed_cwd_if_empty: bool = True,
) -> FastAPI:
    """Build the kept owner-pages application.

    The console serves the kept pages: first-run Setup, Accounts and
    Questions, plus ``/health`` for stale-install diagnostics.  The engine and
    the ``orch`` CLI own run monitoring, run reports, owner accept/reject, plan
    and wave scoring, the routing editor and the storage cleanup.  ``repo_path``
    is accepted for launch compatibility.  Passing an explicit ``db_path`` pins
    the whole app to that single store.
    """
    del seed_cwd_if_empty
    resolved_db_path: str | Path | None = db_path
    if resolved_db_path is None and repo_path is not None:
        from saberops.runtime import project_db_path_for_repo

        resolved_db_path = project_db_path_for_repo(repo_path, state_root=state_root)
    resolved_registry = registry if registry is not None else AdapterRegistry.default()

    runtimes = ProjectRuntimeManager(
        registry=resolved_registry,
        worktree_base=worktree_base,
        pinned_db_path=resolved_db_path,
        state_root=state_root,
    )

    pinned = runtimes.legacy()
    db = pinned.db
    service = pinned.service
    supervisor = pinned.supervisor

    try:
        from saberops.capacity.loop import ensure_scheduler_running

        ensure_scheduler_running(db)
    except Exception:
        import logging

        logging.getLogger(__name__).exception("Could not start scheduler")

    app = FastAPI(title="SaberOps Status")
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
    templates.env.globals["fmt"] = format_timestamp
    templates.env.globals["nav_state"] = _nav_state

    @app.middleware("http")
    async def _origin_guard(request: Request, call_next: Callable[..., Any]) -> Response:
        """Refuse untrusted-Host requests and cross-site/foreign-origin writes.

        Host/Origin headers never choose what the actions do; a foreign
        Host or a foreign origin fails closed.  The Host allowlist
        applies to every private route -- setup, accounts and questions,
        the first-run and account POSTs, and API docs -- so a missing
        Origin header (curl, DNS rebinding, old browser) can never smuggle
        a foreign Host past the guard.  Every POST is treated as a state
        mutation for the Origin / sec-fetch-site check.  ``/health`` and
        ``/static/*`` stay open by design (non-secret diagnostics and
        bundled assets).
        """
        path = request.url.path
        if _is_private_web_path(path) and not _host_header_allowed(request):
            return JSONResponse(
                {"error": "Refuse request with untrusted Host header; use the local SaberOps page."},
                status_code=403,
            )
        mutation = request.method == "POST"
        origin = request.headers.get("origin")
        if mutation and (request.headers.get("sec-fetch-site") == "cross-site"
                         or (origin is not None and origin != str(request.base_url).rstrip("/"))):
            return JSONResponse({"error": "Use the local SaberOps page for this action."}, status_code=403)
        response: Response = await call_next(request)
        return response
    app.state.db = db
    app.state.service = service
    app.state.supervisor = supervisor
    app.state.runtimes = runtimes

    def onboarding_page(
        request: Request,
        *,
        page: str,
        error: str | None = None,
        notice: str | None = None,
        status_code: int = 200,
        first_run: bool = False,
    ) -> HTMLResponse:
        """Render one kept owner page (setup/accounts/questions) for GET and POST."""
        context = _onboarding_context(service, db, request, error=error, notice=notice)
        context["first_run"] = first_run
        if page == "setup":
            from saberops.agent_connect import agent_rows
            from saberops.mcp.switch import mcp_enabled

            context["agents"] = {"rows": agent_rows(), "enabled": mcp_enabled()}
        if page == "accounts":
            from saberops.accounts_view import accounts_view

            context["accounts"] = accounts_view(db)
        return templates.TemplateResponse(
            request, f"{page}.html", context, status_code=status_code
        )

    @app.get("/", name="home")
    def home(request: Request) -> RedirectResponse:
        """The app opens on Setup."""
        return RedirectResponse(url="/setup", status_code=303)

    @app.get("/first-run", name="first_run")
    def first_run(request: Request) -> RedirectResponse:
        """Legacy onboarding URL: Setup now owns first-run, keeping the query."""
        query = request.url.query
        return RedirectResponse(url="/setup" + (f"?{query}" if query else ""), status_code=303)

    def _doctor_repo() -> Path:
        return Path(repo_path).resolve() if repo_path is not None else Path.cwd()

    @app.get("/doctor", response_class=HTMLResponse, name="doctor_page")
    def doctor_page(request: Request) -> HTMLResponse:
        """Read-only doctor report; the same findings as the CLI and MCP tool."""
        notice = None
        if request.query_params.get("fixed") == "1":
            notice = "Repairs were attempted; the recheck below decides what remains."
        return templates.TemplateResponse(
            request,
            "doctor.html",
            {
                "doctor": _doctor_view(db, _doctor_repo()),
                "notice": notice,
                "error": None,
                "csrf_token": _CSRF_TOKEN,
            },
        )

    @app.post("/doctor/fix", name="doctor_fix")
    def doctor_fix_route(
        request: Request,
        csrf: Annotated[str, Form()] = "",
        delete_legacy: Annotated[str, Form()] = "",
    ) -> Response:
        """Owner-approved repair from the local page; the recheck decides."""
        if not _csrf_valid(csrf):
            return templates.TemplateResponse(
                request,
                "doctor.html",
                {
                    "doctor": _doctor_view(db, _doctor_repo()),
                    "notice": None,
                    "error": "Refused: missing or invalid confirmation token.",
                    "csrf_token": _CSRF_TOKEN,
                },
                status_code=403,
            )
        from saberops.doctor import run_doctor

        report = run_doctor(
            _doctor_repo(),
            db.db_path,
            fix=True,
            allow_legacy_delete=bool(delete_legacy),
        )
        return templates.TemplateResponse(
            request,
            "doctor.html",
            {
                "doctor": {
                    "error": None,
                    "counts": report.counts(),
                    "findings": [finding.as_dict() for finding in report.findings],
                    "actions": [action.as_dict() for action in report.actions],
                },
                "notice": "Repairs were attempted; the recheck below decides what remains.",
                "error": None,
                "csrf_token": _CSRF_TOKEN,
            },
        )

    def _owner_page_route(page: str) -> Callable[[Request], HTMLResponse]:
        def render(request: Request) -> HTMLResponse:
            return onboarding_page(
                request,
                page=page,
                notice=_onboarding_notice(request),
                first_run=page == "setup" and _first_run_required(service),
            )

        render.__name__ = f"{page}_page"
        return render

    for _page in OWNER_PAGES:
        app.add_api_route(
            f"/{_page}",
            _owner_page_route(_page),
            methods=["GET"],
            response_class=HTMLResponse,
            name=f"{_page}_page",
        )

    @app.post("/first-run/discover", name="discover_models")
    def discover_models(
        request: Request,
        csrf: Annotated[str, Form()] = "",
        connection_id: Annotated[str, Form()] = "",
    ) -> Response:
        """Run bounded model discovery for one connection (evidence only)."""
        page = "setup"
        if not _csrf_valid(csrf):
            return onboarding_page(
                request,
                page=page,
                error="Refused: missing or invalid confirmation token.",
                status_code=403,
            )
        wanted = connection_id.strip()
        if not wanted:
            return onboarding_page(
                request,
                page=page, error="Choose a connection to discover.", status_code=400
            )
        try:
            outcome = access_ops.discover_access_models(wanted)
        except Exception as exc:
            return onboarding_page(
                request,
                page=page,
                error=f"The model list could not be refreshed: {exc}",
                status_code=400,
            )
        if not outcome.get("ok"):
            reason = str(outcome.get("reason") or "")
            plain = {
                "ENDPOINT_UNREACHABLE": "the tool did not answer",
                "BACKEND_NOT_INSTALLED": "its command-line tool is not installed",
                "NOT_AUTHENTICATED": "you are not signed in to it",
            }.get(reason, "it could not list its models")
            return onboarding_page(
                request,
                page=page,
                error=(
                    f"The model list for {wanted} was not refreshed: {plain}. "
                    "The last list is kept and stays hidden until a refresh works."
                ),
                status_code=502,
            )
        return RedirectResponse(
            url=f"/{page}?discovered=1&connection={quote(wanted)}", status_code=303
        )

    @app.post("/first-run/posture/verify", name="verify_posture_route")
    def verify_posture_route(
        request: Request,
        csrf: Annotated[str, Form()] = "",
        selector: Annotated[str, Form()] = "",
        posture_class: Annotated[str, Form()] = "",
        policy_urls: Annotated[str, Form()] = "",
        note: Annotated[str, Form()] = "",
        return_to: Annotated[str, Form()] = "",
    ) -> Response:
        """Record the owner's attestation for one billing-posture selector."""
        page = "accounts" if return_to == "accounts" else "setup"
        if not _csrf_valid(csrf):
            return onboarding_page(
                request,
                page=page,
                error="Refused: missing or invalid confirmation token.",
                status_code=403,
            )
        urls = tuple(line.strip() for line in policy_urls.splitlines() if line.strip())
        try:
            plans_ops.verify_posture(
                selector.strip(), urls, note or None, posture_class.strip() or None, None
            )
        except plans_ops.PlansOperationError as exc:
            return onboarding_page(
                request,
                page=page, error=f"Billing not confirmed: {exc}", status_code=400
            )
        return RedirectResponse(url=f"/{page}?posture_saved=1", status_code=303)

    @app.post("/setup/agents", name="connect_agent")
    def connect_agent(
        request: Request,
        csrf: Annotated[str, Form()] = "",
        agent: Annotated[str, Form()] = "",
        action: Annotated[str, Form()] = "connect",
    ) -> Response:
        """Owner action: register (or remove) SaberOps in one agent CLI."""
        from saberops.agent_connect import agent_by_slug, connect, disconnect

        if not _csrf_valid(csrf):
            return onboarding_page(
                request, page="setup",
                error="Refused: missing or invalid confirmation token.", status_code=403,
            )
        if agent_by_slug(agent) is None or action not in ("connect", "disconnect"):
            return onboarding_page(
                request, page="setup", error="Choose an agent to connect.", status_code=400
            )
        result = connect(agent) if action == "connect" else disconnect(agent)
        if not result.ok:
            message = result.message
            if result.snippet:
                message += " Add it by hand: " + " ".join(result.snippet.split())
            return onboarding_page(request, page="setup", error=message, status_code=400)
        return RedirectResponse(
            url=f"/setup?agent={quote(agent)}&agent_action={quote(action)}", status_code=303
        )

    @app.post("/setup/mcp-switch", name="mcp_switch")
    def mcp_switch(
        request: Request,
        csrf: Annotated[str, Form()] = "",
        enabled: Annotated[str, Form()] = "",
    ) -> Response:
        """Owner action: the master switch for every connected agent."""
        from saberops.mcp.switch import set_mcp_enabled

        if not _csrf_valid(csrf):
            return onboarding_page(
                request, page="setup",
                error="Refused: missing or invalid confirmation token.", status_code=403,
            )
        set_mcp_enabled(enabled == "on")
        return RedirectResponse(url=f"/setup?mcp={'on' if enabled == 'on' else 'off'}", status_code=303)

    @app.post("/accounts/confirm-all", name="confirm_all_billing")
    def confirm_all_billing(
        request: Request,
        csrf: Annotated[str, Form()] = "",
    ) -> Response:
        """Owner action: confirm every account's billing as currently recorded.

        Re-confirms each non-forbidden record with its existing class and
        policy links (the owner's statement that they still hold); records
        that still need links are left for the owner to fill in.
        """
        from saberops.accounts_view import accounts_view

        if not _csrf_valid(csrf):
            return onboarding_page(
                request, page="accounts",
                error="Refused: missing or invalid confirmation token.", status_code=403,
            )
        view = accounts_view(db)
        rows = [r for card in view["cards"] for r in card["billing"]] + view["others"]
        confirmed, skipped = 0, 0
        for row in rows:
            if row["class"] == "X":
                continue
            if not row["urls"] and row["class"] in ("A", "B", "C"):
                skipped += 1
                continue
            try:
                plans_ops.verify_posture(row["selector"], (), None, None, None)
            except plans_ops.PlansOperationError:
                skipped += 1
                continue
            confirmed += 1
        return RedirectResponse(
            url=f"/accounts?confirmed_all={confirmed}&skipped={skipped}", status_code=303
        )

    @app.post("/accounts/check", name="check_account_usage")
    def check_account_usage(
        request: Request,
        csrf: Annotated[str, Form()] = "",
        account: Annotated[str, Form()] = "",
    ) -> Response:
        """Owner-initiated, zero-inference usage reading for one account."""
        from saberops.accounts_view import ACCOUNTS, check_usage

        if not _csrf_valid(csrf):
            return onboarding_page(
                request,
                page="accounts",
                error="Refused: missing or invalid confirmation token.",
                status_code=403,
            )
        if account not in {a.slug for a in ACCOUNTS}:
            return onboarding_page(
                request, page="accounts", error="Choose an account to check.", status_code=400
            )
        outcome = check_usage(db, account)
        return RedirectResponse(
            url=f"/accounts?checked={quote(account)}&outcome={quote(outcome)}", status_code=303
        )

    @app.post("/questions/{question_id}/answer", name="answer_question")
    def answer_question_route(
        request: Request,
        question_id: str,
        csrf: Annotated[str, Form()] = "",
        answer_text: Annotated[str, Form()] = "",
        chosen_option: Annotated[str, Form()] = "",
    ) -> Response:
        """Record the owner's answer to one queued question, verbatim."""
        if not _csrf_valid(csrf):
            return onboarding_page(
                request,
                page="questions",
                error="Refused: missing or invalid confirmation token.",
                status_code=403,
            )
        if not answer_text.strip():
            return onboarding_page(
                request,
                page="questions", error="An owner answer requires words.", status_code=400
            )
        try:
            _question_queue().answer(
                question_id,
                words=answer_text,
                authority=AnswerAuthority.OWNER,
                answered_by="owner",
                answered_by_role=AskRole.OWNER,
                chosen_option=chosen_option.strip() or None,
            )
        except QuestionError as exc:
            return onboarding_page(
                request,
                page="questions", error=f"Answer not recorded: {exc}", status_code=400
            )
        return RedirectResponse(url="/questions?answered=1", status_code=303)

    @app.post("/questions/mode", name="set_owner_mode_route")
    def set_owner_mode_route(
        request: Request,
        csrf: Annotated[str, Form()] = "",
        mode: Annotated[str, Form()] = "",
        until: Annotated[str, Form()] = "",
        max_jobs: Annotated[str, Form()] = "",
        clear: Annotated[str, Form()] = "",
    ) -> Response:
        """Owner-only mode switch from the Questions page."""
        if not _csrf_valid(csrf):
            return onboarding_page(
                request,
                page="questions",
                error="Refused: missing or invalid confirmation token.",
                status_code=403,
            )
        try:
            if clear == "yes" or not mode.strip():
                clear_owner_mode()
            else:
                budget = int(max_jobs.strip()) if max_jobs.strip() else None
                set_owner_mode(
                    parse_owner_mode(mode.strip()),
                    until=until.strip() or None,
                    max_jobs=budget,
                    set_by="owner",
                )
        except (OwnerModeError, OSError, ValueError) as exc:
            return onboarding_page(
                request,
                page="questions",
                error=f"Owner mode not changed: {exc}",
                status_code=400,
            )
        return RedirectResponse(url="/questions?mode_saved=1", status_code=303)

    @app.post("/questions/pause", name="pause_work_route")
    def pause_work_route(
        request: Request,
        csrf: Annotated[str, Form()] = "",
        reason: Annotated[str, Form()] = "",
        confirm: Annotated[str, Form()] = "",
    ) -> Response:
        """Owner-only instant stop from the Questions page."""
        if not _csrf_valid(csrf):
            return onboarding_page(
                request,
                page="questions",
                error="Refused: missing or invalid confirmation token.",
                status_code=403,
            )
        if confirm != "yes":
            return onboarding_page(
                request,
                page="questions",
                error="Pausing needs your explicit confirmation.",
                status_code=400,
            )
        from saberops.pause_work import pause_now

        try:
            pause_now(db, detail=reason.strip() or "the owner paused all work", by="owner")
        except OSError as exc:
            return onboarding_page(
                request,
                page="questions",
                error=f"Work was not paused: {exc}",
                status_code=400,
            )
        return RedirectResponse(url="/questions?paused=1", status_code=303)

    @app.post("/questions/resume", name="resume_work_route")
    def resume_work_route(
        request: Request,
        csrf: Annotated[str, Form()] = "",
        confirm: Annotated[str, Form()] = "",
    ) -> Response:
        """Owner-only confirmed resume from the Questions page."""
        if not _csrf_valid(csrf):
            return onboarding_page(
                request,
                page="questions",
                error="Refused: missing or invalid confirmation token.",
                status_code=403,
            )
        if confirm != "yes":
            return onboarding_page(
                request,
                page="questions",
                error="Resuming needs your explicit confirmation.",
                status_code=400,
            )
        from saberops.pause_work import resume_work

        try:
            resume_work(db, by="owner")
        except OSError as exc:
            return onboarding_page(
                request,
                page="questions",
                error=f"Work was not resumed: {exc}",
                status_code=400,
            )
        return RedirectResponse(url="/questions?resumed=1", status_code=303)

    @app.post("/questions/{question_id}/defer", name="defer_question")
    def defer_question_route(
        request: Request,
        question_id: str,
        csrf: Annotated[str, Form()] = "",
        until: Annotated[str, Form()] = "",
        note: Annotated[str, Form()] = "",
    ) -> Response:
        """Defer one question until an explicit ISO date."""
        if not _csrf_valid(csrf):
            return onboarding_page(
                request,
                page="questions",
                error="Refused: missing or invalid confirmation token.",
                status_code=403,
            )
        try:
            _question_queue().defer(
                question_id,
                until=until.strip(),
                deferred_by="owner",
                deferred_by_role=AskRole.OWNER,
                note=note,
            )
        except QuestionError as exc:
            return onboarding_page(
                request,
                page="questions", error=f"Question not deferred: {exc}", status_code=400
            )
        return RedirectResponse(url="/questions?deferred=1", status_code=303)

    @app.get("/health")
    def health() -> JSONResponse:
        """Expose non-secret runtime identity for stale-install diagnostics."""
        source_root = _PACKAGE_DIR.parents[1]
        sha, tree_state = _resolve_source_identity(_PACKAGE_DIR)
        return JSONResponse({
            "product": "SaberOps",
            "version": saberops_version(),
            "package_path": str(_PACKAGE_DIR),
            "source_path": str(source_root),
            "source_git_sha": sha,
            "source_tree_state": tree_state,
            "database_path": str(db.db_path),
            "server_pid": os.getpid(),
            "orch_executable": resolve_orch_executable(),
        })

    refresh_stop = threading.Event()

    @app.on_event("startup")
    def _refresh_model_lists() -> None:
        """Keep account model lists current without the owner pressing Refresh.

        On start and then daily, re-list the models of every account whose
        list is missing, out of date or older than a day. Listing is
        read-only metadata (no prompt). Off under tests and with
        SABEROPS_NO_AUTO_REFRESH=1.
        """
        if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("SABEROPS_NO_AUTO_REFRESH"):
            return

        def loop() -> None:
            while not refresh_stop.is_set():
                try:
                    access_ops.refresh_outdated_model_lists()
                except Exception:
                    pass
                refresh_stop.wait(24 * 3600)

        threading.Thread(target=loop, name="saberops-model-refresh", daemon=True).start()

    @app.on_event("shutdown")
    def _shutdown() -> None:
        refresh_stop.set()
        """Server shutdown releases cached runtimes without corrupting state."""
        try:
            runtimes.shutdown()
        except Exception:
            pass

    return app
