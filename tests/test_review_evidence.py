"""Engine-verified review evidence is built from the candidate tree (ADR-0055)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from saberops.review_evidence import build_review_evidence


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Toy Owner")
    _git(repo, "config", "user.email", "toy-owner@example.com")
    (repo / "app.py").write_text(
        'def greet(name: str) -> str:\n    return f"hello {name}"\n', encoding="utf-8"
    )
    (repo / "cli.py").write_text(
        "from app import greet\n\n\ndef main() -> None:\n    print(greet('Ada'))\n",
        encoding="utf-8",
    )
    (repo / "test_app.py").write_text(
        "from app import greet\n\n\ndef test_greet() -> None:\n"
        '    assert greet("Ada") == "hi Ada"\n',
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    return repo, _git(repo, "rev-parse", "HEAD").strip()


def _commit(repo: Path) -> None:
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "candidate")


def test_unchanged_imported_module_content_reaches_the_reviewer(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path)
    (repo / "test_app.py").write_text(
        "from app import greet\n\n\ndef test_greet() -> None:\n"
        '    assert greet("Ada") == "hello Ada"\n',
        encoding="utf-8",
    )
    _commit(repo)

    evidence = build_review_evidence(
        worktree=repo,
        base_commit=base,
        task="Fix the failing test.\n\nAcceptance:\nThe failing test passes.",
        clauses=("The failing test passes.",),
        gate_command="python -B -c 'import app'",
        gate_exit_code=0,
        gate_stdout="",
    )

    assert "--- app.py (candidate HEAD content) ---" in evidence
    assert 'return f"hello {name}"' in evidence
    assert "--- test_app.py (candidate HEAD content) ---" in evidence
    assert "gate and test output" in evidence.lower()
    assert "exit 0" in evidence


def test_repository_wide_clause_gets_the_engines_own_search(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path)
    for name in ("app.py", "cli.py", "test_app.py"):
        path = repo / name
        path.write_text(
            path.read_text(encoding="utf-8").replace("greet", "welcome"),
            encoding="utf-8",
        )
    _commit(repo)

    evidence = build_review_evidence(
        worktree=repo,
        base_commit=base,
        task="Rename greet to welcome everywhere.\n\nAcceptance:\n"
        "No reference to greet remains in the repository.",
        clauses=("No reference to greet remains in the repository.",),
    )

    assert "$ git grep -n -I -i -e greet HEAD -- ." in evidence
    assert "exit 1 (no matches)" in evidence


def test_search_results_are_shown_when_a_clause_is_not_satisfied(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path)
    (repo / "README.md").write_text("# toy\n", encoding="utf-8")
    _commit(repo)

    evidence = build_review_evidence(
        worktree=repo,
        base_commit=base,
        task="Rename greet to welcome.\n\nAcceptance:\n"
        "No reference to greet remains in the repository.",
        clauses=("No reference to greet remains in the repository.",),
    )

    assert "exit 0 (" in evidence
    assert "app.py" in evidence


def test_an_empty_candidate_states_that_the_engine_saw_no_change(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path)
    evidence = build_review_evidence(
        worktree=repo,
        base_commit=base,
        task="Do the work.\n\nAcceptance:\nThe work is done.",
        clauses=("The work is done.",),
    )

    assert "none" in evidence
    assert "the engine found no file changed" in evidence
