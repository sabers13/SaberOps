"""Wave 3 feature drills for the phase 1 benchmark rig (lane B2b).

Every drill runs in fake mode: the lifecycle rig's fake worker and reviewer, an
isolated store and a toy repository.  No vendor CLI is started and no network is
used.  Each drill returns a :class:`~benchmark_core.DrillResult` so it can be
printed in the benchmark results table.

The drills exercise the Wave 3 features the way the owner meets them -- mode
decisions, plan files, quota failover, steering, cleanup, auto-accept, nested
workers, pause, scouts, proxies, the walkthrough and advisor checkpoints -- and
assert the durable records behind each one.  They never weaken billing consent,
owner acceptance, independent review or isolation.
"""

from __future__ import annotations

import io
import json
import os
import shlex
import stat
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from benchmark_core import (
    QUESTION_TASK,
    DrillResult,
    SubscriptionUsage,
    aggregate_subscription_usage,
)
from benchmark_rig import (
    DRILL_VISIBLE_LIMIT_SECONDS,
    SETUP_TIMEOUT_SECONDS,
    _wait_for,
)
from lifecycle_rig import (
    AcceptanceFixture,
    RigEnvironment,
    _install_shim,
    build_acceptance_fixture,
    create_toy_repo,
)

from saberops import away, vendor_health
from saberops.accept import AcceptEngine
from saberops.advisor import set_advisor_route
from saberops.agent_connect import PROXIES, PROXY_FAMILY, PROXY_TOOLS, agent_document
from saberops.auto_accept import (
    AUTO_ACCEPT_EVENT,
    auto_accept_candidate,
    evaluate_auto_accept,
    undo_auto_accept,
)
from saberops.capacity.loop import run_scheduler_loop
from saberops.cli import main as cli_main
from saberops.db import Database, current_iso_timestamp
from saberops.git import GitManager
from saberops.jobs import store
from saberops.jobs.models import (
    RUN_FAILED,
    JobKind,
    JobRequirements,
    JobState,
    WaitingReason,
)
from saberops.jobs.service import (
    get_job,
    job_status_view,
    note_job,
    promote_scout,
    steering_notes,
    submit_job,
)
from saberops.mcp.errors import ForbiddenOperationError
from saberops.mcp.evidence import EvidenceRoots
from saberops.mcp.lease import OrchestratorLease
from saberops.mcp.ledger import EventLedger
from saberops.mcp.tools import ServerContext, build_default_registry
from saberops.model_access.readiness_service import ReadinessService
from saberops.models import (
    Attempt,
    AttemptStatus,
    DispatchEvidence,
    DispatchReason,
    DispatchRole,
    DispatchStage,
    ReviewResult,
    ReviewVerdict,
    Run,
    RunStatus,
    Tier,
)
from saberops.native_workers import claim_native_job, record_native_attempt
from saberops.nudge import NUDGE_FLAG_ENV, NUDGE_NOTE_BODY
from saberops.orchestrator import Orchestrator
from saberops.owner_mode import (
    ModeAction,
    OwnerMode,
    clear_owner_mode,
    decide_owner_action,
    mode_view,
    set_owner_mode,
)
from saberops.paths import owner_state_dir
from saberops.pause_work import resume_work
from saberops.plan_runner import get_plan, project_plan, submit_plan_file
from saberops.plan_state import plan_state_path, project_plan_state
from saberops.questions import (
    AskRole,
    DecisionClass,
    Escalation,
    OwnerQuestionQueue,
    QuestionRequest,
    QuestionStatus,
)
from saberops.runtime_gate import (
    PauseReason,
    clear_pause,
    evaluate_gate,
    mark_presence_known,
    refresh_gate,
    set_pause,
)
from saberops.scout import latest_report
from saberops.storage_gc import retire_landed_worktrees
from saberops.vendor_cli_fixtures import (
    HELP_COMMANDS,
    Fixture,
    Manifest,
    VersionSupport,
    check_version_support,
    run_check,
)
from saberops.walkthrough import build_walkthrough, intro_text, store_needs_introduction

FUTURE = "2999-01-01T00:00:00+00:00"


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------


def _feature_drill(
    name: str,
    group: str,
    *,
    started: float,
    passed: bool,
    final_state: str,
    reason: str = "",
    notices: int = 0,
    detail: str = "",
    slow: bool = True,
) -> DrillResult:
    """Build one drill result; a failed assertion has already aborted the drill."""
    assert passed, f"{name}: {detail or reason}"
    return DrillResult(
        name=name,
        passed=True,
        seconds=time.monotonic() - started,
        final_state=final_state,
        reason=reason or detail or "ok",
        notices=notices,
        detail=detail,
        group=group,
        slow=slow,
    )


def _reset_owner_state() -> None:
    """Return the shared owner store to its safe defaults between drills."""
    clear_owner_mode()
    try:
        clear_pause(by="benchmark")
    except Exception:  # noqa: BLE001 - the pause store may predate the shutdown
        pass


def _tick(db: Any, readiness: ReadinessService | None = None) -> Any:
    resolved = readiness or ReadinessService.for_owner_state(owner_state_dir())
    result = run_scheduler_loop(db, once=True, readiness_service=resolved)
    assert result is not None and result.ran is True, result
    return result


def _tick_stub(db: Any) -> Any:
    """One tick with a launcher that records the dispatch and starts nothing."""
    readiness = ReadinessService.for_owner_state(owner_state_dir())
    result = run_scheduler_loop(
        db,
        once=True,
        readiness_service=readiness,
        run_launcher=lambda job, decision, run_id: run_id,
    )
    assert result is not None and result.ran is True, result
    return result


def _required_job(db: Any, job_id: str) -> Any:
    job = get_job(db, job_id)
    assert job is not None, job_id
    return job


def _question_request(
    key: str,
    *,
    decision_class: DecisionClass = DecisionClass.ROUTINE_QUESTION,
    kind: str = "routine",
    blocks: tuple[str, ...] = (),
    escalated: bool = False,
    run_id: str | None = None,
    subject: str | None = None,
) -> QuestionRequest:
    return QuestionRequest(
        key=key,
        subject=subject or f"question {key}",
        kind=kind,
        decision_class=decision_class,
        raised_by_role=AskRole.BUILD,
        raised_by="worker-1",
        escalation=Escalation(
            requirement="the worker needs one decision to continue",
            proposed_expansion="do the wider thing",
            smallest_compliant="do the narrow thing",
            consequences="the job stays parked until it is answered",
            recommendation="do the narrow thing",
        ),
        run_id=run_id,
        blocks=blocks,
        options=("yes", "no"),
        escalated=escalated,
    )


def _planner_context(rig: RigEnvironment, store_path: Path, lease_path: Path) -> ServerContext:
    lease = OrchestratorLease(lease_path, holder="benchmark-planner", pid=os.getpid())
    assert lease.acquire() is True
    evidence_root = rig.root / ".orch"
    evidence_root.mkdir(exist_ok=True)
    return ServerContext(
        db_path=rig.db().db_path,
        questions_store=store_path,
        config_ledger_path=rig.root / "config.jsonl",
        evidence=EvidenceRoots(roots=(evidence_root,)),
        ledger=EventLedger(rig.root / "events.jsonl"),
        lease=lease,
    )


def _write_worker_control(
    rig: RigEnvironment,
    mode: str,
    *,
    prompt_log: Path,
    wait_file: Path,
    exit_code: int | None = None,
) -> None:
    """Drive the fake CLI through its control file so it can change mid-run."""
    rig.monkeypatch.delenv("ORCH_FAKE_WORKER_MODE", raising=False)
    control = rig.state_home / "saberops" / "fake-worker-mode.json"
    control.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "mode": mode,
        "prompt_log": str(prompt_log),
        "wait_file": str(wait_file),
        "wait_timeout_seconds": 45,
    }
    if exit_code is not None:
        payload["exit_code"] = exit_code
    control.write_text(json.dumps(payload), encoding="utf-8")


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )
    return proc.stdout.strip()


def _create_benchmark_repo(rig: RigEnvironment, name: str) -> Path:
    """A toy repository with the rig's gate and an independent review policy."""
    parent = rig.root / "wave3-repos"
    parent.mkdir(parents=True, exist_ok=True)
    repo, _sha = create_toy_repo(parent, name)
    assert cli_main(["project", "set-gate", "--repo", str(repo), rig.gate_command]) == 0
    return repo


# --------------------------------------------------------------------------
# F10 / F11: critical-only and away
# --------------------------------------------------------------------------


def run_question_mode_drills(rig: RigEnvironment) -> tuple[DrillResult, ...]:
    """Critical-only answers routine questions; away parks one branch and reports."""
    drills: list[DrillResult] = []
    try:
        drills.append(_drill_critical_only(rig))
        drills.append(_drill_away(rig))
    finally:
        _reset_owner_state()
    return tuple(drills)


def _drill_critical_only(rig: RigEnvironment) -> DrillResult:
    started = time.monotonic()
    set_owner_mode(OwnerMode.CRITICAL_ONLY)
    store_path = rig.root / "drill-questions-critical.json"
    queue = OwnerQuestionQueue(store_path)
    routine_key = QUESTION_TASK.task_id
    queue.ask(
        _question_request(
            routine_key,
            decision_class=DecisionClass.FIX_UP_CONTENT,
            subject=QUESTION_TASK.goal,
        )
    )
    queue.ask(
        _question_request(
            "q-critical",
            decision_class=DecisionClass.FIX_UP_CONTENT,
            escalated=True,
        )
    )
    context = _planner_context(rig, store_path, rig.root / "drill-lease-critical.json")
    try:
        registry = build_default_registry(context)
        answered = registry.call(
            "answer_question",
            {
                "question_id_or_key": routine_key,
                "answer_text": "take the recommended option",
                "authority": "PLAN",
                "answered_by": "fake-planner",
                "evidence": "the previous gate passed",
                "rejected_alternative": "escalate to the owner instead",
            },
        )
        assert answered["question"]["status"] == "ANSWERED", answered
        assert answered["answer_policy"]["mode"] == "critical-only", answered
        assert answered["answer_policy"]["disposition"] == "planner_may_answer", answered

        with pytest.raises(ForbiddenOperationError):
            registry.call(
                "answer_question",
                {
                    "question_id_or_key": "q-critical",
                    "answer_text": "answer the escalated question",
                    "authority": "PLAN",
                    "answered_by": "fake-planner",
                    "evidence": "none",
                    "rejected_alternative": "ask the owner",
                },
            )
    finally:
        context.lease.release()

    audit = OwnerQuestionQueue(store_path).orchestrator_alone_decisions()
    assert len(audit) == 1, audit
    assert audit[0].words == "take the recommended option"
    critical = OwnerQuestionQueue(store_path).get("q-critical")
    assert critical is not None and critical.status is QuestionStatus.OPEN
    return _feature_drill(
        "critical-only questions",
        "F10",
        started=started,
        passed=True,
        final_state="ANSWERED",
        reason="routine answered and logged; critical held for the owner",
        detail=(
            f"{len(audit)} logged self-answer from catalogue task "
            f"{QUESTION_TASK.task_id}; critical still open"
        ),
    )


def _drill_away(rig: RigEnvironment) -> DrillResult:
    started = time.monotonic()
    set_owner_mode(OwnerMode.AWAY, until=FUTURE)
    away.save_away_policy(
        away.AwayPolicy(
            budget_units=100,
            wall_clock_seconds=3_600,
            worker_cap=5,
            paths=("app.py",),
            set_at=datetime.now(UTC).isoformat(),
            set_by="owner",
        )
    )
    db = rig.db()
    blocked = submit_job(db, repo=rig.repo, goal="away: never-delegable branch").job
    independent = submit_job(db, repo=rig.repo, goal="away: independent branch").job
    OwnerQuestionQueue().ask(
        _question_request(
            "q-push",
            decision_class=DecisionClass.PUSH,
            blocks=(blocked.job_id,),
        )
    )
    rig.set_worker_mode("commit", target_file="away-independent.txt", content="ok\n")

    readiness = ReadinessService.for_owner_state(owner_state_dir())
    tick = _tick(db, readiness)
    assert tick.ran is True
    parked = get_job(db, blocked.job_id)
    assert parked is not None, blocked.job_id
    assert parked.waiting_reason is WaitingReason.AWAY_PARKED, parked.waiting_reason
    running = get_job(db, independent.job_id)
    assert running is not None and running.current_run_id is not None
    run = rig.wait_for_run(running.current_run_id, timeout=60.0)
    assert run.status is RunStatus.COMPLETED, run.result_summary
    _tick(db, readiness)

    summary = away.build_return_summary()
    assert len(summary.parked_branches) == 1, summary.as_dict()
    entry = summary.parked_branches[0]
    assert entry["never_delegable"] == "publishing", entry
    assert summary.has_items is True
    assert "1 parked" in summary.one_line()
    return _feature_drill(
        "away mode",
        "F11",
        started=started,
        passed=True,
        final_state="SUCCEEDED",
        reason="never-delegable branch parked; independent branch finished",
        detail=f"return summary: {summary.one_line()}",
    )


# --------------------------------------------------------------------------
# F8 / F9: plan file, dependency order and a mid-run edit
# --------------------------------------------------------------------------


def _lane_block(lane: Mapping[str, Any]) -> list[str]:
    machine: dict[str, Any] = {
        "id": lane["id"],
        "wave": lane.get("wave", "0"),
        "depends_on": list(lane.get("needs", ())),
        "run": lane.get("run", "job"),
    }
    if machine["run"] == "job":
        machine["size"] = lane.get("size", "S")
        machine["effort"] = lane.get("effort", "low")
    needs = ", ".join(lane.get("needs", ())) or "none"
    return [
        f"### {lane['id']}. {lane.get('title', lane['id'])}",
        "",
        f"- **Why:** {lane.get('why', 'the plan needs this lane')}",
        f"- **Do:** {lane.get('do', 'make the change')}",
        f"- **Out of scope:** {lane.get('out_of_scope', 'none')}",
        "- **Done when:**",
        f"  - {lane.get('done', 'the change is checked')}",
        f"- **Needs:** {needs}",
        "",
        "```plan",
        json.dumps(machine, sort_keys=True),
        "```",
        "",
    ]


def _write_plan(
    repo: Path,
    lanes: Sequence[Mapping[str, Any]],
    *,
    plan_id: str,
    message: str,
) -> Path:
    lines = [
        f"# SaberOps plan: {plan_id}",
        "",
        "```plan",
        json.dumps({"format": "saberops-plan/1", "plan_id": plan_id, "base": "main"}),
        "```",
        "",
        "## Wave 0",
        "",
    ]
    for lane in lanes:
        lines.extend(_lane_block(lane))
    path = repo / "PLAN.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    _git(repo, "add", "PLAN.md")
    _git(repo, "commit", "-m", message)
    return path


def _activate_plan(
    repo: Path,
    lanes: Sequence[Mapping[str, Any]],
    *,
    plan_id: str,
    message: str = "plan: activate",
) -> Any:
    path = _write_plan(repo, lanes, plan_id=plan_id, message=message)
    return submit_plan_file(
        Database(), repo=repo, plan_path=path, owner_initiated=True
    )


def _lane_job(db: Any, plan_id: str, lane_id: str) -> Any:
    plan = get_plan(db, plan_id)
    assert plan is not None, plan_id
    view = project_plan(db, plan).view(lane_id)
    assert view is not None, lane_id
    if view.job_id is None:
        return None
    return store.get_job(db, view.job_id)


def _plan_row(text: str, lane_id: str) -> str:
    for line in text.splitlines():
        if line.startswith(f"| {lane_id} |"):
            return line
    raise AssertionError(f"lane {lane_id} missing from:\n{text}")


def _assert_state_matches_records(rig: RigEnvironment, plan_id: str) -> None:
    """STATE.md is a projection: every row states the durable record's state."""
    plan = get_plan(rig.db(), plan_id)
    assert plan is not None
    for _attempt in range(5):
        projection = project_plan_state(rig.db(), plan)
        text = plan_state_path(plan).read_text(encoding="utf-8")
        mismatched = [
            (lane.lane_id, lane.state)
            for lane in projection.lanes
            if f"| {lane.state} |" not in _plan_row(text, lane.lane_id)
        ]
        if not mismatched:
            return
        # The tick that dispatches a lane writes STATE.md before the dispatch is
        # durable; one more tick (the single writer) settles the projection.
        _tick_stub(rig.db())
    raise AssertionError(f"STATE.md still disagrees with the records: {mismatched}")


def run_plan_drills(rig: RigEnvironment) -> tuple[DrillResult, ...]:
    """A plan's lanes run in dependency order; independent lanes run in parallel."""
    repo = _create_benchmark_repo(rig, "wave3-plan")
    db = rig.db()
    plan_id = "wave3-plan"
    lanes: list[dict[str, Any]] = [
        {"id": "A", "title": "Root lane", "wave": "0"},
        {"id": "B", "title": "Dependent lane", "wave": "1", "needs": ["A"]},
        {"id": "C", "title": "Independent lane", "wave": "0"},
    ]
    started = time.monotonic()
    _activate_plan(repo, lanes, plan_id=plan_id)
    job_a = _lane_job(db, plan_id, "A")
    job_c = _lane_job(db, plan_id, "C")
    assert job_a is not None and job_c is not None
    # A dependent lane has no job yet: it is submitted when its dependency lands.
    assert _lane_job(db, plan_id, "B") is None

    rig.set_worker_mode("commit", target_file="lane-a.txt", content="a\n")
    first = _tick(db)
    # A and C are independent roots: both leave QUEUED in the same tick.
    assert first.dispatched == 2, first.decisions
    running_a = get_job(db, job_a.job_id)
    running_c = get_job(db, job_c.job_id)
    assert running_a is not None and running_a.current_run_id is not None
    assert running_c is not None and running_c.current_run_id is not None
    plan = get_plan(db, plan_id)
    assert plan is not None
    held = project_plan(db, plan).view("B")
    assert held is not None
    assert held.state == "held"
    assert "dependency_pending" in held.reasons

    rig.set_worker_mode("commit", target_file="lane-a.txt", content="a\n")
    run_a = rig.wait_for_run(running_a.current_run_id, timeout=90.0)
    assert run_a.status is RunStatus.COMPLETED, run_a.result_summary
    rig.set_worker_mode("commit", target_file="lane-c.txt", content="c\n")
    run_c = rig.wait_for_run(running_c.current_run_id, timeout=90.0)
    assert run_c.status is RunStatus.COMPLETED, run_c.result_summary
    _tick(db)
    assert _required_job(db, job_a.job_id).state is JobState.SUCCEEDED
    assert _required_job(db, job_c.job_id).state is JobState.SUCCEEDED
    assert _lane_job(db, plan_id, "B") is None
    _assert_state_matches_records(rig, plan_id)
    dependent_row = _plan_row(_state_text(rig, plan_id), "B")
    assert "| held |" in dependent_row, dependent_row

    # The owner accepts A; only then is B submitted and startable.
    accepted = AcceptEngine(db=db).accept_run(run_a.id)
    assert accepted.accepted is True, accepted.summary
    rig.set_worker_mode("commit", target_file="lane-b.txt", content="b\n")
    deadline = time.monotonic() + SETUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        _tick(db)
        job_b = _lane_job(db, plan_id, "B")
        if job_b is not None and job_b.current_run_id is not None:
            break
        time.sleep(0.05)
    else:  # pragma: no cover - only on a real engine stall
        pytest.fail("B was never submitted and dispatched after A was accepted")
    run_b = rig.wait_for_run(job_b.current_run_id, timeout=90.0)
    assert run_b.status is RunStatus.COMPLETED, run_b.result_summary
    _tick(db)
    _assert_state_matches_records(rig, plan_id)
    dependency_drill = _feature_drill(
        "plan dependencies",
        "F8",
        started=started,
        passed=True,
        final_state="SUCCEEDED",
        reason="A and C ran in parallel; B started only after A was accepted",
        detail=(
            f"parallel roots A+C in one tick, then B on run {run_b.id}; "
            "STATE.md matched the records"
        ),
    )

    # Mid-run edit: add an independent lane and a lane that depends on it.
    edit_started = time.monotonic()
    before = get_plan(db, plan_id)
    assert before is not None
    edited = lanes + [
        {"id": "D", "title": "Edited independent lane", "wave": "0"},
        {"id": "E", "title": "Edited dependent lane", "wave": "1", "needs": ["D"]},
    ]
    _activate_plan(repo, edited, plan_id=plan_id, message="plan: revise mid-run")
    after = get_plan(db, plan_id)
    assert after is not None
    assert after.active_version > before.active_version, (
        before.active_version,
        after.active_version,
    )
    # Existing lanes keep their jobs: accepted or running work is never rewritten.
    assert _lane_job(db, plan_id, "A").job_id == job_a.job_id
    assert _lane_job(db, plan_id, "B").job_id == job_b.job_id
    job_d = _lane_job(db, plan_id, "D")
    assert job_d is not None, "an added root lane is submitted immediately"
    assert _lane_job(db, plan_id, "E") is None, "E waits for D to land"
    revision = after.plan_store().get_revision(after.project_id, after.active_version)
    specs = {task.task_id: task for task in revision.tasks}
    assert specs["E"].start_dependencies == ("D",)
    _tick(db)
    _assert_state_matches_records(rig, plan_id)
    assert "| D |" in _state_text(rig, plan_id)
    edit_drill = _feature_drill(
        "plan file edit",
        "F9",
        started=edit_started,
        passed=True,
        final_state="QUEUED",
        reason="mid-run revision added D and E without rewriting A, B or C",
        detail=f"revision {before.active_version} -> {after.active_version}",
    )
    return (dependency_drill, edit_drill)


def _state_text(rig: RigEnvironment, plan_id: str) -> str:
    plan = get_plan(rig.db(), plan_id)
    assert plan is not None
    return plan_state_path(plan).read_text(encoding="utf-8")


def run_landing_queue_drill(rig: RigEnvironment) -> DrillResult:
    """Accepting one sibling marks the other two stale in a deterministic order."""
    started = time.monotonic()
    repo = _create_benchmark_repo(rig, "wave3-landing")
    db = rig.db()
    plan_id = "wave3-landing"
    lanes = [
        {"id": "A", "title": "Lane A", "wave": "0"},
        {"id": "B", "title": "Lane B", "wave": "0"},
        {"id": "C", "title": "Lane C", "wave": "0"},
    ]
    _activate_plan(repo, lanes, plan_id=plan_id)
    runs: dict[str, Run] = {}
    for lane_id in ("A", "B", "C"):
        job = _lane_job(db, plan_id, lane_id)
        assert job is not None
        runs[lane_id] = _run_lane_for_real(
            rig, db, repo, job.job_id, lane_id, f"landing-{lane_id}.txt"
        )
        assert runs[lane_id].base_commit == job.base_sha, (
            lane_id,
            runs[lane_id].base_commit,
            job.base_sha,
        )
    assert len({run.base_commit for run in runs.values()}) == 1

    head = AcceptEngine(db=db).accept_run(runs["A"].id)
    assert head.accepted is True, head.summary
    _tick(db)
    plan = get_plan(db, plan_id)
    assert plan is not None and plan.auto_refresh is False
    text = _state_text(rig, plan_id)
    lanes_state = {lane.lane_id: lane for lane in project_plan_state(db, plan).lanes}
    assert lanes_state["A"].state == "accepted", lanes_state["A"].state
    assert lanes_state["B"].state == "stale", lanes_state["B"].state
    assert lanes_state["C"].state == "stale", lanes_state["C"].state
    assert f"1. B - stale; `orch refresh {runs['B'].id}`" in text, text
    assert f"2. C - stale; `orch refresh {runs['C'].id}`" in text, text
    assert text.index("1. B") < text.index("2. C")
    assert "orch plan-file auto-refresh on --plan wave3-landing" in text
    return _feature_drill(
        "landing queue",
        "F12",
        started=started,
        passed=True,
        final_state="SUCCEEDED",
        reason="accept marked B and C stale in wave-then-id order",
        detail="exact `orch refresh <run>` commands printed; auto-refresh stays off",
    )


def _run_lane_for_real(
    rig: RigEnvironment,
    db: Any,
    repo: Path,
    job_id: str,
    lane_id: str,
    target_file: str,
) -> Run:
    """Run one lane's job through the real supervisor and reconcile it."""
    from saberops.config import RunConfig
    from saberops.run_creation import RunRequest, resolve_effective_run_config
    from saberops.supervisor import RunSupervisor

    rig.set_worker_mode("commit", target_file=target_file, content=f"{lane_id}\n")
    config: RunConfig = resolve_effective_run_config(
        RunRequest(
            task=f"landing lane {lane_id}",
            repo_path=repo,
            provider_override="codex",
            model_override="gpt-5.6-terra",
            max_attempts=1,
            worker_timeout=20,
            gate_timeout=20,
            review_enabled=True,
            review_timeout=20,
        )
    )
    started = RunSupervisor(db=db).start_supervised_run(config)
    run = rig.wait_for_run(started.run_id, timeout=90.0)
    assert run.status is RunStatus.COMPLETED, run.result_summary
    store.mark_dispatched(db, job_id, run.id)
    store.reconcile_transition(db, job_id, to=JobState.SUCCEEDED)
    return run


# --------------------------------------------------------------------------
# F4 / F13 / F14 / F2: scheduling, steering, nudge, usage
# --------------------------------------------------------------------------


def _dispatch_one(rig: RigEnvironment, *, goal: str, **kwargs: Any) -> tuple[str, str]:
    db = rig.db()
    submitted = submit_job(db, repo=rig.repo, goal=goal, **kwargs)
    tick = _tick(db)
    assert tick.dispatched >= 1, tick.decisions
    job = get_job(db, submitted.job.job_id)
    assert job is not None and job.current_run_id is not None
    return job.job_id, job.current_run_id


def run_scheduling_drills(rig: RigEnvironment) -> tuple[DrillResult, ...]:
    """Quota failover, steering once, one nudge, and usage per subscription (F2).

    Quota exhaustion parks that subscription until its reset, so the failover
    drill runs last: every earlier drill here still needs the codex route.
    """
    return (
        _drill_steering_note(rig),
        _drill_nudge_once(rig),
        _drill_usage_per_subscription(rig),
        _drill_quota_failover(rig),
    )


def _drill_quota_failover(rig: RigEnvironment) -> DrillResult:
    """A quota-exhausted codex attempt continues on opencode from its checkpoint."""
    started = time.monotonic()
    db = rig.db()
    rig.set_worker_mode("commit", per_harness={"codex": "partial_quota"})
    job_id, run_id = _dispatch_one(rig, goal="continue after quota exhaustion")
    run = rig.wait_for_run(run_id, timeout=90.0)
    assert run.status is RunStatus.COMPLETED, run.result_summary

    attempts = db.get_attempts_for_run(run.id)
    assert [attempt.provider for attempt in attempts[:2]] == ["codex", "opencode"], (
        [(attempt.provider, attempt.status.value) for attempt in attempts]
    )
    first, second = attempts[0], attempts[1]
    assert first.status is AttemptStatus.FAILED
    assert first.interruption_reason == "QUOTA_EXHAUSTED"
    checkpoint = db.get_worker_checkpoint_for_attempt(run.id, first.id)
    assert checkpoint is not None, "no checkpoint recorded for the exhausted attempt"
    assert checkpoint.checkpoint_sha != rig.base_sha
    assert "fake_worker_output.txt" in checkpoint.changed_paths
    assert second.base_sha == checkpoint.checkpoint_sha
    inherited = Path(second.worktree_path) / "fake_worker_output.txt"
    assert "codex:" in inherited.read_text(encoding="utf-8")

    caught = build_default_registry(
        ServerContext(
            db_path=Path(db.db_path),
            questions_store=rig.state_home / "saberops" / "questions.json",
        )
    ).call("catch_up", {"job_id": job_id})
    switch = caught["switch"]
    assert switch is not None, caught
    assert switch["reason"] == "QUOTA_EXHAUSTED"
    assert switch["from"]["provider"] == "codex"
    assert switch["to"]["provider"] == "opencode"
    assert switch["continued_from_checkpoint"] is True
    assert switch["work_location_preserved"] is True
    accepted = AcceptEngine(db=db).accept_run(run.id)
    assert accepted.accepted is True, accepted.summary
    return _feature_drill(
        "quota failover",
        "F4",
        started=started,
        passed=True,
        final_state="SUCCEEDED",
        reason="codex exhausted quota; opencode continued from the checkpoint",
        detail=f"checkpoint {checkpoint.checkpoint_sha[:8]} preserved in {second.worktree_path}",
    )


def _drill_steering_note(rig: RigEnvironment) -> DrillResult:
    """One steering note reaches exactly one later attempt, never a live prompt."""
    started = time.monotonic()
    marker = "STEERING-MARKER-b2b: reuse the existing retry helper."
    db = rig.db()
    prompt_log = rig.root / "steering-prompts.jsonl"
    release = rig.root / "steering-release"
    submitted = submit_job(
        db,
        repo=rig.repo,
        goal="Add a steering note line",
        requirements=JobRequirements(model_family_exclude=("opencode",)),
    )
    _write_worker_control(rig, "crash", prompt_log=prompt_log, wait_file=release,
                          exit_code=7)
    tick = _tick(db)
    assert tick.dispatched == 1, tick.decisions
    job = get_job(db, submitted.job.job_id)
    assert job is not None and job.current_run_id is not None
    run_id = job.current_run_id

    def attempt_started() -> bool:
        return bool(db.get_attempts_for_run(run_id))

    _wait_for(attempt_started, timeout=SETUP_TIMEOUT_SECONDS, message="first attempt started")
    _wait_for(
        lambda: prompt_log.is_file() and prompt_log.stat().st_size > 0,
        timeout=SETUP_TIMEOUT_SECONDS,
        message="first attempt logged its prompt",
    )
    note = note_job(db, job.job_id, marker)
    assert note.delivered is False

    _write_worker_control(rig, "commit", prompt_log=prompt_log, wait_file=release)
    release.write_text("go", encoding="utf-8")
    run = rig.wait_for_run(run_id, timeout=90.0)
    assert run.status is RunStatus.COMPLETED, run.result_summary

    prompts = [
        json.loads(line)["prompt"]
        for line in prompt_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert sum(prompt.count(marker) for prompt in prompts) == 1, prompts
    assert marker in prompts[-1]
    notes = steering_notes(db, job.job_id)
    assert len(notes) == 1 and notes[0].delivered is True
    assert notes[0].delivered_attempt_id == db.get_attempts_for_run(run_id)[1].id
    caught = build_default_registry(
        ServerContext(db_path=Path(db.db_path))
    ).call("catch_up", {"job_id": job.job_id})
    assert [item["text"] for item in caught["steering_notes"]] == [marker]
    assert caught["steering_notes"][0]["delivered"] is True
    return _feature_drill(
        "steering note",
        "F13",
        started=started,
        passed=True,
        final_state="SUCCEEDED",
        reason="note recorded live and delivered once in the next attempt",
        detail="catch_up reports the same single delivery",
    )


def _live_idle_job(rig: RigEnvironment) -> tuple[Database, str, str, str]:
    """A RUNNING job bound to a RUNNING run with a live supervision record."""
    from saberops.models import (
        HealthState,
        ManagerState,
        SupervisionRecord,
    )

    db = rig.db()
    job = submit_job(db, repo=rig.repo, goal="idle job for the nudge drill").job
    run_id = f"run-wave3-nudge-{job.job_id.rsplit('-', 1)[-1]}"
    now = current_iso_timestamp()
    db.create_run(
        Run(
            id=run_id,
            task="idle job for the nudge drill",
            target_repo=str(rig.repo),
            base_commit=rig.base_sha,
            tier=Tier.T1,
            training_allowed=False,
            gate_command=rig.gate_command,
            status=RunStatus.RUNNING,
            created_at=now,
        )
    )
    store.mark_dispatched(db, job.job_id, run_id)
    attempt_id = f"att-{run_id}"
    db.upsert_supervision(
        SupervisionRecord(
            run_id=run_id,
            supervisor_identity="benchmark-owner",
            supervisor_state="attached",
            worker_pid=None,
            provider="codex",
            model="gpt-5.6-terra",
            started_at=now,
            last_activity_at=now,
            health_state=HealthState.HEALTHY,
            health_reason="monitor_active",
            current_package_id=None,
            current_attempt_id=attempt_id,
            deadline_at=None,
            transcript_path=None,
            wake_condition=None,
            manager_state=ManagerState.ATTACHED,
            updated_at=now,
        )
    )
    return db, job.job_id, run_id, attempt_id


def _drill_nudge_once(rig: RigEnvironment) -> DrillResult:
    """One bounded "continue" reaches a stalled worker; the second stall does not."""
    started = time.monotonic()
    rig.monkeypatch.setenv(NUDGE_FLAG_ENV, "1")
    db, job_id, run_id, attempt_id = _live_idle_job(rig)
    hooks = Orchestrator(db=db)._worker_activity_hooks(
        run_id, attempt_id, "codex", "gpt-5.6-terra"
    )
    _on_stdout, _on_stderr, on_stall, _on_resume = hooks
    on_stall(301.0)
    notes = steering_notes(db, job_id)
    assert [note.body for note in notes] == [NUDGE_NOTE_BODY], notes
    events = db.get_events_by_type(run_id, "job_nudged")
    assert len(events) == 1, events
    job = get_job(db, job_id)
    assert job is not None and job_status_view(db, job)["activity"] == "running"

    on_stall(602.0)
    assert len(steering_notes(db, job_id)) == 1, "a second stall must not nudge again"
    supervision = db.get_supervision(run_id)
    assert supervision is not None
    assert supervision.manager_state == "ATTENTION_REQUIRED", supervision.manager_state
    assert len(db.get_events_by_type(run_id, "job_nudged")) == 1
    # Leave no RUNNING job behind: the drill simulated one without a run process.
    db.update_run(
        run_id,
        RunStatus.FAILED,
        completed_at=current_iso_timestamp(),
        result_summary="benchmark nudge drill closed",
    )
    store.reconcile_transition(
        db, job_id, to=JobState.FAILED, failure_reason=RUN_FAILED
    )
    return _feature_drill(
        "one automatic nudge",
        "F14",
        started=started,
        passed=True,
        final_state="RUNNING",
        reason="first stall nudged once; second stall went to attention",
        detail="job_nudged count stayed 1",
    )


def _drill_usage_per_subscription(rig: RigEnvironment) -> DrillResult:
    """Two attempts on two subscriptions report their own usage rows (F2)."""
    started = time.monotonic()
    db = rig.db()
    job_ids: list[str] = []
    rig.set_worker_mode("commit", target_file="usage-codex.txt", content="codex\n")
    codex_job, codex_run = _dispatch_one(
        rig,
        goal="usage on the codex subscription",
        requirements=JobRequirements(model_family_exclude=("opencode",)),
    )
    assert rig.wait_for_run(codex_run, timeout=90.0).status is RunStatus.COMPLETED
    job_ids.append(codex_job)

    rig.set_worker_mode("commit", target_file="usage-opencode.txt", content="opencode\n")
    opencode_job, opencode_run = _dispatch_one(
        rig,
        goal="usage on the opencode subscription",
        requirements=JobRequirements(model_family_exclude=("codex",)),
    )
    assert rig.wait_for_run(opencode_run, timeout=90.0).status is RunStatus.COMPLETED
    job_ids.append(opencode_job)

    usage = aggregate_subscription_usage(db, job_ids)
    by_subscription = {row.subscription: row for row in usage}
    # B3 D8: the independent reviews are dispatches too, so their calls belong
    # to the reviewer's own subscription instead of disappearing from the table.
    assert set(by_subscription) == {"codex", "opencode", "claude"}, by_subscription
    for subscription in ("codex", "opencode"):
        row = by_subscription[subscription]
        assert row.jobs == 1, row
        assert row.calls > 0, row
        assert row.reported is True, row
    reviewer = by_subscription["claude"]
    assert reviewer.jobs == 2, reviewer
    assert reviewer.calls > 0, reviewer
    # The fake reviewer reports no counters; a counter the provider did not
    # report is never invented.
    assert reviewer.reported is False, reviewer
    assert reviewer.input_tokens is None and reviewer.unknown_calls == reviewer.calls, reviewer
    assert by_subscription["codex"].reported is True, by_subscription["codex"]
    codex_attempts = db.get_attempts_for_run(codex_run)
    assert [attempt.provider for attempt in codex_attempts] == ["codex"]
    return _feature_drill(
        "usage per subscription",
        "F2",
        started=started,
        passed=True,
        final_state="SUCCEEDED",
        reason=(
            "codex and opencode each reported their own calls and tokens; the "
            "reviewer's calls are attributed to its own subscription"
        ),
        detail=", ".join(
            f"{row.subscription}: {row.calls} calls / "
            f"{row.total_tokens if row.total_tokens is not None else 'unknown'} tokens"
            for row in usage
        ),
    )


def usage_rows(rig: RigEnvironment) -> tuple[SubscriptionUsage, ...]:
    """Usage for every job in the isolated store (the drill report section)."""
    jobs = store.list_jobs_tolerant(rig.db(), limit=500)[0]
    return aggregate_subscription_usage(rig.db(), [job.job_id for job in jobs])


# --------------------------------------------------------------------------
# F5 / F18 / F6: vendor guard, self-healing and cleanup
# --------------------------------------------------------------------------


def run_vendor_drills(rig: RigEnvironment, tmp_path: Path) -> tuple[DrillResult, ...]:
    """A CLI version change is caught, healed or parked, and landed copies clean."""
    drills: list[DrillResult] = [
        _drill_update_guard(tmp_path),
        _drill_self_healing(rig),
        _drill_landed_cleanup(tmp_path),
    ]
    _reset_owner_state()
    return tuple(drills)


def _fixture(cli: str = "codex", version: str = "codex-cli 1.0.0") -> Fixture:
    return Fixture(
        vendor=cli,
        cli=cli,
        cli_version=version,
        captured_on="2026-11-19",
        kind="config_help",
        source="real-capture",
        redacted=True,
        file=f"{cli}/x/config_help.txt",
        consumer=f"saberops.workers.{cli}",
        expect={"flags": ["--model", "--format", "exec"]},
    )


def _drill_update_guard(tmp_path: Path) -> DrillResult:
    """A new CLI version missing a pinned flag is caught as UNSUPPORTED."""
    started = time.monotonic()
    manifest = Manifest(fixtures=(_fixture(),), fake_only=())
    outputs = dict.fromkeys(
        HELP_COMMANDS["codex"], (0, "codex help --model --format exec\n", "")
    )

    def run_ok(command: Sequence[str], _timeout: float) -> tuple[int, str, str]:
        return outputs.get(tuple(command[1:]), (0, "help text", ""))

    supported = check_version_support(
        manifest, "codex", "codex-cli 1.1.0", which=lambda _cli: "/usr/bin/codex", run=run_ok
    )
    assert supported.state is VersionSupport.SUPPORTED, supported
    assert supported.commands

    def run_missing(command: Sequence[str], _timeout: float) -> tuple[int, str, str]:
        return 0, "codex help --format exec\n", ""

    unsupported = check_version_support(
        manifest,
        "codex",
        "codex-cli 1.1.0",
        which=lambda _cli: "/usr/bin/codex",
        run=run_missing,
    )
    assert unsupported.state is VersionSupport.UNSUPPORTED, unsupported
    assert unsupported.missing == ("--model",), unsupported.missing

    def run_never(_command: Sequence[str], _timeout: float) -> tuple[int, str, str]:
        raise AssertionError("a foreign version shape must not be probed")

    foreign = check_version_support(
        manifest, "codex", "codex 0.0.0-fake", which=lambda _cli: "/usr/bin/codex",
        run=run_never,
    )
    assert foreign.state is VersionSupport.UNVERIFIED, foreign
    assert "help probe skipped" in foreign.detail

    # The owner command prints DRIFT and the UNSUPPORTED state, and exits 1.
    stub_dir = tmp_path / "guard-bin"
    stub_dir.mkdir()
    log = tmp_path / "guard-argv.log"
    stub = stub_dir / "codex"
    stub.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$GUARD_LOG"\n'
        'if [ "$1" = "--version" ]; then\n'
        '  echo "codex-cli 1.1.0"\n'
        "  exit 0\n"
        "fi\n"
        'echo "codex help without the pinned flag"\n',
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    manifest_dir = tmp_path / "guard-manifest"
    (manifest_dir / "codex" / "x").mkdir(parents=True)
    (manifest_dir / "codex" / "x" / "config_help.txt").write_text(
        "$ codex --help (exit 0)\nstub help\n", encoding="utf-8"
    )
    manifest_path = manifest_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "version": 1,
                "fixtures": [
                    {
                        "vendor": "codex",
                        "cli": "codex",
                        "cli_version": "codex-cli 1.0.0",
                        "captured_on": "2026-11-19",
                        "kind": "config_help",
                        "source": "real-capture",
                        "redacted": True,
                        "file": "codex/x/config_help.txt",
                        "consumer": "saberops.workers.codex:CodexAdapter",
                        "expect": {"flags": ["--model"]},
                    }
                ],
                "fake_only": [],
            }
        ),
        encoding="utf-8",
    )
    old_log = os.environ.get("GUARD_LOG")
    os.environ["GUARD_LOG"] = str(log)
    try:
        stream = io.StringIO()
        exit_code = run_check(manifest_path, which=lambda _cli: str(stub), stream=stream)
    finally:
        if old_log is None:
            os.environ.pop("GUARD_LOG", None)
        else:
            os.environ["GUARD_LOG"] = old_log
    output = stream.getvalue()
    assert exit_code == 1, output
    assert "DRIFT codex" in output, output
    assert "UNSUPPORTED codex" in output, output
    assert "--model" in output, output
    return _feature_drill(
        "CLI update guard",
        "F5",
        started=started,
        passed=True,
        final_state="UNSUPPORTED",
        reason="a version change without the pinned flag is caught",
        detail="check exits 1 with DRIFT and UNSUPPORTED; a foreign shim stays unverified",
    )


def _drill_self_healing(rig: RigEnvironment) -> DrillResult:
    """A version change parks the vendor; a repaired interface resumes it once."""
    started = time.monotonic()
    db = rig.db()
    old_version = "codex-cli 1.0.0"
    new_version = "codex-cli 1.1.0"
    manifest = Manifest(fixtures=(_fixture(version=old_version),), fake_only=())

    notices: list[tuple[str, str, str]] = []
    rig.monkeypatch.setattr(
        "saberops.vendor_health.notify_once",
        lambda _ledger, key, title, body: notices.append((key, title, body)),
    )
    assert vendor_health.detect_changes(
        db, manifest=manifest, installed={"codex": old_version}
    ) == []
    changes = vendor_health.detect_changes(
        db, manifest=manifest, installed={"codex": new_version}
    )
    assert [change.vendor for change in changes] == ["codex"], changes

    def help_run(_command: Sequence[str], _timeout: float) -> tuple[int, str, str]:
        return 0, "Usage: codex exec --format --model\n", ""

    smoke_calls: list[str] = []

    def smoke(vendor: str, model: str | None) -> vendor_health.SmokeResult:
        smoke_calls.append(vendor)
        return vendor_health.SmokeResult(
            passed=True,
            detail="exit=0 status=success usage_rows=1 known_rows=1",
            provider="codex",
            model=model or "gpt-5.6-terra",
            usage_rows=1,
        )

    results = vendor_health.run_checks(
        db,
        vendors=("codex",),
        manifest=manifest,
        which=lambda _cli: "/usr/bin/codex",
        help_run=help_run,
        smoke=smoke,
        work_allowed=lambda: True,
    )
    assert results[0].state is vendor_health.VendorState.VERIFIED, results[0]
    health = vendor_health.load_health(db)["codex"]
    assert health.verified_version == new_version, health
    assert health.repair_job_id is None
    assert vendor_health.blocked_providers(db) == {}
    assert smoke_calls == ["codex"]
    assert any("resumed" in notice[1].lower() for notice in notices), notices
    return _feature_drill(
        "vendor self-healing",
        "F18",
        started=started,
        passed=True,
        final_state="VERIFIED",
        reason="one smoke re-verified the new interface and routing resumed",
        detail="park notice, then one resume notice; no repair job filed",
    )


def _drill_landed_cleanup(tmp_path: Path) -> DrillResult:
    """Landed clean copies are removed; failed and unlanded copies are kept."""
    started = time.monotonic()
    repo = tmp_path / "cleanup-repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "b2b@example.com")
    _git(repo, "config", "user.name", "B2b")
    (repo / "README.md").write_text("# cleanup\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    db = Database(tmp_path / "cleanup.db")
    base = GitManager.get_default_worktree_base(repo)

    def land(run_id: str, name: str, *, merge: bool, record_accept: bool = True) -> Path:
        worktree = base / run_id / "a1"
        GitManager.create_worktree(repo, f"orch/{run_id}/a1", worktree)
        (worktree / "a.txt").write_text(f"{name}\n", encoding="utf-8")
        _git(worktree, "add", "-A")
        _git(worktree, "commit", "-qm", name)
        candidate = _git(worktree, "rev-parse", "HEAD")
        if merge:
            _git(repo, "merge", "--ff-only", f"orch/{run_id}/a1")
        _seed_cleanup_run(
            db,
            repo=repo,
            run_id=run_id,
            worktree=worktree,
            branch=f"orch/{run_id}/a1",
            candidate=candidate,
            accepted=record_accept,
        )
        return worktree

    landed = land("run_landed", "landed", merge=True)
    unlanded = land("run_unlanded", "unlanded", merge=False)
    failed = land("run_failed", "failed", merge=False, record_accept=False)
    report = retire_landed_worktrees(db, repo_root=repo)
    assert str(landed) in report.removed, report.removed
    assert not landed.exists()
    kept = {item.run_id: item for item in report.kept}
    assert "run_unlanded" in kept, kept
    assert kept["run_unlanded"].reason == "candidate-not-landed", kept["run_unlanded"]
    assert kept["run_failed"].reason.startswith("candidate-not-accepted"), (
        kept["run_failed"]
    )
    assert unlanded.exists() and failed.exists()
    return _feature_drill(
        "worktree cleanup",
        "F6",
        started=started,
        passed=True,
        final_state="CLEANED",
        reason="one verified-landed copy removed; unlanded and failed copies kept",
        detail=f"{len(report.removed)} removed, {len(report.kept)} kept with reasons",
    )


def _seed_cleanup_run(
    db: Database,
    *,
    repo: Path,
    run_id: str,
    worktree: Path,
    branch: str,
    candidate: str,
    accepted: bool,
) -> None:
    from saberops.models import CheckResult

    now = current_iso_timestamp()
    db.create_run(
        Run(
            id=run_id,
            task="cleanup drill",
            target_repo=str(repo),
            base_commit=_git(repo, "rev-parse", "HEAD"),
            tier=Tier.T1,
            training_allowed=True,
            gate_command="true",
            status=RunStatus.COMPLETED,
            created_at=now,
            completed_at=now,
        )
    )
    db.create_attempt(
        Attempt(
            id=f"{run_id}-a1",
            run_id=run_id,
            attempt_number=1,
            provider="codex",
            model="gpt-5.6-terra",
            worktree_path=str(worktree),
            branch_name=branch,
            status=AttemptStatus.SUCCESS if accepted else AttemptStatus.FAILED,
            commit_sha=candidate,
            created_at=now,
            completed_at=now,
            base_sha=_git(repo, "rev-parse", "HEAD"),
        )
    )
    db.create_check(
        CheckResult(
            id=f"{run_id}-c1",
            attempt_id=f"{run_id}-a1",
            gate_command="true",
            exit_code=0,
            stdout="ok",
            stderr="",
            passed=True,
            created_at=now,
        )
    )
    if accepted:
        db.record_event(
            run_id,
            "run_accepted",
            payload={
                "candidate_sha": candidate,
                "final_branch": "main",
                "gate_command": "true",
                "provenance": {
                    "candidate_sha": candidate,
                    "run_id": run_id,
                    "attempt_id": f"{run_id}-a1",
                    "attempt_number": 1,
                },
            },
        )


# --------------------------------------------------------------------------
# F1 / F15 / F16: owner modes, auto-accept and nested native attempts
# --------------------------------------------------------------------------


def run_autonomy_drills(rig: RigEnvironment) -> tuple[DrillResult, ...]:
    """Owner modes gate auto-accept; a native attempt lands only after review."""
    fixture = build_acceptance_fixture(rig)
    try:
        drills: list[DrillResult] = [
            _drill_owner_modes(rig, fixture),
            _drill_auto_accept(rig, fixture),
            _drill_native_attempt(rig),
        ]
    finally:
        _reset_owner_state()
    return tuple(drills)


def _record_review_for_b(fixture: AcceptanceFixture, verdict: ReviewVerdict) -> str:
    db = fixture.rig.db()
    review_id = f"rev_{fixture.run_id}_b_{verdict.value.lower()}"
    now = current_iso_timestamp()
    db.create_dispatch_evidence(
        DispatchEvidence(
            id=f"disp_{review_id}",
            run_id=fixture.run_id,
            association_id=review_id,
            review_id=review_id,
            stage=DispatchStage.REVIEW,
            role=DispatchRole.CHECK,
            tier=Tier.T1,
            dispatch_reason=DispatchReason.INDEPENDENT_REVIEW,
            requested_provider="anthropic",
            requested_model="claude-sonnet-4-6",
            candidate_sha=fixture.candidate_b_sha,
            started_at=now,
            completed_at=now,
        )
    )
    db.create_review(
        ReviewResult(
            id=review_id,
            run_id=fixture.run_id,
            provider="anthropic",
            model="claude-sonnet-4-6",
            verdict=verdict,
            summary=f"Candidate B review verdict {verdict.value}.",
            details="Recorded by the Wave 3 benchmark drill.",
            created_at=now,
        )
    )
    db.link_model_calls_to_review(fixture.run_id, [review_id], review_id)
    return review_id


def _drill_owner_modes(rig: RigEnvironment, fixture: AcceptanceFixture) -> DrillResult:
    """Ask-me parks an accept the unattended modes would otherwise allow."""
    started = time.monotonic()
    _record_review_for_b(fixture, ReviewVerdict.PASS)
    set_owner_mode(OwnerMode.ASK_ME)
    view = mode_view()
    assert view["mode"] == "ask-me", view
    assert view["declared_mode"] == "ask-me", view
    decision = decide_owner_action(ModeAction.ACCEPT)
    assert decision.allowed is False and "ask-me" in decision.reason, decision.as_dict()
    parked = evaluate_auto_accept(db=rig.db(), run_id=fixture.run_id)
    assert parked.allowed is False and "ask-me" in parked.reason, parked.as_dict()

    set_owner_mode(OwnerMode.AWAY, until=FUTURE)
    allowed = evaluate_auto_accept(db=rig.db(), run_id=fixture.run_id)
    assert allowed.allowed is True, allowed.as_dict()
    assert _git(fixture.rig.repo, "rev-parse", "HEAD") == fixture.base_sha
    return _feature_drill(
        "owner modes",
        "F1",
        started=started,
        passed=True,
        final_state="ASK-ME",
        reason="ask-me parks the accept that away would allow",
        detail="get_mode and the accept decision agree; no commit moved",
    )


def _drill_auto_accept(rig: RigEnvironment, fixture: AcceptanceFixture) -> DrillResult:
    """An eligible candidate auto-accepts in away; undo restores the base; ask-me never."""
    started = time.monotonic()
    db = rig.db()
    set_owner_mode(OwnerMode.AWAY, until=FUTURE)
    outcome = auto_accept_candidate(db=db, run_id=fixture.run_id)
    assert outcome.applied is True, outcome.reason
    assert _git(fixture.rig.repo, "rev-parse", "HEAD") == fixture.candidate_b_sha
    assert outcome.undo_command == f"orch accept {fixture.run_id} --undo"
    events = db.get_events_by_type(fixture.run_id, AUTO_ACCEPT_EVENT)
    assert len(events) == 1, events
    assert (events[0].payload or {}).get("pushed") is False

    undo = undo_auto_accept(db=db, run_id=fixture.run_id)
    assert undo.undone is True, undo.reason
    assert _git(fixture.rig.repo, "rev-parse", "HEAD") == fixture.base_sha

    # Ask-me never accepts, even with full autonomy selected.
    set_owner_mode(OwnerMode.ASK_ME)
    refused = auto_accept_candidate(db=db, run_id=fixture.run_id)
    assert refused.applied is False, refused.reason
    assert _git(fixture.rig.repo, "rev-parse", "HEAD") == fixture.base_sha

    # A stale candidate (base no longer the head) is refused even in away.
    set_owner_mode(OwnerMode.AWAY, until=FUTURE)
    _git(fixture.rig.repo, "commit", "--allow-empty", "-m", "move the target")
    moved = _git(fixture.rig.repo, "rev-parse", "HEAD")
    stale = evaluate_auto_accept(db=db, run_id=fixture.run_id)
    assert stale.allowed is False, stale.as_dict()
    assert _git(fixture.rig.repo, "rev-parse", "HEAD") == moved
    _git(fixture.rig.repo, "reset", "--hard", fixture.base_sha)
    return _feature_drill(
        "auto-accept",
        "F15",
        started=started,
        passed=True,
        final_state="ACCEPTED",
        reason="eligible candidate accepted and undone; ask-me and stale never accept",
        detail="local commit only (pushed=false); undo restored the base",
    )


def _drill_native_attempt(rig: RigEnvironment) -> DrillResult:
    """A nested native attempt is accepted only after its gate and a fresh review."""
    started = time.monotonic()
    db = rig.db()
    rig.monkeypatch.setenv("ORCH_NATIVE_WORKERS", "1")
    rig.set_reviewer_verdict("PASS")

    def claim_and_commit(job_id: str, name: str) -> tuple[dict[str, Any], str]:
        claim = claim_native_job(
            db, job_id, vendor="codex", model="codex-native-test-model"
        )
        worktree = Path(claim["worktree"])
        (worktree / name).write_text("native\n", encoding="utf-8")
        _git(worktree, "add", name)
        _git(worktree, "commit", "-m", "native change")
        return claim, _git(worktree, "rev-parse", "HEAD")

    # 1. A review record that is not candidate-linked never makes it eligible.
    unlinked_job = submit_job(
        db, repo=rig.repo, goal="native attempt without a linked review"
    ).job
    claim, candidate = claim_and_commit(unlinked_job.job_id, "native-unlinked.txt")

    class _Review:
        id = "rev_native_b2b"
        provider = "anthropic"
        model = "claude-sonnet-4-6"
        verdict = ReviewVerdict.PASS
        summary = "native review"

    result = record_native_attempt(
        db,
        unlinked_job.job_id,
        vendor=claim["vendor"],
        model=claim["model"],
        base_sha=claim["base_sha"],
        candidate_sha=candidate,
        worktree=claim["worktree"],
        transcript="native transcript",
        reviewer=lambda _db, _run_id, _timeout: _Review(),
    )
    assert result["state"] == "SUCCEEDED", result
    assert result["owner_action"] == f"orch accept {claim['run_id']}"
    unlinked = AcceptEngine(db).check_accept_eligibility(claim["run_id"])
    assert unlinked.eligible is False, unlinked.as_dict()

    # 2. The real independent reviewer records a fresh review of this SHA.
    linked_job = submit_job(
        db, repo=rig.repo, goal="native attempt with a fresh review"
    ).job
    linked_claim, linked_candidate = claim_and_commit(
        linked_job.job_id, "native-linked.txt"
    )
    linked = record_native_attempt(
        db,
        linked_job.job_id,
        vendor=linked_claim["vendor"],
        model=linked_claim["model"],
        base_sha=linked_claim["base_sha"],
        candidate_sha=linked_candidate,
        worktree=linked_claim["worktree"],
        transcript="native transcript",
    )
    assert linked["state"] == "SUCCEEDED", linked
    assert linked["review"]["provider"] == "anthropic", linked["review"]
    eligibility = AcceptEngine(db).check_accept_eligibility(linked_claim["run_id"])
    assert eligibility.eligible is True, eligibility.as_dict()
    assert _git(rig.repo, "rev-parse", "HEAD") == rig.base_sha
    return _feature_drill(
        "imported native attempt",
        "F16",
        started=started,
        passed=True,
        final_state="SUCCEEDED",
        reason="gate passed and a fresh independent review of its SHA made it eligible",
        detail=(
            f"candidate {linked_candidate[:8]} waits for owner acceptance; "
            "an unlinked review stayed ineligible"
        ),
    )


# --------------------------------------------------------------------------
# F17 / F24: pause and presence
# --------------------------------------------------------------------------


def run_presence_drills(rig: RigEnvironment) -> tuple[DrillResult, ...]:
    """Closing the planning session or the terminal pauses work until confirmed."""
    drills: list[DrillResult] = [_drill_pause_and_resume(rig), _drill_park_running(rig)]
    _reset_owner_state()
    return tuple(drills)


def _live_session(rig: RigEnvironment) -> OrchestratorLease:
    lease = OrchestratorLease()
    lease.configure_database(Path(rig.db().db_path))
    assert lease.acquire() is True
    lease.touch()
    mark_presence_known()
    rig.monkeypatch.setattr(
        "saberops.notifications.notify_once", lambda *args, **kwargs: None, raising=False
    )
    return lease


def _drill_pause_and_resume(rig: RigEnvironment) -> DrillResult:
    """A closed session pauses dispatch; nothing resumes without one confirmation."""
    started = time.monotonic()
    db = rig.db()
    assert evaluate_gate(db=db).closed is True
    lease = _live_session(rig)
    assert evaluate_gate(db=db).closed is False
    lease.release()
    refresh_gate(db)
    assert evaluate_gate(db=db).closed is True
    _reset_owner_state()
    lease = _live_session(rig)
    submitted = submit_job(db, repo=rig.repo, goal="pause until confirmed").job
    lease.release()
    tick = _tick(db)
    assert tick.reason == "paused", tick
    assert _required_job(db, submitted.job_id).state is JobState.QUEUED

    returning = _live_session(rig)
    _tick(db)
    assert _required_job(db, submitted.job_id).state is JobState.QUEUED
    assert evaluate_gate(db=db).closed is True

    rig.set_worker_mode("commit", target_file="resumed.txt", content="ok\n")
    resumed = resume_work(db, by="owner")
    assert resumed.closed is False

    def redispatched() -> bool:
        _tick(db)
        current = get_job(db, submitted.job_id)
        assert current is not None
        return bool(current.current_run_id)

    _wait_for(redispatched, timeout=SETUP_TIMEOUT_SECONDS,
              message="confirmed resume dispatched the queued job")
    current = get_job(db, submitted.job_id)
    assert current is not None and current.current_run_id is not None
    run = rig.wait_for_run(current.current_run_id, timeout=90.0)
    assert run.status is RunStatus.COMPLETED, run.result_summary
    returning.release()
    return _feature_drill(
        "pause and confirm",
        "F17/F24",
        started=started,
        passed=True,
        final_state="SUCCEEDED",
        reason="a closed session paused dispatch; a returning session alone did not resume",
        detail="one explicit confirmation resumed and the job finished",
    )


def _drill_park_running(rig: RigEnvironment) -> DrillResult:
    """An owner pause parks a running worker with its work preserved."""
    started = time.monotonic()
    db = rig.db()
    lease = _live_session(rig)
    rig.set_worker_mode("hang", hang_seconds=180)
    submitted = submit_job(db, repo=rig.repo, goal="park the running worker").job

    def dispatched() -> bool:
        _tick(db)
        current = get_job(db, submitted.job_id)
        return bool(current and current.current_run_id)

    _wait_for(dispatched, timeout=SETUP_TIMEOUT_SECONDS, message="job dispatched")
    job = get_job(db, submitted.job_id)
    assert job is not None and job.current_run_id is not None
    run_id = job.current_run_id

    def worker_started() -> bool:
        from saberops.supervisor import RunSupervisor

        owner, _ = RunSupervisor(db=db).owner_evidence(run_id)
        sup = db.get_supervision(run_id)
        return bool(owner and sup and sup.worker_pid and sup.worker_identity)

    _wait_for(worker_started, timeout=SETUP_TIMEOUT_SECONDS, message="worker started")
    attempt = db.get_attempts_for_run(run_id)[-1]
    worktree = Path(attempt.worktree_path)
    marker = worktree / "preserved-b2b.txt"
    marker.write_text("preserved\n", encoding="utf-8")

    set_pause(PauseReason.NO_PLANNING_SESSION, "the planning session went away")
    tick = _tick(db)
    assert tick.reason == "paused", tick

    def parked() -> bool:
        _tick(db)
        current = get_job(db, submitted.job_id)
        return current is not None and current.state is JobState.QUEUED

    _wait_for(parked, timeout=SETUP_TIMEOUT_SECONDS, message="job parked")
    parked_job = get_job(db, submitted.job_id)
    assert parked_job is not None
    assert parked_job.waiting_reason is WaitingReason.PAUSED
    assert parked_job.resume_from is not None
    parked_run = db.get_run(run_id)
    assert parked_run is not None and parked_run.status is RunStatus.PREEMPTED
    assert marker.read_text(encoding="utf-8") == "preserved\n"
    assert db.get_worker_checkpoints_for_run(run_id)
    lease.release()
    return _feature_drill(
        "park running work",
        "F17/F24",
        started=started,
        passed=True,
        final_state="QUEUED",
        reason="the pause preempted the run and kept its work and checkpoint",
        detail=f"resume_from run {parked_job.resume_from.run_id}",
    )


# --------------------------------------------------------------------------
# F19 / F27 / F20 / F21: scout jobs, cheap route and proxy routing
# --------------------------------------------------------------------------


SCOUT_REPORT = """\
## Summary
The retry helper is safe to reuse.

## Findings
- It has one caller and no side effects.
"""


def run_scout_proxy_drills(rig: RigEnvironment) -> tuple[DrillResult, ...]:
    """A cheap read-only scout reports; a family token routes to its subscription."""
    drills = [_drill_scout(rig), _drill_proxy_family(rig)]
    _reset_owner_state()
    return tuple(drills)


def _drill_scout(rig: RigEnvironment) -> DrillResult:
    """A default scout runs on the cheapest codex model, reports, and never commits."""
    started = time.monotonic()
    from saberops.routing_config import (
        add_candidate_to_chain,
        load_effective_routing_payload,
        save_user_routing,
    )

    payload = load_effective_routing_payload()
    payload = add_candidate_to_chain(payload, role="WORKER", candidate_id="codex/gpt-5.6-luna")
    save_user_routing(payload)
    rig.set_worker_mode("commit", report_text=SCOUT_REPORT)
    db = rig.db()
    submitted = submit_job(
        db, repo=rig.repo, goal="Check whether the helper is reusable", kind=JobKind.SCOUT
    )
    assert submitted.created is True
    assert submitted.job.kind is JobKind.SCOUT
    tick = _tick(db)
    assert tick.dispatched == 1, tick.decisions
    job = get_job(db, submitted.job.job_id)
    assert job is not None and job.current_run_id is not None
    run = rig.wait_for_run(job.current_run_id, timeout=60.0)
    assert run.status is RunStatus.COMPLETED, run.result_summary
    _tick(db)

    job = get_job(db, submitted.job.job_id)
    assert job is not None
    assert job.state is JobState.SUCCEEDED
    assert job.requirements.read_only is True
    attempts = db.get_attempts_for_run(run.id)
    assert attempts and attempts[-1].provider == "codex"
    assert attempts[-1].model == "gpt-5.6-luna", attempts[-1].model
    assert attempts[-1].commit_sha is None
    assert _git(rig.repo, "rev-parse", "HEAD") == rig.base_sha
    assert db.get_reviews_for_run(run.id) == []
    report = latest_report(db, job.job_id)
    assert report is not None
    assert Path(str(report["report_path"])).is_file()
    assert "safe to reuse" in Path(str(report["report_path"])).read_text(encoding="utf-8")

    promoted = promote_scout(db, scout_job_id=job.job_id, caller_id="owner")
    assert promoted.created is True
    assert promoted.job.kind is JobKind.BUILD
    assert "safe to reuse" in promoted.job.goal
    return _feature_drill(
        "scout report",
        "F19/F27",
        started=started,
        passed=True,
        final_state="SUCCEEDED",
        reason="cheap read-only scout reported and committed nothing",
        detail=f"codex/gpt-5.6-luna; promotion started build job {promoted.job.job_id}",
    )


def _drill_proxy_family(rig: RigEnvironment) -> DrillResult:
    """Each proxy carries its own family token and the token routes that family."""
    started = time.monotonic()
    families: dict[str, str] = {}
    for slug, proxies in PROXIES.items():
        for proxy in proxies:
            document = agent_document(slug, proxy.name)
            assert proxy.family in document, (slug, proxy.name)
            assert "model_family_include" in document, (slug, proxy.name)
            for tool in PROXY_TOOLS:
                assert tool in document, (slug, proxy.name, tool)
            families.setdefault(proxy.name, proxy.family)
    assert families == PROXY_FAMILY, families
    # No proxy is generated for a host's own subscription.
    own = {slug: {proxy.name for proxy in proxies} for slug, proxies in PROXIES.items()}
    assert "claude-worker" not in own["claude"]
    assert "codex-worker" not in own["codex"]
    assert "opencode-go-worker" not in own["opencode"]

    # A family token routes the submitted job to that subscription.  The rig has
    # codex and opencode workers; anthropic is the reviewer binding.
    db = rig.db()
    expectations = (("codex", "codex"), ("opencode-go", "opencode"))
    for family, expected_provider in expectations:
        rig.set_worker_mode("commit", target_file=f"proxy-{family}.txt", content="ok\n")
        submitted = submit_job(
            db,
            repo=rig.repo,
            goal=f"run this on the {family} family",
            requirements=JobRequirements(model_family_include=(family,)),
        )
        assert submitted.created is True, family
        tick = _tick(db)
        assert tick.dispatched >= 1, (family, tick.decisions)
        job = get_job(db, submitted.job.job_id)
        assert job is not None and job.current_run_id is not None, family
        run_id = job.current_run_id

        def attempt_started(run: str = run_id) -> bool:
            return bool(db.get_attempts_for_run(run))

        _wait_for(
            attempt_started,
            timeout=SETUP_TIMEOUT_SECONDS,
            message=f"{family} attempt started",
        )
        attempts = db.get_attempts_for_run(run_id)
        assert attempts and attempts[-1].provider == expected_provider, [
            attempt.provider for attempt in attempts
        ]
        rig.wait_for_run(run_id, timeout=90.0)
    return _feature_drill(
        "proxy family routing",
        "F20/F21",
        started=started,
        passed=True,
        final_state="SUCCEEDED",
        reason="three proxies carry exactly their family token; tokens routed jobs",
        detail="codex -> codex and opencode-go -> opencode",
    )


# --------------------------------------------------------------------------
# F25 / F26: walkthrough and advisor checkpoints
# --------------------------------------------------------------------------


def run_walkthrough_drill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> DrillResult:
    """Following only the walkthrough's prompts reaches a green preflight."""
    started = time.monotonic()
    repo, _bindir = _fresh_walkthrough_store(tmp_path, monkeypatch)
    owner_steps: list[str] = []
    try:
        for _ in range(40):
            walk = build_walkthrough(repo)
            step = walk.next_step
            if step is None:
                break
            assert step.command, step.key
            if step.key == "scheduler":
                # The walkthrough's agent step starts the scheduler in the
                # background and returns; it never holds the terminal.
                for argv in _walkthrough_argv(step.command):
                    assert cli_main(argv) == 0, (step.key, argv)
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    from saberops.preflight import collect_preflight

                    if not [f for f in collect_preflight(repo) if f.severity == "FAIL"]:
                        break
                    time.sleep(0.2)
                else:
                    raise AssertionError("preflight never turned green")
                continue
            if step.kind == "owner":
                owner_steps.append(step.key)
            for argv in _walkthrough_argv(step.command):
                assert cli_main(argv) == 0, (step.key, argv)
        final = build_walkthrough(repo)
        assert final.ready is True, [s for s in final.steps if not s.done]
        assert final.next_step is None
        assert owner_steps, "a fresh store has owner-only steps to hand over"
    finally:
        # Stop the detached scheduler this drill started, in this process.
        from saberops.capacity.loop import stop_scheduler
        from saberops.db import Database

        try:
            stop_scheduler(Database())
        except Exception:  # noqa: BLE001 - cleanup never fails the drill
            pass
    text = intro_text()
    assert "subscriptions you already pay for" in text
    assert store_needs_introduction() is True
    return _feature_drill(
        "walkthrough",
        "F25",
        started=started,
        passed=True,
        final_state="READY",
        reason="the walkthrough's own prompts reached a green preflight",
        detail=f"{len(owner_steps)} owner-only steps handed over",
    )


def _fresh_walkthrough_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    for variable, name in (
        ("HOME", "walk-home"),
        ("XDG_STATE_HOME", "walk-state"),
        ("XDG_CONFIG_HOME", "walk-config"),
        ("XDG_CACHE_HOME", "walk-cache"),
        ("XDG_DATA_HOME", "walk-data"),
    ):
        directory = tmp_path / name
        directory.mkdir()
        monkeypatch.setenv(variable, str(directory))
    bindir = tmp_path / "walk-bin"
    bindir.mkdir()
    state = tmp_path / "walk-state" / "saberops"
    state.mkdir()
    for cli in ("codex", "claude"):
        shim = _install_shim(bindir, cli, state)
        contents = shim.read_text()
        prefix = f'if [ "$1" = "--version" ]; then echo "{cli} fake"; exit 0; fi\n'
        shim.write_text(contents.replace("#!/bin/sh\n", "#!/bin/sh\n" + prefix))
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    repo, _sha = create_toy_repo(tmp_path / "walk-repo-root")
    return repo, bindir


def _walkthrough_argv(command: str) -> list[list[str]]:
    argv: list[list[str]] = []
    for part in command.split(" && "):
        words = shlex.split(_with_walkthrough_answers(part))
        assert words.pop(0) == "orch"
        argv.append(words)
    return argv


def _with_walkthrough_answers(command: str) -> str:
    return (
        command.replace("<check command>", "true")
        .replace("<number from 0 to 100>", "80")
        .replace("<percent>", "80")
        .replace("<owner-confirmed-class>", "A")
        .replace("<policy-url>", "https://example.com/policy")
    )


def run_advisor_drill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> DrillResult:
    """A worker asks on demand: three calls advise, the fourth is refused."""
    from lifecycle_rig import build_lifecycle_rig

    started = time.monotonic()
    rig = build_lifecycle_rig(tmp_path, monkeypatch)
    db = rig.db()
    advice = "ADVICE-MARKER-b2b: verify the retry helper before adding a new one."
    advisor_log = rig.root / "advisor-calls.jsonl"
    rig.set_worker_mode(
        "commit",
        report_text=advice,
        advisor_questions=["q1", "q2", "q3", "q4"],
        advisor_log=advisor_log,
    )
    set_advisor_route(rig.repo, provider="codex", model="gpt-5.6-sol", call_budget=3)

    original = __import__("saberops.jobs.service", fromlist=["job_dispatch_spec"])
    spec_fn = original.job_dispatch_spec

    def tuned(job: Any) -> Any:
        spec = spec_fn(job)
        return replace(
            spec,
            request=replace(
                spec.request,
                max_attempts=1,
                review_limit=1,
                worker_timeout=20.0,
                gate_timeout=20.0,
                review_enabled=True,
                review_timeout=20.0,
            ),
        )

    monkeypatch.setattr(original, "job_dispatch_spec", tuned)
    submitted = submit_job(db, repo=rig.repo, goal="Ask the advisor on demand")
    tick = run_scheduler_loop(
        db, once=True, readiness_service=ReadinessService.for_owner_state(owner_state_dir())
    )
    assert tick is not None and tick.ran and tick.dispatched == 1, tick
    job = get_job(db, submitted.job.job_id)
    assert job is not None and job.current_run_id is not None
    run = rig.wait_for_run(job.current_run_id, timeout=120.0)
    assert run.status in {RunStatus.COMPLETED, RunStatus.FAILED}, run.result_summary

    from saberops.advisor import (
        ADVISOR_ADVICE_EVENT,
        ADVISOR_CALL_EVENT,
        ADVISOR_SKIPPED_EVENT,
    )

    calls = [
        json.loads(line)
        for line in advisor_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [entry["returncode"] for entry in calls] == [0, 0, 0, 3], calls
    assert all(entry["stdout"] == advice for entry in calls[:3])
    assert "budget exhausted" in calls[3]["stderr"]
    call_events = db.get_events_by_type(run.id, ADVISOR_CALL_EVENT)
    assert len(call_events) == 3
    assert all(
        (event.payload or {}).get("checkpoint") == "on_demand" for event in call_events
    )
    advice_events = db.get_events_by_type(run.id, ADVISOR_ADVICE_EVENT)
    assert len(advice_events) == 3
    assert all((event.payload or {}).get("advice") == advice for event in advice_events)
    skipped = db.get_events_by_type(run.id, ADVISOR_SKIPPED_EVENT)
    assert len(skipped) == 1
    assert (skipped[0].payload or {})["reason"] == "budget exhausted"
    caught = build_default_registry(
        ServerContext(db_path=Path(db.db_path), repo=rig.repo)
    ).call("catch_up", {"job_id": job.job_id})
    assert caught["advisor"]["calls"] == 3
    assert len(caught["advisor"]["advice"]) == 3
    return _feature_drill(
        "advisor on demand",
        "F26",
        started=started,
        passed=True,
        final_state="ADVISED",
        reason="three on-demand calls advised; the fourth was refused on budget",
        detail="advice recorded on the run; catch_up shows the same 3 calls",
    )


# --------------------------------------------------------------------------
# Report helpers
# --------------------------------------------------------------------------


def drills_report(
    mode: str,
    drills: Sequence[DrillResult],
    *,
    usage: Sequence[SubscriptionUsage] = (),
) -> Any:
    """A results table containing only these drills (and optional usage)."""
    from benchmark_core import BenchmarkReport

    return BenchmarkReport(
        mode=mode,
        results=(),
        drills=tuple(drills),
        started_at=0.0,
        finished_at=sum(drill.seconds for drill in drills),
        usage=tuple(usage),
    )


def assert_drill_groups(drills: Sequence[DrillResult], expected: Sequence[str]) -> None:
    assert [drill.group for drill in drills] == list(expected), [
        (drill.name, drill.group) for drill in drills
    ]
    for drill in drills:
        assert drill.slow is True, drill
        assert drill.passed is True, drill
        assert drill.seconds <= DRILL_VISIBLE_LIMIT_SECONDS, drill
        assert drill.reason, drill
    assert_catalogued_drills(drills)


def assert_catalogued_drills(drills: Sequence[DrillResult]) -> None:
    """Every emitted drill is in the H35 catalogue with the same facts.

    This is what makes the checked-in results table trustworthy: a new drill or
    a renamed/changed group or slow marker fails the run until its catalogue
    entry and baseline row are updated.
    """
    from benchmark_core import DRILLS_BY_NAME

    for drill in drills:
        entry = DRILLS_BY_NAME.get(drill.name)
        assert entry is not None, f"drill {drill.name!r} is not in DRILL_CATALOGUE"
        assert entry.group == drill.group, (entry, drill)
        assert entry.slow is drill.slow, (entry, drill)
        assert drill.passed is True, drill
        assert drill.seconds <= entry.limit_seconds, (entry, drill)


__all__ = [
    "SCOUT_REPORT",
    "assert_catalogued_drills",
    "assert_drill_groups",
    "drills_report",
    "run_advisor_drill",
    "run_autonomy_drills",
    "run_landing_queue_drill",
    "run_plan_drills",
    "run_presence_drills",
    "run_question_mode_drills",
    "run_scheduling_drills",
    "run_scout_proxy_drills",
    "run_vendor_drills",
    "run_walkthrough_drill",
    "usage_rows",
]
