"""Deterministic project-child execution-slot admission policy (Step 010 / WX-1).

What this module is
-------------------
One *pure* function, :func:`evaluate_slot_admission`, that answers a single
question for an **authoritative, already-observed** snapshot of a project's
execution slots:

    "May this candidate child task be admitted for execution, given the
    currently occupied child slots and the owner's configured limits?"

It encodes the owner-approved concurrency default -- at most
:data:`DEFAULT_ACTIVE_PROJECT_LIMIT` concurrently active projects and at most
:data:`DEFAULT_CHILD_SLOTS_PER_PROJECT_LIMIT` executing child slots *per
project* -- as two independent constraints. A per-project limit of five and a
project limit of three are **not** interchangeable with a single global cap of
fifteen: the derived ceiling ``3 * 5 == 15`` is exposed for observability only
and is never used as the admission test.

What this module is deliberately NOT
------------------------------------
This is a *projection*, not a scheduler and not an authorization. It is not an
executable scheduler, a compare-and-swap protocol, a lease, a project
registry, or a capacity proof. It performs no I/O, reads no database, spawns
no process, consults no clock, uses no randomness and calls no model. Given
the same immutable inputs it returns the same typed decision.

**No production launch may trust this pure function alone.** A pure evaluator
can only validate the *explicit* complete-and-authoritative flag that its
caller supplies; it cannot establish that the snapshot really is complete,
current, or free of concurrent writers. Any real launch path MUST wrap this
decision in a transactional caller that, at minimum:

* holds an exclusive lease / write fence on the owner database while it reads
  the authoritative snapshot and commits the reservation (read-check-write in
  one transaction, not a read here and a write later);
* proves completeness and freshness of the snapshot (owner-scoped source,
  consistent revision, no partial enumeration) rather than accepting a bare
  boolean from an untrusted caller;
* re-verifies the project identity through the registered
  ``saberops.project.ProjectIdentity.project_id`` and refuses identities that
  are inferred from a directory or branch name;
* atomically reserves the slot so that two concurrent candidates cannot both
  observe free capacity and both start (cross-process race proof), treating a
  conflicting or missing record as occupied rather than free.

Until that transactional caller exists, this module describes intent only.
Phase headings and three-digit display step numbers carry **no scheduling
meaning**: approved DAG prerequisites and write-scope checks remain upstream.

Capacity semantics
------------------
A child slot occupies capacity only while its :class:`SlotState` is
``RESERVED`` or ``EXECUTING``. ``QUEUED``, ``BLOCKED`` and ``TERMINAL`` child
tasks do not occupy a slot, and neither do plan/wave wrapper records or
Orchestrator turns, however many of them exist. Duplicate identical slot
records are an alias of one lane and are counted once; conflicting identities
or malformed rows fail closed. Lowering the limits never terminates a running
task; it only denies further starts until occupancy falls back below the new
limits. A child in a non-occupying state is reported with reason
``non_occupying_candidate`` and ``occupies_slot`` false: it is not permission
to spawn a child process. An unknown, blank or mismatched ``owner_id`` on
either the snapshot or the request denies (``missing_identity`` /
``owner_mismatch``); two blank owners never match, and malformed limits deny
(``invalid_limit``) without raising.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TypeGuard

__all__ = [
    "AdmissionReason",
    "AdmissionStatus",
    "OCCUPYING_SLOT_STATES",
    "PROJECT_SLOT_POLICY_VERSION",
    "REQUIRES_TRANSACTIONAL_VERIFICATION",
    "DEFAULT_ACTIVE_PROJECT_LIMIT",
    "DEFAULT_CHILD_SLOTS_PER_PROJECT_LIMIT",
    "ProjectSlotRecord",
    "SlotAdmissionDecision",
    "SlotAdmissionRequest",
    "SlotCounts",
    "SlotKind",
    "SlotLimits",
    "SlotSnapshot",
    "SlotState",
    "SnapshotSource",
    "evaluate_slot_admission",
    "default_slot_limits",
]

PROJECT_SLOT_POLICY_VERSION = "project-slot-policy/1"

#: Owner-approved default: at most three projects with an executing child slot.
DEFAULT_ACTIVE_PROJECT_LIMIT = 3

#: Owner-approved default: at most five executing child slots per project.
DEFAULT_CHILD_SLOTS_PER_PROJECT_LIMIT = 5

#: Always true: this pure projection is never, by itself, launch authority.
REQUIRES_TRANSACTIONAL_VERIFICATION = True


class SlotState(StrEnum):
    """Authoritative state of one execution slot as observed in a snapshot.

    Only :attr:`RESERVED` and :attr:`EXECUTING` occupy capacity. Review and
    waiting jobs are modelled as :attr:`BLOCKED` (or :attr:`QUEUED`) and are
    excluded exactly like any other non-occupying state.
    """

    RESERVED = "reserved"
    EXECUTING = "executing"
    QUEUED = "queued"
    BLOCKED = "blocked"
    TERMINAL = "terminal"


#: The only slot states that consume a project's execution budget.
OCCUPYING_SLOT_STATES: frozenset[SlotState] = frozenset(
    {SlotState.RESERVED, SlotState.EXECUTING}
)


class SlotKind(StrEnum):
    """Kind of a record observed in a snapshot.

    Only :attr:`CHILD_TASK` records can occupy a child slot. A plan or wave
    coordinator (``PLAN_WRAPPER`` / ``WAVE_WRAPPER``) and an Orchestrator turn
    (``ORCHESTRATOR_TURN``) are never counted as child tasks, so a wave parent
    plus five children counts as five slots, not six.
    """

    CHILD_TASK = "child_task"
    PLAN_WRAPPER = "plan_wrapper"
    WAVE_WRAPPER = "wave_wrapper"
    ORCHESTRATOR_TURN = "orchestrator_turn"


class SnapshotSource(StrEnum):
    """Origin of a slot snapshot.

    Only :attr:`OWNER_DB` is authoritative. Any other value (including the
    explicit :attr:`UNKNOWN`) is *not proven free* and fails closed.
    """

    OWNER_DB = "owner_db"
    UNKNOWN = "unknown"


class AdmissionStatus(StrEnum):
    """Typed outcome of one admission evaluation."""

    ADMITTED = "ADMITTED"
    """The candidate may occupy a slot under the observed authoritative state."""

    REJECTED = "REJECTED"
    """A known, verified owner limit is currently reached; nothing is invalid."""

    DENIED = "DENIED"
    """The inputs are unverifiable, invalid, conflicting or a replay; fail closed."""


class AdmissionReason(StrEnum):
    """Stable machine-readable reason codes, in evaluation precedence order."""

    ADMITTED = "admitted"
    """The candidate is admitted to *occupy* one child slot."""

    NON_OCCUPYING_CANDIDATE = "non_occupying_candidate"
    """The candidate needs no slot: a valid child task in a non-occupying
    state (``queued``/``blocked``/``terminal``). Reported with status
    ``ADMITTED`` for continuity, but it is **not** permission to spawn a
    child process; consult :attr:`SlotAdmissionDecision.occupies_slot`."""

    PROJECT_LIMIT_REACHED = "project_limit_reached"
    PROJECT_SLOT_LIMIT_REACHED = "project_slot_limit_reached"

    INVALID_LIMIT = "invalid_limit"
    MISSING_IDENTITY = "missing_identity"
    UNKNOWN_SLOT_KIND = "unknown_slot_kind"
    UNKNOWN_SLOT_STATE = "unknown_slot_state"
    UNKNOWN_SNAPSHOT_SOURCE = "unknown_snapshot_source"
    INCOMPLETE_SNAPSHOT = "incomplete_snapshot"
    OWNER_MISMATCH = "owner_mismatch"
    CONFLICTING_SLOT_RECORD = "conflicting_slot_record"
    CONFLICTING_PROJECT_IDENTITY = "conflicting_project_identity"
    CONFLICTING_TASK_IDENTITY = "conflicting_task_identity"
    CANDIDATE_NOT_CHILD_TASK = "candidate_not_child_task"
    SLOT_ALREADY_ACTIVE = "slot_already_active"


# Ordering is the enum definition order above; it makes multi-reason output
# deterministic and independent of dict/set iteration order.
_REASON_INDEX: dict[AdmissionReason, int] = {
    reason: index for index, reason in enumerate(AdmissionReason)
}

_DENIED_REASONS: frozenset[AdmissionReason] = frozenset(
    {
        AdmissionReason.INVALID_LIMIT,
        AdmissionReason.MISSING_IDENTITY,
        AdmissionReason.UNKNOWN_SLOT_KIND,
        AdmissionReason.UNKNOWN_SLOT_STATE,
        AdmissionReason.UNKNOWN_SNAPSHOT_SOURCE,
        AdmissionReason.INCOMPLETE_SNAPSHOT,
        AdmissionReason.OWNER_MISMATCH,
        AdmissionReason.CONFLICTING_SLOT_RECORD,
        AdmissionReason.CONFLICTING_PROJECT_IDENTITY,
        AdmissionReason.CONFLICTING_TASK_IDENTITY,
        AdmissionReason.CANDIDATE_NOT_CHILD_TASK,
        AdmissionReason.SLOT_ALREADY_ACTIVE,
    }
)

_REJECTED_REASONS: frozenset[AdmissionReason] = frozenset(
    {
        AdmissionReason.PROJECT_LIMIT_REACHED,
        AdmissionReason.PROJECT_SLOT_LIMIT_REACHED,
    }
)

# Reasons that mean the snapshot could not be fully validated, so any counts
# computed alongside them are not authoritative.
_STRUCTURAL_REASONS: frozenset[AdmissionReason] = frozenset(
    {
        AdmissionReason.INVALID_LIMIT,
        AdmissionReason.MISSING_IDENTITY,
        AdmissionReason.UNKNOWN_SLOT_KIND,
        AdmissionReason.UNKNOWN_SLOT_STATE,
        AdmissionReason.UNKNOWN_SNAPSHOT_SOURCE,
        AdmissionReason.INCOMPLETE_SNAPSHOT,
        AdmissionReason.OWNER_MISMATCH,
    }
)

_STATE_VALUES: frozenset[str] = frozenset(member.value for member in SlotState)
_KIND_VALUES: frozenset[str] = frozenset(member.value for member in SlotKind)
_OCCUPYING_VALUES: frozenset[str] = frozenset(member.value for member in OCCUPYING_SLOT_STATES)


@dataclass(frozen=True)
class SlotLimits:
    """Owner-configured, explicitly positive admission limits.

    Both dimensions are independent. ``derived_global_ceiling`` is reported for
    observability only and is never the admission test.
    """

    active_projects: int = DEFAULT_ACTIVE_PROJECT_LIMIT
    child_slots_per_project: int = DEFAULT_CHILD_SLOTS_PER_PROJECT_LIMIT

    @property
    def derived_global_ceiling(self) -> int:
        """Reported-only product of the two limits (default ``3 * 5 == 15``).

        Any non-positive-integer operand (``None``, a string, a bool, zero or
        negative) makes the ceiling unusable; it is reported as ``0`` instead
        of raising, because :func:`evaluate_slot_admission` already denies such
        limits with :attr:`AdmissionReason.INVALID_LIMIT`.
        """
        if _is_positive_int(self.active_projects) and _is_positive_int(
            self.child_slots_per_project
        ):
            return self.active_projects * self.child_slots_per_project
        return 0


def default_slot_limits() -> SlotLimits:
    """Return the owner-approved default limits (three projects, five each)."""
    return SlotLimits(
        active_projects=DEFAULT_ACTIVE_PROJECT_LIMIT,
        child_slots_per_project=DEFAULT_CHILD_SLOTS_PER_PROJECT_LIMIT,
    )


@dataclass(frozen=True)
class ProjectSlotRecord:
    """One observed slot record for an already-authoritative snapshot.

    ``project_id`` is the canonical, registered
    ``saberops.project.ProjectIdentity.project_id`` -- never a directory or
    branch name. ``slot_id`` is the stable execution-slot identity and
    ``task_id`` the identity of the child task that owns it. ``kind`` and
    ``state`` are untrusted strings validated against :class:`SlotKind` and
    :class:`SlotState` so that an unknown or misspelled value fails closed
    instead of being silently ignored.
    """

    project_id: str
    task_id: str
    slot_id: str
    kind: str
    state: str


@dataclass(frozen=True)
class SlotSnapshot:
    """An explicitly complete, authoritative observation of a project's slots.

    ``source`` must name the authoritative owner database. ``complete`` is the
    caller's explicit assertion that the enumeration is whole; the pure
    evaluator can check the flag but cannot itself prove completeness -- see
    the module docstring for the mandatory transactional caller.
    """

    owner_id: str
    source: str
    complete: bool
    slots: tuple[ProjectSlotRecord, ...] = ()

    @property
    def is_authoritative(self) -> bool:
        """True only for a complete snapshot from the authoritative source."""
        return _normalized(self.source) == SnapshotSource.OWNER_DB.value and self.complete


@dataclass(frozen=True)
class SlotAdmissionRequest:
    """A candidate child task asking for one execution slot."""

    owner_id: str
    candidate: ProjectSlotRecord
    limits: SlotLimits = field(default_factory=default_slot_limits)


@dataclass(frozen=True)
class SlotCounts:
    """Deterministic counts derived from a snapshot; never a free-capacity guess."""

    active_projects: int
    occupied_child_slots: int
    occupied_child_slots_by_project: tuple[tuple[str, int], ...]
    candidate_project_occupied: int
    project_limit: int
    child_slots_per_project_limit: int
    derived_global_ceiling: int
    excluded_records: int


@dataclass(frozen=True)
class SlotAdmissionDecision:
    """The stable, typed result of :func:`evaluate_slot_admission`."""

    status: AdmissionStatus
    reasons: tuple[AdmissionReason, ...]
    counts: SlotCounts
    counts_authoritative: bool
    detail: str

    @property
    def admitted(self) -> bool:
        """True iff the candidate may be admitted under the observed state."""
        return self.status is AdmissionStatus.ADMITTED

    @property
    def occupies_slot(self) -> bool:
        """Machine-readable occupancy discriminator for launch callers.

        True **only** when this admission authorizes the candidate to reserve
        or execute a child slot (status ``ADMITTED`` with reason ``admitted``).
        A valid non-occupying candidate (``queued``/``blocked``/``terminal``)
        reports status ``ADMITTED`` with reason ``non_occupying_candidate`` but
        ``occupies_slot`` false: it needs no slot and must never be interpreted
        as permission to spawn a child process.
        """
        return (
            self.status is AdmissionStatus.ADMITTED
            and self.reason is AdmissionReason.ADMITTED
        )

    @property
    def reason(self) -> AdmissionReason:
        """The highest-precedence reason code."""
        return self.reasons[0]

    def reason_codes(self) -> tuple[str, ...]:
        """Reason codes as plain strings, in deterministic order."""
        return tuple(reason.value for reason in self.reasons)


def _is_positive_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _reported_limit(value: object) -> int:
    """Return a positive-int limit unchanged, else ``0`` for a non-authoritative report."""
    return value if _is_positive_int(value) else 0


def _identity_text(value: object) -> str | None:
    """Return a non-empty trimmed string identity, else ``None`` for unknowns.

    ``None``, non-strings and blank/whitespace-only values are treated as an
    unknown identity so that a blank owner can never match another blank owner.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _normalized(value: object) -> str:
    """Trim + lowercase an enum-like token for case-insensitive validation."""
    return str(value).strip().lower()


def _identity(record: ProjectSlotRecord) -> tuple[str, str, str] | None:
    """Return the trimmed ``(project_id, task_id, slot_id)`` or ``None`` if any is empty."""
    parts: list[str] = []
    for raw in (record.project_id, record.task_id, record.slot_id):
        text = raw.strip() if isinstance(raw, str) else ""
        if not text:
            return None
        parts.append(text)
    return parts[0], parts[1], parts[2]


@dataclass(frozen=True)
class _ValidatedRecord:
    """Internal normalized view of one validated snapshot row."""

    project_id: str
    task_id: str
    slot_id: str
    kind: str
    state: str

    @property
    def occupies(self) -> bool:
        return self.kind == SlotKind.CHILD_TASK.value and self.state in _OCCUPYING_VALUES


def _ordered_reasons(collected: list[AdmissionReason]) -> tuple[AdmissionReason, ...]:
    unique = set(collected)
    return tuple(sorted(unique, key=lambda reason: _REASON_INDEX[reason]))


def _status_for(reasons: tuple[AdmissionReason, ...]) -> AdmissionStatus:
    if any(reason in _DENIED_REASONS for reason in reasons):
        return AdmissionStatus.DENIED
    if any(reason in _REJECTED_REASONS for reason in reasons):
        return AdmissionStatus.REJECTED
    return AdmissionStatus.ADMITTED


def _counts(
    *,
    limits: SlotLimits,
    occupied_by_project: dict[str, int],
    candidate_project: str,
    excluded_records: int,
) -> SlotCounts:
    ordered = tuple(sorted(occupied_by_project.items(), key=lambda item: item[0]))
    return SlotCounts(
        active_projects=len(ordered),
        occupied_child_slots=sum(count for _, count in ordered),
        occupied_child_slots_by_project=ordered,
        candidate_project_occupied=occupied_by_project.get(candidate_project, 0),
        project_limit=_reported_limit(limits.active_projects),
        child_slots_per_project_limit=_reported_limit(limits.child_slots_per_project),
        derived_global_ceiling=limits.derived_global_ceiling,
        excluded_records=excluded_records,
    )


def _empty_counts(limits: SlotLimits, candidate_project: str) -> SlotCounts:
    return _counts(
        limits=limits,
        occupied_by_project={},
        candidate_project=candidate_project,
        excluded_records=0,
    )


def _decision(
    reasons: tuple[AdmissionReason, ...],
    *,
    counts: SlotCounts,
    counts_authoritative: bool,
    detail: str,
) -> SlotAdmissionDecision:
    """Build a decision whose status is derived from its reason codes."""
    return SlotAdmissionDecision(
        status=_status_for(reasons),
        reasons=reasons,
        counts=counts,
        counts_authoritative=counts_authoritative,
        detail=detail,
    )


def evaluate_slot_admission(
    request: SlotAdmissionRequest, snapshot: SlotSnapshot
) -> SlotAdmissionDecision:
    """Return the deterministic admission decision for one candidate child task.

    Precedence is fixed and fail-closed: structural input errors (invalid
    limits, missing identity, unknown kind/state/source, incomplete snapshot,
    owner mismatch) deny; conflicting or replayed slot identities deny;
    only then are the two owner limits evaluated. A structural denial marks
    ``counts_authoritative`` false so no caller mistakes a partial enumeration
    for a free-capacity reading. The function never mutates its inputs, never
    emits a termination, and is deterministic for identical immutable inputs.
    """
    limits = request.limits
    candidate = request.candidate
    reasons: list[AdmissionReason] = []

    limits_valid = _is_positive_int(limits.active_projects) and _is_positive_int(
        limits.child_slots_per_project
    )
    if not limits_valid:
        reasons.append(AdmissionReason.INVALID_LIMIT)

    candidate_identity = _identity(candidate)
    if candidate_identity is None:
        reasons.append(AdmissionReason.MISSING_IDENTITY)
        candidate_project = ""
    else:
        candidate_project = candidate_identity[0]

    candidate_kind = _normalized(candidate.kind)
    if candidate_kind not in _KIND_VALUES:
        reasons.append(AdmissionReason.UNKNOWN_SLOT_KIND)

    candidate_state = _normalized(candidate.state)
    if candidate_state not in _STATE_VALUES:
        reasons.append(AdmissionReason.UNKNOWN_SLOT_STATE)

    source = _normalized(snapshot.source)
    if source != SnapshotSource.OWNER_DB.value:
        reasons.append(AdmissionReason.UNKNOWN_SNAPSHOT_SOURCE)

    if not snapshot.complete:
        reasons.append(AdmissionReason.INCOMPLETE_SNAPSHOT)

    snapshot_owner = _identity_text(snapshot.owner_id)
    request_owner = _identity_text(request.owner_id)
    if snapshot_owner is None or request_owner is None:
        # An unknown or blank owner on either side is never a match: a blank
        # snapshot owner and a blank request owner must not both be treated as
        # the same identity.
        reasons.append(AdmissionReason.MISSING_IDENTITY)
    elif snapshot_owner != request_owner:
        reasons.append(AdmissionReason.OWNER_MISMATCH)

    # Validate every snapshot row before any conflict or capacity reasoning.
    validated: list[_ValidatedRecord] = []
    for record in snapshot.slots:
        identity = _identity(record)
        if identity is None:
            reasons.append(AdmissionReason.MISSING_IDENTITY)
            continue
        kind = _normalized(record.kind)
        state = _normalized(record.state)
        if kind not in _KIND_VALUES:
            reasons.append(AdmissionReason.UNKNOWN_SLOT_KIND)
        if state not in _STATE_VALUES:
            reasons.append(AdmissionReason.UNKNOWN_SLOT_STATE)
        validated.append(
            _ValidatedRecord(
                project_id=identity[0],
                task_id=identity[1],
                slot_id=identity[2],
                kind=kind,
                state=state,
            )
        )

    structural = any(reason in _STRUCTURAL_REASONS for reason in reasons)
    if structural:
        return _decision(
            _ordered_reasons(reasons),
            counts=_empty_counts(limits, candidate_project),
            counts_authoritative=False,
            detail="denied: snapshot or request could not be fully validated",
        )

    # Collapse identical duplicates (aliases) and reject conflicts.
    unique: list[_ValidatedRecord] = []
    by_slot: dict[str, _ValidatedRecord] = {}
    task_slot: dict[tuple[str, str], str] = {}
    for row in validated:
        existing = by_slot.get(row.slot_id)
        if existing is not None:
            if existing.project_id != row.project_id:
                reasons.append(AdmissionReason.CONFLICTING_PROJECT_IDENTITY)
            elif existing != row:
                reasons.append(AdmissionReason.CONFLICTING_SLOT_RECORD)
            continue
        by_slot[row.slot_id] = row
        unique.append(row)
        task_key = (row.project_id, row.task_id)
        prior_slot = task_slot.get(task_key)
        if prior_slot is not None and prior_slot != row.slot_id:
            reasons.append(AdmissionReason.CONFLICTING_TASK_IDENTITY)
        else:
            task_slot[task_key] = row.slot_id

    if reasons:
        return _decision(
            _ordered_reasons(reasons),
            counts=_empty_counts(limits, candidate_project),
            counts_authoritative=False,
            detail="denied: conflicting or malformed slot identity in snapshot",
        )

    occupied_by_project: dict[str, int] = {}
    excluded_records = 0
    for row in unique:
        if row.occupies:
            occupied_by_project[row.project_id] = occupied_by_project.get(row.project_id, 0) + 1
        else:
            excluded_records += 1

    counts = _counts(
        limits=limits,
        occupied_by_project=occupied_by_project,
        candidate_project=candidate_project,
        excluded_records=excluded_records,
    )

    # Candidate-side resolution. A non-child candidate never consumes a slot; a
    # replay of an already-active child slot is not authorization to launch a
    # duplicate process.
    if candidate_kind != SlotKind.CHILD_TASK.value:
        return _decision(
            (AdmissionReason.CANDIDATE_NOT_CHILD_TASK,),
            counts=counts,
            counts_authoritative=True,
            detail="denied: candidate is not a child task and cannot occupy a slot",
        )

    candidate_occupies = candidate_state in _OCCUPYING_VALUES
    if candidate_occupies and candidate_identity is not None:
        _, candidate_task, candidate_slot = candidate_identity
        existing_slot = by_slot.get(candidate_slot)
        existing_task_slot = task_slot.get((candidate_project, candidate_task))
        if (existing_slot is not None and existing_slot.occupies) or (
            existing_task_slot is not None and existing_task_slot != candidate_slot
        ):
            return _decision(
                (AdmissionReason.SLOT_ALREADY_ACTIVE,),
                counts=counts,
                counts_authoritative=True,
                detail=f"denied: slot {candidate_slot!r} is already reserved or executing",
            )

    if not candidate_occupies:
        return _decision(
            (AdmissionReason.NON_OCCUPYING_CANDIDATE,),
            counts=counts,
            counts_authoritative=True,
            detail=(
                "admitted: non-occupying child state consumes no execution slot "
                "and is not permission to spawn a child process"
            ),
        )

    project_is_active = candidate_project in occupied_by_project
    if project_is_active:
        if counts.candidate_project_occupied >= limits.child_slots_per_project:
            return _decision(
                (AdmissionReason.PROJECT_SLOT_LIMIT_REACHED,),
                counts=counts,
                counts_authoritative=True,
                detail=(
                    f"rejected: project {candidate_project!r} already has "
                    f"{counts.candidate_project_occupied} executing child slots "
                    f"(limit {limits.child_slots_per_project})"
                ),
            )
    elif counts.active_projects >= limits.active_projects:
        return _decision(
            (AdmissionReason.PROJECT_LIMIT_REACHED,),
            counts=counts,
            counts_authoritative=True,
            detail=(
                f"rejected: {counts.active_projects} projects already have executing "
                f"child slots (limit {limits.active_projects})"
            ),
        )

    return _decision(
        (AdmissionReason.ADMITTED,),
        counts=counts,
        counts_authoritative=True,
        detail=f"admitted: project {candidate_project!r} may occupy one child slot",
    )
