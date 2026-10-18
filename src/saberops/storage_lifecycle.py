"""Deterministic storage-retention / GC-eligibility planner (early Step 008 slice).

Parent Step 008 (GS-1) is INCOMPLETE: this module is a pure, read-only
planner over a caller-supplied typed artifact inventory. It performs no
filesystem walks, no deletions, no database mutation, no subprocess calls,
and no CLI or scheduler integration. It only answers "what would the
accepted retention policy allow?" so a future GC service can execute it.

Accepted policy implemented here (ADR-0003 D4/D6, Q1/Q4; frozen values):

- Evidence: compress after 7 days (anchored at creation, non-destructive);
  deletion only for terminal runs with an explicit timezone-aware terminal
  timestamp, measured from terminal time: accepted runs 90 days, other
  terminal runs 30 days (owner-configurable via :func:`plan_gc` arguments).
  The terminal anchor is an UNAPPROVED conservative retention safety floor,
  not an ADR decision: ADR-0003 D6/Q4 does not settle the anchor, and the
  owner must rule on the canonical anchor before any GC execution.
- Checkpoint refs (``refs/saberops/checkpoints/<job_id>/<n>``): retained
  until 7 days after the owning job is terminal. The ref's embedded job
  segment must equal the caller-supplied ``job_id`` exactly; terminal
  metadata belonging to a different job never authorizes deletion.
- Gate scratch: eligible at end of gate; on failure only the newest scratch
  per run is kept, capped by total size (default 2 GB); older ones eligible.
- Attempt worktrees: eligible only when the attempt is terminal and its
  candidate is accepted, rejected, or superseded; kept while a candidate
  awaits review/acceptance or while the job is preempted but not terminal
  (recreatable from the checkpoint ref once terminal).
- Worktree venvs die with their worktree (eligible only when the parent
  worktree is gone or itself eligible for removal).
- Owner DB: ``VACUUM`` eligible weekly when the free-page ratio exceeds 20 %.
- Unowned items are NEVER eligible for deletion in this slice. Orphan
  reclamation (ADR-0003 D6) needs engine ownership records that do not exist
  yet (Steps 001/002); until then unknown ownership fails closed to KEEP.

Fail-closed rules: unknown, missing, non-enum or unrecognized ownership
(only an exact :class:`OwnerState` member authorizes a destructive action),
missing required metadata, naive (timezone-unaware) timestamps, paths outside
or exactly equal to the caller-declared owned roots (a path identical to an
owned root is never eligible: ``root-itself-protected``), malformed owned
roots (empty/NUL/relative/traversal/filesystem root are ignored), ``..`` path
escapes, caller-flagged symlink escapes, pending review, live worktrees,
preempted-but-unterminated jobs, and unknown enum states all produce KEEP with
a machine-readable reason. Every decision echoes the caller-supplied owner
identity and a reason code.

The caller-supplied dataclass shapes in this module are explicit and
provisional: no persisted owner table or schema is invented here. They stay
provisional until Steps 001/002 stabilize the storage interfaces.

DEFERRED (not in this slice): the GC service itself, disk/SQLite wiring,
``orch gc`` CLI, hourly/on-DISK_LOW daemon runs, disk-floor admission, and
SQLite robustness (D7). Do not wire this planner into any scheduler or
admission path.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import PurePosixPath

__all__ = [
    "CHECKPOINT_REF_PREFIX",
    "CHECKPOINT_RETENTION_AFTER_TERMINAL",
    "EVIDENCE_ACCEPTED_RETENTION",
    "EVIDENCE_COMPRESS_AFTER",
    "EVIDENCE_OTHER_RETENTION",
    "FAILED_SCRATCH_SIZE_CAP_BYTES",
    "OWNER_DB_VACUUM_FREE_PAGE_RATIO",
    "OWNER_DB_VACUUM_INTERVAL",
    "Action",
    "Artifact",
    "ArtifactKind",
    "AttemptWorktree",
    "CandidateState",
    "CheckpointRef",
    "DeletionEvent",
    "EvidenceBundle",
    "GateScratch",
    "GcPlan",
    "OwnerDatabase",
    "OwnerState",
    "PlanDecision",
    "WorktreeVenv",
    "plan_gc",
]

# Frozen accepted-policy values (ADR-0003 D6 + Q1/Q4).
EVIDENCE_COMPRESS_AFTER = timedelta(days=7)
EVIDENCE_ACCEPTED_RETENTION = timedelta(days=90)
EVIDENCE_OTHER_RETENTION = timedelta(days=30)
CHECKPOINT_RETENTION_AFTER_TERMINAL = timedelta(days=7)
FAILED_SCRATCH_SIZE_CAP_BYTES = 2 * 1024**3
OWNER_DB_VACUUM_INTERVAL = timedelta(days=7)
OWNER_DB_VACUUM_FREE_PAGE_RATIO = 0.20
CHECKPOINT_REF_PREFIX = "refs/saberops/checkpoints/"


class ArtifactKind(StrEnum):
    """D6 artifact classes this planner can judge."""

    GATE_SCRATCH = "gate-scratch"
    ATTEMPT_WORKTREE = "attempt-worktree"
    CHECKPOINT_REF = "checkpoint-ref"
    EVIDENCE = "evidence"
    WORKTREE_VENV = "worktree-venv"
    OWNER_DB = "owner-db"


class OwnerState(StrEnum):
    """Caller-supplied ownership knowledge; UNKNOWN must fail closed."""

    KNOWN_LIVE = "known-live"
    KNOWN_DEAD = "known-dead"
    UNKNOWN = "unknown"


class CandidateState(StrEnum):
    """Candidate lifecycle state for an attempt worktree."""

    ACTIVE = "active"
    PENDING_REVIEW = "pending-review"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"
    UNKNOWN = "unknown"


class Action(StrEnum):
    """Planner verdict for one artifact."""

    KEEP = "keep"
    ELIGIBLE_DELETE = "eligible-delete"
    ELIGIBLE_COMPRESS = "eligible-compress"
    ELIGIBLE_VACUUM = "eligible-vacuum"


@dataclass(frozen=True)
class GateScratch:
    """One gate-run scratch directory as described by the caller."""

    artifact_id: str
    path: str
    owner: str | None
    owner_state: OwnerState
    size_bytes: int | None
    gate_finished: bool | None
    gate_succeeded: bool | None
    is_newest_failed: bool | None
    is_symlink_escape: bool = False


@dataclass(frozen=True)
class AttemptWorktree:
    """One attempt worktree as described by the caller."""

    artifact_id: str
    path: str
    owner: str | None
    owner_state: OwnerState
    attempt_terminal: bool | None
    candidate_state: CandidateState
    job_preempted: bool | None
    job_terminal: bool | None
    worktree_live: bool | None
    is_symlink_escape: bool = False


@dataclass(frozen=True)
class CheckpointRef:
    """One checkpoint ref as described by the caller (not a filesystem path)."""

    artifact_id: str
    ref: str
    owner: str | None
    owner_state: OwnerState
    job_id: str | None
    job_terminal: bool | None
    terminal_at: datetime | None
    job_preempted: bool | None


@dataclass(frozen=True)
class EvidenceBundle:
    """One run-evidence bundle as described by the caller.

    ``terminal_at`` is an optional transitional caller input (default None):
    deletion of terminal evidence is measured from it, never from creation,
    as an UNAPPROVED conservative safety floor until the owner settles the
    canonical retention anchor. Creation time still guides non-destructive
    compression.
    """

    artifact_id: str
    path: str
    owner: str | None
    owner_state: OwnerState
    run_terminal: bool | None
    run_accepted: bool | None
    created_at: datetime | None
    terminal_at: datetime | None = None
    compressed: bool | None = None
    size_bytes: int | None = None
    is_symlink_escape: bool = False


@dataclass(frozen=True)
class WorktreeVenv:
    """One worktree venv; its fate follows its parent worktree."""

    artifact_id: str
    path: str
    owner: str | None
    owner_state: OwnerState
    parent_worktree_live: bool | None
    parent_eligible_for_removal: bool | None
    is_symlink_escape: bool = False


@dataclass(frozen=True)
class OwnerDatabase:
    """The owner DB file; the only eligible action is VACUUM, never delete."""

    artifact_id: str
    path: str
    owner: str | None
    owner_state: OwnerState
    last_vacuum_at: datetime | None
    free_page_ratio: float | None
    is_symlink_escape: bool = False


Artifact = (
    GateScratch | AttemptWorktree | CheckpointRef | EvidenceBundle | WorktreeVenv | OwnerDatabase
)


@dataclass(frozen=True)
class PlanDecision:
    """Verdict for one artifact; always carries owner identity and reason."""

    artifact_id: str
    kind: ArtifactKind
    action: Action
    reason: str
    owner: str | None


@dataclass(frozen=True)
class DeletionEvent:
    """Log row for one eligible deletion (class, path, bytes, reason)."""

    ts: datetime
    artifact_class: str
    path: str
    bytes: int | None
    reason: str


@dataclass(frozen=True)
class GcPlan:
    """Deterministic plan output; decisions sorted by (kind, artifact_id)."""

    planned_at: datetime
    decisions: tuple[PlanDecision, ...]
    deletion_log: tuple[DeletionEvent, ...]


def _owner_ok(owner: str | None, state: object) -> bool:
    """Covers: fail-closed ownership (ADR-0003 D6 orphan rule, provisional).

    Ownership is authorized only by an exact :class:`OwnerState` member that
    is not ``UNKNOWN``. Raw strings (``"unknown"``, ``"known-dead"``),
    foreign or deserialized values, ``None`` and the ``UNKNOWN`` sentinel all
    fail closed, so a corrupt inventory rebuilt from JSON/SQLite can never
    turn an unowned item into an eligible action. A plain string compares
    equal to a ``StrEnum`` member, so an identity comparison alone is not a
    sufficient ownership gate.
    """
    if not isinstance(state, OwnerState) or state is OwnerState.UNKNOWN:
        return False
    return owner is not None and owner != ""


def _usable_root(root: str) -> PurePosixPath | None:
    """Return a usable owned root, or None when the root must be discarded.

    Covers: malformed owned roots fail closed. Empty, NUL-bearing, relative,
    traversal (``..``) and filesystem-root (``/``) roots are ignored so they
    can never confer eligibility on arbitrary paths. This stays within the
    existing lexical parser: no new root source or resolution is introduced.
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
    self-protection (ADR-0003 D6 root confinement). A path equal to an owned
    root is never eligible (``root-itself-protected``); only a strict
    descendant of a usable owned root is. Purely lexical: blind to symlink,
    mount/bind, and TOCTOU escapes. Eligible verdicts are policy proposals
    only, never deletion authorization; only a future GC executor that
    resolves paths against validated owner roots at execution time may act.
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

    Covers: checkpoint ref↔job ownership binding (ADR-0003 D6 "the OWNING
    job"). Terminal metadata belonging to a different job must never
    authorize deletion, and a merely valid prefix never implies ownership.
    """
    if ref == "":
        return "missing-metadata"
    if "\x00" in ref:
        return "invalid-ref"
    if ".." in PurePosixPath(ref).parts:
        return "ref-escape"
    if not ref.startswith(CHECKPOINT_REF_PREFIX):
        return "outside-roots"
    segments = ref[len(CHECKPOINT_REF_PREFIX):].split("/")
    if len(segments) != 2 or segments[0] == "" or segments[1] == "":
        return "invalid-ref"
    if job_id is None or job_id == "" or "/" in job_id:
        return "missing-metadata"
    if segments[0] != job_id:
        return "ref-job-mismatch"
    return None


def _keep(artifact_id: str, kind: ArtifactKind, reason: str, owner: str | None) -> PlanDecision:
    return PlanDecision(
        artifact_id=artifact_id, kind=kind, action=Action.KEEP, reason=reason, owner=owner
    )


def _plan_scratch(
    item: GateScratch,
    *,
    now: datetime,
    owned_roots: Sequence[str],
    failed_scratch_cap_bytes: int,
) -> PlanDecision:
    """Covers: ADR-0003 D4/D6 gate-scratch retention (end of gate, newest-kept)."""
    _ = now
    reason = _check_filesystem_path(item.path, owned_roots)
    if reason is not None:
        return _keep(item.artifact_id, ArtifactKind.GATE_SCRATCH, reason, item.owner)
    if item.is_symlink_escape:
        return _keep(item.artifact_id, ArtifactKind.GATE_SCRATCH, "symlink-escape", item.owner)
    if item.gate_finished is None or item.gate_succeeded is None:
        return _keep(item.artifact_id, ArtifactKind.GATE_SCRATCH, "missing-metadata", item.owner)
    if not item.gate_finished:
        return _keep(item.artifact_id, ArtifactKind.GATE_SCRATCH, "gate-running", item.owner)
    if item.gate_succeeded:
        return PlanDecision(
            artifact_id=item.artifact_id,
            kind=ArtifactKind.GATE_SCRATCH,
            action=Action.ELIGIBLE_DELETE,
            reason="scratch-succeeded-eligible",
            owner=item.owner,
        )
    if item.is_newest_failed is None:
        return _keep(item.artifact_id, ArtifactKind.GATE_SCRATCH, "missing-metadata", item.owner)
    if not item.is_newest_failed:
        return PlanDecision(
            artifact_id=item.artifact_id,
            kind=ArtifactKind.GATE_SCRATCH,
            action=Action.ELIGIBLE_DELETE,
            reason="scratch-superseded-eligible",
            owner=item.owner,
        )
    if item.size_bytes is None:
        return _keep(item.artifact_id, ArtifactKind.GATE_SCRATCH, "missing-metadata", item.owner)
    if item.size_bytes > failed_scratch_cap_bytes:
        return PlanDecision(
            artifact_id=item.artifact_id,
            kind=ArtifactKind.GATE_SCRATCH,
            action=Action.ELIGIBLE_DELETE,
            reason="scratch-cap-exceeded",
            owner=item.owner,
        )
    return _keep(item.artifact_id, ArtifactKind.GATE_SCRATCH, "newest-failed-kept", item.owner)


def _plan_worktree(
    item: AttemptWorktree, *, now: datetime, owned_roots: Sequence[str]
) -> PlanDecision:
    """Covers: ADR-0003 D6 attempt-worktree retention (pending/preempted kept)."""
    _ = now
    reason = _check_filesystem_path(item.path, owned_roots)
    if reason is not None:
        return _keep(item.artifact_id, ArtifactKind.ATTEMPT_WORKTREE, reason, item.owner)
    if item.is_symlink_escape:
        return _keep(item.artifact_id, ArtifactKind.ATTEMPT_WORKTREE, "symlink-escape", item.owner)
    if (
        item.attempt_terminal is None
        or item.job_preempted is None
        or item.job_terminal is None
        or item.worktree_live is None
        or item.candidate_state is CandidateState.UNKNOWN
    ):
        return _keep(
            item.artifact_id, ArtifactKind.ATTEMPT_WORKTREE, "missing-metadata", item.owner
        )
    if item.worktree_live:
        return _keep(item.artifact_id, ArtifactKind.ATTEMPT_WORKTREE, "worktree-live", item.owner)
    if not item.attempt_terminal:
        return _keep(
            item.artifact_id, ArtifactKind.ATTEMPT_WORKTREE, "attempt-active", item.owner
        )
    if item.candidate_state in (CandidateState.PENDING_REVIEW, CandidateState.ACTIVE):
        return _keep(
            item.artifact_id, ArtifactKind.ATTEMPT_WORKTREE, "candidate-pending", item.owner
        )
    if item.job_preempted and not item.job_terminal:
        return _keep(item.artifact_id, ArtifactKind.ATTEMPT_WORKTREE, "job-preempted", item.owner)
    if item.candidate_state in (
        CandidateState.ACCEPTED,
        CandidateState.REJECTED,
        CandidateState.SUPERSEDED,
    ):
        return PlanDecision(
            artifact_id=item.artifact_id,
            kind=ArtifactKind.ATTEMPT_WORKTREE,
            action=Action.ELIGIBLE_DELETE,
            reason="worktree-terminal-eligible",
            owner=item.owner,
        )
    return _keep(item.artifact_id, ArtifactKind.ATTEMPT_WORKTREE, "missing-metadata", item.owner)


def _plan_checkpoint(
    item: CheckpointRef, *, now: datetime, checkpoint_retention: timedelta
) -> PlanDecision:
    """Covers: ADR-0003 D6 checkpoint refs (job terminal + 7 days, job-bound)."""
    reason = _check_ref(item.ref, item.job_id)
    if reason is not None:
        return _keep(item.artifact_id, ArtifactKind.CHECKPOINT_REF, reason, item.owner)
    if item.job_terminal is None or item.job_preempted is None:
        return _keep(item.artifact_id, ArtifactKind.CHECKPOINT_REF, "missing-metadata", item.owner)
    if not item.job_terminal:
        if item.job_preempted:
            return _keep(
                item.artifact_id, ArtifactKind.CHECKPOINT_REF, "job-preempted", item.owner
            )
        return _keep(
            item.artifact_id, ArtifactKind.CHECKPOINT_REF, "checkpoint-job-active", item.owner
        )
    if item.terminal_at is None:
        return _keep(item.artifact_id, ArtifactKind.CHECKPOINT_REF, "missing-metadata", item.owner)
    if item.terminal_at.tzinfo is None:
        return _keep(item.artifact_id, ArtifactKind.CHECKPOINT_REF, "invalid-time", item.owner)
    if now < item.terminal_at + checkpoint_retention:
        return _keep(
            item.artifact_id, ArtifactKind.CHECKPOINT_REF, "checkpoint-retained", item.owner
        )
    return PlanDecision(
        artifact_id=item.artifact_id,
        kind=ArtifactKind.CHECKPOINT_REF,
        action=Action.ELIGIBLE_DELETE,
        reason="checkpoint-eligible",
        owner=item.owner,
    )


def _plan_evidence(
    item: EvidenceBundle,
    *,
    now: datetime,
    owned_roots: Sequence[str],
    compress_after: timedelta,
    accepted_retention: timedelta,
    other_retention: timedelta,
) -> PlanDecision:
    """Covers: ADR-0003 D6/Q4 evidence (compress 7d; delete from terminal time)."""
    reason = _check_filesystem_path(item.path, owned_roots)
    if reason is not None:
        return _keep(item.artifact_id, ArtifactKind.EVIDENCE, reason, item.owner)
    if item.is_symlink_escape:
        return _keep(item.artifact_id, ArtifactKind.EVIDENCE, "symlink-escape", item.owner)
    if item.run_terminal is None or item.created_at is None or item.compressed is None:
        return _keep(item.artifact_id, ArtifactKind.EVIDENCE, "missing-metadata", item.owner)
    if item.created_at.tzinfo is None:
        return _keep(item.artifact_id, ArtifactKind.EVIDENCE, "invalid-time", item.owner)
    if not item.run_terminal:
        return _keep(item.artifact_id, ArtifactKind.EVIDENCE, "evidence-active", item.owner)
    if item.run_accepted is None:
        return _keep(item.artifact_id, ArtifactKind.EVIDENCE, "missing-metadata", item.owner)
    if item.terminal_at is None:
        return _keep(item.artifact_id, ArtifactKind.EVIDENCE, "missing-metadata", item.owner)
    if item.terminal_at.tzinfo is None:
        return _keep(item.artifact_id, ArtifactKind.EVIDENCE, "invalid-time", item.owner)
    if item.terminal_at > now:
        return _keep(item.artifact_id, ArtifactKind.EVIDENCE, "terminal-future", item.owner)
    retention = accepted_retention if item.run_accepted else other_retention
    if now >= item.terminal_at + retention:
        if item.size_bytes is None:
            return _keep(item.artifact_id, ArtifactKind.EVIDENCE, "missing-metadata", item.owner)
        return PlanDecision(
            artifact_id=item.artifact_id,
            kind=ArtifactKind.EVIDENCE,
            action=Action.ELIGIBLE_DELETE,
            reason="evidence-eligible",
            owner=item.owner,
        )
    if now >= item.created_at + compress_after and not item.compressed:
        return PlanDecision(
            artifact_id=item.artifact_id,
            kind=ArtifactKind.EVIDENCE,
            action=Action.ELIGIBLE_COMPRESS,
            reason="evidence-compress-eligible",
            owner=item.owner,
        )
    return _keep(item.artifact_id, ArtifactKind.EVIDENCE, "evidence-retained", item.owner)


def _plan_venv(item: WorktreeVenv, *, now: datetime, owned_roots: Sequence[str]) -> PlanDecision:
    """Covers: ADR-0003 D6 worktree venvs (die with the worktree)."""
    _ = now
    reason = _check_filesystem_path(item.path, owned_roots)
    if reason is not None:
        return _keep(item.artifact_id, ArtifactKind.WORKTREE_VENV, reason, item.owner)
    if item.is_symlink_escape:
        return _keep(item.artifact_id, ArtifactKind.WORKTREE_VENV, "symlink-escape", item.owner)
    if item.parent_worktree_live is None or item.parent_eligible_for_removal is None:
        return _keep(item.artifact_id, ArtifactKind.WORKTREE_VENV, "missing-metadata", item.owner)
    if item.parent_worktree_live:
        return _keep(item.artifact_id, ArtifactKind.WORKTREE_VENV, "venv-parent-live", item.owner)
    if not item.parent_eligible_for_removal:
        return _keep(item.artifact_id, ArtifactKind.WORKTREE_VENV, "venv-parent-kept", item.owner)
    return PlanDecision(
        artifact_id=item.artifact_id,
        kind=ArtifactKind.WORKTREE_VENV,
        action=Action.ELIGIBLE_DELETE,
        reason="venv-follows-worktree",
        owner=item.owner,
    )


def _plan_owner_db(
    item: OwnerDatabase, *, now: datetime, owned_roots: Sequence[str]
) -> PlanDecision:
    """Covers: ADR-0003 D6 owner DB (weekly VACUUM above 20 % free pages)."""
    reason = _check_filesystem_path(item.path, owned_roots)
    if reason is not None:
        return _keep(item.artifact_id, ArtifactKind.OWNER_DB, reason, item.owner)
    if item.is_symlink_escape:
        return _keep(item.artifact_id, ArtifactKind.OWNER_DB, "symlink-escape", item.owner)
    if item.last_vacuum_at is None or item.free_page_ratio is None:
        return _keep(item.artifact_id, ArtifactKind.OWNER_DB, "missing-metadata", item.owner)
    if item.last_vacuum_at.tzinfo is None:
        return _keep(item.artifact_id, ArtifactKind.OWNER_DB, "invalid-time", item.owner)
    if not 0.0 <= item.free_page_ratio <= 1.0:
        return _keep(item.artifact_id, ArtifactKind.OWNER_DB, "invalid-ratio", item.owner)
    if now >= item.last_vacuum_at + OWNER_DB_VACUUM_INTERVAL and (
        item.free_page_ratio > OWNER_DB_VACUUM_FREE_PAGE_RATIO
    ):
        return PlanDecision(
            artifact_id=item.artifact_id,
            kind=ArtifactKind.OWNER_DB,
            action=Action.ELIGIBLE_VACUUM,
            reason="vacuum-due",
            owner=item.owner,
        )
    return _keep(item.artifact_id, ArtifactKind.OWNER_DB, "vacuum-not-due", item.owner)


def plan_gc(
    artifacts: Sequence[Artifact],
    *,
    now: datetime,
    owned_roots: Sequence[str],
    failed_scratch_cap_bytes: int = FAILED_SCRATCH_SIZE_CAP_BYTES,
    compress_after: timedelta = EVIDENCE_COMPRESS_AFTER,
    accepted_retention: timedelta = EVIDENCE_ACCEPTED_RETENTION,
    other_retention: timedelta = EVIDENCE_OTHER_RETENTION,
    checkpoint_retention: timedelta = CHECKPOINT_RETENTION_AFTER_TERMINAL,
) -> GcPlan:
    """Plan retention/GC eligibility for ``artifacts`` at time ``now``.

    Pure and deterministic: no I/O, no clock reads, no randomness. Output
    decisions are sorted by (kind, artifact_id). Raises :exc:`ValueError`
    for a naive ``now``, an empty artifact id, or a duplicated artifact id.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    for name, bound in (
        ("compress_after", compress_after),
        ("accepted_retention", accepted_retention),
        ("other_retention", other_retention),
        ("checkpoint_retention", checkpoint_retention),
    ):
        if bound < timedelta(0):
            raise ValueError(f"{name} must be nonnegative")
    if failed_scratch_cap_bytes < 0:
        raise ValueError("failed_scratch_cap_bytes must be nonnegative")
    seen: set[str] = set()
    staged: list[PlanDecision] = []
    for item in artifacts:
        if item.artifact_id == "":
            raise ValueError("artifact_id must not be empty")
        if item.artifact_id in seen:
            raise ValueError(f"duplicated artifact_id: {item.artifact_id!r}")
        seen.add(item.artifact_id)
        kind = _kind_of(item)
        if not _owner_ok(item.owner, item.owner_state):
            staged.append(_keep(item.artifact_id, kind, "unknown-owner", item.owner))
            continue
        size: int | None = None
        if isinstance(item, (GateScratch, EvidenceBundle)):
            size = item.size_bytes
        if size is not None and size < 0:
            staged.append(_keep(item.artifact_id, kind, "invalid-size", item.owner))
            continue
        if isinstance(item, GateScratch):
            staged.append(
                _plan_scratch(
                    item,
                    now=now,
                    owned_roots=owned_roots,
                    failed_scratch_cap_bytes=failed_scratch_cap_bytes,
                )
            )
        elif isinstance(item, AttemptWorktree):
            staged.append(_plan_worktree(item, now=now, owned_roots=owned_roots))
        elif isinstance(item, CheckpointRef):
            staged.append(
                _plan_checkpoint(item, now=now, checkpoint_retention=checkpoint_retention)
            )
        elif isinstance(item, EvidenceBundle):
            staged.append(
                _plan_evidence(
                    item,
                    now=now,
                    owned_roots=owned_roots,
                    compress_after=compress_after,
                    accepted_retention=accepted_retention,
                    other_retention=other_retention,
                )
            )
        elif isinstance(item, WorktreeVenv):
            staged.append(_plan_venv(item, now=now, owned_roots=owned_roots))
        elif isinstance(item, OwnerDatabase):
            staged.append(_plan_owner_db(item, now=now, owned_roots=owned_roots))
    staged.sort(key=lambda d: (d.kind.value, d.artifact_id))
    by_id = {item.artifact_id: item for item in artifacts}
    log: list[DeletionEvent] = []
    for decision in staged:
        if decision.action is not Action.ELIGIBLE_DELETE:
            continue
        original = by_id[decision.artifact_id]
        path = ""
        logged_size: int | None = None
        if isinstance(original, CheckpointRef):
            path = original.ref
        elif isinstance(original, (GateScratch, EvidenceBundle)):
            path = original.path
            logged_size = original.size_bytes
        else:
            path = original.path
        log.append(
            DeletionEvent(
                ts=now,
                artifact_class=decision.kind.value,
                path=path,
                bytes=logged_size,
                reason=decision.reason,
            )
        )
    return GcPlan(planned_at=now, decisions=tuple(staged), deletion_log=tuple(log))


def _kind_of(item: Artifact) -> ArtifactKind:
    """Covers: every decision preserves kind identity for unknown owners."""
    if isinstance(item, GateScratch):
        return ArtifactKind.GATE_SCRATCH
    if isinstance(item, AttemptWorktree):
        return ArtifactKind.ATTEMPT_WORKTREE
    if isinstance(item, CheckpointRef):
        return ArtifactKind.CHECKPOINT_REF
    if isinstance(item, EvidenceBundle):
        return ArtifactKind.EVIDENCE
    if isinstance(item, WorktreeVenv):
        return ArtifactKind.WORKTREE_VENV
    return ArtifactKind.OWNER_DB
