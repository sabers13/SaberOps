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
    background_min_pace: float = 4.0,
    clock_fn: Callable[[], datetime] | None = None,
    readiness_service: Any = None,
) -> TickResult:
    """Execute one complete scheduler tick.

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
    """
    moment = _utcnow(now)

    def _current_moment() -> datetime:
        if clock_fn is not None:
            return clock_fn()
        return _utcnow(None)

    if not check_lease(db, lease, now=_current_moment()):
        return TickResult(ran=False, reason="lost_lease")

    # 1. Startup and periodic reconciliation
    status_fn = run_status or (lambda r_id: _lookup_run_status(db, r_id))
    changes = reconcile_jobs(db, run_status=status_fn, now=moment)
    for ch in changes:
        if ch.to_state == JobState.SUCCEEDED:
            settle(db, ch.run_id, total_tokens=None, now=moment)
        elif ch.to_state in (JobState.FAILED, JobState.CANCELLED):
            release(db, ch.run_id, now=moment)

    # 2. Collect scheduling inputs
    queued_jobs = store.list_jobs_raw(db, state=JobState.QUEUED, limit=100_000)
    running_jobs = store.list_jobs_raw(db, state=JobState.RUNNING, limit=100_000)
    effective_routes = routes or load_effective_routing()
    effective_pol = policy or load_control_plane_policy()
    quotas = effective_quota_states(db, now=moment)
    cost_states = _load_all_cost_states(db)
    try:
        charges = post_observation_charges(db, now=moment)
    except (sqlite3.OperationalError, sqlite3.DatabaseError):
        return TickResult(ran=False, reason="ledger_read_error")

    # Derive real running counts per pool from running jobs (F3)
    running_counts: dict[str, int] = {}
    for rj in running_jobs:
        if rj.current_run_id:
            try:
                owner = getattr(db, "owner_db", db)
                with owner._get_connection() as conn:
                    rows = conn.execute(
                        "SELECT DISTINCT pool_id FROM capacity_reservations"
                        " WHERE run_id = ? AND state = 'RESERVED'",
                        (rj.current_run_id,),
                    ).fetchall()
                    for r in rows:
                        p = str(r["pool_id"])
                        running_counts[p] = running_counts.get(p, 0) + 1
            except Exception:
                pass

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
        global_slot_limit=global_slot_limit,
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
    )


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
    background_min_pace: float = 4.0,
    now_fn: Callable[[], datetime] | None = None,
    readiness_service: Any = None,
) -> TickResult | None:
    """Run the scheduler loop or a single tick (--once)."""
    clean_holder = holder_id or f"sched_{uuid.uuid4().hex[:8]}"
    now = now_fn() if now_fn else _utcnow(None)

    lease = acquire_lease(db, clean_holder, ttl_seconds=lease_ttl, now=now)
    if lease is None:
        if once:
            return TickResult(ran=False, reason="lease_held_by_other")
        return None

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
                background_min_pace=background_min_pace,
                clock_fn=now_fn,
                readiness_service=readiness_service,
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
                background_min_pace=background_min_pace,
                clock_fn=now_fn,
                readiness_service=readiness_service,
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
