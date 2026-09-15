"""Shared dispatch eligibility evaluation."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from saberops.control_plane import (
    GOVERNED_INSTRUCTIONS_UNSUPPORTED,
    PROVIDER_ALLOWED_WITH_WARNING,
    PROVIDER_DISABLED,
    PROVIDER_EXECUTABLE_UNAVAILABLE,
    PROVIDER_READINESS_NOT_READY,
    PROVIDER_READINESS_UNKNOWN,
    PROVIDER_UNAVAILABLE,
    REQUIRED_CAPABILITY_UNSUPPORTED,
    TRAINING_PERMISSION_DENIED,
    evaluate_control_plane,
)
from saberops.control_plane.capabilities import Capability
from saberops.control_plane.policy import ControlPlanePolicy
from saberops.control_plane.preflight import ControlPlaneDecision
from saberops.models import (
    DispatchRole,
    DispatchStage,
    ProviderReadiness,
    QuotaState,
    RoutingState,
    WorkerCandidate,
    role_for_stage,
)
from saberops.quota import CODEX_PROVIDER, CODEX_WEEKLY_POOL
from saberops.workers.base import WorkerAdapter

# Reusable skip reason constants (same strings as review)
SKIP_PROVIDER_UNAVAILABLE = "provider_unavailable"
SKIP_PROVIDER_EXECUTABLE_UNAVAILABLE = "provider_executable_unavailable"
SKIP_PROVIDER_READINESS_NOT_READY = "provider_readiness_not_ready"
SKIP_PROVIDER_READINESS_UNKNOWN = "provider_readiness_unknown"
SKIP_TRAINING_POLICY = "training_policy_excludes_model"
SKIP_QUOTA_BLOCKED = "quota_blocked"
SKIP_PROMPT_TOO_LARGE = "prompt_exceeds_transport_capability"

#: Fixed sanitized reason used when the readiness authority itself cannot
#: be constructed or read.  Never an exception repr, path, or secret:
#: the single vocabulary token is the whole diagnostic.
READINESS_AUTHORITY_UNAVAILABLE = "READINESS_AUTHORITY_UNAVAILABLE"


@dataclass(frozen=True)
class UnavailableReadinessAdmission:
    """Fail-closed readiness projection when no authority evidence exists.

    Provider-neutral: ``UNKNOWN`` with no connection identity, no
    authentication claim, and preserved executable uncertainty (``True``)
    so automatic dispatch still fails closed on
    ``PROVIDER_READINESS_UNKNOWN`` while an explicit owner exact-UNKNOWN
    bootstrap may be attempted if every higher authority passes.
    """

    state: ProviderReadiness = ProviderReadiness.UNKNOWN
    reason: str = READINESS_AUTHORITY_UNAVAILABLE
    executable_available: bool = True
    connection_id: str | None = None


class UnavailableReadinessService:
    """Composition-seam fallback when readiness authority is unavailable.

    Satisfies the structural readiness-provider shape consumed by the
    workflow and review engines (``admission_for`` /
    ``record_success``) without importing model_access: every
    projection is ``UNKNOWN`` (fail closed for automatic dispatch,
    exact-bootstrapable for an explicit owner override), and success
    recording is a no-op because there is no authority to record
    against.  This must never be confused with an explicit test-only
    disable (``readiness_service=False`` at the service seam, which
    resolves to ``None``): this object keeps readiness admission
    engaged, ``None`` removes it.
    """

    def admission_for(
        self, *, provider: str, profile_id: str = "", backend_ref: str = ""
    ) -> UnavailableReadinessAdmission:
        return UnavailableReadinessAdmission()

    def record_success(self, connection_id: str) -> None:
        return None


def _is_quota_blocked(quota_states: list[QuotaState], provider: str) -> bool:
    provider_norm = provider.strip().lower()
    for state in quota_states:
        if state.provider != provider_norm or state.routing_state != RoutingState.BLOCKED:
            continue
        if state.provider == CODEX_PROVIDER and state.pool != CODEX_WEEKLY_POOL:
            continue
        return True
    return False


def evaluate_eligibility(
    *,
    candidate: WorkerCandidate,
    adapter: WorkerAdapter | None,
    training_allowed: bool,
    quota_states: list[QuotaState],
    prompt_bytes: int,
    explicit_override: bool = False,
    reserve_override: bool = False,
    policy: ControlPlanePolicy | None = None,
    required_capabilities: Iterable[str | Capability] = (),
    control_plane_digest: str | None = None,
    binding_pool_id: str | None = None,
    requires_governed_instructions: bool = False,
    readiness_state: ProviderReadiness | None = None,
    readiness_reason: str | None = None,
    readiness_executable_available: bool = True,
) -> str | None:
    """Return the compatibility skip reason for the canonical C07 decision."""
    decision = control_plane_decision(
        candidate=candidate,
        adapter=adapter,
        quota_states=quota_states,
        training_allowed=training_allowed,
        reserve_override=reserve_override,
        prompt_bytes=prompt_bytes,
        policy=policy,
        required_capabilities=required_capabilities,
        control_plane_digest=control_plane_digest,
        binding_pool_id=binding_pool_id,
        requires_governed_instructions=requires_governed_instructions,
        readiness_state=readiness_state,
        readiness_reason=readiness_reason,
        readiness_executable_available=readiness_executable_available,
        explicit_unknown_readiness=explicit_override,
    )
    if decision.eligible:
        return None
    if decision.reason == PROVIDER_UNAVAILABLE:
        return SKIP_PROVIDER_UNAVAILABLE
    if decision.reason == PROVIDER_EXECUTABLE_UNAVAILABLE:
        return SKIP_PROVIDER_EXECUTABLE_UNAVAILABLE
    if decision.reason == PROVIDER_READINESS_NOT_READY:
        return SKIP_PROVIDER_READINESS_NOT_READY
    if decision.reason == PROVIDER_READINESS_UNKNOWN:
        return SKIP_PROVIDER_READINESS_UNKNOWN
    if decision.reason == TRAINING_PERMISSION_DENIED:
        return SKIP_TRAINING_POLICY
    if decision.reason == REQUIRED_CAPABILITY_UNSUPPORTED:
        return (
            SKIP_PROMPT_TOO_LARGE
            if prompt_bytes and decision.missing_capabilities == ("large_prompt_stdin",)
            else decision.reason
        )
    if decision.reason == "WEEKLY_QUOTA_RED_LINE":
        return SKIP_QUOTA_BLOCKED
    if decision.reason == PROVIDER_DISABLED:
        return PROVIDER_DISABLED
    if decision.reason == GOVERNED_INSTRUCTIONS_UNSUPPORTED:
        return GOVERNED_INSTRUCTIONS_UNSUPPORTED
    # Explicit ordinary overrides are intentionally not a bypass.  Retain a
    # stable reason for callers that do not yet understand C07 enums.
    if decision.reason == PROVIDER_ALLOWED_WITH_WARNING:
        return None
    return decision.reason


def control_plane_decision(
    *,
    candidate: WorkerCandidate,
    adapter: WorkerAdapter | None,
    training_allowed: bool,
    quota_states: list[QuotaState],
    prompt_bytes: int,
    reserve_override: bool = False,
    policy: ControlPlanePolicy | None = None,
    required_capabilities: Iterable[str | Capability] = (),
    control_plane_digest: str | None = None,
    binding_pool_id: str | None = None,
    requires_governed_instructions: bool = False,
    readiness_state: ProviderReadiness | None = None,
    readiness_reason: str | None = None,
    readiness_executable_available: bool = True,
    explicit_unknown_readiness: bool = False,
) -> ControlPlaneDecision:
    """Return the typed C07 decision used by compatibility callers."""
    return evaluate_control_plane(
        candidate,
        adapter=adapter,
        quota_states=quota_states,
        training_allowed=training_allowed,
        reserve_override=reserve_override,
        prompt_bytes=prompt_bytes,
        policy=policy,
        required_capabilities=required_capabilities,
        control_plane_digest=control_plane_digest,
        binding_pool_id=binding_pool_id,
        requires_governed_instructions=requires_governed_instructions,
        readiness_state=readiness_state,
        readiness_reason=readiness_reason,
        readiness_executable_available=readiness_executable_available,
        explicit_unknown_readiness=explicit_unknown_readiness,
    )


def build_skip_payload(
    *,
    role: str | DispatchRole | DispatchStage,
    candidate: WorkerCandidate,
    reason: str,
    prompt_bytes: int,
    max_prompt_bytes: int | None,
    transport: str,
    explicit_override: bool,
) -> dict[str, Any]:
    """Build machine-readable skip payload.

    ``role`` may be a :class:`DispatchRole`, a :class:`DispatchStage`, or a
    legacy plain string.  When given a stage the canonical :class:`DispatchRole`
    is derived through :func:`role_for_stage` so the same skip-payload evidence
    always answers "what role was this dispatch performing?".
    """
    if isinstance(role, DispatchRole):
        role_str = role.value
    elif isinstance(role, DispatchStage):
        role_str = role_for_stage(role).value
    else:
        role_str = str(role)
    return {
        "role": role_str,
        "provider": candidate.provider,
        "model": candidate.model,
        "reason": reason,
        "prompt_bytes": prompt_bytes,
        "max_prompt_bytes": max_prompt_bytes,
        "transport": transport,
        "explicit_override": explicit_override,
    }
