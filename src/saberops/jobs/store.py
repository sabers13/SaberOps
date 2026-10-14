"""Job store (M2-3 sections C + D): owner DB, own DDL, CAS state machine.

Storage lives in the owner DB because pools are global across repos.
Every function opens connections through ``db.owner_db._get_connection()``
(the connection helper from db.py: same pragmas, read-only mode
honoured) and runs ``ensure_tables`` first. ``db.py`` itself is unchanged
and ``DB_SCHEMA_VERSION`` is untouched (C4).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from saberops.jobs.models import (
    TERMINAL_JOB_STATES,
    Job,
    JobRequirements,
    JobState,
    Priority,
    ResumeFrom,
    WaitingReason,
)


class JobNotFoundError(LookupError):
    """Unknown job id."""


class JobConflictError(RuntimeError):
    """Compare-and-set lost: someone else moved the row (single dispatch)."""


class JobTransitionError(ValueError):
    """Transition not in the section D table (raised before touching the DB)."""


class IdempotencyConflictError(RuntimeError):
    """Same (caller_id, idempotency_key) with a different request digest."""


def _utcnow_iso(now: datetime | None) -> str:
    if now is None:
        return datetime.now(UTC).isoformat()
    aware = now if now.tzinfo else now.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat()


@contextmanager
def _owner_conn(db: Any) -> Iterator[sqlite3.Connection]:
    owner = db.owner_db
    with owner._get_connection() as conn:
        yield conn


def ensure_tables(db: Any) -> None:
    """Create ``jobs`` and ``job_events`` idempotently in the owner DB."""
    with _owner_conn(db) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                caller_id TEXT NOT NULL,
                idempotency_key TEXT,
                repo TEXT NOT NULL,
                base_ref TEXT NOT NULL,
                base_sha TEXT NOT NULL,
                goal TEXT NOT NULL,
                acceptance TEXT,
                requirements_json TEXT NOT NULL,
                priority TEXT NOT NULL,
                deadline TEXT,
                not_before TEXT,
                state TEXT NOT NULL,
                waiting_reason TEXT,
                failure_reason TEXT,
                eta TEXT,
                eta_confidence TEXT,
                deadline_at_risk INTEGER NOT NULL DEFAULT 0,
                current_run_id TEXT,
                run_ids_json TEXT NOT NULL DEFAULT '[]',
                resume_from_json TEXT,
                preemptions INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                request_digest TEXT NOT NULL,
                UNIQUE (caller_id, idempotency_key)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS job_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL REFERENCES jobs(job_id),
                seq INTEGER NOT NULL,
                at TEXT NOT NULL,
                kind TEXT NOT NULL,
                from_state TEXT,
                to_state TEXT,
                payload_json TEXT,
                UNIQUE (job_id, seq)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_caller_key "
            "ON jobs(caller_id, idempotency_key)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_job_events_job ON job_events(job_id, seq)"
        )
        conn.commit()


def _job_to_columns(job: Job) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "caller_id": job.caller_id,
        "idempotency_key": job.idempotency_key,
        "repo": job.repo,
        "base_ref": job.base_ref,
        "base_sha": job.base_sha,
        "goal": job.goal,
        "acceptance": job.acceptance,
        "requirements_json": json.dumps(
            job.requirements.to_dict(), sort_keys=True, separators=(",", ":")
        ),
        "priority": job.priority.value,
        "deadline": job.deadline,
        "not_before": job.not_before,
        "state": job.state.value,
        "waiting_reason": job.waiting_reason.value if job.waiting_reason else None,
        "failure_reason": job.failure_reason,
        "eta": job.eta,
        "eta_confidence": job.eta_confidence,
        "deadline_at_risk": 1 if job.deadline_at_risk else 0,
        "current_run_id": job.current_run_id,
        "run_ids_json": json.dumps(list(job.run_ids), separators=(",", ":")),
        "resume_from_json": json.dumps(job.resume_from.to_dict(), sort_keys=True)
        if job.resume_from
        else None,
        "preemptions": job.preemptions,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "request_digest": job.request_digest,
    }


def _row_to_job(row: sqlite3.Row) -> Job:
    requirements = JobRequirements.from_dict(json.loads(str(row["requirements_json"])))
    resume_raw = row["resume_from_json"]
    resume = ResumeFrom.from_dict(json.loads(str(resume_raw))) if resume_raw else None
    waiting_raw = row["waiting_reason"]
    return Job(
        job_id=str(row["job_id"]),
        idempotency_key=str(row["idempotency_key"])
        if row["idempotency_key"] is not None
        else None,
        caller_id=str(row["caller_id"]),
        repo=str(row["repo"]),
        base_ref=str(row["base_ref"]),
        base_sha=str(row["base_sha"]),
        goal=str(row["goal"]),
        acceptance=str(row["acceptance"]) if row["acceptance"] is not None else None,
        requirements=requirements,
        priority=Priority(str(row["priority"])),
        deadline=str(row["deadline"]) if row["deadline"] is not None else None,
        not_before=str(row["not_before"]) if row["not_before"] is not None else None,
        state=JobState(str(row["state"])),
        waiting_reason=WaitingReason(str(waiting_raw)) if waiting_raw else None,
        failure_reason=str(row["failure_reason"])
        if row["failure_reason"] is not None
        else None,
        eta=str(row["eta"]) if row["eta"] is not None else None,
        eta_confidence=str(row["eta_confidence"])
        if row["eta_confidence"] is not None
        else None,
        deadline_at_risk=bool(row["deadline_at_risk"]),
        current_run_id=str(row["current_run_id"])
        if row["current_run_id"] is not None
        else None,
        run_ids=tuple(json.loads(str(row["run_ids_json"] or "[]"))),
        resume_from=resume,
        preemptions=int(row["preemptions"] or 0),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        request_digest=str(row["request_digest"]),
    )


def _next_seq(conn: sqlite3.Connection, job_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) AS m FROM job_events WHERE job_id = ?", (job_id,)
    ).fetchone()
    return int(row["m"] or 0) + 1


def _append_event(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    at: str,
    kind: str,
    from_state: str | None,
    to_state: str | None,
    payload: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        "INSERT INTO job_events (job_id, seq, at, kind, from_state, to_state, payload_json)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            job_id,
            _next_seq(conn, job_id),
            at,
            kind,
            from_state,
            to_state,
            json.dumps(payload, sort_keys=True) if payload is not None else None,
        ),
    )


def insert_job(
    db: Any, job: Job, *, event_kind: str = "submitted", now: datetime | None = None
) -> Job:
    """Insert a new job row plus its first event atomically."""
    ensure_tables(db)
    at = _utcnow_iso(now)
    cols = _job_to_columns(job)
    names = ", ".join(cols)
    placeholders = ", ".join("?" for _ in cols)
    try:
        with _owner_conn(db) as conn:
            conn.execute(
                f"INSERT INTO jobs ({names}) VALUES ({placeholders})", tuple(cols.values())
            )
            _append_event(
                conn,
                job_id=job.job_id,
                at=at,
                kind=event_kind,
                from_state=None,
                to_state=job.state.value,
                payload={"waiting_reason": job.waiting_reason.value}
                if job.waiting_reason
                else (
                    {"failure_reason": job.failure_reason} if job.failure_reason else None
                ),
            )
            conn.commit()
    except sqlite3.IntegrityError as exc:
        raise JobConflictError(f"job insert conflict for {job.job_id}: {exc}") from exc
    return job


def get_job(db: Any, job_id: str) -> Job | None:
    ensure_tables(db)
    with _owner_conn(db) as conn:
        row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return _row_to_job(row) if row is not None else None


def find_by_idempotency(db: Any, caller_id: str, idempotency_key: str) -> Job | None:
    ensure_tables(db)
    with _owner_conn(db) as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE caller_id = ? AND idempotency_key = ?",
            (caller_id, idempotency_key),
        ).fetchone()
        return _row_to_job(row) if row is not None else None


def list_jobs_raw(
    db: Any, *, state: JobState | None = None, limit: int = 50
) -> list[Job]:
    ensure_tables(db)
    with _owner_conn(db) as conn:
        if state is None:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY"
                " CASE priority WHEN 'URGENT' THEN 0 WHEN 'NORMAL' THEN 1"
                " WHEN 'BACKGROUND' THEN 2 ELSE 3 END,"
                " created_at ASC, job_id ASC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE state = ? ORDER BY"
                " CASE priority WHEN 'URGENT' THEN 0 WHEN 'NORMAL' THEN 1"
                " WHEN 'BACKGROUND' THEN 2 ELSE 3 END,"
                " created_at ASC, job_id ASC LIMIT ?",
                (state.value, limit),
            ).fetchall()
        return [_row_to_job(r) for r in rows]


def list_events(db: Any, job_id: str) -> list[dict[str, Any]]:
    ensure_tables(db)
    with _owner_conn(db) as conn:
        rows = conn.execute(
            "SELECT job_id, seq, at, kind, from_state, to_state, payload_json"
            " FROM job_events WHERE job_id = ? ORDER BY seq ASC",
            (job_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def _cas(
    db: Any,
    job_id: str,
    *,
    expected: JobState,
    updates: dict[str, Any],
    kind: str,
    to_state: JobState,
    payload: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> Job:
    ensure_tables(db)
    at = _utcnow_iso(now)
    assignments = ", ".join(f"{name} = ?" for name in updates) + ", updated_at = ?"
    values: list[Any] = list(updates.values()) + [at, job_id, expected.value]
    with _owner_conn(db) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            pass
        cur = conn.execute(
            f"UPDATE jobs SET {assignments} WHERE job_id = ? AND state = ?", values
        )
        if (cur.rowcount or 0) == 0:
            existing = conn.execute(
                "SELECT state FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if existing is None:
                raise JobNotFoundError(f"unknown job: {job_id}")
            raise JobConflictError(
                f"job {job_id} moved concurrently (expected {expected.value})"
            )
        try:
            _append_event(
                conn,
                job_id=job_id,
                at=at,
                kind=kind,
                from_state=expected.value,
                to_state=to_state.value,
                payload=payload,
            )
        except sqlite3.IntegrityError as exc:
            raise JobConflictError(f"event sequence conflict for {job_id}: {exc}") from exc
        conn.commit()
        row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        assert row is not None
        return _row_to_job(row)


def _require_state(job: Job, *allowed: JobState) -> None:
    if job.state not in allowed:
        raise JobTransitionError(
            f"job {job.job_id}: state {job.state.value} does not allow this transition"
        )


def mark_dispatched(
    db: Any, job_id: str, run_id: str, *, now: datetime | None = None
) -> Job:
    """QUEUED -> RUNNING: attach ``run_id``, clear the waiting reason."""
    if not run_id or not run_id.strip():
        raise ValueError("run_id must be a non-empty string")
    clean_run_id = run_id.strip()
    ensure_tables(db)
    at = _utcnow_iso(now)
    with _owner_conn(db) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            pass
        row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            raise JobNotFoundError(f"unknown job: {job_id}")
        state = str(row["state"])
        if state in (s.value for s in TERMINAL_JOB_STATES) or state != JobState.QUEUED.value:
            raise JobTransitionError(
                f"job {job_id}: cannot dispatch from {state}"
            )
        existing_run_ids = list(json.loads(str(row["run_ids_json"] or "[]")))
        run_ids = [*existing_run_ids, clean_run_id]
        cur = conn.execute(
            "UPDATE jobs SET state = ?, waiting_reason = NULL, current_run_id = ?,"
            " run_ids_json = ?, updated_at = ? WHERE job_id = ? AND state = ?",
            (
                JobState.RUNNING.value,
                clean_run_id,
                json.dumps(run_ids, separators=(",", ":")),
                at,
                job_id,
                JobState.QUEUED.value,
            ),
        )
        if (cur.rowcount or 0) == 0:
            raise JobConflictError(f"job {job_id} moved concurrently (expected QUEUED)")
        try:
            _append_event(
                conn,
                job_id=job_id,
                at=at,
                kind="dispatched",
                from_state=JobState.QUEUED.value,
                to_state=JobState.RUNNING.value,
                payload={"run_id": clean_run_id},
            )
        except sqlite3.IntegrityError as exc:
            raise JobConflictError(f"event sequence conflict for {job_id}: {exc}") from exc
        conn.commit()
        updated_row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        assert updated_row is not None
        return _row_to_job(updated_row)


def set_waiting(
    db: Any,
    job_id: str,
    reason: WaitingReason | str,
    *,
    eta: str | None = None,
    eta_confidence: str | None = None,
    now: datetime | None = None,
) -> Job:
    """QUEUED -> QUEUED: reason change only (M2-4b pacing)."""
    want = reason if isinstance(reason, WaitingReason) else WaitingReason(str(reason))
    current = get_job(db, job_id)
    if current is None:
        raise JobNotFoundError(f"unknown job: {job_id}")
    _require_state(current, JobState.QUEUED)
    return _cas(
        db,
        job_id,
        expected=JobState.QUEUED,
        updates={
            "state": JobState.QUEUED.value,
            "waiting_reason": want.value,
            "eta": eta,
            "eta_confidence": eta_confidence,
        },
        kind="waiting",
        to_state=JobState.QUEUED,
        payload={"waiting_reason": want.value, "eta": eta, "eta_confidence": eta_confidence},
        now=now,
    )


def requeue(
    db: Any,
    job_id: str,
    reason: WaitingReason | str,
    *,
    resume_from: ResumeFrom | None = None,
    not_before: str | None = None,
    now: datetime | None = None,
) -> Job:
    """RUNNING -> QUEUED: park work; PREEMPTED bumps ``preemptions`` (M2-5)."""
    want = reason if isinstance(reason, WaitingReason) else WaitingReason(str(reason))
    ensure_tables(db)
    at = _utcnow_iso(now)
    with _owner_conn(db) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            pass
        row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            raise JobNotFoundError(f"unknown job: {job_id}")
        state = str(row["state"])
        if state != JobState.RUNNING.value:
            raise JobTransitionError(
                f"job {job_id}: state {state} does not allow this transition"
            )
        current_preemptions = int(row["preemptions"] or 0)
        preemptions = current_preemptions + (1 if want == WaitingReason.PREEMPTED else 0)
        nb = (
            not_before
            if not_before is not None
            else (str(row["not_before"]) if row["not_before"] is not None else None)
        )
        resume_json = (
            json.dumps(resume_from.to_dict(), sort_keys=True) if resume_from is not None else None
        )
        cur = conn.execute(
            "UPDATE jobs SET state = ?, waiting_reason = ?, current_run_id = NULL,"
            " resume_from_json = ?, not_before = ?, preemptions = ?, updated_at = ?"
            " WHERE job_id = ? AND state = ?",
            (
                JobState.QUEUED.value,
                want.value,
                resume_json,
                nb,
                preemptions,
                at,
                job_id,
                JobState.RUNNING.value,
            ),
        )
        if (cur.rowcount or 0) == 0:
            raise JobConflictError(f"job {job_id} moved concurrently (expected RUNNING)")
        try:
            _append_event(
                conn,
                job_id=job_id,
                at=at,
                kind="requeued",
                from_state=JobState.RUNNING.value,
                to_state=JobState.QUEUED.value,
                payload={"waiting_reason": want.value, "preemptions": preemptions},
            )
        except sqlite3.IntegrityError as exc:
            raise JobConflictError(f"event sequence conflict for {job_id}: {exc}") from exc
        conn.commit()
        updated_row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        assert updated_row is not None
        return _row_to_job(updated_row)


def cancel_transition(db: Any, job_id: str, *, now: datetime | None = None) -> Job:
    """QUEUED/RUNNING -> CANCELLED. Terminal or unknown handled by caller."""
    current = get_job(db, job_id)
    if current is None:
        raise JobNotFoundError(f"unknown job: {job_id}")
    _require_state(current, JobState.QUEUED, JobState.RUNNING)
    return _cas(
        db,
        job_id,
        expected=current.state,
        updates={"state": JobState.CANCELLED.value, "waiting_reason": None},
        kind="cancelled",
        to_state=JobState.CANCELLED,
        payload={"from_state": current.state.value},
        now=now,
    )


def reconcile_transition(
    db: Any,
    job_id: str,
    *,
    to: JobState,
    failure_reason: str | None = None,
    now: datetime | None = None,
) -> Job:
    """RUNNING -> SUCCEEDED/FAILED from the run's terminal status (section G)."""
    if to not in (JobState.SUCCEEDED, JobState.FAILED):
        raise JobTransitionError(f"job {job_id}: reconcile target must be terminal run outcome")
    current = get_job(db, job_id)
    if current is None:
        raise JobNotFoundError(f"unknown job: {job_id}")
    _require_state(current, JobState.RUNNING)
    return _cas(
        db,
        job_id,
        expected=JobState.RUNNING,
        updates={
            "state": to.value,
            "waiting_reason": None,
            "failure_reason": failure_reason,
        },
        kind="reconciled",
        to_state=to,
        payload={"failure_reason": failure_reason},
        now=now,
    )


__all__ = [
    "IdempotencyConflictError",
    "JobConflictError",
    "JobNotFoundError",
    "JobTransitionError",
    "cancel_transition",
    "ensure_tables",
    "find_by_idempotency",
    "get_job",
    "insert_job",
    "list_events",
    "list_jobs_raw",
    "mark_dispatched",
    "reconcile_transition",
    "requeue",
    "set_waiting",
]
