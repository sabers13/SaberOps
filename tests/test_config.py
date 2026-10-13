"""Run configuration: per-role worker timeouts and initial routing role."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from saberops.config import (
    ROLE_WORKER_TIMEOUTS,
    RunConfig,
    reconstruct_run_config,
    resolve_run_config,
    role_worker_timeout,
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
