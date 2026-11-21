"""Regression tests for gate memory-pressure freeze lifetime.

A frozen step cgroup must never outlive the coordination that froze it:

* ``tests/test_memory_budget.py`` once saw a scoped launch stay frozen for
  73 minutes because a *different* lane's gate froze it and then exited.
* The freezer may exit, time out, or be killed; the frozen cgroup must still be
  thawed by the exiting gate, by the next coordination pass, or by a bound on
  how long a step may stay frozen.
* Only scopes that belong to real gate steps may be frozen at all: a test's own
  scoped launch must never be selected.

All cgroups here are fake directories under ``tmp_path``; no real cgroup or
process is frozen, thawed or killed.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

from saberops._gate_tools import gate_tools as gt

GiB = gt.GB
MiB = gt.MIB


class FakeCgroups:
    """Fake cgroup directories whose ``cgroup.freeze`` file is the fake freezer."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.freeze_calls: list[Path] = []
        self.thaw_calls: list[Path] = []

    def make(self, name: str) -> Path:
        cg = self.root / name
        cg.mkdir(parents=True, exist_ok=True)
        (cg / "cgroup.freeze").write_text("0\n", encoding="utf-8")
        (cg / "memory.current").write_text(str(100 * MiB), encoding="utf-8")
        return cg

    def state(self, cg: Path) -> str:
        return (cg / "cgroup.freeze").read_text(encoding="utf-8").strip()

    def freeze(self, cg: Path, unit_name: str = "") -> bool:
        if unit_name and unit_name not in str(cg):
            return False
        self.freeze_calls.append(cg)
        (cg / "cgroup.freeze").write_text("1\n", encoding="utf-8")
        return True

    def thaw(self, cg: Path, unit_name: str = "") -> bool:
        if unit_name and unit_name not in str(cg):
            return False
        self.thaw_calls.append(cg)
        (cg / "cgroup.freeze").write_text("0\n", encoding="utf-8")
        return True


def _ledger(
    root: Path,
    fakes: FakeCgroups,
    *,
    pressure: list[gt.MemoryPressure],
    pid: int,
    alive: set[int],
    now: list[float],
    max_freeze_seconds: float = 300.0,
) -> gt.MemoryLedger:
    return gt.MemoryLedger(
        root,
        reserve_bytes=0,
        cold_start_peak_bytes=1 * GiB,
        available_memory_fn=lambda: 64 * GiB,
        pressure_fn=lambda: pressure[0],
        cgroup_current_fn=lambda _cg: 100 * MiB,
        freeze_fn=fakes.freeze,
        thaw_fn=fakes.thaw,
        clock=lambda: now[0],
        wall_clock=lambda: now[0],
        pid_fn=lambda: pid,
        pid_alive_fn=lambda candidate: candidate in alive,
        freeze_cooldown=0.0,
        max_freeze_seconds=max_freeze_seconds,
    )


def _freeze_newest(ledger: gt.MemoryLedger, pressure: list[gt.MemoryPressure]) -> None:
    """Drive two coordination passes so ``was_active`` is set before freezing."""
    pressure[0] = gt.MemoryPressure(some_avg10=7.0, full_avg10=0.0)
    ledger.step_feedback()
    pressure[0] = gt.MemoryPressure(some_avg10=22.0, full_avg10=2.5)
    ledger.step_feedback()


def test_gate_exit_thaws_targets_it_froze(tmp_path: Path) -> None:
    """A gate that froze another step thaws it when that gate exits."""
    fakes = FakeCgroups(tmp_path)
    pressure = [gt.MemoryPressure(0.0, 0.0)]
    now = [1_000.0]
    alive = {1111, 2222}
    root = tmp_path / "ledger"
    coordinator = _ledger(
        root, fakes, pressure=pressure, pid=1111, alive=alive, now=now
    )
    target = _ledger(root, fakes, pressure=pressure, pid=2222, alive=alive, now=now)

    coord_cg = fakes.make("cg-coordinator")
    target_cg = fakes.make("cg-target")
    lease_coord = coordinator.acquire("full", "full")
    lease_coord.set_cgroup_path(coord_cg, unit_name="cg-coordinator")
    lease_coord.set_freeze_eligible()
    lease_target = target.acquire("affected", "small")
    lease_target.set_cgroup_path(target_cg, unit_name="cg-target")
    lease_target.set_freeze_eligible()
    now[0] += 1.0

    _freeze_newest(coordinator, pressure)
    assert fakes.state(target_cg) == "1"

    # The coordinator's gate exits: it must release every target it froze.
    coordinator.thaw_holders_frozen_by_me()
    assert fakes.state(target_cg) == "0"
    assert fakes.thaw_calls[-1] == target_cg

    lease_coord.release()
    lease_target.release()


def test_dead_freezer_is_thawed_by_the_next_coordination_pass(tmp_path: Path) -> None:
    """A frozen step whose freezer process is gone is thawed by another pass."""
    fakes = FakeCgroups(tmp_path)
    pressure = [gt.MemoryPressure(0.0, 0.0)]
    now = [2_000.0]
    alive = {1111, 2222}
    root = tmp_path / "ledger"
    coordinator = _ledger(
        root, fakes, pressure=pressure, pid=1111, alive=alive, now=now
    )
    target = _ledger(root, fakes, pressure=pressure, pid=2222, alive=alive, now=now)

    coord_cg = fakes.make("cg-coordinator")
    target_cg = fakes.make("cg-target")
    lease_coord = coordinator.acquire("full", "full")
    lease_coord.set_cgroup_path(coord_cg, unit_name="cg-coordinator")
    lease_coord.set_freeze_eligible()
    lease_target = target.acquire("affected", "small")
    lease_target.set_cgroup_path(target_cg, unit_name="cg-target")
    lease_target.set_freeze_eligible()
    now[0] += 1.0

    _freeze_newest(coordinator, pressure)
    assert fakes.state(target_cg) == "1"

    # The freezer exits without a chance to thaw (timeout/SIGKILL).
    alive.discard(1111)
    pressure[0] = gt.MemoryPressure(some_avg10=4.0, full_avg10=0.0)
    target.step_feedback()
    assert fakes.state(target_cg) == "0"

    lease_coord.release()
    lease_target.release()


def test_freeze_duration_is_bounded_even_with_a_live_freezer(tmp_path: Path) -> None:
    """A step cannot stay frozen past the bound while its freezer stays alive."""
    fakes = FakeCgroups(tmp_path)
    pressure = [gt.MemoryPressure(0.0, 0.0)]
    now = [3_000.0]
    alive = {1111, 2222}
    root = tmp_path / "ledger"
    coordinator = _ledger(
        root,
        fakes,
        pressure=pressure,
        pid=1111,
        alive=alive,
        now=now,
        max_freeze_seconds=60.0,
    )
    target = _ledger(
        root,
        fakes,
        pressure=pressure,
        pid=2222,
        alive=alive,
        now=now,
        max_freeze_seconds=60.0,
    )

    coord_cg = fakes.make("cg-coordinator")
    target_cg = fakes.make("cg-target")
    lease_coord = coordinator.acquire("full", "full")
    lease_coord.set_cgroup_path(coord_cg, unit_name="cg-coordinator")
    lease_coord.set_freeze_eligible()
    lease_target = target.acquire("affected", "small")
    lease_target.set_cgroup_path(target_cg, unit_name="cg-target")
    lease_target.set_freeze_eligible()
    now[0] += 1.0

    _freeze_newest(coordinator, pressure)
    assert fakes.state(target_cg) == "1"

    # Pressure sits between "clear" and "appears": the ordinary clear path
    # cannot thaw it, so only the freeze-duration bound can.
    now[0] += 61.0
    pressure[0] = gt.MemoryPressure(some_avg10=4.0, full_avg10=0.0)
    target.step_feedback()
    assert fakes.state(target_cg) == "0"

    lease_coord.release()
    lease_target.release()


def test_dead_lease_owner_is_pruned_and_its_cgroup_thawed(tmp_path: Path) -> None:
    """A holder whose owner process died is pruned and its frozen cgroup thawed."""
    fakes = FakeCgroups(tmp_path)
    pressure = [gt.MemoryPressure(0.0, 0.0)]
    now = [4_000.0]
    alive = {1111, 2222}
    root = tmp_path / "ledger"
    coordinator = _ledger(
        root, fakes, pressure=pressure, pid=1111, alive=alive, now=now
    )
    owner = _ledger(root, fakes, pressure=pressure, pid=2222, alive=alive, now=now)

    coord_cg = fakes.make("cg-coordinator")
    owner_cg = fakes.make("cg-owner")
    lease_coord = coordinator.acquire("full", "full")
    lease_coord.set_cgroup_path(coord_cg, unit_name="cg-coordinator")
    lease_coord.set_freeze_eligible()
    lease_owner = owner.acquire("affected", "small")
    lease_owner.set_cgroup_path(owner_cg, unit_name="cg-owner")
    lease_owner.set_freeze_eligible()
    now[0] += 1.0

    _freeze_newest(coordinator, pressure)
    assert fakes.state(owner_cg) == "1"

    # Simulate an abrupt owner death: the flock is gone but the metadata remains.
    fcntl.flock(lease_owner._lock_file, fcntl.LOCK_UN)
    lease_owner._lock_file.close()
    lease_owner._released = True

    coordinator.active_holders()
    assert fakes.state(owner_cg) == "0"
    assert not (root / "holders" / f"{lease_owner.holder_id}.json").exists()

    lease_coord.release()


def test_only_freeze_eligible_gate_steps_are_frozen(tmp_path: Path) -> None:
    """A scoped launch that is not a real gate step is never a freeze target."""
    fakes = FakeCgroups(tmp_path)
    pressure = [gt.MemoryPressure(0.0, 0.0)]
    now = [5_000.0]
    alive = {1111}
    root = tmp_path / "ledger"
    ledger = _ledger(root, fakes, pressure=pressure, pid=1111, alive=alive, now=now)

    launch_cg = fakes.make("cg-test-launch")
    launch = ledger.acquire("configured-gate", "default")
    launch.set_cgroup_path(launch_cg, unit_name="cg-test-launch")
    now[0] += 1.0

    _freeze_newest(ledger, pressure)
    assert fakes.state(launch_cg) == "0"
    assert launch_cg not in fakes.freeze_calls

    # The same holder becomes a real gate step and is then eligible.
    launch.set_freeze_eligible()
    now[0] += 1.0
    pressure[0] = gt.MemoryPressure(some_avg10=23.0, full_avg10=2.6)
    ledger.step_feedback()
    assert fakes.state(launch_cg) == "1"

    launch.release()
    assert fakes.state(launch_cg) == "0"


def test_coordinator_never_freezes_its_own_step(tmp_path: Path) -> None:
    """A coordination pass skips its own holder; a frozen coordinator cannot thaw."""
    fakes = FakeCgroups(tmp_path)
    pressure = [gt.MemoryPressure(0.0, 0.0)]
    now = [5_500.0]
    alive = {1111}
    ledger = _ledger(
        tmp_path / "ledger", fakes, pressure=pressure, pid=1111, alive=alive, now=now
    )
    own_cg = fakes.make("cg-own")
    lease = ledger.acquire("full", "full")
    lease.set_cgroup_path(own_cg, unit_name="cg-own")
    lease.set_freeze_eligible()
    now[0] += 1.0

    pressure[0] = gt.MemoryPressure(some_avg10=7.0, full_avg10=0.0)
    ledger.step_feedback(caller_holder_id=lease.holder_id)
    pressure[0] = gt.MemoryPressure(some_avg10=22.0, full_avg10=2.5)
    ledger.step_feedback(caller_holder_id=lease.holder_id)
    assert fakes.state(own_cg) == "0"

    # The same target is frozen once a different coordinator picks it.
    pressure[0] = gt.MemoryPressure(some_avg10=23.0, full_avg10=2.6)
    ledger.step_feedback()
    assert fakes.state(own_cg) == "1"
    lease.release()
    assert fakes.state(own_cg) == "0"


def test_tests_use_an_isolated_memory_ledger(tmp_path: Path) -> None:
    """Ordinary tests never resolve the real per-user shared ledger."""
    configured = os.environ.get(gt.LEDGER_ENV, "").strip()
    assert configured, "tests must set SABEROPS_MEMORY_LEDGER"
    assert Path(configured) != gt.disk_scratch_base() / gt.LEDGER_DIRNAME
    # Even a path that is not a git repository must not fall back to /var/tmp.
    assert gt.default_ledger_root(repo=tmp_path / "not-a-repo") == Path(configured)
