"""Owner-aware storage inventory, guarded cleanup and bounded maintenance.

This module is the single inventory/cleanup implementation for the
storage lifecycle: it answers *what does SaberOps own on this host,
what may a validated standing owner policy retire, and how is the scheduled
maintenance pass bounded?*  The adaptive reservation ledger stays separate in
:mod:`saberops.storage_budget`; nothing here activates it.

The merged module keeps the safety posture of its former four modules:

* :func:`build_inventory` is read-only.  Ownership comes from real persisted
  provenance -- run/attempt rows, the candidate lifecycle projection, the
  execution-owner lease table and the job table -- never from a branch name, an
  age, a PID or a SHA that merely happens to be an ancestor of main.  Anything
  the durable state cannot explain is ``UNKNOWN`` and is kept with a reason.
* :func:`preview_gc` / ``orch gc --dry-run`` is a true no-op: it creates
  nothing, writes nothing and deletes nothing.  Retention eligibility is
  decided by the pure functions in the "Retention policy" section below.
* :func:`apply_gc` executes only the exact canonical previewed scope.  It
  re-resolves every identity and every authority immediately before each
  action and refuses on any race (ownership change, symlink/root change,
  branch-tip move, live consumer, pending acceptance, dirty or
  uniquely-ignored content).
* Every intent, result and failure is audited; a successful removal whose
  audit write fails surfaces uncertainty and stops instead of touching another
  target.
* :class:`StorageMaintenance` keeps today's fixed free-space floor on every
  production path, runs bounded policy-permitted GC, and parks a launch with a
  typed ``DISK_LOW``/``DISK_UNKNOWN`` reason instead of fabricating a verdict.

Only native Git operations retire Git objects: ``git worktree remove`` (no
force) and ``git branch -d`` (no force).  There is no ``git worktree prune``,
no ``git branch -D``, no ``rm -rf`` of an unverified path and no remote
operation.  A missing standing owner policy means inventory and report only.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Final

from saberops.candidate_lifecycle import project_candidate_state
from saberops.db import Database
from saberops.git import CHECKPOINT_REF_PREFIX, BranchDeletionRefused, GitManager
from saberops.models import (
    TERMINAL_RUN_STATUSES,
    Attempt,
    AttemptStatus,
    CandidateState,
    Run,
    RunStatus,
)
from saberops.ownership import Liveness, probe_liveness
from saberops.paths import CONFIG_APP_DIR, config_home, saberops_state_dir
from saberops.project import ProjectIdentityError, compute_project_identity
from saberops.sqlite_policy import StorageInfraError, free_page_ratio, read_connection

__all__ = [
    "CHECKPOINT_RETENTION_AFTER_TERMINAL",
    "DEFAULT_DISK_FLOOR_GB",
    "DEFAULT_MAX_RUNS",
    "EVIDENCE_ACCEPTED_RETENTION",
    "EVIDENCE_COMPRESS_AFTER",
    "EVIDENCE_OTHER_RETENTION",
    "GC_POLICY_FILENAME",
    "LANDED_CLEANUP_RETENTION_DAYS",
    "MAINTENANCE_INTERVAL",
    "OWNER_DB_VACUUM_FREE_PAGE_RATIO",
    "OWNER_DB_VACUUM_INTERVAL",
    "ArtifactClass",
    "ByteMeasure",
    "Consumer",
    "DiskAssessment",
    "DiskLowInfra",
    "DiskState",
    "GcAction",
    "GcApplyResult",
    "GcCandidate",
    "GcExclusion",
    "GcOutcome",
    "GcPolicy",
    "GcPolicyError",
    "GcPreview",
    "Inventory",
    "InventoryItem",
    "LandedCleanupKeep",
    "LandedCleanupReport",
    "MaintenanceResult",
    "MaintenanceTrigger",
    "OwnershipState",
    "StorageAuditEvent",
    "StorageMaintenance",
    "apply_gc",
    "build_inventory",
    "current_disk_floor_bytes",
    "database_audit_sink",
    "default_gc_policy_path",
    "disk_ok",
    "load_gc_policy",
    "measure_path_bytes",
    "preview_gc",
    "retire_landed_worktrees",
    "retire_run_worktrees",
]

#: Upper bound on filesystem entries walked per artifact.
MAX_WALK_ENTRIES: Final[int] = 500_000
#: Default upper bound on runs inspected for one repository.
DEFAULT_MAX_RUNS: Final[int] = 2_000

#: Git branch namespace SaberOps creates for engine attempts.
_ORCH_BRANCH_PREFIX: Final[str] = "orch/"


class ArtifactClass(StrEnum):
    """The artifact classes the engine owns plus the Git lifecycle objects it adds."""

    ATTEMPT_WORKTREE = "attempt-worktree"
    LOCAL_BRANCH = "local-branch"
    CHECKPOINT_REF = "checkpoint-ref"
    GATE_SCRATCH = "gate-scratch"
    WORKTREE_VENV = "worktree-venv"
    EVIDENCE = "evidence"
    OWNER_DB = "owner-db"
    OWNED_CACHE = "owned-cache"


class OwnershipState(StrEnum):
    """What the durable records prove about one artifact's owner.

    ``UNKNOWN`` must fail closed to KEEP everywhere; it is the only state a
    consumer may not act on.
    """

    OWNED_LIVE = "owned-live"
    OWNED_TERMINAL = "owned-terminal"
    OWNED_ACCEPTED = "owned-accepted"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Consumer:
    """One known or suspected holder of an artifact."""

    kind: str
    ref: str
    state: str
    protected: bool
    reason: str


@dataclass(frozen=True)
class ByteMeasure:
    """Conservative byte accounting for one on-disk artifact."""

    apparent_bytes: int | None
    reclaimable_bytes: int | None
    hardlinked_files: int
    truncated: bool


@dataclass(frozen=True)
class InventoryItem:
    """One inventoried artifact with its provenance and byte accounting."""

    artifact_class: ArtifactClass
    reference: str
    identity: str
    owner: str | None
    ownership: OwnershipState
    reason: str
    size_bytes: int | None
    reclaimable_bytes: int | None
    consumers: tuple[Consumer, ...] = ()
    detail: Mapping[str, str] = field(default_factory=dict)

    @property
    def protected(self) -> bool:
        """True when any consumer holds the artifact, so it must be kept."""
        return any(consumer.protected for consumer in self.consumers)


@dataclass(frozen=True)
class Inventory:
    """The full inventory for one repository at one instant."""

    generated_at: datetime
    repo_root: str
    project_id: str | None
    items: tuple[InventoryItem, ...]
    warnings: tuple[str, ...] = ()

    def by_class(self, artifact_class: ArtifactClass) -> tuple[InventoryItem, ...]:
        """Return every item of one artifact class, in stable reference order."""
        return tuple(item for item in self.items if item.artifact_class is artifact_class)


def measure_path_bytes(
    path: Path | str, *, max_entries: int = MAX_WALK_ENTRIES
) -> ByteMeasure:
    """Measure one path conservatively without following symlinks.

    Directories are walked with ``followlinks=False``; symlinked directories are
    not descended.  When any regular file has ``st_nlink > 1`` the bytes are
    shared with a link outside the walked tree, so ``reclaimable_bytes`` is
    ``None`` -- shared bytes are never summed as definitely reclaimable.  When
    the walk is truncated, or the path cannot be stat-ed, both byte counts are
    ``None`` (unknown), never zero.

    Covers: reclaimable-byte estimates account for
    hard-linked package caches), and an unknown size fails closed.
    """
    target = Path(path)
    try:
        st = target.lstat()
    except OSError:
        return ByteMeasure(None, None, 0, False)
    if target.is_symlink():
        return ByteMeasure(None, None, 0, False)
    if not target.is_dir():
        return ByteMeasure(int(st.st_size), int(st.st_size), 0, False)

    total = 0
    hardlinked = 0
    entries = 0
    stack: list[Path] = [target]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    entries += 1
                    if entries > max_entries:
                        return ByteMeasure(None, None, hardlinked, True)
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                            continue
                        entry_stat = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    total += int(entry_stat.st_size)
                    if entry_stat.st_nlink > 1:
                        hardlinked += 1
        except OSError:
            continue
    if hardlinked:
        return ByteMeasure(total, None, hardlinked, False)
    return ByteMeasure(total, total, 0, False)


@dataclass(frozen=True)
class _RunFacts:
    run_id: str
    status: RunStatus
    terminal: bool
    preempted: bool


@dataclass(frozen=True)
class _JobFacts:
    job_id: str
    state: str
    waiting_reason: str | None
    current_run_id: str | None
    run_ids: tuple[str, ...]
    updated_at: str | None = None

    @property
    def terminal(self) -> bool:
        return self.state in ("SUCCEEDED", "FAILED", "CANCELLED")


def _resolve_project_id(repo: Path) -> str | None:
    try:
        return compute_project_identity(repo).namespace
    except ProjectIdentityError:
        return None


def _same_repo(candidate: str | None, repo: Path) -> bool:
    if not candidate:
        return False
    try:
        return Path(candidate).resolve() == repo
    except OSError:
        return False


def _read_job_facts(db: Database | None, *, limit: int = 5_000) -> tuple[_JobFacts, ...]:
    """Read job rows without ever creating a table (read-only, no-op safe).

    ``jobs.store`` helpers call ``ensure_tables`` and would write; a dry-run
    inventory must not.  The query is skipped entirely when there is no owner
    database or when the table does not yet exist.
    """
    if db is None:
        return ()
    owner_path = db.owner_db.db_path
    if not owner_path.exists():
        return ()
    try:
        with read_connection(owner_path) as conn:
            exists = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='jobs'"
            ).fetchone()
            if exists is None:
                return ()
            rows = conn.execute(
                "SELECT job_id, state, waiting_reason, current_run_id, run_ids_json, updated_at"
                " FROM jobs LIMIT ?",
                (limit,),
            ).fetchall()
    except (sqlite3.Error, OSError):
        return ()
    facts: list[_JobFacts] = []
    for row in rows:
        raw_run_ids = row["run_ids_json"]
        run_ids: tuple[str, ...] = ()
        if isinstance(raw_run_ids, str) and raw_run_ids:
            try:
                import json

                decoded = json.loads(raw_run_ids)
            except ValueError:
                decoded = None
            if isinstance(decoded, list):
                run_ids = tuple(str(item) for item in decoded)
        facts.append(
            _JobFacts(
                job_id=str(row["job_id"]),
                state=str(row["state"]),
                waiting_reason=(str(row["waiting_reason"]) if row["waiting_reason"] else None),
                current_run_id=(str(row["current_run_id"]) if row["current_run_id"] else None),
                run_ids=run_ids,
                updated_at=(str(row["updated_at"]) if row["updated_at"] else None),
            )
        )
    return tuple(facts)


def _execution_owner_consumer(db: Database, run_id: str) -> Consumer | None:
    try:
        owner = db.get_execution_owner(run_id)
    except Exception:
        return Consumer(
            kind="execution-owner",
            ref=run_id,
            state="UNREADABLE",
            protected=True,
            reason="execution-owner-unreadable",
        )
    if owner is None:
        return None
    liveness = probe_liveness(owner.identity)
    if liveness is Liveness.LIVE or owner.is_active:
        return Consumer(
            kind="execution-owner",
            ref=f"{owner.owner_id}#{owner.generation}",
            state=owner.state,
            protected=True,
            reason="execution-owner-live",
        )
    if liveness is Liveness.UNKNOWN:
        return Consumer(
            kind="execution-owner",
            ref=f"{owner.owner_id}#{owner.generation}",
            state=owner.state,
            protected=True,
            reason="execution-owner-unknown-liveness",
        )
    return None


def _candidate_consumer(db: Database, run_id: str) -> Consumer | None:
    try:
        projection = project_candidate_state(db, run_id)
    except Exception:
        return Consumer(
            kind="candidate",
            ref=run_id,
            state="UNREADABLE",
            protected=True,
            reason="candidate-projection-unreadable",
        )
    state = projection.state
    if state is None:
        return None
    if state in (CandidateState.READY, CandidateState.IN_REVIEW, CandidateState.REVIEWED):
        return Consumer(
            kind="candidate",
            ref=projection.candidate_sha or run_id,
            state=state.value,
            protected=True,
            reason=f"candidate-{state.value.lower()}",
        )
    return None


def _job_consumer(facts: Sequence[_JobFacts], run_id: str) -> Consumer | None:
    for fact in facts:
        if run_id not in fact.run_ids and fact.current_run_id != run_id:
            continue
        if fact.state in ("QUEUED", "RUNNING"):
            return Consumer(
                kind="job",
                ref=fact.job_id,
                state=f"{fact.state}:{fact.waiting_reason or '-'}",
                protected=True,
                reason="job-open",
            )
    return None


def _run_consumers(
    db: Database, facts: Sequence[_JobFacts], run_id: str, status: RunStatus
) -> tuple[Consumer, ...]:
    consumers: list[Consumer] = []
    if status in (RunStatus.RUNNING, RunStatus.PENDING):
        consumers.append(
            Consumer(
                kind="run",
                ref=run_id,
                state=status.value,
                protected=True,
                reason="run-active",
            )
        )
    owner = _execution_owner_consumer(db, run_id)
    if owner is not None:
        consumers.append(owner)
    candidate = _candidate_consumer(db, run_id)
    if candidate is not None:
        consumers.append(candidate)
    job = _job_consumer(facts, run_id)
    if job is not None:
        consumers.append(job)
    return tuple(consumers)


def _classify_run(
    run_facts: _RunFacts,
    candidate_state: CandidateState | None,
    consumers: Sequence[Consumer],
) -> tuple[OwnershipState, str]:
    """Return (ownership, reason) for one run from imported durable facts."""
    if consumers:
        return OwnershipState.OWNED_LIVE, "live-consumer"
    if candidate_state is CandidateState.ACCEPTED:
        return OwnershipState.OWNED_ACCEPTED, "candidate-accepted"
    if candidate_state is CandidateState.REJECTED:
        return OwnershipState.OWNED_TERMINAL, "candidate-rejected"
    if candidate_state is CandidateState.STALE:
        return OwnershipState.OWNED_TERMINAL, "candidate-stale"
    if run_facts.terminal:
        return OwnershipState.OWNED_TERMINAL, f"run-{run_facts.status.value.lower()}"
    return OwnershipState.UNKNOWN, "run-non-terminal-without-consumer"


def _attempt_identity(attempt_id: str, branch: str | None, sha: str | None) -> str:
    parts = [attempt_id]
    if branch:
        parts.append(branch)
    if sha:
        parts.append(sha)
    return "|".join(parts)


def build_inventory(
    db: Database | None,
    *,
    repo_root: Path | str,
    now: datetime | None = None,
    worktree_base: Path | str | None = None,
    state_root: Path | str | None = None,
    max_runs: int = DEFAULT_MAX_RUNS,
    max_entries: int = MAX_WALK_ENTRIES,
    measure_bytes: bool = True,
) -> Inventory:
    """Build the owned-artifact inventory for one repository.

    Purely read-only.  The returned :class:`Inventory` never mutates the
    repository, the filesystem or any database.
    """
    moment = now if now is not None else datetime.now(UTC)
    warnings: list[str] = []
    repo = Path(repo_root).resolve()
    project_id = _resolve_project_id(repo)
    if not repo.exists() or not GitManager.is_git_repo(repo):
        return Inventory(
            generated_at=moment,
            repo_root=str(repo),
            project_id=project_id,
            items=(),
            warnings=("not-a-git-repository",),
        )

    job_facts = _read_job_facts(db)
    items: list[InventoryItem] = []
    run_index: dict[str, Run] = {}
    sha_index: dict[str, str] = {}

    # ---- relational provenance: runs, attempts, worktrees, branches --------
    known_worktrees: dict[str, InventoryItem] = {}
    attempt_branches: dict[str, tuple[str, OwnershipState, str]] = {}
    runs = db.list_runs(limit=max_runs) if db is not None else []
    if db is None:
        warnings.append("no-durable-state")
    if len(runs) >= max_runs:
        warnings.append(f"run-scan-truncated:{max_runs}")
    for run in runs:
        assert db is not None
        if not _same_repo(run.target_repo, repo):
            continue
        run_index[run.id] = run
        status = run.status if isinstance(run.status, RunStatus) else RunStatus(str(run.status))
        run_facts = _RunFacts(
            run_id=run.id,
            status=status,
            terminal=status in TERMINAL_RUN_STATUSES,
            preempted=status is RunStatus.PREEMPTED,
        )
        try:
            projection = project_candidate_state(db, run.id)
            candidate_state = projection.state
        except Exception:
            candidate_state = None
        consumers = _run_consumers(db, job_facts, run.id, status)
        ownership, reason = _classify_run(run_facts, candidate_state, consumers)
        attempts = db.get_attempts_for_run(run.id)
        for attempt in attempts:
            if attempt.branch_name:
                attempt_branches[attempt.branch_name] = (run.id, ownership, reason)
            if not attempt.worktree_path:
                continue
            wt_path = Path(attempt.worktree_path).resolve()
            measure = (
                measure_path_bytes(wt_path, max_entries=max_entries)
                if measure_bytes
                else ByteMeasure(None, None, 0, False)
            )
            if measure.truncated:
                warnings.append(f"measure-truncated:{wt_path}")
            attempt_terminal = attempt.status in (
                AttemptStatus.SUCCESS,
                AttemptStatus.FAILED,
                AttemptStatus.TIMEOUT,
            )
            item = InventoryItem(
                artifact_class=ArtifactClass.ATTEMPT_WORKTREE,
                reference=str(wt_path),
                identity=_attempt_identity(attempt.id, attempt.branch_name, attempt.commit_sha),
                owner=run.id,
                ownership=ownership,
                reason=reason if attempt_terminal or consumers else "attempt-non-terminal",
                size_bytes=measure.apparent_bytes,
                reclaimable_bytes=measure.reclaimable_bytes,
                consumers=consumers,
                detail={
                    "run_id": run.id,
                    "run_status": status.value,
                    "attempt_id": attempt.id,
                    "attempt_status": str(attempt.status),
                    "attempt_terminal": "1" if attempt_terminal else "0",
                    "preempted": "1" if run_facts.preempted else "0",
                    "candidate_state": candidate_state.value if candidate_state else "unknown",
                    "branch": attempt.branch_name or "",
                    "commit_sha": attempt.commit_sha or "",
                    "hardlinked_files": str(measure.hardlinked_files),
                },
            )
            if attempt.commit_sha and attempt_terminal:
                sha_index.setdefault(attempt.commit_sha, run.id)
            items.append(item)
            known_worktrees[str(wt_path)] = item

    # ---- real Git registrations not explained by any attempt ---------------
    worktree_registrations = GitManager.list_worktrees(repo)
    base = (
        Path(worktree_base).resolve()
        if worktree_base is not None
        else GitManager.get_default_worktree_base(repo).resolve()
    )
    for registration in worktree_registrations:
        reg_path = str(Path(registration.path).resolve())
        if reg_path in known_worktrees:
            continue
        try:
            inside_base = Path(reg_path).is_relative_to(base) and Path(reg_path) != base
        except (ValueError, AttributeError):
            inside_base = False
        if not inside_base:
            continue
        measure = (
            measure_path_bytes(reg_path, max_entries=max_entries)
            if measure_bytes
            else ByteMeasure(None, None, 0, False)
        )
        reg_consumers: tuple[Consumer, ...] = ()
        reason = "unregistered-worktree"
        if registration.locked:
            reg_consumers = (
                Consumer(
                    kind="worktree-lock",
                    ref=reg_path,
                    state="locked",
                    protected=True,
                    reason="worktree-locked",
                ),
            )
            reason = "worktree-locked"
        items.append(
            InventoryItem(
                artifact_class=ArtifactClass.ATTEMPT_WORKTREE,
                reference=reg_path,
                identity=registration.head or reg_path,
                owner=None,
                ownership=OwnershipState.UNKNOWN,
                reason=reason,
                size_bytes=measure.apparent_bytes,
                reclaimable_bytes=measure.reclaimable_bytes,
                consumers=reg_consumers,
                detail={
                    "branch": registration.branch or "",
                    "detached": "1" if registration.detached else "0",
                    "prunable": "1" if registration.prunable else "0",
                    "hardlinked_files": str(measure.hardlinked_files),
                },
            )
        )

    # ---- local engine branches --------------------------------------------
    branch_tips = GitManager.list_local_branches(repo)
    for branch, sha in branch_tips.items():
        if not branch.startswith(_ORCH_BRANCH_PREFIX):
            continue
        owning = attempt_branches.get(branch)
        if owning is None:
            items.append(
                InventoryItem(
                    artifact_class=ArtifactClass.LOCAL_BRANCH,
                    reference=branch,
                    identity=sha,
                    owner=None,
                    ownership=OwnershipState.UNKNOWN,
                    reason="unregistered-branch",
                    size_bytes=0,
                    reclaimable_bytes=0,
                    detail={"branch_tip": sha, "registered": "0"},
                )
            )
            continue
        # A registered attempt branch is only as disposable as its owning run.
        run_id, ownership, reason = owning
        items.append(
            InventoryItem(
                artifact_class=ArtifactClass.LOCAL_BRANCH,
                reference=branch,
                identity=sha,
                owner=run_id,
                ownership=ownership,
                reason=f"branch-{reason}",
                size_bytes=0,
                reclaimable_bytes=0,
                detail={"branch_tip": sha, "registered": "1", "run_id": run_id},
            )
        )

    # ---- checkpoint refs ---------------------------------------------------
    ref_result = _list_checkpoint_refs(repo)
    jobs_by_id = {fact.job_id: fact for fact in job_facts}
    for ref, sha in ref_result:
        job_id = ref[len(CHECKPOINT_REF_PREFIX):].split("/", 1)[0]
        job = jobs_by_id.get(job_id)
        if job is not None and job.terminal and job.updated_at:
            ownership = OwnershipState.OWNED_TERMINAL
            reason = f"checkpoint-job-{job.state.lower()}"
            detail: dict[str, str] = {
                "job_id": job_id,
                "sha": sha,
                "job_terminal": "1",
                "terminal_at": job.updated_at,
            }
        elif job is not None and job.terminal:
            # Terminal job with no recorded anchor: keep it, never guess a time.
            ownership = OwnershipState.UNKNOWN
            reason = "checkpoint-terminal-at-missing"
            detail = {"job_id": job_id, "sha": sha, "job_terminal": "1", "terminal_at": ""}
        elif job is not None:
            ownership = OwnershipState.OWNED_LIVE
            reason = "checkpoint-job-active"
            detail = {"job_id": job_id, "sha": sha, "job_terminal": "0", "terminal_at": ""}
        else:
            ownership = OwnershipState.UNKNOWN
            reason = "checkpoint-job-unknown"
            detail = {"job_id": job_id, "sha": sha, "job_terminal": "0", "terminal_at": ""}
        items.append(
            InventoryItem(
                artifact_class=ArtifactClass.CHECKPOINT_REF,
                reference=ref,
                identity=sha,
                owner=job_id or None,
                ownership=ownership,
                reason=reason,
                size_bytes=0,
                reclaimable_bytes=0,
                detail=detail,
            )
        )

    # ---- worktree venvs ----------------------------------------------------
    for wt_key in known_worktrees:
        venv = Path(wt_key) / ".venv"
        if not venv.exists():
            continue
        measure = (
            measure_path_bytes(venv, max_entries=max_entries)
            if measure_bytes
            else ByteMeasure(None, None, 0, False)
        )
        parent = known_worktrees[wt_key]
        parent_live = parent.ownership is OwnershipState.OWNED_LIVE
        parent_eligible = (
            not parent.protected
            and parent.ownership in (OwnershipState.OWNED_TERMINAL, OwnershipState.OWNED_ACCEPTED)
        )
        items.append(
            InventoryItem(
                artifact_class=ArtifactClass.WORKTREE_VENV,
                reference=str(venv),
                identity=str(venv),
                owner=parent.owner,
                ownership=parent.ownership,
                reason="venv-follows-worktree",
                size_bytes=measure.apparent_bytes,
                reclaimable_bytes=measure.reclaimable_bytes,
                detail={
                    "parent_worktree": wt_key,
                    "parent_worktree_live": "1" if parent_live else "0",
                    "parent_eligible": "1" if parent_eligible else "0",
                    "hardlinked_files": str(measure.hardlinked_files),
                },
            )
        )

    # ---- owner database ----------------------------------------------------
    owner_db_path = db.owner_db.db_path if db is not None else _default_owner_db_path()
    items.append(
        InventoryItem(
            artifact_class=ArtifactClass.OWNER_DB,
            reference=str(owner_db_path),
            identity=str(owner_db_path),
            owner="owner",
            ownership=OwnershipState.OWNED_LIVE,
            reason="owner-database",
            size_bytes=_safe_file_size(owner_db_path),
            reclaimable_bytes=0,
            detail={"scope": "owner"},
        )
    )

    # ---- gate scratch ------------------------------------------------------
    items.extend(
        _discover_gate_scratch(
            state_root=_resolve_state_root(state_root, owner_db_path),
            max_entries=max_entries,
            measure_bytes=measure_bytes,
        )
    )

    # ---- run evidence ------------------------------------------------------
    items.extend(
        _discover_evidence(
            db=db,
            run_index=run_index,
            sha_index=sha_index,
            state_root=_resolve_state_root(state_root, owner_db_path),
            owner_db_path=owner_db_path,
            max_entries=max_entries,
            measure_bytes=measure_bytes,
        )
    )

    # ---- engine-owned caches -------------------------------------------------
    items.extend(
        _discover_owned_caches(
            repo=repo,
            state_root=_resolve_state_root(state_root, owner_db_path),
            max_entries=max_entries,
            measure_bytes=measure_bytes,
        )
    )

    items.sort(key=lambda item: (item.artifact_class.value, item.reference))
    return Inventory(
        generated_at=moment,
        repo_root=str(repo),
        project_id=project_id,
        items=tuple(items),
        warnings=tuple(warnings),
    )


def storage_owned_roots(repo_root: Path | str) -> tuple[str, ...]:
    """Return the root directories the GC service may ever act inside.

    Worktree and venv artifacts live under the repository's worktree base; the
    owner database and evidence live under the user's SaberOps state directory.
    A path equal to one of these roots is never eligible (the retention policy
    enforces that), and anything outside them fails closed.  Filesystem root
    (``/``) is never returned.
    """
    from saberops.paths import saberops_state_dir

    repo = Path(repo_root).resolve()
    roots = {
        str(GitManager.get_default_worktree_base(repo).resolve()),
        str((repo.parent / ".orch-worktrees").resolve()),
        str(saberops_state_dir().resolve()),
    }
    return tuple(sorted(root for root in roots if root and root != "/"))


def _safe_file_size(path: Path) -> int | None:
    try:
        return int(path.stat().st_size)
    except OSError:
        return None


def _default_owner_db_path() -> Path:
    from saberops.paths import get_default_db_path

    return get_default_db_path()


def _resolve_state_root(state_root: Path | str | None, owner_db_path: Path) -> Path:
    if state_root is not None:
        return Path(state_root).resolve()
    from saberops.paths import saberops_state_dir

    try:
        return saberops_state_dir().resolve()
    except Exception:  # pragma: no cover - defensive
        return owner_db_path.parent.resolve()


def _read_scratch_owner(directory: Path) -> dict[str, object] | None:
    """Read a gate scratch owner record without importing the gate runner."""
    import json as _json

    try:
        raw = (directory / ".saberops-scratch-owner.json").read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        decoded = _json.loads(raw)
    except ValueError:
        return None
    return decoded if isinstance(decoded, dict) else None


_SCRATCH_PREFIX = "orchestrator-gate-"


def _discover_gate_scratch(
    *, state_root: Path, max_entries: int, measure_bytes: bool
) -> list[InventoryItem]:
    """Inventory engine-owned gate scratch directories under configured roots.

    Only roots the engine itself owns are scanned, and only ``orchestrator-gate-*``
    entries that carry an owner record are adopted.  External or unowned
    directories are ignored (reported as out of scope), never guessed at from a
    name or age.  Ownership follows the recorded process identity when present,
    else a recorded PID; a missing/unreadable owner stays UNKNOWN.
    """
    roots: list[Path] = []
    env_root = os.environ.get("SABEROPS_GATE_TMP")
    if env_root:
        roots.append(Path(env_root))
    roots.append(state_root / "gate-tmp")
    roots.append(Path("/dev/shm"))
    seen: set[str] = set()
    items: list[InventoryItem] = []
    for root in roots:
        if not root.is_dir():
            continue
        try:
            entries = sorted(root.iterdir(), key=lambda entry: entry.name)
        except OSError:
            continue
        for entry in entries:
            if not entry.is_dir() or entry.is_symlink():
                continue
            if not entry.name.startswith(_SCRATCH_PREFIX):
                continue
            resolved = str(entry.resolve())
            if resolved in seen or not _is_within(resolved, root):
                continue
            owner = _read_scratch_owner(entry)
            if owner is None:
                continue
            seen.add(resolved)
            consumers, ownership, reason = _scratch_ownership(owner)
            measure = (
                measure_path_bytes(entry, max_entries=max_entries)
                if measure_bytes
                else ByteMeasure(None, None, 0, False)
            )
            items.append(
                InventoryItem(
                    artifact_class=ArtifactClass.GATE_SCRATCH,
                    reference=resolved,
                    identity=resolved,
                    owner=str(owner.get("pid")) if owner.get("pid") is not None else None,
                    ownership=ownership,
                    reason=reason,
                    size_bytes=measure.apparent_bytes,
                    reclaimable_bytes=measure.reclaimable_bytes,
                    consumers=consumers,
                    detail={"owner_record": "1", "hardlinked_files": str(measure.hardlinked_files)},
                )
            )
    return items


def _scratch_ownership(
    owner: Mapping[str, object],
) -> tuple[tuple[Consumer, ...], OwnershipState, str]:
    from saberops.ownership import Liveness, ProcessIdentity, probe_liveness

    identity_raw = owner.get("identity")
    identity = None
    if isinstance(identity_raw, Mapping):
        identity = ProcessIdentity.from_dict(identity_raw)
    if identity is None:
        pid = owner.get("pid")
        if not isinstance(pid, int):
            return (
                (
                    Consumer(
                        kind="scratch-owner",
                        ref=str(owner),
                        state="UNKNOWN",
                        protected=True,
                        reason="scratch-owner-unknown",
                    ),
                ),
                OwnershipState.UNKNOWN,
                "scratch-owner-unknown",
            )
        from saberops.ownership import ProcessIdentity as _PI

        identity = _PI.for_pid(pid)
        if identity is None:
            # PID no longer running (or unreadable /proc) -> provably not the
            # recorded process; treat as dead-owned disposable scratch.
            return ((), OwnershipState.OWNED_TERMINAL, "scratch-owner-dead")
    liveness = probe_liveness(identity)
    if liveness is Liveness.LIVE:
        return (
            (
                Consumer(
                    kind="scratch-owner",
                    ref=f"pid:{owner.get('pid')}",
                    state="LIVE",
                    protected=True,
                    reason="scratch-owner-live",
                ),
            ),
            OwnershipState.OWNED_LIVE,
            "scratch-owner-live",
        )
    if liveness is Liveness.UNKNOWN:
        return (
            (
                Consumer(
                    kind="scratch-owner",
                    ref=f"pid:{owner.get('pid')}",
                    state="UNKNOWN",
                    protected=True,
                    reason="scratch-owner-unknown-liveness",
                ),
            ),
            OwnershipState.UNKNOWN,
            "scratch-owner-unknown",
        )
    return ((), OwnershipState.OWNED_TERMINAL, "scratch-owner-dead")


def _is_within(path: str, root: Path) -> bool:
    try:
        return Path(path).resolve().is_relative_to(root.resolve())
    except (OSError, ValueError):
        return False


def _discover_evidence(
    *,
    db: Database | None,
    run_index: Mapping[str, Run],
    sha_index: Mapping[str, str],
    state_root: Path,
    owner_db_path: Path,
    max_entries: int,
    measure_bytes: bool,
) -> list[InventoryItem]:
    """Inventory run evidence under engine-owned roots with verified provenance.

    Roots scanned: the transcript directory next to the owner database and every
    project ``gate-evidence/candidates/<sha>`` directory under the state root.
    An entry is linked to a run only when its name is an exact run id or an exact
    persisted candidate SHA; anything else stays UNKNOWN and is kept.  Terminal
    status, acceptance, creation time and the terminal anchor come from the run
    record, never from the directory name or mtime.
    """
    targets: list[tuple[Path, str]] = []
    transcripts = owner_db_path.parent / "transcripts"
    if transcripts.is_dir():
        for entry in sorted(transcripts.iterdir(), key=lambda item: item.name):
            if entry.is_symlink() or not entry.is_file():
                continue
            run_id = entry.stem
            if run_id in run_index:
                targets.append((entry, run_id))
    projects = state_root / "projects"
    if projects.is_dir():
        for project in sorted(projects.iterdir(), key=lambda item: item.name):
            candidates = project / "gate-evidence" / "candidates"
            if not candidates.is_dir():
                continue
            for entry in sorted(candidates.iterdir(), key=lambda item: item.name):
                if entry.is_symlink() or not entry.is_dir():
                    continue
                mapped_run_id = sha_index.get(entry.name)
                if mapped_run_id is not None:
                    targets.append((entry, mapped_run_id))

    items: list[InventoryItem] = []
    seen: set[str] = set()
    for path, run_id in targets:
        resolved = str(path.resolve())
        if resolved in seen:
            continue
        seen.add(resolved)
        run = run_index.get(run_id)
        status = run.status if run is not None else None
        terminal = bool(status in TERMINAL_RUN_STATUSES) if status is not None else False
        ownership = OwnershipState.OWNED_TERMINAL if terminal else OwnershipState.UNKNOWN
        accepted = False
        if terminal and db is not None:
            try:
                accepted = project_candidate_state(db, run_id).state is CandidateState.ACCEPTED
            except Exception:
                accepted = False
        measure = (
            measure_path_bytes(path, max_entries=max_entries)
            if measure_bytes
            else ByteMeasure(None, None, 0, False)
        )
        items.append(
            InventoryItem(
                artifact_class=ArtifactClass.EVIDENCE,
                reference=resolved,
                identity=resolved,
                owner=run_id,
                ownership=ownership,
                reason="evidence-run-terminal" if terminal else "evidence-run-active",
                size_bytes=measure.apparent_bytes,
                reclaimable_bytes=measure.reclaimable_bytes,
                detail={
                    "run_id": run_id,
                    "run_status": status.value if status is not None else "",
                    "run_accepted": "1" if accepted else "0",
                    "compressed": "0",
                    "created_at": (run.created_at if run is not None and run.created_at else ""),
                    "terminal_at": (
                        run.completed_at if run is not None and run.completed_at else ""
                    ),
                    "hardlinked_files": str(measure.hardlinked_files),
                },
            )
        )
    return items


#: Engine-owned cache directory names under the Git common dir.
_OWNED_CACHE_DIR_NAMES: Final[tuple[str, ...]] = (
    "saberops-mypy-cache",
    "saberops-test-map",
)


def _git_common_dir(repo: Path) -> Path:
    """Return the repository's Git common dir (read-only, no writes)."""
    from saberops.process import run_process

    result = run_process(["git", "rev-parse", "--git-common-dir"], cwd=repo)
    if result.passed and result.stdout.strip():
        common = Path(result.stdout.strip())
        if not common.is_absolute():
            common = repo / common
        try:
            return common.resolve()
        except OSError:
            return common
    return (repo / ".git").resolve()


def _discover_owned_caches(
    *,
    repo: Path,
    state_root: Path,
    max_entries: int,
    measure_bytes: bool,
) -> list[InventoryItem]:
    """Inventory engine-owned package/test caches (never eligible for GC).

    Scans ``<git_common_dir>/saberops-mypy-cache``,
    ``<git_common_dir>/saberops-test-map`` and ``<state_root>/cache`` when
    present on disk, skipping symlinks.  Each entry is reported as
    ``OWNED_LIVE`` with a protected ``shared-engine-cache`` consumer so
    ``orch gc --dry-run`` surfaces its bytes and protection reason while never
    scheduling it for deletion.  Unreadable roots are reported as ``UNKNOWN``
    (still protected) rather than guessed at.

    Covers: owned caches are inventoried and protected.
    """
    candidates: list[tuple[Path, str]] = []
    try:
        common = _git_common_dir(repo)
    except Exception:
        common = repo / ".git"
    for name in _OWNED_CACHE_DIR_NAMES:
        candidates.append((common / name, name))
    candidates.append((state_root / "cache", "state-cache"))

    items: list[InventoryItem] = []
    seen: set[str] = set()
    for path, cache_kind in candidates:
        try:
            if path.is_symlink():
                continue
            if not path.is_dir():
                continue
            resolved = str(path.resolve())
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            readable = os.access(resolved, os.R_OK | os.X_OK)
        except OSError:
            readable = False
        ownership = OwnershipState.OWNED_LIVE if readable else OwnershipState.UNKNOWN
        state = "LIVE" if readable else "UNREADABLE"
        measure = (
            measure_path_bytes(path, max_entries=max_entries)
            if measure_bytes
            else ByteMeasure(None, None, 0, False)
        )
        items.append(
            InventoryItem(
                artifact_class=ArtifactClass.OWNED_CACHE,
                reference=resolved,
                identity=resolved,
                owner="engine",
                ownership=ownership,
                reason="shared-engine-cache",
                size_bytes=measure.apparent_bytes,
                reclaimable_bytes=None,
                consumers=(
                    Consumer(
                        kind="engine-cache",
                        ref=resolved,
                        state=state,
                        protected=True,
                        reason="shared-engine-cache",
                    ),
                ),
                detail={
                    "cache_kind": cache_kind,
                    "hardlinked_files": str(measure.hardlinked_files),
                },
            )
        )
    return items


def _list_checkpoint_refs(repo: Path) -> tuple[tuple[str, str], ...]:
    """Return ``((ref, sha), ...)`` for every checkpoint ref (read-only)."""
    from saberops.process import run_process

    result = run_process(
        [
            "git",
            "for-each-ref",
            "--format=%(refname) %(objectname)",
            CHECKPOINT_REF_PREFIX,
        ],
        cwd=repo,
    )
    if not result.passed:
        return ()
    refs: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        parts = line.strip().split()
        if len(parts) != 2:
            continue
        ref, sha = parts
        if ref.startswith(CHECKPOINT_REF_PREFIX) and len(sha) == 40:
            refs.append((ref, sha))
    return tuple(refs)

# ---------------------------------------------------------------------------
# Retention policy
# ---------------------------------------------------------------------------
#
# Frozen accepted-policy values.  The terminal-retention anchor for evidence is
# an UNAPPROVED conservative safety floor, not an ADR decision: it does not
# settle the canonical anchor, and evidence deletion stays refused until a
# validated owner policy explicitly settles it (``evidence_anchor``).

#: Evidence: compress after 7 days (anchored at creation, non-destructive).
EVIDENCE_COMPRESS_AFTER: Final[timedelta] = timedelta(days=7)
#: Evidence deletion from terminal time: accepted 90 days, other terminal 30.
EVIDENCE_ACCEPTED_RETENTION: Final[timedelta] = timedelta(days=90)
EVIDENCE_OTHER_RETENTION: Final[timedelta] = timedelta(days=30)
#: Checkpoint refs are retained until 7 days after the owning job is terminal.
CHECKPOINT_RETENTION_AFTER_TERMINAL: Final[timedelta] = timedelta(days=7)
#: Owner-DB VACUUM: weekly, and only above a 20 % free-page ratio.
OWNER_DB_VACUUM_INTERVAL: Final[timedelta] = timedelta(days=7)
OWNER_DB_VACUUM_FREE_PAGE_RATIO: Final[float] = 0.20

#: Run statuses that make an owning job terminal for checkpoint retention.
_TERMINAL_RUN_STATES: Final[tuple[str, ...]] = ("COMPLETED", "FAILED", "ORPHANED")

#: Candidate detail states for an attempt worktree.
_PENDING_CANDIDATE_STATES: Final[frozenset[str]] = frozenset({"READY", "IN_REVIEW", "REVIEWED"})
_TERMINAL_CANDIDATE_STATES: Final[frozenset[str]] = frozenset(
    {"ACCEPTED", "REJECTED", "STALE"}
)
_KNOWN_CANDIDATE_STATES: Final[frozenset[str]] = (
    _PENDING_CANDIDATE_STATES | _TERMINAL_CANDIDATE_STATES
)


def _owned(item: InventoryItem) -> bool:
    """Covers: fail-closed ownership (the orphan rule).

    Ownership is authorized only by proved provenance (:class:`OwnershipState`
    other than ``UNKNOWN``) *and* a recorded owner identity.  A corrupt
    inventory rebuilt from JSON/SQLite, a raw state string or a missing owner
    can never turn an unowned item into an eligible action.
    """
    return item.ownership is not OwnershipState.UNKNOWN and item.owner not in (None, "")


def _usable_root(root: str) -> PurePosixPath | None:
    """Return a usable owned root, or None when the root must be discarded.

    Covers: malformed owned roots fail closed.  Empty, NUL-bearing, relative,
    traversal (``..``) and filesystem-root (``/``) roots are ignored so they can
    never confer eligibility on arbitrary paths.
    """
    if root == "" or "\x00" in root:
        return None
    pure_root = PurePosixPath(root)
    if not pure_root.is_absolute():
        return None
    if ".." in pure_root.parts:
        return None
    if pure_root == PurePosixPath("/"):
        return None
    return pure_root


def _check_filesystem_path(path: str, owned_roots: Sequence[str]) -> str | None:
    """Return None when ``path`` is safe, else the KEEP reason code.

    Covers: outside-root escapes, ``..`` traversals and owned-root
    self-protection (root confinement).  A path equal to an owned root is never
    eligible (``root-itself-protected``); only a strict descendant of a usable
    owned root is.  Purely lexical: an eligible verdict is a policy proposal,
    never deletion authorization; :func:`apply_gc` re-resolves paths against
    validated owner roots immediately before acting.
    """
    if path == "":
        return "missing-metadata"
    if "\x00" in path:
        return "invalid-path"
    pure = PurePosixPath(path)
    if not pure.is_absolute():
        return "relative-path"
    if ".." in pure.parts:
        return "path-escape"
    descendant = False
    for root in owned_roots:
        pure_root = _usable_root(root)
        if pure_root is None:
            continue
        if pure == pure_root:
            return "root-itself-protected"
        if pure_root in pure.parents:
            descendant = True
    if descendant:
        return None
    return "outside-roots"


def _check_ref(ref: str, job_id: str | None) -> str | None:
    """Return None when ``ref`` is exactly ``<job_id>/<seq>`` in-namespace.

    Covers: checkpoint ref-to-job ownership binding.  Terminal metadata
    belonging to a different job must never authorize deletion, and a merely
    valid prefix never implies ownership.
    """
    if ref == "":
        return "missing-metadata"
    if "\x00" in ref:
        return "invalid-ref"
    if ".." in PurePosixPath(ref).parts:
        return "ref-escape"
    if not ref.startswith(CHECKPOINT_REF_PREFIX):
        return "outside-roots"
    segments = ref[len(CHECKPOINT_REF_PREFIX) :].split("/")
    if len(segments) != 2 or segments[0] == "" or segments[1] == "":
        return "invalid-ref"
    if job_id is None or job_id == "" or "/" in job_id:
        return "missing-metadata"
    if segments[0] != job_id:
        return "ref-job-mismatch"
    return None
GC_POLICY_FILENAME: Final[str] = "storage_gc.json"

#: The retention bound for worktrees this opt-in step must keep.
LANDED_CLEANUP_RETENTION_DAYS: Final[int] = 14

#: Non-`orch/` ignored entries inside a worktree that are proven disposable.
_DISPOSABLE_IGNORED_PREFIXES: Final[tuple[str, ...]] = (".venv/",)


class GcPolicyError(ValueError):
    """A standing GC policy file is malformed or contradictory."""


class GcAction(StrEnum):
    """One executable lifecycle action."""

    REMOVE_WORKTREE = "remove-worktree"
    DELETE_BRANCH = "delete-branch"
    DELETE_CHECKPOINT_REF = "delete-checkpoint-ref"
    DELETE_PATH = "delete-path"
    COMPRESS_EVIDENCE = "compress-evidence"
    VACUUM_DB = "vacuum-db"


_ACTION_ORDER: Final[dict[GcAction, int]] = {
    GcAction.REMOVE_WORKTREE: 0,
    GcAction.DELETE_PATH: 1,
    GcAction.DELETE_BRANCH: 2,
    GcAction.DELETE_CHECKPOINT_REF: 3,
    GcAction.COMPRESS_EVIDENCE: 4,
    GcAction.VACUUM_DB: 5,
}

_ALL_CLASSES: Final[frozenset[ArtifactClass]] = frozenset(ArtifactClass)


@dataclass(frozen=True)
class GcPolicy:
    """A validated, owner-scoped standing lifecycle policy.

    Absent/unapproved policy has ``approved=False`` and authorizes nothing
    destructive. ``evidence_anchor`` is ``None`` while 's
    retention anchor is unsettled, and evidence deletion stays refused until an
    explicit policy settles it.
    """

    approved: bool
    version: str
    source: str
    scope_classes: frozenset[ArtifactClass] = _ALL_CLASSES
    approved_target_ref: str | None = None
    evidence_anchor: str | None = None
    allow_worktree_retirement: bool = False
    allow_branch_retirement: bool = False
    allow_checkpoint_deletion: bool = False
    allow_scratch_deletion: bool = False
    allow_evidence_deletion: bool = False
    allow_evidence_compression: bool = False
    allow_vacuum: bool = False


_UNAPPROVED_POLICY: Final[GcPolicy] = GcPolicy(
    approved=False,
    version="none",
    source="absent",
)


def default_gc_policy_path() -> Path:
    """Return the canonical owner settings path for the GC standing policy."""
    return config_home() / CONFIG_APP_DIR / GC_POLICY_FILENAME


def load_gc_policy(path: Path | None) -> GcPolicy:
    """Read the owner standing GC policy; an absent file authorizes nothing."""
    if path is None or not path.exists():
        return _UNAPPROVED_POLICY
    if path.is_symlink():
        raise GcPolicyError(f"GC policy path is a symlink: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GcPolicyError(f"unreadable GC policy: {exc}") from exc
    if not isinstance(raw, dict):
        raise GcPolicyError("GC policy must be a JSON object")
    approved = raw.get("approved")
    if not isinstance(approved, bool):
        raise GcPolicyError("GC policy 'approved' must be a boolean")
    if not approved:
        return GcPolicy(approved=False, version=str(raw.get("version", "1")), source="owner-file")
    scope_raw = raw.get("scope_classes")
    scope: frozenset[ArtifactClass]
    if scope_raw is None:
        scope = _ALL_CLASSES
    elif isinstance(scope_raw, list):
        parsed: set[ArtifactClass] = set()
        for entry in scope_raw:
            try:
                parsed.add(ArtifactClass(str(entry)))
            except ValueError as exc:
                raise GcPolicyError(f"unknown scope class: {entry!r}") from exc
        scope = frozenset(parsed)
    else:
        raise GcPolicyError("scope_classes must be a list")
    anchor = raw.get("evidence_anchor")
    if anchor not in (None, "terminal", "accepted"):
        raise GcPolicyError("evidence_anchor must be null, 'terminal' or 'accepted'")
    if bool(raw.get("allow_evidence_deletion")) and anchor is None:
        raise GcPolicyError(
            "allow_evidence_deletion requires an explicit evidence_anchor; the "
            "retention anchor is unsettled and must not be assumed"
        )
    target = raw.get("approved_target_ref")
    if target is not None and not isinstance(target, str):
        raise GcPolicyError("approved_target_ref must be a string")
    return GcPolicy(
        approved=True,
        version=str(raw.get("version", "1")),
        source="owner-file",
        scope_classes=scope,
        approved_target_ref=target,
        evidence_anchor=anchor,
        allow_worktree_retirement=bool(raw.get("allow_worktree_retirement")),
        allow_branch_retirement=bool(raw.get("allow_branch_retirement")),
        allow_checkpoint_deletion=bool(raw.get("allow_checkpoint_deletion")),
        allow_scratch_deletion=bool(raw.get("allow_scratch_deletion")),
        allow_evidence_deletion=bool(raw.get("allow_evidence_deletion")),
        allow_evidence_compression=bool(raw.get("allow_evidence_compression")),
        allow_vacuum=bool(raw.get("allow_vacuum")),
    )


@dataclass(frozen=True)
class GcCandidate:
    """One exact, previewed lifecycle action."""

    artifact_class: ArtifactClass
    action: GcAction
    reference: str
    identity: str
    owner: str | None
    reason: str
    estimated_bytes: int | None
    reclaimable: bool
    detail: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class GcExclusion:
    """One artifact the policy keeps, with the machine-readable reason."""

    artifact_class: ArtifactClass
    reference: str
    reason: str


@dataclass(frozen=True)
class GcPreview:
    """A deterministic, side-effect-free preview of one GC scope."""

    generated_at: datetime
    repo_root: str
    project_id: str | None
    policy_ref: str | None
    approved: bool
    scope_classes: tuple[ArtifactClass, ...]
    candidates: tuple[GcCandidate, ...]
    exclusions: tuple[GcExclusion, ...]
    estimated_reclaimable_bytes: int | None
    preview_id: str

    def candidate_for(self, reference: str) -> GcCandidate | None:
        for candidate in self.candidates:
            if candidate.reference == reference:
                return candidate
        return None


def _policy_ref(policy: GcPolicy, policy_path: Path | None) -> str | None:
    if not policy.approved:
        return None
    location = str(policy_path) if policy_path is not None else "owner-policy"
    return f"{policy.source}:{policy.version}:{location}"


def _preview_id(candidates: Sequence[GcCandidate], policy_ref: str | None) -> str:
    digest = sha256()
    digest.update((policy_ref or "unapproved").encode("utf-8"))
    for candidate in sorted(candidates, key=lambda c: (c.artifact_class.value, c.reference)):
        digest.update(
            "\x00".join(
                (
                    candidate.artifact_class.value,
                    candidate.action.value,
                    candidate.reference,
                    candidate.identity,
                )
            ).encode("utf-8")
        )
    return digest.hexdigest()[:32]


def _parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def preview_gc(
    inventory: Inventory,
    *,
    policy: GcPolicy | None = None,
    policy_path: Path | None = None,
    classes: Sequence[ArtifactClass] | None = None,
    owned_roots: Sequence[str] | None = None,
    now: datetime | None = None,
) -> GcPreview:
    """Project the retention policy onto ``inventory`` without any side effect.

    Pure and deterministic.  Only classes permitted by the standing ``policy``
    (or every class when only reporting exclusions) are considered; every other
    artifact is reported as an exclusion with a reason.  Missing policy is not
    an error -- it simply authorizes nothing destructive.
    """
    moment = now if now is not None else inventory.generated_at
    if moment.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    active_policy = policy if policy is not None else _UNAPPROVED_POLICY
    selected = set(classes) if classes is not None else set(_ALL_CLASSES)
    if owned_roots is None:
        owned_roots = storage_owned_roots(inventory.repo_root)

    candidates: list[GcCandidate] = []
    exclusions: list[GcExclusion] = []

    def _exclude(item: InventoryItem, reason: str) -> None:
        exclusions.append(
            GcExclusion(
                artifact_class=item.artifact_class,
                reference=item.reference,
                reason=reason,
            )
        )

    for item in inventory.items:
        if item.artifact_class is ArtifactClass.OWNED_CACHE:
            _exclude(item, "shared-cache-protected")
            continue
        if item.artifact_class not in selected:
            _exclude(item, "class-not-selected")
            continue
        if item.artifact_class not in active_policy.scope_classes:
            _exclude(item, "class-outside-policy-scope")
            continue
        if not active_policy.approved:
            _exclude(item, "no-standing-policy")
            continue

        if item.artifact_class is ArtifactClass.ATTEMPT_WORKTREE:
            candidate = _plan_worktree_item(item, active_policy, owned_roots, now=moment)
        elif item.artifact_class is ArtifactClass.LOCAL_BRANCH:
            candidate = _plan_branch_item(item, active_policy)
        elif item.artifact_class is ArtifactClass.CHECKPOINT_REF:
            candidate = _plan_checkpoint_item(item, active_policy, now=moment)
        elif item.artifact_class is ArtifactClass.WORKTREE_VENV:
            candidate = _plan_venv_item(item, active_policy, owned_roots)
        elif item.artifact_class is ArtifactClass.OWNER_DB:
            candidate = _plan_owner_db_item(item, active_policy)
        elif item.artifact_class is ArtifactClass.GATE_SCRATCH:
            candidate = _plan_scratch_item(item, active_policy, owned_roots)
        elif item.artifact_class is ArtifactClass.EVIDENCE:
            candidate = _plan_evidence_item(item, active_policy, owned_roots, now=moment)
        else:
            candidate = None

        if candidate is None:
            _exclude(item, item.reason)
            continue
        candidates.append(candidate)

    candidates.sort(key=lambda c: (_ACTION_ORDER[c.action], c.artifact_class.value, c.reference))
    exclusions.sort(key=lambda e: (e.artifact_class.value, e.reference))
    estimated: int | None = 0
    running = 0
    for candidate in candidates:
        if not candidate.reclaimable or candidate.estimated_bytes is None:
            estimated = None
            break
        running += candidate.estimated_bytes
    else:
        estimated = running

    return GcPreview(
        generated_at=moment,
        repo_root=inventory.repo_root,
        project_id=inventory.project_id,
        policy_ref=_policy_ref(active_policy, policy_path),
        approved=active_policy.approved,
        scope_classes=tuple(sorted(selected, key=lambda c: c.value)),
        candidates=tuple(candidates),
        exclusions=tuple(exclusions),
        estimated_reclaimable_bytes=estimated,
        preview_id=_preview_id(candidates, _policy_ref(active_policy, policy_path)),
    )


def _plan_worktree_item(
    item: InventoryItem, policy: GcPolicy, owned_roots: Sequence[str], *, now: datetime
) -> GcCandidate | None:
    """Attempt worktrees: terminal candidate, not live, inside owned roots.

    Covers: terminal-candidate retention.  A live owner, an unproved owner, an
    active attempt, a candidate awaiting review, a preempted-but-unterminated
    job, an unknown candidate state or a path outside the owned roots all keep
    the worktree.
    """
    del now  # attempt-worktree eligibility does not depend on elapsed time
    if not policy.allow_worktree_retirement:
        return None
    if item.protected or item.ownership not in (
        OwnershipState.OWNED_TERMINAL,
        OwnershipState.OWNED_ACCEPTED,
    ):
        return None
    if not _owned(item):
        return None
    if _check_filesystem_path(item.reference, owned_roots) is not None:
        return None
    candidate = item.detail.get("candidate_state", "unknown")
    if candidate not in _KNOWN_CANDIDATE_STATES:
        return None
    if item.detail.get("attempt_terminal") != "1":
        return None
    if candidate in _PENDING_CANDIDATE_STATES:
        return None
    if (
        item.detail.get("preempted") == "1"
        and item.detail.get("run_status") not in _TERMINAL_RUN_STATES
    ):
        return None
    if candidate not in _TERMINAL_CANDIDATE_STATES:
        return None
    return GcCandidate(
        artifact_class=item.artifact_class,
        action=GcAction.REMOVE_WORKTREE,
        reference=item.reference,
        identity=item.identity,
        owner=item.owner,
        reason="worktree-terminal-eligible",
        estimated_bytes=item.reclaimable_bytes,
        reclaimable=item.reclaimable_bytes is not None,
        detail=item.detail,
    )


def _plan_branch_item(item: InventoryItem, policy: GcPolicy) -> GcCandidate | None:
    if not policy.allow_branch_retirement:
        return None
    if item.owner is None or item.ownership not in (
        OwnershipState.OWNED_TERMINAL,
        OwnershipState.OWNED_ACCEPTED,
    ):
        return None
    return GcCandidate(
        artifact_class=item.artifact_class,
        action=GcAction.DELETE_BRANCH,
        reference=item.reference,
        identity=item.identity,
        owner=item.owner,
        reason="branch-retirement",
        estimated_bytes=0,
        reclaimable=True,
        detail={**item.detail, "approved_target_ref": policy.approved_target_ref or ""},
    )


def _plan_checkpoint_item(
    item: InventoryItem, policy: GcPolicy, *, now: datetime
) -> GcCandidate | None:
    """Checkpoint refs: job-bound, terminal, retained 7 days after terminal.

    Covers: checkpoint retention and ref-to-job ownership binding.  A ref
    outside the checkpoint namespace, a ref naming another job, a missing or
    naive terminal time, an active or preempted job and the retention window
    all keep the ref.
    """
    if not policy.allow_checkpoint_deletion:
        return None
    if not _owned(item):
        return None
    if _check_ref(item.reference, item.detail.get("job_id") or None) is not None:
        return None
    if item.detail.get("job_terminal") != "1":
        return None
    terminal_at = _parse_iso(item.detail.get("terminal_at"))
    if terminal_at is None:
        return None
    if now < terminal_at + CHECKPOINT_RETENTION_AFTER_TERMINAL:
        return None
    return GcCandidate(
        artifact_class=item.artifact_class,
        action=GcAction.DELETE_CHECKPOINT_REF,
        reference=item.reference,
        identity=item.identity,
        owner=item.owner,
        reason="checkpoint-eligible",
        estimated_bytes=0,
        reclaimable=True,
        detail=item.detail,
    )


def _plan_venv_item(
    item: InventoryItem, policy: GcPolicy, owned_roots: Sequence[str]
) -> GcCandidate | None:
    """Worktree venvs die with their worktree, never alone.

    Covers: a live parent or a parent that stays kept also keeps the venv.
    """
    if not policy.allow_worktree_retirement:
        return None
    if not _owned(item):
        return None
    if _check_filesystem_path(item.reference, owned_roots) is not None:
        return None
    if item.detail.get("parent_worktree_live") == "1":
        return None
    if item.detail.get("parent_eligible") != "1":
        return None
    return GcCandidate(
        artifact_class=item.artifact_class,
        action=GcAction.DELETE_PATH,
        reference=item.reference,
        identity=item.identity,
        owner=item.owner,
        reason="venv-follows-worktree",
        estimated_bytes=item.reclaimable_bytes,
        reclaimable=item.reclaimable_bytes is not None,
        detail=item.detail,
    )


def _plan_owner_db_item(item: InventoryItem, policy: GcPolicy) -> GcCandidate | None:
    if not policy.allow_vacuum:
        return None
    return GcCandidate(
        artifact_class=item.artifact_class,
        action=GcAction.VACUUM_DB,
        reference=item.reference,
        identity=item.identity,
        owner=item.owner,
        reason="vacuum-candidate",
        estimated_bytes=0,
        reclaimable=True,
        detail=item.detail,
    )


def _plan_evidence_item(
    item: InventoryItem, policy: GcPolicy, owned_roots: Sequence[str], *, now: datetime
) -> GcCandidate | None:
    """Evidence: compression under explicit approval; deletion held by default.

    Covers: evidence compression after 7 days from creation; deletion measured
    from terminal time (accepted 90 days, other terminal 30), and only when a
    validated policy settles the retention anchor.  Unproved ownership, an
    active run, an unknown or naive time, a missing size and a path outside the
    owned roots keep the bundle.
    """
    if not _owned(item):
        return None
    if _check_filesystem_path(item.reference, owned_roots) is not None:
        return None
    created_at = _parse_iso(item.detail.get("created_at"))
    terminal_at = _parse_iso(item.detail.get("terminal_at"))
    run_terminal = item.ownership is OwnershipState.OWNED_TERMINAL
    accepted = item.detail.get("run_accepted") == "1"
    if (
        run_terminal
        and created_at is not None
        and terminal_at is not None
        and terminal_at <= now
        and item.size_bytes is not None
        and policy.allow_evidence_deletion
        and policy.evidence_anchor is not None
    ):
        retention = EVIDENCE_ACCEPTED_RETENTION if accepted else EVIDENCE_OTHER_RETENTION
        if now >= terminal_at + retention:
            return GcCandidate(
                artifact_class=item.artifact_class,
                action=GcAction.DELETE_PATH,
                reference=item.reference,
                identity=item.identity,
                owner=item.owner,
                reason="evidence-eligible",
                estimated_bytes=item.reclaimable_bytes,
                reclaimable=item.reclaimable_bytes is not None,
                detail={**item.detail, "evidence_anchor": policy.evidence_anchor},
            )
    # Compression is anchored at creation and independent of the held deletion
    # rule (compress after 7 days).
    if (
        policy.allow_evidence_compression
        and not item.protected
        and run_terminal
        and item.detail.get("compressed") != "1"
        and created_at is not None
        and now >= created_at + EVIDENCE_COMPRESS_AFTER
    ):
        return GcCandidate(
            artifact_class=item.artifact_class,
            action=GcAction.COMPRESS_EVIDENCE,
            reference=item.reference,
            identity=item.identity,
            owner=item.owner,
            reason="evidence-compress-eligible",
            estimated_bytes=item.reclaimable_bytes,
            reclaimable=item.reclaimable_bytes is not None,
            detail={**item.detail, "evidence_anchor": policy.evidence_anchor or ""},
        )
    return None


def _plan_scratch_item(
    item: InventoryItem, policy: GcPolicy, owned_roots: Sequence[str]
) -> GcCandidate | None:
    """Gate scratch: retire only a provably dead owner's scratch, under policy.

    A live or unknown owner is never eligible here; the bounded scratch sweep is
    the maintenance mechanism, and this path is the owner-authorized GC action.
    """
    if not policy.allow_scratch_deletion:
        return None
    if item.ownership is not OwnershipState.OWNED_TERMINAL or item.protected:
        return None
    if not _owned_roots_allow(item.reference, owned_roots):
        return None
    return GcCandidate(
        artifact_class=item.artifact_class,
        action=GcAction.DELETE_PATH,
        reference=item.reference,
        identity=item.identity,
        owner=item.owner,
        reason="scratch-owner-dead",
        estimated_bytes=item.reclaimable_bytes,
        reclaimable=item.reclaimable_bytes is not None,
        detail=item.detail,
    )


def _owned_roots_allow(path: str, owned_roots: Sequence[str]) -> bool:
    try:
        resolved = Path(path).resolve()
    except OSError:
        return False
    for root in owned_roots:
        try:
            root_path = Path(root).resolve()
        except OSError:
            continue
        if root_path == resolved:
            return False
        if resolved.is_relative_to(root_path):
            return True
    return False


@dataclass(frozen=True)
class StorageAuditEvent:
    """One durable audit record (intent, result or failure)."""

    at: datetime
    phase: str
    action: str
    artifact_class: str
    reference: str
    identity: str
    run_id: str | None
    project_id: str | None
    sha: str | None
    policy_ref: str | None
    reason: str
    estimated_bytes: int | None
    actual_bytes: int | None
    detail: Mapping[str, str] = field(default_factory=dict)


AuditSink = Callable[[StorageAuditEvent], None]


@dataclass(frozen=True)
class GcOutcome:
    """Result of executing (or refusing) one previewed candidate."""

    candidate: GcCandidate
    status: str
    reason: str
    actual_bytes: int | None


@dataclass(frozen=True)
class GcApplyResult:
    """The complete result of one ``apply`` invocation."""

    applied: tuple[GcOutcome, ...]
    refused: tuple[GcOutcome, ...]
    audit_uncertain: bool = False


@dataclass(frozen=True)
class LandedCleanupKeep:
    """One attempt worktree the opt-in landed-cleanup step kept."""

    path: str
    run_id: str
    reason: str
    age_days: int | None
    beyond_retention: bool


@dataclass(frozen=True)
class LandedCleanupReport:
    """What one opt-in landed-cleanup sweep did, and why everything else stayed."""

    scanned_runs: int
    removed: tuple[str, ...]
    kept: tuple[LandedCleanupKeep, ...]
    retention_days: int


class _AuditFailure(RuntimeError):
    """Internal marker: an audit write failed after a destructive action."""


def apply_gc(
    preview: GcPreview,
    selection: Sequence[GcCandidate],
    *,
    db: Database,
    repo_root: Path | str,
    audit: AuditSink,
    now: datetime | None = None,
) -> GcApplyResult:
    """Execute an exact, previously previewed selection with revalidation.

    Refuses when the preview is unapproved, when the selection contains a
    candidate that is not exactly in the preview (no widening a stale preview),
    or when any candidate fails revalidation at execution time.
    """
    moment = now if now is not None else datetime.now(UTC)
    repo = Path(repo_root).resolve()
    applied: list[GcOutcome] = []
    refused: list[GcOutcome] = []
    audit_uncertain = False
    if not preview.approved:
        for candidate in selection:
            refused.append(
                GcOutcome(candidate, "refused", "no-standing-policy", None)
            )
        return GcApplyResult(tuple(applied), tuple(refused), False)

    # Canonical authorization: the selection must name exact previewed
    # candidates, including the action and every authority-relevant attribute.
    # Execution always uses the immutable previewed object, never the
    # caller-supplied instance, so a forged or mutated selection cannot change
    # what is executed.
    previewed = {_candidate_key(c): c for c in preview.candidates}
    ordered = sorted(
        selection,
        key=lambda c: (_ACTION_ORDER[c.action], c.artifact_class.value, c.reference),
    )
    seen: set[tuple[str, str, str, str, str]] = set()
    for candidate in ordered:
        key = _candidate_key(candidate)
        canonical = previewed.get(key)
        if canonical is None:
            refused.append(GcOutcome(candidate, "refused", "selection-widens-preview", None))
            continue
        if key in seen:
            refused.append(GcOutcome(canonical, "refused", "duplicate-selection", None))
            continue
        seen.add(key)
        if (
            canonical.artifact_class not in preview.scope_classes
            or not preview.approved
        ):
            refused.append(GcOutcome(canonical, "refused", "policy-not-authorised", None))
            continue
        if canonical.artifact_class is ArtifactClass.OWNED_CACHE:
            refused.append(
                GcOutcome(canonical, "refused", "shared-cache-protected", None)
            )
            continue
        # Intent must be durable BEFORE any destructive call: if it is not,
        # nothing is executed and no "applied" claim is made.
        try:
            _emit(
                audit,
                phase="intent",
                candidate=canonical,
                preview=preview,
                reason=canonical.reason,
                actual_bytes=None,
                at=moment,
            )
        except _AuditFailure:
            refused.append(GcOutcome(canonical, "refused", "audit-intent-failed", None))
            break
        outcome = _dispatch_action(canonical, preview=preview, db=db, repo=repo, now=moment)
        phase = "result" if outcome.status == "applied" else "failure"
        try:
            _emit(
                audit,
                phase=phase,
                candidate=canonical,
                preview=preview,
                reason=outcome.reason,
                actual_bytes=outcome.actual_bytes,
                at=moment,
            )
        except _AuditFailure:
            # The action already ran (or was refused); only its audit result is
            # missing. Tie the uncertainty to the actual outcome, never relabel
            # a refusal as applied, and stop before touching another target.
            audit_uncertain = True
            if outcome.status == "applied":
                applied.append(
                    GcOutcome(canonical, "applied", "audit-uncertain", outcome.actual_bytes)
                )
            else:
                refused.append(
                    GcOutcome(canonical, outcome.status, "audit-uncertain", outcome.actual_bytes)
                )
            break
        if outcome.status == "applied":
            applied.append(outcome)
        else:
            refused.append(outcome)
    return GcApplyResult(tuple(applied), tuple(refused), audit_uncertain)


def _audit(audit: AuditSink, event: StorageAuditEvent) -> None:
    try:
        audit(event)
    except Exception as exc:  # pragma: no cover - defensive boundary
        raise _AuditFailure(str(exc)) from exc


def _emit(
    audit: AuditSink,
    *,
    phase: str,
    candidate: GcCandidate,
    preview: GcPreview,
    reason: str,
    actual_bytes: int | None,
    at: datetime,
) -> None:
    _audit(
        audit,
        StorageAuditEvent(
            at=at,
            phase=phase,
            action=candidate.action.value,
            artifact_class=candidate.artifact_class.value,
            reference=candidate.reference,
            identity=candidate.identity,
            run_id=candidate.owner,
            project_id=preview.project_id,
            sha=candidate.detail.get("commit_sha") or candidate.detail.get("sha") or None,
            policy_ref=preview.policy_ref,
            reason=reason,
            estimated_bytes=candidate.estimated_bytes,
            actual_bytes=actual_bytes,
            detail=dict(candidate.detail),
        ),
    )


def _dispatch_action(
    candidate: GcCandidate,
    preview: GcPreview,
    *,
    db: Database,
    repo: Path,
    now: datetime,
) -> GcOutcome:
    """Execute one canonical previewed candidate. No audit side effects here."""
    if candidate.artifact_class is ArtifactClass.OWNED_CACHE:
        return GcOutcome(candidate, "refused", "shared-cache-protected", None)
    if candidate.action is GcAction.REMOVE_WORKTREE:
        return _revalidate_and_remove_worktree(candidate, db=db, repo=repo, now=now)
    if candidate.action is GcAction.DELETE_BRANCH:
        return _revalidate_and_delete_branch(candidate, db=db, repo=repo)
    if candidate.action is GcAction.DELETE_CHECKPOINT_REF:
        return _revalidate_and_delete_checkpoint(candidate, db=db, repo=repo, now=now)
    if candidate.action is GcAction.DELETE_PATH:
        return _revalidate_and_delete_path(candidate, preview=preview, repo=repo)
    if candidate.action is GcAction.COMPRESS_EVIDENCE:
        return _revalidate_and_compress(candidate, preview=preview)
    if candidate.action is GcAction.VACUUM_DB:
        return _revalidate_and_vacuum(candidate, db=db)
    return GcOutcome(candidate, "refused", "unknown-action", None)


def _authority_digest(candidate: GcCandidate) -> str:
    """Stable digest of every authority-relevant attribute of a candidate.

    Included in the selection identity so a caller cannot keep a preview's
    class/reference/identity while changing the operation or the data an
    operation depends on (policy ref, run, branch tip, target ref...).
    """
    payload = json.dumps(
        {
            "action": candidate.action.value,
            "artifact_class": candidate.artifact_class.value,
            "reference": candidate.reference,
            "identity": candidate.identity,
            "owner": candidate.owner,
            "reason": candidate.reason,
            "estimated_bytes": candidate.estimated_bytes,
            "detail": sorted((str(key), str(value)) for key, value in candidate.detail.items()),
        },
        sort_keys=True,
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def _candidate_key(candidate: GcCandidate) -> tuple[str, str, str, str, str]:
    return (
        candidate.artifact_class.value,
        candidate.action.value,
        candidate.reference,
        candidate.identity,
        _authority_digest(candidate),
    )


def _is_under(path: Path, base: Path) -> bool:
    try:
        return path.is_relative_to(base) and path != base
    except (ValueError, AttributeError):
        return False


def _worktree_base(repo: Path) -> Path:
    return GitManager.get_default_worktree_base(repo).resolve()


def _has_unique_ignored_content(worktree: Path) -> bool:
    """True when the worktree holds ignored content that is NOT proven disposable.

    ``.venv/`` is rebuilt from the shared cache and is disposable; any other
    ignored or untracked unique content keeps the whole worktree.
    """
    from saberops.process import run_process

    res = run_process(["git", "status", "--porcelain", "--ignored"], cwd=worktree)
    if not res.passed:
        return True  # cannot prove disposable -> fail closed
    for raw_line in res.stdout.splitlines():
        if not raw_line.startswith("!! "):
            continue
        ignored = raw_line[3:].strip()
        if not any(
            ignored == prefix.rstrip("/") or ignored.startswith(prefix)
            for prefix in _DISPOSABLE_IGNORED_PREFIXES
        ):
            return True
    return False


def _revalidate_and_remove_worktree(
    candidate: GcCandidate, *, db: Database, repo: Path, now: datetime
) -> GcOutcome:
    del now
    path = Path(candidate.reference)
    try:
        resolved = path.resolve()
    except OSError:
        return GcOutcome(candidate, "refused", "path-unresolvable", None)
    if str(resolved) != candidate.reference:
        return GcOutcome(candidate, "refused", "path-changed", None)
    base = _worktree_base(repo)
    if not _is_under(resolved, base):
        return GcOutcome(candidate, "refused", "outside-worktree-base", None)
    if not resolved.exists():
        return GcOutcome(candidate, "applied", "already-missing", 0)
    if not GitManager.is_clean(resolved):
        return GcOutcome(candidate, "refused", "dirty-worktree", None)
    if _has_unique_ignored_content(resolved):
        return GcOutcome(candidate, "refused", "unique-ignored-content", None)
    run_id = candidate.owner
    if run_id:
        owner = None
        try:
            owner = db.get_execution_owner(run_id)
        except Exception:
            return GcOutcome(candidate, "refused", "ownership-unreadable", None)
        if owner is not None and owner.is_active:
            return GcOutcome(candidate, "refused", "execution-owner-live", None)
    bytes_freed = candidate.estimated_bytes
    try:
        GitManager.remove_worktree(
            repo_path=repo,
            worktree_path=resolved,
            force=False,
            worktree_base=base,
        )
    except RuntimeError as exc:
        return GcOutcome(candidate, "refused", f"git-refused:{exc}"[:200], None)
    return GcOutcome(candidate, "applied", "worktree-removed", bytes_freed)


def _revalidate_and_delete_branch(
    candidate: GcCandidate, *, db: Database, repo: Path
) -> GcOutcome:
    branch = candidate.reference
    current_tip = GitManager.branch_tip(repo, branch)
    if current_tip is None:
        return GcOutcome(candidate, "applied", "already-missing", 0)
    if current_tip != candidate.identity:
        return GcOutcome(candidate, "refused", "branch-tip-changed", None)
    for registration in GitManager.list_worktrees(repo):
        if registration.branch == branch and Path(registration.path).resolve().exists():
            return GcOutcome(candidate, "refused", "branch-worktree-live", None)
    run_id = candidate.owner
    if run_id:
        try:
            owner = db.get_execution_owner(run_id)
        except Exception:
            return GcOutcome(candidate, "refused", "ownership-unreadable", None)
        if owner is not None and owner.is_active:
            return GcOutcome(candidate, "refused", "execution-owner-live", None)
    target_ref = candidate.detail.get("approved_target_ref")
    if target_ref:
        target_tip = GitManager.get_ref_commit(repo, target_ref)
        if target_tip and not GitManager.is_ancestor(repo, current_tip, target_tip):
            return GcOutcome(candidate, "refused", "not-accepted-reachable", None)
    try:
        GitManager.delete_branch(repo, branch, force=False)
    except BranchDeletionRefused as exc:
        return GcOutcome(candidate, "refused", f"git-refused:{exc}"[:200], None)
    return GcOutcome(candidate, "applied", "branch-deleted", 0)


def _revalidate_and_delete_checkpoint(
    candidate: GcCandidate, *, db: Database, repo: Path, now: datetime
) -> GcOutcome:
    del db, now
    ref = candidate.reference
    if not ref.startswith(CHECKPOINT_REF_PREFIX):
        return GcOutcome(candidate, "refused", "invalid-ref", None)
    expected_sha = candidate.detail.get("sha") or candidate.identity
    try:
        GitManager.delete_checkpoint_ref(repo, ref, expected_sha=expected_sha)
    except Exception as exc:
        return GcOutcome(candidate, "refused", f"refusal:{exc}"[:200], None)
    return GcOutcome(candidate, "applied", "checkpoint-ref-deleted", 0)


def _revalidate_and_compress(candidate: GcCandidate, *, preview: GcPreview) -> GcOutcome:
    """Compress one evidence target in place, atomically and non-destructively.

    Two layouts are supported:

    * a regular file ``f`` is gzipped to ``f.gz`` (write temp, fsync, atomic
      ``os.replace``) and only then is the original removed;
    * a directory ``d`` is archived to ``d.tar.gz`` the same way.

    A crash before the final remove leaves the archive and the source; a repeat
    run sees the archive and reports ``already-compressed`` without duplicating
    work, so the operation is idempotent. Readable content and file structure
    are preserved. Deletion is a separate, still-held action.
    """
    path = Path(candidate.reference)
    try:
        resolved = path.resolve()
    except OSError:
        return GcOutcome(candidate, "refused", "path-unresolvable", None)
    if str(resolved) != candidate.reference:
        return GcOutcome(candidate, "refused", "path-changed", None)
    if str(resolved) == preview.repo_root:
        return GcOutcome(candidate, "refused", "root-itself-protected", None)
    if resolved.is_symlink():
        return GcOutcome(candidate, "refused", "symlink-refused", None)
    if not resolved.exists():
        return GcOutcome(candidate, "applied", "already-missing", 0)

    archive = _compress_evidence_path(resolved)
    if archive is None:
        return GcOutcome(candidate, "refused", "compression-unavailable", None)
    if isinstance(archive, str):
        return GcOutcome(candidate, "applied", archive, None)
    freed = archive
    return GcOutcome(candidate, "applied", "evidence-compressed", freed)


def _compress_evidence_path(path: Path) -> int | str | None:
    """Compress *path*; return freed bytes, or a status string, or ``None``.

    ``"already-compressed"`` is returned when the archive already exists and the
    source is gone.
    """
    import gzip
    import tarfile
    import tempfile

    if path.is_dir():
        archive_path = path.with_name(path.name + ".tar.gz")
        if archive_path.exists() and not path.exists():  # pragma: no cover - defensive
            return "already-compressed"
        if archive_path.exists():
            return "already-compressed"
        measure = measure_path_bytes(path)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{archive_path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        os.close(fd)
        tmp_path = Path(tmp_name)
        try:
            with tarfile.open(tmp_path, "w:gz") as tar:
                for member in sorted(path.rglob("*"), key=lambda p: str(p)):
                    tar.add(member, arcname=str(member.relative_to(path)))
            _fsync_file(tmp_path)
            os.replace(tmp_path, archive_path)
            _fsync_dir(archive_path.parent)
            shutil.rmtree(path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
        return measure.apparent_bytes if measure.apparent_bytes is not None else 0

    if path.is_file():
        archive_path = path.with_name(path.name + ".gz")
        if archive_path.exists():
            return "already-compressed"
        before = path.stat().st_size
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{archive_path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as raw, gzip.open(raw, "wb") as gz:
                with path.open("rb") as src:
                    shutil.copyfileobj(src, gz)
            _fsync_file(tmp_path)
            os.replace(tmp_path, archive_path)
            _fsync_dir(archive_path.parent)
            path.unlink()
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
        return before
    return None


def _fsync_file(path: Path) -> None:
    try:
        with path.open("rb") as handle:
            os.fsync(handle.fileno())
    except OSError:
        pass


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _revalidate_and_delete_path(
    candidate: GcCandidate, *, preview: GcPreview, repo: Path
) -> GcOutcome:
    path = Path(candidate.reference)
    try:
        resolved = path.resolve()
    except OSError:
        return GcOutcome(candidate, "refused", "path-unresolvable", None)
    if str(resolved) != candidate.reference:
        return GcOutcome(candidate, "refused", "path-changed", None)
    if str(resolved) == preview.repo_root:
        return GcOutcome(candidate, "refused", "root-itself-protected", None)
    if not resolved.exists():
        return GcOutcome(candidate, "applied", "already-missing", 0)
    if resolved.is_symlink():
        return GcOutcome(candidate, "refused", "symlink-refused", None)
    measure = measure_path_bytes(resolved)
    if measure.hardlinked_files:
        return GcOutcome(candidate, "refused", "shared-bytes-not-reclaimable", None)
    shutil.rmtree(resolved)
    return GcOutcome(candidate, "applied", "path-deleted", measure.apparent_bytes)


def _revalidate_and_vacuum(candidate: GcCandidate, *, db: Database) -> GcOutcome:
    from saberops.sqlite_policy import (
        StorageInfraError,
        free_page_ratio,
        vacuum_database,
    )

    path = Path(candidate.reference)
    if not path.exists():
        return GcOutcome(candidate, "refused", "owner-db-missing", None)
    try:
        ratio = free_page_ratio(path)
    except StorageInfraError:
        return GcOutcome(candidate, "refused", "owner-db-unreadable", None)
    if ratio <= 0.20:
        return GcOutcome(candidate, "refused", "vacuum-not-due", None)
    before = path.stat().st_size
    try:
        vacuum_database(path)
    except StorageInfraError as exc:
        return GcOutcome(candidate, "refused", f"vacuum-infra:{exc.reason_code}", None)
    after = path.stat().st_size
    return GcOutcome(candidate, "applied", "vacuumed", max(0, before - after))


def retire_run_worktrees(
    db: Database,
    *,
    repo_root: Path | str,
    run_id: str,
    worktree_base: Path | str | None = None,
    audit: AuditSink | None = None,
    now: datetime | None = None,
) -> dict[str, list[str]]:
    """Guardedly retire one run's attempt worktrees (the legacy cleanup route).

    This is the single guarded implementation behind both ``orch gc`` and the
    compatibility ``orch cleanup`` command.  It refuses a running run, an
    active execution owner, a worktree outside the worktree base, a symlinked
    worktree, a dirty worktree or one holding unique ignored content.  It never
    runs a repository-wide ``git worktree prune``, never forces, and never
    touches anything outside the resolved worktree base.  Candidate branches
    and database records are preserved.

    Returns the same ``removed``/``skipped_dirty``/``skipped_missing`` keys the
    legacy route returned, plus ``skipped_protected``.
    """
    moment = now if now is not None else datetime.now(UTC)
    repo = Path(repo_root).resolve()
    run = db.get_run(run_id)
    if run is None:
        raise ValueError(f"Run '{run_id}' not found")
    status = run.status if isinstance(run.status, RunStatus) else RunStatus(str(run.status))
    if status is RunStatus.RUNNING:
        raise RuntimeError(f"Cannot clean up run '{run_id}': status is RUNNING")
    try:
        owner = db.get_execution_owner(run_id)
    except Exception:
        owner = None
    if owner is not None and owner.is_active:
        raise RuntimeError(f"Cannot clean up run '{run_id}': an execution owner is active")

    base = (
        Path(worktree_base).resolve()
        if worktree_base is not None
        else GitManager.get_default_worktree_base(repo).resolve()
    )
    removed: list[str] = []
    skipped_dirty: list[str] = []
    skipped_missing: list[str] = []
    skipped_protected: list[str] = []
    for attempt in db.get_attempts_for_run(run_id):
        if not attempt.worktree_path:
            continue
        wt_path = Path(attempt.worktree_path).resolve()
        if not wt_path.exists():
            skipped_missing.append(str(wt_path))
            continue
        if not _is_under(wt_path, base):
            raise RuntimeError(
                f"Refusing cleanup: worktree '{wt_path}' does not belong "
                f"to orchestrator worktree root '{base}'"
            )
        if wt_path.is_symlink() or _has_unique_ignored_content(wt_path):
            skipped_protected.append(str(wt_path))
            continue
        if not GitManager.is_clean(wt_path):
            skipped_dirty.append(str(wt_path))
            continue
        try:
            GitManager.remove_worktree(
                repo_path=repo,
                worktree_path=wt_path,
                force=False,
                worktree_base=base,
            )
        except RuntimeError:
            skipped_protected.append(str(wt_path))
            continue
        removed.append(str(wt_path))
    if audit is not None:
        for path in removed:
            audit(
                StorageAuditEvent(
                    at=moment,
                    phase="result",
                    action=GcAction.REMOVE_WORKTREE.value,
                    artifact_class=ArtifactClass.ATTEMPT_WORKTREE.value,
                    reference=path,
                    identity=path,
                    run_id=run_id,
                    project_id=None,
                    sha=None,
                    policy_ref=None,
                    reason="legacy-cleanup-removed",
                    estimated_bytes=None,
                    actual_bytes=None,
                )
            )
    return {
        "removed": removed,
        "skipped_dirty": skipped_dirty,
        "skipped_missing": skipped_missing,
        "skipped_protected": skipped_protected,
    }


def _same_repo_path(candidate: str | None, repo: Path) -> bool:
    """True when a persisted target path resolves to this exact repository."""
    if not candidate:
        return False
    try:
        return Path(candidate).resolve() == repo
    except OSError:
        return False


def _attempt_worktree_path(attempt: Attempt) -> Path | None:
    if not attempt.worktree_path:
        return None
    try:
        path = Path(attempt.worktree_path).resolve()
    except OSError:
        return None
    return path if path.exists() else None


def _acceptance_target_head(db: Database, run_id: str, repo: Path) -> str | None:
    """Resolve the target branch recorded by the latest acceptance event.

    Falls back to the repository's current ``HEAD`` only when the event
    carries no usable branch.  ``None`` means the landing cannot be proven.
    """
    branch: str | None = None
    for event in db.get_events_by_type(run_id, "run_accepted"):
        payload = event.payload
        if not isinstance(payload, dict):
            continue
        raw = payload.get("final_branch")
        if isinstance(raw, str) and raw:
            branch = raw
    if branch is not None:
        head = GitManager.get_ref_commit(repo, f"refs/heads/{branch}")
        if head:
            return head
    return GitManager.get_ref_commit(repo, "HEAD")


def _landed_candidate(db: Database, run: Run, repo: Path) -> tuple[str | None, str | None]:
    """Return ``(accepted_sha, None)`` for a verified-landed run, else a keep reason."""
    status = run.status if isinstance(run.status, RunStatus) else RunStatus(str(run.status))
    if status is RunStatus.RUNNING:
        return None, "run-active"
    try:
        owner = db.get_execution_owner(run.id)
    except Exception:
        owner = None
    if owner is not None and owner.is_active:
        return None, "run-active"
    try:
        projection = project_candidate_state(db, run.id)
    except Exception:
        return None, "candidate-unreadable"
    if projection.state is not CandidateState.ACCEPTED:
        state = projection.state.value if projection.state is not None else "unknown"
        return None, f"candidate-not-accepted:{state}"
    candidate_sha = projection.candidate_sha
    if not candidate_sha:
        return None, "candidate-unreadable"
    head = _acceptance_target_head(db, run.id, repo)
    if head is None:
        return None, "target-head-unreadable"
    if not GitManager.is_ancestor(repo, candidate_sha, head):
        return None, "candidate-not-landed"
    return candidate_sha, None


def _retire_landed_worktree(repo: Path, worktree: Path, base: Path) -> str | None:
    """Apply the existing guards to one worktree; return a keep reason or ``None``."""
    if not _is_under(worktree, base):
        return "outside-worktree-base"
    if worktree.is_symlink() or _has_unique_ignored_content(worktree):
        return "protected-ignored-content"
    if not GitManager.is_clean(worktree):
        return "dirty"
    try:
        GitManager.remove_worktree(
            repo_path=repo,
            worktree_path=worktree,
            force=False,
            worktree_base=base,
        )
    except RuntimeError:
        return "removal-refused"
    return None


def _keep_reason_report(
    attempt: Attempt,
    *,
    run_id: str,
    reason: str,
    age_days: int | None,
) -> LandedCleanupKeep:
    path = _attempt_worktree_path(attempt)
    reference = str(path) if path is not None else str(attempt.worktree_path)
    beyond = age_days is not None and age_days >= LANDED_CLEANUP_RETENTION_DAYS
    return LandedCleanupKeep(
        path=reference,
        run_id=run_id,
        reason=reason,
        age_days=age_days,
        beyond_retention=beyond,
    )


def _run_age_days(run: Run, moment: datetime) -> int | None:
    anchor = _parse_iso(run.completed_at or run.created_at)
    if anchor is None:
        return None
    delta = moment - anchor
    if delta.total_seconds() < 0:
        return 0
    return delta.days


def retire_landed_worktrees(
    db: Database,
    *,
    repo_root: Path | str,
    worktree_base: Path | str | None = None,
    audit: AuditSink | None = None,
    now: datetime | None = None,
    max_runs: int = DEFAULT_MAX_RUNS,
) -> LandedCleanupReport:
    """Opt-in retirement of clean, verified-landed attempt worktrees.

    A run is *verified landed* only when its candidate projection reports
    ``ACCEPTED`` and that accepted candidate SHA is still an ancestor of the
    target branch named by its latest ``run_accepted`` event.  Only worktrees
    that hold that exact landed candidate are considered, and each one must
    still pass the existing guards: not the worktree base itself, not a
    symlink, no unique ignored content, a clean working tree and no active
    execution owner.  Failed, dirty, active, unaccepted and unlanded copies
    are never removed; they are reported against
    :data:`LANDED_CLEANUP_RETENTION_DAYS` so the owner can decide.

    Removal uses the same non-forcing native ``git worktree remove`` as
    :func:`retire_run_worktrees`; there is no repository-wide prune and no
    branch or database record is touched.  This never runs automatically.
    """
    moment = now if now is not None else datetime.now(UTC)
    repo = Path(repo_root).resolve()
    base = (
        Path(worktree_base).resolve()
        if worktree_base is not None
        else GitManager.get_default_worktree_base(repo).resolve()
    )
    removed: list[str] = []
    kept: list[LandedCleanupKeep] = []
    scanned_runs = 0
    for run in db.list_runs(limit=max_runs):
        if not _same_repo_path(run.target_repo, repo):
            continue
        worktree_attempts = [
            attempt
            for attempt in db.get_attempts_for_run(run.id)
            if _attempt_worktree_path(attempt) is not None
        ]
        if not worktree_attempts:
            continue
        scanned_runs += 1
        age_days = _run_age_days(run, moment)
        accepted_sha, keep_reason = _landed_candidate(db, run, repo)
        if accepted_sha is None:
            for attempt in worktree_attempts:
                kept.append(
                    _keep_reason_report(
                        attempt,
                        run_id=run.id,
                        reason=keep_reason or "candidate-unreadable",
                        age_days=age_days,
                    )
                )
            continue
        for attempt in worktree_attempts:
            if attempt.commit_sha != accepted_sha:
                kept.append(
                    _keep_reason_report(
                        attempt,
                        run_id=run.id,
                        reason="not-the-landed-candidate",
                        age_days=age_days,
                    )
                )
                continue
            worktree = _attempt_worktree_path(attempt)
            if worktree is None:
                continue
            reason = _retire_landed_worktree(repo, worktree, base)
            if reason is not None:
                kept.append(
                    _keep_reason_report(
                        attempt,
                        run_id=run.id,
                        reason=reason,
                        age_days=age_days,
                    )
                )
                continue
            reference = str(worktree)
            removed.append(reference)
            if audit is not None:
                audit(
                    StorageAuditEvent(
                        at=moment,
                        phase="result",
                        action=GcAction.REMOVE_WORKTREE.value,
                        artifact_class=ArtifactClass.ATTEMPT_WORKTREE.value,
                        reference=reference,
                        identity=reference,
                        run_id=run.id,
                        project_id=None,
                        sha=accepted_sha,
                        policy_ref=None,
                        reason="landed-cleanup-removed",
                        estimated_bytes=None,
                        actual_bytes=None,
                    )
                )
    return LandedCleanupReport(
        scanned_runs=scanned_runs,
        removed=tuple(removed),
        kept=tuple(kept),
        retention_days=LANDED_CLEANUP_RETENTION_DAYS,
    )
# ---------------------------------------------------------------------------
# Bounded scheduled maintenance
# ---------------------------------------------------------------------------

GB: Final[int] = 1024**3
DEFAULT_DISK_FLOOR_GB: Final[float] = 10.0
MAINTENANCE_INTERVAL: Final[timedelta] = timedelta(hours=1)
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

    This is the ``max(min_gb, 10 %)`` guard reimplemented as a
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
        anchor = _nearest_existing(path)
        if anchor is None:
            return False
        try:
            usage = _statvfs(anchor)
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


def _nearest_existing(path: Path | str) -> Path | None:
    """Return *path* or its nearest existing ancestor, or ``None``.

    A not-yet-created state directory must not be read as an *unknown* disk: its
    capacity is the capacity of the filesystem that will hold it.
    """
    candidate = Path(path)
    for _ in range(64):
        if candidate.exists():
            return candidate
        parent = candidate.parent
        if parent == candidate:
            return None
        candidate = parent
    return None


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
            anchor = _nearest_existing(path)
            if anchor is None:
                return DiskAssessment(DiskState.UNKNOWN, None, None, resolved, "no-anchor")
            try:
                free_bytes, total_bytes = _statvfs(anchor)
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
            classes=(
                ArtifactClass.ATTEMPT_WORKTREE,
                ArtifactClass.LOCAL_BRANCH,
                ArtifactClass.GATE_SCRATCH,
            ),
        )
        applied = 0
        refused = 0
        ran_gc = False
        vacuumed = False

        # A below-floor or unknown reading is remediated with bounded permitted
        # GC and then re-measured, for every trigger -- not only the explicit
        # DISK_LOW one.  Startup/periodic are where the floor actually gated a
        # launch, so their assessment must propagate infra too.
        low = disk_before.state is not DiskState.OK

        if (self._clock() - started) < self._tick_budget and (
            lease_valid and policy.approved and preview.candidates
        ):
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

        if (
            (self._clock() - started) < self._tick_budget
            and lease_valid
            and policy.approved
            and policy.allow_vacuum
        ):
            vacuumed = self._maybe_vacuum()

        swept: tuple[str, ...] = ()
        if (
            (self._clock() - started) < self._tick_budget
            and lease_valid
            and policy.approved
            and policy.allow_scratch_deletion
        ):
            swept = self._sweep_gate_scratch(deadline=started + self._tick_budget)

        infra_reason: str | None = None
        disk_after: DiskAssessment | None = None
        if low:
            disk_after = self.assess_disk(target_paths)
            if disk_after.state is DiskState.LOW:
                infra_reason = "DISK_LOW"
            elif disk_after.state is DiskState.UNKNOWN:
                infra_reason = "DISK_UNKNOWN"
        elif trigger is MaintenanceTrigger.DISK_LOW:
            disk_after = disk_before

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

    def launch_guard_reason(self, *, paths: Sequence[Path | str] | None = None) -> str | None:
        """Return a typed infra reason when a launch must be parked, else None.

        The non-raising form of :meth:`preflight_launch`, used by the scheduler
        dispatch guard and the gate path so a below-floor or unknown reading
        blocks the expensive launch before any side effect.
        """
        target = tuple(paths) if paths is not None else self.storage_paths()
        assessment = self.assess_disk(target)
        if assessment.state is DiskState.LOW:
            return "DISK_LOW"
        if assessment.state is DiskState.UNKNOWN:
            return "DISK_UNKNOWN"
        return None

    # -- gate scratch sweep -------------------------------------------------

    def _sweep_gate_scratch(self, *, deadline: float | None = None) -> tuple[str, ...]:
        """Sweep provably dead-owned gate scratch (bounded, policy-gated)."""
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
            removed.extend(
                sweep_gate_scratch(
                    root,
                    deadline=deadline,
                    clock=self._clock,
                )
            )
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
