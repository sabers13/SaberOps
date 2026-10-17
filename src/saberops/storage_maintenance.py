"""Bounded storage maintenance service for the GS-1 lifecycle (Step 008).

The service is invoked on scheduler startup, hourly and whenever free space
drops below the current floor.  It is deliberately bounded and lease-aware:

* it never monopolises the scheduler tick or an owner lock -- it takes a short
  wall-clock budget and reports the remainder;
* it keeps today's fixed free-space floor on every production path.  The
  adaptive admission policy of ADR-0003 Amendment 2 is **not** activated here
  and this service never claims that it is: :func:`current_disk_floor_bytes`
  reimplements the existing ``max(min_budget, 10 %)`` guard verbatim as a
  *consumer* of it, so no live guard is lowered or bypassed;
* without a validated scoped owner standing policy it inventories and reports
  only -- no destructive automatic GC;
* a DISK_LOW trigger runs bounded permitted GC and then **rechecks the actual
  filesystem**; if space is still low or unknown the result is a typed
  ``DISK_LOW`` infrastructure park and no worker or gate is launched.

It never dispatches an LLM repair, fabricates a PASS, kills an active writer or
converts an infrastructure event into a code/test failure.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Final

from saberops.db import Database
from saberops.paths import saberops_state_dir
from saberops.sqlite_policy import StorageInfraError, free_page_ratio
from saberops.storage_gc import (
    AuditSink,
    GcApplyResult,
    StorageAuditEvent,
    apply_gc,
    load_gc_policy,
    preview_gc,
)
from saberops.storage_inventory import ArtifactClass, Inventory, build_inventory

__all__ = [
    "DEFAULT_DISK_FLOOR_GB",
    "MAINTENANCE_INTERVAL",
    "OWNER_DB_VACUUM_INTERVAL",
    "DiskAssessment",
    "DiskLowInfra",
    "DiskState",
    "MaintenanceResult",
    "MaintenanceTrigger",
    "StorageMaintenance",
    "current_disk_floor_bytes",
    "database_audit_sink",
    "disk_ok",
]

GB: Final[int] = 1024**3
DEFAULT_DISK_FLOOR_GB: Final[float] = 10.0
MAINTENANCE_INTERVAL: Final[timedelta] = timedelta(hours=1)
OWNER_DB_VACUUM_INTERVAL: Final[timedelta] = timedelta(days=7)
OWNER_DB_VACUUM_FREE_PAGE_RATIO: Final[float] = 0.20
DEFAULT_TICK_BUDGET_SECONDS: Final[float] = 20.0


class DiskLowInfra(StorageInfraError):
    """Free space is below the floor (or unknown) after a bounded recheck."""

    reason_code = "DISK_LOW"


class DiskState(StrEnum):
    OK = "OK"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"


class MaintenanceTrigger(StrEnum):
    STARTUP = "startup"
    PERIODIC = "periodic"
    DISK_LOW = "disk-low"
    EXPLICIT = "explicit"


@dataclass(frozen=True)
class DiskAssessment:
    """Result of comparing live free space against the current fixed floor."""

    state: DiskState
    free_bytes: int | None
    required_bytes: int | None
    paths: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class MaintenanceResult:
    """Bounded telemetry for one maintenance invocation."""

    trigger: MaintenanceTrigger
    ran_gc: bool
    disk_before: DiskAssessment
    disk_after: DiskAssessment | None
    candidates: int
    applied: int
    refused: int
    infra_reason: str | None
    vacuumed: bool
    elapsed_seconds: float
    telemetry: Mapping[str, str] = field(default_factory=dict)


def current_disk_floor_bytes(total_bytes: int, minimum_gb: float = DEFAULT_DISK_FLOOR_GB) -> int:
    """Return the existing fixed free-space floor for one filesystem.

    This is the ADR-0003 D4/Q1 ``max(min_gb, 10 %)`` guard reimplemented as a
    read-only consumer so the storage service and the developer gate can never
    disagree about the effective floor.  It is **not** the adaptive replacement
    of Amendment 2 and it never returns a smaller reserve than the live guard.
    """
    return int(max(minimum_gb * GB, total_bytes * 0.1))


def disk_ok(
    paths: Sequence[Path | str], minimum_gb: float = DEFAULT_DISK_FLOOR_GB
) -> bool:
    """Return True when every path's filesystem is above today's fixed floor."""
    for path in paths:
        try:
            usage = _statvfs(path)
        except OSError:
            return False
        if usage[0] < current_disk_floor_bytes(usage[1], minimum_gb):
            return False
    return True


def _statvfs(path: Path | str) -> tuple[int, int]:
    """Return ``(free_bytes, total_bytes)`` for the filesystem holding *path*."""
    import os

    stat = os.statvfs(str(path))
    return stat.f_bavail * stat.f_frsize, stat.f_blocks * stat.f_frsize


class StorageMaintenance:
    """Bounded, lease-aware storage maintenance over one repository."""

    def __init__(
        self,
        db: Database,
        *,
        repo_root: Path | str,
        policy_path: Path | None = None,
        worktree_base: Path | str | None = None,
        state_root: Path | str | None = None,
        floor_gb: float = DEFAULT_DISK_FLOOR_GB,
        tick_budget_seconds: float = DEFAULT_TICK_BUDGET_SECONDS,
        audit: AuditSink | None = None,
        clock: Callable[[], float] = time.monotonic,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._db = db
        self._repo_root = Path(repo_root).resolve()
        self._policy_path = policy_path
        self._worktree_base = Path(worktree_base).resolve() if worktree_base else None
        self._state_root = Path(state_root).resolve() if state_root else saberops_state_dir()
        self._floor_gb = floor_gb
        self._tick_budget = tick_budget_seconds
        self._audit = audit if audit is not None else database_audit_sink(db)
        self._clock = clock
        self._now_fn = now_fn if now_fn is not None else (lambda: datetime.now(UTC))

    # -- guard adapter -----------------------------------------------------

    def assess_disk(self, paths: Sequence[Path | str]) -> DiskAssessment:
        """Compare live free space against the current fixed floor."""
        resolved = tuple(str(Path(p)) for p in paths)
        free: int | None = None
        required: int | None = None
        for path in paths:
            try:
                free_bytes, total_bytes = _statvfs(path)
            except OSError:
                return DiskAssessment(DiskState.UNKNOWN, None, None, resolved, "statvfs-failed")
            needed = current_disk_floor_bytes(total_bytes, self._floor_gb)
            if free is None or free_bytes < free:
                free = free_bytes
            if required is None or needed > required:
                required = needed
        assert free is not None and required is not None
        if free < required:
            return DiskAssessment(DiskState.LOW, free, required, resolved, "below-floor")
        return DiskAssessment(DiskState.OK, free, required, resolved, "above-floor")

    def storage_paths(self) -> tuple[str, ...]:
        """Paths whose filesystem the engine cares about for this project."""
        return (str(self._repo_root), str(self._state_root))

    # -- inventory / GC ----------------------------------------------------

    def inventory(self) -> Inventory:
        return build_inventory(
            self._db,
            repo_root=self._repo_root,
            now=self._now_fn(),
            worktree_base=self._worktree_base,
            state_root=self._state_root,
        )

    def run(
        self,
        trigger: MaintenanceTrigger,
        *,
        paths: Sequence[Path | str] | None = None,
        lease_valid: bool = True,
    ) -> MaintenanceResult:
        """Run one bounded maintenance pass.

        ``lease_valid=False`` (the scheduler lease is held elsewhere) makes the
        pass report-only: it performs no destructive action.
        """
        started = self._clock()
        if not isinstance(trigger, MaintenanceTrigger):
            trigger = MaintenanceTrigger(trigger)
        target_paths = tuple(paths) if paths is not None else self.storage_paths()
        disk_before = self.assess_disk(target_paths)
        policy = load_gc_policy(self._policy_path)
        inventory = self.inventory()
        preview = preview_gc(
            inventory,
            policy=policy,
            policy_path=self._policy_path,
            classes=(ArtifactClass.ATTEMPT_WORKTREE, ArtifactClass.LOCAL_BRANCH),
        )
        applied = 0
        refused = 0
        ran_gc = False
        vacuumed = False
        within_budget = (self._clock() - started) < self._tick_budget

        if lease_valid and policy.approved and preview.candidates and within_budget:
            result: GcApplyResult = apply_gc(
                preview,
                preview.candidates,
                db=self._db,
                repo_root=self._repo_root,
                audit=self._audit,
                now=self._now_fn(),
            )
            applied = len(result.applied)
            refused = len(result.refused)
            ran_gc = applied > 0

        if lease_valid and policy.approved and policy.allow_vacuum:
            vacuumed = self._maybe_vacuum()

        swept = self._sweep_gate_scratch()
        infra_reason: str | None = None
        disk_after: DiskAssessment | None = None
        if trigger is MaintenanceTrigger.DISK_LOW:
            if disk_before.state is DiskState.OK:
                disk_after = disk_before
            else:
                disk_after = self.assess_disk(target_paths)
                if disk_after.state is DiskState.LOW:
                    infra_reason = "DISK_LOW"
                elif disk_after.state is DiskState.UNKNOWN:
                    infra_reason = "DISK_UNKNOWN"

        elapsed = self._clock() - started
        telemetry = {
            "policy_approved": "1" if policy.approved else "0",
            "inventory_items": str(len(inventory.items)),
            "preview_candidates": str(len(preview.candidates)),
            "preview_id": preview.preview_id,
            "gate_scratch_swept": str(len(swept)),
            "adaptive_admission": "inactive",
        }
        return MaintenanceResult(
            trigger=trigger,
            ran_gc=ran_gc,
            disk_before=disk_before,
            disk_after=disk_after,
            candidates=len(preview.candidates),
            applied=applied,
            refused=refused,
            infra_reason=infra_reason,
            vacuumed=vacuumed,
            elapsed_seconds=elapsed,
            telemetry=telemetry,
        )

    def preflight_launch(self, *, paths: Sequence[Path | str] | None = None) -> DiskAssessment:
        """Gate a worker/gate launch on live disk state (no expensive work).

        Returns the assessment; the caller parks on ``LOW``/``UNKNOWN`` as a
        typed INFRA instead of launching.  Never deletes anything and never
        manufactures a verdict.
        """
        target = tuple(paths) if paths is not None else self.storage_paths()
        assessment = self.assess_disk(target)
        if assessment.state is DiskState.LOW:
            raise DiskLowInfra("free space below the current floor; parking launch")
        if assessment.state is DiskState.UNKNOWN:
            raise DiskLowInfra("free space unknown; parking launch")
        return assessment

    # -- gate scratch sweep -------------------------------------------------

    def _sweep_gate_scratch(self) -> tuple[str, ...]:
        """Sweep stale gate scratch whose owner PID is dead or aged out."""
        from saberops.gate_runner import sweep_gate_scratch

        roots: list[Path] = []
        env_root = os.environ.get("SABEROPS_GATE_TMP")
        if env_root:
            roots.append(Path(env_root))
        roots.append(Path("/dev/shm"))
        roots.append(self._state_root / "gate-tmp")
        removed: list[str] = []
        for root in roots:
            if not root.is_dir():
                continue
            removed.extend(sweep_gate_scratch(root, now=self._now_fn()))
        return tuple(removed)

    # -- vacuum ------------------------------------------------------------

    def _maybe_vacuum(self) -> bool:
        db_path = self._db.owner_db.db_path
        if not db_path.exists():
            return False
        try:
            ratio = free_page_ratio(db_path)
        except StorageInfraError:
            return False
        if ratio <= OWNER_DB_VACUUM_FREE_PAGE_RATIO:
            return False
        last = self._db.get_storage_vacuum_at("owner")
        now = self._now_fn()
        if last is not None and now < last + OWNER_DB_VACUUM_INTERVAL:
            return False
        from saberops.sqlite_policy import vacuum_database

        try:
            vacuum_database(db_path)
        except StorageInfraError:
            return False
        self._db.record_storage_vacuum("owner", now)
        self._audit(
            StorageAuditEvent(
                at=now,
                phase="result",
                action="vacuum-db",
                artifact_class=ArtifactClass.OWNER_DB.value,
                reference=str(db_path),
                identity=str(db_path),
                run_id=None,
                project_id=None,
                sha=None,
                policy_ref=None,
                reason="owner-db-vacuumed",
                estimated_bytes=None,
                actual_bytes=None,
            )
        )
        return True


def _null_audit(event: StorageAuditEvent) -> None:
    del event


def database_audit_sink(db: Database) -> AuditSink:
    """Return an :data:`AuditSink` that persists events in the owner database."""

    def _sink(event: StorageAuditEvent) -> None:
        db.record_storage_event(
            at=event.at.isoformat(),
            phase=event.phase,
            action=event.action,
            artifact_class=event.artifact_class,
            artifact_ref=event.reference,
            artifact_identity=event.identity,
            reason=event.reason,
            run_id=event.run_id,
            project_id=event.project_id,
            sha=event.sha,
            policy_ref=event.policy_ref,
            estimated_bytes=event.estimated_bytes,
            actual_bytes=event.actual_bytes,
            detail=dict(event.detail),
        )

    return _sink
