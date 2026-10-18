"""Adversarial hermetic tests for the wave-plan scoring and optimization engine.

Every test is pure and deterministic: no database, repository, network, clock,
provider or model call. Each test states the invariant it covers with a
``Covers:`` line (README rule 9).
"""

from __future__ import annotations

import pytest

from saberops.capacity import cost_model
from saberops.project_scope.plan_store import (
    PlanRevision,
    PlanTaskDef,
    compute_revision_hash,
)
from saberops.ready_frontier import (
    PlanTask,
    ProjectPlan,
    ScheduledTask,
    ScheduledWave,
    TaskKind,
    TaskRef,
    TaskStatus,
)
from saberops.wave_plan_optimizer import (
    DEFAULT_HEAVY_GATE_SLOTS,
    NodeMetadata,
    OptimizationLimits,
    OptimizedWave,
    PlanNode,
    PlanScore,
    StopReason,
    TaskGraph,
    TaskSize,
    WavePlanError,
    canonicalize_graph,
    compute_critical_path,
    default_optimization_limits,
    derive_waves,
    detect_scope_overlaps,
    dispatch_order,
    from_scheduled_waves,
    graph_from_projects,
    graph_from_revisions,
    optimize_plan,
    remove_redundant_dependencies,
    score_plan,
    to_scheduled_waves,
    validate_graph,
    wave_boundary_replan,
    waves_to_mapping,
)


def _ref(project: str, task: str) -> TaskRef:
    return TaskRef(project_id=project, task_id=task)


def _node(
    project: str,
    task: str,
    *,
    size: str = "M",
    deps: tuple[TaskRef, ...] = (),
    integrate: tuple[TaskRef, ...] = (),
    owns: tuple[str, ...] = (),
    barrier: bool = False,
    gate: bool = False,
    checklist: int = 0,
    duration: float | None = None,
    integr: float | None = None,
) -> PlanNode:
    return PlanNode(
        project_id=project,
        task_id=task,
        size=size,
        start_dependencies=deps,
        integration_prerequisites=integrate,
        owns=owns,
        barrier=barrier,
        requires_heavy_gate=gate,
        checklist_items=checklist,
        duration=duration,
        integration_duration=integr,
    )


def _graph(*nodes: PlanNode) -> TaskGraph:
    return TaskGraph(nodes=tuple(nodes))


def _sample_graph() -> TaskGraph:
    """A small two-project roadmap with a real dependency chain."""
    return _graph(
        _node("p1", "a", size="L"),
        _node("p1", "b", size="M", deps=(_ref("p1", "a"),)),
        _node("p1", "c", size="S", deps=(_ref("p1", "a"),)),
        _node("p1", "d", size="M", deps=(_ref("p1", "b"), _ref("p1", "c"))),
        _node("p2", "e", size="M"),
        _node("p2", "f", size="S", deps=(_ref("p2", "e"),)),
    )


def _schedule_by_ref(score: PlanScore) -> dict[TaskRef, tuple[float, float]]:
    return {window.ref: (window.start, window.finish) for window in score.schedule}


# ---------------------------------------------------------------------------
# Duration model and critical path
# ---------------------------------------------------------------------------


def test_duration_derives_from_capacity_cost_model() -> None:
    """Covers: WP-1 scoring formulas -- per-task work from the cost model."""
    graph = _graph(
        _node("p", "s", size="S"),
        _node("p", "m", size="M"),
        _node("p", "l", size="L"),
    )
    score = score_plan(graph)
    by_ref = _schedule_by_ref(score)
    assert by_ref[_ref("p", "s")][1] == cost_model.prior_pct("5h", "S")
    assert by_ref[_ref("p", "m")][1] == cost_model.prior_pct("5h", "M")
    assert by_ref[_ref("p", "l")][1] == cost_model.prior_pct("5h", "L")


def test_duration_override_and_window_scale() -> None:
    """Covers: WP-1 scoring formulas -- explicit duration and window override."""
    graph = _graph(_node("p", "m", size="M"))
    explicit = score_plan(graph, limits=OptimizationLimits(units_per_cost_pct=2.0))
    assert explicit.schedule[0].duration == pytest.approx(2 * cost_model.prior_pct("5h", "M"))
    pinned = score_plan(graph, limits=OptimizationLimits(min_gain=0.1))
    assert pinned.schedule[0].duration == pytest.approx(cost_model.prior_pct("5h", "M"))
    overridden = _graph(_node("p", "m", duration=1.5))
    assert score_plan(overridden).schedule[0].duration == pytest.approx(1.5)


def test_critical_path_is_longest_dependency_chain() -> None:
    """Covers: WP-1 critical path -- longest dependency chain duration."""
    graph = _sample_graph()
    # a(L=15) -> b(M=8) -> d(M=8) is the longest chain.
    assert compute_critical_path(graph) == pytest.approx(31.0)


def test_parallelism_is_total_work_over_critical_path() -> None:
    """Covers: WP-1 scoring formulas -- parallelism = total work / critical path."""
    score = score_plan(_sample_graph())
    assert score.parallelism == pytest.approx(score.total_work / score.critical_path)
    assert score.total_work == pytest.approx(45.0)
    assert score.critical_path == pytest.approx(31.0)
    assert score.parallelism > 1.0


def test_single_node_has_unit_parallelism() -> None:
    """Covers: WP-1 scoring formulas -- degenerate parallelism bound."""
    score = score_plan(_graph(_node("p", "only", size="S")))
    assert score.parallelism == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Makespan and resource bounds
# ---------------------------------------------------------------------------


def test_makespan_respects_per_project_worker_cap() -> None:
    """Covers: WP-1 makespan -- per-project worker cap serializes work."""
    graph = _graph(
        _node("p", "a"),
        _node("p", "b"),
        _node("p", "c"),
    )
    serial = score_plan(graph, limits=OptimizationLimits(workers_per_project=1))
    assert serial.makespan == pytest.approx(24.0)
    pair = score_plan(graph, limits=OptimizationLimits(workers_per_project=2))
    assert pair.makespan == pytest.approx(16.0)
    parallel = score_plan(graph, limits=OptimizationLimits(workers_per_project=3))
    assert parallel.makespan == pytest.approx(8.0)


def test_makespan_respects_heavy_gate_slots() -> None:
    """Covers: WP-1 makespan -- heavy-gate slot demand queues gate work."""
    graph = _graph(
        _node("p", "a", gate=True),
        _node("p", "b", gate=True),
        _node("p", "c", gate=True),
    )
    queued = score_plan(graph, limits=OptimizationLimits(heavy_gate_slots=1))
    assert queued.makespan == pytest.approx(24.0)
    assert queued.resource_demand.peak_heavy_gate_slots == 1
    open_gates = score_plan(graph, limits=OptimizationLimits(heavy_gate_slots=3))
    assert open_gates.makespan == pytest.approx(8.0)
    assert open_gates.resource_demand.peak_heavy_gate_slots == 3


def test_gate_slots_ignored_when_not_enforced() -> None:
    """Covers: WP-1 resource demands -- gate enforcement is opt-out only."""
    graph = _graph(_node("p", "a", gate=True), _node("p", "b", gate=True))
    free = score_plan(
        graph,
        limits=OptimizationLimits(heavy_gate_slots=1, enforce_gate_slots=False),
    )
    assert free.makespan == pytest.approx(8.0)


def test_resource_demand_reports_peaks_and_totals() -> None:
    """Covers: WP-1 resource demands -- peak worker/gate/quota reporting."""
    graph = _graph(
        _node("p1", "a", gate=True),
        _node("p2", "b", gate=True),
    )
    score = score_plan(graph, limits=OptimizationLimits(heavy_gate_slots=2))
    demand = score.resource_demand
    assert demand.peak_workers == 2
    assert demand.peak_workers_per_project == 1
    assert demand.peak_heavy_gate_slots == 2
    assert demand.peak_provider_pct == pytest.approx(2 * cost_model.prior_pct("5h", "M"))
    assert demand.total_provider_pct == pytest.approx(2 * cost_model.prior_pct("5h", "M"))
    assert DEFAULT_HEAVY_GATE_SLOTS == 2


def test_fixed_cost_and_checklist_totals() -> None:
    """Covers: WP-1 scoring formulas -- fixed cost per task and checklists."""
    graph = _graph(
        _node("p", "a", checklist=3),
        _node("p", "b", checklist=4),
    )
    score = score_plan(graph, limits=OptimizationLimits(fixed_cost_per_task=2.5))
    assert score.fixed_cost_total == pytest.approx(5.0)
    assert score.total_checklist_items == 7


def test_makespan_bounds_critical_path_and_total_work() -> None:
    """Covers: WP-1 makespan -- critical path <= makespan <= total work."""
    score = score_plan(_sample_graph())
    assert score.critical_path - 1e-9 <= score.makespan <= score.total_work + 1e-9


# ---------------------------------------------------------------------------
# Dependency-driven start, chained baseline, waves
# ---------------------------------------------------------------------------


def test_chained_waves_wait_for_the_whole_previous_wave() -> None:
    """Covers: WP-1 makespan -- chained baseline fences independent tasks."""
    graph = _graph(_node("p", "a"), _node("p", "b"))
    waves = {_ref("p", "a"): 0, _ref("p", "b"): 1}
    chained = score_plan(graph, waves=waves, chained=True)
    driven = score_plan(graph, waves=waves, chained=False)
    assert chained.makespan == pytest.approx(16.0)
    assert driven.makespan == pytest.approx(8.0)


def test_computed_waves_match_dependency_graph() -> None:
    """Covers: WP-1 exit -- computed waves match the dependency graph."""
    graph = _sample_graph()
    waves = derive_waves(graph)
    wave_of = waves_to_mapping(waves)
    for node in graph.nodes:
        for dep in node.start_dependencies:
            assert wave_of[dep] < wave_of[node.ref]
    assert [wave.index for wave in waves] == list(range(len(waves)))


def test_no_task_starts_before_its_dependencies() -> None:
    """Covers: WP-1 exit -- no task starts before its dependencies."""
    score = score_plan(_sample_graph(), waves=derive_waves(_sample_graph()))
    finish = {window.ref: window.finish for window in score.schedule}
    for window in score.schedule:
        node = next(node for node in _sample_graph().nodes if node.ref == window.ref)
        for dep in node.start_dependencies:
            assert window.start + 1e-9 >= finish[dep]


def test_optimization_reflows_to_dependency_driven_start() -> None:
    """Covers: WP-1 optimization -- dependency-driven start beats chained."""
    graph = _sample_graph()
    proposal = {
        _ref("p1", "a"): 0,
        _ref("p1", "b"): 1,
        _ref("p1", "c"): 3,
        _ref("p2", "e"): 2,
        _ref("p1", "d"): 4,
        _ref("p2", "f"): 5,
    }
    result = optimize_plan(graph, proposed_waves=proposal)
    assert result.initial_score.makespan == pytest.approx(45.0)
    assert result.final_score.makespan == pytest.approx(31.0)
    assert result.final_score.makespan < result.initial_score.makespan


def test_dispatch_order_follows_start_times() -> None:
    """Covers: WP-1 execution invariant -- dispatch-ready when deps finish."""
    graph = _sample_graph()
    result = optimize_plan(graph)
    order = dispatch_order(graph, result)
    assert len(order) == len(graph.nodes)
    starts = {window.ref: window.start for window in result.final_score.schedule}
    assert [starts[ref] for ref in order] == sorted(starts[ref] for ref in order)


# ---------------------------------------------------------------------------
# Barrier tasks
# ---------------------------------------------------------------------------


def test_barrier_holds_all_later_tasks() -> None:
    """Covers: WP-1 exit -- a barrier holds all later tasks."""
    graph = _graph(
        _node("p", "a"),
        _node("p", "gate", barrier=True, integr=2.0, deps=(_ref("p", "a"),)),
        _node("p", "free"),  # no dependency on the barrier
    )
    waves = derive_waves(graph)
    wave_of = waves_to_mapping(waves)
    assert wave_of[_ref("p", "free")] > wave_of[_ref("p", "gate")]
    score = score_plan(graph, waves=waves, chained=False)
    by_ref = _schedule_by_ref(score)
    gate_finish = by_ref[_ref("p", "gate")][1]
    assert by_ref[_ref("p", "free")][0] + 1e-9 >= gate_finish + 2.0


def test_barrier_does_not_hold_its_dependencies() -> None:
    """Covers: WP-1 barrier semantics -- prerequisites are not held."""
    graph = _graph(
        _node("p", "a"),
        _node("p", "gate", barrier=True, integr=5.0, deps=(_ref("p", "a"),)),
    )
    score = score_plan(graph, waves=derive_waves(graph), chained=False)
    by_ref = _schedule_by_ref(score)
    assert by_ref[_ref("p", "a")] == (0.0, 8.0)


def test_barrier_without_dependencies_fences_everything() -> None:
    """Covers: WP-1 barrier semantics -- an unconstrained barrier fences all."""
    graph = _graph(
        _node("p", "a"),
        _node("p", "gate", barrier=True),
    )
    waves = derive_waves(graph)
    wave_of = waves_to_mapping(waves)
    assert wave_of[_ref("p", "a")] > wave_of[_ref("p", "gate")]
    score = score_plan(graph, waves=waves, chained=False)
    by_ref = _schedule_by_ref(score)
    assert by_ref[_ref("p", "a")][0] + 1e-9 >= by_ref[_ref("p", "gate")][1]


def test_non_barrier_tasks_do_not_integrate() -> None:
    """Covers: WP-1 barrier semantics -- only barriers charge integration."""
    score = score_plan(_graph(_node("p", "a")))
    assert score.schedule[0].integration_duration == pytest.approx(0.0)
    barrier = score_plan(_graph(_node("p", "a", barrier=True)))
    assert barrier.schedule[0].integration_duration == pytest.approx(
        default_optimization_limits().barrier_integration_units
    )


# ---------------------------------------------------------------------------
# Scope overlaps
# ---------------------------------------------------------------------------


def test_scope_overlap_detected_in_the_same_wave() -> None:
    """Covers: WP-1 scope overlap -- same-wave overlapping write scopes."""
    graph = _graph(
        _node("p", "x", owns=("src/a.py",)),
        _node("p", "y", owns=("src/a.py",)),
        _node("p", "z", owns=("src/b.py",)),
    )
    overlaps = detect_scope_overlaps(
        graph, {_ref("p", "x"): 0, _ref("p", "y"): 0, _ref("p", "z"): 0}
    )
    assert len(overlaps) == 1
    assert {overlaps[0].left.task_id, overlaps[0].right.task_id} == {"x", "y"}


def test_derive_waves_separates_overlapping_scopes() -> None:
    """Covers: WP-1 optimization -- overlapping scopes never share a wave."""
    graph = _graph(
        _node("p", "x", owns=("src/a/b.py",)),
        _node("p", "y", owns=("src/a/**",)),
    )
    waves = derive_waves(graph)
    wave_of = waves_to_mapping(waves)
    assert wave_of[_ref("p", "x")] != wave_of[_ref("p", "y")]
    assert score_plan(graph, waves=waves).scope_overlap_count == 0


def test_overlapping_scopes_are_serialized_at_run_time() -> None:
    """Covers: WP-1 execution invariant -- conflicting writes never overlap."""
    graph = _graph(
        _node("p", "x", owns=("src/a.py",)),
        _node("p", "y", owns=("src/a.py",)),
    )
    naive = {_ref("p", "x"): 0, _ref("p", "y"): 0}
    serialized = score_plan(graph, waves=naive, chained=False)
    assert serialized.makespan == pytest.approx(16.0)
    unconcerned = score_plan(
        graph,
        waves=naive,
        chained=False,
        limits=OptimizationLimits(serialize_scope_conflicts=False),
    )
    assert unconcerned.makespan == pytest.approx(8.0)


def test_disjoint_subtree_scopes_do_not_conflict() -> None:
    """Covers: WP-1 scope overlap -- subtree containment is the boundary."""
    graph = _graph(
        _node("p", "x", owns=("src/a/**",)),
        _node("p", "y", owns=("src/b/**",)),
    )
    waves = derive_waves(graph)
    assert len(waves) == 1
    assert score_plan(graph, waves=waves).scope_overlap_count == 0


# ---------------------------------------------------------------------------
# Dependency reduction
# ---------------------------------------------------------------------------


def test_remove_redundant_transitive_dependencies() -> None:
    """Covers: WP-1 optimization -- redundant transitive edges removed."""
    graph = _graph(
        _node("p", "a"),
        _node("p", "b", deps=(_ref("p", "a"),)),
        _node("p", "c", deps=(_ref("p", "a"), _ref("p", "b"))),
    )
    reduction = remove_redundant_dependencies(graph)
    assert reduction.removed == ((_ref("p", "c"), _ref("p", "a")),)
    kept = next(node for node in reduction.graph.nodes if node.task_id == "c")
    assert kept.start_dependencies == (_ref("p", "b"),)
    assert compute_critical_path(reduction.graph) == compute_critical_path(graph)


def test_canonicalize_deduplicates_dependencies() -> None:
    """Covers: WP-1 validation -- duplicate edges fold to one."""
    graph = _graph(
        _node("p", "a"),
        _node("p", "b", deps=(_ref("p", "a"), _ref("p", "a"))),
    )
    canonical = canonicalize_graph(graph)
    node = next(node for node in canonical.nodes if node.task_id == "b")
    assert node.start_dependencies == (_ref("p", "a"),)


# ---------------------------------------------------------------------------
# Optimization rounds
# ---------------------------------------------------------------------------


def test_optimize_reaches_a_scored_plan_within_three_rounds() -> None:
    """Covers: WP-1 exit -- a sample roadmap scores within 3 rounds."""
    graph = _sample_graph()
    proposal = {
        _ref("p1", "a"): 0,
        _ref("p1", "b"): 1,
        _ref("p1", "c"): 2,
        _ref("p1", "d"): 3,
        _ref("p2", "e"): 0,
        _ref("p2", "f"): 1,
    }
    result = optimize_plan(graph, proposed_waves=proposal)
    assert 1 <= result.rounds_executed <= 3
    assert result.stopped_reason in (StopReason.MIN_GAIN, StopReason.MAX_ROUNDS)
    assert result.final_score.makespan <= result.initial_score.makespan
    assert [report.round_index for report in result.rounds] == list(
        range(1, result.rounds_executed + 1)
    )


def test_optimize_stops_below_the_gain_threshold() -> None:
    """Covers: WP-1 optimization -- stops below ~10% gain."""
    graph = _sample_graph()
    proposal = {
        _ref("p1", "a"): 0,
        _ref("p1", "b"): 1,
        _ref("p1", "c"): 3,
        _ref("p2", "e"): 2,
        _ref("p1", "d"): 4,
        _ref("p2", "f"): 5,
    }
    result = optimize_plan(
        graph,
        proposed_waves=proposal,
        limits=OptimizationLimits(min_gain=0.99),
    )
    assert result.rounds_executed == 1
    assert result.rounds[-1].gain < 0.99
    assert result.stopped_reason is StopReason.MIN_GAIN


def test_optimize_respects_the_round_cap() -> None:
    """Covers: WP-1 optimization -- never more than the round cap."""
    graph = _sample_graph()
    proposal = {node.ref: index for index, node in enumerate(graph.nodes)}
    result = optimize_plan(
        graph,
        proposed_waves=proposal,
        limits=OptimizationLimits(min_gain=0.0, max_rounds=3),
    )
    assert result.rounds_executed == 3
    assert result.stopped_reason is StopReason.MAX_ROUNDS


def test_optimize_reports_removed_dependencies_first_round() -> None:
    """Covers: WP-1 optimization -- redundant edges reported in round one."""
    graph = _graph(
        _node("p", "a"),
        _node("p", "b", deps=(_ref("p", "a"),)),
        _node("p", "c", deps=(_ref("p", "a"), _ref("p", "b"))),
    )
    result = optimize_plan(graph)
    assert result.removed_dependencies == ((_ref("p", "c"), _ref("p", "a")),)
    assert result.rounds[0].removed_dependencies == 1


def test_optimize_is_deterministic() -> None:
    """Covers: WP-1 optimization -- computed waves are deterministic."""
    graph = _sample_graph()
    first = optimize_plan(graph)
    second = optimize_plan(graph)
    assert first == second


def test_optimize_empty_graph_is_a_no_op() -> None:
    """Covers: WP-1 scoring -- an empty graph scores zero, never crashes."""
    result = optimize_plan(TaskGraph())
    assert result.rounds_executed == 0
    assert result.stopped_reason is StopReason.NO_TASKS
    assert result.final_score.makespan == 0.0
    assert result.computed_waves == ()


# ---------------------------------------------------------------------------
# Wave-boundary re-planning
# ---------------------------------------------------------------------------


def test_wave_boundary_replan_only_covers_unstarted_waves() -> None:
    """Covers: WP-1 execution invariant -- unstarted waves re-plan once."""
    graph = _sample_graph()
    result = optimize_plan(graph)
    by_wave = waves_to_mapping(result.computed_waves)
    boundary = 0
    replanned = wave_boundary_replan(
        graph,
        result,
        integrated_refs=(_ref("p1", "a"),),
        boundary_wave=boundary,
    )
    assert replanned.base_wave == boundary + 1
    replanned_refs = {ref for wave in replanned.computed_waves for ref in wave.tasks}
    for ref in replanned_refs:
        assert result.base_wave + by_wave[ref] > boundary
        assert ref != _ref("p1", "a")
    assert all(wave.index > boundary for wave in replanned.computed_waves)


def test_wave_boundary_replan_rejects_negative_boundary() -> None:
    """Covers: WP-1 validation -- boundary wave must be a non-negative int."""
    graph = _sample_graph()
    result = optimize_plan(graph)
    with pytest.raises(WavePlanError):
        wave_boundary_replan(graph, result, integrated_refs=(), boundary_wave=-1)


# ---------------------------------------------------------------------------
# Validation (fail closed)
# ---------------------------------------------------------------------------


def test_validate_rejects_cycles() -> None:
    """Covers: WP-1 validation -- START cycles fail closed."""
    graph = _graph(
        _node("p", "a", deps=(_ref("p", "b"),)),
        _node("p", "b", deps=(_ref("p", "a"),)),
    )
    with pytest.raises(WavePlanError):
        validate_graph(graph)


def test_validate_rejects_duplicate_and_unknown_tasks() -> None:
    """Covers: WP-1 validation -- duplicate ids and unknown deps fail closed."""
    with pytest.raises(WavePlanError):
        validate_graph(_graph(_node("p", "a"), _node("p", "a")))
    with pytest.raises(WavePlanError):
        validate_graph(_graph(_node("p", "a", deps=(_ref("p", "ghost"),))))


def test_validate_rejects_self_dependency_and_unknown_size() -> None:
    """Covers: WP-1 validation -- self edges and bad sizes fail closed."""
    with pytest.raises(WavePlanError):
        validate_graph(_graph(_node("p", "a", deps=(_ref("p", "a"),))))
    with pytest.raises(WavePlanError):
        validate_graph(_graph(_node("p", "a", size="XL")))


def test_validate_rejects_unsafe_scope_and_negative_checklist() -> None:
    """Covers: WP-1 validation -- unsafe scopes and bad counts fail closed."""
    with pytest.raises(WavePlanError):
        validate_graph(_graph(_node("p", "a", owns=("../escape",))))
    with pytest.raises(WavePlanError):
        validate_graph(_graph(_node("p", "a", checklist=-1)))


def test_invalid_limits_are_rejected() -> None:
    """Covers: WP-1 validation -- malformed limits fail closed."""
    with pytest.raises(WavePlanError):
        validate_graph(_graph(_node("p", "a")), OptimizationLimits(workers_per_project=0))
    with pytest.raises(WavePlanError):
        validate_graph(_graph(_node("p", "a")), OptimizationLimits(min_gain=-0.1))
    with pytest.raises(WavePlanError):
        validate_graph(_graph(_node("p", "a")), OptimizationLimits(units_per_cost_pct=0.0))


def test_proposed_waves_must_respect_dependencies() -> None:
    """Covers: WP-1 validation -- proposals may not violate the graph."""
    graph = _graph(_node("p", "a"), _node("p", "b", deps=(_ref("p", "a"),)))
    with pytest.raises(WavePlanError):
        score_plan(graph, waves={_ref("p", "a"): 1, _ref("p", "b"): 0}, chained=True)


def test_proposed_wave_sequence_must_cover_every_task() -> None:
    """Covers: WP-1 validation -- wave sequences must cover every task."""
    graph = _graph(_node("p", "a"), _node("p", "b"))
    partial = (OptimizedWave(index=0, tasks=(_ref("p", "a"),)),)
    with pytest.raises(WavePlanError):
        score_plan(graph, waves=partial, chained=True)
    with pytest.raises(WavePlanError):
        score_plan(graph, waves={_ref("p", "a"): True, _ref("p", "b"): 0}, chained=True)


def test_partial_wave_mapping_fills_missing_from_derived() -> None:
    """Covers: WP-1 validation -- partial proposals keep derived waves."""
    graph = _graph(_node("p", "a"), _node("p", "b", deps=(_ref("p", "a"),)))
    score = score_plan(graph, waves={_ref("p", "a"): 0}, chained=True)
    assert score.wave_count == 2


# ---------------------------------------------------------------------------
# Ready-frontier and plan-store integration
# ---------------------------------------------------------------------------


def test_graph_from_projects_skips_coordination_records() -> None:
    """Covers: WP-1 integration -- coordination rows never plan as tasks."""
    projects = (
        ProjectPlan(
            project_id="proj",
            tasks=(
                PlanTask(
                    task_id="t1",
                    phase="P1",
                    status=TaskStatus.PENDING.value,
                    kind=TaskKind.CHILD_TASK.value,
                    owns=("src/x.py",),
                ),
                PlanTask(
                    task_id="t2",
                    phase="P1",
                    status=TaskStatus.PENDING.value,
                    kind=TaskKind.CHILD_TASK.value,
                    start_dependencies=(_ref("proj", "t1"),),
                ),
                PlanTask(
                    task_id="wrap",
                    phase="P1",
                    status=TaskStatus.RUNNING.value,
                    kind=TaskKind.WAVE_WRAPPER.value,
                ),
            ),
        ),
    )
    graph = graph_from_projects(projects)
    assert {node.task_id for node in graph.nodes} == {"t1", "t2"}
    assert graph.nodes[0].size is TaskSize.M


def test_graph_from_projects_applies_metadata_sidecar() -> None:
    """Covers: WP-1 integration -- sizes/barriers/gates come from metadata."""
    projects = (
        ProjectPlan(
            project_id="proj",
            tasks=(
                PlanTask(
                    task_id="t1",
                    phase="P1",
                    status=TaskStatus.PENDING.value,
                    kind=TaskKind.CHILD_TASK.value,
                ),
            ),
        ),
    )
    graph = graph_from_projects(
        projects,
        metadata={
            _ref("proj", "t1"): NodeMetadata(
                size="L",
                barrier=True,
                requires_heavy_gate=True,
                checklist_items=4,
            )
        },
    )
    node = graph.nodes[0]
    assert node.size is TaskSize.L
    assert node.barrier is True
    assert node.requires_heavy_gate is True
    assert node.checklist_items == 4


def test_graph_from_revisions_resolves_cross_project_edges() -> None:
    """Covers: WP-1 integration -- plan-store revisions adapt to a graph."""
    tasks_a = (PlanTaskDef(task_id="t1", owns=("src/a.py",)),)
    tasks_b = (PlanTaskDef(task_id="t2", start_dependencies=("proj-a/t1",), owns=("src/b.py",)),)
    revisions = (
        _revision("proj-a", "v1", tasks_a),
        _revision("proj-b", "v1", tasks_b),
    )
    graph = graph_from_revisions(revisions)
    node = next(node for node in graph.nodes if node.task_id == "t2")
    assert node.start_dependencies == (_ref("proj-a", "t1"),)
    waves = waves_to_mapping(derive_waves(graph))
    assert waves[_ref("proj-a", "t1")] < waves[_ref("proj-b", "t2")]


def test_to_scheduled_waves_matches_computed_waves() -> None:
    """Covers: WP-1 integration -- computed waves render as ScheduledWave."""
    graph = _sample_graph()
    result = optimize_plan(graph)
    scheduled = to_scheduled_waves(graph, result)
    assert [wave.index for wave in scheduled] == [wave.index for wave in result.computed_waves]
    assert all(isinstance(task, ScheduledTask) for wave in scheduled for task in wave.tasks)
    flattened = {task.stable_ref for wave in scheduled for task in wave.tasks}
    assert flattened == set(graph.refs())


def test_from_scheduled_waves_round_trips() -> None:
    """Covers: WP-1 integration -- ready-frontier waves convert back."""
    scheduled = (
        ScheduledWave(
            index=0,
            tasks=(ScheduledTask(project_id="p", task_id="a", phase="P1", title="A"),),
        ),
        ScheduledWave(
            index=2,
            tasks=(ScheduledTask(project_id="p", task_id="b", phase="P1", title="B"),),
        ),
    )
    waves = from_scheduled_waves(scheduled)
    assert [wave.index for wave in waves] == [0, 2]
    assert waves_to_mapping(waves) == {_ref("p", "a"): 0, _ref("p", "b"): 2}


def test_public_scoring_helpers_agree_with_optimizer() -> None:
    """Covers: WP-1 integration -- scoring helpers are independently usable."""
    graph = _sample_graph()
    waves = derive_waves(graph)
    score = score_plan(graph, waves=waves)
    assert score.wave_count == len(waves)
    assert score.critical_path == pytest.approx(compute_critical_path(graph))


def _revision(project: str, version: str, tasks: tuple[PlanTaskDef, ...]) -> PlanRevision:
    stamp = "2026-01-01T00:00:00+00:00"
    return PlanRevision(
        version=version,
        project_id=project,
        tasks=tasks,
        created_at=stamp,
        sha256=compute_revision_hash(version, project, tasks, stamp),
    )
