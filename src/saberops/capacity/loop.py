"""Scheduler loop, single-instance lease, and atomic dispatch (ADR-0002 D7).

Implements:
- Owner DB single-instance scheduler lease with transactional claim, renewal,
  expiry takeover, and fencing token.
- Scheduler tick: startup/periodic reconciliation of terminal runs, pure
  schedule() evaluation, probe dispatch, wait status updates, and atomic
  job dispatch with idempotent ledger reservations and rollback compensation.
- Scheduler loop runner with 60s ticks, wake-event support, and --once mode.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from saberops.capacity.cost_model import CostState
from saberops.capacity.exclusions import parse_iso
from saberops.capacity.ledger import (
    PoolDraw,
    post_observation_charges,
    release,
    reserve,
    settle,
)
from saberops.capacity.probes import run_due_probes
from saberops.capacity.scheduler import (
    DispatchDecision,
    ProbeDecision,
    SchedulerDecision,
    WaitDecision,
    schedule,
)
from saberops.capacity.snapshot import effective_quota_states
from saberops.capacity.store import (
    ensure_tables as ensure_capacity_tables,
)
from saberops.capacity.store import (
    owner_connection,
)
from saberops.control_plane.policy import (
    ControlPlanePolicy,
    effective_global_slot_limit,
    load_control_plane_policy,
)
from saberops.jobs import store
from saberops.jobs.models import (
    Job,
    JobState,
    WaitingReason,
)
from saberops.jobs.service import (
    ReconcileChange,
    reconcile_jobs,
)
from saberops.jobs.store import (
    JobConflictError,
    JobNotFoundError,
    JobTransitionError,
)
from saberops.models import RunStatus
from saberops.routing_config import RoutingSnapshot, load_effective_routing

DEFAULT_LEASE_NAME = "scheduler"
DEFAULT_LEASE_TTL = 30.0
DEFAULT_TICK_INTERVAL = 60.0


def _utcnow(now: datetime | None) -> datetime:
    if now is not None:
        return now.astimezone(UTC) if now.tzinfo else now.replace(tzinfo=UTC)
    return datetime.now(UTC)


_SCHEDULER_WAKE_EVENT = threading.Event()


def wake_scheduler() -> None:
    """Signal the scheduler loop to evaluate a tick immediately."""
    _SCHEDULER_WAKE_EVENT.set()



@dataclass(frozen=True)
class SchedulerLease:
    lease_name: str
    holder_id: str
    fencing_token: int
    acquired_at: str
    expires_at: str


@dataclass(frozen=True)
class TickResult:
    ran: bool
    reason: str | None = None
    reconciled_changes: tuple[ReconcileChange, ...] = ()
    dispatched: int = 0
    probed: int = 0
    waited: int = 0
    decisions: tuple[SchedulerDecision, ...] = ()
    # ADR-0002 D8 crash recovery (R1 + R2) evidence for this tick.
    preemption_settled: int = 0
    preemption_pending: tuple[str, ...] = ()
    preemption_blocked: tuple[str, ...] = ()
    # ADR-0002 D7 legacy-RUNNING hold diagnostics (owner-visible job ids).
    # ``legacy_running_jobs``: RUNNING jobs with no ``current_run_id`` or no
    # live RESERVED reservation row, held by
    # ``reason="legacy_running_without_reservation"``.
    # ``unresolved_running_jobs``: RUNNING jobs whose run is invisible to the
    # tick's status lookup (e.g. project-local run in another store), held by
    # ``reason="running_run_status_unresolved"`` instead of being failed as
    # RUN_MISSING.  Both are pure functions of durable state, so a repeated
    # tick reports the same diagnostics without any side effect.
    legacy_running_jobs: tuple[str, ...] = ()
    unresolved_running_jobs: tuple[str, ...] = ()
    #: Typed GS-1 storage/SQLite infrastructure reason for this tick (ADR-0003
    #: D7 / Amendment 1).  ``None`` when storage maintenance was not invoked or
    #: reported healthy.  A non-``None`` value is an INFRA park, never a code
    #: failure and never a pass.
    storage_infra_reason: str | None = None


def acquire_lease(
    db: Any,
    holder_id: str,
    ttl_seconds: float = DEFAULT_LEASE_TTL,
    *,
    lease_name: str = DEFAULT_LEASE_NAME,
    now: datetime | None = None,
) -> SchedulerLease | None:
    """Attempt to acquire or renew the single-instance scheduler lease.

    Transactional claim using SQLite BEGIN IMMEDIATE in the owner DB:
    - If no lease exists: creates one with fencing_token = 1.
    - If held by the same holder_id: extends expires_at.
    - If expired: takes over the lease and increments fencing_token.
    - If held by another instance and not expired: returns None (denied).
    """
    ensure_capacity_tables(db)
    moment = _utcnow(now)
    now_iso = moment.isoformat()
    expires_iso = (moment + timedelta(seconds=ttl_seconds)).isoformat()
    clean_holder = str(holder_id).strip()
    if not clean_holder:
        raise ValueError("holder_id must be a non-empty string")

    with owner_connection(db) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            return None
        row = conn.execute(
            "SELECT lease_name, holder_id, acquired_at, expires_at, fencing_token "
            "FROM capacity_scheduler_lease WHERE lease_name = ?",
            (lease_name,),
        ).fetchone()

        if row is None:
            # First acquisition
            fencing_token = 1
            try:
                conn.execute(
                    "INSERT INTO capacity_scheduler_lease "
                    "(lease_name, holder_id, acquired_at, expires_at, fencing_token) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (lease_name, clean_holder, now_iso, expires_iso, fencing_token),
                )
                conn.commit()
                return SchedulerLease(
                    lease_name=lease_name,
                    holder_id=clean_holder,
                    fencing_token=fencing_token,
                    acquired_at=now_iso,
                    expires_at=expires_iso,
                )
            except sqlite3.IntegrityError:
                conn.rollback()
                row = conn.execute(
                    "SELECT lease_name, holder_id, acquired_at, expires_at, fencing_token "
                    "FROM capacity_scheduler_lease WHERE lease_name = ?",
                    (lease_name,),
                ).fetchone()
                if row is None:
                    return None

        current_holder = str(row["holder_id"])
        current_token = int(row["fencing_token"])
        current_expires_raw = row["expires_at"]
        current_expires = (
            parse_iso(current_expires_raw)
            if isinstance(current_expires_raw, str)
            else None
        )
        is_expired = current_expires is None or moment >= current_expires

        if current_holder == clean_holder:
            # Same holder: extend expiration
            conn.execute(
                "UPDATE capacity_scheduler_lease SET expires_at = ? "
                "WHERE lease_name = ? AND holder_id = ? AND fencing_token = ?",
                (expires_iso, lease_name, clean_holder, current_token),
            )
            conn.commit()
            return SchedulerLease(
                lease_name=lease_name,
                holder_id=clean_holder,
                fencing_token=current_token,
                acquired_at=str(row["acquired_at"]),
                expires_at=expires_iso,
            )

        if is_expired:
            # Lease expired: takeover by new holder with incremented fencing token
            new_token = current_token + 1
            cur = conn.execute(
                "UPDATE capacity_scheduler_lease SET holder_id = ?, acquired_at = ?, "
                "expires_at = ?, fencing_token = ? "
                "WHERE lease_name = ? AND fencing_token = ?",
                (clean_holder, now_iso, expires_iso, new_token, lease_name, current_token),
            )
            if (cur.rowcount or 0) > 0:
                conn.commit()
                return SchedulerLease(
                    lease_name=lease_name,
                    holder_id=clean_holder,
                    fencing_token=new_token,
                    acquired_at=now_iso,
                    expires_at=expires_iso,
                )

        # Held by another live holder
        return None


def renew_lease(
    db: Any,
    lease: SchedulerLease,
    ttl_seconds: float = DEFAULT_LEASE_TTL,
    *,
    now: datetime | None = None,
) -> SchedulerLease | None:
    """Heartbeat renewal of an active lease; returns None if lease was lost."""
    ensure_capacity_tables(db)
    moment = _utcnow(now)
    expires_iso = (moment + timedelta(seconds=ttl_seconds)).isoformat()

    with owner_connection(db) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            return None
        cur = conn.execute(
            "UPDATE capacity_scheduler_lease SET expires_at = ? "
            "WHERE lease_name = ? AND holder_id = ? AND fencing_token = ?",
            (expires_iso, lease.lease_name, lease.holder_id, lease.fencing_token),
        )
        if (cur.rowcount or 0) > 0:
            conn.commit()
            return replace(lease, expires_at=expires_iso)
        return None


def release_lease(
    db: Any,
    lease: SchedulerLease,
    *,
    now: datetime | None = None,
) -> bool:
    """Release the lease on shutdown."""
    ensure_capacity_tables(db)
    with owner_connection(db) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            return False
        cur = conn.execute(
            "DELETE FROM capacity_scheduler_lease "
            "WHERE lease_name = ? AND holder_id = ? AND fencing_token = ?",
            (lease.lease_name, lease.holder_id, lease.fencing_token),
        )
        conn.commit()
        return (cur.rowcount or 0) > 0


def check_lease(
    db: Any,
    lease: SchedulerLease,
    *,
    now: datetime | None = None,
) -> bool:
    """Verify that this lease is still valid in the owner DB."""
    ensure_capacity_tables(db)
    moment = _utcnow(now)
    with owner_connection(db) as conn:
        row = conn.execute(
            "SELECT holder_id, expires_at, fencing_token "
            "FROM capacity_scheduler_lease WHERE lease_name = ?",
            (lease.lease_name,),
        ).fetchone()
        if row is None:
            return False
        if str(row["holder_id"]) != lease.holder_id:
            return False
        if int(row["fencing_token"]) != lease.fencing_token:
            return False
        expires_raw = row["expires_at"]
        expires_dt = parse_iso(expires_raw) if isinstance(expires_raw, str) else None
        if expires_dt is None or moment >= expires_dt:
            return False
        return True


def _lease_still_valid(
    conn: sqlite3.Connection, lease: SchedulerLease, moment: datetime
) -> bool:
    """Verify *lease* against the lease row inside an open owner transaction.

    Used as the ``lease_guard`` for capacity settlement so a scheduler whose
    lease expired (or was taken over) between discovery and the write commits
    nothing.  Any missing/malformed row fails closed.
    """
    row = conn.execute(
        "SELECT holder_id, expires_at, fencing_token"
        " FROM capacity_scheduler_lease WHERE lease_name = ?",
        (lease.lease_name,),
    ).fetchone()
    if row is None:
        return False
    if str(row["holder_id"]) != lease.holder_id:
        return False
    if int(row["fencing_token"]) != lease.fencing_token:
        return False
    raw = row["expires_at"]
    expires = parse_iso(raw) if isinstance(raw, str) else None
    return expires is not None and moment < expires


def _lookup_run_status(db: Any, run_id: str) -> RunStatus | None:
    try:
        run = db.get_run(run_id)
        return run.status if run is not None else None
    except Exception:
        return None


def _load_all_cost_states(db: Any) -> dict[tuple[str, str, str, str], CostState]:
    ensure_capacity_tables(db)
    out: dict[tuple[str, str, str, str], CostState] = {}
    with owner_connection(db) as conn:
        rows = conn.execute(
            "SELECT route, pool_id, window_id, size_bucket, samples, ewma_pct "
            "FROM capacity_cost_model"
        ).fetchall()
        for r in rows:
            route = str(r["route"])
            pool_id = str(r["pool_id"])
            window_id = str(r["window_id"])
            size_bucket = str(r["size_bucket"])
            samples = int(r["samples"])
            ewma = float(r["ewma_pct"]) if r["ewma_pct"] is not None else None
            out[(route, pool_id, window_id, size_bucket)] = CostState(
                samples=samples, ewma_pct=ewma
            )
    return out


def _default_run_launcher(
    db: Any, job: Job, decision: DispatchDecision, run_id: str
) -> Any:
    from saberops.jobs.service import job_dispatch_spec
    from saberops.models import Run
    from saberops.preemption import prepare_resume
    from saberops.run_creation import resolve_effective_run_config
    from saberops.supervisor import RunSupervisor

    spec = job_dispatch_spec(job)
    cand = decision.candidate
    req = replace(spec.request, provider_override=cand.provider, model_override=cand.model)
    config = resolve_effective_run_config(req)

    run = Run(
        id=run_id,
        task=config.task,
        target_repo=config.target_repo,
        base_commit=spec.base_sha,
        tier=config.initial_tier,
        training_allowed=config.training_allowed,
        gate_command=config.gate_command,
        status=RunStatus.PENDING,
        created_at=datetime.now(UTC).isoformat(),
        config_json=json.dumps(config.as_dict()),
    )
    db.create_run(run)
    # ADR-0002 D8 (M2-5) durable resume wiring -- and ONLY resume wiring:
    # when the dispatched job carries a verified preemption capsule, stage
    # the checkpoint plus capsule into the new run through its ordinary
    # worker-checkpoint channel.  The new run is still anchored at the
    # original job base and still passes the full M2-4b posture / quota /
    # readiness / lease / concurrency guard at (and inside) this dispatch;
    # a malformed resume stages nothing and the run launches fresh, with a
    # ``resume_skipped`` event recording the exact cause.  Admission,
    # ledger, pool scheduler, and capacity safety invariants are untouched.
    if job.resume_from is not None:
        prepare_resume(db, job=job, new_run_id=run_id, repo_path=job.repo)
    supervisor = RunSupervisor(db=db)
    supervisor.ensure_running(run_id, config)
    return run_id


def scheduler_tick(
    db: Any,
    lease: SchedulerLease,
    *,
    now: datetime | None = None,
    run_launcher: Callable[[Job, DispatchDecision, str], Any] | None = None,
    run_status: Callable[[str], RunStatus | None] | None = None,
    probes: dict[str, Any] | None = None,
    policy: ControlPlanePolicy | None = None,
    routes: RoutingSnapshot | None = None,
    global_slot_limit: int | None = None,
    max_concurrent_override: int | None = None,
    background_min_pace: float = 4.0,
    clock_fn: Callable[[], datetime] | None = None,
    readiness_service: Any = None,
    launch_guard: Callable[[], str | None] | None = None,
) -> TickResult:
    """Execute one complete scheduler tick.

    0. ADR-0002 D7 fail-closed RUNNING accounting: refuse the whole tick when
       any RUNNING job has no trustworthy capacity reservation, or when the
       owner status lookup cannot see a RUNNING job's run (a project-local
       run in a distinct store).  Zero schedule/dispatch/CAS/provider calls.
    1. Verify scheduler lease.
    2. Reconcile terminal runs and settle/release their reservations.
    3. Query queued/running jobs and system state.
    4. Call pure schedule().
    5. Execute probe decisions (forced probes).
    6. Apply wait decisions (set_waiting on jobs).
    7. Execute dispatch decisions atomically:
       - Verify lease
       - CAS mark_dispatched (QUEUED -> RUNNING)
       - Idempotent reservation in capacity ledger
       - Verify lease
       - Invoke run_launcher
       - On error or lost lease: compensate by requeueing and releasing reservation.

    ``launch_guard`` (GS-1 / ADR-0003 D4) is consulted BEFORE any dispatch,
    reservation or claim transition.  When it returns a typed reason the whole
    tick parks with zero side effects -- no job leaves QUEUED, no ledger row is
    booked and no launcher runs.  The fixed floor is the current guard; adaptive
    admission stays inactive.
    """
    moment = _utcnow(now)

    def _current_moment() -> datetime:
        if clock_fn is not None:
            return clock_fn()
        return _utcnow(None)

    if not check_lease(db, lease, now=_current_moment()):
        return TickResult(ran=False, reason="lost_lease")

    if launch_guard is not None:
        guard_reason = launch_guard()
        if guard_reason is not None:
            return TickResult(
                ran=False, reason="storage_infra", storage_infra_reason=guard_reason
            )

    status_fn = run_status or (lambda r_id: _lookup_run_status(db, r_id))
    # One status snapshot per tick: the fail-closed RUNNING guard, the RUNNING
    # reconciler and the D8 preemption reconciler all ask about the same runs,
    # so share a single memoized owner lookup instead of repeating the read
    # once per caller.
    status_cache: dict[str, RunStatus | None] = {}

    def tick_status(run_id: str) -> RunStatus | None:
        if run_id not in status_cache:
            status_cache[run_id] = status_fn(run_id)
        return status_cache[run_id]

    # 0. Fail-closed RUNNING accounting (ADR-0002 D7 legacy reservations).
    #
    # Per-pool occupancy and outstanding debt are derived from RESERVED
    # ``capacity_reservations`` rows.  A legacy/upgraded job persisted in
    # RUNNING state with no reservation row (or no ``current_run_id``) would
    # therefore read as ZERO occupancy and ZERO debt, so the per-pool guard in
    # ``admit()`` could over-admit and the red-line check could pass on an
    # already-drained pool while the global ceiling alone still held.  A
    # successful read that returns no rows is NOT proof the pool is free.
    #
    # Before anything is reconciled, scheduled, or dispatched we require EVERY
    # RUNNING job to hold a live ``RESERVED`` row.  Any job that does not
    # holds the whole tick (zero reconcile side effects, zero dispatch, zero
    # CAS, zero provider calls) with an owner-visible diagnostic.  We never
    # backfill a reservation or auto-promote the job: an owner-approved
    # promotion rule is a separate decision.
    #
    # A persisted RUNNING row whose ``current_run_id`` is NULL or empty cannot
    # be represented by the ``Job`` model (``__post_init__`` rejects RUNNING
    # without a run id), so ``store.list_jobs_raw`` below would abort the whole
    # tick with a ``ValueError`` instead of surfacing the clean hold.  The
    # ``jobs.current_run_id`` column is nullable and legacy/upgraded owner DBs
    # can really contain such rows, so read that raw persisted state with one
    # deterministic, schema-safe owner SELECT BEFORE materialising any ``Job``.
    # ``ensure_tables`` is idempotent, so a fresh empty owner DB is initialised
    # here (an absent table is never read as legacy occupancy) and a genuine
    # read error still fails closed as ``ledger_read_error``.
    owner_store = getattr(db, "owner_db", db)
    try:
        store.ensure_tables(db)
        with owner_store._get_connection() as conn:
            null_run_rows = conn.execute(
                "SELECT job_id FROM jobs WHERE state = ?"
                " AND (current_run_id IS NULL OR current_run_id = '')",
                (JobState.RUNNING.value,),
            ).fetchall()
    except (sqlite3.OperationalError, sqlite3.DatabaseError):
        return TickResult(ran=False, reason="ledger_read_error")
    if null_run_rows:
        return TickResult(
            ran=False,
            reason="legacy_running_without_reservation",
            legacy_running_jobs=tuple(str(row["job_id"]) for row in null_run_rows),
        )

    try:
        running_snapshot = store.list_jobs_raw(db, state=JobState.RUNNING, limit=100_000)
    except (sqlite3.OperationalError, sqlite3.DatabaseError):
        return TickResult(ran=False, reason="ledger_read_error")

    reserved_run_ids: set[str] = set()
    if running_snapshot:
        try:
            with owner_store._get_connection() as conn:
                reserved_rows = conn.execute(
                    "SELECT DISTINCT run_id FROM capacity_reservations"
                    " WHERE state = 'RESERVED'"
                ).fetchall()
        except (sqlite3.OperationalError, sqlite3.DatabaseError):
            return TickResult(ran=False, reason="ledger_read_error")
        reserved_run_ids = {str(row["run_id"]) for row in reserved_rows}

    legacy_running_jobs: list[str] = []
    unresolved_running_jobs: list[str] = []
    for running_job in running_snapshot:
        run_id = running_job.current_run_id
        if not run_id or run_id not in reserved_run_ids:
            legacy_running_jobs.append(running_job.job_id)
            continue
        if tick_status(run_id) is None:
            # The tick's status lookup cannot see this run.  ``reconcile_jobs``
            # would read that as RUN_MISSING, mark the job FAILED and (through
            # the reconciler above) release a reservation that may still belong
            # to a live project-local run held in a different store.  An
            # unknown run is never proof the run is gone, so hold instead and
            # retain the job and its reservation for owner resolution.
            unresolved_running_jobs.append(running_job.job_id)

    if legacy_running_jobs:
        return TickResult(
            ran=False,
            reason="legacy_running_without_reservation",
            legacy_running_jobs=tuple(legacy_running_jobs),
        )
    if unresolved_running_jobs:
        return TickResult(
            ran=False,
            reason="running_run_status_unresolved",
            unresolved_running_jobs=tuple(unresolved_running_jobs),
        )

    # 1. Startup and periodic reconciliation
    changes = reconcile_jobs(db, run_status=tick_status, now=moment)
    for ch in changes:
        if ch.to_state == JobState.SUCCEEDED:
            settle(db, ch.run_id, total_tokens=None, now=moment)
        elif ch.to_state in (JobState.FAILED, JobState.CANCELLED):
            release(db, ch.run_id, now=moment)
        elif ch.to_state == JobState.QUEUED:
            # ADR-0002 D8 (M2-5): a completed preemption requeue.  The old
            # run is conclusively terminal (PREEMPTED), so its reservation
            # is released exactly once here; release is idempotent, so this
            # is also safe when the coordinator already released it.
            release(db, ch.run_id, now=moment)

    # 1b. ADR-0002 D8 crash recovery (R1 + R2).  This is the restart entry
    # point for two gaps that the RUNNING-only job reconciliation above cannot
    # see:
    #   R1 -- a job already requeued (QUEUED / terminal / re-dispatched) still
    #         owes its OLD run's RESERVED capacity; the debt is discovered from
    #         durable owner storage, not from job state.
    #   R2 -- a run interrupted between its durable D8 decision and
    #         terminalization; the supervisor finishes it BEFORE generic
    #         orphaning, only for a provably dead owner + quiescent worker.
    # It never decides a route, calls a provider, or frees capacity on an
    # unverified receipt, and it is fully idempotent across restarts.
    from saberops.jobs.service import reconcile_preemptions
    from saberops.supervisor import RunSupervisor

    def _preempt_lease_guard(conn: sqlite3.Connection) -> bool:
        return _lease_still_valid(conn, lease, _current_moment())

    def _preempt_recover(run_id: str) -> Any:
        return RunSupervisor(db=db).reconcile(run_id)

    try:
        preempt_recon = reconcile_preemptions(
            db,
            run_status=tick_status,
            recover=_preempt_recover,
            lease_guard=_preempt_lease_guard,
            now=moment,
        )
    except (sqlite3.OperationalError, sqlite3.DatabaseError):
        return TickResult(ran=False, reason="ledger_read_error")
    except Exception:
        preempt_recon = None

    # 2. Collect scheduling inputs
    queued_jobs = store.list_jobs_raw(db, state=JobState.QUEUED, limit=100_000)
    running_jobs = store.list_jobs_raw(db, state=JobState.RUNNING, limit=100_000)
    effective_routes = routes or load_effective_routing()
    effective_pol = policy or load_control_plane_policy()

    # Resolve the effective aggregate top-level job ceiling (D7/F3).
    # The owner-configured concurrency.max_global (or the deterministic
    # default) is the authority; the CLI --max-concurrent override and any
    # explicit programmatic global_slot_limit are LOWERING-ONLY additional
    # inputs (effective = min of all), so neither can raise admission above
    # the owner's cap. Invalid inputs fail closed.
    try:
        effective_global_limit: int = effective_global_slot_limit(
            effective_pol,
            override=max_concurrent_override,
            injected=global_slot_limit,
        )
    except ValueError:
        # Invalid override / injected limit / config fails closed: no
        # dispatch this tick, never "unlimited".
        return TickResult(ran=False, reason="invalid_global_concurrency")

    quotas = effective_quota_states(db, now=moment)
    cost_states = _load_all_cost_states(db)
    try:
        charges = post_observation_charges(db, now=moment)
    except (sqlite3.OperationalError, sqlite3.DatabaseError):
        return TickResult(ran=False, reason="ledger_read_error")

    # Derive real running counts per pool from running jobs (F3).
    # Fail closed (R3-M1): per-pool occupancy is authoritative owner-DB state.
    # If the reservation scan for any RUNNING job cannot be read, the pool
    # occupancy is UNKNOWN, never zero — a partially counted map must never
    # reach admission (ADR-0002 D7 requires a free pool slot as well as a free
    # global slot). Abort the tick with zero dispatches; do not silently skip a
    # failed job. Only targeted DB read errors are absorbed here; unrelated
    # programming errors are not hidden.
    running_counts: dict[str, int] = {}
    for rj in running_jobs:
        if not rj.current_run_id:
            continue
        try:
            owner = getattr(db, "owner_db", db)
            with owner._get_connection() as conn:
                rows = conn.execute(
                    "SELECT DISTINCT pool_id FROM capacity_reservations"
                    " WHERE run_id = ? AND state = 'RESERVED'",
                    (rj.current_run_id,),
                ).fetchall()
        except (sqlite3.OperationalError, sqlite3.DatabaseError):
            return TickResult(ran=False, reason="ledger_read_error")
        for r in rows:
            p = str(r["pool_id"])
            running_counts[p] = running_counts.get(p, 0) + 1

    # Snapshot readiness/executable availability per provider (F12)
    readiness_map: dict[str, Any] = {}
    if readiness_service is not None:
        try:
            for role_name in ("worker", "surgeon"):
                try:
                    for grp in effective_routes.groups_for(role_name):
                        for c in grp:
                            if c.provider not in readiness_map:
                                readiness_map[c.provider] = readiness_service.admission_for(
                                    provider=c.provider
                                )
                except Exception:
                    pass
        except Exception:
            pass

    # 3. Pure deterministic schedule evaluation
    decisions = schedule(
        now=moment,
        queued=queued_jobs,
        running=running_jobs,
        routing_snapshot=effective_routes,
        policy=effective_pol,
        quota_states=quotas,
        cost_states=cost_states,
        running_counts=running_counts,
        reservations=charges,
        global_slot_limit=effective_global_limit,
        background_min_pace=background_min_pace,
        readiness_states=readiness_map if readiness_map else None,
    )

    dispatched_count = 0
    probed_count = 0
    waited_count = 0

    pool_policies = (
        effective_pol.plans.pool_plans
        if effective_pol and effective_pol.plans
        else {}
    )

    # 4. Execute decisions
    for dec in decisions:
        if isinstance(dec, ProbeDecision):
            run_due_probes(
                db,
                force_harnesses=[dec.harness],
                probes=probes,
                pool_policies=pool_policies,
                now=moment,
            )
            probed_count += 1

        elif isinstance(dec, WaitDecision):
            eta_iso = dec.eta.isoformat() if dec.eta else None
            storable_reason = dec.reason
            try:
                WaitingReason(str(storable_reason))
            except ValueError:
                storable_reason = WaitingReason.NO_CAPACITY.value
            try:
                store.set_waiting(
                    db,
                    dec.job.job_id,
                    reason=storable_reason,
                    eta=eta_iso,
                    eta_confidence=dec.confidence,
                    now=moment,
                )
                waited_count += 1
            except (JobConflictError, JobTransitionError, JobNotFoundError, ValueError):
                continue

        elif isinstance(dec, DispatchDecision):
            # Check lease before dispatch
            if not check_lease(db, lease, now=_current_moment()):
                break

            run_id = f"run_{uuid.uuid4().hex[:12]}"

            # Step A: CAS state transition QUEUED -> RUNNING
            try:
                dispatched_job = store.mark_dispatched(
                    db, dec.job.job_id, run_id=run_id, now=moment
                )
            except (JobConflictError, JobTransitionError, JobNotFoundError):
                continue

            # Step B: Re-verify lease before reservation
            if not check_lease(db, lease, now=_current_moment()):
                try:
                    store.requeue(
                        db,
                        dec.job.job_id,
                        reason=WaitingReason.NO_CAPACITY,
                        now=_current_moment(),
                    )
                except Exception:
                    pass
                break

            # Step C: Idempotent reservation in ledger across ALL drawn pools (F2)
            draws: list[PoolDraw] = list(dec.draws) if dec.draws else []
            if not draws:
                target_pool_ids = (
                    list(dec.drawn_pools)
                    if dec.drawn_pools
                    else ([dec.pool_id] if dec.pool_id else [])
                )
                mult = float(dec.multiplier or 1.0)
                for pid in target_pool_ids:
                    pp = pool_policies.get(pid)
                    windows = pp.windows if pp else ()
                    if not windows:
                        draws.append(PoolDraw(pool_id=pid, window_id="ALL", multiplier=mult))
                    else:
                        for w in windows:
                            draws.append(
                                PoolDraw(pool_id=pid, window_id=w.id, multiplier=mult)
                            )

            route_str = f"{dec.candidate.provider}/{dec.candidate.model}"
            bucket = dec.job.requirements.size_hint or "M"

            try:
                reserve(
                    db,
                    run_id=run_id,
                    job_id=dec.job.job_id,
                    route=route_str,
                    draws=draws,
                    size_bucket=bucket,
                    now=moment,
                )
            except Exception:
                # Reservation failed; compensate partial dispatch
                release(db, run_id=run_id, now=_current_moment())
                try:
                    store.requeue(
                        db,
                        dec.job.job_id,
                        reason=WaitingReason.NO_CAPACITY,
                        now=_current_moment(),
                    )
                except Exception:
                    pass
                continue

            # Step D: Re-verify lease right before launch
            if not check_lease(db, lease, now=_current_moment()):
                # Lost lease between reservation and launch; compensate immediately
                release(db, run_id=run_id, now=_current_moment())
                try:
                    store.requeue(
                        db,
                        dec.job.job_id,
                        reason=WaitingReason.NO_CAPACITY,
                        now=_current_moment(),
                    )
                except Exception:
                    pass
                break

            # Step E: Launch run
            try:
                if run_launcher is not None:
                    run_launcher(dispatched_job, dec, run_id)
                else:
                    _default_run_launcher(db, dispatched_job, dec, run_id)
                dispatched_count += 1
            except Exception:
                # Launch failed; inspect durable run existence (F11)
                run_exists = False
                try:
                    if hasattr(db, "get_run") and db.get_run(run_id) is not None:
                        run_exists = True
                except Exception:
                    run_exists = True

                if run_exists:
                    # Run row exists or status unknown: do NOT requeue, do NOT release reservation.
                    # Leave job in RUNNING for reconciler / supervisor recovery.
                    continue

                # Proven no side effects occurred: safe to compensate partial dispatch
                release(db, run_id=run_id, now=_current_moment())
                try:
                    store.requeue(
                        db,
                        dec.job.job_id,
                        reason=WaitingReason.NO_CAPACITY,
                        now=_current_moment(),
                    )
                except Exception:
                    pass

    return TickResult(
        ran=True,
        reconciled_changes=tuple(changes),
        dispatched=dispatched_count,
        probed=probed_count,
        waited=waited_count,
        decisions=tuple(decisions),
        preemption_settled=(
            preempt_recon.settlement.released if preempt_recon is not None else 0
        ),
        preemption_pending=(
            preempt_recon.pending if preempt_recon is not None else ()
        ),
        preemption_blocked=(
            preempt_recon.blocked if preempt_recon is not None else ()
        ),
    )


def _storage_maintenance_interval() -> timedelta:
    """Cadence between periodic storage maintenance passes (GS-1)."""
    from saberops.storage_maintenance import MAINTENANCE_INTERVAL

    return MAINTENANCE_INTERVAL


def _invoke_storage_maintenance(
    maintenance: Callable[[str], str | None], trigger: str
) -> str | None:
    """Invoke bounded storage maintenance without ever raising into the loop.

    A storage/SQLite infrastructure failure becomes a typed reason code so the
    tick reports an INFRA park instead of crashing the scheduler or relaunching
    work (ADR-0003 D7).  Maintenance runs inside its own bounded budget and must
    not monopolise the tick.
    """
    from saberops.sqlite_policy import StorageInfraError

    try:
        return maintenance(trigger)
    except StorageInfraError as exc:
        return exc.reason_code
    except OSError:
        return "STORAGE_MAINTENANCE_OS_ERROR"


def run_scheduler_loop(
    db: Any,
    *,
    once: bool = False,
    tick_interval: float = DEFAULT_TICK_INTERVAL,
    lease_ttl: float = DEFAULT_LEASE_TTL,
    holder_id: str | None = None,
    stop_event: threading.Event | None = None,
    wake_event: threading.Event | None = None,
    run_launcher: Callable[[Job, DispatchDecision, str], Any] | None = None,
    run_status: Callable[[str], RunStatus | None] | None = None,
    probes: dict[str, Any] | None = None,
    policy: ControlPlanePolicy | None = None,
    routes: RoutingSnapshot | None = None,
    global_slot_limit: int | None = None,
    max_concurrent_override: int | None = None,
    background_min_pace: float = 4.0,
    now_fn: Callable[[], datetime] | None = None,
    readiness_service: Any = None,
    storage_maintenance: Callable[[str], str | None] | None = None,
    launch_guard: Callable[[], str | None] | None = None,
) -> TickResult | None:
    """Run the scheduler loop or a single tick (--once).

    Ordering is safety-critical (ADR-0003 D4):

    * Startup maintenance runs BEFORE any tick in both ``--once`` and continuous
      modes.  When it reports a typed infra reason (disk low/unknown, storage
      infra) the loop parks with zero side effects.
    * Periodic maintenance runs at most once per
      :data:`saberops.storage_maintenance.MAINTENANCE_INTERVAL`, before the tick;
      a low reading parks the tick instead of dispatching.
    * ``launch_guard`` is consulted inside every tick before any dispatch, so an
      expensive launch is blocked even between maintenance passes.

    ``storage_maintenance`` receives the trigger name and returns a typed
    storage/SQLite infra reason code (or ``None`` when healthy).  Neither
    callable may raise into the loop.
    """
    clean_holder = holder_id or f"sched_{uuid.uuid4().hex[:8]}"
    now = now_fn() if now_fn else _utcnow(None)

    lease = acquire_lease(db, clean_holder, ttl_seconds=lease_ttl, now=now)
    if lease is None:
        if once:
            return TickResult(ran=False, reason="lease_held_by_other")
        return None

    # Startup maintenance precedes any tick: a below-floor host must not launch
    # work first and report infra afterwards.
    startup_reason: str | None = None
    if storage_maintenance is not None:
        startup_reason = _invoke_storage_maintenance(storage_maintenance, "startup")
    if startup_reason is not None:
        try:
            return TickResult(
                ran=False, reason="storage_infra", storage_infra_reason=startup_reason
            )
        finally:
            release_lease(db, lease, now=now)

    next_maintenance: datetime | None = (
        now + _storage_maintenance_interval() if storage_maintenance is not None else None
    )

    if once:
        try:
            return scheduler_tick(
                db,
                lease,
                now=now,
                run_launcher=run_launcher,
                run_status=run_status,
                probes=probes,
                policy=policy,
                routes=routes,
                global_slot_limit=global_slot_limit,
                max_concurrent_override=max_concurrent_override,
                background_min_pace=background_min_pace,
                clock_fn=now_fn,
                readiness_service=readiness_service,
                launch_guard=launch_guard,
            )
        finally:
            release_lease(db, lease, now=now)

    last_tick: TickResult | None = None
    stop = stop_event or threading.Event()
    wake = wake_event if wake_event is not None else _SCHEDULER_WAKE_EVENT

    try:
        while not stop.is_set():
            now = now_fn() if now_fn else _utcnow(None)
            active_lease = renew_lease(db, lease, ttl_seconds=lease_ttl, now=now)
            if active_lease is None:
                # Lost lease to another instance
                break
            lease = active_lease

            if storage_maintenance is not None and (
                next_maintenance is None or now >= next_maintenance
            ):
                storage_reason = _invoke_storage_maintenance(storage_maintenance, "periodic")
                next_maintenance = now + _storage_maintenance_interval()
                if storage_reason is not None:
                    last_tick = TickResult(
                        ran=False, reason="storage_infra", storage_infra_reason=storage_reason
                    )
                    break

            last_tick = scheduler_tick(
                db,
                lease,
                now=now,
                run_launcher=run_launcher,
                run_status=run_status,
                probes=probes,
                policy=policy,
                routes=routes,
                global_slot_limit=global_slot_limit,
                max_concurrent_override=max_concurrent_override,
                background_min_pace=background_min_pace,
                clock_fn=now_fn,
                readiness_service=readiness_service,
                launch_guard=launch_guard,
            )
            if not last_tick.ran:
                break

            # Wait for wake event or periodic tick timeout
            waited = 0.0
            step = min(tick_interval, 0.5)
            while waited < tick_interval and not stop.is_set():
                if wake.wait(timeout=step):
                    wake.clear()
                    break
                waited += step
    finally:
        now = now_fn() if now_fn else _utcnow(None)
        release_lease(db, lease, now=now)

    return last_tick


__all__ = [
    "DEFAULT_LEASE_NAME",
    "DEFAULT_LEASE_TTL",
    "DEFAULT_TICK_INTERVAL",
    "SchedulerLease",
    "TickResult",
    "acquire_lease",
    "check_lease",
    "release_lease",
    "renew_lease",
    "run_scheduler_loop",
    "scheduler_tick",
    "wake_scheduler",
]
