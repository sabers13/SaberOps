"""Routing seam: effective quota states with expiry + exclusions (M2-1 F)."""

from __future__ import annotations

import sqlite3
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from saberops.capacity.exclusions import is_excluding, parse_iso
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


def _load_pool_policies() -> dict[str, Any]:
    """Best-effort control-plane pool policies for the no-probe path."""
    try:
        from saberops.control_plane.plans import effective_quota_pools
        from saberops.control_plane.policy import load_control_plane_policy

        return effective_quota_pools(load_control_plane_policy())
    except Exception:
        return {}


_WINDOW_NAMES = frozenset({"5h", "daily", "weekly", "monthly", "all"})


def _legacy_pool_and_window(row: Any) -> tuple[str, str]:
    """Split a legacy (provider, pool) row into (pool_id, window_id).

    Window-named pools (weekly, 5h, ...) stay provider-scoped with that
    window; generated pool ids stay pool-scoped with window ALL.
    """
    pool_raw = str(getattr(row, "pool", "") or "ALL").strip().lower() or "ALL"
    provider_raw = str(getattr(row, "provider", "") or "").strip().lower()
    pool_id_attr = getattr(row, "pool_id", None)
    if isinstance(pool_id_attr, str) and pool_id_attr.strip():
        return pool_id_attr.strip(), pool_raw
    if pool_raw in _WINDOW_NAMES:
        return provider_raw or "unknown", pool_raw
    return pool_raw or "unknown", "ALL"


def _legacy_exclusion_from_row(row: Any, now: datetime) -> dict[str, object] | None:
    """Read (not migrate) a remaining-0 quota_state row as an exclusion."""
    try:
        remaining = float(row.remaining_percent)
    except (TypeError, ValueError):
        return None
    if remaining != 0.0:
        return None
    pool_id, window_id = _legacy_pool_and_window(row)
    reset_raw = getattr(row, "reset_at", None)
    reset = parse_iso(reset_raw) if isinstance(reset_raw, str) else None
    if reset is not None:
        return {
            "id": f"legacy:{row.provider}:{row.pool}",
            "pool_id": pool_id,
            "window_id": window_id,
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
    # No tz-aware reset: UNKNOWN anchored at updated_at. The seam's no-probe
    # path decides clear vs keep from the pool policy, so the row is always
    # returned here; past-backoff rows are PROBE_DUE, never silently dropped.
    updated_raw = getattr(row, "updated_at", None)
    updated = parse_iso(updated_raw) if isinstance(updated_raw, str) else None
    anchor = updated or now
    from saberops.capacity.exclusions import backoff_next_probe

    nxt = backoff_next_probe(anchor, 0, window_id)
    return {
        "id": f"legacy:{row.provider}:{row.pool}",
        "pool_id": pool_id,
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


def _read_latest_observations(db: Database) -> dict[tuple[str, str], dict[str, Any]]:
    """Read the newest capacity_observations row per ``(pool_id, window_id)`` read-only."""
    owner = getattr(db, "owner_db", db)
    out: dict[tuple[str, str], dict[str, Any]] = {}
    try:
        with owner._get_connection() as conn:
            present = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table'"
                " AND name = 'capacity_observations'"
            ).fetchone()
            if present is None:
                return out
            rows = conn.execute(
                "SELECT pool_id, window_id, remaining_pct, reset_at, observed_at,"
                " source, legacy_provider, legacy_pool"
                " FROM capacity_observations"
                " ORDER BY observed_at DESC, id DESC"
            ).fetchall()
            for row in rows:
                key = (str(row["pool_id"]), str(row["window_id"]))
                if key not in out:
                    out[key] = dict(row)
    except (sqlite3.OperationalError, sqlite3.DatabaseError, AttributeError):
        return {}
    return out


_HARNESS_TO_PROVIDER: dict[str, str] = {
    "codex": "codex",
    "claude": "anthropic",
    "anthropic": "anthropic",
    "opencode": "opencode",
    "opencode-go": "opencode",
}


def effective_quota_states(
    db: Database,
    *,
    now: datetime | None = None,
    run_probes: bool = True,
    probes: Any | None = None,
    pool_policies: dict[str, Any] | None = None,
) -> list[Any]:
    """Seam replacing every routing-path ``db.list_quota()`` read.

    1. runs due probes (bounded to <= 45s total; only harnesses with a
       PROBE_DUE exclusion);
    2. builds per-window ``QuotaState`` views from ``capacity_observations``
       (preserving real ``window_id`` and ``source`` without collapsing windows),
       falling back to ``db.list_quota()`` for uncovered legacy rows; a
       BLOCKED/DEMOTED row whose tz-aware ``reset_at`` is in the past
       comes back as NORMAL with STALE;
    3. resolves the no-probe path in the seam: a PROBE_DUE exclusion no
       registered harness can probe clears when its pool policy has
       ``overage_possible`` False (``EXPIRED_NO_PROBE``) and is kept
       otherwise (unknown pools keep: fail closed);
    4. appends one synthesized ``QuotaState`` per active/PROBE_DUE
       exclusion with routing NORMAL (so ``_is_quota_blocked`` never
       turns a pool exclusion into a provider-wide block).

    ``pool_policies`` defaults to the control-plane policy (best effort);
    tests pass it explicitly.
    """
    from saberops.models import QuotaState, RoutingState

    moment = _utcnow(now)
    if not getattr(db, "_read_only", False):
        ensure_tables(db)
    if run_probes:
        try:
            from saberops.capacity import probes as _probes

            deadline = time.monotonic() + 45.0
            _probes.run_due_probes(
                db, now=moment, probes=probes, deadline=deadline,
                pool_policies=pool_policies,
            )
        except Exception:
            pass
    try:
        policies: dict[str, Any] = (
            dict(pool_policies) if pool_policies is not None
            else _load_pool_policies()
        )
    except Exception:
        policies = {}

    latest_obs = _read_latest_observations(db)
    by_pool: dict[str, dict[str, dict[str, Any]]] = {}
    for (pool_id, win_id), obs_row in latest_obs.items():
        by_pool.setdefault(pool_id, {})[win_id] = obs_row

    out: list[Any] = []
    covered_keys: set[tuple[str, str]] = set()
    pools_with_specific: dict[str, datetime | None] = {}

    for pool_id in sorted(by_pool):
        win_map = by_pool[pool_id]
        specific = [(w, r) for w, r in sorted(win_map.items()) if w != "ALL"]
        newest_specific: datetime | None = None
        if specific:
            parsed_times = [
                ts
                for _, r in specific
                if (ts := parse_iso(str(r.get("observed_at") or ""))) is not None
            ]
            newest_specific = max(parsed_times) if parsed_times else None
            pools_with_specific[pool_id] = newest_specific
        selected: list[tuple[str, dict[str, Any]]] = list(specific)
        all_row = win_map.get("ALL")
        if all_row is not None:
            if not specific:
                selected.append(("ALL", all_row))
            else:
                all_ts = parse_iso(str(all_row.get("observed_at") or ""))
                if all_ts is not None and newest_specific is not None and all_ts > newest_specific:
                    selected.append(("ALL", all_row))
        for win_id, obs_row in selected:
            obs_source = str(obs_row["source"]) if obs_row.get("source") else None
            raw_prov = str(obs_row.get("legacy_provider") or "").strip().lower()
            provider = _HARNESS_TO_PROVIDER.get(raw_prov, raw_prov) or pool_id
            legacy_pool = str(obs_row.get("legacy_pool") or "").strip().lower()
            pool_label = legacy_pool if (win_id == "ALL" and legacy_pool) else win_id
            obs_at = str(obs_row.get("observed_at") or "")
            rem = float(obs_row["remaining_pct"])
            reset_raw = obs_row.get("reset_at")
            reset_at = str(reset_raw) if reset_raw else None
            out.append(
                QuotaState(
                    provider=provider,
                    pool=pool_label,
                    remaining_percent=rem,
                    reset_at=reset_at,
                    routing_state=RoutingState.NORMAL,
                    updated_at=obs_at,
                    pool_id=pool_id,
                    normalized_remaining_pct=rem,
                    freshness=freshness(obs_at, win_id, obs_source, moment),
                    source=obs_source,
                    window_id=win_id,
                )
            )
            covered_keys.add((pool_id, win_id))

    rows = db.list_quota()
    for row in rows:
        r_pool_id, r_win_id = _legacy_pool_and_window(row)
        r_state = getattr(row, "routing_state", None)
        if r_state == RoutingState.NORMAL:
            if (r_pool_id, r_win_id) in covered_keys:
                continue
            if r_win_id == "ALL" and r_pool_id in pools_with_specific:
                row_ts = parse_iso(getattr(row, "updated_at", None))
                spec_ts = pools_with_specific[r_pool_id]
                if row_ts is None or spec_ts is None or row_ts <= spec_ts:
                    continue
        if not getattr(row, "pool_id", None):
            try:
                row.pool_id = r_pool_id
            except Exception:
                pass
        if not getattr(row, "window_id", None):
            try:
                row.window_id = r_win_id
            except Exception:
                pass
        row_source = getattr(row, "source", None)
        try:
            row.freshness = freshness(
                getattr(row, "updated_at", None),
                r_win_id,
                row_source,
                moment,
            )
        except Exception:
            pass
        # Expired BLOCKED/DEMOTED rows come back NORMAL + STALE.
        try:
            reset = parse_iso(getattr(row, "reset_at", None))
            if (
                reset is not None
                and getattr(row, "routing_state", None)
                in (RoutingState.BLOCKED, RoutingState.DEMOTED)
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
        # No-probe path (in the seam, not dependent on a probe having run):
        # a PROBE_DUE exclusion no registered harness can probe clears when
        # its pool policy says overage is impossible, and is kept otherwise.
        try:
            from saberops.capacity.exclusions import exclusion_state
            from saberops.capacity.probes import probe_available_for

            due = exclusion_state(exc, moment) == "PROBE_DUE"
        except Exception:
            due = False
        if due:
            try:
                available = probe_available_for(
                    exc, policies=policies, probes=probes, now=moment
                )
            except Exception:
                available = True
            if not available:
                # Fail closed: pools without a known policy keep excluding.
                exc_pool_id = exc.get("pool_id")
                policy = policies.get(exc_pool_id) if isinstance(exc_pool_id, str) else None
                clearable = policy is not None and not bool(
                    getattr(policy, "overage_possible", False)
                )
                if clearable:
                    if not exc.get("_legacy"):
                        try:
                            from saberops.capacity.store import clear_exclusion

                            exc_id = exc.get("id")
                            if isinstance(exc_id, str) and exc_id:
                                clear_exclusion(
                                    db, exc_id, by="EXPIRED_NO_PROBE", now=moment
                                )
                        except Exception:
                            pass
                    continue
        raw_until = exc.get("until")
        until = raw_until if isinstance(raw_until, str) else None
        raw_scope = exc.get("model_scope")
        scope = raw_scope if isinstance(raw_scope, str) else None
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
                exclusion_model_scope=scope,
            )
        )
    return out


__all__ = ["effective_quota_states"]
