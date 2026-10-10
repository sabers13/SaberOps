"""Run summaries name the real cause when the worker (not the gate) failed."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from saberops.config import RunConfig
from saberops.db import Database
from saberops.models import AttemptStatus, RunResult, RunStatus, WorkerRequest, WorkerResult
from saberops.orchestrator import Orchestrator
from saberops.workers.codex import CodexAdapter
from saberops.workers.registry import AdapterRegistry


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-b", "main")
    (path / "source.txt").write_text("original")
    _git(path, "add", ".")
    _git(path, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-m", "base")
    return path


def _orchestrate(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, adapter: CodexAdapter, gate: str
) -> RunResult:
    for key in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(key, str(tmp_path / key))
    for key in ("SABEROPS_PROFILE", "ORCH_BINDING_STORE"):
        monkeypatch.delenv(key, raising=False)
    # Hermetic: availability must not depend on a real `codex` binary being installed.
    monkeypatch.setattr(adapter, "is_available", lambda: True)
    return Orchestrator(
        db=Database(tmp_path / "run.db"),
        registry=AdapterRegistry([adapter]),
        worktree_base=tmp_path / "worktrees",
    ).run(
        config=RunConfig(
            task="Test failure summary",
            target_repo=str(repo),
            provider_override="codex",
            model_override="m",
            max_attempts=1,
            review_disabled=True,
            gate_command=gate,
        ),
    )


def test_worker_failure_is_named_in_the_exhaustion_summary(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = CodexAdapter()

    def crash(request: WorkerRequest) -> WorkerResult:
        return WorkerResult(
            "codex", "m", AttemptStatus.FAILED, "boom", "", "usage limit reached",
            has_changes=False, error="Worker exited with code 1",
        )

    monkeypatch.setattr(adapter, "run", crash)
    outcome = _orchestrate(repo, tmp_path, monkeypatch, adapter, f"{sys.executable} -c pass")
    assert outcome.status == RunStatus.FAILED
    summary = outcome.summary
    assert "attempt(s) failed to pass gate" in summary
    assert "Last attempt: Worker exited with code 1" in summary


def test_gate_that_writes_files_names_the_gitignore_remedy(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = CodexAdapter()

    def edit(request: WorkerRequest) -> WorkerResult:
        (request.worktree_path / "source.txt").write_text("changed")
        return WorkerResult(
            "codex", "m", AttemptStatus.SUCCESS, "ok", "ok", "", has_changes=True
        )

    monkeypatch.setattr(adapter, "run", edit)
    gate = f"{sys.executable} -c \"open('cache.tmp','w').write('x')\""
    outcome = _orchestrate(repo, tmp_path, monkeypatch, adapter, gate)
    assert outcome.status == RunStatus.FAILED
    error = outcome.attempts[-1].worker_error or ""
    assert "mutated" in error
    assert "()" not in error
    assert ".gitignore" in error
