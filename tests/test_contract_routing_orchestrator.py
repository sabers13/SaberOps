"""Contract: orchestrator dispatches through role chains.

The planner's recorded difficulty labels the run tier, and it selects the
route: ``hard`` starts on the build route's stronger-model ``strong_start``
setting, ``routine`` or a missing value starts on the ordinary build chain
(ADR-0005 §3: no second role).  Escalation still moves the build route to its
strong start, and only on typed capability-failure evidence.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pytest import MonkeyPatch

from saberops.config import resolve_run_config
from saberops.continuation import prepare_continuation
from saberops.db import Database
from saberops.git import GitManager
from saberops.jobs import store
from saberops.jobs.models import (
    Job,
    JobRequirements,
    JobRole,
    JobState,
    Priority,
    WaitingReason,
)
from saberops.models import (
    AttemptStatus,
    DispatchOutcome,
    RunStatus,
    Tier,
    WorkerCandidate,
    WorkerInterruptionReason,
    WorkerRequest,
    WorkerResult,
)
from saberops.orchestrator import Orchestrator
from saberops.process import run_process
from saberops.supervisor import RunSupervisor
from saberops.workers.base import WorkerAdapter
from saberops.workers.registry import AdapterRegistry


def _write_synthetic_verified_postures(config_home: Path) -> None:
    """Seed an isolated owner config with a verified posture for the synthetic
    ``h3`` candidate used by this module.

    Covers: WORKFLOW §7 authorized setup-only fixture migration.
    This adds ONLY a synthetic provider/posture record needed to preserve the
    module's historical control-flow assertions now that unverified routes
    fail closed (F1); no assertion or expected behaviour is changed and no real
    vendor receives an attestation.
    """
    cfg_dir = config_home / "saberops"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    today_iso = datetime.now(UTC).date().isoformat()
    cp_doc = {
        "version": 2,
        "providers": {"h3": {"state": "ALLOWED"}},
        "quota_pools": {},
        "postures": {
            "h3": {
                "class": "A",
                "policy_urls": ["https://example.com/h3-policy"],
                "ttl_days": 90,
                "verified_on": today_iso,
                "note": "synthetic verified test posture for role-chain contract",
            }
        },
    }
    (cfg_dir / "control_plane.json").write_text(json.dumps(cp_doc), encoding="utf-8")


def _isolate_xdg_config(
    tmp_path_factory: pytest.TempPathFactory,
) -> Generator[None, None, None]:
    mpatch = pytest.MonkeyPatch()
    temp_dir = tmp_path_factory.mktemp("hermetic_xdg_config_role_contract")
    _write_synthetic_verified_postures(temp_dir)
    mpatch.setenv("XDG_CONFIG_HOME", str(temp_dir))
    mpatch.setenv("XDG_STATE_HOME", str(temp_dir))
    yield
    mpatch.undo()


@pytest.fixture(autouse=True, scope="module")
def _isolate_user_routing_config(
    tmp_path_factory: pytest.TempPathFactory,
) -> Generator[None, None, None]:
    yield from _isolate_xdg_config(tmp_path_factory)


def _init_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    run_process(["git", "init", "-b", "main"], cwd=path)
    run_process(["git", "config", "user.name", "Role Contract"], cwd=path)
    run_process(["git", "config", "user.email", "roles@example.test"], cwd=path)
    (path / ".gitignore").write_text("__pycache__/\n*.pyc\n.pytest_cache/\n")
    (path / "app.py").write_text("def hello(): return 'hello'\n")
    run_process(["git", "add", "."], cwd=path)
    run_process(["git", "commit", "-m", "init: initial code"], cwd=path)
    return GitManager.get_head_commit(path)


class ScriptedWorker(WorkerAdapter):
    """Scripted worker recording the exact model dispatch order."""

    def __init__(
        self,
        provider_name: str,
        script: dict[str, Any],
        call_log: list[str],
    ) -> None:
        self._provider_name = provider_name
        self._script = script
        self._call_log = call_log

    @property
    def provider_name(self) -> str:
        return self._provider_name

    def is_available(self) -> bool:
        return True

    def run(self, request: WorkerRequest) -> WorkerResult:
        if request.read_only:
            self._call_log.append(f"review:{request.model}")
            return WorkerResult(
                provider=self.provider_name,
                model=request.model,
                status=AttemptStatus.SUCCESS,
                summary="Scripted reviewer approved",
                stdout="VERDICT: PASS\n",
                stderr="",
                changed_paths=[],
                has_changes=False,
            )
        self._call_log.append(request.model)
        action = self._script.get(request.model)
        if callable(action):
            result = action(request)
            assert isinstance(result, WorkerResult)
            return result
        return WorkerResult(
            provider=self.provider_name,
            model=request.model,
            status=AttemptStatus.SUCCESS,
            summary="Default no-op",
            stdout="",
            stderr="",
            changed_paths=[],
            has_changes=False,
        )


def _role_keyed_chain(
    worker: list[WorkerCandidate],
    strong: list[WorkerCandidate],
    calls: list[str],
) -> Any:
    """Return a resolve_candidate_chain replacement keyed on the build setting."""

    def _resolve(
        *,
        role: Any | None = None,
        strong_start: bool = False,
        **_: Any,
    ) -> list[WorkerCandidate]:
        key = getattr(role, "value", role)
        calls.append(f"{key}:{'strong' if strong_start else 'ordinary'}")
        return list(strong) if strong_start else list(worker)

    return _resolve


def _success_with_file(provider: str, model: str, rel_path: str, content: str) -> Any:
    def _action(request: WorkerRequest) -> WorkerResult:
        target = request.worktree_path / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return WorkerResult(
            provider=provider,
            model=model,
            status=AttemptStatus.SUCCESS,
            summary=f"Wrote {rel_path}",
            stdout="",
            stderr="",
            changed_paths=[rel_path],
            has_changes=True,
        )

    return _action


def _capability_failure(provider: str, model: str) -> Any:

    def _action(request: WorkerRequest) -> WorkerResult:
        return WorkerResult(
            provider=provider,
            model=model,
            status=AttemptStatus.FAILED,
            summary="Model reports the task exceeds its capability",
            stdout="",
            stderr="",
            has_changes=False,
            error="explicit capability failure",
            outcome=DispatchOutcome.CAPABILITY_FAILURE,
        )

    return _action


def _quiescent_success_with_file(provider: str, model: str, rel_path: str, content: str) -> Any:
    """Like :func:`_success_with_file` but marks the session quiescent.

    Continuation requires the earlier provider session to be confirmed
    stopped; a quiescent outcome records exactly that.
    """

    def _action(request: WorkerRequest) -> WorkerResult:
        target = request.worktree_path / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return WorkerResult(
            provider=provider,
            model=model,
            status=AttemptStatus.SUCCESS,
            summary=f"Wrote {rel_path}",
            stdout="",
            stderr="",
            changed_paths=[rel_path],
            has_changes=True,
            execution_quiescent=True,
        )

    return _action


def _passing_gate() -> str:
    return f"{sys.executable} -c \"import sys; sys.exit(0)\""


def _ok_gate() -> str:
    return (
        f"{sys.executable} -c \"import pathlib,sys; "
        "sys.exit(0 if pathlib.Path(\\\"ok.txt\\\").exists() else 1)\""
    )


def _events_by_type(events: list[Any], event_type: str) -> list[Any]:
    return [e for e in events if e.event_type == event_type]


def test_auto_run_labelled_t1_dispatches_worker_chain_head(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """Covers: a missing difficulty stays on the build route head."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    log: list[str] = []
    calls: list[str] = []
    worker = ScriptedWorker("h3", {"m1": _success_with_file("h3", "m1", "A.txt", "A\n")}, log)
    worker_chain = [WorkerCandidate("h3", "m1")]
    strong_chain = [WorkerCandidate("h3", "s1")]
    monkeypatch.setattr(
        "saberops.orchestrator.resolve_candidate_chain",
        _role_keyed_chain(worker_chain, strong_chain, calls),
    )
    db = Database(tmp_path / "orch.db")
    orch = Orchestrator(db=db, registry=AdapterRegistry([worker]), worktree_base=tmp_path / "wt")
    config = resolve_run_config(
        task="fix typo in readme",
        target_repo=repo,
        gate_command=_passing_gate(),
        publish_candidate=False,
        # Routing probe: the owner switch keeps this test off the review path.
        review_disabled=True,
    )
    result = orch.run(config=config, is_peak=False)
    assert result.status == RunStatus.COMPLETED
    impl_log = [e for e in log if not e.startswith("review:")]
    assert impl_log == ["m1"], impl_log
    # Missing difficulty means routine: the route came from the build route.
    assert db.get_run(result.run_id) is not None
    assert db.get_run(result.run_id).tier == Tier.T1  # type: ignore[union-attr]
    assert calls and calls[0] == "BUILD:ordinary"
    resolved = _events_by_type(db.get_events(result.run_id), "tier_candidates_resolved")
    assert resolved and resolved[0].payload is not None
    assert resolved[0].payload["role"] == "BUILD"
    assert resolved[0].payload["routing_context"] == "BUILD"
    assert resolved[0].payload["strong_start"] is False


def test_low_effort_build_run_prefers_the_cheapest_candidate(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """Covers: H8 (``effort: low`` prefers the cheap model on any run)."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    log: list[str] = []
    calls: list[str] = []
    worker = ScriptedWorker(
        "h3",
        {
            "m2": _success_with_file("h3", "m2", "build.txt", "build\n"),
            "m1": _success_with_file("h3", "m1", "cheap.txt", "cheap\n"),
        },
        log,
    )
    # The owner's route names the ordinary model first; the cheap candidate is
    # a fallback.  Only the family matcher is stand-in here (H8 wiring test);
    # the real ``codex``/``anthropic``/``opencode-go`` families are covered in
    # tests/test_f27_cheap_scout.py.
    worker_chain = [WorkerCandidate("h3", "m2"), WorkerCandidate("h3", "m1")]
    monkeypatch.setattr(
        "saberops.orchestrator.resolve_candidate_chain",
        _role_keyed_chain(worker_chain, [], calls),
    )
    import saberops.scout_routing

    monkeypatch.setattr(
        saberops.scout_routing,
        "is_cheap_scout_model",
        lambda provider, model: model == "m1",
    )
    db = Database(tmp_path / "orch.db")
    orch = Orchestrator(db=db, registry=AdapterRegistry([worker]), worktree_base=tmp_path / "wt")
    config = resolve_run_config(
        task="survey the helper",
        target_repo=repo,
        gate_command=_passing_gate(),
        review_disabled=True,
        publish_candidate=False,
        effort="low",
    )
    result = orch.run(config=config, is_peak=False)
    assert result.status is RunStatus.COMPLETED
    assert log == ["m1"], log


def test_capability_failure_escalates_build_to_strong_start_once(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """Covers: ADR-0005 §3 escalation (one step to the build strong_start)."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    log: list[str] = []
    calls: list[str] = []
    script = {
        "m1": _capability_failure("h3", "m1"),
        "s1": _success_with_file("h3", "s1", "ok.txt", "ok\n"),
    }
    worker = ScriptedWorker("h3", script, log)
    worker_chain = [WorkerCandidate("h3", "m1")]
    strong_chain = [WorkerCandidate("h3", "s1")]
    monkeypatch.setattr(
        "saberops.orchestrator.resolve_candidate_chain",
        _role_keyed_chain(worker_chain, strong_chain, calls),
    )
    db = Database(tmp_path / "orch.db")
    orch = Orchestrator(db=db, registry=AdapterRegistry([worker]), worktree_base=tmp_path / "wt")
    config = resolve_run_config(
        task="fix typo in readme",
        target_repo=repo,
        gate_command=_ok_gate(),
        review_disabled=True,
        publish_candidate=False,
    )
    result = orch.run(config=config, is_peak=False)
    assert result.status == RunStatus.COMPLETED
    impl_log = [e for e in log if not e.startswith("review:")]
    assert impl_log == ["m1", "s1"], impl_log
    events = db.get_events(result.run_id)
    escalated = _events_by_type(events, "tier_escalated")
    assert len(escalated) == 1
    assert escalated[0].payload is not None
    assert escalated[0].payload["from_role"] == "BUILD"
    assert escalated[0].payload["to_role"] == "BUILD"
    assert escalated[0].payload["strong_start"] is True
    assert escalated[0].payload["to"] == "T3"
    resolved = _events_by_type(events, "tier_candidates_resolved")
    assert resolved and resolved[-1].payload is not None
    assert resolved[-1].payload["role"] == "BUILD"
    assert resolved[-1].payload["routing_context"] == "BUILD"
    assert resolved[-1].payload["strong_start"] is True
    assert db.get_run(result.run_id) is not None
    assert db.get_run(result.run_id).tier == Tier.T3  # type: ignore[union-attr]


def test_capability_failure_on_strong_start_never_escalates_further(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """Covers: a manual high-tier run never escalates further."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    log: list[str] = []
    calls: list[str] = []
    worker = ScriptedWorker("h3", {"s1": _capability_failure("h3", "s1")}, log)
    monkeypatch.setattr(
        "saberops.orchestrator.resolve_candidate_chain",
        _role_keyed_chain([WorkerCandidate("h3", "m1")], [WorkerCandidate("h3", "s1")], calls),
    )
    db = Database(tmp_path / "orch.db")
    orch = Orchestrator(db=db, registry=AdapterRegistry([worker]), worktree_base=tmp_path / "wt")
    config = resolve_run_config(
        task="fix typo in readme",
        target_repo=repo,
        routing_mode="manual",
        manual_tier="T3",
        gate_command=_passing_gate(),
        publish_candidate=False,
    )
    result = orch.run(config=config, is_peak=False)
    assert result.status == RunStatus.FAILED
    events = db.get_events(result.run_id)
    assert _events_by_type(events, "tier_escalated") == []


def test_capped_ceiling_withholds_escalation_and_evidence_event(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """Covers: escalation ceiling (max_auto_tier=T2 disables the strong start)."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    log: list[str] = []
    worker = ScriptedWorker("h3", {"m1": _capability_failure("h3", "m1")}, log)
    monkeypatch.setattr(
        "saberops.orchestrator.resolve_candidate_chain",
        _role_keyed_chain([WorkerCandidate("h3", "m1")], [WorkerCandidate("h3", "s1")], []),
    )
    db = Database(tmp_path / "orch.db")
    orch = Orchestrator(db=db, registry=AdapterRegistry([worker]), worktree_base=tmp_path / "wt")
    config = resolve_run_config(
        task="fix typo in readme",
        target_repo=repo,
        max_auto_tier="T2",
        gate_command=_passing_gate(),
        publish_candidate=False,
    )
    result = orch.run(config=config, is_peak=False)
    assert result.status == RunStatus.FAILED
    events = db.get_events(result.run_id)
    assert _events_by_type(events, "tier_escalated") == []
    assert _events_by_type(events, "stage_exhausted_without_escalation_evidence") == []


def test_manual_t3_starts_on_build_strong_start_without_escalation(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """Covers: --tier T3 selects the build route's strong_start setting."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    log: list[str] = []
    worker = ScriptedWorker("h3", {"s1": _success_with_file("h3", "s1", "A.txt", "A\n")}, log)
    monkeypatch.setattr(
        "saberops.orchestrator.resolve_candidate_chain",
        _role_keyed_chain([WorkerCandidate("h3", "m1")], [WorkerCandidate("h3", "s1")], []),
    )
    db = Database(tmp_path / "orch.db")
    orch = Orchestrator(db=db, registry=AdapterRegistry([worker]), worktree_base=tmp_path / "wt")
    config = resolve_run_config(
        task="fix typo in readme",
        target_repo=repo,
        routing_mode="manual",
        manual_tier="T3",
        gate_command=_passing_gate(),
        review_disabled=True,
        publish_candidate=False,
    )
    result = orch.run(config=config, is_peak=False)
    assert result.status == RunStatus.COMPLETED
    impl_log = [e for e in log if not e.startswith("review:")]
    assert impl_log == ["s1"], impl_log
    events = db.get_events(result.run_id)
    resolved = _events_by_type(events, "tier_candidates_resolved")
    assert resolved and resolved[0].payload is not None
    assert resolved[0].payload["role"] == "BUILD"
    assert resolved[0].payload["strong_start"] is True
    assert _events_by_type(events, "tier_escalated") == []


@pytest.mark.parametrize(
    ("difficulty", "expected_model", "expected_call", "expected_tier"),
    [
        ("hard", "s1", "BUILD:strong", Tier.T3),
        ("routine", "m1", "BUILD:ordinary", Tier.T1),
        (None, "m1", "BUILD:ordinary", Tier.T1),
    ],
)
def test_recorded_difficulty_selects_the_start_chain(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    difficulty: str | None,
    expected_model: str,
    expected_call: str,
    expected_tier: Tier,
) -> None:
    """Covers: H1 (hard starts strong, routine/missing start on the build chain)."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    log: list[str] = []
    calls: list[str] = []
    worker = ScriptedWorker(
        "h3",
        {
            "m1": _success_with_file("h3", "m1", "A.txt", "A\n"),
            "s1": _success_with_file("h3", "s1", "A.txt", "A\n"),
        },
        log,
    )
    monkeypatch.setattr(
        "saberops.orchestrator.resolve_candidate_chain",
        _role_keyed_chain([WorkerCandidate("h3", "m1")], [WorkerCandidate("h3", "s1")], calls),
    )
    db = Database(tmp_path / "orch.db")
    orch = Orchestrator(db=db, registry=AdapterRegistry([worker]), worktree_base=tmp_path / "wt")
    config = resolve_run_config(
        task="fix typo in readme",
        target_repo=repo,
        difficulty=difficulty,
        gate_command=_passing_gate(),
        review_disabled=True,
        publish_candidate=False,
    )
    result = orch.run(config=config, is_peak=False)
    assert result.status == RunStatus.COMPLETED
    impl_log = [e for e in log if not e.startswith("review:")]
    assert impl_log == [expected_model], impl_log
    assert calls and calls[0] == expected_call
    run = db.get_run(result.run_id)
    assert run is not None and run.tier is expected_tier
    resolved = _events_by_type(db.get_events(result.run_id), "tier_candidates_resolved")
    assert resolved and resolved[0].payload is not None
    assert resolved[0].payload["strong_start"] is (expected_call == "BUILD:strong")


def test_hard_difficulty_never_overrides_a_capped_auto_ceiling(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """Covers: H1 (the owner's max_auto_tier cap still wins over hard)."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    log: list[str] = []
    calls: list[str] = []
    worker = ScriptedWorker("h3", {"m1": _success_with_file("h3", "m1", "A.txt", "A\n")}, log)
    monkeypatch.setattr(
        "saberops.orchestrator.resolve_candidate_chain",
        _role_keyed_chain([WorkerCandidate("h3", "m1")], [WorkerCandidate("h3", "s1")], calls),
    )
    db = Database(tmp_path / "orch.db")
    orch = Orchestrator(db=db, registry=AdapterRegistry([worker]), worktree_base=tmp_path / "wt")
    config = resolve_run_config(
        task="implement the hard thing",
        target_repo=repo,
        difficulty="hard",
        max_auto_tier="T2",
        gate_command=_passing_gate(),
        review_disabled=True,
        publish_candidate=False,
    )
    result = orch.run(config=config, is_peak=False)
    assert result.status == RunStatus.COMPLETED
    impl_log = [e for e in log if not e.startswith("review:")]
    assert impl_log == ["m1"], impl_log
    assert calls and calls[0] == "BUILD:ordinary"
    run = db.get_run(result.run_id)
    assert run is not None and run.tier is Tier.T2


@pytest.mark.parametrize(
    ("mode", "tier", "expected"),
    [("auto", None, 1200.0), ("manual", "T3", 1200.0)],
)
def test_per_role_worker_timeout_reaches_worker_request(
    tmp_path: Path, monkeypatch: MonkeyPatch, mode: str, tier: str | None, expected: float
) -> None:
    """Covers: the one build-role worker deadline."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    seen: list[float] = []

    def _capture(request: WorkerRequest) -> WorkerResult:
        seen.append(request.timeout_seconds)
        target = request.worktree_path / "A.txt"
        target.write_text("A\n")
        return WorkerResult(
            provider="h3",
            model=request.model,
            status=AttemptStatus.SUCCESS,
            summary="ok",
            stdout="",
            stderr="",
            changed_paths=["A.txt"],
            has_changes=True,
        )

    worker = ScriptedWorker("h3", {"m1": _capture, "s1": _capture}, [])
    chain = [WorkerCandidate("h3", "m1" if mode == "auto" else "s1")]
    monkeypatch.setattr(
        "saberops.orchestrator.resolve_candidate_chain",
        _role_keyed_chain(chain, chain, []),
    )
    db = Database(tmp_path / "orch.db")
    orch = Orchestrator(db=db, registry=AdapterRegistry([worker]), worktree_base=tmp_path / "wt")
    kwargs: dict[str, Any] = {
        "gate_command": _passing_gate(),
        "review_disabled": True,
        "publish_candidate": False,
    }
    if mode == "manual":
        kwargs.update(routing_mode="manual", manual_tier=tier)
    config = resolve_run_config(task="fix typo in readme", target_repo=repo, **kwargs)
    result = orch.run(config=config, is_peak=False)
    assert result.status == RunStatus.COMPLETED
    assert seen == [expected]


def test_explicit_worker_timeout_overrides_role_default(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """Covers: explicit --worker-timeout wins over the role default."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    seen: list[float] = []

    def _capture(request: WorkerRequest) -> WorkerResult:
        seen.append(request.timeout_seconds)
        target = request.worktree_path / "A.txt"
        target.write_text("A\n")
        return WorkerResult(
            provider="h3",
            model=request.model,
            status=AttemptStatus.SUCCESS,
            summary="ok",
            stdout="",
            stderr="",
            changed_paths=["A.txt"],
            has_changes=True,
        )

    worker = ScriptedWorker("h3", {"m1": _capture}, [])
    chain = [WorkerCandidate("h3", "m1")]
    monkeypatch.setattr(
        "saberops.orchestrator.resolve_candidate_chain",
        _role_keyed_chain(chain, chain, []),
    )
    db = Database(tmp_path / "orch.db")
    orch = Orchestrator(db=db, registry=AdapterRegistry([worker]), worktree_base=tmp_path / "wt")
    config = resolve_run_config(
        task="fix typo in readme",
        target_repo=repo,
        gate_command=_passing_gate(),
        worker_timeout=42.0,
        publish_candidate=False,
        # Timeout probe: the owner switch keeps this test off the review path.
        review_disabled=True,
    )
    result = orch.run(config=config, is_peak=False)
    assert result.status == RunStatus.COMPLETED
    assert seen == [42.0]


def test_schema2_frozen_snapshot_continuation_keeps_bytes_and_worker_chain(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """Covers: frozen snapshots keep tier provenance."""
    import json

    from saberops.routing_config import _content_hash, payload_to_snapshot

    for key in ("XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(key, str(tmp_path / key))
    for key in ("SABEROPS_PROFILE", "ORCH_BINDING_STORE"):
        monkeypatch.delenv(key, raising=False)
    _write_synthetic_verified_postures(tmp_path / "XDG_CONFIG_HOME")
    repo = tmp_path / "project"
    _init_repo(repo)
    db = Database(tmp_path / "project-state" / "runs.db")
    worker = ScriptedWorker(
        "h3",
        {"m1": _quiescent_success_with_file("h3", "m1", "features.json", '{"saved_work": true}\n')},
        [],
    )
    chain = [WorkerCandidate("h3", "m1")]
    monkeypatch.setattr(
        "saberops.orchestrator.resolve_candidate_chain",
        _role_keyed_chain(chain, chain, []),
    )
    parent_result = Orchestrator(
        db=db, registry=AdapterRegistry([worker]), worktree_base=tmp_path / "trees"
    ).run(
        config=resolve_run_config(
            task="fix typo in readme",
            target_repo=repo,
            # A failing gate: continuation is available only for failed
            # runs, and the failed candidate commit is the resume source.
            gate_command=f"{sys.executable} -c \"import sys; sys.exit(1)\"",
            max_attempts=1,
            review_disabled=True,
            publish_candidate=False,
        )
    )
    assert parent_result.status == RunStatus.FAILED
    assert parent_result.attempts and parent_result.attempts[-1].commit_sha
    parent_id = parent_result.run_id
    # Freeze the parent's snapshot as a schema-2 document (as runs before
    # R-002 persisted it): T2 first so the derived build chain is ordered.
    legacy_chains = {
        "T1.default": ["opencode/opencode/hy3-free"],
        "T2.default": ["codex/gpt-5.6-terra"],
        "T3.default": ["codex/gpt-5.6-sol"],
    }
    legacy_payload = {
        "schema_version": 2,
        "source": "default",
        "source_path": None,
        "content_hash": _content_hash(legacy_chains, 2),
        "chains": {
            key: [{"provider": cid.split("/", 1)[0], "model": cid.split("/", 1)[1]} for cid in cids]
            for key, cids in legacy_chains.items()
        },
        "created_at": "2026-10-01T00:00:00+00:00",
    }
    parent_bytes = json.dumps(legacy_payload, sort_keys=True)
    with db._get_connection() as conn:
        conn.execute(
            "UPDATE runs SET routing_snapshot_json = ? WHERE id = ?",
            (parent_bytes, parent_id),
        )
    config = prepare_continuation(db, parent_id)
    supervisor = RunSupervisor(db=db, registry=AdapterRegistry([worker]))
    launches: list[str] = []

    def _record_launch(self: RunSupervisor, run_id: str, config: Any) -> None:
        launches.append(run_id)

    monkeypatch.setattr(RunSupervisor, "_launch", _record_launch)
    child_id = supervisor.start_supervised_run(config).run_id
    assert launches == [child_id]
    child = db.get_run(child_id)
    assert child is not None and child.routing_snapshot_json is not None
    assert json.loads(child.routing_snapshot_json) == json.loads(parent_bytes)
    derived = payload_to_snapshot(json.loads(child.routing_snapshot_json), run_id=child_id)
    assert [f"{c.provider}/{c.model}" for c in derived.chain_for_role(JobRole.BUILD)] == [
        "codex/gpt-5.6-terra",
        "opencode/opencode/hy3-free",
        "codex/gpt-5.6-sol",
    ]
    assert derived.legacy_chains is not None


def _quota_exhausted_action(provider: str, model: str) -> Any:
    """Script a genuine plan-quota interruption with unknown reset (D8 trigger)."""

    def _action(request: WorkerRequest) -> WorkerResult:
        return WorkerResult(
            provider=provider,
            model=request.model,
            status=AttemptStatus.FAILED,
            summary="Synthetic plan quota exhausted with unknown reset",
            stdout="",
            stderr="",
            changed_paths=[],
            has_changes=False,
            error="synthetic quota exhausted",
            outcome=DispatchOutcome.PROVIDER_OR_RUNTIME_FAILURE,
            interruption_reason=WorkerInterruptionReason.QUOTA_EXHAUSTED,
            quota_reset_at=None,
            quota_reset_confidence="UNKNOWN",
            execution_quiescent=True,
        )

    return _action


def _owning_job(db: Database, repo: Path, base: str, job_id: str, run_id: str) -> None:
    store.insert_job(
        db,
        Job(
            job_id=job_id,
            idempotency_key=None,
            caller_id="owner",
            repo=str(repo),
            base_ref="HEAD",
            base_sha=base,
            goal="fix typo in readme",
            acceptance=None,
            requirements=JobRequirements(role=JobRole.BUILD),
            priority=Priority.NORMAL,
            state=JobState.QUEUED,
            waiting_reason=WaitingReason.NEW,
            created_at=datetime.now(UTC).isoformat(),
            updated_at=datetime.now(UTC).isoformat(),
            request_digest="digest-" + job_id,
        ),
        now=datetime.now(UTC),
    )
    store.mark_dispatched(db, job_id, run_id, now=datetime.now(UTC))


def test_quota_exhausted_job_run_preempts_with_resume_capsule(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """Covers: trigger end to end (quota + no admitted fallback group).

    A durable job-owned run whose only fallback-group candidate reports
    genuine QUOTA_EXHAUSTED ends PREEMPTED (terminal for the run, not the
    job): the job returns to QUEUED/PREEMPTED with a verified checkpoint ref
    and a factual resume capsule, parked with explicit uncertainty because
    the reset is UNKNOWN.
    """
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    log: list[str] = []
    calls: list[str] = []
    worker = ScriptedWorker("h3", {"m1": _quota_exhausted_action("h3", "m1")}, log)
    monkeypatch.setattr(
        "saberops.orchestrator.resolve_candidate_chain",
        _role_keyed_chain([WorkerCandidate("h3", "m1")], [], calls),
    )
    db = Database(tmp_path / "orch.db")
    orch = Orchestrator(db=db, registry=AdapterRegistry([worker]), worktree_base=tmp_path / "wt")
    config = resolve_run_config(
        task="fix typo in readme",
        target_repo=repo,
        gate_command=_passing_gate(),
        publish_candidate=False,
    )
    # Own the run at mint time, mirroring production order (the scheduler
    # creates the job before the run exists; here the job is attached the
    # moment _run_core mints the run identity, before any quota signal).
    from saberops.provenance import mint_run_provenance as _real_mint

    _owned: list[str] = []

    def _mint_and_own(mint_repo: Any, mint_task: str) -> Any:
        provenance = _real_mint(mint_repo, mint_task)
        if not _owned:
            _owning_job(db, repo, base, "job1", provenance.run_id)
            _owned.append(provenance.run_id)
        return provenance

    monkeypatch.setattr(
        "saberops.orchestrator.mint_run_provenance", _mint_and_own
    )

    result = orch.run(config=config, is_peak=False)
    run_id = result.run_id

    assert result.status == RunStatus.PREEMPTED
    assert db.get_run(run_id) is not None
    assert db.get_run(run_id).status == RunStatus.PREEMPTED  # type: ignore[union-attr]
    job = store.get_job(db, "job1")
    assert job is not None
    assert job.state == JobState.QUEUED
    assert job.waiting_reason == WaitingReason.PREEMPTED
    assert job.preemptions == 1
    assert job.resume_from is not None
    assert job.resume_from.run_id == run_id
    # Unknown reset parks with explicit uncertainty: no fabricated ETA.
    assert job.not_before is None
    assert GitManager.get_ref_commit(
        repo, "refs/saberops/checkpoints/job1/0"
    ) == job.resume_from.checkpoint_sha
    capsule = db.get_handoff(job.resume_from.handoff_id)
    assert capsule is not None and capsule.run_id == run_id
    # The run's own ladder recorded genuine quota evidence: the exact
    # contract the coordinator consumes.  A single-candidate chain breaks
    # on exhaustion before a skip is recorded, so there is deliberately no
    # no_eligible_candidate event here -- every launched attempt dying of
    # quota IS the exhausted shape (see the coordinator's second rule).
    assert not _events_by_type(db.get_events(run_id), "no_eligible_candidate")
    assert _events_by_type(db.get_events(run_id), "quota_exhaustion_recorded")
    assert _events_by_type(db.get_events(run_id), "worker_interrupted")
    assert _events_by_type(db.get_events(run_id), "preemption_committed")
    assert _events_by_type(db.get_events(run_id), "run_preempted")


def test_quota_exhausted_multi_candidate_chain_records_no_eligible(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """Covers: multi-fallback shape records the ladder verdict.

    Every fallback-group candidate reports quota: the ladder records a
    ``no_eligible_candidate`` event (the coordinator's first rule) and the
    job-owned run preempts.
    """
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    log: list[str] = []
    calls: list[str] = []
    worker = ScriptedWorker(
        "h3",
        {
            "m1": _quota_exhausted_action("h3", "m1"),
            "m2": _quota_exhausted_action("h3", "m2"),
        },
        log,
    )
    monkeypatch.setattr(
        "saberops.orchestrator.resolve_candidate_chain",
        _role_keyed_chain(
            [WorkerCandidate("h3", "m1"), WorkerCandidate("h3", "m2")], [], calls
        ),
    )
    db = Database(tmp_path / "orch.db")
    orch = Orchestrator(db=db, registry=AdapterRegistry([worker]), worktree_base=tmp_path / "wt")
    config = resolve_run_config(
        task="fix typo in readme",
        target_repo=repo,
        gate_command=_passing_gate(),
        publish_candidate=False,
    )
    from saberops.provenance import mint_run_provenance as _real_mint

    _owned: list[str] = []

    def _mint_and_own(mint_repo: Any, mint_task: str) -> Any:
        provenance = _real_mint(mint_repo, mint_task)
        if not _owned:
            _owning_job(db, repo, base, "job1", provenance.run_id)
            _owned.append(provenance.run_id)
        return provenance

    monkeypatch.setattr(
        "saberops.orchestrator.mint_run_provenance", _mint_and_own
    )

    result = orch.run(config=config, is_peak=False)

    assert result.status == RunStatus.PREEMPTED
    assert _events_by_type(db.get_events(result.run_id), "no_eligible_candidate")
    job = store.get_job(db, "job1")
    assert job is not None and job.preemptions == 1
    assert job.resume_from is not None


def test_mixed_quota_gate_failure_and_skip_chain_does_not_preempt(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """Covers: the real 3-candidate mixed-chain ladder shape.

    Chain ``[A/h3/m1 -> QUOTA_EXHAUSTED]`` then ``[B/h4/m2 -> admitted, gate
    FAILED]`` then ``[C/h3/m3 -> skipped as quota-blocked]``.  The production
    ladder records a durable ``no_eligible_candidate`` event (it both launched
    and skipped candidates) and a genuine quota attempt, but B was admitted and
    failed the deterministic gate.  The run's true verdict is a gate failure:
    the job-owned run must stay FAILED (never PREEMPTED) and the job must not
    be requeued.
    """
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    # The module fixture only verifies ``h3``; this regression needs a second,
    # independently admitted provider so candidate B can actually be launched.
    # Scope an isolated owner config (h3 + h4) to this test only.
    cfg = tmp_path / "xdg_cfg"
    _write_synthetic_verified_postures(cfg)
    cp_path = cfg / "saberops" / "control_plane.json"
    cp_doc = json.loads(cp_path.read_text(encoding="utf-8"))
    cp_doc["providers"]["h4"] = {"state": "ALLOWED"}
    cp_doc["postures"]["h4"] = {
        "class": "A",
        "policy_urls": ["https://example.com/h4-policy"],
        "ttl_days": 90,
        "verified_on": datetime.now(UTC).date().isoformat(),
        "note": "synthetic verified posture for the mixed-chain F1 regression",
    }
    cp_path.write_text(json.dumps(cp_doc), encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))
    monkeypatch.setenv("XDG_STATE_HOME", str(cfg))
    log: list[str] = []
    calls: list[str] = []
    worker_h3 = ScriptedWorker("h3", {"m1": _quota_exhausted_action("h3", "m1")}, log)
    worker_h4 = ScriptedWorker(
        "h4", {"m2": _success_with_file("h4", "m2", "candidate.txt", "x\n")}, log
    )
    monkeypatch.setattr(
        "saberops.orchestrator.resolve_candidate_chain",
        _role_keyed_chain(
            [
                WorkerCandidate("h3", "m1"),
                WorkerCandidate("h4", "m2"),
                WorkerCandidate("h3", "m3"),
            ],
            [],
            calls,
        ),
    )
    db = Database(tmp_path / "orch.db")
    orch = Orchestrator(
        db=db,
        registry=AdapterRegistry([worker_h3, worker_h4]),
        worktree_base=tmp_path / "wt",
    )
    config = resolve_run_config(
        task="fix typo in readme",
        target_repo=repo,
        # B writes candidate.txt but never ok.txt, so the deterministic gate
        # fails on an admitted candidate (a real GATE_FAILURE, not quota).
        gate_command=_ok_gate(),
        publish_candidate=False,
    )
    from saberops.provenance import mint_run_provenance as _real_mint

    _owned: list[str] = []

    def _mint_and_own(mint_repo: Any, mint_task: str) -> Any:
        provenance = _real_mint(mint_repo, mint_task)
        if not _owned:
            _owning_job(db, repo, base, "job1", provenance.run_id)
            _owned.append(provenance.run_id)
        return provenance

    monkeypatch.setattr("saberops.orchestrator.mint_run_provenance", _mint_and_own)

    result = orch.run(config=config, is_peak=False)
    run_id = result.run_id

    # The production ladder really produced the F1 shape: mixed-chain evidence
    # plus an admitted, gate-failing candidate.
    assert _events_by_type(db.get_events(run_id), "no_eligible_candidate")
    assert _events_by_type(db.get_events(run_id), "quota_exhaustion_recorded")
    assert _events_by_type(db.get_events(run_id), "gate_failed")
    attempts = db.get_attempts_for_run(run_id)
    quota = WorkerInterruptionReason.QUOTA_EXHAUSTED.value
    assert any(a.interruption_reason == quota for a in attempts)
    assert any(
        a.interruption_reason != quota and a.status == AttemptStatus.FAILED
        for a in attempts
    )

    # F1: the truthful FAILED outcome must remain -- no preemption, no requeue.
    assert result.status == RunStatus.FAILED
    assert db.get_run(run_id) is not None
    assert db.get_run(run_id).status == RunStatus.FAILED  # type: ignore[union-attr]
    job = store.get_job(db, "job1")
    assert job is not None
    assert job.state == JobState.RUNNING
    assert job.waiting_reason != WaitingReason.PREEMPTED
    assert job.preemptions == 0
    assert job.resume_from is None
    assert not _events_by_type(db.get_events(run_id), "run_preempted")
    assert not _events_by_type(db.get_events(run_id), "preemption_committed")
    assert GitManager.list_checkpoint_refs(repo, "job1") == {}


def test_d24_pinned_dispatch_keeps_quota_skipped_routes_visible(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """A scheduler pin must not erase the routes that were considered."""
    monkeypatch.setenv("SABEROPS_GATE_MEMORY_GUARD", "0")
    from saberops.control_plane.policy import set_quota_pool_policy

    cfg_dir = Path(os.environ["XDG_CONFIG_HOME"]) / "saberops"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    today_iso = datetime.now(UTC).date().isoformat()
    (cfg_dir / "control_plane.json").write_text(
        json.dumps(
            {
                "version": 2,
                "providers": {
                    "h3": {"state": "ALLOWED"},
                    "blocked-provider": {"state": "ALLOWED"},
                },
                "quota_pools": {},
                "postures": {
                    "h3": {
                        "class": "A",
                        "policy_urls": ["https://example.com/h3-policy"],
                        "ttl_days": 90,
                        "verified_on": today_iso,
                        "note": "synthetic verified posture",
                    },
                    "blocked-provider": {
                        "class": "A",
                        "policy_urls": ["https://example.com/blocked-policy"],
                        "ttl_days": 90,
                        "verified_on": today_iso,
                        "note": "synthetic verified posture",
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    repo = tmp_path / "d24" / "repo"
    _init_repo(repo)
    set_quota_pool_policy(
        "blocked-pool", members=("blocked-provider/model-b",)
    )
    db = Database(tmp_path / "d24" / "orch.db")
    db.set_quota("blocked-provider", "blocked-pool", 0.0)
    chain = [
        WorkerCandidate("blocked-provider", "model-b"),
        WorkerCandidate("h3", "good-model"),
    ]

    def resolve(
        *,
        model_override: str | None = None,
        provider_override: str | None = None,
        **_: Any,
    ) -> list[WorkerCandidate]:
        if model_override:
            return [cand for cand in chain if cand.model == model_override]
        return list(chain)

    monkeypatch.setattr(
        "saberops.orchestrator.resolve_candidate_chain", resolve
    )
    log: list[str] = []
    worker = ScriptedWorker(
        "h3", {"good-model": _success_with_file("h3", "good-model", "ok.txt", "ok\n")}, log
    )
    config = resolve_run_config(
        task="pin evidence",
        target_repo=repo,
        gate_command=_passing_gate(),
        model_override="good-model",
        provider_override="h3",
        review_disabled=True,
        publish_candidate=False,
    )
    orch = Orchestrator(
        db=db,
        registry=AdapterRegistry(
            [worker, ScriptedWorker("blocked-provider", {}, log)]
        ),
        worktree_base=tmp_path / "d24" / "wt",
    )
    result = orch.run(config=config, is_peak=False)
    assert result.status is RunStatus.COMPLETED, result.summary
    resolved = _events_by_type(db.get_events(result.run_id), "tier_candidates_resolved")
    assert resolved and resolved[0].payload is not None
    payload = resolved[0].payload
    models = [str(c["model"]) for c in payload["candidates"]]
    assert models == ["model-b", "good-model"]
    decisions = {
        str(item["model"]): item for item in payload["candidate_decisions"]
    }
    blocked = decisions["model-b"]
    assert blocked["eligible"] is False
    assert blocked["reason"] == "quota_blocked"
    assert blocked["pool_id"] == "blocked-pool"
    assert decisions["good-model"]["eligible"] is True, decisions["good-model"]


def test_quota_exhausted_standalone_run_keeps_failed_behavior(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """Covers: standalone runs keep truthful failure (keep-compat)."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    log: list[str] = []
    calls: list[str] = []
    worker = ScriptedWorker("h3", {"m1": _quota_exhausted_action("h3", "m1")}, log)
    monkeypatch.setattr(
        "saberops.orchestrator.resolve_candidate_chain",
        _role_keyed_chain([WorkerCandidate("h3", "m1")], [], calls),
    )
    db = Database(tmp_path / "orch.db")
    orch = Orchestrator(db=db, registry=AdapterRegistry([worker]), worktree_base=tmp_path / "wt")
    config = resolve_run_config(
        task="fix typo in readme",
        target_repo=repo,
        gate_command=_passing_gate(),
        publish_candidate=False,
    )
    result = orch.run(config=config, is_peak=False)
    assert result.status == RunStatus.FAILED
    assert db.get_run(result.run_id) is not None
    assert db.get_run(result.run_id).status == RunStatus.FAILED  # type: ignore[union-attr]
