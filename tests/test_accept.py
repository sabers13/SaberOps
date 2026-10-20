"""Acceptance tests: engine-derived truth and Web/engine projection parity.

Two merged R0-B generations:

* the Web acceptance surface derives from ``AcceptEngine`` (the run page
  checklist and Accept button use the engine-derived read-only projection --
  the Web holds no independent acceptance policy table);
* every blocking condition in ``AcceptEngine.check_accept_eligibility`` must
  mirror a real pre-CAS rule enforced by ``AcceptEngine.accept_run``.  In
  particular the projection must not invent a ``not_already_accepted``
  blocker: ``accept_run`` has no "accepted only once" precondition (candidate
  lifecycle policy belongs to R3), so a prior ``run_accepted`` event with the
  repository restored to the original base stays eligible.

Permit cases prove the engine path with a mocked non-destructive tail
(CAS + sync + gate): the real repository HEAD is never moved.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from saberops.accept import AcceptEngine, AcceptResult
from saberops.control_plane import (
    BindingRole,
    OwnerBindings,
    materialize_discovered_binding,
    save_owner_bindings,
    upsert_owner_binding,
)
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
    WorkerCandidate,
    WorkerRequest,
    WorkerResult,
)
from saberops.web import create_app
from saberops.workers.base import WorkerAdapter
from saberops.workers.registry import AdapterRegistry

_CREATED = "2026-09-24T10:00:00+00:00"
_LATER = "2026-09-24T11:00:00+00:00"

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
    status: RunStatus = RunStatus.COMPLETED,
    review_enabled: bool = True,
) -> None:
    Database(db_path).create_run(
        Run(
            id=run_id,
            task="r0b acceptance probe",
            target_repo=str(repo),
            base_commit=base,
            tier=Tier.T1,
            training_allowed=True,
            gate_command="true",
            status=status,
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
            branch_name="r0b-candidate-branch",
            status=AttemptStatus.SUCCESS,
            commit_sha=candidate,
            created_at=_CREATED,
            base_sha=base,
        )
    )


def _add_review(
    db_path: Path, run_id: str, verdict: ReviewVerdict, created_at: str = _CREATED
) -> None:
    Database(db_path).create_review(
        ReviewResult(
            id=f"{run_id}-{verdict.value}-{created_at}",
            run_id=run_id,
            provider="opencode",
            model="reviewer",
            verdict=verdict,
            summary=f"summary {verdict.value}",
            details="",
            created_at=created_at,
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


def _client(db_path: Path) -> TestClient:
    return TestClient(create_app(db_path=db_path), raise_server_exceptions=False)


def _checks_by_key(db_path: Path, run_id: str) -> dict[str, dict[str, str]]:
    engine = AcceptEngine(db=Database(db_path))
    eligibility = engine.check_accept_eligibility(run_id)
    return {
        check.key: {"status": check.status, "reason": check.reason}
        for check in eligibility.checks
    }


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


def test_r0b_eligible_candidate_checklist_passes_and_accept_enabled(
    isolated_xdg: None, tmp_path: Path
) -> None:
    """A: eligible candidate renders a passing checklist; Accept enabled."""
    repo, base, candidate = _make_repo(tmp_path, "repo-a")
    db_path = tmp_path / "web.db"
    _seed_run(db_path, "run_r0b_a", repo, base, candidate)
    _add_review(db_path, "run_r0b_a", ReviewVerdict.PASS)

    eligibility = AcceptEngine(db=Database(db_path)).check_accept_eligibility("run_r0b_a")
    assert eligibility.eligible
    assert {c.key for c in eligibility.checks} >= {
        "run_completed",
        "candidate_available",
        "target_clean",
        "target_head",
        "review",
        "fast_forward",
    }
    assert all(c.passed() for c in eligibility.checks)

    page = _client(db_path).get("/runs/run_r0b_a")
    assert page.status_code == 200
    assert 'data-check-key="review" data-check-status="pass"' in page.text
    assert "Review requirement satisfied" in page.text
    # Accept button rendered and NOT disabled.
    assert 'id="acceptRunButton"' in page.text
    accept_segment = page.text.split('id="acceptRunButton"')[1].split(">")[0]
    assert "disabled" not in accept_segment

    # The confirm step is reachable (gating passed, not a 400).
    confirm = _client(db_path).post(
        "/runs/run_r0b_a/accept", data={"confirm_run_id": ""}
    )
    assert confirm.status_code == 200
    assert "Type the run id to confirm" in confirm.text


def test_r0b_moved_target_head_blocks_accept(
    isolated_xdg: None, tmp_path: Path
) -> None:
    """B: HEAD/base mismatch is named and Accept is disabled."""
    repo, base, candidate = _make_repo(tmp_path, "repo-b")
    db_path = tmp_path / "web.db"
    _seed_run(db_path, "run_r0b_b", repo, base, candidate)
    _add_review(db_path, "run_r0b_b", ReviewVerdict.PASS)
    # Advance the target HEAD past the candidate base.
    (repo / "other.txt").write_text("moved\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "moved")

    checks = _checks_by_key(db_path, "run_r0b_b")
    assert checks["target_head"]["status"] == "fail"
    assert "moved since this candidate was created" in checks["target_head"]["reason"]
    eligibility = AcceptEngine(db=Database(db_path)).check_accept_eligibility("run_r0b_b")
    assert not eligibility.eligible

    page = _client(db_path).get("/runs/run_r0b_b")
    assert page.status_code == 200
    assert 'data-check-key="target_head" data-check-status="fail"' in page.text
    assert "Target HEAD moved since this candidate was created" in page.text
    accept_segment = page.text.split('id="acceptRunButton"')[1].split(">")[0]
    assert "disabled" in accept_segment

    blocked = _client(db_path).post(
        "/runs/run_r0b_b/accept", data={"confirm_run_id": ""}
    )
    assert blocked.status_code == 400
    assert "Accept is blocked by the acceptance checklist" in blocked.text


def test_r0b_dirty_target_repo_blocks_accept(
    isolated_xdg: None, tmp_path: Path
) -> None:
    """C: dirty target repository is exposed and Accept is disabled."""
    repo, base, candidate = _make_repo(tmp_path, "repo-c")
    db_path = tmp_path / "web.db"
    _seed_run(db_path, "run_r0b_c", repo, base, candidate)
    _add_review(db_path, "run_r0b_c", ReviewVerdict.PASS)
    (repo / "untracked.txt").write_text("dirty\n")

    checks = _checks_by_key(db_path, "run_r0b_c")
    assert checks["target_clean"]["status"] == "fail"
    assert "must be clean before accept" in checks["target_clean"]["reason"]

    page = _client(db_path).get("/runs/run_r0b_c")
    assert page.status_code == 200
    assert 'data-check-key="target_clean" data-check-status="fail"' in page.text
    accept_segment = page.text.split('id="acceptRunButton"')[1].split(">")[0]
    assert "disabled" in accept_segment


def test_r0b_required_review_missing_blocks_accept(
    isolated_xdg: None, tmp_path: Path
) -> None:
    """D: required but missing/failing review is exposed."""
    repo, base, candidate = _make_repo(tmp_path, "repo-d")
    db_path = tmp_path / "web.db"
    _seed_run(db_path, "run_r0b_d", repo, base, candidate, review_enabled=True)

    checks = _checks_by_key(db_path, "run_r0b_d")
    assert checks["review"]["status"] == "fail"
    assert "no review exists" in checks["review"]["reason"]

    page = _client(db_path).get("/runs/run_r0b_d")
    assert 'data-check-key="review" data-check-status="fail"' in page.text

    # A present non-substantive verdict blocks exactly like the engine.
    _add_review(db_path, "run_r0b_d", ReviewVerdict.BLOCK)
    checks = _checks_by_key(db_path, "run_r0b_d")
    assert checks["review"]["status"] == "fail"
    assert "BLOCK" in checks["review"]["reason"]
    page = _client(db_path).get("/runs/run_r0b_d")
    accept_segment = page.text.split('id="acceptRunButton"')[1].split(">")[0]
    assert "disabled" in accept_segment


def test_r0b_review_not_required_does_not_block(
    isolated_xdg: None, tmp_path: Path
) -> None:
    """E: review-not-required is valid and does not falsely block."""
    repo, base, candidate = _make_repo(tmp_path, "repo-e")
    db_path = tmp_path / "web.db"
    _seed_run(db_path, "run_r0b_e", repo, base, candidate, review_enabled=False)

    checks = _checks_by_key(db_path, "run_r0b_e")
    assert checks["review"]["status"] == "pass"
    assert "not required" in checks["review"]["reason"].lower()
    eligibility = AcceptEngine(db=Database(db_path)).check_accept_eligibility("run_r0b_e")
    assert eligibility.eligible

    page = _client(db_path).get("/runs/run_r0b_e")
    assert page.status_code == 200
    assert 'data-check-key="review" data-check-status="pass"' in page.text
    accept_segment = page.text.split('id="acceptRunButton"')[1].split(">")[0]
    assert "disabled" not in accept_segment


def test_r0b_pass_with_backlog_matches_engine(
    isolated_xdg: None, tmp_path: Path
) -> None:
    """F: PASS_WITH_BACKLOG behaves exactly like AcceptEngine."""
    repo, base, candidate = _make_repo(tmp_path, "repo-f")
    db_path = tmp_path / "web.db"
    _seed_run(db_path, "run_r0b_f", repo, base, candidate, review_enabled=True)
    _add_review(db_path, "run_r0b_f", ReviewVerdict.PASS_WITH_BACKLOG)

    eligibility = AcceptEngine(db=Database(db_path)).check_accept_eligibility("run_r0b_f")
    assert eligibility.eligible
    review_check = next(c for c in eligibility.checks if c.key == "review")
    assert review_check.passed()
    assert "PASS_WITH_BACKLOG" in review_check.reason

    page = _client(db_path).get("/runs/run_r0b_f")
    assert 'data-check-key="review" data-check-status="pass"' in page.text
    accept_segment = page.text.split('id="acceptRunButton"')[1].split(">")[0]
    assert "disabled" not in accept_segment


def test_r0b_engine_change_propagates_without_web_policy_table(
    isolated_xdg: None, tmp_path: Path
) -> None:
    """G: Web state follows the engine; no duplicate Web policy table."""
    web_source = (Path(__file__).parent.parent / "src" / "saberops" / "web.py").read_text()
    assert "latest_review.verdict == ReviewVerdict.PASS" not in web_source
    assert "Accept is only available for completed runs with a PASS review" not in web_source

    repo, base, candidate = _make_repo(tmp_path, "repo-g")
    db_path = tmp_path / "web.db"
    _seed_run(db_path, "run_r0b_g", repo, base, candidate, review_enabled=True)
    _add_review(db_path, "run_r0b_g", ReviewVerdict.PASS, created_at=_CREATED)
    client = _client(db_path)

    enabled_page = client.get("/runs/run_r0b_g")
    assert 'data-check-key="review" data-check-status="pass"' in enabled_page.text

    # A later failing review changes the engine result; the Web follows
    # with no code change and no second policy table.
    _add_review(db_path, "run_r0b_g", ReviewVerdict.BLOCK, created_at=_LATER)
    engine = AcceptEngine(db=Database(db_path))
    assert not engine.check_accept_eligibility("run_r0b_g").eligible
    followed_page = client.get("/runs/run_r0b_g")
    assert 'data-check-key="review" data-check-status="fail"' in followed_page.text
    accept_segment = followed_page.text.split('id="acceptRunButton"')[1].split(">")[0]
    assert "disabled" in accept_segment


# ---------------------------------------------------------------------------
# Ported from the removed test_r3e1 quick / custom workflow checks: the
# workflow resolves gate/review policy through the production run-creation
# authority and reaches acceptance through the real engines.
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "state"
    config = tmp_path / "config"
    cache = tmp_path / "cache"
    for directory in (state, config, cache):
        directory.mkdir(parents=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    monkeypatch.delenv("SABEROPS_PROFILE", raising=False)
    monkeypatch.setenv("ORCH_BINDING_STORE", str(tmp_path / "bindings.json"))
    monkeypatch.setenv("ORCH_ACCESS_STORE", str(tmp_path / "access.json"))
    monkeypatch.setenv("ORCH_ACCESS_MODELS_STORE", str(tmp_path / "models.json"))
    monkeypatch.setenv("ORCH_READINESS_STORE", str(tmp_path / "readiness.json"))
    return tmp_path

def _seed_verified_opencode_posture(config_home: Path) -> None:
    """Seed an isolated owner config with a verified class-A posture for the
    synthetic ``opencode`` provider used by this module's dispatch fixtures.

    Covers: ADR-0002 D2 / WORKFLOW §7 authorized setup-only fixture migration.
    The R3E1 fixtures dispatch synthetic ``opencode/<model>`` candidates with no
    matching posture record, so the accepted M2-4b admission seam rightfully
    returns ``quota_blocked`` (POSTURE_UNVERIFIED fails closed).  This seeds
    ONLY the synthetic provider identity those fixtures use; no assertion,
    parameter or expected model list changes, no real vendor receives an
    attestation, and no production posture/admission/billing logic is touched.
    ``verified_on`` is stamped at run time with a 90-day TTL so replay never
    expires.  The drawn pool is the non-overage synthetic ``opencode`` default,
    so no quota evidence is required.
    """
    cfg_dir = config_home / "saberops"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    today_iso = datetime.now(UTC).date().isoformat()
    cp_doc = {
        "version": 2,
        "providers": {
            "opencode": {"state": "ALLOWED"},
            "codex": {"state": "ALLOWED"},
            "cline": {"state": "ALLOWED"},
        },
        "quota_pools": {},
        "postures": {
            "opencode": {
                "class": "A",
                "policy_urls": ["https://example.com/synthetic-test-posture"],
                "ttl_days": 90,
                "verified_on": today_iso,
                "note": "synthetic verified test posture for ported dispatch fixtures",
            }
        },
    }
    (cfg_dir / "control_plane.json").write_text(json.dumps(cp_doc), encoding="utf-8")

@pytest.fixture()
def dispatch_posture(isolated: Path) -> Path:
    """Hermetic verified class-A posture for the synthetic ``opencode``
    dispatch fixtures in this module (setup-only fixture migration)."""
    _seed_verified_opencode_posture(isolated / "config")
    return isolated

def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t7@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "T-007"], cwd=path, check=True)
    (path / "Makefile").write_text("gate:\n\t@echo ok\n", encoding="utf-8")
    (path / "README.md").write_text("# t7\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=path, check=True, capture_output=True)
    return path

_R3E1_PROVIDER = "opencode"
_R3E1_WORKER_MODEL = "r3e1-worker-model"
_R3E1_REVIEWER_MODEL = "r3e1-reviewer-model"
_R3E1_CONNECTION = "r3e1-conn"


class _ScriptedAdapter(WorkerAdapter):
    """Hermetic adapter: implementer writes a file, reviewer passes."""

    def __init__(self, call_log: list[str], env_log: list[dict[str, str]]) -> None:
        self._call_log = call_log
        self._env_log = env_log

    @property
    def provider_name(self) -> str:
        return _R3E1_PROVIDER

    def is_available(self) -> bool:
        return True

    def run(self, request: WorkerRequest) -> WorkerResult:
        if request.read_only:
            self._call_log.append(f"review:{request.model}")
            self._env_log.append(dict(request.env) if request.env else {})
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
        (request.worktree_path / "r3e1.txt").write_text("r3e1 candidate\n", encoding="utf-8")
        return WorkerResult(
            provider=self.provider_name,
            model=request.model,
            status=AttemptStatus.SUCCESS,
            summary="Wrote r3e1.txt",
            stdout="",
            stderr="",
            changed_paths=["r3e1.txt"],
            has_changes=True,
        )

def _patch_implementer_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    def _resolve(**_: Any) -> list[WorkerCandidate]:
        return [WorkerCandidate(_R3E1_PROVIDER, _R3E1_WORKER_MODEL)]

    monkeypatch.setattr("saberops.orchestrator.resolve_candidate_chain", _resolve)

def test_ported_quick_workflow_skips_gate_and_review_through_acceptance(
    isolated: Path, dispatch_posture: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ported from test_quick_workflow_skips_gate_and_review_through_acceptance."""
    from saberops.accept import AcceptEngine
    from saberops.gate_runner import GateRunner
    from saberops.orchestrator import Orchestrator
    from saberops.project_settings import set_project_workflow
    from saberops.run_creation import RunRequest, resolve_effective_run_config

    repo = _git_repo(tmp_path / "quick" / "repo")
    set_project_workflow(repo, preset="quick")
    config = resolve_effective_run_config(
        RunRequest(task="Quick scaffold", repo_path=repo, publish_candidate=False)
    )
    assert not config.gate_enabled
    assert config.review_disabled
    set_project_workflow(repo, preset="balanced")
    calls: list[str] = []
    environments: list[dict[str, str]] = []
    _patch_implementer_chain(monkeypatch)
    db = Database(tmp_path / "quick" / "orch.db")
    orch = Orchestrator(
        db=db,
        registry=AdapterRegistry([_ScriptedAdapter(calls, environments)]),
        worktree_base=tmp_path / "quick" / "wt",
    )

    def forbidden_gate(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Quick must not execute a gate")

    monkeypatch.setattr("saberops.orchestrator.run_process", forbidden_gate)
    monkeypatch.setattr(GateRunner, "run", forbidden_gate)
    result = orch.run(config=config, is_peak=False)
    assert result.passed, result.summary
    assert db.get_reviews_for_run(result.run_id) == []
    checks = db.get_checks_for_run(result.run_id)
    assert checks and all(check.skipped and not check.passed for check in checks)
    assert db.get_events_by_type(result.run_id, "gate_skipped")
    assert db.get_events_by_type(result.run_id, "review_skipped_by_owner_policy")
    accepted = AcceptEngine(db).accept_run(result.run_id)
    assert accepted.accepted
    assert not accepted.gate_passed


@pytest.mark.parametrize("gate_enabled,review_disabled", [(True, True), (False, False)])
def test_ported_custom_workflow_gate_and_review_are_independent(
    isolated: Path,
    dispatch_posture: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gate_enabled: bool,
    review_disabled: bool,
) -> None:
    """Ported from test_custom_workflow_gate_and_review_are_independent."""
    from saberops.accept import AcceptEngine
    from saberops.orchestrator import Orchestrator
    from saberops.project_settings import set_project_gate_command, set_project_workflow
    from saberops.run_creation import RunRequest, resolve_effective_run_config

    repo = _git_repo(tmp_path / "repo")
    set_project_gate_command(repo, "true")
    set_project_workflow(
        repo,
        gate_enabled=gate_enabled,
        review="disabled" if review_disabled else "always-required",
        limit=3,
    )
    reviewer, profile, pool = materialize_discovered_binding(
        connection_id=_R3E1_CONNECTION,
        provider=_R3E1_PROVIDER,
        model=_R3E1_REVIEWER_MODEL,
        binding_role=BindingRole.REVIEWER,
    )
    save_owner_bindings(
        replace(
            upsert_owner_binding(
                OwnerBindings(), binding=reviewer, profile=profile, pool=pool
            ),
            reviewer_binding_id=reviewer.binding_id,
        )
    )
    _patch_implementer_chain(monkeypatch)
    calls: list[str] = []
    environments: list[dict[str, str]] = []
    db = Database(tmp_path / "orch.db")
    orch = Orchestrator(
        db=db,
        registry=AdapterRegistry([_ScriptedAdapter(calls, environments)]),
        worktree_base=tmp_path / "wt",
    )
    config = resolve_effective_run_config(
        RunRequest(task="Custom scaffold", repo_path=repo, publish_candidate=False)
    )
    result = orch.run(config=config, is_peak=False)
    assert result.passed, result.summary
    assert bool(db.get_reviews_for_run(result.run_id)) is not review_disabled
    checks = db.get_checks_for_run(result.run_id)
    assert checks and all(check.skipped is not gate_enabled for check in checks)
    accepted = AcceptEngine(db).accept_run(result.run_id)
    assert accepted.accepted
    assert accepted.gate_passed is gate_enabled
