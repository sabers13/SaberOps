"""PTY transcript helpers for the web terminal."""

from __future__ import annotations

from pathlib import Path

from orchestrator_mvp.db import Database


def get_transcript_dir(db: Database) -> Path:
    """Return the XDG orchestrator state dir for transcripts.

    Mirrors :func:`orchestrator_mvp.db.get_default_db_path` parent logic:
    ``<state_home>/orchestrator-mvp/transcripts``.
    """
    return Path(db.db_path).resolve().parent / "transcripts"


def get_transcript_path(db: Database, run_id: str) -> Path:
    """Return the ANSI transcript file path for a given run_id."""
    safe = "".join(c if c.isalnum() or c in ("_", "-", ".") else "_" for c in run_id)
    return get_transcript_dir(db) / f"{safe}.ansi"
