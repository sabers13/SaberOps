"""Phase 1 lane P6: deterministic landing and stale-sibling refresh.

The owner's plan files describe parallel lanes; every acceptance moves the
target and invalidates the candidates that were built on the old base.  This
lane makes that cost explicit and bounded:

* the landing queue is deterministic (wave, then lane id) and the ledger names
  the exact ``orch refresh <run>`` command for a stale candidate;
* an accepted lane marks its siblings stale without touching their evidence;
* owner-enabled ``auto_refresh`` rebuilds stale candidates one per tick in
  landing order through E6 (fresh gate, fresh independent review), records
  ``plan_lane_refreshed`` and never accepts or pushes;
* a failed refresh is a visible lane failure (``refresh-failed``) that stops
  the queue instead of retrying every tick or stalling silently;
* closing the planning CLI pauses refresh spending with everything else.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from lifecycle_rig import RigEnvironment, build_lifecycle_rig

from saberops.accept import AcceptEngine
from saberops.capacity.loop import run_scheduler_loop
from saberops.cli import main as cli_main
from saberops.config import RunConfig
from saberops.db import Database
from saberops.jobs import store
from saberops.jobs.dependencies import job_is_accepted
from saberops.jobs.models import Job, JobState
from saberops.mcp.lease import OrchestratorLease
from saberops.model_access.readiness_service import ReadinessService
from saberops.models import Run, RunStatus
from saberops.paths import owner_state_dir
from saberops.plan_runner import (
    get_plan,
    latest_lane_refresh_event,
    plan_target_head,
    project_plan,
    refresh_stale_plan_candidates,
    set_auto_refresh,
    submit_plan_file,
    wave_sort_key,
)
from saberops.plan_state import plan_state_path, project_plan_state
from saberops.run_creation import RunRequest, resolve_effective_run_config
from saberops.runtime_gate import mark_presence_known, refresh_gate
from saberops.supervisor import RunSupervisor

pytestmark = pytest.mark.packaged_routing_default

PLAN_ID = "p6-rig"
LANES: list[dict[str, Any]] = [
    {"id": "A", "title": "First lane", "wave": "0"},
    {"id": "B", "title": "Second lane", "wave": "0"},
    {"id": "C", "title": "Third lane", "wave": "0"},
]


# ---------------------------------------------------------------------------
# Rig helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[RigEnvironment]:
    environment = build_lifecycle_rig(tmp_path, monkeypatch)
    monkeypatch.delenv("SABEROPS_RUNTIME_GATE", raising=False)
    monkeypatch.setenv("SABEROPS_PLANNING_HEARTBEAT_TIMEOUT", "60")
    yield environment
    try:
        OrchestratorLease().release()
    except Exception:
        pass


@pytest.fixture()
def planner(rig: RigEnvironment) -> Iterator[OrchestratorLease]:
    """One connected planning session: a live, heartbeating lease."""
    lease = OrchestratorLease()
    lease.configure_database(Path(rig.db().db_path))
    assert lease.acquire() is True
    lease.touch()
    mark_presence_known()
    try:
        yield lease
    finally:
        lease.release()


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _lane_block(lane: Mapping[str, Any]) -> list[str]:
    machine: dict[str, Any] = {
        "id": lane["id"],
        "wave": lane.get("wave", "0"),
        "depends_on": [],
        "run": "job",
        "size": "S",
        "effort": "low",
    }
    return [
        f"### {lane['id']}. {lane['title']}",
        "",
        f"- **Why:** {lane.get('why', 'the wave needs this lane')}",
        f"- **Do:** {lane.get('do', 'make the change')}",
        "- **Out of scope:** none",
        "- **Done when:**",
        f"  - {lane.get('done', 'the change is checked')}",
        "- **Needs:** none",
        "",
        "```plan",
        json.dumps(machine, sort_keys=True),
        "```",
        "",
    ]


def _activate(rig: RigEnvironment, lanes: Sequence[Mapping[str, Any]]) -> Any:
    lines = [
        f"# SaberOps plan: {PLAN_ID}",
        "",
        "```plan",
        json.dumps({"format": "saberops-plan/1", "plan_id": PLAN_ID, "base": "main"}),
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
    _git(rig.repo, "commit", "-m", f"plan: activate {PLAN_ID}")
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


def _lane_job(db: Database, lane_id: str) -> Job:
    plan = get_plan(db, PLAN_ID)
    assert plan is not None
    view = project_plan(db, plan).view(lane_id)
    assert view is not None and view.job_id is not None
    job = store.get_job(db, view.job_id)
    assert job is not None
    return job


def _run_lane(
    rig: RigEnvironment, db: Database, lane_id: str, target_file: str, content: str
) -> Run:
    """Run one lane's job for real against the toy repo and mark it succeeded."""
    job = _lane_job(db, lane_id)
    assert job.state is JobState.QUEUED, job.state
    rig.set_worker_mode("commit", target_file=target_file, content=content)
    config: RunConfig = resolve_effective_run_config(
        RunRequest(
            task=f"p6 lane {lane_id}",
            repo_path=rig.repo,
            provider_override="codex",
            model_override="gpt-5.6-terra",
            max_attempts=1,
            worker_timeout=15,
            gate_timeout=15,
            review_enabled=True,
            review_timeout=15,
        )
    )
    started = RunSupervisor(db=rig.db()).start_supervised_run(config)
    run = rig.wait_for_run(started.run_id, timeout=90)
    assert run.status is RunStatus.COMPLETED, run.result_summary
    store.mark_dispatched(db, job.job_id, run.id)
    store.reconcile_transition(db, job.job_id, to=JobState.SUCCEEDED)
    return run


def _prepare_wave(
    rig: RigEnvironment, files: Mapping[str, str]
) -> dict[str, Run]:
    """Activate the plan, run every lane and return its completed run."""
    db = rig.db()
    _activate(rig, LANES[: len(files)])
    plan = get_plan(db, PLAN_ID)
    assert plan is not None
    activation_head = plan_target_head(plan)
    assert activation_head is not None
    runs: dict[str, Run] = {}
    for lane in LANES:
        if lane["id"] not in files:
            continue
        runs[lane["id"]] = _run_lane(
            rig, db, lane["id"], files[lane["id"]], f"{lane['id']}\n"
        )
    # Activating the plan committed PLAN.md, so every lane starts from that
    # commit; nothing else moved the target while the lanes ran.
    assert {run.base_commit for run in runs.values()} == {activation_head}
    assert plan_target_head(plan) == activation_head
    return runs


def _accept(db: Database, run: Run) -> str:
    accepted = AcceptEngine(db).accept_run(run.id)
    assert accepted.accepted is True, accepted.summary
    return accepted.final_sha


def _lanes(db: Database) -> dict[str, Any]:
    plan = get_plan(db, PLAN_ID)
    assert plan is not None
    state = project_plan_state(db, plan)
    return {lane.lane_id: lane for lane in state.lanes}


def _events(db: Database, kind: str) -> list[dict[str, Any]]:
    with db.owner_db._get_connection() as conn:
        rows = conn.execute(
            "SELECT lane_id, payload_json FROM plan_events"
            " WHERE plan_id = ? AND kind = ? ORDER BY id ASC",
            (PLAN_ID, kind),
        ).fetchall()
    return [
        {"lane_id": str(row["lane_id"]), **json.loads(str(row["payload_json"]))}
        for row in rows
    ]


def _run_count(db: Database) -> int:
    return len(db.list_runs(limit=1000))


# ---------------------------------------------------------------------------
# Done-when: an accept marks the siblings stale, off shows the exact command
# ---------------------------------------------------------------------------


def test_accept_marks_siblings_stale_and_off_names_the_command(
    rig: RigEnvironment, planner: Any
) -> None:
    db = rig.db()
    runs = _prepare_wave(rig, {"A": "lane-a.txt", "B": "lane-b.txt", "C": "lane-c.txt"})

    head = _accept(db, runs["A"])
    plan = get_plan(db, PLAN_ID)
    assert plan is not None and plan.auto_refresh is False
    _tick(db)  # the scheduler tick is the single writer of STATE.md
    lanes = _lanes(db)

    assert lanes["A"].state == "accepted"
    assert lanes["B"].state == "stale"
    assert lanes["C"].state == "stale"
    assert lanes["B"].run_id == runs["B"].id
    assert lanes["C"].run_id == runs["C"].id

    text = plan_state_path(plan).read_text(encoding="utf-8")
    assert "Auto-refresh: off" in text
    # Deterministic landing order (wave, then lane id) and the exact command.
    assert f"1. B - stale; `orch refresh {runs['B'].id}`" in text
    assert f"2. C - stale; `orch refresh {runs['C'].id}`" in text
    assert text.index("1. B") < text.index("2. C")
    assert "orch plan-file auto-refresh on --plan p6-rig" in text
    assert f"`orch refresh {runs['B'].id}`" in project_plan_state(db, plan).next_action

    # Nothing was pushed and the target still holds only the accepted lane.
    assert _git(rig.repo, "rev-parse", "HEAD") == head
    assert _git(rig.repo, "remote") == ""
    assert (rig.repo / "lane-a.txt").exists()
    assert not (rig.repo / "lane-b.txt").exists()
    # The stale candidate's own evidence is untouched.
    assert db.get_run(runs["B"].id) is not None
    assert db.get_attempts_for_run(runs["B"].id)


# ---------------------------------------------------------------------------
# Done-when: auto-refresh rebuilds in order, re-gates, re-reviews, never accepts
# ---------------------------------------------------------------------------


def test_auto_refresh_rebuilds_in_landing_order_without_accepting(
    rig: RigEnvironment, planner: Any
) -> None:
    db = rig.db()
    runs = _prepare_wave(rig, {"A": "lane-a.txt", "B": "lane-b.txt", "C": "lane-c.txt"})
    head = _accept(db, runs["A"])
    plan = get_plan(db, PLAN_ID)
    assert plan is not None
    updated = set_auto_refresh(db, plan_id=PLAN_ID, enabled=True)
    assert updated.auto_refresh is True

    old_b = db.get_attempts_for_run(runs["B"].id)[-1]
    old_b_reviews = {review.id for review in db.get_reviews_for_run(runs["B"].id)}
    assert old_b.commit_sha is not None

    # One refresh per tick: the earlier lane first, the later lane untouched.
    _tick(db)
    lanes = _lanes(db)
    assert lanes["B"].state == "candidate"
    assert lanes["C"].state == "stale"
    events = _events(db, "plan_lane_refreshed")
    assert [event["lane_id"] for event in events] == ["B"]
    recorded = latest_lane_refresh_event(db, PLAN_ID, "B")
    assert recorded is not None
    assert recorded["kind"] == "plan_lane_refreshed"
    assert recorded["refreshed_run_id"] == events[0]["refreshed_run_id"]

    refreshed_b = lanes["B"].run_id
    assert refreshed_b is not None and refreshed_b != runs["B"].id
    refreshed_run = db.get_run(refreshed_b)
    assert refreshed_run is not None and refreshed_run.status is RunStatus.COMPLETED
    assert refreshed_run.base_commit == head
    # Re-gated and independently re-reviewed on the refreshed commit.
    new_attempt = db.get_attempts_for_run(refreshed_b)[-1]
    assert new_attempt.commit_sha is not None
    assert new_attempt.commit_sha != old_b.commit_sha
    checks = db.get_checks_for_run(refreshed_b)
    assert checks and checks[-1].passed is True
    new_reviews = {review.id for review in db.get_reviews_for_run(refreshed_b)}
    assert new_reviews and new_reviews.isdisjoint(old_b_reviews)
    # Linked into the lane's run chain and still awaiting the owner.
    job_b = _lane_job(db, "B")
    assert job_b.run_ids == (runs["B"].id, refreshed_b)
    assert job_is_accepted(db, job_b) is False
    assert not db.get_events_by_type(refreshed_b, "run_accepted")
    # The old candidate and its evidence survive.
    assert db.get_run(runs["B"].id) is not None
    assert db.get_attempts_for_run(runs["B"].id)[-1].commit_sha == old_b.commit_sha

    # The next tick refreshes the next stale lane; the target never moves.
    _tick(db)
    lanes = _lanes(db)
    assert lanes["C"].state == "candidate"
    assert [event["lane_id"] for event in _events(db, "plan_lane_refreshed")] == [
        "B",
        "C",
    ]
    assert _git(rig.repo, "rev-parse", "HEAD") == head
    assert _git(rig.repo, "status", "--porcelain") == ""

    # Idempotent: nothing stale remains, so no further refresh and no new run.
    before = _run_count(db)
    _tick(db)
    assert _run_count(db) == before
    assert len(_events(db, "plan_lane_refreshed")) == 2


# ---------------------------------------------------------------------------
# Done-when: a failed refresh is a visible lane failure, not a silent stall
# ---------------------------------------------------------------------------


def test_failed_refresh_is_visible_and_stops_the_queue(
    rig: RigEnvironment, planner: Any
) -> None:
    db = rig.db()
    # B writes the same file as A, so refreshing B onto A's accepted commit
    # conflicts.  C is independent and must wait behind the failure.
    _activate(rig, LANES)
    runs = {
        "A": _run_lane(rig, db, "A", "shared.txt", "first\n"),
        "B": _run_lane(rig, db, "B", "shared.txt", "second\n"),
        "C": _run_lane(rig, db, "C", "lane-c.txt", "C\n"),
    }
    head = _accept(db, runs["A"])
    set_auto_refresh(db, plan_id=PLAN_ID, enabled=True)
    before = _run_count(db)

    _tick(db)

    lanes = _lanes(db)
    assert lanes["B"].state == "refresh-failed"
    assert lanes["B"].refresh_error and "conflict" in lanes["B"].refresh_error.lower()
    assert lanes["C"].state == "stale"
    failures = _events(db, "plan_lane_refresh_failed")
    assert len(failures) == 1
    assert failures[0]["lane_id"] == "B"
    assert failures[0]["source_run_id"] == runs["B"].id
    assert _events(db, "plan_lane_refreshed") == []

    plan = get_plan(db, PLAN_ID)
    assert plan is not None
    text = plan_state_path(plan).read_text(encoding="utf-8")
    assert "refresh-failed (Refresh run_" in text
    assert f"`orch refresh {runs['B'].id}`" in text
    assert "Automatic refresh of lane B failed" in text

    # The same projection is available to a read-only client without writing.
    readonly = Database(db.db_path, read_only=True)
    readonly_plan = get_plan(readonly, PLAN_ID)
    assert readonly_plan is not None
    readonly_lanes = {
        lane.lane_id: lane.state
        for lane in project_plan_state(readonly, readonly_plan).lanes
    }
    assert readonly_lanes["B"] == "refresh-failed"

    # The failed refresh left no partial candidate and the target did not move.
    assert _git(rig.repo, "rev-parse", "HEAD") == head
    assert _git(rig.repo, "status", "--porcelain") == ""
    assert (rig.repo / "shared.txt").read_text(encoding="utf-8") == "first\n"
    failed_runs = [
        run
        for run in db.list_runs(limit=1000)
        if run.status is RunStatus.FAILED and run.id not in {r.id for r in runs.values()}
    ]
    assert len(failed_runs) == 1

    # No retry storm and no leapfrogging: the queue stops at the failure.
    _tick(db)
    assert len(_events(db, "plan_lane_refresh_failed")) == 1
    assert _run_count(db) == before + 1  # only the single failed refresh run
    assert _lanes(db)["C"].state == "stale"


def test_auto_refresh_skips_a_superseded_lane(
    rig: RigEnvironment, planner: Any
) -> None:
    """P4: a changed lane's old candidate is an owner decision, not a refresh."""
    db = rig.db()
    runs = _prepare_wave(rig, {"A": "lane-a.txt", "B": "lane-b.txt"})
    _accept(db, runs["A"])
    set_auto_refresh(db, plan_id=PLAN_ID, enabled=True)
    with db.owner_db._get_connection() as conn:
        conn.execute(
            "UPDATE plan_lanes SET superseded_by = 'next-version'"
            " WHERE plan_id = ? AND lane_id = 'B'",
            (PLAN_ID,),
        )
        conn.commit()
    before = _run_count(db)

    _tick(db)

    assert _run_count(db) == before
    assert _events(db, "plan_lane_refreshed") == []
    assert _lanes(db)["B"].state == "needs-decision"


# ---------------------------------------------------------------------------
# Closing the planning CLI pauses refresh spending
# ---------------------------------------------------------------------------
def test_closed_planning_cli_pauses_auto_refresh(
    rig: RigEnvironment, planner: Any
) -> None:
    db = rig.db()
    runs = _prepare_wave(rig, {"A": "lane-a.txt", "B": "lane-b.txt"})
    head = _accept(db, runs["A"])
    set_auto_refresh(db, plan_id=PLAN_ID, enabled=True)

    # The owner closes the planning CLI and the attended terminal; the gate
    # closes with them (F17 sticky pause, F24 strict presence).
    assert planner.release() is True
    rig.end_attendance()
    gate = refresh_gate(db)
    assert gate.closed is True
    before = _run_count(db)

    result = refresh_stale_plan_candidates(db)
    assert result == () or all(not item.outcomes for item in result)
    _tick(db)
    assert _run_count(db) == before
    assert _events(db, "plan_lane_refreshed") == []
    assert _lanes(db)["B"].state == "stale"
    assert _git(rig.repo, "rev-parse", "HEAD") == head


# ---------------------------------------------------------------------------
# Owner command and deterministic order
# ---------------------------------------------------------------------------


def test_owner_toggles_auto_refresh_from_the_cli(
    rig: RigEnvironment, planner: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    db = rig.db()
    _activate(rig, LANES[:1])

    code = cli_main(["plan-file", "auto-refresh", "on", "--plan", PLAN_ID, "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["auto_refresh"] is True
    plan = get_plan(db, PLAN_ID)
    assert plan is not None and plan.auto_refresh is True
    assert len(_events(db, "plan_auto_refresh_set")) == 1

    status = cli_main(["plan-file", "status", "--plan", PLAN_ID])
    assert status == 0
    text = capsys.readouterr().out
    assert "Auto-refresh: on" in text

    off = cli_main(["plan-file", "auto-refresh", "off", "--plan", PLAN_ID, "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert off == 0 and payload["auto_refresh"] is False
    plan = get_plan(db, PLAN_ID)
    assert plan is not None and plan.auto_refresh is False
    assert len(_events(db, "plan_auto_refresh_set")) == 2

    missing = cli_main(["plan-file", "auto-refresh", "on", "--plan", "nope"])
    assert missing == 1


def test_landing_order_is_wave_then_lane_id() -> None:
    lanes = [("P10", "10"), ("P2", "2"), ("B", "0"), ("A", "0"), ("Z", "next")]
    ordered = sorted(lanes, key=lambda lane: (*wave_sort_key(lane[1]), lane[0]))
    assert [lane[0] for lane in ordered] == ["A", "B", "P2", "P10", "Z"]
