"""Canonical admission and scoring policy (ADR-0002 D7, pure).

Shared at BOTH dispatch points:
1. Queued job primary route selection (scheduler).
2. In-run worker / continuation / repair / review dispatch (runner).

Pure and deterministic: injectable clock, no DB, no filesystem, no network,
no provider calls.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from saberops.capacity.cost_model import CostState, SizeBucket, cost_estimate
from saberops.capacity.exclusions import model_scope_matches, parse_iso
from saberops.control_plane import (
    GOVERNED_INSTRUCTIONS_UNSUPPORTED,
    PROVIDER_DISABLED,
    PROVIDER_EXECUTABLE_UNAVAILABLE,
    PROVIDER_READINESS_NOT_READY,
    PROVIDER_READINESS_UNKNOWN,
    PROVIDER_UNAVAILABLE,
    REQUIRED_CAPABILITY_UNSUPPORTED,
    TRAINING_PERMISSION_DENIED,
)
from saberops.control_plane.capabilities import (
    Capability,
    capability_profile_for,
    normalize_required_capabilities,
)
from saberops.control_plane.plans import (
    Window,
    effective_quota_pools,
)
from saberops.control_plane.policy import (
    ControlPlanePolicy,
    ProviderPolicyState,
    load_control_plane_policy,
)
from saberops.control_plane.posture import (
    POSTURE_ATTENDED_ONLY,
    POSTURE_EXPIRED,
    POSTURE_FORBIDDEN,
    POSTURE_METERED,
    POSTURE_UNVERIFIED,
    posture_decision,
)
from saberops.control_plane.preflight import (
    _COMPATIBLE_INSTRUCTION_STATUSES,
    _instruction_compatibility_status,
)
from saberops.control_plane.quota_policy import (
    POOL_OFF,
    QUOTA_EVIDENCE_REQUIRED,
    QUOTA_EXCLUDED_UNTIL_RESET,
    QuotaPoolMode,
    QuotaPoolPolicy,
    _member_matches,
    _red_line_reason,
    pools_for,
)
from saberops.jobs.models import JobRequirements
from saberops.models import ProviderReadiness, QuotaState, WorkerCandidate
from saberops.training_policy import is_training_permitted
from saberops.workers.base import WorkerAdapter

# Known zero-inference harnesses matching capacity.probes
_KNOWN_PROBE_HARNESSES: frozenset[str] = frozenset({"codex", "claude", "opencode-go"})

# Map pools to probe harnesses
_POOL_TO_HARNESS: dict[str, str] = {
    "chatgpt-plus": "codex",
    "codex": "codex",
    "claude-pro": "claude",
    "claude": "claude",
    "anthropic": "claude",
    "opencode-go": "opencode-go",
}

# Day names corresponding to datetime.weekday() 0..6
_WEEKDAYS: tuple[str, ...] = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# Minimum hours divisor to prevent divide-by-zero or extreme spikes
MIN_HOURS_TO_RESET: float = 0.25

# Fallback duration in hours when reset is unknown
FALLBACK_RESET_HOURS: dict[str, float] = {
    "5h": 5.0,
    "session": 5.0,
    "daily": 24.0,
    "weekly": 168.0,
    "app-weekly": 168.0,
    "monthly": 720.0,
}


@dataclass(frozen=True)
class AdmissionDecision:
    """Canonical typed admission decision for one candidate route."""

    admitted: bool
    candidate: WorkerCandidate
    reason: str | None = None
    pool_id: str | None = None
    binding_window: str | None = None
    score: float | None = None
    needs_probe: bool = False
    probe_harness: str | None = None
    reset_at: str | None = None
    eta: datetime | None = None
    wait_confidence: str | None = None  # "EXACT" | "PARSED" | "ESTIMATED" | "CONSERVATIVE"
    cost_estimate: float | None = None
    remaining_pct: float | None = None
    red_line_pct: float | None = None
    reserve_pct: float | None = None
    multiplier: float = 1.0
    details: dict[str, Any] = field(default_factory=dict)


def _utcnow(now: datetime | None) -> datetime:
    if now is not None:
        return now.astimezone(UTC) if now.tzinfo else now.replace(tzinfo=UTC)
    return datetime.now(UTC)


def calculate_hours_to_reset(
    reset_at: str | None,
    now: datetime,
    *,
    fallback_hours: float = 24.0,
) -> float:
    """Calculate hours remaining until reset, bounded below by MIN_HOURS_TO_RESET.

    Conservative: if reset is unparseable or absent, returns fallback_hours.
    """
    if not reset_at or not isinstance(reset_at, str):
        return max(fallback_hours, MIN_HOURS_TO_RESET)
    dt = parse_iso(reset_at)
    if dt is None:
        return max(fallback_hours, MIN_HOURS_TO_RESET)
    moment = _utcnow(now)
    reset_moment = _utcnow(dt)
    diff = (reset_moment - moment).total_seconds() / 3600.0
    return max(diff, MIN_HOURS_TO_RESET)


def score(
    remaining_pct: float,
    red_line_pct: float,
    reserve_pct: float,
    cost_estimate_pct: float,
    hours_to_reset: float,
) -> float:
    """Compute D7 binding-window pace score:

    pace = (remaining_pct - red_line_pct - reserve_pct - cost_estimate_pct) / max(hours, 0.25)

    Fails closed on NaN, Inf, or negative inputs.
    """
    for val in (remaining_pct, red_line_pct, reserve_pct, cost_estimate_pct, hours_to_reset):
        if math.isnan(val) or math.isinf(val):
            raise ValueError("score inputs must be finite and non-NaN")
    if hours_to_reset < 0:
        raise ValueError("hours_to_reset must be non-negative")

    h = max(hours_to_reset, MIN_HOURS_TO_RESET)
    numerator = remaining_pct - red_line_pct - reserve_pct - cost_estimate_pct
    return numerator / h


def matches_model_family(candidate: WorkerCandidate, family: str) -> bool:
    """Return True if candidate belongs to the requested model family.

    Matching is case-insensitive and checks:
    - Candidate provider
    - Model basename or slash-separated prefix
    - Tokenized subparts of candidate id
    """
    fam = family.strip().lower()
    if not fam:
        return False
    provider = candidate.provider.strip().lower()
    if provider == fam:
        return True
    model = candidate.model.strip().lower()
    route = f"{provider}/{model}"
    if model == fam or route == fam:
        return True
    parts = set(re.split(r"[/_\-.]+", route))
    if fam in parts:
        return True
    base_model = model.split("/")[-1]
    if base_model == fam or base_model.startswith(f"{fam}-") or base_model.startswith(f"{fam}_"):
        return True
    return False


def reserve_pct(
    pool_id: str,
    window_id: str,
    now: datetime,
    policy: ControlPlanePolicy | None,
) -> float:
    """Compute the active time-of-day reserve percentage for (pool_id, window_id).

    Evaluated against policy.plans.reserves. Returns 0.0 if unconfigured or inactive.
    """
    if policy is None or not policy.plans.reserves:
        return 0.0

    pid = pool_id.strip()
    wid = window_id.strip()
    active_pct = 0.0

    for res in policy.plans.reserves:
        if res.pool != pid or res.window != wid:
            continue
        tz: Any
        try:
            tz = ZoneInfo(res.tz)
        except (ZoneInfoNotFoundError, ValueError):
            tz = UTC
        now_local = now.astimezone(tz)
        day_str = _WEEKDAYS[now_local.weekday()]
        if res.days and day_str not in res.days:
            continue
        try:
            start_str, end_str = res.hours.split("-")
            start_time = time.fromisoformat(start_str)
            end_time = time.fromisoformat(end_str)
        except (ValueError, TypeError):
            continue

        cur_time = now_local.time()
        matches_hours = False
        if start_time <= end_time:
            matches_hours = start_time <= cur_time < end_time
        else:
            # Overnight span
            matches_hours = cur_time >= start_time or cur_time < end_time

        if matches_hours and res.pct > active_pct:
            active_pct = res.pct

    return active_pct


def _get_cost_estimate(
    candidate: WorkerCandidate,
    pool_id: str,
    window_id: str,
    size_bucket: SizeBucket | str,
    cost_states: Mapping[tuple[str, str, str, str], CostState] | None,
) -> float:
    """Retrieve cost estimate percent for a candidate/pool/window/bucket."""
    route = f"{candidate.provider}/{candidate.model}".strip()
    bucket_val = size_bucket.value if isinstance(size_bucket, SizeBucket) else str(size_bucket)
    st: CostState | None = None
    if cost_states is not None:
        st = cost_states.get((route, pool_id, window_id, bucket_val))
    return cost_estimate(st, window_id, size_bucket).pct


def admit(
    candidate: WorkerCandidate,
    *,
    now: datetime,
    policy: ControlPlanePolicy | None = None,
    quota_states: Iterable[QuotaState] = (),
    requirements: JobRequirements | None = None,
    size_bucket: SizeBucket | str = SizeBucket.M,
    cost_states: Mapping[tuple[str, str, str, str], CostState] | None = None,
    adapter: WorkerAdapter | None = None,
    readiness_state: ProviderReadiness | None = None,
    readiness_reason: str | None = None,
    readiness_executable_available: bool = True,
    running_counts: Mapping[str, int] | None = None,
    global_running: int = 0,
    max_global_concurrent: int | None = None,
    explicit_override: bool = False,
    reserve_override: bool = False,
    binding_pool_id: str | None = None,
    activate_overage: bool = True,
    training_allowed: bool = True,
    training_required: str | None = None,
    routing_snapshot: Any | None = None,
    prompt_bytes: int | None = None,
    requires_governed_instructions: bool = False,
) -> AdmissionDecision:
    """Canonical admission check for one candidate route.

    Enforces:
    1. Identity validity
    2. Requirements: role, capabilities, read_only, model_family include/exclude
    3. Provider & training governance
    4. Readiness / adapter availability
    5. Governed instructions compatibility
    6. Billing posture (unattended A/C with non-expired TTL; B/D override; X/fail closed)
    7. Concurrency limits (global and per-pool)
    8. Quota pools & windows (mode != OFF, exclusions, overage freshness, reserves, red-line)
    9. Scoring across windows (computes pace and binding window)
    """
    moment = _utcnow(now)

    # 1. Identity validity
    if not isinstance(candidate, WorkerCandidate) or not candidate.provider or not candidate.model:
        return AdmissionDecision(
            admitted=False,
            candidate=candidate,
            reason="INVALID_CANDIDATE",
        )

    # 2. Requirements: role, capabilities, read_only, model_family
    if requirements is not None:
        cand_role = getattr(candidate, "role", None)
        if cand_role is not None and cand_role != requirements.role:
            return AdmissionDecision(
                admitted=False,
                candidate=candidate,
                reason="ROLE_MISMATCH",
                details={"candidate_role": cand_role, "required_role": requirements.role},
            )
        profile = capability_profile_for(adapter=adapter, provider=candidate.provider)
        have_caps = {c.value for c in profile.capabilities}

        if requirements.read_only and Capability.READ_ONLY.value not in have_caps:
            return AdmissionDecision(
                admitted=False,
                candidate=candidate,
                reason="READ_ONLY_UNSUPPORTED",
            )

        need_caps = set(normalize_required_capabilities(requirements.required_capabilities))
        if prompt_bytes is not None and adapter is not None:
            max_bytes = getattr(adapter, "max_prompt_bytes", lambda: None)()
            if max_bytes is not None and prompt_bytes > max_bytes:
                need_caps.add(Capability.LARGE_PROMPT_STDIN.value)

        missing_caps = tuple(sorted(need_caps - have_caps))
        if missing_caps:
            return AdmissionDecision(
                admitted=False,
                candidate=candidate,
                reason=REQUIRED_CAPABILITY_UNSUPPORTED,
                details={"missing_capabilities": list(missing_caps)},
            )

        if requirements.model_family_include:
            if not any(
                matches_model_family(candidate, fam)
                for fam in requirements.model_family_include
            ):
                return AdmissionDecision(
                    admitted=False,
                    candidate=candidate,
                    reason="MODEL_FAMILY_NOT_INCLUDED",
                    details={"include": list(requirements.model_family_include)},
                )

        if requirements.model_family_exclude:
            matched_fam = [
                fam
                for fam in requirements.model_family_exclude
                if matches_model_family(candidate, fam)
            ]
            if matched_fam:
                return AdmissionDecision(
                    admitted=False,
                    candidate=candidate,
                    reason="MODEL_FAMILY_EXCLUDED",
                    details={"matched_excluded": matched_fam},
                )

    # 3. Provider & training governance
    effective_policy = policy or load_control_plane_policy()
    provider_policy = effective_policy.provider(candidate.provider)
    if provider_policy.state == ProviderPolicyState.DISABLED:
        return AdmissionDecision(
            admitted=False,
            candidate=candidate,
            reason=PROVIDER_DISABLED,
        )

    if not is_training_permitted(
        candidate,
        training_allowed,
        snapshot=routing_snapshot,
        training_required=training_required,
    ):
        return AdmissionDecision(
            admitted=False,
            candidate=candidate,
            reason=TRAINING_PERMISSION_DENIED,
        )

    # 4. Readiness / adapter availability
    live_adapter = getattr(adapter, "is_live_dispatch", True)
    if adapter is not None and live_adapter and not bool(adapter.is_available()):
        return AdmissionDecision(
            admitted=False,
            candidate=candidate,
            reason=PROVIDER_UNAVAILABLE,
        )

    if readiness_state is not None:
        if not readiness_executable_available:
            return AdmissionDecision(
                admitted=False,
                candidate=candidate,
                reason=PROVIDER_EXECUTABLE_UNAVAILABLE,
            )
        if readiness_state is ProviderReadiness.NOT_READY:
            return AdmissionDecision(
                admitted=False,
                candidate=candidate,
                reason=PROVIDER_READINESS_NOT_READY,
                details={"readiness_reason": readiness_reason},
            )
        if readiness_state is ProviderReadiness.UNKNOWN and not explicit_override:
            return AdmissionDecision(
                admitted=False,
                candidate=candidate,
                reason=PROVIDER_READINESS_UNKNOWN,
                details={"readiness_reason": readiness_reason},
            )

    # 5. Governed instructions
    if requires_governed_instructions:
        compat = _instruction_compatibility_status(adapter, candidate.model)
        if compat is not None and compat not in _COMPATIBLE_INSTRUCTION_STATUSES:
            return AdmissionDecision(
                admitted=False,
                candidate=candidate,
                reason=GOVERNED_INSTRUCTIONS_UNSUPPORTED,
            )

    # 6. Billing posture (ADR-0002 D2)
    posture = posture_decision(
        candidate.provider,
        candidate.model,
        plans=effective_policy.plans,
        today=moment.date(),
    )
    if posture.posture_class == "X" or posture.reason == POSTURE_FORBIDDEN:
        return AdmissionDecision(
            admitted=False,
            candidate=candidate,
            reason=POSTURE_FORBIDDEN,
            details={"posture_class": "X"},
        )
    if posture.reason == POSTURE_EXPIRED:
        return AdmissionDecision(
            admitted=False,
            candidate=candidate,
            reason=POSTURE_EXPIRED,
            details={"expires_on": posture.expires_on},
        )
    if posture.reason == POSTURE_UNVERIFIED:
        return AdmissionDecision(
            admitted=False,
            candidate=candidate,
            reason=POSTURE_UNVERIFIED,
        )
    if posture.reason == POSTURE_ATTENDED_ONLY and not explicit_override:
        return AdmissionDecision(
            admitted=False,
            candidate=candidate,
            reason=POSTURE_ATTENDED_ONLY,
            details={"posture_class": "B"},
        )
    if posture.reason == POSTURE_METERED and not explicit_override:
        return AdmissionDecision(
            admitted=False,
            candidate=candidate,
            reason=POSTURE_METERED,
            details={"posture_class": "D"},
        )

    # 7. Global concurrency
    if max_global_concurrent is not None and global_running >= max_global_concurrent:
        return AdmissionDecision(
            admitted=False,
            candidate=candidate,
            reason="GLOBAL_CONCURRENCY_LIMIT",
            details={"global_running": global_running, "limit": max_global_concurrent},
        )

    # 8. Quota pools & windows
    pool_policies = effective_quota_pools(effective_policy, activate_overage=activate_overage)
    drawn_pools = pools_for(candidate, pool_policies)
    if binding_pool_id is not None:
        drawn_pools = [binding_pool_id] if binding_pool_id in pool_policies else drawn_pools

    states = list(quota_states)

    # First check: active exclusions across drawn pools
    for state in states:
        exc_id = getattr(state, "exclusion_id", None)
        if not exc_id:
            continue
        exc_pool = getattr(state, "pool_id", None) or state.pool
        if exc_pool not in drawn_pools and exc_pool != candidate.provider.lower():
            continue
        scope = getattr(state, "exclusion_model_scope", None)
        if not model_scope_matches(scope if isinstance(scope, str) else None, candidate.model):
            continue
        until_raw = getattr(state, "exclusion_until", None) or state.reset_at
        until_dt = parse_iso(until_raw) if isinstance(until_raw, str) else None
        confidence = getattr(state, "until_confidence", "PARSED") or "PARSED"
        return AdmissionDecision(
            admitted=False,
            candidate=candidate,
            reason=QUOTA_EXCLUDED_UNTIL_RESET,
            pool_id=exc_pool,
            reset_at=until_raw if isinstance(until_raw, str) else None,
            eta=until_dt,
            wait_confidence=confidence,
        )

    # Check each drawn pool
    window_scores: list[tuple[float, str, str, float, float, float, float, float]] = []

    for pool_id in drawn_pools:
        pool_pol = pool_policies.get(pool_id, QuotaPoolPolicy(pool_id=pool_id))
        if pool_pol.mode == QuotaPoolMode.OFF:
            return AdmissionDecision(
                admitted=False,
                candidate=candidate,
                reason=POOL_OFF,
                pool_id=pool_id,
            )

        plan = effective_policy.plans.pool_plans.get(pool_id)
        if plan is not None and plan.max_concurrent is not None and running_counts is not None:
            current_count = running_counts.get(pool_id, 0)
            if current_count >= plan.max_concurrent:
                return AdmissionDecision(
                    admitted=False,
                    candidate=candidate,
                    reason="CONCURRENCY_LIMIT",
                    pool_id=pool_id,
                    details={
                        "pool": pool_id,
                        "running": current_count,
                        "limit": plan.max_concurrent,
                    },
                )

        # Multiplier from pool plan members
        multiplier = 1.0
        if plan is not None and plan.members:
            for member in plan.members:
                if _member_matches(member.route, candidate):
                    multiplier = float(member.multiplier)
                    break

        # Windows to evaluate
        windows = plan.windows if plan is not None and plan.windows else (Window(id="5h"),)

        for win in windows:
            # Find matching quota observation for this pool and window
            matched_state: QuotaState | None = None
            for s in states:
                if getattr(s, "exclusion_id", None):
                    continue
                s_pool = getattr(s, "pool_id", None) or s.pool
                matches_provider_pool = (
                    pool_id == candidate.provider.lower()
                    and s.provider.lower() == pool_id
                )
                if s_pool == pool_id or matches_provider_pool:
                    s_win = getattr(s, "window_id", None) or getattr(s, "window", None) or "5h"
                    if s_win == win.id or s_win == "ALL":
                        matched_state = s
                        if s_win == win.id:
                            break

            # Check D4 overage rule
            if pool_pol.overage_possible:
                is_stale_or_missing = False
                if matched_state is None:
                    is_stale_or_missing = True
                else:
                    freshness_status = str(getattr(matched_state, "freshness", "") or "").upper()
                    rem_raw = getattr(matched_state, "normalized_remaining_pct", None)
                    if rem_raw is None:
                        rem_raw = getattr(matched_state, "remaining_percent", None)
                    if freshness_status in {"STALE", "UNKNOWN"} or rem_raw is None:
                        is_stale_or_missing = True

                if is_stale_or_missing:
                    # Missing/stale evidence on overage pool requires probe/wait
                    probe_harness = _POOL_TO_HARNESS.get(pool_id)
                    can_probe = probe_harness in _KNOWN_PROBE_HARNESSES
                    return AdmissionDecision(
                        admitted=False,
                        candidate=candidate,
                        reason=QUOTA_EVIDENCE_REQUIRED,
                        pool_id=pool_id,
                        binding_window=win.id,
                        needs_probe=can_probe,
                        probe_harness=probe_harness if can_probe else None,
                        red_line_pct=pool_pol.red_line_pct,
                    )

            # Compute remaining percent
            remaining: float | None = None
            reset_at_str: str | None = None
            if matched_state is not None:
                norm = getattr(matched_state, "normalized_remaining_pct", None)
                try:
                    remaining = (
                        float(norm) if norm is not None
                        else (
                            float(matched_state.remaining_percent)
                            if matched_state.remaining_percent is not None
                            else None
                        )
                    )
                except (TypeError, ValueError):
                    remaining = None
                reset_at_str = matched_state.reset_at

            if remaining is None:
                remaining = 100.0

            # Cost estimate
            cost_pct = _get_cost_estimate(candidate, pool_id, win.id, size_bucket, cost_states)
            projected_cost = cost_pct * multiplier

            # Time-of-day reserve
            res_val = reserve_pct(pool_id, win.id, moment, effective_policy)
            if reserve_override and pool_pol.mode == QuotaPoolMode.RESERVE:
                res_val = 0.0

            # Red line check
            effective_rem = remaining - projected_cost - res_val
            red_line = pool_pol.red_line_pct

            if pool_pol.mode != QuotaPoolMode.UNENFORCED and effective_rem < red_line:
                reason = _red_line_reason(win.id)
                eta_dt = parse_iso(reset_at_str) if reset_at_str else None
                return AdmissionDecision(
                    admitted=False,
                    candidate=candidate,
                    reason=reason,
                    pool_id=pool_id,
                    binding_window=win.id,
                    reset_at=reset_at_str,
                    eta=eta_dt,
                    wait_confidence="PARSED" if eta_dt else "CONSERVATIVE",
                    cost_estimate=projected_cost,
                    remaining_pct=remaining,
                    red_line_pct=red_line,
                    reserve_pct=res_val,
                    multiplier=multiplier,
                )

            # Score this window
            fallback_h = FALLBACK_RESET_HOURS.get(win.id, 24.0)
            hours_to_reset = calculate_hours_to_reset(
                reset_at_str, moment, fallback_hours=fallback_h
            )
            pace = score(remaining, red_line, res_val, projected_cost, hours_to_reset)
            window_scores.append((
                pace,
                pool_id,
                win.id,
                remaining,
                red_line,
                res_val,
                projected_cost,
                multiplier,
            ))

    # All pools and windows admitted! Score is the minimum pace across all windows
    if not window_scores:
        return AdmissionDecision(
            admitted=True,
            candidate=candidate,
            score=100.0,
            remaining_pct=100.0,
            red_line_pct=15.0,
            reserve_pct=0.0,
            cost_estimate=5.0,
            multiplier=1.0,
        )

    # Minimum pace binds
    min_entry = min(window_scores, key=lambda item: item[0])
    (
        binding_pace,
        binding_pool,
        binding_window,
        bind_rem,
        bind_red,
        bind_res,
        bind_cost,
        bind_mult,
    ) = min_entry

    return AdmissionDecision(
        admitted=True,
        candidate=candidate,
        score=binding_pace,
        pool_id=binding_pool,
        binding_window=binding_window,
        remaining_pct=bind_rem,
        red_line_pct=bind_red,
        reserve_pct=bind_res,
        cost_estimate=bind_cost,
        multiplier=bind_mult,
    )


__all__ = [
    "AdmissionDecision",
    "admit",
    "calculate_hours_to_reset",
    "matches_model_family",
    "reserve_pct",
    "score",
]
