"""R3-E1.2: exact reviewer resource admission (readiness + C07 quota).

The exact REVIEWER binding is re-resolved before launch (R3-E1.1); this
proves reviewer readiness and C07 quota eligibility execute against that
exact resolved resource rather than at provider level:

* readiness: a bound reviewer admits through its binding's exact
  ``profile_id`` -- never provider-only and never a sibling
  connection's profile;
* quota: the resolved reviewer's ``quota_pool_id`` scopes C07
  eligibility through the existing ``binding_pool_id`` seam, so a
  sibling account's pool can neither block nor authorize the selected
  reviewer.  Unbound ladder candidates keep the legacy provider-level
  behavior (``None`` pool scope).

Hermetic and offline: no provider, model, credential, or network access.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from saberops.control_plane import (
    BindingRole,
    OwnerBindings,
    materialize_discovered_binding,
    save_owner_bindings,
    upsert_owner_binding,
)
from saberops.control_plane.policy import (
    freeze_control_plane_policy,
    load_control_plane_policy,
    set_quota_pool_policy,
)
from saberops.db import Database
from saberops.models import (
    Attempt,
    AttemptStatus,
    ProviderReadiness,
    ReviewResult,
    ReviewVerdict,
    Run,
    RunStatus,
    Tier,
    WorkerCandidate,
    WorkerRequest,
    WorkerResult,
)
from saberops.review import ReviewEngine
from saberops.workers.base import WorkerAdapter
from saberops.workers.registry import AdapterRegistry

_R3E12_CREATED = "2026-09-25T00:00:00+00:00"
_R3E12_PROVIDER = "opencode"
_R3E12_WORKER_MODEL = "r3e12-worker-model"
_R3E12_REVIEWER_MODEL = "r3e12-reviewer-model"
_R3E12_CONN_A = "r3e12-conn-a"
_R3E12_CONN_B = "r3e12-conn-b"


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


class _R3E12ScriptedAdapter(WorkerAdapter):
    """Hermetic reviewer adapter: passes with an explicit verdict."""

    def __init__(self, call_log: list[str]) -> None:
        self._call_log = call_log

    @property
    def provider_name(self) -> str:
        return _R3E12_PROVIDER

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
        raise AssertionError("R3-E1.2 probes never dispatch implementation work")


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


def _seed_two_connection_reviewers() -> tuple[str, str, str, str, str, str]:
    """Seed REVIEWER bindings for two connections; return ids/profiles/pools."""
    owner = OwnerBindings()
    binding_a, profile_a, pool_a = materialize_discovered_binding(
        connection_id=_R3E12_CONN_A,
        provider=_R3E12_PROVIDER,
        model=_R3E12_REVIEWER_MODEL,
        binding_role=BindingRole.REVIEWER,
    )
    owner = upsert_owner_binding(
        owner, binding=binding_a, profile=profile_a, pool=pool_a
    )
    binding_b, profile_b, pool_b = materialize_discovered_binding(
        connection_id=_R3E12_CONN_B,
        provider=_R3E12_PROVIDER,
        model=_R3E12_REVIEWER_MODEL,
        binding_role=BindingRole.REVIEWER,
    )
    owner = upsert_owner_binding(
        owner, binding=binding_b, profile=profile_b, pool=pool_b
    )
    save_owner_bindings(owner)
    assert profile_a.profile_id != profile_b.profile_id
    assert pool_a.pool_id != pool_b.pool_id
    return (
        binding_a.binding_id,
        binding_b.binding_id,
        profile_a.profile_id,
        profile_b.profile_id,
        pool_a.pool_id,
        pool_b.pool_id,
    )


def _seed_reviewable_run(
    db: Database,
    repo: Path,
    run_id: str,
    *,
    control_plane_snapshot_json: str | None = None,
    control_plane_snapshot_digest: str | None = None,
) -> Run:
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    run = Run(
        id=run_id,
        task="r3e12 resource admission probe",
        target_repo=str(repo),
        base_commit=base,
        tier=Tier.T1,
        training_allowed=True,
        gate_command="true",
        status=RunStatus.RUNNING,
        created_at=_R3E12_CREATED,
        control_plane_snapshot_json=control_plane_snapshot_json,
        control_plane_snapshot_digest=control_plane_snapshot_digest,
    )
    db.create_run(run)
    db.create_attempt(
        Attempt(
            id=f"{run_id}-a1",
            run_id=run_id,
            attempt_number=1,
            provider=_R3E12_PROVIDER,
            model=_R3E12_WORKER_MODEL,
            worktree_path=str(repo),
            branch_name="main",
            status=AttemptStatus.SUCCESS,
            commit_sha=base,
            created_at=_R3E12_CREATED,
        )
    )
    return run


# --- readiness ---------------------------------------------------------------


@dataclass(frozen=True)
class _R3E12Admission:
    state: ProviderReadiness
    reason: str
    executable_available: bool
    connection_id: str | None = None
    observed_at: str | None = None


class _ExactProfileReadiness:
    """Readiness double that only honors the selected reviewer's profile.

    Returns READY exclusively for the exact expected ``profile_id``;
    any other request -- the wrong sibling profile or an empty
    provider-only profile -- is NOT_READY, so the reviewer skips and
    the probe fails instead of launching on the wrong resource.
    """

    def __init__(
        self,
        *,
        expected_profile_id: str,
        ready_connection_id: str,
        decoy_connection_id: str,
    ) -> None:
        self._expected = expected_profile_id
        self._ready_connection = ready_connection_id
        self._decoy_connection = decoy_connection_id
        self.calls: list[dict[str, str]] = []
        self.recorded: list[str] = []

    def admission_for(
        self, *, provider: str, profile_id: str = "", backend_ref: str = ""
    ) -> _R3E12Admission:
        self.calls.append(
            {"provider": provider, "profile_id": profile_id, "backend_ref": backend_ref}
        )
        if not profile_id or profile_id != self._expected:
            return _R3E12Admission(
                ProviderReadiness.NOT_READY,
                "WRONG_PROFILE",
                True,
                self._decoy_connection,
            )
        return _R3E12Admission(
            ProviderReadiness.READY, "READY", True, self._ready_connection
        )

    def record_success(self, connection_id: str) -> None:
        self.recorded.append(connection_id)


def test_bound_reviewer_admits_through_exact_connection_profile(
    isolated_xdg: None, tmp_path: Path
) -> None:
    """Reviewer B's admission requests B's exact profile, never A nor provider-only."""
    _binding_a, binding_b, profile_a, profile_b, _pool_a, _pool_b = (
        _seed_two_connection_reviewers()
    )
    repo = _git_repo(tmp_path / "repo")
    db = Database(tmp_path / "review.db")
    _seed_reviewable_run(db, repo, "run_ready_exact")
    readiness = _ExactProfileReadiness(
        expected_profile_id=profile_b,
        ready_connection_id=_R3E12_CONN_B,
        decoy_connection_id=_R3E12_CONN_A,
    )
    call_log: list[str] = []
    engine = ReviewEngine(
        db=db,
        registry=AdapterRegistry([_R3E12ScriptedAdapter(call_log)]),
        readiness_service=readiness,
    )
    result = engine.review_run(
        "run_ready_exact",
        reviewer_override=WorkerCandidate(
            provider=_R3E12_PROVIDER,
            model=_R3E12_REVIEWER_MODEL,
            binding_id=binding_b,
        ),
    )
    assert result.verdict is ReviewVerdict.PASS
    assert call_log == [f"review:{_R3E12_REVIEWER_MODEL}"]
    # Every admission request named B's exact profile: no provider-only
    # (empty profile) call and no sibling-A call could have launched this.
    assert readiness.calls, "readiness admission was never consulted"
    assert all(call["provider"] == _R3E12_PROVIDER for call in readiness.calls)
    assert all(call["profile_id"] == profile_b for call in readiness.calls)
    assert all(call["profile_id"] != profile_a for call in readiness.calls)
    assert all(call["profile_id"] != "" for call in readiness.calls)
    # No readiness skip was recorded for the bound reviewer.
    assert db.get_events_by_type("run_ready_exact", "review_candidate_skipped") == []
    # Successful execution was attributed to B's exact connection.
    assert readiness.recorded == [_R3E12_CONN_B]


# --- quota --------------------------------------------------------------------


def _seed_quota_policy_pools(pool_a: str, pool_b: str) -> tuple[str, str]:
    """Declare canonical STRICT quota policies for both pools; return frozen snapshot."""
    set_quota_pool_policy(pool_a)
    set_quota_pool_policy(pool_b)
    frozen = freeze_control_plane_policy(load_control_plane_policy())
    return frozen.as_json(), frozen.digest


def _run_bound_reviewer(
    db: Database, run_id: str, binding_b: str
) -> tuple[ReviewEngine, list[str], ReviewResult]:
    call_log: list[str] = []
    engine = ReviewEngine(
        db=db,
        registry=AdapterRegistry([_R3E12ScriptedAdapter(call_log)]),
        readiness_service=None,
    )
    result = engine.review_run(
        run_id,
        reviewer_override=WorkerCandidate(
            provider=_R3E12_PROVIDER,
            model=_R3E12_REVIEWER_MODEL,
            binding_id=binding_b,
        ),
    )
    return engine, call_log, result


def test_sibling_blocked_pool_does_not_block_selected_reviewer(
    isolated_xdg: None, tmp_path: Path
) -> None:
    """Pool A blocked + pool B healthy: reviewer B remains eligible and launches."""
    _binding_a, binding_b, _pa, _pb, pool_a, pool_b = _seed_two_connection_reviewers()
    snapshot_json, digest = _seed_quota_policy_pools(pool_a, pool_b)
    repo = _git_repo(tmp_path / "sibling-blocked" / "repo")
    db = Database(tmp_path / "sibling-blocked" / "review.db")
    _seed_reviewable_run(
        db,
        repo,
        "run_pool_a_blocked",
        control_plane_snapshot_json=snapshot_json,
        control_plane_snapshot_digest=digest,
    )
    db.set_quota(_R3E12_PROVIDER, pool_a, 0.0)
    db.set_quota(_R3E12_PROVIDER, pool_b, 100.0)
    _engine, call_log, result = _run_bound_reviewer(db, "run_pool_a_blocked", binding_b)
    assert result.verdict is ReviewVerdict.PASS
    assert call_log == [f"review:{_R3E12_REVIEWER_MODEL}"]
    assert (
        db.get_events_by_type("run_pool_a_blocked", "review_candidate_skipped") == []
    )


def test_selected_reviewer_blocked_pool_refuses_with_zero_launch(
    isolated_xdg: None, tmp_path: Path
) -> None:
    """Pool A healthy + pool B blocked: reviewer B is refused by quota, zero launch."""
    _binding_a, binding_b, _pa, _pb, pool_a, pool_b = _seed_two_connection_reviewers()
    snapshot_json, digest = _seed_quota_policy_pools(pool_a, pool_b)
    repo = _git_repo(tmp_path / "own-blocked" / "repo")
    db = Database(tmp_path / "own-blocked" / "review.db")
    _seed_reviewable_run(
        db,
        repo,
        "run_pool_b_blocked",
        control_plane_snapshot_json=snapshot_json,
        control_plane_snapshot_digest=digest,
    )
    db.set_quota(_R3E12_PROVIDER, pool_a, 100.0)
    db.set_quota(_R3E12_PROVIDER, pool_b, 0.0)
    _engine, call_log, result = _run_bound_reviewer(db, "run_pool_b_blocked", binding_b)
    assert result.verdict is ReviewVerdict.REVIEW_UNAVAILABLE
    assert call_log == []
    skips = db.get_events_by_type("run_pool_b_blocked", "review_candidate_skipped")
    assert len(skips) == 1
    assert skips[0].payload is not None
    assert skips[0].payload["reason"] == "quota_blocked"
    assert skips[0].payload["binding_id"] == binding_b
    assert db.get_dispatch_evidence_for_run("run_pool_b_blocked") == []
