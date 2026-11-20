"""Phase 1 lane P5: modes, notices, catch-up and the guide.

The rig builds a real isolated store and toy repository.  These tests exercise
the owner-visible behaviour the lane promises:

* plan auto-advance asks F1's typed effective-permission function; a narrower
  authority preset refuses a start with the reason visible in ``STATE.md``;
* ``away`` keeps submitting unblocked lanes and then parks at the first
  candidate when the preset does not grant auto-accept, with the accept as the
  next action;
* a stall and a completion each notify once, and a repeated tick sends nothing;
* M3's return summary carries the plan section, and ``catch_up`` exposes it;
* the planner guide/skill and the MCP docs no longer claim a ledger wave or an
  ``orch plan show|validate`` verb the engine does not have.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from lifecycle_rig import RigEnvironment, build_lifecycle_rig

from saberops.authority import set_authority_mode
from saberops.capacity.loop import run_scheduler_loop
from saberops.capacity.scheduler import DispatchDecision
from saberops.db import Database, current_iso_timestamp
from saberops.jobs import store
from saberops.jobs.models import RUN_FAILED, Job, JobState
from saberops.jobs.service import list_jobs
from saberops.mcp.guide import GUIDE
from saberops.mcp.lease import OrchestratorLease
from saberops.mcp.tools import ServerContext, build_default_registry
from saberops.model_access.readiness_service import ReadinessService
from saberops.models import Run, RunStatus, Tier
from saberops.owner_mode import set_owner_mode
from saberops.paths import owner_state_dir
from saberops.plan_runner import (
    NO_PLANNING_SESSION,
    get_plan,
    project_plan,
    reconcile_plan_lanes,
    submit_plan_file,
)
from saberops.plan_state import (
    deliver_plan_notices,
    plan_return_summaries,
    project_plan_state,
)
from saberops.provenance import mint_run_provenance

pytestmark = pytest.mark.packaged_routing_default

PLAN_ID = "p5-rig"


# ---------------------------------------------------------------------------
# Rig helpers (the same shapes the P2/P3 lanes use)
# ---------------------------------------------------------------------------


@pytest.fixture()
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RigEnvironment:
    return build_lifecycle_rig(tmp_path, monkeypatch)


@pytest.fixture()
def planner(rig: RigEnvironment) -> Any:
    """A live planning session: the MCP planning lease held by this process."""
    lease = OrchestratorLease()
    assert lease.acquire() is True
    try:
        yield lease
    finally:
        lease.release()


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _spec(**fields: Any) -> dict[str, Any]:
    return dict(fields)


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
    rig: RigEnvironment, lanes: Sequence[Mapping[str, Any]], *, plan_id: str = PLAN_ID
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
    path = rig.repo / "PLAN.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    _git(rig.repo, "add", "PLAN.md")
    _git(rig.repo, "commit", "-m", "plan: activate p5-rig")
    return path


def _activate(
    rig: RigEnvironment,
    lanes: Sequence[Mapping[str, Any]],
    *,
    owner_initiated: bool = True,
) -> Any:
    path = _write_plan(rig, lanes)
    return submit_plan_file(
        rig.db(),
        repo=rig.repo,
        plan_path=path,
        owner_initiated=owner_initiated,
    )


def _tick(db: Database) -> Any:
    tick = run_scheduler_loop(
        db,
        once=True,
        readiness_service=ReadinessService.for_owner_state(owner_state_dir()),
        run_launcher=lambda job, decision, run_id: run_id,
    )
    assert tick is not None and tick.ran is True, tick
    return tick


def _dispatched(tick: Any) -> list[str]:
    return [
        decision.job.job_id
        for decision in tick.decisions
        if isinstance(decision, DispatchDecision)
    ]


def _lane_job(db: Database, lane_id: str, *, plan_id: str = PLAN_ID) -> Job | None:
    plan = get_plan(db, plan_id)
    assert plan is not None
    view = project_plan(db, plan).view(lane_id)
    assert view is not None
    if view.job_id is None:
        return None
    return store.get_job(db, view.job_id)


def _plan_jobs(db: Database, *, plan_id: str = PLAN_ID) -> list[Job]:
    return [
        job for job in list_jobs(db, limit=500) if job.caller_id == f"plan:{plan_id}"
    ]


def _run_for_job(rig: RigEnvironment, db: Database, job: Job) -> str:
    provenance = mint_run_provenance(rig.repo, "p5 lane")
    now = current_iso_timestamp()
    base_commit = _git(rig.repo, "rev-parse", "HEAD")
    db.create_run(
        Run(
            id=provenance.run_id,
            task="p5 lane",
            target_repo=str(rig.repo),
            base_commit=base_commit,
            tier=Tier.T1,
            training_allowed=True,
            gate_command=rig.gate_command,
            status=RunStatus.COMPLETED,
            created_at=now,
            completed_at=now,
            project_id=provenance.project_id,
            project_identity_json=provenance.identity.to_json(),
            objective_id=provenance.objective_id,
            config_json=json.dumps({"review_enabled": True}),
        )
    )
    store.mark_dispatched(db, job.job_id, provenance.run_id)
    store.reconcile_transition(db, job.job_id, to=JobState.SUCCEEDED)
    return provenance.run_id


def _complete_candidate(rig: RigEnvironment, db: Database, job: Job) -> str:
    """Finish the job as a candidate-ready result; nothing is accepted."""
    return _run_for_job(rig, db, job)


def _complete_and_accept(rig: RigEnvironment, db: Database, job: Job) -> str:
    run_id = _run_for_job(rig, db, job)
    db.record_event(run_id, "run_accepted", payload={"candidate_sha": "c" * 40})
    return run_id


def _fail(rig: RigEnvironment, db: Database, job: Job) -> None:
    store.mark_dispatched(db, job.job_id, "run_p5_failed")
    store.reconcile_transition(
        db, job.job_id, to=JobState.FAILED, failure_reason=RUN_FAILED
    )


def _commit_file(rig: RigEnvironment, name: str) -> str:
    (rig.repo / name).write_text("accepted lane moved HEAD\n", encoding="utf-8")
    _git(rig.repo, "add", name)
    _git(rig.repo, "commit", "-m", f"accept: {name}")
    return _git(rig.repo, "rev-parse", "HEAD")


def _notice_stub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    log = tmp_path / "plan-notices"
    command = tmp_path / "notify-send"
    command.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$NOTICE_LOG"\n', encoding="utf-8")
    command.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    monkeypatch.setenv("NOTICE_LOG", str(log))
    monkeypatch.setenv("SABEROPS_DESKTOP_NOTIFICATIONS", "1")
    return log


LANES_A_B_C: list[dict[str, Any]] = [
    {"id": "A", "title": "Root lane"},
    {"id": "B", "title": "Dependent lane", "needs": ["A"]},
    {"id": "C", "title": "Independent lane"},
]


# ---------------------------------------------------------------------------
# F1: auto-advance asks the typed effective permission
# ---------------------------------------------------------------------------


def test_authority_without_auto_start_refuses_auto_advance(
    rig: RigEnvironment, planner: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = rig.db()
    set_authority_mode("ask_before_actions")
    submission = _activate(rig, LANES_A_B_C, owner_initiated=False)
    assert submission.submitted == ()
    assert submission.paused is not None
    assert submission.paused.startswith("mode_restricted")
    assert _plan_jobs(db) == []

    plan = get_plan(db, PLAN_ID)
    assert plan is not None
    state = project_plan_state(db, plan)
    assert state.advance_refusal is not None
    assert "auto_start" in state.advance_refusal
    # The visible reason is in the rendered ledger, not only in a return value.
    from saberops.plan_state import plan_state_path

    rendered = plan_state_path(plan).read_text(encoding="utf-8")
    assert "Auto-advance: on - blocked:" in rendered

    # The same plan advances once the authority preset allows a start.
    set_authority_mode("supervised")
    result = reconcile_plan_lanes(db)
    assert set(result[0].submitted) == {"A", "C"}
    assert project_plan_state(db, get_plan(db, PLAN_ID)).advance_refusal is None  # type: ignore[arg-type]


def test_away_keeps_starting_unblocked_lanes_then_parks_at_the_accept(
    rig: RigEnvironment, planner: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = rig.db()
    set_owner_mode("away", until="2999-01-01T00:00:00Z")
    submission = _activate(rig, LANES_A_B_C)
    assert set(submission.submitted) == {"A", "C"}
    a_job = _lane_job(db, "A")
    c_job = _lane_job(db, "C")
    assert a_job is not None and c_job is not None

    # Away keeps submitting; the dependent lane stays held until A is accepted.
    tick = _tick(db)
    assert {a_job.job_id, c_job.job_id} <= set(_dispatched(tick))
    assert _lane_job(db, "B") is None

    # Both unblocked lanes reach candidate-ready.  The default supervised
    # preset does not grant auto-accept, so the plan parks here instead of
    # pretending to progress.
    _complete_candidate(rig, db, a_job)
    _complete_candidate(rig, db, c_job)
    _tick(db)
    assert _lane_job(db, "B") is None
    state = project_plan_state(db, get_plan(db, PLAN_ID))  # type: ignore[arg-type]
    assert state.next_action.startswith("Accept lane A's candidate")
    assert state.advance_refusal is None

    summaries = plan_return_summaries(db)
    assert summaries[0]["waiting_for_owner"] == ["A", "C"]
    assert summaries[0]["next_action"] == state.next_action

    # The parked plan sends one "waiting for you" notice, not one per tick.
    log = _notice_stub(tmp_path, monkeypatch)
    deliver_plan_notices(db, mode="away")
    deliver_plan_notices(db, mode="away")
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert "A" in lines[0]
    assert "plan waits for you" in lines[0]

    # A fresh CLI reads the same plan section through M3's return summary.
    registry = build_default_registry(
        ServerContext(
            db_path=db.db_path,
            questions_store=tmp_path / "questions.json",
        )
    )
    caught = registry.call("catch_up", {"job_id": a_job.job_id})
    plans = caught["return_summary"]["plans"]
    assert plans[0]["plan_id"] == PLAN_ID
    assert plans[0]["waiting_for_owner"] == ["A", "C"]
    assert plans[0]["next_action"] == state.next_action


def test_stalled_plan_notifies_once(
    rig: RigEnvironment, planner: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = rig.db()
    _activate(rig, LANES_A_B_C)
    a_job = _lane_job(db, "A")
    assert a_job is not None
    _fail(rig, db, a_job)
    _tick(db)

    state = project_plan_state(db, get_plan(db, PLAN_ID))  # type: ignore[arg-type]
    assert "failed" in state.next_action
    assert plan_return_summaries(db)[0]["parked"] == ["A"]

    log = _notice_stub(tmp_path, monkeypatch)
    deliver_plan_notices(db)
    deliver_plan_notices(db)
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert "plan stalled" in lines[0]
    assert "A" in lines[0]


def test_completed_plan_notifies_once(
    rig: RigEnvironment, planner: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = rig.db()
    _activate(rig, [_spec(id="A", title="Only lane")])
    a_job = _lane_job(db, "A")
    assert a_job is not None
    _complete_and_accept(rig, db, a_job)
    _commit_file(rig, "accepted.txt")
    _tick(db)

    state = project_plan_state(db, get_plan(db, PLAN_ID))  # type: ignore[arg-type]
    assert state.next_action.startswith("Nothing to accept")
    assert plan_return_summaries(db)[0]["accepted"] == ["A"]

    log = _notice_stub(tmp_path, monkeypatch)
    deliver_plan_notices(db)
    deliver_plan_notices(db)
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert "plan complete" in lines[0]


def test_plan_can_disable_auto_advance(rig: RigEnvironment) -> None:
    db = rig.db()
    # Activate the plan with no session attached (F24): an unattended
    # activation records the plan and starts nothing, which gives auto-advance
    # something to prove below.  F24 gives the rig an attended terminal by
    # default, and a live session legitimately starts the ready root lane.
    rig.end_attendance()
    submission = _activate(rig, [_spec(id="A", title="Root lane")], owner_initiated=False)
    assert submission.submitted == ()
    assert submission.paused == NO_PLANNING_SESSION
    assert _plan_jobs(db) == []
    with sqlite3.connect(db.owner_db.db_path) as conn:
        conn.execute("UPDATE plans SET auto_advance = 0 WHERE plan_id = ?", (PLAN_ID,))
        conn.commit()

    lease = OrchestratorLease()
    assert lease.acquire() is True
    try:
        result = reconcile_plan_lanes(db)
    finally:
        lease.release()
    assert result[0].submitted == ()
    assert result[0].paused == "auto_advance_off"
    assert _plan_jobs(db) == []

    state = project_plan_state(db, get_plan(db, PLAN_ID))  # type: ignore[arg-type]
    assert state.advance_refusal == "auto-advance is off for this plan"
    assert "held: auto-advance is off" in state.next_action


# ---------------------------------------------------------------------------
# Docs and the guide: no capability the engine does not have
# ---------------------------------------------------------------------------


def test_docs_and_guide_name_the_real_plan_surface() -> None:
    root = Path(__file__).resolve().parents[1]
    mcp_doc = (root / "docs" / "mcp.md").read_text(encoding="utf-8")
    parallel = (root / "docs" / "orchestrator-parallel-first.md").read_text(
        encoding="utf-8"
    )
    team = (root / "docs" / "parallel-work.md").read_text(encoding="utf-8")

    # No production path appends wave_submitted, and nothing reports waves.
    assert "wave_submitted" not in mcp_doc
    assert "submitted waves" not in mcp_doc
    # Store verbs are `plan-store show|validate`, by symbol, not `plan show`.
    assert "plan-store show|validate" in parallel
    assert "plan-store show|validate" in team
    assert "orch plan show|validate" not in parallel
    assert "orch plan show|validate" not in team
    # The guide teaches the plan surface a planner can actually use.
    assert "plan_status" in GUIDE
    assert "submit_plan" in GUIDE
    assert "plan_id" in GUIDE
