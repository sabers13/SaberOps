"""Phase 1 F15: automatic acceptance of a local commit in away/critical-only.

Every test starts from the A6 lifecycle rig (fresh store, toy repository, fake
worker/reviewer shims) and seeds the acceptance fixture: candidate A has a
candidate-linked PASS review, replacement candidate B is gate-passed but
unreviewed.  Auto-accept must accept only an eligible candidate, only while the
owner mode grants it and the work gate is open, and only as a local commit.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from lifecycle_rig import (
    AcceptanceFixture,
    RigEnvironment,
    build_acceptance_fixture,
    build_lifecycle_rig,
)

from saberops.authority import set_authority_mode
from saberops.auto_accept import (
    ACCEPT_UNDONE_EVENT,
    AUTO_ACCEPT_EVENT,
    auto_accept_candidate,
    auto_accept_next_candidate,
    evaluate_auto_accept,
    undo_auto_accept,
)
from saberops.candidate_lifecycle import project_candidate_state
from saberops.capacity.loop import run_scheduler_loop
from saberops.db import current_iso_timestamp
from saberops.gate_receipt import record_receipt, run_config_for_receipt
from saberops.model_access.readiness_service import ReadinessService
from saberops.models import (
    CandidateState,
    DispatchEvidence,
    DispatchReason,
    DispatchRole,
    DispatchStage,
    ReviewResult,
    ReviewVerdict,
    Tier,
)
from saberops.owner_mode import set_owner_mode
from saberops.paths import owner_state_dir

pytestmark = pytest.mark.packaged_routing_default

FUTURE = "2999-01-01T00:00:00Z"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture()
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RigEnvironment:
    return build_lifecycle_rig(tmp_path, monkeypatch)


@pytest.fixture()
def acceptance_rig(rig: RigEnvironment) -> AcceptanceFixture:
    return build_acceptance_fixture(rig)


def _grant_auto_accept(mode: str = "away") -> None:
    set_authority_mode("full_autonomy")
    if mode == "away":
        set_owner_mode(mode, until=FUTURE)
    else:
        set_owner_mode(mode)


def _record_review_for_b(
    fixture: AcceptanceFixture, verdict: ReviewVerdict
) -> str:
    """Record a candidate-linked review for replacement B with ``verdict``."""
    db = fixture.rig.db()
    review_id = f"rev_{fixture.run_id}_b_{verdict.value.lower()}"
    now = current_iso_timestamp()
    db.create_dispatch_evidence(
        DispatchEvidence(
            id=f"disp_{review_id}",
            run_id=fixture.run_id,
            association_id=review_id,
            review_id=review_id,
            stage=DispatchStage.REVIEW,
            role=DispatchRole.CHECK,
            tier=Tier.T1,
            dispatch_reason=DispatchReason.INDEPENDENT_REVIEW,
            requested_provider="anthropic",
            requested_model="claude-sonnet-4-6",
            candidate_sha=fixture.candidate_b_sha,
            started_at=now,
            completed_at=now,
        )
    )
    db.create_review(
        ReviewResult(
            id=review_id,
            run_id=fixture.run_id,
            provider="anthropic",
            model="claude-sonnet-4-6",
            verdict=verdict,
            summary=f"Candidate B review verdict {verdict.value}.",
            details="Recorded by the F15 test.",
            created_at=now,
        )
    )
    db.link_model_calls_to_review(fixture.run_id, [review_id], review_id)
    return review_id


def _recorder() -> tuple[list[tuple[Any, ...]], Any]:
    calls: list[tuple[Any, ...]] = []

    def notify(ledger: Path, key: str, title: str, body: str) -> None:
        calls.append((ledger, key, title, body))

    return calls, notify


def _tick(db: Any) -> Any:
    readiness = ReadinessService.for_owner_state(owner_state_dir())
    return run_scheduler_loop(
        db,
        once=True,
        readiness_service=readiness,
        run_launcher=lambda job, decision, run_id: run_id,
    )


def test_eligible_candidate_auto_accepts_in_away_and_undo_keeps_it(
    acceptance_rig: AcceptanceFixture,
) -> None:
    """A gate-passed, candidate-reviewed candidate lands as one local commit."""
    db = acceptance_rig.rig.db()
    review_id = _record_review_for_b(acceptance_rig, ReviewVerdict.PASS)
    _grant_auto_accept("away")

    decision = evaluate_auto_accept(db=db, run_id=acceptance_rig.run_id)
    assert decision.allowed is True, decision.as_dict()
    assert decision.candidate_sha == acceptance_rig.candidate_b_sha
    assert decision.review_id == review_id

    calls, notify = _recorder()
    outcome = auto_accept_candidate(
        db=db, run_id=acceptance_rig.run_id, notifier=notify
    )
    assert outcome.applied is True, outcome.reason
    assert _git(acceptance_rig.rig.repo, "rev-parse", "HEAD") == acceptance_rig.candidate_b_sha
    assert outcome.undo_command == f"orch accept {acceptance_rig.run_id} --undo"

    events = db.get_events_by_type(acceptance_rig.run_id, AUTO_ACCEPT_EVENT)
    assert len(events) == 1
    payload = events[0].payload
    assert payload is not None
    assert payload["candidate_sha"] == acceptance_rig.candidate_b_sha
    assert payload["base_sha"] == acceptance_rig.base_sha
    assert payload["review_id"] == review_id
    assert payload["local_commit_only"] is True
    assert payload["pushed"] is False
    assert len(calls) == 1
    assert calls[0][1] == (
        f"auto-accept:{acceptance_rig.run_id}:{acceptance_rig.candidate_b_sha}"
    )
    accepted = db.get_events_by_type(acceptance_rig.run_id, "run_accepted")
    assert accepted and accepted[-1].payload is not None
    automatic = accepted[-1].payload["automatic"]
    assert automatic["base_sha"] == acceptance_rig.base_sha
    assert automatic["review_id"] == review_id
    assert automatic["pushed"] is False

    projection = project_candidate_state(db, acceptance_rig.run_id)
    assert projection.state is CandidateState.ACCEPTED

    undo = undo_auto_accept(db=db, run_id=acceptance_rig.run_id)
    assert undo.undone is True, undo.reason
    assert _git(acceptance_rig.rig.repo, "rev-parse", "HEAD") == acceptance_rig.base_sha
    # The candidate branch, attempt and projection record stay intact.
    assert (
        _git(acceptance_rig.rig.repo, "rev-parse", acceptance_rig.candidate_branch_b)
        == acceptance_rig.candidate_b_sha
    )
    assert db.get_attempts_for_run(acceptance_rig.run_id)
    assert project_candidate_state(db, acceptance_rig.run_id).state is not CandidateState.ACCEPTED
    undone = db.get_events_by_type(acceptance_rig.run_id, ACCEPT_UNDONE_EVENT)
    assert len(undone) == 1
    assert undone[0].payload is not None
    assert undone[0].payload["restored_sha"] == acceptance_rig.base_sha
    assert undone[0].payload["kept_branch"] == acceptance_rig.candidate_branch_b


def test_auto_accept_works_in_critical_only(acceptance_rig: AcceptanceFixture) -> None:
    """The owner's answer names critical-only as the second accepting mode."""
    db = acceptance_rig.rig.db()
    _record_review_for_b(acceptance_rig, ReviewVerdict.PASS)
    _grant_auto_accept("critical-only")

    decision = evaluate_auto_accept(db=db, run_id=acceptance_rig.run_id)
    assert decision.allowed is True, decision.as_dict()
    assert decision.mode == "critical-only"


def test_auto_accept_never_runs_in_ask_me(acceptance_rig: AcceptanceFixture) -> None:
    """Ask-me keeps every accept manual, even with full autonomy selected."""
    db = acceptance_rig.rig.db()
    _record_review_for_b(acceptance_rig, ReviewVerdict.PASS)
    set_authority_mode("full_autonomy")
    set_owner_mode("ask-me")

    decision = evaluate_auto_accept(db=db, run_id=acceptance_rig.run_id)
    assert decision.allowed is False
    assert "ask-me" in decision.reason

    outcome = auto_accept_candidate(db=db, run_id=acceptance_rig.run_id)
    assert outcome.applied is False
    assert _git(acceptance_rig.rig.repo, "rev-parse", "HEAD") == acceptance_rig.base_sha
    assert db.get_events_by_type(acceptance_rig.run_id, AUTO_ACCEPT_EVENT) == []


def test_auto_accept_never_accepts_without_candidate_bound_review(
    acceptance_rig: AcceptanceFixture,
) -> None:
    """Candidate A's passing review never qualifies replacement candidate B."""
    db = acceptance_rig.rig.db()
    _grant_auto_accept("away")

    decision = evaluate_auto_accept(db=db, run_id=acceptance_rig.run_id)
    assert decision.allowed is False
    assert "no candidate-linked independent review" in decision.reason
    assert auto_accept_candidate(db=db, run_id=acceptance_rig.run_id).applied is False
    assert _git(acceptance_rig.rig.repo, "rev-parse", "HEAD") == acceptance_rig.base_sha


def test_auto_accept_never_accepts_a_blocking_review(
    acceptance_rig: AcceptanceFixture,
) -> None:
    """A candidate-linked BLOCK is never a substantive approval."""
    db = acceptance_rig.rig.db()
    _record_review_for_b(acceptance_rig, ReviewVerdict.BLOCK)
    _grant_auto_accept("away")

    decision = evaluate_auto_accept(db=db, run_id=acceptance_rig.run_id)
    assert decision.allowed is False
    assert "not a substantive approval" in decision.reason
    assert auto_accept_candidate(db=db, run_id=acceptance_rig.run_id).applied is False
    assert _git(acceptance_rig.rig.repo, "rev-parse", "HEAD") == acceptance_rig.base_sha


def test_auto_accept_refuses_while_the_work_gate_is_closed(
    acceptance_rig: AcceptanceFixture,
) -> None:
    """Closing the planning session pauses auto-accept like every other start."""
    db = acceptance_rig.rig.db()
    _record_review_for_b(acceptance_rig, ReviewVerdict.PASS)
    _grant_auto_accept("away")
    acceptance_rig.rig.end_attendance()

    decision = evaluate_auto_accept(db=db, run_id=acceptance_rig.run_id)
    assert decision.allowed is False
    assert "work gate is closed" in decision.reason
    assert auto_accept_candidate(db=db, run_id=acceptance_rig.run_id).applied is False
    assert _git(acceptance_rig.rig.repo, "rev-parse", "HEAD") == acceptance_rig.base_sha


def test_auto_accept_refuses_without_passing_gate_evidence(
    acceptance_rig: AcceptanceFixture,
) -> None:
    """Attempt success alone is not gate evidence; a passed check is required."""
    db = acceptance_rig.rig.db()
    _record_review_for_b(acceptance_rig, ReviewVerdict.PASS)
    _grant_auto_accept("away")
    with db._get_connection() as conn:
        conn.execute(
            "DELETE FROM checks WHERE attempt_id = ?",
            (f"{acceptance_rig.run_id}_a2",),
        )
        conn.commit()

    decision = evaluate_auto_accept(db=db, run_id=acceptance_rig.run_id)
    assert decision.allowed is False
    assert "no passing gate evidence" in decision.reason


def test_auto_accept_never_pushes(acceptance_rig: AcceptanceFixture, tmp_path: Path) -> None:
    """The local acceptance path leaves a configured origin untouched."""
    root = acceptance_rig.rig
    db = root.db()
    _record_review_for_b(acceptance_rig, ReviewVerdict.PASS)
    _grant_auto_accept("away")

    remote = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    _git(root.repo, "remote", "add", "origin", str(remote))
    _git(root.repo, "push", "origin", "main")
    remote_before = _git(remote, "rev-parse", "refs/heads/main")
    refs_before = _git(remote, "for-each-ref", "--format=%(refname) %(objectname)")

    outcome = auto_accept_candidate(db=db, run_id=acceptance_rig.run_id)
    assert outcome.applied is True, outcome.reason

    assert _git(remote, "rev-parse", "refs/heads/main") == remote_before
    assert _git(remote, "for-each-ref", "--format=%(refname) %(objectname)") == refs_before


def test_auto_accept_uses_a_valid_receipt_and_reruns_a_stale_one(
    acceptance_rig: AcceptanceFixture,
) -> None:
    """ADR-0010: only a fresh exact receipt may skip the post-integration gate."""
    db = acceptance_rig.rig.db()
    _record_review_for_b(acceptance_rig, ReviewVerdict.PASS)
    _grant_auto_accept("away")
    run = db.get_run(acceptance_rig.run_id)
    assert run is not None
    config = run_config_for_receipt(run)
    now = datetime.now(UTC)
    record_receipt(
        db,
        run_id=acceptance_rig.run_id,
        attempt_id=f"{acceptance_rig.run_id}_a2",
        config=config,
        base_sha=acceptance_rig.base_sha,
        candidate_sha=acceptance_rig.candidate_b_sha,
        started_at=(now - timedelta(minutes=1)).isoformat(),
        finished_at=now.isoformat(),
        passed=True,
    )

    outcome = auto_accept_candidate(db=db, run_id=acceptance_rig.run_id)
    assert outcome.applied is True, outcome.reason
    skipped = db.get_events_by_type(
        acceptance_rig.run_id, "post_integration_gate_skipped"
    )
    assert skipped, "a valid exact receipt should skip the duplicate gate"

    # Undo, then make the same receipt stale: the gate must run again.
    assert undo_auto_accept(db=db, run_id=acceptance_rig.run_id).undone is True
    with db._get_connection() as conn:
        conn.execute("DELETE FROM events WHERE event_type = 'run_accepted'")
        conn.execute("DELETE FROM events WHERE event_type = 'auto_accept_completed'")
        conn.execute("DELETE FROM events WHERE event_type = 'accept_undone'")
        conn.execute("DELETE FROM events WHERE event_type = 'full_gate_receipt'")
        conn.commit()
    old = datetime.now(UTC) - timedelta(days=2)
    record_receipt(
        db,
        run_id=acceptance_rig.run_id,
        attempt_id=f"{acceptance_rig.run_id}_a2",
        config=config,
        base_sha=acceptance_rig.base_sha,
        candidate_sha=acceptance_rig.candidate_b_sha,
        started_at=(old - timedelta(minutes=1)).isoformat(),
        finished_at=old.isoformat(),
        passed=True,
    )
    outcome = auto_accept_candidate(db=db, run_id=acceptance_rig.run_id)
    assert outcome.applied is True, outcome.reason
    accepted = db.get_events_by_type(acceptance_rig.run_id, "run_accepted")
    assert accepted and accepted[-1].payload is not None
    assert not accepted[-1].payload.get("gate_skipped")


def test_undo_refuses_when_later_work_moved_the_target(
    acceptance_rig: AcceptanceFixture,
) -> None:
    """Undo never overwrites a later commit; the candidate stays available."""
    db = acceptance_rig.rig.db()
    _record_review_for_b(acceptance_rig, ReviewVerdict.PASS)
    _grant_auto_accept("away")
    assert auto_accept_candidate(db=db, run_id=acceptance_rig.run_id).applied is True

    (acceptance_rig.rig.repo / "later.txt").write_text("later work\n", encoding="utf-8")
    _git(acceptance_rig.rig.repo, "add", "later.txt")
    _git(acceptance_rig.rig.repo, "commit", "-m", "later work")
    later = _git(acceptance_rig.rig.repo, "rev-parse", "HEAD")

    undo = undo_auto_accept(db=db, run_id=acceptance_rig.run_id)
    assert undo.undone is False
    assert "refusing to overwrite later work" in undo.reason
    assert _git(acceptance_rig.rig.repo, "rev-parse", "HEAD") == later
    assert (
        _git(acceptance_rig.rig.repo, "rev-parse", acceptance_rig.candidate_branch_b)
        == acceptance_rig.candidate_b_sha
    )


def test_undo_refuses_a_manual_acceptance(acceptance_rig: AcceptanceFixture) -> None:
    """The undo command is scoped to automatic acceptance only."""
    db = acceptance_rig.rig.db()
    undo = undo_auto_accept(db=db, run_id=acceptance_rig.run_id)
    assert undo.undone is False
    assert "no durable acceptance record" in undo.reason


def test_undo_refuses_after_a_later_manual_acceptance(
    acceptance_rig: AcceptanceFixture,
) -> None:
    """A manual re-accept after an undo is a new owner decision, not auto work."""
    from saberops.accept import AcceptEngine

    db = acceptance_rig.rig.db()
    _record_review_for_b(acceptance_rig, ReviewVerdict.PASS)
    _grant_auto_accept("away")
    assert auto_accept_candidate(db=db, run_id=acceptance_rig.run_id).applied is True
    assert undo_auto_accept(db=db, run_id=acceptance_rig.run_id).undone is True
    assert AcceptEngine(db=db).accept_run(acceptance_rig.run_id).accepted is True

    undo = undo_auto_accept(db=db, run_id=acceptance_rig.run_id)
    assert undo.undone is False
    assert "not an automatic acceptance" in undo.reason
    assert _git(acceptance_rig.rig.repo, "rev-parse", "HEAD") == acceptance_rig.candidate_b_sha


def test_cli_undo_is_one_command(acceptance_rig: AcceptanceFixture) -> None:
    """`orch accept <run-id> --undo` restores the target and keeps the candidate."""
    db = acceptance_rig.rig.db()
    _record_review_for_b(acceptance_rig, ReviewVerdict.PASS)
    _grant_auto_accept("away")
    assert auto_accept_candidate(db=db, run_id=acceptance_rig.run_id).applied is True

    result = acceptance_rig.rig.run_cli(
        "accept", acceptance_rig.run_id, "--undo", "--db", str(db.db_path)
    )
    assert result.returncode == 0, result.stderr
    assert "AUTO-ACCEPT UNDONE" in result.stdout
    assert _git(acceptance_rig.rig.repo, "rev-parse", "HEAD") == acceptance_rig.base_sha
    assert (
        _git(acceptance_rig.rig.repo, "rev-parse", acceptance_rig.candidate_branch_b)
        == acceptance_rig.candidate_b_sha
    )


def test_an_undone_candidate_is_not_auto_accepted_again(
    acceptance_rig: AcceptanceFixture,
) -> None:
    """Undo is an owner decision; the next tick must not silently re-apply it."""
    db = acceptance_rig.rig.db()
    _record_review_for_b(acceptance_rig, ReviewVerdict.PASS)
    _grant_auto_accept("away")
    assert auto_accept_candidate(db=db, run_id=acceptance_rig.run_id).applied is True
    assert undo_auto_accept(db=db, run_id=acceptance_rig.run_id).undone is True

    decision = evaluate_auto_accept(db=db, run_id=acceptance_rig.run_id)
    assert decision.allowed is False
    assert "already accepted" in decision.reason
    assert auto_accept_candidate(db=db, run_id=acceptance_rig.run_id).applied is False
    assert _git(acceptance_rig.rig.repo, "rev-parse", "HEAD") == acceptance_rig.base_sha


def test_scheduler_tick_auto_accepts_only_when_the_mode_allows(
    acceptance_rig: AcceptanceFixture,
) -> None:
    """The tick is the trigger: ask-me leaves the candidate alone."""
    db = acceptance_rig.rig.db()
    _record_review_for_b(acceptance_rig, ReviewVerdict.PASS)

    set_authority_mode("full_autonomy")
    set_owner_mode("ask-me")
    _tick(db)
    assert _git(acceptance_rig.rig.repo, "rev-parse", "HEAD") == acceptance_rig.base_sha

    set_owner_mode("away", until=FUTURE)
    _tick(db)
    assert _git(acceptance_rig.rig.repo, "rev-parse", "HEAD") == acceptance_rig.candidate_b_sha
    assert db.get_events_by_type(acceptance_rig.run_id, AUTO_ACCEPT_EVENT)


def test_scheduler_tick_skips_auto_accept_when_no_mode_grants_it(
    acceptance_rig: AcceptanceFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A normal attended store never even scans completed runs for acceptance."""
    db = acceptance_rig.rig.db()
    _record_review_for_b(acceptance_rig, ReviewVerdict.PASS)
    set_authority_mode("supervised")
    set_owner_mode("away", until=FUTURE)

    calls: list[str] = []

    def boom(*args: Any, **kwargs: Any) -> None:
        calls.append("scanned")
        raise AssertionError("auto-accept must not scan without a granting preset")

    monkeypatch.setattr("saberops.auto_accept.auto_accept_candidate", boom)
    assert auto_accept_next_candidate(db=db) is None
    assert calls == []
    assert _git(acceptance_rig.rig.repo, "rev-parse", "HEAD") == acceptance_rig.base_sha
