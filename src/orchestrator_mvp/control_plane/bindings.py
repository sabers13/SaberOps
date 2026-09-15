"""C11-B: sparse provider-neutral execution bindings.

A binding is the typed, capability-bearing description of one executable
``(provider, model)`` pair together with the profile that runs it, the
quota pool it consumes, and the harness / semantic / effort capabilities
that have actually been observed for it.  Bindings are deliberately
*sparse*: only the fields needed to decide what may dispatch, through
which backend, against which account, against which scarcity, with what
verified controls.

Three invariants this module is responsible for upholding:

* Profile and QuotaPool are distinct.  A profile is a credential /
  backend reference; a quota pool is a provider-neutral scarcity /
  billing identity carried only by name.  Many profiles may share one
  pool; one profile does not automatically imply one independent pool.
  No real credential value is ever persisted in either; only
  references and names.
* Bindings never silently depend on a specific provider as a structural
  requirement.  The fields below are typed in provider-neutral terms
  (``Capability``, ``DispatchRole``, ``ReasoningEffort``,
  ``BindingKind``); no provider-specific magic string is allowed to be a
  load-bearing requirement to reconstruct a binding.
* Verification status is observable.  :func:`verification_state` answers
  "is this binding still trustworthy for the Orchestrator role" given
  an explicit reference time and maximum age, without making any
  non-binding the authority.

The binding layer stores quota pool *identities*, never the hard quota
policy.  Hard policy -- ``STRICT`` / ``RESERVE`` / ``DISABLED``, the
red-line, the shared-pool membership, the canonical QuotaPoolPolicy --
is owned exclusively by :mod:`orchestrator_mvp.control_plane.quota_policy`
and consulted by :func:`orchestrator_mvp.control_plane.preflight.evaluate_control_plane`.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from orchestrator_mvp.control_plane.capabilities import (
    Capability,
    CapabilityProfile,
    ProviderTransport,
)
from orchestrator_mvp.models import (
    BindingRole,
    DispatchRole,
    ReasoningEffort,
    RiskLevel,
)


class BindingKind(StrEnum):
    """Execution backend a binding runs through.

    Distinct from :class:`ProviderTransport` because a binding's backend
    is a runtime decision (which tool actually executes the model) while
    transport is a static description of how the prompt bytes are
    delivered to the adapter.
    """

    DIRECT_CLI = "DIRECT_CLI"
    DIRECT_API = "DIRECT_API"
    GATEWAY = "GATEWAY"


@dataclass(frozen=True)
class ExecutionProfile:
    """A non-secret account / backend reference for one or more bindings.

    Profiles hold only *references* to credentials (an account name,
    an env-var name, a vault path).  Real secret values never enter
    this record; the binding registry and the rest of the runtime must
    rely on existing secret-resolution layers instead.

    ``enabled`` is the configuration-level gate.  When ``False`` the
    profile and every binding that references it are ineligible for
    Orchestrator selection -- independent of any governance / training
    / quota decision C07 may still take.
    """

    profile_id: str
    provider: str
    account_ref: str = ""
    backend_ref: str = ""
    enabled: bool = True
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.profile_id.strip():
            raise ValueError("ExecutionProfile.profile_id must not be empty")
        object.__setattr__(self, "provider", self.provider.strip().lower())
        if not self.provider:
            raise ValueError(
                f"ExecutionProfile '{self.profile_id}': provider must not be empty"
            )

    def to_dict(self) -> dict[str, Any]:
        """Canonical rendering (sorted keys, no secrets)."""
        return {
            "profile_id": self.profile_id,
            "provider": self.provider,
            "account_ref": self.account_ref,
            "backend_ref": self.backend_ref,
            "enabled": self.enabled,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class QuotaPool:
    """A provider-neutral scarcity / billing pool identity.

    The binding layer stores quota pools *by reference only*.  Hard
    quota policy -- mode (``STRICT`` / ``RESERVE`` / ``DISABLED``),
    red-line percentage, shared-pool membership, freshness rules --
    is owned exclusively by
    :class:`orchestrator_mvp.control_plane.quota_policy.QuotaPoolPolicy`
    inside :class:`orchestrator_mvp.control_plane.policy.ControlPlanePolicy`
    and is consulted by the single canonical C07 evaluator
    (:func:`orchestrator_mvp.control_plane.preflight.evaluate_control_plane`).

    A binding's ``quota_pool_id`` must match a key in the
    ``ControlPlanePolicy.quota_pools`` mapping for any quota decision to
    apply.  When the binding references an unknown pool, the C07
    evaluation remains UNKNOWN/non-fabricated; the binding does not
    silently fall back to a provider-wide default pool.

    Pool membership ("which profiles share this scarcity") is
    exclusively a C07 quota policy concept and is never stored here.
    """

    pool_id: str
    provider: str
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.pool_id.strip():
            raise ValueError("QuotaPool.pool_id must not be empty")
        object.__setattr__(self, "provider", self.provider.strip().lower())
        if not self.provider:
            raise ValueError(
                f"QuotaPool '{self.pool_id}': provider must not be empty"
            )

    def to_dict(self) -> dict[str, Any]:
        """Canonical rendering -- identity only, no policy."""
        return {
            "pool_id": self.pool_id,
            "provider": self.provider,
            "notes": list(self.notes),
        }


class BindingVerificationState(StrEnum):
    """Whether a binding's declared capabilities are still trustworthy.

    Determined deterministically against an explicit
    ``as_of`` reference time and a ``max_age`` policy:

    * ``UNKNOWN`` -- the binding has never been verified
      (``verified_at is None``) or its timestamp cannot be parsed.
    * ``VERIFIED`` -- ``verified_at`` is within ``max_age`` of ``as_of``.
    * ``STALE`` -- ``verified_at`` is older than ``max_age`` from
      ``as_of``.
    """

    VERIFIED = "VERIFIED"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"


def _parse_verified_at(value: str | None) -> datetime | None:
    """Parse a ``verified_at`` ISO-8601 timestamp into a UTC ``datetime``.

    Returns ``None`` when the value is missing or malformed.  A
    malformed timestamp must never be silently treated as ``VERIFIED``;
    the worst the runtime can do is down-grade to ``UNKNOWN``.
    """
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


@dataclass(frozen=True)
class ExecutionBinding:
    """A provider-neutral sparse execution binding.

    A binding binds together only the information required to:

    * identify the semantic model being requested (``provider``,
      ``model``);
    * identify the runtime backend (``backend`` and ``profile_id``);
    * identify the scarcity pool it consumes (``quota_pool_id``); the
      hard policy for that pool lives in
      :class:`orchestrator_mvp.control_plane.policy.ControlPlanePolicy.quota_pools`;
    * enumerate the harness capabilities it actually carries
      (``harness_capabilities``);
    * enumerate the semantic capabilities it actually carries
      (``semantic_capabilities``);
    * enumerate the roles it is qualified to serve
      (``supported_roles`` and :attr:`binding_role`);
    * enumerate the reasoning effort controls it actually supports
      (``reasoning_effort_capabilities``);
    * declare the risk levels at which it may be authorised
      (``risk_levels``);
    * mark when its declaration was last verified (``verified_at``).
    """

    binding_id: str
    provider: str
    model: str
    backend: BindingKind
    profile_id: str
    quota_pool_id: str
    transport: ProviderTransport = ProviderTransport.DIRECT_CLI
    binding_role: BindingRole = BindingRole.WORKER
    harness_capabilities: frozenset[Capability] = frozenset()
    semantic_capabilities: tuple[str, ...] = ()
    supported_roles: frozenset[DispatchRole] = frozenset()
    reasoning_effort_capabilities: frozenset[ReasoningEffort] = frozenset()
    risk_levels: frozenset[RiskLevel] = frozenset()
    verified_at: str | None = None
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.binding_id.strip():
            raise ValueError("ExecutionBinding.binding_id must not be empty")
        object.__setattr__(self, "provider", self.provider.strip().lower())
        if not self.provider:
            raise ValueError(
                f"ExecutionBinding '{self.binding_id}': provider must not be empty"
            )
        if not self.model.strip():
            raise ValueError(
                f"ExecutionBinding '{self.binding_id}': model must not be empty"
            )
        if not self.profile_id.strip():
            raise ValueError(
                f"ExecutionBinding '{self.binding_id}': profile_id must not be empty"
            )
        if not self.quota_pool_id.strip():
            raise ValueError(
                f"ExecutionBinding '{self.binding_id}': quota_pool_id must not be empty"
            )

    @property
    def has_controllable_effort(self) -> bool:
        """True iff the binding actually supports at least one effort tier.

        Bindings without any controllable effort must never be silently
        treated as supporting a tier; orchestrator effort resolution
        uses this signal to surface typed unsupported-effort decisions.
        """
        return bool(self.reasoning_effort_capabilities)

    @property
    def is_orchestrator_capable(self) -> bool:
        """True iff the binding is qualified to fill the Orchestrator role."""
        return self.binding_role is BindingRole.ORCHESTRATOR

    def verification_state(
        self,
        *,
        as_of: datetime,
        max_age: timedelta,
    ) -> BindingVerificationState:
        """Deterministically evaluate verification state.

        The result is a pure function of:

        * ``self.verified_at`` -- when the binding was last verified;
        * ``as_of`` -- the explicit reference time the caller passes in;
        * ``max_age`` -- the policy-supplied maximum allowed age.

        A missing or malformed ``verified_at`` returns ``UNKNOWN``;
        never silently ``VERIFIED``.  A ``verified_at`` whose age
        exceeds ``max_age`` returns ``STALE``.  Otherwise ``VERIFIED``.

        The result must never depend on ambient wall-clock time.
        Callers are responsible for choosing ``as_of`` and ``max_age``.
        """
        parsed = _parse_verified_at(self.verified_at)
        if parsed is None:
            return BindingVerificationState.UNKNOWN
        reference = as_of if as_of.tzinfo is not None else as_of.replace(tzinfo=UTC)
        age = reference - parsed
        if age > max_age:
            return BindingVerificationState.STALE
        if age < timedelta(0):
            # Verified-at in the future is treated as UNKNOWN -- the
            # observation cannot be trusted until the clock catches up.
            return BindingVerificationState.UNKNOWN
        return BindingVerificationState.VERIFIED

    def supports_effort(self, effort: ReasoningEffort) -> bool:
        """True iff this binding declares ``effort`` as a controllable tier."""
        return effort in self.reasoning_effort_capabilities

    def supports_role(self, role: DispatchRole) -> bool:
        """True iff this binding declares it may be ``role``."""
        return role in self.supported_roles

    def supports_risk(self, risk: RiskLevel) -> bool:
        """True iff this binding declares ``risk`` is permitted."""
        return risk in self.risk_levels

    def capability_profile_descriptor(self) -> dict[str, Any]:
        """Return a binding-derived capability descriptor.

        This descriptor is consumed by
        :func:`orchestrator_mvp.control_plane.capabilities.capability_profile_for`
        through the adapter's ``capability_profile_descriptor`` method,
        which lets the existing C07 evaluator receive the binding's
        factual capabilities without constructing a second capability
        evaluator.

        Unknown / invalid binding capability declarations must never
        gain stronger trust: the descriptor reflects exactly what the
        binding declared, no more.
        """
        return {
            "provider": self.provider,
            "transport": self.transport.value,
            "capabilities": sorted(c.value for c in self.harness_capabilities),
        }

    def capability_profile(self) -> CapabilityProfile:
        """Return a C07 :class:`CapabilityProfile` derived from this binding."""
        return CapabilityProfile(
            provider=self.provider,
            transport=self.transport,
            capabilities=self.harness_capabilities,
        )

    def to_dict(self) -> dict[str, Any]:
        """Canonical rendering with sorted keys; never includes secrets.

        The verification state is **not** rendered here: it is a
        derived value computed against an explicit reference time and
        is therefore not a stable part of the binding's own payload.
        See :meth:`verification_state`.
        """
        return {
            "binding_id": self.binding_id,
            "provider": self.provider,
            "model": self.model,
            "backend": self.backend.value,
            "binding_role": self.binding_role.value,
            "profile_id": self.profile_id,
            "quota_pool_id": self.quota_pool_id,
            "transport": self.transport.value,
            "harness_capabilities": sorted(c.value for c in self.harness_capabilities),
            "semantic_capabilities": list(self.semantic_capabilities),
            "supported_roles": sorted(role.value for role in self.supported_roles),
            "reasoning_effort_capabilities": sorted(
                effort.value for effort in self.reasoning_effort_capabilities
            ),
            "risk_levels": sorted(risk.value for risk in self.risk_levels),
            "verified_at": self.verified_at,
            "notes": list(self.notes),
        }

    @property
    def digest(self) -> str:
        """Stable content digest for caching / equality across reloads.

        Digest inputs are the binding's own canonical payload.  The
        digest never depends on wall-clock state and never changes
        merely because time has passed.
        """
        canonical = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _indexed(items: Iterable[Any], *, key: str) -> set[str]:
    out: set[str] = set()
    for item in items:
        out.add(getattr(item, key))
    return out


@dataclass(frozen=True)
class BindingRegistry:
    """The typed, inspectable registry of bindings / profiles / pools.

    The registry answers:

    * ``list_bindings()`` -- every configured binding.
    * ``get(binding_id)`` -- one binding by id.
    * ``for_role(BindingRole)`` -- bindings qualified for one slot.
    * ``for_risk(risk)`` -- bindings allowed at one risk level.
    * ``for_effort(effort)`` -- bindings that actually support a tier.
    * ``profile_of(binding)`` and ``pool_of(binding)`` -- without forcing
      every binding to inline either.
    * ``verification_summary(as_of, max_age)`` -- counts by verification
      state.

    No LLM, provider discovery call, or live network call participates:
      the registry is purely deterministic over the configuration that
      was supplied to it.
    """

    bindings: tuple[ExecutionBinding, ...] = ()
    profiles: tuple[ExecutionProfile, ...] = ()
    pools: tuple[QuotaPool, ...] = ()
    schema_version: int = 1

    def __post_init__(self) -> None:
        binding_ids = [binding.binding_id for binding in self.bindings]
        if len(set(binding_ids)) != len(binding_ids):
            raise ValueError("BindingRegistry: duplicate binding_id")
        profile_ids = [profile.profile_id for profile in self.profiles]
        if len(set(profile_ids)) != len(profile_ids):
            raise ValueError("BindingRegistry: duplicate profile_id")
        pool_ids = [pool.pool_id for pool in self.pools]
        if len(set(pool_ids)) != len(pool_ids):
            raise ValueError("BindingRegistry: duplicate pool_id")
        profile_id_set = set(profile_ids)
        pool_id_set = set(pool_ids)
        for binding in self.bindings:
            if binding.profile_id not in profile_id_set:
                raise ValueError(
                    f"BindingRegistry: binding '{binding.binding_id}' references "
                    f"unknown profile '{binding.profile_id}'"
                )
            if binding.quota_pool_id not in pool_id_set:
                raise ValueError(
                    f"BindingRegistry: binding '{binding.binding_id}' references "
                    f"unknown quota pool '{binding.quota_pool_id}'"
                )

    def list_bindings(self) -> tuple[ExecutionBinding, ...]:
        """Every configured binding in declaration order."""
        return self.bindings

    def get(self, binding_id: str) -> ExecutionBinding:
        """Return the binding with this id, or raise :class:`KeyError`."""
        for binding in self.bindings:
            if binding.binding_id == binding_id:
                return binding
        raise KeyError(f"BindingRegistry: no binding '{binding_id}'")

    def try_get(self, binding_id: str) -> ExecutionBinding | None:
        """Return the binding with this id, or ``None`` if absent."""
        for binding in self.bindings:
            if binding.binding_id == binding_id:
                return binding
        return None

    def for_role(self, role: BindingRole) -> tuple[ExecutionBinding, ...]:
        """Bindings qualified to serve ``role``."""
        return tuple(
            binding for binding in self.bindings if binding.binding_role is role
        )

    def for_risk(self, risk: RiskLevel) -> tuple[ExecutionBinding, ...]:
        """Bindings declared as permitted at ``risk``."""
        return tuple(binding for binding in self.bindings if binding.supports_risk(risk))

    def for_effort(self, effort: ReasoningEffort) -> tuple[ExecutionBinding, ...]:
        """Bindings that declare they actually support ``effort``."""
        return tuple(binding for binding in self.bindings if binding.supports_effort(effort))

    def profile_of(self, binding: ExecutionBinding) -> ExecutionProfile:
        """Return the profile referenced by ``binding``."""
        for profile in self.profiles:
            if profile.profile_id == binding.profile_id:
                return profile
        raise KeyError(
            f"BindingRegistry: binding '{binding.binding_id}' references "
            f"unknown profile '{binding.profile_id}'"
        )

    def pool_of(self, binding: ExecutionBinding) -> QuotaPool:
        """Return the identity-only quota pool referenced by ``binding``.

        The result carries no policy.  Hard quota policy for ``pool_id``
        is read from ``ControlPlanePolicy.quota_pools`` by the C07
        evaluator; the registry stores nothing that could conflict.
        """
        for pool in self.pools:
            if pool.pool_id == binding.quota_pool_id:
                return pool
        raise KeyError(
            f"BindingRegistry: binding '{binding.binding_id}' references "
            f"unknown quota pool '{binding.quota_pool_id}'"
        )

    def verification_summary(
        self,
        *,
        as_of: datetime,
        max_age: timedelta,
    ) -> dict[str, int]:
        """Count bindings by :class:`BindingVerificationState`.

        The summary is computed against an explicit reference time and
        maximum age.  ``STALE`` is reachable: a binding whose
        ``verified_at`` is older than ``max_age`` from ``as_of`` is
        counted under ``STALE``.
        """
        counts: dict[str, int] = {
            BindingVerificationState.VERIFIED.value: 0,
            BindingVerificationState.STALE.value: 0,
            BindingVerificationState.UNKNOWN.value: 0,
        }
        for binding in self.bindings:
            counts[binding.verification_state(as_of=as_of, max_age=max_age).value] += 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        """Canonical rendering (sorted) suitable for durable evidence.

        Verification state is intentionally **not** rendered: it is a
        derived value that depends on a policy-supplied reference time.
        See :meth:`ExecutionBinding.verification_state`.
        """
        return {
            "schema_version": self.schema_version,
            "bindings": [binding.to_dict() for binding in self.bindings],
            "profiles": [profile.to_dict() for profile in self.profiles],
            "pools": [pool.to_dict() for pool in self.pools],
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> BindingRegistry:
        """Reconstruct a registry from its non-secret durable mapping."""
        if not isinstance(payload, Mapping):
            raise BindingRegistryError("binding registry must be a mapping")
        schema_version = payload.get("schema_version", 1)
        if schema_version != 1:
            raise BindingRegistryError("unsupported binding registry schema_version")

        def _records(key: str) -> list[Mapping[str, Any]]:
            raw = payload.get(key, [])
            if not isinstance(raw, list) or any(not isinstance(item, Mapping) for item in raw):
                raise BindingRegistryError(f"binding registry '{key}' must be a list of mappings")
            return list(raw)

        profiles: list[ExecutionProfile] = []
        for item in _records("profiles"):
            try:
                profiles.append(
                    ExecutionProfile(
                        profile_id=item["profile_id"],
                        provider=item["provider"],
                        account_ref=item.get("account_ref", ""),
                        backend_ref=item.get("backend_ref", ""),
                        enabled=item.get("enabled", True),
                        notes=tuple(item.get("notes", ())),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise BindingRegistryError("invalid execution profile mapping") from exc

        pools: list[QuotaPool] = []
        for item in _records("pools"):
            try:
                pools.append(
                    QuotaPool(
                        pool_id=item["pool_id"],
                        provider=item["provider"],
                        notes=tuple(item.get("notes", ())),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise BindingRegistryError("invalid quota pool mapping") from exc

        profile_map = {profile.profile_id: profile for profile in profiles}
        pool_map = {pool.pool_id: pool for pool in pools}
        bindings: list[ExecutionBinding] = []
        for item in _records("bindings"):
            try:
                bindings.append(
                    binding_from_mapping(item, profiles=profile_map, pools=pool_map)
                )
            except (TypeError, ValueError) as exc:
                raise BindingRegistryError("invalid execution binding mapping") from exc
        return cls(
            bindings=tuple(bindings),
            profiles=tuple(profiles),
            pools=tuple(pools),
            schema_version=int(schema_version),
        )


@dataclass(frozen=True)
class BindingRegistryError(ValueError):
    """Typed failure raised by binding-registry helpers."""

    reason: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.reason


def build_binding_registry(
    *,
    bindings: Iterable[ExecutionBinding] = (),
    profiles: Iterable[ExecutionProfile] = (),
    pools: Iterable[QuotaPool] = (),
    schema_version: int = 1,
) -> BindingRegistry:
    """Construct a :class:`BindingRegistry` over the supplied typed inputs.

    A thin convenience that promotes ``Iterable`` inputs to tuples so
    callers may freely pass lists / generators while preserving the
    frozen-dataclass invariants enforced by the registry.
    """
    return BindingRegistry(
        bindings=tuple(bindings),
        profiles=tuple(profiles),
        pools=tuple(pools),
        schema_version=schema_version,
    )


def binding_registry_from_mapping(payload: Mapping[str, Any]) -> BindingRegistry:
    """Reconstruct a binding registry from serialized non-secret values."""
    return BindingRegistry.from_mapping(payload)


def binding_from_mapping(
    payload: Mapping[str, Any],
    *,
    profiles: Mapping[str, ExecutionProfile],
    pools: Mapping[str, QuotaPool],
) -> ExecutionBinding:
    """Construct an :class:`ExecutionBinding` from a serialized mapping.

    Used by the configuration serialiser; not part of the public
    selection surface.  Fails closed on unknown enum members.
    """
    binding_id = str(payload.get("binding_id", "")).strip()
    if not binding_id:
        raise BindingRegistryError("binding_id is required")
    provider = str(payload.get("provider", "")).strip().lower()
    model = str(payload.get("model", "")).strip()
    backend_raw = str(payload.get("backend", BindingKind.DIRECT_CLI.value)).strip()
    try:
        backend = BindingKind(backend_raw)
    except ValueError as exc:
        raise BindingRegistryError(
            f"binding '{binding_id}': unknown backend {backend_raw!r}"
        ) from exc
    profile_id = str(payload.get("profile_id", "")).strip()
    if profile_id not in profiles:
        raise BindingRegistryError(
            f"binding '{binding_id}': unknown profile_id '{profile_id}'"
        )
    quota_pool_id = str(payload.get("quota_pool_id", "")).strip()
    if quota_pool_id not in pools:
        raise BindingRegistryError(
            f"binding '{binding_id}': unknown quota_pool_id '{quota_pool_id}'"
        )
    transport_raw = str(
        payload.get("transport", ProviderTransport.DIRECT_CLI.value)
    ).strip()
    try:
        transport = ProviderTransport(transport_raw)
    except ValueError as exc:
        raise BindingRegistryError(
            f"binding '{binding_id}': unknown transport {transport_raw!r}"
        ) from exc
    role_raw = str(payload.get("binding_role", BindingRole.WORKER.value)).strip()
    try:
        binding_role = BindingRole(role_raw)
    except ValueError as exc:
        raise BindingRegistryError(
            f"binding '{binding_id}': unknown binding_role {role_raw!r}"
        ) from exc
    harness = frozenset(
        Capability(str(item)) for item in payload.get("harness_capabilities", ())
    )
    semantic = tuple(str(item) for item in payload.get("semantic_capabilities", ()))
    roles = frozenset(
        DispatchRole(str(item)) for item in payload.get("supported_roles", ())
    )
    efforts: set[ReasoningEffort] = set()
    for raw in payload.get("reasoning_effort_capabilities", ()):
        try:
            efforts.add(ReasoningEffort(str(raw)))
        except ValueError as exc:
            raise BindingRegistryError(
                f"binding '{binding_id}': unknown effort {raw!r}"
            ) from exc
    risks: set[RiskLevel] = set()
    for raw in payload.get("risk_levels", ()):
        try:
            risks.add(RiskLevel(str(raw)))
        except ValueError as exc:
            raise BindingRegistryError(
                f"binding '{binding_id}': unknown risk level {raw!r}"
            ) from exc
    # An explicitly serialized ``risk_levels`` list (including an empty one)
    # is preserved verbatim so ``to_dict`` -> ``from_mapping`` round-trips
    # exactly.  Only a legacy payload that omits the key entirely receives the
    # historical non-empty default; a binding that declares no risk level must
    # never silently gain one on reload.
    if "risk_levels" in payload:
        risk_levels = risks
    else:
        risk_levels = risks or {RiskLevel.LOW, RiskLevel.MEDIUM}
    return ExecutionBinding(
        binding_id=binding_id,
        provider=provider,
        model=model,
        backend=backend,
        profile_id=profile_id,
        quota_pool_id=quota_pool_id,
        transport=transport,
        binding_role=binding_role,
        harness_capabilities=harness,
        semantic_capabilities=semantic,
        supported_roles=roles,
        reasoning_effort_capabilities=frozenset(efforts),
        risk_levels=frozenset(risk_levels),
        verified_at=(
            None
            if payload.get("verified_at") is None
            else str(payload.get("verified_at"))
        ),
        notes=tuple(str(item) for item in payload.get("notes", ())),
    )


# ---------------------------------------------------------------------------
# C11-D: exact executable binding resolution.
#
# A SupervisorDecision names a ``binding_id``; that decision alone never
# executes anything.  :func:`resolve_execution_binding` turns the named
# id into the narrow typed runtime value the canonical lifecycle both
# invokes AND records evidence from, so bind-B evidence can never cover
# a bind-A invocation.  Every failure is a typed
# :class:`BindingResolutionError` (fail closed, zero launch, no
# evidence); there is deliberately no synthetic ``provider:model``
# fallback here.
# ---------------------------------------------------------------------------

#: Non-secret request-env overlay keys carrying the exact executed
#: binding identity to the worker.  Values are references only (never
#: secret material); adapters observe them via ``WorkerRequest.env``
#: and the autonomous child projection allowlists them verbatim.
ORCH_EXECUTION_BINDING_ENV = "ORCH_EXECUTION_BINDING_ID"
ORCH_EXECUTION_PROFILE_ENV = "ORCH_EXECUTION_PROFILE_ID"
ORCH_EXECUTION_POOL_ENV = "ORCH_EXECUTION_POOL_ID"
ORCH_EXECUTION_BACKEND_ENV = "ORCH_EXECUTION_BACKEND"

#: Non-secret executable-profile selector keys.  Unlike the identity
#: markers above (metadata only), these two keys are populated ONLY
#: from the verified output of an adapter's explicit
#: ``execution_profile_overlay`` hook (see
#: :func:`_direct_cli_profile_overlay`) and the autonomous child
#: projection allowlists them verbatim, so the adapter's actual
#: invocation observes the effective account/backend the evidence
#: will record.  They never carry secret values -- only the
#: non-secret account/backend references the profile declared.
ORCH_EXECUTION_PROFILE_ACCOUNT_ENV = "ORCH_EXECUTION_PROFILE_ACCOUNT"
ORCH_EXECUTION_PROFILE_BACKEND_ENV = "ORCH_EXECUTION_PROFILE_BACKEND"

#: Stable reason vocabulary for binding-resolution refusals.  These
#: strings appear verbatim in durable events; new values are additive
#: only.
BINDING_RESOLUTION_REASONS: tuple[str, ...] = (
    "NO_REGISTRY",
    "UNKNOWN_BINDING",
    "WRONG_ROLE",
    "PROVIDER_MODEL_MISMATCH",
    "PROFILE_UNKNOWN",
    "PROFILE_DISABLED",
    "POOL_UNKNOWN",
    "UNSUPPORTED_BACKEND",
    "NO_ADAPTER",
    "EFFORT_NOT_IN_BINDING",
    "UNMAPPABLE_PROFILE",
)


@dataclass(frozen=True)
class BindingResolutionError(ValueError):
    """Typed refusal to resolve an executable binding (fail closed)."""

    reason: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.reason


@dataclass(frozen=True)
class ResolvedExecutionBinding:
    """One exact executable binding the runtime is about to invoke.

    Resolved from a single ``SupervisorDecision.binding_id`` through
    the authoritative :class:`BindingRegistry` (``try_get`` /
    ``profile_of`` / ``pool_of``).  The canonical lifecycle uses this
    value -- never the unexecuted decision -- as the source for BOTH
    the actual semantic invocation (adapter, backend path, profile
    overlay, effort tier, gateway/account pin) and the durable
    :class:`DispatchEvidence`.
    """

    binding: ExecutionBinding
    profile: ExecutionProfile
    pool: QuotaPool
    adapter: Any
    backend: BindingKind
    overlay: dict[str, str] = field(default_factory=dict)
    reasoning_effort: ReasoningEffort | None = None
    gateway_account_pin: str | None = None

    @property
    def binding_id(self) -> str:
        """The exact binding identity being executed."""
        return self.binding.binding_id

    @property
    def profile_id(self) -> str:
        """The exact profile identity being executed."""
        return self.profile.profile_id

    @property
    def quota_pool_id(self) -> str:
        """The exact quota-pool identity consumed by this execution."""
        return self.pool.pool_id


_COPILOT_FAMILY_PROVIDERS: frozenset[str] = frozenset({"copilot", "minimax"})

#: Overlay keys owned by the exact executable-binding identity (plus the
#: Copilot adapter's existing profile selector).  A generic profile hook
#: may only return the executable selector keys above; anything else --
#: including an attempt to overwrite these -- fails closed instead of
#: executing.
_RESERVED_PROFILE_OVERLAY_KEYS: frozenset[str] = frozenset(
    {
        ORCH_EXECUTION_BINDING_ENV,
        ORCH_EXECUTION_PROFILE_ENV,
        ORCH_EXECUTION_POOL_ENV,
        ORCH_EXECUTION_BACKEND_ENV,
        "ORCH_COPILOT_PROFILE_ID",
    }
)

#: The only overlay keys a generic ``execution_profile_overlay`` hook
#: may return.  The mapping is deliberately closed: executable
#: profile selection travels exclusively through these two
#: projection-allowlisted selectors, never through ad-hoc keys (which
#: the autonomous child projection would drop before the invocation)
#: and never by overwriting the identity markers (which would spoof
#: evidence).
EXECUTABLE_PROFILE_OVERLAY_KEYS: frozenset[str] = frozenset(
    {
        ORCH_EXECUTION_PROFILE_ACCOUNT_ENV,
        ORCH_EXECUTION_PROFILE_BACKEND_ENV,
    }
)


def _copilot_overlay(
    *,
    provider: str,
    profile: ExecutionProfile,
) -> dict[str, str]:
    """Return the Copilot request-env overlay for one resolved profile.

    Known Copilot profile ids reuse the adapter's existing
    ``ORCH_COPILOT_PROFILE_ID`` seam.  A copilot-family profile that
    claims distinct account/backend behavior the adapter cannot
    enforce (non-empty ``account_ref`` / ``backend_ref`` with an
    unknown profile id) is unmappable and fails closed; a profile
    with no distinct claims uses the proven ordinary adapter path
    with no overlay.
    """
    from orchestrator_mvp.workers.copilot import copilot_overlay_for_binding

    return copilot_overlay_for_binding(
        provider=provider,
        profile_id=profile.profile_id,
        account_ref=profile.account_ref,
        backend_ref=profile.backend_ref,
    )


def _direct_cli_profile_overlay(
    *,
    adapter: Any,
    binding: ExecutionBinding,
    profile: ExecutionProfile,
) -> dict[str, str]:
    """Return the executable-profile overlay for one DIRECT_CLI binding.

    This is the smallest generic profile-execution contract: an
    adapter may optionally expose a narrowly typed
    ``execution_profile_overlay(binding, profile) -> dict[str, str]``
    hook that consumes the profile's non-empty ``account_ref`` /
    ``backend_ref`` and returns the executable selectors the adapter's
    actual invocation will observe.  The hook is deliberately tiny
    and optional:

    * a profile with empty ``account_ref`` *and* ``backend_ref``
      needs no mechanism: the proven ordinary adapter path executes
      and this returns no overlay;
    * otherwise the adapter MUST expose the hook.  Absence of the
      hook means the adapter cannot enforce the claimed references;
    * the hook may return ONLY the executable selector keys
      (:data:`EXECUTABLE_PROFILE_OVERLAY_KEYS`), and every non-empty
      claimed reference must be carried verbatim by its selector --
      ``account_ref`` by ``ORCH_EXECUTION_PROFILE_ACCOUNT``,
      ``backend_ref`` by ``ORCH_EXECUTION_PROFILE_BACKEND``.  Merely
      echoing the profile id (which the base ``ORCH_EXECUTION_*``
      identity overlay already does) does not count, and ad-hoc keys
      do not count either: they cannot reach the invocation through
      the allowlisted child projection;
    * no secret value may ever be returned here -- only the
      non-secret references the profile declared, which the adapter
      resolves through its existing secret layers.

    The returned selectors ride the resolved overlay into the actual
    ``WorkerRequest``/child invocation (the autonomous child
    projection allowlists them verbatim), so evidence
    profile/account/backend always equals the effective invocation.
    Any violation fails closed with
    :class:`BindingResolutionError` (``UNMAPPABLE_PROFILE``): zero
    launch, no evidence.  There is no second AdapterRegistry and no
    WorkerAdapter rewrite; adapters opt in by defining the hook.
    """
    account = profile.account_ref.strip()
    backend_ref = profile.backend_ref.strip()
    if not account and not backend_ref:
        return {}
    hook = getattr(adapter, "execution_profile_overlay", None)
    if hook is None or not callable(hook):
        raise BindingResolutionError("UNMAPPABLE_PROFILE")
    try:
        raw_overlay = hook(binding, profile)
    except BindingResolutionError:
        raise
    except Exception as exc:
        raise BindingResolutionError("UNMAPPABLE_PROFILE") from exc
    if not isinstance(raw_overlay, dict) or not raw_overlay:
        raise BindingResolutionError("UNMAPPABLE_PROFILE")
    if len(raw_overlay) > len(EXECUTABLE_PROFILE_OVERLAY_KEYS):
        raise BindingResolutionError("UNMAPPABLE_PROFILE")
    cleaned: dict[str, str] = {}
    for key, value in raw_overlay.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise BindingResolutionError("UNMAPPABLE_PROFILE")
        if key not in EXECUTABLE_PROFILE_OVERLAY_KEYS:
            raise BindingResolutionError("UNMAPPABLE_PROFILE")
        cleaned[key] = value.strip()
    if account:
        if cleaned.get(ORCH_EXECUTION_PROFILE_ACCOUNT_ENV) != account:
            raise BindingResolutionError("UNMAPPABLE_PROFILE")
    elif ORCH_EXECUTION_PROFILE_ACCOUNT_ENV in cleaned:
        raise BindingResolutionError("UNMAPPABLE_PROFILE")
    if backend_ref:
        if cleaned.get(ORCH_EXECUTION_PROFILE_BACKEND_ENV) != backend_ref:
            raise BindingResolutionError("UNMAPPABLE_PROFILE")
    elif ORCH_EXECUTION_PROFILE_BACKEND_ENV in cleaned:
        raise BindingResolutionError("UNMAPPABLE_PROFILE")
    return cleaned


def resolve_execution_binding(
    *,
    registry: Any | None,
    binding_id: str | None,
    adapter: Any | None,
    reasoning_effort: ReasoningEffort | None = None,
) -> ResolvedExecutionBinding:
    """Resolve one decision ``binding_id`` to its executable runtime value.

    Fails closed with :class:`BindingResolutionError` when there is no
    authoritative non-empty registry, the id is unknown, the profile
    is missing/disabled, the pool is unknown, the backend has no
    verified runtime seam (``DIRECT_API``), no live adapter serves
    the binding's provider, the requested effort tier is outside the
    binding's declared capabilities, a copilot-family profile
    cannot be mapped onto the adapter's existing profile seam, a
    DIRECT_CLI profile claims a non-empty ``account_ref`` /
    ``backend_ref`` the adapter has no explicit executable mechanism
    for (no ``execution_profile_overlay`` hook, or a hook whose
    overlay does not actually carry the claimed references), or a
    GATEWAY profile claims a non-empty ``backend_ref`` no existing
    C10/backend mechanism can honor.
    """
    if registry is None or not tuple(registry.list_bindings()):
        raise BindingResolutionError("NO_REGISTRY")
    if not binding_id or not binding_id.strip():
        raise BindingResolutionError("UNKNOWN_BINDING")
    binding = registry.try_get(binding_id)
    if binding is None:
        raise BindingResolutionError("UNKNOWN_BINDING")
    try:
        profile = registry.profile_of(binding)
    except KeyError as exc:
        raise BindingResolutionError("PROFILE_UNKNOWN") from exc
    if not profile.enabled:
        raise BindingResolutionError("PROFILE_DISABLED")
    try:
        pool = registry.pool_of(binding)
    except KeyError as exc:
        raise BindingResolutionError("POOL_UNKNOWN") from exc
    if binding.backend is BindingKind.DIRECT_API:
        # No verified runtime seam exists for DIRECT_API bindings; a
        # new API backend must never be invented to satisfy dispatch.
        raise BindingResolutionError("UNSUPPORTED_BACKEND")
    if adapter is None:
        raise BindingResolutionError("NO_ADAPTER")
    if reasoning_effort is not None and not binding.supports_effort(reasoning_effort):
        raise BindingResolutionError("EFFORT_NOT_IN_BINDING")
    overlay: dict[str, str] = {
        ORCH_EXECUTION_BINDING_ENV: binding.binding_id,
        ORCH_EXECUTION_PROFILE_ENV: profile.profile_id,
        ORCH_EXECUTION_POOL_ENV: pool.pool_id,
        ORCH_EXECUTION_BACKEND_ENV: binding.backend.value,
    }
    if binding.provider in _COPILOT_FAMILY_PROVIDERS:
        try:
            overlay.update(
                _copilot_overlay(provider=binding.provider, profile=profile)
            )
        except ValueError as exc:
            raise BindingResolutionError("UNMAPPABLE_PROFILE") from exc
    elif binding.backend is BindingKind.DIRECT_CLI:
        # Generic DIRECT_CLI profiles: non-empty account_ref /
        # backend_ref execute ONLY through the adapter's explicit
        # executable-profile hook (whose overlay rides the actual
        # WorkerRequest).  Generic ORCH_EXECUTION_* identity markers
        # are metadata, never enforcement.  Empty-ref profiles keep
        # the proven ordinary adapter path with no extra overlay.
        overlay.update(
            _direct_cli_profile_overlay(adapter=adapter, binding=binding, profile=profile)
        )
    gateway_pin: str | None = None
    if binding.backend is BindingKind.GATEWAY:
        if profile.backend_ref.strip():
            # No existing C10/backend mechanism honors a gateway
            # backend_ref: ignoring it would let two same-provider
            # profiles share one real backend under different
            # evidence.  Fail closed instead.
            raise BindingResolutionError("UNMAPPABLE_PROFILE")
        if profile.account_ref.strip():
            gateway_pin = profile.account_ref.strip()
    return ResolvedExecutionBinding(
        binding=binding,
        profile=profile,
        pool=pool,
        adapter=adapter,
        backend=binding.backend,
        overlay=overlay,
        reasoning_effort=reasoning_effort,
        gateway_account_pin=gateway_pin,
    )


def resolve_worker_candidate_binding(
    *,
    registry: Any | None,
    candidate_binding_id: str | None,
    provider: str | None,
    model: str | None,
    adapter: Any | None,
    reasoning_effort: ReasoningEffort | None = None,
) -> ResolvedExecutionBinding | None:
    """Resolve one WORKER candidate's exact binding, or ``None`` when it has none.

    This is the shared ordinary-dispatch counterpart to the autonomous
    supervisor's exact-binding seam.  It preserves the C15-XB-01 law for
    the owner-facing run path:

    * a candidate with an empty/absent ``binding_id`` returns ``None`` --
      the caller keeps the historical provider/model adapter path
      (static routing candidates, explicit ``--provider``/``--model``
      overrides, older configurations without owner bindings);
    * a non-empty ``binding_id`` is authoritative.  It must name an exact
      registry binding whose role is :class:`BindingRole.WORKER` and whose
      ``provider``/``model`` agree with the candidate, and it must resolve
      through :func:`resolve_execution_binding`.  Every failure raises
      :class:`BindingResolutionError` (fail closed, zero launch) and never
      falls back to a same-provider/model first match.

    The returned :class:`ResolvedExecutionBinding` is the single value the
    runtime both invokes (adapter + overlay + effort) and records as
    durable dispatch evidence.
    """
    if not candidate_binding_id or not candidate_binding_id.strip():
        return None
    wanted = candidate_binding_id.strip()
    if registry is None or not tuple(registry.list_bindings()):
        raise BindingResolutionError("NO_REGISTRY")
    binding = registry.try_get(wanted)
    if binding is None:
        raise BindingResolutionError("UNKNOWN_BINDING")
    if getattr(binding, "binding_role", None) is not BindingRole.WORKER:
        raise BindingResolutionError("WRONG_ROLE")
    if (
        getattr(binding, "provider", None) != provider
        or getattr(binding, "model", None) != model
    ):
        raise BindingResolutionError("PROVIDER_MODEL_MISMATCH")
    return resolve_execution_binding(
        registry=registry,
        binding_id=wanted,
        adapter=adapter,
        reasoning_effort=reasoning_effort,
    )


__all__ = [
    "BINDING_RESOLUTION_REASONS",
    "EXECUTABLE_PROFILE_OVERLAY_KEYS",
    "ORCH_EXECUTION_BACKEND_ENV",
    "ORCH_EXECUTION_BINDING_ENV",
    "ORCH_EXECUTION_POOL_ENV",
    "ORCH_EXECUTION_PROFILE_ACCOUNT_ENV",
    "ORCH_EXECUTION_PROFILE_BACKEND_ENV",
    "ORCH_EXECUTION_PROFILE_ENV",
    "BindingKind",
    "BindingRegistry",
    "BindingRegistryError",
    "BindingResolutionError",
    "BindingRole",
    "BindingVerificationState",
    "ExecutionBinding",
    "ExecutionProfile",
    "QuotaPool",
    "ResolvedExecutionBinding",
    "binding_from_mapping",
    "binding_registry_from_mapping",
    "build_binding_registry",
    "resolve_worker_candidate_binding",
    "resolve_execution_binding",
]
