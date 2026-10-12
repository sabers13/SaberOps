"""Pure exclusion rules with an injectable clock (M2-1 A).

Exclusion dict keys (as stored in ``capacity_exclusions``):
``id, pool_id, window_id, model_scope, reason, until, until_confidence,
source, evidence_ref, run_id, created_at, next_probe_at, probe_attempts,
cleared_at, cleared_by``. All timestamps are ISO-8601 UTC strings.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

CONFIDENCE_RANK: dict[str, int] = {
    "UNKNOWN": 0,
    "HINT": 1,
    "PARSED": 2,
    "EXACT": 3,
}

WINDOW_DURATION_MIN: dict[str, int] = {
    "5h": 300,
    "daily": 1440,
    "weekly": 10080,
    "monthly": 43200,
}

SKEW = timedelta(minutes=2)


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp to aware UTC, else None."""
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def window_cap_min(window_id: str) -> int:
    """Cap for UNKNOWN backoff: min(window duration, 6h)."""
    duration = WINDOW_DURATION_MIN.get((window_id or "").lower())
    if duration is None:
        return 360
    return min(duration, 360)


def backoff_next_probe(
    created_at: datetime, attempts: int, window_id: str
) -> datetime:
    """UNKNOWN backoff: created + 15min * 2^attempts, capped."""
    delay_min = 15 * (2**max(0, attempts))
    delay_min = min(delay_min, window_cap_min(window_id))
    return created_at + timedelta(minutes=delay_min)


def exclusion_state(exc: dict[str, Any], now: datetime) -> str:
    """Return ACTIVE | EXPIRED | PROBE_DUE | CLEARED for one exclusion row."""
    if exc.get("cleared_at"):
        return "CLEARED"
    confidence = str(exc.get("until_confidence") or "UNKNOWN").upper()
    until = parse_iso(exc.get("until"))
    now_utc = now.astimezone(UTC) if now.tzinfo else now.replace(tzinfo=UTC)
    if confidence in ("EXACT", "PARSED"):
        if until is None:
            return "PROBE_DUE"
        return "ACTIVE" if now_utc < until + SKEW else "EXPIRED"
    if confidence == "HINT":
        if until is None:
            nxt = parse_iso(exc.get("next_probe_at"))
            if nxt is not None and now_utc < nxt:
                return "ACTIVE"
            return "PROBE_DUE"
        return "ACTIVE" if now_utc < until else "PROBE_DUE"
    # UNKNOWN
    nxt = parse_iso(exc.get("next_probe_at"))
    if nxt is not None:
        return "ACTIVE" if now_utc < nxt else "PROBE_DUE"
    created = parse_iso(exc.get("created_at"))
    if created is None:
        return "PROBE_DUE"
    computed = backoff_next_probe(
        created, int(exc.get("probe_attempts") or 0), str(exc.get("window_id") or "")
    )
    return "ACTIVE" if now_utc < computed else "PROBE_DUE"


def is_excluding(exc: dict[str, Any], now: datetime) -> bool:
    """True when the exclusion denies (ACTIVE or PROBE_DUE)."""
    return exclusion_state(exc, now) in ("ACTIVE", "PROBE_DUE")


def merge_wins(old: dict[str, Any], new: dict[str, Any]) -> bool:
    """Return True when the new exclusion replaces the old one.

    Higher confidence wins (EXACT > PARSED > HINT > UNKNOWN); at equal
    confidence the later ``until`` wins (None sorts before any time).
    """
    old_rank = CONFIDENCE_RANK.get(str(old.get("until_confidence") or "UNKNOWN").upper(), 0)
    new_rank = CONFIDENCE_RANK.get(str(new.get("until_confidence") or "UNKNOWN").upper(), 0)
    if new_rank != old_rank:
        return new_rank > old_rank
    old_until = parse_iso(old.get("until"))
    new_until = parse_iso(new.get("until"))
    if old_until is None and new_until is None:
        # Later creation wins on full tie.
        return str(new.get("created_at") or "") >= str(old.get("created_at") or "")
    if new_until is None:
        return False
    if old_until is None:
        return True
    return new_until >= old_until


def model_scope_matches(exc_model_scope: str | None, candidate_model: str) -> bool:
    """Case-insensitive substring match; None/empty scope matches all."""
    if not exc_model_scope:
        return True
    return exc_model_scope.lower() in (candidate_model or "").lower()


__all__ = [
    "CONFIDENCE_RANK",
    "WINDOW_DURATION_MIN",
    "SKEW",
    "parse_iso",
    "window_cap_min",
    "backoff_next_probe",
    "exclusion_state",
    "is_excluding",
    "merge_wins",
    "model_scope_matches",
]
