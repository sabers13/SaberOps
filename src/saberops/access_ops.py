"""Owner model-access connection operations (service layer).

Single service-layer authority for the ``orch access`` commands, and the
seam the future MCP server will call.  Every function here is the ONE
service-layer function for its operation; the CLI is a thin wrapper that
only parses arguments and renders output.

All behavior mirrors the removed ``/access`` web routes exactly (see
``git show orch/T-003-config-pinning:src/saberops/web.py``): owner
authority, validation, fail-closed persistence, and the no-inference
rule for test/discover (only the declared non-inference status command
or an unauthenticated reachability probe runs; credentials are never
read, let alone transmitted).  ``verify_access_model`` is the explicit
owner-authorized bounded model check the ``/access/verify-model`` route
performed -- it never runs implicitly.

Discovery execution itself stays housed in :mod:`saberops.web`
(:func:`saberops.web._discover_connection` and
:func:`saberops.web._probe_api_connection`), imported locally to keep
the ``web -> service`` module graph cycle-free.  Nothing is duplicated.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from saberops.model_access import (
    ACCOUNT_BACKENDS,
    AccessRegistry,
    AccessStore,
    AccessStoreError,
    AuthMode,
    ModelCatalog,
    ModelCatalogStore,
    ModelCatalogStoreError,
    ProviderConnection,
    ProviderReadinessEvidence,
    ReadinessStore,
    ReadinessStoreError,
    build_access_registry,
    preset_connection,
)
from saberops.model_access.connections import AccessProtocol
from saberops.model_access.discovery import apply_discovery_result
from saberops.model_access.readiness_service import ReadinessService
from saberops.models import ProviderReadiness
from saberops.project import owner_state_dir


class AccessOperationError(ValueError):
    """Fail-closed refusal of a model-access connection operation."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def access_store() -> AccessStore:
    """Return the user-connection store (same override seam as the web routes)."""
    override = os.environ.get("ORCH_ACCESS_STORE", "").strip()
    if override:
        return AccessStore(Path(override).expanduser())
    return AccessStore(owner_state_dir() / AccessStore.FILENAME)


def model_catalog_store() -> ModelCatalogStore:
    """Return the discovered-model catalog store (same override seam)."""
    override = os.environ.get("ORCH_ACCESS_MODELS_STORE", "").strip()
    if override:
        return ModelCatalogStore(Path(override).expanduser())
    return ModelCatalogStore(owner_state_dir() / ModelCatalogStore.FILENAME)


def readiness_store() -> ReadinessStore:
    """Return the readiness-evidence store (same override seam)."""
    override = os.environ.get("ORCH_READINESS_STORE", "").strip()
    if override:
        return ReadinessStore(Path(override).expanduser())
    return ReadinessStore(owner_state_dir() / ReadinessStore.FILENAME)


def load_catalog_safely() -> ModelCatalog:
    """Load the last-observed catalog, degrading to empty on corrupt state."""
    try:
        return model_catalog_store().load()
    except ModelCatalogStoreError:
        return ModelCatalog()


def parse_public_headers(raw: str) -> dict[str, str]:
    """Parse literal ``Name: value`` public-header lines (fail closed).

    Same contract as the removed ``/access/connections`` route helper:
    a malformed line is identified by its 1-based line number and the
    structural defect only; submitted text is never echoed, so a
    rejected line carrying secret-shaped material can never be
    reflected into an error.
    """
    headers: dict[str, str] = {}
    for number, line in enumerate(raw.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        name, separator, value = stripped.partition(":")
        if not separator:
            raise ValueError(f"invalid public header line {number}: missing ':' separator")
        if not name.strip():
            raise ValueError(f"invalid public header line {number}: missing header name")
        if not value.strip():
            raise ValueError(f"invalid public header line {number}: missing header value")
        headers[name.strip()] = value.strip()
    return headers


def _require_registry() -> AccessRegistry:
    try:
        return access_store().load()
    except AccessStoreError as exc:
        raise AccessOperationError(
            "ACCESS_STORE_UNREADABLE", f"Access store unreadable: {exc}"
        ) from exc


def _require_connection(registry: AccessRegistry, connection_id: str) -> ProviderConnection:
    wanted = connection_id.strip()
    if not wanted:
        raise AccessOperationError("CONNECTION_ID_REQUIRED", "A connection id is required.")
    connection = registry.try_get(wanted)
    if connection is None:
        raise AccessOperationError(
            "UNKNOWN_CONNECTION", f"Unknown connection: {wanted}"
        )
    return connection


def _redact(message: str, credential_ref: str) -> str:
    submitted = credential_ref.strip()
    if submitted and submitted in message:
        return message.replace(submitted, "[redacted]")
    return message


def list_access_connections() -> dict[str, Any]:
    """List every connection with safe summaries, readiness, and catalog counts."""
    registry = _require_registry()
    catalog = load_catalog_safely()
    try:
        readiness = readiness_store().load()
    except ReadinessStoreError:
        readiness = None
    rows: list[dict[str, Any]] = []
    for connection in registry.list_connections():
        evidence = readiness.try_get(connection.connection_id) if readiness is not None else None
        rows.append(
            {
                **connection.safe_summary(),
                "credential_display": connection.credential_display,
                "readiness_state": evidence.state.value if evidence is not None else "UNKNOWN",
                "readiness_reason": evidence.reason if evidence is not None else "NO_EVIDENCE",
                "observed_at": evidence.observed_at if evidence is not None else None,
                "catalog_models": len(catalog.for_connection(connection.connection_id)),
            }
        )
    return {"connections": rows}


def show_access_connection(connection_id: str) -> dict[str, Any]:
    """Return one connection's safe summary plus readiness and catalog evidence."""
    registry = _require_registry()
    connection = _require_connection(registry, connection_id)
    catalog = load_catalog_safely()
    try:
        evidence = readiness_store().load().try_get(connection.connection_id)
    except ReadinessStoreError:
        evidence = None
    return {
        **connection.safe_summary(),
        "endpoint": connection.endpoint,
        "model": connection.model,
        "credential_display": connection.credential_display,
        "readiness_state": evidence.state.value if evidence is not None else "UNKNOWN",
        "readiness_reason": evidence.reason if evidence is not None else "NO_EVIDENCE",
        "observed_at": evidence.observed_at if evidence is not None else None,
        "catalog_models": [
            model.to_dict() for model in catalog.for_connection(connection.connection_id)
        ],
    }


def add_access_connection(
    *,
    connection_id: str,
    preset: str = "custom-openai-compatible",
    protocol: str = AccessProtocol.OPENAI_COMPATIBLE.value,
    endpoint: str = "",
    credential_ref: str = "",
    auth_header_name: str = "",
    model: str = "",
    public_headers: str = "",
) -> dict[str, Any]:
    """Save a user-owned API connection (references only, never secrets).

    Same validation and persistence as the removed
    ``POST /access/connections`` route: the credential field is a
    *reference* (``env:`` / ``profile:`` / ``store:`` handle); there is
    no field for a raw secret value anywhere.  A bounded discovery runs
    immediately after a successful save; it never fails the save and
    never erases durable state.
    """
    wanted = connection_id.strip()
    if not wanted:
        raise AccessOperationError("CONNECTION_ID_REQUIRED", "Connection name is required.")
    try:
        headers = parse_public_headers(public_headers)
    except ValueError as exc:
        raise AccessOperationError(
            "INVALID_PUBLIC_HEADERS", f"Invalid public headers: {exc}"
        ) from exc
    try:
        store = access_store()
        registry = store.load()
        if registry.try_get(wanted) is not None:
            raise ValueError(f"connection '{wanted}' already exists")
        if preset.strip() and preset.strip().lower() != "custom-openai-compatible":
            connection = preset_connection(
                preset.strip(),
                credential_ref=credential_ref,
                endpoint=endpoint,
                model=model,
                connection_id=wanted,
            )
            if auth_header_name.strip() or headers:
                connection = replace(
                    connection,
                    auth_header_name=auth_header_name.strip(),
                    public_headers=tuple(sorted(headers.items())),
                )
        else:
            try:
                access_protocol = AccessProtocol(protocol.strip())
            except ValueError as exc:
                raise ValueError(f"unknown protocol {protocol!r}") from exc
            connection = ProviderConnection(
                connection_id=wanted,
                provider="custom",
                protocol=access_protocol,
                backend=(
                    "openai-compatible-api"
                    if access_protocol is AccessProtocol.OPENAI_COMPATIBLE
                    else "custom-api"
                ),
                auth_mode=AuthMode.API_KEY,
                endpoint=endpoint,
                credential_ref=credential_ref,
                public_headers=tuple(sorted(headers.items())),
                auth_header_name=auth_header_name.strip(),
                model=model,
            )
        store.save(build_access_registry((*registry.list_connections(), connection)))
    except (ValueError, AccessStoreError) as exc:
        # Never reflect potential secret material: the submitted
        # reference is redacted from the error text (validation
        # messages may otherwise quote the offending value).
        raise AccessOperationError(
            "CONNECTION_NOT_SAVED",
            f"Connection not saved: {_redact(str(exc), credential_ref)}",
        ) from exc
    # Adding a provider makes its models available for selection, so run
    # one bounded discovery immediately after a successful save.  This
    # never fails the save itself and never erases durable state.
    discovery: dict[str, Any] = {"attempted": False}
    try:
        from saberops.web import _discover_connection

        saved_connection = access_store().load().try_get(wanted)
        if saved_connection is not None:
            from saberops.workers.registry import AdapterRegistry

            auto_result = _discover_connection(
                saved_connection, AdapterRegistry.default()
            )
            try:
                auto_catalog = model_catalog_store().load()
            except ModelCatalogStoreError:
                auto_catalog = ModelCatalog()
            auto_catalog = apply_discovery_result(auto_catalog, auto_result)
            try:
                model_catalog_store().save(auto_catalog)
            except ModelCatalogStoreError:
                auto_result = replace(auto_result, ok=False)
            discovery = {
                "attempted": True,
                "ok": auto_result.ok,
                "models_found": len(auto_result.models) if auto_result.ok else 0,
            }
    except (AccessStoreError, ValueError):
        discovery = {"attempted": True, "ok": False, "models_found": 0}
    return {
        "connection_id": wanted,
        "saved": True,
        "credential_display": connection.credential_display,
        "discovery": discovery,
    }


def _readiness_probe_payload(
    connection: ProviderConnection, evidence: ProviderReadinessEvidence
) -> dict[str, Any]:
    return {
        "connection_id": connection.connection_id,
        "ok": "TRUE" if evidence.state is ProviderReadiness.READY else "FALSE",
        "reason": evidence.reason,
        "detail": (
            (
                "endpoint metadata verified; authentication/model entitlement not proven; "
                if evidence.reason == "API_ENDPOINT_READY"
                else "readiness evidence persisted; "
            )
            + f"state={evidence.state.value} observed_at={evidence.observed_at}"
        ),
        "readiness_state": evidence.state.value,
        "readiness_reason": evidence.reason,
        "observed_at": evidence.observed_at,
    }


def test_access_connection(
    connection_id: str,
    *,
    which: Callable[[str], str | None] | None = None,
    run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, Any]:
    """Verify one connection and persist the classified result.

    Same authority as the removed ``POST /access/test`` route:
    account-backed (and supported API) connections verify through the
    canonical :class:`ReadinessService` refresh over the exact
    already-loaded connection -- only the declared non-inference status
    command runs, no model call occurs, and ``UNKNOWN`` stays
    ``UNKNOWN``.  Unsupported transports keep the unauthenticated
    reachability probe (the stored credential value is never read, let
    alone sent).
    """
    from saberops.workers.direct_api import supports_connection

    registry = _require_registry()
    connection = _require_connection(registry, connection_id)
    if connection.is_account_backed or supports_connection(connection):
        try:
            service = ReadinessService(
                registry,
                readiness_store(),
                which=which or shutil.which,
                run=run or subprocess.run,
            )
            evidence = service.refresh(connection)
        except ReadinessStoreError as exc:
            raise AccessOperationError(
                "READINESS_STORE_UNWRITABLE", f"Readiness store unwritable: {exc}"
            ) from exc
        return _readiness_probe_payload(connection, evidence)
    from saberops.web import _probe_api_connection

    outcome = _probe_api_connection(endpoint=connection.endpoint)
    return {"connection_id": connection.connection_id, **outcome}


def discover_access_models(connection_id: str) -> dict[str, Any]:
    """Discover the models one connection's provider/backend enumerates.

    Same authority as the removed ``POST /access/discover`` route:
    discovery is evidence only -- it writes the non-secret catalog store
    and can never rewrite or erase durable connection configuration; a
    failed discovery keeps prior evidence (marked stale by the canonical
    helper) and reports a classified reason.
    """
    from saberops.web import _discover_connection
    from saberops.workers.registry import AdapterRegistry

    registry = _require_registry()
    connection = _require_connection(registry, connection_id)
    result = _discover_connection(connection, AdapterRegistry.default())
    try:
        catalog = model_catalog_store().load()
    except ModelCatalogStoreError:
        catalog = ModelCatalog()
    catalog = apply_discovery_result(catalog, result)
    try:
        model_catalog_store().save(catalog)
    except ModelCatalogStoreError as exc:
        raise AccessOperationError(
            "CATALOG_NOT_SAVED", f"Model catalog not saved: {exc}"
        ) from exc
    return result.to_dict()


def refresh_outdated_model_lists(max_age_hours: float = 24.0) -> dict[str, str]:
    """Re-list models for account connections whose list is missing or old.

    Covers every installed account backend with a supported listing: no
    catalog entries yet, any entry marked stale, or the newest reading older
    than ``max_age_hours``. Returns ``{connection_id: outcome}``; a failure
    keeps the previous list (marked stale) exactly as a manual refresh does.
    """
    import shutil
    from datetime import UTC, datetime, timedelta

    from saberops.model_access import ACCOUNT_BACKENDS

    registry = _require_registry()
    catalog = load_catalog_safely()
    now = datetime.now(UTC)
    outcomes: dict[str, str] = {}
    for connection in registry.list_connections():
        if not connection.is_account_backed:
            continue
        descriptor = next(
            (b for b in ACCOUNT_BACKENDS if b.connection_id == connection.connection_id), None
        )
        if descriptor is None or not descriptor.has_model_listing:
            continue
        if shutil.which(descriptor.executable) is None:
            continue
        entries = [m for m in catalog.list_models() if m.connection_id == connection.connection_id]
        newest = max(
            (
                datetime.fromisoformat(m.discovered_at.replace("Z", "+00:00"))
                for m in entries
                if m.discovered_at
            ),
            default=None,
        )
        fresh = (
            entries
            and not any(m.is_stale for m in entries)
            and newest is not None
            and now - (newest if newest.tzinfo else newest.replace(tzinfo=UTC))
            < timedelta(hours=max_age_hours)
        )
        if fresh:
            outcomes[connection.connection_id] = "fresh"
            continue
        try:
            result = discover_access_models(connection.connection_id)
        except Exception:
            outcomes[connection.connection_id] = "failed"
            continue
        outcomes[connection.connection_id] = "refreshed" if result.get("ok") else "failed"
    return outcomes


_VERIFY_MESSAGES = {
    "READY": (
        "The selected model completed the access check. Readiness is saved for this "
        "account connection; other models may have different access or limits."
    ),
    "MODEL_CHECK_TIMEOUT": (
        "The check timed out. Open the CLI to check sign-in and network access, then retry."
    ),
    "MODEL_CHECK_FAILED": (
        "The CLI could not complete the check. Open it to check sign-in, model access "
        "and remaining allowance, then retry."
    ),
    "MODEL_CHECK_UNCONFIRMED": (
        "The CLI did not return a confirmed model response. Check its version and "
        "sign-in, then retry."
    ),
    "BACKEND_NOT_INSTALLED": "Install this CLI, restart SaberOps, then retry.",
    "CONNECTION_DISABLED": "Enable this connection before verifying access.",
}


def verify_access_model(
    connection_id: str,
    model_id: str,
    *,
    which: Callable[[str], str | None] | None = None,
) -> dict[str, Any]:
    """Explicit bounded model check for one account connection.

    Same authority as the removed ``POST /access/verify-model`` route:
    the connection must be a supported account connection, the model
    must be an exact current catalog entry for it, and the check runs
    through :meth:`ReadinessService.verify_model`.  This is an explicit
    owner-authorized check; it never runs implicitly and never backs an
    automatic fallback.
    """
    from saberops.model_access.model_verification import SUPPORTED_BACKENDS

    try:
        registry = access_store().load()
    except AccessStoreError as exc:
        raise AccessOperationError(
            "ACCESS_EVIDENCE_UNREADABLE",
            "Access evidence could not be read or saved. No readiness was assumed.",
        ) from exc
    connection = registry.try_get(connection_id.strip())
    descriptor = next(
        (entry for entry in ACCOUNT_BACKENDS if entry.connection_id == connection_id.strip()),
        None,
    )
    if (
        connection is None
        or descriptor is None
        or connection.backend != descriptor.backend
        or connection.provider != descriptor.provider
        or connection.backend not in SUPPORTED_BACKENDS
    ):
        raise AccessOperationError(
            "UNSUPPORTED_CONNECTION", "Choose a supported account connection."
        )
    model = next(
        (
            entry
            for entry in load_catalog_safely().for_connection(connection.connection_id)
            if entry.model_id == model_id.strip()
            and entry.is_execution_supported
            and not entry.is_stale
            and entry.provider == connection.provider
            and entry.backend == connection.backend
        ),
        None,
    )
    if model is None:
        raise AccessOperationError(
            "UNKNOWN_MODEL",
            "Choose a current model from this provider's catalog. Refresh models if needed.",
        )
    try:
        service = ReadinessService(
            registry, readiness_store(), which=which or shutil.which
        )
        evidence = service.verify_model(connection, model.model_id)
    except (AccessStoreError, ReadinessStoreError) as exc:
        raise AccessOperationError(
            "ACCESS_EVIDENCE_UNREADABLE",
            "Access evidence could not be read or saved. No readiness was assumed.",
        ) from exc
    return {
        "connection_id": evidence.connection_id,
        "model_id": model.model_id,
        "ok": "TRUE" if evidence.state == ProviderReadiness.READY else "FALSE",
        "reason": evidence.reason,
        "detail": _VERIFY_MESSAGES.get(evidence.reason, "Access remains unverified."),
        "readiness_state": evidence.state.value,
        "readiness_reason": evidence.reason,
        "observed_at": evidence.observed_at,
    }
