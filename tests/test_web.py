"""C16-C2: minimal status page for the SaberOps engine (L-A step 5).

The workspace console (chats, projects, sessions, settings, terminal) was
cut; the page keeps exactly: run list, run detail (status, attempts, gate
results, review verdict, diff), report JSON, accept/reject actions, health.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from saberops.db import Database
from saberops.models import Run, RunStatus, Tier
from saberops.web import create_app

_CREATED = "2026-09-23T10:00:00+00:00"


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


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(db_path=tmp_path / "web.db"))


def _seed_run(
    tmp_path: Path,
    run_id: str = "run_c16c2",
    status: RunStatus = RunStatus.RUNNING,
) -> None:
    Database(tmp_path / "web.db").create_run(
        Run(
            id=run_id,
            task="console integration probe",
            target_repo="/tmp/does-not-exist-repo",
            base_commit="base-commit-sha",
            tier=Tier.T1,
            training_allowed=True,
            gate_command="true",
            status=status,
            created_at=_CREATED,
            completed_at=None,
            final_attempt_id=None,
            project_id="proj_c16c2",
            config_json=json.dumps({}),
        )
    )


def test_c16c2_dashboard_lists_runs_with_kept_tokens(
    isolated_xdg: None, tmp_path: Path
) -> None:
    """Minimal dashboard lists runs and reuses the approved CSS tokens."""
    _seed_run(tmp_path)
    page = _client(tmp_path).get("/")
    assert page.status_code == 200
    text = page.text
    assert "/static/css/console-v2.css" in text
    assert "/static/js/console.js" in text
    assert "console integration probe" in text
    assert 'href="/runs/run_c16c2"' in text
    # Removed console surfaces stay gone.
    for gone in (
        "/access",
        "/routing",
        "/quota",
        "/inbox",
        "/setup",
        "/chats",
        "chat-composer",
        "resolveDecision",
        "Preview completed run",
    ):
        assert gone not in text, gone


def test_c16c2_run_detail_renders_status_evidence_and_actions(
    isolated_xdg: None, tmp_path: Path
) -> None:
    """Run page shows status, attempts, gate, review, diff, and owner actions."""
    client = _client(tmp_path)
    _seed_run(tmp_path, "run_c16c2_live", RunStatus.RUNNING)
    live = client.get("/runs/run_c16c2_live")
    assert live.status_code == 200
    text = live.text
    for section in (
        "Attempts · gate results",
        "Review verdict",
        "Candidate diff",
        "Owner actions",
        "Events",
    ):
        assert section in text, section
    assert 'data-run-refresh="events-status"' in text
    assert 'href="/runs/run_c16c2_live/report"' in text
    # Removed run operations stay gone.
    for gone in (
        "/runs/run_c16c2_live/cancel",
        "/runs/run_c16c2_live/retry",
        "/runs/run_c16c2_live/review",
        "/runs/run_c16c2_live/cleanup",
        "/runs/run_c16c2_live/continue",
        "runComposerStopForm",
        "resolveDecision",
    ):
        assert gone not in text, gone


def test_c16c2_missing_run_keeps_minimal_page(
    isolated_xdg: None, tmp_path: Path
) -> None:
    client = _client(tmp_path)
    page = client.get("/runs/does-not-exist")
    assert page.status_code == 404
    assert "Run not found" in page.text
    assert "SaberOps · minimal status" in page.text


def test_c16c2_report_and_health_stay_json(
    isolated_xdg: None, tmp_path: Path
) -> None:
    _seed_run(tmp_path, "run_c16c2_done", RunStatus.COMPLETED)
    client = _client(tmp_path)
    report = client.get("/runs/run_c16c2_done/report")
    assert report.status_code == 200
    assert report.json()["identity"]["run_id"] == "run_c16c2_done"
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["product"] == "SaberOps"


def test_c16c2_console_assets_are_served(
    isolated_xdg: None, tmp_path: Path
) -> None:
    client = _client(tmp_path)
    for asset in (
        "/static/css/console-v2.css",
        "/static/css/saberops-console.css",
        "/static/js/console.js",
        "/static/js/app.js",
    ):
        response = client.get(asset)
        assert response.status_code == 200, asset
        assert len(response.text) > 100, asset
    # Removed feature scripts stay gone.
    assert client.get("/static/js/chat.js").status_code == 404
    assert client.get("/static/js/terminal.js").status_code == 404
