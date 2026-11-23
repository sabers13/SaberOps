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
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from saberops.jobs.models import (
    CORRUPT_JOB_ROW,
    TERMINAL_JOB_STATES,
    Job,
    JobKind,
    JobRequirements,
    JobState,
    Priority,
    ResumeFrom,
    SteeringNote,
    WaitingReason,
    coerce_job_kind,
)

#: Bounded note body; an owner note is a correction, not a second task.
MAX_STEERING_NOTE_CHARS = 4000

#: At most this many pending notes travel in one attempt prompt; any further
#: notes wait for the following attempt boundary instead of splitting a note.
MAX_NOTES_PER_DELIVERY = 5

#: Job-event kind marking the one automatic continue note (F14).  Its presence
#: is the durable proof that this job already spent its single nudge.
NUDGE_EVENT_KIND = "nudged"


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
                kind TEXT NOT NULL DEFAULT 'BUILD',
                deadline TEXT,
                not_before TEXT,
                depends_on_json TEXT NOT NULL DEFAULT '[]',
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
        _ensure_depends_on_column(conn)
        _ensure_kind_column(conn)
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
        # ADR-0016: durable steering notes, delivered at the next attempt
        # boundary (never typed into a running worker).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS job_steering_notes (
                note_id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL REFERENCES jobs(job_id),
                body TEXT NOT NULL,
                created_at TEXT NOT NULL,
                delivered_at TEXT,
                delivered_run_id TEXT,
                delivered_attempt_id TEXT,
                source TEXT NOT NULL DEFAULT 'owner'
            )
            """
        )
        _ensure_steering_source_column(conn)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_job_steering_notes_job "
            "ON job_steering_notes(job_id, note_id)"
        )
        conn.commit()


def _ensure_steering_source_column(conn: sqlite3.Connection) -> None:
    """Add ``source`` to a steering-notes table created before F26.

    Existing rows are owner notes, which is exactly the column default.  A
    note must say who wrote it without changing delivery semantics; advisor
    advice recorded before phase 1.1 keeps its ``advisor`` label.
    """
    columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(job_steering_notes)")}
    if "source" not in columns:
        conn.execute(
            "ALTER TABLE job_steering_notes ADD COLUMN source TEXT NOT NULL DEFAULT 'owner'"
        )


def _ensure_depends_on_column(conn: sqlite3.Connection) -> None:
    """Add ``depends_on_json`` to a ``jobs`` table created before F8.

    ``CREATE TABLE IF NOT EXISTS`` never alters an existing table, and stores
    created by an earlier version keep dispatching, so the F8 column is added
    in place with the same default a fresh store gets.  Jobs written before
    this column existed carry no dependencies.
    """
    columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(jobs)")}
    if "depends_on_json" not in columns:
        conn.execute(
            "ALTER TABLE jobs ADD COLUMN depends_on_json TEXT NOT NULL DEFAULT '[]'"
        )


def _ensure_kind_column(conn: sqlite3.Connection) -> None:
    """Add ``kind`` to a ``jobs`` table created before F19.

    ``CREATE TABLE IF NOT EXISTS`` never alters an existing table, so a store
    written by an earlier version gains the column in place.  Every existing
    row is an ordinary BUILD job, which is exactly the column default.
    """
    columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(jobs)")}
    if "kind" not in columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN kind TEXT NOT NULL DEFAULT 'BUILD'")


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
        "kind": job.kind.value,
        "deadline": job.deadline,
        "not_before": job.not_before,
        "depends_on_json": json.dumps(list(job.depends_on), separators=(",", ":")),
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
    # A read-only connection can read a store created before F8, whose jobs
    # table has no ``depends_on_json``; those jobs carry no dependencies.
    depends_raw = row["depends_on_json"] if "depends_on_json" in row.keys() else None
    depends_on = tuple(json.loads(str(depends_raw))) if depends_raw else ()
    kind_raw = row["kind"] if "kind" in row.keys() else None
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
        kind=coerce_job_kind(str(kind_raw)) if kind_raw else JobKind.BUILD,
        deadline=str(row["deadline"]) if row["deadline"] is not None else None,
        not_before=str(row["not_before"]) if row["not_before"] is not None else None,
        depends_on=depends_on,
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
    db: Any, job: Job, *, event_kind: str = "submitted", now: datetime | None = None,
    mcp_session_id: str | None = None,
) -> Job:
    """Insert a new job row plus its first event atomically.

    The dependency graph is validated here, at the single durable choke point,
    so no caller can persist a job whose ``depends_on`` closes a cycle.
    """
    from saberops.jobs.dependencies import validate_job_dependencies

    ensure_tables(db)
    validate_job_dependencies(db, job_id=job.job_id, depends_on=job.depends_on)
    at = _utcnow_iso(now)
    cols = _job_to_columns(job)
    names = ", ".join(cols)
    placeholders = ", ".join("?" for _ in cols)
    try:
        with _owner_conn(db) as conn:
            if mcp_session_id is not None:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS mcp_job_owners ("
                    "job_id TEXT PRIMARY KEY REFERENCES jobs(job_id), session_id TEXT NOT NULL)"
                )
            conn.execute(
                f"INSERT INTO jobs ({names}) VALUES ({placeholders})", tuple(cols.values())
            )
            if mcp_session_id is not None:
                conn.execute(
                    "INSERT INTO mcp_job_owners (job_id, session_id) VALUES (?, ?)",
                    (job.job_id, mcp_session_id),
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


def _prepare_job_read(db: Any) -> bool:
    """Prepare writable stores; never create tables through a read-only connection."""
    if not db.owner_db._read_only:
        ensure_tables(db)
        return True
    with _owner_conn(db) as conn:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'jobs'"
        ).fetchone() is not None


def get_job(db: Any, job_id: str) -> Job | None:
    if not _prepare_job_read(db):
        return None
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


def find_job_for_run(db: Any, run_id: str) -> Job | None:
    """Return the job whose run chain contains ``run_id``, if any (read-only)."""
    if not _prepare_job_read(db):
        return None
    with _owner_conn(db) as conn:
        rows = conn.execute("SELECT * FROM jobs").fetchall()
    for row in rows:
        job = _row_to_job(row)
        if run_id in job.run_ids:
            return job
    return None


def append_run_to_job(
    db: Any, job_id: str, run_id: str, *, now: datetime | None = None
) -> Job:
    """Append an already-created run to a job's chain without changing its state.

    Candidate refresh (E6) creates a new run for the same job.  Linking that run
    here keeps ``job_is_accepted`` and the rendered plan ledger following the
    refreshed candidate instead of the pre-refresh run.  Idempotent.
    """
    clean_run_id = run_id.strip() if isinstance(run_id, str) else ""
    if not clean_run_id:
        raise ValueError("run_id must be a non-empty string")
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
        existing = list(json.loads(str(row["run_ids_json"] or "[]")))
        if clean_run_id not in existing:
            existing.append(clean_run_id)
            conn.execute(
                "UPDATE jobs SET run_ids_json = ?, updated_at = ? WHERE job_id = ?",
                (json.dumps(existing, separators=(",", ":")), at, job_id),
            )
            conn.commit()
        updated = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    if updated is None:  # pragma: no cover - the row was read under the same lock
        raise JobNotFoundError(f"unknown job: {job_id}")
    return _row_to_job(updated)


def resume_transition(
    db: Any,
    job_id: str,
    run_id: str,
    *,
    source_run_id: str | None = None,
    from_: str = "scratch",
    note_id: int | None = None,
    now: datetime | None = None,
) -> tuple[Job, bool]:
    """FAILED -> RUNNING with one successor run (H7 resume).

    The owner or planner resumes a failed job from scratch or from its saved
    candidate; the successor is already created by the service layer, so this
    transition only links it and clears the terminal job state.  It is
    idempotent: a repeat that already points at ``run_id`` returns unchanged.
    Any other state refuses without writing.
    """
    clean_run_id = run_id.strip() if isinstance(run_id, str) else ""
    if not clean_run_id:
        raise ValueError("run_id must be a non-empty string")
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
        current_run = row["current_run_id"]
        if state == JobState.RUNNING.value and current_run == clean_run_id:
            return _row_to_job(row), False
        if state != JobState.FAILED.value:
            raise JobTransitionError(
                f"job {job_id}: state {state} does not allow this transition"
            )
        if source_run_id is not None and current_run != source_run_id:
            raise JobConflictError(
                f"job {job_id} moved to run {current_run!r} "
                f"(expected {source_run_id!r})"
            )
        existing = list(json.loads(str(row["run_ids_json"] or "[]")))
        if clean_run_id not in existing:
            existing.append(clean_run_id)
        cur = conn.execute(
            "UPDATE jobs SET state = ?, waiting_reason = NULL, failure_reason = NULL,"
            " current_run_id = ?, run_ids_json = ?, updated_at = ?"
            " WHERE job_id = ? AND state = ?",
            (
                JobState.RUNNING.value,
                clean_run_id,
                json.dumps(existing, separators=(",", ":")),
                at,
                job_id,
                JobState.FAILED.value,
            ),
        )
        if (cur.rowcount or 0) == 0:
            raise JobConflictError(f"job {job_id} moved concurrently (expected FAILED)")
        try:
            _append_event(
                conn,
                job_id=job_id,
                at=at,
                kind="resumed",
                from_state=JobState.FAILED.value,
                to_state=JobState.RUNNING.value,
                payload={
                    "run_id": clean_run_id,
                    "source_run_id": source_run_id,
                    "from": from_,
                    "note_id": note_id,
                },
            )
        except sqlite3.IntegrityError as exc:
            raise JobConflictError(f"event sequence conflict for {job_id}: {exc}") from exc
        conn.commit()
        updated = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        assert updated is not None
        return _row_to_job(updated), True


def list_jobs_raw(
    db: Any, *, state: JobState | None = None, limit: int = 50
) -> list[Job]:
    if not _prepare_job_read(db):
        return []
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


def list_jobs_tolerant(
    db: Any,
    *,
    state: JobState | None = None,
    limit: int = 50,
    quarantine: bool = True,
    now: datetime | None = None,
) -> tuple[list[Job], tuple[str, ...]]:
    """Read jobs while quarantining unreadable/corrupt rows instead of crashing."""
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

        valid: list[Job] = []
        quarantined: list[str] = []
        at = _utcnow_iso(now)
        for r in rows:
            try:
                valid.append(_row_to_job(r))
            except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
                raw_id = r["job_id"] if "job_id" in r.keys() else None
                job_id = str(raw_id) if raw_id is not None else "<unknown>"
                quarantined.append(job_id)
                print(
                    f"[!] {'Quarantining' if quarantine else 'Skipping'}"
                    f" corrupt job row {job_id}: {exc}",
                    file=sys.stderr,
                )
                if quarantine and raw_id is not None and not getattr(db, "_read_only", False):
                    conn.execute(
                        "CREATE TABLE IF NOT EXISTS jobs_quarantine ("
                        "job_id TEXT PRIMARY KEY, quarantined_at TEXT NOT NULL, "
                        "raw_json TEXT NOT NULL, error TEXT NOT NULL)"
                    )
                    conn.execute(
                        "INSERT OR REPLACE INTO jobs_quarantine VALUES (?, ?, ?, ?)",
                        (job_id, at, json.dumps(dict(r), default=str), str(exc)),
                    )
                    conn.execute(
                        "UPDATE jobs SET state = ?, failure_reason = ?, updated_at = ?, "
                        "waiting_reason = NULL, requirements_json = '{}', priority = 'NORMAL', "
                        "run_ids_json = '[]', resume_from_json = NULL, preemptions = 0 "
                        "WHERE job_id = ?",
                        (JobState.FAILED.value, CORRUPT_JOB_ROW, at, job_id),
                    )
                    _append_event(
                        conn,
                        job_id=job_id,
                        at=at,
                        kind="QUARANTINED",
                        from_state=str(r["state"]) if "state" in r.keys() else None,
                        to_state=JobState.FAILED.value,
                        payload={"reason": CORRUPT_JOB_ROW, "error": str(exc)},
                    )
        if quarantined and quarantine and not getattr(db, "_read_only", False):
            conn.commit()
        return valid, tuple(quarantined)


def append_event(
    db: Any,
    job_id: str,
    kind: str,
    payload: dict[str, Any] | None = None,
    *,
    now: datetime | None = None,
) -> None:
    """Append one durable job event (used to index a scout report, F19)."""
    ensure_tables(db)
    at = _utcnow_iso(now)
    with _owner_conn(db) as conn:
        _append_event(
            conn,
            job_id=job_id,
            at=at,
            kind=kind,
            from_state=None,
            to_state=None,
            payload=payload,
        )
        conn.commit()


def list_events(db: Any, job_id: str) -> list[dict[str, Any]]:
    if not _prepare_job_read(db):
        return []
    with _owner_conn(db) as conn:
        rows = conn.execute(
            "SELECT job_id, seq, at, kind, from_state, to_state, payload_json"
            " FROM job_events WHERE job_id = ? ORDER BY seq ASC",
            (job_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def _row_to_steering_note(row: sqlite3.Row) -> SteeringNote:
    return SteeringNote(
        note_id=int(row["note_id"]),
        job_id=str(row["job_id"]),
        body=str(row["body"]),
        created_at=str(row["created_at"]),
        delivered_at=str(row["delivered_at"]) if row["delivered_at"] is not None else None,
        delivered_run_id=(
            str(row["delivered_run_id"]) if row["delivered_run_id"] is not None else None
        ),
        delivered_attempt_id=(
            str(row["delivered_attempt_id"])
            if row["delivered_attempt_id"] is not None
            else None
        ),
        source=(
            str(row["source"])
            if "source" in row.keys() and row["source"] is not None
            else "owner"
        ),
    )


def insert_steering_note(
    db: Any, job_id: str, body: str, *, now: datetime | None = None, source: str = "owner"
) -> SteeringNote:
    """Record one durable note plus its job event in a single transaction."""
    ensure_tables(db)
    at = _utcnow_iso(now)
    with _owner_conn(db) as conn:
        cursor = conn.execute(
            "INSERT INTO job_steering_notes (job_id, body, created_at, source)"
            " VALUES (?, ?, ?, ?)",
            (job_id, body, at, source),
        )
        note_id = int(cursor.lastrowid or 0)
        _append_event(
            conn,
            job_id=job_id,
            at=at,
            kind="steering_note",
            from_state=None,
            to_state=None,
            payload={"note_id": note_id, "length": len(body), "source": source},
        )
        row = conn.execute(
            "SELECT * FROM job_steering_notes WHERE note_id = ?", (note_id,)
        ).fetchone()
        conn.commit()
    if row is None:  # pragma: no cover - the row was just written
        raise RuntimeError(f"steering note {note_id} disappeared after insert")
    return _row_to_steering_note(row)


def record_nudge_note(
    db: Any,
    job_id: str,
    *,
    run_id: str,
    attempt_id: str | None = None,
    body: str,
    reason: str = "",
    now: datetime | None = None,
) -> SteeringNote | None:
    """Record the one automatic continue note for a job, atomically.

    Returns ``None`` when this job already spent its nudge: the check and the
    insert share one transaction, so two callers can never spend it twice.  The
    note itself is an ordinary steering note (ADR-0016) and is delivered by the
    same attempt-boundary machinery as an owner note.
    """
    ensure_tables(db)
    if not body or len(body) > MAX_STEERING_NOTE_CHARS:
        raise ValueError("automatic nudge body is empty or over the note limit")
    at = _utcnow_iso(now)
    with _owner_conn(db) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            pass
        spent = conn.execute(
            "SELECT 1 FROM job_events WHERE job_id = ? AND kind = ? LIMIT 1",
            (job_id, NUDGE_EVENT_KIND),
        ).fetchone()
        if spent is not None:
            return None
        cursor = conn.execute(
            "INSERT INTO job_steering_notes (job_id, body, created_at) VALUES (?, ?, ?)",
            (job_id, body, at),
        )
        note_id = int(cursor.lastrowid or 0)
        _append_event(
            conn,
            job_id=job_id,
            at=at,
            kind=NUDGE_EVENT_KIND,
            from_state=None,
            to_state=None,
            payload={
                "note_id": note_id,
                "run_id": run_id,
                "attempt_id": attempt_id,
                "length": len(body),
                "reason": reason,
            },
        )
        row = conn.execute(
            "SELECT * FROM job_steering_notes WHERE note_id = ?", (note_id,)
        ).fetchone()
        conn.commit()
    if row is None:  # pragma: no cover - the row was just written
        raise RuntimeError(f"automatic nudge note {note_id} disappeared after insert")
    return _row_to_steering_note(row)


def list_steering_notes(db: Any, job_id: str) -> list[SteeringNote]:
    """Every note for a job, oldest first; empty when the store predates notes."""
    with _owner_conn(db) as conn:
        present = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            ("job_steering_notes",),
        ).fetchone()
        if present is None:
            return []
        rows = conn.execute(
            "SELECT * FROM job_steering_notes WHERE job_id = ? ORDER BY note_id ASC",
            (job_id,),
        ).fetchall()
        return [_row_to_steering_note(r) for r in rows]


def pending_steering_notes_for_run(
    db: Any, run_id: str, *, limit: int = MAX_NOTES_PER_DELIVERY
) -> list[SteeringNote]:
    """Undelivered notes for the job currently dispatched as ``run_id``.

    Read-only and allocation-free: the dispatch path calls this before every
    attempt, so it never creates the notes table or writes when no note exists.
    """
    with _owner_conn(db) as conn:
        present = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            ("job_steering_notes",),
        ).fetchone()
        if present is None:
            return []
        rows = conn.execute(
            "SELECT n.* FROM job_steering_notes AS n"
            " JOIN jobs AS j ON j.job_id = n.job_id"
            " WHERE n.delivered_at IS NULL AND j.current_run_id = ?"
            " ORDER BY n.note_id ASC LIMIT ?",
            (run_id, max(1, int(limit))),
        ).fetchall()
        return [_row_to_steering_note(r) for r in rows]


def mark_steering_notes_delivered(
    db: Any,
    note_ids: tuple[int, ...] | list[int],
    *,
    run_id: str,
    attempt_id: str,
    now: datetime | None = None,
) -> list[SteeringNote]:
    """Record that these notes travelled in one attempt's prompt.

    The ``delivered_at IS NULL`` guard keeps a note single-use even if two
    launchers raced; only rows won by this attempt are returned.
    """
    ids = tuple(int(note_id) for note_id in note_ids)
    if not ids:
        return []
    ensure_tables(db)
    at = _utcnow_iso(now)
    placeholders = ", ".join("?" for _ in ids)
    with _owner_conn(db) as conn:
        conn.execute(
            "UPDATE job_steering_notes SET delivered_at = ?, delivered_run_id = ?,"
            " delivered_attempt_id = ?"
            f" WHERE delivered_at IS NULL AND note_id IN ({placeholders})",
            (at, run_id, attempt_id, *ids),
        )
        rows = conn.execute(
            "SELECT * FROM job_steering_notes"
            " WHERE delivered_run_id = ? AND delivered_attempt_id = ?"
            " ORDER BY note_id ASC",
            (run_id, attempt_id),
        ).fetchall()
        conn.commit()
    wanted = set(ids)
    return [
        note
        for note in (_row_to_steering_note(r) for r in rows)
        if note.note_id in wanted
    ]


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
    db: Any,
    job_id: str,
    run_id: str,
    *,
    dispatch_context: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> Job:
    """QUEUED -> RUNNING: attach ``run_id``, clear the waiting reason.

    ``dispatch_context`` is the dispatching scheduler's durable identity (lease
    holder, fencing token, lease expiry and the store the run row will be
    written to).  It is stored on the ``dispatched`` event beside ``run_id`` so
    a later tick can prove the dispatch was abandoned between this
    compare-and-swap and the run row, instead of guessing.
    """
    if not run_id or not run_id.strip():
        raise ValueError("run_id must be a non-empty string")
    clean_run_id = run_id.strip()
    ensure_tables(db)
    at = _utcnow_iso(now)
    payload: dict[str, Any] = {"run_id": clean_run_id}
    if dispatch_context is not None:
        payload["dispatch"] = dict(dispatch_context)
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
                payload=payload,
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
    details: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> Job:
    """QUEUED -> QUEUED: reason change only (M2-4b pacing).

    ``details`` is durable audit evidence for the reason (F8 records which
    dependencies block a job); it never changes the transition itself.
    """
    want = reason if isinstance(reason, WaitingReason) else WaitingReason(str(reason))
    current = get_job(db, job_id)
    if current is None:
        raise JobNotFoundError(f"unknown job: {job_id}")
    _require_state(current, JobState.QUEUED)
    payload: dict[str, Any] = {
        "waiting_reason": want.value,
        "eta": eta,
        "eta_confidence": eta_confidence,
    }
    if details:
        payload["details"] = dict(details)
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
        payload=payload,
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


def requeue_exact(
    db: Any,
    job_id: str,
    reason: WaitingReason | str,
    *,
    expected_run_id: str,
    expected_preemptions: int,
    resume_from: ResumeFrom | None = None,
    not_before: str | None = None,
    now: datetime | None = None,
) -> Job:
    """RUNNING -> QUEUED fenced to the *exact* generation being parked.

    :func:`requeue` CAS-guards only ``state = 'RUNNING'``.  That is sufficient
    for ordinary dispatch parking, but a crash-recovery replay that resumes
    after an arbitrary delay can observe a job that has already been requeued
    *and re-dispatched* onto a newer run (an A-B-A on the row).  Requeuing then
    would count a second preemption against the wrong generation and clear the
    newer run's ``current_run_id``.

    This variant additionally requires ``current_run_id == expected_run_id``
    **and** ``preemptions == expected_preemptions`` inside the same
    ``BEGIN IMMEDIATE`` transaction, so a stale recovery tail can only ever
    complete the exact generation it inspected.  A mismatch raises
    :class:`JobConflictError` and writes nothing.
    """
    want = reason if isinstance(reason, WaitingReason) else WaitingReason(str(reason))
    if not expected_run_id:
        raise ValueError("expected_run_id must be a non-empty string")
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
        current_run = row["current_run_id"]
        current_preemptions = int(row["preemptions"] or 0)
        if current_run != expected_run_id or current_preemptions != int(expected_preemptions):
            raise JobConflictError(
                f"job {job_id} moved to generation run={current_run!r} "
                f"preemptions={current_preemptions} (expected run={expected_run_id!r} "
                f"preemptions={expected_preemptions})"
            )
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
            " WHERE job_id = ? AND state = ? AND current_run_id = ? AND preemptions = ?",
            (
                JobState.QUEUED.value,
                want.value,
                resume_json,
                nb,
                preemptions,
                at,
                job_id,
                JobState.RUNNING.value,
                expected_run_id,
                int(expected_preemptions),
            ),
        )
        if (cur.rowcount or 0) == 0:
            raise JobConflictError(
                f"job {job_id} moved concurrently (expected RUNNING/{expected_run_id})"
            )
        try:
            _append_event(
                conn,
                job_id=job_id,
                at=at,
                kind="requeued",
                from_state=JobState.RUNNING.value,
                to_state=JobState.QUEUED.value,
                payload={
                    "waiting_reason": want.value,
                    "preemptions": preemptions,
                    "preempted_run_id": expected_run_id,
                },
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
    "requeue_exact",
    "resume_transition",
    "set_waiting",
]
