"""C11-B: owner-level, non-secret persistence for exact user model bindings.

The correction this module exists for is the gap between *discovery* and
*execution*:

    a discovered model is not executable merely because an adapter is
    registered for its provider.

Execution requires an exact :class:`~saberops.control_plane.bindings.ExecutionBinding`
whose complete identity (``binding_id`` / ``provider`` / ``model`` /
``backend`` / ``profile_id`` / ``quota_pool_id``) survives a real
:func:`~saberops.control_plane.bindings.resolve_execution_binding`
call.  This module owns the owner-level durable document that holds exactly
those bindings plus the owner's :class:`OrchBindingPolicy`.

Authority boundaries preserved here:

* This is a **control-plane** (C11-B) document.  It is not Project truth and
  it is not a second routing configuration: routing chains remain owned by
  :mod:`saberops.routing_config`.
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
from saberops.control_plane.orch_binding import OrchBindingPolicy
from saberops.models import ReasoningEffort

#: Reason code surfaced when discovery cannot determine a binding's reasoning
#: effort tier evidence.  This is *not* a permission to fabricate any
#: tier -- the binding's ``reasoning_effort_capabilities`` MUST remain
#: empty / UNKNOWN until an authoritative backend / adapter / model
#: capability source proves otherwise.
EFFORT_DISCOVERY_UNKNOWN = "EFFORT_DISCOVERY_UNKNOWN"

__all__ = [
    "BINDING_STORE_SCHEMA_VERSION",
    "EFFORT_DISCOVERY_UNKNOWN",
    "ORCH_BINDING_STORE_ENV",
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

_BINDING_STORE_DIR = "saberops"
_BINDING_STORE_FILENAME = "bindings.json"


class BindingStoreError(ValueError):
    """Fail-closed error for owner binding-store persistence failures."""


def get_binding_store_path() -> Path:
    """Resolve the canonical owner binding document path.

    ``ORCH_BINDING_STORE`` pins an explicit file (tests / diagnostics).
    Otherwise the store lives beside the other C11-B owner configuration
    (``$XDG_CONFIG_HOME/saberops/bindings.json``).
    """
    override = os.environ.get(ORCH_BINDING_STORE_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    from saberops.paths import get_binding_store_path_no_override as _canonical

    return _canonical()


@dataclass(frozen=True)
class OwnerBindings:
    """The canonical owner-level C11-B document.

    ``registry`` is the exact binding/profile/pool identity set; ``policy``
    is the owner's Orchestrator binding choice expressed over those exact
    binding ids.  Neither carries secret material or routing order.
    """

    registry: BindingRegistry = field(default_factory=BindingRegistry)
    policy: OrchBindingPolicy | None = None

    def to_dict(self) -> dict[str, Any]:
        """Canonical non-secret rendering (sorted by the registry renderer)."""
        return {
            "schema_version": BINDING_STORE_SCHEMA_VERSION,
            "binding_registry": self.registry.to_dict(),
            "orch_binding_policy": (
                None if self.policy is None else self.policy.to_dict()
            ),
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> OwnerBindings:
        """Reconstruct the owner document from its durable mapping."""
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
        policy_raw = payload.get("orch_binding_policy")
        if policy_raw is None:
            policy = None
        elif isinstance(policy_raw, Mapping):
            try:
                policy = OrchBindingPolicy.from_mapping(policy_raw)
            except ValueError as exc:
                raise BindingStoreError(
                    f"owner orch binding policy invalid: {exc}"
                ) from exc
        else:
            raise BindingStoreError("owner orch binding policy must be a mapping or null")
        return cls(registry=registry, policy=policy)


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
    binding_role: BindingRole = BindingRole.WORKER,
    reasoning_effort_capabilities: frozenset[ReasoningEffort] = frozenset(),
) -> tuple[ExecutionBinding, ExecutionProfile, QuotaPool]:
    """Build the exact C11-B identity for one discovered account model.

    This is a *request* to register an identity, not execution authority:
    the caller must still prove the result through the real C11-B resolver
    with the real adapter before it may be marked executable.

    The profile and pool carry no credential value and no quota policy --
    only provider-neutral identity.  Quota scarcity stays UNKNOWN to C07
    until a real ``QuotaPoolPolicy`` exists for the pool id.

    ``binding_role`` defaults to :class:`BindingRole.WORKER`: discovery is
    a worker-routing pathway.  Callers needing a role-compatible binding
    for the ORCHESTRATOR slot must request it explicitly (C15-XB-03:
    reusing an ORCHESTRATOR binding as proof of worker execution would
    be a role violation; reusing a WORKER binding as Orchestrator
    selection would likewise be a role violation).

    The identity scope is ``role``-qualified only at the binding layer so
    the same ``(connection, provider, model)`` triple may register
    distinct exact bindings for distinct semantic roles when it is
    genuinely needed (a discovered model that may legitimately serve
    both worker and orchestrator roles).

    Identity follows the real resource:

    * ``profile_id`` is namespaced by **connection** (the account).  A
      WORKER-role and an ORCHESTRATOR-role binding on the same
      connection share the profile_id -- semantic role does not split
      the real account.
    * ``quota_pool_id`` is namespaced by **connection** (the
      user-owned account / backend reference), not by provider.
      Provider identity is not quota-resource identity: two distinct
      account connections to the same provider must NOT silently
      share a default quota / billing / scarcity resource.  Semantic
      role does not split the real scarcity resource; the same
      connection / provider pair shares one quota pool across roles.
    * ``binding_id`` carries the role so different role-qualified
      bindings on the same ``(connection, model)`` remain distinct.

    C15-XC-01 effort truthfulness: ``reasoning_effort_capabilities`` is
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
    role_slug = binding_role.value.lower()
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
        return OwnerBindings.from_mapping(payload)

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
