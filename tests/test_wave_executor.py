"""Wave executor: state machines, deterministic merges, conflicts and 3x5 limits.

Hermetic: the Git surface, the gate executor and the event sink are all fakes,
so no provider, no network and no real repository are needed except in the two
explicit real-``git`` tests, which build a throwaway repository under
``tmp_path``.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from saberops.gate_runner import EngineGateRequest, EngineGateResult
from saberops.orchestrator import Orchestrator
from saberops.project_slot_policy import (
    ProjectSlotRecord,
    SlotKind,
    SlotLimits,
    SlotSnapshot,
    SlotState,
    SnapshotSource,
)
from saberops.wave_executor import (
    EVENT_WAVE_INTEGRATED_AND_CLOSED,
    INTEGRATOR_ROLE,
    LANE_STATE_TO_SLOT_STATE,
    DatabaseWaveEventSink,
    MergeOutcome,
    RecordingWaveEventSink,
    SubprocessWaveGit,
    WaveBudgetExceeded,
    WaveExecutor,
    WaveExecutorError,
)
from saberops.wave_models import Lane, LaneState, Wave, WaveState


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
        "worker_route": "worker-chain",
        "reviewer_route": "reviewer-chain",
        "effort": "medium",
    }
    base.update(overrides)
    return Lane(**base)  # type: ignore[arg-type]


def _wave(
    lane_count: int = 2, *, merge_order: list[str] | None = None, **overrides: object
) -> Wave:
    lanes = [_lane(f"L{i}", [f"src/{i}.py"]) for i in range(1, lane_count + 1)]
    base: dict[str, object] = {
        "wave_id": "wave-1",
        "base_sha": _sha(999),
        "lanes": lanes,
        "merge_order": merge_order or [lane.lane_id for lane in lanes],
        "named_glue": [],
        "budgets": {},
        "target_branch": "main",
    }
    base.update(overrides)
    return Wave(**base)  # type: ignore[arg-type]


class FakeGit:
    """Deterministic in-memory WaveGitPort with a recorded merge order."""

    def __init__(
        self,
        *,
        tips: dict[str, str] | None = None,
        conflicts: tuple[str, ...] = (),
        head: str = _sha(9000),
        missing_refs: tuple[str, ...] = (),
    ) -> None:
        self.tips = dict(tips or {})
        self.conflicts = set(conflicts)
        self.head = head
        self.missing_refs = set(missing_refs)
        self.merges: list[str] = []
        self.worktrees: list[tuple[str, str, str]] = []

    def resolve_ref(self, repo: str, ref: str) -> str | None:
        if ref in self.missing_refs:
            return None
        return self.tips.get(ref)

    def is_ancestor(self, repo: str, ancestor_sha: str, descendant_sha: str) -> bool:
        return True

    def allocate_worktree(self, repo: str, branch: str, path: str, start_point: str) -> str:
        self.worktrees.append((branch, path, start_point))
        self.tips.setdefault(branch, start_point)
        return path

    def merge_no_ff(self, worktree: str, sha: str) -> MergeOutcome:
        self.merges.append(sha)
        if sha in self.conflicts:
            return MergeOutcome(
                ok=False, conflict_paths=("src/conflict.py",), detail="CONFLICT (content)"
            )
        return MergeOutcome(ok=True, detail="Merge made by the 'ort' strategy.")

    def head_sha(self, worktree: str) -> str:
        return self.head


class FakeGate:
    """Deterministic in-memory GateExecutorPort."""

    def __init__(self, *, accepted: bool = True, outcome: str = "PASS") -> None:
        self.accepted = accepted
        self.outcome = outcome
        self.requests: list[EngineGateRequest] = []

    def execute(self, request: EngineGateRequest) -> EngineGateResult:
        self.requests.append(request)
        return EngineGateResult(
            kind=str(request.kind),
            producer="engine",
            profile="full",
            outcome=self.outcome,
            accepted=self.accepted,
            reason="projected",
            head_sha=request.head_sha,
            tests=12,
            failed=(),
            flaky=(),
            wall_time=1.5,
            history_path=str(Path(request.evidence_dir) / "result" / "full.jsonl"),
            outcome_path=str(Path(request.evidence_dir) / "result" / "outcome.json"),
        )


class FakeRegistry:
    """Optional Step 015 project-registry seam."""

    def __init__(self, project_id: str | None) -> None:
        self.project_id = project_id
        self.calls: list[str] = []

    def resolve_project_id(self, repo: str) -> str | None:
        self.calls.append(repo)
        return self.project_id


class RecordingRecorder:
    """Minimal EventRecorder backing DatabaseWaveEventSink."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str, dict[str, object] | None]] = []

    def record_event(
        self,
        run_id: str,
        event_type: str,
        attempt_id: str | None = None,
        payload: dict[str, object] | None = None,
    ) -> int:
        self.rows.append((run_id, event_type, payload))
        return len(self.rows)


@dataclass
class Harness:
    """One executor plus the fakes it was built from and its lane head SHAs."""

    executor: WaveExecutor
    git: FakeGit
    gate: FakeGate
    sink: RecordingWaveEventSink
    heads: dict[str, str] = field(default_factory=dict)

    def run_lane(self, lane_id: str) -> None:
        """PLANNED -> ... -> REVIEWED_PROVISIONAL for one lane at its pinned head."""
        self.executor.start_lane(lane_id)
        self.executor.lane_review(lane_id)
        self.executor.lane_reviewed_provisional(lane_id, self.heads[lane_id])
        self.executor.lane_accept(lane_id)


def _harness(
    wave: Wave,
    *,
    conflicts: tuple[str, ...] = (),
    gate_accepted: bool = True,
    gate_outcome: str = "PASS",
    registry: FakeRegistry | None = None,
    project_id: str | None = "proj-A",
    missing_refs: tuple[str, ...] = (),
) -> Harness:
    heads = {lane.lane_id: _sha(index) for index, lane in enumerate(wave.lanes, start=1)}
    tips = {f"orch/{wave.wave_id}/{lane_id}": sha for lane_id, sha in heads.items()}
    git = FakeGit(tips=tips, conflicts=conflicts, missing_refs=missing_refs)
    gate = FakeGate(accepted=gate_accepted, outcome=gate_outcome)
    sink = RecordingWaveEventSink()
    executor = WaveExecutor(
        wave,
        git=git,
        event_sink=sink,
        repo="/repo",
        worktree_base="/worktrees",
        gate_executor=gate,
        evidence_dir="/evidence",
        project_registry=registry,
        project_id=project_id,
        integration_worktree=f"/worktrees/{wave.wave_id}/integration",
    )
    return Harness(executor=executor, git=git, gate=gate, sink=sink, heads=heads)


def _integrated_harness(wave: Wave | None = None, **kwargs: object) -> Harness:
    """Start the wave, run every lane to a pinned head, and integrate."""
    harness = _harness(wave or _wave(), **kwargs)  # type: ignore[arg-type]
    harness.executor.start()
    for lane_id in harness.executor.wave.merge_order:
        harness.run_lane(lane_id)
    harness.executor.integrate()
    return harness


# -- wave state machine -----------------------------------------------------


def test_wave_state_machine_happy_path() -> None:
    """Covers: ADR-0004 D2 (wave state machine) + prompt contract 3."""
    harness = _harness(_wave())
    executor = harness.executor
    initial = executor.wave_state
    assert initial is WaveState.PLANNED
    assert executor.start() is WaveState.RUNNING
    for lane_id in executor.wave.merge_order:
        harness.run_lane(lane_id)
    assert executor.integrate().state is WaveState.INTEGRATING
    assert executor.run_g3().wave_state is WaveState.GATED
    assert executor.await_acceptance() is WaveState.AWAITING_ACCEPT
    eligibility = executor.accept(harness.gate.requests[0].head_sha)
    final = executor.wave_state
    assert final is WaveState.ACCEPTED
    assert eligibility.status == "ELIGIBLE_CLEANUP"


def test_wave_refuses_illegal_transitions() -> None:
    """Covers: ADR-0004 D2 (fail-closed wave state machine)."""
    harness = _harness(_wave())
    executor = harness.executor
    with pytest.raises(WaveExecutorError):
        executor.run_g3()  # PLANNED -> G3 is not a legal step
    with pytest.raises(WaveExecutorError):
        executor.await_acceptance()
    with pytest.raises(WaveExecutorError):
        executor.accept(_sha(1))
    executor.start()
    with pytest.raises(WaveExecutorError):
        executor.start()  # RUNNING -> RUNNING is not a legal transition
    with pytest.raises(WaveExecutorError):
        executor.await_acceptance()


def test_a_blocked_wave_resumes() -> None:
    """Covers: ADR-0004 D2 (BLOCKED resumes into RUNNING)."""
    harness = _harness(_wave(), missing_refs=("orch/wave-1/L1",))
    executor = harness.executor
    executor.start()
    harness.run_lane("L1")
    report = executor.integrate()
    assert report.blocked_reason == "lane_ref_missing:L1"
    assert executor.wave_state is WaveState.BLOCKED
    assert executor.resume() is WaveState.RUNNING


def test_lane_state_machine_and_illegal_lane_transitions() -> None:
    """Covers: ADR-0004 D2 (lane states mirror the BOARD)."""
    harness = _harness(_wave())
    executor = harness.executor
    executor.start()
    executor.start_lane("L1")
    assert executor.lane_run("L1").state is LaneState.RUNNING
    executor.lane_review("L1")
    assert executor.lane_run("L1").state is LaneState.REVIEW
    with pytest.raises(WaveExecutorError):
        executor.lane_accept("L1")  # no pinned reviewed head yet
    executor.lane_reviewed_provisional("L1", _sha(1))
    executor.lane_accept("L1")
    assert executor.lane_run("L1").state is LaneState.ACCEPTED
    assert LANE_STATE_TO_SLOT_STATE[LaneState.ACCEPTED] is SlotState.TERMINAL
    with pytest.raises(WaveExecutorError):
        executor.lane_review("L1")  # ACCEPTED is terminal


def test_lane_blocks_and_resumes_without_failing_the_wave() -> None:
    """Covers: ADR-0004 §4 invariant 4 (one failed lane never fails the wave)."""
    harness = _harness(_wave())
    executor = harness.executor
    executor.start()
    executor.start_lane("L1")
    executor.start_lane("L2")
    executor.lane_block("L1", "missing prerequisite")
    assert executor.lane_run("L1").state is LaneState.BLOCKED
    assert executor.lane_run("L2").state is LaneState.RUNNING
    assert executor.wave_state is WaveState.RUNNING
    assert executor.lane_resume("L1").state is LaneState.RUNNING


def test_unknown_lane_is_refused() -> None:
    """Covers: ADR-0004 D2 (lane identity is validated)."""
    harness = _harness(_wave())
    with pytest.raises(WaveExecutorError):
        harness.executor.lane_run("missing")
    harness.executor.start()
    with pytest.raises(WaveExecutorError):
        harness.executor.start_lane("missing")


def test_fix_round_budget_is_enforced() -> None:
    """Covers: ADR-0004 D4 + §4 invariant 10 (budgets become owner questions)."""
    harness = _harness(_wave(budgets={"fix_rounds": 2}))
    executor = harness.executor
    executor.start()
    executor.start_lane("L1")
    for _ in range(2):
        executor.lane_review("L1")
        executor.lane_fix_up("L1")
        assert executor.lane_fix_up_complete("L1").state is LaneState.RUNNING
    executor.lane_review("L1")
    with pytest.raises(WaveBudgetExceeded):
        executor.lane_fix_up("L1")


def test_g3_budget_is_enforced() -> None:
    """Covers: ADR-0004 D4 + §4 invariant 10 (a 2nd G3 over a 1-G3 budget is refused)."""
    harness = _integrated_harness(
        _wave(budgets={"g3_runs": 1}), gate_accepted=False, gate_outcome="FAIL"
    )
    executor = harness.executor
    first = executor.run_g3()
    assert first.accepted is False
    blocked = executor.wave_state
    assert blocked is WaveState.BLOCKED
    executor.resume()
    resumed = executor.wave_state
    assert resumed is WaveState.RUNNING
    assert executor.integrate().state is WaveState.INTEGRATING
    with pytest.raises(WaveBudgetExceeded):
        executor.run_g3()


# -- worktrees --------------------------------------------------------------


def test_each_lane_and_the_integration_get_an_isolated_worktree() -> None:
    """Covers: ADR-0004 D2 + prompt contract 3 (isolated worktree per lane)."""
    harness = _harness(_wave(lane_count=3))
    executor = harness.executor
    executor.start()
    for lane_id in ("L1", "L2", "L3"):
        executor.start_lane(lane_id)
        run = executor.lane_run(lane_id)
        assert run.worktree == f"/worktrees/wave-1/{lane_id}"
        assert run.branch == f"orch/wave-1/{lane_id}"
    branches = [branch for branch, _, _ in harness.git.worktrees]
    assert branches == [
        "orch/wave-1/integration",
        "orch/wave-1/L1",
        "orch/wave-1/L2",
        "orch/wave-1/L3",
    ]
    assert all(base == _sha(999) for _, _, base in harness.git.worktrees)


# -- integration ------------------------------------------------------------


def test_merges_run_sequentially_in_the_pinned_merge_order() -> None:
    """Covers: ADR-0004 D4 + prompt contract 4 (deterministic --no-ff order)."""
    wave = _wave(lane_count=3, merge_order=["L3", "L1", "L2"])
    harness = _harness(wave)
    executor = harness.executor
    executor.start()
    for lane_id in wave.merge_order:
        harness.run_lane(lane_id)
    report = executor.integrate()
    assert [step.lane_id for step in report.steps] == ["L3", "L1", "L2"]
    assert harness.git.merges == [harness.heads["L3"], harness.heads["L1"], harness.heads["L2"]]
    assert report.requires_integrator is False
    assert report.integration_head == _sha(9000)


def test_a_moved_lane_head_blocks_the_merge() -> None:
    """Covers: ADR-0004 §4 invariant 3 (merges use pinned SHAs)."""
    wave = _wave(lane_count=2)
    harness = _harness(wave)
    executor = harness.executor
    executor.start()
    harness.run_lane("L1")
    harness.run_lane("L2")
    harness.git.tips["orch/wave-1/L2"] = _sha(777)  # the lane branch moved after review
    report = executor.integrate()
    assert report.blocked_reason == "lane_head_moved:L2"
    assert executor.wave_state is WaveState.BLOCKED
    assert harness.git.merges == [harness.heads["L1"]]
    assert report.steps[-1].detail.startswith("lane_head_moved")


def test_a_conflict_requires_the_integrator_and_stops_the_sequence() -> None:
    """Covers: ADR-0004 D4 (INTEGRATE only on conflict; no auto-resolution)."""
    wave = _wave(lane_count=3)
    harness = _harness(wave)
    executor = harness.executor
    executor.start()
    for lane_id in wave.merge_order:
        harness.run_lane(lane_id)
    harness.git.conflicts = {harness.heads["L2"]}
    report = executor.integrate()
    assert report.state is WaveState.INTEGRATING
    assert report.requires_integrator is True
    assert report.integrator_role == INTEGRATOR_ROLE
    assert report.integrator_reason == "merge_conflict"
    assert report.conflicts == ("src/conflict.py",)
    assert harness.git.merges == [harness.heads["L1"], harness.heads["L2"]]  # L3 not attempted
    assert executor.wave_state is WaveState.INTEGRATING
    with pytest.raises(WaveExecutorError):
        executor.run_g3()  # an unresolved conflict can never be gated


def test_integrator_resolution_lets_the_sequence_finish() -> None:
    """Covers: ADR-0004 D4 (conflict resolution is pinned, then integration resumes)."""
    wave = _wave(lane_count=3)
    harness = _harness(wave, conflicts=(_sha(2),))
    executor = harness.executor
    executor.start()
    for lane_id in wave.merge_order:
        harness.run_lane(lane_id)
    report = executor.integrate()
    assert report.requires_integrator is True
    executor.record_integrator_resolution("L2", _sha(4242))
    executor.record_glue_written([])
    resumed = executor.integrate()
    assert [step.lane_id for step in resumed.steps] == ["L3"]
    assert resumed.integration_head is not None
    assert resumed.requires_integrator is False


def test_named_glue_requires_the_integrator_before_g3() -> None:
    """Covers: ADR-0004 D4 (named glue is a model's work, never the engine's)."""
    wave = _wave(lane_count=2, named_glue=["src/glue.py"])
    harness = _harness(wave)
    executor = harness.executor
    executor.start()
    for lane_id in wave.merge_order:
        harness.run_lane(lane_id)
    report = executor.integrate()
    assert report.requires_integrator is True
    assert report.integrator_reason == "named_glue"
    assert report.outstanding_glue == ("src/glue.py",)
    with pytest.raises(WaveExecutorError):
        executor.run_g3()
    assert executor.record_glue_written(["src/glue.py"]) == ()
    assert executor.run_g3().accepted is True
    with pytest.raises(WaveExecutorError):
        executor.record_glue_written(["src/unknown.py"])


def test_g3_runs_once_on_the_exact_integrated_head() -> None:
    """Covers: ADR-0004 D3/D4 (single G3 at the exact merged SHA)."""
    harness = _integrated_harness(_wave(lane_count=2))
    executor = harness.executor
    report = executor.run_g3()
    assert report.g3_runs == 1
    assert len(harness.gate.requests) == 1
    request = harness.gate.requests[0]
    assert str(request.kind) == "G3"
    assert request.head_sha == harness.git.head
    assert request.base == _sha(999)
    assert str(request.target_repo) == "/worktrees/wave-1/integration"


def test_g3_requires_a_gate_executor_and_evidence_dir() -> None:
    """Covers: ADR-0004 D3 (engine-owned gate execution is mandatory)."""
    wave = _wave(lane_count=1)
    executor = WaveExecutor(
        wave,
        git=FakeGit(tips={"orch/wave-1/L1": _sha(1)}),
        event_sink=RecordingWaveEventSink(),
        repo="/repo",
        worktree_base="/worktrees",
        integration_worktree="/worktrees/wave-1/integration",
    )
    executor.start()
    executor.start_lane("L1")
    executor.lane_review("L1")
    executor.lane_reviewed_provisional("L1", _sha(1))
    executor.integrate()
    assert executor.wave_state is WaveState.INTEGRATING
    with pytest.raises(WaveExecutorError):
        executor.run_g3()


# -- 3 x 5 concurrency ------------------------------------------------------


def test_wrapper_records_never_occupy_and_review_lanes_do_not_either() -> None:
    """Covers: ADR-0004 Amendment 3 (wrappers and waiting states take no slot)."""
    harness = _harness(_wave(lane_count=2))
    executor = harness.executor
    executor.start()
    executor.start_lane("L1")
    executor.lane_review("L1")
    executor.start_lane("L2")
    records = executor.slot_records()
    assert [record.kind for record in records] == [
        SlotKind.WAVE_WRAPPER.value,
        SlotKind.CHILD_TASK.value,
        SlotKind.CHILD_TASK.value,
    ]
    assert records[0].task_id == "wave-1"
    assert records[0].state == SlotState.BLOCKED.value
    assert records[1].state == SlotState.BLOCKED.value  # L1 is in REVIEW
    assert records[2].state == SlotState.EXECUTING.value  # L2 is RUNNING


def test_one_project_by_six_children_rejects_the_sixth() -> None:
    """Covers: ADR-0004 Amendment 3 (1x6 boundary)."""
    harness = _harness(_wave(lane_count=6))
    executor = harness.executor
    executor.start()
    for lane_id in ("L1", "L2", "L3", "L4", "L5"):
        executor.start_lane(lane_id)
    decision = executor.admit_child_lane("L6", owner_id="owner-1")
    assert decision.status.value == "REJECTED"
    assert decision.reason_codes() == ("project_slot_limit_reached",)
    assert decision.counts.occupied_child_slots == 5
    assert decision.counts.active_projects == 1


def test_wave_parent_plus_five_children_is_five_slots_not_six() -> None:
    """Covers: ADR-0004 Amendment 3 (wrapper and children never double count)."""
    harness = _harness(_wave(lane_count=5))
    executor = harness.executor
    executor.start()
    for lane_id in ("L1", "L2", "L3", "L4"):
        executor.start_lane(lane_id)
    decision = executor.admit_child_lane("L5", owner_id="owner-1")
    assert decision.admitted is True
    assert decision.reason_codes() == ("admitted",)
    assert decision.counts.occupied_child_slots == 4


class _Occupy:
    """Builds occupying child-task records for a synthetic snapshot."""

    @staticmethod
    def records(project_id: str, count: int) -> list[ProjectSlotRecord]:
        return [
            ProjectSlotRecord(
                project_id=project_id,
                task_id=f"{project_id}-task-{index}",
                slot_id=f"{project_id}-slot-{index}",
                kind=SlotKind.CHILD_TASK.value,
                state=SlotState.EXECUTING.value,
            )
            for index in range(count)
        ]


def _snapshot(records: list[ProjectSlotRecord]) -> SlotSnapshot:
    return SlotSnapshot(
        owner_id="owner-1",
        source=SnapshotSource.OWNER_DB.value,
        complete=True,
        slots=tuple(records),
    )


def test_fourth_active_project_is_rejected() -> None:
    """Covers: ADR-0004 Amendment 3 (4x1 boundary)."""
    harness = _harness(_wave(lane_count=1))
    executor = harness.executor
    executor.start()
    snapshot = _snapshot(
        _Occupy.records("p1", 1) + _Occupy.records("p2", 1) + _Occupy.records("p3", 1)
    )
    decision = executor.admit_child_lane("L1", owner_id="owner-1", snapshot=snapshot)
    assert decision.status.value == "REJECTED"
    assert decision.reason_codes() == ("project_limit_reached",)
    assert decision.counts.active_projects == 3


def test_three_by_five_boundary_rejects_one_more_child() -> None:
    """Covers: ADR-0004 Amendment 3 (3x5 boundary, both dimensions)."""
    harness = _harness(_wave(lane_count=1))
    executor = harness.executor
    executor.start()
    full = _Occupy.records("p1", 5) + _Occupy.records("p2", 5) + _Occupy.records("p3", 5)
    decision = executor.admit_child_lane("L1", owner_id="owner-1", snapshot=_snapshot(full))
    assert decision.reason_codes() == ("project_limit_reached",)

    same_project = (
        _Occupy.records("p1", 5) + _Occupy.records("p2", 5) + _Occupy.records("proj-A", 5)
    )
    decision = executor.admit_child_lane(
        "L1", owner_id="owner-1", snapshot=_snapshot(same_project)
    )
    assert decision.reason_codes() == ("project_slot_limit_reached",)


def test_limits_are_configurable_ceilings() -> None:
    """Covers: ADR-0004 Amendment 7 (limits are ceilings, owner-adjustable)."""
    harness = _harness(_wave(lane_count=3))
    executor = harness.executor
    executor.start()
    executor.start_lane("L1")
    executor.start_lane("L2")
    snapshot = _snapshot(_Occupy.records("proj-A", 2))
    decision = executor.admit_child_lane(
        "L3",
        owner_id="owner-1",
        snapshot=snapshot,
        limits=SlotLimits(active_projects=1, child_slots_per_project=2),
    )
    assert decision.reason_codes() == ("project_slot_limit_reached",)


def test_already_executing_and_accepted_lanes_cannot_be_admitted_again() -> None:
    """Covers: ADR-0004 Amendment 3 (no duplicate child slot)."""
    harness = _harness(_wave(lane_count=1))
    executor = harness.executor
    executor.start()
    executor.start_lane("L1")
    with pytest.raises(WaveExecutorError):
        executor.admit_child_lane("L1", owner_id="owner-1")


# -- project registry seam --------------------------------------------------


def test_registry_prefers_a_registered_project_identity() -> None:
    """Covers: ADR-0004 Amendment 3 (clean Step 015 registry seam)."""
    registry = FakeRegistry("registered-project")
    harness = _harness(_wave(lane_count=1), registry=registry, project_id="fallback")
    assert harness.executor.resolve_project_id() == "registered-project"
    assert registry.calls == ["/repo"]


def test_registry_falls_back_to_the_explicit_or_derived_identity() -> None:
    """Covers: ADR-0004 Amendment 3 (graceful fallback without a registry)."""
    harness = _harness(_wave(lane_count=1), registry=FakeRegistry(None), project_id="fallback")
    assert harness.executor.resolve_project_id() == "fallback"
    assert harness.executor.resolve_project_id("explicit") == "explicit"


def test_missing_project_identity_fails_closed() -> None:
    """Covers: ADR-0004 Amendment 3 (no unsafe default on config error)."""
    harness = _harness(_wave(lane_count=1), registry=FakeRegistry(None), project_id=None)
    with pytest.raises(WaveExecutorError):
        harness.executor.resolve_project_id()


# -- event persistence ------------------------------------------------------


def test_database_sink_records_the_wave_event_on_its_run_row() -> None:
    """Covers: ADR-0004 Amendment 4 (the closure event is persisted)."""
    recorder = RecordingRecorder()
    sink = DatabaseWaveEventSink(recorder, "wave-1")
    sink.emit(EVENT_WAVE_INTEGRATED_AND_CLOSED, {"accepted_sha": _sha(1)})
    assert recorder.rows == [
        ("wave-1", EVENT_WAVE_INTEGRATED_AND_CLOSED, {"accepted_sha": _sha(1)})
    ]
    with pytest.raises(WaveExecutorError):
        DatabaseWaveEventSink(recorder, "")


# -- orchestrator seam ------------------------------------------------------


def test_orchestrator_wave_seam_admits_and_builds_the_executor(tmp_path: Path) -> None:
    """Covers: prompt contract 6 (orchestrator wave execution seam)."""
    from saberops.db import Database

    orchestrator = Orchestrator(db=Database(tmp_path / "orchestrator.db"))
    wave = _wave(lane_count=2)
    decision = orchestrator.admit_wave(
        wave, _sha(2), lambda role, route: True, ancestry_checker=lambda a, b: True
    )
    assert decision.admitted is True
    executor = orchestrator.execute_wave(
        wave, repo=tmp_path, project_id="proj-A", gate_executor=FakeGate()
    )
    assert executor.wave_state is WaveState.PLANNED
    assert executor.wave.wave_id == "wave-1"
    assert executor.integration_branch == "orch/wave-1/integration"
    assert str(executor.integration_worktree).endswith("/wave-1/integration")


# -- real git ---------------------------------------------------------------


def _real_repo(path: Path) -> str:
    """Covers: helper (temporary git repository only)."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.check_output(["git", "init", "-q", "-b", "main"], cwd=path)
    subprocess.check_output(["git", "config", "user.email", "t@example.invalid"], cwd=path)
    subprocess.check_output(["git", "config", "user.name", "T"], cwd=path)
    (path / "a.txt").write_text("base\n")
    (path / "b.txt").write_text("base\n")
    subprocess.check_output(["git", "add", "-A"], cwd=path)
    subprocess.check_output(["git", "commit", "-qm", "init"], cwd=path)
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=path, text=True).strip()


def _commit_in(path: Path, name: str, text: str) -> str:
    (path / name).write_text(text)
    subprocess.check_output(["git", "add", "-A"], cwd=path)
    subprocess.check_output(["git", "commit", "-qm", f"change {name}"], cwd=path)
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=path, text=True).strip()


def test_subprocess_git_merges_with_no_ff_and_detects_a_real_conflict(tmp_path: Path) -> None:
    """Covers: ADR-0004 D4 (real `git merge --no-ff` and conflict paths)."""
    repo = tmp_path / "repo"
    base = _real_repo(repo)
    # Two lane branches with overlapping edits to the same file -> a real conflict.
    subprocess.check_output(["git", "checkout", "-q", "-b", "orch/wave-1/L1", base], cwd=repo)
    _commit_in(repo, "a.txt", "lane one\n")
    subprocess.check_output(["git", "checkout", "-q", "-b", "orch/wave-1/L2", base], cwd=repo)
    _commit_in(repo, "a.txt", "lane two\n")
    subprocess.check_output(["git", "checkout", "-q", "main"], cwd=repo)

    port = SubprocessWaveGit()
    integration = tmp_path / "integration"
    port.allocate_worktree(str(repo), "orch/wave-1/integration", str(integration), base)

    # A clean merge of L1 is a real merge commit because --no-ff is used.
    lane_one = port.resolve_ref(str(repo), "orch/wave-1/L1")
    assert lane_one is not None
    outcome = port.merge_no_ff(str(integration), lane_one)
    assert outcome.ok is True
    merge_count = subprocess.check_output(
        ["git", "rev-list", "--merges", "--count", "HEAD"], cwd=integration, text=True
    ).strip()
    assert merge_count == "1"

    # The second lane conflicts on a.txt and the conflict is reported by path.
    lane_two = port.resolve_ref(str(repo), "orch/wave-1/L2")
    assert lane_two is not None
    outcome = port.merge_no_ff(str(integration), lane_two)
    assert outcome.ok is False
    assert outcome.conflict_paths == ("a.txt",)


def test_subprocess_git_drives_a_real_full_wave_integration(tmp_path: Path) -> None:
    """Covers: ADR-0004 D2/D4 (worktrees, merges, one G3, closure end to end)."""
    repo = tmp_path / "repo"
    base = _real_repo(repo)
    wave = _wave(lane_count=2, base_sha=base, merge_order=["L2", "L1"])
    worktrees = tmp_path / "wt"
    executor = WaveExecutor(
        wave,
        git=SubprocessWaveGit(),
        event_sink=RecordingWaveEventSink(),
        repo=str(repo),
        worktree_base=str(worktrees),
        gate_executor=FakeGate(),
        evidence_dir=str(tmp_path / "evidence"),
        integration_worktree=str(worktrees / "wave-1" / "integration"),
    )
    executor.start()
    for lane_id, filename in (("L1", "a.txt"), ("L2", "b.txt")):
        run = executor.start_lane(lane_id)
        assert run.worktree is not None
        head = _commit_in(Path(run.worktree), filename, f"{lane_id} change\n")
        executor.lane_review(lane_id)
        executor.lane_reviewed_provisional(lane_id, head)
        executor.lane_accept(lane_id)
    report = executor.integrate()
    assert report.blocked_reason is None
    assert report.requires_integrator is False
    assert [step.lane_id for step in report.steps] == ["L2", "L1"]
    assert all(step.merged for step in report.steps)
    gated = executor.run_g3()
    assert gated.accepted is True
    assert gated.head_sha == report.integration_head
    executor.await_acceptance()
    eligibility = executor.accept(
        str(report.integration_head), live_plan_refs=["orch/wave-1/L1"]
    )
    assert executor.wave_state is WaveState.ACCEPTED
    assert eligibility.status == "BLOCKED"
    assert any(held.ref == "orch/wave-1/L1" for held in eligibility.event.held_objects)
    # The held branch still exists: the executor only emitted eligibility.
    assert (
        subprocess.check_output(
            ["git", "rev-parse", "--verify", "--quiet", "refs/heads/orch/wave-1/L1"],
            cwd=repo,
            text=True,
        ).strip()
        != ""
    )
