"""H54: one bad run never stops the scheduler, and the owner can see why.

The lane's contract (ADR-0112):

* a PENDING run whose launch failed or whose execution owner is gone is made
  terminal on the tick, with the recorded launch reason and an owner action,
  instead of holding the store with ``legacy_running_without_reservation``;
* the continuous scheduler keeps polling through a resumable RUNNING hold;
* ``orch scheduler status`` reports liveness, the last tick and the last error.

These tests use a real durable store and the real scheduler tick; only the
detached execution launch is stubbed.
"""

from __future__ import annotations

import json
import subprocess
import threading
from argparse import Namespace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from test_loop import _make_candidate, _make_policy, _make_routes

from saberops.capacity import loop
from saberops.capacity.ledger import record_owner_quota
from saberops.capacity.loop import (
    LAUNCH_FAILED_EVENT,
    TickResult,
    acquire_lease,
    read_scheduler_status,
    run_scheduler_loop,
    scheduler_tick,
)
from saberops.capacity.store import owner_connection
from saberops.cli import handle_scheduler_status
from saberops.db import Database, current_iso_timestamp
from saberops.jobs.models import JobState
from saberops.jobs.service import submit_job
from saberops.jobs.store import get_job, mark_dispatched
from saberops.models import Run, RunStatus, Tier

T0 = datetime(2026, 11, 28, 12, 0, 0, tzinfo=UTC)


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "h54@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "H54"], cwd=path, check=True)
    (path / "README.md").write_text("# h54\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"], cwd=path, check=True, capture_output=True
    )
    return path


def _pending_run(repo: Path, run_id: str) -> Run:
    return Run(
        id=run_id,
        task="H54 launch probe",
        target_repo=str(repo),
        base_commit="abc1234",
        tier=Tier.T1,
        training_allowed=True,
        gate_command="true",
        status=RunStatus.PENDING,
        created_at=current_iso_timestamp(),
    )


def _scheduler_inputs(db: Database) -> tuple[Any, Any]:
    record_owner_quota(db, "codex", "chatgpt-plus", 100.0, None, source="OWNER", now=T0)
    routes = _make_routes(((_make_candidate("codex", "gpt-5"),),))
    policy = _make_policy(posture_now=T0)
    return routes, policy


def _reservation_states(db: Database, run_id: str) -> list[str]:
    with owner_connection(db) as conn:
        rows = conn.execute(
            "SELECT state FROM capacity_reservations WHERE run_id = ?", (run_id,)
        ).fetchall()
    return [str(row["state"]) for row in rows]


def test_failed_launch_names_its_reason_and_the_next_tick_runs_other_jobs(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _git_repo(isolated / "failed-launch-repo")
    db = Database(isolated / "failed-launch.db")
    bad = submit_job(db, repo=repo, goal="launch fails").job.job_id
    routes, policy = _scheduler_inputs(db)
    lease = acquire_lease(db, "h54_launch", ttl_seconds=300.0, now=T0)
    assert lease is not None

    def failing_launcher(job: Any, decision: Any, run_id: str) -> None:
        db.create_run(_pending_run(repo, run_id))
        raise RuntimeError("target repository has uncommitted changes")

    first = scheduler_tick(
        db,
        lease,
        now=T0,
        routes=routes,
        policy=policy,
        run_launcher=failing_launcher,
        probes={},
        clock_fn=lambda: T0,
    )
    assert first.ran is True
    still_running = get_job(db, bad)
    assert still_running is not None and still_running.state is JobState.RUNNING
    bad_run = db.get_run(still_running.current_run_id or "")
    assert bad_run is not None and bad_run.status is RunStatus.PENDING
    recorded = db.get_events_by_type(bad_run.id, LAUNCH_FAILED_EVENT)
    assert recorded and "uncommitted changes" in str(recorded[-1].payload)

    # Independent work submitted after the failure still dispatches on the
    # next tick, and the failed launch is named instead of stopping the store.
    good = submit_job(db, repo=repo, goal="independent work").job.job_id
    launched: list[str] = []
    second = scheduler_tick(
        db,
        lease,
        now=T0,
        routes=routes,
        policy=policy,
        run_launcher=lambda job, decision, run_id: launched.append(job.job_id),
        probes={},
        clock_fn=lambda: T0,
    )

    assert second.ran is True
    assert second.reason != "legacy_running_without_reservation"
    failed = get_job(db, bad)
    assert failed is not None and failed.state is JobState.FAILED
    assert failed.failure_reason == "LAUNCH_FAILED"
    settled_run = db.get_run(bad_run.id)
    assert settled_run is not None and settled_run.status is RunStatus.FAILED
    assert "uncommitted changes" in (settled_run.result_summary or "")
    assert "Owner action:" in (settled_run.result_summary or "")
    assert _reservation_states(db, bad_run.id) == ["RELEASED"]
    assert any(change.job_id == bad for change in second.reconciled_changes)
    assert good in launched

    from saberops.cli import handle_job_get

    assert handle_job_get(Namespace(job_id=bad, db=str(db.db_path), json=False)) == 0
    shown = capsys.readouterr().out
    assert "uncommitted changes" in shown
    assert "Owner action:" in shown


def test_unreserved_pending_run_does_not_stop_the_scheduler(isolated: Path) -> None:
    """The exact H37 shape: RUNNING job, PENDING current run, no reservation."""
    repo = _git_repo(isolated / "legacy-pending-repo")
    db = Database(isolated / "legacy-pending.db")
    stuck = submit_job(db, repo=repo, goal="stuck pending run").job.job_id
    run_id = "run_legacy_pending"
    db.create_run(_pending_run(repo, run_id))
    mark_dispatched(db, stuck, run_id)
    good = submit_job(db, repo=repo, goal="independent work").job.job_id
    routes, policy = _scheduler_inputs(db)
    lease = acquire_lease(db, "h54_legacy", ttl_seconds=300.0, now=T0)
    assert lease is not None
    launched: list[str] = []

    result = scheduler_tick(
        db,
        lease,
        now=T0,
        routes=routes,
        policy=policy,
        run_launcher=lambda job, decision, run_id: launched.append(job.job_id),
        probes={},
        clock_fn=lambda: T0,
    )

    assert result.ran is True
    assert result.reason != "legacy_running_without_reservation"
    failed = get_job(db, stuck)
    assert failed is not None and failed.state is JobState.FAILED
    assert failed.failure_reason == "LAUNCH_FAILED"
    settled = db.get_run(run_id)
    assert settled is not None and settled.status is RunStatus.FAILED
    assert "never produced a live execution owner" in (settled.result_summary or "")
    assert "Owner action:" in (settled.result_summary or "")
    assert good in launched


def test_scheduler_loop_keeps_polling_through_a_resumable_hold(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(isolated / "loop-hold.db")
    calls: list[str] = []
    stop = threading.Event()

    def fake_tick(*args: Any, **kwargs: Any) -> TickResult:
        calls.append("tick")
        if len(calls) == 1:
            return TickResult(
                ran=False,
                reason="legacy_running_without_reservation",
                legacy_running_jobs=("job_stuck",),
            )
        stop.set()
        return TickResult(ran=True)

    monkeypatch.setattr(loop, "scheduler_tick", fake_tick)

    result = run_scheduler_loop(
        db, tick_interval=0.01, stop_event=stop, holder_id="h54_loop_hold"
    )

    assert len(calls) == 2
    assert result is not None and result.ran is True
    status = read_scheduler_status(db)
    assert status["running"] is False
    assert status["last_outcome"] == "ok"
    assert status["last_error"] is None


def test_scheduler_status_reports_a_stopped_scheduler_and_its_last_error(
    isolated: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db = Database(isolated / "status.db")

    def fatal_tick(*args: Any, **kwargs: Any) -> TickResult:
        return TickResult(ran=False, reason="ledger_read_error")

    monkeypatch.setattr(loop, "scheduler_tick", fatal_tick)
    result = run_scheduler_loop(db, tick_interval=0.01, holder_id="h54_status")

    assert result is not None and result.reason == "ledger_read_error"
    status = read_scheduler_status(db)
    assert status["running"] is False
    assert status["last_outcome"] == "ledger_read_error"
    assert status["last_error"] == "ledger_read_error"
    assert status["last_tick_at"] is not None
    assert status["stopped_at"] is not None

    assert handle_scheduler_status(Namespace(db=str(db.db_path), json=True)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["running"] is False
    assert payload["last_error"] == "ledger_read_error"

    assert handle_scheduler_status(Namespace(db=str(db.db_path), json=False)) == 0
    text = capsys.readouterr().out
    assert "not running" in text
    assert "ledger_read_error" in text
