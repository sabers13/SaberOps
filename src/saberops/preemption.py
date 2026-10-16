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
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from saberops.git import (
    MAX_CHECKPOINT_INDEX,
    CandidatePublicationError,
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
    AttemptStatus,
    RunStatus,
    WorkerCheckpoint,
    WorkerInterruptionReason,
)
from saberops.ownership import ExecutionOwner
from saberops.process import run_process

#: Default bound from ADR-0002 D8: how many times one job may be preempted
#: before it goes terminally FAILED with ``PREEMPTION_LIMIT``.
DEFAULT_MAX_PREEMPTIONS: int = 5

#: R2 durable receipt: the factual "D8 owns this terminal outcome" decision,
#: written BEFORE any preemption side effect (checkpoint/ref/capsule/intent).
PREPARATION_EVENT = "preemption_preparing"
#: Durable prepared-artifact record (the crash-replay intent).
INTENT_EVENT = "preemption_committed"
#: Terminal run event for a completed park.
RUN_PREEMPTED_EVENT = "run_preempted"
#: Bound payload schema version for the decision receipt and the intent.
DECISION_VERSION: int = 1
INTENT_VERSION: int = 2

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


@dataclass(frozen=True)
class PreemptionDecision:
    """Bounded, factual receipt that D8 owns a run's terminal outcome (R2).

    Written *before* any preemption side effect so a crash at any later
    boundary is recognisable from durable state alone.  It carries only facts
    already durable at decision time -- source attempt identities/states, the
    frozen base, the repository, and (when the caller can supply it) the exact
    execution-owner generation that made the decision.  It is deliberately
    *not* an authority token: recovery still re-checks the live owner/job.
    """

    run_id: str
    job_id: str
    preemptions_before: int
    base_sha: str
    repo: str
    source_attempt_ids: tuple[str, ...] = ()
    source_attempt_states: tuple[str, ...] = ()
    owner_id: str | None = None
    owner_generation: int | None = None
    decision_ref: str | None = None
    version: int = DECISION_VERSION

    def to_payload(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "run_id": self.run_id,
            "job_id": self.job_id,
            "preemptions_before": self.preemptions_before,
            "base_sha": self.base_sha,
            "repo": self.repo,
            "source_attempt_ids": list(self.source_attempt_ids),
            "source_attempt_states": list(self.source_attempt_states),
            "owner_id": self.owner_id,
            "owner_generation": self.owner_generation,
            "decision_ref": self.decision_ref,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any] | None) -> PreemptionDecision | None:
        if not isinstance(payload, Mapping):
            return None
        try:
            run_id = str(payload["run_id"])
            job_id = str(payload["job_id"])
            preemptions_before = int(payload["preemptions_before"])
            base_sha = str(payload["base_sha"])
            repo = str(payload["repo"])
        except (KeyError, TypeError, ValueError):
            return None
        if not run_id or not job_id or preemptions_before < 0:
            return None
        raw_ids = payload.get("source_attempt_ids") or ()
        raw_states = payload.get("source_attempt_states") or ()
        ids = (
            tuple(str(x) for x in raw_ids)
            if isinstance(raw_ids, (list, tuple))
            else ()
        )
        states = (
            tuple(str(x) for x in raw_states)
            if isinstance(raw_states, (list, tuple))
            else ()
        )
        owner_raw = payload.get("owner_id")
        gen_raw = payload.get("owner_generation")
        ref_raw = payload.get("decision_ref")
        return cls(
            run_id=run_id,
            job_id=job_id,
            preemptions_before=preemptions_before,
            base_sha=base_sha,
            repo=repo,
            source_attempt_ids=ids,
            source_attempt_states=states,
            owner_id=str(owner_raw) if isinstance(owner_raw, str) and owner_raw else None,
            owner_generation=int(gen_raw) if isinstance(gen_raw, int) else None,
            decision_ref=str(ref_raw) if isinstance(ref_raw, str) and ref_raw else None,
            version=int(payload.get("version") or 1),
        )


@dataclass(frozen=True)
class PreparedArtifacts:
    """A *verified* prepared preemption: WIP anchor + factual capsule."""

    checkpoint_sha: str
    checkpoint_ref: str
    handoff_id: str
    not_before: str | None = None
    preemptions_next: int | None = None


@dataclass(frozen=True)
class PreemptionTransaction:
    """Classified durable state of one run's D8 transaction.

    ``state`` is one of ``none`` (no D8 receipt), ``preparing`` (a factual
    decision exists, side effects incomplete), ``prepared`` (verified
    artifacts exist), or ``blocked`` (a receipt exists but does not verify --
    fail closed, never fabricate a different transaction from it).
    """

    state: str
    detail: str
    artifacts: PreparedArtifacts | None = None
    decision: PreemptionDecision | None = None
    job: Job | None = None


def build_preemption_decision(
    db: Any,
    *,
    run_id: str,
    job: Job,
    owner_id: str | None = None,
    owner_generation: int | None = None,
    decision_ref: str | None = None,
) -> PreemptionDecision:
    """Assemble the factual D8 decision receipt from durable state."""
    attempt_ids: tuple[str, ...] = ()
    attempt_states: tuple[str, ...] = ()
    try:
        attempts = db.get_attempts_for_run(run_id)
    except Exception:
        attempts = []
    attempt_ids = tuple(str(getattr(a, "id", "") or "") for a in attempts)
    attempt_states = tuple(
        str(getattr(getattr(a, "status", None), "value", "") or "") for a in attempts
    )
    return PreemptionDecision(
        run_id=run_id,
        job_id=job.job_id,
        preemptions_before=int(job.preemptions),
        base_sha=str(job.base_sha or ""),
        repo=str(job.repo or ""),
        source_attempt_ids=attempt_ids,
        source_attempt_states=attempt_states,
        owner_id=owner_id,
        owner_generation=owner_generation,
        decision_ref=decision_ref,
    )


def _decision_payload(db: Any, run_id: str, job_id: str) -> dict[str, Any] | None:
    """Return the recorded ``preemption_preparing`` decision for the job."""
    for payload in _event_payloads(db, run_id, PREPARATION_EVENT):
        if payload.get("job_id") == job_id:
            return payload
    return None


def record_preparation_decision(db: Any, decision: PreemptionDecision) -> bool:
    """Persist the D8 decision receipt exactly once (idempotent).

    Returns True only when this call wrote the receipt.  Called *before* any
    checkpoint, capsule, ref or intent side effect so that a crash between the
    decision and its artifacts is later recognisable; a repeat never
    duplicates the receipt.
    """
    try:
        existing = _decision_payload(db, decision.run_id, decision.job_id)
    except Exception:
        existing = None
    if existing is not None:
        return False
    try:
        db.record_event(
            decision.run_id, PREPARATION_EVENT, payload=decision.to_payload()
        )
    except Exception:
        return False
    return True


def _parse_checkpoint_ref(ref: str, job_id: str) -> int | None:
    prefix = f"refs/saberops/checkpoints/{job_id}/"
    if not isinstance(ref, str) or not ref.startswith(prefix):
        return None
    suffix = ref[len(prefix):]
    if not suffix.isdigit():
        return None
    index = int(suffix)
    if not 0 <= index <= MAX_CHECKPOINT_INDEX:
        return None
    return index


def verify_prepared_artifacts(
    db: Any,
    *,
    run_id: str,
    job: Job,
    repo_path: Path,
    payload: Mapping[str, Any],
    expected_preemptions_before: int | None = None,
) -> tuple[PreparedArtifacts | None, str]:
    """Verify a recorded prepared-artifact intent against durable state.

    Everything must independently hold, or the caller must fail closed:

    * the intent names this job;
    * ``checkpoint_sha`` is a well-formed, existing commit in the job repo;
    * ``checkpoint_ref`` is a well-formed engine checkpoint ref for this job
      and that ref is *currently* a direct anchor resolving to exactly that
      SHA (R3 create-only/read-only discipline; a symbolic or colliding ref is
      refused rather than dereferenced);
    * ``handoff_id`` resolves to a real handoff row belonging to this run;
    * ``preemptions_next`` (when present) equals the expected pre-preemption
      count + 1.

    ``expected_preemptions_before`` defaults to the job's current count (the
    normal pre-preemption call site); a caller looking at an already-consumed
    generation (settlement) passes the pre-preemption count explicitly.
    Verifying artifacts is necessary but **not** sufficient authority: see
    :func:`authenticate_prepared_transaction`.
    """
    raw_sha = payload.get("checkpoint_sha")
    raw_ref = payload.get("checkpoint_ref")
    raw_handoff = payload.get("handoff_id")
    if not isinstance(raw_sha, str) or not raw_sha:
        return None, "missing_sha"
    if not isinstance(raw_ref, str) or not raw_ref:
        return None, "missing_ref"
    if not isinstance(raw_handoff, str) or not raw_handoff:
        return None, "missing_handoff"
    try:
        validate_candidate_sha(raw_sha)
        if not GitManager.commit_exists(repo_path, raw_sha):
            return None, "unreachable_sha"
    except Exception:
        return None, "unreachable_sha"
    index = _parse_checkpoint_ref(raw_ref, job.job_id)
    if index is None:
        return None, "malformed_ref"
    try:
        anchored = GitManager.read_checkpoint_ref(repo_path, job.job_id, index)
    except CandidatePublicationError:
        return None, "symbolic_ref"
    except Exception:
        return None, "ref_read_failed"
    if anchored != raw_sha:
        return None, "ref_mismatch"
    try:
        handoff = db.get_handoff(raw_handoff)
    except Exception:
        return None, "handoff_read_failed"
    if handoff is None or str(getattr(handoff, "run_id", "") or "") != run_id:
        return None, "handoff_mismatch"
    raw_next = payload.get("preemptions_next")
    preemptions_next: int | None = None
    before = (
        int(job.preemptions)
        if expected_preemptions_before is None
        else int(expected_preemptions_before)
    )
    if raw_next is not None:
        try:
            preemptions_next = int(raw_next)
        except (TypeError, ValueError):
            return None, "malformed_count"
        if preemptions_next != before + 1:
            return None, "count_mismatch"
    raw_nb = payload.get("not_before")
    not_before = raw_nb if isinstance(raw_nb, str) and raw_nb else None
    return (
        PreparedArtifacts(
            checkpoint_sha=raw_sha,
            checkpoint_ref=raw_ref,
            handoff_id=raw_handoff,
            not_before=not_before,
            preemptions_next=preemptions_next,
        ),
        "ok",
    )


def _attempt_snapshot(db: Any, run_id: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Current durable (attempt_ids, attempt_states) snapshot for the run."""
    try:
        attempts = db.get_attempts_for_run(run_id)
    except Exception:
        return (), ()
    ids = tuple(str(getattr(a, "id", "") or "") for a in attempts)
    states = tuple(
        str(getattr(getattr(a, "status", None), "value", "") or "") for a in attempts
    )
    return ids, states


def _decision_owner_binding(db: Any, run_id: str, decision: PreemptionDecision) -> tuple[bool, str]:
    """True iff the decision's owner binding names the durable owner row.

    The receipt must name the *exact* executor generation that took the
    decision.  A newer/older generation -- or no row at all -- cannot be
    authenticated, so the caller must fail closed.
    """
    if decision.owner_id is None or decision.owner_generation is None:
        return False, "owner_unbound"
    try:
        owner = db.get_execution_owner(run_id)
    except Exception:
        return False, "owner_unreadable"
    if owner is None:
        return False, "owner_missing"
    if owner.owner_id != decision.owner_id:
        return False, "owner_id_mismatch"
    if int(owner.generation) != int(decision.owner_generation):
        return False, "owner_generation_mismatch"
    return True, "ok"


def authenticate_decision(
    db: Any,
    *,
    run_id: str,
    job: Job,
    decision: PreemptionDecision,
    expected_preemptions_before: int,
) -> tuple[bool, str]:
    """Authenticate a ``preemption_preparing`` receipt against durable state.

    A receipt is authentic only when it is bound to

    * the exact job/run and the frozen job identity (repo + base SHA);
    * a non-empty decision reference (the real terminal routing decision);
    * the pre-preemption count for this generation;
    * the exact durable source-attempt snapshot that was terminal when the
      decision was taken; and
    * the exact durable execution-owner generation.

    All of it must hold, or the caller must fail closed: a plausible receipt is
    not proof that D8 took this job's terminal outcome.
    """
    if decision.run_id != run_id or decision.job_id != job.job_id:
        return False, "decision_mismatched"
    if decision.version < DECISION_VERSION:
        return False, "decision_version"
    if not decision.decision_ref:
        return False, "decision_ref_missing"
    if str(decision.repo or "") != str(job.repo or ""):
        return False, "decision_repo_mismatch"
    if str(decision.base_sha or "") != str(job.base_sha or ""):
        return False, "decision_base_mismatch"
    if int(decision.preemptions_before) != int(expected_preemptions_before):
        return False, "decision_count"
    ids, states = _attempt_snapshot(db, run_id)
    # The snapshot must still be exactly the durable state the decision was
    # taken over (a zero-launch decision legitimately carries an empty
    # snapshot; a run that gained/lost/changed an attempt is ambiguous).
    if tuple(decision.source_attempt_ids) != ids:
        return False, "decision_source_attempts_changed"
    if tuple(decision.source_attempt_states) != states:
        return False, "decision_source_states_changed"
    return _decision_owner_binding(db, run_id, decision)


def _intent_matches_decision(
    intent: Mapping[str, Any], decision: PreemptionDecision
) -> tuple[bool, str]:
    """True iff the prepared intent belongs to the same authenticated transaction."""
    try:
        intent_before = int(intent.get("preemptions_before", -1))
    except (TypeError, ValueError):
        return False, "intent_count_malformed"
    if intent_before != int(decision.preemptions_before):
        return False, "intent_count_mismatch"
    owner_raw = intent.get("owner_id")
    gen_raw = intent.get("owner_generation")
    intent_owner = str(owner_raw) if isinstance(owner_raw, str) and owner_raw else None
    intent_gen = int(gen_raw) if isinstance(gen_raw, int) else None
    if intent_owner != decision.owner_id or intent_gen != decision.owner_generation:
        return False, "intent_owner_mismatch"
    return True, "ok"


@dataclass(frozen=True)
class _AuthenticatedTransaction:
    ok: bool
    detail: str
    decision: PreemptionDecision | None = None
    artifacts: PreparedArtifacts | None = None


def authenticate_prepared_transaction(
    db: Any,
    *,
    run_id: str,
    job: Job,
    repo_path: str | Path,
    expected_preemptions_before: int | None = None,
) -> _AuthenticatedTransaction:
    """Authenticate a durable D8 transaction end to end (read-only).

    Requires **both** an authentic ``preemption_preparing`` decision receipt
    and, when a ``preemption_committed`` intent exists, artifacts that verify
    and belong to that same authenticated transaction.  A legacy/unbound intent
    (no decision, or a decision that cannot be authenticated) is rejected here:
    a valid SHA + capsule is not proof that D8 took this job's terminal
    outcome, so it must never be promoted to authority.
    """
    repo = Path(repo_path).resolve() if not isinstance(repo_path, Path) else repo_path
    before = (
        int(job.preemptions)
        if expected_preemptions_before is None
        else int(expected_preemptions_before)
    )
    raw = _decision_payload(db, run_id, job.job_id)
    intent = _intent_payload(db, run_id, job.job_id)
    if raw is None:
        if intent is not None:
            return _AuthenticatedTransaction(
                ok=False, detail="legacy_intent_without_decision"
            )
        return _AuthenticatedTransaction(ok=False, detail="decision_missing")
    decision = PreemptionDecision.from_payload(raw)
    if decision is None:
        return _AuthenticatedTransaction(ok=False, detail="decision_malformed")
    ok, detail = authenticate_decision(
        db, run_id=run_id, job=job, decision=decision,
        expected_preemptions_before=before,
    )
    if not ok:
        return _AuthenticatedTransaction(ok=False, detail=f"decision_unauthenticated:{detail}",
                                         decision=decision)
    if intent is None:
        return _AuthenticatedTransaction(ok=True, detail="decision_authenticated",
                                         decision=decision)
    artifacts, adetail = verify_prepared_artifacts(
        db, run_id=run_id, job=job, repo_path=repo, payload=intent,
        expected_preemptions_before=before,
    )
    if artifacts is None:
        return _AuthenticatedTransaction(
            ok=False, detail=f"intent_unverified:{adetail}", decision=decision,
        )
    mok, mdetail = _intent_matches_decision(intent, decision)
    if not mok:
        return _AuthenticatedTransaction(ok=False, detail=mdetail, decision=decision)
    return _AuthenticatedTransaction(
        ok=True, detail="transaction_authenticated", decision=decision, artifacts=artifacts,
    )


def classify_preemption_transaction(
    db: Any,
    *,
    run_id: str,
    job: Job,
    repo_path: str | Path,
) -> PreemptionTransaction:
    """Classify one run/job's durable D8 transaction state (read-only).

    Never infers a D8 decision from quota history or WIP alone, and never
    promotes an unauthenticated legacy receipt: a transaction is ``prepared``
    only when an authentic bound decision **and** matching verified artifacts
    exist, ``preparing`` only when an authentic bound decision exists, and
    otherwise ``blocked`` (fail closed) or ``none``.
    """
    txn = authenticate_prepared_transaction(db, run_id=run_id, job=job, repo_path=repo_path)
    if txn.ok:
        if txn.artifacts is not None:
            return PreemptionTransaction(
                state="prepared", detail=txn.detail, artifacts=txn.artifacts,
                decision=txn.decision, job=job,
            )
        return PreemptionTransaction(
            state="preparing", detail=txn.detail, decision=txn.decision, job=job,
        )
    if txn.detail == "decision_missing":
        # No decision receipt and no intent: an ordinary run with no D8
        # transaction at all (not a promoted legacy intent).
        return PreemptionTransaction(state="none", detail="no_transaction", job=job)
    return PreemptionTransaction(
        state="blocked", detail=txn.detail, decision=txn.decision, job=job,
    )


def finish_interrupted_preemption(
    db: Any,
    *,
    run_id: str,
    job: Job,
    repo_path: str | Path,
    owner_id: str | None = None,
    owner_generation: int | None = None,
    now: datetime | None = None,
    max_preemptions: int = DEFAULT_MAX_PREEMPTIONS,
) -> PreemptionOutcome:
    """Continue an already-durable D8 transaction through production recovery.

    This is the narrow entry point used by the scheduler/supervisor restart
    path.  It performs **no** new routing decision, no provider/model call and
    no admission: it only re-verifies durable evidence (genuine quota, F1
    no-eligible, exact RUNNING job/run, verification of the recorded artifacts)
    and then finishes the fenced terminalization, exact-generation requeue and
    settlement.  A caller that has not itself proven the predecessor dead and
    the worker quiescent must not call this.
    """
    return complete_preemption(
        db,
        run_id=run_id,
        repo_path=repo_path,
        job_id=job.job_id,
        base_sha=job.base_sha,
        owner_id=owner_id,
        owner_generation=owner_generation,
        recovering=True,
        max_preemptions=max_preemptions,
        now=now,
    )



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
    """True iff the run's terminal outcome is quota-only exhaustion.

    Deliberately conservative ("fail closed"): D8 may preempt only when the
    run ended with genuine quota exhaustion and **no admitted fallback**.  The
    durable attempt and event channels carry no chain, tier, or generation
    identity, so a non-quota failure can never be proven to belong to an older
    chain than the quota signal.  Whenever the run admitted a candidate that
    then failed for any non-quota reason -- a deterministic gate failure, a
    model error, a timeout, an abort, a review failure -- the run failed on
    its merits and quota-only exhaustion is unproven, even if a
    ``no_eligible_candidate`` event is also present (that event is written for
    mixed chains that both launched and skipped candidates).  A candidate that
    *succeeded* (for example an earlier plan package) is not such a failure and
    does not by itself contradict a later, genuinely quota-only chain.

    Only these durable shapes, all produced by the run's own M2-4b ladder
    (never re-derived here by re-running admission), qualify:

    1. a ``no_eligible_candidate`` event alongside no admitted non-quota
       failure (the multi-candidate ladder shape);
    2. every launched attempt ended with a QUOTA_EXHAUSTED interruption (the
       single-candidate shape, where the ladder breaks on exhaustion before a
       skip is ever recorded); or
    3. no launched attempt at all alongside the recorded event (the zero-launch
       shape, where every candidate was skipped before dispatch).

    An unreadable attempt channel fails closed (declines).  Ordinary gate
    failures, model errors, aborts, and review failures leave at least one
    non-quota failed attempt behind and never qualify.
    """
    try:
        attempts = db.get_attempts_for_run(run_id)
    except Exception:
        return False
    for attempt in attempts:
        if getattr(attempt, "interruption_reason", None) == _QUOTA_EXHAUSTED:
            continue
        if getattr(attempt, "status", None) == AttemptStatus.SUCCESS:
            # An older admitted success (e.g. a previous plan package) is
            # compatible with a later quota-only chain; it is not a failure.
            continue
        # An admitted, non-quota, non-successful attempt: the run's outcome
        # was decided on its merits, so quota-only exhaustion is unproven.
        return False
    if _event_payloads(db, run_id, "no_eligible_candidate"):
        return True
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


def _git_common_dir(path: Path) -> Path | None:
    """Canonical common Git directory for *path*, or ``None`` when unprovable.

    ``git rev-parse --git-common-dir`` is authoritative for every spelling of
    one repository: the main checkout and each linked worktree all resolve to
    the same common directory, while a different repository -- even one that
    shares commit object SHAs through clone/object sharing -- resolves to a
    different one.  Relative Git output is rebased on *path* before
    canonicalization, matching the existing engine convention.
    """
    res = run_process(["git", "rev-parse", "--git-common-dir"], cwd=path)
    if not res.passed or not res.stdout.strip():
        return None
    common = Path(res.stdout.strip())
    if not common.is_absolute():
        common = path / common
    try:
        return common.resolve()
    except OSError:
        return None


def _git_worktree_root(path: Path) -> Path | None:
    """Canonical top-level worktree root containing *path*, or ``None``."""
    res = run_process(["git", "rev-parse", "--show-toplevel"], cwd=path)
    if not res.passed or not res.stdout.strip():
        return None
    try:
        return Path(res.stdout.strip()).resolve()
    except OSError:
        return None


def _registered_worktree_roots(repo_path: Path) -> set[Path] | None:
    """Canonical roots registered for *repo_path*, or ``None`` when unknown.

    ``git worktree list --porcelain`` is the repository's own registration
    record.  A ``None`` result is an unknown/ambiguous ownership answer and
    callers must fail closed; an empty set cannot describe a real repository
    (its main worktree is always registered) and is likewise treated as
    unknown.
    """
    res = run_process(["git", "worktree", "list", "--porcelain"], cwd=repo_path)
    if not res.passed:
        return None
    roots: set[Path] = set()
    for line in res.stdout.splitlines():
        if not line.startswith("worktree "):
            continue
        raw = line[len("worktree ") :].strip()
        if not raw:
            return None
        try:
            roots.add(Path(raw).resolve())
        except OSError:
            return None
    if not roots:
        return None
    return roots


def _verified_worktree_root(repo_path: Path, candidate: str | Path) -> Path | None:
    """Prove *candidate* is the attempt worktree root of the job repository.

    Ownership is established positively from Git's own metadata *before* any
    inspection or mutation, so a valid-but-unrelated repository -- or a stale,
    redirected, or aliased path -- can never be staged, committed, or elected
    as HEAD:

    * the candidate resolves to an existing directory and is itself the
      worktree root (``git rev-parse --show-toplevel``), so a subdirectory or
      nested checkout never qualifies;
    * the candidate's canonical common Git directory equals the job
      repository's canonical common Git directory, so a foreign repository --
      including one that shares object SHAs -- never qualifies;
    * the candidate root is registered for the job repository by
      ``git worktree list --porcelain``.

    Unknown, malformed, or inaccessible metadata returns ``None`` (fail
    closed) rather than a guess.  This is an identity proof over Git's recorded
    ownership, not an OS sandbox: a path replaced between this proof and the
    later mutation is re-proved immediately before the mutating call, and the
    residual race is reported honestly rather than claimed impossible.
    """
    try:
        resolved = Path(candidate).resolve()
    except OSError:
        return None
    if not resolved.is_dir():
        return None
    repo_common = _git_common_dir(repo_path)
    if repo_common is None:
        return None
    candidate_common = _git_common_dir(resolved)
    if candidate_common is None or candidate_common != repo_common:
        return None
    if _git_worktree_root(resolved) != resolved:
        return None
    registered = _registered_worktree_roots(repo_path)
    if registered is None or resolved not in registered:
        return None
    return resolved


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

    The recorded worker checkpoint is only a *starting* candidate, never an
    unconditional winner: an older recorded row (for example the checkpoint a
    previous generation staged through :func:`prepare_resume`) must not shadow
    newer verified work committed by the current attempt.  Resolution first
    proves the current attempt's worktree genuinely belongs to the job
    repository (exact common Git directory, exact registered worktree root) --
    never inspecting, staging, or committing into any other repository -- and
    then proves lineage relative to the recorded anchor before preferring
    either side:

    * a dirty worktree is snapshotted as a fresh checkpoint commit and used
      when it extends the trusted recorded anchor (an independently correct
      capture of the latest attempt state);
    * a clean worktree whose HEAD is the recorded anchor (or descends from it)
      resolves to that newer commit; a clean worktree whose HEAD is an
      *ancestor* of the recorded anchor resolves to the recorded anchor;
    * a worktree HEAD that is neither ancestor nor descendant of the recorded
      anchor is ambiguous -- untrusted, unrelated lineage -- so resolution
      fails closed instead of discarding or fabricating the resume point;
    * with no recorded anchor, the current behaviour is kept: dirty snapshot,
      else the exact existing worktree HEAD, else the job's frozen base SHA.

    Every returned candidate is a verified commit object in ``repo_path``;
    object existence alone is never treated as lineage.  A recorded attempt
    that points at a worktree this process cannot prove belongs to
    ``repo_path`` fails closed rather than silently anchoring the frozen base,
    and its repository, index, HEAD, and refs are left untouched.
    """
    recorded = latest_checkpoint_sha(db, run_id)
    if recorded is not None:
        try:
            if not GitManager.commit_exists(repo_path, recorded):
                recorded = None
        except Exception:
            return None

    hint = worktree_path or _attempt_worktree_path(db, run_id)
    work: Path | None = None
    if hint:
        try:
            # Ownership is proven BEFORE any inspection, index write, or
            # commit: a valid-but-unrelated repository, a stale/repointed
            # path, a subdirectory, or an alias is never trusted.
            work = _verified_worktree_root(repo_path, hint)
        except Exception:
            return None

    if work is not None:
        try:
            changed = GitManager.get_changed_paths(work)
            if changed:
                # Re-prove ownership across the inspection boundary so a path
                # swapped between the read above and the mutation below cannot
                # redirect the commit into another repository.
                if _verified_worktree_root(repo_path, work) is None:
                    return None
                sha = GitManager.create_checkpoint_commit(
                    work,
                    f"orch checkpoint({run_id}/preempt): "
                    "partial worker state (QUOTA_EXHAUSTED)",
                )
                if sha is None:
                    return None
                try:
                    validate_candidate_sha(sha)
                except Exception:
                    return None
                # Defense in depth: the snapshot must be a real commit object
                # in the destination repository, never a SHA from an unrelated
                # object store.
                if not GitManager.commit_exists(repo_path, sha):
                    return None
                if recorded is not None and not GitManager.is_ancestor(
                    repo_path, recorded, sha
                ):
                    # The snapshot diverges from the trusted anchor: refuse
                    # to pretend either lineage.
                    return None
                return sha
            head = GitManager.get_ref_commit(work, "HEAD")
            if head is not None:
                try:
                    validate_candidate_sha(head)
                except Exception:
                    return None
                if recorded is not None:
                    if head == recorded:
                        return recorded
                    if GitManager.is_ancestor(repo_path, recorded, head):
                        # Verified newer work committed on top of the anchor.
                        return head
                    if GitManager.is_ancestor(repo_path, head, recorded):
                        # The recorded anchor already covers this HEAD.
                        return recorded
                    return None
                if GitManager.commit_exists(repo_path, head):
                    return head
                return None
        except Exception:
            return None
        # A usable worktree with no HEAD and nothing dirty carries no newer
        # work; the recorded anchor (if any) is the only honest candidate.
        return recorded

    if hint:
        # An attempt recorded a worktree that cannot be positively verified as
        # this job's own: ownership is ambiguous, there may be unprovable WIP
        # behind the hint, and the hinted repository must stay untouched.  Fail
        # closed rather than falling back to a stale checkpoint, and never
        # anchor the frozen base from ambiguity.
        return None
    if recorded is not None:
        return recorded
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
    db: Any,
    *,
    run_id: str,
    summary: str,
    now_iso: str,
    owner_id: str | None = None,
    owner_generation: int | None = None,
    require_fenced: bool = False,
) -> tuple[bool, bool, str]:
    """Move RUNNING -> PREEMPTED exactly once.

    Returns ``(ok, transitioned, detail)``: ``ok`` is True when the run is
    (now) PREEMPTED; ``transitioned`` is True only when this call performed the
    transition, so replay callers can skip re-emitting terminal events.

    When an exact execution-owner token is supplied the transition uses
    :meth:`Database.terminalize_preemption_fenced`, which additionally refuses
    when any attempt is still RUNNING/PENDING (a live worker is not a
    recoverable D8 crash) or when the generation changed.  Without a token the
    existing guarded read-check + update path is kept, so standalone/in-process
    runs behave exactly as before -- except that ``require_fenced`` callers
    (every crash-recovery pass) must supply a token and never take the
    unfenced branch.
    """
    try:
        run = db.get_run(run_id)
    except Exception:
        return (False, False, "run_lookup_failed")
    if run is None:
        return (False, False, "no_run")
    if run.status == RunStatus.PREEMPTED:
        return (True, False, "already_preempted")
    if run.status in TERMINAL_RUN_STATUSES:
        return (False, False, "run_terminal_other")
    if run.status != RunStatus.RUNNING:
        return (False, False, "run_not_running")
    if owner_id is not None and owner_generation is not None:
        try:
            receipt = db.terminalize_preemption_fenced(
                run_id,
                owner_id=owner_id,
                generation=owner_generation,
                result_summary=summary,
                expected_owner_states=(ExecutionOwner.CLAIMED,),
                completed_at=now_iso,
            )
        except Exception:
            return (False, False, "fenced_terminalize_failed")
        if receipt is None:
            return (False, False, "fenced_terminalize_refused")
        return (True, True, "transitioned")
    if require_fenced:
        # A crash-recovery pass must never fall back to the unfenced writer:
        # without a proven token there is no authority to terminalize.
        return (False, False, "fencing_required")
    try:
        db.update_run(
            run_id=run_id,
            status=RunStatus.PREEMPTED,
            completed_at=now_iso,
            result_summary=summary,
        )
    except Exception:
        return (False, False, "update_failed")
    return (True, True, "transitioned")


def _fail_at_limit(
    db: Any,
    *,
    run_id: str,
    job: Job,
    max_preemptions: int,
    now: datetime,
    now_iso: str,
    owner_id: str | None = None,
    owner_generation: int | None = None,
) -> PreemptionOutcome:
    """Record terminal FAILED ``PREEMPTION_LIMIT`` on the run and the job.

    When an exact execution-owner token is supplied the run transition is the
    fenced :meth:`Database.terminalize_run_fenced` (exact ``CLAIMED``
    generation), so a newer generation that claimed the run while this decision
    was being made is never FAILED or released by a stale caller.  A refusing
    fence means *no authority*: nothing is rewritten and no reservation is
    released.  Without a token the pre-existing in-process path is kept.
    """
    from saberops.capacity.ledger import release_terminal_debt
    from saberops.jobs import store

    summary = (
        f"Job {job.job_id} reached the preemption bound "
        f"({job.preemptions}/{max_preemptions}): PREEMPTION_LIMIT. "
        "Quota exhaustion recurred with no admitted fallback route; "
        "not requeueing again."
    )
    fenced = owner_id is not None and owner_generation is not None
    run_failed = False
    try:
        run = db.get_run(run_id)
        if run is not None and run.status == RunStatus.RUNNING:
            if fenced:
                receipt = db.terminalize_run_fenced(
                    run_id,
                    owner_id=owner_id,
                    generation=owner_generation,
                    status=RunStatus.FAILED,
                    result_summary=summary,
                    attempt_reason=f"preemption limit: {summary}",
                    expected_owner_states=(ExecutionOwner.CLAIMED,),
                    release_reason="preemption_limit",
                    completed_at=now_iso,
                )
                if receipt is None:
                    return PreemptionOutcome(
                        owned=False, detail="limit_fence_refused", job=job,
                    )
            else:
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
            run_failed = True
    except Exception:
        if fenced:
            return PreemptionOutcome(owned=False, detail="limit_terminalize_failed", job=job)
    if fenced and not run_failed:
        return PreemptionOutcome(owned=False, detail="limit_fence_refused", job=job)
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
        release_terminal_debt(db, run_id=run_id, job_id=job.job_id, now=now)
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
    owner_id: str | None = None,
    owner_generation: int | None = None,
    decision_ref: str | None = None,
    recovering: bool = False,
    max_preemptions: int = DEFAULT_MAX_PREEMPTIONS,
    now: datetime | None = None,
) -> PreemptionOutcome:
    """Execute (or idempotently complete) the D8 preemption protocol.

    Ordered for crash safety: durable decision receipt ->
    checkpoint/anchor -> resume capsule -> anchor ref -> verified intent ->
    fenced run terminalization -> exact-generation job requeue -> owner-aware
    settlement of the old run's capacity.  A crash before terminalization
    leaves an exact, recognisable D8 transaction (verified artifacts, or the
    decision receipt) that the scheduler/supervisor restart path finishes;
    a crash after terminalization is completed idempotently.  Never raises for
    expected failure modes: declines with ``owned=False`` instead, so the
    caller keeps its truthful behaviour.

    ``owner_id``/``owner_generation`` are supplied by a recovery caller that
    has itself proven the predecessor generation dead and the worker
    quiescent; when present the run transition is fenced to that exact
    generation and refuses to force-close a live attempt.
    """
    from saberops.capacity.ledger import release_terminal_debt
    from saberops.jobs import store

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
            owner_id=owner_id, owner_generation=owner_generation,
        )

    not_before = quota_reset_eta(db, run_id)
    preemptions_before = int(job.preemptions)

    # Crash-replay: an existing prepared intent is reused ONLY when the whole
    # transaction authenticates (authentic bound decision + matching verified
    # artifacts).  A legacy/unbound intent is fail-closed: never fabricate a
    # different transaction from it, and never terminalize/requeue/release on
    # it -- retain WIP, job and charged capacity and surface the owner question.
    checkpoint_sha: str | None = None
    checkpoint_ref: str | None = None
    handoff_id: str | None = None
    intent = _intent_payload(db, run_id, job.job_id)
    if intent is not None:
        txn = authenticate_prepared_transaction(
            db, run_id=run_id, job=job, repo_path=repo,
            expected_preemptions_before=preemptions_before,
        )
        if not txn.ok or txn.artifacts is None:
            return PreemptionOutcome(
                owned=False, detail=f"intent_not_authenticated:{txn.detail}", job=job
            )
        checkpoint_sha = txn.artifacts.checkpoint_sha
        checkpoint_ref = txn.artifacts.checkpoint_ref
        handoff_id = txn.artifacts.handoff_id

    if checkpoint_sha is None:
        # A durable decision receipt already on record must itself authenticate
        # before this (fresh or recovering) pass continues from it.
        existing = authenticate_prepared_transaction(
            db, run_id=run_id, job=job, repo_path=repo,
            expected_preemptions_before=preemptions_before,
        )
        if not existing.ok:
            if existing.detail != "decision_missing":
                return PreemptionOutcome(
                    owned=False, detail=f"decision_not_authenticated:{existing.detail}",
                    job=job,
                )
            if recovering:
                # Recovery may only *continue* an already-durable transaction,
                # never start one from quota history or WIP alone.
                return PreemptionOutcome(
                    owned=False, detail="recovery_without_decision", job=job
                )
        # R2: record the factual D8 decision BEFORE any side effect so a crash
        # at (or between) the checkpoint/capsule/ref/intent boundaries leaves
        # an exact, recognisable transaction for restart recovery.
        decision = build_preemption_decision(
            db, run_id=run_id, job=job,
            owner_id=owner_id, owner_generation=owner_generation,
            decision_ref=decision_ref,
        )
        if not record_preparation_decision(db, decision):
            # The receipt could not be made durable: do not perform any
            # preemption side effect that recovery would then depend on.
            if _decision_payload(db, run_id, job.job_id) is None:
                return PreemptionOutcome(
                    owned=False, detail="decision_not_durable", job=job
                )

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
                INTENT_EVENT,
                payload={
                    "version": INTENT_VERSION,
                    "job_id": job.job_id,
                    "checkpoint_sha": checkpoint_sha,
                    "checkpoint_ref": checkpoint_ref,
                    "handoff_id": handoff_id,
                    "not_before": not_before,
                    "preemptions_before": preemptions_before,
                    "preemptions_next": preemptions_before + 1,
                    "owner_id": owner_id,
                    "owner_generation": owner_generation,
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
    terminal_ok, terminal_transitioned, terminal_detail = _terminalize_preempted(
        db, run_id=run_id, summary=summary, now_iso=now_iso,
        owner_id=owner_id, owner_generation=owner_generation,
        require_fenced=recovering,
    )
    if not terminal_ok:
        return PreemptionOutcome(owned=False, detail=terminal_detail, job=job)
    if terminal_transitioned:
        try:
            db.record_event(
                run_id,
                RUN_PREEMPTED_EVENT,
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
            preemptions=preemptions_before, not_before=not_before,
        )
    requeued: Job | None = None
    try:
        requeued = store.requeue_exact(
            db, job.job_id, WaitingReason.PREEMPTED,
            expected_run_id=run_id, expected_preemptions=preemptions_before,
            resume_from=resume_from, not_before=not_before, now=moment,
        )
    except Exception:
        requeued = None
    if requeued is None:
        # Lost the requeue race (cancel/concurrent dispatch/new generation):
        # the run is conclusively terminal, so still settle its reservation
        # through the same owner-aware path, but never touch a job we no
        # longer own.  If settlement fails here, the run is terminal and has
        # a D8 receipt, so the durable settlement scan completes it later.
        try:
            release_terminal_debt(db, run_id=run_id, job_id=job.job_id, now=moment)
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
            handoff_id=handoff_id, preemptions=preemptions_before,
            not_before=not_before,
        )
    # The run is terminal and the job is requeued; settle the old run's
    # reserved capacity exactly once.  A failure here is not fatal: the old
    # run's D8 receipt makes it discoverable by the durable settlement scan,
    # which is why R1 no longer depends on this call succeeding.
    try:
        release_terminal_debt(db, run_id=run_id, job_id=job.job_id, now=moment)
    except Exception:
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
    "DECISION_VERSION",
    "INTENT_EVENT",
    "INTENT_VERSION",
    "PREPARATION_EVENT",
    "RUN_PREEMPTED_EVENT",
    "PreemptionDecision",
    "PreemptionOutcome",
    "PreemptionReadiness",
    "PreemptionTransaction",
    "PreparedArtifacts",
    "ResumePreparation",
    "authenticate_decision",
    "authenticate_prepared_transaction",
    "build_preemption_decision",
    "classify_preemption_transaction",
    "complete_preemption",
    "find_job_for_run",
    "find_preempted_job_for_run",
    "finish_interrupted_preemption",
    "has_genuine_quota_signal",
    "has_no_eligible_evidence",
    "latest_checkpoint_sha",
    "prepare_resume",
    "quota_reset_eta",
    "record_preparation_decision",
    "should_preempt",
    "verify_prepared_artifacts",
]
