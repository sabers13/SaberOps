"""Phase 1 lane P3: ``STATE.md``, the rendered progress ledger.

The rig builds a real isolated store and toy repository.  These tests exercise
the owner-visible behaviour the lane promises:

* one projection and one renderer show the records (worker/model, branch,
  commit, evidence), distinguishing ``candidate`` from ``accepted`` and
  ``owner`` from ``queued``;
* the canonical file updates within one scheduler tick after accept and fail,
  and is rewritten only when the projection digest changes;
* ``orch plan-file adopt`` creates a git-ignored ``STATE.md`` symlink in the
  canonical checkout, leaves ``git status --porcelain`` empty, never touches a
  worker worktree, and acceptance still works afterwards;
* a refreshed run is linked into the job's run chain so acceptance and the
  ledger follow the refreshed candidate;
* MCP ``plan_status`` returns the same values from a read-only client, and
  ``catch_up`` accepts an optional ``plan_id`` without changing the job shape.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from lifecycle_rig import RigEnvironment, build_lifecycle_rig

from saberops.accept import AcceptEngine
from saberops.capacity.loop import run_scheduler_loop
from saberops.cli import main as cli_main
from saberops.config import RunConfig
from saberops.db import Database, current_iso_timestamp
from saberops.jobs import store
from saberops.jobs.dependencies import job_is_accepted
from saberops.jobs.models import RUN_FAILED, Job, JobState
from saberops.mcp.errors import InvalidParamsError
from saberops.mcp.lease import OrchestratorLease
from saberops.mcp.ledger import EventLedger
from saberops.mcp.tools import (
    MUTATING_TOOLS,
    READ_TOOLS,
    ServerContext,
    build_default_registry,
)
from saberops.model_access.readiness_service import ReadinessService
from saberops.models import (
    Attempt,
    AttemptStatus,
    CheckResult,
    ReviewResult,
    ReviewVerdict,
    Run,
    RunStatus,
    Tier,
)
from saberops.paths import owner_state_dir
from saberops.plan_runner import get_plan, project_plan, submit_plan_file
from saberops.plan_state import PlanStateError, adopt_state_file
from saberops.provenance import mint_run_provenance
from saberops.run_creation import RunRequest, resolve_effective_run_config
from saberops.supervisor import RunSupervisor

pytestmark = pytest.mark.packaged_routing_default

PLAN_ID = "p3-rig"


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
    _git(rig.repo, "commit", "-m", f"plan: activate {plan_id}")
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


def _lane_job(db: Database, lane_id: str) -> Job | None:
    plan = get_plan(db, PLAN_ID)
    assert plan is not None
    view = project_plan(db, plan).view(lane_id)
    assert view is not None
    if view.job_id is None:
        return None
    return store.get_job(db, view.job_id)


def _finish_lane(
    rig: RigEnvironment,
    db: Database,
    job: Job,
    *,
    accept: bool,
    provider: str = "codex",
    model: str = "gpt-5.6-terra",
    verdict: ReviewVerdict = ReviewVerdict.PASS,
) -> str:
    """Finish one lane on a real run row with attempt, check and review."""
    current = store.get_job(db, job.job_id)
    assert current is not None
    provenance = mint_run_provenance(rig.repo, f"p3 lane {job.job_id}")
    run_id = current.current_run_id or provenance.run_id
    if current.current_run_id is None:
        store.mark_dispatched(db, job.job_id, run_id)
    base = current.base_sha or rig.base_sha
    now = current_iso_timestamp()
    db.create_run(
        Run(
            id=run_id,
            task=f"p3 lane {job.job_id}",
            target_repo=str(rig.repo),
            base_commit=base,
            tier=Tier.T1,
            training_allowed=True,
            gate_command=rig.gate_command,
            status=RunStatus.COMPLETED,
            created_at=now,
            completed_at=now,
            final_attempt_id=f"{run_id}_a1",
            project_id=provenance.project_id,
            project_identity_json=provenance.identity.to_json(),
            objective_id=provenance.objective_id,
            config_json=json.dumps({"review_enabled": True}),
        )
    )
    db.create_attempt(
        Attempt(
            id=f"{run_id}_a1",
            run_id=run_id,
            attempt_number=1,
            provider=provider,
            model=model,
            worktree_path=str(rig.root / "worktrees" / run_id / "a1"),
            branch_name=f"orch/{run_id}/a1",
            status=AttemptStatus.SUCCESS,
            commit_sha=base,
            created_at=now,
            completed_at=now,
            base_sha=base,
        )
    )
    db.create_check(
        CheckResult(
            id=f"check_{run_id}",
            attempt_id=f"{run_id}_a1",
            gate_command=rig.gate_command,
            exit_code=0,
            stdout="ok",
            stderr="",
            passed=True,
            created_at=now,
        )
    )
    db.create_review(
        ReviewResult(
            id=f"review_{run_id}",
            run_id=run_id,
            provider="anthropic",
            model="claude-sonnet-4-6",
            verdict=verdict,
            summary="independent review",
            details="",
            created_at=now,
        )
    )
    store.reconcile_transition(db, job.job_id, to=JobState.SUCCEEDED)
    if accept:
        db.record_event(run_id, "run_accepted", payload={"candidate_sha": base})
    return run_id


def _fail(db: Database, job: Job) -> None:
    current = store.get_job(db, job.job_id)
    assert current is not None
    if current.state is JobState.QUEUED:
        store.mark_dispatched(db, job.job_id, "run_p3_failed")
    store.reconcile_transition(db, job.job_id, to=JobState.FAILED, failure_reason=RUN_FAILED)


def _state_file(rig: RigEnvironment) -> Path:
    plan = get_plan(rig.db(), PLAN_ID)
    assert plan is not None
    return (
        owner_state_dir()
        / "plans"
        / plan.project_id
        / plan.plan_id
        / "state.md"
    )


def _plan_row(text: str, lane_id: str) -> str:
    for line in text.splitlines():
        if line.startswith(f"| {lane_id} |"):
            return line
    raise AssertionError(f"lane {lane_id} missing from:\n{text}")


def _run_real_job(rig: RigEnvironment) -> Run:
    rig.set_worker_mode("commit", target_file="lane.txt", content="lane\n")
    config: RunConfig = resolve_effective_run_config(
        RunRequest(
            task="p3 real lane",
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
    return rig.wait_for_run(started.run_id, timeout=90)


LANES: list[dict[str, Any]] = [
    {"id": "A", "title": "Root lane", "wave": "0"},
    {"id": "B", "title": "Dependent lane", "needs": ["C"], "wave": "1"},
    {"id": "O", "title": "Owner check", "run": "owner", "wave": "2"},
    {"id": "C", "title": "Independent lane", "wave": "0"},
]


# ---------------------------------------------------------------------------
# Done-when: the ledger matches the records
# ---------------------------------------------------------------------------


def test_state_ledger_matches_records(rig: RigEnvironment, planner: Any) -> None:
    db = rig.db()
    _activate(rig, LANES)

    a_job = _lane_job(db, "A")
    c_job = _lane_job(db, "C")
    assert a_job is not None and c_job is not None
    run_id = _finish_lane(rig, db, a_job, accept=True)
    _finish_lane(rig, db, c_job, accept=False, provider="opencode", model="deepseek-v4-flash")

    plan = get_plan(db, PLAN_ID)
    assert plan is not None

    from saberops.plan_state import project_plan_state, render_plan_state_markdown

    projection = project_plan_state(db, plan)
    text = render_plan_state_markdown(projection)

    accepted = projection.lane("A")
    candidate = projection.lane("C")
    held = projection.lane("B")
    owner = projection.lane("O")
    assert accepted is not None and candidate is not None
    assert held is not None and owner is not None

    assert accepted.state == "accepted"
    assert accepted.job_id == a_job.job_id
    assert accepted.worker == "codex"
    assert accepted.model == "gpt-5.6-terra"
    assert accepted.branch == f"orch/{run_id}/a1"
    assert accepted.commit == a_job.base_sha
    assert accepted.run_id == run_id
    assert accepted.check_id == f"check_{run_id}"
    assert accepted.review_id == f"review_{run_id}"

    assert candidate.state == "candidate"
    assert candidate.worker == "opencode"
    assert candidate.model == "deepseek-v4-flash"

    assert held.state == "held"
    assert "dependency_pending" in held.reasons
    assert owner.state == "owner"
    assert owner.owner is True

    a_row = _plan_row(text, "A")
    assert "| accepted |" in a_row
    assert "codex/gpt-5.6-terra" in a_row
    assert f"orch/{run_id}/a1" in a_row
    assert a_job.base_sha in a_row
    assert f"run:{run_id}" in a_row
    assert f"check:check_{run_id}" in a_row
    assert f"review:review_{run_id}" in a_row
    assert f"/runs/{run_id}" in a_row

    assert "| candidate |" in _plan_row(text, "C")
    assert "| owner |" in _plan_row(text, "O")
    assert "| held |" in _plan_row(text, "B")
    assert "## Landing queue" in text
    assert "1. C - candidate ready for accept" in text


def test_projection_reads_a_project_local_run(
    rig: RigEnvironment, planner: Any
) -> None:
    """Run state may live in the owning project store, not the owner database."""
    from saberops.plan_state import project_plan_state
    from saberops.runtime import project_db_path_for_repo

    db = rig.db()
    _activate(rig, [{"id": "A", "title": "Root lane"}])
    job = _lane_job(db, "A")
    plan = get_plan(db, PLAN_ID)
    assert job is not None and plan is not None

    provenance = mint_run_provenance(rig.repo, "p3 project run")
    run_id = provenance.run_id
    base = job.base_sha
    now = current_iso_timestamp()
    project_db = Database(project_db_path_for_repo(rig.repo), owner_db=db)
    project_db.create_run(
        Run(
            id=run_id,
            task="p3 project run",
            target_repo=str(rig.repo),
            base_commit=base,
            tier=Tier.T1,
            training_allowed=True,
            gate_command=rig.gate_command,
            status=RunStatus.COMPLETED,
            created_at=now,
            completed_at=now,
            final_attempt_id=f"{run_id}_a1",
            project_id=provenance.project_id,
            project_identity_json=provenance.identity.to_json(),
            objective_id=provenance.objective_id,
            config_json=json.dumps({"review_enabled": True}),
        )
    )
    project_db.create_attempt(
        Attempt(
            id=f"{run_id}_a1",
            run_id=run_id,
            attempt_number=1,
            provider="opencode",
            model="deepseek-v4-flash",
            worktree_path=str(rig.root / "worktrees" / run_id / "a1"),
            branch_name=f"orch/{run_id}/a1",
            status=AttemptStatus.SUCCESS,
            commit_sha=base,
            created_at=now,
            completed_at=now,
            base_sha=base,
        )
    )
    project_db.create_check(
        CheckResult(
            id=f"check_{run_id}",
            attempt_id=f"{run_id}_a1",
            gate_command=rig.gate_command,
            exit_code=0,
            stdout="ok",
            stderr="",
            passed=True,
            created_at=now,
        )
    )
    project_db.create_review(
        ReviewResult(
            id=f"review_{run_id}",
            run_id=run_id,
            provider="anthropic",
            model="claude-sonnet-4-6",
            verdict=ReviewVerdict.PASS,
            summary="independent review",
            details="",
            created_at=now,
        )
    )
    project_db.record_event(run_id, "run_accepted", payload={"candidate_sha": base})
    store.mark_dispatched(db, job.job_id, run_id)
    store.reconcile_transition(db, job.job_id, to=JobState.SUCCEEDED)

    lane = project_plan_state(db, plan).lane("A")
    assert lane is not None
    assert lane.state == "accepted"
    assert lane.worker == "opencode"
    assert lane.model == "deepseek-v4-flash"
    assert lane.branch == f"orch/{run_id}/a1"
    assert lane.commit == base
    assert lane.check_id == f"check_{run_id}"
    assert lane.review_id == f"review_{run_id}"


def test_state_file_updates_within_one_tick_after_accept_and_fail(
    rig: RigEnvironment, planner: Any
) -> None:
    db = rig.db()
    _activate(rig, LANES)
    path = _state_file(rig)
    assert path.is_file()
    assert "| queued (NEW) |" in _plan_row(path.read_text(encoding="utf-8"), "A")

    a_job = _lane_job(db, "A")
    c_job = _lane_job(db, "C")
    assert a_job is not None and c_job is not None
    _finish_lane(rig, db, a_job, accept=False)

    _tick(db)
    text = path.read_text(encoding="utf-8")
    assert "| candidate |" in _plan_row(text, "A")
    assert "1. A - candidate ready for accept" in text

    # An unchanged projection is not rewritten.
    from saberops.plan_state import render_active_plan_states

    render_active_plan_states(db)  # settle to the post-tick record state
    assert render_active_plan_states(db) == ()

    # A failure becomes visible within the next tick.
    _fail(db, c_job)
    _tick(db)
    text = path.read_text(encoding="utf-8")
    assert "| failed (RUN_FAILED) |" in _plan_row(text, "C")
    assert "- C - failed (RUN_FAILED)" in text
    assert "Accept lane A's candidate" in text

    # An acceptance becomes visible within the next tick.
    refreshed = store.get_job(db, a_job.job_id)
    assert refreshed is not None
    run_id = refreshed.current_run_id
    assert run_id is not None
    db.record_event(run_id, "run_accepted", payload={"candidate_sha": a_job.base_sha})
    _tick(db)
    text = path.read_text(encoding="utf-8")
    assert "| accepted |" in _plan_row(text, "A")
    assert "1. A - candidate ready for accept" not in text
    assert "Refresh lane" not in text


# ---------------------------------------------------------------------------
# Done-when: adoption keeps the checkout clean and acceptance working
# ---------------------------------------------------------------------------


def test_adopt_creates_ignored_link_and_accept_still_works(
    rig: RigEnvironment, planner: Any
) -> None:
    db = rig.db()
    _activate(rig, [{"id": "A", "title": "Root lane"}])
    plan = get_plan(db, PLAN_ID)
    assert plan is not None

    result = adopt_state_file(db, plan)
    link = rig.repo / "STATE.md"
    assert link.is_symlink()
    assert Path(result["canonical"]) == _state_file(rig)
    assert Path(result["canonical"]).is_file()
    assert link.read_text(encoding="utf-8").startswith("# Plan state: p3-rig")
    assert _git(rig.repo, "status", "--porcelain") == ""
    exclude = (rig.repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert "STATE.md" in exclude.splitlines()

    # Idempotent: a second adopt changes nothing and still refuses a real file.
    again = adopt_state_file(db, plan)
    assert Path(again["link"]) == link
    assert _git(rig.repo, "status", "--porcelain") == ""

    # A real file is never replaced.
    (rig.repo / "STATE.md").unlink()
    (rig.repo / "STATE.md").write_text("owner notes\n", encoding="utf-8")
    with pytest.raises(PlanStateError):
        adopt_state_file(db, plan)
    (rig.repo / "STATE.md").unlink()
    assert _git(rig.repo, "status", "--porcelain") == ""

    # A worker worktree never receives the link.
    linked = rig.root / "linked-worktree"
    linked.mkdir()
    (linked / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")
    with pytest.raises(PlanStateError):
        adopt_state_file(db, replace(plan, repo=str(linked)))
    assert not (linked / "STATE.md").exists()

    # The clean tree is exactly the acceptance guard, and a real accept works.
    assert _git(rig.repo, "status", "--porcelain") == ""
    run = _run_real_job(rig)
    assert run.status is RunStatus.COMPLETED, run.result_summary
    assert _git(rig.repo, "status", "--porcelain") == ""
    accepted = AcceptEngine(db).accept_run(run.id)
    assert accepted.accepted is True
    attempt = db.get_attempts_for_run(run.id)[-1]
    assert not (Path(attempt.worktree_path) / "STATE.md").exists()


# ---------------------------------------------------------------------------
# Refresh chain
# ---------------------------------------------------------------------------


def test_refresh_links_the_new_run_into_the_job_chain(
    rig: RigEnvironment, planner: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    db = rig.db()
    _activate(rig, [{"id": "A", "title": "Root lane"}])
    job = _lane_job(db, "A")
    assert job is not None
    source = _run_real_job(rig)
    store.mark_dispatched(db, job.job_id, source.id)
    store.reconcile_transition(db, job.job_id, to=JobState.SUCCEEDED)
    before = store.get_job(db, job.job_id)
    assert before is not None and before.run_ids == (source.id,)

    (rig.repo / "owner.txt").write_text("moved\n", encoding="utf-8")
    _git(rig.repo, "add", "owner.txt")
    _git(rig.repo, "commit", "-m", "owner moves target")

    code = cli_main(["refresh", source.id, "--db", str(db.db_path), "--json"])
    out = capsys.readouterr().out
    payload = json.loads(out[out.index("{") :])
    assert code == 0
    refreshed = payload["run_id"]
    assert refreshed != source.id

    after = store.get_job(db, job.job_id)
    assert after is not None
    assert after.run_ids == (source.id, refreshed)
    assert job_is_accepted(db, after) is False
    db.record_event(refreshed, "run_accepted", payload={"candidate_sha": payload["candidate_sha"]})
    after = store.get_job(db, job.job_id)
    assert after is not None and job_is_accepted(db, after) is True


# ---------------------------------------------------------------------------
# MCP read paths
# ---------------------------------------------------------------------------


def _registry(rig: RigEnvironment, db: Database) -> Any:
    return build_default_registry(
        ServerContext(
            db_path=db.db_path,
            questions_store=rig.root / "questions.json",
            ledger=EventLedger(),
            lease=OrchestratorLease(),
        )
    )


def test_plan_status_matches_the_file_from_a_read_only_client(
    rig: RigEnvironment, planner: Any
) -> None:
    db = rig.db()
    _activate(rig, LANES)
    a_job = _lane_job(db, "A")
    assert a_job is not None
    _finish_lane(rig, db, a_job, accept=True)

    plan = get_plan(db, PLAN_ID)
    assert plan is not None
    from saberops.plan_state import (
        plan_state_path,
        project_plan_state,
        render_plan_state,
    )

    render_plan_state(db, plan, force=True)
    text = plan_state_path(plan).read_text(encoding="utf-8")
    expected = project_plan_state(db, plan).to_dict()

    assert "plan_status" in READ_TOOLS
    assert "plan_status" not in MUTATING_TOOLS
    registry = _registry(rig, db)
    payload = registry.call("plan_status", {"plan_id": PLAN_ID})
    assert payload["v"] == 1
    payload.pop("v")
    assert payload["plan_id"] == expected["plan_id"]
    assert payload["digest"] == expected["digest"]
    assert {lane["id"]: lane["state"] for lane in payload["lanes"]} == {
        lane["id"]: lane["state"] for lane in expected["lanes"]
    }

    # A read-only client never writes the canonical file.
    plan_state_path(plan).unlink()
    by_repo = registry.call("plan_status", {"repo": str(rig.repo)})
    assert by_repo["plan_id"] == PLAN_ID
    assert not plan_state_path(plan).exists()

    for lane in expected["lanes"]:
        row = _plan_row(text, lane["id"])
        assert lane["state"] in row or f"({lane['state']})" in row
        if lane["commit"]:
            assert lane["commit"] in row

    with pytest.raises(InvalidParamsError):
        registry.call("plan_status", {})


def test_catch_up_accepts_an_optional_plan_id(
    rig: RigEnvironment, planner: Any
) -> None:
    db = rig.db()
    _activate(rig, LANES)
    a_job = _lane_job(db, "A")
    assert a_job is not None
    registry = _registry(rig, db)

    plan_payload = registry.call("catch_up", {"plan_id": PLAN_ID})
    assert plan_payload["plan_id"] == PLAN_ID
    assert plan_payload["project_id"]
    assert {lane["id"] for lane in plan_payload["lanes"]} == {"A", "B", "O", "C"}
    assert plan_payload["next_action"]
    assert plan_payload["state"]["digest"]

    job_payload = registry.call("catch_up", {"job_id": a_job.job_id})
    assert job_payload["job_id"] == a_job.job_id
    assert "state" in job_payload and "candidate" in job_payload
    assert "plan_id" not in job_payload

    with pytest.raises(InvalidParamsError):
        registry.call("catch_up", {})


# ---------------------------------------------------------------------------
# CLI parity
# ---------------------------------------------------------------------------


def test_cli_status_render_and_adopt(
    rig: RigEnvironment, planner: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    db = rig.db()
    _activate(rig, [{"id": "A", "title": "Root lane"}])
    plan = get_plan(db, PLAN_ID)
    assert plan is not None
    a_job = _lane_job(db, "A")
    assert a_job is not None
    _finish_lane(rig, db, a_job, accept=True)

    status_code = cli_main(["plan-file", "status", "--plan", PLAN_ID])
    status_out = capsys.readouterr().out
    assert status_code == 0
    assert "# Plan state: p3-rig" in status_out
    assert "| accepted |" in _plan_row(status_out, "A")

    json_code = cli_main(["plan-file", "status", "--json"])
    json_out = capsys.readouterr().out
    assert json_code == 0
    payload = json.loads(json_out)
    assert payload["plan_id"] == PLAN_ID
    assert payload["lanes"][0]["state"] == "accepted"

    out_path = rig.root / "rendered-state.md"
    assert cli_main(["plan-file", "render", "--plan", PLAN_ID, "--out", str(out_path)]) == 0
    assert out_path.is_file()
    assert "# Plan state: p3-rig" in out_path.read_text(encoding="utf-8")

    assert cli_main(["plan-file", "adopt", "--plan", PLAN_ID]) == 0
    assert (rig.repo / "STATE.md").is_symlink()
    assert _git(rig.repo, "status", "--porcelain") == ""
