"""Job store + state machine (M2-3 sections C + D)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

from saberops.db import Database
from saberops.jobs import store
from saberops.jobs.models import (
    Job,
    JobRequirements,
    JobRole,
    JobState,
    Priority,
    WaitingReason,
)

_NOW = datetime(2026, 10, 12, 12, 0, 0, tzinfo=UTC)


def _db(tmp_path: Path, name: str = "jobs.db") -> Database:
    return Database(tmp_path / name)


def _job(job_id: str, *, state: JobState = JobState.QUEUED) -> Job:
    waiting = WaitingReason.NEW if state == JobState.QUEUED else None
    current = "run_1" if state == JobState.RUNNING else None
    run_ids = ("run_1",) if state == JobState.RUNNING else ()
    failure = None
    if state == JobState.FAILED:
        failure = "RUN_FAILED"
    return Job(
        job_id=job_id,
        idempotency_key=None,
        caller_id="owner",
        repo="/tmp/repo",
        base_ref="HEAD",
        base_sha="abc123",
        goal="do the thing",
        acceptance=None,
        requirements=JobRequirements(role=JobRole.WORKER),
        priority=Priority.NORMAL,
        state=state,
        waiting_reason=waiting,
        failure_reason=failure,
        current_run_id=current,
        run_ids=run_ids,
        created_at=_NOW.isoformat(),
        updated_at=_NOW.isoformat(),
        request_digest="digest-" + job_id,
    )


def _seed(db: Database, job_id: str, state: JobState = JobState.QUEUED) -> Job:
    return store.insert_job(db, _job(job_id, state=state), now=_NOW)


# ---------------------------------------------------------------------------
# tables
# ---------------------------------------------------------------------------


def test_tables_created_idempotently_in_owner_db(tmp_path: Path) -> None:
    owner_path = tmp_path / "owner.db"
    project_path = tmp_path / "project.db"
    db = Database(project_path, owner_db=Database(owner_path))
    store.ensure_tables(db)
    store.ensure_tables(db)
    assert owner_path.exists()
    import sqlite3

    names = {
        r[0]
        for r in sqlite3.connect(str(owner_path))
        .execute("SELECT name FROM sqlite_master WHERE type='table'")
        .fetchall()
    }
    assert {"jobs", "job_events"} <= names
    # A project Database with an owner_db writes jobs to the owner file.
    _seed(db, "job_owner_1")
    assert store.get_job(db, "job_owner_1") is not None
    if project_path.exists():
        project_names = {
            r[0]
            for r in sqlite3.connect(str(project_path))
            .execute("SELECT name FROM sqlite_master WHERE type='table'")
            .fetchall()
        }
        assert "jobs" not in project_names


# ---------------------------------------------------------------------------
# allowed transitions write exactly one event
# ---------------------------------------------------------------------------


def test_submit_writes_one_event(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _seed(db, "job_submit_1")
    events = store.list_events(db, "job_submit_1")
    assert len(events) == 1
    assert events[0]["from_state"] is None
    assert events[0]["to_state"] == "QUEUED"


def test_allowed_transitions_write_one_event_each(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _seed(db, "job_flow_1")
    job = store.mark_dispatched(db, "job_flow_1", "run_1", now=_NOW)
    assert job.state == JobState.RUNNING
    assert job.current_run_id == "run_1"
    assert job.run_ids == ("run_1",)
    assert job.waiting_reason is None
    events = store.list_events(db, "job_flow_1")
    assert len(events) == 2
    assert events[-1]["from_state"] == "QUEUED"
    assert events[-1]["to_state"] == "RUNNING"


def test_full_allowed_flow(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _seed(db, "job_flow_2")
    store.mark_dispatched(db, "job_flow_2", "run_1", now=_NOW)
    job = store.requeue(db, "job_flow_2", WaitingReason.PREEMPTED, now=_NOW)
    assert job.state == JobState.QUEUED
    assert job.waiting_reason == WaitingReason.PREEMPTED
    assert job.preemptions == 1
    assert job.current_run_id is None
    assert job.run_ids == ("run_1",)
    job = store.set_waiting(db, "job_flow_2", WaitingReason.NO_CAPACITY, now=_NOW)
    assert job.waiting_reason == WaitingReason.NO_CAPACITY
    store.mark_dispatched(db, "job_flow_2", "run_2", now=_NOW)
    job = store.reconcile_transition(
        db, "job_flow_2", to=JobState.SUCCEEDED, now=_NOW
    )
    assert job.state == JobState.SUCCEEDED
    events = store.list_events(db, "job_flow_2")
    kinds = [e["kind"] for e in events]
    assert kinds[0] == "submitted"
    assert events[1]["from_state"] == "QUEUED" and events[1]["to_state"] == "RUNNING"
    # One event per transition, seq strictly increasing.
    assert [e["seq"] for e in events] == sorted(e["seq"] for e in events)
    assert len({e["seq"] for e in events}) == len(events)


def test_cancel_from_queued_and_running(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _seed(db, "job_cancel_q")
    job = store.cancel_transition(db, "job_cancel_q", now=_NOW)
    assert job.state == JobState.CANCELLED
    _seed(db, "job_cancel_r")
    store.mark_dispatched(db, "job_cancel_r", "run_9", now=_NOW)
    job = store.cancel_transition(db, "job_cancel_r", now=_NOW)
    assert job.state == JobState.CANCELLED


def test_reconcile_to_failed_sets_reason(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _seed(db, "job_rec_f")
    store.mark_dispatched(db, "job_rec_f", "run_1", now=_NOW)
    job = store.reconcile_transition(
        db, "job_rec_f", to=JobState.FAILED, failure_reason="RUN_FAILED", now=_NOW
    )
    assert job.state == JobState.FAILED
    assert job.failure_reason == "RUN_FAILED"


def test_terminal_states_immutable(tmp_path: Path) -> None:
    db = _db(tmp_path)
    for job_id, terminal in (
        ("job_term_s", JobState.SUCCEEDED),
        ("job_term_f", JobState.FAILED),
        ("job_term_c", JobState.CANCELLED),
    ):
        if terminal == JobState.SUCCEEDED:
            _seed(db, job_id)
            store.mark_dispatched(db, job_id, "run_1", now=_NOW)
            store.reconcile_transition(db, job_id, to=JobState.SUCCEEDED, now=_NOW)
        elif terminal == JobState.FAILED:
            _seed(db, job_id)
            store.mark_dispatched(db, job_id, "run_1", now=_NOW)
            store.reconcile_transition(
                db, job_id, to=JobState.FAILED, failure_reason="RUN_FAILED", now=_NOW
            )
        else:
            _seed(db, job_id)
            store.cancel_transition(db, job_id, now=_NOW)
        before = store.get_job(db, job_id)
        assert before is not None
        n_events = len(store.list_events(db, job_id))
        for op in (
            lambda j=job_id: store.mark_dispatched(db, j, "run_x"),
            lambda j=job_id: store.set_waiting(db, j, WaitingReason.NO_CAPACITY),
            lambda j=job_id: store.requeue(db, j, WaitingReason.PREEMPTED),
            lambda j=job_id: store.cancel_transition(db, j),
            lambda j=job_id: store.reconcile_transition(db, j, to=JobState.SUCCEEDED),
        ):
            with pytest.raises(store.JobTransitionError):
                op()
        after = store.get_job(db, job_id)
        assert after == before
        assert len(store.list_events(db, job_id)) == n_events


def test_requeue_preempted_keeps_run_ids(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _seed(db, "job_prem_1")
    store.mark_dispatched(db, "job_prem_1", "run_1", now=_NOW)
    job = store.requeue(db, "job_prem_1", WaitingReason.PREEMPTED, now=_NOW)
    assert job.preemptions == 1
    assert job.run_ids == ("run_1",)
    store.mark_dispatched(db, "job_prem_1", "run_2", now=_NOW)
    job = store.requeue(db, "job_prem_1", WaitingReason.NO_CAPACITY, now=_NOW)
    assert job.preemptions == 1
    assert job.run_ids == ("run_1", "run_2")


# ---------------------------------------------------------------------------
# disallowed pairs: JobTransitionError, DB unchanged
# ---------------------------------------------------------------------------


def _seed_in_state(db: Database, job_id: str, state: JobState) -> None:
    if state == JobState.QUEUED:
        _seed(db, job_id, JobState.QUEUED)
    elif state == JobState.RUNNING:
        _seed(db, job_id)
        store.mark_dispatched(db, job_id, "run_1", now=_NOW)
    elif state == JobState.SUCCEEDED:
        _seed(db, job_id)
        store.mark_dispatched(db, job_id, "run_1", now=_NOW)
        store.reconcile_transition(db, job_id, to=JobState.SUCCEEDED, now=_NOW)
    elif state == JobState.FAILED:
        _seed(db, job_id)
        store.mark_dispatched(db, job_id, "run_1", now=_NOW)
        store.reconcile_transition(
            db, job_id, to=JobState.FAILED, failure_reason="RUN_FAILED", now=_NOW
        )
    else:
        _seed(db, job_id)
        store.cancel_transition(db, job_id, now=_NOW)


_DISALLOWED = [
    ("RUNNING", "mark_dispatched"),
    ("SUCCEEDED", "mark_dispatched"),
    ("FAILED", "mark_dispatched"),
    ("CANCELLED", "mark_dispatched"),
    ("RUNNING", "set_waiting"),
    ("SUCCEEDED", "set_waiting"),
    ("FAILED", "set_waiting"),
    ("CANCELLED", "set_waiting"),
    ("QUEUED", "requeue"),
    ("SUCCEEDED", "requeue"),
    ("FAILED", "requeue"),
    ("CANCELLED", "requeue"),
    ("QUEUED", "reconcile"),
    ("SUCCEEDED", "reconcile"),
    ("FAILED", "reconcile"),
    ("CANCELLED", "reconcile"),
    ("SUCCEEDED", "cancel"),
    ("FAILED", "cancel"),
    ("CANCELLED", "cancel"),
]


@pytest.mark.parametrize("from_state,op", _DISALLOWED)
def test_disallowed_pairs_raise_and_leave_db_unchanged(
    tmp_path: Path, from_state: str, op: str
) -> None:
    db = _db(tmp_path)
    job_id = f"job_neg_{from_state}_{op}"
    _seed_in_state(db, job_id, JobState(from_state))
    before = store.get_job(db, job_id)
    assert before is not None
    before_events = store.list_events(db, job_id)
    with pytest.raises(store.JobTransitionError):
        if op == "mark_dispatched":
            store.mark_dispatched(db, job_id, "run_x", now=_NOW)
        elif op == "set_waiting":
            store.set_waiting(db, job_id, WaitingReason.NO_CAPACITY, now=_NOW)
        elif op == "requeue":
            store.requeue(db, job_id, WaitingReason.PREEMPTED, now=_NOW)
        elif op == "reconcile":
            store.reconcile_transition(db, job_id, to=JobState.SUCCEEDED, now=_NOW)
        elif op == "cancel":
            store.cancel_transition(db, job_id, now=_NOW)
        else:  # pragma: no cover
            raise AssertionError(op)
    assert store.get_job(db, job_id) == before
    assert store.list_events(db, job_id) == before_events


def test_unknown_job_ids(tmp_path: Path) -> None:
    db = _db(tmp_path)
    with pytest.raises(store.JobNotFoundError):
        store.mark_dispatched(db, "no-such-job", "run_1")
    with pytest.raises(store.JobNotFoundError):
        store.set_waiting(db, "no-such-job", WaitingReason.NEW)
    with pytest.raises(store.JobNotFoundError):
        store.requeue(db, "no-such-job", WaitingReason.PREEMPTED)
    with pytest.raises(store.JobNotFoundError):
        store.cancel_transition(db, "no-such-job")


# ---------------------------------------------------------------------------
# compare-and-set: single dispatch (ADR section 4.9/4.11)
# ---------------------------------------------------------------------------


def test_concurrent_dispatch_exactly_one_wins(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _seed(db, "job_race_1")
    outcomes: list[str] = []

    def attempt(run_id: str) -> None:
        try:
            store._cas(
                db,
                "job_race_1",
                expected=JobState.QUEUED,
                updates={
                    "state": JobState.RUNNING.value,
                    "waiting_reason": None,
                    "current_run_id": run_id,
                    "run_ids_json": f'["{run_id}"]',
                },
                kind="dispatched",
                to_state=JobState.RUNNING,
                payload={"run_id": run_id},
                now=_NOW,
            )
            outcomes.append("ok:" + run_id)
        except store.JobConflictError:
            outcomes.append("conflict:" + run_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(attempt, ("run_A", "run_B")))
    assert sorted(o.split(":")[0] for o in outcomes) == ["conflict", "ok"]
    job = store.get_job(db, "job_race_1")
    assert job is not None
    assert job.state == JobState.RUNNING
    assert len(job.run_ids) == 1
