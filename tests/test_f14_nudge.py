"""F14: one automatic nudge before a live but idle job needs attention.

The engine's soft-inactivity callback is the live-but-idle observation point.
Behind ``ORCH_NUDGE_IDLE_WORKERS`` it records one bounded continue note through
the steering-note channel (F13/ADR-0016) and keeps the job running; the next
stall for the same job goes straight to needs-attention.  With the flag off, or
for a run that is not a dispatched job, nothing changes.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from lifecycle_rig import RigEnvironment, build_lifecycle_rig

from saberops.capacity.loop import run_scheduler_loop
from saberops.db import Database, current_iso_timestamp
from saberops.jobs import store as job_store
from saberops.jobs.models import JobState
from saberops.jobs.service import get_job, job_status_view, steering_notes, submit_job
from saberops.model_access.readiness_service import ReadinessService
from saberops.models import (
    AttemptStatus,
    HealthState,
    ManagerState,
    OrchEvent,
    Run,
    RunStatus,
    SupervisionRecord,
    Tier,
)
from saberops.nudge import NUDGE_FLAG_ENV, NUDGE_NOTE_BODY, nudging_enabled
from saberops.orchestrator import Orchestrator
from saberops.paths import owner_state_dir

pytestmark = pytest.mark.packaged_routing_default


@pytest.fixture()
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RigEnvironment:
    return build_lifecycle_rig(tmp_path, monkeypatch)


def _live_job(rig: RigEnvironment) -> tuple[Database, str, str, str]:
    """A RUNNING job bound to a RUNNING run with a live supervision record."""
    db = rig.db()
    job = submit_job(db, repo=rig.repo, goal="idle job for the nudge lane").job
    run_id = f"run-f14-{job.job_id.rsplit('-', 1)[-1]}"
    now = current_iso_timestamp()
    db.create_run(
        Run(
            id=run_id,
            task="idle job for the nudge lane",
            target_repo=str(rig.repo),
            base_commit=rig.base_sha,
            tier=Tier.T1,
            training_allowed=False,
            gate_command=rig.gate_command,
            status=RunStatus.RUNNING,
            created_at=now,
        )
    )
    job_store.mark_dispatched(db, job.job_id, run_id)
    attempt_id = f"att-{run_id}"
    db.upsert_supervision(
        SupervisionRecord(
            run_id=run_id,
            supervisor_identity="test-owner",
            supervisor_state="attached",
            worker_pid=None,
            provider="codex",
            model="gpt-5.6-terra",
            started_at=now,
            last_activity_at=now,
            health_state=HealthState.HEALTHY,
            health_reason="monitor_active",
            current_package_id=None,
            current_attempt_id=attempt_id,
            deadline_at=None,
            transcript_path=None,
            wake_condition=None,
            manager_state=ManagerState.ATTACHED,
            updated_at=now,
        )
    )
    return db, job.job_id, run_id, attempt_id


def _idle_hook(
    db: Database, run_id: str, attempt_id: str
) -> tuple[Callable[[float], None], Callable[[], None]]:
    """The engine's own soft-inactivity callback for one live attempt."""
    hooks = Orchestrator(db=db)._worker_activity_hooks(
        run_id, attempt_id, "codex", "gpt-5.6-terra"
    )
    on_stdout, on_stderr, on_stall, on_resume = hooks
    return on_stall, on_resume


def _nudge_events(db: Database, run_id: str) -> list[OrchEvent]:
    return db.get_events_by_type(run_id, "job_nudged")


def test_nudging_is_off_unless_the_flag_is_truthy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(NUDGE_FLAG_ENV, raising=False)
    assert nudging_enabled() is False
    for raw in ("0", "off", "false", "", "  "):
        monkeypatch.setenv(NUDGE_FLAG_ENV, raw)
        assert nudging_enabled() is False
    for raw in ("1", "true", "YES", "on"):
        monkeypatch.setenv(NUDGE_FLAG_ENV, raw)
        assert nudging_enabled() is True


def test_record_nudge_note_spends_the_single_nudge(
    rig: RigEnvironment,
) -> None:
    """The store lets exactly one automatic note through per job."""
    db, job_id, run_id, attempt_id = _live_job(rig)

    first = job_store.record_nudge_note(
        db, job_id, run_id=run_id, attempt_id=attempt_id, body=NUDGE_NOTE_BODY
    )
    second = job_store.record_nudge_note(
        db, job_id, run_id=run_id, attempt_id=attempt_id, body="another continue"
    )

    assert first is not None
    assert second is None
    assert [note.body for note in steering_notes(db, job_id)] == [NUDGE_NOTE_BODY]
    with pytest.raises(ValueError):
        job_store.record_nudge_note(
            db, "other-job", run_id=run_id, body="x" * 4001
        )


def test_flag_off_keeps_attention_and_writes_no_note(
    rig: RigEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the flag the idle worker still goes straight to attention."""
    monkeypatch.delenv(NUDGE_FLAG_ENV, raising=False)
    db, job_id, run_id, attempt_id = _live_job(rig)
    on_stall, _ = _idle_hook(db, run_id, attempt_id)

    on_stall(301.0)

    supervision = db.get_supervision(run_id)
    assert supervision is not None
    assert supervision.manager_state is ManagerState.ATTENTION_REQUIRED
    assert supervision.health_state is HealthState.UNCERTAIN
    assert steering_notes(db, job_id) == ()
    assert _nudge_events(db, run_id) == []
    job = get_job(db, job_id)
    assert job is not None
    assert job_status_view(db, job)["activity"] == "needs_attention"
    stalled = db.get_events_by_type(run_id, "worker_stalled")
    assert len(stalled) == 1
    assert stalled[0].payload is not None
    assert stalled[0].payload["nudged"] is False


def test_first_stall_nudges_once_and_keeps_the_job_running(
    rig: RigEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first live-but-idle stall spends the one nudge instead of attention."""
    monkeypatch.setenv(NUDGE_FLAG_ENV, "1")
    db, job_id, run_id, attempt_id = _live_job(rig)
    on_stall, _ = _idle_hook(db, run_id, attempt_id)

    on_stall(301.0)

    notes = steering_notes(db, job_id)
    assert [note.body for note in notes] == [NUDGE_NOTE_BODY]
    assert notes[0].delivered is False

    events = _nudge_events(db, run_id)
    assert len(events) == 1
    assert events[0].payload is not None
    assert events[0].payload["note_id"] == notes[0].note_id
    assert events[0].payload["job_id"] == job_id
    assert events[0].payload["delivery"] == "next_attempt"

    job_events = [event for event in job_store.list_events(db, job_id) if event["kind"] == "nudged"]
    assert len(job_events) == 1

    supervision = db.get_supervision(run_id)
    assert supervision is not None
    assert supervision.manager_state is ManagerState.ATTACHED
    assert supervision.health_state is HealthState.UNCERTAIN
    assert supervision.health_reason is not None
    assert "one automatic nudge sent" in supervision.health_reason

    job = get_job(db, job_id)
    assert job is not None
    assert job_status_view(db, job)["activity"] == "running"
    stalled = db.get_events_by_type(run_id, "worker_stalled")
    assert len(stalled) == 1
    assert stalled[0].payload is not None
    assert stalled[0].payload["nudged"] is True


def test_second_stall_goes_straight_to_attention(
    rig: RigEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a resume, the next stall never nudges again."""
    monkeypatch.setenv(NUDGE_FLAG_ENV, "1")
    db, job_id, run_id, attempt_id = _live_job(rig)
    on_stall, on_resume = _idle_hook(db, run_id, attempt_id)

    on_stall(301.0)
    on_resume()
    on_stall(305.0)

    assert len(steering_notes(db, job_id)) == 1
    assert len(_nudge_events(db, run_id)) == 1
    supervision = db.get_supervision(run_id)
    assert supervision is not None
    assert supervision.manager_state is ManagerState.ATTENTION_REQUIRED
    job = get_job(db, job_id)
    assert job is not None
    assert job_status_view(db, job)["activity"] == "needs_attention"
    stalled = db.get_events_by_type(run_id, "worker_stalled")
    assert [event.payload["nudged"] for event in stalled if event.payload] == [True, False]


def test_ordinary_run_without_a_job_is_never_nudged(
    rig: RigEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-job run keeps today's attention behaviour even with the flag on."""
    monkeypatch.setenv(NUDGE_FLAG_ENV, "1")
    db = rig.db()
    now = current_iso_timestamp()
    run_id = "run-f14-no-job"
    db.create_run(
        Run(
            id=run_id,
            task="plain run",
            target_repo=str(rig.repo),
            base_commit=rig.base_sha,
            tier=Tier.T1,
            training_allowed=False,
            gate_command=rig.gate_command,
            status=RunStatus.RUNNING,
            created_at=now,
        )
    )
    attempt_id = "att-run-f14-no-job"
    db.upsert_supervision(
        SupervisionRecord(
            run_id=run_id,
            supervisor_identity="test-owner",
            supervisor_state="attached",
            worker_pid=None,
            provider="codex",
            model="gpt-5.6-terra",
            started_at=now,
            last_activity_at=now,
            health_state=HealthState.HEALTHY,
            health_reason="monitor_active",
            current_package_id=None,
            current_attempt_id=attempt_id,
            deadline_at=None,
            transcript_path=None,
            wake_condition=None,
            manager_state=ManagerState.ATTACHED,
            updated_at=now,
        )
    )
    on_stall, _ = _idle_hook(db, run_id, attempt_id)

    on_stall(301.0)

    assert _nudge_events(db, run_id) == []
    supervision = db.get_supervision(run_id)
    assert supervision is not None
    assert supervision.manager_state is ManagerState.ATTENTION_REQUIRED


def test_nudge_is_refused_for_a_finished_run(
    rig: RigEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a RUNNING run can be nudged; a terminal run raises attention."""
    monkeypatch.setenv(NUDGE_FLAG_ENV, "1")
    db, job_id, run_id, attempt_id = _live_job(rig)
    db.update_run(
        run_id,
        RunStatus.COMPLETED,
        completed_at=current_iso_timestamp(),
        result_summary="finished",
    )
    on_stall, _ = _idle_hook(db, run_id, attempt_id)

    on_stall(301.0)

    assert steering_notes(db, job_id) == ()
    assert _nudge_events(db, run_id) == []


def _write_worker_control(
    rig: RigEnvironment, mode: str, *, prompt_log: Path, wait_file: Path
) -> None:
    rig.monkeypatch.delenv("ORCH_FAKE_WORKER_MODE", raising=False)
    control = rig.state_home / "saberops" / "fake-worker-mode.json"
    control.parent.mkdir(parents=True, exist_ok=True)
    control.write_text(
        json.dumps(
            {
                "mode": mode,
                "prompt_log": str(prompt_log),
                "wait_file": str(wait_file),
                "wait_timeout_seconds": 45,
                "exit_code": 7,
            }
        ),
        encoding="utf-8",
    )


def test_nudged_idle_worker_resumes_and_receives_the_continue(
    rig: RigEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: a live idle job is nudged once and its next attempt carries it."""
    monkeypatch.setenv(NUDGE_FLAG_ENV, "1")
    db = rig.db()
    prompt_log = rig.root / "worker-prompts.jsonl"
    release = rig.root / "release-idle-attempt"
    submitted = submit_job(db, repo=rig.repo, goal="Add the idle-resume feature line")

    # Attempt 1 starts, logs its prompt, then blocks silently on the release
    # file: a live worker with no output, which is the F14 trigger.
    _write_worker_control(rig, "crash", prompt_log=prompt_log, wait_file=release)
    readiness = ReadinessService.for_owner_state(owner_state_dir())
    tick = run_scheduler_loop(db, once=True, readiness_service=readiness)
    assert tick is not None and tick.ran and tick.dispatched == 1

    job = get_job(db, submitted.job.job_id)
    assert job is not None and job.current_run_id is not None
    run_id = job.current_run_id
    deadline = time.monotonic() + 20.0
    attempt_id = ""
    while time.monotonic() < deadline:
        attempts = db.get_attempts_for_run(run_id)
        if attempts:
            attempt_id = attempts[0].id
            break
        time.sleep(0.05)
    assert attempt_id
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline and not (
        prompt_log.is_file() and prompt_log.stat().st_size > 0
    ):
        time.sleep(0.05)
    assert prompt_log.is_file() and prompt_log.stat().st_size > 0

    on_stall, _ = _idle_hook(db, run_id, attempt_id)
    on_stall(301.0)
    assert len(steering_notes(db, job.job_id)) == 1

    # Attempt 1 (already resolved to crash) fails; attempt 2 reads "commit" and
    # carries the pending continue note in its prompt.
    _write_worker_control(rig, "commit", prompt_log=prompt_log, wait_file=release)
    release.write_text("go", encoding="utf-8")

    run = rig.wait_for_run(run_id, timeout=90.0)
    assert run.status == RunStatus.COMPLETED, run.result_summary

    from saberops.jobs.service import reconcile_jobs

    def run_status(run_id_: str) -> RunStatus | None:
        stored = db.get_run(run_id_)
        return stored.status if stored is not None else None

    reconcile_jobs(db, run_status=run_status)

    attempts = db.get_attempts_for_run(run_id)
    assert len(attempts) >= 2, [attempt.status for attempt in attempts]
    assert attempts[1].status is AttemptStatus.SUCCESS
    prompts = [
        json.loads(line)["prompt"]
        for line in prompt_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert prompts
    assert sum(prompt.count(NUDGE_NOTE_BODY) for prompt in prompts) == 1
    assert NUDGE_NOTE_BODY in prompts[-1]

    notes = steering_notes(db, job.job_id)
    assert len(notes) == 1
    assert notes[0].delivered is True
    assert notes[0].delivered_attempt_id == attempts[1].id
    assert len(_nudge_events(db, run_id)) == 1
    final_job = get_job(db, submitted.job.job_id)
    assert final_job is not None
    assert final_job.state is JobState.SUCCEEDED


# ---------------------------------------------------------------------------
# B3 D19: healthy lanes never flash needs-attention on a transient projection
# ---------------------------------------------------------------------------


def test_transient_monitor_attention_is_not_owner_attention(
    rig: RigEnvironment,
) -> None:
    """QUIET/AMBIGUOUS_BUSY/TERMINATING are continuing states, not attention.

    The monitor writes UNCERTAIN + ATTENTION_REQUIRED while a live worker is
    between output samples or terminating.  Reading that as owner attention is
    what made healthy lanes flash ``needs-attention`` for 1-3 s before their
    candidate landed.
    """
    db, job_id, run_id, _attempt_id = _live_job(rig)
    assert db.update_supervision_health(
        run_id,
        health_state=HealthState.UNCERTAIN,
        health_reason="attention: ambiguous_busy continuing (not stall-confirmed)",
        manager_state=ManagerState.ATTENTION_REQUIRED,
    )
    job = get_job(db, job_id)
    assert job is not None
    assert job_status_view(db, job)["activity"] == "running"


def test_worker_stall_attention_still_surfaces(rig: RigEnvironment) -> None:
    """The durable output-quiet stall remains the owner-attention path."""
    db, job_id, run_id, attempt_id = _live_job(rig)
    on_stall, _on_resume = _idle_hook(db, run_id, attempt_id)
    on_stall(301.0)

    job = get_job(db, job_id)
    assert job is not None
    assert job_status_view(db, job)["activity"] == "needs_attention"


def test_terminal_run_handoff_never_flashes_attention(
    rig: RigEnvironment,
) -> None:
    """A released owner after a terminal run is normal handoff bookkeeping."""
    import os

    from saberops.models import RunStatus
    from saberops.ownership import ProcessIdentity

    db, job_id, run_id, _attempt_id = _live_job(rig)
    identity = ProcessIdentity.for_pid(os.getpid())
    assert identity is not None
    owner = db.claim_execution_owner(run_id, identity=identity)
    db.update_run(run_id, RunStatus.COMPLETED)
    assert db.release_execution_owner(
        run_id, owner_id=owner.owner_id, generation=owner.generation
    )

    job = get_job(db, job_id)
    assert job is not None and job.state is JobState.RUNNING
    assert job_status_view(db, job)["activity"] == "running"


def test_dead_owner_on_a_running_run_still_needs_attention(
    rig: RigEnvironment,
) -> None:
    """The safety hold itself is not weakened by the transient-state fix."""
    import os

    from saberops.ownership import ProcessIdentity

    db, job_id, run_id, _attempt_id = _live_job(rig)
    identity = ProcessIdentity.for_pid(os.getpid())
    assert identity is not None
    owner = db.claim_execution_owner(run_id, identity=identity)
    assert db.release_execution_owner(
        run_id, owner_id=owner.owner_id, generation=owner.generation
    )

    job = get_job(db, job_id)
    assert job is not None
    view = job_status_view(db, job)
    assert view["activity"] == "needs_attention"
    assert view["reason"] == "execution owner is dead; worker safety is being checked"
