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
    "ReconcileChange",
    "SubmitResult",
    "cancel_job",
    "capacity",
    "get_job",
    "job_dispatch_spec",
    "list_jobs",
    "mark_dispatched",
    "reconcile_jobs",
    "requeue",
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
    return CapacityView(generated_at=_utcnow_iso(now), ledger_wired=True, pools=pools)


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
    supervisor) with no event. Idempotent: a second call changes
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
