"""Self-healing doctor: check, repair only fixable findings, recheck decides.

Every finding is labelled exactly one of:

``ok``
    The check passed.
``skip``
    The check does not apply on this operating system.
``fixable``
    SaberOps can repair its own state; ``--fix`` (or the ``doctor_fix`` MCP
    tool) attempts it.
``human``
    The owner must act; the finding carries the exact steps.

A repair never decides its own success.  :func:`run_doctor` records the
attempt, runs every check again, and reports the *recheck* result.  A repair
that did not work therefore leaves the same ``fixable`` finding in place.

Repairs only touch things SaberOps owns: its XDG state/config/cache
directories, its database schema and indexes, a stale planning-lease record
whose process is dead, runs whose execution owner is dead (through the
engine's own supervisor reconciliation), a scheduler that stopped while jobs
are queued, and legacy SaberOps-namespace directories (removed only after the
finding has previewed their paths and size).  Vendor logins, API keys, owner
settings, the user's repositories and system packages are never repaired.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import platform
import shutil
import sqlite3
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from saberops.mcp.lease import LeaseRecord, default_lease_path, pid_is_alive
from saberops.paths import (
    saberops_cache_dir,
    saberops_config_dir,
    saberops_state_dir,
)
from saberops.project import projects_state_root

__all__ = [
    "CHECK_STATUSES",
    "DEFAULT_CHECK_TIMEOUT",
    "DoctorFinding",
    "DoctorReport",
    "RepairContext",
    "RepairOutcome",
    "collect_all",
    "collect_doctor_checks",
    "detect_platform",
    "legacy_state_dirs",
    "run_doctor",
]

_LOG = logging.getLogger(__name__)

#: Every finding state, in reporting order.
CHECK_STATUSES: tuple[str, ...] = ("ok", "skip", "fixable", "human")

#: Upper bound for one check.  A check that exceeds it is reported ``human``
#: with the reason, never left to block the doctor.
DEFAULT_CHECK_TIMEOUT = 10.0

#: Indexes whose absence makes ordinary run queries scan whole tables.  Only
#: the hot-path set is checked; a missing index rebuilds through the normal
#: schema bootstrap.
REQUIRED_INDEXES: tuple[str, ...] = (
    "idx_attempts_run_id",
    "idx_checks_attempt_id",
    "idx_reviews_run_id",
    "idx_events_run_id",
)


@dataclass(frozen=True)
class DoctorFinding:
    """One doctor check outcome.

    ``severity`` keeps the read-only CLI's original OK/WARN/FAIL vocabulary.
    ``state`` is the repair contract: ``ok``, ``skip``, ``fixable`` or
    ``human``.
    """

    severity: str
    message: str
    check_id: str = ""
    status: str = ""
    steps: str = ""
    action: str = ""

    @property
    def state(self) -> str:
        """The repair label; derived from severity for legacy findings."""
        if self.status in CHECK_STATUSES:
            return self.status
        return {"OK": "ok", "WARN": "human", "FAIL": "human"}.get(
            self.severity, "human"
        )

    @property
    def fixable(self) -> bool:
        return self.state == "fixable"

    def as_dict(self) -> dict[str, str]:
        return {
            "check_id": self.check_id,
            "state": self.state,
            "severity": self.severity,
            "message": self.message,
            "steps": self.steps,
            "action": self.action,
        }

    @classmethod
    def passed(cls, check_id: str, message: str) -> DoctorFinding:
        return cls("OK", message, check_id=check_id, status="ok")

    @classmethod
    def not_applicable(cls, check_id: str, message: str) -> DoctorFinding:
        return cls("OK", message, check_id=check_id, status="skip")

    @classmethod
    def repairable(cls, check_id: str, message: str, *, action: str) -> DoctorFinding:
        return cls("WARN", message, check_id=check_id, status="fixable", action=action)

    @classmethod
    def human(
        cls,
        check_id: str,
        message: str,
        *,
        steps: str,
        severity: str = "WARN",
    ) -> DoctorFinding:
        return cls(severity, message, check_id=check_id, status="human", steps=steps)


@dataclass(frozen=True)
class RepairContext:
    """The paths one repair may touch."""

    repo: Path
    db_path: Path
    #: Legacy state is preview-only unless the owner explicitly asks for its
    #: deletion (``orch doctor --fix --delete-legacy``).
    allow_legacy_delete: bool = False


@dataclass(frozen=True)
class RepairOutcome:
    """What one repair attempt did.  The recheck, not this, decides."""

    applied: bool
    detail: str


@dataclass(frozen=True)
class RepairAction:
    """One recorded repair attempt and the recheck that followed it."""

    check_id: str
    applied: bool
    detail: str
    before: str
    after: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "applied": self.applied,
            "detail": self.detail,
            "before": self.before,
            "after": self.after,
        }


@dataclass(frozen=True)
class DoctorReport:
    """One check or fix pass: findings plus any repair attempts."""

    findings: tuple[DoctorFinding, ...]
    actions: tuple[RepairAction, ...] = ()
    fix_requested: bool = False

    def counts(self) -> dict[str, int]:
        counts = dict.fromkeys(CHECK_STATUSES, 0)
        for finding in self.findings:
            counts[finding.state] += 1
        return counts

    @property
    def remaining_fixable(self) -> tuple[DoctorFinding, ...]:
        return tuple(finding for finding in self.findings if finding.fixable)

    def as_dict(self) -> dict[str, Any]:
        return {
            "fix_requested": self.fix_requested,
            "counts": self.counts(),
            "findings": [finding.as_dict() for finding in self.findings],
            "actions": [action.as_dict() for action in self.actions],
        }


# --------------------------------------------------------------------------- #
# Platform and small seams (tests inject at these names)
# --------------------------------------------------------------------------- #


def _proc_version_text() -> str:
    """The Linux kernel banner used only to distinguish WSL (test seam)."""
    try:
        return Path("/proc/version").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def detect_platform() -> str:
    """Return ``linux``, ``wsl``, ``macos`` or ``other``.

    WSL reports Linux to :mod:`platform`; the kernel string distinguishes it.
    """
    system = platform.system()
    if system == "Darwin":
        return "macos"
    if system == "Linux":
        return "wsl" if "microsoft" in _proc_version_text().lower() else "linux"
    return "other"


def _home_dir() -> Path:
    """Owner home, resolved per call so a test can point it at scratch."""
    return Path.home()


def legacy_state_dirs() -> tuple[Path, ...]:
    """Known private-development SaberOps namespaces under the owner home.

    They are never a default and SaberOps never adopts them; the doctor only
    previews them and, on explicit ``--fix``, removes them.
    """
    home = _home_dir()
    names = ("orchestrator-mvp", "orchestrator-v2")
    roots: list[Path] = []
    for base in (home / ".local" / "state", home / ".config", home / ".cache"):
        roots.extend(base / name for name in names)
    return tuple(roots)


def _owner_path_holder(db_path: Path) -> Any:
    """A path-only stand-in for :class:`Database` (never opens or migrates)."""

    class _Holder:
        def __init__(self, path: Path) -> None:
            self.db_path = path

        @property
        def owner_db(self) -> _Holder:
            return self

    return _Holder(db_path)


def _scheduler_alive(db_path: Path) -> bool:
    from saberops.capacity.loop import scheduler_alive

    return bool(scheduler_alive(_owner_path_holder(db_path)))


def _start_scheduler(db_path: Path) -> bool:
    """Start SaberOps' own scheduler fully detached (stdin closed, new session)."""
    from saberops.capacity.loop import ensure_scheduler_running

    return bool(
        ensure_scheduler_running(_owner_path_holder(db_path), respect_disable=False)
    )


def _reconcile_dead_runs(db_path: Path, run_ids: Sequence[str]) -> list[str]:
    """Orphan dead-owner runs through the engine's own supervisor."""
    from saberops.db import Database
    from saberops.supervisor import RunSupervisor

    db = Database(db_path)
    supervisor = RunSupervisor(db)
    done: list[str] = []
    for run_id in run_ids:
        supervisor.reconcile(run_id, orphan_recoverable=True)
        done.append(run_id)
    return done


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #


def _read_connection(db_path: Path) -> sqlite3.Connection:
    uri = db_path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _check_portability(ctx: RepairContext) -> list[DoctorFinding]:
    found = detect_platform()
    if found == "other":
        return [
            DoctorFinding.not_applicable(
                "portability",
                f"{platform.system() or 'this OS'} is not a supported SaberOps host "
                "(supported: Linux, macOS, WSL)",
            )
        ]
    return [DoctorFinding.passed("portability", f"running on supported {found}")]


def _state_directories() -> tuple[Path, ...]:
    return (
        saberops_state_dir(),
        saberops_config_dir(),
        saberops_cache_dir(),
        projects_state_root(),
    )


def _check_state_dirs(ctx: RepairContext) -> list[DoctorFinding]:
    missing = [path for path in _state_directories() if not path.is_dir()]
    if missing:
        return [
            DoctorFinding.repairable(
                "state_dirs",
                "missing SaberOps directories: " + ", ".join(str(path) for path in missing),
                action="create the missing XDG state, config and cache directories",
            )
        ]
    blocked = [
        path for path in _state_directories() if not os.access(path, os.W_OK | os.X_OK)
    ]
    if blocked:
        return [
            DoctorFinding.human(
                "state_dirs",
                "SaberOps directories are not writable: "
                + ", ".join(str(path) for path in blocked),
                steps=(
                    "Give your own user write access, for example: chmod u+rwx "
                    + " ".join(str(path) for path in blocked)
                ),
                severity="FAIL",
            )
        ]
    return [
        DoctorFinding.passed(
            "state_dirs", "SaberOps state, config and cache directories are writable"
        )
    ]


def _check_database(ctx: RepairContext) -> list[DoctorFinding]:
    """Use the one database check the CLI collector also calls."""
    from saberops.cli import _check_database as _cli_check_database

    finding, _quota_rows = _cli_check_database(ctx.db_path)
    return [finding]


def _check_planner_lease(ctx: RepairContext) -> list[DoctorFinding]:
    from saberops.mcp.lease import OrchestratorLease

    path = default_lease_path()
    if not path.exists():
        return [DoctorFinding.passed("planner_lease", "no planning lease file")]
    try:
        if path.stat().st_size == 0:
            return [DoctorFinding.passed("planner_lease", "planning lease is free")]
    except OSError as exc:
        return [
            DoctorFinding.human(
                "planner_lease",
                f"planning lease at {path} is unreadable: {exc}",
                steps=f"Check permissions on {path}; the lease is recreated on demand.",
            )
        ]
    lease = OrchestratorLease(path)
    record = lease.read()
    if record is None:
        return [
            DoctorFinding.repairable(
                "planner_lease",
                f"planning lease at {path} holds a malformed record",
                action="clear the malformed lease record",
            )
        ]
    if pid_is_alive(record.pid):
        return [
            DoctorFinding.passed(
                "planner_lease",
                f"planning lease held by live pid {record.pid}",
            )
        ]
    active = OrchestratorLease.active_work(record)
    if active:
        return [
            DoctorFinding.human(
                "planner_lease",
                f"planning lease pid {record.pid} is dead but still owns "
                f"{active} running job(s)",
                steps=(
                    "Resolve the running jobs first (doctor repairs orphaned runs "
                    "when their owner is provably dead), then run `orch doctor` "
                    "again to clear the lease."
                ),
                severity="FAIL",
            )
        ]
    return [
        DoctorFinding.repairable(
            "planner_lease",
            f"planning lease pid {record.pid} is dead",
            action="clear the stale planning lease record",
        )
    ]


def _dead_owner_runs(db_path: Path) -> list[str]:
    if not db_path.exists():
        return []
    try:
        with _read_connection(db_path) as conn:
            if not _table_exists(conn, "runs") or not _table_exists(
                conn, "execution_owners"
            ):
                return []
            rows = conn.execute(
                """
                SELECT r.id AS run_id, o.pid AS pid
                FROM runs r
                JOIN execution_owners o ON o.run_id = r.id
                WHERE r.status = 'RUNNING'
                  AND o.released_at IS NULL
                  AND o.state != 'RELEASED'
                """
            ).fetchall()
    except sqlite3.Error:
        return []
    return [
        str(row["run_id"])
        for row in rows
        if row["pid"] is not None and not pid_is_alive(int(row["pid"]))
    ]


def _check_orphaned_runs(ctx: RepairContext) -> list[DoctorFinding]:
    run_ids = _dead_owner_runs(ctx.db_path)
    if not run_ids:
        return [DoctorFinding.passed("orphaned_runs", "no run has a dead execution owner")]
    return [
        DoctorFinding.repairable(
            "orphaned_runs",
            f"{len(run_ids)} running run(s) have a dead execution owner: "
            + ", ".join(run_ids[:5])
            + (" ..." if len(run_ids) > 5 else ""),
            action="orphan them through the run supervisor's own reconciliation",
        )
    ]


def _queued_jobs(db_path: Path) -> int:
    if not db_path.exists():
        return 0
    try:
        with _read_connection(db_path) as conn:
            if not _table_exists(conn, "jobs"):
                return 0
            row = conn.execute(
                "SELECT count(*) FROM jobs WHERE state = 'QUEUED'"
            ).fetchone()
            return int(row[0])
    except sqlite3.Error:
        return 0


def _check_scheduler(ctx: RepairContext) -> list[DoctorFinding]:
    queued = _queued_jobs(ctx.db_path)
    if queued == 0:
        return [DoctorFinding.passed("scheduler", "no queued jobs need the scheduler")]
    if _scheduler_alive(ctx.db_path):
        return [
            DoctorFinding.passed(
                "scheduler", f"scheduler is running while {queued} job(s) are queued"
            )
        ]
    return [
        DoctorFinding.repairable(
            "scheduler",
            f"{queued} job(s) are queued but no scheduler is running",
            action="start the scheduler fully detached (stdin closed, new session)",
        )
    ]


def _du_usage(path: Path) -> tuple[int, int]:
    files = 0
    size = 0
    for root, _dirs, names in os.walk(path):
        for name in names:
            files += 1
            try:
                size += (Path(root) / name).stat().st_size
            except OSError:
                continue
            if files >= 1000:
                return files, size
    return files, size


def _check_legacy_state(ctx: RepairContext) -> list[DoctorFinding]:
    present = [path for path in legacy_state_dirs() if path.exists()]
    if not present:
        return [DoctorFinding.passed("legacy_state", "no legacy SaberOps state directories")]
    total = 0
    snippets: list[str] = []
    for path in present:
        files, size = _du_usage(path)
        total += size
        snippets.append(f"{path} ({files} file(s), {size} byte(s))")
    return [
        DoctorFinding.repairable(
            "legacy_state",
            "legacy SaberOps state found: " + "; ".join(snippets),
            action=(
                f"delete {len(present)} legacy directory tree(s), "
                f"{total} byte(s); nothing else is touched"
            ),
        )
    ]


#: Every check that is not part of the original read-only CLI collector.
_CHECKS: tuple[tuple[str, Callable[[RepairContext], list[DoctorFinding]]], ...] = (
    ("portability", _check_portability),
    ("state_dirs", _check_state_dirs),
    ("database", _check_database),
    ("planner_lease", _check_planner_lease),
    ("orphaned_runs", _check_orphaned_runs),
    ("scheduler", _check_scheduler),
    ("legacy_state", _check_legacy_state),
)


def _run_bounded(
    check_id: str,
    fn: Callable[[RepairContext], list[DoctorFinding]],
    ctx: RepairContext,
    timeout: float,
) -> list[DoctorFinding]:
    box: dict[str, list[DoctorFinding]] = {}

    def target() -> None:
        try:
            box["value"] = fn(ctx)
        except Exception as exc:  # noqa: BLE001 - one failed check never hides the rest
            box["error"] = [
                DoctorFinding.human(
                    check_id,
                    f"check {check_id} failed: {exc}",
                    steps="Run `orch doctor` again; if it repeats, report this check's message.",
                )
            ]

    thread = threading.Thread(target=target, name=f"doctor-{check_id}", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        return [
            DoctorFinding.human(
                check_id,
                f"check {check_id} did not finish within {timeout:g}s",
                steps="A check is hanging; inspect the named area by hand, then retry.",
            )
        ]
    if "error" in box:
        return box["error"]
    return box.get("value", [])


def collect_doctor_checks(
    repo: Path,
    db_path: Path,
    *,
    timeout: float = DEFAULT_CHECK_TIMEOUT,
) -> list[DoctorFinding]:
    """Run the doctor-owned checks; each one is time-bounded."""
    ctx = RepairContext(repo=repo, db_path=db_path)
    findings: list[DoctorFinding] = []
    for check_id, fn in _CHECKS:
        findings.extend(_run_bounded(check_id, fn, ctx, timeout))
    return findings


def collect_all(
    repo: Path,
    db_path: Path,
    *,
    timeout: float = DEFAULT_CHECK_TIMEOUT,
) -> list[DoctorFinding]:
    """Every finding the CLI, the web page and the MCP tool report."""
    from saberops.cli import collect_doctor_findings

    ctx = RepairContext(repo=repo, db_path=db_path)
    legacy = _run_bounded(
        "legacy_checks",
        lambda _ctx: collect_doctor_findings(repo, db_path),
        ctx,
        timeout,
    )
    return legacy + collect_doctor_checks(repo, db_path, timeout=timeout)


# --------------------------------------------------------------------------- #
# Repairs
# --------------------------------------------------------------------------- #


def _repair_state_dirs(ctx: RepairContext) -> RepairOutcome:
    created: list[str] = []
    for path in _state_directories():
        if not path.is_dir():
            path.mkdir(parents=True, exist_ok=True)
            created.append(str(path))
    return RepairOutcome(
        applied=True,
        detail="created " + (", ".join(created) if created else "nothing (already present)"),
    )


def _repair_database(ctx: RepairContext) -> RepairOutcome:
    from saberops.db import Database

    if not ctx.db_path.exists() or ctx.db_path.stat().st_size == 0:
        return RepairOutcome(applied=False, detail="no store yet; nothing to migrate")
    db = Database(ctx.db_path)
    with sqlite3.connect(str(db.db_path), timeout=5.0) as conn:
        conn.execute("REINDEX")
        conn.commit()
    return RepairOutcome(applied=True, detail="ran additive migrations and rebuilt indexes")


def _repair_planner_lease(ctx: RepairContext) -> RepairOutcome:
    path = default_lease_path()
    try:
        with path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                handle.seek(0)
                raw = handle.read().strip()
                if not raw:
                    return RepairOutcome(applied=False, detail="lease already free")
                try:
                    record: LeaseRecord | None = LeaseRecord.from_mapping(json.loads(raw))
                except (ValueError, TypeError, KeyError):
                    record = None
                if record is not None and pid_is_alive(record.pid):
                    return RepairOutcome(
                        applied=False,
                        detail=f"lease moved to live pid {record.pid}; left alone",
                    )
                handle.seek(0)
                handle.truncate()
                handle.flush()
                os.fsync(handle.fileno())
                return RepairOutcome(applied=True, detail="cleared the stale lease record")
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
    except OSError as exc:
        return RepairOutcome(applied=False, detail=f"could not clear the lease: {exc}")


def _repair_orphaned_runs(ctx: RepairContext) -> RepairOutcome:
    run_ids = _dead_owner_runs(ctx.db_path)
    if not run_ids:
        return RepairOutcome(applied=False, detail="no dead-owner run remains")
    done = _reconcile_dead_runs(ctx.db_path, run_ids)
    return RepairOutcome(
        applied=True,
        detail="asked the supervisor to reconcile: " + ", ".join(done),
    )


def _repair_scheduler(ctx: RepairContext) -> RepairOutcome:
    started = _start_scheduler(ctx.db_path)
    return RepairOutcome(
        applied=started,
        detail="started a detached scheduler" if started else "scheduler already running",
    )


def _repair_legacy_state(ctx: RepairContext) -> RepairOutcome:
    if not ctx.allow_legacy_delete:
        return RepairOutcome(
            applied=False,
            detail=(
                "preview only: legacy state needs an explicit deletion request "
                "(`orch doctor --fix --delete-legacy`)"
            ),
        )
    removed: list[str] = []
    for path in legacy_state_dirs():
        if path.exists():
            shutil.rmtree(path)
            removed.append(str(path))
    return RepairOutcome(
        applied=bool(removed),
        detail="removed " + (", ".join(removed) if removed else "nothing (already absent)"),
    )


_REPAIRS: dict[str, Callable[[RepairContext], RepairOutcome]] = {
    "state_dirs": _repair_state_dirs,
    "database": _repair_database,
    "planner_lease": _repair_planner_lease,
    "orphaned_runs": _repair_orphaned_runs,
    "scheduler": _repair_scheduler,
    "legacy_state": _repair_legacy_state,
}


def _action_log_path() -> Path:
    return saberops_cache_dir() / "doctor-actions.jsonl"


def _log_action(action: RepairAction) -> None:
    """Append one repair record; logging never changes the repair result."""
    line = json.dumps(
        {
            "at": datetime.now(UTC).isoformat(),
            **action.as_dict(),
        },
        sort_keys=True,
    )
    try:
        path = _action_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError as exc:  # pragma: no cover - a logging fault is not a repair fault
        _LOG.warning("doctor action log unavailable: %s", exc)


def run_doctor(
    repo: Path,
    db_path: Path,
    *,
    fix: bool = False,
    only: Sequence[str] | None = None,
    allow_legacy_delete: bool = False,
    checks: Callable[[], list[DoctorFinding]] | None = None,
    repairs: Mapping[str, Callable[[RepairContext], RepairOutcome]] | None = None,
    timeout: float = DEFAULT_CHECK_TIMEOUT,
) -> DoctorReport:
    """Check, optionally repair the fixable findings, then recheck.

    The recheck alone decides: an ``applied`` repair result cannot clear a
    finding.  ``only`` limits which fixable check ids are attempted.
    """
    check_fn = checks or (lambda: collect_all(repo, db_path, timeout=timeout))
    before = tuple(check_fn())
    if not fix:
        return DoctorReport(findings=before, fix_requested=False)

    ctx = RepairContext(
        repo=repo, db_path=db_path, allow_legacy_delete=allow_legacy_delete
    )
    table = dict(_REPAIRS)
    if repairs:
        table.update(repairs)
    selected = [
        finding
        for finding in before
        if finding.fixable and (only is None or finding.check_id in set(only))
    ]
    actions: list[RepairAction] = []
    seen: set[str] = set()
    for finding in selected:
        if not finding.check_id or finding.check_id in seen:
            continue
        seen.add(finding.check_id)
        repair = table.get(finding.check_id)
        if repair is None:
            continue
        try:
            outcome = repair(ctx)
        except Exception as exc:  # noqa: BLE001 - a failed repair is reported, not raised
            outcome = RepairOutcome(applied=False, detail=f"repair raised: {exc}")
        actions.append(
            RepairAction(
                check_id=finding.check_id,
                applied=outcome.applied,
                detail=outcome.detail,
                before=finding.state,
                after=None,
            )
        )

    after = tuple(check_fn())
    if actions:
        by_id = {finding.check_id: finding.state for finding in after}
        actions = [
            RepairAction(
                check_id=action.check_id,
                applied=action.applied,
                detail=action.detail,
                before=action.before,
                after=by_id.get(action.check_id),
            )
            for action in actions
        ]
    for action in actions:
        _log_action(action)
    return DoctorReport(
        findings=after, actions=tuple(actions), fix_requested=True
    )
