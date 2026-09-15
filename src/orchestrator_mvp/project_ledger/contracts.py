"""Durable Project Ledger contracts: the typed shape of Project-level truth.

The Project owns authoritative state; an Orch session is disposable and its
context is a cache.  Every record here is therefore designed to be durable,
deterministically serializable and reconstructable without the session that
produced it: no conversation transcript, no provider session identifier, no
live process handle, and no model identity is ever load-bearing.

Two rules shape the whole model:

*Accepted history is superseded, never rewritten.*
    A record that has been accepted keeps its identity, statement and
    introduction version forever.  Change is expressed by a *new* record that
    names the one it supersedes, and by marking the old one ``SUPERSEDED``.

*Inference is provisional until explicitly accepted.*
    A takeover assumption enters as a ``PROVISIONAL`` record.  It is visible,
    attributable and reviewable, but it is never counted as Project truth and
    never appears among a checkpoint's authoritative references.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

#: Schema version of the canonical Project Ledger state rendering.
LEDGER_SCHEMA_VERSION = 1

#: Schema version of the canonical checkpoint manifest rendering.
CHECKPOINT_SCHEMA_VERSION = 1

#: Schema version of the bootstrap projection rendering.
BOOTSTRAP_SCHEMA_VERSION = 1


def canonical_json(payload: object) -> str:
    """Render ``payload`` as stable canonical JSON (sorted keys, compact)."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest_of(payload: object) -> str:
    """Return the sha256 hex digest of the canonical JSON rendering."""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


class ProjectLedgerError(RuntimeError):
    """Base class for every typed Project Ledger failure."""


class ProjectLedgerIntegrityError(ProjectLedgerError):
    """Raised when durable Project state fails to reconstruct or verify.

    Integrity failures are never repaired by trusting the newest row, the
    newest file or the largest record: they fail closed.
    """


class ProjectLedgerNotFoundError(ProjectLedgerError):
    """Raised when a project, record or checkpoint does not exist."""


class ProjectLedgerCommandError(ProjectLedgerError):
    """Raised when a typed mutation would violate a Project invariant."""


class ProjectLedgerConflictError(ProjectLedgerCommandError):
    """Raised when another session already advanced the project version.

    Two Orch sessions may hold ledger handles on the same store.  The version
    a mutation claims is guarded, so the loser of a race is told it lost
    instead of silently overwriting the winner's authoritative state.
    """


class CheckpointDigestError(ProjectLedgerIntegrityError):
    """Raised when a checkpoint's manifest does not match its digest."""


class CheckpointStateMismatchError(ProjectLedgerIntegrityError):
    """Raised when a checkpoint does not describe the state it claims."""


class CheckpointAncestryError(ProjectLedgerIntegrityError):
    """Raised when a checkpoint's parent chain is missing or inconsistent."""


class ProjectIdentityMismatchError(ProjectLedgerIntegrityError):
    """Raised when a checkpoint belongs to a different project."""


class UnsupportedSchemaVersionError(ProjectLedgerIntegrityError):
    """Raised when durable state declares a schema this build cannot read."""


class RecordStatus(StrEnum):
    """Lifecycle of an authoritative Project record.

    ``PROVISIONAL`` exists so inference can be captured without becoming
    truth.  Only ``ACCEPTED`` records are authoritative.
    """

    PROVISIONAL = "PROVISIONAL"
    ACCEPTED = "ACCEPTED"
    SUPERSEDED = "SUPERSEDED"
    RETIRED = "RETIRED"
    REJECTED = "REJECTED"


class FactCategory(StrEnum):
    """What kind of Project truth a fact carries."""

    GOAL = "GOAL"
    NON_GOAL = "NON_GOAL"
    CONSTRAINT = "CONSTRAINT"
    ARCHITECTURE = "ARCHITECTURE"
    ENVIRONMENT = "ENVIRONMENT"
    EVIDENCE = "EVIDENCE"
    MILESTONE = "MILESTONE"
    OBSERVATION = "OBSERVATION"
    ASSUMPTION = "ASSUMPTION"


class RecordOrigin(StrEnum):
    """How a record entered the ledger, strongest evidence first.

    This is an evidence grade, not an author.  ``DETERMINISTIC_OBSERVATION``
    means the value was read from the repository or the durable store by
    deterministic software; ``INFERENCE`` means somebody or something guessed.
    """

    DETERMINISTIC_OBSERVATION = "DETERMINISTIC_OBSERVATION"
    OWNER = "OWNER"
    ORCH = "ORCH"
    RUNTIME = "RUNTIME"
    INFERENCE = "INFERENCE"


class RoadmapStatus(StrEnum):
    """Work state of one roadmap item.

    The taxonomy deliberately keeps ``INTERRUPTED`` and
    ``OUTCOME_UNCERTAIN`` distinct from both ``IN_PROGRESS`` and
    ``COMPLETED``: an interruption must never be readable as completion, and
    an operation whose side effect could not be confirmed must never be
    readable as either success or failure.
    """

    PLANNED = "PLANNED"
    IN_PROGRESS = "IN_PROGRESS"
    INTERRUPTED = "INTERRUPTED"
    OUTCOME_UNCERTAIN = "OUTCOME_UNCERTAIN"
    COMPLETED = "COMPLETED"
    ABANDONED = "ABANDONED"


#: Roadmap states that describe work neither finished nor deliberately dropped.
INCOMPLETE_ROADMAP_STATUSES = frozenset(
    {
        RoadmapStatus.PLANNED,
        RoadmapStatus.IN_PROGRESS,
        RoadmapStatus.INTERRUPTED,
        RoadmapStatus.OUTCOME_UNCERTAIN,
    }
)

#: Roadmap states that describe work actually under way or recoverable.
IN_FLIGHT_ROADMAP_STATUSES = frozenset(
    {
        RoadmapStatus.IN_PROGRESS,
        RoadmapStatus.INTERRUPTED,
        RoadmapStatus.OUTCOME_UNCERTAIN,
    }
)


class QuestionStatus(StrEnum):
    """Lifecycle of an open question."""

    OPEN = "OPEN"
    RESOLVED = "RESOLVED"
    WITHDRAWN = "WITHDRAWN"


class IntakeDisposition(StrEnum):
    """What happened to a captured idea, requirement or correction.

    ``PENDING`` is the important one: capture must never be the same act as
    acceptance, so a newly recorded intake is explicitly not a requirement.
    """

    PENDING = "PENDING"
    ACCEPTED_AS_ROADMAP = "ACCEPTED_AS_ROADMAP"
    ACCEPTED_AS_DECISION = "ACCEPTED_AS_DECISION"
    ACCEPTED_AS_FACT = "ACCEPTED_AS_FACT"
    DEFERRED = "DEFERRED"
    REJECTED = "REJECTED"


class ProjectOrigin(StrEnum):
    """How the Project Ledger itself came into existence."""

    GENESIS = "GENESIS"
    TAKEOVER = "TAKEOVER"


class LedgerEventType(StrEnum):
    """Append-only event kinds describing authoritative mutations."""

    PROJECT_CREATED = "PROJECT_CREATED"
    FACT_RECORDED = "FACT_RECORDED"
    FACT_SUPERSEDED = "FACT_SUPERSEDED"
    FACT_ACCEPTED = "FACT_ACCEPTED"
    FACT_RETIRED = "FACT_RETIRED"
    DECISION_RECORDED = "DECISION_RECORDED"
    DECISION_SUPERSEDED = "DECISION_SUPERSEDED"
    ROADMAP_ITEM_ADDED = "ROADMAP_ITEM_ADDED"
    ROADMAP_ITEM_UPDATED = "ROADMAP_ITEM_UPDATED"
    OPEN_QUESTION_RECORDED = "OPEN_QUESTION_RECORDED"
    OPEN_QUESTION_RESOLVED = "OPEN_QUESTION_RESOLVED"
    CHANGE_INTAKE_RECORDED = "CHANGE_INTAKE_RECORDED"
    CHANGE_INTAKE_DISPOSED = "CHANGE_INTAKE_DISPOSED"


@dataclass(frozen=True)
class Provenance:
    """Historical evidence about who or what produced a record.

    Every field is *historical*.  Reconstruction must never need any of them,
    and in particular ``model`` and ``backend`` are recorded so the past can be
    audited, never so the future can be replayed: dropping them changes what a
    reader knows about the past, not what the next Orch can decide.
    """

    origin: RecordOrigin
    actor: str = ""
    reference: str = ""
    model: str | None = None
    backend: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Canonical rendering for durable storage."""
        return {
            "origin": self.origin.value,
            "actor": self.actor,
            "reference": self.reference,
            "model": self.model,
            "backend": self.backend,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Provenance:
        """Reconstruct from a persisted mapping."""
        return cls(
            origin=RecordOrigin(str(payload.get("origin", RecordOrigin.ORCH.value))),
            actor=str(payload.get("actor", "") or ""),
            reference=str(payload.get("reference", "") or ""),
            model=None if payload.get("model") is None else str(payload["model"]),
            backend=None if payload.get("backend") is None else str(payload["backend"]),
        )

    @classmethod
    def from_json(cls, raw: str) -> Provenance:
        """Reconstruct from persisted JSON, failing closed on garbage."""
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProjectLedgerIntegrityError(f"provenance is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ProjectLedgerIntegrityError("provenance must be a JSON object")
        return cls.from_dict(payload)


@dataclass(frozen=True)
class ProjectIdentityRecord:
    """The stable identity of one Project Ledger.

    ``project_id`` and ``root_commit`` are the accepted identity; they never
    change once written.  ``genesis`` holds the deterministic observations made
    when the ledger was created.
    """

    project_id: str
    canonical_path: str
    remote_identity: str
    root_commit: str
    origin: ProjectOrigin
    genesis: dict[str, Any]
    project_version: int
    schema_version: int
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        """Canonical rendering; ``updated_at`` is deliberately excluded.

        The identity's mutable bookkeeping timestamp is not Project truth, and
        including it would make an otherwise unchanged state digest depend on
        when the row was last touched.
        """
        return {
            "project_id": self.project_id,
            "canonical_path": self.canonical_path,
            "remote_identity": self.remote_identity,
            "root_commit": self.root_commit,
            "origin": self.origin.value,
            "genesis": self.genesis,
            "created_at": self.created_at,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class ProjectFact:
    """One durable, typed Project fact."""

    id: str
    project_id: str
    category: FactCategory
    statement: str
    reference: str
    origin: RecordOrigin
    status: RecordStatus
    provenance: Provenance
    introduced_version: int
    introduced_at: str
    supersedes: str | None = None
    superseded_by: str | None = None
    retired_version: int | None = None
    retired_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Canonical rendering used for the state digest and observability."""
        return {
            "id": self.id,
            "category": self.category.value,
            "statement": self.statement,
            "reference": self.reference,
            "origin": self.origin.value,
            "status": self.status.value,
            "provenance": self.provenance.to_dict(),
            "introduced_version": self.introduced_version,
            "introduced_at": self.introduced_at,
            "supersedes": self.supersedes,
            "superseded_by": self.superseded_by,
            "retired_version": self.retired_version,
            "retired_at": self.retired_at,
        }


@dataclass(frozen=True)
class ProjectDecision:
    """One durable Project decision with its rationale."""

    id: str
    project_id: str
    statement: str
    rationale: str
    reference: str
    origin: RecordOrigin
    status: RecordStatus
    provenance: Provenance
    introduced_version: int
    introduced_at: str
    supersedes: str | None = None
    superseded_by: str | None = None
    retired_version: int | None = None
    retired_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Canonical rendering used for the state digest and observability."""
        return {
            "id": self.id,
            "statement": self.statement,
            "rationale": self.rationale,
            "reference": self.reference,
            "origin": self.origin.value,
            "status": self.status.value,
            "provenance": self.provenance.to_dict(),
            "introduced_version": self.introduced_version,
            "introduced_at": self.introduced_at,
            "supersedes": self.supersedes,
            "superseded_by": self.superseded_by,
            "retired_version": self.retired_version,
            "retired_at": self.retired_at,
        }


@dataclass(frozen=True)
class RoadmapItem:
    """One durable roadmap item and its versioned work state."""

    id: str
    project_id: str
    title: str
    status: RoadmapStatus
    ordering: int
    depends_on: tuple[str, ...]
    acceptance_ref: str
    result_ref: str
    run_ref: str | None
    origin: RecordOrigin
    provenance: Provenance
    revision: int
    introduced_version: int
    introduced_at: str
    updated_version: int
    updated_at: str

    @property
    def is_incomplete(self) -> bool:
        """True while the item is neither completed nor deliberately dropped."""
        return self.status in INCOMPLETE_ROADMAP_STATUSES

    @property
    def is_in_flight(self) -> bool:
        """True while work is under way, interrupted, or of uncertain outcome."""
        return self.status in IN_FLIGHT_ROADMAP_STATUSES

    def to_dict(self) -> dict[str, Any]:
        """Canonical rendering used for the state digest and observability."""
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status.value,
            "ordering": self.ordering,
            "depends_on": sorted(self.depends_on),
            "acceptance_ref": self.acceptance_ref,
            "result_ref": self.result_ref,
            "run_ref": self.run_ref,
            "origin": self.origin.value,
            "provenance": self.provenance.to_dict(),
            "revision": self.revision,
            "introduced_version": self.introduced_version,
            "introduced_at": self.introduced_at,
            "updated_version": self.updated_version,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class OpenQuestion:
    """One durable open question and, once known, its resolution."""

    id: str
    project_id: str
    question: str
    status: QuestionStatus
    resolution: str
    resolution_ref: str
    origin: RecordOrigin
    provenance: Provenance
    introduced_version: int
    introduced_at: str
    resolved_version: int | None = None
    resolved_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Canonical rendering used for the state digest and observability."""
        return {
            "id": self.id,
            "question": self.question,
            "status": self.status.value,
            "resolution": self.resolution,
            "resolution_ref": self.resolution_ref,
            "origin": self.origin.value,
            "provenance": self.provenance.to_dict(),
            "introduced_version": self.introduced_version,
            "introduced_at": self.introduced_at,
            "resolved_version": self.resolved_version,
            "resolved_at": self.resolved_at,
        }


@dataclass(frozen=True)
class ChangeIntake:
    """One captured idea, new requirement or correction awaiting disposition."""

    id: str
    project_id: str
    summary: str
    detail: str
    origin: RecordOrigin
    disposition: IntakeDisposition
    provenance: Provenance
    recorded_version: int
    recorded_at: str
    linked_fact_id: str | None = None
    linked_decision_id: str | None = None
    linked_roadmap_id: str | None = None
    decided_version: int | None = None
    decided_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Canonical rendering used for the state digest and observability."""
        return {
            "id": self.id,
            "summary": self.summary,
            "detail": self.detail,
            "origin": self.origin.value,
            "disposition": self.disposition.value,
            "provenance": self.provenance.to_dict(),
            "recorded_version": self.recorded_version,
            "recorded_at": self.recorded_at,
            "linked_fact_id": self.linked_fact_id,
            "linked_decision_id": self.linked_decision_id,
            "linked_roadmap_id": self.linked_roadmap_id,
            "decided_version": self.decided_version,
            "decided_at": self.decided_at,
        }


@dataclass(frozen=True)
class LedgerEvent:
    """One append-only record of an authoritative Project mutation."""

    seq: int
    project_id: str
    event_type: LedgerEventType
    subject_kind: str
    subject_id: str
    project_version: int
    state_digest: str
    payload: dict[str, Any]
    created_at: str


@dataclass(frozen=True)
class ProjectState:
    """The complete authoritative state of one Project at one version.

    This is the *authority*.  Checkpoints reference it and bootstraps project
    from it; neither may replace it, and nothing here is derived from an LLM
    summary.
    """

    identity: ProjectIdentityRecord
    facts: tuple[ProjectFact, ...] = ()
    decisions: tuple[ProjectDecision, ...] = ()
    roadmap: tuple[RoadmapItem, ...] = ()
    open_questions: tuple[OpenQuestion, ...] = ()
    change_intake: tuple[ChangeIntake, ...] = ()

    @property
    def project_id(self) -> str:
        """The project this state belongs to."""
        return self.identity.project_id

    @property
    def project_version(self) -> int:
        """The authoritative version this state represents."""
        return self.identity.project_version

    def to_dict(self) -> dict[str, Any]:
        """Deterministic canonical rendering of the whole authoritative state.

        Records are emitted in id order so insertion order, row order and
        directory order can never influence the digest.
        """
        return {
            "schema_version": LEDGER_SCHEMA_VERSION,
            "project_version": self.identity.project_version,
            "identity": self.identity.to_dict(),
            "facts": [record.to_dict() for record in sorted(self.facts, key=lambda r: r.id)],
            "decisions": [
                record.to_dict() for record in sorted(self.decisions, key=lambda r: r.id)
            ],
            "roadmap": [record.to_dict() for record in sorted(self.roadmap, key=lambda r: r.id)],
            "open_questions": [
                record.to_dict() for record in sorted(self.open_questions, key=lambda r: r.id)
            ],
            "change_intake": [
                record.to_dict() for record in sorted(self.change_intake, key=lambda r: r.id)
            ],
        }

    @property
    def digest(self) -> str:
        """The canonical state digest: identical state, identical digest."""
        return digest_of(self.to_dict())

    def accepted_facts(self, category: FactCategory | None = None) -> tuple[ProjectFact, ...]:
        """Accepted facts, optionally of one category, in id order."""
        return tuple(
            fact
            for fact in sorted(self.facts, key=lambda r: r.id)
            if fact.status is RecordStatus.ACCEPTED
            and (category is None or fact.category is category)
        )

    def provisional_facts(self) -> tuple[ProjectFact, ...]:
        """Facts that are captured but explicitly not Project truth yet."""
        return tuple(
            fact
            for fact in sorted(self.facts, key=lambda r: r.id)
            if fact.status is RecordStatus.PROVISIONAL
        )

    def accepted_decisions(self) -> tuple[ProjectDecision, ...]:
        """Currently accepted decisions in id order."""
        return tuple(
            decision
            for decision in sorted(self.decisions, key=lambda r: r.id)
            if decision.status is RecordStatus.ACCEPTED
        )

    def active_roadmap(self) -> tuple[RoadmapItem, ...]:
        """Roadmap items still to be finished, in deterministic work order."""
        return tuple(
            sorted(
                (item for item in self.roadmap if item.is_incomplete),
                key=lambda item: (item.ordering, item.id),
            )
        )

    def in_flight_roadmap(self) -> tuple[RoadmapItem, ...]:
        """Roadmap items under way, interrupted, or of uncertain outcome."""
        return tuple(
            sorted(
                (item for item in self.roadmap if item.is_in_flight),
                key=lambda item: (item.ordering, item.id),
            )
        )

    def open_question_records(self) -> tuple[OpenQuestion, ...]:
        """Questions still awaiting an answer, in id order."""
        return tuple(
            question
            for question in sorted(self.open_questions, key=lambda r: r.id)
            if question.status is QuestionStatus.OPEN
        )

    def pending_intake(self) -> tuple[ChangeIntake, ...]:
        """Captured ideas that have not been dispositioned, in id order."""
        return tuple(
            intake
            for intake in sorted(self.change_intake, key=lambda r: r.id)
            if intake.disposition is IntakeDisposition.PENDING
        )

    def record_ids(self) -> frozenset[str]:
        """Every durable record identity in this state."""
        return frozenset(
            {
                *(record.id for record in self.facts),
                *(record.id for record in self.decisions),
                *(record.id for record in self.roadmap),
                *(record.id for record in self.open_questions),
                *(record.id for record in self.change_intake),
            }
        )


@dataclass(frozen=True)
class ProjectCheckpoint:
    """A deterministic manifest of authoritative Project state at one version.

    A checkpoint is not a summary and carries no prose: only identities,
    versions and digests.  That is what makes it verifiable, secret-free and
    impossible to confuse with the state it points at.
    """

    checkpoint_id: str
    project_id: str
    project_version: int
    parent_checkpoint_id: str | None
    created_at: str
    state_digest: str
    schema_version: int = CHECKPOINT_SCHEMA_VERSION
    authoritative_refs: tuple[str, ...] = ()
    active_roadmap_refs: tuple[str, ...] = ()
    active_decision_refs: tuple[str, ...] = ()
    open_question_refs: tuple[str, ...] = ()
    current_work_refs: tuple[str, ...] = ()
    non_goal_refs: tuple[str, ...] = ()
    recovery_refs: tuple[str, ...] = ()
    provisional_refs: tuple[str, ...] = ()
    pending_intake_refs: tuple[str, ...] = ()

    def manifest(self) -> dict[str, Any]:
        """Canonical manifest rendering; the digest is taken over exactly this."""
        return {
            "schema_version": self.schema_version,
            "project_id": self.project_id,
            "project_version": self.project_version,
            "parent_checkpoint_id": self.parent_checkpoint_id,
            "created_at": self.created_at,
            "state_digest": self.state_digest,
            "authoritative_refs": sorted(self.authoritative_refs),
            "active_roadmap_refs": sorted(self.active_roadmap_refs),
            "active_decision_refs": sorted(self.active_decision_refs),
            "open_question_refs": sorted(self.open_question_refs),
            "current_work_refs": sorted(self.current_work_refs),
            "non_goal_refs": sorted(self.non_goal_refs),
            "recovery_refs": sorted(self.recovery_refs),
            "provisional_refs": sorted(self.provisional_refs),
            "pending_intake_refs": sorted(self.pending_intake_refs),
        }

    @property
    def digest(self) -> str:
        """Digest over the canonical manifest, excluding the derived id."""
        return digest_of(self.manifest())

    def referenced_ids(self) -> frozenset[str]:
        """Every durable record this checkpoint points at."""
        return frozenset(
            {
                *self.authoritative_refs,
                *self.active_roadmap_refs,
                *self.active_decision_refs,
                *self.open_question_refs,
                *self.current_work_refs,
                *self.non_goal_refs,
                *self.recovery_refs,
                *self.provisional_refs,
                *self.pending_intake_refs,
            }
        )


@dataclass(frozen=True)
class BootstrapBudget:
    """A bounded projection budget.

    Continuity is not solved by dumping the Project database into a prompt.
    The budget caps each typed category; anything not selected stays in the
    ledger, which remains the authority.  A later C11 tranche can make this
    token-aware without changing any durable schema.
    """

    max_facts: int = 32
    max_decisions: int = 32
    max_roadmap_items: int = 32
    max_open_questions: int = 32
    max_non_goals: int = 32
    max_provisional: int = 32
    max_intake: int = 32

    def to_dict(self) -> dict[str, Any]:
        """Canonical rendering for bootstrap evidence."""
        return {
            "max_facts": self.max_facts,
            "max_decisions": self.max_decisions,
            "max_roadmap_items": self.max_roadmap_items,
            "max_open_questions": self.max_open_questions,
            "max_non_goals": self.max_non_goals,
            "max_provisional": self.max_provisional,
            "max_intake": self.max_intake,
        }


@dataclass(frozen=True)
class RuntimeComposition:
    """Read-only C05 runtime facts about one referenced run.

    C11 owns Project-level continuity; C05 continues to own run and runtime
    lifecycle semantics.  This view *reads* C05 state so a bootstrap can say
    "this project work is attached to a run that was interrupted" without
    copying, re-deciding or duplicating runtime recovery.
    """

    run_ref: str
    run_known: bool
    run_status: str | None = None
    latest_activation_state: str | None = None
    uncertain_side_effects: tuple[str, ...] = ()
    interrupted: bool = False
    termination_uncertain: bool = False

    @property
    def outcome_uncertain(self) -> bool:
        """True when a recorded effect never reached a confirmed state."""
        return bool(self.uncertain_side_effects) or self.termination_uncertain

    def to_dict(self) -> dict[str, Any]:
        """Canonical rendering for the bootstrap projection."""
        return {
            "run_ref": self.run_ref,
            "run_known": self.run_known,
            "run_status": self.run_status,
            "latest_activation_state": self.latest_activation_state,
            "uncertain_side_effects": sorted(self.uncertain_side_effects),
            "interrupted": self.interrupted,
            "termination_uncertain": self.termination_uncertain,
            "outcome_uncertain": self.outcome_uncertain,
        }


@dataclass(frozen=True)
class InFlightWork:
    """One incomplete roadmap item plus its optional runtime composition."""

    item: RoadmapItem
    runtime: RuntimeComposition | None = None

    def to_dict(self) -> dict[str, Any]:
        """Canonical rendering for the bootstrap projection."""
        return {
            "item": self.item.to_dict(),
            "runtime": None if self.runtime is None else self.runtime.to_dict(),
        }


@dataclass(frozen=True)
class OrchBootstrap:
    """A bounded, regenerable projection of durable Project state.

    A bootstrap is a cache.  It can be rebuilt from the ledger at any time, and
    no durable Project state may depend on one existing.  It is what a fresh
    Orch reads instead of its predecessor's transcript.
    """

    schema_version: int
    project_id: str
    canonical_path: str
    root_commit: str
    project_version: int
    checkpoint_id: str
    checkpoint_digest: str
    state_digest: str
    goals: tuple[ProjectFact, ...] = ()
    non_goals: tuple[ProjectFact, ...] = ()
    decisions: tuple[ProjectDecision, ...] = ()
    roadmap: tuple[RoadmapItem, ...] = ()
    open_questions: tuple[OpenQuestion, ...] = ()
    in_flight: tuple[InFlightWork, ...] = ()
    evidence: tuple[ProjectFact, ...] = ()
    provisional_assumptions: tuple[ProjectFact, ...] = ()
    pending_intake: tuple[ChangeIntake, ...] = ()
    continuation_instructions: tuple[str, ...] = ()
    budget: BootstrapBudget = field(default_factory=BootstrapBudget)
    omitted: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Canonical rendering of the projection."""
        return {
            "schema_version": self.schema_version,
            "project": {
                "project_id": self.project_id,
                "canonical_path": self.canonical_path,
                "root_commit": self.root_commit,
                "project_version": self.project_version,
            },
            "checkpoint": {
                "checkpoint_id": self.checkpoint_id,
                "checkpoint_digest": self.checkpoint_digest,
                "state_digest": self.state_digest,
            },
            "goals": [record.to_dict() for record in self.goals],
            "non_goals": [record.to_dict() for record in self.non_goals],
            "decisions": [record.to_dict() for record in self.decisions],
            "roadmap": [record.to_dict() for record in self.roadmap],
            "open_questions": [record.to_dict() for record in self.open_questions],
            "in_flight": [record.to_dict() for record in self.in_flight],
            "evidence": [record.to_dict() for record in self.evidence],
            "provisional_assumptions": [
                record.to_dict() for record in self.provisional_assumptions
            ],
            "pending_intake": [record.to_dict() for record in self.pending_intake],
            "continuation_instructions": list(self.continuation_instructions),
            "budget": self.budget.to_dict(),
            "omitted": dict(sorted(self.omitted.items())),
        }
