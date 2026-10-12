"""Canonical quota-pool red-line policy (M2-1: T-005 + exclusions)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from saberops.models import QuotaState, WorkerCandidate


class QuotaPoolMode(StrEnum):
    STRICT = "STRICT"
    RESERVE = "RESERVE"
    UNENFORCED = "UNENFORCED"
    OFF = "OFF"
    # Deprecated alias: attribute access resolves to UNENFORCED while the
    # string "DISABLED" still parses via _missing_.
    DISABLED = "UNENFORCED"

    @classmethod
    def _missing_(cls, value: object) -> QuotaPoolMode | None:
        if isinstance(value, str) and value.strip().upper() == "DISABLED":
            return cls.UNENFORCED
        if isinstance(value, str):
            cleaned = value.strip().upper()
            for member in cls:
                if member.value == cleaned and member is not cls.DISABLED:
                    return member
        return None


def _normalize_mode(value: object) -> QuotaPoolMode:
    if isinstance(value, QuotaPoolMode):
        if value is QuotaPoolMode.DISABLED:
            return QuotaPoolMode.UNENFORCED
        return value
    if isinstance(value, str):
        cleaned = value.strip().upper()
        if cleaned == "DISABLED":
            return QuotaPoolMode.UNENFORCED
        try:
            mode = QuotaPoolMode(cleaned)
        except ValueError:
            return QuotaPoolMode.UNENFORCED
        if mode is QuotaPoolMode.DISABLED:
            return QuotaPoolMode.UNENFORCED
        return mode
    return QuotaPoolMode.STRICT


#: Deprecated alias kept importable (T-005 d).
WEEKLY_QUOTA_RED_LINE = "RED_LINE_WEEKLY"

QUOTA_EXCLUDED_UNTIL_RESET = "QUOTA_EXCLUDED_UNTIL_RESET"
QUOTA_EVIDENCE_REQUIRED = "QUOTA_EVIDENCE_REQUIRED"
POOL_OFF = "POOL_OFF"


def _red_line_reason(pool_label: str | None) -> str:
    label = (pool_label or "").strip().lower()
    if label in ("5h", "five_hour", "five-hour", "session", "5-hour"):
        return "RED_LINE_5H"
    if label == "daily":
        return "RED_LINE_DAILY"
    if label == "weekly":
        return "RED_LINE_WEEKLY"
    if label == "monthly":
        return "RED_LINE_MONTHLY"
    return "RED_LINE"


@dataclass(frozen=True)
class QuotaPoolPolicy:
    pool_id: str
    mode: QuotaPoolMode = QuotaPoolMode.STRICT
    red_line_pct: float = 15.0
    members: tuple[str, ...] = ()
    overage_possible: bool = False

    def __post_init__(self) -> None:
        if not 0 <= self.red_line_pct <= 100:
            raise ValueError("red_line_pct must be between 0 and 100")
        if not self.pool_id.strip():
            raise ValueError("pool_id must not be empty")
        normalized = _normalize_mode(self.mode)
        object.__setattr__(self, "mode", normalized)
        object.__setattr__(self, "members", tuple(self.members))


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


def _member_matches(member: str, candidate: WorkerCandidate) -> bool:
    text = member.strip().lower()
    provider = candidate.provider.strip().lower()
    route = f"{candidate.provider}/{candidate.model}".strip().lower()
    if text.endswith("/*"):
        prefix = text[:-2]
        if "/" not in prefix:
            return False
        # Trailing glob matches only at a slash boundary (fix-up 2: the old
        # prefix-without-slash fallback wrongly matched e.g.
        # `opencode/opencode-go/*` against `opencode/opencode-go-extra/m`).
        return route == prefix or route.startswith(prefix + "/")
    return text in (
        route,
        candidate.model.strip().lower(),
        provider,
    )


def _matches(state: QuotaState, candidate: WorkerCandidate, policy: QuotaPoolPolicy) -> bool:
    state_pool = getattr(state, "pool_id", None) or state.pool
    if state_pool != policy.pool_id:
        return False
    # Legacy provider/pool rows are provider-scoped. A configured shared pool
    # declares members explicitly, which is the only case where one row may
    # govern candidates from more than one provider.
    members: tuple[str | dict[str, object], ...] = tuple(policy.members or ())
    # Synthesized exclusion states carry pool identity in pool_id; match them
    # through the same member grammar (provider-scoped default handled by pools_for).
    if members:
        raw_members: list[str] = []
        for member in members:
            if isinstance(member, dict):
                route = member.get("route")
                if isinstance(route, str):
                    raw_members.append(route)
            elif isinstance(member, str):
                raw_members.append(member)
        return any(_member_matches(member, candidate) for member in raw_members)
    # No members: provider-scoped (legacy) semantics.
    exclusion_pool = getattr(state, "pool_id", None)
    if exclusion_pool and getattr(state, "exclusion_id", None):
        # Synthesized exclusion without a member list: it belongs to the
        # provider-scoped default pool, which matches only its provider.
        return state.provider.lower() == candidate.provider.lower()
    return state.provider.lower() == candidate.provider.lower()


def pools_for(candidate: WorkerCandidate, policies: dict[str, QuotaPoolPolicy] | None) -> list[str]:
    """Return every pool id whose members match the candidate, else [provider]."""
    if not policies:
        return [candidate.provider.strip().lower()]
    matched: list[str] = []
    for pool_id, policy in policies.items():
        members = policy.members or ()
        hit = False
        for member in members:
            sel = (
                member.get("route") if isinstance(member, dict) else member
            )
            if not isinstance(sel, str):
                continue
            if _member_matches(sel, candidate):
                hit = True
                break
        if hit:
            matched.append(pool_id)
    if matched:
        return matched
    return [candidate.provider.strip().lower()]


def _utcnow(now: datetime | None) -> datetime:
    if now is not None:
        return now.astimezone(UTC) if now.tzinfo else now.replace(tzinfo=UTC)
    return datetime.now(UTC)


def quota_decision(
    candidate: WorkerCandidate,
    quota_states: Iterable[QuotaState],
    *,
    pool_policies: dict[str, QuotaPoolPolicy] | None = None,
    reserve_override: bool = False,
    pool_id: str | None = None,
    now: datetime | None = None,
) -> QuotaDecision:
    """Evaluate a candidate against its true shared pool.

    Exclusions deny first in every mode (including UNENFORCED); only OFF
    also denies unconditionally. ``reserve_override`` bypasses only a
    RESERVE red line -- never OFF, an active exclusion, or
    QUOTA_EVIDENCE_REQUIRED. Explicit owner overrides reach this function
    through preflight's candidate path (they do not bypass it); audit
    bypasses are separate read paths, not this decision.

    ``pool_id`` is the *binding-scoped filter*, not a piece of evidence.
    ``now`` is an injectable clock for tests.
    """
    from saberops.capacity.exclusions import model_scope_matches, parse_iso

    policies: dict[str, QuotaPoolPolicy] = dict(pool_policies or {})
    moment = _utcnow(now)
    states = list(quota_states)

    # Exclusions first: any active or PROBE_DUE exclusion on a pool the
    # candidate draws denies with QUOTA_EXCLUDED_UNTIL_RESET.
    drawn = pools_for(candidate, policies if policies else None)
    best_exclusion_until: str | None = None
    best_exclusion_dt: datetime | None = None
    excluded = False
    for state in states:
        exc_id = getattr(state, "exclusion_id", None)
        if not exc_id:
            continue
        exc_pool = getattr(state, "pool_id", None) or state.pool
        # Binding-scoped callers only see their own pool's exclusions, so a
        # sibling pool can neither block nor authorize the selected candidate.
        if pool_id is not None and exc_pool != pool_id:
            continue
        if pool_id is None and exc_pool not in drawn:
            # Provider-scoped fallback: synthesized default pools are named
            # for the provider; also honor direct pool match.
            if not (exc_pool == candidate.provider.strip().lower() and exc_pool in drawn):
                continue
        scope = getattr(state, "exclusion_model_scope", None)
        if not model_scope_matches(scope if isinstance(scope, str) else None, candidate.model):
            continue
        excluded = True
        until_raw = getattr(state, "exclusion_until", None) or state.reset_at
        if isinstance(until_raw, str) and until_raw:
            best_exclusion_until = until_raw
            parsed = parse_iso(until_raw)
            if parsed is not None and (best_exclusion_dt is None or parsed > best_exclusion_dt):
                best_exclusion_dt = parsed
                best_exclusion_until = until_raw
    if excluded:
        mode = QuotaPoolMode.UNENFORCED
        # Report the governing pool's mode when known (OFF stays OFF).
        scope_pools = [pool_id] if pool_id is not None else drawn
        for pid in scope_pools:
            policy = policies.get(pid)
            if policy is not None and policy.mode == QuotaPoolMode.OFF:
                mode = QuotaPoolMode.OFF
                break
        return QuotaDecision(
            False, scope_pools[0] if scope_pools else None, 0.0, 15.0, mode,
            QUOTA_EXCLUDED_UNTIL_RESET, best_exclusion_until,
        )

    # Collect matching red-line states (all windows; most restrictive wins).
    matched: list[tuple[QuotaState, QuotaPoolPolicy, float]] = []
    no_match_policies: list[QuotaPoolPolicy] = []
    # For the no-matching-state rule we need the matched policy's
    # overage_possible: the policies of pools the candidate draws.
    drawn_policies = [policies[pid] for pid in drawn if pid in policies]
    for state in states:
        if getattr(state, "exclusion_id", None):
            continue
        state_pool = getattr(state, "pool_id", None) or state.pool
        if pool_id is not None and state_pool != pool_id:
            continue
        if pool_id is not None:
            policy = policies.get(state_pool)
            if policy is None:
                continue
        else:
            policy = policies.get(state_pool, QuotaPoolPolicy(pool_id=state_pool))
        if not _matches(state, candidate, policy):
            continue
        policy = QuotaPoolPolicy(
            policy.pool_id, _normalize_mode(policy.mode),
            policy.red_line_pct, policy.members, policy.overage_possible,
        )
        # OFF denies immediately (nothing bypasses it).
        if policy.mode == QuotaPoolMode.OFF:
            return QuotaDecision(
                False, state_pool, None, policy.red_line_pct,
                policy.mode, POOL_OFF, state.reset_at,
            )
        freshness = str(getattr(state, "freshness", "") or "").upper()
        normalized = getattr(state, "normalized_remaining_pct", None)
        try:
            remaining: float | None = (
                float(normalized) if normalized is not None
                else (
                    float(state.remaining_percent)
                    if state.remaining_percent is not None
                    else None
                )
            )
        except (TypeError, ValueError):
            remaining = None
        if not policies and getattr(state, "mode", None):
            try:
                policy = QuotaPoolPolicy(
                    state_pool,
                    _normalize_mode(str(state.mode)),
                    float(getattr(state, "red_line_pct", None) or 15.0),
                )
            except ValueError:
                pass
        if freshness in {"STALE", "UNKNOWN"} or remaining is None:
            if policy.overage_possible:
                return QuotaDecision(
                    False, state_pool, None, policy.red_line_pct,
                    policy.mode, QUOTA_EVIDENCE_REQUIRED, state.reset_at,
                )
            # Non-authoritative evidence does not exclude; keep looking at
            # other windows (a FRESH restrictive window still governs).
            no_match_policies.append(policy)
            continue
        matched.append((state, policy, float(remaining)))

    if matched:
        # Most restrictive (minimum remaining) decides; its reset governs.
        matched.sort(key=lambda item: item[2])
        state, policy, remaining = matched[0]
        if policy.mode == QuotaPoolMode.UNENFORCED:
            return QuotaDecision(
                True, getattr(state, "pool_id", None) or state.pool,
                remaining, policy.red_line_pct, policy.mode,
                reset_at=state.reset_at,
            )
        below = remaining < policy.red_line_pct
        if below:
            if policy.mode == QuotaPoolMode.RESERVE and reserve_override:
                return QuotaDecision(
                    True, getattr(state, "pool_id", None) or state.pool,
                    remaining, policy.red_line_pct, policy.mode,
                    reset_at=state.reset_at,
                )
            return QuotaDecision(
                False, getattr(state, "pool_id", None) or state.pool,
                remaining, policy.red_line_pct, policy.mode,
                _red_line_reason(state.pool), state.reset_at,
            )
        return QuotaDecision(
            True, getattr(state, "pool_id", None) or state.pool,
            remaining, policy.red_line_pct, policy.mode,
            reset_at=state.reset_at,
        )

    # No matching state: overage_possible True on any drawn pool denies.
    check_policies = drawn_policies or []
    if any(p.overage_possible for p in check_policies):
        first = (
            check_policies[0]
            if check_policies
            else QuotaPoolPolicy(pool_id=drawn[0] if drawn else "")
        )
        return QuotaDecision(
            False, first.pool_id if check_policies else (drawn[0] if drawn else None),
            None, first.red_line_pct if check_policies else 15.0,
            first.mode if check_policies else QuotaPoolMode.UNENFORCED,
            QUOTA_EVIDENCE_REQUIRED, None,
        )
    # Also handle binding-scoped pools with overage via no_match_policies.
    if any(p.overage_possible for p in no_match_policies):
        first = next(p for p in no_match_policies if p.overage_possible)
        return QuotaDecision(
            False, first.pool_id, None, first.red_line_pct,
            first.mode, QUOTA_EVIDENCE_REQUIRED, None,
        )
    _ = moment
    return QuotaDecision(True, None, None, 15.0, QuotaPoolMode.UNENFORCED)


__all__ = [
    "POOL_OFF",
    "QUOTA_EVIDENCE_REQUIRED",
    "QUOTA_EXCLUDED_UNTIL_RESET",
    "WEEKLY_QUOTA_RED_LINE",
    "QuotaDecision",
    "QuotaPoolMode",
    "QuotaPoolPolicy",
    "pools_for",
    "quota_decision",
]
