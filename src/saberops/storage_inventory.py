"""Real owned-artifact inventory for the GS-1 storage lifecycle service.

This module is the read-only front half of Step 008 / GS-1: it answers *what
does SaberOps actually own on this host, and what does its own durable state
say about each object?*  It never deletes anything, never writes the owner
database, and never writes the filesystem.  ``orch gc --dry-run`` and the
scheduled maintenance pass both consume it.

Authority rules (ADR-0003 Amendment 1 §1, §2):

* Ownership comes from real persisted provenance -- run/attempt rows, the
  candidate lifecycle projection, the execution-owner lease table and the job
  table -- never from a branch name, an age, a PID or an SHA that merely
  happens to be an ancestor of main.
* Anything the durable state cannot explain is :data:`OwnershipState.UNKNOWN`
  and must be kept with a reason.  A missing registration or a missing path is
  never justification to prune.
* Byte accounting is conservative: a directory containing any hard-linked file
  reports ``reclaimable_bytes = None`` ("not definitely reclaimable") because
  shared bytes must never be summed as recoverable.

The inventory is bounded: it scans at most ``max_runs`` runs and at most
``max_entries`` filesystem entries per artifact, and reports truncation rather
than silently walking an unbounded tree.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final

from saberops.candidate_lifecycle import project_candidate_state
from saberops.db import Database
from saberops.git import CHECKPOINT_REF_PREFIX, GitManager
from saberops.models import (
    TERMINAL_RUN_STATUSES,
    AttemptStatus,
    CandidateState,
    Run,
    RunStatus,
)
from saberops.ownership import Liveness, probe_liveness
from saberops.project import ProjectIdentityError, compute_project_identity
from saberops.sqlite_policy import read_connection

__all__ = [
    "ArtifactClass",
    "ByteMeasure",
    "Consumer",
    "Inventory",
    "InventoryItem",
    "OwnershipState",
    "build_inventory",
    "measure_path_bytes",
]

#: Upper bound on filesystem entries walked per artifact.
MAX_WALK_ENTRIES: Final[int] = 500_000
#: Default upper bound on runs inspected for one repository.
DEFAULT_MAX_RUNS: Final[int] = 2_000

#: Git branch namespace SaberOps creates for engine attempts.
_ORCH_BRANCH_PREFIX: Final[str] = "orch/"


class ArtifactClass(StrEnum):
    """D6 artifact classes plus the Git lifecycle objects Amendment 1 adds."""

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

    Covers: ADR-0003 Amendment 1 §5 (reclaimable-byte estimates account for
    hard-linked package caches), D6 (unknown size fails closed).
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
    A path equal to one of these roots is never eligible (the pure planner
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

    Covers: ADR-0003 D6 (owned caches are inventoried and protected).
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
