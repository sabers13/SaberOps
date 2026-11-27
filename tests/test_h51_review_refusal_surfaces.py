"""H51: a refused review reaches the planner with its reason and next action.

H37's D9: ``wait_job`` timed out while the candidate already said
``REVIEW_UNAVAILABLE``; the owner had to read the database to learn why.
The reason and the owner's next action must be visible from ``wait_job``,
the status projection and the job's final result.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from lifecycle_rig import RigEnvironment, build_lifecycle_rig

from saberops.db import Database, current_iso_timestamp
from saberops.jobs.models import Job, JobState
from saberops.jobs.service import (
    get_job,
    job_status_view,
    review_refusal_view,
    submit_job,
)
from saberops.mcp.tools import ServerContext, build_default_registry
from saberops.model_access.readiness_service import ReadinessService
from saberops.models import ReviewResult, ReviewVerdict, RunStatus
from saberops.paths import owner_state_dir

pytestmark = pytest.mark.packaged_routing_default


def _tick(db: Database) -> None:
    from saberops.capacity.loop import run_scheduler_loop

    readiness = ReadinessService.for_owner_state(owner_state_dir())
    run_scheduler_loop(db, once=True, readiness_service=readiness)


def _refused_job(rig: RigEnvironment) -> str:
    db = rig.db()
    submitted = submit_job(db, repo=rig.repo, goal="Surface a refused review")
    _tick(db)
    job = get_job(db, submitted.job.job_id)
    assert job is not None and job.current_run_id is not None
    run = rig.wait_for_run(job.current_run_id, timeout=60.0)
    assert run.status is RunStatus.FAILED, run.result_summary
    _tick(db)
    return submitted.job.job_id


def test_review_unavailable_reaches_wait_job_and_job_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A quota-blocked reviewer names the cause and the owner's next action."""
    rig = build_lifecycle_rig(tmp_path, monkeypatch, reviewer_remaining=0)
    db = rig.db()
    job_id = _refused_job(rig)

    job = get_job(db, job_id)
    assert job is not None
    assert job.state is JobState.FAILED

    view = job_status_view(db, job)
    assert view["reason"] and "quota" in view["reason"].lower(), view["reason"]
    review = view["review"]
    assert review["verdict"] == ReviewVerdict.REVIEW_UNAVAILABLE.value
    assert review["reason"] == "quota"
    assert review["next_action"]

    context = ServerContext(
        db_path=db.db_path,
        questions_store=owner_state_dir() / "questions.json",
    )
    registry = build_default_registry(context)
    waited = registry.call("wait_job", {"job_id": job_id, "timeout_seconds": 0})
    assert waited["outcome"] == "final"
    assert waited["job"]["state"] == JobState.FAILED.value
    assert waited["job"]["review"]["reason"] == "quota"
    assert "quota" in waited["guidance"].lower()
    assert "resume_job" in waited["guidance"]
    assert waited["result"]["review"] == waited["job"]["review"]

    fetched = registry.call("get_job", {"job_id": job_id})
    assert fetched["result"]["review"]["verdict"] == (
        ReviewVerdict.REVIEW_UNAVAILABLE.value
    )


class _RefusalDB:
    """The two durable reads the refusal projection makes."""

    def __init__(
        self,
        review: ReviewResult,
        skips: list[dict[str, Any]] | None = None,
    ) -> None:
        self._review = review
        self._skips = skips or []

    def get_reviews_for_run(self, run_id: str) -> list[ReviewResult]:
        assert run_id == self._review.run_id
        return [self._review]

    def get_events_by_type(self, run_id: str, event_type: str) -> list[Any]:
        assert event_type == "review_candidate_skipped"
        return [SimpleNamespace(payload=dict(skip)) for skip in self._skips]


def _job(state: JobState = JobState.FAILED) -> Job:
    return Job.from_dict(
        {
            "job_id": "job_h51",
            "caller_id": "orchestrator",
            "repo": "/tmp/repo",
            "base_ref": "HEAD",
            "base_sha": "0" * 40,
            "goal": "surface a refused review",
            "requirements": {},
            "state": state.value,
            "failure_reason": "RUN_FAILED" if state is JobState.FAILED else None,
            "current_run_id": "run_h51",
            "run_ids": ["run_h51"],
        }
    )


def _review(
    verdict: ReviewVerdict,
    *,
    summary: str = "No reviewer available",
    details: str = "",
) -> ReviewResult:
    return ReviewResult(
        id="rev_h51",
        run_id="run_h51",
        provider="",
        model="",
        verdict=verdict,
        summary=summary,
        details=details,
        created_at=current_iso_timestamp(),
    )


@pytest.mark.parametrize(
    ("review", "skips", "reason"),
    [
        (
            _review(ReviewVerdict.REVIEW_UNAVAILABLE),
            [{"provider": "anthropic", "model": "claude-sonnet", "reason": "quota_blocked"}],
            "quota",
        ),
        (
            _review(ReviewVerdict.REVIEW_UNAVAILABLE),
            [{"provider": "codex", "model": "gpt-6", "reason": "same_model_as_worker"}],
            "no_independent_reviewer",
        ),
        (
            _review(ReviewVerdict.REVIEW_UNAVAILABLE),
            [],
            "no_independent_reviewer",
        ),
        (
            _review(
                ReviewVerdict.REVIEW_EXECUTION_FAILED,
                summary="Reviewer execution failed (no usable verdict produced)",
                details="- anthropic / claude-sonnet (status=TIMEOUT, error=TIMEOUT)",
            ),
            [{"provider": "anthropic", "model": "claude-sonnet", "reason": "provider_unavailable"}],
            "timeout",
        ),
        (
            _review(
                ReviewVerdict.REVIEW_EXECUTION_FAILED,
                summary="Reviewer execution failed (no usable verdict produced)",
                details="- anthropic / claude-sonnet (status=FAILED, error=non-zero exit)",
            ),
            [{"provider": "anthropic", "model": "claude-sonnet", "reason": "provider_unavailable"}],
            "execution_failed",
        ),
        (
            _review(ReviewVerdict.REVIEW_UNAVAILABLE),
            [
                {
                    "provider": "anthropic",
                    "model": "claude-sonnet",
                    "reason": "provider_readiness_not_ready",
                }
            ],
            "unavailable",
        ),
    ],
)
def test_refusal_categories_name_the_owner_action(
    review: ReviewResult,
    skips: list[dict[str, Any]],
    reason: str,
) -> None:
    """Each refusal kind says why and what the owner does next."""
    view = review_refusal_view(_RefusalDB(review, skips), _job())
    assert view is not None
    assert view["reason"] == reason
    assert view["next_action"]
    if skips:
        assert skips[0]["provider"] in view["detail"]


@pytest.mark.parametrize(
    "verdict", [ReviewVerdict.PASS, ReviewVerdict.BLOCK, ReviewVerdict.PASS_WITH_BACKLOG]
)
def test_a_substantive_review_is_not_a_refusal(verdict: ReviewVerdict) -> None:
    assert review_refusal_view(_RefusalDB(_review(verdict)), _job()) is None
