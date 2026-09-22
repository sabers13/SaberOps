"""Core orchestration engine managing multi-provider worktree dispatch.

Handles gate verification, retry evidence forwarding, and error bounds.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import replace as _frozen_replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar, Protocol

from saberops.authority import load_authority_settings
from saberops.autonomous_runtime import (
    AutonomousClosureResult,
    AutonomousProjectResult,
    AutonomousRuntimeContract,
    AutonomousSupervisor,
    SupervisorAction,
    SupervisorBudgets,
    SupervisorStopReason,
    close_accepted_run,
)
from saberops.config import (
    RunConfig,
    freeze_owner_binding_registry_payload,
    resolve_run_config,
)
from saberops.context_projection import (
    CHANGED_PATHS_MAX,
    EVIDENCE_BLOCK_MAX_CHARS,
    STDERR_BLOCK_MAX_CHARS,
    STDOUT_BLOCK_MAX_CHARS,
    ContextProjection,
    RenderResult,
    build_retry_projection,
)
from saberops.context_projection import (
    objective_digest as objective_digest_inline,
)
from saberops.context_projection import (
    objective_slug as objective_slug_inline,
)
from saberops.control_plane import (
    NO_ELIGIBLE_CANDIDATE,
    FrozenControlPlane,
    freeze_control_plane_policy,
    legacy_frozen_control_plane,
    load_control_plane_policy,
    reconstruct_frozen_control_plane,
)
from saberops.control_plane.binding_store import load_owner_bindings
from saberops.control_plane.bindings import (
    BindingKind,
    BindingResolutionError,
    ResolvedExecutionBinding,
    binding_registry_from_mapping,
    resolve_execution_binding,
    resolve_worker_candidate_binding,
)
from saberops.control_plane.effort import (
    DEFAULT_WORKER_EFFORT_POLICY,
    EffortPolicy,
)
from saberops.control_plane.orch_binding import OrchBindingPolicy
from saberops.db import Database, current_iso_timestamp
from saberops.dispatch import build_skip_payload, evaluate_eligibility
from saberops.dispatch import control_plane_decision as control_plane_decision
from saberops.execution_failover import FailoverEngine
from saberops.gateway import (
    AffinityHint,
    ExecutionDisposition,
    FrozenGatewayConfig,
    GatewayExecutionOutcome,
    GatewayLiveState,
    GatewayLiveStateProvider,
    GatewayTransport,
    reconstruct_frozen_gateway_config,
    run_worker_with_gateway_boundary,
    serialize_dispatch_evidence,
)
from saberops.git import (
    CandidatePublication,
    CandidatePublicationError,
    GitManager,
    assert_repository_at_revision,
)
from saberops.models import (
    Attempt,
    AttemptStatus,
    CheckResult,
    DispatchOutcome,
    DispatchReason,
    DispatchRole,
    DispatchStage,
    FailureKind,
    GateDisposition,
    GateOutcome,
    HealthState,
    ManagerState,
    PhaseKind,
    PlanStatus,
    PlanStepStatus,
    ProviderReadiness,
    ReasoningEffort,
    ReviewResult,
    ReviewVerdict,
    Run,
    RunResult,
    RunStatus,
    SideEffectOperation,
    SideEffectState,
    SupervisionRecord,
    Tier,
    WorkerActivation,
    WorkerActivationState,
    WorkerCandidate,
    WorkerCheckpoint,
    WorkerInterruptionReason,
    WorkerRequest,
    WorkerResult,
    role_for_stage,
)
from saberops.observability import new_dispatch_evidence, update_dispatch_from_result
from saberops.observability.phase_telemetry import (
    mark_phase_not_applicable,
    mark_phase_unknown,
    measure_phase,
)
from saberops.ownership import (
    Liveness,
    OwnershipError,
    ProcessIdentity,
    probe_liveness,
)
from saberops.planning import (
    build_context_capsule,
    mark_step,
    persist_plan,
    ready_dependents,
)
from saberops.process import (
    AutonomousDispatchConfig,
    AutonomousEvidenceError,
    ProcessResult,
    SandboxedProcessError,
    is_evidence_uncertain_error,
    run_autonomous_adapter,
    run_process,
    split_command,
)
from saberops.project_scope.graph import build_project_graph
from saberops.project_scope.persistence import (
    freeze_run_scope,
    load_scope_bundle,
    scope_section_for_run,
)
from saberops.project_scope.resolver import resolve_package_scope, resolve_scope
from saberops.provenance import (
    ProvenanceError,
    assert_candidate_provenance,
    assert_run_repository,
    failover_authorized_bases,
    mint_run_provenance,
)

# Kept as a compatibility module attribute for integrations that patch the
# pre-C07 chain helper; C07 dispatch itself evaluates quota at preflight.
from saberops.quota import apply_quota_to_chain  # noqa: F401
from saberops.routing import resolve_candidate_chain
from saberops.routing_config import (
    RoutingConfigError,
    load_effective_routing,
    load_packaged_default,
    payload_to_snapshot,
    snapshot_references_exact_bindings,
    snapshot_to_payload,
)
from saberops.routing_decision import (
    TierDecision,
    choose_tier,
    classify_worker_outcome,
    escalated_decision,
    is_escalation_evidence,
)
from saberops.slicing import resolve_work_packages
from saberops.telemetry import persist_worker_usage
from saberops.terminal import get_transcript_path
from saberops.tooling import (
    FrozenToolContract,
    ToolRegistry,
    freeze_tool_contract,
    tool_context_metrics,
)
from saberops.tooling.invocation import read_telemetry
from saberops.validation_policy import (
    VALIDATION_POLICY_MARKER,
    VALIDATION_POLICY_TEXT,
)
from saberops.workers.base import WorkerAdapter
from saberops.workers.registry import AdapterRegistry

# Minimum interval between coalesced normalized activity touches (supervision
# row update + worker_activity event).  Raw transcript output is never
# rate-limited; only this control-plane projection is bounded.
WORKER_ACTIVITY_TOUCH_MIN_INTERVAL_SECONDS: float = 5.0

# Secret-bearing env vars that must never be copied into WorkerRequest
_WORKER_ENV_SECRET_KEYS = {"OMNIROUTE_API_KEY"}

# Reserved gateway-control variables that must be stripped from the parent
# environment before building every WorkerRequest. The DIRECT path must be
# immune to stale/malicious values; only the C10 boundary may explicitly
# add approved values for a genuine gateway dispatch.
_RESERVED_GATEWAY_ENV_VARS = frozenset(
    {
        "ORCH_GATEWAY_ENDPOINT",
        "ORCH_GATEWAY_CONNECTION",
        "ORCH_GATEWAY_AUTHORIZATION_ENV",
        "ORCH_GATEWAY_FROZEN_DIGEST",
        "ORCH_GATEWAY_SELECTION_REASON",
        "ORCH_GATEWAY_PROVIDER_ID",
        "ORCH_GATEWAY_LOOPBACK_URL",
        "_ORCH_BRIDGE_AUTH_SENTINEL",
    }
)


class _ReadinessAdmission(Protocol):
    """Neutral readiness input for one exact resolved connection.

    Structural (not imported) so workflow never depends on model_access:
    the composition layer injects the real
    :class:`saberops.model_access.ReadinessService`, which
    satisfies this shape.  Only neutral state/reason flows into common
    dispatch admission; readiness never synthesizes or rewrites a
    binding.
    """

    @property
    def state(self) -> ProviderReadiness: ...

    @property
    def reason(self) -> str: ...

    @property
    def executable_available(self) -> bool: ...

    @property
    def connection_id(self) -> str | None: ...


class _ReadinessProvider(Protocol):
    """Composition-supplied readiness acquisition/projection seam."""

    def admission_for(
        self, *, provider: str, profile_id: str = "", backend_ref: str = ""
    ) -> _ReadinessAdmission: ...

    def record_success(self, connection_id: str) -> None: ...


def _filtered_worker_env(
    base: dict[str, str],
    *,
    authorization_env_var: str | None = None,
    strip_gateway_vars: bool = True,
) -> dict[str, str]:
    """Return a copy of ``base`` with secret-bearing keys removed.

    The real OmniRoute token is resolved by the loopback bridge at
    forwarding time from the host environment, never from the worker's
    persisted request. This helper ensures the token never enters
    ``WorkerRequest.env`` or any durable evidence.

    ``authorization_env_var`` is the configured non-secret env-var *name*
    for this run (e.g. ``OMNIROUTE_API_KEY`` or a custom ``TEST_CUSTOM_…``).
    The helper dynamically scrubs that exact name, not only the hardcoded
    default.
    """

    # Dynamically scrub the configured authorization env-var for this run
    secret_keys = set(_WORKER_ENV_SECRET_KEYS)
    if authorization_env_var:
        secret_keys.add(authorization_env_var.strip())
    filtered = {k: v for k, v in base.items() if k not in secret_keys}
    if strip_gateway_vars:
        filtered = {k: v for k, v in filtered.items() if k not in _RESERVED_GATEWAY_ENV_VARS}
    return filtered


def _gateway_authorization_env_for(frozen_gateway: FrozenGatewayConfig | None) -> str | None:
    if frozen_gateway is None or not frozen_gateway.enabled:
        return None
    return getattr(frozen_gateway, "authorization_env_var", None) or "OMNIROUTE_API_KEY"


def _telemetry_total(records: list[dict[str, object]], key: str) -> int:
    total = 0
    for record in records:
        value = record.get(key)
        if isinstance(value, int):
            total += value
    return total


def build_retry_task_prompt(
    original_task: str,
    attempt_idx: int,
    prior_attempt: Attempt,
    prior_check: CheckResult | None,
    inherited_base_sha: str | None = None,
    tool_contract: FrozenToolContract | None = None,
) -> str:
    """Build a concise, evidence-rich retry prompt for a subsequent attempt.

    C04 projection: the raw full ``original_task`` is intentionally NOT
    prepended.  The retry worker receives the bounded objective digest, the
    inherited candidate SHA, and the exact failure evidence; the original
    epic task remains durably persisted on the run/package for audit.
    """
    from saberops.context_projection import (
        build_retry_projection,
        render_projection,
    )

    projection = build_retry_projection(
        original_task=original_task,
        attempt_idx=attempt_idx,
        prior_attempt=prior_attempt,
        prior_check=prior_check,
        inherited_base_sha=inherited_base_sha,
        role="REPAIR",
    )
    rendered = render_projection(
        projection,
        validation_policy_text=VALIDATION_POLICY_TEXT,
        validation_policy_marker=VALIDATION_POLICY_MARKER,
        tool_contract=tool_contract,
    )
    return rendered.prompt


def _render_repair_task(
    original_task: str,
    attempt_idx: int,
    from_sha: str,
    review: ReviewResult,
    package: object | None = None,
    tool_contract: FrozenToolContract | None = None,
) -> RenderResult:
    """Build a concise, evidence-rich repair prompt for an implementation worker.

    C04 projection: the raw full ``original_task`` is intentionally NOT
    prepended.  The repair worker receives the bounded objective digest,
    the inherited candidate SHA, and bounded review evidence.

    C04-R1: when ``package`` is provided the prompt is emitted as a single
    PACKAGE-mode projection; when ``package`` is None it is emitted as the
    ordinary REPAIR projection.  No concatenation of an INITIAL/REPAIR
    contract with a PACKAGE contract.
    """
    from saberops.context_projection import (
        build_package_retry_projection,
        build_repair_projection,
        prune_evidence,
        render_projection,
    )

    if package is not None:
        summary = getattr(review, "summary", "") or ""
        details = getattr(review, "details", "") or ""
        bounded_summary = prune_evidence(summary, limit=2048)
        bounded_details = prune_evidence(details, limit=2048)
        review_excerpt_parts: list[str] = []
        if bounded_summary.text:
            review_excerpt_parts.append(f"Summary: {bounded_summary.text}")
        if bounded_details.text:
            review_excerpt_parts.append(f"Details:\n{bounded_details.text}")
        review_excerpt = "\n".join(review_excerpt_parts) or None
        evidence_truncated = bounded_summary.truncated or bounded_details.truncated
        projection = build_package_retry_projection(
            original_task=original_task,
            package=package,
            inherited_base_sha=from_sha,
            prior_check=None,
            review_excerpt=review_excerpt,
            evidence_truncated=evidence_truncated,
            role="REPAIR",
        )
    else:
        projection = build_repair_projection(
            original_task=original_task,
            attempt_idx=attempt_idx,
            from_sha=from_sha,
            review=review,
            role="REPAIR",
        )
    rendered = render_projection(
        projection,
        validation_policy_text=VALIDATION_POLICY_TEXT,
        validation_policy_marker=VALIDATION_POLICY_MARKER,
        tool_contract=tool_contract,
    )
    return rendered


def build_repair_task_prompt(
    original_task: str,
    attempt_idx: int,
    from_sha: str,
    review: ReviewResult,
    package: object | None = None,
    tool_contract: FrozenToolContract | None = None,
) -> str:
    """Build a concise, evidence-rich repair prompt for an implementation worker."""
    return _render_repair_task(
        original_task=original_task,
        attempt_idx=attempt_idx,
        from_sha=from_sha,
        review=review,
        package=package,
        tool_contract=tool_contract,
    ).prompt


def _render_final_repair_task(
    original_task: str,
    attempt_idx: int,
    from_sha: str,
    reviews: list[ReviewResult],
    package: object | None = None,
    tool_contract: FrozenToolContract | None = None,
) -> RenderResult:
    """Build a repair prompt that aggregates ALL outstanding findings for final convergence.

    C04 projection: the raw full ``original_task`` is intentionally NOT
    prepended.  Outstanding review findings are deterministically bounded and
    deduplicated; already-resolved findings are NOT re-emitted.

    C04-R1: when ``package`` is provided the prompt is emitted as a single
    PACKAGE-mode projection; when ``package`` is None it is emitted as the
    ordinary REPAIR projection.
    """
    from saberops.context_projection import (
        build_final_repair_projection,
        build_package_retry_projection,
        prune_evidence,
        render_projection,
    )

    if package is not None:
        parts: list[str] = []
        seen_keys: set[str] = set()
        evidence_truncated_any = False
        for idx, rev in enumerate(reviews, start=1):
            provider = getattr(rev, "provider", "?")
            model = getattr(rev, "model", "?")
            summary = (getattr(rev, "summary", "") or "").strip()
            details = (getattr(rev, "details", "") or "").strip()
            key = f"{summary.lower()}|{details.lower()}"
            if key in seen_keys:
                continue
            seen_keys.add(key)
            bs = prune_evidence(summary, limit=2048)
            bd = prune_evidence(details, limit=2048)
            evidence_truncated_any = evidence_truncated_any or bs.truncated or bd.truncated
            parts.append(f"--- Finding #{idx} ({provider}/{model}) ---\nSummary: {bs.text}")
            if details:
                parts.append(f"Details:\n{bd.text}")
        review_excerpt = "\n".join(parts) if parts else None
        projection = build_package_retry_projection(
            original_task=original_task,
            package=package,
            inherited_base_sha=from_sha,
            prior_check=None,
            review_excerpt=review_excerpt,
            evidence_truncated=evidence_truncated_any,
            role="REPAIR",
        )
    else:
        projection = build_final_repair_projection(
            original_task=original_task,
            attempt_idx=attempt_idx,
            from_sha=from_sha,
            reviews=reviews,
            role="REPAIR",
        )
    rendered = render_projection(
        projection,
        validation_policy_text=VALIDATION_POLICY_TEXT,
        validation_policy_marker=VALIDATION_POLICY_MARKER,
        tool_contract=tool_contract,
    )
    return rendered


def build_final_repair_task_prompt(
    original_task: str,
    attempt_idx: int,
    from_sha: str,
    reviews: list[ReviewResult],
    package: object | None = None,
    tool_contract: FrozenToolContract | None = None,
) -> str:
    """Build a bounded final-convergence repair prompt."""
    return _render_final_repair_task(
        original_task=original_task,
        attempt_idx=attempt_idx,
        from_sha=from_sha,
        reviews=reviews,
        package=package,
        tool_contract=tool_contract,
    ).prompt


def _bounded_output(raw: str, limit: int = 4000) -> str:
    """Return bounded output preserving prefix and suffix if long."""
    if not raw or len(raw) <= limit * 2:
        return raw
    return raw[:limit] + "\n...[truncated]...\n" + raw[-limit:]


def _is_genuine_capability_escalation(outcome: DispatchOutcome | None) -> bool:
    """Return True only for genuine capability-bearing tier escalation evidence.

    C04 invariant: SURGEON context semantics may activate ONLY on a real
    validation failure on actual candidate changes (``GATE_FAILURE`` with
    real diff).  Provider / quota / infrastructure / timeout / termination /
    mutation / no_changes / already_satisfied / unknown / unknown_no_changes
    / eligibility_skip / worker_failure_or_refusal escalations never select
    SURGEON context semantics, regardless of how cleanly the worker ran.
    """
    return outcome is DispatchOutcome.GATE_FAILURE


def _role_for_orchestrator_dispatch(
    stage: DispatchStage | None,
    *,
    surgeon_active: bool,
    package: Any = None,
) -> DispatchRole:
    """Return the canonical role for one Orchestrator-level dispatch.

    C04 override: when ``surgeon_active`` is True (genuine capability-bearing
    tier escalation), the next dispatch is labelled SURGEON.  Otherwise the
    canonical :func:`role_for_stage` mapping is preserved unchanged for the
    legacy IMPLEMENTATION / RETRY / REPAIR / REVIEW stages.
    """
    if surgeon_active:
        return DispatchRole.SURGEON
    if stage is None:
        return DispatchRole.BULK
    return role_for_stage(stage)


class _InitialOrchestratorGuidanceCache:
    """Bounded first-dispatch Orchestrator guidance cache.

    C15-XEC: the bounded Orchestrator model participates in ONE
    semantic role in a normal run -- initial objective interpretation
    / execution guidance.  The cache is consulted exactly once, on
    the first dispatch of the run.  Subsequent calls return the
    cached result without re-invoking the consumer (deterministic
    bound: ``maximum Orchestrator semantic calls per normal run = 1``).
    """

    __slots__ = ("_consumed", "_reason", "_text")

    def __init__(self) -> None:
        self._consumed = False
        self._text: str | None = None
        self._reason: str | None = None

    def get_or_fetch(
        self,
        *,
        service: Any,
        original_task: str,
        run_id: str,
        correlation_id: str | None = None,
    ) -> tuple[str, str | None]:
        """Return ``(text, reason)`` for the bounded initial guidance.

        On the first call the cache invokes the bounded consumer via
        :func:`fetch_initial_orchestrator_guidance`.  On subsequent
        calls it returns the cached result so the consumer is invoked
        at most once per run.
        """
        if self._consumed:
            return self._text or "", self._reason
        from saberops.orchestrator_consumer import (
            fetch_initial_orchestrator_guidance,
        )

        text, reason = fetch_initial_orchestrator_guidance(
            service=service,
            original_task=original_task,
            run_id=run_id,
            correlation_id=correlation_id,
        )
        self._text = text
        self._reason = reason
        self._consumed = True
        return text, reason


def _classify_gate_outcome(
    gate_res: ProcessResult,
    *,
    mutated_worktree: bool = False,
    changed_paths: tuple[str, ...] = (),
    candidate_sha: str | None = None,
) -> GateDisposition:
    """Classify an authoritative Orch-owned gate outcome into the structured taxonomy.

    The classification is driven by structured fields on ``ProcessResult`` only:
    ``launch_failed``, ``timed_out``, ``termination_incomplete``,
    ``exit_code``.  Stderr text is NEVER used for classification; this keeps
    infrastructure / runtime / timeout / termination-unsafe outcomes
    distinguishable from real validation failures without fuzzy parsing.

    Precedence:
      1. launch_failed            -> INFRASTRUCTURE_FAILURE
      2. termination_incomplete   -> TERMINATION_UNSAFE
      3. timed_out                -> TIMEOUT
      4. mutated_worktree         -> MUTATION
      5. exit_code == 0           -> PASS
      6. otherwise                -> VALIDATION_FAILURE
    """
    if gate_res.launch_failed:
        outcome = GateOutcome.INFRASTRUCTURE_FAILURE
    elif gate_res.termination_incomplete:
        outcome = GateOutcome.TERMINATION_UNSAFE
    elif gate_res.timed_out:
        outcome = GateOutcome.TIMEOUT
    elif mutated_worktree:
        outcome = GateOutcome.MUTATION
    elif gate_res.exit_code == 0:
        outcome = GateOutcome.PASS
    else:
        outcome = GateOutcome.VALIDATION_FAILURE
    return GateDisposition(
        outcome=outcome,
        exit_code=gate_res.exit_code,
        duration_seconds=gate_res.duration_seconds,
        timed_out=gate_res.timed_out,
        termination_incomplete=gate_res.termination_incomplete,
        launch_failed=gate_res.launch_failed,
        mutated_worktree=mutated_worktree,
        changed_paths=changed_paths,
        candidate_sha=candidate_sha,
    )


def _gate_disposition_payload(disposition: GateDisposition) -> dict[str, object]:
    """Project a :class:`GateDisposition` into a durable event payload.

    The :class:`GateOutcome` is emitted under the ``gate_outcome`` key so that
    callers may merge this payload alongside other ``outcome`` fields (notably
    :class:`DispatchOutcome`) without colliding.  The worktree-mutation flag
    keeps its legacy ``gate_mutated_worktree`` key so existing evidence
    consumers (including C02-R1-A) keep working unchanged.
    """
    return {
        "gate_outcome": disposition.outcome.value,
        "exit_code": disposition.exit_code,
        "duration_seconds": disposition.duration_seconds,
        "timed_out": disposition.timed_out,
        "termination_incomplete": disposition.termination_incomplete,
        "launch_failed": disposition.launch_failed,
        "gate_mutated_worktree": disposition.mutated_worktree,
        "changed_paths": list(disposition.changed_paths),
        "candidate_sha": disposition.candidate_sha,
    }


def load_owner_runtime_bindings(
    binding_registry: Any | None,
    binding_policy: OrchBindingPolicy | None,
) -> tuple[Any | None, OrchBindingPolicy | None]:
    """Fill genuinely absent autonomous binding inputs from owner state.

    A caller-supplied registry/policy always wins (tests, recovery).  Owner
    state is consulted only when an input is absent; empty owner state leaves
    the fail-closed NO_ELIGIBLE_BINDING path untouched, and a corrupt owner
    document is treated as absent rather than as a launch authorisation.  The
    owner policy is adopted only alongside its own owner registry so a policy
    can never reference bindings the effective registry lacks.

    C15 separation note: this helper still couples ``binding_policy`` to the
    same call, but the policy it returns is the **Orchestrator-model**
    policy -- not a worker-binding selection policy.  Worker-binding
    selection lives in :class:`AutonomousSupervisor` and consults the
    canonical :class:`BindingRegistry` plus per-candidate ``binding_id``
    references; ``binding_policy`` does not enter that path.
    """
    if binding_registry is not None and binding_policy is not None:
        return binding_registry, binding_policy
    try:
        owner_bindings = load_owner_bindings()
    except Exception:  # noqa: BLE001 - corrupt owner state must not launch
        return binding_registry, binding_policy
    registry = binding_registry
    policy = binding_policy
    if registry is None and tuple(owner_bindings.registry.list_bindings()):
        registry = owner_bindings.registry
        if policy is None and owner_bindings.policy is not None:
            policy = owner_bindings.policy
    return registry, policy


def _adopt_owner_binding_registry(binding_registry: Any | None) -> Any | None:
    """Fill an absent autonomous binding registry from owner state.

    This is the C15-XB-02-aware counterpart to
    :func:`load_owner_runtime_bindings`: it adopts only the registry,
    never an :class:`OrchBindingPolicy`.  The orchestrator lifecycle
    consumes the registry for worker dispatch (C15-XB-01/03) and the
    ``binding_policy`` is separately persisted in the frozen runtime
    contract purely so a fresh restart can recover the owner's
    Orchestrator-model choice.  Worker dispatch is **not** governed
    by the ``binding_policy`` -- changing it MUST NOT change which
    binding a worker-routing candidate resolves to.

    A caller-supplied registry always wins (tests, recovery); empty
    owner state is left alone so the fail-closed NO_ELIGIBLE_BINDING
    path still fires.
    """
    if binding_registry is not None:
        return binding_registry
    try:
        owner_bindings = load_owner_bindings()
    except Exception:  # noqa: BLE001 - corrupt owner state must not launch
        return binding_registry
    if tuple(owner_bindings.registry.list_bindings()):
        return owner_bindings.registry
    return binding_registry


def _reconstruct_frozen_worker_binding_registry(payload: Any | None) -> Any | None:
    """Reconstruct the Run-frozen BindingRegistry from its durable payload.

    The payload is the canonical :meth:`BindingRegistry.to_dict` rendering
    persisted in the Run's ``config_json`` at admission (C15-XB-FREEZE-01).
    Ordinary exact-binding dispatch, repair, retry, adoption and cold
    recovery resolve exclusively from this value; the live owner binding
    store is never consulted for an already-admitted Run.

    Returns ``None`` when no frozen material was persisted (legacy/static
    Run) or when it cannot be reconstructed.  A binding-bearing candidate
    then fails closed with ``NO_REGISTRY`` -- never by loading ambient owner
    state, which could execute a materially different identity under the
    same frozen routing snapshot.
    """
    if not isinstance(payload, dict):
        return None
    try:
        return binding_registry_from_mapping(payload)
    except Exception:  # noqa: BLE001 - corrupt frozen material must not launch
        return None


class Orchestrator:
    """Orchestrates end-to-end task execution in isolated Git worktrees."""

    def __init__(
        self,
        db: Database | None = None,
        registry: AdapterRegistry | None = None,
        default_worker: WorkerAdapter | None = None,
        worktree_base: Path | None = None,
        tool_registry: ToolRegistry | None = None,
        gateway_transport: GatewayTransport | None = None,
        gateway_live_state_provider: GatewayLiveStateProvider | None = None,
        readiness_service: _ReadinessProvider | None = None,
    ) -> None:
        self.db = db or Database()
        if registry is not None:
            self.registry = registry
        elif default_worker is not None:
            self.registry = AdapterRegistry([default_worker])
        else:
            self.registry = AdapterRegistry.default()
        self.worktree_base = worktree_base
        self.tool_registry = tool_registry or ToolRegistry.from_environment()
        self.gateway_transport = gateway_transport
        self.gateway_live_state_provider = gateway_live_state_provider
        # C15-RDY-02A: ``None`` here is an explicit test/compatibility-only
        # disable of readiness admission (legacy hermetic unit scope).
        # Production construction never passes ``None``: the
        # ``OrchestratorService`` composition seam always injects a
        # readiness provider -- the owner-state service when the
        # authority is available, the fail-closed unavailable fallback
        # (automatic UNKNOWN skip) when it is not.  A missing/broken
        # authority must never resolve to this disabled state.
        self.readiness_service = readiness_service

    def _bound_adapter_run(
        self,
        adapter: WorkerAdapter,
        *,
        autonomous: AutonomousDispatchConfig | None,
        attempt_id: str,
        run_id: str,
        repo_root: str,
        provider: str,
        model: str,
    ) -> Callable[[WorkerRequest], WorkerResult]:
        """Return the adapter invocation for one dispatch.

        Legacy (``autonomous`` is None): the adapter's plain ``run``
        bound method -- behavior unchanged.  Autonomous: a closure
        that runs the registry-selected adapter through the proven
        generic enforcement seam
        (:func:`run_autonomous_adapter`: effective per-Attempt
        sandbox/grant/trace contract, kernel containment, observed
        post-execution evidence persisted against the run's database).
        A pre-launch fail-closed refusal becomes a typed FAILED
        result -- no semantic child ever started -- so downstream
        dispatch accounting proceeds uniformly.
        """
        if autonomous is None:
            return adapter.run

        def _invoke(request: WorkerRequest) -> WorkerResult:
            try:
                return run_autonomous_adapter(
                    adapter,
                    request,
                    autonomous=autonomous,
                    attempt_id=attempt_id,
                    run_id=run_id,
                    repo_root=repo_root,
                    db=self.db,
                )
            except AutonomousEvidenceError as exc:
                # Durable trace evidence is missing AFTER semantic
                # execution: effects are uncertain, so this is never an
                # ordinary success -- and never a clean pre-launch
                # refusal either.  The FAILED result carries the
                # uncertainty explicitly via a stable marker (see
                # ``is_evidence_uncertain_error``); its outcome is
                # deliberately NOT an ordinary provider/runtime
                # failure, and the dispatch loop terminalizes on the
                # marker with the FailoverEngine BLOCK in force.
                return WorkerResult(
                    provider=provider,
                    model=model,
                    status=AttemptStatus.FAILED,
                    summary="Autonomous trace evidence persistence failed",
                    stdout="",
                    stderr="",
                    error=(
                        "autonomous trace evidence missing after semantic "
                        f"execution; effects uncertain: {exc}"
                    ),
                    outcome=DispatchOutcome.UNKNOWN,
                )
            except SandboxedProcessError as exc:
                return WorkerResult(
                    provider=provider,
                    model=model,
                    status=AttemptStatus.FAILED,
                    summary="Autonomous boundary refused dispatch",
                    stdout="",
                    stderr="",
                    error=(f"autonomous execution refused before semantic launch: {exc}"),
                    outcome=DispatchOutcome.PROVIDER_OR_RUNTIME_FAILURE,
                )

        return _invoke

    def _ensure_supervision(
        self,
        run_id: str,
        attempt_id: str | None = None,
        *,
        deadline_at: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        worker_pid: int | None = None,
        worker_identity: str | None = None,
    ) -> None:
        """Idempotently upsert a supervision row with authoritative attempt context.

        The supervisor layer normally creates the row before spawning a
        subprocess.  When running directly (e.g. ``orch run`` or tests), the
        orchestrator creates it so ``record_worker_activity`` can always
        update ``last_activity_at``.  Existing rows are updated with the new
        attempt context (deadline, provider, model, attempt_id) so the
        supervision record always reflects the *current* worker dispatch.
        """
        transcript_path = str(get_transcript_path(self.db, run_id))
        existing = self.db.get_supervision(run_id)
        if existing is not None and existing.current_attempt_id == attempt_id:
            # Same attempt: update with new identity info if not already present.
            if existing.worker_identity is None and worker_identity is not None:
                self.db.upsert_supervision(
                    SupervisionRecord(
                        run_id=run_id,
                        supervisor_identity=None,
                        supervisor_state=ManagerState.ATTACHED.value,
                        worker_pid=worker_pid or existing.worker_pid,
                        worker_identity=worker_identity,
                        provider=existing.provider,
                        model=existing.model,
                        started_at=existing.started_at,
                        last_activity_at=existing.last_activity_at,
                        health_state=existing.health_state,
                        health_reason=existing.health_reason,
                        current_package_id=existing.current_package_id,
                        current_attempt_id=existing.current_attempt_id,
                        deadline_at=existing.deadline_at,
                        transcript_path=existing.transcript_path,
                        wake_condition=existing.wake_condition,
                        manager_state=existing.manager_state,
                        updated_at=current_iso_timestamp(),
                    )
                )
            return
        self.db.upsert_supervision(
            SupervisionRecord(
                run_id=run_id,
                supervisor_identity=None,
                supervisor_state=ManagerState.ATTACHED.value,
                worker_pid=worker_pid,
                worker_identity=worker_identity,
                provider=provider or (existing.provider if existing else None),
                model=model or (existing.model if existing else None),
                started_at=current_iso_timestamp(),
                last_activity_at=None,
                health_state=HealthState.UNCERTAIN,
                health_reason=None,
                current_package_id=None,
                current_attempt_id=attempt_id,
                deadline_at=deadline_at,
                transcript_path=transcript_path,
                wake_condition=None,
                manager_state=ManagerState.ATTACHED,
                updated_at=current_iso_timestamp(),
            )
        )

    def _worker_activity_hooks(
        self,
        run_id: str,
        attempt_id: str,
        provider: str,
        model: str,
    ) -> tuple[
        Callable[[str], None],
        Callable[[str], None],
        Callable[[float], None],
        Callable[[], None],
    ]:
        """Build live-output and activity callbacks for one worker dispatch.

        Raw provider lines are mirrored to this process's stdout, which in the
        detached-owner path IS the durable run transcript (and through the web
        PTY terminal, the live UI).  The normalized control-plane projection
        (``supervision.last_activity_at`` plus one ``worker_activity`` event)
        is coalesced to at most one write per
        ``WORKER_ACTIVITY_TOUCH_MIN_INTERVAL_SECONDS`` so observability never
        becomes an event storm.  Soft stalls are reported via
        ``worker_stalled``/``worker_resumed`` events; they never kill the
        worker and never change routing.
        """
        transcript_path = str(get_transcript_path(self.db, run_id))
        last_touch = [0.0]

        def _emit_live(line: str) -> None:
            # Mirroring must never break worker execution.
            # Preserve one line = one transcript line by appending a newline
            # separator.  run_process strips CR/LF before invoking callbacks,
            # so we add back exactly one \\n to keep terminal lines distinct.
            try:
                sys.stdout.write(line + "\n")
                sys.stdout.flush()
            except Exception:
                pass

        def _touch(stream: str) -> None:
            now = time.monotonic()
            if now - last_touch[0] < WORKER_ACTIVITY_TOUCH_MIN_INTERVAL_SECONDS:
                return
            last_touch[0] = now
            self.db.record_worker_activity(
                run_id,
                attempt_id=attempt_id,
                provider=provider,
                model=model,
                stream=stream,
                transcript_path=transcript_path,
            )

        def on_stdout(line: str) -> None:
            _emit_live(line)
            _touch("stdout")

        def on_stderr(line: str) -> None:
            _emit_live(line)
            _touch("stderr")

        def on_stall(quiet_seconds: float) -> None:
            # Output-only quiet is ATTENTION evidence, never a destructive
            # stall verdict: only WorkerMonitor — with whole-tree runtime
            # evidence — may confirm STALLED.  While the monitor says
            # AMBIGUOUS_BUSY (or anything non-STALLED), this projection must
            # not claim the worker is stalled-for-termination.
            reason = f"output_quiet_{quiet_seconds:.0f}s"
            self.db.update_supervision_health(
                run_id,
                health_state=HealthState.UNCERTAIN,
                health_reason=f"attention: {reason} (not stall-confirmed)",
                attempt_id=attempt_id,
                manager_state=ManagerState.ATTENTION_REQUIRED,
            )
            self.db.record_event(
                run_id,
                "worker_stalled",
                attempt_id=attempt_id,
                payload={"reason": reason, "destructive": False},
            )

        def on_resume() -> None:
            self.db.update_supervision_health(
                run_id,
                health_state=HealthState.HEALTHY,
                health_reason="worker_activity_resumed",
                attempt_id=attempt_id,
            )
            self.db.record_event(
                run_id,
                "worker_resumed",
                attempt_id=attempt_id,
                payload={"reason": "worker_activity_resumed"},
            )

        return on_stdout, on_stderr, on_stall, on_resume

    # Normalized MonitorState -> supervision health projection.  AMBIGUOUS_BUSY
    # is attention-but-continuing and is EXPLICITLY NOT stalled-for-termination.
    _MONITOR_STATE_HEALTH: ClassVar[dict[str, tuple[HealthState, str]]] = {
        "ACTIVE": (HealthState.HEALTHY, "monitor_active"),
        "QUIET": (HealthState.UNCERTAIN, "attention: output and tree quiet"),
        "AMBIGUOUS_BUSY": (
            HealthState.UNCERTAIN,
            "attention: ambiguous_busy continuing (not stall-confirmed)",
        ),
        "STALLED": (HealthState.STALLED, "monitor: stall confirmed"),
        "TERMINATING": (HealthState.UNCERTAIN, "worker terminating"),
        "TERMINAL": (HealthState.TERMINAL, "worker process tree extinct"),
    }

    def _monitor_transition_hook(
        self,
        run_id: str,
        attempt_id: str,
        provider: str,
        model: str,
    ) -> Callable[[dict[str, Any]], None]:
        """Build the bounded durable projection for monitor state transitions.

        run_process invokes this callback ONLY when the WorkerMonitor's state
        changes (never per sample).  Each invocation records one normalized
        ``worker_monitor_state`` event (run_id, attempt_id, provider, model,
        state, reason/evidence summary, timestamp) and updates the supervision
        health coherently.  The raw transcript remains the high-volume source;
        this is a bounded control-plane projection.
        """

        def _on_transition(payload: dict[str, Any]) -> None:
            try:
                state = str(payload.get("state") or "")
                reason = str(payload.get("reason") or "")
                self.db.record_event(
                    run_id,
                    "worker_monitor_state",
                    attempt_id=attempt_id,
                    payload={
                        "run_id": run_id,
                        "attempt_id": attempt_id,
                        "provider": provider,
                        "model": model,
                        "state": state,
                        "reason": reason,
                        "member_count": payload.get("member_count"),
                        "evidence_incomplete": payload.get("evidence_incomplete"),
                        "timestamp": current_iso_timestamp(),
                    },
                )
                health, default_reason = self._MONITOR_STATE_HEALTH.get(
                    state, (HealthState.UNCERTAIN, "monitor_state")
                )
                attention = state in ("QUIET", "AMBIGUOUS_BUSY", "STALLED", "TERMINATING")
                health_reason = f"{default_reason}: {reason}" if reason else default_reason
                self.db.update_supervision_health(
                    run_id,
                    health_state=health,
                    health_reason=health_reason,
                    attempt_id=attempt_id,
                    manager_state=(ManagerState.ATTENTION_REQUIRED if attention else None),
                )
            except Exception:
                # Observability must never break worker execution.
                pass

        return _on_transition

    def _publish_gate_passed_candidate(
        self,
        *,
        run_id: str,
        attempt_id: str,
        config: RunConfig,
        repo: Path,
        commit_sha: str | None,
        branch_name: str,
        worktree_path: Path | str,
    ) -> CandidatePublication | None:
        """Publish one gate-passed candidate to the remote, or fail closed.

        Called only at the reviewable candidate boundary: after a candidate
        commit exists, belongs to this run, carries valid provenance, has
        passed its gate, and its worktree is quiescent.  When the run's frozen
        ``publish_candidate`` decision is False this is a no-op.

        Publication failure is an infrastructure failure: it raises
        :class:`CandidatePublicationError` (after recording durable evidence)
        so the run fails deterministically instead of being reclassified as a
        worker/model/gate failure or consuming worker retry budget.
        """
        if not config.publish_candidate:
            return None
        if not commit_sha:
            raise CandidatePublicationError(
                "Candidate publication failed: gate-passed attempt has no candidate commit"
            )

        # Candidate eligibility: the persisted attempt must provably belong to
        # this run with valid provenance and a quiescent worktree.
        run = self.db.get_run(run_id)
        attempt = self.db.get_attempt(attempt_id)
        if run is None or attempt is None:
            raise CandidatePublicationError(
                "Candidate publication failed: run or attempt evidence is missing"
            )
        try:
            assert_candidate_provenance(
                run,
                attempt,
                lineage_base_sha=next(
                    (
                        prior.commit_sha
                        for prior in reversed(self.db.get_attempts_for_run(run_id))
                        if prior.attempt_number < attempt.attempt_number and prior.commit_sha
                    ),
                    None,
                ),
                # C11-D: a failover-legitimate Attempt based at its
                # validated required-base anchor (re-verified live
                # against durable decision evidence) remains provably
                # a candidate of this run.
                extra_authorized_bases=failover_authorized_bases(self.db, run_id),
            )
        except ProvenanceError as exc:
            raise CandidatePublicationError(
                f"Candidate publication failed: provenance check rejected candidate: {exc}"
            ) from exc
        if not GitManager.is_clean(worktree_path):
            raise CandidatePublicationError(
                "Candidate publication failed: candidate worktree is not quiescent"
            )

        # Production publication authority: the run's durable frozen
        # ``publish_candidate`` decision is the ONLY source of
        # candidate-push authority.  It maps to an explicit
        # ``CANDIDATE_BRANCH_PUSH`` grant; without it (unreachable here
        # -- the flag check above returned -- but carried explicitly so
        # the authority composition stays total) publication refuses.
        # The worker half is the run's minimal implementation
        # authority: candidate publication never implies merge / tag /
        # deploy / governance power (see ``worker_can_publish``).
        # No ephemeral allow flag exists on this path.
        from saberops.publishing import (
            DEFAULT_PUBLISHING_AUTHORITY,
            DEFAULT_WORKER_AUTHORITY,
            PublicationKind,
            PublishingAuthority,
            governed_publish_candidate,
        )

        if config.publish_candidate:
            publishing_authority = PublishingAuthority(
                granted=frozenset({PublicationKind.CANDIDATE_BRANCH_PUSH}),
                worker_id=f"orchestrator:{run_id}",
                reason="run frozen publish_candidate decision",
            )
        else:
            publishing_authority = DEFAULT_PUBLISHING_AUTHORITY
        try:
            publication = governed_publish_candidate(
                DEFAULT_WORKER_AUTHORITY,
                publishing_authority,
                repo_path=repo,
                candidate_sha=commit_sha,
                candidate_branch=branch_name,
                remote_name="origin",
            )
        except CandidatePublicationError as exc:
            # Durable, credential-free failure evidence before propagating.
            self.db.record_event(
                run_id,
                "candidate_publish_failed",
                attempt_id=attempt_id,
                payload={
                    "candidate_sha": commit_sha,
                    "candidate_branch": branch_name,
                    "remote_name": "origin",
                    "error": str(exc)[:400],
                    "classification": "infrastructure_publication_failure",
                },
            )
            raise
        self.db.record_event(
            run_id,
            "candidate_published",
            attempt_id=attempt_id,
            payload={
                **publication.as_dict(),
                "run_id": run_id,
            },
        )
        return publication

    def _resolve_c13_f_review_decision(
        self,
        *,
        run_id: str,
        config: RunConfig,
        target_repo: Path | str,
        base_commit: str,
        candidate_sha: str | None,
        is_autonomous: bool = False,
    ) -> Any:
        """Compute and persist the frozen C13-F review-necessity decision.

        Returns the decision whether or not the persistence write
        succeeds: callers consult the returned value but the persisted
        row is also the authoritative source Accept / supervisor
        recovery consult on reconstruction.  A malformed persisted
        decision is never re-derived into a fresh classifier answer:
        it fails closed as review-required via the conservative legacy
        seam (mirroring the Accept engine, which treats a malformed
        persisted decision as review-required).

        The decision is computed against the **actual candidate change
        set** (``git diff base..candidate``), not against speculative
        task text -- this is what lets the LOW skip path be safely
        deterministic.  Empty / missing diffs default to the legacy
        conservative semantic (review required) so a candidate with no
        observable change cannot be silently approved as LOW.

        ``is_autonomous`` is the truth about the producing runtime
        contract (not derived from ``config.review_enabled``); an
        autonomous run produces ``review_requirement_source =
        AUTONOMOUS_SAFETY`` regardless of any subsequent precedence
        level, so the audit cannot lose the autonomous hard-minimum
        signal.  ``actual_tier`` is the durable tier actually used to
        produce this candidate (manual routing tier for ``routing_mode
        == "manual"``, otherwise the run row's persisted ``tier`` which
        ``update_run_tier`` keeps current through every escalation) so
        auto-classified / escalated T3 work carries the T3
        hard-minimum signal into C13-F.
        """
        from saberops.review_adaptive import (
            ReviewDecision,
            ReviewPolicyError,
            classify_change,
            conservative_legacy_decision,
            decide_review,
            validate_project_review_policy_invariants,
        )

        # Reconstruct from persisted decision when present so the
        # decision survives retry / repair / supervisor restart without
        # re-derivation against possibly-evolved classifier state.
        # A malformed persisted value fails closed as review-required:
        # it MUST NOT fall through to a fresh classifier computation,
        # which could reclassify corrupted authoritative state into a
        # LOW skip.  The corrupt bytes are left untouched on the row
        # (forensic evidence); the returned conservative decision
        # requires review, matching the Accept engine's read path.
        persisted = self.db.get_review_risk_decision_json(run_id)
        if persisted is not None:
            try:
                decision = ReviewDecision.from_json(persisted)
                validate_project_review_policy_invariants(decision)
                return decision
            except ReviewPolicyError:
                return conservative_legacy_decision(
                    owner_explicit_review=bool(config.review_enabled),
                    review_policy_mode=config.review_policy_mode,
                )

        task_text = (config.task or "").strip()
        changed_paths: tuple[str, ...] = ()
        if candidate_sha and base_commit and candidate_sha != base_commit:
            try:
                from saberops.review_adaptive import diff_changed_paths

                changed_paths = diff_changed_paths(target_repo, base_commit, candidate_sha)
            except ReviewPolicyError:
                # Cannot read the candidate diff: fail closed (review
                # required).  The conservative decision path below
                # emits the right answer.
                changed_paths = ()

        # Resolve the actual tier that produced this candidate.
        actual_tier: Tier | None = None
        # Tracks when the auto-routed branch could NOT durably resolve the
        # tier this candidate was actually produced at.  The T3 hard
        # minimum must never be silently erasable by a missing / unreadable
        # run row: in any such case we fail closed into
        # ``independent_review_required=True`` after the classifier call,
        # below, with a truthful ``review_requirement_source`` so the audit
        # does not falsely claim the tier was observed as T3.
        auto_tier_unresolvable = False
        if config.routing_mode == "manual":
            actual_tier = config.manual_tier
        else:
            # Auto-routed runs: ``update_run_tier`` keeps the run row's
            # ``tier`` column current through every escalation, so the
            # persisted value at candidate-ready time IS the durable
            # resolved tier.  Inability to read that row, an unexpected
            # missing row, or an unreadable ``tier`` value all leave the
            # candidate's actual tier unknown -- so the candidate may
            # have been produced at T3 without the audit being able to
            # see that signal.  ``auto_tier_unresolvable`` forces the
            # post-classifier override to fail closed regardless of how
            # the diff classifies on path patterns alone.
            run_row: Run | None = None
            try:
                run_row = self.db.get_run(run_id)
            except Exception:
                run_row = None
            if run_row is not None and getattr(run_row, "tier", None) is not None:
                actual_tier = run_row.tier
            else:
                auto_tier_unresolvable = True

        classification = classify_change(
            task_text=task_text,
            changed_paths=changed_paths,
            tier=actual_tier,
            autonomous=is_autonomous,
        )
        decision = decide_review(
            classification=classification,
            review_policy_mode=config.review_policy_mode,
            owner_explicit_review=bool(config.review_enabled),
            is_autonomous=is_autonomous,
            decision_id=f"c13f-{run_id}",
        )
        if auto_tier_unresolvable and not decision.independent_review_required:
            # Auto-routed candidate with an unresolvable durable tier:
            # the classifier may have produced a LOW skip because the
            # observable diff looked LOW-eligible, but the actual tier
            # could have been T3 (escalated at any point in the run
            # loop) -- and a HIGH/T3 candidate requires review by the
            # hard minimum.  Override the decision to fail closed with
            # a truthful audit label that does not falsely claim T3 was
            # observed: only the requirement flips, the classifier's
            # actual classification is unchanged so ``changed_paths``,
            # ``signals``, and ``reason`` stay truthful.  The persisted
            # ``review_requirement_source`` carries the
            # ``CLASSIFIER_AUTO_TIER_UNRESOLVABLE`` label so the audit
            # can answer "why was review required for a LOW-looking
            # auto delta?" without ambiguity.
            from saberops.review_adaptive import ReviewRequirementSource

            decision = _frozen_replace(
                decision,
                independent_review_required=True,
                review_requirement_source=(
                    ReviewRequirementSource.CLASSIFIER_AUTO_TIER_UNRESOLVABLE
                ),
                review_skip_reason="",
            )
        self.db.update_review_risk_decision(run_id, decision.to_json())
        return decision

    def _fail_run_for_project_scope_error(
        self, run_id: str, *, phase: str, exc: BaseException, base_commit: str
    ) -> None:
        """Terminalize ``run_id`` after a fatal pre-dispatch C09 scope failure.

        Called once from the outer guard around project-scope reconstruction/
        freezing in :meth:`_run_core`, before any worker is ever dispatched.
        This is infrastructure failure evidence -- never a worker, provider or
        model capability failure -- so it spends no tier escalation, no
        SURGEON and no worker retry budget.  Bounded, non-secret evidence is
        recorded via the existing event log, and the run is terminalized
        through the existing ``fail_run_if_nonterminal`` lifecycle helper
        (idempotent: a run already terminal is left alone, no duplicate
        terminalization event is created).
        """
        # ``raise ... from exc`` chains leave the original, more specific
        # failure (e.g. ``ProjectMetadataError``/``ProjectScopeIntegrityError``)
        # reachable via ``__cause__``; prefer that for evidence quality while
        # still terminalizing on whatever exception actually propagated.
        root_exc: BaseException = exc.__cause__ if exc.__cause__ is not None else exc
        bounded_message = str(root_exc).strip()[:2000]
        self.db.record_event(
            run_id,
            "project_scope_failed",
            payload={
                "phase": phase,
                "error_class": type(root_exc).__name__,
                "message": bounded_message,
                "base_commit": base_commit,
                "run_id": run_id,
            },
        )
        self.db.fail_run_if_nonterminal(
            run_id,
            completed_at=current_iso_timestamp(),
            result_summary=f"project scope analysis failed ({phase}): {bounded_message}"[:2000],
        )

    def run(
        self,
        task: str | None = None,
        repo_path: Path | str | None = None,
        gate_command: str = "make gate",
        tier: Tier | None = None,
        model_override: str | None = None,
        provider_override: str | None = None,
        training_allowed: bool = True,
        is_peak: bool | None = None,
        max_attempts: int = 2,
        worker_timeout: float | None = None,
        gate_timeout: float | None = None,
        review: bool = False,
        review_timeout: float | None = None,
        config: RunConfig | None = None,
        run_id: str | None = None,
        autonomous: AutonomousDispatchConfig | None = None,
    ) -> RunResult:
        """Execute a task end-to-end with worktree isolation and deterministic gate checks.

        ``run_id`` adoption is restricted to ``PENDING`` runs; interrupted
        ``RUNNING`` recovery must use :meth:`recover_run`, which performs the
        dedicated positive-evidence cold-takeover contract.

        ``autonomous`` (None by default) opts this run's semantic worker
        dispatches into the proven enforcement seam: every
        registry-selected adapter executes through per-Attempt
        sandbox/grant/trace containment, or fails closed before
        semantic launch with a typed reason.
        """
        return self._run_core(
            task=task,
            repo_path=repo_path,
            gate_command=gate_command,
            tier=tier,
            model_override=model_override,
            provider_override=provider_override,
            training_allowed=training_allowed,
            is_peak=is_peak,
            max_attempts=max_attempts,
            worker_timeout=worker_timeout,
            gate_timeout=gate_timeout,
            review=review,
            review_timeout=review_timeout,
            config=config,
            run_id=run_id,
            adoption_mode="pending_only",
            autonomous=autonomous,
        )

    def recover_run(
        self,
        run_id: str,
        *,
        config: RunConfig | None = None,
        expected_owner_generation: int | None = None,
        expected_owner_id: str | None = None,
    ) -> RunResult:
        """Resume a previously interrupted RUNNING run via C05 cold takeover.

        Distinct from :meth:`run`: this entry point accepts only ``RUNNING``
        runs and only proceeds when both the strict positive-evidence recovery
        predicate is satisfied AND the calling process holds an authoritative
        fenced execution-owner reservation for ``expected_owner_generation``.

        The fencing token requirement is what distinguishes C05 detached cold
        recovery from a generic continuation: every mutation under
        ``recover_run`` (closing the crashed attempt, persisting a checkpoint,
        launching a replacement worker) must be fenced to one exact new owner
        generation that has been claimed before this child process was spawned.
        A direct unfenced call -- one that lacks the matching generation in
        ``ORCH_EXECUTION_OWNER_GENERATION`` or finds a different generation on
        the durable record -- fails closed before any checkpoint or attempt
        mutation can occur.  Only the supervisor-launched recovery child
        builds this exact condition (see
        :meth:`saberops.supervisor.RunSupervisor.ensure_recovery_running`).
        """
        # Recovery is a fresh-process boundary.  An autonomous run must carry
        # its complete non-secret dispatch contract in the persisted RunConfig;
        # accepting a caller's partial in-memory reconstruction here would let
        # recovery silently fall back to plain adapter.run.
        autonomous: AutonomousDispatchConfig | None = None
        supervisor: AutonomousSupervisor | None = None
        persisted_run = self.db.get_run(run_id)
        if persisted_run is not None and persisted_run.config_json:
            try:
                persisted_config = json.loads(persisted_run.config_json)
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError(
                    f"Cannot recover run '{run_id}': persisted config is invalid JSON"
                ) from exc
            if not isinstance(persisted_config, dict):
                raise ValueError(
                    f"Cannot recover run '{run_id}': persisted config must be an object"
                )
            if config is None:
                from saberops.config import reconstruct_run_config

                try:
                    config = reconstruct_run_config(persisted_config)
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f"Cannot recover run '{run_id}': persisted config cannot be reconstructed"
                    ) from exc
            runtime_payload = (
                persisted_config.get("autonomous_runtime")
                if isinstance(persisted_config, dict)
                else None
            )
            has_autonomous_evidence = any(
                event.event_type == "autonomous_supervisor_decision"
                for event in self.db.get_complete_events_for_run(run_id)
            )
            if runtime_payload is None:
                if has_autonomous_evidence:
                    raise ValueError(
                        f"Cannot recover autonomous run '{run_id}': persisted "
                        "autonomous runtime contract is missing"
                    )
            else:
                contract = AutonomousRuntimeContract.from_mapping(runtime_payload)
                autonomous = contract.dispatch
                supervisor = AutonomousSupervisor(
                    self.db,
                    self.registry,
                    binding_registry=contract.binding_registry,
                    budgets=contract.budgets,
                    effort_policy=contract.effort_policy,
                    requested_effort=contract.requested_effort,
                    # C15-XB-02: contract.binding_policy is the persisted
                    # Orchestrator-model policy and is intentionally
                    # NOT consulted by worker-binding selection on
                    # restart; the supervisor reads it for read-only
                    # diagnostics only.
                )
        return self._run_core(
            task=None,
            repo_path=None,
            gate_command="make gate",
            tier=None,
            model_override=None,
            provider_override=None,
            training_allowed=True,
            is_peak=None,
            max_attempts=2,
            worker_timeout=None,
            gate_timeout=None,
            review=False,
            review_timeout=None,
            config=config,
            run_id=run_id,
            adoption_mode="recovery_only",
            expected_owner_generation=expected_owner_generation,
            expected_owner_id=expected_owner_id,
            autonomous=autonomous,
            supervisor=supervisor,
        )

    def _autonomous_pre_accept_valid(self, run_id: str, config: RunConfig) -> bool:
        """Verify every required pre-accept condition from durable state.

        Candidate-ready is valid for :class:`AcceptEngine` acceptance only
        when required independent review substantively approved
        (PASS/PASS_WITH_BACKLOG) AND required candidate publication
        succeeded for the final candidate SHA.  Anything else fails closed.
        """
        run = self.db.get_run(run_id)
        if run is None or run.status != RunStatus.COMPLETED:
            return False
        try:
            loop_state = self.db.get_review_loop_state(run_id)
        except Exception:
            loop_state = None
        terminal_raw = loop_state.get("terminal_verdict") if isinstance(loop_state, dict) else None
        terminal_verdict: ReviewVerdict | None = None
        if isinstance(terminal_raw, str) and terminal_raw:
            try:
                terminal_verdict = ReviewVerdict(terminal_raw)
            except ValueError:
                terminal_verdict = None
        if terminal_verdict is None:
            latest_review = self.db.get_latest_review(run_id)
            terminal_verdict = latest_review.verdict if latest_review else None
        if terminal_verdict not in (
            ReviewVerdict.PASS,
            ReviewVerdict.PASS_WITH_BACKLOG,
        ):
            return False
        if not config.publish_candidate:
            return True
        attempts = self.db.get_attempts_for_run(run_id)
        successful = [a for a in attempts if a.status == AttemptStatus.SUCCESS and a.commit_sha]
        if not successful:
            return False
        final_sha = successful[-1].commit_sha
        for event in self.db.get_events_by_type(run_id, "candidate_published"):
            payload = event.payload or {}
            if (
                payload.get("candidate_sha") == final_sha
                and payload.get("verified_remote_sha") == final_sha
            ):
                return True
        return False

    def run_autonomous_project(
        self,
        *,
        task: str | None = None,
        repo_path: Path | str | None = None,
        gate_command: str = "make gate",
        run_id: str | None = None,
        project_id: str | None = None,
        config: RunConfig | None = None,
        tier: Tier | None = None,
        provider_override: str | None = None,
        model_override: str | None = None,
        training_allowed: bool = True,
        is_peak: bool | None = None,
        max_attempts: int = 4,
        worker_timeout: float | None = None,
        gate_timeout: float | None = None,
        review_timeout: float | None = None,
        review_limit: int = 3,
        supervisor_config: SupervisorBudgets | None = None,
        binding_registry: Any | None = None,
        autonomous: AutonomousDispatchConfig | None = None,
        reasoning_effort: ReasoningEffort | None = None,
        binding_policy: OrchBindingPolicy | None = None,
        effort_policy: EffortPolicy | None = None,
    ) -> AutonomousProjectResult:
        """Execute an autonomous project run through the canonical lifecycle.

        Production authority comes ONLY from the persisted owner settings
        (:func:`load_authority_settings`); this signature exposes no
        ``authority_policy`` and no ``authority_settings_loader`` override,
        so no caller can broaden persisted owner authority.

        The run executes through the existing :meth:`_run_core` lifecycle
        with a narrowly typed supervisor-decision seam (supervisor
        :meth:`decide` outputs consumed per dispatch; production never calls
        ``AutonomousSupervisor.drive``).  AUTONOMOUS stops at candidate-ready;
        FULL_AUTONOMY additionally accepts via :class:`AcceptEngine` only
        after every pre-accept condition validates, then projects the
        acceptance into the Project ledger when ``project_id`` is given.

        A caller-supplied ``config`` is accepted only when it preserves the
        mandatory autonomous safety property ``review_enabled=True``; a
        config that disables review is refused before any worker launch,
        run creation, or candidate-ready transition.

        ``autonomous`` is mandatory: without an explicit containment
        contract the autonomous production path would silently fall back
        to plain ``adapter.run`` and bypass the C11-C sandbox/trace/effect
        boundary, so a missing contract is refused closed before any
        worker launch, run creation, or candidate-ready transition.
        ``reasoning_effort`` is an optional typed effort request resolved
        through the canonical C11-B resolver (never a provider/model
        bypass: the exact binding still comes from the eligible ladder).

        Raw ``provider_override`` / ``model_override`` are refused here:
        they must never act as autonomous binding authority.  Manual
        autonomous pinning names an exact binding through the canonical
        ``binding_registry`` (or via a :class:`WorkerCandidate` whose
        ``binding_id`` matches an owner-approved entry); every
        autonomous launch additionally requires an authoritative non-empty
        ``binding_registry`` or the supervisor stops with
        NO_ELIGIBLE_BINDING and zero launch.

        C15-XB-02: ``binding_policy`` (an :class:`OrchBindingPolicy`)
        is preserved as an Orchestrator-model policy parameter for
        /orchestrator UI selection but is **not** consulted by worker
        binding selection in this lifecycle.  Changing
        ``binding_policy`` MUST NOT change which binding a worker
        routing candidate resolves to.
        """
        if provider_override is not None or model_override is not None:
            raise ValueError(
                "Autonomous execution refuses raw provider_override / "
                "model_override: exact execution binding identity is "
                "carried through the canonical BindingRegistry plus "
                "per-candidate WorkerCandidate.binding_id; raw "
                "provider/model overrides never synthesize one."
            )
        if autonomous is None:
            raise ValueError(
                "Autonomous execution requires an explicit "
                "AutonomousDispatchConfig containment contract; "
                "autonomous=None is refused before any worker launch "
                "because the production path must never silently fall "
                "back to plain adapter.run."
            )
        settings = load_authority_settings()
        mode = settings.mode
        policy = settings.policy
        if not policy.auto_start:
            refusal_id = run_id or f"autonomous_refused_{uuid.uuid4().hex[:8]}"
            return AutonomousProjectResult(
                run_id=refusal_id,
                completed=False,
                summary=(
                    f"Owner authority '{mode.value}' does not auto-start "
                    "autonomous work: zero worker launches."
                ),
                authority_mode=mode.value,
            )
        if config is None:
            if task is None or repo_path is None:
                raise ValueError("task and repo_path are required when config is not supplied")
            config = resolve_run_config(
                task=task,
                repo_path=repo_path,
                tier=tier,
                training_allowed=training_allowed,
                provider_override=provider_override,
                model_override=model_override,
                gate_command=gate_command,
                max_attempts=max_attempts,
                worker_timeout=worker_timeout,
                gate_timeout=gate_timeout,
                review_enabled=True,
                review_timeout=review_timeout,
                review_limit=review_limit,
            )
        elif not config.review_enabled:
            # Mandatory autonomous safety: ``review_enabled=True`` is the
            # boundary that gates candidate-ready/acceptance for autonomous
            # operation.  A caller-supplied RunConfig that disables review is
            # not eligible: refuse closed before any worker launch, run
            # creation, or candidate-ready transition.  This is the
            # narrowest contract-consistent point: no autonomous execution
            # can begin under a config that forfeits the independent review
            # gate.
            raise ValueError(
                "Autonomous execution requires review_enabled=True; a "
                "caller-supplied RunConfig with review_enabled=False is "
                "refused before any worker launch."
            )
        if config.model_override is not None or config.provider_override is not None:
            raise ValueError(
                "Autonomous execution refuses raw provider_override / "
                "model_override carried by RunConfig: exact execution "
                "binding identity is carried through the canonical "
                "BindingRegistry plus per-candidate "
                "WorkerCandidate.binding_id; raw provider/model "
                "overrides never synthesize one."
            )
        # Owner-level exact bindings are the canonical source for future
        # autonomous runs.  A caller-supplied registry always wins
        # (tests, recovery); owner state only fills a genuinely absent
        # input, and empty owner state leaves the fail-closed
        # NO_ELIGIBLE_BINDING path untouched.
        #
        # C15-XB-02: ``binding_policy`` (an :class:`OrchBindingPolicy`)
        # is the *Orchestrator-model* selection policy, NOT a worker
        # binding selection policy.  The autonomous supervisor derives
        # worker bindings from the canonical :class:`BindingRegistry`
        # plus the per-candidate :attr:`WorkerCandidate.binding_id`
        # reference (C15-XB-01).  ``binding_policy`` is preserved in
        # the frozen runtime contract so the persisted owner choice is
        # reconstructable across restart, but it is no longer passed
        # to the supervisor's worker-binding selection logic.
        binding_registry = _adopt_owner_binding_registry(binding_registry)
        runtime_contract = AutonomousRuntimeContract(
            dispatch=autonomous,
            binding_registry=binding_registry,
            budgets=supervisor_config or SupervisorBudgets(),
            effort_policy=effort_policy or DEFAULT_WORKER_EFFORT_POLICY,
            requested_effort=reasoning_effort,
            binding_policy=binding_policy,
        )
        # Persist the exact autonomous contract before any worker can launch.
        config = _frozen_replace(config, autonomous_runtime=runtime_contract.as_dict())
        supervisor = AutonomousSupervisor(
            self.db,
            self.registry,
            binding_registry=binding_registry,
            budgets=supervisor_config,
            effort_policy=effort_policy,
            requested_effort=reasoning_effort,
            # C15-XB-02: binding_policy (OrchBindingPolicy) is no longer
            # passed as worker-binding selection authority.  It is
            # preserved on the supervisor attribute for read-only
            # diagnostics but is deliberately not consulted by worker
            # binding resolution.
        )
        direct_owner_holder: list[Any] = []
        try:
            result = self._run_core(
                task=None,
                repo_path=None,
                gate_command=config.gate_command,
                tier=None,
                model_override=None,
                provider_override=None,
                training_allowed=True,
                is_peak=is_peak,
                max_attempts=max_attempts,
                worker_timeout=worker_timeout,
                gate_timeout=gate_timeout,
                review=False,
                review_timeout=review_timeout,
                config=config,
                run_id=run_id,
                adoption_mode="pending_only",
                autonomous=autonomous,
                supervisor=supervisor,
                direct_owner_holder=direct_owner_holder,
            )
            effective_run_id = result.run_id
            if result.status != RunStatus.COMPLETED:
                return AutonomousProjectResult(
                    run_id=effective_run_id,
                    completed=False,
                    summary=result.summary,
                    authority_mode=mode.value,
                    commit_sha=result.commit_sha,
                )
            if not policy.auto_accept:
                return AutonomousProjectResult(
                    run_id=effective_run_id,
                    completed=True,
                    summary=result.summary,
                    authority_mode=mode.value,
                    commit_sha=result.commit_sha,
                )
            if not self._autonomous_pre_accept_valid(effective_run_id, config):
                return AutonomousProjectResult(
                    run_id=effective_run_id,
                    completed=True,
                    summary=(
                        f"{result.summary} Pre-accept verification did not validate "
                        "(required review approval and/or candidate publication "
                        "missing): stopping at candidate-ready without acceptance."
                    ),
                    authority_mode=mode.value,
                    commit_sha=result.commit_sha,
                )
            from saberops.accept import AcceptEngine

            accepted = AcceptEngine(db=self.db).accept_run(effective_run_id)
            closure: AutonomousClosureResult | None = None
            if project_id is not None:
                closure = close_accepted_run(
                    self.db,
                    project_id=project_id,
                    run_id=effective_run_id,
                    final_sha=accepted.final_sha,
                )
            return AutonomousProjectResult(
                run_id=effective_run_id,
                completed=True,
                summary=accepted.summary,
                authority_mode=mode.value,
                commit_sha=result.commit_sha,
                accepted_sha=accepted.final_sha,
                closure=closure,
            )
        finally:
            for owner in direct_owner_holder:
                if self.db.release_execution_owner(
                    owner.run_id,
                    owner_id=owner.owner_id,
                    generation=owner.generation,
                    reason="autonomous_run_finished",
                ):
                    self.db.record_event(
                        owner.run_id,
                        "execution_owner_released",
                        payload={
                            "owner_id": owner.owner_id,
                            "generation": owner.generation,
                            "reason": "autonomous_run_finished",
                        },
                    )

    def _run_core(
        self,
        *,
        task: str | None,
        repo_path: Path | str | None,
        gate_command: str,
        tier: Tier | None,
        model_override: str | None,
        provider_override: str | None,
        training_allowed: bool,
        is_peak: bool | None,
        max_attempts: int,
        worker_timeout: float | None,
        gate_timeout: float | None,
        review: bool,
        review_timeout: float | None,
        config: RunConfig | None,
        run_id: str | None,
        adoption_mode: str,
        expected_owner_generation: int | None = None,
        expected_owner_id: str | None = None,
        autonomous: AutonomousDispatchConfig | None = None,
        supervisor: AutonomousSupervisor | None = None,
        direct_owner_holder: list[Any] | None = None,
    ) -> RunResult:
        """Core orchestration engine; gated by the ``adoption_mode`` contract.

        ``pending_only`` (default) accepts ``run_id`` only when the run is
        ``PENDING``.  ``recovery_only`` accepts only ``RUNNING`` runs and
        only proceeds when both the strict positive-evidence recovery
        predicate passes AND the caller has authoritative fenced ownership
        of ``expected_owner_generation``; otherwise it fails closed before
        any checkpoint or attempt mutation.

        A supervised (autonomous) dispatch without an explicit
        ``autonomous`` containment contract is refused here -- before any
        worker launch -- so the production autonomous path can never
        silently fall back to plain ``adapter.run``.  Ordinary
        (unsupervised) runs preserve the legacy ``None`` behavior.
        """
        if supervisor is not None and autonomous is None:
            raise ValueError(
                "Supervised autonomous execution requires an explicit "
                "AutonomousDispatchConfig containment contract; refusing "
                "before any worker launch."
            )
        if config is None:
            if task is None or repo_path is None:
                raise ValueError("task and repo_path are required when config is not supplied")
            config = resolve_run_config(
                task=task,
                repo_path=repo_path,
                tier=tier,
                training_allowed=training_allowed,
                model_override=model_override,
                provider_override=provider_override,
                gate_command=gate_command,
                max_attempts=max_attempts,
                worker_timeout=worker_timeout,
                gate_timeout=gate_timeout,
                review=review,
                review_timeout=review_timeout,
            )
        task = config.task
        gate_command = config.gate_command
        repo = Path(config.target_repo)
        if not GitManager.is_git_repo(repo):
            raise ValueError(f"Target path is not a valid Git repository: {repo}")

        # Fail closed immediately if source repository has tracked or untracked changes
        if not GitManager.is_clean(repo):
            raise RuntimeError(
                f"Target repository '{repo}' has uncommitted or untracked changes. "
                "Commit, stash, or clean working tree before running orch."
            )

        base_commit = GitManager.get_head_commit(repo)
        # C09: whether this call is adopting an existing run matters for which
        # revision the frozen project graph/scope must be built against.  The
        # ``run_id`` parameter is overwritten further down once the run is
        # resolved, so its adoption/new-run meaning is captured once, here.
        is_adoption = run_id is not None
        # Frozen per-run publication decision lives in ``config``; this tracks
        # the evidence of the latest successful candidate publication so the
        # final run result can surface it to the owner/reviewer.
        candidate_publication: CandidatePublication | None = None
        created_at = current_iso_timestamp()

        current_tier = config.initial_tier
        routing_snapshot = None
        routing_payload: dict[str, Any] | None = None
        frozen_control_plane: FrozenControlPlane
        frozen_tools: FrozenToolContract
        frozen_gateway: FrozenGatewayConfig | None = None
        # C09: the run's own authoritative base_commit, captured once the run
        # is resolved below; used only when ``is_adoption`` is True.
        adopted_base_commit: str = ""

        if run_id is not None:
            existing_run = self.db.get_run(run_id)
            if existing_run is None:
                raise ValueError(f"Cannot adopt run '{run_id}': run not found")
            adopted_base_commit = existing_run.base_commit
            if adoption_mode == "pending_only":
                if existing_run.status != RunStatus.PENDING:
                    raise ValueError(
                        f"Cannot adopt run '{run_id}': "
                        f"status is {existing_run.status.value}, expected PENDING"
                    )
                self.db.adopt_run_if_pending(run_id)
            elif adoption_mode == "recovery_only":
                if existing_run.status != RunStatus.RUNNING:
                    raise ValueError(
                        f"Cannot recover run '{run_id}': "
                        f"status is {existing_run.status.value}, expected RUNNING"
                    )
                # Required fenced execution authority: every ``recover_run``
                # mutation (closing the crashed attempt, persisting a
                # checkpoint, launching a replacement worker) is fenced to a
                # single generation.  The fencing token MUST reach this
                # entry either as an explicit complete reservation (tests)
                # OR via both ORCH_EXECUTION_OWNER_* environment variables
                # set by the supervisor-launched recovery CLI.
                if expected_owner_generation is None or expected_owner_id is None:
                    from saberops.ownership import parse_owner_reservation

                    reservation = parse_owner_reservation()
                    if reservation is not None:
                        expected_owner_id = reservation.owner_id
                        expected_owner_generation = reservation.generation
                # Retain the in-process API used by lifecycle callers/tests:
                # a caller that already *is* the durable owner may supply the
                # generation and have its own owner id resolved from that
                # identity.  Detached CLI recovery never takes this route;
                # it must provide and adopt the complete environment token.
                if expected_owner_generation is not None and expected_owner_id is None:
                    direct_owner = self.db.get_execution_owner(run_id)
                    if (
                        direct_owner is not None
                        and direct_owner.identity == ProcessIdentity.for_self()
                    ):
                        expected_owner_id = direct_owner.owner_id
                if expected_owner_generation is None or expected_owner_id is None:
                    raise OwnershipError(
                        f"Cannot recover run '{run_id}': no fenced execution "
                        "authority (missing owner reservation); only the "
                        "supervisor-launched recovery child can perform takeover"
                    )
                # Fenced execution authority: the supervisor-launched recovery
                # child must hold an authoritative owner reservation for the
                # exact generation it was spawned with.  A direct unfenced
                # call -- missing generation, stale generation, or a newer
                # generation that appeared since the supervisor's claim --
                # fails closed before any checkpoint or attempt mutation.
                #
                # The fenced token must reference a generation that is
                # STRICTLY GREATER than the predecessor generation the
                # supervisor would have already bumped to.  A direct
                # ``recover_run(expected=N)`` where N is the predecessor's
                # generation is exactly what BLOCKER 4 forbids: the call
                # pretends to be fenced by the predecessor itself, not by
                # a fresh recovery successor.
                owner = self.db.get_execution_owner(run_id)
                if owner is None or not owner.is_active:
                    raise OwnershipError(
                        f"Cannot recover run '{run_id}': no authoritative "
                        "fenced recovery ownership; the supervisor must "
                        "claim a successor generation before recovery child "
                        "launch"
                    )
                child_identity = ProcessIdentity.for_self()
                if owner.identity != child_identity:
                    raise OwnershipError(
                        f"Cannot recover run '{run_id}': durable recovery owner "
                        "identity is not this child process"
                    )
                # The fenced token must reference a generation the
                # supervisor actually claimed as a successor for THIS
                # recovery.  Predecessor generations (or generations the
                # supervisor never promoted) are never valid recovery
                # tokens -- they would let an un-fenced caller pretend
                # to hold the predecessor's "live" identity.
                recovery_claim_event = next(
                    (
                        ev
                        for ev in reversed(self.db.get_complete_events_for_run(run_id))
                        if ev.event_type == "recovery_owner_claimed"
                    ),
                    None,
                )
                recovery_claim_owner_id = (
                    (recovery_claim_event.payload or {}).get("owner_id")
                    if recovery_claim_event is not None
                    else None
                )
                if recovery_claim_event is None:
                    raise OwnershipError(
                        f"Cannot recover run '{run_id}': no fenced recovery "
                        "claim event recorded; the supervisor must have "
                        "claimed a successor generation before this call"
                    )
                if expected_owner_generation is not None and (
                    owner.generation != expected_owner_generation
                    or owner.owner_id != expected_owner_id
                    or recovery_claim_owner_id != expected_owner_id
                ):
                    raise OwnershipError(
                        f"Cannot recover run '{run_id}': fenced token "
                        f"generation {expected_owner_generation} does not "
                        f"match the supervisor-claimed recovery "
                        f"({owner.owner_id}, gen={owner.generation})"
                    )
                # Persist durable evidence that the recovery child holds
                # authority for this exact generation before any further work.
                self.db.record_event(
                    run_id,
                    "recovery_owner_authority_verified",
                    payload={
                        "owner_id": owner.owner_id,
                        "generation": owner.generation,
                        "expected_owner_id": expected_owner_id,
                        "expected_owner_generation": expected_owner_generation,
                    },
                )
            effective_run_id = run_id
            adopted = self.db.get_run(effective_run_id)
            assert adopted is not None
            # C15-XB-FREEZE-01 + SABEROPS persisted-binding-authority: for an
            # already-admitted Run, the Run's OWN durable frozen
            # ``worker_binding_registry`` is the SOLE source of ordinary
            # exact-binding identity.  The persisted value is forced into the
            # effective RunConfig regardless of any caller-supplied value:
            #   * persisted has a valid mapping  -> persisted mapping wins
            #   * persisted has no mapping        -> effective registry is None
            # A caller cannot replace / update / backfill / repair / synthesize
            # historical authority through a supplied RunConfig on adoption or
            # recovery.  An older Run that never persisted a registry therefore
            # refuses to manufacture one from caller state and fails closed
            # with ``NO_REGISTRY`` on its binding-bearing candidates.
            # The autonomous runtime contract remains authoritative for
            # autonomous worker dispatch; this invariant concerns the ordinary
            # binding path only and never touches
            # ``AutonomousRuntimeContract.binding_registry``.
            persisted_run_config: dict[str, Any] | None = None
            if adopted.config_json:
                try:
                    parsed = json.loads(adopted.config_json)
                except (TypeError, json.JSONDecodeError):
                    parsed = None
                if isinstance(parsed, dict):
                    persisted_run_config = parsed
            persisted_registry = (
                persisted_run_config.get("worker_binding_registry")
                if persisted_run_config is not None
                else None
            )
            effective_registry: dict[str, object] | None = (
                dict(persisted_registry) if isinstance(persisted_registry, dict) else None
            )
            # Force the persisted identity into the effective RunConfig
            # unconditionally.  A caller may NOT replace / update / backfill
            # / repair / synthesize historical authority through a supplied
            # RunConfig on adoption or recovery.
            if config.worker_binding_registry != effective_registry:
                config = _frozen_replace(config, worker_binding_registry=effective_registry)
            if adopted.tool_contract_json is not None:
                frozen_tools = FrozenToolContract.from_json(adopted.tool_contract_json)
                if adopted.tool_contract_digest != frozen_tools.digest:
                    raise ValueError(
                        f"Stored tool contract digest mismatch for run '{effective_run_id}'"
                    )
            else:
                frozen_tools = freeze_tool_contract(config.tool_refs, self.tool_registry)
            if adopted.control_plane_snapshot_json is not None:
                frozen_control_plane = reconstruct_frozen_control_plane(
                    adopted.control_plane_snapshot_json,
                    adopted.control_plane_snapshot_digest,
                )
            elif adopted.control_plane_snapshot_digest is not None:
                raise ValueError(
                    f"Stored control-plane digest for run '{effective_run_id}' has no snapshot"
                )
            else:
                # Pre-R2 rows have no immutable policy column.  Preserve
                # compatibility without consulting mutable owner state.
                frozen_control_plane = legacy_frozen_control_plane(
                    required_capabilities=config.required_capabilities,
                    reserve_override=config.reserve_override,
                )
            if adopted.routing_snapshot_json is not None:
                try:
                    payload = json.loads(adopted.routing_snapshot_json)
                except json.JSONDecodeError as exc:
                    raise RoutingConfigError(
                        f"Stored routing snapshot for run '{effective_run_id}' "
                        f"is invalid JSON: {exc}"
                    ) from exc
                if not isinstance(payload, dict):
                    raise RoutingConfigError(
                        f"Stored routing snapshot for run '{effective_run_id}' must be a JSON object"  # noqa: E501
                    )
                routing_snapshot = payload_to_snapshot(payload, run_id=effective_run_id)
                routing_payload = payload
            else:
                routing_snapshot = load_packaged_default()
                routing_payload = snapshot_to_payload(routing_snapshot, adopted.created_at)
            # C10-R1: reconstruct the frozen gateway contract from the run row.
            # Live infrastructure observations (health, quota) are NOT frozen
            # here -- the dispatcher reads those at the per-dispatch boundary.
            if adopted.gateway_snapshot_json is not None:
                frozen_gateway = reconstruct_frozen_gateway_config(
                    json.loads(adopted.gateway_snapshot_json),
                    expected_digest=adopted.gateway_snapshot_digest,
                )
        else:
            # New run: load effective routing once before creation (fail-closed)
            routing_snapshot = load_effective_routing()
            # C15-XB-FREEZE-01: freeze the effective owner binding registry
            # together with the routing snapshot, before the Run is persisted,
            # whenever the snapshot can produce an exact-binding candidate.
            # A static-only snapshot loads nothing: the historical
            # provider/model path neither requires nor reads owner bindings.
            # Autonomous runs persist their own registry in the autonomous
            # runtime contract and are deliberately untouched here.
            if (
                autonomous is None
                and config.worker_binding_registry is None
                and snapshot_references_exact_bindings(routing_snapshot)
            ):
                config = _frozen_replace(
                    config,
                    worker_binding_registry=freeze_owner_binding_registry_payload(),
                )
            frozen_control_plane = freeze_control_plane_policy(
                load_control_plane_policy(),
                required_capabilities=config.required_capabilities,
                reserve_override=config.reserve_override,
                host_access=load_authority_settings().host_access.value,
            )
            provenance = mint_run_provenance(repo, task)
            effective_run_id = provenance.run_id
            routing_payload = snapshot_to_payload(routing_snapshot, created_at)
            frozen_tools = freeze_tool_contract(config.tool_refs, self.tool_registry)
            # C10-R1: freeze the optional gateway contract and persist it on
            # the run row.  When ``RunConfig`` does not carry a frozen
            # gateway, the dispatcher is DIRECT and no gateway evidence is
            # recorded at the dispatch boundary.
            if config.gateway_frozen_config is not None:
                frozen_gateway = reconstruct_frozen_gateway_config(
                    config.gateway_frozen_config,
                    expected_digest=config.gateway_frozen_digest,
                )
            run = Run(
                id=effective_run_id,
                task=task,
                target_repo=str(repo),
                base_commit=base_commit,
                tier=current_tier,
                training_allowed=config.training_allowed,
                gate_command=gate_command,
                status=RunStatus.RUNNING,
                created_at=created_at,
                config_json=json.dumps(config.as_dict()),
                routing_snapshot_json=json.dumps(routing_payload),
                control_plane_snapshot_json=frozen_control_plane.as_json(),
                control_plane_snapshot_digest=frozen_control_plane.digest,
                tool_contract_json=frozen_tools.to_json(),
                tool_contract_digest=frozen_tools.digest,
                project_id=provenance.project_id,
                project_identity_json=provenance.identity.to_json(),
                objective_id=provenance.objective_id,
                gateway_snapshot_json=(
                    frozen_gateway.as_json() if frozen_gateway is not None else None
                ),
                gateway_snapshot_digest=(
                    frozen_gateway.digest if frozen_gateway is not None else None
                ),
            )
            with measure_phase(
                self.db,
                run_id=run.id,
                phase=PhaseKind.LEDGER_DB,
                source_kind="orchestrator._run_core",
                boundary_kind="create_run",
                notes="C14-C: time spent inside the create_run() persistence boundary",
            ):
                self.db.create_run(run)

            # C14-C: phases whose exact production boundary is not
            # currently observable without a larger architectural
            # redesign.  We never invent a number; we record an
            # explicit UNKNOWN row with a note explaining what would
            # need to change before the phase becomes MEASURED-eligible.
            # The recorder writes are isolated from the run lifecycle:
            # if a write fails, the orchestrator proceeds without
            # corrupting the run.
            try:
                mark_phase_unknown(
                    self.db,
                    run_id=run.id,
                    phase=PhaseKind.DIGEST_CALCULATION,
                    source_kind="orchestrator._run_core",
                    boundary_kind="digest_block",
                    notes=(
                        "DIGEST_CALCULATION is fused into projection "
                        "construction and dispatch_evidence persistence; "
                        "no separate exact boundary exists in the current "
                        "production architecture."
                    ),
                )
                mark_phase_unknown(
                    self.db,
                    run_id=run.id,
                    phase=PhaseKind.SANDBOX_SETUP,
                    source_kind="orchestrator._run_core",
                    boundary_kind="sandbox_init",
                    notes=(
                        "SANDBOX_SETUP is fused into the worker subprocess "
                        "startup in the current production architecture; "
                        "no separate exact boundary exists before the "
                        "model-call subprocess begins."
                    ),
                )
                mark_phase_unknown(
                    self.db,
                    run_id=run.id,
                    phase=PhaseKind.HUMAN_WAIT,
                    source_kind="orchestrator._run_core",
                    boundary_kind="human_wait",
                    notes=(
                        "HUMAN_WAIT is unknown by construction: the runtime "
                        "has no positive evidence boundary that any human "
                        "wait actually occurred in autonomous execution."
                    ),
                )
            except Exception:
                # Telemetry MUST NOT corrupt the run; swallow only the
                # C14-C write failure and continue.
                pass

        run_id = effective_run_id
        assert routing_snapshot is not None and routing_payload is not None
        # C15-XB-FREEZE-01: the single run-scoped BindingRegistry ordinary
        # exact-binding dispatch, retry, continuation and repair resolve
        # against.  Reconstructed from the Run's own durable frozen material;
        # never from ``load_owner_bindings()`` (the live owner store), which
        # could have changed since admission.
        frozen_worker_binding_registry = _reconstruct_frozen_worker_binding_registry(
            config.worker_binding_registry
        )

        if direct_owner_holder is not None:
            owned_run = self.db.get_run(run_id)
            if owned_run is None:
                raise OwnershipError(
                    f"Cannot claim autonomous execution owner for unknown run '{run_id}'"
                )
            direct_owner = self.db.claim_execution_owner(
                run_id,
                identity=ProcessIdentity.for_self(),
                project_id=owned_run.project_id,
            )
            direct_owner_holder.append(direct_owner)
            self.db.record_event(
                run_id,
                "execution_owner_claimed",
                payload={
                    "owner_id": direct_owner.owner_id,
                    "generation": direct_owner.generation,
                    "mode": "direct_autonomous",
                },
            )

        # C09: freeze the run's project graph and initial scope exactly once.
        # An adopted/recovered run reconstructs its frozen scope instead of
        # re-analyzing; a new run analyzes the authoritative base revision.
        #
        # C09-R3: by this point the run has already been created/adopted into
        # active execution state (RUNNING).  A fatal failure anywhere in this
        # block is pre-dispatch project-scope infrastructure failure -- never
        # a worker/provider/model failure -- and must never leave an orphaned
        # RUNNING run behind: the outer ``except`` below persists bounded
        # durable evidence and terminalizes the run before re-raising the
        # exact exception unchanged.  ``recovery_only`` reaches this point
        # only after fenced recovery ownership has already been verified
        # above, so terminalizing here never mutates run state ahead of that
        # authoritative check.
        scope_failure_phase = "reconstruct_frozen_scope"
        try:
            scope_bundle = load_scope_bundle(self.db, run_id)
            if scope_bundle is None:
                # The run's own authoritative base_commit -- not necessarily
                # the ``base_commit`` read from the live repository above,
                # which for an adoption reflects whatever HEAD happens to be
                # *now*, not whatever HEAD was when the run was created.
                authoritative_base_commit = adopted_base_commit if is_adoption else base_commit
                scope_failure_phase = "base_revision_mismatch"
                if is_adoption and base_commit != authoritative_base_commit:
                    # Legacy/pre-C09 adoption safety: compatibility with a run
                    # that has no frozen scope is allowed only fail-closed.
                    # The working tree may only be analyzed as the run's
                    # historical base revision when the repository truly is
                    # still at that revision; otherwise this would silently
                    # attach a graph built from the wrong revision to the
                    # run's evidence.
                    raise RuntimeError(
                        f"cannot analyze project scope for run '{run_id}': repository "
                        f"HEAD is {base_commit!r} but the run's base_commit is "
                        f"{authoritative_base_commit!r}; refusing to build a project "
                        "graph from the wrong revision"
                    )
                try:
                    # C09-R2: close the TOCTOU window between capturing
                    # ``authoritative_base_commit`` and freezing the graph
                    # built from it -- re-check the repository both
                    # immediately before and immediately after analysis so a
                    # mid-analysis move can never be frozen under the wrong
                    # revision label.
                    scope_failure_phase = "pre_analysis_revision_guard"
                    assert_repository_at_revision(
                        repo, authoritative_base_commit, when="before project-graph analysis"
                    )
                    scope_failure_phase = "project_graph_construction"
                    project_graph = build_project_graph(
                        repo, base_revision=authoritative_base_commit
                    )
                    scope_failure_phase = "post_analysis_revision_guard"
                    assert_repository_at_revision(
                        repo, authoritative_base_commit, when="after project-graph analysis"
                    )
                    scope_failure_phase = "scope_resolution"
                    initial_scope = resolve_scope(project_graph, task)
                    scope_failure_phase = "freeze_run_scope"
                    freeze_run_scope(
                        self.db,
                        run_id,
                        repo_root=str(repo),
                        graph=project_graph,
                        scope=initial_scope,
                    )
                    self.db.record_event(
                        run_id,
                        "project_scope_frozen",
                        payload={
                            "graph_digest": project_graph.digest,
                            "graph_source": project_graph.graph_source.value,
                            "analyzer": project_graph.analyzer,
                            "confidence": project_graph.confidence.value,
                            "scope_digest": initial_scope.digest,
                            "primary_module": initial_scope.primary_module,
                            "included_modules": list(initial_scope.included_modules),
                        },
                    )
                    scope_bundle = load_scope_bundle(self.db, run_id)
                except Exception as exc:  # noqa: BLE001 - fail the run, never guess
                    raise RuntimeError(f"project scope analysis failed: {exc}") from exc
            assert scope_bundle is not None
        except Exception as exc:  # noqa: BLE001 - terminalize, never orphan RUNNING
            self._fail_run_for_project_scope_error(
                run_id, phase=scope_failure_phase, exc=exc, base_commit=base_commit
            )
            raise
        # Record routing snapshot evidence
        self.db.record_event(
            run_id,
            "routing_snapshot",
            payload=routing_payload,
        )
        self.db.record_event(
            run_id,
            "run_started",
            payload={
                "task": task,
                "base_commit": base_commit,
                "config": config.as_dict(),
                "routing_snapshot": routing_payload,
            },
        )
        control_plane_policy = frozen_control_plane.policy
        self.db.record_event(
            run_id,
            "control_plane_snapshot",
            payload=frozen_control_plane.as_dict(),
        )
        base_wt_dir = self.worktree_base or GitManager.get_default_worktree_base(repo)
        attempts: list[Attempt] = []
        checks: list[CheckResult] = []
        reviews: list[ReviewResult] = []

        active_attempt_id: str | None = None
        # A plan is persisted for genuinely broad objectives. Ordinary tasks
        # retain the standard lifecycle/events and do not incur planning model
        # calls; the deterministic sizing assessment never reads adapters.
        plan_steps: list[Any] = []
        sizing, package_proposals = resolve_work_packages(task)
        if sizing.should_slice:
            persisted = persist_plan(
                self.db,
                run_id,
                task,
                sizing,
                package_proposals,
                base_commit,
            )
            plan_steps = list(persisted.steps)
            # C09: attach the deterministic module scope to each work package
            # so package workers receive their own bounded [PROJECT SCOPE].
            assert scope_bundle is not None
            for package in persisted.packages:
                package_scope = resolve_package_scope(
                    scope_bundle.graph, package.goal, package.relevant_paths
                )
                self.db.set_work_package_scope(
                    package.id,
                    json.dumps(package_scope.to_dict(), sort_keys=True, separators=(",", ":")),
                )
        # Each successful package becomes the only permitted start point for
        # its dependent.  The ordinary, no-plan path retains the base commit.
        lineage_base_sha = base_commit

        initial_step = next(
            (
                step
                for step in self.db.get_plan_steps(run_id)
                if step.status in (PlanStepStatus.READY, PlanStepStatus.RUNNING)
            ),
            None,
        )
        initial_package = (
            self.db.get_work_package(initial_step.work_package_id)
            if initial_step is not None and initial_step.work_package_id
            else None
        )
        tier_decision: TierDecision = choose_tier(
            config,
            stage=DispatchStage.IMPLEMENTATION,
            package=initial_package,
        )
        current_tier = tier_decision.effective_tier
        self.db.update_run_tier(run_id, current_tier)
        self.db.record_event(run_id, "tier_decision", payload=tier_decision.as_payload())
        if config.routing_mode == "auto":
            self.db.record_event(
                run_id,
                "tier_classified",
                payload={
                    "chosen": current_tier.value,
                    "package_id": tier_decision.package_id,
                    "source": tier_decision.source,
                    "signals": list(tier_decision.signals),
                },
            )

        try:
            candidate_idx = 0
            attempt_number = 0
            stage_attempts = 0
            package_dispatch_ordinal = 0
            # C15-XEC: the bounded Orchestrator model has exactly ONE
            # semantic role in a normal run -- initial objective
            # interpretation / execution guidance.  It is invoked at
            # most once per run; the cache is consumed by the first
            # dispatch projection.
            initial_orchestrator_guidance = _InitialOrchestratorGuidanceCache()
            # Explicit escalation-evidence flag: True only when an actually
            # dispatched attempt produced a genuine capability-bearing
            # outcome (a worker FAILED status, or a gate failure on
            # real candidate changes) at the CURRENT tier. no_changes,
            # TIMEOUT, and deterministic pre-dispatch skips (availability /
            # quota / training policy / prompt_exceeds_transport_capability)
            # never set this -- exhausting the candidate ladder on those
            # alone is not evidence the task exceeds this tier's capability,
            # and must never by itself justify escalating to a costlier
            # tier. Reset alongside stage_attempts wherever a fresh
            # attempt-budget cycle begins (tier escalation, next package).
            tier_has_escalation_evidence = False
            escalation_outcome: DispatchOutcome | None = None
            escalation_stage: DispatchStage | None = None
            # C04: True only when the just-completed tier escalation is a
            # GENUINE capability-bearing escalation (a real validation
            # failure on actual candidate changes).  Reset alongside
            # tier_has_escalation_evidence wherever a fresh attempt-budget
            # cycle begins.  Used to select SURGEON context semantics for
            # the next dispatch only.
            surgeon_active: bool = False
            quota_states = self.db.list_quota()
            base_candidates = resolve_candidate_chain(
                tier=current_tier,
                training_allowed=config.training_allowed,
                is_peak=is_peak,
                model_override=config.model_override,
                provider_override=config.provider_override,
                snapshot=routing_snapshot,
            )
            # C07 evaluates quota at the same pre-dispatch boundary as every
            # other hard control-plane filter, so excluded candidates still
            # produce typed decision evidence and cannot be resurrected by
            # ranking or overrides.
            candidates = list(base_candidates)
            self.db.record_event(
                run_id,
                "tier_candidates_resolved",
                payload={
                    "tier": current_tier.value,
                    "candidates": [
                        {"provider": c.provider, "model": c.model} for c in base_candidates
                    ],
                    "effective_candidates": [
                        {"provider": c.provider, "model": c.model} for c in candidates
                    ],
                },
            )

            # Check existing durable attempts/checkpoints for run continuation / recovery
            existing_side_effects = self.db.get_side_effects_for_run(run_id)
            started_tool_invocations = [
                se
                for se in existing_side_effects
                if se.operation == SideEffectOperation.TOOL_INVOCATION
                and se.state == SideEffectState.STARTED
            ]
            if started_tool_invocations:
                reason = (
                    "Recovery blocked: a tool invocation was STARTED before its "
                    "owner was lost; its external outcome is unresolved"
                )
                for side_effect in started_tool_invocations:
                    self.db.journal_record_uncertain(side_effect.idempotency_key, reason)
                self.db.record_event(
                    run_id,
                    "recovery_blocked_started_tool_invocation",
                    payload={
                        "operation": SideEffectOperation.TOOL_INVOCATION.value,
                        "idempotency_keys": [
                            side_effect.idempotency_key for side_effect in started_tool_invocations
                        ],
                        "reason": reason,
                    },
                )
                raise RuntimeError(reason)
            uncertain_se = [
                se for se in existing_side_effects if se.state == SideEffectState.UNCERTAIN
            ]
            if uncertain_se:
                msg = f"Recovery blocked: replacement launch outcome is uncertain for run {run_id}"
                self.db.record_event(
                    run_id,
                    "recovery_blocked_uncertain_side_effect",
                    payload={
                        "operation": uncertain_se[0].operation.value,
                        "reason": msg,
                    },
                )
                raise RuntimeError(msg)

            existing_attempts = self.db.get_attempts_for_run(run_id)
            if existing_attempts:
                attempts.extend(existing_attempts)
                attempt_number = len(attempts)

            active_continuation_checkpoint: WorkerCheckpoint | None = None
            active_continuation_reason: str | None = None
            active_continuation_dispatch_reason: DispatchReason | None = None
            active_continuation_attempt: Attempt | None = None
            blocked_providers_in_cycle: set[str] = set()

            # Strict positive-evidence recovery gate.  C05 cold-takeover must
            # only mutate persisted state when the strict predicate passes; an
            # ordinary ``run`` against a RUNNING attempt without positive
            # evidence falls through cleanly without checkpoint mutation.
            recovery_predicate_passes = False
            if (
                adoption_mode == "recovery_only"
                and existing_attempts
                and existing_attempts[-1].status == AttemptStatus.RUNNING
            ):
                from saberops.supervisor import RunSupervisor

                predicate = RunSupervisor(
                    db=self.db, worktree_base=self.worktree_base
                ).assess_recovery_eligibility(
                    self.db.get_run(run_id)  # type: ignore[arg-type]
                )
                if not predicate.eligible:
                    raise RuntimeError(
                        f"Cannot recover run '{run_id}': "
                        f"no positive recovery evidence ({predicate.reason})"
                    )
                recovery_predicate_passes = True

            # If the most recent attempt is in RUNNING status AND the strict
            # recovery predicate passes, a previous execution owner crashed.
            # Perform cold recovery of partial edits / existing checkpoint.
            if (
                recovery_predicate_passes
                and existing_attempts
                and existing_attempts[-1].status == AttemptStatus.RUNNING
            ):
                crashed_att = existing_attempts[-1]
                wt_path = Path(crashed_att.worktree_path)
                cp_key = f"checkpoint:{run_id}:{crashed_att.id}"
                crashed_base_sha = crashed_att.base_sha or lineage_base_sha
                recovered_cp: WorkerCheckpoint | None = None

                if wt_path.exists():
                    current_head_res = run_process(["git", "rev-parse", "HEAD"], cwd=wt_path)
                    current_head = (
                        current_head_res.stdout.strip() if current_head_res.passed else None
                    )
                    if (
                        current_head
                        and current_head != crashed_base_sha
                        and GitManager.is_ancestor(wt_path, crashed_base_sha, current_head)
                    ):
                        # A checkpoint commit was already created before the crash
                        changed_between = GitManager.get_changed_paths_between(
                            wt_path, crashed_base_sha, current_head
                        )
                        existing_cp = self.db.get_worker_checkpoint_by_sha(run_id, current_head)
                        if existing_cp is None:
                            cp_id = f"cp_{uuid.uuid4().hex[:12]}"
                            recovered_cp = WorkerCheckpoint(
                                id=cp_id,
                                run_id=run_id,
                                source_attempt_id=crashed_att.id,
                                source_base_sha=crashed_base_sha,
                                checkpoint_sha=current_head,
                                reason=WorkerInterruptionReason.ABORTED.value,
                                changed_paths=tuple(changed_between),
                                created_at=current_iso_timestamp(),
                            )
                            self.db.create_worker_checkpoint(recovered_cp)
                            self.db.journal_record_completed(
                                idempotency_key=cp_key,
                                result={"checkpoint_sha": current_head, "checkpoint_id": cp_id},
                            )
                        else:
                            recovered_cp = existing_cp
                    elif GitManager.has_diff_against_base(wt_path, crashed_base_sha):
                        # Uncommitted partial edits exist in worktree
                        cold_changed_paths = GitManager.get_changed_paths(wt_path)
                        self.db.journal_record_intent(
                            idempotency_key=cp_key,
                            run_id=run_id,
                            attempt_id=crashed_att.id,
                            operation=SideEffectOperation.CHECKPOINT_CREATION,
                            payload={"cold_takeover": True},
                        )
                        cp_sha = GitManager.create_checkpoint_commit(
                            wt_path, f"orch: checkpoint for crashed attempt {crashed_att.id}"
                        )
                        if cp_sha:
                            cp_id = f"cp_{uuid.uuid4().hex[:12]}"
                            recovered_cp = WorkerCheckpoint(
                                id=cp_id,
                                run_id=run_id,
                                source_attempt_id=crashed_att.id,
                                source_base_sha=crashed_base_sha,
                                checkpoint_sha=cp_sha,
                                reason=WorkerInterruptionReason.ABORTED.value,
                                changed_paths=tuple(cold_changed_paths),
                                created_at=current_iso_timestamp(),
                            )
                            self.db.create_worker_checkpoint(recovered_cp)
                            self.db.journal_record_completed(
                                idempotency_key=cp_key,
                                result={"checkpoint_sha": cp_sha, "checkpoint_id": cp_id},
                            )
                            self.db.record_event(
                                run_id,
                                "worker_interrupted_checkpoint_created",
                                attempt_id=crashed_att.id,
                                payload={
                                    "checkpoint_sha": cp_sha,
                                    "reason": WorkerInterruptionReason.ABORTED.value,
                                    "changed_paths": cold_changed_paths,
                                    "cold_takeover": True,
                                },
                            )

                # Close the crashed attempt
                now_abort = current_iso_timestamp()
                crashed_att.status = AttemptStatus.FAILED
                crashed_att.worker_error = "Process terminated unexpectedly (cold takeover)"
                crashed_att.completed_at = now_abort
                crashed_att.interruption_reason = WorkerInterruptionReason.ABORTED.value
                self.db.update_attempt(
                    attempt_id=crashed_att.id,
                    status=AttemptStatus.FAILED,
                    worker_error=crashed_att.worker_error,
                    completed_at=now_abort,
                    interruption_reason=WorkerInterruptionReason.ABORTED.value,
                )
                self.db.update_worker_activation_state(
                    run_id,
                    crashed_att.attempt_number,
                    (
                        WorkerActivationState.CHECKPOINTED
                        if recovered_cp
                        else WorkerActivationState.TERMINATED
                    ),
                )
                self.db.record_event(
                    run_id,
                    "attempt_recovered_cold_takeover",
                    attempt_id=crashed_att.id,
                    payload={
                        "checkpoint_sha": recovered_cp.checkpoint_sha if recovered_cp else None,
                        "reason": WorkerInterruptionReason.ABORTED.value,
                    },
                )
                for source_dispatch in self.db.get_dispatch_evidence_for_run(run_id):
                    if source_dispatch.attempt_id != crashed_att.id:
                        continue
                    source_dispatch.interruption_reason = WorkerInterruptionReason.ABORTED.value
                    source_dispatch.interruption_timestamp = now_abort
                    source_dispatch.completed_at = now_abort
                    if source_dispatch.dispatch_outcome is None:
                        source_dispatch.dispatch_outcome = (
                            DispatchOutcome.PROVIDER_OR_RUNTIME_FAILURE.value
                        )
                    self.db.update_dispatch_evidence(source_dispatch)
                if recovered_cp:
                    active_continuation_checkpoint = recovered_cp
                    active_continuation_reason = WorkerInterruptionReason.ABORTED.value
                    active_continuation_attempt = crashed_att
                    active_continuation_dispatch_reason = DispatchReason.COLD_TAKEOVER

            cold_takeover_attempt_ids = {
                event.attempt_id
                for event in self.db.get_complete_events_for_run(run_id)
                if event.event_type == "attempt_recovered_cold_takeover"
                and event.attempt_id is not None
            }
            latest_cp = self.db.get_latest_worker_checkpoint(run_id)
            if active_continuation_checkpoint is None and latest_cp is not None:
                active_continuation_checkpoint = latest_cp
                active_continuation_reason = latest_cp.reason
                active_continuation_attempt = self.db.get_attempt(latest_cp.source_attempt_id)
                if (
                    active_continuation_reason == WorkerInterruptionReason.ABORTED.value
                    and latest_cp.source_attempt_id in cold_takeover_attempt_ids
                ):
                    active_continuation_dispatch_reason = DispatchReason.COLD_TAKEOVER
                if (
                    active_continuation_reason == WorkerInterruptionReason.QUOTA_EXHAUSTED.value
                    and active_continuation_attempt is not None
                ):
                    blocked_providers_in_cycle.add(active_continuation_attempt.provider)
            elif (
                active_continuation_checkpoint is None
                and existing_attempts
                and existing_attempts[-1].interruption_reason
            ):
                last_att = existing_attempts[-1]
                active_continuation_reason = last_att.interruption_reason
                active_continuation_attempt = last_att
                if (
                    active_continuation_reason == WorkerInterruptionReason.ABORTED.value
                    and last_att.id in cold_takeover_attempt_ids
                ):
                    active_continuation_dispatch_reason = DispatchReason.COLD_TAKEOVER
                if active_continuation_reason == WorkerInterruptionReason.QUOTA_EXHAUSTED.value:
                    blocked_providers_in_cycle.add(last_att.provider)

            while True:
                current_plan_step = next(
                    (
                        step
                        for step in self.db.get_plan_steps(run_id)
                        if step.status in (PlanStepStatus.READY, PlanStepStatus.RUNNING)
                    ),
                    None,
                )
                current_package = (
                    self.db.get_work_package(current_plan_step.work_package_id)
                    if current_plan_step is not None and current_plan_step.work_package_id
                    else None
                )
                # The candidate-ladder exhaustion check below applies to
                # ordinary mode only.  Under autonomous supervision the
                # supervisor is the authority that decides whether a same-
                # binding retry, a failover to a fresh binding, or a STOP is
                # the right next move -- it must never be preempted by
                # candidate-ladder bookkeeping.  We therefore consult
                # ``supervisor`` first when present; only ordinary mode
                # bails out on ``candidate_idx >= len(candidates)``.
                if stage_attempts >= config.max_attempts or (
                    config.model_override is None
                    and candidate_idx >= len(candidates)
                    and supervisor is None
                ):
                    tier_order = (Tier.T1, Tier.T2, Tier.T3)
                    ceiling_allows_escalation = (
                        config.routing_mode == "auto"
                        and config.model_override is None
                        and tier_order.index(current_tier) < tier_order.index(config.max_auto_tier)
                    )
                    # Exhausting the attempt budget or candidate ladder is
                    # necessary but not sufficient to escalate: it must also
                    # carry explicit escalation evidence (a genuine
                    # capability-bearing failure at this tier). Exhaustion
                    # made up entirely of no_changes, TIMEOUT, and/or
                    # deterministic pre-dispatch skips is not evidence the
                    # task exceeds this tier -- escalating on that alone
                    # would misrepresent transport/availability/quota/policy
                    # skips as model capability evidence.
                    may_escalate = (
                        ceiling_allows_escalation
                        and tier_has_escalation_evidence
                        and escalation_outcome is not None
                        and escalation_stage is not None
                    )
                    if may_escalate and supervisor is not None:
                        # Autonomous escalation is a typed supervisor
                        # decision, not an automatic ladder step: the
                        # persisted owner policy decides (SUPERVISED
                        # forbids it).  Withheld escalation is recorded
                        # distinctly and the stage terminates cleanly.
                        if not supervisor.allows_escalation:
                            may_escalate = False
                            self.db.record_event(
                                run_id,
                                "autonomous_escalation_withheld",
                                payload={
                                    "tier": current_tier.value,
                                    "reason": "persisted_authority_forbids_escalation",
                                },
                            )
                    if not may_escalate:
                        if (
                            config.model_override is None
                            and candidate_idx >= len(candidates)
                            and stage_attempts == 0
                        ):
                            self.db.record_event(
                                run_id,
                                "no_eligible_candidate",
                                payload={
                                    "outcome": NO_ELIGIBLE_CANDIDATE,
                                    "tier": current_tier.value,
                                    "candidates": [
                                        {"provider": c.provider, "model": c.model}
                                        for c in candidates
                                    ],
                                },
                            )
                        if ceiling_allows_escalation and not tier_has_escalation_evidence:
                            # The ceiling and routing mode would otherwise
                            # have allowed escalation; it is withheld here
                            # specifically for lack of evidence. Record that
                            # distinctly so the stage's clean termination is
                            # never confused with a normal ceiling/manual-mode
                            # exhaustion. The outer "all attempts exhausted"
                            # handling below still reports the terminal
                            # run_failed summary; this event exists so the
                            # reason is inspectable, not just the outcome.
                            self.db.record_event(
                                run_id,
                                "stage_exhausted_without_escalation_evidence",
                                payload={
                                    "tier": current_tier.value,
                                    "stage_attempts": stage_attempts,
                                    "candidates_tried": candidate_idx,
                                },
                            )
                        break
                    prior_tier = current_tier
                    current_tier = tier_order[tier_order.index(current_tier) + 1]
                    self.db.update_run_tier(run_id, current_tier)
                    assert escalation_outcome is not None
                    assert escalation_stage is not None
                    tier_decision = escalated_decision(
                        tier_decision,
                        current_tier,
                        escalation_outcome,
                        stage=escalation_stage,
                    )
                    # C04: a genuine capability-bearing tier escalation may
                    # activate SURGEON context semantics for the next
                    # dispatch.  Provider / quota / infrastructure / timeout
                    # / mutation / no_changes / already_satisfied escalations
                    # never set this flag -- those continue to use the
                    # canonical role_for_stage() mapping.  See
                    # ``_is_genuine_capability_escalation``.
                    surgeon_active = _is_genuine_capability_escalation(escalation_outcome)
                    self.db.record_event(
                        run_id,
                        "tier_escalated",
                        payload={
                            "from": prior_tier.value,
                            "to": current_tier.value,
                            "stage": tier_decision.stage.value,
                            "package_id": tier_decision.package_id,
                            "escalation_reason": escalation_outcome.value,
                        },
                    )
                    self.db.record_event(
                        run_id, "tier_decision", payload=tier_decision.as_payload()
                    )
                    quota_states = self.db.list_quota()
                    base_candidates = resolve_candidate_chain(
                        tier=current_tier,
                        training_allowed=config.training_allowed,
                        is_peak=is_peak,
                        provider_override=config.provider_override,
                        snapshot=routing_snapshot,
                    )
                    candidates = list(base_candidates)
                    self.db.record_event(
                        run_id,
                        "tier_candidates_resolved",
                        payload={
                            "tier": current_tier.value,
                            "candidates": [
                                {"provider": c.provider, "model": c.model} for c in base_candidates
                            ],
                            "effective_candidates": [
                                {"provider": c.provider, "model": c.model} for c in candidates
                            ],
                        },
                    )
                    candidate_idx = 0
                    stage_attempts = 0
                    tier_has_escalation_evidence = False
                    escalation_outcome = None
                    escalation_stage = None
                    # C04: the next dispatch after a genuine escalation is a
                    # SURGEON context.  After resetting the attempt-budget
                    # cycle we must also re-evaluate whether the next
                    # dispatch is a SURGEON (set above when the escalation
                    # was genuine) or whether it should fall back to the
                    # canonical role_for_stage() mapping.
                    continue
                auto_pinned_candidate = None
                auto_decision_effort: str | None = None
                # C11-D exact executable binding: the resolved runtime
                # value (never the unexecuted decision) that both the
                # semantic invocation and the dispatch evidence below
                # are sourced from.  ``None`` on the ordinary
                # (unsupervised) path, which preserves legacy behavior.
                auto_resolved: ResolvedExecutionBinding | None = None
                # C11-D failover start override: Attempt B starts at the
                # validated required-base anchor (or its proven
                # checkpoint continuation), never at the run base or
                # the advanced lineage.
                auto_failover_start: str | None = None
                if supervisor is not None:
                    # C11-D canonical seam: the autonomous supervisor names
                    # the exact eligible binding (or STOP) from durable
                    # state; the canonical lifecycle below performs it.
                    # Production consumes decide() outputs only; the legacy
                    # drive method is never invoked here.
                    def _autonomous_stop(stop_summary: str, stop_reason: str) -> RunResult:
                        """Terminalize the run for a supervisor STOP (fail closed)."""
                        now_stop = current_iso_timestamp()
                        self.db.update_run(
                            run_id=run_id,
                            status=RunStatus.FAILED,
                            completed_at=now_stop,
                            result_summary=stop_summary,
                        )
                        self.db.record_event(
                            run_id,
                            "run_failed",
                            payload={
                                "summary": stop_summary,
                                "stop_reason": stop_reason,
                            },
                        )
                        return RunResult(
                            run_id=run_id,
                            passed=False,
                            status=RunStatus.FAILED,
                            summary=stop_summary,
                            target_repo=str(repo),
                            base_commit=base_commit,
                            attempts=attempts,
                            checks=checks,
                            reviews=reviews,
                        )

                    auto_decision = supervisor.decide(run_id, candidates=candidates)
                    self.db.record_event(
                        run_id,
                        "autonomous_supervisor_decision",
                        payload=auto_decision.as_dict(),
                    )
                    if auto_decision.action is not SupervisorAction.STOP:
                        pinned = [
                            c
                            for c in candidates
                            if c.provider == auto_decision.binding_provider
                            and c.model == auto_decision.binding_model
                        ]
                        if not pinned:
                            self.db.record_event(
                                run_id,
                                "autonomous_binding_ineligible",
                                payload={
                                    **auto_decision.as_dict(),
                                    "reason": "decision_binding_not_on_eligible_ladder",
                                },
                            )
                            return _autonomous_stop(
                                "Autonomous supervisor decision names "
                                f"{auto_decision.binding_provider}/"
                                f"{auto_decision.binding_model}, which is not "
                                "on the eligible candidate ladder. "
                                "Failing closed without launch.",
                                SupervisorStopReason.NO_ELIGIBLE_BINDING.value,
                            )
                        # Pin the exact decided binding WITHOUT disturbing
                        # ladder bookkeeping: the ladder still bounds total
                        # iterations; the decision only overrides which
                        # entry dispatches this round.  The decision's
                        # binding id and resolved effort are consumed by
                        # the exact executable-binding resolution below,
                        # which alone sources invocation and evidence.
                        auto_pinned_candidate = pinned[0]
                        auto_decision_effort = auto_decision.reasoning_effort
                    else:
                        auto_stop_reason = (
                            auto_decision.stop_reason.value
                            if auto_decision.stop_reason is not None
                            else "STOP"
                        )
                        return _autonomous_stop(
                            "Autonomous supervisor STOP "
                            f"({auto_stop_reason}): "
                            f"{auto_decision.detail or 'no further dispatch'}",
                            auto_stop_reason,
                        )
                if auto_pinned_candidate is not None:
                    # Typed supervisor override: dispatch the exact decided
                    # eligible binding this round.  When the autonomous
                    # supervisor names a binding (including a same-binding
                    # retry whose original ladder index is exhausted), that
                    # binding is dispatched regardless of ``candidate_idx``
                    # bookkeeping.  The ordinary ladder still bounds total
                    # iterations through ``stage_attempts`` /
                    # ``max_attempts``; the supervisor decision only
                    # overrides which entry dispatches this round and must
                    # never be preempted by candidate-ladder exhaustion.
                    candidate = auto_pinned_candidate
                elif config.model_override is not None:
                    candidate = candidates[0]
                else:
                    while (
                        candidate_idx < len(candidates)
                        and candidates[candidate_idx].provider in blocked_providers_in_cycle
                    ):
                        blocked_cand = candidates[candidate_idx]
                        candidate_idx += 1
                        self.db.record_event(
                            run_id,
                            "worker_candidate_skipped",
                            payload={
                                "provider": blocked_cand.provider,
                                "model": blocked_cand.model,
                                "reason": "quota_blocked",
                                "outcome": DispatchOutcome.ELIGIBILITY_SKIP.value,
                            },
                        )
                    if candidate_idx >= len(candidates):
                        if stage_attempts == 0:
                            self.db.record_event(
                                run_id,
                                "no_eligible_candidate",
                                payload={
                                    "outcome": NO_ELIGIBLE_CANDIDATE,
                                    "tier": current_tier.value,
                                    "candidates": [
                                        {"provider": c.provider, "model": c.model}
                                        for c in candidates
                                    ],
                                },
                            )
                        stage_attempts = config.max_attempts
                        continue
                    candidate = candidates[candidate_idx]
                    candidate_idx += 1

                if supervisor is None:
                    # C15-XB-01 exact worker binding for the ordinary
                    # owner-facing dispatch path.  An ordinary routing
                    # candidate may carry an owner-approved ``binding_id``
                    # from the frozen routing snapshot.  A non-empty
                    # ``binding_id`` is authoritative: it resolves to
                    # exactly that WORKER-role binding and its execution
                    # identity (binding/profile/pool/backend/effort) rides
                    # the adapter request and durable evidence below,
                    # never a provider/model first match.  A candidate
                    # with no ``binding_id`` keeps the historical
                    # provider/model behavior unchanged.
                    ordinary_binding_id = getattr(candidate, "binding_id", None)
                    if ordinary_binding_id and ordinary_binding_id.strip():
                        try:
                            auto_resolved = resolve_worker_candidate_binding(
                                registry=frozen_worker_binding_registry,
                                candidate_binding_id=ordinary_binding_id,
                                provider=candidate.provider,
                                model=candidate.model,
                                adapter=self.registry.get(candidate.provider),
                            )
                        except BindingResolutionError as exc:
                            refusal_payload: dict[str, Any] = {
                                "provider": candidate.provider,
                                "model": candidate.model,
                                "binding_id": ordinary_binding_id,
                                "reason": exc.reason,
                                "outcome": DispatchOutcome.ELIGIBILITY_SKIP.value,
                                "tier": current_tier.value,
                                "routing_snapshot_digest": (routing_snapshot.content_hash),
                            }
                            self.db.record_event(
                                run_id,
                                "exact_binding_unresolvable",
                                payload=refusal_payload,
                            )
                            self.db.record_event(
                                run_id,
                                "worker_candidate_skipped",
                                payload=refusal_payload,
                            )
                            if config.model_override is not None:
                                msg = (
                                    "explicit model is ineligible: exact "
                                    f"worker binding {ordinary_binding_id!r} "
                                    f"does not resolve ({exc.reason})"
                                )
                                now_fail = current_iso_timestamp()
                                self.db.update_run(
                                    run_id=run_id,
                                    status=RunStatus.FAILED,
                                    completed_at=now_fail,
                                    result_summary=msg,
                                )
                                self.db.record_event(
                                    run_id,
                                    "run_failed",
                                    payload={
                                        "summary": msg,
                                        "reason": exc.reason,
                                    },
                                )
                                return RunResult(
                                    run_id=run_id,
                                    passed=False,
                                    status=RunStatus.FAILED,
                                    summary=msg,
                                    target_repo=str(repo),
                                    base_commit=base_commit,
                                    attempts=attempts,
                                    checks=checks,
                                    reviews=reviews,
                                )
                            continue

                if supervisor is not None:
                    # C11-D: resolve the exact executable binding BEFORE
                    # any worktree/attempt/evidence exists.  The decision
                    # alone never executes: resolution through the
                    # authoritative BindingRegistry is what the runtime
                    # invokes and what the evidence records.  Any
                    # resolution failure stops with zero semantic launch
                    # and no evidence claiming the binding executed.
                    if not auto_decision_effort:
                        self.db.record_event(
                            run_id,
                            "autonomous_binding_unresolvable",
                            payload={
                                **auto_decision.as_dict(),
                                "reason": "EFFORT_NOT_IN_BINDING",
                            },
                        )
                        return _autonomous_stop(
                            "Autonomous supervisor decision carries no "
                            "resolved effort tier; failing closed without "
                            "launch.",
                            SupervisorStopReason.NO_ELIGIBLE_BINDING.value,
                        )
                    try:
                        auto_effort_tier = ReasoningEffort(auto_decision_effort)
                    except ValueError:
                        self.db.record_event(
                            run_id,
                            "autonomous_binding_unresolvable",
                            payload={
                                **auto_decision.as_dict(),
                                "reason": "EFFORT_NOT_IN_BINDING",
                            },
                        )
                        return _autonomous_stop(
                            "Autonomous supervisor decision carries an "
                            f"unknown effort tier {auto_decision_effort!r}; "
                            "failing closed without launch.",
                            SupervisorStopReason.NO_ELIGIBLE_BINDING.value,
                        )
                    try:
                        auto_resolved = resolve_execution_binding(
                            registry=getattr(supervisor, "binding_registry", None),
                            binding_id=auto_decision.binding_id,
                            adapter=self.registry.get(candidate.provider),
                            reasoning_effort=auto_effort_tier,
                        )
                    except BindingResolutionError as exc:
                        self.db.record_event(
                            run_id,
                            "autonomous_binding_unresolvable",
                            payload={
                                **auto_decision.as_dict(),
                                "reason": exc.reason,
                            },
                        )
                        return _autonomous_stop(
                            "Autonomous supervisor decision names binding "
                            f"{auto_decision.binding_id!r} which does not "
                            f"resolve to executable runtime ({exc.reason}); "
                            "zero launch.",
                            SupervisorStopReason.NO_ELIGIBLE_BINDING.value,
                        )
                    # C11-D: failover required-base validation.  The
                    # anchor is Attempt A's actual base (never
                    # run.base_commit).  Without a checkpoint the next
                    # Attempt starts exactly at the anchor; with an
                    # admissible checkpoint proven to derive from the
                    # anchor it starts at the checkpoint while the
                    # decision retains the anchor.  Repair lineage is
                    # untouched: this governs NEW ATTEMPT retry/failover
                    # only, and repair never flows through decisions.
                    if auto_decision.failover_from_attempt_id is not None:
                        required_base = auto_decision.required_base_sha
                        source_attempt = self.db.get_attempt(auto_decision.failover_from_attempt_id)
                        if (
                            not required_base
                            or source_attempt is None
                            or source_attempt.base_sha != required_base
                        ):
                            self.db.record_event(
                                run_id,
                                "autonomous_failover_base_rejected",
                                payload={
                                    **auto_decision.as_dict(),
                                    "source_base_sha": (
                                        source_attempt.base_sha
                                        if source_attempt is not None
                                        else None
                                    ),
                                },
                            )
                            return _autonomous_stop(
                                "Autonomous failover required-base anchor "
                                f"{required_base!r} does not match the "
                                "source Attempt's actual base; zero "
                                "Attempt B.",
                                SupervisorStopReason.BLOCKED_ESCALATE.value,
                            )
                        continuation = active_continuation_checkpoint
                        if continuation is not None:
                            derives = (
                                continuation.checkpoint_sha == required_base
                                or continuation.source_base_sha == required_base
                            ) and GitManager.is_ancestor(
                                repo, required_base, continuation.checkpoint_sha
                            )
                            if not derives:
                                self.db.record_event(
                                    run_id,
                                    "autonomous_failover_base_rejected",
                                    payload={
                                        **auto_decision.as_dict(),
                                        "checkpoint_sha": (continuation.checkpoint_sha),
                                        "checkpoint_source_base_sha": (
                                            continuation.source_base_sha
                                        ),
                                    },
                                )
                                return _autonomous_stop(
                                    "Autonomous failover checkpoint "
                                    f"{continuation.checkpoint_sha!r} "
                                    "cannot be proven to derive from the "
                                    f"required-base anchor {required_base!r}; "
                                    "zero Attempt B.",
                                    SupervisorStopReason.BLOCKED_ESCALATE.value,
                                )
                            auto_failover_start = continuation.checkpoint_sha
                        else:
                            auto_failover_start = required_base
                        # Durable authorization of the exact start: the
                        # anchor alone authorizes Attempt B, and a proven
                        # checkpoint continuation additionally
                        # authorizes its start SHA.  Publication and
                        # acceptance re-verify this record live before
                        # treating a non-run-base candidate as the
                        # run's own.
                        self.db.record_event(
                            run_id,
                            "autonomous_failover_start_authorized",
                            payload={
                                "failover_from_attempt_id": (
                                    auto_decision.failover_from_attempt_id
                                ),
                                "required_base_sha": required_base,
                                "start_sha": auto_failover_start,
                                "checkpoint_sha": (
                                    continuation.checkpoint_sha
                                    if continuation is not None
                                    else None
                                ),
                            },
                        )

                # An exact GATEWAY binding must never be allowed to reach
                # the ordinary boundary fallback path.  Reject static
                # gateway unsatisfiability before worktree/Attempt creation;
                # live health and quota observations remain boundary-owned.
                if supervisor is not None and auto_resolved is not None:
                    if auto_resolved.backend is BindingKind.GATEWAY:
                        gateway_adapter = self.registry.get(candidate.provider)
                        gateway_reason: str | None = None
                        if frozen_gateway is None or not frozen_gateway.enabled:
                            gateway_reason = "gateway_binding_without_enabled_gateway"
                        elif not frozen_gateway.endpoint:
                            gateway_reason = "gateway_binding_without_endpoint"
                        elif not bool(
                            getattr(
                                gateway_adapter,
                                "c10_gateway_transport_capabilities",
                                frozenset(),
                            )
                        ):
                            gateway_reason = "adapter_gateway_unsupported"
                        if gateway_reason is not None:
                            self.db.record_event(
                                run_id,
                                "autonomous_gateway_binding_unsatisfied",
                                payload={
                                    **auto_decision.as_dict(),
                                    "reason": gateway_reason,
                                },
                            )
                            return _autonomous_stop(
                                "Autonomous GATEWAY binding cannot be satisfied "
                                f"before launch ({gateway_reason}); zero launch.",
                                SupervisorStopReason.NO_ELIGIBLE_BINDING.value,
                            )

                # Assemble final prompt BEFORE creating attempt/worktree for eligibility preflight.
                #
                # C04-R1: ONE worker dispatch -> ONE context projection ->
                # ONE rendered worker contract.  No composition of an INITIAL
                # / REPAIR contract with a separate PACKAGE contract; the
                # package branch selects ONE PACKAGE projection directly.  The
                # durable ``context_projected`` event describes the FINAL
                # model-visible prompt and carries the source parent byte
                # count for downstream metrics.
                is_explicit = config.model_override is not None
                next_attempt_num = attempt_number + 1
                from saberops.context_projection import (
                    SurgicalCapsule,
                    build_continuation_projection,
                    build_initial_projection,
                    build_package_initial_projection,
                    build_package_retry_projection,
                    prune_evidence,
                    render_projection,
                )

                # Local computation of the stage label — it is computed later
                # in the dispatch block too, but we need it here to choose
                # the right prompt renderer.
                local_stage_label = (
                    DispatchStage.IMPLEMENTATION.value
                    if package_dispatch_ordinal == 0
                    else DispatchStage.RETRY.value
                )
                local_stage = DispatchStage(local_stage_label)

                # The one and only projection for this dispatch.
                dispatch_projection: ContextProjection | None = None
                prior_attempt: Attempt | None = None
                # Captured failure / review evidence for package retries.
                pkg_failure_excerpt: str | None = None
                pkg_evidence_truncated: bool = False

                if (
                    active_continuation_reason is not None
                    and active_continuation_attempt is not None
                ):
                    continuation_checkpoint_sha = (
                        active_continuation_checkpoint.checkpoint_sha
                        if active_continuation_checkpoint is not None
                        else lineage_base_sha
                    )
                    continuation_changed_paths = (
                        active_continuation_checkpoint.changed_paths
                        if active_continuation_checkpoint is not None
                        else ()
                    )
                    diff_summary: str | None = None
                    if active_continuation_checkpoint is not None:
                        try:
                            if (
                                active_continuation_attempt.worktree_path
                                and Path(active_continuation_attempt.worktree_path).exists()
                            ):
                                diff_summary = GitManager.get_diff(
                                    active_continuation_attempt.worktree_path,
                                    base_commit=lineage_base_sha,
                                )
                        except Exception:
                            diff_summary = None

                    dispatch_projection = build_continuation_projection(
                        original_task=task,
                        run_id=run_id,
                        attempt_idx=next_attempt_num,
                        checkpoint_sha=continuation_checkpoint_sha,
                        last_verified_candidate_sha=lineage_base_sha,
                        interruption_reason=active_continuation_reason,
                        previous_provider=active_continuation_attempt.provider,
                        previous_model=active_continuation_attempt.model,
                        changed_paths=continuation_changed_paths,
                        diff_summary=diff_summary,
                        package=current_package,
                        role=DispatchRole.REPAIR.value,
                    )
                elif current_package is not None:
                    # PACKAGE branch: ONE package projection, NEVER composed
                    # with an INITIAL/REPAIR contract.
                    if next_attempt_num > 1 and attempts:
                        # Package retry: bounded corrective evidence on the
                        # inherited candidate commit.
                        prior_attempt = attempts[-1]
                        prior_checks = self.db.get_checks_for_attempt(prior_attempt.id)
                        prior_chk = prior_checks[-1] if prior_checks else None
                        # Pull bounded gate stderr / stdout from the prior
                        # attempt's most recent gate_completed event payload.
                        bounded_failure_parts: list[str] = []
                        truncated_failure = False
                        if prior_chk is not None:
                            try:
                                for ev in self.db.get_complete_events_for_run(run_id):
                                    if (
                                        ev.event_type == "gate_completed"
                                        and ev.attempt_id == prior_attempt.id
                                        and ev.payload is not None
                                    ):
                                        gate_stderr = str(ev.payload.get("stderr") or "")
                                        gate_stdout = str(ev.payload.get("stdout") or "")
                                        if gate_stderr.strip():
                                            pe = prune_evidence(
                                                gate_stderr.strip(),
                                                limit=EVIDENCE_BLOCK_MAX_CHARS,
                                            )
                                            bounded_failure_parts.append(
                                                f"Prior gate '{prior_chk.gate_command}' failed "
                                                f"with exit code {prior_chk.exit_code}.\n"
                                                f"Gate stderr excerpt:\n{pe.text}"
                                            )
                                            truncated_failure = truncated_failure or pe.truncated
                                        elif gate_stdout.strip():
                                            pe = prune_evidence(
                                                gate_stdout.strip(),
                                                limit=EVIDENCE_BLOCK_MAX_CHARS,
                                            )
                                            bounded_failure_parts.append(
                                                f"Prior gate '{prior_chk.gate_command}' failed "
                                                f"with exit code {prior_chk.exit_code}.\n"
                                                f"Gate stdout excerpt:\n{pe.text}"
                                            )
                                            truncated_failure = truncated_failure or pe.truncated
                                        else:
                                            bounded_failure_parts.append(
                                                f"Prior gate '{prior_chk.gate_command}' failed "
                                                f"with exit code {prior_chk.exit_code}."
                                            )
                                        break
                            except Exception:
                                pass
                        if getattr(prior_attempt, "worker_error", None):
                            bounded_failure_parts.insert(
                                0,
                                f"Prior worker error: {prior_attempt.worker_error}",
                            )
                        pkg_failure_excerpt = (
                            "\n".join(bounded_failure_parts) if bounded_failure_parts else None
                        )
                        pkg_evidence_truncated = truncated_failure
                        # Review-repair path is handled separately by
                        # ``_do_repair``; this dispatch is the gate-failure
                        # retry on the same package.
                        pkg_retry_role = _role_for_orchestrator_dispatch(
                            DispatchStage.REPAIR, surgeon_active=surgeon_active
                        )
                        dispatch_projection = build_package_retry_projection(
                            original_task=task,
                            package=current_package,
                            inherited_base_sha=lineage_base_sha,
                            prior_check=prior_chk,
                            failure_excerpt=pkg_failure_excerpt,
                            evidence_truncated=pkg_evidence_truncated,
                            role=pkg_retry_role.value,
                        )
                    else:
                        # Initial package dispatch: bounded package
                        # responsibility, no raw full parent objective.
                        completed_steps = [
                            f"{step.title}: {step.goal[:240]}"
                            for step in self.db.get_plan_steps(run_id)
                            if step.status == PlanStepStatus.COMPLETED
                        ]
                        pkg_role = _role_for_orchestrator_dispatch(
                            local_stage, surgeon_active=surgeon_active
                        )
                        # C15-XEC: bounded initial-orchestrator-guidance
                        # is consumed exactly once per run.  It is
                        # passed here as an advisory ``extra_sections``
                        # entry on the FIRST package dispatch.  Worker
                        # routing / binding identity remain unchanged.
                        _pkg_guidance_text, _pkg_guidance_reason = (
                            initial_orchestrator_guidance.get_or_fetch(
                                service=self,
                                original_task=task,
                                run_id=run_id,
                            )
                        )
                        dispatch_projection = build_package_initial_projection(
                            original_task=task,
                            package=current_package,
                            completed_summaries=tuple(completed_steps),
                            inherited_base_sha=lineage_base_sha,
                            role=pkg_role.value,
                            orchestrator_guidance=(
                                _pkg_guidance_text or None
                            ),
                        )
                elif next_attempt_num > 1 and attempts:
                    # Unslciced retry (no current package).
                    prior_attempt = attempts[-1]
                    prior_checks = self.db.get_checks_for_attempt(prior_attempt.id)
                    prior_chk = prior_checks[-1] if prior_checks else None
                    if surgeon_active:
                        # C04: a SURGEON dispatch after a genuine capability
                        # escalation receives a Surgical Escalation Capsule
                        # rather than the original epic task.
                        changed_paths: tuple[str, ...] = ()
                        if prior_chk is not None:
                            try:
                                for ev in self.db.get_complete_events_for_run(run_id):
                                    if (
                                        ev.event_type == "gate_completed"
                                        and ev.attempt_id == prior_attempt.id
                                        and ev.payload is not None
                                    ):
                                        cp = ev.payload.get("changed_paths") or []
                                        changed_paths = tuple(cp)
                                        break
                            except Exception:
                                changed_paths = ()
                        bounded_stderr = (
                            prune_evidence(
                                prior_chk.stderr.strip() if prior_chk else None,
                                limit=STDERR_BLOCK_MAX_CHARS,
                            ).text
                            if prior_chk is not None
                            else None
                        )
                        bounded_stdout = (
                            prune_evidence(
                                prior_chk.stdout.strip() if prior_chk else None,
                                limit=STDOUT_BLOCK_MAX_CHARS,
                            ).text
                            if prior_chk is not None
                            else None
                        )
                        diff_ref = (
                            f"{(prior_attempt.base_sha or 'UNKNOWN')}.."
                            f"{(prior_attempt.commit_sha or 'UNKNOWN')}"
                            if prior_attempt.commit_sha
                            else None
                        )
                        surgical_capsule = SurgicalCapsule(
                            parent_slug=objective_slug_inline(task),
                            inherited_candidate_sha=(
                                lineage_base_sha or prior_attempt.base_sha or "UNKNOWN"
                            ),
                            dispatch_reason=(
                                f"SURGICAL escalation after attempt "
                                f"#{prior_attempt.attempt_number} on "
                                f"({prior_attempt.provider}/"
                                f"{prior_attempt.model})"
                            ),
                            bounded_defect=(
                                f"Resolve the residual validation failure "
                                f"from attempt "
                                f"#{prior_attempt.attempt_number}; the "
                                f"bounded objective digest is: "
                                f"{objective_digest_inline(task)}"
                            ),
                            failing_check_command=(
                                getattr(prior_chk, "gate_command", None)
                                if prior_chk is not None
                                else None
                            ),
                            failing_exit_code=(
                                getattr(prior_chk, "exit_code", None)
                                if prior_chk is not None
                                else None
                            ),
                            bounded_stdout_excerpt=bounded_stdout,
                            bounded_stderr_excerpt=bounded_stderr,
                            blocking_review_finding=(
                                prior_attempt.worker_error if prior_attempt.worker_error else None
                            ),
                            changed_paths=changed_paths[:CHANGED_PATHS_MAX],
                            diff_ref=diff_ref,
                            invariants_to_preserve=(
                                "Acceptance criteria of the inherited "
                                "candidate must remain satisfied.",
                                "Original task context (bounded digest): "
                                + objective_digest_inline(task),
                            ),
                            source_original_task_bytes=len(task.encode("utf-8")),
                        )
                        dispatch_projection = surgical_capsule.to_projection(
                            role=DispatchRole.SURGEON.value
                        )
                    else:
                        dispatch_projection = build_retry_projection(
                            original_task=task,
                            attempt_idx=next_attempt_num,
                            prior_attempt=prior_attempt,
                            prior_check=prior_chk,
                            inherited_base_sha=lineage_base_sha,
                            role=DispatchRole.REPAIR.value,
                        )
                else:
                    # Unslciced initial dispatch.
                    initial_role = _role_for_orchestrator_dispatch(
                        local_stage, surgeon_active=surgeon_active
                    )
                    # C15-XEC: bounded initial-orchestrator-guidance
                    # is consumed exactly once per run.  It is
                    # passed here as an advisory ``extra_sections``
                    # entry on the FIRST unsliced initial dispatch.
                    # Worker routing / binding identity remain
                    # unchanged.
                    _init_guidance_text, _init_guidance_reason = (
                        initial_orchestrator_guidance.get_or_fetch(
                            service=self,
                            original_task=task,
                            run_id=run_id,
                        )
                    )
                    dispatch_projection = build_initial_projection(
                        original_task=task,
                        role=initial_role.value,
                        package_id=current_package.id if current_package is not None else None,
                        orchestrator_guidance=(
                            _init_guidance_text or None
                        ),
                    )

                assert dispatch_projection is not None
                # Render ONE projection -- this is the actual prompt that
                # the worker will receive.
                with measure_phase(
                    self.db,
                    run_id=run_id,
                    phase=PhaseKind.CONTEXT_SELECTION_RENDERING,
                    source_kind="orchestrator._run_core",
                    boundary_kind="render_projection",
                    package_id=(current_package.id if current_package is not None else None),
                    notes="C14-C: rendering sub-boundary only -- time spent "
                    "inside render_projection() producing the worker prompt. "
                    "Projection construction (build_*_projection) happens "
                    "before this timing boundary and is NOT included in the "
                    "measurement.",
                ):
                    rendered = render_projection(
                        dispatch_projection,
                        validation_policy_text=VALIDATION_POLICY_TEXT,
                        validation_policy_marker=VALIDATION_POLICY_MARKER,
                        tool_contract=frozen_tools,
                        project_scope_text=scope_section_for_run(
                            self.db,
                            run_id,
                            current_package.scope_json if current_package is not None else None,
                        ),
                    )
                task_for_worker_pre = rendered.prompt
                prompt_bytes_pre = len(task_for_worker_pre.encode("utf-8"))
                adapter_pre = self.registry.get(candidate.provider)
                if self.readiness_service is not None:
                    auto_profile = (
                        auto_resolved.profile_id
                        if auto_resolved is not None
                        else ""
                    )
                    readiness_pre = self.readiness_service.admission_for(
                        provider=candidate.provider,
                        profile_id=auto_profile,
                    )
                else:
                    readiness_pre = None
                # C15-GC: governed worker dispatches against an
                # Orchestrator-managed worktree MUST have the canonical
                # project instruction contract (e.g. the scope-first
                # `python -m saberops.devscope scope` rule)
                # faithfully delivered to the worker.  C07 evaluates the
                # adapter's factual instruction-compatibility evidence at
                # the SAME pre-dispatch boundary as every other hard
                # filter, so a candidate whose transport is proven
                # incompatible (reproduced General Compute via OpenCode:
                # opaque upstream ``403 Forbidden``) is skipped before any
                # worker process is launched and Orch routing may continue
                # to the next candidate.  The adapter only reports the
                # evidence -- never chooses a replacement model or alters
                # the routing chain.
                decision_pre = control_plane_decision(
                    candidate=candidate,
                    adapter=adapter_pre,
                    training_allowed=config.training_allowed,
                    quota_states=quota_states,
                    prompt_bytes=prompt_bytes_pre,
                    reserve_override=frozen_control_plane.reserve_override,
                    policy=control_plane_policy,
                    required_capabilities=frozen_control_plane.required_capabilities,
                    control_plane_digest=frozen_control_plane.digest,
                    # C11-D: the exact binding's quota pool gates the
                    # canonical C07 evaluation (never a provider-wide
                    # default); ordinary dispatches keep legacy behavior.
                    binding_pool_id=(
                        auto_resolved.quota_pool_id if auto_resolved is not None else None
                    ),
                    requires_governed_instructions=True,
                    readiness_state=(
                        readiness_pre.state if readiness_pre is not None else None
                    ),
                    readiness_reason=(
                        readiness_pre.reason if readiness_pre is not None else None
                    ),
                    readiness_executable_available=(
                        readiness_pre.executable_available
                        if readiness_pre is not None
                        else True
                    ),
                    explicit_unknown_readiness=is_explicit,
                )
                # Keep the legacy string at this boundary for existing event
                # consumers while persisting the authoritative typed decision
                # alongside it.
                with measure_phase(
                    self.db,
                    run_id=run_id,
                    phase=PhaseKind.ROUTING_POLICY,
                    source_kind="orchestrator._run_core",
                    boundary_kind="evaluate_eligibility",
                    package_id=(current_package.id if current_package is not None else None),
                    notes="C14-C: eligibility-evaluation sub-boundary only -- "
                    "time spent inside the C07 evaluate_eligibility() call. "
                    "The preceding control_plane_decision() call happens "
                    "before this timing boundary and is NOT included in the "
                    "measurement.",
                ):
                    reason_pre = evaluate_eligibility(
                        candidate=candidate,
                        adapter=adapter_pre,
                        training_allowed=config.training_allowed,
                        quota_states=quota_states,
                        prompt_bytes=prompt_bytes_pre,
                        reserve_override=frozen_control_plane.reserve_override,
                        policy=control_plane_policy,
                        required_capabilities=frozen_control_plane.required_capabilities,
                        control_plane_digest=frozen_control_plane.digest,
                        binding_pool_id=(
                            auto_resolved.quota_pool_id if auto_resolved is not None else None
                        ),
                        requires_governed_instructions=True,
                        explicit_override=is_explicit,
                        readiness_state=(
                            readiness_pre.state
                            if readiness_pre is not None
                            else None
                        ),
                        readiness_reason=(
                            readiness_pre.reason
                            if readiness_pre is not None
                            else None
                        ),
                        readiness_executable_available=(
                            readiness_pre.executable_available
                            if readiness_pre is not None
                            else True
                        ),
                    )
                self.db.record_event(
                    run_id,
                    "control_plane_decision",
                    payload=decision_pre.as_payload(),
                )
                if reason_pre is not None:
                    transport_pre = (
                        adapter_pre.prompt_transport_for(prompt_bytes_pre)
                        if adapter_pre
                        else "unknown"
                    )
                    max_bytes_pre: int | None = None
                    try:
                        max_bytes_pre = adapter_pre.max_prompt_bytes() if adapter_pre else None
                    except Exception:
                        max_bytes_pre = None
                    payload_pre = build_skip_payload(
                        role=(
                            DispatchRole.BULK
                            if package_dispatch_ordinal == 0
                            else DispatchRole.REPAIR
                        ),
                        candidate=candidate,
                        reason=reason_pre,
                        prompt_bytes=prompt_bytes_pre,
                        max_prompt_bytes=max_bytes_pre,
                        transport=transport_pre,
                        explicit_override=is_explicit,
                    )
                    payload_pre.update(
                        {
                            "outcome": DispatchOutcome.ELIGIBILITY_SKIP.value,
                            "stage": (
                                DispatchStage.IMPLEMENTATION.value
                                if package_dispatch_ordinal == 0
                                else DispatchStage.RETRY.value
                            ),
                            "tier": current_tier.value,
                            "package_id": tier_decision.package_id,
                            "control_plane": decision_pre.as_payload(),
                        }
                    )
                    if supervisor is not None:
                        self.db.record_event(
                            run_id,
                            "autonomous_preflight_refused_after_decision",
                            payload={
                                **auto_decision.as_dict(),
                                "reason": reason_pre,
                                "control_plane": decision_pre.as_payload(),
                            },
                        )
                        self.db.record_event(
                            run_id, "worker_candidate_skipped", payload=payload_pre
                        )
                        return _autonomous_stop(
                            "Autonomous supervisor launch became ineligible at "
                            f"live preflight ({reason_pre}); stopping without "
                            "launching another candidate.",
                            SupervisorStopReason.NO_ELIGIBLE_BINDING.value,
                        )
                    if is_explicit:
                        # Fail fast without launching anything
                        self.db.record_event(
                            run_id, "worker_candidate_skipped", payload=payload_pre
                        )
                        if reason_pre == "prompt_exceeds_transport_capability":
                            msg = (
                                f"explicit model is ineligible: prompt {prompt_bytes_pre} bytes exceeds "  # noqa: E501
                                f"verified {candidate.provider} transport cap {max_bytes_pre} bytes"
                            )
                        elif reason_pre == "provider_unavailable":
                            msg = f"explicit model is ineligible: provider {candidate.provider} unavailable"  # noqa: E501
                        elif reason_pre == "training_policy_excludes_model":
                            msg = f"explicit model is ineligible: training policy excludes model {candidate.model}"  # noqa: E501
                        elif reason_pre == "quota_blocked":
                            msg = f"explicit model is ineligible: provider {candidate.provider} quota blocked"  # noqa: E501
                        else:
                            msg = f"explicit model is ineligible: {reason_pre}"
                        now_fail = current_iso_timestamp()
                        self.db.update_run(
                            run_id=run_id,
                            status=RunStatus.FAILED,
                            completed_at=now_fail,
                            result_summary=msg,
                        )
                        self.db.record_event(
                            run_id, "run_failed", payload={"summary": msg, "reason": reason_pre}
                        )
                        return RunResult(
                            run_id=run_id,
                            passed=False,
                            status=RunStatus.FAILED,
                            summary=msg,
                            target_repo=str(repo),
                            base_commit=base_commit,
                            attempts=attempts,
                            checks=checks,
                            reviews=reviews,
                        )
                    else:
                        self.db.record_event(
                            run_id, "worker_candidate_skipped", payload=payload_pre
                        )
                        continue

                if (
                    is_explicit
                    and readiness_pre is not None
                    and readiness_pre.state is ProviderReadiness.UNKNOWN
                ):
                    # Explicit-owner bootstrap: the exact requested
                    # provider/model proceeds once under UNKNOWN
                    # readiness.  No substitution happened (explicit
                    # candidates fail fast instead of falling through),
                    # UNKNOWN is not converted to READY, and all other
                    # admission rules already passed above.
                    self.db.record_event(
                        run_id,
                        "explicit_unknown_readiness_dispatch",
                        payload={
                            "provider": candidate.provider,
                            "model": candidate.model,
                            "readiness_reason": readiness_pre.reason,
                            "connection_id": readiness_pre.connection_id,
                        },
                    )

                # Eligible: now create worktree/attempt. Attempt-budget
                # accounting and lifecycle identity are separate state. The
                # dispatch ordinal resets only for a new package, never for a
                # tier escalation or a no_changes/TIMEOUT exemption.
                attempt_number += 1
                attempt_id = f"{run_id}_a{attempt_number}"
                active_attempt_id = attempt_id
                branch_name = f"orch/{run_id}/a{attempt_number}"
                wt_path = base_wt_dir / run_id / f"a{attempt_number}"

                start_point = (
                    auto_failover_start
                    if auto_failover_start is not None
                    else (
                        active_continuation_checkpoint.checkpoint_sha
                        if active_continuation_checkpoint is not None
                        else lineage_base_sha
                    )
                )

                try:
                    GitManager.create_worktree(
                        repo_path=repo,
                        branch_name=branch_name,
                        worktree_path=wt_path,
                        start_point=start_point,
                    )
                except Exception as exc:
                    self.db.record_event(
                        run_id,
                        "worktree_creation_failed",
                        payload={"error": str(exc), "attempt": attempt_number},
                    )
                    # A failed worktree is not a worker dispatch, but it
                    # consumes this stage's bounded launch budget.
                    stage_attempts += 1
                    continue

                stage_label = (
                    DispatchStage.IMPLEMENTATION.value
                    if package_dispatch_ordinal == 0
                    else DispatchStage.RETRY.value
                )
                package_dispatch_ordinal += 1
                stage_attempts += 1
                effective_role = _role_for_orchestrator_dispatch(
                    DispatchStage(stage_label), surgeon_active=surgeon_active
                )

                rep_launch_key: str | None = None
                if active_continuation_attempt is not None:
                    rep_launch_key = (
                        f"replacement_launch:{run_id}:"
                        f"{active_continuation_attempt.id}:{attempt_number}"
                    )
                    # Check if replacement launch for this run was marked UNCERTAIN
                    side_effects = self.db.get_side_effects_for_run(run_id)
                    uncertain_rep = [
                        se
                        for se in side_effects
                        if se.operation == SideEffectOperation.REPLACEMENT_LAUNCH
                        and se.state == SideEffectState.UNCERTAIN
                    ]
                    if uncertain_rep:
                        msg = (
                            f"Recovery blocked: replacement launch outcome is "
                            f"uncertain for run {run_id}"
                        )
                        self.db.record_event(
                            run_id,
                            "recovery_blocked_uncertain_side_effect",
                            payload={
                                "operation": SideEffectOperation.REPLACEMENT_LAUNCH.value,
                                "reason": msg,
                            },
                        )
                        raise RuntimeError(msg)

                    self.db.journal_record_intent(
                        idempotency_key=rep_launch_key,
                        run_id=run_id,
                        attempt_id=active_continuation_attempt.id,
                        operation=SideEffectOperation.REPLACEMENT_LAUNCH,
                        payload={
                            "replacement_attempt_number": attempt_number,
                            "base_sha": start_point,
                            "role": effective_role.value,
                        },
                    )

                attempt = Attempt(
                    id=attempt_id,
                    run_id=run_id,
                    attempt_number=attempt_number,
                    provider=candidate.provider,
                    model=candidate.model,
                    worktree_path=str(wt_path),
                    branch_name=branch_name,
                    status=AttemptStatus.RUNNING,
                    created_at=current_iso_timestamp(),
                    base_sha=start_point,
                )
                self.db.create_attempt(attempt)
                worker_activation = WorkerActivation(
                    id=f"act_{uuid.uuid4().hex[:10]}",
                    run_id=run_id,
                    attempt_id=attempt_id,
                    generation=attempt_number,
                    state=WorkerActivationState.STARTING,
                    created_at=current_iso_timestamp(),
                    updated_at=current_iso_timestamp(),
                )
                self.db.create_worker_activation(worker_activation)

                if rep_launch_key and active_continuation_attempt is not None:
                    self.db.journal_record_completed(
                        idempotency_key=rep_launch_key,
                        result={"attempt_id": attempt_id, "activation_id": worker_activation.id},
                    )
                    self.db.update_worker_activation_state(
                        run_id,
                        active_continuation_attempt.attempt_number,
                        WorkerActivationState.REPLACED,
                    )

                self.db.record_event(
                    run_id,
                    "attempt_started",
                    attempt_id=attempt_id,
                    payload={
                        "provider": candidate.provider,
                        "model": candidate.model,
                        "worktree": str(wt_path),
                        "branch": branch_name,
                        "inherited_base_sha": start_point,
                        "role": effective_role.value,
                    },
                )
                # Execution telemetry (never token telemetry): exact dispatch
                # size/transport/stage so a given launch can be inspected
                # without re-deriving it from the adapter afterward.
                self.db.record_event(
                    run_id,
                    "worker_started",
                    attempt_id=attempt_id,
                    payload={
                        "provider": candidate.provider,
                        "model": candidate.model,
                        "worktree": str(wt_path),
                        "branch": branch_name,
                        "prompt_bytes": prompt_bytes_pre,
                        "transport": (
                            adapter_pre.prompt_transport_for(prompt_bytes_pre)
                            if adapter_pre
                            else "unknown"
                        ),
                        "stage": stage_label,
                        "role": effective_role.value,
                        "tier": current_tier.value,
                        "tier_reason": tier_decision.source,
                        "package_id": tier_decision.package_id,
                        "dispatch_ordinal": package_dispatch_ordinal,
                        "attempt": attempt_number,
                        "inherited_base_sha": start_point,
                    },
                )
                if (
                    current_plan_step is not None
                    and current_plan_step.status == PlanStepStatus.READY
                ):
                    mark_step(
                        self.db,
                        current_plan_step,
                        PlanStepStatus.RUNNING,
                        attempt_id=attempt_id,
                    )

                # task_for_worker already assembled as task_for_worker_pre for this attempt
                task_for_worker = task_for_worker_pre

                # Dispatch task to worker with live-output/activity hooks
                adapter = adapter_pre
                assert adapter is not None
                worker_timeout_seconds = config.worker_timeout_for(current_tier)
                deadline_iso = None
                if worker_timeout_seconds is not None and worker_timeout_seconds > 0:
                    deadline_iso = (
                        datetime.now(UTC) + timedelta(seconds=worker_timeout_seconds)
                    ).isoformat()
                self._ensure_supervision(
                    run_id,
                    attempt_id,
                    deadline_at=deadline_iso,
                    provider=candidate.provider,
                    model=candidate.model,
                )

                def _on_worker_start(
                    run_id: str, att_id: str, att_num: int, db: Database
                ) -> Callable[[dict[str, Any]], None]:
                    """Build a non-fatal callback that persists real worker process identity.

                    Uses set_worker_identity_fenced so only worker_pid and
                    worker_identity are updated, never provider/model/deadline.
                    Process identity must be persisted before the worker is
                    considered safely cancellable.  If persistence fails,
                    no PID fence is invented.
                    """

                    def _callback(
                        identity_dict: dict[str, Any],
                    ) -> None:
                        try:
                            import json as _json

                            pid = identity_dict.get("pid")
                            identity_str = _json.dumps(identity_dict, sort_keys=True)
                            db.set_worker_identity_fenced(
                                run_id,
                                att_id,
                                worker_pid=pid,
                                worker_identity=identity_str,
                            )
                            db.update_worker_activation_state(
                                run_id,
                                att_num,
                                WorkerActivationState.RUNNING,
                                worker_identity_json=identity_str,
                            )
                        except Exception:
                            pass

                    return _callback

                on_process_start = _on_worker_start(run_id, attempt_id, attempt_number, self.db)

                on_stdout, on_stderr, on_stall, on_resume = self._worker_activity_hooks(
                    run_id, attempt_id, candidate.provider, candidate.model
                )
                on_monitor_transition = self._monitor_transition_hook(
                    run_id, attempt_id, candidate.provider, candidate.model
                )
                # C11-D: the exactly resolved binding's effort reaches
                # the actual execution request as a typed tier (parsed
                # and capability-checked at resolution time, never
                # re-derived here).  Ordinary (non-supervised)
                # dispatches carry no typed effort and keep the legacy
                # adapter fallback.
                worker_effort: ReasoningEffort | None = (
                    auto_resolved.reasoning_effort if auto_resolved is not None else None
                )
                worker_req = WorkerRequest(
                    task=task_for_worker,
                    worktree_path=wt_path,
                    model=candidate.model,
                    timeout_seconds=(
                        worker_timeout_seconds
                        if worker_timeout_seconds is not None
                        else float("inf")
                    ),
                    inactivity_timeout=config.worker_inactivity_timeout,
                    on_stdout=on_stdout,
                    on_stderr=on_stderr,
                    on_stall=on_stall,
                    on_resume=on_resume,
                    on_process_start=on_process_start,
                    on_monitor_transition=on_monitor_transition,
                    reasoning_effort=worker_effort,
                    # C09: run-scoped identity (ORCH_RUN_ID/ORCH_DB_PATH) is
                    # always provided so deterministic scope expansion works
                    # without any configured tool bindings; tool-specific keys
                    # remain conditional on the frozen contract.  C11-D:
                    # the exactly resolved execution overlay (binding /
                    # profile / pool / backend identity, plus any
                    # provider-seam profile selector) rides the same
                    # request so the adapter invokes exactly what the
                    # evidence will record.
                    env=_filtered_worker_env(
                        {
                            **os.environ,
                            "ORCH_RUN_ID": run_id,
                            "ORCH_ATTEMPT_ID": attempt_id,
                            "ORCH_ASSOCIATION_ID": attempt_id,
                            "ORCH_DB_PATH": str(self.db.db_path),
                            **(auto_resolved.overlay if auto_resolved is not None else {}),
                            **(
                                {
                                    "ORCH_TOOL_TELEMETRY_SINK": str(
                                        self.db.db_path.parent
                                        / "tool-telemetry"
                                        / f"{run_id}_{attempt_id}.jsonl"
                                    ),
                                    "ORCH_TOOL_CONTRACT_DIGEST": frozen_tools.digest,
                                }
                                if frozen_tools.bindings
                                else {}
                            ),
                        },
                        authorization_env_var=_gateway_authorization_env_for(frozen_gateway),
                    ),
                )
                # Evidence is authoritative only for an actual launch: this
                # attempt has passed eligibility and has a worktree/identity.
                self.db.record_event(
                    run_id,
                    "context_projected",
                    attempt_id=attempt_id,
                    payload=rendered.event_payload(),
                )
                if active_continuation_attempt is not None:
                    continuation_reason_map = {
                        WorkerInterruptionReason.QUOTA_EXHAUSTED.value: DispatchReason.CONTINUATION_AFTER_QUOTA,  # noqa: E501
                        WorkerInterruptionReason.CONTEXT_EXHAUSTED.value: DispatchReason.CONTINUATION_AFTER_CONTEXT,  # noqa: E501
                        WorkerInterruptionReason.GRACEFUL_HANDOFF.value: DispatchReason.CONTINUATION_AFTER_GRACEFUL_HANDOFF,  # noqa: E501
                    }
                    dispatch_reason = (
                        active_continuation_dispatch_reason
                        or continuation_reason_map.get(
                            active_continuation_reason or "",
                            DispatchReason.CONTINUATION_AFTER_GRACEFUL_HANDOFF,
                        )
                    )
                elif (
                    current_package is not None
                    and stage_label == DispatchStage.IMPLEMENTATION.value
                ):
                    dispatch_reason = DispatchReason.PACKAGE_IMPLEMENTATION
                elif stage_label == DispatchStage.IMPLEMENTATION.value:
                    dispatch_reason = DispatchReason.INITIAL_IMPLEMENTATION
                else:
                    dispatch_reason = DispatchReason.VALIDATION_REPAIR
                continuation_handoff_id: str | None = None
                if active_continuation_checkpoint is not None:
                    continuation_handoff_id = active_continuation_checkpoint.handoff_id
                    if continuation_handoff_id is None:
                        resume_handoff = self.db.get_latest_handoff(
                            run_id, kind="handoff_resume_capsule"
                        )
                        continuation_handoff_id = resume_handoff.id if resume_handoff else None
                dispatch_evidence = new_dispatch_evidence(
                    run_id=run_id,
                    association_id=attempt_id,
                    attempt_id=attempt_id,
                    stage=DispatchStage(stage_label),
                    role=effective_role,
                    tier=current_tier,
                    dispatch_reason=dispatch_reason,
                    requested_provider=candidate.provider,
                    requested_model=candidate.model,
                    task=worker_req.task,
                    base_sha=start_point,
                    inherited_checkpoint_sha=(
                        active_continuation_checkpoint.checkpoint_sha
                        if active_continuation_checkpoint is not None
                        else None
                    ),
                    context_mode=rendered.projection.mode.value,
                    raw_parent_included=rendered.full_task_included,
                    prompt_transport=(
                        adapter.prompt_transport_for(prompt_bytes_pre)
                        if adapter is not None
                        else None
                    ),
                    package_id=tier_decision.package_id,
                    plan_step_id=current_plan_step.id if current_plan_step is not None else None,
                    authority_mode=load_authority_settings().mode.value,
                    routing_snapshot_digest=routing_snapshot.content_hash,
                    control_plane_digest=frozen_control_plane.digest,
                    prior_failure_refs=([prior_attempt.id] if prior_attempt is not None else None),
                    handoff_id=continuation_handoff_id,
                    # C11-D: the exact binding identity the resolved
                    # executable binding carries (binding_id/profile_id/
                    # quota_pool_id) plus the resolved effort tier --
                    # durable evidence sourced from the value the runtime
                    # is about to invoke, never from an unexecuted
                    # decision.  Resolution is mandatory on the
                    # supervised path, so a resolved value always exists
                    # here; ordinary dispatches record no binding.
                    binding_id=(auto_resolved.binding_id if auto_resolved is not None else None),
                    profile_id=(auto_resolved.profile_id if auto_resolved is not None else None),
                    quota_pool_id=(
                        auto_resolved.quota_pool_id if auto_resolved is not None else None
                    ),
                    reasoning_effort=(
                        auto_resolved.reasoning_effort.value
                        if auto_resolved is not None and auto_resolved.reasoning_effort is not None
                        else None
                    ),
                )
                metrics = tool_context_metrics(worker_req.task, frozen_tools)
                if frozen_tools.bindings:
                    dispatch_evidence.tool_transport = ",".join(
                        sorted({binding.transport.value for binding in frozen_tools.bindings})
                    )
                    dispatch_evidence.tool_schema_bytes = metrics["tool_schema_bytes"]
                    dispatch_evidence.tool_description_bytes = metrics["tool_description_bytes"]
                    dispatch_evidence.tools_exposed_count = metrics["tools_exposed_count"]
                self.db.create_dispatch_evidence(dispatch_evidence)
                if active_continuation_attempt is not None:
                    self.db.link_dispatch_replacement(
                        run_id=run_id,
                        source_attempt_id=active_continuation_attempt.id,
                        replacement_attempt_id=attempt_id,
                        replacement_started_at=dispatch_evidence.started_at,
                        handoff_id=dispatch_evidence.handoff_id,
                    )
                    linked_dispatch = self.db.get_dispatch_evidence(dispatch_evidence.id)
                    if linked_dispatch is not None:
                        dispatch_evidence.handoff_bytes = linked_dispatch.handoff_bytes
                        dispatch_evidence.time_to_resume_seconds = (
                            linked_dispatch.time_to_resume_seconds
                        )
                # C10-R4: consult the optional gateway before invoking
                # the worker.  Live infrastructure observations (health,
                # quota, pool, affinity) are read per dispatch via the
                # injectable gateway-owned provider and are NOT frozen.
                live_state = GatewayLiveState()
                if frozen_gateway is not None and frozen_gateway.enabled:
                    if self.gateway_live_state_provider is not None:
                        try:
                            live_state = self.gateway_live_state_provider.get_live_state(
                                provider=candidate.provider,
                                model=candidate.model,
                                run_id=run_id,
                            )
                        except Exception:
                            live_state = GatewayLiveState()
                # C11-D: the executed transport path must agree with the
                # exact binding's backend.  DIRECT_CLI never traverses
                # the gateway (pure DIRECT dispatch); GATEWAY executes
                # only through the existing C10 boundary with direct
                # fallback forbidden for this dispatch (a GATEWAY
                # binding never silently degrades to DIRECT even when
                # ordinary fallback would permit it) and with the
                # binding's account pin supplied as the C10 affinity
                # hint where one exists.  DIRECT_API has no verified
                # seam and never reaches this point (resolution
                # refuses it).  Ordinary dispatches keep the
                # run-configured gateway behavior verbatim.
                gateway_frozen = frozen_gateway
                gateway_endpoint = frozen_gateway.endpoint if frozen_gateway is not None else None
                gateway_accounts = frozen_gateway.accounts if frozen_gateway is not None else ()
                gateway_supports = bool(
                    getattr(adapter, "c10_gateway_transport_capabilities", frozenset())
                )
                gateway_hint = live_state.affinity_hint
                # When True the boundary must not be invoked at all for
                # this dispatch (GATEWAY binding with no enabled frozen
                # gateway): the typed FAILED result below carries zero
                # worker launch.
                gateway_unsatisfied = False
                if auto_resolved is not None and (auto_resolved.backend is BindingKind.DIRECT_CLI):
                    gateway_frozen = None
                    gateway_endpoint = None
                    gateway_accounts = ()
                    gateway_supports = False
                elif auto_resolved is not None and (auto_resolved.backend is BindingKind.GATEWAY):
                    if frozen_gateway is None or not frozen_gateway.enabled:
                        gateway_unsatisfied = True
                    else:
                        gateway_frozen = _frozen_replace(
                            frozen_gateway, direct_fallback_allowed=False
                        )
                        if auto_resolved.gateway_account_pin:
                            gateway_hint = AffinityHint(
                                run_id=run_id,
                                provider=candidate.provider,
                                model=candidate.model,
                                account_id=auto_resolved.gateway_account_pin,
                                created_at=current_iso_timestamp(),
                            )
                worker_res: WorkerResult | None = None
                gateway_evidence = None
                if gateway_unsatisfied:
                    assert auto_resolved is not None
                    unsatisfied_payload: dict[str, Any] = {
                        "reason": "gateway_binding_without_enabled_gateway",
                        "binding_id": auto_resolved.binding_id,
                        "provider": candidate.provider,
                        "model": candidate.model,
                    }
                    if supervisor is not None:
                        unsatisfied_payload.update(auto_decision.as_dict())
                    self.db.record_event(
                        run_id,
                        "autonomous_gateway_binding_unsatisfied",
                        payload=unsatisfied_payload,
                    )
                    worker_res = WorkerResult(
                        provider=candidate.provider,
                        model=candidate.model,
                        status=AttemptStatus.FAILED,
                        summary=(
                            "GATEWAY binding unsatisfiable: no enabled frozen gateway contract"
                        ),
                        stdout="",
                        stderr="",
                        error=(
                            "GATEWAY binding unsatisfiable: no enabled "
                            "frozen gateway contract; zero worker launch"
                        ),
                        outcome=DispatchOutcome.PROVIDER_OR_RUNTIME_FAILURE,
                    )
                    gateway_outcome = None
                else:
                    gateway_outcome = run_worker_with_gateway_boundary(
                        frozen_gateway=gateway_frozen,
                        endpoint=gateway_endpoint,
                        base_request=worker_req,
                        provider=candidate.provider,
                        model=candidate.model,
                        run_adapter=self._bound_adapter_run(
                            adapter,
                            autonomous=autonomous,
                            attempt_id=attempt_id,
                            run_id=run_id,
                            repo_root=str(wt_path),
                            provider=candidate.provider,
                            model=candidate.model,
                        ),
                        adapter_supports_gateway=gateway_supports,
                        accounts=gateway_accounts,
                        health_state=live_state.health_state,
                        affinity_hint=gateway_hint,
                        quota_observations=live_state.quota_observations,
                        pool_observations=live_state.pool_observations,
                    )
                    worker_req = gateway_outcome.worker_request
                    worker_res = gateway_outcome.worker_result
                    gateway_evidence = gateway_outcome.gateway_evidence
                if (
                    auto_resolved is not None
                    and auto_resolved.backend is BindingKind.GATEWAY
                    and gateway_outcome is not None
                    and gateway_outcome.disposition
                    in (
                        ExecutionDisposition.DIRECT,
                        ExecutionDisposition.SAFE_DIRECT_FALLBACK,
                    )
                ):
                    # Defense in depth (unreachable while fallback is
                    # forced off above): a GATEWAY binding that somehow
                    # executed DIRECT is a backend violation, never a
                    # success.  Terminalize the attempt as failed.
                    violation_payload: dict[str, Any] = {
                        "disposition": gateway_outcome.disposition.value,
                        "binding_id": auto_resolved.binding_id,
                        "provider": candidate.provider,
                        "model": candidate.model,
                    }
                    if supervisor is not None:
                        violation_payload.update(auto_decision.as_dict())
                    self.db.record_event(
                        run_id,
                        "autonomous_binding_backend_violation",
                        payload=violation_payload,
                    )
                    worker_res = WorkerResult(
                        provider=candidate.provider,
                        model=candidate.model,
                        status=AttemptStatus.FAILED,
                        summary="GATEWAY binding executed DIRECT",
                        stdout=(
                            gateway_outcome.worker_result.stdout
                            if gateway_outcome.worker_result is not None
                            else ""
                        ),
                        stderr=(
                            gateway_outcome.worker_result.stderr
                            if gateway_outcome.worker_result is not None
                            else ""
                        ),
                        error=(
                            "binding backend violation: GATEWAY binding "
                            "executed outside the gateway boundary"
                        ),
                        outcome=DispatchOutcome.PROVIDER_OR_RUNTIME_FAILURE,
                    )
                    gateway_evidence = gateway_outcome.gateway_evidence
                if gateway_evidence is not None:
                    dispatch_evidence.gateway_evidence_json = serialize_dispatch_evidence(
                        gateway_evidence
                    )
                if gateway_outcome is not None and (
                    gateway_outcome.disposition == ExecutionDisposition.GATEWAY_BLOCKED
                ):
                    # C10-R3/R4: the worker is not invoked when the gateway
                    # blocks the dispatch. Synthesize a typed failed
                    # WorkerResult so the downstream orchestrator code can
                    # proceed uniformly. Preserve the actual failure reason
                    # (adapter unsupported, endpoint missing, account
                    # unavailable, quota, auth, mismatch, etc.) — do not
                    # collapse every block into adapter_gateway_unsupported.
                    actual_reason = "gateway_blocked"
                    actual_error_kind = None
                    if gateway_evidence is not None:
                        if gateway_evidence.error is not None:
                            actual_reason = gateway_evidence.error.message or actual_reason
                            actual_error_kind = (
                                gateway_evidence.error.kind.value
                                if gateway_evidence.error.kind
                                else None
                            )
                        else:
                            actual_reason = (
                                gateway_evidence.evidence.get("reason")
                                or gateway_evidence.evidence.get("fallback_reason")
                                or gateway_evidence.evidence.get("pre_launch_error")
                                or actual_reason
                            )
                    # Fallback to outcome's evidence reason if still generic
                    if actual_reason == "gateway_blocked" and gateway_evidence is not None:
                        actual_reason = gateway_evidence.evidence.get("reason", actual_reason)
                    worker_res = WorkerResult(
                        provider=candidate.provider,
                        model=candidate.model,
                        status=AttemptStatus.FAILED,
                        summary="Gateway blocked dispatch",
                        stdout="",
                        stderr="",
                        error=(f"Gateway execution blocked: {actual_reason}"),
                        outcome=DispatchOutcome.PROVIDER_OR_RUNTIME_FAILURE,
                    )
                    payload_gb: dict[str, Any] = {
                        "provider": candidate.provider,
                        "model": candidate.model,
                        "disposition": gateway_outcome.disposition.value,
                        "reason": actual_reason,
                    }
                    if actual_error_kind is not None:
                        payload_gb["error_kind"] = actual_error_kind
                    if gateway_evidence is not None:
                        payload_gb["gateway_evidence"] = gateway_evidence.evidence
                    self.db.record_event(
                        run_id,
                        "gateway_blocked",
                        attempt_id=attempt_id,
                        payload=payload_gb,
                    )
                assert worker_res is not None
                if (
                    worker_res.status is AttemptStatus.SUCCESS
                    and self.readiness_service is not None
                    and readiness_pre is not None
                    and readiness_pre.connection_id is not None
                ):
                    # A successful real execution is direct evidence
                    # that the exact connection used was usable.  Only
                    # the exact connection is marked; failures record
                    # nothing (generic failure never fabricates
                    # NOT_READY).
                    self.readiness_service.record_success(
                        readiness_pre.connection_id
                    )

                if frozen_tools.bindings:
                    records = read_telemetry(
                        self.db.db_path.parent / "tool-telemetry" / f"{run_id}_{attempt_id}.jsonl"
                    )
                    used_refs = {str(record["ref"]) for record in records}
                    dispatch_evidence.tool_discovery_bytes = _telemetry_total(
                        records, "discovery_bytes"
                    )
                    dispatch_evidence.tool_result_bytes = _telemetry_total(records, "result_bytes")
                    dispatch_evidence.tools_actually_used_count = len(used_refs)

                update_dispatch_from_result(dispatch_evidence, worker_res)
                self.db.update_dispatch_evidence(dispatch_evidence)

                actual_prompt_bytes = (
                    worker_res.prompt_bytes
                    if worker_res.prompt_bytes is not None
                    else prompt_bytes_pre
                )
                actual_transport = worker_res.prompt_transport_used or (
                    adapter_pre.prompt_transport_for(prompt_bytes_pre) if adapter_pre else "unknown"
                )
                persist_worker_usage(
                    self.db,
                    run_id=run_id,
                    association_id=attempt_id,
                    attempt_id=attempt_id,
                    review_id=None,
                    stage=DispatchStage(stage_label),
                    tier=current_tier,
                    provider=candidate.provider,
                    model=candidate.model,
                    prompt_bytes=actual_prompt_bytes,
                    transport=actual_transport,
                    result=worker_res,
                )

                # C14-C: the worker's aggregate subprocess duration
                # (worker_res.duration_seconds) is MEASURED, but the
                # current production architecture cannot honestly split
                # it into PROCESS_STARTUP / MODEL_WAIT / TOOL_EXECUTION
                # sub-boundaries.  Recording an explicit UNKNOWN row per
                # phase per attempt keeps the run truthful: the
                # aggregate duration is preserved on worker_res, but the
                # three canonical sub-phases are surfaced as UNKNOWN
                # with an honest note.  Telemetry writes are isolated so
                # a recording failure cannot corrupt the worker result.
                try:
                    attempt_unknown_notes = (
                        "The current production worker subprocess "
                        "exposes only one boundary (process start to "
                        "process exit, reported as "
                        "WorkerResult.duration_seconds). Splitting that "
                        "boundary into PROCESS_STARTUP, MODEL_WAIT, and "
                        "TOOL_EXECUTION sub-boundaries would require "
                        "additional structured events from every "
                        "worker adapter that the runtime does not "
                        "currently produce. The aggregate is therefore "
                        "MEASURED on the worker result but the three "
                        "ROADMAP §11 sub-phases remain UNKNOWN here."
                    )
                    pkg_id = current_package.id if current_package is not None else None
                    for unknown_phase in (
                        PhaseKind.PROCESS_STARTUP,
                        PhaseKind.MODEL_WAIT,
                        PhaseKind.TOOL_EXECUTION,
                    ):
                        mark_phase_unknown(
                            self.db,
                            run_id=run_id,
                            phase=unknown_phase,
                            source_kind="orchestrator._run_core",
                            boundary_kind="worker_subprocess",
                            attempt_id=attempt_id,
                            package_id=pkg_id,
                            notes=attempt_unknown_notes,
                        )
                except Exception:
                    pass

                self.db.record_event(
                    run_id,
                    "worker_completed",
                    attempt_id=attempt_id,
                    payload={
                        "provider": candidate.provider,
                        "model": candidate.model,
                        "status": worker_res.status.value,
                        "summary": worker_res.summary,
                        "stdout": _bounded_output(worker_res.stdout),
                        "stderr": _bounded_output(worker_res.stderr),
                        "error": worker_res.error,
                        "duration_seconds": worker_res.duration_seconds,
                        "prompt_bytes": actual_prompt_bytes,
                        "transport": actual_transport,
                        "stage": stage_label,
                        "role": effective_role.value,
                        "tier": current_tier.value,
                        "package_id": tier_decision.package_id,
                        "attempt": attempt_number,
                    },
                )

                if worker_res.interruption_reason is not None:
                    interruption_reason = worker_res.interruption_reason
                    # Quiescence check
                    worker_liveness = Liveness.DEAD
                    sup = self.db.get_supervision(run_id)
                    if sup is not None and sup.worker_identity:
                        try:
                            proc_id = ProcessIdentity.from_json(sup.worker_identity)
                            worker_liveness = probe_liveness(proc_id)
                        except Exception:
                            worker_liveness = Liveness.UNKNOWN
                    elif sup is not None and sup.worker_pid is not None:
                        worker_liveness = Liveness.UNKNOWN

                    if worker_liveness is Liveness.UNKNOWN:
                        self.db.update_worker_activation_state(
                            run_id,
                            attempt_number,
                            WorkerActivationState.TERMINATION_UNCERTAIN,
                        )
                        self.db.record_event(
                            run_id,
                            "recovery_blocked_unknown_worker_liveness",
                            payload={"attempt_id": attempt_id},
                        )
                        raise RuntimeError(
                            f"Worker process liveness is unknown for attempt {attempt_id}; "
                            "failing closed to prevent concurrent mutation."
                        )

                    # Worktree changes check
                    changed_paths = tuple(GitManager.get_changed_paths(wt_path))
                    has_changes = len(changed_paths) > 0
                    cp_obj: WorkerCheckpoint | None = None
                    if has_changes:
                        idempotency_key = f"checkpoint:{run_id}:{attempt_id}"
                        existing_se = self.db.get_side_effect(idempotency_key)
                        if (
                            existing_se
                            and existing_se.state == SideEffectState.COMPLETED
                            and existing_se.result_json
                        ):
                            try:
                                res_dict = json.loads(existing_se.result_json)
                                cp_sha = res_dict.get("checkpoint_sha")
                                if cp_sha:
                                    cp_obj = self.db.get_worker_checkpoint_by_sha(run_id, cp_sha)
                            except Exception:
                                cp_obj = None
                        if cp_obj is None:
                            self.db.journal_record_intent(
                                idempotency_key=idempotency_key,
                                run_id=run_id,
                                attempt_id=attempt_id,
                                operation=SideEffectOperation.CHECKPOINT_CREATION,
                                payload={"reason": interruption_reason.value},
                            )
                            try:
                                cp_sha = GitManager.create_checkpoint_commit(
                                    worktree_path=wt_path,
                                    message=(
                                        f"orch checkpoint({run_id}/{attempt_id}): "
                                        f"partial worker state ({interruption_reason.value})"
                                    ),
                                )
                                if cp_sha is not None:
                                    cp_obj = WorkerCheckpoint(
                                        id=f"cp_{uuid.uuid4().hex[:10]}",
                                        run_id=run_id,
                                        source_attempt_id=attempt_id,
                                        source_base_sha=start_point,
                                        checkpoint_sha=cp_sha,
                                        reason=interruption_reason.value,
                                        changed_paths=changed_paths,
                                        created_at=current_iso_timestamp(),
                                    )
                                    self.db.create_worker_checkpoint(cp_obj)
                                    self.db.journal_record_completed(
                                        idempotency_key=idempotency_key,
                                        result={
                                            "checkpoint_sha": cp_sha,
                                            "checkpoint_id": cp_obj.id,
                                        },
                                    )
                                else:
                                    self.db.journal_record_completed(
                                        idempotency_key=idempotency_key,
                                        result={"no_changes": True},
                                    )
                            except Exception as exc:
                                self.db.journal_record_failed(
                                    idempotency_key=idempotency_key, error=str(exc)
                                )
                                raise

                    if cp_obj is not None:
                        active_continuation_checkpoint = cp_obj
                        self.db.record_event(
                            run_id,
                            "worker_checkpoint_created",
                            attempt_id=attempt_id,
                            payload={
                                "checkpoint_sha": cp_obj.checkpoint_sha,
                                "source_attempt_id": attempt_id,
                                "reason": interruption_reason.value,
                                "changed_paths": list(changed_paths),
                            },
                        )
                    else:
                        active_continuation_checkpoint = None
                        self.db.record_event(
                            run_id,
                            "worker_interrupted_no_changes",
                            attempt_id=attempt_id,
                            payload={"reason": interruption_reason.value},
                        )

                    active_continuation_reason = interruption_reason.value
                    active_continuation_dispatch_reason = None
                    active_continuation_attempt = attempt

                    now = current_iso_timestamp()
                    attempt.status = AttemptStatus.FAILED
                    attempt.interruption_reason = interruption_reason.value
                    attempt.worker_stdout = worker_res.stdout
                    attempt.worker_stderr = worker_res.stderr
                    attempt.worker_error = f"Worker interrupted: {interruption_reason.value}"
                    attempt.completed_at = now
                    self.db.update_attempt(
                        attempt_id=attempt_id,
                        status=AttemptStatus.FAILED,
                        worker_stdout=worker_res.stdout,
                        worker_stderr=worker_res.stderr,
                        worker_error=attempt.worker_error,
                        completed_at=now,
                        interruption_reason=interruption_reason.value,
                    )
                    self.db.update_worker_activation_state(
                        run_id,
                        attempt_number,
                        (
                            WorkerActivationState.CHECKPOINTED
                            if active_continuation_checkpoint
                            else WorkerActivationState.INTERRUPTED
                        ),
                    )
                    self.db.record_event(
                        run_id,
                        "worker_interrupted",
                        attempt_id=attempt_id,
                        payload={
                            "reason": interruption_reason.value,
                            "provider": candidate.provider,
                            "model": candidate.model,
                            "has_changes": has_changes,
                            "checkpoint_sha": (
                                active_continuation_checkpoint.checkpoint_sha
                                if active_continuation_checkpoint
                                else None
                            ),
                        },
                    )
                    self.db.record_event(
                        run_id,
                        "dispatch_outcome_classified",
                        attempt_id=attempt_id,
                        payload={
                            "outcome": DispatchOutcome.PROVIDER_OR_RUNTIME_FAILURE.value,
                            "escalation_evidence": False,
                            "role": effective_role.value,
                            "reason": interruption_reason.value,
                        },
                    )

                    if interruption_reason == WorkerInterruptionReason.QUOTA_EXHAUSTED:
                        blocked_providers_in_cycle.add(candidate.provider)

                    if config.model_override is None:
                        stage_attempts = max(0, stage_attempts - 1)

                    attempts.append(attempt)
                    active_attempt_id = None
                    continue

                elif worker_res.status == AttemptStatus.SUCCESS:
                    self.db.update_worker_activation_state(
                        run_id, attempt_number, WorkerActivationState.SUCCEEDED
                    )
                    # Check for candidate changes
                    has_changes = GitManager.has_diff_against_base(wt_path, lineage_base_sha)
                    changed_paths_in_wt = GitManager.get_changed_paths(wt_path)
                    if changed_paths_in_wt:
                        # Capture and commit changes in worktree
                        commit_sha = GitManager.commit_changes(
                            worktree_path=wt_path,
                            message=(
                                f"orch({run_id}): candidate changes for attempt {attempt_number} "
                                f"({candidate.provider}/{candidate.model})"
                            ),
                        )
                        if commit_sha is None:
                            raise RuntimeError("Candidate commit unexpectedly has no SHA")
                    else:
                        commit_sha = start_point

                    dispatch_evidence.candidate_sha = commit_sha
                    self.db.update_dispatch_evidence(dispatch_evidence)

                    # Run gate verification in worktree
                    gate_payload: dict[str, Any] = {
                        "gate_command": gate_command,
                        "candidate_sha": commit_sha,
                        "role": effective_role.value,
                    }
                    if not has_changes:
                        gate_payload["no_changes"] = True
                    self.db.record_event(
                        run_id,
                        "gate_started",
                        attempt_id=attempt_id,
                        payload=gate_payload,
                    )

                    gate_args = split_command(gate_command)
                    with measure_phase(
                        self.db,
                        run_id=run_id,
                        phase=PhaseKind.DETERMINISTIC_VALIDATION,
                        source_kind="orchestrator._run_core",
                        boundary_kind="gate_run",
                        attempt_id=attempt_id,
                        notes="C14-C: time spent inside the gate subprocess boundary",
                    ):
                        gate_res = run_process(
                            gate_args,
                            cwd=wt_path,
                            timeout=config.gate_timeout,
                        )
                    changed_paths = tuple(GitManager.get_changed_paths(wt_path))
                    post_gate_has_diff = (
                        GitManager.has_diff_against_base(wt_path, lineage_base_sha)
                        if not has_changes
                        else True
                    )
                    mutated_worktree = bool(
                        gate_res.passed and (not has_changes) and post_gate_has_diff
                    )
                    disposition = _classify_gate_outcome(
                        gate_res,
                        mutated_worktree=mutated_worktree,
                        changed_paths=changed_paths,
                        candidate_sha=commit_sha,
                    )

                    completed_gate_payload: dict[str, Any] = {
                        "gate_command": gate_command,
                        "candidate_sha": commit_sha,
                        "stdout": _bounded_output(gate_res.stdout),
                        "stderr": _bounded_output(gate_res.stderr),
                        "changed_paths": list(changed_paths),
                        "role": effective_role.value,
                    }
                    completed_gate_payload.update(_gate_disposition_payload(disposition))
                    if not has_changes:
                        completed_gate_payload["no_changes"] = True
                    self.db.record_event(
                        run_id,
                        "gate_completed",
                        attempt_id=attempt_id,
                        payload=completed_gate_payload,
                    )

                    check_id = f"check_{uuid.uuid4().hex[:10]}"
                    check = CheckResult(
                        id=check_id,
                        attempt_id=attempt_id,
                        gate_command=gate_command,
                        exit_code=gate_res.exit_code,
                        stdout=gate_res.stdout,
                        stderr=gate_res.stderr,
                        passed=gate_res.passed,
                        created_at=current_iso_timestamp(),
                    )
                    self.db.create_check(check)
                    checks.append(check)

                    is_clean_pass = disposition.outcome is GateOutcome.PASS and (
                        has_changes or not post_gate_has_diff
                    )
                    if is_clean_pass:
                        now = current_iso_timestamp()
                        attempt.status = AttemptStatus.SUCCESS
                        attempt.worker_stdout = worker_res.stdout
                        attempt.worker_stderr = worker_res.stderr
                        attempt.commit_sha = commit_sha
                        attempt.completed_at = now
                        self.db.update_attempt(
                            attempt_id=attempt_id,
                            status=AttemptStatus.SUCCESS,
                            worker_stdout=worker_res.stdout,
                            worker_stderr=worker_res.stderr,
                            commit_sha=commit_sha,
                            completed_at=now,
                        )

                        attempt_succeeded_payload: dict[str, Any] = {
                            "commit_sha": commit_sha,
                            "branch": branch_name,
                            "changed_paths": changed_paths,
                        }
                        if not has_changes:
                            attempt_succeeded_payload["no_changes"] = True
                            attempt_succeeded_payload["already_satisfied"] = True
                            attempt_succeeded_payload["disposition"] = "already_satisfied"
                        self.db.record_event(
                            run_id,
                            "attempt_succeeded",
                            attempt_id=attempt_id,
                            payload=attempt_succeeded_payload,
                        )
                        gate_passed_payload: dict[str, Any] = {
                            "candidate_sha": commit_sha,
                            "gate_command": gate_command,
                            "role": effective_role.value,
                            "gate_outcome": GateOutcome.PASS.value,
                        }
                        if not has_changes:
                            gate_passed_payload["no_changes"] = True
                            gate_passed_payload["already_satisfied"] = True
                        self.db.record_event(
                            run_id,
                            "gate_passed",
                            attempt_id=attempt_id,
                            payload=gate_passed_payload,
                        )
                        if not has_changes:
                            self.db.record_event(
                                run_id,
                                "dispatch_outcome_classified",
                                attempt_id=attempt_id,
                                payload={
                                    "outcome": DispatchOutcome.ALREADY_SATISFIED.value,
                                    "escalation_evidence": False,
                                    "candidate_sha": lineage_base_sha,
                                    "no_changes": True,
                                    "gate_outcome": disposition.outcome.value,
                                    "role": role_for_stage(DispatchStage(stage_label)).value,
                                },
                            )

                        # Candidate boundary reached: gate passed, candidate
                        # committed, worktree quiescent.  The terminal
                        # candidate is published only AFTER required review
                        # passes (see the post-review gate below); review
                        # BLOCK / infrastructure failures must never leave a
                        # published candidate or a COMPLETED run behind.
                        # Intermediate multi-package lineage candidates keep
                        # the historical publish-then-continue behavior under
                        # the run's frozen publication policy.

                        # A broad objective is executed as strictly sequential
                        # gated descendants.  Never create the next worktree
                        # from the original base after package one.
                        if current_plan_step is not None and current_package is not None:
                            assert commit_sha is not None
                            candidate_publication = self._publish_gate_passed_candidate(
                                run_id=run_id,
                                attempt_id=attempt_id,
                                config=config,
                                repo=repo,
                                commit_sha=commit_sha,
                                branch_name=branch_name,
                                worktree_path=wt_path,
                            )
                            mark_step(
                                self.db,
                                current_plan_step,
                                PlanStepStatus.COMPLETED,
                                attempt_id=attempt_id,
                            )
                            self.db.update_work_package_candidate(
                                current_package.id,
                                lineage_base_sha,
                                json.dumps(build_context_capsule(current_package)),
                            )
                            pkg_completed_payload: dict[str, Any] = {
                                "step_id": current_plan_step.id,
                                "package_id": current_package.id,
                                "candidate_sha": commit_sha,
                            }
                            if not has_changes:
                                pkg_completed_payload["disposition"] = "already_satisfied"
                                pkg_completed_payload["no_changes"] = True
                            self.db.record_event(
                                run_id,
                                "package_completed",
                                attempt_id=attempt_id,
                                payload=pkg_completed_payload,
                            )
                            lineage_base_sha = commit_sha
                            active_continuation_checkpoint = None
                            active_continuation_reason = None
                            active_continuation_dispatch_reason = None
                            active_continuation_attempt = None
                            blocked_providers_in_cycle.clear()
                            ready_dependents(self.db, run_id)
                            next_step = next(
                                (
                                    step
                                    for step in self.db.get_plan_steps(run_id)
                                    if step.status == PlanStepStatus.READY
                                ),
                                None,
                            )
                            if next_step is not None:
                                if next_step.work_package_id:
                                    self.db.update_work_package_candidate(
                                        next_step.work_package_id, lineage_base_sha
                                    )
                                self.db.record_event(
                                    run_id,
                                    "next_package_started",
                                    payload={
                                        "step_id": next_step.id,
                                        "package_id": next_step.work_package_id,
                                        "base_candidate_sha": lineage_base_sha,
                                    },
                                )
                                attempts.append(attempt)
                                active_attempt_id = None
                                next_package = (
                                    self.db.get_work_package(next_step.work_package_id)
                                    if next_step.work_package_id
                                    else None
                                )
                                tier_decision = choose_tier(
                                    config,
                                    stage=DispatchStage.IMPLEMENTATION,
                                    package=next_package,
                                )
                                current_tier = tier_decision.effective_tier
                                self.db.update_run_tier(run_id, current_tier)
                                self.db.record_event(
                                    run_id,
                                    "tier_decision",
                                    payload=tier_decision.as_payload(),
                                )
                                if config.routing_mode == "auto":
                                    self.db.record_event(
                                        run_id,
                                        "tier_classified",
                                        payload={
                                            "chosen": current_tier.value,
                                            "package_id": tier_decision.package_id,
                                            "source": tier_decision.source,
                                            "signals": list(tier_decision.signals),
                                        },
                                    )
                                quota_states = self.db.list_quota()
                                base_candidates = resolve_candidate_chain(
                                    tier=current_tier,
                                    training_allowed=config.training_allowed,
                                    is_peak=is_peak,
                                    model_override=config.model_override,
                                    provider_override=config.provider_override,
                                    snapshot=routing_snapshot,
                                )
                                candidates = list(base_candidates)
                                self.db.record_event(
                                    run_id,
                                    "tier_candidates_resolved",
                                    payload={
                                        "tier": current_tier.value,
                                        "package_id": tier_decision.package_id,
                                        "candidates": [
                                            {"provider": c.provider, "model": c.model}
                                            for c in base_candidates
                                        ],
                                        "effective_candidates": [
                                            {"provider": c.provider, "model": c.model}
                                            for c in candidates
                                        ],
                                    },
                                )
                                candidate_idx = 0
                                stage_attempts = 0
                                package_dispatch_ordinal = 0
                                tier_has_escalation_evidence = False
                                escalation_stage = None
                                continue

                        lineage_base_sha = commit_sha
                        active_continuation_checkpoint = None
                        active_continuation_reason = None
                        active_continuation_dispatch_reason = None
                        active_continuation_attempt = None
                        blocked_providers_in_cycle.clear()

                        if not has_changes:
                            summary = (
                                f"Task completed successfully on attempt {attempt_number} "
                                f"({candidate.provider}/{candidate.model}) with no diff "
                                f"(already satisfied). Gate '{gate_command}' PASSED on "
                                f"candidate {lineage_base_sha}."
                            )
                        else:
                            summary = (
                                f"Task completed successfully on attempt {attempt_number} "
                                f"({candidate.provider}/{candidate.model}). "
                                f"Gate '{gate_command}' PASSED."
                            )

                        def _mark_candidate_ready(
                            ready_attempt_id: str,
                            ready_commit_sha: str | None,
                            ready_branch: str,
                            ready_summary: str,
                            ready_no_changes: bool,
                        ) -> None:
                            """Persist the single candidate-ready transition.

                            The ONE owner of Run COMPLETED for both ordinary
                            and autonomous operation.  Called only after
                            semantic worker success, a real candidate commit
                            with provenance, a deterministic gate PASS,
                            required independent review
                            PASS/PASS_WITH_BACKLOG, and required candidate
                            publication have ALL succeeded -- never before.
                            """
                            now_ready = current_iso_timestamp()
                            self.db.update_run(
                                run_id=run_id,
                                status=RunStatus.COMPLETED,
                                completed_at=now_ready,
                                final_attempt_id=ready_attempt_id,
                                result_summary=ready_summary,
                            )
                            ready_payload: dict[str, Any] = {
                                "commit_sha": ready_commit_sha,
                                "branch": ready_branch,
                            }
                            if ready_no_changes:
                                ready_payload["no_changes"] = True
                                ready_payload["already_satisfied"] = True
                            self.db.record_event(
                                run_id,
                                "run_completed",
                                attempt_id=ready_attempt_id,
                                payload=ready_payload,
                            )
                            self.db.record_event(
                                run_id,
                                "run_ready",
                                attempt_id=ready_attempt_id,
                                payload=ready_payload,
                            )
                            if plan_steps:
                                for plan_step in plan_steps:
                                    if plan_step.status.value != "COMPLETED":
                                        mark_step(
                                            self.db,
                                            plan_step,
                                            PlanStepStatus.COMPLETED,
                                            attempt_id=ready_attempt_id,
                                        )
                                persisted_plan = self.db.get_run_plan(run_id)
                                if persisted_plan is not None:
                                    self.db.update_run_plan(persisted_plan.id, PlanStatus.COMPLETED)
                                pkg_completed_end_payload: dict[str, Any] = {
                                    "candidate_sha": ready_commit_sha,
                                }
                                if ready_no_changes:
                                    pkg_completed_end_payload["disposition"] = "already_satisfied"
                                    pkg_completed_end_payload["no_changes"] = True
                                self.db.record_event(
                                    run_id,
                                    "package_completed",
                                    attempt_id=ready_attempt_id,
                                    payload=pkg_completed_end_payload,
                                )

                        def _terminal_review_verdict(
                            terminal_reviews: list[ReviewResult],
                        ) -> ReviewVerdict:
                            """Resolve the terminal review verdict from durable state.

                            Prefers the persisted review-loop
                            ``terminal_verdict`` (durable across Database
                            reopen mid-loop); falls back to the last
                            in-memory review.  With no review evidence at
                            all, fails closed as REVIEW_EXECUTION_FAILED.
                            Uses the existing review lifecycle semantics --
                            no second verdict mapper.
                            """
                            try:
                                loop_state = self.db.get_review_loop_state(run_id)
                            except Exception:
                                loop_state = None
                            raw = (
                                loop_state.get("terminal_verdict")
                                if isinstance(loop_state, dict)
                                else None
                            )
                            if isinstance(raw, str) and raw:
                                try:
                                    return ReviewVerdict(raw)
                                except ValueError:
                                    pass
                            if terminal_reviews:
                                return terminal_reviews[-1].verdict
                            return ReviewVerdict.REVIEW_EXECUTION_FAILED

                        attempts.append(attempt)
                        active_attempt_id = None

                        # C13-F: deterministic risk-adaptive review
                        # classification.  Computed once at candidate-ready
                        # time, persisted on the run row, and consulted by
                        # Accept + supervisor recovery + handoff + retry.
                        # When the decision says no independent review is
                        # required, the existing review+repair loop is
                        # skipped entirely -- this is the canonical seam
                        # the doctrine authorizes.
                        decision = self._resolve_c13_f_review_decision(
                            run_id=run_id,
                            config=config,
                            target_repo=repo,
                            # Both paths expose the run's authoritative
                            # base commit: for adoption, ``adopted_base_commit``
                            # holds the existing run row's base; for new runs,
                            # ``base_commit`` was captured from the live HEAD.
                            base_commit=adopted_base_commit or base_commit,
                            candidate_sha=commit_sha,
                            is_autonomous=bool(autonomous is not None),
                        )
                        if not decision.independent_review_required:
                            self.db.record_event(
                                run_id,
                                "review_skipped_by_risk_adaptive_policy",
                                attempt_id=attempt_id,
                                payload={
                                    "decision_id": decision.decision_id,
                                    "change_class": decision.classification.change_class.value,
                                    "risk_level": decision.classification.risk_level.value,
                                    "review_policy_mode": decision.review_policy_mode.value,
                                    "skip_reason": decision.review_skip_reason,
                                    "review_requirement_source": (
                                        decision.review_requirement_source.value
                                    ),
                                    "is_autonomous": decision.is_autonomous,
                                },
                            )
                            # C14-C: at this exact seam the runtime
                            # positively knows that an independent REVIEW
                            # phase does not apply to this candidate
                            # (authoritative C13-F decision:
                            # ``independent_review_required == False``).
                            # The REVIEW phase is therefore recorded as
                            # NOT_APPLICABLE with truthful attribution --
                            # never inferred from elapsed time and never
                            # left silently absent.  The write is purely
                            # observational: a telemetry failure here is
                            # swallowed and cannot alter the established
                            # review-skip decision or the run outcome.
                            try:
                                mark_phase_not_applicable(
                                    self.db,
                                    run_id=run_id,
                                    phase=PhaseKind.REVIEW,
                                    source_kind="orchestrator._run_core",
                                    boundary_kind="review_skipped_by_risk_adaptive_policy",
                                    attempt_id=attempt_id,
                                    package_id=(
                                        current_package.id if current_package is not None else None
                                    ),
                                    notes=(
                                        "Review did not apply to this candidate -- the "
                                        "authoritative risk-adaptive decision resolved "
                                        "independent_review_required=False at "
                                        "the canonical review-skip seam "
                                        "(decision_id="
                                        + decision.decision_id
                                        + ", skip_reason="
                                        + str(decision.review_skip_reason)
                                        + "); no reviewer was launched."
                                    ),
                                )
                            except Exception:
                                # Telemetry MUST NOT affect the review-skip
                                # decision or the run outcome.
                                pass

                        if decision.independent_review_required:
                            from saberops.review import ReviewEngine

                            review_engine = ReviewEngine(
                                db=self.db,
                                registry=self.registry,
                                readiness_service=self.readiness_service,
                            )
                            current_commit_sha = commit_sha
                            current_branch = branch_name
                            current_wt_path = wt_path
                            # The attempt owning the terminal reviewable
                            # candidate for the deferred publication step.
                            # Starts at the initial gate-passed attempt;
                            # each successful repair round advances it.
                            review_publish_attempt_id = attempt_id

                            def _normalize(s: str | None) -> str:
                                return (s or "").strip().lower()

                            def _do_repair(
                                from_sha_local: str,
                                rendered_task: RenderResult,
                                findings_cnt: int,
                                repair_requirements: str,
                                package_local: Any = current_package,
                            ) -> tuple[Attempt | None, str | None, Path | None, bool]:
                                """Execute repair worktree cycle with continuation support."""
                                nonlocal attempt_number, active_attempt_id, branch_name
                                nonlocal review_publish_attempt_id

                                cur_from_sha = from_sha_local
                                cur_rendered_task = rendered_task
                                blocked_repair_providers: set[str] = set()
                                rep_continuation_attempt: Attempt | None = None
                                repair_resolved: ResolvedExecutionBinding | None = None

                                while True:
                                    prompt_task = cur_rendered_task.prompt
                                    repair_decision = choose_tier(
                                        config,
                                        stage=DispatchStage.REPAIR,
                                        package=package_local,
                                        requirements=repair_requirements,
                                    )
                                    cur_tier = repair_decision.effective_tier
                                    self.db.record_event(
                                        run_id,
                                        "tier_decision",
                                        payload=repair_decision.as_payload(),
                                    )
                                    is_explicit_rep = config.model_override is not None
                                    prompt_bytes_rep = len(prompt_task.encode("utf-8"))
                                    quota_states_rep = self.db.list_quota()
                                    base_repair_candidates = resolve_candidate_chain(
                                        tier=cur_tier,
                                        training_allowed=config.training_allowed,
                                        is_peak=is_peak,
                                        model_override=config.model_override,
                                        provider_override=config.provider_override,
                                        snapshot=routing_snapshot,
                                    )
                                    if not is_explicit_rep and blocked_repair_providers:
                                        base_repair_candidates = [
                                            c
                                            for c in base_repair_candidates
                                            if c.provider not in blocked_repair_providers
                                        ]
                                    repair_candidates = list(base_repair_candidates)
                                    selected_repair: (
                                        tuple[WorkerCandidate, WorkerAdapter] | None
                                    ) = None
                                    selected_repair_resolved: ResolvedExecutionBinding | None = None
                                    selected_repair_readiness = None
                                    last_reason: str | None = None
                                    for repair_candidate in repair_candidates:
                                        repair_resolved_candidate: (
                                            ResolvedExecutionBinding | None
                                        ) = None
                                        repair_adapter = self.registry.get(
                                            repair_candidate.provider
                                        )
                                        repair_binding_pool_id: str | None = None
                                        if supervisor is not None:
                                            try:
                                                repair_resolved_candidate = (
                                                    supervisor.resolve_candidate_binding(
                                                        repair_candidate
                                                    )
                                                )
                                                repair_adapter = repair_resolved_candidate.adapter
                                                repair_binding_pool_id = (
                                                    repair_resolved_candidate.quota_pool_id
                                                )
                                            except BindingResolutionError as exc:
                                                last_reason = (
                                                    f"autonomous_binding_{exc.reason.lower()}"
                                                )
                                                repair_adapter = None
                                        elif getattr(repair_candidate, "binding_id", None):
                                            # C15-XB-01: ordinary repair
                                            # dispatches preserve the exact
                                            # owner-approved binding too.  A
                                            # non-empty binding_id is
                                            # authoritative; failure fails
                                            # closed to the next candidate
                                            # with no provider/model
                                            # substitution.
                                            try:
                                                ordinary_repair_resolved = (
                                                    resolve_worker_candidate_binding(
                                                        registry=(frozen_worker_binding_registry),
                                                        candidate_binding_id=(
                                                            repair_candidate.binding_id
                                                        ),
                                                        provider=repair_candidate.provider,
                                                        model=repair_candidate.model,
                                                        adapter=repair_adapter,
                                                    )
                                                )
                                                assert ordinary_repair_resolved is not None
                                                repair_resolved_candidate = ordinary_repair_resolved
                                                repair_adapter = repair_resolved_candidate.adapter
                                                repair_binding_pool_id = (
                                                    repair_resolved_candidate.quota_pool_id
                                                )
                                            except BindingResolutionError as exc:
                                                last_reason = f"exact_binding_{exc.reason.lower()}"
                                                repair_adapter = None
                                        # C15-GC: repair dispatches execute
                                        # against an Orchestrator-managed
                                        # worktree containing the canonical
                                        # AGENTS.md, so the same
                                        # governed-instruction contract
                                        # applies.  C07 evaluates the
                                        # adapter's typed
                                        # instruction-compatibility evidence
                                        # before any worker process is
                                        # launched; an INCOMPATIBLE/UNKNOWN
                                        # adapter is recorded as a skip
                                        # event and Orch routing may
                                        # continue to the next repair
                                        # candidate.
                                        if self.readiness_service is not None:
                                            rep_profile = (
                                                repair_resolved_candidate.profile_id
                                                if repair_resolved_candidate
                                                is not None
                                                else ""
                                            )
                                            readiness_rep = (
                                                self.readiness_service.admission_for(
                                                    provider=repair_candidate.provider,
                                                    profile_id=rep_profile,
                                                )
                                            )
                                        else:
                                            readiness_rep = None
                                        rep_readiness_state = (
                                            readiness_rep.state
                                            if readiness_rep is not None
                                            else None
                                        )
                                        rep_readiness_reason = (
                                            readiness_rep.reason
                                            if readiness_rep is not None
                                            else None
                                        )
                                        if (
                                            readiness_rep is not None
                                        ):
                                            rep_executable = (
                                                readiness_rep.executable_available
                                            )
                                        else:
                                            rep_executable = True
                                        reason_rep = evaluate_eligibility(
                                            candidate=repair_candidate,
                                            adapter=repair_adapter,
                                            training_allowed=config.training_allowed,
                                            quota_states=quota_states_rep,
                                            prompt_bytes=prompt_bytes_rep,
                                            explicit_override=is_explicit_rep,
                                            reserve_override=frozen_control_plane.reserve_override,
                                            policy=control_plane_policy,
                                            required_capabilities=frozen_control_plane.required_capabilities,
                                            control_plane_digest=frozen_control_plane.digest,
                                            binding_pool_id=repair_binding_pool_id,
                                            requires_governed_instructions=True,
                                            readiness_state=rep_readiness_state,
                                            readiness_reason=rep_readiness_reason,
                                            readiness_executable_available=rep_executable,
                                        )
                                        decision_rep = control_plane_decision(
                                            candidate=repair_candidate,
                                            adapter=repair_adapter,
                                            training_allowed=config.training_allowed,
                                            quota_states=quota_states_rep,
                                            prompt_bytes=prompt_bytes_rep,
                                            reserve_override=frozen_control_plane.reserve_override,
                                            policy=control_plane_policy,
                                            required_capabilities=frozen_control_plane.required_capabilities,
                                            control_plane_digest=frozen_control_plane.digest,
                                            binding_pool_id=repair_binding_pool_id,
                                            requires_governed_instructions=True,
                                            readiness_state=rep_readiness_state,
                                            readiness_reason=rep_readiness_reason,
                                            readiness_executable_available=rep_executable,
                                            explicit_unknown_readiness=is_explicit_rep,
                                        )
                                        self.db.record_event(
                                            run_id,
                                            "control_plane_decision",
                                            payload=decision_rep.as_payload(),
                                        )
                                        if reason_rep is None and repair_adapter is not None:
                                            selected_repair = (repair_candidate, repair_adapter)
                                            selected_repair_resolved = repair_resolved_candidate
                                            selected_repair_readiness = readiness_rep
                                            break
                                        last_reason = reason_rep or last_reason
                                        transport_rep = (
                                            repair_adapter.prompt_transport_for(prompt_bytes_rep)
                                            if repair_adapter
                                            else "unknown"
                                        )
                                        max_bytes_rep: int | None = None
                                        try:
                                            max_bytes_rep = (
                                                repair_adapter.max_prompt_bytes()
                                                if repair_adapter
                                                else None
                                            )
                                        except Exception:
                                            max_bytes_rep = None
                                        payload_rep = build_skip_payload(
                                            role=DispatchRole.REPAIR,
                                            candidate=repair_candidate,
                                            reason=reason_rep or "provider_unavailable",
                                            prompt_bytes=prompt_bytes_rep,
                                            max_prompt_bytes=max_bytes_rep,
                                            transport=transport_rep,
                                            explicit_override=is_explicit_rep,
                                        )
                                        payload_rep.update(
                                            {
                                                "outcome": DispatchOutcome.ELIGIBILITY_SKIP.value,
                                                "stage": DispatchStage.REPAIR.value,
                                                "tier": cur_tier.value,
                                            }
                                        )
                                        self.db.record_event(
                                            run_id, "repair_candidate_skipped", payload=payload_rep
                                        )
                                    if selected_repair is None:
                                        if is_explicit_rep:
                                            raise RuntimeError(
                                                "explicit repair model is ineligible: "
                                                f"{last_reason or 'unknown'}"
                                            )
                                        return None, None, None, False
                                    repair_candidate, cur_adapter = selected_repair
                                    repair_resolved = selected_repair_resolved
                                    cand_provider = repair_candidate.provider
                                    cand_model = repair_candidate.model
                                    attempt_number += 1
                                    local_attempt_id = f"{run_id}_a{attempt_number}"
                                    active_attempt_id = local_attempt_id
                                    local_branch = f"orch/{run_id}/a{attempt_number}"
                                    local_wt_path = base_wt_dir / run_id / f"a{attempt_number}"

                                    rep_launch_key: str | None = None
                                    if rep_continuation_attempt is not None:
                                        rep_launch_key = (
                                            f"replacement_launch:{run_id}:"
                                            f"{rep_continuation_attempt.id}:{attempt_number}"
                                        )
                                        self.db.journal_record_intent(
                                            idempotency_key=rep_launch_key,
                                            run_id=run_id,
                                            attempt_id=rep_continuation_attempt.id,
                                            operation=SideEffectOperation.REPLACEMENT_LAUNCH,
                                            payload={
                                                "replacement_attempt_number": attempt_number,
                                                "base_sha": cur_from_sha,
                                                "role": DispatchRole.REPAIR.value,
                                            },
                                        )

                                    try:
                                        GitManager.create_worktree(
                                            repo_path=repo,
                                            branch_name=local_branch,
                                            worktree_path=local_wt_path,
                                            start_point=cur_from_sha,
                                        )
                                    except Exception as exc:
                                        self.db.record_event(
                                            run_id,
                                            "worktree_creation_failed",
                                            payload={"error": str(exc), "attempt": attempt_number},
                                        )
                                        return None, None, None, False

                                    self.db.record_event(
                                        run_id,
                                        "repair_started",
                                        attempt_id=local_attempt_id,
                                        payload={
                                            "from_sha": cur_from_sha,
                                            "findings_count": findings_cnt,
                                            "tier": cur_tier.value,
                                            "tier_reason": repair_decision.source,
                                            "role": DispatchRole.REPAIR.value,
                                        },
                                    )
                                    rep_attempt = Attempt(
                                        id=local_attempt_id,
                                        run_id=run_id,
                                        attempt_number=attempt_number,
                                        provider=cand_provider,
                                        model=cand_model,
                                        worktree_path=str(local_wt_path),
                                        branch_name=local_branch,
                                        status=AttemptStatus.RUNNING,
                                        created_at=current_iso_timestamp(),
                                        base_sha=cur_from_sha,
                                    )
                                    self.db.create_attempt(rep_attempt)

                                    rep_activation = WorkerActivation(
                                        id=f"act_{uuid.uuid4().hex[:10]}",
                                        run_id=run_id,
                                        attempt_id=local_attempt_id,
                                        generation=attempt_number,
                                        state=WorkerActivationState.STARTING,
                                        created_at=current_iso_timestamp(),
                                        updated_at=current_iso_timestamp(),
                                    )
                                    self.db.create_worker_activation(rep_activation)

                                    if rep_launch_key and rep_continuation_attempt is not None:
                                        self.db.journal_record_completed(
                                            idempotency_key=rep_launch_key,
                                            result={
                                                "attempt_id": local_attempt_id,
                                                "activation_id": rep_activation.id,
                                            },
                                        )
                                        self.db.update_worker_activation_state(
                                            run_id,
                                            rep_continuation_attempt.attempt_number,
                                            WorkerActivationState.REPLACED,
                                        )

                                    self.db.record_event(
                                        run_id,
                                        "attempt_started",
                                        attempt_id=local_attempt_id,
                                        payload={
                                            "provider": cand_provider,
                                            "model": cand_model,
                                            "worktree": str(local_wt_path),
                                            "branch": local_branch,
                                            "inherited_base_sha": cur_from_sha,
                                            "role": DispatchRole.REPAIR.value,
                                        },
                                    )
                                    self.db.record_event(
                                        run_id,
                                        "worker_started",
                                        attempt_id=local_attempt_id,
                                        payload={
                                            "provider": cand_provider,
                                            "model": cand_model,
                                            "worktree": str(local_wt_path),
                                            "branch": local_branch,
                                            "prompt_bytes": prompt_bytes_rep,
                                            "transport": (
                                                cur_adapter.prompt_transport_for(prompt_bytes_rep)
                                                if cur_adapter
                                                else "unknown"
                                            ),
                                            "stage": "repair",
                                            "role": DispatchRole.REPAIR.value,
                                            "tier": cur_tier.value,
                                            "tier_reason": repair_decision.source,
                                            "package_id": repair_decision.package_id,
                                            "attempt": attempt_number,
                                            "inherited_base_sha": cur_from_sha,
                                        },
                                    )
                                    rep_timeout_sec = config.worker_timeout_for(cur_tier)
                                    rep_deadline = None
                                    if rep_timeout_sec is not None and rep_timeout_sec > 0:
                                        rep_deadline = (
                                            datetime.now(UTC) + timedelta(seconds=rep_timeout_sec)
                                        ).isoformat()
                                    self._ensure_supervision(
                                        run_id,
                                        local_attempt_id,
                                        deadline_at=rep_deadline,
                                        provider=cand_provider,
                                        model=cand_model,
                                    )

                                    def _rep_on_worker_start(
                                        r_id: str,
                                        att_id: str,
                                        att_num: int,
                                        db: Database,
                                    ) -> Callable[[dict[str, Any]], None]:
                                        def _cb(identity_dict: dict[str, Any]) -> None:
                                            try:
                                                import json as _json

                                                pid = identity_dict.get("pid")
                                                identity_str = _json.dumps(
                                                    identity_dict, sort_keys=True
                                                )
                                                db.set_worker_identity_fenced(
                                                    r_id,
                                                    att_id,
                                                    worker_pid=pid,
                                                    worker_identity=identity_str,
                                                )
                                                db.update_worker_activation_state(
                                                    r_id,
                                                    att_num,
                                                    WorkerActivationState.RUNNING,
                                                    worker_identity_json=identity_str,
                                                )
                                            except Exception:
                                                pass

                                        return _cb

                                    r_on_process_start = _rep_on_worker_start(
                                        run_id, local_attempt_id, attempt_number, self.db
                                    )

                                    r_on_stdout, r_on_stderr, r_on_stall, r_on_resume = (
                                        self._worker_activity_hooks(
                                            run_id, local_attempt_id, cand_provider, cand_model
                                        )
                                    )
                                    r_on_monitor_transition = self._monitor_transition_hook(
                                        run_id, local_attempt_id, cand_provider, cand_model
                                    )
                                    w_req = WorkerRequest(
                                        task=prompt_task,
                                        worktree_path=local_wt_path,
                                        model=cand_model,
                                        timeout_seconds=(
                                            rep_timeout_sec
                                            if rep_timeout_sec is not None
                                            else float("inf")
                                        ),
                                        inactivity_timeout=config.worker_inactivity_timeout,
                                        on_stdout=r_on_stdout,
                                        on_stderr=r_on_stderr,
                                        on_stall=r_on_stall,
                                        on_resume=r_on_resume,
                                        on_process_start=r_on_process_start,
                                        on_monitor_transition=r_on_monitor_transition,
                                        reasoning_effort=(
                                            repair_resolved.reasoning_effort
                                            if repair_resolved is not None
                                            else None
                                        ),
                                        # C09: run-scoped identity is always
                                        # provided (see the primary dispatch).
                                        env=_filtered_worker_env(
                                            {
                                                **os.environ,
                                                "ORCH_RUN_ID": run_id,
                                                "ORCH_ATTEMPT_ID": local_attempt_id,
                                                "ORCH_ASSOCIATION_ID": local_attempt_id,
                                                "ORCH_DB_PATH": str(self.db.db_path),
                                                **(
                                                    repair_resolved.overlay
                                                    if repair_resolved is not None
                                                    else {}
                                                ),
                                                **(
                                                    {
                                                        "ORCH_TOOL_TELEMETRY_SINK": str(
                                                            self.db.db_path.parent
                                                            / "tool-telemetry"
                                                            / f"{run_id}_{local_attempt_id}.jsonl"
                                                        ),
                                                        "ORCH_TOOL_CONTRACT_DIGEST": (
                                                            frozen_tools.digest
                                                        ),
                                                    }
                                                    if frozen_tools.bindings
                                                    else {}
                                                ),
                                            },
                                            authorization_env_var=_gateway_authorization_env_for(
                                                frozen_gateway
                                            ),
                                        ),
                                    )
                                    self.db.record_event(
                                        run_id,
                                        "context_projected",
                                        attempt_id=local_attempt_id,
                                        payload=cur_rendered_task.event_payload(),
                                    )
                                    if rep_continuation_attempt is not None:
                                        is_cold_takeover = (
                                            rep_continuation_attempt.interruption_reason
                                            == WorkerInterruptionReason.ABORTED.value
                                            and any(
                                                event.event_type
                                                == "attempt_recovered_cold_takeover"
                                                and event.attempt_id == rep_continuation_attempt.id
                                                for event in self.db.get_complete_events_for_run(
                                                    run_id
                                                )
                                            )
                                        )
                                        continuation_reason_map = {
                                            WorkerInterruptionReason.QUOTA_EXHAUSTED.value: DispatchReason.CONTINUATION_AFTER_QUOTA,  # noqa: E501
                                            WorkerInterruptionReason.CONTEXT_EXHAUSTED.value: DispatchReason.CONTINUATION_AFTER_CONTEXT,  # noqa: E501
                                            WorkerInterruptionReason.GRACEFUL_HANDOFF.value: DispatchReason.CONTINUATION_AFTER_GRACEFUL_HANDOFF,  # noqa: E501
                                        }
                                        repair_dispatch_reason = continuation_reason_map.get(
                                            rep_continuation_attempt.interruption_reason or "",
                                            DispatchReason.CONTINUATION_AFTER_GRACEFUL_HANDOFF,
                                        )
                                        if is_cold_takeover:
                                            repair_dispatch_reason = DispatchReason.COLD_TAKEOVER
                                    else:
                                        repair_dispatch_reason = (
                                            DispatchReason.PACKAGE_REVIEW_REPAIR
                                            if package_local is not None
                                            else DispatchReason.REVIEW_REPAIR
                                        )
                                    repair_handoff_id: str | None = None
                                    if rep_continuation_attempt is not None:
                                        repair_handoff = self.db.get_latest_handoff(
                                            run_id, kind="handoff_resume_capsule"
                                        )
                                        repair_handoff_id = (
                                            repair_handoff.id if repair_handoff else None
                                        )
                                    repair_dispatch_evidence = new_dispatch_evidence(
                                        run_id=run_id,
                                        association_id=local_attempt_id,
                                        attempt_id=local_attempt_id,
                                        stage=DispatchStage.REPAIR,
                                        role=DispatchRole.REPAIR,
                                        tier=cur_tier,
                                        dispatch_reason=repair_dispatch_reason,
                                        requested_provider=cand_provider,
                                        requested_model=cand_model,
                                        task=w_req.task,
                                        base_sha=cur_from_sha,
                                        inherited_checkpoint_sha=(
                                            cur_from_sha
                                            if rep_continuation_attempt is not None
                                            else None
                                        ),
                                        context_mode=cur_rendered_task.projection.mode.value,
                                        raw_parent_included=cur_rendered_task.full_task_included,
                                        prompt_transport=cur_adapter.prompt_transport_for(
                                            prompt_bytes_rep
                                        ),
                                        package_id=repair_decision.package_id,
                                        authority_mode=load_authority_settings().mode.value,
                                        routing_snapshot_digest=routing_snapshot.content_hash,
                                        control_plane_digest=frozen_control_plane.digest,
                                        prior_failure_refs=(
                                            [rep_continuation_attempt.id]
                                            if rep_continuation_attempt is not None
                                            else None
                                        ),
                                        handoff_id=repair_handoff_id,
                                        binding_id=(
                                            repair_resolved.binding_id
                                            if repair_resolved is not None
                                            else None
                                        ),
                                        profile_id=(
                                            repair_resolved.profile_id
                                            if repair_resolved is not None
                                            else None
                                        ),
                                        quota_pool_id=(
                                            repair_resolved.quota_pool_id
                                            if repair_resolved is not None
                                            else None
                                        ),
                                        reasoning_effort=(
                                            repair_resolved.reasoning_effort.value
                                            if repair_resolved is not None
                                            and repair_resolved.reasoning_effort is not None
                                            else None
                                        ),
                                    )
                                    metrics = tool_context_metrics(w_req.task, frozen_tools)
                                    if frozen_tools.bindings:
                                        repair_dispatch_evidence.tool_transport = ",".join(
                                            sorted(
                                                {
                                                    binding.transport.value
                                                    for binding in frozen_tools.bindings
                                                }
                                            )
                                        )
                                        repair_dispatch_evidence.tool_schema_bytes = metrics[
                                            "tool_schema_bytes"
                                        ]
                                        repair_dispatch_evidence.tool_description_bytes = metrics[
                                            "tool_description_bytes"
                                        ]
                                        repair_dispatch_evidence.tools_exposed_count = metrics[
                                            "tools_exposed_count"
                                        ]
                                    self.db.create_dispatch_evidence(repair_dispatch_evidence)
                                    if rep_continuation_attempt is not None:
                                        self.db.link_dispatch_replacement(
                                            run_id=run_id,
                                            source_attempt_id=rep_continuation_attempt.id,
                                            replacement_attempt_id=local_attempt_id,
                                            replacement_started_at=repair_dispatch_evidence.started_at,
                                            handoff_id=repair_handoff_id,
                                        )
                                        linked_dispatch = self.db.get_dispatch_evidence(
                                            repair_dispatch_evidence.id
                                        )
                                        if linked_dispatch is not None:
                                            repair_dispatch_evidence.handoff_bytes = (
                                                linked_dispatch.handoff_bytes
                                            )
                                            repair_dispatch_evidence.time_to_resume_seconds = (
                                                linked_dispatch.time_to_resume_seconds
                                            )
                                    # C10-R4: consult the optional gateway before
                                    # invoking the repair worker. Live state
                                    # is read per dispatch via the injectable
                                    # provider and never frozen.
                                    live_state_rep = GatewayLiveState()
                                    if frozen_gateway is not None and frozen_gateway.enabled:
                                        if self.gateway_live_state_provider is not None:
                                            try:
                                                live_state_rep = (
                                                    self.gateway_live_state_provider.get_live_state(
                                                        provider=cand_provider,
                                                        model=cand_model,
                                                        run_id=run_id,
                                                    )
                                                )
                                            except Exception:
                                                live_state_rep = GatewayLiveState()
                                    repair_gateway = frozen_gateway
                                    repair_gateway_endpoint = (
                                        frozen_gateway.endpoint
                                        if frozen_gateway is not None
                                        else None
                                    )
                                    repair_gateway_accounts = (
                                        frozen_gateway.accounts
                                        if frozen_gateway is not None
                                        else ()
                                    )
                                    repair_gateway_supports = bool(
                                        getattr(
                                            cur_adapter,
                                            "c10_gateway_transport_capabilities",
                                            frozenset(),
                                        )
                                    )
                                    repair_gateway_hint = live_state_rep.affinity_hint
                                    repair_gateway_unsatisfied = False
                                    if repair_resolved is not None:
                                        if repair_resolved.backend is BindingKind.DIRECT_CLI:
                                            repair_gateway = None
                                            repair_gateway_endpoint = None
                                            repair_gateway_accounts = ()
                                            repair_gateway_supports = False
                                        elif repair_resolved.backend is BindingKind.GATEWAY:
                                            if frozen_gateway is None or not frozen_gateway.enabled:
                                                repair_gateway_unsatisfied = True
                                            else:
                                                repair_gateway = _frozen_replace(
                                                    frozen_gateway,
                                                    direct_fallback_allowed=False,
                                                )
                                                if repair_resolved.gateway_account_pin:
                                                    repair_gateway_hint = AffinityHint(
                                                        run_id=run_id,
                                                        provider=cand_provider,
                                                        model=cand_model,
                                                        account_id=(
                                                            repair_resolved.gateway_account_pin
                                                        ),
                                                        created_at=current_iso_timestamp(),
                                                    )
                                    if repair_gateway_unsatisfied:
                                        self.db.record_event(
                                            run_id,
                                            "autonomous_gateway_binding_unsatisfied",
                                            attempt_id=local_attempt_id,
                                            payload={
                                                "binding_id": (
                                                    repair_resolved.binding_id
                                                    if repair_resolved is not None
                                                    else None
                                                ),
                                                "reason": "gateway_binding_without_enabled_gateway",
                                                "role": DispatchRole.REPAIR.value,
                                            },
                                        )
                                        rep_outcome = GatewayExecutionOutcome(
                                            worker_request=w_req,
                                            worker_result=WorkerResult(
                                                provider=cand_provider,
                                                model=cand_model,
                                                status=AttemptStatus.FAILED,
                                                summary=(
                                                    "GATEWAY binding unsatisfiable: no enabled "
                                                    "frozen gateway contract"
                                                ),
                                                stdout="",
                                                stderr="",
                                                error=(
                                                    "GATEWAY binding unsatisfiable: no enabled "
                                                    "frozen gateway contract; zero worker launch"
                                                ),
                                                outcome=DispatchOutcome.PROVIDER_OR_RUNTIME_FAILURE,
                                            ),
                                            gateway_evidence=None,
                                            disposition=ExecutionDisposition.GATEWAY_BLOCKED,
                                            worker_invoked=False,
                                        )
                                    else:
                                        rep_outcome = run_worker_with_gateway_boundary(
                                            frozen_gateway=repair_gateway,
                                            endpoint=repair_gateway_endpoint,
                                            base_request=w_req,
                                            provider=cand_provider,
                                            model=cand_model,
                                            run_adapter=self._bound_adapter_run(
                                                cur_adapter,
                                                autonomous=autonomous,
                                                attempt_id=local_attempt_id,
                                                run_id=run_id,
                                                repo_root=str(local_wt_path),
                                                provider=cand_provider,
                                                model=cand_model,
                                            ),
                                            adapter_supports_gateway=repair_gateway_supports,
                                            accounts=repair_gateway_accounts,
                                            health_state=live_state_rep.health_state,
                                            affinity_hint=repair_gateway_hint,
                                            quota_observations=live_state_rep.quota_observations,
                                            pool_observations=live_state_rep.pool_observations,
                                        )
                                    w_req = rep_outcome.worker_request
                                    gateway_evidence_rep = rep_outcome.gateway_evidence
                                    if gateway_evidence_rep is not None:
                                        repair_dispatch_evidence.gateway_evidence_json = (
                                            serialize_dispatch_evidence(gateway_evidence_rep)
                                        )
                                    w_res: WorkerResult | None = rep_outcome.worker_result
                                    if (
                                        rep_outcome.disposition
                                        == ExecutionDisposition.GATEWAY_BLOCKED
                                    ):
                                        actual_reason_rep = "gateway_blocked"
                                        if gateway_evidence_rep is not None:
                                            if gateway_evidence_rep.error is not None:
                                                actual_reason_rep = (
                                                    gateway_evidence_rep.error.message
                                                    or actual_reason_rep
                                                )
                                            else:
                                                actual_reason_rep = (
                                                    gateway_evidence_rep.evidence.get("reason")
                                                    or gateway_evidence_rep.evidence.get(
                                                        "fallback_reason"
                                                    )
                                                    or actual_reason_rep
                                                )
                                        w_res = WorkerResult(
                                            provider=cand_provider,
                                            model=cand_model,
                                            status=AttemptStatus.FAILED,
                                            summary="Gateway blocked repair dispatch",
                                            stdout="",
                                            stderr="",
                                            error=(
                                                f"Gateway execution blocked: {actual_reason_rep}"  # noqa: E501
                                            ),
                                            outcome=DispatchOutcome.PROVIDER_OR_RUNTIME_FAILURE,
                                        )
                                        # Record blocked event for repair as well
                                        self.db.record_event(
                                            run_id,
                                            "gateway_blocked",
                                            attempt_id=local_attempt_id,
                                            payload={
                                                "provider": cand_provider,
                                                "model": cand_model,
                                                "disposition": rep_outcome.disposition.value,
                                                "reason": actual_reason_rep,
                                            },
                                        )
                                    assert w_res is not None
                                    if (
                                        w_res.status is AttemptStatus.SUCCESS
                                        and selected_repair_readiness is not None
                                        and selected_repair_readiness.connection_id is not None
                                        and self.readiness_service is not None
                                    ):
                                        self.readiness_service.record_success(
                                            selected_repair_readiness.connection_id
                                        )
                                    try:
                                        if frozen_tools.bindings:
                                            records = read_telemetry(
                                                self.db.db_path.parent
                                                / "tool-telemetry"
                                                / f"{run_id}_{local_attempt_id}.jsonl"
                                            )
                                            used_refs = {str(record["ref"]) for record in records}
                                            repair_dispatch_evidence.tool_discovery_bytes = (
                                                _telemetry_total(records, "discovery_bytes")
                                            )
                                            repair_dispatch_evidence.tool_result_bytes = (
                                                _telemetry_total(records, "result_bytes")
                                            )
                                            repair_dispatch_evidence.tools_actually_used_count = (
                                                len(used_refs)
                                            )
                                    except Exception as exc:
                                        exception_result = WorkerResult(
                                            provider=cand_provider,
                                            model=cand_model,
                                            status=AttemptStatus.FAILED,
                                            summary="Repair adapter raised during dispatch",
                                            stdout="",
                                            stderr=str(exc),
                                            error=str(exc),
                                            outcome=DispatchOutcome.PROVIDER_OR_RUNTIME_FAILURE,
                                        )
                                        update_dispatch_from_result(
                                            repair_dispatch_evidence, exception_result
                                        )
                                        self.db.update_dispatch_evidence(repair_dispatch_evidence)
                                        persist_worker_usage(
                                            self.db,
                                            run_id=run_id,
                                            association_id=local_attempt_id,
                                            attempt_id=local_attempt_id,
                                            review_id=None,
                                            stage=DispatchStage.REPAIR,
                                            tier=cur_tier,
                                            provider=cand_provider,
                                            model=cand_model,
                                            prompt_bytes=prompt_bytes_rep,
                                            transport=cur_adapter.prompt_transport_for(
                                                prompt_bytes_rep
                                            ),
                                            result=exception_result,
                                        )
                                        self.db.record_event(
                                            run_id,
                                            "worker_completed",
                                            attempt_id=local_attempt_id,
                                            payload={
                                                "provider": cand_provider,
                                                "model": cand_model,
                                                "status": AttemptStatus.FAILED.value,
                                                "summary": f"Worker exception: {exc}",
                                                "stdout": "",
                                                "stderr": str(exc),
                                                "error": str(exc),
                                                "role": DispatchRole.REPAIR.value,
                                            },
                                        )
                                        now_exc2 = current_iso_timestamp()
                                        rep_attempt.status = AttemptStatus.FAILED
                                        rep_attempt.worker_error = str(exc)
                                        rep_attempt.completed_at = now_exc2
                                        self.db.update_attempt(
                                            attempt_id=local_attempt_id,
                                            status=AttemptStatus.FAILED,
                                            worker_error=rep_attempt.worker_error,
                                            completed_at=now_exc2,
                                        )
                                        self.db.update_worker_activation_state(
                                            run_id,
                                            attempt_number,
                                            WorkerActivationState.FAILED,
                                        )
                                        self.db.record_event(
                                            run_id,
                                            "worker_failed",
                                            attempt_id=local_attempt_id,
                                            payload={
                                                "error": str(exc),
                                                "status": AttemptStatus.FAILED.value,
                                                "role": DispatchRole.REPAIR.value,
                                            },
                                        )
                                        attempts.append(rep_attempt)
                                        active_attempt_id = None
                                        return rep_attempt, None, None, False

                                    repair_prompt_bytes = (
                                        w_res.prompt_bytes
                                        if w_res.prompt_bytes is not None
                                        else prompt_bytes_rep
                                    )
                                    repair_transport = w_res.prompt_transport_used or (
                                        cur_adapter.prompt_transport_for(prompt_bytes_rep)
                                    )
                                    update_dispatch_from_result(repair_dispatch_evidence, w_res)
                                    self.db.update_dispatch_evidence(repair_dispatch_evidence)
                                    persist_worker_usage(
                                        self.db,
                                        run_id=run_id,
                                        association_id=local_attempt_id,
                                        attempt_id=local_attempt_id,
                                        review_id=None,
                                        stage=DispatchStage.REPAIR,
                                        tier=cur_tier,
                                        provider=cand_provider,
                                        model=cand_model,
                                        prompt_bytes=repair_prompt_bytes,
                                        transport=repair_transport,
                                        result=w_res,
                                    )
                                    self.db.record_event(
                                        run_id,
                                        "worker_completed",
                                        attempt_id=local_attempt_id,
                                        payload={
                                            "provider": cand_provider,
                                            "model": cand_model,
                                            "status": w_res.status.value,
                                            "summary": w_res.summary,
                                            "stdout": _bounded_output(w_res.stdout),
                                            "stderr": _bounded_output(w_res.stderr),
                                            "error": w_res.error,
                                            "prompt_bytes": repair_prompt_bytes,
                                            "transport": repair_transport,
                                            "stage": "repair",
                                            "role": DispatchRole.REPAIR.value,
                                            "tier": cur_tier.value,
                                            "attempt": attempt_number,
                                        },
                                    )

                                    if (
                                        w_res.interruption_reason is not None
                                        and w_res.interruption_reason
                                        in (
                                            WorkerInterruptionReason.QUOTA_EXHAUSTED,
                                            WorkerInterruptionReason.CONTEXT_EXHAUSTED,
                                            WorkerInterruptionReason.GRACEFUL_HANDOFF,
                                            WorkerInterruptionReason.ABORTED,
                                        )
                                    ):
                                        self.db.update_worker_activation_state(
                                            run_id,
                                            attempt_number,
                                            WorkerActivationState.QUIESCING,
                                        )
                                        changed_paths_rep = GitManager.get_changed_paths(
                                            local_wt_path
                                        )
                                        cp_obj_rep: WorkerCheckpoint | None = None
                                        if changed_paths_rep:
                                            cp_idempotency_key = (
                                                f"checkpoint:{run_id}:{local_attempt_id}"
                                            )
                                            self.db.journal_record_intent(
                                                idempotency_key=cp_idempotency_key,
                                                run_id=run_id,
                                                attempt_id=local_attempt_id,
                                                operation=SideEffectOperation.CHECKPOINT_CREATION,
                                                payload={
                                                    "reason": w_res.interruption_reason.value,
                                                    "role": DispatchRole.REPAIR.value,
                                                },
                                            )
                                            cp_sha = GitManager.create_checkpoint_commit(
                                                worktree_path=local_wt_path,
                                                message=(
                                                    f"orch repair checkpoint("
                                                    f"{run_id}/{local_attempt_id}): "
                                                    f"{w_res.interruption_reason.value}"
                                                ),
                                            )
                                            if cp_sha is not None:
                                                cp_obj_rep = WorkerCheckpoint(
                                                    id=f"cp_{uuid.uuid4().hex[:10]}",
                                                    run_id=run_id,
                                                    source_attempt_id=local_attempt_id,
                                                    source_base_sha=cur_from_sha,
                                                    checkpoint_sha=cp_sha,
                                                    reason=w_res.interruption_reason.value,
                                                    changed_paths=tuple(changed_paths_rep),
                                                    created_at=current_iso_timestamp(),
                                                )
                                                self.db.create_worker_checkpoint(cp_obj_rep)
                                                self.db.journal_record_completed(
                                                    idempotency_key=cp_idempotency_key,
                                                    result={
                                                        "checkpoint_sha": cp_sha,
                                                        "checkpoint_id": cp_obj_rep.id,
                                                    },
                                                )
                                                self.db.update_worker_activation_state(
                                                    run_id,
                                                    attempt_number,
                                                    WorkerActivationState.CHECKPOINTED,
                                                )
                                                self.db.record_event(
                                                    run_id,
                                                    "worker_checkpoint_created",
                                                    attempt_id=local_attempt_id,
                                                    payload={
                                                        "checkpoint_sha": cp_sha,
                                                        "source_attempt_id": local_attempt_id,
                                                        "reason": w_res.interruption_reason.value,
                                                        "changed_paths": list(changed_paths_rep),
                                                        "role": DispatchRole.REPAIR.value,
                                                    },
                                                )
                                                cur_from_sha = cp_sha
                                        else:
                                            self.db.update_worker_activation_state(
                                                run_id,
                                                attempt_number,
                                                WorkerActivationState.INTERRUPTED,
                                            )
                                            self.db.record_event(
                                                run_id,
                                                "worker_interrupted_no_changes",
                                                attempt_id=local_attempt_id,
                                                payload={
                                                    "reason": w_res.interruption_reason.value,
                                                    "role": DispatchRole.REPAIR.value,
                                                },
                                            )

                                        now_int = current_iso_timestamp()
                                        rep_attempt.status = AttemptStatus.FAILED
                                        rep_attempt.interruption_reason = (
                                            w_res.interruption_reason.value
                                        )
                                        rep_attempt.worker_stdout = w_res.stdout
                                        rep_attempt.worker_stderr = w_res.stderr
                                        rep_attempt.worker_error = (
                                            f"Repair worker interrupted: "
                                            f"{w_res.interruption_reason.value}"
                                        )
                                        rep_attempt.completed_at = now_int
                                        self.db.update_attempt(
                                            attempt_id=local_attempt_id,
                                            status=AttemptStatus.FAILED,
                                            worker_stdout=w_res.stdout,
                                            worker_stderr=w_res.stderr,
                                            worker_error=rep_attempt.worker_error,
                                            completed_at=now_int,
                                            interruption_reason=w_res.interruption_reason.value,
                                        )
                                        self.db.record_event(
                                            run_id,
                                            "worker_interrupted",
                                            attempt_id=local_attempt_id,
                                            payload={
                                                "reason": w_res.interruption_reason.value,
                                                "provider": cand_provider,
                                                "model": cand_model,
                                                "has_changes": len(changed_paths_rep) > 0,
                                                "checkpoint_sha": (
                                                    cp_obj_rep.checkpoint_sha
                                                    if cp_obj_rep
                                                    else None
                                                ),
                                                "role": DispatchRole.REPAIR.value,
                                            },
                                        )
                                        self.db.record_event(
                                            run_id,
                                            "dispatch_outcome_classified",
                                            attempt_id=local_attempt_id,
                                            payload={
                                                "outcome": (
                                                    DispatchOutcome.PROVIDER_OR_RUNTIME_FAILURE.value
                                                ),
                                                "escalation_evidence": False,
                                                "role": DispatchRole.REPAIR.value,
                                                "reason": w_res.interruption_reason.value,
                                            },
                                        )

                                        if (
                                            w_res.interruption_reason
                                            == WorkerInterruptionReason.QUOTA_EXHAUSTED
                                        ):
                                            blocked_repair_providers.add(cand_provider)

                                        rep_continuation_attempt = rep_attempt
                                        attempts.append(rep_attempt)
                                        active_attempt_id = None

                                        proj = build_continuation_projection(
                                            original_task=config.task,
                                            run_id=run_id,
                                            attempt_idx=attempt_number + 1,
                                            checkpoint_sha=cur_from_sha,
                                            last_verified_candidate_sha=from_sha_local,
                                            interruption_reason=w_res.interruption_reason.value,
                                            previous_provider=cand_provider,
                                            previous_model=cand_model,
                                            changed_paths=tuple(changed_paths_rep),
                                            diff_summary=w_res.summary,
                                            role="repair",
                                            package=package_local,
                                        )
                                        cur_rendered_task = render_projection(
                                            proj,
                                            validation_policy_text=VALIDATION_POLICY_TEXT,
                                            validation_policy_marker=VALIDATION_POLICY_MARKER,
                                            tool_contract=frozen_tools,
                                            project_scope_text=scope_section_for_run(
                                                self.db,
                                                run_id,
                                                (
                                                    package_local.scope_json
                                                    if package_local is not None
                                                    else None
                                                ),
                                            ),
                                        )
                                        continue

                                    if w_res.status != AttemptStatus.SUCCESS:
                                        now2 = current_iso_timestamp()
                                        rep_attempt.status = w_res.status
                                        rep_attempt.worker_stdout = w_res.stdout
                                        rep_attempt.worker_stderr = w_res.stderr
                                        rep_attempt.worker_error = w_res.error
                                        rep_attempt.completed_at = now2
                                        self.db.update_attempt(
                                            attempt_id=local_attempt_id,
                                            status=w_res.status,
                                            worker_stdout=w_res.stdout,
                                            worker_stderr=w_res.stderr,
                                            worker_error=w_res.error,
                                            completed_at=now2,
                                        )
                                        self.db.update_worker_activation_state(
                                            run_id,
                                            attempt_number,
                                            (
                                                WorkerActivationState.TIMED_OUT
                                                if w_res.status == AttemptStatus.TIMEOUT
                                                else WorkerActivationState.FAILED
                                            ),
                                        )
                                        changed_paths_rep2: list[str] = []
                                        try:
                                            changed_paths_rep2 = GitManager.get_changed_paths(
                                                local_wt_path
                                            )
                                        except Exception:
                                            pass
                                        self.db.record_event(
                                            run_id,
                                            "worker_failed",
                                            attempt_id=local_attempt_id,
                                            payload={
                                                "error": w_res.error,
                                                "status": w_res.status.value,
                                                "provider": cand_provider,
                                                "model": cand_model,
                                                "duration_seconds": w_res.duration_seconds,
                                                "changed_paths": changed_paths_rep2,
                                                "worktree_path": str(local_wt_path),
                                                "transcript_path": str(
                                                    get_transcript_path(self.db, run_id)
                                                ),
                                                "role": DispatchRole.REPAIR.value,
                                            },
                                        )
                                        attempts.append(rep_attempt)
                                        active_attempt_id = None
                                        return rep_attempt, None, None, False

                                    break
                                has_changes_local = GitManager.has_diff_against_base(
                                    local_wt_path, from_sha_local
                                )
                                if not has_changes_local:
                                    now2 = current_iso_timestamp()
                                    rep_attempt.status = AttemptStatus.FAILED
                                    rep_attempt.worker_stdout = w_res.stdout
                                    rep_attempt.worker_stderr = w_res.stderr
                                    err2 = "Worker exited without making changes (no_changes)"
                                    rep_attempt.worker_error = err2
                                    rep_attempt.completed_at = now2
                                    self.db.update_attempt(
                                        attempt_id=local_attempt_id,
                                        status=AttemptStatus.FAILED,
                                        worker_stdout=w_res.stdout,
                                        worker_stderr=w_res.stderr,
                                        worker_error=rep_attempt.worker_error,
                                        completed_at=now2,
                                    )
                                    self.db.record_event(
                                        run_id,
                                        "attempt_failed_no_changes",
                                        attempt_id=local_attempt_id,
                                        payload={
                                            "reason": "no_changes",
                                            "role": DispatchRole.REPAIR.value,
                                        },
                                    )
                                    attempts.append(rep_attempt)
                                    active_attempt_id = None
                                    # Signal no-diff for stall detection
                                    return rep_attempt, None, None, True
                                to_sha_local = GitManager.commit_changes(
                                    worktree_path=local_wt_path,
                                    message=(
                                        f"orch({run_id}): candidate changes for "
                                        f"attempt {attempt_number} "
                                        f"({cand_provider}/{cand_model})"
                                    ),
                                )
                                repair_dispatch_evidence.candidate_sha = to_sha_local
                                self.db.update_dispatch_evidence(repair_dispatch_evidence)
                                self.db.record_event(
                                    run_id,
                                    "gate_started",
                                    attempt_id=local_attempt_id,
                                    payload={
                                        "gate_command": gate_command,
                                        "candidate_sha": to_sha_local,
                                        "role": DispatchRole.REPAIR.value,
                                    },
                                )
                                gate_args_local = split_command(gate_command)
                                gate_res_local = run_process(
                                    gate_args_local, cwd=local_wt_path, timeout=config.gate_timeout
                                )
                                changed_paths_local_t = tuple(
                                    GitManager.get_changed_paths(local_wt_path)
                                )
                                rep_disposition = _classify_gate_outcome(
                                    gate_res_local,
                                    mutated_worktree=False,
                                    changed_paths=changed_paths_local_t,
                                    candidate_sha=to_sha_local,
                                )
                                completed_local_payload: dict[str, Any] = {
                                    "gate_command": gate_command,
                                    "candidate_sha": to_sha_local,
                                    "stdout": _bounded_output(gate_res_local.stdout),
                                    "stderr": _bounded_output(gate_res_local.stderr),
                                    "changed_paths": list(changed_paths_local_t),
                                    "role": DispatchRole.REPAIR.value,
                                }
                                completed_local_payload.update(
                                    _gate_disposition_payload(rep_disposition)
                                )
                                self.db.record_event(
                                    run_id,
                                    "gate_completed",
                                    attempt_id=local_attempt_id,
                                    payload=completed_local_payload,
                                )
                                check_id_local = f"check_{uuid.uuid4().hex[:10]}"
                                check_local = CheckResult(
                                    id=check_id_local,
                                    attempt_id=local_attempt_id,
                                    gate_command=gate_command,
                                    exit_code=gate_res_local.exit_code,
                                    stdout=gate_res_local.stdout,
                                    stderr=gate_res_local.stderr,
                                    passed=gate_res_local.passed,
                                    created_at=current_iso_timestamp(),
                                )
                                self.db.create_check(check_local)
                                checks.append(check_local)
                                if rep_disposition.outcome is not GateOutcome.PASS:
                                    now2 = current_iso_timestamp()
                                    rep_attempt.status = AttemptStatus.FAILED
                                    rep_attempt.worker_stdout = w_res.stdout
                                    rep_attempt.worker_stderr = w_res.stderr
                                    err_msg_local = (
                                        f"Gate check failed "
                                        f"(gate outcome {rep_disposition.outcome.value}, "
                                        f"exit code {gate_res_local.exit_code})"
                                    )
                                    rep_attempt.worker_error = err_msg_local
                                    rep_attempt.commit_sha = to_sha_local
                                    rep_attempt.completed_at = now2
                                    self.db.update_attempt(
                                        attempt_id=local_attempt_id,
                                        status=AttemptStatus.FAILED,
                                        worker_stdout=w_res.stdout,
                                        worker_stderr=w_res.stderr,
                                        worker_error=rep_attempt.worker_error,
                                        commit_sha=to_sha_local,
                                        completed_at=now2,
                                    )
                                    gate_failed_payload: dict[str, Any] = {
                                        "candidate_sha": to_sha_local,
                                        "role": DispatchRole.REPAIR.value,
                                    }
                                    gate_failed_payload.update(
                                        _gate_disposition_payload(rep_disposition)
                                    )
                                    self.db.record_event(
                                        run_id,
                                        "gate_failed",
                                        attempt_id=local_attempt_id,
                                        payload=gate_failed_payload,
                                    )
                                    self.db.update_worker_activation_state(
                                        run_id,
                                        attempt_number,
                                        WorkerActivationState.FAILED,
                                    )
                                    attempts.append(rep_attempt)
                                    active_attempt_id = None
                                    return rep_attempt, to_sha_local, local_wt_path, False
                                now2 = current_iso_timestamp()
                                rep_attempt.status = AttemptStatus.SUCCESS
                                rep_attempt.worker_stdout = w_res.stdout
                                rep_attempt.worker_stderr = w_res.stderr
                                rep_attempt.commit_sha = to_sha_local
                                rep_attempt.completed_at = now2
                                self.db.update_attempt(
                                    attempt_id=local_attempt_id,
                                    status=AttemptStatus.SUCCESS,
                                    worker_stdout=w_res.stdout,
                                    worker_stderr=w_res.stderr,
                                    commit_sha=to_sha_local,
                                    completed_at=now2,
                                )
                                self.db.update_worker_activation_state(
                                    run_id,
                                    attempt_number,
                                    WorkerActivationState.SUCCEEDED,
                                )
                                self.db.record_event(
                                    run_id,
                                    "attempt_succeeded",
                                    attempt_id=local_attempt_id,
                                    payload={
                                        "commit_sha": to_sha_local,
                                        "branch": local_branch,
                                        "changed_paths": list(changed_paths_local_t),
                                    },
                                )
                                self.db.record_event(
                                    run_id,
                                    "repair_completed",
                                    attempt_id=local_attempt_id,
                                    payload={
                                        "from_sha": from_sha_local,
                                        "to_sha": to_sha_local,
                                        "findings_count": findings_cnt,
                                        "role": DispatchRole.REPAIR.value,
                                    },
                                )
                                # Repair candidate reached the reviewable
                                # boundary (gate passed, committed, quiescent
                                # worktree).  Publication and the
                                # candidate-ready transition stay deferred
                                # until required review passes terminally;
                                # this repair round only records its
                                # candidate for the next review round.
                                review_publish_attempt_id = local_attempt_id
                                summary_local = (
                                    f"Task completed successfully on attempt {attempt_number} "
                                    f"({cand_provider}/{cand_model}). "
                                    f"Gate '{gate_command}' PASSED."
                                )
                                attempts.append(rep_attempt)
                                active_attempt_id = None
                                # Update summary for outer scope  # noqa: B023
                                nonlocal_summary[0] = summary_local  # noqa: B023
                                return rep_attempt, to_sha_local, local_wt_path, False

                            nonlocal_summary: list[str] = [summary]
                            if config.review_mode == "bounded":
                                outstanding: list[ReviewResult] = []
                                while True:
                                    launched_so_far = self.db.count_launched_reviews(run_id)
                                    remaining_budget = max(0, config.review_limit - launched_so_far)
                                    next_attempt = len(reviews) + 1
                                    is_final = next_attempt == config.review_limit
                                    # Pre-final aggregated repair before final review
                                    if is_final and outstanding:
                                        from_sha_pre = (
                                            current_commit_sha
                                            or GitManager.get_head_commit(current_wt_path)
                                        )
                                        final_repair_task = _render_final_repair_task(
                                            original_task=task,
                                            attempt_idx=attempt_number + 1,
                                            from_sha=from_sha_pre,
                                            reviews=outstanding,
                                            package=current_package,
                                            tool_contract=frozen_tools,
                                        )
                                        rep, to_sha_p, wt_p, is_no_diff = _do_repair(
                                            from_sha_pre,
                                            final_repair_task,
                                            len(outstanding),
                                            "\n".join(
                                                f"{item.summary}\n{item.details}"
                                                for item in outstanding
                                            ),
                                        )
                                        if rep is None:
                                            self.db.update_review_loop_state(
                                                run_id,
                                                current_attempt=next_attempt,
                                                is_final=is_final,
                                                terminal_verdict=ReviewVerdict.REVIEW_EXECUTION_FAILED.value,
                                            )
                                            break
                                        if is_no_diff:
                                            self.db.update_review_loop_state(
                                                run_id,
                                                current_attempt=next_attempt,
                                                is_final=is_final,
                                                terminal_verdict=ReviewVerdict.CONVERGENCE_STALLED.value,
                                            )
                                            break
                                        if rep.status != AttemptStatus.SUCCESS:
                                            self.db.update_review_loop_state(
                                                run_id,
                                                current_attempt=next_attempt,
                                                is_final=is_final,
                                                terminal_verdict=ReviewVerdict.REVIEW_EXECUTION_FAILED.value,
                                            )
                                            break
                                        # Update current candidate to final-repaired version
                                        assert to_sha_p is not None and wt_p is not None
                                        current_commit_sha = to_sha_p
                                        current_branch = rep.branch_name
                                        current_wt_path = wt_p
                                        # Clear outstanding after aggregated repair (consider them addressed)  # noqa: E501
                                        # Keep for record but not needed
                                    # Persist per-run review loop state durably before dispatch
                                    # (survives Database reopen mid-loop).
                                    self.db.update_review_loop_state(
                                        run_id,
                                        current_attempt=next_attempt,
                                        is_final=is_final,
                                    )
                                    # Launch review (final or not)
                                    rev_res = review_engine.review_run(
                                        run_id,
                                        timeout=config.review_timeout,
                                        review_limit=remaining_budget,
                                        is_final=is_final,
                                        final_attempt=next_attempt,
                                        final_limit=config.review_limit,
                                    )
                                    reviews.append(rev_res)
                                    # Gate final-only verdicts: on non-final reviews FINAL_BLOCK
                                    # and PASS_WITH_BACKLOG deterministically map to BLOCK.
                                    effective_verdict = rev_res.verdict
                                    if not is_final and effective_verdict in (
                                        ReviewVerdict.FINAL_BLOCK,
                                        ReviewVerdict.PASS_WITH_BACKLOG,
                                    ):
                                        effective_verdict = ReviewVerdict.BLOCK
                                    if is_final:
                                        # Final review is always terminal; handle verdicts
                                        # Even if BLOCK, we treat as terminal (no further repair)
                                        self.db.update_review_loop_state(
                                            run_id,
                                            current_attempt=next_attempt,
                                            is_final=is_final,
                                            terminal_verdict=rev_res.verdict.value,
                                        )
                                        break
                                    if effective_verdict == ReviewVerdict.BLOCK:
                                        if supervisor is not None and not supervisor.allows_repair:
                                            # Persisted owner authority
                                            # forbids autonomous repair
                                            # (SUPERVISED): the BLOCK stands
                                            # terminally and fails closed
                                            # below -- no repair dispatch.
                                            self.db.update_review_loop_state(
                                                run_id,
                                                current_attempt=next_attempt,
                                                is_final=is_final,
                                                terminal_verdict=ReviewVerdict.BLOCK.value,
                                            )
                                            break
                                        outstanding.append(rev_res)
                                        # Normal single-finding repair
                                        from_sha2 = (
                                            current_commit_sha
                                            or GitManager.get_head_commit(  # noqa: E501
                                                current_wt_path
                                            )
                                        )
                                        single_task = _render_repair_task(
                                            original_task=task,
                                            attempt_idx=attempt_number + 1,
                                            from_sha=from_sha2,
                                            review=rev_res,
                                            package=current_package,
                                            tool_contract=frozen_tools,
                                        )
                                        rep2, to_sha2, wt2, is_no_diff2 = _do_repair(
                                            from_sha2,
                                            single_task,
                                            1,
                                            f"{rev_res.summary}\n{rev_res.details}",
                                        )
                                        if rep2 is None:
                                            self.db.update_review_loop_state(
                                                run_id,
                                                current_attempt=next_attempt,
                                                is_final=is_final,
                                                terminal_verdict=ReviewVerdict.REVIEW_EXECUTION_FAILED.value,
                                            )
                                            break
                                        if is_no_diff2:
                                            self.db.update_review_loop_state(
                                                run_id,
                                                current_attempt=next_attempt,
                                                is_final=is_final,
                                                terminal_verdict=ReviewVerdict.CONVERGENCE_STALLED.value,
                                            )
                                            break
                                        if rep2.status != AttemptStatus.SUCCESS:
                                            self.db.update_review_loop_state(
                                                run_id,
                                                current_attempt=next_attempt,
                                                is_final=is_final,
                                                terminal_verdict=ReviewVerdict.REVIEW_EXECUTION_FAILED.value,
                                            )
                                            break
                                        assert to_sha2 is not None and wt2 is not None
                                        current_commit_sha = to_sha2
                                        current_branch = rep2.branch_name
                                        current_wt_path = wt2
                                        outstanding.clear()
                                        continue
                                    else:
                                        # PASS, PASS_WITH_BACKLOG, FINAL_BLOCK, REVIEW_EXECUTION_FAILED etc all terminal  # noqa: E501
                                        self.db.update_review_loop_state(
                                            run_id,
                                            current_attempt=next_attempt,
                                            is_final=is_final,
                                            terminal_verdict=rev_res.verdict.value,
                                        )
                                        break
                                summary = nonlocal_summary[0]
                            else:
                                # MAX mode: autonomous loop with stall guard
                                seen_shas: set[str] = set()
                                if current_commit_sha:
                                    seen_shas.add(current_commit_sha)
                                prev_blocker_norm: str | None = None
                                prev_output_norm: str | None = None
                                while True:
                                    # Persist per-run review loop state durably before dispatch
                                    self.db.update_review_loop_state(
                                        run_id,
                                        current_attempt=len(reviews) + 1,
                                        is_final=False,
                                    )
                                    # Unlimited budget for max
                                    rev_res = review_engine.review_run(
                                        run_id,
                                        timeout=config.review_timeout,
                                        review_limit=9999,
                                    )
                                    reviews.append(rev_res)
                                    # Gate final-only verdicts: max mode is always non-final, so
                                    # FINAL_BLOCK and PASS_WITH_BACKLOG map to BLOCK.
                                    effective_verdict = rev_res.verdict
                                    if effective_verdict in (
                                        ReviewVerdict.FINAL_BLOCK,
                                        ReviewVerdict.PASS_WITH_BACKLOG,
                                    ):
                                        effective_verdict = ReviewVerdict.BLOCK
                                    # Stall detection: identical reviewer output
                                    curr_output_norm = _normalize(rev_res.details) or _normalize(
                                        rev_res.summary
                                    )
                                    if prev_output_norm is not None and curr_output_norm:
                                        if prev_output_norm == curr_output_norm:
                                            self.db.record_event(
                                                run_id,
                                                "convergence_stalled",
                                                payload={"reason": "identical_reviewer_output"},
                                            )
                                            stalled = ReviewResult(
                                                id=f"rev_{uuid.uuid4().hex[:10]}",
                                                run_id=run_id,
                                                provider="",
                                                model="",
                                                verdict=ReviewVerdict.CONVERGENCE_STALLED,
                                                summary="Convergence stalled: identical reviewer output",  # noqa: E501
                                                details="Reason: identical_reviewer_output",
                                                created_at=current_iso_timestamp(),
                                            )
                                            self.db.create_review(stalled)
                                            self.db.record_event(
                                                run_id,
                                                "review_completed",
                                                payload={
                                                    "review_id": stalled.id,
                                                    "verdict": stalled.verdict.value,
                                                    "summary": stalled.summary,
                                                },
                                            )
                                            reviews.append(stalled)
                                            self.db.update_review_loop_state(
                                                run_id,
                                                current_attempt=len(reviews),
                                                is_final=False,
                                                terminal_verdict=stalled.verdict.value,
                                            )
                                            break
                                    # Stall detection: identical blocker set repeats twice consecutively  # noqa: E501
                                    if effective_verdict == ReviewVerdict.BLOCK:
                                        curr_blocker_norm = _normalize(
                                            rev_res.summary + "\n" + rev_res.details
                                        )
                                        if prev_blocker_norm is not None and curr_blocker_norm:
                                            if prev_blocker_norm == curr_blocker_norm:
                                                self.db.record_event(
                                                    run_id,
                                                    "convergence_stalled",
                                                    payload={"reason": "identical_blocker_set"},
                                                )
                                                stalled = ReviewResult(
                                                    id=f"rev_{uuid.uuid4().hex[:10]}",
                                                    run_id=run_id,
                                                    provider="",
                                                    model="",
                                                    verdict=ReviewVerdict.CONVERGENCE_STALLED,
                                                    summary="Convergence stalled: identical blocker set",  # noqa: E501
                                                    details="Reason: identical_blocker_set",
                                                    created_at=current_iso_timestamp(),
                                                )
                                                self.db.create_review(stalled)
                                                self.db.record_event(
                                                    run_id,
                                                    "review_completed",
                                                    payload={
                                                        "review_id": stalled.id,
                                                        "verdict": stalled.verdict.value,
                                                        "summary": stalled.summary,
                                                    },
                                                )
                                                reviews.append(stalled)
                                                self.db.update_review_loop_state(
                                                    run_id,
                                                    current_attempt=len(reviews),
                                                    is_final=False,
                                                    terminal_verdict=stalled.verdict.value,
                                                )
                                                break
                                        prev_blocker_norm = curr_blocker_norm
                                    else:
                                        # Reset blocker tracking on non-BLOCK? Keep last?
                                        pass
                                    if prev_output_norm is not None or curr_output_norm:
                                        prev_output_norm = curr_output_norm

                                    if effective_verdict in (
                                        ReviewVerdict.PASS,
                                        ReviewVerdict.PASS_WITH_BACKLOG,
                                    ):
                                        self.db.update_review_loop_state(
                                            run_id,
                                            current_attempt=len(reviews),
                                            is_final=False,
                                            terminal_verdict=effective_verdict.value,
                                        )
                                        break
                                    if effective_verdict != ReviewVerdict.BLOCK:
                                        # For max, non-BLOCK non-PASS (e.g., execution failed) we continue? But to avoid infinite, break  # noqa: E501
                                        # To allow stall guard to work, we treat REVIEW_EXECUTION_FAILED as not stalled but break after  # noqa: E501
                                        self.db.update_review_loop_state(
                                            run_id,
                                            current_attempt=len(reviews),
                                            is_final=False,
                                            terminal_verdict=effective_verdict.value,
                                        )
                                        break
                                    # BLOCK -> repair (forbidden without repair authority)
                                    if supervisor is not None and not supervisor.allows_repair:
                                        self.db.update_review_loop_state(
                                            run_id,
                                            current_attempt=len(reviews),
                                            is_final=False,
                                            terminal_verdict=ReviewVerdict.BLOCK.value,
                                        )
                                        break
                                    from_sha_m = current_commit_sha or GitManager.get_head_commit(
                                        current_wt_path
                                    )
                                    single_task_m = _render_repair_task(
                                        original_task=task,
                                        attempt_idx=attempt_number + 1,
                                        from_sha=from_sha_m,
                                        review=rev_res,
                                        package=current_package,
                                        tool_contract=frozen_tools,
                                    )
                                    rep_m, to_sha_m, wt_m, is_no_diff_m = _do_repair(
                                        from_sha_m,
                                        single_task_m,
                                        1,
                                        f"{rev_res.summary}\n{rev_res.details}",
                                    )
                                    if rep_m is None:
                                        self.db.update_review_loop_state(
                                            run_id,
                                            current_attempt=len(reviews),
                                            is_final=False,
                                            terminal_verdict=ReviewVerdict.REVIEW_EXECUTION_FAILED.value,
                                        )
                                        break
                                    if is_no_diff_m:
                                        self.db.record_event(
                                            run_id,
                                            "convergence_stalled",
                                            payload={"reason": "repair_no_diff"},
                                        )
                                        stalled = ReviewResult(
                                            id=f"rev_{uuid.uuid4().hex[:10]}",
                                            run_id=run_id,
                                            provider="",
                                            model="",
                                            verdict=ReviewVerdict.CONVERGENCE_STALLED,
                                            summary="Convergence stalled: repair produced no diff",
                                            details="Reason: repair_no_diff",
                                            created_at=current_iso_timestamp(),
                                        )
                                        self.db.create_review(stalled)
                                        self.db.record_event(
                                            run_id,
                                            "review_completed",
                                            payload={
                                                "review_id": stalled.id,
                                                "verdict": stalled.verdict.value,
                                                "summary": stalled.summary,
                                            },
                                        )
                                        reviews.append(stalled)
                                        self.db.update_review_loop_state(
                                            run_id,
                                            current_attempt=len(reviews),
                                            is_final=False,
                                            terminal_verdict=stalled.verdict.value,
                                        )
                                        break
                                    if rep_m.status != AttemptStatus.SUCCESS:
                                        self.db.update_review_loop_state(
                                            run_id,
                                            current_attempt=len(reviews),
                                            is_final=False,
                                            terminal_verdict=ReviewVerdict.REVIEW_EXECUTION_FAILED.value,
                                        )
                                        break
                                    assert to_sha_m is not None and wt_m is not None
                                    # Stall: candidate SHA repeats
                                    if to_sha_m in seen_shas:
                                        self.db.record_event(
                                            run_id,
                                            "convergence_stalled",
                                            payload={"reason": "candidate_sha_repeats"},
                                        )
                                        stalled = ReviewResult(
                                            id=f"rev_{uuid.uuid4().hex[:10]}",
                                            run_id=run_id,
                                            provider="",
                                            model="",
                                            verdict=ReviewVerdict.CONVERGENCE_STALLED,
                                            summary="Convergence stalled: candidate SHA repeats",
                                            details="Reason: candidate_sha_repeats",
                                            created_at=current_iso_timestamp(),
                                        )
                                        self.db.create_review(stalled)
                                        self.db.record_event(
                                            run_id,
                                            "review_completed",
                                            payload={
                                                "review_id": stalled.id,
                                                "verdict": stalled.verdict.value,
                                                "summary": stalled.summary,
                                            },
                                        )
                                        reviews.append(stalled)
                                        self.db.update_review_loop_state(
                                            run_id,
                                            current_attempt=len(reviews),
                                            is_final=False,
                                            terminal_verdict=stalled.verdict.value,
                                        )
                                        break
                                    seen_shas.add(to_sha_m)
                                    current_commit_sha = to_sha_m
                                    current_branch = rep_m.branch_name
                                    current_wt_path = wt_m
                                summary = nonlocal_summary[0]

                            commit_sha = current_commit_sha
                            branch_name = current_branch
                            wt_path = current_wt_path

                            # Pre-accept verification gate (fail closed).
                            # Only a substantive PASS continues toward
                            # candidate publication and the candidate-ready
                            # transition.  BLOCK / FINAL_BLOCK /
                            # REVIEW_UNAVAILABLE / REVIEW_EXECUTION_FAILED /
                            # CONVERGENCE_STALLED never become
                            # candidate-ready, published, or accepted.
                            review_terminal = _terminal_review_verdict(reviews)
                            if review_terminal == ReviewVerdict.PASS:
                                # Required review passed: publish the exact
                                # terminal candidate, then the single
                                # candidate-ready transition.  Publication
                                # failure raises CandidatePublicationError
                                # and fails the run closed (never COMPLETED).
                                candidate_publication = self._publish_gate_passed_candidate(
                                    run_id=run_id,
                                    attempt_id=review_publish_attempt_id,
                                    config=config,
                                    repo=repo,
                                    commit_sha=current_commit_sha,
                                    branch_name=current_branch,
                                    worktree_path=current_wt_path,
                                )
                                _mark_candidate_ready(
                                    review_publish_attempt_id,
                                    current_commit_sha,
                                    current_branch,
                                    summary,
                                    not has_changes,
                                )
                            elif review_terminal == ReviewVerdict.PASS_WITH_BACKLOG:
                                # The descendant flow below owns the
                                # publication + candidate-ready transition.
                                pass
                            else:
                                review_fail_summary = (
                                    "Required independent review did not approve "
                                    f"candidate {current_commit_sha}: terminal verdict "
                                    f"{review_terminal.value}. Failing closed with no "
                                    "candidate publication, no candidate-ready transition, "
                                    "and no acceptance."
                                )
                                now_review_fail = current_iso_timestamp()
                                self.db.update_run(
                                    run_id=run_id,
                                    status=RunStatus.FAILED,
                                    completed_at=now_review_fail,
                                    result_summary=review_fail_summary,
                                )
                                self.db.record_event(
                                    run_id,
                                    "review_failed_closed",
                                    attempt_id=review_publish_attempt_id,
                                    payload={
                                        "terminal_verdict": review_terminal.value,
                                        "candidate_sha": current_commit_sha,
                                        "candidate_published": False,
                                    },
                                )
                                self.db.record_event(
                                    run_id,
                                    "run_failed",
                                    attempt_id=review_publish_attempt_id,
                                    payload={
                                        "summary": review_fail_summary,
                                        "terminal_verdict": review_terminal.value,
                                    },
                                )
                                return RunResult(
                                    run_id=run_id,
                                    passed=False,
                                    status=RunStatus.FAILED,
                                    summary=review_fail_summary,
                                    target_repo=str(repo),
                                    base_commit=base_commit,
                                    branch_name=current_branch,
                                    commit_sha=current_commit_sha,
                                    worktree_path=str(current_wt_path),
                                    has_changes=True,
                                    attempts=attempts,
                                    checks=checks,
                                    reviews=reviews,
                                )

                            # If the final review verdict was PASS_WITH_BACKLOG, generate a
                            # documentation-only descendant candidate with BACKLOG.md changes.
                            if reviews and reviews[-1].verdict == ReviewVerdict.PASS_WITH_BACKLOG:
                                from saberops.backlog import (
                                    append_backlog_entries,
                                    parse_backlog_entries,
                                )

                                final_rev = reviews[-1]
                                from_sha_b = current_commit_sha or commit_sha
                                attempt_number += 1
                                desc_attempt_id = f"{run_id}_a{attempt_number}"
                                desc_branch = f"orch/{run_id}/a{attempt_number}"
                                desc_wt_path = base_wt_dir / run_id / f"a{attempt_number}"
                                try:
                                    GitManager.create_worktree(
                                        repo_path=repo,
                                        branch_name=desc_branch,
                                        worktree_path=desc_wt_path,
                                        start_point=from_sha_b,
                                    )
                                except Exception as exc:
                                    self.db.record_event(
                                        run_id,
                                        "worktree_creation_failed",
                                        payload={"error": str(exc), "attempt": attempt_number},
                                    )
                                    err_wt = f"Backlog worktree creation failed: {exc}"
                                    self.db.update_run(
                                        run_id=run_id,
                                        status=RunStatus.FAILED,
                                        completed_at=current_iso_timestamp(),
                                        result_summary=err_wt,
                                    )
                                    return RunResult(
                                        run_id=run_id,
                                        passed=False,
                                        status=RunStatus.FAILED,
                                        summary=err_wt,
                                        target_repo=str(repo),
                                        base_commit=base_commit,
                                        branch_name=desc_branch,
                                        commit_sha=None,
                                        worktree_path=str(desc_wt_path),
                                        has_changes=False,
                                        attempts=attempts,
                                        checks=checks,
                                        reviews=reviews,
                                    )

                                desc_attempt = Attempt(
                                    id=desc_attempt_id,
                                    run_id=run_id,
                                    attempt_number=attempt_number,
                                    provider="orchestrator",
                                    model="backlog_writer",
                                    worktree_path=str(desc_wt_path),
                                    branch_name=desc_branch,
                                    status=AttemptStatus.RUNNING,
                                    created_at=current_iso_timestamp(),
                                )
                                self.db.create_attempt(desc_attempt)
                                self.db.record_event(
                                    run_id,
                                    "attempt_started",
                                    attempt_id=desc_attempt_id,
                                    payload={
                                        "provider": "orchestrator",
                                        "model": "backlog_writer",
                                        "worktree": str(desc_wt_path),
                                        "branch": desc_branch,
                                    },
                                )
                                self.db.record_event(
                                    run_id,
                                    "backlog_descendant_started",
                                    attempt_id=desc_attempt_id,
                                    payload={"from_sha": from_sha_b},
                                )

                                # Append structured backlog entries to descendant BACKLOG.md
                                b_entries = parse_backlog_entries(
                                    text=final_rev.details or final_rev.summary,
                                    originating_run_id=run_id,
                                    review_number=len(reviews),
                                    default_summary=final_rev.summary,
                                )
                                append_backlog_entries(desc_wt_path / "BACKLOG.md", b_entries)

                                # Flow guard: verify that ONLY BACKLOG.md was mutated
                                status_res = run_process(
                                    ["git", "status", "--porcelain"],
                                    cwd=desc_wt_path,
                                )
                                changed_raw_lines = [
                                    line.strip()
                                    for line in status_res.stdout.splitlines()
                                    if line.strip()
                                ]
                                unexpected_files: list[str] = []
                                for line in changed_raw_lines:
                                    parts = line.split(maxsplit=1)
                                    if len(parts) == 2:
                                        rel_path = parts[1].strip().strip('"')
                                        if rel_path != "BACKLOG.md":
                                            unexpected_files.append(rel_path)
                                    else:
                                        unexpected_files.append(line)

                                if unexpected_files:
                                    err_msg = (
                                        "Backlog descendant mutation guard failed: "
                                        f"non-BACKLOG.md file(s) modified: {unexpected_files}"
                                    )
                                    now_f = current_iso_timestamp()
                                    desc_attempt.status = AttemptStatus.FAILED
                                    desc_attempt.worker_error = err_msg
                                    desc_attempt.completed_at = now_f
                                    self.db.update_attempt(
                                        attempt_id=desc_attempt_id,
                                        status=AttemptStatus.FAILED,
                                        worker_error=err_msg,
                                        completed_at=now_f,
                                    )
                                    self.db.record_event(
                                        run_id,
                                        "backlog_guard_failed",
                                        attempt_id=desc_attempt_id,
                                        payload={"unexpected_files": unexpected_files},
                                    )
                                    attempts.append(desc_attempt)
                                    self.db.update_run(
                                        run_id=run_id,
                                        status=RunStatus.FAILED,
                                        completed_at=now_f,
                                        result_summary=err_msg,
                                    )
                                    return RunResult(
                                        run_id=run_id,
                                        passed=False,
                                        status=RunStatus.FAILED,
                                        summary=err_msg,
                                        target_repo=str(repo),
                                        base_commit=base_commit,
                                        branch_name=desc_branch,
                                        commit_sha=None,
                                        worktree_path=str(desc_wt_path),
                                        has_changes=False,
                                        attempts=attempts,
                                        checks=checks,
                                        reviews=reviews,
                                    )

                                # Full gate check on descendant worktree
                                self.db.record_event(
                                    run_id,
                                    "gate_started",
                                    attempt_id=desc_attempt_id,
                                    payload={
                                        "gate_command": config.gate_command,
                                        "role": DispatchRole.REVIEW.value,
                                    },
                                )
                                gate_cmd_parts = split_command(config.gate_command)
                                gate_res = run_process(
                                    gate_cmd_parts,
                                    cwd=desc_wt_path,
                                    timeout=config.gate_timeout,
                                )
                                chk_id = f"chk_{uuid.uuid4().hex[:10]}"
                                chk = CheckResult(
                                    id=chk_id,
                                    attempt_id=desc_attempt_id,
                                    gate_command=config.gate_command,
                                    exit_code=gate_res.exit_code,
                                    stdout=gate_res.stdout,
                                    stderr=gate_res.stderr,
                                    passed=gate_res.passed,
                                    created_at=current_iso_timestamp(),
                                )
                                self.db.create_check(chk)
                                checks.append(chk)

                                desc_changed_paths = tuple(
                                    GitManager.get_changed_paths(desc_wt_path)
                                )
                                desc_disposition = _classify_gate_outcome(
                                    gate_res,
                                    mutated_worktree=False,
                                    changed_paths=desc_changed_paths,
                                )

                                if desc_disposition.outcome is not GateOutcome.PASS:
                                    err_gate = (
                                        f"Gate check failed on backlog descendant "
                                        f"(gate outcome {desc_disposition.outcome.value}, "
                                        f"exit code {gate_res.exit_code})"
                                    )
                                    now_f = current_iso_timestamp()
                                    desc_attempt.status = AttemptStatus.FAILED
                                    desc_attempt.worker_error = err_gate
                                    desc_attempt.completed_at = now_f
                                    self.db.update_attempt(
                                        attempt_id=desc_attempt_id,
                                        status=AttemptStatus.FAILED,
                                        worker_error=err_gate,
                                        completed_at=now_f,
                                    )
                                    gate_failed_payload_d: dict[str, Any] = {
                                        "role": DispatchRole.REVIEW.value,
                                    }
                                    gate_failed_payload_d.update(
                                        _gate_disposition_payload(desc_disposition)
                                    )
                                    self.db.record_event(
                                        run_id,
                                        "gate_failed",
                                        attempt_id=desc_attempt_id,
                                        payload=gate_failed_payload_d,
                                    )
                                    attempts.append(desc_attempt)
                                    self.db.update_run(
                                        run_id=run_id,
                                        status=RunStatus.FAILED,
                                        completed_at=now_f,
                                        result_summary=err_gate,
                                    )
                                    return RunResult(
                                        run_id=run_id,
                                        passed=False,
                                        status=RunStatus.FAILED,
                                        summary=err_gate,
                                        target_repo=str(repo),
                                        base_commit=base_commit,
                                        branch_name=desc_branch,
                                        commit_sha=None,
                                        worktree_path=str(desc_wt_path),
                                        has_changes=False,
                                        attempts=attempts,
                                        checks=checks,
                                        reviews=reviews,
                                    )

                                self.db.record_event(
                                    run_id,
                                    "gate_completed",
                                    attempt_id=desc_attempt_id,
                                    payload={
                                        **_gate_disposition_payload(desc_disposition),
                                        "role": DispatchRole.REVIEW.value,
                                    },
                                )
                                self.db.record_event(
                                    run_id,
                                    "gate_passed",
                                    attempt_id=desc_attempt_id,
                                    payload={
                                        "exit_code": 0,
                                        "role": DispatchRole.REVIEW.value,
                                        "gate_outcome": GateOutcome.PASS.value,
                                    },
                                )
                                # Post-gate mutation guard: re-check AFTER gate
                                post_status_res = run_process(
                                    ["git", "status", "--porcelain"],
                                    cwd=desc_wt_path,
                                )
                                post_changed_lines = [
                                    line.strip()
                                    for line in post_status_res.stdout.splitlines()
                                    if line.strip()
                                ]
                                post_unexpected: list[str] = []
                                for line in post_changed_lines:
                                    parts = line.split(maxsplit=1)
                                    if len(parts) == 2:
                                        rel_path = parts[1].strip().strip('"')
                                        if rel_path == "BACKLOG.md":
                                            continue
                                        post_unexpected.append(rel_path)
                                    else:
                                        post_unexpected.append(line)
                                if post_unexpected:
                                    err_post = (
                                        "Backlog descendant mutation guard failed: "
                                        f"non-BACKLOG.md file(s) modified: {post_unexpected}"
                                    )
                                    now_post = current_iso_timestamp()
                                    desc_attempt.status = AttemptStatus.FAILED
                                    desc_attempt.worker_error = err_post
                                    desc_attempt.completed_at = now_post
                                    self.db.update_attempt(
                                        attempt_id=desc_attempt_id,
                                        status=AttemptStatus.FAILED,
                                        worker_error=err_post,
                                        completed_at=now_post,
                                    )
                                    self.db.record_event(
                                        run_id,
                                        "backlog_guard_failed",
                                        attempt_id=desc_attempt_id,
                                        payload={"unexpected_files": post_unexpected},
                                    )
                                    attempts.append(desc_attempt)
                                    self.db.update_run(
                                        run_id=run_id,
                                        status=RunStatus.FAILED,
                                        completed_at=now_post,
                                        result_summary=err_post,
                                    )
                                    return RunResult(
                                        run_id=run_id,
                                        passed=False,
                                        status=RunStatus.FAILED,
                                        summary=err_post,
                                        target_repo=str(repo),
                                        base_commit=base_commit,
                                        branch_name=desc_branch,
                                        commit_sha=None,
                                        worktree_path=str(desc_wt_path),
                                        has_changes=False,
                                        attempts=attempts,
                                        checks=checks,
                                        reviews=reviews,
                                    )
                                GitManager.commit_changes(
                                    worktree_path=desc_wt_path,
                                    message=f"docs: record review backlog items for {run_id}",
                                )
                                desc_sha = GitManager.get_head_commit(desc_wt_path)
                                now_succ = current_iso_timestamp()
                                desc_attempt.commit_sha = desc_sha
                                desc_attempt.status = AttemptStatus.SUCCESS
                                desc_attempt.completed_at = now_succ
                                self.db.update_attempt(
                                    attempt_id=desc_attempt_id,
                                    status=AttemptStatus.SUCCESS,
                                    commit_sha=desc_sha,
                                    completed_at=now_succ,
                                )
                                self.db.record_event(
                                    run_id,
                                    "attempt_completed",
                                    attempt_id=desc_attempt_id,
                                    payload={
                                        "status": AttemptStatus.SUCCESS.value,
                                        "commit_sha": desc_sha,
                                    },
                                )
                                # The backlog descendant is now the reviewable
                                # final candidate: publish it under this run's
                                # frozen publication policy.
                                candidate_publication = self._publish_gate_passed_candidate(
                                    run_id=run_id,
                                    attempt_id=desc_attempt_id,
                                    config=config,
                                    repo=repo,
                                    commit_sha=desc_sha,
                                    branch_name=desc_branch,
                                    worktree_path=desc_wt_path,
                                )
                                attempts.append(desc_attempt)
                                current_commit_sha = desc_sha
                                current_branch = desc_branch
                                current_wt_path = desc_wt_path
                                commit_sha = desc_sha
                                branch_name = desc_branch
                                wt_path = desc_wt_path
                                summary = (
                                    f"{summary} (with backlog recorded in BACKLOG.md at "
                                    f"{desc_sha[:8]})"
                                )
                                self.db.update_run(
                                    run_id=run_id,
                                    status=RunStatus.COMPLETED,
                                    completed_at=now_succ,
                                    final_attempt_id=desc_attempt_id,
                                    result_summary=summary,
                                )
                                self.db.record_event(
                                    run_id,
                                    "run_completed",
                                    attempt_id=desc_attempt_id,
                                    payload={"commit_sha": desc_sha, "branch": desc_branch},
                                )
                        else:
                            # Review not required for this run: publish the
                            # gate-passed candidate, then the single
                            # candidate-ready transition.
                            candidate_publication = self._publish_gate_passed_candidate(
                                run_id=run_id,
                                attempt_id=attempt_id,
                                config=config,
                                repo=repo,
                                commit_sha=commit_sha,
                                branch_name=branch_name,
                                worktree_path=wt_path,
                            )
                            _mark_candidate_ready(
                                attempt_id,
                                commit_sha,
                                branch_name,
                                summary,
                                not has_changes,
                            )

                        return RunResult(
                            run_id=run_id,
                            passed=True,
                            status=RunStatus.COMPLETED,
                            summary=summary,
                            target_repo=str(repo),
                            base_commit=base_commit,
                            branch_name=branch_name,
                            commit_sha=commit_sha,
                            worktree_path=str(wt_path),
                            has_changes=GitManager.has_diff_against_base(wt_path, base_commit),
                            attempts=attempts,
                            checks=checks,
                            reviews=reviews,
                            candidate_published=candidate_publication is not None,
                            candidate_remote=(
                                candidate_publication.remote_name
                                if candidate_publication is not None
                                else None
                            ),
                            candidate_remote_branch=(
                                candidate_publication.remote_branch
                                if candidate_publication is not None
                                else None
                            ),
                            candidate_verified_remote_sha=(
                                candidate_publication.verified_remote_sha
                                if candidate_publication is not None
                                else None
                            ),
                        )
                    else:
                        now = current_iso_timestamp()
                        attempt.status = AttemptStatus.FAILED
                        attempt.worker_stdout = worker_res.stdout
                        attempt.worker_stderr = worker_res.stderr
                        attempt.completed_at = now
                        # Capability-bearing evidence is ONLY a real validation
                        # failure on actual candidate changes.  INFRASTRUCTURE_FAILURE,
                        # TIMEOUT, TERMINATION_UNSAFE, and MUTATION are
                        # deterministic-gate outcomes that must NEVER trigger a
                        # stronger model dispatch.
                        capability_bearing_gate_failure = (
                            disposition.outcome is GateOutcome.VALIDATION_FAILURE and has_changes
                        )
                        if capability_bearing_gate_failure:
                            attempt.worker_error = (
                                f"Gate check failed (exit code {gate_res.exit_code})"
                            )
                            attempt.commit_sha = commit_sha
                            self.db.update_attempt(
                                attempt_id=attempt_id,
                                status=AttemptStatus.FAILED,
                                worker_stdout=worker_res.stdout,
                                worker_stderr=worker_res.stderr,
                                worker_error=attempt.worker_error,
                                commit_sha=commit_sha,
                                completed_at=now,
                            )
                            gate_failed_payload: dict[str, Any] = {
                                "candidate_sha": commit_sha,
                                "stage": stage_label,
                                "tier": current_tier.value,
                                "role": role_for_stage(DispatchStage(stage_label)).value,
                                "outcome": DispatchOutcome.GATE_FAILURE.value,
                            }
                            gate_failed_payload.update(_gate_disposition_payload(disposition))
                            self.db.record_event(
                                run_id,
                                "gate_failed",
                                attempt_id=attempt_id,
                                payload=gate_failed_payload,
                            )
                            # A committed candidate that failed the gate is not
                            # accepted, but it is still the strongest safe
                            # implementation state for a corrective descendant.
                            # Never make the next worker reconstruct it from the
                            # run's original base.
                            inherited_base_sha = lineage_base_sha
                            lineage_base_sha = commit_sha
                            self.db.record_event(
                                run_id,
                                "candidate_lineage_advanced",
                                attempt_id=attempt_id,
                                payload={
                                    "from_sha": inherited_base_sha,
                                    "to_sha": commit_sha,
                                    "reason": "gate_failed_committed_candidate",
                                },
                            )
                            # A real gate failure on actual candidate changes is
                            # genuine capability-bearing evidence: the worker
                            # produced a substantive attempt and it did not pass
                            # verification. This (unlike no_changes/TIMEOUT) may
                            # justify escalating to a costlier tier once the
                            # attempt budget/candidate ladder is exhausted.
                            tier_has_escalation_evidence = True
                            escalation_outcome = DispatchOutcome.GATE_FAILURE
                            escalation_stage = DispatchStage(stage_label)
                            self.db.record_event(
                                run_id,
                                "dispatch_outcome_classified",
                                attempt_id=attempt_id,
                                payload={
                                    "outcome": DispatchOutcome.GATE_FAILURE.value,
                                    "escalation_evidence": True,
                                    "candidate_sha": commit_sha,
                                    "role": role_for_stage(DispatchStage(stage_label)).value,
                                },
                            )
                        else:
                            # Non-capability-bearing gate failure or no-diff
                            # VALIDATION_FAILURE.  Candidate SHA is preserved.
                            # INFRASTRUCTURE_FAILURE / TIMEOUT /
                            # TERMINATION_UNSAFE / MUTATION must never produce
                            # escalation evidence regardless of how cleanly the
                            # worker ran.
                            if disposition.outcome is GateOutcome.MUTATION:
                                reason = "gate_mutated_worktree"
                                attempt.worker_error = (
                                    "Gate check passed but mutated worktree "
                                    f"with nonignored changes "
                                    f"({', '.join(list(disposition.changed_paths)[:5])})"
                                )
                            elif disposition.outcome is GateOutcome.INFRASTRUCTURE_FAILURE:
                                reason = "gate_infrastructure_failure"
                                attempt.worker_error = (
                                    "Gate could not be launched (missing executable "
                                    "or environment). Candidate is preserved."
                                )
                            elif disposition.outcome is GateOutcome.TIMEOUT:
                                reason = "gate_timeout"
                                attempt.worker_error = (
                                    f"Gate exceeded its deterministic runtime budget "
                                    f"({config.gate_timeout}s). Candidate is preserved."
                                )
                            elif disposition.outcome is GateOutcome.TERMINATION_UNSAFE:
                                reason = "gate_termination_unsafe"
                                attempt.worker_error = (
                                    "Gate process tree could not be proven quiescent. "
                                    "Fails closed. Candidate is preserved."
                                )
                            else:
                                reason = "gate_failed"
                                attempt.worker_error = (
                                    "Gate check failed with no changes "
                                    f"(exit code {gate_res.exit_code})"
                                )
                            # C03 invariant: when the worker produced real
                            # candidate changes, preserve the candidate commit
                            # SHA in durable evidence even though the gate did
                            # not succeed.  The candidate is not promoted (no
                            # lineage advance), but it is not silently erased.
                            preserved_candidate_sha = commit_sha if has_changes else None
                            attempt.commit_sha = preserved_candidate_sha
                            self.db.update_attempt(
                                attempt_id=attempt_id,
                                status=AttemptStatus.FAILED,
                                worker_stdout=worker_res.stdout,
                                worker_stderr=worker_res.stderr,
                                worker_error=attempt.worker_error,
                                commit_sha=preserved_candidate_sha,
                                completed_at=now,
                            )
                            no_change_gate_failed_payload: dict[str, Any] = {
                                "candidate_sha": (commit_sha if has_changes else lineage_base_sha),
                                "stage": stage_label,
                                "tier": current_tier.value,
                                "role": role_for_stage(DispatchStage(stage_label)).value,
                                "outcome": DispatchOutcome.GATE_FAILURE.value,
                            }
                            if not has_changes:
                                no_change_gate_failed_payload["no_changes"] = True
                            no_change_gate_failed_payload.update(
                                _gate_disposition_payload(disposition)
                            )
                            self.db.record_event(
                                run_id,
                                "gate_failed",
                                attempt_id=attempt_id,
                                payload=no_change_gate_failed_payload,
                            )
                            no_change_attempt_failed_payload: dict[str, Any] = {
                                "reason": reason,
                                "outcome": DispatchOutcome.GATE_FAILURE.value,
                                "stage": stage_label,
                                "tier": current_tier.value,
                                "role": role_for_stage(DispatchStage(stage_label)).value,
                                "candidate_sha": (commit_sha if has_changes else lineage_base_sha),
                            }
                            if not has_changes:
                                no_change_attempt_failed_payload["no_changes"] = True
                            no_change_attempt_failed_payload.update(
                                _gate_disposition_payload(disposition)
                            )
                            self.db.record_event(
                                run_id,
                                "attempt_failed_no_changes",
                                attempt_id=attempt_id,
                                payload=no_change_attempt_failed_payload,
                            )
                            no_change_classified_payload: dict[str, Any] = {
                                "outcome": DispatchOutcome.GATE_FAILURE.value,
                                "escalation_evidence": False,
                                "candidate_sha": (commit_sha if has_changes else lineage_base_sha),
                                "role": role_for_stage(DispatchStage(stage_label)).value,
                            }
                            if not has_changes:
                                no_change_classified_payload["no_changes"] = True
                            no_change_classified_payload.update(
                                _gate_disposition_payload(disposition)
                            )
                            self.db.record_event(
                                run_id,
                                "dispatch_outcome_classified",
                                attempt_id=attempt_id,
                                payload=no_change_classified_payload,
                            )
                            # Do NOT advance candidate lineage
                            # Do NOT spend down per-tier escalation budget for auto routing
                            if config.model_override is None:
                                stage_attempts = max(0, stage_attempts - 1)
                            # C03 invariant: deterministic-gate infrastructure
                            # / runtime / safety failures (INFRASTRUCTURE_FAILURE,
                            # TIMEOUT, TERMINATION_UNSAFE) must NEVER buy another
                            # model dispatch.  Fail the run closed at the
                            # control-plane level instead of rotating candidates.
                            if disposition.outcome in (
                                GateOutcome.INFRASTRUCTURE_FAILURE,
                                GateOutcome.TIMEOUT,
                                GateOutcome.TERMINATION_UNSAFE,
                            ):
                                non_capability_attempt = attempt
                                attempts.append(non_capability_attempt)
                                active_attempt_id = None
                                err_summary = (
                                    f"Gate {disposition.outcome.value}: "
                                    "deterministic full gate cannot run in the "
                                    "current environment; the run stops here "
                                    "without spending another model dispatch."
                                )
                                self.db.update_run(
                                    run_id=run_id,
                                    status=RunStatus.FAILED,
                                    completed_at=now,
                                    result_summary=err_summary,
                                )
                                self.db.record_event(
                                    run_id,
                                    "run_failed",
                                    payload={
                                        "summary": err_summary,
                                        "gate_outcome": disposition.outcome.value,
                                        "candidate_sha": (
                                            commit_sha if has_changes else lineage_base_sha
                                        ),
                                        "role": role_for_stage(DispatchStage(stage_label)).value,
                                    },
                                )
                                return RunResult(
                                    run_id=run_id,
                                    passed=False,
                                    status=RunStatus.FAILED,
                                    summary=err_summary,
                                    target_repo=str(repo),
                                    base_commit=base_commit,
                                    branch_name=branch_name,
                                    commit_sha=preserved_candidate_sha,
                                    worktree_path=str(wt_path),
                                    has_changes=has_changes,
                                    attempts=attempts,
                                    checks=checks,
                                    reviews=reviews,
                                )
                else:
                    self.db.update_worker_activation_state(
                        run_id,
                        attempt_number,
                        (
                            WorkerActivationState.TIMED_OUT
                            if worker_res.status == AttemptStatus.TIMEOUT
                            else WorkerActivationState.FAILED
                        ),
                    )
                    active_continuation_checkpoint = None
                    active_continuation_reason = None
                    active_continuation_dispatch_reason = None
                    active_continuation_attempt = None

                    now = current_iso_timestamp()
                    attempt.status = worker_res.status
                    attempt.worker_stdout = worker_res.stdout
                    attempt.worker_stderr = worker_res.stderr
                    attempt.worker_error = worker_res.error
                    attempt.completed_at = now
                    self.db.update_attempt(
                        attempt_id=attempt_id,
                        status=worker_res.status,
                        worker_stdout=worker_res.stdout,
                        worker_stderr=worker_res.stderr,
                        worker_error=worker_res.error,
                        completed_at=now,
                    )
                    # Capture changed paths even for abnormal termination
                    changed_paths_timeout: list[str] = []
                    try:
                        changed_paths_timeout = GitManager.get_changed_paths(wt_path)
                    except Exception:
                        pass
                    self.db.record_event(
                        run_id,
                        "worker_failed",
                        attempt_id=attempt_id,
                        payload={
                            "error": worker_res.error,
                            "status": worker_res.status.value,
                            "provider": attempt.provider,
                            "model": attempt.model,
                            "duration_seconds": worker_res.duration_seconds,
                            "changed_paths": changed_paths_timeout,
                            "worktree_path": str(wt_path),
                            "transcript_path": str(get_transcript_path(self.db, run_id)),
                            "role": effective_role.value,
                        },
                    )
                    if changed_paths_timeout:
                        # The worktree is intentionally retained.  This is
                        # recovery evidence only: an uncommitted, ungated tree
                        # can never advance canonical candidate lineage.
                        self.db.record_event(
                            run_id,
                            "attempt_changed_uncommitted",
                            attempt_id=attempt_id,
                            payload={
                                "changed_paths": changed_paths_timeout,
                                "worktree_path": str(wt_path),
                                "lineage_base_sha": lineage_base_sha,
                                "role": effective_role.value,
                            },
                        )
                    dispatch_outcome = classify_worker_outcome(worker_res)
                    evidence = is_escalation_evidence(dispatch_outcome)
                    self.db.record_event(
                        run_id,
                        "dispatch_outcome_classified",
                        attempt_id=attempt_id,
                        payload={
                            "outcome": dispatch_outcome.value,
                            "escalation_evidence": evidence,
                            "role": effective_role.value,
                        },
                    )
                    if is_evidence_uncertain_error(worker_res.error):
                        # EVIDENCE UNCERTAIN is terminal for the current
                        # autonomous dispatch loop: semantic execution
                        # occurred while authoritative trace/effect
                        # evidence is incomplete, so no blind candidate
                        # rotation, no tier escalation, and no
                        # continuation/replay may follow.  The run stops
                        # here for owner review with the FailoverEngine
                        # BLOCK decision recorded as evidence.  Ordinary
                        # pre-launch/provider failures (marker absent)
                        # fall through to normal candidate routing below.
                        evidence_trace = self.db.get_attempt_trace_for_attempt(attempt_id)
                        failover_decision = FailoverEngine(db=self.db).evaluate(
                            attempt=attempt,
                            trace=evidence_trace,
                            failure_kind=FailureKind.TRACE_UNAVAILABLE,
                        )
                        self.db.record_event(
                            run_id,
                            "autonomous_evidence_uncertain",
                            attempt_id=attempt_id,
                            payload={
                                "provider": candidate.provider,
                                "model": candidate.model,
                                "failure_kind": FailureKind.TRACE_UNAVAILABLE.value,
                                "failover_action": failover_decision.action.value,
                                "failover_reason_code": (failover_decision.reason_code.value),
                                "role": effective_role.value,
                            },
                        )
                        uncertain_summary = (
                            f"Attempt {attempt_number} "
                            f"({candidate.provider}/{candidate.model}) "
                            "is evidence-uncertain: autonomous trace evidence "
                            "persistence failed after semantic execution. "
                            "The run stops here for owner review without "
                            "launching another candidate."
                        )
                        attempts.append(attempt)
                        active_attempt_id = None
                        now_uncertain = current_iso_timestamp()
                        self.db.update_run(
                            run_id=run_id,
                            status=RunStatus.FAILED,
                            completed_at=now_uncertain,
                            result_summary=uncertain_summary,
                        )
                        self.db.record_event(
                            run_id,
                            "run_failed",
                            attempt_id=attempt_id,
                            payload={
                                "summary": uncertain_summary,
                                "attempts_count": len(attempts),
                                "failure_kind": FailureKind.TRACE_UNAVAILABLE.value,
                                "failover_action": failover_decision.action.value,
                            },
                        )
                        return RunResult(
                            run_id=run_id,
                            passed=False,
                            status=RunStatus.FAILED,
                            summary=uncertain_summary,
                            target_repo=str(repo),
                            base_commit=base_commit,
                            attempts=attempts,
                            checks=checks,
                            reviews=reviews,
                        )
                    if evidence:
                        tier_has_escalation_evidence = True
                        escalation_outcome = dispatch_outcome
                        escalation_stage = DispatchStage(stage_label)
                    if worker_res.status == AttemptStatus.TIMEOUT:
                        if config.model_override is None:
                            # A timeout is not evidence the task is beyond
                            # this tier's capability (it may just be slow,
                            # contended, or unlucky) -- it must not spend
                            # down the per-tier attempt budget that drives
                            # auto-escalation. The next candidate is still
                            # tried in order. Explicit model_override runs
                            # have no candidate fallback and no escalation
                            # path, so stage_attempts must keep counting
                            # every attempt there (see the matching
                            # no_changes guard above) or the run never
                            # terminates.
                            stage_attempts = max(0, stage_attempts - 1)
                        # TIMEOUT never contributes escalation evidence,
                        # regardless of routing mode.

                attempts.append(attempt)
                active_attempt_id = None

            # All attempts exhausted
            now = current_iso_timestamp()
            summary = f"All {len(attempts)} attempt(s) failed to pass gate '{gate_command}'."
            failed_step = next(
                (
                    step
                    for step in self.db.get_plan_steps(run_id)
                    if step.status == PlanStepStatus.RUNNING
                ),
                None,
            )
            if failed_step is not None:
                mark_step(self.db, failed_step, PlanStepStatus.FAILED, error=summary)
                for downstream in self.db.get_plan_steps(run_id):
                    if downstream.status in (PlanStepStatus.PENDING, PlanStepStatus.READY):
                        mark_step(
                            self.db,
                            downstream,
                            PlanStepStatus.BLOCKED,
                            blocker=f"dependency {failed_step.id} failed gate",
                        )
                persisted_plan = self.db.get_run_plan(run_id)
                if persisted_plan is not None:
                    self.db.update_run_plan(persisted_plan.id, PlanStatus.FAILED)
            self.db.update_run(
                run_id=run_id,
                status=RunStatus.FAILED,
                completed_at=now,
                result_summary=summary,
            )
            self.db.record_event(
                run_id,
                "run_failed",
                payload={"summary": summary, "attempts_count": len(attempts)},
            )

            return RunResult(
                run_id=run_id,
                passed=False,
                status=RunStatus.FAILED,
                summary=summary,
                target_repo=str(repo),
                base_commit=base_commit,
                attempts=attempts,
                checks=checks,
                reviews=reviews,
            )

        except CandidatePublicationError as exc:
            # Publication failure is an infrastructure failure, never a
            # worker/model/review/gate failure.  Candidate evidence (commit,
            # branch, worktree, provenance, events) is preserved above; the
            # run fails deterministically with an actionable summary and no
            # worker retry/escalation budget is consumed.  It is NOT remotely
            # reviewable, so the run must not report success.
            now = current_iso_timestamp()
            err_msg = f"Candidate publication failed: {exc}"
            self.db.update_run(
                run_id=run_id,
                status=RunStatus.FAILED,
                completed_at=now,
                result_summary=err_msg,
            )
            self.db.record_event(
                run_id,
                "run_failed",
                attempt_id=active_attempt_id,
                payload={
                    "summary": err_msg,
                    "classification": "infrastructure_publication_failure",
                },
            )
            return RunResult(
                run_id=run_id,
                passed=False,
                status=RunStatus.FAILED,
                summary=err_msg,
                target_repo=str(repo),
                base_commit=base_commit,
                branch_name=branch_name,
                commit_sha=commit_sha,
                worktree_path=str(wt_path),
                has_changes=True,
                attempts=attempts,
                checks=checks,
                reviews=reviews,
            )

        except Exception as exc:
            if str(exc).startswith("Recovery blocked:"):
                raise
            # Bounded failure handler: prevent runaway RUNNING state in DB
            now = current_iso_timestamp()
            raw_msg = str(exc)
            if raw_msg.startswith("explicit model is ineligible:"):
                err_msg = raw_msg
            else:
                err_msg = f"Unexpected error during run execution: {exc}"
            if active_attempt_id:
                # Pair worker lifecycle: ensure worker_completed precedes terminal failure
                self.db.record_event(
                    run_id,
                    "worker_completed",
                    attempt_id=active_attempt_id,
                    payload={
                        "status": AttemptStatus.FAILED.value,
                        "summary": err_msg,
                        "error": str(exc),
                    },
                )
                self.db.update_attempt(
                    attempt_id=active_attempt_id,
                    status=AttemptStatus.FAILED,
                    worker_error=err_msg,
                    completed_at=now,
                )
            self.db.update_run(
                run_id=run_id,
                status=RunStatus.FAILED,
                completed_at=now,
                result_summary=err_msg,
            )
            self.db.record_event(
                run_id,
                "run_exception",
                attempt_id=active_attempt_id,
                payload={"error": str(exc)},
            )
            self.db.record_event(
                run_id,
                "run_failed",
                attempt_id=active_attempt_id,
                payload={"summary": err_msg, "error": str(exc)},
            )
            return RunResult(
                run_id=run_id,
                passed=False,
                status=RunStatus.FAILED,
                summary=err_msg,
                target_repo=str(repo),
                base_commit=base_commit,
                attempts=attempts,
                checks=checks,
                reviews=reviews,
            )

    def cleanup_run(self, run_id: str) -> dict[str, list[str]]:
        """Safely remove clean attempt worktrees for a completed or failed run.

        Refuses cleanup for running runs, external worktrees, or dirty worktrees.
        Preserves candidate branches and database records.

        Returns:
            Dict containing 'removed', 'skipped_dirty', and 'skipped_missing' paths.
        """
        run = self.db.get_run(run_id)
        if not run:
            raise ValueError(f"Run '{run_id}' not found")

        if run.status == RunStatus.RUNNING:
            raise RuntimeError(f"Cannot clean up run '{run_id}': status is RUNNING")

        # The repository always comes from the persisted run identity; the UI's
        # currently selected project can never retarget an existing run.
        try:
            repo = assert_run_repository(run).resolve()
        except ProvenanceError as exc:
            raise RuntimeError(f"Refusing cleanup for run '{run_id}': {exc}") from exc

        base_wt_dir = (
            self.worktree_base.resolve()
            if self.worktree_base is not None
            else GitManager.get_default_worktree_base(repo).resolve()
        )

        attempts = self.db.get_attempts_for_run(run_id)
        removed: list[str] = []
        skipped_dirty: list[str] = []
        skipped_missing: list[str] = []

        for attempt in attempts:
            if not attempt.worktree_path:
                continue
            wt_path = Path(attempt.worktree_path).resolve()
            if not wt_path.exists():
                skipped_missing.append(str(wt_path))
                continue

            # Verify path belongs to orchestrator worktree root
            try:
                is_subpath = wt_path.is_relative_to(base_wt_dir) and wt_path != base_wt_dir
            except (ValueError, AttributeError):
                is_subpath = False

            if not is_subpath:
                raise RuntimeError(
                    f"Refusing cleanup: worktree '{wt_path}' does not belong "
                    f"to orchestrator worktree root '{base_wt_dir}'"
                )

            # Check if worktree is clean
            if not GitManager.is_clean(wt_path):
                skipped_dirty.append(str(wt_path))
                continue

            # Safely remove clean worktree
            GitManager.remove_worktree(
                repo_path=repo,
                worktree_path=wt_path,
                force=False,
                worktree_base=base_wt_dir,
            )
            removed.append(str(wt_path))

        # Prune git worktree metadata
        run_process(["git", "worktree", "prune"], cwd=repo)

        return {
            "removed": removed,
            "skipped_dirty": skipped_dirty,
            "skipped_missing": skipped_missing,
        }
