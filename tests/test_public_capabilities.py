"""Public, offline capability checks for the SaberOps package.

Every check here is hermetic: deterministic fake adapters stand in for
provider CLIs, so no real provider or model is contacted and no credentials
are required.  The suite covers the capabilities SaberOps v0.3.0 advertises:
model identity preservation, exact binding and role separation, routing
through exact worker identity, and the deterministic lifecycle result.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Generator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from saberops.cli import build_parser
from saberops.control_plane import (
    BindingRole,
    build_binding_registry,
    materialize_discovered_binding,
)
from saberops.control_plane.bindings import resolve_execution_binding
from saberops.db import Database
from saberops.model_access import (
    ReadinessService,
    ReadinessStore,
    build_access_registry,
    default_account_connections,
)
from saberops.models import (
    AttemptStatus,
    RunStatus,
    Tier,
    WorkerRequest,
    WorkerResult,
)
from saberops.process import run_process
from saberops.routing_config import format_candidate_id
from saberops.service import OrchestratorService
from saberops.workers.base import WorkerAdapter
from saberops.workers.instruction_compat import (
    InstructionCompatibility,
    instruction_compatibility_for_opencode_model,
)
from saberops.workers.registry import AdapterRegistry


@pytest.fixture(autouse=True, scope="module")
def _isolate_xdg(tmp_path_factory: pytest.TempPathFactory) -> Generator[None, None, None]:
    """Keep this suite away from any real SaberOps state on the machine."""
    patch = pytest.MonkeyPatch()
    for variable in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        patch.setenv(variable, str(tmp_path_factory.mktemp(f"hermetic-{variable}")))
    yield
    patch.undo()


# ---------------------------------------------------------------------------
# Hermetic fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeAdapter(WorkerAdapter):
    """A deterministic stand-in for a provider CLI adapter."""

    provider: str
    output_text: str = "bounded guidance"
    call_count: int = 0
    observed_models: list[str] = field(default_factory=list)
    observed_efforts: list[Any] = field(default_factory=list)
    observed_envs: list[dict[str, str]] = field(default_factory=list)

    @property
    def provider_name(self) -> str:
        return self.provider

    def is_available(self) -> bool:
        return True

    def run(self, request: WorkerRequest) -> WorkerResult:
        self.call_count += 1
        self.observed_models.append(request.model)
        self.observed_efforts.append(request.reasoning_effort)
        self.observed_envs.append(dict(request.env or {}))
        return WorkerResult(
            provider=self.provider,
            model=request.model,
            status=AttemptStatus.SUCCESS,
            summary="ok",
            stdout=self.output_text,
            stderr="",
            changed_paths=[],
            has_changes=False,
        )


# ---------------------------------------------------------------------------
# Package and CLI
# ---------------------------------------------------------------------------


def test_cli_exposes_local_commands() -> None:
    parser = build_parser()
    assert parser.parse_args(["models"]).command == "models"
    assert parser.parse_args(["ui"]).command == "ui"


def test_packaged_runtime_resources_are_present() -> None:
    package = Path(__file__).resolve().parents[1] / "src" / "saberops"
    for template in ("setup.html", "accounts.html", "questions.html"):
        assert (package / "templates" / template).is_file(), template
    assert (package / "static" / "css" / "app.css").is_file()
    assert (package / "static" / "js" / "console.js").is_file()
    assert (package / "templates" / "base.html").is_file()
    assert (package / "defaults" / "routing.json").is_file()


# ---------------------------------------------------------------------------
# Model identity preservation
# ---------------------------------------------------------------------------


def test_discovered_identities_stay_distinct() -> None:
    """Same short model name on different upstreams keeps distinct identity."""
    opencode, _, _ = materialize_discovered_binding(
        connection_id="opencode-account",
        provider="opencode",
        model="opencode/gemini-3.5-flash-lite",
    )
    google, _, _ = materialize_discovered_binding(
        connection_id="opencode-account",
        provider="opencode",
        model="google/gemini-3.5-flash-lite",
    )
    go, _, _ = materialize_discovered_binding(
        connection_id="opencode-account",
        provider="opencode",
        model="opencode-go/deepseek-v4-pro",
    )
    assert opencode.model == "opencode/gemini-3.5-flash-lite"
    assert google.model == "google/gemini-3.5-flash-lite"
    assert go.model == "opencode-go/deepseek-v4-pro"
    assert len({opencode.binding_id, google.binding_id, go.binding_id}) == 3
    assert format_candidate_id("opencode", opencode.model) == (
        "opencode/opencode/gemini-3.5-flash-lite"
    )
    assert format_candidate_id("opencode", google.model) == (
        "opencode/google/gemini-3.5-flash-lite"
    )


def test_instruction_compatibility_distinguishes_upstreams() -> None:
    """Verified upstreams are FULL; siblings are UNKNOWN; GC is INCOMPATIBLE."""
    assert (
        instruction_compatibility_for_opencode_model("opencode/gemini-3.5-flash-lite")
        is InstructionCompatibility.FULL
    )
    assert (
        instruction_compatibility_for_opencode_model("google/gemini-3.5-flash-lite")
        is InstructionCompatibility.UNKNOWN
    )
    assert (
        instruction_compatibility_for_opencode_model("generalcompute/deepseek-v3.1")
        is InstructionCompatibility.INCOMPATIBLE
    )
    assert (
        instruction_compatibility_for_opencode_model("some-new-lab/model-1")
        is InstructionCompatibility.UNKNOWN
    )


# ---------------------------------------------------------------------------
# Exact binding and role separation
# ---------------------------------------------------------------------------


def test_binding_roles_share_resource_but_stay_distinct() -> None:
    """Role splits the binding id, never the account or quota resource."""
    worker, w_profile, w_pool = materialize_discovered_binding(
        connection_id="opencode-account",
        provider="opencode",
        model="opencode/gemini-3.5-flash-lite",
        binding_role=BindingRole.BUILD,
    )
    orchestrator, o_profile, o_pool = materialize_discovered_binding(
        connection_id="opencode-account",
        provider="opencode",
        model="opencode/gemini-3.5-flash-lite",
        binding_role=BindingRole.PLAN,
    )
    assert worker.binding_role is BindingRole.BUILD
    assert orchestrator.binding_role is BindingRole.PLAN
    assert worker.binding_id != orchestrator.binding_id
    assert w_profile.profile_id == o_profile.profile_id
    assert w_pool.pool_id == o_pool.pool_id


def test_exact_worker_binding_resolves_to_selected_identity() -> None:
    """The resolver returns the exact binding id, not a first match."""
    worker_a, profile_a, pool_a = materialize_discovered_binding(
        connection_id="opencode-account",
        provider="opencode",
        model="opencode/gemini-3.5-flash-lite",
        binding_role=BindingRole.BUILD,
    )
    adapter = _FakeAdapter(provider="opencode")
    registry = build_binding_registry(
        bindings=(worker_a,), profiles=(profile_a,), pools=(pool_a,)
    )
    resolved = resolve_execution_binding(
        registry=registry, binding_id=worker_a.binding_id, adapter=adapter
    )
    assert resolved.binding_id == worker_a.binding_id
    assert resolved.binding.model == "opencode/gemini-3.5-flash-lite"
    assert resolved.binding.binding_role is BindingRole.BUILD


# ---------------------------------------------------------------------------
# Deterministic lifecycle result
# ---------------------------------------------------------------------------


class _MockWorker(WorkerAdapter):
    @property
    def provider_name(self) -> str:
        return "opencode"

    def is_available(self) -> bool:
        return True

    def run(self, request: WorkerRequest) -> WorkerResult:
        if request.read_only:
            return WorkerResult(
                provider=self.provider_name,
                model=request.model,
                status=AttemptStatus.SUCCESS,
                summary="Scripted reviewer approved",
                stdout="VERDICT: PASS\n",
                stderr="",
            )
        (request.worktree_path / "calc.py").write_text("def add(a, b): return a + b\n")
        return WorkerResult(
            provider=self.provider_name,
            model=request.model,
            status=AttemptStatus.SUCCESS,
            summary="implemented add",
            stdout="done",
            stderr="",
            changed_paths=["calc.py"],
            has_changes=True,
        )


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    run_process(["git", "init", "-b", "main"], cwd=path)
    run_process(["git", "config", "user.name", "Test Runner"], cwd=path)
    run_process(["git", "config", "user.email", "test@localhost"], cwd=path)
    (path / "calc.py").write_text("# placeholder\n")
    run_process(["git", "add", "."], cwd=path)
    run_process(["git", "commit", "-m", "init"], cwd=path)


def _ready_readiness_service(tmp_path: Path) -> ReadinessService:
    service = ReadinessService(
        build_access_registry(default_account_connections()),
        ReadinessStore(tmp_path / "access-readiness.json"),
        which=lambda _name: "/fake/bin",
        max_age=timedelta(days=36500),
    )
    service.record_success("opencode-account")
    service.record_success("codex-chatgpt-account")
    return service


def _seed_verified_worker_posture(config_home: Path) -> None:
    """Seed an isolated owner config with a verified Class-A posture for the
    fake adapter's exact candidate identity used by the lifecycle test.

    Covers: WORKFLOW §7 — explicitly authorized SETUP-ONLY
    fixture addition (orchestrator disposition `orch-f561e-disposition.md`,
    finding B1). This adds ONLY a synthetic, test-only policy record for the
    deterministic fake route; no production default, packaged policy, existing
    assertion, test name, skip marker or workflow semantics are changed, and no
    real vendor receives an attestation.
    """
    cfg_dir = config_home / "saberops"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    today_iso = datetime.now(UTC).date().isoformat()
    cp_doc = {
        "version": 2,
        "providers": {"opencode": {"state": "ALLOWED"}},
        # Test-only UNENFORCED pools so the fake adapter's routes need no
        # capacity evidence; mirrors the authorized fixture pattern used by
        # the other migrated suites. No production/packaged policy is touched.
        "quota_pools": {
            "opencode-go": {"mode": "UNENFORCED", "overage_possible": False},
            "opencode-zen": {"mode": "UNENFORCED", "overage_possible": False},
        },
        "postures": {
            # Provider identity of the deterministic fake adapter
            # (`_MockWorker.provider_name == "opencode"`), covering the
            # packaged WORKER chain head and the reviewer ladder candidates
            # that the same fake adapter serves.
            "opencode": {
                "class": "A",
                "policy_urls": ["https://example.com/opencode-policy"],
                "ttl_days": 90,
                "verified_on": today_iso,
                "note": "synthetic verified test posture for lifecycle fake worker",
            }
        },
    }
    (cfg_dir / "control_plane.json").write_text(json.dumps(cp_doc), encoding="utf-8")


def test_worker_success_and_passing_gate_reaches_completed_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    _seed_verified_worker_posture(tmp_path / "xdg-config")
    repo = tmp_path / "repo"
    _init_repo(repo)
    db = Database(tmp_path / "orch.db")
    service = OrchestratorService(
        db=db,
        registry=AdapterRegistry([_MockWorker()]),
        worktree_base=tmp_path / "worktrees",
        readiness_service=_ready_readiness_service(tmp_path),
    )
    result = service.run_sync(
        task="Add function",
        repo_path=repo,
        gate_command=f"{sys.executable} -c pass",
        tier=Tier.T1,
    )
    assert result.passed is True
    assert result.status == RunStatus.COMPLETED
