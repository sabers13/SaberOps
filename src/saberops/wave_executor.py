"""Deterministic wave execution: worktrees, merges, gates and closure (WX-1).

Covers: ADR-0004 §3 D2 (wave/lane state machine, worktrees, deterministic
merges), §3 D4 (integration, named glue, one G3 per wave), §4 invariants 3
(pinned SHAs; a moved lane head blocks the merge), 4 (one failed lane never
fails the wave by itself) and 10 (budgets), Amendment 3 (3 projects x 5 child
tasks), Amendment 4 (accepted-wave cleanup eligibility) and Amendment 7
(concurrency limits are maxima, never targets).

Division of labour
------------------
Everything here is deterministic: the engine allocates worktrees, merges pinned
SHAs in ``merge_order`` with ``--no-ff``, runs exactly one G3 on the exact
integrated head, drives the two state machines, and -- after owner acceptance --
emits the structured ``wave_integrated_and_closed`` event. Models only ever
supply a lane's code, a conflict resolution or named glue; the executor never
calls a model, never moves ``main``, never pushes, and never deletes a
worktree, branch or object (Amendment 4: it emits eligibility; Step 008 executes
storage GC).

Ports
-----
Git, the gate executor, the event sink and the optional project registry are
injected, so the whole module is hermetic in tests and free of hidden provider
calls. :class:`SubprocessWaveGit` is the default real Git port.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from saberops.gate_history import GateKind
from saberops.gate_runner import EngineGateRequest, EngineGateResult
from saberops.git import redact_remote_secrets
from saberops.process import run_process
from saberops.project_slot_policy import (
    OCCUPYING_SLOT_STATES,
    ProjectSlotRecord,
    SlotAdmissionDecision,
    SlotAdmissionRequest,
    SlotKind,
    SlotLimits,
    SlotSnapshot,
    SlotState,
    SnapshotSource,
    default_slot_limits,
    evaluate_slot_admission,
)
from saberops.wave_models import (
    LaneState,
    Wave,
    WaveModelError,
    WaveState,
    transition_lane,
    transition_wave,
)

__all__ = [
    "EVENT_GLUE_REQUIRED",
    "EVENT_INTEGRATION_STARTED",
    "EVENT_LANE_ACCEPTED",
    "EVENT_LANE_BLOCKED",
    "EVENT_LANE_REVIEWED_PROVISIONAL",
    "EVENT_LANE_STARTED",
    "EVENT_WAVE_ACCEPTED",
    "EVENT_WAVE_AWAITING_ACCEPT",
    "EVENT_WAVE_BLOCKED",
    "EVENT_WAVE_GATED",
    "EVENT_WAVE_INTEGRATED_AND_CLOSED",
    "EVENT_WAVE_STARTED",
    "INTEGRATOR_ROLE",
    "LANE_STATE_TO_SLOT_STATE",
    "CleanupEligibility",
    "CleanupObject",
    "DatabaseWaveEventSink",
    "EventRecorder",
    "GateExecutorPort",
    "GateReport",
    "IntegrationReport",
    "IntegrationStep",
    "LaneRun",
    "MergeOutcome",
    "ProjectRegistry",
    "RecordingWaveEventSink",
    "SubprocessWaveGit",
    "WaveClosureEvent",
    "WaveExecutor",
    "WaveExecutorError",
    "WaveBudgetExceeded",
    "WaveEventSink",
    "WaveGitPort",
]

#: Engine role for the Integrator dispatch: merge conflicts and named glue only.
INTEGRATOR_ROLE = "INTEGRATE"

EVENT_WAVE_STARTED = "wave_started"
EVENT_INTEGRATION_STARTED = "wave_integration_started"
EVENT_GLUE_REQUIRED = "wave_integrator_required"
EVENT_WAVE_GATED = "wave_gated"
EVENT_WAVE_BLOCKED = "wave_blocked"
EVENT_WAVE_AWAITING_ACCEPT = "wave_awaiting_accept"
EVENT_WAVE_ACCEPTED = "wave_accepted"
EVENT_WAVE_INTEGRATED_AND_CLOSED = "wave_integrated_and_closed"
EVENT_LANE_STARTED = "lane_started"
EVENT_LANE_REVIEWED_PROVISIONAL = "lane_reviewed_provisional"
EVENT_LANE_ACCEPTED = "lane_accepted"
EVENT_LANE_BLOCKED = "lane_blocked"

#: Cleanup-eligibility statuses (Amendment 4).
STATUS_ELIGIBLE = "ELIGIBLE_CLEANUP"
STATUS_BLOCKED = "BLOCKED"
STATUS_RETAINED = "RETAINED"
STATUS_MERGED_AND_CLOSED = "MERGED+CLOSED"

#: Non-occupying states used for records that must never hold a child slot.
_NON_OCCUPYING = SlotState.BLOCKED.value

#: Lane lifecycle -> child-slot occupancy (ADR-0004 Amendment 3).
#:
#: A lane executes only while RUNNING or in a FIX_UP_K turn; REVIEW,
#: REVIEWED_PROVISIONAL, PLANNED and BLOCKED are waiting states that consume no
#: slot, and ACCEPTED is terminal. This is what keeps 3x5 a *ceiling*: review
#: and owner-wait time never occupy execution capacity.
LANE_STATE_TO_SLOT_STATE: Mapping[LaneState, SlotState] = {
    LaneState.PLANNED: SlotState.QUEUED,
    LaneState.RUNNING: SlotState.EXECUTING,
    LaneState.REVIEW: SlotState.BLOCKED,
    LaneState.FIX_UP_K: SlotState.EXECUTING,
    LaneState.REVIEWED_PROVISIONAL: SlotState.BLOCKED,
    LaneState.ACCEPTED: SlotState.TERMINAL,
    LaneState.BLOCKED: SlotState.BLOCKED,
}

_SHA_HEX = set("0123456789abcdef")


class WaveExecutorError(RuntimeError):
    """A wave operation was refused; nothing was mutated by the refused call."""


class WaveBudgetExceeded(WaveExecutorError):
    """A wave budget (G3 runs, fix rounds) was exceeded; the owner must decide."""


def _is_sha(value: object) -> bool:
    if not isinstance(value, str) or len(value) not in (40, 64):
        return False
    return all(ch in _SHA_HEX for ch in value)


def _require_sha(value: object, field_name: str) -> str:
    if not _is_sha(value):
        raise WaveExecutorError(f"{field_name} must be a full lowercase hex commit SHA")
    return str(value)


# ---------------------------------------------------------------------------
# Ports and their default implementations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MergeOutcome:
    """Result of one ``git merge --no-ff`` attempt."""

    ok: bool
    conflict_paths: tuple[str, ...] = ()
    detail: str = ""


class WaveGitPort(Protocol):
    """The deterministic Git surface the executor depends on."""

    def resolve_ref(self, repo: str, ref: str) -> str | None: ...

    def is_ancestor(self, repo: str, ancestor_sha: str, descendant_sha: str) -> bool: ...

    def allocate_worktree(
        self, repo: str, branch: str, path: str, start_point: str
    ) -> str: ...

    def merge_no_ff(self, worktree: str, sha: str) -> MergeOutcome: ...

    def head_sha(self, worktree: str) -> str: ...


class GateExecutorPort(Protocol):
    """One engine-run gate executor (GS-2 ``EngineGateExecutor``)."""

    def execute(self, request: EngineGateRequest) -> EngineGateResult: ...


class WaveEventSink(Protocol):
    """Destination for one structured wave event."""

    def emit(self, event_type: str, payload: dict[str, Any]) -> None: ...


class EventRecorder(Protocol):
    """Minimal slice of :class:`saberops.db.Database` used for event persistence."""

    def record_event(
        self,
        run_id: str,
        event_type: str,
        attempt_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> int: ...


class ProjectRegistry(Protocol):
    """Step 015 project registry seam (optional)."""

    def resolve_project_id(self, repo: str) -> str | None: ...


class RecordingWaveEventSink:
    """In-memory sink: records events in order. Used by tests and dry runs."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        self.events.append((event_type, payload))

    def types(self) -> tuple[str, ...]:
        """Event types in emission order."""
        return tuple(event_type for event_type, _ in self.events)

    def payloads(self, event_type: str) -> list[dict[str, Any]]:
        """Every payload emitted for ``event_type`` in order."""
        return [payload for kind, payload in self.events if kind == event_type]


class DatabaseWaveEventSink:
    """Persist wave events on the wave's own run record.

    A wave is persisted as a job of kind ``wave`` with its own ``runs`` row, so
    its events attach to that row exactly like any other run's events. The
    caller must have created that row (``Database.create_run``) before the first
    emission; the underlying foreign key enforces it, which is deliberate.
    """

    def __init__(self, recorder: EventRecorder, run_id: str) -> None:
        if not isinstance(run_id, str) or not run_id:
            raise WaveExecutorError("run_id must be a non-empty string")
        self._recorder = recorder
        self._run_id = run_id

    def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        self._recorder.record_event(self._run_id, event_type, payload=payload)


class SubprocessWaveGit:
    """Default :class:`WaveGitPort` backed by the real ``git`` binary."""

    def resolve_ref(self, repo: str, ref: str) -> str | None:
        from saberops.git import GitManager

        return GitManager.branch_tip(repo, ref)

    def is_ancestor(self, repo: str, ancestor_sha: str, descendant_sha: str) -> bool:
        from saberops.git import GitManager

        return GitManager.is_ancestor(repo, ancestor_sha, descendant_sha)

    def allocate_worktree(
        self, repo: str, branch: str, path: str, start_point: str
    ) -> str:
        from saberops.git import GitManager

        return str(GitManager.create_worktree(repo, branch, path, start_point))

    def merge_no_ff(self, worktree: str, sha: str) -> MergeOutcome:
        result = run_process(
            ["git", "merge", "--no-ff", "--no-edit", sha], cwd=worktree
        )
        if result.passed:
            return MergeOutcome(ok=True, detail=result.stdout.strip()[:400])
        conflicts = self._conflict_paths(worktree)
        detail = redact_remote_secrets((result.stdout + result.stderr).strip())[:400]
        return MergeOutcome(
            ok=False,
            conflict_paths=conflicts,
            detail=detail or "git merge --no-ff failed",
        )

    def head_sha(self, worktree: str) -> str:
        from saberops.git import GitManager

        return GitManager.get_head_commit(worktree)

    @staticmethod
    def _conflict_paths(worktree: str) -> tuple[str, ...]:
        result = run_process(
            ["git", "diff", "--name-only", "--diff-filter=U"], cwd=worktree
        )
        if not result.passed:
            return ()
        return tuple(
            sorted(line.strip() for line in result.stdout.splitlines() if line.strip())
        )


# ---------------------------------------------------------------------------
# Records and reports
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LaneRun:
    """Observed state of one lane inside a wave execution."""

    lane_id: str
    task_id: str
    state: LaneState
    worktree: str | None = None
    branch: str | None = None
    head_sha: str | None = None
    fix_rounds: int = 0


@dataclass(frozen=True)
class IntegrationStep:
    """One deterministic merge attempt for one lane, in ``merge_order``."""

    lane_id: str
    sha: str
    merged: bool
    conflict: bool = False
    conflict_paths: tuple[str, ...] = ()
    detail: str = ""


@dataclass(frozen=True)
class IntegrationReport:
    """Outcome of one :meth:`WaveExecutor.integrate` call."""

    state: WaveState
    steps: tuple[IntegrationStep, ...]
    requires_integrator: bool
    integrator_role: str | None
    integrator_reason: str | None
    named_glue: tuple[str, ...]
    outstanding_glue: tuple[str, ...]
    integration_head: str | None
    blocked_reason: str | None

    @property
    def conflicts(self) -> tuple[str, ...]:
        """Every conflicted path across the attempted steps, sorted and unique."""
        found: set[str] = set()
        for step in self.steps:
            found.update(step.conflict_paths)
        return tuple(sorted(found))


@dataclass(frozen=True)
class GateReport:
    """Outcome of the single G3 run on the exact integrated head."""

    kind: str
    outcome: str
    accepted: bool
    head_sha: str
    reason: str
    evidence_dir: str
    history_path: str
    wave_state: WaveState
    g3_runs: int


@dataclass(frozen=True)
class CleanupObject:
    """One engine-owned local Git object offered to (or held from) cleanup."""

    kind: str
    ref: str
    status: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "ref": self.ref, "status": self.status, "reason": self.reason}


@dataclass(frozen=True)
class WaveClosureEvent:
    """Payload of ``wave_integrated_and_closed`` (ADR-0004 Amendment 4)."""

    wave_id: str
    accepted_sha: str
    target_branch: str
    child_heads: tuple[tuple[str, str], ...]
    evidence_locations: tuple[str, ...]
    retained_recovery_refs: tuple[str, ...]
    eligible_objects: tuple[CleanupObject, ...]
    held_objects: tuple[CleanupObject, ...]
    status: str
    integration_status: str

    def as_payload(self) -> dict[str, Any]:
        """Return the JSON-serializable event payload."""
        return {
            "wave_id": self.wave_id,
            "accepted_sha": self.accepted_sha,
            "target_branch": self.target_branch,
            "child_heads": [list(item) for item in self.child_heads],
            "evidence_locations": list(self.evidence_locations),
            "retained_recovery_refs": list(self.retained_recovery_refs),
            "eligible_git_objects": [item.as_dict() for item in self.eligible_objects],
            "held_git_objects": [item.as_dict() for item in self.held_objects],
            "status": self.status,
            "integration_status": self.integration_status,
        }


@dataclass(frozen=True)
class CleanupEligibility:
    """Result of :meth:`WaveExecutor.accept` (never performs any deletion)."""

    status: str
    cleanup_eligible: bool
    event: WaveClosureEvent
    blocked_reasons: tuple[str, ...] = ()

    def as_payload(self) -> dict[str, Any]:
        return self.event.as_payload()


# ---------------------------------------------------------------------------
# The executor
# ---------------------------------------------------------------------------


class WaveExecutor:
    """Drive one wave through RUNNING, INTEGRATING, GATED, AWAITING_ACCEPT, ACCEPTED.

    The executor owns exactly one wave. Every mutating method either completes
    deterministically or raises :class:`WaveExecutorError` without advancing the
    state machine, so an illegal transition can never be observed as progress.
    """

    def __init__(
        self,
        wave: Wave,
        *,
        git: WaveGitPort,
        event_sink: WaveEventSink,
        repo: str,
        worktree_base: str,
        gate_executor: GateExecutorPort | None = None,
        evidence_dir: str | None = None,
        project_registry: ProjectRegistry | None = None,
        project_id: str | None = None,
        integration_branch: str | None = None,
        integration_worktree: str | None = None,
    ) -> None:
        if not isinstance(wave, Wave):
            raise WaveExecutorError("wave must be a Wave")
        self._wave = wave
        self._git = git
        self._events = event_sink
        self._repo = repo
        self._worktree_base = worktree_base.rstrip("/")
        self._gate = gate_executor
        self._evidence_dir = evidence_dir
        self._registry = project_registry
        self._project_id = project_id
        self._integration_branch = integration_branch or f"orch/{wave.wave_id}/integration"
        self._integration_worktree = integration_worktree
        self._state = WaveState.PLANNED
        self._lanes: dict[str, LaneRun] = {
            lane.lane_id: LaneRun(
                lane_id=lane.lane_id, task_id=lane.task_id, state=LaneState.PLANNED
            )
            for lane in wave.lanes
        }
        self._completed_merges: dict[str, str] = {}
        self._glue_attested: set[str] = set()
        self._g3_runs = 0
        self._wave_head: str | None = None

    # -- read-only views ---------------------------------------------------

    @property
    def wave(self) -> Wave:
        return self._wave

    @property
    def wave_state(self) -> WaveState:
        return self._state

    @property
    def integration_branch(self) -> str:
        return self._integration_branch

    @property
    def integration_worktree(self) -> str | None:
        return self._integration_worktree

    @property
    def g3_runs(self) -> int:
        return self._g3_runs

    def lane_run(self, lane_id: str) -> LaneRun:
        """Return the run record for one lane, or raise for an unknown lane."""
        try:
            return self._lanes[lane_id]
        except KeyError as exc:
            raise WaveExecutorError(f"unknown lane_id: {lane_id!r}") from exc

    def lane_runs(self) -> tuple[LaneRun, ...]:
        """Lane records in declared lane order (deterministic)."""
        return tuple(self._lanes[lane.lane_id] for lane in self._wave.lanes)

    def lane_branch_name(self, lane_id: str) -> str:
        """Branch name the executor would allocate for one lane."""
        return f"orch/{self._wave.wave_id}/{lane_id}"

    # -- state machine -----------------------------------------------------

    def _set_wave(self, target: WaveState) -> WaveState:
        try:
            self._state = transition_wave(self._state, target)
        except WaveModelError as exc:
            raise WaveExecutorError(str(exc)) from exc
        return self._state

    def _set_lane(self, lane_id: str, target: LaneState) -> LaneRun:
        run = self.lane_run(lane_id)
        try:
            new_state = transition_lane(run.state, target)
        except WaveModelError as exc:
            raise WaveExecutorError(str(exc)) from exc
        updated = replace(run, state=new_state)
        self._lanes[lane_id] = updated
        return updated

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> WaveState:
        """PLANNED -> RUNNING and allocate the isolated integration worktree."""
        if not self._integration_worktree:
            raise WaveExecutorError("integration_worktree is required before start")
        self._set_wave(WaveState.RUNNING)
        allocated = self._git.allocate_worktree(
            self._repo,
            self._integration_branch,
            self._integration_worktree,
            self._wave.base_sha,
        )
        self._integration_worktree = allocated
        self._emit(
            EVENT_WAVE_STARTED,
            {
                "wave_id": self._wave.wave_id,
                "base_sha": self._wave.base_sha,
                "target_branch": self._wave.target_branch,
                "integration_branch": self._integration_branch,
                "integration_worktree": allocated,
                "merge_order": list(self._wave.merge_order),
            },
        )
        return self._state

    def start_lane(self, lane_id: str) -> LaneRun:
        """PLANNED -> RUNNING for one lane and allocate its isolated worktree."""
        if self._state is not WaveState.RUNNING:
            raise WaveExecutorError(
                f"lane {lane_id!r} cannot start in wave state {self._state.value}"
            )
        branch = self.lane_branch_name(lane_id)
        path = f"{self._worktree_base}/{self._wave.wave_id}/{lane_id}"
        allocated = self._git.allocate_worktree(
            self._repo, branch, path, self._wave.base_sha
        )
        self._set_lane(lane_id, LaneState.RUNNING)
        run = replace(self.lane_run(lane_id), worktree=allocated, branch=branch)
        self._lanes[lane_id] = run
        self._emit(
            EVENT_LANE_STARTED,
            {
                "wave_id": self._wave.wave_id,
                "lane_id": lane_id,
                "task_id": run.task_id,
                "branch": branch,
                "worktree": allocated,
                "base_sha": self._wave.base_sha,
            },
        )
        return run

    def lane_review(self, lane_id: str) -> LaneRun:
        """RUNNING -> REVIEW: the committed lane head goes to a cold Reviewer."""
        return self._set_lane(lane_id, LaneState.REVIEW)

    def lane_fix_up(self, lane_id: str) -> LaneRun:
        """REVIEW -> FIX_UP_K, enforcing the fix-round budget fail-closed."""
        run = self.lane_run(lane_id)
        limit = self._wave.budget("fix_rounds", 2)
        if run.fix_rounds + 1 > limit:
            raise WaveBudgetExceeded(
                f"lane {lane_id!r} fix round {run.fix_rounds + 1} exceeds the "
                f"budget of {limit}; the owner must decide"
            )
        updated = self._set_lane(lane_id, LaneState.FIX_UP_K)
        updated = replace(updated, fix_rounds=updated.fix_rounds + 1)
        self._lanes[lane_id] = updated
        return updated

    def lane_fix_up_complete(self, lane_id: str) -> LaneRun:
        """FIX_UP_K -> RUNNING: the same session resumes the lane's own worktree."""
        return self._set_lane(lane_id, LaneState.RUNNING)

    def lane_reviewed_provisional(self, lane_id: str, head_sha: str) -> LaneRun:
        """REVIEW -> REVIEWED_PROVISIONAL and pin the reviewed exact SHA."""
        sha = _require_sha(head_sha, "head_sha")
        self._set_lane(lane_id, LaneState.REVIEWED_PROVISIONAL)
        run = replace(self.lane_run(lane_id), head_sha=sha)
        self._lanes[lane_id] = run
        self._emit(
            EVENT_LANE_REVIEWED_PROVISIONAL,
            {"wave_id": self._wave.wave_id, "lane_id": lane_id, "head_sha": sha},
        )
        return run

    def lane_accept(self, lane_id: str) -> LaneRun:
        """-> ACCEPTED once the lane has a pinned reviewed head."""
        if not self.lane_run(lane_id).head_sha:
            raise WaveExecutorError(f"lane {lane_id!r} has no pinned reviewed head to accept")
        accepted = self._set_lane(lane_id, LaneState.ACCEPTED)
        self._emit(
            EVENT_LANE_ACCEPTED,
            {"wave_id": self._wave.wave_id, "lane_id": lane_id, "head_sha": accepted.head_sha},
        )
        return accepted

    def lane_block(self, lane_id: str, reason: str) -> LaneRun:
        """-> BLOCKED for one lane; the wave continues with its other lanes."""
        blocked = self._set_lane(lane_id, LaneState.BLOCKED)
        self._emit(
            EVENT_LANE_BLOCKED,
            {"wave_id": self._wave.wave_id, "lane_id": lane_id, "reason": reason},
        )
        return blocked

    def lane_resume(self, lane_id: str) -> LaneRun:
        """BLOCKED -> RUNNING in the lane's own worktree."""
        return self._set_lane(lane_id, LaneState.RUNNING)

    # -- integration -------------------------------------------------------

    def integrate(self, lane_heads: Mapping[str, str] | None = None) -> IntegrationReport:
        """Merge the pinned lane heads in ``merge_order`` with ``--no-ff``.

        Deterministic and sequential: exactly one merge per lane, in the pinned
        order, into the isolated integration worktree. A conflicting merge or a
        named glue item does not auto-resolve: the wave stays ``INTEGRATING``
        and the report names the ``INTEGRATE`` candidate. A moved lane head or a
        missing pinned SHA blocks the wave fail-closed.
        """
        if self._state is WaveState.RUNNING:
            self._set_wave(WaveState.INTEGRATING)
            self._emit(
                EVENT_INTEGRATION_STARTED,
                {
                    "wave_id": self._wave.wave_id,
                    "integration_branch": self._integration_branch,
                    "integration_worktree": self._integration_worktree,
                    "merge_order": list(self._wave.merge_order),
                },
            )
        if self._state is not WaveState.INTEGRATING:
            raise WaveExecutorError(f"cannot integrate in wave state {self._state.value}")
        if not self._integration_worktree:
            raise WaveExecutorError("integration worktree is not allocated")

        heads = dict(lane_heads or {})
        steps: list[IntegrationStep] = []
        blocked_reason: str | None = None

        for lane_id in self._wave.merge_order:
            if lane_id in self._completed_merges:
                continue
            pinned = heads.get(lane_id) or self.lane_run(lane_id).head_sha
            if not _is_sha(pinned):
                steps.append(
                    IntegrationStep(
                        lane_id=lane_id, sha="", merged=False, detail="missing_pinned_sha"
                    )
                )
                blocked_reason = f"missing_pinned_sha:{lane_id}"
                break
            assert pinned is not None  # narrowed by _is_sha
            actual = self._git.resolve_ref(self._repo, self.lane_branch_name(lane_id))
            if actual is None:
                steps.append(
                    IntegrationStep(
                        lane_id=lane_id, sha=pinned, merged=False, detail="lane_ref_missing"
                    )
                )
                blocked_reason = f"lane_ref_missing:{lane_id}"
                break
            if actual != pinned:
                steps.append(
                    IntegrationStep(
                        lane_id=lane_id,
                        sha=pinned,
                        merged=False,
                        detail=f"lane_head_moved:{actual}",
                    )
                )
                blocked_reason = f"lane_head_moved:{lane_id}"
                break
            outcome = self._git.merge_no_ff(self._integration_worktree, pinned)
            if not outcome.ok:
                steps.append(
                    IntegrationStep(
                        lane_id=lane_id,
                        sha=pinned,
                        merged=False,
                        conflict=True,
                        conflict_paths=outcome.conflict_paths,
                        detail=outcome.detail or "merge_conflict",
                    )
                )
                break
            self._completed_merges[lane_id] = pinned
            steps.append(IntegrationStep(lane_id=lane_id, sha=pinned, merged=True))

        conflict = any(step.conflict for step in steps)
        outstanding = tuple(
            item for item in self._wave.named_glue if item not in self._glue_attested
        )
        needs_integrator = blocked_reason is None and (conflict or bool(outstanding))

        if blocked_reason is not None:
            self._set_wave(WaveState.BLOCKED)
            self._emit(
                EVENT_WAVE_BLOCKED,
                {"wave_id": self._wave.wave_id, "reason": blocked_reason},
            )
        elif needs_integrator:
            self._emit(
                EVENT_GLUE_REQUIRED,
                {
                    "wave_id": self._wave.wave_id,
                    "role": INTEGRATOR_ROLE,
                    "reason": "merge_conflict" if conflict else "named_glue",
                    "conflicts": [path for step in steps for path in step.conflict_paths],
                    "outstanding_glue": list(outstanding),
                },
            )

        report = IntegrationReport(
            state=self._state,
            steps=tuple(steps),
            requires_integrator=needs_integrator,
            integrator_role=INTEGRATOR_ROLE if needs_integrator else None,
            integrator_reason=(
                None
                if blocked_reason is not None
                else "merge_conflict"
                if conflict
                else "named_glue"
                if outstanding
                else None
            ),
            named_glue=tuple(self._wave.named_glue),
            outstanding_glue=outstanding,
            integration_head=self._integration_head_if_complete(),
            blocked_reason=blocked_reason,
        )
        return report

    def _integration_head_if_complete(self) -> str | None:
        if set(self._completed_merges) != set(self._wave.merge_order):
            return None
        if self._integration_worktree is None:
            return None
        return self._git.head_sha(self._integration_worktree)

    def record_integrator_resolution(
        self, lane_id: str, resolved_sha: str, *, note: str = ""
    ) -> None:
        """Record the Integrator's conflict resolution for one lane.

        The resolved commit is pinned exactly like a reviewed lane head; the
        engine never merges a moving ref.
        """
        if self._state is not WaveState.INTEGRATING:
            raise WaveExecutorError(
                f"integrator resolution requires INTEGRATING, not {self._state.value}"
            )
        sha = _require_sha(resolved_sha, "resolved_sha")
        self._wave.lane_by_id(lane_id)
        if lane_id in self._completed_merges:
            raise WaveExecutorError(f"lane {lane_id!r} is already merged; nothing to resolve")
        self._completed_merges[lane_id] = sha
        self._emit(
            "wave_integrator_resolution",
            {
                "wave_id": self._wave.wave_id,
                "lane_id": lane_id,
                "resolved_sha": sha,
                "role": INTEGRATOR_ROLE,
                "note": note,
            },
        )

    def record_glue_written(self, items: Sequence[str]) -> tuple[str, ...]:
        """Record that the Integrator wrote the named glue items."""
        if self._state is not WaveState.INTEGRATING:
            raise WaveExecutorError(
                f"glue attestation requires INTEGRATING, not {self._state.value}"
            )
        for item in items:
            if item not in self._wave.named_glue:
                raise WaveExecutorError(f"{item!r} is not a named glue item of this wave")
            self._glue_attested.add(item)
        outstanding = tuple(
            item for item in self._wave.named_glue if item not in self._glue_attested
        )
        self._emit(
            "wave_integrator_glue_written",
            {
                "wave_id": self._wave.wave_id,
                "role": INTEGRATOR_ROLE,
                "items": list(items),
                "outstanding": list(outstanding),
            },
        )
        return outstanding

    # -- gate --------------------------------------------------------------

    def run_g3(self, *, slot_wait: float | None = None) -> GateReport:
        """Run exactly one G3 on the exact integrated head (heavy gate slot)."""
        if self._state is not WaveState.INTEGRATING:
            raise WaveExecutorError(f"G3 requires INTEGRATING, not {self._state.value}")
        if self._gate is None:
            raise WaveExecutorError("no gate executor is configured")
        if self._evidence_dir is None:
            raise WaveExecutorError("evidence_dir is required to run G3")
        outstanding = tuple(
            item for item in self._wave.named_glue if item not in self._glue_attested
        )
        if outstanding:
            raise WaveExecutorError(
                f"named glue not attested before G3: {', '.join(outstanding)}"
            )
        head = self._integration_head_if_complete()
        if head is None:
            raise WaveExecutorError("integration is not complete; G3 requires every lane merged")
        limit = self._wave.budget("g3_runs", 4)
        if self._g3_runs + 1 > limit:
            raise WaveBudgetExceeded(
                f"G3 run {self._g3_runs + 1} exceeds the budget of {limit}; "
                "the owner must decide"
            )
        request = EngineGateRequest(
            target_repo=Path(self._integration_worktree or self._repo),
            kind=GateKind.G3,
            evidence_dir=Path(self._evidence_dir),
            base=self._wave.base_sha,
            head_sha=head,
            slot_wait=slot_wait,
        )
        result = self._gate.execute(request)
        self._g3_runs += 1
        accepted = bool(result.accepted)
        self._wave_head = head
        if accepted:
            self._set_wave(WaveState.GATED)
            self._emit(
                EVENT_WAVE_GATED,
                {
                    "wave_id": self._wave.wave_id,
                    "head_sha": head,
                    "outcome": result.outcome,
                    "history_path": result.history_path,
                },
            )
        else:
            self._set_wave(WaveState.BLOCKED)
            self._emit(
                EVENT_WAVE_BLOCKED,
                {
                    "wave_id": self._wave.wave_id,
                    "head_sha": head,
                    "reason": f"g3_{result.outcome.lower()}",
                },
            )
        return GateReport(
            kind=GateKind.G3.value,
            outcome=result.outcome,
            accepted=accepted,
            head_sha=head,
            reason=result.reason,
            evidence_dir=self._evidence_dir,
            history_path=result.history_path,
            wave_state=self._state,
            g3_runs=self._g3_runs,
        )

    def await_acceptance(self) -> WaveState:
        """GATED -> AWAITING_ACCEPT: the wave is reported to the Orchestrator."""
        self._set_wave(WaveState.AWAITING_ACCEPT)
        self._emit(
            EVENT_WAVE_AWAITING_ACCEPT,
            {"wave_id": self._wave.wave_id, "head_sha": self._wave_head},
        )
        return self._state

    def resume(self) -> WaveState:
        """BLOCKED -> RUNNING: a cleared block or a resolved conflict resumes the wave."""
        self._set_wave(WaveState.RUNNING)
        self._emit(EVENT_WAVE_STARTED, {"wave_id": self._wave.wave_id, "resumed": True})
        return self._state

    # -- acceptance and cleanup eligibility --------------------------------

    def accept(
        self,
        accepted_sha: str,
        *,
        evidence_locations: Sequence[str] = (),
        retained_recovery_refs: Sequence[str] = (),
        live_plan_refs: Sequence[str] = (),
    ) -> CleanupEligibility:
        """Record owner acceptance and emit the cleanup-eligibility event.

        ``accepted_sha`` must be the exact integrated wave head (the SHA the
        owner fast-forwarded the target branch to). A mismatch is refused: the
        engine never reports closure for a SHA it did not gate. The method
        checks "child wave or branch still referenced by a live plan" and holds
        those objects back; it performs no Git or filesystem mutation and does
        not change ``ACCEPTED`` while Step 008 tracks cleanup.
        """
        if self._state is not WaveState.AWAITING_ACCEPT:
            raise WaveExecutorError(f"accept requires AWAITING_ACCEPT, not {self._state.value}")
        sha = _require_sha(accepted_sha, "accepted_sha")
        if self._wave_head is not None and sha != self._wave_head:
            raise WaveExecutorError(
                f"accepted_sha {sha} is not the gated wave head {self._wave_head}"
            )
        self._set_wave(WaveState.ACCEPTED)
        self._wave_head = sha
        self._emit(
            EVENT_WAVE_ACCEPTED,
            {
                "wave_id": self._wave.wave_id,
                "accepted_sha": sha,
                "target_branch": self._wave.target_branch,
            },
        )

        live = {ref for ref in live_plan_refs if isinstance(ref, str) and ref}
        eligible: list[CleanupObject] = []
        held: list[CleanupObject] = []
        blocked_reasons: list[str] = []

        for candidate in self._cleanup_candidates(sha, retained_recovery_refs):
            if candidate.ref in live:
                blocked_reasons.append(f"held_by_live_plan:{candidate.kind}:{candidate.ref}")
                held.append(
                    replace(
                        candidate,
                        status=STATUS_BLOCKED,
                        reason="referenced by a live plan",
                    )
                )
            else:
                eligible.append(candidate)

        child_heads = tuple(
            (lane_id, self._completed_merges.get(lane_id, self.lane_run(lane_id).head_sha or ""))
            for lane_id in self._wave.merge_order
        )
        if held:
            status = STATUS_BLOCKED
        elif eligible:
            status = STATUS_ELIGIBLE
        else:
            status = STATUS_RETAINED

        event = WaveClosureEvent(
            wave_id=self._wave.wave_id,
            accepted_sha=sha,
            target_branch=self._wave.target_branch,
            child_heads=child_heads,
            evidence_locations=tuple(
                [self._evidence_dir, *evidence_locations]
                if self._evidence_dir
                else evidence_locations
            ),
            retained_recovery_refs=tuple(retained_recovery_refs),
            eligible_objects=tuple(eligible),
            held_objects=tuple(held),
            status=status,
            integration_status=STATUS_MERGED_AND_CLOSED,
        )
        self._emit(EVENT_WAVE_INTEGRATED_AND_CLOSED, event.as_payload())
        return CleanupEligibility(
            status=status,
            cleanup_eligible=status is STATUS_ELIGIBLE,
            event=event,
            blocked_reasons=tuple(blocked_reasons),
        )

    def _cleanup_candidates(
        self, accepted_sha: str, retained_recovery_refs: Sequence[str]
    ) -> tuple[CleanupObject, ...]:
        """Engine-owned local objects the accepted wave no longer needs pinned.

        Only objects this wave created are ever offered: its isolated lane
        worktrees, its lane branches and its integration branch/worktree.
        Retained recovery refs and objects not owned by this wave are never
        candidates, and no remote ref is ever mentioned (Amendment 4).
        """
        retained = {ref for ref in retained_recovery_refs if isinstance(ref, str) and ref}
        candidates: list[CleanupObject] = []
        for lane in self._wave.lanes:
            run = self.lane_run(lane.lane_id)
            if run.worktree and run.worktree not in retained:
                candidates.append(
                    CleanupObject(
                        kind="worktree",
                        ref=run.worktree,
                        status=STATUS_ELIGIBLE,
                        reason=f"lane {lane.lane_id} accepted at {accepted_sha}",
                    )
                )
            branch = run.branch or self.lane_branch_name(lane.lane_id)
            if branch not in retained:
                candidates.append(
                    CleanupObject(
                        kind="branch",
                        ref=branch,
                        status=STATUS_ELIGIBLE,
                        reason=f"lane {lane.lane_id} merged into the accepted wave",
                    )
                )
        if self._integration_worktree and self._integration_worktree not in retained:
            candidates.append(
                CleanupObject(
                    kind="worktree",
                    ref=self._integration_worktree,
                    status=STATUS_ELIGIBLE,
                    reason="wave integration worktree after acceptance",
                )
            )
        if self._integration_branch not in retained:
            candidates.append(
                CleanupObject(
                    kind="branch",
                    ref=self._integration_branch,
                    status=STATUS_ELIGIBLE,
                    reason="wave integration branch after acceptance",
                )
            )
        return tuple(candidates)

    # -- 3 x 5 project/child admission -------------------------------------

    def slot_records(self, project_id: str | None = None) -> tuple[ProjectSlotRecord, ...]:
        """Derive the wave's own slot records for the 3x5 admission policy.

        The wave itself is a ``WAVE_WRAPPER`` and never occupies a child slot;
        each lane contributes exactly one ``CHILD_TASK`` record whose occupancy
        follows :data:`LANE_STATE_TO_SLOT_STATE`. A wave parent plus five
        children therefore counts as five slots, not six.
        """
        project = self.resolve_project_id(project_id)
        records = [
            ProjectSlotRecord(
                project_id=project,
                task_id=self._wave.wave_id,
                slot_id=f"{self._wave.wave_id}:wave",
                kind=SlotKind.WAVE_WRAPPER.value,
                state=_NON_OCCUPYING,
            )
        ]
        for lane in self._wave.lanes:
            run = self.lane_run(lane.lane_id)
            records.append(
                ProjectSlotRecord(
                    project_id=project,
                    task_id=lane.task_id,
                    slot_id=self._slot_id(lane.lane_id),
                    kind=SlotKind.CHILD_TASK.value,
                    state=LANE_STATE_TO_SLOT_STATE[run.state].value,
                )
            )
        return tuple(records)

    def _slot_id(self, lane_id: str) -> str:
        return f"{lane_id}:slot"

    def resolve_project_id(self, explicit: str | None = None) -> str:
        """Resolve the project identity: registry, then explicit, then derived.

        The Step 015 registry is optional; when it is absent (or returns
        nothing) the engine falls back to the derived project identity of the
        repository, and fails closed when none can be computed.
        """
        if self._registry is not None:
            resolved = self._registry.resolve_project_id(self._repo)
            if isinstance(resolved, str) and resolved.strip():
                return resolved.strip()
        if isinstance(explicit, str) and explicit.strip():
            return explicit.strip()
        if isinstance(self._project_id, str) and self._project_id.strip():
            return self._project_id.strip()
        from saberops.project import ProjectIdentityError, compute_project_identity

        try:
            return compute_project_identity(self._repo).project_id
        except ProjectIdentityError as exc:
            raise WaveExecutorError(
                f"no project identity is available for {self._repo!r}: {exc}"
            ) from exc

    def admit_child_lane(
        self,
        lane_id: str,
        *,
        owner_id: str,
        limits: SlotLimits | None = None,
        snapshot: SlotSnapshot | None = None,
        project_id: str | None = None,
    ) -> SlotAdmissionDecision:
        """Admit one lane as a child task under the 3x5 policy.

        The candidate is presented as the ``RESERVED`` slot the lane *would*
        occupy, and (by default) the snapshot is this wave's own records with
        the candidate lane removed, so the wrapper/lane accounting is exactly
        the engine's: the wave wrapper never counts, REVIEW and
        REVIEWED_PROVISIONAL lanes never count, and a wave parent plus five
        running children leaves a sixth child over the limit. A lane that
        already occupies a slot or is accepted is refused outright, so a
        duplicate start is never authorized.

        The decision is a pure projection: it is *not* launch authority by
        itself -- the transactional caller described by
        :mod:`saberops.project_slot_policy` is still required.
        """
        project = self.resolve_project_id(project_id)
        self._wave.lane_by_id(lane_id)
        run = self.lane_run(lane_id)
        if LANE_STATE_TO_SLOT_STATE[run.state] in OCCUPYING_SLOT_STATES:
            raise WaveExecutorError(f"lane {lane_id!r} already occupies an execution slot")
        if run.state is LaneState.ACCEPTED:
            raise WaveExecutorError(f"lane {lane_id!r} is already accepted")
        candidate = ProjectSlotRecord(
            project_id=project,
            task_id=run.task_id,
            slot_id=self._slot_id(lane_id),
            kind=SlotKind.CHILD_TASK.value,
            state=SlotState.RESERVED.value,
        )
        snap = snapshot or SlotSnapshot(
            owner_id=owner_id,
            source=SnapshotSource.OWNER_DB.value,
            complete=True,
            slots=tuple(
                record
                for record in self.slot_records(project)
                if record.slot_id != self._slot_id(lane_id)
            ),
        )
        return evaluate_slot_admission(
            SlotAdmissionRequest(
                owner_id=owner_id,
                candidate=candidate,
                limits=limits or default_slot_limits(),
            ),
            snap,
        )

    # -- helpers -----------------------------------------------------------

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        self._events.emit(event_type, payload)
