"""Dynamic host-wide memory budget, shared ledger, and dead-holder reaping tests.

Covers the shared implementation in ``scripts/gate_tools.py`` and its
delegating engine pool (:class:`saberops.gate_runner.GateSlotPool`).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from saberops._gate_tools import gate_tools as gt
from saberops.gate_runner import (
    GIB,
    GateSlotPool,
    GateSlotWaitTimeout,
    available_memory_bytes,
    cpu_core_count,
    read_meminfo,
    total_memory_bytes,
)

_DEAD_PID = 999_999_999


def _write_meminfo(path: Path, **fields_kb: int) -> Path:
    lines = [f"{name}: {value} kB" for name, value in fields_kb.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _pin_pressure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin zero PSI so other lanes' load cannot park these ledger tests."""
    pressure = tmp_path / "memory.pressure"
    pressure.write_text(
        "some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
        "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(gt.PROC_PRESSURE_MEMORY_ENV, str(pressure))


def test_meminfo_is_parsed_into_bytes(tmp_path: Path) -> None:
    """Covers: MemAvailable/MemTotal read from /proc/meminfo."""
    fake = _write_meminfo(tmp_path / "meminfo", MemTotal=1_000, MemAvailable=400)
    parsed = read_meminfo(fake)
    assert parsed["MemTotal"] == 1_000 * 1024
    assert parsed["MemAvailable"] == 400 * 1024
    assert gt.read_meminfo(fake) == parsed


def test_meminfo_unreadable_is_empty_and_never_raises(tmp_path: Path) -> None:
    """Covers: absent /proc/meminfo is not a failure."""
    assert read_meminfo(tmp_path / "does-not-exist") == {}
    assert gt.read_meminfo(tmp_path / "does-not-exist") == {}


def test_available_memory_falls_back_to_free_buffers_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: MemAvailable fallback to MemFree+Buffers+Cached."""
    fake = _write_meminfo(
        tmp_path / "meminfo", MemFree=100, Buffers=20, Cached=30, MemTotal=1_000
    )
    parsed = read_meminfo(fake)
    monkeypatch.setattr(gt, "read_meminfo", lambda path=None: parsed)
    assert available_memory_bytes() == (100 + 20 + 30) * 1024
    assert total_memory_bytes() == 1_000 * 1024


def test_sysconf_fallback_when_meminfo_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Covers: os.sysconf fallback carries no extra dependency."""
    monkeypatch.delenv(gt.PROC_MEMINFO_ENV, raising=False)
    monkeypatch.setattr(gt, "read_meminfo", lambda path=None: {})
    monkeypatch.setattr(
        os,
        "sysconf",
        lambda name: {"SC_PHYS_PAGES": 10, "SC_AVPHYS_PAGES": 4, "SC_PAGE_SIZE": 4096}[name],
    )
    assert total_memory_bytes() == 10 * 4096
    assert available_memory_bytes() == 4 * 4096


def test_cpu_core_count_is_at_least_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """Covers: core count never collapses to zero."""
    monkeypatch.setattr(os, "cpu_count", lambda: 0)
    assert cpu_core_count() == 1
    assert gt.cpu_core_count() == 1


# ---------------------------------------------------------------------------
# Engine GateSlotPool delegation to host-wide MemoryLedger
# ---------------------------------------------------------------------------


def test_slot_pool_uses_shared_ledger_and_reaps_unflocked_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: GateSlotPool shares gate_tools.MemoryLedger and cleans dead metadata."""
    avail_kb = (gt.DESKTOP_RESERVE_BYTES + 4 * GIB) // 1024
    meminfo = _write_meminfo(tmp_path / "meminfo", MemTotal=avail_kb * 2, MemAvailable=avail_kb)
    monkeypatch.setenv(gt.PROC_MEMINFO_ENV, str(meminfo))
    _pin_pressure(tmp_path, monkeypatch)
    monkeypatch.delenv(gt.LEDGER_ENV, raising=False)
    common = tmp_path / "common"
    holders = common / gt.LEDGER_DIRNAME / "holders"
    holders.mkdir(parents=True)
    # Stale unlocked holder left behind by an abrupt crash.
    (holders / "stale-1.lock").write_text("", encoding="utf-8")
    (holders / "stale-1.json").write_text(
        json.dumps({"holder_id": "stale-1", "pid": _DEAD_PID, "grant_bytes": 99 * GIB}),
        encoding="utf-8",
    )
    pool = GateSlotPool(common)
    with pool.light(wait=0.2):
        assert not (holders / "stale-1.json").exists()
        assert pool.current_lease() is not None


def test_slot_pool_blocks_when_budget_cannot_cover_predicted_peak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: GateSlotPool waits and raises GateSlotWaitTimeout when memory is tight."""
    low_kb = (gt.DESKTOP_RESERVE_BYTES + 100 * gt.MIB) // 1024
    meminfo = _write_meminfo(tmp_path / "meminfo", MemTotal=low_kb * 10, MemAvailable=low_kb)
    monkeypatch.setenv(gt.PROC_MEMINFO_ENV, str(meminfo))
    _pin_pressure(tmp_path, monkeypatch)
    monkeypatch.delenv(gt.LEDGER_ENV, raising=False)
    pool = GateSlotPool(tmp_path / "common")
    with pytest.raises(GateSlotWaitTimeout):
        with pool.heavy(wait=0.15):
            pytest.fail("heavy entered when predicted peak exceeded budget")


def test_gate_runner_has_no_configured_slot_or_worker_counts() -> None:
    """Verify GateRunner removed legacy fixed/formulaic slot and worker counts."""
    import saberops.gate_runner as gr

    removed = (
        "DEFAULT_GATE_WORKERS",
        "DEFAULT_LANE_WORKERS",
        "HEAVY_SLOT_GIB",
        "LIGHT_SLOT_GIB",
        "MAX_LIGHT_SLOTS",
        "SAFETY_FLOOR_GIB",
        "WORKER_SESSION_GIB",
        "SlotCapacity",
        "dynamic_heavy_slots",
        "dynamic_light_slots",
        "dynamic_worker_count",
        "resolve_slot_capacity",
    )
    for name in removed:
        assert not hasattr(gr, name), f"{name} should have been removed from gate_runner"
        assert not hasattr(gt, name), f"{name} should have been removed from gate_tools"
