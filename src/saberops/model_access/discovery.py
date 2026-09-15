"""C15: discovered-model catalog and bounded discovery seam.

This module answers exactly one question:

    WHAT models does a connected provider/backend actually enumerate?

It never answers WHICH model Orchestrator should select.  The result is
*evidence*, never selection policy:

* no ranking and no preferred-model semantics;
* no quota policy, no reasoning-effort policy and no routing order;
* no provider alias rewriting;
* ``UNKNOWN`` discovery stays ``UNKNOWN`` and is never turned into
  ``FALSE`` merely because nothing could be observed.

Three facts are deliberately kept distinct -- ``DISCOVERED`` does not
mean ``CONFIGURED``, does not mean ``EXECUTABLE`` and does not mean
``ROUTING-ELIGIBLE``:

* ``discovery_status`` records whether the provider/backend actually
  enumerated this model (``TRUE`` / ``FALSE`` / ``UNKNOWN``).
* ``execution_support`` records whether an executable transport exists
  for this connection/backend/model.  It defaults to ``UNKNOWN`` and is
  only ever ``SUPPORTED`` when a caller supplies real evidence.
* routing eligibility is *derived by the existing C07 authority* from
  routing configuration, adapter availability, capability/quota/training
  policy -- this module never computes or stores it.

Discovery for account-backed backends happens through the backend's own
supported listing boundary (injectable here).  Discovery never scrapes
websites, never copies OAuth/session tokens and never reads a raw
credential value: a network model-list request may use an existing
credential only at the narrow transport boundary owned by the caller,
and no secret value can enter a :class:`DiscoveredModel` record or its
``to_dict`` / ``repr`` output.

A manual model id may remain available, but a manual entry is always
distinguishable from a discovered one: ``source = MANUAL`` and
``discovery_status = UNKNOWN`` (never verified by discovery).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

from saberops.model_access.connections import (
    ConnectionTestReason,
    DiscoveryState,
    ProviderConnection,
    TriState,
)

__all__ = [
    "DiscoveredModel",
    "ExecutionSupport",
    "ModelCatalog",
    "ModelDiscoveryResult",
    "ModelListingError",
    "ModelSource",
    "ModelCatalogStore",
    "ModelCatalogStoreError",
    "apply_discovery_result",
    "build_model_catalog",
    "discover_connection_models",
    "discovery_state_for",
    "discovered_model_from_mapping",
    "model_catalog_from_mapping",
    "replace_connection_models",
]


class ModelListingError(ValueError):
    """Typed refusal from a provider's model-listing boundary.

    The caller that owns the network/transport boundary raises this with
    a classified :class:`ConnectionTestReason` so discovery can report
    *why* it could not enumerate (unreachable, credential rejected,
    protocol unsupported) instead of collapsing every failure to a
    boolean.  It carries no secret material.
    """

    def __init__(self, reason: ConnectionTestReason, detail: str = "") -> None:
        super().__init__(detail or reason.value)
        self.reason = reason
        self.detail = detail


class ModelSource(StrEnum):
    """How a catalog entry came to be known.

    ``DISCOVERED`` entries were enumerated by the provider/backend
    through a supported listing boundary.  ``MANUAL`` entries were typed
    by the owner and are never discovery-verified.
    """

    DISCOVERED = "DISCOVERED"
    MANUAL = "MANUAL"


class ExecutionSupport(StrEnum):
    """Whether a real executable path exists for a model.

    ``SUPPORTED`` is only ever produced from positive evidence supplied
    by an execution-aware caller (an installed adapter whose provider /
    backend actually serves the connection).  ``UNSUPPORTED`` records an
    executable path that is known absent.  Everything unobserved stays
    ``UNKNOWN`` -- discovery must never manufacture support merely
    because a model was listed.
    """

    SUPPORTED = "SUPPORTED"
    UNSUPPORTED = "UNSUPPORTED"
    UNKNOWN = "UNKNOWN"


def _normalize_identity(
    *, connection_id: str, provider: str, backend: str, model_id: str
) -> tuple[str, str, str, str]:
    """Validate and normalize the four-part model identity (fail closed).

    The identity deliberately includes the connection, provider and
    backend context: the same ``model_id`` reached through two different
    providers or connections stays two distinct catalog entries.
    """
    cleaned_connection = connection_id.strip()
    if not cleaned_connection:
        raise ValueError("DiscoveredModel.connection_id must not be empty")
    cleaned_provider = provider.strip().lower()
    if not cleaned_provider:
        raise ValueError("DiscoveredModel.provider must not be empty")
    cleaned_backend = backend.strip().lower()
    if not cleaned_backend:
        raise ValueError("DiscoveredModel.backend must not be empty")
    cleaned_model = model_id.strip()
    if not cleaned_model:
        raise ValueError("DiscoveredModel.model_id must not be empty")
    return cleaned_connection, cleaned_provider, cleaned_backend, cleaned_model


def _normalize_capabilities(
    capabilities: Iterable[tuple[str, TriState]] | Mapping[str, TriState],
) -> tuple[tuple[str, TriState], ...]:
    """Normalize capability evidence without inventing any values.

    A capability whose state was never observed must be carried as
    ``UNKNOWN`` rather than dropped: dropping it would silently claim
    the capability does not exist.
    """
    items = capabilities.items() if isinstance(capabilities, Mapping) else capabilities
    cleaned: dict[str, TriState] = {}
    for name, state in items:
        key = str(name).strip().lower()
        if not key:
            raise ValueError("capability name must not be empty")
        if not isinstance(state, TriState):
            state = TriState(str(state))
        cleaned[key] = state
    return tuple(sorted(cleaned.items()))


@dataclass(frozen=True)
class DiscoveredModel:
    """One model entry associated with exactly one connection.

    The record holds evidence only.  It carries no selection preference,
    no ranking, no quota policy and no reasoning-effort policy, and it
    has no field capable of holding secret material: connection
    credentials stay in the existing sandbox/secret-reference authority
    and never enter this record.  ``repr`` / ``to_dict`` are therefore
    safe for logs, telemetry and durable evidence by construction.

    ``discovered_at`` is observational metadata: it is deliberately not
    part of :attr:`identity` and never influences routing or execution
    identity.
    """

    connection_id: str
    provider: str
    backend: str
    model_id: str
    display_name: str = ""
    source: ModelSource = ModelSource.DISCOVERED
    discovery_status: TriState = TriState.UNKNOWN
    execution_support: ExecutionSupport = ExecutionSupport.UNKNOWN
    capabilities: tuple[tuple[str, TriState], ...] = ()
    discovered_at: str | None = None
    notes: tuple[str, ...] = ()
    stale: bool = False

    def __post_init__(self) -> None:
        connection_id, provider, backend, model_id = _normalize_identity(
            connection_id=self.connection_id,
            provider=self.provider,
            backend=self.backend,
            model_id=self.model_id,
        )
        object.__setattr__(self, "connection_id", connection_id)
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "backend", backend)
        object.__setattr__(self, "model_id", model_id)
        object.__setattr__(self, "display_name", self.display_name.strip())
        object.__setattr__(
            self,
            "capabilities",
            _normalize_capabilities(self.capabilities),
        )
        object.__setattr__(self, "notes", tuple(str(note) for note in self.notes))
        object.__setattr__(self, "stale", bool(self.stale))
        if self.discovered_at is not None:
            object.__setattr__(self, "discovered_at", str(self.discovered_at))

    @property
    def identity(self) -> tuple[str, str, str, str]:
        """The context-bearing identity ``(connection, provider, backend, model)``."""
        return (self.connection_id, self.provider, self.backend, self.model_id)

    @property
    def is_discovery_verified(self) -> bool:
        """True only when the provider/backend actually enumerated this id."""
        return self.discovery_status is TriState.TRUE

    @property
    def is_execution_supported(self) -> bool:
        """True only when a real executable path was positively observed."""
        return self.execution_support is ExecutionSupport.SUPPORTED

    @property
    def is_stale(self) -> bool:
        """True when this entry is retained last-known evidence, not a fresh read.

        A failed/``UNKNOWN`` refresh may retain prior evidence, but it is
        marked stale so it can never masquerade as freshly discovered.
        """
        return self.stale

    def capability(self, name: str) -> TriState:
        """Return observed state of ``name`` (``UNKNOWN`` when unobserved)."""
        wanted = name.strip().lower()
        for key, state in self.capabilities:
            if key == wanted:
                return state
        return TriState.UNKNOWN

    def safe_summary(self) -> dict[str, Any]:
        """Log/telemetry-safe summary (no secrets, evidence only)."""
        return {
            "connection_id": self.connection_id,
            "provider": self.provider,
            "backend": self.backend,
            "model_id": self.model_id,
            "source": self.source.value,
            "discovery_status": self.discovery_status.value,
            "execution_support": self.execution_support.value,
            "stale": self.stale,
        }

    def to_dict(self) -> dict[str, Any]:
        """Canonical rendering (evidence only, never secret material)."""
        return {
            "connection_id": self.connection_id,
            "provider": self.provider,
            "backend": self.backend,
            "model_id": self.model_id,
            "display_name": self.display_name,
            "source": self.source.value,
            "discovery_status": self.discovery_status.value,
            "execution_support": self.execution_support.value,
            "capabilities": [
                {"name": name, "state": state.value} for name, state in self.capabilities
            ],
            "discovered_at": self.discovered_at,
            "notes": list(self.notes),
            "stale": self.stale,
        }

    @property
    def digest(self) -> str:
        """Stable content digest (never depends on wall-clock state)."""
        canonical = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _decode_discovered_at(payload: Mapping[str, Any]) -> str | None:
    raw = payload.get("discovered_at")
    if raw in (None, ""):
        return None
    return str(raw)


def _decode_stale(payload: Mapping[str, Any], *, model_id: str) -> bool:
    """Decode the durable ``stale`` flag strictly (fail closed).

    An absent key keeps the v1 schema default (``False``); a present value
    must be a real boolean so a malformed record cannot silently turn
    retained last-known evidence into fresh evidence.
    """
    if "stale" not in payload:
        return False
    value = payload["stale"]
    if not isinstance(value, bool):
        raise ValueError(
            f"discovered model {model_id!r}: stale must be a boolean, "
            f"got {type(value).__name__}"
        )
    return value


def discovered_model_from_mapping(payload: Mapping[str, Any]) -> DiscoveredModel:
    """Reconstruct a catalog entry from its non-secret durable mapping."""
    if not isinstance(payload, Mapping):
        raise ValueError("discovered model mapping must be a mapping")
    try:
        source = ModelSource(str(payload.get("source", ModelSource.DISCOVERED.value)))
    except ValueError as exc:
        raise ValueError(f"unknown model source {payload.get('source')!r}") from exc
    try:
        discovery_status = TriState(str(payload.get("discovery_status", TriState.UNKNOWN.value)))
    except ValueError as exc:
        raise ValueError(
            f"unknown discovery status {payload.get('discovery_status')!r}"
        ) from exc
    try:
        execution_support = ExecutionSupport(
            str(payload.get("execution_support", ExecutionSupport.UNKNOWN.value))
        )
    except ValueError as exc:
        raise ValueError(
            f"unknown execution support {payload.get('execution_support')!r}"
        ) from exc
    raw_capabilities = payload.get("capabilities", ())
    capabilities: list[tuple[str, TriState]] = []
    if isinstance(raw_capabilities, Mapping):
        capabilities = [
            (str(name), TriState(str(state))) for name, state in raw_capabilities.items()
        ]
    elif isinstance(raw_capabilities, list):
        for item in raw_capabilities:
            if not isinstance(item, Mapping) or "name" not in item:
                raise ValueError("invalid capability entry")
            capabilities.append(
                (str(item["name"]), TriState(str(item.get("state", TriState.UNKNOWN.value))))
            )
    else:
        raise ValueError("capabilities must be a mapping or a list")
    return DiscoveredModel(
        connection_id=str(payload.get("connection_id", "")),
        provider=str(payload.get("provider", "")),
        backend=str(payload.get("backend", "")),
        model_id=str(payload.get("model_id", "")),
        display_name=str(payload.get("display_name", "")),
        source=source,
        discovery_status=discovery_status,
        execution_support=execution_support,
        capabilities=tuple(capabilities),
        discovered_at=_decode_discovered_at(payload),
        notes=tuple(str(note) for note in payload.get("notes", ())),
        stale=_decode_stale(payload, model_id=str(payload.get("model_id", ""))),
    )


@dataclass(frozen=True)
class ModelCatalog:
    """The typed, inspectable catalog of known models across connections.

    The catalog answers lookup questions only (``for_connection`` /
    ``for_provider`` / ``get`` / ``try_get``).  It performs no network
    call, holds no secret material and imposes no ordering policy: the
    stored order is the caller's emission order, and equality/dedup is
    by :attr:`DiscoveredModel.identity`.
    """

    models: tuple[DiscoveredModel, ...] = ()
    schema_version: int = 1

    def __post_init__(self) -> None:
        seen: set[tuple[str, str, str, str]] = set()
        for model in self.models:
            if model.identity in seen:
                raise ValueError(
                    f"ModelCatalog: duplicate model identity {model.identity!r}"
                )
            seen.add(model.identity)

    def list_models(self) -> tuple[DiscoveredModel, ...]:
        """Every catalog entry in emission order."""
        return self.models

    def for_connection(self, connection_id: str) -> tuple[DiscoveredModel, ...]:
        """Entries whose connection id matches exactly."""
        wanted = connection_id.strip()
        return tuple(model for model in self.models if model.connection_id == wanted)

    def for_provider(self, provider: str) -> tuple[DiscoveredModel, ...]:
        """Entries whose provider matches (normalized), across connections."""
        wanted = provider.strip().lower()
        return tuple(model for model in self.models if model.provider == wanted)

    def get(self, identity: tuple[str, str, str, str]) -> DiscoveredModel:
        """Return the entry for ``identity`` or raise :class:`KeyError`."""
        wanted = tuple(identity)
        for model in self.models:
            if model.identity == wanted:
                return model
        raise KeyError(f"ModelCatalog: no model {wanted!r}")

    def try_get(
        self,
        *,
        connection_id: str,
        provider: str,
        backend: str,
        model_id: str,
    ) -> DiscoveredModel | None:
        """Return the entry for a context-bearing identity, or ``None``."""
        wanted = _normalize_identity(
            connection_id=connection_id,
            provider=provider,
            backend=backend,
            model_id=model_id,
        )
        for model in self.models:
            if model.identity == wanted:
                return model
        return None

    def execution_supported(self) -> tuple[DiscoveredModel, ...]:
        """Entries with positively observed execution support (evidence only)."""
        return tuple(model for model in self.models if model.is_execution_supported)

    def to_dict(self) -> dict[str, Any]:
        """Canonical rendering (evidence only, no secrets)."""
        return {
            "schema_version": self.schema_version,
            "models": [model.to_dict() for model in self.models],
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> ModelCatalog:
        """Reconstruct a catalog from its non-secret durable mapping."""
        if not isinstance(payload, Mapping):
            raise ValueError("model catalog must be a mapping")
        schema_version = payload.get("schema_version", 1)
        if schema_version != 1:
            raise ValueError("unsupported model catalog schema_version")
        raw = payload.get("models", [])
        if not isinstance(raw, list) or any(not isinstance(item, Mapping) for item in raw):
            raise ValueError("model catalog 'models' must be a list of mappings")
        try:
            models = tuple(discovered_model_from_mapping(item) for item in raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid model catalog mapping: {exc}") from exc
        return cls(models=models, schema_version=int(schema_version))


def build_model_catalog(
    models: Iterable[DiscoveredModel] = (),
) -> ModelCatalog:
    """Construct a :class:`ModelCatalog` over the supplied entries."""
    return ModelCatalog(models=tuple(models))


def model_catalog_from_mapping(payload: Mapping[str, Any]) -> ModelCatalog:
    """Reconstruct a model catalog from serialized non-secret values."""
    return ModelCatalog.from_mapping(payload)


@dataclass(frozen=True)
class ModelDiscoveryResult:
    """Bounded, classified outcome of one connection model-discovery run."""

    connection_id: str
    ok: bool
    reason: ConnectionTestReason
    discovery_status: TriState
    detail: str = ""
    models: tuple[DiscoveredModel, ...] = ()

    def __post_init__(self) -> None:
        if self.ok and self.reason is not ConnectionTestReason.OK:
            raise ValueError("a passing discovery result must carry reason OK")
        if not self.ok and self.reason is ConnectionTestReason.OK:
            raise ValueError("a failing discovery result must not carry reason OK")

    @property
    def catalog(self) -> ModelCatalog:
        """The discovered (plus manual) entries for this connection."""
        return ModelCatalog(models=self.models)

    def to_dict(self) -> dict[str, Any]:
        """Canonical rendering (non-secret detail only)."""
        return {
            "connection_id": self.connection_id,
            "ok": self.ok,
            "reason": self.reason.value,
            "discovery_status": self.discovery_status.value,
            "detail": self.detail,
            "models": [model.to_dict() for model in self.models],
        }


#: An execution-support resolver answers whether a real executable path
#: exists for one connection/backend/model.  It is injectable so the
#: execution-aware caller (which owns the adapter registry) supplies the
#: evidence; this module never imports worker/adapter code.
ExecutionSupportResolver = Callable[[ProviderConnection, str], ExecutionSupport]


def _resolve_execution_support(
    resolver: ExecutionSupportResolver | None,
    connection: ProviderConnection,
    model_id: str,
) -> ExecutionSupport:
    """Return observed execution support, defaulting to ``UNKNOWN``."""
    if resolver is None:
        return ExecutionSupport.UNKNOWN
    try:
        support = resolver(connection, model_id)
    except Exception:
        return ExecutionSupport.UNKNOWN
    if not isinstance(support, ExecutionSupport):
        return ExecutionSupport.UNKNOWN
    return support


def _manual_entry(
    connection: ProviderConnection,
    *,
    execution_support: ExecutionSupport,
    discovered_at: str | None,
) -> DiscoveredModel | None:
    """Return the owner-declared model as an explicitly MANUAL entry.

    A manual model id is never discovery-verified (``UNKNOWN``) and is
    always labelled ``MANUAL`` so it can never be mistaken for a model
    the provider actually enumerated.
    """
    model_id = connection.model.strip()
    if not model_id:
        return None
    return DiscoveredModel(
        connection_id=connection.connection_id,
        provider=connection.provider,
        backend=connection.backend,
        model_id=model_id,
        source=ModelSource.MANUAL,
        discovery_status=TriState.UNKNOWN,
        execution_support=execution_support,
        discovered_at=discovered_at,
        notes=("Owner-declared model id; not verified by provider discovery.",),
    )


def discover_connection_models(
    connection: ProviderConnection,
    *,
    list_models: Callable[[], Iterable[str]] | None = None,
    execution_support: ExecutionSupportResolver | None = None,
    discovered_at: str | None = None,
) -> ModelDiscoveryResult:
    """Discover the models a connection's provider/backend enumerates.

    ``list_models`` is the narrow, supported listing boundary supplied
    by the caller: the provider API model-list endpoint, a provider SDK
    listing, a backend CLI/account listing, or a local
    OpenAI-compatible ``/models`` endpoint.  When it is ``None`` the
    backend has no supported enumeration mechanism and discovery stays
    ``UNKNOWN`` -- results are never invented.

    ``execution_support`` is an optional caller-supplied evidence
    resolver.  It defaults to ``UNKNOWN`` for every entry.

    A discovery failure never mutates the connection: the connection is
    an input value and nothing here writes durable configuration.
    """
    manual_support = _resolve_execution_support(execution_support, connection, connection.model)

    if not connection.is_configured:
        return ModelDiscoveryResult(
            connection_id=connection.connection_id,
            ok=False,
            reason=ConnectionTestReason.CONFIG_INVALID,
            discovery_status=TriState.UNKNOWN,
            detail="connection is not configured; discovery refused",
            models=tuple(
                entry
                for entry in (
                    _manual_entry(
                        connection, execution_support=manual_support, discovered_at=discovered_at
                    ),
                )
                if entry is not None
            ),
        )

    if list_models is None:
        entries: list[DiscoveredModel] = []
        manual = _manual_entry(
            connection, execution_support=manual_support, discovered_at=discovered_at
        )
        if manual is not None:
            entries.append(manual)
        return ModelDiscoveryResult(
            connection_id=connection.connection_id,
            ok=False,
            reason=ConnectionTestReason.CAPABILITY_UNKNOWN,
            discovery_status=TriState.UNKNOWN,
            detail="backend exposes no supported model enumeration",
            models=tuple(entries),
        )

    try:
        raw_models = list(list_models())
    except ModelListingError as exc:
        # A classified listing refusal is reported truthfully (endpoint
        # unreachable, credential rejected, protocol unsupported ...)
        # and never erases the connection.
        entries = []
        manual = _manual_entry(
            connection, execution_support=manual_support, discovered_at=discovered_at
        )
        if manual is not None:
            entries.append(manual)
        return ModelDiscoveryResult(
            connection_id=connection.connection_id,
            ok=False,
            reason=exc.reason,
            discovery_status=TriState.UNKNOWN,
            detail=exc.detail or "model enumeration failed; discovery state UNKNOWN",
            models=tuple(entries),
        )
    except Exception:
        # A listing failure is classified, not raised, and never erases
        # the connection: reachability/auth state is UNKNOWN.
        entries = []
        manual = _manual_entry(
            connection, execution_support=manual_support, discovered_at=discovered_at
        )
        if manual is not None:
            entries.append(manual)
        return ModelDiscoveryResult(
            connection_id=connection.connection_id,
            ok=False,
            reason=ConnectionTestReason.ENDPOINT_UNREACHABLE,
            discovery_status=TriState.UNKNOWN,
            detail="model enumeration failed; discovery state UNKNOWN",
            models=tuple(entries),
        )

    seen: set[str] = set()
    entries = []
    for raw in raw_models:
        model_id = str(raw).strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        entries.append(
            DiscoveredModel(
                connection_id=connection.connection_id,
                provider=connection.provider,
                backend=connection.backend,
                model_id=model_id,
                source=ModelSource.DISCOVERED,
                discovery_status=TriState.TRUE,
                execution_support=_resolve_execution_support(
                    execution_support, connection, model_id
                ),
                discovered_at=discovered_at,
            )
        )
    manual = _manual_entry(
        connection, execution_support=manual_support, discovered_at=discovered_at
    )
    if manual is not None and manual.model_id not in seen:
        entries.append(manual)
    return ModelDiscoveryResult(
        connection_id=connection.connection_id,
        ok=True,
        reason=ConnectionTestReason.OK,
        discovery_status=TriState.TRUE,
        detail=f"enumerated {len(seen)} model(s)",
        models=tuple(entries),
    )


def discovery_state_for(
    result: ModelDiscoveryResult,
    *,
    installed: TriState = TriState.UNKNOWN,
    authenticated: TriState = TriState.UNKNOWN,
    reachable: TriState = TriState.UNKNOWN,
) -> DiscoveryState:
    """Project a discovery result onto the connection's tri-state evidence.

    This is a convenience for callers that already hold installed /
    authenticated / reachable evidence; ``models`` on the returned state
    are the model ids only, in emitted order.
    """
    return DiscoveryState(
        connection_id=result.connection_id,
        installed=installed,
        authenticated=authenticated,
        reachable=reachable,
        models=tuple(model.model_id for model in result.models),
    )


def replace_connection_models(
    catalog: ModelCatalog,
    *,
    connection_id: str,
    models: Iterable[DiscoveredModel],
) -> ModelCatalog:
    """Return ``catalog`` with one connection's entries replaced.

    Entries for every other connection are preserved verbatim; the
    replacement is appended in emitted order.  A failed discovery that
    produces no models for a connection therefore removes only that
    connection's stale evidence when the caller chooses to apply it --
    durable *connection configuration* is never touched by this helper.
    """
    wanted = connection_id.strip()
    kept = [model for model in catalog.models if model.connection_id != wanted]
    return ModelCatalog(models=tuple(kept) + tuple(models))


def apply_discovery_result(
    catalog: ModelCatalog,
    result: ModelDiscoveryResult,
) -> ModelCatalog:
    """Fold one discovery run into the durable catalog with honest staleness.

    Refresh semantics are deliberately asymmetric:

    * **successful enumeration** replaces that connection's catalog with
      exactly what the provider enumerated (plus any owner-declared manual
      entry).  A truthful zero-model enumeration therefore clears models the
      provider just proved absent -- stale ids are never retained.
    * **failed / ``UNKNOWN`` refresh** never destroys durable configuration
      and never pretends the old evidence is fresh: any retained last-known
      entries are marked ``stale=True`` so they cannot masquerade as a fresh
      discovery.

    Other connections' entries are always preserved verbatim.
    """
    wanted = result.connection_id.strip()
    if result.ok:
        return replace_connection_models(
            catalog, connection_id=wanted, models=result.models
        )
    existing = catalog.for_connection(wanted)
    if not existing:
        return replace_connection_models(
            catalog, connection_id=wanted, models=result.models
        )
    retained = tuple(replace(model, stale=True) for model in existing)
    return replace_connection_models(catalog, connection_id=wanted, models=retained)


class ModelCatalogStoreError(ValueError):
    """Fail-closed error for model-catalog persistence failures."""


class ModelCatalogStore:
    """Durable JSON store for the last-observed discovered-model catalog.

    The store persists non-secret evidence only (ids, statuses,
    timestamps).  It is deliberately separate from the connection store:
    a discovery refresh writes *here* and can never rewrite or erase
    durable connection configuration.
    """

    FILENAME = "access-models.json"

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        """The file this store reads and writes."""
        return self._path

    def load(self) -> ModelCatalog:
        """Load the last-observed catalog (empty when none exists)."""
        if not self._path.exists():
            return ModelCatalog()
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ModelCatalogStoreError(f"model catalog store unreadable: {exc}") from exc
        try:
            return ModelCatalog.from_mapping(payload)
        except ValueError as exc:
            raise ModelCatalogStoreError(f"model catalog store invalid: {exc}") from exc

    def save(self, catalog: ModelCatalog) -> None:
        """Persist the catalog atomically; fail closed on write errors."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(catalog.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
            )
            tmp.replace(self._path)
        except OSError as exc:
            raise ModelCatalogStoreError(f"model catalog store unwritable: {exc}") from exc
