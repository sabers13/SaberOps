"""Guarded GC planner and executor for the GS-1 storage lifecycle service.

The early :mod:`saberops.storage_lifecycle` planner is a pure eligibility
function.  This module turns it into an *executable* lifecycle service:

* :func:`preview_gc` projects the accepted retention policy onto a real
  :class:`~saberops.storage_inventory.Inventory`.  It is a true no-op: it
  creates nothing, writes nothing and deletes nothing.
* :func:`apply_gc` executes an exact, previously previewed selection.  It
  re-resolves every identity and every authority immediately before each
  action, under the repository worktree-metadata lock, and refuses on any race
  (ownership change, symlink/root change, branch-tip move, live consumer,
  pending acceptance, dirty or uniquely-ignored content).  It never widens a
  stale preview.
* Every intent, result and failure is audited.  A crash between Git actions is
  recoverable and idempotent; a successful removal whose audit write fails
  surfaces uncertainty and stops instead of touching another target.

Safety posture (ADR-0003 Amendment 1 and Amendment 2):

* Only the exact native Git operations ``git worktree remove`` (no force) and
  ``git branch -d`` (no force) retire Git objects.  No ``git worktree prune``,
  no ``git branch -D``, no ``rm -rf`` of an unverified path, no remote deletion.
* Evidence deletion stays held while the retention anchor is unsettled: the
  preview reports it and the executor refuses it unless a validated owner
  policy explicitly settles the anchor.
* A missing standing owner policy means inventory/report only; there is no
  destructive automatic GC and no launch permission.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import Final

from saberops.db import Database
from saberops.git import (
    CHECKPOINT_REF_PREFIX,
    BranchDeletionRefused,
    GitManager,
)
from saberops.paths import CONFIG_APP_DIR, config_home
from saberops.storage_inventory import (
    ArtifactClass,
    Inventory,
    InventoryItem,
    OwnershipState,
    measure_path_bytes,
    storage_owned_roots,
)
from saberops.storage_lifecycle import (
    Action,
    Artifact,
    AttemptWorktree,
    EvidenceBundle,
    GateScratch,
    OwnerDatabase,
    OwnerState,
    WorktreeVenv,
    plan_gc,
)
from saberops.storage_lifecycle import CandidateState as PlannerCandidateState
from saberops.storage_lifecycle import CheckpointRef as PlannerCheckpointRef

__all__ = [
    "GC_POLICY_FILENAME",
    "GcAction",
    "GcApplyResult",
    "GcCandidate",
    "GcExclusion",
    "GcOutcome",
    "GcPolicy",
    "GcPolicyError",
    "GcPreview",
    "StorageAuditEvent",
    "apply_gc",
    "default_gc_policy_path",
    "load_gc_policy",
    "preview_gc",
    "retire_run_worktrees",
]

GC_POLICY_FILENAME: Final[str] = "storage_gc.json"

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
    destructive.  ``evidence_anchor`` is ``None`` while ADR-0003 Amendment 1's
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


def _planner_owner_state(item: InventoryItem) -> OwnerState:
    if item.ownership is OwnershipState.UNKNOWN:
        return OwnerState.UNKNOWN
    if item.ownership is OwnershipState.OWNED_LIVE:
        return OwnerState.KNOWN_LIVE
    return OwnerState.KNOWN_DEAD


def _to_planner_artifact(item: InventoryItem) -> Artifact | None:
    owner_state = _planner_owner_state(item)
    detail = item.detail
    if item.artifact_class is ArtifactClass.ATTEMPT_WORKTREE:
        candidate = detail.get("candidate_state", "unknown")
        candidate_map = {
            "READY": PlannerCandidateState.PENDING_REVIEW,
            "IN_REVIEW": PlannerCandidateState.PENDING_REVIEW,
            "REVIEWED": PlannerCandidateState.PENDING_REVIEW,
            "ACCEPTED": PlannerCandidateState.ACCEPTED,
            "REJECTED": PlannerCandidateState.REJECTED,
            "STALE": PlannerCandidateState.SUPERSEDED,
            "CLEANED": PlannerCandidateState.UNKNOWN,
            "unknown": PlannerCandidateState.UNKNOWN,
        }
        return AttemptWorktree(
            artifact_id=item.reference,
            path=item.reference,
            owner=item.owner,
            owner_state=owner_state,
            attempt_terminal=detail.get("attempt_terminal") == "1",
            candidate_state=candidate_map.get(candidate, PlannerCandidateState.UNKNOWN),
            job_preempted=detail.get("preempted") == "1",
            job_terminal=detail.get("run_status") in ("COMPLETED", "FAILED", "ORPHANED"),
            worktree_live=False,
        )
    if item.artifact_class is ArtifactClass.CHECKPOINT_REF:
        return PlannerCheckpointRef(
            artifact_id=item.reference,
            ref=item.reference,
            owner=item.owner,
            owner_state=owner_state,
            job_id=detail.get("job_id") or None,
            job_terminal=detail.get("job_terminal") == "1",
            terminal_at=None,
            job_preempted=detail.get("preempted") == "1",
        )
    if item.artifact_class is ArtifactClass.WORKTREE_VENV:
        return WorktreeVenv(
            artifact_id=item.reference,
            path=item.reference,
            owner=item.owner,
            owner_state=owner_state,
            parent_worktree_live=detail.get("parent_worktree_live") == "1",
            parent_eligible_for_removal=detail.get("parent_eligible") == "1",
        )
    if item.artifact_class is ArtifactClass.OWNER_DB:
        return OwnerDatabase(
            artifact_id=item.reference,
            path=item.reference,
            owner=item.owner,
            owner_state=owner_state,
            last_vacuum_at=None,
            free_page_ratio=None,
        )
    if item.artifact_class is ArtifactClass.GATE_SCRATCH:
        return GateScratch(
            artifact_id=item.reference,
            path=item.reference,
            owner=item.owner,
            owner_state=owner_state,
            size_bytes=item.size_bytes,
            gate_finished=True,
            gate_succeeded=detail.get("gate_succeeded") == "1",
            is_newest_failed=detail.get("is_newest_failed") == "1",
        )
    if item.artifact_class is ArtifactClass.EVIDENCE:
        return EvidenceBundle(
            artifact_id=item.reference,
            path=item.reference,
            owner=item.owner,
            owner_state=owner_state,
            run_terminal=True,
            run_accepted=detail.get("run_accepted") == "1",
            created_at=None,
            terminal_at=None,
            compressed=False,
            size_bytes=item.size_bytes,
        )
    return None


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
            candidate = _plan_worktree_item(item, active_policy, owned_roots)
        elif item.artifact_class is ArtifactClass.LOCAL_BRANCH:
            candidate = _plan_branch_item(item, active_policy)
        elif item.artifact_class is ArtifactClass.CHECKPOINT_REF:
            candidate = _plan_checkpoint_item(item, active_policy, owned_roots)
        elif item.artifact_class is ArtifactClass.WORKTREE_VENV:
            candidate = _plan_venv_item(item, active_policy, owned_roots)
        elif item.artifact_class is ArtifactClass.OWNER_DB:
            candidate = _plan_owner_db_item(item, active_policy)
        elif item.artifact_class is ArtifactClass.EVIDENCE:
            candidate = _plan_evidence_item(item, active_policy)
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
    item: InventoryItem, policy: GcPolicy, owned_roots: Sequence[str]
) -> GcCandidate | None:
    if not policy.allow_worktree_retirement:
        return None
    if item.protected or item.ownership not in (
        OwnershipState.OWNED_TERMINAL,
        OwnershipState.OWNED_ACCEPTED,
    ):
        return None
    artifact = _to_planner_artifact(item)
    if artifact is None:
        return None
    plan = plan_gc([artifact], now=datetime.now(UTC), owned_roots=owned_roots)
    decision = plan.decisions[0]
    if decision.action is not Action.ELIGIBLE_DELETE:
        return None
    return GcCandidate(
        artifact_class=item.artifact_class,
        action=GcAction.REMOVE_WORKTREE,
        reference=item.reference,
        identity=item.identity,
        owner=item.owner,
        reason=decision.reason,
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
    item: InventoryItem, policy: GcPolicy, owned_roots: Sequence[str]
) -> GcCandidate | None:
    if not policy.allow_checkpoint_deletion:
        return None
    artifact = _to_planner_artifact(item)
    if artifact is None:
        return None
    plan = plan_gc([artifact], now=datetime.now(UTC), owned_roots=owned_roots)
    decision = plan.decisions[0]
    if decision.action is not Action.ELIGIBLE_DELETE:
        return None
    return GcCandidate(
        artifact_class=item.artifact_class,
        action=GcAction.DELETE_CHECKPOINT_REF,
        reference=item.reference,
        identity=item.identity,
        owner=item.owner,
        reason=decision.reason,
        estimated_bytes=0,
        reclaimable=True,
        detail=item.detail,
    )


def _plan_venv_item(
    item: InventoryItem, policy: GcPolicy, owned_roots: Sequence[str]
) -> GcCandidate | None:
    if not policy.allow_worktree_retirement:
        return None
    artifact = _to_planner_artifact(item)
    if artifact is None:
        return None
    plan = plan_gc([artifact], now=datetime.now(UTC), owned_roots=owned_roots)
    decision = plan.decisions[0]
    if decision.action is not Action.ELIGIBLE_DELETE:
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


def _plan_evidence_item(item: InventoryItem, policy: GcPolicy) -> GcCandidate | None:
    if not policy.allow_evidence_deletion:
        return None
    if policy.evidence_anchor is None:
        return None
    return GcCandidate(
        artifact_class=item.artifact_class,
        action=GcAction.DELETE_PATH,
        reference=item.reference,
        identity=item.identity,
        owner=item.owner,
        reason="evidence-eligible",
        estimated_bytes=item.reclaimable_bytes,
        reclaimable=item.reclaimable_bytes is not None,
        detail=item.detail,
    )


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

    previewed = {(c.artifact_class, c.reference, c.identity): c for c in preview.candidates}
    ordered = sorted(
        selection,
        key=lambda c: (_ACTION_ORDER[c.action], c.artifact_class.value, c.reference),
    )
    for candidate in ordered:
        key = (candidate.artifact_class, candidate.reference, candidate.identity)
        if key not in previewed:
            refused.append(GcOutcome(candidate, "refused", "selection-widens-preview", None))
            continue
        try:
            outcome = _execute_candidate(
                candidate, preview, db=db, repo=repo, audit=audit, now=moment
            )
        except _AuditFailure:
            audit_uncertain = True
            applied.append(GcOutcome(candidate, "applied", "audit-uncertain", None))
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


def _execute_candidate(
    candidate: GcCandidate,
    preview: GcPreview,
    *,
    db: Database,
    repo: Path,
    audit: AuditSink,
    now: datetime,
) -> GcOutcome:
    _emit(audit, phase="intent", candidate=candidate, preview=preview, reason=candidate.reason,
          actual_bytes=None, at=now)
    if candidate.action is GcAction.REMOVE_WORKTREE:
        result = _revalidate_and_remove_worktree(candidate, db=db, repo=repo, now=now)
    elif candidate.action is GcAction.DELETE_BRANCH:
        result = _revalidate_and_delete_branch(candidate, db=db, repo=repo)
    elif candidate.action is GcAction.DELETE_CHECKPOINT_REF:
        result = _revalidate_and_delete_checkpoint(candidate, db=db, repo=repo, now=now)
    elif candidate.action is GcAction.DELETE_PATH:
        result = _revalidate_and_delete_path(candidate, preview=preview, repo=repo)
    elif candidate.action is GcAction.COMPRESS_EVIDENCE:
        result = GcOutcome(candidate, "refused", "compression-requires-dedicated-policy", None)
    elif candidate.action is GcAction.VACUUM_DB:
        result = _revalidate_and_vacuum(candidate, db=db)
    else:  # pragma: no cover - exhaustive enum
        result = GcOutcome(candidate, "refused", "unknown-action", None)
    phase = "result" if result.status == "applied" else "failure"
    _emit(audit, phase=phase, candidate=candidate, preview=preview, reason=result.reason,
          actual_bytes=result.actual_bytes, at=now)
    return result


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
    try:
        GitManager.delete_checkpoint_ref(repo, ref)
    except Exception as exc:
        return GcOutcome(candidate, "refused", f"refusal:{exc}"[:200], None)
    return GcOutcome(candidate, "applied", "checkpoint-ref-deleted", 0)


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
    from saberops.models import RunStatus

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
