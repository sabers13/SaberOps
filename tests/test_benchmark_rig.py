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
import textwrap
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from benchmark_rig import (
    BENCHMARK_TIMEOUT_SECONDS,
    C2_RECOVERY_LIMIT_SECONDS,
    DRILL_VISIBLE_LIMIT_SECONDS,
    install_stub_notifier,
    run_failure_drills,
    run_fake_benchmark,
    run_gate_protection_drill,
    run_recovery_drill,
)
from benchmark_wave3 import (
    assert_catalogued_drills,
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
    DEFAULT_SUBMIT_CONCURRENCY,
    DRILL_TASKS,
    QUESTION_TASK,
    TARGET_SUBMIT_TO_START_P95_SECONDS,
    TASKS,
    TASKS_BY_ID,
    BenchmarkReport,
    BenchmarkTask,
    RealModeRefused,
    SubmitPacer,
    TaskResult,
    apply_isolated_environment,
    benchmark_gate_command,
    configure_real_reviewer,
    create_toy_repository,
    format_real_mode_preflight,
    gate_interpreter_pytest_error,
    isolated_environment,
    median_value,
    metered_credential_keys,
    nearest_rank_percentile,
    parse_per_cli_selector,
    prepare_real_store,
    probe_must_fail_acceptance,
    real_batches,
    real_family_requirements,
    real_mode_presence,
    real_model_for,
    real_worker_candidates,
    run_real_benchmark,
    select_benchmark_tasks,
    select_benchmark_tasks_by_cli,
    unclassified_metered_credentials,
    validate_real_mode,
    wait_for_batch,
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
    # D21: the catalogue also carries a drill task that forces a routine
    # question, so critical-only self-answering is measurable.
    assert [task.task_id for task in DRILL_TASKS] == ["routine-question"]
    assert QUESTION_TASK in DRILL_TASKS
    assert "question" in QUESTION_TASK.goal.lower()


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
        # N3: the expectation is "the reviewer blocked at least once", which
        # the fake reviewer records durably just as a real one does.
        assert result.block_verdicts >= 1
    # Both fake subscriptions are exercised.
    providers = {result.subscription.split("/")[0] for result in report.results}
    assert providers == {"codex", "opencode"}
    # Every task measured submit-to-start and a completion notice.
    for result in report.results:
        assert result.started_at is not None
        assert result.notice_at is not None
    # The drive loop has its own BENCHMARK_TIMEOUT_SECONDS stall bound; paced
    # submission now places each task's submission work on the critical path, so
    # this end-to-end bound adds room for setup, acceptance and notice delivery
    # under the gate's parallel load while still catching a runaway run.
    assert report.elapsed < BENCHMARK_TIMEOUT_SECONDS + 60

    # Paced submission is the default: the next task waits for a free worker
    # slot, so no task grows a queue wait.  On the pre-pacing code the same
    # catalogue was submitted at once and the later rows measured 14-28s of
    # queue wait here, so these bounds fail before the change and pass after.
    assert report.submission_mode == "paced"
    assert report.submit_concurrency == DEFAULT_SUBMIT_CONCURRENCY
    assert "submission: paced" in report.table()
    assert 1 <= report.max_submit_depth <= DEFAULT_SUBMIT_CONCURRENCY
    submit_to_start = nearest_rank_percentile(
        [
            result.submit_to_start
            for result in accepted
            if result.submit_to_start is not None
        ],
        95,
    )
    assert submit_to_start is not None
    # The p95 queue wait must stay within one task's own service time (never
    # above the absolute target while tasks are slower than it).  This is the
    # queue-bound proof and it scales with the host, so a loaded parallel gate
    # cannot fail it for being slow.
    service_seconds = median_value(
        result.terminal_at - result.started_at
        for result in accepted
        if result.terminal_at is not None and result.started_at is not None
    )
    assert service_seconds is not None
    queue_wait_bound = max(
        TARGET_SUBMIT_TO_START_P95_SECONDS, service_seconds
    )
    assert submit_to_start <= queue_wait_bound, report.table()
    # Target rows are informational in fake mode but still show the measurement.
    assert {row.name for row in report.target_rows()} == {
        "time to accepted, median",
        "time to accepted, p95",
        "submit to start, p95",
    }

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
    assert_catalogued_drills(drills)
    print("failure drills:")
    for drill in drills:
        print(
            f"  {drill.name:<20} {drill.final_state:<10} "
            f"{drill.seconds:>6.1f}s notices={drill.notices} {drill.reason}"
        )


def test_gate_protection_must_fail_drill(
    rig: RigEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drill = run_gate_protection_drill(rig, monkeypatch)
    assert drill.passed, drill
    assert drill.final_state == "FAILED"
    assert "GATE_PROTECTED_PATH_MUTATED" in drill.reason


def test_combined_recovery_drill(
    rig: RigEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    drill = run_recovery_drill(rig, monkeypatch)
    assert drill.passed, drill
    assert drill.seconds <= C2_RECOVERY_LIMIT_SECONDS
    assert drill.notices == 1
    assert drill.final_state == "FAILED"
    assert "terminal events 1" in drill.detail
    assert_catalogued_drills([drill])
    print(
        f"  {'combined recovery':<20} {drill.final_state:<10} "
        f"{drill.seconds:>6.1f}s notices={drill.notices} {drill.reason}"
    )


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
    """F26: a worker asks on demand within the budget; the fourth call is refused."""
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
            "XDG_DATA_HOME": "/home/owner/.local/share",
            "OPENAI_API_KEY": "secret",
            "ANTHROPIC_API_KEY": "secret",
            "GEMINI_API_KEY": "secret",
            "CODEX_API_KEY": "secret",
            "TOKENROUTER_API_KEY": "secret",
            "YDC_API_KEY": "secret",
            "SOME_UNKNOWN_GATEWAY_API_KEY": "secret",
            "PATH": "/usr/bin",
        },
    )
    assert env["HOME"] == "/home/owner"
    for key in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "CODEX_API_KEY",
        "TOKENROUTER_API_KEY",
        "YDC_API_KEY",
        "SOME_UNKNOWN_GATEWAY_API_KEY",
    ):
        assert key not in env
    assert env["XDG_STATE_HOME"] == str(store / "state")
    assert env["XDG_CONFIG_HOME"] == str(store / "config")
    assert env["XDG_CACHE_HOME"] == str(store / "cache")
    # D2: OpenCode keeps its sign-in under XDG_DATA_HOME; real mode must not
    # redirect it into the isolated store.
    assert env["XDG_DATA_HOME"] == "/home/owner/.local/share"
    # D3: one scheduler driver.  Real mode ticks in-process.
    assert env["SABEROPS_DISABLE_AUTO_SCHEDULER"] == "1"


def test_metered_credential_class_covers_known_and_shaped_keys() -> None:
    """N6: the metered class names every known gateway plus the API-key shape."""
    base = {
        "TOKENROUTER_API_KEY": "secret",
        "YDC_API_KEY": "secret",
        "OPENAI_API_KEY": "secret",
        "GEMINI_API_KEY": "secret",
        "CODEX_ACCESS_TOKEN": "secret",
        "SOME_FUTURE_VENDOR_API_KEY": "secret",
        "PATH": "/usr/bin",
        "ANTHROPIC_AUTH_TOKEN": "not-an-api-key-name",
    }
    assert metered_credential_keys(base) == (
        "CODEX_ACCESS_TOKEN",
        "GEMINI_API_KEY",
        "OPENAI_API_KEY",
        "SOME_FUTURE_VENDOR_API_KEY",
        "TOKENROUTER_API_KEY",
        "YDC_API_KEY",
    )
    assert unclassified_metered_credentials(base) == ("SOME_FUTURE_VENDOR_API_KEY",)


def test_validate_real_mode_refuses_an_unclassified_api_key(
    tmp_path: Path,
) -> None:
    """N6 fail-safe: an unknown metered key aborts before any vendor CLI runs."""
    store = tmp_path / "isolated"
    base = {"HOME": "/home/owner", "ACME_GATEWAY_API_KEY": "sk-never-print-me"}
    with pytest.raises(RealModeRefused, match="ACME_GATEWAY_API_KEY") as refused:
        validate_real_mode(acknowledged=True, store=store, base=base)
    assert "sk-never-print-me" not in str(refused.value)


def test_validate_real_mode_accepts_only_the_known_metered_class(
    tmp_path: Path,
) -> None:
    store = tmp_path / "isolated"
    base = {
        "HOME": "/home/owner",
        "OPENAI_API_KEY": "secret",
        "TOKENROUTER_API_KEY": "secret",
        "YDC_API_KEY": "secret",
    }
    assert validate_real_mode(acknowledged=True, store=store, base=base) == store.resolve()


def test_apply_isolated_environment_removes_stripped_keys_in_place(
    tmp_path: Path,
) -> None:
    """N6: an update alone leaves a popped key behind; applying must delete it."""
    environ = {
        "HOME": "/home/owner",
        "PATH": "/usr/bin",
        "TOKENROUTER_API_KEY": "secret",
        "YDC_API_KEY": "secret",
        "OPENAI_API_KEY": "secret",
    }
    apply_isolated_environment(tmp_path / "store", environ=environ)
    assert "TOKENROUTER_API_KEY" not in environ
    assert "YDC_API_KEY" not in environ
    assert "OPENAI_API_KEY" not in environ
    assert environ["HOME"] == "/home/owner"
    assert environ["XDG_STATE_HOME"] == str(tmp_path / "store" / "state")


def test_isolated_environment_leaves_an_unset_data_home_alone(tmp_path: Path) -> None:
    env = isolated_environment(tmp_path / "store", base={"HOME": "/home/owner"})
    assert "XDG_DATA_HOME" not in env


def test_real_batches_run_ten_tasks_per_cli_with_a_different_vendor_reviewer() -> None:
    models = {"codex": "gpt-6-luna", "opencode": "oc-model", "claude": "claude-model"}
    batches = real_batches()
    assert [batch.subscription for batch in batches] == ["codex", "opencode", "claude"]
    for batch in batches:
        assert len(batch.tasks) == 10
        assert batch.reviewer_subscription != batch.subscription
        assert batch.reviewer_subscription in {"codex", "opencode", "anthropic"}
        assert real_model_for(batch.reviewer_subscription, models)
        # Review routing is independent of the worker's family filter, so a
        # different-vendor reviewer is always reachable.
        assert batch.reviewer_subscription in {"codex", "opencode", "anthropic"}


def test_configure_real_reviewer_selects_a_check_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The batch reviewer is an exact CHECK binding, not free provider text."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    from saberops.control_plane.binding_store import load_owner_bindings
    from saberops.control_plane.bindings import BindingRole

    binding_id = configure_real_reviewer("anthropic", DEFAULT_CLAUDE_MODEL)
    owner = load_owner_bindings()
    assert owner.reviewer_binding_ids == (binding_id,)
    binding = owner.registry.try_get(binding_id)
    assert binding is not None
    assert binding.binding_role is BindingRole.CHECK
    assert binding.provider == "anthropic"
    assert binding.model == DEFAULT_CLAUDE_MODEL


def test_real_routes_default_to_the_owner_models() -> None:
    assert DEFAULT_OPENCODE_MODEL == "opencode-go/deepseek-v4.1-flash"
    assert DEFAULT_CLAUDE_MODEL == "claude-haiku-4-5-20251001"
    assert DEFAULT_CODEX_MODEL == "gpt-6-luna"
    for subscription in ("codex", "opencode", "claude"):
        excluded = real_family_requirements(subscription)
        assert subscription not in excluded


def _git_status(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def test_toy_repository_gate_does_not_mutate_the_worktree(tmp_path: Path) -> None:
    """D1: the shipped gate imports app and must leave the worktree clean."""
    repo, _sha = create_toy_repository(
        tmp_path / "repos", "toy-add", task_id="add-function"
    )
    assert (repo / ".gitignore").exists()
    subprocess.run([sys.executable, "-c", "import app"], cwd=repo, check=True)
    assert _git_status(repo) == ""


def test_toy_repository_is_well_posed_for_every_task(tmp_path: Path) -> None:
    """D5: every catalogue task can actually be attempted on the seeded repo."""
    parent = tmp_path / "repos"
    repos = {
        task.task_id: create_toy_repository(
            parent, f"toy-{task.task_id}", task_id=task.task_id
        )[0]
        for task in TASKS
    }
    app = (repos["add-function"] / "app.py").read_text(encoding="utf-8")
    assert "def greet(name: str)" in app
    assert "def farewell" not in app
    cli = repos["cli-flag"] / "cli.py"
    assert cli.exists()
    cli_run = subprocess.run(
        [sys.executable, "cli.py", "Ada"],
        cwd=repos["cli-flag"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert cli_run.stdout.strip() == "hello Ada"

    # fix-failing-test alone starts red.
    failing = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "test_app.py"],
        cwd=repos["fix-failing-test"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert failing.returncode != 0
    passing = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "test_app.py"],
        cwd=repos["add-function"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert passing.returncode == 0, passing.stdout + passing.stderr

    # The validation task still has work to do, and the two must-fail-review
    # tasks start from real validation so their goals are meaningful.
    for task_id in ("validation", "review-block-a", "review-block-b"):
        repo = repos[task_id]
        empty = subprocess.run(
            [sys.executable, "-c", "import app; app.greet('')"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
        assert "ValueError" in empty.stderr, (task_id, empty.stderr)
    whitespace = subprocess.run(
        [sys.executable, "-c", "import app; print(app.greet('   '))"],
        cwd=repos["validation"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert whitespace.returncode == 0, whitespace.stderr


@pytest.mark.no_attendance
def test_real_mode_presence_establishes_attendance_and_clears_a_presence_pause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D3: real mode holds attendance and clears only an engine presence pause."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("SABEROPS_ATTENDANCE_DIR", str(tmp_path / "attendance"))
    from saberops.attendance import live_attendance
    from saberops.runtime_gate import evaluate_gate, note_presence_seen, set_pause

    note_presence_seen()
    set_pause("no_planning_session", "paused: no planning session", by="engine")
    assert evaluate_gate().closed is True
    assert live_attendance() is None

    with real_mode_presence():
        assert live_attendance() is not None
        assert evaluate_gate().closed is False
    assert live_attendance() is None


@pytest.mark.no_attendance
def test_real_mode_presence_never_clears_an_owner_pause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("SABEROPS_ATTENDANCE_DIR", str(tmp_path / "attendance"))
    from saberops.runtime_gate import evaluate_gate, set_pause

    set_pause("owner_pause", "paused by the owner", by="owner")
    with real_mode_presence():
        state = evaluate_gate()
        assert state.closed is True
        assert state.reason == "owner_pause"


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


def test_benchmark_script_dry_run_states_the_submission_mode(tmp_path: Path) -> None:
    """The plan names the default paced submission and the burst stress option."""
    script = Path(__file__).resolve().parents[1] / "scripts" / "benchmark.py"
    base = [
        sys.executable,
        str(script),
        "--mode",
        "real",
        "--yes-real-cli",
        "--dry-run",
    ]
    env = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
    }
    paced = subprocess.run(
        [*base, "--store", str(tmp_path / "paced"), "--concurrency", "3"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert paced.returncode == 0, paced
    assert "submission: paced" in paced.stdout
    assert "concurrency 3" in paced.stdout

    burst = subprocess.run(
        [*base, "--store", str(tmp_path / "burst"), "--submit", "burst"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert burst.returncode == 0, burst
    assert "submission: burst" in burst.stdout
    assert "stress only, not scored" in burst.stdout

    invalid = subprocess.run(
        [*base, "--store", str(tmp_path / "invalid"), "--concurrency", "0"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert invalid.returncode == 2
    assert "at least 1" in invalid.stderr


# --------------------------------------------------------------------------
# Shipped real-mode entry point on a fresh store (N1) and honest latency (N5)
# --------------------------------------------------------------------------


_FAKE_CODEX_CLI = """\
#!/usr/bin/env python3
import json
import sys

args = sys.argv[1:]
if args == ["--version"]:
    print("0.0.0-fake")
elif args == ["doctor", "--json"]:
    print(json.dumps({"schemaVersion": 1, "checks": {
        "auth.credentials": {"status": "ok"}}}))
elif args == ["debug", "models"]:
    print(json.dumps({"models": ["gpt-6-luna"]}))
elif args == ["--help"]:
    print("fake codex help")
else:
    print(f"fake codex: unexpected args {args!r}", file=sys.stderr)
    raise SystemExit(1)
"""

_FAKE_CLAUDE_CLI = """\
#!/usr/bin/env python3
import json
import sys

args = sys.argv[1:]
if args == ["--version"]:
    print("0.0.0-fake")
elif args == ["auth", "status", "--json"]:
    print(json.dumps({"loggedIn": True}))
elif "--input-format" in args:
    request = json.loads(sys.stdin.readline())
    reply = {"type": "control_response", "response": {
        "subtype": "success",
        "request_id": request.get("request_id"),
        "response": {"models": [{"value": "claude-haiku-4-5-20251001"}]},
    }}
    print(json.dumps(reply), flush=True)
elif args == ["--help"]:
    print("fake claude help")
else:
    print(f"fake claude: unexpected args {args!r}", file=sys.stderr)
    raise SystemExit(1)
"""

_FAKE_OPENCODE_CLI = """\
#!/usr/bin/env python3
import json
import sys

args = sys.argv[1:]
if args == ["--version"]:
    print("0.0.0-fake")
elif args == ["auth", "list", "--format", "json", "--standalone"]:
    print(json.dumps([{"id": "opencode-go", "connections": [
        {"id": "cred_fake", "type": "credential"}]}]))
elif args in (["models"], ["models", "--standalone"]):
    print("opencode-go/deepseek-v4.1-flash")
elif args == ["--help"]:
    print("fake opencode help")
else:
    print(f"fake opencode: unexpected args {args!r}", file=sys.stderr)
    raise SystemExit(1)
"""


def _install_fake_vendor_clis(root: Path) -> Path:
    """Write discovery-capable fake ``codex``/``claude``/``opencode`` executables."""
    bin_dir = root / "fake-bin"
    bin_dir.mkdir(parents=True)
    for name, body in (
        ("codex", _FAKE_CODEX_CLI),
        ("claude", _FAKE_CLAUDE_CLI),
        ("opencode", _FAKE_OPENCODE_CLI),
    ):
        path = bin_dir / name
        path.write_text(textwrap.dedent(body), encoding="utf-8")
        path.chmod(0o755)
    return bin_dir


def _load_benchmark_script() -> object:
    import importlib.util

    script = Path(__file__).resolve().parents[1] / "scripts" / "benchmark.py"
    spec = importlib.util.spec_from_file_location("saberops_benchmark_entry", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _StopAfterSubmission(Exception):
    """Sentinel that stops the shipped entry point at its first job submission."""


@pytest.mark.no_attendance
def test_benchmark_script_real_mode_prepares_a_fresh_store_and_submits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N1: the shipped real path reaches its first job on a fresh store.

    The fake vendor CLIs answer only the documented discovery, sign-in status
    and model-listing seams, so this proves preparation -- not a real provider
    prompt.  ``submit_job`` is the first durable write the run needs; stopping
    there keeps the test provider-free while still exercising the real
    discovery -> routing -> posture -> quota -> preflight chain.
    """
    bin_dir = _install_fake_vendor_clis(tmp_path)
    store = tmp_path / "store"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("TMPDIR", "/var/tmp")
    # The shipped path installs the isolated store into ``os.environ``; pinning
    # these through monkeypatch keeps teardown from leaking it into later tests.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "ambient-state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "ambient-config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "ambient-cache"))
    monkeypatch.delenv("SABEROPS_PROFILE", raising=False)

    submitted: list[dict[str, object]] = []

    def stop_submit(db: object, **kwargs: object) -> None:
        submitted.append(kwargs)
        raise _StopAfterSubmission

    monkeypatch.setattr("saberops.jobs.service.submit_job", stop_submit)
    script = _load_benchmark_script()

    with pytest.raises(_StopAfterSubmission):
        script.main(  # type: ignore[attr-defined]
            ["--mode", "real", "--yes-real-cli", "--store", str(store)]
        )

    assert len(submitted) == 1
    first = TASKS_BY_ID["add-function"]
    assert submitted[0]["goal"] == first.goal
    assert submitted[0]["acceptance"] == first.acceptance
    assert Path(str(submitted[0]["repo"])).name == f"toy-{first.task_id}"

    models = {
        "codex": DEFAULT_CODEX_MODEL,
        "opencode": DEFAULT_OPENCODE_MODEL,
        "claude": DEFAULT_CLAUDE_MODEL,
    }
    from saberops.routing_config import load_effective_routing_payload

    payload = load_effective_routing_payload()
    routes = [route for group in payload["roles"]["BUILD"] for route in group["routes"]]
    for candidate in real_worker_candidates(models):
        assert candidate in routes

    from saberops.control_plane.policy import load_control_plane_policy
    from saberops.control_plane.posture import posture_decision
    from saberops.db import Database

    policy = load_control_plane_policy()
    for provider, model in (
        ("codex", models["codex"]),
        ("opencode", models["opencode"]),
        ("anthropic", models["claude"]),
    ):
        assert posture_decision(
            provider, model, plans=policy.plans, today=datetime.now(UTC).date()
        ).admitted_unattended

    db = Database()
    quota_rows = {(row.provider, row.pool) for row in db.list_quota()}
    assert ("codex", "chatgpt-plus") in quota_rows
    assert ("opencode", "opencode-go") in quota_rows
    assert ("anthropic", "claude-pro") in quota_rows
    # Reviewer bindings are discovered pools, the identity the recheck had to
    # record by hand (N1/N2).
    for pool in (
        "discovered-pool:claude-code-subscription",
        "discovered-pool:codex-chatgpt-account",
        "discovered-pool:opencode-account",
    ):
        assert any(pool == row[1] for row in quota_rows), pool


@pytest.mark.no_attendance
def test_prepare_real_store_is_idempotent_on_its_own_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N1: a second preparation neither aborts nor rewrites the routing document."""
    bin_dir = _install_fake_vendor_clis(tmp_path)
    store = tmp_path / "store"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("TMPDIR", "/var/tmp")
    monkeypatch.setenv("XDG_STATE_HOME", str(store / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(store / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(store / "cache"))
    monkeypatch.delenv("SABEROPS_PROFILE", raising=False)

    from saberops.db import Database
    from saberops.routing_config import load_effective_routing_payload

    models = {
        "codex": DEFAULT_CODEX_MODEL,
        "opencode": DEFAULT_OPENCODE_MODEL,
        "claude": DEFAULT_CLAUDE_MODEL,
    }
    reviewers = ("anthropic", "opencode", "codex")
    db = Database()
    prepare_real_store(db=db, models=models, reviewer_subscriptions=reviewers)
    first = load_effective_routing_payload()

    # Before N1 this raised "User routing config already exists ... use --force".
    prepare_real_store(db=db, models=models, reviewer_subscriptions=reviewers)
    assert load_effective_routing_payload() == first


def _review_blocked_result(*, block_verdicts: int) -> TaskResult:
    return TaskResult(
        task_id="review-block-a",
        kind="must fail review",
        expect="review_blocked",
        final_state="SUCCEEDED",
        accepted_commit=None,
        submitted_at=100.0,
        started_at=101.0,
        terminal_at=110.0,
        accepted_at=None,
        notice_at=111.0,
        owner_actions=0,
        subscription="codex/gpt-6-luna",
        block_verdicts=block_verdicts,
    )


def test_review_blocked_expectation_scores_the_block_verdict() -> None:
    """N3: a BLOCK verdict is the measurement, not an unattainable FAILED state."""
    blocked = _review_blocked_result(block_verdicts=1)
    assert blocked.passed, "a real BLOCK followed by a legitimate repair still passes"
    assert _review_blocked_result(block_verdicts=0).passed is False
    assert blocked.time_to_candidate == pytest.approx(10.0)


def test_must_fail_probe_is_deterministic_and_gates_the_row(tmp_path: Path) -> None:
    """N14: a must-fail row cannot pass on a reviewer block alone.

    The probe reads the exact candidate tree: a candidate that still raises
    ``ValueError`` for an empty name is compliant (False), so a reviewer BLOCK
    on it is not evidence of the must-fail guard.
    """
    repo = tmp_path / "probe-repo"
    repo.mkdir()

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init", "-b", "main")
    git("config", "user.email", "probe@example.com")
    git("config", "user.name", "Probe")
    (repo / "app.py").write_text(
        'def greet(name: str) -> str:\n'
        '    if name == "":\n'
        '        raise ValueError("name must not be empty")\n'
        '    return f"hello {name}"\n',
        encoding="utf-8",
    )
    git("add", ".")
    git("commit", "-m", "compliant")
    compliant_sha = git("rev-parse", "HEAD")

    (repo / "app.py").write_text(
        'def greet(name: str) -> str:\n    return f"hello {name}"\n',
        encoding="utf-8",
    )
    git("add", ".")
    git("commit", "-m", "violating")
    violating_sha = git("rev-parse", "HEAD")

    assert probe_must_fail_acceptance(repo, compliant_sha) is False
    assert probe_must_fail_acceptance(repo, violating_sha) is True
    assert probe_must_fail_acceptance(repo, "0" * 40) is None

    blocked_on_compliant = replace(
        _review_blocked_result(block_verdicts=1),
        acceptance_violation=False,
    )
    assert blocked_on_compliant.passed is False
    blocked_on_violation = replace(
        _review_blocked_result(block_verdicts=1),
        acceptance_violation=True,
    )
    assert blocked_on_violation.passed is True


def test_probe_without_a_violation_is_not_measured() -> None:
    """N14-rig: a compliant candidate means there was nothing to catch.

    A reviewer block on a candidate whose acceptance still holds cannot fail
    the suite (the worker chose acceptance); the row is excluded, not failed.
    Before the fix the same row was measured and failed the whole benchmark.
    """
    from saberops.benchmark import BenchmarkReport

    compliant_block = replace(
        _review_blocked_result(block_verdicts=1),
        review_count=1,
        acceptance_violation=False,
    )
    assert compliant_block.measured is False
    assert compliant_block.passed is False

    measured_pass = replace(
        _review_blocked_result(block_verdicts=1),
        review_count=1,
        acceptance_violation=True,
    )
    assert measured_pass.measured is True
    assert measured_pass.passed is True
    report = BenchmarkReport(
        mode="real",
        results=(compliant_block, measured_pass),
        started_at=0.0,
        finished_at=1.0,
    )
    assert report.passed is True
    assert "not measured" in report.table()


def test_per_cli_selector_accepts_task_ids_per_batch() -> None:
    """N13 residual: each CLI may name task ids, not only a prefix count."""
    spec = parse_per_cli_selector(
        "codex=rename,review-block-a,opencode=10,claude=review-block-b"
    )
    assert spec == {
        "codex": ("rename", "review-block-a"),
        "opencode": 10,
        "claude": ("review-block-b",),
    }
    selected, skipped = select_benchmark_tasks_by_cli(spec)
    assert [task.task_id for task in selected["codex"]] == [
        "rename",
        "review-block-a",
    ]
    assert [task.task_id for task in selected["claude"]] == ["review-block-b"]
    assert len(selected["opencode"]) == 10
    assert len(skipped["codex"]) == 8
    assert "review-block-a" not in {task.task_id for task in skipped["codex"]}

    with pytest.raises(RealModeRefused, match="unknown benchmark task"):
        select_benchmark_tasks_by_cli({"codex": ("not-a-task",)})
    with pytest.raises(RealModeRefused, match="cannot be mixed"):
        parse_per_cli_selector("codex=2,review-block-a")
    with pytest.raises(RealModeRefused, match="more than once"):
        parse_per_cli_selector("codex=rename,codex=review-block-a")


def test_benchmark_gate_uses_the_running_interpreter_and_runs_pytest() -> None:
    """N3/§12.5 + D2: no ``python`` alias, and the gate executes the tests."""
    import shlex

    command = benchmark_gate_command("fix-failing-test")
    assert shlex.split(command) == [sys.executable, "-B", "-m", "pytest", "-q"]
    assert not command.startswith("python ")


def test_cli_flag_gate_also_invokes_the_built_cli() -> None:
    """D2: the CLI acceptance is exercised, not just pytest."""
    import shlex

    argv = shlex.split(benchmark_gate_command("cli-flag"))
    assert argv[:3] == [sys.executable, "-B", "-c"]
    script = argv[3]
    assert "'-m','pytest','-q'" in script
    assert "cli.main(['--name','Ada'])" in script


def test_gate_interpreter_pytest_check_passes_in_the_dev_environment() -> None:
    """D9: the documented dev recipe installs pytest for the toy gates."""
    assert gate_interpreter_pytest_error() is None


def test_gate_interpreter_pytest_check_names_the_dev_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D9: a runtime-only install fails once, with the install remedy."""

    def _missing(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="ModuleNotFoundError: No module named 'pytest'"
        )

    monkeypatch.setattr("saberops.benchmark.subprocess.run", _missing)
    message = gate_interpreter_pytest_error()
    assert message is not None
    assert sys.executable in message
    assert "cannot import pytest" in message
    assert "pip install '.[dev]'" in message


def test_real_benchmark_refuses_before_seeding_without_pytest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D9: fail before the first toy repository instead of every gate."""
    monkeypatch.setattr(
        "saberops.benchmark.gate_interpreter_pytest_error",
        lambda: "the benchmark gate interpreter cannot import pytest",
    )
    store = tmp_path / "store"
    with pytest.raises(RealModeRefused, match="cannot import pytest"):
        run_real_benchmark(
            store=store,
            repo_root=tmp_path / "repos",
            models={},
        )
    assert not store.exists()


def test_benchmark_gate_output_proves_the_fix_and_cli_acceptance(
    tmp_path: Path,
) -> None:
    """D2 replay: the exact gate now emits the output the reviewers demanded."""
    import shlex

    from saberops.process import run_process

    fix_repo, _ = create_toy_repository(
        tmp_path / "gate-fix", "toy-fix", task_id="fix-failing-test"
    )
    test_file = fix_repo / "test_app.py"
    test_file.write_text(
        test_file.read_text(encoding="utf-8").replace('"hi Ada"', '"hello Ada"'),
        encoding="utf-8",
    )
    fixed = run_process(
        shlex.split(benchmark_gate_command("fix-failing-test")),
        cwd=fix_repo,
        timeout=60.0,
    )
    assert fixed.exit_code == 0, fixed.stderr
    assert "2 passed" in fixed.stdout

    cli_repo, _ = create_toy_repository(
        tmp_path / "gate-cli", "toy-cli", task_id="cli-flag"
    )
    cli_file = cli_repo / "cli.py"
    cli_file.write_text(
        cli_file.read_text(encoding="utf-8")
        .replace(
            'parser.add_argument("name", nargs="?", default="world")',
            'parser.add_argument("name", nargs="?", default="world")\n'
            '    parser.add_argument("--name", dest="cli_name")',
        )
        .replace(
            "print(greet(args.name))",
            'print(greet(args.cli_name if args.cli_name is not None else args.name))',
        ),
        encoding="utf-8",
    )
    cli_gate = run_process(
        shlex.split(benchmark_gate_command("cli-flag")),
        cwd=cli_repo,
        timeout=60.0,
    )
    assert cli_gate.exit_code == 0, cli_gate.stderr
    assert "2 passed" in cli_gate.stdout
    assert "hello Ada" in cli_gate.stdout


def test_scheduler_finding_prints_as_information_in_benchmark_mode() -> None:
    """D5: real mode ticks in-process; it must not print [FAIL] scheduler."""
    from saberops.preflight import Finding

    lines = format_real_mode_preflight(
        [
            Finding("FAIL", "scheduler: not running", "orch scheduler start"),
            Finding("PASS", "git: clean"),
        ]
    )
    joined = "\n".join(lines)
    assert lines[0] == "real-mode preflight:"
    assert "[INFO] scheduler: in-process tick (not required)" in joined
    assert "[FAIL] scheduler" not in joined
    assert "[PASS] git: clean" in joined


def test_wait_for_batch_accepts_the_first_eligible_candidate() -> None:
    """N5: acceptance happens on arrival, not after the batch's slowest task."""
    tasks = (TASKS_BY_ID["add-function"], TASKS_BY_ID["review-block-a"])
    jobs = {"add-function": "job-a", "review-block-a": "job-b"}
    events: list[str] = []
    state = {"ticks": 0, "now": 0.0}

    def tick() -> None:
        state["ticks"] += 1
        events.append(f"tick-{state['ticks']}")

    def job_state(job_id: str) -> str:
        if job_id == "job-a":
            return "SUCCEEDED"
        return "SUCCEEDED" if state["ticks"] >= 2 else "RUNNING"

    def accept_job(job_id: str) -> str:
        events.append(f"accept-{job_id}")
        return "sha-a"

    def notice_delivered(job_id: str, final_state: str) -> bool:
        return state["ticks"] >= 2

    def monotonic() -> float:
        state["now"] += 0.1
        return state["now"]

    outcome = wait_for_batch(
        tasks=tasks,
        jobs=jobs,
        tick=tick,
        job_state=job_state,
        accept_job=accept_job,
        notice_delivered=notice_delivered,
        max_wait=10.0,
        monotonic=monotonic,
        sleep=lambda _seconds: None,
    )

    assert events.index("accept-job-a") < events.index("tick-2")
    assert "accept-job-b" not in events
    assert outcome.accepted["add-function"][0] == "sha-a"
    assert set(outcome.terminal) == {"add-function", "review-block-a"}


def _drive_with_pacer(
    pacer: SubmitPacer, task_count: int = 5
) -> tuple[list[str], list[int], list[int], set[str]]:
    """Drive a stub two-slot engine and record the queue depth at each submit."""
    tasks = TASKS[:task_count]
    jobs: dict[str, str] = {}
    states: dict[str, str] = {}
    submitted: list[str] = []
    depths: list[int] = []
    submissions_before_tick: list[int] = []
    clock = {"mono": 0.0}

    def in_flight() -> int:
        return sum(1 for state in states.values() if state != "SUCCEEDED")

    def submit(task: BenchmarkTask) -> str:
        task_id = task.task_id
        depths.append(in_flight())
        submitted.append(task_id)
        job_id = f"job-{task_id}"
        states[job_id] = "QUEUED"
        return job_id

    def tick() -> None:
        submissions_before_tick.append(len(submitted))
        # The stub engine finishes every running job, then starts queued work
        # on its two worker slots.
        for job_id, state in list(states.items()):
            if state == "RUNNING":
                states[job_id] = "SUCCEEDED"
        running = sum(1 for state in states.values() if state == "RUNNING")
        for job_id, state in list(states.items()):
            if state == "QUEUED" and running < 2:
                states[job_id] = "RUNNING"
                running += 1

    def monotonic() -> float:
        clock["mono"] += 0.1
        return clock["mono"]

    outcome = wait_for_batch(
        tasks=tasks,
        jobs=jobs,
        tick=tick,
        job_state=lambda job_id: states.get(job_id),
        accept_job=lambda job_id: f"sha-{job_id}",
        notice_delivered=lambda _job_id, _state: True,
        max_wait=100.0,
        monotonic=monotonic,
        sleep=lambda _seconds: None,
        submit=submit,
        pacer=pacer,
    )
    assert set(outcome.terminal) == set(submitted)
    assert outcome.max_submit_depth == (max(depths) + 1 if depths else 0)
    return submitted, depths, submissions_before_tick, set(jobs)


def test_paced_submission_fills_free_slots_instead_of_a_queue() -> None:
    """The shared driver submits into a free slot, never behind a queue.

    Burst keeps the old all-at-once behaviour: every task is submitted before
    the first tick, so the later rows accumulate queue wait.  Paced (the
    default) holds the depth below the configured worker count, which is what
    keeps submit-to-start bounded in the fake/rig benchmark.
    """
    concurrency = 2
    assert SubmitPacer("paced", concurrency).may_submit(concurrency - 1)
    assert not SubmitPacer("paced", concurrency).may_submit(concurrency)
    assert SubmitPacer("burst", concurrency).may_submit(99)
    with pytest.raises(ValueError):
        SubmitPacer(concurrency=0)
    with pytest.raises(ValueError):
        SubmitPacer(concurrency=True)

    order = [task.task_id for task in TASKS[:5]]
    paced_tasks, paced_depths, paced_before_tick, paced_jobs = _drive_with_pacer(
        SubmitPacer("paced", concurrency)
    )
    assert paced_tasks == order
    assert paced_jobs == set(order)
    assert max(paced_depths) < concurrency
    # The first tick sees only the tasks that fit in free slots.
    assert paced_before_tick[0] == concurrency

    burst_tasks, burst_depths, burst_before_tick, _jobs = _drive_with_pacer(
        SubmitPacer("burst", concurrency)
    )
    assert burst_tasks == order
    assert burst_depths == list(range(5))
    assert burst_before_tick[0] == len(order)


def test_batch_pacer_uses_the_route_pool_limit() -> None:
    """A batch's paced slots are min(global, its route's pool max_concurrent).

    Claude's packaged plan allows one concurrent job while Codex and OpenCode
    Go allow two, so the global default of two must not submit a second Claude
    task into a queued position.
    """
    from saberops.benchmark import batch_submit_pacer
    from saberops.control_plane.plans import (
        PlanSet,
        PoolMember,
        PoolPlan,
    )
    from saberops.control_plane.policy import (
        ControlPlanePolicy,
        packaged_control_plane_policy,
    )
    from saberops.models import WorkerCandidate

    global_pacer = SubmitPacer("paced", DEFAULT_SUBMIT_CONCURRENCY)
    packaged = packaged_control_plane_policy()
    codex = WorkerCandidate("codex", "gpt-6-luna")
    opencode = WorkerCandidate("opencode", "opencode-go/deepseek-v4.1-flash")
    claude = WorkerCandidate("anthropic", "claude-haiku-4-5-20251001")

    assert batch_submit_pacer(global_pacer, codex, policy=packaged).concurrency == 2
    assert (
        batch_submit_pacer(global_pacer, opencode, policy=packaged).concurrency == 2
    )
    assert batch_submit_pacer(global_pacer, claude, policy=packaged).concurrency == 1
    # A lower global setting still wins.
    assert (
        batch_submit_pacer(
            SubmitPacer("paced", 1), codex, policy=packaged
        ).concurrency
        == 1
    )
    # Burst keeps its always-submit behaviour whatever the pool allows.
    assert batch_submit_pacer(
        SubmitPacer("burst", 2), claude, policy=packaged
    ).may_submit(9)

    # An owner plan override wins over the packaged limit.
    owner_capped = ControlPlanePolicy(
        packaged.provider_policies,
        {},
        "test-policy-hash",
        PlanSet(
            pool_plans={
                "codex-one": PoolPlan(
                    pool_id="codex-one",
                    members=(PoolMember(route="codex"),),
                    max_concurrent=1,
                )
            }
        ),
    )
    assert (
        batch_submit_pacer(global_pacer, codex, policy=owner_capped).concurrency == 1
    )


def test_paced_batch_never_queues_behind_a_one_slot_pool(
    rig: RigEnvironment,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """With a pool limit of 1 the second task is submitted only when it is free.

    The owner plan is overridden to one codex slot while the global paced
    default stays two.  The engine must never record a ``CONCURRENCY`` wait for
    the batch, which is the failure the confirming run exposed when Claude's
    one-slot pool was paced at the global two.
    """
    from saberops.benchmark import (
        RealBatch,
        _drive_real_batch,
        _worker_submitter,
        batch_submit_pacer,
        batch_worker_candidate,
        create_toy_repository,
    )
    from saberops.cli import main as cli_main
    from saberops.control_plane.plans import load_packaged_plans_raw
    from saberops.control_plane.policy import (
        _read_raw_doc,
        load_control_plane_policy,
        save_owner_control_plane,
    )
    from saberops.jobs import store as jobs_store

    packaged = load_packaged_plans_raw()
    entry = dict(packaged["quota_pools"]["chatgpt-plus"])
    entry.pop("account", None)
    entry["max_concurrent"] = 1
    # Preserve the rig's posture attestations: a full owner document replaces
    # every section it carries.
    owner_doc = _read_raw_doc()
    owner_doc.setdefault("version", 2)
    raw_pools = owner_doc.get("quota_pools")
    pools = dict(raw_pools) if isinstance(raw_pools, dict) else {}
    pools["chatgpt-plus"] = entry
    owner_doc["quota_pools"] = pools
    save_owner_control_plane(owner_doc)
    policy = load_control_plane_policy()
    assert policy.plans.pool_plans["chatgpt-plus"].max_concurrent == 1

    models = {"codex": "gpt-5.6-terra"}
    candidate = batch_worker_candidate("codex", models)
    global_pacer = SubmitPacer("paced", DEFAULT_SUBMIT_CONCURRENCY)
    assert global_pacer.concurrency == 2
    assert (
        batch_submit_pacer(global_pacer, candidate, policy=policy).concurrency == 1
    )

    tasks = (TASKS_BY_ID["add-function"], TASKS_BY_ID["rename"])
    parent = tmp_path / "one-slot-repos"
    repos: dict[str, Path] = {}
    for task in tasks:
        repo, _sha = create_toy_repository(
            parent, f"toy-{task.task_id}", task_id=task.task_id
        )
        assert (
            cli_main(["project", "set-gate", "--repo", str(repo), rig.gate_command])
            == 0
        )
        repos[task.task_id] = repo
    rig.set_reviewer_verdict("PASS")
    # The batch driver waits for the engine's completion-notice attempt; the
    # suite disables desktop delivery, so give it the same local stub the fake
    # benchmark uses instead of waiting for a notice that can never arrive.
    install_stub_notifier(rig.root, monkeypatch)

    db = rig.db()
    jobs: dict[str, str] = {}
    submitted_at: dict[str, float] = {}
    all_jobs: list[str] = []
    outcome = _drive_real_batch(
        batch=RealBatch("codex", "anthropic", tasks),
        db=db,
        readiness=ReadinessService.for_owner_state(owner_state_dir()),
        jobs=jobs,
        max_wait=120.0,
        worker_candidate=candidate,
        policy=policy,
        submit=_worker_submitter(
            db=db,
            repos=repos,
            subscription="codex",
            submitted_at=submitted_at,
            all_jobs=all_jobs,
        ),
        pacer=global_pacer,
    )

    assert outcome.submit_concurrency == 1
    assert outcome.max_submit_depth == 1
    assert set(outcome.accepted) == {task.task_id for task in tasks}
    for job_id in jobs.values():
        reasons = [
            json.loads(event["payload_json"] or "{}").get("waiting_reason")
            for event in jobs_store.list_events(db, job_id)
            if event["kind"] == "waiting"
        ]
        assert WaitingReason.CONCURRENCY.value not in reasons


def _accepted_task(*, start_after: float, accepted_after: float) -> TaskResult:
    """One accepted row with known submit->start and submit->accept latencies."""
    return TaskResult(
        task_id="add-function",
        kind="add a function",
        expect="accepted",
        final_state="SUCCEEDED",
        accepted_commit="abcdef123456",
        submitted_at=1000.0,
        started_at=1000.0 + start_after,
        terminal_at=1000.0 + accepted_after - 1.0,
        accepted_at=1000.0 + accepted_after,
        notice_at=1000.0 + accepted_after + 0.1,
        owner_actions=1,
        subscription="codex/gpt-6-luna",
    )


def test_report_scores_final_targets_only_for_a_paced_real_run() -> None:
    """Targets score from submit under pacing; burst is a labelled stress figure."""
    passing = (
        _accepted_task(start_after=1.0, accepted_after=10.0),
        _accepted_task(start_after=2.0, accepted_after=20.0),
        _accepted_task(start_after=3.0, accepted_after=30.0),
        _accepted_task(start_after=4.0, accepted_after=100.0),
    )
    report = BenchmarkReport(
        mode="real",
        results=passing,
        started_at=1000.0,
        finished_at=1200.0,
        submission_mode="paced",
        submit_concurrency=2,
        batch_slot_limits=(("codex", 2), ("opencode", 2), ("claude", 1)),
    )
    assert report.targets_scored is True
    assert report.targets_passed is True
    assert report.passed is True
    assert median_value([10.0, 20.0, 30.0, 100.0]) == 25.0
    assert nearest_rank_percentile([10.0, 20.0, 30.0, 100.0], 95) == 100.0
    table = report.table()
    assert "submission: paced" in table
    # Queue wait is part of the scored submit-to-start figure, and each batch's
    # pool-capped slot count is printed beside the deepest queue.
    assert "queue wait is included in submit-to-start" in table
    assert "slots codex=2, opencode=2, claude=1" in table
    assert "target verdict: PASS" in table
    # From-start is printed next to the from-submit figure (100s vs 96s).
    for result in report.results:
        assert result.time_to_accepted_from_start is not None
    assert "96.0s" in table

    queued = (
        passing[0],
        passing[1],
        passing[2],
        _accepted_task(start_after=50.0, accepted_after=100.0),
    )
    queued_report = BenchmarkReport(
        mode="real",
        results=queued,
        started_at=1000.0,
        finished_at=1200.0,
        submission_mode="paced",
        submit_concurrency=2,
    )
    assert queued_report.targets_passed is False
    assert queued_report.passed is False
    assert "target verdict: FAIL" in queued_report.table()

    stress = BenchmarkReport(
        mode="real",
        results=queued,
        started_at=1000.0,
        finished_at=1200.0,
        submission_mode="burst",
        submit_concurrency=2,
    )
    stress_table = stress.table()
    assert stress.targets_scored is False
    assert stress.passed is True, "the stress section never fails the run"
    assert "submission: burst" in stress_table
    assert "stress section: not scored" in stress_table

    fake = BenchmarkReport(
        mode="fake",
        results=passing,
        started_at=1000.0,
        finished_at=1200.0,
        submission_mode="paced",
    )
    assert fake.targets_scored is False
    assert "not scored" in fake.table()


def test_benchmark_table_reports_candidate_and_acceptance_latency() -> None:
    """N5: both honest times are visible in the printed table."""
    results = (
        TaskResult(
            task_id="add-function",
            kind="add a function",
            expect="accepted",
            final_state="SUCCEEDED",
            accepted_commit="abcdef123456",
            submitted_at=100.0,
            started_at=101.0,
            terminal_at=140.0,
            accepted_at=141.0,
            notice_at=141.5,
            owner_actions=1,
            subscription="codex/gpt-6-luna",
        ),
    )
    from saberops.benchmark import BenchmarkReport

    table = BenchmarkReport(
        mode="real", results=results, started_at=100.0, finished_at=200.0
    ).table()
    assert "to candidate" in table
    assert "to accepted" in table
    assert "40.0s" in table
    assert "41.0s" in table


def test_benchmark_table_withholds_candidate_latency_without_a_candidate() -> None:
    """N8: submit-to-terminal is not printed as time to candidate."""
    from saberops.benchmark import BenchmarkReport

    result = TaskResult(
        task_id="review-block-b",
        kind="must fail review",
        expect="review_blocked",
        final_state="FAILED",
        accepted_commit=None,
        submitted_at=100.0,
        started_at=None,
        terminal_at=293.9,
        accepted_at=None,
        notice_at=294.0,
        owner_actions=0,
        subscription="codex/gpt-6-luna",
        block_verdicts=0,
        review_count=0,
        candidate_produced=False,
    )
    assert result.time_to_candidate is None
    table = BenchmarkReport(
        mode="real", results=(result,), started_at=100.0, finished_at=300.0
    ).table()
    assert "to candidate" in table
    assert "193.9s" not in table
    assert "not measured" in table


def test_must_fail_row_without_review_is_not_measured_not_failed() -> None:
    """N10: no review and no block is an unmeasured row, never a failure."""
    from saberops.benchmark import BenchmarkReport

    unmeasured = TaskResult(
        task_id="review-block-b",
        kind="must fail review",
        expect="review_blocked",
        final_state="FAILED",
        accepted_commit=None,
        submitted_at=100.0,
        started_at=None,
        terminal_at=190.0,
        accepted_at=None,
        notice_at=191.0,
        owner_actions=0,
        subscription="codex/gpt-6-luna",
        block_verdicts=0,
        review_count=0,
        candidate_produced=False,
    )
    measured = TaskResult(
        task_id="review-block-a",
        kind="must fail review",
        expect="review_blocked",
        final_state="SUCCEEDED",
        accepted_commit=None,
        submitted_at=100.0,
        started_at=101.0,
        terminal_at=200.0,
        accepted_at=None,
        notice_at=201.0,
        owner_actions=0,
        subscription="codex/gpt-6-luna",
        block_verdicts=1,
        review_count=2,
    )
    assert unmeasured.measured is False
    assert measured.measured is True
    report = BenchmarkReport(
        mode="real",
        results=(unmeasured, measured),
        started_at=100.0,
        finished_at=300.0,
    )
    assert report.passed is True
    assert "not measured" in report.table()

    # A review that happened but returned no BLOCK is still a measured failure:
    # the fix must not make blocking easier to fake.
    reviewed_no_block = TaskResult(
        task_id="review-block-b",
        kind="must fail review",
        expect="review_blocked",
        final_state="SUCCEEDED",
        accepted_commit="abcdef123456",
        submitted_at=100.0,
        started_at=101.0,
        terminal_at=190.0,
        accepted_at=191.0,
        notice_at=192.0,
        owner_actions=1,
        subscription="codex/gpt-6-luna",
        block_verdicts=0,
        review_count=1,
    )
    assert reviewed_no_block.measured is True
    assert reviewed_no_block.passed is False
    assert (
        BenchmarkReport(
            mode="real",
            results=(reviewed_no_block,),
            started_at=100.0,
            finished_at=300.0,
        ).passed
        is False
    )


def test_reduced_catalogue_selection_is_documented_and_ordered() -> None:
    """N11: --tasks/--per-cli narrow the catalogue and name skipped rows."""
    selected, skipped = select_benchmark_tasks(
        ["docs", "add-function"]
    )
    assert [task.task_id for task in selected] == ["add-function", "docs"]
    assert "review-block-b" in {task.task_id for task in skipped}

    first_two, rest = select_benchmark_tasks(per_cli=2)
    assert first_two == TASKS[:2]
    assert rest == TASKS[2:]

    with pytest.raises(RealModeRefused, match="unknown benchmark task"):
        select_benchmark_tasks(["not-a-task"])
    with pytest.raises(RealModeRefused, match="not both"):
        select_benchmark_tasks(["docs"], per_cli=1)
    with pytest.raises(RealModeRefused, match="at least 1"):
        select_benchmark_tasks(per_cli=0)

    batches = real_batches(selected)
    assert all(batch.tasks == selected for batch in batches)


def test_benchmark_script_dry_run_prints_skipped_rows(tmp_path: Path) -> None:
    """N11: a reduced confirmation run is a documented, visible option."""
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
            "--tasks",
            "docs,review-block-b",
            "--store",
            str(store),
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
    )
    assert result.returncode == 0, result
    assert "reduced catalogue" in result.stdout
    assert "skipping 8" in result.stdout
    assert "tasks: docs, review-block-b" in result.stdout
    assert "tasks: add-function" not in result.stdout
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


def test_per_cli_selector_varies_the_reduced_catalogue_per_batch() -> None:
    """N13: each CLI can get its own count while plain forms keep working."""
    assert parse_per_cli_selector("2") == 2
    spec = parse_per_cli_selector("opencode=10,codex=2,claude=2")
    assert spec == {"opencode": 10, "codex": 2, "claude": 2}

    selected, skipped = select_benchmark_tasks_by_cli(spec)
    assert len(selected["opencode"]) == 10
    assert len(selected["codex"]) == 2
    assert len(selected["claude"]) == 2
    assert skipped["codex"] == TASKS[2:]
    assert skipped["opencode"] == ()

    batches = real_batches(selected)
    by_subscription = {batch.subscription: batch.tasks for batch in batches}
    assert by_subscription["opencode"] == TASKS
    assert by_subscription["codex"] == TASKS[:2]
    assert by_subscription["claude"] == TASKS[:2]

    with pytest.raises(RealModeRefused, match="unknown CLI"):
        parse_per_cli_selector("gemini=2")
    with pytest.raises(RealModeRefused, match="more than once"):
        parse_per_cli_selector("codex=2,codex=3")
    with pytest.raises(RealModeRefused, match="at least 1"):
        select_benchmark_tasks_by_cli({"codex": 0})


def test_benchmark_script_accepts_task_ids_per_cli(tmp_path: Path) -> None:
    """N13 residual: a batch can name its task ids and still prints the split."""
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
            "--per-cli",
            "codex=rename,review-block-a,opencode=10,claude=2",
            "--store",
            str(store),
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
    )
    assert result.returncode == 0, result
    assert (
        "reduced catalogue [codex batch]: running 2 task(s); skipping 8"
        in result.stdout
    )
    assert "tasks: rename, review-block-a" in result.stdout
    assert "reduced catalogue [opencode batch]: running 10 task(s); skipping 0: -" in (
        result.stdout
    )
    assert not store.exists()


def test_benchmark_script_prints_a_count_for_each_batch(tmp_path: Path) -> None:
    """N13: the reduced-run print says which batch each count applies to."""
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
            "--per-cli",
            "opencode=10,codex=2,claude=2",
            "--store",
            str(store),
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
    )
    assert result.returncode == 0, result
    assert "reduced catalogue [opencode batch]: running 10 task(s); skipping 0: -" in (
        result.stdout
    )
    assert "reduced catalogue [codex batch]: running 2 task(s); skipping 8" in (
        result.stdout
    )
    assert "reduced catalogue [claude batch]: running 2 task(s); skipping 8" in (
        result.stdout
    )
    assert not store.exists()
