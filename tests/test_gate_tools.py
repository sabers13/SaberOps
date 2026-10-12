"""Synthetic resource and evidence checks; no product/provider execution."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from types import ModuleType

import pytest


def load_tools() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "gate_tools", Path(__file__).resolve().parents[1] / "scripts/gate_tools.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


g = load_tools()


def owned(root: Path, name: str, pid: int, failed: bool = False) -> Path:
    path = root / name
    path.mkdir()
    (path / "owner.json").write_text(json.dumps({"pid": pid, "worktree": name, "failed": failed}))
    return path


def test_sweep(tmp_path: Path) -> None:
    dead = owned(tmp_path, "dead", 99999999)
    live = owned(tmp_path, "live", os.getpid())
    old = owned(tmp_path, "old", os.getpid())
    os.utime(old, (time.time() - 90000, time.time() - 90000))
    g.sweep(tmp_path)
    assert not dead.exists() and live.exists() and not old.exists()


def test_floor() -> None:
    assert g.required_free(100 * g.GB, 5) == 10 * g.GB
    assert g.required_free(10 * g.GB, 5) == 5 * g.GB


def test_keep_failed_cap(tmp_path: Path) -> None:
    old = owned(tmp_path, "old", os.getpid(), True)
    new = owned(tmp_path, "new", os.getpid(), True)
    (old / "data").write_bytes(b"x" * 100)
    (new / "data").write_bytes(b"x" * 100)
    os.utime(old, (1, 1))
    g.keep_failed(tmp_path, "new", g.size(new))
    assert not old.exists() and new.exists()
    g.keep_failed(tmp_path, "new", 0)
    assert not new.exists()


def test_slot_waits(tmp_path: Path) -> None:
    notices: list[str] = []
    started = time.monotonic()
    with g.slot(tmp_path, {"pid": os.getpid()}):
        with pytest.raises(TimeoutError):
            with g.slot(tmp_path, {}, wait=0.2, notify=notices.append):
                pytest.fail("second slot acquired")
    assert 0.15 <= time.monotonic() - started < 2
    assert notices and "waiting" in notices[0]
    with g.slot(tmp_path, {}, wait=0):
        pass


def test_light_slots_third_waits(tmp_path: Path) -> None:
    import threading

    light = tmp_path / "light"
    entered = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with g.slot(
            light,
            {"pid": os.getpid()},
            count=2,
            wait=5,
            message="waiting for lane slot: ",
            slot_env="SABEROPS_LANE_SLOTS",
        ):
            entered.set()
            assert release.wait(timeout=5)

    threads = [threading.Thread(target=holder) for _ in range(2)]
    for thread in threads:
        thread.start()
    assert entered.wait(timeout=5)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if (light / "0.json").exists() and (light / "1.json").exists():
            break
        time.sleep(0.05)
    assert (light / "0.json").exists() and (light / "1.json").exists()
    notices: list[str] = []
    with pytest.raises(TimeoutError):
        with g.slot(
            light,
            {},
            count=2,
            wait=0.3,
            message="waiting for lane slot: ",
            slot_env="SABEROPS_LANE_SLOTS",
            notify=notices.append,
        ):
            pytest.fail("third light slot acquired")
    assert notices and "waiting for lane slot:" in notices[0]
    release.set()
    for thread in threads:
        thread.join(timeout=5)
    with g.slot(
        light,
        {},
        count=2,
        wait=1,
        message="waiting for lane slot: ",
        slot_env="SABEROPS_LANE_SLOTS",
    ):
        pass
    with pytest.raises(ValueError):
        with g.slot(light, {}, count=0, slot_env="SABEROPS_LANE_SLOTS"):
            pass


def test_lane_waits_while_heavy_held(tmp_path: Path) -> None:
    heavy = tmp_path / "heavy"
    notices: list[str] = []
    with g.slot(heavy, {"pid": os.getpid()}, count=1, wait=5):
        with pytest.raises(TimeoutError):
            with g.slot(
                heavy, {}, count=1, wait=0.3, observe=True, notify=notices.append
            ):
                pass
    assert notices and "waiting for full-gate slot:" in notices[0]


def test_full_gate_drains_light_slots(tmp_path: Path) -> None:
    import threading

    heavy = tmp_path / "heavy"
    light = tmp_path / "light"
    release = threading.Event()
    light_held = threading.Event()

    def lane_holder() -> None:
        with g.slot(
            light,
            {"pid": os.getpid()},
            count=2,
            wait=5,
            message="waiting for lane slot: ",
            slot_env="SABEROPS_LANE_SLOTS",
        ):
            light_held.set()
            assert release.wait(timeout=5)

    thread = threading.Thread(target=lane_holder)
    thread.start()
    assert light_held.wait(timeout=5)
    time.sleep(0.2)
    notices: list[str] = []
    with g.slot(heavy, {"pid": os.getpid()}, count=1, wait=5):
        with pytest.raises(TimeoutError):
            with g.slot(
                light,
                {"pid": os.getpid()},
                count=2,
                wait=0.3,
                acquire_all=True,
                message="waiting for running lane checks: ",
                slot_env="SABEROPS_LANE_SLOTS",
                notify=notices.append,
            ):
                pytest.fail("drain acquired while lane holds light")
        assert notices and "waiting for running lane checks:" in notices[0]
        release.set()
        thread.join(timeout=5)
        with g.slot(
            light,
            {"pid": os.getpid()},
            count=2,
            wait=5,
            acquire_all=True,
            message="waiting for running lane checks: ",
            slot_env="SABEROPS_LANE_SLOTS",
        ):
            assert (light / "0.json").exists() and (light / "1.json").exists()
            blocked: list[str] = []
            with pytest.raises(TimeoutError):
                with g.slot(
                    light,
                    {},
                    count=2,
                    wait=0.3,
                    message="waiting for lane slot: ",
                    slot_env="SABEROPS_LANE_SLOTS",
                    notify=blocked.append,
                ):
                    pass
            assert blocked and "waiting for lane slot:" in blocked[0]
    assert not (light / "0.json").exists() and not (light / "1.json").exists()


def test_lane_queues_behind_draining_full_gate(tmp_path: Path) -> None:
    heavy = tmp_path / "heavy"
    with g.slot(heavy, {"pid": os.getpid()}, count=1, wait=5):
        notices: list[str] = []
        with pytest.raises(TimeoutError):
            with g.slot(
                heavy, {}, count=1, wait=0.3, observe=True, notify=notices.append
            ):
                pytest.fail("lane passed heavy while full gate holds it")
        assert notices and "waiting for full-gate slot:" in notices[0]


def test_lane_floor_default_is_10(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import subprocess

    monkeypatch.delenv("SABEROPS_GATE_MIN_FREE_GB", raising=False)
    subprocess.check_output(["git", "init", "-q"], cwd=tmp_path)
    subprocess.check_output(["git", "config", "user.email", "t@example.invalid"], cwd=tmp_path)
    subprocess.check_output(["git", "config", "user.name", "T"], cwd=tmp_path)
    subprocess.check_output(
        ["git", "commit", "-q", "--allow-empty", "-m", "init"], cwd=tmp_path
    )
    seen: dict[str, float] = {}
    real_disk_ok = g.disk_ok

    def capture(paths: list[Path], minimum_gb: float) -> bool:
        seen["minimum"] = minimum_gb
        return False

    monkeypatch.setattr(g, "disk_ok", capture)
    runner = g.Runner(tmp_path, "lane")
    try:
        code = runner.execute("HEAD", [])
    finally:
        runner.stop_monitor.set()
        runner.monitor.join(timeout=5)
    assert code == 3
    assert seen.get("minimum") == 10.0
    assert real_disk_ok is not None


def test_lane_targets(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    for name in ("test_new.py", "test_modified.py", "test_core.py"):
        (tmp_path / "tests" / name).touch()
    assert g.lane_targets(
        tmp_path,
        [
            "tests/test_new.py",
            "tests/test_modified.py",
            "src/saberops/sub/core.py",
            "tests/test_deleted.py",
            "docs/gate.md",
        ],
        ["tests/test_core.py::test_one"],
    ) == [
        "tests/test_core.py",
        "tests/test_core.py::test_one",
        "tests/test_modified.py",
        "tests/test_new.py",
    ]
    assert g.lane_targets(tmp_path, ["docs/gate.md"], []) == []


@pytest.mark.parametrize(
    ("code", "log", "expected"),
    [
        (0, "", ("PASS", "", 0)),
        (1, "No space left on device", ("INFRA", "ENOSPC", 3)),
        (1, "Errno 28", ("INFRA", "ENOSPC", 3)),
        (3, "", ("INFRA", "PYTEST_INTERNAL", 3)),
        (1, "INTERNALERROR", ("INFRA", "PYTEST_INTERNAL", 3)),
        (2, "ImportError", ("FAIL", "", 1)),
        (124, "", ("TIMEOUT", "", 1)),
        (-15, "", ("INFRA", "INTERRUPTED", 3)),
    ],
)
def test_outcomes(code: int, log: str, expected: tuple[str, str, int]) -> None:
    assert g.evaluate(g.Evidence(codes=[code], log=log, count=1))[:3] == expected


def test_junit_and_rerun(tmp_path: Path) -> None:
    xml = tmp_path / "first.xml"
    xml.write_text(
        '<testsuites><testsuite><testcase classname="tests.test_a.TestA" '
        'name="test_one"><failure>AssertionError: broken</failure></testcase>'
        "</testsuite></testsuites>"
    )
    first = g.junit(xml)
    node = "tests/test_a.py::TestA::test_one"
    assert list(first.failed) == [node]
    first.codes = [1]
    assert g.evaluate(first)[0] == "FAIL"
    rerun = g.Evidence(codes=[0], count=1, passed={node})
    assert g.evaluate(first, rerun, {node})[0] == "FLAKY_PASS"
    assert g.evaluate(first, rerun)[0] == "FAIL"
    first.failed[node] = "Failed: Timeout (>180.0s)"
    assert g.evaluate(first)[0] == "TIMEOUT"
    assert g.evaluate(g.Evidence(codes=[5]), selected=True)[2] == 5


def test_skipped_rerun_is_not_flaky_pass() -> None:
    first = g.Evidence(codes=[1], count=1, failed={"tests/test_a.py::test_a": "broken"})
    rerun = g.Evidence(codes=[0], count=1)
    assert g.evaluate(first, rerun, {"tests/test_a.py::test_a"})[0] == "FAIL"


def test_keep_only_newest_failure_per_worktree(tmp_path: Path) -> None:
    old = owned(tmp_path, "old", os.getpid(), True)
    new = owned(tmp_path, "new", os.getpid(), True)
    record = json.loads((old / "owner.json").read_text())
    record["worktree"] = "new"
    (old / "owner.json").write_text(json.dumps(record))
    os.utime(old, (1, 1))
    g.keep_failed(tmp_path, "new", g.GB)
    assert not old.exists() and new.exists()


def test_collection_junit_keeps_file(tmp_path: Path) -> None:
    xml = tmp_path / "collection.xml"
    xml.write_text(
        '<testsuites><testsuite><testcase classname="" '
        'name="tests/test_broken.py"><error>ImportError: broken</error>'
        "</testcase></testsuite></testsuites>"
    )
    evidence = g.junit(xml)
    evidence.codes = [2]
    assert list(evidence.failed) == ["tests/test_broken.py"]
    assert g.evaluate(evidence)[0] == "FAIL"


def _init_main_repo(path: Path) -> Path:
    """Covers: test helper (temporary git repos only)."""
    import subprocess

    subprocess.check_output(["git", "init", "-q", "-b", "main"], cwd=path)
    subprocess.check_output(["git", "config", "user.email", "t@example.invalid"], cwd=path)
    subprocess.check_output(["git", "config", "user.name", "T"], cwd=path)
    (path / "README.md").write_text("hi\n")
    subprocess.check_output(["git", "add", "-A"], cwd=path)
    subprocess.check_output(["git", "commit", "-qm", "init"], cwd=path)
    return path


def _make_runner(
    repo: Path, kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> object:
    """Covers: test helper (isolated gate tmp)."""
    scratch = tmp_path / f"gate-tmp-{kind}"
    scratch.mkdir(exist_ok=True)
    monkeypatch.setenv("SABEROPS_GATE_TMP", str(scratch))
    monkeypatch.setenv("SABEROPS_GATE_KEEP", "1")
    return g.Runner(repo, kind)


def test_history_full_appends_one_line_default_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: prompt section A (full finish appends one line, default label)."""
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_main_repo(repo)
    monkeypatch.delenv("SABEROPS_GATE_LABEL", raising=False)
    runner = _make_runner(repo, "full", tmp_path, monkeypatch)
    assert runner is not None
    code = runner.finish(("PASS", "", 0, [], []), 12)  # type: ignore[attr-defined]
    assert code == 0
    history = g.history_path(runner.common)  # type: ignore[attr-defined]
    lines = history.read_text().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["label"] == "manual"
    assert entry["outcome"] == "PASS"
    assert entry["tests"] == 12
    assert entry["failed"] == [] and entry["flaky"] == []
    assert entry["head_sha"] == subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True
    ).strip()
    assert entry["worktree"] == str(repo)
    for key in ("ts", "reason", "wall_time", "peak_bytes"):
        assert key in entry


def test_history_label_from_env_and_infra_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: prompt section A (label from env, INFRA recorded too)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_main_repo(repo)
    monkeypatch.setenv("SABEROPS_GATE_LABEL", "nightly")
    runner = _make_runner(repo, "full", tmp_path, monkeypatch)
    code = runner.finish(("INFRA", "DISK_LOW", 3, [], []), 0)  # type: ignore[attr-defined]
    assert code == 3
    history = g.history_path(runner.common)  # type: ignore[attr-defined]
    entry = json.loads(history.read_text().splitlines()[-1])
    assert entry["label"] == "nightly"
    assert entry["outcome"] == "INFRA"
    assert entry["reason"] == "DISK_LOW"


def test_history_lane_appends_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Covers: prompt section A (lane runs never write history)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_main_repo(repo)
    runner = _make_runner(repo, "lane", tmp_path, monkeypatch)
    code = runner.finish(("PASS", "", 0, [], []), 3)  # type: ignore[attr-defined]
    assert code == 0
    history = g.history_path(runner.common)  # type: ignore[attr-defined]
    assert not history.exists()


def test_history_unwritable_warns_and_keeps_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Covers: prompt section A (history failure warns, keeps outcome)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_main_repo(repo)
    runner = _make_runner(repo, "full", tmp_path, monkeypatch)
    history = g.history_path(runner.common)  # type: ignore[attr-defined]
    history.parent.mkdir(parents=True, exist_ok=True)
    # Make the history directory unusable: replace it with a plain file.
    import shutil

    shutil.rmtree(history.parent)
    history.parent.write_text("blocked")
    code = runner.finish(("PASS", "", 0, [], []), 5)  # type: ignore[attr-defined]
    assert code == 0
    out = capsys.readouterr().out
    assert "warning" in out.lower() and "gate history" in out.lower()
    outcome = json.loads((runner.result / "outcome.json").read_text())  # type: ignore[attr-defined]
    assert outcome["outcome"] == "PASS"


def _write_history(path: Path, entries: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        for entry in entries:
            handle.write(json.dumps(entry) + "\n")


def _entry(
    ts: str,
    outcome: str,
    flaky: list[str],
    failed: list[str] | None = None,
    label: str = "manual",
    sha: str = "abc",
) -> dict[str, object]:
    return {
        "ts": ts,
        "head_sha": sha,
        "worktree": "/tmp/w",
        "label": label,
        "outcome": outcome,
        "reason": "",
        "tests": 10,
        "failed": failed or [],
        "flaky": flaky,
        "wall_time": 1.0,
        "peak_bytes": 10,
    }


def test_flaky_streak_skips_infra(tmp_path: Path) -> None:
    """Covers: prompt section B (streak newest-first, INFRA skipped)."""
    history = tmp_path / "full.jsonl"
    _write_history(
        history,
        [
            _entry("2026-10-01T00:00:00+00:00", "PASS", ["b"]),
            _entry("2026-10-02T00:00:00+00:00", "INFRA", []),
            _entry("2026-10-03T00:00:00+00:00", "PASS", ["a"]),
            _entry("2026-10-04T00:00:00+00:00", "PASS", ["a"]),
            _entry("2026-10-05T00:00:00+00:00", "PASS", ["a"]),
        ],
    )
    entries = g.load_history(history)
    per_test, _ = g.flaky_stats(entries)
    assert per_test["a"]["streak"] == 3
    assert per_test["a"]["total"] == 3
    assert per_test["a"]["last_seen"] == "2026-10-05T00:00:00+00:00"
    # b missed the newest non-INFRA gates, so its streak is 0.
    assert per_test["b"]["streak"] == 0


def test_flaky_report_threshold_and_quarantine_mark(tmp_path: Path) -> None:
    """Covers: prompt section B (threshold 3, quarantine marked)."""
    history = tmp_path / "full.jsonl"
    _write_history(
        history,
        [
            _entry("2026-10-01T00:00:00+00:00", "PASS", ["q1"]),
            _entry("2026-10-02T00:00:00+00:00", "PASS", ["q1"]),
            _entry("2026-10-03T00:00:00+00:00", "PASS", ["q1"]),
        ],
    )
    entries = g.load_history(history)
    text = g.render_flaky_report(history, entries, 3, {"q1"}, False)
    assert "## quarantine proposals" in text
    assert "q1" in text and "[in quarantine]" in text
    text_default = g.render_flaky_report(history, entries, 4, set(), False)
    assert "(none)" in text_default
    payload = json.loads(g.render_flaky_report(history, entries, 2, set(), True))
    assert payload["proposals"][0]["id"] == "q1"
    assert payload["min_streak"] == 2


def test_flaky_report_missing_and_nightly_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: prompt section B (missing file empty, nightly failures listed)."""
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_main_repo(repo)
    monkeypatch.chdir(repo)
    # Missing history file -> empty report, exit 0.
    code = g.flaky_report_cli([])
    assert code == 0
    # Now write a nightly run with failures.
    common = (repo / subprocess.check_output(
        ["git", "rev-parse", "--git-common-dir"], cwd=repo, text=True
    ).strip()).resolve()
    history = g.history_path(common)
    _write_history(
        history,
        [
            _entry("2026-10-06T00:00:00+00:00", "FAIL", [], ["n-fail-1"], label="nightly"),
        ],
    )
    assert g.flaky_report_cli([]) == 0
    entries = g.load_history(history)
    text = g.render_flaky_report(history, entries, 3, set(), False)
    assert "## nightly failures" in text
    assert "n-fail-1" in text


def test_exclusive_passthrough_and_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: prompt section C (exit passthrough, slot timeout exit 3)."""
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_main_repo(repo)
    monkeypatch.chdir(repo)
    assert g.exclusive_cli(["--", "true"]) == 0
    assert g.exclusive_cli(["--", "bash", "-c", "exit 7"]) == 7
    # Hold the heavy slot, then exclusive must time out.
    common = (repo / subprocess.check_output(
        ["git", "rev-parse", "--git-common-dir"], cwd=repo, text=True
    ).strip()).resolve()
    heavy = common / "saberops-gate-slots"
    monkeypatch.setenv("SABEROPS_GATE_SLOT_WAIT", "0.2")
    with g.slot(heavy, {"pid": os.getpid()}, 1, 5):
        code = g.exclusive_cli(["--", "true"])
        assert code == 3


def test_exclusive_orders_after_heavy_and_blocks_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: prompt section C (waits for heavy release, blocks lane)."""
    import subprocess
    import threading

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_main_repo(repo)
    monkeypatch.chdir(repo)
    common = (repo / subprocess.check_output(
        ["git", "rev-parse", "--git-common-dir"], cwd=repo, text=True
    ).strip()).resolve()
    heavy = common / "saberops-gate-slots"
    light = common / "saberops-gate-light-slots"
    marker = tmp_path / "exclusive-ran"
    release = threading.Event()
    started = threading.Event()

    def hold_heavy() -> None:
        with g.slot(heavy, {"pid": os.getpid()}, 1, 5):
            started.set()
            assert release.wait(timeout=10)

    holder = threading.Thread(target=hold_heavy)
    holder.start()
    assert started.wait(timeout=10)
    result: dict[str, int] = {}

    def run_exclusive() -> None:
        result["code"] = g.exclusive_cli(
            ["--", "python3", "-c", f"open({str(marker)!r}, 'w').write('x')"]
        )

    worker = threading.Thread(target=run_exclusive)
    worker.start()
    # CMD runs only after the held heavy slot is released.
    time.sleep(0.5)
    assert not marker.exists()
    release.set()
    holder.join(timeout=10)
    worker.join(timeout=10)
    assert marker.exists()
    assert result.get("code") == 0
    # While exclusive holds every slot, a lane observer waits.
    got = tmp_path / "exclusive-held"
    entered = threading.Event()
    leave = threading.Event()

    def hold_exclusive() -> None:
        repo_local = Path.cwd()
        common_local = (repo_local / subprocess.check_output(
            ["git", "rev-parse", "--git-common-dir"], cwd=repo_local, text=True
        ).strip()).resolve()
        heavy_local = common_local / "saberops-gate-slots"
        light_local = common_local / "saberops-gate-light-slots"
        with g.slot(heavy_local, {"pid": os.getpid()}, 1, 5):
            with g.slot(
                light_local,
                {"pid": os.getpid()},
                2,
                5,
                acquire_all=True,
                slot_env="SABEROPS_LANE_SLOTS",
            ):
                entered.set()
                assert leave.wait(timeout=10)

    holder2 = threading.Thread(target=hold_exclusive)
    holder2.start()
    assert entered.wait(timeout=10)
    with pytest.raises(TimeoutError):
        with g.slot(light, {}, count=2, wait=0.3, slot_env="SABEROPS_LANE_SLOTS"):
            pass
    leave.set()
    holder2.join(timeout=10)
    assert got is not None


def _setup_nightly_main(path: Path) -> Path:
    """Covers: prompt section D (temp MAIN with gate_tools for nightly tests)."""
    import shutil
    import subprocess

    path.mkdir(parents=True, exist_ok=True)
    subprocess.check_output(["git", "init", "-q", "-b", "main"], cwd=path)
    subprocess.check_output(["git", "config", "user.email", "t@example.invalid"], cwd=path)
    subprocess.check_output(["git", "config", "user.name", "T"], cwd=path)
    (path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    scripts = path / "scripts"
    scripts.mkdir(exist_ok=True)
    real_tools = Path(__file__).resolve().parents[1] / "scripts/gate_tools.py"
    shutil.copy2(real_tools, scripts / "gate_tools.py")
    (path / "tests").mkdir(exist_ok=True)
    (path / "tests" / "quarantine.txt").write_text("")
    subprocess.check_output(["git", "add", "-A"], cwd=path)
    subprocess.check_output(["git", "commit", "-qm", "init"], cwd=path)
    return path


def _run_nightly(
    env: dict[str, str], script: Path, timeout_s: int = 120
) -> tuple[int, str]:
    """Covers: test helper (run nightly-gate.sh with stub env)."""
    import subprocess

    completed = subprocess.run(
        ["bash", str(script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    return completed.returncode, completed.stdout + completed.stderr


def test_nightly_creates_worktree_gate_pass_and_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: prompt section D (worktree, PASS map rebuild, summary/latest)."""
    import os
    import subprocess

    main = _setup_nightly_main(tmp_path / "main")
    wt = tmp_path / "nightly-wt"
    cache = tmp_path / "cache"
    orch = tmp_path / "orch"
    script = Path(__file__).resolve().parents[1] / "scripts/nightly-gate.sh"
    gate_stub = (
        'mkdir -p "$OUT_DIR/result" && '
        "printf '{\"outcome\":\"PASS\",\"reason\":\"\",\"counts\":{\"tests\":4,\"failed\":0,"
        "\"flaky\":0},\"wall_time\":1.5,\"head_sha\":\"deadbeef\",\"peak_bytes\":99}' "
        '> "$OUT_DIR/result/outcome.json"'
    )
    map_stub = 'echo "fake map build"'
    env = dict(os.environ)
    env.update(
        {
            "SABEROPS_NIGHTLY_MAIN": str(main),
            "NIGHTLY_WT": str(wt),
            "SABEROPS_ORCH_DIR": str(orch),
            "SABEROPS_NIGHTLY_CACHE": str(cache),
            "SABEROPS_NIGHTLY_SKIP_VENV": "1",
            "SABEROPS_NIGHTLY_GATE_CMD": gate_stub,
            "SABEROPS_NIGHTLY_MAP_CMD": map_stub,
            "SABEROPS_GATE_LABEL": "nightly",
        }
    )
    code, output = _run_nightly(env, script)
    assert code == 0, output
    assert wt.is_dir()
    # Detached worktree at main.
    head_ref = subprocess.check_output(
        ["git", "-C", str(wt), "rev-parse", "--abbrev-ref", "HEAD"], text=True
    ).strip()
    assert head_ref == "HEAD"
    out_dirs = [p for p in cache.iterdir() if p.is_dir()]
    assert len(out_dirs) == 1
    out_dir = out_dirs[0]
    assert (out_dir / "result" / "outcome.json").exists()
    # Map rebuild only on PASS: map.txt exists and used the exclusive slot.
    assert (out_dir / "map.txt").exists()
    assert "fake map build" in (out_dir / "map.txt").read_text()
    assert (out_dir / "flaky.md").exists()
    assert (out_dir / "summary.md").exists()
    latest = orch / "nightly" / "latest.md"
    assert latest.exists()
    assert "PASS" in latest.read_text()


def test_nightly_skip_rule_and_fail_skips_map(tmp_path: Path) -> None:
    """Covers: prompt section D (skip unless REPEAT, FAIL never rebuilds map)."""
    import json
    import os
    import subprocess

    main = _setup_nightly_main(tmp_path / "main")
    wt = tmp_path / "nightly-wt"
    cache = tmp_path / "cache"
    orch = tmp_path / "orch"
    script = Path(__file__).resolve().parents[1] / "scripts/nightly-gate.sh"
    main_sha = subprocess.check_output(
        ["git", "-C", str(main), "rev-parse", "main"], text=True
    ).strip()
    common = subprocess.check_output(
        ["git", "-C", str(main), "rev-parse", "--git-common-dir"], cwd=main, text=True
    ).strip()
    common_path = (main / common).resolve() if not common.startswith("/") else Path(common)
    history = common_path / "saberops-gate-history" / "full.jsonl"
    history.parent.mkdir(parents=True, exist_ok=True)
    with history.open("w") as handle:
        handle.write(
            json.dumps(_entry("2026-10-11T00:00:00+00:00", "PASS", [], [], "nightly", main_sha))
            + "\n"
        )
    base_env = dict(os.environ)
    gate_skip_stub = 'mkdir -p "$OUT_DIR/result" && echo GATE_RAN > "$OUT_DIR/gate-marker"'
    base_env.update(
        {
            "SABEROPS_NIGHTLY_MAIN": str(main),
            "NIGHTLY_WT": str(wt),
            "SABEROPS_ORCH_DIR": str(orch),
            "SABEROPS_NIGHTLY_CACHE": str(cache),
            "SABEROPS_NIGHTLY_SKIP_VENV": "1",
            "SABEROPS_NIGHTLY_GATE_CMD": gate_skip_stub,
        }
    )
    # Unchanged + PASS without REPEAT -> skip.
    code, output = _run_nightly(base_env, script)
    assert code == 0, output
    assert "main unchanged since last green nightly" in output
    # FAIL history never causes a skip: overwrite with FAIL and rerun.
    with history.open("w") as handle:
        fail_entry = _entry("2026-10-11T00:00:00+00:00", "FAIL", [], ["t::x"], "nightly", main_sha)
        handle.write(json.dumps(fail_entry) + "\n")
    fail_env = dict(base_env)
    fail_env["SABEROPS_NIGHTLY_GATE_CMD"] = (
        'mkdir -p "$OUT_DIR/result" && '
        "printf '{\"outcome\":\"FAIL\",\"reason\":\"\",\"counts\":{\"tests\":2,\"failed\":1,"
        "\"flaky\":0},\"wall_time\":0.5,\"head_sha\":\"x\",\"peak_bytes\":1}' "
        '> "$OUT_DIR/result/outcome.json"; exit 1'
    )
    # Clear cache so the FAIL run has a fresh dir.
    import shutil

    shutil.rmtree(cache, ignore_errors=True)
    code, output = _run_nightly(fail_env, script)
    assert code != 0, output
    out_dirs = [p for p in cache.iterdir() if p.is_dir()]
    assert len(out_dirs) == 1
    assert not (out_dirs[0] / "map.txt").exists()
    assert (orch / "nightly" / "latest.md").exists()


def test_nightly_retention_removes_only_old_nightly_dirs(tmp_path: Path) -> None:
    """Covers: prompt section D (retention removes only >30-day nightly dirs)."""
    import os
    import time

    main = _setup_nightly_main(tmp_path / "main")
    wt = tmp_path / "nightly-wt"
    cache = tmp_path / "cache"
    cache.mkdir()
    orch = tmp_path / "orch"
    old_nightly = cache / "2025-01-01-abc1234"
    old_nightly.mkdir()
    (old_nightly / "x.txt").write_text("old")
    old_epoch = time.time() - 40 * 86400
    os.utime(old_nightly, (old_epoch, old_epoch))
    keep_other = cache / "not-a-nightly-dir"
    keep_other.mkdir()
    os.utime(keep_other, (old_epoch, old_epoch))
    recent = cache / "2026-10-12-abc1234"
    recent.mkdir()
    script = Path(__file__).resolve().parents[1] / "scripts/nightly-gate.sh"
    env = dict(os.environ)
    env.update(
        {
            "SABEROPS_NIGHTLY_MAIN": str(main),
            "NIGHTLY_WT": str(wt),
            "SABEROPS_ORCH_DIR": str(orch),
            "SABEROPS_NIGHTLY_CACHE": str(cache),
            "SABEROPS_NIGHTLY_SKIP_VENV": "1",
            "SABEROPS_NIGHTLY_GATE_CMD": (
                'mkdir -p "$OUT_DIR/result" && '
                "printf '{\"outcome\":\"PASS\",\"reason\":\"\",\"counts\":{\"tests\":1,"
                "\"failed\":0,\"flaky\":0},\"wall_time\":0.1,\"head_sha\":\"y\",\"peak_bytes\":1}' "
                '> "$OUT_DIR/result/outcome.json"'
            ),
            "SABEROPS_NIGHTLY_MAP_CMD": "echo map",
        }
    )
    code, output = _run_nightly(env, script)
    assert code == 0, output
    assert not old_nightly.exists()
    assert keep_other.exists()
    assert recent.exists()

