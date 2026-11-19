"""Phase 1 benchmark rig (lane B2): fake-mode table, drills and real-mode guards.

The fake-mode tests drive the lifecycle rig's fake worker and reviewer through
the real job, scheduler, worker, gate, review and accept path.  They never start
a vendor CLI.  The real-mode tests only exercise the refusal guards.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from benchmark_rig import (
    C2_RECOVERY_LIMIT_SECONDS,
    DRILL_VISIBLE_LIMIT_SECONDS,
    run_failure_drills,
    run_fake_benchmark,
    run_recovery_drill,
)
from lifecycle_rig import RigEnvironment, build_lifecycle_rig

from saberops.benchmark import (
    DEFAULT_CLAUDE_MODEL,
    DEFAULT_CODEX_MODEL,
    DEFAULT_OPENCODE_MODEL,
    TASKS,
    RealModeRefused,
    isolated_environment,
    real_family_requirements,
    real_subscription,
    validate_real_mode,
)
from saberops.capacity.loop import run_scheduler_loop
from saberops.capacity.store import ensure_tables as ensure_capacity_tables
from saberops.db import current_iso_timestamp
from saberops.jobs import store
from saberops.jobs.models import JobState
from saberops.jobs.service import get_job, submit_job
from saberops.model_access.readiness_service import ReadinessService
from saberops.paths import owner_state_dir

pytestmark = [
    pytest.mark.packaged_routing_default,
    pytest.mark.slow,
    pytest.mark.serial,
]


@pytest.fixture()
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RigEnvironment:
    return build_lifecycle_rig(tmp_path, monkeypatch)


# --------------------------------------------------------------------------
# Fake mode: the ten fixed tasks
# --------------------------------------------------------------------------


def test_benchmark_catalogue_has_ten_fixed_tasks() -> None:
    assert len(TASKS) == 10
    assert len({task.task_id for task in TASKS}) == 10
    assert sum(1 for task in TASKS if task.expect == "review_blocked") == 2
    assert sum(1 for task in TASKS if task.expect == "accepted") == 8
    assert {task.family for task in TASKS} == {"codex", "opencode"}
    kinds = {task.kind for task in TASKS}
    for expected in (
        "add a function",
        "fix a failing test",
        "rename",
        "small refactor",
        "docs",
        "must fail review",
    ):
        assert expected in kinds


def test_fake_benchmark_ten_tasks_reach_final_state(
    rig: RigEnvironment,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = run_fake_benchmark(rig, monkeypatch)
    with capsys.disabled():
        print(report.table())
    assert "SaberOps phase 1 benchmark (fake mode)" in report.table()

    assert len(report.results) == 10
    assert report.passed, report.table()
    accepted = [result for result in report.results if result.expect == "accepted"]
    blocked = [result for result in report.results if result.expect == "review_blocked"]
    assert len(accepted) == 8 and len(blocked) == 2
    for result in accepted:
        assert result.final_state == "SUCCEEDED"
        assert result.accepted_commit
        assert result.owner_actions == 1
    for result in blocked:
        assert result.final_state == "FAILED"
        assert result.accepted_commit is None
        assert result.detail
    # Both fake subscriptions are exercised.
    providers = {result.subscription.split("/")[0] for result in report.results}
    assert providers == {"codex", "opencode"}
    # Every task measured submit-to-start and a completion notice.
    for result in report.results:
        assert result.started_at is not None
        assert result.notice_at is not None
    assert report.elapsed < 120


# --------------------------------------------------------------------------
# Failure drills
# --------------------------------------------------------------------------


def test_failure_drills_reach_visible_state(
    rig: RigEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drills = run_failure_drills(rig, monkeypatch)
    names = [drill.name for drill in drills]
    assert names == [
        "kill worker",
        "kill scheduler",
        "quota exhausted",
        "bad route",
        "duplicate submission",
    ]
    for drill in drills:
        assert drill.passed, drill
        assert drill.seconds <= DRILL_VISIBLE_LIMIT_SECONDS
        assert drill.notices == 1, drill
        assert drill.reason or drill.detail
    by_name = {drill.name: drill for drill in drills}
    assert "exited with code" in by_name["kill worker"].reason
    assert "quota" in by_name["quota exhausted"].reason.lower()
    assert "NO_ROUTE_FOR_REQUIREMENTS" in by_name["bad route"].reason
    assert by_name["kill scheduler"].final_state == "SUCCEEDED"
    assert by_name["duplicate submission"].final_state == "SUCCEEDED"
    print("failure drills:")
    for drill in drills:
        print(
            f"  {drill.name:<20} {drill.final_state:<10} "
            f"{drill.seconds:>6.1f}s notices={drill.notices} {drill.reason}"
        )


def test_combined_recovery_drill(
    rig: RigEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    drill = run_recovery_drill(rig, monkeypatch)
    assert drill.passed, drill
    assert drill.seconds <= C2_RECOVERY_LIMIT_SECONDS
    assert drill.notices == 1
    assert drill.final_state == "FAILED"
    assert "terminal events 1" in drill.detail


# --------------------------------------------------------------------------
# Real mode guards (never starts a vendor CLI)
# --------------------------------------------------------------------------


def test_real_mode_refuses_without_the_explicit_flag(tmp_path: Path) -> None:
    with pytest.raises(RealModeRefused, match="--yes-real-cli"):
        validate_real_mode(acknowledged=False, store=tmp_path / "store")


def test_real_mode_refuses_the_owner_store(tmp_path: Path) -> None:
    owner_state = tmp_path / "home" / ".local" / "state" / "saberops"
    owner_state.mkdir(parents=True)
    with pytest.raises(RealModeRefused, match="owner's real SaberOps store"):
        validate_real_mode(
            acknowledged=True, store=owner_state, home=tmp_path / "home"
        )


def test_real_mode_refuses_a_nonempty_store(tmp_path: Path) -> None:
    store = tmp_path / "store"
    store.mkdir()
    (store / "leftover").write_text("x", encoding="utf-8")
    with pytest.raises(RealModeRefused, match="non-empty store"):
        validate_real_mode(acknowledged=True, store=store)


def test_real_mode_accepts_an_empty_isolated_store(tmp_path: Path) -> None:
    store = tmp_path / "isolated"
    assert validate_real_mode(acknowledged=True, store=store) == store.resolve()


def test_isolated_environment_moves_state_and_drops_pay_per_token_keys(
    tmp_path: Path,
) -> None:
    store = tmp_path / "store"
    env = isolated_environment(
        store,
        base={
            "HOME": "/home/owner",
            "OPENAI_API_KEY": "secret",
            "ANTHROPIC_API_KEY": "secret",
            "GEMINI_API_KEY": "secret",
            "CODEX_API_KEY": "secret",
            "PATH": "/usr/bin",
        },
    )
    assert env["HOME"] == "/home/owner"
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "CODEX_API_KEY"):
        assert key not in env
    assert env["XDG_STATE_HOME"] == str(store / "state")
    assert env["XDG_CONFIG_HOME"] == str(store / "config")
    assert env["XDG_CACHE_HOME"] == str(store / "cache")
    assert env["XDG_DATA_HOME"] == str(store / "data")


def test_real_routes_default_to_the_owner_models() -> None:
    assert DEFAULT_OPENCODE_MODEL == "opencode-go/deepseek-v4.1-flash"
    assert DEFAULT_CLAUDE_MODEL == "claude-haiku-5-5"
    assert DEFAULT_CODEX_MODEL == "gpt-6-luna"
    assert {real_subscription(index) for index in range(10)} == {
        "codex",
        "opencode",
        "claude",
    }
    for subscription in ("codex", "opencode", "claude"):
        excluded = real_family_requirements(subscription)
        assert subscription not in excluded


def test_benchmark_script_real_mode_refuses_without_flag(tmp_path: Path) -> None:
    script = Path(__file__).resolve().parents[1] / "scripts" / "benchmark.py"
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--mode",
            "real",
            "--store",
            str(tmp_path / "store"),
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
    )
    assert result.returncode == 2, result
    assert "--yes-real-cli" in result.stderr
    assert not (tmp_path / "store").exists()


def test_benchmark_script_real_mode_dry_run_creates_nothing(tmp_path: Path) -> None:
    script = Path(__file__).resolve().parents[1] / "scripts" / "benchmark.py"
    store = tmp_path / "store"
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--mode",
            "real",
            "--yes-real-cli",
            "--dry-run",
            "--store",
            str(store),
            "--codex-model",
            "custom-codex-model",
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
    )
    assert result.returncode == 0, result
    assert "nothing started" in result.stdout
    assert "custom-codex-model" in result.stdout
    assert not store.exists()


# --------------------------------------------------------------------------
# Engine defect exposed by the kill-scheduler drill (filed in the lane notes)
# --------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "engine defect: a scheduler killed between the dispatch CAS and run "
        "creation leaves a RUNNING job with a RESERVED capacity row but no run "
        "row; the next tick exits with running_run_status_unresolved and never "
        "repairs or fails the job, so the stall is silent"
    ),
)
def test_scheduler_killed_inside_the_dispatch_window_recovers(
    rig: RigEnvironment,
) -> None:
    """A dispatch-window kill must not leave a permanently RUNNING job."""
    db = rig.db()
    submitted = submit_job(db, repo=rig.repo, goal="dispatch window kill")
    job_id = submitted.job.job_id
    run_id = "run_dispatch_window"
    store.mark_dispatched(db, job_id, run_id)
    ensure_capacity_tables(db)
    now = current_iso_timestamp()
    with db._get_connection() as conn:
        conn.execute(
            "INSERT INTO capacity_reservations ("
            "id, run_id, job_id, route, pool_id, window_id, multiplier,"
            " size_bucket, cost_pct, spread_pct, state, total_tokens,"
            " created_at, settled_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "res_dispatch_window",
                run_id,
                job_id,
                "codex/gpt-5.6-terra",
                "chatgpt-plus",
                "5h",
                1.0,
                "M",
                8.0,
                4.0,
                "RESERVED",
                None,
                now,
                None,
            ),
        )
        conn.commit()

    readiness = ReadinessService.for_owner_state(owner_state_dir())
    for _ in range(3):
        run_scheduler_loop(db, once=True, readiness_service=readiness)

    job = get_job(db, job_id)
    assert job is not None and job.state in {JobState.FAILED, JobState.SUCCEEDED}
