"""Ported engine-rule assertions from tests removed by the console cut (T-007).

Each test names its removed source.  Rules already covered by kept tests
or by ``tests/test_cli_parity.py`` are NOT duplicated here; the full
mapping (every removed function -> UI-ONLY | ENGINE-RULE | OBSOLETE) is
in ``.orch/T-007/test-audit.md``.  No HTML is asserted anywhere.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from saberops.control_plane import (
    BindingRole,
    OwnerBindings,
    load_owner_bindings,
    materialize_discovered_binding,
    save_owner_bindings,
    upsert_owner_binding,
)
from saberops.control_plane.binding_store import BindingStoreError
from saberops.control_plane.bindings import BindingResolutionError, resolve_execution_binding
from saberops.db import Database
from saberops.model_access import (
    AccessRegistry,
    AccessStore,
    ProviderConnection,
    ProviderReadinessEvidence,
    ReadinessCatalog,
    ReadinessEvidenceSource,
    ReadinessStore,
)
from saberops.model_access.connections import AccessProtocol, AuthMode
from saberops.model_access.readiness_service import ReadinessService
from saberops.models import (
    Attempt,
    AttemptStatus,
    ProviderReadiness,
    Run,
    RunStatus,
    Tier,
    WorkerCandidate,
    WorkerRequest,
    WorkerResult,
)
from saberops.run_titles import clean_title, fallback_title, parse_named_guidance
from saberops.workers.base import WorkerAdapter
from saberops.workers.registry import AdapterRegistry

_CREATED = "2026-09-25T00:00:00+00:00"


@pytest.fixture()
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "state"
    config = tmp_path / "config"
    cache = tmp_path / "cache"
    for directory in (state, config, cache):
        directory.mkdir(parents=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    monkeypatch.delenv("SABEROPS_PROFILE", raising=False)
    monkeypatch.setenv("ORCH_BINDING_STORE", str(tmp_path / "bindings.json"))
    monkeypatch.setenv("ORCH_ACCESS_STORE", str(tmp_path / "access.json"))
    monkeypatch.setenv("ORCH_ACCESS_MODELS_STORE", str(tmp_path / "models.json"))
    monkeypatch.setenv("ORCH_READINESS_STORE", str(tmp_path / "readiness.json"))
    return tmp_path


def _seed_verified_opencode_posture(config_home: Path) -> None:
    """Seed an isolated owner config with a verified class-A posture for the
    synthetic ``opencode`` provider used by this module's dispatch fixtures.

    Covers: ADR-0002 D2 / WORKFLOW §7 authorized setup-only fixture migration.
    The R3E1 fixtures dispatch synthetic ``opencode/<model>`` candidates with no
    matching posture record, so the accepted M2-4b admission seam rightfully
    returns ``quota_blocked`` (POSTURE_UNVERIFIED fails closed).  This seeds
    ONLY the synthetic provider identity those fixtures use; no assertion,
    parameter or expected model list changes, no real vendor receives an
    attestation, and no production posture/admission/billing logic is touched.
    ``verified_on`` is stamped at run time with a 90-day TTL so replay never
    expires.  The drawn pool is the non-overage synthetic ``opencode`` default,
    so no quota evidence is required.
    """
    cfg_dir = config_home / "saberops"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    today_iso = datetime.now(UTC).date().isoformat()
    cp_doc = {
        "version": 2,
        "providers": {
            "opencode": {"state": "ALLOWED"},
            "codex": {"state": "ALLOWED"},
            "cline": {"state": "ALLOWED"},
        },
        "quota_pools": {},
        "postures": {
            "opencode": {
                "class": "A",
                "policy_urls": ["https://example.com/synthetic-test-posture"],
                "ttl_days": 90,
                "verified_on": today_iso,
                "note": "synthetic verified test posture for ported dispatch fixtures",
            }
        },
    }
    (cfg_dir / "control_plane.json").write_text(json.dumps(cp_doc), encoding="utf-8")


@pytest.fixture()
def dispatch_posture(isolated: Path) -> Path:
    """Hermetic verified class-A posture for the synthetic ``opencode``
    dispatch fixtures in this module (setup-only fixture migration)."""
    _seed_verified_opencode_posture(isolated / "config")
    return isolated


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t7@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "T-007"], cwd=path, check=True)
    (path / "Makefile").write_text("gate:\n\t@echo ok\n", encoding="utf-8")
    (path / "README.md").write_text("# t7\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=path, check=True, capture_output=True)
    return path


# ---------------------------------------------------------------------------
# test_r0b_readiness_truth.py: readiness evidence rules at the service seam.
# The removed tests rendered these through /access; the rules live in
# ReadinessService / the readiness catalog / the binding resolver.
# ---------------------------------------------------------------------------


def _account_connection(connection_id: str = "opencode-account") -> ProviderConnection:
    return next(
        entry
        for entry in AccessStore(Path("/nonexistent")).load().list_connections()
        if entry.connection_id == connection_id
    )


def test_ported_installed_cli_is_not_proven_readiness(
    isolated: Path,
) -> None:
    """Ported from test_r0b_cli_installed_is_not_proven_readiness."""
    connection = _account_connection()
    service = ReadinessService(
        AccessRegistry((connection,)),
        ReadinessStore(isolated / "readiness.json"),
        which=lambda _exe: "/usr/bin/opencode",
    )
    evidence = service.refresh(connection)
    # The opencode backend exposes no auth probe: a present binary is
    # availability, never proven readiness.
    assert evidence.state is ProviderReadiness.UNKNOWN


def test_ported_stale_ready_evidence_marked_stale(isolated: Path) -> None:
    """Ported from test_r0b_stale_evidence_marked_stale."""
    store = ReadinessStore(isolated / "readiness.json")
    old = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
    store.replace(
        ProviderReadinessEvidence(
            "opencode-account",
            "opencode",
            "opencode",
            ProviderReadiness.READY,
            "READY",
            old,
            ReadinessEvidenceSource.CONNECTION_TEST,
        )
    )
    catalog = store.load()
    assert isinstance(catalog, ReadinessCatalog)
    entry = catalog.try_get("opencode-account")
    assert entry is not None and entry.state is ProviderReadiness.READY
    projected = entry.freshness(as_of=datetime.now(UTC), max_age=timedelta(hours=24))
    assert projected.state is ProviderReadiness.UNKNOWN
    assert projected.reason == "STALE_READINESS_EVIDENCE"
    fresh = entry.freshness(as_of=datetime.now(UTC), max_age=timedelta(hours=26))
    assert fresh.state is ProviderReadiness.READY


def test_ported_no_evidence_is_not_verified(isolated: Path) -> None:
    """Ported from test_r0b_no_evidence_says_not_verified."""
    catalog = ReadinessStore(isolated / "readiness.json").load()
    assert catalog.try_get("opencode-account") is None


def test_ported_verified_login_does_not_imply_execution_adapter(
    isolated: Path,
) -> None:
    """Ported from test_r0b_verified_login_does_not_imply_execution_adapter."""
    store = ReadinessStore(isolated / "readiness.json")
    store.replace(
        ProviderReadinessEvidence(
            "opencode-account",
            "opencode",
            "opencode",
            ProviderReadiness.READY,
            "READY",
            datetime.now(UTC).isoformat(),
            ReadinessEvidenceSource.CONNECTION_TEST,
        )
    )
    # READY transport evidence with no executable binding still refuses.
    with pytest.raises(BindingResolutionError):
        resolve_execution_binding(
            registry=None, binding_id="discovered-binding:worker:opencode-account:x",
            adapter=None,
        )


def test_ported_api_credential_missing_never_ready(isolated: Path) -> None:
    """Ported from the CREDENTIAL_MISSING half of the access-test rules."""
    connection = ProviderConnection(
        connection_id="t7-keyless-api",
        provider="custom",
        protocol=AccessProtocol.OPENAI_COMPATIBLE,
        backend="openai-compatible-api",
        auth_mode=AuthMode.API_KEY,
        endpoint="http://127.0.0.1:1",
        credential_ref="env:T7_PORTED_DEFINITELY_MISSING",
        model="t7-model",
    )
    service = ReadinessService(
        AccessRegistry((connection,)), ReadinessStore(isolated / "readiness.json")
    )
    evidence = service.refresh(connection)
    assert evidence.state is not ProviderReadiness.READY
    assert evidence.reason == "CREDENTIAL_MISSING"


# ---------------------------------------------------------------------------
# test_c16c2_console.py::test_c16c2_quota_set_round_trip: quota persistence.
# ---------------------------------------------------------------------------


def test_ported_quota_set_round_trip(isolated: Path) -> None:
    """Ported from test_c16c2_quota_set_round_trip (backend state, not HTML)."""
    from saberops.service import OrchestratorService

    service = OrchestratorService(db=Database(isolated / "quota.db"))
    state = service.set_quota(
        provider="codex", pool="weekly", remaining_percent=42.5, reset_at=None
    )
    assert state.remaining_percent == 42.5
    fetched = service.get_quota(provider="codex", pool="weekly")
    assert fetched is not None
    assert fetched.remaining_percent == 42.5
    assert fetched.provider == "codex" and fetched.pool == "weekly"


# ---------------------------------------------------------------------------
# test_r3e1: reviewer fallback chain + injected registry (production path).
# ---------------------------------------------------------------------------

_R3E1_PROVIDER = "opencode"
_R3E1_WORKER_MODEL = "r3e1-worker-model"
_R3E1_REVIEWER_MODEL = "r3e1-reviewer-model"
_R3E1_CONNECTION = "r3e1-conn"


class _ScriptedAdapter(WorkerAdapter):
    """Hermetic adapter: implementer writes a file, reviewer passes."""

    def __init__(self, call_log: list[str], env_log: list[dict[str, str]]) -> None:
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
        (request.worktree_path / "r3e1.txt").write_text("r3e1 candidate\n", encoding="utf-8")
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


def _seed_reviewable_run(db: Database, repo: Path, run_id: str) -> None:
    """Seed one reviewable run bound to the isolated owner control plane.

    Covers: ADR-0002 D2 / WORKFLOW §7 authorized setup-only fixture migration
    for the synthetic r3e1 reviewable run.  Attaching the frozen owner
    control-plane snapshot (with the ``dispatch_posture`` fixture's verified
    synthetic ``opencode`` posture) is setup only: the run's frozen contract now
    reflects the same isolated owner policy the reviewer dispatch is evaluated
    against, exactly as ``tests/test_r3e1_reviewer_dispatch_authority.py`` does.
    No assertion, candidate, or expected model list changes.
    """
    from saberops.control_plane.policy import freeze_control_plane_policy, load_control_plane_policy

    frozen = freeze_control_plane_policy(load_control_plane_policy())
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    db.create_run(
        Run(
            id=run_id,
            task="r3e1 dispatch authority probe",
            target_repo=str(repo),
            base_commit=base,
            tier=Tier.T1,
            training_allowed=True,
            gate_command="true",
            status=RunStatus.RUNNING,
            created_at=_CREATED,
            control_plane_snapshot_json=frozen.as_json(),
            control_plane_snapshot_digest=frozen.digest,
        )
    )
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
            created_at=_CREATED,
        )
    )


@pytest.mark.parametrize(
    "first_outcome,limit,expected_models",
    [
        ("failed", 2, ["review-first", "review-second"]),
        ("failed", 1, ["review-first"]),
        ("block", 2, ["review-first"]),
    ],
)
def test_ported_owner_reviewer_chain_respects_fallback_budget(
    isolated: Path,
    dispatch_posture: Path,
    tmp_path: Path,
    first_outcome: str,
    limit: int,
    expected_models: list[str],
) -> None:
    """Ported from test_owner_reviewer_chain_respects_fallback_budget_and_substantive_block."""
    from saberops.models import ReviewVerdict
    from saberops.review import ReviewEngine

    owner = OwnerBindings()
    ids: list[str] = []
    for model in ("review-first", "review-second", "unpicked-review"):
        binding, profile, pool = materialize_discovered_binding(
            connection_id=_R3E1_CONNECTION,
            provider=_R3E1_PROVIDER,
            model=model,
            binding_role=BindingRole.REVIEWER,
        )
        owner = upsert_owner_binding(owner, binding=binding, profile=profile, pool=pool)
        ids.append(binding.binding_id)
    save_owner_bindings(owner.with_role_order("reviewer", ids[:2]))
    repo = _git_repo(tmp_path / "repo")
    db = Database(tmp_path / "review.db")
    _seed_reviewable_run(db, repo, "fallback-review")
    calls: list[str] = []
    envs: list[dict[str, str]] = []
    scripted = _ScriptedAdapter(calls, envs)
    real_run = _ScriptedAdapter.run

    def scripted_run(self: _ScriptedAdapter, request: WorkerRequest) -> WorkerResult:
        result = real_run(self, request)
        if request.model == "review-first" and first_outcome == "failed":
            return replace(result, status=AttemptStatus.FAILED, stdout="", error="down")
        if request.model == "review-first" and first_outcome == "block":
            return replace(result, stdout="VERDICT: BLOCK\nSUMMARY: Missing work\n")
        return result

    scripted.run = scripted_run.__get__(scripted)  # type: ignore[method-assign]
    engine = ReviewEngine(db=db, registry=AdapterRegistry([scripted]), readiness_service=None)
    result = engine.review_run(
        "fallback-review",
        review_limit=limit,
        reviewer_override=WorkerCandidate(
            provider=_R3E1_PROVIDER, model="review-first", binding_id=ids[0]
        ),
    )
    assert calls == [f"review:{model}" for model in expected_models]
    assert [env["ORCH_EXECUTION_BINDING_ID"] for env in envs] == ids[: len(expected_models)]
    if first_outcome == "block":
        assert result.verdict is ReviewVerdict.BLOCK
    elif limit == 2:
        assert result.verdict is ReviewVerdict.PASS


def test_ported_reviewer_selection_uses_injected_registry(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ported from test_reviewer_surfaces_use_injected_registry (no HTTP)."""
    from saberops.service import OrchestratorService

    reviewer, profile, pool = materialize_discovered_binding(
        connection_id=_R3E1_CONNECTION,
        provider=_R3E1_PROVIDER,
        model=_R3E1_REVIEWER_MODEL,
        binding_role=BindingRole.REVIEWER,
    )
    save_owner_bindings(
        upsert_owner_binding(OwnerBindings(), binding=reviewer, profile=profile, pool=pool)
    )

    def _empty_default(cls: Any) -> AdapterRegistry:
        return AdapterRegistry(adapters=[])

    monkeypatch.setattr(AdapterRegistry, "default", classmethod(_empty_default))
    injected = AdapterRegistry([_ScriptedAdapter([], [])])
    service = OrchestratorService(registry=injected)
    # Resolution goes through the injected registry, never a fresh default:
    # with an empty default this would raise NO_ADAPTER otherwise.
    result = service.set_reviewer_binding(reviewer.binding_id)
    assert result["selected_binding_id"] == reviewer.binding_id
    assert load_owner_bindings().reviewer_binding_id == reviewer.binding_id


# ---------------------------------------------------------------------------
# test_r3e1 quick / custom workflow through acceptance.
# ---------------------------------------------------------------------------


def _patch_implementer_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    def _resolve(**_: Any) -> list[WorkerCandidate]:
        return [WorkerCandidate(_R3E1_PROVIDER, _R3E1_WORKER_MODEL)]

    monkeypatch.setattr("saberops.orchestrator.resolve_candidate_chain", _resolve)


def test_ported_quick_workflow_skips_gate_and_review_through_acceptance(
    isolated: Path, dispatch_posture: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ported from test_quick_workflow_skips_gate_and_review_through_acceptance."""
    from saberops.accept import AcceptEngine
    from saberops.gate_runner import GateRunner
    from saberops.orchestrator import Orchestrator
    from saberops.project_settings import set_project_workflow
    from saberops.run_creation import RunRequest, resolve_effective_run_config

    repo = _git_repo(tmp_path / "quick" / "repo")
    set_project_workflow(repo, preset="quick")
    config = resolve_effective_run_config(
        RunRequest(task="Quick scaffold", repo_path=repo, publish_candidate=False)
    )
    assert not config.gate_enabled
    assert config.review_disabled
    set_project_workflow(repo, preset="balanced")
    calls: list[str] = []
    environments: list[dict[str, str]] = []
    _patch_implementer_chain(monkeypatch)
    db = Database(tmp_path / "quick" / "orch.db")
    orch = Orchestrator(
        db=db,
        registry=AdapterRegistry([_ScriptedAdapter(calls, environments)]),
        worktree_base=tmp_path / "quick" / "wt",
    )

    def forbidden_gate(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Quick must not execute a gate")

    monkeypatch.setattr("saberops.orchestrator.run_process", forbidden_gate)
    monkeypatch.setattr(GateRunner, "run", forbidden_gate)
    result = orch.run(config=config, is_peak=False)
    assert result.passed, result.summary
    assert db.get_reviews_for_run(result.run_id) == []
    checks = db.get_checks_for_run(result.run_id)
    assert checks and all(check.skipped and not check.passed for check in checks)
    assert db.get_events_by_type(result.run_id, "gate_skipped")
    assert db.get_events_by_type(result.run_id, "review_skipped_by_owner_policy")
    accepted = AcceptEngine(db).accept_run(result.run_id)
    assert accepted.accepted
    assert not accepted.gate_passed


@pytest.mark.parametrize("gate_enabled,review_disabled", [(True, True), (False, False)])
def test_ported_custom_workflow_gate_and_review_are_independent(
    isolated: Path,
    dispatch_posture: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gate_enabled: bool,
    review_disabled: bool,
) -> None:
    """Ported from test_custom_workflow_gate_and_review_are_independent."""
    from saberops.accept import AcceptEngine
    from saberops.orchestrator import Orchestrator
    from saberops.project_settings import set_project_gate_command, set_project_workflow
    from saberops.run_creation import RunRequest, resolve_effective_run_config

    repo = _git_repo(tmp_path / "repo")
    set_project_gate_command(repo, "true")
    set_project_workflow(
        repo,
        gate_enabled=gate_enabled,
        review="disabled" if review_disabled else "always-required",
        limit=3,
    )
    reviewer, profile, pool = materialize_discovered_binding(
        connection_id=_R3E1_CONNECTION,
        provider=_R3E1_PROVIDER,
        model=_R3E1_REVIEWER_MODEL,
        binding_role=BindingRole.REVIEWER,
    )
    save_owner_bindings(
        replace(
            upsert_owner_binding(
                OwnerBindings(), binding=reviewer, profile=profile, pool=pool
            ),
            reviewer_binding_id=reviewer.binding_id,
        )
    )
    _patch_implementer_chain(monkeypatch)
    calls: list[str] = []
    environments: list[dict[str, str]] = []
    db = Database(tmp_path / "orch.db")
    orch = Orchestrator(
        db=db,
        registry=AdapterRegistry([_ScriptedAdapter(calls, environments)]),
        worktree_base=tmp_path / "wt",
    )
    config = resolve_effective_run_config(
        RunRequest(task="Custom scaffold", repo_path=repo, publish_candidate=False)
    )
    result = orch.run(config=config, is_peak=False)
    assert result.passed, result.summary
    assert bool(db.get_reviews_for_run(result.run_id)) is not review_disabled
    checks = db.get_checks_for_run(result.run_id)
    assert checks and all(check.skipped is not gate_enabled for check in checks)
    accepted = AcceptEngine(db).accept_run(result.run_id)
    assert accepted.accepted
    assert accepted.gate_passed is gate_enabled


# ---------------------------------------------------------------------------
# test_orchestrator_chat.py::test_codex_image_input_preserves_readonly_execution
# ---------------------------------------------------------------------------


def test_ported_codex_image_input_preserves_readonly_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ported from test_codex_image_input_preserves_readonly_execution."""
    import sys as _sys

    from saberops.git import GitManager
    from saberops.workers.codex import CodexAdapter

    bindir = tmp_path / "bin"
    bindir.mkdir()
    outfile = tmp_path / "child.json"
    script = (
        f"#!{_sys.executable}\n"
        "import json, os, sys\n"
        f"with open({str(outfile)!r}, 'w', encoding='utf-8') as fh:\n"
        "    json.dump({'argv': sys.argv, 'env': dict(os.environ)}, fh, sort_keys=True)\n"
    )
    stub = bindir / "stub-codex"
    stub.write_text(script, encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setattr(GitManager, "get_changed_paths", lambda _path: [])
    image = tmp_path / "reference.png"
    image.write_bytes(b"owner-picture")
    adapter = CodexAdapter(codex_bin="stub-codex")
    result = adapter.run(
        WorkerRequest(
            "Describe this picture",
            tmp_path,
            "gpt-6.1-sol",
            read_only=True,
            input_images=(image,),
        )
    )
    assert result.status is AttemptStatus.SUCCESS
    recorded = json.loads(outfile.read_text(encoding="utf-8"))
    argv = recorded["argv"]
    assert "--image" in argv and str(image) in argv
    sandbox = argv[argv.index("--sandbox") + 1]
    assert sandbox == "read-only"


# ---------------------------------------------------------------------------
# test_project_chat_navigation.py: run-title rules (chat halves omitted).
# ---------------------------------------------------------------------------


def _nav_run(repo: str, identity: str = "run_navigation") -> Run:
    return Run(
        id=identity,
        task="Build “Incident Desk,” an issue tracker in " + repo,
        target_repo=repo,
        base_commit="base",
        tier=Tier.T1,
        training_allowed=False,
        gate_command="true",
        status=RunStatus.COMPLETED,
        created_at="2026-10-09T10:00:00Z",
    )


def test_ported_legacy_name_migration_preserves_objective(tmp_path: Path) -> None:
    """Ported from test_legacy_name_migration_preserves_objective."""
    path = tmp_path / "runs.db"
    original = _nav_run("/repos/project")
    Database(path).create_run(original)
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE runs DROP COLUMN display_title")
    legacy = Database(path, read_only=True).get_run(original.id)
    assert legacy and legacy.display_title is None
    migrated = Database(path)
    migrated.set_run_title(original.id, "Issue tracker")
    saved = migrated.get_run(original.id)
    assert saved and saved.task == original.task and saved.status == original.status
    assert saved.display_title == "Issue tracker"


def test_ported_run_title_survives_lifecycle_updates(tmp_path: Path) -> None:
    """Ported from test_run_title_survives_creation_and_lifecycle_updates."""
    from dataclasses import replace as _replace

    db = Database(tmp_path / "runs.db")
    original = _replace(_nav_run("/repos/project"), display_title="Issue tracker")
    db.create_run(original)
    db.update_run(original.id, RunStatus.FAILED, result_summary="Gate failed")
    saved = Database(db.db_path).get_run(original.id)
    assert saved and saved.display_title == "Issue tracker" and saved.task == original.task


def test_ported_name_bounds_and_transcript_fallback() -> None:
    """Ported from test_name_bounds_and_transcript_fallback."""
    repo = "/repos/my-project"
    assert (
        fallback_title("Owner conversation: Build “Incident Desk,” in " + repo, repo)
        == "Build Incident Desk"
    )
    assert fallback_title("", repo) == "Engineering task"
    label = clean_title("my-project /repos/my-project <b>Fix login\n" + "problem " * 20, repo)
    assert len(label) <= 80 and len(label.split()) <= 10
    assert "my-project" not in label and "/repos" not in label and "<" not in label
    assert parse_named_guidance("Legacy advisory") == ("", "Legacy advisory")


def test_ported_duplicate_names_reserved_atomically(tmp_path: Path) -> None:
    """Ported from test_duplicate_names_are_reserved_atomically_and_survive_restart."""
    from concurrent.futures import ThreadPoolExecutor

    db = Database(tmp_path / "runs.db")
    originals = [_nav_run("/repos/project", f"run_{index}") for index in range(8)]
    for original in originals:
        db.create_run(original)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(
            pool.map(
                lambda item: db.set_run_title(item.id, "Build shared issue tracker"),
                originals,
            )
        )
    titles = {item.id: item.display_title for item in db.list_runs()}
    assert len({title.casefold() for title in titles.values() if title}) == 8
    restarted = Database(db.db_path)
    assert {item.id: item.display_title for item in restarted.list_runs()} == titles


def test_ported_legacy_run_labels_upgrade(tmp_path: Path) -> None:
    """Ported from the run half of test_legacy_labels_upgrade_without_rewriting_transcripts."""
    db = Database(tmp_path / "runs.db")
    db.create_run(_nav_run("/repos/project", "first"))
    db.create_run(_nav_run("/repos/project", "second"))
    with sqlite3.connect(db.db_path) as conn:
        conn.execute("UPDATE runs SET display_title='Incident Desk'")
    upgraded = Database(db.db_path)
    labels = [item.display_title for item in upgraded.list_runs()]
    assert set(labels) == {
        "Build Incident Desk issue tracker",
        "Build Incident Desk issue tracker · 2",
    }


@pytest.mark.parametrize(
    "output,expected",
    [
        (json.dumps({"title": "project Fix login", "guidance": "Keep checks."}), "Fix login"),
        ("Legacy advisory", None),
        ('{"title": "Fix login", "guidance": false}', None),
    ],
)
def test_ported_startup_names_fetch_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output: str, expected: str | None
) -> None:
    """Ported from test_startup_names_with_one_existing_call (fetch-once half)."""
    from saberops.orchestrator import _InitialOrchestratorGuidanceCache
    from saberops.orchestrator_consumer import (
        OrchestratorModelConsumer,
        OrchestratorModelRequest,
        OrchestratorModelResult,
    )

    db = Database(tmp_path / "runs.db")
    original = _nav_run("/repos/project")
    db.create_run(original)
    db.create_run(
        replace(original, id="earlier_run", display_title="Build previous account settings")
    )
    requests: list[OrchestratorModelRequest] = []

    def invoke(
        self: OrchestratorModelConsumer, request: OrchestratorModelRequest
    ) -> OrchestratorModelResult:
        requests.append(request)
        return OrchestratorModelResult(
            status="SUCCESS",
            binding_id="b",
            profile_id="p",
            quota_pool_id="q",
            provider="stub",
            model="stub",
            backend="stub",
            reasoning_effort=None,
            output_text=output,
            summary="",
            error=None,
            prompt_bytes=0,
            duration_seconds=0,
            correlation_id=None,
            evidence_run_id=original.id,
            evidence_event_id=0,
        )

    monkeypatch.setattr(OrchestratorModelConsumer, "invoke", invoke)
    cache = _InitialOrchestratorGuidanceCache()
    service = SimpleNamespace(db=db)
    guidance = cache.get_or_fetch(
        service=service, original_task=original.task, run_id=original.id
    )
    assert (
        cache.get_or_fetch(service=service, original_task=original.task, run_id=original.id)
        == guidance
    )
    assert len(requests) == 1 and "title and guidance" in requests[0].system_prompt
    saved = db.get_run(original.id)
    assert saved and saved.display_title == (
        expected or fallback_title(original.task, original.target_repo)
    )
    assert (
        sum(e.event_type == "orchestrator_guidance_result" for e in db.get_events(original.id))
        == 1
    )


def test_ported_unavailable_orchestrator_keeps_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ported from test_unavailable_orchestrator_keeps_fallback."""
    from saberops.orchestrator import _InitialOrchestratorGuidanceCache
    from saberops.orchestrator_consumer import (
        OrchestratorModelConsumer,
        OrchestratorModelInvocationError,
        OrchestratorModelRequest,
        OrchestratorModelResult,
    )

    db = Database(tmp_path / "runs.db")
    original = _nav_run("/repos/project")
    db.create_run(original)

    def refuse(
        self: OrchestratorModelConsumer, request: OrchestratorModelRequest
    ) -> OrchestratorModelResult:
        raise OrchestratorModelInvocationError("ORCH_MODEL_NO_POLICY", message="Unavailable")

    monkeypatch.setattr(OrchestratorModelConsumer, "invoke", refuse)
    cache = _InitialOrchestratorGuidanceCache()
    text, reason = cache.get_or_fetch(
        service=SimpleNamespace(db=db), original_task=original.task, run_id=original.id
    )
    assert not text and reason
    saved = db.get_run(original.id)
    assert saved and saved.task == original.task
    assert (
        fallback_title(saved.task, saved.target_repo) == "Build Incident Desk issue tracker"
    )


# ---------------------------------------------------------------------------
# test_chat_quick_access.py::test_role_routing_*: role-order validation.
# ---------------------------------------------------------------------------


def test_ported_role_order_validation(isolated: Path) -> None:
    """Ported engine half of test_role_routing_append_reorder_remove_and_chat_scope."""
    owner = OwnerBindings()
    updated = owner.with_role_order("reviewer", ("first", "second"))
    assert updated.role_binding_ids("reviewer") == ("first", "second")
    assert updated.reviewer_binding_id == "first"
    assert updated.role_binding_ids("orchestrator") == ()
    with pytest.raises(BindingStoreError):
        owner.with_role_order("reviewer", ("dup", "dup"))
    with pytest.raises(BindingStoreError):
        owner.with_role_order("reviewer", ("",))
    with pytest.raises(BindingStoreError):
        owner.with_role_order("no-such-role", ("x",))
    cleared = updated.with_role_order("reviewer", ())
    assert cleared.role_binding_ids("reviewer") == ()
    assert cleared.reviewer_binding_id is None


# ---------------------------------------------------------------------------
# test_native_access.py: cline session-id parsing (copilot half obsolete).
# ---------------------------------------------------------------------------


def test_ported_cline_session_id_parsing() -> None:
    """Ported cline half of test_additional_provider_session_ids_are_captured."""
    from saberops.agent_sessions import _native_id

    assert (
        _native_id(json.dumps({"type": "run_result", "sessionId": "native_session123"}), "cline")
        == "native_session123"
    )
    assert _native_id(json.dumps({"type": "other"}), "cline") is None
