"""C14-C production phase telemetry (ROADMAP §11 wall-clock decomposition).

This module is observational ONLY.  It persists the canonical phase
vocabulary into a dedicated, additive ``phase_observations`` table so the
runtime can answer the supervisor question: ``for run_id, which ROADMAP §11
phases were MEASURED, which were UNKNOWN, and which were NOT_APPLICABLE?``

The recorder never invents a duration.  When the runtime cannot honestly
separate a phase (e.g. the kernel-enforced sandbox setup is fused with
``PROCESS_STARTUP`` in the current architecture), the phase is persisted as
``UNKNOWN`` with a note explaining why -- never split or inferred.

Authority isolation: nothing here influences routing, effort, quota,
sandbox/tool authorization, scheduling, lifecycle, review, validation, or
gate policy.  The recorder is a passive observer of real production
boundaries.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from types import TracebackType
from typing import Protocol

from orchestrator_mvp.db import Database, current_iso_timestamp
from orchestrator_mvp.models import (
    CANONICAL_PHASE_STATUSES,
    CANONICAL_PHASES,
    PhaseKind,
    PhaseObservation,
    PhaseStatus,
)


class _PhaseClock(Protocol):
    """Bounded monotonic-time source for phase boundary measurement.

    Production callers use :func:`time.monotonic`; tests inject a fake
    clock so the boundary math is deterministic without monkey-patching the
    stdlib.
    """

    def __call__(self) -> float: ...


@dataclass(frozen=True)
class _RecordedBoundary:
    """Internal record produced by :func:`measure_phase`."""

    phase: PhaseKind
    status: PhaseStatus
    duration_seconds: float | None
    started_monotonic: float | None
    ended_monotonic: float | None
    source_kind: str
    boundary_kind: str | None
    attempt_id: str | None
    review_id: str | None
    package_id: str | None
    plan_step_id: str | None
    notes: str | None


def _validate_phase(value: str) -> str:
    """Return ``value`` verbatim when it is a canonical phase label."""
    if value not in CANONICAL_PHASES:
        raise ValueError(
            f"phase '{value}' is not a canonical ROADMAP §11 phase; "
            f"expected one of {sorted(CANONICAL_PHASES)}"
        )
    return value


def _validate_status(value: str) -> str:
    """Return ``value`` verbatim when it is a canonical phase status."""
    if value not in CANONICAL_PHASE_STATUSES:
        raise ValueError(
            f"phase status '{value}' is not canonical; expected one of "
            f"{sorted(CANONICAL_PHASE_STATUSES)}"
        )
    return value


def record_phase_observation(
    db: Database,
    *,
    run_id: str,
    phase: PhaseKind | str,
    status: PhaseStatus | str,
    duration_seconds: float | None = None,
    source_kind: str,
    boundary_kind: str | None = None,
    attempt_id: str | None = None,
    review_id: str | None = None,
    package_id: str | None = None,
    plan_step_id: str | None = None,
    started_monotonic: float | None = None,
    ended_monotonic: float | None = None,
    notes: str | None = None,
) -> PhaseObservation:
    """Persist one phase observation row verbatim.

    This is the low-level record call.  Production runtime code prefers
    :class:`PhaseRecorder` for MEASURED phases because the recorder owns
    the monotonic anchors.  Use this helper directly for UNKNOWN /
    NOT_APPLICABLE observations where there is no real boundary to time.
    For MEASURED observations the supplied ``duration_seconds`` must equal
    ``ended_monotonic - started_monotonic`` exactly (validated by the
    :class:`PhaseObservation` typed boundary), so a caller can never
    persist a duration that contradicts its own anchors.
    """
    phase_value = phase.value if isinstance(phase, PhaseKind) else _validate_phase(phase)
    status_value = status.value if isinstance(status, PhaseStatus) else _validate_status(status)
    observation = PhaseObservation(
        id=f"phase_{uuid.uuid4().hex}",
        run_id=run_id,
        phase=phase_value,
        status=status_value,
        duration_seconds=duration_seconds,
        source_kind=source_kind,
        boundary_kind=boundary_kind,
        attempt_id=attempt_id,
        review_id=review_id,
        package_id=package_id,
        plan_step_id=plan_step_id,
        started_monotonic=started_monotonic,
        ended_monotonic=ended_monotonic,
        observed_at=current_iso_timestamp(),
        notes=notes,
    )
    db.create_phase_observation(observation)
    return observation


def mark_phase_unknown(
    db: Database,
    *,
    run_id: str,
    phase: PhaseKind | str,
    source_kind: str,
    boundary_kind: str | None = None,
    attempt_id: str | None = None,
    review_id: str | None = None,
    package_id: str | None = None,
    plan_step_id: str | None = None,
    notes: str,
) -> PhaseObservation:
    """Persist an explicit UNKNOWN observation for ``phase``.

    ``notes`` is mandatory and must explain why the phase cannot be split
    honestly in the current production architecture; it is the only place
    where the absence of a measurement is justified to the supervisor.
    """
    return record_phase_observation(
        db,
        run_id=run_id,
        phase=phase,
        status=PhaseStatus.UNKNOWN,
        source_kind=source_kind,
        boundary_kind=boundary_kind,
        attempt_id=attempt_id,
        review_id=review_id,
        package_id=package_id,
        plan_step_id=plan_step_id,
        notes=notes,
    )


def mark_phase_not_applicable(
    db: Database,
    *,
    run_id: str,
    phase: PhaseKind | str,
    source_kind: str,
    boundary_kind: str | None = None,
    attempt_id: str | None = None,
    review_id: str | None = None,
    package_id: str | None = None,
    plan_step_id: str | None = None,
    notes: str | None = None,
) -> PhaseObservation:
    """Persist an explicit NOT_APPLICABLE observation for ``phase``.

    Use this when the runtime positively knows the phase did not apply to
    the execution (for example, ``REVIEW`` on a run that never launched
    a reviewer).  An absent row is NOT a substitute; the contract requires
    an explicit NOT_APPLICABLE row with an honest note.
    """
    return record_phase_observation(
        db,
        run_id=run_id,
        phase=phase,
        status=PhaseStatus.NOT_APPLICABLE,
        source_kind=source_kind,
        boundary_kind=boundary_kind,
        attempt_id=attempt_id,
        review_id=review_id,
        package_id=package_id,
        plan_step_id=plan_step_id,
        notes=notes,
    )


@contextmanager
def measure_phase(
    db: Database,
    *,
    run_id: str,
    phase: PhaseKind | str,
    source_kind: str,
    boundary_kind: str | None = None,
    attempt_id: str | None = None,
    review_id: str | None = None,
    package_id: str | None = None,
    plan_step_id: str | None = None,
    notes: str | None = None,
    clock: _PhaseClock = time.monotonic,
) -> Iterator[_RecordedBoundary]:
    """Context manager that times one real production boundary.

    Captures a monotonic ``started`` on enter and ``ended`` on exit,
    persists a MEASURED ``PhaseObservation`` row, and yields the captured
    boundary so callers can introspect it before the row reaches the
    database.  The recorder NEVER suppresses an exception: a measured
    phase that raises propagates the original exception after the row has
    been written, so the lifecycle semantics are preserved.

    The recorder never inspects the return value of the wrapped body, so
    a MEASURED duration cannot influence routing, scheduling, retry
    budgets, validation outcomes, or review decisions downstream.
    """
    phase_value = phase.value if isinstance(phase, PhaseKind) else _validate_phase(phase)
    started = clock()
    record: _RecordedBoundary | None = None
    try:
        record = _RecordedBoundary(
            phase=PhaseKind(phase_value),
            status=PhaseStatus.MEASURED,
            duration_seconds=None,
            started_monotonic=started,
            ended_monotonic=None,
            source_kind=source_kind,
            boundary_kind=boundary_kind,
            attempt_id=attempt_id,
            review_id=review_id,
            package_id=package_id,
            plan_step_id=plan_step_id,
            notes=notes,
        )
        yield record
    finally:
        ended = clock()
        if record is not None:
            assert record.started_monotonic is not None
            # Durable evidence keeps the actual monotonic delta at full
            # float precision.  No persistence-level rounding: a positive
            # measurement (however small) must never collapse into 0.0,
            # and the persisted duration must stay exactly consistent
            # with the persisted anchors.
            duration = ended - record.started_monotonic
            observation = PhaseObservation(
                id=f"phase_{uuid.uuid4().hex}",
                run_id=run_id,
                phase=phase_value,
                status=PhaseStatus.MEASURED.value,
                duration_seconds=duration,
                source_kind=source_kind,
                boundary_kind=boundary_kind,
                attempt_id=attempt_id,
                review_id=review_id,
                package_id=package_id,
                plan_step_id=plan_step_id,
                started_monotonic=record.started_monotonic,
                ended_monotonic=ended,
                observed_at=current_iso_timestamp(),
                notes=notes,
            )
            try:
                db.create_phase_observation(observation)
            except Exception:
                # Telemetry must not corrupt the wrapped boundary.  An
                # observation write that fails is silently dropped here
                # and re-raised for the supervisor to inspect separately;
                # the original exception (if any) still propagates.
                pass


@dataclass
class PhaseRecorder:
    """Object-oriented wrapper around :func:`measure_phase`.

    Use when several sub-boundaries under the same phase must share an
    explicit source_kind / attribution and the call site prefers a
    context-manager object to a ``with`` statement.
    """

    db: Database
    run_id: str
    phase: PhaseKind | str
    source_kind: str
    boundary_kind: str | None = None
    attempt_id: str | None = None
    review_id: str | None = None
    package_id: str | None = None
    plan_step_id: str | None = None
    notes: str | None = None
    clock: _PhaseClock = time.monotonic

    def __call__(self) -> _PhaseMeasurement:
        return _PhaseMeasurement(self)


@dataclass
class _PhaseMeasurement:
    """Single-measurement handle produced by :class:`PhaseRecorder`."""

    recorder: PhaseRecorder
    _cm: AbstractContextManager[_RecordedBoundary] | None = None
    _boundary: _RecordedBoundary | None = None

    def __enter__(self) -> _RecordedBoundary:
        self._cm = measure_phase(
            self.recorder.db,
            run_id=self.recorder.run_id,
            phase=self.recorder.phase,
            source_kind=self.recorder.source_kind,
            boundary_kind=self.recorder.boundary_kind,
            attempt_id=self.recorder.attempt_id,
            review_id=self.recorder.review_id,
            package_id=self.recorder.package_id,
            plan_step_id=self.recorder.plan_step_id,
            notes=self.recorder.notes,
            clock=self.recorder.clock,
        )
        self._boundary = self._cm.__enter__()
        return self._boundary

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._cm is None:
            return
        self._cm.__exit__(exc_type, exc, tb)


def list_phase_observations(db: Database, run_id: str) -> list[PhaseObservation]:
    """Read back every phase observation for ``run_id`` in stable order.

    This is a thin wrapper over
    :meth:`orchestrator_mvp.db.Database.list_phase_observations_for_run` so
    callers outside the persistence module do not need to import the
    table-level method directly.  It still returns a fresh list of typed
    rows with no in-process caching.
    """
    return db.list_phase_observations_for_run(run_id)


def summarise_observations(
    observations: list[PhaseObservation],
) -> dict[str, dict[str, int]]:
    """Bounded aggregate of measurement statuses per phase.

    This is a coverage summary only: it never computes a synthetic total,
    never collapses occurrences, and never invents durations.  It exists
    so a supervisor can answer "for each phase, how many MEASURED /
    UNKNOWN / NOT_APPLICABLE rows did the run produce?" without writing
    the aggregation themselves.
    """
    summary: dict[str, dict[str, int]] = {}
    for observation in observations:
        bucket = summary.setdefault(
            observation.phase,
            {"MEASURED": 0, "UNKNOWN": 0, "NOT_APPLICABLE": 0},
        )
        bucket[observation.status] = bucket.get(observation.status, 0) + 1
    return summary


def assert_canonical_phase(value: object) -> str:
    """Public guard that fails closed on an unknown phase string.

    Tests and runtime callers use this as the canonical admission gate;
    a free-form label cannot enter a typed production API.
    """
    if not isinstance(value, str):
        raise ValueError(f"phase label must be a string, got {type(value).__name__}")
    return _validate_phase(value)


__all__ = [
    "PhaseRecorder",
    "assert_canonical_phase",
    "list_phase_observations",
    "mark_phase_not_applicable",
    "mark_phase_unknown",
    "measure_phase",
    "record_phase_observation",
    "summarise_observations",
]
