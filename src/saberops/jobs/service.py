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
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from saberops.jobs import store
from saberops.jobs.dependencies import normalize_depends_on, validate_job_dependencies
from saberops.jobs.feasibility import feasible
from saberops.jobs.ids import new_job_id
from saberops.jobs.models import (
    NO_ROUTE_FOR_REQUIREMENTS,
    PREEMPTION_LIMIT,
    RUN_FAILED,
    RUN_MISSING,
    TERMINAL_JOB_STATES,
    Effort,
    Job,
    JobKind,
    JobRequirements,
    JobState,
    Priority,
    ResumeFrom,
    SteeringNote,
    WaitingReason,
    coerce_job_kind,
    read_only_requirements,
)
from saberops.jobs.store import (
    IdempotencyConflictError,
    JobConflictError,
    JobNotFoundError,
    JobTransitionError,
)
from saberops.models import AttemptStatus, HealthState, ManagerState, RunStatus
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
    "serialize_capacity_view",
    "get_job",
    "job_dispatch_spec",
    "job_usage_view",
    "list_jobs",
    "mark_dispatched",
    "note_job",
    "reconcile_jobs",
    "reconcile_preemptions",
    "promote_scout",
    "requeue",
    "requeue_preemption",
    "settle_preemption_debts",
    "set_waiting",
    "steering_notes",
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
    kind: JobKind = JobKind.BUILD


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
    kind: JobKind,
    deadline: str | None,
    not_before: str | None,
    depends_on: tuple[str, ...] = (),
) -> str:
    canonical = {
        "acceptance": acceptance,
        "base_ref": base_ref,
        "caller_id": caller_id,
        "deadline": deadline,
        "depends_on": list(depends_on),
        "goal": goal,
        # Only a scout changes the request identity: an ordinary BUILD job's
        # digest must stay identical to an idempotency key stored before F19.
        "kind": kind.value if kind is not JobKind.BUILD else None,
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
    depends_on: Sequence[str] | None = None,
    kind: JobKind | str = JobKind.BUILD,
    idempotency_key: str | None = None,
    caller_id: str = "owner",
    routing: RoutingSnapshot | None = None,
    now: datetime | None = None,
    mcp_session_id: str | None = None,
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
    job_kind = coerce_job_kind(kind)
    if job_kind is JobKind.SCOUT:
        # F19: read-only is enforced by the job kind, not requested by the
        # caller, and the requirement must be in force *before* feasibility so
        # a scout can never be routed to a provider without read-only support.
        req = read_only_requirements(req)
    prio = _coerce_priority(priority)
    repo_abs = _resolve_repo(repo)
    ref_given = (base_ref or "").strip() or "HEAD"
    base_sha = _resolve_base_sha(repo_abs, ref_given)
    deadline_iso = _parse_aware_instant(deadline, field="deadline")
    not_before_iso = _parse_aware_instant(not_before, field="not_before")
    depends = normalize_depends_on(depends_on)
    snapshot = routing if routing is not None else load_effective_routing()
    # Dependencies are part of the request identity: the same idempotency key
    # with a different dependency list is a different request, not a replay.
    digest = _request_digest(
        caller_id=caller,
        repo=repo_abs,
        goal=clean_goal,
        acceptance=clean_acceptance,
        base_ref=ref_given,
        requirements=req,
        priority=prio,
        kind=job_kind,
        deadline=deadline_iso,
        not_before=not_before_iso,
        depends_on=depends,
    )
    new_id = new_job_id(now=now)
    validate_job_dependencies(db, job_id=new_id, depends_on=depends)
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
            job_id=new_id,
            idempotency_key=key,
            caller_id=caller,
            repo=repo_abs,
            base_ref=ref_given,
            base_sha=base_sha,
            goal=clean_goal,
            acceptance=clean_acceptance,
            requirements=req,
            priority=prio,
            kind=job_kind,
            deadline=deadline_iso,
            not_before=not_before_iso,
            depends_on=depends,
            state=JobState.QUEUED,
            waiting_reason=WaitingReason.NEW,
            created_at=stamp,
            updated_at=stamp,
            request_digest=digest,
        )
    else:
        job = Job(
            job_id=new_id,
            idempotency_key=key,
            caller_id=caller,
            repo=repo_abs,
            base_ref=ref_given,
            base_sha=base_sha,
            goal=clean_goal,
            acceptance=clean_acceptance,
            requirements=req,
            priority=prio,
            kind=job_kind,
            deadline=deadline_iso,
            not_before=not_before_iso,
            depends_on=depends,
            state=JobState.FAILED,
            waiting_reason=None,
            failure_reason=NO_ROUTE_FOR_REQUIREMENTS,
            created_at=stamp,
            updated_at=stamp,
            request_digest=digest,
        )
    try:
        store.insert_job(db, job, now=now, mcp_session_id=mcp_session_id)
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
        from saberops.capacity.loop import notify_scheduler_submission

        notify_scheduler_submission(db)
    except Exception:
        import logging

        logging.getLogger(__name__).exception("Could not start or notify scheduler")
    return SubmitResult(job=job, created=True)


def get_job(db: Any, job_id: str) -> Job | None:
    return store.get_job(db, job_id)


#: Bound on the scout report text carried into a promoted build job's goal.
MAX_PROMOTED_REPORT_CHARS: int = 20_000


def promote_scout(
    db: Any,
    *,
    scout_job_id: str,
    goal: str | None = None,
    acceptance: str | None = None,
    base_ref: str = "HEAD",
    priority: Priority | str = Priority.NORMAL,
    depends_on: Sequence[str] | None = None,
    idempotency_key: str | None = None,
    caller_id: str = "owner",
    routing: RoutingSnapshot | None = None,
    now: datetime | None = None,
) -> SubmitResult:
    """Start a build job whose starting context is a scout report (F19).

    The report is the scout's durable deliverable, so promotion copies its
    bounded text into the new job's goal and records the lineage on both job
    records.  Promotion is an explicit owner/planner action; a scout is never
    promoted automatically.
    """
    from saberops.scout import latest_report

    scout = store.get_job(db, scout_job_id)
    if scout is None:
        raise JobNotFoundError(f"unknown job: {scout_job_id}")
    if scout.kind is not JobKind.SCOUT:
        raise ValueError(f"job {scout_job_id} is not a scout job")
    if scout.state is not JobState.SUCCEEDED:
        raise ValueError(
            f"scout job {scout_job_id} has no completed report (state {scout.state.value})"
        )
    report = latest_report(db, scout_job_id)
    if not report or not report.get("report_path"):
        raise ValueError(f"scout job {scout_job_id} has no indexed report")
    try:
        body = Path(str(report["report_path"])).read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(
            f"scout report is unreadable at {report['report_path']}: {exc}"
        ) from exc
    cleaned_goal = (goal or scout.goal).strip()
    if not cleaned_goal:
        raise ValueError("a promoted build job needs a non-empty goal")
    carried = body.strip()[:MAX_PROMOTED_REPORT_CHARS]
    promoted_goal = (
        f"{cleaned_goal}\n\n"
        f"[SCOUT REPORT from job {scout_job_id}]\n"
        f"Path: {report['report_path']}\n\n"
        f"{carried}"
    )
    result = submit_job(
        db,
        repo=scout.repo,
        goal=promoted_goal,
        acceptance=acceptance,
        base_ref=base_ref,
        requirements=JobRequirements(),
        priority=priority,
        depends_on=depends_on,
        idempotency_key=idempotency_key,
        caller_id=caller_id,
        routing=routing,
        now=now,
    )
    store.append_event(
        db,
        result.job.job_id,
        "promoted_from_scout",
        {
            "scout_job_id": scout_job_id,
            "report_path": report["report_path"],
            "report_digest": report.get("digest"),
        },
        now=now,
    )
    store.append_event(
        db,
        scout_job_id,
        "scout_promoted",
        {"job_id": result.job.job_id, "report_path": report["report_path"]},
        now=now,
    )
    return result

def note_job(
    db: Any, job_id: str, body: str, *, now: datetime | None = None
) -> SteeringNote:
    """Attach one durable owner note to a job (ADR-0016).

    The note is never injected into a running worker.  It stays pending until
    the job's next worker attempt starts, whose prompt is the next safe point,
    and delivery is recorded once on that attempt.  Notes for terminal jobs
    are refused because no boundary remains to deliver them.
    """
    job = store.get_job(db, job_id)
    if job is None:
        raise JobNotFoundError(f"unknown job: {job_id}")
    text = (body or "").strip()
    if not text:
        raise ValueError("a steering note needs non-empty text")
    if len(text) > store.MAX_STEERING_NOTE_CHARS:
        raise ValueError(
            "a steering note is limited to "
            f"{store.MAX_STEERING_NOTE_CHARS} characters; got {len(text)}"
        )
    if job.state in TERMINAL_JOB_STATES:
        raise ValueError(
            f"job {job_id} is {job.state.value}; no worker attempt remains to deliver a note"
        )
    return store.insert_steering_note(db, job_id, text, now=now)


def steering_notes(db: Any, job_id: str) -> tuple[SteeringNote, ...]:
    """Every note recorded for a job, oldest first (read-only)."""
    return tuple(store.list_steering_notes(db, job_id))


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
        from saberops.native_workers import native_run_marker, request_native_stop

        native = native_run_marker(db, job.current_run_id) is not None
        if native:
            # F16: SaberOps cannot kill a native child.  It posts the stop the
            # orchestrator acts on, cancels the job so the child's report is
            # refused, and never claims the process is gone.
            request_native_stop(
                db,
                job,
                reason="owner_cancelled",
                detail="the owner cancelled the job; stop the native child",
            )
        if native or run_canceller(job.current_run_id):
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


def serialize_capacity_view(
    view: CapacityView,
    *,
    policy: Any | None = None,
    scheduler_alive: bool | None = None,
) -> dict[str, Any]:
    """Serialize a CapacityView with source, age, and explicit ``'unknown'`` entries."""
    from saberops.capacity.exclusions import parse_iso
    from saberops.capacity.freshness import age_seconds, format_age
    from saberops.control_plane.plans import effective_pool_plans
    from saberops.control_plane.policy import load_control_plane_policy

    moment = parse_iso(view.generated_at) or datetime.now(UTC)
    entries: list[dict[str, Any]] = []
    seen_windows: set[tuple[str, str]] = set()
    pools_with_all: set[str] = set()
    for p in view.pools:
        seen_windows.add((p.pool_id, p.window_id))
        if p.window_id == "ALL":
            pools_with_all.add(p.pool_id)
        entries.append(
            {
                "pool_id": p.pool_id,
                "window_id": p.window_id,
                "remaining_est": p.remaining_est,
                "remaining": (
                    f"{p.remaining_est:.1f}%"
                    if p.remaining_est is not None
                    else "unknown"
                ),
                "uncertainty": p.uncertainty,
                "observed_at": p.observed_at,
                "source": p.source or "unknown",
                "age": format_age(p.observed_at, moment),
                "age_seconds": age_seconds(p.observed_at, moment),
                "stale": p.stale,
                "excluded_until": p.excluded_until,
            }
        )
    try:
        eff_policy = policy if policy is not None else load_control_plane_policy()
        pool_plans = effective_pool_plans(eff_policy)
    except Exception:
        pool_plans = {}
    for pool_id, plan in sorted(pool_plans.items()):
        if pool_id in pools_with_all:
            continue
        for win in plan.windows:
            if (pool_id, win.id) in seen_windows:
                continue
            entries.append(
                {
                    "pool_id": pool_id,
                    "window_id": win.id,
                    "remaining_est": None,
                    "remaining": "unknown",
                    "uncertainty": 0.0,
                    "observed_at": None,
                    "source": "unknown",
                    "age": "unknown",
                    "age_seconds": None,
                    "stale": True,
                    "excluded_until": None,
                }
            )
    entries.sort(key=lambda item: (str(item["pool_id"]), str(item["window_id"])))
    payload: dict[str, Any] = {
        "generated_at": view.generated_at,
        "ledger_wired": view.ledger_wired,
        "pools": entries,
    }
    if scheduler_alive is not None:
        payload["scheduler_alive"] = bool(scheduler_alive)
    return payload


def mark_dispatched(
    db: Any,
    job_id: str,
    run_id: str,
    *,
    dispatch_context: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> Job:
    """QUEUED -> RUNNING (dispatch side; the scheduler owns the loop)."""
    return store.mark_dispatched(
        db, job_id, run_id, dispatch_context=dispatch_context, now=now
    )


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
        running_jobs, _ = store.list_jobs_tolerant(
            db, state=JobState.RUNNING, limit=job_scan_limit
        )
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
    ``None`` (run not found) -> FAILED ``RUN_MISSING``; ``ORPHANED`` ->
    FAILED ``RUN_FAILED``; ``PENDING`` and ``RUNNING`` stay (recovery belongs to the
    supervisor) with no event.  ``PREEMPTED`` (ADR-0002 D8, M2-5) completes
    an interrupted preemption idempotently: the run is conclusively
    terminal, so the job is requeued to QUEUED/PREEMPTED with its verified
    resume capsule instead of failing.  Idempotent: a second call changes
    nothing. A concurrent ``cancel_job`` wins or loses cleanly per job
    via the same compare-and-set (the loser is skipped).
    """
    changes: list[ReconcileChange] = []
    running_jobs, _ = store.list_jobs_tolerant(db, state=JobState.RUNNING, limit=100_000)
    for job in running_jobs:
        run_id = job.current_run_id
        if run_id is None:  # pragma: no cover - invariant: RUNNING holds a run
            continue
        status = run_status(run_id)
        if status is None:
            to_state, failure = JobState.FAILED, RUN_MISSING
        elif status == RunStatus.COMPLETED:
            to_state, failure = JobState.SUCCEEDED, None
        elif status in (RunStatus.FAILED, RunStatus.ORPHANED):
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
        else:  # PENDING, RUNNING: supervisor's recovery, not ours
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


#: Explicit absence marker: a provider that reported no counters stays
#: unknown rather than becoming a false zero.
USAGE_UNKNOWN = "unknown"


def subscription_for_provider(provider: str) -> str:
    """Owner-facing subscription slug for a provider.

    Unknown providers stay named as themselves: usage is never attributed to
    a subscription SaberOps cannot prove.
    """
    from saberops.accounts_view import ACCOUNTS

    for account in ACCOUNTS:
        if account.provider == provider:
            return account.slug
    return provider


def _usage_totals(rows: list[Any]) -> dict[str, Any]:
    """Aggregate usage events; a counter with no observation stays unknown."""

    def total(field: str) -> int | str:
        values = [
            value
            for row in rows
            if (value := getattr(row, field, None)) is not None
        ]
        return sum(values) if values else USAGE_UNKNOWN

    known = [row for row in rows if row.input_tokens is not None or row.output_tokens is not None]
    return {
        "reported": bool(known),
        "usage_sources": sorted({str(row.usage_source) for row in rows}),
        "provider_usage_event_count": len(rows),
        "calls_with_known_usage": len(known),
        "calls_with_unknown_usage": len(rows) - len(known),
        "input_tokens": total("input_tokens"),
        "output_tokens": total("output_tokens"),
        "cached_tokens": total("cached_tokens"),
        "cache_read_tokens": total("cache_read_tokens"),
        "cache_write_tokens": total("cache_write_tokens"),
        "reasoning_tokens": total("reasoning_tokens"),
        "total_tokens": total("total_tokens"),
    }


def job_usage_view(db: Any, job: Job) -> dict[str, Any]:
    """Per-attempt and per-subscription usage from durable records.

    Read-only and presentation-free: the same projection serves ``get_job``,
    ``catch_up`` and the owner's return summary, so quota truth is stated once.
    Usage belongs to the attempt that produced it; review dispatches are a
    separate record and never appear as an attempt.  Token counters the
    provider did not report stay ``"unknown"``.
    """
    usage_by_run: dict[str, list[Any]] = {}
    attempts: list[dict[str, Any]] = []
    attempt_rows: dict[str, list[Any]] = {}
    subscription_rows: dict[str, list[Any]] = {}
    subscription_attempts: dict[str, list[str]] = {}
    run_ids = list(job.run_ids)
    if job.current_run_id and job.current_run_id not in run_ids:
        run_ids.append(job.current_run_id)
    seen_runs: set[str] = set()
    for run_id in run_ids:
        if not run_id or run_id in seen_runs:
            continue
        seen_runs.add(run_id)
        rows = usage_by_run.get(run_id)
        if rows is None:
            rows = db.get_model_call_usage(run_id)
            usage_by_run[run_id] = rows
        rows_by_attempt: dict[str, list[Any]] = {}
        for row in rows:
            if row.attempt_id:
                rows_by_attempt.setdefault(str(row.attempt_id), []).append(row)
        for attempt in db.get_attempts_for_run(run_id):
            subscription = subscription_for_provider(attempt.provider)
            attempt_usage = rows_by_attempt.get(attempt.id, [])
            attempt_rows[attempt.id] = attempt_usage
            subscription_rows.setdefault(subscription, []).extend(attempt_usage)
            subscription_attempts.setdefault(subscription, []).append(attempt.id)
            attempts.append(
                {
                    "attempt_id": attempt.id,
                    "run_id": attempt.run_id,
                    "attempt_number": attempt.attempt_number,
                    "provider": attempt.provider,
                    "subscription": subscription,
                    "model": attempt.model,
                    "status": attempt.status.value,
                    "usage": _usage_totals(attempt_usage),
                }
            )
    all_rows = [row for rows in attempt_rows.values() for row in rows]
    return {
        "attempts": attempts,
        "subscriptions": [
            {
                "subscription": subscription,
                "attempt_ids": subscription_attempts[subscription],
                "usage": _usage_totals(subscription_rows.get(subscription, [])),
            }
            for subscription in sorted(subscription_attempts)
        ],
        "total": _usage_totals(all_rows),
        "coverage_complete": bool(attempts)
        and all(entry["usage"]["reported"] for entry in attempts),
    }


def job_status_view(db: Any, job: Job) -> dict[str, Any]:
    """Project durable execution evidence onto a job without changing its state."""
    view = job.to_dict()
    view.update(activity=None, reason=None, work_location=None, last_failed_attempt=None)
    from saberops.observability.run_report import build_roles_view

    try:
        from saberops.scout import latest_report

        report = latest_report(db, job.job_id)
    except Exception:  # noqa: BLE001 - a report read never breaks a status view
        report = None
    if report is not None:
        view["report"] = report
    view["steering_notes"] = [note.to_dict() for note in steering_notes(db, job.job_id)]
    # F26: advisor route, calls and advice share one read-only projection with
    # get_job, tail_job and catch_up, so every surface states the same record.
    try:
        from saberops.advisor import advisor_view_for_job

        view["advisor"] = advisor_view_for_job(db, job)
    except Exception as exc:  # noqa: BLE001 - a projection never breaks status
        view["advisor"] = {"enabled": False, "error": exc.__class__.__name__}
    roles = (
        build_roles_view(db, job.current_run_id, plan_identity=job.caller_id)
        if job.current_run_id
        else None
    )
    view["roles"] = roles or {
        "plan": {
            "role": "plan",
            "identity": job.caller_id or "UNKNOWN",
            "source": "job caller",
        },
        "build": {
            "role": "build",
            "provider": "UNKNOWN",
            "model": "UNKNOWN",
            "identity_status": "UNKNOWN",
        },
        "check": {
            "role": "check",
            "provider": "UNKNOWN",
            "model": "UNKNOWN",
            "verdict": "UNKNOWN",
        },
    }
    if not job.current_run_id:
        return view
    run = db.get_run(job.current_run_id)
    attempts = db.get_attempts_for_run(job.current_run_id)
    failed = [a for a in attempts if a.status in (AttemptStatus.FAILED, AttemptStatus.TIMEOUT)]
    last = failed[-1] if failed else (attempts[-1] if attempts else None)
    if last is not None:
        view["work_location"] = last.worktree_path
    if failed:
        attempt = failed[-1]
        view["last_failed_attempt"] = {
            "attempt_id": attempt.id,
            "reason": (
                attempt.worker_error or attempt.interruption_reason or "worker attempt failed"
            ),
            "work_location": attempt.worktree_path,
        }
    from saberops.native_workers import native_status

    native = native_status(db, job)
    if native is not None:
        # F16: a native child has no execution owner to ask; its liveness is
        # the heartbeat deadline plus the durable stop request.
        view["native"] = native
        if job.state == JobState.RUNNING:
            view["activity"] = "needs_attention" if native["needs_attention"] else "running"
            if native.get("stop_reason"):
                view["reason"] = native["stop_reason"]
        else:
            run_row = db.get_run(job.current_run_id)
            view["reason"] = view["reason"] or (
                run_row.result_summary if run_row is not None else None
            )
        return view
    if run is not None:
        view["reason"] = run.result_summary
    if job.state != JobState.RUNNING:
        if not view["reason"]:
            view["reason"] = job.failure_reason or (
                view["last_failed_attempt"]["reason"] if failed else None
            )
        return view
    from saberops.ownership import Liveness
    from saberops.supervisor import RunSupervisor

    _, liveness = RunSupervisor(db=db).owner_evidence(job.current_run_id)
    supervision = db.get_supervision(job.current_run_id)
    if liveness is Liveness.DEAD:
        view["activity"] = "needs_attention"
        view["reason"] = "execution owner is dead; worker safety is being checked"
    elif supervision is not None and (
        supervision.health_state == HealthState.STALLED
        # A healthy worker overrides a stale manager attention flag: recovery
        # paths refresh health without clearing the older attention mark.
        or (
            supervision.health_state != HealthState.HEALTHY
            and supervision.manager_state == ManagerState.ATTENTION_REQUIRED
        )
    ):
        view["activity"] = "needs_attention"
        view["reason"] = supervision.health_reason or "worker needs attention"
    elif failed and attempts[-1].id != failed[-1].id:
        view["activity"] = "retrying"
        view["reason"] = view["last_failed_attempt"]["reason"]
    else:
        view["activity"] = "running"
    return view


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
        kind=job.kind.value.lower(),
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
        kind=job.kind,
    )
