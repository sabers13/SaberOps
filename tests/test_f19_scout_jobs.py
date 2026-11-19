"""F19: scout jobs investigate read-only and deliver a report.

A scout is a job *kind* on the build route (roles stay PLAN/BUILD/CHECK).  It
runs the worker with the CLI's own read-only rules, saves a Markdown report in
the job's E4 folder, records the report on the job, turns owner decisions into
owner questions, and never produces a commit, a gate, a review or an
acceptance.

Hermetic: the A6 lifecycle rig's fake worker/reviewer CLI and a fresh store.
No vendor CLI, model or network call is made.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from lifecycle_rig import RigEnvironment, build_lifecycle_rig

from saberops.db import Database
from saberops.jobs.dependencies import dependency_blockers
from saberops.jobs.models import JobKind, JobState
from saberops.jobs.service import get_job, promote_scout, submit_job
from saberops.mcp.lease import OrchestratorLease
from saberops.mcp.tools import ServerContext, build_default_registry
from saberops.model_access.readiness_service import ReadinessService
from saberops.models import RunStatus
from saberops.paths import owner_state_dir
from saberops.questions import DecisionClass, OwnerQuestionQueue, QuestionStatus
from saberops.scout import SCOUT_REPORT_EVENT, parse_report, write_report

pytestmark = pytest.mark.packaged_routing_default

REPORT_TEXT = """\
## Summary
The retry helper is safe to reuse; no change is required.

## Findings
- `app.py:greet` already returns the greeting the task expects.
- No caller depends on the old signature.
"""

DECISION_REPORT_TEXT = """\
## Summary
Two storage layouts are plausible.

## Findings
- The current store keeps per-project evidence beside the owner database.

## Owner decisions
- Keep per-project evidence folders or move them under one shared root?
"""


@pytest.fixture()
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RigEnvironment:
    return build_lifecycle_rig(tmp_path, monkeypatch)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _tick(db: Database) -> None:
    from saberops.capacity.loop import run_scheduler_loop

    readiness = ReadinessService.for_owner_state(owner_state_dir())
    run_scheduler_loop(db, once=True, readiness_service=readiness)


def _run_scout(rig: RigEnvironment, *, report_text: str = REPORT_TEXT) -> tuple[str, str]:
    """Submit one scout, dispatch it, and wait for its run to finish."""
    rig.set_worker_mode("commit", report_text=report_text)
    db = rig.db()
    submitted = submit_job(db, repo=rig.repo, goal="Check the retry helper", kind="SCOUT")
    assert submitted.created is True
    assert submitted.job.kind is JobKind.SCOUT
    _tick(db)
    job = get_job(db, submitted.job.job_id)
    assert job is not None and job.current_run_id is not None
    run = rig.wait_for_run(job.current_run_id, timeout=30.0)
    assert run.status == RunStatus.COMPLETED, run.result_summary
    _tick(db)
    return submitted.job.job_id, run.id


def _scout_report(db: Database, job_id: str) -> dict[str, Any]:
    from saberops.scout import latest_report

    report = latest_report(db, job_id)
    assert report is not None
    return report


def test_scout_job_finishes_with_a_report_and_no_commit(rig: RigEnvironment) -> None:
    """Read-only scout: report saved in the job folder, no commit, no accept."""
    db = rig.db()
    job_id, run_id = _run_scout(rig)

    job = get_job(db, job_id)
    assert job is not None
    # A scout enforces read-only itself: the caller did not have to ask.
    assert job.requirements.read_only is True
    assert "read_only" in job.requirements.required_capabilities
    assert job.state is JobState.SUCCEEDED

    assert db.get_run(run_id) is not None
    attempts = db.get_attempts_for_run(run_id)
    assert attempts and attempts[-1].commit_sha is None
    # Enforced read-only routing picked the provider whose CLI supports it.
    assert attempts[-1].provider == "codex"
    assert _git(rig.repo, "rev-parse", "HEAD") == rig.base_sha
    assert not (rig.repo / "fake_worker_output.txt").exists()
    assert db.get_reviews_for_run(run_id) == []

    report = _scout_report(db, job_id)
    path = Path(str(report["report_path"]))
    assert path.is_file()
    assert path.parent == db.db_path.parent / "jobs" / run_id
    body = path.read_text(encoding="utf-8")
    assert "The retry helper is safe to reuse" in body
    assert report["summary"]
    assert any("app.py:greet" in finding for finding in report["findings"])
    assert report["violations"] == []
    assert _run_event_kinds(db, run_id, "run_completed")


def _run_event_kinds(db: Database, run_id: str, kind: str) -> list[str]:
    return [event.event_type for event in db.get_events_by_type(run_id, kind)]


def test_scout_attempted_edit_is_discarded_and_recorded(rig: RigEnvironment) -> None:
    """A write during a read-only scout is refused, discarded and recorded."""
    rig.set_worker_mode("commit", ignore_read_only=True, report_text=REPORT_TEXT)
    db = rig.db()
    submitted = submit_job(db, repo=rig.repo, goal="Check the retry helper", kind="SCOUT")
    _tick(db)
    job = get_job(db, submitted.job.job_id)
    assert job is not None and job.current_run_id is not None
    run = rig.wait_for_run(job.current_run_id, timeout=30.0)
    assert run.status == RunStatus.COMPLETED, run.result_summary
    _tick(db)

    # No commit reached the repository, and the scratch copy was reset.
    assert _git(rig.repo, "rev-parse", "HEAD") == rig.base_sha
    attempts = db.get_attempts_for_run(run.id)
    assert attempts and attempts[-1].commit_sha is None
    assert attempts[-1].worktree_path
    scratch = Path(attempts[-1].worktree_path)
    assert _git(scratch, "status", "--porcelain") == ""

    violations = db.get_events_by_type(run.id, "scout_write_violation")
    assert violations, "the attempted edit must be recorded as a violation"
    payload = violations[-1].payload
    assert payload is not None
    assert "fake_worker_output.txt" in payload["changed_paths"]

    report = _scout_report(db, submitted.job.job_id)
    assert report["violations"], "the job record keeps the violation"


def test_scout_report_decision_becomes_an_owner_question(rig: RigEnvironment) -> None:
    """A report's decisions section is never lost in prose."""
    job_id, run_id = _run_scout(rig, report_text=DECISION_REPORT_TEXT)
    _ = run_id
    queue = OwnerQuestionQueue()
    questions = queue.questions(status=QuestionStatus.OPEN)
    matched = [q for q in questions if q.request.run_id is not None]
    assert matched, "the report's decision must become an owner question"
    question = matched[0]
    assert question.request.decision_class is DecisionClass.OTHER
    assert question.answerable_by.value == "OWNER"
    assert question.request.key.startswith(f"scout:{job_id}:")
    assert "evidence folders" in question.request.subject.lower()

    # Idempotent: a second identical report does not duplicate the question.
    report = _scout_report(rig.db(), job_id)
    from saberops.scout import raise_decision_questions

    again = raise_decision_questions(
        job_id=job_id,
        run_id=str(report["run_id"]),
        report_path=str(report["report_path"]),
        decisions=tuple(str(item) for item in report["decisions"]),
    )
    assert list(again) == [question.question_id]


def test_catch_up_and_wait_job_show_the_report(rig: RigEnvironment) -> None:
    """A fresh session retrieves the scout report from saved records."""
    job_id, _run_id = _run_scout(rig)
    db = rig.db()
    context = ServerContext(
        db_path=db.db_path,
        questions_store=owner_state_dir() / "questions.json",
    )
    registry = build_default_registry(context)

    caught = registry.call("catch_up", {"job_id": job_id})
    report = caught["report"]
    assert report and Path(str(report["report_path"])).is_file()
    assert report["summary"]
    assert caught["next_action"]["action"] == "read_scout_report"
    assert caught["next_action"]["promote_command"] == f"orch promote-scout {job_id}"

    waited = registry.call("wait_job", {"job_id": job_id, "timeout_seconds": 0})
    assert waited["outcome"] == "final"
    assert waited["job"]["report"]["report_path"] == report["report_path"]

    fetched = registry.call("get_job", {"job_id": job_id})
    assert fetched["job"]["report"]["summary"] == report["summary"]
    assert fetched["result"]["candidate_sha"] is None
    assert fetched["result"]["owner_action"] is None


def test_promotion_starts_a_build_job_carrying_the_report(rig: RigEnvironment) -> None:
    """Promotion copies the report into a new build job and records lineage."""
    job_id, _run_id = _run_scout(rig)
    db = rig.db()
    result = promote_scout(
        db,
        scout_job_id=job_id,
        goal="Apply the recommended reuse",
        idempotency_key="promote-once",
    )
    assert result.created is True
    promoted = result.job
    assert promoted.kind is JobKind.BUILD
    assert promoted.requirements.read_only is False
    assert "Apply the recommended reuse" in promoted.goal
    assert f"[SCOUT REPORT from job {job_id}]" in promoted.goal
    assert "The retry helper is safe to reuse" in promoted.goal

    from saberops.jobs import store

    new_events = store.list_events(db, promoted.job_id)
    lineage = [e for e in new_events if e["kind"] == "promoted_from_scout"]
    assert lineage and json.loads(lineage[-1]["payload_json"])["scout_job_id"] == job_id
    old_events = store.list_events(db, job_id)
    assert any(e["kind"] == "scout_promoted" for e in old_events)

    replay = promote_scout(
        db,
        scout_job_id=job_id,
        goal="Apply the recommended reuse",
        idempotency_key="promote-once",
    )
    assert replay.created is False and replay.job.job_id == promoted.job_id


def test_a_succeeded_scout_satisfies_its_dependants(rig: RigEnvironment) -> None:
    """A scout has no candidate to accept, so completion unblocks dependants."""
    db = rig.db()
    scout_id, _run_id = _run_scout(rig)
    rig.set_worker_mode("commit", report_text=REPORT_TEXT)
    dependant = submit_job(
        db,
        repo=rig.repo,
        goal="Use the scout findings",
        depends_on=(scout_id,),
    )
    assert dependant.created is True
    scout = get_job(db, scout_id)
    assert scout is not None
    blockers = dependency_blockers(db, [dependant.job])
    assert blockers == {}, "a completed scout must not leave a dependant parked"


def test_mcp_submit_job_accepts_the_scout_kind(rig: RigEnvironment) -> None:
    """The MCP surface exposes the job kind and enforces read-only at submit."""
    db = rig.db()
    lease = OrchestratorLease(rig.state_home / "saberops" / "scout-lease.json", holder="f19")
    lease.acquire()
    context = ServerContext(
        db_path=db.db_path,
        questions_store=owner_state_dir() / "questions.json",
        lease=lease,
    )
    registry = build_default_registry(context)
    submitted = registry.call(
        "submit_job",
        {"repo": str(rig.repo), "goal": "Investigate the greeting", "kind": "SCOUT"},
    )
    assert submitted["job"]["kind"] == "SCOUT"
    job = get_job(db, submitted["job_id"])
    assert job is not None and job.requirements.read_only is True
    assert "read_only" in job.requirements.required_capabilities


def test_plan_lane_kind_scout_unblocks_its_dependant_with_the_report(
    rig: RigEnvironment,
) -> None:
    """A scout lane's report is attached to the lanes that depend on it."""
    from saberops.capacity.loop import run_scheduler_loop
    from saberops.mcp.lease import OrchestratorLease
    from saberops.plan_runner import get_plan, project_plan, submit_plan_file

    lease = OrchestratorLease()
    assert lease.acquire() is True
    try:
        rig.set_worker_mode("commit", report_text=REPORT_TEXT)
        plan_text = "\n".join(
            [
                "# SaberOps plan: f19-rig",
                "",
                "```plan",
                json.dumps({"format": "saberops-plan/1", "plan_id": "f19-rig", "base": "main"}),
                "```",
                "",
                "## Wave 0",
                "",
                "### A. Inspect the helper",
                "",
                "- **Why:** the dependant needs the answer.",
                "- **Do:** inspect the retry helper.",
                "- **Out of scope:** none",
                "- **Done when:**",
                "  - a report exists",
                "- **Needs:** none",
                "",
                "```plan",
                json.dumps(
                    {
                        "id": "A",
                        "wave": "0",
                        "depends_on": [],
                        "run": "job",
                        "kind": "scout",
                    },
                    sort_keys=True,
                ),
                "```",
                "",
                "### B. Use the findings",
                "",
                "- **Why:** apply what the scout learned.",
                "- **Do:** apply the recommendation.",
                "- **Out of scope:** none",
                "- **Done when:**",
                "  - the recommendation is applied",
                "- **Needs:** A",
                "",
                "```plan",
                json.dumps(
                    {
                        "id": "B",
                        "wave": "0",
                        "depends_on": ["A"],
                        "run": "job",
                        "size": "S",
                        "effort": "low",
                    },
                    sort_keys=True,
                ),
                "```",
                "",
            ]
        )
        plan_path = rig.repo / "PLAN.md"
        plan_path.write_text(plan_text, encoding="utf-8")
        _git(rig.repo, "add", "PLAN.md")
        _git(rig.repo, "commit", "-m", "plan: f19 rig")
        db = rig.db()
        submission = submit_plan_file(
            db, repo=rig.repo, plan_path=plan_path, owner_initiated=True
        )
        assert submission.submitted == ("A",)
        plan = get_plan(db, "f19-rig")
        assert plan is not None
        view = project_plan(db, plan).view("A")
        assert view is not None and view.job_id is not None
        scout = get_job(db, view.job_id)
        assert scout is not None and scout.kind is JobKind.SCOUT

        tick = run_scheduler_loop(
            db,
            once=True,
            readiness_service=ReadinessService.for_owner_state(owner_state_dir()),
        )
        assert tick is not None and tick.ran
        scout = get_job(db, scout.job_id)
        assert scout is not None and scout.current_run_id is not None
        run = rig.wait_for_run(scout.current_run_id, timeout=30.0)
        assert run.status == RunStatus.COMPLETED, run.result_summary

        # The next tick reconciles the scout job and submits the dependant.
        tick = run_scheduler_loop(
            db,
            once=True,
            readiness_service=ReadinessService.for_owner_state(owner_state_dir()),
            run_launcher=lambda job, decision, run_id: run_id,
        )
        assert tick is not None and tick.ran
        plan = get_plan(db, "f19-rig")
        assert plan is not None
        dependant_view = project_plan(db, plan).view("B")
        assert dependant_view is not None and dependant_view.job_id is not None
        dependant = get_job(db, dependant_view.job_id)
        assert dependant is not None and dependant.kind is JobKind.BUILD
        assert "The retry helper is safe to reuse" in dependant.goal
        assert f"[SCOUT REPORT from lane A (job {scout.job_id})]" in dependant.goal

        with db.owner_db._get_connection() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM plan_events WHERE plan_id = ?"
                " AND lane_id = 'B' AND kind = 'plan_lane_submitted'",
                ("f19-rig",),
            ).fetchall()
        assert rows
        payload = json.loads(str(rows[-1]["payload_json"]))
        assert payload["kind"] == "BUILD"
        assert payload["scout_reports"] == [
            {
                "lane_id": "A",
                "job_id": scout.job_id,
                "report_path": payload["scout_reports"][0]["report_path"],
                "report_digest": payload["scout_reports"][0]["report_digest"],
            }
        ]
    finally:
        lease.release()


def test_plan_format_accepts_kind_scout_and_keeps_old_hashes() -> None:
    """The plan format carries `kind`, and a pre-F19 revision still verifies."""
    from saberops.plan_file import parse_plan_file, validate_plan
    from saberops.project_scope.plan_store import (
        PlanRevision,
        PlanTaskDef,
        compute_revision_hash,
    )

    text = "\n".join(
        [
            "# SaberOps plan: kinds",
            "",
            "```plan",
            json.dumps({"format": "saberops-plan/1", "plan_id": "kinds"}),
            "```",
            "",
            "### S. Scout lane",
            "",
            "- **Why:** answer a question.",
            "- **Do:** investigate.",
            "- **Out of scope:** none",
            "- **Done when:**",
            "  - a report exists",
            "- **Needs:** none",
            "",
            "```plan",
            json.dumps({"id": "S", "depends_on": [], "run": "job", "kind": "scout"}),
            "```",
            "",
        ]
    )
    parsed = parse_plan_file(text)
    validate_plan(parsed)
    assert parsed.lanes[0].kind == "scout"
    assert parsed.to_tasks()[0].kind == "scout"
    assert parsed.to_tasks()[0].as_dict()["kind"] == "scout"

    # A format-2 revision stored before F19 hashed no `kind` key; the default
    # job task must produce exactly that hash again.
    created = "2026-11-19T00:00:00+00:00"
    legacy_task = PlanTaskDef(task_id="A", title="Legacy")
    assert "kind" not in legacy_task.as_dict()
    legacy_hash = compute_revision_hash(
        "v1", "p", (legacy_task,), created, format_version=2, plan_id="p", base="main"
    )
    revision = PlanRevision(
        version="v1",
        project_id="p",
        tasks=(legacy_task,),
        created_at=created,
        sha256=legacy_hash,
        plan_id="p",
        base="main",
        format_version=2,
    )
    assert revision.verify() is True


def test_empty_report_is_refused_and_a_report_indexes_its_sections(tmp_path: Path) -> None:
    """The completion rule is "non-empty report"; parsing is deterministic."""
    with pytest.raises(ValueError):
        write_report(
            tmp_path,
            run_id="run-1",
            job_id="job-1",
            goal="ask",
            provider="codex",
            model="m",
            body="   \n\n ",
        )
    parsed = parse_report(REPORT_TEXT)
    assert parsed["summary"].startswith("The retry helper")
    assert len(parsed["findings"]) == 2
    parsed_decisions = parse_report(DECISION_REPORT_TEXT)
    assert parsed_decisions["decisions"] == (
        "Keep per-project evidence folders or move them under one shared root?",
    )
    assert SCOUT_REPORT_EVENT == "scout_report"
