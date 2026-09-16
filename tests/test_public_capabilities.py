"""Public, offline capability checks for the SaberOps package.

Every check here is hermetic: deterministic fake adapters stand in for
provider CLIs, so no real provider or model is contacted and no credentials
are required.  The suite covers the capabilities SaberOps v0.3.0 advertises:
model identity preservation, exact binding and role separation, the bounded
Orchestrator-model consumer, provider-default effort behaviour, routing
through exact worker identity, and the deterministic lifecycle result.
"""

from __future__ import annotations

import sys
from collections.abc import Generator
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from saberops.cli import build_parser
from saberops.context_projection import build_initial_projection
from saberops.control_plane import (
    BindingKind,
    BindingRole,
    ExecutionBinding,
    ExecutionProfile,
    OrchBindingPolicy,
    OwnerBindings,
    QuotaPool,
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
    OrchBindingMode,
    ReasoningEffort,
    RunStatus,
    Tier,
    WorkerRequest,
    WorkerResult,
)
from saberops.orchestrator_consumer import (
    OrchestratorModelConsumer,
    OrchestratorModelInvocationError,
    OrchestratorModelRequest,
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


def _binding(
    *,
    binding_id: str,
    provider: str,
    model: str,
    profile_id: str,
    quota_pool_id: str,
    role: BindingRole = BindingRole.ORCHESTRATOR,
    efforts: frozenset[ReasoningEffort] = frozenset(),
) -> ExecutionBinding:
    return ExecutionBinding(
        binding_id=binding_id,
        provider=provider,
        model=model,
        backend=BindingKind.DIRECT_CLI,
        profile_id=profile_id,
        quota_pool_id=quota_pool_id,
        binding_role=role,
        reasoning_effort_capabilities=efforts,
    )


def _profile(profile_id: str, provider: str) -> ExecutionProfile:
    return ExecutionProfile(profile_id=profile_id, provider=provider)


def _pool(pool_id: str, provider: str) -> QuotaPool:
    return QuotaPool(pool_id=pool_id, provider=provider)


def _owner(
    *,
    binding: ExecutionBinding,
    profile: ExecutionProfile,
    pool: QuotaPool,
) -> OwnerBindings:
    return OwnerBindings(
        registry=build_binding_registry(
            bindings=(binding,), profiles=(profile,), pools=(pool,)
        ),
        policy=OrchBindingPolicy(
            mode=OrchBindingMode.PINNED, pinned_binding_id=binding.binding_id
        ),
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
    assert (package / "templates" / "base.html").is_file()
    assert (package / "static" / "css" / "style.css").is_file()
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
        binding_role=BindingRole.WORKER,
    )
    orchestrator, o_profile, o_pool = materialize_discovered_binding(
        connection_id="opencode-account",
        provider="opencode",
        model="opencode/gemini-3.5-flash-lite",
        binding_role=BindingRole.ORCHESTRATOR,
    )
    assert worker.binding_role is BindingRole.WORKER
    assert orchestrator.binding_role is BindingRole.ORCHESTRATOR
    assert worker.binding_id != orchestrator.binding_id
    assert w_profile.profile_id == o_profile.profile_id
    assert w_pool.pool_id == o_pool.pool_id


def test_exact_worker_binding_resolves_to_selected_identity() -> None:
    """The resolver returns the exact binding id, not a first match."""
    worker_a, profile_a, pool_a = materialize_discovered_binding(
        connection_id="opencode-account",
        provider="opencode",
        model="opencode/gemini-3.5-flash-lite",
        binding_role=BindingRole.WORKER,
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
    assert resolved.binding.binding_role is BindingRole.WORKER


# ---------------------------------------------------------------------------
# Bounded Orchestrator-model consumer
# ---------------------------------------------------------------------------


def test_orchestrator_consumer_uses_exact_pinned_binding() -> None:
    adapter = _FakeAdapter(provider="opencode", output_text="focus on tests")
    registry = AdapterRegistry([adapter])
    binding = _binding(
        binding_id="orch-exact",
        provider="opencode",
        model="opencode/gemini-3.5-flash-lite",
        profile_id="p-orch",
        quota_pool_id="pool-orch",
    )
    consumer = OrchestratorModelConsumer(
        db=None,
        registry=registry,
        owner_bindings=_owner(
            binding=binding,
            profile=_profile("p-orch", "opencode"),
            pool=_pool("pool-orch", "opencode"),
        ),
    )
    result = consumer.invoke(
        OrchestratorModelRequest(user_prompt="hi", timeout_seconds=5.0)
    )
    assert result.status == "SUCCESS"
    assert adapter.call_count == 1
    assert adapter.observed_models == ["opencode/gemini-3.5-flash-lite"]
    assert result.binding_id == "orch-exact"
    assert result.model == "opencode/gemini-3.5-flash-lite"


def test_worker_role_binding_is_refused_by_orchestrator_consumer() -> None:
    adapter = _FakeAdapter(provider="opencode")
    registry = AdapterRegistry([adapter])
    worker = _binding(
        binding_id="worker-only",
        provider="opencode",
        model="opencode/gemini-3.5-flash-lite",
        profile_id="p-worker",
        quota_pool_id="pool-worker",
        role=BindingRole.WORKER,
    )
    consumer = OrchestratorModelConsumer(
        db=None,
        registry=registry,
        owner_bindings=_owner(
            binding=worker,
            profile=_profile("p-worker", "opencode"),
            pool=_pool("pool-worker", "opencode"),
        ),
    )
    with pytest.raises(OrchestratorModelInvocationError):
        consumer.invoke(OrchestratorModelRequest(user_prompt="hi", timeout_seconds=5.0))
    assert adapter.call_count == 0


# ---------------------------------------------------------------------------
# Provider-default effort behaviour
# ---------------------------------------------------------------------------


def test_provider_default_effort_when_capabilities_unknown() -> None:
    """Empty capabilities + no explicit tier -> provider default, no fabrication."""
    adapter = _FakeAdapter(provider="opencode")
    registry = AdapterRegistry([adapter])
    binding = _binding(
        binding_id="orch-default",
        provider="opencode",
        model="opencode/gemini-3.5-flash-lite",
        profile_id="p-default",
        quota_pool_id="pool-default",
        efforts=frozenset(),
    )
    consumer = OrchestratorModelConsumer(
        db=None,
        registry=registry,
        owner_bindings=_owner(
            binding=binding,
            profile=_profile("p-default", "opencode"),
            pool=_pool("pool-default", "opencode"),
        ),
    )
    result = consumer.invoke(
        OrchestratorModelRequest(user_prompt="hi", timeout_seconds=5.0)
    )
    assert result.status == "SUCCESS"
    assert result.reasoning_effort is None
    assert adapter.observed_efforts == [None]
    assert binding.reasoning_effort_capabilities == frozenset()


def test_explicit_unsupported_effort_fails_closed() -> None:
    adapter = _FakeAdapter(provider="opencode")
    registry = AdapterRegistry([adapter])
    binding = _binding(
        binding_id="orch-default",
        provider="opencode",
        model="opencode/gemini-3.5-flash-lite",
        profile_id="p-default",
        quota_pool_id="pool-default",
        efforts=frozenset(),
    )
    consumer = OrchestratorModelConsumer(
        db=None,
        registry=registry,
        owner_bindings=_owner(
            binding=binding,
            profile=_profile("p-default", "opencode"),
            pool=_pool("pool-default", "opencode"),
        ),
    )
    with pytest.raises(OrchestratorModelInvocationError):
        consumer.invoke(
            OrchestratorModelRequest(
                user_prompt="hi",
                reasoning_effort=ReasoningEffort.HIGH,
                timeout_seconds=5.0,
            )
        )
    assert adapter.call_count == 0


# ---------------------------------------------------------------------------
# Guidance reaches the first worker
# ---------------------------------------------------------------------------


def test_bounded_guidance_reaches_first_worker_context() -> None:
    with_guidance = build_initial_projection(
        original_task="implement X",
        role="BULK",
        orchestrator_guidance="focus on bounded validation",
    )
    titles = tuple(title for title, _ in with_guidance.extra_sections)
    assert "ORCHESTRATOR GUIDANCE" in titles

    without_guidance = build_initial_projection(
        original_task="implement X",
        role="BULK",
        orchestrator_guidance=None,
    )
    titles_without = tuple(title for title, _ in without_guidance.extra_sections)
    assert "ORCHESTRATOR GUIDANCE" not in titles_without


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


def test_worker_success_and_passing_gate_reaches_completed_state(tmp_path: Path) -> None:
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
