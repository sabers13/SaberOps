"""Deterministic quota-aware routing policy.

Quota state is owner-supplied and persisted in SQLite. It is applied only to
automatically resolved candidate chains, after the ordinary deterministic chain
has been resolved. Explicit model overrides bypass quota policy entirely.
"""

from __future__ import annotations

import math
from typing import Any, cast

from saberops.models import QuotaRawSemantics, QuotaState, RoutingState, WorkerCandidate

QUOTA_THRESHOLD_PERCENT = 25.0
CODEX_PROVIDER = "codex"
CODEX_WEEKLY_POOL = "weekly"


def apply_c07_quota_to_chain(
    candidates: list[WorkerCandidate],
    quota_states: list[QuotaState],
    *,
    reserve_override: bool = False,
) -> list[WorkerCandidate]:
    """Apply the C07 shared-pool red-line policy without ranking survivors.

    The older helpers below remain source-compatible for C01-C06 callers;
    actual C07 dispatch paths use this function and the preflight evaluator.
    """
    from saberops.control_plane import quota_decision

    return [
        candidate
        for candidate in candidates
        if quota_decision(
            candidate, quota_states, reserve_override=reserve_override
        ).eligible
    ]


def derive_routing_state(
    provider: str,
    pool: str,
    remaining_percent: float,
    current_state: RoutingState | None,
) -> RoutingState:
    """Derive the routing state for a quota update.

    Only a Codex ``weekly`` pool below the 25% threshold produces BLOCKED
    routing. A low Codex row for any other pool does not block Codex, and any
    other provider below the threshold is DEMOTED (moved behind NORMAL
    providers).

    At or above the threshold, providers return to NORMAL except a sticky
    Codex ``weekly`` BLOCK, which persists until an explicit owner ``enable``.
    """
    if provider == CODEX_PROVIDER and pool != CODEX_WEEKLY_POOL:
        return RoutingState.NORMAL

    if remaining_percent < QUOTA_THRESHOLD_PERCENT:
        if provider == CODEX_PROVIDER:
            return RoutingState.BLOCKED
        return RoutingState.DEMOTED

    if current_state == RoutingState.BLOCKED:
        return RoutingState.BLOCKED
    return RoutingState.NORMAL


def apply_quota_policy(
    candidates: list[WorkerCandidate],
    quota_states: list[QuotaState],
) -> list[WorkerCandidate]:
    """Apply quota state to an ordered candidate chain.

    BLOCKED providers are removed entirely. DEMOTED providers keep their
    candidates but move them behind candidates from NORMAL providers while
    preserving relative order within each group.
    """
    blocked: set[str] = set()
    demoted: set[str] = set()
    for state in quota_states:
        if state.routing_state == RoutingState.BLOCKED:
            if state.provider == CODEX_PROVIDER and state.pool != CODEX_WEEKLY_POOL:
                continue
            blocked.add(state.provider)
        elif state.routing_state == RoutingState.DEMOTED:
            demoted.add(state.provider)

    normal: list[WorkerCandidate] = []
    lowered: list[WorkerCandidate] = []
    for candidate in candidates:
        if candidate.provider in blocked:
            continue
        if candidate.provider in demoted:
            lowered.append(candidate)
        else:
            normal.append(candidate)
    return normal + lowered


def apply_quota_to_chain(
    candidates: list[WorkerCandidate],
    quota_states: list[QuotaState],
    model_override: str | None = None,
    provider_override: str | None = None,
) -> list[WorkerCandidate]:
    """Apply quota policy to a resolved chain, bypassing explicit overrides.

    An explicit model or provider override is an intentional owner routing
    decision and is not silently rewritten by automatic quota policy.

    Raises:
        ValueError: If quota policy removes every automatic candidate.
    """
    if model_override or provider_override:
        return candidates

    effective = apply_quota_policy(candidates, quota_states)
    if not effective:
        raise ValueError(
            "Quota policy blocked every automatic candidate for this tier. "
            "Use an explicit --model/--provider override, or re-enable a provider "
            "via 'orch quota enable'."
        )
    return effective


def normalize_quota_observation(
    raw_value: object,
    raw_semantics: QuotaRawSemantics | str,
    raw_limit: object = None,
) -> float | None:
    """Normalize one raw observation without applying routing policy.

    This lives with the quota observation vocabulary so the database write
    boundary can derive the canonical value without importing observability.
    The persisted meaning is always *remaining* percentage: 100 is unused and
    0 is exhausted.
    """
    try:
        value = float(cast(Any, raw_value)) if raw_value is not None else math.nan
        semantics = QuotaRawSemantics(str(raw_semantics))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    if semantics in (QuotaRawSemantics.REMAINING_PERCENT, QuotaRawSemantics.USED_PERCENT):
        if value > 100:
            return None
        result = (
            value
            if semantics == QuotaRawSemantics.REMAINING_PERCENT
            else 100.0 - value
        )
    else:
        try:
            limit = float(cast(Any, raw_limit)) if raw_limit is not None else math.nan
        except (TypeError, ValueError):
            return None
        if not math.isfinite(limit) or limit <= 0 or value > limit:
            return None
        result = (
            100.0 - (value / limit * 100.0)
            if semantics == QuotaRawSemantics.USED_AND_LIMIT
            else value / limit * 100.0
        )
    if not math.isfinite(result) or result < 0 or result > 100:
        return None
    return round(result, 10)
