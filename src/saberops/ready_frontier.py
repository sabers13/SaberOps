"""Deterministic cross-phase ready-frontier projection (Step 015 / PL-1-PF).

What this module is
-------------------
One *pure* function, :func:`project_ready_frontier`, that consumes an
**authoritative, explicitly complete** snapshot of per-project plans and returns
the deterministic set of tasks that may be *started now*, plus a reasoned hold
for every task that may not. It recomputes the eligible set across **all named
phases at once**: a task in Phase 4 is startable while a Phase 1 dependency is
blocked, because phases and three-digit display steps are presentation labels,
never dependency edges (WORKFLOW.md sections 15-17, ADR-0004 Amendment 5).

What this module is deliberately NOT
------------------------------------
This is a *readiness projection*, not a scheduler and not a launch authority.
It performs no I/O, reads no database, spawns no process, consults no clock,
uses no randomness and calls no model or provider. Given the same immutable
inputs it returns the same typed result. It does not fabricate tasks, split a
task, dispatch a worker, allocate a filesystem reservation, choose a paid
provider or mutate a project database.

It is also **not** a replacement for either existing mechanism, and it does not
reimplement them:

* ``saberops.planning.represent_independent_ready_work`` / ``ready_dependents``
  are the *persisted within-run* plan/package dependency authority. They issue
  dependency-state transitions against the run database and validate the frozen
  project write envelope. This module never reads or advances that state; it
  consumes already-validated task facts supplied by a trusted caller.
* ``saberops.project_slot_policy.evaluate_slot_admission`` (Step 010 / WX-1) is
  the *live* 3x5 execution-slot admission contract against observed occupancy,
  with a mandatory transactional/lease caller. This module performs **no slot
  admission**: it has no notion of reserved or executing slots, no lease, no
  compare-and-swap and no occupancy read. The :class:`FrontierLimits` it accepts
  are a declared planning bound echoed from the owner policy so the *ready set*
  can be reported without silently under-reporting it (the projection never
  lowers the owner's 3x5 default). Step 010 may then admit fewer, never more.
* ``saberops.parallel_work.execute_team`` is the current single-run 2-4 worker
  team executor. This module computes no wave and launches nothing.

The 3x5 relationship in one sentence: this projection answers *"which tasks are
ready and, up to the owner's declared per-project and active-project maxima,
reportable as a ready set across all phases?"*; Step 010 answers *"given
currently occupied slots, may this specific candidate start, transactionally?"*.
Only the second is launch authority.

Semantics
---------
* A hard **START** dependency (``start_dependencies``) must be ``COMPLETED``
  before a dependent may start. A blocked or failed dependency holds its
  dependents and nothing else; unrelated lanes stay eligible.
* A **deferred integration prerequisite** (``integration_prerequisites``) is
  recorded and surfaced but never blocks a start. Integration-ready is not
  implementation-startable, and implementation-startable is not integrated.
* A task occupies exactly one execution lane only when it is a
  ``CHILD_TASK``; plan/wave wrappers and Orchestrator turns are coordination
  records and are reported as such, never as extra child tasks. Identical
  duplicate records are one alias.
* Declared write scopes are **canonical repo-relative paths namespaced by the
  trusted registered ``project_id``** the caller attached to the row. Two
  projects owning the same relative path (``p1/src/main.py`` and
  ``p2/src/main.py``) are unrelated and may both be eligible; two tasks of one
  project owning the same path or subtree conflict. Every executing
  (``RUNNING``) child task reserves its scopes before any new candidate is
  examined, so no ready/pending task is reported over a running writer.
  Non-executing holders (``OWNER_WAIT`` / ``RESOURCE_WAIT`` / ``BLOCKED`` /
  terminal / coordination) execute no resource and reserve no scope.
  Benign scope spellings are folded (``a/b``, ``a/b/``, ``./a/b``), while
  absolute, ``..``-traversing, backslash or glob-bearing scopes are rejected as
  unsafe rather than silently accepted (``UNSAFE_WRITE_SCOPE``).
* Unknown statuses, unknown kinds, missing/conflicting identities, unsafe write
  scopes, missing dependency targets, dependency cycles, incomplete snapshots
  and non authoritative sources **fail closed**: the whole projection is denied
  and no task is reported eligible.

The optional :data:`REQUIRES_TRANSACTIONAL_VERIFICATION` flag is always true:
no production launch may trust this pure projection on its own. A real launch
path MUST hold an exclusive lease/write fence, re-verify snapshot completeness
and freshness inside the same critical section, and defer slot admission to
Step 010's transactional caller.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath

__all__ = [
    "DEFAULT_ACTIVE_PROJECT_LIMIT",
    "DEFAULT_CHILD_SLOTS_PER_PROJECT_LIMIT",
    "ELIGIBLE_REASON",
    "FrontierLimits",
    "FrontierProjection",
    "FrontierReason",
    "FrontierRequest",
    "FrontierSnapshot",
    "HeldTask",
    "EligibleTask",
    "PlanRunReport",
    "PlanTask",
    "ProjectPlan",
    "ProjectionStatus",
    "READY_FRONTIER_VERSION",
    "REQUIRES_TRANSACTIONAL_VERIFICATION",
    "ScheduledTask",
    "ScheduledWave",
    "SnapshotSource",
    "TaskKind",
    "TaskRef",
    "TaskStatus",
    "WaveScheduleError",
    "build_plan_run_report",
    "default_frontier_limits",
    "project_ready_frontier",
    "schedule_chained_waves",
]

READY_FRONTIER_VERSION = "ready-frontier/1"

#: Owner-approved default: at most three concurrently active projects.
DEFAULT_ACTIVE_PROJECT_LIMIT = 3

#: Owner-approved default: at most five executing child tasks per active project.
DEFAULT_CHILD_SLOTS_PER_PROJECT_LIMIT = 5

#: Always true: this pure projection is never, by itself, launch authority.
REQUIRES_TRANSACTIONAL_VERIFICATION = True


class SnapshotSource(StrEnum):
    """Origin of a plan snapshot.

    Only :attr:`OWNER_DB` is authoritative. Any other value (including the
    explicit :attr:`UNKNOWN`) fails closed, because an unverifiable snapshot
    cannot establish a trustworthy ready set.
    """

    OWNER_DB = "owner_db"
    UNKNOWN = "unknown"


class TaskKind(StrEnum):
    """Kind of a record observed in a plan snapshot.

    Only :attr:`CHILD_TASK` records are independently executable lanes. A
    plan/wave coordinator (:attr:`PLAN_WRAPPER` / :attr:`WAVE_WRAPPER`) and an
    Orchestrator turn (:attr:`ORCHESTRATOR_TURN`) are coordination records and
    never become extra child tasks.
    """

    CHILD_TASK = "child_task"
    PLAN_WRAPPER = "plan_wrapper"
    WAVE_WRAPPER = "wave_wrapper"
    ORCHESTRATOR_TURN = "orchestrator_turn"


class TaskStatus(StrEnum):
    """Authoritative lifecycle status of one task as observed in a snapshot.

    ``OWNER_WAIT`` and ``RESOURCE_WAIT`` are declared resource/capacity
    constraints: the task is not yet startable because an owner decision or a
    host resource is pending. They are holds, not failures, and they hold only
    their own dependents.
    """

    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    BLOCKED = "blocked"
    OWNER_WAIT = "owner_wait"
    RESOURCE_WAIT = "resource_wait"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class FrontierReason(StrEnum):
    """Stable machine-readable reason codes, in evaluation precedence order."""

    ELIGIBLE = "eligible"

    # Structural: a single occurrence denies the whole projection (fail closed).
    INVALID_LIMIT = "invalid_limit"
    MISSING_IDENTITY = "missing_identity"
    UNKNOWN_TASK_KIND = "unknown_task_kind"
    UNKNOWN_TASK_STATUS = "unknown_task_status"
    UNKNOWN_SNAPSHOT_SOURCE = "unknown_snapshot_source"
    INCOMPLETE_SNAPSHOT = "incomplete_snapshot"
    UNSAFE_WRITE_SCOPE = "unsafe_write_scope"
    #: Reserved for the future owner-identity integration check. Not emitted by
    #: this projection: per-project owner verification requires the registered
    #: owner/project identity service (see the integration-handoff obligation).
    #: It is kept so an integration consumer does not have to guess a new code.
    OWNER_MISMATCH = "owner_mismatch"
    CONFLICTING_TASK_IDENTITY = "conflicting_task_identity"
    MISSING_DEPENDENCY = "missing_dependency"
    MISSING_INTEGRATION_PREREQUISITE = "missing_integration_prerequisite"
    CYCLIC_DEPENDENCY = "cyclic_dependency"
    CYCLIC_INTEGRATION_PREREQUISITE = "cyclic_integration_prerequisite"

    # Per-task holds: a valid snapshot where this task may not start yet.
    COORDINATION_RECORD = "coordination_record"
    TASK_RUNNING = "task_running"
    TASK_TERMINAL = "task_terminal"
    TASK_BLOCKED = "task_blocked"
    OWNER_WAIT = "owner_wait"
    RESOURCE_WAIT = "resource_wait"
    DEPENDENCY_BLOCKED = "dependency_blocked"
    DEPENDENCY_PENDING = "dependency_pending"
    WRITE_SCOPE_CONFLICT = "write_scope_conflict"
    CAPACITY_WAIT = "capacity_wait"


#: Sentinel reason attached to every :class:`EligibleTask` for a single shape.
ELIGIBLE_REASON = FrontierReason.ELIGIBLE

# Ordering is the enum definition order above; it makes multi-reason output
# deterministic and independent of dict/set iteration order.
_REASON_INDEX: dict[FrontierReason, int] = {
    reason: index for index, reason in enumerate(FrontierReason)
}

_STRUCTURAL_REASONS: frozenset[FrontierReason] = frozenset(
    {
        FrontierReason.INVALID_LIMIT,
        FrontierReason.MISSING_IDENTITY,
        FrontierReason.UNKNOWN_TASK_KIND,
        FrontierReason.UNKNOWN_TASK_STATUS,
        FrontierReason.UNKNOWN_SNAPSHOT_SOURCE,
        FrontierReason.INCOMPLETE_SNAPSHOT,
        FrontierReason.UNSAFE_WRITE_SCOPE,
        FrontierReason.OWNER_MISMATCH,
        FrontierReason.CONFLICTING_TASK_IDENTITY,
        FrontierReason.MISSING_DEPENDENCY,
        FrontierReason.MISSING_INTEGRATION_PREREQUISITE,
        FrontierReason.CYCLIC_DEPENDENCY,
        FrontierReason.CYCLIC_INTEGRATION_PREREQUISITE,
    }
)

#: Start statuses for a task that has not yet begun and is not waiting.
_STARTABLE_STATUSES: frozenset[str] = frozenset(
    {TaskStatus.PENDING.value, TaskStatus.READY.value}
)

#: A dependency in one of these states can never satisfy a hard START edge.
_INCAPACITATED_STATUSES: frozenset[str] = frozenset(
    {TaskStatus.BLOCKED.value, TaskStatus.FAILED.value, TaskStatus.SKIPPED.value}
)

_STATUS_VALUES: frozenset[str] = frozenset(member.value for member in TaskStatus)
_KIND_VALUES: frozenset[str] = frozenset(member.value for member in TaskKind)


class ProjectionStatus(StrEnum):
    """Typed outcome of one ready-frontier projection."""

    PROJECTED = "PROJECTED"
    """The snapshot was valid; the eligible set and holds are authoritative."""

    DENIED = "DENIED"
    """The snapshot was unverifiable, invalid or conflicting; fail closed."""


def _normalized(value: object) -> str:
    """Trim and lowercase an enum-like token for case-insensitive validation."""
    return str(value).strip().lower()


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _clean_text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _normalized_scope(raw: object) -> str | None:
    """Return the canonical repo-relative form of one declared write scope.

    Benign presentation variants are folded so they can never silently appear
    disjoint: ``a/b``, ``a/b/``, ``a//b``, ``a/./b`` and ``./a/b`` all name the
    same scope. Genuinely unsafe forms are rejected (``None``) rather than
    accepted: absolute paths, ``..`` traversal, backslashes, embedded NUL and
    glob metacharacters other than the recognised trailing ``/**`` subtree
    suffix. A caller that supplies an unsafe scope is a validation error, not an
    empty scope, so the projection fails closed instead of quietly ignoring it.

    This mirrors the strict canonical-form validation used by
    ``saberops.planning`` while additionally folding benign variants.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text or "\\" in text or "\x00" in text:
        return None
    subtree = text.endswith("/**")
    literal = text[:-3] if subtree else text
    if not literal or any(char in literal for char in "*?[]"):
        return None
    path = PurePosixPath(literal)
    if path.is_absolute() or ".." in path.parts:
        return None
    canonical = path.as_posix()
    if canonical in ("", "."):
        return None
    return f"{canonical}/**" if subtree else canonical


def _clean_scopes(scopes: tuple[str, ...]) -> tuple[tuple[str, ...], bool]:
    """Return canonical, de-duplicated scopes plus a safety flag.

    The flag is ``False`` when any declared scope is unsafe or ambiguous (see
    :func:`_normalized_scope`). Blank entries declare no scope and are ignored,
    which is documented and covered, not silent.
    """
    cleaned: list[str] = []
    safe = True
    for scope in scopes:
        normalized = _normalized_scope(scope)
        if normalized is None:
            if isinstance(scope, str) and not scope.strip():
                continue
            safe = False
            continue
        if normalized not in cleaned:
            cleaned.append(normalized)
    return tuple(cleaned), safe


@dataclass(frozen=True)
class TaskRef:
    """Immutable stable identity of one task: its ``(project_id, task_id)``.

    A display step number is never part of task identity and is never used as a
    dependency reference.
    """

    project_id: str
    task_id: str

    @property
    def is_valid(self) -> bool:
        """True only when both identity components are non-empty."""
        return bool(_clean_text(self.project_id)) and bool(_clean_text(self.task_id))


@dataclass(frozen=True)
class PlanTask:
    """One already-validated task fact supplied by a trusted caller.

    ``status`` and ``kind`` are untrusted strings validated against
    :class:`TaskStatus` and :class:`TaskKind`, so an unknown or misspelled value
    fails closed instead of being silently ignored. ``start_dependencies`` are
    hard START edges; ``integration_prerequisites`` are deferred integration
    edges that never block a start. ``owns`` declares the task's exact write
    scope as canonical repository-relative paths, interpreted relative to that
    task's own project identity (``ProjectPlan.project_id``): the same relative
    path in a different project is a different physical file. Benign spelling
    variants are folded; unsafe (absolute, traversal, backslash, glob) scopes
    fail the projection closed. ``phase`` and ``display_step`` are presentation
    only.
    """

    task_id: str
    phase: str
    status: str
    kind: str = TaskKind.CHILD_TASK.value
    title: str = ""
    display_step: str | None = None
    start_dependencies: tuple[TaskRef, ...] = ()
    integration_prerequisites: tuple[TaskRef, ...] = ()
    owns: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProjectPlan:
    """A complete per-project slice of one authoritative plan snapshot."""

    project_id: str
    tasks: tuple[PlanTask, ...] = ()


@dataclass(frozen=True)
class FrontierSnapshot:
    """An explicitly complete, authoritative observation of per-project plans.

    ``source`` must name the authoritative owner database. ``complete`` is the
    caller's explicit assertion that the enumeration is whole; the pure
    projection can check the flag but cannot itself prove completeness -- see
    the module docstring for the mandatory transactional caller.
    """

    owner_id: str
    source: str
    complete: bool
    projects: tuple[ProjectPlan, ...] = ()

    @property
    def is_authoritative(self) -> bool:
        """True only for a complete snapshot from the authoritative source."""
        return _normalized(self.source) == SnapshotSource.OWNER_DB.value and self.complete


@dataclass(frozen=True)
class FrontierLimits:
    """Declared owner maxima for the ready-set report.

    Both dimensions are independent. These are a *planning bound* echoed from
    the owner's policy so the projection never silently under-reports ready work
    and never lowers the owner's 3x5 default; they are **not** live slot
    admission. ``derived_global_ceiling`` is reported for observability only.
    """

    active_projects: int = DEFAULT_ACTIVE_PROJECT_LIMIT
    child_slots_per_project: int = DEFAULT_CHILD_SLOTS_PER_PROJECT_LIMIT

    @property
    def derived_global_ceiling(self) -> int:
        """Reported-only product of the two limits (default ``3 * 5 == 15``)."""
        return self.active_projects * self.child_slots_per_project


def default_frontier_limits() -> FrontierLimits:
    """Return the owner-approved default limits (three projects, five each)."""
    return FrontierLimits(
        active_projects=DEFAULT_ACTIVE_PROJECT_LIMIT,
        child_slots_per_project=DEFAULT_CHILD_SLOTS_PER_PROJECT_LIMIT,
    )


@dataclass(frozen=True)
class FrontierRequest:
    """A snapshot plus the owner maxima to report against."""

    snapshot: FrontierSnapshot
    limits: FrontierLimits = field(default_factory=default_frontier_limits)


@dataclass(frozen=True)
class EligibleTask:
    """One task that may be started now, with its immutable display identity.

    ``requires_integration`` is true when the task carries deferred integration
    prerequisites. It is informational: integration readiness never blocked the
    start and never means the task is integrated.
    """

    project_id: str
    task_id: str
    phase: str
    display_step: str | None
    title: str
    owns: tuple[str, ...]
    integration_prerequisites: tuple[TaskRef, ...]

    @property
    def requires_integration(self) -> bool:
        """True iff this task still has a deferred integration prerequisite."""
        return bool(self.integration_prerequisites)

    @property
    def stable_ref(self) -> TaskRef:
        """The immutable stable identity of this task."""
        return TaskRef(project_id=self.project_id, task_id=self.task_id)


@dataclass(frozen=True)
class HeldTask:
    """One non-eligible task with its deterministic, ordered hold reasons."""

    project_id: str
    task_id: str
    phase: str
    display_step: str | None
    status: str
    kind: str
    reasons: tuple[FrontierReason, ...]

    @property
    def reason(self) -> FrontierReason:
        """The highest-precedence hold reason."""
        return self.reasons[0]

    @property
    def stable_ref(self) -> TaskRef:
        """The immutable stable identity of this task."""
        return TaskRef(project_id=self.project_id, task_id=self.task_id)

    def reason_codes(self) -> tuple[str, ...]:
        """Reason codes as plain strings, in deterministic precedence order."""
        return tuple(reason.value for reason in self.reasons)


@dataclass(frozen=True)
class FrontierProjection:
    """The stable, typed result of :func:`project_ready_frontier`."""

    status: ProjectionStatus
    reasons: tuple[FrontierReason, ...]
    eligible: tuple[EligibleTask, ...]
    held: tuple[HeldTask, ...]
    project_order: tuple[str, ...]
    limits: FrontierLimits
    counts_authoritative: bool
    detail: str

    #: Always true: a pure projection is never launch authority on its own.
    requires_transactional_verification: bool = REQUIRES_TRANSACTIONAL_VERIFICATION

    @property
    def projected(self) -> bool:
        """True iff the snapshot was valid and the ready set is authoritative."""
        return self.status is ProjectionStatus.PROJECTED

    @property
    def reason(self) -> FrontierReason:
        """The highest-precedence projection-level reason code."""
        return self.reasons[0]

    @property
    def derived_global_ceiling(self) -> int:
        """Reported-only product of the declared owner limits."""
        return self.limits.derived_global_ceiling

    @property
    def eligible_count(self) -> int:
        """Number of tasks reported eligible."""
        return len(self.eligible)

    @property
    def held_count(self) -> int:
        """Number of non-eligible tasks reported with at least one hold."""
        return len(self.held)

    @property
    def coordination_count(self) -> int:
        """Number of held records that are coordination, not child tasks."""
        return sum(
            1 for task in self.held if FrontierReason.COORDINATION_RECORD in task.reasons
        )

    def eligible_refs(self) -> tuple[TaskRef, ...]:
        """Eligible tasks' stable identities, in deterministic order."""
        return tuple(task.stable_ref for task in self.eligible)

    def reason_codes(self) -> tuple[str, ...]:
        """Projection-level reason codes as plain strings."""
        return tuple(reason.value for reason in self.reasons)


@dataclass(frozen=True)
class _ValidatedTask:
    """Internal normalized view of one validated task row."""

    project_id: str
    task_id: str
    phase: str
    kind: str
    status: str
    display_step: str | None
    title: str
    start_dependencies: tuple[TaskRef, ...]
    integration_prerequisites: tuple[TaskRef, ...]
    owns: tuple[str, ...]

    @property
    def ref(self) -> TaskRef:
        return TaskRef(project_id=self.project_id, task_id=self.task_id)

    @property
    def sort_key(self) -> tuple[str, str]:
        # Stable identity order only: phase and display_step never reorder lanes.
        return (self.project_id, self.task_id)


def _ordered_reasons(collected: list[FrontierReason]) -> tuple[FrontierReason, ...]:
    unique = set(collected)
    return tuple(sorted(unique, key=lambda reason: _REASON_INDEX[reason]))


def _path_owned(path: str, scope: str) -> bool:
    return path == scope or (scope.endswith("/**") and path.startswith(scope[:-2]))


def _scopes_overlap(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    """Return True when two declared write scopes overlap.

    Mirrors ``saberops.parallel_work.scopes_overlap`` semantics (exact match or
    one subtree containing the other) but is implemented locally so this module
    stays pure and free of database imports.
    """
    return any(
        _path_owned(a[:-3] if a.endswith("/**") else a, b)
        or _path_owned(b[:-3] if b.endswith("/**") else b, a)
        for a in left
        for b in right
    )


def _denied(reasons: list[FrontierReason], limits: FrontierLimits) -> FrontierProjection:
    return FrontierProjection(
        status=ProjectionStatus.DENIED,
        reasons=_ordered_reasons(reasons),
        eligible=(),
        held=(),
        project_order=(),
        limits=limits,
        counts_authoritative=False,
        detail="denied: snapshot could not be fully validated (fail closed)",
    )


def _validate(
    request: FrontierRequest,
) -> tuple[list[FrontierReason], dict[TaskRef, _ValidatedTask]]:
    """Validate the request and return structural reasons plus a task index.

    The returned index is authoritative only when ``reasons`` is empty.
    """
    snapshot = request.snapshot
    limits = request.limits
    reasons: list[FrontierReason] = []

    if not _is_positive_int(limits.active_projects) or not _is_positive_int(
        limits.child_slots_per_project
    ):
        reasons.append(FrontierReason.INVALID_LIMIT)

    if _normalized(snapshot.source) != SnapshotSource.OWNER_DB.value:
        reasons.append(FrontierReason.UNKNOWN_SNAPSHOT_SOURCE)

    if not snapshot.complete:
        reasons.append(FrontierReason.INCOMPLETE_SNAPSHOT)

    if not _clean_text(snapshot.owner_id):
        reasons.append(FrontierReason.MISSING_IDENTITY)

    # (project_id -> task_id -> record); duplicate identical rows are one alias
    # and duplicate project slices for one project_id merge.
    by_project: dict[str, dict[str, _ValidatedTask]] = {}
    for project in snapshot.projects:
        project_id = _clean_text(project.project_id)
        if not project_id:
            reasons.append(FrontierReason.MISSING_IDENTITY)
            continue
        tasks = by_project.setdefault(project_id, {})
        for task in project.tasks:
            task_id = _clean_text(task.task_id)
            if not task_id:
                reasons.append(FrontierReason.MISSING_IDENTITY)
                continue
            kind = _normalized(task.kind)
            status = _normalized(task.status)
            if kind not in _KIND_VALUES:
                reasons.append(FrontierReason.UNKNOWN_TASK_KIND)
            if status not in _STATUS_VALUES:
                reasons.append(FrontierReason.UNKNOWN_TASK_STATUS)
            display_step = _clean_text(task.display_step) or None
            owns, scopes_safe = _clean_scopes(task.owns)
            if not scopes_safe:
                reasons.append(FrontierReason.UNSAFE_WRITE_SCOPE)
            row = _ValidatedTask(
                project_id=project_id,
                task_id=task_id,
                phase=_clean_text(task.phase),
                kind=kind,
                status=status,
                display_step=display_step,
                title=_clean_text(task.title),
                start_dependencies=tuple(task.start_dependencies),
                integration_prerequisites=tuple(task.integration_prerequisites),
                owns=owns,
            )
            existing = tasks.get(task_id)
            if existing is not None and existing != row:
                reasons.append(FrontierReason.CONFLICTING_TASK_IDENTITY)
                continue
            tasks[task_id] = row

    index: dict[TaskRef, _ValidatedTask] = {
        TaskRef(project_id=project_id, task_id=task_id): row
        for project_id, tasks in by_project.items()
        for task_id, row in tasks.items()
    }

    # Resolve every referenced identity before any cycle reasoning.
    for row in sorted(index.values(), key=lambda item: item.sort_key):
        for dep in row.start_dependencies:
            if not dep.is_valid or dep not in index:
                reasons.append(FrontierReason.MISSING_DEPENDENCY)
        for prereq in row.integration_prerequisites:
            if not prereq.is_valid or prereq not in index:
                reasons.append(FrontierReason.MISSING_INTEGRATION_PREREQUISITE)

    if reasons:
        return reasons, index

    if _has_cycle(index, edges=lambda row: row.start_dependencies):
        reasons.append(FrontierReason.CYCLIC_DEPENDENCY)
    if _has_cycle(index, edges=lambda row: row.integration_prerequisites):
        reasons.append(FrontierReason.CYCLIC_INTEGRATION_PREREQUISITE)

    return reasons, index


def _has_cycle(
    index: dict[TaskRef, _ValidatedTask],
    *,
    edges: Callable[[_ValidatedTask], tuple[TaskRef, ...]],
) -> bool:
    """Deterministic DFS cycle detection over one edge kind."""
    visiting: set[TaskRef] = set()
    done: set[TaskRef] = set()

    def walk(node: TaskRef) -> bool:
        if node in done:
            return False
        if node in visiting:
            return True
        visiting.add(node)
        for nxt in edges(index[node]):
            if nxt in index and walk(nxt):
                return True
        visiting.discard(node)
        done.add(node)
        return False

    for ref in sorted(index, key=lambda item: (item.project_id, item.task_id)):
        if walk(ref):
            return True
    return False


def _dependency_hold(
    row: _ValidatedTask, index: dict[TaskRef, _ValidatedTask]
) -> FrontierReason | None:
    """Return the hold reason from unsatisfied hard START edges, if any."""
    blocked = False
    pending = False
    for dep_ref in row.start_dependencies:
        dep = index[dep_ref]
        if dep.status == TaskStatus.COMPLETED.value:
            continue
        if dep.status in _INCAPACITATED_STATUSES:
            blocked = True
        else:
            pending = True
    if blocked:
        return FrontierReason.DEPENDENCY_BLOCKED
    if pending:
        return FrontierReason.DEPENDENCY_PENDING
    return None


def _status_hold(row: _ValidatedTask) -> FrontierReason | None:
    """Return the self-hold reason for a task that cannot start as observed."""
    if row.kind != TaskKind.CHILD_TASK.value:
        return FrontierReason.COORDINATION_RECORD
    if row.status in _STARTABLE_STATUSES:
        return None
    return {
        TaskStatus.RUNNING.value: FrontierReason.TASK_RUNNING,
        TaskStatus.COMPLETED.value: FrontierReason.TASK_TERMINAL,
        TaskStatus.FAILED.value: FrontierReason.TASK_TERMINAL,
        TaskStatus.SKIPPED.value: FrontierReason.TASK_TERMINAL,
        TaskStatus.BLOCKED.value: FrontierReason.TASK_BLOCKED,
        TaskStatus.OWNER_WAIT.value: FrontierReason.OWNER_WAIT,
        TaskStatus.RESOURCE_WAIT.value: FrontierReason.RESOURCE_WAIT,
    }[row.status]


def _project(index: dict[TaskRef, _ValidatedTask], limits: FrontierLimits) -> tuple[
    tuple[EligibleTask, ...], tuple[HeldTask, ...], tuple[str, ...]
]:
    """Compute deterministic eligibles and holds.

    Ordering is by immutable stable identity ``(project_id, task_id)``. Phase
    names and three-digit display steps never reorder or gate a lane.

    Write scopes are namespaced by the trusted registered ``project_id`` the
    caller attached to the row: two projects owning the same *relative* path are
    unrelated and may both be eligible, while two tasks of one project owning the
    same path conflict. Reservations are two-pass over the complete authoritative
    snapshot: every executing (``RUNNING``) child task reserves its scopes first,
    so no newly startable candidate can be reported over a running writer even
    when the running row sorts later by stable identity. A task that is not
    executing (``OWNER_WAIT`` / ``RESOURCE_WAIT`` / ``BLOCKED`` / terminal /
    coordination) holds no executing resource and therefore reserves nothing;
    it still holds only its own START dependents.
    """
    ordered = sorted(index.values(), key=lambda item: item.sort_key)

    eligible: list[EligibleTask] = []
    held: list[tuple[_ValidatedTask, list[FrontierReason]]] = []

    accepted_projects: set[str] = set()
    per_project_count: dict[str, int] = {}

    # Pass 1: executing child tasks are authoritative active writers. Their
    # scopes are reserved per owning project before any candidate is examined.
    reserved_scopes: dict[str, list[tuple[str, ...]]] = {}
    for row in ordered:
        if (
            row.kind == TaskKind.CHILD_TASK.value
            and row.status == TaskStatus.RUNNING.value
            and row.owns
        ):
            reserved_scopes.setdefault(row.project_id, []).append(row.owns)

    # Pass 2: evaluate startable candidates against the pre-reserved writers and
    # against scopes reserved earlier in this same pass.
    for row in ordered:
        status_reason = _status_hold(row)
        if status_reason is not None:
            held.append((row, [status_reason]))
            continue

        dep_reason = _dependency_hold(row, index)
        if dep_reason is not None:
            held.append((row, [dep_reason]))
            continue

        reasons: list[FrontierReason] = []

        # Declared planning bound: never open a new active project beyond the
        # owner maximum, and never exceed the per-project maximum.
        at_project_limit = (
            row.project_id not in accepted_projects
            and len(accepted_projects) >= limits.active_projects
        )
        at_child_limit = (
            per_project_count.get(row.project_id, 0) >= limits.child_slots_per_project
        )
        if at_project_limit or at_child_limit:
            reasons.append(FrontierReason.CAPACITY_WAIT)

        project_scopes = reserved_scopes.get(row.project_id, ())
        if any(_scopes_overlap(row.owns, scopes) for scopes in project_scopes):
            reasons.append(FrontierReason.WRITE_SCOPE_CONFLICT)

        if reasons:
            held.append((row, reasons))
            continue

        eligible.append(
            EligibleTask(
                project_id=row.project_id,
                task_id=row.task_id,
                phase=row.phase,
                display_step=row.display_step,
                title=row.title,
                owns=row.owns,
                integration_prerequisites=row.integration_prerequisites,
            )
        )
        accepted_projects.add(row.project_id)
        per_project_count[row.project_id] = per_project_count.get(row.project_id, 0) + 1
        if row.owns:
            reserved_scopes.setdefault(row.project_id, []).append(row.owns)

    held_tasks = tuple(
        HeldTask(
            project_id=row.project_id,
            task_id=row.task_id,
            phase=row.phase,
            display_step=row.display_step,
            status=row.status,
            kind=row.kind,
            reasons=_ordered_reasons(reasons),
        )
        for row, reasons in held
    )
    project_order = tuple(sorted({row.project_id for row in ordered}))
    return tuple(eligible), held_tasks, project_order


def project_ready_frontier(request: FrontierRequest) -> FrontierProjection:
    """Return the deterministic cross-phase ready frontier for one snapshot.

    Precedence is fixed and fail-closed: structural input problems (invalid
    limits, missing/conflicting identity, unknown kind/status, unknown source,
    incomplete snapshot, missing or cyclic dependencies) deny the entire
    projection so no caller mistakes a partial enumeration for a ready set.
    Otherwise every task is reported exactly once, either as eligible or with
    its ordered hold reasons. The function never mutates its inputs and is
    deterministic for identical immutable inputs.
    """
    reasons, index = _validate(request)
    if reasons:
        return _denied(reasons, request.limits)

    eligible, held, project_order = _project(index, request.limits)
    return FrontierProjection(
        status=ProjectionStatus.PROJECTED,
        reasons=(ELIGIBLE_REASON,),
        eligible=eligible,
        held=held,
        project_order=project_order,
        limits=request.limits,
        counts_authoritative=True,
        detail=(
            f"projected: {len(eligible)} eligible, {len(held)} held "
            f"(limits {request.limits.active_projects}x"
            f"{request.limits.child_slots_per_project})"
        ),
    )


class WaveScheduleError(ValueError):
    """Raised when a plan cannot be sequenced into executable waves."""


#: Task statuses the wave scheduler treats as already satisfied.
_SATISFIED_STATUSES: frozenset[str] = frozenset(
    {TaskStatus.COMPLETED.value, TaskStatus.SKIPPED.value}
)

#: Task statuses the wave scheduler treats as schedulable work.
_SCHEDULABLE_STATUSES: frozenset[str] = frozenset(
    {TaskStatus.PENDING.value, TaskStatus.READY.value, TaskStatus.RUNNING.value}
)


@dataclass(frozen=True)
class ScheduledTask:
    """One task placed in a chained wave, with its stable identity."""

    project_id: str
    task_id: str
    phase: str
    title: str
    running: bool = False

    @property
    def stable_ref(self) -> TaskRef:
        """The immutable stable identity of this task."""
        return TaskRef(project_id=self.project_id, task_id=self.task_id)


@dataclass(frozen=True)
class ScheduledWave:
    """One executable wave: tasks that may run once earlier waves completed."""

    index: int
    tasks: tuple[ScheduledTask, ...] = ()


def schedule_chained_waves(
    projects: tuple[ProjectPlan, ...],
    limits: FrontierLimits | None = None,
) -> tuple[ScheduledWave, ...]:
    """Derive the deterministic sequence of executable waves for stored plans.

    Layering is topological over hard START edges: a task enters the first
    wave after every wave holding its START dependencies, so completing wave
    ``n`` unlocks wave ``n+1``. Deferred integration prerequisites never move
    a task. Ordering inside a wave is by stable identity
    ``(project_id, task_id)``; each wave honors the owner maxima (at most
    ``active_projects`` distinct projects, at most ``child_slots_per_project``
    tasks per project) by splitting an oversized layer into consecutive
    waves. Already ``RUNNING`` tasks all sit in wave 0: they were admitted by
    the live slot authority, so wave 0 reports underway work rather than
    granting new admission.

    Fail-closed: unknown statuses/kinds, missing dependency targets, START
    cycles, conflicting duplicate rows, and any task stuck in
    ``blocked`` / ``failed`` / ``owner_wait`` / ``resource_wait`` (which no
    wave can execute) raise :class:`WaveScheduleError` naming the blocker.
    The recovery path is a new plan revision. This function performs no I/O,
    holds no lease and launches nothing.
    """
    bound = limits if limits is not None else default_frontier_limits()
    if not _is_positive_int(bound.active_projects) or not _is_positive_int(
        bound.child_slots_per_project
    ):
        raise WaveScheduleError("invalid wave limits: both maxima must be >= 1")

    index: dict[TaskRef, PlanTask] = {}
    for project in projects:
        project_id = _clean_text(project.project_id)
        if not project_id:
            raise WaveScheduleError("wave input holds a project with no identity")
        for task in project.tasks:
            task_id = _clean_text(task.task_id)
            if not task_id:
                raise WaveScheduleError("wave input holds a task with no identity")
            kind = _normalized(task.kind)
            status = _normalized(task.status)
            if kind != TaskKind.CHILD_TASK.value:
                if kind not in _KIND_VALUES:
                    raise WaveScheduleError(
                        f"unknown task kind {task.kind!r} for {project_id}/{task_id}"
                    )
                continue
            if status not in _STATUS_VALUES:
                raise WaveScheduleError(
                    f"unknown task status {task.status!r} for {project_id}/{task_id}"
                )
            ref = TaskRef(project_id=project_id, task_id=task_id)
            row = PlanTask(
                task_id=task_id,
                phase=_clean_text(task.phase),
                status=status,
                kind=kind,
                title=_clean_text(task.title),
                start_dependencies=tuple(task.start_dependencies),
                integration_prerequisites=tuple(task.integration_prerequisites),
                owns=task.owns,
            )
            existing = index.get(ref)
            if existing is not None and existing != row:
                raise WaveScheduleError(
                    f"conflicting duplicate wave row for {project_id}/{task_id}"
                )
            index[ref] = row

    for ref, row in sorted(index.items(), key=lambda item: (item[0].project_id, item[0].task_id)):
        for dep in row.start_dependencies:
            if not dep.is_valid or dep not in index:
                raise WaveScheduleError(
                    f"task {ref.project_id}/{ref.task_id} depends on unknown "
                    f"task {dep.project_id}/{dep.task_id}"
                )
    if _has_cycle(
        {
            ref: _ValidatedTask(
                project_id=ref.project_id,
                task_id=row.task_id,
                phase=row.phase,
                kind=row.kind,
                status=row.status,
                display_step=row.display_step,
                title=row.title,
                start_dependencies=row.start_dependencies,
                integration_prerequisites=row.integration_prerequisites,
                owns=row.owns,
            )
            for ref, row in index.items()
        },
        edges=lambda row: row.start_dependencies,
    ):
        raise WaveScheduleError("hard START dependencies contain a cycle")

    schedulable: dict[TaskRef, PlanTask] = {}
    for ref, row in index.items():
        if row.status in _SATISFIED_STATUSES:
            continue
        if row.status not in _SCHEDULABLE_STATUSES:
            raise WaveScheduleError(
                f"task {ref.project_id}/{ref.task_id} is {row.status}: no wave "
                "can execute it, resolve it in a new plan revision"
            )
        schedulable[ref] = row
    for ref, row in sorted(
        schedulable.items(), key=lambda item: (item[0].project_id, item[0].task_id)
    ):
        for dep in row.start_dependencies:
            dep_row = index[dep]
            if dep_row.status not in _SATISFIED_STATUSES and dep not in schedulable:
                raise WaveScheduleError(
                    f"task {ref.project_id}/{ref.task_id} is blocked by "
                    f"{dep.project_id}/{dep.task_id} ({dep_row.status}): no wave "
                    "can execute past it, resolve it in a new plan revision"
                )

    # Topological levels over the schedulable subgraph; satisfied tasks are
    # level -1 (already done) and impose no wave.
    level: dict[TaskRef, int] = {}

    def task_level(ref: TaskRef) -> int:
        if ref in level:
            return level[ref]
        deps = [
            task_level(dep)
            for dep in schedulable[ref].start_dependencies
            if dep in schedulable
        ]
        level[ref] = (max(deps) + 1) if deps else 0
        return level[ref]

    for ref in sorted(schedulable, key=lambda item: (item.project_id, item.task_id)):
        task_level(ref)

    by_level: dict[int, list[TaskRef]] = {}
    for ref, depth in level.items():
        by_level.setdefault(depth, []).append(ref)

    waves: list[ScheduledWave] = []
    running = sorted(
        (ref for ref, row in schedulable.items() if row.status == TaskStatus.RUNNING.value),
        key=lambda item: (item.project_id, item.task_id),
    )
    if running:
        waves.append(
            ScheduledWave(
                index=0,
                tasks=tuple(
                    ScheduledTask(
                        project_id=ref.project_id,
                        task_id=ref.task_id,
                        phase=schedulable[ref].phase,
                        title=schedulable[ref].title,
                        running=True,
                    )
                    for ref in running
                ),
            )
        )
    for depth in sorted(by_level):
        pending = [
            ref
            for ref in sorted(
                by_level[depth], key=lambda item: (item.project_id, item.task_id)
            )
            if schedulable[ref].status != TaskStatus.RUNNING.value
        ]
        cursor = 0
        while cursor < len(pending):
            wave_projects: set[str] = set()
            wave_counts: dict[str, int] = {}
            chunk: list[TaskRef] = []
            while cursor < len(pending):
                ref = pending[cursor]
                project_count = wave_counts.get(ref.project_id, 0)
                new_project = ref.project_id not in wave_projects
                if project_count >= bound.child_slots_per_project or (
                    new_project and len(wave_projects) >= bound.active_projects
                ):
                    break
                wave_projects.add(ref.project_id)
                wave_counts[ref.project_id] = project_count + 1
                chunk.append(ref)
                cursor += 1
            waves.append(
                ScheduledWave(
                    index=len(waves),
                    tasks=tuple(
                        ScheduledTask(
                            project_id=ref.project_id,
                            task_id=ref.task_id,
                            phase=schedulable[ref].phase,
                            title=schedulable[ref].title,
                        )
                        for ref in chunk
                    ),
                )
            )
    return tuple(waves)


@dataclass(frozen=True)
class HeldSummary:
    """One held task with its ordered machine-readable hold reason codes."""

    project_id: str
    task_id: str
    status: str
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlanRunReport:
    """Structured run summary for one plan version and one projection.

    Sections mirror the projection exactly: ``eligible`` tasks that may
    start, ``running`` tasks already underway, and ``held`` tasks with the
    exact dependency reason codes that hold them. A denied projection still
    reports (``status`` ``"DENIED"``) with the projection-level reason codes
    and empty sections, so a report never masquerades a failed validation as
    an empty ready set. ``generated_at`` is caller-supplied (no clock read).
    """

    plan_version: str
    project_ids: tuple[str, ...]
    generated_at: str
    status: str
    eligible: tuple[TaskRef, ...] = ()
    running: tuple[TaskRef, ...] = ()
    held: tuple[HeldSummary, ...] = ()
    wave_count: int = 0
    active_projects_limit: int = DEFAULT_ACTIVE_PROJECT_LIMIT
    child_slots_limit: int = DEFAULT_CHILD_SLOTS_PER_PROJECT_LIMIT
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        """Render as JSON-serializable evidence."""
        return {
            "plan_version": self.plan_version,
            "project_ids": list(self.project_ids),
            "generated_at": self.generated_at,
            "status": self.status,
            "eligible": [
                {"project_id": ref.project_id, "task_id": ref.task_id}
                for ref in self.eligible
            ],
            "running": [
                {"project_id": ref.project_id, "task_id": ref.task_id}
                for ref in self.running
            ],
            "held": [
                {
                    "project_id": item.project_id,
                    "task_id": item.task_id,
                    "status": item.status,
                    "reason_codes": list(item.reason_codes),
                }
                for item in self.held
            ],
            "wave_count": self.wave_count,
            "limits": {
                "active_projects": self.active_projects_limit,
                "child_slots_per_project": self.child_slots_limit,
            },
            "detail": self.detail,
        }


def build_plan_run_report(
    projection: FrontierProjection,
    *,
    plan_version: str,
    generated_at: str,
    wave_count: int = 0,
) -> PlanRunReport:
    """Build a structured run summary from one frontier projection."""
    version = plan_version.strip() or "unknown"
    if not projection.projected:
        return PlanRunReport(
            plan_version=version,
            project_ids=projection.project_order,
            generated_at=generated_at,
            status=projection.status.value,
            wave_count=wave_count,
            active_projects_limit=projection.limits.active_projects,
            child_slots_limit=projection.limits.child_slots_per_project,
            detail=f"denied: {', '.join(projection.reason_codes())}",
        )
    running = tuple(
        task.stable_ref
        for task in projection.held
        if FrontierReason.TASK_RUNNING in task.reasons
    )
    held = tuple(
        HeldSummary(
            project_id=task.project_id,
            task_id=task.task_id,
            status=task.status,
            reason_codes=task.reason_codes(),
        )
        for task in projection.held
        if FrontierReason.TASK_RUNNING not in task.reasons
    )
    return PlanRunReport(
        plan_version=version,
        project_ids=projection.project_order,
        generated_at=generated_at,
        status=projection.status.value,
        eligible=projection.eligible_refs(),
        running=running,
        held=held,
        wave_count=wave_count,
        active_projects_limit=projection.limits.active_projects,
        child_slots_limit=projection.limits.child_slots_per_project,
        detail=(
            f"projected: {len(projection.eligible)} eligible, "
            f"{len(running)} running, {len(held)} held "
            f"(plan {version}, {wave_count} waves)"
        ),
    )
