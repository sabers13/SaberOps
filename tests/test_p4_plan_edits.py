"""Phase 1 lane P4: reconcile a plan edit mid-run.

The rig builds a real isolated store and toy repository.  These tests walk the
report's edit matrix cell by cell:

* a new lane is registered and submitted; a removed lane is dropped, its
  QUEUED job cancelled, its RUNNING job left alone, and its candidate or
  accepted result kept for the owner;
* a changed brief supersedes a QUEUED job (cancel + resubmit), never signals a
  RUNNING one, and turns a ready candidate into an owner decision;
* a dependency-only change is never retroactive;
* an invalid revision or an accepted rewrite leaves the previous revision
  active and cancels nothing;
* a running lane's process is never signalled.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from lifecycle_rig import RigEnvironment, build_lifecycle_rig

import saberops.plan_runner as plan_runner
from saberops.capacity.loop import run_scheduler_loop
from saberops.db import Database, current_iso_timestamp
from saberops.jobs import store
from saberops.jobs.dependencies import job_is_accepted
from saberops.jobs.models import RUN_FAILED, Job, JobState
from saberops.jobs.service import list_jobs
from saberops.mcp.lease import OrchestratorLease
from saberops.model_access.readiness_service import ReadinessService
from saberops.models import Run, RunStatus, Tier
from saberops.paths import owner_state_dir
from saberops.plan_runner import (
    get_plan,
    lane_spec_digest,
    mark_owner_lane_done,
    plan_lane_rows,
    project_plan,
    reconcile_plan_lanes,
    set_lane_submit_gate,
    submit_plan_file,
)
from saberops.plan_state import project_plan_state
from saberops.project_scope.plan_store import PlanRevision, PlanTaskDef
from saberops.provenance import mint_run_provenance

pytestmark = pytest.mark.packaged_routing_default

PLAN_ID = "p4-rig"


# ---------------------------------------------------------------------------
# Rig helpers
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


@pytest.fixture()
def paused() -> Any:
    """Hold every lane submission without a live planning session."""
    set_lane_submit_gate(lambda _db: "paused for the edit test")
    try:
        yield
    finally:
        set_lane_submit_gate(None)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _lane_block(lane: Mapping[str, Any], *, include_done: bool = True) -> list[str]:
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
    lines = [
        f"### {lane['id']}. {lane.get('title', lane['id'])}",
        "",
        f"- **Why:** {lane.get('why', 'the plan needs this lane')}",
        f"- **Do:** {lane.get('do', 'make the change')}",
        f"- **Out of scope:** {lane.get('out_of_scope', 'none')}",
    ]
    if include_done:
        lines.extend(
            ["- **Done when:**", f"  - {lane.get('done', 'the change is checked')}"]
        )
    lines.extend(
        [
            f"- **Needs:** {needs}",
            "",
            "```plan",
            json.dumps(machine, sort_keys=True),
            "```",
            "",
        ]
    )
    return lines


def _write_plan(
    rig: RigEnvironment,
    lanes: Sequence[Mapping[str, Any]],
    *,
    plan_id: str = PLAN_ID,
    include_done: bool = True,
    commit: bool = True,
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
        lines.extend(_lane_block(lane, include_done=include_done))
    path = rig.repo / "PLAN.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    if commit:
        _git(rig.repo, "add", "PLAN.md")
        _git(rig.repo, "commit", "-m", f"plan: revise {plan_id}")
    return path


def _activate(rig: RigEnvironment, lanes: Sequence[Mapping[str, Any]]) -> Any:
    path = _write_plan(rig, lanes)
    return submit_plan_file(
        rig.db(), repo=rig.repo, plan_path=path, owner_initiated=True
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


def _plan(db: Database) -> Any:
    plan = get_plan(db, PLAN_ID)
    assert plan is not None
    return plan


def _revision(db: Database) -> PlanRevision:
    plan = _plan(db)
    revision: PlanRevision = plan.plan_store().get_revision(
        plan.project_id, plan.active_version
    )
    return revision


def _lane_job(db: Database, lane_id: str) -> Job | None:
    plan = _plan(db)
    view = project_plan(db, plan).view(lane_id)
    if view is None:
        return None
    if view.job_id is None:
        return None
    return store.get_job(db, view.job_id)


def _plan_jobs(db: Database) -> list[Job]:
    return [
        job for job in list_jobs(db, limit=500) if job.caller_id == f"plan:{PLAN_ID}"
    ]


def _run_lane(db: Database, job: Job) -> str:
    """Move a lane job to RUNNING with a run id, without executing anything."""
    run_id = f"run_{job.job_id}"
    store.mark_dispatched(db, job.job_id, run_id)
    return run_id


def _finish_lane(
    rig: RigEnvironment, db: Database, job: Job, *, accept: bool
) -> str:
    """Finish a lane on a real run row; acceptance is an explicit event."""
    current = store.get_job(db, job.job_id)
    assert current is not None
    if current.current_run_id is None:
        run_id = f"run_{job.job_id}"
        store.mark_dispatched(db, job.job_id, run_id)
    else:
        run_id = current.current_run_id
    provenance = mint_run_provenance(rig.repo, f"p4 lane {job.job_id}")
    now = current_iso_timestamp()
    db.create_run(
        Run(
            id=run_id,
            task=f"p4 lane {job.job_id}",
            target_repo=str(rig.repo),
            base_commit=current.base_sha or rig.base_sha,
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
    store.reconcile_transition(db, job.job_id, to=JobState.SUCCEEDED)
    if accept:
        db.record_event(run_id, "run_accepted", payload={"candidate_sha": "c" * 40})
    return run_id


def _fail_lane(db: Database, job: Job) -> None:
    current = store.get_job(db, job.job_id)
    assert current is not None
    if current.state is JobState.QUEUED:
        store.mark_dispatched(db, job.job_id, f"run_{job.job_id}")
    store.reconcile_transition(
        db, job.job_id, to=JobState.FAILED, failure_reason=RUN_FAILED
    )


def _events(db: Database, *, kind: str | None = None) -> list[dict[str, Any]]:
    with db.owner_db._get_connection() as conn:
        rows = conn.execute(
            "SELECT kind, lane_id, payload_json FROM plan_events"
            " WHERE plan_id = ? ORDER BY id",
            (PLAN_ID,),
        ).fetchall()
    events = [
        {
            "kind": str(row["kind"]),
            "lane_id": str(row["lane_id"]) if row["lane_id"] else None,
            "payload": json.loads(str(row["payload_json"])),
        }
        for row in rows
    ]
    if kind is not None:
        events = [event for event in events if event["kind"] == kind]
    return events


def _supersede_actions(db: Database, lane_id: str) -> list[str]:
    return [
        str(event["payload"].get("action"))
        for event in _events(db, kind="plan_lane_superseded")
        if event["lane_id"] == lane_id
    ]


def _lane_spec(db: Database, lane_id: str) -> PlanTaskDef | None:
    for task in _revision(db).tasks:
        if task.task_id == lane_id:
            return task
    return None


@pytest.fixture()
def cancel_spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every job id the plan layer asks ``cancel_job`` to cancel."""
    calls: list[str] = []
    original = plan_runner.__dict__["cancel_job"]

    def spy(db: Any, job_id: str, *, run_canceller: Any) -> Any:
        calls.append(job_id)
        return original(db, job_id, run_canceller=run_canceller)

    monkeypatch.setattr(plan_runner, "cancel_job", spy)
    return calls


LANES_A_C: list[dict[str, Any]] = [
    {"id": "A"},
    {"id": "C"},
]


def _spec_id(lane_id: str, **fields: Any) -> dict[str, Any]:
    return {"id": lane_id, **fields}


# ---------------------------------------------------------------------------
# New lane added
# ---------------------------------------------------------------------------


def test_new_lane_is_registered_and_submitted(
    rig: RigEnvironment, planner: Any
) -> None:
    db = rig.db()
    _activate(rig, [_spec_id("A")])
    a_job = _lane_job(db, "A")
    assert a_job is not None

    _activate(rig, [_spec_id("A"), _spec_id("C")])
    c_job = _lane_job(db, "C")
    assert c_job is not None and c_job.state is JobState.QUEUED
    assert _lane_job(db, "A") is not None
    assert _lane_job(db, "A").job_id == a_job.job_id  # type: ignore[union-attr]
    assert {task.task_id for task in _revision(db).tasks} == {"A", "C"}


# ---------------------------------------------------------------------------
# Lane removed
# ---------------------------------------------------------------------------


def test_removed_lane_without_a_job_is_dropped(
    rig: RigEnvironment, paused: Any
) -> None:
    db = rig.db()
    _activate(rig, LANES_A_C)
    assert _plan_jobs(db) == []

    _activate(rig, [_spec_id("A")])
    assert _lane_job(db, "C") is None
    assert {task.task_id for task in _revision(db).tasks} == {"A"}
    assert _supersede_actions(db, "C") == ["dropped"]
    assert _plan_jobs(db) == []


def test_removed_queued_lane_is_cancelled(rig: RigEnvironment, planner: Any) -> None:
    db = rig.db()
    _activate(rig, LANES_A_C)
    c_job = _lane_job(db, "C")
    assert c_job is not None
    c_id = c_job.job_id

    _activate(rig, [_spec_id("A")])
    cancelled = store.get_job(db, c_id)
    assert cancelled is not None and cancelled.state is JobState.CANCELLED
    assert _supersede_actions(db, "C") == ["cancelled_queued"]
    assert _lane_job(db, "C") is None


def test_removed_running_lane_is_never_signalled(
    rig: RigEnvironment, planner: Any, cancel_spy: list[str]
) -> None:
    db = rig.db()
    _activate(rig, LANES_A_C)
    running = _lane_job(db, "C")
    assert running is not None
    _run_lane(db, running)
    running_id = running.job_id

    _activate(rig, [_spec_id("A")])
    after = store.get_job(db, running_id)
    assert after is not None and after.state is JobState.RUNNING
    assert running_id not in cancel_spy
    assert _supersede_actions(db, "C") == ["kept_running"]
    row = plan_lane_rows(db, PLAN_ID)["C"]
    assert row.superseded_by == _plan(db).active_version


def test_removed_candidate_waits_for_the_owner(
    rig: RigEnvironment, planner: Any
) -> None:
    db = rig.db()
    _activate(rig, LANES_A_C)
    candidate = _lane_job(db, "C")
    assert candidate is not None
    run_id = _finish_lane(rig, db, candidate, accept=False)

    _activate(rig, [_spec_id("A")])
    kept = store.get_job(db, candidate.job_id)
    assert kept is not None and kept.state is JobState.SUCCEEDED
    assert not job_is_accepted(db, kept)
    assert _supersede_actions(db, "C") == ["needs_decision"]
    # The owner may still accept the preserved candidate.
    db.record_event(run_id, "run_accepted", payload={"candidate_sha": "c" * 40})
    assert job_is_accepted(db, store.get_job(db, candidate.job_id))  # type: ignore[arg-type]


def test_removed_accepted_lane_is_kept_as_history(
    rig: RigEnvironment, planner: Any
) -> None:
    db = rig.db()
    _activate(rig, LANES_A_C)
    accepted = _lane_job(db, "C")
    assert accepted is not None
    _finish_lane(rig, db, accepted, accept=True)

    _activate(rig, [_spec_id("A")])
    kept = store.get_job(db, accepted.job_id)
    assert kept is not None and job_is_accepted(db, kept)
    row = plan_lane_rows(db, PLAN_ID)["C"]
    assert row.superseded_by is None
    assert _supersede_actions(db, "C") == []


# ---------------------------------------------------------------------------
# Lane brief changed
# ---------------------------------------------------------------------------


def test_changed_lane_without_a_job_uses_the_new_brief(
    rig: RigEnvironment, paused: Any
) -> None:
    db = rig.db()
    _activate(rig, [_spec_id("A", do="old body")])
    assert _plan_jobs(db) == []

    _activate(rig, [_spec_id("A", do="new body")])
    row = plan_lane_rows(db, PLAN_ID)["A"]
    task = _lane_spec(db, "A")
    assert task is not None
    assert row.lane_digest == plan_runner.lane_digest(task)
    assert row.job_id is None


def test_changed_queued_lane_is_cancelled_and_resubmitted(
    rig: RigEnvironment, planner: Any
) -> None:
    db = rig.db()
    _activate(rig, [_spec_id("A", do="old body")])
    old = _lane_job(db, "A")
    assert old is not None
    old_id = old.job_id

    _activate(rig, [_spec_id("A", do="new body")])
    retired = store.get_job(db, old_id)
    assert retired is not None and retired.state is JobState.CANCELLED
    replacement = _lane_job(db, "A")
    assert replacement is not None
    assert replacement.job_id != old_id
    assert "new body" in replacement.goal
    assert _supersede_actions(db, "A") == ["cancelled_queued"]


def test_changed_running_lane_finishes_its_original_brief(
    rig: RigEnvironment, planner: Any, cancel_spy: list[str]
) -> None:
    db = rig.db()
    _activate(rig, [_spec_id("A", do="old body")])
    running = _lane_job(db, "A")
    assert running is not None
    _run_lane(db, running)
    running_id = running.job_id

    _activate(rig, [_spec_id("A", do="new body")])
    kept = store.get_job(db, running_id)
    assert kept is not None and kept.state is JobState.RUNNING
    assert "old body" in kept.goal
    assert running_id not in cancel_spy
    assert _supersede_actions(db, "A") == ["kept_running"]
    row = plan_lane_rows(db, PLAN_ID)["A"]
    assert row.superseded_by == _plan(db).active_version
    # The superseded attempt is not counted; its failure frees the new brief.
    _fail_lane(db, kept)
    _tick(db)
    replacement = _lane_job(db, "A")
    assert replacement is not None
    assert replacement.job_id != running_id
    assert "new body" in replacement.goal
    assert _events(db, kind="plan_lane_retried")[0]["lane_id"] == "A"


def test_changed_candidate_becomes_an_owner_decision(
    rig: RigEnvironment, planner: Any
) -> None:
    db = rig.db()
    _activate(rig, [_spec_id("A", do="old body")])
    candidate = _lane_job(db, "A")
    assert candidate is not None
    _finish_lane(rig, db, candidate, accept=False)

    _activate(rig, [_spec_id("A", do="new body")])
    kept = store.get_job(db, candidate.job_id)
    assert kept is not None and kept.state is JobState.SUCCEEDED
    # No automatic discard and no automatic new attempt.
    assert [job.job_id for job in _plan_jobs(db)] == [candidate.job_id]
    assert _supersede_actions(db, "A") == ["needs_decision"]
    state = project_plan_state(db, _plan(db))
    lane = next(item for item in state.lanes if item.lane_id == "A")
    assert lane.state == "needs-decision"
    assert "changed after its candidate was ready" in state.next_action


def test_accepted_lane_cannot_be_rewritten(
    rig: RigEnvironment, planner: Any
) -> None:
    db = rig.db()
    _activate(rig, [_spec_id("A", do="old body")])
    accepted = _lane_job(db, "A")
    assert accepted is not None
    _finish_lane(rig, db, accepted, accept=True)
    version = _plan(db).active_version
    job_ids = [job.job_id for job in _plan_jobs(db)]

    with pytest.raises(plan_runner.PlanRunnerError, match="accepted"):
        _activate(rig, [_spec_id("A", do="new body")])

    assert _plan(db).active_version == version
    assert _lane_spec(db, "A") is not None and _lane_spec(db, "A").goal == "old body"  # type: ignore[union-attr]
    assert [job.job_id for job in _plan_jobs(db)] == job_ids
    assert _supersede_actions(db, "A") == []


def test_owner_completed_lane_cannot_be_rewritten(
    rig: RigEnvironment, planner: Any
) -> None:
    db = rig.db()
    _activate(rig, [_spec_id("O", run="owner", do="check it")])
    mark_owner_lane_done(
        db, plan_id=PLAN_ID, lane_id="O", evidence="captain confirmed"
    )

    with pytest.raises(plan_runner.PlanRunnerError, match="completed by the owner"):
        _activate(rig, [_spec_id("O", run="owner", do="check it differently")])


# ---------------------------------------------------------------------------
# Dependencies changed
# ---------------------------------------------------------------------------


def test_dependency_change_without_a_job_applies_the_new_edge(
    rig: RigEnvironment, planner: Any, paused: Any
) -> None:
    db = rig.db()
    _activate(rig, LANES_A_C)
    assert _plan_jobs(db) == []

    _activate(rig, [_spec_id("A"), _spec_id("C", needs=["A"])])
    task = _lane_spec(db, "C")
    assert task is not None and task.start_dependencies == ("A",)
    row = plan_lane_rows(db, PLAN_ID)["C"]
    assert row.lane_digest == plan_runner.lane_digest(task)
    assert row.job_id is None
    set_lane_submit_gate(None)
    _tick(db)
    assert _lane_job(db, "A") is not None
    assert _lane_job(db, "C") is None
    assert "dependency_pending" in project_plan(db, _plan(db)).view("C").reasons  # type: ignore[union-attr]


def test_dependency_change_on_a_queued_lane_cancels_and_waits(
    rig: RigEnvironment, planner: Any
) -> None:
    db = rig.db()
    _activate(rig, LANES_A_C)
    c_job = _lane_job(db, "C")
    assert c_job is not None
    c_id = c_job.job_id

    _activate(rig, [_spec_id("A"), _spec_id("C", needs=["A"])])
    retired = store.get_job(db, c_id)
    assert retired is not None and retired.state is JobState.CANCELLED
    assert _lane_job(db, "C") is None
    assert _events(db, kind="plan_lane_dependencies_changed")[0]["lane_id"] == "C"

    # Accepting A releases the new edge and the lane is submitted then.
    a_job = _lane_job(db, "A")
    assert a_job is not None
    _finish_lane(rig, db, a_job, accept=True)
    _tick(db)
    replacement = _lane_job(db, "C")
    assert replacement is not None and replacement.job_id != c_id


def test_dependency_change_on_a_running_lane_is_not_retroactive(
    rig: RigEnvironment, planner: Any
) -> None:
    db = rig.db()
    _activate(rig, LANES_A_C)
    running = _lane_job(db, "C")
    assert running is not None
    _run_lane(db, running)
    running_id = running.job_id

    _activate(rig, [_spec_id("A"), _spec_id("C", needs=["A"])])
    kept = store.get_job(db, running_id)
    assert kept is not None and kept.state is JobState.RUNNING
    row = plan_lane_rows(db, PLAN_ID)["C"]
    assert row.superseded_by is None
    assert row.job_id == running_id
    assert _supersede_actions(db, "C") == []
    event = _events(db, kind="plan_lane_dependencies_changed")
    assert event[0]["payload"]["depends_on"] == ["A"]


def test_dependency_change_on_a_candidate_is_not_retroactive(
    rig: RigEnvironment, planner: Any
) -> None:
    db = rig.db()
    _activate(rig, LANES_A_C)
    candidate = _lane_job(db, "C")
    assert candidate is not None
    _finish_lane(rig, db, candidate, accept=False)

    _activate(rig, [_spec_id("A"), _spec_id("C", needs=["A"])])
    kept = store.get_job(db, candidate.job_id)
    assert kept is not None and kept.state is JobState.SUCCEEDED
    assert plan_lane_rows(db, PLAN_ID)["C"].superseded_by is None
    assert _supersede_actions(db, "C") == []


def test_dependency_change_on_an_accepted_lane_is_not_retroactive(
    rig: RigEnvironment, planner: Any
) -> None:
    db = rig.db()
    _activate(rig, LANES_A_C)
    accepted = _lane_job(db, "C")
    assert accepted is not None
    _finish_lane(rig, db, accepted, accept=True)

    _activate(rig, [_spec_id("A"), _spec_id("C", needs=["A"])])
    kept = store.get_job(db, accepted.job_id)
    assert kept is not None and job_is_accepted(db, kept)
    assert plan_lane_rows(db, PLAN_ID)["C"].superseded_by is None


# ---------------------------------------------------------------------------
# Invalid revision
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "lanes, include_done, message",
    [
        ([_spec_id("A"), _spec_id("B", needs=["Z"])], True, "unknown"),
        ([_spec_id("A", needs=["B"]), _spec_id("B", needs=["A"])], True, "cycle"),
        ([_spec_id("A")], False, "Done when"),
    ],
)
def test_invalid_revision_leaves_the_old_one_active_and_cancels_nothing(
    rig: RigEnvironment,
    planner: Any,
    lanes: list[dict[str, Any]],
    include_done: bool,
    message: str,
) -> None:
    db = rig.db()
    _activate(rig, [_spec_id("A")])
    a_job = _lane_job(db, "A")
    assert a_job is not None
    version = _plan(db).active_version
    old_tasks = {task.task_id for task in _revision(db).tasks}

    path = _write_plan(rig, lanes, include_done=include_done)
    with pytest.raises(plan_runner.PlanRunnerError, match=message):
        submit_plan_file(db, repo=rig.repo, plan_path=path, owner_initiated=True)

    assert _plan(db).active_version == version
    assert {task.task_id for task in _revision(db).tasks} == old_tasks
    kept = store.get_job(db, a_job.job_id)
    assert kept is not None and kept.state is JobState.QUEUED
    assert _supersede_actions(db, "A") == []


def test_removing_an_accepted_dependency_refuses_the_revision(
    rig: RigEnvironment, planner: Any
) -> None:
    db = rig.db()
    _activate(rig, [_spec_id("A"), _spec_id("B", needs=["A"])])
    accepted = _lane_job(db, "A")
    assert accepted is not None
    _finish_lane(rig, db, accepted, accept=True)
    version = _plan(db).active_version

    # B still names A, so removing A leaves an unknown dependency.
    path = _write_plan(rig, [_spec_id("B", needs=["A"])])
    with pytest.raises(plan_runner.PlanRunnerError, match="unknown"):
        submit_plan_file(db, repo=rig.repo, plan_path=path, owner_initiated=True)

    assert _plan(db).active_version == version
    kept = store.get_job(db, accepted.job_id)
    assert kept is not None and job_is_accepted(db, kept)


def test_second_tick_does_not_reapply_an_edit(
    rig: RigEnvironment, planner: Any, cancel_spy: list[str]
) -> None:
    db = rig.db()
    _activate(rig, [_spec_id("A", do="old body")])
    running = _lane_job(db, "A")
    assert running is not None
    _run_lane(db, running)
    _activate(rig, [_spec_id("A", do="new body")])
    calls = len(cancel_spy)

    reconcile_plan_lanes(db)
    reconcile_plan_lanes(db)
    assert len(cancel_spy) == calls
    assert len(_supersede_actions(db, "A")) == 1
    kept = store.get_job(db, running.job_id)
    assert kept is not None and kept.state is JobState.RUNNING
    task = _lane_spec(db, "A")
    assert task is not None
    assert lane_spec_digest(task) == plan_lane_rows(db, PLAN_ID)[
        "A"
    ].lane_spec_digest
