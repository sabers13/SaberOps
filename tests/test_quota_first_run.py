"""A5 tests: fresh-setup quota evidence gathering and truthful `orch quota list`."""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from saberops.cli import main
from saberops.db import Database
from saberops.models import AttemptStatus, RunStatus, WorkerRequest, WorkerResult
from saberops.plans_ops import verify_posture
from saberops.service import OrchestratorService
from saberops.workers.base import WorkerAdapter
from saberops.workers.registry import AdapterRegistry


class _MockCodexWorker(WorkerAdapter):
    factual_capabilities = frozenset({"streaming", "large_prompt_stdin"})

    def __init__(self, provider: str = "codex") -> None:
        self._provider = provider
        self.called = False

    @property
    def provider_name(self) -> str:
        return self._provider

    def is_available(self) -> bool:
        return True

    def max_prompt_bytes(self) -> int:
        return 100_000

    def prompt_transport(self) -> str:
        return "stdin"

    def run(self, request: WorkerRequest) -> WorkerResult:
        self.called = True
        (request.worktree_path / "a5_probe.txt").write_text("ok\n", encoding="utf-8")
        return WorkerResult(
            provider=self._provider,
            model=request.model,
            status=AttemptStatus.SUCCESS,
            summary="completed",
            stdout="ok",
            stderr="",
            has_changes=True,
        )


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "a5@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "A5 Probe"], cwd=repo, check=True)
    (repo / "README.md").write_text("# a5\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)


@pytest.fixture()
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name, sub in (
        ("XDG_CONFIG_HOME", "xdg-config"),
        ("XDG_STATE_HOME", "xdg-state"),
        ("XDG_CACHE_HOME", "xdg-cache"),
    ):
        d = tmp_path / sub
        d.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(name, str(d))
    monkeypatch.delenv("SABEROPS_PROFILE", raising=False)
    return tmp_path


def test_pinned_run_auto_probes_safe_reader_and_proceeds_on_fresh_store(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    verify_posture("codex", (), None, None, None)
    repo = isolated_env / "repo-ok"
    _init_repo(repo)
    db = Database(isolated_env / "orch.db")
    worker = _MockCodexWorker("codex")
    service = OrchestratorService(
        db=db,
        registry=AdapterRegistry([worker]),
        worktree_base=isolated_env / "worktrees",
        readiness_service=False,
    )
    monkeypatch.setattr(
        service.orchestrator,
        "quota_probes",
        {
            "codex": lambda now: [
                ("5h", 85.0, now + timedelta(hours=3), "EXACT"),
                ("weekly", 90.0, now + timedelta(days=3), "EXACT"),
            ]
        },
        raising=False,
    )

    from saberops.config import RunConfig

    # This test is about the quota probe, not review: use the owner's one
    # manual switch so no reviewer is required.
    probe_config = RunConfig(
        task="Probe task",
        target_repo=str(repo),
        gate_command=f"{sys.executable} -c pass",
        provider_override="codex",
        model_override="gpt-5.3-codex",
        review_disabled=True,
    )
    result = service.run_sync(
        task="Probe task",
        repo_path=repo,
        config=probe_config,
    )
    assert worker.called is True, result.summary
    assert result.passed is True
    assert result.status == RunStatus.COMPLETED


def test_pinned_run_refusal_on_failing_or_unsafe_reader_names_remedy(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from saberops.capacity.probes import run_due_probes, try_safe_quota_probe

    verify_posture("codex", (), None, None, None)
    verify_posture("anthropic", (), None, None, None)
    repo = isolated_env / "repo-fail"
    _init_repo(repo)
    db = Database(isolated_env / "orch.db")

    # 1. Failing safe reader for codex -> refusal names exact remedy command.
    codex_worker = _MockCodexWorker("codex")
    codex_service = OrchestratorService(
        db=db,
        registry=AdapterRegistry([codex_worker]),
        worktree_base=isolated_env / "worktrees-codex",
        readiness_service=False,
    )

    def _failing_probe(_now: datetime) -> list[Any]:
        raise RuntimeError("codex probe failed")

    monkeypatch.setattr(
        codex_service.orchestrator,
        "quota_probes",
        {"codex": _failing_probe},
        raising=False,
    )
    codex_res = codex_service.run_sync(
        task="Probe fail task",
        repo_path=repo,
        gate_command=f"{sys.executable} -c pass",
        provider_override="codex",
        model_override="gpt-5.3-codex",
        review=False,
    )
    assert codex_worker.called is False
    assert codex_res.status == RunStatus.FAILED
    assert "orch quota set codex" in codex_res.summary

    # 2. Unsafe reader for anthropic (claude PTY trust-dialog probe) must NOT
    #    run automatically, and refusal must name `orch quota set anthropic`.
    def _must_not_run(*_args: Any, **_kwargs: Any) -> list[Any]:
        raise AssertionError("claude PTY reader must not run automatically")

    monkeypatch.setattr("saberops.capacity.probes.read_claude_windows", _must_not_run)
    assert try_safe_quota_probe(db, provider="anthropic") == "unsafe-auto-probe"
    assert (
        run_due_probes(db, force_harnesses=("claude",), auto_only=True)["claude"]
        == "unsafe-auto-probe"
    )

    claude_worker = _MockCodexWorker("anthropic")
    claude_service = OrchestratorService(
        db=db,
        registry=AdapterRegistry([claude_worker]),
        worktree_base=isolated_env / "worktrees-claude",
        readiness_service=False,
    )
    claude_res = claude_service.run_sync(
        task="Claude unsafe auto probe task",
        repo_path=repo,
        gate_command=f"{sys.executable} -c pass",
        provider_override="anthropic",
        model_override="claude-sonnet-4-5",
        review=False,
    )
    assert claude_worker.called is False
    assert claude_res.status == RunStatus.FAILED
    assert "orch quota set anthropic" in claude_res.summary


def test_parked_job_quota_evidence_refusal_names_the_exact_command(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The planner sees the remedy for a pool only the owner can supply."""
    from saberops.jobs import store
    from saberops.jobs.service import submit_job
    from saberops.mcp.tools import ServerContext, _job_guidance, build_default_registry

    repo = isolated_env / "repo-guidance"
    _init_repo(repo)
    db = Database(isolated_env / "guidance.db")
    job = submit_job(db, repo=repo, goal="needs owner quota evidence").job
    store.set_waiting(
        db,
        job.job_id,
        reason="QUOTA_EVIDENCE_REQUIRED",
        details={
            "diagnostic": "QUOTA_EVIDENCE_REQUIRED",
            "provider": "anthropic",
            "pool_id": "claude-pro",
            "remedy": "orch quota set anthropic claude-pro --remaining <percent>",
        },
    )
    current = store.get_job(db, job.job_id)
    assert current is not None
    guidance = _job_guidance(current, db)
    assert guidance is not None
    assert "orch quota set anthropic claude-pro --remaining <percent>" in guidance
    assert "owner" in guidance.lower()

    # wait_job cannot clear this by waiting, so it reports it at once with the
    # same remedy instead of timing out and looking idle.
    monkeypatch.setattr(
        "saberops.runtime_gate.gate_facts",
        lambda **_kwargs: {"closed": False, "label": "running"},
    )
    registry = build_default_registry(ServerContext(db_path=db.db_path))
    waited = registry.call(
        "wait_job", {"job_id": job.job_id, "timeout_seconds": 30}
    )
    assert waited["outcome"] == "needs_attention"
    assert waited["timed_out"] is False
    assert "orch quota set anthropic claude-pro" in waited["reason"]


def test_quota_set_records_the_window_an_owner_reading_belongs_to(
    isolated_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """H48/ADR-0106: the weekly figure is recorded as a weekly reading.

    H37 recorded the owner's weekly Claude figure on a subscription pool whose
    name is not a window name, so every review projected it as a 5-hour reading.
    ``--window`` records what the number actually measures and the state that
    reaches admission carries it.
    """
    from datetime import UTC

    from saberops.capacity.snapshot import effective_quota_states

    db_path = isolated_env / "orch.db"
    reset_at = "2026-11-27T07:00:00+00:00"
    assert main(
        ["quota", "set", "anthropic", "discovered-pool:claude-code-subscription",
         "--remaining", "20", "--window", "weekly", "--reset-at", reset_at,
         "--db", str(db_path)]
    ) == 0
    out = capsys.readouterr().out
    assert "window        : weekly" in out

    states = effective_quota_states(
        Database(db_path), now=datetime(2026, 11, 27, 3, 0, tzinfo=UTC), run_probes=False
    )
    state = next(
        s for s in states
        if (getattr(s, "pool_id", None) or s.pool)
        == "discovered-pool:claude-code-subscription"
    )
    assert state.window_id == "weekly"
    assert state.normalized_remaining_pct == pytest.approx(20.0)


def test_quota_list_on_fresh_store_does_not_claim_normal(
    isolated_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["quota", "list", "--db", str(isolated_env / "orch.db")])
    captured = capsys.readouterr()
    assert code == 0
    assert "treated as NORMAL" not in captured.out
    assert "QUOTA_EVIDENCE_REQUIRED" in captured.out
    assert "orch quota set" in captured.out


def test_quota_set_accepts_the_percentage_an_owner_actually_answers(
    isolated_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """D11: an answer of "94%" is a number, not an argparse error."""
    db_path = isolated_env / "orch.db"
    code = main(
        ["quota", "set", "anthropic", "discovered-pool:claude-code-subscription",
         "--remaining", "94%", "--db", str(db_path)]
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert "remaining     : 94%" in out

    # A bare number still works, and out-of-range values stay refused.
    assert main(["quota", "set", "codex", "weekly", "--remaining", "60",
                 "--db", str(db_path)]) == 0
    capsys.readouterr()
    with pytest.raises(SystemExit):
        main(["quota", "set", "codex", "weekly", "--remaining", "140%",
              "--db", str(db_path)])
