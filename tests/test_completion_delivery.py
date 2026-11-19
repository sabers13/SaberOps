from __future__ import annotations

from pathlib import Path

import pytest
from lifecycle_rig import build_lifecycle_rig

from saberops.jobs.service import submit_job
from saberops.mcp.tools import ServerContext, build_default_registry

pytestmark = pytest.mark.packaged_routing_default


def test_wait_timeout_on_queued_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = build_lifecycle_rig(tmp_path, monkeypatch)
    job = submit_job(rig.db(), repo=rig.repo, goal="wait test").job
    registry = build_default_registry(
        ServerContext(db_path=rig.db().db_path, questions_store=tmp_path / "questions.json")
    )
    result = registry.call("wait_job", {"job_id": job.job_id, "timeout_seconds": 0})
    assert result["timed_out"] is True
    assert result["job"]["state"] == "QUEUED"


def test_notices_are_deduplicated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from saberops.notifications import notify_once

    monkeypatch.setenv("SABEROPS_DESKTOP_NOTIFICATIONS", "1")
    log = tmp_path / "notices"
    command = tmp_path / "notify-send"
    command.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$NOTICE_LOG"\n', encoding="utf-8")
    command.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    monkeypatch.setenv("NOTICE_LOG", str(log))
    db = tmp_path / "notices.db"
    notify_once(db, "job:1:failed", "Failed", "job 1")
    notify_once(db, "job:1:failed", "Failed", "job 1")
    notify_once(db, "question:1", "Question", "question 1")
    assert log.read_text().splitlines() == ["Failed job 1", "Question question 1"]


@pytest.mark.parametrize("state", ["SUCCEEDED", "FAILED"])
def test_wait_returns_terminal_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    import threading
    import time

    from saberops.jobs import store
    from saberops.jobs.models import RUN_FAILED, JobState

    rig = build_lifecycle_rig(tmp_path, monkeypatch)
    db = rig.db()
    job = submit_job(db, repo=rig.repo, goal="terminal test").job
    store.mark_dispatched(db, job.job_id, "run-test")

    def finish() -> None:
        time.sleep(0.2)
        store.reconcile_transition(
            db,
            job.job_id,
            to=JobState(state),
            failure_reason=RUN_FAILED if state == "FAILED" else None,
        )

    thread = threading.Thread(target=finish)
    thread.start()
    registry = build_default_registry(
        ServerContext(db_path=db.db_path, questions_store=tmp_path / "questions.json")
    )
    result = registry.call("wait_job", {"job_id": job.job_id, "timeout_seconds": 3})
    thread.join()
    assert result["timed_out"] is False
    assert result["job"]["state"] == state


def test_question_wakes_wait_and_delivery_survives_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from saberops.notifications import deliver_pending
    from saberops.questions import (
        AskRole,
        DecisionClass,
        Escalation,
        OwnerQuestionQueue,
        QuestionRequest,
    )

    rig = build_lifecycle_rig(tmp_path, monkeypatch)
    db = rig.db()
    job = submit_job(db, repo=rig.repo, goal="question test").job
    queue = OwnerQuestionQueue()
    queue.ask(
        QuestionRequest(
            key="test-question",
            subject="Choose a direction",
            kind="decision",
            decision_class=DecisionClass.OTHER,
            raised_by_role=AskRole.BUILD,
            raised_by="worker",
            escalation=Escalation(
                requirement="r",
                proposed_expansion="p",
                smallest_compliant="s",
                consequences="c",
                recommendation="r",
            ),
            blocks=(job.job_id,),
        )
    )
    registry = build_default_registry(ServerContext(db_path=db.db_path, questions_store=queue.path))
    result = registry.call("wait_job", {"job_id": job.job_id, "timeout_seconds": 0})
    assert result["outcome"] == "needs_attention"
    log = tmp_path / "notice-log"
    stub = tmp_path / "notify-send"
    stub.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$NOTICE_LOG"\n', encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    monkeypatch.setenv("NOTICE_LOG", str(log))
    monkeypatch.setenv("SABEROPS_DESKTOP_NOTIFICATIONS", "1")
    deliver_pending(db)
    deliver_pending(rig.db())
    assert len(log.read_text().splitlines()) == 2


def test_disabled_notifications_never_invoke_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from saberops.notifications import notify_once

    command = tmp_path / "notify-send"
    log = tmp_path / "log"
    command.write_text('#!/bin/sh\ntouch "$NOTICE_LOG"\n', encoding="utf-8")
    command.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    monkeypatch.setenv("NOTICE_LOG", str(log))
    monkeypatch.setenv("SABEROPS_DESKTOP_NOTIFICATIONS", "0")
    notify_once(tmp_path / "ledger", "event", "title", "body")
    assert not log.exists()


@pytest.mark.parametrize(("mode", "expected"), [("commit", "SUCCEEDED"), ("crash", "FAILED")])
def test_wait_real_scheduler_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, expected: str
) -> None:
    import threading
    from typing import Literal, cast

    from saberops.capacity.loop import run_scheduler_loop
    from saberops.jobs.service import get_job
    from saberops.model_access.readiness_service import ReadinessService
    from saberops.paths import owner_state_dir

    rig = build_lifecycle_rig(tmp_path, monkeypatch)
    rig.set_worker_mode(cast(Literal["commit", "crash"], mode))
    log = tmp_path / "notices"
    stub = tmp_path / "notify-send"
    stub.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$NOTICE_LOG"\n', encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{__import__('os').environ['PATH']}")
    monkeypatch.setenv("NOTICE_LOG", str(log))
    monkeypatch.setenv("SABEROPS_DESKTOP_NOTIFICATIONS", "1")
    db = rig.db()
    job = submit_job(db, repo=rig.repo, goal="real wait lifecycle").job
    readiness = ReadinessService.for_owner_state(owner_state_dir())
    first = run_scheduler_loop(db, once=True, readiness_service=readiness)
    assert first and first.dispatched == 1
    running = get_job(db, job.job_id)
    assert running and running.current_run_id
    registry = build_default_registry(ServerContext(db_path=db.db_path))
    result: dict[str, object] = {}

    def wait() -> None:
        result.update(registry.call("wait_job", {"job_id": job.job_id, "timeout_seconds": 60}))

    thread = threading.Thread(target=wait)
    thread.start()
    try:
        rig.wait_for_run(running.current_run_id, timeout=50)
        for _ in range(2):
            tick = run_scheduler_loop(db, once=True, readiness_service=readiness)
            assert tick and tick.ran
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert result["timed_out"] is False
        assert isinstance(result["job"], dict) and result["job"]["state"] == expected
        assert len(log.read_text().splitlines()) == 1
        assert ("finished" if expected == "SUCCEEDED" else "failed") in log.read_text()
        if expected == "SUCCEEDED":
            assert isinstance(result["result"], dict)
            assert result["result"]["candidate_sha"]
            assert result["result"]["owner_action"]
    finally:
        thread.join(timeout=60)


def test_ordinary_lifecycle_delivery_is_disabled_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from saberops.jobs import store
    from saberops.jobs.models import JobState
    from saberops.notifications import deliver_pending

    rig = build_lifecycle_rig(tmp_path, monkeypatch)
    log = tmp_path / "notice-log"
    stub = tmp_path / "notify-send"
    stub.write_text('#!/bin/sh\ntouch "$NOTICE_LOG"\n', encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    monkeypatch.setenv("NOTICE_LOG", str(log))
    db = rig.db()
    job = submit_job(db, repo=rig.repo, goal="ordinary lifecycle").job
    store.mark_dispatched(db, job.job_id, "run-test")
    store.reconcile_transition(db, job.job_id, to=JobState.SUCCEEDED)
    deliver_pending(db)
    assert not log.exists()


def test_persistent_notifications_setting_and_environment_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from saberops.cli import main
    from saberops.notifications import notify_once

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("SABEROPS_DESKTOP_NOTIFICATIONS", raising=False)
    log = tmp_path / "log"
    stub = tmp_path / "notify-send"
    stub.write_text('#!/bin/sh\ntouch "$NOTICE_LOG"\n', encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    monkeypatch.setenv("NOTICE_LOG", str(log))
    assert main(["notifications", "set", "off"]) == 0
    assert main(["notifications", "show", "--json"]) == 0
    assert '"enabled": false' in capsys.readouterr().out
    notify_once(tmp_path / "ledger", "a", "title", "body")
    assert not log.exists()
    monkeypatch.setenv("SABEROPS_DESKTOP_NOTIFICATIONS", "1")
    notify_once(tmp_path / "ledger", "a", "title", "body")
    assert log.exists()
    log.unlink()
    assert main(["notifications", "set", "on"]) == 0
    monkeypatch.setenv("SABEROPS_DESKTOP_NOTIFICATIONS", "0")
    notify_once(tmp_path / "ledger", "b", "title", "body")
    assert not log.exists()


def _install_corrupt_running_row(db: object, repo: Path, created_at: str) -> None:
    """Write a RUNNING job row with a NULL run id the ``Job`` model rejects."""
    from saberops.capacity.store import owner_connection

    with owner_connection(db) as conn:  # type: ignore[arg-type]
        conn.execute(
            "INSERT INTO jobs (job_id, caller_id, repo, base_ref, base_sha,"
            " goal, requirements_json, priority, state, current_run_id,"
            " created_at, updated_at, request_digest, run_ids_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy_null_run",
                "test",
                str(repo),
                "main",
                "abc1234",
                "legacy row without a run id",
                "{}",
                "NORMAL",
                "RUNNING",
                None,
                created_at,
                created_at,
                "dg_legacy_null_run",
                "[]",
            ),
        )
        conn.commit()


def _failed_job(db: object, repo: Path) -> str:
    from saberops.jobs import store
    from saberops.jobs.models import RUN_FAILED, JobState

    job = submit_job(db, repo=repo, goal="failed job needing a notice").job
    store.mark_dispatched(db, job.job_id, "run-notice")
    store.reconcile_transition(db, job.job_id, to=JobState.FAILED, failure_reason=RUN_FAILED)
    return job.job_id


def _notice_stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log = tmp_path / "notice-log"
    stub = tmp_path / "notify-send"
    stub.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$NOTICE_LOG"\n', encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    monkeypatch.setenv("NOTICE_LOG", str(log))
    monkeypatch.setenv("SABEROPS_DESKTOP_NOTIFICATIONS", "1")
    return log


def test_delivery_scan_is_read_only_for_corrupt_running_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C3 delivery runs before C1's fail-closed guards, so its job scan must not
    quarantine the legacy RUNNING row those guards report to the owner.
    """
    from saberops.capacity.store import owner_connection
    from saberops.notifications import deliver_pending

    rig = build_lifecycle_rig(tmp_path, monkeypatch)
    log = _notice_stub(tmp_path, monkeypatch)
    db = rig.db()
    job_id = _failed_job(db, rig.repo)
    _install_corrupt_running_row(db, rig.repo, "2026-11-19T12:00:00+00:00")

    deliver_pending(db)

    # The terminal job still produces exactly one notice.
    lines = log.read_text().splitlines()
    assert len(lines) == 1
    assert "failed" in lines[0] and job_id in lines[0]
    # The corrupt row is left exactly as found: no quarantine, no FAILED rewrite.
    with owner_connection(db) as conn:
        row = conn.execute(
            "SELECT state, current_run_id, failure_reason FROM jobs WHERE job_id = ?",
            ("legacy_null_run",),
        ).fetchone()
        tables = {
            str(r["name"])
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        quarantined = (
            conn.execute(
                "SELECT COUNT(*) AS n FROM jobs_quarantine WHERE job_id = ?",
                ("legacy_null_run",),
            ).fetchone()["n"]
            if "jobs_quarantine" in tables
            else 0
        )
    assert row is not None
    assert row["state"] == "RUNNING"
    assert row["current_run_id"] is None
    assert row["failure_reason"] is None
    assert quarantined == 0


def test_tick_delivers_notice_while_fail_closed_guard_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Delivery at the top of the tick still runs when the legacy-RUNNING guard
    parks the tick, and it must not erase the row the guard names.
    """
    from datetime import UTC, datetime

    from saberops.capacity.loop import acquire_lease, scheduler_tick

    rig = build_lifecycle_rig(tmp_path, monkeypatch)
    log = _notice_stub(tmp_path, monkeypatch)
    t0 = datetime(2026, 11, 19, 12, 0, 0, tzinfo=UTC)
    db = rig.db()
    _failed_job(db, rig.repo)
    _install_corrupt_running_row(db, rig.repo, t0.isoformat())
    lease = acquire_lease(db, "c3c_delivery", ttl_seconds=300.0, now=t0)
    assert lease is not None

    result = scheduler_tick(db, lease, now=t0, probes={}, clock_fn=lambda: t0)

    assert result.ran is False
    assert result.reason == "legacy_running_without_reservation"
    assert result.legacy_running_jobs == ("legacy_null_run",)
    lines = log.read_text().splitlines()
    assert len(lines) == 1 and "failed" in lines[0]
