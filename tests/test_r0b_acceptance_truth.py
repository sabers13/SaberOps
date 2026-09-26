"""R0-B: the Web acceptance surface derives from AcceptEngine.

Proves the run page checklist and the Accept button use the
engine-derived read-only projection -- the Web holds no independent
acceptance policy table.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from saberops.accept import AcceptEngine
from saberops.db import Database
from saberops.models import (
    Attempt,
    AttemptStatus,
    ReviewResult,
    ReviewVerdict,
    Run,
    RunStatus,
    Tier,
)
from saberops.web import create_app

_CREATED = "2026-09-24T10:00:00+00:00"
_LATER = "2026-09-24T11:00:00+00:00"


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
    _git(repo, "config", "user.email", "r0b@example.com")
    _git(repo, "config", "user.name", "r0b")
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


def _client(db_path: Path) -> TestClient:
    return TestClient(create_app(db_path=db_path), raise_server_exceptions=False)


def _checks_by_key(db_path: Path, run_id: str) -> dict[str, dict[str, str]]:
    engine = AcceptEngine(db=Database(db_path))
    eligibility = engine.check_accept_eligibility(run_id)
    return {
        check.key: {"status": check.status, "reason": check.reason}
        for check in eligibility.checks
    }


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
    assert "Type the exact run id to confirm." in confirm.text


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
