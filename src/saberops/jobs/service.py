"""Job service functions (M2-3 section F): the future MCP surface (D6).

All functions take ``db: Database`` first; injectables are keyword-only.
Each validates input before any write. No provider, model, tier or
billing field exists in the submit input (ADR invariant 3: callers state
requirements).
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from saberops.jobs import store
from saberops.jobs.feasibility import feasible
from saberops.jobs.ids import new_job_id
from saberops.jobs.models import (
    NO_ROUTE_FOR_REQUIREMENTS,
    PREEMPTION_LIMIT,
    RUN_FAILED,
    RUN_MISSING,
    Effort,
    Job,
    JobRequirements,
    JobState,
    Priority,
    ResumeFrom,
    WaitingReason,
)
from saberops.jobs.store import (
    IdempotencyConflictError,
    JobConflictError,
    JobNotFoundError,
    JobTransitionError,
)
from saberops.models import RunStatus
from saberops.routing_config import RoutingSnapshot, load_effective_routing
from saberops.run_creation import RunRequest

__all__ = [
    "CancelResult",
    "CapacityView",
    "IdempotencyConflictError",
    "JobConflictError",
    "JobDispatchSpec",
    "JobNotFoundError",
    "PoolCapacity",
    "PreemptionReconciliation",
    "PreemptionSettlement",
    "ReconcileChange",
    "SubmitResult",
    "cancel_job",
    "capacity",
    "get_job",
    "job_dispatch_spec",
    "list_jobs",
    "mark_dispatched",
    "reconcile_jobs",
    "reconcile_preemptions",
    "requeue",
    "requeue_preemption",
    "settle_preemption_debts",
    "set_waiting",
    "submit_job",
]


@dataclass(frozen=True)
class SubmitResult:
    job: Job
    created: bool


@dataclass(frozen=True)
class CancelResult:
    job: Job
    cancelled: bool


@dataclass(frozen=True)
class ReconcileChange:
    job_id: str
    run_id: str
    to_state: JobState
    failure_reason: str | None
    job: Job


@dataclass(frozen=True)
class JobDispatchSpec:
    """Frozen job -> run mapping (section H).

    The ``request`` carries no frozen policy: freezing happens when
    M2-4b calls ``resolve_effective_run_config`` at dispatch, so a job
    that waits for days gets the policy of the day it runs.
    ``base_ref``/``base_sha``/``effort``/``size_hint`` travel beside the
    request because ``RunRequest`` has no field for them today.
    """

    job_id: str
    request: RunRequest
    base_ref: str
    base_sha: str
    effort: Effort | None
    size_hint: str | None
    resume_from: ResumeFrom | None


@dataclass(frozen=True)
class PoolCapacity:
    """C5 read model: same eight fields, names, types and order as LedgerEstimate."""

    pool_id: str
    window_id: str
    remaining_est: float | None
    uncertainty: float
    observed_at: str | None
    source: str | None
    stale: bool
    excluded_until: str | None


@dataclass(frozen=True)
class CapacityView:
    generated_at: str
    ledger_wired: bool
    pools: tuple[PoolCapacity, ...]


def _utcnow_iso(now: datetime | None) -> str:
    if now is None:
        return datetime.now(UTC).isoformat()
    aware = now if now.tzinfo else now.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat()


def _parse_aware_instant(raw: str | None, *, field: str) -> str | None:
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        raise ValueError(f"{field} must be a timezone-aware ISO-8601 instant")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field} must be timezone-aware ISO-8601; got {raw!r}") from exc
    if moment.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware ISO-8601; got {raw!r}")
    return moment.astimezone(UTC).isoformat()


def _resolve_repo(repo: Path | str) -> str:
    path = Path(repo).expanduser()
    if not path.exists():
        raise ValueError(f"repo does not exist: {repo}")
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"repo is not a git work tree: {repo} ({exc})") from exc
    if proc.returncode != 0 or proc.stdout.strip() != "true":
        raise ValueError(f"repo is not a git work tree: {repo}")
    return str(path.resolve())


def _resolve_base_sha(repo_abs: str, base_ref: str) -> str:
    ref = (base_ref or "").strip()
    if not ref:
        raise ValueError("base_ref must be a non-empty string")
    try:
        proc = subprocess.run(
            ["git", "-C", repo_abs, "rev-parse", "--verify", ref + "^{commit}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"base_ref does not resolve in {repo_abs}: {ref}") from exc
    if proc.returncode != 0:
        raise ValueError(f"base_ref does not resolve in {repo_abs}: {ref}")
    sha = proc.stdout.strip()
    if not sha:
        raise ValueError(f"base_ref does not resolve in {repo_abs}: {ref}")
    return sha


def _coerce_priority(value: Priority | str) -> Priority:
    if isinstance(value, Priority):
        return value
    try:
        return Priority(str(value).strip().upper())
    except ValueError as exc:
        known = ", ".join(p.value for p in Priority)
        raise ValueError(f"priority must be one of {known}; got {value!r}") from exc


def _coerce_state(value: JobState | str | None) -> JobState | None:
    if value is None:
        return None
    if isinstance(value, JobState):
        return value
    try:
        return JobState(str(value).strip().upper())
    except ValueError as exc:
        known = ", ".join(s.value for s in JobState)
        raise ValueError(f"state must be one of {known}; got {value!r}") from exc


def _request_digest(
    *,
    caller_id: str,
    repo: str,
    goal: str,
    acceptance: str | None,
    base_ref: str,
    requirements: JobRequirements,
    priority: Priority,
    deadline: str | None,
    not_before: str | None,
) -> str:
    canonical = {
        "acceptance": acceptance,
        "base_ref": base_ref,
        "caller_id": caller_id,
        "deadline": deadline,
        "goal": goal,
        "not_before": not_before,
        "priority": priority.value,
        "repo": repo,
        "requirements": requirements.to_dict(),
    }
    raw = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def submit_job(
    db: Any,
    *,
    repo: Path | str,
    goal: str,
    acceptance: str | None = None,
    base_ref: str = "HEAD",
    requirements: JobRequirements | None = None,
    priority: Priority | str = Priority.NORMAL,
    deadline: str | None = None,
    not_before: str | None = None,
    idempotency_key: str | None = None,
    caller_id: str = "owner",
    routing: RoutingSnapshot | None = None,
    now: datetime | None = None,
) -> SubmitResult:
    """Submit one job; infeasible requirements store FAILED, never park forever."""
    if not caller_id or not caller_id.strip():
        raise ValueError("caller_id must be a non-empty string")
    caller = caller_id.strip()
    key = idempotency_key.strip() if isinstance(idempotency_key, str) else idempotency_key
    if key is not None and not key:
        raise ValueError("idempotency_key must be a non-empty string when given")
    if not isinstance(goal, str) or not goal.strip():
        raise ValueError("goal must be a non-empty string")
    clean_goal = goal.strip()
    clean_acceptance = acceptance.strip() if isinstance(acceptance, str) else acceptance
    if clean_acceptance is not None and not clean_acceptance:
        clean_acceptance = None
    req = requirements if requirements is not None else JobRequirements()
    if not isinstance(req, JobRequirements):
        raise ValueError("requirements must be a JobRequirements")
    prio = _coerce_priority(priority)
    repo_abs = _resolve_repo(repo)
    ref_given = (base_ref or "").strip() or "HEAD"
    base_sha = _resolve_base_sha(repo_abs, ref_given)
    deadline_iso = _parse_aware_instant(deadline, field="deadline")
    not_before_iso = _parse_aware_instant(not_before, field="not_before")
    snapshot = routing if routing is not None else load_effective_routing()
    digest = _request_digest(
        caller_id=caller,
        repo=repo_abs,
        goal=clean_goal,
        acceptance=clean_acceptance,
        base_ref=ref_given,
        requirements=req,
        priority=prio,
        deadline=deadline_iso,
        not_before=not_before_iso,
    )
    if key is not None:
        existing = store.find_by_idempotency(db, caller, key)
        if existing is not None:
            if existing.request_digest == digest:
                return SubmitResult(job=existing, created=False)
            raise IdempotencyConflictError(
                f"key {key!r} for caller {caller!r} was used with a different request"
            )
    ok, _reason = feasible(req, snapshot)
    stamp = _utcnow_iso(now)
    if ok:
        job = Job(
            job_id=new_job_id(now=now),
            idempotency_key=key,
            caller_id=caller,
            repo=repo_abs,
            base_ref=ref_given,
            base_sha=base_sha,
            goal=clean_goal,
            acceptance=clean_acceptance,
            requirements=req,
            priority=prio,
            deadline=deadline_iso,
            not_before=not_before_iso,
            state=JobState.QUEUED,
            waiting_reason=WaitingReason.NEW,
            created_at=stamp,
            updated_at=stamp,
            request_digest=digest,
        )
    else:
        job = Job(
            job_id=new_job_id(now=now),
            idempotency_key=key,
            caller_id=caller,
            repo=repo_abs,
            base_ref=ref_given,
            base_sha=base_sha,
            goal=clean_goal,
            acceptance=clean_acceptance,
            requirements=req,
            priority=prio,
            deadline=deadline_iso,
            not_before=not_before_iso,
            state=JobState.FAILED,
            waiting_reason=None,
            failure_reason=NO_ROUTE_FOR_REQUIREMENTS,
            created_at=stamp,
            updated_at=stamp,
            request_digest=digest,
        )
    try:
        store.insert_job(db, job, now=now)
    except JobConflictError:
        if key is not None:
            existing = store.find_by_idempotency(db, caller, key)
            if existing is not None and existing.request_digest == digest:
                return SubmitResult(job=existing, created=False)
            if existing is not None:
                raise IdempotencyConflictError(
                    f"key {key!r} for caller {caller!r} was used with a different request"
                ) from None
        raise
    try:
        from saberops.capacity.loop import wake_scheduler

        wake_scheduler()
    except Exception:
        pass
    return SubmitResult(job=job, created=True)


def get_job(db: Any, job_id: str) -> Job | None:
    return store.get_job(db, job_id)


def list_jobs(
    db: Any, *, state: JobState | str | None = None, limit: int = 50
) -> list[Job]:
    want = _coerce_state(state)
    count = int(limit)
    if count < 1:
        raise ValueError(f"limit must be >= 1; got {limit!r}")
    return store.list_jobs_raw(db, state=want, limit=count)


def cancel_job(
    db: Any, job_id: str, *, run_canceller: Callable[[str], bool]
) -> CancelResult:
    """QUEUED -> CANCELLED; RUNNING asks ``run_canceller`` first."""
    job = store.get_job(db, job_id)
    if job is None:
        raise JobNotFoundError(f"unknown job: {job_id}")
    if job.state == JobState.QUEUED:
        try:
            return CancelResult(job=store.cancel_transition(db, job_id), cancelled=True)
        except (JobConflictError, JobTransitionError):
            current = store.get_job(db, job_id)
            if current is None:
                raise JobNotFoundError(f"unknown job: {job_id}") from None
            return CancelResult(job=current, cancelled=False)
    if job.state == JobState.RUNNING:
        assert job.current_run_id is not None
        if run_canceller(job.current_run_id):
            try:
                cancelled_job = store.cancel_transition(db, job_id)
                try:
                    from saberops.capacity.ledger import release

                    release(db, job.current_run_id)
                except Exception:
                    pass
                try:
                    from saberops.capacity.loop import wake_scheduler

                    wake_scheduler()
                except Exception:
                    pass
                return CancelResult(job=cancelled_job, cancelled=True)
            except (JobConflictError, JobTransitionError):
                current = store.get_job(db, job_id)
                if current is None:
                    raise JobNotFoundError(f"unknown job: {job_id}") from None
                return CancelResult(job=current, cancelled=False)
        current = store.get_job(db, job_id)
        assert current is not None
        return CancelResult(job=current, cancelled=False)
    return CancelResult(job=job, cancelled=False)


def capacity(db: Any, now: datetime | None = None) -> CapacityView:
    """Return the ledger snapshot through the C5 capacity read model."""
    from saberops.capacity.ledger import snapshot

    moment = now if now is not None else datetime.now(UTC)
    pools = tuple(
        PoolCapacity(
            pool_id=entry.pool_id,
            window_id=entry.window_id,
            remaining_est=entry.remaining_est,
            uncertainty=entry.uncertainty,
            observed_at=entry.observed_at,
            source=entry.source,
            stale=entry.stale,
            excluded_until=entry.excluded_until,
        )
        for entry in sorted(
            snapshot(db, moment), key=lambda entry: (entry.pool_id, entry.window_id)
        )
    )
    return CapacityView(generated_at=moment.isoformat(), ledger_wired=True, pools=pools)


def mark_dispatched(
    db: Any, job_id: str, run_id: str, *, now: datetime | None = None
) -> Job:
    """QUEUED -> RUNNING (dispatch side; the scheduler owns the loop)."""
    return store.mark_dispatched(db, job_id, run_id, now=now)


def set_waiting(
    db: Any,
    job_id: str,
    reason: WaitingReason | str,
    *,
    eta: str | None = None,
    eta_confidence: str | None = None,
    now: datetime | None = None,
) -> Job:
    """QUEUED -> QUEUED reason change (dispatch side)."""
    return store.set_waiting(
        db, job_id, reason, eta=eta, eta_confidence=eta_confidence, now=now
    )


def requeue(
    db: Any,
    job_id: str,
    reason: WaitingReason | str,
    *,
    resume_from: ResumeFrom | None = None,
    not_before: str | None = None,
    now: datetime | None = None,
) -> Job:
    """RUNNING -> QUEUED (dispatch side; preemption is M2-5)."""
    clean_not_before = (
        _parse_aware_instant(not_before, field="not_before")
        if not_before is not None
        else None
    )
    return store.requeue(
        db, job_id, reason, resume_from=resume_from, not_before=clean_not_before, now=now
    )


def requeue_preemption(
    db: Any,
    job_id: str,
    *,
    expected_run_id: str,
    expected_preemptions: int,
    resume_from: ResumeFrom | None = None,
    not_before: str | None = None,
    now: datetime | None = None,
) -> Job:
    """RUNNING -> QUEUED fenced to the exact preempted generation (D8).

    Unlike :func:`requeue` this refuses an A-B-A on the job row: the CAS
    requires ``current_run_id == expected_run_id`` and
    ``preemptions == expected_preemptions``, so a crash-recovery tail that
    resumes after the job was requeued *and re-dispatched* cannot count a
    second preemption or clear the newer run's ownership.
    """
    clean_not_before = (
        _parse_aware_instant(not_before, field="not_before")
        if not_before is not None
        else None
    )
    return store.requeue_exact(
        db,
        job_id,
        WaitingReason.PREEMPTED,
        expected_run_id=expected_run_id,
        expected_preemptions=expected_preemptions,
        resume_from=resume_from,
        not_before=clean_not_before,
        now=now,
    )


@dataclass(frozen=True)
class PreemptionSettlement:
    """Outcome of one durable old-run capacity settlement scan (R1)."""

    scanned: int = 0
    released: int = 0
    skipped: int = 0


@dataclass(frozen=True)
class PreemptionReconciliation:
    """Outcome of one D8 crash-recovery reconciliation pass (R1 + R2)."""

    settlement: PreemptionSettlement = PreemptionSettlement()
    pending: tuple[str, ...] = ()
    blocked: tuple[str, ...] = ()


def _resolve_run_and_db(db: Any, run_id: str) -> tuple[Any, Any] | None:
    """Return ``(run, db_owning_the_run)`` or ``None`` when unresolvable.

    Runtime state is project-local while jobs/capacity live in the owner DB,
    so the run may not be in ``db`` at all.  We resolve through the run ID's
    own project segment (never a current selection); anything unresolvable
    fails closed (the caller leaves the charge in place).
    """
    try:
        run = db.get_run(run_id)
    except Exception:
        return None
    if run is not None:
        return run, db
    try:
        from saberops.db import Database
        from saberops.runtime import db_path_for_run

        path = db_path_for_run(run_id)
        if path is None:
            return None
        other = Database(path)
        other_run = other.get_run(run_id)
        if other_run is None:
            return None
        return other_run, other
    except Exception:
        return None


def _run_has_authentic_d8_receipt(
    run_db: Any, run_id: str, job: Job
) -> bool:
    """True iff the run's terminal D8 outcome can be authenticated.

    ``run_db`` **must** be the database that owns the run's execution evidence
    (events, attempts, execution-owner row, handoffs), never the owner DB: in
    the split layout the decision/intent/owner/handoff rows live with the run,
    so authenticating them against the owner DB would make every legitimate
    preemption look like ``decision_missing`` and leak the old run's charge
    forever.

    A receipt is acceptable only when it is **authentic**:

    * an authenticated bound ``preemption_preparing`` decision (real decision
      reference, exact job/repo/base identity, exact attempt snapshot, exact
      durable owner generation) -- with or without a matching verified intent;
      or
    * the typed ``PREEMPTION_LIMIT`` terminal failure, whose only production
      writer is the fenced limit tail.

    A plausible-looking legacy intent, or a receipt whose owner/source binding
    cannot be proven, returns False so the caller retains WIP, job and charged
    capacity and surfaces the owner question.  Any unreadable channel fails
    closed.
    """
    from saberops import preemption

    consumed = not (job.state == JobState.RUNNING and job.current_run_id == run_id)
    expected_before = int(job.preemptions) - (1 if consumed else 0)
    if expected_before < 0:
        return False
    try:
        txn = preemption.authenticate_prepared_transaction(
            run_db,
            run_id=run_id,
            job=job,
            repo_path=job.repo,
            expected_preemptions_before=expected_before,
        )
    except Exception:
        return False
    if txn.ok:
        return True
    if txn.detail != "decision_missing":
        return False
    # Only the fenced limit tail may stand in for a preemption decision here.
    try:
        failed = run_db.get_events_by_type(run_id, "run_failed")
    except Exception:
        return False
    return any(
        isinstance(getattr(event, "payload", None), dict)
        and event.payload.get("reason") == PREEMPTION_LIMIT
        for event in failed
    )


def _terminal_run_status(
    resolved_run: Any,
    run_id: str,
    run_status: Callable[[str], RunStatus | None] | None,
) -> RunStatus | None:
    """Authoritative terminal status for a reservation's run.

    The run row resolved from the run's **own** database (``resolved_run``) is
    authoritative.  An injected ``run_status`` callback -- in production the
    scheduler's lookup over the owner DB -- may legitimately have no evidence
    at all for a project-local run, in which case its ``None`` is not a verdict
    and the resolved row decides.  A callback that positively *contradicts* the
    resolved row is ambiguous evidence and fails closed.
    """
    resolved = getattr(resolved_run, "status", None)
    if not isinstance(resolved, RunStatus):
        return None
    if run_status is None:
        return resolved
    try:
        injected = run_status(run_id)
    except Exception:
        return None
    if injected is None:
        return resolved
    if injected != resolved:
        return None
    return resolved


def settle_preemption_debts(
    db: Any,
    *,
    run_status: Callable[[str], RunStatus | None] | None = None,
    lease_guard: Callable[[Any], bool] | None = None,
    now: datetime | None = None,
    page_size: int = 200,
) -> PreemptionSettlement:
    """Settle terminal D8 runs' reserved capacity, independent of job state.

    R1: :func:`reconcile_jobs` scans only RUNNING jobs, so a job that has
    already been requeued (QUEUED / terminal / re-dispatched onto a newer run)
    can leave its *old* run's RESERVED rows charged forever.  This scan starts
    from durable owner-storage debt (:func:`ledger.list_reserved_debts`, paged
    so old debt is never starved) and releases only runs that are provably
    terminal **and** whose D8 outcome can be authenticated (bound decision or
    the typed limit tail).  It is:

    * idempotent (a released row no longer appears; the UPDATE is
      state-guarded);
    * independent of ``waiting_reason`` and of the job's current RUNNING state;
    * fenced, when a ``lease_guard`` is supplied, inside the same transaction
      as the release (a stale scheduler writes nothing);
    * fail-closed: an unresolvable run, a non-terminal run, a COMPLETED run
      (settled elsewhere), a run outside the job's dispatch lineage, a live
      generation, or a run whose receipt cannot be authenticated (e.g. an
      unbound legacy intent) is skipped -- WIP, job and charge are retained.
    """
    moment = now if now is not None else datetime.now(UTC)
    from saberops.capacity.ledger import list_reserved_debts, release_terminal_debt

    scanned = 0
    released = 0
    skipped = 0
    after: str | None = None
    while True:
        page = list_reserved_debts(db, limit=page_size, after_run_id=after)
        if not page:
            break
        for debt in page:
            scanned += 1
            after = debt.run_id
            resolved = _resolve_run_and_db(db, debt.run_id)
            if resolved is None:
                skipped += 1
                continue
            run, run_db = resolved
            status = _terminal_run_status(run, debt.run_id, run_status)
            if status in (None, RunStatus.RUNNING, RunStatus.PENDING, RunStatus.COMPLETED):
                skipped += 1
                continue
            if debt.job_id is None:
                # No owning job means no way to authenticate the D8 receipt:
                # fail closed and retain the charge.
                skipped += 1
                continue
            try:
                owning_job = store.get_job(db, debt.job_id)
            except Exception:
                skipped += 1
                continue
            if owning_job is None:
                skipped += 1
                continue
            if debt.run_id not in owning_job.run_ids:
                # The reservation is not on this job's own dispatch lineage.
                skipped += 1
                continue
            if owning_job.state == JobState.RUNNING and owning_job.current_run_id == (
                debt.run_id
            ):
                # Never free capacity while the owning job is STILL the live
                # generation of this exact run: its reservation is what makes
                # pool/global occupancy truthful (early release could
                # over-admit).  That generation's own requeue tail settles it.
                skipped += 1
                continue
            if not _run_has_authentic_d8_receipt(run_db, debt.run_id, owning_job):
                skipped += 1
                continue
            result = release_terminal_debt(
                db,
                run_id=debt.run_id,
                job_id=debt.job_id,
                now=moment,
                lease_guard=lease_guard,
            )
            if result < 0:
                skipped += 1
                continue
            released += result
        if len(page) < page_size:
            break
    return PreemptionSettlement(scanned=scanned, released=released, skipped=skipped)


def reconcile_preemptions(
    db: Any,
    *,
    run_status: Callable[[str], RunStatus | None] | None = None,
    recover: Callable[[str], Any] | None = None,
    lease_guard: Callable[[Any], bool] | None = None,
    now: datetime | None = None,
    page_size: int = 200,
    job_scan_limit: int = 100_000,
) -> PreemptionReconciliation:
    """One D8 crash-recovery pass: settle settled debt and finish interrupted
    preemption transactions (R1 + R2).

    ``recover`` is an injected production entry point (in the scheduler this is
    :meth:`RunSupervisor.reconcile`, which owns the dead-owner/quiescence
    fencing).  This function only *discovers* interrupted transactions from
    durable state and delegates; it never decides a route, calls a provider,
    or fabricates a preemption.
    """
    from saberops import preemption

    moment = now if now is not None else datetime.now(UTC)
    settlement = settle_preemption_debts(
        db, run_status=run_status, lease_guard=lease_guard, now=moment, page_size=page_size
    )
    pending: list[str] = []
    blocked: list[str] = []
    if recover is None:
        return PreemptionReconciliation(settlement=settlement)
    try:
        running_jobs = store.list_jobs_raw(db, state=JobState.RUNNING, limit=job_scan_limit)
    except Exception:
        running_jobs = []
    for job in running_jobs:
        run_id = job.current_run_id
        if not run_id:
            continue
        resolved = _resolve_run_and_db(db, run_id)
        if resolved is None:
            continue
        run, run_db = resolved
        if run.status != RunStatus.RUNNING:
            continue
        try:
            txn = preemption.classify_preemption_transaction(
                run_db, run_id=run_id, job=job, repo_path=job.repo
            )
        except Exception:
            continue
        if txn.state == "none":
            continue
        if txn.state == "blocked":
            blocked.append(run_id)
            continue
        pending.append(run_id)
        try:
            recover(run_id)
        except Exception:
            pass
    return PreemptionReconciliation(
        settlement=settlement, pending=tuple(pending), blocked=tuple(blocked)
    )


def reconcile_jobs(
    db: Any,
    *,
    run_status: Callable[[str], RunStatus | None],
    now: datetime | None = None,
) -> list[ReconcileChange]:
    """Fix RUNNING jobs from their run's terminal status (section G).

    Called by whoever owns the loop (M2-4b scheduler start and tick).
    ``COMPLETED`` -> SUCCEEDED; ``FAILED`` -> FAILED ``RUN_FAILED``;
    ``None`` (run not found) -> FAILED ``RUN_MISSING``; ``PENDING``,
    ``RUNNING`` and ``ORPHANED`` stay (run recovery belongs to the
    supervisor) with no event.  ``PREEMPTED`` (ADR-0002 D8, M2-5) completes
    an interrupted preemption idempotently: the run is conclusively
    terminal, so the job is requeued to QUEUED/PREEMPTED with its verified
    resume capsule instead of failing.  Idempotent: a second call changes
    nothing. A concurrent ``cancel_job`` wins or loses cleanly per job
    via the same compare-and-set (the loser is skipped).
    """
    changes: list[ReconcileChange] = []
    for job in store.list_jobs_raw(db, state=JobState.RUNNING, limit=100_000):
        run_id = job.current_run_id
        if run_id is None:  # pragma: no cover - invariant: RUNNING holds a run
            continue
        status = run_status(run_id)
        if status is None:
            to_state, failure = JobState.FAILED, RUN_MISSING
        elif status == RunStatus.COMPLETED:
            to_state, failure = JobState.SUCCEEDED, None
        elif status == RunStatus.FAILED:
            to_state, failure = JobState.FAILED, RUN_FAILED
        elif status == RunStatus.PREEMPTED:
            # Crash between run terminalization and job requeue (D8): finish
            # the protocol from durable state.  The coordinator is
            # idempotent; a concurrent cancel still wins via CAS inside.
            from saberops.preemption import complete_preemption

            try:
                outcome = complete_preemption(
                    db, run_id=run_id, repo_path=job.repo,
                    job_id=job.job_id, base_sha=job.base_sha, now=now,
                )
            except Exception:
                continue
            if not outcome.owned or outcome.job is None:
                continue
            changes.append(
                ReconcileChange(
                    job_id=job.job_id,
                    run_id=run_id,
                    to_state=JobState.QUEUED,
                    failure_reason=None,
                    job=outcome.job,
                )
            )
            continue
        else:  # PENDING, RUNNING, ORPHANED: supervisor's recovery, not ours
            continue
        try:
            updated = store.reconcile_transition(
                db, job.job_id, to=to_state, failure_reason=failure, now=now
            )
        except (JobConflictError, JobTransitionError):
            # A concurrent cancel moved the job first; it wins cleanly.
            continue
        changes.append(
            ReconcileChange(
                job_id=job.job_id,
                run_id=run_id,
                to_state=to_state,
                failure_reason=failure,
                job=updated,
            )
        )
    if changes:
        try:
            from saberops.capacity.loop import wake_scheduler

            wake_scheduler()
        except Exception:
            pass
    return changes


def job_dispatch_spec(job: Job) -> JobDispatchSpec:
    """Build the frozen job -> run mapping (section H, pure).

    This function does not call ``resolve_effective_run_config``:
    freezing happens when M2-4b calls it at dispatch.
    """
    from saberops.control_plane.capabilities import Capability

    task = job.goal
    if job.acceptance:
        task = task + "\n\nAcceptance:\n" + job.acceptance
    caps = set(job.requirements.required_capabilities)
    if job.requirements.read_only:
        caps.add(Capability.READ_ONLY.value)
    request = RunRequest(
        task=task,
        repo_path=job.repo,
        routing_mode="auto",
        required_capabilities=tuple(sorted(caps)),
    )
    return JobDispatchSpec(
        job_id=job.job_id,
        request=request,
        base_ref=job.base_ref,
        base_sha=job.base_sha,
        effort=job.requirements.effort,
        size_hint=job.requirements.size_hint,
        resume_from=job.resume_from,
    )
