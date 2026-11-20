"""Durable gate-verdict projection for owner-facing run summaries.

The ``orch run`` end-of-run summary answers one narrow question: what did the
authoritative gate do for this run's current candidate?  That answer is not the
run's terminal status -- a gate can pass and a later required review can still
fail the run closed -- so it is projected here from durable evidence only.

Authority order (never inferred from run status, acceptance, or branch state):

1. ``checks`` rows bound to the current candidate attempt.  The current
   candidate is the latest ``SUCCESS`` attempt carrying a commit SHA, the same
   identity rule the accept and candidate-lifecycle projections use.
2. Explicit ``gate_passed`` / ``gate_failed`` / ``gate_skipped`` events, for
   runs that never produced a successful candidate (for example a failing
   gate).
3. Any persisted check row, when events are unavailable.

A run with no gate evidence at all is ``NOT_RUN`` rather than a guess.

This module is read-only: no writes and no second state machine.
"""

from __future__ import annotations

from enum import StrEnum

from saberops.db import Database
from saberops.models import AttemptStatus


class GateVerdict(StrEnum):
    """Projected gate verdict for a run's current candidate."""

    PASSED = "PASSED"
    FAILED = "FAILED"
    NOT_RUN = "NOT_RUN"

    def summary_label(self) -> str:
        """Return the value printed on the ``Gate Passed`` summary line."""
        return {
            GateVerdict.PASSED: "YES",
            GateVerdict.FAILED: "NO",
            GateVerdict.NOT_RUN: "NOT RUN",
        }[self]


_GATE_EVENT_VERDICTS: dict[str, GateVerdict] = {
    "gate_passed": GateVerdict.PASSED,
    "gate_failed": GateVerdict.FAILED,
    "gate_skipped": GateVerdict.NOT_RUN,
}


def _check_verdict(passed: bool, skipped: bool) -> GateVerdict:
    if skipped:
        return GateVerdict.NOT_RUN
    return GateVerdict.PASSED if passed else GateVerdict.FAILED


def run_gate_verdict(db: Database, run_id: str) -> GateVerdict:
    """Return the durable gate verdict for ``run_id``'s current candidate."""
    attempts = db.get_attempts_for_run(run_id)
    checks = db.get_checks_for_run(run_id)

    candidates = [
        attempt
        for attempt in attempts
        if attempt.status == AttemptStatus.SUCCESS and attempt.commit_sha
    ]
    if candidates:
        candidate = candidates[-1]
        candidate_checks = [
            check for check in checks if check.attempt_id == candidate.id
        ]
        if candidate_checks:
            last = candidate_checks[-1]
            return _check_verdict(last.passed, last.skipped)

    verdict: GateVerdict | None = None
    for event in db.get_events(run_id, limit=100_000):
        mapped = _GATE_EVENT_VERDICTS.get(event.event_type)
        if mapped is not None:
            verdict = mapped
    if verdict is not None:
        return verdict

    if checks:
        last_check = checks[-1]
        return _check_verdict(last_check.passed, last_check.skipped)

    return GateVerdict.NOT_RUN


def gate_summary_label(db: Database, run_id: str) -> str:
    """Return the rendered ``Gate Passed`` summary value for ``run_id``."""
    return run_gate_verdict(db, run_id).summary_label()
