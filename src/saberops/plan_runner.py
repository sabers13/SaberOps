"""Plan-lane submission and the scheduler reconciler (Phase 1 lane P2).

What this module is
-------------------
A plan file is authored intent: ``PLAN.md`` holds waves and lanes, each lane a
small brief.  This module turns the *ready* lanes of an activated plan into
ordinary jobs, one ``submit_job`` per lane, and nothing else.

* Activation (``submit_plan_file``) validates the file, reuses
  :func:`saberops.plan_file.activate_plan_file` to store an immutable revision,
  records the plan in the owner database, and asks the scheduler to run.
* The reconciler (:func:`reconcile_plan_lanes`) is called from
  ``scheduler_tick`` before it reads the queue, so a lane submitted here can be
  dispatched in the same tick.  Eligibility comes from the pure
  :func:`saberops.ready_frontier.project_ready_frontier`; a dependency is
  satisfied only by an accepted candidate (``run_accepted``), never merely by
  a finished run.
* The plan layer chooses *what may start*.  Routing, quota, the memory budget,
  worktrees, retries and acceptance stay exactly where they are: the scheduler
  and the owner.

Two owner decisions shape this lane:

1. **Owner-held lanes are never dispatched.**  ``run: "owner"`` work is
   completed only by ``orch plan-file done``.
2. **Closing the planning CLI pauses the work (F17).**  Every lane submission
   goes through :func:`submit_lane_job`, which consults
   :data:`LANE_SUBMIT_GATE` (the seam lane F17 can install) and, for the
   scheduler reconciler, refuses to submit when no live planning session holds
   the MCP planning lease.  The scheduler lease alone is not a planning
   session.  When F17 lands it will extend that pause to every other kind of
   work; this lane only guarantees plan lanes.

No model, provider or network call is made here.  The only side effects are
owner-database rows, the immutable plan revision, and ordinary jobs.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from saberops.jobs import store
from saberops.jobs.dependencies import job_is_accepted
from saberops.jobs.models import (
    Effort,
    Job,
    JobRequirements,
    JobRole,
    JobState,
    Priority,
)
from saberops.jobs.service import submit_job
from saberops.mcp.lease import OrchestratorLease, pid_is_alive
from saberops.plan_file import (
    ParsedPlan,
    PlanFileError,
    activate_plan_file,
    parse_plan_file,
    validate_plan,
)
from saberops.project import ProjectRegistry, canonical_repo_root, compute_project_identity
from saberops.project_scope.plan_store import (
    PlanNotFoundError,
    PlanRevision,
    PlanStore,
    PlanStoreError,
    PlanTamperError,
    PlanTaskDef,
    PlanValidationError,
)
from saberops.ready_frontier import (
    FrontierLimits,
    FrontierRequest,
    FrontierSnapshot,
    PlanTask,
    ProjectionStatus,
    ProjectPlan,
    SnapshotSource,
    TaskKind,
    TaskRef,
    TaskStatus,
    default_frontier_limits,
    project_ready_frontier,
)

__all__ = [
    "LANE_SUBMIT_GATE",
    "LaneSubmitResult",
    "LaneView",
    "PlanProjection",
    "PlanReconcileResult",
    "PlanRecord",
    "PlanRunnerError",
    "PlanSubmission",
    "ensure_plan_tables",
    "lane_digest",
    "list_active_plans",
    "mark_owner_lane_done",
    "planning_session_active",
    "project_plan",
    "reconcile_plan_lanes",
    "resolve_plan_path",
    "resolve_project_id",
    "set_lane_submit_gate",
    "submit_lane_job",
    "submit_plan_file",
]

#: Upper bound on an authored plan file read over MCP or the CLI.
MAX_PLAN_FILE_BYTES = 256 * 1024

#: At most this many plans are reconciled per scheduler tick (report §4.1).
MAX_ACTIVE_PLANS_PER_TICK = 3

#: F17 seam: a callable returning a pause reason, or ``None`` to allow work.
#: It receives the owner database.  A raising gate fails closed.
LANE_SUBMIT_GATE: Callable[[Any], str | None] | None = None

#: Reason recorded when the reconciler runs without a planning session.
NO_PLANNING_SESSION = "no_planning_session"
PLANNING_GATE_ERROR = "planning_gate_error"

_PRIORITIES = {item.value for item in Priority}
_EFFORTS = {item.value for item in Effort}
_SIZES = {"S", "M", "L"}
_PROJECT_ID_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")


class PlanRunnerError(ValueError):
    """Refusal or failure while activating or reconciling a plan."""


def _utcnow(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(UTC)
    return now.astimezone(UTC) if now.tzinfo else now.replace(tzinfo=UTC)


def _iso(now: datetime | None) -> str:
    return _utcnow(now).isoformat()


@contextmanager
def _owner_conn(db: Any) -> Iterator[sqlite3.Connection]:
    owner = getattr(db, "owner_db", db)
    with owner._get_connection() as conn:
        yield conn


# ---------------------------------------------------------------------------
# Owner-database schema (plans, plan_lanes, plan_events)
# ---------------------------------------------------------------------------


def ensure_plan_tables(db: Any) -> None:
    """Create the plan tables idempotently in the owner database.

    The scheduler calls this every tick, so an already-initialised store takes
    a single read-only catalog check instead of re-running DDL (which would add
    a write transaction to every 2 s tick).
    """
    with _owner_conn(db) as conn:
        present = {
            str(row["name"])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')"
                " AND name IN ('plans', 'plan_lanes', 'plan_events',"
                " 'idx_plans_active_project')"
            )
        }
        if present == {
            "plans",
            "plan_lanes",
            "plan_events",
            "idx_plans_active_project",
        }:
            return
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS plans (
                plan_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                repo TEXT NOT NULL,
                base_ref TEXT NOT NULL DEFAULT 'HEAD',
                active_version TEXT NOT NULL,
                file_sha256 TEXT NOT NULL,
                store_root TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                auto_advance INTEGER NOT NULL DEFAULT 1,
                auto_refresh INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS plan_lanes (
                plan_id TEXT NOT NULL,
                lane_id TEXT NOT NULL,
                lane_digest TEXT NOT NULL,
                job_id TEXT,
                run_ids_json TEXT NOT NULL DEFAULT '[]',
                superseded_by TEXT,
                owner_done_at TEXT,
                owner_evidence TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (plan_id, lane_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS plan_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id TEXT NOT NULL,
                lane_id TEXT,
                kind TEXT NOT NULL,
                event_key TEXT NOT NULL UNIQUE,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        # One ACTIVE plan per project; completed plans stay as history.
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_plans_active_project "
            "ON plans(project_id) WHERE status = 'ACTIVE'"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_plan_lanes_plan ON plan_lanes(plan_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_plan_events_plan ON plan_events(plan_id, id)"
        )
        conn.commit()


def _event(
    db: Any,
    *,
    plan_id: str,
    kind: str,
    event_key: str,
    payload: dict[str, Any],
    lane_id: str | None = None,
    now: datetime | None = None,
) -> None:
    """Record one idempotent plan event (same key + payload is a replay)."""
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    with _owner_conn(db) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO plan_events"
            " (plan_id, lane_id, kind, event_key, payload_json, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (plan_id, lane_id, kind, event_key, body, _iso(now)),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Records and views
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanRecord:
    """One row of the ``plans`` owner table."""

    plan_id: str
    project_id: str
    repo: str
    base_ref: str
    active_version: str
    file_sha256: str
    store_root: str
    status: str
    auto_advance: bool
    auto_refresh: bool
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> PlanRecord:
        return cls(
            plan_id=str(row["plan_id"]),
            project_id=str(row["project_id"]),
            repo=str(row["repo"]),
            base_ref=str(row["base_ref"]),
            active_version=str(row["active_version"]),
            file_sha256=str(row["file_sha256"]),
            store_root=str(row["store_root"]),
            status=str(row["status"]),
            auto_advance=bool(row["auto_advance"]),
            auto_refresh=bool(row["auto_refresh"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def plan_store(self) -> PlanStore:
        """Return the plan store this plan's revision lives in."""
        return PlanStore(self.store_root or None)


@dataclass(frozen=True)
class _LaneRow:
    lane_id: str
    lane_digest: str
    job_id: str | None
    superseded_by: str | None
    owner_done_at: str | None
    owner_evidence: str | None


@dataclass(frozen=True)
class LaneView:
    """One lane's live, record-derived state (the basis of P3's ledger)."""

    lane_id: str
    title: str
    wave: str
    owner: bool
    state: str
    status: str
    reasons: tuple[str, ...] = ()
    job_id: str | None = None

    @property
    def is_ready(self) -> bool:
        return self.state == "ready"


@dataclass(frozen=True)
class PlanProjection:
    """The active revision plus its live lane views and ready set."""

    plan: PlanRecord
    revision: PlanRevision
    views: tuple[LaneView, ...]
    eligible: tuple[str, ...]
    projected: bool
    reasons: tuple[str, ...] = ()

    def task(self, lane_id: str) -> PlanTaskDef | None:
        for item in self.revision.tasks:
            if item.task_id == lane_id:
                return item
        return None

    def view(self, lane_id: str) -> LaneView | None:
        for item in self.views:
            if item.lane_id == lane_id:
                return item
        return None


@dataclass(frozen=True)
class LaneSubmitResult:
    """The outcome of one lane-submission attempt."""

    lane_id: str
    job_id: str | None = None
    created: bool = False
    refused: str | None = None


@dataclass(frozen=True)
class PlanReconcileResult:
    """What one reconciler pass did to one plan."""

    plan_id: str
    project_id: str
    revision: str
    submitted: tuple[str, ...] = ()
    recovered: tuple[str, ...] = ()
    paused: str | None = None
    error: str | None = None
    lanes: tuple[LaneView, ...] = ()

    def lane(self, lane_id: str) -> LaneView | None:
        for item in self.lanes:
            if item.lane_id == lane_id:
                return item
        return None


@dataclass(frozen=True)
class PlanSubmission:
    """The result of activating a plan file."""

    plan: PlanRecord
    revision: PlanRevision
    lanes: tuple[LaneView, ...] = ()
    submitted: tuple[str, ...] = ()
    paused: str | None = None
    error: str | None = None

    @property
    def held(self) -> tuple[LaneView, ...]:
        return tuple(view for view in self.lanes if view.state in {"held", "owner"})


# ---------------------------------------------------------------------------
# Planning-session gate (F17 seam)
# ---------------------------------------------------------------------------


def set_lane_submit_gate(gate: Callable[[Any], str | None] | None) -> None:
    """Install (or clear) the gate every lane submission passes through.

    Lane F17 (closing the planning CLI pauses all work) will register its pause
    check here.  The gate returns a short reason when work must not start; a
    raising gate is treated as a pause, never as permission.
    """
    global LANE_SUBMIT_GATE
    LANE_SUBMIT_GATE = gate


def planning_session_active() -> bool:
    """True when a live planning session holds the MCP planning lease.

    A missing, malformed or dead lease is not a session.  Plan lanes therefore
    pause when the owner closes the planning CLI, even though the scheduler
    keeps running.
    """
    try:
        record = OrchestratorLease().read()
    except Exception:  # pragma: no cover - defensive: an unreadable lease is not a session
        return False
    return record is not None and pid_is_alive(record.pid)


def _submission_refusal(db: Any, *, owner_initiated: bool) -> str | None:
    gate = LANE_SUBMIT_GATE
    if gate is not None:
        try:
            reason = gate(db)
        except Exception:
            logging.getLogger(__name__).exception("lane submission gate raised")
            return PLANNING_GATE_ERROR
        if reason:
            return str(reason)
    if owner_initiated:
        return None
    if not planning_session_active():
        return NO_PLANNING_SESSION
    return None


# ---------------------------------------------------------------------------
# Repository, project and lane identity
# ---------------------------------------------------------------------------


def resolve_project_id(repo: Path | str, explicit: str | None = None) -> str:
    """Resolve the plan-store project id for a repository.

    An explicit slug wins.  Otherwise a registered project whose canonical
    checkout matches is used; an unregistered repository falls back to its
    stable canonical identity (the same hash runs are recorded under), so a
    plan can be activated before registration without inventing a second key.
    """
    root = canonical_repo_root(repo)
    if root is None:
        raise PlanRunnerError(f"{repo} is not a git repository")
    if explicit:
        slug = explicit.strip()
        if _PROJECT_ID_RE.fullmatch(slug) is None:
            raise PlanRunnerError(
                f"malformed project id {explicit!r}: use 1-64 lowercase letters, "
                "digits and interior dashes"
            )
        return slug
    resolved = Path(root).resolve()
    try:
        projects = ProjectRegistry().list_projects()
    except Exception as exc:
        raise PlanRunnerError(f"project registry is unreadable: {exc}") from exc
    for project in projects:
        try:
            same = Path(project.repository_root).resolve() == resolved
        except OSError:
            same = False
        if same:
            return project.project_id
    try:
        return compute_project_identity(root).project_id
    except Exception as exc:  # pragma: no cover - canonical identity already proved git
        raise PlanRunnerError(f"cannot derive a project id for {root}: {exc}") from exc


def resolve_plan_path(repo_root: Path | str, plan_path: Path | str) -> Path:
    """Resolve a path-only plan reference inside the canonical checkout.

    The file must be a regular Markdown file at or below ``repo_root`` after
    symlink resolution, so ``..`` and symlink escapes are refused, and it may
    not exceed :data:`MAX_PLAN_FILE_BYTES`.
    """
    root = Path(repo_root).expanduser().resolve()
    candidate = Path(plan_path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PlanRunnerError(
            f"plan file {plan_path} resolves outside the checkout {root}"
        ) from exc
    if resolved.suffix.lower() != ".md":
        raise PlanRunnerError(f"plan file {plan_path} must be a Markdown (.md) file")
    if not resolved.is_file():
        raise PlanRunnerError(f"plan file {plan_path} does not exist")
    size = resolved.stat().st_size
    if size > MAX_PLAN_FILE_BYTES:
        raise PlanRunnerError(
            f"plan file {plan_path} is {size} bytes; the cap is {MAX_PLAN_FILE_BYTES}"
        )
    return resolved


def lane_digest(task: PlanTaskDef) -> str:
    """Return the semantic digest of one lane spec.

    Idempotency is keyed on this digest, so re-submitting an unchanged lane is
    a no-op while a changed lane is a different request.
    """
    payload = task.as_dict(format_version=2)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Plan records
# ---------------------------------------------------------------------------


def get_plan(db: Any, plan_id: str) -> PlanRecord | None:
    """Return one plan row, or ``None``."""
    ensure_plan_tables(db)
    with _owner_conn(db) as conn:
        row = conn.execute(
            "SELECT * FROM plans WHERE plan_id = ?", (plan_id,)
        ).fetchone()
    return PlanRecord.from_row(row) if row is not None else None


def active_plan_for_project(db: Any, project_id: str) -> PlanRecord | None:
    """Return the project's single ACTIVE plan, if any."""
    ensure_plan_tables(db)
    with _owner_conn(db) as conn:
        row = conn.execute(
            "SELECT * FROM plans WHERE project_id = ? AND status = 'ACTIVE'",
            (project_id,),
        ).fetchone()
    return PlanRecord.from_row(row) if row is not None else None


def list_active_plans(db: Any) -> tuple[PlanRecord, ...]:
    """Return ACTIVE plans in creation order, capped per tick."""
    ensure_plan_tables(db)
    with _owner_conn(db) as conn:
        rows = conn.execute(
            "SELECT * FROM plans WHERE status = 'ACTIVE'"
            " ORDER BY created_at ASC, plan_id ASC LIMIT ?",
            (MAX_ACTIVE_PLANS_PER_TICK,),
        ).fetchall()
    return tuple(PlanRecord.from_row(row) for row in rows)


def _upsert_plan(
    db: Any,
    *,
    plan_id: str,
    project_id: str,
    repo: str,
    base_ref: str,
    active_version: str,
    file_sha256: str,
    store_root: str,
    now: datetime | None = None,
) -> PlanRecord:
    stamp = _iso(now)
    ensure_plan_tables(db)
    with _owner_conn(db) as conn:
        existing = conn.execute(
            "SELECT * FROM plans WHERE plan_id = ?", (plan_id,)
        ).fetchone()
        if existing is None:
            active = conn.execute(
                "SELECT plan_id FROM plans WHERE project_id = ? AND status = 'ACTIVE'",
                (project_id,),
            ).fetchone()
            if active is not None:
                raise PlanRunnerError(
                    f"project {project_id!r} already has an active plan "
                    f"{str(active['plan_id'])!r}; complete or abandon it before "
                    f"activating {plan_id!r}"
                )
            conn.execute(
                "INSERT INTO plans (plan_id, project_id, repo, base_ref,"
                " active_version, file_sha256, store_root, status, auto_advance,"
                " auto_refresh, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'ACTIVE', 1, 0, ?, ?)",
                (
                    plan_id,
                    project_id,
                    repo,
                    base_ref,
                    active_version,
                    file_sha256,
                    store_root,
                    stamp,
                    stamp,
                ),
            )
        else:
            conn.execute(
                "UPDATE plans SET project_id = ?, repo = ?, base_ref = ?,"
                " active_version = ?, file_sha256 = ?, store_root = ?,"
                " status = 'ACTIVE', updated_at = ? WHERE plan_id = ?",
                (
                    project_id,
                    repo,
                    base_ref,
                    active_version,
                    file_sha256,
                    store_root,
                    stamp,
                    plan_id,
                ),
            )
        try:
            conn.commit()
        except sqlite3.IntegrityError as exc:
            raise PlanRunnerError(
                f"project {project_id!r} already has an active plan: {exc}"
            ) from exc
        row = conn.execute(
            "SELECT * FROM plans WHERE plan_id = ?", (plan_id,)
        ).fetchone()
    if row is None:  # pragma: no cover - just written in this transaction
        raise PlanRunnerError(f"plan {plan_id!r} disappeared after write")
    return PlanRecord.from_row(row)


def _lane_rows(db: Any, plan_id: str) -> dict[str, _LaneRow]:
    ensure_plan_tables(db)
    with _owner_conn(db) as conn:
        rows = conn.execute(
            "SELECT lane_id, lane_digest, job_id, superseded_by, owner_done_at,"
            " owner_evidence FROM plan_lanes WHERE plan_id = ?",
            (plan_id,),
        ).fetchall()
    return {
        str(row["lane_id"]): _LaneRow(
            lane_id=str(row["lane_id"]),
            lane_digest=str(row["lane_digest"]),
            job_id=str(row["job_id"]) if row["job_id"] else None,
            superseded_by=str(row["superseded_by"]) if row["superseded_by"] else None,
            owner_done_at=str(row["owner_done_at"]) if row["owner_done_at"] else None,
            owner_evidence=(
                str(row["owner_evidence"]) if row["owner_evidence"] else None
            ),
        )
        for row in rows
    }


def _sync_lane_rows(
    db: Any, plan: PlanRecord, revision: PlanRevision, *, now: datetime | None = None
) -> None:
    """Register every lane in the revision without disturbing an existing job."""
    stamp = _iso(now)
    with _owner_conn(db) as conn:
        for task in revision.tasks:
            digest = lane_digest(task)
            conn.execute(
                "INSERT INTO plan_lanes (plan_id, lane_id, lane_digest,"
                " run_ids_json, updated_at) VALUES (?, ?, ?, '[]', ?)"
                " ON CONFLICT(plan_id, lane_id) DO UPDATE SET"
                " lane_digest = excluded.lane_digest, updated_at = excluded.updated_at",
                (plan.plan_id, task.task_id, digest, stamp),
            )
        conn.commit()


def _write_lane_job(
    db: Any,
    *,
    plan_id: str,
    lane_id: str,
    digest: str,
    job_id: str,
    now: datetime | None = None,
) -> None:
    """Link a lane to its job *after* the job exists (crash-safe mapping)."""
    with _owner_conn(db) as conn:
        conn.execute(
            "UPDATE plan_lanes SET job_id = ?, lane_digest = ?, updated_at = ?"
            " WHERE plan_id = ? AND lane_id = ?",
            (job_id, digest, _iso(now), plan_id, lane_id),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Projection (reused by the reconciler and, later, the state ledger)
# ---------------------------------------------------------------------------


def _lane_status(
    db: Any,
    task: PlanTaskDef,
    row: _LaneRow | None,
    jobs: dict[str, Job | None],
    accepted: dict[str, bool],
) -> tuple[str, Job | None]:
    """Map one lane's live records onto the frontier's status vocabulary."""
    if task.run == "owner":
        done = row is not None and row.owner_done_at is not None
        return (TaskStatus.COMPLETED.value if done else TaskStatus.PENDING.value, None)
    if row is not None and row.superseded_by:
        return TaskStatus.SKIPPED.value, None
    job_id = row.job_id if row is not None else None
    if not job_id:
        return TaskStatus.PENDING.value, None
    if job_id not in jobs:
        jobs[job_id] = store.get_job(db, job_id)
    job = jobs[job_id]
    if job is None:
        return TaskStatus.BLOCKED.value, None
    if job.state == JobState.SUCCEEDED:
        if job.job_id not in accepted:
            accepted[job.job_id] = job_is_accepted(db, job)
        return (
            TaskStatus.COMPLETED.value
            if accepted[job.job_id]
            else TaskStatus.OWNER_WAIT.value,
            job,
        )
    mapping = {
        JobState.QUEUED: TaskStatus.RESOURCE_WAIT.value,
        JobState.RUNNING: TaskStatus.RUNNING.value,
        JobState.FAILED: TaskStatus.FAILED.value,
        JobState.CANCELLED: TaskStatus.SKIPPED.value,
    }
    return mapping.get(job.state, TaskStatus.BLOCKED.value), job


def _dependency_ref(project_id: str, edge: str) -> TaskRef:
    """Resolve one stored dependency entry against its home project."""
    other, _, local = edge.partition("/")
    if local and other and "/" not in local:
        return TaskRef(project_id=other, task_id=local)
    return TaskRef(project_id=project_id, task_id=edge)


def _snapshot(
    project_id: str,
    revision: PlanRevision,
    statuses: dict[str, str],
) -> FrontierSnapshot:
    rows: list[PlanTask] = []
    for task in revision.tasks:
        rows.append(
            PlanTask(
                task_id=task.task_id,
                phase=task.wave or task.phase,
                status=statuses[task.task_id],
                kind=TaskKind.CHILD_TASK.value,
                title=task.title,
                start_dependencies=tuple(
                    _dependency_ref(project_id, edge)
                    for edge in task.start_dependencies
                ),
                integration_prerequisites=tuple(
                    _dependency_ref(project_id, edge)
                    for edge in task.integration_prerequisites
                ),
                owns=tuple(task.owns),
            )
        )
    return FrontierSnapshot(
        owner_id="owner",
        source=SnapshotSource.OWNER_DB.value,
        complete=True,
        projects=(ProjectPlan(project_id=project_id, tasks=tuple(rows)),),
    )


def _job_state_name(job: Job | None, accepted: dict[str, bool], row: _LaneRow | None) -> str:
    if job is None:
        if row is not None and row.superseded_by:
            return "superseded"
        return "blocked"
    if job.state == JobState.SUCCEEDED:
        return "accepted" if accepted.get(job.job_id) else "candidate"
    return {
        JobState.QUEUED: "queued",
        JobState.RUNNING: "running",
        JobState.FAILED: "failed",
        JobState.CANCELLED: "cancelled",
    }.get(job.state, "blocked")


def _project(
    db: Any, plan: PlanRecord, *, limits: FrontierLimits | None = None
) -> PlanProjection:
    try:
        revision = plan.plan_store().get_revision(plan.project_id, plan.active_version)
    except (PlanNotFoundError, PlanTamperError, PlanValidationError) as exc:
        raise PlanRunnerError(
            f"plan {plan.plan_id!r} revision {plan.active_version!r} is unreadable: {exc}"
        ) from exc
    rows = _lane_rows(db, plan.plan_id)
    jobs: dict[str, Job | None] = {}
    accepted: dict[str, bool] = {}
    statuses: dict[str, str] = {}
    for task in revision.tasks:
        statuses[task.task_id], _ = _lane_status(db, task, rows.get(task.task_id), jobs, accepted)

    snapshot = _snapshot(plan.project_id, revision, statuses)
    projection = project_ready_frontier(
        FrontierRequest(snapshot=snapshot, limits=limits or default_frontier_limits())
    )
    eligible_ids = tuple(item.task_id for item in projection.eligible)
    held = {item.task_id: item.reason_codes() for item in projection.held}
    views: list[LaneView] = []
    for task in revision.tasks:
        row = rows.get(task.task_id)
        job = jobs.get(row.job_id) if row is not None and row.job_id else None
        if task.run == "owner":
            state = "done" if row is not None and row.owner_done_at else "owner"
            reasons: tuple[str, ...] = ()
            job_id = None
        elif task.task_id in held:
            reasons = held[task.task_id]
            if row is not None and row.job_id:
                state = _job_state_name(job, accepted, row)
            else:
                state = "held"
            job_id = row.job_id if row is not None else None
        elif task.task_id in eligible_ids:
            reasons = ()
            state = "ready"
            job_id = None
        else:  # pragma: no cover - a task is either eligible or held
            reasons = ()
            state = "held"
            job_id = None
        views.append(
            LaneView(
                lane_id=task.task_id,
                title=task.title,
                wave=task.wave,
                owner=task.run == "owner",
                state=state,
                status=statuses[task.task_id],
                reasons=reasons,
                job_id=job_id,
            )
        )
    return PlanProjection(
        plan=plan,
        revision=revision,
        views=tuple(views),
        eligible=eligible_ids,
        projected=projection.status == ProjectionStatus.PROJECTED,
        reasons=projection.reason_codes(),
    )


def project_plan(db: Any, plan: PlanRecord) -> PlanProjection:
    """Project one plan's live lane state (deterministic, read-only)."""
    return _project(db, plan)


# ---------------------------------------------------------------------------
# Activation
# ---------------------------------------------------------------------------


def _base_ref(plan: PlanRecord) -> str:
    return (plan.base_ref or "HEAD").strip() or "HEAD"


def submit_plan_file(
    db: Any,
    *,
    repo: Path | str,
    plan_path: Path | str,
    project_id: str | None = None,
    base_ref: str | None = None,
    store: PlanStore | None = None,
    owner_initiated: bool = False,
    now: datetime | None = None,
) -> PlanSubmission:
    """Validate, activate and submit the ready lanes of one ``PLAN.md``.

    Activation is idempotent on semantic content.  It refuses a dirty checkout
    (acceptance needs a clean tree) and an unconfined path, then submits every
    currently eligible lane through :func:`submit_lane_job` -- the single
    function the F17 pause gate wraps.
    """
    root = canonical_repo_root(repo)
    if root is None:
        raise PlanRunnerError(f"{repo} is not a git repository")
    resolved = resolve_plan_path(root, plan_path)
    try:
        text = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        raise PlanRunnerError(f"cannot read plan file {resolved}: {exc}") from exc
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    try:
        parsed: ParsedPlan = parse_plan_file(text)
        validate_plan(parsed, project_id=project_id)
    except PlanFileError as exc:
        raise PlanRunnerError(str(exc)) from exc
    project = project_id or resolve_project_id(root)
    plan_id = parsed.plan_id

    existing = get_plan(db, plan_id)
    if existing is not None and existing.project_id != project:
        raise PlanRunnerError(
            f"plan {plan_id!r} already belongs to project {existing.project_id!r}"
        )
    active = active_plan_for_project(db, project)
    if active is not None and active.plan_id != plan_id:
        raise PlanRunnerError(
            f"project {project!r} already has an active plan {active.plan_id!r}; "
            "complete or abandon it first"
        )

    plan_store = store or PlanStore()
    try:
        revision = activate_plan_file(plan_store, parsed, project, repo=root)
    except (PlanFileError, PlanStoreError, OSError) as exc:
        raise PlanRunnerError(str(exc)) from exc

    effective_base = (base_ref or parsed.base or revision.base or "HEAD").strip() or "HEAD"
    plan = _upsert_plan(
        db,
        plan_id=plan_id,
        project_id=project,
        repo=str(root),
        base_ref=effective_base,
        active_version=revision.version,
        file_sha256=digest,
        store_root=str(plan_store.root),
        now=now,
    )
    _sync_lane_rows(db, plan, revision, now=now)
    _event(
        db,
        plan_id=plan_id,
        kind="plan_submitted",
        event_key=f"plan:{plan_id}:{revision.sha256}",
        payload={
            "plan_id": plan_id,
            "project_id": project,
            "version": revision.version,
            "file_sha256": digest,
            "revision_sha256": revision.sha256,
        },
        now=now,
    )
    _notify_scheduler(db)
    results = reconcile_plan_lanes(db, now=now, owner_initiated=owner_initiated)
    result = next((item for item in results if item.plan_id == plan_id), None)
    return PlanSubmission(
        plan=plan,
        revision=revision,
        lanes=() if result is None else result.lanes,
        submitted=() if result is None else result.submitted,
        paused=None if result is None else result.paused,
        error=None if result is None else result.error,
    )


def _notify_scheduler(db: Any) -> None:
    try:
        from saberops.capacity.loop import notify_scheduler_submission

        notify_scheduler_submission(db)
    except Exception:  # pragma: no cover - a missing scheduler is surfaced by the CLI/MCP
        logging.getLogger(__name__).exception("could not start or notify the scheduler")


# ---------------------------------------------------------------------------
# Lane submission (the one function F17 gates)
# ---------------------------------------------------------------------------


def _lane_requirements(task: PlanTaskDef) -> JobRequirements:
    effort = task.effort if task.effort in _EFFORTS else None
    size = task.size if task.size in _SIZES else None
    return JobRequirements(
        role=JobRole.BUILD,
        effort=Effort(effort) if effort else None,
        model_family_include=task.model_family_include,
        model_family_exclude=task.model_family_exclude,
        size_hint=size,
    )


def _lane_goal(task: PlanTaskDef) -> str:
    title = (task.title or task.task_id).strip()
    body = task.goal.strip()
    return f"{title}\n\n{body}" if body else title


def submit_lane_job(
    db: Any,
    *,
    plan: PlanRecord,
    task: PlanTaskDef,
    owner_initiated: bool = False,
    now: datetime | None = None,
) -> LaneSubmitResult:
    """Submit one eligible lane as an ordinary job, or refuse.

    This is the only lane-submission path.  It consults the F17 gate and, when
    the caller is the scheduler reconciler, requires a live planning session to
    hold the planning lease.  A lane already linked to a job is never
    resubmitted; a crash between submission and the mapping write recovers
    through the stable per-lane idempotency key.
    """
    if task.run == "owner":
        return LaneSubmitResult(lane_id=task.task_id, refused="owner_lane")
    refusal = _submission_refusal(db, owner_initiated=owner_initiated)
    if refusal is not None:
        return LaneSubmitResult(lane_id=task.task_id, refused=refusal)

    rows = _lane_rows(db, plan.plan_id)
    row = rows.get(task.task_id)
    if row is not None and row.job_id:
        return LaneSubmitResult(lane_id=task.task_id, job_id=row.job_id)

    digest = lane_digest(task)
    caller = f"plan:{plan.plan_id}"
    key = f"plan:{plan.plan_id}:{digest}"
    try:
        result = submit_job(
            db,
            repo=plan.repo,
            goal=_lane_goal(task),
            acceptance="\n".join(task.acceptance) or None,
            base_ref=_base_ref(plan),
            requirements=_lane_requirements(task),
            priority=task.priority if task.priority in _PRIORITIES else Priority.NORMAL,
            idempotency_key=key,
            caller_id=caller,
            now=now,
        )
    except (ValueError, sqlite3.Error) as exc:
        raise PlanRunnerError(f"lane {task.task_id}: cannot submit job: {exc}") from exc

    job = result.job
    _event(
        db,
        plan_id=plan.plan_id,
        lane_id=task.task_id,
        kind="plan_lane_submitted",
        event_key=f"plan:{plan.plan_id}:lane:{task.task_id}:{digest}",
        payload={
            "plan_id": plan.plan_id,
            "lane_id": task.task_id,
            "job_id": job.job_id,
            "base_ref": _base_ref(plan),
            "base_sha": job.base_sha,
            "revision": plan.active_version,
            "created": result.created,
        },
        now=now,
    )
    _write_lane_job(
        db,
        plan_id=plan.plan_id,
        lane_id=task.task_id,
        digest=digest,
        job_id=job.job_id,
        now=now,
    )
    return LaneSubmitResult(
        lane_id=task.task_id, job_id=job.job_id, created=result.created
    )


# ---------------------------------------------------------------------------
# Reconciler (called from scheduler_tick)
# ---------------------------------------------------------------------------


def _dependencies_satisfied(
    db: Any, plan: PlanRecord, task: PlanTaskDef, rows: dict[str, _LaneRow]
) -> bool:
    """Re-read dependency acceptance immediately before a submission."""
    if not task.start_dependencies:
        return True
    tasks = {item.task_id: item for item in plan.plan_store().get_revision(
        plan.project_id, plan.active_version
    ).tasks}
    jobs: dict[str, Job | None] = {}
    accepted: dict[str, bool] = {}
    for edge in task.start_dependencies:
        if "/" in edge:
            return False
        dependency = tasks.get(edge)
        if dependency is None:
            return False
        status, _ = _lane_status(db, dependency, rows.get(edge), jobs, accepted)
        if status != TaskStatus.COMPLETED.value:
            return False
    return True


def _reconcile_one(
    db: Any, plan: PlanRecord, *, owner_initiated: bool, now: datetime | None
) -> PlanReconcileResult:
    projection = _project(db, plan)
    if not projection.projected:
        raise PlanRunnerError(
            "projection denied: " + ", ".join(projection.reasons or ("unknown",))
        )
    rows = _lane_rows(db, plan.plan_id)
    eligible = [
        lane_id
        for lane_id in projection.eligible
        if (task := projection.task(lane_id)) is not None and task.run != "owner"
    ]
    submitted: list[str] = []
    recovered: list[str] = []
    paused: str | None = None
    for lane_id in eligible:
        task = projection.task(lane_id)
        if task is None:  # pragma: no cover - eligible ids come from the revision
            continue
        if not _dependencies_satisfied(db, plan, task, rows):
            # An acceptance landed between the projection and this write; the
            # next tick recomputes against the new HEAD.  Idempotency keeps the
            # race harmless.
            continue
        outcome = submit_lane_job(
            db, plan=plan, task=task, owner_initiated=owner_initiated, now=now
        )
        if outcome.refused is not None:
            paused = outcome.refused
            break
        if outcome.created:
            submitted.append(lane_id)
        elif outcome.job_id is not None:
            recovered.append(lane_id)
    final_views = projection.views
    if submitted or recovered:
        try:
            final_views = _project(db, plan).views
        except PlanRunnerError:  # pragma: no cover - the plan was readable a moment ago
            logging.getLogger(__name__).exception(
                "plan %s re-projection after submission failed", plan.plan_id
            )
    return PlanReconcileResult(
        plan_id=plan.plan_id,
        project_id=plan.project_id,
        revision=plan.active_version,
        submitted=tuple(submitted),
        recovered=tuple(recovered),
        paused=paused,
        lanes=final_views,
    )


def reconcile_plan_lanes(
    db: Any, *, now: datetime | None = None, owner_initiated: bool = False
) -> tuple[PlanReconcileResult, ...]:
    """Submit every eligible plan lane, one ordinary job per lane.

    Runs inside ``scheduler_tick`` before the queue is read, so a lane that
    becomes ready in this tick can also dispatch in it.  A malformed or
    unreadable plan records one ``plan_reconcile_failed`` event and never stops
    unrelated jobs.
    """
    ensure_plan_tables(db)
    results: list[PlanReconcileResult] = []
    for plan in list_active_plans(db):
        try:
            results.append(
                _reconcile_one(db, plan, owner_initiated=owner_initiated, now=now)
            )
        except Exception as exc:
            logging.getLogger(__name__).exception(
                "plan %s reconcile failed", plan.plan_id
            )
            reason = f"{type(exc).__name__}: {exc}"
            _event(
                db,
                plan_id=plan.plan_id,
                kind="plan_reconcile_failed",
                event_key=(
                    f"plan:{plan.plan_id}:reconcile_failed:"
                    f"{hashlib.sha256(reason.encode()).hexdigest()[:16]}"
                ),
                payload={"reason": reason},
                now=now,
            )
            results.append(
                PlanReconcileResult(
                    plan_id=plan.plan_id,
                    project_id=plan.project_id,
                    revision=plan.active_version,
                    error=reason,
                )
            )
    return tuple(results)


# ---------------------------------------------------------------------------
# Owner-lane completion
# ---------------------------------------------------------------------------


def mark_owner_lane_done(
    db: Any,
    *,
    plan_id: str,
    lane_id: str,
    evidence: str,
    now: datetime | None = None,
) -> LaneView:
    """Record owner completion of one ``run: "owner"`` lane.

    Only an owner command calls this.  The engine never marks such a lane done
    and never edits the progress view.
    """
    clean = evidence.strip() if isinstance(evidence, str) else ""
    if not clean:
        raise PlanRunnerError("owner-lane completion needs non-empty evidence")
    plan = get_plan(db, plan_id)
    if plan is None:
        raise PlanRunnerError(f"unknown plan {plan_id!r}")
    projection = _project(db, plan)
    task = projection.task(lane_id)
    if task is None:
        raise PlanRunnerError(f"plan {plan_id!r} has no lane {lane_id!r}")
    if task.run != "owner":
        raise PlanRunnerError(
            f"lane {lane_id!r} is a job lane; the engine submits it and only the "
            "owner accepts its result"
        )
    rows = _lane_rows(db, plan_id)
    row = rows.get(lane_id)
    if row is not None and row.owner_done_at is not None:
        view = _project(db, plan).view(lane_id)
        if view is not None:
            return view
    stamp = _iso(now)
    with _owner_conn(db) as conn:
        conn.execute(
            "UPDATE plan_lanes SET owner_done_at = ?, owner_evidence = ?, updated_at = ?"
            " WHERE plan_id = ? AND lane_id = ?",
            (stamp, clean, stamp, plan_id, lane_id),
        )
        conn.commit()
    _event(
        db,
        plan_id=plan_id,
        lane_id=lane_id,
        kind="plan_lane_done",
        event_key=f"plan:{plan_id}:lane:{lane_id}:done",
        payload={"lane_id": lane_id, "evidence": clean},
        now=now,
    )
    _notify_scheduler(db)
    view = _project(db, plan).view(lane_id)
    if view is None:  # pragma: no cover - the lane was just proved present
        raise PlanRunnerError(f"lane {lane_id!r} disappeared after completion")
    return view
