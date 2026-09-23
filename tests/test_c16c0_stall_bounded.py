"""C16-C0: provider stall boundedness.

Proves that normal worker execution is always bounded by a hard deadline
(explicit owner ``--worker-timeout`` when supplied, else the tier default
T1=600 / T2=1200 / T3=1800), that a hanging worker is terminated through
the existing fenced process-group mechanism and classified TIMEOUT (never
a model-correctness failure, never a fabricated provider error), and that
a timeout preserves evidence while permitting routing failover.

All execution seams are fake/local and deterministic: ``sleep`` stands in
for a stalled provider CLI, so no real provider is contacted.  The
inactivity/stall threshold is never lowered and quiet stdout alone never
kills a worker -- termination below comes only from the hard deadline.
"""

from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path
from typing import Any

import pytest

from saberops.config import (
    TIER_WORKER_TIMEOUTS,
    RunConfig,
    reconstruct_run_config,
    resolve_run_config,
    tier_worker_timeout,
    timeout_defaults,
)
from saberops.db import Database, current_iso_timestamp
from saberops.execution_failover import FailoverAction, FailoverEngine
from saberops.models import (
    Attempt,
    AttemptStatus,
    AttemptTrace,
    DispatchOutcome,
    Run,
    RunStatus,
    Tier,
    TraceCapability,
    WorkerRequest,
)
from saberops.process import ProcessResult, run_process
from saberops.routing_decision import classify_worker_outcome, is_escalation_evidence
from saberops.supervisor import RunSupervisor
from saberops.workers.opencode import OpenCodeAdapter


@pytest.fixture()
def isolated_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep every test away from real owner state."""
    state = tmp_path / "state"
    config = tmp_path / "config"
    state.mkdir(parents=True)
    config.mkdir(parents=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    return tmp_path


def _base_config(**overrides: Any) -> RunConfig:
    return resolve_run_config(
        task="bounded stall proof task",
        target_repo=str(overrides.pop("target_repo", "/tmp")),
        **overrides,
    )


# ---------------------------------------------------------------------------
# 1-3. Tier defaults match the documented contract.
# ---------------------------------------------------------------------------


def test_t1_default_is_600(isolated_xdg: Path) -> None:
    config = _base_config()
    assert tier_worker_timeout(Tier.T1) == 600.0
    assert TIER_WORKER_TIMEOUTS[Tier.T1] == 600.0
    assert timeout_defaults()["T1"] == 600.0
    assert config.worker_timeout_for(Tier.T1) == 600.0


def test_t2_default_is_1200(isolated_xdg: Path) -> None:
    config = _base_config()
    assert tier_worker_timeout(Tier.T2) == 1200.0
    assert TIER_WORKER_TIMEOUTS[Tier.T2] == 1200.0
    assert timeout_defaults()["T2"] == 1200.0
    assert config.worker_timeout_for(Tier.T2) == 1200.0


def test_t3_default_is_1800(isolated_xdg: Path) -> None:
    config = _base_config()
    assert tier_worker_timeout(Tier.T3) == 1800.0
    assert TIER_WORKER_TIMEOUTS[Tier.T3] == 1800.0
    assert timeout_defaults()["T3"] == 1800.0
    assert config.worker_timeout_for(Tier.T3) == 1800.0


# ---------------------------------------------------------------------------
# 4. Explicit owner timeout remains authoritative across tiers.
# ---------------------------------------------------------------------------


def test_explicit_worker_timeout_overrides_tier_default(isolated_xdg: Path) -> None:
    config = _base_config(worker_timeout=45.0)
    assert config.worker_timeout_explicit is True
    assert config.has_worker_deadline is True
    assert config.worker_timeout_for(Tier.T1) == 45.0
    assert config.worker_timeout_for(Tier.T2) == 45.0
    assert config.worker_timeout_for(Tier.T3) == 45.0


# ---------------------------------------------------------------------------
# 5. Effective timeout survives detached/reconstructed execution.
# ---------------------------------------------------------------------------


def test_effective_timeout_survives_detached_reconstruction(isolated_xdg: Path) -> None:
    for kwargs in ({"worker_timeout": None}, {"worker_timeout": 90.0}):
        original = _base_config(**kwargs)
        payload = json.loads(json.dumps(original.as_dict()))
        rebuilt = reconstruct_run_config(payload)
        assert rebuilt.worker_timeout_explicit == original.worker_timeout_explicit
        for tier in (Tier.T1, Tier.T2, Tier.T3):
            assert rebuilt.worker_timeout_for(tier) == original.worker_timeout_for(tier)
    # The default ladder itself survives the round trip (no silent unbounded).
    rebuilt_default = reconstruct_run_config(
        json.loads(json.dumps(_base_config().as_dict()))
    )
    assert rebuilt_default.worker_timeout_for(Tier.T1) == 600.0
    assert rebuilt_default.worker_timeout_for(Tier.T2) == 1200.0
    assert rebuilt_default.worker_timeout_for(Tier.T3) == 1800.0


def test_supervisor_forwards_timeout_only_when_explicit(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    """Detached relaunch must not promote a tier default into an explicit flag."""
    db = Database(tmp_path / "sup.sqlite")
    supervisor = RunSupervisor(db=db)
    default_config = _base_config()
    default_args = supervisor._build_cli_args("run-detached-default", default_config)
    assert "--worker-timeout" not in default_args
    explicit_config = _base_config(worker_timeout=90.0)
    explicit_args = supervisor._build_cli_args("run-detached-explicit", explicit_config)
    assert "--worker-timeout" in explicit_args
    assert explicit_args[explicit_args.index("--worker-timeout") + 1] == "90.0"
    # Either way the reconstructed detached owner computes the same bound.
    for config in (default_config, explicit_config):
        rebuilt = reconstruct_run_config(json.loads(json.dumps(config.as_dict())))
        for tier in (Tier.T1, Tier.T2, Tier.T3):
            assert rebuilt.worker_timeout_for(tier) == config.worker_timeout_for(tier)


# ---------------------------------------------------------------------------
# Hanging-worker seam (fake local provider stall).
# ---------------------------------------------------------------------------


def _hanging_runner(argv: list[str], **kwargs: Any) -> ProcessResult:
    """Stand in for a stalled provider CLI: sleep past any sane deadline.

    Honors the ``timeout`` kwarg exactly like the real process layer, so
    the hard deadline -- and only the hard deadline -- terminates it.
    """
    timeout = kwargs.get("timeout")
    assert timeout is not None and math.isfinite(float(timeout))
    return run_process(["sleep", "30"], **kwargs)


def _hanging_request(tmp_path: Path, timeout: float) -> tuple[WorkerRequest, dict[str, Any]]:
    seen: dict[str, Any] = {"stdout": [], "stalls": [], "resumes": 0, "pid": None}

    def _on_start(identity: dict[str, Any]) -> None:
        seen["pid"] = identity.get("pid")

    return (
        WorkerRequest(
            task="hang like a rate-limited provider",
            worktree_path=tmp_path,
            model="fake/proof-model",
            timeout_seconds=timeout,
            # The general stall threshold is untouched: quiet output must
            # never terminate the worker on its own.
            inactivity_timeout=300.0,
            on_stdout=lambda line: seen["stdout"].append(line),
            on_stderr=lambda line: None,
            on_stall=lambda quiet: seen["stalls"].append(quiet),
            on_resume=lambda: seen.__setitem__("resumes", seen["resumes"] + 1),
            on_process_start=_on_start,
        ),
        seen,
    )


# ---------------------------------------------------------------------------
# 6. A hanging worker reaches TIMEOUT (never indefinitely RUNNING).
# ---------------------------------------------------------------------------


def test_hanging_worker_reaches_timeout(isolated_xdg: Path, tmp_path: Path) -> None:
    adapter = OpenCodeAdapter(runner=_hanging_runner)
    request, seen = _hanging_request(tmp_path, timeout=1.0)
    started = time.monotonic()
    result = adapter.run(request)
    elapsed = time.monotonic() - started
    assert result.status == AttemptStatus.TIMEOUT
    assert result.outcome == DispatchOutcome.TIMEOUT
    assert result.error is not None and "timed out" in result.error
    # Bounded: nowhere near the 30s stall the fake provider wanted.
    assert elapsed < 20.0
    assert result.duration_seconds is not None and result.duration_seconds < 20.0
    # Terminated by the hard deadline, not by quiet stdout: the soft stall
    # threshold (300s) was never crossed, so no stall was ever reported.
    assert seen["stalls"] == []
    # The fenced process group is extinct: the worker PID is gone.
    assert isinstance(seen["pid"], int)
    with pytest.raises(ProcessLookupError):
        os.kill(seen["pid"], 0)


# ---------------------------------------------------------------------------
# 7. Timeout permits routing failover when another attempt is available.
# ---------------------------------------------------------------------------


def test_timeout_permits_routing_failover(isolated_xdg: Path, tmp_path: Path) -> None:
    adapter = OpenCodeAdapter(runner=_hanging_runner)
    request, _ = _hanging_request(tmp_path, timeout=1.0)
    result = adapter.run(request)
    # A timeout is a bounded runtime outcome, never model-correctness
    # evidence: it must not spend the tier-escalation budget.
    assert classify_worker_outcome(result) == DispatchOutcome.TIMEOUT
    assert is_escalation_evidence(DispatchOutcome.TIMEOUT) is False

    db = Database(tmp_path / "failover.sqlite")
    attempt = Attempt(
        id="att-timeout-1",
        run_id="run-timeout-1",
        attempt_number=1,
        provider="fake-provider",
        model="fake/proof-model",
        worktree_path=str(tmp_path),
        branch_name="orch/run-timeout-1/attempt-1",
        status=AttemptStatus.TIMEOUT,
        worker_stdout=result.stdout,
        worker_stderr=result.stderr,
        worker_error=result.error,
        created_at=current_iso_timestamp(),
        completed_at=current_iso_timestamp(),
    )
    trace = AttemptTrace(
        id="trace-timeout-1",
        attempt_id=attempt.id,
        run_id=attempt.run_id,
        trace_class=TraceCapability.NATIVE_STRUCTURED,
        source_kind="test-harness",
        started_at=current_iso_timestamp(),
        updated_at=current_iso_timestamp(),
        completed_at=current_iso_timestamp(),
    )
    decision = FailoverEngine(db=db).evaluate(attempt=attempt, trace=trace)
    assert decision.action == FailoverAction.NEW_ATTEMPT_DIFFERENT_BINDING
    assert decision.source_attempt_id == attempt.id


# ---------------------------------------------------------------------------
# 8. Timeout evidence is persisted.
# ---------------------------------------------------------------------------


def test_timeout_evidence_is_persisted(isolated_xdg: Path, tmp_path: Path) -> None:
    adapter = OpenCodeAdapter(runner=_hanging_runner)
    request, _ = _hanging_request(tmp_path, timeout=1.0)
    result = adapter.run(request)
    assert result.status == AttemptStatus.TIMEOUT

    db = Database(tmp_path / "evidence.sqlite")
    now = current_iso_timestamp()
    db.create_run(
        Run(
            id="run-evidence-1",
            task="bounded stall proof task",
            target_repo=str(tmp_path),
            base_commit="abc123",
            tier=Tier.T1,
            training_allowed=True,
            gate_command="true",
            status=RunStatus.RUNNING,
            created_at=now,
        )
    )
    db.create_attempt(
        Attempt(
            id="att-evidence-1",
            run_id="run-evidence-1",
            attempt_number=1,
            provider=result.provider,
            model=result.model,
            worktree_path=str(tmp_path),
            branch_name="orch/run-evidence-1/attempt-1",
            status=AttemptStatus.RUNNING,
            created_at=now,
        )
    )
    completed = current_iso_timestamp()
    db.update_attempt(
        attempt_id="att-evidence-1",
        status=result.status,
        worker_stdout=result.stdout,
        worker_stderr=result.stderr,
        worker_error=result.error,
        completed_at=completed,
    )
    db.record_event(
        "run-evidence-1",
        "worker_failed",
        attempt_id="att-evidence-1",
        payload={
            "error": result.error,
            "status": result.status.value,
            "provider": result.provider,
            "model": result.model,
            "duration_seconds": result.duration_seconds,
        },
    )
    stored = db.get_attempt("att-evidence-1")
    assert stored is not None
    assert stored.status == AttemptStatus.TIMEOUT
    assert stored.worker_error is not None and "timed out" in stored.worker_error
    assert stored.completed_at == completed
    events = db.get_events_by_type("run-evidence-1", "worker_failed")
    assert len(events) == 1
    payload = events[0].payload or {}
    assert payload["status"] == "TIMEOUT"
    assert payload["duration_seconds"] is not None
    assert "timed out" in (payload["error"] or "")


# ---------------------------------------------------------------------------
# 9. Normal successful workers are unaffected.
# ---------------------------------------------------------------------------


def _quick_runner(argv: list[str], **kwargs: Any) -> ProcessResult:
    return ProcessResult(
        args=list(argv),
        exit_code=0,
        stdout='{"type":"ok"}',
        stderr="",
        duration_seconds=0.05,
    )


def test_successful_worker_unaffected(isolated_xdg: Path, tmp_path: Path) -> None:
    adapter = OpenCodeAdapter(runner=_quick_runner)
    # The default tier bound is finite yet far above a healthy dispatch.
    config = _base_config()
    bound = config.worker_timeout_for(Tier.T1)
    assert math.isfinite(bound) and bound == 600.0
    result = adapter.run(
        WorkerRequest(
            task="healthy task",
            worktree_path=tmp_path,
            model="fake/proof-model",
            timeout_seconds=bound,
            inactivity_timeout=300.0,
        )
    )
    assert result.status == AttemptStatus.SUCCESS
    assert result.outcome is None
    assert result.error is None


# ---------------------------------------------------------------------------
# 10. Cancellation and existing monitor semantics remain intact.
# ---------------------------------------------------------------------------


def test_soft_inactivity_reports_without_killing(isolated_xdg: Path) -> None:
    """Crossing the soft inactivity threshold reports a stall but the worker
    runs to normal completion -- quiet stdout alone never terminates."""
    stalls: list[float] = []
    resumed: list[int] = []
    res = run_process(
        ["sleep", "1"],
        timeout=60.0,
        inactivity_timeout=0.2,
        on_stdout=lambda line: None,
        on_stderr=lambda line: None,
        on_stall=lambda quiet: stalls.append(quiet),
        on_resume=lambda: resumed.append(1),
    )
    assert res.passed
    assert res.timed_out is False
    assert res.exit_code == 0
    assert len(stalls) >= 1


def test_monitor_stall_confirmation_semantics_unchanged(isolated_xdg: Path) -> None:
    """The WorkerMonitor still requires sustained whole-tree inactivity
    before confirming STALLED: a fresh monitor is ACTIVE and must not
    authorize termination."""
    from saberops.monitor import MonitorState, WorkerMonitor

    monitor = WorkerMonitor(
        1 << 20,  # Unused PGID: only lifecycle-state semantics are probed.
        soft_inactivity=300.0,
        stall_confirmation=1800.0,
        sample_interval=3600.0,
    )
    assert monitor.state == MonitorState.ACTIVE
    assert monitor.should_terminate() is False
