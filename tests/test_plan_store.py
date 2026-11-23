"""Hermetic tests for the versioned plan store.

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
    FrontierReason,
    FrontierRequest,
    TaskRef,
    project_ready_frontier,
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


def test_cli_plan_validate_all_empty_store_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Covers: validating --all with no stored plans exits 2 without traceback."""
    code, _, err = _cli(
        "plan", "validate", "--all", "--store", str(tmp_path / "plans"), "--json", capsys=capsys
    )
    assert code == 2


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
