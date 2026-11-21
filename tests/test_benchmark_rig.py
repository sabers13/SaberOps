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
    DRILL_TASKS,
    QUESTION_TASK,
    TASKS,
    TASKS_BY_ID,
    RealModeRefused,
    TaskResult,
    configure_real_reviewer,
    create_toy_repository,
    isolated_environment,
    prepare_real_store,
    real_batches,
    real_family_requirements,
    real_mode_presence,
    real_model_for,
    real_worker_candidates,
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
            "XDG_DATA_HOME": "/home/owner/.local/share",
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
    # D2: OpenCode keeps its sign-in under XDG_DATA_HOME; real mode must not
    # redirect it into the isolated store.
    assert env["XDG_DATA_HOME"] == "/home/owner/.local/share"
    # D3: one scheduler driver.  Real mode ticks in-process.
    assert env["SABEROPS_DISABLE_AUTO_SCHEDULER"] == "1"


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
