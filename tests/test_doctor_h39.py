"""H39 self-healing doctor: check, repair only fixable, recheck decides.

Every fix has a fake-mode test (no vendor CLI, no network) and a companion
that proves a repair which did not work leaves the recheck still reporting the
fault.  All state lives under the test's own temporary XDG roots.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from saberops import doctor
from saberops.cli import handle_doctor
from saberops.db import Database
from saberops.mcp.lease import OrchestratorLease, default_lease_path
from saberops.mcp.tools import ServerContext, build_default_registry
from saberops.owner_mode import set_owner_mode


def _now() -> str:
    return datetime.now(UTC).isoformat()


@pytest.fixture
def scratch_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point legacy-state discovery at a scratch home, never the owner's."""
    home = tmp_path / "home"
    home.mkdir()
    for variable, name in (
        ("XDG_STATE_HOME", "state"),
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_CACHE_HOME", "cache"),
    ):
        directory = tmp_path / name
        directory.mkdir()
        monkeypatch.setenv(variable, str(directory))
    monkeypatch.setattr(doctor, "_home_dir", lambda: home)
    monkeypatch.setenv("SABEROPS_MCP_LEASE", str(tmp_path / "planner.lock"))
    return home


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "owner.db"


def _filtered(check_id: str, repo: Path, db_path: Path) -> Any:
    def run() -> list[doctor.DoctorFinding]:
        return [
            finding
            for finding in doctor.collect_doctor_checks(repo, db_path, timeout=5)
            if finding.check_id == check_id
        ]

    return run


def _repair(report: doctor.DoctorReport, check_id: str) -> doctor.RepairAction:
    for action in report.actions:
        if action.check_id == check_id:
            return action
    raise AssertionError(f"no action for {check_id}: {report.actions}")


# --------------------------------------------------------------------------- #
# One fix per SaberOps-owned area, plus the failed-repair companion
# --------------------------------------------------------------------------- #


def test_state_dirs_fix_creates_owner_directories(
    scratch_home: Path, tmp_path: Path, db_path: Path
) -> None:
    from saberops.paths import saberops_cache_dir, saberops_config_dir, saberops_state_dir

    for path in (saberops_state_dir(), saberops_config_dir(), saberops_cache_dir()):
        assert not path.exists()
    report = doctor.run_doctor(
        tmp_path,
        db_path,
        fix=True,
        only=["state_dirs"],
        checks=_filtered("state_dirs", tmp_path, db_path),
    )
    assert not report.remaining_fixable
    assert report.findings and all(f.state == "ok" for f in report.findings)
    assert _repair(report, "state_dirs").after == "ok"
    assert saberops_state_dir().is_dir()
    assert saberops_config_dir().is_dir()
    assert saberops_cache_dir().is_dir()


def test_state_dirs_recheck_still_reports_when_fix_did_not_work(
    scratch_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db_path: Path
) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(doctor, "_state_directories", lambda: (blocker / "child",))
    report = doctor.run_doctor(
        tmp_path,
        db_path,
        fix=True,
        only=["state_dirs"],
        checks=_filtered("state_dirs", tmp_path, db_path),
    )
    assert report.remaining_fixable
    assert _repair(report, "state_dirs").after == "fixable"


def test_database_fix_rebuilds_a_missing_index(scratch_home: Path, tmp_path: Path) -> None:
    path = tmp_path / "owner.db"
    Database(path)
    with sqlite3.connect(path) as conn:
        conn.execute("DROP INDEX idx_attempts_run_id")
        conn.commit()
    report = doctor.run_doctor(
        tmp_path,
        path,
        fix=True,
        only=["database"],
        checks=_filtered("database", tmp_path, path),
    )
    assert not report.remaining_fixable
    with sqlite3.connect(path) as conn:
        names = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
    assert "idx_attempts_run_id" in names


def test_database_recheck_still_reports_when_fix_did_not_work(
    scratch_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "owner.db"
    Database(path)
    with sqlite3.connect(path) as conn:
        conn.execute("DROP INDEX idx_attempts_run_id")
        conn.commit()

    def noop(ctx: doctor.RepairContext) -> doctor.RepairOutcome:
        return doctor.RepairOutcome(applied=False, detail="not attempted")

    monkeypatch.setitem(doctor._REPAIRS, "database", noop)
    report = doctor.run_doctor(
        tmp_path,
        path,
        fix=True,
        only=["database"],
        checks=_filtered("database", tmp_path, path),
    )
    assert report.remaining_fixable
    assert _repair(report, "database").after == "fixable"


def test_planner_lease_fix_clears_only_a_dead_holder(
    scratch_home: Path, tmp_path: Path, db_path: Path
) -> None:
    lease_path = default_lease_path()
    record = OrchestratorLease(lease_path, pid=999_999)
    lease_path.parent.mkdir(parents=True, exist_ok=True)
    lease_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "holder": "dead-planner",
                "pid": 999_999,
                "acquired_at": _now(),
                "session_id": "",
                "epoch": 1,
                "last_activity": 0.0,
                "db_path": "",
                "heartbeat_at": 0.0,
            }
        ),
        encoding="utf-8",
    )
    assert record.read() is not None
    report = doctor.run_doctor(
        tmp_path,
        db_path,
        fix=True,
        only=["planner_lease"],
        checks=_filtered("planner_lease", tmp_path, db_path),
    )
    assert not report.remaining_fixable
    assert lease_path.stat().st_size == 0


def test_planner_lease_recheck_still_reports_when_fix_did_not_work(
    scratch_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db_path: Path
) -> None:
    lease_path = default_lease_path()
    lease_path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(
        {
            "schema_version": 1,
            "holder": "dead-planner",
            "pid": 999_999,
            "acquired_at": _now(),
            "session_id": "",
        }
    )
    lease_path.write_text(raw, encoding="utf-8")

    def noop(ctx: doctor.RepairContext) -> doctor.RepairOutcome:
        return doctor.RepairOutcome(applied=False, detail="not attempted")

    monkeypatch.setitem(doctor._REPAIRS, "planner_lease", noop)
    report = doctor.run_doctor(
        tmp_path,
        db_path,
        fix=True,
        only=["planner_lease"],
        checks=_filtered("planner_lease", tmp_path, db_path),
    )
    assert report.remaining_fixable
    assert lease_path.read_text(encoding="utf-8") == raw


def _seed_dead_owner_run(path: Path) -> str:
    Database(path)
    run_id = "run-dead-owner"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO runs (id, task, target_repo, base_commit, tier, "
            "training_allowed, gate_command, status, created_at) "
            "VALUES (?, 'task', ?, '0'*40, 'T1', 0, 'true', 'RUNNING', ?)",
            (run_id, str(path.parent), _now()),
        )
        conn.execute(
            "INSERT INTO execution_owners (run_id, owner_id, generation, state, "
            "host, pid, claimed_at) VALUES (?, 'owner', 1, 'CLAIMED', 'host', "
            "999999, ?)",
            (run_id, _now()),
        )
        conn.commit()
    return run_id


def test_orphaned_runs_fix_uses_supervisor_reconciliation(
    scratch_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "owner.db"
    run_id = _seed_dead_owner_run(path)

    def fake_reconcile(db_file: Path, run_ids: list[str]) -> list[str]:
        with sqlite3.connect(db_file) as conn:
            for item in run_ids:
                conn.execute("UPDATE runs SET status='ORPHANED' WHERE id=?", (item,))
            conn.commit()
        return list(run_ids)

    monkeypatch.setattr(doctor, "_reconcile_dead_runs", fake_reconcile)
    report = doctor.run_doctor(
        tmp_path,
        path,
        fix=True,
        only=["orphaned_runs"],
        checks=_filtered("orphaned_runs", tmp_path, path),
    )
    assert not report.remaining_fixable
    with sqlite3.connect(path) as conn:
        state = conn.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()[0]
    assert state == "ORPHANED"


def test_orphaned_runs_recheck_still_reports_when_fix_did_not_work(
    scratch_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "owner.db"
    _seed_dead_owner_run(path)

    def noop_reconcile(db_file: Path, run_ids: list[str]) -> list[str]:
        return list(run_ids)

    monkeypatch.setattr(doctor, "_reconcile_dead_runs", noop_reconcile)
    report = doctor.run_doctor(
        tmp_path,
        path,
        fix=True,
        only=["orphaned_runs"],
        checks=_filtered("orphaned_runs", tmp_path, path),
    )
    assert report.remaining_fixable
    assert _repair(report, "orphaned_runs").after == "fixable"


def _queued_store(path: Path) -> Path:
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE jobs (job_id TEXT PRIMARY KEY, state TEXT NOT NULL)")
        conn.execute("INSERT INTO jobs VALUES ('job-1', 'QUEUED')")
        conn.commit()
    return path


def test_scheduler_fix_starts_a_detached_scheduler(
    scratch_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _queued_store(tmp_path / "owner.db")
    state = {"alive": False}

    def alive(db_file: Path) -> bool:
        return state["alive"]

    def start(db_file: Path) -> bool:
        state["alive"] = True
        return True

    monkeypatch.setattr(doctor, "_scheduler_alive", alive)
    monkeypatch.setattr(doctor, "_start_scheduler", start)
    report = doctor.run_doctor(
        tmp_path,
        path,
        fix=True,
        only=["scheduler"],
        checks=_filtered("scheduler", tmp_path, path),
    )
    assert not report.remaining_fixable
    assert state["alive"]


def test_scheduler_recheck_still_reports_when_fix_did_not_work(
    scratch_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _queued_store(tmp_path / "owner.db")
    monkeypatch.setattr(doctor, "_scheduler_alive", lambda db_file: False)
    monkeypatch.setattr(doctor, "_start_scheduler", lambda db_file: False)
    report = doctor.run_doctor(
        tmp_path,
        path,
        fix=True,
        only=["scheduler"],
        checks=_filtered("scheduler", tmp_path, path),
    )
    assert report.remaining_fixable
    assert _repair(report, "scheduler").after == "fixable"


def test_legacy_state_is_preview_only_until_explicitly_deleted(
    scratch_home: Path, tmp_path: Path, db_path: Path
) -> None:
    legacy = scratch_home / ".local" / "state" / "orchestrator-mvp"
    legacy.mkdir(parents=True)
    (legacy / "old.db").write_text("x", encoding="utf-8")

    report = doctor.run_doctor(
        tmp_path,
        db_path,
        fix=True,
        only=["legacy_state"],
        checks=_filtered("legacy_state", tmp_path, db_path),
    )
    assert legacy.exists()
    assert report.remaining_fixable
    assert _repair(report, "legacy_state").applied is False

    report = doctor.run_doctor(
        tmp_path,
        db_path,
        fix=True,
        only=["legacy_state"],
        allow_legacy_delete=True,
        checks=_filtered("legacy_state", tmp_path, db_path),
    )
    assert not legacy.exists()
    assert not report.remaining_fixable


def test_legacy_state_recheck_still_reports_when_fix_did_not_work(
    scratch_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
) -> None:
    legacy = scratch_home / ".local" / "state" / "orchestrator-mvp"
    legacy.mkdir(parents=True)
    (legacy / "old.db").write_text("x", encoding="utf-8")

    def noop_rmtree(path: str | os.PathLike[str], *args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(shutil, "rmtree", noop_rmtree)
    report = doctor.run_doctor(
        tmp_path,
        db_path,
        fix=True,
        only=["legacy_state"],
        allow_legacy_delete=True,
        checks=_filtered("legacy_state", tmp_path, db_path),
    )
    assert legacy.exists()
    assert report.remaining_fixable
    assert _repair(report, "legacy_state").after == "fixable"


# --------------------------------------------------------------------------- #
# Read-only, bounds and portability
# --------------------------------------------------------------------------- #


def test_cli_doctor_without_fix_stays_read_only_and_fix_repairs(
    scratch_home: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    fake_vendor_cli: Callable[[str], Path],
) -> None:
    import argparse

    from saberops.paths import saberops_state_dir

    # The doctor fails when no worker provider is AVAILABLE.  Supply a stub
    # provider CLI rather than depending on the host having one installed.
    fake_vendor_cli("opencode")
    db_file = tmp_path / "owner.db"
    args = argparse.Namespace(
        repo=str(Path.cwd()), db=str(db_file), fix=False, json=False, delete_legacy=False
    )
    handle_doctor(args)
    assert not saberops_state_dir().exists()
    assert "fixable" in capsys.readouterr().out

    args.fix = True
    assert handle_doctor(args) == 0
    assert saberops_state_dir().is_dir()
    assert "state_dirs" in capsys.readouterr().out


def test_plain_doctor_is_strictly_read_only(
    scratch_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "owner.db"
    Database(path)
    with sqlite3.connect(path) as conn:
        conn.execute("DROP INDEX idx_attempts_run_id")
        conn.commit()
    lease_path = default_lease_path()
    lease_path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(
        {
            "schema_version": 1,
            "holder": "dead-planner",
            "pid": 999_999,
            "acquired_at": _now(),
        }
    )
    lease_path.write_text(raw, encoding="utf-8")
    legacy = scratch_home / ".config" / "orchestrator-v2"
    legacy.mkdir(parents=True)
    before = {
        "lease": lease_path.read_text(encoding="utf-8"),
        "legacy": legacy.exists(),
        "db_hash": path.read_bytes(),
    }

    report = doctor.run_doctor(
        tmp_path,
        path,
        fix=False,
        checks=lambda: [
            finding
            for finding in doctor.collect_doctor_checks(tmp_path, path, timeout=5)
            if finding.check_id in {"state_dirs", "database", "planner_lease", "legacy_state"}
        ],
    )
    assert report.remaining_fixable
    assert report.actions == ()
    assert lease_path.read_text(encoding="utf-8") == before["lease"]
    assert legacy.exists() == before["legacy"]
    assert path.read_bytes() == before["db_hash"]


def test_every_check_is_time_bounded(
    scratch_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def slow(ctx: doctor.RepairContext) -> list[doctor.DoctorFinding]:
        time.sleep(5)
        return [doctor.DoctorFinding.passed("slow", "finished")]

    monkeypatch.setattr(doctor, "_CHECKS", (("slow", slow),))
    started = time.monotonic()
    findings = doctor.collect_doctor_checks(tmp_path, tmp_path / "x.db", timeout=0.05)
    assert time.monotonic() - started < 3
    assert findings and findings[0].state == "human"
    assert "did not finish" in findings[0].message


def test_platform_branches_report_skip_on_an_unsupported_os(
    scratch_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    findings = doctor.collect_doctor_checks(tmp_path, tmp_path / "x.db", timeout=5)
    portability = [item for item in findings if item.check_id == "portability"]
    assert portability and portability[0].state == "skip"


def test_wsl_is_recognised_from_the_kernel_banner(
    scratch_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(doctor, "_proc_version_text", lambda: "microsoft-standard-WSL2")
    assert doctor.detect_platform() == "wsl"
    findings = doctor.collect_doctor_checks(tmp_path, tmp_path / "x.db", timeout=5)
    portability = [item for item in findings if item.check_id == "portability"]
    assert portability and portability[0].state == "ok"
    assert "wsl" in portability[0].message


# --------------------------------------------------------------------------- #
# CLI, web page and MCP report the same findings; owner mode governs repair
# --------------------------------------------------------------------------- #


def _expected_pairs(repo: Path, db_path: Path) -> set[tuple[str, str]]:
    return {
        (finding.check_id, finding.state)
        for finding in doctor.collect_all(repo, db_path)
    }


def test_cli_web_and_mcp_report_the_same_findings(
    scratch_home: Path, tmp_path: Path
) -> None:
    import argparse

    db_file = tmp_path / "owner.db"
    Database(db_file)
    repo = tmp_path

    client = TestClient(
        __import__("saberops.web", fromlist=["create_app"]).create_app(
            db_path=db_file, repo_path=repo
        ),
        raise_server_exceptions=False,
    )
    # The app opens (and therefore migrates) the store on creation.  Create the
    # fault after it, so CLI, web and MCP all read the same state.
    with sqlite3.connect(db_file) as conn:
        conn.execute("DROP INDEX idx_reviews_run_id")
        conn.commit()
    expected = _expected_pairs(repo, db_file)

    registry = build_default_registry(
        ServerContext(db_path=db_file, repo=repo, lease=OrchestratorLease())
    )
    mcp = registry.call("doctor", {})
    mcp_pairs = {(item["check_id"], item["state"]) for item in mcp["findings"]}
    assert mcp_pairs == expected

    args = argparse.Namespace(repo=str(repo), db=str(db_file), fix=False, json=True)
    assert handle_doctor(args) in (0, 1)

    response = client.get("/doctor")
    assert response.status_code == 200
    import re

    web_pairs = {
        (match.group(1), match.group(2))
        for match in re.finditer(
            r'data-check-id="([^"]+)" data-state="([^"]+)"', response.text
        )
    }
    assert web_pairs == expected


def test_doctor_fix_asks_the_owner_in_ask_me(
    scratch_home: Path, tmp_path: Path
) -> None:
    lease = OrchestratorLease(default_lease_path())
    assert lease.acquire()
    context = ServerContext(
        db_path=tmp_path / "owner.db", repo=tmp_path, lease=lease
    )
    registry = build_default_registry(context)
    payload = registry.call("doctor_fix", {"checks": ["state_dirs"]})
    assert payload["applied"] is False
    assert payload["question"]["kind"] == "doctor_fix"
    from saberops.paths import saberops_config_dir

    # The question itself writes under state; no repair ran, so config and
    # cache stay exactly as they were.
    assert not saberops_config_dir().exists()
    assert any(item["state"] == "fixable" for item in payload["findings"])


def test_doctor_fix_applies_safe_repairs_away(
    scratch_home: Path, tmp_path: Path
) -> None:
    set_owner_mode("away", max_jobs=1)
    lease = OrchestratorLease(default_lease_path())
    assert lease.acquire()
    context = ServerContext(
        db_path=tmp_path / "owner.db", repo=tmp_path, lease=lease
    )
    registry = build_default_registry(context)
    payload = registry.call("doctor_fix", {"checks": ["state_dirs"]})
    assert payload["applied"] is True
    assert payload["mode"] == "away"
    from saberops.paths import saberops_state_dir

    assert saberops_state_dir().is_dir()
    assert not [item for item in payload["findings"] if item["state"] == "fixable"]


# --------------------------------------------------------------------------- #
# Mutation-testing gaps (H46): one fix drill, filters and action logging
# --------------------------------------------------------------------------- #


def _finding(check_id: str, status: str = "fixable") -> doctor.DoctorFinding:
    return doctor.DoctorFinding(
        severity="FAIL", message=f"{check_id} is broken", check_id=check_id, status=status
    )


def test_fix_drill_records_every_repair_and_a_raising_one(
    scratch_home: Path, tmp_path: Path, db_path: Path
) -> None:
    """Covers: two findings, one raising repair, both recorded and logged."""
    findings = [_finding("alpha"), _finding("beta")]

    def repair_alpha(_: doctor.RepairContext) -> doctor.RepairOutcome:
        return doctor.RepairOutcome(applied=True, detail="fixed")

    def repair_beta(_: doctor.RepairContext) -> doctor.RepairOutcome:
        raise RuntimeError("boom")

    report = doctor.run_doctor(
        tmp_path,
        db_path,
        fix=True,
        checks=lambda: findings,
        repairs={"alpha": repair_alpha, "beta": repair_beta},
    )
    assert report.fix_requested is True
    assert [action.check_id for action in report.actions] == ["alpha", "beta"]
    assert _repair(report, "alpha").applied is True
    beta = _repair(report, "beta")
    assert beta.applied is False
    assert "repair raised: boom" in beta.detail
    log = doctor._action_log_path().read_text(encoding="utf-8").splitlines()
    assert len(log) == 2


def test_plain_run_does_not_request_fixes(
    scratch_home: Path, tmp_path: Path, db_path: Path
) -> None:
    """Covers: a read-only run reports fix_requested=False and no actions."""
    report = doctor.run_doctor(tmp_path, db_path, checks=lambda: [_finding("alpha")])
    assert report.fix_requested is False
    assert report.actions == ()


def test_fix_skips_non_fixable_duplicate_and_unmapped_check_ids(
    scratch_home: Path, tmp_path: Path, db_path: Path
) -> None:
    """Covers: the fix loop filters, deduplicates and continues past a skip."""
    findings = [
        _finding("alpha"),
        _finding("alpha"),
        _finding("unmapped"),
        _finding("human", status="human"),
        _finding("beta"),
    ]

    def applied(_: doctor.RepairContext) -> doctor.RepairOutcome:
        return doctor.RepairOutcome(applied=True, detail="fixed")

    report = doctor.run_doctor(
        tmp_path,
        db_path,
        fix=True,
        checks=lambda: findings,
        repairs={"alpha": applied, "human": applied, "beta": applied},
    )
    assert [action.check_id for action in report.actions] == ["alpha", "beta"]
