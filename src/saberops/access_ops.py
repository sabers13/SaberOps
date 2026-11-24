"""Owner model-access connection operations (service layer).

Single service-layer authority for the ``orch access`` commands, and the
seam the MCP server calls.  Every function here is the ONE service-layer
function for its operation; the CLI is a thin wrapper that only parses
arguments and renders output.

Every operation keeps the owner access rules: owner authority, validation,
fail-closed persistence, and the no-inference rule for test/discover (only
the declared non-inference status command or an unauthenticated reachability
probe runs; credentials are never read, let alone transmitted).
``verify_access_model`` is the explicit owner-authorized bounded model
check; it never runs implicitly.

Discovery execution itself stays housed in :mod:`saberops.web`
(:func:`saberops.web._discover_connection`), imported locally to keep
the ``web -> service`` module graph cycle-free.  Nothing is duplicated.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from saberops.model_access import (
    ACCOUNT_BACKENDS,
    AccessRegistry,
    AccessStore,
    AccessStoreError,
    ModelCatalog,
    ModelCatalogStore,
    ModelCatalogStoreError,
    ProviderConnection,
    ProviderReadinessEvidence,
    ReadinessStore,
    ReadinessStoreError,
)
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


def _readiness_probe_payload(
    connection: ProviderConnection,
    evidence: ProviderReadinessEvidence,
    *,
    missing_evidence: tuple[str, ...] = (),
) -> dict[str, Any]:
    remedy = ""
    if evidence.reason == "INSUFFICIENT_EVIDENCE" or evidence.state is ProviderReadiness.UNKNOWN:
        missing_text = (
            ", ".join(missing_evidence)
            if missing_evidence
            else "installed, authenticated, reachable"
        )
        remedy = (
            f"missing evidence ({missing_text}); "
            f"authenticate {connection.backend} "
            f"(e.g. `{connection.backend} auth login` or configure credentials), "
            f"then run `orch access discover {connection.connection_id}` and "
            f"`orch access test {connection.connection_id}`"
        )
    return {
        "connection_id": connection.connection_id,
        "ok": "TRUE" if evidence.state is ProviderReadiness.READY else "FALSE",
        "reason": evidence.reason,
        "detail": (
            "readiness evidence persisted; "
            f"state={evidence.state.value} observed_at={evidence.observed_at}"
        ),
        "readiness_state": evidence.state.value,
        "readiness_reason": evidence.reason,
        "observed_at": evidence.observed_at,
        **({"missing_evidence": list(missing_evidence)} if missing_evidence else {}),
        **({"remedy": remedy} if remedy else {}),
    }


def test_access_connection(
    connection_id: str,
    *,
    which: Callable[[str], str | None] | None = None,
    run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, Any]:
    """Verify one connection and persist the classified result.

    Account-backed (and supported API) connections verify through the
    canonical :class:`ReadinessService` refresh over the exact
    already-loaded connection -- only the declared non-inference status
    command runs, no model call occurs, and ``UNKNOWN`` stays
    ``UNKNOWN``. A stored direct-API row has no
    transport and is refused; the stored credential value is never read,
    let alone sent.
    """
    registry = _require_registry()
    connection = _require_connection(registry, connection_id)
    if not connection.is_account_backed:
        raise AccessOperationError(
            "UNSUPPORTED_CONNECTION",
            "Direct API connections were removed (H17); only declared CLI accounts are tested.",
        )
    try:
        service = ReadinessService(
            registry,
            readiness_store(),
            which=which or shutil.which,
            run=run or subprocess.run,
        )
        evidence = service.refresh(connection)
        missing_evidence: tuple[str, ...] = ()
        if evidence.state is ProviderReadiness.UNKNOWN:
            from saberops.model_access import TriState

            missing_parts: list[str] = []
            disc = (
                service.last_discovery(connection.connection_id)
                if hasattr(service, "last_discovery")
                else None
            )
            if disc is None:
                missing_parts.extend(["installed", "authenticated", "reachable"])
            else:
                if disc.installed is not TriState.TRUE:
                    missing_parts.append("installed")
                if disc.authenticated is not TriState.TRUE:
                    missing_parts.append("authenticated")
                if disc.reachable is not TriState.TRUE:
                    missing_parts.append("reachable")
            if not missing_parts:
                missing_parts.append("connection_test")
            missing_evidence = tuple(missing_parts)
    except ReadinessStoreError as exc:
        raise AccessOperationError(
            "READINESS_STORE_UNWRITABLE", f"Readiness store unwritable: {exc}"
        ) from exc
    return _readiness_probe_payload(connection, evidence, missing_evidence=missing_evidence)


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
