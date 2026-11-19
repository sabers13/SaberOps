"""Scheduler loop, single-instance lease, and atomic dispatch (ADR-0002 D7).

Implements:
- Owner DB single-instance scheduler lease with transactional claim, renewal,
  expiry takeover, and fencing token.
- Scheduler tick: startup/periodic reconciliation of terminal runs, pure
  schedule() evaluation, probe dispatch, wait status updates, and atomic
  job dispatch with idempotent ledger reservations and rollback compensation.
- Scheduler loop runner with bounded polling, wake-event support, and --once mode.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
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
from saberops.control_plane.plans import effective_pool_plans
from saberops.control_plane.policy import (
    ControlPlanePolicy,
    load_control_plane_policy,
)
from saberops.jobs import store
from saberops.jobs.models import (
    DISPATCH_WINDOW_LOST,
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
DEFAULT_TICK_INTERVAL = 2.0

#: Tick holds that clear from durable state or a live run alone.  The
#: continuous loop logs and keeps polling on these instead of exiting: a
#: dispatch-window residue becomes repairable as soon as its lease expiry and
#: grace bound pass, and a live split-store run can finish on a later tick.
_RESUMABLE_HOLD_REASONS = frozenset({"running_run_status_unresolved"})


def _utcnow(now: datetime | None) -> datetime:
    if now is not None:
        return now.astimezone(UTC) if now.tzinfo else now.replace(tzinfo=UTC)
    return datetime.now(UTC)


_SCHEDULER_WAKE_EVENT = threading.Event()


def wake_scheduler() -> None:
    """Signal the scheduler loop to evaluate a tick immediately."""
    _SCHEDULER_WAKE_EVENT.set()


_STARTED_SCHEDULERS: dict[str, subprocess.Popen[Any]] = {}
_START_LOCK = threading.Lock()


def _scheduler_path(db: Any) -> Path:
    owner = getattr(db, "owner_db", db)
    return Path(owner.db_path).resolve()


def _holder_dead(holder: str) -> bool:
    """Prove death only for scheduler holders with a recorded process identity."""
    if not holder.startswith("process:"):
        return False
    try:
        from saberops.ownership import Liveness, ProcessIdentity, probe_liveness

        identity = ProcessIdentity.from_json(holder[len("process:"):])
        return identity is not None and probe_liveness(identity) is Liveness.DEAD
    except (ValueError, TypeError, KeyError):
        return False


def scheduler_alive(db: Any) -> bool:
    """Report an unexpired scheduler lease without mutating the store."""
    path = _scheduler_path(db)
    if not path.exists():
        return False
    try:
        with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=1) as conn:
            row = conn.execute(
                "SELECT expires_at, holder_id FROM capacity_scheduler_lease WHERE lease_name = ?",
                (DEFAULT_LEASE_NAME,),
            ).fetchone()
        expires = parse_iso(row[0]) if row and isinstance(row[0], str) else None
        return (expires is not None and expires > datetime.now(UTC)
                and not _holder_dead(str(row[1])))
    except sqlite3.Error:
        return False


def ensure_scheduler_running(db: Any) -> bool:
    """Start one detached scheduler per owner store, returning whether we started it."""
    if os.environ.get("SABEROPS_DISABLE_AUTO_SCHEDULER") == "1":
        return False
    path = _scheduler_path(db)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _START_LOCK:
        if scheduler_alive(db):
            return False
        lock = open(str(path) + ".scheduler.lock", "a+", encoding="utf-8")
        try:
            deadline = time.monotonic() + 5.0
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if scheduler_alive(db):
                        return False
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"Scheduler startup lock held without a lease: {path}"
                        ) from None
                    time.sleep(0.05)
            env = dict(os.environ)
            source = str(Path(__file__).resolve().parents[2])
            env["PYTHONPATH"] = os.pathsep.join((source, env.get("PYTHONPATH", "")))
            with open(str(path) + ".scheduler.log", "ab", buffering=0) as log:
                proc = subprocess.Popen(
                    [sys.executable, "-m", "saberops.cli", "scheduler", "run", "--db",
                     str(path), "--interval-seconds", "2"],
                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                    start_new_session=True, pass_fds=(lock.fileno(),), env=env,
                )
            _STARTED_SCHEDULERS[str(path)] = proc
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if scheduler_alive(db):
                    return True
                if proc.poll() is not None:
                    raise RuntimeError(
                        f"Scheduler exited with code {proc.returncode}; "
                        f"see {path}.scheduler.log"
                    )
                time.sleep(0.05)
            raise TimeoutError(f"Scheduler did not acquire its lease; see {path}.scheduler.log")
        finally:
            lock.close()


def stop_scheduler(db: Any) -> None:
    """Stop and reap a scheduler started by this process (useful for tests)."""
    proc = _STARTED_SCHEDULERS.pop(str(_scheduler_path(db)), None)
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def notify_scheduler_submission(db: Any) -> None:
    """Ensure a scheduler exists and prompt local loops; remote loops poll."""
    ensure_scheduler_running(db)
    wake_scheduler()



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
    #: RUNNING jobs repaired by dispatch-window recovery in this tick (job ids):
    #: their reserved capacity was released and each was requeued once, or
    #: failed with ``DISPATCH_WINDOW_LOST`` if it already lost a dispatch.
    dispatch_window_repaired: tuple[str, ...] = ()
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
        is_expired = (current_expires is None or moment >= current_expires
                      or _holder_dead(current_holder))

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


#: How long after a dispatch (and after its lease expiry) a RUNNING job with no
#: run row may still be launching.  A residue younger than this is never
#: reconciled: its dispatching scheduler could still be inside the launch
#: window.
DISPATCH_WINDOW_GRACE_SECONDS = 15.0


def _store_path(db: Any) -> str | None:
    """Canonical path of the SQLite store *db* writes runs to, when known."""
    path = getattr(db, "db_path", None)
    if path is None:
        return None
    try:
        return str(Path(path).resolve())
    except OSError:  # pragma: no cover - defensive: unreadable path
        return str(path)


def _dispatch_context(db: Any, lease: SchedulerLease) -> dict[str, Any]:
    """Durable identity of one dispatch generation, stored on its event.

    Recorded beside ``run_id`` inside the same transaction as the QUEUED ->
    RUNNING compare-and-swap, so a later tick can tell *which* scheduler took
    the job and *where* it was about to write the run row.
    """
    return {
        "holder_id": lease.holder_id,
        "fencing_token": lease.fencing_token,
        "lease_expires_at": lease.expires_at,
        "run_store": _store_path(db),
    }


def _event_payload(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("payload_json")
    if not isinstance(raw, str):
        return {}
    try:
        payload = json.loads(raw)
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _run_row_absent(run_id: str, run_store: Any, db: Any) -> bool | None:
    """True only when the run row is *provably* absent from its store.

    ``False`` means the run row exists; ``None`` means the answer cannot be
    proven (unreadable store, unknown path) and the caller must not act.
    """
    current = _store_path(db)
    if not isinstance(run_store, str) or not run_store:
        try:
            return db.get_run(run_id) is None
        except Exception:
            return None
    if run_store == current:
        try:
            return db.get_run(run_id) is None
        except Exception:
            return None
    try:
        with sqlite3.connect(
            f"{Path(run_store).as_uri()}?mode=ro", uri=True, timeout=1
        ) as conn:
            row = conn.execute("SELECT 1 FROM runs WHERE id = ?", (run_id,)).fetchone()
            return row is None
    except (sqlite3.Error, OSError, ValueError):
        return None


def reconcile_dispatch_window(
    db: Any,
    *,
    lease: SchedulerLease,
    running_jobs: Sequence[Job],
    unresolved_job_ids: Sequence[str],
    now: datetime | None = None,
) -> list[ReconcileChange]:
    """Repair jobs whose scheduler died between the dispatch CAS and its run.

    A scheduler that is killed after ``mark_dispatched`` (job RUNNING) but
    before the run row exists leaves a RUNNING job holding a RESERVED capacity
    row with nothing running.  Such a job is provable, never guessed:

    * the latest ``dispatched`` event must carry the dispatching lease identity
      (holder, fencing token, lease expiry) and the store the run was going to
      be written to;
    * the run row must be provably absent from that store;
    * that lease must be superseded (this tick holds a different generation)
      and both the dispatch and the recorded lease expiry must be older than
      :data:`DISPATCH_WINDOW_GRACE_SECONDS`.

    A dispatch without that record (an older engine, or a row inserted by hand)
    is never repaired: its dispatching scheduler is unknowable.  A job that
    already lost one dispatch this way is failed instead of requeued a second
    time, so a persistent fault cannot loop.  The caller releases the reserved
    capacity for every returned change.
    """
    if not unresolved_job_ids:
        return []
    moment = _utcnow(now)
    by_id = {job.job_id: job for job in running_jobs}
    changes: list[ReconcileChange] = []
    logger = logging.getLogger(__name__)
    for job_id in unresolved_job_ids:
        job = by_id.get(job_id)
        if job is None or not job.current_run_id:
            continue
        run_id = job.current_run_id
        try:
            events = store.list_events(db, job_id)
        except (sqlite3.OperationalError, sqlite3.DatabaseError):
            continue
        dispatched = next(
            (event for event in reversed(events) if event.get("kind") == "dispatched"),
            None,
        )
        if dispatched is None:
            continue
        payload = _event_payload(dispatched)
        if payload.get("run_id") != run_id:
            continue
        context = payload.get("dispatch")
        if not isinstance(context, dict):
            # A pre-fix dispatch: the dispatching scheduler is unknowable.
            continue
        holder = context.get("holder_id")
        token = context.get("fencing_token")
        dispatched_at = parse_iso(str(dispatched.get("at") or ""))
        recorded_expires = parse_iso(str(context.get("lease_expires_at") or ""))
        if (
            not isinstance(holder, str)
            or not holder
            or isinstance(token, bool)
            or not isinstance(token, int)
            or dispatched_at is None
            or recorded_expires is None
        ):
            continue
        if holder == lease.holder_id and token == lease.fencing_token:
            # This scheduler generation took the job: it is launching it, or
            # the in-tick compensation path already handled the failure.
            continue
        if dispatched_at >= recorded_expires:
            # Inconsistent record (an unleased or already-expired dispatch).
            continue
        bound = max(dispatched_at, recorded_expires) + timedelta(
            seconds=DISPATCH_WINDOW_GRACE_SECONDS
        )
        if moment < bound:
            continue
        if _run_row_absent(run_id, context.get("run_store"), db) is not True:
            continue
        already_requeued = any(
            event.get("kind") == "requeued"
            and _event_payload(event).get("waiting_reason")
            == WaitingReason.DISPATCH_WINDOW_LOST.value
            for event in events
        )
        try:
            if already_requeued:
                updated = store.reconcile_transition(
                    db,
                    job_id,
                    to=JobState.FAILED,
                    failure_reason=DISPATCH_WINDOW_LOST,
                    now=moment,
                )
            else:
                updated = store.requeue_exact(
                    db,
                    job_id,
                    WaitingReason.DISPATCH_WINDOW_LOST,
                    expected_run_id=run_id,
                    expected_preemptions=job.preemptions,
                    now=moment,
                )
        except (JobConflictError, JobTransitionError, JobNotFoundError):
            # A concurrent cancel/transition owns the row; never fight it.
            continue
        logger.warning(
            "dispatch window lost: job %s run %s -> %s (%s)",
            job_id,
            run_id,
            updated.state.value,
            DISPATCH_WINDOW_LOST,
        )
        changes.append(
            ReconcileChange(
                job_id=job_id,
                run_id=run_id,
                to_state=updated.state,
                failure_reason=(
                    DISPATCH_WINDOW_LOST if updated.state is JobState.FAILED else None
                ),
                job=updated,
            )
        )
    return changes


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
    db: Any, job: Job, decision: DispatchDecision, run_id: str, memory_lease: Any = None
) -> Any:
    from saberops.authority import load_authority_settings
    from saberops.config import freeze_owner_binding_registry_payload
    from saberops.control_plane import freeze_control_plane_policy
    from saberops.jobs.service import job_dispatch_spec
    from saberops.models import Run
    from saberops.preemption import prepare_resume
    from saberops.provenance import mint_run_provenance
    from saberops.routing_config import snapshot_references_exact_bindings, snapshot_to_payload
    from saberops.run_creation import resolve_effective_run_config
    from saberops.supervisor import RunSupervisor
    from saberops.tooling import freeze_tool_contract

    spec = job_dispatch_spec(job)
    cand = decision.candidate
    effort_value: str | None = spec.effort.value if spec.effort is not None else None
    if spec.effort is not None and cand.binding_id:
        from saberops.control_plane.binding_store import load_owner_bindings
        from saberops.models import ReasoningEffort

        requested_effort = ReasoningEffort(spec.effort.value.upper())
        owner_bindings = load_owner_bindings()
        binding = owner_bindings.registry.get(cand.binding_id)
        if binding is not None and not binding.supports_effort(requested_effort):
            raise ValueError(
                f"EFFORT_UNSUPPORTED: binding {cand.binding_id!r} does not support "
                f"requested effort {spec.effort.value!r}"
            )
    req = replace(
        spec.request,
        provider_override=cand.provider,
        model_override=cand.model,
        effort=effort_value,
    )
    config = resolve_effective_run_config(req)
    snapshot = load_effective_routing()
    if config.worker_binding_registry is None and snapshot_references_exact_bindings(snapshot):
        config = replace(config, worker_binding_registry=freeze_owner_binding_registry_payload())
    supervisor = RunSupervisor(db=db, memory_lease=memory_lease)
    frozen_policy = freeze_control_plane_policy(
        load_control_plane_policy(),
        required_capabilities=config.required_capabilities,
        reserve_override=config.reserve_override,
        host_access=load_authority_settings().host_access.value,
    )
    frozen_tools = freeze_tool_contract(config.tool_refs, supervisor.tool_registry)
    provenance = mint_run_provenance(config.target_repo, config.task)
    created_at = datetime.now(UTC).isoformat()

    run = Run(
        id=run_id,
        task=config.task,
        target_repo=config.target_repo,
        base_commit=spec.base_sha,
        tier=config.initial_tier,
        training_allowed=config.training_allowed,
        gate_command=config.gate_command,
        status=RunStatus.PENDING,
        created_at=created_at,
        config_json=json.dumps(config.as_dict()),
        routing_snapshot_json=json.dumps(snapshot_to_payload(snapshot, created_at)),
        control_plane_snapshot_json=frozen_policy.as_json(),
        control_plane_snapshot_digest=frozen_policy.digest,
        tool_contract_json=frozen_tools.to_json(),
        tool_contract_digest=frozen_tools.digest,
        project_id=provenance.project_id,
        project_identity_json=provenance.identity.to_json(),
        objective_id=provenance.objective_id,
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
    if not supervisor.ensure_running(run_id, config):
        raise RuntimeError(f"Failed to start execution owner for {run_id}")
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

    from saberops.notifications import deliver_pending

    try:
        deliver_pending(db)
    except (OSError, sqlite3.Error, ValueError):
        logging.getLogger(__name__).exception("desktop notification scan failed")

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
        running_snapshot, _ = store.list_jobs_tolerant(
            db, state=JobState.RUNNING, limit=100_000
        )
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
            # retain the job and its reservation for owner resolution --
            # unless the dispatch itself is proven abandoned below.
            unresolved_running_jobs.append(running_job.job_id)

    if legacy_running_jobs:
        return TickResult(
            ran=False,
            reason="legacy_running_without_reservation",
            legacy_running_jobs=tuple(legacy_running_jobs),
        )

    # 0b. Dispatch-window recovery (ADR-0011 §2.2).  A scheduler killed between
    # the dispatch CAS and its run row leaves a RUNNING job with a RESERVED
    # capacity row and no run.  When that dispatch is proven abandoned from
    # durable records (dispatcher lease identity, superseded generation, run
    # row absent from the recorded store, older than the grace bound) the job
    # is released and requeued once -- or failed with a reason if it already
    # lost a dispatch this way.  An unprovable row still holds the tick as
    # before, but never silently: the continuous loop keeps running on this
    # hold reason instead of exiting.
    dispatch_window_changes: tuple[ReconcileChange, ...] = ()
    if unresolved_running_jobs:
        try:
            dispatch_window_changes = tuple(
                reconcile_dispatch_window(
                    db,
                    lease=lease,
                    running_jobs=running_snapshot,
                    unresolved_job_ids=unresolved_running_jobs,
                    now=moment,
                )
            )
        except (sqlite3.OperationalError, sqlite3.DatabaseError):
            return TickResult(ran=False, reason="ledger_read_error")
        repaired = {change.job_id for change in dispatch_window_changes}
        for change in dispatch_window_changes:
            if change.to_state in (JobState.FAILED, JobState.QUEUED):
                release(db, change.run_id, now=moment)
        unresolved_running_jobs = [
            job_id for job_id in unresolved_running_jobs if job_id not in repaired
        ]

    if unresolved_running_jobs:
        return TickResult(
            ran=False,
            reason="running_run_status_unresolved",
            unresolved_running_jobs=tuple(unresolved_running_jobs),
            dispatch_window_repaired=tuple(
                change.job_id for change in dispatch_window_changes
            ),
        )

    # Reconcile execution owners before projecting terminal runs into jobs.
    # The supervisor fences the owner generation and refuses to orphan while a
    # worker may still be alive. Worktrees and attempts are retained.
    if run_status is None:
        from saberops.supervisor import RunSupervisor

        supervisor = RunSupervisor(db=db)
        for running_job in running_snapshot:
            run_id = running_job.current_run_id
            if run_id and tick_status(run_id) in (RunStatus.PENDING, RunStatus.RUNNING):
                try:
                    supervisor.reconcile(run_id, orphan_recoverable=True)
                except ValueError:
                    # Unresolvable provenance remains an owner-visible hold.
                    continue
                status_cache.pop(run_id, None)

    # 1. Startup and periodic reconciliation
    changes = reconcile_jobs(db, run_status=tick_status, now=moment)
    try:
        deliver_pending(db)
    except (OSError, sqlite3.Error, ValueError):
        logging.getLogger(__name__).exception("desktop notification scan failed")
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
    queued_jobs, _ = store.list_jobs_tolerant(db, state=JobState.QUEUED, limit=100_000)
    running_jobs, _ = store.list_jobs_tolerant(db, state=JobState.RUNNING, limit=100_000)
    # F8: resolve dependency gating from durable records before any routing
    # work.  A dependency is satisfied only by an accepted candidate, never by
    # "its run completed earlier".
    from saberops.jobs.dependencies import dependency_blockers

    blockers = dependency_blockers(db, queued_jobs)
    effective_routes = routes or load_effective_routing()
    effective_pol = policy or load_control_plane_policy()

    # Respect explicit owner and caller caps, but do not impose a default
    # job-count ceiling: the memory ledger controls host admission.
    try:
        limits = [v for v in (effective_pol.max_global_concurrent,
                              max_concurrent_override, global_slot_limit) if v is not None]
        if any(isinstance(v, bool) or not isinstance(v, int) or v < 1 for v in limits):
            raise ValueError("invalid concurrency limit")
        effective_global_limit = min(limits) if limits else None
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
            for role_name in ("build",):
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
        dependency_blockers=blockers,
    )

    dispatched_count = 0
    probed_count = 0
    waited_count = 0

    pool_policies = effective_pool_plans(effective_pol)

    repaired_job_ids = tuple(change.job_id for change in dispatch_window_changes)

    def lost_during_tick() -> TickResult:
        return TickResult(
            ran=False, reason="lost_lease", reconciled_changes=tuple(changes),
            dispatched=dispatched_count, probed=probed_count, waited=waited_count,
            decisions=tuple(decisions),
            dispatch_window_repaired=repaired_job_ids,
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
                auto_only=(probes is None),
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
                    details=dec.details or None,
                    now=moment,
                )
                waited_count += 1
            except (JobConflictError, JobTransitionError, JobNotFoundError, ValueError):
                continue

        elif isinstance(dec, DispatchDecision):
            # Check lease before dispatch
            if not check_lease(db, lease, now=_current_moment()):
                return lost_during_tick()

            from saberops._gate_tools import gate_tools

            memory_ledger = gate_tools.MemoryLedger(
                gate_tools.default_ledger_root(repo=Path(dec.job.repo))
            )
            memory_lease, memory_reason = memory_ledger.try_acquire_nonblocking(
                "worker_cli", dec.job.requirements.size_hint or "M", worktree=dec.job.repo
            )
            if memory_lease is None:
                store.set_waiting(
                    db, dec.job.job_id, reason=memory_reason or WaitingReason.MEMORY_LOW.value,
                    eta=None, eta_confidence=None, now=moment,
                )
                waited_count += 1
                continue

            run_id = f"run_{uuid.uuid4().hex[:12]}"

            # Step A: CAS state transition QUEUED -> RUNNING.  The same
            # transaction records this scheduler's lease generation and the
            # store the run row will be written to, so a kill inside the launch
            # window is recoverable from durable evidence (ADR-0011 §2.2).
            try:
                dispatched_job = store.mark_dispatched(
                    db,
                    dec.job.job_id,
                    run_id=run_id,
                    dispatch_context=_dispatch_context(db, lease),
                    now=moment,
                )
            except (JobConflictError, JobTransitionError, JobNotFoundError):
                memory_lease.release()
                continue

            # Step B: Re-verify lease before reservation
            if not check_lease(db, lease, now=_current_moment()):
                memory_lease.release()
                try:
                    store.requeue(
                        db,
                        dec.job.job_id,
                        reason=WaitingReason.NO_CAPACITY,
                        now=_current_moment(),
                    )
                except Exception:
                    pass
                return lost_during_tick()

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
                memory_lease.release()
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
                memory_lease.release()
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
                return lost_during_tick()

            # Step E: Launch run
            try:
                if run_launcher is not None:
                    run_launcher(dispatched_job, dec, run_id)
                    memory_lease.release()
                else:
                    _default_run_launcher(db, dispatched_job, dec, run_id, memory_lease)
                    memory_lease.handoff()
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
                    # A spawned owner may still hold the inherited lock.  Do
                    # not unlock it while its execution state is uncertain.
                    memory_lease.handoff()
                    # Run row exists or status unknown: do NOT requeue, do NOT release reservation.
                    # Leave job in RUNNING for reconciler / supervisor recovery.
                    continue

                # Proven no side effects occurred: safe to compensate partial dispatch
                memory_lease.release()
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
        dispatch_window_repaired=repaired_job_ids,
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
    if holder_id is None:
        from saberops.ownership import ProcessIdentity

        identity_data = ProcessIdentity.for_self().to_dict()
        identity_data["instance"] = uuid.uuid4().hex
        holder_id = "process:" + json.dumps(identity_data)
    clean_holder = holder_id
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
    hold_logged: str | None = None

    try:
        while not stop.is_set():
            now = now_fn() if now_fn else _utcnow(None)
            active_lease = renew_lease(db, lease, ttl_seconds=lease_ttl, now=now)
            if active_lease is None:
                # Lost lease to another instance
                last_tick = TickResult(ran=False, reason="lost_lease")
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
                if last_tick.reason in _RESUMABLE_HOLD_REASONS:
                    # The hold is reported (reason + job ids) and re-evaluated
                    # on the next tick; exiting here would make the residue a
                    # silent stall that nothing ever repairs.  Log once per
                    # distinct hold so a long-lived hold does not flood.
                    if last_tick.reason != hold_logged:
                        logging.getLogger(__name__).warning(
                            "scheduler hold: %s unresolved=%s repaired=%s",
                            last_tick.reason,
                            last_tick.unresolved_running_jobs,
                            last_tick.dispatch_window_repaired,
                        )
                        hold_logged = last_tick.reason
                else:
                    break
            else:
                hold_logged = None

            # Bound the poll even when a caller requests a longer cadence: jobs
            # can be submitted by another process which cannot signal this event.
            poll_interval = min(tick_interval, DEFAULT_TICK_INTERVAL)
            waited = 0.0
            step = min(poll_interval, 0.5)
            while waited < poll_interval and not stop.is_set():
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
