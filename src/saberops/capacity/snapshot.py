"""Routing seam: effective quota states with expiry + exclusions (M2-1 F)."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from saberops.capacity.exclusions import is_excluding, model_scope_matches, parse_iso
from saberops.capacity.freshness import freshness
from saberops.capacity.store import ensure_tables, list_active_exclusions

if TYPE_CHECKING:
    from saberops.db import Database
else:  # pragma: no cover - typing only
    Database = Any  # type: ignore[assignment,misc]


def _utcnow(now: datetime | None) -> datetime:
    if now is not None:
        return now.astimezone(UTC) if now.tzinfo else now.replace(tzinfo=UTC)
    return datetime.now(UTC)


def _legacy_exclusion_from_row(row: Any, now: datetime) -> dict | None:
    """Read (not migrate) a remaining-0 quota_state row as an exclusion."""
    try:
        remaining = float(row.remaining_percent)
    except (TypeError, ValueError):
        return None
    if remaining != 0.0:
        return None
    reset_raw = getattr(row, "reset_at", None)
    reset = parse_iso(reset_raw) if isinstance(reset_raw, str) else None
    if reset is not None:
        return {
            "id": f"legacy:{row.provider}:{row.pool}",
            "pool_id": str(row.provider or "").strip().lower() or "unknown",
            "window_id": str(row.pool or "ALL").strip().lower() or "ALL",
            "model_scope": None,
            "reason": "LEGACY_ROW",
            "until": reset.isoformat(),
            "until_confidence": "EXACT",
            "source": "legacy-row",
            "evidence_ref": None,
            "run_id": None,
            "created_at": str(getattr(row, "updated_at", "") or now.isoformat()),
            "next_probe_at": None,
            "probe_attempts": 0,
            "cleared_at": None,
            "cleared_by": None,
            "_legacy": True,
        }
    # No tz-aware reset: UNKNOWN anchored at updated_at.
    updated_raw = getattr(row, "updated_at", None)
    updated = parse_iso(updated_raw) if isinstance(updated_raw, str) else None
    anchor = updated or now
    from saberops.capacity.exclusions import backoff_next_probe

    window_id = str(row.pool or "ALL").strip().lower() or "ALL"
    nxt = backoff_next_probe(anchor, 0, window_id)
    # Expired legacy UNKNOWN rows are dropped by the caller (no probe +
    # overage False clears at next_probe_at; overage True stays handled
    # by quota_decision's evidence rule via the synthesized state? No:
    # expired legacy rows simply stop excluding).
    if now >= nxt:
        return None
    return {
        "id": f"legacy:{row.provider}:{row.pool}",
        "pool_id": str(row.provider or "").strip().lower() or "unknown",
        "window_id": window_id,
        "model_scope": None,
        "reason": "LEGACY_ROW",
        "until": None,
        "until_confidence": "UNKNOWN",
        "source": "legacy-row",
        "evidence_ref": None,
        "run_id": None,
        "created_at": anchor.isoformat(),
        "next_probe_at": nxt.isoformat(),
        "probe_attempts": 0,
        "cleared_at": None,
        "cleared_by": None,
        "_legacy": True,
    }


def effective_quota_states(
    db: Database,
    *,
    now: datetime | None = None,
    run_probes: bool = True,
    probes: Any | None = None,
) -> list[Any]:
    """Seam replacing every routing-path ``db.list_quota()`` read.

    1. runs due probes (bounded to <= 45s total);
    2. reads ``db.list_quota()`` and sets ``freshness`` per row; a
       BLOCKED/DEMOTED row whose tz-aware ``reset_at`` is in the past
       comes back as NORMAL with STALE;
    3. appends one synthesized ``QuotaState`` per active/PROBE_DUE
       exclusion with routing NORMAL (so ``_is_quota_blocked`` never
       turns a pool exclusion into a provider-wide block).
    """
    from saberops.models import RoutingState

    moment = _utcnow(now)
    ensure_tables(db)
    if run_probes:
        try:
            from saberops.capacity import probes as _probes

            deadline = time.monotonic() + 45.0
            _probes.run_due_probes(db, now=moment, probes=probes, deadline=deadline)
        except Exception:
            pass
    rows = db.list_quota()
    out: list[Any] = []
    for row in rows:
        # Freshness per row: window = pool label; source unknown for raw
        # quota_state rows -> owner rule (max(table, 24h)).
        try:
            row.freshness = freshness(
                getattr(row, "updated_at", None),
                getattr(row, "pool", None),
                None,
                moment,
            )
        except Exception:
            pass
        # Expired BLOCKED/DEMOTED rows come back NORMAL + STALE.
        try:
            reset = parse_iso(getattr(row, "reset_at", None))
            if (
                reset is not None
                and getattr(row, "routing_state", None) in (RoutingState.BLOCKED, RoutingState.DEMOTED)
                and moment >= reset
            ):
                row.routing_state = RoutingState.NORMAL
                row.freshness = "STALE"
        except Exception:
            pass
        out.append(row)
    # Exclusions (persisted) + legacy remaining-0 rows (read, not migrated).
    try:
        exclusions = list_active_exclusions(db, now=moment)
    except Exception:
        exclusions = []
    for row in rows:
        try:
            legacy = _legacy_exclusion_from_row(row, moment)
        except Exception:
            legacy = None
        if legacy is not None:
            exclusions.append(legacy)
    from saberops.models import QuotaState

    for exc in exclusions:
        try:
            if not is_excluding(exc, moment):
                continue
        except Exception:
            continue
        until = exc.get("until")
        out.append(
            QuotaState(
                provider=str(exc.get("source") or "exclusion"),
                pool=str(exc.get("window_id") or "ALL"),
                remaining_percent=0.0,
                reset_at=until,
                routing_state=RoutingState.NORMAL,
                updated_at=str(exc.get("created_at") or moment.isoformat()),
                pool_id=str(exc.get("pool_id") or ""),
                normalized_remaining_pct=0.0,
                freshness="FRESH",
                exclusion_id=str(exc.get("id") or ""),
                exclusion_until=until,
                exclusion_confidence=str(exc.get("until_confidence") or "UNKNOWN"),
                exclusion_model_scope=exc.get("model_scope"),
            )
        )
    return out


__all__ = ["effective_quota_states"]
