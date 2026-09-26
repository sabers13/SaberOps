"""R0-B: one SSE owner, one theme implementation, cheap shell middleware.

Uses seams/mocks/counters rather than timing-based assertions.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

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
from saberops.projects import ProjectRegistry
from saberops.web import create_app

_CREATED = "2026-09-24T10:00:00+00:00"


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


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(db_path=tmp_path / "web.db"), raise_server_exceptions=False)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _make_repo(root: Path, name: str) -> tuple[Path, str, str]:
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
    return repo, base, candidate


def test_r0b_run_page_has_one_sse_owner(isolated_xdg: None, tmp_path: Path) -> None:
    """Exactly one EventSource owner per run page; no duplicate loops."""
    client = _client(tmp_path)
    app_js = client.get("/static/js/app.js")
    console_js = client.get("/static/js/console.js")
    assert app_js.status_code == 200
    assert console_js.status_code == 200
    assert app_js.text.count("new EventSource") == 1
    assert console_js.text.count("new EventSource") == 0
    assert "watchEventStream" not in console_js.text
    # The single owner refreshes action state from the server on lifecycle change.
    assert "/actions" in app_js.text
    assert "refreshActionState" in app_js.text


def test_r0b_one_theme_implementation(isolated_xdg: None, tmp_path: Path) -> None:
    """Theme read/toggle/persist/control-state lives in exactly one file."""
    client = _client(tmp_path)
    app_js = client.get("/static/js/app.js").text
    console_js = client.get("/static/js/console.js").text
    assert "saberops-theme" in console_js
    assert "saberops-theme" not in app_js
    assert "orch-theme" not in app_js
    assert "data-set-theme" in console_js
    assert "data-set-theme" not in app_js
    assert 'setAttribute("data-theme"' not in app_js
    assert "toggleTheme" in console_js
    assert "toggleTheme" not in app_js


def test_r0b_action_state_refreshes_after_lifecycle_change(
    isolated_xdg: None, tmp_path: Path
) -> None:
    """The actions endpoint follows durable state (review arrival flips Accept)."""
    repo, base, candidate = _make_repo(tmp_path, "repo-actions")
    db_path = tmp_path / "web.db"
    Database(db_path).create_run(
        Run(
            id="run_r0b_actions",
            task="r0b actions probe",
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
            config_json=json.dumps({"review_enabled": True}),
        )
    )
    Database(db_path).create_attempt(
        Attempt(
            id="run_r0b_actions-a1",
            run_id="run_r0b_actions",
            attempt_number=1,
            provider="opencode",
            model="test-model",
            worktree_path=str(repo),
            branch_name="r0b-actions-branch",
            status=AttemptStatus.SUCCESS,
            commit_sha=candidate,
            created_at=_CREATED,
            base_sha=base,
        )
    )
    client = _client(tmp_path)

    before = client.get("/runs/run_r0b_actions/actions")
    assert before.status_code == 200
    assert before.json()["status"] == "COMPLETED"
    assert before.json()["can_accept"] is False
    assert before.json()["can_review"] is True
    assert before.json()["can_cancel"] is False

    Database(db_path).create_review(
        ReviewResult(
            id="run_r0b_actions-rev1",
            run_id="run_r0b_actions",
            provider="opencode",
            model="reviewer",
            verdict=ReviewVerdict.PASS,
            summary="looks good",
            details="",
            created_at=_CREATED,
        )
    )

    after = client.get("/runs/run_r0b_actions/actions")
    assert after.status_code == 200
    assert after.json()["can_accept"] is True
    review_checks = [c for c in after.json()["accept_checklist"] if c["key"] == "review"]
    assert review_checks and review_checks[0]["status"] == "pass"

    missing = client.get("/runs/does-not-exist/actions")
    assert missing.status_code == 404


def test_r0b_static_and_health_skip_project_validation(
    isolated_xdg: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Neither /static/* nor /health triggers project Git validation."""
    calls: list[str] = []
    original = ProjectRegistry.validate_project

    def counting(self: ProjectRegistry, path: Path | str):  # type: ignore[no-untyped-def]
        calls.append(str(path))
        return original(self, path)

    monkeypatch.setattr(ProjectRegistry, "validate_project", counting)
    client = _client(tmp_path)
    calls.clear()

    static = client.get("/static/js/console.js")
    assert static.status_code == 200
    assert calls == []

    health = client.get("/health")
    assert health.status_code == 200
    assert calls == []


def test_r0b_repeated_requests_do_not_revalidate_projects(
    isolated_xdg: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bounded cache absorbs repeated shell/dashboard validation."""
    calls: list[str] = []
    original = ProjectRegistry.validate_project

    def counting(self: ProjectRegistry, path: Path | str):  # type: ignore[no-untyped-def]
        calls.append(str(path))
        return original(self, path)

    monkeypatch.setattr(ProjectRegistry, "validate_project", counting)
    client = _client(tmp_path)
    calls.clear()

    first = client.get("/")
    assert first.status_code == 200
    first_calls = len(calls)
    assert first_calls > 0

    second = client.get("/")
    assert second.status_code == 200
    assert len(calls) == first_calls


def test_r0b_project_select_invalidates_cached_validation(
    isolated_xdg: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Selecting a project invalidates the cached entry so render is live."""
    repo_b, _, _ = _make_repo(tmp_path, "repo-select-b")
    calls: list[str] = []
    original = ProjectRegistry.validate_project

    def counting(self: ProjectRegistry, path: Path | str):  # type: ignore[no-untyped-def]
        calls.append(str(path))
        return original(self, path)

    monkeypatch.setattr(ProjectRegistry, "validate_project", counting)
    client = TestClient(
        create_app(db_path=tmp_path / "web.db"),
        raise_server_exceptions=False,
        follow_redirects=False,
    )
    calls.clear()

    assert client.get("/").status_code == 200

    select = client.post("/projects/select", data={"path": str(repo_b)})
    assert select.status_code == 303
    assert str(repo_b) in calls

    calls_after_select = len(calls)
    assert client.get("/").status_code == 200
    # The dashboard/shell revalidated live after invalidation.
    assert len(calls) > calls_after_select
    assert str(repo_b) in calls[calls_after_select:]
