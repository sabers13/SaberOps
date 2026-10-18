"""Wave recovery: run-level handoffs, long-worker wiring and quota preemption.

Covers: WX-2 -- factual handoffs on attempt completion, attempt failure and the
long-worker observation (the existing ``observe_long_worker`` transition wired
into production), and quota preemption/auto-resume for a lane bridged to the
M2-5 durable preemption protocol.

Hermetic: the Git surface, gate executor, event sink, long-worker observer and
preemption port are fakes; the real M2-5 adapter is exercised only with
monkeypatched protocol functions, and the orchestrator seam with a ``tmp_path``
database. No provider, network or real repository is required.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from saberops.gate_runner import EngineGateRequest, EngineGateResult
from saberops.models import HealthState
from saberops.wave_executor import (
    EVENT_ATTEMPT_HANDOFF,
    EVENT_LANE_PREEMPTED,
    EVENT_LANE_RESUMED,
    EVENT_LONG_WORKER_HANDOFF,
    HANDOFF_ATTEMPT_COMPLETED,
    HANDOFF_ATTEMPT_FAILED,
    HANDOFF_LONG_WORKER,
    LANE_STATE_TO_SLOT_STATE,
    REPAIR_ESCALATE,
    DatabaseQuotaPreemption,
    MergeOutcome,
    QuotaPreemptionReceipt,
    QuotaPreemptionRequest,
    RecordingWaveEventSink,
    SupervisorLongWorkerObserver,
    WaveExecutor,
    WaveExecutorError,
)
from saberops.wave_models import Lane, LanePreemption, LaneState, Wave, WaveModelError


def _sha(index: int) -> str:
    return f"{index:040x}"


def _lane(lane_id: str, touches: list[str], **overrides: object) -> Lane:
    base: dict[str, object] = {
        "lane_id": lane_id,
        "task_id": f"task-{lane_id}",
        "prompt": "work",
        "touches": touches,
        "must_not_touch": [],
        "contracts": [],
        "retirement_table": {},
        "worker_route": "worker-chain-main",
        "reviewer_route": "reviewer-chain",
        "effort": "medium",
    }
    base.update(overrides)
    return Lane(**base)  # type: ignore[arg-type]


def _wave(lane_count: int = 1, **overrides: object) -> Wave:
    lanes = [_lane(f"L{i}", [f"src/{i}.py"]) for i in range(1, lane_count + 1)]
    base: dict[str, object] = {
        "wave_id": "wave-1",
        "base_sha": _sha(999),
        "lanes": lanes,
        "merge_order": [lane.lane_id for lane in lanes],
        "named_glue": [],
        "budgets": {},
        "target_branch": "main",
    }
    base.update(overrides)
    return Wave(**base)  # type: ignore[arg-type]


class FakeGit:
    """Deterministic in-memory WaveGitPort."""

    def __init__(self, head: str = _sha(9000)) -> None:
        self.head = head
        self.tips: dict[str, str] = {}

    def resolve_ref(self, repo: str, ref: str) -> str | None:
        return self.tips.get(ref)

    def is_ancestor(self, repo: str, ancestor_sha: str, descendant_sha: str) -> bool:
        return True

    def allocate_worktree(self, repo: str, branch: str, path: str, start_point: str) -> str:
        self.tips.setdefault(branch, start_point)
        return path

    def merge_no_ff(self, worktree: str, sha: str) -> MergeOutcome:
        return MergeOutcome(ok=True, detail="ok")

    def head_sha(self, worktree: str) -> str:
        return self.head


class FakeGate:
    """Deterministic in-memory GateExecutorPort."""

    def execute(self, request: EngineGateRequest) -> EngineGateResult:
        return EngineGateResult(
            kind=str(request.kind),
            producer="engine",
            profile="full",
            outcome="PASS",
            accepted=True,
            reason="projected",
            head_sha=request.head_sha,
            tests=1,
            failed=(),
            flaky=(),
            wall_time=0.1,
            history_path="history.jsonl",
            outcome_path="outcome.json",
        )


class FakeObserver:
    """Records the exact long-worker transition call and returns a health."""

    def __init__(self, health: HealthState = HealthState.HEALTHY) -> None:
        self.health = health
        self.calls: list[dict[str, Any]] = []

    def observe_long_worker(
        self,
        run_id: str,
        *,
        terminal_event_exists: bool,
        recent_activity: bool,
        timed_out: bool = False,
        stalled: bool = False,
    ) -> HealthState:
        self.calls.append(
            {
                "run_id": run_id,
                "terminal_event_exists": terminal_event_exists,
                "recent_activity": recent_activity,
                "timed_out": timed_out,
                "stalled": stalled,
            }
        )
        return self.health


class FakePreemption:
    """Deterministic in-memory QuotaPreemptionPort with a recorded request."""

    def __init__(
        self,
        *,
        checkpoint_sha: str | None = None,
        checkpoint_ref: str = "refs/saberops/checkpoints/job-1/1",
        preemptions: int = 1,
        job_id: str | None = "job-1",
        reset_deadline: str | None = "2026-10-18T12:00:00+00:00",
        not_before: str | None = "2026-10-18T12:00:00+00:00",
    ) -> None:
        self.checkpoint_sha = checkpoint_sha or _sha(7)
        self.checkpoint_ref = checkpoint_ref
        self.preemptions = preemptions
        self.job_id = job_id
        self.reset_deadline = reset_deadline
        self.not_before = not_before
        self.requests: list[QuotaPreemptionRequest] = []

    def preempt(self, request: QuotaPreemptionRequest) -> QuotaPreemptionReceipt:
        self.requests.append(request)
        return QuotaPreemptionReceipt(
            checkpoint_sha=self.checkpoint_sha,
            checkpoint_ref=self.checkpoint_ref,
            preemptions=self.preemptions,
            job_id=self.job_id,
            reset_deadline=self.reset_deadline,
            not_before=self.not_before,
        )


@dataclass
class Harness:
    executor: WaveExecutor
    sink: RecordingWaveEventSink = field(default_factory=RecordingWaveEventSink)
    observer: FakeObserver | None = None
    preemption: FakePreemption | None = None

    def start_lane(self, lane_id: str = "L1") -> None:
        self.executor.start()
        self.executor.start_lane(lane_id)


def _harness(
    wave: Wave | None = None,
    *,
    observer: FakeObserver | None = None,
    preemption: FakePreemption | None = None,
    run_id: str = "run-1",
    budgets: dict[str, object] | None = None,
) -> Harness:
    active_wave = wave or _wave()
    if budgets is not None:
        active_wave = _wave(lane_count=len(active_wave.lanes), budgets=budgets)
    sink = RecordingWaveEventSink()
    executor = WaveExecutor(
        active_wave,
        git=FakeGit(),
        event_sink=sink,
        repo="/repo",
        worktree_base="/worktrees",
        integration_worktree=f"/worktrees/{active_wave.wave_id}/integration",
        run_id=run_id,
        long_worker_observer=observer,
        quota_preemption=preemption,
    )
    return Harness(executor=executor, sink=sink, observer=observer, preemption=preemption)


# -- handoffs ---------------------------------------------------------------


def test_completed_attempt_leaves_a_factual_handoff() -> None:
    """Covers: WX-2 (attempt completion leaves a handoff from durable state)."""
    harness = _harness()
    harness.start_lane()
    handoff = harness.executor.emit_attempt_handoff(
        "L1",
        succeeded=True,
        summary="lane landed a candidate",
        attempt_id="attempt-1",
        candidate_sha=_sha(3),
        artifacts=["tests/test_x.py"],
        tests_passed=12,
        created_at="2026-10-18T10:00:00+00:00",
    )
    assert handoff.kind == HANDOFF_ATTEMPT_COMPLETED
    assert handoff.run_id == "run-1"
    assert handoff.candidate_sha == _sha(3)
    assert handoff.tests_passed == 12
    assert handoff.repair_route is None
    assert handoff.escalate is False
    payloads = harness.sink.payloads(EVENT_ATTEMPT_HANDOFF)
    assert payloads == [handoff.as_payload()]
    assert payloads[0]["kind"] == HANDOFF_ATTEMPT_COMPLETED


def test_failed_attempt_handoff_routes_repair_in_the_same_tier() -> None:
    """Covers: WX-2 (failure handoff routes repair to the same tier)."""
    harness = _harness()
    harness.start_lane()
    handoff = harness.executor.emit_attempt_handoff(
        "L1",
        succeeded=False,
        summary="gate failed",
        attempt_id="attempt-1",
        failed_tests=["tests/test_a.py::test_x"],
        tests_failed=1,
        repair_suggestions=["fix the regression"],
        source_events=["gate_failed"],
        created_at="2026-10-18T10:00:00+00:00",
    )
    assert handoff.kind == HANDOFF_ATTEMPT_FAILED
    assert handoff.failed_tests == ("tests/test_a.py::test_x",)
    assert handoff.repair_suggestions == ("fix the regression",)
    assert handoff.repair_route == "worker-chain-main"
    assert handoff.escalate is False


def test_failed_attempt_escalates_once_the_fix_budget_is_spent() -> None:
    """Covers: WX-2 (no unconstrained repair loop; the owner must decide)."""
    harness = _harness(budgets={"fix_rounds": 1})
    harness.start_lane()
    harness.executor.lane_review("L1")
    harness.executor.lane_fix_up("L1")  # one of one fix rounds used
    harness.executor.lane_fix_up_complete("L1")
    handoff = harness.executor.emit_attempt_handoff(
        "L1",
        succeeded=False,
        summary="still failing",
        attempt_id="attempt-2",
        created_at="2026-10-18T10:00:00+00:00",
    )
    assert handoff.escalate is True
    assert handoff.repair_route == REPAIR_ESCALATE


def test_handoff_requires_a_durable_anchor() -> None:
    """Covers: WX-2 (handoffs are factual, never model prose)."""
    harness = _harness()
    harness.start_lane()
    with pytest.raises(WaveExecutorError):
        harness.executor.emit_attempt_handoff(
            "L1", succeeded=True, summary="no anchor", created_at="2026-10-18T10:00:00+00:00"
        )


def test_handoff_rejects_unknown_lane_and_bad_fields() -> None:
    """Covers: WX-2 (handoff inputs are validated fail-closed)."""
    harness = _harness()
    harness.start_lane()
    with pytest.raises(WaveExecutorError):
        harness.executor.emit_attempt_handoff(
            "missing", succeeded=True, summary="x", attempt_id="a-1"
        )
    with pytest.raises(WaveExecutorError):
        harness.executor.emit_attempt_handoff(
            "L1", succeeded=True, summary="", attempt_id="a-1"
        )
    with pytest.raises(WaveExecutorError):
        harness.executor.emit_attempt_handoff(
            "L1", succeeded=True, summary="x", candidate_sha="not-a-sha"
        )


# -- long-worker observation ------------------------------------------------


def test_long_worker_observation_is_wired_and_emits_a_handoff() -> None:
    """Covers: WX-2 (the observe_long_worker transition wired into production)."""
    observer = FakeObserver(HealthState.HEALTHY)
    harness = _harness(observer=observer)
    harness.start_lane()
    handoff = harness.executor.observe_long_worker(
        "L1",
        terminal_event_exists=False,
        recent_activity=True,
        health_reason="alive_nonterminal_recent_activity",
        created_at="2026-10-18T10:05:00+00:00",
    )
    assert observer.calls == [
        {
            "run_id": "run-1",
            "terminal_event_exists": False,
            "recent_activity": True,
            "timed_out": False,
            "stalled": False,
        }
    ]
    assert handoff.kind == HANDOFF_LONG_WORKER
    assert handoff.health == "healthy"
    assert handoff.health_reason == "alive_nonterminal_recent_activity"
    payloads = harness.sink.payloads(EVENT_LONG_WORKER_HANDOFF)
    assert payloads and payloads[0]["health"] == "healthy"


def test_long_worker_observation_records_a_stall() -> None:
    """Covers: WX-2 (a stalled observation is factual, not a run verdict)."""
    observer = FakeObserver(HealthState.STALLED)
    harness = _harness(observer=observer)
    harness.start_lane()
    handoff = harness.executor.observe_long_worker(
        "L1", terminal_event_exists=False, recent_activity=False, timed_out=True
    )
    assert handoff.health == "stalled"
    assert observer.calls[0]["timed_out"] is True


def test_long_worker_observation_requires_an_observer() -> None:
    """Covers: WX-2 (no hidden polling; the transition must be injected)."""
    harness = _harness()
    harness.start_lane()
    with pytest.raises(WaveExecutorError):
        harness.executor.observe_long_worker(
            "L1", terminal_event_exists=False, recent_activity=True
        )


def test_supervisor_adapter_forwards_the_exact_transition() -> None:
    """Covers: WX-2 (RunSupervisor.observe_long_worker is the production port)."""

    class StubSupervisor:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def observe_long_worker(self, run_id: str, **kwargs: Any) -> HealthState:
            self.calls.append({"run_id": run_id, **kwargs})
            return HealthState.UNCERTAIN

    supervisor = StubSupervisor()
    adapter = SupervisorLongWorkerObserver(supervisor)
    result = adapter.observe_long_worker(
        "run-9", terminal_event_exists=True, recent_activity=False, stalled=True
    )
    assert result is HealthState.UNCERTAIN
    assert supervisor.calls == [
        {
            "run_id": "run-9",
            "terminal_event_exists": True,
            "recent_activity": False,
            "timed_out": False,
            "stalled": True,
        }
    ]


def test_supervisor_adapter_rejects_an_object_without_the_transition() -> None:
    """Covers: WX-2 (the wiring seam fails closed on a bad supervisor)."""
    with pytest.raises(WaveExecutorError):
        SupervisorLongWorkerObserver(object())


# -- quota preemption & auto-resume -----------------------------------------


def test_preempt_lane_parks_with_anchored_checkpoint_and_reset() -> None:
    """Covers: WX-2 + M2-5 (PREEMPTED, checkpoint ref, parsed reset deadline)."""
    preemption = FakePreemption(preemptions=2)
    harness = _harness(preemption=preemption)
    harness.start_lane()
    record = harness.executor.preempt_lane(
        "L1", attempt_terminal=True, fallback_admitted=False, job_id="job-1"
    )
    assert harness.executor.lane_run("L1").state is LaneState.PREEMPTED
    assert record.checkpoint_ref == "refs/saberops/checkpoints/job-1/1"
    assert record.checkpoint_sha == _sha(7)
    assert record.preemptions == 2
    assert record.reset_deadline == "2026-10-18T12:00:00+00:00"
    assert record.not_before == "2026-10-18T12:00:00+00:00"
    assert harness.executor.lane_preemption("L1") == record
    request = preemption.requests[0]
    assert request.run_id == "run-1"
    assert request.base_sha == _sha(999)
    assert request.worktree == "/worktrees/wave-1/L1"
    payloads = harness.sink.payloads(EVENT_LANE_PREEMPTED)
    assert payloads and payloads[0]["checkpoint_ref"] == record.checkpoint_ref


def test_preempt_refuses_an_admitted_fallback() -> None:
    """Covers: WX-2 (a quota signal with an admitted fallback is not a park)."""
    preemption = FakePreemption()
    harness = _harness(preemption=preemption)
    harness.start_lane()
    with pytest.raises(WaveExecutorError):
        harness.executor.preempt_lane("L1", attempt_terminal=True, fallback_admitted=True)
    assert harness.executor.lane_run("L1").state is LaneState.RUNNING
    assert preemption.requests == []


def test_preempt_refuses_mid_attempt() -> None:
    """Covers: WX-2 (never preempt mid-attempt)."""
    harness = _harness(preemption=FakePreemption())
    harness.start_lane()
    with pytest.raises(WaveExecutorError):
        harness.executor.preempt_lane("L1", attempt_terminal=False, fallback_admitted=False)
    assert harness.executor.lane_run("L1").state is LaneState.RUNNING


def test_preempt_requires_a_configured_port_and_an_executing_lane() -> None:
    """Covers: WX-2 (no preemption without the M2-5 port; only executing lanes)."""
    no_port = _harness()
    no_port.start_lane()
    with pytest.raises(WaveExecutorError):
        no_port.executor.preempt_lane("L1", attempt_terminal=True, fallback_admitted=False)

    idle = _harness(preemption=FakePreemption())
    idle.executor.start()
    with pytest.raises(WaveExecutorError):
        idle.executor.preempt_lane("L1", attempt_terminal=True, fallback_admitted=False)


def test_resume_is_refused_before_the_exclusion_window() -> None:
    """Covers: WX-2 (never dispatch a parked lane before the parsed reset)."""
    harness = _harness(preemption=FakePreemption(not_before="2026-10-18T12:00:00+00:00"))
    harness.start_lane()
    harness.executor.preempt_lane("L1", attempt_terminal=True, fallback_admitted=False)
    with pytest.raises(WaveExecutorError):
        harness.executor.resume_preempted_lane(
            "L1", now=datetime(2026, 10, 18, 11, 59, tzinfo=UTC)
        )
    assert harness.executor.lane_run("L1").state is LaneState.PREEMPTED


def test_resume_returns_the_lane_to_the_main_model_after_reset() -> None:
    """Covers: WX-2 (the next dispatch uses the main route after reset)."""
    harness = _harness(preemption=FakePreemption(not_before="2026-10-18T12:00:00+00:00"))
    harness.start_lane()
    harness.executor.preempt_lane("L1", attempt_terminal=True, fallback_admitted=False)
    resumed = harness.executor.resume_preempted_lane(
        "L1", now=datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
    )
    assert resumed.state is LaneState.RUNNING
    payloads = harness.sink.payloads(EVENT_LANE_RESUMED)
    assert payloads == [
        {
            "wave_id": "wave-1",
            "run_id": "run-1",
            "lane_id": "L1",
            "worker_route": "worker-chain-main",
            "job_id": "job-1",
            "checkpoint_ref": "refs/saberops/checkpoints/job-1/1",
            "checkpoint_sha": _sha(7),
            "not_before": "2026-10-18T12:00:00+00:00",
            "dispatch_model": "main",
        }
    ]


def test_unknown_reset_resumes_without_a_fabricated_delay() -> None:
    """Covers: WX-2 + M2-5 (no evidence-backed ETA -> probe, never a guess)."""
    harness = _harness(
        preemption=FakePreemption(reset_deadline=None, not_before=None)
    )
    harness.start_lane()
    record = harness.executor.preempt_lane(
        "L1", attempt_terminal=True, fallback_admitted=False
    )
    assert record.reset_deadline is None
    assert record.not_before is None
    resumed = harness.executor.resume_preempted_lane(
        "L1", now=datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
    )
    assert resumed.state is LaneState.RUNNING


def test_preempted_lane_consumes_no_child_slot() -> None:
    """Covers: WX-2 + ADR-0004 Amendment 3 (parked work never holds a slot)."""
    harness = _harness(preemption=FakePreemption())
    harness.start_lane()
    assert LANE_STATE_TO_SLOT_STATE[LaneState.PREEMPTED].value == "blocked"
    harness.executor.preempt_lane("L1", attempt_terminal=True, fallback_admitted=False)
    records = harness.executor.slot_records("proj-A")
    lane_records = [record for record in records if record.kind == "child_task"]
    assert lane_records and lane_records[0].state == "blocked"


def test_resume_requires_a_preempted_lane() -> None:
    """Covers: WX-2 (only a parked lane may be resumed)."""
    harness = _harness()
    harness.start_lane()
    with pytest.raises(WaveExecutorError):
        harness.executor.resume_preempted_lane(
            "L1", now=datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
        )


def test_database_quota_preemption_delegates_to_m2_5(monkeypatch: pytest.MonkeyPatch) -> None:
    """Covers: WX-2 (the default port executes the real M2-5 protocol)."""
    from saberops import preemption as preemption_module

    calls: list[dict[str, Any]] = []

    def fake_complete(db: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)
        return preemption_module.PreemptionOutcome(
            owned=True,
            detail="preempted",
            checkpoint_sha=_sha(5),
            checkpoint_ref="refs/saberops/checkpoints/job-1/2",
            preemptions=3,
            not_before="2026-10-18T12:00:00+00:00",
            handoff_id="handoff-1",
        )

    monkeypatch.setattr(preemption_module, "complete_preemption", fake_complete)
    monkeypatch.setattr(
        preemption_module, "quota_reset_eta", lambda db, run_id: "2026-10-18T12:00:00+00:00"
    )
    port = DatabaseQuotaPreemption(db=object())
    receipt = port.preempt(
        QuotaPreemptionRequest(
            run_id="run-1", lane_id="L1", repo="/repo", base_sha=_sha(1), job_id="job-1"
        )
    )
    assert calls and calls[0]["run_id"] == "run-1"
    assert calls[0]["base_sha"] == _sha(1)
    assert receipt.checkpoint_sha == _sha(5)
    assert receipt.checkpoint_ref == "refs/saberops/checkpoints/job-1/2"
    assert receipt.preemptions == 3
    assert receipt.reset_deadline == "2026-10-18T12:00:00+00:00"
    assert receipt.not_before == "2026-10-18T12:00:00+00:00"


def test_database_quota_preemption_fails_closed_when_not_owned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Covers: WX-2 (an ordinary failure keeps its truthful path)."""
    from saberops import preemption as preemption_module

    def fake_complete(db: Any, **kwargs: Any) -> Any:
        return preemption_module.PreemptionOutcome(owned=False, detail="no_quota_signal")

    monkeypatch.setattr(preemption_module, "complete_preemption", fake_complete)
    port = DatabaseQuotaPreemption(db=object())
    with pytest.raises(WaveExecutorError):
        port.preempt(
            QuotaPreemptionRequest(run_id="run-1", lane_id="L1", repo="/repo", base_sha=_sha(1))
        )


# -- model records ----------------------------------------------------------


def test_lane_preemption_record_validates_and_round_trips() -> None:
    """Covers: WX-2 / M2-5 (the park record is validated and serializable)."""
    record = LanePreemption(
        lane_id="L1",
        checkpoint_ref="refs/saberops/checkpoints/job-1/1",
        checkpoint_sha=_sha(7),
        reason="quota_exhausted_no_admitted_fallback",
        preemptions=1,
        job_id="job-1",
        reset_deadline="2026-10-18T12:00:00+00:00",
        not_before="2026-10-18T12:00:00+00:00",
    )
    assert LanePreemption.from_dict(record.as_dict()) == record
    assert record.exclusion_expired(datetime(2026, 10, 18, 11, 0, tzinfo=UTC)) is False
    assert record.exclusion_expired(datetime(2026, 10, 18, 13, 0, tzinfo=UTC)) is True
    with pytest.raises(WaveModelError):
        LanePreemption(
            lane_id="L1",
            checkpoint_ref="ref",
            checkpoint_sha="A" * 40,
            reason="quota",
        )
    with pytest.raises(WaveModelError):
        LanePreemption(
            lane_id="L1",
            checkpoint_ref="ref",
            checkpoint_sha=_sha(1),
            reason="quota",
            not_before="2026-10-18T12:00:00",
        )
    with pytest.raises(WaveModelError):
        record.exclusion_expired(datetime(2026, 10, 18, 12, 0))
    with pytest.raises(WaveModelError):
        LanePreemption.from_dict({"lane_id": "L1"})


def test_quota_request_and_receipt_validate_fail_closed() -> None:
    """Covers: WX-2 (the M2-5 seam validates every field)."""
    with pytest.raises(WaveExecutorError):
        QuotaPreemptionRequest(run_id="", lane_id="L1", repo="/repo", base_sha=_sha(1))
    with pytest.raises(WaveExecutorError):
        QuotaPreemptionRequest(run_id="r", lane_id="L1", repo="/repo", base_sha="nope")
    with pytest.raises(WaveExecutorError):
        QuotaPreemptionReceipt(checkpoint_sha="nope", checkpoint_ref="ref")
    with pytest.raises(WaveExecutorError):
        QuotaPreemptionReceipt(
            checkpoint_sha=_sha(1), checkpoint_ref="ref", reset_deadline="2026-10-18T12:00:00"
        )
    with pytest.raises(WaveExecutorError):
        QuotaPreemptionReceipt(checkpoint_sha=_sha(1), checkpoint_ref="ref", preemptions=0)


# -- orchestrator seam ------------------------------------------------------


def test_orchestrator_seam_wires_recovery_defaults(tmp_path: Path) -> None:
    """Covers: WX-2 (production wiring of the observation and preemption ports)."""
    from saberops.db import Database
    from saberops.orchestrator import Orchestrator

    orchestrator = Orchestrator(db=Database(tmp_path / "orchestrator.db"))
    wave = _wave()
    executor = orchestrator.execute_wave(
        wave,
        repo=tmp_path,
        worktree_base=tmp_path / "wt",
        git=FakeGit(),
        event_sink=RecordingWaveEventSink(),
        project_id="proj-A",
        gate_executor=FakeGate(),
        integration_worktree=tmp_path / "wt" / "wave-1" / "integration",
    )
    executor.start()
    executor.start_lane("L1")
    # The default long-worker observer is the real RunSupervisor transition.
    handoff = executor.observe_long_worker(
        "L1", terminal_event_exists=True, recent_activity=False
    )
    assert handoff.health == "terminal"
    # The default preemption port routes to M2-5; an unknown run is declined.
    with pytest.raises(WaveExecutorError, match="not owned"):
        executor.preempt_lane("L1", attempt_terminal=True, fallback_admitted=False)
