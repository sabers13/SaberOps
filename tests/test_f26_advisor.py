"""F26 (ADR-0034): advisor checkpoints, a stronger model on call, never coding.

These tests use the real-process lifecycle rig and its fake CLI shims.  The
advisor route is a fake plan-subscription provider; no real vendor CLI or
network call is made.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from lifecycle_rig import RigEnvironment, build_lifecycle_rig

from saberops.advisor import (
    ADVISOR_ADVICE_EVENT,
    ADVISOR_CALL_EVENT,
    ADVISOR_SKIPPED_EVENT,
    ADVISOR_UNAVAILABLE_EVENT,
    CHECKPOINT_BEFORE_PLAN,
    CHECKPOINT_BEFORE_REVIEW,
    CHECKPOINT_REPEAT_FAILURE,
    advisor_before_attempt,
    advisor_view_for_job,
    load_advisor_route,
    set_advisor_route,
)
from saberops.capacity.loop import run_scheduler_loop
from saberops.db import Database, current_iso_timestamp
from saberops.git import GitManager
from saberops.jobs import service as job_service
from saberops.jobs.models import Job
from saberops.jobs.service import (
    JobDispatchSpec,
    get_job,
    steering_notes,
    submit_job,
)
from saberops.mcp.evidence import EvidenceRoots
from saberops.mcp.lease import OrchestratorLease
from saberops.mcp.ledger import EventLedger
from saberops.mcp.tools import ServerContext, build_default_registry
from saberops.model_access.readiness_service import ReadinessService
from saberops.models import DispatchStage, Run, RunStatus, Tier
from saberops.paths import owner_state_dir
from saberops.runtime_gate import set_pause
from saberops.workers.registry import AdapterRegistry

pytestmark = pytest.mark.packaged_routing_default

ADVICE = "ADVICE-MARKER-f26: verify the retry helper before adding a new one."
ADVISOR_MODEL = "gpt-5.6-sol"


@pytest.fixture()
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RigEnvironment:
    return build_lifecycle_rig(tmp_path, monkeypatch)


def _counted_gate(tmp_path: Path, *, failures: int) -> str:
    """A gate command that fails ``failures`` times, then passes."""
    counter = tmp_path / "gate-count"
    script = tmp_path / "gate_script.py"
    script.write_text(
        "import pathlib, sys\n"
        f"counter = pathlib.Path({str(counter)!r})\n"
        "seen = int(counter.read_text()) if counter.exists() else 0\n"
        "counter.write_text(str(seen + 1))\n"
        f"sys.exit(0 if seen >= {failures} else 1)\n",
        encoding="utf-8",
    )
    return f"{sys.executable} {script}"


def _run_job(
    rig: RigEnvironment,
    *,
    goal: str,
    max_attempts: int = 3,
    review_limit: int = 3,
) -> tuple[str, str]:
    """Submit one job and let the scheduler dispatch it with tuned limits.

    A job-dispatched run owns a durable job row, which is what the steering
    channel (F13) delivers advisor notes through.  The tuning is applied to the
    frozen dispatch request exactly the way C2 does it.
    """
    original = job_service.job_dispatch_spec

    def tuned(job: Job) -> JobDispatchSpec:
        spec = original(job)
        return replace(
            spec,
            request=replace(
                spec.request,
                max_attempts=max_attempts,
                review_limit=review_limit,
                worker_timeout=20.0,
                gate_timeout=20.0,
                review_enabled=True,
                review_timeout=20.0,
            ),
        )

    rig.monkeypatch.setattr(job_service, "job_dispatch_spec", tuned)
    db = rig.db()
    submitted = submit_job(db, repo=rig.repo, goal=goal)
    readiness = ReadinessService.for_owner_state(owner_state_dir())
    tick = run_scheduler_loop(db, once=True, readiness_service=readiness)
    assert tick is not None and tick.ran and tick.dispatched == 1
    job = get_job(db, submitted.job.job_id)
    assert job is not None and job.current_run_id is not None
    rig.wait_for_run(job.current_run_id, timeout=180.0)
    return job.job_id, job.current_run_id


def _event_checkpoints(db: Database, run_id: str) -> list[str]:
    return [
        str((event.payload or {}).get("checkpoint"))
        for event in db.get_events_by_type(run_id, ADVISOR_CALL_EVENT)
    ]


def _prompts(path: Path) -> list[str]:
    return [
        json.loads(line)["prompt"]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _worker_prompts(path: Path) -> list[str]:
    return [
        prompt for prompt in _prompts(path) if not prompt.startswith("[ADVISOR CHECKPOINT")
    ]


def _advisor_prompts(path: Path) -> list[str]:
    return [
        prompt for prompt in _prompts(path) if prompt.startswith("[ADVISOR CHECKPOINT")
    ]


def _catch_up_payload(rig: RigEnvironment, job_id: str) -> dict[str, Any]:
    state_dir = rig.state_home / "saberops"
    context = ServerContext(
        db_path=rig.db().db_path,
        questions_store=state_dir / "questions.json",
        config_ledger_path=state_dir / "config.jsonl",
        evidence=EvidenceRoots(roots=(rig.root,)),
        ledger=EventLedger(state_dir / "events.jsonl"),
        lease=OrchestratorLease(state_dir / "lease.json"),
    )
    registry = build_default_registry(context)
    return registry.call("catch_up", {"job_id": job_id})


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )
    return proc.stdout.strip()


def test_each_checkpoint_fires_once_and_advice_reaches_the_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """before_plan, repeat_failure and before_review each fire once."""
    gate = _counted_gate(tmp_path, failures=2)
    rig = build_lifecycle_rig(tmp_path, monkeypatch, gate_command=gate)
    db = rig.db()
    prompt_log = rig.root / "advisor-prompts.jsonl"
    rig.set_worker_mode("commit", report_text=ADVICE, prompt_log=prompt_log)
    rig.set_reviewer_verdict("BLOCK")
    set_advisor_route(rig.repo, provider="codex", model=ADVISOR_MODEL, call_budget=3)

    job_id, run_id = _run_job(
        rig, goal="Add the advisor test line", max_attempts=3, review_limit=2
    )
    run = db.get_run(run_id)
    assert run is not None
    assert run.status in {RunStatus.COMPLETED, RunStatus.FAILED}

    assert _event_checkpoints(db, run_id) == [
        CHECKPOINT_BEFORE_PLAN,
        CHECKPOINT_REPEAT_FAILURE,
        CHECKPOINT_BEFORE_REVIEW,
    ]
    advice_events = db.get_events_by_type(run_id, ADVISOR_ADVICE_EVENT)
    assert len(advice_events) == 3
    assert all((event.payload or {}).get("advice") == ADVICE for event in advice_events)
    assert all(event.attempt_id for event in advice_events)

    # Every advice reached a worker at an attempt boundary, never mid-tool.
    worker_prompts = _worker_prompts(prompt_log)
    assert len(worker_prompts) >= 4, "expected three attempts plus one review repair"
    assert "ADVISOR (before_plan" in worker_prompts[0]
    assert "ADVISOR (repeat_failure" in worker_prompts[2]
    assert any("ADVISOR (before_review" in prompt for prompt in worker_prompts[3:])

    # Advice is recorded as advisor-sourced steering notes and delivered once.
    advisor_notes = [note for note in steering_notes(db, job_id) if note.source == "advisor"]
    assert len(advisor_notes) == 3
    assert all(note.delivered for note in advisor_notes)
    assert len({note.delivered_attempt_id for note in advisor_notes}) == 3

    # Advisor calls are plan-limited usage events (F2), separate from worker calls.
    usage = [
        row
        for row in db.get_model_call_usage(run_id)
        if row.stage is DispatchStage.ADVISOR
    ]
    assert len(usage) == 3
    assert all(row.provider == "codex" and row.model == ADVISOR_MODEL for row in usage)

    # catch_up shows the same advice from durable records.
    caught_up = _catch_up_payload(rig, job_id)
    advisor = caught_up["advisor"]
    assert advisor["enabled"] is True
    assert advisor["calls"] == 3
    assert len(advisor["advice"]) == 3
    assert all(entry["delivered"] is True for entry in advisor["advice"])
    # The return summary carries the same projection, never a second record.
    assert caught_up["return_summary"]["advisor"]["calls"] == 3


def test_advisor_off_by_default_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No route means no advisor call, no note and no extra usage row."""
    rig = build_lifecycle_rig(tmp_path, monkeypatch)
    db = rig.db()
    prompt_log = rig.root / "off-prompts.jsonl"
    rig.set_worker_mode("commit", prompt_log=prompt_log)
    assert load_advisor_route(rig.repo) is None

    job_id, run_id = _run_job(rig, goal="Ship without an advisor", max_attempts=1)
    run = db.get_run(run_id)
    assert run is not None and run.status is RunStatus.COMPLETED

    for event_type in (
        ADVISOR_CALL_EVENT,
        ADVISOR_ADVICE_EVENT,
        ADVISOR_SKIPPED_EVENT,
        ADVISOR_UNAVAILABLE_EVENT,
    ):
        assert db.get_events_by_type(run_id, event_type) == []
    assert [
        row for row in db.get_model_call_usage(run_id) if row.stage is DispatchStage.ADVISOR
    ] == []
    for prompt in _worker_prompts(prompt_log):
        assert "ADVISOR" not in prompt
    assert _advisor_prompts(prompt_log) == []
    assert [note for note in steering_notes(db, job_id) if note.source == "advisor"] == []

    job = get_job(db, job_id)
    assert job is not None
    assert advisor_view_for_job(db, job)["enabled"] is False


def test_budget_caps_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A call budget of one allows exactly one advisor checkpoint call."""
    gate = _counted_gate(tmp_path, failures=99)
    rig = build_lifecycle_rig(tmp_path, monkeypatch, gate_command=gate)
    db = rig.db()
    rig.set_worker_mode("commit", report_text=ADVICE)
    set_advisor_route(rig.repo, provider="codex", model=ADVISOR_MODEL, call_budget=1)

    _job_id, run_id = _run_job(rig, goal="Always failing gate", max_attempts=3)
    assert _event_checkpoints(db, run_id) == [CHECKPOINT_BEFORE_PLAN]
    assert len(db.get_events_by_type(run_id, ADVISOR_ADVICE_EVENT)) == 1


def test_repeated_identical_failure_advises_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three identical gate failures produce one repeat_failure call, not one per retry."""
    gate = _counted_gate(tmp_path, failures=99)
    rig = build_lifecycle_rig(tmp_path, monkeypatch, gate_command=gate)
    db = rig.db()
    rig.set_worker_mode("commit", report_text=ADVICE)
    # A budget larger than the number of meaningful checkpoints so a per-retry
    # call would be visible instead of hidden by the cap.
    set_advisor_route(rig.repo, provider="codex", model=ADVISOR_MODEL, call_budget=5)

    _job_id, run_id = _run_job(rig, goal="Identical failures", max_attempts=4)
    checkpoints = _event_checkpoints(db, run_id)
    assert checkpoints == [CHECKPOINT_BEFORE_PLAN, CHECKPOINT_REPEAT_FAILURE]
    assert checkpoints.count(CHECKPOINT_REPEAT_FAILURE) == 1
    assert len(db.get_events_by_type(run_id, ADVISOR_CALL_EVENT)) == 2


def test_paused_store_calls_no_advisor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A closed work gate refuses the advisor before any process starts."""
    rig = build_lifecycle_rig(tmp_path, monkeypatch)
    db = rig.db()
    prompt_log = rig.root / "paused-prompts.jsonl"
    rig.set_worker_mode("commit", report_text=ADVICE, prompt_log=prompt_log)
    set_advisor_route(rig.repo, provider="codex", model=ADVISOR_MODEL, call_budget=3)
    set_pause("owner_pause", "test pause")

    db.create_run(
        Run(
            id="run_paused_advisor",
            task="paused task",
            target_repo=str(rig.repo),
            base_commit=rig.base_sha,
            tier=Tier.T1,
            training_allowed=True,
            gate_command=rig.gate_command,
            status=RunStatus.RUNNING,
            created_at=current_iso_timestamp(),
        )
    )
    advice = advisor_before_attempt(
        db,
        repo=rig.repo,
        run_id="run_paused_advisor",
        registry=AdapterRegistry.default(),
        attempt_id="run_paused_advisor_a1",
        attempts=[],
        plan_steps=[],
        base_sha=rig.base_sha,
        task="paused task",
    )
    assert advice is None
    assert db.get_events_by_type("run_paused_advisor", ADVISOR_CALL_EVENT) == []
    assert db.get_events_by_type("run_paused_advisor", ADVISOR_ADVICE_EVENT) == []
    skipped = db.get_events_by_type("run_paused_advisor", ADVISOR_SKIPPED_EVENT)
    assert len(skipped) == 1
    assert (skipped[0].payload or {})["reason"] == "paused"
    assert not prompt_log.exists() or prompt_log.read_text(encoding="utf-8") == ""


def test_unavailable_route_continues_without_advice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unavailable advisor route is recorded, and the job still completes."""
    rig = build_lifecycle_rig(tmp_path, monkeypatch)
    db = rig.db()
    rig.set_worker_mode("commit", report_text=ADVICE)
    # ``gemini`` has no plan-subscription adapter, so the advisor is refused.
    set_advisor_route(rig.repo, provider="gemini", model="strong-model", call_budget=3)

    job_id, run_id = _run_job(
        rig, goal="Run without advisor availability", max_attempts=1
    )
    run = db.get_run(run_id)
    assert run is not None and run.status is RunStatus.COMPLETED
    assert db.get_events_by_type(run_id, ADVISOR_CALL_EVENT) == []
    assert db.get_events_by_type(run_id, ADVISOR_ADVICE_EVENT) == []
    unavailable = db.get_events_by_type(run_id, ADVISOR_UNAVAILABLE_EVENT)
    # One visible refusal at each checkpoint the run reached, and no advice.
    assert {
        str((event.payload or {}).get("checkpoint")) for event in unavailable
    } == {CHECKPOINT_BEFORE_PLAN, CHECKPOINT_BEFORE_REVIEW}
    assert all(
        "plan subscription" in str((event.payload or {})["reason"])
        for event in unavailable
    )

    job = get_job(db, job_id)
    assert job is not None
    view = advisor_view_for_job(db, job)
    assert view["enabled"] is True
    assert view["calls"] == 0
    assert len(view["unavailable"]) == len(unavailable)


def test_advisor_never_produces_a_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even a fake advisor that ignores read-only leaves no commit behind."""
    rig = build_lifecycle_rig(tmp_path, monkeypatch)
    db = rig.db()
    rig.set_worker_mode("commit", report_text=ADVICE, ignore_read_only=True)
    set_advisor_route(rig.repo, provider="codex", model=ADVISOR_MODEL, call_budget=3)

    _job_id, run_id = _run_job(rig, goal="Advisor must not commit", max_attempts=1)
    run = db.get_run(run_id)
    assert run is not None and run.status is RunStatus.COMPLETED
    assert len(db.get_events_by_type(run_id, ADVISOR_ADVICE_EVENT)) == 2

    # The candidate holds exactly the worker's one commit: no advisor commit.
    attempts = db.get_attempts_for_run(run_id)
    candidate_sha = attempts[-1].commit_sha
    assert candidate_sha is not None
    count = _git(rig.repo, "rev-list", "--count", f"{rig.base_sha}..{candidate_sha}")
    assert count == "1", "the advisor produced an extra commit"

    # No advisor scratch worktree survives, and the target branch never moved.
    assert not [
        wt for wt in GitManager.list_worktrees(rig.repo) if "advisor-" in wt.path
    ]
    assert _git(rig.repo, "rev-parse", "main") == rig.base_sha


def test_cli_sets_shows_and_clears_the_advisor_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner-facing CLI is the one write path for the advisor route."""
    rig = build_lifecycle_rig(tmp_path, monkeypatch)
    assert load_advisor_route(rig.repo) is None

    shown = rig.run_cli("project", "advisor", "show", "--repo", str(rig.repo), "--json")
    assert shown.returncode == 0, shown.stderr
    assert json.loads(shown.stdout)["enabled"] is False

    set_result = rig.run_cli(
        "project",
        "advisor",
        "set",
        "codex",
        ADVISOR_MODEL,
        "--calls",
        "2",
        "--repo",
        str(rig.repo),
    )
    assert set_result.returncode == 0, set_result.stderr
    route = load_advisor_route(rig.repo)
    assert route is not None
    assert (route.provider, route.model, route.call_budget) == ("codex", ADVISOR_MODEL, 2)

    shown = rig.run_cli("project", "advisor", "show", "--repo", str(rig.repo), "--json")
    assert shown.returncode == 0, shown.stderr
    payload = json.loads(shown.stdout)
    assert payload["enabled"] is True
    assert payload["route"]["route"] == f"codex/{ADVISOR_MODEL}"
    assert payload["call_budget"] == 2

    refused = rig.run_cli(
        "project", "advisor", "set", "gemini", "strong-model", "--repo", str(rig.repo)
    )
    assert refused.returncode == 2
    assert "plan subscription" in refused.stderr

    off = rig.run_cli("project", "advisor", "off", "--repo", str(rig.repo))
    assert off.returncode == 0, off.stderr
    assert load_advisor_route(rig.repo) is None


def test_api_key_billing_route_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The advisor never runs on a provider the owner allowed for API billing."""
    from saberops.control_plane.policy import (
        get_control_plane_config_path,
        save_owner_control_plane,
    )

    rig = build_lifecycle_rig(tmp_path, monkeypatch)
    db = rig.db()
    rig.set_worker_mode("commit", report_text=ADVICE)
    set_advisor_route(rig.repo, provider="codex", model=ADVISOR_MODEL, call_budget=3)
    # Preserve the rig's postures/quota evidence and add only the API-billing
    # allow-list, the exact owner decision the advisor must refuse.
    document = json.loads(get_control_plane_config_path().read_text(encoding="utf-8"))
    document["api_billing"] = {"providers": ["codex"]}
    save_owner_control_plane(document)

    _job_id, run_id = _run_job(rig, goal="No advisor on API billing", max_attempts=1)
    assert db.get_events_by_type(run_id, ADVISOR_CALL_EVENT) == []
    assert db.get_events_by_type(run_id, ADVISOR_ADVICE_EVENT) == []
    unavailable = db.get_events_by_type(run_id, ADVISOR_UNAVAILABLE_EVENT)
    assert unavailable
    assert all(
        "API key" in str((event.payload or {})["reason"]) for event in unavailable
    )
