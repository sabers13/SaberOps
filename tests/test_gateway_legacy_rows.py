"""Legacy gateway rows remain readable after the gateway cut.

New runs and dispatches never write the gateway columns, but databases
written before the cut carry ``runs.gateway_snapshot_json/digest`` and
``dispatch_evidence.gateway_evidence_json``.  Those rows must still load,
and the observability snapshot must still surface them -- without the
``saberops.gateway`` package, which no longer exists.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

from saberops.db import Database, current_iso_timestamp
from saberops.models import (
    DispatchReason,
    DispatchRole,
    DispatchStage,
    Run,
    RunStatus,
    Tier,
)
from saberops.observability import build_run_observability_snapshot, new_dispatch_evidence

LEGACY_SNAPSHOT = {"schema_version": 1, "digest": "legacy-digest", "kind": "OMNIROUTE"}
LEGACY_EVIDENCE = {"selection": {"requested_account_id": "acct-1"}, "status_code": 200}


def _legacy_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "legacy.db")
    run = Run(
        id="run-legacy",
        task="legacy task",
        target_repo=str(tmp_path),
        base_commit="abc123",
        tier=Tier.T1,
        training_allowed=True,
        gate_command="make gate",
        status=RunStatus.RUNNING,
        created_at=current_iso_timestamp(),
        config_json=json.dumps({"task": "legacy task"}),
    )
    db.create_run(run)
    with sqlite3.connect(db.db_path) as conn:
        conn.execute(
            "UPDATE runs SET gateway_snapshot_json = ?, gateway_snapshot_digest = ?"
            " WHERE id = ?",
            (json.dumps(LEGACY_SNAPSHOT), "legacy-digest", "run-legacy"),
        )
        conn.commit()
    evidence = new_dispatch_evidence(
        run_id="run-legacy",
        association_id="attempt-1",
        attempt_id="attempt-1",
        stage=DispatchStage.IMPLEMENTATION,
        role=DispatchRole.BULK,
        tier=Tier.T1,
        dispatch_reason=DispatchReason.INITIAL_IMPLEMENTATION,
        requested_provider="codex",
        requested_model="gpt-6.1-sol",
        task="legacy task",
        base_sha="abc123",
    )
    db.create_dispatch_evidence(evidence)
    with sqlite3.connect(db.db_path) as conn:
        conn.execute(
            "UPDATE dispatch_evidence SET gateway_evidence_json = ? WHERE id = ?",
            (json.dumps(LEGACY_EVIDENCE), evidence.id),
        )
        conn.commit()
    return db


def test_legacy_run_with_gateway_snapshot_still_loads(tmp_path: Path) -> None:
    db = _legacy_db(tmp_path)
    run = db.get_run("run-legacy")
    assert run is not None
    assert run.gateway_snapshot_json is not None
    assert json.loads(run.gateway_snapshot_json)["digest"] == "legacy-digest"
    assert run.gateway_snapshot_digest == "legacy-digest"
    rows = db.get_dispatch_evidence_for_run("run-legacy")
    assert len(rows) == 1
    assert json.loads(rows[0].gateway_evidence_json or "{}")["status_code"] == 200


def test_legacy_gateway_evidence_still_surfaces_in_snapshot(tmp_path: Path) -> None:
    db = _legacy_db(tmp_path)
    snapshot = build_run_observability_snapshot(db, "run-legacy")
    assert snapshot["frozen_gateway"] is not None
    assert snapshot["frozen_gateway"]["digest"] == "legacy-digest"
    assert snapshot["gateway_evidence"] == [LEGACY_EVIDENCE]


def test_new_rows_write_no_gateway_columns(tmp_path: Path) -> None:
    db = Database(tmp_path / "fresh.db")
    run = Run(
        id="run-fresh",
        task="fresh task",
        target_repo=str(tmp_path),
        base_commit="abc123",
        tier=Tier.T1,
        training_allowed=True,
        gate_command="make gate",
        status=RunStatus.RUNNING,
        created_at=current_iso_timestamp(),
        config_json=json.dumps({"task": "fresh task"}),
    )
    db.create_run(run)
    fresh = db.get_run("run-fresh")
    assert fresh is not None
    assert fresh.gateway_snapshot_json is None
    assert fresh.gateway_snapshot_digest is None
    with sqlite3.connect(db.db_path) as conn:
        row = conn.execute(
            "SELECT gateway_snapshot_json, gateway_snapshot_digest FROM runs WHERE id = ?",
            ("run-fresh",),
        ).fetchone()
    assert row[0] is None and row[1] is None


def test_gateway_package_is_gone() -> None:
    assert importlib.util.find_spec("saberops.gateway") is None


def test_legacy_run_config_with_gateway_keys_still_reconstructs() -> None:
    from saberops.config import reconstruct_run_config

    config = reconstruct_run_config(
        {
            "task": "legacy",
            "target_repo": "/tmp/repo",
            "gateway_frozen_config": {"kind": "OMNIROUTE"},
            "gateway_frozen_digest": "legacy-digest",
        }
    )
    assert config.task == "legacy"
    assert not hasattr(config, "gateway_frozen_config")
    assert not hasattr(config, "gateway_frozen_digest")
