"""One automatic "continue" before a live but idle job needs attention.

A worker can go quiet while its process is still alive.  Today that quiet is
attention evidence and the job is surfaced as needing attention immediately, so
a worker that only stopped early waits for the owner.  Behind an explicit flag,
the engine instead records ONE bounded continue note on the job through the
existing steering-note channel (ADR-0016) and leaves the job running; the note
reaches the worker inside the prompt of its next attempt, exactly like an owner
note, and is never typed into the live process.

The nudge is spent once per job, ever: a later stall -- after the worker resumed
or after a retry -- goes straight to needs-attention instead of nudging again.
A run that is not a dispatched job (an ordinary ``orch run``) is never nudged,
and a nudge failure is fail-closed: the caller keeps raising attention.

Flag: ``ORCH_NUDGE_IDLE_WORKERS`` in ``1/true/yes/on`` enables the nudge.  Any
other value, including absence, leaves today's behaviour unchanged.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from saberops.jobs.models import JobState
from saberops.jobs.store import NUDGE_EVENT_KIND, record_nudge_note

if TYPE_CHECKING:
    from saberops.db import Database

__all__ = [
    "NUDGE_FLAG_ENV",
    "NUDGE_NOTE_BODY",
    "NudgeDecision",
    "maybe_nudge_idle_run",
    "nudging_enabled",
]

#: Off-by-default owner flag for the automatic nudge.
NUDGE_FLAG_ENV = "ORCH_NUDGE_IDLE_WORKERS"

#: The bounded continue instruction.  It is corrective text for the worker's
#: next attempt only: it cannot change scope, authority, routing or the gate.
NUDGE_NOTE_BODY = (
    "CONTINUE (automatic nudge): this job went quiet while the worker was "
    "still live. Resume the assigned task from where it stopped; keep the "
    "completed work, do not redo it, and finish or report the result."
)

_TRUTHY = frozenset({"1", "true", "yes", "on"})

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NudgeDecision:
    """Outcome of one nudge attempt; ``nudged`` alone grants the deferral."""

    nudged: bool
    reason: str
    job_id: str | None = None
    note_id: int | None = None


def nudging_enabled() -> bool:
    """Read the owner flag at call time (absent or anything else means off)."""
    return os.environ.get(NUDGE_FLAG_ENV, "").strip().lower() in _TRUTHY


def _running_job_for_run(db: Database, run_id: str) -> str | None:
    from saberops.jobs.service import list_jobs

    for job in list_jobs(db, state=JobState.RUNNING, limit=10_000):
        if job.current_run_id == run_id:
            return job.job_id
    return None


def _nudge(db: Database, *, run_id: str, attempt_id: str | None, reason: str) -> NudgeDecision:
    from saberops.models import RunStatus

    run = db.get_run(run_id)
    if run is None or run.status != RunStatus.RUNNING:
        return NudgeDecision(False, "run_not_running")
    job_id = _running_job_for_run(db, run_id)
    if job_id is None:
        return NudgeDecision(False, "no_running_job")
    note = record_nudge_note(
        db,
        job_id,
        run_id=run_id,
        attempt_id=attempt_id,
        body=NUDGE_NOTE_BODY,
        reason=reason,
    )
    if note is None:
        return NudgeDecision(False, "already_nudged", job_id=job_id)
    try:
        db.record_event(
            run_id,
            "job_nudged",
            attempt_id=attempt_id,
            payload={
                "job_id": job_id,
                "note_id": note.note_id,
                "reason": reason,
                "note_length": len(NUDGE_NOTE_BODY),
                "delivery": "next_attempt",
                "event_kind": NUDGE_EVENT_KIND,
            },
        )
    except Exception:
        # The durable note already spent the nudge; a failed run-event write
        # must not turn a real continue into attention.
        _logger.exception("could not record job_nudged for run %s", run_id)
    return NudgeDecision(True, "nudged", job_id=job_id, note_id=note.note_id)


def maybe_nudge_idle_run(
    db: Database,
    *,
    run_id: str,
    attempt_id: str | None = None,
    reason: str = "",
) -> NudgeDecision:
    """Nudge this run's job once, or report why attention must be raised.

    Never raises: any failure is reported as a non-nudge so the caller keeps the
    existing needs-attention behaviour instead of losing the signal.
    """
    if not nudging_enabled():
        return NudgeDecision(False, "flag_off")
    try:
        return _nudge(db, run_id=run_id, attempt_id=attempt_id, reason=reason)
    except Exception:
        _logger.exception("automatic nudge failed for run %s", run_id)
        return NudgeDecision(False, "nudge_failed")
