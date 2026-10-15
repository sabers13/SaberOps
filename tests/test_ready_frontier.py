"""Adversarial hermetic tests for the cross-phase ready-frontier projection.

Every test is pure and deterministic: no database, no repository, no network,
no clock, no provider/model call. Each test states the invariant it covers.
"""

from __future__ import annotations

import socket
from dataclasses import replace
from typing import Any

import pytest

from saberops.ready_frontier import (
    DEFAULT_ACTIVE_PROJECT_LIMIT,
    DEFAULT_CHILD_SLOTS_PER_PROJECT_LIMIT,
    REQUIRES_TRANSACTIONAL_VERIFICATION,
    EligibleTask,
    FrontierLimits,
    FrontierProjection,
    FrontierReason,
    FrontierRequest,
    FrontierSnapshot,
    PlanTask,
    ProjectionStatus,
    ProjectPlan,
    SnapshotSource,
    TaskKind,
    TaskRef,
    TaskStatus,
    default_frontier_limits,
    project_ready_frontier,
)

OWNER = "owner-1"


def _task(
    task_id: str,
    *,
    phase: str,
    status: str = TaskStatus.PENDING.value,
    kind: str = TaskKind.CHILD_TASK.value,
    display_step: str | None = None,
    title: str = "",
    start: tuple[TaskRef, ...] = (),
    integrate: tuple[TaskRef, ...] = (),
    owns: tuple[str, ...] = (),
) -> PlanTask:
    return PlanTask(
        task_id=task_id,
        phase=phase,
        status=status,
        kind=kind,
        title=title,
        display_step=display_step,
        start_dependencies=start,
        integration_prerequisites=integrate,
        owns=owns,
    )


def _snapshot(
    *projects: ProjectPlan,
    complete: bool = True,
    source: str = SnapshotSource.OWNER_DB.value,
    owner: str = OWNER,
) -> FrontierSnapshot:
    return FrontierSnapshot(owner_id=owner, source=source, complete=complete, projects=projects)


def _project(project_id: str, *tasks: PlanTask) -> ProjectPlan:
    return ProjectPlan(project_id=project_id, tasks=tasks)


def _projection(
    snapshot: FrontierSnapshot, limits: FrontierLimits | None = None
) -> FrontierProjection:
    request = FrontierRequest(snapshot=snapshot, limits=limits or default_frontier_limits())
    return project_ready_frontier(request)


def _ref(project_id: str, task_id: str) -> TaskRef:
    return TaskRef(project_id=project_id, task_id=task_id)


def _eligible_ids(projection: FrontierProjection) -> list[tuple[str, str]]:
    return [(task.project_id, task.task_id) for task in projection.eligible]


def _held_reasons(projection: FrontierProjection, task_id: str) -> tuple[FrontierReason, ...]:
    for task in projection.held:
        if task.task_id == task_id:
            return task.reasons
    raise AssertionError(f"task {task_id!r} not present in holds")


def _sole_hold(projection: FrontierProjection, task_id: str) -> FrontierReason:
    reasons = _held_reasons(projection, task_id)
    assert len(reasons) == 1, reasons
    return reasons[0]


# --------------------------------------------------------------------------
# Cross-phase independence
# --------------------------------------------------------------------------


def test_blocked_early_phase_keeps_later_phase_independent_task_ready() -> None:
    """Covers: ADR-0004 Amendment 5 - a blocked dependency holds only its dependents.

    Four named phases are present; the Phase 1 task is blocked while a Phase 4
    task with no dependency on it remains eligible.
    """
    snapshot = _snapshot(
        _project(
            "p1",
            _task("build-core", phase="01-engine-core", status=TaskStatus.BLOCKED.value),
            _task("finish-core", phase="01-engine-core", start=(_ref("p1", "build-core"),)),
        ),
        _project("p4", _task("build-ui", phase="04-interfaces-usability", display_step="019")),
    )
    projection = _projection(snapshot)
    assert projection.projected
    assert _eligible_ids(projection) == [("p4", "build-ui")]
    assert _sole_hold(projection, "build-core") is FrontierReason.TASK_BLOCKED
    assert _sole_hold(projection, "finish-core") is FrontierReason.DEPENDENCY_BLOCKED


def test_later_phase_implementation_starts_while_earlier_integration_waits() -> None:
    """Covers: ADR-0004 Amendment 5 - later-phase work does not wait on an integration edge.

    The Phase 1 task is complete but unintegrated (its dependent carries it as a
    deferred integration prerequisite, not a START dependency).
    """
    snapshot = _snapshot(
        _project(
            "p1",
            _task("core-a", phase="01-engine-core", status=TaskStatus.COMPLETED.value),
            _task(
                "core-b",
                phase="01-engine-core",
                integrate=(_ref("p1", "core-a"),),
            ),
        ),
    )
    projection = _projection(snapshot)
    assert _eligible_ids(projection) == [("p1", "core-b")]
    core_b = projection.eligible[0]
    assert core_b.requires_integration is True
    assert core_b.integration_prerequisites == (_ref("p1", "core-a"),)


def test_dependency_pending_is_distinct_from_dependency_blocked() -> None:
    """Covers: a merely not-yet-complete dependency is pending, not failed."""
    snapshot = _snapshot(
        _project(
            "p1",
            _task("dep", phase="01-engine-core", status=TaskStatus.RUNNING.value),
            _task("child", phase="02-test-reliability", start=(_ref("p1", "dep"),)),
        ),
    )
    projection = _projection(snapshot)
    assert _eligible_ids(projection) == []
    assert _sole_hold(projection, "child") is FrontierReason.DEPENDENCY_PENDING


def test_completion_releases_dependent_and_failure_holds_it() -> None:
    """Covers: ADR-0004 Amendment 5 - completion and failure updates move the frontier."""
    edge = (_ref("p1", "dep"),)
    child = _task("child", phase="01-engine-core", start=edge)

    completed = _snapshot(
        _project(
            "p1",
            _task("dep", phase="01-engine-core", status=TaskStatus.COMPLETED.value),
            child,
        ),
    )
    assert _eligible_ids(_projection(completed)) == [("p1", "child")]

    failed = _snapshot(
        _project(
            "p1",
            _task("dep", phase="01-engine-core", status=TaskStatus.FAILED.value),
            child,
        ),
    )
    projection = _projection(failed)
    assert projection.eligible == ()
    assert _sole_hold(projection, "child") is FrontierReason.DEPENDENCY_BLOCKED


# --------------------------------------------------------------------------
# Ownership and coordination records
# --------------------------------------------------------------------------


def test_overlapping_write_scope_is_held_but_disjoint_scope_is_eligible() -> None:
    """Covers: WORKFLOW section 16 - exclusive write scopes; disjoint lanes progress."""
    snapshot = _snapshot(
        _project(
            "p1",
            _task("a", phase="01-engine-core", owns=("src/saberops/a.py",)),
            _task("b", phase="01-engine-core", owns=("src/saberops/a.py",)),
            _task("c", phase="01-engine-core", owns=("src/saberops/c.py",)),
        ),
    )
    projection = _projection(snapshot)
    assert _eligible_ids(projection) == [("p1", "a"), ("p1", "c")]
    assert _sole_hold(projection, "b") is FrontierReason.WRITE_SCOPE_CONFLICT


def test_subtree_scope_overlap_is_detected() -> None:
    """Covers: subtree write-scope overlap uses the same semantics as parallel_work."""
    snapshot = _snapshot(
        _project(
            "p1",
            _task("a", phase="01", owns=("src/saberops/**",)),
            _task("b", phase="01", owns=("src/saberops/pkg/mod.py",)),
        ),
    )
    projection = _projection(snapshot)
    assert _eligible_ids(projection) == [("p1", "a")]
    assert _sole_hold(projection, "b") is FrontierReason.WRITE_SCOPE_CONFLICT


def test_duplicate_coordination_wrappers_are_not_extra_child_tasks() -> None:
    """Covers: ADR-0004 Amendment 5 - wrapper records do not double-count as lanes."""
    wrapper = _task(
        "plan-1", phase="01-engine-core", kind=TaskKind.PLAN_WRAPPER.value
    )
    snapshot = _snapshot(_project("p1", wrapper, replace(wrapper), wrapper))
    projection = _projection(snapshot)
    assert projection.eligible == ()
    assert projection.coordination_count == 1
    assert len(projection.held) == 1
    assert _sole_hold(projection, "plan-1") is FrontierReason.COORDINATION_RECORD


def test_orchestrator_turn_and_wave_wrapper_never_eligible() -> None:
    """Covers: ADR-0004 Amendment 5 - Orchestrator turns are not executable lanes."""
    snapshot = _snapshot(
        _project(
            "p1",
            _task("wave", phase="01", kind=TaskKind.WAVE_WRAPPER.value),
            _task("turn", phase="01", kind=TaskKind.ORCHESTRATOR_TURN.value),
            _task("lane", phase="01"),
        ),
    )
    projection = _projection(snapshot)
    assert _eligible_ids(projection) == [("p1", "lane")]
    assert _sole_hold(projection, "wave") is FrontierReason.COORDINATION_RECORD
    assert _sole_hold(projection, "turn") is FrontierReason.COORDINATION_RECORD


# --------------------------------------------------------------------------
# Declared resource/capacity constraints
# --------------------------------------------------------------------------


def test_owner_and_resource_waits_are_reasoned_holds() -> None:
    """Covers: candidate resource/capacity constraints - explicit owner/resource waits."""
    snapshot = _snapshot(
        _project(
            "p1",
            _task("owner-gated", phase="01", status=TaskStatus.OWNER_WAIT.value),
            _task("disk-gated", phase="01", status=TaskStatus.RESOURCE_WAIT.value),
        ),
    )
    projection = _projection(snapshot)
    assert projection.eligible == ()
    assert _sole_hold(projection, "owner-gated") is FrontierReason.OWNER_WAIT
    assert _sole_hold(projection, "disk-gated") is FrontierReason.RESOURCE_WAIT


def test_owner_wait_dependency_holds_only_its_dependents() -> None:
    """Covers: ADR-0004 Amendment 5 - an owner-blocked task freezes only dependents."""
    snapshot = _snapshot(
        _project(
            "p1",
            _task("gated", phase="01", status=TaskStatus.OWNER_WAIT.value),
            _task("child", phase="01", start=(_ref("p1", "gated"),)),
        ),
        _project("p2", _task("other", phase="05")),
    )
    projection = _projection(snapshot)
    assert _eligible_ids(projection) == [("p2", "other")]
    assert _sole_hold(projection, "child") is FrontierReason.DEPENDENCY_PENDING


# --------------------------------------------------------------------------
# Capacity bound / 3x5
# --------------------------------------------------------------------------


def test_three_projects_by_five_tasks_reports_full_fifteen() -> None:
    """Covers: ADR-0004 Amendment 5 - no silent lowering of the owner 3x5 bound."""
    projects = tuple(
        _project(f"p{n}", *(_task(f"t{i}", phase="01") for i in range(5)))
        for n in range(DEFAULT_ACTIVE_PROJECT_LIMIT)
    )
    projection = _projection(_snapshot(*projects))
    assert projection.eligible_count == 15
    assert projection.derived_global_ceiling == (
        DEFAULT_ACTIVE_PROJECT_LIMIT * DEFAULT_CHILD_SLOTS_PER_PROJECT_LIMIT
    )
    assert projection.limits.active_projects == 3
    assert projection.limits.child_slots_per_project == 5


def test_sixth_ready_task_in_one_project_is_capacity_held() -> None:
    """Covers: ADR-0004 Amendment 5 - per-project maximum holds the overflow."""
    snapshot = _snapshot(_project("p1", *(_task(f"t{i}", phase="01") for i in range(6))))
    projection = _projection(snapshot)
    assert projection.eligible_count == 5
    assert _sole_hold(projection, "t5") is FrontierReason.CAPACITY_WAIT


def test_fourth_active_project_is_capacity_held() -> None:
    """Covers: ADR-0004 Amendment 5 - three active projects, not a global cap of 15."""
    projects = tuple(_project(f"p{n}", _task("only", phase="01")) for n in range(4))
    projection = _projection(_snapshot(*projects))
    assert projection.eligible_count == 3
    assert _sole_hold(projection, "only") is FrontierReason.CAPACITY_WAIT
    assert [ref.project_id for ref in projection.eligible_refs()] == ["p0", "p1", "p2"]


def test_explicit_lower_limits_are_respected_not_silently_raised() -> None:
    """Covers: declared limits are honoured; the projection never invents capacity."""
    snapshot = _snapshot(_project("p1", _task("a", phase="01"), _task("b", phase="01")))
    projection = _projection(snapshot, FrontierLimits(active_projects=1, child_slots_per_project=1))
    assert projection.eligible_count == 1
    assert _sole_hold(projection, "b") is FrontierReason.CAPACITY_WAIT


# --------------------------------------------------------------------------
# Determinism, ordering, immutability
# --------------------------------------------------------------------------


def test_eligibles_are_ordered_by_stable_identity_not_display_number() -> None:
    """Covers: WORKFLOW section 15 - display number is not identity or order."""
    snapshot = _snapshot(
        _project(
            "p1",
            _task("zulu", phase="01", display_step="001"),
            _task("alpha", phase="04", display_step="900"),
        ),
        _project(
            "p0",
            _task("beta", phase="02", display_step="500"),
        ),
    )
    projection = _projection(snapshot)
    assert _eligible_ids(projection) == [("p0", "beta"), ("p1", "alpha"), ("p1", "zulu")]
    assert projection.eligible[0].display_step == "500"
    assert projection.eligible[1].display_step == "900"


def test_projection_is_deterministic_and_does_not_mutate_inputs() -> None:
    """Covers: pure determinism and no input mutation."""
    snapshot = _snapshot(
        _project(
            "p1",
            _task("a", phase="01", status=TaskStatus.COMPLETED.value),
            _task("b", phase="01", start=(_ref("p1", "a"),), owns=("x/y.py",)),
            _task("c", phase="04", display_step="019"),
        ),
    )
    before = snapshot
    first = _projection(snapshot)
    second = _projection(snapshot)
    assert first == second
    assert snapshot == before
    assert _eligible_ids(first) == [("p1", "b"), ("p1", "c")]


def test_repeated_calls_are_stable_across_input_order() -> None:
    """Covers: the eligible set and its order are independent of declaration order."""
    t1 = _task("t1", phase="04", display_step="019")
    t2 = _task("t2", phase="01", owns=("only.py",))
    forward = _snapshot(_project("p1", t1, t2))
    reverse = _snapshot(_project("p1", t2, t1))
    assert _eligible_ids(_projection(forward)) == _eligible_ids(_projection(reverse))


# --------------------------------------------------------------------------
# Fail-closed structural cases
# --------------------------------------------------------------------------


def test_unknown_status_fails_closed() -> None:
    """Covers: ADR-0004 Amendment 5 - unknown status fails closed."""
    snapshot = _snapshot(_project("p1", _task("a", phase="01", status="vibing")))
    projection = _projection(snapshot)
    assert projection.status is ProjectionStatus.DENIED
    assert projection.eligible == ()
    assert projection.counts_authoritative is False
    assert FrontierReason.UNKNOWN_TASK_STATUS in projection.reasons


def test_unknown_kind_fails_closed() -> None:
    """Covers: unknown task kind fails closed instead of being ignored."""
    snapshot = _snapshot(_project("p1", _task("a", phase="01", kind="sidequest")))
    projection = _projection(snapshot)
    assert projection.status is ProjectionStatus.DENIED
    assert FrontierReason.UNKNOWN_TASK_KIND in projection.reasons


def test_conflicting_duplicate_task_identity_fails_closed() -> None:
    """Covers: ADR-0004 Amendment 5 - conflicting duplicate identity fails closed."""
    snapshot = _snapshot(
        _project(
            "p1",
            _task("dup", phase="01", status=TaskStatus.PENDING.value),
            _task("dup", phase="04", status=TaskStatus.RUNNING.value),
        ),
    )
    projection = _projection(snapshot)
    assert projection.status is ProjectionStatus.DENIED
    assert FrontierReason.CONFLICTING_TASK_IDENTITY in projection.reasons


def test_duplicate_task_id_in_two_projects_is_not_a_conflict() -> None:
    """Covers: stable identity is the (project_id, task_id) pair, not the task_id alone."""
    snapshot = _snapshot(
        _project("p1", _task("shared-name", phase="01")),
        _project("p2", _task("shared-name", phase="04")),
    )
    projection = _projection(snapshot)
    assert projection.projected
    assert _eligible_ids(projection) == [("p1", "shared-name"), ("p2", "shared-name")]


def test_missing_start_dependency_fails_closed() -> None:
    """Covers: missing dependencies fail closed."""
    snapshot = _snapshot(
        _project("p1", _task("a", phase="01", start=(_ref("p1", "ghost"),)))
    )
    projection = _projection(snapshot)
    assert projection.status is ProjectionStatus.DENIED
    assert FrontierReason.MISSING_DEPENDENCY in projection.reasons


def test_missing_integration_prerequisite_fails_closed() -> None:
    """Covers: deferred integration prerequisites must resolve to a known task."""
    snapshot = _snapshot(
        _project("p1", _task("a", phase="01", integrate=(_ref("p1", "ghost"),)))
    )
    projection = _projection(snapshot)
    assert projection.status is ProjectionStatus.DENIED
    assert FrontierReason.MISSING_INTEGRATION_PREREQUISITE in projection.reasons


def test_start_dependency_cycle_fails_closed() -> None:
    """Covers: cycle detection on hard START edges."""
    snapshot = _snapshot(
        _project(
            "p1",
            _task("a", phase="01", start=(_ref("p1", "b"),)),
            _task("b", phase="01", start=(_ref("p1", "a"),)),
        ),
    )
    projection = _projection(snapshot)
    assert projection.status is ProjectionStatus.DENIED
    assert FrontierReason.CYCLIC_DEPENDENCY in projection.reasons


def test_self_dependency_fails_closed() -> None:
    """Covers: a task may not depend on itself."""
    snapshot = _snapshot(
        _project("p1", _task("a", phase="01", start=(_ref("p1", "a"),)))
    )
    projection = _projection(snapshot)
    assert FrontierReason.CYCLIC_DEPENDENCY in projection.reasons


def test_integration_prerequisite_cycle_fails_closed() -> None:
    """Covers: a cycle among deferred integration edges is still unverifiable."""
    snapshot = _snapshot(
        _project(
            "p1",
            _task("a", phase="01", integrate=(_ref("p1", "b"),)),
            _task("b", phase="01", integrate=(_ref("p1", "a"),)),
        ),
    )
    projection = _projection(snapshot)
    assert FrontierReason.CYCLIC_INTEGRATION_PREREQUISITE in projection.reasons


def test_incomplete_snapshot_fails_closed() -> None:
    """Covers: ADR-0004 Amendment 5 - a partial snapshot cannot claim a ready set."""
    snapshot = _snapshot(_project("p1", _task("a", phase="01")), complete=False)
    projection = _projection(snapshot)
    assert projection.status is ProjectionStatus.DENIED
    assert projection.eligible == ()
    assert FrontierReason.INCOMPLETE_SNAPSHOT in projection.reasons


def test_unknown_snapshot_source_fails_closed() -> None:
    """Covers: only the authoritative owner source is trusted."""
    snapshot = _snapshot(
        _project("p1", _task("a", phase="01")), source=SnapshotSource.UNKNOWN.value
    )
    projection = _projection(snapshot)
    assert FrontierReason.UNKNOWN_SNAPSHOT_SOURCE in projection.reasons


def test_missing_owner_identity_fails_closed() -> None:
    """Covers: missing identity fails closed."""
    snapshot = _snapshot(_project("p1", _task("a", phase="01")), owner="  ")
    projection = _projection(snapshot)
    assert FrontierReason.MISSING_IDENTITY in projection.reasons


def test_blank_project_identity_fails_closed() -> None:
    """Covers: missing project identity fails closed."""
    snapshot = _snapshot(_project("   ", _task("a", phase="01")))
    projection = _projection(snapshot)
    assert FrontierReason.MISSING_IDENTITY in projection.reasons


def test_invalid_limits_fail_closed() -> None:
    """Covers: invalid declared limits fail closed; no unlimited fallback."""
    snapshot = _snapshot(_project("p1", _task("a", phase="01")))
    projection = _projection(snapshot, FrontierLimits(active_projects=0, child_slots_per_project=5))
    assert projection.status is ProjectionStatus.DENIED
    assert FrontierReason.INVALID_LIMIT in projection.reasons


# --------------------------------------------------------------------------
# No launch authority / no provider / no side effects
# --------------------------------------------------------------------------


def test_projection_is_never_launch_authority() -> None:
    """Covers: a pure projection alone is not dispatch authority (Step 010 owns admission)."""
    snapshot = _snapshot(_project("p1", _task("a", phase="01")))
    projection = _projection(snapshot)
    assert REQUIRES_TRANSACTIONAL_VERIFICATION is True
    assert projection.requires_transactional_verification is True


def test_projection_makes_no_network_or_provider_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Covers: no model/provider/network call - scheduler decisions are deterministic."""

    def _forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the ready-frontier projection must not touch the network")

    monkeypatch.setattr(socket, "socket", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)
    snapshot = _snapshot(
        _project(
            "p1",
            _task("a", phase="01", status=TaskStatus.COMPLETED.value),
            _task("b", phase="04", start=(_ref("p1", "a"),)),
        ),
    )
    projection = _projection(snapshot)
    assert _eligible_ids(projection) == [("p1", "b")]


def test_eligible_task_exposes_stable_ref_and_display_step() -> None:
    """Covers: immutable display identity is carried, never used as identity."""
    task = _task("t", phase="04", display_step="019", title="First-run UI")
    projection = _projection(_snapshot(_project("p1", task)))
    eligible: EligibleTask = projection.eligible[0]
    assert eligible.stable_ref == TaskRef(project_id="p1", task_id="t")
    assert eligible.display_step == "019"
    assert eligible.title == "First-run UI"
