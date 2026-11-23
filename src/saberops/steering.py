"""Owner steering notes, delivered at the next worker attempt boundary.

A note is never typed into a live worker: it is recorded on the job and
included in the prompt of the next attempt that starts for the job's current
run, which is the next safe point (ADR-0016).  Delivery is recorded once, on
the attempt whose prompt carried the note; later attempts do not repeat it.

The orchestrator only reads notes at attempt assembly and records delivery
after the attempt row exists, so a note cannot reach a process that is
already running.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from saberops.jobs.models import SteeringNote

if TYPE_CHECKING:
    from saberops.db import Database

__all__ = [
    "append_steering_section",
    "render_steering_section",
    "steering_section_for_run",
    "record_steering_delivery",
]

_STEERING_BANNER = (
    "[STEERING NOTES]\n"
    "Corrections for work already in progress. Owner notes come from the owner; "
    "an older advisor note is advice, not authority. Apply them to the current "
    "attempt; keep completed work and do not redo it."
)


def render_steering_section(notes: tuple[SteeringNote, ...] | list[SteeringNote]) -> str:
    """Render one bounded, clearly delimited section for the attempt prompt."""
    lines = [_STEERING_BANNER, ""]
    for note in notes:
        source = "advisor" if getattr(note, "source", "owner") == "advisor" else "owner"
        lines.append(f"- ({note.created_at}) [{source}] {note.body}")
    return "\n".join(lines)


def append_steering_section(prompt: str, section: str) -> str:
    if not section:
        return prompt
    return prompt.rstrip() + "\n\n" + section.rstrip()


def steering_section_for_run(db: Database, run_id: str) -> tuple[str, tuple[SteeringNote, ...]]:
    """Section text plus the pending notes it covers (a read; no delivery mark)."""
    from saberops.jobs.store import pending_steering_notes_for_run

    notes = tuple(pending_steering_notes_for_run(db, run_id))
    if not notes:
        return "", ()
    return render_steering_section(notes), notes


def record_steering_delivery(
    db: Database,
    notes: tuple[SteeringNote, ...] | list[SteeringNote],
    *,
    run_id: str,
    attempt_id: str,
) -> tuple[SteeringNote, ...]:
    """Mark these notes delivered to ``attempt_id`` once and return the winners."""
    from saberops.jobs.store import mark_steering_notes_delivered

    if not notes:
        return ()
    delivered = tuple(
        mark_steering_notes_delivered(
            db,
            [note.note_id for note in notes],
            run_id=run_id,
            attempt_id=attempt_id,
        )
    )
    if delivered:
        db.record_event(
            run_id,
            "steering_notes_delivered",
            attempt_id=attempt_id,
            payload={
                "note_ids": [note.note_id for note in delivered],
                "count": len(delivered),
                "job_ids": sorted({note.job_id for note in delivered}),
            },
        )
    return delivered
