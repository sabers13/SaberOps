"""R3-C: durable owner Reject decision regressions.

Builds every scenario with real Database APIs, the real
``project_candidate_state`` / ``reject_current_candidate`` seams, the
real AcceptEngine, and the real Web app -- never mocks of the seams
themselves.  Covers the required regressions A-K:

A. READY candidate -> Reject (one canonical event, exact SHA,
   REJECTED, survives DB close/reopen).
B. REVIEWED candidate -> Reject (review stays durable, rejection wins
   precedence).
C. Candidate scoping (rejecting A never rejects later candidate B).
D. Foreign/malformed rejection evidence never rejects the current
   candidate.
E. No gate proof -> Reject blocked, no event written.
F. Already accepted -> Reject blocked, no event, stays ACCEPTED.
G. Idempotency (exactly one event, both calls truthful).
H. Acceptance after rejection (eligibility blocker, accept_run refused
   before mutation, HEAD unchanged, no run_accepted event).
I. Diff preservation (same R3-B base->candidate diff after rejection).
J. Web parity (confirmation, POST, REJECTED display, Accept/Review
   blocked, Retry available, diff inspectable).
K. Read-only (lifecycle/action/page reads add no rejection event).
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import threading
from pathlib import Path

import pytest

from saberops.accept import AcceptEngine, AcceptPrecondition
from saberops.candidate_lifecycle import (
    REJECTION_EVENT_TYPE,
    can_reject_candidate,
    find_matching_rejection_event,
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
    OrchEvent,
    ReviewResult,
    ReviewVerdict,
    Run,
    RunStatus,
    Tier,
)
from saberops.service import OrchestratorService

_CREATED = "2026-09-24T10:00:00+00:00"


def _sha(tag: str) -> str:
    """Deterministic 40-hex candidate SHA for ``tag``."""
    return hashlib.sha1(tag.encode("utf-8")).hexdigest()


def _db(path: Path) -> Database:
    return Database(path / "orch.db")


def _seed_run(
    db: Database, run_id: str, base: str, repo: str = "/tmp/r3c-target"
) -> None:
    db.create_run(
        Run(
            id=run_id,
            task="r3c reject probe",
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
            worktree_path="/tmp/r3c-wt",
            branch_name="r3c-candidate",
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


def _accept(db: Database, run_id: str, sha: str) -> None:
    """Record valid authoritative AcceptEngine-shaped acceptance evidence."""
    db.record_event(
        run_id,
        "run_accepted",
        payload={
            "candidate_sha": sha,
            "final_branch": "main",
            "gate_command": "true",
            "provenance": {
                "candidate_sha": sha,
                "run_id": run_id,
                "attempt_id": "probe",
                "attempt_number": 1,
            },
        },
    )


def _rejections(db: Database, run_id: str) -> list[OrchEvent]:
    return list(db.get_events_by_type(run_id, REJECTION_EVENT_TYPE))


def _rejection_blocker(
    checks: tuple[AcceptPrecondition, ...],
) -> AcceptPrecondition | None:
    """Return the canonical rejection blocker check, if present."""
    return next(
        (check for check in checks if check.key == "candidate_not_rejected"),
        None,
    )


def _git_repo(path: Path) -> tuple[str, str, str]:
    """Create a real repo with base -> candidate commits.

    Returns ``(repo_path, base_sha, candidate_sha)`` with HEAD left at
    the base commit so the acceptance contract still holds.
    """
    repo = path / "target"
    repo.mkdir(parents=True, exist_ok=True)

    def _git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    _git("init", "-q")
    _git("config", "user.email", "r3c@test")
    _git("config", "user.name", "r3c")
    (repo / "feature.txt").write_text("base\n")
    _git("add", ".")
    _git("commit", "-qm", "base")
    base = _git("rev-parse", "HEAD")
    (repo / "feature.txt").write_text("candidate\n")
    _git("add", ".")
    _git("commit", "-qm", "candidate")
    candidate = _git("rev-parse", "HEAD")
    _git("branch", "-f", "r3c-candidate", candidate)
    _git("checkout", "-q", base)
    return str(repo), base, candidate


def _seed_eligible(
    db: Database, run_id: str, repo: str, base: str, candidate: str
) -> None:
    """Seed a run whose candidate is fully accept-eligible pre-rejection."""
    db.create_run(
        Run(
            id=run_id,
            task="r3c eligible probe",
            target_repo=repo,
            base_commit=base,
            tier=Tier.T1,
            training_allowed=True,
            gate_command="true",
            status=RunStatus.COMPLETED,
            created_at=_CREATED,
            completed_at=_CREATED,
            config_json=json.dumps({"review_enabled": False}),
        )
    )
    db.create_attempt(
        Attempt(
            id=f"{run_id}-a1",
            run_id=run_id,
            attempt_number=1,
            provider="opencode",
            model="test-model",
            worktree_path="/tmp/r3c-wt",
            branch_name="r3c-no-such-ref",
            status=AttemptStatus.SUCCESS,
            commit_sha=candidate,
            created_at=_CREATED,
            base_sha=base,
        )
    )
    db.create_check(
        CheckResult(
            id=f"check-{run_id}-a1",
            attempt_id=f"{run_id}-a1",
            gate_command="true",
            exit_code=0,
            stdout="ok",
            stderr="",
            passed=True,
            created_at=_CREATED,
        )
    )


def test_a_ready_candidate_reject(tmp_path: Path) -> None:
    db = _db(tmp_path)
    base = _sha("base-r3c-a")
    candidate = _sha("candidate-r3c-a")
    _seed_run(db, "run-a", base)
    _gate_pass(db, "run-a", "run-a-a1", 1, candidate, base)
    before = project_candidate_state(db, "run-a")
    assert before.state is CandidateState.READY

    service = OrchestratorService(db=db)
    result = service.reject_candidate("run-a")
    assert result.candidate_sha == candidate
    assert result.state is CandidateState.REJECTED
    assert result.reason == "owner_rejected_matching_candidate"
    assert any(
        part.startswith(f"event:{REJECTION_EVENT_TYPE}#") for part in result.evidence
    )

    events = _rejections(db, "run-a")
    assert len(events) == 1
    payload = events[0].payload
    assert isinstance(payload, dict)
    assert payload["candidate_sha"] == candidate

    reopened = Database(db.db_path)
    survived = project_candidate_state(reopened, "run-a")
    assert survived.candidate_sha == candidate
    assert survived.state is CandidateState.REJECTED
    assert survived.reason == "owner_rejected_matching_candidate"


def test_b_reviewed_candidate_reject(tmp_path: Path) -> None:
    db = _db(tmp_path)
    base = _sha("base-r3c-b")
    candidate = _sha("candidate-r3c-b")
    _seed_run(db, "run-b", base)
    _gate_pass(db, "run-b", "run-b-a1", 1, candidate, base)
    _review_dispatch(db, "run-b", "rev-b1", candidate)
    _review(db, "run-b", "rev-b1", ReviewVerdict.PASS)
    assert project_candidate_state(db, "run-b").state is CandidateState.REVIEWED

    OrchestratorService(db=db).reject_candidate("run-b")
    proj = project_candidate_state(db, "run-b")
    assert proj.candidate_sha == candidate
    assert proj.state is CandidateState.REJECTED

    # Review evidence remains durable but no longer wins precedence.
    reviews = db.get_reviews_for_run("run-b")
    assert len(reviews) == 1
    assert reviews[0].verdict is ReviewVerdict.PASS
    assert len(db.get_dispatch_evidence_for_run("run-b")) == 1


def test_c_reject_scoped_to_exact_candidate(tmp_path: Path) -> None:
    db = _db(tmp_path)
    base = _sha("base-r3c-c")
    candidate_a = _sha("candidate-r3c-c-a")
    candidate_b = _sha("candidate-r3c-c-b")
    _seed_run(db, "run-c", base)
    _gate_pass(db, "run-c", "run-c-a1", 1, candidate_a, base)
    OrchestratorService(db=db).reject_candidate("run-c")
    assert project_candidate_state(db, "run-c").state is CandidateState.REJECTED

    # Candidate B later becomes current through its own real passed gate.
    _gate_pass(db, "run-c", "run-c-a2", 2, candidate_b, base)
    proj = project_candidate_state(db, "run-c")
    assert proj.candidate_sha == candidate_b
    assert proj.state is CandidateState.READY
    assert find_matching_rejection_event(db, "run-c", candidate_b) is None


def test_d_foreign_or_malformed_rejection_never_rejects(tmp_path: Path) -> None:
    db = _db(tmp_path)
    base = _sha("base-r3c-d")
    candidate = _sha("candidate-r3c-d")
    foreign = _sha("foreign-r3c-d")
    _seed_run(db, "run-d", base)
    _gate_pass(db, "run-d", "run-d-a1", 1, candidate, base)

    db.record_event("run-d", REJECTION_EVENT_TYPE, payload={"candidate_sha": foreign})
    db.record_event("run-d", REJECTION_EVENT_TYPE, payload={})
    db.record_event(
        "run-d", REJECTION_EVENT_TYPE, payload={"candidate_sha": candidate[:8]}
    )
    db.record_event(
        "run-d", REJECTION_EVENT_TYPE, payload={"candidate_sha": candidate.upper()}
    )
    db.record_event("run-d", REJECTION_EVENT_TYPE, payload=None)

    proj = project_candidate_state(db, "run-d")
    assert proj.candidate_sha == candidate
    assert proj.state is CandidateState.READY
    assert find_matching_rejection_event(db, "run-d", candidate) is None


def test_e_no_gate_proof_reject_blocked(tmp_path: Path) -> None:
    db = _db(tmp_path)
    base = _sha("base-r3c-e")
    candidate = _sha("candidate-r3c-e")
    _seed_run(db, "run-e", base)
    # SUCCESS + SHA but no passed CheckResult: gate success unproven.
    db.create_attempt(
        Attempt(
            id="run-e-a1",
            run_id="run-e",
            attempt_number=1,
            provider="opencode",
            model="test-model",
            worktree_path="/tmp/r3c-wt",
            branch_name="r3c-candidate",
            status=AttemptStatus.SUCCESS,
            commit_sha=candidate,
            created_at=_CREATED,
            base_sha=base,
        )
    )
    proj = project_candidate_state(db, "run-e")
    assert proj.candidate_sha == candidate
    assert proj.state is None

    with pytest.raises(RuntimeError):
        OrchestratorService(db=db).reject_candidate("run-e")
    assert _rejections(db, "run-e") == []


def test_e1_noncanonical_candidate_sha_reject_blocked(tmp_path: Path) -> None:
    db = _db(tmp_path)
    base = _sha("base-r3c-e1")
    malformed = "abc"
    _seed_run(db, "run-e1", base)
    _gate_pass(db, "run-e1", "run-e1-a1", 1, malformed, base)

    proj = project_candidate_state(db, "run-e1")
    assert proj.candidate_sha == malformed
    assert proj.state is CandidateState.READY
    assert can_reject_candidate(db, "run-e1") is False

    with pytest.raises(RuntimeError):
        OrchestratorService(db=db).reject_candidate("run-e1")
    assert _rejections(db, "run-e1") == []

    reopened = Database(db.db_path)
    assert _rejections(reopened, "run-e1") == []
    survived = project_candidate_state(reopened, "run-e1")
    assert survived.candidate_sha == malformed
    assert survived.state is CandidateState.READY


def test_f_already_accepted_reject_blocked(tmp_path: Path) -> None:
    db = _db(tmp_path)
    base = _sha("base-r3c-f")
    candidate = _sha("candidate-r3c-f")
    _seed_run(db, "run-f", base)
    _gate_pass(db, "run-f", "run-f-a1", 1, candidate, base)
    _accept(db, "run-f", candidate)
    assert project_candidate_state(db, "run-f").state is CandidateState.ACCEPTED

    with pytest.raises(RuntimeError):
        OrchestratorService(db=db).reject_candidate("run-f")
    assert _rejections(db, "run-f") == []
    after = project_candidate_state(db, "run-f")
    assert after.state is CandidateState.ACCEPTED


def test_g_reject_idempotent(tmp_path: Path) -> None:
    db = _db(tmp_path)
    base = _sha("base-r3c-g")
    candidate = _sha("candidate-r3c-g")
    _seed_run(db, "run-g", base)
    _gate_pass(db, "run-g", "run-g-a1", 1, candidate, base)
    service = OrchestratorService(db=db)

    first = service.reject_candidate("run-g")
    second = service.reject_candidate("run-g")
    assert first.state is CandidateState.REJECTED
    assert second.state is CandidateState.REJECTED
    assert second.candidate_sha == candidate
    assert len(_rejections(db, "run-g")) == 1
    assert project_candidate_state(db, "run-g").state is CandidateState.REJECTED


def test_h_acceptance_after_rejection(tmp_path: Path) -> None:
    repo, base, candidate = _git_repo(tmp_path / "repo-h")
    db = _db(tmp_path / "db-h")
    db.db_path.parent.mkdir(parents=True, exist_ok=True)
    _seed_eligible(db, "run-h", repo, base, candidate)

    engine = AcceptEngine(db=db)
    assert engine.check_accept_eligibility("run-h").eligible is True

    OrchestratorService(db=db).reject_candidate("run-h")
    assert project_candidate_state(db, "run-h").state is CandidateState.REJECTED

    eligibility = engine.check_accept_eligibility("run-h")
    assert eligibility.eligible is False
    blocker = _rejection_blocker(eligibility.checks)
    assert blocker is not None
    assert blocker.status == "fail"

    head_before = subprocess.run(
        ["git", "-C", repo, "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    with pytest.raises(RuntimeError, match="[Rr]eject"):
        engine.accept_run("run-h")
    head_after = subprocess.run(
        ["git", "-C", repo, "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert head_after == head_before == base
    assert db.get_events_by_type("run-h", "run_accepted") == []


def test_h1_rejection_blocker_check_shape(tmp_path: Path) -> None:
    db = _db(tmp_path)
    base = _sha("base-r3c-h1")
    candidate = _sha("candidate-r3c-h1")
    _seed_run(db, "run-h1", base)
    _gate_pass(db, "run-h1", "run-h1-a1", 1, candidate, base)
    OrchestratorService(db=db).reject_candidate("run-h1")

    eligibility = AcceptEngine(db=db).check_accept_eligibility("run-h1")
    assert eligibility.eligible is False
    matches = [c for c in eligibility.checks if c.key == "candidate_not_rejected"]
    assert len(matches) == 1
    assert matches[0].label == "Candidate not rejected"
    assert matches[0].status == "fail"


def test_i_diff_preserved_after_rejection(tmp_path: Path) -> None:
    from saberops.candidate_diff import project_candidate_diff

    repo, base, candidate = _git_repo(tmp_path / "repo-i")
    db = _db(tmp_path / "db-i")
    db.db_path.parent.mkdir(parents=True, exist_ok=True)
    _seed_eligible(db, "run-i", repo, base, candidate)

    before = project_candidate_diff(db, "run-i")
    assert before.available is True
    assert before.candidate_sha == candidate
    assert before.base_sha == base

    OrchestratorService(db=db).reject_candidate("run-i")

    after = project_candidate_diff(db, "run-i")
    assert after == before
    assert after.available is True
    assert after.diff_text
    assert after.changed_paths == ("feature.txt",)


def test_j_web_parity(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from saberops.web import create_app

    repo, base, candidate = _git_repo(tmp_path / "repo-j")
    db_path = tmp_path / "web.db"
    db = Database(db_path)
    _seed_eligible(db, "run-j", repo, base, candidate)
    client = TestClient(create_app(db_path=db_path))

    actions_before = client.get("/runs/run-j/actions").json()
    assert actions_before["candidate_state"] == "READY"
    assert actions_before["can_reject"] is True

    page = client.get("/runs/run-j")
    assert page.status_code == 200
    assert "Reject candidate" in page.text
    assert 'data-action="reject"' in page.text

    confirm = client.post("/runs/run-j/reject", data={"confirm": "0"})
    assert confirm.status_code == 200
    assert "Reject this candidate?" in confirm.text
    assert "This records a durable owner decision." in confirm.text
    assert "It does not delete the commit or worktree." in confirm.text
    assert "Type the exact run id" not in confirm.text
    rejected_events = db.get_events_by_type("run-j", REJECTION_EVENT_TYPE)
    assert rejected_events == []

    decided = client.post(
        "/runs/run-j/reject", data={"confirm": "1"}, follow_redirects=False
    )
    assert decided.status_code == 303
    assert decided.headers["location"].endswith("/runs/run-j?rejected=1")

    events = db.get_events_by_type("run-j", REJECTION_EVENT_TYPE)
    assert len(events) == 1
    assert isinstance(events[0].payload, dict)
    assert events[0].payload["candidate_sha"] == candidate

    after = client.get("/runs/run-j?rejected=1")
    assert after.status_code == 200
    assert "REJECTED" in after.text
    assert 'id="candidate-state"' in after.text
    assert 'data-action="reject"' not in after.text
    assert 'disabled title="Accept is blocked by the checklist below"' in after.text
    assert 'disabled title="Review unlocks once the run completes"' in after.text
    assert (
        'data-action="review"><button type="submit" '
        'class="text-button primary-button">Request review' not in after.text
    )
    assert "/runs/run-j/retry" in after.text
    assert candidate in after.text

    actions_after = client.get("/runs/run-j/actions").json()
    assert actions_after["candidate_state"] == "REJECTED"
    assert actions_after["can_reject"] is False
    assert actions_after["can_accept"] is False
    assert actions_after["can_review"] is False
    assert actions_after["can_retry"] is True


def test_g1_concurrent_reject_is_durably_idempotent(tmp_path: Path) -> None:
    """Concurrent Reject from separate DB instances writes exactly one event.

    R3-C.1 regression: N callers contend on the real durable SQLite
    boundary (own Database instance each, barrier-synchronized start,
    real service-level Reject).  Exactly one canonical
    ``candidate_rejected`` event must exist afterwards and every caller
    must resolve truthfully to the same REJECTED candidate.  Repeated
    over several trials so a non-atomic check-then-write cannot pass by
    scheduling luck.
    """
    from saberops.candidate_lifecycle import CandidateProjection

    db_path = tmp_path / "orch-g1.db"
    seed_db = Database(db_path)
    n_callers = 16
    n_trials = 5
    for trial in range(n_trials):
        run_id = f"run-g1-t{trial}"
        base = _sha(f"base-r3c-g1-{trial}")
        candidate = _sha(f"candidate-r3c-g1-{trial}")
        _seed_run(seed_db, run_id, base)
        _gate_pass(seed_db, run_id, f"{run_id}-a1", 1, candidate, base)
        assert project_candidate_state(seed_db, run_id).state is CandidateState.READY
        assert find_matching_rejection_event(seed_db, run_id, candidate) is None

        barrier = threading.Barrier(n_callers)
        # One independent Database instance per caller, all pointed at
        # the same durable file; built before contention starts so only
        # the Reject decision itself contends.
        caller_dbs = [Database(db_path) for _ in range(n_callers)]
        results: list[CandidateProjection | None] = [None] * n_callers
        errors: list[BaseException | None] = [None] * n_callers

        def _work(
            index: int,
            _barrier: threading.Barrier = barrier,
            _dbs: list[Database] = caller_dbs,
            _run: str = run_id,
            _results: list[CandidateProjection | None] = results,
            _errors: list[BaseException | None] = errors,
        ) -> None:
            try:
                _barrier.wait(timeout=30)
                _results[index] = OrchestratorService(
                    db=_dbs[index]
                ).reject_candidate(_run)
            except BaseException as exc:  # noqa: BLE001 - collected, asserted below
                _errors[index] = exc

        threads = [
            threading.Thread(target=_work, args=(index,), name=f"g1-{trial}-{index}")
            for index in range(n_callers)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
        assert all(not thread.is_alive() for thread in threads)
        for index in range(n_callers):
            assert errors[index] is None, f"trial {trial} caller {index}: {errors[index]!r}"
            proj = results[index]
            assert proj is not None
            assert proj.candidate_sha == candidate
            assert proj.state is CandidateState.REJECTED

        check_db = Database(db_path)
        events = _rejections(check_db, run_id)
        assert len(events) == 1, f"trial {trial}: {len(events)} rejection events"
        assert events[0].payload == {"candidate_sha": candidate}

        reopened = Database(db_path)
        survived = project_candidate_state(reopened, run_id)
        assert survived.candidate_sha == candidate
        assert survived.state is CandidateState.REJECTED

        retry = OrchestratorService(db=Database(db_path)).reject_candidate(run_id)
        assert retry.candidate_sha == candidate
        assert retry.state is CandidateState.REJECTED
        assert len(_rejections(Database(db_path), run_id)) == 1


def test_k_reads_alone_add_no_rejection_event(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from saberops.candidate_diff import project_candidate_diff
    from saberops.web import create_app

    repo, base, candidate = _git_repo(tmp_path / "repo-k")
    db_path = tmp_path / "web-k.db"
    db = Database(db_path)
    _seed_eligible(db, "run-k", repo, base, candidate)

    project_candidate_state(db, "run-k")
    project_candidate_diff(db, "run-k")
    AcceptEngine(db=db).check_accept_eligibility("run-k")
    assert db.get_events_by_type("run-k", REJECTION_EVENT_TYPE) == []

    client = TestClient(create_app(db_path=db_path))
    assert client.get("/runs/run-k").status_code == 200
    assert client.get("/runs/run-k/actions").status_code == 200
    assert client.get("/runs/run-k/diff").status_code == 200

    fresh = Database(db_path)
    assert fresh.get_events_by_type("run-k", REJECTION_EVENT_TYPE) == []
    assert project_candidate_state(fresh, "run-k").state is CandidateState.READY
