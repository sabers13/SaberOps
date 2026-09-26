"""R3-A: deterministic read-only CandidateState projection.

CandidateState is a PROJECTION over existing durable run evidence.  It is
not a second state machine and not new authority: no DB column, lifecycle
table, migration, shadow state file, write-on-read event, or state
mutation from the Web is introduced here.  The projection is
reconstructable from authoritative existing evidence on every read.

R3-C adds exactly one durable owner-decision write seam on top of this
read path: :func:`reject_current_candidate`, the single authority that
may record a ``candidate_rejected`` event.  The Web and the service call
that seam; they never write rejection events directly.  Every other
reader (the projection itself, the Reject action, the AcceptEngine
precondition) interprets rejection evidence through the single shared
:func:`find_matching_rejection_event` helper, so no second definition of
"rejected" exists.

Durable-evidence characterization (verified against the runtime at
``ae1bf49``; "which exact candidate SHA does this evidence describe?"
answered per source):

* Candidate commit identity -- ``attempts`` rows: ``commit_sha`` on the
  row plus ``status``.  CRITICAL: a later FAILED repair attempt can also
  carry a ``commit_sha`` (gate-failed repair commits persist the SHA on
  the FAILED row, ``orchestrator._run_core`` repair path), so "latest
  attempt commit_sha" is NOT the candidate.  Current-candidate identity
  is the latest ``SUCCESS`` attempt carrying a commit SHA -- the same
  rule :func:`saberops.accept._select_final_candidate` uses -- and a
  passed ``checks`` row bound to that attempt id is required as the
  deterministic gate-success evidence (``attempt_succeeded`` /
  ``gate_passed`` events corroborate but are not independently
  sufficient).
* Gate success -- ``checks`` rows (``attempt_id``, ``passed``).  A check
  row is bound to exactly one attempt, hence to exactly one candidate
  SHA via that attempt's ``commit_sha``.
* Review start -- ``review_started`` / ``review_dispatch_started`` events
  carry NO candidate SHA and are therefore never sufficient alone.
  Deterministic binding comes from ``dispatch_evidence`` rows with
  ``stage == REVIEW`` and ``candidate_sha`` set (the review engine
  stamps the reviewed candidate SHA on the dispatch row).  Production
  gap: the review dispatch row only carries ``candidate_sha`` once the
  dispatch completes today, so a live in-flight review may project as
  READY (fail closed) rather than IN_REVIEW until the row is bound.
* Review completion / verdict -- ``reviews`` rows (typed
  :class:`ReviewVerdict`) bound to a dispatch via
  ``review.id == dispatch.association_id`` (production convention: the
  review row id IS the dispatch association id) or
  ``dispatch.review_id == review.id``.  ``review_completed`` events
  corroborate.  Substantive verdicts -- PASS, BLOCK, PASS_WITH_BACKLOG,
  FINAL_BLOCK, CONVERGENCE_STALLED -- are reviewer judgments and may
  yield REVIEWED.  REVIEW_EXECUTION_FAILED (provider/transport/timeout/
  unusable output) and REVIEW_UNAVAILABLE (no reviewer dispatchable)
  are infrastructure outcomes, never reviewer judgments, and never
  yield REVIEWED.
* Review dispatch <-> candidate SHA association --
  ``dispatch_evidence.candidate_sha`` on ``stage == REVIEW`` rows, as
  above.  A review bound to candidate A never applies to a later
  candidate B: every review read is filtered by ``== current``.
* Risk-adaptive review skip -- the frozen ``runs.review_risk_decision_json``
  (``independent_review_required=False``) plus the corroborating
  ``review_skipped_by_risk_adaptive_policy`` event.  A skip stays READY;
  it is never REVIEWED.
* Acceptance -- the repository's single authoritative AcceptEngine
  record for the CURRENT candidate, verified by
  :func:`saberops.autonomous_runtime._verify_accept_engine_evidence`
  (latest-``run_accepted`` semantics: canonical candidate SHA, identity
  match, unambiguous provenance with matching
  ``provenance.candidate_sha``).  A matching ``candidate_sha`` alone is
  never sufficient.  Terminal for the projection.
* Frozen target branch / run base -- ``runs.base_commit`` (immutable per
  run; the acceptance contract compares the live target HEAD against
  it).  Current target HEAD is live repository state, never durable
  evidence: it is supplied by the caller through ``target_head_resolver``
  and only ever used for the STALE branch.  Without a resolver, STALE
  is unreachable (fail closed) rather than guessed.
* Cleanup evidence -- NONE.  ``cleanup_run`` removes worktrees without
  recording any event or durable row, so CLEANED is unreachable.
  Missing worktrees or filesystem state are never synthesized into
  CLEANED.
* Rejection evidence -- ``candidate_rejected`` events carrying a
  canonical ``candidate_sha`` payload (R3-C).  Only an event whose
  ``candidate_sha`` is a canonical 40-lowercase-hex SHA naming the
  EXACT current candidate counts; the event's ``run_id`` already binds
  it to the run.  Branch HEAD, latest arbitrary attempt SHA, review
  prose, filesystem state, and UI state are never rejection authority.
  Malformed events (missing/truncated/noncanonical SHA) or events
  naming another candidate never reject the current candidate.  An
  explicit terminal owner decision: REJECTED survives later
  target-HEAD movement, but authoritative acceptance still wins
  (ACCEPTED > REJECTED), because acceptance means the repository was
  actually integrated.

Precedence (highest first): ACCEPTED > REJECTED > STALE > REVIEWED >
IN_REVIEW > READY.  Rationale: terminal owner acceptance is never
overwritten by target-HEAD movement, and malformed historical data
containing both an acceptance and a rejection for the same candidate
still projects ACCEPTED rather than pretending the integration did not
happen; rejection is otherwise an explicit terminal owner decision that
survives later target-HEAD movement, so it outranks STALE; an obsolete
candidate is STALE even when an old review exists (accept would refuse
it under the unchanged target-HEAD/base comparison); review evidence is
always filtered to the current candidate SHA so stale/in-progress old
reviews cannot leak onto a newer candidate; a risk-adaptive skip cannot
fabricate REVIEWED; a failed/unavailable review is never presented as a
successful review.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from saberops.models import (
    AttemptStatus,
    CandidateState,
    DispatchStage,
    OrchEvent,
    ReviewVerdict,
)

if TYPE_CHECKING:
    from saberops.db import Database


#: Reviewer judgments.  Only these verdicts may yield REVIEWED.
#: REVIEW_EXECUTION_FAILED / REVIEW_UNAVAILABLE are infrastructure
#: outcomes (no reviewer judgment exists) and are deliberately excluded.
SUBSTANTIVE_REVIEW_VERDICTS: frozenset[ReviewVerdict] = frozenset(
    {
        ReviewVerdict.PASS,
        ReviewVerdict.BLOCK,
        ReviewVerdict.PASS_WITH_BACKLOG,
        ReviewVerdict.FINAL_BLOCK,
        ReviewVerdict.CONVERGENCE_STALLED,
    }
)

#: Callable resolving the live target HEAD for a run.  Receives
#: ``(run_id, frozen_base_commit)`` and returns the live HEAD SHA, or
#: ``None`` when the repository is unreadable.  Live state is only ever
#: consulted for the STALE branch; every other branch is purely durable.
TargetHeadResolver = Callable[[str, str], str | None]


#: Canonical durable event type for an explicit owner Reject decision
#: (R3-C).  Only :func:`reject_current_candidate` may write it.
REJECTION_EVENT_TYPE: str = "candidate_rejected"

#: Projection reason used when a matching rejection event proves REJECTED.
REJECTION_REASON: str = "owner_rejected_matching_candidate"

#: Strict canonical candidate SHA: 40 lowercase hex chars.  Mirrors the
#: acceptance verifier's canonical-SHA rule so rejection evidence can
#: never be weaker than acceptance evidence.
_CANONICAL_SHA_RE = re.compile(r"[0-9a-f]{40}")

#: Owner-visible states that may still receive a NEW Reject decision.
#: ACCEPTED is terminal integration (never rejectable); REJECTED already
#: carries the decision (a repeat resolves idempotently through the
#: service seam, never as a new decision); None is gate-unproven (fail
#: closed); CLEANED is untouched by this tranche.
REJECTABLE_STATES: frozenset[CandidateState] = frozenset(
    {
        CandidateState.READY,
        CandidateState.IN_REVIEW,
        CandidateState.REVIEWED,
        CandidateState.STALE,
    }
)


def is_canonical_candidate_sha(value: object) -> bool:
    """True iff ``value`` is a strict 40-char lowercase hex SHA-1 string.

    The single canonical-SHA authority shared by the rejection
    lifecycle and the Inbox classification: acceptance evidence,
    rejection evidence, and owner-visible candidate identity all use
    exactly this predicate, so no second regex/table/policy may exist
    elsewhere.
    """
    return isinstance(value, str) and _CANONICAL_SHA_RE.fullmatch(value) is not None


def find_matching_rejection_event(
    db: Database, run_id: str, candidate_sha: str
) -> OrchEvent | None:
    """Return the latest canonical rejection event for the EXACT candidate.

    This is the ONE shared seam interpreting rejection evidence.  The
    lifecycle projection, the Reject action, and the AcceptEngine
    precondition all consume it, so "rejected" has exactly one
    definition: a ``candidate_rejected`` event on this run whose payload
    carries a canonical ``candidate_sha`` equal to ``candidate_sha``.
    The event's ``run_id`` already binds it to the run.

    Malformed evidence (missing/truncated/noncanonical ``candidate_sha``,
    non-dict payload) and events naming another candidate return None --
    they never reject the current candidate.  A noncanonical
    ``candidate_sha`` argument itself also returns None (fail closed).
    """
    if not is_canonical_candidate_sha(candidate_sha):
        return None
    latest: OrchEvent | None = None
    for event in db.get_events_by_type(run_id, REJECTION_EVENT_TYPE):
        payload = event.payload
        if not isinstance(payload, dict):
            continue
        named = payload.get("candidate_sha")
        if not is_canonical_candidate_sha(named):
            continue
        if named != candidate_sha:
            continue
        if latest is None or event.id > latest.id:
            latest = event
    return latest


def can_reject_candidate(db: Database, run_id: str) -> bool:
    """Return True when the current candidate may receive a NEW decision.

    Derived from the same projection authority as the Reject service
    seam (never from ``RunStatus`` alone): the run must prove a current
    candidate SHA in an owner-visible, non-terminal state.  An already
    rejected candidate returns False here -- repeats resolve
    idempotently through :func:`reject_current_candidate`, never as a
    new decision.
    """
    projection = project_candidate_state(db, run_id)
    return (
        projection.candidate_sha is not None
        and is_canonical_candidate_sha(projection.candidate_sha)
        and projection.state in REJECTABLE_STATES
    )


def reject_current_candidate(db: Database, run_id: str) -> CandidateProjection:
    """Record a durable owner Reject decision for the exact current candidate.

    The single Reject authority.  The Web and the service call this seam;
    they never write ``candidate_rejected`` events directly.

    The decision succeeds only when the run exists, R3-A proves a current
    candidate SHA in an owner-visible state (therefore gate success is
    proven -- a SHA with ``state is None``, e.g.
    ``candidate_without_gate_success``, fails closed with no event
    written), and the current candidate is not already ACCEPTED.  No PASS
    review is required: READY, IN_REVIEW, REVIEWED (and STALE) candidates
    are all rejectable.

    The decision records exactly one canonical event --
    ``{"candidate_sha": "<full canonical candidate SHA>"}`` -- bound to
    the exact current SHA.  It does NOT modify the target Git branch, does
    NOT delete the candidate, and does NOT clean worktrees.  A repeat for
    the same exact candidate is idempotent: no duplicate event is written
    and the already-rejected projection is returned truthfully.  Rejecting
    candidate A never rejects a later gate-passed candidate B, because the
    projection only honors events naming the CURRENT candidate SHA.

    There is no "unreject" action: rejection is terminal for the
    candidate (a later candidate B remains independently rejectable).
    """
    run = db.get_run(run_id)
    if run is None:
        raise ValueError(f"Run '{run_id}' not found")
    projection = project_candidate_state(db, run_id)
    if projection.candidate_sha is None or projection.state is None:
        raise RuntimeError(
            f"Cannot reject run '{run_id}': no gate-proven current candidate "
            f"({projection.reason}); rejection requires an owner-visible state."
        )
    if projection.state is CandidateState.ACCEPTED:
        raise RuntimeError(
            f"Cannot reject run '{run_id}': candidate "
            f"{projection.candidate_sha[:8]} was already accepted."
        )
    candidate_sha = projection.candidate_sha
    if not is_canonical_candidate_sha(candidate_sha):
        raise RuntimeError(
            f"Cannot reject run '{run_id}': current candidate SHA is not "
            "a canonical 40-character lowercase hex SHA; "
            "rejection requires a canonical gate-proven candidate."
        )
    # Atomically idempotent durable append (R3-C.1): the check for an
    # existing matching decision and the insert happen inside one
    # BEGIN IMMEDIATE transaction at the SQLite boundary, so concurrent
    # writers on separate Database instances cannot both append.  A
    # repeat -- sequential or concurrent -- observes the durable row and
    # writes nothing.
    db.record_candidate_rejection_once(run_id, candidate_sha)
    return project_candidate_state(db, run_id)


@dataclass(frozen=True)
class CandidateProjection:
    """Read-only lifecycle projection for one run's current candidate."""

    run_id: str
    candidate_sha: str | None
    state: CandidateState | None
    verdict: ReviewVerdict | None
    reason: str
    evidence: tuple[str, ...] = ()
    review_required: bool | None = None


def _short(sha: str) -> str:
    return sha[:8] if len(sha) >= 8 else sha


def _resolve_review_required(db: Database, run_id: str) -> bool | None:
    """Return the frozen risk-adaptive answer, or None when unknown.

    Unknown covers legacy rows without a persisted decision and malformed
    persisted JSON (fail closed: the caller treats unknown as "no skip
    proven", never as "review not required").
    """
    from saberops.review_adaptive import ReviewDecision, ReviewPolicyError

    raw = db.get_review_risk_decision_json(run_id)
    if raw is None:
        return None
    try:
        return bool(ReviewDecision.from_json(raw).independent_review_required)
    except ReviewPolicyError:
        return None


def _is_substantive(verdict: ReviewVerdict) -> bool:
    return verdict in SUBSTANTIVE_REVIEW_VERDICTS


def project_candidate_state(
    db: Database,
    run_id: str,
    *,
    target_head_resolver: TargetHeadResolver | None = None,
) -> CandidateProjection:
    """Project the current candidate lifecycle state for ``run_id``.

    Pure read path: no writes, no mutations, no event recording.  Every
    branch is derived from durable evidence re-read on each call, so
    DB close/reopen with identical evidence yields an identical
    projection.
    """
    run = db.get_run(run_id)
    if run is None:
        return CandidateProjection(
            run_id=run_id,
            candidate_sha=None,
            state=None,
            verdict=None,
            reason="run_unknown",
            evidence=(),
            review_required=None,
        )

    # Current-candidate identity: latest SUCCESSFUL attempt carrying a
    # commit SHA.  A later FAILED repair attempt may carry its own
    # commit SHA (gate-failed repair commits persist it); that SHA is
    # NOT the owner-visible candidate and must never be selected here.
    # Mirrors AcceptEngine._select_final_candidate without importing
    # engine behavior into the read path.
    attempts = db.get_attempts_for_run(run_id)
    successful = [
        attempt
        for attempt in attempts
        if attempt.status == AttemptStatus.SUCCESS and attempt.commit_sha
    ]
    if not successful:
        return CandidateProjection(
            run_id=run_id,
            candidate_sha=None,
            state=None,
            verdict=None,
            reason="no_trustworthy_candidate",
            evidence=(f"attempts:{len(attempts)}",),
            review_required=_resolve_review_required(db, run_id),
        )
    current = successful[-1]
    candidate_sha = str(current.commit_sha)
    candidate_ref = f"attempt:{current.id}"

    # Deterministic gate-success evidence: a passed check row bound to
    # the current candidate's attempt.  SUCCESS-without-passed-check
    # fails closed (never READY on status alone).
    checks = db.get_checks_for_run(run_id)
    gate_checks = [
        check.id for check in checks if check.attempt_id == current.id and check.passed
    ]
    if not gate_checks:
        return CandidateProjection(
            run_id=run_id,
            candidate_sha=candidate_sha,
            state=None,
            verdict=None,
            reason="candidate_without_gate_success",
            evidence=(candidate_ref,),
            review_required=_resolve_review_required(db, run_id),
        )
    gate_refs = tuple(f"check:{check_id}" for check_id in gate_checks)

    # ACCEPTED: the single authoritative AcceptEngine record for the
    # CURRENT candidate.  Reuses
    # saberops.autonomous_runtime._verify_accept_engine_evidence directly
    # -- no second definition of acceptance lives here.  The verifier
    # enforces the latest-run_accepted semantics plus canonical SHA,
    # identity match, and unambiguous provenance (with matching
    # provenance.candidate_sha).  Evidence the verifier rejects fails
    # closed into the remaining trusted lifecycle evidence below;
    # acceptance is never manufactured.  Terminal: returned before any
    # REJECTED/STALE logic so later target-HEAD movement can never
    # overwrite it, and malformed history containing both an acceptance
    # and a rejection still projects ACCEPTED.
    accepted_ref: str | None = None
    try:
        from saberops.autonomous_runtime import _verify_accept_engine_evidence

        _verify_accept_engine_evidence(db, run_id=run_id, final_sha=candidate_sha)
    except Exception:
        accepted_ref = None
    else:
        latest_id: int | None = None
        for event in db.get_events_by_type(run_id, "run_accepted"):
            if latest_id is None or event.id > latest_id:
                latest_id = event.id
        if latest_id is not None:
            accepted_ref = f"event:run_accepted#{latest_id}"
    if accepted_ref is not None:
        return CandidateProjection(
            run_id=run_id,
            candidate_sha=candidate_sha,
            state=CandidateState.ACCEPTED,
            verdict=None,
            reason="owner_accepted_matching_candidate",
            evidence=(candidate_ref, *gate_refs, accepted_ref),
            review_required=_resolve_review_required(db, run_id),
        )

    # REJECTED: one canonical owner Reject decision for the CURRENT
    # candidate, interpreted through the single shared seam.  Placed
    # after ACCEPTED (authoritative integration wins over malformed
    # history containing both) and before STALE (an explicit terminal
    # owner decision survives later target-HEAD movement).  Review
    # evidence below is therefore unreachable for a rejected candidate,
    # but it stays durable -- rejection wins precedence, it never
    # deletes the review.  Never synthesized from run status, failed
    # review, BLOCK verdict, missing worktree, failed gate, or absent
    # acceptance: only a matching canonical event counts.
    rejection = find_matching_rejection_event(db, run_id, candidate_sha)
    if rejection is not None:
        return CandidateProjection(
            run_id=run_id,
            candidate_sha=candidate_sha,
            state=CandidateState.REJECTED,
            verdict=None,
            reason=REJECTION_REASON,
            evidence=(
                candidate_ref,
                *gate_refs,
                f"event:{REJECTION_EVENT_TYPE}#{rejection.id}",
            ),
            review_required=_resolve_review_required(db, run_id),
        )

    # STALE: reuse the existing acceptance contract verbatim -- the
    # candidate base (runs.base_commit) no longer equals the live
    # target HEAD.  Only reachable with a caller-supplied resolver;
    # without live HEAD evidence the branch is skipped (fail closed),
    # never guessed.  R4 owns rebasing/recovery; this only projects.
    if target_head_resolver is not None:
        try:
            live_head = target_head_resolver(run_id, run.base_commit)
        except Exception:
            live_head = None
        if live_head is not None and live_head != run.base_commit:
            return CandidateProjection(
                run_id=run_id,
                candidate_sha=candidate_sha,
                state=CandidateState.STALE,
                verdict=None,
                reason="target_head_moved",
                evidence=(
                    candidate_ref,
                    *gate_refs,
                    f"base:{_short(run.base_commit)}",
                    f"head:{_short(live_head)}",
                ),
                review_required=_resolve_review_required(db, run_id),
            )

    # Review evidence bound to the CURRENT candidate only.  Dispatch
    # rows for older candidates (different candidate_sha) are ignored,
    # as are non-REVIEW stages.
    dispatches = [
        dispatch
        for dispatch in db.get_dispatch_evidence_for_run(run_id)
        if dispatch.stage == DispatchStage.REVIEW
        and dispatch.candidate_sha == candidate_sha
    ]
    bound_associations = {dispatch.association_id for dispatch in dispatches}
    bound_review_ids = {dispatch.review_id for dispatch in dispatches if dispatch.review_id}
    reviews = db.get_reviews_for_run(run_id)
    bound_reviews = [
        review
        for review in reviews
        if review.id in bound_associations or review.id in bound_review_ids
    ]
    substantive = [review for review in bound_reviews if _is_substantive(review.verdict)]
    if substantive:
        latest = substantive[-1]
        return CandidateProjection(
            run_id=run_id,
            candidate_sha=candidate_sha,
            state=CandidateState.REVIEWED,
            verdict=latest.verdict,
            reason="substantive_review_completed",
            evidence=(
                candidate_ref,
                *gate_refs,
                f"review:{latest.id}",
                f"verdict:{latest.verdict.value}",
            ),
            review_required=_resolve_review_required(db, run_id),
        )
    # A bound dispatch whose review already concluded -- even
    # inconclusively (execution failure / unavailable) -- is settled,
    # not in flight.  Only dispatches with no completed review at all
    # for their association count as IN_REVIEW.
    completed_ids = {review.id for review in bound_reviews}
    in_flight = sorted(
        dispatch.association_id
        for dispatch in dispatches
        if dispatch.association_id not in completed_ids
        and (dispatch.review_id is None or dispatch.review_id not in completed_ids)
    )
    if in_flight:
        return CandidateProjection(
            run_id=run_id,
            candidate_sha=candidate_sha,
            state=CandidateState.IN_REVIEW,
            verdict=None,
            reason="review_dispatched_no_substantive_completion",
            evidence=(
                candidate_ref,
                *gate_refs,
                *(f"review_dispatch:{assoc}" for assoc in in_flight),
            ),
            review_required=_resolve_review_required(db, run_id),
        )

    # READY: gate-passed current candidate with no bound substantive or
    # in-progress review.  A risk-adaptive skip stays READY ("review
    # skipped" is never REVIEWED); a failed/unavailable review likewise
    # leaves the candidate READY rather than fabricating REVIEWED.
    review_required = _resolve_review_required(db, run_id)
    reason = "review_not_required" if review_required is False else "awaiting_review"
    return CandidateProjection(
        run_id=run_id,
        candidate_sha=candidate_sha,
        state=CandidateState.READY,
        verdict=None,
        reason=reason,
        evidence=(candidate_ref, *gate_refs),
        review_required=review_required,
    )
