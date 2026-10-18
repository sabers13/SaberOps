"""Run-title rules (ported from the removed project-chat navigation tests).

Ported run-title half of test_project_chat_navigation.py; the chat halves
were removed with the console."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from saberops.db import Database
from saberops.models import (
    Run,
    RunStatus,
    Tier,
)
from saberops.run_titles import clean_title, fallback_title, parse_named_guidance


def _nav_run(repo: str, identity: str = "run_navigation") -> Run:
    return Run(
        id=identity,
        task="Build “Incident Desk,” an issue tracker in " + repo,
        target_repo=repo,
        base_commit="base",
        tier=Tier.T1,
        training_allowed=False,
        gate_command="true",
        status=RunStatus.COMPLETED,
        created_at="2026-10-09T10:00:00Z",
    )

def test_ported_legacy_name_migration_preserves_objective(tmp_path: Path) -> None:
    """Ported from test_legacy_name_migration_preserves_objective."""
    path = tmp_path / "runs.db"
    original = _nav_run("/repos/project")
    Database(path).create_run(original)
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE runs DROP COLUMN display_title")
    legacy = Database(path, read_only=True).get_run(original.id)
    assert legacy and legacy.display_title is None
    migrated = Database(path)
    migrated.set_run_title(original.id, "Issue tracker")
    saved = migrated.get_run(original.id)
    assert saved and saved.task == original.task and saved.status == original.status
    assert saved.display_title == "Issue tracker"


def test_ported_run_title_survives_lifecycle_updates(tmp_path: Path) -> None:
    """Ported from test_run_title_survives_creation_and_lifecycle_updates."""
    from dataclasses import replace as _replace

    db = Database(tmp_path / "runs.db")
    original = _replace(_nav_run("/repos/project"), display_title="Issue tracker")
    db.create_run(original)
    db.update_run(original.id, RunStatus.FAILED, result_summary="Gate failed")
    saved = Database(db.db_path).get_run(original.id)
    assert saved and saved.display_title == "Issue tracker" and saved.task == original.task


def test_ported_name_bounds_and_transcript_fallback() -> None:
    """Ported from test_name_bounds_and_transcript_fallback."""
    repo = "/repos/my-project"
    assert (
        fallback_title("Owner conversation: Build “Incident Desk,” in " + repo, repo)
        == "Build Incident Desk"
    )
    assert fallback_title("", repo) == "Engineering task"
    label = clean_title("my-project /repos/my-project <b>Fix login\n" + "problem " * 20, repo)
    assert len(label) <= 80 and len(label.split()) <= 10
    assert "my-project" not in label and "/repos" not in label and "<" not in label
    assert parse_named_guidance("Legacy advisory") == ("", "Legacy advisory")


def test_ported_duplicate_names_reserved_atomically(tmp_path: Path) -> None:
    """Ported from test_duplicate_names_are_reserved_atomically_and_survive_restart."""
    from concurrent.futures import ThreadPoolExecutor

    db = Database(tmp_path / "runs.db")
    originals = [_nav_run("/repos/project", f"run_{index}") for index in range(8)]
    for original in originals:
        db.create_run(original)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(
            pool.map(
                lambda item: db.set_run_title(item.id, "Build shared issue tracker"),
                originals,
            )
        )
    titles = {item.id: item.display_title for item in db.list_runs()}
    assert len({title.casefold() for title in titles.values() if title}) == 8
    restarted = Database(db.db_path)
    assert {item.id: item.display_title for item in restarted.list_runs()} == titles


def test_ported_legacy_run_labels_upgrade(tmp_path: Path) -> None:
    """Ported from the run half of test_legacy_labels_upgrade_without_rewriting_transcripts."""
    db = Database(tmp_path / "runs.db")
    db.create_run(_nav_run("/repos/project", "first"))
    db.create_run(_nav_run("/repos/project", "second"))
    with sqlite3.connect(db.db_path) as conn:
        conn.execute("UPDATE runs SET display_title='Incident Desk'")
    upgraded = Database(db.db_path)
    labels = [item.display_title for item in upgraded.list_runs()]
    assert set(labels) == {
        "Build Incident Desk issue tracker",
        "Build Incident Desk issue tracker · 2",
    }


@pytest.mark.parametrize(
    "output,expected",
    [
        (json.dumps({"title": "project Fix login", "guidance": "Keep checks."}), "Fix login"),
        ("Legacy advisory", None),
        ('{"title": "Fix login", "guidance": false}', None),
    ],
)
def test_ported_startup_names_fetch_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output: str, expected: str | None
) -> None:
    """Ported from test_startup_names_with_one_existing_call (fetch-once half)."""
    from saberops.orchestrator import _InitialOrchestratorGuidanceCache
    from saberops.orchestrator_consumer import (
        OrchestratorModelConsumer,
        OrchestratorModelRequest,
        OrchestratorModelResult,
    )

    db = Database(tmp_path / "runs.db")
    original = _nav_run("/repos/project")
    db.create_run(original)
    db.create_run(
        replace(original, id="earlier_run", display_title="Build previous account settings")
    )
    requests: list[OrchestratorModelRequest] = []

    def invoke(
        self: OrchestratorModelConsumer, request: OrchestratorModelRequest
    ) -> OrchestratorModelResult:
        requests.append(request)
        return OrchestratorModelResult(
            status="SUCCESS",
            binding_id="b",
            profile_id="p",
            quota_pool_id="q",
            provider="stub",
            model="stub",
            backend="stub",
            reasoning_effort=None,
            output_text=output,
            summary="",
            error=None,
            prompt_bytes=0,
            duration_seconds=0,
            correlation_id=None,
            evidence_run_id=original.id,
            evidence_event_id=0,
        )

    monkeypatch.setattr(OrchestratorModelConsumer, "invoke", invoke)
    cache = _InitialOrchestratorGuidanceCache()
    service = SimpleNamespace(db=db)
    guidance = cache.get_or_fetch(
        service=service, original_task=original.task, run_id=original.id
    )
    assert (
        cache.get_or_fetch(service=service, original_task=original.task, run_id=original.id)
        == guidance
    )
    assert len(requests) == 1 and "title and guidance" in requests[0].system_prompt
    saved = db.get_run(original.id)
    assert saved and saved.display_title == (
        expected or fallback_title(original.task, original.target_repo)
    )
    assert (
        sum(e.event_type == "orchestrator_guidance_result" for e in db.get_events(original.id))
        == 1
    )


def test_ported_unavailable_orchestrator_keeps_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ported from test_unavailable_orchestrator_keeps_fallback."""
    from saberops.orchestrator import _InitialOrchestratorGuidanceCache
    from saberops.orchestrator_consumer import (
        OrchestratorModelConsumer,
        OrchestratorModelInvocationError,
        OrchestratorModelRequest,
        OrchestratorModelResult,
    )

    db = Database(tmp_path / "runs.db")
    original = _nav_run("/repos/project")
    db.create_run(original)

    def refuse(
        self: OrchestratorModelConsumer, request: OrchestratorModelRequest
    ) -> OrchestratorModelResult:
        raise OrchestratorModelInvocationError("ORCH_MODEL_NO_POLICY", message="Unavailable")

    monkeypatch.setattr(OrchestratorModelConsumer, "invoke", refuse)
    cache = _InitialOrchestratorGuidanceCache()
    text, reason = cache.get_or_fetch(
        service=SimpleNamespace(db=db), original_task=original.task, run_id=original.id
    )
    assert not text and reason
    saved = db.get_run(original.id)
    assert saved and saved.task == original.task
    assert (
        fallback_title(saved.task, saved.target_repo) == "Build Incident Desk issue tracker"
    )
