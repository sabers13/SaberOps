"""ADR-0010 full-gate receipt construction and recording."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from saberops.config import RunConfig
from saberops.db import Database
from saberops.gate_receipt import (
    FULL_GATE_RECEIPT_EVENT,
    build_receipt,
    find_valid_receipt,
    gate_configuration_digest,
    receipt_matches,
    run_config_for_receipt,
)
from saberops.models import Run, RunStatus, Tier
from saberops.orchestrator import _record_full_gate_receipt
from saberops.process import ProcessResult

_NOW = datetime(2026, 11, 19, 12, 0, 0, tzinfo=UTC)


def _config(**changes: object) -> RunConfig:
    base = RunConfig(
        task="receipt test",
        target_repo="/tmp/repo",
        gate_command="bash scripts/gate-full.sh",
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def _run(db: Database, config: RunConfig, run_id: str = "run_receipt") -> Run:
    run = Run(
        id=run_id,
        task=config.task,
        target_repo=config.target_repo,
        base_commit="a" * 40,
        tier=Tier.T1,
        training_allowed=True,
        gate_command=config.gate_command,
        status=RunStatus.COMPLETED,
        created_at=_NOW.isoformat(),
        completed_at=_NOW.isoformat(),
        config_json=json.dumps(config.as_dict()),
    )
    db.create_run(run)
    return run


def _passing(duration: float = 1.0) -> ProcessResult:
    return ProcessResult(
        args=["gate"], exit_code=0, stdout="", stderr="", duration_seconds=duration
    )


def test_configuration_digest_binds_every_gate_field() -> None:
    base = _config()
    digest = gate_configuration_digest(base)
    assert digest == gate_configuration_digest(_config())
    for change in (
        {"gate_command": "make gate"},
        {"gate_enabled": False},
        {"gate_timeout": 9.0},
    ):
        assert gate_configuration_digest(_config(**change)) != digest


def test_receipt_matches_only_the_exact_base_candidate_command_and_config() -> None:
    config = _config()
    receipt = build_receipt(
        config=config,
        base_sha="a" * 40,
        candidate_sha="b" * 40,
        started_at=_NOW.isoformat(),
        finished_at=(_NOW + timedelta(seconds=1)).isoformat(),
        passed=True,
    )
    digest = gate_configuration_digest(config)
    assert receipt_matches(
        receipt,
        base_sha="a" * 40,
        candidate_sha="b" * 40,
        gate_command=config.gate_command,
        config_digest=digest,
        now=_NOW + timedelta(hours=1),
    )
    assert not receipt_matches(
        receipt,
        base_sha="c" * 40,
        candidate_sha="b" * 40,
        gate_command=config.gate_command,
        config_digest=digest,
        now=_NOW,
    )
    assert not receipt_matches(
        receipt,
        base_sha="a" * 40,
        candidate_sha="d" * 40,
        gate_command=config.gate_command,
        config_digest=digest,
        now=_NOW,
    )
    assert not receipt_matches(
        receipt,
        base_sha="a" * 40,
        candidate_sha="b" * 40,
        gate_command="make gate",
        config_digest=digest,
        now=_NOW,
    )
    assert not receipt_matches(
        receipt,
        base_sha="a" * 40,
        candidate_sha="b" * 40,
        gate_command=config.gate_command,
        config_digest="0" * 64,
        now=_NOW,
    )
    assert not receipt_matches(
        receipt,
        base_sha="a" * 40,
        candidate_sha="b" * 40,
        gate_command=config.gate_command,
        config_digest=digest,
        now=_NOW + timedelta(hours=25),
    )


def test_failed_or_skipped_gate_records_no_receipt(tmp_path: Path) -> None:
    db = Database(tmp_path / "runs.db")
    config = _config()
    _run(db, config)
    _record_full_gate_receipt(
        db,
        run_id="run_receipt",
        attempt_id="a1",
        config=config,
        base_sha="a" * 40,
        candidate_sha="b" * 40,
        gate_result=ProcessResult(
            args=["gate"], exit_code=1, stdout="", stderr="", duration_seconds=1.0
        ),
    )
    _record_full_gate_receipt(
        db,
        run_id="run_receipt",
        attempt_id="a1",
        config=config,
        base_sha="a" * 40,
        candidate_sha="b" * 40,
        gate_result=ProcessResult(
            args=[], exit_code=0, stdout="", stderr="", duration_seconds=0.0, skipped=True
        ),
    )
    assert db.get_events_by_type("run_receipt", FULL_GATE_RECEIPT_EVENT) == []


def test_passing_gate_records_a_findable_receipt(tmp_path: Path) -> None:
    db = Database(tmp_path / "runs.db")
    config = _config()
    run = _run(db, config)
    _record_full_gate_receipt(
        db,
        run_id=run.id,
        attempt_id="a1",
        config=config,
        base_sha=run.base_commit,
        candidate_sha="b" * 40,
        gate_result=_passing(),
    )
    found = find_valid_receipt(
        db,
        run,
        candidate_sha="b" * 40,
        now=datetime.now(UTC),
    )
    assert found is not None
    assert found["candidate_sha"] == "b" * 40
    assert found["base_sha"] == run.base_commit
    assert found["gate_command"] == config.gate_command
    assert found["receipt_digest"]


def test_find_valid_receipt_reconstructs_the_frozen_config(tmp_path: Path) -> None:
    """A run row without a config payload still yields a stable digest."""
    db = Database(tmp_path / "runs.db")
    config = _config()
    run = _run(db, config)
    run_without_config = replace(run, config_json=None)
    assert run_config_for_receipt(run_without_config).gate_command == config.gate_command
    _record_full_gate_receipt(
        db,
        run_id=run.id,
        attempt_id="a1",
        config=config,
        base_sha=run.base_commit,
        candidate_sha="b" * 40,
        gate_result=_passing(),
    )
    assert find_valid_receipt(
        db, run_without_config, candidate_sha="b" * 40, now=datetime.now(UTC)
    ) is not None
