"""Run detail shows the resume capsule and frozen logical tools."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from saberops.db import Database
from saberops.models import Handoff, Run, RunStatus, Tier
from saberops.web import create_app


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    for name, sub in (("XDG_STATE_HOME", "state"), ("XDG_CONFIG_HOME", "config")):
        (tmp_path / sub).mkdir()
        monkeypatch.setenv(name, str(tmp_path / sub))
    for name in ("ORCH_ACCESS_STORE", "ORCH_ACCESS_MODELS_STORE", "ORCH_BINDING_STORE"):
        monkeypatch.delenv(name, raising=False)
    db = Database(tmp_path / "web.db")
    for run_id, tools in (("run_cap", ["github.read_issue"]), ("run_plain", [])):
        db.create_run(Run(
            id=run_id, task="probe", target_repo="/tmp/does-not-exist-repo",
            base_commit="base", tier=Tier.T1, training_allowed=True, gate_command="true",
            status=RunStatus.FAILED, created_at="2026-10-09T10:00:00+00:00",
            completed_at=None, final_attempt_id=None, project_id="proj",
            config_json=json.dumps({"tool_refs": tools}),
        ))
    db.create_handoff(Handoff("handoff_cap1", "run_cap", "handoff_resume_capsule", 1,
                              json.dumps({"NEXT_STEP": "fix the failing gate"}),
                              "2026-10-09T10:05:00+00:00"))
    return TestClient(create_app(db_path=tmp_path / "web.db"))


def test_capsule_and_tools_are_shown(client: TestClient) -> None:
    page = client.get("/runs/run_cap").text
    assert 'id="resume-capsule"' in page and "What the next attempt receives" in page
    assert "fix the failing gate" in page and "resume capsule v1" in page
    assert 'id="frozen-tools"' in page and "github.read_issue" in page


def test_sections_are_absent_without_evidence(client: TestClient) -> None:
    page = client.get("/runs/run_plain").text
    assert 'id="resume-capsule"' not in page and 'id="frozen-tools"' not in page
