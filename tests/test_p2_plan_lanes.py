"""Phase 1 lane P2: submit plan lanes as jobs in dependency order.

The rig builds a real isolated store and toy repository.  These tests exercise
the owner-visible behaviour the lane promises:

* a plan (A; B needs A; C independent) submits A and C and holds B;
* B starts on the new HEAD only after A is accepted;
* an owner lane is never dispatched and unblocks its dependent only after the
  owner records completion;
* a second tick submits nothing twice, including after a crash between the job
  write and the lane mapping;
* a failed dependency blocks its dependant with a reason and leaves independent
  lanes alone;
* no lane is submitted without a live planning session or while the F17 pause
  gate refuses;
* ``submit_plan`` over MCP is path-only and repository-confined, and the CLI
  ``plan-file submit`` / ``plan-file done`` verbs reach the same service.
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
from saberops.capacity.scheduler import DispatchDecision
from saberops.db import Database, current_iso_timestamp
from saberops.jobs import store
from saberops.jobs.models import RUN_FAILED, Job, JobState
from saberops.jobs.service import list_jobs
from saberops.mcp.errors import InvalidParamsError
from saberops.mcp.lease import OrchestratorLease
from saberops.mcp.ledger import EventLedger
from saberops.mcp.tools import ServerContext, build_default_registry
from saberops.model_access.readiness_service import ReadinessService
from saberops.models import Run, RunStatus, Tier
from saberops.paths import owner_state_dir
from saberops.plan_runner import (
    NO_PLANNING_SESSION,
    get_plan,
    mark_owner_lane_done,
    project_plan,
    reconcile_plan_lanes,
    set_lane_submit_gate,
    submit_plan_file,
)
from saberops.provenance import mint_run_provenance

pytestmark = pytest.mark.packaged_routing_default

PLAN_ID = "p2-rig"


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


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _spec(**fields: Any) -> dict[str, Any]:
    """Build one lane spec with an explicit, mypy-stable value type."""
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
        if lane.get("difficulty"):
            machine["difficulty"] = lane["difficulty"]
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
    _git(rig.repo, "commit", "-m", "plan: activate p2-rig")
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
    """One scheduler tick with a launcher that records dispatch and spawns nothing."""
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


def _complete_and_accept(rig: RigEnvironment, db: Database, job: Job) -> str:
    """Finish a QUEUED lane job on a real run row and record its acceptance."""
    provenance = mint_run_provenance(rig.repo, "p2 lane")
    now = current_iso_timestamp()
    db.create_run(
        Run(
            id=provenance.run_id,
            task="p2 lane",
            target_repo=str(rig.repo),
            base_commit=rig.base_sha,
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
    db.record_event(provenance.run_id, "run_accepted", payload={"candidate_sha": "c" * 40})
    return provenance.run_id


def _fail(rig: RigEnvironment, db: Database, job: Job) -> None:
    store.mark_dispatched(db, job.job_id, "run_p2_failed")
    store.reconcile_transition(
        db, job.job_id, to=JobState.FAILED, failure_reason=RUN_FAILED
    )


def _commit_file(rig: RigEnvironment, name: str) -> str:
    (rig.repo / name).write_text("accepted lane moved HEAD\n", encoding="utf-8")
    _git(rig.repo, "add", name)
    _git(rig.repo, "commit", "-m", f"accept: {name}")
    return _git(rig.repo, "rev-parse", "HEAD")


LANES_A_B_C: list[dict[str, Any]] = [
    {"id": "A", "title": "Root lane"},
    {"id": "B", "title": "Dependent lane", "needs": ["A"]},
    {"id": "C", "title": "Independent lane"},
]


# ---------------------------------------------------------------------------
# Done-when: parallel ready lanes, held dependent
# ---------------------------------------------------------------------------


def test_lane_difficulty_reaches_the_submitted_job(
    rig: RigEnvironment, planner: Any
) -> None:
    """Covers: H1 (a lane's difficulty is a requirement of its job)."""
    from saberops.jobs.models import Difficulty

    db = rig.db()
    submission = _activate(
        rig,
        [
            {"id": "H", "title": "Hard lane", "difficulty": "hard"},
            {"id": "R", "title": "Routine lane"},
        ],
    )
    assert set(submission.submitted) == {"H", "R"}
    hard = _lane_job(db, "H")
    routine = _lane_job(db, "R")
    assert hard is not None and hard.requirements.difficulty is Difficulty.HARD
    assert routine is not None and routine.requirements.difficulty is Difficulty.ROUTINE


def test_ready_lanes_submit_in_parallel_and_hold_the_dependent(
    rig: RigEnvironment, planner: Any
) -> None:
    db = rig.db()
    submission = _activate(rig, LANES_A_B_C)
    assert set(submission.submitted) == {"A", "C"}
    assert submission.paused is None

    a_job = _lane_job(db, "A")
    c_job = _lane_job(db, "C")
    assert a_job is not None and c_job is not None
    assert _lane_job(db, "B") is None

    tick = _tick(db)
    dispatched = set(_dispatched(tick))
    assert {a_job.job_id, c_job.job_id} <= dispatched
    assert _lane_job(db, "B") is None

    plan = get_plan(db, PLAN_ID)
    assert plan is not None
    view = project_plan(db, plan).view("B")
    assert view is not None
    assert view.state == "held"
    assert "dependency_pending" in view.reasons


def test_dependent_lane_starts_after_accept_on_the_new_head(
    rig: RigEnvironment, planner: Any
) -> None:
    db = rig.db()
    _activate(rig, LANES_A_B_C)
    a_job = _lane_job(db, "A")
    assert a_job is not None and a_job.state == JobState.QUEUED
    _complete_and_accept(rig, db, a_job)
    new_head = _commit_file(rig, "accepted.txt")

    tick = _tick(db)
    b_job = _lane_job(db, "B")
    assert b_job is not None
    assert b_job.base_sha == new_head
    assert b_job.job_id in _dispatched(tick)
    assert "p2-rig:B" in tick.plan_lanes_submitted


def test_second_tick_submits_nothing_twice(rig: RigEnvironment, planner: Any) -> None:
    db = rig.db()
    _activate(rig, LANES_A_B_C)
    assert len(_plan_jobs(db)) == 2

    # The scheduler tick calls this reconciler before reading the queue; a
    # second pass must find every lane already mapped and submit nothing.
    again = reconcile_plan_lanes(db)
    assert again[0].submitted == ()
    assert again[0].recovered == ()
    assert len(_plan_jobs(db)) == 2


# ---------------------------------------------------------------------------
# Done-when: owner lanes
# ---------------------------------------------------------------------------


def test_owner_lane_is_never_submitted_and_done_unblocks_its_dependent(
    rig: RigEnvironment, planner: Any
) -> None:
    db = rig.db()
    submission = _activate(
        rig,
        [
            _spec(id="O", title="Captain check", run="owner"),
            _spec(id="D", title="After the captain", needs=["O"]),
        ],
    )
    assert submission.submitted == ()
    assert _plan_jobs(db) == []
    view = project_plan(db, get_plan(db, PLAN_ID)).view("O")  # type: ignore[arg-type]
    assert view is not None and view.state == "owner"

    done = mark_owner_lane_done(
        db, plan_id=PLAN_ID, lane_id="O", evidence="captain confirmed the manual step"
    )
    assert done.state == "done"
    # Recording completion alone does not submit; the next tick does.
    assert _lane_job(db, "D") is None

    tick = _tick(db)
    d_job = _lane_job(db, "D")
    assert d_job is not None
    assert d_job.job_id in _dispatched(tick)


def test_cli_plan_file_submit_and_done(rig: RigEnvironment, planner: Any) -> None:
    path = _write_plan(
        rig,
        [
            _spec(id="O", title="Captain check", run="owner"),
            _spec(id="D", title="After the captain", needs=["O"]),
        ],
    )
    submitted = rig.run_cli(
        "plan-file", "submit", str(path), "--repo", str(rig.repo), "--json"
    )
    assert submitted.returncode == 0, submitted.stderr
    payload = json.loads(submitted.stdout)
    assert payload["ok"] is True
    assert payload["submitted"] == []

    done = rig.run_cli(
        "plan-file",
        "done",
        "O",
        "--plan",
        PLAN_ID,
        "--evidence",
        "captain confirmed",
        "--json",
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["state"] == "done"

    tick = _tick(rig.db())
    d_job = _lane_job(rig.db(), "D")
    assert d_job is not None
    assert d_job.job_id in _dispatched(tick)


def test_a_job_lane_cannot_be_marked_done_by_the_owner(rig: RigEnvironment, planner: Any) -> None:
    db = rig.db()
    _activate(rig, [_spec(id="A", title="Root lane")])
    with pytest.raises(plan_runner.PlanRunnerError, match="job lane"):
        mark_owner_lane_done(db, plan_id=PLAN_ID, lane_id="A", evidence="nope")


# ---------------------------------------------------------------------------
# Done-when: failure, crash safety, pause gates
# ---------------------------------------------------------------------------


def test_failed_lane_blocks_its_dependent_and_leaves_independents_alone(
    rig: RigEnvironment, planner: Any
) -> None:
    db = rig.db()
    _activate(rig, LANES_A_B_C)
    a_job = _lane_job(db, "A")
    c_job = _lane_job(db, "C")
    assert a_job is not None and c_job is not None
    _fail(rig, db, a_job)

    _tick(db)
    assert _lane_job(db, "B") is None
    plan = get_plan(db, PLAN_ID)
    assert plan is not None
    view = project_plan(db, plan).view("B")
    assert view is not None
    assert "dependency_blocked" in view.reasons
    independent = store.get_job(db, c_job.job_id)
    assert independent is not None
    assert independent.state in {JobState.QUEUED, JobState.RUNNING}


def test_crash_between_submit_and_mapping_does_not_duplicate(
    rig: RigEnvironment, planner: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = rig.db()
    original = plan_runner._write_lane_job
    calls = {"n": 0}

    def flaky(*args: Any, **kwargs: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated crash between submit and mapping write")
        original(*args, **kwargs)

    monkeypatch.setattr(plan_runner, "_write_lane_job", flaky)
    submission = _activate(rig, [_spec(id="A", title="Root lane")])
    assert submission.error is not None
    assert _lane_job(db, "A") is None
    assert len(_plan_jobs(db)) == 1

    monkeypatch.setattr(plan_runner, "_write_lane_job", original)
    result = reconcile_plan_lanes(db, owner_initiated=True)
    assert result[0].recovered == ("A",)
    jobs = _plan_jobs(db)
    assert len(jobs) == 1
    assert _lane_job(db, "A") is not None
    assert _lane_job(db, "A").job_id == jobs[0].job_id  # type: ignore[union-attr]


def test_reconciler_submits_nothing_without_a_planning_session(
    rig: RigEnvironment,
) -> None:
    # F24: the lane seam is about absence, so close the rig's attended terminal.
    rig.end_attendance()
    db = rig.db()
    submission = _activate(rig, LANES_A_B_C, owner_initiated=False)
    assert submission.submitted == ()
    assert submission.paused == NO_PLANNING_SESSION
    assert _plan_jobs(db) == []

    lease = OrchestratorLease()
    assert lease.acquire() is True
    try:
        result = reconcile_plan_lanes(db)
        assert set(result[0].submitted) == {"A", "C"}
    finally:
        lease.release()


def test_f17_gate_pauses_every_lane_submission(
    rig: RigEnvironment, planner: Any
) -> None:
    db = rig.db()
    set_lane_submit_gate(lambda _db: "paused: no planning session")
    try:
        submission = _activate(rig, LANES_A_B_C, owner_initiated=True)
    finally:
        set_lane_submit_gate(None)
    assert submission.submitted == ()
    assert submission.paused == "paused: no planning session"
    assert _plan_jobs(db) == []

    # Clearing the gate lets the ordinary reconciler submit on the next tick.
    _tick(db)
    assert len(_plan_jobs(db)) == 2


# ---------------------------------------------------------------------------
# MCP surface
# ---------------------------------------------------------------------------


def test_mcp_submit_plan_is_path_only_and_repo_confined(
    rig: RigEnvironment, planner: Any
) -> None:
    path = _write_plan(rig, LANES_A_B_C)
    db = rig.db()
    ledger = EventLedger()
    registry = build_default_registry(
        ServerContext(
            db_path=db.db_path,
            questions_store=rig.root / "questions.json",
            ledger=ledger,
            lease=planner,
        )
    )
    payload = registry.call(
        "submit_plan", {"repo": str(rig.repo), "plan_path": str(path)}
    )
    assert payload["plan_id"] == PLAN_ID
    assert set(payload["submitted"]) == {"A", "C"}
    assert payload["v"] == 1

    events = ledger.events()
    assert [event.type for event in events].count("plan_submitted") == 1
    lane_events = [event for event in events if event.type == "plan_lane_submitted"]
    assert {event.payload["lane_id"] for event in lane_events} == {"A", "C"}

    with pytest.raises(InvalidParamsError):
        registry.call(
            "submit_plan", {"repo": str(rig.repo), "plan_path": "../outside.md"}
        )

    # Re-submitting identical content is a replay: no new jobs, no ledger
    # conflict, no duplicate lane events.
    replay = registry.call(
        "submit_plan", {"repo": str(rig.repo), "plan_path": str(path)}
    )
    assert replay["submitted"] == []
    assert len(_plan_jobs(db)) == 2
    assert len(ledger.events()) == len(events)


def test_plan_events_are_recorded_once(rig: RigEnvironment, planner: Any) -> None:
    db = rig.db()
    _activate(rig, LANES_A_B_C)
    reconcile_plan_lanes(db)
    reconcile_plan_lanes(db)
    with db.owner_db._get_connection() as conn:
        rows = conn.execute(
            "SELECT kind, event_key FROM plan_events WHERE plan_id = ?", (PLAN_ID,)
        ).fetchall()
    keys = [str(row["event_key"]) for row in rows]
    assert len(keys) == len(set(keys))
    kinds = {str(row["kind"]) for row in rows}
    assert "plan_submitted" in kinds
    assert "plan_lane_submitted" in kinds
