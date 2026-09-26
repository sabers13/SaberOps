"""R0-B.1: acceptance projection parity with the engine policy.

Every blocking condition in
``AcceptEngine.check_accept_eligibility`` must mirror a real pre-CAS
rule enforced by ``AcceptEngine.accept_run``.  In particular the
projection must not invent a ``not_already_accepted`` blocker:
``accept_run`` has no "accepted only once" precondition (candidate
lifecycle policy belongs to R3), so a prior ``run_accepted`` event
with the repository restored to the original base stays eligible.

Permit cases prove the engine path with a mocked non-destructive
tail (CAS + sync + gate): the real repository HEAD is never moved.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from saberops.accept import AcceptEngine, AcceptResult
from saberops.db import Database
from saberops.gate_runner import (
    GateReceipt,
    GateRunner,
    GateRunnerRequest,
    GateScratchBackend,
)
from saberops.git import GitManager
from saberops.models import (
    Attempt,
    AttemptStatus,
    GateScratchMode,
    ReviewResult,
    ReviewVerdict,
    Run,
    RunStatus,
    Tier,
)

_CREATED = "2026-09-24T10:00:00+00:00"

# Every key the projection may report as a *blocking* (fail) check, and
# the accept_run() pre-CAS rule it mirrors.  Anything outside this set
# that gates eligibility is a projection-only invention.
#
# R3-C: "candidate_not_rejected" mirrors the real pre-CAS rejection
# precondition in accept_run() (a rejected current candidate is refused
# before any Git mutation), so it belongs here.  It is not a
# projection-only invention and it is not an "accepted only once" rule.
ENGINE_MIRRORED_BLOCKING_KEYS = frozenset(
    {
        "run_completed",
        "candidate_available",
        "candidate_provenance",
        "candidate_not_rejected",
        "target_repository",
        "target_clean",
        "target_head",
        "candidate_ref",
        "review",
        "fast_forward",
        "gate_scratch",
    }
)


@pytest.fixture()
def isolated_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "state"
    config = tmp_path / "config"
    state.mkdir(parents=True)
    config.mkdir(parents=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.delenv("ORCH_ACCESS_STORE", raising=False)
    monkeypatch.delenv("ORCH_ACCESS_MODELS_STORE", raising=False)
    monkeypatch.delenv("ORCH_BINDING_STORE", raising=False)
    monkeypatch.delenv("ORCH_READINESS_STORE", raising=False)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _make_repo(root: Path, name: str) -> tuple[Path, str, str]:
    """Create a repo with base commit and a child candidate, HEAD at base."""
    repo = root / name
    repo.mkdir(parents=True)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "r0b1@example.com")
    _git(repo, "config", "user.name", "r0b1")
    (repo / "file.txt").write_text("base\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "file.txt").write_text("candidate\n")
    _git(repo, "commit", "-am", "candidate")
    candidate = _git(repo, "rev-parse", "HEAD")
    _git(repo, "reset", "--hard", base)
    assert _git(repo, "rev-parse", "HEAD") == base
    return repo, base, candidate


def _seed_run(
    db_path: Path,
    run_id: str,
    repo: Path,
    base: str,
    candidate: str,
    *,
    review_enabled: bool = True,
) -> None:
    Database(db_path).create_run(
        Run(
            id=run_id,
            task="r0b1 parity probe",
            target_repo=str(repo),
            base_commit=base,
            tier=Tier.T1,
            training_allowed=True,
            gate_command="true",
            status=RunStatus.COMPLETED,
            created_at=_CREATED,
            completed_at=_CREATED,
            final_attempt_id=None,
            project_id=None,
            config_json=json.dumps({"review_enabled": review_enabled}),
        )
    )
    Database(db_path).create_attempt(
        Attempt(
            id=f"{run_id}-a1",
            run_id=run_id,
            attempt_number=1,
            provider="opencode",
            model="test-model",
            worktree_path=str(repo),
            branch_name="r0b1-candidate-branch",
            status=AttemptStatus.SUCCESS,
            commit_sha=candidate,
            created_at=_CREATED,
            base_sha=base,
        )
    )


def _add_review(db_path: Path, run_id: str, verdict: ReviewVerdict) -> None:
    Database(db_path).create_review(
        ReviewResult(
            id=f"{run_id}-{verdict.value}-{_CREATED}",
            run_id=run_id,
            provider="opencode",
            model="reviewer",
            verdict=verdict,
            summary=f"summary {verdict.value}",
            details="",
            created_at=_CREATED,
        )
    )


def _final_candidate_sha(db_path: Path, run_id: str) -> str:
    attempts = Database(db_path).get_attempts_for_run(run_id)
    for attempt in reversed(attempts):
        if attempt.status == AttemptStatus.SUCCESS and attempt.commit_sha:
            assert attempt.commit_sha is not None
            return attempt.commit_sha
    raise AssertionError(f"no candidate for {run_id}")


def _accept_with_mocked_tail(
    monkeypatch: pytest.MonkeyPatch, db_path: Path, run_id: str
) -> AcceptResult:
    """Run accept_run() with a non-destructive tail seam.

    The pre-CAS policy executes for real.  The CAS, worktree sync,
    post-CAS HEAD reads, and gate phase are faked so the real target
    repository is never mutated and no real gate runs.
    """
    db = Database(db_path)
    run = db.get_run(run_id)
    assert run is not None
    base_sha = run.base_commit
    candidate_sha = _final_candidate_sha(db_path, run_id)
    real_head = GitManager.get_head_commit
    head_calls = {"count": 0}

    def fake_head(repo_path: Path | str) -> str:
        head_calls["count"] += 1
        if head_calls["count"] == 1:
            return real_head(repo_path)
        return candidate_sha

    def fake_cas(
        repo_path: Path | str,
        ref: str,
        *,
        expected_old_sha: str,
        new_target_sha: str,
    ) -> str:
        _ = (repo_path, ref)
        assert expected_old_sha == base_sha
        assert new_target_sha == candidate_sha
        return new_target_sha

    def fake_sync(repo_path: Path | str) -> None:
        _ = repo_path
        return None

    def fake_gate(
        self: AcceptEngine,
        gate_run_id: str,
        runner: GateRunner,
        request: GateRunnerRequest,
        *,
        post_gate_sha_resolver: Callable[[], tuple[str, bool]] | None = None,
    ) -> GateReceipt:
        _ = (self, gate_run_id, runner, post_gate_sha_resolver)
        return GateReceipt(
            requested_scratch_mode=GateScratchMode.DISK,
            actual_scratch_backend=GateScratchBackend.DISK,
            fallback_reason=None,
            scratch_path="",
            candidate_sha=request.candidate_sha,
            post_gate_candidate_sha=request.candidate_sha,
            post_gate_worktree_clean=True,
            gate_command=request.gate_command,
            exit_code=0,
            start_timestamp=_CREATED,
            end_timestamp=_CREATED,
            duration_seconds=0.0,
            log_path="",
            log_digest="",
            stdout_excerpt="",
            stderr_excerpt="",
            timed_out=False,
        )

    monkeypatch.setattr(GitManager, "get_head_commit", fake_head)
    monkeypatch.setattr(GitManager, "compare_and_swap_ref", fake_cas)
    monkeypatch.setattr(GitManager, "sync_index_worktree", fake_sync)
    monkeypatch.setattr(AcceptEngine, "_execute_gate_phase", fake_gate)
    return AcceptEngine(db=db).accept_run(run_id)


def _no_projection_only_blocker(run_id: str, db_path: Path) -> None:
    eligibility = AcceptEngine(db=Database(db_path)).check_accept_eligibility(run_id)
    for check in eligibility.checks:
        if not check.passed():
            assert check.key in ENGINE_MIRRORED_BLOCKING_KEYS, (
                f"projection-only blocker {check.key!r}: {check.reason}"
            )
    assert not any(
        check.key == "not_already_accepted" and not check.passed()
        for check in eligibility.checks
    )


def test_r0b1_eligible_candidate_engine_permits(
    isolated_xdg: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A: normal eligible candidate -- projection and engine agree to permit."""
    repo, base, candidate = _make_repo(tmp_path, "repo-a")
    db_path = tmp_path / "web.db"
    _seed_run(db_path, "run_r0b1_a", repo, base, candidate)
    _add_review(db_path, "run_r0b1_a", ReviewVerdict.PASS)

    eligibility = AcceptEngine(db=Database(db_path)).check_accept_eligibility("run_r0b1_a")
    assert eligibility.eligible
    assert all(check.passed() for check in eligibility.checks)
    _no_projection_only_blocker("run_r0b1_a", db_path)

    result = _accept_with_mocked_tail(monkeypatch, db_path, "run_r0b1_a")
    assert result.accepted
    assert result.gate_passed
    # Non-destructive seam: the real repository never moved.
    assert _git(repo, "rev-parse", "HEAD") == base
    accepted = Database(db_path).get_events_by_type("run_r0b1_a", "run_accepted")
    assert len(accepted) == 1
    _ = candidate


def test_r0b1_moved_head_blocks_both(
    isolated_xdg: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B: moved HEAD -- projection and engine block for the same reason."""
    repo, base, candidate = _make_repo(tmp_path, "repo-b")
    db_path = tmp_path / "web.db"
    _seed_run(db_path, "run_r0b1_b", repo, base, candidate)
    _add_review(db_path, "run_r0b1_b", ReviewVerdict.PASS)
    (repo / "other.txt").write_text("moved\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "moved")
    moved = _git(repo, "rev-parse", "HEAD")
    assert moved != base
    _ = monkeypatch

    eligibility = AcceptEngine(db=Database(db_path)).check_accept_eligibility("run_r0b1_b")
    assert not eligibility.eligible
    failing = [c for c in eligibility.checks if not c.passed()]
    assert any(c.key == "target_head" for c in failing)
    _no_projection_only_blocker("run_r0b1_b", db_path)

    with pytest.raises(RuntimeError, match="has moved from recorded base commit"):
        AcceptEngine(db=Database(db_path)).accept_run("run_r0b1_b")
    # Failed pre-CAS: target untouched, nothing accepted.
    assert _git(repo, "rev-parse", "HEAD") == moved
    assert Database(db_path).get_events_by_type("run_r0b1_b", "run_accepted") == []


def test_r0b1_dirty_target_blocks_both(
    isolated_xdg: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C: dirty target -- projection and engine block for the same reason."""
    repo, base, candidate = _make_repo(tmp_path, "repo-c")
    db_path = tmp_path / "web.db"
    _seed_run(db_path, "run_r0b1_c", repo, base, candidate)
    _add_review(db_path, "run_r0b1_c", ReviewVerdict.PASS)
    (repo / "untracked.txt").write_text("dirty\n")
    _ = monkeypatch

    eligibility = AcceptEngine(db=Database(db_path)).check_accept_eligibility("run_r0b1_c")
    assert not eligibility.eligible
    failing = [c for c in eligibility.checks if not c.passed()]
    assert any(c.key == "target_clean" for c in failing)
    _no_projection_only_blocker("run_r0b1_c", db_path)

    with pytest.raises(RuntimeError, match="must be clean before accept"):
        AcceptEngine(db=Database(db_path)).accept_run("run_r0b1_c")
    assert (repo / "untracked.txt").read_text() == "dirty\n"
    assert Database(db_path).get_events_by_type("run_r0b1_c", "run_accepted") == []


def test_r0b1_missing_review_blocks_both(
    isolated_xdg: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D: missing required review -- projection and engine block together."""
    repo, base, candidate = _make_repo(tmp_path, "repo-d")
    db_path = tmp_path / "web.db"
    _seed_run(db_path, "run_r0b1_d", repo, base, candidate, review_enabled=True)
    _ = monkeypatch

    eligibility = AcceptEngine(db=Database(db_path)).check_accept_eligibility("run_r0b1_d")
    assert not eligibility.eligible
    failing = [c for c in eligibility.checks if not c.passed()]
    assert any(c.key == "review" for c in failing)
    _no_projection_only_blocker("run_r0b1_d", db_path)

    with pytest.raises(RuntimeError, match="no review exists"):
        AcceptEngine(db=Database(db_path)).accept_run("run_r0b1_d")
    assert _git(repo, "rev-parse", "HEAD") == base
    assert Database(db_path).get_events_by_type("run_r0b1_d", "run_accepted") == []


def test_r0b1_pass_with_backlog_permits_both(
    isolated_xdg: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E: PASS_WITH_BACKLOG -- projection and engine both permit."""
    repo, base, candidate = _make_repo(tmp_path, "repo-e")
    db_path = tmp_path / "web.db"
    _seed_run(db_path, "run_r0b1_e", repo, base, candidate, review_enabled=True)
    _add_review(db_path, "run_r0b1_e", ReviewVerdict.PASS_WITH_BACKLOG)

    eligibility = AcceptEngine(db=Database(db_path)).check_accept_eligibility("run_r0b1_e")
    assert eligibility.eligible
    review_check = next(c for c in eligibility.checks if c.key == "review")
    assert review_check.passed()
    _no_projection_only_blocker("run_r0b1_e", db_path)

    result = _accept_with_mocked_tail(monkeypatch, db_path, "run_r0b1_e")
    assert result.accepted
    assert _git(repo, "rev-parse", "HEAD") == base
    _ = candidate


def test_r0b1_review_not_required_permits_both(
    isolated_xdg: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F: review not required and absent -- projection and engine both permit."""
    repo, base, candidate = _make_repo(tmp_path, "repo-f")
    db_path = tmp_path / "web.db"
    _seed_run(db_path, "run_r0b1_f", repo, base, candidate, review_enabled=False)

    eligibility = AcceptEngine(db=Database(db_path)).check_accept_eligibility("run_r0b1_f")
    assert eligibility.eligible
    review_check = next(c for c in eligibility.checks if c.key == "review")
    assert review_check.passed()
    assert "not required" in review_check.reason.lower()
    _no_projection_only_blocker("run_r0b1_f", db_path)

    result = _accept_with_mocked_tail(monkeypatch, db_path, "run_r0b1_f")
    assert result.accepted
    assert _git(repo, "rev-parse", "HEAD") == base
    _ = candidate


def test_r0b1_prior_acceptance_restored_base_stays_eligible(
    isolated_xdg: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G: prior run_accepted + manual restore to base -- no invented blocker."""
    repo, base, candidate = _make_repo(tmp_path, "repo-g")
    db_path = tmp_path / "web.db"
    _seed_run(db_path, "run_r0b1_g", repo, base, candidate)
    _add_review(db_path, "run_r0b1_g", ReviewVerdict.PASS)
    Database(db_path).record_event(
        "run_r0b1_g",
        "run_accepted",
        payload={"candidate_sha": candidate, "note": "prior acceptance"},
    )
    # Owner manually resets the target back to the run's original base.
    _git(repo, "reset", "--hard", candidate)
    assert _git(repo, "rev-parse", "HEAD") == candidate
    _git(repo, "reset", "--hard", base)
    assert _git(repo, "rev-parse", "HEAD") == base

    eligibility = AcceptEngine(db=Database(db_path)).check_accept_eligibility("run_r0b1_g")
    assert eligibility.eligible, [
        (c.key, c.status, c.reason) for c in eligibility.checks if not c.passed()
    ]
    assert not any(
        check.key == "not_already_accepted" and not check.passed()
        for check in eligibility.checks
    )
    _no_projection_only_blocker("run_r0b1_g", db_path)

    # The engine itself has no once-only precondition: the acceptance
    # path stays open (proven via the non-destructive seam).
    result = _accept_with_mocked_tail(monkeypatch, db_path, "run_r0b1_g")
    assert result.accepted
    assert _git(repo, "rev-parse", "HEAD") == base


def test_r0b1_blocking_keys_all_mirror_engine_rules(
    isolated_xdg: None, tmp_path: Path
) -> None:
    """Audit: the projection exposes no blocker outside engine-mirrored keys."""
    repo, base, candidate = _make_repo(tmp_path, "repo-h")
    db_path = tmp_path / "web.db"
    _seed_run(db_path, "run_r0b1_h", repo, base, candidate)
    _add_review(db_path, "run_r0b1_h", ReviewVerdict.PASS)

    eligibility = AcceptEngine(db=Database(db_path)).check_accept_eligibility("run_r0b1_h")
    assert eligibility.eligible
    keys = {check.key for check in eligibility.checks}
    assert "not_already_accepted" not in keys
    assert keys <= ENGINE_MIRRORED_BLOCKING_KEYS | {"deterministic_gate"}
    _ = candidate
