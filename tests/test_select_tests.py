"""Selection rules for scripts/select_tests.py, the per-task affected-test gate.

Every test builds a throwaway git repository with a tiny fake package, so the real
saberops package is never imported and the fake tests are never executed: only the
selector's decisions are checked. No provider, model or network access.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SELECTOR_SCRIPT = REPO_ROOT / "scripts" / "select_tests.py"
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
