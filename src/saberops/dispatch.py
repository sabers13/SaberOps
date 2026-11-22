"""Shared dispatch eligibility evaluation."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from saberops.capacity.cost_model import CostState
    from saberops.jobs.models import JobRequirements

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
from saberops.control_plane.quota_policy import QUOTA_EVIDENCE_REQUIRED
from saberops.models import (
    DispatchRole,
    DispatchStage,
    ProviderReadiness,
    QuotaState,
    WorkerCandidate,
    role_for_stage,
)
from saberops.workers.base import WorkerAdapter

# Reusable skip reason constants (same strings as review)
SKIP_PROVIDER_UNAVAILABLE = "provider_unavailable"
SKIP_PROVIDER_EXECUTABLE_UNAVAILABLE = "provider_executable_unavailable"
SKIP_PROVIDER_READINESS_NOT_READY = "provider_readiness_not_ready"
SKIP_PROVIDER_READINESS_UNKNOWN = "provider_readiness_unknown"
SKIP_TRAINING_POLICY = "training_policy_excludes_model"
SKIP_QUOTA_BLOCKED = "quota_blocked"
SKIP_PROMPT_TOO_LARGE = "prompt_exceeds_transport_capability"

#: Typed admission reasons that mean the pool crossed its red line.  A red-line
#: refusal is re-evaluated against a fresh safe probe when one is available
#: (N7); these are the only reasons where synthetic post-observation charges
#: can be the sole tipping term.
RED_LINE_REASONS: frozenset[str] = frozenset(
    {
        "WEEKLY_QUOTA_RED_LINE",
        "RED_LINE_5H",
        "RED_LINE_DAILY",
        "RED_LINE_WEEKLY",
        "RED_LINE_MONTHLY",
        "RED_LINE",
    }
)


def is_red_line_reason(reason: str | None) -> bool:
    """True when ``reason`` is a typed pool red-line refusal."""
    return reason in RED_LINE_REASONS


def _capacity_terms(admission: Any) -> dict[str, object] | None:
    """Expose the charge-ledger terms behind an admission decision (N7)."""
    pool_id = getattr(admission, "pool_id", None)
    if pool_id is None and getattr(admission, "remaining_pct", None) is None:
        return None
    return {
        "pool_id": pool_id,
        "window_id": getattr(admission, "binding_window", None),
        "observed_remaining_pct": getattr(
            admission, "observed_remaining_pct", None
        ),
        "prior_charges_pct": getattr(admission, "prior_charges_pct", 0.0),
        "remaining_after_charges_pct": getattr(
            admission, "remaining_pct", None
        ),
        "projected_cost_pct": getattr(admission, "cost_estimate", None),
        "reserve_pct": getattr(admission, "reserve_pct", None),
        "red_line_pct": getattr(admission, "red_line_pct", None),
        "needs_probe": bool(getattr(admission, "needs_probe", False)),
        "probe_harness": getattr(admission, "probe_harness", None),
    }


def refresh_quota_evidence(
    db: Any,
    *,
    provider: str,
    probes: Mapping[str, Any] | None = None,
    policy: ControlPlanePolicy | None = None,
    now: datetime | None = None,
) -> str:
    """Run the safe zero-inference probe seam for ``provider`` (N7/D14).

    Returns the probe outcome (``ran`` when fresh evidence was recorded).  The
    caller re-reads quota states and re-decides; a probe that cannot run leaves
    the original refusal standing.
    """
    from saberops.capacity.probes import try_safe_quota_probe
    from saberops.control_plane.plans import effective_quota_pools

    return try_safe_quota_probe(
        db,
        provider=provider,
        now=now,
        probes=dict(probes) if probes is not None else None,
        pool_policies=effective_quota_pools(policy) if policy is not None else None,
    )

#: Skip reasons where readiness evidence participated in the decision.
#: Callers use this to decide whether readiness state/reason/connection
#: fields belong on the durable skip record.
READINESS_SKIP_REASONS: frozenset[str] = frozenset(
    {SKIP_PROVIDER_READINESS_NOT_READY, SKIP_PROVIDER_READINESS_UNKNOWN}
)


def routing_context_for_role(role: Any) -> str:
    """Return the canonical routing context for a dispatch role.

    The three-role vocabulary (``PLAN`` / ``BUILD`` / ``CHECK``) is the new
    ``routing_context`` in skip/exhaustion evidence; a retired role name
    resolves to ``BUILD``.
    """
    from saberops.jobs.models import JobRole, coerce_job_role

    if isinstance(role, JobRole):
        return role.value
    try:
        return coerce_job_role(str(role)).value
    except ValueError:
        return str(role).strip().upper()


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


def capacity_probe_event_payload(
    decision: Any, probe_outcome: str
) -> dict[str, Any]:
    """Durable evidence for a zero-inference probe of a refused candidate.

    The post-probe decision alone cannot answer "which term tipped it?" because
    the successful probe resets ``prior_charges_pct`` to zero.  This payload
    keeps the pre-probe reason, the full capacity block, and the probe outcome
    together (N7-observability), and also names a stale-evidence exclusion.
    """
    payload = dict(decision.as_payload())
    payload["probe_outcome"] = probe_outcome
    payload["evidence_state"] = (
        "stale" if decision.exclusion_reason == "QUOTA_EVIDENCE_REQUIRED" else "fresh"
    )
    return payload


def format_mixed_exhaustion_summary(
    gate_command: str,
    attempt_count: int,
    skips: list[dict[str, Any]],
    *,
    routing_context: str = "",
) -> str:
    """Name the real terminal cause when attempts ran *and* candidates skipped.

    The launched attempts failed the gate; the skipped candidates are a
    secondary fact.  Reusing the zero-launch ``No eligible candidate`` wording
    here made a two-gate-failure run read as ``provider_unavailable`` (N16).
    """
    summary = (
        f"All {attempt_count} attempt(s) failed to pass gate '{gate_command}'."
    )
    if skips:
        context_suffix = f" for {routing_context}" if routing_context else ""
        lines = "\n".join(format_skipped_candidate_line(skip) for skip in skips)
        summary += (
            f" {len(skips)} further candidate(s) were skipped as ineligible"
            f"{context_suffix}:\n{lines}"
        )
    return summary

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


def skip_reason_for_decision(
    decision: ControlPlaneDecision, *, prompt_bytes: int = 0
) -> str | None:
    """Return the canonical owner-facing skip reason for one decision."""
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
    if decision.reason in (
        "WEEKLY_QUOTA_RED_LINE",
        "RED_LINE_5H",
        "RED_LINE_DAILY",
        "RED_LINE_WEEKLY",
        "RED_LINE_MONTHLY",
        "RED_LINE",
        "QUOTA_EXCLUDED_UNTIL_RESET",
        "QUOTA_EVIDENCE_REQUIRED",
        "POOL_OFF",
        "POSTURE_FORBIDDEN",
        "POSTURE_EXPIRED",
        "POSTURE_UNVERIFIED",
        "POSTURE_ATTENDED_ONLY",
        "POSTURE_METERED",
        "GLOBAL_CONCURRENCY_EXCEEDED",
        "POOL_CONCURRENCY_EXCEEDED",
        "GLOBAL_CONCURRENCY_LIMIT",
        "CONCURRENCY_LIMIT",
    ):
        return SKIP_QUOTA_BLOCKED
    if decision.reason == "MODEL_FAMILY_EXCLUDED":
        return "model_family_excluded"
    if decision.reason == "ROLE_MISMATCH":
        return "role_mismatch"
    if decision.reason == "READ_ONLY_UNSUPPORTED":
        return "read_only_unsupported"
    if decision.reason == PROVIDER_DISABLED:
        return PROVIDER_DISABLED
    if decision.reason == GOVERNED_INSTRUCTIONS_UNSUPPORTED:
        return GOVERNED_INSTRUCTIONS_UNSUPPORTED
    # Explicit ordinary overrides are intentionally not a bypass.  Retain a
    # stable reason for callers that do not yet understand quota enums.
    if decision.reason == PROVIDER_ALLOWED_WITH_WARNING:
        return None
    return decision.reason


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
    now: datetime | None = None,
    requirements: JobRequirements | None = None,
    db: Any | None = None,
    cost_states: Mapping[tuple[str, str, str, str], CostState] | None = None,
    running_counts: Mapping[str, int] | None = None,
    global_running: int = 0,
    max_global_concurrent: int | None = None,
    capacity_configured: bool | None = None,
    reservations: Mapping[tuple[str, str], float] | None = None,
) -> str | None:
    """Return the compatibility skip reason for the canonical quota decision.

    ``training_required`` / ``routing_snapshot`` carry frozen
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
        now=now,
        requirements=requirements,
        db=db,
        cost_states=cost_states,
        running_counts=running_counts,
        global_running=global_running,
        max_global_concurrent=max_global_concurrent,
        capacity_configured=capacity_configured,
        reservations=reservations,
    )
    return skip_reason_for_decision(decision, prompt_bytes=prompt_bytes)


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
    now: datetime | None = None,
    requirements: JobRequirements | None = None,
    db: Any | None = None,
    cost_states: Mapping[tuple[str, str, str, str], CostState] | None = None,
    running_counts: Mapping[str, int] | None = None,
    global_running: int = 0,
    max_global_concurrent: int | None = None,
    capacity_configured: bool | None = None,
    reservations: Mapping[tuple[str, str], float] | None = None,
) -> ControlPlaneDecision:
    """Return the typed quota decision used by compatibility callers."""
    dec = evaluate_control_plane(
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
    if not dec.eligible:
        return dec

    from saberops.capacity.admission import admit, check_capacity_tables_exist

    # Boundary: snapshot active reservations from db if not explicitly supplied
    if reservations is None and db is not None:
        try:
            if check_capacity_tables_exist(db):
                from saberops.capacity.ledger import post_observation_charges

                reservations = post_observation_charges(db, now or datetime.now(UTC))
        except (sqlite3.OperationalError, sqlite3.DatabaseError):
            return ControlPlaneDecision(
                candidate=candidate,
                eligible=False,
                warnings=dec.warnings,
                warning_reasons=dec.warning_reasons,
                exclusion_reason=QUOTA_EVIDENCE_REQUIRED,
                missing_capabilities=dec.missing_capabilities,
                provider_policy=dec.provider_policy,
                quota=dec.quota,
                training_permitted=dec.training_permitted,
                reserve_override=reserve_override,
                capability_profile_digest=dec.capability_profile_digest,
                control_plane_digest=dec.control_plane_digest,
            )

    admit_dec = admit(
        candidate=candidate,
        now=now or datetime.now(UTC),
        policy=policy,
        quota_states=quota_states,
        requirements=requirements,
        cost_states=cost_states,
        adapter=adapter,
        readiness_state=readiness_state,
        readiness_reason=readiness_reason,
        readiness_executable_available=readiness_executable_available,
        running_counts=running_counts,
        global_running=global_running,
        max_global_concurrent=max_global_concurrent,
        explicit_override=explicit_unknown_readiness,
        reserve_override=reserve_override,
        binding_pool_id=binding_pool_id,
        training_allowed=training_allowed,
        training_required=training_required,
        routing_snapshot=routing_snapshot,
        prompt_bytes=prompt_bytes,
        requires_governed_instructions=requires_governed_instructions,
        db=db,
        capacity_configured=capacity_configured,
        reservations=reservations,
    )
    if not admit_dec.admitted:
        return ControlPlaneDecision(
            candidate=candidate,
            eligible=False,
            warnings=dec.warnings,
            warning_reasons=dec.warning_reasons,
            exclusion_reason=admit_dec.reason,
            missing_capabilities=dec.missing_capabilities,
            provider_policy=dec.provider_policy,
            quota=dec.quota,
            training_permitted=dec.training_permitted,
            reserve_override=reserve_override,
            capability_profile_digest=dec.capability_profile_digest,
            control_plane_digest=dec.control_plane_digest,
            capacity=_capacity_terms(admit_dec),
        )
    return ControlPlaneDecision(
        candidate=candidate,
        eligible=True,
        warnings=dec.warnings,
        warning_reasons=dec.warning_reasons,
        exclusion_reason=dec.exclusion_reason,
        missing_capabilities=dec.missing_capabilities,
        provider_policy=dec.provider_policy,
        quota=dec.quota,
        training_permitted=dec.training_permitted,
        reserve_override=reserve_override,
        capability_profile_digest=dec.capability_profile_digest,
        control_plane_digest=dec.control_plane_digest,
        capacity=_capacity_terms(admit_dec),
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
