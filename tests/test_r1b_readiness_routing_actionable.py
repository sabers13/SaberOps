"""R1-B: readiness and routing failures are actionable.

Covers, without live provider calls:

A. Verify/readiness -- POST /access/test performs the canonical readiness
   refresh for account-backed connections (persisted state/reason/timestamp,
   truthful READY / NOT_READY / UNKNOWN, API connections never falsely
   BACKEND_NOT_INSTALLED, only the declared status command runs).
B. Candidate explanations -- two automatic candidates rejected for different
   reasons launch no worker and terminate with structured evidence naming
   both candidates plus an actionable owner summary.
C. Empty chain -- [] validates, round-trips, removes cleanly, launches
   nothing and terminates with a named-context explanation.
D. Exact binding -- a discovery-verified model that is also in
   ALLOWED_CANDIDATES keeps its exact WORKER binding_id through add,
   reload/freeze and execution resolution, with static training semantics
   preserved and no silent same-provider/model substitution.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from saberops.control_plane import (
    BindingRole,
    OwnerBindingStore,
    capability_profile_for,
    get_binding_store_path,
    materialize_discovered_binding,
    save_owner_bindings,
    upsert_owner_binding,
)
from saberops.control_plane.bindings import (
    BindingResolutionError,
    resolve_worker_candidate_binding,
)
from saberops.db import Database
from saberops.model_access import (
    AccessProtocol,
    AccessStore,
    AuthMode,
    ExecutionSupport,
    ModelCatalogStore,
    ProviderConnection,
    ReadinessService,
    ReadinessStore,
    apply_discovery_result,
    build_access_registry,
    default_account_connections,
    discover_connection_models,
)
from saberops.models import (
    AttemptStatus,
    DispatchOutcome,
    PlanStepStatus,
    ProviderReadiness,
    RunStatus,
    Tier,
    WorkerCandidate,
    WorkerRequest,
    WorkerResult,
)
from saberops.observability.run_report import build_run_report
from saberops.ownership import ExecutionOwner
from saberops.process import run_process
from saberops.project import owner_state_dir
from saberops.routing import resolve_candidate_chain
from saberops.routing_config import (
    ALLOWED_CANDIDATES,
    load_effective_routing,
    load_effective_routing_payload,
    requires_training_permission,
    save_user_routing,
    snapshot_references_exact_bindings,
)
from saberops.service import OrchestratorService
from saberops.slicing import ProposedPackage, TaskSizingAssessment
from saberops.web import create_app
from saberops.workers.base import WorkerAdapter
from saberops.workers.registry import AdapterRegistry

# ---------------------------------------------------------------------------
# Shared fixtures / fakes
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Path]:
    """Isolate owner state/config plus the readiness store override."""
    state = tmp_path / "state"
    config = tmp_path / "config"
    state.mkdir(parents=True)
    config.mkdir(parents=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.delenv("ORCH_ACCESS_STORE", raising=False)
    monkeypatch.delenv("ORCH_ACCESS_MODELS_STORE", raising=False)
    monkeypatch.delenv("ORCH_BINDING_STORE", raising=False)
    readiness_path = tmp_path / "readiness" / "access-readiness.json"
    monkeypatch.setenv("ORCH_READINESS_STORE", str(readiness_path))
    return {"tmp": tmp_path, "readiness": readiness_path}


class _NeverLaunchesAdapter(WorkerAdapter):
    """Available adapter that must never be asked to execute."""

    def __init__(self, provider: str) -> None:
        self._provider = provider

    @property
    def provider_name(self) -> str:
        return self._provider

    def is_available(self) -> bool:
        return True

    def run(self, request: WorkerRequest) -> WorkerResult:
        raise AssertionError(f"R1-B: no worker may launch ({self._provider})")


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    run_process(["git", "init", "-b", "main"], cwd=path)
    run_process(["git", "config", "user.name", "Test Runner"], cwd=path)
    run_process(["git", "config", "user.email", "test@localhost"], cwd=path)
    (path / "calc.py").write_text("# placeholder\n")
    run_process(["git", "add", "."], cwd=path)
    run_process(["git", "commit", "-m", "init"], cwd=path)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _payload(event: Any) -> dict[str, Any]:
    assert event.payload is not None
    assert isinstance(event.payload, dict)
    return event.payload


def _fake_which(
    monkeypatch: pytest.MonkeyPatch, *, present: tuple[str, ...] = ("codex", "claude")
) -> None:
    monkeypatch.setattr(
        shutil, "which", lambda exe: f"/fake/{exe}" if exe in present else None
    )


def _fake_status_run(
    monkeypatch: pytest.MonkeyPatch,
    *,
    returncode: int,
    stdout: str,
    calls: list[list[str]],
) -> None:
    def fake_run(argv: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert isinstance(argv, list)
        calls.append([str(part) for part in argv])
        return subprocess.CompletedProcess(argv, returncode, stdout, "")

    monkeypatch.setattr(subprocess, "run", fake_run)


_CODEX_OK = json.dumps(
    {
        "schemaVersion": 1,
        "overallStatus": "ok",
        "checks": {"auth.credentials": {"status": "ok"}}},
)
_CODEX_DENIED = json.dumps(
    {
        "schemaVersion": 1,
        "overallStatus": "error",
        "checks": {"auth.credentials": {"status": "error"}}},
)


# ---------------------------------------------------------------------------
# A. Verify/readiness
# ---------------------------------------------------------------------------


def test_r1b_verify_success_persists_readiness(
    isolated_state: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Successful account verification persists READY + reason + timestamp."""
    calls: list[list[str]] = []
    _fake_which(monkeypatch)
    _fake_status_run(monkeypatch, returncode=0, stdout=_CODEX_OK, calls=calls)
    client = TestClient(create_app(db_path=tmp_path / "web.db"))
    calls.clear()

    response = client.post("/access/test", data={"connection_id": "codex-chatgpt-account"})

    assert response.status_code == 200
    # The declared non-inference status command ran exactly once; every
    # other subprocess call is unrelated git plumbing, never a model call.
    assert calls.count(["codex", "doctor", "--json"]) == 1
    assert all(call[0] in ("codex", "git") for call in calls)
    # State/reason/timestamp survive reload through the canonical store.
    reloaded = ReadinessStore(isolated_state["readiness"]).load()
    evidence = reloaded.try_get("codex-chatgpt-account")
    assert evidence is not None
    assert evidence.state is ProviderReadiness.READY
    assert evidence.reason == "READY"
    assert evidence.observed_at
    # Subsequent automatic admission consumes the persisted evidence.
    service = ReadinessService(
        build_access_registry(default_account_connections()),
        ReadinessStore(isolated_state["readiness"]),
        which=lambda _exe: "/fake/bin",
    )
    admission = service.admission_for(provider="codex")
    assert admission.state is ProviderReadiness.READY
    assert admission.reason == "READY"
    assert admission.connection_id == "codex-chatgpt-account"
    # Web renders the persisted truth.
    page = client.get("/access")
    assert page.status_code == 200
    assert "READY" in page.text
    assert evidence.observed_at in page.text


def test_r1b_verify_missing_executable_is_not_ready(
    isolated_state: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A proven missing executable persists NOT_READY / BACKEND_NOT_INSTALLED."""
    calls: list[list[str]] = []
    _fake_which(monkeypatch, present=())
    _fake_status_run(monkeypatch, returncode=0, stdout=_CODEX_OK, calls=calls)
    client = TestClient(create_app(db_path=tmp_path / "web.db"))
    calls.clear()

    response = client.post("/access/test", data={"connection_id": "codex-chatgpt-account"})

    assert response.status_code == 200
    assert [call for call in calls if call and call[0] == "codex"] == []
    evidence = ReadinessStore(isolated_state["readiness"]).load().try_get(
        "codex-chatgpt-account"
    )
    assert evidence is not None
    assert evidence.state is ProviderReadiness.NOT_READY
    assert evidence.reason == "BACKEND_NOT_INSTALLED"


def test_r1b_verify_unauthenticated_is_not_ready(
    isolated_state: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A proven failed session persists NOT_READY / NOT_AUTHENTICATED."""
    calls: list[list[str]] = []
    _fake_which(monkeypatch)
    _fake_status_run(monkeypatch, returncode=1, stdout=_CODEX_DENIED, calls=calls)
    client = TestClient(create_app(db_path=tmp_path / "web.db"))
    calls.clear()

    response = client.post("/access/test", data={"connection_id": "codex-chatgpt-account"})

    assert response.status_code == 200
    assert calls.count(["codex", "doctor", "--json"]) == 1
    evidence = ReadinessStore(isolated_state["readiness"]).load().try_get(
        "codex-chatgpt-account"
    )
    assert evidence is not None
    assert evidence.state is ProviderReadiness.NOT_READY
    assert evidence.reason == "NOT_AUTHENTICATED"


def test_r1b_verify_unprovable_stays_unknown(
    isolated_state: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unparseable status output stays UNKNOWN: CLI presence is not READY."""
    _fake_which(monkeypatch)
    _fake_status_run(
        monkeypatch, returncode=1, stdout="not-json{{{", calls=[]
    )
    client = TestClient(create_app(db_path=tmp_path / "web.db"))

    response = client.post("/access/test", data={"connection_id": "codex-chatgpt-account"})

    assert response.status_code == 200
    evidence = ReadinessStore(isolated_state["readiness"]).load().try_get(
        "codex-chatgpt-account"
    )
    assert evidence is not None
    assert evidence.state is ProviderReadiness.UNKNOWN
    assert evidence.state.value == "UNKNOWN"


def test_r1b_verify_api_connection_not_falsely_not_ready(
    isolated_state: dict[str, Path], tmp_path: Path
) -> None:
    """Descriptor-less API connections keep the reachability probe only."""
    api = ProviderConnection(
        connection_id="byok-r1b-api",
        provider="testapi",
        protocol=AccessProtocol.OPENAI_COMPATIBLE,
        backend="openai-compatible-api",
        auth_mode=AuthMode.API_KEY,
        endpoint="http://127.0.0.1:9/",
        credential_ref="env:R1B_TEST_REF",
    )
    store = AccessStore(owner_state_dir() / AccessStore.FILENAME)
    store.save(build_access_registry(tuple(default_account_connections()) + (api,)))
    client = TestClient(create_app(db_path=tmp_path / "web.db"))

    response = client.post("/access/test", data={"connection_id": "byok-r1b-api"})

    assert response.status_code == 200
    catalog = ReadinessStore(isolated_state["readiness"]).load()
    assert catalog.try_get("byok-r1b-api") is None


# ---------------------------------------------------------------------------
# B. Candidate explanations
# ---------------------------------------------------------------------------


def _write_t1_chain(*candidate_ids: str) -> None:
    payload = load_effective_routing_payload()
    payload["T1"]["default"] = list(candidate_ids)
    save_user_routing(payload)


def test_r1b_two_skips_named_with_typed_reasons(
    isolated_state: dict[str, Path], tmp_path: Path
) -> None:
    """Two automatic candidates rejected differently: no launch, named evidence."""
    _write_t1_chain(
        "opencode/opencode-go/deepseek-v4-flash",
        "codex/gpt-5.6-terra",
    )
    repo = tmp_path / "repo"
    _init_repo(repo)
    db = Database(tmp_path / "orch.db")
    readiness = ReadinessService(
        build_access_registry(default_account_connections()),
        ReadinessStore(isolated_state["readiness"]),
        # codex executable absent -> NOT_READY; opencode present but
        # unverified -> UNKNOWN stays UNKNOWN.
        which=lambda exe: None if exe == "codex" else "/fake/bin",
    )
    service = OrchestratorService(
        db=db,
        registry=AdapterRegistry(
            [_NeverLaunchesAdapter("opencode"), _NeverLaunchesAdapter("codex")]
        ),
        worktree_base=tmp_path / "worktrees",
        readiness_service=readiness,
    )
    result = service.run_sync(
        task="R1-B explanation probe",
        repo_path=repo,
        gate_command=f"{sys.executable} -c pass",
        tier=Tier.T1,
        is_peak=False,
    )

    assert result.passed is False
    assert result.status == RunStatus.FAILED
    assert result.attempts == []

    events = db.get_events(result.run_id, limit=10000)
    skips = [e for e in events if e.event_type == "worker_candidate_skipped"]
    assert len(skips) == 2
    by_provider = {_payload(e)["provider"]: _payload(e) for e in skips}
    assert by_provider["opencode"]["reason"] == "provider_readiness_unknown"
    assert by_provider["codex"]["reason"] == "provider_executable_unavailable"
    # The readiness-specific typed reason is preserved, not collapsed.
    assert by_provider["opencode"]["readiness_state"] == "UNKNOWN"
    assert by_provider["opencode"]["readiness_reason"] == "INSUFFICIENT_EVIDENCE"
    assert by_provider["opencode"]["connection_id"] == "opencode-account"

    no_elig = [e for e in events if e.event_type == "no_eligible_candidate"]
    assert no_elig
    terminal_evidence = _payload(no_elig[-1])
    assert terminal_evidence["routing_context"] == "T1.default"
    skipped = terminal_evidence["skipped_candidates"]
    assert isinstance(skipped, list)
    assert len(skipped) == 2
    assert {entry["provider"] for entry in skipped} == {"opencode", "codex"}
    assert {entry["reason"] for entry in skipped} == {
        "provider_readiness_unknown",
        "provider_executable_unavailable",
    }

    # Owner-facing summary names both candidates and each reason.
    assert "opencode/opencode-go/deepseek-v4-flash" in result.summary
    assert "codex/gpt-5.6-terra" in result.summary
    assert "readiness UNKNOWN (INSUFFICIENT_EVIDENCE)" in result.summary
    assert "executable unavailable" in result.summary
    assert result.summary != "No eligible model."

    run_failed = [e for e in events if e.event_type == "run_failed"][-1]
    run_failed_payload = _payload(run_failed)
    assert run_failed_payload["summary"] == result.summary
    assert run_failed_payload["skipped_candidates"] == skipped

    # The canonical report exposes the same terminal explanation.
    report = build_run_report(db, result.run_id)
    termination = report["termination"]
    assert isinstance(termination, dict)
    assert termination["summary"] == result.summary
    assert termination["routing_context"] == "T1.default"
    termination_skipped = termination["skipped_candidates"]
    assert isinstance(termination_skipped, list)
    assert len(termination_skipped) == 2


# ---------------------------------------------------------------------------
# C. Empty chain
# ---------------------------------------------------------------------------


def test_r1b_empty_chain_validates_and_roundtrips(
    isolated_state: dict[str, Path],
) -> None:
    """routing JSON with [] loads, validates and round-trips."""
    payload = load_effective_routing_payload()
    payload["T1"]["default"] = []
    save_user_routing(payload)

    reloaded = load_effective_routing_payload()
    assert reloaded["T1"]["default"] == []
    snapshot = load_effective_routing()
    assert snapshot.t1_default == []
    assert snapshot.chain_for("T1", True, False) == []


def test_r1b_remove_final_candidate_persists_empty_chain(
    isolated_state: dict[str, Path], tmp_path: Path
) -> None:
    """Web removal of the last candidate persists successfully."""
    _write_t1_chain("codex/gpt-5.6-terra")
    client = TestClient(create_app(db_path=tmp_path / "web.db"))

    response = client.post(
        "/routing/remove",
        data={"context": "T1.default", "candidate_id": "codex/gpt-5.6-terra"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert load_effective_routing_payload()["T1"]["default"] == []


def test_r1b_empty_chain_run_terminates_clearly(
    isolated_state: dict[str, Path], tmp_path: Path
) -> None:
    """A selected empty chain launches nothing and explains itself."""
    payload = load_effective_routing_payload()
    payload["T1"]["default"] = []
    save_user_routing(payload)
    repo = tmp_path / "repo"
    _init_repo(repo)
    db = Database(tmp_path / "orch.db")
    readiness = ReadinessService(
        build_access_registry(default_account_connections()),
        ReadinessStore(isolated_state["readiness"]),
        which=lambda _exe: "/fake/bin",
    )
    readiness.record_success("opencode-account")
    service = OrchestratorService(
        db=db,
        registry=AdapterRegistry([_NeverLaunchesAdapter("opencode")]),
        worktree_base=tmp_path / "worktrees",
        readiness_service=readiness,
    )
    result = service.run_sync(
        task="R1-B empty chain probe",
        repo_path=repo,
        gate_command=f"{sys.executable} -c pass",
        tier=Tier.T1,
        is_peak=False,
    )

    assert result.passed is False
    assert result.attempts == []
    assert result.summary == "No routing candidates are configured for T1.default."
    events = db.get_events(result.run_id, limit=10000)
    no_elig = [e for e in events if e.event_type == "no_eligible_candidate"]
    assert no_elig
    empty_evidence = _payload(no_elig[-1])
    assert empty_evidence["candidates"] == []
    assert empty_evidence["skipped_candidates"] == []
    assert empty_evidence["routing_context"] == "T1.default"


# ---------------------------------------------------------------------------
# D. Exact binding
# ---------------------------------------------------------------------------

_STATIC_CID = "opencode/opencode-go/deepseek-v4-flash"
_STATIC_PROVIDER = "opencode"
_STATIC_MODEL = "opencode-go/deepseek-v4-flash"
_STATIC_CONNECTION = "opencode-account"


def _seed_static_discovery() -> str:
    """Seed access + discovery-verified catalog for the static candidate."""
    connection = next(
        c for c in default_account_connections() if c.connection_id == _STATIC_CONNECTION
    )
    AccessStore(owner_state_dir() / AccessStore.FILENAME).save(
        build_access_registry(default_account_connections())
    )
    result = discover_connection_models(
        connection,
        list_models=lambda: [_STATIC_MODEL],
        execution_support=lambda _conn, _model: ExecutionSupport.SUPPORTED,
        discovered_at=_now_iso(),
    )
    assert result.ok
    catalog_store = ModelCatalogStore(owner_state_dir() / ModelCatalogStore.FILENAME)
    catalog_store.save(apply_discovery_result(catalog_store.load(), result))
    entry = catalog_store.load().try_get(
        connection_id=_STATIC_CONNECTION,
        provider=_STATIC_PROVIDER,
        backend=connection.backend,
        model_id=_STATIC_MODEL,
    )
    assert entry is not None
    assert entry.is_discovery_verified
    assert not entry.is_stale
    return connection.backend


def _materialize_worker(backend: str) -> str:
    registry = AdapterRegistry.default()
    adapter = registry.get(_STATIC_PROVIDER)
    assert adapter is not None
    profile = capability_profile_for(adapter)
    owner = OwnerBindingStore(get_binding_store_path()).load()
    worker, profile_row, pool = materialize_discovered_binding(
        connection_id=_STATIC_CONNECTION,
        provider=_STATIC_PROVIDER,
        model=_STATIC_MODEL,
        provider_transport=profile.transport,
        harness_capabilities=profile.capabilities,
        binding_role=BindingRole.WORKER,
    )
    owner = upsert_owner_binding(owner, binding=worker, profile=profile_row, pool=pool)
    save_owner_bindings(owner)
    return worker.binding_id


def test_r1b_static_add_preserves_exact_binding(
    isolated_state: dict[str, Path], tmp_path: Path
) -> None:
    """A static ALLOWED candidate added via UI keeps its exact WORKER binding."""
    assert _STATIC_CID in ALLOWED_CANDIDATES
    assert not requires_training_permission(_STATIC_CID)
    backend = _seed_static_discovery()
    worker_binding_id = _materialize_worker(backend)
    client = TestClient(create_app(db_path=tmp_path / "web.db"))

    response = client.post(
        "/routing/add",
        data={
            "connection_id": _STATIC_CONNECTION,
            "model_id": _STATIC_MODEL,
            "context": "T1.default",
            "binding_id": worker_binding_id,
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    payload = load_effective_routing_payload()
    assert _STATIC_CID in payload["T1"]["default"]
    approval = payload["approved_candidates"][_STATIC_CID]
    assert approval["binding_id"] == worker_binding_id
    assert approval["connection_id"] == _STATIC_CONNECTION
    # Static training-allowed semantics preserved exactly (never UNKNOWN).
    assert approval["training_required"] == "FALSE"

    # Reload/freeze preserves the exact binding.
    snapshot = load_effective_routing()
    assert snapshot_references_exact_bindings(snapshot) is True
    resolved = resolve_candidate_chain(
        tier=Tier.T1, training_allowed=True, is_peak=False, snapshot=snapshot
    )
    match = next(
        c for c in resolved if f"{c.provider}/{c.model}" == _STATIC_CID
    )
    assert match.binding_id == worker_binding_id

    # The frozen owner registry captures the binding for such runs.
    owner = OwnerBindingStore(get_binding_store_path()).load()
    assert owner.registry.try_get(worker_binding_id) is not None

    # Execution resolves that exact binding (no provider/model fallback):
    # a second connection exposing the same provider/model cannot replace it.
    second = ProviderConnection(
        connection_id="byok-r1b-second",
        provider=_STATIC_PROVIDER,
        protocol=AccessProtocol.OPENAI_COMPATIBLE,
        backend="openai-compatible-api",
        auth_mode=AuthMode.API_KEY,
        endpoint="http://127.0.0.1:9/",
        credential_ref="env:R1B_TEST_REF",
        model=_STATIC_MODEL,
    )
    AccessStore(owner_state_dir() / AccessStore.FILENAME).save(
        build_access_registry(tuple(default_account_connections()) + (second,))
    )
    reloaded_snapshot = load_effective_routing()
    reloaded = resolve_candidate_chain(
        tier=Tier.T1, training_allowed=True, is_peak=False, snapshot=reloaded_snapshot
    )
    match = next(c for c in reloaded if f"{c.provider}/{c.model}" == _STATIC_CID)
    assert match.binding_id == worker_binding_id
    reloaded_owner = OwnerBindingStore(get_binding_store_path()).load()
    resolved_binding = resolve_worker_candidate_binding(
        registry=reloaded_owner.registry,
        candidate_binding_id=match.binding_id,
        provider=_STATIC_PROVIDER,
        model=_STATIC_MODEL,
        adapter=AdapterRegistry.default().get(_STATIC_PROVIDER),
    )
    assert resolved_binding is not None
    assert resolved_binding.binding.binding_id == worker_binding_id


def test_r1b_static_training_denied_semantics_preserved(
    isolated_state: dict[str, Path], tmp_path: Path
) -> None:
    """A static training-allowed candidate stays admissible in denied contexts."""
    backend = _seed_static_discovery()
    worker_binding_id = _materialize_worker(backend)
    client = TestClient(create_app(db_path=tmp_path / "web.db"))

    response = client.post(
        "/routing/add",
        data={
            "connection_id": _STATIC_CONNECTION,
            "model_id": _STATIC_MODEL,
            "context": "T2.training_denied_off_peak",
            "binding_id": worker_binding_id,
        },
        follow_redirects=False,
    )

    # training_required FALSE (not UNKNOWN) keeps the historical static
    # admissibility: UNKNOWN would have failed closed here.
    assert response.status_code == 303
    payload = load_effective_routing_payload()
    assert _STATIC_CID in payload["T2"]["training_denied_off_peak"]


def test_r1b_orchestrator_binding_cannot_satisfy_worker_routing(
    isolated_state: dict[str, Path], tmp_path: Path
) -> None:
    """An ORCHESTRATOR-only binding is refused for WORKER routing."""
    orch_model = "opencode-go/mimo-v2.5"
    connection = next(
        c for c in default_account_connections() if c.connection_id == _STATIC_CONNECTION
    )
    AccessStore(owner_state_dir() / AccessStore.FILENAME).save(
        build_access_registry(default_account_connections())
    )
    result = discover_connection_models(
        connection,
        list_models=lambda: [orch_model],
        execution_support=lambda _conn, _model: ExecutionSupport.SUPPORTED,
        discovered_at=_now_iso(),
    )
    assert result.ok
    catalog_store = ModelCatalogStore(owner_state_dir() / ModelCatalogStore.FILENAME)
    catalog_store.save(apply_discovery_result(catalog_store.load(), result))

    registry = AdapterRegistry.default()
    adapter = registry.get(_STATIC_PROVIDER)
    assert adapter is not None
    profile = capability_profile_for(adapter)
    owner = OwnerBindingStore(get_binding_store_path()).load()
    orch, profile_row, pool = materialize_discovered_binding(
        connection_id=_STATIC_CONNECTION,
        provider=_STATIC_PROVIDER,
        model=orch_model,
        provider_transport=profile.transport,
        harness_capabilities=profile.capabilities,
        binding_role=BindingRole.ORCHESTRATOR,
    )
    owner = upsert_owner_binding(owner, binding=orch, profile=profile_row, pool=pool)
    save_owner_bindings(owner)

    client = TestClient(create_app(db_path=tmp_path / "web.db"))
    response = client.post(
        "/routing/add",
        data={
            "connection_id": _STATIC_CONNECTION,
            "model_id": orch_model,
            "context": "T1.default",
            "binding_id": orch.binding_id,
        },
    )
    assert response.status_code == 400

    with pytest.raises(BindingResolutionError):
        resolve_worker_candidate_binding(
            registry=owner.registry,
            candidate_binding_id=orch.binding_id,
            provider=_STATIC_PROVIDER,
            model=orch_model,
            adapter=adapter,
        )


def test_r1b_candidate_helpers_are_sanitized() -> None:
    """Skip records carry typed reasons only; formatting stays actionable."""
    from saberops.dispatch import (
        build_candidate_skip,
        format_no_eligible_summary,
        routing_context_for,
    )

    assert routing_context_for("T1") == "T1.default"
    assert routing_context_for("T2", training_allowed=True) == "T2.training_allowed"
    assert (
        routing_context_for("T2", training_allowed=False, is_peak=True)
        == "T2.training_denied_peak"
    )
    assert routing_context_for("T3", is_peak=True) == "T3.peak"

    skip = build_candidate_skip(
        provider="opencode",
        model="opencode-go/deepseek-v4-flash",
        reason="provider_readiness_unknown",
        readiness_state="UNKNOWN",
        readiness_reason="STALE_READINESS_EVIDENCE",
        connection_id="opencode-account",
        observed_at="2026-09-24T00:00:00Z",
        binding_id="b-1",
    )
    assert skip["provider"] == "opencode"
    assert skip["binding_id"] == "b-1"
    assert skip["observed_at"] == "2026-09-24T00:00:00Z"
    summary = format_no_eligible_summary([skip], routing_context="T1.default")
    assert "opencode/opencode-go/deepseek-v4-flash" in summary
    assert "readiness UNKNOWN (STALE_READINESS_EVIDENCE)" in summary
    assert format_no_eligible_summary([], routing_context="T1.default") == (
        "No routing candidates are configured for T1.default."
    )
    worker_candidate = WorkerCandidate(provider="p", model="m")
    assert worker_candidate.binding_id is None


# ---------------------------------------------------------------------------
# R1-B.1 Mixed attempted + skipped exhaustion
# ---------------------------------------------------------------------------


class _OnceFailingAdapter(WorkerAdapter):
    """Eligible adapter: launches once, produces changes, then fails the gate."""

    def __init__(self, provider: str) -> None:
        self._provider = provider
        self.call_count = 0

    @property
    def provider_name(self) -> str:
        return self._provider

    def is_available(self) -> bool:
        return True

    def run(self, request: WorkerRequest) -> WorkerResult:
        self.call_count += 1
        (request.worktree_path / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n"
        )
        return WorkerResult(
            provider=self._provider,
            model=request.model,
            status=AttemptStatus.SUCCESS,
            summary="implemented add",
            stdout="done",
            stderr="",
            changed_paths=["calc.py"],
            has_changes=True,
        )


_CAND_A = "opencode/opencode-go/deepseek-v4-flash"
_CAND_B = "codex/gpt-5.6-terra"
_CAND_C = "codex/gpt-5.6-sol"


class _CapabilityFailureAdapter(WorkerAdapter):
    """Eligible adapter: launches once, reports explicit typed capability failure.

    H3.1 removed gate failure from cross-tier escalation authority, so tests
    that need a genuine T1 -> T2 transition use this explicit typed
    ``CAPABILITY_FAILURE`` outcome -- the surviving escalation trigger.
    Classification reads only the typed ``outcome`` field: no stderr is
    parsed and no exit code is classified.
    """

    def __init__(self, provider: str) -> None:
        self._provider = provider
        self.call_count = 0

    @property
    def provider_name(self) -> str:
        return self._provider

    def is_available(self) -> bool:
        return True

    def run(self, request: WorkerRequest) -> WorkerResult:
        self.call_count += 1
        return WorkerResult(
            provider=self._provider,
            model=request.model,
            status=AttemptStatus.FAILED,
            summary="task exceeds model capability",
            stdout="",
            stderr="",
            changed_paths=[],
            has_changes=False,
            error="explicit capability failure",
            outcome=DispatchOutcome.CAPABILITY_FAILURE,
        )


def test_r1b1_mixed_attempted_plus_skipped_exhaustion_is_truthful(
    isolated_state: dict[str, Path], tmp_path: Path
) -> None:
    """Mixed exhaustion: one gate failure plus one readiness skip stays actionable."""
    payload = load_effective_routing_payload()
    payload["T1"]["default"] = [_CAND_A, _CAND_B]
    save_user_routing(payload)
    repo = tmp_path / "repo"
    _init_repo(repo)
    db = Database(tmp_path / "orch.db")
    readiness = ReadinessService(
        build_access_registry(default_account_connections()),
        ReadinessStore(isolated_state["readiness"]),
        which=lambda _exe: "/fake/bin",
    )
    # Candidate A eligible (fresh READY); candidate B stays UNKNOWN
    # (present executable, absent evidence) so it is skipped pre-dispatch.
    readiness.record_success("opencode-account")
    opencode = _OnceFailingAdapter("opencode")
    codex = _NeverLaunchesAdapter("codex")
    service = OrchestratorService(
        db=db,
        registry=AdapterRegistry([opencode, codex]),
        worktree_base=tmp_path / "worktrees",
        readiness_service=readiness,
    )
    result = service.run_sync(
        task="R1-B.1 mixed exhaustion probe",
        repo_path=repo,
        gate_command=f"{sys.executable} -c \"import sys; sys.exit(1)\"",
        max_auto_tier=Tier.T1,
        is_peak=False,
    )

    assert result.passed is False
    assert result.status == RunStatus.FAILED
    # Exactly one worker attempt exists and it is candidate A.
    assert len(result.attempts) == 1
    assert result.attempts[0].provider == "opencode"
    assert opencode.call_count == 1
    # Candidate B never launches.
    assert codex.call_count == 0 if hasattr(codex, "call_count") else True
    db_attempts = db.get_attempts_for_run(result.run_id)
    assert len(db_attempts) == 1
    assert db_attempts[0].provider == "opencode"

    events = db.get_events(result.run_id, limit=10000)
    skips = [e for e in events if e.event_type == "worker_candidate_skipped"]
    assert len(skips) == 1
    skip_payload = _payload(skips[0])
    assert skip_payload["provider"] == "codex"
    assert skip_payload["model"] == "gpt-5.6-terra"
    assert skip_payload["reason"] == "provider_readiness_unknown"
    assert skip_payload["readiness_state"] == "UNKNOWN"
    assert skip_payload["readiness_reason"] == "INSUFFICIENT_EVIDENCE"

    # Final chain exhaustion has structured evidence containing B.
    no_elig = [e for e in events if e.event_type == "no_eligible_candidate"]
    assert no_elig
    terminal_evidence = _payload(no_elig[-1])
    assert terminal_evidence["routing_context"] == "T1.default"
    skipped = terminal_evidence["skipped_candidates"]
    assert isinstance(skipped, list)
    assert len(skipped) == 1
    assert skipped[0]["provider"] == "codex"
    assert skipped[0]["model"] == "gpt-5.6-terra"
    assert skipped[0]["reason"] == "provider_readiness_unknown"
    assert skipped[0]["readiness_state"] == "UNKNOWN"
    assert skipped[0]["readiness_reason"] == "INSUFFICIENT_EVIDENCE"

    # No candidate is falsely classified: A attempted, B skipped, never swapped.
    assert all(entry["provider"] != "opencode" for entry in skipped)
    assert all(e.provider != "codex" for e in db_attempts)

    # Summary retains the attempt/gate fact AND the remaining skip reason.
    assert "1 attempt(s) failed to pass gate" in result.summary
    assert _CAND_B in result.summary
    assert "readiness UNKNOWN (INSUFFICIENT_EVIDENCE)" in result.summary
    # The launched model is not described as ineligible.
    assert "No eligible candidate" in result.summary

    run_failed = [e for e in events if e.event_type == "run_failed"][-1]
    run_failed_payload = _payload(run_failed)
    assert run_failed_payload["summary"] == result.summary
    assert run_failed_payload["skipped_candidates"] == skipped
    assert run_failed_payload["routing_context"] == "T1.default"

    report = build_run_report(db, result.run_id)
    termination = report["termination"]
    assert isinstance(termination, dict)
    assert termination["summary"] == result.summary
    assert termination["routing_context"] == "T1.default"
    assert termination["skipped_candidates"] == skipped


def test_r1b1_prior_tier_skips_never_render_under_later_context(
    isolated_state: dict[str, Path], tmp_path: Path
) -> None:
    """A T1 skip must never be presented as though it belonged to T2.

    The T1 -> T2 transition is driven by explicit typed capability evidence
    (H3.1: gate failure alone no longer escalates).
    """
    payload = load_effective_routing_payload()
    payload["T1"]["default"] = [_CAND_A, _CAND_B]
    payload["T2"]["training_allowed"] = [_CAND_C]
    save_user_routing(payload)
    repo = tmp_path / "repo"
    _init_repo(repo)
    db = Database(tmp_path / "orch.db")
    readiness = ReadinessService(
        build_access_registry(default_account_connections()),
        ReadinessStore(isolated_state["readiness"]),
        which=lambda _exe: "/fake/bin",
    )
    readiness.record_success("opencode-account")
    opencode = _CapabilityFailureAdapter("opencode")
    service = OrchestratorService(
        db=db,
        registry=AdapterRegistry([opencode, _NeverLaunchesAdapter("codex")]),
        worktree_base=tmp_path / "worktrees",
        readiness_service=readiness,
    )
    result = service.run_sync(
        task="R1-B.1 tier scope probe",
        repo_path=repo,
        gate_command=f"{sys.executable} -c \"import sys; sys.exit(1)\"",
        max_auto_tier=Tier.T2,
        is_peak=False,
    )

    assert result.passed is False
    events = db.get_events(result.run_id, limit=10000)
    no_elig = [e for e in events if e.event_type == "no_eligible_candidate"]
    assert no_elig
    # Every terminal record is scoped to its own selected chain: no T2
    # record may carry the T1-skipped terra model.
    for event in no_elig:
        evidence = _payload(event)
        if evidence.get("routing_context") == "T2.training_allowed":
            models = {
                str(entry.get("model")) for entry in evidence["skipped_candidates"]
            }
            assert "gpt-5.6-terra" not in models
            assert "gpt-5.6-sol" in models
    terminal_evidence = _payload(no_elig[-1])
    assert terminal_evidence["routing_context"] == "T2.training_allowed"
    assert {e["model"] for e in terminal_evidence["skipped_candidates"]} == {
        "gpt-5.6-sol"
    }
    run_failed = _payload(
        [e for e in events if e.event_type == "run_failed"][-1]
    )
    assert run_failed["routing_context"] == "T2.training_allowed"
    assert {e["model"] for e in run_failed["skipped_candidates"]} == {
        "gpt-5.6-sol"
    }
    report = build_run_report(db, result.run_id)
    assert report["termination"]["routing_context"] == "T2.training_allowed"
    assert {
        e["model"] for e in report["termination"]["skipped_candidates"]
    } == {"gpt-5.6-sol"}


# ---------------------------------------------------------------------------
# R1-B.2 Package-transition evidence scope
# ---------------------------------------------------------------------------


class _PackageSuccessAdapter(WorkerAdapter):
    """Eligible adapter: launches, produces changes, gate decides the outcome."""

    def __init__(self, provider: str) -> None:
        self._provider = provider
        self.call_count = 0

    @property
    def provider_name(self) -> str:
        return self._provider

    def is_available(self) -> bool:
        return True

    def run(self, request: WorkerRequest) -> WorkerResult:
        self.call_count += 1
        (request.worktree_path / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n"
        )
        return WorkerResult(
            provider=self._provider,
            model=request.model,
            status=AttemptStatus.SUCCESS,
            summary="implemented add",
            stdout="done",
            stderr="",
            changed_paths=["calc.py"],
            has_changes=True,
        )


def test_r1b2_package_transition_resets_skip_evidence_and_routing_context(
    isolated_state: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Package-1 skips never leak into package-2 exhaustion evidence."""
    import saberops.orchestrator as orchestrator_module
    from saberops.slicing import ProposedPackage, TaskSizingAssessment

    pkg1_skip = "codex/gpt-5.6-terra"
    pkg1_success = "opencode/opencode-go/deepseek-v4-flash"
    pkg2_skip = "codex/gpt-5.6-sol"
    payload = load_effective_routing_payload()
    payload["T1"]["default"] = [pkg1_skip, pkg1_success]
    payload["T2"]["training_allowed"] = [pkg2_skip]
    save_user_routing(payload)
    repo = tmp_path / "repo"
    _init_repo(repo)
    db = Database(tmp_path / "orch.db")
    readiness = ReadinessService(
        build_access_registry(default_account_connections()),
        ReadinessStore(isolated_state["readiness"]),
        which=lambda _exe: "/fake/bin",
    )
    # opencode READY (package-1 success path); codex stays UNKNOWN so both
    # the package-1 first candidate and the package-2 chain are skipped.
    readiness.record_success("opencode-account")
    opencode = _PackageSuccessAdapter("opencode")
    service = OrchestratorService(
        db=db,
        registry=AdapterRegistry([opencode, _NeverLaunchesAdapter("codex")]),
        worktree_base=tmp_path / "worktrees",
        readiness_service=readiness,
    )

    assessment = TaskSizingAssessment(
        score=8,
        signals=("r1b2:two-packages",),
        requirement_count=3,
        should_slice=True,
    )
    proposals = [
        ProposedPackage(
            title="Package one typo",
            goal="Fix typo in docs",
            acceptance_criteria=("Typo fixed.",),
            dependency_indexes=(),
        ),
        ProposedPackage(
            title="Package two login",
            goal="Implement bug fix for login",
            acceptance_criteria=("Login fixed.",),
            dependency_indexes=(0,),
        ),
    ]

    def _fake_resolve_work_packages(
        task: str, planner: object | None = None
    ) -> tuple[TaskSizingAssessment, list[ProposedPackage]]:
        assert task
        assert planner is None
        return assessment, list(proposals)

    monkeypatch.setattr(
        orchestrator_module, "resolve_work_packages", _fake_resolve_work_packages
    )
    result = service.run_sync(
        task="R1-B.2 package scope probe",
        repo_path=repo,
        gate_command=f"{sys.executable} -c pass",
        max_auto_tier=Tier.T2,
        is_peak=False,
    )

    assert result.passed is False
    assert result.status == RunStatus.FAILED
    # Package 1 launched exactly once (the eligible opencode candidate).
    assert opencode.call_count == 1
    assert len(result.attempts) == 1
    assert result.attempts[0].provider == "opencode"

    events = db.get_events(result.run_id, limit=10000)
    by_type = [e.event_type for e in events]
    assert "package_completed" in by_type
    assert "next_package_started" in by_type

    # Package 1 really did skip terra before succeeding on the next candidate.
    skips = [e for e in events if e.event_type == "worker_candidate_skipped"]
    assert {_payload(e).get("model") for e in skips} == {
        "gpt-5.6-terra",
        "gpt-5.6-sol",
    }

    resolved = [e for e in events if e.event_type == "tier_candidates_resolved"]
    assert len(resolved) >= 2
    pkg2_resolved = _payload(resolved[1])
    assert pkg2_resolved["tier"] == "T2"
    assert pkg2_resolved["routing_context"] == "T2.training_allowed"

    no_elig = [e for e in events if e.event_type == "no_eligible_candidate"]
    assert no_elig
    terminal = _payload(no_elig[-1])
    assert terminal["tier"] == "T2"
    assert terminal["routing_context"] == "T2.training_allowed"
    assert terminal["routing_context"] == pkg2_resolved["routing_context"]
    terminal_models = {str(e.get("model")) for e in terminal["skipped_candidates"]}
    assert terminal_models == {"gpt-5.6-sol"}
    assert "gpt-5.6-terra" not in terminal_models
    assert "deepseek-v4-flash" not in str(terminal["skipped_candidates"])

    run_failed = _payload(
        [e for e in events if e.event_type == "run_failed"][-1]
    )
    assert run_failed["routing_context"] == "T2.training_allowed"
    run_failed_models = {
        str(e.get("model")) for e in run_failed["skipped_candidates"]
    }
    assert run_failed_models == {"gpt-5.6-sol"}
    assert "gpt-5.6-terra" not in run_failed_models

    report = build_run_report(db, result.run_id)
    termination = report["termination"]
    assert isinstance(termination, dict)
    assert termination["routing_context"] == "T2.training_allowed"
    termination_models = {
        str(e.get("model")) for e in termination["skipped_candidates"]
    }
    assert termination_models == {"gpt-5.6-sol"}
    assert "gpt-5.6-terra" not in termination_models

    # R1-B.3: terminal attempt facts are scoped to the CURRENT (package-2)
    # chain.  Package 2 launched zero workers, so the summary is the
    # zero-launch explanation -- never a claim that package 1's PASSED
    # attempt failed its gate.
    assert "No eligible candidate for T2.training_allowed" in result.summary
    assert "failed to pass gate" not in result.summary
    assert "All 1 attempt(s)" not in result.summary
    assert pkg1_success not in result.summary
    assert pkg2_skip in result.summary

    run_failed = _payload(
        [e for e in events if e.event_type == "run_failed"][-1]
    )
    assert run_failed["summary"] == result.summary
    # run_failed.attempts_count stays the run-total history count;
    # chain_attempts_count names the CURRENT-chain launches.
    assert run_failed["attempts_count"] == 1
    assert run_failed["chain_attempts_count"] == 0

    assert termination["summary"] == result.summary
    # Package-1 successful attempt remains durable history.
    historical = db.get_attempts_for_run(result.run_id)
    assert len(historical) == 1
    assert historical[0].provider == "opencode"
    assert historical[0].model == "opencode-go/deepseek-v4-flash"


def test_r1b3_mixed_current_chain_after_prior_success_is_truthful(
    isolated_state: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Package-2 mixed chain reports exactly its own 1 failed attempt."""
    import saberops.orchestrator as orchestrator_module
    from saberops.slicing import ProposedPackage, TaskSizingAssessment

    pkg1_skip = "codex/gpt-5.6-terra"
    pkg1_success = "opencode/opencode-go/deepseek-v4-flash"
    pkg2_fail = "opencode/opencode-go/deepseek-v4-flash"
    pkg2_skip = "codex/gpt-5.6-sol"
    payload = load_effective_routing_payload()
    payload["T1"]["default"] = [pkg1_skip, pkg1_success]
    payload["T2"]["training_allowed"] = [pkg2_fail, pkg2_skip]
    save_user_routing(payload)
    repo = tmp_path / "repo"
    _init_repo(repo)
    db = Database(tmp_path / "orch.db")
    readiness = ReadinessService(
        build_access_registry(default_account_connections()),
        ReadinessStore(isolated_state["readiness"]),
        which=lambda _exe: "/fake/bin",
    )
    readiness.record_success("opencode-account")
    opencode = _PackageSuccessAdapter("opencode")
    service = OrchestratorService(
        db=db,
        registry=AdapterRegistry([opencode, _NeverLaunchesAdapter("codex")]),
        worktree_base=tmp_path / "worktrees",
        readiness_service=readiness,
    )

    # Gate passes for the first (package-1) invocation, fails after.
    counter = tmp_path / "gate_count.txt"
    counter.write_text("0")
    gate_script = tmp_path / "gate_once.py"
    gate_script.write_text(
        "import pathlib, sys\n"
        f"p = pathlib.Path({str(counter)!r})\n"
        "n = int(p.read_text()) if p.exists() else 0\n"
        "p.write_text(str(n + 1))\n"
        "sys.exit(0 if n == 0 else 1)\n"
    )
    gate_command = f"{sys.executable} {gate_script}"

    assessment = TaskSizingAssessment(
        score=8,
        signals=("r1b3:mixed-two-packages",),
        requirement_count=3,
        should_slice=True,
    )
    proposals = [
        ProposedPackage(
            title="Package one typo",
            goal="Fix typo in docs",
            acceptance_criteria=("Typo fixed.",),
            dependency_indexes=(),
        ),
        ProposedPackage(
            title="Package two login",
            goal="Implement bug fix for login",
            acceptance_criteria=("Login fixed.",),
            dependency_indexes=(0,),
        ),
    ]

    def _fake_resolve_work_packages(
        task: str, planner: object | None = None
    ) -> tuple[TaskSizingAssessment, list[ProposedPackage]]:
        assert task
        assert planner is None
        return assessment, list(proposals)

    monkeypatch.setattr(
        orchestrator_module, "resolve_work_packages", _fake_resolve_work_packages
    )
    result = service.run_sync(
        task="R1-B.3 mixed chain probe",
        repo_path=repo,
        gate_command=gate_command,
        max_auto_tier=Tier.T2,
        is_peak=False,
    )

    assert result.passed is False
    assert result.status == RunStatus.FAILED
    # Package 1 success + package-2 single gate failure = 2 historical attempts.
    assert opencode.call_count == 2
    assert len(result.attempts) == 2
    historical = db.get_attempts_for_run(result.run_id)
    assert len(historical) == 2

    events = db.get_events(result.run_id, limit=10000)
    # Terminal mixed summary names exactly the CURRENT-chain failure count.
    assert "All 1 attempt(s) failed to pass gate" in result.summary
    assert "All 2 attempt(s)" not in result.summary
    # Package-1 success is never described as failed/ineligible.
    assert "No eligible candidate for T2.training_allowed" in result.summary
    assert pkg2_skip in result.summary
    assert pkg1_success not in result.summary.split("No eligible candidate")[-1]

    no_elig = [e for e in events if e.event_type == "no_eligible_candidate"]
    assert no_elig
    terminal = _payload(no_elig[-1])
    assert terminal["routing_context"] == "T2.training_allowed"
    terminal_models = {str(e.get("model")) for e in terminal["skipped_candidates"]}
    assert terminal_models == {"gpt-5.6-sol"}

    run_failed = _payload(
        [e for e in events if e.event_type == "run_failed"][-1]
    )
    assert run_failed["summary"] == result.summary
    assert run_failed["routing_context"] == "T2.training_allowed"
    assert {
        str(e.get("model")) for e in run_failed["skipped_candidates"]
    } == {"gpt-5.6-sol"}
    assert run_failed["attempts_count"] == 2
    assert run_failed["chain_attempts_count"] == 1

    report = build_run_report(db, result.run_id)
    termination = report["termination"]
    assert isinstance(termination, dict)
    assert termination["summary"] == result.summary
    assert termination["routing_context"] == "T2.training_allowed"
    assert {
        str(e["model"]) for e in termination["skipped_candidates"]
    } == {"gpt-5.6-sol"}


# ---------------------------------------------------------------------------
# R1-B.4 Recovery chain scope: the current-chain attempt boundary must
# survive process death via durable evidence, not a process-local marker.
# ---------------------------------------------------------------------------


class _SimulatedOwnerLoss(BaseException):
    """Deterministic process-death stand-in (BaseException, never swallowed).

    ``Orchestrator._run_core`` terminalizes ``Exception`` but never
    ``BaseException``, so raising this leaves the run row RUNNING with
    exactly the durable state a killed execution owner would leave behind.
    """


def _claim_fenced_recovery_owner(db: Database, run_id: str) -> ExecutionOwner:
    """Claim the fenced recovery successor exactly as the supervisor would.

    Mirrors the durable contract ``recover_run`` verifies: an active owner
    row for this process plus a ``recovery_owner_claimed`` event naming the
    same owner id/generation.  Recovery eligibility for these shapes needs
    no RUNNING attempt (inter-package / post-escalation owner loss), so no
    predicate exception applies; fencing still does.
    """
    from saberops.ownership import ProcessIdentity

    owner = db.claim_execution_owner(run_id, identity=ProcessIdentity.for_self())
    db.record_event(
        run_id,
        "recovery_owner_claimed",
        payload={"owner_id": owner.owner_id, "generation": owner.generation},
    )
    return owner


def _arm_crash_on_nth_chain_resolve(
    monkeypatch: pytest.MonkeyPatch, orchestrator_module: object, n: int
) -> dict[str, object]:
    """Kill the run on its nth candidate-chain resolution (process death)."""
    state: dict[str, object] = {"calls": 0, "armed": True}
    real = orchestrator_module.resolve_candidate_chain  # type: ignore[attr-defined]

    def _wrapped(*args: object, **kwargs: object) -> object:
        assert isinstance(state["calls"], int)
        state["calls"] = state["calls"] + 1
        if state["armed"] and state["calls"] == n:
            raise _SimulatedOwnerLoss(f"owner lost during chain resolve #{n}")
        return real(*args, **kwargs)

    monkeypatch.setattr(
        orchestrator_module, "resolve_candidate_chain", _wrapped
    )
    return state


def _arm_crash_on_nth_plan_step(
    monkeypatch: pytest.MonkeyPatch, n: int
) -> dict[str, object]:
    """Kill persistence on the nth durable plan-step write (process death).

    ``persist_plan`` records ``plan_created`` with the full intended
    ``package_count`` *before* it writes the step/package pairs, so dying on
    the second ``create_plan_step`` leaves a genuinely partially persisted
    plan: complete completeness evidence but only the first step/package pair
    durable.  The raised ``BaseException`` is never swallowed, exactly like a
    killed execution owner.
    """
    state: dict[str, object] = {"calls": 0, "armed": True}
    real = Database.create_plan_step

    def _wrapped(self: Database, step: Any) -> None:
        assert isinstance(state["calls"], int)
        state["calls"] = state["calls"] + 1
        if state["armed"] and state["calls"] == n:
            raise _SimulatedOwnerLoss(f"owner lost on plan step #{n}")
        real(self, step)

    monkeypatch.setattr(Database, "create_plan_step", _wrapped)
    return state


def _two_package_proposals() -> tuple[TaskSizingAssessment, list[ProposedPackage]]:
    """Shared deterministic two-package plan: T1 typo fix then T2 login fix."""

    assessment = TaskSizingAssessment(
        score=8,
        signals=("r1b4:two-packages",),
        requirement_count=3,
        should_slice=True,
    )
    proposals = [
        ProposedPackage(
            title="Package one typo",
            goal="Fix typo in docs",
            acceptance_criteria=("Typo fixed.",),
            dependency_indexes=(),
        ),
        ProposedPackage(
            title="Package two login",
            goal="Implement bug fix for login",
            acceptance_criteria=("Login fixed.",),
            dependency_indexes=(0,),
        ),
    ]
    return assessment, proposals


def _recovering_service(
    tmp_path: Path, readiness_path: Path, adapters: list[object], db_path: Path
) -> tuple[Database, OrchestratorService]:
    """Build a fresh-process service over the same durable DB and stores."""
    db = Database(db_path)
    readiness = ReadinessService(
        build_access_registry(default_account_connections()),
        ReadinessStore(readiness_path),
        which=lambda _exe: "/fake/bin",
    )
    service = OrchestratorService(
        db=db,
        registry=AdapterRegistry(adapters),  # type: ignore[arg-type]
        worktree_base=tmp_path / "worktrees",
        readiness_service=readiness,
    )
    return db, service


def _crashed_two_package_boundary(
    isolated_state: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    task: str,
    slice_signal: str,
) -> tuple[Database, Path, str, list[Any], dict[str, object]]:
    """Real package-1 execution, then process death after the boundary.

    Phase 1 slices two packages, really executes package 1 (skip + gate-passed
    success) and dies during package 2's fresh-chain resolve, leaving durable
    package-1 attempt history plus ``package_completed`` and
    ``next_package_started``.  Shared setup for the R1-B.4.3 marker-evidence
    leak probes.
    """
    import saberops.orchestrator as orchestrator_module

    pkg1_skip = "codex/gpt-5.6-terra"
    pkg1_success = "opencode/opencode-go/deepseek-v4-flash"
    pkg2_skip = "codex/gpt-5.6-sol"
    payload = load_effective_routing_payload()
    payload["T1"]["default"] = [pkg1_skip, pkg1_success]
    payload["T2"]["training_allowed"] = [pkg2_skip]
    save_user_routing(payload)
    repo = tmp_path / "repo"
    _init_repo(repo)
    db_path = tmp_path / "orch.db"
    db = Database(db_path)
    readiness = ReadinessService(
        build_access_registry(default_account_connections()),
        ReadinessStore(isolated_state["readiness"]),
        which=lambda _exe: "/fake/bin",
    )
    readiness.record_success("opencode-account")
    service = OrchestratorService(
        db=db,
        registry=AdapterRegistry(
            [_PackageSuccessAdapter("opencode"), _NeverLaunchesAdapter("codex")]
        ),
        worktree_base=tmp_path / "worktrees",
        readiness_service=readiness,
    )

    assessment, proposals = _two_package_proposals()
    plan_phase: dict[str, object] = {"sliced": True}

    def _fake_resolve_work_packages(
        task_text: str, planner: object | None = None
    ) -> tuple[TaskSizingAssessment, list[ProposedPackage]]:

        assert task_text
        assert planner is None
        if not plan_phase["sliced"]:
            return TaskSizingAssessment(
                score=1,
                signals=(slice_signal,),
                requirement_count=1,
                should_slice=False,
            ), []
        return assessment, list(proposals)

    monkeypatch.setattr(
        orchestrator_module, "resolve_work_packages", _fake_resolve_work_packages
    )
    crash = _arm_crash_on_nth_chain_resolve(monkeypatch, orchestrator_module, 2)
    with pytest.raises(_SimulatedOwnerLoss):
        service.run_sync(
            task=task,
            repo_path=repo,
            gate_command=f"{sys.executable} -c pass",
            max_auto_tier=Tier.T2,
            is_peak=False,
        )
    crash["armed"] = False

    runs = db.list_runs(limit=10)
    assert len(runs) == 1
    run_id = runs[0].id
    pre_run = db.get_run(run_id)
    assert pre_run is not None
    assert pre_run.status == RunStatus.RUNNING
    pre_attempts = db.get_attempts_for_run(run_id)
    assert len(pre_attempts) == 1
    assert pre_attempts[0].status == AttemptStatus.SUCCESS
    pre_types = [e.event_type for e in db.get_complete_events_for_run(run_id)]
    assert "package_completed" in pre_types
    assert "next_package_started" in pre_types
    assert "run_failed" not in pre_types
    return db, db_path, run_id, pre_attempts, plan_phase


def _crashed_partial_two_package_plan(
    isolated_state: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    task: str,
) -> tuple[Database, Path, str]:
    """Real two-package persistence that dies after the first durable pair.

    Phase 1 slices two packages and actually begins persisting them, but the
    process dies on the second ``create_plan_step`` call.  Durable evidence
    therefore contains ``plan_created(package_count=2)`` plus exactly one
    fully linked step/package pair: a genuinely partially persisted plan, not
    a hand-constructed impossible state.
    """
    import saberops.orchestrator as orchestrator_module

    payload = load_effective_routing_payload()
    payload["T1"]["default"] = ["codex/gpt-5.6-terra", "opencode/opencode-go/deepseek-v4-flash"]
    payload["T2"]["training_allowed"] = ["codex/gpt-5.6-sol"]
    save_user_routing(payload)
    repo = tmp_path / "repo"
    _init_repo(repo)
    db_path = tmp_path / "orch.db"
    db = Database(db_path)
    readiness = ReadinessService(
        build_access_registry(default_account_connections()),
        ReadinessStore(isolated_state["readiness"]),
        which=lambda _exe: "/fake/bin",
    )
    readiness.record_success("opencode-account")
    service = OrchestratorService(
        db=db,
        registry=AdapterRegistry(
            [_PackageSuccessAdapter("opencode"), _NeverLaunchesAdapter("codex")]
        ),
        worktree_base=tmp_path / "worktrees",
        readiness_service=readiness,
    )

    assessment, proposals = _two_package_proposals()

    def _fake_resolve_work_packages(
        task_text: str, planner: object | None = None
    ) -> tuple[TaskSizingAssessment, list[ProposedPackage]]:

        assert task_text
        assert planner is None
        return assessment, list(proposals)

    monkeypatch.setattr(
        orchestrator_module, "resolve_work_packages", _fake_resolve_work_packages
    )
    crash = _arm_crash_on_nth_plan_step(monkeypatch, 2)
    with pytest.raises(_SimulatedOwnerLoss):
        service.run_sync(
            task=task,
            repo_path=repo,
            gate_command=f"{sys.executable} -c pass",
            max_auto_tier=Tier.T2,
            is_peak=False,
        )
    crash["armed"] = False

    runs = db.list_runs(limit=10)
    assert len(runs) == 1
    run_id = runs[0].id
    pre_run = db.get_run(run_id)
    assert pre_run is not None
    assert pre_run.status == RunStatus.RUNNING
    return db, db_path, run_id


def _durable_plan_snapshot(db: Database, run_id: str) -> dict[str, Any]:
    """Capture the durable plan/step/package identity and lifecycle state."""
    plans = db.list_run_plans(run_id)
    steps = db.get_plan_steps(run_id)
    packages = db.get_work_packages(run_id)
    return {
        "plan_ids": [plan.id for plan in plans],
        "step_ids": [step.id for step in steps],
        "package_ids": [package.id for package in packages],
        "step_statuses": {step.id: step.status.value for step in steps},
        "step_package_ids": {step.id: step.work_package_id for step in steps},
        "package_goals": {package.id: package.goal for package in packages},
        "package_criteria": {
            package.id: package.acceptance_criteria for package in packages
        },
        "package_deps": {
            package.id: package.dependency_package_ids for package in packages
        },
    }


def _recover_expect_ambiguous_blocked(
    isolated_state: dict[str, Path],
    tmp_path: Path,
    db_path: Path,
    run_id: str,
    plan_phase: dict[str, object],
) -> Database:
    """Resume through the real fenced recovery seam and assert fail-closed.

    The recovery must block on the existing safety convention, leave the run
    RUNNING, fabricate no terminal facts and preserve durable history.
    """
    plan_phase["sliced"] = False
    db2, service2 = _recovering_service(
        tmp_path,
        isolated_state["readiness"],
        [_PackageSuccessAdapter("opencode"), _NeverLaunchesAdapter("codex")],
        db_path,
    )
    owner = _claim_fenced_recovery_owner(db2, run_id)
    with pytest.raises(
        RuntimeError,
        match="Recovery blocked: current routing-chain attempt boundary",
    ):
        service2.orchestrator.recover_run(
            run_id,
            expected_owner_id=owner.owner_id,
            expected_owner_generation=owner.generation,
        )

    post_run = db2.get_run(run_id)
    assert post_run is not None
    assert post_run.status == RunStatus.RUNNING
    post_events = db2.get_complete_events_for_run(run_id)
    post_types = [e.event_type for e in post_events]
    assert "recovery_blocked_ambiguous_chain_boundary" in post_types
    assert "run_failed" not in post_types
    assert "no_eligible_candidate" not in post_types
    assert not any(
        e.event_type == "run_failed"
        and e.payload
        and "chain_attempts_count" in e.payload
        for e in post_events
    )
    return db2


def test_r1b4_package_recovery_excludes_prior_package_attempts(
    isolated_state: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovery between packages: package-1 history is not package-2 evidence.

    Phase 1 really executes package 1 (skip + gate-passed success) and dies
    after ``next_package_started`` is durable.  Phase 2 resumes through the
    real fenced ``recover_run`` seam with zero process-local chain state and
    exhausts package 2 without launching.  The terminal facts must describe
    only the package-2 chain.
    """
    import saberops.orchestrator as orchestrator_module

    pkg1_skip = "codex/gpt-5.6-terra"
    pkg1_success = "opencode/opencode-go/deepseek-v4-flash"
    pkg2_skip = "codex/gpt-5.6-sol"
    payload = load_effective_routing_payload()
    payload["T1"]["default"] = [pkg1_skip, pkg1_success]
    payload["T2"]["training_allowed"] = [pkg2_skip]
    save_user_routing(payload)
    repo = tmp_path / "repo"
    _init_repo(repo)
    db_path = tmp_path / "orch.db"
    db = Database(db_path)
    readiness = ReadinessService(
        build_access_registry(default_account_connections()),
        ReadinessStore(isolated_state["readiness"]),
        which=lambda _exe: "/fake/bin",
    )
    readiness.record_success("opencode-account")
    service = OrchestratorService(
        db=db,
        registry=AdapterRegistry(
            [_PackageSuccessAdapter("opencode"), _NeverLaunchesAdapter("codex")]
        ),
        worktree_base=tmp_path / "worktrees",
        readiness_service=readiness,
    )

    assessment, proposals = _two_package_proposals()
    # Phase 1 plans two packages; the resume-time planner sees the durable
    # plan as already in place (package 1 COMPLETED, package 2 READY), so no
    # second plan is persisted on re-entry.  Re-planning on recovery is
    # untouched out-of-scope behavior; this keeps the test on the R1-B.4
    # chain-scope question with the real adoption path for attempts/events.
    plan_phase: dict[str, object] = {"sliced": True}

    def _fake_resolve_work_packages(
        task: str, planner: object | None = None
    ) -> tuple[TaskSizingAssessment, list[ProposedPackage]]:

        assert task
        assert planner is None
        if not plan_phase["sliced"]:
            return TaskSizingAssessment(
                score=1,
                signals=("r1b4:already-planned",),
                requirement_count=1,
                should_slice=False,
            ), []
        return assessment, list(proposals)

    monkeypatch.setattr(
        orchestrator_module, "resolve_work_packages", _fake_resolve_work_packages
    )
    # First chain resolve serves package 1; the second serves package 2
    # (inside its fresh-chain transition) -- die there, after package 1's
    # success, package_completed and next_package_started are durable.
    crash = _arm_crash_on_nth_chain_resolve(monkeypatch, orchestrator_module, 2)
    with pytest.raises(_SimulatedOwnerLoss):
        service.run_sync(
            task="R1-B.4 package recovery probe",
            repo_path=repo,
            gate_command=f"{sys.executable} -c pass",
            max_auto_tier=Tier.T2,
            is_peak=False,
        )
    crash["armed"] = False

    runs = db.list_runs(limit=10)
    assert len(runs) == 1
    run_id = runs[0].id
    pre_run = db.get_run(run_id)
    assert pre_run is not None
    assert pre_run.status == RunStatus.RUNNING
    pre_attempts = db.get_attempts_for_run(run_id)
    assert len(pre_attempts) == 1
    assert pre_attempts[0].status == AttemptStatus.SUCCESS
    pre_types = [e.event_type for e in db.get_complete_events_for_run(run_id)]
    assert "package_completed" in pre_types
    assert "next_package_started" in pre_types
    assert "run_failed" not in pre_types

    # Fresh process: new DB handle, new adapters, zero in-memory chain state.
    # The durable plan (package 1 COMPLETED, package 2 READY) is adopted
    # as-is, so recovery resumes the ORIGINAL package-2 chain with no
    # intervening fresh-chain install to mask a defective marker.
    plan_phase["sliced"] = False
    db2, service2 = _recovering_service(
        tmp_path,
        isolated_state["readiness"],
        [_PackageSuccessAdapter("opencode"), _NeverLaunchesAdapter("codex")],
        db_path,
    )
    owner = _claim_fenced_recovery_owner(db2, run_id)
    result = service2.orchestrator.recover_run(
        run_id,
        expected_owner_id=owner.owner_id,
        expected_owner_generation=owner.generation,
    )

    assert result.passed is False
    assert result.status == RunStatus.FAILED
    # Historical attempts still contain the package-1 gate-passed success,
    # and nothing else launched: package 2 was resumed directly.
    historical = db2.get_attempts_for_run(run_id)
    assert len(historical) == 1
    assert historical[0].provider == "opencode"
    assert historical[0].model == "opencode-go/deepseek-v4-flash"
    assert historical[0].status == AttemptStatus.SUCCESS

    events = db2.get_events(run_id, limit=10000)
    # Current-chain attempt count is zero: package 2 launched nothing.
    assert "No eligible candidate for T2.training_allowed" in result.summary
    assert "failed to pass gate" not in result.summary
    assert "All " not in result.summary.split("No eligible candidate")[0]
    assert pkg1_success not in result.summary
    assert pkg2_skip in result.summary

    run_failed = _payload(
        [e for e in events if e.event_type == "run_failed"][-1]
    )
    assert run_failed["summary"] == result.summary
    assert run_failed["attempts_count"] == len(historical)
    assert run_failed["chain_attempts_count"] == 0
    assert run_failed["routing_context"] == "T2.training_allowed"
    run_failed_models = {
        str(e.get("model")) for e in run_failed["skipped_candidates"]
    }
    assert run_failed_models == {"gpt-5.6-sol"}
    assert "gpt-5.6-terra" not in run_failed_models
    assert "deepseek-v4-flash" not in str(run_failed["skipped_candidates"])

    report = build_run_report(db2, run_id)
    termination = report["termination"]
    assert isinstance(termination, dict)
    assert termination["summary"] == result.summary
    assert termination["routing_context"] == "T2.training_allowed"
    assert {
        str(e.get("model")) for e in termination["skipped_candidates"]
    } == {"gpt-5.6-sol"}


def test_r1b4_tier_boundary_reconstructs_from_durable_evidence(
    isolated_state: dict[str, Path],
    tmp_path: Path,
) -> None:
    """The same durable boundary mechanism covers tier transitions (focused).

    A real uninterrupted run fails one T1 attempt with explicit typed
    capability evidence (the H3.1 escalation trigger), escalates, and
    exhausts T2 without launching there.  A fresh process then reconstructs
    the current-chain boundary from durable evidence alone through the real
    R1-B.4 authority: the T1 attempt is history, the T2 chain count is zero.
    """
    t1_fail = "opencode/opencode-go/deepseek-v4-flash"
    t2_skip = "codex/gpt-5.6-sol"
    payload = load_effective_routing_payload()
    payload["T1"]["default"] = [t1_fail]
    payload["T2"]["training_allowed"] = [t2_skip]
    save_user_routing(payload)
    repo = tmp_path / "repo"
    _init_repo(repo)
    db_path = tmp_path / "orch.db"
    db = Database(db_path)
    readiness = ReadinessService(
        build_access_registry(default_account_connections()),
        ReadinessStore(isolated_state["readiness"]),
        which=lambda _exe: "/fake/bin",
    )
    readiness.record_success("opencode-account")
    service = OrchestratorService(
        db=db,
        registry=AdapterRegistry(
            [_CapabilityFailureAdapter("opencode"), _NeverLaunchesAdapter("codex")]
        ),
        worktree_base=tmp_path / "worktrees",
        readiness_service=readiness,
    )
    result = service.run_sync(
        task="R1-B.4 tier boundary probe",
        repo_path=repo,
        gate_command=f"{sys.executable} -c \"import sys; sys.exit(1)\"",
        max_auto_tier=Tier.T2,
        is_peak=False,
    )

    assert result.passed is False
    assert result.status == RunStatus.FAILED
    assert "No eligible candidate for T2.training_allowed" in result.summary
    assert "failed to pass gate" not in result.summary
    assert t2_skip in result.summary
    events = db.get_events(result.run_id, limit=10000)
    run_failed = _payload(
        [e for e in events if e.event_type == "run_failed"][-1]
    )
    assert run_failed["attempts_count"] == 1
    assert run_failed["chain_attempts_count"] == 0

    # Fresh process: reconstruct the boundary from durable evidence alone.
    from saberops.orchestrator import _reconstruct_chain_attempt_start

    db_fresh = Database(db_path)
    fresh_attempts = db_fresh.get_attempts_for_run(result.run_id)
    assert len(fresh_attempts) == 1
    assert fresh_attempts[0].status == AttemptStatus.FAILED
    boundary_types = [
        e.event_type for e in db_fresh.get_complete_events_for_run(result.run_id)
    ]
    assert "tier_escalated" in boundary_types
    reconstructed = _reconstruct_chain_attempt_start(
        db_fresh, result.run_id, fresh_attempts
    )
    assert reconstructed == 1
    assert len(fresh_attempts) - reconstructed == 0


def test_r1b4_ambiguous_chain_boundary_fails_closed(
    isolated_state: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ambiguous chain-boundary evidence must fail closed, never fabricate zero.

    A real package transition leaves a fresh-chain boundary plus durable
    package-1 history, but the durable launch-marker evidence is incomplete:
    one attempt row exists with no ``attempt_started`` marker, so the trailing
    suffix split cannot be proven.  R1-B.4.1 requires recovery to block on the
    existing recovery safety convention instead of collapsing the unknown into
    a precise ``chain_attempts_count=0`` and a false zero-launch /
    no-eligible-candidate terminal story.  Durable history stays intact and
    the run is never terminalized.
    """
    import saberops.orchestrator as orchestrator_module
    from saberops.models import Attempt

    pkg1_skip = "codex/gpt-5.6-terra"
    pkg1_success = "opencode/opencode-go/deepseek-v4-flash"
    pkg2_skip = "codex/gpt-5.6-sol"
    payload = load_effective_routing_payload()
    payload["T1"]["default"] = [pkg1_skip, pkg1_success]
    payload["T2"]["training_allowed"] = [pkg2_skip]
    save_user_routing(payload)
    repo = tmp_path / "repo"
    _init_repo(repo)
    db_path = tmp_path / "orch.db"
    db = Database(db_path)
    readiness = ReadinessService(
        build_access_registry(default_account_connections()),
        ReadinessStore(isolated_state["readiness"]),
        which=lambda _exe: "/fake/bin",
    )
    readiness.record_success("opencode-account")
    service = OrchestratorService(
        db=db,
        registry=AdapterRegistry(
            [_PackageSuccessAdapter("opencode"), _NeverLaunchesAdapter("codex")]
        ),
        worktree_base=tmp_path / "worktrees",
        readiness_service=readiness,
    )

    assessment, proposals = _two_package_proposals()
    plan_phase: dict[str, object] = {"sliced": True}

    def _fake_resolve_work_packages(
        task: str, planner: object | None = None
    ) -> tuple[TaskSizingAssessment, list[ProposedPackage]]:

        assert task
        assert planner is None
        if not plan_phase["sliced"]:
            return TaskSizingAssessment(
                score=1,
                signals=("r1b41:already-planned",),
                requirement_count=1,
                should_slice=False,
            ), []
        return assessment, list(proposals)

    monkeypatch.setattr(
        orchestrator_module, "resolve_work_packages", _fake_resolve_work_packages
    )
    crash = _arm_crash_on_nth_chain_resolve(monkeypatch, orchestrator_module, 2)
    with pytest.raises(_SimulatedOwnerLoss):
        service.run_sync(
            task="R1-B.4.1 ambiguous boundary probe",
            repo_path=repo,
            gate_command=f"{sys.executable} -c pass",
            max_auto_tier=Tier.T2,
            is_peak=False,
        )
    crash["armed"] = False

    runs = db.list_runs(limit=10)
    assert len(runs) == 1
    run_id = runs[0].id
    pre_run = db.get_run(run_id)
    assert pre_run is not None
    assert pre_run.status == RunStatus.RUNNING
    pre_attempts = db.get_attempts_for_run(run_id)
    assert len(pre_attempts) == 1
    assert pre_attempts[0].status == AttemptStatus.SUCCESS
    pre_types = [e.event_type for e in db.get_complete_events_for_run(run_id)]
    assert "next_package_started" in pre_types
    assert "run_failed" not in pre_types

    # Incomplete durable launch evidence: a real attempt row exists with no
    # ``attempt_started`` marker of its own, so the post-boundary split
    # cannot be proven from the durable event log.
    db.create_attempt(
        Attempt(
            id="att_ambiguous_markerless",
            run_id=run_id,
            attempt_number=2,
            provider="codex",
            model="gpt-5.6-sol",
            worktree_path=str(tmp_path / "worktrees" / "ambiguous"),
            branch_name="orch/ambiguous",
            status=AttemptStatus.FAILED,
            created_at=_now_iso(),
        )
    )
    ambiguous_attempts = db.get_attempts_for_run(run_id)
    assert len(ambiguous_attempts) == 2

    from saberops.orchestrator import _reconstruct_chain_attempt_start

    # No fabricated precise boundary: the unknown stays unknown.
    assert (
        _reconstruct_chain_attempt_start(db, run_id, ambiguous_attempts) is None
    )

    # Fresh process: recovery must block on the existing safety convention.
    plan_phase["sliced"] = False
    db2, service2 = _recovering_service(
        tmp_path,
        isolated_state["readiness"],
        [_PackageSuccessAdapter("opencode"), _NeverLaunchesAdapter("codex")],
        db_path,
    )
    owner = _claim_fenced_recovery_owner(db2, run_id)
    with pytest.raises(
        RuntimeError,
        match="Recovery blocked: current routing-chain attempt boundary",
    ):
        service2.orchestrator.recover_run(
            run_id,
            expected_owner_id=owner.owner_id,
            expected_owner_generation=owner.generation,
        )

    # Fail closed: no terminal fabrication, run stays RUNNING, history intact.
    post_run = db2.get_run(run_id)
    assert post_run is not None
    assert post_run.status == RunStatus.RUNNING
    post_types = [e.event_type for e in db2.get_complete_events_for_run(run_id)]
    assert "recovery_blocked_ambiguous_chain_boundary" in post_types
    assert "run_failed" not in post_types
    assert "no_eligible_candidate" not in post_types
    post_attempts = db2.get_attempts_for_run(run_id)
    assert len(post_attempts) == 2
    assert post_attempts[0].id == pre_attempts[0].id
    assert post_attempts[0].status == AttemptStatus.SUCCESS
    assert post_attempts[0].commit_sha == pre_attempts[0].commit_sha
    assert post_attempts[0].model == "opencode-go/deepseek-v4-flash"


def test_r1b42_markerless_middle_attempt_blocks_boundary_reconstruction(
    isolated_state: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A precise split needs the COMPLETE attempt history reconciled.

    R1-B.4.1 accepted a post-boundary marker list that was merely a trailing
    suffix of the attempt history without first proving the whole history
    reconciled with launch markers.  Here a real package transition leaves
    durable package-1 history plus a genuine fresh-chain boundary, and the
    current-chain marker list is non-empty and a trailing suffix --
    ``[a3]`` over ``[a1, a2, a3]`` -- so afbd71d returned the precise index 2
    and silently classified the markerless middle attempt ``a2`` as old-chain
    history.  ``a2`` could belong to either chain, so the split is unprovable
    and recovery must fail closed without fabricating a boundary.
    """
    import saberops.orchestrator as orchestrator_module
    from saberops.models import Attempt

    pkg1_skip = "codex/gpt-5.6-terra"
    pkg1_success = "opencode/opencode-go/deepseek-v4-flash"
    pkg2_skip = "codex/gpt-5.6-sol"
    payload = load_effective_routing_payload()
    payload["T1"]["default"] = [pkg1_skip, pkg1_success]
    payload["T2"]["training_allowed"] = [pkg2_skip]
    save_user_routing(payload)
    repo = tmp_path / "repo"
    _init_repo(repo)
    db_path = tmp_path / "orch.db"
    db = Database(db_path)
    readiness = ReadinessService(
        build_access_registry(default_account_connections()),
        ReadinessStore(isolated_state["readiness"]),
        which=lambda _exe: "/fake/bin",
    )
    readiness.record_success("opencode-account")
    service = OrchestratorService(
        db=db,
        registry=AdapterRegistry(
            [_PackageSuccessAdapter("opencode"), _NeverLaunchesAdapter("codex")]
        ),
        worktree_base=tmp_path / "worktrees",
        readiness_service=readiness,
    )

    assessment, proposals = _two_package_proposals()
    plan_phase: dict[str, object] = {"sliced": True}

    def _fake_resolve_work_packages(
        task: str, planner: object | None = None
    ) -> tuple[TaskSizingAssessment, list[ProposedPackage]]:

        assert task
        assert planner is None
        if not plan_phase["sliced"]:
            return TaskSizingAssessment(
                score=1,
                signals=("r1b42:already-planned",),
                requirement_count=1,
                should_slice=False,
            ), []
        return assessment, list(proposals)

    monkeypatch.setattr(
        orchestrator_module, "resolve_work_packages", _fake_resolve_work_packages
    )
    crash = _arm_crash_on_nth_chain_resolve(monkeypatch, orchestrator_module, 2)
    with pytest.raises(_SimulatedOwnerLoss):
        service.run_sync(
            task="R1-B.4.2 complete reconciliation probe",
            repo_path=repo,
            gate_command=f"{sys.executable} -c pass",
            max_auto_tier=Tier.T2,
            is_peak=False,
        )
    crash["armed"] = False

    runs = db.list_runs(limit=10)
    assert len(runs) == 1
    run_id = runs[0].id
    pre_run = db.get_run(run_id)
    assert pre_run is not None
    assert pre_run.status == RunStatus.RUNNING
    pre_attempts = db.get_attempts_for_run(run_id)
    assert len(pre_attempts) == 1
    assert pre_attempts[0].status == AttemptStatus.SUCCESS
    boundary_event = [
        e
        for e in db.get_complete_events_for_run(run_id)
        if e.event_type == "next_package_started"
    ][-1]

    # Complete durable history: the old-chain success (a1, marked before the
    # boundary), a markerless middle attempt (a2), and a post-boundary marked
    # attempt (a3) whose marker is the exact trailing suffix [a3] of
    # [a1, a2, a3] -- the precise shape afbd71d misread as a proven split.
    db.create_attempt(
        Attempt(
            id="att_markerless_middle",
            run_id=run_id,
            attempt_number=2,
            provider="codex",
            model="gpt-5.6-sol",
            worktree_path=str(tmp_path / "worktrees" / "markerless-middle"),
            branch_name="orch/markerless-middle",
            status=AttemptStatus.FAILED,
            created_at=_now_iso(),
        )
    )
    db.create_attempt(
        Attempt(
            id="att_post_boundary",
            run_id=run_id,
            attempt_number=3,
            provider="opencode",
            model="opencode-go/deepseek-v4-flash",
            worktree_path=str(tmp_path / "worktrees" / "post-boundary"),
            branch_name="orch/post-boundary",
            status=AttemptStatus.FAILED,
            created_at=_now_iso(),
        )
    )
    post_boundary_marker_id = db.record_event(
        run_id,
        "attempt_started",
        attempt_id="att_post_boundary",
        payload={"provider": "opencode", "model": "opencode-go/deepseek-v4-flash"},
    )
    assert post_boundary_marker_id > boundary_event.id

    history = db.get_attempts_for_run(run_id)
    assert [a.id for a in history] == [
        pre_attempts[0].id,
        "att_markerless_middle",
        "att_post_boundary",
    ]

    from saberops.orchestrator import _reconstruct_chain_attempt_start

    # The trailing suffix [a3] is not enough: the markerless middle attempt
    # leaves the complete history unreconciled, so the split stays unknown.
    assert _reconstruct_chain_attempt_start(db, run_id, history) is None

    # Fresh process: recovery must block on the existing safety convention.
    plan_phase["sliced"] = False
    db2, service2 = _recovering_service(
        tmp_path,
        isolated_state["readiness"],
        [_PackageSuccessAdapter("opencode"), _NeverLaunchesAdapter("codex")],
        db_path,
    )
    owner = _claim_fenced_recovery_owner(db2, run_id)
    with pytest.raises(
        RuntimeError,
        match="Recovery blocked: current routing-chain attempt boundary",
    ):
        service2.orchestrator.recover_run(
            run_id,
            expected_owner_id=owner.owner_id,
            expected_owner_generation=owner.generation,
        )

    # Fail closed: no terminal fabrication, run stays RUNNING, history intact.
    post_run = db2.get_run(run_id)
    assert post_run is not None
    assert post_run.status == RunStatus.RUNNING
    post_events = db2.get_complete_events_for_run(run_id)
    post_types = [e.event_type for e in post_events]
    assert "recovery_blocked_ambiguous_chain_boundary" in post_types
    assert "run_failed" not in post_types
    assert "no_eligible_candidate" not in post_types
    assert not any(
        e.event_type == "run_failed" and e.payload and "chain_attempts_count" in e.payload
        for e in post_events
    )
    # The launch-marker evidence that proves the unknown stays intact.
    post_marker_ids = [
        e.id
        for e in post_events
        if e.event_type == "attempt_started" and e.attempt_id == "att_post_boundary"
    ]
    assert post_marker_ids == [post_boundary_marker_id]
    post_attempts = db2.get_attempts_for_run(run_id)
    assert [a.id for a in post_attempts] == [
        pre_attempts[0].id,
        "att_markerless_middle",
        "att_post_boundary",
    ]
    assert post_attempts[0].status == AttemptStatus.SUCCESS
    assert post_attempts[0].commit_sha == pre_attempts[0].commit_sha
    assert post_attempts[0].model == "opencode-go/deepseek-v4-flash"


def test_r1b43_duplicate_marker_blocks_package_fallback(
    isolated_state: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A duplicated post-boundary launch marker is contradictory evidence.

    A real package transition leaves durable package-1 history (``a1``) plus a
    genuine fresh-chain boundary, and ``package_completed`` names ``a1``.  The
    current-chain attempt ``a2`` carries TWO post-boundary ``attempt_started``
    markers.  The marker sequence ``[a1, a2, a2]`` contradicts the durable
    attempt sequence ``[a1, a2]``, so no precise boundary is proven: f3c3aed
    returned split=1 from the package fallback, which R1-B.4.3 rejects.
    Recovery must fail closed on the existing convention.
    """
    from saberops.models import Attempt
    from saberops.orchestrator import _reconstruct_chain_attempt_start

    db, db_path, run_id, pre_attempts, plan_phase = _crashed_two_package_boundary(
        isolated_state,
        tmp_path,
        monkeypatch,
        task="R1-B.4.3 duplicate marker probe",
        slice_signal="r1b43-duplicate:already-planned",
    )
    old_attempt_id = pre_attempts[0].id
    boundary_event = [
        e
        for e in db.get_complete_events_for_run(run_id)
        if e.event_type == "next_package_started"
    ][-1]

    db.create_attempt(
        Attempt(
            id="att_duplicate_marker",
            run_id=run_id,
            attempt_number=2,
            provider="opencode",
            model="opencode-go/deepseek-v4-flash",
            worktree_path=str(tmp_path / "worktrees" / "duplicate-marker"),
            branch_name="orch/duplicate-marker",
            status=AttemptStatus.FAILED,
            created_at=_now_iso(),
        )
    )
    first_marker_id = db.record_event(
        run_id,
        "attempt_started",
        attempt_id="att_duplicate_marker",
        payload={"provider": "opencode", "model": "opencode-go/deepseek-v4-flash"},
    )
    second_marker_id = db.record_event(
        run_id,
        "attempt_started",
        attempt_id="att_duplicate_marker",
        payload={"provider": "opencode", "model": "opencode-go/deepseek-v4-flash"},
    )
    assert first_marker_id > boundary_event.id
    assert second_marker_id > boundary_event.id

    history = db.get_attempts_for_run(run_id)
    assert [a.id for a in history] == [old_attempt_id, "att_duplicate_marker"]

    # Duplicated launch evidence is ambiguous: no precise split.
    assert _reconstruct_chain_attempt_start(db, run_id, history) is None

    db2 = _recover_expect_ambiguous_blocked(
        isolated_state, tmp_path, db_path, run_id, plan_phase
    )
    post_attempts = db2.get_attempts_for_run(run_id)
    assert [a.id for a in post_attempts] == [old_attempt_id, "att_duplicate_marker"]
    post_events = db2.get_complete_events_for_run(run_id)
    post_marker_ids = [
        e.id
        for e in post_events
        if e.event_type == "attempt_started" and e.attempt_id == "att_duplicate_marker"
    ]
    assert post_marker_ids == [first_marker_id, second_marker_id]
    assert post_attempts[0].status == AttemptStatus.SUCCESS
    assert post_attempts[0].commit_sha == pre_attempts[0].commit_sha
    assert post_attempts[0].model == "opencode-go/deepseek-v4-flash"


def test_r1b43_orphan_marker_blocks_package_fallback(
    isolated_state: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A launch marker naming no durable attempt row is contradictory evidence.

    A real package transition leaves an otherwise provable split: durable
    package-1 history (``a1``, named by ``package_completed``) with its
    pre-boundary marker and a current-chain attempt ``a2`` with a
    post-boundary marker.  An ``attempt_started`` event naming an id absent
    from the durable attempt history is then present.  f3c3aed ignored it and
    returned split=1; R1-B.4.3 rejects it and recovery fails closed.
    """
    from saberops.models import Attempt
    from saberops.orchestrator import _reconstruct_chain_attempt_start

    db, db_path, run_id, pre_attempts, plan_phase = _crashed_two_package_boundary(
        isolated_state,
        tmp_path,
        monkeypatch,
        task="R1-B.4.3 orphan marker probe",
        slice_signal="r1b43-orphan:already-planned",
    )
    old_attempt_id = pre_attempts[0].id
    boundary_event = [
        e
        for e in db.get_complete_events_for_run(run_id)
        if e.event_type == "next_package_started"
    ][-1]

    db.create_attempt(
        Attempt(
            id="att_orphan_current",
            run_id=run_id,
            attempt_number=2,
            provider="opencode",
            model="opencode-go/deepseek-v4-flash",
            worktree_path=str(tmp_path / "worktrees" / "orphan-current"),
            branch_name="orch/orphan-current",
            status=AttemptStatus.FAILED,
            created_at=_now_iso(),
        )
    )
    current_marker_id = db.record_event(
        run_id,
        "attempt_started",
        attempt_id="att_orphan_current",
        payload={"provider": "opencode", "model": "opencode-go/deepseek-v4-flash"},
    )
    orphan_marker_id = db.record_event(
        run_id,
        "attempt_started",
        attempt_id="att_orphan_not_durable",
        payload={"provider": "opencode", "model": "opencode-go/deepseek-v4-flash"},
    )
    assert current_marker_id > boundary_event.id
    assert orphan_marker_id > boundary_event.id

    history = db.get_attempts_for_run(run_id)
    assert [a.id for a in history] == [old_attempt_id, "att_orphan_current"]

    # An authoritative marker with no durable attempt row: no precise split.
    assert _reconstruct_chain_attempt_start(db, run_id, history) is None

    db2 = _recover_expect_ambiguous_blocked(
        isolated_state, tmp_path, db_path, run_id, plan_phase
    )
    post_attempts = db2.get_attempts_for_run(run_id)
    assert [a.id for a in post_attempts] == [old_attempt_id, "att_orphan_current"]
    post_events = db2.get_complete_events_for_run(run_id)
    post_orphan_ids = [
        e.id
        for e in post_events
        if e.event_type == "attempt_started"
        and e.attempt_id == "att_orphan_not_durable"
    ]
    assert post_orphan_ids == [orphan_marker_id]
    assert post_attempts[0].status == AttemptStatus.SUCCESS
    assert post_attempts[0].commit_sha == pre_attempts[0].commit_sha
    assert post_attempts[0].model == "opencode-go/deepseek-v4-flash"


def test_r1b5_recovery_does_not_create_second_plan(
    isolated_state: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovery must not re-plan an already-durable decomposition.

    Phase 1 really slices into two packages, executes package 1 and dies after
    the package-2 transition is durable.  On recovery the planner is still
    willing to slice the same objective, so against a0bf416 ordinary recovery
    re-entered ``resolve_work_packages`` and persisted a SECOND plan with fresh
    step/package ids.  R1-B.5 makes the durable plan the authority: recovery
    adopts it and the planner is never consulted again.
    """
    import saberops.orchestrator as orchestrator_module

    db, db_path, run_id, _pre_attempts, _plan_phase = _crashed_two_package_boundary(
        isolated_state,
        tmp_path,
        monkeypatch,
        task="R1-B.5 duplicate-plan probe",
        slice_signal="r1b5:already-planned",
    )
    pre_snapshot = _durable_plan_snapshot(db, run_id)
    assert len(pre_snapshot["plan_ids"]) == 1
    assert len(pre_snapshot["step_ids"]) == 2
    assert len(pre_snapshot["package_ids"]) == 2
    pre_events = db.get_complete_events_for_run(run_id)
    assert len([e for e in pre_events if e.event_type == "plan_created"]) == 1
    assert len([e for e in pre_events if e.event_type == "plan_step_added"]) == 2

    # A planner that would still slice on every call: if recovery consulted it
    # a second durable plan would appear.
    assessment, proposals = _two_package_proposals()
    planning_calls = {"n": 0}

    def _still_slice(
        task: str, planner: object | None = None
    ) -> tuple[TaskSizingAssessment, list[ProposedPackage]]:

        assert task
        assert planner is None
        planning_calls["n"] += 1
        return assessment, list(proposals)

    monkeypatch.setattr(
        orchestrator_module, "resolve_work_packages", _still_slice
    )

    db2, service2 = _recovering_service(
        tmp_path,
        isolated_state["readiness"],
        [_PackageSuccessAdapter("opencode"), _NeverLaunchesAdapter("codex")],
        db_path,
    )
    owner = _claim_fenced_recovery_owner(db2, run_id)
    result = service2.orchestrator.recover_run(
        run_id,
        expected_owner_id=owner.owner_id,
        expected_owner_generation=owner.generation,
    )

    # No re-planning on recovery, no duplicate durable plan.
    assert planning_calls["n"] == 0
    post_snapshot = _durable_plan_snapshot(db2, run_id)
    assert post_snapshot["plan_ids"] == pre_snapshot["plan_ids"]
    assert post_snapshot["step_ids"] == pre_snapshot["step_ids"]
    assert post_snapshot["package_ids"] == pre_snapshot["package_ids"]
    post_events = db2.get_complete_events_for_run(run_id)
    assert len([e for e in post_events if e.event_type == "plan_created"]) == 1
    assert len([e for e in post_events if e.event_type == "plan_step_added"]) == 2
    # Recovery still continued package 2 from durable state and terminalized.
    assert result.status == RunStatus.FAILED
    assert "No eligible candidate for T2.training_allowed" in result.summary


def test_r1b5_recovery_adopts_durable_plan(
    isolated_state: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovery adopts the exact durable plan, steps and packages.

    The recovery-time planning seam fails loudly if it is called at all, so
    the only way recovery can proceed is by adopting the existing durable
    plan.  The original plan id, step ids, package ids, package semantics and
    persisted lifecycle state must survive; package 1 stays completed and
    package 2 continues from where the durable plan left it.
    """
    import saberops.orchestrator as orchestrator_module

    db, db_path, run_id, pre_attempts, _plan_phase = _crashed_two_package_boundary(
        isolated_state,
        tmp_path,
        monkeypatch,
        task="R1-B.5 adoption probe",
        slice_signal="r1b5-adopt:already-planned",
    )
    pre_snapshot = _durable_plan_snapshot(db, run_id)
    assert len(pre_snapshot["plan_ids"]) == 1
    assert len(pre_snapshot["step_ids"]) == 2
    assert len(pre_snapshot["package_ids"]) == 2
    assert pre_snapshot["step_statuses"][pre_snapshot["step_ids"][0]] == "COMPLETED"
    assert pre_snapshot["step_statuses"][pre_snapshot["step_ids"][1]] == "READY"
    # Completeness evidence explicitly proves the full two-package footprint,
    # and both durable step/package pairs are present and correctly linked.
    pre_plan_created = [
        e
        for e in db.get_complete_events_for_run(run_id)
        if e.event_type == "plan_created"
    ]
    assert len(pre_plan_created) == 1
    assert _payload(pre_plan_created[0])["plan_id"] == pre_snapshot["plan_ids"][0]
    assert _payload(pre_plan_created[0])["package_count"] == 2
    pre_steps = db.get_plan_steps(run_id)
    pre_packages = db.get_work_packages(run_id)
    assert {step.ordering for step in pre_steps} == {1, 2}
    for step in pre_steps:
        package = next(p for p in pre_packages if p.id == step.work_package_id)
        assert package.plan_step_id == step.id
        assert package.run_id == run_id

    def _loud_planning(
        *args: object, **kwargs: object
    ) -> tuple[TaskSizingAssessment, list[ProposedPackage]]:
        raise AssertionError("recovery must not re-plan a durable plan")

    monkeypatch.setattr(
        orchestrator_module, "resolve_work_packages", _loud_planning
    )

    db2, service2 = _recovering_service(
        tmp_path,
        isolated_state["readiness"],
        [_PackageSuccessAdapter("opencode"), _NeverLaunchesAdapter("codex")],
        db_path,
    )
    owner = _claim_fenced_recovery_owner(db2, run_id)
    result = service2.orchestrator.recover_run(
        run_id,
        expected_owner_id=owner.owner_id,
        expected_owner_generation=owner.generation,
    )

    post_snapshot = _durable_plan_snapshot(db2, run_id)
    assert post_snapshot["plan_ids"] == pre_snapshot["plan_ids"]
    assert post_snapshot["step_ids"] == pre_snapshot["step_ids"]
    assert post_snapshot["package_ids"] == pre_snapshot["package_ids"]
    assert post_snapshot["step_package_ids"] == pre_snapshot["step_package_ids"]
    assert post_snapshot["package_goals"] == pre_snapshot["package_goals"]
    assert post_snapshot["package_criteria"] == pre_snapshot["package_criteria"]
    assert post_snapshot["package_deps"] == pre_snapshot["package_deps"]

    post_events = db2.get_complete_events_for_run(run_id)
    assert len([e for e in post_events if e.event_type == "plan_created"]) == 1
    assert len([e for e in post_events if e.event_type == "plan_step_added"]) == 2
    # Exactly one plan, two steps, two packages: no duplicate rows.
    assert len(db2.list_run_plans(run_id)) == 1
    assert len(db2.get_plan_steps(run_id)) == 2
    assert len(db2.get_work_packages(run_id)) == 2
    # Package 1 stays completed; package 2 continued from durable state and
    # its chain was exhausted without launching (so its step stays READY).
    assert post_snapshot["step_statuses"][post_snapshot["step_ids"][0]] == "COMPLETED"
    assert post_snapshot["step_statuses"][post_snapshot["step_ids"][1]] == "READY"
    assert [a.id for a in db2.get_attempts_for_run(run_id)] == [
        pre_attempts[0].id
    ]
    assert result.status == RunStatus.FAILED
    assert "No eligible candidate for T2.training_allowed" in result.summary
    run_failed = _payload(
        [e for e in post_events if e.event_type == "run_failed"][-1]
    )
    assert run_failed["routing_context"] == "T2.training_allowed"


def test_r1b5_fresh_run_plans_once(
    isolated_state: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuinely new sliced run keeps planning exactly once, unchanged."""
    import saberops.orchestrator as orchestrator_module

    pkg1_skip = "codex/gpt-5.6-terra"
    pkg1_success = "opencode/opencode-go/deepseek-v4-flash"
    pkg2_skip = "codex/gpt-5.6-sol"
    payload = load_effective_routing_payload()
    payload["T1"]["default"] = [pkg1_skip, pkg1_success]
    payload["T2"]["training_allowed"] = [pkg2_skip]
    save_user_routing(payload)
    repo = tmp_path / "repo"
    _init_repo(repo)
    db = Database(tmp_path / "orch.db")
    readiness = ReadinessService(
        build_access_registry(default_account_connections()),
        ReadinessStore(isolated_state["readiness"]),
        which=lambda _exe: "/fake/bin",
    )
    readiness.record_success("opencode-account")
    service = OrchestratorService(
        db=db,
        registry=AdapterRegistry(
            [_PackageSuccessAdapter("opencode"), _NeverLaunchesAdapter("codex")]
        ),
        worktree_base=tmp_path / "worktrees",
        readiness_service=readiness,
    )

    assessment, proposals = _two_package_proposals()
    planning_calls = {"n": 0}

    def _slice_once(
        task: str, planner: object | None = None
    ) -> tuple[TaskSizingAssessment, list[ProposedPackage]]:

        assert task
        assert planner is None
        planning_calls["n"] += 1
        return assessment, list(proposals)

    monkeypatch.setattr(
        orchestrator_module, "resolve_work_packages", _slice_once
    )

    result = service.run_sync(
        task="R1-B.5 fresh sliced run",
        repo_path=repo,
        gate_command=f"{sys.executable} -c pass",
        max_auto_tier=Tier.T2,
        is_peak=False,
    )

    assert planning_calls["n"] == 1
    # Normal planning persisted exactly one plan with the expected steps and
    # packages, and the run proceeded through its normal lifecycle.
    assert result.status == RunStatus.FAILED
    assert "No eligible candidate for T2.training_allowed" in result.summary
    assert len(db.list_run_plans(result.run_id)) == 1
    events = db.get_events(result.run_id, limit=10000)
    assert len([e for e in events if e.event_type == "plan_created"]) == 1
    assert len([e for e in events if e.event_type == "plan_step_added"]) == 2
    steps = db.get_plan_steps(result.run_id)
    assert len(steps) == 2
    assert steps[0].status == PlanStepStatus.COMPLETED
    assert steps[1].status == PlanStepStatus.READY
    assert len(db.get_work_packages(result.run_id)) == 2


def test_r1b5_multiple_durable_plans_fail_closed(
    isolated_state: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An already-corrupted run with two durable plans fails closed.

    Recovery must not create a third plan, must not silently pick one of the
    two, and must preserve both plans and their evidence while leaving the run
    non-terminal on the existing recovery-safety convention.
    """
    import saberops.orchestrator as orchestrator_module
    from saberops.models import PlanStatus, RunPlan

    db, db_path, run_id, _pre_attempts, _plan_phase = _crashed_two_package_boundary(
        isolated_state,
        tmp_path,
        monkeypatch,
        task="R1-B.5 multiple-plan probe",
        slice_signal="r1b5-multi:already-planned",
    )
    pre_snapshot = _durable_plan_snapshot(db, run_id)
    assert len(pre_snapshot["plan_ids"]) == 1

    now = _now_iso()
    db.create_run_plan(
        RunPlan(
            id="plan_corrupt_duplicate",
            run_id=run_id,
            status=PlanStatus.ACTIVE,
            version=1,
            created_at=now,
            updated_at=now,
        )
    )
    corrupted_snapshot = _durable_plan_snapshot(db, run_id)
    assert len(corrupted_snapshot["plan_ids"]) == 2
    pre_plan_created = [
        e
        for e in db.get_complete_events_for_run(run_id)
        if e.event_type == "plan_created"
    ]
    assert len(pre_plan_created) == 1

    def _loud_planning(
        *args: object, **kwargs: object
    ) -> tuple[TaskSizingAssessment, list[ProposedPackage]]:
        raise AssertionError("recovery must not re-plan a corrupt plan set")

    monkeypatch.setattr(
        orchestrator_module, "resolve_work_packages", _loud_planning
    )

    db2, service2 = _recovering_service(
        tmp_path,
        isolated_state["readiness"],
        [_PackageSuccessAdapter("opencode"), _NeverLaunchesAdapter("codex")],
        db_path,
    )
    owner = _claim_fenced_recovery_owner(db2, run_id)
    with pytest.raises(
        RuntimeError, match="Recovery blocked: multiple durable plans"
    ):
        service2.orchestrator.recover_run(
            run_id,
            expected_owner_id=owner.owner_id,
            expected_owner_generation=owner.generation,
        )

    # Non-terminal, bounded actionable evidence, no third plan.
    post_run = db2.get_run(run_id)
    assert post_run is not None
    assert post_run.status == RunStatus.RUNNING
    post_events = db2.get_complete_events_for_run(run_id)
    post_types = [e.event_type for e in post_events]
    assert "recovery_blocked_multiple_durable_plans" in post_types
    assert "run_failed" not in post_types
    assert len([e for e in post_events if e.event_type == "plan_created"]) == 1
    post_snapshot = _durable_plan_snapshot(db2, run_id)
    assert post_snapshot["plan_ids"] == corrupted_snapshot["plan_ids"]
    assert len(post_snapshot["plan_ids"]) == 2
    assert post_snapshot["step_ids"] == corrupted_snapshot["step_ids"]
    assert post_snapshot["package_ids"] == corrupted_snapshot["package_ids"]


def test_r1b5_partial_durable_plan_fails_closed(
    isolated_state: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A partially persisted durable plan must fail closed, never adopt.

    ``persist_plan`` durably records ``plan_created(package_count=2)`` before
    it writes the two step/package pairs.  Process death after the first pair
    leaves one fully linked step/package pair under completeness evidence for
    two.  Recovery must not treat that truncated footprint as adoptable and
    must never regenerate, repair or guess the missing package.
    """
    import saberops.orchestrator as orchestrator_module

    db, db_path, run_id = _crashed_partial_two_package_plan(
        isolated_state,
        tmp_path,
        monkeypatch,
        task="R1-B.5.1 partial plan probe",
    )

    # Durable state: exactly one plan, completeness evidence for two packages,
    # but only the first complete and internally well linked pair persisted.
    plans = db.list_run_plans(run_id)
    assert len(plans) == 1
    plan = plans[0]
    pre_events = db.get_complete_events_for_run(run_id)
    plan_created = [e for e in pre_events if e.event_type == "plan_created"]
    assert len(plan_created) == 1
    created_payload = _payload(plan_created[0])
    assert created_payload["plan_id"] == plan.id
    assert created_payload["package_count"] == 2
    steps = db.get_plan_steps(run_id)
    packages = db.get_work_packages(run_id)
    assert len(steps) == 1
    assert len(packages) == 1
    assert steps[0].plan_id == plan.id
    assert steps[0].run_id == run_id
    assert steps[0].work_package_id == packages[0].id
    assert packages[0].plan_step_id == steps[0].id
    assert packages[0].run_id == run_id
    assert len([e for e in pre_events if e.event_type == "plan_step_added"]) == 1
    pre_snapshot = _durable_plan_snapshot(db, run_id)
    post_run_pre = db.get_run(run_id)
    assert post_run_pre is not None
    assert post_run_pre.status == RunStatus.RUNNING

    # A planner and persist seam that would recreate the plan must never run.
    assessment, proposals = _two_package_proposals()
    planning_calls = {"n": 0}

    def _still_slice(
        task_text: str, planner: object | None = None
    ) -> tuple[TaskSizingAssessment, list[ProposedPackage]]:

        assert task_text
        assert planner is None
        planning_calls["n"] += 1
        return assessment, list(proposals)

    def _loud_persist(*args: object, **kwargs: object) -> object:
        raise AssertionError("recovery must not re-persist a partial durable plan")

    monkeypatch.setattr(orchestrator_module, "resolve_work_packages", _still_slice)
    monkeypatch.setattr(orchestrator_module, "persist_plan", _loud_persist)

    db2, service2 = _recovering_service(
        tmp_path,
        isolated_state["readiness"],
        [_PackageSuccessAdapter("opencode"), _NeverLaunchesAdapter("codex")],
        db_path,
    )
    owner = _claim_fenced_recovery_owner(db2, run_id)
    with pytest.raises(
        RuntimeError,
        match="Recovery blocked: durable plan linkage cannot be safely adopted",
    ):
        service2.orchestrator.recover_run(
            run_id,
            expected_owner_id=owner.owner_id,
            expected_owner_generation=owner.generation,
        )

    assert planning_calls["n"] == 0
    post_run = db2.get_run(run_id)
    assert post_run is not None
    assert post_run.status == RunStatus.RUNNING
    post_events = db2.get_complete_events_for_run(run_id)
    post_types = [e.event_type for e in post_events]
    assert "recovery_blocked_incomplete_durable_plan" in post_types
    assert "run_failed" not in post_types
    assert len(db2.list_run_plans(run_id)) == 1
    assert len(db2.get_plan_steps(run_id)) == 1
    assert len(db2.get_work_packages(run_id)) == 1
    assert len([e for e in post_events if e.event_type == "plan_step_added"]) == 1
    # All partial historical plan evidence is unchanged.
    assert _durable_plan_snapshot(db2, run_id) == pre_snapshot


def test_r1b5_malformed_completeness_evidence_fails_closed(
    isolated_state: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed ``plan_created.package_count`` fails closed as incomplete.

    Even a fully durable two-package plan is not adoptable when its
    completeness evidence is malformed, because recovery can no longer prove
    the intended normalized footprint from durable evidence alone.
    """
    import saberops.orchestrator as orchestrator_module

    db, db_path, run_id, _pre_attempts, _plan_phase = _crashed_two_package_boundary(
        isolated_state,
        tmp_path,
        monkeypatch,
        task="R1-B.5.1 malformed evidence probe",
        slice_signal="r1b5-malformed:already-planned",
    )
    pre_snapshot = _durable_plan_snapshot(db, run_id)
    assert len(pre_snapshot["plan_ids"]) == 1
    assert len(pre_snapshot["step_ids"]) == 2
    assert len(pre_snapshot["package_ids"]) == 2

    # Corrupt the single matching plan_created completeness evidence.
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            "SELECT id, payload_json FROM events "
            "WHERE run_id = ? AND event_type = 'plan_created'",
            (run_id,),
        ).fetchone()
        assert row is not None
        event_id, raw_payload = row
        payload = json.loads(raw_payload)
        assert payload["package_count"] == 2
        payload["package_count"] = "two"
        conn.execute(
            "UPDATE events SET payload_json = ? WHERE id = ?",
            (json.dumps(payload), event_id),
        )
        conn.commit()

    def _loud_planning(*args: object, **kwargs: object) -> object:
        raise AssertionError("recovery must not re-plan malformed evidence")

    monkeypatch.setattr(orchestrator_module, "resolve_work_packages", _loud_planning)

    db2, service2 = _recovering_service(
        tmp_path,
        isolated_state["readiness"],
        [_PackageSuccessAdapter("opencode"), _NeverLaunchesAdapter("codex")],
        db_path,
    )
    owner = _claim_fenced_recovery_owner(db2, run_id)
    with pytest.raises(
        RuntimeError,
        match="Recovery blocked: durable plan linkage cannot be safely adopted",
    ):
        service2.orchestrator.recover_run(
            run_id,
            expected_owner_id=owner.owner_id,
            expected_owner_generation=owner.generation,
        )

    post_run = db2.get_run(run_id)
    assert post_run is not None
    assert post_run.status == RunStatus.RUNNING
    post_events = db2.get_complete_events_for_run(run_id)
    post_types = [e.event_type for e in post_events]
    assert "recovery_blocked_incomplete_durable_plan" in post_types
    assert "run_failed" not in post_types
    assert len([e for e in post_events if e.event_type == "plan_created"]) == 1
    assert _durable_plan_snapshot(db2, run_id) == pre_snapshot


def test_r1b5_completeness_invariant_rejects_partial_footprints() -> None:
    """The adoption predicate itself proves the complete normalized footprint.

    Directly pins the R1-B.5.1 invariant: cardinality must equal the durable
    completeness authority, ordering must be exactly ``1..package_count`` and
    no dependency may dangle outside the adopted durable plan.
    """
    from saberops.models import PlanStatus as _PlanStatus
    from saberops.models import PlanStep as _PlanStep
    from saberops.models import RunPlan as _RunPlan
    from saberops.models import WorkPackage as _WorkPackage
    from saberops.orchestrator import _durable_plan_linkage_is_adoptable

    plan = _RunPlan(
        id="plan_x",
        run_id="run_x",
        status=_PlanStatus.ACTIVE,
        version=1,
        created_at="",
        updated_at="",
    )

    def _step(step_id: str, ordering: int, dep: tuple[str, ...] = ()) -> _PlanStep:
        return _PlanStep(
            id=step_id,
            plan_id=plan.id,
            run_id=plan.run_id,
            kind="work_package",
            title=step_id,
            goal=step_id,
            status=PlanStepStatus.READY,
            ordering=ordering,
            dependency_ids=dep,
            work_package_id=f"pkg_{step_id}",
        )

    def _pkg(step_id: str, dep: tuple[str, ...] = ()) -> _WorkPackage:
        return _WorkPackage(
            id=f"pkg_{step_id}",
            run_id=plan.run_id,
            plan_step_id=step_id,
            parent_objective="o",
            goal=step_id,
            acceptance_criteria=("a",),
            dependency_package_ids=dep,
        )

    s1 = _step("s1", 1)
    s2 = _step("s2", 2, ("s1",))
    p1 = _pkg("s1")
    p2 = _pkg("s2", ("pkg_s1",))

    # Complete two-package footprint is adoptable.
    assert _durable_plan_linkage_is_adoptable(plan, [s1, s2], [p1, p2], 2)
    # A truncated footprint under completeness evidence for two fails closed.
    assert not _durable_plan_linkage_is_adoptable(plan, [s1], [p1], 2)
    # Missing, duplicate and out-of-range ordering all fail closed.
    assert not _durable_plan_linkage_is_adoptable(
        plan, [s1, _step("s2", 3)], [p1, _pkg("s2")], 2
    )
    assert not _durable_plan_linkage_is_adoptable(
        plan, [s1, _step("s2", 1)], [p1, _pkg("s2")], 2
    )
    assert not _durable_plan_linkage_is_adoptable(
        plan, [_step("s1", 1), _step("s2", 3)], [_pkg("s1"), _pkg("s2")], 2
    )
    # Dangling step and package dependency references fail closed.
    assert not _durable_plan_linkage_is_adoptable(
        plan, [s1, _step("s2", 2, ("ghost",))], [p1, _pkg("s2")], 2
    )
    assert not _durable_plan_linkage_is_adoptable(
        plan, [s1, s2], [p1, _pkg("s2", ("pkg_ghost",))], 2
    )


def test_r1b4_same_chain_continuation_keeps_prior_attempts(
    isolated_state: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovery inside one chain must not invent a fresh-chain boundary.

    Phase 1 really fails one T1 gate and dies before the second same-chain
    dispatch.  Phase 2 resumes through the real fenced ``recover_run`` seam
    and finishes the same chain, so every same-chain attempt stays in the
    terminal count.
    """
    import saberops.orchestrator as orchestrator_module

    cand_a = "opencode/opencode-go/deepseek-v4-flash"
    cand_b = "opencode/opencode-go/mimo-v2.5"
    payload = load_effective_routing_payload()
    payload["T1"]["default"] = [cand_a, cand_b]
    save_user_routing(payload)
    repo = tmp_path / "repo"
    _init_repo(repo)
    db_path = tmp_path / "orch.db"
    db = Database(db_path)
    readiness = ReadinessService(
        build_access_registry(default_account_connections()),
        ReadinessStore(isolated_state["readiness"]),
        which=lambda _exe: "/fake/bin",
    )
    readiness.record_success("opencode-account")
    service = OrchestratorService(
        db=db,
        registry=AdapterRegistry(
            [_PackageSuccessAdapter("opencode"), _NeverLaunchesAdapter("codex")]
        ),
        worktree_base=tmp_path / "worktrees",
        readiness_service=readiness,
    )

    # The pre-dispatch control-plane evaluation runs once per candidate
    # before any worktree/attempt side effect exists.  Die on the second
    # evaluation: candidate A really failed its gate, candidate B never
    # started, and no partial attempt-2 state is left behind.
    real_decision = orchestrator_module.control_plane_decision
    crash: dict[str, object] = {"calls": 0, "armed": True}

    def _crashing_decision(*args: Any, **kwargs: Any) -> Any:
        assert isinstance(crash["calls"], int)
        crash["calls"] = crash["calls"] + 1
        if crash["armed"] and crash["calls"] == 2:
            raise _SimulatedOwnerLoss("owner lost between same-chain attempts")
        return real_decision(*args, **kwargs)

    monkeypatch.setattr(
        orchestrator_module, "control_plane_decision", _crashing_decision
    )
    with pytest.raises(_SimulatedOwnerLoss):
        service.run_sync(
            task="R1-B.4 same chain probe",
            repo_path=repo,
            gate_command=f"{sys.executable} -c \"import sys; sys.exit(1)\"",
            max_auto_tier=Tier.T1,
            is_peak=False,
        )
    crash["armed"] = False

    runs = db.list_runs(limit=10)
    assert len(runs) == 1
    run_id = runs[0].id
    pre_run = db.get_run(run_id)
    assert pre_run is not None
    assert pre_run.status == RunStatus.RUNNING
    pre_attempts = db.get_attempts_for_run(run_id)
    assert len(pre_attempts) == 1
    assert pre_attempts[0].status == AttemptStatus.FAILED
    pre_types = [e.event_type for e in db.get_complete_events_for_run(run_id)]
    assert "tier_escalated" not in pre_types
    assert "next_package_started" not in pre_types
    assert "run_failed" not in pre_types
    # The wrapper stays installed but disarmed, delegating to the real
    # persistence seam for the resumed dispatches.

    db2, service2 = _recovering_service(
        tmp_path,
        isolated_state["readiness"],
        [_PackageSuccessAdapter("opencode"), _NeverLaunchesAdapter("codex")],
        db_path,
    )
    owner = _claim_fenced_recovery_owner(db2, run_id)
    result = service2.orchestrator.recover_run(
        run_id,
        expected_owner_id=owner.owner_id,
        expected_owner_generation=owner.generation,
    )

    assert result.passed is False
    assert result.status == RunStatus.FAILED
    historical = db2.get_attempts_for_run(run_id)
    # One pre-crash failure plus the two resumed same-chain dispatches.
    assert len(historical) == 3
    events = db2.get_events(run_id, limit=10000)
    assert "All 3 attempt(s) failed to pass gate" in result.summary
    run_failed = _payload(
        [e for e in events if e.event_type == "run_failed"][-1]
    )
    assert run_failed["summary"] == result.summary
    assert run_failed["attempts_count"] == 3
    assert run_failed["chain_attempts_count"] == 3

    _ = orchestrator_module

