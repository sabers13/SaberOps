"""Run configuration: per-role worker timeouts and initial routing role."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from saberops.config import (
    MAX_CONCURRENT_TASKS,
    MAX_WORKER_SESSIONS_PER_PROJECT,
    ROLE_WORKER_TIMEOUTS,
    WORKFLOW_SETTINGS_VERSION,
    ConfigError,
    ConfigLedger,
    RunConfig,
    WorkflowSettings,
    approve_config_proposal,
    config_ledger_path,
    current_workflow_settings,
    default_workflow_settings,
    propose_config_change,
    reconstruct_run_config,
    record_cleanup_decision,
    resolve_run_config,
    role_worker_timeout,
    validate_workflow_settings,
)
from saberops.jobs.models import JobRole
from saberops.models import Tier


def _config(tmp_path: Path, **kwargs: Any) -> RunConfig:
    return resolve_run_config(
        task="fix typo in readme",
        target_repo=tmp_path,
        publish_candidate=False,
        **kwargs,
    )


def test_role_worker_timeout_table() -> None:
    """Covers: per-role worker timeouts (replacing TIER_WORKER_TIMEOUTS)."""
    assert ROLE_WORKER_TIMEOUTS == {
        JobRole.WORKER: 1200.0,
        JobRole.SURGEON: 1800.0,
        JobRole.INTEGRATOR: 1200.0,
    }
    assert role_worker_timeout(JobRole.WORKER) == 1200.0
    assert role_worker_timeout(JobRole.SURGEON) == 1800.0
    assert role_worker_timeout(JobRole.INTEGRATOR) == 1200.0
    assert role_worker_timeout("worker") == 1200.0


def test_role_worker_timeout_rejects_unrouted_roles() -> None:
    """Covers: per-role worker timeouts (ORCHESTRATOR/REVIEWER have none)."""
    with pytest.raises(ValueError, match="no worker timeout"):
        role_worker_timeout(JobRole.ORCHESTRATOR)
    with pytest.raises(ValueError, match="no worker timeout"):
        role_worker_timeout(JobRole.REVIEWER)
    with pytest.raises(ValueError, match="no worker timeout"):
        role_worker_timeout("reviewer")


def test_explicit_worker_timeout_wins_and_degenerate_falls_back(tmp_path: Path) -> None:
    """Covers: explicit --worker-timeout wins; non-positive falls back."""
    config = _config(tmp_path, worker_timeout=42.0)
    assert config.worker_timeout_for_role(JobRole.WORKER) == 42.0
    assert config.worker_timeout_for_role(JobRole.SURGEON) == 42.0
    fallback = _config(tmp_path, worker_timeout=0.0)
    assert fallback.worker_timeout_for_role(JobRole.WORKER) == 1200.0
    assert fallback.worker_timeout_for_role(JobRole.SURGEON) == 1800.0
    default = _config(tmp_path)
    assert default.worker_timeout_for_role(JobRole.WORKER) == 1200.0
    assert default.worker_timeout_for_role(JobRole.INTEGRATOR) == 1200.0


def test_initial_role_for_auto_and_manual_tiers(tmp_path: Path) -> None:
    """Covers: RunConfig.initial_role (SURGEON iff manual T3, else WORKER)."""
    auto = _config(tmp_path)
    assert auto.initial_role == JobRole.WORKER
    for tier in (Tier.T1, Tier.T2):
        manual = _config(tmp_path, routing_mode="manual", manual_tier=tier)
        assert manual.initial_role == JobRole.WORKER
    manual_t3 = _config(tmp_path, routing_mode="manual", manual_tier=Tier.T3)
    assert manual_t3.initial_role == JobRole.SURGEON


def test_initial_role_survives_detached_reconstruction(tmp_path: Path) -> None:
    """Covers: RunConfig.initial_role (derived, needs no persisted field)."""
    for kwargs, expected in (
        ({"routing_mode": "manual", "manual_tier": Tier.T3}, JobRole.SURGEON),
        ({"routing_mode": "manual", "manual_tier": Tier.T1}, JobRole.WORKER),
        ({"max_auto_tier": Tier.T2}, JobRole.WORKER),
    ):
        config = _config(tmp_path, **kwargs)
        rebuilt = reconstruct_run_config(config.as_dict())
        assert rebuilt.as_dict() == config.as_dict()
        assert rebuilt.initial_role == expected


# ---------------------------------------------------------------------------
# CFG-1: central workflow configuration, approval and the audit ledger
# ---------------------------------------------------------------------------


def _ledger(tmp_path: Path) -> ConfigLedger:
    return ConfigLedger(config_ledger_path(tmp_path))


def test_workflow_settings_defaults_are_3x5(tmp_path: Path) -> None:
    """Covers: CFG-1 (concurrency defaults are the owner 3x5 ceilings)."""
    settings = default_workflow_settings()
    assert MAX_CONCURRENT_TASKS == 3
    assert MAX_WORKER_SESSIONS_PER_PROJECT == 5
    assert settings.max_concurrent_tasks == MAX_CONCURRENT_TASKS
    assert settings.max_worker_sessions_per_project == MAX_WORKER_SESSIONS_PER_PROJECT
    # The gate-slot budget always hosts the configured worker concurrency.
    assert settings.gate_light_slots_max >= settings.max_concurrent_tasks
    assert settings.gate_heavy_slots >= 1


def test_workflow_settings_fail_closed_on_illegal_values() -> None:
    """Covers: CFG-1 (owner approval is only recorded for valid settings)."""
    with pytest.raises(ConfigError, match="max_concurrent_tasks"):
        WorkflowSettings(max_concurrent_tasks=MAX_CONCURRENT_TASKS + 1)
    with pytest.raises(ConfigError, match="max_worker_sessions_per_project"):
        WorkflowSettings(max_worker_sessions_per_project=MAX_WORKER_SESSIONS_PER_PROJECT + 1)
    with pytest.raises(ConfigError, match="gate_light_slots_max"):
        WorkflowSettings(max_concurrent_tasks=3, gate_light_slots_max=2)
    with pytest.raises(ConfigError, match="gate_heavy_slots"):
        WorkflowSettings(gate_heavy_slots=0)
    with pytest.raises(ConfigError, match="evidence_other_retention_days"):
        WorkflowSettings(
            evidence_accepted_retention_days=30, evidence_other_retention_days=90
        )
    with pytest.raises(ConfigError, match="failed_scratch_cap_gib"):
        WorkflowSettings(failed_scratch_cap_gib=-1.0)


def test_workflow_settings_snapshot_roundtrip_and_frozen_hash() -> None:
    """Covers: CFG-1 (frozen-hash compatibility of a settings snapshot)."""
    settings = default_workflow_settings()
    snapshot = settings.as_dict()
    assert snapshot["version"] == WORKFLOW_SETTINGS_VERSION
    assert WorkflowSettings.from_dict(snapshot) == settings
    assert settings.frozen_hash() == default_workflow_settings().frozen_hash()
    with pytest.raises(ConfigError, match="unknown keys"):
        WorkflowSettings.from_dict({**snapshot, "bogus": 1})
    with pytest.raises(ConfigError, match="integer"):
        WorkflowSettings.from_dict({**snapshot, "max_concurrent_tasks": "three"})
    # The validator is idempotent and never mutates a valid object.
    validate_workflow_settings(settings)


def test_proposal_rejects_unknown_noop_and_illegal() -> None:
    """Covers: CFG-1 (proposals are validated before any owner approval)."""
    base = default_workflow_settings()
    with pytest.raises(ConfigError, match="unknown workflow setting"):
        propose_config_change(base, {"not_a_setting": 1}, rationale="r", proposed_by="orch")
    with pytest.raises(ConfigError, match="does not change"):
        propose_config_change(base, {"max_concurrent_tasks": 3}, rationale="r", proposed_by="orch")
    with pytest.raises(ConfigError, match="max_concurrent_tasks"):
        propose_config_change(base, {"max_concurrent_tasks": 9}, rationale="r", proposed_by="orch")


def test_proposal_approval_builds_supersedes_chain(tmp_path: Path) -> None:
    """Covers: CFG-1 (config-log entries link to the decision they supersede)."""
    ledger = _ledger(tmp_path)
    assert current_workflow_settings(ledger) == default_workflow_settings()
    first = propose_config_change(
        current_workflow_settings(ledger),
        {"max_concurrent_tasks": 2},
        rationale="trim concurrency for the merger",
        proposed_by="orch",
    )
    assert first.changed == ("max_concurrent_tasks",)
    d1 = approve_config_proposal(
        first, ledger=ledger, approved_by="owner", created_at="2026-01-01T00:00:00Z"
    )
    assert d1.supersedes is None
    assert d1.kind == "config"
    assert current_workflow_settings(ledger).max_concurrent_tasks == 2
    second = propose_config_change(
        current_workflow_settings(ledger),
        {"gate_heavy_slots": 2},
        rationale="bigger host",
        proposed_by="orch",
    )
    d2 = approve_config_proposal(
        second, ledger=ledger, approved_by="owner", created_at="2026-01-02T00:00:00Z"
    )
    assert d2.supersedes == d1.decision_id
    assert [entry.decision_id for entry in ledger.entries()] == [d1.decision_id, d2.decision_id]
    assert ledger.tip_for("config") == d2


def test_ledger_is_append_only_and_refuses_forks_and_replays(tmp_path: Path) -> None:
    """Covers: CFG-1 (immutable ledger: no duplicate, fork or replayed entry)."""
    ledger = _ledger(tmp_path)
    d1 = approve_config_proposal(
        propose_config_change(
            default_workflow_settings(),
            {"max_concurrent_tasks": 2},
            rationale="r",
            proposed_by="orch",
        ),
        ledger=ledger,
        approved_by="owner",
        created_at="2026-01-01T00:00:00Z",
    )
    approve_config_proposal(
        propose_config_change(
            current_workflow_settings(ledger),
            {"max_concurrent_tasks": 1},
            rationale="r",
            proposed_by="orch",
        ),
        ledger=ledger,
        approved_by="owner",
        created_at="2026-01-02T00:00:00Z",
    )
    # Replaying an already-recorded decision is refused.
    with pytest.raises(ConfigError, match="already recorded"):
        ledger.append(d1)
    # A second root for the same kind would fork the audit chain.
    forked = d1.__class__(
        created_at="2026-01-03T00:00:00Z",
        kind="config",
        subject="workflow-settings",
        value=default_workflow_settings().as_dict(),
        rationale="fork",
        approved_by="owner",
    ).with_id()
    with pytest.raises(ConfigError, match="already has a head"):
        ledger.append(forked)
    # Superseding a decision that is not the chain tip is refused.
    dangling = replace(d1, supersedes="0" * 64, created_at="2026-01-04T00:00:00Z").with_id()
    with pytest.raises(ConfigError, match="not in the ledger"):
        ledger.append(dangling)


def test_ledger_detects_tampered_entry(tmp_path: Path) -> None:
    """Covers: CFG-1 (a rewritten ledger line fails its content-hash check)."""
    ledger = _ledger(tmp_path)
    approve_config_proposal(
        propose_config_change(
            default_workflow_settings(),
            {"max_concurrent_tasks": 2},
            rationale="r",
            proposed_by="orch",
        ),
        ledger=ledger,
        approved_by="owner",
        created_at="2026-01-01T00:00:00Z",
    )
    path = config_ledger_path(tmp_path)
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            '"max_concurrent_tasks":2', '"max_concurrent_tasks":1'
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="content-hash"):
        ledger.entries()


def test_current_settings_detects_frozen_hash_mismatch(tmp_path: Path) -> None:
    """Covers: CFG-1 (a snapshot that no longer hashes to its record fails closed)."""
    ledger = _ledger(tmp_path)
    decision = approve_config_proposal(
        propose_config_change(
            default_workflow_settings(),
            {"max_concurrent_tasks": 2},
            rationale="r",
            proposed_by="orch",
        ),
        ledger=ledger,
        approved_by="owner",
        created_at="2026-01-01T00:00:00Z",
    )
    tampered = replace(decision, frozen_hash="0" * 64).with_id()
    ledger.path.write_text(
        json.dumps(tampered.as_dict(), sort_keys=True) + "\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="frozen-hash"):
        current_workflow_settings(ledger)


def test_cleanup_policy_requires_a_valid_owner_approved_shape(tmp_path: Path) -> None:
    """Covers: CFG-1 (owner-approved Git/storage cleanup is logged and validated)."""
    ledger = _ledger(tmp_path)
    with pytest.raises(ConfigError, match="ttl_days"):
        record_cleanup_decision(
            ledger=ledger,
            policy={"kind": "git", "ttl_days": -1, "subjects": ["refs/x"]},
            rationale="r",
            approved_by="owner",
        )
    with pytest.raises(ConfigError, match="kind"):
        record_cleanup_decision(
            ledger=ledger,
            policy={"kind": "elsewhere", "ttl_days": 1, "subjects": ["refs/x"]},
            rationale="r",
            approved_by="owner",
        )
    decision = record_cleanup_decision(
        ledger=ledger,
        policy={"kind": "storage", "ttl_days": 30, "subjects": ["saberops-gate-last"]},
        rationale="owner approved the 30-day gate-evidence cleanup",
        approved_by="owner",
        created_at="2026-02-01T00:00:00Z",
    )
    assert decision.kind == "cleanup"
    assert ledger.tip_for("cleanup") == decision
    assert ledger.tip_for("config") is None
