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

from saberops.accept import AcceptEngine, AcceptResult
from saberops.config import RunConfig, resolve_run_config
from saberops.control_plane.binding_store import (
    BindingStoreError,
    load_owner_bindings,
    save_owner_bindings,
)
from saberops.control_plane.reviewer_binding import (
    ReviewerSelectionError,
    ReviewerSelectionStatus,
    reviewer_candidate_for_dispatch,
    select_reviewer_binding,
)
from saberops.db import Database
from saberops.dispatch import UnavailableReadinessService
from saberops.gate_runner import (
    GateScratchMode,
    GateScratchPolicyError,
    load_project_gate_scratch_policy,
)
from saberops.jobs.models import JobRole, JobState, coerce_job_role
from saberops.model_access import ReadinessService
from saberops.models import (
    OrchEvent,
    QuotaState,
    ReviewResult,
    Run,
    RunResult,
    Tier,
    WorkerCandidate,
)
from saberops.orchestrator import Orchestrator
from saberops.project import owner_state_dir
from saberops.review import ReviewEngine
from saberops.routing_config import load_effective_routing
from saberops.workers.registry import AdapterRegistry

if TYPE_CHECKING:
    from saberops.candidate_lifecycle import CandidateProjection
    from saberops.supervisor import RunSupervisor


class OwnerSelectionError(ValueError):
    """Fail-closed refusal of an owner binding selection (reviewer/orchestrator)."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


class RunOperationError(ValueError):
    """Fail-closed refusal of an owner run operation (resume/retry/continue)."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


#: The two ways to resume one failed job.
RESUME_FROM_SCRATCH = "scratch"
RESUME_FROM_LAST_CANDIDATE = "last_candidate"
RESUME_FROM_VALUES = (RESUME_FROM_SCRATCH, RESUME_FROM_LAST_CANDIDATE)


def coerce_resume_from(value: str | None) -> str:
    """Normalise the resume choice; anything else refuses with the two values."""
    if value is None:
        return RESUME_FROM_SCRATCH
    cleaned = str(value).strip().lower().replace("-", "_")
    if cleaned not in RESUME_FROM_VALUES:
        raise RunOperationError(
            "RESUME_FROM_INVALID",
            f"from must be one of {list(RESUME_FROM_VALUES)}; got {value!r}",
        )
    return cleaned


class OrchestratorService:
    """Unified application service facade for Web UI and CLI."""

    def __init__(
        self,
        db: Database | None = None,
        registry: AdapterRegistry | None = None,
        worktree_base: Path | None = None,
        readiness_service: ReadinessService | bool | None = None,
    ) -> None:
        self.db = db or Database()
        self.registry = registry or AdapterRegistry.default()
        self.worktree_base = worktree_base
        # The composition layer owns readiness acquisition.
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
        gate_scratch_mode: GateScratchMode | str | None = None,
    ) -> RunResult:
        """Execute a run synchronously (used by CLI orch run).

        When the caller does not supply ``gate_scratch_mode`` explicitly, the
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

    def review_run(
        self,
        run_id: str,
        reviewer_override: WorkerCandidate | None = None,
        timeout: float = 1200.0,
        review_limit: int | None = None,
    ) -> ReviewResult:
        """Execute independent code review for a completed run.

        When the caller supplies no explicit override, the
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
        """Guardedly retire this run's clean attempt worktrees.

        Delegates to the single guarded storage-lifecycle implementation so the
        compatibility route and ``orch gc`` enforce identical protections and no
        repository-wide prune.
        """
        return self.orchestrator.cleanup_run(run_id=run_id)

    def storage_maintenance(
        self, *, repo_root: Path | str, policy_path: Path | None = None
    ) -> Any:
        """Return the bounded storage-maintenance service for ``repo_root``.

        The single service-level seam the owner lifecycle and the scheduler use
        to obtain storage maintenance; the Web never constructs it directly.
        """
        from saberops.storage_cleanup import StorageMaintenance

        return StorageMaintenance(self.db, repo_root=repo_root, policy_path=policy_path)

    def list_models(self) -> dict[str, Any]:
        """Return the role routing chains and quota states."""
        try:
            from saberops.capacity.snapshot import effective_quota_states

            quota_states = effective_quota_states(self.db)
        except Exception:
            quota_states = self.db.list_quota()
        snap = load_effective_routing()
        return {
            "quota_states": quota_states,
            "role_chains": {
                "BUILD": snap.chain_for_role(JobRole.BUILD),
                "strong_start": snap.chain_for_role(JobRole.BUILD, strong_start=True),
            },
            "routing_snapshot": snap,
        }

    def run_routing_role(self, run_id: str) -> str:
        """Return the routing role of the newest ``tier_candidates_resolved`` event.

        A run with no such role (an older run) reports
        ``"LEGACY:<tier>"``. A retired role name stored before ADR-0005 §3
        resolves to ``BUILD``. Read-only; no DB schema change.
        """
        run = self.db.get_run(run_id)
        if run is None:
            raise RunOperationError("RUN_NOT_FOUND", f"Run not found: {run_id}")
        newest_role: str | None = None
        for event in self.db.get_events_by_type(run_id, "tier_candidates_resolved"):
            payload = event.payload
            if isinstance(payload, dict):
                role = payload.get("role")
                if isinstance(role, str) and role.strip():
                    newest_role = role.strip().upper()
        if newest_role is not None:
            return coerce_job_role(newest_role).value
        return f"LEGACY:{run.tier.value}"

    def run_strong_start(self, run_id: str) -> bool:
        """True when the newest routing chain used the build route's strong start.

        Read-only over the same durable ``tier_candidates_resolved`` evidence;
        a legacy stored role counts as a strong start (migration alias).
        """
        strong = False
        for event in self.db.get_events_by_type(run_id, "tier_candidates_resolved"):
            payload = event.payload
            if not isinstance(payload, dict):
                continue
            # A retired role in old evidence is the predecessor of strong_start.
            if str(payload.get("role", "")).strip().upper() == "SURGEON":
                strong = True
            elif "strong_start" in payload:
                strong = bool(payload.get("strong_start"))
        return strong

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
        """Re-enable normal routing state for a provider pool.

        Also clears active exclusions for that provider's pools
        (``cleared_by`` OWNER): ``Service.enable_quota`` is the function
        behind ``orch quota enable``.
        """
        state = self.db.enable_quota(provider=provider, pool=pool)
        try:
            from saberops.capacity.store import clear_pool_exclusions

            provider_norm = provider.strip().lower()
            try:
                from saberops.control_plane.plans import effective_quota_pools
                from saberops.control_plane.policy import load_control_plane_policy

                policies = effective_quota_pools(load_control_plane_policy())
            except Exception:
                policies = {}
            cleared_any = False
            for pool_id, policy in policies.items():
                members = getattr(policy, "members", ()) or ()
                hit = False
                for member in members:
                    sel = member.get("route") if isinstance(member, dict) else member
                    if isinstance(sel, str) and (
                        sel.strip().lower() == provider_norm
                        or sel.strip().lower().startswith(provider_norm + "/")
                    ):
                        hit = True
                        break
                if hit:
                    try:
                        clear_pool_exclusions(self.db, pool_id, by="OWNER")
                        cleared_any = True
                    except Exception:
                        pass
            if not cleared_any:
                try:
                    clear_pool_exclusions(self.db, provider_norm, by="OWNER")
                except Exception:
                    pass
                try:
                    clear_pool_exclusions(self.db, pool.strip().lower(), by="OWNER")
                except Exception:
                    pass
        except Exception:
            pass
        return state

    # ------------------------------------------------------------------
    # Owner binding selection (reviewer).
    #
    # Single service-layer authority for the ``orch reviewer`` command,
    # mirroring the removed ``/reviewer/select`` route exactly:
    # the persisted choice is a binding id (never free-floating
    # provider/model text); only a role-qualified binding that exists
    # in the canonical registry and resolves through the real binding
    # resolver may be selected; nothing is persisted on refusal.
    # ------------------------------------------------------------------

    def _load_owner_for_selection(self, *, for_write: bool) -> Any:
        """Load the owner binding document for a selection operation.

        Reads degrade to an empty document (the read surface never
        fails); mutations fail closed on an unreadable store so a
        write can never be based on unread state.
        """
        from saberops.control_plane.binding_store import OwnerBindings

        try:
            return load_owner_bindings()
        except BindingStoreError as exc:
            if for_write:
                raise OwnerSelectionError(
                    "OWNER_STORE_UNREADABLE",
                    f"owner binding state unreadable; refusing selection: {exc}",
                ) from exc
            return OwnerBindings()

    def get_reviewer_status(self) -> dict[str, Any]:
        """Return the owner's reviewer selection and the eligible bindings."""
        from saberops.control_plane.bindings import BindingRole

        owner = self._load_owner_for_selection(for_write=False)
        selection = select_reviewer_binding(owner, adapter_registry=self.registry)
        eligible = [
            binding.to_dict()
            for binding in owner.registry.list_bindings()
            if binding.binding_role is BindingRole.CHECK
        ]
        return {
            "selected_binding_id": next(iter(owner.role_binding_ids("reviewer")), None),
            "selected_binding_ids": list(owner.role_binding_ids("reviewer")),
            "status": selection.status.value,
            "reason": selection.reason,
            "resolved_binding": (
                None if selection.binding is None else selection.binding.to_dict()
            ),
            "eligible": eligible,
        }

    def set_reviewer_binding(self, binding_id: str) -> dict[str, Any]:
        """Persist the owner's exact reviewer binding choice.

        Only a ``CHECK``-role binding that exists in the canonical
        registry and resolves through the real binding resolver with
        the real adapter may be selected.  A ``BUILD``/``PLAN``
        binding, an unknown id, or an unresolvable binding is refused;
        nothing is persisted on refusal.
        """
        from dataclasses import replace

        owner = self._load_owner_for_selection(for_write=True)
        wanted = binding_id.strip()
        candidate_owner = replace(owner, reviewer_binding_ids=(wanted,) if wanted else ())
        selection = select_reviewer_binding(candidate_owner, adapter_registry=self.registry)
        if selection.status is not ReviewerSelectionStatus.RESOLVED or selection.binding is None:
            if not wanted or selection.reason == "REVIEWER_BINDING_NOT_FOUND":
                raise OwnerSelectionError(
                    "UNKNOWN_BINDING",
                    f"No such canonical binding: {wanted or '(empty)'}",
                )
            if selection.reason == "REVIEWER_WRONG_ROLE":
                role = (
                    selection.binding.binding_role.value
                    if selection.binding is not None
                    else "?"
                )
                raise OwnerSelectionError(
                    "WRONG_ROLE",
                    "Binding is not qualified for the Reviewer slot: "
                    f"role is {role!r}",
                )
            raise OwnerSelectionError(
                "REVIEWER_UNRESOLVABLE",
                "Reviewer selection refused: exact binding does not "
                "resolve for this adapter",
            )
        new_owner = owner.with_role_order("reviewer", (wanted,))
        save_owner_bindings(new_owner)
        return {
            "selected_binding_id": wanted,
            "provider": selection.binding.provider,
            "model": selection.binding.model,
        }

    def clear_reviewer_binding(self) -> dict[str, Any]:
        """Remove the owner's reviewer selection (legacy ladder applies)."""
        owner = self._load_owner_for_selection(for_write=True)
        save_owner_bindings(owner.with_role_order("reviewer", ()))
        return {"cleared": True}

    # ------------------------------------------------------------------
    # Owner run operations (retry / continue / try-worker).
    #
    # Single service-layer authority for the ``orch retry``,
    # ``orch continue`` and ``orch try-worker`` commands, mirroring the
    # removed ``/runs/{id}/retry``, ``/continue`` and ``/try-worker``
    # routes exactly: the retry/continuation target is the repository
    # the run was created for (validated against its persisted project
    # identity, never a current selection); frozen validation and
    # acceptance policy are kept; try-worker admits only the exact
    # blocked UNKNOWN worker still present in the routing chain.
    # Launch goes through the durable supervisor, as for ``orch run``.
    # ------------------------------------------------------------------

    def retry_run(self, run_id: str) -> dict[str, Any]:
        """Start a fresh attempt of a run's frozen task and policy."""
        import json
        from dataclasses import replace

        from saberops.config import reconstruct_run_config
        from saberops.provenance import assert_run_repository

        run = self.db.get_run(run_id)
        if run is None:
            raise RunOperationError("RUN_NOT_FOUND", f"Run not found: {run_id}")
        try:
            target = assert_run_repository(run)
            if run.config_json:
                persisted = json.loads(run.config_json)
                config = replace(
                    reconstruct_run_config(persisted), target_repo=str(target)
                )
            else:
                config = resolve_run_config(
                    task=run.task,
                    target_repo=target,
                    routing_mode="manual",
                    manual_tier=run.tier,
                    training_allowed=run.training_allowed,
                    gate_command=run.gate_command,
                )
            started = self._durable_supervisor().start_supervised_run(config=config)
        except RunOperationError:
            raise
        except Exception as exc:
            raise RunOperationError(
                "RETRY_NOT_STARTED", f"Retry failed to start: {exc}"
            ) from exc
        return {
            "operation": "retry",
            "source_run_id": run_id,
            "run_id": started.run_id,
        }

    def refresh_run(self, run_id: str) -> dict[str, Any]:
        """Refresh a stale candidate without moving its target."""
        from saberops.refresh import refresh_candidate

        try:
            return refresh_candidate(self, run_id)
        except (ValueError, RuntimeError) as exc:
            raise RunOperationError("REFRESH_REFUSED", str(exc)) from exc

    def continue_run(self, run_id: str) -> dict[str, Any]:
        """Continue a failed run from its immutable saved candidate.

        When the run already has a continuation, its id is returned
        without launching anything (idempotent, as the removed route).
        """
        from saberops.resume import ContinuationError, prepare_continuation

        run = self.db.get_run(run_id)
        if run is None:
            raise RunOperationError("RUN_NOT_FOUND", f"Run not found: {run_id}")
        existing = self.db.get_continuation(run_id)
        if existing:
            return {
                "operation": "continue",
                "source_run_id": run_id,
                "run_id": existing,
                "already_continued": True,
            }
        try:
            config = prepare_continuation(self.db, run_id)
            started = self._durable_supervisor().start_supervised_run(config=config)
        except (ContinuationError, RuntimeError) as exc:
            raise RunOperationError(
                "CONTINUE_REFUSED", f"Cannot continue: {exc}"
            ) from exc
        return {
            "operation": "continue",
            "source_run_id": run_id,
            "run_id": started.run_id,
            "already_continued": False,
        }

    def try_worker(self, run_id: str, binding_id: str) -> dict[str, Any]:
        """Explicitly retry a failed run with one blocked UNKNOWN worker.

        The binding must name the exact skipped UNKNOWN candidate from
        the failed run's termination evidence and still be present in
        the current routing chain with the same provider/model.  The
        failed run's frozen validation and acceptance policy are kept.
        """
        import json
        from dataclasses import replace

        from saberops.config import reconstruct_run_config
        from saberops.models import RunStatus
        from saberops.observability.run_report import build_run_report
        from saberops.provenance import assert_run_repository
        from saberops.routing import resolve_candidate_chain
        from saberops.routing_config import load_effective_routing

        run = self.db.get_run(run_id)
        if run is None:
            raise RunOperationError("RUN_NOT_FOUND", f"Run not found: {run_id}")
        if run.status != RunStatus.FAILED:
            raise RunOperationError(
                "RUN_NOT_FAILED", "Only a failed run can try a blocked worker."
            )
        wanted = binding_id.strip()
        report = build_run_report(self.db, run_id)
        termination = report.get("termination", {})
        blocked = (
            termination.get("skipped_candidates", [])
            if isinstance(termination, dict)
            else []
        )
        skipped = next(
            (
                row
                for row in blocked
                if isinstance(row, dict)
                and row.get("binding_id") == wanted
                and row.get("readiness_state") == "UNKNOWN"
            ),
            None,
        )
        try:
            snapshot = load_effective_routing()
            role = self.run_routing_role(run_id)
            strong_start = self.run_strong_start(run_id)
            if role.startswith("LEGACY:"):
                # A run carrying legacy tier labels: they all resolve to
                # the build route; T3 asks for its stronger-model setting.
                from saberops.models import Tier as _Tier

                tier = _Tier(role.split(":", 1)[1])
                candidates = resolve_candidate_chain(
                    tier=tier, snapshot=snapshot, strong_start=tier == _Tier.T3
                )
            else:
                candidates = resolve_candidate_chain(
                    role=role,
                    snapshot=snapshot,
                    strong_start=strong_start,
                )
        except ValueError as exc:
            raise RunOperationError(
                "ROUTING_UNAVAILABLE",
                f"Worker routing is unavailable. Check Routing: {exc}",
            ) from exc
        candidate = next(
            (entry for entry in candidates if entry.binding_id == wanted), None
        )
        if (
            skipped is None
            or candidate is None
            or not wanted
            or (
                candidate.provider != skipped.get("provider")
                or candidate.model != skipped.get("model")
            )
        ):
            raise RunOperationError(
                "WORKER_UNAVAILABLE",
                "This exact worker is no longer in the chosen routing list.",
            )
        try:
            target = assert_run_repository(run)
            if not run.config_json:
                raise ValueError("Run policy is unavailable")
            config = replace(
                reconstruct_run_config(json.loads(run.config_json)),
                target_repo=str(target),
                provider_override=candidate.provider,
                model_override=candidate.model,
                worker_binding_registry=None,
            )
            started = self._durable_supervisor().start_supervised_run(config=config)
            self.db.record_event(
                started.run_id,
                "owner_worker_bootstrap_requested",
                payload={
                    "source_run_id": run_id,
                    "binding_id": wanted,
                    "provider": candidate.provider,
                    "model": candidate.model,
                },
            )
        except RunOperationError:
            raise
        except Exception as exc:
            raise RunOperationError(
                "WORKER_NOT_STARTED",
                f"Could not start this worker. Check Routing and project policy: {exc}",
            ) from exc
        return {
            "operation": "try-worker",
            "source_run_id": run_id,
            "run_id": started.run_id,
            "binding_id": wanted,
            "provider": candidate.provider,
            "model": candidate.model,
        }

    def resume_job(
        self,
        target_id: str,
        *,
        from_: str = RESUME_FROM_SCRATCH,
        note: str | None = None,
        worker_binding: str | None = None,
    ) -> dict[str, Any]:
        """Resume one failed job: from scratch or from its saved candidate.

        The one owner/planner recovery verb. It maps onto the existing
        retry and continue paths, links the successor run back to the job so
        the job's state follows it, and refuses with a reason when neither
        path applies.  ``worker_binding`` is the folded-in try-worker option
        (one exact blocked UNKNOWN worker, fresh attempt only).

        A repeat for the same job never starts a second successor: once the
        job is RUNNING it returns the current successor instead.
        """
        from saberops.jobs.service import get_job as read_job
        from saberops.jobs.service import note_job as record_note
        from saberops.jobs.store import (
            JobConflictError,
            JobTransitionError,
            find_job_for_run,
            resume_transition,
        )

        target = (target_id or "").strip()
        if not target:
            raise RunOperationError("RESUME_TARGET_REQUIRED", "A job or run id is required.")
        chosen = coerce_resume_from(from_)
        binding = (worker_binding or "").strip() or None
        if binding is not None and chosen != RESUME_FROM_SCRATCH:
            raise RunOperationError(
                "WORKER_BINDING_NEEDS_SCRATCH",
                "An exact worker binding applies only to a fresh attempt (from scratch).",
            )

        # Starting a resumed attempt is starting work.  A transient closed gate
        # (a fresh store that never had a session) keeps the existing owner
        # retry behaviour; an explicit or sticky pause is never bypassed from
        # chat: only the owner's `orch resume` clears it (ADR-0045).
        from saberops.runtime_gate import evaluate_gate

        gate = evaluate_gate(db=self.db)
        if gate.closed and not gate.transient:
            raise RunOperationError(
                "RESUME_PAUSED",
                f"Resume is paused: {gate.label}. Ask the owner to run `orch resume` "
                "first; no tool clears a pause.",
            )

        job = read_job(self.db, target)
        named_run = None
        if job is None:
            named_run = self.db.get_run(target)
            if named_run is None:
                raise RunOperationError(
                    "RUN_NOT_FOUND",
                    f"Run not found: {target} (and no job has that id).",
                )
            job = find_job_for_run(self.db, named_run.id)

        if job is None:
            # A run from a plain `orch run` has no job record; keep the run-level
            # retry/continue behaviour without inventing job state.
            assert named_run is not None
            if note:
                raise RunOperationError(
                    "NOTE_NEEDS_JOB",
                    "A resume note is stored on a job; this run has none. "
                    "Use `orch run` with the note in the task instead.",
                )
            if binding is not None:
                started = self.try_worker(named_run.id, binding)
            elif chosen == RESUME_FROM_LAST_CANDIDATE:
                started = self.continue_run(named_run.id)
            else:
                started = self.retry_run(named_run.id)
            already = bool(started.get("already_continued", False))
            return {
                "operation": "resume_job",
                "job_id": None,
                "from": chosen,
                "source_run_id": named_run.id,
                "run_id": started["run_id"],
                "already_resumed": already,
                # Kept for the one-release `orch continue` alias.
                "already_continued": already,
            }

        job_id = job.job_id
        if job.state is JobState.RUNNING:
            previous = (
                named_run.id
                if named_run is not None
                else (job.run_ids[-2] if len(job.run_ids) > 1 else None)
            )
            if not note:
                return self._resumed_payload(
                    job, chosen, job.current_run_id, True, source_run_id=previous
                )
            try:
                written = record_note(self.db, job_id, note)
            except ValueError as exc:
                raise RunOperationError("RESUME_NOTE_INVALID", str(exc)) from exc
            return self._resumed_payload(
                job,
                chosen,
                job.current_run_id,
                True,
                source_run_id=previous,
                note_id=written.note_id,
            )
        if job.state is not JobState.FAILED:
            raise RunOperationError(
                "JOB_NOT_RESUMABLE",
                f"job {job_id} is {job.state.value}; only a failed job can be resumed. "
                "Wait for it, cancel it, or submit a new job.",
            )

        if (
            named_run is not None
            and job.current_run_id
            and named_run.id != job.current_run_id
        ):
            raise RunOperationError(
                "RESUME_NOT_CURRENT_RUN",
                f"run {named_run.id} is not the job's current run "
                f"({job.current_run_id}); resume the current one.",
            )
        source_run_id = (
            named_run.id
            if named_run is not None
            else job.current_run_id or (job.run_ids[-1] if job.run_ids else None)
        )
        if not source_run_id:
            raise RunOperationError(
                "JOB_HAS_NO_RUN",
                f"job {job_id} never dispatched a run, so there is nothing to resume; "
                "cancel it and submit again.",
            )

        note_id: int | None = None
        if note:
            try:
                note_id = record_note(
                    self.db, job_id, note, allow_terminal=True
                ).note_id
            except ValueError as exc:
                raise RunOperationError("RESUME_NOTE_INVALID", str(exc)) from exc

        if binding is not None:
            started = self.try_worker(source_run_id, binding)
        elif chosen == RESUME_FROM_LAST_CANDIDATE:
            started = self.continue_run(source_run_id)
        else:
            started = self.retry_run(source_run_id)
        successor = str(started["run_id"])
        try:
            updated, linked = resume_transition(
                self.db,
                job_id,
                successor,
                source_run_id=source_run_id,
                from_=chosen,
                note_id=note_id,
            )
        except (JobConflictError, JobTransitionError) as exc:
            raise RunOperationError(
                "RESUME_NOT_LINKED",
                f"successor run {successor} started but the job could not follow it: {exc}",
            ) from exc
        return self._resumed_payload(
            updated,
            chosen,
            successor,
            bool(started.get("already_continued", False)) or not linked,
            source_run_id=source_run_id,
            note_id=note_id,
            worker_binding=binding,
            provider=started.get("provider"),
            model=started.get("model"),
        )

    @staticmethod
    def _resumed_payload(
        job: Any,
        chosen: str,
        successor: str | None,
        already_resumed: bool,
        *,
        source_run_id: str | None,
        note_id: int | None = None,
        worker_binding: str | None = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "operation": "resume_job",
            "job_id": job.job_id,
            "from": chosen,
            "source_run_id": source_run_id,
            "run_id": successor,
            "already_resumed": already_resumed,
            "state": job.state.value,
        }
        if note_id is not None:
            payload["note_id"] = note_id
        if worker_binding is not None:
            payload["worker_binding"] = worker_binding
            payload["binding_id"] = worker_binding
        if provider is not None:
            payload["provider"] = provider
        if model is not None:
            payload["model"] = model
        return payload
