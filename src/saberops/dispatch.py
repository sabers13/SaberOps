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

#: Skip reasons where readiness evidence participated in the decision.
#: Callers use this to decide whether readiness state/reason/connection
#: fields belong on the durable skip record.
READINESS_SKIP_REASONS: frozenset[str] = frozenset(
    {SKIP_PROVIDER_READINESS_NOT_READY, SKIP_PROVIDER_READINESS_UNKNOWN}
)


def routing_context_for(
    tier: str,
    *,
    training_allowed: bool = True,
    is_peak: bool = False,
) -> str:
    """Return the canonical selected routing context key for a dispatch tier.

    Mirrors the chain selection in
    :func:`saberops.routing.resolve_candidate_chain` (snapshot path):
    T1 always resolves ``T1.default``; T2 splits on training permission
    then peak; T3 splits on peak only.  The value names the actual
    selected context so empty-chain and exhaustion evidence can quote it.
    """
    cleaned = tier.strip().upper()
    if cleaned == "T2" and training_allowed:
        return "T2.training_allowed"
    if cleaned == "T2":
        return "T2.training_denied_peak" if is_peak else "T2.training_denied_off_peak"
    if cleaned == "T3":
        return "T3.peak" if is_peak else "T3.off_peak"
    return "T1.default"


def build_candidate_skip(
    *,
    provider: str,
    model: str,
    reason: str,
    binding_id: str | None = None,
    readiness_state: str | None = None,
    readiness_reason: str | None = None,
    connection_id: str | None = None,
    observed_at: str | None = None,
) -> dict[str, Any]:
    """Build the structured, non-secret record for one skipped candidate.

    Only sanitized typed reasons owned by the control plane are stored:
    provider/model identity, the canonical skip reason, readiness
    state/reason when readiness participated, the resolved connection id
    when known, the exact owner-selected binding id when known, and the
    existing readiness observation timestamp when it can be carried
    through.  No credential material, command output, exception repr, or
    provider stderr ever enters this record.
    """
    record: dict[str, Any] = {
        "provider": provider.strip().lower(),
        "model": model.strip(),
        "reason": reason.strip(),
    }
    cleaned_binding = (binding_id or "").strip()
    if cleaned_binding:
        record["binding_id"] = cleaned_binding
    if reason.strip() in READINESS_SKIP_REASONS:
        if readiness_state:
            record["readiness_state"] = readiness_state.strip()
        if readiness_reason:
            record["readiness_reason"] = readiness_reason.strip()
        cleaned_connection = (connection_id or "").strip()
        if cleaned_connection:
            record["connection_id"] = cleaned_connection
        cleaned_observed = (observed_at or "").strip()
        if cleaned_observed:
            record["observed_at"] = cleaned_observed
    else:
        cleaned_connection = (connection_id or "").strip()
        if cleaned_connection:
            record["connection_id"] = cleaned_connection
    return record


def format_skipped_candidate_line(skip: dict[str, Any] | Any) -> str:
    """Render one skipped candidate as an owner-actionable evidence line."""
    if isinstance(skip, dict):
        provider = str(skip.get("provider", ""))
        model = str(skip.get("model", ""))
        reason = str(skip.get("reason", ""))
        readiness_state = skip.get("readiness_state")
        readiness_reason = skip.get("readiness_reason")
    else:
        provider = str(getattr(skip, "provider", ""))
        model = str(getattr(skip, "model", ""))
        reason = str(getattr(skip, "reason", ""))
        readiness_state = getattr(skip, "readiness_state", None)
        readiness_reason = getattr(skip, "readiness_reason", None)
    identity = f"{provider}/{model}"
    if reason in READINESS_SKIP_REASONS and readiness_state and readiness_reason:
        return f"{identity}:\n    readiness {readiness_state} ({readiness_reason})"
    if reason == SKIP_PROVIDER_EXECUTABLE_UNAVAILABLE:
        return f"{identity}:\n    executable unavailable"
    return f"{identity}:\n    {reason}"


def format_no_eligible_summary(
    skips: list[dict[str, Any]],
    *,
    routing_context: str = "",
) -> str:
    """Render the owner-facing summary for an exhausted candidate chain."""
    context_suffix = f" for {routing_context}" if routing_context else ""
    if not skips:
        return f"No routing candidates are configured{context_suffix}."
    lines = "\n".join(format_skipped_candidate_line(skip) for skip in skips)
    if routing_context:
        return f"No eligible candidate{context_suffix}:\n{lines}"
    return f"No eligible candidate:\n{lines}"

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
    observed_at: str | None = None


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
    training_required: str | None = None,
    routing_snapshot: Any | None = None,
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
    """Return the compatibility skip reason for the canonical C07 decision.

    ``training_required`` / ``routing_snapshot`` carry frozen R2-B
    data-policy evidence through to the single canonical decision; when
    omitted the candidate's own frozen classification decides.
    """
    decision = control_plane_decision(
        candidate=candidate,
        adapter=adapter,
        quota_states=quota_states,
        training_allowed=training_allowed,
        training_required=training_required,
        routing_snapshot=routing_snapshot,
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
    training_required: str | None = None,
    routing_snapshot: Any | None = None,
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
        training_required=training_required,
        routing_snapshot=routing_snapshot,
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
