"""H34: the size check records logic lines and suppression comments.

A change that grows either number past the recorded budget fails until the
recorded number is updated with a one-line reason.  The counting rule is
token-based: strings, docstrings, comments and layout are not logic lines, and
only check-suppressing comment directives count as suppressions.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
import size_budget

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "size_budget.py"
BUDGET = REPO / "scripts" / "size_budget.json"

FIXTURE_MODULE = '''"""Module docstring.

More prose with a # noqa marker that is not a comment.
"""
# A plain comment; comments have no limit.
import os


def read(value: int) -> str:
    """Return text.

    A docstring mentioning # noqa is still a docstring.
    """
    text = """multi
line data"""
    marker = "# noqa"
    "bare data"
    if value:  # noqa: E501
        return f"{value}"  # type: ignore[return-value]
    return text + marker
'''


def _tree(root: Path) -> Path:
    module = root / "src" / "pkg" / "mod.py"
    module.parent.mkdir(parents=True)
    module.write_text(FIXTURE_MODULE, encoding="utf-8")
    return module


def _run(root: Path, budget: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            *args,
            "--root",
            str(root),
            "--budget",
            str(budget),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_a_line_counts_only_when_it_carries_code(tmp_path: Path) -> None:
    """Covers: docstrings, string content, blank lines and plain comments are free."""
    module = _tree(tmp_path)
    assert size_budget.count_file(module) == size_budget.Counts(
        logic_lines=7, suppression_comments=2
    )


def test_growth_past_the_recorded_number_fails(tmp_path: Path) -> None:
    """Covers: H34 done-when (a growing change fails without an updated number)."""
    _tree(tmp_path)
    budget = tmp_path / "budget.json"
    budget.write_text(
        json.dumps(
            {"logic_lines": 6, "suppression_comments": 1, "reason": "older tree"}
        ),
        encoding="utf-8",
    )
    result = _run(tmp_path, budget, "--check")
    assert result.returncode == 1
    assert "exceed the recorded" in result.stderr


def test_updated_number_with_a_reason_passes(tmp_path: Path) -> None:
    """Covers: H34 done-when (the update plus a one-line reason passes)."""
    _tree(tmp_path)
    budget = tmp_path / "budget.json"
    written = _run(tmp_path, budget, "--write", "--reason", "adds the read helper")
    assert written.returncode == 0, written.stderr
    payload = json.loads(budget.read_text(encoding="utf-8"))
    assert payload == {
        "logic_lines": 7,
        "suppression_comments": 2,
        "reason": "adds the read helper",
    }
    result = _run(tmp_path, budget, "--check")
    assert result.returncode == 0, result.stderr
    assert "within budget" in result.stdout


def test_write_without_a_reason_is_refused(tmp_path: Path) -> None:
    """Covers: an update without a reason is not a valid update."""
    _tree(tmp_path)
    result = _run(tmp_path, tmp_path / "budget.json", "--write")
    assert result.returncode == 2
    assert "one-line --reason" in result.stderr


def test_budget_reason_must_be_one_line(tmp_path: Path) -> None:
    """Covers: the recorded reason is one line, never a narrative."""
    _tree(tmp_path)
    budget = tmp_path / "budget.json"
    budget.write_text(
        json.dumps(
            {"logic_lines": 7, "suppression_comments": 2, "reason": "two\nlines"}
        ),
        encoding="utf-8",
    )
    result = _run(tmp_path, budget, "--check")
    assert result.returncode == 2
    assert "one non-empty line" in result.stderr


@pytest.mark.serial
def test_current_tree_is_within_the_recorded_budget_and_counts_under_a_second() -> None:
    """Covers: the recorded post-phase-1.1 numbers and the sub-second budget.

    Process time measures the check's own work.  Wall time inside a parallel
    xdist run also measures the xdist crowd; the check on its own (including
    interpreter startup) is measured in the ADR and the lane notes.
    """
    budget = size_budget.load_budget(BUDGET)
    timings: list[float] = []
    counts = size_budget.Counts(0, 0)
    for _ in range(3):
        started = time.process_time()
        counts = size_budget.count_tree(REPO)
        timings.append(time.process_time() - started)
    assert counts.logic_lines <= budget.logic_lines
    assert counts.suppression_comments <= budget.suppression_comments
    assert min(timings) < 1.0


def test_gate_runs_the_size_check() -> None:
    """Covers: the check is wired into the shared ruff/mypy/pytest gate runner."""
    tree = ast.parse((REPO / "scripts" / "gate_tools.py").read_text(encoding="utf-8"))
    run_checks = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "run_checks"
    )
    literals = {
        node.value
        for node in ast.walk(run_checks)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert "scripts/size_budget.py" in literals
