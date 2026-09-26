"""R3-D: cross-project owner Inbox -- a read-only attention projection.

Inbox is the owner's cross-project work queue: candidates needing a
decision or review action plus failed/orphaned runs needing owner
action.  It is NOT a second run database, a notification log, a copy of
every run, a new lifecycle state machine, or a mutable queue.

Every row is a cheap read-only summary derived on each read from
existing durable authority:

* candidate classification reuses
  :func:`saberops.candidate_lifecycle.project_candidate_state` verbatim
  (no reimplemented selection, gate proof, review binding, acceptance,
  rejection, or canonical-SHA policy);
* run fallback uses only the durable :class:`RunStatus`.

Candidate authority outranks generic ``RunStatus``: one run produces at
most one Inbox item, and a trustworthy candidate-attention item never
gains a duplicate run-failure row.

If the underlying run/candidate stops needing attention, it disappears
from Inbox automatically on the next read.  No Inbox row is ever
explicitly deleted, acknowledged, or dismissed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from saberops.candidate_lifecycle import (
    is_canonical_candidate_sha as is_canonical_candidate_sha,
)
from saberops.models import CandidateState, Run, RunStatus

if TYPE_CHECKING:
    from saberops.db import Database
    from saberops.runtime import ProjectRuntimeManager


#: Default Inbox bound: deterministic newest-first rows per read.
DEFAULT_MAX_ITEMS: int = 100

#: Hard upper bound: a caller-supplied limit is clamped to this.
HARD_MAX_ITEMS: int = 500

#: Per-store read cap: Inbox rows are cheap summaries, so each project
#: store is read generously and the global bound/sort applies after.
_PER_STORE_LIMIT: int = 10_000


class InboxAttentionKind(StrEnum):
    """Owner vocabulary for why a run needs attention."""

    DECISION_REQUIRED = "DECISION_REQUIRED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    RUN_FAILED = "RUN_FAILED"
    RUN_ORPHANED = "RUN_ORPHANED"
    STALE_CANDIDATE = "STALE_CANDIDATE"


#: Owner-facing label per attention kind.  Implementation vocabulary
#: (tiers, pools, bindings, manager, routing context) never appears.
ATTENTION_LABELS: dict[InboxAttentionKind, str] = {
    InboxAttentionKind.DECISION_REQUIRED: "Needs decision",
    InboxAttentionKind.REVIEW_REQUIRED: "Review required",
    InboxAttentionKind.RUN_FAILED: "Run failed",
    InboxAttentionKind.RUN_ORPHANED: "Run orphaned",
    InboxAttentionKind.STALE_CANDIDATE: "Candidate stale",
}


def attention_label(kind: InboxAttentionKind) -> str:
    """Return the owner-facing label for ``kind``."""
    return ATTENTION_LABELS[kind]


@dataclass(frozen=True)
class InboxItem:
    """One immutable read-only Inbox row for a single run."""

    run_id: str
    project_id: str | None
    project_path: str
    task: str
    run_status: RunStatus
    candidate_sha: str | None
    candidate_state: CandidateState | None
    review_verdict: Any | None
    attention_kind: InboxAttentionKind
    attention_reason: str
    created_at: str
    completed_at: str | None

    @property
    def short_sha(self) -> str | None:
        """Return the 8-char candidate prefix, if a SHA is present."""
        if self.candidate_sha and len(self.candidate_sha) >= 8:
            return self.candidate_sha[:8]
        return self.candidate_sha

    @property
    def attention_label(self) -> str:
        """Return the owner-facing attention label."""
        return ATTENTION_LABELS[self.attention_kind]

    @property
    def updated_at(self) -> str | None:
        """Return the durable completed/created time for display/sort."""
        return self.completed_at or self.created_at or None

    def as_dict(self) -> dict[str, Any]:
        """Serialize deterministically for JSON/API consumers."""
        verdict = self.review_verdict
        return {
            "run_id": self.run_id,
            "project_id": self.project_id,
            "project_path": self.project_path,
            "task": self.task,
            "run_status": self.run_status.value,
            "candidate_sha": self.candidate_sha,
            "candidate_state": (
                self.candidate_state.value if self.candidate_state is not None else None
            ),
            "review_verdict": (
                verdict.value if isinstance(verdict, StrEnum) else verdict
            ),
            "attention_kind": self.attention_kind.value,
            "attention_label": self.attention_label,
            "attention_reason": self.attention_reason,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
        }


@dataclass(frozen=True)
class InboxSnapshot:
    """One bounded deterministic Inbox read."""

    items: tuple[InboxItem, ...]
    unreadable_projects: int = 0

    def as_dict(self) -> dict[str, Any]:
        """Serialize deterministically for JSON/API consumers."""
        return {
            "items": [item.as_dict() for item in self.items],
            "count": len(self.items),
            "unreadable_projects": self.unreadable_projects,
        }


def classify_run(
    db: Database,
    run: Run,
    *,
    project_id: str | None,
    project_path: str,
) -> InboxItem | None:
    """Classify one run into at most one Inbox item, or ``None``.

    Pure read path: derives from the existing R3-A candidate projection
    plus the durable ``RunStatus``.  Candidate authority outranks generic
    run status, so a run whose trustworthy candidate already classifies
    never gains a second run-failure row.

    STALE is only reachable when the R3-A projection itself proves it;
    without a caller-supplied live-HEAD resolver that branch stays
    unreachable (fail closed) rather than guessed -- R4 owns stale
    recovery.
    """
    from saberops.candidate_lifecycle import project_candidate_state

    try:
        projection = project_candidate_state(db, run.id)
    except Exception:
        return None
    state = projection.state
    sha = projection.candidate_sha
    # Canonical identity is the candidate-lifecycle authority's own
    # predicate: a malformed/noncanonical gate-passed SHA can never
    # produce a decision action that assumes canonical owner authority
    # (fail closed into the run-status branch).
    trustworthy = (
        state is not None
        and sha is not None
        and is_canonical_candidate_sha(sha)
    )

    def _item(
        kind: InboxAttentionKind,
        reason: str,
        *,
        candidate_state: CandidateState | None = state,
        candidate_sha: str | None = sha if trustworthy else None,
    ) -> InboxItem:
        return InboxItem(
            run_id=run.id,
            project_id=project_id,
            project_path=project_path,
            task=run.task,
            run_status=run.status,
            candidate_sha=candidate_sha,
            candidate_state=candidate_state,
            review_verdict=projection.verdict,
            attention_kind=kind,
            attention_reason=reason,
            created_at=run.created_at,
            completed_at=run.completed_at,
        )

    if trustworthy and state is not None:
        # A. REVIEWED -> the owner inspects the review and chooses
        # Accept, Reject or Re-run.  The actual ReviewVerdict is
        # preserved separately; PASS/BLOCK is never the owner decision.
        if state is CandidateState.REVIEWED:
            return _item(
                InboxAttentionKind.DECISION_REQUIRED, projection.reason
            )
        # B. READY with review provably unnecessary -> decision.
        if state is CandidateState.READY and projection.review_required is False:
            return _item(
                InboxAttentionKind.DECISION_REQUIRED, projection.reason
            )
        # C. READY with review required OR unknown/legacy requirement ->
        # review action.  Unknown is never silently treated as
        # "review not required".
        if state is CandidateState.READY:
            return _item(InboxAttentionKind.REVIEW_REQUIRED, projection.reason)
        # D. STALE, only when R3-A actually proves it (display only).
        if state is CandidateState.STALE:
            return _item(InboxAttentionKind.STALE_CANDIDATE, projection.reason)
        # E/F. IN_REVIEW (system acting) and ACCEPTED / REJECTED /
        # CLEANED (terminal) need no owner attention.
        return None
    # A malformed/noncanonical gate-passed SHA (or gate-unproven
    # candidate) is never a trustworthy owner-visible candidate: fail
    # closed into the run-status branch rather than inventing decision
    # authority.
    if run.status is RunStatus.FAILED:
        return _item(
            InboxAttentionKind.RUN_FAILED,
            "run_failed_no_trustworthy_candidate",
            candidate_state=None,
        )
    if run.status is RunStatus.ORPHANED:
        return _item(
            InboxAttentionKind.RUN_ORPHANED,
            "run_orphaned_no_trustworthy_candidate",
            candidate_state=None,
        )
    # H. PENDING / RUNNING / COMPLETED-without-attention: not in Inbox.
    return None


def collect_project_inbox(
    db: Database,
    *,
    project_id: str | None,
    project_path: str,
) -> list[InboxItem]:
    """Classify every run in one project store (read-only, cheap rows).

    Never loads diffs, transcripts, or review details: each row is a
    summary over the candidate projection plus durable run fields.

    Read failures (``db.list_runs``) propagate so the caller can count
    the source as unreadable; an unreadable source must stay
    distinguishable from a valid empty project.  Per-run
    classification failures still skip just that run.
    """
    runs = db.list_runs(limit=_PER_STORE_LIMIT)
    items: list[InboxItem] = []
    for run in runs:
        try:
            item = classify_run(
                db, run, project_id=project_id, project_path=project_path
            )
        except Exception:
            continue
        if item is not None:
            items.append(item)
    return items


def _sort_timestamp(item: InboxItem) -> float:
    """Return the durable sort time (completed, else created) as epoch."""
    raw = item.completed_at or item.created_at or ""
    text = raw.strip()
    if not text:
        return 0.0
    normalized = text
    if text.endswith("Z") or text.endswith("z"):
        normalized = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return 0.0
    try:
        return parsed.timestamp()
    except (OverflowError, OSError, ValueError):
        return 0.0


def build_inbox(
    manager: ProjectRuntimeManager, *, limit: int = DEFAULT_MAX_ITEMS
) -> InboxSnapshot:
    """Build one bounded deterministic cross-project Inbox snapshot.

    Enumerates every persisted project store through the manager's
    genuinely read-only seam (:meth:`list_inbox_stores`, never the
    bounded recent-project UI list, never the active-project
    selection), classifies each run once, sorts newest attention first
    with ``run_id`` as the deterministic tie-breaker, and truncates to
    ``limit`` (default 100, hard upper bound 500).

    Read-only: no events, attempts, checks, reviews, runs, project
    bindings, or registry entries are created.  A corrupt/unreadable
    project state fails locally for that directory (counted in
    ``unreadable_projects``) rather than breaking the whole Inbox.
    """
    bound = max(1, min(int(limit), HARD_MAX_ITEMS))
    try:
        stores, unreadable = manager.list_inbox_stores()
    except Exception:
        return InboxSnapshot(items=(), unreadable_projects=0)
    seen: set[str] = set()
    merged: list[InboxItem] = []
    failed_sources = 0
    for store in stores:
        identity = store.identity
        project_id = identity.project_id if identity is not None else None
        try:
            items = collect_project_inbox(
                store.db,
                project_id=project_id,
                project_path=(
                    identity.canonical_path if identity is not None else ""
                ),
            )
        except Exception:
            failed_sources += 1
            continue
        for item in items:
            # Project-scoped state is never duplicated into legacy
            # results: the first source owning a run id wins (project
            # runtimes enumerate before the legacy store).
            if item.run_id in seen:
                continue
            seen.add(item.run_id)
            if project_id is None:
                merged.append(_with_legacy_project(item, store.db))
            else:
                merged.append(item)
    ordered = sorted(
        merged, key=lambda entry: (-_sort_timestamp(entry), entry.run_id)
    )
    return InboxSnapshot(
        items=tuple(ordered[:bound]),
        unreadable_projects=unreadable + failed_sources,
    )


def _with_legacy_project(item: InboxItem, db: Database) -> InboxItem:
    """Fill the legacy item's project path from its own durable run row.

    Resolution derives the owning project from the run itself (its
    persisted ``target_repo``), never from the active-project
    selection.
    """
    if item.project_path:
        return item
    try:
        run = db.get_run(item.run_id)
    except Exception:
        return item
    if run is None:
        return item
    return InboxItem(
        run_id=item.run_id,
        project_id=run.project_id,
        project_path=run.target_repo or "",
        task=item.task,
        run_status=item.run_status,
        candidate_sha=item.candidate_sha,
        candidate_state=item.candidate_state,
        review_verdict=item.review_verdict,
        attention_kind=item.attention_kind,
        attention_reason=item.attention_reason,
        created_at=item.created_at,
        completed_at=item.completed_at,
    )
