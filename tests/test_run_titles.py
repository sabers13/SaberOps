"""Run-title rules (ported from the removed project-chat navigation tests).

Ported run-title half of test_project_chat_navigation.py; the chat halves
were removed with the console."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from saberops.db import Database
from saberops.models import (
    Run,
    RunStatus,
    Tier,
)
from saberops.run_titles import clean_title, fallback_title


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
