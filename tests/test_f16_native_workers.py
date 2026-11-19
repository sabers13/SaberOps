"""F16: hybrid native subagent workers under two-layer control (ADR-0030).

A native child of the orchestrating CLI cooperates through MCP: it claims a
queued job (SaberOps prepares the worktree), heartbeats, reads steering notes
and stop requests, and reports its candidate.  SaberOps never trusts the child:
every report is verified for lineage, worktree and canonical isolation, gated on
the exact commit and independently reviewed by a different family.

Hermetic: fresh empty store, toy repository, fake gate/reviewer, no vendor
prompt and no network.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from lifecycle_rig import RigEnvironment, build_lifecycle_rig

from saberops import native_workers
from saberops.jobs.models import JobState
from saberops.jobs.service import cancel_job, get_job, job_status_view, note_job, submit_job
from saberops.mcp.evidence import EvidenceRoots
from saberops.mcp.lease import OrchestratorLease
from saberops.mcp.ledger import EventLedger
from saberops.mcp.tools import (
    NATIVE_MUTATING_TOOLS,
    TOOL_NAMES,
    ServerContext,
    build_default_registry,
)
from saberops.models import AttemptStatus, ReviewVerdict, RunStatus
from saberops.native_workers import (
    NATIVE_TOOL_NAMES,
    NativeWorkerError,
    claim_native_job,
    native_controls,
    native_enabled,
    native_heartbeat,
    record_native_attempt,
)

pytestmark = pytest.mark.packaged_routing_default


@pytest.fixture()
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RigEnvironment:
    monkeypatch.setenv("SABEROPS_DISABLE_AUTO_SCHEDULER", "1")
    monkeypatch.setenv("ORCH_NATIVE_WORKERS", "1")
    monkeypatch.delenv("ORCH_NATIVE_QUOTA_SAMPLE", raising=False)
    return build_lifecycle_rig(tmp_path, monkeypatch)


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _load(rig: RigEnvironment, job_id: str) -> Any:
    job = get_job(rig.db(), job_id)
    assert job is not None
    return job


def _job(rig: RigEnvironment, goal: str = "Add a native change") -> str:
    result = submit_job(rig.db(), repo=rig.repo, goal=goal, acceptance="the file exists")
    return result.job.job_id


def _commit(rig: RigEnvironment, worktree: Path, name: str = "native.txt") -> str:
    (worktree / name).write_text("native\n", encoding="utf-8")
    git(worktree, "add", name)
    git(worktree, "commit", "-m", "native change")
    return git(worktree, "rev-parse", "HEAD")


class FakeReview:
    """A stand-in for the independent reviewer (different family by default)."""

    def __init__(
        self,
        *,
        provider: str = "anthropic",
        model: str = "claude-sonnet-4-6",
        verdict: ReviewVerdict = ReviewVerdict.PASS,
    ) -> None:
        self.id = "rev_native"
        self.provider = provider
        self.model = model
        self.verdict = verdict
        self.summary = "native review"


def _reviewer(**kwargs: Any) -> Any:
    return lambda _db, _run_id, _timeout: FakeReview(**kwargs)


def _claim(rig: RigEnvironment, job_id: str, vendor: str = "codex") -> dict[str, Any]:
    return claim_native_job(
        rig.db(), job_id, vendor=vendor, model=f"{vendor}-native-test-model"
    )


def _report(
    rig: RigEnvironment,
    job_id: str,
    claim: dict[str, Any],
    *,
    candidate: str | None = None,
    base: str | None = None,
    worktree: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    return record_native_attempt(
        rig.db(),
        job_id,
        vendor=claim["vendor"],
        model=claim["model"],
        base_sha=base or claim["base_sha"],
        candidate_sha=candidate or claim["base_sha"],
        worktree=worktree or claim["worktree"],
        transcript="native transcript",
        reviewer=kwargs.pop("reviewer", _reviewer()),
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# Flag and tool surface
# --------------------------------------------------------------------------- #


def test_flag_off_removes_the_native_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ORCH_NATIVE_WORKERS", raising=False)
    assert not native_enabled()
    for name in NATIVE_TOOL_NAMES:
        assert name not in TOOL_NAMES
    context = ServerContext(
        db_path=tmp_path / "db.sqlite3",
        questions_store=tmp_path / "questions.json",
        config_ledger_path=tmp_path / "config.jsonl",
        evidence=EvidenceRoots(roots=(tmp_path,)),
        ledger=EventLedger(tmp_path / "ledger.jsonl"),
        lease=OrchestratorLease(tmp_path / "lease.json", holder="test", pid=os.getpid()),
    )
    assert not (set(build_default_registry(context).names()) & set(NATIVE_TOOL_NAMES))


def test_flag_on_adds_exactly_the_native_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ORCH_NATIVE_WORKERS", "1")
    lease = OrchestratorLease(tmp_path / "lease.json", holder="test", pid=os.getpid())
    lease.acquire()
    context = ServerContext(
        db_path=tmp_path / "db.sqlite3",
        questions_store=tmp_path / "questions.json",
        config_ledger_path=tmp_path / "config.jsonl",
        evidence=EvidenceRoots(roots=(tmp_path,)),
        ledger=EventLedger(tmp_path / "ledger.jsonl"),
        lease=lease,
    )
    registry = build_default_registry(context)
    assert set(registry.names()) == set(TOOL_NAMES) | set(NATIVE_TOOL_NAMES)
    for name in NATIVE_TOOL_NAMES:
        assert name in NATIVE_MUTATING_TOOLS
        assert registry.get(name) is not None
    # The tool refusal is durable: Antigravity is never an automated worker.
    from saberops.mcp.errors import InvalidParamsError

    with pytest.raises(InvalidParamsError):
        registry.call("native_claim", {"job_id": "job_x", "vendor": "antigravity", "model": "m"})


def test_native_tools_require_the_writer_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from saberops.mcp.errors import LeaseReadOnlyError

    monkeypatch.setenv("ORCH_NATIVE_WORKERS", "1")
    lease = OrchestratorLease(tmp_path / "lease.json", holder="someone-else", pid=1)
    context = ServerContext(
        db_path=tmp_path / "db.sqlite3",
        questions_store=tmp_path / "questions.json",
        config_ledger_path=tmp_path / "config.jsonl",
        evidence=EvidenceRoots(roots=(tmp_path,)),
        ledger=EventLedger(tmp_path / "ledger.jsonl"),
        lease=lease,
    )
    registry = build_default_registry(context)
    for name in sorted(NATIVE_MUTATING_TOOLS):
        with pytest.raises(LeaseReadOnlyError):
            registry.call(name, {"job_id": "job_x"})


def test_flag_off_refuses_a_direct_claim(
    rig: RigEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = _job(rig)
    monkeypatch.delenv("ORCH_NATIVE_WORKERS", raising=False)
    with pytest.raises(NativeWorkerError) as excinfo:
        _claim(rig, job_id)
    assert excinfo.value.code == "NATIVE_WORKERS_DISABLED"
    assert _load(rig, job_id).state is JobState.QUEUED


# --------------------------------------------------------------------------- #
# Claim
# --------------------------------------------------------------------------- #


def test_claim_prepares_the_worktree_and_marks_the_job_running(rig: RigEnvironment) -> None:
    job_id = _job(rig)
    claim = _claim(rig, job_id)
    db = rig.db()
    job = _load(rig, job_id)
    assert job.state is JobState.RUNNING
    assert job.current_run_id == claim["run_id"]
    worktree = Path(claim["worktree"])
    assert worktree.is_dir()
    assert git(worktree, "rev-parse", "HEAD") == claim["base_sha"]
    assert git(worktree, "rev-parse", "--abbrev-ref", "HEAD") == claim["branch"]
    attempts = db.get_attempts_for_run(claim["run_id"])
    assert len(attempts) == 1 and attempts[0].status is AttemptStatus.RUNNING
    assert attempts[0].worktree_path == str(worktree)
    marker = native_workers.native_run_marker(db, claim["run_id"])
    assert marker is not None
    assert marker["supervision"] == "host" and marker["quota"] == "unknown"
    # The canonical checkout did not move.
    assert git(rig.repo, "rev-parse", "HEAD") == rig.base_sha
    assert git(rig.repo, "status", "--porcelain") == ""


def test_claim_refuses_antigravity_and_a_second_concurrent_child(
    rig: RigEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _job(rig, "First native task")
    _claim(rig, first)
    with pytest.raises(NativeWorkerError) as excinfo:
        _claim(rig, first)
    assert excinfo.value.code == "NATIVE_JOB_NOT_QUEUED"

    second = _job(rig, "Second native task")
    with pytest.raises(NativeWorkerError) as excinfo:
        _claim(rig, second, vendor="claude")
    assert excinfo.value.code == "NATIVE_CONCURRENCY_LIMIT"

    third = _job(rig, "Antigravity must not run")
    with pytest.raises(NativeWorkerError) as excinfo:
        _claim(rig, third, vendor="antigravity")
    assert excinfo.value.code == "NATIVE_VENDOR_UNSUPPORTED"
    assert _load(rig, third).state is JobState.QUEUED


# --------------------------------------------------------------------------- #
# Fail-closed verification
# --------------------------------------------------------------------------- #


def test_mismatched_lineage_worktree_and_canonical_checkout_fail_closed(
    rig: RigEnvironment,
) -> None:
    job_id = _job(rig)
    claim = _claim(rig, job_id)
    worktree = Path(claim["worktree"])
    candidate = _commit(rig, worktree)

    with pytest.raises(NativeWorkerError) as excinfo:
        _report(rig, job_id, claim, candidate=candidate, base="1" * 40)
    assert excinfo.value.code == "NATIVE_BASE_MISMATCH"

    with pytest.raises(NativeWorkerError) as excinfo:
        _report(rig, job_id, claim, candidate=candidate, worktree=str(rig.repo))
    assert excinfo.value.code == "NATIVE_WORKTREE_MISMATCH"

    # A dirty canonical checkout is a violation, even if the candidate is fine.
    (rig.repo / "owner.txt").write_text("owner edit\n", encoding="utf-8")
    with pytest.raises(NativeWorkerError) as excinfo:
        _report(rig, job_id, claim, candidate=candidate)
    assert excinfo.value.code == "NATIVE_CANONICAL_MOVED"
    (rig.repo / "owner.txt").unlink()

    assert _load(rig, job_id).state is JobState.RUNNING


def test_empty_candidate_fails_closed(rig: RigEnvironment) -> None:
    job_id = _job(rig)
    claim = _claim(rig, job_id)
    with pytest.raises(NativeWorkerError) as excinfo:
        _report(rig, job_id, claim, candidate=claim["base_sha"])
    assert excinfo.value.code == "NATIVE_EMPTY_CANDIDATE"
    assert _load(rig, job_id).state is JobState.RUNNING


def test_gate_failure_blocks_acceptance(rig: RigEnvironment) -> None:
    job_id = _job(rig)
    claim = _claim(rig, job_id)
    worktree = Path(claim["worktree"])
    (worktree / "app.py").unlink()
    git(worktree, "add", "app.py")
    git(worktree, "commit", "-m", "break the gate")
    candidate = git(worktree, "rev-parse", "HEAD")
    with pytest.raises(NativeWorkerError) as excinfo:
        _report(rig, job_id, claim, candidate=candidate)
    assert excinfo.value.code == "NATIVE_GATE_FAILED"
    db = rig.db()
    assert _load(rig, job_id).state is JobState.FAILED
    failed_run = db.get_run(claim["run_id"])
    assert failed_run is not None and failed_run.status is RunStatus.FAILED
    assert db.get_checks_for_run(claim["run_id"])[-1].passed is False


def test_gate_infrastructure_failure_fails_closed(rig: RigEnvironment) -> None:
    job_id = _job(rig)
    claim = _claim(rig, job_id)
    candidate = _commit(rig, Path(claim["worktree"]))

    def broken_gate(_config: Any, _worktree: Path) -> Any:
        raise RuntimeError("gate runner crashed")

    with pytest.raises(NativeWorkerError) as excinfo:
        _report(rig, job_id, claim, candidate=candidate, gate_runner=broken_gate)
    assert excinfo.value.code == "NATIVE_GATE_UNAVAILABLE"
    assert _load(rig, job_id).state is JobState.FAILED


def test_same_vendor_review_fails_closed(rig: RigEnvironment) -> None:
    job_id = _job(rig)
    claim = _claim(rig, job_id, vendor="codex")
    candidate = _commit(rig, Path(claim["worktree"]))
    with pytest.raises(NativeWorkerError) as excinfo:
        _report(
            rig,
            job_id,
            claim,
            candidate=candidate,
            reviewer=_reviewer(provider="codex", model="gpt-5.6-terra"),
        )
    assert excinfo.value.code == "NATIVE_REVIEW_SAME_VENDOR"
    assert _load(rig, job_id).state is JobState.FAILED


def test_rejected_review_fails_closed(rig: RigEnvironment) -> None:
    job_id = _job(rig)
    claim = _claim(rig, job_id)
    candidate = _commit(rig, Path(claim["worktree"]))
    with pytest.raises(NativeWorkerError) as excinfo:
        _report(
            rig,
            job_id,
            claim,
            candidate=candidate,
            reviewer=_reviewer(verdict=ReviewVerdict.BLOCK),
        )
    assert excinfo.value.code == "NATIVE_REVIEW_REJECTED"
    assert _load(rig, job_id).state is JobState.FAILED


# --------------------------------------------------------------------------- #
# The passing path
# --------------------------------------------------------------------------- #


def test_verified_native_candidate_waits_for_owner_acceptance(rig: RigEnvironment) -> None:
    job_id = _job(rig)
    claim = _claim(rig, job_id, vendor="codex")
    worktree = Path(claim["worktree"])
    candidate = _commit(rig, worktree)
    result = _report(rig, job_id, claim, candidate=candidate)

    db = rig.db()
    assert result["state"] == "SUCCEEDED"
    assert result["candidate_sha"] == candidate
    assert result["supervision"] == "host"
    assert result["owner_action"] == f"orch accept {claim['run_id']}"
    job = _load(rig, job_id)
    assert job.state is JobState.SUCCEEDED
    run = db.get_run(claim["run_id"])
    assert run is not None and run.status is RunStatus.COMPLETED
    assert run.final_attempt_id == claim["attempt_id"]
    attempt = db.get_attempts_for_run(claim["run_id"])[-1]
    assert attempt.status is AttemptStatus.SUCCESS and attempt.commit_sha == candidate
    check = db.get_checks_for_run(claim["run_id"])[-1]
    assert check.passed is True and check.attempt_id == claim["attempt_id"]
    # The canonical checkout is untouched and the candidate is not on it.
    assert git(rig.repo, "rev-parse", "HEAD") == rig.base_sha
    assert git(rig.repo, "status", "--porcelain") == ""
    # The candidate is reachable only through the prepared worktree branch.
    assert git(worktree, "rev-parse", "HEAD") == candidate


def test_native_run_is_never_orphaned_and_never_parks_the_scheduler(
    rig: RigEnvironment,
) -> None:
    from datetime import UTC, datetime

    from saberops.capacity.loop import acquire_lease, scheduler_tick
    from saberops.supervisor import RunSupervisor

    job_id = _job(rig)
    claim = _claim(rig, job_id)
    db = rig.db()
    # The supervisor has no process to probe; it must keep the cooperative run.
    reconciled = RunSupervisor(db=db).reconcile(claim["run_id"], orphan_recoverable=True)
    assert reconciled["action"] == "native_cooperative"
    run = db.get_run(claim["run_id"])
    assert run is not None and run.status is RunStatus.RUNNING
    assert _load(rig, job_id).state is JobState.RUNNING
    # A native attempt holds no capacity reservation; it must not be read as a
    # legacy RUNNING job that parks every other dispatch in the store.
    t0 = datetime(2026, 11, 19, 12, 0, 0, tzinfo=UTC)
    lease = acquire_lease(db, "f16_tick", ttl_seconds=300.0, now=t0)
    assert lease is not None
    result = scheduler_tick(db, lease, now=t0, probes={}, clock_fn=lambda: t0)
    assert result.reason != "legacy_running_without_reservation"
    assert job_id not in result.legacy_running_jobs


def test_native_candidate_is_accept_eligible_after_the_real_review(rig: RigEnvironment) -> None:
    """The candidate reaches the owner through the real gate and reviewer."""
    from saberops.accept import AcceptEngine

    job_id = _job(rig)
    claim = _claim(rig, job_id, vendor="codex")
    candidate = _commit(rig, Path(claim["worktree"]))
    result = record_native_attempt(
        rig.db(),
        job_id,
        vendor=claim["vendor"],
        model=claim["model"],
        base_sha=claim["base_sha"],
        candidate_sha=candidate,
        worktree=claim["worktree"],
        transcript="native transcript",
    )
    assert result["review"]["provider"] == "anthropic"
    eligibility = AcceptEngine(rig.db()).check_accept_eligibility(claim["run_id"])
    assert eligibility.eligible is True, eligibility.as_dict()
    assert result["owner_action"] == f"orch accept {claim['run_id']}"


# --------------------------------------------------------------------------- #
# F17 work gate
# --------------------------------------------------------------------------- #


def test_paused_gate_refuses_new_native_claims_and_reports(rig: RigEnvironment) -> None:
    from saberops.runtime_gate import PauseReason, clear_pause, set_pause

    job_id = _job(rig)
    set_pause(PauseReason.OWNER, "owner paused the engine")
    with pytest.raises(NativeWorkerError) as excinfo:
        _claim(rig, job_id)
    assert excinfo.value.code == "NATIVE_WORK_GATE_CLOSED"
    assert _load(rig, job_id).state is JobState.QUEUED

    clear_pause()
    claim = _claim(rig, job_id)
    candidate = _commit(rig, Path(claim["worktree"]))
    set_pause(PauseReason.OWNER, "owner paused during the attempt")
    with pytest.raises(NativeWorkerError) as excinfo:
        _report(rig, job_id, claim, candidate=candidate)
    assert excinfo.value.code == "NATIVE_WORK_GATE_CLOSED"
    # Nothing was mutated: the worktree, the run and the job survive the pause.
    assert _load(rig, job_id).state is JobState.RUNNING
    run = rig.db().get_run(claim["run_id"])
    assert run is not None and run.status is RunStatus.RUNNING
    assert git(Path(claim["worktree"]), "rev-parse", "HEAD") == candidate

    clear_pause()
    result = _report(rig, job_id, claim, candidate=candidate)
    assert result["state"] == "SUCCEEDED"


def test_engine_pause_posts_a_native_stop_without_parking_the_work(
    rig: RigEnvironment,
) -> None:
    from saberops.pause_work import pause_running_jobs

    job_id = _job(rig)
    claim = _claim(rig, job_id)
    worktree = Path(claim["worktree"])
    candidate = _commit(rig, worktree)
    from saberops.runtime_gate import PauseReason

    parked = pause_running_jobs(
        rig.db(),
        reason=PauseReason.NO_PLANNING_SESSION.value,
        detail="CLI closed",
    )
    # A native child cannot be proven stopped, so it is never reported parked.
    assert job_id not in parked
    controls = native_controls(rig.db(), job_id)
    assert controls["stop_requested"] is True
    assert controls["stop_reason"] == PauseReason.NO_PLANNING_SESSION.value
    assert _load(rig, job_id).state is JobState.RUNNING
    run = rig.db().get_run(claim["run_id"])
    assert run is not None and run.status is RunStatus.RUNNING
    assert git(worktree, "rev-parse", "HEAD") == candidate


# --------------------------------------------------------------------------- #
# Heartbeat, steering and stop
# --------------------------------------------------------------------------- #


def test_heartbeat_delivers_steering_once(rig: RigEnvironment) -> None:
    job_id = _job(rig)
    _claim(rig, job_id)
    note_job(rig.db(), job_id, "Keep the public API unchanged.")
    first = native_heartbeat(rig.db(), job_id, progress="half way")
    assert [note["text"] for note in first["steering_notes"]] == [
        "Keep the public API unchanged."
    ]
    assert first["stop_requested"] is False
    second = native_heartbeat(rig.db(), job_id, progress="still working")
    assert second["steering_notes"] == []


def test_missed_heartbeat_marks_needs_attention_and_posts_a_stop(rig: RigEnvironment) -> None:
    from datetime import UTC, datetime, timedelta

    job_id = _job(rig)
    claim = _claim(rig, job_id)
    later = datetime.now(UTC) + timedelta(seconds=3600)
    status = native_controls(rig.db(), job_id, now=later)
    assert status["needs_attention"] is True
    assert status["stop_requested"] is True
    assert status["stop_reason"] == "heartbeat_deadline"
    view = job_status_view(rig.db(), _load(rig, job_id))
    assert view["activity"] == "needs_attention"
    assert view["native"]["stop_reason"] == "heartbeat_deadline"
    # A heartbeat after the deadline still reports the stop: it never clears.
    heartbeat = native_heartbeat(rig.db(), job_id, now=later + timedelta(seconds=5))
    assert heartbeat["stop_requested"] is True
    assert claim["run_id"] == status["run_id"]


def test_cancel_posts_a_stop_and_refuses_the_late_report(rig: RigEnvironment) -> None:
    job_id = _job(rig)
    claim = _claim(rig, job_id)
    candidate = _commit(rig, Path(claim["worktree"]))
    result = cancel_job(rig.db(), job_id, run_canceller=lambda _run_id: False)
    assert result.cancelled is True
    assert result.job.state is JobState.CANCELLED
    controls = native_controls(rig.db(), job_id)
    assert controls["stop_requested"] is True
    assert controls["stop_reason"] == "owner_cancelled"
    with pytest.raises(NativeWorkerError) as excinfo:
        _report(rig, job_id, claim, candidate=candidate)
    assert excinfo.value.code == "NATIVE_JOB_NOT_RUNNING"
    assert _load(rig, job_id).state is JobState.CANCELLED


# --------------------------------------------------------------------------- #
# MCP surface
# --------------------------------------------------------------------------- #


def test_mcp_native_tools_run_the_whole_loop(
    rig: RigEnvironment, tmp_path: Path
) -> None:
    lease = OrchestratorLease(tmp_path / "lease.json", holder="test", pid=os.getpid())
    lease.acquire()
    context = ServerContext(
        db_path=Path(rig.db().db_path),
        questions_store=tmp_path / "questions.json",
        config_ledger_path=tmp_path / "config.jsonl",
        evidence=EvidenceRoots(roots=(tmp_path,)),
        ledger=EventLedger(tmp_path / "ledger.jsonl"),
        lease=lease,
    )
    registry = build_default_registry(context)
    job_id = _job(rig)
    claim = registry.call(
        "native_claim",
        {"job_id": job_id, "vendor": "codex", "model": "gpt-native-test"},
    )
    assert claim["worktree"]
    heartbeat = registry.call("native_heartbeat", {"job_id": job_id, "progress": "go"})
    assert heartbeat["job_id"] == job_id
    controls = registry.call("native_controls", {"job_id": job_id})
    assert controls["stop_requested"] is False
    # A report with no candidate commit fails closed at the evidence edge.
    from saberops.mcp.errors import ToolError

    with pytest.raises(ToolError):
        registry.call(
            "record_native_attempt",
            {
                "job_id": job_id,
                "vendor": "codex",
                "model": "gpt-native-test",
                "base_sha": claim["base_sha"],
                "candidate_sha": claim["base_sha"],
                "worktree": claim["worktree"],
            },
        )
    assert _load(rig, job_id).state is JobState.RUNNING


def test_guide_documents_two_layer_control() -> None:
    from saberops.mcp.guide import GUIDE

    assert "native_controls" in GUIDE
    assert "saberops-worker" in GUIDE
    assert "stop_requested" in GUIDE
    assert "needs-attention" in GUIDE.replace("needs attention", "needs-attention")
    for name in NATIVE_TOOL_NAMES:
        assert f"`{name}`" in GUIDE
