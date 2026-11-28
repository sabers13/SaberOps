"""F26 (ADR-0034): on-demand advisor, a stronger model on call, never coding.

These tests use the real-process lifecycle rig and its fake CLI shims.  The
advisor route is a fake plan-subscription provider; no real vendor CLI or
network call is made.  The fake worker asks from its own worktree through
``orch advisor ask`` exactly as a headless worker does.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from lifecycle_rig import RigEnvironment, build_lifecycle_rig

from saberops.advisor import (
    ADVISOR_ADVICE_EVENT,
    ADVISOR_CALL_EVENT,
    ADVISOR_SKIPPED_EVENT,
    ADVISOR_UNAVAILABLE_EVENT,
    DEFAULT_ADVISOR_TIMEOUT_SECONDS,
    MAX_ADVISOR_POLL_SECONDS,
    AdvisorRefused,
    advisor_route_path,
    advisor_view_for_job,
    ask_advisor,
    load_advisor_route,
    set_advisor_route,
)
from saberops.capacity.loop import run_scheduler_loop
from saberops.db import current_iso_timestamp
from saberops.git import GitManager
from saberops.jobs import service as job_service
from saberops.jobs.models import Job
from saberops.jobs.service import (
    JobDispatchSpec,
    get_job,
    submit_job,
)
from saberops.mcp.errors import ToolError
from saberops.mcp.evidence import EvidenceRoots
from saberops.mcp.lease import OrchestratorLease
from saberops.mcp.ledger import EventLedger
from saberops.mcp.tools import ServerContext, build_default_registry
from saberops.model_access.readiness_service import ReadinessService
from saberops.models import Attempt, AttemptStatus, DispatchStage, Run, RunStatus, Tier
from saberops.paths import owner_state_dir
from saberops.runtime_gate import set_pause
from saberops.workers.registry import AdapterRegistry

pytestmark = pytest.mark.packaged_routing_default

ADVICE = "ADVICE-MARKER-f26: verify the retry helper before adding a new one."
ADVISOR_MODEL = "gpt-5.6-sol"
QUESTION = "Should the retry helper be reused?"


@pytest.fixture()
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RigEnvironment:
    return build_lifecycle_rig(tmp_path, monkeypatch)


def _run_job(
    rig: RigEnvironment,
    *,
    goal: str,
    max_attempts: int = 3,
    review_limit: int = 3,
) -> tuple[str, str]:
    """Submit one job and let the scheduler dispatch it with tuned limits."""
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


def _prompts(path: Path) -> list[str]:
    return [
        json.loads(line)["prompt"]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _advisor_calls(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _registry_for(rig: RigEnvironment) -> Any:
    state_dir = rig.state_home / "saberops"
    context = ServerContext(
        db_path=rig.db().db_path,
        repo=rig.repo,
        questions_store=state_dir / "questions.json",
        config_ledger_path=state_dir / "config.jsonl",
        evidence=EvidenceRoots(roots=(rig.root,)),
        ledger=EventLedger(state_dir / "events.jsonl"),
        lease=OrchestratorLease(state_dir / "lease.json"),
    )
    return build_default_registry(context)


def _poll_advisor(
    registry: Any, job_id: str, call_id: str, *, wait_seconds: float = 10.0
) -> dict[str, Any]:
    """Poll one started call, bounded, exactly as a planner does (ADR-0114)."""
    result: dict[str, Any] = {"status": "running", "call_id": call_id}
    deadline = time.monotonic() + 60.0
    while result["status"] == "running":
        assert time.monotonic() < deadline, f"advisor call {call_id} never finished"
        result = registry.call(
            "ask_advisor",
            {"job_id": job_id, "call_id": call_id, "wait_seconds": wait_seconds},
        )
    return result


def _ask_over_mcp(
    registry: Any,
    job_id: str,
    question: str,
    *,
    wait_seconds: float = 10.0,
) -> dict[str, Any]:
    """Start one advisor call over MCP and poll it to an answer."""
    started = registry.call(
        "ask_advisor",
        {"job_id": job_id, "question": question, "wait_seconds": wait_seconds},
    )
    if started["status"] == "answered":
        return dict(started)
    assert started["status"] in {"started", "running"}, started
    return _poll_advisor(
        registry, job_id, started["call_id"], wait_seconds=wait_seconds
    )


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )
    return proc.stdout.strip()


def test_worker_asks_and_gets_advice_within_the_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker asks from its worktree; three calls advise, the fourth refuses."""
    rig = build_lifecycle_rig(tmp_path, monkeypatch)
    db = rig.db()
    prompt_log = rig.root / "worker-prompts.jsonl"
    advisor_log = rig.root / "advisor-calls.jsonl"
    rig.set_worker_mode(
        "commit",
        report_text=ADVICE,
        prompt_log=prompt_log,
        advisor_questions=["q1", "q2", "q3", "q4"],
        advisor_log=advisor_log,
    )
    set_advisor_route(rig.repo, provider="codex", model=ADVISOR_MODEL, call_budget=3)

    job_id, run_id = _run_job(rig, goal="Ask the advisor", max_attempts=1, review_limit=1)
    run = db.get_run(run_id)
    assert run is not None and run.status is RunStatus.COMPLETED

    # The prompt names the command while the route is on.
    assert any("[ADVISOR]" in prompt for prompt in _prompts(prompt_log))

    # Calls one to three print the advice; the fourth is refused with a reason.
    calls = _advisor_calls(advisor_log)
    assert [entry["returncode"] for entry in calls] == [0, 0, 0, 3]
    assert all(entry["stdout"] == ADVICE for entry in calls[:3])
    assert "budget exhausted" in calls[3]["stderr"]
    assert "3 of 3" in calls[3]["stderr"]

    # Every call and every piece of advice is recorded on the run itself.
    recorded = db.get_events_by_type(run_id, ADVISOR_CALL_EVENT)
    assert len(recorded) == 3
    assert all((event.payload or {})["checkpoint"] == "on_demand" for event in recorded)
    advice_events = db.get_events_by_type(run_id, ADVISOR_ADVICE_EVENT)
    assert len(advice_events) == 3
    assert all((event.payload or {})["advice"] == ADVICE for event in advice_events)
    assert all((event.payload or {})["question"] for event in advice_events)
    assert all(event.attempt_id for event in advice_events)
    skipped = db.get_events_by_type(run_id, ADVISOR_SKIPPED_EVENT)
    assert len(skipped) == 1
    assert (skipped[0].payload or {})["reason"] == "budget exhausted"

    # Advisor calls are plan-limited usage rows (F2), separate from workers.
    usage = [
        row
        for row in db.get_model_call_usage(run_id)
        if row.stage is DispatchStage.ADVISOR
    ]
    assert len(usage) == 3
    assert all(row.provider == "codex" and row.model == ADVISOR_MODEL for row in usage)

    # The one projection reads the same durable records.
    job = get_job(db, job_id)
    assert job is not None
    view = advisor_view_for_job(db, job)
    assert view["enabled"] is True
    assert view["calls"] == 3
    assert len(view["advice"]) == 3
    assert view["call_budget"] == 3


def test_off_means_no_tool_and_no_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a route there is no MCP tool, and the worker command refuses."""
    rig = build_lifecycle_rig(tmp_path, monkeypatch)
    db = rig.db()
    advisor_log = rig.root / "off-advisor.jsonl"
    rig.set_worker_mode(
        "commit",
        advisor_questions=["is the advisor there?"],
        advisor_log=advisor_log,
    )
    assert load_advisor_route(rig.repo) is None

    # "Off means no tool": the project session cannot even list it.
    assert "ask_advisor" not in _registry_for(rig).names()

    job_id, run_id = _run_job(rig, goal="Ship without an advisor", max_attempts=1)
    run = db.get_run(run_id)
    assert run is not None and run.status is RunStatus.COMPLETED
    calls = _advisor_calls(advisor_log)
    assert len(calls) == 1
    assert calls[0]["returncode"] == 2
    assert "advisor is off" in calls[0]["stderr"]

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


def test_planner_asks_over_mcp_and_the_run_records_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The planner's MCP call returns advice and appends the same evidence."""
    rig = build_lifecycle_rig(tmp_path, monkeypatch)
    db = rig.db()
    rig.set_worker_mode("commit", report_text=ADVICE)
    set_advisor_route(rig.repo, provider="codex", model=ADVISOR_MODEL, call_budget=3)

    job_id, run_id = _run_job(rig, goal="Plan an advice question", max_attempts=1)
    run = db.get_run(run_id)
    assert run is not None and run.status is RunStatus.COMPLETED

    registry = _registry_for(rig)
    assert "ask_advisor" in registry.names()
    result = _ask_over_mcp(registry, job_id, QUESTION)
    assert result["status"] == "answered"
    assert result["advice"] == ADVICE
    assert result["provider"] == "codex"
    advice = db.get_events_by_type(run_id, ADVISOR_ADVICE_EVENT)
    assert len(advice) == 1
    assert (advice[0].payload or {})["question"] == QUESTION
    # The start record carries what a poll needs to tell running from lost.
    started = db.get_events_by_type(run_id, ADVISOR_CALL_EVENT)
    assert len(started) == 1
    payload = started[0].payload or {}
    assert payload["started_at"]
    assert payload["timeout_seconds"] == DEFAULT_ADVISOR_TIMEOUT_SECONDS

    # The per-job budget is shared with the worker path.
    _ask_over_mcp(registry, job_id, "second")
    _ask_over_mcp(registry, job_id, "third")
    with pytest.raises(ToolError) as refused:
        registry.call("ask_advisor", {"job_id": job_id, "question": "fourth"})
    assert "ADVISOR_BUDGET_EXHAUSTED" in str(refused.value)


def test_h56_a_slow_advisor_never_runs_inside_the_planner_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first call returns a handle; the advice arrives on a later poll.

    This is the shape that fixes H37 N7: a 5 s fake turn cannot finish inside
    the start call, so the planner gets ``started`` plus a ``call_id`` and a
    zero-wait poll reports it still running instead of an empty result.
    """
    rig = build_lifecycle_rig(tmp_path, monkeypatch)
    db = rig.db()
    rig.set_worker_mode("commit", report_text=ADVICE, advisor_delay_seconds=5)
    set_advisor_route(rig.repo, provider="codex", model=ADVISOR_MODEL)
    job_id, run_id = _run_job(rig, goal="Poll a slow advisor", max_attempts=1)

    registry = _registry_for(rig)
    started = registry.call("ask_advisor", {"job_id": job_id, "question": QUESTION})
    assert started["status"] == "started"
    assert started["job_id"] == job_id and started["run_id"] == run_id
    assert started["call_id"] == "advisor_1"
    assert "advice" not in started
    assert "call_id" in started["next_action"]

    running = registry.call(
        "ask_advisor",
        {"job_id": job_id, "call_id": started["call_id"], "wait_seconds": 0},
    )
    assert running["status"] == "running"
    assert running["call_budget"] == 3

    result = _poll_advisor(registry, job_id, started["call_id"])
    assert result["status"] == "answered"
    assert result["advice"] == ADVICE
    advice = db.get_events_by_type(run_id, ADVISOR_ADVICE_EVENT)
    assert len(advice) == 1
    assert (advice[0].payload or {})["call_id"] == "advisor_1"


def test_h56_a_refused_call_is_polled_to_its_typed_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A call that fails in the background still reports why, once."""
    rig = build_lifecycle_rig(tmp_path, monkeypatch)
    db = rig.db()
    rig.set_worker_mode("commit", report_text=ADVICE, advisor_delay_seconds=3600)
    set_advisor_route(
        rig.repo, provider="codex", model=ADVISOR_MODEL, timeout_seconds=2
    )
    job_id, run_id = _run_job(rig, goal="Poll a hung advisor", max_attempts=1)

    registry = _registry_for(rig)
    started = registry.call("ask_advisor", {"job_id": job_id, "question": QUESTION})
    assert started["status"] == "started"
    with pytest.raises(ToolError) as refused:
        registry.call(
            "ask_advisor",
            {"job_id": job_id, "call_id": started["call_id"], "wait_seconds": 10},
        )
    message = str(refused.value)
    assert "ADVISOR_UNAVAILABLE" in message
    assert f"advisor route codex/{ADVISOR_MODEL} timed out after 2s" in message
    unavailable = db.get_events_by_type(run_id, ADVISOR_UNAVAILABLE_EVENT)
    assert len(unavailable) == 1
    assert (unavailable[0].payload or {})["call_id"] == "advisor_1"


def test_h56_a_call_lost_with_its_engine_is_reported_not_polled_forever(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A poll after the engine restarted says ask again, it never hangs."""
    rig = build_lifecycle_rig(tmp_path, monkeypatch)
    db = rig.db()
    rig.set_worker_mode("commit", report_text=ADVICE)
    set_advisor_route(rig.repo, provider="codex", model=ADVISOR_MODEL)
    job_id, run_id = _run_job(rig, goal="A lost advisor call", max_attempts=1)

    old = datetime.now(UTC) - timedelta(hours=1)
    db.record_event(
        run_id,
        ADVISOR_CALL_EVENT,
        payload={
            "checkpoint": "on_demand",
            "call_id": "advisor_9",
            "provider": "codex",
            "model": ADVISOR_MODEL,
            "call_index": 9,
            "call_budget": 3,
            "timeout_seconds": 2.0,
            "started_at": old.isoformat(),
            "question": QUESTION,
        },
    )
    registry = _registry_for(rig)
    result = registry.call(
        "ask_advisor",
        {"job_id": job_id, "call_id": "advisor_9", "wait_seconds": 0},
    )
    assert result["status"] == "lost"
    assert "ask again" in str(result["reason"])

    with pytest.raises(ToolError):
        registry.call(
            "ask_advisor",
            {"job_id": job_id, "call_id": "advisor_does_not_exist", "wait_seconds": 0},
        )


@pytest.mark.slow
@pytest.mark.timeout(420)
def test_h56_an_advisor_slower_than_the_client_cap_still_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H37 N7: a turn slower than Codex's 300 s cap answers through polls.

    The fake advisor takes 301 s, longer than the tightest measured client call
    cap (300 s).  Every tools/call stays under the 240 s poll bound, the handle
    comes back immediately, and the answer is recorded on the run.
    """
    rig = build_lifecycle_rig(tmp_path, monkeypatch)
    db = rig.db()
    rig.set_worker_mode("commit", report_text=ADVICE, advisor_delay_seconds=301)
    set_advisor_route(rig.repo, provider="codex", model=ADVISOR_MODEL)
    job_id, run_id = _run_job(rig, goal="A very slow advisor", max_attempts=1)

    registry = _registry_for(rig)
    started = registry.call("ask_advisor", {"job_id": job_id, "question": QUESTION})
    assert started["status"] == "started"
    call_id = started["call_id"]
    durations = [0.0]
    status: dict[str, Any] = started
    while status["status"] in {"started", "running"}:
        tick = time.monotonic()
        status = registry.call(
            "ask_advisor",
            {
                "job_id": job_id,
                "call_id": call_id,
                "wait_seconds": MAX_ADVISOR_POLL_SECONDS,
            },
        )
        durations.append(time.monotonic() - tick)
        assert len(durations) <= 4, status
    assert status["status"] == "answered", status
    assert status["advice"] == ADVICE
    # No single tool call approached Codex's measured 300 s cap.
    assert max(durations) < 300.0
    assert all(duration <= MAX_ADVISOR_POLL_SECONDS + 5 for duration in durations)
    advice = db.get_events_by_type(run_id, ADVISOR_ADVICE_EVENT)
    assert len(advice) == 1
    assert (advice[0].payload or {})["advice"] == ADVICE


def test_paused_store_refuses_the_advisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A closed work gate refuses the advisor before any process starts."""
    rig = build_lifecycle_rig(tmp_path, monkeypatch)
    db = rig.db()
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
    db.create_attempt(
        Attempt(
            id="run_paused_advisor_a1",
            run_id="run_paused_advisor",
            attempt_number=1,
            provider="codex",
            model=ADVISOR_MODEL,
            worktree_path=str(rig.repo),
            branch_name="advisor-paused",
            status=AttemptStatus.RUNNING,
            created_at=current_iso_timestamp(),
            base_sha=rig.base_sha,
        )
    )
    with pytest.raises(AdvisorRefused) as refused:
        ask_advisor(
            db,
            repo=rig.repo,
            run_id="run_paused_advisor",
            registry=AdapterRegistry.default(),
            attempt_id="run_paused_advisor_a1",
            task="paused task",
            question=QUESTION,
            base_sha=rig.base_sha,
        )
    assert refused.value.code == "ADVISOR_PAUSED"
    assert db.get_events_by_type("run_paused_advisor", ADVISOR_CALL_EVENT) == []
    assert db.get_events_by_type("run_paused_advisor", ADVISOR_ADVICE_EVENT) == []
    skipped = db.get_events_by_type("run_paused_advisor", ADVISOR_SKIPPED_EVENT)
    assert len(skipped) == 1
    assert (skipped[0].payload or {})["reason"] == "paused"


def test_unavailable_route_is_refused_and_the_job_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unavailable route is recorded, refused, and the job still completes."""
    rig = build_lifecycle_rig(tmp_path, monkeypatch)
    db = rig.db()
    advisor_log = rig.root / "unavailable-advisor.jsonl"
    rig.set_worker_mode(
        "commit",
        advisor_questions=[QUESTION],
        advisor_log=advisor_log,
    )
    # ``gemini`` has no plan-subscription adapter, so the advisor is refused.
    set_advisor_route(rig.repo, provider="gemini", model="strong-model", call_budget=3)

    job_id, run_id = _run_job(rig, goal="Run without advisor availability", max_attempts=1)
    run = db.get_run(run_id)
    assert run is not None and run.status is RunStatus.COMPLETED
    calls = _advisor_calls(advisor_log)
    assert calls[0]["returncode"] == 2
    assert "plan subscription" in calls[0]["stderr"]
    assert db.get_events_by_type(run_id, ADVISOR_CALL_EVENT) == []
    assert db.get_events_by_type(run_id, ADVISOR_ADVICE_EVENT) == []
    unavailable = db.get_events_by_type(run_id, ADVISOR_UNAVAILABLE_EVENT)
    assert len(unavailable) == 1
    assert "plan subscription" in str((unavailable[0].payload or {})["reason"])

    job = get_job(db, job_id)
    assert job is not None
    view = advisor_view_for_job(db, job)
    assert view["enabled"] is True
    assert view["calls"] == 0
    assert len(view["unavailable"]) == 1


def test_advisor_never_produces_a_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even a fake advisor that ignores read-only leaves no commit behind."""
    rig = build_lifecycle_rig(tmp_path, monkeypatch)
    db = rig.db()
    advisor_log = rig.root / "readonly-advisor.jsonl"
    rig.set_worker_mode(
        "commit",
        report_text=ADVICE,
        ignore_read_only=True,
        advisor_questions=[QUESTION],
        advisor_log=advisor_log,
    )
    set_advisor_route(rig.repo, provider="codex", model=ADVISOR_MODEL, call_budget=3)

    _job_id, run_id = _run_job(rig, goal="Advisor must not commit", max_attempts=1)
    run = db.get_run(run_id)
    assert run is not None and run.status is RunStatus.COMPLETED
    assert len(db.get_events_by_type(run_id, ADVISOR_ADVICE_EVENT)) == 1
    assert _advisor_calls(advisor_log)[0]["returncode"] == 0

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


def _advisor_run(rig: RigEnvironment) -> tuple[Any, str, str]:
    """One run and attempt the advisor can answer about, without a dispatch."""
    db = rig.db()
    run_id = "run_h49_advisor"
    attempt_id = "run_h49_advisor_a1"
    db.create_run(
        Run(
            id=run_id,
            task="h49 advisor budget",
            target_repo=str(rig.repo),
            base_commit=rig.base_sha,
            tier=Tier.T1,
            training_allowed=True,
            gate_command=rig.gate_command,
            status=RunStatus.RUNNING,
            created_at=current_iso_timestamp(),
        )
    )
    db.create_attempt(
        Attempt(
            id=attempt_id,
            run_id=run_id,
            attempt_number=1,
            provider="codex",
            model=ADVISOR_MODEL,
            worktree_path=str(rig.repo),
            branch_name="h49-advisor-budget",
            status=AttemptStatus.RUNNING,
            created_at=current_iso_timestamp(),
            base_sha=rig.base_sha,
        )
    )
    return db, run_id, attempt_id


@pytest.mark.slow
def test_h49_slow_advisor_answers_within_its_own_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An advisor turn past the old 120 s bound still answers (ADR-0107).

    H37 saw a real advisor turn outlive 120 s and be reported as
    ``ADVISOR_UNAVAILABLE: advisor call returned TIMEOUT``.  The advisor now
    carries its own 600 s bound, so a 121 s fake turn is advice, not a timeout.
    """
    rig = build_lifecycle_rig(tmp_path, monkeypatch)
    db, run_id, attempt_id = _advisor_run(rig)
    rig.set_worker_mode("commit", report_text=ADVICE, advisor_delay_seconds=121)
    set_advisor_route(rig.repo, provider="codex", model=ADVISOR_MODEL)

    advice = ask_advisor(
        db,
        repo=rig.repo,
        run_id=run_id,
        registry=AdapterRegistry.default(),
        attempt_id=attempt_id,
        task="h49 advisor budget",
        question=QUESTION,
        base_sha=rig.base_sha,
    )
    assert advice.advice == ADVICE
    recorded = db.get_events_by_type(run_id, ADVISOR_ADVICE_EVENT)
    assert len(recorded) == 1


def test_h49_hung_advisor_ends_naming_its_route_and_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hung advisor still ends, and the refusal names the route and the bound."""
    rig = build_lifecycle_rig(tmp_path, monkeypatch)
    db, run_id, attempt_id = _advisor_run(rig)
    rig.set_worker_mode("commit", report_text=ADVICE, advisor_delay_seconds=3600)
    set_advisor_route(rig.repo, provider="codex", model=ADVISOR_MODEL, timeout_seconds=2)

    with pytest.raises(AdvisorRefused) as refused:
        ask_advisor(
            db,
            repo=rig.repo,
            run_id=run_id,
            registry=AdapterRegistry.default(),
            attempt_id=attempt_id,
            task="h49 advisor bound",
            question=QUESTION,
            base_sha=rig.base_sha,
        )
    assert refused.value.code == "ADVISOR_UNAVAILABLE"
    expected = f"advisor route codex/{ADVISOR_MODEL} timed out after 2s"
    assert expected in str(refused.value)
    unavailable = db.get_events_by_type(run_id, ADVISOR_UNAVAILABLE_EVENT)
    assert len(unavailable) == 1
    payload = unavailable[0].payload or {}
    assert payload["reason"] == expected
    assert payload["route"] == f"codex/{ADVISOR_MODEL}"
    assert payload["timeout_seconds"] == 2.0
    assert db.get_events_by_type(run_id, ADVISOR_ADVICE_EVENT) == []


def test_h49_route_timeout_is_configurable_and_old_documents_keep_working(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The call bound is a route setting, and a pre-ADR-0107 document still reads."""
    rig = build_lifecycle_rig(tmp_path, monkeypatch)
    route = set_advisor_route(
        rig.repo, provider="codex", model=ADVISOR_MODEL, timeout_seconds=90
    )
    assert route.timeout_seconds == 90.0
    loaded = load_advisor_route(rig.repo)
    assert loaded is not None and loaded.timeout_seconds == 90.0

    # An older document (schema v1, written before the field existed) keeps
    # working on the stated default, which is the bound the old code lacked.
    path = advisor_route_path(rig.repo)
    document = json.loads(path.read_text(encoding="utf-8"))
    del document["timeout_seconds"]
    path.write_text(json.dumps(document), encoding="utf-8")
    old = load_advisor_route(rig.repo)
    assert old is not None
    assert old.timeout_seconds == DEFAULT_ADVISOR_TIMEOUT_SECONDS
    assert old.timeout_seconds > 120.0

    set_result = rig.run_cli(
        "project",
        "advisor",
        "set",
        "codex",
        ADVISOR_MODEL,
        "--timeout",
        "45",
        "--repo",
        str(rig.repo),
    )
    assert set_result.returncode == 0, set_result.stderr
    shown = json.loads(
        rig.run_cli("project", "advisor", "show", "--repo", str(rig.repo), "--json").stdout
    )
    assert shown["timeout_seconds"] == 45.0
    assert shown["route"]["timeout_seconds"] == 45.0

    refused = rig.run_cli(
        "project",
        "advisor",
        "set",
        "codex",
        ADVISOR_MODEL,
        "--timeout",
        "0",
        "--repo",
        str(rig.repo),
    )
    assert refused.returncode == 1
    assert "timeout_seconds" in refused.stderr


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
    advisor_log = rig.root / "api-billing-advisor.jsonl"
    rig.set_worker_mode(
        "commit",
        report_text=ADVICE,
        advisor_questions=[QUESTION],
        advisor_log=advisor_log,
    )
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
    calls = _advisor_calls(advisor_log)
    assert calls[0]["returncode"] == 2
    assert "API key" in calls[0]["stderr"]
