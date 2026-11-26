"""H41: gate config overrides and added switch-offs are owner questions.

A candidate could add an empty ``pytest.ini`` (which makes pytest ignore the
trusted ``pyproject.toml`` options), a ``pytest.ini`` whose ``addopts``
deselects the failing test, or a skip marker on the failing test, and pass
both checks.  Every override config file is now a protected gate definition,
and every suppression or skip directive on an added line is the same owner
question, listing each directive.  Fake CLIs only; no vendor or model call.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from saberops.config import RunConfig
from saberops.gate_runner import (
    GateProtectionBlockedError,
    GateRunner,
    GateRunnerRequest,
    verify_gate_definitions,
)
from saberops.gate_scope import (
    GATE_PROTECTED_PATH_MUTATED,
    GATE_WEAKENING_DIRECTIVE_ADDED,
)
from saberops.models import GateScratchMode
from saberops.orchestrator import _run_configured_gate

_OVERRIDE_CONFIG_PATHS = (
    "pytest.ini",
    ".pytest.ini",
    "tox.ini",
    "setup.cfg",
    "ruff.toml",
    ".ruff.toml",
    "mypy.ini",
    ".mypy.ini",
    ".github/workflows/release.yml",
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


def _repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "T")
    (repo / ".gitignore").write_text(".pytest_cache/\n__pycache__/\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\naddopts = "-q"\n', encoding="utf-8"
    )
    (repo / "app.py").write_text(
        "def value() -> int:\n    return 1\n", encoding="utf-8"
    )
    (repo / "test_app.py").write_text(
        "def test_strict():\n    assert False, 'base gate fails'\n"
        "\n\ndef test_ok():\n    assert True\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base with a failing test")
    return repo, _git(repo, "rev-parse", "HEAD")


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


def test_empty_pytest_ini_override_is_blocked_before_the_gate_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: H41 reproduction (an empty pytest.ini buys a passing check)."""
    monkeypatch.setenv("SABEROPS_GATE_MEMORY_GUARD", "0")
    repo, base = _repo(tmp_path)
    (repo / "pytest.ini").write_text("", encoding="utf-8")
    _commit(repo, "candidate adds an empty pytest.ini")
    marker = tmp_path / "gate-ran"
    command = [
        sys.executable,
        "-c",
        f"import pathlib; pathlib.Path({str(marker)!r}).write_text('ran')",
    ]
    config = RunConfig(task="repro", target_repo=str(repo))
    result = _run_configured_gate(
        config, command, cwd=repo, timeout=60.0, base=base
    )
    assert result.passed is False
    assert "GATE_PROTECTION_BLOCKED" in result.stderr
    assert GATE_PROTECTED_PATH_MUTATED in result.stderr
    assert "Owner decision required" in result.stderr
    assert marker.exists() is False


def test_addopts_deselect_is_blocked_on_the_acceptance_path(tmp_path: Path) -> None:
    """Covers: H41 reproduction on acceptance (the gate never runs)."""
    repo, base = _repo(tmp_path)
    (repo / "pytest.ini").write_text(
        "[pytest]\naddopts = --deselect test_app.py::test_strict\n", encoding="utf-8"
    )
    candidate = _commit(repo, "candidate deselects the failing test")
    evidence = tmp_path / "evidence"
    with pytest.raises(GateProtectionBlockedError) as excinfo:
        GateRunner().run(
            GateRunnerRequest(
                target_repo=repo,
                gate_command=f"{sys.executable} -m pytest -q",
                timeout=60.0,
                frozen_gate_scratch_mode=GateScratchMode.DISK,
                candidate_sha=candidate,
                expected_post_gate_sha=candidate,
                evidence_dir=evidence,
                base_sha=base,
                changed_files=("pytest.ini",),
            ),
            disk_guard=lambda _repo: None,
        )
    assert GATE_PROTECTED_PATH_MUTATED in str(excinfo.value)
    assert excinfo.value.protection.owner_question
    assert list(evidence.rglob("outcome.json")) == []


@pytest.mark.parametrize("path", _OVERRIDE_CONFIG_PATHS)
def test_each_override_config_file_is_a_protected_gate_definition(
    tmp_path: Path, path: str
) -> None:
    """Covers: H41 (one test per protected config file type)."""
    repo, base = _repo(tmp_path)
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("[pytest]\naddopts = --ignore=test_app.py\n", encoding="utf-8")
    _commit(repo, f"candidate adds {path}")

    protection = verify_gate_definitions(repo, base, changed_paths=(path,))

    assert protection.is_protected is True
    assert protection.allowed is False
    assert path in protection.tampered_paths
    assert protection.reason_code == GATE_PROTECTED_PATH_MUTATED
    assert protection.owner_question


@pytest.mark.parametrize(
    ("kind", "snippet"),
    [
        ("noqa", "    return 1  # noqa: E501"),
        ("type-ignore", "    return 1  # type: ignore[return-value]"),
        ("lint-disable", "    return 1  # pylint: disable=unused-argument"),
        ("skip", "    pytest.skip('not today')"),
        ("skip", "@pytest.mark.skip(reason='not today')"),
        ("skip", "@pytest.mark.xfail(reason='not today')"),
    ],
)
def test_each_added_weakening_directive_is_an_owner_question(
    tmp_path: Path, kind: str, snippet: str
) -> None:
    """Covers: H41 (one test per directive kind; the question lists each one)."""
    repo, base = _repo(tmp_path)
    (repo / "app.py").write_text(
        "def value() -> int:\n" + snippet + "\n", encoding="utf-8"
    )
    _commit(repo, "candidate adds a directive")

    protection = verify_gate_definitions(repo, base, changed_paths=("app.py",))

    assert protection.is_protected is True
    assert protection.allowed is False
    assert protection.reason_code == GATE_WEAKENING_DIRECTIVE_ADDED
    assert [item.kind for item in protection.directives] == [kind]
    assert snippet.strip() in protection.owner_question
    assert "app.py" in protection.owner_question


def test_prose_about_a_directive_is_not_a_directive(tmp_path: Path) -> None:
    """Covers: H41 (only a real directive fires; a comment about one does not)."""
    repo, base = _repo(tmp_path)
    (repo / "app.py").write_text(
        "def value() -> int:\n"
        "    # this comment explains why noqa and type: ignore are avoided\n"
        "    return 1\n",
        encoding="utf-8",
    )
    _commit(repo, "candidate adds an explanatory comment")

    protection = verify_gate_definitions(repo, base, changed_paths=("app.py",))

    assert protection.allowed is True
    assert protection.directives == ()


def test_owner_acceptance_into_the_base_lets_a_deliberate_change_through(
    tmp_path: Path,
) -> None:
    """Covers: H41 (the owner accepts the change into the base; the next gate allows it)."""
    repo, base = _repo(tmp_path)
    (repo / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (repo / "app.py").write_text(
        "def value() -> int:\n    return 1  # noqa: E501\n", encoding="utf-8"
    )
    candidate = _commit(repo, "candidate changes the gate config and adds a directive")

    blocked = verify_gate_definitions(
        repo, base, changed_paths=("pytest.ini", "app.py")
    )
    assert blocked.is_protected is True

    # The owner's decision is to accept the change into the base SHA.  The
    # working tree is already the candidate; judged against the new base it
    # has no diff, so no definition changed and no directive was added.
    accepted = verify_gate_definitions(repo, candidate)
    assert accepted.allowed is True
    assert accepted.tampered_paths == ()
    assert accepted.directives == ()


def test_an_ordinary_added_assertion_is_not_a_directive(tmp_path: Path) -> None:
    """Covers: H41 (ordinary work stays allowed)."""
    repo, base = _repo(tmp_path)
    (repo / "app.py").write_text(
        "def value() -> int:\n    result = 1\n    assert result == 1\n    return result\n",
        encoding="utf-8",
    )
    _commit(repo, "candidate adds an assertion")

    protection = verify_gate_definitions(repo, base, changed_paths=("app.py",))

    assert protection.allowed is True
    assert protection.directives == ()
