"""ReviewEngine reviewer fallback ladder (ported production path).

Ported from the removed test_r3e1 reviewer-fallback chain checks: the
engine resolves the owner reviewer binding chain and honours the review
budget through the real registry seam."""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
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
from saberops.db import Database
from saberops.models import (
    Attempt,
    AttemptStatus,
    Run,
    RunStatus,
    Tier,
    WorkerCandidate,
    WorkerRequest,
    WorkerResult,
)
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


def test_structured_findings_accept_absent_optional_fields() -> None:
    """The production findings protocol accepts omitted optional fields.

    Covers: D-0045 retained-behavior repair of the surviving
    ``review_policy._optional_string`` ``None`` branch through the live
    ``parse_review_findings`` seam.  The payload is a real reviewer-shaped
    answer; the parsed evidence semantics and computed disposition are
    asserted, not the private helper call.
    """
    from saberops.review_policy import ReviewPolicyVerdict, parse_review_findings

    answer = (
        "VERDICT: PASS_WITH_BACKLOG\n"
        "Review notes only.\n\n"
        "```json\n"
        '{"findings": [{"id": "N1", "classification": "N1", '
        '"title": "Cosmetic naming", "severity": "low", '
        '"invariant_or_requirement": "Names stay readable", '
        '"failure_scenario": "None", '
        '"evidence": "cosmetic only; non-blocking backlog note", '
        '"minimum_correction": "Rename the local variable", '
        '"repair_scope": "LOCAL_MECHANICAL"}]}\n'
        "```\n"
    )
    parsed = parse_review_findings(answer)
    assert parsed.verdict is ReviewPolicyVerdict.PASS_WITH_BACKLOG
    assert parsed.summary == ""
    assert len(parsed.findings) == 1
    finding = parsed.findings[0]
    # Absent optional fields are retained as ``None``; required evidence is intact.
    assert finding.contract_reference is None
    assert finding.code_location is None
    assert finding.evidence == "cosmetic only; non-blocking backlog note"
    assert finding.blocking is False
