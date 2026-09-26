"""R3-E1.1: exact REVIEWER dispatch authority on the production path.

The owner-selected exact REVIEWER binding must govern every production
semantic-review dispatch -- not just the service/manual seam -- and the
exact binding must be re-resolved at the final reviewer launch boundary.
Hermetic and offline: no provider, model, credential, or network access.
"""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from saberops.config import resolve_run_config
from saberops.control_plane import (
    BindingRole,
    OwnerBindings,
    load_owner_bindings,
    materialize_discovered_binding,
    save_owner_bindings,
    upsert_owner_binding,
)
from saberops.db import Database
from saberops.models import (
    Attempt,
    AttemptStatus,
    DispatchStage,
    ReviewVerdict,
    Run,
    RunStatus,
    Tier,
    WorkerCandidate,
    WorkerRequest,
    WorkerResult,
)
from saberops.orchestrator import Orchestrator
from saberops.review import ReviewEngine, select_reviewer_ladder_unfiltered
from saberops.web import create_app
from saberops.workers.base import WorkerAdapter
from saberops.workers.registry import AdapterRegistry

_R3E1_CREATED = "2026-09-25T00:00:00+00:00"
_R3E1_PROVIDER = "opencode"
_R3E1_WORKER_MODEL = "r3e1-worker-model"
_R3E1_REVIEWER_MODEL = "r3e1-reviewer-model"
_R3E1_CONNECTION = "r3e1-conn"


@pytest.fixture()
def isolated_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "state"
    config = tmp_path / "config"
    cache = tmp_path / "cache"
    state.mkdir(parents=True)
    config.mkdir(parents=True)
    cache.mkdir(parents=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    monkeypatch.delenv("ORCH_BINDING_STORE", raising=False)
    monkeypatch.delenv("SABEROPS_PROFILE", raising=False)


class _R3E1ScriptedAdapter(WorkerAdapter):
    """Hermetic adapter: the implementer writes a file, the reviewer passes."""

    def __init__(
        self, call_log: list[str], env_log: list[dict[str, str]]
    ) -> None:
        self._call_log = call_log
        self._env_log = env_log

    @property
    def provider_name(self) -> str:
        return _R3E1_PROVIDER

    def is_available(self) -> bool:
        return True

    def run(self, request: WorkerRequest) -> WorkerResult:
        if request.read_only:
            self._call_log.append(f"review:{request.model}")
            self._env_log.append(dict(request.env) if request.env else {})
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
        target = request.worktree_path / "r3e1.txt"
        target.write_text("r3e1 candidate\n", encoding="utf-8")
        return WorkerResult(
            provider=self.provider_name,
            model=request.model,
            status=AttemptStatus.SUCCESS,
            summary="Wrote r3e1.txt",
            stdout="",
            stderr="",
            changed_paths=["r3e1.txt"],
            has_changes=True,
        )


def _patch_implementer_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin worker routing to the single R3-E1.1 implementer candidate."""

    def _resolve(*, tier: Tier, **_: Any) -> list[WorkerCandidate]:
        return [WorkerCandidate(_R3E1_PROVIDER, _R3E1_WORKER_MODEL)]

    monkeypatch.setattr(
        "saberops.orchestrator.resolve_candidate_chain", _resolve
    )


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True
    )
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "Makefile").write_text("gate:\n\t@echo ok\n", encoding="utf-8")
    (path / "README.md").write_text("# test\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"], cwd=path, check=True, capture_output=True
    )
    return path


def _seed_configured_reviewer() -> str:
    """Seed one REVIEWER binding and select it; return its binding id."""
    reviewer, profile, pool = materialize_discovered_binding(
        connection_id=_R3E1_CONNECTION,
        provider=_R3E1_PROVIDER,
        model=_R3E1_REVIEWER_MODEL,
        binding_role=BindingRole.REVIEWER,
    )
    owner = upsert_owner_binding(
        OwnerBindings(), binding=reviewer, profile=profile, pool=pool
    )
    save_owner_bindings(replace(owner, reviewer_binding_id=reviewer.binding_id))
    return reviewer.binding_id


def _seed_reviewable_run(db: Database, repo: Path, run_id: str) -> Run:
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    run = Run(
        id=run_id,
        task="r3e1 dispatch authority probe",
        target_repo=str(repo),
        base_commit=base,
        tier=Tier.T1,
        training_allowed=True,
        gate_command="true",
        status=RunStatus.RUNNING,
        created_at=_R3E1_CREATED,
    )
    db.create_run(run)
    db.create_attempt(
        Attempt(
            id=f"{run_id}-a1",
            run_id=run_id,
            attempt_number=1,
            provider=_R3E1_PROVIDER,
            model=_R3E1_WORKER_MODEL,
            worktree_path=str(repo),
            branch_name="main",
            status=AttemptStatus.SUCCESS,
            commit_sha=base,
            created_at=_R3E1_CREATED,
        )
    )
    return run


def _legacy_head_model() -> str:
    """Return the model the legacy ladder would launch first, from the real seam."""
    head = select_reviewer_ladder_unfiltered(
        _R3E1_PROVIDER, _R3E1_WORKER_MODEL, Tier.T1
    )[0]
    assert head.provider == _R3E1_PROVIDER
    assert head.model != _R3E1_REVIEWER_MODEL
    assert head.binding_id is None
    return head.model


def _run_production(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    review_mode: str,
    subdir: str,
) -> tuple[Any, list[str], list[dict[str, str]], Database]:
    """Drive the real in-run review lifecycle; return result, logs, db."""
    repo = _git_repo(tmp_path / subdir / "repo")
    call_log: list[str] = []
    env_log: list[dict[str, str]] = []
    _patch_implementer_chain(monkeypatch)
    db = Database(tmp_path / subdir / "orch.db")
    orch = Orchestrator(
        db=db,
        registry=AdapterRegistry([_R3E1ScriptedAdapter(call_log, env_log)]),
        worktree_base=tmp_path / subdir / "wt",
    )
    config = resolve_run_config(
        task="r3e1 probe",
        target_repo=repo,
        gate_command="true",
        review=True,
        review_mode=review_mode,
        publish_candidate=False,
    )
    result = orch.run(config=config, is_peak=False)
    return result, call_log, env_log, db


# --- production review loops -------------------------------------------------


def test_production_bounded_review_launches_configured_reviewer(
    isolated_xdg: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bounded in-run review launches B, never the legacy head A."""
    reviewer_id = _seed_configured_reviewer()
    legacy_a = _legacy_head_model()
    result, call_log, env_log, db = _run_production(
        tmp_path, monkeypatch, review_mode="bounded", subdir="bounded"
    )
    assert result.passed is True
    assert f"review:{_R3E1_REVIEWER_MODEL}" in call_log
    assert f"review:{legacy_a}" not in call_log
    reviews = db.get_reviews_for_run(result.run_id)
    assert reviews
    assert reviews[-1].provider == _R3E1_PROVIDER
    assert reviews[-1].model == _R3E1_REVIEWER_MODEL
    evidence = [
        e
        for e in db.get_dispatch_evidence_for_run(result.run_id)
        if e.stage == DispatchStage.REVIEW
    ]
    assert evidence
    assert all(e.binding_id == reviewer_id for e in evidence)
    assert env_log
    assert all(
        env.get("ORCH_EXECUTION_BINDING_ID") == reviewer_id for env in env_log
    )


def test_production_max_review_launches_configured_reviewer(
    isolated_xdg: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MAX in-run review launches B, never the legacy head A."""
    reviewer_id = _seed_configured_reviewer()
    legacy_a = _legacy_head_model()
    result, call_log, env_log, db = _run_production(
        tmp_path, monkeypatch, review_mode="max", subdir="max"
    )
    assert result.passed is True
    assert f"review:{_R3E1_REVIEWER_MODEL}" in call_log
    assert f"review:{legacy_a}" not in call_log
    evidence = [
        e
        for e in db.get_dispatch_evidence_for_run(result.run_id)
        if e.stage == DispatchStage.REVIEW
    ]
    assert evidence
    assert all(e.binding_id == reviewer_id for e in evidence)


def test_production_unconfigured_review_uses_legacy_ladder(
    isolated_xdg: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control: without a configured Reviewer the legacy head A launches."""
    legacy_a = _legacy_head_model()
    result, call_log, _env_log, _db = _run_production(
        tmp_path, monkeypatch, review_mode="bounded", subdir="legacy"
    )
    assert result.passed is True
    assert f"review:{legacy_a}" in call_log
    assert f"review:{_R3E1_REVIEWER_MODEL}" not in call_log


def test_production_review_with_invalid_reviewer_fails_closed(
    isolated_xdg: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured-but-missing Reviewer fails the run with zero review launch."""
    _seed_configured_reviewer()
    owner = load_owner_bindings()
    save_owner_bindings(
        replace(
            owner,
            reviewer_binding_id=f"discovered-binding:reviewer:{_R3E1_CONNECTION}:missing",
        )
    )
    repo = _git_repo(tmp_path / "invalid" / "repo")
    call_log: list[str] = []
    env_log: list[dict[str, str]] = []
    _patch_implementer_chain(monkeypatch)
    db = Database(tmp_path / "invalid" / "orch.db")
    orch = Orchestrator(
        db=db,
        registry=AdapterRegistry([_R3E1ScriptedAdapter(call_log, env_log)]),
        worktree_base=tmp_path / "invalid" / "wt",
    )
    config = resolve_run_config(
        task="r3e1 probe",
        target_repo=repo,
        gate_command="true",
        review=True,
        publish_candidate=False,
    )
    # The run-level boundary terminalizes the refusal as a failed run:
    # no exception escapes, no legacy reviewer is substituted.
    result = orch.run(config=config, is_peak=False)
    assert result.passed is False
    assert "REVIEWER_BINDING_NOT_FOUND" in (result.summary or "")
    assert [entry for entry in call_log if entry.startswith("review:")] == []
    assert db.get_reviews_for_run(result.run_id) == []
    assert [
        e
        for e in db.get_dispatch_evidence_for_run(result.run_id)
        if e.stage == DispatchStage.REVIEW
    ] == []
    assert (
        db.get_events_by_type(result.run_id, "review_dispatch_started") == []
    )
    refusals = db.get_events_by_type(result.run_id, "reviewer_binding_unresolvable")
    assert len(refusals) == 1
    assert refusals[0].payload is not None
    assert refusals[0].payload["reason"] == "REVIEWER_BINDING_NOT_FOUND"


# --- launch-boundary refusal --------------------------------------------------


def _reviewer_pair() -> tuple[str, str, str]:
    """Seed REVIEWER bindings for two models plus a WORKER twin; return ids."""
    owner = OwnerBindings()
    first, first_profile, first_pool = materialize_discovered_binding(
        connection_id=_R3E1_CONNECTION,
        provider=_R3E1_PROVIDER,
        model="r3e1-first-model",
        binding_role=BindingRole.REVIEWER,
    )
    owner = upsert_owner_binding(
        owner, binding=first, profile=first_profile, pool=first_pool
    )
    second, second_profile, second_pool = materialize_discovered_binding(
        connection_id=_R3E1_CONNECTION,
        provider=_R3E1_PROVIDER,
        model="r3e1-second-model",
        binding_role=BindingRole.REVIEWER,
    )
    owner = upsert_owner_binding(
        owner, binding=second, profile=second_profile, pool=second_pool
    )
    twin, twin_profile, twin_pool = materialize_discovered_binding(
        connection_id=_R3E1_CONNECTION,
        provider=_R3E1_PROVIDER,
        model="r3e1-first-model",
        binding_role=BindingRole.WORKER,
    )
    owner = upsert_owner_binding(
        owner, binding=twin, profile=twin_profile, pool=twin_pool
    )
    save_owner_bindings(owner)
    return first.binding_id, second.binding_id, twin.binding_id


@pytest.mark.parametrize(
    ("candidate_kwargs", "expected_reason"),
    [
        (
            {
                "provider": _R3E1_PROVIDER,
                "model": "r3e1-first-model",
                "binding_id": "discovered-binding:reviewer:r3e1-conn:ghost",
            },
            "UNKNOWN_BINDING",
        ),
        (
            {
                "provider": _R3E1_PROVIDER,
                "model": "r3e1-first-model",
                "binding_id": "twin",
            },
            "WRONG_ROLE",
        ),
        (
            {
                "provider": "other-provider",
                "model": "r3e1-first-model",
                "binding_id": "first",
            },
            "PROVIDER_MODEL_MISMATCH",
        ),
        (
            {
                "provider": _R3E1_PROVIDER,
                "model": "r3e1-second-model",
                "binding_id": "first",
            },
            "PROVIDER_MODEL_MISMATCH",
        ),
    ],
)
def test_bound_reviewer_candidate_refusal_cases(
    isolated_xdg: None,
    tmp_path: Path,
    candidate_kwargs: dict[str, str],
    expected_reason: str,
) -> None:
    """Bound candidates refuse with zero launch and no sibling substitution."""
    first_id, _second_id, twin_id = _reviewer_pair()
    resolved_kwargs = {
        key: (
            {"twin": twin_id, "first": first_id}[value]
            if key == "binding_id" and value in ("twin", "first")
            else value
        )
        for key, value in candidate_kwargs.items()
    }
    repo = _git_repo(tmp_path / "repo")
    db = Database(tmp_path / "review.db")
    _seed_reviewable_run(db, repo, "run_refuse")
    call_log: list[str] = []
    env_log: list[dict[str, str]] = []
    engine = ReviewEngine(
        db=db,
        registry=AdapterRegistry([_R3E1ScriptedAdapter(call_log, env_log)]),
        readiness_service=None,
    )
    result = engine.review_run(
        "run_refuse", reviewer_override=WorkerCandidate(**resolved_kwargs)
    )
    assert result.verdict is ReviewVerdict.REVIEW_UNAVAILABLE
    assert result.provider == ""
    assert result.model == ""
    assert call_log == []
    assert env_log == []
    refusals = db.get_events_by_type("run_refuse", "reviewer_binding_unresolvable")
    assert len(refusals) == 1
    assert refusals[0].payload is not None
    assert refusals[0].payload["reason"] == expected_reason
    assert refusals[0].payload["binding_id"] == resolved_kwargs["binding_id"]
    assert db.get_dispatch_evidence_for_run("run_refuse") == []


def test_bound_reviewer_candidate_empty_registry_refuses(
    isolated_xdg: None, tmp_path: Path
) -> None:
    """A bound candidate with no owner registry refuses (NO_REGISTRY)."""
    repo = _git_repo(tmp_path / "repo")
    db = Database(tmp_path / "review.db")
    _seed_reviewable_run(db, repo, "run_noregistry")
    call_log: list[str] = []
    env_log: list[dict[str, str]] = []
    engine = ReviewEngine(
        db=db,
        registry=AdapterRegistry([_R3E1ScriptedAdapter(call_log, env_log)]),
        readiness_service=None,
    )
    result = engine.review_run(
        "run_noregistry",
        reviewer_override=WorkerCandidate(
            provider=_R3E1_PROVIDER,
            model="r3e1-first-model",
            binding_id="discovered-binding:reviewer:r3e1-conn:first-model",
        ),
    )
    assert result.verdict is ReviewVerdict.REVIEW_UNAVAILABLE
    assert call_log == []
    refusals = db.get_events_by_type("run_noregistry", "reviewer_binding_unresolvable")
    assert len(refusals) == 1
    assert refusals[0].payload is not None
    assert refusals[0].payload["reason"] == "NO_REGISTRY"


def test_review_dispatch_evidence_names_exact_reviewer_binding(
    isolated_xdg: None, tmp_path: Path
) -> None:
    """A launched bound reviewer records its exact binding/profile/pool identity."""
    reviewer_id = _seed_configured_reviewer()
    owner = load_owner_bindings()
    binding = owner.registry.try_get(reviewer_id)
    assert binding is not None
    repo = _git_repo(tmp_path / "repo")
    db = Database(tmp_path / "review.db")
    _seed_reviewable_run(db, repo, "run_evidence")
    call_log: list[str] = []
    env_log: list[dict[str, str]] = []
    engine = ReviewEngine(
        db=db,
        registry=AdapterRegistry([_R3E1ScriptedAdapter(call_log, env_log)]),
        readiness_service=None,
    )
    result = engine.review_run(
        "run_evidence",
        reviewer_override=WorkerCandidate(
            provider=_R3E1_PROVIDER,
            model=_R3E1_REVIEWER_MODEL,
            binding_id=reviewer_id,
        ),
    )
    assert result.verdict is ReviewVerdict.PASS
    assert call_log == [f"review:{_R3E1_REVIEWER_MODEL}"]
    evidence = db.get_dispatch_evidence_for_run("run_evidence")
    assert len(evidence) == 1
    assert evidence[0].binding_id == reviewer_id
    assert evidence[0].profile_id == binding.profile_id
    assert evidence[0].quota_pool_id == binding.quota_pool_id
    assert len(env_log) == 1
    assert env_log[0].get("ORCH_EXECUTION_BINDING_ID") == reviewer_id
    assert env_log[0].get("ORCH_EXECUTION_PROFILE_ID") == binding.profile_id
    assert env_log[0].get("ORCH_EXECUTION_POOL_ID") == binding.quota_pool_id


# --- web consistency ----------------------------------------------------------


def test_reviewer_surfaces_use_injected_registry(
    isolated_xdg: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reviewer GET/POST resolve through the app registry, never a fresh default."""
    reviewer_id = _seed_configured_reviewer()
    owner = load_owner_bindings()
    save_owner_bindings(replace(owner, reviewer_binding_id=None))

    def _empty_default(cls: Any) -> AdapterRegistry:
        return AdapterRegistry(adapters=[])

    monkeypatch.setattr(AdapterRegistry, "default", classmethod(_empty_default))
    injected = AdapterRegistry(
        [_R3E1ScriptedAdapter(call_log=[], env_log=[])]
    )
    client = TestClient(create_app(db_path=tmp_path / "web.db", registry=injected))
    page = client.get("/reviewer")
    assert page.status_code == 200
    assert reviewer_id in page.text
    response = client.post(
        "/reviewer/select", data={"binding_id": reviewer_id}, follow_redirects=False
    )
    assert response.status_code == 303
    assert load_owner_bindings().reviewer_binding_id == reviewer_id
