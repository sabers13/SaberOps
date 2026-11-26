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
    assert "Gate command output (engine-run; exact command below)" in evidence
    assert "$ python -B -c 'import app'" in evidence
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


def test_deleted_assertions_are_engine_evidence(tmp_path: Path) -> None:
    """Covers: H41 (a removed assertion reaches the reviewer as engine evidence)."""
    repo, _base = _repo(tmp_path)
    (repo / "app.py").write_text(
        "def greet(name: str) -> str:\n"
        "    assert name\n"
        "    return f\"hello {name}\"\n",
        encoding="utf-8",
    )
    _commit(repo)
    with_assert = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "app.py").write_text(
        "def greet(name: str) -> str:\n"
        "    return f\"hello {name}\"\n",
        encoding="utf-8",
    )
    _commit(repo)

    evidence = build_review_evidence(
        worktree=repo,
        base_commit=with_assert,
        task="Simplify greeting.\n\nAcceptance:\nThe greeting still works.",
        clauses=("The greeting still works.",),
    )

    assert "deleted assertions: 1" in evidence
    assert "app.py: assert name" in evidence


def test_retirement_suppressions_and_catch_alls_are_engine_evidence(
    tmp_path: Path,
) -> None:
    repo, base = _repo(tmp_path)
    (repo / "test_app.py").unlink()
    (repo / "app.py").write_text(
        "def greet(name: str) -> str:\n"
        "    try:\n"
        "        return f\"hello {name}\"\n"
        "    except Exception:\n"
        "        return \"hello\"  # noqa: BLE001\n",
        encoding="utf-8",
    )
    (repo / "test_new.py").write_text(
        "import pytest\n\n\n"
        "@pytest.mark.skip(reason='temporarily disabled')\n"
        "def test_greet() -> None:\n"
        "    assert True\n",
        encoding="utf-8",
    )
    _commit(repo)

    evidence = build_review_evidence(
        worktree=repo,
        base_commit=base,
        task="Make greeting robust.\n\nAcceptance:\nThe greeting still works.",
        clauses=("The greeting still works.",),
        touches=("app.py",),
    )

    assert "deleted tests: test_app.py::test_greet" in evidence
    assert "outside the lane's declared area" in evidence
    assert "test_app.py" in evidence
    assert "new skip/xfail markers" in evidence
    assert "new suppression comments: app.py:" in evidence
    assert "new catch-all exception handlers: app.py:" in evidence


def test_empty_owns_is_no_scope_evidence_and_never_a_block(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path)
    (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    _commit(repo)

    evidence = build_review_evidence(
        worktree=repo,
        base_commit=base,
        task="Change the value.",
        clauses=(),
    )

    assert "declared no write scope" in evidence
    assert "legacy plan" in evidence


def test_an_ordinary_fix_that_edits_a_test_is_only_evidence(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path)
    (repo / "test_app.py").write_text(
        "from app import greet\n\n\ndef test_greet() -> None:\n"
        '    assert greet("Ada") == "hello Ada"\n',
        encoding="utf-8",
    )
    (repo / "app.py").write_text(
        'def greet(name: str) -> str:\n    return f"hello {name}"\n',
        encoding="utf-8",
    )
    _commit(repo)

    evidence = build_review_evidence(
        worktree=repo,
        base_commit=base,
        task="Fix the failing test.",
        clauses=("The failing test passes.",),
        touches=("app.py", "test_app.py"),
    )

    assert "rewritten test bodies" in evidence
    assert "test_app.py::test_greet" in evidence
