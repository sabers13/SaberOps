"""Owner-level, non-secret persistence for exact user model bindings.

The correction this module exists for is the gap between *discovery* and
*execution*:

    a discovered model is not executable merely because an adapter is
    registered for its provider.

Execution requires an exact :class:`~saberops.control_plane.bindings.ExecutionBinding`
whose complete identity (``binding_id`` / ``provider`` / ``model`` /
``backend`` / ``profile_id`` / ``quota_pool_id``) survives a real
:func:`~saberops.control_plane.bindings.resolve_execution_binding`
call.  This module owns the owner-level durable document that holds exactly
those bindings plus the owner's Reviewer selection.

Authority boundaries preserved here:

* This is a **control-plane** document.  It is not Project truth and
  worker chains remain owned by :mod:`saberops.routing_config`; exact role
  selections and their order live here.
* Only non-secret identity is persisted.  :class:`ExecutionProfile` carries
  credential *references* (account/backend names) and never secret values;
  the schema has no field capable of holding an API key or session token.
* :meth:`BindingRegistry.to_dict` / :meth:`BindingRegistry.from_mapping` are
  reused verbatim, so a reload reconstructs the identical typed registry.
* Writes are atomic; an unreadable/invalid document fails closed rather than
  degrading to an empty registry (the caller may still choose to degrade, but
  the store never silently invents state).
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from saberops.control_plane.bindings import (
    BindingKind,
    BindingRegistry,
    BindingRole,
    ExecutionBinding,
    ExecutionProfile,
    QuotaPool,
)
from saberops.control_plane.capabilities import Capability, ProviderTransport
from saberops.models import ReasoningEffort

#: Reason code surfaced when discovery cannot determine a binding's reasoning
#: effort tier evidence.  This is *not* a permission to fabricate any
#: tier -- the binding's ``reasoning_effort_capabilities`` MUST remain
#: empty / UNKNOWN until an authoritative backend / adapter / model
#: capability source proves otherwise.
EFFORT_DISCOVERY_UNKNOWN = "EFFORT_DISCOVERY_UNKNOWN"

#: A store written before the planner owned selection still carries
#: ``orch_binding_policy`` (ADR-0064); the engine ignores it and tells the
#: owner once per process.
RETIRED_SELECTION_NOTICE = (
    "SaberOps: the saved orchestrator model binding is no longer used and was "
    "ignored (ADR-0064). The agent you plan from is the planner; the next "
    "settings write drops the setting."
)

#: Pre-ADR-0086 stores kept the reviewer choice in ``reviewer_binding_id``
#: beside the ordered chain.  The order is the one selection; a legacy primary
#: is migrated into it on load and the owner is told once per process.
LEGACY_REVIEWER_MIGRATION_NOTICE = (
    "SaberOps: the saved reviewer selection was moved into the reviewer order "
    "(ADR-0086); the next settings write drops the old field."
)

_notified_retired_selection: set[Path] = set()
_notified_legacy_reviewer: set[Path] = set()

__all__ = [
    "BINDING_STORE_SCHEMA_VERSION",
    "EFFORT_DISCOVERY_UNKNOWN",
    "LEGACY_REVIEWER_MIGRATION_NOTICE",
    "ORCH_BINDING_STORE_ENV",
    "RETIRED_SELECTION_NOTICE",
    "BindingStoreError",
    "OwnerBindings",
    "OwnerBindingStore",
    "get_binding_store_path",
    "load_owner_bindings",
    "materialize_discovered_binding",
    "save_owner_bindings",
    "upsert_owner_binding",
]

BINDING_STORE_SCHEMA_VERSION = 1

#: Test / diagnostics override for the owner binding document path.
ORCH_BINDING_STORE_ENV = "ORCH_BINDING_STORE"

_BINDING_STORE_FILENAME = "bindings.json"


class BindingStoreError(ValueError):
    """Fail-closed error for owner binding-store persistence failures."""


def get_binding_store_path() -> Path:
    """Resolve the canonical owner binding document path.

    ``ORCH_BINDING_STORE`` pins an explicit file (tests / diagnostics).
    Otherwise the store lives beside the other binding owner configuration
    (``$XDG_CONFIG_HOME/saberops/bindings.json``).
    """
    override = os.environ.get(ORCH_BINDING_STORE_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    from saberops.paths import get_binding_store_path_no_override as _canonical

    return _canonical()


def _legacy_reviewer_primary(payload: Mapping[str, Any]) -> str | None:
    """Return the pre-ADR-0086 ``reviewer_binding_id`` primary, validated.

    A non-string, non-null value is a corrupt owner document and fails closed;
    an empty or whitespace-only string means "no selection".
    """
    raw = payload.get("reviewer_binding_id")
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise BindingStoreError("owner reviewer binding id must be a string or null")
    return raw.strip() or None


@dataclass(frozen=True)
class OwnerBindings:
    """The canonical owner-level binding document.

    ``registry`` is the exact binding/profile/pool identity set;
    ``reviewer_binding_ids`` is the owner's ordered Reviewer selection.
    ``None`` means the owner never selected one (the legacy review ladder
    applies); ``()`` is an explicit clear.  Neither carries secret material.
    """

    registry: BindingRegistry = field(default_factory=BindingRegistry)
    # None preserves the legacy unconfigured reviewer; () is explicitly empty.
    reviewer_binding_ids: tuple[str, ...] | None = None

    def role_binding_ids(self, role: str) -> tuple[str, ...]:
        if role != "reviewer":
            raise ValueError("unknown routing role")
        return self.reviewer_binding_ids or ()

    def with_role_order(self, role: str, order: Iterable[str]) -> OwnerBindings:
        ids = tuple(order)
        if any(not isinstance(bid, str) or not bid.strip() for bid in ids) or len(set(ids)) != len(
            ids
        ):
            raise BindingStoreError("role order must contain unique nonempty binding ids")
        if role != "reviewer":
            raise BindingStoreError("unknown routing role")
        return replace(self, reviewer_binding_ids=ids)

    def to_dict(self) -> dict[str, Any]:
        """Canonical non-secret rendering (sorted by the registry renderer)."""
        return {
            "schema_version": BINDING_STORE_SCHEMA_VERSION,
            "binding_registry": self.registry.to_dict(),
            "reviewer_binding_ids": None
            if self.reviewer_binding_ids is None
            else list(self.reviewer_binding_ids),
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> OwnerBindings:
        """Reconstruct the owner document from its durable mapping.

        A pre-ADR-0086 document keeps its selection in ``reviewer_binding_id``;
        absent an ordered chain, that primary is the one-element order.  When
        both keys are present they must agree -- an ambiguous document fails
        closed instead of letting one field silently override the other.
        """
        if not isinstance(payload, Mapping):
            raise BindingStoreError("owner bindings must be a mapping")
        schema_version = payload.get("schema_version", BINDING_STORE_SCHEMA_VERSION)
        if schema_version != BINDING_STORE_SCHEMA_VERSION:
            raise BindingStoreError("unsupported owner bindings schema_version")
        registry_raw = payload.get("binding_registry", {})
        if registry_raw is None:
            registry_raw = {}
        if not isinstance(registry_raw, Mapping):
            raise BindingStoreError("owner bindings registry must be a mapping")
        try:
            registry = BindingRegistry.from_mapping(registry_raw)
        except Exception as exc:  # noqa: BLE001 - normalize to one typed refusal
            raise BindingStoreError(f"owner bindings registry invalid: {exc}") from exc
        legacy_primary = _legacy_reviewer_primary(payload)
        ids = payload.get("reviewer_binding_ids")
        if ids is not None and (
            not isinstance(ids, list)
            or any(not isinstance(bid, str) or not bid.strip() for bid in ids)
            or len(set(ids)) != len(ids)
        ):
            raise BindingStoreError("invalid reviewer routing order")
        if ids is None and legacy_primary is not None:
            ids = [legacy_primary]
        if (
            ids is not None
            and legacy_primary is not None
            and legacy_primary != (ids[0] if ids else None)
        ):
            raise BindingStoreError("reviewer primary does not match routing order")
        return cls(
            registry=registry,
            reviewer_binding_ids=None if ids is None else tuple(ids),
        )


def _upsert_by_id(
    items: Iterable[Any], new_item: Any, key: str
) -> tuple[Any, ...]:
    """Return ``items`` with ``new_item`` replacing/adding by its id.

    Replacement is positional so declaration order is preserved; a brand new
    id is appended.  Duplicate ids can only arise from malformed input and
    are caught by :class:`BindingRegistry` construction.
    """
    wanted = getattr(new_item, key)
    out: list[Any] = []
    replaced = False
    for item in items:
        if getattr(item, key) == wanted:
            if replaced:
                continue
            out.append(new_item)
            replaced = True
        else:
            out.append(item)
    if not replaced:
        out.append(new_item)
    return tuple(out)


def upsert_owner_binding(
    owner: OwnerBindings,
    *,
    binding: ExecutionBinding,
    profile: ExecutionProfile,
    pool: QuotaPool,
) -> OwnerBindings:
    """Return ``owner`` with one exact binding/profile/pool upserted.

    The result is a new immutable document.  The caller is responsible for
    verifying the new binding through
    :func:`~saberops.control_plane.bindings.resolve_execution_binding`
    before persisting; this helper performs identity bookkeeping only.
    """
    registry = BindingRegistry(
        bindings=_upsert_by_id(owner.registry.bindings, binding, "binding_id"),
        profiles=_upsert_by_id(owner.registry.profiles, profile, "profile_id"),
        pools=_upsert_by_id(owner.registry.pools, pool, "pool_id"),
    )
    return replace(owner, registry=registry)


def materialize_discovered_binding(
    *,
    connection_id: str,
    provider: str,
    model: str,
    provider_transport: ProviderTransport = ProviderTransport.DIRECT_CLI,
    harness_capabilities: frozenset[Capability] = frozenset(),
    backend: BindingKind = BindingKind.DIRECT_CLI,
    binding_role: BindingRole = BindingRole.BUILD,
    reasoning_effort_capabilities: frozenset[ReasoningEffort] = frozenset(),
) -> tuple[ExecutionBinding, ExecutionProfile, QuotaPool]:
    """Build the exact binding identity for one discovered account model.

    This is a *request* to register an identity, not execution authority:
    the caller must still prove the result through the real binding resolver
    with the real adapter before it may be marked executable.

    The profile and pool carry no credential value and no quota policy --
    only provider-neutral identity.  Quota scarcity stays UNKNOWN to quota policy
    until a real ``QuotaPoolPolicy`` exists for the pool id.

    ``binding_role`` defaults to :class:`BindingRole.BUILD`: discovery is
    a worker-routing pathway.  Callers needing a role-compatible binding
    for another slot request it explicitly (reusing one role's binding as
    proof of another role's execution would be a role violation).

    The identity scope is ``role``-qualified only at the binding layer so
    the same ``(connection, provider, model)`` triple may register
    distinct exact bindings for distinct semantic roles when it is
    genuinely needed (a discovered model that may legitimately serve
    both worker and reviewer roles).

    Identity follows the real resource:

    * ``profile_id`` is namespaced by **connection** (the account).  A
      BUILD-role and a CHECK-role binding on the same connection share the
      profile_id -- semantic role does not split the real account.
    * ``quota_pool_id`` is namespaced by **connection** (the
      user-owned account / backend reference), not by provider.
      Provider identity is not quota-resource identity: two distinct
      account connections to the same provider must NOT silently
      share a default quota / billing / scarcity resource.  Semantic
      role does not split the real scarcity resource; the same
      connection / provider pair shares one quota pool across roles.
    * ``binding_id`` carries the role so different role-qualified
      bindings on the same ``(connection, model)`` remain distinct.

    Effort truthfulness: ``reasoning_effort_capabilities`` is
    NEVER fabricated by this materialization seam.  ``LOW`` /
    ``MEDIUM`` / ``HIGH`` / ``ULTRA`` are not added merely because
    discovery succeeded or because an adapter is registered.  The
    materialization seam accepts explicit reasoning-effort evidence
    only when an authoritative backend / adapter / model-capability
    source has already proven it (passed as
    ``reasoning_effort_capabilities``); otherwise the field stays
    empty and the binding's :attr:`has_controllable_effort` returns
    ``False``.  ULTRA in particular is never invented.  When the
    runtime needs an effort tier before dispatch and no authoritative
    evidence exists, it must fail closed with
    :data:`EFFORT_DISCOVERY_UNKNOWN` rather than silently fabricating
    support.
    """
    cleaned_connection = connection_id.strip()
    cleaned_provider = provider.strip().lower()
    cleaned_model = model.strip()
    if not cleaned_connection or not cleaned_provider or not cleaned_model:
        raise BindingStoreError(
            "a discovered binding requires connection, provider and model"
        )
    # Binding ids are durable identity: the slug keeps its pre-rename spelling.
    role_slug = binding_role.id_slug
    profile = ExecutionProfile(
        profile_id=f"discovered-profile:{cleaned_connection}",
        provider=cleaned_provider,
    )
    pool = QuotaPool(
        pool_id=f"discovered-pool:{cleaned_connection}",
        provider=cleaned_provider,
    )
    binding = ExecutionBinding(
        binding_id=(
            f"discovered-binding:{role_slug}:{cleaned_connection}:{cleaned_model}"
        ),
        provider=cleaned_provider,
        model=cleaned_model,
        backend=backend,
        profile_id=profile.profile_id,
        quota_pool_id=pool.pool_id,
        transport=provider_transport,
        binding_role=binding_role,
        harness_capabilities=frozenset(harness_capabilities),
        reasoning_effort_capabilities=frozenset(reasoning_effort_capabilities),
    )
    return binding, profile, pool


class OwnerBindingStore:
    """Durable JSON store for the canonical owner binding document."""

    FILENAME = _BINDING_STORE_FILENAME

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        """The file this store reads and writes."""
        return self._path

    def load(self) -> OwnerBindings:
        """Load the owner document (empty when none exists).

        An unreadable or invalid document raises :class:`BindingStoreError`
        rather than silently degrading: a corrupt binding registry must
        never be mistaken for "no bindings configured".
        """
        if not self._path.exists():
            return OwnerBindings()
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BindingStoreError(f"owner bindings store unreadable: {exc}") from exc
        if isinstance(payload, Mapping):
            self._notice_retired_selection(payload)
            self._notice_legacy_reviewer_selection(payload)
        return OwnerBindings.from_mapping(payload)

    def _notice_retired_selection(self, payload: object) -> None:
        """Tell the owner once that a pre-1.1 Orchestrator selection is ignored."""
        if not isinstance(payload, Mapping) or not payload.get("orch_binding_policy"):
            return
        key = Path(os.path.abspath(self._path))
        if key in _notified_retired_selection:
            return
        _notified_retired_selection.add(key)
        print(RETIRED_SELECTION_NOTICE, file=sys.stderr)

    def _notice_legacy_reviewer_selection(self, payload: Mapping[str, Any]) -> None:
        """Tell the owner once that the old reviewer field joined the order list."""
        if payload.get("reviewer_binding_ids") is not None:
            return
        if _legacy_reviewer_primary(payload) is None:
            return
        key = Path(os.path.abspath(self._path))
        if key in _notified_legacy_reviewer:
            return
        _notified_legacy_reviewer.add(key)
        print(LEGACY_REVIEWER_MIGRATION_NOTICE, file=sys.stderr)

    def save(self, owner: OwnerBindings) -> None:
        """Persist the owner document atomically; fail closed on write errors."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(owner.to_dict(), indent=2, sort_keys=True),
                encoding="utf-8",
            )
            tmp.replace(self._path)
        except OSError as exc:
            raise BindingStoreError(f"owner bindings store unwritable: {exc}") from exc


def load_owner_bindings(path: Path | str | None = None) -> OwnerBindings:
    """Load the canonical owner bindings document from its default path."""
    store = OwnerBindingStore(path if path is not None else get_binding_store_path())
    return store.load()


def save_owner_bindings(
    owner: OwnerBindings, path: Path | str | None = None
) -> Path:
    """Persist the canonical owner bindings document; returns its path."""
    store = OwnerBindingStore(path if path is not None else get_binding_store_path())
    store.save(owner)
    return store.path
