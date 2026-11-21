"""Phase 1 F17: closing the planning CLI always pauses the work.

The owner's safety rule: SaberOps runs only while at least one planning
session that turned SaberOps on is connected.  Closing that session -- cleanly,
by a killed CLI, or by a lost heartbeat -- stops new dispatch and parks every
running worker with its worktree, checkpoint and evidence intact, in every
owner mode.  Nothing resumes without an explicit confirmation.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from lifecycle_rig import RigEnvironment, build_lifecycle_rig

from saberops.authority import set_authority_mode
from saberops.cli import main
from saberops.jobs.models import JobState, WaitingReason
from saberops.jobs.service import get_job, submit_job
from saberops.mcp.errors import ForbiddenOperationError
from saberops.mcp.lease import LeaseRecord, OrchestratorLease
from saberops.mcp.tools import (
    TOOL_NAMES,
    ServerContext,
    _catch_up,
    _get_pause,
    build_default_registry,
)
from saberops.model_access.readiness_service import ReadinessService
from saberops.models import RunStatus
from saberops.owner_mode import (
    OwnerModeError,
    clear_owner_mode,
    load_owner_mode_settings,
    set_owner_mode,
)
from saberops.paths import get_runtime_gate_path, owner_state_dir
from saberops.pause_work import resume_work
from saberops.runtime_gate import (
    PauseReason,
    evaluate_gate,
    mark_presence_known,
    maybe_send_digest,
    presence,
    read_gate,
    refresh_gate,
    set_pause,
)

pytestmark = [
    pytest.mark.packaged_routing_default,
    # F24: this file drives presence itself; never hold a test terminal.
    pytest.mark.no_attendance,
]

_SETUP_TIMEOUT = 180


@pytest.fixture()
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[RigEnvironment]:
    # F24: this file is about presence *ending*, so the rig must not hold an
    # attended terminal of its own.
    environment = build_lifecycle_rig(tmp_path, monkeypatch, attended=False)
    # The lifecycle rig already pins HOME/XDG under tmp_path; the gate document
    # must live there too.
    monkeypatch.delenv("SABEROPS_RUNTIME_GATE", raising=False)
    monkeypatch.setenv("SABEROPS_PLANNING_HEARTBEAT_TIMEOUT", "60")
    yield environment
    try:
        OrchestratorLease().release()
    except Exception:
        pass


def _wait(predicate: Callable[[], bool], timeout: float = _SETUP_TIMEOUT) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.2)
    pytest.fail(f"condition {predicate.__name__} did not become true within {timeout:g}s")


def _tick(rig: RigEnvironment) -> Any:
    from saberops.capacity.loop import run_scheduler_loop

    readiness = ReadinessService.for_owner_state(owner_state_dir())
    return run_scheduler_loop(rig.db(), once=True, readiness_service=readiness)


def _live_session(rig: RigEnvironment, monkeypatch: pytest.MonkeyPatch) -> OrchestratorLease:
    """Simulate one connected planning session that turned SaberOps on."""
    lease = OrchestratorLease()
    lease.configure_database(Path(rig.db().db_path))
    assert lease.acquire() is True
    lease.touch()
    mark_presence_known()
    monkeypatch.setattr(
        "saberops.notifications.notify_once", lambda *args, **kwargs: None, raising=False
    )
    return lease


def _close_clean(lease: OrchestratorLease) -> None:
    assert lease.release() is True


def _close_killed(rig: RigEnvironment) -> None:
    """A CLI killed with the planning lease still recorded (dead pid)."""
    path = OrchestratorLease().path
    record = LeaseRecord(
        holder="orch-pid-999999",
        pid=999_999,
        acquired_at=datetime.now(UTC).isoformat(),
        session_id="dead-session",
        epoch=1,
        last_activity=time.time(),
        heartbeat_at=time.time(),
        db_path=str(rig.db().db_path),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record.to_mapping()) + "\n", encoding="utf-8")


def _close_lost_heartbeat(rig: RigEnvironment) -> None:
    """A live pid whose heartbeat stopped (crashed CLI / lost connection)."""
    path = OrchestratorLease().path
    stale = time.time() - 3600
    record = LeaseRecord(
        holder=f"orch-pid-{__import__('os').getpid()}",
        pid=__import__("os").getpid(),
        acquired_at=datetime.now(UTC).isoformat(),
        session_id="silent-session",
        epoch=1,
        last_activity=stale,
        heartbeat_at=stale,
        db_path=str(rig.db().db_path),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record.to_mapping()) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# Presence
# --------------------------------------------------------------------------- #


def test_presence_requires_a_registered_session(rig: RigEnvironment) -> None:
    """F24: a fresh store with no session is closed but never sticky."""
    assert presence().live is False
    fresh = evaluate_gate(db=rig.db())
    assert fresh.closed is True
    assert fresh.reason == PauseReason.NO_PLANNING_SESSION.value
    assert fresh.transient is True
    # No session was ever live, so nothing was interrupted and no pause is
    # written by the engine.
    refresh_gate(rig.db())
    assert read_gate().pause is None

    mark_presence_known()
    state = evaluate_gate(db=rig.db())
    assert state.closed is True
    assert state.reason == PauseReason.NO_PLANNING_SESSION.value
    assert state.transient is False
    refresh_gate(rig.db())
    assert read_gate().pause is not None


def test_live_session_is_seen_through_its_heartbeat(
    rig: RigEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease = _live_session(rig, monkeypatch)
    assert evaluate_gate(db=rig.db()).closed is False
    assert presence().live is True
    with lease.path.open("a", encoding="utf-8"):
        pass


# --------------------------------------------------------------------------- #
# New dispatch stops in every mode
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("mode", ["ask-me", "critical-only", "away"])
@pytest.mark.parametrize("closing", ["clean", "killed", "heartbeat"])
def test_closing_the_last_session_stops_new_dispatch(
    rig: RigEnvironment,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    closing: str,
) -> None:
    set_authority_mode("autonomous")
    clear_owner_mode()
    if mode == "away":
        set_owner_mode("away", max_jobs=10)
    else:
        set_owner_mode(mode)
    lease = _live_session(rig, monkeypatch)
    assert evaluate_gate(db=rig.db()).closed is False

    if closing == "clean":
        _close_clean(lease)
    elif closing == "killed":
        _close_killed(rig)
    else:
        _close_lost_heartbeat(rig)

    db = rig.db()
    submitted = submit_job(db, repo=rig.repo, goal=f"paused in {mode}").job
    result = _tick(rig)
    assert result is not None
    assert result.ran is True
    assert result.reason == "paused"
    assert result.pause_reason == PauseReason.NO_PLANNING_SESSION.value

    current = get_job(db, submitted.job_id)
    assert current is not None
    assert current.state is JobState.QUEUED
    assert current.current_run_id is None

    record = read_gate()
    assert record.pause is not None
    assert record.pause.reason == PauseReason.NO_PLANNING_SESSION.value
    # Exactly one pause record: a repeated tick keeps the original timestamp.
    since = record.pause.since
    _tick(rig)
    repeated = read_gate().pause
    assert repeated is not None
    assert repeated.since == since


def test_nothing_resumes_without_confirmation(
    rig: RigEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_authority_mode("autonomous")
    lease = _live_session(rig, monkeypatch)
    db = rig.db()
    submitted = submit_job(db, repo=rig.repo, goal="resume needs confirmation").job
    _close_clean(lease)
    _tick(rig)
    assert get_job(db, submitted.job_id).state is JobState.QUEUED  # type: ignore[union-attr]

    # A returning session alone never resumes anything.
    returning = _live_session(rig, monkeypatch)
    _tick(rig)
    assert get_job(db, submitted.job_id).state is JobState.QUEUED  # type: ignore[union-attr]
    state = evaluate_gate(db=db)
    assert state.closed is True
    assert state.reason == PauseReason.NO_PLANNING_SESSION.value

    # One explicit owner confirmation does; the planning session cannot.
    resumed = resume_work(db, by="owner")
    assert resumed.closed is False

    def redispatch() -> bool:
        _tick(rig)
        current = get_job(db, submitted.job_id)
        assert current is not None
        return bool(current.current_run_id) or current.state in (
            JobState.SUCCEEDED,
            JobState.FAILED,
        )

    _wait(redispatch)
    assert returning.writer is True


def test_owner_pause_pauses_immediately(
    rig: RigEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_authority_mode("autonomous")
    _live_session(rig, monkeypatch)
    db = rig.db()
    submit_job(db, repo=rig.repo, goal="owner pause")
    assert main(["pause", "--json"]) == 0
    state = evaluate_gate(db=db)
    assert state.closed is True
    assert state.reason == PauseReason.OWNER.value
    assert main(["resume", "--json"]) == 0
    assert evaluate_gate(db=db).closed is False


# --------------------------------------------------------------------------- #
# Running workers are parked, not discarded
# --------------------------------------------------------------------------- #


def test_running_worker_is_parked_with_work_preserved(
    rig: RigEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_authority_mode("autonomous")
    _live_session(rig, monkeypatch)
    rig.set_worker_mode("hang", hang_seconds=180)
    db = rig.db()
    submitted = submit_job(db, repo=rig.repo, goal="hold the work").job

    def dispatched() -> bool:
        _tick(rig)
        current = get_job(db, submitted.job_id)
        return bool(current and current.current_run_id)

    _wait(dispatched)
    job = get_job(db, submitted.job_id)
    assert job is not None and job.current_run_id
    run_id = job.current_run_id

    def worker_started() -> bool:
        from saberops.supervisor import RunSupervisor

        owner, _ = RunSupervisor(db=db).owner_evidence(run_id)
        sup = db.get_supervision(run_id)
        return bool(owner and sup and sup.worker_pid and sup.worker_identity)

    _wait(worker_started)
    attempt = db.get_attempts_for_run(run_id)[-1]
    worktree = Path(attempt.worktree_path)
    assert worktree.exists()
    marker = worktree / "unfinished-work.txt"
    marker.write_text("preserved\n", encoding="utf-8")

    # The planning CLI goes away; the gate closes and the worker is stopped.
    set_pause(PauseReason.NO_PLANNING_SESSION, "lost the planning session")
    result = _tick(rig)
    assert result is not None and result.ran is True
    assert result.reason == "paused"

    def parked_job() -> bool:
        _tick(rig)  # a closed gate keeps retrying until the worker is stopped
        current = get_job(db, submitted.job_id)
        return current is not None and current.state is JobState.QUEUED

    _wait(parked_job)
    parked = get_job(db, submitted.job_id)
    assert parked is not None
    assert parked.waiting_reason is WaitingReason.PAUSED
    assert parked.resume_from is not None
    assert parked.resume_from.run_id == run_id
    assert parked.preemptions == 0  # a pause never consumes the preemption budget
    run = db.get_run(run_id)
    assert run is not None and run.status is RunStatus.PREEMPTED
    assert marker.read_text(encoding="utf-8") == "preserved\n"
    assert worktree.exists()
    assert db.get_worker_checkpoints_for_run(run_id)


def test_paused_job_redispatches_from_its_checkpoint(
    rig: RigEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_authority_mode("autonomous")
    _live_session(rig, monkeypatch)
    rig.set_worker_mode("hang", hang_seconds=180)
    db = rig.db()
    submitted = submit_job(db, repo=rig.repo, goal="resume from checkpoint").job

    def dispatched() -> bool:
        _tick(rig)
        current = get_job(db, submitted.job_id)
        return bool(current and current.current_run_id)

    _wait(dispatched)
    first_run = get_job(db, submitted.job_id).current_run_id  # type: ignore[union-attr]
    set_pause(PauseReason.OWNER, "owner stop")

    def parked() -> bool:
        _tick(rig)  # a closed gate parks on every tick until the worker stops
        current = get_job(db, submitted.job_id)
        return current is not None and current.waiting_reason is WaitingReason.PAUSED

    _wait(parked)

    rig.set_worker_mode("commit", target_file="done.txt", content="ok\n")
    resume_work(db, by="owner")

    def redispatch() -> bool:
        _tick(rig)
        current = get_job(db, submitted.job_id)
        assert current is not None
        return current.current_run_id not in (None, first_run)

    _wait(redispatch, timeout=240)
    resumed = get_job(db, submitted.job_id)
    assert resumed is not None
    assert resumed.current_run_id is not None
    assert resumed.current_run_id != first_run
    # Let the resumed run finish so the test leaves no live worker behind.
    rig.wait_for_run(resumed.current_run_id, timeout=240)


# --------------------------------------------------------------------------- #
# Away mode needs a bound
# --------------------------------------------------------------------------- #


def test_away_without_an_end_time_or_budget_is_refused(
    rig: RigEnvironment,
) -> None:
    with pytest.raises(OwnerModeError, match="end time or a job budget"):
        set_owner_mode("away")
    assert main(["mode", "set", "away", "--json"]) == 1
    assert load_owner_mode_settings().mode.value == "ask-me"
    # A pre-F17 document (away, no bounds) loads but never starts work.
    mark_presence_known()
    path = get_runtime_gate_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    from saberops.paths import get_owner_mode_path

    get_owner_mode_path().write_text(
        json.dumps(
            {
                "version": 1,
                "mode": "away",
                "until": None,
                "set_at": datetime.now(UTC).isoformat(),
                "set_by": "owner",
            }
        ),
        encoding="utf-8",
    )
    state = evaluate_gate(db=rig.db())
    assert state.closed is True
    assert state.reason == PauseReason.AWAY_UNBOUNDED.value


def test_away_job_budget_pauses_once_with_a_notice(
    rig: RigEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_authority_mode("autonomous")
    set_owner_mode("away", max_jobs=1)
    _live_session(rig, monkeypatch)
    notices: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        "saberops.notifications.notify_once",
        lambda _ledger, key, title, body: notices.append((key, title, body)),
    )
    db = rig.db()
    submit_job(db, repo=rig.repo, goal="first away job")
    state = refresh_gate(db)
    assert state.closed is True
    assert state.reason == PauseReason.AWAY_CAP.value
    assert len(notices) == 1
    assert "paused" in notices[0][1].lower()
    refresh_gate(db)
    assert len(notices) == 1  # one notice per pause, never per tick


def test_expired_away_mode_ignores_its_own_sticky_pause(
    rig: RigEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A legacy AWAY_ENDED record cannot keep work paused after the grant ends."""
    set_authority_mode("autonomous")
    set_owner_mode("away", until="2026-01-01T00:00:00Z")
    _live_session(rig, monkeypatch)
    set_pause(PauseReason.AWAY_ENDED, "the away session ended")
    state = refresh_gate(rig.db())
    assert state.closed is False
    assert state.reason is None


def test_expired_away_mode_does_not_pause_work(
    rig: RigEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ended away grant narrows to ask-me; it never becomes a sticky pause."""
    set_authority_mode("autonomous")
    set_owner_mode("away", until="2026-01-01T00:00:00Z")
    _live_session(rig, monkeypatch)
    state = refresh_gate(rig.db())
    assert state.closed is False
    assert state.reason is None
    assert state.mode.value == "ask-me"
    assert state.away is not None and state.away.expired is True


# --------------------------------------------------------------------------- #
# Digest
# --------------------------------------------------------------------------- #


def test_digest_lists_running_work_and_usage(
    rig: RigEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_authority_mode("autonomous")
    set_owner_mode("critical-only")
    _live_session(rig, monkeypatch)
    db = rig.db()
    submitted = submit_job(db, repo=rig.repo, goal="digest job").job
    from saberops.jobs import store

    # Park it as a running job without launching a vendor CLI: the digest reads
    # durable records only.
    store.mark_dispatched(db, submitted.job_id, run_id="run_digest_0001")
    notices: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        "saberops.notifications.notify_once",
        lambda _ledger, key, title, body: notices.append((key, title, body)),
    )
    state = evaluate_gate(db=db)
    key = maybe_send_digest(db, state)
    assert key is not None and key.startswith("digest:critical-only:")
    assert len(notices) == 1
    body = notices[0][2]
    assert submitted.job_id in body
    assert "running" in body


# --------------------------------------------------------------------------- #
# MCP surface
# --------------------------------------------------------------------------- #


def test_catch_up_and_mcp_show_the_pause_but_only_the_owner_resumes(
    rig: RigEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_authority_mode("autonomous")
    lease = _live_session(rig, monkeypatch)
    db = rig.db()
    submitted = submit_job(db, repo=rig.repo, goal="mcp pause").job
    set_pause(PauseReason.OWNER, "owner stop")

    context = ServerContext(db_path=Path(db.db_path), lease=lease)
    pause = _get_pause(context, {})
    assert pause["closed"] is True
    assert pause["reason"] == PauseReason.OWNER.value
    catch = _catch_up(context, {"job_id": submitted.job_id})
    assert catch["pause"]["closed"] is True

    # D16: an agent cannot clear a pause over MCP, even as the writer.
    assert "resume_work" not in TOOL_NAMES
    assert "resume_work" not in build_default_registry(context).names()
    with pytest.raises(ForbiddenOperationError):
        build_default_registry(context).call("resume_work", {})
    assert evaluate_gate(db=db).closed is True

    # The owner's own `orch resume` path still works.
    resumed = resume_work(db, by="owner")
    assert resumed.closed is False
    assert evaluate_gate(db=db).closed is False


def test_turn_on_and_job_reads_surface_the_paused_gate(
    rig: RigEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A paused store is never silent: turn-on, get_job and wait_job all say so."""
    from saberops.mcp.protocol import McpServer
    from saberops.mcp.tools import build_default_registry

    monkeypatch.setenv("SABEROPS_DISABLE_AUTO_SCHEDULER", "1")
    set_authority_mode("autonomous")
    lease = _live_session(rig, monkeypatch)
    db = rig.db()
    submitted = submit_job(db, repo=rig.repo, goal="paused gate").job
    set_pause(PauseReason.OWNER, "owner stop")

    context = ServerContext(db_path=Path(db.db_path), lease=lease)
    registry = build_default_registry(context)

    # get_job carries the gate facts, so a planner that reads before waiting
    # sees the same pause the resume tool clears.
    view = registry.call("get_job", {"job_id": submitted.job_id})
    assert view["pause"]["closed"] is True
    assert view["pause"]["reason"] == PauseReason.OWNER.value
    assert view["pause"]["resume_command"] == "orch resume"
    assert view["pause"]["resume_tool"] is None

    # wait_job must return the paused outcome immediately instead of burning
    # its timeout and reporting that nothing needs the owner.
    started = time.monotonic()
    waited = registry.call(
        "wait_job", {"job_id": submitted.job_id, "timeout_seconds": 30}
    )
    assert time.monotonic() - started < 5.0
    assert waited["outcome"] == "paused"
    assert waited["timed_out"] is False
    assert waited["resume"]["command"] == "orch resume"
    assert waited["resume"]["tool"] is None

    # Turning SaberOps on in a fresh session states the gate and the resume
    # action before the planner can submit anything else.
    server = McpServer(context, start_active=False)
    assert server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"clientInfo": {"name": "planner"}},
        }
    ) is not None
    server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    turned_on = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "saberops_on", "arguments": {}},
        }
    )
    assert turned_on is not None
    text = turned_on["result"]["content"][0]["text"]
    assert "paused by the owner" in text
    assert "orch resume" in text
    assert "resume_work" not in text


def test_session_loss_records_one_event_and_one_notice(
    rig: RigEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pause is recorded once and noticed once, never once per tick."""
    from saberops.mcp.ledger import EventLedger

    mark_presence_known()
    notices: list[str] = []
    monkeypatch.setattr(
        "saberops.notifications.notify_once",
        lambda _ledger, key, _title, _body: notices.append(key),
    )
    refresh_gate(rig.db())
    refresh_gate(rig.db())
    assert len(notices) == 1
    paused_events = [event for event in EventLedger().events() if event.type == "work_paused"]
    assert len(paused_events) == 1
    assert paused_events[0].payload["reason"] == PauseReason.NO_PLANNING_SESSION.value
