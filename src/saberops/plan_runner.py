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
* P6 adds the deterministic landing queue (wave, then lane id), stale marking
  for a candidate built on an old target and an owner-enabled refresh pass that
  rebuilds at most one stale sibling per tick through E6.  It never accepts and
  never pushes; a failed refresh is recorded on the lane, not retried forever.

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
3. **Auto-advance asks the typed owner decision (F1).**  The same
   :func:`saberops.owner_mode.decide_owner_action` every other decision point
   calls answers whether a lane may start.  A mode can only narrow the
   authority preset; an unreadable mode or authority document refuses the
   start.  This is a *start* capability only: acceptance stays owner-only
   until F15, so an away plan parks at the first candidate with that reason
   visible in ``STATE.md``.

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

from saberops.git import GitManager
from saberops.jobs import store
from saberops.jobs.dependencies import job_is_accepted, run_is_accepted
from saberops.jobs.models import (
    Effort,
    Job,
    JobRequirements,
    JobRole,
    JobState,
    Priority,
)
from saberops.jobs.service import cancel_job, submit_job
from saberops.models import Run, RunStatus
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
    "LaneRefresh",
    "LaneSubmitResult",
    "LaneView",
    "PlanProjection",
    "PlanReconcileResult",
    "PlanRecord",
    "PlanRefreshResult",
    "PlanRunnerError",
    "PlanSubmission",
    "RefreshOutcome",
    "ensure_plan_tables",
    "lane_digest",
    "lane_refresh_failure",
    "lane_run",
    "latest_lane_refresh_event",
    "list_active_plans",
    "mark_owner_lane_done",
    "plan_lane_rows",
    "plan_target_head",
    "planning_session_active",
    "project_plan",
    "reconcile_plan_lanes",
    "refresh_stale_plan_candidates",
    "resolve_plan_path",
    "resolve_project_id",
    "run_store",
    "set_auto_refresh",
    "set_lane_submit_gate",
    "stale_candidate_lanes",
    "submit_lane_job",
    "submit_plan_file",
    "wave_sort_key",
]

#: Upper bound on an authored plan file read over MCP or the CLI.
MAX_PLAN_FILE_BYTES = 256 * 1024

#: At most this many plans are reconciled per scheduler tick (report §4.1).
MAX_ACTIVE_PLANS_PER_TICK = 3

#: At most one stale sibling is refreshed per scheduler tick.  A refresh runs
#: a gate and an independent review, so the queue advances one lane per tick
#: instead of blocking the tick for a whole wave.
MAX_REFRESHES_PER_TICK = 1

#: F17 seam: a callable returning a pause reason, or ``None`` to allow work.
#: It receives the owner database.  A raising gate fails closed.
LANE_SUBMIT_GATE: Callable[[Any], str | None] | None = None

#: Reason recorded when the reconciler runs without a planning session.
NO_PLANNING_SESSION = "no_planning_session"
PLANNING_GATE_ERROR = "planning_gate_error"
#: Reason recorded when the plan record itself disables automatic advance.
AUTO_ADVANCE_OFF = "auto_advance_off"
#: Prefix for a refusal from F1's effective-permission function.
MODE_RESTRICTED = "mode_restricted"

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
            columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(plan_lanes)")
            }
            if {"lane_spec_digest", "generation"} <= columns:
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
                lane_spec_digest TEXT NOT NULL DEFAULT '',
                generation INTEGER NOT NULL DEFAULT 0,
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
        # Older plan tables predate mid-run edits (P4).  Adding the columns in
        # place keeps an existing owner store readable without a second table.
        columns = {
            str(row["name"]) for row in conn.execute("PRAGMA table_info(plan_lanes)")
        }
        if "lane_spec_digest" not in columns:
            conn.execute(
                "ALTER TABLE plan_lanes ADD COLUMN lane_spec_digest TEXT NOT NULL DEFAULT ''"
            )
        if "generation" not in columns:
            conn.execute(
                "ALTER TABLE plan_lanes ADD COLUMN generation INTEGER NOT NULL DEFAULT 0"
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
    lane_spec_digest: str
    generation: int
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
    #: The revision that superseded this lane's mapped attempt, if any (P4).
    superseded_by: str | None = None

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


@dataclass(frozen=True)
class LaneRefresh:
    """One stale candidate awaiting refresh, in deterministic landing order."""

    lane_id: str
    wave: str
    job_id: str
    run_id: str
    base_commit: str


@dataclass(frozen=True)
class RefreshOutcome:
    """The result of one automatic refresh attempt."""

    lane_id: str
    source_run_id: str
    target_base_sha: str
    refreshed_run_id: str | None = None
    candidate_sha: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class PlanRefreshResult:
    """What one tick's landing-refresh pass did to one plan."""

    plan_id: str
    outcomes: tuple[RefreshOutcome, ...] = ()
    stopped: str | None = None


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

    F17: liveness is the heartbeat, not only the pid.  A session whose CLI was
    killed leaves a dead pid; one whose connection broke or that crashed leaves
    a stale heartbeat.  Both are "no session", so plan lanes stop being
    submitted.  A missing, malformed or unreadable lease is not a session.
    """
    try:
        from saberops.runtime_gate import presence

        return presence().live
    except Exception:  # pragma: no cover - defensive: an unreadable lease is not a session
        return False


def _dispatch_permission() -> str | None:
    """Return a refusal reason when F1's effective permission forbids a start.

    Plan auto-advance is a start capability, so it asks the same typed owner
    decision every other decision point asks (F1/ADR-0020).  `ask-me` leaves
    the authority preset's ``auto_start`` intact: starting an already-activated
    lane is not an owner judgment, while acceptance stays owner-only.  A
    narrower authority preset (`ask_before_actions`) refuses it here, and a
    mode can only narrow the preset, never widen it.  An unreadable authority
    or mode document fails closed.
    """
    try:
        from saberops.owner_mode import ModeAction, decide_owner_action

        decision = decide_owner_action(ModeAction.DISPATCH)
    except Exception as exc:  # a corrupt document must never become permission
        logging.getLogger(__name__).exception("owner-mode decision failed")
        return f"{MODE_RESTRICTED}: owner mode is unreadable ({type(exc).__name__})"
    if decision.allowed:
        return None
    return f"{MODE_RESTRICTED}: {decision.reason}"


def _submission_refusal(
    db: Any, *, plan: PlanRecord, owner_initiated: bool
) -> str | None:
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
    if not plan.auto_advance:
        return AUTO_ADVANCE_OFF
    refusal = _dispatch_permission()
    if refusal is not None:
        return refusal
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


def lane_spec_digest(task: PlanTaskDef) -> str:
    """Return the digest of a lane's brief, ignoring its dependency edges.

    A mid-run edit (P4) treats a dependency-only change differently from a
    changed brief: edges are never retroactive, while a changed brief supersedes
    a live attempt.  The edges are part of :func:`lane_digest` (the idempotency
    key), so the two cases need separate hashes.
    """
    payload = task.as_dict(format_version=2)
    payload["start_dependencies"] = []
    payload["integration_prerequisites"] = []
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


def set_auto_refresh(
    db: Any, *, plan_id: str, enabled: bool, now: datetime | None = None
) -> PlanRecord:
    """Turn automatic stale-sibling refresh on or off for one active plan.

    Owner-only: no MCP tool calls this.  Turning it on only authorises the
    bounded refresh pass; it never accepts a candidate and never pushes.
    """
    plan = get_plan(db, plan_id)
    if plan is None:
        raise PlanRunnerError(f"unknown plan {plan_id!r}")
    if plan.status != "ACTIVE":
        raise PlanRunnerError(
            f"plan {plan_id!r} is {plan.status.lower()}; only an active plan "
            "refreshes candidates"
        )
    if plan.auto_refresh == enabled:
        return plan
    stamp = _iso(now)
    with _owner_conn(db) as conn:
        conn.execute(
            "UPDATE plans SET auto_refresh = ?, updated_at = ? WHERE plan_id = ?",
            (1 if enabled else 0, stamp, plan_id),
        )
        conn.commit()
    _event(
        db,
        plan_id=plan_id,
        kind="plan_auto_refresh_set",
        event_key=f"plan:{plan_id}:auto_refresh:{'on' if enabled else 'off'}:{stamp}",
        payload={"plan_id": plan_id, "enabled": enabled},
        now=now,
    )
    _notify_scheduler(db)
    updated = get_plan(db, plan_id)
    if updated is None:  # pragma: no cover - just updated in this transaction
        raise PlanRunnerError(f"plan {plan_id!r} disappeared after update")
    return updated


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
            "SELECT lane_id, lane_digest, lane_spec_digest, generation, job_id,"
            " superseded_by, owner_done_at, owner_evidence FROM plan_lanes"
            " WHERE plan_id = ?",
            (plan_id,),
        ).fetchall()
    return {
        str(row["lane_id"]): _LaneRow(
            lane_id=str(row["lane_id"]),
            lane_digest=str(row["lane_digest"]),
            lane_spec_digest=str(row["lane_spec_digest"] or ""),
            generation=int(row["generation"] or 0),
            job_id=str(row["job_id"]) if row["job_id"] else None,
            superseded_by=str(row["superseded_by"]) if row["superseded_by"] else None,
            owner_done_at=str(row["owner_done_at"]) if row["owner_done_at"] else None,
            owner_evidence=(
                str(row["owner_evidence"]) if row["owner_evidence"] else None
            ),
        )
        for row in rows
    }


def plan_lane_rows(db: Any, plan_id: str) -> dict[str, _LaneRow]:
    """Public read-only accessor for one plan's lane rows (P3 projection)."""
    return _lane_rows(db, plan_id)


# ---------------------------------------------------------------------------
# Landing order and the lane's live run (shared by the ledger and P6)
# ---------------------------------------------------------------------------


def wave_sort_key(wave: str) -> tuple[int, int, str]:
    """Deterministic wave order: numeric waves ascending, then labels by name.

    Landing order is ``(wave, lane id)``; this is the one wave key both the
    rendered ledger and the refresh queue use, so they cannot disagree.
    """
    text = (wave or "").strip()
    if text.isdigit():
        return (0, int(text), "")
    return (1, 0, text.lower())


def plan_target_head(plan: PlanRecord) -> str | None:
    """Current target commit of the plan's checkout, or ``None`` if unreadable."""
    try:
        root = canonical_repo_root(plan.repo)
        if root is None:
            return None
        return GitManager.get_head_commit(root)
    except Exception:
        return None


def run_store(db: Any, run_id: str) -> Any:
    """The store that owns ``run_id``: the caller's, else its project store.

    Jobs live in the owner database while run state may live in the owning
    project's store.  Resolution follows the run id's own project segment and
    fails back to the caller's store when the run is unknown, so a read-only
    client never writes and a missing project store never hides a job row.
    """
    try:
        if db.get_run(run_id) is not None:
            return db
    except Exception:  # pragma: no cover - unreadable run row falls back
        return db
    try:
        from saberops.db import Database
        from saberops.runtime import db_path_for_run

        path = db_path_for_run(run_id)
        if path is None:
            return db
        caller_path = getattr(db, "db_path", None)
        if caller_path is not None and Path(caller_path).resolve() == Path(path).resolve():
            return db
        read_only = bool(getattr(db, "_read_only", False))
        owner = getattr(db, "owner_db", db)
        return Database(path, owner_db=owner, read_only=read_only)
    except Exception:
        return db


def lane_run(db: Any, job: Job | None, *, accepted: bool) -> tuple[Run | None, Any]:
    """Follow the job's run chain, including refreshed runs linked to it.

    Returns the newest completed run (the accepted one when the lane is
    accepted), else the newest run of any status, together with the store that
    holds it.  A refreshed run is appended to ``job.run_ids`` (P3), so a
    projection and the refresh queue see the same candidate.
    """
    if job is None or not job.run_ids:
        return None, db
    found: list[tuple[Run, Any]] = []
    for run_id in job.run_ids:
        store_for_run = run_store(db, run_id)
        run = store_for_run.get_run(run_id)
        if run is not None:
            found.append((run, store_for_run))
    completed = [item for item in found if item[0].status is RunStatus.COMPLETED]
    if accepted:
        accepted_runs = [item for item in completed if run_is_accepted(db, item[0].id)]
        if accepted_runs:
            return accepted_runs[-1]
    if completed:
        return completed[-1]
    if found:
        return found[-1]
    return None, db


def _sync_lane_rows(
    db: Any, plan: PlanRecord, revision: PlanRevision, *, now: datetime | None = None
) -> None:
    """Register every lane in the revision without disturbing an existing row.

    An existing row keeps the digest of the spec its current attempt was
    submitted under; the reconciler compares that digest with the active
    revision and applies the mid-run edit rules (P4).  Only a lane the plan has
    never seen is inserted here.
    """
    stamp = _iso(now)
    with _owner_conn(db) as conn:
        for task in revision.tasks:
            conn.execute(
                "INSERT INTO plan_lanes (plan_id, lane_id, lane_digest,"
                " lane_spec_digest, generation, run_ids_json, updated_at)"
                " VALUES (?, ?, ?, ?, 0, '[]', ?)"
                " ON CONFLICT(plan_id, lane_id) DO NOTHING",
                (
                    plan.plan_id,
                    task.task_id,
                    lane_digest(task),
                    lane_spec_digest(task),
                    stamp,
                ),
            )
        conn.commit()


def _write_lane_job(
    db: Any,
    *,
    plan_id: str,
    lane_id: str,
    digest: str,
    spec_digest: str,
    job_id: str,
    now: datetime | None = None,
) -> None:
    """Link a lane to its job *after* the job exists (crash-safe mapping)."""
    with _owner_conn(db) as conn:
        conn.execute(
            "UPDATE plan_lanes SET job_id = ?, lane_digest = ?, lane_spec_digest = ?,"
            " superseded_by = NULL, updated_at = ?"
            " WHERE plan_id = ? AND lane_id = ?",
            (job_id, digest, spec_digest, _iso(now), plan_id, lane_id),
        )
        conn.commit()


def _update_lane_row(
    db: Any,
    *,
    plan_id: str,
    lane_id: str,
    digest: str,
    spec_digest: str,
    job_id: str | None,
    superseded_by: str | None,
    bump_generation: bool = False,
    now: datetime | None = None,
) -> None:
    """Write one lane row's active spec and attempt in a single UPDATE.

    ``bump_generation`` retires the previous attempt: the next submission's
    idempotency key must differ from the retired job, so re-adding an identical
    lane cannot collide with a cancelled one.
    """
    generation_sql = ", generation = generation + 1" if bump_generation else ""
    with _owner_conn(db) as conn:
        conn.execute(
            "UPDATE plan_lanes SET job_id = ?, lane_digest = ?, lane_spec_digest = ?,"
            f" superseded_by = ?{generation_sql}, updated_at = ?"
            " WHERE plan_id = ? AND lane_id = ?",
            (
                job_id,
                digest,
                spec_digest,
                superseded_by,
                _iso(now),
                plan_id,
                lane_id,
            ),
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
    """Map one lane's live records onto the frontier's status vocabulary.

    A superseded lane (P4) keeps the status of its live attempt: an attempt
    that is still running reserves its scopes and holds its dependants, and an
    accepted attempt is an owner decision that counts.  Only an attempt with no
    job is ``PENDING`` again, so the reconciler can submit the new spec.
    """
    if task.run == "owner":
        done = row is not None and row.owner_done_at is not None
        return (TaskStatus.COMPLETED.value if done else TaskStatus.PENDING.value, None)
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
                superseded_by=row.superseded_by if row is not None else None,
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

    # P4: refuse a revision that rewrites accepted or owner-completed work
    # BEFORE anything is stored or cancelled.  The old revision stays active.
    if existing is not None and existing.active_version:
        try:
            old_revision = existing.plan_store().get_revision(
                existing.project_id, existing.active_version
            )
        except (PlanNotFoundError, PlanTamperError, PlanValidationError) as exc:
            raise PlanRunnerError(
                f"plan {plan_id!r} revision {existing.active_version!r} is "
                f"unreadable: {exc}"
            ) from exc
        _backfill_lane_specs(db, existing, old_revision, now=now)
        refuse_lane_rewrite(db, existing, old_revision, parsed.to_tasks())
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
    # P3: render immediately after activation; the tick re-renders only when
    # the projection digest changes.  A render failure never fails the submit.
    try:
        from saberops.plan_state import render_plan_state

        render_plan_state(db, plan, now=now)
    except Exception:  # pragma: no cover - the projection is best-effort here
        logging.getLogger(__name__).exception(
            "plan state render after submit failed for plan %s", plan_id
        )
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


#: Bound on each scout report carried into a dependant lane's goal (F19).
MAX_ATTACHED_SCOUT_REPORT_CHARS = 20_000


def _scout_report_context(
    db: Any, plan: PlanRecord, task: PlanTaskDef, rows: dict[str, _LaneRow]
) -> tuple[str, tuple[dict[str, str], ...]]:
    """Attach the reports of a lane's scout dependencies (F19).

    A scout lane has no candidate to accept, so its report *is* what a
    dependant inherits.  The report is read from its durable E4 path and its
    path and digest are recorded on the dependant's submission event.
    """
    if not task.start_dependencies:
        return "", ()
    from saberops.scout import latest_report

    revision = plan.plan_store().get_revision(plan.project_id, plan.active_version)
    tasks = {item.task_id: item for item in revision.tasks}
    blocks: list[str] = []
    refs: list[dict[str, str]] = []
    for edge in task.start_dependencies:
        dependency = tasks.get(edge)
        row = rows.get(edge)
        if dependency is None or dependency.kind != "scout" or row is None or not row.job_id:
            continue
        report = latest_report(db, row.job_id)
        if not report or not report.get("report_path"):
            continue
        try:
            body = Path(str(report["report_path"])).read_text(encoding="utf-8")
        except OSError:
            continue
        blocks.append(
            f"[SCOUT REPORT from lane {edge} (job {row.job_id})]\n"
            f"Path: {report['report_path']}\n\n"
            f"{body.strip()[:MAX_ATTACHED_SCOUT_REPORT_CHARS]}"
        )
        refs.append(
            {
                "lane_id": edge,
                "job_id": str(row.job_id),
                "report_path": str(report["report_path"]),
                "report_digest": str(report.get("digest") or ""),
            }
        )
    return "\n\n".join(blocks), tuple(refs)


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
    refusal = _submission_refusal(
        db, plan=plan, owner_initiated=owner_initiated
    )
    if refusal is not None:
        return LaneSubmitResult(lane_id=task.task_id, refused=refusal)

    rows = _lane_rows(db, plan.plan_id)
    row = rows.get(task.task_id)
    if row is not None and row.job_id:
        return LaneSubmitResult(lane_id=task.task_id, job_id=row.job_id)

    digest = lane_digest(task)
    spec_digest = lane_spec_digest(task)
    generation = row.generation if row is not None else 0
    caller = f"plan:{plan.plan_id}"
    key = f"plan:{plan.plan_id}:{generation}:{digest}"
    goal = _lane_goal(task)
    report_context, scout_reports = _scout_report_context(db, plan, task, rows)
    if report_context:
        goal = f"{goal}\n\n{report_context}"
    try:
        result = submit_job(
            db,
            repo=plan.repo,
            goal=goal,
            acceptance="\n".join(task.acceptance) or None,
            base_ref=_base_ref(plan),
            requirements=_lane_requirements(task),
            priority=task.priority if task.priority in _PRIORITIES else Priority.NORMAL,
            kind="SCOUT" if task.kind == "scout" else "BUILD",
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
        event_key=(
            f"plan:{plan.plan_id}:lane:{task.task_id}:{generation}:{digest}"
        ),
        payload={
            "plan_id": plan.plan_id,
            "lane_id": task.task_id,
            "job_id": job.job_id,
            "base_ref": _base_ref(plan),
            "base_sha": job.base_sha,
            "revision": plan.active_version,
            "created": result.created,
            "kind": job.kind.value,
            "generation": generation,
            "scout_reports": list(scout_reports),
        },
        now=now,
    )
    _write_lane_job(
        db,
        plan_id=plan.plan_id,
        lane_id=task.task_id,
        digest=digest,
        spec_digest=spec_digest,
        job_id=job.job_id,
        now=now,
    )
    return LaneSubmitResult(
        lane_id=task.task_id, job_id=job.job_id, created=result.created
    )


# ---------------------------------------------------------------------------
# Mid-run plan edits (P4)
# ---------------------------------------------------------------------------


def _never_signal_running(run_id: str) -> bool:
    """Refuse to cancel a RUNNING attempt (a superseded lane finishes its brief).

    ``cancel_job`` asks a run canceller only for a RUNNING job.  Returning
    ``False`` means "leave it alone"; it is the mechanical form of the lane's
    promise that a plan edit never cuts off work already in flight.
    """
    logging.getLogger(__name__).info(
        "plan edit kept running attempt %s; it finishes under its original brief",
        run_id,
    )
    return False


def _cancel_queued_lane_job(db: Any, job: Job) -> tuple[bool, Job | None]:
    """Cancel a QUEUED lane job; never signal a RUNNING one.

    Returns ``(cancelled, current)`` where ``current`` is the re-read job
    (``None`` when it disappeared).  A job that raced into RUNNING between the
    read and the cancel comes back unchanged.
    """
    if job.state != JobState.QUEUED:
        return False, job
    try:
        result = cancel_job(db, job.job_id, run_canceller=_never_signal_running)
    except Exception:  # pragma: no cover - a cancel failure is retried next tick
        logging.getLogger(__name__).exception(
            "cannot cancel superseded queued job %s", job.job_id
        )
        result = None
    current = store.get_job(db, job.job_id)
    return (result is not None and result.cancelled), current


def _supersede_event(
    db: Any,
    plan: PlanRecord,
    lane_id: str,
    *,
    action: str,
    previous_digest: str,
    digest: str,
    job: Job | None,
    now: datetime | None = None,
) -> None:
    """Record one idempotent ``plan_lane_superseded`` event."""
    _event(
        db,
        plan_id=plan.plan_id,
        lane_id=lane_id,
        kind="plan_lane_superseded",
        event_key=(
            f"plan:{plan.plan_id}:lane:{lane_id}:superseded:"
            f"{plan.active_version}:{digest}"
        ),
        payload={
            "plan_id": plan.plan_id,
            "lane_id": lane_id,
            "action": action,
            "revision": plan.active_version,
            "previous_digest": previous_digest,
            "digest": digest,
            "job_id": job.job_id if job is not None else None,
            "job_state": job.state.value if job is not None else None,
        },
        now=now,
    )


def _backfill_lane_specs(
    db: Any, plan: PlanRecord, revision: PlanRevision, *, now: datetime | None = None
) -> None:
    """Give rows written before P4 a brief digest from the previous revision.

    Without this, the first edit after an upgrade cannot tell a dependency-only
    change from a changed brief on a lane whose live attempt predates the
    column.
    """
    rows = _lane_rows(db, plan.plan_id)
    for task in revision.tasks:
        row = rows.get(task.task_id)
        if row is None or row.lane_spec_digest:
            continue
        _update_lane_row(
            db,
            plan_id=plan.plan_id,
            lane_id=task.task_id,
            digest=row.lane_digest,
            spec_digest=lane_spec_digest(task),
            job_id=row.job_id,
            superseded_by=row.superseded_by,
            now=now,
        )


def refuse_lane_rewrite(
    db: Any, plan: PlanRecord, revision: PlanRevision, tasks: tuple[PlanTaskDef, ...]
) -> None:
    """Refuse a revision that rewrites accepted or owner-completed work.

    Run before the new revision is stored, so the previously active revision
    stays active and nothing is cancelled.  Accepted lanes are facts; replacing
    one means adding a new lane.
    """
    previous = {task.task_id: task for task in revision.tasks}
    rows = _lane_rows(db, plan.plan_id)
    for task in tasks:
        old = previous.get(task.task_id)
        if old is None or lane_digest(old) == lane_digest(task):
            continue
        if lane_spec_digest(old) == lane_spec_digest(task):
            # A dependency-only edit is not a rewrite of the lane's brief; the
            # matrix records it with no retroaction instead of refusing.
            continue
        row = rows.get(task.task_id)
        if row is None:
            continue
        if row.owner_done_at is not None:
            raise PlanRunnerError(
                f"lane {task.task_id!r} was completed by the owner and cannot be "
                "rewritten; add a new lane instead"
            )
        if row.job_id:
            job = store.get_job(db, row.job_id)
            if job is not None and job_is_accepted(db, job):
                raise PlanRunnerError(
                    f"lane {task.task_id!r} is accepted and cannot be rewritten; "
                    "add a new lane instead"
                )


def _dependencies_changed_event(
    db: Any,
    plan: PlanRecord,
    task: PlanTaskDef,
    *,
    row: _LaneRow,
    digest: str,
    job: Job,
    now: datetime | None = None,
) -> None:
    """Record a dependency-only edit without treating it as a rewrite."""
    _event(
        db,
        plan_id=plan.plan_id,
        lane_id=task.task_id,
        kind="plan_lane_dependencies_changed",
        event_key=(
            f"plan:{plan.plan_id}:lane:{task.task_id}:deps:"
            f"{plan.active_version}:{digest}"
        ),
        payload={
            "plan_id": plan.plan_id,
            "lane_id": task.task_id,
            "revision": plan.active_version,
            "previous_digest": row.lane_digest,
            "digest": digest,
            "depends_on": list(task.start_dependencies),
            "job_id": job.job_id,
            "job_state": job.state.value,
        },
        now=now,
    )


def _apply_lane_edits(
    db: Any,
    plan: PlanRecord,
    revision: PlanRevision,
    rows: dict[str, _LaneRow],
    *,
    now: datetime | None = None,
) -> tuple[str, ...]:
    """Apply the plan-edit matrix to lanes that still exist (report §6)."""
    touched: list[str] = []
    for task in revision.tasks:
        row = rows.get(task.task_id)
        if row is None:
            continue
        digest = lane_digest(task)
        spec = lane_spec_digest(task)
        if row.lane_digest == digest:
            if row.lane_spec_digest != spec:
                # A row written before P4 has no brief digest yet; the spec is
                # unchanged, so this is a backfill, not an edit.
                _update_lane_row(
                    db,
                    plan_id=plan.plan_id,
                    lane_id=task.task_id,
                    digest=digest,
                    spec_digest=spec,
                    job_id=row.job_id,
                    superseded_by=row.superseded_by,
                    now=now,
                )
            continue
        job = store.get_job(db, row.job_id) if row.job_id else None
        if row.job_id and job is None:
            # The attempt row is unreadable; adopt the new spec rather than
            # waiting forever on a job that is not there.
            _update_lane_row(
                db,
                plan_id=plan.plan_id,
                lane_id=task.task_id,
                digest=digest,
                spec_digest=spec,
                job_id=None,
                superseded_by=None,
                bump_generation=True,
                now=now,
            )
            touched.append(task.task_id)
            continue
        dependencies_only = bool(row.lane_spec_digest) and row.lane_spec_digest == spec
        if job is not None and job_is_accepted(db, job):
            # Acceptance landed between activation and this tick.  Accepted
            # work is a fact: a brief change is never rewritten, and a
            # dependency-only change is recorded with no retroaction.
            if dependencies_only:
                _dependencies_changed_event(
                    db, plan, task, row=row, digest=digest, job=job, now=now
                )
                _update_lane_row(
                    db,
                    plan_id=plan.plan_id,
                    lane_id=task.task_id,
                    digest=digest,
                    spec_digest=spec,
                    job_id=job.job_id,
                    superseded_by=row.superseded_by,
                    now=now,
                )
                touched.append(task.task_id)
            continue
        if job is None:
            _update_lane_row(
                db,
                plan_id=plan.plan_id,
                lane_id=task.task_id,
                digest=digest,
                spec_digest=spec,
                job_id=None,
                superseded_by=None,
                now=now,
            )
            touched.append(task.task_id)
            continue
        if dependencies_only:
            # Edges are never retroactive.  A QUEUED job is cancelled and the
            # lane waits for the new edge to be accepted; a live or finished
            # attempt keeps running under its original edges.
            if job.state == JobState.QUEUED:
                cancelled, current = _cancel_queued_lane_job(db, job)
                if current is not None and current.state == JobState.RUNNING:
                    _dependencies_changed_event(
                        db, plan, task, row=row, digest=digest, job=current, now=now
                    )
                    _update_lane_row(
                        db,
                        plan_id=plan.plan_id,
                        lane_id=task.task_id,
                        digest=digest,
                        spec_digest=spec,
                        job_id=current.job_id,
                        superseded_by=row.superseded_by,
                        now=now,
                    )
                elif cancelled:
                    _dependencies_changed_event(
                        db, plan, task, row=row, digest=digest, job=current or job, now=now
                    )
                    _update_lane_row(
                        db,
                        plan_id=plan.plan_id,
                        lane_id=task.task_id,
                        digest=digest,
                        spec_digest=spec,
                        job_id=None,
                        superseded_by=None,
                        bump_generation=True,
                        now=now,
                    )
                else:  # pragma: no cover - a failed cancel is retried next tick
                    continue
            else:
                _dependencies_changed_event(
                    db, plan, task, row=row, digest=digest, job=job, now=now
                )
                _update_lane_row(
                    db,
                    plan_id=plan.plan_id,
                    lane_id=task.task_id,
                    digest=digest,
                    spec_digest=spec,
                    job_id=job.job_id,
                    superseded_by=row.superseded_by,
                    now=now,
                )
            touched.append(task.task_id)
            continue

        # The brief changed: a running attempt finishes under its own brief.
        if job.state == JobState.QUEUED:
            cancelled, current = _cancel_queued_lane_job(db, job)
            if current is not None and current.state == JobState.RUNNING:
                _supersede_event(
                    db,
                    plan,
                    task.task_id,
                    action="kept_running",
                    previous_digest=row.lane_digest,
                    digest=digest,
                    job=current,
                    now=now,
                )
                _update_lane_row(
                    db,
                    plan_id=plan.plan_id,
                    lane_id=task.task_id,
                    digest=digest,
                    spec_digest=spec,
                    job_id=current.job_id,
                    superseded_by=plan.active_version,
                    now=now,
                )
            elif cancelled:
                _supersede_event(
                    db,
                    plan,
                    task.task_id,
                    action="cancelled_queued",
                    previous_digest=row.lane_digest,
                    digest=digest,
                    job=current or job,
                    now=now,
                )
                _update_lane_row(
                    db,
                    plan_id=plan.plan_id,
                    lane_id=task.task_id,
                    digest=digest,
                    spec_digest=spec,
                    job_id=None,
                    superseded_by=None,
                    bump_generation=True,
                    now=now,
                )
            else:  # pragma: no cover - a failed cancel is retried next tick
                _supersede_event(
                    db,
                    plan,
                    task.task_id,
                    action="cancel_failed",
                    previous_digest=row.lane_digest,
                    digest=digest,
                    job=current or job,
                    now=now,
                )
                _update_lane_row(
                    db,
                    plan_id=plan.plan_id,
                    lane_id=task.task_id,
                    digest=digest,
                    spec_digest=spec,
                    job_id=job.job_id,
                    superseded_by=plan.active_version,
                    now=now,
                )
        elif job.state == JobState.RUNNING:
            _supersede_event(
                db,
                plan,
                task.task_id,
                action="kept_running",
                previous_digest=row.lane_digest,
                digest=digest,
                job=job,
                now=now,
            )
            _update_lane_row(
                db,
                plan_id=plan.plan_id,
                lane_id=task.task_id,
                digest=digest,
                spec_digest=spec,
                job_id=job.job_id,
                superseded_by=plan.active_version,
                now=now,
            )
        elif job.state == JobState.SUCCEEDED:
            _supersede_event(
                db,
                plan,
                task.task_id,
                action="needs_decision",
                previous_digest=row.lane_digest,
                digest=digest,
                job=job,
                now=now,
            )
            _update_lane_row(
                db,
                plan_id=plan.plan_id,
                lane_id=task.task_id,
                digest=digest,
                spec_digest=spec,
                job_id=job.job_id,
                superseded_by=plan.active_version,
                now=now,
            )
        else:  # FAILED or CANCELLED: the new brief needs a fresh attempt
            _supersede_event(
                db,
                plan,
                task.task_id,
                action="retried",
                previous_digest=row.lane_digest,
                digest=digest,
                job=job,
                now=now,
            )
            _update_lane_row(
                db,
                plan_id=plan.plan_id,
                lane_id=task.task_id,
                digest=digest,
                spec_digest=spec,
                job_id=None,
                superseded_by=None,
                bump_generation=True,
                now=now,
            )
        touched.append(task.task_id)
    return tuple(touched)


def _apply_removed_lanes(
    db: Any,
    plan: PlanRecord,
    revision: PlanRevision,
    rows: dict[str, _LaneRow],
    *,
    now: datetime | None = None,
) -> tuple[str, ...]:
    """Retire lanes the active revision no longer contains (report §6)."""
    kept = {task.task_id for task in revision.tasks}
    touched: list[str] = []
    for lane_id, row in rows.items():
        if lane_id in kept:
            continue
        job = store.get_job(db, row.job_id) if row.job_id else None
        if (
            row.superseded_by == plan.active_version
            and (job is None or job.state != JobState.QUEUED)
        ):
            continue
        if job is None:
            if plan.active_version == row.superseded_by:
                continue
            _update_lane_row(
                db,
                plan_id=plan.plan_id,
                lane_id=lane_id,
                digest=row.lane_digest,
                spec_digest=row.lane_spec_digest,
                job_id=None,
                superseded_by=plan.active_version,
                now=now,
            )
            _supersede_event(
                db,
                plan,
                lane_id,
                action="dropped",
                previous_digest=row.lane_digest,
                digest=row.lane_digest,
                job=None,
                now=now,
            )
            touched.append(lane_id)
            continue
        if job_is_accepted(db, job):
            # Accepted lanes are facts; removing one keeps its history intact.
            continue
        if job.state == JobState.QUEUED:
            cancelled, current = _cancel_queued_lane_job(db, job)
            if current is not None and current.state == JobState.RUNNING:
                _supersede_event(
                    db,
                    plan,
                    lane_id,
                    action="kept_running",
                    previous_digest=row.lane_digest,
                    digest=row.lane_digest,
                    job=current,
                    now=now,
                )
                _update_lane_row(
                    db,
                    plan_id=plan.plan_id,
                    lane_id=lane_id,
                    digest=row.lane_digest,
                    spec_digest=row.lane_spec_digest,
                    job_id=current.job_id,
                    superseded_by=plan.active_version,
                    now=now,
                )
            elif cancelled:
                _supersede_event(
                    db,
                    plan,
                    lane_id,
                    action="cancelled_queued",
                    previous_digest=row.lane_digest,
                    digest=row.lane_digest,
                    job=current or job,
                    now=now,
                )
                _update_lane_row(
                    db,
                    plan_id=plan.plan_id,
                    lane_id=lane_id,
                    digest=row.lane_digest,
                    spec_digest=row.lane_spec_digest,
                    job_id=None,
                    superseded_by=plan.active_version,
                    bump_generation=True,
                    now=now,
                )
            else:  # pragma: no cover - a failed cancel is retried next tick
                continue
        elif job.state == JobState.RUNNING:
            _supersede_event(
                db,
                plan,
                lane_id,
                action="kept_running",
                previous_digest=row.lane_digest,
                digest=row.lane_digest,
                job=job,
                now=now,
            )
            _update_lane_row(
                db,
                plan_id=plan.plan_id,
                lane_id=lane_id,
                digest=row.lane_digest,
                spec_digest=row.lane_spec_digest,
                job_id=job.job_id,
                superseded_by=plan.active_version,
                now=now,
            )
        elif job.state == JobState.SUCCEEDED:
            _supersede_event(
                db,
                plan,
                lane_id,
                action="needs_decision",
                previous_digest=row.lane_digest,
                digest=row.lane_digest,
                job=job,
                now=now,
            )
            _update_lane_row(
                db,
                plan_id=plan.plan_id,
                lane_id=lane_id,
                digest=row.lane_digest,
                spec_digest=row.lane_spec_digest,
                job_id=job.job_id,
                superseded_by=plan.active_version,
                now=now,
            )
        else:
            _supersede_event(
                db,
                plan,
                lane_id,
                action="dropped",
                previous_digest=row.lane_digest,
                digest=row.lane_digest,
                job=job,
                now=now,
            )
            _update_lane_row(
                db,
                plan_id=plan.plan_id,
                lane_id=lane_id,
                digest=row.lane_digest,
                spec_digest=row.lane_spec_digest,
                job_id=None,
                superseded_by=plan.active_version,
                bump_generation=True,
                now=now,
            )
        touched.append(lane_id)
    return tuple(touched)


def _recover_superseded(
    db: Any,
    plan: PlanRecord,
    revision: PlanRevision,
    rows: dict[str, _LaneRow],
    *,
    now: datetime | None = None,
) -> tuple[str, ...]:
    """Move a superseded attempt forward once it reaches a terminal state.

    A running attempt is left alone.  A failed or cancelled one frees the lane
    for the new brief on the next submission; a succeeded one waits for the
    owner, who alone decides whether the old result is acceptable.
    """
    touched: list[str] = []
    for task in revision.tasks:
        row = rows.get(task.task_id)
        if row is None or not row.superseded_by or not row.job_id:
            continue
        job = store.get_job(db, row.job_id)
        if job is not None and job.state in (JobState.RUNNING, JobState.SUCCEEDED):
            continue
        if job is not None and job.state == JobState.QUEUED:
            cancelled, current = _cancel_queued_lane_job(db, job)
            if current is not None and current.state == JobState.RUNNING:
                continue
            if not cancelled and current is not None and current.state == JobState.QUEUED:
                continue
            job = current
        _event(
            db,
            plan_id=plan.plan_id,
            lane_id=task.task_id,
            kind="plan_lane_retried",
            event_key=(
                f"plan:{plan.plan_id}:lane:{task.task_id}:retried:"
                f"{plan.active_version}:{lane_digest(task)}"
            ),
            payload={
                "plan_id": plan.plan_id,
                "lane_id": task.task_id,
                "revision": plan.active_version,
                "previous_job_id": row.job_id,
                "previous_job_state": job.state.value if job is not None else None,
            },
            now=now,
        )
        _update_lane_row(
            db,
            plan_id=plan.plan_id,
            lane_id=task.task_id,
            digest=lane_digest(task),
            spec_digest=lane_spec_digest(task),
            job_id=None,
            superseded_by=None,
            bump_generation=True,
            now=now,
        )
        touched.append(task.task_id)
    return tuple(touched)


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
    # P4: apply a mid-run edit before projecting.  The edit rules are
    # idempotent and keyed on the stored digests, so a crash between the
    # revision activation and this step is completed on the next tick.
    revision = plan.plan_store().get_revision(plan.project_id, plan.active_version)
    rows = _lane_rows(db, plan.plan_id)
    _apply_lane_edits(db, plan, revision, rows, now=now)
    rows = _lane_rows(db, plan.plan_id)
    _apply_removed_lanes(db, plan, revision, rows, now=now)
    _recover_superseded(db, plan, revision, rows, now=now)
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
# Landing queue and stale-sibling refresh (P6)
# ---------------------------------------------------------------------------


def _short_error(exc: BaseException) -> str:
    text = " ".join(str(exc).split())
    return text if len(text) <= 400 else text[:397] + "..."


def latest_lane_refresh_event(
    db: Any, plan_id: str, lane_id: str
) -> dict[str, Any] | None:
    """Return the newest refresh outcome recorded for one lane, if any.

    Reads ``plan_events`` only; it never creates a table, so the read-only
    ``plan_status`` projection stays read-only.
    """
    try:
        with _owner_conn(db) as conn:
            row = conn.execute(
                "SELECT kind, payload_json FROM plan_events"
                " WHERE plan_id = ? AND lane_id = ?"
                " AND kind IN ('plan_lane_refreshed', 'plan_lane_refresh_failed')"
                " ORDER BY id DESC LIMIT 1",
                (plan_id, lane_id),
            ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    try:
        payload = json.loads(str(row["payload_json"]))
    except ValueError:
        payload = {}
    return {"kind": str(row["kind"]), **payload}


def lane_refresh_failure(
    db: Any, plan_id: str, lane_id: str, *, run_id: str, target_head: str
) -> str | None:
    """The recorded refresh failure for this exact stale pair, if still current.

    A failure is current only for the same source run and the same target
    commit; a newer successful refresh or a moved target clears it.  This is
    what keeps a failed refresh visible without retrying it every tick.
    """
    event = latest_lane_refresh_event(db, plan_id, lane_id)
    if event is None or event.get("kind") != "plan_lane_refresh_failed":
        return None
    if event.get("source_run_id") != run_id:
        return None
    if event.get("target_base_sha") != target_head:
        return None
    return str(event.get("error") or "refresh failed")


def stale_candidate_lanes(db: Any, plan: PlanRecord) -> tuple[LaneRefresh, ...]:
    """Candidate-ready lanes whose run base differs from the current target.

    Ordered by ``(wave, lane id)``.  An accepted lane, a running lane, a queued
    lane, an owner lane and a scout job are never refresh candidates.
    """
    head = plan_target_head(plan)
    if not head:
        return ()
    try:
        revision = plan.plan_store().get_revision(plan.project_id, plan.active_version)
    except (PlanNotFoundError, PlanTamperError, PlanValidationError) as exc:
        raise PlanRunnerError(
            f"plan {plan.plan_id!r} revision {plan.active_version!r} is unreadable: {exc}"
        ) from exc
    rows = _lane_rows(db, plan.plan_id)
    stale: list[LaneRefresh] = []
    for task in revision.tasks:
        if task.run == "owner":
            continue
        row = rows.get(task.task_id)
        if row is None or not row.job_id:
            continue
        if row.superseded_by:
            # P4: a changed lane's old candidate is an owner decision, never
            # something the refresh pass silently rebuilds.
            continue
        job = store.get_job(db, row.job_id)
        if job is None or job.state is not JobState.SUCCEEDED:
            continue
        if job_is_accepted(db, job):
            continue
        run, _ = lane_run(db, job, accepted=False)
        if run is None or run.status is not RunStatus.COMPLETED:
            continue
        if run.base_commit == head:
            continue
        stale.append(
            LaneRefresh(
                lane_id=task.task_id,
                wave=task.wave or task.phase,
                job_id=job.job_id,
                run_id=run.id,
                base_commit=str(run.base_commit),
            )
        )
    stale.sort(key=lambda lane: (*wave_sort_key(lane.wave), lane.lane_id))
    return tuple(stale)


def _refresh_lane(
    db: Any, plan: PlanRecord, lane: LaneRefresh, *, target_head: str, now: datetime | None
) -> RefreshOutcome:
    """Refresh one stale candidate through E6 and record the outcome."""
    try:
        from saberops.refresh import refresh_candidate
        from saberops.service import OrchestratorService

        service = OrchestratorService(db=run_store(db, lane.run_id))
        result = refresh_candidate(service, lane.run_id)
    except Exception as exc:
        message = _short_error(exc)
        _event(
            db,
            plan_id=plan.plan_id,
            lane_id=lane.lane_id,
            kind="plan_lane_refresh_failed",
            event_key=(
                f"plan:{plan.plan_id}:lane:{lane.lane_id}:refresh:"
                f"{lane.run_id}:{target_head}:failed:"
                f"{hashlib.sha256(message.encode()).hexdigest()[:12]}"
            ),
            payload={
                "lane_id": lane.lane_id,
                "source_run_id": lane.run_id,
                "source_base_sha": lane.base_commit,
                "target_base_sha": target_head,
                "error": message,
            },
            now=now,
        )
        return RefreshOutcome(
            lane_id=lane.lane_id,
            source_run_id=lane.run_id,
            target_base_sha=target_head,
            error=message,
        )
    refreshed_id = str(result.get("run_id") or "") or None
    candidate_sha = str(result.get("candidate_sha") or "") or None
    _event(
        db,
        plan_id=plan.plan_id,
        lane_id=lane.lane_id,
        kind="plan_lane_refreshed",
        event_key=(
            f"plan:{plan.plan_id}:lane:{lane.lane_id}:refresh:"
            f"{lane.run_id}:{target_head}:refreshed:{refreshed_id or 'unknown'}"
        ),
        payload={
            "lane_id": lane.lane_id,
            "source_run_id": lane.run_id,
            "refreshed_run_id": refreshed_id,
            "source_base_sha": lane.base_commit,
            "target_base_sha": target_head,
            "candidate_sha": candidate_sha,
        },
        now=now,
    )
    return RefreshOutcome(
        lane_id=lane.lane_id,
        source_run_id=lane.run_id,
        target_base_sha=target_head,
        refreshed_run_id=refreshed_id,
        candidate_sha=candidate_sha,
    )


def _refresh_plan(
    db: Any, plan: PlanRecord, *, now: datetime | None
) -> PlanRefreshResult:
    refusal = _submission_refusal(db, plan=plan, owner_initiated=False)
    if refusal is not None:
        return PlanRefreshResult(plan.plan_id, stopped=f"paused:{refusal}")
    stale = stale_candidate_lanes(db, plan)
    if not stale:
        return PlanRefreshResult(plan.plan_id)
    head = plan_target_head(plan)
    if not head:  # pragma: no cover - stale_candidate_lanes already proved a head
        return PlanRefreshResult(plan.plan_id, stopped="target_unreadable")
    outcomes: list[RefreshOutcome] = []
    for lane in stale[:MAX_REFRESHES_PER_TICK]:
        failure = lane_refresh_failure(
            db, plan.plan_id, lane.lane_id, run_id=lane.run_id, target_head=head
        )
        if failure is not None:
            # The queue stops at the first failed refresh; the lane shows the
            # failure and the exact manual command until the pair changes.
            outcomes.append(
                RefreshOutcome(
                    lane_id=lane.lane_id,
                    source_run_id=lane.run_id,
                    target_base_sha=head,
                    error=failure,
                )
            )
            return PlanRefreshResult(
                plan.plan_id, outcomes=tuple(outcomes), stopped="refresh_failed"
            )
        outcome = _refresh_lane(db, plan, lane, target_head=head, now=now)
        outcomes.append(outcome)
        if outcome.error is not None:
            return PlanRefreshResult(
                plan.plan_id, outcomes=tuple(outcomes), stopped="refresh_failed"
            )
    return PlanRefreshResult(
        plan.plan_id,
        outcomes=tuple(outcomes),
        stopped=(
            "refresh_limit" if len(stale) > MAX_REFRESHES_PER_TICK else None
        ),
    )


def refresh_stale_plan_candidates(
    db: Any, *, now: datetime | None = None
) -> tuple[PlanRefreshResult, ...]:
    """Refresh stale candidate-ready siblings in landing order (P6).

    Runs only for plans whose owner turned ``auto_refresh`` on, only while work
    is not paused (the same gate and planning-session check lane submission
    uses), and at most :data:`MAX_REFRESHES_PER_TICK` lane per tick.  It reuses
    :func:`saberops.refresh.refresh_candidate`, which re-gates and re-reviews
    the new candidate in isolation; it never accepts and never pushes.  A
    failed refresh records ``plan_lane_refresh_failed`` and stops the queue.
    """
    ensure_plan_tables(db)
    results: list[PlanRefreshResult] = []
    for plan in list_active_plans(db):
        if not plan.auto_refresh:
            continue
        try:
            results.append(_refresh_plan(db, plan, now=now))
        except Exception:
            logging.getLogger(__name__).exception(
                "plan %s landing refresh failed", plan.plan_id
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
