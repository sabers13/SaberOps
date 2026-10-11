"""Selection rules for scripts/select_tests.py, the per-task affected-test gate.

Every test builds a throwaway git repository with a tiny fake package, so the real
saberops package is never imported and the fake tests are never executed: only the
selector's decisions are checked. No provider, model or network access.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SELECTOR_SCRIPT = REPO_ROOT / "scripts" / "select_tests.py"
BUILD_MAP_SCRIPT = REPO_ROOT / "scripts" / "build_test_map.py"
GATE_UI_FAST = REPO_ROOT / "scripts" / "gate-ui-fast.sh"

CORE = "src/fakepkg/core.py"
MID = "src/fakepkg/mid.py"
REL = "src/fakepkg/rel.py"
LEAF = "src/fakepkg/leaf.py"
ORPHAN = "src/fakepkg/orphan.py"
PAGE = "src/fakepkg/templates/page.html"

BASE_TREE: dict[str, str] = {
    "src/fakepkg/__init__.py": '"""Fake package for the selector tests."""\n',
    CORE: "VALUE = 1\n\n\ndef core_value() -> int:\n    return VALUE\n",
    MID: (
        "from fakepkg.core import core_value\n"
        "\n"
        "\n"
        "def mid_value() -> int:\n"
        "    return core_value()\n"
    ),
    REL: "from . import core\n\n\ndef rel_value() -> int:\n    return core.VALUE\n",
    "src/fakepkg/typed.py": (
        "from typing import TYPE_CHECKING\n"
        "\n"
        "if TYPE_CHECKING:\n"
        "    from fakepkg.core import core_value\n"
        "\n"
        "\n"
        "def typed_value() -> int:\n"
        "    return 2\n"
    ),
    LEAF: "LEAF = 1\n",
    ORPHAN: "ORPHAN = 1\n",
    PAGE: "<html><body>page</body></html>\n",
    "tests/conftest.py": '"""Fake conftest."""\n',
    "tests/test_direct.py": (
        "from fakepkg.core import core_value\n"
        "\n"
        "\n"
        "def test_direct() -> None:\n"
        "    assert core_value() == 1\n"
    ),
    "tests/test_transitive.py": (
        "from fakepkg.mid import mid_value\n"
        "\n"
        "\n"
        "def test_transitive() -> None:\n"
        "    assert mid_value() == 1\n"
    ),
    "tests/test_relative.py": (
        "from fakepkg.rel import rel_value\n"
        "\n"
        "\n"
        "def test_relative() -> None:\n"
        "    assert rel_value() == 1\n"
    ),
    "tests/test_string_target.py": (
        'PATCH_TARGET = "fakepkg.core.core_value"\n'
        "\n"
        "\n"
        "def test_patch_target() -> None:\n"
        '    assert PATCH_TARGET.startswith("fakepkg.core")\n'
    ),
    "tests/test_typed.py": (
        "from fakepkg.typed import typed_value\n"
        "\n"
        "\n"
        "def test_typed() -> None:\n"
        "    assert typed_value() == 2\n"
    ),
    "tests/test_leaf.py": (
        "from fakepkg.leaf import LEAF\n"
        "\n"
        "\n"
        "def test_leaf() -> None:\n"
        "    assert LEAF == 1\n"
    ),
    "tests/test_template_asset.py": (
        'ASSET = "src/fakepkg/templates/page.html"\n'
        "\n"
        "\n"
        "def test_template_asset() -> None:\n"
        '    assert ASSET.endswith("page.html")\n'
    ),
    "tests/test_unrelated.py": "def test_unrelated() -> None:\n    assert True\n",
    "docs/guide.md": "# Guide\n",
}


@pytest.fixture(scope="session")
def selector() -> ModuleType:
    """scripts/select_tests.py, loaded from this checkout without installing it."""
    spec = importlib.util.spec_from_file_location("saberops_select_tests", SELECTOR_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repository holding BASE_TREE, committed on main."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    # One extra commit so HEAD~1 exists and the default base has something to point at.
    git(
        repo,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@localhost",
        "commit",
        "--allow-empty",
        "-m",
        "root",
    )
    for path, text in BASE_TREE.items():
        write(repo, path, text)
    commit(repo, "fake package")
    return repo


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def write(repo: Path, path: str, text: str) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def commit(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(
        repo,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@localhost",
        "commit",
        "-m",
        message,
    )
    return git(repo, "rev-parse", "HEAD")


def selected(repo: Path, selector: ModuleType, base: str = "HEAD") -> set[str]:
    targets: set[str] = set()
    for item in selector.select(repo, base).tests:
        targets.add(item.target)
    return targets


def details(repo: Path, selector: ModuleType, base: str = "HEAD") -> dict[str, str]:
    reasons: dict[str, str] = {}
    for item in selector.select(repo, base).tests:
        reasons[item.target] = item.detail
    return reasons


def chains(repo: Path, selector: ModuleType, base: str = "HEAD") -> dict[str, tuple[str, ...]]:
    paths: dict[str, tuple[str, ...]] = {}
    for item in selector.select(repo, base).tests:
        paths[item.target] = tuple(str(link) for link in item.chain)
    return paths


def full_reason(repo: Path, selector: ModuleType, base: str = "HEAD") -> str:
    selection = selector.select(repo, base)
    assert selection.full is True
    return str(selection.full_reason)


def run_selector(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SELECTOR_SCRIPT), "--repo", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )


# ---------------------------------------------------------------------------
# rules
# ---------------------------------------------------------------------------


def test_direct_import(repo: Path, selector: ModuleType) -> None:
    write(repo, CORE, BASE_TREE[CORE].replace("VALUE = 1", "VALUE = 5"))
    targets = selected(repo, selector)
    assert targets == {
        "tests/test_direct.py",
        "tests/test_relative.py",
        "tests/test_string_target.py",
        "tests/test_transitive.py",
    }
    assert details(repo, selector)["tests/test_direct.py"] == "direct import"


def test_transitive_import(repo: Path, selector: ModuleType) -> None:
    """A test imports mid, mid imports core: a core change is two hops away."""
    write(repo, CORE, BASE_TREE[CORE].replace("VALUE = 1", "VALUE = 5"))
    targets = selected(repo, selector)
    assert "tests/test_transitive.py" in targets
    assert details(repo, selector)["tests/test_transitive.py"] == "transitive import"
    assert len(chains(repo, selector)["tests/test_transitive.py"]) == 3


def test_relative_import(repo: Path, selector: ModuleType) -> None:
    write(repo, REL, BASE_TREE[REL].replace("core.VALUE", "core.VALUE + 0"))
    assert selected(repo, selector) == {"tests/test_relative.py"}


def test_string_literal_patch_target(repo: Path, selector: ModuleType) -> None:
    write(repo, CORE, BASE_TREE[CORE].replace("VALUE = 1", "VALUE = 7"))
    assert "tests/test_string_target.py" in selected(repo, selector)
    detail = details(repo, selector)["tests/test_string_target.py"]
    assert detail.startswith("patch target")
    assert len(chains(repo, selector)["tests/test_string_target.py"]) == 2


def test_type_checking_only_import_is_not_a_runtime_dependency(
    repo: Path, selector: ModuleType
) -> None:
    write(repo, CORE, BASE_TREE[CORE].replace("VALUE = 1", "VALUE = 9"))
    assert "tests/test_typed.py" not in selected(repo, selector)


def test_change_to_a_module_no_test_imports_selects_nothing(
    repo: Path, selector: ModuleType
) -> None:
    write(repo, ORPHAN, "ORPHAN = 3\n")
    selection = selector.select(repo, "HEAD")
    assert selection.full is False
    assert selection.tests == []


def test_unrelated_test_is_never_selected(repo: Path, selector: ModuleType) -> None:
    write(repo, ORPHAN, "ORPHAN = 4\n")
    write(repo, "docs/guide.md", "# Guide\n\nmore\n")
    assert "tests/test_unrelated.py" not in selected(repo, selector)


def test_conftest_change_requires_the_full_suite(repo: Path, selector: ModuleType) -> None:
    write(repo, "tests/conftest.py", '"""Fake conftest."""\n\nimport fakepkg.core\n')
    assert full_reason(repo, selector) == "tests/conftest.py"


def test_template_change_selects_the_ui_node_ids_and_naming_tests(
    repo: Path, selector: ModuleType
) -> None:
    write(repo, PAGE, BASE_TREE[PAGE].replace("page", "page v2"))
    selection = selector.select(repo, "HEAD")
    assert selection.full is False
    assert {item.target for item in selection.tests} == set(selector.UI_NODE_IDS) | {
        "tests/test_template_asset.py"
    }


def test_docs_only_change_selects_no_tests(repo: Path, selector: ModuleType) -> None:
    write(repo, "docs/guide.md", "# Guide\n\nUpdated.\n")
    selection = selector.select(repo, "HEAD")
    assert selection.full is False
    assert selection.tests == []
    assert "docs/guide.md" in selection.ignored


def test_markdown_and_orch_paths_select_no_tests(repo: Path, selector: ModuleType) -> None:
    write(repo, "README.md", "# Readme\n")
    write(repo, ".orch/T-002/prompt.md", "# prompt\n")
    selection = selector.select(repo, "HEAD")
    assert selection.full is False
    assert selection.tests == []
    assert set(selection.ignored) == {"README.md", ".orch/T-002/prompt.md"}


def test_unresolvable_python_file_requires_the_full_suite(
    repo: Path, selector: ModuleType
) -> None:
    write(repo, "tools/extra.py", "VALUE = 1\n")
    assert full_reason(repo, selector) == "tools/extra.py"


def test_unknown_file_type_requires_the_full_suite(repo: Path, selector: ModuleType) -> None:
    write(repo, "src/fakepkg/defaults/data.json", '{"a": 1}\n')
    assert full_reason(repo, selector) == "src/fakepkg/defaults/data.json"


def test_setup_file_change_requires_the_full_suite(repo: Path, selector: ModuleType) -> None:
    write(repo, "pyproject.toml", "[project]\nname = 'fakepkg'\n")
    assert full_reason(repo, selector) == "pyproject.toml"


def test_top_level_package_init_requires_the_full_suite(
    repo: Path, selector: ModuleType
) -> None:
    write(repo, "src/fakepkg/__init__.py", '"""Changed."""\n')
    assert full_reason(repo, selector) == "src/fakepkg/__init__.py"


def test_gate_script_change_requires_the_full_suite(repo: Path, selector: ModuleType) -> None:
    write(repo, "scripts/gate-affected.sh", "#!/usr/bin/env bash\n")
    assert full_reason(repo, selector) == "scripts/gate-affected.sh"


def test_new_test_file_is_selected(repo: Path, selector: ModuleType) -> None:
    write(
        repo,
        "tests/test_added.py",
        "def test_added() -> None:\n    assert True\n",
    )
    targets = selected(repo, selector)
    assert "tests/test_added.py" in targets
    assert details(repo, selector)["tests/test_added.py"] == "changed test file"


def test_deleted_test_file_is_selected(repo: Path, selector: ModuleType) -> None:
    (repo / "tests/test_unrelated.py").unlink()
    assert "tests/test_unrelated.py" in selected(repo, selector)


def test_deleted_module_still_selects_its_tests(repo: Path, selector: ModuleType) -> None:
    (repo / MID).unlink()
    assert selected(repo, selector) == {"tests/test_transitive.py"}


def test_staged_change_is_selected(repo: Path, selector: ModuleType) -> None:
    write(repo, REL, BASE_TREE[REL].replace("core.VALUE", "core.VALUE + 1"))
    git(repo, "add", "src/fakepkg/rel.py")
    assert selected(repo, selector) == {"tests/test_relative.py"}


def test_committed_change_is_selected(repo: Path, selector: ModuleType) -> None:
    base = git(repo, "rev-parse", "HEAD")
    write(repo, MID, BASE_TREE[MID].replace("core_value()", "core_value() + 1"))
    commit(repo, "change mid")
    assert selected(repo, selector, base) == {"tests/test_transitive.py"}


def test_untracked_module_change_is_selected(repo: Path, selector: ModuleType) -> None:
    write(
        repo,
        "src/fakepkg/extra.py",
        "VALUE = 1\n",
    )
    write(
        repo,
        "tests/test_extra.py",
        "from fakepkg.extra import VALUE\n"
        "\n"
        "\n"
        "def test_extra() -> None:\n"
        "    assert VALUE == 1\n",
    )
    targets = selected(repo, selector)
    assert targets == {"tests/test_extra.py"}


def test_default_base_is_the_parent_commit(repo: Path, selector: ModuleType) -> None:
    """Without an upstream the selector diffs against HEAD~1."""
    assert selector.default_base(repo) == git(repo, "rev-parse", "HEAD~1")


# ---------------------------------------------------------------------------
# CLI contract
# ---------------------------------------------------------------------------


def test_cli_prints_only_the_selection_on_stdout(repo: Path) -> None:
    write(repo, MID, BASE_TREE[MID].replace("core_value()", "core_value() + 1"))
    completed = run_selector(repo, "--base", "HEAD", "--explain")
    assert completed.returncode == 0
    assert completed.stdout.splitlines() == ["tests/test_transitive.py"]
    assert "base: HEAD" in completed.stderr
    assert "tests/test_transitive.py" in completed.stderr
    assert "chain: tests.test_transitive -> fakepkg.mid" in completed.stderr


def test_cli_prints_full_for_a_full_suite_change(repo: Path) -> None:
    write(repo, "pyproject.toml", "[project]\n")
    completed = run_selector(repo, "--base", "HEAD")
    assert completed.returncode == 0
    assert completed.stdout.splitlines() == ["FULL"]
    assert "FULL suite required: pyproject.toml" in completed.stderr


def test_cli_prints_nothing_when_no_test_is_affected(repo: Path) -> None:
    write(repo, "docs/guide.md", "# Guide\n\nagain\n")
    completed = run_selector(repo, "--base", "HEAD", "--explain")
    assert completed.returncode == 0
    assert completed.stdout == ""
    assert "no tests for (1):" in completed.stderr
    assert "selected tests (0):" in completed.stderr


def test_cli_json_reports_the_same_selection(repo: Path) -> None:
    write(repo, REL, BASE_TREE[REL].replace("core.VALUE", "core.VALUE + 1"))
    completed = run_selector(repo, "--base", "HEAD", "--json")
    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["full"] is False
    assert payload["base"] == "HEAD"
    assert [item["target"] for item in payload["tests"]] == ["tests/test_relative.py"]
    assert payload["tests"][0]["via"] == "src/fakepkg/rel.py"


def test_cli_reports_an_unknown_base_ref(repo: Path) -> None:
    completed = run_selector(repo, "--base", "no-such-ref")
    assert completed.returncode == 2
    assert "no-such-ref" in completed.stderr


def test_ui_node_ids_match_gate_ui_fast(selector: ModuleType) -> None:
    """The presentation rule must run exactly what scripts/gate-ui-fast.sh runs."""
    text = GATE_UI_FAST.read_text(encoding="utf-8")
    declared = set(re.findall(r"tests/[\w./]+::\w+", text))
    assert declared
    assert set(selector.UI_NODE_IDS) == declared


# ---------------------------------------------------------------------------
# coverage strategy (synthetic map JSON; no coverage run inside unit tests)
# ---------------------------------------------------------------------------

DIRECT_NODE = "tests/test_direct.py::test_direct"
TRANSITIVE_NODE = "tests/test_transitive.py::test_transitive"


def write_map(
    repo: Path,
    files: dict[str, dict[str, list[str]]],
    blind: tuple[str, ...] = (),
    commit: str | None = None,
) -> Path:
    """Write a fresh (HEAD-built) sharded coverage map into the fake repo."""
    if commit is None:
        commit = git(repo, "rev-parse", "HEAD")
    common = git(repo, "rev-parse", "--git-common-dir")
    map_dir = repo / common / "saberops-test-map"
    nodes = sorted({node for lines in files.values() for group in lines.values() for node in group})
    index = {node: position for position, node in enumerate(nodes)}
    by_test: dict[str, list[int]] = {}
    for position, node in enumerate(nodes):
        by_test.setdefault(node.split("::")[0], []).append(position)
    shards = map_dir / "files"
    for rel, lines in files.items():
        shard = shards / f"{rel}.json"
        shard.parent.mkdir(parents=True, exist_ok=True)
        shard.write_text(
            json.dumps(
                {line: sorted(index[node] for node in group) for line, group in lines.items()}
            ),
            encoding="utf-8",
        )
    payload = {
        "version": 2,
        "commit": commit,
        "built_at": "2026-01-01T00:00:00+00:00",
        "test_count": 2,
        "context_count": 2,
        "unmatched_nodes": 0,
        "coverage_version": "7.0.0",
        "build_seconds": 1.0,
        "pytest_exit": 0,
        "subprocess_capture": False,
        "nodes": nodes,
        "by_test": by_test,
        "blind": list(blind),
    }
    (map_dir / "meta.json").write_text(json.dumps(payload), encoding="utf-8")
    return map_dir


def cov_select(
    repo: Path, selector: ModuleType, base: str = "HEAD", strategy: str = "coverage"
) -> dict[str, str]:
    reasons: dict[str, str] = {}
    for item in selector.select(repo, base, strategy=strategy).tests:
        reasons[item.target] = item.detail
    return reasons


def test_coverage_line_hit_selects_only_covering_tests(
    repo: Path, selector: ModuleType
) -> None:
    """Executed-code reachability, not import reachability: only the covering node."""
    write_map(repo, {CORE: {"1": [], "4": [], "5": [DIRECT_NODE]}})
    write(repo, CORE, BASE_TREE[CORE].replace("return VALUE", "return VALUE + 1"))
    reasons = cov_select(repo, selector)
    assert reasons == {DIRECT_NODE: "line 5 executed by this test"}


def test_coverage_is_the_default_when_a_fresh_map_exists(
    repo: Path, selector: ModuleType
) -> None:
    write_map(repo, {CORE: {"1": [], "4": [], "5": [DIRECT_NODE]}})
    write(repo, CORE, BASE_TREE[CORE].replace("return VALUE", "return VALUE + 1"))
    selection = selector.select(repo, "HEAD")
    assert selection.strategy == "coverage"
    assert [item.target for item in selection.tests] == [DIRECT_NODE]


def test_coverage_neighbour_in_function_for_added_lines(
    repo: Path, selector: ModuleType
) -> None:
    """An added line selects the tests of the nearest covered line, same function."""
    write_map(repo, {MID: {"1": [], "4": [TRANSITIVE_NODE]}})
    write(
        repo,
        MID,
        BASE_TREE[MID].replace(
            "    return core_value()",
            "    result = core_value()\n    return result",
        ),
    )
    reasons = cov_select(repo, selector)
    assert list(reasons) == [TRANSITIVE_NODE]
    assert "nearest covered line in mid_value()" in reasons[TRANSITIVE_NODE]


def test_coverage_new_uncovered_function_selects_nothing(
    repo: Path, selector: ModuleType
) -> None:
    """Added code with no covered neighbour in its function affects no test."""
    write_map(repo, {CORE: {"1": [], "4": [], "5": [DIRECT_NODE]}})
    write(repo, CORE, BASE_TREE[CORE] + "\n\ndef helper() -> int:\n    return 0\n")
    assert cov_select(repo, selector) == {}


def test_coverage_comment_only_change_selects_nothing(
    repo: Path, selector: ModuleType
) -> None:
    write_map(repo, {CORE: {"1": [], "4": [], "5": [DIRECT_NODE]}})
    write(repo, CORE, BASE_TREE[CORE] + "# a comment\n")
    assert cov_select(repo, selector) == {}


def test_coverage_import_time_only_line_falls_back_to_static(
    repo: Path, selector: ModuleType
) -> None:
    """Executable but unattributed (import-time) code selects direct importers."""
    write_map(repo, {CORE: {"1": []}})
    write(repo, CORE, BASE_TREE[CORE].replace("VALUE = 1", "VALUE = 2"))
    reasons = cov_select(repo, selector)
    # Depth 1: the importing test and the string patch-target test, nothing else.
    assert set(reasons) == {"tests/test_direct.py", "tests/test_string_target.py"}
    assert "no covering test" in reasons["tests/test_direct.py"]


def test_coverage_new_file_falls_back_to_static_direct(
    repo: Path, selector: ModuleType
) -> None:
    write_map(repo, {CORE: {"5": [DIRECT_NODE]}})
    write(repo, "tests/test_extra.py", "from fakepkg.extra import VALUE\n")
    commit(repo, "test for the new module")
    write(repo, "src/fakepkg/extra.py", "from fakepkg.core import VALUE\n")
    reasons = cov_select(repo, selector)
    assert list(reasons) == ["tests/test_extra.py"]
    assert "new or deleted file" in reasons["tests/test_extra.py"]


def test_coverage_deleted_module_falls_back_to_static_direct(
    repo: Path, selector: ModuleType
) -> None:
    write_map(repo, {CORE: {"5": [DIRECT_NODE]}, MID: {"5": [TRANSITIVE_NODE]}})
    (repo / MID).unlink()
    reasons = cov_select(repo, selector)
    assert list(reasons) == ["tests/test_transitive.py"]
    assert "new or deleted file" in reasons["tests/test_transitive.py"]


def test_coverage_stale_map_falls_back_to_static_direct(
    repo: Path, selector: ModuleType
) -> None:
    write_map(repo, {CORE: {"5": [DIRECT_NODE]}}, commit="f" * 40)
    write(repo, CORE, BASE_TREE[CORE].replace("return VALUE", "return VALUE + 1"))
    selection = selector.select(repo, "HEAD", strategy="coverage")
    assert selection.strategy == "static"
    assert selection.map_stale is True
    reasons = {item.target: item.detail for item in selection.tests}
    # Stale map: direct importers only (depth 1), not the transitive closure.
    assert set(reasons) == {"tests/test_direct.py", "tests/test_string_target.py"}
    assert "stale map" in reasons["tests/test_direct.py"]


def test_coverage_blind_spot_falls_back_to_static_direct(
    repo: Path, selector: ModuleType
) -> None:
    write(
        repo,
        "tests/test_orphan.py",
        "from fakepkg.orphan import ORPHAN\n"
        "\n"
        "\n"
        "def test_orphan() -> None:\n"
        "    assert ORPHAN == 1\n",
    )
    commit(repo, "orphan test")
    write_map(repo, {CORE: {"5": [DIRECT_NODE]}}, blind=(ORPHAN,))
    write(repo, ORPHAN, "ORPHAN = 2\n")
    reasons = cov_select(repo, selector)
    assert list(reasons) == ["tests/test_orphan.py"]
    assert "blind spot" in reasons["tests/test_orphan.py"]


def test_coverage_changed_test_file_selects_its_node_ids(
    repo: Path, selector: ModuleType
) -> None:
    write_map(repo, {CORE: {"5": [DIRECT_NODE]}})
    write(repo, "tests/test_direct.py", BASE_TREE["tests/test_direct.py"] + "# note\n")
    reasons = cov_select(repo, selector)
    assert reasons == {DIRECT_NODE: "changed test file"}


def test_coverage_version_mismatch_means_no_map(
    repo: Path, selector: ModuleType
) -> None:
    map_dir = write_map(repo, {CORE: {"5": [DIRECT_NODE]}})
    meta_path = map_dir / "meta.json"
    payload = json.loads(meta_path.read_text(encoding="utf-8"))
    payload["version"] = 999
    meta_path.write_text(json.dumps(payload), encoding="utf-8")
    write(repo, CORE, BASE_TREE[CORE].replace("return VALUE", "return VALUE + 1"))
    selection = selector.select(repo, "HEAD", strategy="coverage")
    assert selection.strategy == "static"
    assert selection.map_missing is True
    # Legacy static without a map is transitive: direct, mid, rel and string users.
    assert {item.target for item in selection.tests} == {
        "tests/test_direct.py",
        "tests/test_transitive.py",
        "tests/test_relative.py",
        "tests/test_string_target.py",
    }


def test_cli_coverage_strategy_reports_node_ids(repo: Path) -> None:
    write_map(repo, {CORE: {"5": [DIRECT_NODE]}})
    write(repo, CORE, BASE_TREE[CORE].replace("return VALUE", "return VALUE + 1"))
    completed = run_selector(repo, "--base", "HEAD", "--strategy", "coverage", "--explain")
    assert completed.returncode == 0
    assert completed.stdout.splitlines() == [DIRECT_NODE]
    assert "strategy: coverage" in completed.stderr
    assert "line 5 executed by this test" in completed.stderr


def test_cli_static_strategy_keeps_transitive_selection(repo: Path) -> None:
    write_map(repo, {CORE: {"5": [DIRECT_NODE]}})
    write(repo, CORE, BASE_TREE[CORE].replace("return VALUE", "return VALUE + 1"))
    completed = run_selector(repo, "--base", "HEAD", "--strategy", "static")
    assert completed.returncode == 0
    assert "tests/test_direct.py" in completed.stdout.splitlines()
    assert "tests/test_transitive.py" in completed.stdout.splitlines()


def test_builder_maps_node_ids_to_setup_plugin_labels() -> None:
    """The setup-context plugin and the map share one label contract.

    build_test_map writes pytest_runtest_setup labels as dotted
    module.qualname (no parameters); context_labels_for must resolve node IDs
    (with class scopes and parameters) back to exactly those labels.
    """
    spec = importlib.util.spec_from_file_location("saberops_build_test_map", BUILD_MAP_SCRIPT)
    assert spec is not None and spec.loader is not None
    builder = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = builder
    spec.loader.exec_module(builder)
    observed = {"test_m.TestC.test_p", "test_m.test_f", "pkg.test_m.test_f"}
    assert builder.context_labels_for("tests/test_m.py::TestC::test_p[1]", observed) == [
        "test_m.TestC.test_p"
    ]
    assert builder.context_labels_for("tests/test_m.py::test_f", observed) == [
        "pkg.test_m.test_f",
        "test_m.test_f",
    ]
    assert "pytest_runtest_setup" in builder.SETUP_CONTEXT_PLUGIN
    assert "switch_context" in builder.SETUP_CONTEXT_PLUGIN


# ---------------------------------------------------------------------------
# G-001: ghost resolution, symbol-level selection, staleness signal
# ---------------------------------------------------------------------------


def test_partition_targets_drops_only_ghosts(
    repo: Path, selector: ModuleType
) -> None:
    collected = {"tests/test_direct.py::test_direct"}
    targets = [
        "tests/test_direct.py::test_direct",
        "tests/test_direct.py::test_gone",
        "tests/test_gone.py::test_x",
        "tests/test_direct.py",
    ]
    existing, ghosts = selector.partition_targets(repo, targets, collected, True)
    assert existing == ["tests/test_direct.py::test_direct", "tests/test_direct.py"]
    assert ghosts == ["tests/test_direct.py::test_gone", "tests/test_gone.py::test_x"]


def test_partition_targets_keeps_unverifiable_when_collection_failed(
    repo: Path, selector: ModuleType
) -> None:
    """A broken collect must not empty a non-empty selection: only files that
    are plainly gone are dropped, everything else is kept conservatively."""
    targets = ["tests/test_direct.py::test_direct", "tests/test_gone.py::test_x"]
    existing, ghosts = selector.partition_targets(repo, targets, set(), False)
    assert existing == ["tests/test_direct.py::test_direct"]
    assert ghosts == ["tests/test_gone.py::test_x"]


def test_resolve_drops_ghost_node_ids(
    repo: Path, selector: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A map shard naming a deleted test selects it, but --resolve drops it."""
    ghost = "tests/test_direct.py::test_gone"
    write_map(repo, {CORE: {"5": [DIRECT_NODE, ghost]}})
    write(repo, CORE, BASE_TREE[CORE].replace("return VALUE", "return VALUE + 1"))
    plain = {item.target for item in selector.select(repo, "HEAD", strategy="coverage").tests}
    assert plain == {DIRECT_NODE, ghost}
    monkeypatch.setenv(
        "PYTHONPATH",
        str(repo / "src") + os.pathsep + os.environ.get("PYTHONPATH", ""),
    )
    resolved = selector.select(repo, "HEAD", strategy="coverage", resolve=True)
    assert [item.target for item in resolved.tests] == [DIRECT_NODE]
    assert resolved.ghosts == [ghost]


def test_resolve_keeps_file_targets_and_reports_no_ghosts(
    repo: Path, selector: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_map(repo, {CORE: {"5": [DIRECT_NODE]}})
    write(repo, CORE, BASE_TREE[CORE].replace("return VALUE", "return VALUE + 1"))
    monkeypatch.setenv(
        "PYTHONPATH",
        str(repo / "src") + os.pathsep + os.environ.get("PYTHONPATH", ""),
    )
    resolved = selector.select(repo, "HEAD", strategy="coverage", resolve=True)
    assert [item.target for item in resolved.tests] == [DIRECT_NODE]
    assert resolved.ghosts == []


def test_symbol_module_constant_selects_covering_usages(
    repo: Path, selector: ModuleType
) -> None:
    """An import-time-only constant change selects tests covering its usages."""
    write_map(repo, {CORE: {"1": [], "4": [], "5": [DIRECT_NODE]}})
    write(repo, CORE, BASE_TREE[CORE].replace("VALUE = 1", "VALUE = 2"))
    reasons = cov_select(repo, selector)
    assert list(reasons) == [DIRECT_NODE]
    assert "symbol VALUE" in reasons[DIRECT_NODE]
    assert "line 5 executed by this test" in reasons[DIRECT_NODE]


def test_symbol_import_line_change_falls_back_with_reason(
    repo: Path, selector: ModuleType
) -> None:
    """An import line has no top-level symbol: static fallback, saying why."""
    write_map(repo, {MID: {"1": []}})
    write(
        repo,
        MID,
        BASE_TREE[MID].replace(
            "from fakepkg.core import core_value",
            "from fakepkg.core import core_value as cv",
        ),
    )
    reasons = cov_select(repo, selector)
    assert set(reasons) == {"tests/test_transitive.py"}
    assert "no covering test" in reasons["tests/test_transitive.py"]
    assert "no top-level symbol at line 1" in reasons["tests/test_transitive.py"]


def test_symbol_deleted_function_resolves_usages_in_base(
    repo: Path, selector: ModuleType
) -> None:
    """A deleted function selects the tests that referenced it at BASE."""
    write_map(repo, {MID: {"5": [TRANSITIVE_NODE]}, CORE: {"5": [DIRECT_NODE]}})
    write(repo, MID, "from fakepkg.core import core_value\n")
    reasons = cov_select(repo, selector)
    assert TRANSITIVE_NODE in reasons
    assert "deleted symbol mid_value" in reasons[TRANSITIVE_NODE]


def test_symbol_import_counts_as_reference(
    repo: Path, selector: ModuleType
) -> None:
    """`from fakepkg.leaf import LEAF` references LEAF even without a call."""
    write_map(repo, {LEAF: {"1": []}})
    write(repo, LEAF, "LEAF = 2\n")
    reasons = cov_select(repo, selector)
    assert set(reasons) == {"tests/test_leaf.py"}
    assert "symbol LEAF: referenced by this test file" in reasons["tests/test_leaf.py"]


def test_symbol_no_covering_usage_falls_back_with_reason(
    repo: Path, selector: ModuleType
) -> None:
    """A constant nothing names has no covering usage: static fallback."""
    util = "src/fakepkg/util.py"
    write(repo, util, "K = 1\n")
    write(
        repo,
        "tests/test_util.py",
        "from fakepkg import util\n\n\ndef test_util() -> None:\n    assert util is not None\n",
    )
    commit(repo, "util module and importer")
    write_map(repo, {util: {"1": []}})
    write(repo, util, "K = 2\n")
    reasons = cov_select(repo, selector)
    assert set(reasons) == {"tests/test_util.py"}
    assert "symbol K: no covering usage found" in reasons["tests/test_util.py"]


def test_symbol_function_call_needs_import_or_module_qualifier(
    repo: Path, selector: ModuleType
) -> None:
    """`svc.run()` attributes to svc.run; a bare `run()` that is never
    imported from svc (another module's own run) does not."""
    svc = "src/fakepkg/svc.py"
    write(repo, svc, "def run() -> int:\n    return 1\n")
    write(
        repo,
        "tests/test_svc.py",
        "import fakepkg.svc as svc\n\n\ndef test_uses_svc() -> None:\n    assert svc.run() == 1\n",
    )
    write(
        repo,
        "tests/test_other_run.py",
        "def run() -> int:\n    return 0\n\n\ndef test_other() -> None:\n    assert run() == 0\n",
    )
    commit(repo, "svc module and callers")
    svc_node = "tests/test_svc.py::test_uses_svc"
    write_map(repo, {svc: {"1": [], "2": [svc_node]}})
    write(repo, svc, "def run() -> str:\n    return 1\n")
    reasons = cov_select(repo, selector)
    assert list(reasons) == [svc_node]
    assert "symbol run" in reasons[svc_node]


def test_symbol_field_default_observed_by_construction(
    repo: Path, selector: ModuleType
) -> None:
    """`Config()` without the field observes its default; `Config(flag=...)`
    does not. Only the observing test file is selected."""
    mod = "src/fakepkg/holder.py"
    write(repo, mod, "class Config:\n    flag: bool = True\n")
    write(
        repo,
        "tests/test_holder_default.py",
        "from fakepkg.holder import Config\n"
        "\n"
        "\n"
        "def test_default() -> None:\n"
        "    cfg = Config()\n"
        "    assert isinstance(cfg, Config)\n",
    )
    write(
        repo,
        "tests/test_holder_explicit.py",
        "from fakepkg.holder import Config\n"
        "\n"
        "\n"
        "def test_explicit() -> None:\n"
        "    cfg = Config(flag=False)\n"
        "    assert isinstance(cfg, Config)\n",
    )
    commit(repo, "holder and constructors")
    node = "tests/test_holder_default.py::test_default"
    write_map(repo, {mod: {"2": [], "1": [node]}})
    write(repo, mod, "class Config:\n    flag: bool = False\n")
    reasons = cov_select(repo, selector)
    assert list(reasons) == [node]
    assert "symbol flag" in reasons[node]


def test_map_age_and_gap_are_reported(repo: Path, selector: ModuleType) -> None:
    write_map(repo, {CORE: {"5": [DIRECT_NODE]}})
    write(repo, "docs/note.md", "# note\n")
    commit(repo, "one commit after the map")
    write(repo, CORE, BASE_TREE[CORE].replace("return VALUE", "return VALUE + 1"))
    selection = selector.select(repo, "HEAD", strategy="coverage")
    assert selection.map_age_commits == 1
    assert selection.changed_line_total == 1
    assert selection.unmapped_lines == 0
    assert selection.unmapped_share == 0.0


def test_stale_for_diff_message_when_most_lines_unmapped(
    repo: Path, selector: ModuleType
) -> None:
    """Changed lines absent from the shard count as unmapped; >50% warns."""
    write_map(repo, {CORE: {"1": []}})
    write(repo, CORE, BASE_TREE[CORE].replace("return VALUE", "return VALUE + 1"))
    selection = selector.select(repo, "HEAD", strategy="coverage")
    assert selection.changed_line_total == 1
    assert selection.unmapped_lines == 1
    explained = selector.format_explain(selection)
    assert "changed lines without coverage data: 1/1 (100%)" in explained
    assert "map stale for this diff, consider rebuild" in explained
    payload = json.loads(selector.selection_json(selection))
    assert payload["map"]["age_commits"] == 0
    assert payload["map"]["unmapped_lines"] == 1
    assert payload["map"]["changed_line_total"] == 1
    assert payload["ghosts"] == []
