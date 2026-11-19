"""Durable, deterministic desktop delivery for job and question events."""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def notify_once(ledger: Path, key: str, title: str, body: str) -> None:
    """Send a notice once per key, serialized across scheduler processes."""
    if os.environ.get("SABEROPS_DESKTOP_NOTIFICATIONS", "1").lower() in {"0", "false", "off"}:
        return
    command = shutil.which("notify-send")
    if command is None:
        return
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(ledger, timeout=10) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS sent (event_key TEXT PRIMARY KEY)")
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM sent WHERE event_key = ?", (key,)).fetchone():
            return
        try:
            subprocess.run(
                [command, title, body],
                check=True,
                timeout=5,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("desktop notification failed: %s", exc)
            return
        # The desktop service cannot participate in SQLite transactions. A crash
        # after delivery but before this commit can produce a duplicate on restart.
        conn.execute("INSERT INTO sent VALUES (?)", (key,))


def deliver_pending(db: Any) -> None:
    """Scan durable job and question records; repeated ticks are idempotent."""
    from saberops.jobs.store import list_jobs_tolerant
    from saberops.questions import OwnerQuestionQueue

    ledger = db.owner_db.db_path.with_name(db.owner_db.db_path.name + ".notifications.sqlite3")
    jobs, _ = list_jobs_tolerant(db, limit=100_000)
    for job in jobs:
        if job.state.value in {"SUCCEEDED", "FAILED"}:
            label = "finished" if job.state.value == "SUCCEEDED" else "failed"
            notify_once(
                ledger, f"job:{job.job_id}:{job.state.value}", f"SaberOps job {label}", job.job_id
            )
    for question in OwnerQuestionQueue().questions():
        notify_once(
            ledger,
            f"question:{question.question_id}",
            "SaberOps question asked",
            question.request.subject,
        )
        if question.request.blocks or question.request.run_id:
            notify_once(
                ledger,
                f"attention:{question.question_id}",
                "SaberOps needs attention",
                question.request.subject,
            )
