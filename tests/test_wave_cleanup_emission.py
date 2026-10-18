"""Accepted-wave cleanup eligibility emission (ADR-0004 Amendment 4).

Hermetic: the Git surface, gate executor and event sink are fakes, so the tests
assert the emitted event, the exact-SHA matching and the held-branch protection
without any real Git mutation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from saberops.gate_runner import EngineGateRequest, EngineGateResult
from saberops.wave_executor import (
    EVENT_INTEGRATION_STARTED,
    EVENT_WAVE_GATED,
    EVENT_WAVE_INTEGRATED_AND_CLOSED,
    CleanupEligibility,
    DatabaseWaveEventSink,
    MergeOutcome,
    RecordingWaveEventSink,
    WaveEventSink,
    WaveExecutor,
    WaveExecutorError,
)
from saberops.wave_models import Lane, Wave, WaveState

STATUS_ELIGIBLE = "ELIGIBLE_CLEANUP"
STATUS_BLOCKED = "BLOCKED"
STATUS_RETAINED = "RETAINED"
MERGED_AND_CLOSED = "MERGED+CLOSED"


def _sha(index: int) -> str:
    return f"{index:040x}"


def _lane(lane_id: str) -> Lane:
    return Lane(
        lane_id=lane_id,
        task_id=f"task-{lane_id}",
        prompt="work",
        touches=[f"src/{lane_id}.py"],
        must_not_touch=[],
        contracts=[],
        retirement_table={},
        worker_route="worker-chain",
        reviewer_route="reviewer-chain",
        effort="medium",
    )


def _wave(lane_count: int = 2) -> Wave:
    lanes = [_lane(f"L{i}") for i in range(1, lane_count + 1)]
    return Wave(
        wave_id="wave-1",
        base_sha=_sha(999),
        lanes=lanes,
        merge_order=[lane.lane_id for lane in lanes],
        named_glue=[],
        budgets={},
        target_branch="main",
    )


class FakeGit:
    """Read/merge/allocate-only WaveGitPort: no deletion surface exists."""

    def __init__(self, heads: dict[str, str]) -> None:
        self.heads = heads
        self.merges: list[str] = []

    def resolve_ref(self, repo: str, ref: str) -> str | None:
        return self.heads.get(ref)

    def is_ancestor(self, repo: str, ancestor_sha: str, descendant_sha: str) -> bool:
        return True

    def allocate_worktree(self, repo: str, branch: str, path: str, start_point: str) -> str:
        return path

    def merge_no_ff(self, worktree: str, sha: str) -> MergeOutcome:
        self.merges.append(sha)
        return MergeOutcome(ok=True, detail="merged")

    def head_sha(self, worktree: str) -> str:
        return _sha(9000)


class FakeGate:
    def __init__(self) -> None:
        self.requests: list[EngineGateRequest] = []

    def execute(self, request: EngineGateRequest) -> EngineGateResult:
        self.requests.append(request)
        return EngineGateResult(
            kind=str(request.kind),
            producer="engine",
            profile="full",
            outcome="PASS",
            accepted=True,
            reason="projected",
            head_sha=request.head_sha,
            tests=12,
            failed=(),
            flaky=(),
            wall_time=1.0,
            history_path="/evidence/result/full.jsonl",
            outcome_path="/evidence/result/outcome.json",
        )


class RecordingRecorder:
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


class _Harness:
    def __init__(self, lane_count: int = 2, sink: WaveEventSink | None = None) -> None:
        self.wave = _wave(lane_count)
        self.heads = {
            f"orch/wave-1/{lane_id}": _sha(index)
            for index, lane_id in enumerate(self.wave.merge_order, start=1)
        }
        self.git = FakeGit(self.heads)
        self.gate = FakeGate()
        self.sink: WaveEventSink = sink or RecordingWaveEventSink()
        self.executor = WaveExecutor(
            self.wave,
            git=self.git,
            event_sink=self.sink,
            repo="/repo",
            worktree_base="/worktrees",
            gate_executor=self.gate,
            evidence_dir="/evidence",
            project_id="proj-A",
            integration_worktree="/worktrees/wave-1/integration",
        )

    def run_to_awaiting_acceptance(self) -> None:
        self.executor.start()
        for lane_id in self.wave.merge_order:
            self.executor.start_lane(lane_id)
            self.executor.lane_review(lane_id)
            self.executor.lane_reviewed_provisional(lane_id, self.heads[f"orch/wave-1/{lane_id}"])
            self.executor.lane_accept(lane_id)
        self.executor.integrate()
        self.executor.run_g3()
        self.executor.await_acceptance()

    def accept(self, **kwargs: object) -> CleanupEligibility:
        return self.executor.accept(_sha(9000), **kwargs)  # type: ignore[arg-type]


def _event_types(sink: WaveEventSink) -> tuple[str, ...]:
    assert isinstance(sink, RecordingWaveEventSink)
    return sink.types()


def _closure_events(sink: WaveEventSink) -> list[dict[str, object]]:
    assert isinstance(sink, RecordingWaveEventSink)
    return sink.payloads(EVENT_WAVE_INTEGRATED_AND_CLOSED)


# -- emission timing --------------------------------------------------------


def test_closure_event_is_emitted_only_after_owner_acceptance() -> None:
    """Covers: ADR-0004 Amendment 4 (no cleanup on an unaccepted integration)."""
    harness = _Harness()
    harness.executor.start()
    assert _closure_events(harness.sink) == []
    for lane_id in harness.wave.merge_order:
        harness.executor.start_lane(lane_id)
        harness.executor.lane_review(lane_id)
        harness.executor.lane_reviewed_provisional(
            lane_id, harness.heads[f"orch/wave-1/{lane_id}"]
        )
        harness.executor.lane_accept(lane_id)
    harness.executor.integrate()
    assert EVENT_INTEGRATION_STARTED in _event_types(harness.sink)
    assert _closure_events(harness.sink) == []
    harness.executor.run_g3()
    assert EVENT_WAVE_GATED in _event_types(harness.sink)
    assert _closure_events(harness.sink) == []
    harness.executor.await_acceptance()
    assert _closure_events(harness.sink) == []
    harness.accept()
    events = _closure_events(harness.sink)
    assert len(events) == 1


def test_closure_payload_carries_the_required_evidence() -> None:
    """Covers: ADR-0004 Amendment 4 (accepted SHA, child heads, evidence, objects)."""
    harness = _Harness(lane_count=2)
    harness.run_to_awaiting_acceptance()
    eligibility = harness.accept(
        evidence_locations=["$ORCH/wave-1/lane-check.txt"],
        retained_recovery_refs=["refs/saberops/checkpoints/wave-1/0"],
    )
    payload = eligibility.as_payload()
    assert payload["accepted_sha"] == _sha(9000)
    assert payload["wave_id"] == "wave-1"
    assert payload["target_branch"] == "main"
    assert payload["integration_status"] == MERGED_AND_CLOSED
    assert payload["status"] == STATUS_ELIGIBLE
    assert payload["child_heads"] == [
        ["L1", harness.heads["orch/wave-1/L1"]],
        ["L2", harness.heads["orch/wave-1/L2"]],
    ]
    assert "/evidence" in payload["evidence_locations"]
    assert "$ORCH/wave-1/lane-check.txt" in payload["evidence_locations"]
    assert payload["retained_recovery_refs"] == ["refs/saberops/checkpoints/wave-1/0"]
    objects = payload["eligible_git_objects"]
    assert isinstance(objects, list)
    kinds = {item["kind"] for item in objects}
    assert kinds == {"worktree", "branch"}
    refs = {item["ref"] for item in objects}
    assert "orch/wave-1/L1" in refs
    assert "/worktrees/wave-1/integration" in refs
    assert all(item["status"] == STATUS_ELIGIBLE for item in objects)
    assert all(item["reason"] for item in objects)
    # The payload is a plain JSON document for Step 008.
    assert json.loads(json.dumps(payload)) == payload


def test_eligible_objects_cover_lane_and_integration_worktrees_and_branches() -> None:
    """Covers: ADR-0004 Amendment 4 (engine-owned worktrees and branches only)."""
    harness = _Harness(lane_count=2)
    harness.run_to_awaiting_acceptance()
    eligibility = harness.accept()
    refs = {item.ref for item in eligibility.event.eligible_objects}
    assert refs == {
        "/worktrees/wave-1/L1",
        "/worktrees/wave-1/L2",
        "orch/wave-1/L1",
        "orch/wave-1/L2",
        "/worktrees/wave-1/integration",
        "orch/wave-1/integration",
    }
    assert eligibility.cleanup_eligible is True


def test_retained_recovery_refs_are_never_offered_for_cleanup() -> None:
    """Covers: ADR-0004 Amendment 4 (retained refs stay)."""
    harness = _Harness(lane_count=1)
    harness.run_to_awaiting_acceptance()
    eligibility = harness.accept(
        retained_recovery_refs=["orch/wave-1/L1", "/worktrees/wave-1/integration"]
    )
    refs = {item.ref for item in eligibility.event.eligible_objects}
    assert "orch/wave-1/L1" not in refs
    assert "/worktrees/wave-1/integration" not in refs
    assert "/worktrees/wave-1/L1" in refs
    assert "orch/wave-1/integration" in refs


# -- exact-SHA matching -----------------------------------------------------


def test_accept_refuses_a_sha_that_is_not_the_gated_head() -> None:
    """Covers: ADR-0004 Amendment 4 (exact accepted SHA)."""
    harness = _Harness()
    harness.run_to_awaiting_acceptance()
    with pytest.raises(WaveExecutorError):
        harness.executor.accept(_sha(123))
    assert harness.executor.wave_state is WaveState.AWAITING_ACCEPT
    assert _closure_events(harness.sink) == []


def test_accept_refuses_a_malformed_sha() -> None:
    """Covers: ADR-0004 Amendment 4 (accepted SHA shape)."""
    harness = _Harness()
    harness.run_to_awaiting_acceptance()
    with pytest.raises(WaveExecutorError):
        harness.executor.accept("NOT-A-SHA")
    assert harness.executor.wave_state is WaveState.AWAITING_ACCEPT


def test_accept_requires_awaiting_acceptance() -> None:
    """Covers: ADR-0004 D2 (accept is only reachable from AWAITING_ACCEPT)."""
    harness = _Harness()
    harness.executor.start()
    for lane_id in harness.wave.merge_order:
        harness.executor.start_lane(lane_id)
        harness.executor.lane_review(lane_id)
        harness.executor.lane_reviewed_provisional(
            lane_id, harness.heads[f"orch/wave-1/{lane_id}"]
        )
        harness.executor.lane_accept(lane_id)
    harness.executor.integrate()
    harness.executor.run_g3()
    assert harness.executor.wave_state is WaveState.GATED
    with pytest.raises(WaveExecutorError):
        harness.accept()
    assert _closure_events(harness.sink) == []


# -- live-plan protection ---------------------------------------------------


def test_a_branch_referenced_by_a_live_plan_is_held_back() -> None:
    """Covers: ADR-0004 Amendment 4 (blocks cleanup while a live plan holds it)."""
    harness = _Harness(lane_count=2)
    harness.run_to_awaiting_acceptance()
    eligibility = harness.accept(live_plan_refs=["orch/wave-1/L2"])
    assert eligibility.status == STATUS_BLOCKED
    assert eligibility.cleanup_eligible is False
    assert eligibility.blocked_reasons == ("held_by_live_plan:branch:orch/wave-1/L2",)
    held = {item.ref for item in eligibility.event.held_objects}
    offered = {item.ref for item in eligibility.event.eligible_objects}
    assert held == {"orch/wave-1/L2"}
    assert "orch/wave-1/L2" not in offered
    assert "orch/wave-1/L1" in offered
    # The payload still describes the whole closure, and ACCEPTED is unchanged.
    payload = eligibility.as_payload()
    assert payload["status"] == STATUS_BLOCKED
    assert payload["held_git_objects"][0]["status"] == STATUS_BLOCKED
    assert harness.executor.wave_state is WaveState.ACCEPTED


def test_a_child_wave_integration_branch_can_be_held_for_the_next_wave() -> None:
    """Covers: ADR-0004 Amendment 4 (child wave branch held while the plan continues)."""
    harness = _Harness(lane_count=1)
    harness.run_to_awaiting_acceptance()
    eligibility = harness.accept(live_plan_refs=["orch/wave-1/integration"])
    held = {item.ref for item in eligibility.event.held_objects}
    assert held == {"orch/wave-1/integration"}
    assert eligibility.status == STATUS_BLOCKED


def test_all_objects_retained_reports_retained() -> None:
    """Covers: ADR-0004 Amendment 4 (RETAINED is distinct from ELIGIBLE)."""
    harness = _Harness(lane_count=1)
    harness.run_to_awaiting_acceptance()
    eligibility = harness.accept(
        retained_recovery_refs=[
            "orch/wave-1/L1",
            "/worktrees/wave-1/L1",
            "orch/wave-1/integration",
            "/worktrees/wave-1/integration",
        ]
    )
    assert eligibility.status == STATUS_RETAINED
    assert eligibility.cleanup_eligible is False
    assert eligibility.event.eligible_objects == ()
    assert eligibility.event.held_objects == ()


def test_acceptance_never_deletes_and_never_mutates_git() -> None:
    """Covers: ADR-0004 Amendment 4 (emission only; Step 008 executes cleanup)."""
    harness = _Harness(lane_count=2)
    harness.run_to_awaiting_acceptance()
    merges_before = list(harness.git.merges)
    harness.accept(live_plan_refs=["orch/wave-1/L1"])
    assert harness.git.merges == merges_before
    public = {name for name in dir(harness.git) if not name.startswith("_")}
    assert not {name for name in public if "delete" in name or "remove" in name}


# -- determinism and persistence --------------------------------------------


def test_closure_payload_is_deterministic() -> None:
    """Covers: ADR-0004 Amendment 4 (deterministic eligibility)."""
    first = _Harness(lane_count=2)
    first.run_to_awaiting_acceptance()
    first_payload = first.accept().as_payload()
    second = _Harness(lane_count=2)
    second.run_to_awaiting_acceptance()
    second_payload = second.accept().as_payload()
    assert first_payload == second_payload


def test_database_sink_persists_the_closure_event() -> None:
    """Covers: ADR-0004 Amendment 4 (the closure event is durably recorded)."""
    recorder = RecordingRecorder()
    harness = _Harness(lane_count=1, sink=DatabaseWaveEventSink(recorder, "wave-1"))
    harness.run_to_awaiting_acceptance()
    harness.accept()
    closure = [row for row in recorder.rows if row[1] == EVENT_WAVE_INTEGRATED_AND_CLOSED]
    assert len(closure) == 1
    assert closure[0][0] == "wave-1"
    payload = closure[0][2]
    assert payload is not None
    assert payload["accepted_sha"] == _sha(9000)
    assert all(row[0] == "wave-1" for row in recorder.rows)


def test_evidence_locations_default_to_the_gate_evidence_dir() -> None:
    """Covers: ADR-0004 Amendment 4 (evidence locations are reported)."""
    harness = _Harness(lane_count=1)
    harness.run_to_awaiting_acceptance()
    eligibility = harness.accept()
    assert eligibility.event.evidence_locations == ("/evidence",)
    assert Path("/evidence").is_absolute()
