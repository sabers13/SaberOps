"""R3+R4 union contract: verified-ownership resolution (R4) meets create-only
checkpoint-ref anchoring (R3) through the real preemption protocol.

Covers: ADR-0002 D8 (Step001 R3+R4 compatibility join).

Bridge scope only: each side's unit/adversarial behavior is proved in
``tests/test_checkpoint_ref_safety.py`` (R3, 12 cases) and
``tests/test_preemption.py`` (R4, incl. 8 ownership cases).  These cases prove
the *combined* boundary through ``preemption.complete_preemption``, which
neither lane exercises:

* a newer dirty snapshot resolved by R4 ownership proof is published by the
  R3 create-only CAS as a genuine direct anchor carrying capsule/resume
  identity, with same-SHA reuse staying a non-duplicating no-op;
* a malicious symbolic ref (live or dangling) at the allocated checkpoint
  name fails the full protocol closed: no user branch moves, the run is not
  terminalized, the job is not requeued, and no durable anchor is fabricated;
* a recorded attempt worktree that can no longer be proven (removed /
  deregistered path) declines with ``no_anchor`` even when a recorded anchor
  exists -- no stale-S fallback -- and leaves no capsule or intent behind.

Hermetic by construction: every case builds real isolated ``tmp_path`` Git
repositories driven via the local ``git`` binary and a file-backed
:class:`Database`.  No remotes, no network, no provider/model/credential/
billing state.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from saberops import preemption
from saberops.db import Database
from saberops.git import (
    CandidatePublicationError,
    GitManager,
)
from saberops.jobs import store
from saberops.jobs.models import (
    Job,
    JobRequirements,
    JobRole,
    JobState,
    Priority,
    WaitingReason,
)
from saberops.models import (
    Attempt,
    AttemptStatus,
    Run,
    RunStatus,
    Tier,
    WorkerInterruptionReason,
)

_NOW = datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC)
_QUOTA = WorkerInterruptionReason.QUOTA_EXHAUSTED.value


def _git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


def _repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    _git(["init"], path)
    _git(["config", "user.email", "test@example.com"], path)
    _git(["config", "user.name", "test"], path)
    (path / "base.txt").write_text("base\n", encoding="utf-8")
    _git(["add", "-A"], path)
    _git(["commit", "-m", "base"], path)
    return _git(["rev-parse", "HEAD"], path)


def _db(tmp_path: Path, name: str = "union.db") -> Database:
    return Database(tmp_path / name)


def _job(
    job_id: str,
    repo: str,
    base_sha: str,
    *,
    state: JobState = JobState.QUEUED,
    waiting: WaitingReason | None = WaitingReason.NEW,
    run_id: str | None = None,
    preemptions: int = 0,
) -> Job:
    return Job(
        job_id=job_id,
        idempotency_key=None,
        caller_id="owner",
        repo=repo,
        base_ref="HEAD",
        base_sha=base_sha,
        goal="do the thing",
        acceptance=None,
        requirements=JobRequirements(role=JobRole.WORKER),
        priority=Priority.NORMAL,
        state=state,
        waiting_reason=waiting,
        current_run_id=run_id,
        run_ids=(run_id,) if run_id else (),
        preemptions=preemptions,
        created_at=_NOW.isoformat(),
        updated_at=_NOW.isoformat(),
        request_digest="digest-" + job_id,
    )


def _run(run_id: str, repo: str, base: str, status: RunStatus = RunStatus.RUNNING) -> Run:
    return Run(
        id=run_id,
        task="do the thing",
        target_repo=repo,
        base_commit=base,
        tier=Tier.T2,
        training_allowed=True,
        gate_command="make gate",
        status=status,
        created_at=_NOW.isoformat(),
        config_json="{}",
    )


def _quota_evidence(db: Database, run_id: str) -> None:
    db.record_event(
        run_id,
        "quota_exhaustion_recorded",
        payload={
            "provider": "synthetic-provider",
            "pool": "synthetic-pool",
            "until": "2026-10-16T00:00:00+00:00",
            "until_confidence": "EXACT",
        },
    )
    db.record_event(
        run_id,
        "no_eligible_candidate",
        payload={"outcome": "NO_ELIGIBLE_CANDIDATE"},
    )


def _dispatched(db: Database, repo: str, base: str, job_id: str, run_id: str) -> None:
    store.insert_job(db, _job(job_id, repo, base), now=_NOW)
    store.mark_dispatched(db, job_id, run_id, now=_NOW)
    db.create_run(_run(run_id, repo, base))


def _seed_quota_attempt(
    db: Database,
    run_id: str,
    repo: str,
    num: int = 1,
    *,
    worktree: str | Path | None = None,
) -> None:
    db.create_attempt(
        Attempt(
            id=f"{run_id}_a{num}",
            run_id=run_id,
            attempt_number=num,
            provider="synthetic-provider",
            model="synthetic-model",
            worktree_path=str(worktree) if worktree is not None else repo,
            branch_name=f"orch/{run_id}/a{num}",
            status=AttemptStatus.FAILED,
            created_at=_NOW.isoformat(),
            completed_at=_NOW.isoformat(),
            interruption_reason=_QUOTA,
        )
    )


def test_union_newer_wip_snapshot_anchors_direct_ref_with_capsule_and_reuse(
    tmp_path: Path,
) -> None:
    """Covers: ADR-0002 D8 R4-resolved newer WIP is published by the R3 CAS."""
    repo = tmp_path / "repo"
    base = _repo(repo)
    db = _db(tmp_path)

    # Older generation: dirty WIP in a verified linked worktree preempts and
    # anchors the stale recorded anchor S at index 0.
    wt_old = tmp_path / "wt_old"
    GitManager.create_worktree(repo, "wt-old", wt_old, start_point=base)
    (wt_old / "old.txt").write_text("older wip\n", encoding="utf-8")
    _dispatched(db, str(repo), base, "job1", "run_old")
    _seed_quota_attempt(db, "run_old", str(repo), worktree=wt_old)
    _quota_evidence(db, "run_old")
    first = preemption.complete_preemption(
        db, run_id="run_old", repo_path=repo,
        worktree_path=str(wt_old), base_sha=base, now=_NOW,
    )
    assert first.owned and first.checkpoint_sha is not None
    recorded = first.checkpoint_sha
    assert GitManager.read_checkpoint_ref(repo, "job1", 0) == recorded
    job = store.get_job(db, "job1")
    assert job is not None

    # New generation stages S, commits clean T on top, then leaves dirty WIP.
    wt_new = tmp_path / "wt_new"
    GitManager.create_worktree(repo, "wt-new", wt_new, start_point=recorded)
    db.create_run(_run("run_new", str(repo), base, status=RunStatus.PENDING))
    prep = preemption.prepare_resume(db, job=job, new_run_id="run_new", now=_NOW)
    assert prep.resumed and prep.checkpoint_sha == recorded
    db.update_run(run_id="run_new", status=RunStatus.RUNNING)
    _seed_quota_attempt(db, "run_new", str(repo), worktree=wt_new)
    store.mark_dispatched(db, "job1", "run_new", now=_NOW)
    _quota_evidence(db, "run_new")
    (wt_new / "newer.txt").write_text("newer\n", encoding="utf-8")
    _git(["add", "-A"], wt_new)
    _git(["commit", "-m", "newer work"], wt_new)
    newer = _git(["rev-parse", "HEAD"], wt_new)
    assert newer != recorded and GitManager.is_ancestor(repo, recorded, newer)
    (wt_new / "dirty.txt").write_text("newer dirty\n", encoding="utf-8")

    out = preemption.complete_preemption(
        db, run_id="run_new", repo_path=repo, base_sha=base, now=_NOW
    )

    # R4 chose the newest trusted snapshot; R3 published it as a genuine
    # direct anchor with capsule/resume identity (union obligation 1).
    assert out.owned and out.detail == "preempted"
    assert out.checkpoint_sha is not None
    assert out.checkpoint_sha != recorded and out.checkpoint_sha != newer
    assert GitManager.is_ancestor(repo, newer, out.checkpoint_sha)
    assert GitManager.read_checkpoint_ref(repo, "job1", 1) == out.checkpoint_sha
    assert GitManager.list_checkpoint_refs(repo, "job1") == {
        0: recorded,
        1: out.checkpoint_sha,
    }
    assert GitManager.read_checkpoint_ref(repo, "job1", 0) == recorded
    assert out.checkpoint_ref == "refs/saberops/checkpoints/job1/1"
    assert out.handoff_id is not None
    capsule = db.get_handoff(out.handoff_id)
    assert capsule is not None and capsule.run_id == "run_new"
    assert _git(["show", f"{out.checkpoint_sha}:newer.txt"], repo) == "newer"
    assert _git(["show", f"{out.checkpoint_sha}:dirty.txt"], repo) == "newer dirty"
    requeued = store.get_job(db, "job1")
    assert requeued is not None and requeued.resume_from is not None
    assert requeued.resume_from.checkpoint_sha == out.checkpoint_sha

    # Same-SHA direct-anchor reuse stays a non-duplicating no-op.
    ref_again, idx_again = preemption._anchor_verified_ref(
        repo, job_id="job1", checkpoint_sha=out.checkpoint_sha
    )
    assert (ref_again, idx_again) == (out.checkpoint_ref, 1)
    assert GitManager.list_checkpoint_refs(repo, "job1") == {
        0: recorded,
        1: out.checkpoint_sha,
    }


def test_union_symref_at_allocated_index_fails_protocol_closed(
    tmp_path: Path,
) -> None:
    """Covers: ADR-0002 D8 symref at the checkpoint name fails preemption closed."""
    repo = tmp_path / "repo"
    base = _repo(repo)
    _git(["branch", "valuable"], repo)
    valuable = _git(["rev-parse", "refs/heads/valuable"], repo)

    # Malicious live symref planted exactly at the index the protocol would
    # allocate next.
    _git(
        ["symbolic-ref", "refs/saberops/checkpoints/job1/0", "refs/heads/valuable"],
        repo,
    )

    (repo / "wip.txt").write_text("wip\n", encoding="utf-8")
    db = _db(tmp_path)
    _dispatched(db, str(repo), base, "job1", "run_old")
    _seed_quota_attempt(db, "run_old", str(repo), worktree=repo)
    _quota_evidence(db, "run_old")

    out = preemption.complete_preemption(
        db, run_id="run_old", repo_path=repo, base_sha=base, now=_NOW
    )

    # No unsafe success/terminal preemption/requeue or fabricated anchor.
    assert not out.owned and out.detail == "ref_failed"
    assert _git(["rev-parse", "refs/heads/valuable"], repo) == valuable
    assert (
        repo / ".git" / "refs/saberops/checkpoints/job1/0"
    ).read_text(encoding="utf-8").strip() == "ref: refs/heads/valuable"
    with pytest.raises(CandidatePublicationError):
        GitManager.list_checkpoint_refs(repo, "job1")
    with pytest.raises(CandidatePublicationError):
        GitManager.read_checkpoint_ref(repo, "job1", 0)
    run = db.get_run("run_old")
    assert run is not None and run.status == RunStatus.RUNNING
    job = store.get_job(db, "job1")
    assert job is not None and job.state == JobState.RUNNING
    assert job.current_run_id == "run_old" and job.preemptions == 0
    assert db.get_events_by_type("run_old", "preemption_committed") == []


def test_union_dangling_symref_at_allocated_index_fails_protocol_closed(
    tmp_path: Path,
) -> None:
    """Covers: ADR-0002 D8 a dangling symref is not converted into an anchor."""
    repo = tmp_path / "repo"
    base = _repo(repo)

    # Dangling symref at the allocated name: no referent branch may be
    # created and no direct anchor may be reported.
    _git(
        ["symbolic-ref", "refs/saberops/checkpoints/job1/0", "refs/heads/ghost"],
        repo,
    )

    (repo / "wip.txt").write_text("wip\n", encoding="utf-8")
    db = _db(tmp_path)
    _dispatched(db, str(repo), base, "job1", "run_old")
    _seed_quota_attempt(db, "run_old", str(repo), worktree=repo)
    _quota_evidence(db, "run_old")

    out = preemption.complete_preemption(
        db, run_id="run_old", repo_path=repo, base_sha=base, now=_NOW
    )

    assert not out.owned and out.detail == "ref_failed"
    ghost = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", "refs/heads/ghost"],
        cwd=repo,
        capture_output=True,
    )
    assert ghost.returncode != 0
    with pytest.raises(CandidatePublicationError):
        GitManager.read_checkpoint_ref(repo, "job1", 0)
    run = db.get_run("run_old")
    assert run is not None and run.status == RunStatus.RUNNING
    job = store.get_job(db, "job1")
    assert job is not None and job.state == JobState.RUNNING
    assert db.get_events_by_type("run_old", "preemption_committed") == []


def test_union_deregistered_hint_with_recorded_anchor_declines_without_fallback(
    tmp_path: Path,
) -> None:
    """Covers: ADR-0002 D8 unverifiable hint + recorded S declines, no stale reuse."""
    repo = tmp_path / "repo"
    base = _repo(repo)
    db = _db(tmp_path)

    wt_old = tmp_path / "wt_old"
    GitManager.create_worktree(repo, "wt-old", wt_old, start_point=base)
    (wt_old / "old.txt").write_text("older wip\n", encoding="utf-8")
    _dispatched(db, str(repo), base, "job1", "run_old")
    _seed_quota_attempt(db, "run_old", str(repo), worktree=wt_old)
    _quota_evidence(db, "run_old")
    first = preemption.complete_preemption(
        db, run_id="run_old", repo_path=repo,
        worktree_path=str(wt_old), base_sha=base, now=_NOW,
    )
    assert first.owned and first.checkpoint_sha is not None
    recorded = first.checkpoint_sha
    job = store.get_job(db, "job1")
    assert job is not None

    # The new generation's attempt worktree is retired before preemption runs,
    # so the recorded hint can no longer be proven -- ownership is ambiguous.
    wt_new = tmp_path / "wt_new"
    GitManager.create_worktree(repo, "wt-new", wt_new, start_point=recorded)
    db.create_run(_run("run_new", str(repo), base, status=RunStatus.PENDING))
    prep = preemption.prepare_resume(db, job=job, new_run_id="run_new", now=_NOW)
    assert prep.resumed and prep.checkpoint_sha == recorded
    db.update_run(run_id="run_new", status=RunStatus.RUNNING)
    _seed_quota_attempt(db, "run_new", str(repo), worktree=wt_new)
    store.mark_dispatched(db, "job1", "run_new", now=_NOW)
    _quota_evidence(db, "run_new")
    _git(["worktree", "remove", "--force", str(wt_new)], repo)
    assert not wt_new.exists()

    out = preemption.complete_preemption(
        db, run_id="run_new", repo_path=repo, base_sha=base, now=_NOW
    )

    # Fail closed with no stale-S fallback and no protocol side effects.
    assert not out.owned and out.detail == "no_anchor"
    assert GitManager.read_checkpoint_ref(repo, "job1", 0) == recorded
    assert GitManager.read_checkpoint_ref(repo, "job1", 1) is None
    assert GitManager.list_checkpoint_refs(repo, "job1") == {0: recorded}
    assert db.get_events_by_type("run_new", "preemption_committed") == []
    assert db.get_handoffs_for_run("run_new") == []
    run = db.get_run("run_new")
    assert run is not None and run.status == RunStatus.RUNNING
    still = store.get_job(db, "job1")
    assert still is not None and still.state == JobState.RUNNING
    assert still.current_run_id == "run_new" and still.preemptions == 1
