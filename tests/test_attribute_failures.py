"""Attribution against tiny local branches; fake tests are never executed."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).resolve().parents[1] / "scripts" / f"{name}.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


load("select_tests")
load("gate_tools")
a = load("attribute_failures")


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


def test_branches_and_attribution(tmp_path: Path) -> None:
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "src/fake").mkdir(parents=True)
    (tmp_path / "src/fake/core.py").write_text("VALUE = 1\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_core.py").write_text("from fake.core import VALUE\n")
    (tmp_path / "tests/test_other.py").write_text("def test_other(): pass\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "base")
    base = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "checkout", "-qb", "lane-a")
    (tmp_path / "src/fake/core.py").write_text("VALUE = 2\n")
    git(tmp_path, "commit", "-qam", "core change")
    git(tmp_path, "checkout", "-qb", "lane-b", base)
    (tmp_path / "tests/test_core.py").write_text("from fake.core import VALUE\n# modified\n")
    git(tmp_path, "commit", "-qam", "test change")
    before = git(tmp_path, "rev-parse", "HEAD")
    lanes = {
        name: a.lane_evidence(tmp_path, base, branch)
        for name, branch in [("A", "lane-a"), ("B", "lane-b")]
    }
    assert git(tmp_path, "rev-parse", "HEAD") == before
    node = "tests/test_core.py::test_value"
    error = {node: "src/fake/core.py:1: AssertionError"}
    high = a.attribute([node], {"A": lanes["A"]}, error)
    assert "A (high confidence)" in high and "selects, traceback" in high
    interaction = a.attribute([node], lanes, error)
    assert "interaction" in interaction and "owns-test" in interaction
    low = a.attribute([node], {"B": lanes["B"]}, {})
    assert "high confidence" in low and "owns-test" in low
    unknown = a.attribute(["tests/test_other.py::test_other"], lanes, {})
    assert "unattributed" in unknown
    lint = a.attribute(
        [],
        lanes,
        {},
        "+ .venv/bin/python -m mypy --strict src tests\n"
        "src/fake/core.py:1: error: incompatible type",
    )
    assert "## A" in lint and "incompatible type" in lint


def test_single_signal_and_ruff_location() -> None:
    node = "tests/test_a.py::test_a"
    lanes = {"A": ({"src/pkg/a.py"}, {node}, False)}
    text = a.attribute(
        [node],
        lanes,
        {},
        "+ .venv/bin/python -m ruff check .\n"
        "F821 Undefined name `missing`\n"
        "  --> src/pkg/a.py:2:1",
    )
    assert "A (low confidence)" in text
    assert "lint/type `src/pkg/a.py`" in text and "F821" in text


def test_full_selection_does_not_create_interaction() -> None:
    node = "tests/test_a.py::test_a"
    lanes = {
        "A": ({"src/pkg/a.py"}, {node}, False),
        "B": ({"src/pkg/defaults/plans.json"}, set(), True),
    }
    text = a.attribute([node], lanes, {})
    assert "## A (low confidence)" in text
    assert "A: selects" in text
    assert "full-selection by B" in text
    assert "interaction" not in text and "B: selects" not in text


def test_full_selection_only_is_weak_unattributed_candidate() -> None:
    node = "tests/test_unknown.py::test_unknown"
    lanes = {
        "A": ({"src/pkg/a.py"}, {"tests/test_a.py"}, False),
        "B": ({"src/pkg/defaults/plans.json"}, set(), True),
    }
    text = a.attribute([node], lanes, {})
    assert "## unattributed" in text
    assert "weak: full-selection by B" in text
    assert "interaction" not in text and "B: selects" not in text


def test_traceback_lines_are_not_lint_type() -> None:
    lanes: dict[str, tuple[set[str], set[str], bool]] = {"A": ({"src/fake/core.py"}, set(), False)}
    log = (
        "+ .venv/bin/python -m ruff check .\n"
        "All checks passed!\n"
        "+ .venv/bin/python -m mypy --strict src tests\n"
        "src/fake/core.py:3: error: incompatible type [assignment]\n"
        "src/fake/core.py:4: note: see definition\n"
        "Found 1 error in 1 file (checked 10 source files)\n"
        "+ .venv/bin/python -m pytest -q tests/test_x.py\n"
        "tests/test_x.py:12: AssertionError\n"
        "src/fake/core.py:3: in f\n"
        "E AssertionError: broken\n"
    )
    text = a.attribute([], lanes, {}, log)
    assert text.count("- lint/type") == 1
    assert "lint/type `src/fake/core.py`" in text
    assert "incompatible type" in text
    assert "AssertionError" not in text
    assert "## notes" not in text


def test_log_without_ruff_mypy_sections_skips_lint_type() -> None:
    lanes: dict[str, tuple[set[str], set[str], bool]] = {"A": ({"src/fake/core.py"}, set(), False)}
    log = (
        "+ .venv/bin/python -m pytest -q tests/test_x.py\n"
        "tests/test_x.py:12: AssertionError\n"
        "src/fake/core.py:3: in f\n"
    )
    text = a.attribute([], lanes, {}, log)
    assert "- lint/type" not in text
    assert "## notes" in text
    assert "gate log has no ruff/mypy sections; lint/type attribution skipped" in text


def test_fix_lane_with_fork(tmp_path: Path) -> None:
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "src/fake").mkdir(parents=True)
    (tmp_path / "src/fake/core.py").write_text("VALUE = 1\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_core.py").write_text("def test_core(): pass\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "base")
    base = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "checkout", "-qb", "integration")
    (tmp_path / "src/fake/core.py").write_text("VALUE = 2\n")
    git(tmp_path, "commit", "-qam", "integration change")
    fork = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "checkout", "-qb", "fix-branch")
    (tmp_path / "src/fake/fix.py").write_text("FIX = 1\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "fix change")
    changed, _, _ = a.lane_evidence(tmp_path, base, "fix-branch", fork)
    assert changed == {"src/fake/fix.py"}
    changed_base, _, _ = a.lane_evidence(tmp_path, base, "fix-branch")
    assert changed_base == {"src/fake/core.py", "src/fake/fix.py"}
    git(tmp_path, "checkout", "-qb", "other", base)
    (tmp_path / "src/fake/core.py").write_text("VALUE = 3\n")
    git(tmp_path, "commit", "-qam", "other change")
    script = Path(__file__).resolve().parents[1] / "scripts" / "attribute_failures.py"
    failed = tmp_path / "failed.txt"
    failed.write_text("")
    proc = subprocess.run(
        [
            sys.executable,
            str(script),
            "--base",
            base,
            "--lane",
            "FIX=fix-branch@other",
            "--failed",
            str(failed),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2
    assert "FIX" in proc.stderr
    git(tmp_path, "checkout", "integration")
    git(tmp_path, "merge", "--no-ff", "-qm", "merge fix", "fix-branch")
    head = git(tmp_path, "rev-parse", "HEAD")
    before = git(tmp_path, "rev-parse", "HEAD")
    glue, selected, full = a.glue_evidence(tmp_path, base, head, ["fix-branch"])
    assert selected == set() and full is False
    assert "src/fake/fix.py" not in glue
    assert git(tmp_path, "rev-parse", "HEAD") == before


def _init_repo(repo: Path) -> str:
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test")
    (repo / "src/fake").mkdir(parents=True)
    (repo / "src/fake/core.py").write_text("VALUE = 1\n")
    (repo / "tests").mkdir()
    (repo / "tests/test_a.py").write_text("def test_a(): pass\n")
    (repo / "tests/test_b.py").write_text("def test_b(): pass\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    return git(repo, "rev-parse", "HEAD")


def test_glue_from_glue_commit(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    base = _init_repo(repo)
    git(repo, "checkout", "-qb", "lane-a", base)
    (repo / "src/fake/a.py").write_text("A = 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "lane a")
    git(repo, "checkout", "-qb", "lane-b", base)
    (repo / "src/fake/b.py").write_text("B = 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "lane b")
    git(repo, "checkout", "-qb", "integration", base)
    git(repo, "merge", "--no-ff", "-qm", "merge a", "lane-a")
    git(repo, "merge", "--no-ff", "-qm", "merge b", "lane-b")
    (repo / "tests/test_glue.py").write_text("def test_glue(): pass\n")
    (repo / "src/fake/core.py").write_text("VALUE = 99\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "glue commit")
    head = git(repo, "rev-parse", "HEAD")
    before = git(repo, "rev-parse", "HEAD")
    lanes = {
        name: a.lane_evidence(repo, base, branch)
        for name, branch in [("A", "lane-a"), ("B", "lane-b")]
    }
    glue = a.glue_evidence(repo, base, head, ["lane-a", "lane-b"])
    assert git(repo, "rev-parse", "HEAD") == before
    assert glue[0] == {"tests/test_glue.py", "src/fake/core.py"}
    assert glue[1] == set() and glue[2] is False
    full_lanes = {**lanes, "GLUE": glue}
    node = "tests/test_glue.py::test_glue"
    low = a.attribute([node], full_lanes, {})
    assert "GLUE (low confidence)" in low and "owns-test" in low
    assert "GLUE has no selects signal" in low
    high = a.attribute([node], full_lanes, {node: "src/fake/core.py:1: AssertionError"})
    assert "GLUE (high confidence)" in high


def test_glue_from_conflict_resolution(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src/fake").mkdir(parents=True)
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test")
    (repo / "src/fake/shared.py").write_text("VALUE = 1\n")
    (repo / "src/fake/other.py").write_text("OTHER = 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-qb", "lane-a", base)
    (repo / "src/fake/shared.py").write_text("VALUE = 2\n")
    git(repo, "commit", "-qam", "lane a")
    git(repo, "checkout", "-qb", "lane-b", base)
    (repo / "src/fake/shared.py").write_text("VALUE = 3\n")
    git(repo, "commit", "-qam", "lane b")
    git(repo, "checkout", "-qb", "integration", base)
    git(repo, "merge", "--no-ff", "-qm", "merge a", "lane-a")
    proc = subprocess.run(
        ["git", "merge", "lane-b"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    (repo / "src/fake/shared.py").write_text("VALUE = 4\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "resolve conflict")
    head = git(repo, "rev-parse", "HEAD")
    glue = a.glue_evidence(repo, base, head, ["lane-a", "lane-b"])
    assert glue[0] == {"src/fake/shared.py"}
    clean = tmp_path / "clean"
    clean.mkdir()
    clean_base = _init_repo(clean)
    git(clean, "checkout", "-qb", "lane-a", clean_base)
    (clean / "src/fake/a.py").write_text("A = 1\n")
    git(clean, "add", ".")
    git(clean, "commit", "-qm", "lane a")
    git(clean, "checkout", "-qb", "lane-b", clean_base)
    (clean / "src/fake/b.py").write_text("B = 1\n")
    git(clean, "add", ".")
    git(clean, "commit", "-qm", "lane b")
    git(clean, "checkout", "-qb", "integration", clean_base)
    git(clean, "merge", "--no-ff", "-qm", "merge a", "lane-a")
    git(clean, "merge", "--no-ff", "-qm", "merge b", "lane-b")
    clean_head = git(clean, "rev-parse", "HEAD")
    clean_glue = a.glue_evidence(clean, clean_base, clean_head, ["lane-a", "lane-b"])
    assert clean_glue[0] == set()


def test_glue_empty_matches_no_glue(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    base = _init_repo(repo)
    git(repo, "checkout", "-qb", "lane-a", base)
    (repo / "src/fake/a.py").write_text("A = 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "lane a")
    git(repo, "checkout", "-qb", "lane-b", base)
    (repo / "src/fake/b.py").write_text("B = 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "lane b")
    git(repo, "checkout", "-qb", "integration", base)
    git(repo, "merge", "--no-ff", "-qm", "merge a", "lane-a")
    git(repo, "merge", "--no-ff", "-qm", "merge b", "lane-b")
    head = git(repo, "rev-parse", "HEAD")
    lanes = {
        name: a.lane_evidence(repo, base, branch)
        for name, branch in [("A", "lane-a"), ("B", "lane-b")]
    }
    glue = a.glue_evidence(repo, base, head, ["lane-a", "lane-b"])
    assert glue[0] == set()
    node = "tests/test_a.py::test_a"
    without = a.attribute([node], lanes, {})
    with_glue = a.attribute([node], {**lanes, "GLUE": glue}, {})
    assert with_glue == without
