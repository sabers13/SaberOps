"""H55: a rejection while work is paused records nothing.

``reject_candidate`` used to write the rejection and the note, then call the
owner-resume-gated resume path; with the work gate paused it returned
``RESUME_PAUSED`` and left the job ``SUCCEEDED``, the candidate ``REJECTED``, a
pending note and no successor.  ADR-0113 makes the gate part of the refusal:
nothing is written and the reply says what the owner does next.

Real fake-worker lifecycle rig (real store, toy repository, real scheduler and
fake CLIs); no vendor CLI is launched.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from lifecycle_rig import RigEnvironment, build_lifecycle_rig

from saberops.candidate_lifecycle import project_candidate_state
from saberops.capacity.loop import run_scheduler_loop
from saberops.cli import main
from saberops.db import Database
from saberops.jobs.models import JobState
from saberops.jobs.service import get_job, steering_notes, submit_job
from saberops.mcp.errors import ToolError
from saberops.mcp.evidence import EvidenceRoots
from saberops.mcp.lease import OrchestratorLease
from saberops.mcp.ledger import EventLedger
from saberops.mcp.tools import ServerContext, build_default_registry
from saberops.model_access.readiness_service import ReadinessService
from saberops.models import CandidateState, Run
from saberops.paths import owner_state_dir
from saberops.runtime_gate import PauseReason, clear_pause, set_pause

pytestmark = pytest.mark.packaged_routing_default

MARKER = "H55 rejection: keep the public API unchanged."


@pytest.fixture()
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RigEnvironment:
    return build_lifecycle_rig(tmp_path, monkeypatch)


def _succeed_job(rig: RigEnvironment, goal: str) -> tuple[Database, Any, Run]:
    db = rig.db()
    submitted = submit_job(db, repo=rig.repo, goal=goal)
    readiness = ReadinessService.for_owner_state(owner_state_dir())
    tick = run_scheduler_loop(db, once=True, readiness_service=readiness)
    assert tick is not None and tick.ran and tick.dispatched == 1

    job = get_job(db, submitted.job.job_id)
    assert job is not None and job.current_run_id is not None
    run = rig.wait_for_run(job.current_run_id, timeout=90.0)
    assert run.status.value == "COMPLETED", run.result_summary

    run_scheduler_loop(db, once=True, readiness_service=readiness)
    job = get_job(db, submitted.job.job_id)
    assert job is not None and job.state is JobState.SUCCEEDED
    return db, job, run


def _registry(rig: RigEnvironment) -> Any:
    db = rig.db()
    lease = OrchestratorLease(rig.root / "h55-lease.json", holder="h55-test", pid=1)
    lease.acquire()
    context = ServerContext(
        db_path=db.db_path,
        questions_store=rig.root / "questions.json",
        config_ledger_path=rig.root / "config.jsonl",
        evidence=EvidenceRoots(roots=(rig.root,)),
        ledger=EventLedger(rig.root / "events.jsonl"),
        lease=lease,
    )
    return build_default_registry(context)


def test_reject_while_paused_records_nothing_and_works_after_resume(
    rig: RigEnvironment, capsys: pytest.CaptureFixture[str]
) -> None:
    db, job, source = _succeed_job(rig, "Add the H55 paused rejection feature line")
    before = project_candidate_state(db, source.id)
    assert before.state is not None and before.candidate_sha is not None
    prompt_log = rig.root / "h55-prompts.jsonl"
    rig.set_worker_mode("commit", prompt_log=prompt_log)
    set_pause(PauseReason.NO_PLANNING_SESSION, "paused: no planning session")

    registry = _registry(rig)
    with pytest.raises(ToolError, match="REJECT_PAUSED"):
        registry.call("reject_candidate", {"job_id": job.job_id, "note": MARKER})

    # A clean refusal: the note, the rejection and the job are all untouched.
    after = project_candidate_state(db, source.id)
    assert (after.state, after.candidate_sha) == (before.state, before.candidate_sha)
    unchanged = get_job(db, job.job_id)
    assert unchanged is not None and unchanged.state is JobState.SUCCEEDED
    assert unchanged.current_run_id == source.id
    assert steering_notes(db, job.job_id) == ()
    assert db.get_continuation(source.id) is None

    # The owner's CLI verb refuses the same way and names the next action.
    assert main(["reject", job.job_id, "--note", MARKER, "--json"]) == 1
    captured = capsys.readouterr()
    assert "REJECT_PAUSED" in captured.err
    assert "orch resume" in captured.err
    assert steering_notes(db, job.job_id) == ()

    # After the owner resumes, the same rejection records and corrects.
    clear_pause()
    payload = registry.call("reject_candidate", {"job_id": job.job_id, "note": MARKER})
    successor = payload["run_id"]
    assert project_candidate_state(db, source.id).state is CandidateState.REJECTED

    run = rig.wait_for_run(successor, timeout=90.0)
    assert run.status.value == "COMPLETED", run.result_summary
    notes = steering_notes(db, job.job_id)
    assert [note.body for note in notes] == [MARKER]
    assert notes[0].delivered_run_id == successor
