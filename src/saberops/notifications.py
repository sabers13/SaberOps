"""Durable, deterministic desktop delivery for job and question events."""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def notification_settings_path() -> Path:
    from saberops.paths import saberops_config_dir

    return saberops_config_dir() / "notifications.json"


def saved_notifications_enabled() -> bool:
    path = notification_settings_path()
    if not path.exists():
        return True
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("enabled"), bool):
        raise ValueError(f"invalid notification settings: {path}")
    return bool(data["enabled"])


def notifications_enabled() -> bool:
    override = os.environ.get("SABEROPS_DESKTOP_NOTIFICATIONS")
    if override is not None:
        return override.lower() not in {"0", "false", "off"}
    return saved_notifications_enabled()


def set_notifications_enabled(enabled: bool) -> None:
    path = notification_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".notifications-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"enabled": enabled}) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def notify_once(ledger: Path, key: str, title: str, body: str) -> None:
    """Send a notice once per key, serialized across scheduler processes."""
    try:
        if not notifications_enabled():
            return
    except (OSError, ValueError) as exc:
        logger.warning("invalid desktop notification settings: %s", exc)
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
    """Scan durable job and question records; repeated ticks are idempotent.

    The scan is read-only: ``quarantine=False`` keeps it from rewriting a row
    it cannot materialise.  Delivery runs before the scheduler tick's
    fail-closed RUNNING guards, and quarantining a legacy RUNNING row there
    would erase the evidence those guards base their owner-visible hold on.
    """
    from saberops.jobs.store import list_jobs_tolerant
    from saberops.questions import OwnerQuestionQueue, is_critical_question
    from saberops.scout import latest_report

    ledger = db.owner_db.db_path.with_name(db.owner_db.db_path.name + ".notifications.sqlite3")
    jobs, _ = list_jobs_tolerant(db, limit=100_000, quarantine=False)
    for job in jobs:
        if job.state.value in {"SUCCEEDED", "FAILED"}:
            label = "finished" if job.state.value == "SUCCEEDED" else "failed"
            body = job.job_id
            if job.kind.value == "SCOUT":
                try:
                    report = latest_report(db, job.job_id)
                except Exception:  # noqa: BLE001 - a notice never breaks delivery
                    report = None
                if report and report.get("report_path"):
                    summary = report.get("summary") or "no summary"
                    body = f"{job.job_id}\n{summary}\nreport: {report['report_path']}"
            notify_once(
                ledger, f"job:{job.job_id}:{job.state.value}", f"SaberOps job {label}", body
            )
    for question in OwnerQuestionQueue().questions():
        critical = is_critical_question(question)
        notify_once(
            ledger,
            f"question:{question.question_id}",
            "SaberOps critical question" if critical else "SaberOps question asked",
            question.request.subject,
        )
        if question.request.blocks or question.request.run_id:
            notify_once(
                ledger,
                f"attention:{question.question_id}",
                "SaberOps needs attention",
                question.request.subject,
            )
