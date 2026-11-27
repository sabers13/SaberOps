"""H50: the owner rejects a succeeded candidate with a note.

The lane wires the existing ``reject_current_candidate`` authority to one MCP
tool and one ``orch`` verb.  Rejecting a succeeded candidate must record the
rejection, keep the candidate unacceptable, and start the resumed attempt with
the note as its resume note (the H47 linked-resume path), so the job finishes.

These tests use the real fake-worker lifecycle rig (real store, toy repository,
real scheduler, detached supervisor, fake CLIs) and the real service, MCP and
CLI seams.  No vendor CLI is launched.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from lifecycle_rig import RigEnvironment, build_lifecycle_rig

from saberops.accept import AcceptEngine
from saberops.candidate_lifecycle import project_candidate_state
from saberops.capacity.loop import run_scheduler_loop
from saberops.cli import main
from saberops.db import Database
from saberops.jobs.models import JobState
from saberops.jobs.service import get_job, note_job, steering_notes, submit_job
from saberops.mcp.errors import ToolError
from saberops.mcp.evidence import EvidenceRoots
from saberops.mcp.lease import OrchestratorLease
from saberops.mcp.ledger import EventLedger
from saberops.mcp.tools import ServerContext, build_default_registry
from saberops.model_access.readiness_service import ReadinessService
from saberops.models import CandidateState, Run
from saberops.paths import owner_state_dir

pytestmark = pytest.mark.packaged_routing_default

MARKER = "H50 rejection: keep the public API unchanged."


@pytest.fixture()
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RigEnvironment:
    return build_lifecycle_rig(tmp_path, monkeypatch)


def _succeed_job(rig: RigEnvironment, goal: str) -> tuple[Database, Any, Run]:
    """Submit, dispatch, run, and reconcile one job to SUCCEEDED."""
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
    lease = OrchestratorLease(rig.root / "h50-lease.json", holder="h50-test", pid=1)
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


def _prompts(path: Path) -> list[str]:
    return [
        json.loads(line)["prompt"]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_reject_candidate_is_exposed_over_mcp() -> None:
    from saberops.mcp.tools import MUTATING_TOOLS, TOOL_NAMES

    assert "reject_candidate" in TOOL_NAMES
    assert "reject_candidate" in MUTATING_TOOLS


def test_mcp_reject_delivers_the_note_and_the_job_finishes(rig: RigEnvironment) -> None:
    db, job, source = _succeed_job(rig, "Add the H50 MCP rejection feature line")
    before = project_candidate_state(db, source.id)
    assert before.state in {CandidateState.READY, CandidateState.REVIEWED}
    prompt_log = rig.root / "h50-mcp-prompts.jsonl"
    rig.set_worker_mode("commit", prompt_log=prompt_log)

    payload = _registry(rig).call(
        "reject_candidate", {"job_id": job.job_id, "note": MARKER}
    )

    assert payload["operation"] == "reject_candidate"
    assert payload["rejected_sha"] == before.candidate_sha
    assert payload["job_id"] == job.job_id
    assert payload["from"] == "last_candidate"
    successor = payload["run_id"]
    assert successor != source.id
    assert db.get_continuation(source.id) == successor

    # The rejection is durable and the candidate can no longer be accepted.
    assert project_candidate_state(db, source.id).state is CandidateState.REJECTED
    with pytest.raises(RuntimeError, match="rejected by the owner"):
        AcceptEngine(db=db).accept_run(source.id)

    run = rig.wait_for_run(successor, timeout=90.0)
    assert run.status.value == "COMPLETED", run.result_summary
    run_scheduler_loop(
        db, once=True, readiness_service=ReadinessService.for_owner_state(owner_state_dir())
    )
    finished = get_job(db, job.job_id)
    assert finished is not None and finished.state is JobState.SUCCEEDED
    assert finished.current_run_id == successor

    prompts = _prompts(prompt_log)
    assert prompts and MARKER in prompts[-1]
    notes = steering_notes(db, job.job_id)
    assert len(notes) == 1
    assert notes[0].body == MARKER
    assert notes[0].delivered is True
    assert notes[0].delivered_run_id == successor


def test_cli_reject_delivers_the_note_and_the_job_finishes(
    rig: RigEnvironment, capsys: pytest.CaptureFixture[str]
) -> None:
    db, job, source = _succeed_job(rig, "Add the H50 CLI rejection feature line")
    prompt_log = rig.root / "h50-cli-prompts.jsonl"
    rig.set_worker_mode("commit", prompt_log=prompt_log)

    code = main(["reject", job.job_id, "--note", MARKER, "--json"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    payload = json.loads(captured.out)
    assert payload["operation"] == "reject_candidate"
    successor = payload["run_id"]
    assert payload["from"] == "last_candidate"
    assert project_candidate_state(db, source.id).state is CandidateState.REJECTED

    run = rig.wait_for_run(successor, timeout=90.0)
    assert run.status.value == "COMPLETED", run.result_summary
    run_scheduler_loop(
        db, once=True, readiness_service=ReadinessService.for_owner_state(owner_state_dir())
    )
    finished = get_job(db, job.job_id)
    assert finished is not None and finished.state is JobState.SUCCEEDED
    prompts = _prompts(prompt_log)
    assert prompts and MARKER in prompts[-1]


def test_reject_refuses_without_any_note(rig: RigEnvironment) -> None:
    db, job, source = _succeed_job(rig, "Add the H50 no-note refusal feature line")

    with pytest.raises(ToolError, match="REJECT_NOTE_REQUIRED"):
        _registry(rig).call("reject_candidate", {"job_id": job.job_id})

    assert project_candidate_state(db, source.id).state is not CandidateState.REJECTED
    unchanged = get_job(db, job.job_id)
    assert unchanged is not None and unchanged.state is JobState.SUCCEEDED


def test_reject_delivers_a_note_already_recorded_on_the_job(rig: RigEnvironment) -> None:
    db, job, source = _succeed_job(rig, "Add the H50 existing-note feature line")
    note_job(db, job.job_id, MARKER, allow_terminal=True)

    payload = _registry(rig).call("reject_candidate", {"job_id": job.job_id})
    successor = payload["run_id"]

    rig.wait_for_run(successor, timeout=90.0)
    notes = steering_notes(db, job.job_id)
    assert [note.body for note in notes] == [MARKER]
    assert notes[0].delivered is True
    assert notes[0].delivered_run_id == successor


def test_reject_refuses_an_already_accepted_candidate(rig: RigEnvironment) -> None:
    db, job, source = _succeed_job(rig, "Add the H50 accepted refusal feature line")
    accepted = AcceptEngine(db=db).accept_run(source.id)
    assert accepted.accepted is True

    with pytest.raises(ToolError, match="REJECT_ACCEPTED"):
        _registry(rig).call(
            "reject_candidate", {"job_id": job.job_id, "note": MARKER}
        )

    projection = project_candidate_state(db, source.id)
    assert projection.state is CandidateState.ACCEPTED
    unchanged = get_job(db, job.job_id)
    assert unchanged is not None and unchanged.current_run_id == source.id
