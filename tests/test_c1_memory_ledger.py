"""Regression checks for nonblocking worker memory reservations."""

import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from saberops._gate_tools import gate_tools as gt
from saberops.capacity.loop import ensure_scheduler_running, scheduler_alive, stop_scheduler
from saberops.db import Database
from saberops.jobs.service import submit_job


def _wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def test_two_sessions_start_one_scheduler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SABEROPS_DISABLE_AUTO_SCHEDULER")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    db = Database(tmp_path / "owner.db")
    barrier = threading.Barrier(2)
    started: list[bool] = []

    def turn_on() -> None:
        barrier.wait()
        started.append(ensure_scheduler_running(db))

    threads = [threading.Thread(target=turn_on) for _ in range(2)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert sorted(started) == [False, True]
        assert _wait_until(lambda: scheduler_alive(db))
    finally:
        stop_scheduler(db)
    assert not scheduler_alive(db)


def test_submit_starts_scheduler_and_picks_up_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SABEROPS_DISABLE_AUTO_SCHEDULER")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c",
                    "user.email=test@example.invalid", "commit", "--allow-empty", "-qm", "init"],
                   check=True)
    db = Database(tmp_path / "owner.db")
    try:
        result = submit_job(db, repo=repo, goal="Exercise scheduler")
        assert result.created
        from saberops.jobs.models import JobState

        assert result.job.state == JobState.QUEUED
        assert _wait_until(lambda: scheduler_alive(db))
        from saberops.jobs import store
        from saberops.jobs.models import WaitingReason

        def processed() -> bool:
            job = store.get_job(db, result.job.job_id)
            return job is not None and job.waiting_reason != WaitingReason.NEW

        assert _wait_until(processed, 5.0)
        second = submit_job(db, repo=repo, goal="Exercise cross-process polling")
        assert second.job.state == JobState.QUEUED
        start = time.monotonic()

        def second_processed() -> bool:
            job = store.get_job(db, second.job.job_id)
            return job is not None and job.waiting_reason != WaitingReason.NEW

        assert _wait_until(second_processed, 5.0)
        assert time.monotonic() - start <= 5.0
    finally:
        stop_scheduler(db)


def test_capacity_reports_scheduler_liveness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_mcp_contracts import _payload, _server

    monkeypatch.delenv("SABEROPS_DISABLE_AUTO_SCHEDULER")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    server = _server(tmp_path)
    db = server.context.database(read_only=False)
    assert db is not None
    assert _payload(server, "capacity", {})["capacity"]["scheduler_alive"] is False
    try:
        ensure_scheduler_running(db)
        assert _wait_until(lambda: scheduler_alive(db))
        assert _payload(server, "capacity", {})["capacity"]["scheduler_alive"] is True
    finally:
        stop_scheduler(db)


@pytest.mark.parametrize(
    "reason", ["ledger_read_error", "legacy_running_without_reservation",
               "running_run_status_unresolved", "lost_lease"]
)
def test_scheduler_cli_reports_error_exit(
    reason: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from argparse import Namespace

    from saberops import cli
    from saberops.capacity import loop

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(loop, "run_scheduler_loop", lambda *a, **kw: loop.TickResult(
        ran=False, reason=reason
    ))
    monkeypatch.setattr(cli, "_scheduler_storage_callbacks", lambda db: (None, None))
    args = Namespace(db=str(tmp_path / "owner.db"), once=True, interval_seconds=2,
                     max_concurrent=None, json=False)
    assert cli.handle_scheduler_run(args) != 0
    assert reason in capsys.readouterr().err


def test_nonblocking_worker_reservation_denial_and_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(gt.MEMORY_GUARD_ENV, "1")
    available = 300 * gt.MIB
    ledger = gt.MemoryLedger(
        tmp_path / "ledger",
        reserve_bytes=0,
        available_memory_fn=lambda: available,
        pressure_fn=lambda: gt.MemoryPressure(0.0, 0.0),
    )
    first, reason = ledger.try_acquire_nonblocking("worker_cli", "M")
    assert first is not None and reason is None
    denied, reason = ledger.try_acquire_nonblocking("worker_cli", "M")
    assert denied is None and reason == "MEMORY_LOW"
    first.release()
    second, reason = ledger.try_acquire_nonblocking("worker_cli", "M")
    assert second is not None and reason is None
    second.release()
    assert ledger.active_holders() == []


def test_learned_worker_sizes_and_pressure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(gt.MEMORY_GUARD_ENV, "1")
    pressure = gt.MemoryPressure(0.0, 0.0)
    ledger = gt.MemoryLedger(
        tmp_path / "ledger", reserve_bytes=0,
        available_memory_fn=lambda: 1000 * gt.MIB,
        pressure_fn=lambda: pressure,
    )
    ledger.record_peak("worker_cli", "small", 100 * gt.MIB)
    ledger.record_peak("worker_cli", "large", 450 * gt.MIB)
    small = [ledger.try_acquire_nonblocking("worker_cli", "small")[0] for _ in range(8)]
    assert all(lease is not None for lease in small)
    for lease in small:
        assert lease is not None
        lease.release()
    large = [ledger.try_acquire_nonblocking("worker_cli", "large")[0] for _ in range(3)]
    assert large[0] is not None and large[1] is not None and large[2] is None
    for lease in large:
        if lease is not None:
            lease.release()
    pressure = gt.MemoryPressure(10.0, 0.0)
    assert ledger.try_acquire_nonblocking("worker_cli", "small")[1] == "MEMORY_LOW"
    pressure = gt.MemoryPressure(0.0, 0.0)
    resumed, reason = ledger.try_acquire_nonblocking("worker_cli", "small")
    assert resumed is not None and reason is None
    resumed.release()


def test_memory_handoff_survives_launcher_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(gt.MEMORY_GUARD_ENV, "1")
    ledger = gt.MemoryLedger(
        tmp_path / "ledger", reserve_bytes=0,
        available_memory_fn=lambda: 250 * gt.MIB,
        pressure_fn=lambda: gt.MemoryPressure(0.0, 0.0),
    )
    lease, _ = ledger.try_acquire_nonblocking("worker_cli", "M")
    assert lease is not None
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        pass_fds=(lease._lock_file.fileno(),),
    )
    try:
        lease.handoff()
        assert ledger.try_acquire_nonblocking("worker_cli", "M")[1] == "MEMORY_LOW"
    finally:
        child.terminate()
        child.wait(timeout=5)
    after, reason = ledger.try_acquire_nonblocking("worker_cli", "M")
    assert after is not None and reason is None
    after.release()


def test_corrupt_job_is_quarantined_and_memory_park_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_loop import _NOW, _make_candidate, _make_job, _make_policy, _make_routes

    from saberops.capacity.ledger import record_owner_quota
    from saberops.capacity.loop import acquire_lease, scheduler_tick
    from saberops.jobs import store
    from saberops.jobs.models import JobState, WaitingReason

    monkeypatch.setenv(gt.LEDGER_ENV, str(tmp_path / "ledger"))
    monkeypatch.setenv(gt.MEMORY_GUARD_ENV, "1")
    available = 0
    monkeypatch.setattr(gt, "available_memory_bytes", lambda: available)
    monkeypatch.setattr(gt, "read_memory_pressure", lambda: gt.MemoryPressure(0.0, 0.0))
    db = Database(tmp_path / "owner.db")
    repo = tmp_path / "repo"
    repo.mkdir()
    for name in ("bad", "good"):
        job = _make_job(name, str(repo), created_at=_NOW.isoformat(),
                        updated_at=_NOW.isoformat(), request_digest=name)
        store.insert_job(db, job, now=_NOW)
    with db._get_connection() as conn:
        conn.execute("UPDATE jobs SET requirements_json = ? WHERE job_id = ?", ("{", "bad"))
        conn.commit()
    record_owner_quota(db, "openai", "chatgpt-plus", 100.0, None, source="OWNER", now=_NOW)
    lease = acquire_lease(db, "test", now=_NOW)
    assert lease is not None
    routes = _make_routes(((_make_candidate("openai", "gpt-5"),),))
    launched: list[str] = []

    def launch(job: object, decision: object, run_id: str) -> None:
        launched.append(run_id)

    first = scheduler_tick(db, lease, now=_NOW, clock_fn=lambda: _NOW, routes=routes,
                           policy=_make_policy(), run_launcher=launch)
    assert first.ran and first.dispatched == 0
    parked = store.get_job(db, "good")
    assert parked is not None and parked.waiting_reason == WaitingReason.MEMORY_LOW
    with db._get_connection() as conn:
        row = conn.execute("SELECT state, failure_reason FROM jobs WHERE job_id='bad'").fetchone()
    assert row is not None and row["state"] == JobState.FAILED.value
    assert str(row["failure_reason"]).startswith("CORRUPT_JOB_ROW")
    quarantined = store.get_job(db, "bad")
    assert quarantined is not None and quarantined.failure_reason == "CORRUPT_JOB_ROW"
    with db._get_connection() as conn:
        raw = conn.execute("SELECT raw_json FROM jobs_quarantine WHERE job_id='bad'").fetchone()
    assert raw is not None and '"requirements_json": "{"' in raw["raw_json"]
    available = gt.DESKTOP_RESERVE_BYTES + 1000 * gt.MIB
    second = scheduler_tick(db, lease, now=_NOW, clock_fn=lambda: _NOW, routes=routes,
                            policy=_make_policy(), run_launcher=launch)
    assert second.ran and second.dispatched == 1 and len(launched) == 1
