"""Application service layer for Orchestrator MVP.

Provides a unified facade for Web UI and CLI operations, event access, model
discovery, and delegating to authoritative core engines.  Asynchronous run
start is delegated to the durable detached execution-owner supervisor
(:class:`saberops.supervisor.RunSupervisor`), so no run ever executes
in the caller's process: the caller may exit freely once the owner is launched.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from saberops.accept import AcceptEligibility, AcceptEngine, AcceptResult
from saberops.config import RunConfig, resolve_run_config
from saberops.control_plane.binding_store import (
    BindingStoreError,
    load_owner_bindings,
)
from saberops.control_plane.orch_binding import (
    OrchSelectionStatus,
    select_orch_binding,
)
from saberops.control_plane.reviewer_binding import (
    ReviewerSelectionError,
    reviewer_candidate_for_dispatch,
)
from saberops.db import Database
from saberops.dispatch import UnavailableReadinessService
from saberops.gate_runner import (
    GateScratchMode,
    GateScratchPolicyError,
    load_project_gate_scratch_policy,
)
from saberops.model_access import ReadinessService
from saberops.models import (
    OrchEvent,
    QuotaState,
    ReasoningEffort,
    ReviewResult,
    Run,
    RunResult,
    StartRunResult,
    Tier,
    WorkerCandidate,
)
from saberops.observability import build_run_observability_snapshot
from saberops.orchestrator import Orchestrator
from saberops.project import owner_state_dir
from saberops.review import ReviewEngine
from saberops.routing import is_peak_hours
from saberops.routing_config import load_effective_routing
from saberops.tooling import ToolRegistry
from saberops.workers.registry import AdapterRegistry

if TYPE_CHECKING:
    from saberops.candidate_lifecycle import CandidateProjection
    from saberops.supervisor import RunSupervisor


class OrchestratorService:
    """Unified application service facade for Web UI and CLI."""

    def __init__(
        self,
        db: Database | None = None,
        registry: AdapterRegistry | None = None,
        worktree_base: Path | None = None,
        tool_registry: ToolRegistry | None = None,
        readiness_service: ReadinessService | bool | None = None,
    ) -> None:
        self.db = db or Database()
        self.registry = registry or AdapterRegistry.default()
        self.worktree_base = worktree_base
        self.tool_registry = tool_registry or ToolRegistry.from_environment()
        # C15-RDY-02A: the composition layer owns readiness acquisition.
        # ``None``/``True`` builds the owner-state service; when that
        # authority cannot be constructed/read the seam resolves to the
        # fail-closed unavailable fallback (automatic dispatch sees
        # UNKNOWN and skips) -- never to "no gating".  Only an explicit
        # ``False`` disables gating, and that is a test/compatibility
        # control that production construction can never reach by
        # accident.  An instance injects a fake/override.
        self.readiness_service: ReadinessService | UnavailableReadinessService | None
        if readiness_service is None or readiness_service is True:
            try:
                self.readiness_service = ReadinessService.for_owner_state(
                    owner_state_dir()
                )
            except Exception:
                self.readiness_service = UnavailableReadinessService()
        elif readiness_service is False:
            self.readiness_service = None
        else:
            self.readiness_service = readiness_service
        self.orchestrator = Orchestrator(
            db=self.db,
            registry=self.registry,
            worktree_base=self.worktree_base,
            tool_registry=self.tool_registry,
            readiness_service=self.readiness_service,
        )
        self._supervisor: RunSupervisor | None = None

    def _durable_supervisor(self) -> RunSupervisor:
        """Return the service's durable execution supervisor (lazily built).

        The local import keeps the module import graph cycle-free; every
        asynchronous launch must converge on this one durable supervisor.
        """
        if self._supervisor is None:
            from saberops.supervisor import RunSupervisor

            self._supervisor = RunSupervisor(
                db=self.db,
                worktree_base=self.worktree_base,
                registry=self.registry,
                tool_registry=self.tool_registry,
            )
        return self._supervisor

    @staticmethod
    def resolve_project_gate_scratch_mode(
        repo_path: Path | str,
    ) -> GateScratchMode:
        """Resolve the project's persisted gate-scratch mode for run creation.

        Centralized single-read entry point so every caller (Web UI, CLI,
        Manager) sees the same policy and freezes the same resolved value
        into the run's ``RunConfig``.  A malformed/unreadable policy fails
        closed (the engine never silently falls back from a corrupt
        project authority), while a missing policy resolves to the
        product default ``DISK``.
        """
        try:
            return load_project_gate_scratch_policy(repo_path)
        except GateScratchPolicyError as exc:
            # Re-raise so the caller (UI/CLI) sees the same typed error
            # the persistence layer surfaces.  We never silently revert
            # corrupt project authority.
            raise GateScratchPolicyError(
                f"Project gate-scratch policy at {repo_path} is invalid: {exc}"
            ) from exc

    @staticmethod
    def get_project_gate_scratch_policy(
        repo_path: Path | str,
    ) -> dict[str, object]:
        """Return the rendered project gate-scratch policy for UI/CLI display.

        Both surfaces consume the same shape so a single source of truth
        owns the user vocabulary.  A missing policy reports the DISK
        default; a corrupt policy surfaces the typed failure so the
        caller can refuse to render rather than silently downgrade.
        """
        from saberops.gate_runner import GATE_SCRATCH_LABELS

        mode = OrchestratorService.resolve_project_gate_scratch_mode(repo_path)
        return {
            "mode": mode.value,
            "label": GATE_SCRATCH_LABELS[mode],
            "is_default": mode == GateScratchMode.DISK,
        }

    @staticmethod
    def set_project_gate_scratch_policy(
        repo_path: Path | str,
        mode: GateScratchMode | str,
    ) -> Path:
        """Validate and atomically persist the project's gate-scratch mode.

        The caller (UI/CLI) gets the on-disk path back so it can echo the
        exact file in diagnostics and surface the same path both
        surfaces agree on.
        """
        from saberops.gate_runner import set_project_gate_scratch_policy

        return set_project_gate_scratch_policy(repo_path, mode)

    def start_run(
        self,
        task: str,
        repo_path: Path | str,
        gate_command: str = "make gate",
        tier: Tier | None = None,
        routing_mode: str = "auto",
        manual_tier: Tier | str | None = None,
        max_auto_tier: Tier | str | None = None,
        training_allowed: bool = True,
        provider_override: str | None = None,
        model_override: str | None = None,
        required_capabilities: tuple[str, ...] = (),
        max_attempts: int = 2,
        worker_timeout: float | None = None,
        gate_timeout: float | None = None,
        review: bool = False,
        review_timeout: float | None = None,
        config: RunConfig | None = None,
        tool_refs: tuple[str, ...] = (),
        gate_scratch_mode: GateScratchMode | str | None = None,
    ) -> StartRunResult:
        """Resolve the RunConfig and hand execution to the durable supervisor.

        Nothing executes in this process.  The PENDING run is created, its
        durable execution ownership is claimed, and the detached execution
        owner is launched by :meth:`RunSupervisor.start_supervised_run` in its
        own session -- the exact same caller-independent lifecycle the Web
        application and the CLI async start path use -- so this caller may
        exit at any time without affecting the run.

        When ``gate_scratch_mode`` is not supplied, the project's persisted
        gate-scratch policy is read ONCE at run creation and frozen into
        the resulting :class:`RunConfig`.  An already-created run therefore
        never re-reads mutable project authority.
        """
        resolved_gate_scratch_mode = (
            gate_scratch_mode
            if gate_scratch_mode is not None
            else self.resolve_project_gate_scratch_mode(repo_path)
        )
        config = config or resolve_run_config(
            task=task,
            repo_path=repo_path,
            tier=tier,
            routing_mode=routing_mode,
            manual_tier=manual_tier,
            max_auto_tier=max_auto_tier,
            training_allowed=training_allowed,
            provider_override=provider_override,
            model_override=model_override,
            required_capabilities=required_capabilities,
            gate_command=gate_command,
            max_attempts=max_attempts,
            worker_timeout=worker_timeout,
            gate_timeout=gate_timeout,
            review=review,
            review_timeout=review_timeout,
            tool_refs=tool_refs,
            gate_scratch_mode=resolved_gate_scratch_mode,
        )
        return self._durable_supervisor().start_supervised_run(config)

    def run_sync(
        self,
        task: str,
        repo_path: Path | str,
        gate_command: str = "make gate",
        tier: Tier | None = None,
        routing_mode: str = "auto",
        manual_tier: Tier | str | None = None,
        max_auto_tier: Tier | str | None = None,
        model_override: str | None = None,
        provider_override: str | None = None,
        required_capabilities: tuple[str, ...] = (),
        training_allowed: bool = True,
        is_peak: bool | None = None,
        max_attempts: int = 2,
        worker_timeout: float | None = None,
        gate_timeout: float | None = None,
        review: bool = False,
        review_timeout: float | None = None,
        config: RunConfig | None = None,
        run_id: str | None = None,
        tool_refs: tuple[str, ...] = (),
        gate_scratch_mode: GateScratchMode | str | None = None,
    ) -> RunResult:
        """Execute a run synchronously (used by CLI orch run).

        Mirrors :meth:`start_run` for the synchronous path: when the
        caller does not supply ``gate_scratch_mode`` explicitly, the
        project's persisted policy is read once and frozen into the
        RunConfig used by the durable supervisor.
        """
        resolved_gate_scratch_mode = (
            gate_scratch_mode
            if gate_scratch_mode is not None
            else self.resolve_project_gate_scratch_mode(repo_path)
        )
        config = config or resolve_run_config(
            task=task,
            repo_path=repo_path,
            tier=tier,
            routing_mode=routing_mode,
            manual_tier=manual_tier,
            max_auto_tier=max_auto_tier,
            training_allowed=training_allowed,
            provider_override=provider_override,
            model_override=model_override,
            required_capabilities=required_capabilities,
            gate_command=gate_command,
            max_attempts=max_attempts,
            worker_timeout=worker_timeout,
            gate_timeout=gate_timeout,
            review=review,
            review_timeout=review_timeout,
            tool_refs=tool_refs,
            gate_scratch_mode=resolved_gate_scratch_mode,
        )
        return self.orchestrator.run(config=config, is_peak=is_peak, run_id=run_id)

    def recover_sync(
        self,
        run_id: str,
        *,
        config: RunConfig | None = None,
    ) -> RunResult:
        """Dedicated recovery entry that delegates to :meth:`Orchestrator.recover_run`.

        Distinct from :meth:`run_sync`: this service method is reached only
        from the explicit ``orch recover`` CLI subcommand, after the
        supervisor has claimed a fenced execution-owner generation for this
        caller.  The fencing token (an ``OWNER_ENV_GENERATION`` set by the
        parent supervisor) is read here so the underlying orchestrator can
        confirm authority for exactly that generation.

        Direct callers without a fenced token (no env override and no explicit
        ``expected_owner_generation`` argument) fail closed with a structured
        ownership error before any checkpoint or attempt mutation.
        """
        from saberops.ownership import parse_owner_reservation

        reservation = parse_owner_reservation()
        return self.orchestrator.recover_run(
            run_id=run_id,
            config=config,
            expected_owner_id=reservation.owner_id if reservation is not None else None,
            expected_owner_generation=reservation.generation if reservation is not None else None,
        )

    def get_run(self, run_id: str) -> Run | None:
        """Fetch run details by ID."""
        return self.db.get_run(run_id)

    def list_runs(self, limit: int = 50) -> list[Run]:
        """Fetch list of recent runs."""
        return self.db.list_runs(limit=limit)

    def get_events(self, run_id: str, after_id: int = 0, limit: int = 1000) -> list[OrchEvent]:
        """Fetch ordered events for a run."""
        return self.db.get_events(run_id=run_id, after_id=after_id, limit=limit)

    def get_observability_snapshot(self, run_id: str) -> dict[str, Any]:
        """Return a deterministic, read-only C06 run snapshot."""
        return build_run_observability_snapshot(self.db, run_id)

    def get_plan(self, run_id: str) -> dict[str, Any]:
        """Return the stable persisted plan/service surface for UI and consoles."""
        plan = self.db.get_run_plan(run_id)
        supervision = self.db.get_supervision(run_id)
        return {
            "plan": plan,
            "steps": self.db.get_plan_steps(run_id),
            "packages": self.db.get_work_packages(run_id),
            "supervision": supervision,
            "current_candidate": next(
                (
                    attempt.commit_sha
                    for attempt in reversed(self.db.get_attempts_for_run(run_id))
                    if attempt.commit_sha
                ),
                None,
            ),
            "latest_handoff": self.db.get_latest_handoff(run_id),
        }

    def review_run(
        self,
        run_id: str,
        reviewer_override: WorkerCandidate | None = None,
        timeout: float = 1200.0,
        review_limit: int | None = None,
    ) -> ReviewResult:
        """Execute independent code review for a completed run.

        R3-E1: when the caller supplies no explicit override, the
        owner's configured exact Reviewer binding (if any) is resolved
        to a single :class:`WorkerCandidate` carrying the exact
        provider, exact model, and exact ``binding_id``.  An
        unconfigured owner keeps the legacy review ladder
        (``None``).  A configured-but-invalid reviewer fails closed
        here -- it is never silently replaced by another reviewer.
        The existing :class:`ReviewEngine` remains authoritative for
        whether the candidate can actually launch.
        """
        if reviewer_override is None:
            reviewer_override = self._configured_reviewer_candidate()
        engine = ReviewEngine(
            db=self.db,
            registry=self.registry,
            readiness_service=self.readiness_service,
        )
        return engine.review_run(
            run_id=run_id,
            reviewer_override=reviewer_override,
            timeout=timeout,
            review_limit=review_limit,
        )

    def _configured_reviewer_candidate(self) -> WorkerCandidate | None:
        """Resolve the owner's exact Reviewer selection, if one is set.

        Returns ``None`` when no reviewer is configured (legacy ladder
        stays available).  Raises :class:`ReviewerSelectionError` when
        a configured reviewer is missing, wrong-role, unresolvable, or
        when the owner document itself is unreadable (a corrupt store
        must never be mistaken for "no reviewer configured").
        """
        try:
            owner = load_owner_bindings()
        except BindingStoreError as exc:
            raise ReviewerSelectionError(
                "REVIEWER_UNRESOLVABLE",
                f"owner reviewer state unreadable; refusing review: {exc}",
            ) from exc
        return reviewer_candidate_for_dispatch(owner, adapter_registry=self.registry)

    def accept_eligibility(self, run_id: str) -> AcceptEligibility:
        """Return the engine-derived read-only acceptance projection.

        The Web renders this as a checklist and gates the Accept button
        on it; the projection never mutates state and never recreates
        acceptance policy.
        """
        engine = AcceptEngine(db=self.db)
        return engine.check_accept_eligibility(run_id=run_id)

    def accept_run(self, run_id: str, gate_timeout: float = 300.0) -> AcceptResult:
        """Safely integrate a verified candidate run into the target repository."""
        engine = AcceptEngine(db=self.db)
        return engine.accept_run(run_id=run_id, gate_timeout=gate_timeout)

    def reject_candidate(self, run_id: str) -> CandidateProjection:
        """Record a durable owner Reject decision for the current candidate.

        The single service-level Reject authority: delegates to
        :func:`saberops.candidate_lifecycle.reject_current_candidate`,
        the only seam that may write a ``candidate_rejected`` event.
        The Web calls this method; it never writes rejection events
        directly.  Repeats for the same exact candidate resolve
        idempotently without a duplicate event.
        """
        from saberops.candidate_lifecycle import reject_current_candidate

        return reject_current_candidate(self.db, run_id)

    def can_reject_candidate(self, run_id: str) -> bool:
        """Return whether the current candidate may receive a new decision.

        Derived from the same projection authority as
        :meth:`reject_candidate` (never from run status alone).
        """
        from saberops.candidate_lifecycle import can_reject_candidate

        return can_reject_candidate(self.db, run_id)

    def cleanup_run(self, run_id: str) -> dict[str, list[str]]:
        """Safely clean up clean attempt worktrees for a run."""
        return self.orchestrator.cleanup_run(run_id=run_id)

    def list_models(self) -> dict[str, Any]:
        """Return model routing tiers, current schedule, and quota states."""
        peak = is_peak_hours()
        quota_states = self.db.list_quota()
        snap = load_effective_routing()
        return {
            "peak": peak,
            "quota_states": quota_states,
            "tier_1_chain": snap.t1_default,
            "tier_2_training_allowed_chain": snap.t2_training_allowed,
            "tier_2_off_peak_chain": snap.t2_training_denied_off_peak,
            "tier_2_peak_chain": snap.t2_training_denied_peak,
            "tier_3_off_peak_chain": snap.t3_off_peak,
            "tier_3_peak_chain": snap.t3_peak,
            "routing_snapshot": snap,
        }

    def get_quota(self, provider: str, pool: str) -> QuotaState | None:
        """Fetch quota state for a provider pool."""
        return self.db.get_quota(provider=provider, pool=pool)

    def list_quota(self) -> list[QuotaState]:
        """List all configured quota states."""
        return self.db.list_quota()

    def set_quota(
        self,
        provider: str,
        pool: str,
        remaining_percent: float,
        reset_at: str | None = None,
    ) -> QuotaState:
        """Set quota state for a provider pool."""
        return self.db.set_quota(
            provider=provider,
            pool=pool,
            remaining_percent=remaining_percent,
            reset_at=reset_at,
        )

    def enable_quota(self, provider: str, pool: str) -> QuotaState:
        """Re-enable normal routing state for a provider pool."""
        return self.db.enable_quota(provider=provider, pool=pool)

    def invoke_orchestrator_model(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        reasoning_effort: ReasoningEffort | str | None = None,
        timeout_seconds: float = 60.0,
        correlation_id: str | None = None,
        run_id: str | None = None,
    ) -> Any:
        """Invoke the persisted Orchestrator-model binding through the consumer.

        This is the production seam that connects the persisted owner
        selection (the :class:`OrchBindingPolicy` saved on
        ``/orchestrator``) to the actual model call.  No provider,
        model, binding id, or transport is accepted from the caller:
        every one of those comes exclusively from the owner document,
        and a refusal surfaces as
        :class:`OrchestratorModelInvocationError` with a typed reason
        -- never a silent launch against a sibling binding.
        """
        from saberops.orchestrator_consumer import (
            OrchestratorModelConsumer,
            OrchestratorModelInvocationError,
            OrchestratorModelRequest,
            OrchestratorModelResult,
        )

        coerced_effort: ReasoningEffort | None
        if reasoning_effort is None:
            coerced_effort = None
        elif isinstance(reasoning_effort, ReasoningEffort):
            coerced_effort = reasoning_effort
        else:
            try:
                coerced_effort = ReasoningEffort(
                    str(reasoning_effort).strip().upper()
                )
            except ValueError as exc:
                raise OrchestratorModelInvocationError(
                    "ORCH_MODEL_UNKNOWN_EFFORT",
                    message=f"unknown effort tier {reasoning_effort!r}",
                ) from exc

        consumer = OrchestratorModelConsumer(
            db=self.db,
            registry=self.registry,
        )
        request = OrchestratorModelRequest(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            reasoning_effort=coerced_effort,
            timeout_seconds=timeout_seconds,
            correlation_id=correlation_id,
            run_id=run_id,
        )
        result: OrchestratorModelResult = consumer.invoke(request)
        return result

    def get_orchestrator_model_status(self) -> dict[str, Any]:
        """Read-only introspection of the persisted Orchestrator selection.

        The dashboard and CLI both surface the same shape so the
        ``/orchestrator`` page can truthfully report whether the
        selected binding is the one the production consumer would
        actually invoke.
        """
        owner = load_owner_bindings()
        if owner.policy is None:
            return {
                "configured": False,
                "selection_status": OrchSelectionStatus.UNRESOLVED.value,
                "reason": "ORCH_MODEL_NO_POLICY",
            }
        selection = select_orch_binding(owner.policy, owner.registry)
        binding = selection.binding
        profile = None
        pool = None
        if binding is not None:
            try:
                profile = owner.registry.profile_of(binding)
                pool = owner.registry.pool_of(binding)
            except KeyError:
                profile = None
                pool = None
        return {
            "configured": True,
            "policy_mode": owner.policy.mode.value,
            "pinned_binding_id": owner.policy.pinned_binding_id,
            "selection_status": selection.status.value,
            "selection_reason": selection.reason,
            "binding": None
            if binding is None
            else {
                "binding_id": binding.binding_id,
                "provider": binding.provider,
                "model": binding.model,
                "backend": binding.backend.value,
                "binding_role": binding.binding_role.value,
                "profile_id": binding.profile_id,
                "quota_pool_id": binding.quota_pool_id,
                "profile_enabled": (
                    bool(profile.enabled) if profile is not None else False
                ),
                "reasoning_effort_capabilities": sorted(
                    e.value for e in binding.reasoning_effort_capabilities
                ),
                "pool_id": None if pool is None else pool.pool_id,
            },
        }
