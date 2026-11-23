"""The one bounded resume capsule every interruption path records."""

from __future__ import annotations

import json
import uuid
from typing import Any

from saberops.context_projection import objective_digest, objective_slug
from saberops.db import Database, current_iso_timestamp
from saberops.models import Handoff

CAPSULE_KIND = "handoff_resume_capsule"


def _unknown(value: object | None) -> object | str:
    return "UNKNOWN" if value is None or value == "" else value


def _bounded(value: object | None, limit: int = 240) -> str:
    """Render factual capsule data without turning a handoff into a transcript."""
    text = str(_unknown(value)).replace("\n", " ").strip()
    return text[:limit]


def build_resume_capsule(db: Database, run_id: str) -> dict[str, Any]:
    """Build the bounded capsule from persisted state, never model inference.

    The capsule is the one saved handoff: it names the run, its plan, its
    latest candidate, the checkpoint gate evidence and the next action, all
    from durable rows.  The owner-facing evidence stays in ``orch report``;
    this artifact is what a resumed attempt and ``catch_up`` reference.
    """
    run = db.get_run(run_id)
    if run is None:
        raise ValueError(f"unknown run: {run_id}")
    plan = db.get_run_plan(run_id)
    steps = db.get_plan_steps(run_id)
    attempts = db.get_attempts_for_run(run_id)
    checks = [check for attempt in attempts for check in db.get_checks_for_attempt(attempt.id)]
    supervisor = db.get_supervision(run_id)
    current = next((step for step in steps if step.status.value in ("RUNNING", "READY")), None)
    candidate = next(
        (attempt.commit_sha for attempt in reversed(attempts) if attempt.commit_sha), None
    )
    latest_check = checks[-1] if checks else None
    checked_attempt = next(
        (
            attempt
            for attempt in attempts
            if latest_check is not None and attempt.id == latest_check.attempt_id
        ),
        None,
    )
    tool_refs: list[Any] = []
    if run.tool_contract_json:
        try:
            tool_refs = [
                item.get("ref")
                for item in json.loads(run.tool_contract_json).get("bindings", [])
                if isinstance(item, dict) and item.get("ref")
            ]
        except ValueError:
            tool_refs = []
    return {
        "run_id": run_id,
        "objective_slug": objective_slug(run.task),
        "objective_digest": objective_digest(run.task),
        "plan": plan.version if plan else None,
        "candidate_sha": _unknown(candidate),
        "current_subgoal": _bounded(current.goal if current else run.status.value),
        "active_work": {
            key: _bounded(value)
            for key, value in {
                "package": supervisor.current_package_id if supervisor else None,
                "attempt": supervisor.current_attempt_id if supervisor else None,
                "provider": supervisor.provider if supervisor else None,
                "model": supervisor.model if supervisor else None,
                "health": supervisor.health_state.value if supervisor else None,
                "wake_condition": supervisor.wake_condition if supervisor else None,
            }.items()
        },
        "gate": {
            "command": _unknown(latest_check.gate_command if latest_check else None),
            "passed": _unknown(latest_check.passed if latest_check else None),
            # A later mutation invalidates any earlier PASS: the gate SHA is
            # the candidate of the attempt that actually ran it.
            "candidate_sha": _unknown(checked_attempt.commit_sha if checked_attempt else None),
            "created_at": _unknown(latest_check.created_at if latest_check else None),
        },
        "next_action": (
            "Resume the first READY/RUNNING persisted plan step; "
            "fail closed if evidence differs."
        ),
        "wake_reason": _bounded(supervisor.wake_condition if supervisor else None),
        "invariants": ("C05-INV-1", "C05-INV-2", "C05-INV-3", "C05-INV-6"),
        "tool_refs": tool_refs,
        "tool_contract_digest": run.tool_contract_digest or "UNKNOWN",
    }


def create_resume_capsule(db: Database, run_id: str) -> Handoff:
    """Append one bounded resume capsule, preserving prior versions."""
    capsule = Handoff(
        f"handoff_{uuid.uuid4().hex[:10]}",
        run_id,
        CAPSULE_KIND,
        db.next_handoff_version(run_id, CAPSULE_KIND),
        json.dumps(build_resume_capsule(db, run_id), sort_keys=True),
        current_iso_timestamp(),
    )
    db.create_handoff(capsule)
    db.record_event(
        run_id,
        "checkpoint_created",
        payload={"capsule_version": capsule.version},
    )
    return capsule


__all__ = ["CAPSULE_KIND", "build_resume_capsule", "create_resume_capsule"]
