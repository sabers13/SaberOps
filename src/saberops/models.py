"""Domain models and data structures for SaberOps."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class RunStatus(StrEnum):
    """Status of an orchestrator run."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ORPHANED = "ORPHANED"


# A run in one of these statuses has reached its final outcome.  Terminal state
# is monotonic: nothing -- reconciliation, cancellation, or a late worker -- may
# move a run out of it, and no execution owner may be claimed for it again.
TERMINAL_RUN_STATUSES: frozenset[RunStatus] = frozenset(
    {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.ORPHANED}
)


class PlanStepStatus(StrEnum):
    """Durable status of an explicit execution-plan step."""

    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class PlanStatus(StrEnum):
    """Durable status of a run plan."""

    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class IndependenceState(StrEnum):
    """Fail-closed admission state for independent semantic work."""

    ESTABLISHED = "ESTABLISHED"
    NOT_ESTABLISHED = "NOT_ESTABLISHED"


class IntegrationDisposition(StrEnum):
    """Representation-only state of deterministic branch integration."""

    PENDING_CANDIDATES = "PENDING_CANDIDATES"
    READY_FOR_INTEGRATION = "READY_FOR_INTEGRATION"
    INTEGRATED = "INTEGRATED"
    FAILED = "FAILED"


class PostIntegrationValidationState(StrEnum):
    """State of deterministic validation bound to one integration digest."""

    REQUIRED_PENDING = "REQUIRED_PENDING"
    PASSED = "PASSED"
    FAILED = "FAILED"


class HealthState(StrEnum):
    """Evidence-based worker health, intentionally separate from run status."""

    HEALTHY = "healthy"
    UNCERTAIN = "uncertain"
    STALLED = "stalled"
    TERMINAL = "terminal"


class ManagerState(StrEnum):
    """Frontend manager attachment state; the supervisor remains the owner."""

    ATTACHED = "ATTACHED"
    SUSPENDED = "SUSPENDED"
    ATTENTION_REQUIRED = "ATTENTION_REQUIRED"


class AttemptStatus(StrEnum):
    """Status of an individual worker attempt."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"


class Tier(StrEnum):
    """Execution tiers for model routing."""

    T1 = "T1"
    T2 = "T2"
    T3 = "T3"


class DispatchStage(StrEnum):
    """Operation identity for a model dispatch, independent of tier/budget."""

    IMPLEMENTATION = "implementation"
    RETRY = "retry"
    REPAIR = "repair"
    REVIEW = "review"


class ProviderReadiness(StrEnum):
    """Provider-neutral dispatch-readiness evidence state.

    This is evidence, not admission policy: model-access produces it from
    sanctioned non-inference observations and a future control-plane consumer
    decides how it affects dispatch.  ``UNKNOWN`` must never be treated as
    positive readiness.
    """

    READY = "READY"
    NOT_READY = "NOT_READY"
    UNKNOWN = "UNKNOWN"


class DispatchReason(StrEnum):
    """Small typed vocabulary for why a model call was actually launched."""

    INITIAL_IMPLEMENTATION = "initial_implementation"
    PACKAGE_IMPLEMENTATION = "package_implementation"
    VALIDATION_REPAIR = "validation_repair"
    REVIEW_REPAIR = "review_repair"
    PACKAGE_REVIEW_REPAIR = "package_review_repair"
    CONTINUATION_AFTER_QUOTA = "continuation_after_quota"
    CONTINUATION_AFTER_CONTEXT = "continuation_after_context"
    CONTINUATION_AFTER_GRACEFUL_HANDOFF = "continuation_after_graceful_handoff"
    COLD_TAKEOVER = "cold_takeover"
    INDEPENDENT_REVIEW = "independent_review"
    EXPLICIT = "explicit"


class DispatchRole(StrEnum):
    """Canonical role vocabulary for a model dispatch.

    Roles describe WHAT KIND of work the dispatched model is performing, not
    which tier/provider/model is selected.  The mapping is intentionally small:

    * ``BULK``    — primary implementation work on a fresh slice.
    * ``REPAIR``  — bounded corrective work on a committed candidate (retry,
                    repair, final-repair).
    * ``SURGEON`` — reserved for Surgical Escalation Capsule (C04+).  The value
                    exists today so durable evidence already answers "what role
                    was this dispatch performing?", but no automatic routing
                    behaviour is wired to it in C03.
    * ``REVIEW``  — independent reviewer dispatch.
    * ``PLANNER`` — reserved for explicit planning/planner dispatches.  The
                    value exists today but is not auto-selected in C03.

    SURGEON and PLANNER must never gain automatic routing behavior in C03;
    they are placeholders for later slices.
    """

    BULK = "BULK"
    REPAIR = "REPAIR"
    SURGEON = "SURGEON"
    REVIEW = "REVIEW"
    PLANNER = "PLANNER"


# Canonical mapping of the legacy DispatchStage to the new DispatchRole.
# A SURGEON/PLANNER dispatch must never claim one of the legacy stage labels,
# and legacy stage labels must always project to a non-SURGEON/non-PLANNER role.
STAGE_TO_ROLE: dict[DispatchStage, DispatchRole] = {
    DispatchStage.IMPLEMENTATION: DispatchRole.BULK,
    DispatchStage.RETRY: DispatchRole.REPAIR,
    DispatchStage.REPAIR: DispatchRole.REPAIR,
    DispatchStage.REVIEW: DispatchRole.REVIEW,
}


def role_for_stage(stage: DispatchStage) -> DispatchRole:
    """Return the canonical role for a legacy dispatch stage.

    Centralised so evidence-emitting code never drifts from the documented
    IMPLEMENTATION=BULK / RETRY=REPAIR / REPAIR=REPAIR / REVIEW=REVIEW mapping.
    """
    return STAGE_TO_ROLE[stage]


# ---------------------------------------------------------------------------
# C11-B: provider-neutral effort / risk / orch-binding vocabulary.
#
# Effort and risk are deliberately separate.  Effort answers "how much
# reasoning budget is requested"; risk answers "what governance / review /
# security controls are required".  Correlation is policy, never identity.
# ---------------------------------------------------------------------------


class ReasoningEffort(StrEnum):
    """Provider-neutral reasoning effort tiers.

    ``AUTO`` is NOT a tier; it is a policy which selects a tier.  Bindings
    declare which subset of tiers they actually support, and the provider
    adapter translates a neutral tier into a concrete provider option only
    where a real mapping is declared.
    """

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    ULTRA = "ULTRA"


# Numeric ordering for AUTO bounds.  Lower number = lower cost / faster.
_REASONING_EFFORT_ORDER: dict[ReasoningEffort, int] = {
    ReasoningEffort.LOW: 0,
    ReasoningEffort.MEDIUM: 1,
    ReasoningEffort.HIGH: 2,
    ReasoningEffort.ULTRA: 3,
}


def effort_order(effort: ReasoningEffort) -> int:
    """Return the deterministic ordinal for ``effort``.

    Used only to express inclusive min/max bounds; never as a substitute
    for an explicit tier equality check.
    """
    return _REASONING_EFFORT_ORDER[effort]


class RiskLevel(StrEnum):
    """Structured risk classification for a semantic action.

    Risk answers what governance / review / security controls a dispatch
    requires.  It is independent of reasoning effort: an owner may
    authorize LOW effort at HIGH risk (focused surgical repair) or HIGH
    effort at LOW risk (deep exploratory planning) -- the two fields
    correlate only through explicit policy.
    """

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class ReviewPolicyMode(StrEnum):
    """Project-scoped risk-adaptive review policy.

    Lives in :mod:`saberops.models` so the canonical enum is
    importable from the module-owned authority surface (config,
    accept, supervisor, CLI, web) without forcing every consumer to
    depend on :mod:`saberops.review_adaptive`.  The runtime
    policy decision and project-policy persistence remain in
    :mod:`saberops.review_adaptive`; this enum is the
    stable vocabulary those surfaces exchange.

    * ``RISK_ADAPTIVE`` -- canonical default; review intensity follows
      the deterministic classifier.
    * ``ALWAYS_REQUIRED`` -- only tightening override; forces an
      independent semantic reviewer even when the classifier would
      classify the delta LOW.  No ``NEVER_REQUIRED`` mode exists: a
      project may NOT silently weaken the hard minimums.
    """

    RISK_ADAPTIVE = "RISK_ADAPTIVE"
    ALWAYS_REQUIRED = "ALWAYS_REQUIRED"


class GateScratchMode(StrEnum):
    """Project-scoped authoritative gate scratch backend preference.

    This is the resolved owner/project preference recorded at run creation.
    It is independent of, and lower than, every governance/precedence band
    in :class:`Architecture ownership precedence <>`: it is an execution-
    efficiency preference at the lowest band, never Project truth.

    DISK            -- default; ordinary durable scratch on disk.
    RAM_PREFERRED   -- explicit project opt-in; tmpfs preflight before the
                       gate command, falling back to disk when preflight
                       cannot safely satisfy the requirement.
    RAM_REQUIRED    -- explicit project opt-in; tmpfs preflight before the
                       gate command, BLOCKING gate execution entirely when
                       preflight cannot safely satisfy the requirement
                       (no silent disk fallback).
    """

    DISK = "DISK"
    RAM_PREFERRED = "RAM_PREFERRED"
    RAM_REQUIRED = "RAM_REQUIRED"


# Stable product order for UI dropdowns / ``orch project gate-scratch`` lists.
GATE_SCRATCH_MODE_ORDER: tuple[GateScratchMode, ...] = (
    GateScratchMode.DISK,
    GateScratchMode.RAM_PREFERRED,
    GateScratchMode.RAM_REQUIRED,
)


class OrchBindingMode(StrEnum):
    """How an owner-selected Orchestrator binding is resolved.

    PINNED              — exact owner-selected binding only; no silent
                          substitution; typed failure otherwise.
    PREFERRED_WITH_FALLBACK — owner-ordered list; try in order; C07 hard
                          policy still applies to every candidate.
    AUTO                — let existing routing policy choose among eligible
                          Orchestrator bindings.
    """

    PINNED = "PINNED"
    PREFERRED_WITH_FALLBACK = "PREFERRED_WITH_FALLBACK"
    AUTO = "AUTO"


class BindingRole(StrEnum):
    """Semantic role a binding is qualified to serve.

    Distinct from :class:`DispatchRole`: ``BindingRole`` describes WHAT
    KIND of binding this is (the slot it fills in the autonomous
    runtime), while ``DispatchRole`` describes WHAT KIND OF WORK one
    particular dispatch is performing.
    """

    ORCHESTRATOR = "ORCHESTRATOR"
    WORKER = "WORKER"
    REVIEWER = "REVIEWER"


class GateOutcome(StrEnum):
    """Structured taxonomy for an authoritative Orch-owned full gate.

    The taxonomy is deliberately small and records the operational outcome
    of one gate execution.  It does NOT prove why a gate returned nonzero:
    current structured evidence does not distinguish a genuine candidate
    validation failure from a launchable gate-configuration error (H3/H3.1;
    e.g. ``make definitely_missing_target`` classifies as
    ``VALIDATION_FAILURE`` exactly like a genuinely failing assertion).

    Gate truth is distinct from routing authority: ``VALIDATION_FAILURE``
    means deterministic verification did not pass, but it carries no
    cross-tier escalation authority under H3.1; only an explicit typed
    capability failure does.

    * ``PASS``                 — gate launched normally, completed normally,
      exit code 0, clean candidate.
    * ``VALIDATION_FAILURE``   — gate launched and completed normally with a
      nonzero exit, and no typed infrastructure / timeout / termination /
      mutation condition applied.  This records that deterministic
      verification did not pass; it does NOT prove the nonzero exit was
      caused by candidate validation failure.  This records gate truth but
      is NOT cross-tier escalation evidence.
    * ``INFRASTRUCTURE_FAILURE`` — gate executable could not be launched (e.g.
      missing executable, missing tool, OSError at spawn).  This is NOT
      capability evidence and must never trigger another model dispatch.
    * ``TIMEOUT``              — gate exceeded its deterministic runtime budget.
      This is NOT automatically capability evidence.
    * ``TERMINATION_UNSAFE``   — gate process tree could not be proven
      quiescent.  Fails closed.  This is NOT capability evidence.
    * ``MUTATION``             — gate exited successfully but introduced a
      nonignored worktree mutation (C02 fail-closed).  This is NOT capability
      evidence merely because the deterministic gate mutated the worktree.
    """

    PASS = "PASS"
    VALIDATION_FAILURE = "VALIDATION_FAILURE"
    INFRASTRUCTURE_FAILURE = "INFRASTRUCTURE_FAILURE"
    TIMEOUT = "TIMEOUT"
    TERMINATION_UNSAFE = "TERMINATION_UNSAFE"
    MUTATION = "MUTATION"


@dataclass(frozen=True)
class GateDisposition:
    """Structured Orch-owned gate outcome.

    Carries the typed :class:`GateOutcome` plus the minimum structured evidence
    necessary to record which operational gate condition applied
    (launch / timeout / termination / mutation / exit code) without fuzzy
    parsing.  It does NOT distinguish a genuine candidate validation failure
    from a launchable gate-configuration error: both present as
    ``VALIDATION_FAILURE``.  Designed to be durable-evidence friendly: every
    field is a primitive or tuple of primitives, never a free-form string
    subject to fuzzy parsing.
    """

    outcome: GateOutcome
    exit_code: int
    duration_seconds: float
    timed_out: bool
    termination_incomplete: bool
    launch_failed: bool
    mutated_worktree: bool = False
    changed_paths: tuple[str, ...] = ()
    candidate_sha: str | None = None


class DispatchOutcome(StrEnum):
    """Structured outcome taxonomy used for capability-escalation evidence."""

    CAPABILITY_FAILURE = "CAPABILITY_FAILURE"
    GATE_FAILURE = "GATE_FAILURE"
    NO_CHANGES = "NO_CHANGES"
    ALREADY_SATISFIED = "ALREADY_SATISFIED"
    WORKER_FAILURE_OR_REFUSAL = "WORKER_FAILURE_OR_REFUSAL"
    UNKNOWN_NO_CHANGES = "UNKNOWN_NO_CHANGES"
    TIMEOUT = "TIMEOUT"
    PROVIDER_OR_RUNTIME_FAILURE = "PROVIDER_OR_RUNTIME_FAILURE"
    ELIGIBILITY_SKIP = "ELIGIBILITY_SKIP"
    UNKNOWN = "UNKNOWN"


class WorkerInterruptionReason(StrEnum):
    """Structured taxonomy for continuable worker interruptions.

    Distinguishes continuable resource exhaustion and graceful handoff from
    ordinary worker or capability failure.
    """

    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    CONTEXT_EXHAUSTED = "CONTEXT_EXHAUSTED"
    GRACEFUL_HANDOFF = "GRACEFUL_HANDOFF"
    ABORTED = "ABORTED"


class WorkerActivationState(StrEnum):
    """Durable lifecycle states for active worker invocations."""

    STARTING = "STARTING"
    RUNNING = "RUNNING"
    INTERRUPTED = "INTERRUPTED"
    QUIESCING = "QUIESCING"
    CHECKPOINTED = "CHECKPOINTED"
    REPLACED = "REPLACED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    TERMINATED = "TERMINATED"
    TERMINATION_UNCERTAIN = "TERMINATION_UNCERTAIN"


class SideEffectState(StrEnum):
    """Minimal lifecycle states for recovery side-effect journal."""

    INTENT = "INTENT"
    STARTED = "STARTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    UNCERTAIN = "UNCERTAIN"


class SideEffectOperation(StrEnum):
    """Operations governed by the minimal recovery side-effect journal."""

    CHECKPOINT_CREATION = "CHECKPOINT_CREATION"
    REPLACEMENT_LAUNCH = "REPLACEMENT_LAUNCH"
    TOOL_INVOCATION = "TOOL_INVOCATION"


@dataclass(frozen=True)
class WorkerCheckpoint:
    """Durable partial-work checkpoint created when a worker is interrupted."""

    id: str
    run_id: str
    source_attempt_id: str
    source_base_sha: str
    checkpoint_sha: str
    reason: str
    changed_paths: tuple[str, ...]
    created_at: str
    handoff_id: str | None = None


# ---------------------------------------------------------------------------
# C11-C: structured execution trace + tool-effect evidence.
#
# ``TraceCapability`` declares the evidence class the runtime actually has
# for an autonomous Attempt: NATIVE_STRUCTURED when the backend emits a
# structured event stream the harness can read, SANDBOX_INTERCEPTED when
# the sandbox boundary deterministically observes tool effects, and NONE
# when neither is available (autonomous execution becomes ineligible).
#
# ``EffectStatus`` and ``ReconciliationStatus`` are the bounded state
# vocabularies for one tool effect.  Reconciliation is the typed answer to
# "is this effect safe to retry without blind replay"; an
# ``EXTERNAL_UNCERTAIN`` effect must be reconciled before any new attempt.
# ---------------------------------------------------------------------------


class TraceCapability(StrEnum):
    """Evidence class for a structured execution trace.

    * ``NATIVE_STRUCTURED``     -- backend/harness emits a native structured
      event stream (e.g. OpenCode JSONL step_finish, Cline JSONL,
      Codex JSONL turn.completed).  The runtime must never fall back to
      parsing terminal-rendering text when a native event exists.
    * ``SANDBOX_INTERCEPTED``   -- backend lacks a structured event stream
      but the per-Attempt sandbox can deterministically observe tool
      operations and effects at the enforcement boundary.
    * ``NONE``                 -- neither a native structured event stream
      nor a deterministic sandbox interception is available.  An autonomous
      Attempt with this trace class is ineligible for autonomous execution.
    """

    NATIVE_STRUCTURED = "NATIVE_STRUCTURED"
    SANDBOX_INTERCEPTED = "SANDBOX_INTERCEPTED"
    NONE = "NONE"


class EffectStatus(StrEnum):
    """Bounded lifecycle for one recorded tool effect."""

    STARTED = "STARTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    UNCERTAIN = "UNCERTAIN"


class ReconciliationStatus(StrEnum):
    """Typed answer to "is this effect safe to retry without blind replay"."""

    UNKNOWN = "UNKNOWN"
    NO_EFFECT = "NO_EFFECT"
    LOCAL_REPLAY_SAFE = "LOCAL_REPLAY_SAFE"
    EXTERNAL_IDEMPOTENT_RECEIPT = "EXTERNAL_IDEMPOTENT_RECEIPT"
    RECONCILED = "RECONCILED"
    UNCERTAIN = "UNCERTAIN"


class FailureKind(StrEnum):
    """Bounded failure taxonomy for failover decisions.

    Distinct from :class:`DispatchOutcome` and :class:`GateOutcome`: this
    vocabulary is the runtime taxonomy the failover engine consumes.  It is
    deliberately small and intentionally refuses to collapse everything
    into ``retry=True/False`` -- the supervisor (C11-D) will reason over
    these typed values.
    """

    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    TRANSPORT_FAILURE = "TRANSPORT_FAILURE"
    TIMEOUT = "TIMEOUT"
    QUOTA_UNAVAILABLE = "QUOTA_UNAVAILABLE"
    TOOL_AUTH_DENIED = "TOOL_AUTH_DENIED"
    SANDBOX_VIOLATION = "SANDBOX_VIOLATION"
    TRACE_UNAVAILABLE = "TRACE_UNAVAILABLE"
    EXTERNAL_EFFECT_UNCERTAIN = "EXTERNAL_EFFECT_UNCERTAIN"
    INVALID_BASE_OR_CHECKPOINT = "INVALID_BASE_OR_CHECKPOINT"
    SEMANTIC_RESULT_INVALID = "SEMANTIC_RESULT_INVALID"
    CANCELLATION = "CANCELLATION"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ToolEffect:
    """One durable record of a tool operation / effect for an Attempt.

    ``capability`` is the C08 logical ref (``github.merge_pull_request``)
    when known, otherwise the canonical :class:`CapabilityOperation` value;
    ``resource_ref`` is the bounded target (e.g. a relative path or
    destination host); ``side_effect_class`` is the
    :class:`saberops.sandbox.SideEffectSemantics` value the runtime
    recorded against this effect; ``reconciliation_status`` is the typed
    answer to "is this effect safe to retry without blind replay".

    ``result_redacted`` carries a bounded, secret-stripped snippet of the
    result when one exists.  Real secret material never enters this row.
    """

    id: str
    attempt_id: str
    run_id: str
    operation: str
    capability: str | None
    resource_ref: str | None
    side_effect_class: str
    status: EffectStatus
    idempotency_key: str | None
    started_at: str
    completed_at: str | None
    result_class: str | None
    result_redacted: str | None
    reconciliation_status: str
    error: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class AttemptTrace:
    """Durable evidence record describing one Attempt's structured trace.

    ``trace_class`` is the :class:`TraceCapability` the runtime established
    *before* semantic execution.  ``source_kind`` names the actual producer
    (e.g. ``"opencode_jsonl"``, ``"codex_jsonl"``, ``"sandbox_enforcer"``).
    The summary JSON carries a bounded, structured digest of the recorded
    events; the full stream stays out of durable evidence to keep the row
    compact.
    """

    id: str
    attempt_id: str
    run_id: str
    trace_class: TraceCapability
    source_kind: str
    started_at: str
    updated_at: str
    tool_effects_count: int = 0
    completed_at: str | None = None
    summary_json: str | None = None
    failure_kind: str | None = None
    failure_reason: str | None = None


@dataclass(frozen=True)
class WorkerActivation:
    """Durable identity and state for an active worker execution."""

    id: str
    run_id: str
    attempt_id: str
    generation: int
    state: WorkerActivationState
    created_at: str
    updated_at: str
    worker_identity_json: str | None = None


@dataclass(frozen=True)
class SideEffectRecord:
    """Minimal side-effect journal record for exactly-once recovery."""

    idempotency_key: str
    run_id: str
    attempt_id: str
    operation: SideEffectOperation
    state: SideEffectState
    created_at: str
    updated_at: str
    payload_json: str | None = None
    result_json: str | None = None


class RoutingState(StrEnum):
    """Quota-driven routing state for a provider pool."""

    NORMAL = "NORMAL"
    DEMOTED = "DEMOTED"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class StartRunResult:
    """Result of starting an asynchronous run via service."""

    run_id: str


@dataclass(frozen=True)
class OrchEvent:
    """An event recorded during orchestrator execution."""

    id: int
    run_id: str
    attempt_id: str | None
    event_type: str
    payload: dict[str, Any] | None
    created_at: str


@dataclass(frozen=True)
class CheckResult:
    """Deterministic gate check outcome."""

    id: str
    attempt_id: str
    gate_command: str
    exit_code: int
    stdout: str
    stderr: str
    passed: bool
    created_at: str


@dataclass
class Attempt:
    """An execution attempt by a specific worker model."""

    id: str
    run_id: str
    attempt_number: int
    provider: str
    model: str
    worktree_path: str
    branch_name: str
    status: AttemptStatus
    worker_stdout: str = ""
    worker_stderr: str = ""
    worker_error: str | None = None
    commit_sha: str | None = None
    created_at: str = ""
    completed_at: str | None = None
    # Provenance inherited from the parent run when the row is created; see
    # :mod:`saberops.provenance`.
    project_id: str | None = None
    objective_id: str | None = None
    base_sha: str | None = None
    interruption_reason: str | None = None


@dataclass
class Run:
    """An end-to-end task run."""

    id: str
    task: str
    target_repo: str
    base_commit: str
    tier: Tier
    training_allowed: bool
    gate_command: str
    status: RunStatus
    created_at: str
    completed_at: str | None = None
    final_attempt_id: str | None = None
    result_summary: str | None = None
    config_json: str | None = None
    review_state_json: str | None = None
    # C13-F: frozen, JSON-serializable review-necessity decision.  Persisted
    # at candidate-ready time by :func:`saberops.review_adaptive.decide_review`
    # and consulted by acceptance, supervisor recovery, retry, repair, and
    # handoff.  A missing value means the Run was created before C13-F --
    # conservative reconstruction (no retroactive skip) is required.
    review_risk_decision_json: str | None = None
    routing_snapshot_json: str | None = None
    # Durable project/objective provenance.  ``project_id`` and
    # ``project_identity_json`` pin the run to the repository it was created
    # for; ``objective_id`` is the immutable objective identity.  All three are
    # ``None`` only for legacy rows written before isolation existed.
    project_id: str | None = None
    project_identity_json: str | None = None
    objective_id: str | None = None
    # Immutable non-secret C07 policy contract captured when the run is created.
    control_plane_snapshot_json: str | None = None
    control_plane_snapshot_digest: str | None = None
    tool_contract_json: str | None = None
    tool_contract_digest: str | None = None
    # C10: immutable non-secret gateway contract captured at run creation.
    # Both fields default to None for legacy rows predating C10.
    gateway_snapshot_json: str | None = None
    gateway_snapshot_digest: str | None = None


@dataclass(frozen=True)
class RunPlan:
    """Persisted, versioned execution plan for a run."""

    id: str
    run_id: str
    status: PlanStatus
    version: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class PlanStep:
    """A stable step in a persisted run plan."""

    id: str
    plan_id: str
    run_id: str
    kind: str
    title: str
    goal: str
    status: PlanStepStatus
    ordering: int
    dependency_ids: tuple[str, ...] = ()
    work_package_id: str | None = None
    attempt_id: str | None = None
    review_id: str | None = None
    blocker: str | None = None
    error: str | None = None
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class WorkPackage:
    """A coherent implementation package with enough context to resume safely."""

    id: str
    run_id: str
    plan_step_id: str
    parent_objective: str
    goal: str
    acceptance_criteria: tuple[str, ...]
    dependency_package_ids: tuple[str, ...] = ()
    base_candidate_sha: str | None = None
    constraints: tuple[str, ...] = ()
    relevant_paths: tuple[str, ...] = ()
    prerequisite_summaries: tuple[str, ...] = ()
    gate_required: bool = True
    capability_hint: str | None = None
    tier_hint: str | None = None
    long_running_hint: bool | None = None
    long_running: bool = False
    long_running_reason: str | None = None
    context_capsule_json: str | None = None
    scope_json: str | None = None
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class IndependentWorkBranch:
    """One dependency-safe branch slot; representation only, never a launch.

    C14-A explicit CONCURRENT-WRITE CONTRACT: every component of a
    concurrent-execution consumer's BWRAP-exact sandbox is carried directly
    on the branch:

    * ``writable_existing_paths``: canonical literal paths the branch may edit
      (derived from ``owned_files ∪ interface_files ∪ test_files`` of the
      module).  Distinct C09 meanings are preserved: ``owned_files`` remain
      the production owner; ``interface_files``/``test_files`` are added to
      the contract only when structurally coherent (see planning.py).
    * ``writable_new_subtrees``: literal ``<canonical-dir>/**`` subtree
      prefixes the branch may extend.  Each is an EXACT, enforcement-
      compatible literal prefix derived from a ``ProjectModule.root``;
      glob-language intersection is not implemented.
    * ``deny_paths``: sandbox deny patterns the contract commits to (the
      existing C11-C default deny paths: ``.git/**``, ``.github/**``).
    * ``write_contract_digest``: stable digest over all three components in
      canonical order; rotates when any component rotates.

    The branch's stable ``branch_id`` transitively binds
    ``write_contract_digest``, so any contract change rotates ``branch_id``
    and therefore ``integration_id``, ``integration_digest`` and
    ``validation_id``.  No DB schema change.
    """

    branch_id: str
    plan_step_id: str
    work_package_id: str
    attempt_slot_id: str
    attempt_id: str | None
    integration_base_sha: str
    isolation_requirement_id: str
    context_capsule_digest: str
    owner_module_id: str
    writable_existing_paths: tuple[str, ...]
    writable_new_subtrees: tuple[str, ...]
    deny_paths: tuple[str, ...]
    write_contract_digest: str
    ordering_key: str
    candidate_slot_id: str
    candidate_sha: str | None = None


@dataclass(frozen=True)
class IndependentWorkDecision:
    """Auditable admission decision for a set of independent READY packages."""

    run_id: str
    state: IndependenceState
    branches: tuple[IndependentWorkBranch, ...]
    integration_base_sha: str | None
    sequential_fallback_required: bool
    reasons: tuple[str, ...]
    evidence: tuple[str, ...]
    decision_digest: str


@dataclass(frozen=True)
class IntegrationParticipant:
    """Exact branch/candidate slot participating in deterministic integration."""

    branch_id: str
    plan_step_id: str
    work_package_id: str
    attempt_slot_id: str
    candidate_slot_id: str
    candidate_sha: str | None


@dataclass(frozen=True)
class DeterministicIntegration:
    """Representation of deterministic integration; it performs no merge."""

    integration_id: str
    integration_digest: str
    integration_base_sha: str
    participants: tuple[IntegrationParticipant, ...]
    deterministic_order: tuple[str, ...]
    disposition: IntegrationDisposition
    integration_result_sha: str | None
    failure_reason: str | None
    post_integration_validation_required: bool = True


@dataclass(frozen=True)
class PostIntegrationValidation:
    """Validation state cryptographically bound to one integration rendering."""

    validation_id: str
    integration_id: str
    integration_digest: str
    integration_result_sha: str | None
    state: PostIntegrationValidationState
    reason: str | None = None


@dataclass(frozen=True)
class SupervisionRecord:
    """Non-secret durable evidence of supervisor ownership of a worker."""

    run_id: str
    supervisor_identity: str | None
    supervisor_state: str | None
    worker_pid: int | None
    provider: str | None
    model: str | None
    started_at: str | None
    last_activity_at: str | None
    health_state: HealthState
    health_reason: str | None
    current_package_id: str | None
    current_attempt_id: str | None
    deadline_at: str | None
    transcript_path: str | None
    wake_condition: str | None
    manager_state: ManagerState
    updated_at: str
    worker_identity: str | None = None


@dataclass(frozen=True)
class Handoff:
    """An immutable full handoff or compact resume capsule."""

    id: str
    run_id: str
    kind: str
    version: int
    content: str
    created_at: str


@dataclass(frozen=True)
class WorkerCandidate:
    """A specific worker candidate with provider and model identifiers.

    ``binding_id`` is the *exact* C11-B :class:`ExecutionBinding` identity
    this candidate must execute through when present.  A non-empty value
    is the durable, restart-deterministic reference the autonomous
    supervisor and the dispatch lifecycle consume: provider/model alone
    never silently substitutes for it.  Static candidates have no
    ``binding_id`` and resolve through registry declaration order; an
    owner-approved dynamic candidate carries the binding id its
    :class:`~saberops.routing_config.DynamicCandidate` records.

    ``training_required`` is the frozen R2-B data-policy classification
    (``"TRUE"``/``"FALSE"``/``"UNKNOWN"``) attached at chain resolution
    from the Run's frozen routing snapshot.  ``None`` means unresolved:
    the canonical :mod:`saberops.training_policy` decision falls back to
    the frozen snapshot record, then the static v1 knowledge.  It is
    never populated from mutable live discovery/catalog/routing state.
    """

    provider: str
    model: str
    binding_id: str | None = None
    training_required: str | None = None


@dataclass
class QuotaState:
    """Normalized owner-supplied quota state for a provider pool."""

    provider: str
    pool: str
    remaining_percent: float
    reset_at: str | None
    routing_state: RoutingState
    updated_at: str
    # C07 additive fields. Legacy databases/constructors continue to work;
    # the control plane uses these when a durable pool observation supplies
    # them.
    pool_id: str | None = None
    normalized_remaining_pct: float | None = None
    freshness: str | None = None
    confidence: str | None = None
    mode: str | None = None
    red_line_pct: float | None = None
    members: tuple[str, ...] = ()


class ReviewVerdict(StrEnum):
    """Verdict of an independent code review.

    The substantive verdicts are ``PASS`` and ``BLOCK``. The remaining states
    describe non-substantive outcomes that must never be confused with a real
    reviewer rejection:

    * ``PASS_WITH_BACKLOG`` / ``FINAL_BLOCK`` / ``CONVERGENCE_STALLED`` are
      reserved for the multi-round (``max`` mode) convergence flow.
    * ``REVIEW_EXECUTION_FAILED`` marks an infrastructure failure of an
      attempted reviewer (provider down, transport error, reviewer process
      timeout, or empty/unusable output).
    * ``REVIEW_UNAVAILABLE`` marks the case where no reviewer could be
      dispatched at all.
    """

    PASS = "PASS"
    BLOCK = "BLOCK"
    PASS_WITH_BACKLOG = "PASS_WITH_BACKLOG"
    FINAL_BLOCK = "FINAL_BLOCK"
    REVIEW_EXECUTION_FAILED = "REVIEW_EXECUTION_FAILED"
    REVIEW_UNAVAILABLE = "REVIEW_UNAVAILABLE"
    CONVERGENCE_STALLED = "CONVERGENCE_STALLED"


@dataclass
class ReviewResult:
    """Outcome of an independent code review."""

    id: str
    run_id: str
    provider: str
    model: str
    verdict: ReviewVerdict
    summary: str
    details: str = ""
    created_at: str = ""


@dataclass(frozen=True)
class ReviewFinding:
    """A structured reviewer finding persisted alongside its review row.

    Fields mirror the ``review_findings`` columns; classification, severity,
    and repair_scope are stored as their protocol string values.
    """

    id: str
    review_id: str
    run_id: str
    classification: str
    title: str
    severity: str
    contract_reference: str | None
    invariant_or_requirement: str
    code_location: str | None
    failure_scenario: str
    evidence: str
    minimum_correction: str
    repair_scope: str
    blocking: bool
    created_at: str


class CandidateState(StrEnum):
    """Read-only lifecycle projection vocabulary for one run candidate (R3-A).

    This is a PROJECTION, not a second state machine and not new authority:
    every value is derived on each read from existing durable evidence (see
    :mod:`saberops.candidate_lifecycle`).  No DB column, table, migration,
    or write-on-read event backs it.

    * ``READY`` -- a current gate-passed candidate exists; no substantive
      review bound to it yet.  A risk-adaptive "review not required"
      decision stays READY ("review skipped" is never REVIEWED).
    * ``IN_REVIEW`` -- review started for the CURRENT candidate with no
      later substantive completion for that same candidate.
    * ``REVIEWED`` -- a substantive completed review is bound to the
      CURRENT candidate (actual ReviewVerdict preserved separately).
    * ``ACCEPTED`` -- a durable AcceptEngine-owned ``run_accepted`` event
      matches the current candidate.  Terminal for the projection.
    * ``REJECTED`` -- an explicit durable owner Reject decision: a
      canonical ``candidate_rejected`` event whose ``candidate_sha``
      names the exact current candidate (see
      :mod:`saberops.candidate_lifecycle`).  Terminal for the
      candidate unless authoritative acceptance already integrated it
      (ACCEPTED outranks REJECTED); otherwise REJECTED outranks STALE,
      REVIEWED, IN_REVIEW, and READY, and survives later target-HEAD
      movement.  Never synthesized from run status, failed review,
      BLOCK verdict, missing worktree, failed gate, or absent
      acceptance.
    * ``STALE`` -- a current non-accepted candidate based on an obsolete
      target state under the existing acceptance contract.
    * ``CLEANED`` -- reserved: cleanup removes worktrees without writing
      durable candidate-cleaned evidence, so nothing can prove it.
      Unreachable until a later R3 slice adds real cleanup authority.
      Never synthesized from missing worktrees or filesystem state.
    """

    READY = "READY"
    IN_REVIEW = "IN_REVIEW"
    REVIEWED = "REVIEWED"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    STALE = "STALE"
    CLEANED = "CLEANED"


@dataclass
class WorkerRequest:
    """Specification for task dispatch to a worker adapter.

    The ``timeout_seconds`` value is the hard absolute deadline.  The optional
    ``inactivity_timeout`` is a soft expectation: crossing it reports a stall
    (``on_stall``) but never terminates the worker.  The ``on_stdout`` /
    ``on_stderr`` callbacks expose provider output live while the worker is
    still running; adapters that support streaming pass them straight through
    to the process layer.  Callbacks must never raise into the adapter.

    ``on_process_start`` is a non-fatal advisory callback that fires once
    immediately after the provider process starts.  It receives the real
    provider process identity (dict) so the caller can fence later destructive
    operations against PID reuse.  A failure in this callback must not abort
    the worker or truncate output.

    ``on_monitor_transition`` is a non-fatal callback invoked ONLY when the
    worker's lifecycle MonitorState changes (never per sample).  It receives a
    dict payload with at least ``state`` and ``reason`` keys so the caller can
    project durable, normalized lifecycle transitions.  A failure in this
    callback must not abort the worker or truncate output.
    """

    task: str
    worktree_path: Path
    model: str
    timeout_seconds: float = 300.0
    env: dict[str, str] | None = None
    read_only: bool = False
    inactivity_timeout: float | None = None
    on_stdout: Callable[[str], None] | None = None
    on_stderr: Callable[[str], None] | None = None
    on_stall: Callable[[float], None] | None = None
    on_resume: Callable[[], None] | None = None
    on_process_start: Callable[[dict[str, Any]], None] | None = None
    on_monitor_transition: Callable[[dict[str, Any]], None] | None = None
    inherit_env_exclude: tuple[str, ...] = ()
    # C11-D: typed reasoning-effort decision.  When set, the adapter MUST
    # translate this tier to the concrete provider option for the chosen
    # binding and MUST NOT silently fall back to ambient env state
    # (``ORCH_AGY_EFFORT`` for the Antigravity adapter).  When ``None``,
    # adapters may keep their legacy fallback only when explicitly
    # required for backward compatibility with non-autonomous callers.
    reasoning_effort: ReasoningEffort | None = None


@dataclass
class WorkerResult:
    """Result of worker task execution.

    ``stdout``/``stderr`` always contain the complete captured output even
    when the same bytes were already streamed live via ``WorkerRequest``
    callbacks, so streaming never loses output.  ``duration_seconds`` carries
    the observed execution time (including hard-deadline terminations) as
    timeout evidence.

    ``prompt_bytes`` and ``prompt_transport_used`` are execution telemetry
    (never token telemetry): the exact size of ``WorkerRequest.task`` in
    bytes and the transport the adapter actually used to deliver it for
    *this* call (``"argv"`` or ``"stdin"``).  Adapters that support a
    verified large-prompt fallback populate both so callers can inspect
    which path a given dispatch took without re-deriving it from the
    adapter's static :meth:`WorkerAdapter.prompt_transport` capability
    descriptor.

    ``actual_provider``/``actual_model`` optionally preserve the
    provider-reported identity of the model that actually executed a
    dispatch, from the provider's own structured result.  They are evidence
    only: when they disagree with the requested ``provider``/``model``,
    BOTH values are preserved (never silently rewritten) so the
    discrepancy remains inspectable downstream.
    """

    provider: str
    model: str
    status: AttemptStatus
    summary: str
    stdout: str
    stderr: str
    changed_paths: list[str] = field(default_factory=list)
    has_changes: bool = True
    commit_sha: str | None = None
    error: str | None = None
    duration_seconds: float | None = None
    prompt_bytes: int | None = None
    prompt_transport_used: str | None = None
    actual_provider: str | None = None
    actual_model: str | None = None
    outcome: DispatchOutcome | None = None
    usage: list[ProviderUsage] = field(default_factory=list)
    interruption_reason: WorkerInterruptionReason | None = None


@dataclass(frozen=True)
class ProviderUsage:
    """Exact usage from one provider-native structured model-call event."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_tokens: int | None = None
    total_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    usage_source: str = "unknown"
    raw_payload: str | None = None


@dataclass(frozen=True)
class ModelCallUsage:
    """Durable normalized telemetry for one actual or known-unknown model call."""

    id: str
    run_id: str
    association_id: str
    attempt_id: str | None
    review_id: str | None
    stage: DispatchStage
    tier: Tier
    provider: str
    model: str
    prompt_bytes: int
    transport: str
    input_tokens: int | None
    output_tokens: int | None
    cached_tokens: int | None
    total_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    reasoning_tokens: int | None
    usage_source: str
    raw_usage_json: str | None
    created_at: str


@dataclass
class DispatchEvidence:
    """Durable evidence for exactly one actual model dispatch."""

    id: str
    run_id: str
    association_id: str
    stage: DispatchStage
    role: DispatchRole
    tier: Tier
    dispatch_reason: str
    requested_provider: str
    requested_model: str
    started_at: str
    attempt_id: str | None = None
    review_id: str | None = None
    actual_provider: str | None = None
    actual_model: str | None = None
    package_id: str | None = None
    plan_step_id: str | None = None
    base_sha: str | None = None
    candidate_sha: str | None = None
    inherited_checkpoint_sha: str | None = None
    context_mode: str | None = None
    handoff_id: str | None = None
    prompt_bytes: int | None = None
    prompt_sha256: str | None = None
    prompt_sections_json: str | None = None
    raw_parent_included: bool | None = None
    validation_policy_occurrence_count: int | None = None
    prompt_transport: str | None = None
    tool_transport: str | None = None
    authority_mode: str | None = None
    routing_snapshot_digest: str | None = None
    control_plane_digest: str | None = None
    selected_candidate: str | None = None
    prior_failure_refs_json: str | None = None
    dispatch_outcome: str | None = None
    interruption_reason: str | None = None
    interruption_timestamp: str | None = None
    completed_at: str | None = None
    handoff_bytes: int | None = None
    replacement_attempt_id: str | None = None
    time_to_resume_seconds: float | None = None
    tool_schema_bytes: int | None = None
    tool_description_bytes: int | None = None
    tool_discovery_bytes: int | None = None
    tool_result_bytes: int | None = None
    tools_exposed_count: int | None = None
    tools_actually_used_count: int | None = None
    # C10: per-dispatch non-secret gateway evidence (selection, observed
    # account, latency, error classification). None for dispatches that did
    # not exercise a gateway.
    gateway_evidence_json: str | None = None
    # C11-D: exact binding identity chosen by the supervisor (canonical
    # C11-B ``ExecutionBinding``).  ``provider``/``model`` are
    # insufficient: two bindings with the same provider/model remain
    # distinguishable through ``binding_id`` (and the referenced profile
    # / quota pool).  Persisted verbatim so the same identity survives
    # restart without re-derivation from registry declaration order.
    binding_id: str | None = None
    profile_id: str | None = None
    quota_pool_id: str | None = None
    # C11-D: typed resolved effort tier the adapter was instructed to
    # execute at.  ``None`` for non-autonomous dispatches or whenever no
    # canonical resolution was available.
    reasoning_effort: str | None = None


class QuotaRawSemantics(StrEnum):
    """Semantics of a raw provider quota observation."""

    REMAINING_PERCENT = "REMAINING_PERCENT"
    USED_PERCENT = "USED_PERCENT"
    USED_AND_LIMIT = "USED_AND_LIMIT"
    REMAINING_AND_LIMIT = "REMAINING_AND_LIMIT"


@dataclass(frozen=True)
class QuotaObservation:
    """Truthful provider quota/resource observation; never a routing policy."""

    id: str
    provider: str
    account_ref: str | None
    pool: str | None
    window_type: str | None
    raw_value: float | None
    raw_limit: float | None
    raw_semantics: str
    normalized_remaining_pct: float | None
    reset_at: str | None
    observed_at: str
    source: str
    freshness: str | None = None
    confidence: str | None = None
    run_id: str | None = None


@dataclass
class RunResult:
    """Overall result of orchestrator execution."""

    run_id: str
    passed: bool
    status: RunStatus
    summary: str
    target_repo: str
    base_commit: str
    branch_name: str | None = None
    commit_sha: str | None = None
    worktree_path: str | None = None
    has_changes: bool = True
    attempts: list[Attempt] = field(default_factory=list)
    checks: list[CheckResult] = field(default_factory=list)
    reviews: list[ReviewResult] = field(default_factory=list)
    # Candidate-publication evidence for this run's reviewable candidate.
    # ``candidate_published`` is True only when the exact candidate SHA was
    # pushed (non-force) to the candidate branch on the remote AND the remote
    # ref was independently verified to resolve to that exact SHA.  These are
    # candidate-visibility fields only; accepted-target publication is a
    # separate authority dimension and is never reported here.
    candidate_published: bool = False
    candidate_remote: str | None = None
    candidate_remote_branch: str | None = None
    candidate_verified_remote_sha: str | None = None


# ---------------------------------------------------------------------------
# C14-C: canonical production phase vocabulary.
#
# ROADMAP §11 asks for wall-clock decomposition of the orchestration runtime
# into the eleven named phases below.  Each phase MUST be reported using one
# of the values defined here; ad-hoc strings are not a substitute.  New
# phases can be added later by widening this enum explicitly, never by
# sprinkling free-form labels through the runtime.
# ---------------------------------------------------------------------------


class PhaseKind(StrEnum):
    """Canonical production phase vocabulary (ROADMAP §11).

    The values are deliberately stable strings: callers and durable
    evidence readers exchange them verbatim, so renaming or re-ordering
    requires a deliberate migration.
    """

    LEDGER_DB = "LEDGER_DB"
    CONTEXT_SELECTION_RENDERING = "CONTEXT_SELECTION_RENDERING"
    DIGEST_CALCULATION = "DIGEST_CALCULATION"
    ROUTING_POLICY = "ROUTING_POLICY"
    SANDBOX_SETUP = "SANDBOX_SETUP"
    PROCESS_STARTUP = "PROCESS_STARTUP"
    MODEL_WAIT = "MODEL_WAIT"
    TOOL_EXECUTION = "TOOL_EXECUTION"
    DETERMINISTIC_VALIDATION = "DETERMINISTIC_VALIDATION"
    REVIEW = "REVIEW"
    HUMAN_WAIT = "HUMAN_WAIT"


CANONICAL_PHASES: frozenset[str] = frozenset(phase.value for phase in PhaseKind)


class PhaseStatus(StrEnum):
    """Honest measurement status for a single phase observation.

    * ``MEASURED``        — duration is real and was actually measured at a
      real production boundary using a monotonic clock.
    * ``UNKNOWN``         — no exact production boundary currently exists;
      the duration is genuinely unknown and MUST NOT be invented.
    * ``NOT_APPLICABLE``  — the phase is positively known not to apply to
      this run (e.g. a path that does not exercise review for a run with
      review disabled).

    Zero-duration MEASURED values are permitted ONLY when zero is itself
    directly and positively established at the real boundary; they are
    never a fabricated placeholder.
    """

    MEASURED = "MEASURED"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


CANONICAL_PHASE_STATUSES: frozenset[str] = frozenset(status.value for status in PhaseStatus)


@dataclass(frozen=True)
class PhaseObservation:
    """One durable C14-C production phase observation.

    ``phase`` and ``status`` are persisted as the canonical enum strings
    so the row survives schema inspection without re-importing the enums.
    ``duration_seconds`` carries the real monotonic-derived duration when
    ``status`` is :attr:`PhaseStatus.MEASURED`; it is ``None`` for
    :attr:`PhaseStatus.UNKNOWN` and :attr:`PhaseStatus.NOT_APPLICABLE`.
    """

    id: str
    run_id: str
    phase: str
    status: str
    duration_seconds: float | None
    source_kind: str
    boundary_kind: str | None
    attempt_id: str | None
    review_id: str | None
    package_id: str | None
    plan_step_id: str | None
    started_monotonic: float | None
    ended_monotonic: float | None
    observed_at: str
    notes: str | None = None

    def __post_init__(self) -> None:  # pragma: no cover - structural guard
        if self.phase not in CANONICAL_PHASES:
            raise ValueError(f"phase '{self.phase}' is not a canonical ROADMAP §11 phase")
        if self.status not in CANONICAL_PHASE_STATUSES:
            raise ValueError(f"phase status '{self.status}' is not a canonical PhaseStatus value")
        if self.status == PhaseStatus.MEASURED.value:
            if self.duration_seconds is None:
                raise ValueError("MEASURED phase observation must carry a duration")
            if self.started_monotonic is None or self.ended_monotonic is None:
                raise ValueError(
                    "MEASURED phase observation must carry monotonic start/end anchors"
                )
            # Fail closed on ALL non-finite numeric evidence: NaN and the
            # infinities are not measurements and must never become durable
            # MEASURED evidence.
            if not math.isfinite(self.duration_seconds):
                raise ValueError("MEASURED phase observation duration must be finite")
            if not math.isfinite(self.started_monotonic) or not math.isfinite(
                self.ended_monotonic
            ):
                raise ValueError("MEASURED phase observation monotonic anchors must be finite")
            if self.ended_monotonic < self.started_monotonic:
                raise ValueError("MEASURED phase observation ended before it started")
            if self.duration_seconds < 0:
                raise ValueError("MEASURED phase observation duration must be >= 0")
            # The persisted duration and the monotonic anchors must be
            # mutually consistent: the duration is exactly the anchor
            # delta, so a caller cannot claim a duration that contradicts
            # its own anchors.
            if self.duration_seconds != self.ended_monotonic - self.started_monotonic:
                raise ValueError(
                    "MEASURED phase observation duration must equal "
                    "ended_monotonic - started_monotonic"
                )
        else:
            if self.duration_seconds is not None:
                raise ValueError(f"{self.status} phase observation must not carry a duration")
            if self.started_monotonic is not None or self.ended_monotonic is not None:
                raise ValueError(
                    f"{self.status} phase observation must not carry monotonic anchors"
                )
