"""C9 tests: run visibility for project-scoped CLI and Web Runs page."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from saberops.cli import main
from saberops.db import Database, current_iso_timestamp
from saberops.models import Run, RunStatus, Tier
from saberops.paths import get_default_db_path
from saberops.runtime import project_db_path_for_repo
from saberops.web import create_app


def _make_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "c9@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "C9"], cwd=path, check=True)
    (path / "README.md").write_text(f"# {path.name}\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)
    return path


@pytest.fixture()
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name, sub in (
        ("XDG_STATE_HOME", "state"),
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_CACHE_HOME", "cache"),
    ):
        d = tmp_path / sub
        d.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(name, str(d))
    monkeypatch.delenv("SABEROPS_PROFILE", raising=False)
    return tmp_path


def test_runs_visible_in_cli_and_web_for_second_project_from_fresh_store(
    isolated_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _make_repo(isolated_env / "proj1")
    proj2 = _make_repo(isolated_env / "proj2")

    db2_path = project_db_path_for_repo(proj2)
    db2 = Database(db2_path)
    db2.create_run(
        Run(
            id="run_c9_proj2_fail",
            task="c9 visible failed task",
            target_repo=str(proj2),
            gate_command="true",
            base_commit="0000000",
            training_allowed=False,
            created_at=current_iso_timestamp(),
            status=RunStatus.FAILED,
            tier=Tier.T1,
        )
    )

    code = main(["list", "--repo", str(proj2)])
    out = capsys.readouterr().out
    assert code == 0
    assert str(db2_path) in out
    assert "run_c9_proj2_fail" in out

    client = TestClient(create_app(repo_path=proj2))
    resp = client.get("/")
    assert resp.status_code == 200
    assert "c9 visible failed task" in resp.text
    assert str(db2_path) in resp.text


def test_list_and_web_without_repo_state_which_store_is_read(
    isolated_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    non_repo = isolated_env / "plain-dir"
    non_repo.mkdir()
    monkeypatch.chdir(non_repo)

    default_db_path = get_default_db_path()
    code = main(["list"])
    empty_out = capsys.readouterr().out
    assert code == 0
    assert str(default_db_path) in empty_out

    default_db = Database(default_db_path)
    default_db.create_run(
        Run(
            id="run_c9_default_fail",
            task="c9 default store task",
            target_repo=str(non_repo),
            gate_command="true",
            base_commit="0000000",
            training_allowed=False,
            created_at=current_iso_timestamp(),
            status=RunStatus.FAILED,
            tier=Tier.T1,
        )
    )

    code = main(["list"])
    filled_out = capsys.readouterr().out
    assert code == 0
    assert str(default_db_path) in filled_out
    assert "run_c9_default_fail" in filled_out

    client = TestClient(create_app())
    resp = client.get("/")
    assert resp.status_code == 200
    assert "c9 default store task" in resp.text
    assert str(default_db_path) in resp.text


# ---------------------------------------------------------------------------
# D7: the owner store the scheduler writes to, and the exact owner action
# ---------------------------------------------------------------------------


def test_list_for_a_repo_also_reads_the_owner_store_the_scheduler_writes(
    isolated_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """D7: a scheduled run lives in the owner store; listing must find it."""
    from saberops.db import get_default_db_path
    from saberops.project import compute_project_identity

    repo = _make_repo(isolated_env / "scheduled-proj")
    project_id = compute_project_identity(repo).project_id
    owner_path = get_default_db_path()
    owner_db = Database(owner_path)
    owner_db.create_run(
        Run(
            id="run_d7_owner_store",
            task="d7 scheduled task",
            target_repo=str(repo),
            gate_command="true",
            base_commit="0000000",
            training_allowed=False,
            created_at=current_iso_timestamp(),
            status=RunStatus.COMPLETED,
            tier=Tier.T1,
            project_id=project_id,
        )
    )

    code = main(["list", "--repo", str(repo)])
    out = capsys.readouterr().out
    assert code == 0
    assert "run_d7_owner_store" in out, out
    assert str(owner_path) in out
    assert str(project_db_path_for_repo(repo)) in out


def test_job_get_names_the_exact_owner_action(
    isolated_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """D7: ``orch job get`` must surface the same owner action ``wait_job`` does."""
    from saberops import candidate_lifecycle
    from saberops.accept import AcceptEngine
    from saberops.jobs import store as job_store

    db = Database(get_default_db_path())
    job_store.ensure_tables(db)
    repo = _make_repo(isolated_env / "job-get-proj")
    job_store.insert_job(
        db,
        _queued_job("job-d7-owner-action", repo, run_ids=("run_d7_action",)),
    )

    class _Projection:
        candidate_sha = "c0ffee1"

    monkeypatch.setattr(
        candidate_lifecycle, "project_candidate_state", lambda _db, _run_id: _Projection()
    )

    class _Eligibility:
        eligible = True

    monkeypatch.setattr(
        AcceptEngine, "check_accept_eligibility", lambda self, run_id: _Eligibility()
    )
    code = main(["job", "get", "job-d7-owner-action", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["result"]["owner_action"] == "orch accept run_d7_action"
    assert payload["result"]["candidate_sha"] == "c0ffee1"

    code = main(["job", "get", "job-d7-owner-action"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Owner action: orch accept run_d7_action" in out
    assert "Candidate  : c0ffee1 (run_d7_action)" in out


def _queued_job(job_id: str, repo: Path, *, run_ids: tuple[str, ...] = ()) -> Any:
    """One minimal queued job for store-level tests."""
    from saberops.jobs.models import (
        Job,
        JobRequirements,
        JobState,
        Priority,
        WaitingReason,
    )

    created = current_iso_timestamp()
    return Job(
        job_id=job_id,
        idempotency_key=None,
        caller_id="test",
        repo=str(repo),
        base_ref="main",
        base_sha="0000000",
        goal="d7 owner action",
        acceptance=None,
        requirements=JobRequirements(),
        priority=Priority.NORMAL,
        deadline=None,
        not_before=None,
        state=JobState.QUEUED,
        waiting_reason=WaitingReason.NEW,
        failure_reason=None,
        eta=None,
        eta_confidence=None,
        current_run_id=None,
        run_ids=run_ids,
        created_at=created,
        updated_at=created,
        request_digest=f"digest-{job_id}",
    )
