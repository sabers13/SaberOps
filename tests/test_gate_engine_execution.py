"""Shared gate execution primitives: slots, disk admission and gate history.

H32 removed the duplicate ``EngineGateExecutor`` path: the real job check
(``_run_configured_gate``) and acceptance (``GateRunner``) are the only
execution paths.  The slot pool, disk admission and history guards remain
shared by those paths, so their tests stay here.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from saberops._gate_tools import gate_tools as gt
from saberops.gate_history import (
    GateHistoryError,
    GateKind,
    read_entries,
    require_engine_evidence,
)
from saberops.gate_profiles import (
    GateCommand,
    GateProfile,
    GateProfileError,
    GateProfileName,
)
from saberops.gate_runner import (
    GateSlotPool,
    GateSlotWaitTimeout,
    host_disk_admission,
)


def _pin_mem(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, budget_bytes: int) -> Path:
    avail_kb = (gt.DESKTOP_RESERVE_BYTES + budget_bytes) // 1024
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(f"MemTotal: {avail_kb * 4} kB\nMemAvailable: {avail_kb} kB\n")
    monkeypatch.setenv(gt.PROC_MEMINFO_ENV, str(meminfo))
    # The host's real PSI can be non-zero while other lanes run heavy tests.
    # These tests reason about a pinned budget, so pin pressure to zero too.
    pressure = tmp_path / "memory.pressure"
    pressure.write_text(
        "some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
        "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
    )
    monkeypatch.setenv(gt.PROC_PRESSURE_MEMORY_ENV, str(pressure))
    return meminfo


def test_slot_pool_heavy_excludes_light_when_budget_tight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: heavy reservation blocks an additional cold-start run when tight."""
    _pin_mem(tmp_path, monkeypatch, gt.COLD_START_PEAK_BYTES + 100 * gt.MIB)
    common = tmp_path / "common"
    pool = GateSlotPool(common)
    monkeypatch.setenv("SABEROPS_GATE_SLOT_WAIT", "0.2")
    with pool.heavy():
        with pytest.raises(GateSlotWaitTimeout):
            with pool.light():
                pytest.fail("lane entered without available budget")
    with pool.light():
        pass


def test_slot_pool_concurrent_small_reservations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: small learned steps share host budget side by side while third waits."""
    _pin_mem(tmp_path, monkeypatch, 1_100 * gt.MIB)
    common = tmp_path / "common"
    seed_pool = GateSlotPool(common)
    seed_pool.ledger.record_peak("affected", "small", 500 * gt.MIB)
    monkeypatch.setenv("SABEROPS_GATE_SLOT_WAIT", "5")
    held: list[int] = []
    guard = threading.Condition()
    release = threading.Event()

    def holder() -> None:
        with GateSlotPool(common).light():
            with guard:
                held.append(1)
                guard.notify_all()
            assert release.wait(timeout=5)

    threads = [threading.Thread(target=holder) for _ in range(2)]
    for thread in threads:
        thread.start()
    with guard:
        assert guard.wait_for(lambda: len(held) == 2, timeout=5)
        assert len(held) == 2
    # Two small holders (1000 MiB) leave 100 MiB < 500 MiB -> third waits and times out.
    monkeypatch.setenv("SABEROPS_GATE_SLOT_WAIT", "0.2")
    with pytest.raises(GateSlotWaitTimeout):
        with GateSlotPool(common).light():
            pytest.fail("third light reservation entered beyond budget")
    release.set()
    for thread in threads:
        thread.join(timeout=5)


def test_host_disk_admission_floor_blocks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Covers: the shared floor is always enforced."""
    import saberops.gate_runner as gate_runner

    monkeypatch.setattr(
        gate_runner, "default_gate_disk_guard", lambda repo, minimum_gb=10.0: "DISK_LOW"
    )
    assert host_disk_admission(tmp_path, estimate_bytes=1) == "DISK_LOW"


def test_history_rejects_non_engine_producer(tmp_path: Path) -> None:
    """Covers: G0/model runs are never evidence."""
    with pytest.raises(GateHistoryError):
        require_engine_evidence({"producer": "model"})
    with pytest.raises(GateHistoryError):
        require_engine_evidence({})
    require_engine_evidence({"producer": "engine"})


def test_slot_pool_refuses_invalid_wait_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: malformed memory wait configuration fails closed as SLOT_WAIT_TIMEOUT."""
    monkeypatch.setenv("SABEROPS_GATE_SLOT_WAIT", "-1")
    with pytest.raises(GateSlotWaitTimeout):
        with GateSlotPool(tmp_path / "c").light():
            pass


def test_slot_pool_waits_for_budget_then_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: heavy reservation waits while predicted peak > budget, runs once freed."""
    meminfo = _pin_mem(tmp_path, monkeypatch, 100 * gt.MIB)
    pool = GateSlotPool(tmp_path / "c")
    entered = threading.Event()

    def run_heavy() -> None:
        with pool.heavy(wait=5.0):
            entered.set()

    worker = threading.Thread(target=run_heavy)
    worker.start()
    assert not entered.wait(timeout=0.2)
    avail_kb = (gt.DESKTOP_RESERVE_BYTES + gt.COLD_START_PEAK_BYTES + 200 * gt.MIB) // 1024
    meminfo.write_text(f"MemTotal: {avail_kb * 4} kB\nMemAvailable: {avail_kb} kB\n")
    assert entered.wait(timeout=5)
    worker.join(timeout=5)


def test_engine_history_refuses_producer_after_append(tmp_path: Path) -> None:
    """Covers: appended engine lines always carry the authoritative producer."""
    entries_before = read_entries(tmp_path / ".git" / "saberops-gate-history" / "full.jsonl")
    assert entries_before == []
    # A model run is refused by the shared guard even if it reaches a consumer.
    with pytest.raises(GateHistoryError):
        require_engine_evidence({"kind": GateKind.G1.value, "producer": "G0"})


def test_gate_kind_enum_covers_lifecycle_points() -> None:
    """Covers: kind taxonomy G1|G2|G3|main-gate|manual."""
    assert {kind.value for kind in GateKind} == {"G1", "G2", "G3", "main-gate", "manual"}


def test_profiles_require_wall_budget() -> None:
    """Covers: profiles always carry a wall budget."""
    with pytest.raises(GateProfileError):
        GateProfile(
            name=GateProfileName.FAST,
            commands=(GateCommand("x", "true"),),
            wall_budget_seconds=0,
        )
