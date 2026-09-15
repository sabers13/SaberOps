"""C11-B: owner-selectable Orchestrator binding policy and selection.

The owner chooses the Orchestrator binding:

* ``PINNED`` -- use the exact owner-selected eligible binding; no silent
  substitution; typed failure otherwise.
* ``PREFERRED_WITH_FALLBACK`` -- try the owner-defined ordered list;
  C07 hard policy still applies to every candidate.
* ``AUTO`` -- choose among eligible ``ORCHESTRATOR`` bindings using
  the configured ordinary routing preference (not incidental
  declaration order).

Hard C07 policy (governance > training > required capabilities > quota
> transport > ordinary routing preference) is preserved verbatim: a
binding disabled by policy stays disabled, a training-denied provider
stays denied, a required capability cannot be waived by pinning, and a
profile whose owner has marked ``enabled=False`` cannot be silently
substituted by another binding that happens to share a quota pool.

PINNED never silently becomes AUTO: a pinned unavailable binding
returns a typed ``ORCH_BINDING_PROFILE_DISABLED`` / ``ORCH_BINDING_*
`` reason (or the corresponding C07 reason), never a quiet swap to a
different eligible binding.

The selection-layer adapter is not a live adapter; it carries only the
binding-derived facts (capability profile, transport, prompt-byte
limit) that the canonical C07 evaluator consumes.  Real adapter /
process availability is confirmed at dispatch time and is never
encoded as a planning-time positive observation.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from saberops.control_plane.bindings import (
    BindingRegistry,
    BindingRole,
    ExecutionBinding,
)
from saberops.control_plane.capabilities import Capability
from saberops.control_plane.policy import ControlPlanePolicy
from saberops.control_plane.preflight import (
    PROVIDER_DISABLED,
    PROVIDER_UNAVAILABLE,
    REQUIRED_CAPABILITY_UNSUPPORTED,
    TRAINING_PERMISSION_DENIED,
    ControlPlaneDecision,
    evaluate_control_plane,
)
from saberops.models import (
    OrchBindingMode,
    QuotaState,
    WorkerCandidate,
)


class OrchSelectionStatus(StrEnum):
    """Outcome of :func:`select_orch_binding`."""

    RESOLVED = "RESOLVED"
    UNRESOLVED = "UNRESOLVED"


# Stable reason vocabulary for Orchestrator binding failures.  These are
# emitted verbatim into historical evidence; renaming them changes
# persisted evidence, so new values are additive only.
ORCH_BINDING_NOT_FOUND = "ORCH_BINDING_NOT_FOUND"
ORCH_BINDING_PINNED_UNAVAILABLE = "ORCH_BINDING_PINNED_UNAVAILABLE"
ORCH_BINDING_PINNED_DISABLED = "ORCH_BINDING_PINNED_DISABLED"
ORCH_BINDING_PINNED_TRAINING = "ORCH_BINDING_PINNED_TRAINING"
ORCH_BINDING_PINNED_CAPABILITY = "ORCH_BINDING_PINNED_CAPABILITY"
ORCH_BINDING_PINNED_QUOTA = "ORCH_BINDING_PINNED_QUOTA"
ORCH_BINDING_PINNED_RISK = "ORCH_BINDING_PINNED_RISK"
ORCH_BINDING_PROFILE_DISABLED = "ORCH_BINDING_PROFILE_DISABLED"
ORCH_BINDING_NO_ELIGIBLE = "ORCH_BINDING_NO_ELIGIBLE"
ORCH_BINDING_AUTO_NONE = "ORCH_BINDING_AUTO_NONE"
ORCH_BINDING_EMPTY_PREFERRED = "ORCH_BINDING_EMPTY_PREFERRED"


@dataclass(frozen=True)
class OrchBindingPolicy:
    """The owner's binding policy for the Orchestrator slot.

    The owner sets one of the three :class:`OrchBindingMode` values.  For
    ``PINNED`` exactly one of ``pinned_binding_id`` must name a known
    binding; for ``PREFERRED_WITH_FALLBACK`` ``preferred_binding_ids``
    is the ordered list; for ``AUTO`` both are ignored and the
    selection considers every ``ORCHESTRATOR`` binding eligible for the
    requested risk level, ordered by the configured ordinary routing
    preference.

    This is owner *execution* configuration.  It is not Project truth:
    nothing here ever enters the Project Ledger as a fact, decision or
    roadmap item.
    """

    mode: OrchBindingMode
    pinned_binding_id: str | None = None
    preferred_binding_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.mode is OrchBindingMode.PINNED:
            if not self.pinned_binding_id or not self.pinned_binding_id.strip():
                raise ValueError(
                    "OrchBindingPolicy: PINNED mode requires pinned_binding_id"
                )
            if self.preferred_binding_ids:
                raise ValueError(
                    "OrchBindingPolicy: PINNED mode forbids preferred_binding_ids"
                )
        elif self.mode is OrchBindingMode.PREFERRED_WITH_FALLBACK:
            if not self.preferred_binding_ids:
                raise ValueError(
                    "OrchBindingPolicy: PREFERRED_WITH_FALLBACK requires a "
                    "non-empty preferred_binding_ids ordering"
                )

    def to_dict(self) -> dict[str, Any]:
        """Canonical rendering suitable for durable evidence."""
        return {
            "mode": self.mode.value,
            "pinned_binding_id": self.pinned_binding_id,
            "preferred_binding_ids": list(self.preferred_binding_ids),
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> OrchBindingPolicy:
        """Reconstruct an owner binding policy from durable values."""
        if not isinstance(payload, Mapping):
            raise ValueError("OrchBindingPolicy must be a mapping")
        try:
            mode = payload.get("mode")
            parsed_mode = mode if isinstance(mode, OrchBindingMode) else OrchBindingMode(str(mode))
            pinned = payload.get("pinned_binding_id")
            preferred = payload.get("preferred_binding_ids", ())
            if not isinstance(preferred, list | tuple) or any(
                not isinstance(item, str) for item in preferred
            ):
                raise ValueError("preferred_binding_ids must be a list of strings")
            if pinned is not None and not isinstance(pinned, str):
                raise ValueError("pinned_binding_id must be a string or null")
            return cls(
                mode=parsed_mode,
                pinned_binding_id=pinned,
                preferred_binding_ids=tuple(preferred),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid OrchBindingPolicy mapping") from exc


@dataclass(frozen=True)
class OrchBindingSelection:
    """The typed result of resolving an Orchestrator binding.

    ``RESOLVED`` carries the chosen binding and the candidate pool that
    was considered; ``UNRESOLVED`` carries the typed reason plus the
    pool that was actually inspected so historical evidence can answer
    *what* the owner asked for and *why* nothing could satisfy it.
    """

    status: OrchSelectionStatus
    binding: ExecutionBinding | None
    requested_binding_id: str | None = None
    candidate_pool: tuple[str, ...] = ()
    reason: str | None = None
    reason_chain: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Canonical rendering for durable evidence."""
        return {
            "status": self.status.value,
            "binding": None if self.binding is None else self.binding.to_dict(),
            "requested_binding_id": self.requested_binding_id,
            "candidate_pool": list(self.candidate_pool),
            "reason": self.reason,
            "reason_chain": list(self.reason_chain),
        }


def _binding_to_candidate(binding: ExecutionBinding) -> WorkerCandidate:
    """Project a binding onto the legacy ``WorkerCandidate`` shape C07 needs."""
    return WorkerCandidate(provider=binding.provider, model=binding.model)


class _SelectionAdapter:
    """Selection-layer adapter carrying binding-derived facts.

    This adapter exists solely so the existing canonical C07 evaluator
    can consume the binding's factual capabilities and transport
    through the same ``capability_profile_descriptor`` seam the real
    adapter surface uses.  It is **not** a live adapter:

    * ``is_live_dispatch = False`` -- the C07 availability gate
      skips this adapter; actual executable availability is confirmed
      at dispatch time, never encoded as a planning-time positive
      observation.
    * ``is_available()`` exists only to satisfy the protocol and is
      never consulted while ``is_live_dispatch`` is ``False``.
    * ``max_prompt_bytes()`` is unset at selection time; the prompt
      length gate is checked at dispatch.
    """

    is_live_dispatch: bool = False

    def __init__(self, binding: ExecutionBinding) -> None:
        self._binding = binding

    def capability_profile_descriptor(self) -> dict[str, Any]:
        """Forward the binding's factual capabilities to the C07 evaluator."""
        return self._binding.capability_profile_descriptor()

    def provider_name(self) -> str:
        return self._binding.provider

    def is_available(self) -> bool:
        # Never consulted (see class docstring).  Returns ``True``
        # defensively in case a future caller ignores
        # ``is_live_dispatch``.
        return True

    def max_prompt_bytes(self) -> int | None:
        return None


def _orchestrator_candidates(
    registry: BindingRegistry,
    *,
    risk: Any | None = None,
) -> list[ExecutionBinding]:
    """Every binding qualified to serve the Orchestrator slot.

    If ``risk`` is supplied, only bindings declared for that risk are
    returned.
    """
    orch = list(registry.for_role(BindingRole.ORCHESTRATOR))
    if risk is None:
        return orch
    return [binding for binding in orch if binding.supports_risk(risk)]


def _profile_enabled(
    registry: BindingRegistry, binding: ExecutionBinding
) -> bool:
    """True iff the profile this binding references is owner-enabled.

    Profile ``enabled`` is the configuration gate.  When ``False`` the
    binding is ineligible regardless of any C07 policy outcome.
    """
    profile = registry.profile_of(binding)
    return bool(profile.enabled)


def _auto_preference_order(
    bindings: list[ExecutionBinding],
    routing_preference: Sequence[tuple[str, str]] | None,
) -> list[ExecutionBinding]:
    """Apply the configured ordinary routing preference to AUTO candidates.

    ``routing_preference`` is the ordered list of ``(provider, model)``
    tuples the routing layer already exposes (e.g. the canonical
    ``RoutingSnapshot.t1_default`` chain).  When supplied, candidates
    whose ``(provider, model)`` matches a tuple appear in the order of
    that tuple; candidates not present in the preference are appended
    afterwards in stable declaration order so AUTO never silently drops
    an eligible binding.  When ``routing_preference`` is ``None``, the
    configured declaration order is used and the call site is
    responsible for documenting it as the configured preference.
    """
    if routing_preference is None:
        return list(bindings)
    position: dict[tuple[str, str], int] = {
        (provider.strip().lower(), model.strip()): index
        for index, (provider, model) in enumerate(routing_preference)
    }
    ranked: list[tuple[int, int, ExecutionBinding]] = []
    for index, binding in enumerate(bindings):
        key = (binding.provider, binding.model)
        order = position.get(key, len(position) + 1)
        ranked.append((order, index, binding))
    ranked.sort(key=lambda item: (item[0], item[1]))
    return [binding for _, _, binding in ranked]


def _candidate_chain_for_auto(
    registry: BindingRegistry,
    *,
    risk: Any | None = None,
    routing_preference: Sequence[tuple[str, str]] | None = None,
) -> list[ExecutionBinding]:
    """The deterministic candidate chain considered by ``AUTO`` mode.

    Returns the eligible orchestrator bindings in the configured
    ordinary routing preference order, then declaration order for any
    binding not present in the preference.  C07 still decides
    eligibility for each candidate individually.
    """
    candidates = _orchestrator_candidates(registry, risk=risk)
    return _auto_preference_order(candidates, routing_preference)


def _evaluate_binding(
    binding: ExecutionBinding,
    *,
    required_capabilities: Iterable[str | Capability],
    control_plane_policy: ControlPlanePolicy | None,
    quota_states: Sequence[QuotaState],
    training_allowed: bool,
    reserve_override: bool,
    prompt_bytes: int | None,
) -> ControlPlaneDecision:
    """Run the canonical C07 evaluator over one binding.

    The binding's facts (capability profile, transport, scarcity
    identity) reach the evaluator through the
    ``_SelectionAdapter.capability_profile_descriptor`` seam and the
    ``binding_pool_id`` parameter.  No second capability evaluator is
    introduced.
    """
    return evaluate_control_plane(
        _binding_to_candidate(binding),
        required_capabilities=required_capabilities,
        policy=control_plane_policy,
        adapter=_SelectionAdapter(binding),
        quota_states=quota_states,
        training_allowed=training_allowed,
        reserve_override=reserve_override,
        prompt_bytes=prompt_bytes,
        binding_pool_id=binding.quota_pool_id,
    )


def select_orch_binding(
    policy: OrchBindingPolicy,
    registry: BindingRegistry,
    *,
    quota_states: Sequence[QuotaState] = (),
    training_allowed: bool = True,
    required_capabilities: Iterable[str | Capability] = (),
    risk: Any | None = None,
    reserve_override: bool = False,
    control_plane_policy: ControlPlanePolicy | None = None,
    prompt_bytes: int | None = None,
    routing_preference: Sequence[tuple[str, str]] | None = None,
) -> OrchBindingSelection:
    """Resolve an Orchestrator binding per owner policy.

    The function is pure over its inputs.  It never contacts a
    provider, runs a CLI, or consults a live process; the only live
    inputs are owner policy and the registry / quota observations /
    control-plane policy / preference routing preference the caller hands
    it.

    C07 hard policy precedence is preserved verbatim:

    1. provider governance (disabled wins outright);
    2. training permission;
    3. required factual capabilities (model-neutral, capability-based,
       evaluated against the binding's declared capability profile);
    4. quota eligibility (only observations for the binding's
       ``quota_pool_id`` are considered);
    5. transport / availability.

    A binding that fails any of those gates is not silently replaced.
    Profile-level ``enabled=False`` is an additional, earlier
    configuration gate: a disabled profile is ineligible for PINNED,
    PREFERRED and AUTO alike.
    """
    if policy.mode is OrchBindingMode.PINNED:
        pinned = registry.try_get(policy.pinned_binding_id or "")
        if pinned is None:
            return OrchBindingSelection(
                status=OrchSelectionStatus.UNRESOLVED,
                binding=None,
                requested_binding_id=policy.pinned_binding_id,
                candidate_pool=(),
                reason=ORCH_BINDING_NOT_FOUND,
                reason_chain=(ORCH_BINDING_NOT_FOUND,),
            )
        if not pinned.is_orchestrator_capable:
            return OrchBindingSelection(
                status=OrchSelectionStatus.UNRESOLVED,
                binding=pinned,
                requested_binding_id=pinned.binding_id,
                candidate_pool=(pinned.binding_id,),
                reason=ORCH_BINDING_PINNED_DISABLED,
                reason_chain=(ORCH_BINDING_PINNED_DISABLED,),
            )
        if not _profile_enabled(registry, pinned):
            return OrchBindingSelection(
                status=OrchSelectionStatus.UNRESOLVED,
                binding=pinned,
                requested_binding_id=pinned.binding_id,
                candidate_pool=(pinned.binding_id,),
                reason=ORCH_BINDING_PROFILE_DISABLED,
                reason_chain=(ORCH_BINDING_PROFILE_DISABLED,),
            )
        decision = _evaluate_binding(
            pinned,
            required_capabilities=required_capabilities,
            control_plane_policy=control_plane_policy,
            quota_states=quota_states,
            training_allowed=training_allowed,
            reserve_override=reserve_override,
            prompt_bytes=prompt_bytes,
        )
        if not decision.eligible:
            return OrchBindingSelection(
                status=OrchSelectionStatus.UNRESOLVED,
                binding=pinned,
                requested_binding_id=pinned.binding_id,
                candidate_pool=(pinned.binding_id,),
                reason=_orchestrator_reason(decision),
                reason_chain=(_orchestrator_reason(decision), decision.reason or ""),
            )
        if risk is not None and not pinned.supports_risk(risk):
            return OrchBindingSelection(
                status=OrchSelectionStatus.UNRESOLVED,
                binding=pinned,
                requested_binding_id=pinned.binding_id,
                candidate_pool=(pinned.binding_id,),
                reason=ORCH_BINDING_PINNED_RISK,
                reason_chain=(ORCH_BINDING_PINNED_RISK,),
            )
        return OrchBindingSelection(
            status=OrchSelectionStatus.RESOLVED,
            binding=pinned,
            requested_binding_id=pinned.binding_id,
            candidate_pool=(pinned.binding_id,),
            reason=None,
            reason_chain=(),
        )

    if policy.mode is OrchBindingMode.PREFERRED_WITH_FALLBACK:
        chain = [b for bid in policy.preferred_binding_ids for b in (registry.try_get(bid),) if b]
        chain_ids = tuple(binding.binding_id for binding in chain)
        if not chain:
            return OrchBindingSelection(
                status=OrchSelectionStatus.UNRESOLVED,
                binding=None,
                requested_binding_id=None,
                candidate_pool=(),
                reason=ORCH_BINDING_EMPTY_PREFERRED,
                reason_chain=(ORCH_BINDING_EMPTY_PREFERRED,),
            )
        skipped_profile_disabled: list[str] = []
        for binding in chain:
            if not binding.is_orchestrator_capable:
                continue
            if not _profile_enabled(registry, binding):
                skipped_profile_disabled.append(binding.binding_id)
                continue
            decision = _evaluate_binding(
                binding,
                required_capabilities=required_capabilities,
                control_plane_policy=control_plane_policy,
                quota_states=quota_states,
                training_allowed=training_allowed,
                reserve_override=reserve_override,
                prompt_bytes=prompt_bytes,
            )
            if not decision.eligible:
                continue
            if risk is not None and not binding.supports_risk(risk):
                continue
            return OrchBindingSelection(
                status=OrchSelectionStatus.RESOLVED,
                binding=binding,
                requested_binding_id=binding.binding_id,
                candidate_pool=chain_ids,
                reason=None,
                reason_chain=(),
            )
        if skipped_profile_disabled and len(skipped_profile_disabled) == len(chain):
            return OrchBindingSelection(
                status=OrchSelectionStatus.UNRESOLVED,
                binding=None,
                requested_binding_id=None,
                candidate_pool=chain_ids,
                reason=ORCH_BINDING_PROFILE_DISABLED,
                reason_chain=(ORCH_BINDING_PROFILE_DISABLED,),
            )
        return OrchBindingSelection(
            status=OrchSelectionStatus.UNRESOLVED,
            binding=None,
            requested_binding_id=None,
            candidate_pool=chain_ids,
            reason=ORCH_BINDING_NO_ELIGIBLE,
            reason_chain=(ORCH_BINDING_NO_ELIGIBLE,),
        )

    # AUTO: use the configured ordinary routing preference over ORCHESTRATOR
    # bindings; declaration order is the documented fallback when no
    # preference is supplied.
    chain = _candidate_chain_for_auto(
        registry, risk=risk, routing_preference=routing_preference
    )
    if not chain:
        return OrchBindingSelection(
            status=OrchSelectionStatus.UNRESOLVED,
            binding=None,
            requested_binding_id=None,
            candidate_pool=(),
            reason=ORCH_BINDING_AUTO_NONE,
            reason_chain=(ORCH_BINDING_AUTO_NONE,),
        )
    chain_ids = tuple(binding.binding_id for binding in chain)
    for binding in chain:
        if not _profile_enabled(registry, binding):
            continue
        decision = _evaluate_binding(
            binding,
            required_capabilities=required_capabilities,
            control_plane_policy=control_plane_policy,
            quota_states=quota_states,
            training_allowed=training_allowed,
            reserve_override=reserve_override,
            prompt_bytes=prompt_bytes,
        )
        if decision.eligible:
            return OrchBindingSelection(
                status=OrchSelectionStatus.RESOLVED,
                binding=binding,
                requested_binding_id=None,
                candidate_pool=chain_ids,
                reason=None,
                reason_chain=(),
            )
    return OrchBindingSelection(
        status=OrchSelectionStatus.UNRESOLVED,
        binding=None,
        requested_binding_id=None,
        candidate_pool=chain_ids,
        reason=ORCH_BINDING_NO_ELIGIBLE,
        reason_chain=(ORCH_BINDING_NO_ELIGIBLE,),
    )


def _orchestrator_reason(decision: ControlPlaneDecision) -> str:
    """Translate a C07 preflight reason into the orchestrator vocabulary."""
    if decision.reason == PROVIDER_DISABLED:
        return ORCH_BINDING_PINNED_DISABLED
    if decision.reason == TRAINING_PERMISSION_DENIED:
        return ORCH_BINDING_PINNED_TRAINING
    if decision.reason == REQUIRED_CAPABILITY_UNSUPPORTED:
        return ORCH_BINDING_PINNED_CAPABILITY
    # C07 emits ``WEEKLY_QUOTA_RED_LINE`` (or other ``*_QUOTA_*`` variants);
    # any quota-typed reason maps to the orchestrator quota vocabulary.
    if decision.reason and "QUOTA" in decision.reason:
        return ORCH_BINDING_PINNED_QUOTA
    if decision.reason == PROVIDER_UNAVAILABLE:
        return ORCH_BINDING_PINNED_UNAVAILABLE
    return decision.reason or ORCH_BINDING_PINNED_UNAVAILABLE


__all__ = [
    "ORCH_BINDING_AUTO_NONE",
    "ORCH_BINDING_EMPTY_PREFERRED",
    "ORCH_BINDING_NO_ELIGIBLE",
    "ORCH_BINDING_NOT_FOUND",
    "ORCH_BINDING_PINNED_CAPABILITY",
    "ORCH_BINDING_PINNED_DISABLED",
    "ORCH_BINDING_PINNED_QUOTA",
    "ORCH_BINDING_PINNED_RISK",
    "ORCH_BINDING_PINNED_TRAINING",
    "ORCH_BINDING_PINNED_UNAVAILABLE",
    "ORCH_BINDING_PROFILE_DISABLED",
    "OrchBindingPolicy",
    "OrchBindingSelection",
    "OrchSelectionStatus",
    "select_orch_binding",
]
