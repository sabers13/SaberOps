"""R1-A: characterize automatic routing vs readiness evidence (H1).

Executable characterization only -- no product behavior is changed here.

H1 (docs/product/UI_BACKEND_RECONCILIATION.md section 2.2) claims automatic
routing silently skips models when readiness evidence is (a) absent, (b) older
than the 24h freshness window, or (c) fresh.  The traced path is::

    ReadinessService / readiness store
    -> control_plane/preflight.py (automatic candidate evaluation)
    -> Orchestrator dispatch/routing.

These tests drive that real composition seam for one otherwise-eligible
automatic candidate:

* ``ReadinessService.admission_for`` (real registry + real durable store,
  executable faked present so readiness is the only gate), then
* ``saberops.dispatch.control_plane_decision`` /
  ``saberops.dispatch.evaluate_eligibility`` with ``explicit_override=False``
  -- the exact functions ``Orchestrator._run_core`` calls for automatic
  (non-override) candidates.

A model override (``explicit_override=True``) is explicitly out of scope and
is pinned only as a boundary marker.  No provider/model calls are made.

Timestamps are explicit (25h-old stale vs freshly recorded); no sleeping and
no timing-sensitive assertions -- margins are hours against a 24h window.

Verdict key: missing->skip plus stale->skip plus fresh->eligible means H1 is
PROVEN; any other combination is PARTIALLY PROVEN or DISPROVEN and must be
reported as such.
"""

from __future__ import annotations

import inspect
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import saberops.web as web_module
from saberops.control_plane.policy import packaged_control_plane_policy
from saberops.control_plane.preflight import PROVIDER_READINESS_UNKNOWN, ControlPlaneDecision
from saberops.dispatch import (
    SKIP_PROVIDER_READINESS_UNKNOWN,
    control_plane_decision,
    evaluate_eligibility,
)
from saberops.model_access import (
    ReadinessAdmission,
    ReadinessService,
    ReadinessStore,
    build_access_registry,
    default_account_connections,
)
from saberops.model_access.readiness import (
    ProviderReadinessEvidence,
    ReadinessEvidenceSource,
)
from saberops.models import ProviderReadiness, WorkerCandidate, WorkerRequest, WorkerResult
from saberops.web import create_app
from saberops.workers.base import WorkerAdapter


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


def _automatic_outcome(
    service: ReadinessService, candidate: WorkerCandidate
) -> tuple[ReadinessAdmission, ControlPlaneDecision, str | None]:
    """Run the real automatic preflight seam for one candidate.

    Returns ``(admission, decision, skip_reason)``.  ``skip_reason is None``
    means the candidate survives preflight and can reach dispatch; any string
    is the typed skip reason the orchestrator records and continues past.
    """
    adapter = _AvailableAdapter(candidate.provider)
    policy = packaged_control_plane_policy()
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
    policy = packaged_control_plane_policy()
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
    """
    source = inspect.getsource(web_module)
    assert "ReadinessService" in source
    assert ".refresh(" in source
    assert "probe_account_backend" not in source


def test_r1a_web_probe_writes_no_readiness_evidence(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    """R1-B: POST /access/test persists readiness evidence for the connection.

    (This test supersedes its R1-A characterization name: the R1-A suite
    pinned that Verify wrote nothing; R1-B specifies that Verify must
    write readiness, so the test now pins the new contract -- persisted
    sanitized state/reason/timestamp for the exact verified connection.)
    """
    client = TestClient(create_app(db_path=tmp_path / "web.db"), raise_server_exceptions=False)
    response = client.post("/access/test", data={"connection_id": "opencode-account"})

    assert response.status_code == 200
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
