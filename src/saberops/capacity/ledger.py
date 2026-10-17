"""Capacity ledger (M2-4a C): owner observations, reservations, estimates.

Storage lives in the owner DB (see :mod:`saberops.capacity.store`); every
writer here creates its tables idempotently first. The job queue package
is never imported (cross-lane contract C2): ``job_id`` is opaque.

Ledger keys are ``(pool_id, window_id)`` pairs derived with
:func:`saberops.capacity.snapshot._legacy_pool_and_window`, so the ledger
and the routing seam can never disagree on keys.

Known limitation: the probe red-line path records a **per-pool** minimum,
so its ledger key is ``(pool_id, "ALL")``; per-window probe evidence in
the ledger comes with M2-4b's evidence-driven probing.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

from saberops.capacity.cost_model import (
    CostState,
    SizeBucket,
    cost_estimate,
    validate_route,
)
from saberops.capacity.cost_model import (
    update as _cost_update,
)
from saberops.capacity.exclusions import is_excluding, parse_iso
from saberops.capacity.freshness import freshness
from saberops.capacity.snapshot import _legacy_pool_and_window
from saberops.capacity.store import (
    ensure_tables,
    list_active_exclusions,
    owner_connection,
)
from saberops.models import QuotaState, RoutingState

if TYPE_CHECKING:
    from saberops.db import Database
else:  # pragma: no cover - typing only
    Database = Any  # type: ignore[assignment,misc]

OWNER_SOURCES = ("OWNER", "PROBE")

#: Capacity tables that the debt accounting in
#: :func:`post_observation_charges` depends on.  A DB is only a genuinely
#: unconfigured owner DB (legacy graceful empty debt) when **none** of
#: these exist; a schema that has some but not all of them is *partially*
#: initialized and must fail closed rather than report zero debt.
REQUIRED_CAPACITY_TABLES: frozenset[str] = frozenset(
    {"capacity_observations", "capacity_reservations"}
)


def capacity_tables_present(conn: sqlite3.Connection) -> frozenset[str]:
    """Return which of :data:`REQUIRED_CAPACITY_TABLES` exist in ``conn``.

    Reads ``sqlite_master`` only.  Any database error propagates to the
    caller, which is expected to fail closed (never coerce to "no tables").
    """
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
        " AND name IN ('capacity_observations', 'capacity_reservations')"
    ).fetchall()
    return frozenset(str(row[0]) for row in rows)


@dataclass(frozen=True)
class LedgerEstimate:
    pool_id: str
    window_id: str
    remaining_est: float | None
    uncertainty: float
    observed_at: str | None
    source: str | None
    stale: bool
    excluded_until: str | None


@dataclass(frozen=True)
class PoolDraw:
    pool_id: str
    window_id: str
    multiplier: float = 1.0


@dataclass(frozen=True)
class Reservation:
    id: str
    run_id: str
    job_id: str | None
    route: str
    pool_id: str
    window_id: str
    multiplier: float
    size_bucket: SizeBucket
    cost_pct: float
    spread_pct: float
    state: str
    total_tokens: int | None
    created_at: str
    settled_at: str | None


def _utcnow(now: datetime | None) -> datetime:
    if now is not None:
        return now.astimezone(UTC) if now.tzinfo else now.replace(tzinfo=UTC)
    return datetime.now(UTC)


def _row_to_quota(row: sqlite3.Row) -> QuotaState:
    """Map a ``quota_state`` row (moved here from ``Database``)."""
    return QuotaState(
        provider=str(row["provider"]),
        pool=str(row["pool"]),
        remaining_percent=float(row["remaining_percent"]),
        reset_at=str(row["reset_at"]) if row["reset_at"] else None,
        routing_state=RoutingState(str(row["routing_state"])),
        updated_at=str(row["updated_at"]),
    )


def _coerce_bucket(bucket: SizeBucket | str) -> SizeBucket:
    if isinstance(bucket, SizeBucket):
        return bucket
    try:
        return SizeBucket(str(bucket))
    except ValueError as exc:
        raise ValueError(f"unknown size bucket: {bucket!r}") from exc


def _reservation_from_row(row: Mapping[str, Any]) -> Reservation:
    raw_tokens = row.get("total_tokens")
    return Reservation(
        id=str(row["id"]),
        run_id=str(row["run_id"]),
        job_id=str(row["job_id"]) if row.get("job_id") is not None else None,
        route=str(row["route"]),
        pool_id=str(row["pool_id"]),
        window_id=str(row["window_id"]),
        multiplier=float(row["multiplier"]),
        size_bucket=_coerce_bucket(str(row["size_bucket"])),
        cost_pct=float(row["cost_pct"]),
        spread_pct=float(row["spread_pct"]),
        state=str(row["state"]),
        total_tokens=int(raw_tokens) if raw_tokens is not None else None,
        created_at=str(row["created_at"]),
        settled_at=str(row["settled_at"]) if row.get("settled_at") else None,
    )


def _load_cost_state(
    conn: sqlite3.Connection, route: str, pool_id: str, window_id: str,
    bucket: SizeBucket,
) -> CostState | None:
    row = conn.execute(
        "SELECT samples, ewma_pct FROM capacity_cost_model"
        " WHERE route = ? AND pool_id = ? AND window_id = ? AND size_bucket = ?",
        (route, pool_id, window_id, bucket.value),
    ).fetchone()
    if row is None:
        return None
    raw_ewma = row["ewma_pct"]
    return CostState(
        samples=int(row["samples"]),
        ewma_pct=float(raw_ewma) if raw_ewma is not None else None,
    )


def _store_cost_state(
    conn: sqlite3.Connection, route: str, pool_id: str, window_id: str,
    bucket: SizeBucket, state: CostState, updated_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO capacity_cost_model (
            route, pool_id, window_id, size_bucket, samples, ewma_pct, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(route, pool_id, window_id, size_bucket) DO UPDATE SET
            samples = excluded.samples,
            ewma_pct = excluded.ewma_pct,
            updated_at = excluded.updated_at
        """,
        (route, pool_id, window_id, bucket.value, state.samples,
         state.ewma_pct, updated_at),
    )


@contextmanager
def _read_connection(db: Database) -> Iterator[sqlite3.Connection]:
    """Yield a read connection on the owner DB (honours read-only)."""
    owner: Database = db.owner_db if hasattr(db, "owner_db") else db
    with owner._get_connection() as conn:
        yield conn


def record_owner_quota(
    db: Database,
    provider: str,
    pool: str,
    remaining_pct: float,
    reset_at: str | None,
    *,
    source: str,
    now: datetime | None = None,
) -> QuotaState:
    """Record an owner (or probe) quota observation and its quota row.

    In one owner-DB transaction: (1) append a ``capacity_observations``
    row keyed by ``(pool_id, window_id)`` via
    :func:`saberops.capacity.snapshot._legacy_pool_and_window`; (2)
    upsert the ``quota_state`` row exactly as ``db.set_quota`` did at
    ``7cac956``, but with ``routing_state = NORMAL``.
    """
    if source not in OWNER_SOURCES:
        raise ValueError(f"source must be one of {OWNER_SOURCES}")
    pct = float(remaining_pct)
    if not 0.0 <= pct <= 100.0:
        raise ValueError("remaining_percent must be between 0 and 100")
    provider_norm = provider.strip().lower()
    pool_norm = pool.strip().lower()
    moment = _utcnow(now)
    observed_iso = moment.isoformat()
    key = _legacy_pool_and_window(
        SimpleNamespace(provider=provider_norm, pool=pool_norm)
    )
    pool_id, window_id = key[0], key[1]
    ensure_tables(db)
    obs_id = f"obs_{uuid.uuid4().hex[:12]}"
    with owner_connection(db) as conn:
        conn.execute(
            """
            INSERT INTO capacity_observations (
                id, pool_id, window_id, remaining_pct, reset_at, observed_at,
                source, legacy_provider, legacy_pool, evidence_ref
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (obs_id, pool_id, window_id, pct, reset_at, observed_iso,
             source, provider_norm, pool_norm, None),
        )
        conn.execute(
            """
            INSERT INTO quota_state (
                provider, pool, remaining_percent, reset_at, routing_state, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider, pool) DO UPDATE SET
                remaining_percent = excluded.remaining_percent,
                reset_at = excluded.reset_at,
                routing_state = excluded.routing_state,
                updated_at = excluded.updated_at
            """,
            (provider_norm, pool_norm, pct, reset_at,
             RoutingState.NORMAL.value, observed_iso),
        )
        if source == "PROBE":
            _learn_from_probe(
                conn, pool_id, window_id, pct, reset_at,
                observed_iso, moment, new_row_id=obs_id,
            )
        conn.commit()
    return QuotaState(
        provider=provider_norm,
        pool=pool_norm,
        remaining_percent=pct,
        reset_at=reset_at,
        routing_state=RoutingState.NORMAL,
        updated_at=observed_iso,
    )


def record_probe_window_observation(
    db: Database,
    pool_id: str,
    window_id: str,
    remaining_pct: float,
    reset_at: str | None,
    *,
    harness: str,
    now: datetime | None = None,
    evidence_ref: str | None = None,
) -> str:
    """Record a per-window probe observation in capacity_observations and fold into learning."""
    pct = float(remaining_pct)
    if not 0.0 <= pct <= 100.0:
        raise ValueError("remaining_percent must be between 0 and 100")
    moment = _utcnow(now)
    observed_iso = moment.isoformat()
    ensure_tables(db)
    obs_id = f"obs_{uuid.uuid4().hex[:12]}"
    with owner_connection(db) as conn:
        conn.execute(
            """
            INSERT INTO capacity_observations (
                id, pool_id, window_id, remaining_pct, reset_at, observed_at,
                source, legacy_provider, legacy_pool, evidence_ref
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (obs_id, pool_id, window_id, pct, reset_at, observed_iso,
             "PROBE", harness, None, evidence_ref),
        )
        _learn_from_probe(
            conn, pool_id, window_id, pct, reset_at,
            observed_iso, moment, new_row_id=obs_id,
        )
        conn.commit()
    return obs_id


def _learn_from_probe(
    conn: sqlite3.Connection,
    pool_id: str,
    window_id: str,
    new_pct: float,
    new_reset: str | None,
    new_observed_iso: str,
    moment: datetime,
    new_row_id: str | None = None,
) -> None:
    """Fold one PROBE interval into the cost model (no sample or more).

    ``ALL``-keyed observations never produce samples: they are minima
    across windows, not per-window deltas.
    """
    if window_id == "ALL":
        return
    new_observed = parse_iso(new_observed_iso) or moment
    rows = conn.execute(
        "SELECT * FROM capacity_observations"
        " WHERE pool_id = ? AND window_id = ? AND source = ?"
        " ORDER BY observed_at DESC, id DESC",
        (pool_id, window_id, "PROBE"),
    ).fetchall()
    previous: Mapping[str, Any] | None = None
    for row in rows:
        candidate = dict(row)
        cand_id = str(candidate.get("id") or "")
        if new_row_id is not None and cand_id == new_row_id:
            continue
        if str(candidate.get("observed_at") or "") > new_observed_iso:
            continue
        if cand_id == "":
            continue
        # Skip the row just inserted above if new_row_id not provided.
        if (
            new_row_id is None
            and previous is None
            and candidate.get("observed_at") == new_observed_iso
        ):
            if float(cast(float, candidate.get("remaining_pct"))) == new_pct and (
                (candidate.get("reset_at") or None) == (new_reset or None)
            ):
                continue
        previous = candidate
        break
    if previous is None:
        return
    prev_observed = parse_iso(previous.get("observed_at") if isinstance(
        previous.get("observed_at"), str) else None)
    if prev_observed is None:
        return
    prev_pct = float(cast(float, previous.get("remaining_pct")))
    delta = prev_pct - new_pct
    if delta <= 0:
        return
    prev_reset_raw = previous.get("reset_at")
    prev_reset = parse_iso(prev_reset_raw) if isinstance(prev_reset_raw, str) else None
    new_reset_dt = parse_iso(new_reset) if isinstance(new_reset, str) else None
    for reset in (prev_reset, new_reset_dt):
        if reset is not None and prev_observed < reset <= new_observed:
            return
    prev_iso = previous.get("observed_at") if isinstance(
        previous.get("observed_at"), str) else None
    if prev_iso is None:
        return
    settled = conn.execute(
        "SELECT * FROM capacity_reservations"
        " WHERE pool_id = ? AND window_id = ? AND state = ?"
        " AND settled_at > ? AND settled_at <= ?",
        (pool_id, window_id, "SETTLED", prev_iso, new_observed_iso),
    ).fetchall()
    if not settled:
        return
    reservations = [dict(r) for r in settled]
    tokens: list[int | None] = []
    for res in reservations:
        raw = res.get("total_tokens")
        tokens.append(int(raw) if raw is not None else None)
    if any(t is None for t in tokens):
        shares = [delta / len(reservations)] * len(reservations)
    else:
        total = sum(int(t) for t in tokens if t is not None)
        if total <= 0:
            shares = [delta / len(reservations)] * len(reservations)
        else:
            shares = [delta * int(t) / total for t in tokens if t is not None]
    updated_iso = moment.isoformat()
    for res, share in zip(reservations, shares, strict=True):
        route = str(res.get("route") or "")
        bucket = _coerce_bucket(str(res.get("size_bucket") or "M"))
        old = _load_cost_state(conn, route, pool_id, window_id, bucket)
        new_state = _cost_update(old, float(share))
        _store_cost_state(conn, route, pool_id, window_id, bucket,
                           new_state, updated_iso)


def list_owner_quota(db: Database) -> list[QuotaState]:
    """Today's ``list_quota`` SQL and semantics, moved here."""
    with _read_connection(db) as conn:
        rows = conn.execute(
            "SELECT * FROM quota_state ORDER BY provider ASC, pool ASC"
        ).fetchall()
        return [_row_to_quota(row) for row in rows]


def get_owner_quota(db: Database, provider: str, pool: str) -> QuotaState | None:
    """Today's ``get_quota`` SQL and semantics, moved here."""
    with _read_connection(db) as conn:
        row = conn.execute(
            "SELECT * FROM quota_state WHERE provider = ? AND pool = ?",
            (provider.strip().lower(), pool.strip().lower()),
        ).fetchone()
        return _row_to_quota(row) if row is not None else None


def enable_owner_quota(db: Database, provider: str, pool: str) -> QuotaState:
    """Today's ``enable_quota`` SQL and semantics, moved here."""
    from saberops.db import current_iso_timestamp

    provider_norm = provider.strip().lower()
    pool_norm = pool.strip().lower()
    now_iso = current_iso_timestamp()
    with owner_connection(db) as conn:
        row = conn.execute(
            "SELECT remaining_percent, reset_at FROM quota_state "
            "WHERE provider = ? AND pool = ?",
            (provider_norm, pool_norm),
        ).fetchone()
        remaining = float(row["remaining_percent"]) if row is not None else 100.0
        reset_at = str(row["reset_at"]) if row is not None and row["reset_at"] else None
        conn.execute(
            """
            INSERT INTO quota_state (
                provider, pool, remaining_percent, reset_at, routing_state, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider, pool) DO UPDATE SET
                routing_state = excluded.routing_state,
                updated_at = excluded.updated_at
            """,
            (provider_norm, pool_norm, remaining, reset_at,
             RoutingState.NORMAL.value, now_iso),
        )
        conn.commit()
    return QuotaState(
        provider=provider_norm,
        pool=pool_norm,
        remaining_percent=remaining,
        reset_at=reset_at,
        routing_state=RoutingState.NORMAL,
        updated_at=now_iso,
    )


def reserve(
    db: Database,
    *,
    run_id: str,
    job_id: str | None = None,
    route: str,
    draws: Sequence[PoolDraw],
    size_bucket: SizeBucket | str,
    now: datetime | None = None,
) -> list[Reservation]:
    """Reserve cost for one run across its pool draws (idempotent)."""
    rid = run_id.strip() if isinstance(run_id, str) else ""
    if not rid:
        raise ValueError("run_id is required")
    clean_route = validate_route(route)
    bucket = _coerce_bucket(size_bucket)
    moment = _utcnow(now)
    created_iso = moment.isoformat()
    ensure_tables(db)
    out: list[Reservation] = []
    with owner_connection(db) as conn:
        for draw in draws:
            pool_id = draw.pool_id.strip()
            window_id = (draw.window_id or "ALL").strip() or "ALL"
            if not pool_id:
                raise ValueError("draw pool_id is required")
            multiplier = float(draw.multiplier)
            existing = conn.execute(
                "SELECT * FROM capacity_reservations"
                " WHERE run_id = ? AND pool_id = ? AND window_id = ?",
                (rid, pool_id, window_id),
            ).fetchall()
            if existing:
                out.extend(_reservation_from_row(dict(r)) for r in existing)
                continue
            old = _load_cost_state(conn, clean_route, pool_id, window_id, bucket)
            est = cost_estimate(old, window_id, bucket)
            row_id = f"res_{uuid.uuid4().hex[:12]}"
            conn.execute(
                """
                INSERT INTO capacity_reservations (
                    id, run_id, job_id, route, pool_id, window_id, multiplier,
                    size_bucket, cost_pct, spread_pct, state, total_tokens,
                    created_at, settled_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (row_id, rid, job_id, clean_route, pool_id, window_id,
                 multiplier, bucket.value, est.pct * multiplier,
                 est.spread * multiplier, "RESERVED", None, created_iso, None),
            )
            row = conn.execute(
                "SELECT * FROM capacity_reservations WHERE id = ?", (row_id,)
            ).fetchone()
            assert row is not None
            out.append(_reservation_from_row(dict(row)))
        conn.commit()
    return out


def settle(
    db: Database, run_id: str, *, total_tokens: int | None,
    now: datetime | None = None,
) -> int:
    """Mark a run's RESERVED rows SETTLED (idempotent)."""
    rid = run_id.strip() if isinstance(run_id, str) else ""
    if not rid:
        return 0
    moment = _utcnow(now)
    ensure_tables(db)
    with owner_connection(db) as conn:
        cur = conn.execute(
            "UPDATE capacity_reservations SET state = ?, settled_at = ?,"
            " total_tokens = ? WHERE run_id = ? AND state = ?",
            ("SETTLED", moment.isoformat(), total_tokens, rid, "RESERVED"),
        )
        conn.commit()
        return int(cur.rowcount or 0)


def release(db: Database, run_id: str, now: datetime | None = None) -> int:
    """Mark a run's RESERVED rows RELEASED (idempotent)."""
    rid = run_id.strip() if isinstance(run_id, str) else ""
    if not rid:
        return 0
    moment = _utcnow(now)
    ensure_tables(db)
    with owner_connection(db) as conn:
        cur = conn.execute(
            "UPDATE capacity_reservations SET state = ?, settled_at = ?"
            " WHERE run_id = ? AND state = ?",
            ("RELEASED", moment.isoformat(), rid, "RESERVED"),
        )
        conn.commit()
        return int(cur.rowcount or 0)


@dataclass(frozen=True)
class ReservedDebt:
    """One run's outstanding (RESERVED) capacity obligation.

    ``job_id`` is the opaque owning job recorded at dispatch, or ``None`` for
    a legacy row.  This is a *read model* only: the ledger never interprets
    job state (cross-lane contract C2).
    """

    run_id: str
    job_id: str | None
    pool_windows: tuple[tuple[str, str], ...]


def list_reserved_debts(
    db: Database,
    *,
    limit: int = 200,
    after_run_id: str | None = None,
) -> list[ReservedDebt]:
    """Deterministically page runs that still hold RESERVED capacity.

    Ordered by ``run_id`` with an exclusive ``after_run_id`` cursor so a
    caller scanning a large owner DB cannot permanently hide old debt behind
    a fixed ``LIMIT``.  A non-positive ``limit`` is refused (fail closed).
    """
    if int(limit) < 1:
        raise ValueError(f"limit must be >= 1; got {limit!r}")
    ensure_tables(db)
    out: list[ReservedDebt] = []
    with owner_connection(db) as conn:
        rows = conn.execute(
            "SELECT run_id, job_id, pool_id, window_id FROM capacity_reservations"
            " WHERE state = 'RESERVED' AND run_id > ?"
            " ORDER BY run_id ASC, pool_id ASC, window_id ASC LIMIT ?",
            (after_run_id or "", int(limit)),
        ).fetchall()
    by_run: dict[str, list[tuple[str, str]]] = {}
    jobs: dict[str, str | None] = {}
    for row in rows:
        rid = str(row["run_id"])
        jobs.setdefault(rid, str(row["job_id"]) if row["job_id"] is not None else None)
        by_run.setdefault(rid, []).append((str(row["pool_id"]), str(row["window_id"])))
    for rid in by_run:
        out.append(
            ReservedDebt(
                run_id=rid,
                job_id=jobs.get(rid),
                pool_windows=tuple(by_run[rid]),
            )
        )
    return out


def release_terminal_debt(
    db: Database,
    *,
    run_id: str,
    job_id: str | None = None,
    now: datetime | None = None,
    lease_guard: Any | None = None,
) -> int:
    """Release one conclusively terminal run's RESERVED rows (idempotent).

    This is the owner-authorised settlement write for old-run D8 debt.  It is
    deliberately narrower than :func:`release`:

    * a ``job_id`` guard (when given) keeps an unrelated job's rows with the
      same ``run_id`` untouched, so the write can never free another job's
      obligation;
    * an optional ``lease_guard(conn) -> bool`` is evaluated *inside* the same
      ``BEGIN IMMEDIATE`` transaction as the UPDATE, so a stale scheduler
      holding an expired lease cannot commit a release (the callback returns
      False and nothing is written);
    * it only ever moves ``RESERVED`` rows to ``RELEASED`` -- never
      ``SETTLED``, never a delete -- so observation-based charging stays
      truthful.

    Returns the number of rows released, or ``-1`` when the lease guard denied
    the write (the caller must treat that as "nothing was settled").
    """
    rid = run_id.strip() if isinstance(run_id, str) else ""
    if not rid:
        return 0
    moment = _utcnow(now)
    ensure_tables(db)
    where = "run_id = ? AND state = 'RESERVED'"
    params: list[Any] = [rid]
    if job_id is not None:
        where += " AND job_id = ?"
        params.append(job_id)
    with owner_connection(db) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            return 0
        if lease_guard is not None and not bool(lease_guard(conn)):
            conn.rollback()
            return -1
        cur = conn.execute(
            f"UPDATE capacity_reservations SET state = 'RELEASED', settled_at = ?"
            f" WHERE {where}",
            (moment.isoformat(), *params),
        )
        conn.commit()
        return int(cur.rowcount or 0)


def _default_window_kinds() -> dict[tuple[str, str], str]:
    """Resolve window kinds from the effective plan set.

    Never called with test isolation broken: tests always pass explicit
    ``window_kinds`` or isolate ``XDG_*``. Missing windows are UNKNOWN.
    """
    try:
        from saberops.control_plane.policy import load_control_plane_policy

        policy = load_control_plane_policy()
        kinds: dict[tuple[str, str], str] = {}
        for pool_id, plan in policy.plans.pool_plans.items():
            for window in plan.windows:
                kinds[(pool_id, window.id)] = str(window.kind or "UNKNOWN").upper()
        return kinds
    except Exception:
        return {}


def _latest_observation(
    conn: sqlite3.Connection, pool_id: str, window_id: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM capacity_observations"
        " WHERE pool_id = ? AND window_id = ?"
        " ORDER BY observed_at DESC, id DESC LIMIT 1",
        (pool_id, window_id),
    ).fetchone()
    return dict(row) if row is not None else None


def _counted_reservations(
    conn: sqlite3.Connection, pool_id: str, window_id: str, cutoff_iso: str,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT cost_pct, spread_pct, created_at FROM capacity_reservations"
        " WHERE pool_id = ? AND window_id = ?"
        " AND state IN ('RESERVED', 'SETTLED') AND created_at >= ?",
        (pool_id, window_id, cutoff_iso),
    ).fetchall()
    return [dict(r) for r in rows]


def _active_exclusion(
    db: Database, pool_id: str, window_id: str, moment: datetime,
) -> dict[str, Any] | None:
    try:
        active = list_active_exclusions(db, now=moment)
    except Exception:
        return None
    for exc in active:
        if str(exc.get("pool_id") or "") != pool_id:
            continue
        exc_window = str(exc.get("window_id") or "ALL")
        if exc_window != window_id and exc_window != "ALL":
            continue
        try:
            if not is_excluding(exc, moment):
                continue
        except Exception:
            continue
        return exc
    return None


def estimate(
    db: Database,
    pool_id: str,
    window_id: str,
    *,
    now: datetime,
    window_kinds: Mapping[tuple[str, str], str] | None = None,
) -> LedgerEstimate:
    """Estimate remaining capacity for one ``(pool_id, window_id)`` key."""
    moment = _utcnow(now)
    pool = pool_id.strip()
    window = (window_id or "ALL").strip() or "ALL"
    if window_kinds is not None:
        kind = str(window_kinds.get((pool, window), "UNKNOWN")).upper()
    else:
        kind = str(_default_window_kinds().get((pool, window), "UNKNOWN")).upper()
    if kind not in ("CALENDAR", "ROLLING", "UNKNOWN"):
        kind = "UNKNOWN"

    obs: dict[str, Any] | None = None
    try:
        with _read_connection(db) as conn:
            obs = _latest_observation(conn, pool, window)
            if obs is None and window != "ALL":
                obs = _latest_observation(conn, pool, "ALL")
            counted: list[dict[str, Any]] = []
            if obs is not None:
                obs_reset_raw = obs.get("reset_at")
                obs_reset = parse_iso(obs_reset_raw) if isinstance(
                    obs_reset_raw, str) else None
                if obs_reset is not None and moment >= obs_reset:
                    # Window roll: reservations before the reset stop
                    # counting; post-reset reservations draw from 100.
                    counted = _counted_reservations(
                        conn, pool, window, obs_reset.isoformat())
                    spent = sum(float(r.get("cost_pct") or 0.0) for r in counted)
                    spread = sum(float(r.get("spread_pct") or 0.0) for r in counted)
                    remaining = max(0.0, min(100.0, 100.0 - spent))
                    rolled_stale = kind != "CALENDAR"
                    return LedgerEstimate(
                        pool_id=pool, window_id=window,
                        remaining_est=remaining, uncertainty=spread,
                        observed_at=str(obs.get("observed_at")),
                        source=str(obs.get("source")),
                        stale=rolled_stale, excluded_until=None,
                    )
                counted = _counted_reservations(
                    conn, pool, window,
                    str(obs.get("observed_at") or ""),
                )
    except sqlite3.OperationalError:
        obs = None
        counted = []

    exc = _active_exclusion(db, pool, window, moment)
    if exc is not None:
        raw_until = exc.get("until")
        until = raw_until if isinstance(raw_until, str) else None
        obs_at: str | None = None
        if obs is not None and isinstance(obs.get("observed_at"), str):
            obs_at = str(obs.get("observed_at"))
        return LedgerEstimate(
            pool_id=pool, window_id=window, remaining_est=0.0,
            uncertainty=0.0, observed_at=obs_at, source="EXHAUSTION",
            stale=False, excluded_until=until,
        )

    if obs is None:
        return LedgerEstimate(
            pool_id=pool, window_id=window, remaining_est=None,
            uncertainty=0.0, observed_at=None, source=None,
            stale=True, excluded_until=None,
        )
    spent = sum(float(r.get("cost_pct") or 0.0) for r in counted)
    spread = sum(float(r.get("spread_pct") or 0.0) for r in counted)
    remaining = max(0.0, min(100.0, float(cast(float, obs.get("remaining_pct"))) - spent))
    obs_source = str(obs.get("source") or "")
    uncertainty = spread + (1.0 if obs_source == "PROBE" else 0.0)
    obs_at_raw = obs.get("observed_at")
    obs_at_str = obs_at_raw if isinstance(obs_at_raw, str) else None
    stale = freshness(obs_at_str, window, obs_source or None, moment) != "FRESH"
    return LedgerEstimate(
        pool_id=pool, window_id=window, remaining_est=remaining,
        uncertainty=uncertainty, observed_at=obs_at_str,
        source=obs_source or None, stale=stale, excluded_until=None,
    )


def snapshot(db: Database, now: datetime) -> list[LedgerEstimate]:
    """Estimate every key with an observation, reservation or exclusion."""
    moment = _utcnow(now)
    keys: set[tuple[str, str]] = set()
    try:
        with _read_connection(db) as conn:
            for row in conn.execute(
                "SELECT DISTINCT pool_id, window_id FROM capacity_observations"
            ).fetchall():
                keys.add((str(row["pool_id"]), str(row["window_id"])))
            for row in conn.execute(
                "SELECT DISTINCT pool_id, window_id FROM capacity_reservations"
                " WHERE state IN ('RESERVED', 'SETTLED')"
            ).fetchall():
                keys.add((str(row["pool_id"]), str(row["window_id"])))
    except sqlite3.OperationalError:
        return []
    try:
        for exc in list_active_exclusions(db, now=moment):
            pool_raw = exc.get("pool_id")
            window_raw = exc.get("window_id")
            if isinstance(pool_raw, str) and pool_raw:
                window_key = (
                    window_raw
                    if isinstance(window_raw, str) and window_raw
                    else "ALL"
                )
                keys.add((pool_raw, window_key))
    except Exception:
        pass
    out = [estimate(db, pool, window, now=moment) for pool, window in sorted(keys)]
    return out


def post_observation_charges(
    db: Database, now: datetime
) -> dict[tuple[str, str], float]:
    """Return map of (pool_id, window_id) -> total cost_pct of counted reservations/settled rows.

    Fail-closed schema rule (ADR-0002 D2/D7, F2 follow-up): ``{}`` is only
    returned when there is positive, authoritative proof that **every**
    required capacity table is absent, i.e. a genuinely uninitialized owner
    DB.  If the schema is partially initialized, if schema discovery itself
    errors, or if any read fails, the underlying ``sqlite3`` error is
    raised so the boundary converts it into a typed
    ``QUOTA_EVIDENCE_REQUIRED`` denial instead of treating unknown
    obligations as zero debt.
    """
    moment = _utcnow(now)
    charges: dict[tuple[str, str], float] = {}
    with _read_connection(db) as conn:
        present = capacity_tables_present(conn)
        missing = REQUIRED_CAPACITY_TABLES - present
        if not present:
            # Positive proof that no required capacity table exists: the
            # legacy graceful "unconfigured, no debt" path.
            return {}
        if missing:
            raise sqlite3.OperationalError(
                "capacity schema partially initialized; missing required"
                " tables: " + ", ".join(sorted(missing))
            )
        keys: set[tuple[str, str]] = set()
        for row in conn.execute(
            "SELECT DISTINCT pool_id, window_id FROM capacity_observations"
        ).fetchall():
            keys.add((str(row["pool_id"]), str(row["window_id"])))
        for row in conn.execute(
            "SELECT DISTINCT pool_id, window_id FROM capacity_reservations"
            " WHERE state IN ('RESERVED', 'SETTLED')"
        ).fetchall():
            keys.add((str(row["pool_id"]), str(row["window_id"])))

        for pool, window in keys:
            obs = _latest_observation(conn, pool, window)
            if obs is None and window != "ALL":
                obs = _latest_observation(conn, pool, "ALL")
            if obs is not None:
                obs_reset_raw = obs.get("reset_at")
                obs_reset = (
                    parse_iso(obs_reset_raw)
                    if isinstance(obs_reset_raw, str)
                    else None
                )
                if obs_reset is not None and moment >= obs_reset:
                    cutoff = obs_reset.isoformat()
                else:
                    cutoff = str(obs.get("observed_at") or "")
            else:
                cutoff = ""
            counted = _counted_reservations(conn, pool, window, cutoff)
            spent = sum(float(r.get("cost_pct") or 0.0) for r in counted)
            charges[(pool, window)] = spent
    return charges


__all__ = [
    "LedgerEstimate",
    "PoolDraw",
    "REQUIRED_CAPACITY_TABLES",
    "Reservation",
    "ReservedDebt",
    "capacity_tables_present",
    "record_owner_quota",
    "record_probe_window_observation",
    "list_owner_quota",
    "get_owner_quota",
    "enable_owner_quota",
    "reserve",
    "settle",
    "release",
    "list_reserved_debts",
    "release_terminal_debt",
    "estimate",
    "snapshot",
    "post_observation_charges",
]
