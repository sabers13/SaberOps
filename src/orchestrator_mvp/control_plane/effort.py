"""C11-B: provider-neutral reasoning effort controls.

Reasoning effort is a typed, provider-neutral declaration of HOW MUCH
reasoning budget is requested.  It is independent of:

* ``provider`` -- one tier may map to many concrete provider options;
* ``model``    -- different models on the same provider may support
                 different subsets of tiers;
* ``role``     -- Orchestrator, Worker and Reviewer may each carry
                 their own owner-bounded effort policy;
* ``risk``     -- the governance / review / security controls required
                 by a semantic action;
* ``quota``    -- a quota pool is a scarcity identity, not an effort
                 control;
* ``status``   -- project / run / attempt state.

Bindings declare the subset of tiers they actually support.  AUTO is
NOT a tier; it is a policy which selects a tier.  When an explicit
selected tier is unsupported by the resolved binding, the failure is
typed and observable; ULTRA in particular is never silently downgraded
to HIGH.

Ownership is shaped as:

    USER     -- sets global / role effort bounds, opts in ULTRA
    ORCH     -- selects worker / reviewer effort inside bounds
    WORKER   -- never exceeds Orch bounds (cannot choose ULTRA if Orch
                 did not authorise it)

The functions in this module are pure: no model call, no provider
discovery, no I/O.  Owner configuration is supplied by the caller.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from orchestrator_mvp.control_plane.bindings import ExecutionBinding
from orchestrator_mvp.models import ReasoningEffort, effort_order


class EffortDecisionStatus(StrEnum):
    """Typed outcome of an effort resolution."""

    RESOLVED = "RESOLVED"
    UNSUPPORTED = "UNSUPPORTED"
    UNAUTHORIZED = "UNAUTHORIZED"


# Stable reason vocabulary for effort decisions.  These strings appear
# verbatim in historical evidence; renaming changes persisted rows, so
# new values are additive only.
EFFORT_OK = "EFFORT_OK"
EFFORT_UNSUPPORTED = "EFFORT_UNSUPPORTED"
EFFORT_ULTRA_DISABLED = "EFFORT_ULTRA_DISABLED"
EFFORT_BELOW_MIN = "EFFORT_BELOW_MIN"
EFFORT_ABOVE_MAX = "EFFORT_ABOVE_MAX"


@dataclass(frozen=True)
class EffortPolicy:
    """Owner-bounded reasoning-effort policy for one slot.

    ``min_effort`` and ``max_effort`` define the inclusive range the
    Orchestrator may pick within for this slot.  ``auto`` permits the
    Orchestrator to choose; an explicit ``selected_effort`` forces that
    tier (provided the binding supports it AND the owner bounds
    include it).

    ``ultra_enabled=False`` is the safe default; enabling ULTRA is an
    explicit owner act and must never be silent.
    """

    min_effort: ReasoningEffort = ReasoningEffort.LOW
    max_effort: ReasoningEffort = ReasoningEffort.HIGH
    selected_effort: ReasoningEffort | None = None
    ultra_enabled: bool = False

    def __post_init__(self) -> None:
        if effort_order(self.min_effort) > effort_order(self.max_effort):
            raise ValueError(
                f"EffortPolicy: min_effort {self.min_effort.value} is above "
                f"max_effort {self.max_effort.value}"
            )

    @property
    def auto(self) -> bool:
        """True iff the orchestrator is permitted to choose any tier in range."""
        return self.selected_effort is None

    def contains(self, effort: ReasoningEffort) -> bool:
        """True iff ``effort`` falls inside the owner-bounded range."""
        return (
            effort_order(self.min_effort)
            <= effort_order(effort)
            <= effort_order(self.max_effort)
        )

    def to_dict(self) -> dict[str, Any]:
        """Canonical rendering suitable for owner-side evidence."""
        return {
            "min_effort": self.min_effort.value,
            "max_effort": self.max_effort.value,
            "selected_effort": (
                None if self.selected_effort is None else self.selected_effort.value
            ),
            "ultra_enabled": self.ultra_enabled,
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> EffortPolicy:
        """Reconstruct an owner effort policy from durable values."""
        if not isinstance(payload, Mapping):
            raise ValueError("effort policy must be a mapping")

        def _effort(key: str, default: ReasoningEffort) -> ReasoningEffort:
            raw = payload.get(key, default.value)
            try:
                return raw if isinstance(raw, ReasoningEffort) else ReasoningEffort(str(raw))
            except ValueError as exc:
                raise ValueError(f"unknown effort policy value for '{key}'") from exc

        raw_selected = payload.get("selected_effort")
        selected = None if raw_selected in (None, "") else _effort(
            "selected_effort", ReasoningEffort.MEDIUM
        )
        ultra = payload.get("ultra_enabled", False)
        if not isinstance(ultra, bool):
            raise ValueError("effort policy ultra_enabled must be boolean")
        return cls(
            min_effort=_effort("min_effort", ReasoningEffort.LOW),
            max_effort=_effort("max_effort", ReasoningEffort.HIGH),
            selected_effort=selected,
            ultra_enabled=ultra,
        )


@dataclass(frozen=True)
class EffortDecision:
    """The typed result of resolving an effort request.

    On ``RESOLVED`` ``resolved_effort`` is the tier the binding will
    actually execute at (after provider-specific translation by the
    adapter) and ``reason`` is ``EFFORT_OK``.  On ``UNSUPPORTED`` the
    binding does not declare a matching controllable tier.  On
    ``UNAUTHORIZED`` the owner policy forbids the selected tier -- in
    particular ULTRA was requested but the owner has not enabled it.
    """

    status: EffortDecisionStatus
    resolved_effort: ReasoningEffort | None
    requested_effort: ReasoningEffort | None
    reason: str
    binding_id: str | None = None
    policy_min: ReasoningEffort | None = None
    policy_max: ReasoningEffort | None = None

    def to_dict(self) -> dict[str, Any]:
        """Canonical rendering for historical evidence."""
        return {
            "status": self.status.value,
            "resolved_effort": (
                None if self.resolved_effort is None else self.resolved_effort.value
            ),
            "requested_effort": (
                None if self.requested_effort is None else self.requested_effort.value
            ),
            "reason": self.reason,
            "binding_id": self.binding_id,
            "policy_min": None if self.policy_min is None else self.policy_min.value,
            "policy_max": None if self.policy_max is None else self.policy_max.value,
        }


# Stable effort ranks used to express inclusive bounds.  The order
# matches :func:`orchestrator_mvp.models.effort_order`; centralised here
# so we never silently disagree with the canonical model order.
EFFORT_RANK: dict[ReasoningEffort, int] = {
    ReasoningEffort.LOW: 0,
    ReasoningEffort.MEDIUM: 1,
    ReasoningEffort.HIGH: 2,
    ReasoningEffort.ULTRA: 3,
}


def _auto_pick(
    supported: tuple[ReasoningEffort, ...],
    policy: EffortPolicy,
) -> ReasoningEffort | None:
    """Pick the highest ``supported`` tier that fits inside `` ``effort`` bounds.

    When the policy requests a specific tier (``selected_effort is not
    None``) this helper returns ``None`` so the explicit-tier branch can
    take over and emit the typed unsupported-effort reason if needed.
    """
    if not supported:
        return None
    rank = EFFORT_RANK
    in_range = tuple(
        tier for tier in supported if policy.contains(tier)
    )
    if not in_range:
        return None
    return max(in_range, key=lambda tier: rank[tier])


def resolve_effort(
    binding: ExecutionBinding | None,
    policy: EffortPolicy,
    *,
    requested_effort: ReasoningEffort | None = None,
) -> EffortDecision:
    """Resolve one reasoning-effort request against a binding and owner policy.

    Resolution order:

    1. ``requested_effort`` if the caller passed one explicitly,
       otherwise fall back to ``policy.selected_effort``.
    2. ULTRA gate: if the resolved tier is ``ULTRA`` and the owner has
       not enabled ULTRA, return ``UNAUTHORIZED`` -- ULTRA never
       silently degrades.
    3. Owner bounds: the resolved tier must fall inside
       ``[policy.min_effort, policy.max_effort]``.
    4. Binding capability: the resolved tier must be in
       ``binding.reasoning_effort_capabilities`` -- if not, the decision
       is ``UNSUPPORTED`` (typed), not a quiet downgrade.
    5. AUTO selection: when neither ``requested_effort`` nor
       ``policy.selected_effort`` is set, the highest ``supported``
       tier in the owner bounds is chosen. `` ````effort`` is
       exclusive -- ``ULTRA`` is never picked unless the owner enabled
       it AND the binding supports it.
    """
    explicit = requested_effort if requested_effort is not None else policy.selected_effort
    binding_id = None if binding is None else binding.binding_id
    supported = (
        () if binding is None else tuple(binding.reasoning_effort_capabilities)
    )

    if explicit is not None:
        tier = explicit
        if tier is ReasoningEffort.ULTRA and not policy.ultra_enabled:
            return EffortDecision(
                status=EffortDecisionStatus.UNAUTHORIZED,
                resolved_effort=None,
                requested_effort=tier,
                reason=EFFORT_ULTRA_DISABLED,
                binding_id=binding_id,
                policy_min=policy.min_effort,
                policy_max=policy.max_effort,
            )
        if not policy.contains(tier):
            reason = (
                EFFORT_BELOW_MIN
                if effort_order(tier) < effort_order(policy.min_effort)
                else EFFORT_ABOVE_MAX
            )
            return EffortDecision(
                status=EffortDecisionStatus.UNAUTHORIZED,
                resolved_effort=None,
                requested_effort=tier,
                reason=reason,
                binding_id=binding_id,
                policy_min=policy.min_effort,
                policy_max=policy.max_effort,
            )
        if tier not in supported:
            return EffortDecision(
                status=EffortDecisionStatus.UNSUPPORTED,
                resolved_effort=None,
                requested_effort=tier,
                reason=EFFORT_UNSUPPORTED,
                binding_id=binding_id,
                policy_min=policy.min_effort,
                policy_max=policy.max_effort,
            )
        return EffortDecision(
            status=EffortDecisionStatus.RESOLVED,
            resolved_effort=tier,
            requested_effort=tier,
            reason=EFFORT_OK,
            binding_id=binding_id,
            policy_min=policy.min_effort,
            policy_max=policy.max_effort,
        )

    # AUTO path.
    if not policy.ultra_enabled:
        # ULTRA is owner-gated for AUTO too -- never silently picked.
        candidate_supported: tuple[ReasoningEffort, ...] = tuple(
            t for t in supported if t is not ReasoningEffort.ULTRA
        )
    else:
        candidate_supported = supported
    chosen = _auto_pick(candidate_supported, policy)
    if chosen is None:
        return EffortDecision(
            status=EffortDecisionStatus.UNSUPPORTED,
            resolved_effort=None,
            requested_effort=None,
            reason=EFFORT_UNSUPPORTED,
            binding_id=binding_id,
            policy_min=policy.min_effort,
            policy_max=policy.max_effort,
        )
    return EffortDecision(
        status=EffortDecisionStatus.RESOLVED,
        resolved_effort=chosen,
        requested_effort=None,
        reason=EFFORT_OK,
        binding_id=binding_id,
        policy_min=policy.min_effort,
        policy_max=policy.max_effort,
    )


# Default owner-side effort policy per slot.  The defaults express the
# recommended C11-B bounds; they are policy/configuration, not hidden
# model behaviour, and may be overridden by the owner.
DEFAULT_ORCH_EFFORT_POLICY = EffortPolicy(
    min_effort=ReasoningEffort.LOW,
    max_effort=ReasoningEffort.HIGH,
    selected_effort=None,
    ultra_enabled=False,
)
DEFAULT_WORKER_EFFORT_POLICY = EffortPolicy(
    min_effort=ReasoningEffort.LOW,
    max_effort=ReasoningEffort.HIGH,
    selected_effort=None,
    ultra_enabled=False,
)
DEFAULT_REVIEWER_EFFORT_POLICY = EffortPolicy(
    min_effort=ReasoningEffort.MEDIUM,
    max_effort=ReasoningEffort.HIGH,
    selected_effort=None,
    ultra_enabled=False,
)


@dataclass(frozen=True)
class OrchestratorExecutionConfig:
    """The owner's execution-time configuration for the Orchestrator slot.

    This is owner *execution* configuration, not Project truth: nothing
    here ever enters the Project Ledger as a fact, decision or roadmap
    item.  If persistence is appropriate, it lives in
    ``owner_execution_config`` records (out of scope for this tranche);
    Project reconstruction must never depend on it.
    """

    orch_effort: EffortPolicy = DEFAULT_ORCH_EFFORT_POLICY
    worker_effort_bounds: EffortPolicy = DEFAULT_WORKER_EFFORT_POLICY
    reviewer_effort_bounds: EffortPolicy = DEFAULT_REVIEWER_EFFORT_POLICY

    def __post_init__(self) -> None:
        # Fail-closed validation: an inverted inner policy is a config
        # error, not a silent default.
        for attr in ("orch_effort", "worker_effort_bounds", "reviewer_effort_bounds"):
            value = getattr(self, attr)
            if not isinstance(value, EffortPolicy):
                raise ValueError(
                    f"OrchestratorExecutionConfig: {attr} must be an EffortPolicy"
                )

    def bounds_for(self, role: str) -> EffortPolicy:
        """Return the owner-bounded effort policy for ``role``."""
        from orchestrator_mvp.models import DispatchRole

        if role == "ORCHESTRATOR":
            return self.orch_effort
        if role == DispatchRole.REVIEW.value:
            return self.reviewer_effort_bounds
        return self.worker_effort_bounds

    def to_dict(self) -> dict[str, Any]:
        """Canonical rendering for owner-side evidence."""
        return {
            "orch_effort": self.orch_effort.to_dict(),
            "worker_effort_bounds": self.worker_effort_bounds.to_dict(),
            "reviewer_effort_bounds": self.reviewer_effort_bounds.to_dict(),
        }


def clip_orch_to_bounds(
    orch_choice: ReasoningEffort,
    bounds: EffortPolicy,
) -> EffortDecision:
    """Clip an Orchestrator-picked effort into the owner bounds for one slot.

    Used at the WORKER / REVIEWER layer to guarantee the Orchestrator
    cannot exceed owner-bounded effort. `` ``effort`` is exclusive:
    ``ULTRA`` is always rejected when the owner has not enabled it,
    even if the Orchestrator policy would otherwise allow it.
    """
    if orch_choice is ReasoningEffort.ULTRA and not bounds.ultra_enabled:
        return EffortDecision(
            status=EffortDecisionStatus.UNAUTHORIZED,
            resolved_effort=None,
            requested_effort=orch_choice,
            reason=EFFORT_ULTRA_DISABLED,
            binding_id=None,
            policy_min=bounds.min_effort,
            policy_max=bounds.max_effort,
        )
    if not bounds.contains(orch_choice):
        return EffortDecision(
            status=EffortDecisionStatus.UNAUTHORIZED,
            resolved_effort=None,
            requested_effort=orch_choice,
            reason=EFFORT_ABOVE_MAX,
            binding_id=None,
            policy_min=bounds.min_effort,
            policy_max=bounds.max_effort,
        )
    return EffortDecision(
        status=EffortDecisionStatus.RESOLVED,
        resolved_effort=orch_choice,
        requested_effort=orch_choice,
        reason=EFFORT_OK,
        binding_id=None,
        policy_min=bounds.min_effort,
        policy_max=bounds.max_effort,
    )


def supported_tiers(binding: ExecutionBinding | None) -> tuple[ReasoningEffort, ...]:
    """Return the supported effort tiers of ``binding`` in canonical order.
    `` ``effort`` is preserved across bindings."""
    if binding is None:
        return ()
    return tuple(
        sorted(binding.reasoning_effort_capabilities, key=effort_order)
    )


def is_compatible_with_translation(
    requested: ReasoningEffort,
    binding: ExecutionBinding | None,
    provider_translation: Iterable[ReasoningEffort] | None = None,
) -> bool:
    """True iff ``requested`` is actually selectable on ``binding``.

    ``provider_translation`` is the optional list of tiers a specific
    provider adapter can translate to.  When provided, both the
    binding's declared capability AND the adapter's translation must
    include ``requested``.  Use ``None`` to defer translation to the
    adapter layer.
    """
    if binding is not None and requested not in binding.reasoning_effort_capabilities:
        return False
    if provider_translation is None:
        return True
    return requested in tuple(provider_translation)


__all__ = [
    "DEFAULT_ORCH_EFFORT_POLICY",
    "DEFAULT_REVIEWER_EFFORT_POLICY",
    "DEFAULT_WORKER_EFFORT_POLICY",
    "EFFORT_ABOVE_MAX",
    "EFFORT_BELOW_MIN",
    "EFFORT_OK",
    "EFFORT_ULTRA_DISABLED",
    "EFFORT_UNSUPPORTED",
    "EffortDecision",
    "EffortDecisionStatus",
    "EffortPolicy",
    "OrchestratorExecutionConfig",
    "ReasoningEffort",
    "clip_orch_to_bounds",
    "is_compatible_with_translation",
    "resolve_effort",
    "supported_tiers",
]
