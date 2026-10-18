"""Hermetic tests for the versioned plan store, wave scheduling and run reports.

Every test is hermetic: all state lives under ``tmp_path`` or in immutable
in-memory snapshots, with no provider call, no network and no clock. Each
case states the invariant it covers.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from saberops.cli import main
from saberops.project_scope.plan_store import (
    PlanNotFoundError,
    PlanRevision,
    PlanStore,
    PlanStoreError,
    PlanTamperError,
    PlanTaskDef,
    PlanValidationError,
    compute_revision_hash,
    plan_revisions_to_snapshot,
)
from saberops.ready_frontier import (
    FrontierLimits,
    FrontierReason,
    FrontierRequest,
    PlanTask,
    ProjectPlan,
    TaskRef,
    WaveScheduleError,
    build_plan_run_report,
    default_frontier_limits,
    project_ready_frontier,
    schedule_chained_waves,
)


def _store(tmp_path: Path) -> PlanStore:
    """Covers: store isolation — every test owns its backing directory."""
    return PlanStore(tmp_path / "plans")


def _task(
    task_id: str,
    start: tuple[str, ...] = (),
    integrate: tuple[str, ...] = (),
    owns: tuple[str, ...] = (),
    phase: str = "P1",
    title: str = "",
) -> PlanTaskDef:
    """Covers: compact stored-task construction shared by DAG cases."""
    return PlanTaskDef(
        task_id=task_id,
        title=title or task_id,
        phase=phase,
        start_dependencies=start,
        integration_prerequisites=integrate,
        owns=owns,
    )


def _row(
    project: str,
    task_id: str,
    status: str = "pending",
    start: tuple[TaskRef, ...] = (),
    integrate: tuple[TaskRef, ...] = (),
) -> PlanTask:
    """Covers: compact wave-scheduler row construction."""
    return PlanTask(
        task_id=task_id,
        phase="P1",
        status=status,
        start_dependencies=start,
        integration_prerequisites=integrate,
    )


def _plan(project: str, *tasks: PlanTask) -> ProjectPlan:
    """Covers: compact single-project wave input."""
    return ProjectPlan(project_id=project, tasks=tasks)


# ---------------------------------------------------------------------------
# PlanStore persistence
# ---------------------------------------------------------------------------


def test_save_get_round_trip_verifies(tmp_path: Path) -> None:
    """Covers: revision save/load round-trips with a verifiable content hash."""
    store = _store(tmp_path)
    revision = store.save_revision(
        "alpha-1", [_task("t1"), _task("t2", start=("t1",))], created_at="2026-10-18T00:00:00+00:00"
    )
    assert revision.version == "v1"
    assert revision.verify() is True
    loaded = store.get_revision("alpha-1", "v1")
    assert loaded == revision


def test_versions_auto_increment(tmp_path: Path) -> None:
    """Covers: versions default to monotonic v1, v2, ... per project."""
    store = _store(tmp_path)
    first = store.save_revision("alpha-1", [_task("t1")], created_at="2026-10-18T00:00:00+00:00")
    second = store.save_revision(
        "alpha-1", [_task("t1"), _task("t2")], created_at="2026-10-18T00:00:01+00:00"
    )
    assert (first.version, second.version) == ("v1", "v2")
    assert store.list_revisions("alpha-1") == ("v1", "v2")


def test_revisions_are_immutable(tmp_path: Path) -> None:
    """Covers: re-saving an existing version is refused (correct via new version)."""
    store = _store(tmp_path)
    store.save_revision(
        "alpha-1", [_task("t1")], version="v7", created_at="2026-10-18T00:00:00+00:00"
    )
    with pytest.raises(PlanStoreError):
        store.save_revision(
            "alpha-1", [_task("t1")], version="v7", created_at="2026-10-18T00:00:00+00:00"
        )


def test_active_pointer_lifecycle(tmp_path: Path) -> None:
    """Covers: set_active/load_active track one active revision per project."""
    store = _store(tmp_path)
    with pytest.raises(PlanNotFoundError):
        store.load_active("alpha-1")
    store.save_revision("alpha-1", [_task("t1")], created_at="2026-10-18T00:00:00+00:00")
    store.save_revision("alpha-1", [_task("t1")], created_at="2026-10-18T00:00:01+00:00")
    assert store.active_version("alpha-1") is None
    store.set_active("alpha-1", "v2")
    assert store.active_version("alpha-1") == "v2"
    assert store.load_active("alpha-1").version == "v2"


def test_set_active_unknown_refused(tmp_path: Path) -> None:
    """Covers: activating a nonexistent revision fails closed."""
    store = _store(tmp_path)
    with pytest.raises(PlanNotFoundError):
        store.set_active("alpha-1", "v9")


def test_get_unknown_revision_refused(tmp_path: Path) -> None:
    """Covers: loading a nonexistent revision fails closed."""
    store = _store(tmp_path)
    with pytest.raises(PlanNotFoundError):
        store.get_revision("alpha-1", "v1")


def test_tamper_detected_on_load(tmp_path: Path) -> None:
    """Covers: hand-edited revision files are detected, never trusted."""
    store = _store(tmp_path)
    store.save_revision("alpha-1", [_task("t1")], created_at="2026-10-18T00:00:00+00:00")
    path = tmp_path / "plans" / "alpha-1" / "v1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["tasks"].append(
        {
            "task_id": "injected",
            "title": "",
            "phase": "",
            "start_dependencies": [],
            "integration_prerequisites": [],
            "owns": [],
        }
    )
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(PlanTamperError):
        store.get_revision("alpha-1", "v1")


def test_corrupt_revision_file_detected(tmp_path: Path) -> None:
    """Covers: an unreadable revision file is tamper evidence, not empty data."""
    store = _store(tmp_path)
    store.save_revision("alpha-1", [_task("t1")], created_at="2026-10-18T00:00:00+00:00")
    (tmp_path / "plans" / "alpha-1" / "v1.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(PlanTamperError):
        store.get_revision("alpha-1", "v1")


def test_hash_is_deterministic() -> None:
    """Covers: the content hash is a pure function of revision content."""
    tasks = (_task("t1"), _task("t2", start=("t1",)))
    left = compute_revision_hash("v1", "alpha-1", tasks, "2026-10-18T00:00:00+00:00")
    right = compute_revision_hash("v1", "alpha-1", tasks, "2026-10-18T00:00:00+00:00")
    assert left == right and len(left) == 64


def test_from_dict_rejects_missing_hash() -> None:
    """Covers: a stored revision without a hash is rejected."""
    with pytest.raises(PlanTamperError):
        PlanRevision.from_dict({"version": "v1", "project_id": "alpha-1", "tasks": []})


# ---------------------------------------------------------------------------
# PlanStore DAG validation
# ---------------------------------------------------------------------------


def test_duplicate_task_ids_refused(tmp_path: Path) -> None:
    """Covers: duplicate task IDs fail validation before anything is written."""
    store = _store(tmp_path)
    with pytest.raises(PlanValidationError):
        store.save_revision(
            "alpha-1", [_task("t1"), _task("t1")], created_at="2026-10-18T00:00:00+00:00"
        )
    assert store.list_revisions("alpha-1") == ()


def test_unknown_dependency_refused(tmp_path: Path) -> None:
    """Covers: START edges to unknown same-project tasks are refused."""
    store = _store(tmp_path)
    with pytest.raises(PlanValidationError):
        store.save_revision(
            "alpha-1", [_task("t1", start=("ghost",))], created_at="2026-10-18T00:00:00+00:00"
        )


def test_self_dependency_refused(tmp_path: Path) -> None:
    """Covers: self-dependencies are refused as trivial START cycles."""
    store = _store(tmp_path)
    with pytest.raises(PlanValidationError):
        store.save_revision(
            "alpha-1", [_task("t1", start=("t1",))], created_at="2026-10-18T00:00:00+00:00"
        )


def test_start_cycle_refused(tmp_path: Path) -> None:
    """Covers: hard START cycles (a->b->a) are refused at save time."""
    store = _store(tmp_path)
    with pytest.raises(PlanValidationError):
        store.save_revision(
            "alpha-1",
            [_task("t1", start=("t2",)), _task("t2", start=("t1",))],
            created_at="2026-10-18T00:00:00+00:00",
        )


def test_blank_task_id_refused(tmp_path: Path) -> None:
    """Covers: blank task IDs are refused."""
    store = _store(tmp_path)
    with pytest.raises(PlanValidationError):
        store.save_revision("alpha-1", [_task("  ")], created_at="2026-10-18T00:00:00+00:00")


def test_cross_project_ref_shape_accepted(tmp_path: Path) -> None:
    """Covers: qualified cross-project refs pass save-time shape checks."""
    store = _store(tmp_path)
    revision = store.save_revision(
        "alpha-1",
        [_task("t1", start=("beta-2/t9",))],
        created_at="2026-10-18T00:00:00+00:00",
    )
    assert revision.tasks[0].start_dependencies == ("beta-2/t9",)


def test_malformed_cross_project_ref_refused(tmp_path: Path) -> None:
    """Covers: malformed qualified refs (bad project slug) are refused."""
    store = _store(tmp_path)
    with pytest.raises(PlanValidationError):
        store.save_revision(
            "alpha-1", [_task("t1", start=("BAD/t9",))], created_at="2026-10-18T00:00:00+00:00"
        )


def test_malformed_version_and_project_refused(tmp_path: Path) -> None:
    """Covers: malformed version strings and project IDs are refused."""
    store = _store(tmp_path)
    with pytest.raises(PlanValidationError):
        store.save_revision("BAD ID", [_task("t1")], created_at="2026-10-18T00:00:00+00:00")
    with pytest.raises(PlanValidationError):
        store.save_revision(
            "alpha-1", [_task("t1")], version="../evil", created_at="2026-10-18T00:00:00+00:00"
        )


# ---------------------------------------------------------------------------
# Snapshot adapter
# ---------------------------------------------------------------------------


def test_adapter_projects_with_dependency_holds() -> None:
    """Covers: stored revisions adapt into a PROJECTED snapshot with exact holds."""
    first = PlanRevision(
        version="v1",
        project_id="alpha-1",
        created_at="2026-10-18T00:00:00+00:00",
        tasks=(_task("t1"), _task("t2", start=("t1",))),
        sha256=compute_revision_hash(
            "v1",
            "alpha-1",
            (_task("t1"), _task("t2", start=("t1",))),
            "2026-10-18T00:00:00+00:00",
        ),
    )
    snapshot = plan_revisions_to_snapshot([first], owner_id="owner-1")
    projection = project_ready_frontier(FrontierRequest(snapshot=snapshot))
    assert projection.projected is True
    assert [task.task_id for task in projection.eligible] == ["t1"]
    held = {task.task_id: task.reason_codes() for task in projection.held}
    assert held["t2"] == (FrontierReason.DEPENDENCY_PENDING.value,)


def test_adapter_cross_project_edge_resolves() -> None:
    """Covers: qualified cross-project edges resolve against the named project."""
    first = PlanRevision(
        version="v1",
        project_id="alpha-1",
        created_at="2026-10-18T00:00:00+00:00",
        tasks=(_task("t1", start=("beta-2/t9",)),),
        sha256="",
    )
    second = PlanRevision(
        version="v1",
        project_id="beta-2",
        created_at="2026-10-18T00:00:00+00:00",
        tasks=(_task("t9"),),
        sha256="",
    )
    snapshot = plan_revisions_to_snapshot([first, second], owner_id="owner-1")
    alpha = next(p for p in snapshot.projects if p.project_id == "alpha-1")
    assert alpha.tasks[0].start_dependencies == (TaskRef("beta-2", "t9"),)
    projection = project_ready_frontier(FrontierRequest(snapshot=snapshot))
    assert projection.projected is True
    assert [task.task_id for task in projection.eligible] == ["t9"]


# ---------------------------------------------------------------------------
# Chained wave scheduling
# ---------------------------------------------------------------------------


def test_waves_follow_start_topology() -> None:
    """Covers: waves sequence so each wave's START deps sit in earlier waves."""
    waves = schedule_chained_waves(
        (
            _plan(
                "p1",
                _row("p1", "t1"),
                _row("p1", "t2", start=(TaskRef("p1", "t1"),)),
                _row("p1", "t3", start=(TaskRef("p1", "t2"),)),
            ),
        )
    )
    assert [[task.task_id for task in wave.tasks] for wave in waves] == [
        ["t1"],
        ["t2"],
        ["t3"],
    ]
    assert [wave.index for wave in waves] == [0, 1, 2]


def test_waves_group_independent_tasks() -> None:
    """Covers: independent tasks share one wave in stable identity order."""
    waves = schedule_chained_waves(
        (_plan("p1", _row("p1", "b"), _row("p1", "a")),), default_frontier_limits()
    )
    assert len(waves) == 1
    assert [task.task_id for task in waves[0].tasks] == ["a", "b"]


def test_waves_split_oversized_project_layer() -> None:
    """Covers: a layer beyond 5 child slots per project splits into waves."""
    tasks = tuple(_row("p1", f"t{i:02d}") for i in range(6))
    waves = schedule_chained_waves((_plan("p1", *tasks),), default_frontier_limits())
    assert [len(wave.tasks) for wave in waves] == [5, 1]


def test_waves_split_oversized_project_set() -> None:
    """Covers: a wave holds at most 3 distinct projects (4 projects -> 2 waves)."""
    plans = tuple(_plan(f"p{i}", _row(f"p{i}", "t1")) for i in range(4))
    waves = schedule_chained_waves(plans, default_frontier_limits())
    assert len(waves) == 2
    assert [task.project_id for task in waves[0].tasks] == ["p0", "p1", "p2"]
    assert [task.project_id for task in waves[1].tasks] == ["p3"]


def test_waves_custom_limits_respected() -> None:
    """Covers: caller-supplied limits bound wave width (1x1 serializes)."""
    limits = FrontierLimits(active_projects=1, child_slots_per_project=1)
    waves = schedule_chained_waves((_plan("p1", _row("p1", "a"), _row("p1", "b")),), limits)
    assert [[task.task_id for task in wave.tasks] for wave in waves] == [["a"], ["b"]]


def test_running_tasks_reported_in_wave_zero() -> None:
    """Covers: RUNNING tasks sit in wave 0 as underway work, not new admission."""
    waves = schedule_chained_waves(
        (
            _plan(
                "p1",
                _row("p1", "t1", status="running"),
                _row("p1", "t2", start=(TaskRef("p1", "t1"),)),
            ),
        )
    )
    assert [task.task_id for task in waves[0].tasks] == ["t1"]
    assert waves[0].tasks[0].running is True
    assert [task.task_id for task in waves[1].tasks] == ["t2"]


def test_completed_tasks_excluded_as_satisfied() -> None:
    """Covers: COMPLETED tasks impose no wave and unblock dependents."""
    waves = schedule_chained_waves(
        (
            _plan(
                "p1",
                _row("p1", "t1", status="completed"),
                _row("p1", "t2", start=(TaskRef("p1", "t1"),)),
            ),
        )
    )
    assert [[task.task_id for task in wave.tasks] for wave in waves] == [["t2"]]


def test_blocked_task_fails_closed() -> None:
    """Covers: no wave can execute past a BLOCKED task — schedule is refused."""
    with pytest.raises(WaveScheduleError):
        schedule_chained_waves(
            (
                _plan(
                    "p1",
                    _row("p1", "t1", status="failed"),
                    _row("p1", "t2", start=(TaskRef("p1", "t1"),)),
                ),
            )
        )


def test_undepended_blocked_task_fails_closed() -> None:
    """Covers: even an undepended-upon BLOCKED task refuses scheduling."""
    with pytest.raises(WaveScheduleError):
        schedule_chained_waves((_plan("p1", _row("p1", "t1", status="blocked")),))


def test_wave_cycle_fails_closed() -> None:
    """Covers: START cycles are refused by the scheduler."""
    with pytest.raises(WaveScheduleError):
        schedule_chained_waves(
            (
                _plan(
                    "p1",
                    _row("p1", "t1", start=(TaskRef("p1", "t2"),)),
                    _row("p1", "t2", start=(TaskRef("p1", "t1"),)),
                ),
            )
        )


def test_wave_missing_dependency_fails_closed() -> None:
    """Covers: edges to unknown tasks refuse scheduling."""
    with pytest.raises(WaveScheduleError):
        schedule_chained_waves((_plan("p1", _row("p1", "t1", start=(TaskRef("p1", "ghost"),))),))


def test_wave_unknown_status_fails_closed() -> None:
    """Covers: unknown statuses refuse scheduling (never silently dropped)."""
    with pytest.raises(WaveScheduleError):
        schedule_chained_waves((_plan("p1", _row("p1", "t1", status="mystery")),))


def test_wave_invalid_limits_refused() -> None:
    """Covers: non-positive wave limits are refused."""
    with pytest.raises(WaveScheduleError):
        schedule_chained_waves(
            (_plan("p1", _row("p1", "t1")),),
            FrontierLimits(active_projects=0, child_slots_per_project=5),
        )


def test_wave_empty_input_schedules_nothing() -> None:
    """Covers: empty plan input yields zero waves (legitimate null schedule)."""
    assert schedule_chained_waves(()) == ()


def test_wave_coordination_rows_ignored() -> None:
    """Covers: plan/wave wrappers schedule nothing and block nothing."""
    row = PlanTask(task_id="w1", phase="P1", status="pending", kind="wave_wrapper")
    waves = schedule_chained_waves((_plan("p1", row, _row("p1", "t1")),))
    assert [[task.task_id for task in wave.tasks] for wave in waves] == [["t1"]]


def test_integration_prerequisites_never_block_waves() -> None:
    """Covers: deferred INTEGRATE edges never move a task to a later wave."""
    waves = schedule_chained_waves(
        (_plan("p1", _row("p1", "t1", integrate=(TaskRef("p1", "t2"),)), _row("p1", "t2")),)
    )
    assert len(waves) == 1


# ---------------------------------------------------------------------------
# Run reports
# ---------------------------------------------------------------------------


def test_run_report_sections_mirror_projection() -> None:
    """Covers: eligible/running/held sections mirror the projection exactly."""
    from saberops.ready_frontier import FrontierSnapshot, SnapshotSource

    plans = (
        ProjectPlan(
            project_id="alpha-1",
            tasks=(
                _row("alpha-1", "done", status="completed"),
                _row("alpha-1", "work", status="running"),
                _row("alpha-1", "next", start=(TaskRef("alpha-1", "work"),)),
            ),
        ),
    )

    full = FrontierSnapshot(
        owner_id="owner-1", source=SnapshotSource.OWNER_DB.value, complete=True, projects=plans
    )
    projection = project_ready_frontier(FrontierRequest(snapshot=full))
    assert projection.projected is True
    report = build_plan_run_report(
        projection, plan_version="v3", generated_at="2026-10-18T00:00:00+00:00", wave_count=2
    )
    assert report.status == "PROJECTED"
    assert [(ref.project_id, ref.task_id) for ref in report.eligible] == []
    assert [(ref.project_id, ref.task_id) for ref in report.running] == [("alpha-1", "work")]
    held_ids = {(item.project_id, item.task_id) for item in report.held}
    assert ("alpha-1", "next") in held_ids
    payload = report.to_dict()
    json.dumps(payload)
    assert payload["plan_version"] == "v3"
    assert payload["wave_count"] == 2
    assert payload["limits"] == {"active_projects": 3, "child_slots_per_project": 5}


def test_run_report_denied_projection_reports_denial() -> None:
    """Covers: a DENIED projection reports denial, never an empty ready set."""
    from saberops.ready_frontier import FrontierSnapshot, SnapshotSource

    snapshot = FrontierSnapshot(
        owner_id="", source=SnapshotSource.UNKNOWN.value, complete=False, projects=()
    )
    projection = project_ready_frontier(FrontierRequest(snapshot=snapshot))
    assert projection.projected is False
    report = build_plan_run_report(
        projection, plan_version="v1", generated_at="2026-10-18T00:00:00+00:00"
    )
    assert report.status == "DENIED"
    assert report.eligible == () and report.held == ()
    assert "unknown_snapshot_source" in report.detail


# ---------------------------------------------------------------------------
# CLI: plan show / validate
# ---------------------------------------------------------------------------


def _cli(*argv: str, capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    """Covers: CLI tests drive the real entry point with an explicit store."""
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_cli_plan_show_and_validate(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Covers: orch plan show|validate end-to-end over the file-backed store."""
    store_root = str(tmp_path / "plans")
    store = PlanStore(store_root)
    store.save_revision(
        "alpha-1", [_task("t1"), _task("t2", start=("t1",))], created_at="2026-10-18T00:00:00+00:00"
    )
    store.set_active("alpha-1", "v1")

    code, out, _ = _cli(
        "plan", "show", "--project", "alpha-1", "--store", store_root, "--json", capsys=capsys
    )
    assert code == 0
    payload: dict[str, Any] = json.loads(out)
    assert payload["version"] == "v1"
    assert [task["task_id"] for task in payload["tasks"]] == ["t1", "t2"]

    code, out, _ = _cli(
        "plan", "validate", "--project", "alpha-1", "--store", store_root, "--json", capsys=capsys
    )
    assert code == 0
    assert json.loads(out) == {"errors": [], "ok": True}


def test_cli_plan_show_missing_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Covers: showing a plan with no active revision exits 1."""
    code, _, err = _cli(
        "plan", "show", "--project", "ghost", "--store", str(tmp_path / "plans"), capsys=capsys
    )
    assert code == 1
    assert "no active plan revision" in err


def test_cli_plan_validate_tampered_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Covers: validating a tampered revision exits 2 with errors."""
    store_root = tmp_path / "plans"
    store = PlanStore(store_root)
    store.save_revision("alpha-1", [_task("t1")], created_at="2026-10-18T00:00:00+00:00")
    (store_root / "alpha-1" / "v1.json").write_text("{broken", encoding="utf-8")
    code, _, _ = _cli(
        "plan",
        "validate",
        "--project",
        "alpha-1",
        "--store",
        str(store_root),
        "--json",
        capsys=capsys,
    )
    assert code == 2


def test_cli_plan_requires_run_id_or_subcommand() -> None:
    """Covers: bare orch plan (no run_id, no subcommand) still exits 2."""
    with pytest.raises(SystemExit) as excinfo:
        main(["plan"])
    assert excinfo.value.code == 2


def test_cli_plan_legacy_run_id_still_works(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: orch plan <run_id> keeps its legacy contract (no run -> legacy)."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("SABEROPS_PROFILE", raising=False)
    code, out, _ = _cli("plan", "run_deadbeef01", capsys=capsys)
    assert code == 0
    assert json.loads(out)["legacy"] is True
