"""H29: the test session admits without the host gate memory guard.

The gate's memory guard is a *host* admission rule: it reads real
``MemAvailable`` and waits for a budget that sibling lanes' gates already hold.
An ordinary test that admits against the host turns another lane's load into a
pytest-timeout victim (``data/so-testprof/report.md`` §4.3b: 22 tests hit the
180 s cap inside ``MemoryLedger.acquire``).  ``tests/conftest.py`` therefore
admits without the guard for every test, and the guard's own contract tests
carry ``pytest.mark.memory_guard`` to get it back.

Grants, learned sizes and worker counts are unchanged; only the wait/refusal
that reads live host memory is skipped.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from saberops._gate_tools import gate_tools


def _starved_ledger(tmp_path: Path) -> gate_tools.MemoryLedger:
    """A ledger that sees no free host memory at all."""
    return gate_tools.MemoryLedger(
        tmp_path / "ledger",
        cold_start_peak_bytes=4 * gate_tools.GB,
        available_memory_fn=lambda: 0,
        pressure_fn=lambda: gate_tools.MemoryPressure(0.0, 0.0),
    )


def test_ordinary_tests_admit_without_the_host_guard(tmp_path: Path) -> None:
    assert gate_tools.memory_guard_enabled() is False
    # No budget at all: with the guard on, this raises MEMORY_BUDGET_TIMEOUT
    # instead of granting (wait=0 makes the failure immediate, never a hang).
    with _starved_ledger(tmp_path).acquire("full", "full", wait=0.0):
        pass


@pytest.mark.memory_guard
def test_the_guards_own_tests_re_enable_it(tmp_path: Path) -> None:
    assert gate_tools.memory_guard_enabled() is True
    with pytest.raises(TimeoutError, match="MEMORY_BUDGET_TIMEOUT"):
        with _starved_ledger(tmp_path).acquire("full", "full", wait=0.0):
            pytest.fail("admitted with no budget while the guard is on")
