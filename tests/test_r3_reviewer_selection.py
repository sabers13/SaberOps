"""R3-E1: owner-selected exact Reviewer model.

The owner chooses one exact REVIEWER-role binding from discovered
executable models through the UI, and real review execution honors
that exact selection.  Hermetic and offline: no provider, model,
credential, or network access.

Covers the bounded contract:

* discovery materializes WORKER + REVIEWER + ORCHESTRATOR bindings for
  one model, sharing profile/quota pool with role-distinct ids;
* the Reviewer UI lists only REVIEWER bindings as selectable;
* selecting a REVIEWER binding persists across restart/reload;
* WORKER/ORCHESTRATOR bindings submitted as reviewer are rejected;
* a configured reviewer becomes a WorkerCandidate with the exact
  binding id and reaches the real review dispatch;
* a configured-but-invalid reviewer fails closed (never falls back);
* no configured reviewer preserves the legacy ladder;
* a selected reviewer cannot silently self-review the final implementer;
* /setup stays incomplete without a Reviewer selection and becomes
  reviewer-ready after a valid one;
* existing Orchestrator and Worker selections are unchanged.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from saberops.control_plane import (
    BindingRole,
    OwnerBindings,
    ReviewerSelectionError,
    ReviewerSelectionStatus,
    load_owner_bindings,
    materialize_discovered_binding,
    reviewer_candidate_for_dispatch,
    save_owner_bindings,
    select_reviewer_binding,
    upsert_owner_binding,
)
from saberops.control_plane.orch_binding import OrchBindingPolicy
from saberops.db import Database
from saberops.models import (
    Attempt,
    AttemptStatus,
    OrchBindingMode,
    ReviewResult,
    ReviewVerdict,
    Run,
    RunStatus,
    Tier,
    WorkerCandidate,
)
from saberops.projects import ProjectRegistry
from saberops.review import ReviewEngine
from saberops.routing_config import (
    DynamicCandidate,
    add_dynamic_candidate_to_chain,
    format_candidate_id,
    load_effective_routing_payload,
    save_user_routing,
)
from saberops.service import OrchestratorService
from saberops.web import create_app
from saberops.workers.registry import AdapterRegistry

_CREATED = "2026-09-25T00:00:00+00:00"
_CONNECTION = "opencode-account"
_PROVIDER = "opencode"
_MODEL = "r3-reviewer-test-model"


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


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(db_path=tmp_path / "web.db"))


def _seed_three_role_bindings() -> tuple[str, str, str]:
    """Seed WORKER + REVIEWER + ORCHESTRATOR bindings for one model.

    Returns ``(worker_id, reviewer_id, orchestrator_id)``.  All three
    share the same connection-scoped profile and quota pool; only the
    binding ids carry the semantic role.
    """
    owner = OwnerBindings()
    worker, profile, pool = materialize_discovered_binding(
        connection_id=_CONNECTION, provider=_PROVIDER, model=_MODEL,
        binding_role=BindingRole.WORKER,
    )
    reviewer, reviewer_profile, reviewer_pool = materialize_discovered_binding(
        connection_id=_CONNECTION, provider=_PROVIDER, model=_MODEL,
        binding_role=BindingRole.REVIEWER,
    )
    orch, orch_profile, orch_pool = materialize_discovered_binding(
        connection_id=_CONNECTION, provider=_PROVIDER, model=_MODEL,
        binding_role=BindingRole.ORCHESTRATOR,
    )
    assert reviewer_profile.profile_id == profile.profile_id
    assert reviewer_pool.pool_id == pool.pool_id
    assert orch_profile.profile_id == profile.profile_id
    assert orch_pool.pool_id == pool.pool_id
    owner = upsert_owner_binding(owner, binding=worker, profile=profile, pool=pool)
    owner = upsert_owner_binding(
        owner, binding=reviewer, profile=reviewer_profile, pool=reviewer_pool
    )
    owner = upsert_owner_binding(owner, binding=orch, profile=orch_profile, pool=orch_pool)
    save_owner_bindings(owner)
    return worker.binding_id, reviewer.binding_id, orch.binding_id


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


def _seed_reviewable_run(db: Database, repo: Path, run_id: str) -> Run:
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    run = Run(
        id=run_id,
        task="reviewer selection probe",
        target_repo=str(repo),
        base_commit=base,
        tier=Tier.T1,
        training_allowed=True,
        gate_command="true",
        status=RunStatus.RUNNING,
        created_at=_CREATED,
    )
    db.create_run(run)
    db.create_attempt(
        Attempt(
            id=f"{run_id}-a1",
            run_id=run_id,
            attempt_number=1,
            provider=_PROVIDER,
            model=_MODEL,
            worktree_path=str(repo),
            branch_name="main",
            status=AttemptStatus.SUCCESS,
            commit_sha=base,
            created_at=_CREATED,
        )
    )
    return run


# --- discovery ------------------------------------------------------------


def test_discovery_materializes_three_role_bindings_sharing_resources(
    isolated_xdg: None,
) -> None:
    """One executable model yields WORKER + REVIEWER + ORCHESTRATOR bindings."""
    worker_id, reviewer_id, orch_id = _seed_three_role_bindings()
    assert worker_id == f"discovered-binding:worker:{_CONNECTION}:{_MODEL}"
    assert reviewer_id == f"discovered-binding:reviewer:{_CONNECTION}:{_MODEL}"
    assert orch_id == f"discovered-binding:orchestrator:{_CONNECTION}:{_MODEL}"
    assert len({worker_id, reviewer_id, orch_id}) == 3

    owner = load_owner_bindings()
    worker = owner.registry.try_get(worker_id)
    reviewer = owner.registry.try_get(reviewer_id)
    orch = owner.registry.try_get(orch_id)
    assert worker is not None and reviewer is not None and orch is not None
    assert worker.binding_role is BindingRole.WORKER
    assert reviewer.binding_role is BindingRole.REVIEWER
    assert orch.binding_role is BindingRole.ORCHESTRATOR
    # Same real account/model resource: shared profile and quota pool.
    assert reviewer.profile_id == worker.profile_id == orch.profile_id
    assert reviewer.quota_pool_id == worker.quota_pool_id == orch.quota_pool_id
    assert reviewer.provider == worker.provider == _PROVIDER
    assert reviewer.model == worker.model == _MODEL


def test_discovered_reviewer_binding_proves_through_canonical_resolver(
    isolated_xdg: None,
) -> None:
    """The REVIEWER binding is executable, not merely registered."""
    _, reviewer_id, _ = _seed_three_role_bindings()
    owner = load_owner_bindings()
    selection = select_reviewer_binding(
        replace(owner, reviewer_binding_id=reviewer_id),
        adapter_registry=AdapterRegistry.default(),
    )
    assert selection.status is ReviewerSelectionStatus.RESOLVED
    assert selection.binding is not None
    assert selection.binding.binding_id == reviewer_id


# --- persistence ----------------------------------------------------------


def test_reviewer_selection_absent_in_legacy_documents(
    isolated_xdg: None, tmp_path: Path
) -> None:
    """Owner files written before R3-E1 load with no reviewer configured."""
    store_path = tmp_path / "legacy-bindings.json"
    store_path.write_text(
        json.dumps({"schema_version": 1, "binding_registry": {}}), encoding="utf-8"
    )
    owner = load_owner_bindings(store_path)
    assert owner.reviewer_binding_id is None
    selection = select_reviewer_binding(owner, adapter_registry=AdapterRegistry.default())
    assert selection.status is ReviewerSelectionStatus.NOT_CONFIGURED


def test_reviewer_selection_persists_across_reload(isolated_xdg: None) -> None:
    """A selected REVIEWER binding survives a store reload."""
    _, reviewer_id, _ = _seed_three_role_bindings()
    owner = load_owner_bindings()
    save_owner_bindings(replace(owner, reviewer_binding_id=reviewer_id))
    reloaded = load_owner_bindings()
    assert reloaded.reviewer_binding_id == reviewer_id


# --- reviewer UI ----------------------------------------------------------


def test_reviewer_page_lists_only_reviewer_bindings(
    isolated_xdg: None, tmp_path: Path
) -> None:
    """Only REVIEWER-role bindings are selectable; role text never qualifies."""
    worker_id, reviewer_id, orch_id = _seed_three_role_bindings()
    page = _client(tmp_path).get("/reviewer")
    assert page.status_code == 200
    assert 'action="/reviewer/select"' in page.text
    assert reviewer_id in page.text
    assert worker_id not in page.text
    assert orch_id not in page.text


def test_select_reviewer_binding_persists(isolated_xdg: None, tmp_path: Path) -> None:
    """POST /reviewer/select stores the exact REVIEWER binding id."""
    _, reviewer_id, _ = _seed_three_role_bindings()
    client = _client(tmp_path)
    response = client.post(
        "/reviewer/select", data={"binding_id": reviewer_id}, follow_redirects=False
    )
    assert response.status_code == 303
    assert load_owner_bindings().reviewer_binding_id == reviewer_id
    page = client.get("/reviewer")
    assert page.status_code == 200
    assert "currently selected" in page.text
    assert reviewer_id in page.text


def test_worker_binding_submitted_as_reviewer_is_rejected(
    isolated_xdg: None, tmp_path: Path
) -> None:
    """A WORKER binding id is refused even though provider/model match."""
    worker_id, _, _ = _seed_three_role_bindings()
    client = _client(tmp_path)
    response = client.post("/reviewer/select", data={"binding_id": worker_id})
    assert response.status_code == 400
    assert load_owner_bindings().reviewer_binding_id is None


def test_orchestrator_binding_submitted_as_reviewer_is_rejected(
    isolated_xdg: None, tmp_path: Path
) -> None:
    """An ORCHESTRATOR binding id is refused even though provider/model match."""
    _, _, orch_id = _seed_three_role_bindings()
    client = _client(tmp_path)
    response = client.post("/reviewer/select", data={"binding_id": orch_id})
    assert response.status_code == 400
    assert load_owner_bindings().reviewer_binding_id is None


def test_unknown_binding_submitted_as_reviewer_is_rejected(
    isolated_xdg: None, tmp_path: Path
) -> None:
    """A binding id naming nothing in the registry is refused."""
    _seed_three_role_bindings()
    client = _client(tmp_path)
    response = client.post(
        "/reviewer/select",
        data={"binding_id": "discovered-binding:reviewer:nope:missing"},
    )
    assert response.status_code == 400
    assert load_owner_bindings().reviewer_binding_id is None


# --- candidate conversion -------------------------------------------------


def test_configured_reviewer_becomes_exact_worker_candidate(
    isolated_xdg: None,
) -> None:
    """The selection converts to a candidate with the exact binding id."""
    _, reviewer_id, _ = _seed_three_role_bindings()
    owner = replace(load_owner_bindings(), reviewer_binding_id=reviewer_id)
    candidate = reviewer_candidate_for_dispatch(
        owner, adapter_registry=AdapterRegistry.default()
    )
    assert candidate is not None
    assert candidate.provider == _PROVIDER
    assert candidate.model == _MODEL
    assert candidate.binding_id == reviewer_id


def test_unconfigured_reviewer_preserves_legacy_ladder(isolated_xdg: None) -> None:
    """No selection converts to ``None``: the legacy ladder stays available."""
    _seed_three_role_bindings()
    candidate = reviewer_candidate_for_dispatch(
        load_owner_bindings(), adapter_registry=AdapterRegistry.default()
    )
    assert candidate is None


# --- real execution -------------------------------------------------------


def _spy_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    """Replace ReviewEngine.review_run with a recorder; return the capture."""
    captured: dict[str, Any] = {}

    def _spy(
        self: ReviewEngine,
        run_id: str,
        reviewer_override: WorkerCandidate | None = None,
        timeout: float = 1200.0,
        review_limit: int | None = None,
    ) -> ReviewResult:
        captured["run_id"] = run_id
        captured["reviewer_override"] = reviewer_override
        captured["called"] = True
        return ReviewResult(
            id="rev_spy",
            run_id=run_id,
            provider=(reviewer_override.provider if reviewer_override else ""),
            model=(reviewer_override.model if reviewer_override else ""),
            verdict=ReviewVerdict.PASS,
            summary="spy",
            details="",
            created_at=_CREATED,
        )

    monkeypatch.setattr(ReviewEngine, "review_run", _spy)
    return captured


def test_service_review_run_uses_configured_reviewer(
    isolated_xdg: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SaberOpsService.review_run supplies the exact selected reviewer."""
    _, reviewer_id, _ = _seed_three_role_bindings()
    save_owner_bindings(
        replace(load_owner_bindings(), reviewer_binding_id=reviewer_id)
    )
    captured = _spy_engine(monkeypatch)
    service = OrchestratorService(
        db=Database(tmp_path / "svc.db"),
        registry=AdapterRegistry.default(),
        readiness_service=False,
    )
    service.review_run("run_r3e1")
    assert captured.get("called") is True
    override = captured.get("reviewer_override")
    assert isinstance(override, WorkerCandidate)
    assert override.provider == _PROVIDER
    assert override.model == _MODEL
    assert override.binding_id == reviewer_id


def test_service_review_run_without_reviewer_keeps_legacy_ladder(
    isolated_xdg: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No selection reaches the engine as ``None`` (legacy ladder)."""
    _seed_three_role_bindings()
    captured = _spy_engine(monkeypatch)
    service = OrchestratorService(
        db=Database(tmp_path / "svc.db"),
        registry=AdapterRegistry.default(),
        readiness_service=False,
    )
    service.review_run("run_r3e1")
    assert captured.get("called") is True
    assert captured.get("reviewer_override") is None


@pytest.mark.parametrize(
    "selection",
    [
        "discovered-binding:reviewer:opencode-account:missing-model",
        f"discovered-binding:worker:{_CONNECTION}:{_MODEL}",
        f"discovered-binding:orchestrator:{_CONNECTION}:{_MODEL}",
    ],
)
def test_service_review_run_with_invalid_reviewer_fails_closed(
    isolated_xdg: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    selection: str,
) -> None:
    """Missing/wrong-role selections refuse; engine never runs."""
    _seed_three_role_bindings()
    save_owner_bindings(replace(load_owner_bindings(), reviewer_binding_id=selection))
    captured = _spy_engine(monkeypatch)
    service = OrchestratorService(
        db=Database(tmp_path / "svc.db"),
        registry=AdapterRegistry.default(),
        readiness_service=False,
    )
    with pytest.raises(ReviewerSelectionError):
        service.review_run("run_r3e1")
    assert captured.get("called") is not True


def test_service_review_run_with_no_adapter_reviewer_fails_closed(
    isolated_xdg: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A REVIEWER binding with no registered adapter is unresolvable: refuse."""
    ghost, ghost_profile, ghost_pool = materialize_discovered_binding(
        connection_id="ghost-account",
        provider="ghost-provider",
        model="ghost-model",
        binding_role=BindingRole.REVIEWER,
    )
    owner = upsert_owner_binding(
        OwnerBindings(), binding=ghost, profile=ghost_profile, pool=ghost_pool
    )
    save_owner_bindings(replace(owner, reviewer_binding_id=ghost.binding_id))
    captured = _spy_engine(monkeypatch)
    service = OrchestratorService(
        db=Database(tmp_path / "svc.db"),
        registry=AdapterRegistry.default(),
        readiness_service=False,
    )
    with pytest.raises(ReviewerSelectionError):
        service.review_run("run_r3e1")
    assert captured.get("called") is not True


def test_engine_override_never_silently_falls_back(
    isolated_xdg: None, tmp_path: Path
) -> None:
    """An override that cannot launch yields UNAVAILABLE, not another reviewer."""
    repo = _git_repo(tmp_path / "repo")
    db = Database(tmp_path / "review.db")
    _seed_reviewable_run(db, repo, "run_nofallback")
    engine = ReviewEngine(
        db=db, registry=AdapterRegistry(adapters=[]), readiness_service=None
    )
    result = engine.review_run(
        "run_nofallback",
        reviewer_override=WorkerCandidate(
            provider="ghost-provider", model="ghost-model"
        ),
    )
    assert result.verdict is ReviewVerdict.REVIEW_UNAVAILABLE
    # No substantive verdict from a substituted reviewer.
    assert result.provider == ""
    assert result.model == ""


def test_configured_reviewer_cannot_silently_self_review(
    isolated_xdg: None, tmp_path: Path
) -> None:
    """An override matching the final implementer fails closed, not swapped."""
    repo = _git_repo(tmp_path / "repo")
    db = Database(tmp_path / "review.db")
    _seed_reviewable_run(db, repo, "run_selfreview")
    engine = ReviewEngine(
        db=db, registry=AdapterRegistry.default(), readiness_service=None
    )
    with pytest.raises(ValueError, match="self-review"):
        engine.review_run(
            "run_selfreview",
            reviewer_override=WorkerCandidate(provider=_PROVIDER, model=_MODEL),
        )
    assert db.get_reviews_for_run("run_selfreview") == []


# --- setup ----------------------------------------------------------------


def _seed_routing_chain() -> None:
    candidate_id = format_candidate_id(_PROVIDER, _MODEL)
    approval = DynamicCandidate(
        candidate_id=candidate_id,
        provider=_PROVIDER,
        model=_MODEL,
        connection_id=_CONNECTION,
        backend="opencode",
        binding_id=f"discovered-binding:worker:{_CONNECTION}:{_MODEL}",
        discovery_status="TRUE",
        execution_support="SUPPORTED",
        training_required="UNKNOWN",
        capabilities=(),
        approved_at=_CREATED,
    )
    save_user_routing(
        add_dynamic_candidate_to_chain(
            load_effective_routing_payload(),
            top="T1",
            sub="default",
            candidate=approval,
        )
    )


def _setup_client_with_project(tmp_path: Path) -> TestClient:
    repo = _git_repo(tmp_path / "repo")
    projects = ProjectRegistry(path=tmp_path / "projects.json")
    client = TestClient(
        create_app(projects=projects, db_path=tmp_path / "web.db")
    )
    selected = client.post(
        "/projects/select", data={"path": str(repo)}, follow_redirects=False
    )
    assert selected.status_code == 303
    return client


def test_setup_incomplete_without_reviewer_selection(
    isolated_xdg: None, tmp_path: Path
) -> None:
    """Worker + Orchestrator + project are not enough; reviewer gates READY."""
    _, _, orch_id = _seed_three_role_bindings()
    save_owner_bindings(
        replace(
            load_owner_bindings(),
            policy=OrchBindingPolicy(
                mode=OrchBindingMode.PINNED, pinned_binding_id=orch_id
            ),
        )
    )
    _seed_routing_chain()
    setup = _setup_client_with_project(tmp_path).get("/setup")
    assert setup.status_code == 200
    assert "SETUP INCOMPLETE" in setup.text
    assert "READY FOR DOGFOOD" not in setup.text


def test_setup_becomes_reviewer_ready_after_valid_selection(
    isolated_xdg: None, tmp_path: Path
) -> None:
    """A valid reviewer selection flips the reviewer step to ready."""
    _, reviewer_id, orch_id = _seed_three_role_bindings()
    save_owner_bindings(
        replace(
            load_owner_bindings(),
            policy=OrchBindingPolicy(
                mode=OrchBindingMode.PINNED, pinned_binding_id=orch_id
            ),
            reviewer_binding_id=reviewer_id,
        )
    )
    _seed_routing_chain()
    setup = _setup_client_with_project(tmp_path).get("/setup")
    assert setup.status_code == 200
    assert "RESOLVED" in setup.text
    assert reviewer_id in setup.text


def test_setup_ready_for_dogfood_requires_reviewer(
    isolated_xdg: None, tmp_path: Path
) -> None:
    """Full readiness is reachable and includes the reviewer step."""
    _, reviewer_id, orch_id = _seed_three_role_bindings()
    save_owner_bindings(
        replace(
            load_owner_bindings(),
            policy=OrchBindingPolicy(
                mode=OrchBindingMode.PINNED, pinned_binding_id=orch_id
            ),
            reviewer_binding_id=reviewer_id,
        )
    )
    _seed_routing_chain()
    setup = _setup_client_with_project(tmp_path).get("/setup")
    assert setup.status_code == 200
    assert "READY FOR DOGFOOD" in setup.text


# --- role separation ------------------------------------------------------


def test_orchestrator_and_worker_selections_unchanged(
    isolated_xdg: None, tmp_path: Path
) -> None:
    """Reviewer selection leaves Orchestrator policy and worker routing alone."""
    worker_id, reviewer_id, orch_id = _seed_three_role_bindings()
    save_owner_bindings(
        replace(
            load_owner_bindings(),
            policy=OrchBindingPolicy(
                mode=OrchBindingMode.PINNED, pinned_binding_id=orch_id
            ),
        )
    )
    _seed_routing_chain()
    client = _client(tmp_path)
    response = client.post(
        "/reviewer/select", data={"binding_id": reviewer_id}, follow_redirects=False
    )
    assert response.status_code == 303
    owner = load_owner_bindings()
    assert owner.reviewer_binding_id == reviewer_id
    assert owner.policy is not None
    assert owner.policy.pinned_binding_id == orch_id
    worker_binding = owner.registry.try_get(worker_id)
    assert worker_binding is not None
    assert worker_binding.binding_role is BindingRole.WORKER
    orch_page = client.get("/orchestrator")
    assert orch_page.status_code == 200
    assert orch_id in orch_page.text
    routing_page = client.get("/routing")
    assert routing_page.status_code == 200
    assert _MODEL in routing_page.text
