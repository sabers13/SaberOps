"""Canonical autonomous runtime: typed supervisor decisions.

The canonical execution lifecycle is :meth:`Orchestrator._run_core` and only
that.  :class:`AutonomousSupervisor` holds decisions and state only.  It
reconstructs durable state, evaluates :class:`FailoverEngine` results, computes
durable budget/stall/no-progress state, selects the exact eligible binding, and
resolves binding-specific reasoning effort.  It never executes adapters, never
touches worktrees/candidates/gates/reviews/publication, and never accepts runs.
:meth:`AutonomousSupervisor.drive` is legacy-only and is never on the
production path (it raises).  :func:`_verify_accept_engine_evidence` checks
durable AcceptEngine evidence for the candidate lifecycle.

Production authority always comes from the persisted owner settings
(:func:`load_authority_settings`); nothing here accepts a caller-supplied
authority override.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from saberops.authority import AuthorityPolicy, load_authority_settings
from saberops.control_plane.bindings import (
    BindingRegistry,
    BindingResolutionError,
    ResolvedExecutionBinding,
    resolve_execution_binding,
)
from saberops.control_plane.effort import (
    DEFAULT_WORKER_EFFORT_POLICY,
    EFFORT_UNSUPPORTED,
    EffortDecisionStatus,
    EffortPolicy,
    resolve_effort,
)
from saberops.control_plane.orch_binding import OrchBindingPolicy
from saberops.db import Database
from saberops.execution_failover import FailoverAction, FailoverEngine
from saberops.models import (
    Attempt,
    AttemptStatus,
    BindingRole,
    FailureKind,
    ReasoningEffort,
    WorkerCandidate,
)
from saberops.process import AutonomousDispatchConfig


class SupervisorAction(StrEnum):
    """The bounded set of autonomous supervisor decisions.

    Decisions name work to do or a reason to stop; the canonical
    ``_run_core`` lifecycle performs it.  There is deliberately no
    CANDIDATE_READY / ACCEPT action: candidate-ready is a lifecycle
    transition owned by ``_run_core``, and acceptance is owned solely by
    :class:`AcceptEngine`.
    """

    LAUNCH_ATTEMPT = "LAUNCH_ATTEMPT"
    FAILOVER_NEW_ATTEMPT = "FAILOVER_NEW_ATTEMPT"
    STOP = "STOP"


class SupervisorStopReason(StrEnum):
    """Typed reason a STOP decision was produced.  Persisted verbatim."""

    SUCCESS = "SUCCESS"
    BLOCKED_ESCALATE = "BLOCKED_ESCALATE"
    ATTEMPT_BUDGET_EXHAUSTED = "ATTEMPT_BUDGET_EXHAUSTED"
    FAILOVER_BUDGET_EXHAUSTED = "FAILOVER_BUDGET_EXHAUSTED"
    NO_PROGRESS = "NO_PROGRESS"
    NO_ELIGIBLE_BINDING = "NO_ELIGIBLE_BINDING"
    STALL = "STALL"
    AUTHORITY_REFUSED = "AUTHORITY_REFUSED"


@dataclass(frozen=True)
class SupervisorDecision:
    """One typed, durable supervisor decision.

    ``binding_provider`` / ``binding_model`` name the exact eligible binding
    the canonical runtime must dispatch (never a raw provider/model bypass:
    the binding always comes from the eligible candidate ladder the runtime
    supplied).  ``reasoning_effort`` is the binding-specific resolved effort.
    ``required_base_sha`` is the FailoverEngine required-base anchor
    copied verbatim for SAME_BINDING / DIFFERENT_BINDING decisions
    (``None`` for initial launches and STOPs): the canonical lifecycle
    validates it against the source Attempt's actual base before
    creating Attempt B.  It is the authorization anchor, never the
    worktree start itself (a checkpoint continuation starts at the
    checkpoint while retaining this anchor).
    """

    action: SupervisorAction
    run_id: str
    binding_id: str | None = None
    profile_id: str | None = None
    quota_pool_id: str | None = None
    binding_provider: str | None = None
    binding_model: str | None = None
    reasoning_effort: str = ReasoningEffort.MEDIUM.value
    failover_from_attempt_id: str | None = None
    required_base_sha: str | None = None
    stop_reason: SupervisorStopReason | None = None
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Render the decision as a JSON-compatible mapping for events."""
        return {
            "action": self.action.value,
            "run_id": self.run_id,
            "binding_id": self.binding_id,
            "profile_id": self.profile_id,
            "quota_pool_id": self.quota_pool_id,
            "binding_provider": self.binding_provider,
            "binding_model": self.binding_model,
            "reasoning_effort": self.reasoning_effort,
            "failover_from_attempt_id": self.failover_from_attempt_id,
            "required_base_sha": self.required_base_sha,
            "stop_reason": self.stop_reason.value if self.stop_reason else None,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class SupervisorBudgets:
    """Durable-at-read budget configuration for autonomous decisions."""

    max_failovers: int = 3
    max_no_progress: int = 2
    stall_seconds: float = 0.0  # <= 0 disables stall detection

    def as_dict(self) -> dict[str, Any]:
        """Render the bounded autonomous budget contract."""
        return {
            "max_failovers": self.max_failovers,
            "max_no_progress": self.max_no_progress,
            "stall_seconds": self.stall_seconds,
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> SupervisorBudgets:
        """Reconstruct autonomous budgets from durable values."""
        if not isinstance(payload, Mapping):
            raise ValueError("supervisor budgets must be a mapping")
        return cls(
            max_failovers=int(payload.get("max_failovers", 3)),
            max_no_progress=int(payload.get("max_no_progress", 2)),
            stall_seconds=float(payload.get("stall_seconds", 0.0)),
        )


@dataclass(frozen=True)
class AutonomousRuntimeContract:
    """Complete non-secret autonomous execution contract for one run.

    The contract is stored inside the existing ``Run.config_json`` payload so
    a fresh recovery process can rebuild the same autonomous boundary and
    exact binding registry without relying on caller memory or ambient config.
    """

    dispatch: AutonomousDispatchConfig
    binding_registry: BindingRegistry | None
    budgets: SupervisorBudgets = field(default_factory=SupervisorBudgets)
    effort_policy: EffortPolicy = DEFAULT_WORKER_EFFORT_POLICY
    requested_effort: ReasoningEffort | None = None
    binding_policy: OrchBindingPolicy | None = None

    def as_dict(self) -> dict[str, Any]:
        """Render only reconstructable, non-secret configuration."""
        return {
            "schema_version": 1,
            "dispatch": self.dispatch.as_dict(),
            "binding_registry": (
                None
                if self.binding_registry is None
                else self.binding_registry.to_dict()
            ),
            "budgets": self.budgets.as_dict(),
            "effort_policy": self.effort_policy.to_dict(),
            "requested_effort": (
                None if self.requested_effort is None else self.requested_effort.value
            ),
            "binding_policy": (
                None if self.binding_policy is None else self.binding_policy.to_dict()
            ),
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> AutonomousRuntimeContract:
        """Rebuild a complete autonomous contract after process restart."""
        if not isinstance(payload, Mapping):
            raise ValueError("autonomous runtime contract must be a mapping")
        if payload.get("schema_version", 1) != 1:
            raise ValueError("unsupported autonomous runtime contract schema_version")
        dispatch_raw = payload.get("dispatch")
        if not isinstance(dispatch_raw, Mapping):
            raise ValueError("autonomous runtime contract dispatch is required")
        registry_raw = payload.get("binding_registry")
        if registry_raw is not None and not isinstance(registry_raw, Mapping):
            raise ValueError("autonomous runtime contract binding_registry must be a mapping")
        budgets_raw = payload.get("budgets", {})
        effort_raw = payload.get("effort_policy", {})
        policy_raw = payload.get("binding_policy")
        if not isinstance(budgets_raw, Mapping) or not isinstance(effort_raw, Mapping):
            raise ValueError("autonomous runtime contract policy fields must be mappings")
        if policy_raw is not None and not isinstance(policy_raw, Mapping):
            raise ValueError("autonomous runtime contract binding_policy must be a mapping")
        requested_raw = payload.get("requested_effort")
        requested = None
        if requested_raw not in (None, ""):
            try:
                requested = ReasoningEffort(str(requested_raw))
            except ValueError as exc:
                raise ValueError("unknown autonomous requested effort") from exc
        return cls(
            dispatch=AutonomousDispatchConfig.from_mapping(dispatch_raw),
            binding_registry=(
                None
                if registry_raw is None
                else BindingRegistry.from_mapping(registry_raw)
            ),
            budgets=SupervisorBudgets.from_mapping(budgets_raw),
            effort_policy=EffortPolicy.from_mapping(effort_raw),
            requested_effort=requested,
            binding_policy=(
                None
                if policy_raw is None
                else OrchBindingPolicy.from_mapping(policy_raw)
            ),
        )


def _is_evidence_uncertain(attempt: Attempt) -> bool:
    from saberops.process import is_evidence_uncertain_error

    return is_evidence_uncertain_error(attempt.worker_error)


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


class AutonomousSupervisor:
    """Decision/state-only autonomous supervisor.

    Constructed with durable handles only (:class:`Database`, adapter
    registry, optional binding registry and budgets).  Production authority
    is read live from the persisted owner settings on every decision, so an
    owner narrowing authority mid-run takes effect on the next decision.

    Separation invariant: worker binding selection
    derives from the canonical :class:`BindingRegistry` plus the per-
    candidate ``binding_id`` reference carried by the routing chain
    (a :class:`WorkerCandidate` whose ``binding_id`` is non-empty
    resolves THAT binding exactly; empty falls back to the first
    registry-declared WORKER-role match).  :class:`OrchBindingPolicy`
    belongs to the Orchestrator-model slot and is **not** consulted
    for worker dispatch -- changing an owner
    :class:`OrchBindingPolicy` MUST NOT change which binding a worker
    routing candidate resolves to.
    """

    def __init__(
        self,
        db: Database,
        registry: Any | None = None,
        *,
        binding_registry: Any | None = None,
        budgets: SupervisorBudgets | None = None,
        effort_policy: EffortPolicy | None = None,
        requested_effort: ReasoningEffort | None = None,
        binding_policy: OrchBindingPolicy | None = None,
    ) -> None:
        self.db = db
        self.registry = registry
        self.binding_registry = binding_registry
        self.budgets = budgets or SupervisorBudgets()
        self.effort_policy = effort_policy or DEFAULT_WORKER_EFFORT_POLICY
        self.requested_effort = requested_effort
        # ``binding_policy`` is preserved on the supervisor instance so
        # existing orchestration code that reads the attribute keeps
        # working, but it is **never** consulted by worker binding
        # selection.  Worker dispatch uses the canonical
        # BindingRegistry + WorkerCandidate.binding_id.
        self.binding_policy = binding_policy
        self._failover = FailoverEngine(db=db)

    # -- authority (persisted owner state; no caller override exists) --------

    @property
    def _policy(self) -> AuthorityPolicy:
        return load_authority_settings().policy

    @property
    def allows_retry(self) -> bool:
        """Whether the persisted policy permits autonomous retry."""
        return self._policy.auto_retry

    @property
    def allows_repair(self) -> bool:
        """Whether the persisted policy permits autonomous repair."""
        return self._policy.auto_repair

    @property
    def allows_escalation(self) -> bool:
        """Whether the persisted policy permits autonomous escalation."""
        return self._policy.auto_escalate_when_policy_justifies

    # -- decisions ------------------------------------------------------------

    # -- exact execution binding (identity, never provider/model) -------

    def _registry_bindings(self) -> tuple[Any, ...]:
        """Return every configured execution binding in registry order."""
        list_bindings = getattr(self.binding_registry, "list_bindings", None)
        if not callable(list_bindings):
            return ()
        try:
            return tuple(list_bindings())
        except Exception:
            return ()

    def _worker_eligible_bindings(self) -> tuple[Any, ...]:
        """Return every WORKER-role binding in registry declaration order.

        Worker dispatch only consumes bindings whose
        :class:`BindingRole` is :class:`BindingRole.WORKER`.  An
        ORCHESTRATOR-role binding is **not** eligible for worker
        execution; reusing one as worker dispatch authority would be a
        role violation.  The :class:`OrchBindingPolicy` (an
        Orchestrator-model concept) is deliberately ignored here --
        changing it MUST NOT change which binding a worker-routing
        candidate resolves to.
        """
        configured = self._registry_bindings()
        return tuple(
            binding
            for binding in configured
            if getattr(binding, "binding_role", None) is BindingRole.WORKER
        )

    def _policy_bindings(self) -> tuple[Any, ...]:
        """Return the WORKER-role eligible bindings (historical alias).

        Separation: the historical :class:`OrchBindingPolicy`
        parameter is the Orchestrator-model policy, NOT a worker-binding
        selection policy.  Worker binding selection now consumes the
        canonical WORKER-role :class:`BindingRegistry` declarations
        directly; the optional persisted :attr:`binding_policy` is read
        only by the orchestrator-model selection surface and is
        deliberately not consulted here.

        This helper remains under its historical name for callers that
        still iterate eligible bindings (e.g. failover exploration).
        The result is the WORKER-role subset of the registry in
        declaration order -- identical to the prior ``AUTO`` behavior
        for worker dispatch, with the additional role filter.
        """
        return self._worker_eligible_bindings()

    def _binding_for_candidate(
        self,
        *,
        provider: str | None,
        model: str | None,
        binding_id: str | None = None,
    ) -> Any | None:
        """Return the exact eligible registry binding for one candidate.

        Resolution order:

        1. If ``binding_id`` is non-empty, the exact binding with that
           id is returned when its role is :class:`BindingRole.WORKER`
           and its ``provider``/``model`` agree with ``provider``/``model``.
           A missing binding id, a wrong role, or a mismatched
           provider/model causes ``None`` -- fail closed, never a
           substitution by ``provider``/``model``.
        2. Otherwise the first WORKER-role registry declaration whose
           ``provider``/``model`` matches is returned (the historical
           static-chain behavior preserved for non-approval candidates).

        The owner's persisted :class:`OrchBindingPolicy` is **not**
        consulted here -- worker dispatch is not its authority.
        """
        if not provider or not model:
            return None
        eligible = self._worker_eligible_bindings()
        if binding_id and binding_id.strip():
            wanted = binding_id.strip()
            for binding in eligible:
                if getattr(binding, "binding_id", None) != wanted:
                    continue
                if (
                    getattr(binding, "provider", None) == provider
                    and getattr(binding, "model", None) == model
                ):
                    return binding
                # Exact binding id is known but its provider/model no
                # longer agrees with the routing chain -- never
                # silently substitute by provider/model.
                return None
            # Exact binding id is referenced but the registry does not
            # name it (or it is ORCHESTRATOR-only); fail closed.
            return None
        for binding in eligible:
            if (
                getattr(binding, "provider", None) == provider
                and getattr(binding, "model", None) == model
            ):
                return binding
        return None

    def _binding_by_id(self, binding_id: str | None) -> Any | None:
        """Return the registry binding with this id, or ``None``."""
        if not binding_id:
            return None
        try_get = getattr(self.binding_registry, "try_get", None)
        if callable(try_get):
            try:
                return try_get(binding_id)
            except Exception:
                return None
        for binding in self._registry_bindings():
            if getattr(binding, "binding_id", None) == binding_id:
                return binding
        return None

    def resolve_candidate_binding(
        self, candidate: WorkerCandidate
    ) -> ResolvedExecutionBinding:
        """Resolve a repair/continuation candidate through the same exact seam."""
        binding = self._binding_for_candidate(
            provider=candidate.provider,
            model=candidate.model,
            binding_id=getattr(candidate, "binding_id", None),
        )
        if binding is None:
            raise BindingResolutionError("UNKNOWN_BINDING")
        status, effort, _reason = self._resolve_binding_effort(binding)
        if status is not EffortDecisionStatus.RESOLVED or effort is None:
            raise BindingResolutionError("EFFORT_NOT_IN_BINDING")
        adapter = self.registry.get(candidate.provider) if self.registry is not None else None
        return resolve_execution_binding(
            registry=self.binding_registry,
            binding_id=getattr(binding, "binding_id", None),
            adapter=adapter,
            reasoning_effort=effort,
        )

    def _binding_identity(
        self, binding: Any | None, *, provider: str | None, model: str | None
    ) -> tuple[str | None, str | None, str | None]:
        """Return ``(binding_id, profile_id, quota_pool_id)`` for a binding.

        A registry binding contributes its exact identity.  Without
        one there is no executable identity to return (a raw
        provider/model name never synthesizes one); callers fail
        closed instead of launching.
        """
        if binding is not None:
            return (
                getattr(binding, "binding_id", None),
                getattr(binding, "profile_id", None),
                getattr(binding, "quota_pool_id", None),
            )
        return None, None, None

    def _prior_binding_id(self, run_id: str, attempt_id: str) -> str | None:
        """Reconstruct the exact binding identity of a past Attempt.

        Reads the durable dispatch evidence -- never the registry
        declaration order -- so a fresh supervisor after restart
        recovers the same binding_id the Attempt actually dispatched
        with.
        """
        try:
            for evidence in self.db.get_dispatch_evidence_for_run(run_id):
                if evidence.attempt_id == attempt_id and evidence.binding_id:
                    return str(evidence.binding_id)
        except Exception:
            return None
        return None

    # -- canonical binding effort resolution (no local algorithm) ----------------

    def _resolve_binding_effort(
        self, binding: Any | None
    ) -> tuple[EffortDecisionStatus, ReasoningEffort | None, str]:
        """Resolve effort through the canonical binding resolver.

        Returns ``(status, resolved_tier, reason)`` preserving the typed
        RESOLVED / UNSUPPORTED / UNAUTHORIZED outcomes exactly -- never
        a silent downgrade.  Without an exact configured binding there
        are no declared capabilities to resolve against, so the
        outcome is UNSUPPORTED (zero launch): no binding means no
        declared effort capability, and ULTRA in particular is never
        fabricated or downgraded.
        """
        if binding is None:
            return (
                EffortDecisionStatus.UNSUPPORTED,
                None,
                EFFORT_UNSUPPORTED,
            )
        decision = resolve_effort(
            binding,
            self.effort_policy,
            requested_effort=self.requested_effort,
        )
        reason = getattr(decision, "reason", "")
        return decision.status, decision.resolved_effort, str(reason)

    # -- typed failure evidence for the failover engine ------------------------

    @staticmethod
    def _failure_kind_for_trace(trace: Any | None) -> FailureKind | None:
        """Return typed failure evidence for one Attempt trace, if any.

        Uses only existing typed evidence: the persisted trace's
        ``failure_kind`` when it parses to a valid :class:`FailureKind`.
        Returns ``None`` when genuinely unknown -- never a fabricated
        specific kind (the engine itself defaults TIMEOUT attempts).
        """
        raw = getattr(trace, "failure_kind", None) if trace is not None else None
        if isinstance(raw, FailureKind):
            return raw
        if isinstance(raw, str) and raw:
            try:
                return FailureKind(raw)
            except ValueError:
                return None
        return None

    def _failover_consumed(self, run_id: str) -> int:
        return sum(
            1
            for event in self.db.get_events_by_type(
                run_id, "autonomous_supervisor_decision"
            )
            if isinstance(event.payload, dict)
            and event.payload.get("action") == SupervisorAction.FAILOVER_NEW_ATTEMPT.value
        )

    def decide(
        self, run_id: str, *, candidates: Sequence[WorkerCandidate]
    ) -> SupervisorDecision:
        """Compute the next typed autonomous decision from durable state.

        Pure decision/state: reads the run row, attempts, events, review
        loop state and persisted authority; executes nothing and mutates
        nothing (callers persist the returned decision as an event).
        """
        policy = self._policy
        run = self.db.get_run(run_id)
        if run is None:
            raise ValueError(f"Cannot decide for unknown run '{run_id}'")
        if not policy.auto_start:
            return SupervisorDecision(
                action=SupervisorAction.STOP,
                run_id=run_id,
                stop_reason=SupervisorStopReason.AUTHORITY_REFUSED,
                detail="persisted owner authority does not auto-start autonomous work",
            )
        attempts = self.db.get_attempts_for_run(run_id)
        terminal = [
            a
            for a in attempts
            if a.status in (AttemptStatus.FAILED, AttemptStatus.TIMEOUT)
        ]
        if any(a.status == AttemptStatus.SUCCESS for a in attempts):
            return SupervisorDecision(
                action=SupervisorAction.STOP,
                run_id=run_id,
                stop_reason=SupervisorStopReason.SUCCESS,
                detail="a successful candidate attempt already exists",
            )
        ladder = list(candidates)
        if not ladder:
            return SupervisorDecision(
                action=SupervisorAction.STOP,
                run_id=run_id,
                stop_reason=SupervisorStopReason.NO_ELIGIBLE_BINDING,
                detail="eligible candidate ladder is empty",
            )
        if not terminal:
            running = [a for a in attempts if a.status == AttemptStatus.RUNNING]
            if running and self.budgets.stall_seconds > 0:
                latest_event_at: datetime | None = None
                for event in self.db.get_complete_events_for_run(run_id):
                    created = getattr(event, "created_at", None)
                    parsed = _parse_ts(created) if isinstance(created, str) else None
                    if parsed is not None and (
                        latest_event_at is None or parsed > latest_event_at
                    ):
                        latest_event_at = parsed
                now = datetime.now(UTC)
                if (
                    latest_event_at is not None
                    and (now - latest_event_at).total_seconds()
                    > self.budgets.stall_seconds
                ):
                    return SupervisorDecision(
                        action=SupervisorAction.STOP,
                        run_id=run_id,
                        stop_reason=SupervisorStopReason.STALL,
                        detail="running attempt produced no durable progress within budget",
                    )
            head = ladder[0]
            binding = self._binding_for_candidate(
                provider=head.provider,
                model=head.model,
                binding_id=getattr(head, "binding_id", None),
            )
            if binding is None:
                # Production autonomous execution requires an
                # authoritative non-empty BindingRegistry: a ladder
                # head with no exact WORKER-role registry binding
                # (or with an exact binding_id whose registry entry is
                # missing/ORCHESTRATOR-only) is not an eligible
                # autonomous binding.  A raw provider/model override
                # can never synthesize one.  Fail closed with zero launch.
                return SupervisorDecision(
                    action=SupervisorAction.STOP,
                    run_id=run_id,
                    stop_reason=SupervisorStopReason.NO_ELIGIBLE_BINDING,
                    detail=(
                        f"ladder head {head.provider}/{head.model} names no "
                        "exact WORKER-role ExecutionBinding; zero launch"
                    ),
                )
            binding_id, profile_id, quota_pool_id = self._binding_identity(
                binding, provider=head.provider, model=head.model
            )
            status, tier, reason = self._resolve_binding_effort(binding)
            if status is not EffortDecisionStatus.RESOLVED or tier is None:
                stop = (
                    SupervisorStopReason.AUTHORITY_REFUSED
                    if status is EffortDecisionStatus.UNAUTHORIZED
                    else SupervisorStopReason.NO_ELIGIBLE_BINDING
                )
                return SupervisorDecision(
                    action=SupervisorAction.STOP,
                    run_id=run_id,
                    stop_reason=stop,
                    detail=(
                        "canonical effort resolution refused the head "
                        f"binding ({status.value}/{reason}); zero launch"
                    ),
                )
            return SupervisorDecision(
                action=SupervisorAction.LAUNCH_ATTEMPT,
                run_id=run_id,
                binding_id=binding_id,
                profile_id=profile_id,
                quota_pool_id=quota_pool_id,
                binding_provider=head.provider,
                binding_model=head.model,
                reasoning_effort=tier.value,
                detail="first autonomous dispatch on the eligible ladder head",
            )
        last = terminal[-1]
        # Every autonomous retry/failover is authorized by the
        # canonical FailoverEngine after trace/effect
        # reconciliation.  The supervisor never infers retry/failover
        # locally from provider/model, worker status, or remaining
        # ladder entries; the engine result below is authoritative.
        trace = self.db.get_attempt_trace_for_attempt(last.id)
        failover = self._failover.evaluate(
            attempt=last,
            trace=trace,
            failure_kind=self._failure_kind_for_trace(trace),
        )
        try:
            self.db.record_event(
                run_id,
                "autonomous_failover_evaluated",
                attempt_id=last.id,
                payload=failover.as_dict(),
            )
        except Exception:
            pass
        if _is_evidence_uncertain(last):
            return SupervisorDecision(
                action=SupervisorAction.STOP,
                run_id=run_id,
                stop_reason=SupervisorStopReason.BLOCKED_ESCALATE,
                detail=(
                    f"evidence-uncertain attempt {last.id}: FailoverEngine "
                    f"{failover.action.value}/{failover.reason_code.value}; "
                    "no blind replay"
                ),
            )
        if failover.action is FailoverAction.NOOP_TERMINAL:
            return SupervisorDecision(
                action=SupervisorAction.STOP,
                run_id=run_id,
                stop_reason=SupervisorStopReason.SUCCESS,
                detail=(
                    f"FailoverEngine NOOP_TERMINAL for attempt {last.id} "
                    f"({failover.reason_code.value}); no new Attempt"
                ),
            )
        if failover.action is FailoverAction.BLOCKED_ESCALATE:
            return SupervisorDecision(
                action=SupervisorAction.STOP,
                run_id=run_id,
                stop_reason=SupervisorStopReason.BLOCKED_ESCALATE,
                detail=(
                    f"FailoverEngine BLOCKED_ESCALATE for attempt {last.id} "
                    f"({failover.reason_code.value}): {failover.reason}"
                ),
            )
        if not policy.auto_retry:
            return SupervisorDecision(
                action=SupervisorAction.STOP,
                run_id=run_id,
                stop_reason=SupervisorStopReason.AUTHORITY_REFUSED,
                detail=(
                    "persisted owner authority forbids autonomous retry; "
                    f"attempt {last.id} already terminal"
                ),
            )
        # Required-base validation before Attempt B may exist.  The
        # anchor is Attempt A's ACTUAL execution base -- never
        # ``run.base_commit``, which a continued Attempt's base
        # generally differs from.  A missing anchor, a missing source
        # Attempt, or any disagreement with the source Attempt's
        # durable base fails closed with zero Attempt B.
        if failover.requires_base_validation:
            if not failover.required_base_sha:
                return SupervisorDecision(
                    action=SupervisorAction.STOP,
                    run_id=run_id,
                    stop_reason=SupervisorStopReason.BLOCKED_ESCALATE,
                    detail=(
                        "FailoverEngine requires base validation for attempt "
                        f"{last.id} but carries no required_base_sha; "
                        "zero Attempt B"
                    ),
                )
            source_attempt = self.db.get_attempt(failover.source_attempt_id)
            if (
                source_attempt is None
                or source_attempt.base_sha != failover.required_base_sha
            ):
                return SupervisorDecision(
                    action=SupervisorAction.STOP,
                    run_id=run_id,
                    stop_reason=SupervisorStopReason.BLOCKED_ESCALATE,
                    detail=(
                        "FailoverEngine requires base validation for attempt "
                        f"{last.id} but required_base_sha "
                        f"{failover.required_base_sha!r} does not match the "
                        "source Attempt's actual base "
                        f"{getattr(source_attempt, 'base_sha', None)!r}; "
                        "zero Attempt B"
                    ),
                )
        behavior = failover.required_binding_behavior
        source_binding_id = self._prior_binding_id(
            run_id, failover.source_attempt_id
        )
        if not source_binding_id:
            # No durable dispatch evidence names the exact binding
            # Attempt A executed: the failover source identity cannot
            # be reconstructed without guessing.  Fail closed.
            return SupervisorDecision(
                action=SupervisorAction.STOP,
                run_id=run_id,
                stop_reason=SupervisorStopReason.NO_ELIGIBLE_BINDING,
                detail=(
                    f"FailoverEngine {failover.action.value} after terminal "
                    f"attempt {last.id} but no durable dispatch evidence "
                    "names the exact executed binding; failing closed "
                    "without guessing"
                ),
            )
        if (
            failover.action is FailoverAction.NEW_ATTEMPT_SAME_BINDING
            or behavior == "SAME_BINDING"
        ):
            return self._same_binding_decision(
                run_id=run_id,
                ladder=ladder,
                terminal=terminal,
                last=last,
                source_binding_id=source_binding_id,
                failover= failover,
            )
        if (
            failover.action is FailoverAction.NEW_ATTEMPT_DIFFERENT_BINDING
            or behavior == "DIFFERENT_BINDING"
        ):
            return self._different_binding_decision(
                run_id=run_id,
                ladder=ladder,
                last=last,
                source_binding_id=source_binding_id,
                failover=failover,
            )
        return SupervisorDecision(
            action=SupervisorAction.STOP,
            run_id=run_id,
            stop_reason=SupervisorStopReason.BLOCKED_ESCALATE,
            detail=(
                f"FailoverEngine returned an unrecognized binding behavior "
                f"({failover.action.value}/{behavior}) for attempt {last.id}; "
                "failing closed without launch"
            ),
        )

    def _ladder_compatible(
        self, ladder: Sequence[WorkerCandidate], *, provider: str, model: str
    ) -> bool:
        """True iff ``(provider, model)`` is on the quota-eligible ladder."""
        return any(
            c.provider == provider and c.model == model for c in ladder
        )

    def _effort_stop(
        self,
        run_id: str,
        *,
        status: EffortDecisionStatus,
        reason: str,
        binding_id: str | None,
    ) -> SupervisorDecision:
        """STOP for a non-RESOLVED canonical effort outcome (zero launch)."""
        stop = (
            SupervisorStopReason.AUTHORITY_REFUSED
            if status is EffortDecisionStatus.UNAUTHORIZED
            else SupervisorStopReason.NO_ELIGIBLE_BINDING
        )
        return SupervisorDecision(
            action=SupervisorAction.STOP,
            run_id=run_id,
            stop_reason=stop,
            detail=(
                "canonical effort resolution refused binding "
                f"{binding_id} ({status.value}/{reason}); zero launch"
            ),
        )

    def _same_binding_decision(
        self,
        *,
        run_id: str,
        ladder: Sequence[WorkerCandidate],
        terminal: list[Attempt],
        last: Attempt,
        source_binding_id: str,
        failover: Any,
    ) -> SupervisorDecision:
        """Consume a SAME_BINDING engine verdict with the exact binding_id."""
        binding = self._binding_by_id(source_binding_id)
        if binding is None:
            # No registry (or registry rotated): the exact source
            # binding cannot be reconstructed -- never guess it from
            # provider/model.  Fail closed.
            return SupervisorDecision(
                action=SupervisorAction.STOP,
                run_id=run_id,
                stop_reason=SupervisorStopReason.NO_ELIGIBLE_BINDING,
                detail=(
                    f"SAME_BINDING requires exact binding "
                    f"{source_binding_id} which the registry no longer "
                    "names; failing closed without guessing"
                ),
            )
        if source_binding_id not in {
            getattr(eligible, "binding_id", None)
            for eligible in self._worker_eligible_bindings()
        }:
            return SupervisorDecision(
                action=SupervisorAction.STOP,
                run_id=run_id,
                stop_reason=SupervisorStopReason.NO_ELIGIBLE_BINDING,
                detail=(
                    f"SAME_BINDING requires exact binding "
                    f"{source_binding_id} which is no longer "
                    "a WORKER-role eligible binding; failing closed without "
                    "substitution"
                ),
            )
        provider = str(getattr(binding, "provider", last.provider))
        model = str(getattr(binding, "model", last.model))
        if not self._ladder_compatible(
            ladder, provider=provider, model=model
        ):
            return SupervisorDecision(
                action=SupervisorAction.STOP,
                run_id=run_id,
                stop_reason=SupervisorStopReason.NO_ELIGIBLE_BINDING,
                detail=(
                    f"SAME_BINDING target {provider}/{model} is not on the "
                    "eligible candidate ladder; failing closed"
                ),
            )
        # No-progress budget keyed on exact binding identity (provider/model
        # fallback only when no durable identity was ever persisted).
        prior_ids = [
            self._prior_binding_id(run_id, a.id) or f"{a.provider}:{a.model}"
            for a in terminal
        ]
        same_binding_run = 0
        for prior_id in reversed(prior_ids):
            if prior_id == source_binding_id:
                same_binding_run += 1
            else:
                break
        if same_binding_run >= self.budgets.max_no_progress + 1:
            return SupervisorDecision(
                action=SupervisorAction.STOP,
                run_id=run_id,
                stop_reason=SupervisorStopReason.NO_PROGRESS,
                detail=(
                    f"{same_binding_run} consecutive terminal failures on "
                    f"exact binding {source_binding_id}"
                ),
            )
        if len(terminal) >= self.budgets.max_no_progress + 1 and not any(
            binding_id != source_binding_id for binding_id in prior_ids
        ):
            return SupervisorDecision(
                action=SupervisorAction.STOP,
                run_id=run_id,
                stop_reason=SupervisorStopReason.ATTEMPT_BUDGET_EXHAUSTED,
                detail="eligible ladder exhausted and retry budget spent",
            )
        status, tier, reason = self._resolve_binding_effort(binding)
        if status is not EffortDecisionStatus.RESOLVED or tier is None:
            return self._effort_stop(
                run_id, status=status, reason=reason, binding_id=source_binding_id
            )
        exact_ids = self._binding_identity(
            binding, provider=provider, model=model
        )
        return SupervisorDecision(
            action=SupervisorAction.LAUNCH_ATTEMPT,
            run_id=run_id,
            binding_id=exact_ids[0],
            profile_id=exact_ids[1],
            quota_pool_id=exact_ids[2],
            binding_provider=provider,
            binding_model=model,
            reasoning_effort=tier.value,
            failover_from_attempt_id=last.id,
            required_base_sha=failover.required_base_sha,
            detail=(
                f"FailoverEngine SAME_BINDING after terminal attempt "
                f"{last.id}: exact binding {source_binding_id}"
            ),
        )

    def _different_binding_decision(
        self,
        *,
        run_id: str,
        ladder: Sequence[WorkerCandidate],
        last: Attempt,
        source_binding_id: str,
        failover: Any,
    ) -> SupervisorDecision:
        """Consume a DIFFERENT_BINDING verdict with a different binding_id.

        Every worker-dispatch path -- initial attempt,
        repair/continuation, SAME_BINDING and DIFFERENT_BINDING -- must
        enforce the same invariants through the canonical exact resolver:

            exact binding exists
            exact id matches
            provider matches
            model matches
            role == WORKER

        This method therefore delegates to
        :meth:`_binding_for_candidate` (the same seam the initial-attempt
        and SAME_BINDING paths already use) so the dynamic-candidate
        path cannot accidentally widen authority by consulting
        ``_binding_by_id`` directly -- a path that returns any role
        binding and therefore allowed an ORCHESTRATOR-role binding to
        drive a worker dispatch.
        """
        if self._failover_consumed(run_id) >= self.budgets.max_failovers:
            return SupervisorDecision(
                action=SupervisorAction.STOP,
                run_id=run_id,
                stop_reason=SupervisorStopReason.FAILOVER_BUDGET_EXHAUSTED,
                detail="autonomous failover budget exhausted",
            )
        for candidate in ladder:
            candidate_binding_id = getattr(candidate, "binding_id", None)
            if candidate_binding_id:
                # Dynamic approval pinning: route through the canonical
                # exact candidate resolver so the WORKER-role filter is
                # applied uniformly.  The previous direct ``_binding_by_id``
                # lookup bypassed the role filter and could resolve an
                # ORCHESTRATOR-role binding as a worker target.
                binding = self._binding_for_candidate(
                    provider=candidate.provider,
                    model=candidate.model,
                    binding_id=candidate_binding_id,
                )
                if binding is None:
                    return SupervisorDecision(
                        action=SupervisorAction.STOP,
                        run_id=run_id,
                        stop_reason=SupervisorStopReason.NO_ELIGIBLE_BINDING,
                        detail=(
                            f"FailoverEngine DIFFERENT_BINDING after terminal "
                            f"attempt {last.id} but dynamic candidate "
                            f"{candidate.provider}/{candidate.model} does not "
                            "resolve to an exact WORKER-role binding for "
                            f"binding_id={candidate_binding_id!r}; failing "
                            "closed"
                        ),
                    )
                if getattr(binding, "binding_id", None) == source_binding_id:
                    continue
                status, tier, reason = self._resolve_binding_effort(binding)
                if status is not EffortDecisionStatus.RESOLVED or tier is None:
                    return self._effort_stop(
                        run_id,
                        status=status,
                        reason=reason,
                        binding_id=str(getattr(binding, "binding_id", None)),
                    )
                binding_id, profile_id, quota_pool_id = self._binding_identity(
                    binding,
                    provider=candidate.provider,
                    model=candidate.model,
                )
                return SupervisorDecision(
                    action=SupervisorAction.FAILOVER_NEW_ATTEMPT,
                    run_id=run_id,
                    binding_id=binding_id,
                    profile_id=profile_id,
                    quota_pool_id=quota_pool_id,
                    binding_provider=candidate.provider,
                    binding_model=candidate.model,
                    reasoning_effort=tier.value,
                    failover_from_attempt_id=last.id,
                    required_base_sha=failover.required_base_sha,
                    detail=(
                        f"FailoverEngine DIFFERENT_BINDING after terminal "
                        f"attempt {last.id}: {source_binding_id} -> "
                        f"{binding_id}"
                    ),
                )
            for binding in self._worker_eligible_bindings():
                if (
                    getattr(binding, "provider", None) != candidate.provider
                    or getattr(binding, "model", None) != candidate.model
                ):
                    continue
                if getattr(binding, "binding_id", None) == source_binding_id:
                    continue
                status, tier, reason = self._resolve_binding_effort(binding)
                if status is not EffortDecisionStatus.RESOLVED or tier is None:
                    return self._effort_stop(
                        run_id,
                        status=status,
                        reason=reason,
                        binding_id=str(getattr(binding, "binding_id", None)),
                    )
                binding_id, profile_id, quota_pool_id = self._binding_identity(
                    binding,
                    provider=candidate.provider,
                    model=candidate.model,
                )
                return SupervisorDecision(
                    action=SupervisorAction.FAILOVER_NEW_ATTEMPT,
                    run_id=run_id,
                    binding_id=binding_id,
                    profile_id=profile_id,
                    quota_pool_id=quota_pool_id,
                    binding_provider=candidate.provider,
                    binding_model=candidate.model,
                    reasoning_effort=tier.value,
                    failover_from_attempt_id=last.id,
                    required_base_sha=failover.required_base_sha,
                    detail=(
                        f"FailoverEngine DIFFERENT_BINDING after terminal "
                        f"attempt {last.id}: {source_binding_id} -> "
                        f"{binding_id}"
                    ),
                )
        # No different exact eligible WORKER-role binding exists
        # (including the no-registry case: a raw provider/model name
        # never synthesizes one).  Fail closed with zero launch.
        return SupervisorDecision(
            action=SupervisorAction.STOP,
            run_id=run_id,
            stop_reason=SupervisorStopReason.NO_ELIGIBLE_BINDING,
            detail=(
                f"FailoverEngine DIFFERENT_BINDING after terminal attempt "
                f"{last.id} but no different exact eligible binding_id "
                "exists; failing closed"
            ),
        )

    def drive(self, *args: Any, **kwargs: Any) -> Any:
        """Legacy only: never on the production path.

        Production autonomous execution runs through the canonical
        ``Orchestrator._run_core`` lifecycle consuming :meth:`decide`
        outputs.  This method exists so historical references keep
        resolving; it always raises.
        """
        raise RuntimeError(
            "AutonomousSupervisor.drive is legacy-only and never on the "
            "production path; production consumes decide() via _run_core"
        )


# ---------------------------------------------------------------------------
# Accept -> Project continuity
# ---------------------------------------------------------------------------

#: Canonical Git SHA-1 hex pattern (40 lowercase hex chars).  Used to
#: validate every SHA accepted into Project authority; uppercase,
#: truncated, or non-hex inputs are never accepted as evidence.
_CANONICAL_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")

#: Evidence marker binding an accepted SHA to its run.  The base resolver
#: parses exactly this marker form; statement prose is never parsed.
ACCEPT_EVIDENCE_PREFIX = "run:"


class AcceptanceAuthorityError(RuntimeError):
    """Raised when Project projection lacks durable :class:`AcceptEngine` evidence.

    Only an :class:`AcceptEngine` acceptance creates authoritative
    Project bases.  A missing/forged/mismatched acceptance is a
    contract violation; the projector/closure boundary must fail
    closed.  No fallback path bypasses this check.
    """


def _accept_marker(run_id: str, final_sha: str) -> str:
    return f"{ACCEPT_EVIDENCE_PREFIX}{run_id}:sha:{final_sha}"


def _parse_accept_marker(reference: str) -> tuple[str, str] | None:
    if not reference.startswith(ACCEPT_EVIDENCE_PREFIX):
        return None
    rest = reference[len(ACCEPT_EVIDENCE_PREFIX):]
    run_id, sep, tail = rest.partition(":sha:")
    if not sep or not run_id or not tail:
        return None
    return run_id, tail


def _is_canonical_sha(value: object) -> bool:
    """True iff *value* is a strict 40-char lowercase hex SHA-1 string."""
    return isinstance(value, str) and bool(_CANONICAL_SHA_PATTERN.match(value))


def _latest_run_accepted(
    db: Database, run_id: str
) -> tuple[int, dict[str, Any]] | None:
    """Locate the latest durable ``run_accepted`` event for *run_id*.

    Returns ``(event_id, payload)`` of the highest-id ``run_accepted``
    event whose ``run_id`` matches, or ``None`` if no such event exists.
    Only :class:`AcceptEngine` writes this event and it is the sole
    authoritative acceptance record.
    """
    last: tuple[int, dict[str, Any]] | None = None
    for event in db.get_events_by_type(run_id, "run_accepted"):
        payload = event.payload
        if not isinstance(payload, dict):
            continue
        last = (int(event.id), payload)
    return last


def _verify_accept_engine_evidence(
    db: Database, *, run_id: str, final_sha: str
) -> dict[str, Any]:
    """Verify a durable :class:`AcceptEngine` record exists for ``(run_id, final_sha)``.

    Returns the verified acceptance event payload on success.  On any
    identity mismatch, canonical-SHA violation, missing run, missing
    acceptance event, ambiguous payload, or provenance contradiction,
    raises :class:`AcceptanceAuthorityError`.  This is the single
    boundary that authorises Project projection; callers cannot
    bypass it.
    """
    if not isinstance(run_id, str) or not run_id.strip():
        raise AcceptanceAuthorityError(
            "Project projection refused: run_id is empty or non-string."
        )
    if not _is_canonical_sha(final_sha):
        raise AcceptanceAuthorityError(
            "Project projection refused: final_sha is not a canonical "
            "40-lowercase-hex Git SHA-1."
        )
    run = db.get_run(run_id)
    if run is None:
        raise AcceptanceAuthorityError(
            f"Project projection refused: run '{run_id}' does not exist."
        )
    if run.id != run_id:
        raise AcceptanceAuthorityError(
            f"Project projection refused: run identity mismatch for "
            f"'{run_id}' (run row id is '{run.id}')."
        )
    accepted = _latest_run_accepted(db, run_id)
    if accepted is None:
        raise AcceptanceAuthorityError(
            f"Project projection refused: no durable AcceptEngine "
            f"'run_accepted' event exists for run '{run_id}'."
        )
    event_id, payload = accepted
    candidate_sha = payload.get("candidate_sha")
    if not _is_canonical_sha(candidate_sha):
        raise AcceptanceAuthorityError(
            f"Project projection refused: 'run_accepted' event {event_id} "
            f"for run '{run_id}' has a non-canonical candidate_sha."
        )
    if candidate_sha != final_sha:
        raise AcceptanceAuthorityError(
            f"Project projection refused: 'run_accepted' event {event_id} "
            f"for run '{run_id}' binds candidate_sha={candidate_sha}, "
            f"not the requested final_sha."
        )
    provenance = payload.get("provenance")
    if not isinstance(provenance, dict) or not provenance:
        raise AcceptanceAuthorityError(
            f"Project projection refused: 'run_accepted' event {event_id} "
            f"for run '{run_id}' lacks unambiguous provenance."
        )
    if provenance.get("candidate_sha") != candidate_sha:
        raise AcceptanceAuthorityError(
            f"Project projection refused: 'run_accepted' event {event_id} "
            f"for run '{run_id}' has inconsistent provenance.candidate_sha."
        )
    return payload

