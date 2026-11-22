"""Durable, deterministic desktop delivery for job and question events."""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import UTC, datetime
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


def _ensure_delivery_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS delivery (
            event_key TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            detail TEXT NOT NULL,
            attempted_at TEXT NOT NULL
        )
        """
    )


#: How long a recorded non-delivery status suppresses another attempt.  A
#: headless host has no desktop transport, and retrying ``notify-send`` on
#: every scheduler tick turned one notice into thousands of process launches
#: (report §12.6: 22,863 calls in 26 minutes).  The durable record stays the
#: truth -- status and time are still written -- but a failed or unavailable
#: key is not retried until its window has passed.  ``unavailable`` waits
#: longer because a missing session bus is an environment fact, while a
#: transient ``failed`` attempt deserves an earlier retry.
DELIVERY_RETRY_SECONDS: dict[str, float] = {
    "failed": 300.0,
    "unavailable": 3600.0,
}

#: Lower-case markers that mean "there is no desktop transport here", not
#: "this one notification failed".  The message is vendor text, so it is only
#: used to classify the recorded status; it is never shown as authority.
_NO_TRANSPORT_MARKERS: tuple[str, ...] = (
    "dbus",
    "session bus",
    "autolaunch",
    "could not connect to display",
    "cannot open display",
)


def _retry_is_due(status: str, attempted_at: str) -> bool:
    """True when a recorded attempt is outside its retry window."""
    delay = DELIVERY_RETRY_SECONDS.get(status, 0.0)
    if delay <= 0:
        return True
    try:
        attempted = datetime.fromisoformat(attempted_at)
    except (TypeError, ValueError):
        return True
    if attempted.tzinfo is None:
        attempted = attempted.replace(tzinfo=UTC)
    return (datetime.now(UTC) - attempted).total_seconds() >= delay


def _classify_failure(detail: str) -> str:
    """Map a ``notify-send`` failure to ``unavailable`` or ``failed``."""
    lowered = detail.lower()
    if any(marker in lowered for marker in _NO_TRANSPORT_MARKERS):
        return "unavailable"
    return "failed"


def _record_attempt(
    conn: sqlite3.Connection, key: str, status: str, detail: str = ""
) -> None:
    """Record one delivery attempt independently of desktop success."""
    _ensure_delivery_table(conn)
    conn.execute(
        """
        INSERT INTO delivery (event_key, status, detail, attempted_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(event_key) DO UPDATE SET
            status = excluded.status,
            detail = excluded.detail,
            attempted_at = excluded.attempted_at
        """,
        (key, status, detail, datetime.now(UTC).isoformat()),
    )


def notify_once(ledger: Path, key: str, title: str, body: str) -> str:
    """Send a notice once per key, serialized across scheduler processes.

    The desktop channel is best effort, but the *attempt* is durable: when
    ``notify-send`` is missing or has no session bus (headless hosts, a
    benchmark store with no desktop), the outcome is recorded as
    ``unavailable``/``failed`` instead of leaving the key absent.  Callers that
    account for notice delivery (the real-mode benchmark) can therefore wait on
    the engine's own record instead of requiring a working desktop bus.  A
    recorded non-delivery is retried only after its backoff window
    (:data:`DELIVERY_RETRY_SECONDS`), so a headless host does not launch a
    doomed notifier on every tick; the durable status still records what
    happened.
    """
    try:
        if not notifications_enabled():
            return "disabled"
    except (OSError, ValueError) as exc:
        logger.warning("invalid desktop notification settings: %s", exc)
        return "disabled"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    command = shutil.which("notify-send")
    with sqlite3.connect(ledger, timeout=10) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS sent (event_key TEXT PRIMARY KEY)")
        _ensure_delivery_table(conn)
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM sent WHERE event_key = ?", (key,)).fetchone():
            return "duplicate"
        if command is None:
            _record_attempt(conn, key, "unavailable", "notify-send is not installed")
            return "unavailable"
        previous = conn.execute(
            "SELECT status, attempted_at FROM delivery WHERE event_key = ?", (key,)
        ).fetchone()
        if previous is not None:
            previous_status, attempted_at = str(previous[0]), str(previous[1])
            if not _retry_is_due(previous_status, attempted_at):
                return previous_status
        try:
            completed = subprocess.run(
                [command, title, body],
                check=False,
                timeout=5,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("desktop notification failed: %s", exc)
            _record_attempt(conn, key, "failed", str(exc)[:500])
            return "failed"
        if completed.returncode != 0:
            detail = (
                completed.stderr.strip()
                or completed.stdout.strip()
                or f"notify-send exited with status {completed.returncode}"
            )[:500]
            status = _classify_failure(detail)
            if status == "failed":
                logger.warning("desktop notification failed: %s", detail)
            _record_attempt(conn, key, status, detail)
            return status
        _record_attempt(conn, key, "delivered")
        # The desktop service cannot participate in SQLite transactions. A crash
        # after delivery but before this commit can produce a duplicate on restart.
        conn.execute("INSERT INTO sent VALUES (?)", (key,))
        return "delivered"


def notice_attempted(ledger: Path, key: str) -> bool:
    """True once any delivery attempt for *key* was durably recorded."""
    if not ledger.exists():
        return False
    try:
        with sqlite3.connect(ledger, timeout=5) as conn:
            row = conn.execute(
                "SELECT 1 FROM delivery WHERE event_key = ?", (key,)
            ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def run_notification_ledger(db: Any) -> Path:
    """The owner-store ledger a direct run's terminal notice is recorded in."""
    owner_db = getattr(db, "owner_db", db)
    path = Path(owner_db.db_path)
    return path.with_name(path.name + ".notifications.sqlite3")


def notify_run_terminal(
    db: Any, *, run_id: str, status: str, summary: str | None = None
) -> str:
    """Deliver one durable terminal notice for a job-less direct run (N16).

    Scheduler-dispatched runs are already covered by :func:`deliver_pending`'s
    job scan, so a run that any job owns is skipped here to avoid a duplicate
    notice.  A direct ``orch run`` has no job row; the execution owner records
    this attempt itself so a detached run's failure is visible without an
    attached ``orch watch``.
    """
    from saberops.jobs.store import list_jobs_tolerant

    try:
        jobs, _ = list_jobs_tolerant(db, limit=100_000, quarantine=False)
    except (OSError, sqlite3.Error, ValueError):
        jobs = []
    for job in jobs:
        if job.current_run_id == run_id or run_id in job.run_ids:
            return "job-owned"
    label = "finished" if status.upper() == "SUCCEEDED" else "failed"
    body = run_id
    if summary:
        body = f"{run_id}\n{summary[:500]}"
    return notify_once(
        run_notification_ledger(db),
        f"run:{run_id}:{status.upper()}",
        f"SaberOps run {label}",
        body,
    )


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
