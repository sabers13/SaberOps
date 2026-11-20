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


def test_memory_ledger_waits_and_acquires_when_released(tmp_path: Path) -> None:
    """Holding predicted budget blocks a second reservation until released."""
    avail = g.DESKTOP_RESERVE_BYTES + 2 * g.GB
    ledger = g.MemoryLedger(
        tmp_path / "ledger",
        cold_start_peak_bytes=2 * g.GB,
        available_memory_fn=lambda: avail,
        pressure_fn=lambda: g.MemoryPressure(0.0, 0.0),
    )
    notices: list[str] = []
    started = time.monotonic()
    with ledger.acquire("full", "full"):
        with pytest.raises(TimeoutError, match="MEMORY_BUDGET_TIMEOUT"):
            with ledger.acquire("full", "full", wait=0.2, notify=notices.append):
                pytest.fail("second reservation acquired without free budget")
    assert 0.15 <= time.monotonic() - started < 2
    assert notices and "waiting for memory budget" in notices[0]
    with ledger.acquire("full", "full", wait=0):
        pass


def test_small_steps_run_concurrently_while_heavy_waits(tmp_path: Path) -> None:
    """Small learned steps share remaining budget concurrently while heavy waits."""
    import threading

    avail = g.DESKTOP_RESERVE_BYTES + int(1.5 * g.GB)
    ledger = g.MemoryLedger(
        tmp_path / "ledger",
        cold_start_peak_bytes=3 * g.GB,
        available_memory_fn=lambda: avail,
        pressure_fn=lambda: g.MemoryPressure(0.0, 0.0),
    )
    ledger.record_peak("affected", "small", int(0.5 * g.GB))
    heavy_entered = threading.Event()
    heavy_thread_done = threading.Event()
    small_barrier = threading.Barrier(2, timeout=5)
    both_small_concurrent = threading.Event()

    def run_heavy() -> None:
        with ledger.acquire("full", "full", wait=5):
            heavy_entered.set()
        heavy_thread_done.set()

    def run_small() -> None:
        with ledger.acquire("affected", "small", wait=2):
            small_barrier.wait()
            both_small_concurrent.set()

    heavy_t = threading.Thread(target=run_heavy)
    heavy_t.start()
    time.sleep(0.15)
    assert not heavy_entered.is_set()
    s1 = threading.Thread(target=run_small)
    s2 = threading.Thread(target=run_small)
    s1.start()
    s2.start()
    s1.join(timeout=5)
    s2.join(timeout=5)
    assert both_small_concurrent.is_set()
    assert not heavy_entered.is_set()
    # Now free memory so heavy step completes.
    avail = g.DESKTOP_RESERVE_BYTES + 4 * g.GB
    assert heavy_thread_done.wait(timeout=5)
    heavy_t.join(timeout=5)
    assert heavy_entered.is_set()


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
    monkeypatch.setenv("SABEROPS_GATE_LABEL", "main-gate")
    runner = _make_runner(repo, "full", tmp_path, monkeypatch)
    code = runner.finish(("INFRA", "DISK_LOW", 3, [], []), 0)  # type: ignore[attr-defined]
    assert code == 3
    history = g.history_path(runner.common)  # type: ignore[attr-defined]
    entry = json.loads(history.read_text().splitlines()[-1])
    assert entry["label"] == "main-gate"
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


def test_flaky_report_missing_and_main_gate_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: prompt section B (missing file empty, main-gate failures listed)."""
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_main_repo(repo)
    monkeypatch.chdir(repo)
    # Missing history file -> empty report, exit 0.
    code = g.flaky_report_cli([])
    assert code == 0
    # Now write a main-gate run with failures.
    common = (repo / subprocess.check_output(
        ["git", "rev-parse", "--git-common-dir"], cwd=repo, text=True
    ).strip()).resolve()
    history = g.history_path(common)
    _write_history(
        history,
        [
            _entry("2026-10-06T00:00:00+00:00", "FAIL", [], ["n-fail-1"], label="main-gate"),
        ],
    )
    assert g.flaky_report_cli([]) == 0
    entries = g.load_history(history)
    text = g.render_flaky_report(history, entries, 3, set(), False)
    assert "## main-gate failures" in text
    assert "n-fail-1" in text


def test_exclusive_passthrough_and_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: exclusive CLI exit passthrough and memory budget timeout (exit 3)."""
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_main_repo(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("SABEROPS_GATE_TMP", str(tmp_path / "gate-tmp"))
    meminfo = tmp_path / "meminfo"
    avail_kb = (g.DESKTOP_RESERVE_BYTES + 2 * g.COLD_START_PEAK_BYTES) // 1024
    meminfo.write_text(f"MemTotal: {avail_kb * 2} kB\nMemAvailable: {avail_kb} kB\n")
    monkeypatch.setenv(g.PROC_MEMINFO_ENV, str(meminfo))
    assert g.exclusive_cli(["--", "true"]) == 0
    assert g.exclusive_cli(["--", "bash", "-c", "exit 7"]) == 7

    # Fresh repo whose coverage-map still has cold-start estimate: holding one heavy
    # lease leaves 100 MiB < COLD_START_PEAK_BYTES, so exclusive times out (exit 3).
    repo_timeout = tmp_path / "repo-timeout"
    repo_timeout.mkdir()
    _init_main_repo(repo_timeout)
    monkeypatch.chdir(repo_timeout)
    tight_kb = (g.DESKTOP_RESERVE_BYTES + g.COLD_START_PEAK_BYTES + 100 * g.MIB) // 1024
    meminfo.write_text(f"MemTotal: {avail_kb * 2} kB\nMemAvailable: {tight_kb} kB\n")
    common = (repo_timeout / subprocess.check_output(
        ["git", "rev-parse", "--git-common-dir"], cwd=repo_timeout, text=True
    ).strip()).resolve()
    ledger = g.MemoryLedger(g.default_ledger_root(common, repo=repo_timeout))
    monkeypatch.setenv("SABEROPS_GATE_SLOT_WAIT", "0.2")
    with ledger.acquire("full", "full"):
        code = g.exclusive_cli(["--", "true"])
        assert code == 3


def test_exclusive_orders_after_heavy_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: exclusive waits when budget is tight and runs once memory frees."""
    import subprocess
    import threading

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_main_repo(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("SABEROPS_GATE_TMP", str(tmp_path / "gate-tmp"))
    meminfo = tmp_path / "meminfo"
    tight_kb = (g.DESKTOP_RESERVE_BYTES + g.COLD_START_PEAK_BYTES + 100 * g.MIB) // 1024
    meminfo.write_text(f"MemTotal: {tight_kb * 4} kB\nMemAvailable: {tight_kb} kB\n")
    monkeypatch.setenv(g.PROC_MEMINFO_ENV, str(meminfo))

    common = (repo / subprocess.check_output(
        ["git", "rev-parse", "--git-common-dir"], cwd=repo, text=True
    ).strip()).resolve()
    ledger = g.MemoryLedger(g.default_ledger_root(common, repo=repo))
    marker = tmp_path / "exclusive-ran"
    release = threading.Event()
    started = threading.Event()

    def hold_heavy() -> None:
        with ledger.acquire("full", "full"):
            started.set()
            assert release.wait(timeout=10)

    holder = threading.Thread(target=hold_heavy)
    holder.start()
    assert started.wait(timeout=10)
    result: dict[str, int] = {}

    def run_exclusive() -> None:
        result["code"] = g.exclusive_cli(
            ["--", sys.executable, "-c", f"open({str(marker)!r}, 'w').write('x')"]
        )

    worker = threading.Thread(target=run_exclusive)
    worker.start()
    time.sleep(0.4)
    assert not marker.exists()
    release.set()
    holder.join(timeout=10)
    worker.join(timeout=10)
    assert marker.exists()
    assert result.get("code") == 0


def _setup_main_gate_repo(path: Path) -> Path:
    """Covers: prompt section D (temp MAIN with gate_tools for main-gate tests)."""
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


def _run_main_gate(
    env: dict[str, str], script: Path, timeout_s: int = 120
) -> tuple[int, str]:
    """Covers: test helper (run main-gate.sh with stub env)."""
    import subprocess

    completed = subprocess.run(
        ["bash", str(script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    return completed.returncode, completed.stdout + completed.stderr


def test_main_gate_creates_worktree_gate_pass_and_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: prompt section D (worktree, PASS map rebuild, summary/latest)."""
    import os
    import subprocess

    main = _setup_main_gate_repo(tmp_path / "main")
    wt = tmp_path / "_main-gate"
    cache = tmp_path / "cache"
    orch = tmp_path / "orch"
    script = Path(__file__).resolve().parents[1] / "scripts/main-gate.sh"
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
            "SABEROPS_MAIN_GATE_REPO": str(main),
            "SABEROPS_MAIN_GATE_WT": str(wt),
            "SABEROPS_ORCH_DIR": str(orch),
            "SABEROPS_MAIN_GATE_CACHE": str(cache),
            "SABEROPS_MAIN_GATE_SKIP_VENV": "1",
            "SABEROPS_MAIN_GATE_GATE_CMD": gate_stub,
            "SABEROPS_MAIN_GATE_MAP_CMD": map_stub,
            "SABEROPS_GATE_LABEL": "main-gate",
        }
    )
    code, output = _run_main_gate(env, script)
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
    latest = orch / "main-gate" / "latest.md"
    assert latest.exists()
    assert "PASS" in latest.read_text()


def test_main_gate_skip_rule_and_fail_skips_map(tmp_path: Path) -> None:
    """Covers: prompt section D (skip unless REPEAT, FAIL never rebuilds map)."""
    import json
    import os
    import subprocess

    main = _setup_main_gate_repo(tmp_path / "main")
    wt = tmp_path / "_main-gate"
    cache = tmp_path / "cache"
    orch = tmp_path / "orch"
    script = Path(__file__).resolve().parents[1] / "scripts/main-gate.sh"
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
            json.dumps(_entry("2026-10-11T00:00:00+00:00", "PASS", [], [], "main-gate", main_sha))
            + "\n"
        )
    base_env = dict(os.environ)
    gate_skip_stub = 'mkdir -p "$OUT_DIR/result" && echo GATE_RAN > "$OUT_DIR/gate-marker"'
    base_env.update(
        {
            "SABEROPS_MAIN_GATE_REPO": str(main),
            "SABEROPS_MAIN_GATE_WT": str(wt),
            "SABEROPS_ORCH_DIR": str(orch),
            "SABEROPS_MAIN_GATE_CACHE": str(cache),
            "SABEROPS_MAIN_GATE_SKIP_VENV": "1",
            "SABEROPS_MAIN_GATE_GATE_CMD": gate_skip_stub,
        }
    )
    # Unchanged + PASS without REPEAT -> skip.
    code, output = _run_main_gate(base_env, script)
    assert code == 0, output
    assert "main unchanged since last green main gate" in output
    # FAIL history never causes a skip: overwrite with FAIL and rerun.
    with history.open("w") as handle:
        fail_entry = _entry(
            "2026-10-11T00:00:00+00:00", "FAIL", [], ["t::x"], "main-gate", main_sha
        )
        handle.write(json.dumps(fail_entry) + "\n")
    fail_env = dict(base_env)
    fail_env["SABEROPS_MAIN_GATE_GATE_CMD"] = (
        'mkdir -p "$OUT_DIR/result" && '
        "printf '{\"outcome\":\"FAIL\",\"reason\":\"\",\"counts\":{\"tests\":2,\"failed\":1,"
        "\"flaky\":0},\"wall_time\":0.5,\"head_sha\":\"x\",\"peak_bytes\":1}' "
        '> "$OUT_DIR/result/outcome.json"; exit 1'
    )
    # Clear cache so the FAIL run has a fresh dir.
    import shutil

    shutil.rmtree(cache, ignore_errors=True)
    code, output = _run_main_gate(fail_env, script)
    assert code != 0, output
    out_dirs = [p for p in cache.iterdir() if p.is_dir()]
    assert len(out_dirs) == 1
    assert not (out_dirs[0] / "map.txt").exists()
    assert (orch / "main-gate" / "latest.md").exists()


def test_main_gate_retention_removes_only_old_main_gate_dirs(tmp_path: Path) -> None:
    """Covers: prompt section D (retention removes only >30-day main-gate dirs)."""
    import os
    import time

    main = _setup_main_gate_repo(tmp_path / "main")
    wt = tmp_path / "_main-gate"
    cache = tmp_path / "cache" / "main-gate"
    cache.mkdir(parents=True)
    orch = tmp_path / "orch"
    old_main_gate = cache / "2025-01-01-abc1234"
    old_main_gate.mkdir()
    (old_main_gate / "x.txt").write_text("old")
    old_epoch = time.time() - 40 * 86400
    os.utime(old_main_gate, (old_epoch, old_epoch))
    keep_other = cache / "not-a-main-gate-dir"
    keep_other.mkdir()
    os.utime(keep_other, (old_epoch, old_epoch))
    recent = cache / "2026-10-12-abc1234"
    recent.mkdir()
    script = Path(__file__).resolve().parents[1] / "scripts/main-gate.sh"
    env = dict(os.environ)
    env.update(
        {
            "SABEROPS_MAIN_GATE_REPO": str(main),
            "SABEROPS_MAIN_GATE_WT": str(wt),
            "SABEROPS_ORCH_DIR": str(orch),
            "SABEROPS_MAIN_GATE_CACHE": str(cache),
            "SABEROPS_MAIN_GATE_SKIP_VENV": "1",
            "SABEROPS_MAIN_GATE_GATE_CMD": (
                'mkdir -p "$OUT_DIR/result" && '
                "printf '{\"outcome\":\"PASS\",\"reason\":\"\",\"counts\":{\"tests\":1,"
                "\"failed\":0,\"flaky\":0},\"wall_time\":0.1,\"head_sha\":\"y\",\"peak_bytes\":1}' "
                '> "$OUT_DIR/result/outcome.json"'
            ),
            "SABEROPS_MAIN_GATE_MAP_CMD": "echo map",
        }
    )
    code, output = _run_main_gate(env, script)
    assert code == 0, output
    assert not old_main_gate.exists()
    assert keep_other.exists()
    assert recent.exists()


def _main_gate_env(
    main: Path,
    wt: Path,
    cache: Path,
    orch: Path,
    gate_stub: str,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Covers: test helper (stub env for main-gate.sh under the new contract)."""
    import os

    env = dict(os.environ)
    env.update(
        {
            "SABEROPS_MAIN_GATE_REPO": str(main),
            "SABEROPS_MAIN_GATE_WT": str(wt),
            "SABEROPS_ORCH_DIR": str(orch),
            "SABEROPS_MAIN_GATE_CACHE": str(cache),
            "SABEROPS_MAIN_GATE_SKIP_VENV": "1",
            "SABEROPS_MAIN_GATE_GATE_CMD": gate_stub,
        }
    )
    if extra:
        env.update(extra)
    return env


def _pass_gate_stub(outcome: str, exit_code: int = 0) -> str:
    """Covers: test helper (stub full gate writing a single outcome)."""
    return (
        'mkdir -p "$OUT_DIR/result" && '
        f"printf '{{\"outcome\":\"{outcome}\",\"reason\":\"\",\"counts\":{{\"tests\":2,"
        '"failed\":0,\"flaky\":0},\"wall_time\":0.5,\"head_sha\":\"x\",\"peak_bytes\":1} '
        f"'> \"$OUT_DIR/result/outcome.json\"; exit {exit_code}"
    )


def _snapshot_tree(path: Path) -> tuple[str, str, str]:
    """Covers: test helper (record HEAD, branch and status of a worktree)."""
    import subprocess

    head = subprocess.check_output(
        ["git", "-C", str(path), "rev-parse", "HEAD"], text=True
    ).strip()
    branch = subprocess.check_output(
        ["git", "-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"], text=True
    ).strip()
    status = subprocess.check_output(
        ["git", "-C", str(path), "status", "--porcelain"], text=True
    )
    return head, branch, status


def test_main_gate_guard_refuses_unsafe_worktrees(tmp_path: Path) -> None:
    """Covers: fixup-1 item 1 (guard refuses attached/MAIN/unregistered/basename)."""
    import subprocess

    script = Path(__file__).resolve().parents[1] / "scripts/main-gate.sh"
    main = _setup_main_gate_repo(tmp_path / "main")
    cache = tmp_path / "cache" / "main-gate"
    orch = tmp_path / "orch"
    gate_stub = _pass_gate_stub("PASS")

    def run_against(target: Path) -> tuple[int, str]:
        return _run_main_gate(_main_gate_env(main, target, cache, orch, gate_stub), script)

    # (a) an attached lane-style worktree with an uncommitted change.
    lane = tmp_path / "lane"
    subprocess.check_output(["git", "-C", str(main), "worktree", "add", str(lane)])
    (lane / "uncommitted.txt").write_text("precious")
    before = _snapshot_tree(lane)
    code, output = run_against(lane)
    assert code == 2, output
    assert "not detached" in output
    assert _snapshot_tree(lane) == before
    assert (lane / "uncommitted.txt").read_text() == "precious"

    # (b) the temp repo's main worktree itself.
    main_before = _snapshot_tree(main)
    code, output = run_against(main)
    assert code == 2, output
    assert "MAIN's checkout" in output
    assert _snapshot_tree(main) == main_before

    # (c) a plain directory that is not a registered worktree.
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "file.txt").write_text("data")
    code, output = run_against(plain)
    assert code == 2, output
    assert "not a registered worktree" in output
    assert (plain / "file.txt").read_text() == "data"

    # (d) a detached registered worktree with another basename, without the hatch.
    other = tmp_path / "other"
    subprocess.check_output(
        ["git", "-C", str(main), "worktree", "add", "--detach", str(other), "main"]
    )
    other_before = _snapshot_tree(other)
    code, output = run_against(other)
    assert code == 2, output
    assert "basename is not _main-gate" in output
    assert _snapshot_tree(other) == other_before


def test_main_gate_guard_accepts_detached_main_gate_and_hatch(tmp_path: Path) -> None:
    """Covers: fixup-1 item 1 (existing _main-gate and hatch paths proceed)."""
    import subprocess

    script = Path(__file__).resolve().parents[1] / "scripts/main-gate.sh"
    main = _setup_main_gate_repo(tmp_path / "main")
    orch = tmp_path / "orch"
    gate_stub = _pass_gate_stub("PASS")
    map_stub = 'echo "fake map build"'

    # (e) an existing detached `_main-gate` worktree is refreshed and gated.
    existing = tmp_path / "_main-gate"
    subprocess.check_output(
        ["git", "-C", str(main), "worktree", "add", "--detach", str(existing), "main"]
    )
    cache_e = tmp_path / "cache-e" / "main-gate"
    code, output = _run_main_gate(
        _main_gate_env(
            main, existing, cache_e, orch, gate_stub, {"SABEROPS_MAIN_GATE_MAP_CMD": map_stub}
        ),
        script,
    )
    assert code == 0, output
    head_ref = subprocess.check_output(
        ["git", "-C", str(existing), "rev-parse", "--abbrev-ref", "HEAD"], text=True
    ).strip()
    assert head_ref == "HEAD"

    # A detached worktree with another basename proceeds with the hatch.
    other = tmp_path / "other"
    subprocess.check_output(
        ["git", "-C", str(main), "worktree", "add", "--detach", str(other), "main"]
    )
    cache_o = tmp_path / "cache-o" / "main-gate"
    code, output = _run_main_gate(
        _main_gate_env(
            main,
            other,
            cache_o,
            orch,
            gate_stub,
            {"SABEROPS_MAIN_GATE_MAP_CMD": map_stub, "SABEROPS_MAIN_GATE_ALLOW_PATH": "1"},
        ),
        script,
    )
    assert code == 0, output
    out_dirs = [p for p in cache_o.iterdir() if p.is_dir()]
    assert len(out_dirs) == 1
    assert "fake map build" in (out_dirs[0] / "map.txt").read_text()
    assert (orch / "main-gate" / "latest.md").exists()


@pytest.mark.parametrize(
    ("outcome", "gate_exit", "expect_map"),
    [
        ("PASS", 0, True),
        ("FLAKY_PASS", 0, True),
        ("FAIL", 1, False),
        ("TIMEOUT", 1, False),
        ("INFRA", 3, False),
    ],
)
def test_main_gate_map_rebuild_on_satisfied_gate(
    tmp_path: Path, outcome: str, gate_exit: int, expect_map: bool
) -> None:
    """Covers: fixup-1 item 2 (map rebuild on PASS/FLAKY_PASS only)."""
    script = Path(__file__).resolve().parents[1] / "scripts/main-gate.sh"
    main = _setup_main_gate_repo(tmp_path / "main")
    wt = tmp_path / "_main-gate"
    cache = tmp_path / "cache" / "main-gate"
    orch = tmp_path / "orch"
    env = _main_gate_env(
        main,
        wt,
        cache,
        orch,
        _pass_gate_stub(outcome, gate_exit),
        {"SABEROPS_MAIN_GATE_MAP_CMD": "echo map"},
    )
    code, output = _run_main_gate(env, script)
    assert code == gate_exit, output
    out_dirs = [p for p in cache.iterdir() if p.is_dir()]
    assert len(out_dirs) == 1
    assert (out_dirs[0] / "map.txt").exists() == expect_map
    assert (orch / "main-gate" / "latest.md").exists()


def test_main_gate_retention_refuses_unsafe_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: fixup-1 item 4 (retention refuses $HOME-like, non-main-gate dirs)."""
    import os
    import time

    script = Path(__file__).resolve().parents[1] / "scripts/main-gate.sh"
    main = _setup_main_gate_repo(tmp_path / "main")
    wt = tmp_path / "_main-gate"
    orch = tmp_path / "orch"
    gate_stub = _pass_gate_stub("PASS")
    old_epoch = time.time() - 40 * 86400

    # $HOME itself (simulated with a temp HOME; never the real home).
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    home_old = fake_home / "2025-01-01-abc1234"
    home_old.mkdir()
    (home_old / "x.txt").write_text("old")
    os.utime(home_old, (old_epoch, old_epoch))
    code, output = _run_main_gate(_main_gate_env(main, wt, fake_home, orch, gate_stub), script)
    assert code == 0, output
    assert "refusing retention" in output
    assert home_old.exists()

    # A path with no `main-gate` component.
    other = tmp_path / "other-cache"
    other.mkdir()
    other_old = other / "2025-01-01-abc1234"
    other_old.mkdir()
    (other_old / "x.txt").write_text("old")
    os.utime(other_old, (old_epoch, old_epoch))
    code, output = _run_main_gate(_main_gate_env(main, wt, other, orch, gate_stub), script)
    assert code == 0, output
    assert "refusing retention" in output
    assert other_old.exists()


def test_flaky_total_ignores_infra(tmp_path: Path) -> None:
    """Covers: fixup-1 Q2 (totals and last-seen count non-INFRA gates only)."""
    history = tmp_path / "full.jsonl"
    _write_history(
        history,
        [
            _entry("2026-10-01T00:00:00+00:00", "INFRA", ["a", "only-infra"]),
            _entry("2026-10-02T00:00:00+00:00", "PASS", ["a"]),
            _entry("2026-10-03T00:00:00+00:00", "PASS", ["a"]),
        ],
    )
    per_test, _ = g.flaky_stats(g.load_history(history))
    assert per_test["a"]["total"] == 2
    assert per_test["a"]["streak"] == 2
    assert per_test["a"]["last_seen"] == "2026-10-03T00:00:00+00:00"
    assert "only-infra" not in per_test


def test_flaky_stats_legacy_nightly_label_is_newest_main_gate(tmp_path: Path) -> None:
    """Covers: G-006 K1 (a legacy `nightly` line counts as the newest main gate)."""
    history = tmp_path / "full.jsonl"
    _write_history(
        history,
        [
            _entry("2026-10-05T00:00:00+00:00", "FAIL", [], ["old-fail"], label="nightly"),
            _entry("2026-10-06T00:00:00+00:00", "FAIL", [], ["new-fail"], label="main-gate"),
        ],
    )
    entries = g.load_history(history)
    text = g.render_flaky_report(history, entries, 3, set(), False)
    assert "## main-gate failures" in text
    assert "new-fail" in text and "old-fail" not in text
    payload = json.loads(g.render_flaky_report(history, entries, 3, set(), True))
    assert payload["main_gate_failures"] == ["new-fail"]
    # A lone legacy `nightly` line is still used as the newest main gate.
    _write_history(
        history,
        [_entry("2026-10-07T00:00:00+00:00", "FAIL", [], ["only-fail"], label="nightly")],
    )
    entries = g.load_history(history)
    text = g.render_flaky_report(history, entries, 3, set(), False)
    assert "only-fail" in text


def test_name_map_has_no_nightly_in_renamed_files() -> None:
    """Covers: G-006 name map (only K1's legacy label keeps `nightly`)."""
    root = Path(__file__).resolve().parents[1]
    files = [
        root / "scripts/main-gate.sh",
        root / "scripts/systemd/saberops-main-gate.service",
        root / "scripts/systemd/saberops-main-gate.timer",
        root / "scripts/gate_tools.py",
    ]
    for path in files:
        assert path.exists(), path
        text = path.read_text()
        if path.name == "gate_tools.py":
            # K1 keeps exactly one legacy label occurrence; drop it before the check.
            text = text.replace('"nightly"', "", 1)
        assert "nightly" not in text.lower(), f"{path} still names the old gate"


# ---------------------------------------------------------------------------
# D-0061: single parallel pass, bounded workers, cgroup isolation
# ---------------------------------------------------------------------------


def load_map_script() -> ModuleType:
    """Load scripts/build_test_map.py as a module (parallel argv helpers)."""
    spec = importlib.util.spec_from_file_location(
        "build_test_map", Path(__file__).resolve().parents[1] / "scripts/build_test_map.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


m = load_map_script()


def _capture_runner(
    repo: Path, kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[object, list[tuple[str, list[str]]]]:
    """Runner with ruff/mypy/pytest captured as recorded calls."""
    runner = _make_runner(repo, kind, tmp_path, monkeypatch)
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(runner, "command", lambda args, name, timeout=None: (0, ""))

    def fake_pytest(name: str, args: list[str], budget: float | None = None) -> object:
        calls.append((name, list(args)))
        return g.Evidence(codes=[0], count=5)

    monkeypatch.setattr(runner, "pytest", fake_pytest)
    return runner, calls


def test_full_gate_is_one_parallel_pass_without_serial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: single xdist worksteal pass with workers derived from grant."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_main_repo(repo)
    monkeypatch.setattr(g, "cpu_core_count", lambda: 8)
    runner, calls = _capture_runner(repo, "full", tmp_path, monkeypatch)
    runner.ledger.record_peak("full", "full", int(2.0 * g.GB), workers=4)  # type: ignore[attr-defined]
    code = runner.run_checks("HEAD", [])  # type: ignore[attr-defined]
    assert code == 0
    assert [name for name, _ in calls] == ["parallel"]
    args = calls[0][1]
    assert args[:2] == ["-n", "4"], args
    assert "--dist" in args and args[args.index("--dist") + 1] == "worksteal"
    assert "-p" not in args and "no:xdist" not in args
    assert "-m" not in args


def test_workers_follow_grant_without_configured_caps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Workers scale from grant // per_worker_peak, clamped between 1 and CPU cores."""
    monkeypatch.setattr(g, "cpu_core_count", lambda: 8)
    ledger = g.MemoryLedger(tmp_path / "ledger")
    ledger.record_peak("full", "full", int(2.0 * g.GB), workers=4)  # per_worker = 512 MiB
    assert ledger.workers_for_grant(int(2.0 * g.GB), "full", "full") == 4
    assert ledger.workers_for_grant(int(1.0 * g.GB), "full", "full") == 2
    assert ledger.workers_for_grant(int(0.2 * g.GB), "full", "full") == 1
    assert ledger.workers_for_grant(int(16.0 * g.GB), "full", "full") == 8
    # Configured env worker knobs are ignored (none remain in gate code).
    monkeypatch.setenv("SABEROPS_GATE_WORKERS", "1")
    assert ledger.workers_for_grant(int(2.0 * g.GB), "full", "full") == 4


def test_full_gate_reruns_only_failing_ids_serially(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: deterministic serial rerun only for failures."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_main_repo(repo)
    runner = _make_runner(repo, "full", tmp_path, monkeypatch)
    monkeypatch.setattr(runner, "command", lambda args, name, timeout=None: (0, ""))
    calls: list[tuple[str, list[str]]] = []

    def fake_pytest(name: str, args: list[str], budget: float | None = None) -> object:
        calls.append((name, list(args)))
        if name == "parallel":
            return g.Evidence(
                codes=[1], count=5, failed={"tests/test_a.py::test_x": "broken"}
            )
        return g.Evidence(codes=[0], count=1, passed={"tests/test_a.py::test_x"})

    monkeypatch.setattr(runner, "pytest", fake_pytest)
    runner.run_checks("HEAD", [])  # type: ignore[attr-defined]
    assert [name for name, _ in calls] == ["parallel", "rerun"]
    assert calls[1][1] == ["-p", "no:xdist", "tests/test_a.py::test_x"]


def test_lane_checks_run_parallel_when_multiple_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: multi-target lane checks scale workers from grant."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_main_repo(repo)
    monkeypatch.setattr(g, "cpu_core_count", lambda: 8)
    runner, calls = _capture_runner(repo, "lane", tmp_path, monkeypatch)
    runner.ledger.record_peak("affected", "small", int(1.0 * g.GB), workers=2)  # type: ignore[attr-defined]
    monkeypatch.setattr(runner, "_select_targets", lambda base, also: ["a", "b"])
    runner.run_checks("HEAD", [])  # type: ignore[attr-defined]
    assert calls[0][1][:4] == ["-n", "2", "--dist", "worksteal"]
    assert calls[0][1][4:] == ["a", "b"]
    assert "no:xdist" not in calls[0][1]


def test_lane_single_target_stays_single_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: one lane target needs no xdist startup."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_main_repo(repo)
    runner, calls = _capture_runner(repo, "lane", tmp_path, monkeypatch)
    monkeypatch.setattr(runner, "_select_targets", lambda base, also: ["only"])
    runner.run_checks("HEAD", [])  # type: ignore[attr-defined]
    assert calls[0][1] == ["only"]


def test_positive_int_env() -> None:
    """Covers: D-0061 (worker knobs are always positive integers)."""
    import os as _os

    previous = _os.environ.get("SABEROPS_TEST_WORKERS")
    try:
        _os.environ.pop("SABEROPS_TEST_WORKERS", None)
        assert g.positive_int_env("SABEROPS_TEST_WORKERS", 4) == 4
        _os.environ["SABEROPS_TEST_WORKERS"] = "6"
        assert g.positive_int_env("SABEROPS_TEST_WORKERS", 4) == 6
        _os.environ["SABEROPS_TEST_WORKERS"] = "auto"
        assert g.positive_int_env("SABEROPS_TEST_WORKERS", 4) == 4
        _os.environ["SABEROPS_TEST_WORKERS"] = "0"
        assert g.positive_int_env("SABEROPS_TEST_WORKERS", 4) == 4
    finally:
        if previous is None:
            _os.environ.pop("SABEROPS_TEST_WORKERS", None)
        else:
            _os.environ["SABEROPS_TEST_WORKERS"] = previous


def test_cgroup_prefix_disabled_by_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Covers: cgroup isolation can be disabled explicitly."""
    monkeypatch.setattr(g, "_systemd_run_probed", False)
    monkeypatch.setattr(g, "_systemd_run_cache", None)
    monkeypatch.setenv("SABEROPS_GATE_CGROUP", "0")
    assert g.build_cgroup_scope_prefix(2 * g.GB, unit_name="u", meta_dir=tmp_path) == []


def test_cgroup_scope_sets_dynamic_memory_high(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Covers: each step scope sets MemoryHigh to its grant (no fixed 6G/8G ceiling)."""
    class _Probe:
        returncode = 0

    monkeypatch.setattr(g, "_systemd_run_probed", False)
    monkeypatch.setattr(g, "_systemd_run_cache", None)
    monkeypatch.delenv("SABEROPS_GATE_CGROUP", raising=False)
    monkeypatch.setattr(g.shutil, "which", lambda name: "/usr/bin/systemd-run")
    monkeypatch.setattr(g.subprocess, "run", lambda *a, **k: _Probe())
    grant = 1_500_000_000
    prefix = g.build_cgroup_scope_prefix(grant, unit_name="saberops-test-unit", meta_dir=tmp_path)
    assert prefix[:5] == [
        "/usr/bin/systemd-run",
        "--user",
        "--scope",
        "--quiet",
        "--collect",
    ]
    assert f"MemoryHigh={grant}" in prefix
    assert not any(part.startswith("MemoryMax=") for part in prefix)
    assert "--unit=saberops-test-unit" in prefix


def test_cgroup_prefix_probe_failure_falls_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Covers: no usable user manager -> plain spawn, never a failure."""
    class _Probe:
        returncode = 1

    monkeypatch.setattr(g, "_systemd_run_probed", False)
    monkeypatch.setattr(g, "_systemd_run_cache", None)
    monkeypatch.delenv("SABEROPS_GATE_CGROUP", raising=False)
    monkeypatch.setattr(g.shutil, "which", lambda name: "/usr/bin/systemd-run")
    monkeypatch.setattr(g.subprocess, "run", lambda *a, **k: _Probe())
    assert g.build_cgroup_scope_prefix(2 * g.GB, unit_name="u", meta_dir=tmp_path) == []


def test_external_slot_env_skips_acquisition_without_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: engine holds host-gates lease; child must not re-acquire (no deadlock)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_main_repo(repo)
    tight_avail = g.DESKTOP_RESERVE_BYTES + g.COLD_START_PEAK_BYTES + 10 * g.MIB
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(
        f"MemTotal: {tight_avail * 2 // 1024} kB\nMemAvailable: {tight_avail // 1024} kB\n"
    )
    monkeypatch.setenv(g.PROC_MEMINFO_ENV, str(meminfo))
    monkeypatch.setenv("SABEROPS_GATE_EXTERNAL_SLOT", "1")
    monkeypatch.setenv(g.GRANTED_MEMORY_ENV, str(g.COLD_START_PEAK_BYTES))
    runner = _make_runner(repo, "full", tmp_path, monkeypatch)
    monkeypatch.setattr(runner, "command", lambda args, name, timeout=None: (0, ""))
    monkeypatch.setattr(
        runner,
        "pytest",
        lambda name, args, budget=None: g.Evidence(codes=[0], count=2),
    )
    started = time.monotonic()
    with runner.ledger.acquire("full", "full"):  # type: ignore[attr-defined]
        code = runner.execute("HEAD", [])  # type: ignore[attr-defined]
    assert code == 0
    assert time.monotonic() - started < 5


def test_no_history_env_suppresses_engine_managed_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: engine owns the single history line (no duplicate child line)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_main_repo(repo)
    monkeypatch.setenv("SABEROPS_GATE_NO_HISTORY", "1")
    runner = _make_runner(repo, "full", tmp_path, monkeypatch)
    code = runner.finish(("PASS", "", 0, [], []), 3)  # type: ignore[attr-defined]
    assert code == 0
    assert not g.history_path(runner.common).exists()  # type: ignore[attr-defined]


def test_outcome_json_carries_kind_producer_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: outcome.json kind/producer/profile/checks_ok."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_main_repo(repo)
    monkeypatch.setenv("SABEROPS_GATE_KIND", "G3")
    monkeypatch.setenv("SABEROPS_GATE_PRODUCER", "engine")
    runner = _make_runner(repo, "full", tmp_path, monkeypatch)
    runner.checks_ok = True  # type: ignore[attr-defined]
    runner.finish(("PASS", "", 0, [], []), 4)  # type: ignore[attr-defined]
    payload = json.loads((runner.result / "outcome.json").read_text())  # type: ignore[attr-defined]
    assert payload["kind"] == "G3"
    assert payload["producer"] == "engine"
    assert payload["profile"] == "full"
    assert payload["checks_ok"] is True
    entry = json.loads(g.history_path(runner.common).read_text().splitlines()[-1])  # type: ignore[attr-defined]
    assert entry["kind"] == "G3" and entry["producer"] == "engine"


# ---------------------------------------------------------------------------
# D-0061: coverage test-map build runs parallel under the exclusive slot
# ---------------------------------------------------------------------------


def test_map_suite_command_is_parallel() -> None:
    """Covers: D-0061 (map build uses xdist worksteal, not a serial run)."""
    command = m.suite_command("/py", Path("/rc"), "plugin", 4)
    assert command[:3] == ["/py", "-m", "coverage"]
    assert "-n" in command and command[command.index("-n") + 1] == "4"
    assert "--dist" in command and command[command.index("--dist") + 1] == "worksteal"
    assert "-p" in command and "plugin" in command


def test_map_workers_follow_memory_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: map workers derive from active memory grant or learned ledger peaks."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_main_repo(repo)
    monkeypatch.setenv(g.GRANTED_WORKERS_ENV, "5")
    assert m.map_workers(repo) == 5
    monkeypatch.delenv(g.GRANTED_WORKERS_ENV, raising=False)
    monkeypatch.setenv(g.GRANTED_MEMORY_ENV, str(2 * g.GB))
    monkeypatch.setattr(g, "cpu_core_count", lambda: 8)
    ledger = g.MemoryLedger(g.default_ledger_root(repo=repo))
    ledger.record_peak("coverage-map", "full", int(2.0 * g.GB), workers=4)
    assert m.map_workers(repo) == 4


def test_map_exclusive_wraps_the_canonical_slot(tmp_path: Path) -> None:
    """Covers: D-0061 item 3 (map build holds the exclusive heavy-gate slot)."""
    command = m.exclusive_command("/py", ["/py", "-m", "coverage", "run"])
    assert Path(command[1]).name == "gate_tools.py"
    assert command[2:4] == ["exclusive", "--"]
    assert command[4:] == ["/py", "-m", "coverage", "run"]


def test_map_wave_barrier_policy(tmp_path: Path) -> None:
    """Covers: D-0076 item 3 (reuse-if-current no-op; wave-barrier rebuilds)."""
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_main_repo(repo)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    map_dir = tmp_path / "map"
    map_dir.mkdir()
    # No map yet -> nothing to reuse.
    assert m.map_commit(map_dir) == ""
    assert m.map_is_current(map_dir, repo, head) is False
    # A map built from HEAD is current for HEAD.
    (map_dir / m.MAP_META).write_text(json.dumps({"commit": head}))
    assert m.map_commit(map_dir) == head
    assert m.map_is_current(map_dir, repo, head) is True
    assert (
        m.wave_barrier_decision(
            map_dir, repo, head, wave_barrier=False, reuse_if_current=True
        )
        == "reuse"
    )
    # The consolidation barrier always rebuilds, even when a reuse flag is set.
    assert (
        m.wave_barrier_decision(
            map_dir, repo, head, wave_barrier=True, reuse_if_current=True
        )
        == "rebuild"
    )
    # Without either flag the historical always-rebuild behaviour is preserved.
    assert (
        m.wave_barrier_decision(
            map_dir, repo, head, wave_barrier=False, reuse_if_current=False
        )
        == "rebuild"
    )
    # A map whose build commit is not an ancestor of HEAD is stale -> rebuild.
    (map_dir / m.MAP_META).write_text(json.dumps({"commit": "0" * 40}))
    assert m.map_is_current(map_dir, repo, head) is False
    assert (
        m.wave_barrier_decision(
            map_dir, repo, head, wave_barrier=False, reuse_if_current=True
        )
        == "rebuild"
    )





def test_lane_selection_uses_the_one_shared_selector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: T4 -- lane checks and gate-affected share select_tests.py."""
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_main_repo(repo)
    runner = _make_runner(repo, "lane", tmp_path, monkeypatch)
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        _ = kwargs
        calls.append(list(argv))
        return subprocess.CompletedProcess(
            argv, 0, "tests/test_selected.py\n", "strategy: coverage\n"
        )

    monkeypatch.setattr(g.subprocess, "run", fake_run)
    assert runner._select_targets("HEAD", ["tests/test_extra.py"]) == [  # type: ignore[attr-defined]
        "tests/test_extra.py",
        "tests/test_selected.py",
    ]
    assert "scripts/select_tests.py" in calls[0]
    assert calls[0][calls[0].index("--strategy") + 1] == "auto"
    assert "--exclude-slow" in calls[0]


def test_lane_selection_full_and_failure_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: T4 -- FULL and selector errors run the full suite, never nothing."""
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_main_repo(repo)
    runner = _make_runner(repo, "lane", tmp_path, monkeypatch)
    monkeypatch.setattr(
        g.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "FULL\n", ""),
    )
    assert runner._select_targets("HEAD", []) is None  # type: ignore[attr-defined]
    monkeypatch.setattr(
        g.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 2, "", "boom\n"),
    )
    assert runner._select_targets("HEAD", []) is None  # type: ignore[attr-defined]


def test_lane_full_selection_runs_the_whole_suite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: T4 -- a FULL selector answer runs pytest without target paths."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_main_repo(repo)
    monkeypatch.setattr(g, "cpu_core_count", lambda: 8)
    runner, calls = _capture_runner(repo, "lane", tmp_path, monkeypatch)
    monkeypatch.setattr(runner, "_select_targets", lambda base, also: None)
    runner.lane_full = True  # type: ignore[attr-defined]
    runner.run_checks("HEAD", [])  # type: ignore[attr-defined]
    assert calls[0][0] == "lane"
    assert "-n" in calls[0][1]
    assert not any(item.startswith("tests/") for item in calls[0][1])


def test_selector_all_ghosts_fails_the_lane_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: a non-empty selection resolving to nothing is FAIL 5."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_main_repo(repo)
    runner, _calls = _capture_runner(repo, "lane", tmp_path, monkeypatch)
    monkeypatch.setattr(runner, "_select_targets", lambda base, also: [])
    runner.selector_all_ghosts = True  # type: ignore[attr-defined]
    code = runner.run_checks("HEAD", [])  # type: ignore[attr-defined]
    assert code == 5


def test_main_gate_map_rebuild_can_run_detached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: T5 -- the map rebuild does not hold the gate after landing."""
    import os
    import time

    main = _setup_main_gate_repo(tmp_path / "main")
    wt = tmp_path / "_main-gate"
    cache = tmp_path / "cache"
    orch = tmp_path / "orch"
    script = Path(__file__).resolve().parents[1] / "scripts/main-gate.sh"
    gate_stub = (
        'mkdir -p "$OUT_DIR/result" && '
        "printf '{\"outcome\":\"PASS\",\"reason\":\"\",\"counts\":{\"tests\":1,\"failed\":0,"
        "\"flaky\":0},\"wall_time\":0.1,\"head_sha\":\"x\",\"peak_bytes\":1}' "
        '> "$OUT_DIR/result/outcome.json"'
    )
    env = dict(os.environ)
    env.update(
        {
            "SABEROPS_MAIN_GATE_REPO": str(main),
            "SABEROPS_MAIN_GATE_WT": str(wt),
            "SABEROPS_ORCH_DIR": str(orch),
            "SABEROPS_MAIN_GATE_CACHE": str(cache),
            "SABEROPS_MAIN_GATE_SKIP_VENV": "1",
            "SABEROPS_MAIN_GATE_GATE_CMD": gate_stub,
            "SABEROPS_MAIN_GATE_MAP_CMD": "echo detached-map-build",
            "SABEROPS_MAIN_GATE_MAP_MODE": "background",
        }
    )
    code, output = _run_main_gate(env, script)
    assert code == 0, output
    out_dir = next(p for p in cache.iterdir() if p.is_dir())
    map_file = out_dir / "map.txt"
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if map_file.exists() and "detached-map-build" in map_file.read_text():
            break
        time.sleep(0.02)
    assert "detached-map-build" in map_file.read_text()
    assert "map rebuilt: background" in (out_dir / "summary.md").read_text()


def test_all_slow_selection_skips_pytest_without_failing_the_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: B2 -- an all-slow selection is a clean skip, not FAIL 5."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_main_repo(repo)
    runner, calls = _capture_runner(repo, "lane", tmp_path, monkeypatch)
    monkeypatch.setattr(runner, "_select_targets", lambda base, also: [])
    code = runner.run_checks("HEAD", [])  # type: ignore[attr-defined]
    assert code == 0
    assert calls == []
