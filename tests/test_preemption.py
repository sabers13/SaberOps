"""Deterministic adversarial regression tests for D8 preemption (M2-5 Step 001).

Hermetic by construction: every test uses an isolated ``tmp_path`` git
repository (local ``git`` binary only, no remotes, no network), an isolated
file-backed :class:`Database`, synthetic quota evidence recorded directly as
durable run events, and a frozen clock.  No provider, model, credential,
billing, or owner-config state is touched.
"""

from __future__ import annotations

import subprocess
from dataclasses import replace as dc_replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from saberops import preemption
from saberops.capacity.ledger import PoolDraw, release, reserve
from saberops.db import Database
from saberops.git import (
    MAX_CHECKPOINT_INDEX,
    CandidatePublicationError,
    GitManager,
    validate_checkpoint_ref,
)
from saberops.jobs import service, store
from saberops.jobs.models import (
    FAILURE_REASONS,
    PREEMPTION_LIMIT,
    Job,
    JobRequirements,
    JobRole,
    JobState,
    Priority,
    WaitingReason,
)
from saberops.models import (
    TERMINAL_RUN_STATUSES,
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


def _db(tmp_path: Path, name: str = "t.db") -> Database:
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


def _quota_evidence(
    db: Database,
    run_id: str,
    *,
    until: str | None = "2026-10-16T00:00:00+00:00",
    confidence: str = "EXACT",
    no_eligible: bool = True,
) -> None:
    db.record_event(
        run_id,
        "quota_exhaustion_recorded",
        payload={
            "provider": "synthetic-provider",
            "pool": "synthetic-pool",
            "until": until,
            "until_confidence": confidence,
        },
    )
    if no_eligible:
        db.record_event(
            run_id,
            "no_eligible_candidate",
            payload={"outcome": "NO_ELIGIBLE_CANDIDATE"},
        )


def _dispatched(db: Database, repo: str, base: str, job_id: str, run_id: str) -> None:
    store.insert_job(db, _job(job_id, repo, base), now=_NOW)
    store.mark_dispatched(db, job_id, run_id, now=_NOW)
    db.create_run(_run(run_id, repo, base))


def _stage_run(db: Database, repo: str, base: str, run_id: str) -> None:
    """Create the fresh run row, mirroring launcher order (row before resume)."""
    db.create_run(_run(run_id, repo, base, status=RunStatus.PENDING))


def _seed_quota_attempt(db: Database, run_id: str, repo: str, num: int = 1) -> None:
    db.create_attempt(
        Attempt(
            id=f"{run_id}_a{num}",
            run_id=run_id,
            attempt_number=num,
            provider="synthetic-provider",
            model="synthetic-model",
            worktree_path=repo,
            branch_name=f"orch/{run_id}/a{num}",
            status=AttemptStatus.FAILED,
            created_at=_NOW.isoformat(),
            completed_at=_NOW.isoformat(),
            interruption_reason=_QUOTA,
        )
    )


# ---------------------------------------------------------------------------
# Git checkpoint refs
# ---------------------------------------------------------------------------


def test_validate_checkpoint_ref_shape(tmp_path: Path) -> None:
    """Covers: ADR-0002 D8 engine-owned ref namespace + injection guard."""
    assert (
        validate_checkpoint_ref("job_abc-123", 0)
        == "refs/saberops/checkpoints/job_abc-123/0"
    )
    for bad_job in [
        "../escape",
        "a/b",
        "job.lock",
        "",
        "x" * 65,
        "job id",
        "job@{1}",
        "job:thing",
        "job~1",
    ]:
        with pytest.raises(CandidatePublicationError):
            validate_checkpoint_ref(bad_job, 0)
    for bad_index in [-1, MAX_CHECKPOINT_INDEX + 1, True, "0", None]:
        with pytest.raises(CandidatePublicationError):
            validate_checkpoint_ref("job1", bad_index)


def test_anchor_read_list_roundtrip(tmp_path: Path) -> None:
    """Covers: ADR-0002 D8 anchor + verify + monotonic bounded index."""
    repo = tmp_path / "repo"
    base = _repo(repo)
    ref0 = GitManager.anchor_checkpoint_ref(repo, "job1", 0, base)
    assert ref0 == "refs/saberops/checkpoints/job1/0"
    assert GitManager.read_checkpoint_ref(repo, "job1", 0) == base
    assert GitManager.read_checkpoint_ref(repo, "job1", 1) is None
    assert GitManager.list_checkpoint_refs(repo, "job1") == {0: base}
    # Same SHA reuses its index instead of spraying a duplicate ref.
    ref_again, idx_again = preemption._anchor_verified_ref(
        repo, job_id="job1", checkpoint_sha=base
    )
    assert (ref_again, idx_again) == (ref0, 0)
    # A new SHA takes the next monotonic index.
    (repo / "wip.txt").write_text("wip\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-m", "wip"], repo)
    wip = _git(["rev-parse", "HEAD"], repo)
    ref1, idx1 = preemption._anchor_verified_ref(
        repo, job_id="job1", checkpoint_sha=wip
    )
    assert (ref1, idx1) == ("refs/saberops/checkpoints/job1/1", 1)
    assert GitManager.commit_exists(repo, wip)
    assert not GitManager.commit_exists(repo, "0" * 40)


def test_anchor_rejects_malformed_sha_and_job(tmp_path: Path) -> None:
    """Covers: prompt §3.7 failed Git ref + forbidden ref name fail closed."""
    repo = tmp_path / "repo"
    _repo(repo)
    with pytest.raises(CandidatePublicationError):
        GitManager.anchor_checkpoint_ref(repo, "job1", 0, "not-a-sha")
    with pytest.raises(CandidatePublicationError):
        GitManager.anchor_checkpoint_ref(repo, "../../evil", 0, "0" * 40)
    with pytest.raises(CandidatePublicationError):
        GitManager.list_checkpoint_refs(repo, "../../evil")


# ---------------------------------------------------------------------------
# Full preemption protocol
# ---------------------------------------------------------------------------


def test_exhausted_route_preempts_with_capsule_and_ref(tmp_path: Path) -> None:
    """Covers: prompt §3.1 exhausted route + no group + checkpoint -> PREEMPTED."""
    repo = tmp_path / "repo"
    base = _repo(repo)
    db = _db(tmp_path)
    _dispatched(db, str(repo), base, "job1", "run_old")
    _quota_evidence(db, "run_old")

    out = preemption.complete_preemption(
        db, run_id="run_old", repo_path=repo, base_sha=base, now=_NOW
    )

    assert out.owned and out.detail == "preempted"
    assert out.status == RunStatus.PREEMPTED
    assert out.checkpoint_sha == base
    assert out.checkpoint_ref == "refs/saberops/checkpoints/job1/0"
    assert GitManager.read_checkpoint_ref(repo, "job1", 0) == base
    assert out.handoff_id is not None
    capsule = db.get_handoff(out.handoff_id)
    assert capsule is not None and capsule.run_id == "run_old"
    assert db.get_run("run_old").status == RunStatus.PREEMPTED  # type: ignore[union-attr]
    job = store.get_job(db, "job1")
    assert job is not None
    assert job.state == JobState.QUEUED
    assert job.waiting_reason == WaitingReason.PREEMPTED
    assert job.preemptions == 1
    assert job.current_run_id is None
    assert job.resume_from is not None
    assert job.resume_from.run_id == "run_old"
    assert job.resume_from.checkpoint_sha == base
    assert job.resume_from.handoff_id == out.handoff_id
    assert job.not_before == "2026-10-16T00:00:00+00:00"
    assert out.preemptions == 1
    # Preempted run is terminal everywhere, job stays queued for re-dispatch.
    assert RunStatus.PREEMPTED in TERMINAL_RUN_STATUSES


def test_wip_checkpoint_commit_is_anchored_not_fabricated(tmp_path: Path) -> None:
    """Covers: prompt §3.1 immutable capsule+resume + exact SHA anchoring."""
    repo = tmp_path / "repo"
    base = _repo(repo)
    wt = tmp_path / "wt"
    GitManager.create_worktree(repo, "wt-wip-1", wt, start_point=base)
    (wt / "wip.txt").write_text("partial work\n", encoding="utf-8")
    db = _db(tmp_path)
    _dispatched(db, str(repo), base, "job1", "run_old")
    _quota_evidence(db, "run_old")

    out = preemption.complete_preemption(
        db, run_id="run_old", repo_path=repo,
        worktree_path=str(wt), base_sha=base, now=_NOW,
    )

    assert out.owned
    assert out.checkpoint_sha is not None and out.checkpoint_sha != base
    assert GitManager.commit_exists(repo, out.checkpoint_sha)
    assert GitManager.read_checkpoint_ref(repo, "job1", 0) == out.checkpoint_sha
    # The anchored commit really contains the WIP content.
    content = _git(["show", f"{out.checkpoint_sha}:wip.txt"], repo)
    assert content == "partial work"


def test_checkpoint_ref_survives_worktree_retirement_and_gc(tmp_path: Path) -> None:
    """Covers: prompt §3.2 checkpoint reachable after retirement/GC simulation."""
    repo = tmp_path / "repo"
    base = _repo(repo)
    wt = tmp_path / "wt"
    GitManager.create_worktree(repo, "wt-wip-2", wt, start_point=base)
    (wt / "wip.txt").write_text("partial\n", encoding="utf-8")
    db = _db(tmp_path)
    _dispatched(db, str(repo), base, "job1", "run_old")
    _quota_evidence(db, "run_old")
    out = preemption.complete_preemption(
        db, run_id="run_old", repo_path=repo,
        worktree_path=str(wt), base_sha=base, now=_NOW,
    )
    assert out.owned and out.checkpoint_sha is not None

    GitManager.remove_worktree(repo, wt, worktree_base=tmp_path)
    _git(["gc", "--quiet"], repo)

    assert GitManager.read_checkpoint_ref(repo, "job1", 0) == out.checkpoint_sha
    assert GitManager.commit_exists(repo, out.checkpoint_sha)


def test_empty_worktree_anchors_real_head(tmp_path: Path) -> None:
    """Covers: prompt §3.2 empty checkpoint anchors HEAD, never a fake hash."""
    repo = tmp_path / "repo"
    base = _repo(repo)
    db = _db(tmp_path)
    _dispatched(db, str(repo), base, "job1", "run_old")
    _quota_evidence(db, "run_old")

    out = preemption.complete_preemption(
        db, run_id="run_old", repo_path=repo,
        worktree_path=str(repo), base_sha=base, now=_NOW,
    )

    assert out.owned and out.checkpoint_sha == base


def test_reset_confidence_gating(tmp_path: Path) -> None:
    """Covers: prompt §3.3 EXACT/PARSED vs UNKNOWN/HINT reset handling."""
    repo = tmp_path / "repo"
    base = _repo(repo)
    cases: list[tuple[str | None, str, str | None]] = [
        ("2026-10-16T00:00:00+00:00", "EXACT", "2026-10-16T00:00:00+00:00"),
        ("2026-10-17T00:00:00+00:00", "PARSED", "2026-10-17T00:00:00+00:00"),
        ("2026-10-18T00:00:00+00:00", "HINT", None),
        ("2026-10-19T00:00:00+00:00", "UNKNOWN", None),
        (None, "EXACT", None),
        ("not-an-instant", "EXACT", None),
        ("2026-10-20 00:00:00", "EXACT", None),  # naive: no timezone, no ETA
    ]
    for idx, (until, confidence, expected) in enumerate(cases):
        db = _db(tmp_path, name=f"reset{idx}.db")
        jid, rid = f"job{idx}", f"run{idx}"
        _dispatched(db, str(repo), base, jid, rid)
        _quota_evidence(db, rid, until=until, confidence=confidence)
        out = preemption.complete_preemption(
            db, run_id=rid, repo_path=repo, base_sha=base, now=_NOW
        )
        assert out.owned, (idx, out.detail)
        assert out.not_before == expected, (idx, until, confidence)
        job = store.get_job(db, jid)
        assert job is not None and job.not_before == expected


def test_repeated_quota_event_counts_exactly_once(tmp_path: Path) -> None:
    """Covers: prompt §3.5 idempotency of the repeated quota signal."""
    repo = tmp_path / "repo"
    base = _repo(repo)
    db = _db(tmp_path)
    _dispatched(db, str(repo), base, "job1", "run_old")
    _quota_evidence(db, "run_old")

    first = preemption.complete_preemption(
        db, run_id="run_old", repo_path=repo, base_sha=base, now=_NOW
    )
    second = preemption.complete_preemption(
        db, run_id="run_old", repo_path=repo, base_sha=base, now=_NOW
    )
    third = preemption.complete_preemption(
        db, run_id="run_old", repo_path=repo, base_sha=base, now=_NOW
    )

    assert first.owned and first.detail == "preempted"
    assert second.owned and second.detail == "already_preempted"
    assert third.owned and third.detail == "already_preempted"
    job = store.get_job(db, "job1")
    assert job is not None and job.preemptions == 1
    assert GitManager.list_checkpoint_refs(repo, "job1") == {
        0: first.checkpoint_sha
    }
    intents = db.get_events_by_type("run_old", "preemption_committed")
    assert len(intents) == 1


def test_preemption_bound_five_then_limit(tmp_path: Path) -> None:
    """Covers: prompt §3.5 default cap 5; 6th denied as PREEMPTION_LIMIT."""
    assert preemption.DEFAULT_MAX_PREEMPTIONS == 5
    repo = tmp_path / "repo"
    base = _repo(repo)
    db = _db(tmp_path)
    store.insert_job(db, _job("job1", str(repo), base, preemptions=5), now=_NOW)
    store.mark_dispatched(db, "job1", "run_old", now=_NOW)
    db.create_run(_run("run_old", str(repo), base))
    _quota_evidence(db, "run_old")

    out = preemption.complete_preemption(
        db, run_id="run_old", repo_path=repo, base_sha=base, now=_NOW
    )

    assert out.owned and out.limit_reached
    assert out.status == RunStatus.FAILED
    assert out.detail == "preemption_limit"
    assert db.get_run("run_old").status == RunStatus.FAILED  # type: ignore[union-attr]
    job = store.get_job(db, "job1")
    assert job is not None
    assert job.state == JobState.FAILED
    assert job.failure_reason == PREEMPTION_LIMIT
    assert job.preemptions == 5
    # No silent 6th preemption: no resume capsule was staged.
    assert job.resume_from is None


def test_five_sequential_preemptions_then_limit(tmp_path: Path) -> None:
    """Covers: prompt §3.5 exactly 5 permitted across re-dispatch generations."""
    repo = tmp_path / "repo"
    base = _repo(repo)
    db = _db(tmp_path)
    store.insert_job(db, _job("job1", str(repo), base), now=_NOW)
    for gen in range(5):
        rid = f"run_g{gen}"
        store.mark_dispatched(db, "job1", rid, now=_NOW)
        db.create_run(_run(rid, str(repo), base))
        _quota_evidence(db, rid)
        out = preemption.complete_preemption(
            db, run_id=rid, repo_path=repo, base_sha=base, now=_NOW
        )
        assert out.owned and out.status == RunStatus.PREEMPTED, (gen, out.detail)
    job = store.get_job(db, "job1")
    assert job is not None and job.preemptions == 5
    store.mark_dispatched(db, "job1", "run_g5", now=_NOW)
    db.create_run(_run("run_g5", str(repo), base))
    _quota_evidence(db, "run_g5")
    denied = preemption.complete_preemption(
        db, run_id="run_g5", repo_path=repo, base_sha=base, now=_NOW
    )
    assert denied.owned and denied.limit_reached
    assert store.get_job(db, "job1").state == JobState.FAILED  # type: ignore[union-attr]


def test_standalone_run_keeps_truthful_behavior(tmp_path: Path) -> None:
    """Covers: prompt §3.8 standalone run with no job is never preempted."""
    repo = tmp_path / "repo"
    base = _repo(repo)
    db = _db(tmp_path)
    db.create_run(_run("run_solo", str(repo), base))
    _quota_evidence(db, "run_solo")

    out = preemption.complete_preemption(
        db, run_id="run_solo", repo_path=repo, base_sha=base, now=_NOW
    )

    assert not out.owned and out.detail == "no_job"
    assert db.get_run("run_solo").status == RunStatus.RUNNING  # type: ignore[union-attr]


def test_non_quota_exhaustion_never_preempts(tmp_path: Path) -> None:
    """Covers: prompt §3.8 ordinary errors/model failure/posture never preempt."""
    repo = tmp_path / "repo"
    base = _repo(repo)
    db = _db(tmp_path)
    _dispatched(db, str(repo), base, "job1", "run_old")
    # Ladder exhausted, but for an ordinary gate failure: no quota signal.
    db.record_event(
        "run_old", "no_eligible_candidate", payload={"outcome": "NO_ELIGIBLE_CANDIDATE"}
    )
    db.create_attempt(
        Attempt(
            id="run_old_a1", run_id="run_old", attempt_number=1,
            provider="p", model="m", worktree_path=str(repo),
            branch_name="orch/run_old/a1", status=AttemptStatus.FAILED,
            interruption_reason="GATE_FAILED",
        )
    )

    out = preemption.complete_preemption(
        db, run_id="run_old", repo_path=repo, base_sha=base, now=_NOW
    )

    assert not out.owned and out.detail == "no_quota_signal"
    assert db.get_run("run_old").status == RunStatus.RUNNING  # type: ignore[union-attr]
    assert store.get_job(db, "job1").state == JobState.RUNNING  # type: ignore[union-attr]


def test_quota_without_ladder_exhaustion_does_not_preempt(tmp_path: Path) -> None:
    """Covers: prompt §3.8 quota alone (eligible fallback remains) never preempts."""
    repo = tmp_path / "repo"
    base = _repo(repo)
    db = _db(tmp_path)
    _dispatched(db, str(repo), base, "job1", "run_old")
    _quota_evidence(db, "run_old", no_eligible=False)

    out = preemption.complete_preemption(
        db, run_id="run_old", repo_path=repo, base_sha=base, now=_NOW
    )

    assert not out.owned and out.detail == "eligible_candidate_remains"


def test_single_candidate_all_quota_attempts_counts_as_exhausted(tmp_path: Path) -> None:
    """Covers: prompt §3.1 single-route chain dying of quota has no fallback left."""
    repo = tmp_path / "repo"
    base = _repo(repo)
    db = _db(tmp_path)
    _dispatched(db, str(repo), base, "job1", "run_old")
    _seed_quota_attempt(db, "run_old", str(repo))
    # Genuine quota evidence, but the single-candidate ladder broke before a
    # skip could be recorded, so no no_eligible_candidate event exists.
    db.record_event(
        "run_old", "quota_exhaustion_recorded",
        payload={"provider": "p", "until": None, "until_confidence": "UNKNOWN"},
    )

    out = preemption.complete_preemption(
        db, run_id="run_old", repo_path=repo, base_sha=base, now=_NOW
    )

    assert out.owned and out.detail == "preempted"


def test_mixed_quota_and_gate_failure_does_not_preempt(tmp_path: Path) -> None:
    """Covers: prompt §3.8 a fallback that ran and failed gate is not exhaustion."""
    repo = tmp_path / "repo"
    base = _repo(repo)
    db = _db(tmp_path)
    _dispatched(db, str(repo), base, "job1", "run_old")
    _seed_quota_attempt(db, "run_old", str(repo), num=1)
    db.create_attempt(
        Attempt(
            id="run_old_a2", run_id="run_old", attempt_number=2,
            provider="p2", model="m2", worktree_path=str(repo),
            branch_name="orch/run_old/a2", status=AttemptStatus.FAILED,
            created_at=_NOW.isoformat(), completed_at=_NOW.isoformat(),
            interruption_reason="GATE_FAILED",
        )
    )
    db.record_event(
        "run_old", "quota_exhaustion_recorded",
        payload={"provider": "p", "until": None, "until_confidence": "UNKNOWN"},
    )

    out = preemption.complete_preemption(
        db, run_id="run_old", repo_path=repo, base_sha=base, now=_NOW
    )

    assert not out.owned and out.detail == "eligible_candidate_remains"


def test_quota_then_admitted_gate_failure_with_no_eligible_event_declines(
    tmp_path: Path,
) -> None:
    """Covers: ADR-0002 D8 F1 -- a durable no_eligible_candidate event must not
    convert an admitted, gate-failing fallback into quota preemption.

    The production ladder writes ``no_eligible_candidate`` for MIXED chains
    that both launched and skipped candidates, so its mere presence is not
    proof that no route was admitted.  Genuine quota (A) followed by an
    admitted candidate that failed the gate (B) leaves the run's true verdict a
    gate failure: the coordinator must decline and the normal FAILED path must
    remain.
    """
    repo = tmp_path / "repo"
    base = _repo(repo)
    db = _db(tmp_path)
    _dispatched(db, str(repo), base, "job1", "run_old")
    # A: genuine quota interruption (attempt channel).
    _seed_quota_attempt(db, "run_old", str(repo), num=1)
    db.record_event(
        "run_old", "quota_exhaustion_recorded",
        payload={"provider": "p", "until": None, "until_confidence": "UNKNOWN"},
    )
    # B: admitted candidate that ran and failed the deterministic gate.  A gate
    # failure carries no quota interruption reason (status FAILED only).
    db.create_attempt(
        Attempt(
            id="run_old_a2", run_id="run_old", attempt_number=2,
            provider="p2", model="m2", worktree_path=str(repo),
            branch_name="orch/run_old/a2", status=AttemptStatus.FAILED,
            created_at=_NOW.isoformat(), completed_at=_NOW.isoformat(),
        )
    )
    # The ladder's own durable mixed-chain evidence.
    db.record_event(
        "run_old", "no_eligible_candidate",
        payload={"outcome": "NO_ELIGIBLE_CANDIDATE"},
    )

    # Genuine quota is unchanged; the conservative predicate is what declines.
    assert preemption.has_genuine_quota_signal(db, "run_old")
    assert not preemption.has_no_eligible_evidence(db, "run_old")

    out = preemption.complete_preemption(
        db, run_id="run_old", repo_path=repo, base_sha=base, now=_NOW
    )

    assert not out.owned and out.detail == "eligible_candidate_remains"
    assert db.get_run("run_old").status == RunStatus.RUNNING  # type: ignore[union-attr]
    assert store.get_job(db, "job1").state == JobState.RUNNING  # type: ignore[union-attr]
    assert GitManager.list_checkpoint_refs(repo, "job1") == {}


def test_earlier_quota_evidence_plus_later_admitted_failure_declines(
    tmp_path: Path,
) -> None:
    """Covers: ADR-0002 D8 F1 -- cross-generation pairing must not preempt.

    Genuine quota evidence from an *earlier* chain/package (here the durable
    ``quota_exhaustion_recorded`` event channel, with no quota attempt) must
    never be paired with a *later* chain's mixed ``no_eligible_candidate``
    event to mask an admitted candidate that failed on its merits.  The
    durable schema carries no chain identity, so the predicate is run-global
    and fails closed on this ambiguity.
    """
    repo = tmp_path / "repo"
    base = _repo(repo)
    db = _db(tmp_path)
    _dispatched(db, str(repo), base, "job1", "run_old")
    # Earlier generation/package: genuine quota, recorded as durable evidence.
    db.record_event(
        "run_old", "quota_exhaustion_recorded",
        payload={
            "provider": "pkg1-provider", "until": None,
            "until_confidence": "UNKNOWN",
        },
    )
    # Later generation/package: an admitted candidate that failed (non-quota).
    db.create_attempt(
        Attempt(
            id="run_old_a1", run_id="run_old", attempt_number=1,
            provider="pkg2-provider", model="m", worktree_path=str(repo),
            branch_name="orch/run_old/a1", status=AttemptStatus.FAILED,
            created_at=_NOW.isoformat(), completed_at=_NOW.isoformat(),
        )
    )
    db.record_event(
        "run_old", "no_eligible_candidate",
        payload={"outcome": "NO_ELIGIBLE_CANDIDATE"},
    )

    assert preemption.has_genuine_quota_signal(db, "run_old")
    assert not preemption.has_no_eligible_evidence(db, "run_old")
    ready = preemption.should_preempt(db, "run_old")
    assert not ready.eligible and ready.detail == "eligible_candidate_remains"

    out = preemption.complete_preemption(
        db, run_id="run_old", repo_path=repo, base_sha=base, now=_NOW
    )

    assert not out.owned and out.detail == "eligible_candidate_remains"
    assert db.get_run("run_old").status == RunStatus.RUNNING  # type: ignore[union-attr]


def test_admitted_timeout_attempt_blocks_quota_only_preemption(tmp_path: Path) -> None:
    """Covers: ADR-0002 D8 F1 -- any admitted non-quota failure is disqualifying.

    A timeout (or any non-quota failure) is not quota exhaustion; combined with
    genuine quota and a durable no_eligible_candidate event it makes the run's
    outcome ambiguous, so the conservative predicate declines.
    """
    repo = tmp_path / "repo"
    base = _repo(repo)
    db = _db(tmp_path)
    _dispatched(db, str(repo), base, "job1", "run_old")
    _seed_quota_attempt(db, "run_old", str(repo), num=1)
    db.record_event(
        "run_old", "quota_exhaustion_recorded",
        payload={"provider": "p", "until": None, "until_confidence": "UNKNOWN"},
    )
    db.create_attempt(
        Attempt(
            id="run_old_a2", run_id="run_old", attempt_number=2,
            provider="p2", model="m2", worktree_path=str(repo),
            branch_name="orch/run_old/a2", status=AttemptStatus.TIMEOUT,
            created_at=_NOW.isoformat(), completed_at=_NOW.isoformat(),
        )
    )
    db.record_event(
        "run_old", "no_eligible_candidate",
        payload={"outcome": "NO_ELIGIBLE_CANDIDATE"},
    )

    assert preemption.has_genuine_quota_signal(db, "run_old")
    assert not preemption.has_no_eligible_evidence(db, "run_old")

    out = preemption.complete_preemption(
        db, run_id="run_old", repo_path=repo, base_sha=base, now=_NOW
    )

    assert not out.owned and out.detail == "eligible_candidate_remains"
    assert db.get_run("run_old").status == RunStatus.RUNNING  # type: ignore[union-attr]


def test_later_quota_only_chain_after_earlier_success_still_preempts(
    tmp_path: Path,
) -> None:
    """Covers: ADR-0002 D8 -- the conservative predicate is not a blanket rule.

    An admitted candidate that *succeeded* in an earlier plan package is
    compatible with a later, genuinely quota-only chain: the run still has no
    admitted fallback left and must preempt.
    """
    repo = tmp_path / "repo"
    base = _repo(repo)
    db = _db(tmp_path)
    _dispatched(db, str(repo), base, "job1", "run_old")
    # Earlier package: an admitted candidate that completed successfully.
    db.create_attempt(
        Attempt(
            id="run_old_a1", run_id="run_old", attempt_number=1,
            provider="p1", model="m1", worktree_path=str(repo),
            branch_name="orch/run_old/a1", status=AttemptStatus.SUCCESS,
            commit_sha=base,
            created_at=_NOW.isoformat(), completed_at=_NOW.isoformat(),
        )
    )
    # Current chain: the only route dies of quota and the ladder records the
    # no-eligible event for the skipped fallbacks.
    _seed_quota_attempt(db, "run_old", str(repo), num=2)
    db.record_event(
        "run_old", "quota_exhaustion_recorded",
        payload={"provider": "p2", "until": None, "until_confidence": "UNKNOWN"},
    )
    db.record_event(
        "run_old", "no_eligible_candidate",
        payload={"outcome": "NO_ELIGIBLE_CANDIDATE"},
    )

    assert preemption.has_no_eligible_evidence(db, "run_old")

    out = preemption.complete_preemption(
        db, run_id="run_old", repo_path=repo, base_sha=base, now=_NOW
    )

    assert out.owned and out.detail == "preempted"
    assert store.get_job(db, "job1").state == JobState.QUEUED  # type: ignore[union-attr]


def test_quota_only_multi_candidate_skip_still_preempts(tmp_path: Path) -> None:
    """Covers: ADR-0002 D8 -- quota-only multi-candidate exhaustion still preempts.

    Attempts are exclusively quota while the ladder skipped the remaining
    candidates: the durable event plus the quota-only attempt history is the
    genuine exhausted shape and must still preempt.
    """
    repo = tmp_path / "repo"
    base = _repo(repo)
    db = _db(tmp_path)
    _dispatched(db, str(repo), base, "job1", "run_old")
    _seed_quota_attempt(db, "run_old", str(repo), num=1)
    db.record_event(
        "run_old", "quota_exhaustion_recorded",
        payload={"provider": "p", "until": None, "until_confidence": "UNKNOWN"},
    )
    db.record_event(
        "run_old", "no_eligible_candidate",
        payload={"outcome": "NO_ELIGIBLE_CANDIDATE"},
    )

    assert preemption.has_no_eligible_evidence(db, "run_old")

    out = preemption.complete_preemption(
        db, run_id="run_old", repo_path=repo, base_sha=base, now=_NOW
    )

    assert out.owned and out.detail == "preempted"


def test_cancel_racing_preemption_fails_closed(tmp_path: Path) -> None:
    """Covers: prompt §3.7 cancel vs requeue races fail closed, touch nothing."""
    repo = tmp_path / "repo"
    base = _repo(repo)
    db = _db(tmp_path)
    _dispatched(db, str(repo), base, "job1", "run_old")
    _quota_evidence(db, "run_old")
    store.cancel_transition(db, "job1", now=_NOW)

    out = preemption.complete_preemption(
        db, run_id="run_old", repo_path=repo, base_sha=base, now=_NOW
    )

    assert not out.owned and out.detail == "job_not_owned"
    assert db.get_run("run_old").status == RunStatus.RUNNING  # type: ignore[union-attr]
    assert store.get_job(db, "job1").state == JobState.CANCELLED  # type: ignore[union-attr]
    assert GitManager.list_checkpoint_refs(repo, "job1") == {}


def test_crash_between_terminalize_and_requeue_recovers_once(tmp_path: Path) -> None:
    """Covers: prompt §3.6 crash at the DB terminalization boundary recovers."""
    repo = tmp_path / "repo"
    base = _repo(repo)
    db = _db(tmp_path)
    _dispatched(db, str(repo), base, "job1", "run_old")
    _quota_evidence(db, "run_old")
    # Simulate the crash: intent + PREEMPTED run durable, job still RUNNING.
    db.record_event(
        "run_old", "preemption_committed",
        payload={
            "job_id": "job1", "checkpoint_sha": base,
            "checkpoint_ref": "refs/saberops/checkpoints/job1/0",
            "handoff_id": "handoff_crash1",
            "not_before": None, "preemptions_next": 1,
        },
    )
    GitManager.anchor_checkpoint_ref(repo, "job1", 0, base)
    db.update_run(
        run_id="run_old", status=RunStatus.PREEMPTED,
        completed_at=_NOW.isoformat(), result_summary="crashed mid-protocol",
    )

    out = preemption.complete_preemption(
        db, run_id="run_old", repo_path=repo, base_sha=base, now=_NOW
    )

    assert out.owned and out.detail == "preempted"
    job = store.get_job(db, "job1")
    assert job is not None
    assert job.state == JobState.QUEUED
    assert job.preemptions == 1
    # The crashed run's own ref is reused, not duplicated.
    assert GitManager.list_checkpoint_refs(repo, "job1") == {0: base}


def test_crash_before_intent_reruns_full_protocol(tmp_path: Path) -> None:
    """Covers: prompt §3.6 crash before any preemption side effect reruns clean."""
    repo = tmp_path / "repo"
    base = _repo(repo)
    db = _db(tmp_path)
    _dispatched(db, str(repo), base, "job1", "run_old")
    _quota_evidence(db, "run_old")
    # Crash left durable evidence but no intent, no terminal state.

    out = preemption.complete_preemption(
        db, run_id="run_old", repo_path=repo, base_sha=base, now=_NOW
    )

    assert out.owned and out.detail == "preempted"
    assert store.get_job(db, "job1").preemptions == 1  # type: ignore[union-attr]


def test_mismatched_job_run_and_stale_states_decline(tmp_path: Path) -> None:
    """Covers: prompt §3.7 mismatched job/run and stale base fail closed."""
    repo = tmp_path / "repo"
    base = _repo(repo)
    db = _db(tmp_path)
    _dispatched(db, str(repo), base, "job1", "run_old")
    _quota_evidence(db, "run_old")
    # The job moved on to a newer run: the old run must not own anything.
    store.requeue(db, "job1", WaitingReason.NO_CAPACITY, now=_NOW)
    store.mark_dispatched(db, "job1", "run_new", now=_NOW)

    out = preemption.complete_preemption(
        db, run_id="run_old", repo_path=repo, base_sha=base, now=_NOW
    )
    assert not out.owned and out.detail == "job_not_owned"

    # Explicit job routing to a different job's run also declines.
    out2 = preemption.complete_preemption(
        db, run_id="run_old", repo_path=repo, job_id="job1",
        base_sha=base, now=_NOW,
    )
    assert not out2.owned


def test_resume_restores_checkpoint_and_capsule(tmp_path: Path) -> None:
    """Covers: prompt §3.4 new run resumes full data/worktree from checkpoint."""
    repo = tmp_path / "repo"
    base = _repo(repo)
    wt = tmp_path / "wt"
    GitManager.create_worktree(repo, "wt-wip-3", wt, start_point=base)
    (wt / "wip.txt").write_text("partial\n", encoding="utf-8")
    db = _db(tmp_path)
    _dispatched(db, str(repo), base, "job1", "run_old")
    _seed_quota_attempt(db, "run_old", str(repo))
    _quota_evidence(db, "run_old")
    out = preemption.complete_preemption(
        db, run_id="run_old", repo_path=repo,
        worktree_path=str(wt), base_sha=base, now=_NOW,
    )
    assert out.owned and out.checkpoint_sha is not None
    job = store.get_job(db, "job1")
    assert job is not None

    _stage_run(db, str(repo), base, "run_new")
    prep = preemption.prepare_resume(db, job=job, new_run_id="run_new", now=_NOW)

    assert prep.resumed and prep.reason == "resumed"
    assert prep.checkpoint_sha == out.checkpoint_sha
    assert prep.handoff_id == out.handoff_id
    staged = db.get_latest_worker_checkpoint("run_new")
    assert staged is not None
    assert staged.checkpoint_sha == out.checkpoint_sha
    assert staged.handoff_id == out.handoff_id
    assert staged.source_base_sha == base
    # Lineage references the real producing attempt (FK-honest, cross-run).
    assert staged.source_attempt_id == "run_old_a1"
    # The capsule is the factual preemption capsule, retrievable verbatim.
    assert db.get_handoff(out.handoff_id) is not None  # type: ignore[arg-type]
    resumed_events = db.get_events_by_type("run_new", "resume_prepared")
    assert len(resumed_events) == 1
    # Resume stages checkpoint state only: the job row itself is untouched.
    assert store.get_job(db, "job1").resume_from == job.resume_from  # type: ignore[union-attr]


def test_resume_malformed_refs_fail_closed(tmp_path: Path) -> None:
    """Covers: prompt §3.7 wrong SHA / capsule mismatch / stale base / bad ref."""
    repo = tmp_path / "repo"
    base = _repo(repo)
    db = _db(tmp_path)
    _dispatched(db, str(repo), base, "job1", "run_old")
    _quota_evidence(db, "run_old")
    out = preemption.complete_preemption(
        db, run_id="run_old", repo_path=repo, base_sha=base, now=_NOW
    )
    assert out.owned
    job = store.get_job(db, "job1")
    assert job is not None and job.resume_from is not None

    from saberops.jobs.models import ResumeFrom

    # Unreachable SHA.
    bad_sha = dc_replace(job.resume_from, checkpoint_sha="f" * 40)
    _stage_run(db, str(repo), base, "run_x1")
    assert not preemption.prepare_resume(
        db, job=dc_replace(job, resume_from=bad_sha),
        new_run_id="run_x1", now=_NOW,
    ).resumed
    skipped = db.get_events_by_type("run_x1", "resume_skipped")
    assert skipped and isinstance(skipped[0].payload, dict)
    assert skipped[0].payload["reason"] == "checkpoint_unreachable"

    # Capsule belonging to a different run.
    bad_capsule = ResumeFrom(
        run_id="run_other",
        checkpoint_sha=job.resume_from.checkpoint_sha,
        handoff_id=job.resume_from.handoff_id,
    )
    _stage_run(db, str(repo), base, "run_x2")
    assert not preemption.prepare_resume(
        db, job=dc_replace(job, resume_from=bad_capsule),
        new_run_id="run_x2", now=_NOW,
    ).resumed

    # Stale base.
    stale_job = dc_replace(job, base_sha="e" * 40)
    _stage_run(db, str(repo), base, "run_x3")
    assert not preemption.prepare_resume(
        db, job=stale_job, new_run_id="run_x3", now=_NOW
    ).resumed

    # Forbidden job identity for the ref namespace.
    evil_job = dc_replace(job, job_id="../../evil")
    _stage_run(db, str(repo), base, "run_x4")
    assert not preemption.prepare_resume(
        db, job=evil_job, new_run_id="run_x4", now=_NOW
    ).resumed

    # None of the malformed attempts staged checkpoint state.
    for rid in ("run_x1", "run_x2", "run_x3", "run_x4"):
        assert db.get_latest_worker_checkpoint(rid) is None


def test_zero_launch_preemption_parks_without_wip_resume(tmp_path: Path) -> None:
    """Covers: prompt §3.2 + §3.4 quota exhaustion before the first attempt.

    No attempt means no WIP and no honest checkpoint row (the checkpoint
    table references attempts by foreign key): the run still parks with an
    anchored HEAD ref and a capsule, and the new run launches fresh from
    the identical base with an exact-cause event instead of staged state.
    """
    repo = tmp_path / "repo"
    base = _repo(repo)
    db = _db(tmp_path)
    _dispatched(db, str(repo), base, "job1", "run_old")
    _quota_evidence(db, "run_old")

    out = preemption.complete_preemption(
        db, run_id="run_old", repo_path=repo, base_sha=base, now=_NOW
    )
    assert out.owned and out.checkpoint_sha == base
    job = store.get_job(db, "job1")
    assert job is not None and job.resume_from is not None

    _stage_run(db, str(repo), base, "run_new")
    prep = preemption.prepare_resume(db, job=job, new_run_id="run_new", now=_NOW)
    assert not prep.resumed and prep.reason == "no_source_attempt"
    assert db.get_latest_worker_checkpoint("run_new") is None
    skipped = db.get_events_by_type("run_new", "resume_skipped")
    assert skipped and isinstance(skipped[0].payload, dict)
    assert skipped[0].payload["reason"] == "no_source_attempt"


def test_reconcile_completes_interrupted_preemption(tmp_path: Path) -> None:
    """Covers: prompt §3.6 + §3.9 reconciler finishes a crashed preemption."""
    repo = tmp_path / "repo"
    base = _repo(repo)
    db = _db(tmp_path)
    _dispatched(db, str(repo), base, "job1", "run_old")
    _quota_evidence(db, "run_old")
    db.record_event(
        "run_old", "preemption_committed",
        payload={
            "job_id": "job1", "checkpoint_sha": base,
            "checkpoint_ref": "refs/saberops/checkpoints/job1/0",
            "handoff_id": "handoff_crash2",
            "not_before": None, "preemptions_next": 1,
        },
    )
    GitManager.anchor_checkpoint_ref(repo, "job1", 0, base)
    db.update_run(
        run_id="run_old", status=RunStatus.PREEMPTED,
        completed_at=_NOW.isoformat(), result_summary="crashed",
    )

    changes = service.reconcile_jobs(
        db, run_status=lambda rid: db.get_run(rid).status, now=_NOW  # type: ignore[union-attr]
    )

    assert len(changes) == 1
    assert changes[0].job_id == "job1"
    assert changes[0].to_state == JobState.QUEUED
    job = store.get_job(db, "job1")
    assert job is not None
    assert job.state == JobState.QUEUED
    assert job.waiting_reason == WaitingReason.PREEMPTED
    assert job.preemptions == 1
    # Second call changes nothing: exactly-once requeue.
    assert service.reconcile_jobs(
        db, run_status=lambda rid: db.get_run(rid).status, now=_NOW  # type: ignore[union-attr]
    ) == []


def test_reservation_released_exactly_once(tmp_path: Path) -> None:
    """Covers: prompt §3.6 no leaked slots, no double release on replay."""
    repo = tmp_path / "repo"
    base = _repo(repo)
    db = _db(tmp_path)
    _dispatched(db, str(repo), base, "job1", "run_old")
    _quota_evidence(db, "run_old")
    reserve(
        db, run_id="run_old", job_id="job1", route="p/m",
        draws=[PoolDraw("pool1", "5h")], size_bucket="M", now=_NOW,
    )

    out = preemption.complete_preemption(
        db, run_id="run_old", repo_path=repo, base_sha=base, now=_NOW
    )
    assert out.owned
    assert release(db, "run_old", now=_NOW) == 0
    replay = preemption.complete_preemption(
        db, run_id="run_old", repo_path=repo, base_sha=base, now=_NOW
    )
    assert replay.owned and replay.detail == "already_preempted"
    assert release(db, "run_old", now=_NOW) == 0


def test_should_preempt_predicate_matrix(tmp_path: Path) -> None:
    """Covers: D8 trigger gates (quota x no-eligible x ownership x budget)."""
    repo = tmp_path / "repo"
    base = _repo(repo)

    db = _db(tmp_path, name="m.db")
    ready = preemption.should_preempt(db, "run_absent")
    assert not ready.eligible and ready.detail == "no_job"

    _dispatched(db, str(repo), base, "job1", "run_old")
    assert preemption.should_preempt(db, "run_old").detail == "no_quota_signal"
    _quota_evidence(db, "run_old", no_eligible=False)
    assert (
        preemption.should_preempt(db, "run_old").detail == "eligible_candidate_remains"
    )
    db.record_event(
        "run_old", "no_eligible_candidate", payload={"outcome": "NO_ELIGIBLE_CANDIDATE"}
    )
    ok = preemption.should_preempt(db, "run_old")
    assert ok.eligible and ok.detail == "ok"
    assert ok.genuine_quota and ok.no_eligible_candidate
    assert ok.job is not None and ok.job.job_id == "job1"
    assert ok.checkpoint_sha is None  # no checkpoint row recorded yet

    limited = preemption.should_preempt(db, "run_old", max_preemptions=0)
    assert not limited.eligible and limited.detail == "preemption_limit"


def test_failure_vocabulary_and_terminal_recognition() -> None:
    """Covers: prompt §3.9 PREEMPTION_LIMIT vocab + PREEMPTED terminality."""
    assert PREEMPTION_LIMIT in FAILURE_REASONS
    assert RunStatus.PREEMPTED in TERMINAL_RUN_STATUSES
    assert RunStatus.PREEMPTED.value == "PREEMPTED"
    failed = Job(
        job_id="j", idempotency_key=None, caller_id="owner", repo="/r",
        base_ref="HEAD", base_sha="b", goal="g", acceptance=None,
        requirements=JobRequirements(role=JobRole.WORKER),
        state=JobState.FAILED, failure_reason=PREEMPTION_LIMIT,
        created_at=_NOW.isoformat(), updated_at=_NOW.isoformat(),
        request_digest="d",
    )
    assert failed.failure_reason == "PREEMPTION_LIMIT"


def test_genuine_signal_channels(tmp_path: Path) -> None:
    """Covers: attempt / checkpoint / event quota channels all count as genuine."""
    repo = tmp_path / "repo"
    base = _repo(repo)

    db = _db(tmp_path, name="a.db")
    _dispatched(db, str(repo), base, "job1", "run_old")
    db.create_attempt(
        Attempt(
            id="run_old_a1", run_id="run_old", attempt_number=1,
            provider="p", model="m", worktree_path=str(repo),
            branch_name="orch/run_old/a1", status=AttemptStatus.FAILED,
            interruption_reason=_QUOTA,
        )
    )
    assert preemption.has_genuine_quota_signal(db, "run_old")
    # A lone quota-interrupted attempt is the single-candidate exhausted shape.
    assert preemption.has_no_eligible_evidence(db, "run_old")
    assert preemption.quota_reset_eta(db, "run_old") is None
    db.create_attempt(
        Attempt(
            id="run_old_a2", run_id="run_old", attempt_number=2,
            provider="p", model="m", worktree_path=str(repo),
            branch_name="orch/run_old/a2", status=AttemptStatus.FAILED,
            interruption_reason="GATE_FAILED",
        )
    )
    assert preemption.has_genuine_quota_signal(db, "run_old")
    assert not preemption.has_no_eligible_evidence(db, "run_old")


def test_preempted_run_report_renders_terminal_status(tmp_path: Path) -> None:
    """Covers: prompt §1.H runtime status honestly renders PREEMPTED.

    The run report derives ``terminal:PREEMPTED`` from the canonical
    terminal set, and the monitor-loop exit predicate (cli.py readout)
    recognizes the report lifecycle status through that same set -- so a
    preempted run never spins the monitor nor renders as failure/success.
    """
    from saberops.observability.run_report import build_run_report

    repo = tmp_path / "repo"
    base = _repo(repo)
    db = _db(tmp_path)
    _dispatched(db, str(repo), base, "job1", "run_old")
    _quota_evidence(db, "run_old")
    out = preemption.complete_preemption(
        db, run_id="run_old", repo_path=repo, base_sha=base, now=_NOW
    )
    assert out.owned

    report = build_run_report(db, "run_old")
    lifecycle = report.get("lifecycle")
    assert isinstance(lifecycle, dict)
    assert lifecycle.get("status") == RunStatus.PREEMPTED.value
    assert lifecycle.get("current_stage") == "terminal:PREEMPTED"
    assert isinstance(lifecycle.get("status"), str) and lifecycle.get(
        "status"
    ) in {s.value for s in TERMINAL_RUN_STATUSES}
