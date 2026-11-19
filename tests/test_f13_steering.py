"""F13 (ADR-0016): durable steering notes reach a running job's next attempt.

A note is recorded on the job and only ever reaches the worker inside the
prompt of the next attempt that starts, never typed into a running process.
These tests use the real-process lifecycle rig and its fake CLI shims.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest
from lifecycle_rig import RigEnvironment, build_lifecycle_rig

from saberops.capacity.loop import run_scheduler_loop
from saberops.db import Database
from saberops.jobs.service import (
    JobNotFoundError,
    cancel_job,
    get_job,
    job_status_view,
    note_job,
    steering_notes,
    submit_job,
)
from saberops.mcp.errors import InvalidParamsError
from saberops.mcp.evidence import EvidenceRoots
from saberops.mcp.lease import OrchestratorLease
from saberops.mcp.ledger import EventLedger
from saberops.mcp.tools import ServerContext, build_default_registry
from saberops.model_access.readiness_service import ReadinessService
from saberops.models import Attempt, AttemptStatus, RunStatus
from saberops.paths import owner_state_dir

pytestmark = pytest.mark.packaged_routing_default

MARKER = "STEERING-MARKER-4f13: use the existing retry helper; do not add a new one."


@pytest.fixture()
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RigEnvironment:
    return build_lifecycle_rig(tmp_path, monkeypatch)


def _wait_for_first_attempt(
    db: Database, run_id: str, *, timeout: float = 20.0
) -> list[Attempt]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        attempts = db.get_attempts_for_run(run_id)
        if attempts:
            return attempts
        time.sleep(0.05)
    raise AssertionError(f"no attempt started for run {run_id} within {timeout}s")


def _wait_for_logged_prompt(path: Path, *, timeout: float = 20.0) -> None:
    """Wait until attempt 1 has read its mode and logged its prompt.

    The fake CLI resolves its mode at process start and logs the prompt before
    it blocks on the rig's release file, so a log entry proves attempt 1 is
    live with the mode it already read.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file() and path.stat().st_size > 0:
            return
        time.sleep(0.05)
    raise AssertionError(f"no worker prompt logged to {path} within {timeout}s")


def _write_worker_control(
    rig: RigEnvironment,
    mode: str,
    *,
    prompt_log: Path,
    wait_file: Path,
    exit_code: int | None = None,
) -> None:
    """Drive the fake CLI through its control file only.

    The rig's ``set_worker_mode`` also exports ``ORCH_FAKE_WORKER_MODE``; that
    environment value is inherited by the whole detached run, so it cannot
    change between attempts.  This test switches modes mid-run, so the control
    file (read by each worker process at start) is authoritative here.
    """
    rig.monkeypatch.delenv("ORCH_FAKE_WORKER_MODE", raising=False)
    control = rig.state_home / "saberops" / "fake-worker-mode.json"
    control.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "mode": mode,
        "prompt_log": str(prompt_log),
        "wait_file": str(wait_file),
        "wait_timeout_seconds": 45,
    }
    if exit_code is not None:
        payload["exit_code"] = exit_code
    control.write_text(json.dumps(payload), encoding="utf-8")


def test_note_job_validates_and_records_pending(rig: RigEnvironment) -> None:
    """A note on a live job is durable, pending, and refuses invalid input."""
    db = rig.db()
    submitted = submit_job(db, repo=rig.repo, goal="steering note validation")
    job_id = submitted.job.job_id

    note = note_job(db, job_id, MARKER)
    assert note.note_id > 0
    assert note.delivered is False
    recorded = steering_notes(db, job_id)
    assert [item.body for item in recorded] == [MARKER]

    with pytest.raises(ValueError):
        note_job(db, job_id, "   ")
    with pytest.raises(ValueError):
        note_job(db, job_id, "x" * 4001)
    with pytest.raises(JobNotFoundError):
        note_job(db, "job-missing", MARKER)

    cancel_job(db, job_id, run_canceller=lambda _run_id: False)
    with pytest.raises(ValueError, match="no worker attempt remains"):
        note_job(db, job_id, "too late")


def test_notes_read_is_empty_when_the_store_predates_the_table(
    rig: RigEnvironment,
) -> None:
    """A legacy store without the notes table reads as zero notes, not an error."""
    db = rig.db()
    submitted = submit_job(db, repo=rig.repo, goal="legacy notes read")
    job = get_job(db, submitted.job.job_id)
    assert job is not None

    with sqlite3.connect(str(db.owner_db.db_path)) as conn:
        conn.execute("DROP TABLE job_steering_notes")
        conn.commit()

    assert steering_notes(db, job.job_id) == ()
    assert job_status_view(db, job)["steering_notes"] == []


def test_running_job_note_is_delivered_once_at_next_attempt(rig: RigEnvironment) -> None:
    """A note sent while attempt 1 runs appears once, in attempt 2's prompt."""
    db = rig.db()
    prompt_log = rig.root / "worker-prompts.jsonl"
    release_first = rig.root / "release-first-attempt"
    submitted = submit_job(db, repo=rig.repo, goal="Add a steering note feature line")

    # Attempt 1 crashes after the rig releases it, so a second attempt runs.
    _write_worker_control(
        rig, "crash", prompt_log=prompt_log, wait_file=release_first, exit_code=7
    )
    readiness = ReadinessService.for_owner_state(owner_state_dir())
    tick = run_scheduler_loop(db, once=True, readiness_service=readiness)
    assert tick is not None and tick.ran and tick.dispatched == 1

    job = get_job(db, submitted.job.job_id)
    assert job is not None and job.current_run_id is not None
    run_id = job.current_run_id
    first_attempts = _wait_for_first_attempt(db, run_id)
    assert first_attempts[0].status in {AttemptStatus.RUNNING, AttemptStatus.FAILED}
    _wait_for_logged_prompt(prompt_log)

    note = note_job(db, job.job_id, MARKER)

    # The note existed while attempt 1 was live; only the next attempt carries it.
    _write_worker_control(rig, "commit", prompt_log=prompt_log, wait_file=release_first)
    release_first.write_text("go", encoding="utf-8")

    run = rig.wait_for_run(run_id, timeout=60.0)
    assert run.status == RunStatus.COMPLETED, run.result_summary

    attempts = db.get_attempts_for_run(run_id)
    assert len(attempts) >= 2, [a.status for a in attempts]
    delivered_attempt = attempts[1]

    prompts = [
        json.loads(line)["prompt"]
        for line in prompt_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert prompts, "the fake worker recorded no prompt"
    assert sum(prompt.count(MARKER) for prompt in prompts) == 1
    assert MARKER in prompts[-1]

    recorded = steering_notes(db, job.job_id)
    assert len(recorded) == 1
    assert recorded[0].note_id == note.note_id
    assert recorded[0].delivered is True
    assert recorded[0].delivered_run_id == run_id
    assert recorded[0].delivered_attempt_id == delivered_attempt.id

    events = db.get_complete_events_for_run(run_id)
    delivery = [
        event
        for event in events
        if event.event_type == "steering_notes_delivered"
        and event.attempt_id == delivered_attempt.id
    ]
    assert len(delivery) == 1
    assert delivery[0].payload is not None
    assert delivery[0].payload["note_ids"] == [note.note_id]

    # catch_up shows the same single delivery from a fresh read-only context.
    context = ServerContext(
        db_path=db.db_path,
        questions_store=rig.root / "questions.json",
        config_ledger_path=rig.root / "config.jsonl",
        evidence=EvidenceRoots(roots=(rig.root,)),
        ledger=EventLedger(rig.root / "events.jsonl"),
        lease=OrchestratorLease(rig.root / "lease.json"),
    )
    registry = build_default_registry(context)
    caught_up = registry.call("catch_up", {"job_id": job.job_id})
    notes_view = caught_up["steering_notes"]
    assert len(notes_view) == 1
    assert notes_view[0]["text"] == MARKER
    assert notes_view[0]["delivered"] is True
    assert notes_view[0]["delivered_attempt_id"] == delivered_attempt.id


def test_note_job_over_mcp_then_catch_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The MCP note_job tool records a pending note that catch_up reports."""
    rig = build_lifecycle_rig(tmp_path, monkeypatch)
    db = rig.db()
    submitted = submit_job(db, repo=rig.repo, goal="mcp steering note")
    job_id = submitted.job.job_id
    lease = OrchestratorLease(tmp_path / "lease.json", holder="test", pid=1)
    lease.acquire()
    context = ServerContext(
        db_path=db.db_path,
        questions_store=tmp_path / "questions.json",
        config_ledger_path=tmp_path / "config.jsonl",
        evidence=EvidenceRoots(roots=(tmp_path,)),
        ledger=EventLedger(tmp_path / "events.jsonl"),
        lease=lease,
    )
    registry = build_default_registry(context)

    payload = registry.call("note_job", {"job_id": job_id, "text": MARKER})
    assert payload["job_id"] == job_id
    assert payload["note"]["delivered"] is False
    assert "next attempt" in payload["delivery"]

    caught_up = registry.call("catch_up", {"job_id": job_id})
    assert [note["text"] for note in caught_up["steering_notes"]] == [MARKER]
    assert caught_up["steering_notes"][0]["delivered"] is False

    with pytest.raises(InvalidParamsError):
        registry.call("note_job", {"job_id": job_id, "text": "   "})
