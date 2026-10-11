"""Run evidence must refresh after dispatch and distinguish completion from acceptance."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from saberops.db import Database
from saberops.models import (
    Attempt,
    AttemptStatus,
    CheckResult,
    ReviewResult,
    ReviewVerdict,
    Run,
    RunStatus,
    Tier,
)
from saberops.web import create_app

ROOT = Path(__file__).resolve().parents[1]
CREATED = "2026-10-05T20:34:54+00:00"
COMPLETED = "2026-10-05T20:44:36+00:00"


@pytest.fixture()
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Database, TestClient]:
    for key, folder in (
        ("XDG_STATE_HOME", "state"),
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_CACHE_HOME", "cache"),
    ):
        monkeypatch.setenv(key, str(tmp_path / folder))
    for key in (
        "SABEROPS_PROFILE",
        "ORCH_ACCESS_STORE",
        "ORCH_ACCESS_MODELS_STORE",
        "ORCH_BINDING_STORE",
        "ORCH_READINESS_STORE",
    ):
        monkeypatch.delenv(key, raising=False)
    path = tmp_path / "runs.db"
    return Database(path), TestClient(create_app(db_path=path))


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def seed_candidate(db: Database, tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "project"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "live@localhost")
    git(repo, "config", "user.name", "Live results")
    (repo / "README.md").write_text("Base\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-b", "candidate")
    (repo / "app.py").write_text("print('hello')\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "candidate")
    candidate = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "main")
    db.create_run(
        Run(
            id="run-live",
            task="Build a local app",
            target_repo=str(repo),
            base_commit=base,
            tier=Tier.T1,
            training_allowed=True,
            gate_command="make gate",
            status=RunStatus.RUNNING,
            created_at=CREATED,
        )
    )
    return repo, base, candidate


def complete(db: Database, candidate: str, base: str) -> None:
    db.create_attempt(
        Attempt(
            id="a1",
            run_id="run-live",
            attempt_number=1,
            provider="opencode",
            model="test",
            worktree_path="/tmp/candidate",
            branch_name="candidate",
            status=AttemptStatus.SUCCESS,
            commit_sha=candidate,
            base_sha=base,
            created_at=CREATED,
            worker_stdout="Built the task board. Start with make run.",
        )
    )
    db.create_check(
        CheckResult(
            id="c1",
            attempt_id="a1",
            gate_command="make gate",
            exit_code=0,
            stdout="All checks passed",
            stderr="",
            passed=True,
            created_at=COMPLETED,
        )
    )
    db.create_review(
        ReviewResult(
            id="r1",
            run_id="run-live",
            provider="anthropic",
            model="sonnet",
            verdict=ReviewVerdict.PASS,
            summary="Approved",
            created_at=COMPLETED,
        )
    )
    db.update_run(
        "run-live",
        RunStatus.COMPLETED,
        completed_at=COMPLETED,
        final_attempt_id="a1",
        result_summary="Built app <script>alert(1)</script>",
    )


def result_region(html: str) -> str:
    match = re.search(r'data-run-refresh="result".*?(?=data-run-refresh="plan")', html, re.S)
    assert match
    return match.group()


def test_running_to_completed_snapshot_exposes_all_evidence(
    state: tuple[Database, TestClient],
    tmp_path: Path,
) -> None:
    db, client = state
    repo, base, candidate = seed_candidate(db, tmp_path)
    running = client.get("/runs/run-live").text
    assert "Awaiting your acceptance" not in running
    for key in (
        "chat-result",
        "chat-actions",
        "result",
        "plan",
        "details",
        "checklist",
        "workers",
        "changes",
        "checks",
        "review",
        "routing",
        "usage",
        "monitoring",
        "worker-filters",
        "worker-dock",
        "worker-footer",
    ):
        assert f'data-run-refresh="{key}"' in running
    # Unsaved owner input, acceptance confirmations, SSE and PTY stay outside
    # these explicitly marked replacement regions.
    assert 'id="runningTask"' in running
    assert 'data-panel="terminal" data-run-refresh' not in running
    assert 'data-panel="events" data-run-refresh' not in running
    complete(db, candidate, base)
    before = len(db.get_events("run-live"))
    html = client.get("/runs/run-live").text
    result = result_region(html)
    assert "Awaiting your acceptance" in result
    assert "1 changed file in the recorded candidate" in result
    assert "Candidate folder:" in result and "/tmp/candidate" in result
    assert "Checks: 1 passed, 0 failed, 0 skipped. Review: PASS." in result
    assert "What the worker built" in result
    assert "Built the task board. Start with make run." in result
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in result
    assert "<script>alert(1)</script>" not in result
    assert "All checks passed" in html and "Approved" in html and "app.py" in html
    assert git(repo, "rev-parse", "HEAD") == base
    assert len(db.get_events("run-live")) == before


@pytest.mark.parametrize("decision", ["accepted", "rejected"])
def test_result_distinguishes_owner_decisions(
    state: tuple[Database, TestClient],
    tmp_path: Path,
    decision: str,
) -> None:
    db, client = state
    _, base, candidate = seed_candidate(db, tmp_path)
    complete(db, candidate, base)
    db.record_event(
        "run-live",
        "run_accepted" if decision == "accepted" else "candidate_rejected",
        payload={"candidate_sha": candidate, "final_branch": "main"},
    )
    result = result_region(client.get("/runs/run-live").text)
    assert "Awaiting your acceptance" not in result
    assert (
        "<strong>Accepted.</strong>"
        if decision == "accepted"
        else "<strong>Candidate rejected.</strong>"
    ) in result


def test_completed_without_candidate_does_not_claim_ready_for_acceptance(
    state: tuple[Database, TestClient],
    tmp_path: Path,
) -> None:
    db, client = state
    seed_candidate(db, tmp_path)
    db.update_run("run-live", RunStatus.COMPLETED, completed_at=COMPLETED)
    result = result_region(client.get("/runs/run-live").text)
    assert "No result summary was recorded" in result
    assert "Awaiting your acceptance" not in result
    assert "Candidate folder:" not in result


def test_live_controller_refresh_ordering_drafts_timer_and_recovery() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable; browser interaction still requires verification")
    result = subprocess.run(
        [
            node,
            str(ROOT / "tests/js/run_live_refresh.cjs"),
            str(ROOT / "src/saberops/static/js/app.js"),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_total_changes_uses_exact_counts_and_same_expandable_diff(
    state: tuple[Database, TestClient],
    tmp_path: Path,
) -> None:
    db, client = state
    _, base, candidate = seed_candidate(db, tmp_path)
    complete(db, candidate, base)
    projected = client.get("/runs/run-live/diff").json()
    assert projected["totals"] == {
        "added": 1,
        "removed": 0,
        "binary": 0,
        "files": 1,
        "available": True,
    }
    page = client.get("/runs/run-live").text
    assert 'class="file-diff diff-total" data-diff-total' in page
    assert "Total changes" in page and page.count("print(&#39;hello&#39;)") == 2


def test_composer_cancellation_scope_modes_and_failure_recovery() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable; browser interaction still requires verification")
    result = subprocess.run(
        [node, str(ROOT / "tests/js/composer_controls.cjs"),
         str(ROOT / "src/saberops/static/js/composer-controls.js")],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
