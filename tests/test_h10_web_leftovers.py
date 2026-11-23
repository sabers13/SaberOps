"""The web-only planning leftovers stay deleted (phase 1.1 lane H10, ADR-0071).

The wave optimizer, the project entry modes, the wave records and the
orchestrator admission seam had no live caller; re-adding any of them is a new
decision, not an accident a later change may make silently.
"""

from __future__ import annotations

import importlib.util

import pytest

import saberops.ready_frontier as ready_frontier

REMOVED_MODULES = (
    "saberops.project_mode",
    "saberops.wave_admission",
    "saberops.wave_models",
    "saberops.wave_plan_optimizer",
)


@pytest.mark.parametrize("module", REMOVED_MODULES)
def test_removed_planning_modules_stay_deleted(module: str) -> None:
    assert importlib.util.find_spec(module) is None


def test_orchestrator_has_no_wave_admission_seam() -> None:
    from saberops.orchestrator import Orchestrator

    assert not hasattr(Orchestrator, "admit_wave")


def test_ready_frontier_keeps_no_wave_records() -> None:
    for name in ("ScheduledTask", "ScheduledWave", "WaveScheduleError"):
        assert not hasattr(ready_frontier, name)
