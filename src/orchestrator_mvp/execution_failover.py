"""Attempt-level failover + effect reconciliation.

C11-C widens the canonical C05 lifecycle so that an Attempt boundary is the
*only* place the runtime may hand off execution from one provider / backend
to another.  A failover from ``Attempt A`` to ``Attempt B`` is therefore a
purely deterministic decision: classify A's failure, reconcile A's tool
effects, validate the new starting state, and decide whether B may begin.

Three architectural rules govern this module:

* * **Failover boundary = Attempt.**  A run may not continue mid-Attempt
  across harnesses; Attempt A terminates, its evidence is preserved, and
  Attempt B begins independently.  No raw transcript / conversation is
  ever handed across.
* * **No blind replay of uncertain effects.**  ``EXTERNAL_UNCERTAIN`` and
  ``EXTERNAL_IDEMPOTENT`` effects that have not been verified must block
  the new Attempt.  Reconciliation is an obligation, not an option.
* * **Project continuity wins over Attempt continuity.**  Reconstruction
  authority is the Project Ledger / checkpoint, not this module.  Failover
  consumes Project evidence to start the next Attempt; it does not
  *create* Project evidence.

This module contains no provider, model, or live network call.  The
failover decision is a pure function of the database state plus the typed
:class:`FailureKind` that the supervisor / recorder handed it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from orchestrator_mvp.db import Database, current_iso_timestamp
from orchestrator_mvp.models import (
    Attempt,
    AttemptStatus,
    AttemptTrace,
    EffectStatus,
    FailureKind,
    ReconciliationStatus,
    SideEffectRecord,
    ToolEffect,
)
from orchestrator_mvp.observability.trace import is_autonomous_eligible
from orchestrator_mvp.sandbox import SideEffectSemantics

# ---------------------------------------------------------------------------
# Decision types.
# ---------------------------------------------------------------------------


class FailoverAction(StrEnum):
    """The bounded set of decisions the engine can emit."""

    NEW_ATTEMPT_SAME_BINDING = "NEW_ATTEMPT_SAME_BINDING"
    NEW_ATTEMPT_DIFFERENT_BINDING = "NEW_ATTEMPT_DIFFERENT_BINDING"
    BLOCKED_ESCALATE = "BLOCKED_ESCALATE"
    NOOP_TERMINAL = "NOOP_TERMINAL"


class FailoverReasonCode(StrEnum):
    """Typed reason for the decision -- never a free-form string."""

    NO_OPERATION = "NO_OPERATION"
    RECONCILED_NO_EFFECT = "RECONCILED_NO_EFFECT"
    RECONCILED_LOCAL_REPLAY_SAFE = "RECONCILED_LOCAL_REPLAY_SAFE"
    RECONCILED_EXTERNAL_IDEMPOTENT_RECEIPT = "RECONCILED_EXTERNAL_IDEMPOTENT_RECEIPT"
    BLOCKED_EXTERNAL_UNCERTAIN = "BLOCKED_EXTERNAL_UNCERTAIN"
    BLOCKED_EXTERNAL_IDEMPOTENT_NO_RECEIPT = "BLOCKED_EXTERNAL_IDEMPOTENT_NO_RECEIPT"
    BLOCKED_SANDBOX_VIOLATION = "BLOCKED_SANDBOX_VIOLATION"
    BLOCKED_TOOL_AUTH_DENIED = "BLOCKED_TOOL_AUTH_DENIED"
    BLOCKED_INVALID_BASE_OR_CHECKPOINT = "BLOCKED_INVALID_BASE_OR_CHECKPOINT"
    BLOCKED_SEMANTIC_RESULT_INVALID = "BLOCKED_SEMANTIC_RESULT_INVALID"
    BLOCKED_TRACE_UNAVAILABLE = "BLOCKED_TRACE_UNAVAILABLE"
    BLOCKED_CANCELLATION = "BLOCKED_CANCELLATION"
    BINDING_UNAVAILABLE_TRY_DIFFERENT = "BINDING_UNAVAILABLE_TRY_DIFFERENT"
    BINDING_AVAILABLE_TRY_SAME = "BINDING_AVAILABLE_TRY_SAME"


@dataclass(frozen=True)
class AttemptFailoverDecision:
    """The deterministic typed decision for what should happen after Attempt A.

    ``action`` is the bounded enum value; ``reason_code`` is the typed
    reason; ``reason`` is a bounded human-readable explanation.
    ``source_attempt_id`` pins the terminal Attempt A this decision was
    derived from; Attempt A itself is never mutated or reopened.

    Attempt B contract (consumed by the C11-D supervisor loop, which
    owns ``new_attempt_id`` assignment and Attempt B creation -- this
    engine only emits a decision and never creates B):

    * ``required_binding_behavior`` -- ``"SAME_BINDING"``,
      ``"DIFFERENT_BINDING"``, or ``None`` when no new Attempt may
      start;
    * ``reconciled_effect_ids`` / ``reconciliation_summary`` -- the
      reconciled effects the next Attempt must respect;
    * ``failure_kind`` -- the typed classification of A's failure;
    * ``required_base_sha`` + ``requires_base_validation`` -- the base
      reference C11-D must validate (checkpoint / ledger ancestry)
      before creating B.

    The decision carries no raw transcript, no partial assistant
    response, and no hidden provider session state -- by construction
    there is no field for any of them.
    """

    action: FailoverAction
    reason_code: FailoverReasonCode
    reason: str
    source_attempt_id: str
    new_attempt_id: str | None
    reconciled_effect_ids: tuple[str, ...] = ()
    reconciliation_summary: dict[str, int] = field(default_factory=dict)
    failure_kind: FailureKind | None = None
    required_binding_behavior: str | None = None
    required_base_sha: str | None = None
    requires_base_validation: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "reason_code": self.reason_code.value,
            "reason": self.reason,
            "source_attempt_id": self.source_attempt_id,
            "new_attempt_id": self.new_attempt_id,
            "reconciled_effect_ids": list(self.reconciled_effect_ids),
            "reconciliation_summary": dict(self.reconciliation_summary),
            "failure_kind": self.failure_kind.value if self.failure_kind is not None else None,
            "required_binding_behavior": self.required_binding_behavior,
            "required_base_sha": self.required_base_sha,
            "requires_base_validation": self.requires_base_validation,
        }


# ---------------------------------------------------------------------------
# Effect reconciler.
# ---------------------------------------------------------------------------


_RECONCILIATION_MAP: dict[SideEffectSemantics, ReconciliationStatus] = {
    SideEffectSemantics.READ_ONLY: ReconciliationStatus.NO_EFFECT,
    SideEffectSemantics.LOCAL_REPLAY_SAFE: ReconciliationStatus.LOCAL_REPLAY_SAFE,
    SideEffectSemantics.EXTERNAL_IDEMPOTENT: ReconciliationStatus.EXTERNAL_IDEMPOTENT_RECEIPT,
    SideEffectSemantics.EXTERNAL_UNCERTAIN: ReconciliationStatus.UNCERTAIN,
}


@dataclass
class EffectReconciler:
    """The deterministic reconciler for the effects of one Attempt.

    The reconciler classifies every recorded tool effect against the four
    side-effect semantics and the side-effect journal.  It then reduces the
    per-effect decisions to a single :class:`ReconciliationOutcome` the
    failover engine consumes.

    The reconciler is *additive*: it persists reconciled evidence into the
    existing ``tool_effects`` rows; it does not introduce a parallel journal.
    """

    db: Database

    def reconcile(
        self,
        attempt_id: str,
        *,
        now: str | None = None,
    ) -> ReconciliationOutcome:
        """Reconcile every recorded effect for ``attempt_id``.

        Returns a :class:`ReconciliationOutcome` that summarises the
        effects' statuses, the side-effect-journal receipts (if any), and
        the bounded decision the failover engine should take.
        """
        timestamp = now or current_iso_timestamp()
        effects = list(self.db.get_tool_effects_for_attempt(attempt_id))
        journal_records = self._journal_records_for_attempt(attempt_id)
        outcome = ReconciliationOutcome(
            attempt_id=attempt_id,
            effects=tuple(effects),
            now=timestamp,
        )
        # Apply journal-derived receipt verification first; ``with_journal``
        # produces an updated effects tuple which ``classify_effect``
        # then refines.
        outcome = outcome.with_journal(journal_records)
        classified = tuple(outcome.classify_effect(effect) for effect in outcome.effects)
        for updated in classified:
            self.db.update_tool_effect(updated)
        return ReconciliationOutcome(
            attempt_id=attempt_id,
            effects=classified,
            now=timestamp,
        ).reduced()

    def _journal_records_for_attempt(self, attempt_id: str) -> list[SideEffectRecord]:
        return list(self.db.get_side_effects_for_attempt(attempt_id))


@dataclass(frozen=True)
class ReconciliationOutcome:
    """The result of reconciling one Attempt's tool effects.

    ``effects`` carries every recorded effect row (after classification);
    ``decision`` is the bounded action the next Attempt may take.

    The decision is reduced from the *worst* per-effect outcome:

    * any ``UNCERTAIN`` -> ``BLOCKED``;
    * any ``EXTERNAL_IDEMPOTENT_RECEIPT`` without verified receipt -> blocked;
    * only ``LOCAL_REPLAY_SAFE`` / ``NO_EFFECT`` -> proceed.
    """

    attempt_id: str
    effects: tuple[ToolEffect, ...] = ()
    now: str = field(default_factory=current_iso_timestamp)
    decision: str = "PROCEED"
    counts: dict[str, int] = field(default_factory=dict)

    def with_journal(
        self,
        records: Sequence[SideEffectRecord],
    ) -> ReconciliationOutcome:
        """Return a copy with ``journal_records`` factored into the decision.

        ``EXTERNAL_IDEMPOTENT`` effects whose journal record is missing or
        in a non-``COMPLETED`` state are demoted to ``UNCERTAIN``; the
        runtime may not assume the receipt exists without verifying it.
        """
        receipt_keys = {
            record.idempotency_key: record.state for record in records
        }
        updated: list[ToolEffect] = []
        for effect in self.effects:
            if effect.side_effect_class != SideEffectSemantics.EXTERNAL_IDEMPOTENT.value:
                updated.append(effect)
                continue
            key = effect.idempotency_key
            if key is None:
                updated.append(effect)
                continue
            state = receipt_keys.get(key)
            if state is None:
                # No receipt found; treat as uncertain.
                updated.append(
                    _replace_effect(
                        effect,
                        reconciliation_status=ReconciliationStatus.UNCERTAIN.value,
                    )
                )
                continue
            try:
                state_enum = state
            except AttributeError:
                state_enum = state
            from orchestrator_mvp.models import SideEffectState

            try:
                state_value = state_enum.value if hasattr(state_enum, "value") else str(state_enum)
            except Exception:
                state_value = str(state_enum)
            if state_value == SideEffectState.COMPLETED.value:
                updated.append(
                    _replace_effect(
                        effect,
                        reconciliation_status=ReconciliationStatus.EXTERNAL_IDEMPOTENT_RECEIPT.value,
                    )
                )
            else:
                updated.append(
                    _replace_effect(
                        effect,
                        reconciliation_status=ReconciliationStatus.UNCERTAIN.value,
                    )
                )
        return ReconciliationOutcome(
            attempt_id=self.attempt_id,
            effects=tuple(updated),
            now=self.now,
            decision=self.decision,
            counts=self.counts,
        )

    def classify_effect(self, effect: ToolEffect) -> ToolEffect:
        """Return an updated effect row reflecting the typed classification.

        The mapping is purely deterministic:

        * ``READ_ONLY``           -> ``NO_EFFECT`` (a retry is always safe)
        * ``LOCAL_REPLAY_SAFE``   -> ``LOCAL_REPLAY_SAFE``
        * ``EXTERNAL_IDEMPOTENT`` with a verified receipt -> same
          ``EXTERNAL_IDEMPOTENT_RECEIPT``
        * ``EXTERNAL_IDEMPOTENT`` without a receipt -> ``UNCERTAIN``
          (the runtime cannot prove the effect was actually realised)
        * ``EXTERNAL_UNCERTAIN``  -> ``UNCERTAIN``

        The reconciliation status is the answer the failover engine reads; it
        is also persisted in the durable row so a later supervisor can
        reconstruct the exact reason the failover decision was made.

        An effect that has already been demoted to ``UNCERTAIN`` by the
        journal-receipt verifier (``with_journal``) is never silently
        upgraded back to ``EXTERNAL_IDEMPOTENT_RECEIPT``; ``UNCERTAIN`` is
        sticky once it appears in the durable evidence.
        """
        if effect.reconciliation_status == ReconciliationStatus.UNCERTAIN.value:
            return _replace_effect(effect, updated_at=self.now)
        try:
            side_effect = SideEffectSemantics(effect.side_effect_class)
        except ValueError:
            side_effect = SideEffectSemantics.EXTERNAL_UNCERTAIN
        status = _RECONCILIATION_MAP[side_effect]
        if side_effect is SideEffectSemantics.EXTERNAL_IDEMPOTENT:
            if effect.idempotency_key is None:
                status = ReconciliationStatus.UNCERTAIN
            elif effect.status is not EffectStatus.COMPLETED:
                status = ReconciliationStatus.UNCERTAIN
        return _replace_effect(effect, reconciliation_status=status.value, updated_at=self.now)

    def reduced(self) -> ReconciliationOutcome:
        """Return a new outcome with ``decision`` and ``counts`` populated."""
        counts: dict[str, int] = {}
        for effect in self.effects:
            key = effect.reconciliation_status
            counts[key] = counts.get(key, 0) + 1
        decision = "PROCEED"
        if counts.get(ReconciliationStatus.UNCERTAIN.value, 0) > 0:
            decision = "BLOCKED"
        elif counts.get(ReconciliationStatus.EXTERNAL_IDEMPOTENT_RECEIPT.value, 0) > 0:
            decision = "PROCEED_IDEMPOTENT"
        return ReconciliationOutcome(
            attempt_id=self.attempt_id,
            effects=self.effects,
            now=self.now,
            decision=decision,
            counts=counts,
        )


def _replace_effect(
    effect: ToolEffect,
    *,
    reconciliation_status: str | None = None,
    updated_at: str | None = None,
) -> ToolEffect:
    """Return a copy of ``effect`` with optional replacement fields."""
    return ToolEffect(
        id=effect.id,
        run_id=effect.run_id,
        attempt_id=effect.attempt_id,
        operation=effect.operation,
        capability=effect.capability,
        resource_ref=effect.resource_ref,
        side_effect_class=effect.side_effect_class,
        status=effect.status,
        idempotency_key=effect.idempotency_key,
        started_at=effect.started_at,
        completed_at=effect.completed_at,
        result_class=effect.result_class,
        result_redacted=effect.result_redacted,
        reconciliation_status=reconciliation_status or effect.reconciliation_status,
        error=effect.error,
        created_at=effect.created_at,
        updated_at=updated_at or effect.updated_at,
    )


# ---------------------------------------------------------------------------
# Failover engine.
# ---------------------------------------------------------------------------


_BLOCKING_FAILURE_KINDS = frozenset(
    {
        FailureKind.SANDBOX_VIOLATION,
        FailureKind.TOOL_AUTH_DENIED,
        FailureKind.INVALID_BASE_OR_CHECKPOINT,
        FailureKind.SEMANTIC_RESULT_INVALID,
        FailureKind.TRACE_UNAVAILABLE,
        FailureKind.CANCELLATION,
    }
)


_BINDING_FAILURE_KINDS = frozenset(
    {
        FailureKind.PROVIDER_UNAVAILABLE,
        FailureKind.TRANSPORT_FAILURE,
        FailureKind.TIMEOUT,
        FailureKind.QUOTA_UNAVAILABLE,
    }
)


@dataclass
class FailoverEngine:
    """Deterministic decision engine for the post-Attempt boundary.

    The engine is constructed once per run; it shares a single
    :class:`Database` with the supervisor.  Its decisions are pure
    functions of the supplied typed inputs and the persisted DB state.
    """

    db: Database

    def evaluate(
        self,
        *,
        attempt: Attempt,
        trace: AttemptTrace | None,
        failure_kind: FailureKind | None = None,
        now: str | None = None,
    ) -> AttemptFailoverDecision:
        """Decide what should happen after ``attempt`` ends.

        Semantic distinction (Attempt state is monotonic and immutable):

        * ``SUCCESS`` is terminal success -> ``NOOP_TERMINAL``.  Nothing
          follows a successful Attempt.
        * ``FAILED`` / ``TIMEOUT`` are terminal *as Attempts* -- Attempt
          A is never mutated or reopened -- BUT post-Attempt failover
          evaluation still decides whether a NEW Attempt B may start.
          The flow is: final trace exists -> effects reconciled ->
          failure kind classified -> deterministic decision; the
          supervisor may then create fresh Attempt B.
        * ``RUNNING`` / ``PENDING`` evaluate the same reconciliation
          rules for mid-flight inspection.

        The decision is deterministic and matches the structured rules:

        * an explicit blocking failure -> ``BLOCKED_ESCALATE``;
        * a binding / quota / transport failure -> ``NEW_ATTEMPT_DIFFERENT_BINDING``;
        * a normal termination -> ``NEW_ATTEMPT_SAME_BINDING`` when eligible;
        * terminal success -> ``NOOP_TERMINAL``;
        * ``trace`` missing or ``NONE`` -> blocked.  ``trace=None``
          never silently means "eligible": autonomous execution
          requires explicit trace evidence.  A non-``NONE`` trace
          that was never finalized (``completed_at is None``) is
          partial evidence -- the recorder persists the trace row
          before its tool-effect rows and before finalise() -- and
          likewise blocks: only durable + finalized evidence may
          authorize a new Attempt.
        """
        timestamp = now or current_iso_timestamp()
        attempt_id = attempt.id
        if attempt.status is AttemptStatus.SUCCESS:
            return AttemptFailoverDecision(
                action=FailoverAction.NOOP_TERMINAL,
                reason_code=FailoverReasonCode.NO_OPERATION,
                reason=f"attempt {attempt_id} succeeded ({attempt.status.value})",
                source_attempt_id=attempt_id,
                new_attempt_id=None,
                failure_kind=failure_kind,
                required_binding_behavior=None,
                required_base_sha=attempt.base_sha,
                requires_base_validation=False,
            )

        # One effective failure kind, derived once and used for routing,
        # reason, and the decision payload alike: explicit
        # ``failure_kind``, else the persisted ``trace.failure_kind``
        # when it parses to a valid :class:`FailureKind`, else
        # ``TIMEOUT`` when the attempt itself timed out, else None
        # (genuinely unknown -- never a fabricated specific kind).
        effective_kind = _effective_failure_kind(
            failure_kind=failure_kind,
            trace=trace,
            attempt_status=attempt.status,
        )

        reconciler = EffectReconciler(db=self.db)
        reconciliation = reconciler.reconcile(attempt_id, now=timestamp)

        if trace is None or not is_autonomous_eligible(trace.trace_class):
            observed = trace.trace_class.value if trace is not None else "missing"
            return AttemptFailoverDecision(
                action=FailoverAction.BLOCKED_ESCALATE,
                reason_code=FailoverReasonCode.BLOCKED_TRACE_UNAVAILABLE,
                reason=(
                    f"trace capability is {observed}; "
                    "autonomous execution ineligible"
                ),
                source_attempt_id=attempt_id,
                new_attempt_id=None,
                reconciled_effect_ids=tuple(effect.id for effect in reconciliation.effects),
                reconciliation_summary=dict(reconciliation.counts),
                failure_kind=effective_kind,
                required_binding_behavior=None,
                required_base_sha=attempt.base_sha,
                requires_base_validation=False,
            )

        # Failover-eligible trace = durable + finalized + internally
        # complete.  The recorder persists the trace row before its
        # tool-effect rows and before finalise(), so a non-NONE trace
        # with ``completed_at is None`` is partial evidence (a later
        # persistence step failed) and must NEVER authorize a new
        # Attempt -- however capable its trace class looks.
        if trace.completed_at is None:
            return AttemptFailoverDecision(
                action=FailoverAction.BLOCKED_ESCALATE,
                reason_code=FailoverReasonCode.BLOCKED_TRACE_UNAVAILABLE,
                reason=(
                    f"attempt {attempt_id} trace is not finalized "
                    "(completed_at is None): partial evidence must not "
                    "authorize a new Attempt"
                ),
                source_attempt_id=attempt_id,
                new_attempt_id=None,
                reconciled_effect_ids=tuple(effect.id for effect in reconciliation.effects),
                reconciliation_summary=dict(reconciliation.counts),
                failure_kind=effective_kind,
                required_binding_behavior=None,
                required_base_sha=attempt.base_sha,
                requires_base_validation=False,
            )

        if effective_kind in _BLOCKING_FAILURE_KINDS:
            return _blocking_decision(
                attempt_id=attempt_id,
                failure_kind=effective_kind,
                reconciliation=reconciliation,
                required_base_sha=attempt.base_sha,
            )

        if effective_kind is FailureKind.EXTERNAL_EFFECT_UNCERTAIN:
            return AttemptFailoverDecision(
                action=FailoverAction.BLOCKED_ESCALATE,
                reason_code=FailoverReasonCode.BLOCKED_EXTERNAL_UNCERTAIN,
                reason=(
                    "failure kind EXTERNAL_EFFECT_UNCERTAIN for attempt "
                    f"{attempt_id}: uncertain external effects must not be replayed"
                ),
                source_attempt_id=attempt_id,
                new_attempt_id=None,
                reconciled_effect_ids=tuple(effect.id for effect in reconciliation.effects),
                reconciliation_summary=dict(reconciliation.counts),
                failure_kind=effective_kind,
                required_binding_behavior=None,
                required_base_sha=attempt.base_sha,
                requires_base_validation=False,
            )

        if reconciliation.decision == "BLOCKED":
            return AttemptFailoverDecision(
                action=FailoverAction.BLOCKED_ESCALATE,
                reason_code=FailoverReasonCode.BLOCKED_EXTERNAL_UNCERTAIN,
                reason=(
                    "tool-effect reconciliation left at least one "
                    f"UNCERTAIN effect for attempt {attempt_id}"
                ),
                source_attempt_id=attempt_id,
                new_attempt_id=None,
                reconciled_effect_ids=tuple(
                    effect.id
                    for effect in reconciliation.effects
                    if effect.reconciliation_status
                    == ReconciliationStatus.UNCERTAIN.value
                ),
                reconciliation_summary=dict(reconciliation.counts),
                failure_kind=effective_kind,
                required_binding_behavior=None,
                required_base_sha=attempt.base_sha,
                requires_base_validation=False,
            )

        if effective_kind in _BINDING_FAILURE_KINDS:
            return AttemptFailoverDecision(
                action=FailoverAction.NEW_ATTEMPT_DIFFERENT_BINDING,
                reason_code=FailoverReasonCode.BINDING_UNAVAILABLE_TRY_DIFFERENT,
                reason=(
                    f"binding / transport failure ({effective_kind.value}); "
                    "next Attempt must use a different binding"
                ),
                source_attempt_id=attempt_id,
                new_attempt_id=None,
                reconciled_effect_ids=tuple(effect.id for effect in reconciliation.effects),
                reconciliation_summary=dict(reconciliation.counts),
                failure_kind=effective_kind,
                required_binding_behavior="DIFFERENT_BINDING",
                required_base_sha=attempt.base_sha,
                requires_base_validation=True,
            )

        return AttemptFailoverDecision(
            action=FailoverAction.NEW_ATTEMPT_SAME_BINDING,
            reason_code=FailoverReasonCode.RECONCILED_NO_EFFECT,
            reason=(
                f"Attempt {attempt_id} ended with no blocking failure; "
                "next Attempt may use the same binding"
            ),
            source_attempt_id=attempt_id,
            new_attempt_id=None,
            reconciled_effect_ids=tuple(effect.id for effect in reconciliation.effects),
            reconciliation_summary=dict(reconciliation.counts),
            failure_kind=effective_kind,
            required_binding_behavior="SAME_BINDING",
            required_base_sha=attempt.base_sha,
            requires_base_validation=True,
        )


def _effective_failure_kind(
    *,
    failure_kind: FailureKind | None,
    trace: AttemptTrace | None,
    attempt_status: AttemptStatus,
) -> FailureKind | None:
    """Normalise one effective failure kind for routing, reason and payload.

    Precedence: the explicit ``failure_kind`` handed to the engine,
    else the persisted ``trace.failure_kind`` when it parses to a
    valid :class:`FailureKind`, else ``TIMEOUT`` when the attempt
    itself carries :attr:`AttemptStatus.TIMEOUT`, else ``None`` for
    genuinely-unknown (never a fabricated specific kind).
    """
    if failure_kind is not None:
        return failure_kind
    if trace is not None and trace.failure_kind:
        try:
            return FailureKind(trace.failure_kind)
        except ValueError:
            pass
    if attempt_status is AttemptStatus.TIMEOUT:
        return FailureKind.TIMEOUT
    return None


def _blocking_decision(
    *,
    attempt_id: str,
    failure_kind: FailureKind | None,
    reconciliation: ReconciliationOutcome,
    required_base_sha: str | None = None,
) -> AttemptFailoverDecision:
    code = FailoverReasonCode.BLOCKED_SEMANTIC_RESULT_INVALID
    if failure_kind is FailureKind.SANDBOX_VIOLATION:
        code = FailoverReasonCode.BLOCKED_SANDBOX_VIOLATION
    elif failure_kind is FailureKind.TOOL_AUTH_DENIED:
        code = FailoverReasonCode.BLOCKED_TOOL_AUTH_DENIED
    elif failure_kind is FailureKind.INVALID_BASE_OR_CHECKPOINT:
        code = FailoverReasonCode.BLOCKED_INVALID_BASE_OR_CHECKPOINT
    elif failure_kind is FailureKind.TRACE_UNAVAILABLE:
        code = FailoverReasonCode.BLOCKED_TRACE_UNAVAILABLE
    elif failure_kind is FailureKind.CANCELLATION:
        code = FailoverReasonCode.BLOCKED_CANCELLATION
    return AttemptFailoverDecision(
        action=FailoverAction.BLOCKED_ESCALATE,
        reason_code=code,
        reason=f"blocking failure kind {failure_kind.value if failure_kind else 'UNKNOWN'}",
        source_attempt_id=attempt_id,
        new_attempt_id=None,
        reconciled_effect_ids=tuple(effect.id for effect in reconciliation.effects),
        reconciliation_summary=dict(reconciliation.counts),
        failure_kind=failure_kind,
        required_binding_behavior=None,
        required_base_sha=required_base_sha,
        requires_base_validation=False,
    )


def _attempt_trace_failure_kind(trace: AttemptTrace) -> FailureKind | None:
    if trace.failure_kind is None:
        return None
    try:
        return FailureKind(trace.failure_kind)
    except ValueError:
        return None


AttemptTrace.failure_kind_as_failure_kind = _attempt_trace_failure_kind  # type: ignore[attr-defined]


__all__ = [
    "AttemptFailoverDecision",
    "EffectReconciler",
    "FailoverAction",
    "FailoverEngine",
    "FailoverReasonCode",
    "ReconciliationOutcome",
]
