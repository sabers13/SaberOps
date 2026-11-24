"""H15: the deterministic project-scope guessers are gone.

``devscope`` and the non-plan-store ``project_scope`` modules guessed which
modules a task touches, and ``orch scope`` exposed the guess.  Choosing files
is the planner's job now; the worker prompt keeps the goal, acceptance, the
paths the planner named and the project notes.  This guard keeps a later change
from reintroducing a second, keyword-driven scope path beside the planner.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys

REMOVED_MODULES = (
    "saberops.devscope",
    "saberops.project_scope.graph",
    "saberops.project_scope.resolver",
    "saberops.project_scope.expansion",
    "saberops.project_scope.persistence",
    "saberops.project_scope.python_analyzer",
    "saberops.project_scope.affected",
    "saberops.project_scope.cli",
    "saberops.project_scope.context",
    "saberops.project_scope.contracts",
    "saberops.project_scope.metadata",
)


def test_scope_guesser_modules_are_not_importable() -> None:
    for name in REMOVED_MODULES:
        assert importlib.util.find_spec(name) is None, name


def test_plan_store_stays() -> None:
    assert importlib.util.find_spec("saberops.project_scope.plan_store") is not None


def test_scope_verb_is_not_accepted() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "saberops.cli", "scope", "show", "a task"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2
    assert "invalid choice" in proc.stderr
    help_proc = subprocess.run(
        [sys.executable, "-m", "saberops.cli", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert help_proc.returncode == 0
    assert "Inspect or expand deterministic project scope" not in help_proc.stdout
