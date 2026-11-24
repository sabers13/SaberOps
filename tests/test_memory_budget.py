"""Hermetic tests for A8 host-wide dynamic memory budget across every heavy step."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from saberops._gate_tools import gate_tools as gt
from saberops.config import RunConfig
from saberops.gate_runner import (
    _DEFAULT_SAFETY_FLOOR_BYTES,
    GateRunner,
    GateRunnerRequest,
    _tmpfs_mount_for,
)
from saberops.models import GateScratchMode
from saberops.orchestrator import _run_configured_gate
from saberops.verification_snapshot import fixed_snapshot, source_tree

# H29: this module asserts the host memory guard itself, so conftest's
# default (off for ordinary tests) does not apply here.
pytestmark = pytest.mark.memory_guard


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.check_output(["git", "init", "-q", "-b", "main"], cwd=path)
    subprocess.check_output(["git", "config", "user.email", "t@example.invalid"], cwd=path)
    subprocess.check_output(["git", "config", "user.name", "T"], cwd=path)
    (path / "README.md").write_text("hi\n", encoding="utf-8")
    subprocess.check_output(["git", "add", "-A"], cwd=path)
    subprocess.check_output(["git", "commit", "-qm", "init"], cwd=path)
    return path


def test_heavy_step_waits_when_predicted_peak_exceeds_budget_and_starts_when_freed(
    tmp_path: Path,
) -> None:
    """Heavy step queues while predicted peak > budget and runs once memory frees."""
    reserve = 3 * gt.GB
    cold_start = 3 * gt.GB
    avail = reserve + 1 * gt.GB
    ledger = gt.MemoryLedger(
        tmp_path / "ledger",
        reserve_bytes=reserve,
        cold_start_peak_bytes=cold_start,
        available_memory_fn=lambda: avail,
        pressure_fn=lambda: gt.MemoryPressure(0.0, 0.0),
    )
    entered = threading.Event()
    done = threading.Event()
    seen_grant: dict[str, int] = {}

    def run_heavy() -> None:
        def invoke(
            cmd: list[str], env: dict[str, str], grant: int, _workers: int
        ) -> tuple[int, bool, bool, int]:
            seen_grant["grant"] = grant
            entered.set()
            return 0, False, False, 0

        _, stats = gt.run_with_lease(
            ledger=ledger,
            step="full",
            size_bucket="full",
            invoke=invoke,
            cmd=["true"],
            wait=5.0,
            scope_prefix_fn=lambda _g, _u, _m: [],
        )
        seen_grant["wait_calls_ok"] = 1 if stats.wait_seconds >= 0.1 else 0
        done.set()

    t = threading.Thread(target=run_heavy)
    t.start()
    time.sleep(0.25)
    assert not entered.is_set()
    # Free desktop/other RAM so budget covers predicted peak.
    avail = reserve + 4 * gt.GB
    assert done.wait(timeout=5.0)
    t.join(timeout=5.0)
    assert entered.is_set()
    assert seen_grant["grant"] == cold_start
    assert seen_grant["wait_calls_ok"] == 1


def test_small_steps_run_side_by_side_while_heavy_waits(tmp_path: Path) -> None:
    """Multiple small steps run concurrently under budget while heavy waits."""
    reserve = 3 * gt.GB
    avail = reserve + 1 * gt.GB
    ledger = gt.MemoryLedger(
        tmp_path / "ledger",
        reserve_bytes=reserve,
        cold_start_peak_bytes=3 * gt.GB,
        available_memory_fn=lambda: avail,
        pressure_fn=lambda: gt.MemoryPressure(0.0, 0.0),
    )
    ledger.record_peak("affected", "small", 400 * gt.MIB, workers=1)
    ledger.record_peak("full", "full", 3 * gt.GB, workers=4)

    heavy_started = threading.Event()
    heavy_done = threading.Event()

    def heavy_worker() -> None:
        with ledger.acquire("full", "full", wait=5.0):
            heavy_started.set()
        heavy_done.set()

    ht = threading.Thread(target=heavy_worker)
    ht.start()
    time.sleep(0.2)
    assert not heavy_started.is_set()

    # 400 MiB + 400 MiB = 800 MiB <= 1 GiB -> both small steps run simultaneously.
    both_inside = threading.Barrier(2, timeout=5.0)
    completed: list[int] = []

    def small_worker(idx: int) -> None:
        with ledger.acquire("affected", "small", wait=2.0) as lease:
            assert lease.grant_bytes == 400 * gt.MIB
            both_inside.wait()
            completed.append(idx)

    s1 = threading.Thread(target=small_worker, args=(1,))
    s2 = threading.Thread(target=small_worker, args=(2,))
    s1.start()
    s2.start()
    s1.join(timeout=5.0)
    s2.join(timeout=5.0)
    assert sorted(completed) == [1, 2]
    assert not heavy_started.is_set()

    avail = reserve + 4 * gt.GB
    assert heavy_done.wait(timeout=5.0)
    ht.join(timeout=5.0)


def test_pytest_workers_follow_grant_bounds(tmp_path: Path) -> None:
    """pytest workers scale with grant // learned per-worker peak, clamped 1..cores."""
    ledger = gt.MemoryLedger(
        tmp_path / "ledger",
        cold_start_per_worker_bytes=600 * gt.MIB,
        cpu_cores_fn=lambda: 6,
    )
    # Record a 4-worker run that peaked at 2000 MiB => per-worker = 500 MiB.
    ledger.record_peak("full", "full", 2000 * gt.MIB, workers=4)
    assert ledger.predict_per_worker_peak("full", "full") == 500 * gt.MIB
    assert ledger.workers_for_grant(2000 * gt.MIB, "full", "full") == 4
    assert ledger.workers_for_grant(1000 * gt.MIB, "full", "full") == 2
    assert ledger.workers_for_grant(250 * gt.MIB, "full", "full") == 1
    assert ledger.workers_for_grant(10_000 * gt.MIB, "full", "full") == 6


def test_rising_pressure_freezes_newest_gate_and_resumes_on_clear(tmp_path: Path) -> None:
    """Pressure stall blocks new grants; rising freezes newest gate; clearing resumes it."""
    reserve = 2 * gt.GB
    avail = reserve + 8 * gt.GB
    pressure = gt.MemoryPressure(0.0, 0.0)

    cg1 = tmp_path / "cg-older"
    cg1.mkdir()
    (cg1 / "cgroup.freeze").write_text("0\n", encoding="utf-8")
    (cg1 / "memory.current").write_text(str(500 * gt.MIB), encoding="utf-8")
    (cg1 / "memory.peak").write_text(str(600 * gt.MIB), encoding="utf-8")
    cg2_box: dict[str, Path] = {}

    ledger = gt.MemoryLedger(
        tmp_path / "ledger",
        reserve_bytes=reserve,
        cold_start_peak_bytes=1 * gt.GB,
        available_memory_fn=lambda: avail,
        pressure_fn=lambda: pressure,
        freeze_cooldown=0.0,
    )

    gate1_entered = threading.Event()
    gate1_release = threading.Event()
    gate2_entered = threading.Event()
    gate2_saw_frozen = threading.Event()
    gate2_done = threading.Event()
    gate2_stats_box: dict[str, gt.MemoryRunStats] = {}

    def run_gate1() -> None:
        with ledger.acquire("full", "full") as lease:
            lease.set_cgroup_path(cg1, unit_name="cg-older")
            lease.set_freeze_eligible()
            gate1_entered.set()
            assert gate1_release.wait(timeout=10.0)

    def run_gate2() -> None:
        def scope_prefix(_grant: int, unit: str, meta: Path) -> list[str]:
            cg2 = tmp_path / f"cg2-{unit}"
            cg2.mkdir(exist_ok=True)
            (cg2 / "cgroup.freeze").write_text("0\n", encoding="utf-8")
            (cg2 / "memory.current").write_text(str(500 * gt.MIB), encoding="utf-8")
            (cg2 / "memory.peak").write_text(str(750 * gt.MIB), encoding="utf-8")
            cg2_box["path"] = cg2
            (meta / "cgroup_path").write_text(str(cg2), encoding="utf-8")
            (meta / "memory.peak").write_text(str(750 * gt.MIB), encoding="utf-8")
            return []

        def invoke(
            cmd: list[str], env: dict[str, str], _grant: int, _workers: int
        ) -> tuple[int, bool, bool, int]:
            cg2 = cg2_box["path"]
            gate2_entered.set()
            # Simulate kernel freezer pausing execution until cgroup.freeze returns to 0.
            saw_frozen = False
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                state = (cg2 / "cgroup.freeze").read_text(encoding="utf-8").strip()
                if state == "1":
                    saw_frozen = True
                    gate2_saw_frozen.set()
                elif saw_frozen and state == "0":
                    return 0, False, False, 0
                time.sleep(0.01)
            return 1, False, False, 1

        res, stats = gt.run_with_lease(
            ledger=ledger,
            step="affected",
            size_bucket="small",
            invoke=invoke,
            cmd=["true"],
            scope_prefix_fn=scope_prefix,
            freeze_eligible=True,
        )
        gate2_stats_box["res"] = stats
        assert res == 0
        gate2_done.set()

    t1 = threading.Thread(target=run_gate1)
    t1.start()
    assert gate1_entered.wait(timeout=5.0)
    # Ensure gate 2 clearly has a newer started_at timestamp than gate 1.
    time.sleep(0.05)

    t2 = threading.Thread(target=run_gate2)
    t2.start()
    assert gate2_entered.wait(timeout=5.0)
    cg2 = cg2_box["path"]
    # Wait for gate2 monitor loop to publish cg2 into holder metadata.
    for _ in range(50):
        if any(h.get("cgroup_path") == str(cg2) for h in ledger.active_holders()):
            break
        time.sleep(0.05)

    # 1) Pressure appears -> hold new grants without freezing yet.
    pressure = gt.MemoryPressure(some_avg10=7.0, full_avg10=0.0)
    ledger.step_feedback()
    with pytest.raises(TimeoutError):
        with ledger.acquire("affected", "small", wait=0.15, notify=lambda _m: None):
            pytest.fail("new grant allowed while pressure active")
    assert (cg1 / "cgroup.freeze").read_text().strip() == "0"
    assert (cg2 / "cgroup.freeze").read_text().strip() == "0"

    # 2) Pressure keeps rising -> freezes most recently started gate (gate 2), never gate 1.
    pressure = gt.MemoryPressure(some_avg10=22.0, full_avg10=2.5)
    ledger.step_feedback()
    assert (cg2 / "cgroup.freeze").read_text().strip() == "1"
    assert (cg1 / "cgroup.freeze").read_text().strip() == "0"
    assert gate2_saw_frozen.wait(timeout=2.0)
    assert not gate2_done.is_set()

    # 3) Pressure clears -> thaws gate 2, which completes cleanly, and unblocks new grants.
    pressure = gt.MemoryPressure(some_avg10=0.0, full_avg10=0.0)
    ledger.step_feedback()
    assert (cg2 / "cgroup.freeze").read_text().strip() == "0"
    assert gate2_done.wait(timeout=5.0)
    t2.join(timeout=5.0)
    assert gate2_stats_box["res"].freeze_count >= 1

    with ledger.acquire("affected", "small", wait=0.5):
        pass

    gate1_release.set()
    t1.join(timeout=5.0)


def test_stopped_test_run_is_reran_not_failed(tmp_path: Path) -> None:
    """A test run stopped externally (SIGKILL/137/oom_kill) is re-run, never failed."""
    ledger = gt.MemoryLedger(
        tmp_path / "ledger",
        reserve_bytes=0,
        cold_start_peak_bytes=1 * gt.GB,
        available_memory_fn=lambda: 8 * gt.GB,
        pressure_fn=lambda: gt.MemoryPressure(0.0, 0.0),
    )
    attempts: list[int] = []

    def invoke(
        _cmd: list[str], _env: dict[str, str], _grant: int, _workers: int
    ) -> tuple[int, bool, bool, str]:
        attempts.append(len(attempts) + 1)
        if len(attempts) == 1:
            return 137, False, False, "stopped"
        return 0, False, False, "passed"

    payload, stats = gt.run_with_lease(
        ledger=ledger,
        step="affected",
        size_bucket="small",
        invoke=invoke,
        cmd=["pytest"],
        rerun_if_stopped=True,
        scope_prefix_fn=lambda _g, _u, _m: [],
    )
    assert payload == "passed"
    assert attempts == [1, 2]
    assert stats.rerun_count == 1


def test_killed_holder_releases_reservation(tmp_path: Path) -> None:
    """Killing a process (SIGKILL) immediately releases its flock-backed reservation."""
    ledger_dir = tmp_path / "ledger"
    meminfo = tmp_path / "meminfo"
    avail_bytes = gt.DESKTOP_RESERVE_BYTES + 3 * gt.GB
    meminfo.write_text(
        f"MemTotal: {avail_bytes * 2 // 1024} kB\nMemAvailable: {avail_bytes // 1024} kB\n",
        encoding="utf-8",
    )
    ready_file = tmp_path / "child-ready"
    gt_path = Path(__file__).resolve().parents[1] / "scripts" / "gate_tools.py"
    child_code = f"""
import importlib.util, pathlib, sys, time
spec = importlib.util.spec_from_file_location("gate_tools", {str(gt_path)!r})
gt = importlib.util.module_from_spec(spec)
sys.modules["gate_tools"] = gt
spec.loader.exec_module(gt)
ledger = gt.MemoryLedger(pathlib.Path({str(ledger_dir)!r}), cold_start_peak_bytes=2 * gt.GB)
with ledger.acquire("full", "full"):
    pathlib.Path({str(ready_file)!r}).write_text("ready")
    time.sleep(60)
"""
    env = dict(os.environ)
    env[gt.PROC_MEMINFO_ENV] = str(meminfo)
    env[gt.PROC_PRESSURE_MEMORY_ENV] = str(tmp_path / "no-pressure")
    proc = subprocess.Popen([sys.executable, "-c", child_code], env=env)
    try:
        for _ in range(100):
            if ready_file.exists():
                break
            time.sleep(0.05)
        assert ready_file.exists()

        parent_ledger = gt.MemoryLedger(
            ledger_dir,
            cold_start_peak_bytes=2 * gt.GB,
            available_memory_fn=lambda: avail_bytes,
            pressure_fn=lambda: gt.MemoryPressure(0.0, 0.0),
        )
        # 3 GiB budget minus 2 GiB child grant = 1 GiB remaining < 2 GiB required.
        with pytest.raises(TimeoutError):
            with parent_ledger.acquire("full", "full", wait=0.15, notify=lambda _m: None):
                pytest.fail("acquired while live holder held 2 GiB")
    finally:
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5.0)

    # Flock released by OS on SIGKILL -> dead metadata reaped & reservation succeeds immediately.
    with parent_ledger.acquire("full", "full", wait=0.5):
        assert len(parent_ledger.active_holders()) == 1


def test_learned_peaks_replace_cold_start_after_recorded_runs(tmp_path: Path) -> None:
    """Fresh ledger assumes largest cold start, then switches to measured memory.peak."""
    ledger_dir = tmp_path / "ledger"
    avail = 400 * gt.MIB
    ledger = gt.MemoryLedger(
        ledger_dir,
        reserve_bytes=0,
        cold_start_peak_bytes=3 * gt.GB,
        available_memory_fn=lambda: avail,
        pressure_fn=lambda: gt.MemoryPressure(0.0, 0.0),
    )
    assert not ledger.peaks_path.exists()
    assert ledger.predict_peak("affected", "small") == 3 * gt.GB

    # On cold start with 400 MiB budget, small step waits because cold-start assumes 3 GiB.
    with pytest.raises(TimeoutError):
        with ledger.acquire("affected", "small", wait=0.05, notify=lambda _m: None):
            pass

    # Run once with sufficient memory and capture measured cgroup peak = 250 MiB.
    avail = 4 * gt.GB

    def scope_with_peak(_grant: int, _unit: str, meta: Path) -> list[str]:
        (meta / "memory.peak").write_text(str(250 * gt.MIB), encoding="utf-8")
        return []

    _, stats = gt.run_with_lease(
        ledger=ledger,
        step="affected",
        size_bucket="small",
        invoke=lambda _c, _e, _g, _w: (0, False, False, 0),
        cmd=["true"],
        scope_prefix_fn=scope_with_peak,
    )
    assert stats.measured_peak_bytes == 250 * gt.MIB
    assert ledger.predict_peak("affected", "small") == 250 * gt.MIB

    # Even with only 400 MiB available, learned small step now fits while unseen full waits.
    avail = 400 * gt.MIB
    with ledger.acquire("affected", "small", wait=0.1) as lease:
        assert lease.grant_bytes == 250 * gt.MIB
    with pytest.raises(TimeoutError):
        with ledger.acquire("full", "full", wait=0.05, notify=lambda _m: None):
            pass

    # Spike raises estimate immediately; lower peak decays smoothly.
    ledger.record_peak("affected", "small", 400 * gt.MIB)
    assert ledger.predict_peak("affected", "small") == 400 * gt.MIB
    ledger.record_peak("affected", "small", 200 * gt.MIB)
    assert ledger.predict_peak("affected", "small") == 320 * gt.MIB


def test_scratch_and_snapshots_land_on_disk_when_tmp_is_tmpfs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """mountinfo parser + disk_scratch_base + snapshot dirs avoid tmpfs /tmp."""
    fake_mountinfo = tmp_path / "mountinfo"
    disk_dir = tmp_path / "disk_var_tmp"
    disk_dir.mkdir()
    fake_mountinfo.write_text(
        "29 1 259:2 / / rw,relatime shared:1 - ext4 /dev/nvme0n1p2 rw\n"
        "334 33 0:49 / /tmp rw,nosuid,nodev shared:299 - tmpfs tmpfs rw,inode64\n"
        f"335 33 259:2 / {disk_dir} rw,relatime shared:300 - ext4 /dev/nvme0n1p3 rw\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(gt.PROC_MOUNTINFO_ENV, str(fake_mountinfo))
    monkeypatch.delenv("SABEROPS_GATE_TMP", raising=False)
    monkeypatch.setenv("TMPDIR", "/tmp")

    assert _tmpfs_mount_for(Path("/tmp/some-dir")) == "TMPFS"
    # Verify sep_index + 1 column bug is fixed (ext4, not /dev/nvme0n1p3!).
    assert _tmpfs_mount_for(disk_dir / "nested") == "EXT4"

    chosen = gt.disk_scratch_base(disk_fallbacks=(disk_dir,))
    assert chosen == disk_dir

    # Fixed verification snapshot & source_tree also obey disk_scratch_base().
    repo = _init_repo(tmp_path / "repo")
    monkeypatch.setenv("SABEROPS_GATE_TMP", str(disk_dir))
    assert gt.disk_scratch_base() == disk_dir
    assert source_tree(repo) is not None
    with fixed_snapshot(repo) as snap:
        assert snap.path.exists()
    # Clean snapshot removal left zero snapshot dirs behind.
    assert [p for p in disk_dir.iterdir() if p.name.startswith("saberops-")] == []
    # RAM safety floor is a multi-GiB reserve, not 16 MiB.
    assert _DEFAULT_SAFETY_FLOOR_BYTES >= 2 * gt.GB


def test_passing_per_change_gate_leaves_no_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Passing gate-affected.sh removes its temporary scratch directory completely."""
    root = Path(__file__).resolve().parents[1]
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "src" / "pkg").mkdir(parents=True)
    (repo / "src" / "pkg" / "__init__.py").write_text(
        '"""Minimal module."""\n\n\ndef add(a: int, b: int) -> int:\n    return a + b\n',
        encoding="utf-8",
    )
    (repo / "tests").mkdir()
    (repo / "tests" / "test_ok.py").write_text(
        "from pkg import add\n\n\ndef test_ok() -> None:\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    (repo / ".gitignore").write_text(
        ".venv/\n__pycache__/\n.mypy_cache/\n.pytest_cache/\n",
        encoding="utf-8",
    )
    (repo / "pyproject.toml").write_text(
        '[tool.ruff]\nexclude = ["scripts"]\n'
        '[tool.pytest.ini_options]\npythonpath = ["src"]\n'
        '[tool.mypy]\npython_version = "3.12"\nmypy_path = ["src"]\n',
        encoding="utf-8",
    )
    scripts = repo / "scripts"
    scripts.mkdir()
    for script_name in ("gate_tools.py", "select_tests.py", "gate-affected.sh"):
        shutil.copy2(root / "scripts" / script_name, scripts / script_name)
    # Reuse the current Python environment cleanly for mypy/ruff/pytest inside temp repo.
    os.symlink(root / ".venv", repo / ".venv")
    subprocess.check_output(["git", "add", "-A"], cwd=repo)
    subprocess.check_output(["git", "commit", "-qm", "seed"], cwd=repo)
    # Create a small passing change on top of main.
    (repo / "src" / "pkg" / "__init__.py").write_text(
        '"""Minimal module."""\n\n\ndef add(a: int, b: int) -> int:\n    return a + b\n',
        encoding="utf-8",
    )
    (repo / "tests" / "test_ok.py").write_text(
        "from pkg import add\n\n\ndef test_ok() -> None:\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )

    scratch_root = tmp_path / "gate-scratch"
    scratch_root.mkdir()
    meminfo = tmp_path / "meminfo"
    avail_bytes = gt.DESKTOP_RESERVE_BYTES + 6 * gt.GB
    meminfo.write_text(
        f"MemTotal: {avail_bytes * 2 // 1024} kB\nMemAvailable: {avail_bytes // 1024} kB\n",
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["SABEROPS_GATE_TMP"] = str(scratch_root)
    env["SABEROPS_GATE_KEEP"] = "0"
    env["SABEROPS_GATE_MIN_FREE_GB"] = "0"
    env["SABEROPS_GATE_CGROUP"] = "0"
    env[gt.PROC_MEMINFO_ENV] = str(meminfo)
    env[gt.PROC_PRESSURE_MEMORY_ENV] = str(tmp_path / "no-psi")

    proc = subprocess.run(
        ["bash", str(scripts / "gate-affected.sh"), "--base", "HEAD"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "[memory-budget] step=mypy" in proc.stdout
    assert "[memory-budget] step=affected" in proc.stdout
    # Passing gate cleaned up ALL temporary files inside scratch_root.
    assert list(scratch_root.iterdir()) == []


def test_zero_configured_worker_or_slot_counts_remain_in_gate_code() -> None:
    """Assert zero configured worker/slot counts or fixed cgroup ceilings remain."""
    root = Path(__file__).resolve().parents[1]
    gate_files = [
        root / "scripts" / "gate_tools.py",
        root / "scripts" / "gate-affected.sh",
        root / "scripts" / "build_test_map.py",
        root / "scripts" / "gate_lib.sh",
        root / "src" / "saberops" / "gate_runner.py",
    ]
    forbidden = (
        "DEFAULT_GATE_WORKERS",
        "DEFAULT_LANE_WORKERS",
        "DEFAULT_MAP_WORKERS",
        "SABEROPS_GATE_WORKERS",
        "SABEROPS_LANE_WORKERS",
        "SABEROPS_MAP_WORKERS",
        "SABEROPS_GATE_SLOTS",
        "SABEROPS_LANE_SLOTS",
        "MAX_LIGHT_SLOTS",
        "CGROUP_MEMORY_HIGH",
        "CGROUP_MEMORY_MAX",
        "SAFETY_FLOOR_GIB",
        "LIGHT_SLOT_GIB",
        "HEAVY_SLOT_GIB",
        "WORKER_SESSION_GIB",
        "dynamic_light_slots",
        "dynamic_heavy_slots",
        "dynamic_worker_count",
        "resolve_slot_counts",
        "resolve_slot_capacity",
        "-n auto",
    )
    for path in gate_files:
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in text, f"forbidden token {token!r} still in {path}"


def test_gate_runner_and_configured_gate_record_memory_visibility(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both GateRunner and _run_configured_gate record grant/peak/wait/freeze visibility."""
    repo = _init_repo(tmp_path / "repo")
    avail_bytes = gt.DESKTOP_RESERVE_BYTES + 6 * gt.GB
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(
        f"MemTotal: {avail_bytes * 2 // 1024} kB\nMemAvailable: {avail_bytes // 1024} kB\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(gt.PROC_MEMINFO_ENV, str(meminfo))
    monkeypatch.setenv("SABEROPS_GATE_TMP", str(tmp_path / "disk-tmp"))

    evidence_dir = tmp_path / "evidence"
    head_sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True
    ).strip()
    req = GateRunnerRequest(
        target_repo=repo,
        gate_command=f"{sys.executable} -c pass",
        timeout=30.0,
        frozen_gate_scratch_mode=GateScratchMode.DISK,
        candidate_sha=head_sha,
        expected_post_gate_sha="HEAD",
        evidence_dir=evidence_dir,
    )
    receipt = GateRunner().run(req, disk_guard=lambda _r: None)
    assert receipt.passed()
    assert receipt.memory_grant_bytes > 0
    assert receipt.memory_wait_seconds >= 0.0
    assert receipt.memory_freeze_count == 0
    receipt_files = list(evidence_dir.glob("gate-receipt.*.json"))
    assert len(receipt_files) == 1
    decoded = json.loads(receipt_files[0].read_text(encoding="utf-8"))
    assert decoded["memory_grant_bytes"] == receipt.memory_grant_bytes

    cfg = RunConfig(task="t", target_repo=str(repo), gate_command=f"{sys.executable} -c pass")
    res = _run_configured_gate(cfg, [sys.executable, "-c", "pass"], cwd=repo, timeout=30.0)
    assert res.passed
    assert res.memory_grant_bytes > 0
    assert res.memory_wait_seconds >= 0.0


def test_scoped_launch_failure_propagates_without_masking_exit_127(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unlaunchable target binaries report launch_failed=True even inside systemd-run."""
    repo = _init_repo(tmp_path / "repo")
    non_exec = repo / "non_exec.sh"
    non_exec.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    non_exec.chmod(0o644)
    subprocess.check_output(["git", "add", "-A"], cwd=repo)
    subprocess.check_output(["git", "commit", "-qm", "add non_exec"], cwd=repo)

    avail_bytes = gt.DESKTOP_RESERVE_BYTES + 6 * gt.GB
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(
        f"MemTotal: {avail_bytes * 2 // 1024} kB\nMemAvailable: {avail_bytes // 1024} kB\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(gt.PROC_MEMINFO_ENV, str(meminfo))
    monkeypatch.setenv("SABEROPS_GATE_TMP", str(tmp_path / "disk-tmp"))
    monkeypatch.delenv("SABEROPS_GATE_CGROUP", raising=False)

    cfg = RunConfig(task="t", target_repo=str(repo), gate_command="/nonexistent/path/missing_gate")
    res_missing = _run_configured_gate(
        cfg, ["/nonexistent/path/missing_gate"], cwd=repo, timeout=30.0
    )
    assert res_missing.launch_failed is True
    assert res_missing.exit_code == -1

    res_perm = _run_configured_gate(cfg, [str(non_exec)], cwd=repo, timeout=30.0)
    assert res_perm.launch_failed is True
    assert res_perm.exit_code == -1

    head_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    req = GateRunnerRequest(
        target_repo=repo,
        gate_command="/nonexistent/path/missing_gate",
        timeout=30.0,
        frozen_gate_scratch_mode=GateScratchMode.DISK,
        candidate_sha=head_sha,
        expected_post_gate_sha="HEAD",
        evidence_dir=tmp_path / "evidence",
    )
    receipt = GateRunner().run(req, disk_guard=lambda _r: None)
    assert receipt.launch_failed is True
    assert receipt.exit_code == -1
    assert not receipt.passed()

    # A valid binary that actually runs and exits 127 must NOT be misclassified as launch_failed.
    res_real_127 = _run_configured_gate(
        cfg, [sys.executable, "-c", "raise SystemExit(127)"], cwd=repo, timeout=30.0
    )
    assert res_real_127.launch_failed is False
    assert res_real_127.exit_code == 127


def test_parallel_step_grant_does_not_collapse_to_one_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: E2 -- a stale one-worker total peak must not freeze worker count."""
    monkeypatch.setattr(gt, "cpu_core_count", lambda: 8)
    reserve = 2 * gt.GB
    avail = reserve + 6 * gt.GB
    ledger = gt.MemoryLedger(
        tmp_path / "ledger",
        reserve_bytes=reserve,
        cold_start_peak_bytes=1 * gt.GB,
        available_memory_fn=lambda: avail,
        pressure_fn=lambda: gt.MemoryPressure(0.0, 0.0),
    )
    # A partial or one-worker run recorded a small total peak.
    ledger.record_peak("full", "full", 142 * gt.MIB, workers=1)
    assert ledger.predict_peak("full", "full") == 142 * gt.MIB
    with ledger.acquire("full", "full", wait=0.0) as lease:
        assert lease.grant_bytes > 142 * gt.MIB
        assert ledger.workers_for_grant(lease.grant_bytes, "full", "full") >= 2
    # A learned total that genuinely exceeds the budget still waits.
    ledger.record_peak("full", "full", 8 * gt.GB, workers=4)
    with pytest.raises(TimeoutError):
        with ledger.acquire("full", "full", wait=0.05, notify=lambda _m: None):
            pass
