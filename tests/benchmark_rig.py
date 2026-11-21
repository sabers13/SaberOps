"""Fake-mode benchmark harness for the Phase 1 B2 rig.

Runs the ten fixed tasks from :mod:`saberops.benchmark` through the lifecycle
test rig's real job/scheduler/worker/gate/review/accept path using the fake
worker and reviewer.  No vendor CLI and no network access.
"""

from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from lifecycle_rig import RigEnvironment, create_toy_repo

from saberops._gate_tools import gate_tools
from saberops.accept import AcceptEngine
from saberops.benchmark import (
    FAKE_REVIEW_BLOCK_SENTINEL,
    TASKS,
    BenchmarkReport,
    BenchmarkTask,
    DrillResult,
    TaskResult,
    aggregate_subscription_usage,
    count_block_verdicts,
)
from saberops.capacity.loop import run_scheduler_loop, scheduler_alive
from saberops.cli import main as cli_main
from saberops.control_plane.capabilities import Capability
from saberops.jobs import store
from saberops.jobs.models import JobRequirements, JobState
from saberops.jobs.service import get_job, job_status_view, submit_job
from saberops.mcp.tools import ServerContext, build_default_registry
from saberops.model_access.readiness_service import ReadinessService
from saberops.models import ReviewPolicyMode
from saberops.ownership import Liveness, ProcessIdentity, probe_liveness
from saberops.paths import owner_state_dir
from saberops.project_settings import set_project_review_policy
from saberops.supervisor import RunSupervisor

#: The fake-mode table must stay comfortably inside the gate budget; the drills
#: have their own 120-second visible-state bound from the phase 1 exit test.
BENCHMARK_TIMEOUT_SECONDS = 120.0
DRILL_VISIBLE_LIMIT_SECONDS = 120.0
C2_RECOVERY_LIMIT_SECONDS = 60.0
SETUP_TIMEOUT_SECONDS = 180.0


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------


def _epoch(value: str | None) -> float | None:
    if not value:
        return None
    return datetime.fromisoformat(value).timestamp()


def _tick(db: Any, readiness: ReadinessService) -> None:
    result = run_scheduler_loop(db, once=True, readiness_service=readiness)
    assert result is not None and result.ran, result


def _wait_for(
    predicate: Callable[[], bool],
    *,
    timeout: float,
    message: str,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    pytest.fail(f"{message} (waited {timeout:g}s)")


def install_stub_notifier(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put a stub ``notify-send`` first on PATH and log one line per notice.

    The stub never contacts a desktop service.  Each line is
    ``epoch_ns<TAB>title<TAB>body`` so a test can measure delivery delay and
    count notices per job.
    """
    bin_dir = tmp_path / "notice-bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    log_path = tmp_path / "notices.log"
    stub = bin_dir / "notify-send"
    stub.write_text(
        "#!/bin/sh\n"
        'printf "%s\\t%s\\t%s\\n" "$(date +%s%N)" "$1" "$2" >> "$NOTICE_LOG"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("NOTICE_LOG", str(log_path))
    monkeypatch.setenv("SABEROPS_DESKTOP_NOTIFICATIONS", "1")
    return log_path


def read_notices(log_path: Path) -> list[tuple[float, str, str]]:
    """Parse the stub notifier log into ``(epoch_seconds, title, body)``."""
    if not log_path.exists():
        return []
    notices: list[tuple[float, str, str]] = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        notices.append((int(parts[0]) / 1_000_000_000, parts[1], parts[2]))
    return notices


def notices_for_job(log_path: Path, job_id: str) -> list[float]:
    return [when for when, _title, body in read_notices(log_path) if body == job_id]


def prepare_isolated_memory(
    rig: RigEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pin the host-wide memory ledger so dispatch does not flake on other lanes.

    The benchmark is not a memory-admission test; C1 owns that.  A private
    ledger plus deterministic readings keep ten small fake workers admitted
    while the real gate guard stays on.
    """
    monkeypatch.setenv(gate_tools.LEDGER_ENV, str(rig.root / "benchmark-ledger"))
    monkeypatch.setattr(gate_tools, "available_memory_bytes", lambda: 8 * 1024**3)
    monkeypatch.setattr(
        gate_tools, "read_memory_pressure", lambda: gate_tools.MemoryPressure(0.0, 0.0)
    )


def create_task_repos(rig: RigEnvironment) -> dict[str, Path]:
    """Create one small toy repository per task and configure its gate/review."""
    parent = rig.root / "benchmark-repos"
    parent.mkdir(parents=True, exist_ok=True)
    repos: dict[str, Path] = {}
    for task in TASKS:
        repo, _sha = create_toy_repo(parent, f"toy-{task.task_id}")
        set_project_review_policy(repo, ReviewPolicyMode.ALWAYS_REQUIRED)
        assert cli_main(["project", "set-gate", "--repo", str(repo), rig.gate_command]) == 0
        repos[task.task_id] = repo
    return repos


def _family_requirements(task: BenchmarkTask) -> JobRequirements:
    """Force the task onto its chosen subscription's fake provider."""
    excluded = "opencode" if task.family == "codex" else "codex"
    return JobRequirements(model_family_exclude=(excluded,))


def _job_terminal(job_id: str, db: Any) -> bool:
    job = get_job(db, job_id)
    return job is not None and job.state in {
        JobState.SUCCEEDED,
        JobState.FAILED,
        JobState.CANCELLED,
    }


def _subscription_for(db: Any, job_id: str) -> str:
    job = get_job(db, job_id)
    if job is None or job.current_run_id is None:
        return "unknown"
    attempts = db.get_attempts_for_run(job.current_run_id)
    if not attempts:
        return "unknown"
    attempt = attempts[-1]
    return f"{attempt.provider}/{attempt.model}"


def _started_at(db: Any, job_id: str) -> float | None:
    job = get_job(db, job_id)
    if job is None or job.current_run_id is None:
        return None
    attempts = db.get_attempts_for_run(job.current_run_id)
    if not attempts:
        return None
    return _epoch(attempts[0].created_at)


def _terminal_at(db: Any, job_id: str) -> float | None:
    job = get_job(db, job_id)
    if job is None or job.current_run_id is None:
        return None
    run = db.get_run(job.current_run_id)
    return None if run is None else _epoch(run.completed_at)


# --------------------------------------------------------------------------
# Ten-task benchmark
# --------------------------------------------------------------------------


def run_fake_benchmark(
    rig: RigEnvironment,
    monkeypatch: pytest.MonkeyPatch,
    *,
    timeout: float = BENCHMARK_TIMEOUT_SECONDS,
) -> BenchmarkReport:
    """Submit all ten tasks, drive the scheduler, accept eligible runs, measure."""
    started = time.time()
    db = rig.db()
    readiness = ReadinessService.for_owner_state(owner_state_dir())
    notice_log = install_stub_notifier(rig.root, monkeypatch)
    prepare_isolated_memory(rig, monkeypatch)
    repos = create_task_repos(rig)
    rig.set_reviewer_verdict("PASS")

    submitted_at: dict[str, float] = {}
    jobs: dict[str, str] = {}
    for task in TASKS:
        goal = task.goal
        if task.expect == "review_blocked":
            goal = f"{goal}\n\n{FAKE_REVIEW_BLOCK_SENTINEL}"
        submitted_at[task.task_id] = time.time()
        submitted = submit_job(
            db,
            repo=repos[task.task_id],
            goal=goal,
            acceptance=task.acceptance,
            requirements=_family_requirements(task),
        )
        jobs[task.task_id] = submitted.job.job_id

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _tick(db, readiness)
        if all(_job_terminal(job_id, db) for job_id in jobs.values()):
            break
        time.sleep(0.05)
    else:  # pragma: no cover - only on a real engine stall
        pytest.fail(f"benchmark jobs did not reach a final state within {timeout:g}s")

    # One more tick delivers the terminal notices for every finished job.
    _tick(db, readiness)
    _wait_for(
        lambda: all(notices_for_job(notice_log, job_id) for job_id in jobs.values()),
        timeout=10.0,
        message="every benchmark job produced a completion notice",
    )

    results: list[TaskResult] = []
    for task in TASKS:
        job_id = jobs[task.task_id]
        job = get_job(db, job_id)
        assert job is not None
        view = job_status_view(db, job)
        accepted_commit: str | None = None
        accepted_at: float | None = None
        owner_actions = 0
        detail = ""
        if task.expect == "accepted":
            assert job.current_run_id is not None
            accepted = AcceptEngine(db=db).accept_run(job.current_run_id)
            assert accepted.accepted, f"{task.task_id}: {accepted}"
            accepted_commit = accepted.final_sha
            accepted_at = time.time()
            owner_actions = 1
        else:
            detail = str(view.get("reason") or job.failure_reason or "")
        notices = notices_for_job(notice_log, job_id)
        results.append(
            TaskResult(
                task_id=task.task_id,
                kind=task.kind,
                expect=task.expect,
                final_state=job.state.value,
                accepted_commit=accepted_commit,
                submitted_at=submitted_at[task.task_id],
                started_at=_started_at(db, job_id),
                terminal_at=_terminal_at(db, job_id),
                accepted_at=accepted_at,
                notice_at=notices[0] if notices else None,
                owner_actions=owner_actions,
                subscription=_subscription_for(db, job_id),
                detail=detail,
                block_verdicts=(
                    count_block_verdicts(db, job.current_run_id)
                    if job.current_run_id is not None
                    else 0
                ),
            )
        )

    return BenchmarkReport(
        mode="fake",
        results=tuple(results),
        started_at=started,
        finished_at=time.time(),
        usage=aggregate_subscription_usage(db, jobs.values()),
    )


# --------------------------------------------------------------------------
# Failure drills
# --------------------------------------------------------------------------


def _single_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail a killed worker's job after one attempt instead of retrying it."""
    from saberops.jobs import service

    original = service.job_dispatch_spec

    def once(job: Any) -> service.JobDispatchSpec:
        spec = original(job)
        return replace(spec, request=replace(spec.request, max_attempts=1))

    monkeypatch.setattr(service, "job_dispatch_spec", once)


def _tick_until_terminal(
    db: Any,
    readiness: ReadinessService,
    job_id: str,
    *,
    timeout: float,
    message: str,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _tick(db, readiness)
        if _job_terminal(job_id, db):
            return
        time.sleep(0.05)
    pytest.fail(f"{message} (waited {timeout:g}s)")


def _wait_dispatched(
    rig: RigEnvironment,
    job_id: str,
    *,
    tick: bool,
    readiness: ReadinessService | None = None,
) -> str:
    """Wait until *job_id* owns a run; optionally drive in-process ticks."""
    db = rig.db()
    deadline = time.monotonic() + SETUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if tick:
            assert readiness is not None
            _tick(db, readiness)
        job = get_job(db, job_id)
        if job is not None and job.current_run_id is not None:
            return job.current_run_id
        time.sleep(0.05)
    pytest.fail("job was not dispatched")


def _submit(rig: RigEnvironment, *, goal: str, **kwargs: Any) -> str:
    return submit_job(rig.db(), repo=rig.repo, goal=goal, **kwargs).job.job_id


def _drill_result(
    name: str,
    *,
    started: float,
    job_id: str,
    db: Any,
    notice_log: Path,
    detail: str = "",
) -> DrillResult:
    job = get_job(db, job_id)
    assert job is not None
    view = job_status_view(db, job)
    last_failed = view.get("last_failed_attempt") or {}
    reason = (
        last_failed.get("reason")
        or view.get("reason")
        or job.failure_reason
        or ""
    )
    seconds = time.monotonic() - started
    notices = len(notices_for_job(notice_log, job_id))
    return DrillResult(
        name=name,
        passed=(
            job.state in {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED}
            and seconds <= DRILL_VISIBLE_LIMIT_SECONDS
            and notices == 1
        ),
        seconds=seconds,
        final_state=job.state.value,
        reason=str(reason),
        notices=notices,
        detail=detail or str(view.get("work_location") or ""),
    )


def run_failure_drills(
    rig: RigEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[DrillResult, ...]:
    """Kill worker, kill scheduler, exhaust quota, bad route, duplicate submit."""
    notice_log = install_stub_notifier(rig.root, monkeypatch)
    prepare_isolated_memory(rig, monkeypatch)
    readiness = ReadinessService.for_owner_state(owner_state_dir())
    db = rig.db()
    drills: list[DrillResult] = []

    # 1. Kill the worker process while it hangs.
    rig.set_worker_mode("hang", hang_seconds=120)
    _single_attempt(monkeypatch)
    started = time.monotonic()
    job_id = _submit(rig, goal="kill the worker drill")
    run_id = _wait_dispatched(rig, job_id, tick=True, readiness=readiness)
    try:
        _wait_for(
            lambda: _worker_started(db, run_id),
            timeout=SETUP_TIMEOUT_SECONDS,
            message="worker started",
        )
        identity = _worker_identity(db, run_id)
        os.kill(identity.pid, signal.SIGKILL)
        _tick_until_terminal(
            db,
            readiness,
            job_id,
            timeout=C2_RECOVERY_LIMIT_SECONDS,
            message="killed worker's job reached a final state",
        )
    finally:
        _cleanup_run_processes(db, run_id)
    _tick(db, readiness)
    drills.append(
        _drill_result(
            "kill worker", started=started, job_id=job_id, db=db, notice_log=notice_log
        )
    )

    # 2. Kill the scheduler process, then restart it.
    started = time.monotonic()
    rig.set_worker_mode("commit")
    scheduler = _start_scheduler(rig)
    try:
        job_id = _submit(rig, goal="kill the scheduler drill")
        run_id = _wait_dispatched(rig, job_id, tick=False)
        # Kill only after the launcher has persisted the run and its owner; a
        # kill inside the dispatch window is a separate engine defect (noted in
        # the lane notes), not the scheduler-restart drill.
        _wait_for(
            lambda: db.get_run(run_id) is not None
            and db.get_supervision(run_id) is not None,
            timeout=SETUP_TIMEOUT_SECONDS,
            message="run and its execution owner were created before the kill",
        )
        scheduler.proc.kill()
        scheduler.proc.wait(timeout=10)
        _wait_for(
            lambda: not scheduler_alive(db),
            timeout=10,
            message="scheduler lease released after the kill",
        )
        scheduler = _start_scheduler(rig)
        _wait_for(
            lambda: _job_terminal(job_id, db),
            timeout=DRILL_VISIBLE_LIMIT_SECONDS,
            message="job reached a final state after the scheduler restart",
        )
        final_state = get_job(db, job_id).state.value  # type: ignore[union-attr]
        # Let the restarted scheduler finish its notice transaction before we
        # stop it; a kill between delivery and commit is the documented
        # duplicate-on-restart window, not this drill.
        _wait_for(
            lambda: _notice_committed(db, job_id, final_state),
            timeout=10.0,
            message="the restarted scheduler committed the notice",
        )
    finally:
        _stop_scheduler(scheduler)
    _tick(db, readiness)
    drills.append(
        _drill_result(
            "kill scheduler",
            started=started,
            job_id=job_id,
            db=db,
            notice_log=notice_log,
        )
    )

    # 3. Exhaust the provider quota.
    started = time.monotonic()
    rig.set_worker_mode("quota_exhausted")
    job_id = _submit(rig, goal="quota exhaustion drill")
    _wait_dispatched(rig, job_id, tick=True, readiness=readiness)
    _tick_until_terminal(
        db,
        readiness,
        job_id,
        timeout=DRILL_VISIBLE_LIMIT_SECONDS,
        message="quota-exhausted job reached a final state",
    )
    _tick(db, readiness)
    drills.append(
        _drill_result(
            "quota exhausted",
            started=started,
            job_id=job_id,
            db=db,
            notice_log=notice_log,
        )
    )

    # 4. A route that cannot satisfy the requirements.
    started = time.monotonic()
    rig.set_worker_mode("commit")
    job_id = _submit(
        rig,
        goal="bad route drill",
        requirements=JobRequirements(required_capabilities=(Capability.RESUME.value,)),
    )
    _tick_until_terminal(
        db,
        readiness,
        job_id,
        timeout=DRILL_VISIBLE_LIMIT_SECONDS,
        message="unroutable job reached a final state",
    )
    _tick(db, readiness)
    drills.append(
        _drill_result(
            "bad route",
            started=started,
            job_id=job_id,
            db=db,
            notice_log=notice_log,
        )
    )

    # 5. Duplicate submission with one idempotency key.
    started = time.monotonic()
    first = submit_job(
        db,
        repo=rig.repo,
        goal="duplicate submission drill",
        idempotency_key="benchmark-duplicate",
    )
    second = submit_job(
        db,
        repo=rig.repo,
        goal="duplicate submission drill",
        idempotency_key="benchmark-duplicate",
    )
    assert second.created is False and second.job.job_id == first.job.job_id
    try:
        submit_job(
            db,
            repo=rig.repo,
            goal="a different request under the same key",
            idempotency_key="benchmark-duplicate",
        )
    except Exception as exc:  # the engine's typed conflict is the visible state
        conflict = str(exc)
        assert "benchmark-duplicate" in conflict
    else:  # pragma: no cover - the engine must refuse a conflicting key
        pytest.fail("a conflicting duplicate submission was accepted")
    job_id = first.job.job_id
    _wait_dispatched(rig, job_id, tick=True, readiness=readiness)
    _tick_until_terminal(
        db,
        readiness,
        job_id,
        timeout=DRILL_VISIBLE_LIMIT_SECONDS,
        message="duplicate-submission job reached a final state",
    )
    _tick(db, readiness)
    duplicate_rows = _jobs_with_idempotency_key(db, "benchmark-duplicate")
    assert duplicate_rows == 1
    drills.append(
        _drill_result(
            "duplicate submission",
            started=started,
            job_id=job_id,
            db=db,
            notice_log=notice_log,
            detail=f"{duplicate_rows} job row for the key",
        )
    )

    return tuple(drills)


def _worker_started(db: Any, run_id: str) -> bool:
    supervision = db.get_supervision(run_id)
    return bool(
        supervision
        and supervision.worker_pid
        and supervision.worker_identity
        and supervision.current_attempt_id
    )


def _worker_identity(db: Any, run_id: str) -> ProcessIdentity:
    supervision = db.get_supervision(run_id)
    assert supervision is not None and supervision.worker_identity
    identity = ProcessIdentity.from_json(supervision.worker_identity)
    assert identity is not None
    return identity


def _notice_committed(db: Any, job_id: str, state: str) -> bool:
    """True once the desktop-notice ledger has committed this job's key."""
    path = Path(db.db_path)
    ledger = path.with_name(path.name + ".notifications.sqlite3")
    if not ledger.exists():
        return False
    try:
        with sqlite3.connect(ledger, timeout=5) as conn:
            row = conn.execute(
                "SELECT 1 FROM sent WHERE event_key = ?",
                (f"job:{job_id}:{state}",),
            ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def _jobs_with_idempotency_key(db: Any, key: str) -> int:
    with db._get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE idempotency_key = ?", (key,)
        ).fetchone()
    return int(row["n"])


def _cleanup_run_processes(db: Any, run_id: str) -> None:
    supervision = db.get_supervision(run_id)
    identities: list[ProcessIdentity] = []
    if supervision is not None and supervision.worker_identity:
        identity = ProcessIdentity.from_json(supervision.worker_identity)
        if identity is not None:
            identities.append(identity)
    owner, _ = RunSupervisor(db=db).owner_evidence(run_id)
    if owner is not None and owner.identity is not None:
        identities.append(owner.identity)
    for identity in identities:
        if probe_liveness(identity) is Liveness.LIVE:
            try:
                os.kill(identity.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


# --------------------------------------------------------------------------
# Combined recovery drill (B3 evidence, fake worker + stub notifier)
# --------------------------------------------------------------------------


def run_recovery_drill(
    rig: RigEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> DrillResult:
    """Close the planner, kill the worker, restart the scheduler, catch up.

    A fresh MCP session must see the failed job, its reason and the preserved
    work location within C2's 60-second limit.  A second scheduler tick must not
    duplicate the terminal event or the desktop notice.
    """
    notice_log = install_stub_notifier(rig.root, monkeypatch)
    prepare_isolated_memory(rig, monkeypatch)
    db = rig.db()
    rig.set_worker_mode("hang", hang_seconds=120)
    _single_attempt(monkeypatch)

    started = time.monotonic()
    run_id: str | None = None
    scheduler: _SchedulerProcess | None = None
    try:
        # The submitting planner session is closed immediately after submission.
        job_id = _submit(
            rig,
            goal="recovery drill: planner closes while the worker runs",
            mcp_session_id="planner-session-closed",
        )
        scheduler = _start_scheduler(rig)
        run_id = _wait_dispatched(rig, job_id, tick=False)
        _wait_for(
            lambda: _worker_started(db, run_id),
            timeout=SETUP_TIMEOUT_SECONDS,
            message="worker started",
        )
        attempts = db.get_attempts_for_run(run_id)
        worktree = Path(attempts[-1].worktree_path)
        assert worktree.exists()
        marker = worktree / "preserved-partial-work.txt"
        marker.write_text("unfinished recovery work\n", encoding="utf-8")

        # Kill the fake worker, then kill and restart the scheduler.
        killed_attempts: set[str] = set()

        def kill_live_worker() -> None:
            supervision = db.get_supervision(run_id)
            if (
                supervision is None
                or not supervision.current_attempt_id
                or not supervision.worker_identity
                or supervision.current_attempt_id in killed_attempts
            ):
                return
            identity = ProcessIdentity.from_json(supervision.worker_identity)
            if identity is None or probe_liveness(identity) is not Liveness.LIVE:
                return
            attempt = next(
                (
                    item
                    for item in db.get_attempts_for_run(run_id)
                    if item.id == supervision.current_attempt_id
                ),
                None,
            )
            if attempt is not None:
                attempt_worktree = Path(attempt.worktree_path)
                if attempt_worktree.exists():
                    (attempt_worktree / "preserved-partial-work.txt").write_text(
                        "unfinished recovery work\n", encoding="utf-8"
                    )
            os.kill(identity.pid, signal.SIGKILL)
            killed_attempts.add(supervision.current_attempt_id)

        kill_live_worker()
        scheduler.proc.kill()
        scheduler.proc.wait(timeout=10)
        _wait_for(
            lambda: not scheduler_alive(db),
            timeout=10,
            message="scheduler stopped before restart",
        )
        scheduler = _start_scheduler(rig)
        # The job may retry inside its own attempt budget; each killed attempt
        # is the same injected failure, so keep killing until the run ends.
        deadline = time.monotonic() + C2_RECOVERY_LIMIT_SECONDS
        while time.monotonic() < deadline and not _job_terminal(job_id, db):
            kill_live_worker()
            time.sleep(0.05)
        assert _job_terminal(job_id, db), (
            "job reached a final state within C2's 60-second limit"
        )

        # A fresh MCP session has no memory of the closed planner session.
        context = ServerContext(db_path=Path(db.db_path))
        registry = build_default_registry(context)
        snapshot = registry.call("catch_up", {"job_id": job_id})
        assert snapshot["state"] == "FAILED", snapshot
        assert snapshot["failure"], snapshot
        assert snapshot["failure"]["reason"], snapshot
        work_location = Path(str(snapshot["failure"]["work_location"]))
        assert work_location.is_dir(), snapshot
        assert (work_location / "preserved-partial-work.txt").read_text(
            encoding="utf-8"
        ) == "unfinished recovery work\n"
        assert marker.read_text(encoding="utf-8") == "unfinished recovery work\n"

        # Wait for the first delivery, let the restarted scheduler take more
        # ticks, then count again: each event and notice must occur once.
        _wait_for(
            lambda: len(notices_for_job(notice_log, job_id)) >= 1,
            timeout=10.0,
            message="the failed job produced its desktop notice",
        )
        terminal_events = _terminal_event_count(db, job_id)
        notices = len(notices_for_job(notice_log, job_id))
        time.sleep(1.5)
        terminal_events_after = _terminal_event_count(db, job_id)
        notices_after = len(notices_for_job(notice_log, job_id))
        elapsed = time.monotonic() - started
        assert terminal_events == 1 and terminal_events_after == 1
        assert notices == 1 and notices_after == 1
        assert elapsed <= C2_RECOVERY_LIMIT_SECONDS
        return DrillResult(
            name="combined recovery",
            passed=True,
            seconds=elapsed,
            final_state="FAILED",
            reason=str(snapshot["failure"]["reason"]),
            notices=notices_after,
            detail=(
                f"terminal events {terminal_events_after}, notices {notices_after}, "
                f"work location {snapshot['failure']['work_location']}"
            ),
        )
    finally:
        _stop_scheduler(scheduler)
        if run_id is not None:
            _cleanup_run_processes(db, run_id)


def _terminal_event_count(db: Any, job_id: str) -> int:
    terminal = {
        JobState.SUCCEEDED.value,
        JobState.FAILED.value,
        JobState.CANCELLED.value,
    }
    return sum(
        1 for event in store.list_events(db, job_id) if event.get("to_state") in terminal
    )


# --------------------------------------------------------------------------
# Real scheduler process helpers (used by the kill/recovery drills)
# --------------------------------------------------------------------------


@dataclass
class _SchedulerProcess:
    proc: subprocess.Popen[Any]
    log_handle: Any

    def close(self) -> None:
        self.log_handle.close()


def _start_scheduler(rig: RigEnvironment) -> _SchedulerProcess:
    log_path = rig.root / "scheduler-drill.log"
    handle = open(log_path, "ab", buffering=0)
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "saberops.cli",
            "scheduler",
            "run",
            "--db",
            str(rig.db().db_path),
            "--interval-seconds",
            "0.5",
        ],
        cwd=str(rig.repo),
        env=dict(os.environ),
        stdin=subprocess.DEVNULL,
        stdout=handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        _wait_for(
            lambda: scheduler_alive(rig.db()),
            timeout=30.0,
            message=f"scheduler acquired its lease (see {log_path})",
        )
    except BaseException:
        handle.close()
        if proc.poll() is None:
            proc.kill()
        raise
    return _SchedulerProcess(proc=proc, log_handle=handle)


def _stop_scheduler(scheduler: _SchedulerProcess | None) -> None:
    if scheduler is None:
        return
    if scheduler.proc.poll() is None:
        scheduler.proc.terminate()
        try:
            scheduler.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            scheduler.proc.kill()
            scheduler.proc.wait(timeout=10)
    scheduler.close()
