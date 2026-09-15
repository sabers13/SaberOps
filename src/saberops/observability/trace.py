"""C11-C structured execution trace + tool-effect evidence recorder.

The runtime must be able to answer the supervisor (C11-D) questions:

* Did the Attempt produce a structured execution trace?
* What :class:`TraceCapability` class was established *before* semantic
  execution?  ``NONE`` is an autonomous-eligibility fail-closed.
* Which tool operations / effects were observed against the attempt, and
  what is the typed :class:`ReconciliationStatus` for each one?
* Can the next Attempt safely replay any of those effects, or must it
  reconcile first?

This module contains no provider, model, or orchestration logic.  It is
purely the recorder that turns runtime observations into durable evidence.
The recorder never invents trace data: when no real native event is
available, it answers ``SANDBOX_INTERCEPTED`` only if the sandbox actually
intercepted.  It never falls back to terminal-text scraping when a structured
event is available, and it never lets secret values enter the row.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from saberops.db import Database, current_iso_timestamp
from saberops.models import (
    AttemptTrace,
    EffectStatus,
    FailureKind,
    ReconciliationStatus,
    ToolEffect,
    TraceCapability,
)
from saberops.sandbox import (
    CapabilityOperation,
    SideEffectSemantics,
)

_SECRET_VALUE_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"ghp_[A-Za-z0-9]{8,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{8,}"),
    re.compile(r"AIza[0-9A-Za-z_-]{8,}"),
    re.compile(r"xai-[A-Za-z0-9_-]{8,}"),
)


def _redact_secret_payload(value: str | None) -> str | None:
    """Return ``value`` with obvious API-key shapes replaced by ``***``.

    Bounded mitigation only -- the recorder never relies on regex for
    secret hygiene.  Real secrets must be excluded at the source by using
    ``credential_ref`` references and resolving values only at the
    narrowest execution boundary.
    """
    if value is None:
        return None
    redacted = value
    for pattern in _SECRET_VALUE_PATTERNS:
        redacted = pattern.sub("***", redacted)
    return redacted


def _bounded(value: str | None, *, limit: int) -> str | None:
    """Return ``value`` truncated to ``limit`` bytes; ``None`` passes through."""
    if value is None:
        return None
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    return encoded[:limit].decode("utf-8", errors="replace")


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _summary_digest(payload: dict[str, Any]) -> str:
    canonical = _canonical_json(payload)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Failover evidence: bounded exception -> typed FailureKind mapping.
# ---------------------------------------------------------------------------


FAILURE_KIND_MAP: dict[str, FailureKind] = {
    "ProviderUnavailableError": FailureKind.PROVIDER_UNAVAILABLE,
    "TransportError": FailureKind.TRANSPORT_FAILURE,
    "TimeoutError": FailureKind.TIMEOUT,
    "TimeoutExpired": FailureKind.TIMEOUT,
    "QuotaExhausted": FailureKind.QUOTA_UNAVAILABLE,
    "QuotaUnavailable": FailureKind.QUOTA_UNAVAILABLE,
    "ToolAuthorizationDenied": FailureKind.TOOL_AUTH_DENIED,
    "ToolInvocationBlocked": FailureKind.TOOL_AUTH_DENIED,
    "SandboxViolation": FailureKind.SANDBOX_VIOLATION,
    "SandboxContractError": FailureKind.SANDBOX_VIOLATION,
    "TraceUnavailable": FailureKind.TRACE_UNAVAILABLE,
    "EffectUncertain": FailureKind.EXTERNAL_EFFECT_UNCERTAIN,
    "InvalidBaseOrCheckpoint": FailureKind.INVALID_BASE_OR_CHECKPOINT,
    "InvalidBaseCheckpoint": FailureKind.INVALID_BASE_OR_CHECKPOINT,
    "SemanticResultInvalid": FailureKind.SEMANTIC_RESULT_INVALID,
    "CancelledError": FailureKind.CANCELLATION,
    "KeyboardInterrupt": FailureKind.CANCELLATION,
}


def classify_failure(exc: BaseException | str) -> FailureKind:
    """Return the typed :class:`FailureKind` for a bounded failure shape.

    The mapping is intentionally small; an unmapped exception collapses to
    :attr:`FailureKind.UNKNOWN` rather than a misleading specific class.
    The supervisor (C11-D) reasons over these values; it never collapses
    failures into ``retry=True/False`` upstream of this classifier.
    """
    if isinstance(exc, str):
        return FAILURE_KIND_MAP.get(exc, FailureKind.UNKNOWN)
    name = type(exc).__name__
    return FAILURE_KIND_MAP.get(name, FailureKind.UNKNOWN)


# ---------------------------------------------------------------------------
# AttemptTraceRecorder.
# ---------------------------------------------------------------------------


_REDACT_RESULT_LIMIT = 4096
_SUMMARY_LIMIT = 4096


@dataclass
class AttemptTraceRecorder:
    """Recorder that turns runtime observations into durable evidence.

    A recorder is created once per Attempt.  It captures exactly one
    :class:`AttemptTrace` row plus N :class:`ToolEffect` rows.  The
    recorder never contacts the filesystem; it persists through the
    supplied :class:`Database` only.

    Native structured events from a backend are accepted as-is.  The
    recorder never re-parses terminal-rendering text into events; if a
    backend does not produce structured events, the recorder must not
    invent them.
    """

    db: Database
    attempt_id: str
    run_id: str
    trace_class: TraceCapability
    source_kind: str
    trace_id: str = field(default_factory=lambda: f"trace_{uuid.uuid4().hex}")
    started_at: str = field(default_factory=current_iso_timestamp)
    updated_at: str = field(default_factory=current_iso_timestamp)
    completed_at: str | None = None
    failure_kind: FailureKind | None = None
    failure_reason: str | None = None
    _effects: dict[str, ToolEffect] = field(default_factory=dict)
    _summary: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._persist_trace()

    # -- public recording API ---------------------------------------------

    def record_tool_effect(
        self,
        *,
        operation: str,
        capability: str | None,
        resource_ref: str | None,
        side_effect_class: SideEffectSemantics,
        status: EffectStatus,
        idempotency_key: str | None = None,
        result_class: str | None = None,
        result_redacted: str | None = None,
        reconciliation_status: ReconciliationStatus = ReconciliationStatus.UNKNOWN,
        error: str | None = None,
        started_at: str | None = None,
        completed_at: str | None = None,
        effect_id: str | None = None,
    ) -> ToolEffect:
        """Record one tool effect for this Attempt.

        The recorder enforces that no secret value ever reaches the
        row.  ``result_redacted`` is bounded and pattern-stripped before
        persistence; callers should still avoid passing secret-shaped
        values.  Repeated calls with the same ``effect_id`` overwrite the
        previous row in place so the lifecycle STARTED -> COMPLETED
        transition keeps a single id.
        """
        effect = ToolEffect(
            id=effect_id or f"effect_{uuid.uuid4().hex}",
            run_id=self.run_id,
            attempt_id=self.attempt_id,
            operation=operation,
            capability=capability,
            resource_ref=resource_ref,
            side_effect_class=side_effect_class.value,
            status=status,
            idempotency_key=idempotency_key,
            started_at=started_at or current_iso_timestamp(),
            completed_at=completed_at,
            result_class=result_class,
            result_redacted=_bounded(
                _redact_secret_payload(result_redacted), limit=_REDACT_RESULT_LIMIT
            ),
            reconciliation_status=reconciliation_status.value,
            error=_bounded(error, limit=_REDACT_RESULT_LIMIT),
            created_at=current_iso_timestamp(),
            updated_at=current_iso_timestamp(),
        )
        self.db.create_tool_effect(effect)
        self._effects[effect.id] = effect
        self._bump_summary()
        return effect

    def update_tool_effect(
        self,
        effect_id: str,
        *,
        status: EffectStatus | None = None,
        result_class: str | None = None,
        result_redacted: str | None = None,
        reconciliation_status: ReconciliationStatus | None = None,
        error: str | None = None,
        completed_at: str | None = None,
    ) -> ToolEffect:
        """Update an existing tool effect row in place."""
        existing = self._effects.get(effect_id)
        if existing is None:
            existing = self.db.get_tool_effect(effect_id)
            if existing is None:
                raise ValueError(f"unknown tool effect: {effect_id}")
        updated = ToolEffect(
            id=existing.id,
            run_id=existing.run_id,
            attempt_id=existing.attempt_id,
            operation=existing.operation,
            capability=existing.capability,
            resource_ref=existing.resource_ref,
            side_effect_class=existing.side_effect_class,
            status=status or existing.status,
            idempotency_key=existing.idempotency_key,
            started_at=existing.started_at,
            completed_at=completed_at or existing.completed_at,
            result_class=result_class or existing.result_class,
            result_redacted=(
                _bounded(
                    _redact_secret_payload(result_redacted),
                    limit=_REDACT_RESULT_LIMIT,
                )
                if result_redacted is not None
                else existing.result_redacted
            ),
            reconciliation_status=(
                reconciliation_status.value
                if reconciliation_status is not None
                else existing.reconciliation_status
            ),
            error=(
                _bounded(error, limit=_REDACT_RESULT_LIMIT)
                if error is not None
                else existing.error
            ),
            created_at=existing.created_at,
            updated_at=current_iso_timestamp(),
        )
        self.db.update_tool_effect(updated)
        self._effects[updated.id] = updated
        self._bump_summary()
        return updated

    def record_failure(
        self,
        failure_kind: FailureKind,
        failure_reason: str | None = None,
    ) -> None:
        """Set the attempt-level failure classification.

        Does not close the trace; use :meth:`finalise` for that.  The
        reason is bounded and secret-stripped like every other redacted
        field.
        """
        self.failure_kind = failure_kind
        self.failure_reason = _bounded(
            _redact_secret_payload(failure_reason), limit=_REDACT_RESULT_LIMIT
        )
        self.updated_at = current_iso_timestamp()
        self._persist_trace()

    def finalise(self) -> AttemptTrace:
        """Close the trace row and return the persisted evidence.

        Safe to call multiple times; only the first call writes
        ``completed_at``.  Use this when the Attempt has reached a final
        outcome (success, terminal failure, or before launching the failover
        Attempt B).
        """
        if self.completed_at is None:
            self.completed_at = current_iso_timestamp()
            self.updated_at = self.completed_at
        self._persist_trace()
        return self.as_attempt_trace()

    def as_attempt_trace(self) -> AttemptTrace:
        return AttemptTrace(
            id=self.trace_id,
            run_id=self.run_id,
            attempt_id=self.attempt_id,
            trace_class=self.trace_class,
            source_kind=self.source_kind,
            started_at=self.started_at,
            updated_at=self.updated_at,
            completed_at=self.completed_at,
            tool_effects_count=len(self._effects),
            summary_json=_bounded(_canonical_json(self._summary), limit=_SUMMARY_LIMIT)
            if self._summary
            else None,
            failure_kind=self.failure_kind.value if self.failure_kind is not None else None,
            failure_reason=self.failure_reason,
        )

    # -- native structured event ingestion --------------------------------

    def ingest_native_event(self, event: dict[str, Any]) -> None:
        """Ingest one native structured event from the backend.

        The recorder never parses terminal text into events.  It accepts
        whatever the backend actually emitted, normalises the bounded
        counters into the recorder summary, and lets the caller decide
        whether the event represents a tool operation.  The backend is
        responsible for translating its native event vocabulary into the
        canonical :class:`ToolEffect` shape via
        :meth:`record_tool_effect`.
        """
        if not isinstance(event, dict):
            return
        events_total = int(self._summary.get("native_events_total", 0)) + 1
        self._summary["native_events_total"] = events_total
        self._summary["last_native_event_digest"] = _summary_digest(
            {"event": event}
        )
        self._summary["last_native_event_type"] = str(event.get("type") or "unknown")
        self._bump_summary()

    # -- private helpers --------------------------------------------------

    def _bump_summary(self) -> None:
        self.updated_at = current_iso_timestamp()
        self._summary["tool_effects_count"] = len(self._effects)
        reconciliation_counts: dict[str, int] = {}
        for effect in self._effects.values():
            key = effect.reconciliation_status
            reconciliation_counts[key] = reconciliation_counts.get(key, 0) + 1
        self._summary["reconciliation_counts"] = reconciliation_counts
        self._summary["trace_class"] = self.trace_class.value
        self._summary["source_kind"] = self.source_kind
        self._persist_trace()

    def _persist_trace(self) -> None:
        self.db.create_attempt_trace(self.as_attempt_trace())


def is_autonomous_eligible(trace_class: TraceCapability) -> bool:
    """Return ``True`` iff ``trace_class`` permits autonomous execution.

    Per the C11-C trace rule: an autonomous Attempt requires
    ``trace != NONE``.  ``NONE`` must reject the dispatch before any
    semantic execution; the recorder itself does not enforce that -- a
    runtime dispatcher asks this predicate before launching a worker.
    """
    return trace_class is not TraceCapability.NONE


def operation_for_capability(value: str | None) -> str:
    """Return the canonical operation string for a C08 capability ref.

    The fallback is :attr:`CapabilityOperation.PROCESS_RUN` so a worker
    tool that does not match one of the canonical operation classes is
    still recorded under a bounded, deterministic operation name.
    """
    if value is None:
        return CapabilityOperation.PROCESS_RUN.value
    if value in {op.value for op in CapabilityOperation}:
        return value
    return CapabilityOperation.PROCESS_RUN.value


__all__ = [
    "AttemptTraceRecorder",
    "FAILURE_KIND_MAP",
    "classify_failure",
    "is_autonomous_eligible",
    "operation_for_capability",
]
