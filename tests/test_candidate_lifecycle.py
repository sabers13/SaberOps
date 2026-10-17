"""R3-A: deterministic read-only CandidateState projection regressions.

Builds every scenario with real Database APIs and the real
``project_candidate_state`` seam -- never mocks of the projector
itself.  Covers the required regressions A-L plus the precedence
bullets (failed-repair SHA hijack, failed/unavailable review, terminal
verdict characterization).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from saberops.candidate_lifecycle import (
    SUBSTANTIVE_REVIEW_VERDICTS,
    project_candidate_state,
)
from saberops.db import Database
from saberops.models import (
    Attempt,
    AttemptStatus,
    CandidateState,
    CheckResult,
    DispatchEvidence,
    DispatchReason,
    DispatchRole,
    DispatchStage,
    ReviewResult,
    ReviewVerdict,
    RiskLevel,
    Run,
    RunStatus,
    Tier,
)
from saberops.review_adaptive import (
    ChangeClass,
    ChangeClassification,
    ReviewPolicyMode,
    decide_review,
)

_CREATED = "2026-09-24T10:00:00+00:00"


def _sha(tag: str) -> str:
    """Deterministic 40-hex candidate SHA for ``tag``."""
    return hashlib.sha1(tag.encode("utf-8")).hexdigest()


def _db(path: Path) -> Database:
    return Database(path / "orch.db")


def _seed_run(db: Database, run_id: str, base: str, repo: str = "/tmp/r3a-target") -> None:
    db.create_run(
        Run(
            id=run_id,
            task="r3a projection probe",
            target_repo=repo,
            base_commit=base,
            tier=Tier.T1,
            training_allowed=True,
            gate_command="true",
            status=RunStatus.COMPLETED,
            created_at=_CREATED,
            completed_at=_CREATED,
        )
    )


def _gate_pass(
    db: Database, run_id: str, attempt_id: str, num: int, sha: str, base: str
) -> None:
    """Record a gate-passed (SUCCESS + passed check) candidate attempt."""
    db.create_attempt(
        Attempt(
            id=attempt_id,
            run_id=run_id,
            attempt_number=num,
            provider="opencode",
            model="test-model",
            worktree_path="/tmp/r3a-wt",
            branch_name="r3a-candidate",
            status=AttemptStatus.SUCCESS,
            commit_sha=sha,
            created_at=_CREATED,
            base_sha=base,
        )
    )
    db.create_check(
        CheckResult(
            id=f"check-{attempt_id}",
            attempt_id=attempt_id,
            gate_command="true",
            exit_code=0,
            stdout="ok",
            stderr="",
            passed=True,
            created_at=_CREATED,
        )
    )


def _failed_attempt_with_sha(
    db: Database, run_id: str, attempt_id: str, num: int, sha: str, base: str
) -> None:
    """Record a FAILED attempt that still carries a commit SHA.

    Mirrors the production repair path: gate-failed repair commits
    persist their SHA on the FAILED row.
    """
    db.create_attempt(
        Attempt(
            id=attempt_id,
            run_id=run_id,
            attempt_number=num,
            provider="opencode",
            model="test-model",
            worktree_path="/tmp/r3a-wt",
            branch_name="r3a-candidate",
            status=AttemptStatus.FAILED,
            worker_error="Gate check failed",
            commit_sha=sha,
            created_at=_CREATED,
            completed_at=_CREATED,
            base_sha=base,
        )
    )
    db.create_check(
        CheckResult(
            id=f"check-{attempt_id}",
            attempt_id=attempt_id,
            gate_command="true",
            exit_code=1,
            stdout="",
            stderr="fail",
            passed=False,
            created_at=_CREATED,
        )
    )


def _review_dispatch(db: Database, run_id: str, assoc: str, sha: str | None) -> None:
    db.create_dispatch_evidence(
        DispatchEvidence(
            id=f"dispatch-{assoc}",
            run_id=run_id,
            association_id=assoc,
            stage=DispatchStage.REVIEW,
            role=DispatchRole.REVIEW,
            tier=Tier.T1,
            dispatch_reason=DispatchReason.INDEPENDENT_REVIEW,
            requested_provider="opencode",
            requested_model="reviewer",
            candidate_sha=sha,
            started_at=_CREATED,
        )
    )


def _review(db: Database, run_id: str, review_id: str, verdict: ReviewVerdict) -> None:
    db.create_review(
        ReviewResult(
            id=review_id,
            run_id=run_id,
            provider="opencode",
            model="reviewer",
            verdict=verdict,
            summary=f"{verdict.value} summary",
            details="details",
            created_at=_CREATED,
        )
    )


def _skip_decision_json(run_id: str) -> str:
    classification = ChangeClassification(
        change_class=ChangeClass.DOCS_ONLY,
        risk_level=RiskLevel.LOW,
        changed_paths=("README.md",),
        crosses_ownership_boundary=False,
        changes_hard_governance=False,
        changes_security_boundary=False,
        changes_acceptance_or_gate_authority=False,
        changes_publication_authority=False,
        changes_recovery_or_exactly_once=False,
        deterministic_coverage_available=True,
        signals=("docs_only",),
        reason="docs only",
    )
    decision = decide_review(
        classification=classification,
        review_policy_mode=ReviewPolicyMode.RISK_ADAPTIVE,
        owner_explicit_review=False,
        is_autonomous=False,
        decision_id=f"c13f-{run_id}",
    )
    assert decision.independent_review_required is False
    return decision.to_json()


def _accept(db: Database, run_id: str, sha: str | None) -> None:
    """Record valid authoritative AcceptEngine-shaped acceptance evidence.

    R3-A.1: the legacy weak helper (matching ``candidate_sha`` alone) is
    corrected to emit the canonical authoritative shape the existing
    AcceptEngine verifier requires: canonical ``candidate_sha`` plus
    unambiguous ``provenance`` with matching ``provenance.candidate_sha``.
    """
    payload: dict[str, object] = {"final_branch": "main", "gate_command": "true"}
    if sha is not None:
        payload["candidate_sha"] = sha
        payload["provenance"] = {
            "candidate_sha": sha,
            "run_id": run_id,
            "attempt_id": "probe",
            "attempt_number": 1,
        }
    db.record_event(run_id, "run_accepted", payload=payload)


def _accept_raw(db: Database, run_id: str, payload: dict[str, object]) -> None:
    """Record a ``run_accepted`` event with an exact caller-supplied payload."""
    db.record_event(run_id, "run_accepted", payload=payload)


def test_a_gate_passed_review_not_required_ready(tmp_path: Path) -> None:
    db = _db(tmp_path)
    base = _sha("base-a")
    candidate = _sha("candidate-a")
    _seed_run(db, "run-a", base)
    _gate_pass(db, "run-a", "run-a-a1", 1, candidate, base)
    db.update_review_risk_decision("run-a", _skip_decision_json("run-a"))
    before = len(db.get_events("run-a", limit=10000))
    proj = project_candidate_state(
        db, "run-a", target_head_resolver=lambda _rid, _b: base
    )
    after = len(db.get_events("run-a", limit=10000))
    assert proj.candidate_sha == candidate
    assert proj.state is CandidateState.READY
    assert proj.verdict is None
    assert proj.review_required is False
    assert proj.reason == "review_not_required"
    assert proj.evidence
    assert before == after  # read-only: no write-on-read


def test_b_review_dispatched_bound_to_current_in_review(tmp_path: Path) -> None:
    db = _db(tmp_path)
    base = _sha("base-b")
    candidate = _sha("candidate-b")
    _seed_run(db, "run-b", base)
    _gate_pass(db, "run-b", "run-b-a1", 1, candidate, base)
    db.record_event("run-b", "review_started", payload={"run_id": "run-b"})
    # An unbound review_started event alone must NOT imply IN_REVIEW.
    proj_unbound = project_candidate_state(db, "run-b")
    assert proj_unbound.state is CandidateState.READY
    _review_dispatch(db, "run-b", "rev-b1", candidate)
    proj = project_candidate_state(db, "run-b")
    assert proj.candidate_sha == candidate
    assert proj.state is CandidateState.IN_REVIEW
    assert proj.verdict is None
    assert proj.reason == "review_dispatched_no_substantive_completion"


def test_c_substantive_review_preserves_verdict(tmp_path: Path) -> None:
    for verdict in (
        ReviewVerdict.PASS,
        ReviewVerdict.BLOCK,
        ReviewVerdict.PASS_WITH_BACKLOG,
    ):
        db = _db(tmp_path / f"c-{verdict.value}")
        db.db_path.parent.mkdir(parents=True, exist_ok=True)
        base = _sha(f"base-c-{verdict.value}")
        candidate = _sha(f"candidate-c-{verdict.value}")
        _seed_run(db, "run-c", base)
        _gate_pass(db, "run-c", "run-c-a1", 1, candidate, base)
        _review_dispatch(db, "run-c", "rev-c1", candidate)
        _review(db, "run-c", "rev-c1", verdict)
        proj = project_candidate_state(db, "run-c")
        assert proj.candidate_sha == candidate
        assert proj.state is CandidateState.REVIEWED
        assert proj.verdict is verdict
        assert proj.reason == "substantive_review_completed"


def test_c1_terminal_convergence_verdicts_are_substantive(tmp_path: Path) -> None:
    assert ReviewVerdict.FINAL_BLOCK in SUBSTANTIVE_REVIEW_VERDICTS
    assert ReviewVerdict.CONVERGENCE_STALLED in SUBSTANTIVE_REVIEW_VERDICTS
    assert ReviewVerdict.REVIEW_EXECUTION_FAILED not in SUBSTANTIVE_REVIEW_VERDICTS
    assert ReviewVerdict.REVIEW_UNAVAILABLE not in SUBSTANTIVE_REVIEW_VERDICTS
    for verdict in (ReviewVerdict.FINAL_BLOCK, ReviewVerdict.CONVERGENCE_STALLED):
        db = _db(tmp_path / f"c1-{verdict.value}")
        db.db_path.parent.mkdir(parents=True, exist_ok=True)
        base = _sha(f"base-c1-{verdict.value}")
        candidate = _sha(f"candidate-c1-{verdict.value}")
        _seed_run(db, "run-c1", base)
        _gate_pass(db, "run-c1", "run-c1-a1", 1, candidate, base)
        _review_dispatch(db, "run-c1", "rev-c1", candidate)
        _review(db, "run-c1", "rev-c1", verdict)
        proj = project_candidate_state(db, "run-c1")
        assert proj.state is CandidateState.REVIEWED
        assert proj.verdict is verdict


def test_c2_failed_or_unavailable_review_never_reviewed(tmp_path: Path) -> None:
    for verdict in (
        ReviewVerdict.REVIEW_EXECUTION_FAILED,
        ReviewVerdict.REVIEW_UNAVAILABLE,
    ):
        db = _db(tmp_path / f"c2-{verdict.value}")
        db.db_path.parent.mkdir(parents=True, exist_ok=True)
        base = _sha(f"base-c2-{verdict.value}")
        candidate = _sha(f"candidate-c2-{verdict.value}")
        _seed_run(db, "run-c2", base)
        _gate_pass(db, "run-c2", "run-c2-a1", 1, candidate, base)
        _review_dispatch(db, "run-c2", "rev-c2", candidate)
        _review(db, "run-c2", "rev-c2", verdict)
        proj = project_candidate_state(db, "run-c2")
        assert proj.state is not CandidateState.REVIEWED
        # Infrastructure failure leaves the gate-passed candidate READY;
        # it is never presented as a successful review.
        assert proj.state is CandidateState.READY
        assert proj.verdict is None


def test_d_old_review_does_not_apply_to_new_candidate(tmp_path: Path) -> None:
    db = _db(tmp_path)
    base = _sha("base-d")
    candidate_a = _sha("candidate-d-a")
    candidate_b = _sha("candidate-d-b")
    _seed_run(db, "run-d", base)
    _gate_pass(db, "run-d", "run-d-a1", 1, candidate_a, base)
    _review_dispatch(db, "run-d", "rev-d-a", candidate_a)
    _review(db, "run-d", "rev-d-a", ReviewVerdict.PASS)
    # Stale in-progress dispatch noise for the old candidate.
    _review_dispatch(db, "run-d", "rev-d-a2", candidate_a)
    # Candidate B later becomes current via its own gate success.
    _gate_pass(db, "run-d", "run-d-a2", 2, candidate_b, base)
    proj = project_candidate_state(db, "run-d")
    assert proj.candidate_sha == candidate_b
    assert proj.state is CandidateState.READY
    assert proj.verdict is None


def test_e_matching_accept_is_accepted(tmp_path: Path) -> None:
    db = _db(tmp_path)
    base = _sha("base-e")
    candidate = _sha("candidate-e")
    _seed_run(db, "run-e", base)
    _gate_pass(db, "run-e", "run-e-a1", 1, candidate, base)
    _review_dispatch(db, "run-e", "rev-e1", candidate)
    _review(db, "run-e", "rev-e1", ReviewVerdict.PASS)
    _accept(db, "run-e", candidate)
    proj = project_candidate_state(db, "run-e")
    assert proj.candidate_sha == candidate
    assert proj.state is CandidateState.ACCEPTED


def test_f_accepted_survives_target_head_move(tmp_path: Path) -> None:
    db = _db(tmp_path)
    base = _sha("base-f")
    moved = _sha("moved-f")
    candidate = _sha("candidate-f")
    _seed_run(db, "run-f", base)
    _gate_pass(db, "run-f", "run-f-a1", 1, candidate, base)
    _accept(db, "run-f", candidate)
    proj = project_candidate_state(
        db, "run-f", target_head_resolver=lambda _rid, _b: moved
    )
    assert proj.state is CandidateState.ACCEPTED


def test_g_stale_for_nonaccepted_candidate_after_head_move(tmp_path: Path) -> None:
    db = _db(tmp_path)
    base = _sha("base-g")
    moved = _sha("moved-g")
    candidate = _sha("candidate-g")
    _seed_run(db, "run-g", base)
    _gate_pass(db, "run-g", "run-g-a1", 1, candidate, base)
    _review_dispatch(db, "run-g", "rev-g1", candidate)
    _review(db, "run-g", "rev-g1", ReviewVerdict.PASS)
    # Fresh HEAD: reviewed, not stale.
    fresh = project_candidate_state(
        db, "run-g", target_head_resolver=lambda _rid, _b: base
    )
    assert fresh.state is CandidateState.REVIEWED
    # Moved HEAD: the same evidence now projects STALE (R4 owns recovery).
    stale = project_candidate_state(
        db, "run-g", target_head_resolver=lambda _rid, _b: moved
    )
    assert stale.candidate_sha == candidate
    assert stale.state is CandidateState.STALE
    assert stale.reason == "target_head_moved"


def test_h_foreign_or_malformed_accept_never_accepts(tmp_path: Path) -> None:
    db = _db(tmp_path)
    base = _sha("base-h")
    candidate = _sha("candidate-h")
    foreign = _sha("foreign-h")
    _seed_run(db, "run-h", base)
    _gate_pass(db, "run-h", "run-h-a1", 1, candidate, base)
    _accept(db, "run-h", foreign)
    _accept(db, "run-h", None)  # malformed: no candidate SHA at all
    proj = project_candidate_state(db, "run-h")
    assert proj.candidate_sha == candidate
    assert proj.state is CandidateState.READY


def test_i_risk_adaptive_skip_stays_ready_never_reviewed(tmp_path: Path) -> None:
    db = _db(tmp_path)
    base = _sha("base-i")
    candidate = _sha("candidate-i")
    _seed_run(db, "run-i", base)
    _gate_pass(db, "run-i", "run-i-a1", 1, candidate, base)
    decision_json = _skip_decision_json("run-i")
    db.update_review_risk_decision("run-i", decision_json)
    db.record_event(
        "run-i",
        "review_skipped_by_risk_adaptive_policy",
        attempt_id="run-i-a1",
        payload={"skip_reason": "LOW docs-only delta"},
    )
    proj = project_candidate_state(db, "run-i")
    assert proj.candidate_sha == candidate
    assert proj.state is CandidateState.READY
    assert proj.verdict is None
    assert proj.review_required is False


def test_j_no_trustworthy_candidate_is_none(tmp_path: Path) -> None:
    db = _db(tmp_path)
    base = _sha("base-j")
    # No attempts at all: never fabricate READY.
    _seed_run(db, "run-j-empty", base)
    empty = project_candidate_state(db, "run-j-empty")
    assert empty.candidate_sha is None
    assert empty.state is None

    # Only FAILED attempts (even carrying SHAs): no current candidate.
    _seed_run(db, "run-j-failed", base)
    _failed_attempt_with_sha(
        db, "run-j-failed", "run-j-failed-a1", 1, _sha("failed-j"), base
    )
    failed = project_candidate_state(db, "run-j-failed")
    assert failed.candidate_sha is None
    assert failed.state is None

    # SUCCESS without a passed check row: gate success unproven, fail closed.
    _seed_run(db, "run-j-nocheck", base)
    db.create_attempt(
        Attempt(
            id="run-j-nocheck-a1",
            run_id="run-j-nocheck",
            attempt_number=1,
            provider="opencode",
            model="test-model",
            worktree_path="/tmp/r3a-wt",
            branch_name="r3a-candidate",
            status=AttemptStatus.SUCCESS,
            commit_sha=_sha("nocheck-j"),
            created_at=_CREATED,
            base_sha=base,
        )
    )
    nocheck = project_candidate_state(db, "run-j-nocheck")
    assert nocheck.state is None
    assert nocheck.reason == "candidate_without_gate_success"

    # Unknown run id: None, never fabricated.
    missing = project_candidate_state(db, "run-j-missing")
    assert missing.candidate_sha is None
    assert missing.state is None


def test_j1_failed_repair_sha_does_not_hijack_candidate(tmp_path: Path) -> None:
    db = _db(tmp_path)
    base = _sha("base-j1")
    verified = _sha("verified-j1")
    failed_sha = _sha("failed-repair-j1")
    _seed_run(db, "run-j1", base)
    _gate_pass(db, "run-j1", "run-j1-a1", 1, verified, base)
    # A later failed repair attempt carries its own commit SHA; the
    # owner-visible candidate remains the earlier verified one.
    _failed_attempt_with_sha(db, "run-j1", "run-j1-a2", 2, failed_sha, base)
    proj = project_candidate_state(db, "run-j1")
    assert proj.candidate_sha == verified
    assert proj.state is CandidateState.READY


def test_k_reopen_yields_identical_projection(tmp_path: Path) -> None:
    db = _db(tmp_path)
    base = _sha("base-k")
    candidate = _sha("candidate-k")
    _seed_run(db, "run-k", base)
    _gate_pass(db, "run-k", "run-k-a1", 1, candidate, base)
    _review_dispatch(db, "run-k", "rev-k1", candidate)
    _review(db, "run-k", "rev-k1", ReviewVerdict.PASS_WITH_BACKLOG)
    first = project_candidate_state(db, "run-k")
    reopened = Database(db.db_path)
    second = project_candidate_state(reopened, "run-k")
    assert first == second
    assert second.state is CandidateState.REVIEWED
    assert second.verdict is ReviewVerdict.PASS_WITH_BACKLOG


def test_l_ordering_noise_preserves_current_projection(tmp_path: Path) -> None:
    db = _db(tmp_path)
    base = _sha("base-l")
    candidate_a = _sha("candidate-l-a")
    candidate_b = _sha("candidate-l-b")
    _seed_run(db, "run-l", base)
    # Older candidate A: full history including its own acceptance.
    _gate_pass(db, "run-l", "run-l-a1", 1, candidate_a, base)
    db.record_event("run-l", "gate_failed", attempt_id="run-l-a1", payload={})
    _review_dispatch(db, "run-l", "rev-l-a", candidate_a)
    _review(db, "run-l", "rev-l-a", ReviewVerdict.BLOCK)
    _accept(db, "run-l", candidate_a)
    db.record_event("run-l", "review_started", payload={"run_id": "run-l"})
    # Newer candidate B becomes current through its own gate success.
    _gate_pass(db, "run-l", "run-l-a2", 2, candidate_b, base)
    first = project_candidate_state(db, "run-l")
    assert first.candidate_sha == candidate_b
    # A's acceptance must not leak onto B; B has no bound review.
    assert first.state is CandidateState.READY
    # Reordering-equivalent noise: more unbound events change nothing.
    db.record_event("run-l", "review_started", payload={"run_id": "run-l"})
    db.record_event("run-l", "gate_failed", attempt_id="run-l-a1", payload={})
    second = project_candidate_state(db, "run-l")
    assert second == first


def test_r3a1_forged_matching_sha_without_provenance_never_accepted(
    tmp_path: Path,
) -> None:
    """R3-A.1 pre-fix proof: matching SHA, absent provenance.

    Against starting HEAD 3290723 this projected ACCEPTED (weaker
    ``candidate_sha``-match rule) while the authoritative AcceptEngine
    verifier rejected the same event for lacking unambiguous provenance.
    """
    from saberops.autonomous_runtime import (
        AcceptanceAuthorityError,
        _verify_accept_engine_evidence,
    )

    db = _db(tmp_path)
    base = _sha("base-r3a1-proof")
    candidate = _sha("candidate-r3a1-proof")
    _seed_run(db, "run-r3a1-proof", base)
    _gate_pass(db, "run-r3a1-proof", "run-r3a1-proof-a1", 1, candidate, base)
    _accept_raw(
        db,
        "run-r3a1-proof",
        {"candidate_sha": candidate, "final_branch": "main", "gate_command": "true"},
    )
    try:
        _verify_accept_engine_evidence(
            db, run_id="run-r3a1-proof", final_sha=candidate
        )
    except AcceptanceAuthorityError:
        pass
    else:
        raise AssertionError("authoritative verifier must reject forged evidence")
    proj = project_candidate_state(db, "run-r3a1-proof")
    assert proj.candidate_sha == candidate
    assert proj.state is not CandidateState.ACCEPTED
    assert proj.state is CandidateState.READY


def test_r3a1_a_matching_sha_missing_provenance_never_accepted(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    base = _sha("base-r3a1-a")
    candidate = _sha("candidate-r3a1-a")
    _seed_run(db, "run-r3a1-a", base)
    _gate_pass(db, "run-r3a1-a", "run-r3a1-a-a1", 1, candidate, base)
    _accept_raw(
        db,
        "run-r3a1-a",
        {"candidate_sha": candidate, "final_branch": "main", "gate_command": "true"},
    )
    proj = project_candidate_state(db, "run-r3a1-a")
    assert proj.candidate_sha == candidate
    assert proj.state is CandidateState.READY


def test_r3a1_b_matching_sha_contradictory_provenance_never_accepted(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    base = _sha("base-r3a1-b")
    candidate = _sha("candidate-r3a1-b")
    other = _sha("other-r3a1-b")
    _seed_run(db, "run-r3a1-b", base)
    _gate_pass(db, "run-r3a1-b", "run-r3a1-b-a1", 1, candidate, base)
    _accept_raw(
        db,
        "run-r3a1-b",
        {
            "candidate_sha": candidate,
            "final_branch": "main",
            "gate_command": "true",
            "provenance": {"candidate_sha": other},
        },
    )
    proj = project_candidate_state(db, "run-r3a1-b")
    assert proj.candidate_sha == candidate
    assert proj.state is CandidateState.READY


def test_r3a1_c_valid_authoritative_evidence_accepted(tmp_path: Path) -> None:
    db = _db(tmp_path)
    base = _sha("base-r3a1-c")
    candidate = _sha("candidate-r3a1-c")
    _seed_run(db, "run-r3a1-c", base)
    _gate_pass(db, "run-r3a1-c", "run-r3a1-c-a1", 1, candidate, base)
    _accept(db, "run-r3a1-c", candidate)
    proj = project_candidate_state(db, "run-r3a1-c")
    assert proj.candidate_sha == candidate
    assert proj.state is CandidateState.ACCEPTED


def test_r3a1_d_valid_acceptance_survives_head_move(tmp_path: Path) -> None:
    db = _db(tmp_path)
    base = _sha("base-r3a1-d")
    moved = _sha("moved-r3a1-d")
    candidate = _sha("candidate-r3a1-d")
    _seed_run(db, "run-r3a1-d", base)
    _gate_pass(db, "run-r3a1-d", "run-r3a1-d-a1", 1, candidate, base)
    _accept(db, "run-r3a1-d", candidate)
    proj = project_candidate_state(
        db, "run-r3a1-d", target_head_resolver=lambda _rid, _b: moved
    )
    assert proj.state is CandidateState.ACCEPTED


def test_r3a1_e_foreign_candidate_never_accepted(tmp_path: Path) -> None:
    db = _db(tmp_path)
    base = _sha("base-r3a1-e")
    candidate = _sha("candidate-r3a1-e")
    foreign = _sha("foreign-r3a1-e")
    _seed_run(db, "run-r3a1-e", base)
    _gate_pass(db, "run-r3a1-e", "run-r3a1-e-a1", 1, candidate, base)
    _accept(db, "run-r3a1-e", foreign)
    proj = project_candidate_state(db, "run-r3a1-e")
    assert proj.candidate_sha == candidate
    assert proj.state is CandidateState.READY


def test_r3a1_f_malformed_acceptance_sha_never_accepted(tmp_path: Path) -> None:
    db = _db(tmp_path)
    base = _sha("base-r3a1-f")
    candidate = _sha("candidate-r3a1-f")
    _seed_run(db, "run-r3a1-f", base)
    _gate_pass(db, "run-r3a1-f", "run-r3a1-f-a1", 1, candidate, base)
    _accept_raw(
        db,
        "run-r3a1-f",
        {
            "candidate_sha": candidate[:8],
            "final_branch": "main",
            "gate_command": "true",
            "provenance": {"candidate_sha": candidate[:8]},
        },
    )
    truncated = project_candidate_state(db, "run-r3a1-f")
    assert truncated.state is CandidateState.READY
    _accept_raw(
        db,
        "run-r3a1-f",
        {
            "candidate_sha": candidate.upper(),
            "final_branch": "main",
            "gate_command": "true",
            "provenance": {"candidate_sha": candidate.upper()},
        },
    )
    upper = project_candidate_state(db, "run-r3a1-f")
    assert upper.state is CandidateState.READY


def test_r3a1_g_latest_record_wins_over_older_match(tmp_path: Path) -> None:
    db = _db(tmp_path / "g1")
    db.db_path.parent.mkdir(parents=True, exist_ok=True)
    base = _sha("base-r3a1-g1")
    candidate = _sha("candidate-r3a1-g1")
    _seed_run(db, "run-r3a1-g1", base)
    _gate_pass(db, "run-r3a1-g1", "run-r3a1-g1-a1", 1, candidate, base)
    # Older valid acceptance, then a contradictory latest record with a
    # matching candidate_sha but absent provenance: the canonical latest
    # semantics must fail closed, not honor the older match.
    _accept(db, "run-r3a1-g1", candidate)
    _accept_raw(
        db,
        "run-r3a1-g1",
        {"candidate_sha": candidate, "final_branch": "main", "gate_command": "true"},
    )
    proj = project_candidate_state(db, "run-r3a1-g1")
    assert proj.state is CandidateState.READY

    db2 = _db(tmp_path / "g2")
    db2.db_path.parent.mkdir(parents=True, exist_ok=True)
    base2 = _sha("base-r3a1-g2")
    candidate2 = _sha("candidate-r3a1-g2")
    _seed_run(db2, "run-r3a1-g2", base2)
    _gate_pass(db2, "run-r3a1-g2", "run-r3a1-g2-a1", 1, candidate2, base2)
    # Older forged record, then a valid latest record: ACCEPTED.
    _accept_raw(
        db2,
        "run-r3a1-g2",
        {"candidate_sha": candidate2, "final_branch": "main", "gate_command": "true"},
    )
    _accept(db2, "run-r3a1-g2", candidate2)
    proj2 = project_candidate_state(db2, "run-r3a1-g2")
    assert proj2.state is CandidateState.ACCEPTED

    db3 = _db(tmp_path / "g3")
    db3.db_path.parent.mkdir(parents=True, exist_ok=True)
    base3 = _sha("base-r3a1-g3")
    candidate3 = _sha("candidate-r3a1-g3")
    foreign3 = _sha("foreign-r3a1-g3")
    _seed_run(db3, "run-r3a1-g3", base3)
    _gate_pass(db3, "run-r3a1-g3", "run-r3a1-g3-a1", 1, candidate3, base3)
    # Older valid acceptance for the current candidate, then a latest
    # foreign record: the latest binds a different SHA, never ACCEPTED.
    _accept(db3, "run-r3a1-g3", candidate3)
    _accept(db3, "run-r3a1-g3", foreign3)
    proj3 = project_candidate_state(db3, "run-r3a1-g3")
    assert proj3.candidate_sha == candidate3
    assert proj3.state is CandidateState.READY


def test_r3a1_h_reopen_yields_identical_acceptance(tmp_path: Path) -> None:
    db = _db(tmp_path)
    base = _sha("base-r3a1-h")
    candidate = _sha("candidate-r3a1-h")
    _seed_run(db, "run-r3a1-h", base)
    _gate_pass(db, "run-r3a1-h", "run-r3a1-h-a1", 1, candidate, base)
    _accept(db, "run-r3a1-h", candidate)
    first = project_candidate_state(db, "run-r3a1-h")
    assert first.state is CandidateState.ACCEPTED
    reopened = Database(db.db_path)
    second = project_candidate_state(reopened, "run-r3a1-h")
    assert first == second
    assert second.state is CandidateState.ACCEPTED
