"""Gate-profile project data: defaults, validation, and the no-lowering floor."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from saberops.gate_history import GateKind
from saberops.gate_profiles import (
    ACCEPTANCE_WALL_BUDGET_SECONDS,
    FAST_WALL_BUDGET_SECONDS,
    FULL_WALL_BUDGET_SECONDS,
    PROFILES_FILENAME,
    GateCommand,
    GateProfile,
    GateProfileError,
    GateProfileName,
    acceptance_profile,
    default_profiles,
    load_profiles,
    parse_profiles,
    required_profile,
    resolve_profile,
)


def test_default_budgets_and_names() -> None:
    """Covers: ADR-0003 D2 (fast 10 min, acceptance 20 min, full 30 min)."""
    profiles = default_profiles()
    assert set(profiles) == {
        GateProfileName.FAST,
        GateProfileName.ACCEPTANCE,
        GateProfileName.FULL,
    }
    assert profiles[GateProfileName.FAST].wall_budget_seconds == FAST_WALL_BUDGET_SECONDS == 600
    assert (
        profiles[GateProfileName.ACCEPTANCE].wall_budget_seconds
        == ACCEPTANCE_WALL_BUDGET_SECONDS
        == 1200
    )
    assert profiles[GateProfileName.FULL].wall_budget_seconds == FULL_WALL_BUDGET_SECONDS == 1800
    assert profiles[GateProfileName.ACCEPTANCE].rerun_failures is True
    assert profiles[GateProfileName.ACCEPTANCE].require_map_freshness is True
    assert profiles[GateProfileName.FAST].rerun_failures is False


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("G1", GateProfileName.FAST),
        ("G2", GateProfileName.FAST),
        ("G3", GateProfileName.FULL),
        ("main-gate", GateProfileName.FULL),
        ("manual", GateProfileName.FULL),
    ],
)
def test_required_profile_mapping(kind: str, expected: GateProfileName) -> None:
    """Covers: ADR-0003 D2 lifecycle-to-profile mapping (engine policy)."""
    assert required_profile(kind) is expected
    assert required_profile(GateKind(kind)) is expected


def test_unknown_kind_and_profile_refused() -> None:
    """Covers: fail-closed unknown kind/profile selection."""
    with pytest.raises(GateProfileError):
        required_profile("G9")
    with pytest.raises(GateProfileError):
        resolve_profile("G1", requested="turbo")


def test_caller_cannot_lower_required_profile() -> None:
    """Covers: ADR-0001 invariant 3 (callers may strengthen, never weaken)."""
    assert resolve_profile("G1").name is GateProfileName.FAST
    assert (
        resolve_profile("G1", requested="full").name is GateProfileName.FULL
    )
    assert (
        resolve_profile("G3", requested=GateProfileName.FULL).name is GateProfileName.FULL
    )
    with pytest.raises(GateProfileError):
        resolve_profile("G3", requested="fast")
    with pytest.raises(GateProfileError):
        resolve_profile("manual", requested="acceptance")


def test_render_commands_substitutes_placeholders() -> None:
    """Covers: project-defined commands with engine placeholder substitution."""
    command = GateCommand(
        "probe",
        "{python} -m pytest {base} {head} {repo} {result_dir}",
    )
    profile = GateProfile(
        name=GateProfileName.FAST,
        commands=(command,),
        wall_budget_seconds=10,
    )
    rendered = profile.render_commands(
        repo="/r",
        base="abc",
        head="def",
        result_dir="/out",
        python="/py",
    )
    assert rendered == [["/py", "-m", "pytest", "abc", "def", "/r", "/out"]]


def test_selector_nodes_are_shell_quoted() -> None:
    """Covers: optional selector plugin substitution ({selector})."""
    profile = GateProfile(
        name=GateProfileName.FAST,
        commands=(GateCommand("sel", "pytest {selector}"),),
        wall_budget_seconds=10,
    )
    rendered = profile.render_commands(
        repo="/r",
        base="a",
        head="b",
        result_dir="/o",
        python="/py",
        selector_nodes=("tests/test_x.py::test_a", "tests/test_y.py::test_b"),
    )
    assert rendered == [["pytest", "tests/test_x.py::test_a", "tests/test_y.py::test_b"]]


def test_parse_profiles_accepts_override() -> None:
    """Covers: ADR-0003 D2 (profiles are project data)."""
    data = {
        "version": 1,
        "profiles": {
            "fast": {
                "commands": [{"name": "mine", "command": "make check"}],
                "wall_budget_seconds": 120,
                "selector_command": "python scripts/select.py",
            }
        },
    }
    profiles = parse_profiles(data)
    fast = profiles[GateProfileName.FAST]
    assert fast.commands[0].command == "make check"
    assert fast.wall_budget_seconds == 120
    assert fast.selector_command == "python scripts/select.py"
    # Untouched profiles keep their defaults.
    assert profiles[GateProfileName.FULL] == default_profiles()[GateProfileName.FULL]


@pytest.mark.parametrize(
    "data",
    [
        {"profiles": {}},  # missing version
        {"version": 2, "profiles": {}},
        {"version": 1, "profiles": {"turbo": {}}},  # unknown profile
        {"version": 1, "profiles": {"fast": {}}},  # no commands
        {"version": 1, "profiles": {"fast": {"commands": []}}},
        {
            "version": 1,
            "profiles": {"fast": {"commands": [{"name": "x"}], "wall_budget_seconds": 10}},
        },
        {
            "version": 1,
            "profiles": {
                "fast": {"commands": [{"name": "x", "command": "y"}], "wall_budget_seconds": 0}
            },
        },
        {"version": 1, "profiles": {"fast": {"commands": [{"name": "x", "command": "y"}],
                                            "wall_budget_seconds": 10, "junk": True}}},
    ],
)
def test_parse_profiles_fails_closed(data: object) -> None:
    """Covers: malformed profile data is refused, never silently weakened."""
    with pytest.raises(GateProfileError):
        parse_profiles(data)


def test_load_profiles_absent_is_defaults_and_corrupt_fails(tmp_path: Path) -> None:
    """Covers: absent file -> defaults; corrupt override -> fail closed."""
    assert load_profiles(tmp_path) == default_profiles()
    assert load_profiles(None) == default_profiles()
    (tmp_path / PROFILES_FILENAME).write_text("{not json")
    with pytest.raises(GateProfileError):
        load_profiles(tmp_path)


def test_load_profiles_reads_valid_document(tmp_path: Path) -> None:
    """Covers: valid project document loads through load_profiles()."""
    (tmp_path / PROFILES_FILENAME).write_text(
        json.dumps(
            {
                "version": 1,
                "profiles": {
                    "full": {
                        "commands": [{"name": "gate", "command": "make gate"}],
                        "wall_budget_seconds": 900,
                    }
                },
            }
        )
    )
    profiles = load_profiles(tmp_path)
    assert profiles[GateProfileName.FULL].commands[0].command == "make gate"


def test_acceptance_profile_is_acceptance() -> None:
    """Covers: ADR-0003 D2 acceptance = fast + rerun + map freshness."""
    profile = acceptance_profile()
    assert profile.name is GateProfileName.ACCEPTANCE
    assert profile.rerun_failures is True
    assert profile.require_map_freshness is True
