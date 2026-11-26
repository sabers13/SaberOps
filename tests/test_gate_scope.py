"""Deterministic write-scope and test-retirement checks."""

from __future__ import annotations

from pathlib import Path

import pytest

from saberops.gate_scope import (
    GATE_CONFIG_SECTIONS,
    GATE_PROTECTED_PATH_MUTATED,
    PROTECTED_GATE_SCRIPT_PATHS,
    GateScopeError,
    RetirementAction,
    RetirementEntry,
    changed_gate_config_sections,
    check_gate_protection,
    check_test_retirement,
    check_write_scope,
    collect_tests,
    collect_tests_from_sources,
    definition_digest,
    gate_definition_mismatches,
    git_changed_files,
    is_protected_gate_path,
    normalize_path,
    parse_retirement_table,
)


def test_write_scope_accepts_subpaths() -> None:
    """Covers: changed files subset of Touches."""
    result = check_write_scope(
        ["src/saberops/gate_runner.py", "tests/test_gate_scope.py", "docs/gate.md"],
        ["src/saberops/", "tests/test_gate_scope.py", "docs/gate.md"],
    )
    assert result.allowed is True
    assert result.out_of_scope == ()
    assert result.must_not_touch == ()


def test_write_scope_rejects_out_of_scope() -> None:
    """Covers: out-of-scope paths fail closed."""
    result = check_write_scope(
        ["src/saberops/web.py"],
        ["src/saberops/gate_runner.py"],
    )
    assert result.allowed is False
    assert result.out_of_scope == ("src/saberops/web.py",)
    assert "outside Touches" in result.reason


def test_write_scope_rejects_must_not_touch() -> None:
    """Covers: Intersect must-not-touch = empty."""
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


def test_gate_protection_covers_the_named_gate_definitions() -> None:
    """Covers: GT-1 (gate scripts, conftest files, tool config and CI workflow)."""
    for path in PROTECTED_GATE_SCRIPT_PATHS:
        assert is_protected_gate_path(path) is True
    # The directory rule is deliberately wider than the named list: the G1
    # executor itself and any future gate helper are gate definitions too.
    assert is_protected_gate_path("scripts/lane-check.sh") is True
    assert is_protected_gate_path("scripts/gate_lib.sh") is True
    assert is_protected_gate_path("conftest.py") is True
    assert is_protected_gate_path("tests/conftest.py") is True
    assert is_protected_gate_path("tests/deep/nested/conftest.py") is True
    assert is_protected_gate_path("pyproject.toml") is True
    assert is_protected_gate_path(".github/workflows/ci.yml") is True
    # Every workflow is a gate definition, not only ci.yml.
    assert is_protected_gate_path(".github/workflows/nightly.yml") is True
    assert is_protected_gate_path(".github/workflows/release.yaml") is True
    # Config files that override pyproject.toml for pytest, ruff or mypy.
    for name in (
        "pytest.ini",
        ".pytest.ini",
        "tox.ini",
        "setup.cfg",
        "ruff.toml",
        ".ruff.toml",
        "mypy.ini",
        ".mypy.ini",
        "pkg/sub/pytest.ini",
        "pkg/sub/ruff.toml",
        "pkg/sub/pyproject.toml",
    ):
        assert is_protected_gate_path(name) is True


def test_gate_protection_leaves_product_and_test_paths_alone() -> None:
    """Covers: GT-1 (only gate definitions are protected)."""
    for path in (
        "src/saberops/gate_runner.py",
        "tests/test_gate_scope.py",
        "tests/quarantine.txt",
        "docs/gate.md",
        "scripts.txt",
        "docs/conftest.md",
    ):
        assert is_protected_gate_path(path) is False


def test_check_gate_protection_blocks_a_conftest_edit() -> None:
    """Covers: GT-1 (a protected mutation fails closed as an owner question)."""
    result = check_gate_protection(["tests/conftest.py", "src/saberops/web.py"])
    assert result.is_protected is True
    assert result.allowed is False
    assert result.tampered_paths == ("tests/conftest.py",)
    assert result.reason_code == GATE_PROTECTED_PATH_MUTATED
    assert GATE_PROTECTED_PATH_MUTATED in result.reason
    assert result.owner_question
    assert result.detected_by == ("branch-diff",)
    assert result.as_dict()["tampered_paths"] == ["tests/conftest.py"]


def test_check_gate_protection_allows_a_clean_candidate() -> None:
    """Covers: GT-1 (an authorized product change passes the check)."""
    result = check_gate_protection(["src/saberops/web.py", "docs/gate.md"])
    assert result.is_protected is False
    assert result.allowed is True
    assert result.tampered_paths == ()
    assert result.reason_code == ""


def test_check_gate_protection_declared_edit_is_still_blocked() -> None:
    """Covers: GT-1 (a declared gate edit is evidence, never authorization)."""
    result = check_gate_protection(["scripts/gate-full.sh"], ["scripts/gate-full.sh", "src/"])
    assert result.allowed is False
    assert result.tampered_paths == ("scripts/gate-full.sh",)
    assert result.declared_paths == ("scripts/gate-full.sh",)
    assert "not an authorization" in result.owner_question


def test_gate_config_sections_report_gate_relevant_changes() -> None:
    """Covers: GT-1 (pyproject gate sections are reported as evidence)."""
    base = "[tool.mypy]\nstrict = true\n\n[project]\nversion = '1'\n"
    head = "[tool.mypy]\nstrict = false\n\n[project]\nversion = '2'\n"
    assert changed_gate_config_sections(base, head) == ("tool.mypy",)
    assert changed_gate_config_sections(base, base) == ()
    assert changed_gate_config_sections("not = = toml", base) == GATE_CONFIG_SECTIONS


def test_gate_definition_mismatches_detects_added_removed_and_edited() -> None:
    """Covers: GT-1 (base comparison covers edits, deletions and additions)."""
    assert gate_definition_mismatches({"a": "1"}, {"a": "1"}) == ()
    assert gate_definition_mismatches({"a": "1"}, {"a": "2"}) == ("a",)
    assert gate_definition_mismatches({"a": "1"}, {}) == ("a",)
    assert gate_definition_mismatches({}, {"b": "1"}) == ("b",)


def test_definition_digest_is_stable_and_content_sensitive() -> None:
    """Covers: GT-1 (recorded evidence digests identify one definition version)."""
    assert definition_digest("x") == definition_digest("x")
    assert definition_digest("x") != definition_digest("y")


# ---------------------------------------------------------------------------
# Mutation-testing gaps (H46): failure paths and order-sensitive comparisons.
# ---------------------------------------------------------------------------


def test_collect_tests_reports_invalid_test_source() -> None:
    """Covers: an unparsable test source is a GateScopeError, not a crash."""
    with pytest.raises(GateScopeError, match="cannot parse test source"):
        collect_tests("def test_broken(:\n    pass\n", "tests/test_broken.py")


def test_git_changed_files_reports_a_failed_git_diff(tmp_path: Path) -> None:
    """Covers: a failing git diff becomes a GateScopeError."""
    with pytest.raises(GateScopeError, match="git diff"):
        git_changed_files(str(tmp_path), "no-such-ref", "HEAD")


def test_changed_gate_config_sections_reports_a_one_sided_section() -> None:
    """Covers: a gate section present on only one side is a change."""
    base = "[tool.mypy]\nstrict = true\n"
    head = "[tool.mypy]\nstrict = true\n\n[tool.ruff]\nline-length = 100\n"
    assert changed_gate_config_sections(base, head) == ("tool.ruff",)


def test_retirement_blocks_a_rewrite_even_when_the_head_sorts_first() -> None:
    """Covers: rewritten bodies compare for inequality, not sort order."""
    base = {"tests/test_mod.py": "def test_a():\n    assert goal_z\n"}
    head = {"tests/test_mod.py": "def test_a():\n    assert goal_a\n"}
    result = check_test_retirement(base, head)
    assert result.allowed is False
    assert result.rewritten == ("tests/test_mod.py::test_a",)


def test_normalize_path_accepts_a_legal_component_before_dotdot() -> None:
    """Covers: only the exact '..' component escapes, not any earlier value."""
    assert normalize_path("src/!notes.py") == "src/!notes.py"


def test_normalize_path_rejects_a_drive_prefix_but_allows_an_inner_colon() -> None:
    """Covers: a colon in the first component is absolute; later ones are legal."""
    with pytest.raises(GateScopeError):
        normalize_path("C:/repo/file.py")
    assert normalize_path("src/a:b.py") == "src/a:b.py"


def test_retirement_leaves_unchanged_sources_alone() -> None:
    """Covers: equal bodies read from separate sources are not a rewrite."""
    source = "def test_a():\n    assert goal\n"
    result = check_test_retirement(
        {"tests/test_mod.py": source}, {"tests/test_mod.py": source}
    )
    assert result.allowed is True
    assert result.rewritten == ()
