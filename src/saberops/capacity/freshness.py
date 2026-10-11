"""Freshness computed at read time (M2-1 F)."""

from __future__ import annotations

from datetime import UTC, datetime

WINDOW_MAX_AGE_MIN: dict[str, int] = {
    "5h": 20,
    "daily": 60,
    "weekly": 180,
    "monthly": 720,
}

DEFAULT_MAX_AGE_MIN = 20
OWNER_MAX_AGE_MIN = 24 * 60


def _parse_observed(value: str | None) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _is_owner_source(source: str | None) -> bool:
    if not source:
        return True
    low = source.strip().lower()
    if not low:
        return True
    if "probe" in low or "exhaustion" in low or "plan-exhaustion" in low:
        return False
    return True


def max_age_minutes(window_id: str | None, source: str | None) -> int:
    """Return max fresh age in minutes for a window/source."""
    key = (window_id or "").strip().lower()
    base = WINDOW_MAX_AGE_MIN.get(key, DEFAULT_MAX_AGE_MIN)
    if _is_owner_source(source):
        return max(base, OWNER_MAX_AGE_MIN)
    return base


def freshness(
    observed_at: str | None,
    window_id: str | None,
    source: str | None,
    now: datetime | None = None,
) -> str:
    """Return FRESH if age <= max_age else STALE (never UNKNOWN here)."""
    moment = now.astimezone(UTC) if now is not None and now.tzinfo else (
        now.replace(tzinfo=UTC) if now is not None else datetime.now(UTC)
    )
    observed = _parse_observed(observed_at)
    if observed is None:
        return "STALE"
    from datetime import timedelta

    age_min = (moment - observed).total_seconds() / 60.0
    if age_min < 0:
        return "FRESH"
    return "FRESH" if age_min <= max_age_minutes(window_id, source) else "STALE"


__all__ = [
    "WINDOW_MAX_AGE_MIN",
    "DEFAULT_MAX_AGE_MIN",
    "OWNER_MAX_AGE_MIN",
    "max_age_minutes",
    "freshness",
]
