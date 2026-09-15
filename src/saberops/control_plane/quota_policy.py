"""Canonical C07 quota-pool red-line policy."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from saberops.models import QuotaState, WorkerCandidate


class QuotaPoolMode(StrEnum):
    STRICT = "STRICT"
    RESERVE = "RESERVE"
    DISABLED = "DISABLED"


@dataclass(frozen=True)
class QuotaPoolPolicy:
    pool_id: str
    mode: QuotaPoolMode = QuotaPoolMode.STRICT
    red_line_pct: float = 15.0
    members: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not 0 <= self.red_line_pct <= 100:
            raise ValueError("red_line_pct must be between 0 and 100")
        if not self.pool_id.strip():
            raise ValueError("pool_id must not be empty")


@dataclass(frozen=True)
class QuotaDecision:
    eligible: bool
    pool_id: str | None
    normalized_remaining_pct: float | None
    red_line_pct: float
    mode: QuotaPoolMode
    reason: str | None = None
    reset_at: str | None = None


def _candidate_ids(candidate: WorkerCandidate) -> tuple[str, str, str]:
    return (
        f"{candidate.provider}/{candidate.model}".lower(),
        candidate.model.lower(),
        candidate.provider.lower(),
    )


def _matches(state: QuotaState, candidate: WorkerCandidate, policy: QuotaPoolPolicy) -> bool:
    ids = _candidate_ids(candidate)
    state_pool = getattr(state, "pool_id", None) or state.pool
    if state_pool != policy.pool_id:
        return False
    # Legacy provider/pool rows are provider-scoped. A configured shared pool
    # declares members explicitly, which is the only case where one row may
    # govern candidates from more than one provider.
    if policy.members:
        return any(member.lower() in ids for member in policy.members)
    return state.provider.lower() == candidate.provider.lower()


def quota_decision(
    candidate: WorkerCandidate,
    quota_states: Iterable[QuotaState],
    *,
    pool_policies: dict[str, QuotaPoolPolicy] | None = None,
    reserve_override: bool = False,
    pool_id: str | None = None,
) -> QuotaDecision:
    """Evaluate a candidate against its true shared pool.

    Unknown and stale observations are non-authoritative and therefore do not
    exclude a candidate. Explicit ordinary provider/model overrides are not
    represented by ``reserve_override`` and cannot bypass this decision.

    ``pool_id`` is the *binding-scoped filter*, not a piece of evidence:

    * When ``pool_id`` is supplied (binding-scoped path), only quota
      observations whose canonical ``pool_id`` matches are considered.
      Hard quota policy for that pool must come **only** from the
      supplied ``pool_policies``: a missing canonical policy means the
      binding has no enforced quota policy for this pool and the state
      is *not* evaluated (the binding does not get a synthesised
      default STRICT / 15% policy).
    * When ``pool_id`` is ``None`` (legacy path), every supplied state
      is considered and the existing canonical-or-default policy
      matching applies; this preserves the documented legacy default
      behaviour.

    The returned :attr:`QuotaDecision.pool_id` is always the
    **matched** state's canonical ``pool_id`` (or ``None`` only when
    no state matched), so legacy C07 evidence is preserved verbatim
    for both legacy and binding-scoped callers.
    """
    policies = pool_policies or {}
    for state in quota_states:
        state_pool = getattr(state, "pool_id", None) or state.pool
        if pool_id is not None and state_pool != pool_id:
            continue
        if pool_id is not None:
            # Binding-scoped: hard policy MUST come from the canonical
            # C07 ``ControlPlanePolicy``.  Synthesising a default
            # STRICT / 15% policy when no canonical policy is declared
            # would silently invent red-line enforcement that the
            # owner never authorised for this binding's pool.
            policy = policies.get(state_pool)
            if policy is None:
                continue
        else:
            # Legacy: keep the existing default synthesis for states
            # whose pool is not present in the supplied canonical
            # policies.  This preserves the documented legacy
            # behaviour; the synthesised policy remains a state-level
            # policy, never a binding-level fabrication.
            policy = policies.get(state_pool, QuotaPoolPolicy(pool_id=state_pool))
        if not _matches(state, candidate, policy):
            continue
        freshness = str(getattr(state, "freshness", "") or "").upper()
        normalized = getattr(state, "normalized_remaining_pct", None)
        remaining = normalized if normalized is not None else state.remaining_percent
        if not policies and getattr(state, "mode", None):
            policy = QuotaPoolPolicy(
                state_pool,
                QuotaPoolMode(str(state.mode)),
                float(getattr(state, "red_line_pct", None) or 15.0),
            )
        if freshness in {"STALE", "UNKNOWN"} or remaining is None:
            return QuotaDecision(
                True,
                state_pool,
                None,
                policy.red_line_pct,
                policy.mode,
                reset_at=state.reset_at,
            )
        if policy.mode == QuotaPoolMode.DISABLED:
            return QuotaDecision(
                True,
                state_pool,
                float(remaining),
                policy.red_line_pct,
                policy.mode,
                reset_at=state.reset_at,
            )
        below = float(remaining) < policy.red_line_pct
        if below and (policy.mode == QuotaPoolMode.STRICT or not reserve_override):
            return QuotaDecision(
                False,
                state_pool,
                float(remaining),
                policy.red_line_pct,
                policy.mode,
                "WEEKLY_QUOTA_RED_LINE",
                state.reset_at,
            )
        return QuotaDecision(
            True,
            state_pool,
            float(remaining),
            policy.red_line_pct,
            policy.mode,
            reset_at=state.reset_at,
        )
    return QuotaDecision(True, None, None, 15.0, QuotaPoolMode.DISABLED)
