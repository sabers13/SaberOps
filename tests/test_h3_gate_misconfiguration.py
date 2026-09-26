"""H3/H3.1 — gate failure decoupled from cross-tier spend authority.

Executable characterization of the H3 hypothesis and its H3.1 correction:

    A launchable but misconfigured gate and a genuine candidate validation
    failure produce no reliable structured distinction before the escalation
    decision.  Therefore a deterministic gate failure may reject/repair a
    candidate, but gate failure alone must NOT authorize cross-tier model
    spend.

Five real Orchestrator runs are driven through the canonical dispatch loop
using the C03 characterization harness (a real git repo, a real gate
subprocess, scripted workers, and the real durable event log):

* A — gate executable does not exist (unlaunchable gate);
* B — gate command launches successfully but the configured target does not
      exist (``make definitely_missing_target`` with a Makefile that defines
      only ``gate``);
* C — correctly configured gate whose assertion genuinely fails on the
      candidate (control);
* D — explicit worker ``CAPABILITY_FAILURE`` (escalation control);
* E — same-tier repair: a second T1 candidate completes from the failed
      candidate lineage.

VERDICT (recorded by this characterization): **H3 WAS CONFIRMED and H3.1
CLOSES IT.**  No structured field on ``ProcessResult`` / ``GateDisposition``
/ ``GateReceipt`` distinguishes "the gate command itself is misconfigured"
from "the candidate genuinely failed validation"; the only structured
discriminator is ``launch_failed`` (a spawn-level failure), which a missing
``make`` target does not set.  Cases B and C therefore keep
``GateOutcome.VALIDATION_FAILURE`` gate truth (FAILED attempt, candidate
commit, ``gate_failed``, repair lineage) but record
``escalation_evidence: False`` and never emit ``tier_escalated``.  Case D
proves explicit ``CAPABILITY_FAILURE`` still escalates T1 -> T2.  Case E
proves suppressing cross-tier escalation does not destroy same-tier repair
lineage.

No free-form stderr is parsed anywhere and no exit-code heuristics exist:
every assertion reads typed structured fields (``gate_outcome``,
``launch_failed``, ``exit_code``, ``escalation_evidence``, typed
``DispatchOutcome``) plus the presence/absence of the ``tier_escalated``
event and the exact provider/model dispatch log.
"""

from __future__ import annotations

import sys
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from pytest import MonkeyPatch

from saberops.config import resolve_run_config
from saberops.db import Database
from saberops.git import GitManager
from saberops.models import (
    AttemptStatus,
    DispatchOutcome,
    GateOutcome,
    RunStatus,
    Tier,
    WorkerCandidate,
    WorkerRequest,
    WorkerResult,
)
from saberops.orchestrator import Orchestrator
from saberops.process import run_process
from saberops.workers.base import WorkerAdapter
from saberops.workers.registry import AdapterRegistry


def _isolate_xdg_config(
    tmp_path_factory: pytest.TempPathFactory,
) -> Generator[None, None, None]:
    mpatch = pytest.MonkeyPatch()
    temp_dir = tmp_path_factory.mktemp("hermetic_xdg_config_h3")
    mpatch.setenv("XDG_CONFIG_HOME", str(temp_dir))
    yield
    mpatch.undo()


@pytest.fixture(autouse=True, scope="module")
def _isolate_user_routing_config(
    tmp_path_factory: pytest.TempPathFactory,
) -> Generator[None, None, None]:
    yield from _isolate_xdg_config(tmp_path_factory)


def _init_repo(path: Path, *, with_makefile: bool = False) -> str:
    path.mkdir(parents=True, exist_ok=True)
    run_process(["git", "init", "-b", "main"], cwd=path)
    run_process(["git", "config", "user.name", "H3 Tester"], cwd=path)
    run_process(["git", "config", "user.email", "h3@example.test"], cwd=path)
    (path / ".gitignore").write_text("__pycache__/\n*.pyc\n.pytest_cache/\n")
    (path / "app.py").write_text("def hello(): return 'hello'\n")
    if with_makefile:
        # A realistic owner gate: only the documented ``gate`` target exists.
        (path / "Makefile").write_text("gate:\n\t@echo ok\n")
    run_process(["git", "add", "."], cwd=path)
    run_process(["git", "commit", "-m", "init: initial code"], cwd=path)
    return GitManager.get_head_commit(path)


class ScriptedWorker(WorkerAdapter):
    """C03-style scripted worker recording the exact model dispatch order."""

    def __init__(
        self,
        provider_name: str,
        script: dict[str, Any],
        call_log: list[str],
    ) -> None:
        self._provider_name = provider_name
        self._script = script
        self._call_log = call_log

    @property
    def provider_name(self) -> str:
        return self._provider_name

    def is_available(self) -> bool:
        return True

    def run(self, request: WorkerRequest) -> WorkerResult:
        if request.read_only:
            self._call_log.append(f"review:{request.model}")
            return WorkerResult(
                provider=self.provider_name,
                model=request.model,
                status=AttemptStatus.SUCCESS,
                summary="Scripted reviewer approved",
                stdout="VERDICT: PASS\n",
                stderr="",
                changed_paths=[],
                has_changes=False,
            )
        self._call_log.append(request.model)
        action = self._script.get(request.model)
        if callable(action):
            result = action(request)
            assert isinstance(result, WorkerResult)
            return result
        return WorkerResult(
            provider=self.provider_name,
            model=request.model,
            status=AttemptStatus.SUCCESS,
            summary="Default no-op",
            stdout="",
            stderr="",
            changed_paths=[],
            has_changes=False,
        )


class _ProviderAliasWorker(WorkerAdapter):
    def __init__(self, alias: str, backing: WorkerAdapter) -> None:
        self._alias = alias
        self._backing = backing

    @property
    def provider_name(self) -> str:
        return self._alias

    def is_available(self) -> bool:
        return self._backing.is_available()

    def run(self, request: WorkerRequest) -> WorkerResult:
        return self._backing.run(request)


def _registry_with_reviewer_ladder(*workers: WorkerAdapter) -> AdapterRegistry:
    reg = AdapterRegistry(list(workers))
    fallback_worker: WorkerAdapter | None = next(
        (w for w in workers if w.is_available()), None
    )
    if fallback_worker is None:
        return reg
    for alias in ("opencode", "codex", "antigravity"):
        if reg.get(alias) is None:
            reg.register(_ProviderAliasWorker(alias, fallback_worker))
    return reg


def _success_with_file(provider: str, model: str, rel_path: str, content: str) -> Any:
    def _action(request: WorkerRequest) -> WorkerResult:
        target = request.worktree_path / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return WorkerResult(
            provider=provider,
            model=model,
            status=AttemptStatus.SUCCESS,
            summary=f"Wrote {rel_path}",
            stdout="",
            stderr="",
            changed_paths=[rel_path],
            has_changes=True,
        )

    return _action


def _tier_keyed_chain(
    t1: list[WorkerCandidate], t2: list[WorkerCandidate]
) -> Any:
    """Return a resolve_candidate_chain replacement keyed on the tier."""

    def _resolve(*, tier: Tier, **_: Any) -> list[WorkerCandidate]:
        return t1 if tier is Tier.T1 else t2

    return _resolve


def _events_by_type(events: list[Any], event_type: str) -> list[Any]:
    return [e for e in events if e.event_type == event_type]


# ---------------------------------------------------------------------------
# CHARACTERIZATION A — UNLAUNCHABLE GATE
# ---------------------------------------------------------------------------


def test_h3_a_unlaunchable_gate_is_not_capability_evidence(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """A T1 worker produces changes; the gate executable does not exist.

    Expected safety property: unlaunchable gate != capability evidence and
    no stronger-tier escalation.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    log: list[str] = []

    script = {"m1": _success_with_file("h3", "m1", "A.txt", "A\n")}
    worker = ScriptedWorker("h3", script, log)
    t1 = [WorkerCandidate("h3", "m1")]
    t2 = [WorkerCandidate("h3", "t2a")]
    monkeypatch.setattr(
        "saberops.orchestrator.resolve_candidate_chain",
        _tier_keyed_chain(t1, t2),
    )
    db = Database(tmp_path / "orch.db")
    orch = Orchestrator(
        db=db,
        registry=_registry_with_reviewer_ladder(worker),
        worktree_base=tmp_path / "wt",
    )
    config = resolve_run_config(
        task="hello",
        target_repo=repo,
        gate_command="/nonexistent/path/h3_gate_binary",
        max_auto_tier="T2",
        publish_candidate=False,
    )
    result = orch.run(config=config, is_peak=False)

    assert result.status == RunStatus.FAILED
    impl_log = [e for e in log if not e.startswith("review:")]
    assert impl_log == ["m1"], impl_log

    events = db.get_events(result.run_id)
    att1 = result.attempts[0]
    att1_events = [e for e in events if e.attempt_id == att1.id]
    gate_completed = next(
        e for e in att1_events if e.event_type == "gate_completed"
    )
    assert gate_completed.payload is not None
    assert gate_completed.payload["gate_outcome"] == GateOutcome.INFRASTRUCTURE_FAILURE.value
    assert gate_completed.payload["launch_failed"] is True
    assert att1.commit_sha is not None

    classified = _events_by_type(att1_events, "dispatch_outcome_classified")
    assert classified, "dispatch_outcome_classified must be durable"
    assert classified[-1].payload is not None
    assert classified[-1].payload["escalation_evidence"] is False
    assert _events_by_type(events, "tier_escalated") == []
    assert "t2a" not in log


# ---------------------------------------------------------------------------
# CHARACTERIZATION B — LAUNCHABLE BUT MISCONFIGURED GATE
# ---------------------------------------------------------------------------


def test_h3_b_misconfigured_gate_target_does_not_escalate(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """A T1 worker produces changes; the gate command launches but its target
    does not exist (``make definitely_missing_target``).

    H3.1 corrected policy: process evidence cannot distinguish this owner
    gate-configuration error from a genuine candidate validation failure, so
    gate truth stays ``VALIDATION_FAILURE`` but the run records
    ``escalation_evidence: False`` and never spends a stronger tier.
    """
    repo = tmp_path / "repo"
    _init_repo(repo, with_makefile=True)
    log: list[str] = []

    script = {
        "m1": _success_with_file("h3", "m1", "A.txt", "A\n"),
        "t2a": _success_with_file("h3", "t2a", "B.txt", "B\n"),
    }
    worker = ScriptedWorker("h3", script, log)
    t1 = [WorkerCandidate("h3", "m1")]
    t2 = [WorkerCandidate("h3", "t2a")]
    monkeypatch.setattr(
        "saberops.orchestrator.resolve_candidate_chain",
        _tier_keyed_chain(t1, t2),
    )
    db = Database(tmp_path / "orch.db")
    orch = Orchestrator(
        db=db,
        registry=_registry_with_reviewer_ladder(worker),
        worktree_base=tmp_path / "wt",
    )
    config = resolve_run_config(
        task="hello",
        target_repo=repo,
        gate_command="make definitely_missing_target",
        max_auto_tier="T2",
        publish_candidate=False,
    )
    result = orch.run(config=config, is_peak=False)

    assert result.status == RunStatus.FAILED
    impl_log = [e for e in log if not e.startswith("review:")]
    assert impl_log == ["m1"], impl_log
    events = db.get_events(result.run_id)
    att1 = result.attempts[0]
    att1_events = [e for e in events if e.attempt_id == att1.id]
    gate_completed = next(
        e for e in att1_events if e.event_type == "gate_completed"
    )
    gate_failed = next(
        e for e in att1_events if e.event_type == "gate_failed"
    )
    assert gate_completed.payload is not None
    assert gate_failed.payload is not None
    classified = _events_by_type(att1_events, "dispatch_outcome_classified")
    assert classified and classified[0].payload is not None

    # Gate truth is preserved: a missing ``make`` target launches ``make``
    # successfully (launch_failed is False) and exits nonzero, so the gate
    # is classified as VALIDATION_FAILURE on real candidate changes.
    assert gate_completed.payload["gate_outcome"] == GateOutcome.VALIDATION_FAILURE.value
    assert gate_completed.payload["launch_failed"] is False
    assert gate_completed.payload["exit_code"] != 0
    assert gate_failed.payload["gate_outcome"] == GateOutcome.VALIDATION_FAILURE.value
    assert gate_failed.payload["outcome"] == DispatchOutcome.GATE_FAILURE.value
    # H3.1: gate failure alone carries no cross-tier spend authority.
    assert classified[0].payload["outcome"] == DispatchOutcome.GATE_FAILURE.value
    assert classified[0].payload["escalation_evidence"] is False

    assert _events_by_type(events, "tier_escalated") == []
    # No stronger-tier worker launched even though a T2 candidate exists.
    assert "t2a" not in impl_log, impl_log


# ---------------------------------------------------------------------------
# CHARACTERIZATION C — REAL VALIDATION FAILURE CONTROL
# ---------------------------------------------------------------------------


def test_h3_c_real_validation_failure_keeps_truth_without_escalation(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """Correctly configured gate launches normally; the candidate genuinely
    fails the assertion.

    H3.1 corrected policy: gate verification truth is fully preserved
    (FAILED attempt, candidate commit, ``gate_failed`` with
    ``VALIDATION_FAILURE``, repair lineage) but the failure alone must not
    launch a stronger tier.  This conservative loss of automatic
    gate-driven cross-tier escalation is intentional until structured
    gate-cause evidence exists.
    """
    repo = tmp_path / "repo"
    base_sha = _init_repo(repo)
    log: list[str] = []

    script = {
        "m1": _success_with_file("h3", "m1", "A.txt", "A\n"),
        "t2a": _success_with_file("h3", "t2a", "ok.txt", "ok\n"),
    }
    worker = ScriptedWorker("h3", script, log)
    t1 = [WorkerCandidate("h3", "m1")]
    t2 = [WorkerCandidate("h3", "t2a")]
    monkeypatch.setattr(
        "saberops.orchestrator.resolve_candidate_chain",
        _tier_keyed_chain(t1, t2),
    )
    db = Database(tmp_path / "orch.db")
    orch = Orchestrator(
        db=db,
        registry=_registry_with_reviewer_ladder(worker),
        worktree_base=tmp_path / "wt",
    )
    # Correctly configured gate: exits 0 only when ok.txt exists.
    gate = (
        f'{sys.executable} -c "import pathlib,sys; '
        'sys.exit(0 if pathlib.Path(\\"ok.txt\\").exists() else 1)"'
    )
    config = resolve_run_config(
        task="hello",
        target_repo=repo,
        gate_command=gate,
        max_auto_tier="T2",
        publish_candidate=False,
    )
    result = orch.run(config=config, is_peak=False)

    assert result.status == RunStatus.FAILED
    impl_log = [e for e in log if not e.startswith("review:")]
    assert impl_log == ["m1"], impl_log
    events = db.get_events(result.run_id)
    att1 = result.attempts[0]
    assert att1.status == AttemptStatus.FAILED
    # The failed candidate commit is preserved, not erased.
    assert att1.commit_sha is not None
    att1_events = [e for e in events if e.attempt_id == att1.id]
    gate_completed = next(
        e for e in att1_events if e.event_type == "gate_completed"
    )
    gate_failed = next(e for e in att1_events if e.event_type == "gate_failed")
    assert gate_completed.payload is not None
    assert gate_failed.payload is not None
    assert gate_completed.payload["gate_outcome"] == GateOutcome.VALIDATION_FAILURE.value
    assert gate_failed.payload["gate_outcome"] == GateOutcome.VALIDATION_FAILURE.value
    assert gate_failed.payload["outcome"] == DispatchOutcome.GATE_FAILURE.value
    assert gate_failed.payload["candidate_sha"] == att1.commit_sha
    # Repair lineage: the failed candidate remains the strongest safe
    # implementation state for a corrective descendant.
    lineage = [
        e
        for e in att1_events
        if e.event_type == "candidate_lineage_advanced"
    ]
    assert lineage, "candidate_lineage_advanced must be durable"
    assert lineage[0].payload is not None
    assert lineage[0].payload["from_sha"] == base_sha
    assert lineage[0].payload["to_sha"] == att1.commit_sha
    assert lineage[0].payload["reason"] == "gate_failed_committed_candidate"
    # H3.1: the durable outcome stays GATE_FAILURE but carries no
    # cross-tier spend authority.
    classified = _events_by_type(att1_events, "dispatch_outcome_classified")
    assert classified and classified[-1].payload is not None
    assert classified[-1].payload["outcome"] == DispatchOutcome.GATE_FAILURE.value
    assert classified[-1].payload["escalation_evidence"] is False
    assert _events_by_type(events, "tier_escalated") == []
    assert "t2a" not in impl_log, impl_log


# ---------------------------------------------------------------------------
# H3.1-D — EXPLICIT CAPABILITY EVIDENCE STILL ESCALATES
# ---------------------------------------------------------------------------


def _capability_failure(provider: str, model: str) -> Any:
    """Return a worker action reporting an explicit typed capability failure.

    Classification reads only the typed ``outcome`` field -- no stderr is
    parsed and no exit code is classified.
    """

    def _action(request: WorkerRequest) -> WorkerResult:
        return WorkerResult(
            provider=provider,
            model=model,
            status=AttemptStatus.FAILED,
            summary="Model reports the task exceeds its capability",
            stdout="",
            stderr="",
            has_changes=False,
            error="explicit capability failure",
            outcome=DispatchOutcome.CAPABILITY_FAILURE,
        )

    return _action


def test_h3_1_d_explicit_capability_failure_escalates(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """A worker result carrying typed ``CAPABILITY_FAILURE`` under an
    automatic T1 -> T2 setup must still escalate and launch the
    stronger-tier worker.  H3.1 removes gate failure from escalation
    authority; it does NOT disable automatic escalation globally.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    log: list[str] = []

    script = {
        "m1": _capability_failure("h3", "m1"),
        "t2a": _success_with_file("h3", "t2a", "ok.txt", "ok\n"),
    }
    worker = ScriptedWorker("h3", script, log)
    t1 = [WorkerCandidate("h3", "m1")]
    t2 = [WorkerCandidate("h3", "t2a")]
    monkeypatch.setattr(
        "saberops.orchestrator.resolve_candidate_chain",
        _tier_keyed_chain(t1, t2),
    )
    db = Database(tmp_path / "orch.db")
    orch = Orchestrator(
        db=db,
        registry=_registry_with_reviewer_ladder(worker),
        worktree_base=tmp_path / "wt",
    )
    gate = (
        f'{sys.executable} -c "import pathlib,sys; '
        'sys.exit(0 if pathlib.Path(\\"ok.txt\\").exists() else 1)"'
    )
    config = resolve_run_config(
        task="hello",
        target_repo=repo,
        gate_command=gate,
        max_auto_tier="T2",
        publish_candidate=False,
    )
    result = orch.run(config=config, is_peak=False)

    assert result.status == RunStatus.COMPLETED
    impl_log = [e for e in log if not e.startswith("review:")]
    events = db.get_events(result.run_id)
    att1 = result.attempts[0]
    att1_events = [e for e in events if e.attempt_id == att1.id]
    classified = _events_by_type(att1_events, "dispatch_outcome_classified")
    assert classified and classified[-1].payload is not None
    assert classified[-1].payload["outcome"] == DispatchOutcome.CAPABILITY_FAILURE.value
    assert classified[-1].payload["escalation_evidence"] is True

    tier_escalated = _events_by_type(events, "tier_escalated")
    assert tier_escalated, "explicit capability evidence must escalate"
    assert tier_escalated[0].payload is not None
    assert tier_escalated[0].payload["from"] == Tier.T1.value
    assert tier_escalated[0].payload["to"] == Tier.T2.value
    # A stronger-tier worker actually launched and completed the run.
    assert "t2a" in impl_log, impl_log


# ---------------------------------------------------------------------------
# H3.1-E — SAME-TIER REPAIR FROM THE FAILED CANDIDATE LINEAGE
# ---------------------------------------------------------------------------


def test_h3_1_e_same_tier_repair_inherits_failed_candidate(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """Two T1 candidates: m1 produces real changes that genuinely fail the
    gate; m2 repairs from the failed candidate lineage and completes.

    Proves cross-tier suppression does not destroy repair lineage: m2
    launches before any stronger tier, inherits the failed candidate state,
    and no T2 launch occurs.
    """
    repo = tmp_path / "repo"
    base_sha = _init_repo(repo)
    log: list[str] = []

    def _repair_from_lineage(request: WorkerRequest) -> WorkerResult:
        # The repair worktree must start from the failed candidate, not the
        # original base: m1's A.txt is already present via lineage.
        assert (request.worktree_path / "A.txt").is_file(), (
            "m2 must inherit the failed candidate state",
        )
        target = request.worktree_path / "ok.txt"
        target.write_text("ok\n")
        return WorkerResult(
            provider="h3",
            model="m2",
            status=AttemptStatus.SUCCESS,
            summary="Repaired from failed candidate lineage",
            stdout="",
            stderr="",
            changed_paths=["ok.txt"],
            has_changes=True,
        )

    script = {
        "m1": _success_with_file("h3", "m1", "A.txt", "A\n"),
        "m2": _repair_from_lineage,
    }
    worker = ScriptedWorker("h3", script, log)
    t1 = [WorkerCandidate("h3", "m1"), WorkerCandidate("h3", "m2")]
    t2 = [WorkerCandidate("h3", "t2a")]
    monkeypatch.setattr(
        "saberops.orchestrator.resolve_candidate_chain",
        _tier_keyed_chain(t1, t2),
    )
    db = Database(tmp_path / "orch.db")
    orch = Orchestrator(
        db=db,
        registry=_registry_with_reviewer_ladder(worker),
        worktree_base=tmp_path / "wt",
    )
    gate = (
        f'{sys.executable} -c "import pathlib,sys; '
        'sys.exit(0 if pathlib.Path(\\"ok.txt\\").exists() else 1)"'
    )
    config = resolve_run_config(
        task="hello",
        target_repo=repo,
        gate_command=gate,
        max_auto_tier="T2",
        publish_candidate=False,
    )
    result = orch.run(config=config, is_peak=False)

    assert result.status == RunStatus.COMPLETED
    impl_log = [e for e in log if not e.startswith("review:")]
    # m2 launches before any stronger tier; T2 never launches.
    assert impl_log == ["m1", "m2"], impl_log
    events = db.get_events(result.run_id)
    assert _events_by_type(events, "tier_escalated") == []

    att1 = result.attempts[0]
    att2 = result.attempts[1]
    assert att1.status == AttemptStatus.FAILED
    assert att1.commit_sha is not None
    assert att2.status == AttemptStatus.SUCCESS

    # m1's failure advanced the lineage away from the original base.
    att1_events = [e for e in events if e.attempt_id == att1.id]
    lineage = [
        e for e in att1_events if e.event_type == "candidate_lineage_advanced"
    ]
    assert lineage, "candidate_lineage_advanced must be durable"
    assert lineage[0].payload is not None
    assert lineage[0].payload["from_sha"] == base_sha
    assert lineage[0].payload["to_sha"] == att1.commit_sha

    # m2 started exactly from the failed candidate commit.
    att2_events = [e for e in events if e.attempt_id == att2.id]
    started = next(
        e for e in att2_events if e.event_type == "attempt_started"
    )
    assert started.payload is not None
    assert started.payload["inherited_base_sha"] == att1.commit_sha

    # m1's gate failure carried no escalation authority.
    classified = _events_by_type(att1_events, "dispatch_outcome_classified")
    assert classified and classified[-1].payload is not None
    assert classified[-1].payload["outcome"] == DispatchOutcome.GATE_FAILURE.value
    assert classified[-1].payload["escalation_evidence"] is False
