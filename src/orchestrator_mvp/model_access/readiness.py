"""Non-secret provider/backend dispatch-readiness evidence.

This module records observations about an already-declared connection.  It
does not select, filter, or dispatch candidates; a later control-plane change
may consume the evidence.  Derivation is pure and accepts only existing typed
probe observations, so it never invokes a model or resolves credentials.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from orchestrator_mvp.model_access.connections import (
    ConnectionTestReason,
    ConnectionTestResult,
    DiscoveryState,
    ProviderConnection,
    TriState,
)
from orchestrator_mvp.models import ProviderReadiness

__all__ = [
    "READINESS_REASONS",
    "ProviderReadinessEvidence",
    "ReadinessCatalog",
    "ReadinessEvidenceSource",
    "ReadinessStore",
    "ReadinessStoreError",
    "build_readiness_catalog",
    "derive_dispatch_readiness",
    "readiness_catalog_from_mapping",
    "readiness_evidence_from_mapping",
]


class ReadinessEvidenceSource(StrEnum):
    """Sanitized source of a readiness observation."""

    CONFIGURATION = "CONFIGURATION"
    CONNECTION_TEST = "CONNECTION_TEST"
    SUCCESSFUL_EXECUTION = "SUCCESSFUL_EXECUTION"


READINESS_REASONS: tuple[str, ...] = (
    "READY",
    "CONNECTION_DISABLED",
    *(reason.value for reason in ConnectionTestReason),
    "INSUFFICIENT_EVIDENCE",
    "STALE_READINESS_EVIDENCE",
    # Fixed sanitized token for readiness-authority construction/read
    # failures (unconstructable service, unreadable store).  Never an
    # exception repr, path, or secret.
    "READINESS_AUTHORITY_UNAVAILABLE",
)

_NOT_READY_REASONS = frozenset(
    {
        ConnectionTestReason.BACKEND_NOT_INSTALLED,
        ConnectionTestReason.NOT_AUTHENTICATED,
        ConnectionTestReason.CREDENTIAL_MISSING,
        ConnectionTestReason.CREDENTIAL_REJECTED,
        ConnectionTestReason.ENDPOINT_UNREACHABLE,
        ConnectionTestReason.CONFIG_INVALID,
        ConnectionTestReason.PROTOCOL_UNSUPPORTED,
    }
)


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("observed_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("observed_at must include a timezone")
    return parsed.astimezone(UTC)


@dataclass(frozen=True)
class ProviderReadinessEvidence:
    """One non-secret readiness observation for a declared connection."""

    connection_id: str
    provider: str
    backend: str
    state: ProviderReadiness
    reason: str
    observed_at: str
    source: ReadinessEvidenceSource

    def __post_init__(self) -> None:
        if not self.connection_id.strip():
            raise ValueError("readiness connection_id must not be empty")
        if not self.provider.strip():
            raise ValueError("readiness provider must not be empty")
        if not self.backend.strip():
            raise ValueError("readiness backend must not be empty")
        if self.reason not in READINESS_REASONS:
            raise ValueError("readiness reason is not recognized")
        _parse_timestamp(self.observed_at)
        object.__setattr__(self, "connection_id", self.connection_id.strip())
        object.__setattr__(self, "provider", self.provider.strip().lower())
        object.__setattr__(self, "backend", self.backend.strip().lower())
        object.__setattr__(self, "observed_at", self.observed_at.strip())

    def to_dict(self) -> dict[str, str]:
        """Canonical, safe representation suitable for later diagnostics."""
        return {
            "connection_id": self.connection_id,
            "provider": self.provider,
            "backend": self.backend,
            "state": self.state.value,
            "reason": self.reason,
            "observed_at": self.observed_at,
            "source": self.source.value,
        }

    def freshness(self, *, as_of: datetime, max_age: timedelta) -> ProviderReadinessEvidence:
        """Return stale positive evidence as UNKNOWN against caller policy."""
        if max_age < timedelta(0):
            raise ValueError("max_age must not be negative")
        if self.state is not ProviderReadiness.READY:
            return self
        reference = as_of if as_of.tzinfo is not None else as_of.replace(tzinfo=UTC)
        if reference.astimezone(UTC) - _parse_timestamp(self.observed_at) > max_age:
            return replace(
                self,
                state=ProviderReadiness.UNKNOWN,
                reason="STALE_READINESS_EVIDENCE",
            )
        return self


def derive_dispatch_readiness(
    connection: ProviderConnection,
    *,
    discovery: DiscoveryState | None = None,
    test_result: ConnectionTestResult | None = None,
    observed_at: str,
) -> ProviderReadinessEvidence:
    """Derive readiness from existing typed evidence without probing anything."""
    if discovery is not None and discovery.connection_id != connection.connection_id:
        raise ValueError("discovery evidence connection_id does not match connection")
    if test_result is not None and test_result.connection_id != connection.connection_id:
        raise ValueError("connection test result connection_id does not match connection")
    if not connection.enabled:
        return _evidence(
            connection,
            ProviderReadiness.NOT_READY,
            "CONNECTION_DISABLED",
            observed_at,
            ReadinessEvidenceSource.CONFIGURATION,
        )
    if not connection.is_configured:
        return _evidence(
            connection,
            ProviderReadiness.NOT_READY,
            ConnectionTestReason.CONFIG_INVALID.value,
            observed_at,
            ReadinessEvidenceSource.CONFIGURATION,
        )
    if test_result is not None and test_result.reason in _NOT_READY_REASONS:
        return _evidence(
            connection,
            ProviderReadiness.NOT_READY,
            test_result.reason.value,
            observed_at,
            ReadinessEvidenceSource.CONNECTION_TEST,
        )
    if discovery is not None:
        if discovery.installed is TriState.FALSE:
            return _evidence(connection, ProviderReadiness.NOT_READY,
                             ConnectionTestReason.BACKEND_NOT_INSTALLED.value, observed_at,
                             ReadinessEvidenceSource.CONNECTION_TEST)
        if discovery.authenticated is TriState.FALSE:
            return _evidence(connection, ProviderReadiness.NOT_READY,
                             ConnectionTestReason.NOT_AUTHENTICATED.value, observed_at,
                             ReadinessEvidenceSource.CONNECTION_TEST)
        if discovery.reachable is TriState.FALSE:
            return _evidence(connection, ProviderReadiness.NOT_READY,
                             ConnectionTestReason.ENDPOINT_UNREACHABLE.value, observed_at,
                             ReadinessEvidenceSource.CONNECTION_TEST)
        if (
            test_result is not None
            and test_result.ok
            and discovery.installed is TriState.TRUE
            and discovery.authenticated is TriState.TRUE
            and discovery.reachable is TriState.TRUE
        ):
            return _evidence(connection, ProviderReadiness.READY, "READY", observed_at,
                             ReadinessEvidenceSource.CONNECTION_TEST)
    return _evidence(connection, ProviderReadiness.UNKNOWN, "INSUFFICIENT_EVIDENCE", observed_at,
                     ReadinessEvidenceSource.CONNECTION_TEST)


def _evidence(
    connection: ProviderConnection,
    state: ProviderReadiness,
    reason: str,
    observed_at: str,
    source: ReadinessEvidenceSource,
) -> ProviderReadinessEvidence:
    return ProviderReadinessEvidence(
        connection_id=connection.connection_id,
        provider=connection.provider,
        backend=connection.backend,
        state=state,
        reason=reason,
        observed_at=observed_at,
        source=source,
    )


def readiness_evidence_from_mapping(payload: Mapping[str, Any]) -> ProviderReadinessEvidence:
    """Reconstruct sanitized readiness evidence from a durable mapping."""
    if not isinstance(payload, Mapping):
        raise ValueError("readiness evidence must be a mapping")
    try:
        state = ProviderReadiness(str(payload.get("state", "")))
        source = ReadinessEvidenceSource(str(payload.get("source", "")))
    except ValueError as exc:
        raise ValueError("invalid readiness state or source") from exc
    return ProviderReadinessEvidence(
        connection_id=str(payload.get("connection_id", "")),
        provider=str(payload.get("provider", "")),
        backend=str(payload.get("backend", "")),
        state=state,
        reason=str(payload.get("reason", "")),
        observed_at=str(payload.get("observed_at", "")),
        source=source,
    )


@dataclass(frozen=True)
class ReadinessCatalog:
    """Lookup-only collection of the last observation for each connection."""

    evidence: tuple[ProviderReadinessEvidence, ...] = ()
    schema_version: int = 1

    def __post_init__(self) -> None:
        ids = [entry.connection_id for entry in self.evidence]
        if len(ids) != len(set(ids)):
            raise ValueError("ReadinessCatalog: duplicate connection_id")

    def try_get(self, connection_id: str) -> ProviderReadinessEvidence | None:
        wanted = connection_id.strip()
        return next((entry for entry in self.evidence if entry.connection_id == wanted), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "evidence": [entry.to_dict() for entry in self.evidence],
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> ReadinessCatalog:
        if not isinstance(payload, Mapping) or payload.get("schema_version", 1) != 1:
            raise ValueError("unsupported readiness catalog")
        entries = payload.get("evidence", [])
        if not isinstance(entries, list):
            raise ValueError("readiness catalog evidence must be a list")
        return cls(evidence=tuple(readiness_evidence_from_mapping(entry) for entry in entries))


def build_readiness_catalog(
    evidence: Iterable[ProviderReadinessEvidence] = (),
) -> ReadinessCatalog:
    return ReadinessCatalog(evidence=tuple(evidence))


def readiness_catalog_from_mapping(payload: Mapping[str, Any]) -> ReadinessCatalog:
    return ReadinessCatalog.from_mapping(payload)


class ReadinessStoreError(ValueError):
    """Fail-closed error for readiness-evidence store operations."""


class ReadinessStore:
    """Durable store for sanitized observations, separate from connection config."""

    FILENAME = "access-readiness.json"

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)

    def load(self) -> ReadinessCatalog:
        if not self._path.exists():
            return ReadinessCatalog()
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            return ReadinessCatalog.from_mapping(payload)
        except (OSError, ValueError) as exc:
            raise ReadinessStoreError(f"readiness store unreadable: {exc}") from exc

    def save(self, catalog: ReadinessCatalog) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(catalog.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
            )
            tmp.replace(self._path)
        except OSError as exc:
            raise ReadinessStoreError(f"readiness store unwritable: {exc}") from exc

    def replace(self, evidence: ProviderReadinessEvidence) -> None:
        """Atomically retain other observations while replacing this connection."""
        catalog = self.load()
        self.save(
            ReadinessCatalog(
                evidence=tuple(
                    entry
                    for entry in catalog.evidence
                    if entry.connection_id != evidence.connection_id
                )
                + (evidence,)
            )
        )
