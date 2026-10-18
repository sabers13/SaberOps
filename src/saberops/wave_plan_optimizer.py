"""Wave-plan scoring and optimization engine (Step 027 / WP-1).

What this module is
-------------------
A pure, deterministic planning aid for the Orchestrator's plan-initiation
step. Given a proposed task graph (dependencies, file scopes, S/M/L size and
checklist counts) plus proposed wave groupings, it *scores* the plan and
*optimizes* it by bounded, deterministic revision rounds; it never launches,
admits, merges or mutates anything.

Scoring (see ``docs/wave-plan-optimization.md``):

* **critical path** -- longest dependency chain by task weight;
* **makespan** -- completion time under the per-project worker cap, the
  heavy-gate-slot cap and the capacity cost model's provider demand;
* **parallelism** -- ``total work / critical path``;
* **scope overlap** -- same-wave tasks whose declared write scopes overlap;
* **resource demand** -- peak workers, peak heavy-gate slots and peak/total
  provider quota percent;
* **fixed cost per task** -- a per-task planning overhead.

Optimization rounds apply deterministic moves (dependency-driven reflow,
redundant transitive dependency removal, concurrency balancing and safe wave
merging) and stop below the configured gain threshold or after the configured
round cap.

Execution invariants encoded here:

* **computed wave numbers** are derived from the dependency graph, never
  hand-written labels;
* **dependency-driven start** -- inside an active wave a task becomes
  dispatch-ready as soon as its own START dependencies complete;
* **barrier tasks** -- a barrier task holds every later-wave task until it is
  fully integrated;
* **wave-boundary re-planning** -- unstarted waves can be re-planned once at a
  wave boundary.

Integration is read-only: :func:`graph_from_projects` and
:func:`graph_from_revisions` adapt
:class:`~saberops.ready_frontier.ProjectPlan` and
:class:`~saberops.project_scope.plan_store.PlanRevision` inputs, and
:func:`to_scheduled_waves` renders computed waves back into
:class:`~saberops.ready_frontier.ScheduledWave` objects. Neither authority is
modified.

Zero model, provider or network calls; the module never reads a clock.
"""

from __future__ import annotations

import heapq
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from saberops.capacity import cost_model
from saberops.ready_frontier import (
    DEFAULT_ACTIVE_PROJECT_LIMIT,
    DEFAULT_CHILD_SLOTS_PER_PROJECT_LIMIT,
    PlanTask,
    ProjectPlan,
    ScheduledTask,
    ScheduledWave,
    TaskKind,
    TaskRef,
)

if TYPE_CHECKING:
    from saberops.project_scope.plan_store import PlanRevision

__all__ = [
    "DEFAULT_BARRIER_INTEGRATION_UNITS",
    "DEFAULT_FIXED_COST_PER_TASK",
    "DEFAULT_HEAVY_GATE_SLOTS",
    "DEFAULT_MAX_ROUNDS",
    "DEFAULT_MIN_GAIN",
    "DependencyReduction",
    "NodeMetadata",
    "OptimizationLimits",
    "OptimizationResult",
    "OptimizedWave",
    "PlanNode",
    "PlanScore",
    "ResourceDemand",
    "RoundReport",
    "ScopeOverlap",
    "StopReason",
    "TaskGraph",
    "TaskSize",
    "TaskWindow",
    "WavePlanError",
    "canonicalize_graph",
    "compute_critical_path",
    "default_optimization_limits",
    "derive_waves",
    "detect_scope_overlaps",
    "dispatch_order",
    "from_scheduled_waves",
    "graph_from_projects",
    "graph_from_revisions",
    "optimize_plan",
    "remove_redundant_dependencies",
    "score_plan",
    "to_scheduled_waves",
    "validate_graph",
    "wave_boundary_replan",
    "waves_to_mapping",
]

#: Default planning overhead charged once per task (fixed cost per task).
DEFAULT_FIXED_COST_PER_TASK = 1.0

#: Default extra units a barrier task takes to integrate before later work.
DEFAULT_BARRIER_INTEGRATION_UNITS = 1.0

#: Default number of heavy-gate slots per project (Step 013 CFG-1 default).
DEFAULT_HEAVY_GATE_SLOTS = 2

#: Default cap on optimization rounds.
DEFAULT_MAX_ROUNDS = 3

#: Default fractional makespan gain below which optimization stops.
DEFAULT_MIN_GAIN = 0.10

_EPS = 1e-9
_SIZE_VALUES = frozenset(("S", "M", "L"))


class WavePlanError(ValueError):
    """Raised when a task graph is malformed or cannot be scheduled."""


class TaskSize(StrEnum):
    """Relative effort bucket of one task (S/M/L)."""

    S = "S"
    M = "M"
    L = "L"


class StopReason(StrEnum):
    """Why the iterative revision loop stopped."""

    MIN_GAIN = "min_gain"
    MAX_ROUNDS = "max_rounds"
    NO_TASKS = "no_tasks"


def _clean_text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _coerce_size(value: object) -> TaskSize:
    text = _clean_text(value).upper()
    if text not in _SIZE_VALUES:
        raise WavePlanError(f"unknown task size {value!r}: use S, M or L")
    return TaskSize(text)


def _require_positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise WavePlanError(f"{field} must be an integer >= 1, got {value!r}")
    return value


def _base_scope(scope: str) -> str:
    return scope[:-3] if scope.endswith("/**") else scope


def _path_owned(path: str, scope: str) -> bool:
    return path == scope or (scope.endswith("/**") and path.startswith(scope[:-2]))


def _canonical_scope(raw: object) -> str | None:
    """Return the canonical repo-relative form of one declared write scope.

    Benign variants (``a/b``, ``a/b/``, ``a//b``, ``./a/b``) fold to one form;
    unsafe forms (absolute, traversal, backslash, NUL, glob metacharacters
    other than a trailing ``/**``) return ``None`` so the graph fails closed.
    Mirrors :func:`saberops.ready_frontier._normalized_scope` semantics.
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


def _canonical_scopes(scopes: Iterable[str]) -> tuple[str, ...]:
    cleaned: list[str] = []
    for scope in scopes:
        normalized = _canonical_scope(scope)
        if normalized is None:
            if isinstance(scope, str) and not scope.strip():
                continue
            raise WavePlanError(f"unsafe or ambiguous write scope {scope!r}")
        if normalized not in cleaned:
            cleaned.append(normalized)
    return tuple(cleaned)


def _scopes_overlap(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    """True when two declared write scopes overlap (exact or subtree)."""
    return any(
        _path_owned(_base_scope(a), b) or _path_owned(_base_scope(b), a)
        for a in left
        for b in right
    )


@dataclass(frozen=True)
class PlanNode:
    """One proposed planning task.

    ``size`` uses the S/M/L buckets the capacity cost model prices;
    ``start_dependencies`` are hard START edges that must exist in the graph
    and ``integration_prerequisites`` are deferred edges that never block a
    start. ``barrier`` marks a task that holds every later-wave task until it
    is fully integrated. ``duration`` and ``integration_duration`` override the
    cost-model/heuristic defaults (abstract planning units, not wall-clock).
    """

    project_id: str
    task_id: str
    size: TaskSize | str = TaskSize.M
    title: str = ""
    phase: str = ""
    start_dependencies: tuple[TaskRef, ...] = ()
    integration_prerequisites: tuple[TaskRef, ...] = ()
    owns: tuple[str, ...] = ()
    barrier: bool = False
    requires_heavy_gate: bool = False
    checklist_items: int = 0
    duration: float | None = None
    integration_duration: float | None = None

    @property
    def ref(self) -> TaskRef:
        """Stable identity ``(project_id, task_id)`` of this task."""
        return TaskRef(project_id=_clean_text(self.project_id), task_id=_clean_text(self.task_id))


@dataclass(frozen=True)
class NodeMetadata:
    """Optional per-task planning metadata supplied by the Orchestrator.

    The plan store does not persist size/barrier/gate/checklist today, so the
    adapters accept this sidecar keyed by :class:`TaskRef`.
    """

    size: TaskSize | str = TaskSize.M
    barrier: bool = False
    requires_heavy_gate: bool = False
    checklist_items: int = 0
    duration: float | None = None
    integration_duration: float | None = None


@dataclass(frozen=True)
class TaskGraph:
    """An immutable proposed task graph."""

    nodes: tuple[PlanNode, ...] = ()

    def refs(self) -> tuple[TaskRef, ...]:
        """Every task identity in deterministic order."""
        return tuple(
            sorted((node.ref for node in self.nodes), key=lambda ref: (ref.project_id, ref.task_id))
        )


@dataclass(frozen=True)
class OptimizationLimits:
    """Owner maxima and tuning knobs for scoring and optimization."""

    active_projects: int = DEFAULT_ACTIVE_PROJECT_LIMIT
    workers_per_project: int = DEFAULT_CHILD_SLOTS_PER_PROJECT_LIMIT
    heavy_gate_slots: int = DEFAULT_HEAVY_GATE_SLOTS
    window_id: str = "5h"
    units_per_cost_pct: float = 1.0
    fixed_cost_per_task: float = DEFAULT_FIXED_COST_PER_TASK
    barrier_integration_units: float = DEFAULT_BARRIER_INTEGRATION_UNITS
    max_rounds: int = DEFAULT_MAX_ROUNDS
    min_gain: float = DEFAULT_MIN_GAIN
    enforce_gate_slots: bool = True
    serialize_scope_conflicts: bool = True


def default_optimization_limits() -> OptimizationLimits:
    """Return the owner-approved defaults (3 projects x 5 workers, 2 gates)."""
    return OptimizationLimits()


@dataclass(frozen=True)
class TaskWindow:
    """One scheduled task's time window and resource footprint."""

    ref: TaskRef
    wave: int
    start: float
    finish: float
    duration: float
    integration_duration: float
    provider_pct: float
    barrier: bool
    requires_heavy_gate: bool


@dataclass(frozen=True)
class ResourceDemand:
    """Peak and cumulative resource demands of a scored plan."""

    peak_workers: int
    peak_workers_per_project: int
    peak_heavy_gate_slots: int
    peak_provider_pct: float
    total_provider_pct: float


@dataclass(frozen=True)
class ScopeOverlap:
    """One same-wave pair of tasks whose declared write scopes overlap."""

    left: TaskRef
    right: TaskRef
    left_scopes: tuple[str, ...]
    right_scopes: tuple[str, ...]


@dataclass(frozen=True)
class OptimizedWave:
    """One computed wave: an integer index plus its tasks."""

    index: int
    tasks: tuple[TaskRef, ...] = ()


@dataclass(frozen=True)
class PlanScore:
    """The full score of one plan proposal."""

    critical_path: float
    total_work: float
    makespan: float
    parallelism: float
    scope_overlaps: tuple[ScopeOverlap, ...]
    resource_demand: ResourceDemand
    fixed_cost_total: float
    total_checklist_items: int
    waves: tuple[OptimizedWave, ...]
    schedule: tuple[TaskWindow, ...]

    @property
    def wave_count(self) -> int:
        """Number of computed waves."""
        return len(self.waves)

    @property
    def scope_overlap_count(self) -> int:
        """Number of overlapping same-wave task pairs."""
        return len(self.scope_overlaps)


@dataclass(frozen=True)
class RoundReport:
    """One optimization round's outcome and improvement over the previous."""

    round_index: int
    makespan: float
    critical_path: float
    scope_overlap_count: int
    wave_count: int
    gain: float
    removed_dependencies: int


@dataclass(frozen=True)
class DependencyReduction:
    """Result of removing transitively redundant START dependencies."""

    graph: TaskGraph
    removed: tuple[tuple[TaskRef, TaskRef], ...] = ()


@dataclass(frozen=True)
class OptimizationResult:
    """The scored, optimized plan plus its revision history.

    ``initial_score`` scores the proposal (executed as chained waves);
    ``final_score`` scores the optimized dependency-driven plan.
    ``computed_waves`` are the optimizer's integer wave numbers and
    ``base_wave`` is the offset used by wave-boundary re-planning.
    """

    graph: TaskGraph
    limits: OptimizationLimits
    initial_score: PlanScore
    final_score: PlanScore
    rounds: tuple[RoundReport, ...]
    rounds_executed: int
    stopped_reason: StopReason
    removed_dependencies: tuple[tuple[TaskRef, TaskRef], ...]
    baseline_waves: tuple[OptimizedWave, ...]
    computed_waves: tuple[OptimizedWave, ...]
    base_wave: int = 0


# ---------------------------------------------------------------------------
# Graph canonicalization and validation
# ---------------------------------------------------------------------------


def _canonical_refs(
    raw: tuple[TaskRef, ...], project_id: str, task_id: str, field: str, *, self_check: bool
) -> tuple[TaskRef, ...]:
    """Canonicalize, validate and de-duplicate one dependency tuple."""
    seen: list[TaskRef] = []
    for dep in raw:
        ref = TaskRef(_clean_text(dep.project_id), _clean_text(dep.task_id))
        if not ref.is_valid:
            raise WavePlanError(f"task {project_id}/{task_id}: malformed {field} {dep!r}")
        if self_check and ref == TaskRef(project_id, task_id):
            raise WavePlanError(f"task {project_id}/{task_id} depends on itself")
        if ref not in seen:
            seen.append(ref)
    return tuple(sorted(seen, key=lambda item: (item.project_id, item.task_id)))


def _canonical_node(node: PlanNode) -> PlanNode:
    project_id = _clean_text(node.project_id)
    task_id = _clean_text(node.task_id)
    if not project_id or not task_id:
        raise WavePlanError("every plan node needs a non-empty project_id and task_id")
    start = _canonical_refs(
        node.start_dependencies, project_id, task_id, "START dependency", self_check=True
    )
    integrate = _canonical_refs(
        node.integration_prerequisites,
        project_id,
        task_id,
        "integration prerequisite",
        self_check=False,
    )
    if node.checklist_items < 0:
        raise WavePlanError(f"task {project_id}/{task_id}: checklist_items must be >= 0")
    if node.duration is not None and node.duration < 0:
        raise WavePlanError(f"task {project_id}/{task_id}: duration must be >= 0")
    if node.integration_duration is not None and node.integration_duration < 0:
        raise WavePlanError(f"task {project_id}/{task_id}: integration_duration must be >= 0")
    return replace(
        node,
        project_id=project_id,
        task_id=task_id,
        size=_coerce_size(node.size),
        title=_clean_text(node.title),
        phase=_clean_text(node.phase),
        start_dependencies=start,
        integration_prerequisites=integrate,
        owns=_canonical_scopes(node.owns),
    )


def _canon_index(graph: TaskGraph, limits: OptimizationLimits) -> dict[TaskRef, PlanNode]:
    """Validate a graph fail-closed and return canonical nodes keyed by ref."""
    _require_positive_int(limits.active_projects, "active_projects")
    _require_positive_int(limits.workers_per_project, "workers_per_project")
    _require_positive_int(limits.heavy_gate_slots, "heavy_gate_slots")
    _require_positive_int(limits.max_rounds, "max_rounds")
    if limits.min_gain < 0:
        raise WavePlanError("min_gain must be >= 0")
    if limits.units_per_cost_pct <= 0:
        raise WavePlanError("units_per_cost_pct must be > 0")
    if limits.fixed_cost_per_task < 0:
        raise WavePlanError("fixed_cost_per_task must be >= 0")
    if limits.barrier_integration_units < 0:
        raise WavePlanError("barrier_integration_units must be >= 0")
    index: dict[TaskRef, PlanNode] = {}
    for node in graph.nodes:
        canonical = _canonical_node(node)
        ref = canonical.ref
        if ref in index:
            raise WavePlanError(f"duplicate task {ref.project_id}/{ref.task_id}")
        index[ref] = canonical
    for ref, node in index.items():
        for dep in node.start_dependencies:
            if dep not in index:
                raise WavePlanError(
                    f"task {ref.project_id}/{ref.task_id} depends on unknown task "
                    f"{dep.project_id}/{dep.task_id}"
                )
    _topological_order(index)
    return index


def _graph_from_index(index: Mapping[TaskRef, PlanNode]) -> TaskGraph:
    return TaskGraph(
        nodes=tuple(sorted(index.values(), key=lambda node: (node.project_id, node.task_id)))
    )


def _topological_order(index: Mapping[TaskRef, PlanNode]) -> tuple[TaskRef, ...]:
    """Deterministic Kahn topological order over hard START edges."""
    indegree = dict.fromkeys(index, 0)
    dependents: dict[TaskRef, list[TaskRef]] = {ref: [] for ref in index}
    for ref, node in index.items():
        for dep in node.start_dependencies:
            dependents[dep].append(ref)
            indegree[ref] += 1
    heap = [(ref.project_id, ref.task_id) for ref, degree in indegree.items() if degree == 0]
    heapq.heapify(heap)
    ordered: list[TaskRef] = []
    while heap:
        project_id, task_id = heapq.heappop(heap)
        ref = TaskRef(project_id=project_id, task_id=task_id)
        ordered.append(ref)
        for dependent in dependents[ref]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                heapq.heappush(heap, (dependent.project_id, dependent.task_id))
    if len(ordered) != len(index):
        raise WavePlanError("hard START dependencies contain a cycle")
    return tuple(ordered)


def validate_graph(graph: TaskGraph, limits: OptimizationLimits | None = None) -> None:
    """Validate a task graph fail-closed; raise :class:`WavePlanError` otherwise."""
    _canon_index(graph, limits if limits is not None else default_optimization_limits())


def canonicalize_graph(graph: TaskGraph, limits: OptimizationLimits | None = None) -> TaskGraph:
    """Return the canonical, validated form of a task graph."""
    return _graph_from_index(
        _canon_index(graph, limits if limits is not None else default_optimization_limits())
    )


# ---------------------------------------------------------------------------
# Scoring primitives
# ---------------------------------------------------------------------------


def _duration(node: PlanNode, limits: OptimizationLimits) -> float:
    if node.duration is not None:
        return float(node.duration)
    return float(cost_model.prior_pct(limits.window_id, _coerce_size(node.size).value)) * float(
        limits.units_per_cost_pct
    )


def _integration_duration(node: PlanNode, limits: OptimizationLimits) -> float:
    if node.integration_duration is not None:
        return float(node.integration_duration)
    return float(limits.barrier_integration_units) if node.barrier else 0.0


def _provider_pct(node: PlanNode, limits: OptimizationLimits) -> float:
    return float(cost_model.prior_pct(limits.window_id, _coerce_size(node.size).value))


def compute_critical_path(graph: TaskGraph, limits: OptimizationLimits | None = None) -> float:
    """Longest dependency chain duration (weight includes barrier integration)."""
    bound = limits if limits is not None else default_optimization_limits()
    index = _canon_index(graph, bound)
    return _critical_path(index, bound)


def _critical_path(index: Mapping[TaskRef, PlanNode], limits: OptimizationLimits) -> float:
    chain: dict[TaskRef, float] = {}
    for ref in _topological_order(index):
        node = index[ref]
        weight = _duration(node, limits) + _integration_duration(node, limits)
        best_dep = max((chain[dep] for dep in node.start_dependencies), default=0.0)
        chain[ref] = weight + best_dep
    return max(chain.values(), default=0.0)


def remove_redundant_dependencies(
    graph: TaskGraph, limits: OptimizationLimits | None = None
) -> DependencyReduction:
    """Drop START edges already implied transitively by another direct edge.

    When ``t`` depends on both ``d`` and ``d2`` and ``d2`` transitively depends
    on ``d``, the direct ``t -> d`` edge is redundant. Removing it never changes
    reachability, dependency-driven start times or the critical path.
    """
    bound = limits if limits is not None else default_optimization_limits()
    index = _canon_index(graph, bound)
    ancestors: dict[TaskRef, frozenset[TaskRef]] = {}
    for ref in _topological_order(index):
        collected: set[TaskRef] = set()
        for dep in index[ref].start_dependencies:
            collected.add(dep)
            collected |= ancestors[dep]
        ancestors[ref] = frozenset(collected)
    removed: list[tuple[TaskRef, TaskRef]] = []
    updated: dict[TaskRef, PlanNode] = {}
    for ref, node in index.items():
        kept: list[TaskRef] = []
        for dep in node.start_dependencies:
            redundant = any(
                other != dep and dep in ancestors[other] for other in node.start_dependencies
            )
            if redundant:
                removed.append((ref, dep))
            else:
                kept.append(dep)
        updated[ref] = replace(node, start_dependencies=tuple(kept))
    return DependencyReduction(
        graph=_graph_from_index(updated),
        removed=tuple(
            sorted(
                removed,
                key=lambda pair: (
                    pair[0].project_id,
                    pair[0].task_id,
                    pair[1].project_id,
                    pair[1].task_id,
                ),
            )
        ),
    )


def _scopes_conflict(left: PlanNode, right: PlanNode) -> bool:
    return _scopes_overlap(left.owns, right.owns)


# ---------------------------------------------------------------------------
# Wave derivation
# ---------------------------------------------------------------------------


def _derive_waves_from_index(
    index: Mapping[TaskRef, PlanNode], limits: OptimizationLimits
) -> tuple[OptimizedWave, ...]:
    """Greedy, deterministic, conflict-, capacity- and barrier-aware packing.

    A barrier task fences the plan: every task that is not one of its
    transitive dependencies is placed in a strictly later wave, so a barrier
    holds all subsequent tasks until it is fully integrated.
    """
    return _compact_waves(_fence_waves(index, limits))


def _pack_waves(
    index: Mapping[TaskRef, PlanNode],
    limits: OptimizationLimits,
    floor: Mapping[TaskRef, int],
) -> dict[TaskRef, int]:
    """Greedy pack honoring dependency, capacity, conflict and floor minima."""
    wave: dict[TaskRef, int] = {}
    members: dict[int, list[TaskRef]] = {}
    for ref in _topological_order(index):
        node = index[ref]
        candidate = max(
            max((wave[dep] for dep in node.start_dependencies), default=-1) + 1,
            floor.get(ref, 0),
        )
        while True:
            occupants = members.get(candidate, [])
            conflict = any(_scopes_conflict(node, index[other]) for other in occupants)
            project_count = sum(1 for other in occupants if other.project_id == ref.project_id)
            projects = {other.project_id for other in occupants}
            too_many_projects = (
                ref.project_id not in projects and len(projects) >= limits.active_projects
            )
            if conflict or project_count >= limits.workers_per_project or too_many_projects:
                candidate += 1
                continue
            break
        wave[ref] = candidate
        members.setdefault(candidate, []).append(ref)
    return wave


def _ancestors(index: Mapping[TaskRef, PlanNode]) -> dict[TaskRef, frozenset[TaskRef]]:
    ancestors: dict[TaskRef, frozenset[TaskRef]] = {}
    for ref in _topological_order(index):
        collected: set[TaskRef] = set()
        for dep in index[ref].start_dependencies:
            collected.add(dep)
            collected |= ancestors[dep]
        ancestors[ref] = frozenset(collected)
    return ancestors


def _fence_waves(
    index: Mapping[TaskRef, PlanNode], limits: OptimizationLimits
) -> dict[TaskRef, int]:
    """Pack waves and enforce the ancestor-based barrier fence at a fixpoint.

    A barrier holds every task that is not a transitive dependency (ancestor) of
    it and is not the barrier itself; those tasks are placed in a strictly later
    wave and may not start until the barrier is fully integrated. The barrier's
    own dependencies stay at or before it. Iteration is monotonic, so it
    converges well within ``len(index) + 2`` rounds.
    """
    barrier_refs = tuple(ref for ref, node in index.items() if node.barrier)
    if not barrier_refs:
        return _pack_waves(index, limits, {})
    ancestors = _ancestors(index)
    floor: dict[TaskRef, int] = dict.fromkeys(index, 0)
    waves = _pack_waves(index, limits, floor)
    for _ in range(len(index) + 2):
        required: dict[TaskRef, int] = dict.fromkeys(index, 0)
        for barrier in barrier_refs:
            barrier_wave = waves[barrier]
            for ref in index:
                if ref == barrier or ref in ancestors[barrier]:
                    continue
                required[ref] = max(required[ref], barrier_wave + 1)
        changed = False
        for ref in index:
            if required[ref] > floor[ref]:
                floor[ref] = required[ref]
                changed = True
        if not changed:
            break
        waves = _pack_waves(index, limits, floor)
    return waves


def _compact_waves(wave: Mapping[TaskRef, int]) -> tuple[OptimizedWave, ...]:
    used = sorted(set(wave.values()))
    remap = {value: position for position, value in enumerate(used)}
    grouped: dict[int, list[TaskRef]] = {}
    for ref, value in wave.items():
        grouped.setdefault(remap[value], []).append(ref)
    return tuple(
        OptimizedWave(
            index=position,
            tasks=tuple(sorted(grouped[position], key=lambda ref: (ref.project_id, ref.task_id))),
        )
        for position in sorted(grouped)
    )


def _mapping_to_waves(wave: Mapping[TaskRef, int]) -> tuple[OptimizedWave, ...]:
    """Group a wave mapping into waves preserving the given indices."""
    grouped: dict[int, list[TaskRef]] = {}
    for ref, value in wave.items():
        grouped.setdefault(value, []).append(ref)
    return tuple(
        OptimizedWave(
            index=value,
            tasks=tuple(sorted(grouped[value], key=lambda ref: (ref.project_id, ref.task_id))),
        )
        for value in sorted(grouped)
    )


def derive_waves(
    graph: TaskGraph, limits: OptimizationLimits | None = None
) -> tuple[OptimizedWave, ...]:
    """Compute deterministic integer waves from the dependency graph.

    Every task's wave is strictly greater than each START dependency's wave, no
    wave holds two same-project tasks with overlapping scopes, and each wave
    honors the per-project worker cap and the active-project cap.
    """
    bound = limits if limits is not None else default_optimization_limits()
    return _derive_waves_from_index(_canon_index(graph, bound), bound)


def _waves_to_mapping(waves: Sequence[OptimizedWave]) -> dict[TaskRef, int]:
    mapping: dict[TaskRef, int] = {}
    for wave in waves:
        for ref in wave.tasks:
            mapping[ref] = wave.index
    return mapping


def waves_to_mapping(waves: Sequence[OptimizedWave]) -> dict[TaskRef, int]:
    """Return ``{task_ref: wave_index}`` for a computed wave sequence."""
    return _waves_to_mapping(waves)


def from_scheduled_waves(waves: Sequence[ScheduledWave]) -> tuple[OptimizedWave, ...]:
    """Adapt ready-frontier :class:`ScheduledWave` objects into optimizer waves."""
    return tuple(
        OptimizedWave(
            index=int(wave.index),
            tasks=tuple(
                sorted(
                    (task.stable_ref for task in wave.tasks),
                    key=lambda ref: (ref.project_id, ref.task_id),
                )
            ),
        )
        for wave in waves
    )


def _offset_waves(waves: Sequence[OptimizedWave], base_wave: int) -> tuple[OptimizedWave, ...]:
    return tuple(OptimizedWave(index=wave.index + base_wave, tasks=wave.tasks) for wave in waves)


def _coerce_wave_of(
    index: Mapping[TaskRef, PlanNode],
    limits: OptimizationLimits,
    waves: Mapping[TaskRef, int] | Sequence[OptimizedWave] | None,
) -> dict[TaskRef, int]:
    if waves is None:
        return _waves_to_mapping(_derive_waves_from_index(index, limits))
    if isinstance(waves, Mapping):
        proposed: dict[TaskRef, int] = {}
        for ref in index:
            if ref in waves:
                value = waves[ref]
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise WavePlanError(
                        f"task {ref.project_id}/{ref.task_id}: wave must be an int >= 0"
                    )
                proposed[ref] = value
        derived = _waves_to_mapping(_derive_waves_from_index(index, limits))
        for ref, value in derived.items():
            proposed.setdefault(ref, value)
    else:
        proposed = _waves_to_mapping(waves)
        if set(proposed) != set(index):
            raise WavePlanError("proposed waves must cover exactly the graph's tasks")
    for ref, node in index.items():
        for dep in node.start_dependencies:
            if proposed[dep] >= proposed[ref]:
                raise WavePlanError(
                    f"task {ref.project_id}/{ref.task_id} is placed at wave "
                    f"{proposed[ref]} but depends on {dep.project_id}/{dep.task_id} at "
                    f"wave {proposed[dep]}: proposals must respect the dependency graph"
                )
    return proposed


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------


def _schedule(
    index: Mapping[TaskRef, PlanNode],
    wave_of: Mapping[TaskRef, int],
    limits: OptimizationLimits,
    *,
    chained: bool,
) -> dict[TaskRef, tuple[float, float]]:
    """Greedy resource-constrained list schedule.

    ``chained`` models today's chained-wave baseline (every task in wave ``n``
    waits for the whole of wave ``n-1``); otherwise tasks start as soon as
    their own START dependencies complete (dependency-driven start). Barrier
    tasks always hold every strictly-later-wave task until they are integrated,
    and overlapping scopes are serialized when enabled.
    """
    projects = sorted({ref.project_id for ref in index})
    slots: dict[str, list[float]] = {
        project: [0.0] * limits.workers_per_project for project in projects
    }
    gate_slots: list[float] = [0.0] * limits.heavy_gate_slots if limits.enforce_gate_slots else []
    wave_members: dict[int, tuple[TaskRef, ...]] = {}
    for ref, value in wave_of.items():
        wave_members.setdefault(value, ())
        wave_members[value] = wave_members[value] + (ref,)
    previous_wave: dict[int, int | None] = {}
    ordered_waves = sorted(wave_members)
    for position, value in enumerate(ordered_waves):
        previous_wave[value] = ordered_waves[position - 1] if position > 0 else None
    barriers_before: dict[TaskRef, tuple[TaskRef, ...]] = {}
    barrier_refs = tuple(ref for ref, node in index.items() if node.barrier)
    for ref in index:
        barriers_before[ref] = tuple(
            barrier for barrier in barrier_refs if wave_of[barrier] < wave_of[ref]
        )
    assigned: dict[TaskRef, tuple[float, float]] = {}
    remaining = set(index)
    while remaining:
        progressed = False
        ready: list[tuple[float, float, str, str, TaskRef]] = []
        for ref in sorted(remaining, key=lambda item: (item.project_id, item.task_id)):
            node = index[ref]
            if any(dep not in assigned for dep in node.start_dependencies):
                continue
            earliest = max((assigned[dep][1] for dep in node.start_dependencies), default=0.0)
            if chained:
                predecessor = previous_wave.get(wave_of[ref])
                previous = wave_members.get(predecessor, ()) if predecessor is not None else ()
                if any(member not in assigned for member in previous):
                    continue
                earliest = max(
                    earliest, max((assigned[member][1] for member in previous), default=0.0)
                )
            pending_barriers = [b for b in barriers_before[ref] if b not in assigned]
            if pending_barriers:
                continue
            earliest = max(
                earliest,
                max(
                    (
                        assigned[b][1] + _integration_duration(index[b], limits)
                        for b in barriers_before[ref]
                    ),
                    default=0.0,
                ),
            )
            ready.append((earliest, -_duration(node, limits), ref.project_id, ref.task_id, ref))
        ready.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
        for earliest, _, _, _, ref in ready:
            start = _feasible_start(ref, earliest, index, assigned, slots, gate_slots, limits)
            node = index[ref]
            slot = min(range(len(slots[ref.project_id])), key=lambda i: slots[ref.project_id][i])
            finish = start + _duration(node, limits)
            slots[ref.project_id][slot] = finish
            if limits.enforce_gate_slots and node.requires_heavy_gate and gate_slots:
                gate = min(range(len(gate_slots)), key=lambda i: gate_slots[i])
                gate_slots[gate] = finish
            assigned[ref] = (start, finish)
            remaining.discard(ref)
            progressed = True
        if not progressed:
            raise WavePlanError(
                "could not schedule the plan: unresolved dependencies or empty waves"
            )
    return assigned


def _feasible_start(
    ref: TaskRef,
    earliest: float,
    index: Mapping[TaskRef, PlanNode],
    assigned: Mapping[TaskRef, tuple[float, float]],
    slots: dict[str, list[float]],
    gate_slots: list[float],
    limits: OptimizationLimits,
) -> float:
    node = index[ref]
    project_slots = slots[ref.project_id]
    desired = earliest
    for _ in range(len(assigned) + 3):
        candidate = max(desired, min(project_slots))
        if limits.enforce_gate_slots and node.requires_heavy_gate and gate_slots:
            candidate = max(candidate, min(gate_slots))
        if limits.serialize_scope_conflicts:
            duration = _duration(node, limits)
            ceiling = candidate
            for other, (other_start, other_end) in assigned.items():
                if other_end <= candidate + _EPS or other_start >= candidate + duration - _EPS:
                    continue
                if _scopes_conflict(node, index[other]):
                    ceiling = max(ceiling, other_end)
            candidate = ceiling
        if candidate <= desired + _EPS:
            return desired
        desired = candidate
    return desired


def _resource_demand(
    index: Mapping[TaskRef, PlanNode],
    assigned: Mapping[TaskRef, tuple[float, float]],
    limits: OptimizationLimits,
) -> ResourceDemand:
    events: list[tuple[float, float]] = []
    project_events: dict[str, list[tuple[float, float]]] = {}
    gate_events: list[tuple[float, float]] = []
    provider_events: list[tuple[float, float]] = []
    total_pct = 0.0
    for ref, (start, finish) in assigned.items():
        node = index[ref]
        events.extend(((start, 1.0), (finish, -1.0)))
        project_events.setdefault(ref.project_id, []).extend(((start, 1.0), (finish, -1.0)))
        if node.requires_heavy_gate:
            gate_events.extend(((start, 1.0), (finish, -1.0)))
        pct = _provider_pct(node, limits)
        total_pct += pct
        provider_events.extend(((start, pct), (finish, -pct)))
    peak_project = 0
    for bucket in project_events.values():
        peak_project = max(peak_project, int(_sweep_peak(bucket, integral=True)))
    return ResourceDemand(
        peak_workers=int(_sweep_peak(events, integral=True)),
        peak_workers_per_project=peak_project,
        peak_heavy_gate_slots=int(_sweep_peak(gate_events, integral=True)),
        peak_provider_pct=_sweep_peak(provider_events, integral=False),
        total_provider_pct=total_pct,
    )


def _sweep_peak(events: list[tuple[float, float]], *, integral: bool) -> float:
    events.sort(key=lambda event: (event[0], event[1]))
    running = 0.0
    peak = 0.0
    for _, delta in events:
        running += delta
        peak = max(peak, running)
    if integral:
        return float(int(round(peak)))
    return round(peak, 6)


def detect_scope_overlaps(
    graph: TaskGraph,
    waves: Mapping[TaskRef, int] | Sequence[OptimizedWave] | None = None,
    limits: OptimizationLimits | None = None,
) -> tuple[ScopeOverlap, ...]:
    """Return every same-wave task pair with overlapping write scopes."""
    bound = limits if limits is not None else default_optimization_limits()
    index = _canon_index(graph, bound)
    wave_of = _coerce_wave_of(index, bound, waves)
    return _detect_overlaps(index, wave_of)


def _detect_overlaps(
    index: Mapping[TaskRef, PlanNode], wave_of: Mapping[TaskRef, int]
) -> tuple[ScopeOverlap, ...]:
    by_wave: dict[int, list[TaskRef]] = {}
    for ref, value in wave_of.items():
        by_wave.setdefault(value, []).append(ref)
    found: list[ScopeOverlap] = []
    for value in sorted(by_wave):
        refs = sorted(by_wave[value], key=lambda ref: (ref.project_id, ref.task_id))
        for position, left in enumerate(refs):
            for right in refs[position + 1 :]:
                if _scopes_conflict(index[left], index[right]):
                    found.append(
                        ScopeOverlap(
                            left=left,
                            right=right,
                            left_scopes=index[left].owns,
                            right_scopes=index[right].owns,
                        )
                    )
    return tuple(found)


def _score(
    index: Mapping[TaskRef, PlanNode],
    limits: OptimizationLimits,
    wave_of: Mapping[TaskRef, int],
    *,
    chained: bool,
) -> PlanScore:
    assigned = _schedule(index, wave_of, limits, chained=chained)
    schedule = tuple(
        TaskWindow(
            ref=ref,
            wave=wave_of[ref],
            start=assigned[ref][0],
            finish=assigned[ref][1],
            duration=_duration(index[ref], limits),
            integration_duration=_integration_duration(index[ref], limits),
            provider_pct=_provider_pct(index[ref], limits),
            barrier=index[ref].barrier,
            requires_heavy_gate=index[ref].requires_heavy_gate,
        )
        for ref in sorted(assigned, key=lambda ref: (assigned[ref][0], ref.project_id, ref.task_id))
    )
    critical = _critical_path(index, limits)
    total_work = sum(_duration(node, limits) for node in index.values())
    makespan = max((finish for _, finish in assigned.values()), default=0.0)
    by_wave: dict[int, list[TaskRef]] = {}
    for ref, value in wave_of.items():
        by_wave.setdefault(value, []).append(ref)
    waves = tuple(
        OptimizedWave(
            index=value,
            tasks=tuple(sorted(by_wave[value], key=lambda ref: (ref.project_id, ref.task_id))),
        )
        for value in sorted(by_wave)
    )
    return PlanScore(
        critical_path=critical,
        total_work=total_work,
        makespan=makespan,
        parallelism=(total_work / critical) if critical > 0 else 0.0,
        scope_overlaps=_detect_overlaps(index, wave_of),
        resource_demand=_resource_demand(index, assigned, limits),
        fixed_cost_total=float(limits.fixed_cost_per_task) * len(index),
        total_checklist_items=sum(node.checklist_items for node in index.values()),
        waves=waves,
        schedule=schedule,
    )


def score_plan(
    graph: TaskGraph,
    *,
    waves: Mapping[TaskRef, int] | Sequence[OptimizedWave] | None = None,
    limits: OptimizationLimits | None = None,
    chained: bool = False,
) -> PlanScore:
    """Score a proposed plan under the given limits.

    ``chained=True`` models executing the proposal as chained waves (each wave
    waits for the previous one); the default is dependency-driven start.
    """
    bound = limits if limits is not None else default_optimization_limits()
    index = _canon_index(graph, bound)
    wave_of = _coerce_wave_of(index, bound, waves)
    return _score(index, bound, wave_of, chained=chained)


# ---------------------------------------------------------------------------
# Optimization
# ---------------------------------------------------------------------------


def _merge_waves(
    index: Mapping[TaskRef, PlanNode],
    waves: Sequence[OptimizedWave],
    limits: OptimizationLimits,
) -> tuple[OptimizedWave, ...]:
    """Merge adjacent waves when caps, scopes and barriers permit."""
    groups: list[list[TaskRef]] = [list(wave.tasks) for wave in waves]
    merged = True
    while merged:
        merged = False
        position = 0
        while position < len(groups) - 1:
            left, right = groups[position], groups[position + 1]
            if (
                _contains_barrier(left, index)
                or _contains_barrier(right, index)
                or not _can_merge(left, right, index, limits)
            ):
                position += 1
                continue
            groups[position] = left + right
            del groups[position + 1]
            merged = True
    return tuple(
        OptimizedWave(
            index=position,
            tasks=tuple(sorted(group, key=lambda ref: (ref.project_id, ref.task_id))),
        )
        for position, group in enumerate(groups)
    )


def _contains_barrier(group: Sequence[TaskRef], index: Mapping[TaskRef, PlanNode]) -> bool:
    return any(index[ref].barrier for ref in group)


def _can_merge(
    left: Sequence[TaskRef],
    right: Sequence[TaskRef],
    index: Mapping[TaskRef, PlanNode],
    limits: OptimizationLimits,
) -> bool:
    left_set = set(left)
    for ref in right:
        if any(dep in left_set for dep in index[ref].start_dependencies):
            return False
    combined = list(left) + list(right)
    projects: set[str] = set()
    counts: dict[str, int] = {}
    for ref in combined:
        projects.add(ref.project_id)
        counts[ref.project_id] = counts.get(ref.project_id, 0) + 1
        if counts[ref.project_id] > limits.workers_per_project:
            return False
    if len(projects) > limits.active_projects:
        return False
    for position, ref in enumerate(combined):
        for other in combined[position + 1 :]:
            if _scopes_conflict(index[ref], index[other]):
                return False
    return True


def _wave_signature(waves: Sequence[OptimizedWave]) -> tuple[tuple[int, ...], tuple[str, ...]]:
    return (
        tuple(wave.index for wave in waves),
        tuple(f"{ref.project_id}/{ref.task_id}" for wave in waves for ref in wave.tasks),
    )


def _refine(
    index: Mapping[TaskRef, PlanNode],
    limits: OptimizationLimits,
    current: tuple[OptimizedWave, ...],
) -> tuple[OptimizedWave, ...]:
    candidates = [
        current,
        _merge_waves(index, current, limits),
        _derive_waves_from_index(index, limits),
    ]
    best: tuple[OptimizedWave, ...] | None = None
    best_key: tuple[float, int, tuple[tuple[int, ...], tuple[str, ...]]] | None = None
    for candidate in candidates:
        if not candidate:
            continue
        wave_of = _waves_to_mapping(candidate)
        makespan = _score(index, limits, wave_of, chained=False).makespan
        key = (makespan, len(candidate), _wave_signature(candidate))
        if best_key is None or key < best_key:
            best_key = key
            best = candidate
    return best if best is not None else current


def _optimize(
    index: Mapping[TaskRef, PlanNode],
    limits: OptimizationLimits,
    proposed_waves: Mapping[TaskRef, int] | Sequence[OptimizedWave] | None,
    base_wave: int,
) -> OptimizationResult:
    graph = _graph_from_index(index)
    if not index:
        empty = PlanScore(
            critical_path=0.0,
            total_work=0.0,
            makespan=0.0,
            parallelism=0.0,
            scope_overlaps=(),
            resource_demand=ResourceDemand(0, 0, 0, 0.0, 0.0),
            fixed_cost_total=0.0,
            total_checklist_items=0,
            waves=(),
            schedule=(),
        )
        return OptimizationResult(
            graph=graph,
            limits=limits,
            initial_score=empty,
            final_score=empty,
            rounds=(),
            rounds_executed=0,
            stopped_reason=StopReason.NO_TASKS,
            removed_dependencies=(),
            baseline_waves=(),
            computed_waves=(),
            base_wave=base_wave,
        )
    baseline_wave_of = _coerce_wave_of(index, limits, proposed_waves)
    baseline_waves = _offset_waves(_mapping_to_waves(baseline_wave_of), base_wave)
    initial_score = _score(index, limits, baseline_wave_of, chained=True)
    current = _offset_waves(_derive_waves_from_index(index, limits), base_wave)
    previous = initial_score.makespan
    rounds: list[RoundReport] = []
    final_score = initial_score
    stopped = StopReason.MAX_ROUNDS
    for round_index in range(1, limits.max_rounds + 1):
        if round_index > 1:
            current = _refine(index, limits, current)
        wave_of = _waves_to_mapping(current)
        scored = _score(index, limits, wave_of, chained=False)
        gain = (previous - scored.makespan) / previous if previous > 0 else 0.0
        rounds.append(
            RoundReport(
                round_index=round_index,
                makespan=scored.makespan,
                critical_path=scored.critical_path,
                scope_overlap_count=scored.scope_overlap_count,
                wave_count=scored.wave_count,
                gain=gain,
                removed_dependencies=0,
            )
        )
        final_score = scored
        previous = scored.makespan
        if gain < limits.min_gain:
            stopped = StopReason.MIN_GAIN
            break
        if round_index == limits.max_rounds:
            stopped = StopReason.MAX_ROUNDS
    return OptimizationResult(
        graph=graph,
        limits=limits,
        initial_score=initial_score,
        final_score=final_score,
        rounds=tuple(rounds),
        rounds_executed=len(rounds),
        stopped_reason=stopped,
        removed_dependencies=(),
        baseline_waves=baseline_waves,
        computed_waves=current,
        base_wave=base_wave,
    )


def optimize_plan(
    graph: TaskGraph,
    *,
    proposed_waves: Mapping[TaskRef, int] | Sequence[OptimizedWave] | None = None,
    limits: OptimizationLimits | None = None,
) -> OptimizationResult:
    """Score and optimize a proposed plan within the configured round cap.

    The first round removes transitively redundant START dependencies and
    reflows the plan from chained waves to dependency-driven start; later rounds
    merge underfilled waves and re-derive where that improves the makespan. The
    loop stops as soon as the fractional makespan gain falls below
    ``limits.min_gain`` or after ``limits.max_rounds`` rounds.
    """
    bound = limits if limits is not None else default_optimization_limits()
    reduction = remove_redundant_dependencies(graph, bound)
    index = _canon_index(reduction.graph, bound)
    result = _optimize(index, bound, proposed_waves, 0)
    if reduction.removed and result.rounds:
        first = replace(result.rounds[0], removed_dependencies=len(reduction.removed))
        result = replace(result, rounds=(first, *result.rounds[1:]))
    return replace(result, removed_dependencies=reduction.removed)


def wave_boundary_replan(
    graph: TaskGraph,
    result: OptimizationResult,
    *,
    integrated_refs: Iterable[TaskRef],
    boundary_wave: int,
    limits: OptimizationLimits | None = None,
) -> OptimizationResult:
    """Re-plan the still-unstarted waves once at a wave boundary.

    Tasks whose computed wave is ``<= boundary_wave`` are treated as already
    started; ``integrated_refs`` are satisfied dependencies. The returned plan
    numbers its computed waves continuing after the boundary.
    """
    if isinstance(boundary_wave, bool) or not isinstance(boundary_wave, int) or boundary_wave < 0:
        raise WavePlanError("boundary_wave must be an integer >= 0")
    bound = limits if limits is not None else result.limits
    wave_of = _waves_to_mapping(result.computed_waves)
    done = set(integrated_refs)
    index = _canon_index(graph, bound)
    remaining = {ref for ref in index if wave_of.get(ref, 0) > boundary_wave and ref not in done}
    sub_nodes: list[PlanNode] = []
    for ref in sorted(remaining, key=lambda item: (item.project_id, item.task_id)):
        node = index[ref]
        sub_nodes.append(
            replace(
                node,
                start_dependencies=tuple(
                    dep for dep in node.start_dependencies if dep in remaining
                ),
            )
        )
    subgraph = TaskGraph(nodes=tuple(sub_nodes))
    return _optimize(_canon_index(subgraph, bound), bound, None, boundary_wave + 1)


# ---------------------------------------------------------------------------
# Ready-frontier / plan-store integration
# ---------------------------------------------------------------------------


def _metadata_for(ref: TaskRef, metadata: Mapping[TaskRef, NodeMetadata] | None) -> NodeMetadata:
    if metadata is None:
        return NodeMetadata()
    return metadata.get(ref, NodeMetadata())


def _node_from_parts(
    project_id: str,
    task_id: str,
    *,
    meta: NodeMetadata,
    start_dependencies: tuple[TaskRef, ...],
    integration_prerequisites: tuple[TaskRef, ...],
    owns: tuple[str, ...],
    title: str = "",
    phase: str = "",
) -> PlanNode:
    return PlanNode(
        project_id=project_id,
        task_id=task_id,
        size=_coerce_size(meta.size),
        title=title,
        phase=phase,
        start_dependencies=start_dependencies,
        integration_prerequisites=integration_prerequisites,
        owns=owns,
        barrier=meta.barrier,
        requires_heavy_gate=meta.requires_heavy_gate,
        checklist_items=meta.checklist_items,
        duration=meta.duration,
        integration_duration=meta.integration_duration,
    )


def graph_from_projects(
    projects: Sequence[ProjectPlan],
    *,
    metadata: Mapping[TaskRef, NodeMetadata] | None = None,
) -> TaskGraph:
    """Adapt ready-frontier ``ProjectPlan`` rows into a proposed task graph.

    Coordination records (anything that is not a ``child_task``) never become
    planning nodes; an unknown task kind fails closed.
    """
    nodes: list[PlanNode] = []
    for project in projects:
        project_id = _clean_text(project.project_id)
        if not project_id:
            raise WavePlanError("project plan without a project identity")
        for task in project.tasks:
            _append_project_task(nodes, project_id, task, metadata)
    return TaskGraph(nodes=tuple(sorted(nodes, key=lambda node: (node.project_id, node.task_id))))


def _append_project_task(
    nodes: list[PlanNode],
    project_id: str,
    task: PlanTask,
    metadata: Mapping[TaskRef, NodeMetadata] | None,
) -> None:
    kind = _clean_text(task.kind).lower()
    if kind != TaskKind.CHILD_TASK.value:
        known = {member.value for member in TaskKind}
        if kind not in known:
            raise WavePlanError(f"unknown task kind {task.kind!r}")
        return
    task_id = _clean_text(task.task_id)
    if not task_id:
        raise WavePlanError(f"project {project_id}: task without an identity")
    ref = TaskRef(project_id=project_id, task_id=task_id)
    nodes.append(
        _node_from_parts(
            project_id,
            task_id,
            meta=_metadata_for(ref, metadata),
            start_dependencies=task.start_dependencies,
            integration_prerequisites=task.integration_prerequisites,
            owns=task.owns,
            title=task.title,
            phase=task.phase,
        )
    )


def _ref_from_edge(home_project: str, edge: str) -> TaskRef:
    project_part, separator, task_part = edge.partition("/")
    if separator and project_part and task_part and "/" not in task_part:
        return TaskRef(project_id=project_part, task_id=task_part)
    return TaskRef(project_id=home_project, task_id=edge)


def graph_from_revisions(
    revisions: Iterable[PlanRevision],
    *,
    metadata: Mapping[TaskRef, NodeMetadata] | None = None,
) -> TaskGraph:
    """Adapt stored plan revisions into a proposed task graph.

    Same-project dependency entries resolve against the revision's home
    project; ``"other-project/task"`` entries resolve cross-project. Sizes,
    barriers and gate demands arrive through ``metadata`` because the plan store
    does not persist them yet.
    """
    nodes: list[PlanNode] = []
    for revision in revisions:
        project_id = _clean_text(revision.project_id)
        if not project_id:
            raise WavePlanError("plan revision without a project identity")
        for task in revision.tasks:
            task_id = _clean_text(task.task_id)
            if not task_id:
                raise WavePlanError(f"project {project_id}: task without an identity")
            ref = TaskRef(project_id=project_id, task_id=task_id)
            start = tuple(
                sorted(
                    (_ref_from_edge(project_id, edge) for edge in task.start_dependencies),
                    key=lambda item: (item.project_id, item.task_id),
                )
            )
            integrate = tuple(
                sorted(
                    (_ref_from_edge(project_id, edge) for edge in task.integration_prerequisites),
                    key=lambda item: (item.project_id, item.task_id),
                )
            )
            nodes.append(
                _node_from_parts(
                    project_id,
                    task_id,
                    meta=_metadata_for(ref, metadata),
                    start_dependencies=start,
                    integration_prerequisites=integrate,
                    owns=task.owns,
                    title=task.title,
                    phase=task.phase,
                )
            )
    return TaskGraph(nodes=tuple(sorted(nodes, key=lambda node: (node.project_id, node.task_id))))


def to_scheduled_waves(graph: TaskGraph, result: OptimizationResult) -> tuple[ScheduledWave, ...]:
    """Render computed waves as ready-frontier ``ScheduledWave`` objects."""
    index = _canon_index(graph, result.limits)
    scheduled: list[ScheduledWave] = []
    for wave in result.computed_waves:
        tasks = tuple(
            ScheduledTask(
                project_id=ref.project_id,
                task_id=ref.task_id,
                phase=index[ref].phase,
                title=index[ref].title,
                running=False,
            )
            for ref in wave.tasks
            if ref in index
        )
        scheduled.append(ScheduledWave(index=wave.index, tasks=tasks))
    return tuple(scheduled)


def dispatch_order(graph: TaskGraph, result: OptimizationResult) -> tuple[TaskRef, ...]:
    """Dependency-driven dispatch order: earliest start, then stable identity."""
    return tuple(window.ref for window in result.final_score.schedule)
