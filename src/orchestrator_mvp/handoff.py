"""Structured, immutable run handoffs and compact resume capsules."""
# ruff: noqa: E501

from __future__ import annotations

import json
import uuid
from typing import Any

from orchestrator_mvp.context_projection import objective_digest, objective_slug
from orchestrator_mvp.db import Database, current_iso_timestamp
from orchestrator_mvp.models import Handoff


def _unknown(value: object | None) -> object | str:
    return "UNKNOWN" if value is None or value == "" else value


def _bounded(value: object | None, limit: int = 240) -> str:
    """Render factual capsule data without turning a handoff into a transcript."""
    text = str(_unknown(value)).replace("\n", " ").strip()
    return text[:limit]


def build_full_handoff(db: Database, run_id: str) -> str:
    """Build a factual handoff from persisted state, never model/history inference."""
    run = db.get_run(run_id)
    if run is None:
        raise ValueError(f"unknown run: {run_id}")
    plan = db.get_run_plan(run_id)
    steps = db.get_plan_steps(run_id)
    packages = db.get_work_packages(run_id)
    attempts = db.get_attempts_for_run(run_id)
    checks = [check for attempt in attempts for check in db.get_checks_for_attempt(attempt.id)]
    reviews = db.get_reviews_for_run(run_id)
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
    payload: dict[str, Any] = {
        "OBJECTIVE": {
            "original_goal": run.task,
            "current_subgoal": current.goal if current else "UNKNOWN",
            "success_criteria": [package.acceptance_criteria for package in packages],
            "out_of_scope": "UNKNOWN",
        },
        "AUTHORITATIVE_STATE": {
            "repo": run.target_repo,
            "base_sha": run.base_commit,
            "candidate_sha": _unknown(candidate),
            "working_tree_vs_candidate_vs_runtime": "UNKNOWN",
            "db_run_id": run.id,
            "current_stage": current.status.value if current else run.status.value,
        },
        "COMPLETED_WORK": [
            {
                "package": package.id,
                "goal": package.goal,
                "attempt": next(
                    (attempt.id for attempt in attempts if attempt.id == step.attempt_id), "UNKNOWN"
                ),
                "result": step.status.value,
            }
            for package, step in zip(packages, steps, strict=False)
            if step.status.value == "COMPLETED"
        ],
        "CURRENT_ACTIVE_WORK": {
            "package": _unknown(supervisor.current_package_id if supervisor else None),
            "attempt": _unknown(supervisor.current_attempt_id if supervisor else None),
            "provider": _unknown(supervisor.provider if supervisor else None),
            "model": _unknown(supervisor.model if supervisor else None),
            "assignment": current.goal if current else "UNKNOWN",
            "started_at": _unknown(supervisor.started_at if supervisor else None),
            "last_activity": _unknown(supervisor.last_activity_at if supervisor else None),
            "health": _unknown(supervisor.health_state.value if supervisor else None),
            "deadline": _unknown(supervisor.deadline_at if supervisor else None),
            "supervisor": _unknown(supervisor.supervisor_identity if supervisor else None),
            "wake_condition": _unknown(supervisor.wake_condition if supervisor else None),
        },
        "DECISIONS_ALREADY_MADE": {
            "routing_snapshot": _unknown(run.routing_snapshot_json),
            "plan_version": _unknown(plan.version if plan else None),
            "tier": run.tier.value,
            "training_allowed": run.training_allowed,
            "review_state": run.review_state_json or "UNKNOWN",
            "tool_refs": [
                item.get("ref")
                for item in (
                    json.loads(run.tool_contract_json).get("bindings", [])
                    if run.tool_contract_json
                    else []
                )
                if isinstance(item, dict) and item.get("ref")
            ],
            "tool_contract_digest": run.tool_contract_digest or "UNKNOWN",
        },
        "FAILURES_BLOCKERS_LESSONS": [
            {"attempt": attempt.id, "error": attempt.worker_error}
            for attempt in attempts
            if attempt.worker_error
        ]
        or "UNKNOWN",
        "REVIEW_STATE": [
            {
                "review": review.id,
                "provider": review.provider,
                "model": review.model,
                "verdict": review.verdict.value,
                "summary": review.summary,
            }
            for review in reviews
        ]
        or "UNKNOWN",
        "GATE_TEST_STATE": {
            "command": _unknown(latest_check.gate_command if latest_check else None),
            "passed": _unknown(latest_check.passed if latest_check else None),
            # A later mutation invalidates any earlier PASS.  The gate SHA is
            # therefore the candidate of the attempt which actually ran it,
            # not the newest candidate in the run.
            "candidate_sha": _unknown(checked_attempt.commit_sha if checked_attempt else None),
            "created_at": _unknown(latest_check.created_at if latest_check else None),
        },
        "FILE_CHANGE_SUMMARY": "UNKNOWN",
        "RUNTIME_PROVIDER_CONSTRAINTS": "See frozen routing snapshot and dispatch events.",
        "RELEVANT_OWNER_WORKFLOW_INSTRUCTIONS": "Use orch wait; do not infer worker health from PID alone.",
        "REMAINING_WORK": [
            {
                "step_id": step.id,
                "goal": step.goal,
                "status": step.status.value,
                "dependencies": list(step.dependency_ids),
            }
            for step in steps
            if step.status.value not in ("COMPLETED", "SKIPPED")
        ],
        "EXACT_NEXT_ACTION": "Resume the first READY/RUNNING persisted plan step; fail closed if evidence differs.",
        "WAKE_RESUME_CONDITION": _unknown(supervisor.wake_condition if supervisor else None),
        "COMPACT_MACHINE_SUMMARY": {
            "run_id": run_id,
            "status": run.status.value,
            "candidate_sha": _unknown(candidate),
            "plan_id": _unknown(plan.id if plan else None),
        },
    }
    return json.dumps(payload, sort_keys=True, indent=2)


def create_handoffs(db: Database, run_id: str) -> tuple[Handoff, Handoff]:
    """Append a full checkpoint plus bounded capsule, preserving prior versions."""
    full_content = build_full_handoff(db, run_id)
    full = Handoff(
        f"handoff_{uuid.uuid4().hex[:10]}",
        run_id,
        "handoff_full",
        db.next_handoff_version(run_id, "handoff_full"),
        full_content,
        current_iso_timestamp(),
    )
    db.create_handoff(full)
    parsed = json.loads(full_content)
    objective = str(parsed["OBJECTIVE"]["original_goal"])
    # This is deliberately built from bounded, deterministic state rather
    # than by copying OBJECTIVE.original_goal out of the operator/audit
    # handoff.  The full handoff retains that field; a continuation worker
    # must never receive it through this capsule.
    compact = {
        "run_id": run_id,
        "objective_slug": objective_slug(objective),
        "objective_digest": objective_digest(objective),
        "plan": parsed["DECISIONS_ALREADY_MADE"]["plan_version"],
        "candidate_sha": parsed["AUTHORITATIVE_STATE"]["candidate_sha"],
        "current_subgoal": _bounded(parsed["OBJECTIVE"]["current_subgoal"]),
        "active_work": {
            key: _bounded(value)
            for key, value in parsed["CURRENT_ACTIVE_WORK"].items()
            if key
            in {"package", "attempt", "provider", "model", "health", "wake_condition"}
        },
        "gate": parsed["GATE_TEST_STATE"],
        "next_action": _bounded(parsed["EXACT_NEXT_ACTION"]),
        "wake_reason": _bounded(parsed["WAKE_RESUME_CONDITION"]),
        "invariants": ("C05-INV-1", "C05-INV-2", "C05-INV-3", "C05-INV-6"),
        "full_handoff_version": full.version,
        "tool_refs": parsed["DECISIONS_ALREADY_MADE"].get("tool_refs", []),
        "tool_contract_digest": parsed["DECISIONS_ALREADY_MADE"].get(
            "tool_contract_digest", "UNKNOWN"
        ),
    }
    capsule = Handoff(
        f"handoff_{uuid.uuid4().hex[:10]}",
        run_id,
        "handoff_resume_capsule",
        db.next_handoff_version(run_id, "handoff_resume_capsule"),
        json.dumps(compact, sort_keys=True),
        current_iso_timestamp(),
    )
    db.create_handoff(capsule)
    db.record_event(
        run_id,
        "checkpoint_created",
        payload={"full_version": full.version, "capsule_version": capsule.version},
    )
    return full, capsule
