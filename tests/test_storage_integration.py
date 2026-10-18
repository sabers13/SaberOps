"""GS-1 service-integration contracts across the storage lifecycle and its seams.

Covers: ADR-0003 D4/D6/D7 and Amendments 1/2 -- the production caller path, the
durable audit record, the SB-1 accounting/fencing surviving consolidation, the
scheduler maintenance hook, and the honest non-activation of adaptive admission.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from saberops.capacity.loop import _invoke_storage_maintenance, run_scheduler_loop
from saberops.db import Database, DatabaseSchemaError
from saberops.gate_runner import GateScratchMode
from saberops.git import GitManager
from saberops.models import Attempt, AttemptStatus, CheckResult, Run, RunStatus, Tier
from saberops.sqlite_policy import SqliteBusyError
from saberops.storage_budget import (
    AdmissionOutcome,
    Attribution,
    EstimateComponent,
    EstimateSource,
    FilesystemIdentity,
    LedgerConfig,
    OwnerToken,
    ReservationKey,
    StorageDecision,
    StorageEstimate,
    StorageLedger,
)
from saberops.storage_gc import GcPolicy, apply_gc, preview_gc
from saberops.storage_inventory import ArtifactClass, build_inventory
from saberops.storage_maintenance import StorageMaintenance, database_audit_sink

_CREATED = "2026-09-24T10:00:00+00:00"
_AUTHOR = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **_AUTHOR},
    ).stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "proj"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("hello\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    return repo


def _terminal_run(db: Database, repo: Path, run_id: str, wt: Path, branch: str) -> str:
    base = GitManager.get_head_commit(repo)
    db.create_run(
        Run(
            id=run_id,
            task="integ",
            target_repo=str(repo),
            base_commit=base,
            tier=Tier.T1,
            training_allowed=True,
            gate_command="true",
            status=RunStatus.COMPLETED,
            created_at=_CREATED,
            completed_at=_CREATED,
        )
    )
    db.create_attempt(
        Attempt(
            id=f"{run_id}-a1",
            run_id=run_id,
            attempt_number=1,
            provider="p",
            model="m",
            worktree_path=str(wt),
            branch_name=branch,
            status=AttemptStatus.SUCCESS,
            commit_sha=base,
            created_at=_CREATED,
            base_sha=base,
        )
    )
    db.create_check(
        CheckResult(
            id=f"{run_id}-c1",
            attempt_id=f"{run_id}-a1",
            gate_command="true",
            exit_code=0,
            stdout="ok",
            stderr="",
            passed=True,
            created_at=_CREATED,
        )
    )
    db.record_candidate_rejection_once(run_id, base)
    return base


def _approved() -> GcPolicy:
    return GcPolicy(
        approved=True,
        version="integ",
        source="owner-file",
        allow_worktree_retirement=True,
        allow_branch_retirement=True,
    )


def test_end_to_end_retire_and_durable_audit(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 1 §4 -- provenance and audit survive cleanup."""
    repo = _repo(tmp_path)
    db = Database(tmp_path / "orch.db")
    wt = GitManager.get_default_worktree_base(repo) / "run1" / "a1"
    GitManager.create_worktree(repo, "orch/run1/a1", wt)
    sha = _terminal_run(db, repo, "run1", wt, "orch/run1/a1")

    inventory = build_inventory(db, repo_root=repo)
    preview = preview_gc(inventory, policy=_approved(), policy_path=tmp_path / "gc.json")
    result = apply_gc(
        preview,
        preview.candidates,
        db=db,
        repo_root=repo,
        audit=database_audit_sink(db),
    )
    assert len(result.applied) == 2
    assert not wt.exists()
    assert GitManager.branch_tip(repo, "orch/run1/a1") is None
    assert GitManager.commit_exists(repo, sha)

    events = db.list_storage_events()
    phases = [event["phase"] for event in events]
    assert phases.count("intent") == 2 and phases.count("result") == 2
    result_events = [e for e in events if e["phase"] == "result"]
    assert all(e["artifact_ref"] and e["artifact_identity"] for e in result_events)
    assert any(e["sha"] for e in result_events)
    assert all(e["policy_ref"] and e["reason"] for e in events)
    assert any(e["artifact_class"] == ArtifactClass.ATTEMPT_WORKTREE.value for e in events)


def test_apply_requires_a_standing_policy(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 1 §7 -- no policy means no destructive apply."""
    repo = _repo(tmp_path)
    db = Database(tmp_path / "orch.db")
    wt = GitManager.get_default_worktree_base(repo) / "run1" / "a1"
    GitManager.create_worktree(repo, "orch/run1/a1", wt)
    _terminal_run(db, repo, "run1", wt, "orch/run1/a1")
    inventory = build_inventory(db, repo_root=repo)
    preview = preview_gc(inventory, policy=None)
    result = apply_gc(preview, preview.candidates, db=db, repo_root=repo, audit=lambda e: None)
    assert result.applied == ()
    assert wt.exists()


def test_sb1_accounting_and_fencing_survive_consolidation(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 2 -- 1/2/4 sessions sum, duplicates are one."""
    policy = tmp_path / "margin.json"
    policy.write_text(
        json.dumps(
            {
                "approved": True,
                "fixed_bytes": 1 << 20,
                "min_bytes": 1 << 20,
                "max_bytes": 1 << 40,
                "version": "t",
            }
        ),
        encoding="utf-8",
    )
    identity = FilesystemIdentity(
        key="fs1", device=1, total_bytes=100 << 30, free_bytes=50 << 30
    )
    config = LedgerConfig(
        root=tmp_path / "ledger", policy_path=policy, allow_isolated=True
    )
    with StorageLedger(config, probe=lambda path: identity) as ledger:
        def estimate() -> StorageEstimate:
            return StorageEstimate(
                components=(EstimateComponent(path=str(tmp_path), remaining_growth_bytes=1 << 30),),
                source=EstimateSource.OWNER_HINT,
                sample_count=0,
                confidence=0.5,
                attribution=Attribution.OWNED,
            )

        def reserve(session: str, owner_gen: int = 1) -> StorageDecision:
            return ledger.try_reserve(
                ReservationKey(
                    installation_id="inst",
                    session_id=session,
                    attempt_id="a1",
                    phase_id="p1",
                ),
                OwnerToken(owner_id=f"owner-{session}", generation=owner_gen),
                estimate(),
            )

        for index in range(1, 5):
            decision = reserve(f"session-{index}")
            assert decision.outcome is AdmissionOutcome.ADMITTED
        decision = ledger.assess(estimate())
        # Four live reservations must be summed, not assessed independently.
        assert decision.total_active_remaining is not None
        assert decision.total_active_remaining >= 4 * (1 << 30)

        # Duplicate key is idempotent: it does not add a fifth reservation.
        before = len(ledger.active_reservations())
        reserve("session-4")
        assert len(ledger.active_reservations()) == before

        # An unknown/empty estimate fails closed rather than reserving zero.
        empty = StorageEstimate(
            components=(),
            source=EstimateSource.OWNER_HINT,
            sample_count=0,
            confidence=0.0,
        )
        unavailable = ledger.try_reserve(
            ReservationKey(
                installation_id="inst",
                session_id="session-unknown",
                attempt_id="a1",
                phase_id="p1",
            ),
            OwnerToken(owner_id="owner-unknown", generation=1),
            empty,
        )
        assert unavailable.outcome is AdmissionOutcome.UNAVAILABLE


def test_scheduler_loop_invokes_maintenance_and_surfaces_typed_infra(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: ADR-0003 D7/Amendment 1 -- typed INFRA surfaces, no crash/relaunch."""
    from saberops.capacity.loop import TickResult

    db = Database(tmp_path / "orch.db")
    calls: list[str] = []

    def maintenance(trigger: str) -> str | None:
        calls.append(trigger)
        return SqliteBusyError("busy").reason_code

    monkeypatch.setattr(
        "saberops.capacity.loop.scheduler_tick", lambda *a, **k: TickResult(ran=True)
    )
    moment = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
    result = run_scheduler_loop(
        db,
        once=True,
        now_fn=lambda: moment,
        storage_maintenance=maintenance,
    )
    assert result is not None
    assert calls == ["startup"]
    assert result.storage_infra_reason == "SQLITE_BUSY"


def test_scheduler_loop_runs_periodic_maintenance_on_its_cadence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: ADR-0003 Amendment 1 §5 -- hourly periodic maintenance cadence."""
    from datetime import timedelta

    from saberops.capacity.loop import TickResult

    db = Database(tmp_path / "orch.db")
    calls: list[str] = []
    clock = {"t": datetime(2026, 10, 17, 12, 0, tzinfo=UTC)}
    ticks: list[int] = []

    def maintenance(trigger: str) -> str | None:
        calls.append(trigger)
        return None

    def tick(*_a: object, **_k: object) -> TickResult:
        ticks.append(1)
        if len(ticks) == 1:
            clock["t"] = clock["t"] + timedelta(hours=2)
            return TickResult(ran=True)
        return TickResult(ran=False)

    monkeypatch.setattr("saberops.capacity.loop.scheduler_tick", tick)
    run_scheduler_loop(
        db,
        once=False,
        tick_interval=0.0,
        now_fn=lambda: clock["t"],
        storage_maintenance=maintenance,
    )
    assert calls == ["startup", "periodic"]


def test_maintenance_invocation_never_raises_into_the_loop() -> None:
    """Covers: ADR-0003 D7 -- an infra failure is a typed reason, never a crash."""
    def boom(_trigger: str) -> str | None:
        raise SqliteBusyError("locked")

    assert _invoke_storage_maintenance(boom, "periodic") == "SQLITE_BUSY"

    def healthy(_trigger: str) -> str | None:
        return None

    assert _invoke_storage_maintenance(healthy, "periodic") is None


def test_disk_low_maintenance_launches_no_workers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: ADR-0003 D4 -- DISK_LOW parks the real scheduler with zero launches."""
    from saberops.storage_maintenance import MaintenanceTrigger

    repo = _repo(tmp_path)
    db = Database(tmp_path / "orch.db")
    _seed_queued_job(db, repo)
    monkeypatch.setattr(
        "saberops.storage_maintenance._statvfs", lambda _p: (1 << 30, 100 << 30)
    )
    launched: list[str] = []

    def launcher(_job: object, _decision: object, run_id: str) -> None:
        launched.append(run_id)

    service = StorageMaintenance(
        db,
        repo_root=repo,
        now_fn=lambda: datetime(2026, 10, 17, 12, 0, tzinfo=UTC),
    )
    result = run_scheduler_loop(
        db,
        once=True,
        now_fn=lambda: datetime(2026, 10, 17, 12, 0, tzinfo=UTC),
        run_launcher=launcher,
        storage_maintenance=lambda trigger: service.run(MaintenanceTrigger(trigger)).infra_reason,
        launch_guard=service.launch_guard_reason,
    )
    assert result is not None
    assert result.ran is False
    assert result.storage_infra_reason == "DISK_LOW"
    assert launched == []


def test_corrupt_or_unknown_database_is_never_adopted(tmp_path: Path) -> None:
    """Covers: ADR-0003 D6 -- corrupt/foreign/unknown DB fails closed."""
    db_path = tmp_path / "orch.db"
    Database(db_path)
    # Simulate a foreign/unknown schema version without touching the service.
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA user_version = 999")
    conn.commit()
    conn.close()
    with pytest.raises(DatabaseSchemaError):
        Database(db_path)


# --------------------------------------------------------------------------
# CLI surface (dry-run no-op, explicit authority, legacy cleanup protections)
# --------------------------------------------------------------------------


def test_cli_gc_dry_run_creates_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Covers: ADR-0003 Amendment 1 §6 -- CLI dry-run on a missing store is a no-op."""
    from saberops.cli import main

    repo = _repo(tmp_path)
    db_path = tmp_path / "missing.db"
    code = main(["gc", "--dry-run", "--json", "--repo", str(repo), "--db", str(db_path)])
    assert code == 0
    assert not db_path.exists()
    assert not Path(str(db_path) + "-wal").exists()
    payload = json.loads(capsys.readouterr().out)
    assert payload["approved"] is False
    assert payload["candidates"] == []
    assert payload["repo_root"] == str(repo.resolve())


def test_cli_gc_apply_requires_explicit_authority(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Covers: ADR-0003 Amendment 1 §7 -- apply without a policy is refused."""
    from saberops.cli import main

    repo = _repo(tmp_path)
    db_path = tmp_path / "orch.db"
    code = main(["gc", "--apply", "--repo", str(repo), "--db", str(db_path)])
    assert code == 1
    assert "Refusing --apply" in capsys.readouterr().err
    assert not db_path.exists()


def test_cli_cleanup_uses_guarded_route_and_never_prunes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Covers: ADR-0003 Amendment 1 §3 -- legacy cleanup keeps protections, no prune."""
    from saberops.cli import main

    repo = _repo(tmp_path)
    db = Database(tmp_path / "orch.db")
    clean = GitManager.get_default_worktree_base(repo) / "run1" / "a1"
    dirty = GitManager.get_default_worktree_base(repo) / "run1" / "a2"
    GitManager.create_worktree(repo, "orch/run1/a1", clean)
    GitManager.create_worktree(repo, "orch/run1/a2", dirty)
    prunable = GitManager.get_default_worktree_base(repo) / "run1" / "a3"
    GitManager.create_worktree(repo, "orch/run1/a3", prunable)
    _terminal_run(db, repo, "run1", clean, "orch/run1/a1")
    base = GitManager.get_head_commit(repo)
    for index, path in ((2, dirty), (3, prunable)):
        db.create_attempt(
            Attempt(
                id=f"run1-a{index}",
                run_id="run1",
                attempt_number=index,
                provider="p",
                model="m",
                worktree_path=str(path),
                branch_name=f"orch/run1/a{index}",
                status=AttemptStatus.SUCCESS,
                commit_sha=base,
                created_at=_CREATED,
                base_sha=base,
            )
        )
    (dirty / "a.txt").write_text("dirty\n", encoding="utf-8")
    # Make one registered worktree prunable by deleting its directory out of band.
    import shutil

    shutil.rmtree(prunable)
    capsys.readouterr()

    code = main(["cleanup", "run1", "--db", str(tmp_path / "orch.db")])
    assert code == 0
    assert not clean.exists()
    assert dirty.exists()
    assert GitManager.branch_tip(repo, "orch/run1/a1") is not None  # branches preserved

    registrations = GitManager.list_worktrees(repo)
    assert any(Path(reg.path).resolve() == prunable.resolve() for reg in registrations)
    assert any(reg.prunable for reg in registrations)


# --------------------------------------------------------------------------
# F1 -- launch ordering and the wired disk guard (zero launches on refusal)
# --------------------------------------------------------------------------


def _seed_queued_job(db: Database, repo: Path) -> str:
    from saberops.jobs.models import Job, JobRequirements, JobState, WaitingReason
    from saberops.jobs.store import insert_job

    job = Job(
        job_id="job1",
        idempotency_key=None,
        caller_id="owner",
        repo=str(repo.resolve()),
        base_ref="HEAD",
        base_sha=GitManager.get_head_commit(repo),
        goal="storage guard probe",
        acceptance=None,
        requirements=JobRequirements(),
        state=JobState.QUEUED,
        waiting_reason=WaitingReason.NEW,
    )
    insert_job(db, job)
    return job.job_id


def test_scheduler_parks_before_any_tick_on_disk_low(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: ADR-0003 D4 -- startup maintenance precedes the tick; zero launches."""
    from saberops.jobs.models import JobState
    from saberops.jobs.store import get_job
    from saberops.storage_maintenance import MaintenanceTrigger, StorageMaintenance

    repo = _repo(tmp_path)
    db = Database(tmp_path / "orch.db")
    job_id = _seed_queued_job(db, repo)
    monkeypatch.setattr(
        "saberops.storage_maintenance._statvfs", lambda _p: (1 << 30, 100 << 30)
    )
    launched: list[str] = []

    def launcher(job: object, decision: object, run_id: str) -> None:
        launched.append(run_id)

    service = StorageMaintenance(
        db, repo_root=repo, now_fn=lambda: datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
    )

    def maintenance(trigger: str) -> str | None:
        return service.run(MaintenanceTrigger(trigger)).infra_reason

    result = run_scheduler_loop(
        db,
        once=True,
        now_fn=lambda: datetime(2026, 10, 17, 12, 0, tzinfo=UTC),
        storage_maintenance=maintenance,
        launch_guard=service.launch_guard_reason,
        run_launcher=launcher,
    )
    assert result is not None
    assert result.ran is False
    assert result.storage_infra_reason == "DISK_LOW"
    assert launched == []
    job = get_job(db, job_id)
    assert job is not None and job.state is JobState.QUEUED and job.current_run_id is None
    assert db.list_storage_events() == []


def test_tick_launch_guard_blocks_dispatch_without_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: ADR-0003 D4 -- the per-tick guard blocks between maintenance passes."""
    from saberops.capacity.loop import acquire_lease, release_lease, scheduler_tick
    from saberops.jobs.models import JobState
    from saberops.jobs.store import get_job

    repo = _repo(tmp_path)
    db = Database(tmp_path / "orch.db")
    job_id = _seed_queued_job(db, repo)
    launched: list[str] = []

    def launcher(job: object, decision: object, run_id: str) -> None:
        launched.append(run_id)

    moment = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
    lease = acquire_lease(db, "test-holder", ttl_seconds=60.0, now=moment)
    assert lease is not None
    try:
        result = scheduler_tick(
            db,
            lease,
            now=moment,
            clock_fn=lambda: moment,
            run_launcher=launcher,
            launch_guard=lambda: "DISK_LOW",
        )
    finally:
        release_lease(db, lease, now=moment)
    assert result.ran is False
    assert result.storage_infra_reason == "DISK_LOW"
    assert launched == []
    job = get_job(db, job_id)
    assert job is not None and job.state is JobState.QUEUED


def test_scheduler_proceeds_when_disk_is_healthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: ADR-0003 D4 -- a healthy floor does not block the tick."""
    from saberops.storage_maintenance import MaintenanceTrigger, StorageMaintenance

    repo = _repo(tmp_path)
    db = Database(tmp_path / "orch.db")
    _seed_queued_job(db, repo)
    monkeypatch.setattr(
        "saberops.storage_maintenance._statvfs", lambda _p: (500 << 30, 1000 << 30)
    )
    ticked: list[int] = []
    from saberops.capacity.loop import TickResult

    def _fake_tick(*_args: object, **_kwargs: object) -> TickResult:
        ticked.append(1)
        return TickResult(ran=True)

    monkeypatch.setattr("saberops.capacity.loop.scheduler_tick", _fake_tick)
    service = StorageMaintenance(
        db, repo_root=repo, now_fn=lambda: datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
    )
    result = run_scheduler_loop(
        db,
        once=True,
        now_fn=lambda: datetime(2026, 10, 17, 12, 0, tzinfo=UTC),
        storage_maintenance=lambda trigger: service.run(MaintenanceTrigger(trigger)).infra_reason,
        launch_guard=service.launch_guard_reason,
    )
    assert result is not None and result.ran is True
    assert ticked == [1]


def test_gate_runner_disk_guard_blocks_before_any_subprocess(tmp_path: Path) -> None:
    """Covers: ADR-0003 D4 -- the real GateRunner hits the floor guard."""
    from saberops.gate_runner import GateRunner, GateRunnerDiskLowError, GateRunnerRequest

    repo = _repo(tmp_path)
    request = GateRunnerRequest(
        target_repo=repo,
        gate_command="true",
        timeout=30.0,
        frozen_gate_scratch_mode=GateScratchMode.DISK,
        candidate_sha="a" * 40,
        expected_post_gate_sha="a" * 40,
        evidence_dir=tmp_path / "evidence",
    )
    runner = GateRunner()
    with pytest.raises(GateRunnerDiskLowError) as excinfo:
        runner.run(request, disk_guard=lambda _path: "DISK_LOW")
    assert excinfo.value.reason_code == "DISK_LOW"
    assert not (tmp_path / "evidence").exists()
