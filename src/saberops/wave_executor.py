"""Deterministic wave execution: worktrees, merges, gates and closure (WX-1).

Covers: ADR-0004 §3 D2 (wave/lane state machine, worktrees, deterministic
merges), §3 D4 (integration, named glue, one G3 per wave), §4 invariants 3
(pinned SHAs; a moved lane head blocks the merge), 4 (one failed lane never
fails the wave by itself) and 10 (budgets), Amendment 3 (3 projects x 5 child
tasks), Amendment 4 (accepted-wave cleanup eligibility) and Amendment 7
(concurrency limits are maxima, never targets).

Also covers WX-2 recovery (this module is the deterministic seam):
run-level handoffs on attempt completion, attempt failure and the long-worker
observation; and quota preemption/auto-resume for a lane, bridging the existing
M2-5 durable preemption protocol (``saberops.preemption``) without duplicating
its decisions. Handoffs and park records are built only from durable state and
events passed in; nothing here polls, sleeps, calls a model or guesses a reset.

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

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
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
    LanePreemption,
    LaneState,
    Wave,
    WaveModelError,
    WaveState,
    transition_lane,
    transition_wave,
)

__all__ = [
    "EVENT_ATTEMPT_HANDOFF",
    "EVENT_GLUE_REQUIRED",
    "EVENT_INTEGRATION_STARTED",
    "EVENT_LANE_ACCEPTED",
    "EVENT_LANE_BLOCKED",
    "EVENT_LANE_PREEMPTED",
    "EVENT_LANE_RESUMED",
    "EVENT_LANE_REVIEWED_PROVISIONAL",
    "EVENT_LANE_STARTED",
    "EVENT_LONG_WORKER_HANDOFF",
    "EVENT_WAVE_ACCEPTED",
    "EVENT_WAVE_AWAITING_ACCEPT",
    "EVENT_WAVE_BLOCKED",
    "EVENT_WAVE_GATED",
    "EVENT_WAVE_INTEGRATED_AND_CLOSED",
    "EVENT_WAVE_STARTED",
    "HANDOFF_ATTEMPT_COMPLETED",
    "HANDOFF_ATTEMPT_FAILED",
    "HANDOFF_LONG_WORKER",
    "INTEGRATOR_ROLE",
    "LANE_STATE_TO_SLOT_STATE",
    "REPAIR_ESCALATE",
    "CleanupEligibility",
    "CleanupObject",
    "DatabaseQuotaPreemption",
    "DatabaseWaveEventSink",
    "EventRecorder",
    "GateExecutorPort",
    "GateReport",
    "IntegrationReport",
    "IntegrationStep",
    "LaneRun",
    "LongWorkerObserver",
    "MergeOutcome",
    "ProjectRegistry",
    "QuotaPreemptionPort",
    "QuotaPreemptionReceipt",
    "QuotaPreemptionRequest",
    "RecordingWaveEventSink",
    "SubprocessWaveGit",
    "SupervisorLongWorkerObserver",
    "WaveClosureEvent",
    "WaveExecutor",
    "WaveExecutorError",
    "WaveBudgetExceeded",
    "WaveEventSink",
    "WaveGitPort",
    "WaveHandoff",
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
#: WX-2 recovery events.
EVENT_LANE_PREEMPTED = "lane_preempted"
EVENT_LANE_RESUMED = "lane_resumed"
EVENT_ATTEMPT_HANDOFF = "wave_attempt_handoff"
EVENT_LONG_WORKER_HANDOFF = "wave_long_worker_handoff"

#: Handoff kinds (WX-2): one per attempt completion/failure and the t=300
#: long-worker observation.
HANDOFF_ATTEMPT_COMPLETED = "attempt_completed"
HANDOFF_ATTEMPT_FAILED = "attempt_failed"
HANDOFF_LONG_WORKER = "long_worker"
_HANDOFF_KINDS = frozenset(
    {HANDOFF_ATTEMPT_COMPLETED, HANDOFF_ATTEMPT_FAILED, HANDOFF_LONG_WORKER}
)

#: Repair route when the fix-round budget is spent: the owner must decide.
REPAIR_ESCALATE = "ESCALATE"

#: Default preemption reason: quota exhausted with no admitted fallback model.
PREEMPTION_REASON = "quota_exhausted_no_admitted_fallback"

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
#: REVIEWED_PROVISIONAL, PLANNED, BLOCKED and PREEMPTED are waiting states that
#: consume no slot, and ACCEPTED is terminal. This is what keeps 3x5 a *ceiling*:
#: review, preemption and owner-wait time never occupy execution capacity.
LANE_STATE_TO_SLOT_STATE: Mapping[LaneState, SlotState] = {
    LaneState.PLANNED: SlotState.QUEUED,
    LaneState.RUNNING: SlotState.EXECUTING,
    LaneState.REVIEW: SlotState.BLOCKED,
    LaneState.FIX_UP_K: SlotState.EXECUTING,
    LaneState.REVIEWED_PROVISIONAL: SlotState.BLOCKED,
    LaneState.ACCEPTED: SlotState.TERMINAL,
    LaneState.BLOCKED: SlotState.BLOCKED,
    LaneState.PREEMPTED: SlotState.BLOCKED,
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


def _require_nonempty_str(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WaveExecutorError(f"{field_name} must be a non-empty string")
    return value


def _require_aware_iso(value: object, field_name: str) -> str | None:
    """Validate a timezone-aware ISO-8601 instant, allowing explicit ``None``."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise WaveExecutorError(f"{field_name} must be a timezone-aware ISO-8601 instant")
    try:
        instant = datetime.fromisoformat(value)
    except ValueError as exc:
        raise WaveExecutorError(
            f"{field_name} must be a timezone-aware ISO-8601 instant"
        ) from exc
    if instant.tzinfo is None:
        raise WaveExecutorError(f"{field_name} must be timezone-aware")
    return value


def _require_count(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise WaveExecutorError(f"{field_name} must be a non-negative integer")
    return value


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


class LongWorkerObserver(Protocol):
    """The t=300 long-worker observation transition (WX-2 wiring seam).

    Matches :meth:`saberops.supervisor.RunSupervisor.observe_long_worker`
    exactly: it persists the deterministic observation and returns a health
    verdict. It polls nothing and sleeps nowhere.
    """

    def observe_long_worker(
        self,
        run_id: str,
        *,
        terminal_event_exists: bool,
        recent_activity: bool,
        timed_out: bool = False,
        stalled: bool = False,
    ) -> object: ...


class QuotaPreemptionPort(Protocol):
    """Durable quota-preemption seam for one lane (M2-5 bridge)."""

    def preempt(self, request: QuotaPreemptionRequest) -> QuotaPreemptionReceipt: ...


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


class SupervisorLongWorkerObserver:
    """Adapt :class:`RunSupervisor` to the executor's long-worker port.

    The engine never imports the process-launching supervisor at module load;
    it is handed an already-constructed supervisor and calls only its existing
    persisting observation transition, so the durable supervision row, the
    ``manager_suspended``/``manager_attention_required`` events and the
    ``create_handoffs`` checkpoint all stay the single source of truth.
    """

    def __init__(self, supervisor: object) -> None:
        observe = getattr(supervisor, "observe_long_worker", None)
        if not callable(observe):
            raise WaveExecutorError("supervisor must expose observe_long_worker(...)")
        self._observe: Callable[..., object] = observe

    def observe_long_worker(
        self,
        run_id: str,
        *,
        terminal_event_exists: bool,
        recent_activity: bool,
        timed_out: bool = False,
        stalled: bool = False,
    ) -> object:
        return self._observe(
            run_id,
            terminal_event_exists=terminal_event_exists,
            recent_activity=recent_activity,
            timed_out=timed_out,
            stalled=stalled,
        )


class DatabaseQuotaPreemption:
    """Default :class:`QuotaPreemptionPort` over the M2-5 preemption protocol.

    Delegates every decision to :mod:`saberops.preemption`: it parks the lane's
    durable run (capturing the WIP checkpoint, anchoring the engine-owned
    checkpoint ref and persisting the factual resume capsule) and then reads
    back the reset instant M2-5 parsed. It never guesses a deadline, never
    fabricates a checkpoint and never resumes anything by itself.
    """

    def __init__(self, db: object) -> None:
        self._db = db

    def preempt(self, request: QuotaPreemptionRequest) -> QuotaPreemptionReceipt:
        from saberops.preemption import complete_preemption, quota_reset_eta

        outcome = complete_preemption(
            self._db,
            run_id=request.run_id,
            repo_path=request.repo,
            job_id=request.job_id,
            worktree_path=request.worktree,
            base_sha=request.base_sha,
        )
        if not outcome.owned:
            raise WaveExecutorError(f"quota preemption was not owned: {outcome.detail}")
        if not outcome.checkpoint_sha or not outcome.checkpoint_ref:
            raise WaveExecutorError("preemption produced no anchored checkpoint")
        return QuotaPreemptionReceipt(
            checkpoint_sha=outcome.checkpoint_sha,
            checkpoint_ref=outcome.checkpoint_ref,
            preemptions=outcome.preemptions or 1,
            job_id=outcome.job.job_id if outcome.job is not None else request.job_id,
            reset_deadline=quota_reset_eta(self._db, request.run_id),
            not_before=outcome.not_before,
            handoff_id=outcome.handoff_id,
        )


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
class WaveHandoff:
    """One factual run-level handoff (WX-2).

    Every field is an observation the caller already holds durably: identities,
    SHAs, recorded event names and test counts. The record is deliberately
    *not* free-form model prose and carries no inferred outcome; a consumer
    resumes from the durable run/checkpoint, not from this text.
    """

    kind: str
    run_id: str
    wave_id: str
    lane_id: str
    summary: str
    created_at: str
    attempt_id: str | None = None
    candidate_sha: str | None = None
    artifacts: tuple[str, ...] = ()
    tests_passed: int | None = None
    tests_failed: int | None = None
    failed_tests: tuple[str, ...] = ()
    repair_suggestions: tuple[str, ...] = ()
    repair_route: str | None = None
    escalate: bool = False
    checkpoint_ref: str | None = None
    health: str | None = None
    health_reason: str | None = None
    source_events: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        problems = self.problems()
        if problems:
            raise WaveExecutorError("; ".join(problems))

    def problems(self) -> list[str]:
        """Return deterministic structural problems; empty means valid."""
        problems: list[str] = []
        if self.kind not in _HANDOFF_KINDS:
            problems.append(f"kind must be one of {sorted(_HANDOFF_KINDS)}")
        for name, text in (
            ("run_id", self.run_id),
            ("wave_id", self.wave_id),
            ("lane_id", self.lane_id),
            ("summary", self.summary),
            ("created_at", self.created_at),
        ):
            if not isinstance(text, str) or not text.strip():
                problems.append(f"{name} must be a non-empty string")
        for name, count in (
            ("tests_passed", self.tests_passed),
            ("tests_failed", self.tests_failed),
        ):
            if count is not None and (
                isinstance(count, bool) or not isinstance(count, int) or count < 0
            ):
                problems.append(f"{name} must be a non-negative integer when set")
        for name, optional in (
            ("attempt_id", self.attempt_id),
            ("candidate_sha", self.candidate_sha),
            ("repair_route", self.repair_route),
            ("checkpoint_ref", self.checkpoint_ref),
            ("health", self.health),
            ("health_reason", self.health_reason),
        ):
            if optional is not None and (
                not isinstance(optional, str) or not optional.strip()
            ):
                problems.append(f"{name} must be a non-empty string when set")
        if self.candidate_sha is not None and not _is_sha(self.candidate_sha):
            problems.append("candidate_sha must be a full lowercase hex commit SHA")
        if self.kind != HANDOFF_LONG_WORKER and not (
            self.attempt_id or self.candidate_sha or self.source_events
        ):
            problems.append(
                "an attempt handoff needs a durable anchor "
                "(attempt_id, candidate_sha or source_events)"
            )
        return problems

    def as_payload(self) -> dict[str, Any]:
        """Return the JSON-serializable event payload (deterministic key order)."""
        return {
            "kind": self.kind,
            "run_id": self.run_id,
            "wave_id": self.wave_id,
            "lane_id": self.lane_id,
            "summary": self.summary,
            "created_at": self.created_at,
            "attempt_id": self.attempt_id,
            "candidate_sha": self.candidate_sha,
            "artifacts": list(self.artifacts),
            "tests_passed": self.tests_passed,
            "tests_failed": self.tests_failed,
            "failed_tests": list(self.failed_tests),
            "repair_suggestions": list(self.repair_suggestions),
            "repair_route": self.repair_route,
            "escalate": self.escalate,
            "checkpoint_ref": self.checkpoint_ref,
            "health": self.health,
            "health_reason": self.health_reason,
            "source_events": list(self.source_events),
        }


@dataclass(frozen=True)
class QuotaPreemptionRequest:
    """Durable facts handed to the M2-5 preemption protocol for one lane."""

    run_id: str
    lane_id: str
    repo: str
    base_sha: str
    job_id: str | None = None
    worktree: str | None = None
    reason: str = PREEMPTION_REASON

    def __post_init__(self) -> None:
        _require_nonempty_str(self.run_id, "run_id")
        _require_nonempty_str(self.lane_id, "lane_id")
        _require_nonempty_str(self.repo, "repo")
        _require_sha(self.base_sha, "base_sha")
        _require_nonempty_str(self.reason, "reason")
        if self.job_id is not None:
            _require_nonempty_str(self.job_id, "job_id")
        if self.worktree is not None:
            _require_nonempty_str(self.worktree, "worktree")


@dataclass(frozen=True)
class QuotaPreemptionReceipt:
    """Verified artifacts returned by :class:`QuotaPreemptionPort`."""

    checkpoint_sha: str
    checkpoint_ref: str
    preemptions: int = 1
    job_id: str | None = None
    reset_deadline: str | None = None
    not_before: str | None = None
    handoff_id: str | None = None

    def __post_init__(self) -> None:
        _require_sha(self.checkpoint_sha, "checkpoint_sha")
        _require_nonempty_str(self.checkpoint_ref, "checkpoint_ref")
        if (
            isinstance(self.preemptions, bool)
            or not isinstance(self.preemptions, int)
            or self.preemptions < 1
        ):
            raise WaveExecutorError("preemptions must be a positive integer")
        _require_aware_iso(self.reset_deadline, "reset_deadline")
        _require_aware_iso(self.not_before, "not_before")
        for name, value in (
            ("job_id", self.job_id),
            ("handoff_id", self.handoff_id),
        ):
            if value is not None:
                _require_nonempty_str(value, name)


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
        run_id: str | None = None,
        long_worker_observer: LongWorkerObserver | None = None,
        quota_preemption: QuotaPreemptionPort | None = None,
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
        self._run_id = run_id or wave.wave_id
        self._observer = long_worker_observer
        self._preemption_port = quota_preemption
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
        self._preemptions: dict[str, LanePreemption] = {}

    # -- read-only views ---------------------------------------------------

    @property
    def wave(self) -> Wave:
        return self._wave

    @property
    def run_id(self) -> str:
        """The durable run identity handoffs and observations attach to."""
        return self._run_id

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

    # -- run-level handoffs (WX-2) -----------------------------------------

    def emit_attempt_handoff(
        self,
        lane_id: str,
        *,
        succeeded: bool,
        summary: str,
        attempt_id: str | None = None,
        candidate_sha: str | None = None,
        source_events: Sequence[str] = (),
        artifacts: Sequence[str] = (),
        tests_passed: int | None = None,
        tests_failed: int | None = None,
        failed_tests: Sequence[str] = (),
        repair_suggestions: Sequence[str] = (),
        created_at: str | None = None,
    ) -> WaveHandoff:
        """Emit a factual handoff for one finished lane attempt.

        Covers: WX-2 (handoffs on attempt completion and failure). The record
        is assembled only from the durable anchors the caller holds -- the
        attempt id, candidate SHA, recorded event names and test counts -- so a
        continuation worker resumes from persisted state, never model memory.
        A failure additionally carries a bounded repair route: the lane's own
        worker route while the fix-round budget permits, otherwise an explicit
        ``ESCALATE`` so a failed candidate can never open an unconstrained
        repair loop.
        """
        run = self.lane_run(lane_id)
        lane = self._wave.lane_by_id(lane_id)
        summary = _require_nonempty_str(summary, "summary")
        if candidate_sha is not None:
            candidate_sha = _require_sha(candidate_sha, "candidate_sha")
        for name, value in (
            ("tests_passed", tests_passed),
            ("tests_failed", tests_failed),
        ):
            if value is not None:
                _require_count(value, name)
        kind = HANDOFF_ATTEMPT_COMPLETED if succeeded else HANDOFF_ATTEMPT_FAILED
        repair_route: str | None = None
        escalate = False
        if not succeeded:
            limit = self._wave.budget("fix_rounds", 2)
            escalate = run.fix_rounds >= limit
            repair_route = REPAIR_ESCALATE if escalate else lane.worker_route
        handoff = WaveHandoff(
            kind=kind,
            run_id=self._run_id,
            wave_id=self._wave.wave_id,
            lane_id=lane_id,
            summary=summary,
            created_at=created_at or self._now_iso(),
            attempt_id=attempt_id,
            candidate_sha=candidate_sha,
            artifacts=tuple(artifacts),
            tests_passed=tests_passed,
            tests_failed=tests_failed,
            failed_tests=tuple(failed_tests),
            repair_suggestions=tuple(repair_suggestions),
            repair_route=repair_route,
            escalate=escalate,
            source_events=tuple(source_events),
        )
        self._emit(EVENT_ATTEMPT_HANDOFF, handoff.as_payload())
        return handoff

    def observe_long_worker(
        self,
        lane_id: str,
        *,
        terminal_event_exists: bool,
        recent_activity: bool,
        timed_out: bool = False,
        stalled: bool = False,
        health_reason: str | None = None,
        created_at: str | None = None,
        source_events: Sequence[str] = ("supervisor_observation",),
    ) -> WaveHandoff:
        """Persist the t=300 long-worker observation and emit its handoff.

        Covers: WX-2 (the existing ``observe_long_worker`` transition wired into
        production). Delegates the durable supervision transition to the
        injected observer -- the run supervisor -- and records the resulting
        health as a factual handoff. Nothing here polls, sleeps or calls a
        model; the observation boundary is the caller's.
        """
        self.lane_run(lane_id)
        if self._observer is None:
            raise WaveExecutorError("no long-worker observer is configured")
        outcome = self._observer.observe_long_worker(
            self._run_id,
            terminal_event_exists=terminal_event_exists,
            recent_activity=recent_activity,
            timed_out=timed_out,
            stalled=stalled,
        )
        health = str(getattr(outcome, "value", outcome))
        handoff = WaveHandoff(
            kind=HANDOFF_LONG_WORKER,
            run_id=self._run_id,
            wave_id=self._wave.wave_id,
            lane_id=lane_id,
            summary=f"long-worker observation: {health}",
            created_at=created_at or self._now_iso(),
            health=health,
            health_reason=health_reason,
            source_events=tuple(source_events),
        )
        self._emit(EVENT_LONG_WORKER_HANDOFF, handoff.as_payload())
        return handoff

    # -- quota preemption & auto-resume (WX-2 / M2-5) ----------------------

    def lane_preemption(self, lane_id: str) -> LanePreemption | None:
        """Return the durable park record for one lane, if it is parked."""
        self.lane_run(lane_id)
        return self._preemptions.get(lane_id)

    def preempt_lane(
        self,
        lane_id: str,
        *,
        attempt_terminal: bool,
        fallback_admitted: bool,
        repo: str | None = None,
        job_id: str | None = None,
        worktree: str | None = None,
        base_sha: str | None = None,
        reason: str = PREEMPTION_REASON,
    ) -> LanePreemption:
        """Park an executing lane on quota exhaustion with no admitted fallback.

        Covers: WX-2 + ADR-0002 D8 / M2-5. The lane must be executing and its
        attempt must already have ended -- quota is never preempted mid-attempt.
        A quota signal with an admitted fallback model is not a preemption and is
        refused, so ordinary fallback routing continues. The injected M2-5 port
        captures and anchors the checkpoint, parses the reset deadline and
        returns the owning durable job; the lane then moves to ``PREEMPTED``
        with the exact park record, and ``resume_preempted_lane`` returns it to
        the main route once the exclusion window has expired.
        """
        run = self.lane_run(lane_id)
        if self._preemption_port is None:
            raise WaveExecutorError("no quota preemption port is configured")
        if fallback_admitted:
            raise WaveExecutorError(
                f"lane {lane_id!r} has an admitted fallback; a quota signal is not a preemption"
            )
        if not attempt_terminal:
            raise WaveExecutorError(
                f"lane {lane_id!r} attempt is not terminal; never preempt mid-attempt"
            )
        if LANE_STATE_TO_SLOT_STATE[run.state] not in OCCUPYING_SLOT_STATES:
            raise WaveExecutorError(
                f"lane {lane_id!r} is not executing in state {run.state.value}"
            )
        request = QuotaPreemptionRequest(
            run_id=self._run_id,
            lane_id=lane_id,
            repo=repo if repo is not None else self._repo,
            base_sha=base_sha if base_sha is not None else self._wave.base_sha,
            job_id=job_id,
            worktree=worktree if worktree is not None else run.worktree,
            reason=reason,
        )
        receipt = self._preemption_port.preempt(request)
        record = LanePreemption(
            lane_id=lane_id,
            checkpoint_ref=receipt.checkpoint_ref,
            checkpoint_sha=receipt.checkpoint_sha,
            reason=reason,
            preemptions=receipt.preemptions,
            job_id=receipt.job_id,
            reset_deadline=receipt.reset_deadline,
            not_before=receipt.not_before,
        )
        self._set_lane(lane_id, LaneState.PREEMPTED)
        self._preemptions[lane_id] = record
        self._emit(
            EVENT_LANE_PREEMPTED,
            {
                "wave_id": self._wave.wave_id,
                "run_id": self._run_id,
                **record.as_dict(),
            },
        )
        return record

    def resume_preempted_lane(self, lane_id: str, *, now: datetime) -> LaneRun:
        """``PREEMPTED -> RUNNING`` once the exclusion window has expired.

        Covers: WX-2 (the next dispatch returns to the main model after the
        parsed reset, never mid-attempt). Refuses before ``not_before`` so a
        parked lane can never be dispatched early. The lane resumes into its own
        worktree and the emitted event names the lane's own worker route, which
        is the main model the next dispatch uses.
        """
        run = self.lane_run(lane_id)
        if run.state is not LaneState.PREEMPTED:
            raise WaveExecutorError(
                f"lane {lane_id!r} is not PREEMPTED (state {run.state.value})"
            )
        record = self._preemptions.get(lane_id)
        if record is None:
            raise WaveExecutorError(f"lane {lane_id!r} has no durable park record")
        if now.tzinfo is None:
            raise WaveExecutorError("now must be a timezone-aware datetime")
        if not record.exclusion_expired(now):
            raise WaveExecutorError(
                f"lane {lane_id!r} exclusion window has not expired "
                f"(not_before {record.not_before})"
            )
        resumed = self._set_lane(lane_id, LaneState.RUNNING)
        route = self._wave.lane_by_id(lane_id).worker_route
        self._emit(
            EVENT_LANE_RESUMED,
            {
                "wave_id": self._wave.wave_id,
                "run_id": self._run_id,
                "lane_id": lane_id,
                "worker_route": route,
                "job_id": record.job_id,
                "checkpoint_ref": record.checkpoint_ref,
                "checkpoint_sha": record.checkpoint_sha,
                "not_before": record.not_before,
                "dispatch_model": "main",
            },
        )
        return resumed

    def _now_iso(self) -> str:
        from saberops.db import current_iso_timestamp

        return current_iso_timestamp()

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
