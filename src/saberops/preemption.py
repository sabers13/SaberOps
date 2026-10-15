"""Durable quota preemption and resume (ADR-0002 D8, M2-5 Step 001).

A durable JOB-owned run that suffers
:attr:`WorkerInterruptionReason.QUOTA_EXHAUSTED
<saberops.models.WorkerInterruptionReason.QUOTA_EXHAUSTED>` and then has no
admitted candidate left in ANY role fallback group -- as determined by the
accepted M2-4b admission policy and recorded as a ``no_eligible_candidate``
run event -- is preempted instead of failed:

1. the current WIP is captured as a checkpoint commit (reusing the existing
   worker-checkpoint row when one already covers the worktree HEAD; anchoring
   the exact existing HEAD when there is nothing dirty; never fabricating
   progress);
2. the checkpoint SHA is anchored under the exact engine-owned Git ref
   ``refs/saberops/checkpoints/<job_id>/<n>`` and verified;
3. a factual bounded harness-neutral resume capsule is persisted through the
   actual handoff contract (:func:`saberops.handoff.create_handoffs`);
4. the RUN ends as :attr:`RunStatus.PREEMPTED <saberops.models.RunStatus.PREEMPTED>`
   -- terminal for that RUN, never an accepted candidate;
5. the JOB returns to QUEUED with ``waiting_reason == PREEMPTED``, a verified
   ``resume_from`` (:class:`ResumeFrom <saberops.jobs.models.ResumeFrom>`),
   an evidence-backed ``not_before`` only when an EXACT/PARSED reset exists,
   and exactly one ``preemptions`` increment;
6. the old run's capacity reservation is released exactly once.

A later scheduled re-dispatch constructs a NEW run anchored at the original
job base; :func:`prepare_resume` restores the verified checkpoint plus the
factual capsule through the new run's ordinary worker-checkpoint channel, so
no orchestration, admission, posture, quota, lease, or review logic is
bypassed: the resumed run passes the normal M2-4b guard and still requires
independent review plus owner acceptance before anything publishes.

Bound: ``max_preemptions`` (default 5).  At the limit the run AND the job go
terminally FAILED with the typed ``PREEMPTION_LIMIT`` reason -- never a
silent 6th preemption.  The bound is an explicit coordinator parameter so a
future owner policy surface can thread it through without redesign; no new
owner config schema is invented here.

Crash/replay discipline: every side effect is ordered so a crash lands in
exactly one recoverable state, and :func:`complete_preemption` is idempotent
-- a repeated quota event for the same run, a restart between any two steps,
or a reconciler replay converges instead of double-counting.  Steps that run
before the run is terminalized return "not owned" on failure so the caller
keeps its existing truthful FAILED path; nothing after terminalization ever
raises (terminal state is monotonic and must never regress to FAILED).

No provider, model, network, billing-posture, quota-limit, admission, or
owner-authority decision is made or bypassed here: this module only parks and
restores verified durable state.  Standalone ``orch run`` executions without
a durable job association always report "not owned" and keep their existing
truthful failure behavior.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from saberops.git import (
    MAX_CHECKPOINT_INDEX,
    GitManager,
    validate_candidate_sha,
)
from saberops.jobs.models import (
    PREEMPTION_LIMIT,
    Job,
    JobState,
    ResumeFrom,
    WaitingReason,
)
from saberops.models import (
    TERMINAL_RUN_STATUSES,
    RunStatus,
    WorkerCheckpoint,
    WorkerInterruptionReason,
)

#: Default bound from ADR-0002 D8: how many times one job may be preempted
#: before it goes terminally FAILED with ``PREEMPTION_LIMIT``.
DEFAULT_MAX_PREEMPTIONS: int = 5

_QUOTA_EXHAUSTED = WorkerInterruptionReason.QUOTA_EXHAUSTED.value

#: Reset confidences that count as evidence-backed ETAs.  Anything else
#: (HINT/UNKNOWN/missing) parks the job with explicit uncertainty
#: (``not_before=None``) and the accepted probe/periodic wake behavior --
#: never a guessed timestamp, never unsafe early re-admission.
_EVIDENCE_BACKED_RESET = frozenset({"EXACT", "PARSED"})


@dataclass(frozen=True)
class PreemptionReadiness:
    """Whether a run currently qualifies for D8 preemption, and why."""

    eligible: bool
    detail: str
    job: Job | None = None
    genuine_quota: bool = False
    no_eligible_candidate: bool = False
    not_before: str | None = None
    checkpoint_sha: str | None = None


@dataclass(frozen=True)
class PreemptionOutcome:
    """Terminal responsibility taken (or declined) by :func:`complete_preemption`."""

    owned: bool
    detail: str
    status: RunStatus | None = None
    summary: str = ""
    job: Job | None = None
    checkpoint_sha: str | None = None
    checkpoint_ref: str | None = None
    handoff_id: str | None = None
    preemptions: int | None = None
    not_before: str | None = None
    limit_reached: bool = False


@dataclass(frozen=True)
class ResumePreparation:
    """Result of :func:`prepare_resume` for one freshly dispatched run."""

    resumed: bool
    reason: str
    checkpoint_sha: str | None = None
    handoff_id: str | None = None


def find_job_for_run(db: Any, run_id: str) -> Job | None:
    """Return the durable RUNNING job whose ``current_run_id`` is *run_id*.

    Standalone runs have no job row and yield None (their existing truthful
    failure behavior is preserved).  At most one RUNNING job can own a run:
    dispatch CAS guarantees ``current_run_id`` uniqueness per live job.
    """
    from saberops.jobs import store

    if not isinstance(run_id, str) or not run_id:
        return None
    for job in store.list_jobs_raw(db, state=JobState.RUNNING, limit=100_000):
        if job.current_run_id == run_id:
            return job
    return None


def find_preempted_job_for_run(db: Any, run_id: str) -> Job | None:
    """Return the QUEUED/PREEMPTED job already requeued from *run_id*, if any.

    This is the idempotency anchor for replays: after a completed
    preemption the owning job is QUEUED (not RUNNING), so
    :func:`find_job_for_run` no longer sees it.  Matches only on an exact
    ``resume_from.run_id`` so a later re-dispatch of the same job (a NEW
    ``current_run_id``) is never confused with the preempted generation.
    """
    from saberops.jobs import store

    if not isinstance(run_id, str) or not run_id:
        return None
    for job in store.list_jobs_raw(db, state=JobState.QUEUED, limit=100_000):
        if job.waiting_reason != WaitingReason.PREEMPTED:
            continue
        resume = job.resume_from
        if resume is not None and resume.run_id == run_id:
            return job
    return None


def _find_any_job_for_run(db: Any, run_id: str) -> Job | None:
    """Return any job related to *run_id* regardless of state.

    Used only for accurate decline diagnostics (cancelled vs moved-on vs
    genuinely standalone).  Matches the live owner first, then an already
    requeued preemption generation, then historical dispatch lineage.
    """
    from saberops.jobs import store

    if not isinstance(run_id, str) or not run_id:
        return None
    try:
        jobs = store.list_jobs_raw(db, state=None, limit=100_000)
    except Exception:
        return None
    for job in jobs:
        if job.current_run_id == run_id:
            return job
    for job in jobs:
        if job.waiting_reason != WaitingReason.PREEMPTED:
            continue
        resume = job.resume_from
        if resume is not None and resume.run_id == run_id:
            return job
    for job in jobs:
        if run_id in job.run_ids:
            return job
    return None
    """Return the QUEUED/PREEMPTED job already requeued from *run_id*, if any.

    This is the idempotency anchor for replays: after a completed
    preemption the owning job is QUEUED (not RUNNING), so
    :func:`find_job_for_run` no longer sees it.  Matches only on an exact
    ``resume_from.run_id`` so a later re-dispatch of the same job (a NEW
    ``current_run_id``) is never confused with the preempted generation.
    """
    from saberops.jobs import store

    if not isinstance(run_id, str) or not run_id:
        return None
    for job in store.list_jobs_raw(db, state=JobState.QUEUED, limit=100_000):
        if job.waiting_reason != WaitingReason.PREEMPTED:
            continue
        resume = job.resume_from
        if resume is not None and resume.run_id == run_id:
            return job
    return None


def _event_payloads(db: Any, run_id: str, event_type: str) -> list[dict[str, Any]]:
    try:
        events = db.get_events_by_type(run_id, event_type)
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for event in events:
        payload = getattr(event, "payload", None)
        out.append(dict(payload) if isinstance(payload, dict) else {})
    return out


def has_genuine_quota_signal(db: Any, run_id: str) -> bool:
    """True iff the run durably recorded a QUOTA_EXHAUSTED interruption.

    Any one of three durable channels counts: an attempt carrying the
    interruption reason (set only by worker exhaustion classifiers, never by
    prose matching), a worker checkpoint recorded for that reason, or a
    ``quota_exhaustion_recorded`` evidence event.  Ordinary errors, model
    failures, denied auth, unverified posture, generic quota-unknown,
    stalls, and review failures leave none of these behind.
    """
    try:
        for attempt in db.get_attempts_for_run(run_id):
            if getattr(attempt, "interruption_reason", None) == _QUOTA_EXHAUSTED:
                return True
    except Exception:
        pass
    try:
        for checkpoint in db.get_worker_checkpoints_for_run(run_id):
            if getattr(checkpoint, "reason", None) == _QUOTA_EXHAUSTED:
                return True
    except Exception:
        pass
    return bool(_event_payloads(db, run_id, "quota_exhaustion_recorded"))


def has_no_eligible_evidence(db: Any, run_id: str) -> bool:
    """True iff the run determined it has no admitted fallback route left.

    Two durable shapes count, both produced by the run's own M2-4b ladder
    (never re-derived here by re-running admission):

    1. a ``no_eligible_candidate`` event -- written when the ladder skips
       every remaining candidate (the multi-candidate shape); or
    2. every launched attempt ended with a QUOTA_EXHAUSTED interruption --
       the single-candidate shape, where the ladder breaks on exhaustion
       before a skip is ever recorded because the only route died of quota.

    Ordinary gate failures, model errors, aborts, and review failures leave
    at least one non-quota attempt behind and never qualify.
    """
    if _event_payloads(db, run_id, "no_eligible_candidate"):
        return True
    try:
        attempts = db.get_attempts_for_run(run_id)
    except Exception:
        return False
    if not attempts:
        return False
    return all(
        getattr(attempt, "interruption_reason", None) == _QUOTA_EXHAUSTED
        for attempt in attempts
    )


def quota_reset_eta(db: Any, run_id: str) -> str | None:
    """Latest evidence-backed quota-reset instant for the run, if any.

    Only EXACT/PARSED ``until_confidence`` observations with a parseable
    timezone-aware ISO instant qualify.  HINT/UNKNOWN resets yield None so
    the caller parks with explicit uncertainty instead of a guessed ETA.
    """
    best: str | None = None
    for payload in _event_payloads(db, run_id, "quota_exhaustion_recorded"):
        confidence = str(payload.get("until_confidence") or "").upper()
        if confidence not in _EVIDENCE_BACKED_RESET:
            continue
        raw_until = payload.get("until") or payload.get("reset_at")
        if not isinstance(raw_until, str) or not raw_until:
            continue
        try:
            instant = datetime.fromisoformat(raw_until)
        except ValueError:
            continue
        if instant.tzinfo is None:
            continue
        best = raw_until
    return best


def latest_checkpoint_sha(db: Any, run_id: str) -> str | None:
    """Newest worker-checkpoint SHA recorded for the run, if any."""
    try:
        latest = db.get_latest_worker_checkpoint(run_id)
    except Exception:
        return None
    sha = getattr(latest, "checkpoint_sha", None)
    return sha if isinstance(sha, str) and sha else None


def should_preempt(
    db: Any,
    run_id: str,
    *,
    job: Job | None = None,
    max_preemptions: int = DEFAULT_MAX_PREEMPTIONS,
) -> PreemptionReadiness:
    """Decide whether *run_id* qualifies for D8 preemption right now.

    Pure read-only predicate: no Git, handoff, run, job, or ledger state is
    touched.  All four gates must hold: genuine quota signal, recorded
    no-eligible determination, a RUNNING job owning this exact run, and
    remaining preemption budget.
    """
    owner = job if job is not None else find_job_for_run(db, run_id)
    if owner is None:
        return PreemptionReadiness(
            eligible=False, detail="no_job", job=None,
            genuine_quota=False, no_eligible_candidate=False,
        )
    genuine = has_genuine_quota_signal(db, run_id)
    no_eligible = has_no_eligible_evidence(db, run_id)
    if not genuine:
        return PreemptionReadiness(
            eligible=False, detail="no_quota_signal", job=owner,
            genuine_quota=False, no_eligible_candidate=no_eligible,
        )
    if not no_eligible:
        return PreemptionReadiness(
            eligible=False, detail="eligible_candidate_remains", job=owner,
            genuine_quota=True, no_eligible_candidate=False,
        )
    if owner.state != JobState.RUNNING or owner.current_run_id != run_id:
        return PreemptionReadiness(
            eligible=False, detail="job_not_owned", job=owner,
            genuine_quota=True, no_eligible_candidate=True,
        )
    if owner.preemptions >= max_preemptions:
        return PreemptionReadiness(
            eligible=False, detail="preemption_limit", job=owner,
            genuine_quota=True, no_eligible_candidate=True,
        )
    return PreemptionReadiness(
        eligible=True, detail="ok", job=owner,
        genuine_quota=True, no_eligible_candidate=True,
        not_before=quota_reset_eta(db, run_id),
        checkpoint_sha=latest_checkpoint_sha(db, run_id),
    )


def _attempt_worktree_path(db: Any, run_id: str) -> str | None:
    """Newest attempt worktree path recorded for the run, if any."""
    try:
        attempts = db.get_attempts_for_run(run_id)
    except Exception:
        return None
    if not attempts:
        return None
    path = getattr(attempts[-1], "worktree_path", None)
    return path if isinstance(path, str) and path else None


def _resolve_anchor_sha(
    db: Any,
    *,
    repo_path: Path,
    run_id: str,
    job: Job,
    worktree_path: str | None,
    base_sha: str | None,
) -> str | None:
    """Resolve the exact commit SHA to anchor, failing closed with None.

    Preference order: the newest recorded worker checkpoint (verified present
    in the repo) -- the interruption path already committed the WIP there;
    else a fresh checkpoint commit of dirty worktree state (the worktree is
    located from the explicit hint or, when absent, from the newest attempt
    row); else the exact existing worktree HEAD (never a fabricated hash);
    else the job's frozen base SHA.  Every candidate is verified to exist
    before it is returned.
    """
    recorded = latest_checkpoint_sha(db, run_id)
    if recorded is not None:
        try:
            if GitManager.commit_exists(repo_path, recorded):
                return recorded
        except Exception:
            return None
    hint = worktree_path or _attempt_worktree_path(db, run_id)
    if hint:
        work = Path(hint)
        try:
            if work.exists():
                changed = GitManager.get_changed_paths(work)
                if changed:
                    sha = GitManager.create_checkpoint_commit(
                        work,
                        f"orch checkpoint({run_id}/preempt): "
                        "partial worker state (QUOTA_EXHAUSTED)",
                    )
                    if sha is not None:
                        try:
                            validate_candidate_sha(sha)
                        except Exception:
                            return None
                        return sha
                head = GitManager.get_ref_commit(work, "HEAD")
                if head is not None:
                    try:
                        validate_candidate_sha(head)
                    except Exception:
                        return None
                    return head
        except Exception:
            return None
        return None
    for candidate in (base_sha, job.base_sha):
        if not candidate:
            continue
        try:
            validate_candidate_sha(candidate)
        except Exception:
            continue
        try:
            if GitManager.commit_exists(repo_path, candidate):
                return candidate
        except Exception:
            return None
    return None


def _ensure_old_checkpoint_row(
    db: Any,
    *,
    run_id: str,
    job: Job,
    checkpoint_sha: str,
    handoff_id: str | None,
    now_iso: str,
) -> None:
    """Ensure the old run owns a checkpoint row for the anchored SHA.

    Reuses the existing row when one already covers the SHA (the normal
    interruption-path case); otherwise records a factual row carrying the
    available lineage.  The row references a REAL attempt id: the
    checkpoint table has a foreign key onto attempts, so a fabricated or
    empty source is refused by the database itself.  When the run launched
    nothing (zero attempts) no row can be recorded honestly and none is --
    the anchor ref plus the resume capsule already carry the durable
    resume contract, and the tree is identical to base.  Best-effort
    history only: persistence failure here must not block preemption.
    """
    try:
        if db.get_worker_checkpoint_by_sha(run_id, checkpoint_sha) is not None:
            return
    except Exception:
        return
    source_attempt = ""
    reason = _QUOTA_EXHAUSTED
    changed: tuple[str, ...] = ()
    try:
        latest = db.get_latest_worker_checkpoint(run_id)
        if latest is not None:
            source_attempt = str(getattr(latest, "source_attempt_id", "") or "")
            reason = str(getattr(latest, "reason", "") or _QUOTA_EXHAUSTED)
            raw_changed = getattr(latest, "changed_paths", ())
            changed = tuple(str(p) for p in (raw_changed or ()))
    except Exception:
        pass
    if not source_attempt:
        try:
            attempts = db.get_attempts_for_run(run_id)
            if attempts:
                last = attempts[-1]
                source_attempt = str(getattr(last, "id", "") or "")
                if getattr(last, "interruption_reason", None):
                    reason = str(last.interruption_reason)
        except Exception:
            pass
    if not source_attempt:
        return
    try:
        db.create_worker_checkpoint(
            WorkerCheckpoint(
                id=f"cp_{uuid.uuid4().hex[:10]}",
                run_id=run_id,
                source_attempt_id=source_attempt,
                source_base_sha=job.base_sha,
                checkpoint_sha=checkpoint_sha,
                reason=reason,
                changed_paths=changed,
                created_at=now_iso,
                handoff_id=handoff_id,
            )
        )
    except Exception:
        pass


def _intent_payload(db: Any, run_id: str, job_id: str) -> dict[str, Any] | None:
    """Return the recorded ``preemption_committed`` intent for the job, if any."""
    for payload in _event_payloads(db, run_id, "preemption_committed"):
        if payload.get("job_id") == job_id:
            return payload
    return None


def _anchor_verified_ref(
    repo_path: Path, *, job_id: str, checkpoint_sha: str
) -> tuple[str, int]:
    """Anchor *checkpoint_sha*, reusing the existing index when already anchored.

    Returns ``(ref, index)``.  The index is the smallest non-negative integer
    not yet taken, so a retry that already anchored this SHA reuses its
    index instead of spraying a duplicate ref (bounded monotonic ``n``).
    """
    existing = GitManager.list_checkpoint_refs(repo_path, job_id)
    for index, sha in sorted(existing.items()):
        if sha == checkpoint_sha:
            return (f"refs/saberops/checkpoints/{job_id}/{index}", index)
    index = (max(existing) + 1) if existing else 0
    if index > MAX_CHECKPOINT_INDEX:
        raise ValueError(f"checkpoint index {index} exceeds bound")
    ref = GitManager.anchor_checkpoint_ref(repo_path, job_id, index, checkpoint_sha)
    return (ref, index)


def _terminalize_preempted(
    db: Any, *, run_id: str, summary: str, now_iso: str
) -> tuple[bool, bool]:
    """Move RUNNING -> PREEMPTED exactly once.

    Returns ``(ok, transitioned)``: ``ok`` is True when the run is (now)
    PREEMPTED; ``transitioned`` is True only when this call performed the
    transition, so replay callers can skip re-emitting terminal events.
    Any other terminal state, a missing run, or a non-RUNNING live state
    yields ``(False, False)`` and touches nothing.
    """
    try:
        run = db.get_run(run_id)
    except Exception:
        return (False, False)
    if run is None:
        return (False, False)
    if run.status == RunStatus.PREEMPTED:
        return (True, False)
    if run.status in TERMINAL_RUN_STATUSES:
        return (False, False)
    if run.status != RunStatus.RUNNING:
        return (False, False)
    try:
        db.update_run(
            run_id=run_id,
            status=RunStatus.PREEMPTED,
            completed_at=now_iso,
            result_summary=summary,
        )
    except Exception:
        return (False, False)
    return (True, True)


def _fail_at_limit(
    db: Any,
    *,
    run_id: str,
    job: Job,
    max_preemptions: int,
    now: datetime,
    now_iso: str,
) -> PreemptionOutcome:
    """Record terminal FAILED ``PREEMPTION_LIMIT`` on the run and the job."""
    from saberops.capacity.ledger import release
    from saberops.jobs import store

    summary = (
        f"Job {job.job_id} reached the preemption bound "
        f"({job.preemptions}/{max_preemptions}): PREEMPTION_LIMIT. "
        "Quota exhaustion recurred with no admitted fallback route; "
        "not requeueing again."
    )
    try:
        run = db.get_run(run_id)
        if run is not None and run.status == RunStatus.RUNNING:
            db.update_run(
                run_id=run_id,
                status=RunStatus.FAILED,
                completed_at=now_iso,
                result_summary=summary,
            )
            db.record_event(
                run_id,
                "run_failed",
                payload={
                    "summary": summary,
                    "reason": PREEMPTION_LIMIT,
                    "preemptions": job.preemptions,
                    "max_preemptions": max_preemptions,
                },
            )
    except Exception:
        pass
    failed_job: Job | None = None
    try:
        fresh = store.get_job(db, job.job_id)
        if fresh is not None and fresh.state == JobState.RUNNING:
            failed_job = store.reconcile_transition(
                db, job.job_id, to=JobState.FAILED,
                failure_reason=PREEMPTION_LIMIT, now=now,
            )
    except Exception:
        failed_job = None
    try:
        release(db, run_id, now=now)
    except Exception:
        pass
    return PreemptionOutcome(
        owned=True, detail="preemption_limit", status=RunStatus.FAILED,
        summary=summary, job=failed_job if failed_job is not None else job,
        preemptions=job.preemptions, limit_reached=True,
    )


def complete_preemption(
    db: Any,
    *,
    run_id: str,
    repo_path: str | Path,
    job_id: str | None = None,
    worktree_path: str | None = None,
    base_sha: str | None = None,
    max_preemptions: int = DEFAULT_MAX_PREEMPTIONS,
    now: datetime | None = None,
) -> PreemptionOutcome:
    """Execute (or idempotently complete) the D8 preemption protocol.

    Ordered for crash safety: checkpoint commit -> resume capsule -> anchor
    ref -> durable intent event -> run terminalization -> job requeue ->
    reservation release.  A crash before terminalization leaves the caller's
    existing FAILED path intact (reconciler replays from durable state); a
    crash after terminalization is completed idempotently from the intent
    event.  Never raises for expected failure modes: declines with
    ``owned=False`` instead, so the caller keeps its truthful behavior.
    """
    from saberops.capacity.ledger import release
    from saberops.jobs import service, store

    moment = now if now is not None else datetime.now(UTC)
    now_iso = moment.isoformat()
    repo = Path(repo_path).resolve() if not isinstance(repo_path, Path) else repo_path

    # Cheap durable-evidence gates first: ordinary failures (no quota
    # signal, or candidates remaining) decline here without a job-table
    # scan, so the normal FAILED path is untouched and unslowed.
    try:
        run_row = db.get_run(run_id)
    except Exception:
        return PreemptionOutcome(owned=False, detail="run_lookup_failed")
    if run_row is None:
        return PreemptionOutcome(owned=False, detail="no_run")
    if run_row.status == RunStatus.PREEMPTED:
        run_already_preempted = True
    elif run_row.status != RunStatus.RUNNING:
        return PreemptionOutcome(owned=False, detail="run_not_running")
    else:
        run_already_preempted = False
    if not run_already_preempted:
        if not has_genuine_quota_signal(db, run_id):
            return PreemptionOutcome(owned=False, detail="no_quota_signal")
        if not has_no_eligible_evidence(db, run_id):
            return PreemptionOutcome(owned=False, detail="eligible_candidate_remains")

    job: Job | None
    if job_id is not None:
        try:
            job = store.get_job(db, job_id)
        except Exception:
            return PreemptionOutcome(owned=False, detail="job_lookup_failed")
    else:
        try:
            job = find_job_for_run(db, run_id)
        except Exception:
            return PreemptionOutcome(owned=False, detail="job_lookup_failed")
        if job is None and run_already_preempted:
            # Completed preemption replay: the owning job is QUEUED now.
            try:
                job = find_preempted_job_for_run(db, run_id)
            except Exception:
                return PreemptionOutcome(owned=False, detail="job_lookup_failed")
    if job is None:
        # No RUNNING owner.  Distinguish a raced job (cancelled, moved on)
        # from a genuinely standalone run before declining, so the caller
        # -- and the operator reading the detail -- sees the true cause.
        # Standalone runs keep their existing truthful behavior either way.
        related = _find_any_job_for_run(db, run_id)
        if related is not None:
            return PreemptionOutcome(
                owned=False, detail="job_not_owned", job=related
            )
        return PreemptionOutcome(owned=False, detail="no_job")
    if job.state == JobState.QUEUED and job.waiting_reason == WaitingReason.PREEMPTED:
        resume = job.resume_from
        if resume is not None and resume.run_id == run_id:
            # Already preempted by an earlier invocation: converge, count once.
            return PreemptionOutcome(
                owned=True, detail="already_preempted",
                status=RunStatus.PREEMPTED,
                summary=f"Run {run_id} already preempted for job {job.job_id}.",
                job=job, checkpoint_sha=resume.checkpoint_sha,
                handoff_id=resume.handoff_id, preemptions=job.preemptions,
                not_before=job.not_before,
            )
        return PreemptionOutcome(owned=False, detail="job_not_owned", job=job)
    if job.state != JobState.RUNNING or job.current_run_id != run_id:
        # Cancelled, terminal, or already re-dispatched: fail closed, touch nothing.
        return PreemptionOutcome(owned=False, detail="job_not_owned", job=job)
    if job.preemptions >= max_preemptions:
        return _fail_at_limit(
            db, run_id=run_id, job=job,
            max_preemptions=max_preemptions, now=moment, now_iso=now_iso,
        )

    not_before = quota_reset_eta(db, run_id)

    # Crash-replay fast path: a recorded intent already carries every
    # verified artifact; skip straight to terminalization + requeue.
    intent = _intent_payload(db, run_id, job.job_id)
    checkpoint_sha: str | None = None
    checkpoint_ref: str | None = None
    handoff_id: str | None = None
    if intent is not None:
        raw_sha = intent.get("checkpoint_sha")
        raw_ref = intent.get("checkpoint_ref")
        raw_handoff = intent.get("handoff_id")
        if (
            isinstance(raw_sha, str) and raw_sha
            and isinstance(raw_ref, str) and raw_ref
            and isinstance(raw_handoff, str) and raw_handoff
        ):
            checkpoint_sha, checkpoint_ref, handoff_id = raw_sha, raw_ref, raw_handoff

    if checkpoint_sha is None:
        checkpoint_sha = _resolve_anchor_sha(
            db, repo_path=repo, run_id=run_id, job=job,
            worktree_path=worktree_path, base_sha=base_sha,
        )
        if checkpoint_sha is None:
            return PreemptionOutcome(owned=False, detail="no_anchor", job=job)

        from saberops.handoff import create_handoffs

        try:
            _, capsule = create_handoffs(db, run_id)
            handoff_id = capsule.id
        except Exception:
            return PreemptionOutcome(owned=False, detail="capsule_failed", job=job)

        try:
            checkpoint_ref, _ = _anchor_verified_ref(
                repo, job_id=job.job_id, checkpoint_sha=checkpoint_sha
            )
        except Exception:
            return PreemptionOutcome(owned=False, detail="ref_failed", job=job)

        _ensure_old_checkpoint_row(
            db, run_id=run_id, job=job, checkpoint_sha=checkpoint_sha,
            handoff_id=handoff_id, now_iso=now_iso,
        )
        try:
            db.record_event(
                run_id,
                "preemption_committed",
                payload={
                    "job_id": job.job_id,
                    "checkpoint_sha": checkpoint_sha,
                    "checkpoint_ref": checkpoint_ref,
                    "handoff_id": handoff_id,
                    "not_before": not_before,
                    "preemptions_next": job.preemptions + 1,
                },
            )
        except Exception:
            return PreemptionOutcome(owned=False, detail="intent_failed", job=job)

    assert checkpoint_sha is not None
    assert checkpoint_ref is not None
    assert handoff_id is not None
    summary = (
        f"Run {run_id} PREEMPTED: genuine quota exhaustion with no admitted "
        f"fallback route. WIP anchored at {checkpoint_ref} "
        f"({checkpoint_sha[:12]}); job {job.job_id} requeued with resume capsule."
    )
    terminal_ok, terminal_transitioned = _terminalize_preempted(
        db, run_id=run_id, summary=summary, now_iso=now_iso
    )
    if not terminal_ok:
        return PreemptionOutcome(owned=False, detail="run_not_running", job=job)
    if terminal_transitioned:
        try:
            db.record_event(
                run_id,
                "run_preempted",
                payload={
                    "summary": summary,
                    "job_id": job.job_id,
                    "checkpoint_sha": checkpoint_sha,
                    "checkpoint_ref": checkpoint_ref,
                    "handoff_id": handoff_id,
                    "not_before": not_before,
                },
            )
        except Exception:
            pass

    try:
        resume_from = ResumeFrom(
            run_id=run_id, checkpoint_sha=checkpoint_sha, handoff_id=handoff_id
        )
    except ValueError:
        return PreemptionOutcome(
            owned=True, detail="resume_malformed", status=RunStatus.PREEMPTED,
            summary=summary, job=job, checkpoint_sha=checkpoint_sha,
            checkpoint_ref=checkpoint_ref, handoff_id=handoff_id,
            preemptions=job.preemptions, not_before=not_before,
        )
    requeued: Job | None = None
    try:
        requeued = service.requeue(
            db, job.job_id, WaitingReason.PREEMPTED,
            resume_from=resume_from, not_before=not_before, now=moment,
        )
    except Exception:
        requeued = None
    if requeued is None:
        # Lost the requeue race (cancel/concurrent dispatch): the run is
        # conclusively terminal, so still release its reservation, but never
        # touch a job we no longer own.
        try:
            release(db, run_id, now=moment)
        except Exception:
            pass
        try:
            current = store.get_job(db, job.job_id)
        except Exception:
            current = None
        return PreemptionOutcome(
            owned=True, detail="job_raced", status=RunStatus.PREEMPTED,
            summary=summary, job=current if current is not None else job,
            checkpoint_sha=checkpoint_sha, checkpoint_ref=checkpoint_ref,
            handoff_id=handoff_id, preemptions=job.preemptions,
            not_before=not_before,
        )
    try:
        release(db, run_id, now=moment)
    except Exception:
        # The scheduler-tick settlement (QUEUED -> release) completes this;
        # release is idempotent, so a later retry is safe.
        pass
    return PreemptionOutcome(
        owned=True, detail="preempted", status=RunStatus.PREEMPTED,
        summary=summary, job=requeued, checkpoint_sha=checkpoint_sha,
        checkpoint_ref=checkpoint_ref, handoff_id=handoff_id,
        preemptions=requeued.preemptions, not_before=requeued.not_before,
    )


def prepare_resume(
    db: Any,
    *,
    job: Job,
    new_run_id: str,
    repo_path: str | Path | None = None,
    now: datetime | None = None,
) -> ResumePreparation:
    """Verify a job's ``resume_from`` and stage it into a fresh run.

    Every artifact is re-verified fail-closed: the capsule must exist and
    belong to the recorded run, the checkpoint SHA must be a well-formed
    commit present in the job repo and still anchored under the job's
    engine-owned ref namespace, and the job base must still resolve.  Any
    mismatch stages nothing (the new run launches fresh from base) and
    records a ``resume_skipped`` event with the exact cause.

    On success a WorkerCheckpoint row is copied onto the NEW run carrying
    the verified SHA plus the capsule id, so the run's ordinary
    continuation channel restores the worktree at the checkpoint and the
    factual capsule flows into dispatch evidence -- through the normal
    M2-4b admission guard, never around it.

    The caller must have created the new run row first (the scheduler
    launcher does): checkpoint and event writes reference it by foreign
    key, so staging into a nonexistent run fails closed with a skip.
    """
    moment = now if now is not None else datetime.now(UTC)
    now_iso = moment.isoformat()
    resume = job.resume_from
    if resume is None:
        return ResumePreparation(resumed=False, reason="no_resume")
    repo = Path(repo_path) if repo_path is not None else Path(job.repo)
    try:
        handoff = db.get_handoff(resume.handoff_id)
    except Exception:
        handoff = None
    if handoff is None or getattr(handoff, "run_id", None) != resume.run_id:
        try:
            db.record_event(
                new_run_id, "resume_skipped",
                payload={
                    "reason": "capsule_mismatch",
                    "job_id": job.job_id,
                    "resume_run_id": resume.run_id,
                },
            )
        except Exception:
            pass
        return ResumePreparation(resumed=False, reason="capsule_mismatch")
    try:
        validate_candidate_sha(resume.checkpoint_sha)
        sha_ok = GitManager.commit_exists(repo, resume.checkpoint_sha)
    except Exception:
        sha_ok = False
    if not sha_ok:
        try:
            db.record_event(
                new_run_id, "resume_skipped",
                payload={
                    "reason": "checkpoint_unreachable",
                    "job_id": job.job_id,
                    "resume_run_id": resume.run_id,
                },
            )
        except Exception:
            pass
        return ResumePreparation(resumed=False, reason="checkpoint_unreachable")
    try:
        anchored = GitManager.list_checkpoint_refs(repo, job.job_id)
        still_anchored = resume.checkpoint_sha in set(anchored.values())
    except Exception:
        still_anchored = False
    if not still_anchored:
        try:
            db.record_event(
                new_run_id, "resume_skipped",
                payload={
                    "reason": "checkpoint_unanchored",
                    "job_id": job.job_id,
                    "resume_run_id": resume.run_id,
                },
            )
        except Exception:
            pass
        return ResumePreparation(resumed=False, reason="checkpoint_unanchored")
    try:
        base_ok = GitManager.commit_exists(repo, job.base_sha)
    except Exception:
        base_ok = False
    if not base_ok:
        try:
            db.record_event(
                new_run_id, "resume_skipped",
                payload={
                    "reason": "stale_base",
                    "job_id": job.job_id,
                    "resume_run_id": resume.run_id,
                },
            )
        except Exception:
            pass
        return ResumePreparation(resumed=False, reason="stale_base")

    source_attempt = ""
    reason = _QUOTA_EXHAUSTED
    changed: tuple[str, ...] = ()
    try:
        original = db.get_worker_checkpoint_by_sha(resume.run_id, resume.checkpoint_sha)
    except Exception:
        original = None
    if original is not None:
        source_attempt = str(getattr(original, "source_attempt_id", "") or "")
        reason = str(getattr(original, "reason", "") or _QUOTA_EXHAUSTED)
        raw_changed = getattr(original, "changed_paths", ())
        changed = tuple(str(p) for p in (raw_changed or ()))
    if source_attempt:
        try:
            if db.get_attempt(source_attempt) is None:
                source_attempt = ""
        except Exception:
            source_attempt = ""
    if not source_attempt:
        try:
            old_attempts = db.get_attempts_for_run(resume.run_id)
            if old_attempts:
                source_attempt = str(getattr(old_attempts[-1], "id", "") or "")
        except Exception:
            source_attempt = ""
    if not source_attempt:
        # Zero-launch preemption: no attempt exists to reference (the
        # checkpoint table has a foreign key onto attempts), and the tree
        # is identical to base, so there is no WIP to restore.  Stage
        # nothing; the new run launches fresh with an exact-cause event.
        try:
            db.record_event(
                new_run_id, "resume_skipped",
                payload={
                    "reason": "no_source_attempt",
                    "job_id": job.job_id,
                    "resume_run_id": resume.run_id,
                },
            )
        except Exception:
            pass
        return ResumePreparation(resumed=False, reason="no_source_attempt")
    try:
        db.create_worker_checkpoint(
            WorkerCheckpoint(
                id=f"cp_{uuid.uuid4().hex[:10]}",
                run_id=new_run_id,
                # Intentional cross-run lineage: the attempt that produced
                # the checkpoint lives on the preempted run; the new run
                # reads only its provider/reason/SHA through the ordinary
                # continuation channel.  The id is real, so the FK holds.
                source_attempt_id=source_attempt,
                source_base_sha=job.base_sha,
                checkpoint_sha=resume.checkpoint_sha,
                reason=reason,
                changed_paths=changed,
                created_at=now_iso,
                handoff_id=resume.handoff_id,
            )
        )
    except Exception:
        return ResumePreparation(resumed=False, reason="checkpoint_stage_failed")
    try:
        db.record_event(
            new_run_id, "resume_prepared",
            payload={
                "job_id": job.job_id,
                "resume_run_id": resume.run_id,
                "checkpoint_sha": resume.checkpoint_sha,
                "handoff_id": resume.handoff_id,
            },
        )
    except Exception:
        pass
    return ResumePreparation(
        resumed=True, reason="resumed",
        checkpoint_sha=resume.checkpoint_sha, handoff_id=resume.handoff_id,
    )


__all__ = [
    "DEFAULT_MAX_PREEMPTIONS",
    "PreemptionOutcome",
    "PreemptionReadiness",
    "ResumePreparation",
    "complete_preemption",
    "find_job_for_run",
    "find_preempted_job_for_run",
    "has_genuine_quota_signal",
    "has_no_eligible_evidence",
    "latest_checkpoint_sha",
    "prepare_resume",
    "quota_reset_eta",
    "should_preempt",
]
