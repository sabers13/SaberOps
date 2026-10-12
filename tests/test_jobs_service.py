"""Job service: submit, idempotency, feasibility, list, cancel, capacity.

Feasibility tests always pass an explicit RoutingSnapshot (never the
machine's real owner config); the one ``routing=None`` test isolates
``XDG_CONFIG_HOME`` instead (T-009 hazard).
"""

from __future__ import annotations

import dataclasses
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from saberops.db import Database
from saberops.jobs import store
from saberops.jobs.feasibility import feasible
from saberops.jobs.models import (
    NO_ROUTE_FOR_REQUIREMENTS,
    JobRequirements,
    JobRole,
    JobState,
    Priority,
    WaitingReason,
)
from saberops.jobs.service import (
    CapacityView,
    IdempotencyConflictError,
    PoolCapacity,
    cancel_job,
    capacity,
    get_job,
    job_dispatch_spec,
    list_jobs,
    reconcile_jobs,
    submit_job,
)
from saberops.models import RunStatus, WorkerCandidate
from saberops.routing_config import RoutingSnapshot, load_packaged_default

_NOW = datetime(2026, 10, 12, 12, 0, 0, tzinfo=UTC)


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "jobs@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Jobs"], cwd=path, check=True)
    (path / "README.md").write_text("# jobs\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=path, check=True, capture_output=True)
    return path


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _snapshot() -> RoutingSnapshot:
    return load_packaged_default()


def _opencode_only_snapshot() -> RoutingSnapshot:
    only = [WorkerCandidate(provider="opencode", model="opencode-go/m")]
    return RoutingSnapshot(
        schema_version=2,
        source="test",
        source_path=None,
        content_hash="test",
        t1_default=list(only),
        t2_training_allowed=[],
        t2_training_denied_off_peak=[],
        t2_training_denied_peak=[],
        t3_off_peak=[],
        t3_peak=[],
    )


# ---------------------------------------------------------------------------
# submit happy path
# ---------------------------------------------------------------------------


def test_submit_happy_path(tmp_path: Path) -> None:
    db = Database(tmp_path / "jobs.db")
    repo = _git_repo(tmp_path / "repo")
    result = submit_job(db, repo=repo, goal="do the thing", routing=_snapshot(), now=_NOW)
    assert result.created is True
    job = result.job
    assert job.state == JobState.QUEUED
    assert job.waiting_reason == WaitingReason.NEW
    assert job.base_ref == "HEAD"
    assert job.base_sha == _head(repo)
    assert job.repo == str(repo.resolve())
    assert job.request_digest
    stored = get_job(db, job.job_id)
    assert stored == job
    assert len(store.list_events(db, job.job_id)) == 1


def test_submit_routing_none_uses_packaged_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    db = Database(tmp_path / "jobs.db")
    repo = _git_repo(tmp_path / "repo")
    result = submit_job(db, repo=repo, goal="do the thing", routing=None, now=_NOW)
    assert result.job.state == JobState.QUEUED


def test_submit_rejects_bad_repo_and_ref(tmp_path: Path) -> None:
    db = Database(tmp_path / "jobs.db")
    with pytest.raises(ValueError):
        submit_job(db, repo=tmp_path / "missing", goal="x", routing=_snapshot())
    repo = _git_repo(tmp_path / "repo")
    (repo / "plain.txt").write_text("not a repo file\n", encoding="utf-8")
    with pytest.raises(ValueError):
        submit_job(
            db, repo=repo, goal="x", base_ref="no-such-ref", routing=_snapshot()
        )
    with pytest.raises(ValueError):
        submit_job(db, repo=repo, goal="   ", routing=_snapshot())


def test_submit_rejects_naive_deadline(tmp_path: Path) -> None:
    db = Database(tmp_path / "jobs.db")
    repo = _git_repo(tmp_path / "repo")
    with pytest.raises(ValueError):
        submit_job(
            db, repo=repo, goal="x", deadline="2026-10-13T12:00:00",
            routing=_snapshot(),
        )
    result = submit_job(
        db, repo=repo, goal="x", deadline="2026-10-13T12:00:00+00:00",
        routing=_snapshot(), now=_NOW,
    )
    assert result.job.deadline is not None


# ---------------------------------------------------------------------------
# idempotency
# ---------------------------------------------------------------------------


def test_idempotent_replay_returns_same_job_without_new_event(tmp_path: Path) -> None:
    db = Database(tmp_path / "jobs.db")
    repo = _git_repo(tmp_path / "repo")
    first = submit_job(
        db, repo=repo, goal="same goal", idempotency_key="k1",
        routing=_snapshot(), now=_NOW,
    )
    assert first.created is True
    second = submit_job(
        db, repo=repo, goal="same goal", idempotency_key="k1",
        routing=_snapshot(), now=_NOW,
    )
    assert second.created is False
    assert second.job.job_id == first.job.job_id
    assert len(store.list_events(db, first.job.job_id)) == 1


def test_same_key_different_goal_conflicts_without_write(tmp_path: Path) -> None:
    db = Database(tmp_path / "jobs.db")
    repo = _git_repo(tmp_path / "repo")
    first = submit_job(
        db, repo=repo, goal="goal one", idempotency_key="k2",
        routing=_snapshot(), now=_NOW,
    )
    n_events = len(store.list_events(db, first.job.job_id))
    with pytest.raises(IdempotencyConflictError):
        submit_job(
            db, repo=repo, goal="goal two", idempotency_key="k2",
            routing=_snapshot(), now=_NOW,
        )
    assert len(store.list_events(db, first.job.job_id)) == n_events
    assert len(list_jobs(db, limit=100)) == 1


def test_no_key_always_new_job(tmp_path: Path) -> None:
    db = Database(tmp_path / "jobs.db")
    repo = _git_repo(tmp_path / "repo")
    first = submit_job(db, repo=repo, goal="g", routing=_snapshot(), now=_NOW)
    second = submit_job(db, repo=repo, goal="g", routing=_snapshot(), now=_NOW)
    assert first.job.job_id != second.job.job_id


# ---------------------------------------------------------------------------
# feasibility at submit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "role", ["ORCHESTRATOR", "INTEGRATOR", "SURGEON", "REVIEWER"]
)
def test_non_worker_roles_store_failed(tmp_path: Path, role: str) -> None:
    db = Database(tmp_path / "jobs.db")
    repo = _git_repo(tmp_path / "repo")
    result = submit_job(
        db, repo=repo, goal="x",
        requirements=JobRequirements(role=JobRole(role)),
        routing=_snapshot(), now=_NOW,
    )
    assert result.created is True
    assert result.job.state == JobState.FAILED
    assert result.job.failure_reason == NO_ROUTE_FOR_REQUIREMENTS


def test_unsupported_capability_stores_failed(tmp_path: Path) -> None:
    db = Database(tmp_path / "jobs.db")
    repo = _git_repo(tmp_path / "repo")
    result = submit_job(
        db, repo=repo, goal="x",
        requirements=JobRequirements(required_capabilities=("tool_restriction",)),
        routing=_snapshot(), now=_NOW,
    )
    assert result.job.state == JobState.FAILED
    assert result.job.failure_reason == NO_ROUTE_FOR_REQUIREMENTS


def test_unknown_capability_is_validation_error(tmp_path: Path) -> None:
    db = Database(tmp_path / "jobs.db")
    _git_repo(tmp_path / "repo")
    with pytest.raises(ValueError):
        JobRequirements(required_capabilities=("teleport",))
    assert list_jobs(db, limit=100) == []


def test_read_only_maps_to_read_only_capability() -> None:
    req = JobRequirements(read_only=True)
    ok, _ = feasible(req, _snapshot())
    # The packaged chains include codex candidates, which carry READ_ONLY.
    assert ok is True
    ok_opencode, reason = feasible(req, _opencode_only_snapshot())
    assert ok_opencode is False
    assert reason == NO_ROUTE_FOR_REQUIREMENTS


def test_size_effort_family_validation(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        JobRequirements(size_hint="XL")
    with pytest.raises(ValueError):
        JobRequirements(size_hint="s")
    with pytest.raises(ValueError):
        JobRequirements(effort="ultra")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        JobRequirements(
            model_family_include=("a",), model_family_exclude=("a",)
        )
    with pytest.raises(ValueError):
        JobRequirements(model_family_include=("UPPER",))
    db = Database(tmp_path / "jobs.db")
    assert list_jobs(db, limit=100) == []


# ---------------------------------------------------------------------------
# list ordering + filter
# ---------------------------------------------------------------------------


def test_list_jobs_ordering_and_state_filter(tmp_path: Path) -> None:
    db = Database(tmp_path / "jobs.db")
    repo = _git_repo(tmp_path / "repo")
    snap = _snapshot()
    low = submit_job(
        db, repo=repo, goal="background",
        requirements=JobRequirements(role=JobRole.WORKER),
        priority=Priority.BACKGROUND, routing=snap, now=_NOW,
    ).job
    urgent = submit_job(
        db, repo=repo, goal="urgent", priority=Priority.URGENT,
        routing=snap, now=_NOW + timedelta(seconds=1),
    ).job
    normal = submit_job(
        db, repo=repo, goal="normal", priority=Priority.NORMAL,
        routing=snap, now=_NOW + timedelta(seconds=2),
    ).job
    failed = submit_job(
        db, repo=repo, goal="bad role",
        requirements=JobRequirements(role=JobRole.SURGEON),
        routing=snap, now=_NOW + timedelta(seconds=3),
    ).job
    assert [j.job_id for j in list_jobs(db, limit=100)] == [
        urgent.job_id, normal.job_id, failed.job_id, low.job_id,
    ]
    assert {j.job_id for j in list_jobs(db, state=JobState.FAILED)} == {failed.job_id}
    assert len(list_jobs(db, state=JobState.QUEUED, limit=100)) == 3


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------


def test_cancel_queued(tmp_path: Path) -> None:
    db = Database(tmp_path / "jobs.db")
    repo = _git_repo(tmp_path / "repo")
    job = submit_job(db, repo=repo, goal="x", routing=_snapshot(), now=_NOW).job
    result = cancel_job(db, job.job_id, run_canceller=lambda _run: True)
    assert result.cancelled is True
    assert result.job.state == JobState.CANCELLED


def test_cancel_running_true_and_false(tmp_path: Path) -> None:
    db = Database(tmp_path / "jobs.db")
    repo = _git_repo(tmp_path / "repo")
    job = submit_job(db, repo=repo, goal="x", routing=_snapshot(), now=_NOW).job
    store.mark_dispatched(db, job.job_id, "run_1", now=_NOW)
    refused = cancel_job(db, job.job_id, run_canceller=lambda _run: False)
    assert refused.cancelled is False
    assert refused.job.state == JobState.RUNNING
    calls: list[str] = []

    def _record(run: str) -> bool:
        calls.append(run)
        return True

    done = cancel_job(db, job.job_id, run_canceller=_record)
    assert done.cancelled is True
    assert calls == ["run_1"]
    assert done.job.state == JobState.CANCELLED


def test_cancel_terminal_and_unknown(tmp_path: Path) -> None:
    from saberops.jobs.store import JobNotFoundError

    db = Database(tmp_path / "jobs.db")
    repo = _git_repo(tmp_path / "repo")
    job = submit_job(db, repo=repo, goal="x", routing=_snapshot(), now=_NOW).job
    cancel_job(db, job.job_id, run_canceller=lambda _run: True)
    again = cancel_job(db, job.job_id, run_canceller=lambda _run: True)
    assert again.cancelled is False
    with pytest.raises(JobNotFoundError):
        cancel_job(db, "no-such", run_canceller=lambda _run: True)


# ---------------------------------------------------------------------------
# capacity stub (C5)
# ---------------------------------------------------------------------------


def test_capacity_stub_shape(tmp_path: Path) -> None:
    db = Database(tmp_path / "jobs.db")
    view = capacity(db, now=_NOW)
    assert isinstance(view, CapacityView)
    assert view.ledger_wired is True
    assert view.pools == ()
    assert view.generated_at
    names = [f.name for f in dataclasses.fields(PoolCapacity)]
    assert names == [
        "pool_id", "window_id", "remaining_est", "uncertainty",
        "observed_at", "source", "stale", "excluded_until",
    ]
    types = [f.type for f in dataclasses.fields(PoolCapacity)]
    assert types == [
        "str", "str", "float | None", "float",
        "str | None", "str | None", "bool", "str | None",
    ]


# ---------------------------------------------------------------------------
# reconciliation (section G)
# ---------------------------------------------------------------------------


def _submit_running(
    db: Database, repo: Path, goal: str, run_id: str = "run_1"
) -> str:
    job = submit_job(db, repo=repo, goal=goal, routing=_snapshot(), now=_NOW).job
    store.mark_dispatched(db, job.job_id, run_id, now=_NOW)
    return job.job_id


def test_reconcile_status_mapping(tmp_path: Path) -> None:
    from saberops.jobs.models import RUN_FAILED, RUN_MISSING

    db = Database(tmp_path / "jobs.db")
    repo = _git_repo(tmp_path / "repo")
    completed = _submit_running(db, repo, "g1", "run_completed")
    failed = _submit_running(db, repo, "g2", "run_failed")
    missing = _submit_running(db, repo, "g3", "run_missing")
    pending = _submit_running(db, repo, "g4", "run_pending")
    running = _submit_running(db, repo, "g5", "run_running")
    orphaned = _submit_running(db, repo, "g6", "run_orphaned")

    def status(run_id: str) -> RunStatus | None:
        return {
            "run_completed": RunStatus.COMPLETED,
            "run_failed": RunStatus.FAILED,
            "run_pending": RunStatus.PENDING,
            "run_running": RunStatus.RUNNING,
            "run_orphaned": RunStatus.ORPHANED,
        }.get(run_id)

    changes = reconcile_jobs(db, run_status=status, now=_NOW)
    by_job = {c.job_id: c for c in changes}
    assert set(by_job) == {completed, failed, missing}
    assert by_job[completed].to_state == JobState.SUCCEEDED
    assert by_job[completed].failure_reason is None
    assert by_job[failed].to_state == JobState.FAILED
    assert by_job[failed].failure_reason == RUN_FAILED
    assert by_job[missing].to_state == JobState.FAILED
    assert by_job[missing].failure_reason == RUN_MISSING
    for untouched in (pending, running, orphaned):
        current = get_job(db, untouched)
        assert current is not None
        assert current.state == JobState.RUNNING
    # No event for untouched RUNNING jobs: still exactly 2 events each.
    for untouched in (pending, running, orphaned):
        assert len(store.list_events(db, untouched)) == 2
    # Idempotent second call changes nothing.
    assert reconcile_jobs(db, run_status=status, now=_NOW) == []


def test_reconcile_cancel_race_one_terminal_state_one_event(tmp_path: Path) -> None:
    db = Database(tmp_path / "jobs.db")
    repo = _git_repo(tmp_path / "repo")
    # Cancel wins first: reconcile then finds no RUNNING job.
    job_id = _submit_running(db, repo, "race-cancel", "run_race_1")
    cancel_job(db, job_id, run_canceller=lambda _run: True)
    changes = reconcile_jobs(
        db, run_status=lambda _run: RunStatus.COMPLETED, now=_NOW
    )
    assert changes == []
    final = get_job(db, job_id)
    assert final is not None and final.state == JobState.CANCELLED
    terminal = [e for e in store.list_events(db, job_id) if e["to_state"] == "CANCELLED"]
    assert len(terminal) == 1
    # Reconcile wins first: cancel then refuses.
    job_id2 = _submit_running(db, repo, "race-reconcile", "run_race_2")
    changes2 = reconcile_jobs(
        db, run_status=lambda _run: RunStatus.COMPLETED, now=_NOW
    )
    assert [c.job_id for c in changes2] == [job_id2]
    refused = cancel_job(db, job_id2, run_canceller=lambda _run: True)
    assert refused.cancelled is False
    assert refused.job.state == JobState.SUCCEEDED
    terminal2 = [
        e for e in store.list_events(db, job_id2) if e["to_state"] == "SUCCEEDED"
    ]
    assert len(terminal2) == 1


def test_reconcile_tolerates_cancel_between_list_and_cas(tmp_path: Path) -> None:
    db = Database(tmp_path / "jobs.db")
    repo = _git_repo(tmp_path / "repo")
    job_id = _submit_running(db, repo, "race-mid", "run_mid")

    def status(run_id: str) -> RunStatus | None:
        # Simulate a concurrent cancel winning mid-reconcile.
        cancel_job(db, job_id, run_canceller=lambda _run: True)
        assert run_id == "run_mid"
        return RunStatus.COMPLETED

    assert reconcile_jobs(db, run_status=status, now=_NOW) == []
    final = get_job(db, job_id)
    assert final is not None and final.state == JobState.CANCELLED


# ---------------------------------------------------------------------------
# job -> run mapping (section H)
# ---------------------------------------------------------------------------


def test_job_dispatch_spec_fields(tmp_path: Path) -> None:
    from saberops.jobs.models import Effort

    db = Database(tmp_path / "jobs.db")
    repo = _git_repo(tmp_path / "repo")
    job = submit_job(
        db, repo=repo, goal="implement X", acceptance="gate passes",
        requirements=JobRequirements(effort=Effort.medium, read_only=True,
                                     size_hint="M"),
        routing=_snapshot(), now=_NOW,
    ).job
    spec = job_dispatch_spec(job)
    assert spec.job_id == job.job_id
    assert spec.request.task == "implement X\n\nAcceptance:\n" + "gate passes"
    assert spec.request.repo_path == job.repo
    assert spec.request.routing_mode == "auto"
    assert "read_only" in tuple(spec.request.required_capabilities)
    assert spec.request.provider_override is None
    assert spec.request.model_override is None
    assert spec.request.manual_tier is None
    assert spec.request.max_auto_tier is None
    assert spec.base_ref == "HEAD"
    assert spec.base_sha == _head(repo)
    assert spec.effort == Effort.medium
    assert spec.size_hint == "M"
    assert spec.resume_from is None


def test_job_dispatch_spec_no_acceptance_no_frozen_policy(tmp_path: Path) -> None:
    db = Database(tmp_path / "jobs.db")
    repo = _git_repo(tmp_path / "repo")
    job = submit_job(db, repo=repo, goal="plain", routing=_snapshot(), now=_NOW).job
    spec = job_dispatch_spec(job)
    assert spec.request.task == "plain"
    assert spec.request.required_capabilities == ()
    # No frozen policy travels in the spec.
    assert not hasattr(spec, "run_config")
    assert not hasattr(spec, "control_plane_digest")
    assert not hasattr(spec.request, "control_plane_digest")
