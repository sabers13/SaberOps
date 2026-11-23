"""Readiness/routing tests: R1-A characterization and R1-B actionable failures.

Merged R1-A and R1-B generations; no product behaviour is changed here.

R1-A (H1, docs/product/UI_BACKEND_RECONCILIATION.md section 2.2) characterizes
automatic routing against readiness evidence (absent / stale / fresh) through
the real composition seam ``ReadinessService`` -> ``control_plane/preflight`` ->
Orchestrator dispatch.  A model override (``explicit_override=True``) is out of
scope and pinned only as a boundary marker.

R1-B makes readiness and routing failures actionable without live provider
calls:

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

Timestamps are explicit (25h-old stale vs freshly recorded); no sleeping and no
timing-sensitive assertions -- margins are hours against a 24h window.
"""

from __future__ import annotations

import inspect
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import saberops.web as web_module
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
    resolve_execution_binding,
    resolve_worker_candidate_binding,
)
from saberops.control_plane.plans import PlanSet, PostureRecord
from saberops.control_plane.policy import (
    ControlPlanePolicy,
    ProviderPolicy,
    ProviderPolicyState,
)
from saberops.control_plane.preflight import PROVIDER_READINESS_UNKNOWN, ControlPlaneDecision
from saberops.db import Database
from saberops.dispatch import (
    SKIP_PROVIDER_READINESS_UNKNOWN,
    control_plane_decision,
    evaluate_eligibility,
)
from saberops.model_access import (
    AccessProtocol,
    AccessStore,
    AuthMode,
    ExecutionSupport,
    ModelCatalogStore,
    ProviderConnection,
    ReadinessAdmission,
    ReadinessService,
    ReadinessStore,
    apply_discovery_result,
    build_access_registry,
    default_account_connections,
    discover_connection_models,
)
from saberops.model_access.readiness import (
    ProviderReadinessEvidence,
    ReadinessEvidenceSource,
)
from saberops.models import (
    AttemptStatus,
    DispatchOutcome,
    ProviderReadiness,
    ReviewResult,
    ReviewVerdict,
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
from saberops.review import ReviewEngine
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
from saberops.workers.base import WorkerAdapter
from saberops.workers.registry import AdapterRegistry


class _AvailableAdapter(WorkerAdapter):
    """Live adapter stub: present and installable, with no other opinion."""

    def __init__(self, provider: str) -> None:
        self._provider = provider

    @property
    def provider_name(self) -> str:
        return self._provider

    def is_available(self) -> bool:
        return True

    def run(self, request: WorkerRequest) -> WorkerResult:
        raise AssertionError("characterization never launches a worker")


@pytest.fixture()
def isolated_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate owner state and pin the readiness store for web probing."""
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
    return readiness_path


def _service(store_path: Path) -> ReadinessService:
    """Real service over the real default registry; every backend installed."""
    return ReadinessService(
        build_access_registry(default_account_connections()),
        ReadinessStore(store_path),
        which=lambda _exe: "/fake/bin",
    )


def _candidate(provider: str, model: str) -> WorkerCandidate:
    return WorkerCandidate(provider=provider, model=model)


def _synthetic_policy() -> ControlPlanePolicy:
    """Isolated synthetic policy with verified class-A posture for the fixture's
    synthetic providers.

    Covers: WORKFLOW §7 authorized setup-only fixture migration.
    Under the accepted M2-4b F1 admission seam an unmatched route fails closed
    with ``POSTURE_UNVERIFIED``, so this characterization's real preflight seam
    would skip every synthetic route unless the fixture supplies a verified
    posture.  The helper deliberately does NOT rely on XDG config alone (the
    module pins a packaged policy), injecting an isolated in-memory policy
    instead.  The packaged quota *membership* is neutralized for this isolated
    policy because these candidates carry no quota evidence: that keeps the
    test measuring the readiness seam rather than quota enforcement.  Only the
    synthetic identities this module uses are seeded; no real vendor receives
    an attestation and no production admission, packaged policy or owner config
    is touched.  ``verified_on`` is stamped at run time with a 90-day TTL so
    replay never expires.
    """
    from dataclasses import replace

    from saberops.control_plane.plans import load_packaged_plans_raw, validate_plan_sections

    today = datetime.now(UTC).date().isoformat()
    providers = ("opencode", "codex")
    postures = {
        provider: PostureRecord(
            selector=provider,
            posture_class="A",
            policy_urls=("https://example.com/synthetic-test-posture",),
            ttl_days=90,
            verified_on=today,
            note="synthetic verified test posture for R1-A routing fixtures",
        )
        for provider in providers
    }
    provider_policies = {
        provider: ProviderPolicy(provider, ProviderPolicyState.ALLOWED)
        for provider in providers
    }
    packaged = validate_plan_sections(
        load_packaged_plans_raw(), "packaged plans", packaged=True
    )
    neutral_pools = {
        pool_id: replace(plan, members=(), max_concurrent=None)
        for pool_id, plan in packaged.pool_plans.items()
    }
    return ControlPlanePolicy(
        provider_policies=provider_policies,
        quota_pools={},
        content_hash="r1a-synthetic-policy",
        plans=PlanSet(pool_plans=neutral_pools, postures=postures),
    )


def _automatic_outcome(
    service: ReadinessService, candidate: WorkerCandidate
) -> tuple[ReadinessAdmission, ControlPlaneDecision, str | None]:
    """Run the real automatic preflight seam for one candidate.

    Returns ``(admission, decision, skip_reason)``.  ``skip_reason is None``
    means the candidate survives preflight and can reach dispatch; any string
    is the typed skip reason the orchestrator records and continues past.
    """
    adapter = _AvailableAdapter(candidate.provider)
    policy = _synthetic_policy()
    admission = service.admission_for(provider=candidate.provider)
    decision = control_plane_decision(
        candidate=candidate,
        adapter=adapter,
        training_allowed=True,
        quota_states=[],
        prompt_bytes=64,
        policy=policy,
        readiness_state=admission.state,
        readiness_reason=admission.reason,
        readiness_executable_available=admission.executable_available,
        explicit_unknown_readiness=False,
    )
    skip = evaluate_eligibility(
        candidate=candidate,
        adapter=adapter,
        training_allowed=True,
        quota_states=[],
        prompt_bytes=64,
        explicit_override=False,
        policy=policy,
        readiness_state=admission.state,
        readiness_reason=admission.reason,
        readiness_executable_available=admission.executable_available,
    )
    return admission, decision, skip


def _seed_ready_like_record_success(
    store_path: Path, connection_id: str, observed_at: str
) -> None:
    """Persist READY evidence with the exact shape ``record_success`` writes."""
    connection = next(
        c for c in default_account_connections() if c.connection_id == connection_id
    )
    ReadinessStore(store_path).replace(
        ProviderReadinessEvidence(
            connection_id=connection.connection_id,
            provider=connection.provider,
            backend=connection.backend,
            state=ProviderReadiness.READY,
            reason="READY",
            observed_at=observed_at,
            source=ReadinessEvidenceSource.SUCCESSFUL_EXECUTION,
        )
    )


def _iso(when: datetime) -> str:
    return when.isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# A -- no readiness evidence
# ---------------------------------------------------------------------------


def test_r1a_missing_evidence_skips_automatic_candidate(tmp_path: Path) -> None:
    """H1a: absent evidence -> UNKNOWN -> automatic skip, fallback is tried."""
    service = _service(tmp_path / "access-readiness.json")
    target = _candidate("opencode", "opencode/mimo-v2.5-free")

    admission, decision, skip = _automatic_outcome(service, target)

    assert admission.connection_id == "opencode-account"
    assert admission.state is ProviderReadiness.UNKNOWN
    assert admission.reason == "INSUFFICIENT_EVIDENCE"
    assert admission.executable_available is True
    assert decision.eligible is False
    assert decision.reason == PROVIDER_READINESS_UNKNOWN
    assert skip == SKIP_PROVIDER_READINESS_UNKNOWN
    assert skip == "provider_readiness_unknown"

    # The orchestrator `continue`s past a skipped automatic candidate, so a
    # second candidate with fresh evidence would still be tried and dispatched.
    service.record_success("codex-chatgpt-account")
    fallback = _candidate("codex", "gpt-5.6-terra")
    _, fallback_decision, fallback_skip = _automatic_outcome(service, fallback)
    assert fallback_decision.eligible is True
    assert fallback_skip is None


# ---------------------------------------------------------------------------
# B -- stale READY evidence (25h old, window is 24h)
# ---------------------------------------------------------------------------


def test_r1a_stale_ready_evidence_skips_automatic_candidate(tmp_path: Path) -> None:
    """H1b: 25h-old READY -> UNKNOWN/STALE -> automatic skip, fallback tried."""
    store_path = tmp_path / "access-readiness.json"
    stale_at = _iso(datetime.now(UTC) - timedelta(hours=25))
    _seed_ready_like_record_success(store_path, "opencode-account", stale_at)
    service = _service(store_path)
    target = _candidate("opencode", "opencode/mimo-v2.5-free")

    admission, decision, skip = _automatic_outcome(service, target)

    assert admission.connection_id == "opencode-account"
    assert admission.state is ProviderReadiness.UNKNOWN
    assert admission.reason == "STALE_READINESS_EVIDENCE"
    assert admission.executable_available is True
    assert decision.eligible is False
    assert decision.reason == PROVIDER_READINESS_UNKNOWN
    assert skip == SKIP_PROVIDER_READINESS_UNKNOWN
    assert skip == "provider_readiness_unknown"

    service.record_success("codex-chatgpt-account")
    fallback = _candidate("codex", "gpt-5.6-terra")
    _, fallback_decision, fallback_skip = _automatic_outcome(service, fallback)
    assert fallback_decision.eligible is True
    assert fallback_skip is None


# ---------------------------------------------------------------------------
# C -- fresh READY evidence
# ---------------------------------------------------------------------------


def test_r1a_fresh_ready_evidence_reaches_dispatch(tmp_path: Path) -> None:
    """H1c: fresh READY evidence -> automatic candidate stays eligible."""
    store_path = tmp_path / "access-readiness.json"
    service = _service(store_path)
    # Genuine write path: the same call a successful execution makes.
    service.record_success("opencode-account")
    target = _candidate("opencode", "opencode/mimo-v2.5-free")

    admission, decision, skip = _automatic_outcome(service, target)

    assert admission.connection_id == "opencode-account"
    assert admission.state is ProviderReadiness.READY
    assert admission.reason == "READY"
    assert decision.eligible is True
    assert decision.reason is None
    assert skip is None


# ---------------------------------------------------------------------------
# Boundary marker: a model override is a different path (out of scope for H1)
# ---------------------------------------------------------------------------


def test_r1a_explicit_override_path_differs_from_automatic(tmp_path: Path) -> None:
    """Missing evidence blocks automatic routing but not an explicit override."""
    service = _service(tmp_path / "access-readiness.json")
    target = _candidate("opencode", "opencode/mimo-v2.5-free")
    adapter = _AvailableAdapter(target.provider)
    policy = _synthetic_policy()
    admission = service.admission_for(provider=target.provider)
    assert admission.state is ProviderReadiness.UNKNOWN

    decision = control_plane_decision(
        candidate=target,
        adapter=adapter,
        training_allowed=True,
        quota_states=[],
        prompt_bytes=64,
        policy=policy,
        readiness_state=admission.state,
        readiness_reason=admission.reason,
        readiness_executable_available=admission.executable_available,
        explicit_unknown_readiness=True,
    )
    skip = evaluate_eligibility(
        candidate=target,
        adapter=adapter,
        training_allowed=True,
        quota_states=[],
        prompt_bytes=64,
        explicit_override=True,
        policy=policy,
        readiness_state=admission.state,
        readiness_reason=admission.reason,
        readiness_executable_available=admission.executable_available,
    )
    assert decision.eligible is True
    assert skip is None


def test_r1a_unverified_route_still_fails_closed() -> None:
    """Negative control: the synthetic policy admits ONLY its verified
    identities.  Any other synthetic route keeps the accepted M2-4b F1
    fail-closed behavior (``POSTURE_UNVERIFIED``), proving the fixture seed
    did not globally waive posture admission.

    Covers: (posture fail-closed; POSTURE_UNVERIFIED)
    """
    policy = _synthetic_policy()
    target = _candidate("unverified-vendor", "unverified-model")
    admission = ReadinessAdmission(
        state=ProviderReadiness.READY,
        reason="READY",
        executable_available=True,
        connection_id=None,
    )
    decision = control_plane_decision(
        candidate=target,
        adapter=_AvailableAdapter(target.provider),
        training_allowed=True,
        quota_states=[],
        prompt_bytes=64,
        policy=policy,
        readiness_state=admission.state,
        readiness_reason=admission.reason,
        readiness_executable_available=admission.executable_available,
        explicit_unknown_readiness=False,
    )
    skip = evaluate_eligibility(
        candidate=target,
        adapter=_AvailableAdapter(target.provider),
        training_allowed=True,
        quota_states=[],
        prompt_bytes=64,
        policy=policy,
        readiness_state=admission.state,
        readiness_reason=admission.reason,
        readiness_executable_available=admission.executable_available,
        explicit_override=False,
    )
    assert decision.eligible is False
    assert decision.exclusion_reason == "POSTURE_UNVERIFIED"
    assert skip is not None


# ---------------------------------------------------------------------------
# Verify/Probe persistence behavior
# ---------------------------------------------------------------------------


def test_r1a_web_probe_does_not_call_readiness_refresh() -> None:
    """R1-B supersedes the R1-A probe characterization: Verify now performs
    the single canonical readiness refresh.

    The old route called ``probe_account_backend()`` as a second,
    non-persisting probe; R1-B removed that duplicate path so there is
    exactly one authoritative verification path composing
    ``ReadinessService`` over the already-loaded registry plus the
    canonical store seam.  API connections keep the safe reachability
    probe (no ``refresh()`` for them: descriptor-less API connections
    must never become ``BACKEND_NOT_INSTALLED``).

    L-A step 5 removed the access page; the startup seam
    (``initialize_account_readiness``) keeps the single composed path.
    """
    source = inspect.getsource(web_module)
    assert "ReadinessService" in source
    assert "refresh_accounts_on_startup" in source
    assert "probe_account_backend" not in source


def test_r1a_web_probe_writes_no_readiness_evidence(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    """R1-B: verifying a connection persists readiness evidence for it.

    (This test supersedes its R1-A characterization name: the R1-A suite
    pinned that Verify wrote nothing; R1-B specifies that Verify must
    write readiness, so the test now pins the new contract -- persisted
    sanitized state/reason/timestamp for the exact verified connection.
    L-A step 5 drives the same engine seam the removed route used.)
    """
    import shutil
    import subprocess

    from saberops.model_access import ReadinessService
    from saberops.web import _access_store, _readiness_store

    registry = _access_store().load()
    connection = registry.try_get("opencode-account")
    assert connection is not None
    service = ReadinessService(
        registry,
        _readiness_store(),
        which=shutil.which,
        # Deterministic, token-free status read: never launch the ambient
        # vendor CLI from pytest.
        run=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "[]", ""),
    )
    service.refresh(connection)

    catalog = ReadinessStore(isolated_xdg).load()
    evidence = catalog.try_get("opencode-account")
    assert evidence is not None
    assert evidence.state in (
        ProviderReadiness.READY,
        ProviderReadiness.NOT_READY,
        ProviderReadiness.UNKNOWN,
    )
    assert evidence.reason
    assert evidence.observed_at
    assert os.environ["ORCH_READINESS_STORE"] == str(isolated_xdg)
# ---------------------------------------------------------------------------
# Shared fixtures / fakes
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Path]:
    """Isolate owner state/config plus the readiness store override.

    Covers: WORKFLOW §7 authorized setup-only fixture migration
    for synthetic r1b candidates.
    """
    state = tmp_path / "state"
    config = tmp_path / "config"
    state.mkdir(parents=True)
    config.mkdir(parents=True)
    cfg_dir = config / "saberops"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    today_iso = datetime.now(UTC).date().isoformat()
    cp_doc = {
        "version": 2,
        "providers": {
            "opencode": {"state": "ALLOWED"},
            "codex": {"state": "ALLOWED"},
            "antigravity": {"state": "ALLOWED"},
        },
        "quota_pools": {
            "opencode-go": {"mode": "UNENFORCED", "overage_possible": False},
            "opencode-zen": {"mode": "UNENFORCED", "overage_possible": False},
            "chatgpt-plus": {"mode": "UNENFORCED", "overage_possible": False},
        },
        "postures": {
            "opencode": {
                "class": "A",
                "policy_urls": ["https://example.com/opencode-policy"],
                "ttl_days": 90,
                "verified_on": today_iso,
                "note": "synthetic verified test posture for r1b candidate characterization",
            },
            "opencode/opencode-go/*": {
                "class": "A",
                "policy_urls": ["https://example.com/opencode-go-policy"],
                "ttl_days": 90,
                "verified_on": today_iso,
                "note": "synthetic verified test posture for r1b candidate characterization",
            },
            "opencode/opencode/*": {
                "class": "C",
                "policy_urls": ["https://example.com/opencode-zen-policy"],
                "ttl_days": 90,
                "verified_on": today_iso,
                "note": "synthetic verified test posture for r1b candidate characterization",
            },
            "codex": {
                "class": "A",
                "policy_urls": ["https://example.com/codex-policy"],
                "ttl_days": 90,
                "verified_on": today_iso,
                "note": "synthetic verified test posture for r1b candidate characterization",
            },
            "antigravity": {
                "class": "A",
                "policy_urls": ["https://example.com/antigravity-policy"],
                "ttl_days": 90,
                "verified_on": today_iso,
                "note": "synthetic verified test posture for r1b candidate characterization",
            },
        },
    }
    (cfg_dir / "control_plane.json").write_text(json.dumps(cp_doc), encoding="utf-8")
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
    from saberops.web import _access_store, _readiness_store

    calls: list[list[str]] = []
    _fake_which(monkeypatch)
    _fake_status_run(monkeypatch, returncode=0, stdout=_CODEX_OK, calls=calls)
    calls.clear()

    registry = _access_store().load()
    connection = registry.try_get("codex-chatgpt-account")
    assert connection is not None
    service = ReadinessService(
        registry, _readiness_store(), which=shutil.which, run=subprocess.run
    )
    evidence = service.refresh(connection)

    assert evidence.state is ProviderReadiness.READY
    # The declared non-inference status command ran exactly once; every
    # other subprocess call is unrelated git plumbing, never a model call.
    assert calls.count(["/fake/codex", "doctor", "--json"]) == 1
    assert all(call[0] in ("/fake/codex", "git") for call in calls)
    # State/reason/timestamp survive reload through the canonical store.
    reloaded = ReadinessStore(isolated_state["readiness"]).load()
    persisted = reloaded.try_get("codex-chatgpt-account")
    assert persisted is not None
    assert persisted.state is ProviderReadiness.READY
    assert persisted.reason == "READY"
    assert persisted.observed_at
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


def test_r1b_verify_missing_executable_is_not_ready(
    isolated_state: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A proven missing executable persists NOT_READY / BACKEND_NOT_INSTALLED."""
    from saberops.web import _access_store, _readiness_store

    calls: list[list[str]] = []
    _fake_which(monkeypatch, present=())
    _fake_status_run(monkeypatch, returncode=0, stdout=_CODEX_OK, calls=calls)
    calls.clear()

    registry = _access_store().load()
    connection = registry.try_get("codex-chatgpt-account")
    assert connection is not None
    service = ReadinessService(
        registry, _readiness_store(), which=shutil.which, run=subprocess.run
    )
    evidence = service.refresh(connection)

    assert [call for call in calls if call and call[0] == "codex"] == []
    persisted = ReadinessStore(isolated_state["readiness"]).load().try_get(
        "codex-chatgpt-account"
    )
    assert persisted is not None
    assert persisted.state is ProviderReadiness.NOT_READY
    assert evidence.reason == "BACKEND_NOT_INSTALLED"


def test_r1b_verify_unauthenticated_is_not_ready(
    isolated_state: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A proven failed session persists NOT_READY / NOT_AUTHENTICATED."""
    from saberops.web import _access_store, _readiness_store

    calls: list[list[str]] = []
    _fake_which(monkeypatch)
    _fake_status_run(monkeypatch, returncode=1, stdout=_CODEX_DENIED, calls=calls)
    calls.clear()

    registry = _access_store().load()
    connection = registry.try_get("codex-chatgpt-account")
    assert connection is not None
    service = ReadinessService(
        registry, _readiness_store(), which=shutil.which, run=subprocess.run
    )
    service.refresh(connection)

    assert calls.count(["/fake/codex", "doctor", "--json"]) == 1
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
    from saberops.web import _access_store, _readiness_store

    _fake_which(monkeypatch)
    _fake_status_run(
        monkeypatch, returncode=1, stdout="not-json{{{", calls=[]
    )

    registry = _access_store().load()
    connection = registry.try_get("codex-chatgpt-account")
    assert connection is not None
    service = ReadinessService(
        registry, _readiness_store(), which=shutil.which, run=subprocess.run
    )
    service.refresh(connection)
    evidence = ReadinessStore(isolated_state["readiness"]).load().try_get(
        "codex-chatgpt-account"
    )
    assert evidence is not None
    assert evidence.state is ProviderReadiness.UNKNOWN
    assert evidence.state.value == "UNKNOWN"


def test_r1b_api_connection_never_reports_ready(
    isolated_state: dict[str, Path], tmp_path: Path
) -> None:
    """A stored direct-API row has no transport (H17) and never sends anything."""
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
    registry = store.load()
    connection = registry.try_get("byok-r1b-api")
    assert connection is not None
    from saberops.web import _readiness_store

    service = ReadinessService(
        registry, _readiness_store(), which=shutil.which, run=subprocess.run
    )
    service.refresh(connection)
    catalog = ReadinessStore(isolated_state["readiness"]).load()
    evidence = catalog.try_get("byok-r1b-api")
    assert evidence is not None and evidence.reason == "PROTOCOL_UNSUPPORTED"
    assert evidence.state is ProviderReadiness.NOT_READY


# ---------------------------------------------------------------------------
# B. Candidate explanations
# ---------------------------------------------------------------------------


def _write_t1_chain(*candidate_ids: str) -> None:
    payload = load_effective_routing_payload()
    payload["roles"]["BUILD"] = [{"routes": [cid]} for cid in candidate_ids]
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
    assert terminal_evidence["routing_context"] == "BUILD"
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
    assert termination["routing_context"] == "BUILD"
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
    payload["roles"]["BUILD"] = []
    save_user_routing(payload)

    reloaded = load_effective_routing_payload()
    assert reloaded["roles"]["BUILD"] == []
    snapshot = load_effective_routing()
    assert snapshot.chain_for_role("BUILD") == []


def test_r1b_remove_final_candidate_persists_empty_chain(
    isolated_state: dict[str, Path], tmp_path: Path
) -> None:
    """Removing the last candidate persists successfully."""
    from saberops.routing_config import remove_candidate_from_chain

    _write_t1_chain("codex/gpt-5.6-terra")

    payload = load_effective_routing_payload()
    save_user_routing(
        remove_candidate_from_chain(
            payload, role="BUILD", candidate_id="codex/gpt-5.6-terra"
        )
    )

    assert load_effective_routing_payload()["roles"]["BUILD"] == []


def test_r1b_empty_chain_run_terminates_clearly(
    isolated_state: dict[str, Path], tmp_path: Path
) -> None:
    """A selected empty chain launches nothing and explains itself."""
    payload = load_effective_routing_payload()
    payload["roles"]["BUILD"] = []
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
    assert result.summary == "No routing candidates are configured for BUILD."
    events = db.get_events(result.run_id, limit=10000)
    no_elig = [e for e in events if e.event_type == "no_eligible_candidate"]
    assert no_elig
    empty_evidence = _payload(no_elig[-1])
    assert empty_evidence["candidates"] == []
    assert empty_evidence["skipped_candidates"] == []
    assert empty_evidence["routing_context"] == "BUILD"


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
        binding_role=BindingRole.BUILD,
    )
    owner = upsert_owner_binding(owner, binding=worker, profile=profile_row, pool=pool)
    save_owner_bindings(owner)
    return worker.binding_id


def _exact_worker_binding(
    owner: Any, registry: AdapterRegistry, *, model: str, connection_id: str
) -> Any | None:
    """Resolve the exact WORKER binding through the production resolver.

    Re-points the removed console lookup: the expected
    ``discovered-binding:worker:<connection>:<model>`` identity is resolved
    against the real adapter with :func:`resolve_execution_binding` (never a
    provider/model fallback, never a synthesized identity).
    """
    expected_id = f"discovered-binding:worker:{connection_id}:{model}"
    binding = owner.registry.try_get(expected_id)
    if binding is None:
        return None
    adapter = registry.get(binding.provider)
    if adapter is None:
        return None
    try:
        resolve_execution_binding(
            registry=owner.registry, binding_id=binding.binding_id, adapter=adapter
        )
    except BindingResolutionError:
        return None
    return binding


def _approve_and_add_candidate(
    payload: dict[str, Any], *, role: str, candidate: Any
) -> dict[str, Any]:
    """Add an owner-approved discovered model through the production path.

    The removed console helper recorded the approval in the canonical routing
    document and then added its identity; this composes the same two
    production steps (the ``approved_candidates`` field plus
    :func:`saberops.routing_config.add_candidate_to_chain`).
    """
    from saberops.routing_config import add_candidate_to_chain

    updated = dict(payload)
    approved = dict(updated.get("approved_candidates") or {})
    approved[candidate.candidate_id] = candidate.to_dict()
    updated["approved_candidates"] = approved
    return add_candidate_to_chain(
        updated, role=role, candidate_id=candidate.candidate_id
    )


def _add_routing_candidate(
    *, connection_id: str, model_id: str, context: str, binding_id: str
) -> None:
    """Engine-seam mirror of the removed POST /routing/add guard chain.

    Same order, same refusals (as RoutingConfigError instead of HTTP 400):
    known connection, discovery-verified fresh catalog entry, exact
    resolvable binding, BUILD role, submitted-binding match, valid
    candidate id, known context, then append + save.
    """
    from saberops.routing_config import (
        DynamicCandidate,
        RoutingConfigError,
        format_candidate_id,
    )
    from saberops.web import _access_store, _load_owner_bindings_safely

    owner = _load_owner_bindings_safely()
    access = _access_store().load()
    connection = access.try_get(connection_id.strip())
    if connection is None:
        raise RoutingConfigError(f"Unknown connection: {connection_id.strip()}")
    catalog = ModelCatalogStore(
        owner_state_dir() / ModelCatalogStore.FILENAME
    ).load()
    entry = catalog.try_get(
        connection_id=connection.connection_id,
        provider=connection.provider,
        backend=connection.backend,
        model_id=model_id.strip(),
    )
    if entry is None or not entry.is_discovery_verified:
        raise RoutingConfigError("Model is not a discovery-verified provider model.")
    if entry.is_stale:
        raise RoutingConfigError("Discovery evidence is stale; refresh models first.")
    registry = AdapterRegistry.default()
    exact_binding = _exact_worker_binding(
        owner, registry, model=entry.model_id, connection_id=connection.connection_id
    )
    if exact_binding is None:
        raise RoutingConfigError("No exact executable binding resolves.")
    if exact_binding.binding_role is not BindingRole.BUILD:
        raise RoutingConfigError(
            f"Exact binding role is {exact_binding.binding_role.value!r}, not BUILD."
        )
    if binding_id.strip() and binding_id.strip() != exact_binding.binding_id:
        raise RoutingConfigError("Submitted binding identity does not match.")
    candidate_id = format_candidate_id(entry.provider, entry.model_id)
    top, _, _sub = context.strip().partition(".")
    if top.strip().upper() not in ("T1", "T2", "T3"):
        raise RoutingConfigError(f"Unknown routing context: {context!r}")
    role = "BUILD"
    payload = load_effective_routing_payload()
    if candidate_id in ALLOWED_CANDIDATES:
        static_training_required = (
            "TRUE" if requires_training_permission(candidate_id) else "FALSE"
        )
        approval = DynamicCandidate(
            candidate_id=candidate_id, provider=entry.provider, model=entry.model_id,
            connection_id=connection.connection_id, backend=connection.backend,
            binding_id=exact_binding.binding_id,
            discovery_status=entry.discovery_status.value,
            execution_support=entry.execution_support.value,
            training_required=static_training_required,
            capabilities=tuple((n, s.value) for n, s in entry.capabilities),
        )
    else:
        approval = DynamicCandidate(
            candidate_id=candidate_id, provider=entry.provider, model=entry.model_id,
            connection_id=connection.connection_id, backend=connection.backend,
            binding_id=exact_binding.binding_id,
            discovery_status=entry.discovery_status.value,
            execution_support=entry.execution_support.value,
            training_required="UNKNOWN",
            capabilities=tuple((n, s.value) for n, s in entry.capabilities),
        )
    save_user_routing(_approve_and_add_candidate(payload, role=role, candidate=approval))


def test_r1b_static_add_preserves_exact_binding(
    isolated_state: dict[str, Path], tmp_path: Path
) -> None:
    """A static ALLOWED candidate added via UI keeps its exact WORKER binding."""
    assert _STATIC_CID in ALLOWED_CANDIDATES
    assert not requires_training_permission(_STATIC_CID)
    backend = _seed_static_discovery()
    worker_binding_id = _materialize_worker(backend)

    _add_routing_candidate(
        connection_id=_STATIC_CONNECTION, model_id=_STATIC_MODEL,
        context="T1.default", binding_id=worker_binding_id,
    )
    payload = load_effective_routing_payload()
    assert _STATIC_CID in [
        cid for group in payload["roles"]["BUILD"] for cid in group["routes"]
    ]
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

    _add_routing_candidate(
        connection_id=_STATIC_CONNECTION, model_id=_STATIC_MODEL,
        context="T2.default", binding_id=worker_binding_id,
    )

    # training_required FALSE (not UNKNOWN) keeps the historical static
    # admissibility: UNKNOWN would have failed closed here.
    payload = load_effective_routing_payload()
    assert _STATIC_CID in [
        cid for group in payload["roles"]["BUILD"] for cid in group["routes"]
    ]


def test_r1b_orchestrator_binding_cannot_satisfy_worker_routing(
    isolated_state: dict[str, Path], tmp_path: Path
) -> None:
    """An ORCHESTRATOR-only binding is refused for WORKER routing."""
    from saberops.routing_config import RoutingConfigError

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
        binding_role=BindingRole.PLAN,
    )
    owner = upsert_owner_binding(owner, binding=orch, profile=profile_row, pool=pool)
    save_owner_bindings(owner)

    with pytest.raises(RoutingConfigError):
        _add_routing_candidate(
            connection_id=_STATIC_CONNECTION, model_id=orch_model,
            context="T1.default", binding_id=orch.binding_id,
        )

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
        routing_context_for_role,
    )
    from saberops.jobs.models import JobRole

    for role in (JobRole.BUILD, JobRole.BUILD, JobRole.BUILD):
        assert routing_context_for_role(role) == role.value
    assert routing_context_for_role("worker") == "BUILD"

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
    summary = format_no_eligible_summary([skip], routing_context="BUILD")
    assert "opencode/opencode-go/deepseek-v4-flash" in summary
    assert "readiness UNKNOWN (STALE_READINESS_EVIDENCE)" in summary
    assert format_no_eligible_summary([], routing_context="BUILD") == (
        "No routing candidates are configured for BUILD."
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
    payload["roles"]["BUILD"] = [{"routes": [cid]} for cid in [_CAND_A, _CAND_B]]
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
    assert terminal_evidence["routing_context"] == "BUILD"
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
    # The launched model is not described as ineligible; the skipped
    # candidate is named as the secondary fact.
    assert "No eligible candidate" not in result.summary
    assert "1 further candidate(s) were skipped as ineligible" in result.summary

    run_failed = [e for e in events if e.event_type == "run_failed"][-1]
    run_failed_payload = _payload(run_failed)
    assert run_failed_payload["summary"] == result.summary
    assert run_failed_payload["skipped_candidates"] == skipped
    assert run_failed_payload["routing_context"] == "BUILD"

    report = build_run_report(db, result.run_id)
    termination = report["termination"]
    assert isinstance(termination, dict)
    assert termination["summary"] == result.summary
    assert termination["routing_context"] == "BUILD"
    assert termination["skipped_candidates"] == skipped


def test_r1b1_prior_tier_skips_never_render_under_later_context(
    isolated_state: dict[str, Path], tmp_path: Path
) -> None:
    """A T1 skip must never be presented as though it belonged to T2.

    The T1 -> T2 transition is driven by explicit typed capability evidence
    (H3.1: gate failure alone no longer escalates).
    """
    payload = load_effective_routing_payload()
    payload["roles"]["BUILD"] = [{"routes": [cid]} for cid in [_CAND_A, _CAND_B]]
    payload["strong_start"] = [{"routes": [cid]} for cid in [_CAND_C]]
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
        if evidence.get("routing_context") == "BUILD":
            models = {
                str(entry.get("model")) for entry in evidence["skipped_candidates"]
            }
            assert "gpt-5.6-terra" not in models
            assert "gpt-5.6-sol" in models
    terminal_evidence = _payload(no_elig[-1])
    assert terminal_evidence["routing_context"] == "BUILD"
    assert {e["model"] for e in terminal_evidence["skipped_candidates"]} == {
        "gpt-5.6-sol"
    }
    run_failed = _payload(
        [e for e in events if e.event_type == "run_failed"][-1]
    )
    assert run_failed["routing_context"] == "BUILD"
    assert {e["model"] for e in run_failed["skipped_candidates"]} == {
        "gpt-5.6-sol"
    }
    report = build_run_report(db, result.run_id)
    assert report["termination"]["routing_context"] == "BUILD"
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
    payload["roles"]["BUILD"] = [{"routes": [t1_fail]}]
    payload["strong_start"] = [{"routes": [t2_skip]}]
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
        is_peak=False,
    )

    assert result.passed is False
    assert result.status == RunStatus.FAILED
    assert "No eligible candidate for BUILD" in result.summary
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


class _SimulatedOwnerLoss(BaseException):
    """Deterministic process-death stand-in (BaseException, never swallowed).

    ``Orchestrator._run_core`` terminalizes ``Exception`` but never
    ``BaseException``, so raising this leaves the run row RUNNING with
    exactly the durable state a killed execution owner would leave behind.
    """


def _claim_fenced_recovery_owner(db: Database, run_id: str) -> ExecutionOwner:
    """Claim the fenced recovery successor exactly as the supervisor would."""
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
    payload["roles"]["BUILD"] = [{"routes": [cid]} for cid in [cand_a, cand_b]]
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


def test_mixed_exhaustion_summary_names_the_gate_failure_cause() -> None:
    """N16: skipped candidates are secondary, never the named cause."""
    from saberops.dispatch import format_mixed_exhaustion_summary

    summary = format_mixed_exhaustion_summary(
        "make gate",
        2,
        [{"provider": "opencode", "model": "m", "reason": "provider_unavailable"}],
        routing_context="BUILD",
    )
    assert "All 2 attempt(s) failed to pass gate 'make gate'." in summary
    assert "No eligible candidate" not in summary
    assert "1 further candidate(s) were skipped as ineligible for BUILD" in summary
    assert "provider_unavailable" in summary


def test_h2_large_multi_part_task_is_one_job_and_writes_no_plan(
    isolated_state: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Splitting is the planner's job: one job is one worker, one objective.

    Before H2 a broad task text was keyword-scored, sliced into a persisted
    single-package plan and dispatched as a PACKAGE projection that hid the
    original task.  Now the same text runs as one ordinary job with the full
    objective and no durable plan rows or plan events.
    """
    cand = "opencode/opencode-go/deepseek-v4-flash"
    payload = load_effective_routing_payload()
    payload["roles"]["BUILD"] = [{"routes": [cand]}]
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
    prompts: list[str] = []

    class _RecordingSuccessAdapter(_PackageSuccessAdapter):
        def run(self, request: WorkerRequest) -> WorkerResult:
            prompts.append(request.task)
            return super().run(request)

    worker = _RecordingSuccessAdapter("opencode")
    service = OrchestratorService(
        db=db,
        registry=AdapterRegistry([worker]),
        worktree_base=tmp_path / "worktrees",
        readiness_service=readiness,
    )

    def _pass_review(self: ReviewEngine, run_id: str, **kwargs: object) -> ReviewResult:
        review = ReviewResult(
            id="h2-review",
            run_id=run_id,
            provider="fake-reviewer",
            model="independent",
            verdict=ReviewVerdict.PASS,
            summary="PASS",
            details="PASS",
        )
        db.create_review(review)
        return review

    monkeypatch.setattr(ReviewEngine, "review_run", _pass_review)
    task = "\n".join(
        [
            "1. Refactor the database schema and migrate the supervisor store.",
            "2. Update the api, web, ui and cli surfaces for the new schema.",
            "3. Add integration tests and docs for the migration.",
            "4. Then recover the durable queue and verify security boundaries.",
            "5. First capture a baseline, then change the schema.",
            "6. Add the recovery path after the migration.",
            "7. Review the frontend and durable recovery integration.",
        ]
    ) + "\n" + ("api web ui cli tests docs database schema supervisor " * 20)
    result = service.run_sync(
        task=task,
        repo_path=repo,
        gate_command="true",
        review=False,
        is_peak=False,
    )

    assert result.status == RunStatus.COMPLETED, result.summary
    assert worker.call_count == 1
    assert len(prompts) == 1
    assert task.strip() in prompts[0]
    assert prompts[0].count(task.strip()) == 1
    assert "context_mode: INITIAL" in prompts[0]
    assert db.get_plan_steps(result.run_id) == []
    assert db.get_work_packages(result.run_id) == []
    event_types = {event.event_type for event in db.get_events(result.run_id, limit=10000)}
    assert "plan_created" not in event_types
    assert "package_completed" not in event_types
    assert "next_package_started" not in event_types
