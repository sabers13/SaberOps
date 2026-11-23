"""H19: unused plan-scheduling and logical-tool layers stay deleted.

The chained-wave scheduler, its run report and the logical tool registry were
reached by no owner, planner or scheduler path (H0 sections 8 and 9). These
checks fail if any of them comes back.
"""

from __future__ import annotations

import dataclasses
import importlib.util

import pytest

from saberops import ready_frontier
from saberops.cli import build_parser
from saberops.config import RunConfig
from saberops.run_creation import RunRequest


def test_chained_wave_scheduler_and_run_report_are_absent() -> None:
    for name in (
        "schedule_chained_waves",
        "build_plan_run_report",
        "PlanRunReport",
        "HeldSummary",
    ):
        assert not hasattr(ready_frontier, name)


def test_run_config_and_request_carry_no_tool_refs() -> None:
    assert "tool_refs" not in {field.name for field in dataclasses.fields(RunConfig)}
    assert "tool_refs" not in {field.name for field in dataclasses.fields(RunRequest)}


def test_logical_tool_registry_modules_are_absent() -> None:
    for module in (
        "saberops.tooling.contracts",
        "saberops.tooling.generator",
        "saberops.tooling.invocation",
        "saberops.tooling.registry",
    ):
        assert importlib.util.find_spec(module) is None


def test_cli_refuses_the_tool_flag_and_tools_command() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--task", "task", "--tool", "bundle.tool"])
    with pytest.raises(SystemExit):
        parser.parse_args(["tools", "list"])
