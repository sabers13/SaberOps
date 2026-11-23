"""H6 (ADR-0016): steering that always arrives.

The lane's contract:

* ``redirect_job`` (MCP and CLI) stops the current attempt, keeps its work in
  progress and starts a continuation attempt whose prompt carries the note.
* A candidate whose job still has an undelivered note cannot be accepted, nor
  auto-accepted in away mode, and the planner is told to redirect or discard
  the note.
* Plain ``note_job`` keeps meaning "the next attempt is fine".

The tests use the real lifecycle rig (isolated store, toy repository, fake CLI
shims) plus the acceptance fixture; no vendor CLI is launched and no prompt is
sent to a real model.
"""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from lifecycle_rig import (
    RigEnvironment,
    build_acceptance_fixture,
    build_lifecycle_rig,
)

from saberops.accept import AcceptEngine
from saberops.auto_accept import (
    AUTO_ACCEPT_EVENT,
    auto_accept_candidate,
    auto_accept_next_candidate,
)
from saberops.capacity.loop import run_scheduler_loop
from saberops.db import Database
from saberops.jobs.models import JobState, WaitingReason
from saberops.jobs.service import get_job, note_job, submit_job
from saberops.jobs.store import mark_dispatched, reconcile_transition
from saberops.mcp.errors import InvalidParamsError
from saberops.mcp.evidence import EvidenceRoots
from saberops.mcp.lease import OrchestratorLease
from saberops.mcp.ledger import EventLedger
from saberops.mcp.tools import TOOL_NAMES, ServerContext, build_default_registry
from saberops.model_access.readiness_service import ReadinessService
from saberops.models import RunStatus
from saberops.owner_mode import set_owner_mode
from saberops.paths import owner_state_dir
from saberops.supervisor import RunSupervisor

pytestmark = pytest.mark.packaged_routing_default

MARKER = "STEERING-H6: keep the public helper name; do not rename it."
FUTURE = "2999-01-01T00:00:00Z"
_SETUP_TIMEOUT = 180


@pytest.fixture()
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RigEnvironment:
    return build_lifecycle_rig(tmp_path, monkeypatch)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _tick(rig: RigEnvironment) -> Any:
    readiness = ReadinessService.for_owner_state(owner_state_dir())
    return run_scheduler_loop(rig.db(), once=True, readiness_service=readiness)


def _wait(predicate: Callable[[], bool], *, timeout: float = _SETUP_TIMEOUT) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.2)
    pytest.fail(f"{getattr(predicate, '__name__', 'condition')} did not become true")


def _registry(tmp_path: Path, db: Database) -> Any:
    lease = OrchestratorLease(tmp_path / "h6-lease.json", holder="h6-test", pid=1)
    lease.acquire()
    context = ServerContext(
        db_path=db.db_path,
        questions_store=tmp_path / "questions.json",
        config_ledger_path=tmp_path / "config.jsonl",
        evidence=EvidenceRoots(roots=(tmp_path,)),
        ledger=EventLedger(tmp_path / "events.jsonl"),
        lease=lease,
    )
    return build_default_registry(context)


def _completed_job_for_run(rig: RigEnvironment, run_id: str, *, note: str | None = None) -> str:
    """Attach the fixture run to a real job, note it, then let the job succeed."""
    db = rig.db()
    submitted = submit_job(db, repo=rig.repo, goal="H6 undelivered note probe")
    job_id = submitted.job.job_id
    mark_dispatched(db, job_id, run_id)
    if note is not None:
        note_job(db, job_id, note)
    reconcile_transition(db, job_id, to=JobState.SUCCEEDED)
    return job_id


def _wait_for_logged_prompt(path: Path, *, timeout: float = _SETUP_TIMEOUT) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file() and path.stat().st_size > 0:
            return
        time.sleep(0.05)
    pytest.fail(f"no worker prompt logged to {path}")


# ---------------------------------------------------------------------------
# The engine rule: no acceptance past an undelivered note
# ---------------------------------------------------------------------------


def test_note_then_success_blocks_acceptance_with_a_clear_reason(rig: RigEnvironment) -> None:
    """A finished candidate with a pending note is not acceptable until resolved."""
    from saberops.jobs.service import discard_note, undelivered_steering_notes

    fixture = build_acceptance_fixture(rig)
    fixture.record_passing_review_for_b()
    job_id = _completed_job_for_run(rig, fixture.run_id, note=MARKER)
    db = rig.db()

    pending = undelivered_steering_notes(db, job_id)
    assert [item.body for item in pending] == [MARKER]

    eligibility = AcceptEngine(db).check_accept_eligibility(fixture.run_id)
    assert eligibility.eligible is False, eligibility
    note_check = next(check for check in eligibility.checks if check.key == "steering_notes")
    assert note_check.status == "fail"
    assert "undelivered steering note" in note_check.reason
    assert "redirect_job" in note_check.reason
    assert "discard_note" in note_check.reason

    with pytest.raises(RuntimeError) as refusal:
        rig.service().accept_run(fixture.run_id)
    message = str(refusal.value)
    assert "undelivered steering note" in message
    assert "redirect_job" in message
    assert "discard_note" in message
    # Nothing was integrated by the refused acceptance.
    assert _git(rig.repo, "rev-parse", "HEAD") == fixture.base_sha

    discarded = discard_note(db, pending[0].note_id)
    assert discarded.discarded is True
    assert undelivered_steering_notes(db, job_id) == ()
    assert AcceptEngine(db).check_accept_eligibility(fixture.run_id).eligible is True

    accepted = rig.service().accept_run(fixture.run_id)
    assert accepted.accepted is True
    assert _git(rig.repo, "rev-parse", "HEAD") == fixture.candidate_b_sha


def test_away_mode_never_auto_accepts_past_an_undelivered_note(
    rig: RigEnvironment,
) -> None:
    """The away-mode auto-accept pass refuses a candidate with a pending note."""
    fixture = build_acceptance_fixture(rig)
    fixture.record_passing_review_for_b()
    _completed_job_for_run(rig, fixture.run_id, note=MARKER)
    db = rig.db()
    set_owner_mode("away", until=FUTURE)

    from saberops.auto_accept import evaluate_auto_accept

    decision = evaluate_auto_accept(db=db, run_id=fixture.run_id)
    assert decision.allowed is False, decision.as_dict()
    assert "undelivered steering note" in decision.reason
    assert "redirect_job" in decision.reason

    outcome = auto_accept_candidate(db=db, run_id=fixture.run_id)
    assert outcome.applied is False
    assert "undelivered steering note" in outcome.reason
    assert auto_accept_next_candidate(db=db, now=datetime.now(UTC)) is None
    assert db.get_events_by_type(fixture.run_id, AUTO_ACCEPT_EVENT) == []
    assert _git(rig.repo, "rev-parse", "HEAD") == fixture.base_sha


# ---------------------------------------------------------------------------
# Redirect: stop the attempt, keep the work, carry the note
# ---------------------------------------------------------------------------


def test_redirect_preserves_the_work_and_the_next_attempt_carries_the_note(
    rig: RigEnvironment,
) -> None:
    """A redirected running job continues from its WIP with the note in the prompt."""
    from lifecycle_rig import WORKER_CANDIDATE_OPENCODE

    from saberops.jobs.service import redirect_job, steering_notes, undelivered_steering_notes
    from saberops.routing_config import (
        load_effective_routing_payload,
        remove_candidate_from_chain,
        save_user_routing,
    )

    db = rig.db()
    # One worker harness keeps the continuation attempt's prompt observable.
    save_user_routing(
        remove_candidate_from_chain(
            load_effective_routing_payload(),
            role="BUILD",
            candidate_id=WORKER_CANDIDATE_OPENCODE,
        )
    )
    prompt_log = rig.root / "worker-prompts.jsonl"
    rig.set_worker_mode("hang", hang_seconds=180, prompt_log=prompt_log)
    submitted = submit_job(db, repo=rig.repo, goal="redirect a running job")

    def dispatched() -> bool:
        _tick(rig)
        current = get_job(db, submitted.job.job_id)
        return bool(current and current.current_run_id)

    _wait(dispatched)
    job = get_job(db, submitted.job.job_id)
    assert job is not None and job.current_run_id is not None
    first_run = job.current_run_id

    def worker_started() -> bool:
        owner, _ = RunSupervisor(db=db).owner_evidence(first_run)
        supervision = db.get_supervision(first_run)
        return bool(owner and supervision and supervision.worker_pid)

    _wait(worker_started)

    def attempt_started() -> bool:
        return bool(db.get_attempts_for_run(first_run))

    _wait(attempt_started)
    attempts = db.get_attempts_for_run(first_run)
    worktree = Path(attempts[-1].worktree_path)
    marker = worktree / "unfinished-work.txt"
    marker.write_text("preserved by redirect\n", encoding="utf-8")
    _wait_for_logged_prompt(prompt_log)

    result = redirect_job(db, job.job_id, MARKER)

    assert result.stopped_run_id == first_run
    assert result.note.delivered is False
    assert result.note.body == MARKER
    stopped = db.get_run(first_run)
    assert stopped is not None and stopped.status is RunStatus.PREEMPTED
    redirected = get_job(db, job.job_id)
    assert redirected is not None
    assert redirected.state is JobState.QUEUED
    assert redirected.waiting_reason is WaitingReason.REDIRECTED
    assert redirected.preemptions == 0  # a redirect never spends the preemption budget
    assert redirected.resume_from is not None
    assert redirected.resume_from.run_id == first_run
    assert result.checkpoint_sha is not None
    # The WIP is intact in the old worktree and captured by the checkpoint.
    assert marker.read_text(encoding="utf-8") == "preserved by redirect\n"
    preserved = _git(
        rig.repo, "show", f"{result.checkpoint_sha}:unfinished-work.txt"
    )
    assert preserved == "preserved by redirect"
    assert [item.note_id for item in undelivered_steering_notes(db, job.job_id)] == [
        result.note.note_id
    ]

    rig.set_worker_mode(
        "commit", target_file="redirected.txt", content="redirected\n", prompt_log=prompt_log
    )

    def redispatched() -> bool:
        _tick(rig)
        current = get_job(db, job.job_id)
        return bool(current and current.current_run_id not in (None, first_run))

    _wait(redispatched)
    current = get_job(db, job.job_id)
    assert current is not None and current.current_run_id is not None
    second_run = current.current_run_id
    run = rig.wait_for_run(second_run, timeout=240)
    assert run.status is RunStatus.COMPLETED, run.result_summary

    prompts = [
        json.loads(line)["prompt"]
        for line in prompt_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert prompts
    assert sum(prompt.count(MARKER) for prompt in prompts) == 1
    assert MARKER in prompts[-1]

    recorded = steering_notes(db, job.job_id)
    assert len(recorded) == 1
    assert recorded[0].note_id == result.note.note_id
    assert recorded[0].delivered is True
    assert recorded[0].delivered_run_id == second_run
    assert recorded[0].delivered_attempt_id == db.get_attempts_for_run(second_run)[-1].id
    # The continuation candidate descends from the preserved checkpoint.
    candidate = db.get_attempts_for_run(second_run)[-1].commit_sha
    assert candidate is not None
    assert (
        subprocess.run(
            ["git", "-C", str(rig.repo), "merge-base", "--is-ancestor",
             str(result.checkpoint_sha), candidate],
            capture_output=True,
        ).returncode
        == 0
    )


def test_redirect_refuses_a_job_with_no_running_attempt(rig: RigEnvironment) -> None:
    """A queued job has no attempt to stop; the note tool is the right call."""
    from saberops.jobs.service import redirect_job

    db = rig.db()
    submitted = submit_job(db, repo=rig.repo, goal="queued redirect probe")
    with pytest.raises(ValueError, match="only a RUNNING job can be redirected"):
        redirect_job(db, submitted.job.job_id, MARKER)
    assert get_job(db, submitted.job.job_id).state is JobState.QUEUED  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Surfaces: MCP and CLI
# ---------------------------------------------------------------------------


def test_redirect_and_discard_are_exposed_over_mcp_and_cli(
    rig: RigEnvironment, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Both verbs exist over MCP and the CLI and refuse with clear reasons."""
    from saberops.cli import main
    from saberops.jobs.service import steering_notes, undelivered_steering_notes

    assert "redirect_job" in TOOL_NAMES
    assert "discard_note" in TOOL_NAMES
    db = rig.db()
    submitted = submit_job(db, repo=rig.repo, goal="surface probes")
    job_id = submitted.job.job_id
    registry = _registry(tmp_path, db)

    with pytest.raises(InvalidParamsError, match="only a RUNNING job can be redirected"):
        registry.call("redirect_job", {"job_id": job_id, "text": MARKER})

    note = note_job(db, job_id, MARKER)
    discarded = registry.call("discard_note", {"note_id": note.note_id})
    assert discarded["note"]["discarded"] is True
    assert [item.note_id for item in steering_notes(db, job_id)] == [note.note_id]
    assert undelivered_steering_notes(db, job_id) == ()
    with pytest.raises(InvalidParamsError, match="discard"):
        registry.call("discard_note", {"note_id": note.note_id})

    code = main(["job", "redirect", job_id, "--text", MARKER, "--json"])
    captured = capsys.readouterr()
    assert code == 2
    assert "only a RUNNING job can be redirected" in captured.err

    second = note_job(db, job_id, "second note")
    code = main(["job", "note-discard", str(second.note_id), "--json"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert json.loads(captured.out)["discarded"] is True


def test_catch_up_names_redirect_or_discard_for_a_pending_note(
    rig: RigEnvironment, tmp_path: Path
) -> None:
    """The planner is told what to do about a note that never reached a worker."""
    fixture = build_acceptance_fixture(rig)
    fixture.record_passing_review_for_b()
    job_id = _completed_job_for_run(rig, fixture.run_id, note=MARKER)
    db = rig.db()
    registry = _registry(tmp_path, db)

    caught_up = registry.call("catch_up", {"job_id": job_id})
    assert caught_up["steering_notes"][0]["delivered"] is False
    next_action = caught_up["next_action"]
    assert next_action["action"] == "resolve_steering_note"
    assert "redirect_job" in next_action["detail"]
    assert "discard_note" in next_action["detail"]
    assert next_action["redirect"]["tool"] == "redirect_job"
    assert next_action["discard"]["tool"] == "discard_note"


def test_discard_is_refused_for_a_delivered_note(rig: RigEnvironment) -> None:
    """A note that already reached an attempt is not silently discarded."""
    from saberops.jobs.service import discard_note
    from saberops.jobs.store import mark_steering_notes_delivered

    db = rig.db()
    submitted = submit_job(db, repo=rig.repo, goal="delivered note probe")
    job_id = submitted.job.job_id
    note = note_job(db, job_id, MARKER)
    mark_steering_notes_delivered(
        db, [note.note_id], run_id="run_delivered", attempt_id="run_delivered_a1"
    )
    with pytest.raises(ValueError, match="already delivered"):
        discard_note(db, note.note_id)
