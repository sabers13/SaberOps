"""Bounded read-only artifact evidence for conversational run explanations.

Canonical candidate identity and filenames come from the same projector as
Changes. Worker prose stays advisory; check results come from persisted rows.
No repository files are executed, candidates accepted, or models invoked here.
"""
from __future__ import annotations

from typing import Any

from saberops.candidate_diff import CHANGED_PATHS_MAX, project_candidate_diff
from saberops.db import Database
from saberops.models import AttemptStatus

TEXT_LIMIT = 12000
PATH_BUDGET = 8000
RUN_TEXT_BUDGET = 32000


def bounded_text(value: str, limit: int = TEXT_LIMIT) -> dict[str, Any]:
    """Keep both the start and final result of long logs, with explicit gaps."""
    if len(value) <= limit:
        return {"text": value, "truncated": False}
    if limit <= 0:
        return {"text": "", "truncated": True}
    half = limit // 2
    return {
        "text": value[:half] + "\n[... log truncated for chat ...]\n" + value[-(limit - half):],
        "truncated": True,
    }


def build_chat_artifacts(db: Database, run_id: str) -> dict[str, Any]:
    text_budget = RUN_TEXT_BUDGET

    def text(value: str, limit: int = TEXT_LIMIT) -> dict[str, Any]:
        nonlocal text_budget
        allowance = min(limit, text_budget)
        text_budget -= min(len(value), allowance)
        return bounded_text(value, allowance)

    diff = project_candidate_diff(db, run_id)
    paths: list[str] = []
    remaining = PATH_BUDGET
    for path in diff.changed_paths:
        if len(path) > remaining:
            break
        paths.append(path)
        remaining -= len(path)
    # The canonical projection's truncated flag also covers diff text. A
    # path list below its cap is complete even if the unused diff was cut.
    paths_complete = (
        diff.available
        and len(paths) == len(diff.changed_paths)
        and len(diff.changed_paths) < CHANGED_PATHS_MAX
    )
    candidate = {
        "source": "canonical_candidate_diff",
        "candidate_sha": diff.candidate_sha,
        "base_sha": diff.base_sha,
        "available": diff.available,
        "changed_paths": paths,
        "changed_paths_complete": paths_complete,
        "changed_file_count": len(paths) if paths_complete else None,
        "unavailable_reason": diff.unavailable_reason,
    }
    # A later failed repair carrying a SHA must never become the handoff
    # for the verified candidate. Match the projector, not latest attempt.
    selected = next(
        (attempt for attempt in reversed(db.get_attempts_for_run(run_id))
         if diff.available and attempt.status == AttemptStatus.SUCCESS
         and attempt.commit_sha == diff.candidate_sha),
        None,
    )
    handoff: dict[str, Any] | None = None
    checks: list[dict[str, Any]] = []
    checks_complete = False
    if selected is not None:
        handoff = {
            "source": "recorded_worker_response_advisory",
            "attempt_id": selected.id,
            "candidate_sha": selected.commit_sha,
            "recorded_worktree_path": selected.worktree_path,
            "stdout": text(selected.worker_stdout),
            "stderr": text(selected.worker_stderr, 2000),
        }
        receipts = db.get_checks_for_attempt(selected.id)
        checks_complete = len(receipts) <= 3
        for check in receipts[-3:]:
            checks.append({
                "source": "persisted_gate_check",
                "id": check.id,
                "attempt_id": check.attempt_id,
                "candidate_sha": selected.commit_sha,
                "command": check.gate_command,
                "exit_code": check.exit_code,
                "passed": check.passed,
                "skipped": check.skipped,
                "stdout": text(check.stdout),
                "stderr": text(check.stderr),
            })
    reviews = db.get_reviews_for_run(run_id)
    return {
        "candidate": candidate,
        "worker_handoff": handoff,
        "candidate_checks": checks,
        "candidate_checks_complete": checks_complete,
        "review_responses": [
            {
                "id": review.id,
                "verdict": review.verdict.value,
                "summary": text(review.summary, 1000),
                "details": text(review.details, 2000),
            }
            for review in reviews[-3:]
        ],
        "review_responses_complete": len(reviews) <= 3,
    }
