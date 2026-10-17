"""GS-1 bounded storage-maintenance contracts.

Covers: ADR-0003 D4/D6 (current floor, weekly vacuum, bounded sweep),
ADR-0003 Amendment 1 §5 (bounded GC on DISK_LOW), and the explicit
non-activation of adaptive admission (Amendment 2).
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from saberops.db import Database
from saberops.git import GitManager
from saberops.models import Attempt, AttemptStatus, CheckResult, Run, RunStatus, Tier
from saberops.storage_maintenance import (
    DEFAULT_DISK_FLOOR_GB,
    DiskLowInfra,
    DiskState,
    MaintenanceTrigger,
    StorageMaintenance,
    current_disk_floor_bytes,
    disk_ok,
)

_CREATED = "2026-09-24T10:00:00+00:00"
_GB = 1024**3
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


def _terminal_run(db: Database, repo: Path, run_id: str, wt: Path, branch: str) -> None:
    base = GitManager.get_head_commit(repo)
    db.create_run(
        Run(
            id=run_id,
            task="maint",
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


def _maintenance(
    db: Database,
    repo: Path,
    *,
    policy_path: Path | None = None,
    now: datetime | None = None,
    audit: object | None = None,
) -> StorageMaintenance:
    moment = now or datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
    return StorageMaintenance(
        db,
        repo_root=repo,
        policy_path=policy_path,
        now_fn=lambda: moment,
        audit=audit,  # type: ignore[arg-type]
    )


def test_current_floor_matches_the_live_guard(tmp_path: Path) -> None:
    """Covers: ADR-0003 D4/Q1 -- the fixed floor is consumed, never lowered."""
    assert current_disk_floor_bytes(100 * _GB) == 10 * _GB
    assert current_disk_floor_bytes(100 * _GB, DEFAULT_DISK_FLOOR_GB) == 10 * _GB
    assert current_disk_floor_bytes(2000 * _GB) == 200 * _GB  # 10 % wins
    assert disk_ok([tmp_path]) is True


def test_assess_disk_states(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Covers: ADR-0003 D4 -- LOW / OK / UNKNOWN are distinguished honestly."""
    db = Database(tmp_path / "orch.db")
    maintenance = _maintenance(db, tmp_path)
    total = 100 * _GB

    monkeypatch.setattr(
        "saberops.storage_maintenance._statvfs", lambda _p: (5 * _GB, total)
    )
    assert maintenance.assess_disk([tmp_path]).state is DiskState.LOW

    monkeypatch.setattr(
        "saberops.storage_maintenance._statvfs", lambda _p: (50 * _GB, total)
    )
    assert maintenance.assess_disk([tmp_path]).state is DiskState.OK

    def boom(_p: str) -> tuple[int, int]:
        raise OSError("nope")

    monkeypatch.setattr("saberops.storage_maintenance._statvfs", boom)
    assert maintenance.assess_disk([tmp_path]).state is DiskState.UNKNOWN


def test_maintenance_without_policy_is_non_destructive(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 1 §7 -- no policy means no destructive GC."""
    repo = _repo(tmp_path)
    db = Database(tmp_path / "orch.db")
    wt = GitManager.get_default_worktree_base(repo) / "run1" / "a1"
    GitManager.create_worktree(repo, "orch/run1/a1", wt)
    _terminal_run(db, repo, "run1", wt, "orch/run1/a1")
    maintenance = _maintenance(db, repo)
    result = maintenance.run(MaintenanceTrigger.PERIODIC)
    assert result.ran_gc is False
    assert result.applied == 0
    assert wt.exists()
    assert result.telemetry["policy_approved"] == "0"
    assert result.telemetry["adaptive_admission"] == "inactive"


def test_maintenance_applies_bounded_gc_under_policy(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 1 §5 -- bounded GC under a standing policy."""
    repo = _repo(tmp_path)
    db = Database(tmp_path / "orch.db")
    wt = GitManager.get_default_worktree_base(repo) / "run1" / "a1"
    GitManager.create_worktree(repo, "orch/run1/a1", wt)
    _terminal_run(db, repo, "run1", wt, "orch/run1/a1")
    policy_file = tmp_path / "gc.json"
    policy_file.write_text(
        json.dumps(
            {"approved": True, "allow_worktree_retirement": True, "allow_branch_retirement": True}
        ),
        encoding="utf-8",
    )
    maintenance = _maintenance(db, repo, policy_path=policy_file)
    result = maintenance.run(MaintenanceTrigger.PERIODIC)
    assert result.ran_gc is True
    assert result.applied == 2
    assert not wt.exists()
    # The audit trail is durable in the owner database.
    assert any(event["phase"] == "result" for event in db.list_storage_events())


def test_disk_low_runs_gc_then_rechecks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Covers: ADR-0003 D4/Amendment 1 §5 -- GC then a real recheck on DISK_LOW."""
    repo = _repo(tmp_path)
    db = Database(tmp_path / "orch.db")
    wt = GitManager.get_default_worktree_base(repo) / "run1" / "a1"
    GitManager.create_worktree(repo, "orch/run1/a1", wt)
    _terminal_run(db, repo, "run1", wt, "orch/run1/a1")
    policy_file = tmp_path / "gc.json"
    policy_file.write_text(
        json.dumps({"approved": True, "allow_worktree_retirement": True}),
        encoding="utf-8",
    )
    total = 10 * _GB
    calls = {"n": 0}

    def fake(_p: str) -> tuple[int, int]:
        calls["n"] += 1
        if calls["n"] <= 2:
            return (1 * _GB, total)
        return (50 * _GB, total)

    monkeypatch.setattr("saberops.storage_maintenance._statvfs", fake)
    maintenance = _maintenance(db, repo, policy_path=policy_file)
    result = maintenance.run(MaintenanceTrigger.DISK_LOW)
    assert result.disk_before.state is DiskState.LOW
    assert result.disk_after is not None and result.disk_after.state is DiskState.OK
    assert result.infra_reason is None


def test_disk_low_still_low_is_a_typed_infra_park(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: ADR-0003 D4 -- insufficient/unknown space stays a typed INFRA."""
    repo = _repo(tmp_path)
    db = Database(tmp_path / "orch.db")
    total = 10 * _GB
    monkeypatch.setattr(
        "saberops.storage_maintenance._statvfs", lambda _p: (1 * _GB, total)
    )
    maintenance = _maintenance(db, repo)
    result = maintenance.run(MaintenanceTrigger.DISK_LOW)
    assert result.infra_reason == "DISK_LOW"
    assert result.applied == 0


def test_preflight_launch_parks_on_low_and_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: ADR-0003 D4 -- a launch below the floor/unknown never starts."""
    db = Database(tmp_path / "orch.db")
    maintenance = _maintenance(db, tmp_path)
    monkeypatch.setattr(
        "saberops.storage_maintenance._statvfs", lambda _p: (1 * _GB, 100 * _GB)
    )
    with pytest.raises(DiskLowInfra) as excinfo:
        maintenance.preflight_launch()
    assert excinfo.value.reason_code == "DISK_LOW"

    def boom(_p: str) -> tuple[int, int]:
        raise OSError("nope")

    monkeypatch.setattr("saberops.storage_maintenance._statvfs", boom)
    with pytest.raises(DiskLowInfra):
        maintenance.preflight_launch()


def test_weekly_vacuum_respects_ratio_and_interval(tmp_path: Path) -> None:
    """Covers: ADR-0003 D6 -- weekly owner-DB VACUUM above 20 % free pages."""
    repo = _repo(tmp_path)
    db_path = tmp_path / "orch.db"
    db = Database(db_path)
    policy_file = tmp_path / "gc.json"
    policy_file.write_text(json.dumps({"approved": True, "allow_vacuum": True}), encoding="utf-8")
    # Grow the DB, then delete the rows so its free-page ratio exceeds 20 %.
    with db.storage_write_transaction() as conn:
        conn.executemany(
            "INSERT INTO storage_cleanup_events ("
            " at, phase, action, artifact_class, artifact_ref, artifact_identity, reason"
            ") VALUES (?, 'intent', 'noop', 'evidence', ?, ?, 'bulk')",
            [(_CREATED, f"/tmp/x{index}", str(index)) for index in range(2000)],
        )
    with db.storage_write_transaction() as conn:
        conn.execute("DELETE FROM storage_cleanup_events")
    moment = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
    maintenance = _maintenance(db, repo, policy_path=policy_file, now=moment)
    ran = maintenance._maybe_vacuum()
    assert ran is True
    assert db.get_storage_vacuum_at("owner") is not None
    # A second call inside the weekly interval does nothing.
    assert maintenance._maybe_vacuum() is False


def test_gate_scratch_sweep_removes_only_stale(tmp_path: Path) -> None:
    """Covers: ADR-0003 D4 -- scratch sweep uses real liveness/age, bounded root."""
    from saberops.gate_runner import sweep_gate_scratch

    root = tmp_path / "shm"
    root.mkdir()
    dead = root / "orchestrator-gate-dead"
    dead.mkdir()
    (dead / ".saberops-scratch-owner.json").write_text(
        json.dumps({"pid": 999_999_999}), encoding="utf-8"
    )
    live = root / "orchestrator-gate-live"
    live.mkdir()
    (live / ".saberops-scratch-owner.json").write_text(
        json.dumps({"pid": os.getpid()}), encoding="utf-8"
    )
    unrelated = root / "not-a-scratch-dir"
    unrelated.mkdir()
    removed = sweep_gate_scratch(root, now=datetime(2026, 10, 17, 12, 0, tzinfo=UTC))
    assert str(dead) in removed
    assert live.exists()
    assert unrelated.exists()


def test_gate_scratch_sweep_never_adopts_unowned_by_age(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 1 §2 -- age/name is never removal authority."""
    from saberops.gate_runner import sweep_gate_scratch

    root = tmp_path / "shm"
    root.mkdir()
    unowned = root / "orchestrator-gate-unowned"
    unowned.mkdir()
    now = datetime.now(UTC)
    assert sweep_gate_scratch(root, now=now) == ()
    assert unowned.exists()
    later = now + timedelta(days=30)
    assert sweep_gate_scratch(root, now=later) == ()
    assert unowned.exists()


def test_gate_scratch_sweep_preserves_live_owner_regardless_of_age(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 1 §2 -- a live owner is never swept by age."""
    import os
    import time

    from saberops.gate_runner import sweep_gate_scratch

    root = tmp_path / "shm"
    root.mkdir()
    live = root / "orchestrator-gate-live"
    live.mkdir()
    (live / ".saberops-scratch-owner.json").write_text(
        json.dumps({"pid": os.getpid()}), encoding="utf-8"
    )
    old = time.time() - 60 * 60 * 48
    os.utime(live, (old, old))
    assert sweep_gate_scratch(root) == ()
    assert live.exists()


def test_gate_scratch_sweep_skips_symlink_entries(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 1 §2 -- symlinked scratch is never followed."""
    from saberops.gate_runner import sweep_gate_scratch

    root = tmp_path / "shm"
    root.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (external / "sentinel.txt").write_text("keep\n", encoding="utf-8")
    link = root / "orchestrator-gate-linked"
    link.symlink_to(external, target_is_directory=True)
    assert sweep_gate_scratch(root) == ()
    assert external.exists() and (external / "sentinel.txt").exists()


def test_gate_scratch_sweep_removes_pid_reuse_only_when_identity_dead(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 1 §2 -- a reused PID is not a live owner."""
    import os

    from saberops.gate_runner import sweep_gate_scratch

    root = tmp_path / "shm"
    root.mkdir()
    # A live PID recorded without a durable identity must NOT be swept.
    bare_live = root / "orchestrator-gate-bare-live"
    bare_live.mkdir()
    (bare_live / ".saberops-scratch-owner.json").write_text(
        json.dumps({"pid": os.getpid()}), encoding="utf-8"
    )
    # A dead PID (no identity) is provably not running and is disposable.
    dead = root / "orchestrator-gate-dead"
    dead.mkdir()
    (dead / ".saberops-scratch-owner.json").write_text(
        json.dumps({"pid": 999_999_999}), encoding="utf-8"
    )
    removed = sweep_gate_scratch(root)
    assert bare_live.exists()
    assert str(dead) in removed


def test_maintenance_without_approved_policy_never_sweeps_dead_scratch(
    tmp_path: Path,
) -> None:
    """Covers: ADR-0003 D4 / D6 -- unapproved policy never sweeps dead scratch."""
    import json as _json

    repo = _repo(tmp_path)
    db = Database(tmp_path / "orch.db")
    state_root = tmp_path / "state"
    scratch_root = state_root / "gate-tmp"
    scratch_root.mkdir(parents=True)
    dead = scratch_root / "orchestrator-gate-dead"
    dead.mkdir()
    (dead / ".saberops-scratch-owner.json").write_text(
        _json.dumps({"pid": 999_999_999}), encoding="utf-8"
    )
    # Absent standing policy: report-only, no sweep.
    maintenance = StorageMaintenance(
        db,
        repo_root=repo,
        policy_path=None,
        state_root=state_root,
        now_fn=lambda: datetime(2026, 10, 17, 12, 0, tzinfo=UTC),
    )
    result = maintenance.run(MaintenanceTrigger.PERIODIC)
    assert result.telemetry["gate_scratch_swept"] == "0"
    assert dead.exists()
    # Explicit unapproved policy file: still no sweep.
    denied = tmp_path / "denied.json"
    denied.write_text(_json.dumps({"approved": False}), encoding="utf-8")
    maintenance_denied = StorageMaintenance(
        db,
        repo_root=repo,
        policy_path=denied,
        state_root=state_root,
        now_fn=lambda: datetime(2026, 10, 17, 12, 0, tzinfo=UTC),
    )
    result_denied = maintenance_denied.run(MaintenanceTrigger.PERIODIC)
    assert result_denied.telemetry["gate_scratch_swept"] == "0"
    assert dead.exists()


def test_maintenance_exhausted_budget_skips_vacuum_and_sweep(tmp_path: Path) -> None:
    """Covers: ADR-0003 D4 -- an expensive GC phase cannot spill past budget."""
    import json as _json

    repo = _repo(tmp_path)
    db = Database(tmp_path / "orch.db")
    wt = GitManager.get_default_worktree_base(repo) / "run1" / "a1"
    GitManager.create_worktree(repo, "orch/run1/a1", wt)
    _terminal_run(db, repo, "run1", wt, "orch/run1/a1")
    policy_file = tmp_path / "gc.json"
    policy_file.write_text(
        _json.dumps(
            {
                "approved": True,
                "allow_worktree_retirement": True,
                "allow_vacuum": True,
                "allow_scratch_deletion": True,
            }
        ),
        encoding="utf-8",
    )
    state_root = tmp_path / "state"
    (state_root / "gate-tmp").mkdir(parents=True)
    budget = 20.0
    clock_now = [0.0]

    def _clock() -> float:
        return clock_now[0]

    maintenance = StorageMaintenance(
        db,
        repo_root=repo,
        policy_path=policy_file,
        state_root=state_root,
        tick_budget_seconds=budget,
        clock=_clock,
        now_fn=lambda: datetime(2026, 10, 17, 12, 0, tzinfo=UTC),
    )
    calls = {"vacuum": 0, "sweep": 0}

    def _fake_vacuum() -> bool:
        calls["vacuum"] += 1
        return True

    def _fake_sweep(*, deadline: float | None = None) -> tuple[str, ...]:
        calls["sweep"] += 1
        return ()

    maintenance._maybe_vacuum = _fake_vacuum  # type: ignore[method-assign]
    maintenance._sweep_gate_scratch = _fake_sweep  # type: ignore[method-assign]

    import unittest.mock as _mock

    from saberops.storage_gc import apply_gc as _real_apply_gc

    def _expensive_apply_gc(preview: object, selection: object, **kwargs: object) -> object:
        clock_now[0] = budget + 5.0
        return _real_apply_gc(preview, selection, **kwargs)  # type: ignore[arg-type]

    with _mock.patch(
        "saberops.storage_maintenance.apply_gc", side_effect=_expensive_apply_gc
    ):
        result = maintenance.run(MaintenanceTrigger.PERIODIC)
    assert calls == {"vacuum": 0, "sweep": 0}
    assert result.vacuumed is False
    assert result.telemetry["gate_scratch_swept"] == "0"
