"""E5: read-only "catch me up" handoff from a second planning session.

A saved job must be recoverable after the original MCP session closes: the
fresh session identifies the current candidate, the latest check and review
(with the commit each assessed), any open question, and the next action the
engine allows.  The lookup writes nothing and needs no planning lease.

Hermetic: a temp store, a temp toy repository and real stdio MCP processes.
No vendor CLI, model or network call is made.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from saberops.db import Database, current_iso_timestamp
from saberops.jobs.models import RUN_FAILED, Job, JobRequirements, JobState, WaitingReason
from saberops.jobs.store import insert_job, mark_dispatched, reconcile_transition
from saberops.models import (
    Attempt,
    AttemptStatus,
    CheckResult,
    DispatchEvidence,
    DispatchReason,
    DispatchRole,
    DispatchStage,
    ReviewResult,
    ReviewVerdict,
    Run,
    RunStatus,
    Tier,
)
from saberops.questions import (
    AskRole,
    DecisionClass,
    Escalation,
    OwnerQuestionQueue,
    QuestionRequest,
)

CANDIDATE_A = "a" * 40
CANDIDATE_B = "b" * 40


def _git_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    for command in (
        ["git", "init", "-b", "main"],
        ["git", "config", "user.email", "e5@example.com"],
        ["git", "config", "user.name", "E5"],
    ):
        subprocess.run(command, cwd=path, check=True, capture_output=True)
    (path / "README.md").write_text("# repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"], cwd=path, check=True, capture_output=True
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture()
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """An isolated owner store, toy repo and explicit server paths."""
    home = tmp_path / "home"
    state = tmp_path / "state"
    config = tmp_path / "config"
    for directory in (home, state, config):
        directory.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setenv("TMPDIR", "/var/tmp")
    for var in (
        "SABEROPS_PROFILE",
        "ORCH_DIR",
        "SABEROPS_DIR",
        "ORCH_QUESTIONS_STORE",
    ):
        monkeypatch.delenv(var, raising=False)

    repo = tmp_path / "repo"
    base_sha = _git_repo(repo)
    state_dir = state / "saberops"
    state_dir.mkdir(parents=True)
    return {
        "root": tmp_path,
        "repo": repo,
        "base_sha": base_sha,
        "db_path": state_dir / "orchestrator.db",
        "questions_store": state_dir / "questions.json",
        "lease": tmp_path / "lease.json",
        "ledger": tmp_path / "events.jsonl",
        "config_ledger": tmp_path / "config.jsonl",
    }


def _job(
    *,
    job_id: str,
    goal: str,
    repo: Path,
    base_sha: str,
    digest: str,
) -> Job:
    now = current_iso_timestamp()
    return Job(
        job_id=job_id,
        idempotency_key=None,
        caller_id="planner",
        repo=str(repo),
        base_ref="HEAD",
        base_sha=base_sha,
        goal=goal,
        acceptance="pytest -q passes",
        requirements=JobRequirements(),
        state=JobState.QUEUED,
        waiting_reason=WaitingReason.NEW,
        created_at=now,
        updated_at=now,
        request_digest=digest,
    )


def _seed_failed_check_job(db: Database, store: dict[str, Any]) -> None:
    """A run whose latest check failed on a repair commit; candidate A survives."""
    repo = store["repo"]
    base_sha = store["base_sha"]
    run_id = "run-failed-check"
    now = current_iso_timestamp()
    db.create_run(
        Run(
            id=run_id,
            task="Fix the greeting",
            target_repo=str(repo),
            base_commit=base_sha,
            tier=Tier.T1,
            training_allowed=True,
            gate_command="pytest -q",
            status=RunStatus.FAILED,
            created_at=now,
            completed_at=now,
            result_summary="Gate failed on the repair attempt",
        )
    )
    attempt_a = f"{run_id}-a1"
    db.create_attempt(
        Attempt(
            id=attempt_a,
            run_id=run_id,
            attempt_number=1,
            provider="codex",
            model="gpt-5.6-terra",
            worktree_path=str(store["root"] / "worktree-a"),
            branch_name=f"orch/{run_id}/a1",
            status=AttemptStatus.SUCCESS,
            commit_sha=CANDIDATE_A,
            created_at=now,
            completed_at=now,
        )
    )
    db.create_check(
        CheckResult(
            id=f"check-{attempt_a}",
            attempt_id=attempt_a,
            gate_command="pytest -q",
            exit_code=0,
            stdout="12 passed\n",
            stderr="",
            passed=True,
            created_at="2026-11-19T10:00:00+00:00",
        )
    )
    review_id = f"review-{run_id}"
    db.create_dispatch_evidence(
        DispatchEvidence(
            id=f"dispatch-{review_id}",
            run_id=run_id,
            association_id=review_id,
            review_id=review_id,
            stage=DispatchStage.REVIEW,
            role=DispatchRole.CHECK,
            tier=Tier.T1,
            dispatch_reason=DispatchReason.INDEPENDENT_REVIEW,
            requested_provider="anthropic",
            requested_model="claude-sonnet-4-6",
            candidate_sha=CANDIDATE_A,
            started_at=now,
            completed_at=now,
        )
    )
    db.create_review(
        ReviewResult(
            id=review_id,
            run_id=run_id,
            provider="anthropic",
            model="claude-sonnet-4-6",
            verdict=ReviewVerdict.PASS,
            summary="Candidate A passed independent review.",
            details="Full review detail for candidate A.",
            created_at="2026-11-19T10:05:00+00:00",
        )
    )
    attempt_b = f"{run_id}-a2"
    db.create_attempt(
        Attempt(
            id=attempt_b,
            run_id=run_id,
            attempt_number=2,
            provider="codex",
            model="gpt-5.6-terra",
            worktree_path=str(store["root"] / "worktree-b"),
            branch_name=f"orch/{run_id}/a2",
            status=AttemptStatus.FAILED,
            worker_error="gate failed: exit 1",
            commit_sha=CANDIDATE_B,
            created_at=now,
            completed_at=now,
        )
    )
    db.create_check(
        CheckResult(
            id=f"check-{attempt_b}",
            attempt_id=attempt_b,
            gate_command="pytest -q",
            exit_code=1,
            stdout="",
            stderr="1 failed: test_greeting\n",
            passed=False,
            created_at="2026-11-19T10:10:00+00:00",
        )
    )

    job_id = "job-failed-check"
    insert_job(
        db,
        _job(
            job_id=job_id,
            goal="Fix the greeting",
            repo=repo,
            base_sha=base_sha,
            digest="digest-a",
        ),
    )
    mark_dispatched(db, job_id, run_id)
    reconcile_transition(db, job_id, to=JobState.FAILED, failure_reason=RUN_FAILED)


def _seed_waiting_job(db: Database, store: dict[str, Any]) -> str:
    """A running job blocked on an owner-only question; returns the question id."""
    run_id = "run-waiting"
    now = current_iso_timestamp()
    db.create_run(
        Run(
            id=run_id,
            task="Rename the public API",
            target_repo=str(store["repo"]),
            base_commit=store["base_sha"],
            tier=Tier.T1,
            training_allowed=True,
            gate_command="pytest -q",
            status=RunStatus.RUNNING,
            created_at=now,
        )
    )
    job_id = "job-waiting"
    insert_job(
        db,
        _job(
            job_id=job_id,
            goal="Rename the public API",
            repo=store["repo"],
            base_sha=store["base_sha"],
            digest="digest-b",
        ),
    )
    mark_dispatched(db, job_id, run_id)
    question = OwnerQuestionQueue(store["questions_store"]).ask(
        QuestionRequest(
            key="e5-billing-question",
            subject="May this job touch the billing posture?",
            kind="escalation",
            decision_class=DecisionClass.BILLING_OR_POSTURE,
            raised_by_role=AskRole.BUILD,
            raised_by="worker-1",
            escalation=Escalation(
                requirement="The rename needs a billing attestation.",
                proposed_expansion="Reconfirm the account posture.",
                smallest_compliant="Use the current attested posture.",
                consequences="Without it the run stays blocked.",
                recommendation="Reconfirm the current posture.",
            ),
            run_id=run_id,
            blocks=(job_id,),
            options=("yes", "no"),
        )
    )
    return question.question_id


def _server_command(store: dict[str, Any]) -> list[str]:
    return [
        sys.executable,
        "-m",
        "saberops.cli",
        "mcp",
        "serve",
        "--active",
        "--db",
        str(store["db_path"]),
        "--lease",
        str(store["lease"]),
        "--ledger",
        str(store["ledger"]),
        "--questions-store",
        str(store["questions_store"]),
        "--config-ledger",
        str(store["config_ledger"]),
        "--cwd",
        str(store["repo"]),
    ]


def _start(store: dict[str, Any]) -> tuple[subprocess.Popen[str], str]:
    process = subprocess.Popen(
        _server_command(store),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=dict(os.environ),
    )
    assert process.stderr is not None
    return process, process.stderr.readline()


def _send(process: subprocess.Popen[str], message: dict[str, Any]) -> dict[str, Any]:
    assert process.stdin is not None and process.stdout is not None
    process.stdin.write(json.dumps(message) + "\n")
    process.stdin.flush()
    return cast(dict[str, Any], json.loads(process.stdout.readline()))


def _initialize(process: subprocess.Popen[str], name: str) -> None:
    response = _send(
        process,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"clientInfo": {"name": name}},
        },
    )
    assert "result" in response, response
    assert process.stdin is not None
    process.stdin.write(
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
    )
    process.stdin.flush()


def _catch_up(process: subprocess.Popen[str], job_id: str) -> dict[str, Any]:
    response = _send(
        process,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "catch_up", "arguments": {"job_id": job_id}},
        },
    )
    assert "result" in response, response
    result = response["result"]
    assert result["isError"] is False, result
    return cast(dict[str, Any], result["structuredContent"])


def _stop(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    assert process.stdin is not None
    process.stdin.close()
    process.wait(timeout=15)


def test_fresh_session_catches_up_and_changes_nothing(store: dict[str, Any]) -> None:
    db = Database(store["db_path"])
    _seed_failed_check_job(db, store)
    question_id = _seed_waiting_job(db, store)

    ledger_before = store["ledger"].read_text() if store["ledger"].exists() else ""
    questions_before = store["questions_store"].read_text()

    # The original planning session reads the jobs, then closes.
    original, message = _start(store)
    assert "lease acquired" in message
    _initialize(original, "original-planner")
    first = _catch_up(original, "job-failed-check")
    _stop(original)

    # A fresh process against the same store, with no chat history.
    fresh, message = _start(store)
    assert "lease acquired" in message
    try:
        _initialize(fresh, "fresh-planner")
        failed = _catch_up(fresh, "job-failed-check")
        waiting = _catch_up(fresh, "job-waiting")
        again = _catch_up(fresh, "job-failed-check")
        assert first == failed == again

        # The pause block is a fact about the gate, not about the reader: it
        # carries the gate state and the owner resume command, never live
        # per-session presence, which differs between observers of one store.
        assert "presence" not in failed["pause"]
        assert failed["pause"]["closed"] is False
        assert failed["pause"]["resume_tool"] is None
        assert failed["pause"]["resume_command"] == "orch resume"

        # Catch-up works from a session that does NOT hold the writer lease.
        bystander, message = _start(store)
        assert "read-only" in message
        try:
            _initialize(bystander, "bystander-planner")
            assert _catch_up(bystander, "job-failed-check") == failed
        finally:
            _stop(bystander)
    finally:
        _stop(fresh)

    # Failed-check job: correct candidate, latest check on the repair commit,
    # review attributed to the commit it assessed, and the allowed next action.
    assert failed["request"]["goal"] == "Fix the greeting"
    assert failed["state"] == "FAILED"
    assert failed["run_status"] == "FAILED"
    assert failed["candidate"]["candidate_sha"] == CANDIDATE_A
    assert failed["candidate"]["base_sha"] == store["base_sha"]
    assert failed["candidate"]["state"] == "REVIEWED"
    assert failed["candidate"]["verdict"] == "PASS"
    assert failed["check"]["passed"] is False
    assert failed["check"]["assessed_commit"] == CANDIDATE_B
    assert failed["check"]["assessed_current_candidate"] is False
    assert "1 failed" in failed["check"]["excerpt"]
    assert failed["review"]["verdict"] == "PASS"
    assert failed["review"]["assessed_commit"] == CANDIDATE_A
    assert failed["review"]["assessed_current_candidate"] is True
    assert failed["failure"]["reason"] == "gate failed: exit 1"
    assert failed["failure"]["work_location"] == str(store["root"] / "worktree-b")
    assert failed["next_action"]["action"] == "inspect_and_resubmit"
    assert failed["result"]["owner_action"] is None
    assert failed["evidence"]["run_page"] == "/runs/run-failed-check"

    # Waiting-on-answer job: the blocker and the owner's next action.
    assert waiting["state"] == "RUNNING"
    assert waiting["candidate"]["candidate_sha"] is None
    assert waiting["candidate"]["reason"] == "no_trustworthy_candidate"
    assert waiting["open_questions"][0]["question_id"] == question_id
    assert waiting["open_questions"][0]["decision_class"] == "BILLING_OR_POSTURE"
    assert waiting["next_action"]["action"] == "answer_question"
    assert waiting["next_action"]["owner_only"] is True
    assert question_id in waiting["next_action"]["command"]

    # No state change, no notice, no retained lease.
    after_db = Database(store["db_path"], read_only=True)
    from saberops.jobs.service import get_job

    failed_job = get_job(after_db, "job-failed-check")
    assert failed_job is not None and failed_job.state is JobState.FAILED
    waiting_job = get_job(after_db, "job-waiting")
    assert waiting_job is not None and waiting_job.state is JobState.RUNNING
    assert store["questions_store"].read_text() == questions_before
    assert (store["ledger"].read_text() if store["ledger"].exists() else "") == ledger_before
    assert not store["lease"].read_text().strip()
    notifications = store["db_path"].with_name(
        store["db_path"].name + ".notifications.sqlite3"
    )
    assert not notifications.exists()


def test_next_action_offers_accept_and_the_web_reject_action() -> None:
    from types import SimpleNamespace

    from saberops.mcp.tools import _catch_up_next_action
    from saberops.models import CandidateState

    job = SimpleNamespace(state=JobState.SUCCEEDED, failure_reason=None, waiting_reason=None)
    result = {
        "run_id": "run-1",
        "candidate_sha": CANDIDATE_A,
        "owner_action": "orch accept run-1",
    }
    projection = SimpleNamespace(state=CandidateState.REVIEWED, candidate_sha=CANDIDATE_A)
    action = _catch_up_next_action(
        job, {"activity": None, "work_location": None}, result, projection, []
    )
    assert action["action"] == "owner_accept"
    assert action["command"] == "orch accept run-1"
    assert action["owner_only"] is True
    assert action["reject"]["surface"] == "web"
    assert action["reject"]["path"] == "/runs/run-1"


def test_guide_and_installed_skill_teach_the_phrase() -> None:
    from saberops.agent_connect import skill_document
    from saberops.mcp.guide import GUIDE, SHORT_INSTRUCTIONS

    assert "catch me up" in GUIDE.lower()
    assert "`catch_up`" in GUIDE
    assert "catch_up" in SHORT_INSTRUCTIONS
    assert "`catch_up`" in skill_document()
