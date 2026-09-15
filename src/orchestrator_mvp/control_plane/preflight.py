"""One shared candidate admission decision used before every model call."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from orchestrator_mvp.control_plane.capabilities import (
    Capability,
    CapabilityProfile,
    capability_profile_for,
    normalize_required_capabilities,
)
from orchestrator_mvp.control_plane.policy import (
    ControlPlanePolicy,
    ProviderPolicyState,
    load_control_plane_policy,
)
from orchestrator_mvp.control_plane.quota_policy import QuotaDecision, quota_decision
from orchestrator_mvp.models import ProviderReadiness, QuotaState, WorkerCandidate
from orchestrator_mvp.routing import is_muse_model
from orchestrator_mvp.routing_config import requires_training_permission

PROVIDER_DISABLED = "PROVIDER_DISABLED"
PROVIDER_ALLOWED_WITH_WARNING = "PROVIDER_ALLOWED_WITH_WARNING"
REQUIRED_CAPABILITY_UNSUPPORTED = "REQUIRED_CAPABILITY_UNSUPPORTED"
TRAINING_PERMISSION_DENIED = "TRAINING_PERMISSION_DENIED"
PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
PROVIDER_EXECUTABLE_UNAVAILABLE = "PROVIDER_EXECUTABLE_UNAVAILABLE"
PROVIDER_READINESS_NOT_READY = "PROVIDER_READINESS_NOT_READY"
PROVIDER_READINESS_UNKNOWN = "PROVIDER_READINESS_UNKNOWN"
NO_ELIGIBLE_CANDIDATE = "NO_ELIGIBLE_CANDIDATE"
GOVERNED_INSTRUCTIONS_UNSUPPORTED = "GOVERNED_INSTRUCTIONS_UNSUPPORTED"


class _Adapter(Protocol):
    def is_available(self) -> bool: ...

    def max_prompt_bytes(self) -> int | None: ...


#: Instruction-compatibility statuses that prove faithful delivery of a
#: required project instruction contract.  Compared by string value so
#: this module never imports provider transport code: the adapter owns
#: the typed evidence, C07 owns the eligibility meaning.  Anything else
#: (``INCOMPATIBLE``, ``UNKNOWN``, garbage, exceptions) fails closed.
_COMPATIBLE_INSTRUCTION_STATUSES = frozenset({"FULL", "ADAPTED"})


def _instruction_compatibility_status(adapter: Any | None, model: str) -> str | None:
    """Return the adapter's instruction-compatibility evidence, if any.

    Returns ``None`` when the adapter exposes no
    ``instruction_compatibility`` hook (no evidence: legacy behavior is
    preserved).  Otherwise returns the hook's normalized status string;
    a raising hook or a non-string payload is reported as ``"UNKNOWN"``
    so it fails closed wherever governed delivery is required.
    """
    hook = getattr(adapter, "instruction_compatibility", None)
    if hook is None or not callable(hook):
        return None
    try:
        raw = hook(model)
    except Exception:  # noqa: BLE001 - intentional: never trust a throwing hook
        return "UNKNOWN"
    if isinstance(raw, str):
        normalized = raw.strip().upper()
        return normalized if normalized else "UNKNOWN"
    value = getattr(raw, "value", None)
    if isinstance(value, str) and value.strip():
        return value.strip().upper()
    return "UNKNOWN"


@dataclass(frozen=True)
class ControlPlaneDecision:
    candidate: WorkerCandidate
    eligible: bool
    warnings: tuple[str, ...]
    warning_reasons: tuple[str, ...]
    exclusion_reason: str | None
    missing_capabilities: tuple[str, ...]
    provider_policy: str
    quota: QuotaDecision
    training_permitted: bool
    reserve_override: bool
    capability_profile_digest: str
    control_plane_digest: str

    @property
    def reason(self) -> str | None:
        return self.exclusion_reason

    def as_payload(self) -> dict[str, object]:
        return {
            "provider": self.candidate.provider,
            "model": self.candidate.model,
            "eligible": self.eligible,
            "warnings": list(self.warnings),
            "warning_reasons": list(self.warning_reasons),
            "reason": self.exclusion_reason,
            "missing_capabilities": list(self.missing_capabilities),
            "provider_policy": self.provider_policy,
            "quota": {
                "pool_id": self.quota.pool_id,
                "normalized_remaining_pct": self.quota.normalized_remaining_pct,
                "red_line_pct": self.quota.red_line_pct,
                "mode": self.quota.mode.value,
                "reason": self.quota.reason,
                "reset_at": self.quota.reset_at,
            },
            "training_permitted": self.training_permitted,
            "reserve_override": self.reserve_override,
            "capability_profile_digest": self.capability_profile_digest,
            "control_plane_digest": self.control_plane_digest,
        }


def evaluate_control_plane(
    candidate: WorkerCandidate,
    *,
    required_capabilities: Iterable[str | Capability] = (),
    policy: ControlPlanePolicy | None = None,
    adapter: _Adapter | None = None,
    quota_states: Iterable[QuotaState] = (),
    training_allowed: bool = True,
    reserve_override: bool = False,
    prompt_bytes: int | None = None,
    control_plane_digest: str | None = None,
    binding_pool_id: str | None = None,
    requires_governed_instructions: bool = False,
    readiness_state: ProviderReadiness | None = None,
    readiness_reason: str | None = None,
    readiness_executable_available: bool = True,
    explicit_unknown_readiness: bool = False,
) -> ControlPlaneDecision:
    """Evaluate governance, training, facts, quota, then adapter availability.

    When ``binding_pool_id`` is supplied, the quota evaluation considers
    only observations whose canonical ``pool_id`` matches.  This is
    the seam through which a binding's ``quota_pool_id`` actually
    gates C07 evaluation.  When ``None``, the legacy behaviour applies
    (every supplied state is considered).

    When ``requires_governed_instructions`` is true, the dispatch must
    faithfully deliver the canonical project instruction contract (e.g.
    OpenCode's automatic ``AGENTS.md`` injection into a governed repo
    worktree).  The contract is OPTION B (narrow) by design: only
    adapters that opt into the instruction-transport evidence contract
    via the ``instruction_compatibility`` hook are evaluated against
    it.  An adapter that participates and reports evidence weaker than
    ``FULL``/``ADAPTED`` (``INCOMPATIBLE``, ``UNKNOWN``, garbage, or a
    raising hook) makes the candidate ineligible with
    ``GOVERNED_INSTRUCTIONS_UNSUPPORTED`` instead of attempting
    execution and receiving an opaque upstream refusal.  An adapter
    that does not opt in has simply not joined this compatibility
    contract: the requirement does not apply to that transport and the
    candidate is not implicitly being certified ``FULL`` -- it is
    unevaluated, the same way the contract is silent about adapters
    that have no opinion about project instructions.  Adapters that
    never carry an automatic project-instruction mechanism (for
    example Codex, Cline, Antigravity, Copilot) are correctly outside
    this contract and behave exactly as before.  The default ``False``
    preserves every existing caller exactly.
    """
    effective_policy = policy or load_control_plane_policy()
    provider_policy = effective_policy.provider(candidate.provider)
    profile: CapabilityProfile = capability_profile_for(adapter, candidate.provider)
    required = set(normalize_required_capabilities(required_capabilities))
    if prompt_bytes is not None and adapter is not None:
        max_bytes = getattr(adapter, "max_prompt_bytes", lambda: None)()
        if max_bytes is not None and prompt_bytes > max_bytes:
            required.add(Capability.LARGE_PROMPT_STDIN.value)
    missing = tuple(sorted(required - {cap.value for cap in profile.capabilities}))
    training_permitted = training_allowed or not (
        is_muse_model(candidate.model)
        or requires_training_permission(f"{candidate.provider}/{candidate.model}")
        or requires_training_permission(candidate.model)
    )
    warnings: tuple[str, ...] = ()
    warning_reasons: tuple[str, ...] = ()
    if provider_policy.state == ProviderPolicyState.ALLOWED_WITH_WARNING:
        warnings = (provider_policy.warning or "provider policy warning",)
        warning_reasons = ("PROVIDER_ALLOWED_WITH_WARNING",)
    reason: str | None = None
    eligible = True
    if provider_policy.state == ProviderPolicyState.DISABLED:
        eligible, reason = False, PROVIDER_DISABLED
    elif not training_permitted:
        eligible, reason = False, TRAINING_PERMISSION_DENIED
    elif missing:
        eligible, reason = False, REQUIRED_CAPABILITY_UNSUPPORTED
    quota = quota_decision(
        candidate,
        quota_states,
        pool_policies=effective_policy.quota_pools,
        reserve_override=reserve_override,
        pool_id=binding_pool_id,
    )
    if eligible and not quota.eligible:
        eligible, reason = False, quota.reason
    # Availability is a *live* adapter/process check, never a binding
    # configuration claim.  Selection adapters that exist solely to
    # carry binding-derived facts must declare ``is_live_dispatch=False``
    # so they are not asked to confirm a process that does not yet
    # exist.  When the adapter is ``None`` or is not a live adapter,
    # actual executable availability remains a later dispatch-time
    # check.
    live_adapter = getattr(adapter, "is_live_dispatch", True)
    if eligible and live_adapter and adapter is not None and not bool(adapter.is_available()):
        eligible, reason = False, PROVIDER_UNAVAILABLE
    elif eligible and live_adapter and adapter is None:
        eligible, reason = False, PROVIDER_UNAVAILABLE
    # Readiness is composition-supplied evidence about the exact resolved
    # connection. It follows all higher C07 authorities and never selects.
    if eligible and readiness_state is not None:
        if not readiness_executable_available:
            eligible, reason = False, PROVIDER_EXECUTABLE_UNAVAILABLE
        elif readiness_state is ProviderReadiness.NOT_READY:
            eligible, reason = False, PROVIDER_READINESS_NOT_READY
        elif readiness_state is ProviderReadiness.UNKNOWN and not explicit_unknown_readiness:
            eligible, reason = False, PROVIDER_READINESS_UNKNOWN
    if eligible and requires_governed_instructions:
        compat = _instruction_compatibility_status(adapter, candidate.model)
        if compat is not None and compat not in _COMPATIBLE_INSTRUCTION_STATUSES:
            eligible, reason = False, GOVERNED_INSTRUCTIONS_UNSUPPORTED
    return ControlPlaneDecision(
        candidate=candidate,
        eligible=eligible,
        warnings=warnings,
        warning_reasons=warning_reasons,
        exclusion_reason=reason,
        missing_capabilities=missing,
        provider_policy=provider_policy.state,
        quota=quota,
        training_permitted=training_permitted,
        reserve_override=reserve_override,
        capability_profile_digest=profile.digest,
        control_plane_digest=control_plane_digest or effective_policy.content_hash,
    )


def filter_control_plane_candidates(
    candidates: Iterable[WorkerCandidate], **kwargs: Any
) -> list[WorkerCandidate]:
    """Keep only candidates surviving hard C07 preflight, preserving order."""
    return [
        candidate
        for candidate in candidates
        if evaluate_control_plane(candidate, **kwargs).eligible
    ]
