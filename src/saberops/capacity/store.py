"""Owner-DB exclusion + probe-run store (M2-1 A).

Storage lives in the owner DB (``db.owner_db.db_path``). Connections mirror
the pragmas of ``Database._get_connection``. Tables are created with
``CREATE TABLE IF NOT EXISTS`` on first use. ``DB_SCHEMA_VERSION`` is
unchanged.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from saberops.capacity.exclusions import (
    backoff_next_probe,
    exclusion_state,
    merge_wins,
    parse_iso,
)

if TYPE_CHECKING:
    from saberops.db import Database
else:  # pragma: no cover - typing only
    Database = Any  # type: ignore[assignment,misc]


@contextmanager
def owner_connection(db: Database) -> Iterator[sqlite3.Connection]:
    """Open the owner DB with the same pragmas as Database._get_connection."""
    from saberops.db import Database as _Db

    owner: _Db = db.owner_db if hasattr(db, "owner_db") else db
    path: Path = owner.db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def ensure_tables(db: Database) -> None:
    """Create capacity tables idempotently."""
    with owner_connection(db) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS capacity_exclusions (
                id TEXT PRIMARY KEY,
                pool_id TEXT NOT NULL,
                window_id TEXT NOT NULL,
                model_scope TEXT,
                reason TEXT NOT NULL,
                until TEXT,
                until_confidence TEXT NOT NULL,
                source TEXT NOT NULL,
                evidence_ref TEXT,
                run_id TEXT,
                created_at TEXT NOT NULL,
                next_probe_at TEXT,
                probe_attempts INTEGER NOT NULL DEFAULT 0,
                cleared_at TEXT,
                cleared_by TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS capacity_probe_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                harness TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ok INTEGER NOT NULL,
                error_code TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS capacity_observations (
                id TEXT PRIMARY KEY,
                pool_id TEXT NOT NULL,
                window_id TEXT NOT NULL,
                remaining_pct REAL NOT NULL,
                reset_at TEXT,
                observed_at TEXT NOT NULL,
                source TEXT NOT NULL,
                legacy_provider TEXT,
                legacy_pool TEXT,
                evidence_ref TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS capacity_reservations (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                job_id TEXT,
                route TEXT NOT NULL,
                pool_id TEXT NOT NULL,
                window_id TEXT NOT NULL,
                multiplier REAL NOT NULL DEFAULT 1.0,
                size_bucket TEXT NOT NULL,
                cost_pct REAL NOT NULL,
                spread_pct REAL NOT NULL,
                state TEXT NOT NULL,
                total_tokens INTEGER,
                created_at TEXT NOT NULL,
                settled_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS capacity_cost_model (
                route TEXT NOT NULL,
                pool_id TEXT NOT NULL,
                window_id TEXT NOT NULL,
                size_bucket TEXT NOT NULL,
                samples INTEGER NOT NULL DEFAULT 0,
                ewma_pct REAL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (route, pool_id, window_id, size_bucket)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_capacity_obs_pool_window
                ON capacity_observations (pool_id, window_id, observed_at)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_capacity_res_run
                ON capacity_reservations (run_id)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_capacity_res_pool_window
                ON capacity_reservations (pool_id, window_id, state)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS capacity_scheduler_lease (
                lease_name TEXT PRIMARY KEY,
                holder_id TEXT NOT NULL,
                acquired_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                fencing_token INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        conn.commit()


def _utcnow(now: datetime | None) -> datetime:
    if now is not None:
        return now.astimezone(UTC) if now.tzinfo else now.replace(tzinfo=UTC)
    return datetime.now(UTC)


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    aware = dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat()


def upsert_exclusion(
    db: Database,
    *,
    pool_id: str,
    window_id: str,
    reason: str,
    until: datetime | None,
    until_confidence: str,
    source: str,
    model_scope: str | None = None,
    evidence_ref: str | None = None,
    run_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Merge-upsert one exclusion; at most one active per key.

    A re-exhaustion of the same pool within 24h of a clear continues the
    attempt count instead of restarting.
    """
    ensure_tables(db)
    moment = _utcnow(now)
    pool = pool_id.strip()
    window = (window_id or "ALL").strip() or "ALL"
    scope = (model_scope.strip() if model_scope and model_scope.strip() else None)
    confidence = (until_confidence or "UNKNOWN").strip().upper()
    if confidence not in ("EXACT", "PARSED", "HINT", "UNKNOWN"):
        confidence = "UNKNOWN"
    created_iso = moment.isoformat()
    until_iso = _iso(until)
    next_probe_iso: str | None = None
    if confidence == "UNKNOWN":
        next_probe_iso = _iso(backoff_next_probe(moment, 0, window))
    elif confidence == "HINT" and until_iso is None:
        next_probe_iso = _iso(backoff_next_probe(moment, 0, window))
    with owner_connection(db) as conn:
        if scope is None:
            rows = conn.execute(
                "SELECT * FROM capacity_exclusions WHERE pool_id = ? AND window_id = ?"
                " AND model_scope IS NULL AND cleared_at IS NULL",
                (pool, window),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM capacity_exclusions WHERE pool_id = ? AND window_id = ?"
                " AND model_scope = ? AND cleared_at IS NULL",
                (pool, window, scope),
            ).fetchall()
        existing = [dict(r) for r in rows]
        # Continue attempt count when re-exhausted within 24h of a clear.
        attempts = 0
        if not existing:
            if scope is None:
                last = conn.execute(
                    "SELECT * FROM capacity_exclusions WHERE pool_id = ? AND window_id = ?"
                    " AND model_scope IS NULL AND cleared_at IS NOT NULL"
                    " ORDER BY cleared_at DESC LIMIT 1",
                    (pool, window),
                ).fetchone()
            else:
                last = conn.execute(
                    "SELECT * FROM capacity_exclusions WHERE pool_id = ? AND window_id = ?"
                    " AND model_scope = ? AND cleared_at IS NOT NULL"
                    " ORDER BY cleared_at DESC LIMIT 1",
                    (pool, window, scope),
                ).fetchone()
            if last is not None:
                cleared = parse_iso(last["cleared_at"])
                if cleared is not None and (moment - cleared) <= timedelta(hours=24):
                    try:
                        attempts = int(last["probe_attempts"] or 0)
                    except (TypeError, ValueError):
                        attempts = 0
            if confidence == "UNKNOWN":
                next_probe_iso = _iso(backoff_next_probe(moment, attempts, window))
        new_row: dict[str, object] = {
            "id": f"exc_{uuid.uuid4().hex[:12]}",
            "pool_id": pool,
            "window_id": window,
            "model_scope": scope,
            "reason": reason,
            "until": until_iso,
            "until_confidence": confidence,
            "source": source,
            "evidence_ref": evidence_ref,
            "run_id": run_id,
            "created_at": created_iso,
            "next_probe_at": next_probe_iso,
            "probe_attempts": attempts,
            "cleared_at": None,
            "cleared_by": None,
        }
        replaced = False
        for old in existing:
            if merge_wins(old, new_row):
                continue
            # Old wins: keep old, drop new.
            return old
        for old in existing:
            # New wins over this old row: clear it superseded (keep history).
            conn.execute(
                "UPDATE capacity_exclusions SET cleared_at = ?, cleared_by = ? WHERE id = ?",
                (created_iso, "SUPERSEDED", old["id"]),
            )
            replaced = True
        _ = replaced
        conn.execute(
            """
            INSERT INTO capacity_exclusions (
                id, pool_id, window_id, model_scope, reason, until,
                until_confidence, source, evidence_ref, run_id,
                created_at, next_probe_at, probe_attempts, cleared_at, cleared_by
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_row["id"], new_row["pool_id"], new_row["window_id"],
                new_row["model_scope"], new_row["reason"], new_row["until"],
                new_row["until_confidence"], new_row["source"], new_row["evidence_ref"],
                new_row["run_id"], new_row["created_at"], new_row["next_probe_at"],
                new_row["probe_attempts"], new_row["cleared_at"], new_row["cleared_by"],
            ),
        )
        conn.commit()
        return new_row


def list_active_exclusions(
    db: Database, *, now: datetime | None = None
) -> list[dict[str, object]]:
    """Return active + PROBE_DUE exclusions, expiring EXACT/PARSED inline."""
    ensure_tables(db)
    moment = _utcnow(now)
    with owner_connection(db) as conn:
        rows = conn.execute(
            "SELECT * FROM capacity_exclusions WHERE cleared_at IS NULL"
            " ORDER BY created_at ASC, id ASC"
        ).fetchall()
        result: list[dict[str, object]] = []
        for row in rows:
            exc = dict(row)
            state = exclusion_state(exc, moment)
            if state == "EXPIRED":
                conn.execute(
                    "UPDATE capacity_exclusions SET cleared_at = ?, cleared_by = ?"
                    " WHERE id = ?",
                    (moment.isoformat(), "EXPIRED", exc["id"]),
                )
                continue
            if state in ("ACTIVE", "PROBE_DUE"):
                result.append(exc)
        conn.commit()
        return result


def clear_exclusion(
    db: Database, exclusion_id: str, *, by: str, now: datetime | None = None
) -> None:
    """Mark one exclusion cleared."""
    ensure_tables(db)
    moment = _utcnow(now)
    with owner_connection(db) as conn:
        conn.execute(
            "UPDATE capacity_exclusions SET cleared_at = ?, cleared_by = ?"
            " WHERE id = ? AND cleared_at IS NULL",
            (moment.isoformat(), by, exclusion_id),
        )
        conn.commit()


def clear_pool_exclusions(
    db: Database, pool_id: str, *, window_id: str | None = None, by: str,
    now: datetime | None = None,
) -> int:
    """Clear active exclusions for one pool (optionally one window)."""
    ensure_tables(db)
    moment = _utcnow(now)
    with owner_connection(db) as conn:
        if window_id is None:
            cur = conn.execute(
                "UPDATE capacity_exclusions SET cleared_at = ?, cleared_by = ?"
                " WHERE pool_id = ? AND cleared_at IS NULL",
                (moment.isoformat(), by, pool_id),
            )
        else:
            cur = conn.execute(
                "UPDATE capacity_exclusions SET cleared_at = ?, cleared_by = ?"
                " WHERE pool_id = ? AND window_id = ? AND cleared_at IS NULL",
                (moment.isoformat(), by, pool_id, window_id),
            )
        conn.commit()
        return cur.rowcount or 0


def bump_probe_attempt(
    db: Database, exclusion_id: str, *, now: datetime | None = None
) -> None:
    """Increment probe attempts and recompute next_probe_at (UNKNOWN backoff)."""
    ensure_tables(db)
    moment = _utcnow(now)
    with owner_connection(db) as conn:
        row = conn.execute(
            "SELECT * FROM capacity_exclusions WHERE id = ?", (exclusion_id,)
        ).fetchone()
        if row is None:
            return
        exc = dict(row)
        attempts = int(exc.get("probe_attempts") or 0) + 1
        created = parse_iso(exc.get("created_at")) or moment
        nxt = _iso(backoff_next_probe(created, attempts, str(exc.get("window_id") or "")))
        conn.execute(
            "UPDATE capacity_exclusions SET probe_attempts = ?, next_probe_at = ?"
            " WHERE id = ?",
            (attempts, nxt, exclusion_id),
        )
        conn.commit()


def record_probe_run(
    db: Database, *, harness: str, ok: bool, error_code: str | None = None,
    now: datetime | None = None,
) -> None:
    """Persist one probe run for rate limiting."""
    ensure_tables(db)
    moment = _utcnow(now)
    with owner_connection(db) as conn:
        conn.execute(
            "INSERT INTO capacity_probe_runs (harness, started_at, ok, error_code)"
            " VALUES (?, ?, ?, ?)",
            (harness, moment.isoformat(), 1 if ok else 0, error_code),
        )
        conn.commit()


def last_probe_run(db: Database, harness: str) -> dict[str, object] | None:
    """Return the most recent probe run for a harness, else None."""
    ensure_tables(db)
    with owner_connection(db) as conn:
        row = conn.execute(
            "SELECT * FROM capacity_probe_runs WHERE harness = ?"
            " ORDER BY started_at DESC, id DESC LIMIT 1",
            (harness,),
        ).fetchone()
        return dict(row) if row is not None else None


def probe_allowed(
    db: Database, harness: str, *, now: datetime | None = None,
    hint_due: bool = False,
) -> bool:
    """At most 1 probe per harness per 10 min, except when a HINT is due."""
    if hint_due:
        return True
    ensure_tables(db)
    moment = _utcnow(now)
    last = last_probe_run(db, harness)
    if last is None:
        return True
    raw_started = last.get("started_at")
    started = parse_iso(raw_started if isinstance(raw_started, str) else None)
    if started is None:
        return True
    return (moment - started) >= timedelta(minutes=10)


__all__ = [
    "owner_connection",
    "ensure_tables",
    "upsert_exclusion",
    "list_active_exclusions",
    "clear_exclusion",
    "clear_pool_exclusions",
    "bump_probe_attempt",
    "record_probe_run",
    "last_probe_run",
    "probe_allowed",
]
