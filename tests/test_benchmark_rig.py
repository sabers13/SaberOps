"""Phase 1 benchmark rig (lane B2): fake-mode table, drills and real-mode guards.

The fake-mode tests drive the lifecycle rig's fake worker and reviewer through
the real job, scheduler, worker, gate, review and accept path.  They never start
a vendor CLI.  The real-mode tests only exercise the refusal guards.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from benchmark_rig import (
    C2_RECOVERY_LIMIT_SECONDS,
    DRILL_VISIBLE_LIMIT_SECONDS,
    run_failure_drills,
    run_fake_benchmark,
    run_recovery_drill,
)
from benchmark_wave3 import (
    assert_drill_groups,
    drills_report,
    run_advisor_drill,
    run_autonomy_drills,
    run_landing_queue_drill,
    run_plan_drills,
    run_presence_drills,
    run_question_mode_drills,
    run_scheduling_drills,
    run_scout_proxy_drills,
    run_vendor_drills,
    run_walkthrough_drill,
    usage_rows,
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
from saberops.jobs.models import JobState, WaitingReason
from saberops.jobs.service import get_job, submit_job
from saberops.model_access.readiness_service import ReadinessService
from saberops.ownership import Liveness, ProcessIdentity, probe_liveness
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

    # OpenCode Go reports the real 2.3.2 counter set: exact input/output/cache
    # counters per call, no invented total, and no identity guess.
    usage_by_subscription = {row.subscription: row for row in report.usage}
    opencode = usage_by_subscription["opencode"]
    assert opencode.jobs == 5
    assert opencode.calls > 0
    assert opencode.unknown_calls == 0
    assert opencode.input_tokens == 12 * opencode.calls
    assert opencode.output_tokens == 8 * opencode.calls
    assert opencode.cache_read_tokens == 4 * opencode.calls
    assert opencode.cache_write_tokens == 2 * opencode.calls
    assert opencode.total_tokens is None
    # The table states the missing provider total instead of inventing one.
    assert "not reported" in report.table()


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
# Wave 3 drills (lane B2b)
# --------------------------------------------------------------------------


def test_wave3_question_mode_drills(
    rig: RigEnvironment,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """F10/F11: critical-only answers routine questions; away parks one branch."""
    drills = run_question_mode_drills(rig)
    report = drills_report("fake wave 3", drills)
    with capsys.disabled():
        print(report.table())
    assert_drill_groups(drills, ["F10", "F11"])
    assert "Failure and feature drills" in report.table()


def test_wave3_plan_drills(
    rig: RigEnvironment,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """F8/F9/F12: dependency order, a mid-run edit and the landing queue."""
    drills = list(run_plan_drills(rig))
    drills.append(run_landing_queue_drill(rig))
    report = drills_report("fake wave 3", drills)
    with capsys.disabled():
        print(report.table())
    assert_drill_groups(drills, ["F8", "F9", "F12"])


def test_wave3_scheduling_drills(
    rig: RigEnvironment,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """F4/F13/F14/F2: quota failover, steering, one nudge and per-subscription usage."""
    drills = run_scheduling_drills(rig)
    report = drills_report("fake wave 3", drills, usage=usage_rows(rig))
    table = report.table()
    with capsys.disabled():
        print(table)
    assert_drill_groups(drills, ["F13", "F14", "F2", "F4"])
    assert "Usage per subscription (F2)" in table
    assert "codex" in table and "opencode" in table


def test_wave3_vendor_and_cleanup_drills(
    rig: RigEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """F5/F18/F6: the update guard, self-healing and landed worktree cleanup."""
    drills = run_vendor_drills(rig, tmp_path)
    report = drills_report("fake wave 3", drills)
    with capsys.disabled():
        print(report.table())
    assert_drill_groups(drills, ["F5", "F18", "F6"])


def test_wave3_autonomy_drills(
    rig: RigEnvironment,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """F1/F15/F16: owner modes, auto-accept and an imported native attempt."""
    drills = run_autonomy_drills(rig)
    report = drills_report("fake wave 3", drills)
    with capsys.disabled():
        print(report.table())
    assert_drill_groups(drills, ["F1", "F15", "F16"])


@pytest.mark.no_attendance
def test_wave3_presence_drills(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """F17/F24: a closed session or terminal pauses work until confirmed."""
    rig = build_lifecycle_rig(tmp_path, monkeypatch, attended=False)
    monkeypatch.delenv("SABEROPS_RUNTIME_GATE", raising=False)
    monkeypatch.setenv("SABEROPS_PLANNING_HEARTBEAT_TIMEOUT", "60")
    try:
        drills = run_presence_drills(rig)
        report = drills_report("fake wave 3", drills)
        with capsys.disabled():
            print(report.table())
        assert_drill_groups(drills, ["F17/F24", "F17/F24"])
    finally:
        try:
            from saberops.mcp.lease import OrchestratorLease

            OrchestratorLease().release()
        except Exception:  # noqa: BLE001 - cleanup never fails the drill
            pass


def test_wave3_scout_and_proxy_drills(
    rig: RigEnvironment,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """F19/F27/F20/F21: a cheap scout report and proxy routing by family token."""
    drills = run_scout_proxy_drills(rig)
    report = drills_report("fake wave 3", drills)
    with capsys.disabled():
        print(report.table())
    assert_drill_groups(drills, ["F19/F27", "F20/F21"])


def test_wave3_walkthrough_drill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """F25: only the walkthrough's own prompts are needed for a green preflight."""
    drill = run_walkthrough_drill(tmp_path, monkeypatch)
    report = drills_report("fake wave 3", [drill])
    with capsys.disabled():
        print(report.table())
    assert_drill_groups([drill], ["F25"])


def test_wave3_advisor_drill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """F26: each advisor checkpoint fires once and its advice reaches the worker."""
    drill = run_advisor_drill(tmp_path, monkeypatch)
    report = drills_report("fake wave 3", [drill])
    with capsys.disabled():
        print(report.table())
    assert_drill_groups([drill], ["F26"])


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
# Dispatch-window kill (defect found by the kill-scheduler drill, lane C2B)
# --------------------------------------------------------------------------


def _dead_scheduler_holder() -> str:
    """Lease holder identity of a scheduler process that is provably gone."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    identity = ProcessIdentity.for_pid(proc.pid)
    assert identity is not None
    proc.wait()
    assert probe_liveness(identity) is Liveness.DEAD
    return "process:" + json.dumps(identity.to_dict())


def _reservation_states(db: object, run_id: str) -> list[str]:
    with db._get_connection() as conn:  # type: ignore[attr-defined]
        rows = conn.execute(
            "SELECT state FROM capacity_reservations WHERE run_id = ? ORDER BY id",
            (run_id,),
        ).fetchall()
    return [str(row["state"]) for row in rows]


def test_scheduler_killed_inside_the_dispatch_window_recovers(
    rig: RigEnvironment,
) -> None:
    """A kill between the dispatch CAS and the run row must not strand the job.

    The residue is exactly what the dispatcher durably leaves when it dies in
    that window: the job is RUNNING with the ``dispatched`` event carrying the
    dead scheduler's lease generation, its capacity row is RESERVED, and no run
    row exists.  A restarted scheduler must release the reservation and requeue
    the never-started job once, then dispatch it on a fresh run.
    """
    db = rig.db()
    submitted = submit_job(db, repo=rig.repo, goal="dispatch window kill")
    job_id = submitted.job.job_id
    run_id = "run_dispatch_window"
    dispatched_at = datetime.now(UTC)
    store.mark_dispatched(
        db,
        job_id,
        run_id,
        dispatch_context={
            "holder_id": _dead_scheduler_holder(),
            "fencing_token": 4,
            "lease_expires_at": (dispatched_at + timedelta(seconds=30)).isoformat(),
            "run_store": str(Path(db.db_path).resolve()),
        },
        now=dispatched_at,
    )
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
    # The restarted scheduler runs well past the dead dispatch's lease expiry.
    restarted_at = dispatched_at + timedelta(seconds=60)

    # The restart repairs the residue and re-dispatches the never-started job
    # in the same tick: release the reserved capacity, requeue once, dispatch.
    first = run_scheduler_loop(
        db, once=True, readiness_service=readiness, now_fn=lambda: restarted_at
    )
    assert first is not None and first.ran
    assert first.dispatch_window_repaired == (job_id,)
    assert first.dispatched == 1

    job = get_job(db, job_id)
    assert job is not None
    assert job.state is JobState.RUNNING
    assert job.current_run_id not in (None, run_id)
    new_run_id = job.current_run_id
    assert db.get_run(new_run_id) is not None

    # The dead dispatch's reservation is released and the requeue is recorded.
    assert _reservation_states(db, run_id) == ["RELEASED"]
    requeued = [
        event for event in store.list_events(db, job_id) if event["kind"] == "requeued"
    ]
    assert requeued, "the repair is recorded, not silent"
    assert json.loads(requeued[-1]["payload_json"])["waiting_reason"] == (
        WaitingReason.DISPATCH_WINDOW_LOST.value
    )

    # It really ran: the fresh run completes, and the next tick settles the job.
    rig.wait_for_run(new_run_id, timeout=60.0)
    third = run_scheduler_loop(db, once=True, readiness_service=readiness)
    assert third is not None and third.ran
    final = get_job(db, job_id)
    assert final is not None and final.state is JobState.SUCCEEDED
