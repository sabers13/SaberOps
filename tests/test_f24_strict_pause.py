"""Phase 1 F24: strict pause -- no AI CLI session, no work.

The owner's rule, given twice on 2026-11-19: closing the CLI pauses the work
"no matter what", because spending subscription usage with nobody watching is a
serious leak.  F17 armed that pause only after an MCP planning session had
taken the planning lease at least once, so a store driven only by plain
``orch`` commands in a terminal was never paused.  This lane requires presence
from the start for every store: with no planning session and no attended
terminal, SaberOps starts nothing.

Presence has two sources:

* an AI CLI planning session that holds the MCP lease and heartbeats it;
* an *attended terminal session*: ``orch watch`` while a terminal is attached,
  or ``orch run --attended`` while that command runs.

A fresh store with no session is closed but not sticky -- nothing was
interrupted, so queued work starts as soon as an attended session opens.  Once
presence has been seen, losing it is F17's sticky pause: parked workers, one
notice and an explicit confirmation to resume.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from lifecycle_rig import RigEnvironment, build_lifecycle_rig

from saberops.attendance import (
    ATTENDANCE_DIR_ENV,
    attend,
    live_attendance,
    record_attendance,
    record_path,
)
from saberops.authority import set_authority_mode
from saberops.jobs.models import JobState
from saberops.jobs.service import get_job, submit_job
from saberops.mcp.ledger import EventLedger
from saberops.model_access.readiness_service import ReadinessService
from saberops.paths import get_runtime_gate_path, owner_state_dir
from saberops.runtime_gate import (
    PauseReason,
    evaluate_gate,
    presence,
    read_gate,
    refresh_gate,
)

pytestmark = [
    pytest.mark.packaged_routing_default,
    pytest.mark.no_attendance,
]

_SETUP_TIMEOUT = 180


@pytest.fixture()
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[RigEnvironment]:
    """A fresh store that starts unattended; each test opens presence itself."""
    environment = build_lifecycle_rig(tmp_path, monkeypatch, attended=False)
    monkeypatch.setenv("SABEROPS_PLANNING_HEARTBEAT_TIMEOUT", "60")
    yield environment


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


# --------------------------------------------------------------------------- #
# A fresh store starts nothing
# --------------------------------------------------------------------------- #


def test_fresh_store_with_no_session_never_dispatches(rig: RigEnvironment) -> None:
    """Covers: F24 -- a never-attended store dispatches nothing."""
    set_authority_mode("autonomous")
    db = rig.db()
    submitted = submit_job(db, repo=rig.repo, goal="no session, no work").job

    result = _tick(rig)
    assert result is not None
    assert result.ran is True
    assert result.reason == "paused"
    assert result.pause_reason == PauseReason.NO_PLANNING_SESSION.value

    current = get_job(db, submitted.job_id)
    assert current is not None
    assert current.state is JobState.QUEUED
    assert current.current_run_id is None
    # Nothing was interrupted, so the engine records no sticky pause and no
    # notice yet: the gate is simply closed until somebody is present.
    state = evaluate_gate(db=db)
    assert state.closed is True
    assert state.transient is True
    assert read_gate().pause is None


def test_submit_queues_until_an_attended_terminal_opens(
    rig: RigEnvironment,
) -> None:
    """Covers: F24 -- `orch submit` queues; work starts when presence opens."""
    set_authority_mode("autonomous")
    db = rig.db()
    completed = rig.run_cli(
        "job", "submit", "--repo", str(rig.repo), "--goal", "attended terminal starts me", "--json"
    )
    assert completed.returncode == 0, completed.stderr
    job_id = json.loads(completed.stdout)["job"]["job_id"]
    _tick(rig)
    assert get_job(db, job_id).state is JobState.QUEUED  # type: ignore[union-attr]

    with attend("watch", label="orch watch test"):
        assert presence().live is True
        assert presence().source == "terminal"

        def dispatched() -> bool:
            _tick(rig)
            current = get_job(db, job_id)
            assert current is not None
            return bool(current.current_run_id) or current.state in (
                JobState.SUCCEEDED,
                JobState.FAILED,
            )

        _wait(dispatched)

    run_id = get_job(db, job_id).current_run_id  # type: ignore[union-attr]
    assert run_id is not None
    rig.wait_for_run(run_id, timeout=240)


# --------------------------------------------------------------------------- #
# Closing the attended terminal pauses within the heartbeat bound
# --------------------------------------------------------------------------- #


def test_closing_the_attended_terminal_pauses_the_run(
    rig: RigEnvironment,
) -> None:
    """Covers: F24 -- presence ends when the attended terminal closes."""
    set_authority_mode("autonomous")
    rig.set_worker_mode("hang", hang_seconds=180)
    db = rig.db()
    submitted = submit_job(db, repo=rig.repo, goal="paused when I close watch").job

    with attend("watch", label="orch watch test"):
        def dispatched() -> bool:
            _tick(rig)
            current = get_job(db, submitted.job_id)
            return bool(current and current.current_run_id)

        _wait(dispatched)
        run_id = get_job(db, submitted.job_id).current_run_id  # type: ignore[union-attr]
        assert run_id is not None

        # The terminal closes: the record disappears and the next tick parks
        # the running worker at a safe point.
        def parked() -> bool:
            _tick(rig)
            current = get_job(db, submitted.job_id)
            return current is not None and current.state is JobState.QUEUED

        # End presence before asking the scheduler to notice.
        assert live_attendance() is not None

    _wait(parked)
    parked_job = get_job(db, submitted.job_id)
    assert parked_job is not None
    assert parked_job.waiting_reason is not None
    assert evaluate_gate(db=db).closed is True
    assert read_gate().pause is not None


def test_a_crashed_attended_terminal_is_stale_within_the_heartbeat_bound(
    rig: RigEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: F24 -- a killed attended terminal stops counting presence."""
    monkeypatch.setenv("SABEROPS_PLANNING_HEARTBEAT_TIMEOUT", "1")
    # A live pid (this process) with an old heartbeat: a terminal that stopped
    # heartbeating (crash, lost connection) is not presence after the bound.
    stale = record_attendance("watch", label="stale terminal")
    stale_path = record_path(stale.session_id)
    payload = json.loads(stale_path.read_text(encoding="utf-8"))
    payload["heartbeat_at"] = time.time() - 5
    stale_path.write_text(json.dumps(payload), encoding="utf-8")
    assert presence().live is False
    assert evaluate_gate(db=rig.db()).reason == PauseReason.NO_PLANNING_SESSION.value

    # A record whose process is gone is not presence even while it looks fresh.
    dead_payload = dict(payload)
    dead_payload["pid"] = 999_999
    dead_payload["heartbeat_at"] = time.time()
    stale_path.write_text(json.dumps(dead_payload), encoding="utf-8")
    assert presence().live is False


def test_watch_and_attended_run_both_grant_presence(
    rig: RigEnvironment,
) -> None:
    """Covers: F24 -- both attended terminal kinds count, in the same store."""
    for kind in ("watch", "attended-run"):
        record = record_attendance(kind, label=f"test {kind}")
        assert presence().live is True
        assert presence().source == "terminal"
        assert live_attendance() is not None
        record_path(record.session_id).unlink()
        assert presence().live is False


# --------------------------------------------------------------------------- #
# An F17-unarmed store is migrated on upgrade
# --------------------------------------------------------------------------- #


def test_f17_unarmed_store_becomes_armed_on_upgrade(
    rig: RigEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: F24 -- an F17 'presence_known: false' document is armed once."""
    path = get_runtime_gate_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "presence_known": False,
                "pause": None,
                "last_resume": None,
                "paused_runs": [],
            }
        ),
        encoding="utf-8",
    )
    notices: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        "saberops.notifications.notify_once",
        lambda _ledger, key, title, body: notices.append((key, title, body)),
    )

    before = evaluate_gate(db=rig.db())
    assert before.closed is True
    assert before.migration_pending is True

    migrated = refresh_gate(rig.db())
    assert migrated.closed is True
    assert migrated.reason == PauseReason.NO_PLANNING_SESSION.value
    assert read_gate().presence_known is True
    assert read_gate().legacy_unarmed is False
    assert len(notices) == 1
    assert "requires a session" in notices[0][1]
    armed_events = [e for e in EventLedger().events() if e.type == "work_gate_armed"]
    assert len(armed_events) == 1

    # One migration only: repeated evaluation changes nothing and notifies once.
    refresh_gate(rig.db())
    assert len(notices) == 1
    assert len([e for e in EventLedger().events() if e.type == "work_gate_armed"]) == 1


def test_attendance_directory_is_isolated(
    rig: RigEnvironment, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Presence records never escape the active profile/state directory."""
    override = tmp_path / "attendance-override"
    monkeypatch.setenv(ATTENDANCE_DIR_ENV, str(override))
    assert os.environ.get(ATTENDANCE_DIR_ENV) == str(override)
    with attend("attended-run", label="override"):
        assert presence().live is True
        assert list(override.glob("*.json"))
        assert Path(owner_state_dir()) != override


# --------------------------------------------------------------------------- #
# `orch run --attended` and `orch watch` hold presence
# --------------------------------------------------------------------------- #


def test_attended_run_flag_holds_presence(
    rig: RigEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: F24 -- `orch run --attended` is presence while it runs."""
    from types import SimpleNamespace

    from saberops.cli import main

    observed: list[bool] = []

    def fake_start(self: Any, config: Any) -> Any:
        observed.append(presence().live)
        return SimpleNamespace(run_id="run_f24_attended")

    monkeypatch.setattr(
        "saberops.cli.RunSupervisor.start_supervised_run", fake_start
    )
    assert (
        main(
            [
                "run",
                "--repo",
                str(rig.repo),
                "--task",
                "attended run test",
                "--attended",
            ]
        )
        == 1  # no execution owner is claimed by the fake supervisor
    )
    assert observed == [True]
    # The attendance record ends with the command, so presence is gone again.
    assert presence().live is False


def test_attended_run_cannot_detach(rig: RigEnvironment) -> None:
    """Covers: F24 -- an attended run must stay in the foreground."""
    from saberops.cli import main

    code = main(
        [
            "run",
            "--repo",
            str(rig.repo),
            "--task",
            "attended run test",
            "--attended",
            "--detach",
        ]
    )
    assert code == 2


def test_plain_run_requires_presence(
    rig: RigEnvironment, capsys: pytest.CaptureFixture[str]
) -> None:
    """Covers: F24 -- a direct run without a session never starts work."""
    from saberops.cli import main

    code = main(
        [
            "run",
            "--repo",
            str(rig.repo),
            "--task",
            "unattended direct run",
        ]
    )
    assert code == 1
    assert "--attended" in capsys.readouterr().err


def test_attended_run_respects_an_owner_pause(
    rig: RigEnvironment, capsys: pytest.CaptureFixture[str]
) -> None:
    """Covers: F24 -- presence never clears an explicit pause."""
    from saberops.cli import main
    from saberops.runtime_gate import PauseReason, set_pause

    set_pause(PauseReason.OWNER, "owner stop")
    code = main(
        [
            "run",
            "--repo",
            str(rig.repo),
            "--task",
            "attended run under pause",
            "--attended",
        ]
    )
    assert code == 1
    assert "orch resume" in capsys.readouterr().err
    assert presence().live is False


def test_watch_presence_follows_an_attached_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: F24 -- `orch watch` is presence only while a terminal is attached."""
    from saberops.watch import _refresh_watch_attendance

    folder = tmp_path / "attendance"
    monkeypatch.setenv(ATTENDANCE_DIR_ENV, str(folder))

    class FakeTmux:
        def __init__(self, attached: bool) -> None:
            self.attached = attached

        def has_client(self, session: str) -> bool:
            return self.attached

    tmux = FakeTmux(False)
    stub = cast(Any, tmux)
    assert _refresh_watch_attendance(stub, "watch-session", None) is None
    assert presence().live is False

    tmux.attached = True
    keeper = _refresh_watch_attendance(stub, "watch-session", None)
    assert keeper is not None
    assert presence().live is True

    tmux.attached = False
    assert _refresh_watch_attendance(stub, "watch-session", keeper) is None
    assert presence().live is False
