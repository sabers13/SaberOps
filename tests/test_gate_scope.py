"""Deterministic write-scope and test-retirement checks (ADR-0004 D3)."""

from __future__ import annotations

import pytest

from saberops.gate_scope import (
    GateScopeError,
    RetirementAction,
    RetirementEntry,
    check_test_retirement,
    check_write_scope,
    collect_tests,
    collect_tests_from_sources,
    normalize_path,
    parse_retirement_table,
)


def test_write_scope_accepts_subpaths() -> None:
    """Covers: ADR-0004 D3 (changed files subset of Touches)."""
    result = check_write_scope(
        ["src/saberops/gate_runner.py", "tests/test_gate_scope.py", "docs/gate.md"],
        ["src/saberops/", "tests/test_gate_scope.py", "docs/gate.md"],
    )
    assert result.allowed is True
    assert result.out_of_scope == ()
    assert result.must_not_touch == ()


def test_write_scope_rejects_out_of_scope() -> None:
    """Covers: ADR-0004 D3 (out-of-scope paths fail closed)."""
    result = check_write_scope(
        ["src/saberops/web.py"],
        ["src/saberops/gate_runner.py"],
    )
    assert result.allowed is False
    assert result.out_of_scope == ("src/saberops/web.py",)
    assert "outside Touches" in result.reason


def test_write_scope_rejects_must_not_touch() -> None:
    """Covers: ADR-0004 D3 (Intersect must-not-touch = empty)."""
    result = check_write_scope(
        ["pyproject.toml", "src/saberops/gate_runner.py"],
        ["src/saberops/", "pyproject.toml"],
        ["pyproject.toml", ".orch/"],
    )
    assert result.allowed is False
    assert result.must_not_touch == ("pyproject.toml",)
    assert "must-not-touch" in result.reason


def test_write_scope_empty_touches_rejects_everything() -> None:
    """Covers: a lane with no declared Touches may not change anything."""
    result = check_write_scope(["src/saberops/gate_runner.py"], [])
    assert result.allowed is False


def test_normalize_path_rejects_escape_and_absolute() -> None:
    """Covers: scope paths are repo-relative; escapes are refused."""
    assert normalize_path("./docs//gate.md") == "docs/gate.md"
    with pytest.raises(GateScopeError):
        normalize_path("../secrets")
    with pytest.raises(GateScopeError):
        normalize_path("/etc/passwd")
    with pytest.raises(GateScopeError):
        normalize_path("   ")


def test_collect_tests_finds_functions_and_methods() -> None:
    """Covers: retirement inventory is AST-based, not filename-based."""
    source = (
        "def test_a():\n    assert True\n"
        "def helper():\n    pass\n"
        "class TestC:\n"
        "    def test_b(self):\n        assert True\n"
        "    def other(self):\n        pass\n"
    )
    found = collect_tests(source, "tests/test_mod.py")
    assert set(found) == {
        "tests/test_mod.py::test_a",
        "tests/test_mod.py::TestC::test_b",
    }


def test_retirement_allows_added_tests_and_authorized_removal() -> None:
    """Covers: unauthorized removal blocks; the retirement table authorizes it."""
    base = {"tests/test_mod.py": "def test_a():\n    assert True\n"}
    head = {"tests/test_mod.py": "def test_c():\n    assert True\n"}
    blocked = check_test_retirement(base, head, ())
    assert blocked.allowed is False
    assert blocked.unauthorized_removed == ("tests/test_mod.py::test_a",)
    allowed = check_test_retirement(
        base,
        head,
        (RetirementEntry("tests/test_mod.py::test_a", RetirementAction.REMOVED),),
    )
    assert allowed.allowed is True
    assert allowed.removed == ("tests/test_mod.py::test_a",)
    assert allowed.unauthorized_removed == ()


def test_retirement_blocks_unauthorized_rewrite() -> None:
    """Covers: tests are the contract; a weakened body is refused."""
    base = {"tests/test_mod.py": "def test_a():\n    assert run() == 1\n"}
    head = {"tests/test_mod.py": "def test_a():\n    assert run() >= 0\n"}
    blocked = check_test_retirement(base, head, ())
    assert blocked.allowed is False
    assert blocked.unauthorized_rewritten == ("tests/test_mod.py::test_a",)
    assert "weakening" in blocked.reason
    allowed = check_test_retirement(
        base,
        head,
        (RetirementEntry("tests/test_mod.py::test_a", RetirementAction.REWRITTEN),),
    )
    assert allowed.allowed is True


def test_retirement_wrong_action_is_unauthorized() -> None:
    """Covers: authorizing 'rewritten' does not authorize a removal."""
    base = {"tests/test_mod.py": "def test_a():\n    assert True\n"}
    head = {"tests/test_mod.py": "def test_b():\n    assert True\n"}
    result = check_test_retirement(
        base,
        head,
        (RetirementEntry("tests/test_mod.py::test_a", RetirementAction.REWRITTEN),),
    )
    assert result.allowed is False
    assert result.unauthorized_removed == ("tests/test_mod.py::test_a",)


def test_collect_tests_from_sources_normalizes_paths() -> None:
    """Covers: source mapping paths are normalized before node ids are built."""
    collected = collect_tests_from_sources({"./tests/test_m.py": "def test_a():\n    pass\n"})
    assert list(collected) == ["tests/test_m.py::test_a"]


def test_parse_retirement_table() -> None:
    """Covers: prompt "Tests retired / rewritten" table parsing."""
    entries = parse_retirement_table(
        [
            {"node_id": "tests/test_a.py::test_x", "action": "removed", "reason": "obsolete"},
            {"node_id": "tests/test_b.py::test_y", "action": "rewritten"},
        ]
    )
    assert entries[0].action is RetirementAction.REMOVED
    assert entries[1].action is RetirementAction.REWRITTEN
    with pytest.raises(GateScopeError):
        parse_retirement_table([{"action": "removed"}])
    with pytest.raises(GateScopeError):
        parse_retirement_table([{"node_id": "x", "action": "deleted"}])
