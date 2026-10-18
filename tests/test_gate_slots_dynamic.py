"""D-0076: dynamic RAM-based slots, worker auto-scaling and dead-PID self-healing.

Covers the two implementations that must agree: the product pool
(:class:`saberops.gate_runner.GateSlotPool` and its memory helpers) and the
developer gate (``scripts/gate_tools.py``).  Every memory reading is injected, so
the tests never depend on the host's real RAM.
"""

from __future__ import annotations

import fcntl
import importlib.util
import itertools
import json
import os
import sys
from pathlib import Path
from types import ModuleType

import pytest

from saberops.gate_runner import (
    GIB,
    MAX_LIGHT_SLOTS,
    GateSlotPool,
    GateSlotWaitTimeout,
    available_memory_bytes,
    cpu_core_count,
    dynamic_heavy_slots,
    dynamic_light_slots,
    dynamic_worker_count,
    purge_stale_slot_metadata,
    read_meminfo,
    resolve_slot_capacity,
    total_memory_bytes,
    wait_for_memory_budget,
)

_DEAD_PID = 999_999_999


class _FakeClock:
    """Deterministic monotonic clock + sleep for the memory-guard tests."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _load_gate_tools() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "gate_tools_dynamic", Path(__file__).resolve().parents[1] / "scripts/gate_tools.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gt = _load_gate_tools()


# ---------------------------------------------------------------------------
# Hermetic memory detection (both implementations)
# ---------------------------------------------------------------------------


def _write_meminfo(path: Path, **fields_kb: int) -> Path:
    lines = [f"{name}: {value} kB" for name, value in fields_kb.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_meminfo_is_parsed_into_bytes(tmp_path: Path) -> None:
    """Covers: D-0076 (MemAvailable/MemTotal read from /proc/meminfo)."""
    fake = _write_meminfo(tmp_path / "meminfo", MemTotal=1_000, MemAvailable=400)
    parsed = read_meminfo(fake)
    assert parsed["MemTotal"] == 1_000 * 1024
    assert parsed["MemAvailable"] == 400 * 1024
    assert gt.read_meminfo(fake) == parsed


def test_meminfo_unreadable_is_empty_and_never_raises(tmp_path: Path) -> None:
    """Covers: D-0076 (absent /proc/meminfo is not a failure)."""
    assert read_meminfo(tmp_path / "does-not-exist") == {}
    assert gt.read_meminfo(tmp_path / "does-not-exist") == {}


def test_available_memory_falls_back_to_free_buffers_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: D-0076 (MemAvailable fallback to MemFree+Buffers+Cached)."""
    fake = _write_meminfo(
        tmp_path / "meminfo", MemFree=100, Buffers=20, Cached=30, MemTotal=1_000
    )
    parsed = read_meminfo(fake)
    monkeypatch.setattr("saberops.gate_runner.read_meminfo", lambda path=None: parsed)
    assert available_memory_bytes() == (100 + 20 + 30) * 1024
    assert total_memory_bytes() == 1_000 * 1024


def test_sysconf_fallback_when_meminfo_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Covers: D-0076 (os.sysconf fallback carries no extra dependency)."""
    monkeypatch.setattr("saberops.gate_runner.read_meminfo", lambda path=None: {})
    monkeypatch.setattr(
        os,
        "sysconf",
        lambda name: {"SC_PHYS_PAGES": 10, "SC_AVPHYS_PAGES": 4, "SC_PAGE_SIZE": 4096}[name],
    )
    assert total_memory_bytes() == 10 * 4096
    assert available_memory_bytes() == 4 * 4096


def test_cpu_core_count_is_at_least_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """Covers: D-0076 (core count never collapses to zero)."""
    monkeypatch.setattr(os, "cpu_count", lambda: 0)
    assert cpu_core_count() == 1
    assert gt.cpu_core_count() == 1


# ---------------------------------------------------------------------------
# Dynamic formulas (both implementations agree)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("available_gib", "cores", "expected"),
    [
        (0.5, 8, 1),  # below the 2 GiB safety floor -> floor of one slot
        (3.0, 8, 1),  # (3 - 2) // 1 = 1
        (6.0, 8, 4),  # (6 - 2) // 1 = 4, cpu//2 = 4
        (6.0, 3, 1),  # cpu//2 = 1 caps it
        (64.0, 64, MAX_LIGHT_SLOTS),  # the ceiling caps it
    ],
)
def test_dynamic_light_slots_formula(
    available_gib: float, cores: int, expected: int
) -> None:
    """Covers: D-0076 light formula max(1, min(MAX, RAM, cpu//2))."""
    available = int(available_gib * GIB)
    assert dynamic_light_slots(available_bytes=available, cpu_cores=cores) == expected
    assert gt.dynamic_light_slots(available, cores) == expected


def test_dynamic_light_slots_without_memory_is_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Covers: D-0076 (unreadable memory degrades to a single light slot)."""
    monkeypatch.setattr("saberops.gate_runner.available_memory_bytes", lambda: None)
    assert dynamic_light_slots() == 1
    monkeypatch.setattr(gt, "available_memory_bytes", lambda: None)
    assert gt.dynamic_light_slots() == 1


@pytest.mark.parametrize(
    ("total_gib", "available_gib", "cores", "expected"),
    [
        (16.0, 8.0, 16, 1),  # below 32 GiB total
        (32.0, 7.0, 16, 1),  # below 8 GiB available
        (32.0, 8.0, 11, 1),  # below 12 cores
        (32.0, 8.0, 12, 2),  # exactly at every threshold
        (64.0, 40.0, 32, 2),  # a large idle host
    ],
)
def test_dynamic_heavy_slots_thresholds(
    total_gib: float, available_gib: float, cores: int, expected: int
) -> None:
    """Covers: D-0076 (heavy default 1, doubling only on a large idle host)."""
    total = int(total_gib * GIB)
    available = int(available_gib * GIB)
    assert (
        dynamic_heavy_slots(total_bytes=total, available_bytes=available, cpu_cores=cores)
        == expected
    )
    assert gt.dynamic_heavy_slots(total, available, cores) == expected


@pytest.mark.parametrize(
    ("available_gib", "cores", "expected"),
    [
        (10.0, 8, 8),  # min(cores, 10/0.6=16) = 8
        (3.0, 8, 5),  # min(8, 5) = 5
        (0.5, 8, 1),  # never below one worker
        (64.0, 4, 4),  # never above the core count
    ],
)
def test_dynamic_worker_count_formula(
    available_gib: float, cores: int, expected: int
) -> None:
    """Covers: D-0076 workers = max(1, min(cores, MemAvailable // 0.6 GiB))."""
    available = int(available_gib * GIB)
    assert dynamic_worker_count(available_bytes=available, cpu_cores=cores) == expected
    assert gt.dynamic_worker_count(available, cores) == expected


def test_dynamic_worker_count_unreadable_memory_keeps_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Covers: D-0076 (unreadable memory keeps the conservative worker cap)."""
    monkeypatch.setattr("saberops.gate_runner.available_memory_bytes", lambda: None)
    monkeypatch.setattr("saberops.gate_runner.cpu_core_count", lambda: 64)
    assert dynamic_worker_count() == 64  # product pool trusts the core count
    monkeypatch.setattr(gt, "available_memory_bytes", lambda: None)
    monkeypatch.setattr(gt, "cpu_core_count", lambda: 64)
    assert gt.dynamic_worker_count() == gt.DEFAULT_GATE_WORKERS


# ---------------------------------------------------------------------------
# Slot resolution: env override vs dynamic policy
# ---------------------------------------------------------------------------


def test_resolve_slot_capacity_env_overrides_dynamic(monkeypatch: pytest.MonkeyPatch) -> None:
    """Covers: D-0076 (explicit env knobs stay authoritative)."""
    capacity = resolve_slot_capacity(
        {"SABEROPS_GATE_SLOTS": "2", "SABEROPS_LANE_SLOTS": "5", "SABEROPS_GATE_SLOT_WAIT": "12"}
    )
    assert (capacity.heavy, capacity.light, capacity.wait) == (2, 5, 12.0)
    assert capacity.source == "env"


def test_resolve_slot_capacity_dynamic_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Covers: D-0076 (no env -> counts derived from live memory)."""
    monkeypatch.setattr("saberops.gate_runner.dynamic_heavy_slots", lambda: 1)
    monkeypatch.setattr("saberops.gate_runner.dynamic_light_slots", lambda: 6)
    capacity = resolve_slot_capacity({})
    assert (capacity.heavy, capacity.light) == (1, 6)
    assert capacity.source == "dynamic"
    assert capacity.drain_lock_count >= MAX_LIGHT_SLOTS + 1


@pytest.mark.parametrize("bad", ["zero", "0", "-1"])
def test_resolve_slot_capacity_rejects_malformed_heavy(bad: str) -> None:
    """Covers: D-0076 (a malformed slot knob still fails closed)."""
    with pytest.raises(GateSlotWaitTimeout):
        resolve_slot_capacity({"SABEROPS_GATE_SLOTS": bad})


def test_gate_tools_resolve_slot_counts_dynamic_and_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Covers: D-0076 (developer gate resolves the same policy)."""
    assert gt.resolve_slot_counts({"SABEROPS_GATE_SLOTS": "1", "SABEROPS_LANE_SLOTS": "2"}) == (
        1,
        2,
        gt.DEFAULT_SLOT_WAIT,
    )
    monkeypatch.setattr(gt, "dynamic_heavy_slots", lambda: 1)
    monkeypatch.setattr(gt, "dynamic_light_slots", lambda: 3)
    assert gt.resolve_slot_counts({}) == (1, 3, gt.DEFAULT_SLOT_WAIT)
    with pytest.raises(ValueError):
        gt.resolve_slot_counts({"SABEROPS_LANE_SLOTS": "lots"})


def test_configured_light_slots_above_ceiling_are_clamped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Covers: D-0076 (a light count above MAX_LIGHT_SLOTS cannot outrun the drain)."""
    capacity = resolve_slot_capacity({"SABEROPS_LANE_SLOTS": "16"})
    assert capacity.light == MAX_LIGHT_SLOTS
    # The drain covers 0..max(light, MAX_LIGHT_SLOTS), so the usable light
    # indices can never exceed the drained range.
    assert capacity.drain_lock_count >= max(capacity.light, MAX_LIGHT_SLOTS) + 1
    assert gt.resolve_slot_counts({"SABEROPS_LANE_SLOTS": "16"})[1] == gt.MAX_LIGHT_SLOTS


def test_heavy_drain_covers_clamped_ceiling_indices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: D-0076 (heavy hold still spans 0..MAX_LIGHT_SLOTS when pinned high)."""
    monkeypatch.setenv("SABEROPS_GATE_MEMORY_GUARD", "0")
    monkeypatch.setenv("SABEROPS_GATE_SLOTS", "1")
    monkeypatch.setenv("SABEROPS_LANE_SLOTS", "16")
    common = tmp_path / "common"
    with GateSlotPool(common).heavy():
        for index in range(MAX_LIGHT_SLOTS + 1):
            handle = (common / "saberops-gate-light-slots" / f"{index}.lock").open("a+")
            try:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                handle.close()


# ---------------------------------------------------------------------------
# Pre-admission memory guard
# ---------------------------------------------------------------------------


def test_memory_budget_waits_then_releases(monkeypatch: pytest.MonkeyPatch) -> None:
    """Covers: D-0076 (guard queues until RAM clears the floor)."""
    readings = iter([100, 100, 500])
    clock = _FakeClock()
    monkeypatch.setattr("saberops.gate_runner.available_memory_bytes", lambda: next(readings))
    notices: list[str] = []
    assert (
        wait_for_memory_budget(
            400, wait=10, notify=notices.append, clock=clock, sleep=clock.sleep
        )
        is True
    )
    assert notices and "waiting for memory budget" in notices[0]


def test_memory_budget_times_out_below_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Covers: D-0076 (guard is bounded by the slot wait, never a new failure)."""
    monkeypatch.setattr("saberops.gate_runner.available_memory_bytes", lambda: 10)
    assert wait_for_memory_budget(400, wait=0, notify=lambda _m: None) is False
    assert wait_for_memory_budget(0, wait=0) is True


def test_memory_budget_unreadable_memory_does_not_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Covers: D-0076 (unreadable memory never becomes a gate failure)."""
    monkeypatch.setattr("saberops.gate_runner.available_memory_bytes", lambda: None)
    assert wait_for_memory_budget(400, wait=0) is True


def test_gate_tools_memory_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Covers: D-0076 (developer gate guard uses injected clock/sleep)."""
    monkeypatch.setattr(gt, "available_memory_bytes", lambda: 0)
    ticks = itertools.count(0.0, 10.0)
    assert (
        gt.wait_for_memory_budget(
            100,
            wait=5,
            notify=lambda _m: None,
            clock=lambda: next(ticks),
            sleep=lambda _s: None,
        )
        is False
    )
    monkeypatch.setattr(gt, "available_memory_bytes", lambda: 10_000)
    assert gt.wait_for_memory_budget(100, wait=5) is True


# ---------------------------------------------------------------------------
# Self-healing stale slot metadata (dead-PID purge)
# ---------------------------------------------------------------------------


def test_purge_stale_slot_metadata_removes_only_dead_pid(tmp_path: Path) -> None:
    """Covers: D-0076 (dead <index>.json is unlinked; a live one is kept)."""
    root = tmp_path / "slots"
    root.mkdir()
    (root / "0.json").write_text(json.dumps({"pid": _DEAD_PID, "worktree": "gone"}))
    (root / "1.json").write_text(json.dumps({"pid": os.getpid(), "worktree": "live"}))
    purged = purge_stale_slot_metadata(root, range(2))
    assert purged == (0,)
    assert not (root / "0.json").exists()
    assert (root / "1.json").exists()


def test_gate_tools_purge_stale_slot_metadata(tmp_path: Path) -> None:
    """Covers: D-0076 (developer gate purges phantom holders too)."""
    root = tmp_path / "slots"
    root.mkdir()
    (root / "0.json").write_text(json.dumps({"pid": _DEAD_PID}))
    assert gt.purge_stale_slot_metadata(root, range(1)) == (0,)
    assert not (root / "0.json").exists()


def test_slot_pool_purges_dead_holder_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: D-0076 (GateSlotPool clears a phantom light holder)."""
    monkeypatch.setenv("SABEROPS_GATE_MEMORY_GUARD", "0")
    monkeypatch.setenv("SABEROPS_LANE_SLOTS", "2")
    common = tmp_path / "common"
    light_root = common / "saberops-gate-light-slots"
    light_root.mkdir(parents=True)
    (light_root / "0.json").write_text(json.dumps({"pid": _DEAD_PID, "worktree": "gone"}))
    with GateSlotPool(common).light():
        assert not (light_root / "0.json").exists()


def test_gate_tools_slot_never_notices_a_dead_pid(tmp_path: Path) -> None:
    """Covers: D-0076 (a wait notice never names a dead PID)."""
    root = tmp_path / "slot"
    root.mkdir()
    (root / "0.json").write_text(json.dumps({"pid": _DEAD_PID, "worktree": "gone"}))
    notices: list[str] = []
    holder = (root / "0.lock").open("a+")
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(TimeoutError):
            with gt.slot(root, {}, wait=0.3, notify=notices.append):
                pytest.fail("slot acquired while lock held")
    finally:
        holder.close()
    assert notices
    assert all(str(_DEAD_PID) not in notice for notice in notices)
    assert not (root / "0.json").exists()


# ---------------------------------------------------------------------------
# Heavy-gate monopoly invariant: drain 0..MAX_LIGHT_SLOTS
# ---------------------------------------------------------------------------


def test_heavy_gate_drains_every_potential_light_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: D-0076 (heavy drains 0..MAX_LIGHT_SLOTS regardless of count)."""
    monkeypatch.setenv("SABEROPS_GATE_MEMORY_GUARD", "0")
    monkeypatch.setenv("SABEROPS_GATE_SLOTS", "1")
    monkeypatch.setenv("SABEROPS_LANE_SLOTS", "1")  # a smaller dynamic light count
    common = tmp_path / "common"
    with GateSlotPool(common).heavy():
        for index in range(MAX_LIGHT_SLOTS + 1):
            handle = (common / "saberops-gate-light-slots" / f"{index}.lock").open("a+")
            try:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                handle.close()


def test_gate_tools_drain_max_holds_every_potential_light_index(tmp_path: Path) -> None:
    """Covers: D-0076 (developer gate drain_max extends to the ceiling)."""
    root = tmp_path / "light"
    with gt.slot(
        root,
        {"pid": os.getpid()},
        count=1,
        wait=5,
        acquire_all=True,
        drain_max=gt.MAX_LIGHT_SLOTS,
        slot_env="SABEROPS_LANE_SLOTS",
    ):
        for index in range(gt.MAX_LIGHT_SLOTS + 1):
            handle = (root / f"{index}.lock").open("a+")
            try:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                handle.close()
