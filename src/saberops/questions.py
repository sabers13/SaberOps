"""Owner question queue and the five-part escalation protocol (Q-1, Step 012).

What this module is
-------------------
One service layer for the owner question queue of ADR-0004 §3 D6 and the
Step 012 outcome.  A question is **keyed**: the same key asked twice is one
question, never a duplicate (idempotent tool contract, ADR-0004 invariant 13).
Any role may raise one, including a worker mid-run.

The invariants this module owns
-------------------------------
* An answer stores the owner's **exact words**.  Nothing is paraphrased,
  trimmed or normalized.
* A question leaves ``OPEN`` only through an explicit verb: ``answer``,
  ``defer`` (a postponement with a date, not a close) or an
  **evidence-backed** ``moot`` close.  Task completion and storage cleanup
  have no close authority at all -- :data:`TASK_LIFECYCLE_CLOSE_AUTHORITIES`
  is deliberately empty and this module exposes no delete/prune/clear path.
* A later conflicting answer is recorded as a **divergent record** and
  reported; the primary answer is never overwritten and nothing is
  auto-closed.  Changing an owner decision requires a new question that
  ``supersedes`` the old one.
* A deferred question resurfaces on its date.
* Every decision the Orchestrator took alone appears in the audit list, with
  the evidence and the rejected alternative.
* A repeated same-kind owner answer becomes a **standing-rule proposal** for
  CFG-1 (Step 013).  Proposals are inert data: this module applies nothing.

The five-part escalation protocol
---------------------------------
Every question carries an :class:`Escalation` with exactly the five parts the
owner demanded: the requirement that forces the question, the proposed
expansion, the smallest compliant option, the concrete consequences and the
Orchestrator's recommendation.  A question cannot exist without all five parts.

Authority
---------
:func:`resolve_answer_authority` maps a :class:`DecisionClass` onto its answer
authority.  The owner-only classes of ADR-0004 D6 (moving ``main``, pushing,
quarantine, billing/posture, test changes against an accepted ADR, deleting
outside the engine's own folders, approving config proposals) resolve to
``OWNER``; the Orchestrator-answerable criteria (classifying review findings,
fix-up content, routing a G3 failure where the rules allow two targets, and
other routine questions acknowledged under Q2) resolve to ``ORCHESTRATOR``;
anything unknown fails closed to ``OWNER``.  An implementer role can never
answer a question, and an Orchestrator can never answer its own finding.

Storage
-------
One owner-level JSON document at ``$XDG_STATE_HOME/saberops/questions.json``
(override ``ORCH_QUESTIONS_STORE`` for tests and diagnostics).  Writes are
atomic, and an unreadable or malformed document fails closed instead of
degrading to an empty queue.  Questions are evidence, so the document is never
reduced by cleanup.

Entry points
------------
``saberops.cli`` exposes ``orch questions ...`` and
:func:`build_questions_router` returns a FastAPI ``APIRouter`` for the local
web UI.  Both call exactly the verbs of :class:`OwnerQuestionQueue` and hold no
authority of their own.  The web layer must apply its existing local-only
origin guard when it mounts the router; the router additionally refuses any
non-loopback client.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

__all__ = [
    "ASK_EVENT_VERB",
    "CFG_1_TARGET",
    "ESCALATION_PARTS",
    "IMPLEMENTER_ROLES",
    "ORCHESTRATOR_ANSWERABLE_DECISION_CLASSES",
    "OWNER_ONLY_DECISION_CLASSES",
    "QUESTIONS_ROUTER_PREFIX",
    "QUESTIONS_STORE_ENV",
    "QUESTIONS_STORE_SCHEMA_VERSION",
    "QUESTION_EVENT_VERBS",
    "QUESTION_TRANSITION_VERBS",
    "STANDING_RULE_THRESHOLD",
    "TASK_LIFECYCLE_CLOSE_AUTHORITIES",
    "AnswerAuthority",
    "AnswerAuthorityError",
    "AskRole",
    "DecisionClass",
    "DivergenceReason",
    "DivergenceReport",
    "DivergentRecord",
    "Escalation",
    "EscalationError",
    "OrchestratorAloneDecision",
    "OwnerAnswer",
    "OwnerQuestion",
    "OwnerQuestionQueue",
    "QuestionError",
    "QuestionEvent",
    "QuestionLedger",
    "QuestionRequest",
    "QuestionStateError",
    "QuestionStatus",
    "QuestionStoreError",
    "StandingRuleProposal",
    "build_questions_router",
    "get_questions_store_path",
    "is_due",
    "is_orchestrator_answerable",
    "question_id_for_key",
    "question_request",
    "question_view",
    "request_from_payload",
    "resolve_answer_authority",
]

#: Schema version of the durable question document.
QUESTIONS_STORE_SCHEMA_VERSION = 1

#: Test/diagnostics override for the question document path.
QUESTIONS_STORE_ENV = "ORCH_QUESTIONS_STORE"

#: Default number of repeated same-kind answers that becomes a standing rule.
STANDING_RULE_THRESHOLD = 3

#: The step that owns standing-rule proposals (CFG-1, Step 013).
CFG_1_TARGET = "CFG-1"

#: The base path :func:`build_questions_router` mounts.
QUESTIONS_ROUTER_PREFIX = "/questions"

#: The five required escalation parts, in the owner's order.
ESCALATION_PARTS: tuple[str, ...] = (
    "requirement",
    "proposed_expansion",
    "smallest_compliant",
    "consequences",
    "recommendation",
)

#: The only verbs that may change a question's status.
QUESTION_TRANSITION_VERBS: frozenset[str] = frozenset(
    {"answer", "defer", "mark_moot", "record_divergence"}
)

#: The verb recorded when a question is first asked.  Creation is not a status
#: transition, so it stays out of :data:`QUESTION_TRANSITION_VERBS`.
ASK_EVENT_VERB = "ask"

#: Every verb that may appear in a question's append-only audit trail.
QUESTION_EVENT_VERBS: frozenset[str] = QUESTION_TRANSITION_VERBS | {ASK_EVENT_VERB}

#: Sources permitted to close a question when a task completes or is cleaned
#: up.  Deliberately empty: a question is evidence and is closed only by an
#: explicit owner verb, never by task lifecycle or storage cleanup.
TASK_LIFECYCLE_CLOSE_AUTHORITIES: frozenset[str] = frozenset()


class QuestionError(ValueError):
    """Base class for every typed refusal of the question queue."""


class QuestionStoreError(QuestionError):
    """The durable question document is unreadable, malformed or unwritable."""


class EscalationError(QuestionError):
    """A question was proposed without all five escalation parts."""


class QuestionStateError(QuestionError):
    """The requested verb is not allowed in the question's current status."""


class AnswerAuthorityError(QuestionError):
    """The requested answer authority may not answer this question."""


class AskRole(StrEnum):
    """The role that raised a question or performed a queue verb."""

    OWNER = "OWNER"
    ORCHESTRATOR = "ORCHESTRATOR"
    WORKER = "WORKER"
    SURGEON = "SURGEON"
    REVIEWER = "REVIEWER"
    INTEGRATOR = "INTEGRATOR"
    ENGINE = "ENGINE"


#: Roles that implement work or review it.  None of them may answer a
#: question: answering is owner or Orchestrator authority only.
IMPLEMENTER_ROLES: frozenset[AskRole] = frozenset(
    {AskRole.WORKER, AskRole.SURGEON, AskRole.REVIEWER, AskRole.INTEGRATOR}
)


class AnswerAuthority(StrEnum):
    """Who may answer a question."""

    OWNER = "OWNER"
    ORCHESTRATOR = "ORCHESTRATOR"


class QuestionStatus(StrEnum):
    """The durable status of one question."""

    OPEN = "OPEN"
    DEFERRED = "DEFERRED"
    ANSWERED = "ANSWERED"
    MOOT = "MOOT"


class DecisionClass(StrEnum):
    """The decision kind a question asks about.

    The split is the ADR-0004 D6 one.  Owner-only decisions can only ever be
    answered by the owner (ADR-0004 invariant 6).
    """

    # Owner-only (ADR-0004 D6).
    MOVE_MAIN = "MOVE_MAIN"
    PUSH = "PUSH"
    QUARANTINE = "QUARANTINE"
    BILLING_OR_POSTURE = "BILLING_OR_POSTURE"
    TEST_CHANGE_AGAINST_ADR = "TEST_CHANGE_AGAINST_ADR"
    DELETE_OUTSIDE_ENGINE_FOLDERS = "DELETE_OUTSIDE_ENGINE_FOLDERS"
    APPROVE_CONFIG_PROPOSAL = "APPROVE_CONFIG_PROPOSAL"
    # Orchestrator-answerable (ADR-0004 D6, accepted answer Q2).
    REVIEW_FINDING_CLASSIFICATION = "REVIEW_FINDING_CLASSIFICATION"
    FIX_UP_CONTENT = "FIX_UP_CONTENT"
    G3_FAILURE_ROUTING = "G3_FAILURE_ROUTING"
    ROUTINE_QUESTION = "ROUTINE_QUESTION"
    # Anything the ADR does not classify: owner-only by default (fail closed).
    OTHER = "OTHER"


#: Decisions only the owner may take (ADR-0004 D6).
OWNER_ONLY_DECISION_CLASSES: frozenset[DecisionClass] = frozenset(
    {
        DecisionClass.MOVE_MAIN,
        DecisionClass.PUSH,
        DecisionClass.QUARANTINE,
        DecisionClass.BILLING_OR_POSTURE,
        DecisionClass.TEST_CHANGE_AGAINST_ADR,
        DecisionClass.DELETE_OUTSIDE_ENGINE_FOLDERS,
        DecisionClass.APPROVE_CONFIG_PROPOSAL,
        DecisionClass.OTHER,
    }
)

#: Decisions the Orchestrator may answer without the owner (ADR-0004 D6 / Q2).
ORCHESTRATOR_ANSWERABLE_DECISION_CLASSES: frozenset[DecisionClass] = frozenset(
    {
        DecisionClass.REVIEW_FINDING_CLASSIFICATION,
        DecisionClass.FIX_UP_CONTENT,
        DecisionClass.G3_FAILURE_ROUTING,
        DecisionClass.ROUTINE_QUESTION,
    }
)


class DivergenceReason(StrEnum):
    """Why a later record conflicts with the question's current outcome."""

    DIFFERENT_WORDS = "DIFFERENT_WORDS"
    DIFFERENT_OPTION = "DIFFERENT_OPTION"
    ANSWER_AFTER_MOOT = "ANSWER_AFTER_MOOT"
    MOOT_AFTER_ANSWER = "MOOT_AFTER_ANSWER"
    DIFFERENT_EVIDENCE = "DIFFERENT_EVIDENCE"


def resolve_answer_authority(decision_class: DecisionClass) -> AnswerAuthority:
    """Return the authority allowed to answer ``decision_class``.

    Only the explicit Orchestrator-answerable list resolves to
    ``ORCHESTRATOR``; every other class -- including an unknown one -- fails
    closed to ``OWNER``.
    """
    if decision_class in ORCHESTRATOR_ANSWERABLE_DECISION_CLASSES:
        return AnswerAuthority.ORCHESTRATOR
    return AnswerAuthority.OWNER


def is_orchestrator_answerable(decision_class: DecisionClass) -> bool:
    """Return True only for an explicitly Orchestrator-answerable decision."""
    return decision_class in ORCHESTRATOR_ANSWERABLE_DECISION_CLASSES


def question_id_for_key(key: str) -> str:
    """Return the deterministic question id for a key.

    Determinism is what makes ``ask`` idempotent: the same key always maps to
    the same id, so a retried ask can never create a second question.
    """
    if not isinstance(key, str) or not key.strip():
        raise QuestionError("a question key is required")
    digest = hashlib.sha256(key.strip().encode("utf-8")).hexdigest()
    return f"q-{digest[:12]}"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _require_text(value: object, field_name: str) -> str:
    """Return non-empty stripped text, or fail closed."""
    if not isinstance(value, str) or not value.strip():
        raise QuestionStoreError(f"{field_name} is required and must be non-empty text")
    return value.strip()


def _require_verbatim(value: object, field_name: str) -> str:
    """Return text exactly as given; only emptiness is refused."""
    if not isinstance(value, str) or not value.strip():
        raise QuestionStoreError(f"{field_name} is required and must be non-empty text")
    return value


def _optional_text(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise QuestionStoreError(f"{field_name} must be text when present")
    return value.strip() or None


def _text_tuple(value: object, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise QuestionStoreError(f"{field_name} must be a list of text values")
    out = tuple(_require_text(item, field_name) for item in value)
    if len(set(out)) != len(out):
        raise QuestionStoreError(f"{field_name} must not repeat a value")
    return out


def _iso_date(value: object, field_name: str) -> str:
    """Normalize an ISO date (``YYYY-MM-DD``) or fail closed."""
    if isinstance(value, datetime):
        raise QuestionStoreError(f"{field_name} must be a date, not a timestamp")
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str) or not value.strip():
        raise QuestionStoreError(f"{field_name} must be an ISO date (YYYY-MM-DD)")
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError as exc:
        raise QuestionStoreError(
            f"{field_name} must be an ISO date (YYYY-MM-DD); got {value!r}"
        ) from exc


def _enum[E: StrEnum](enum_type: type[E], value: object, field_name: str) -> E:
    if not isinstance(value, str):
        raise QuestionStoreError(f"unknown {field_name}: {value!r}")
    try:
        return enum_type(value)
    except ValueError as exc:
        raise QuestionStoreError(f"unknown {field_name}: {value!r}") from exc


def _ask_role(value: object, field_name: str) -> AskRole:
    return _enum(AskRole, value, field_name)


def _authority(value: object, field_name: str) -> AnswerAuthority:
    return _enum(AnswerAuthority, value, field_name)


def _decision_class(value: object, field_name: str) -> DecisionClass:
    return _enum(DecisionClass, value, field_name)


def _status(value: object, field_name: str) -> QuestionStatus:
    return _enum(QuestionStatus, value, field_name)


def _divergence_reason(value: object, field_name: str) -> DivergenceReason:
    return _enum(DivergenceReason, value, field_name)


def _mapping(value: object, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise QuestionStoreError(f"{field_name} must be an object")
    return value


def _sequence(value: object, field_name: str) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise QuestionStoreError(f"{field_name} must be a list")
    return list(value)


@dataclass(frozen=True)
class Escalation:
    """The five-part escalation a question must carry.

    Order and naming follow the owner's protocol exactly: the requirement that
    forces the question, the proposed expansion, the smallest compliant
    option, the concrete consequences and the Orchestrator's recommendation.
    """

    requirement: str
    proposed_expansion: str
    smallest_compliant: str
    consequences: str
    recommendation: str

    def __post_init__(self) -> None:
        for part in ESCALATION_PARTS:
            value = getattr(self, part)
            if not isinstance(value, str) or not value.strip():
                raise EscalationError(
                    f"escalation part '{part}' is required and must be non-empty text"
                )

    def to_mapping(self) -> dict[str, str]:
        """Render the five parts verbatim."""
        return {part: getattr(self, part) for part in ESCALATION_PARTS}

    @classmethod
    def from_mapping(cls, payload: object) -> Escalation:
        """Rebuild one escalation from its durable mapping, fail-closed."""
        data = _mapping(payload, "escalation")
        missing = [part for part in ESCALATION_PARTS if part not in data]
        if missing:
            raise EscalationError(
                "escalation is missing required part(s): " + ", ".join(missing)
            )
        parts: dict[str, str] = {}
        for part in ESCALATION_PARTS:
            value = data[part]
            if not isinstance(value, str) or not value.strip():
                raise EscalationError(
                    f"escalation part '{part}' is required and must be non-empty text"
                )
            parts[part] = value
        return cls(**parts)


@dataclass(frozen=True)
class QuestionRequest:
    """The immutable ask payload of one keyed question."""

    key: str
    subject: str
    kind: str
    decision_class: DecisionClass
    raised_by_role: AskRole
    raised_by: str
    escalation: Escalation
    scope: str | None = None
    run_id: str | None = None
    blocks: tuple[str, ...] = ()
    options: tuple[str, ...] = ()
    default_option: str | None = None
    supersedes: str | None = None

    def __post_init__(self) -> None:
        key = _require_text(self.key, "key")
        object.__setattr__(self, "key", key)
        object.__setattr__(self, "subject", _require_text(self.subject, "subject"))
        object.__setattr__(self, "kind", _require_text(self.kind, "kind"))
        object.__setattr__(self, "raised_by", _require_text(self.raised_by, "raised_by"))
        if not isinstance(self.decision_class, DecisionClass):
            raise QuestionStoreError("decision_class must be a DecisionClass")
        if not isinstance(self.raised_by_role, AskRole):
            raise QuestionStoreError("raised_by_role must be an AskRole")
        if not isinstance(self.escalation, Escalation):
            raise EscalationError("every question requires all five escalation parts")
        object.__setattr__(self, "scope", _optional_text(self.scope, "scope"))
        object.__setattr__(self, "run_id", _optional_text(self.run_id, "run_id"))
        object.__setattr__(self, "blocks", _text_tuple(self.blocks, "blocks"))
        object.__setattr__(self, "options", _text_tuple(self.options, "options"))
        default = _optional_text(self.default_option, "default_option")
        if default is not None:
            if default not in self.options:
                raise QuestionStoreError("default_option must be one of the options")
        object.__setattr__(self, "default_option", default)
        object.__setattr__(self, "supersedes", _optional_text(self.supersedes, "supersedes"))

    @property
    def question_id(self) -> str:
        """The deterministic id derived from the key."""
        return question_id_for_key(self.key)

    @property
    def answerable_by(self) -> AnswerAuthority:
        """Who is allowed to answer: derived, never caller-chosen."""
        return resolve_answer_authority(self.decision_class)

    def to_mapping(self) -> dict[str, Any]:
        """Render the ask payload (verbatim text) for comparison or storage."""
        return {
            "key": self.key,
            "subject": self.subject,
            "kind": self.kind,
            "decision_class": self.decision_class.value,
            "raised_by_role": self.raised_by_role.value,
            "raised_by": self.raised_by,
            "scope": self.scope,
            "run_id": self.run_id,
            "blocks": list(self.blocks),
            "options": list(self.options),
            "default_option": self.default_option,
            "supersedes": self.supersedes,
            "escalation": self.escalation.to_mapping(),
        }


@dataclass(frozen=True)
class OwnerAnswer:
    """One record of answer words, stored exactly as given."""

    question_id: str
    words: str
    authority: AnswerAuthority
    answered_by: str
    answered_by_role: AskRole
    answered_at: str
    chosen_option: str | None = None
    evidence: str | None = None
    rejected_alternative: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "question_id", _require_text(self.question_id, "question_id"))
        object.__setattr__(self, "words", _require_verbatim(self.words, "words"))
        object.__setattr__(self, "answered_by", _require_text(self.answered_by, "answered_by"))
        object.__setattr__(self, "answered_at", _require_text(self.answered_at, "answered_at"))
        if not isinstance(self.authority, AnswerAuthority):
            raise QuestionStoreError("authority must be an AnswerAuthority")
        if not isinstance(self.answered_by_role, AskRole):
            raise QuestionStoreError("answered_by_role must be an AskRole")
        expected_role = (
            AskRole.OWNER
            if self.authority is AnswerAuthority.OWNER
            else AskRole.ORCHESTRATOR
        )
        if self.answered_by_role is not expected_role:
            raise AnswerAuthorityError(
                f"{self.authority.value} authority requires the "
                f"{expected_role.value} role; implementers never answer"
            )
        object.__setattr__(
            self, "chosen_option", _optional_text(self.chosen_option, "chosen_option")
        )
        object.__setattr__(self, "evidence", _optional_text(self.evidence, "evidence"))
        object.__setattr__(
            self,
            "rejected_alternative",
            _optional_text(self.rejected_alternative, "rejected_alternative"),
        )
        if self.authority is AnswerAuthority.ORCHESTRATOR:
            missing = [
                name
                for name, value in (
                    ("evidence", self.evidence),
                    ("rejected_alternative", self.rejected_alternative),
                )
                if value is None
            ]
            if missing:
                raise AnswerAuthorityError(
                    "an Orchestrator-alone decision requires "
                    + " and ".join(missing)
                    + " (RD-1: logged with evidence and the rejected alternative)"
                )

    def to_mapping(self) -> dict[str, Any]:
        """Render the answer verbatim."""
        return {
            "question_id": self.question_id,
            "words": self.words,
            "authority": self.authority.value,
            "answered_by": self.answered_by,
            "answered_by_role": self.answered_by_role.value,
            "answered_at": self.answered_at,
            "chosen_option": self.chosen_option,
            "evidence": self.evidence,
            "rejected_alternative": self.rejected_alternative,
        }

    @classmethod
    def from_mapping(cls, payload: object) -> OwnerAnswer:
        """Rebuild one answer from its durable mapping, fail-closed."""
        data = _mapping(payload, "answer")
        return cls(
            question_id=_require_text(data.get("question_id"), "answer.question_id"),
            words=_require_verbatim(data.get("words"), "answer.words"),
            authority=_authority(data.get("authority"), "answer.authority"),
            answered_by=_require_text(data.get("answered_by"), "answer.answered_by"),
            answered_by_role=_ask_role(
                data.get("answered_by_role"), "answer.answered_by_role"
            ),
            answered_at=_require_text(data.get("answered_at"), "answer.answered_at"),
            chosen_option=_optional_text(data.get("chosen_option"), "answer.chosen_option"),
            evidence=_optional_text(data.get("evidence"), "answer.evidence"),
            rejected_alternative=_optional_text(
                data.get("rejected_alternative"), "answer.rejected_alternative"
            ),
        )


@dataclass(frozen=True)
class DivergentRecord:
    """A later record that conflicts with a question's current outcome."""

    question_id: str
    reason: DivergenceReason
    words: str
    recorded_by: str
    recorded_by_role: AskRole
    recorded_at: str
    chosen_option: str | None = None
    note: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "question_id", _require_text(self.question_id, "question_id"))
        object.__setattr__(self, "words", _require_verbatim(self.words, "words"))
        object.__setattr__(self, "recorded_by", _require_text(self.recorded_by, "recorded_by"))
        object.__setattr__(self, "recorded_at", _require_text(self.recorded_at, "recorded_at"))
        if not isinstance(self.reason, DivergenceReason):
            raise QuestionStoreError("reason must be a DivergenceReason")
        if not isinstance(self.recorded_by_role, AskRole):
            raise QuestionStoreError("recorded_by_role must be an AskRole")
        object.__setattr__(
            self, "chosen_option", _optional_text(self.chosen_option, "chosen_option")
        )
        object.__setattr__(self, "note", str(self.note))

    def to_mapping(self) -> dict[str, Any]:
        """Render the divergent record verbatim."""
        return {
            "question_id": self.question_id,
            "reason": self.reason.value,
            "words": self.words,
            "recorded_by": self.recorded_by,
            "recorded_by_role": self.recorded_by_role.value,
            "recorded_at": self.recorded_at,
            "chosen_option": self.chosen_option,
            "note": self.note,
        }

    @classmethod
    def from_mapping(cls, payload: object) -> DivergentRecord:
        """Rebuild one divergent record from its durable mapping."""
        data = _mapping(payload, "divergence")
        return cls(
            question_id=_require_text(data.get("question_id"), "divergence.question_id"),
            reason=_divergence_reason(data.get("reason"), "divergence.reason"),
            words=_require_verbatim(data.get("words"), "divergence.words"),
            recorded_by=_require_text(data.get("recorded_by"), "divergence.recorded_by"),
            recorded_by_role=_ask_role(
                data.get("recorded_by_role"), "divergence.recorded_by_role"
            ),
            recorded_at=_require_text(data.get("recorded_at"), "divergence.recorded_at"),
            chosen_option=_optional_text(
                data.get("chosen_option"), "divergence.chosen_option"
            ),
            note=str(data.get("note") or ""),
        )


@dataclass(frozen=True)
class QuestionEvent:
    """One entry in a question's append-only audit trail."""

    at: str
    verb: str
    actor: str
    role: AskRole
    detail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "at", _require_text(self.at, "event.at"))
        object.__setattr__(self, "verb", _require_text(self.verb, "event.verb"))
        object.__setattr__(self, "actor", _require_text(self.actor, "event.actor"))
        if self.verb not in QUESTION_EVENT_VERBS:
            raise QuestionStoreError(f"unknown question verb: {self.verb!r}")
        if not isinstance(self.role, AskRole):
            raise QuestionStoreError("event.role must be an AskRole")
        object.__setattr__(self, "detail", str(self.detail))

    def to_mapping(self) -> dict[str, Any]:
        """Render one audit event."""
        return {
            "at": self.at,
            "verb": self.verb,
            "actor": self.actor,
            "role": self.role.value,
            "detail": self.detail,
        }

    @classmethod
    def from_mapping(cls, payload: object) -> QuestionEvent:
        """Rebuild one audit event from its durable mapping."""
        data = _mapping(payload, "history")
        return cls(
            at=_require_text(data.get("at"), "history.at"),
            verb=_require_text(data.get("verb"), "history.verb"),
            actor=_require_text(data.get("actor"), "history.actor"),
            role=_ask_role(data.get("role"), "history.role"),
            detail=str(data.get("detail") or ""),
        )


@dataclass(frozen=True)
class OwnerQuestion:
    """One keyed owner question and its durable outcome."""

    request: QuestionRequest
    raised_at: str
    status: QuestionStatus = QuestionStatus.OPEN
    defer_until: str | None = None
    answer: OwnerAnswer | None = None
    moot_evidence: str | None = None
    divergent_records: tuple[DivergentRecord, ...] = ()
    history: tuple[QuestionEvent, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.request, QuestionRequest):
            raise QuestionStoreError("question.request must be a QuestionRequest")
        object.__setattr__(self, "raised_at", _require_text(self.raised_at, "raised_at"))
        if not isinstance(self.status, QuestionStatus):
            raise QuestionStoreError("status must be a QuestionStatus")
        object.__setattr__(
            self, "defer_until", _optional_text(self.defer_until, "defer_until")
        )
        object.__setattr__(
            self, "moot_evidence", _optional_text(self.moot_evidence, "moot_evidence")
        )
        if self.defer_until is not None:
            object.__setattr__(
                self, "defer_until", _iso_date(self.defer_until, "defer_until")
            )
        if not isinstance(self.divergent_records, tuple):
            object.__setattr__(
                self, "divergent_records", tuple(self.divergent_records)
            )
        if not isinstance(self.history, tuple):
            object.__setattr__(self, "history", tuple(self.history))
        for record in self.divergent_records:
            if not isinstance(record, DivergentRecord):
                raise QuestionStoreError("divergent_records must hold DivergentRecord")
            if record.question_id != self.question_id:
                raise QuestionStoreError(
                    "divergent record belongs to a different question"
                )
        for event in self.history:
            if not isinstance(event, QuestionEvent):
                raise QuestionStoreError("history must hold QuestionEvent entries")
        if self.status is QuestionStatus.ANSWERED:
            if self.answer is None:
                raise QuestionStoreError("an ANSWERED question requires an answer")
        elif self.answer is not None:
            raise QuestionStoreError(
                f"a {self.status.value} question may not carry a primary answer"
            )
        if self.status is QuestionStatus.MOOT and self.moot_evidence is None:
            raise QuestionStoreError("a MOOT question requires the closing evidence")
        if self.status is not QuestionStatus.MOOT and self.moot_evidence is not None:
            raise QuestionStoreError(
                f"a {self.status.value} question may not carry moot evidence"
            )
        if self.status is QuestionStatus.DEFERRED and self.defer_until is None:
            raise QuestionStoreError("a DEFERRED question requires a defer-until date")
        if self.status is QuestionStatus.OPEN and self.defer_until is not None:
            raise QuestionStoreError("an OPEN question may not carry a defer-until date")
        if self.answer is not None:
            if self.answer.question_id != self.question_id:
                raise QuestionStoreError("answer belongs to a different question")
            if (
                self.answer.authority is AnswerAuthority.ORCHESTRATOR
                and self.request.answerable_by is not AnswerAuthority.ORCHESTRATOR
            ):
                raise AnswerAuthorityError(
                    "an Orchestrator answer on an owner-only decision class is refused"
                )

    @property
    def question_id(self) -> str:
        """The deterministic id derived from the key."""
        return self.request.question_id

    @property
    def key(self) -> str:
        """The caller-supplied idempotency key."""
        return self.request.key

    @property
    def kind(self) -> str:
        """The caller-supplied question kind (grouping key for standing rules)."""
        return self.request.kind

    @property
    def answerable_by(self) -> AnswerAuthority:
        """Who may answer this question."""
        return self.request.answerable_by

    @property
    def is_closed(self) -> bool:
        """True for the two evidence-bearing terminal outcomes."""
        return self.status in (QuestionStatus.ANSWERED, QuestionStatus.MOOT)

    @property
    def closed_by(self) -> str | None:
        """The explicit verb that produced the current status, or None."""
        if self.status is QuestionStatus.ANSWERED:
            return "answer"
        if self.status is QuestionStatus.MOOT:
            return "mark_moot"
        if self.status is QuestionStatus.DEFERRED:
            return "defer"
        return None

    def to_mapping(self) -> dict[str, Any]:
        """Render the whole question, verbatim text preserved."""
        return {
            "question_id": self.question_id,
            "status": self.status.value,
            "raised_at": self.raised_at,
            "answerable_by": self.answerable_by.value,
            "defer_until": self.defer_until,
            "answer": None if self.answer is None else self.answer.to_mapping(),
            "moot_evidence": self.moot_evidence,
            "divergent_records": [r.to_mapping() for r in self.divergent_records],
            "history": [e.to_mapping() for e in self.history],
            **self.request.to_mapping(),
        }

    @classmethod
    def from_mapping(cls, payload: object) -> OwnerQuestion:
        """Rebuild one question from its durable mapping, fail-closed."""
        data = _mapping(payload, "question")
        request = QuestionRequest(
            key=_require_text(data.get("key"), "question.key"),
            subject=_require_text(data.get("subject"), "question.subject"),
            kind=_require_text(data.get("kind"), "question.kind"),
            decision_class=_decision_class(
                data.get("decision_class"), "question.decision_class"
            ),
            raised_by_role=_ask_role(
                data.get("raised_by_role"), "question.raised_by_role"
            ),
            raised_by=_require_text(data.get("raised_by"), "question.raised_by"),
            escalation=Escalation.from_mapping(data.get("escalation")),
            scope=data.get("scope"),
            run_id=data.get("run_id"),
            blocks=_text_tuple(data.get("blocks"), "question.blocks"),
            options=_text_tuple(data.get("options"), "question.options"),
            default_option=data.get("default_option"),
            supersedes=data.get("supersedes"),
        )
        answer_raw = data.get("answer")
        answer = None if answer_raw is None else OwnerAnswer.from_mapping(answer_raw)
        return cls(
            request=request,
            raised_at=_require_text(data.get("raised_at"), "question.raised_at"),
            status=_status(data.get("status"), "question.status"),
            defer_until=data.get("defer_until"),
            answer=answer,
            moot_evidence=data.get("moot_evidence"),
            divergent_records=tuple(
                DivergentRecord.from_mapping(item)
                for item in _sequence(data.get("divergent_records"), "divergent_records")
            ),
            history=tuple(
                QuestionEvent.from_mapping(item)
                for item in _sequence(data.get("history"), "history")
            ),
        )


@dataclass(frozen=True)
class QuestionLedger:
    """The durable set of questions, keyed by deterministic id."""

    questions: tuple[OwnerQuestion, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.questions, tuple):
            object.__setattr__(self, "questions", tuple(self.questions))
        ids = [question.question_id for question in self.questions]
        if len(set(ids)) != len(ids):
            raise QuestionStoreError("the ledger holds a duplicate question id")
        keys = [question.key for question in self.questions]
        if len(set(keys)) != len(keys):
            raise QuestionStoreError("the ledger holds a duplicate question key")

    def get(self, ref: str) -> OwnerQuestion | None:
        """Resolve a question by exact key first, then by id."""
        cleaned = str(ref).strip()
        if not cleaned:
            return None
        for question in self.questions:
            if question.key == cleaned:
                return question
        for question in self.questions:
            if question.question_id == cleaned:
                return question
        return None

    def with_question(self, question: OwnerQuestion) -> QuestionLedger:
        """Return a new ledger with ``question`` replaced or appended."""
        kept = [item for item in self.questions if item.question_id != question.question_id]
        return QuestionLedger(questions=(*kept, question))

    def to_mapping(self) -> dict[str, Any]:
        """Render the whole document."""
        return {
            "schema_version": QUESTIONS_STORE_SCHEMA_VERSION,
            "questions": [question.to_mapping() for question in self.questions],
        }

    @classmethod
    def from_mapping(cls, payload: object) -> QuestionLedger:
        """Rebuild the document, refusing an unknown schema or malformed row."""
        data = _mapping(payload, "questions document")
        version = data.get("schema_version", QUESTIONS_STORE_SCHEMA_VERSION)
        if version != QUESTIONS_STORE_SCHEMA_VERSION:
            raise QuestionStoreError(
                f"unsupported questions schema_version: {version!r}"
            )
        return cls(
            questions=tuple(
                OwnerQuestion.from_mapping(item)
                for item in _sequence(data.get("questions"), "questions")
            )
        )


@dataclass(frozen=True)
class DivergenceReport:
    """One divergent record with the question context that proves no auto-close."""

    question_id: str
    key: str
    kind: str
    status: QuestionStatus
    record: DivergentRecord

    def to_mapping(self) -> dict[str, Any]:
        """Render the report for CLI or web consumption."""
        return {
            "question_id": self.question_id,
            "key": self.key,
            "kind": self.kind,
            "status": self.status.value,
            "record": self.record.to_mapping(),
        }


@dataclass(frozen=True)
class OrchestratorAloneDecision:
    """One decision the Orchestrator took without the owner."""

    question_id: str
    key: str
    kind: str
    decision_class: DecisionClass
    scope: str | None
    requirement: str
    recommendation: str
    words: str
    evidence: str
    rejected_alternative: str
    answered_by: str
    answered_at: str

    def to_mapping(self) -> dict[str, Any]:
        """Render the audit entry."""
        return {
            "question_id": self.question_id,
            "key": self.key,
            "kind": self.kind,
            "decision_class": self.decision_class.value,
            "scope": self.scope,
            "requirement": self.requirement,
            "recommendation": self.recommendation,
            "words": self.words,
            "evidence": self.evidence,
            "rejected_alternative": self.rejected_alternative,
            "answered_by": self.answered_by,
            "answered_at": self.answered_at,
        }


@dataclass(frozen=True)
class StandingRuleProposal:
    """A repeated same-kind owner answer, proposed as a standing rule to CFG-1."""

    kind: str
    chosen_answer: str
    occurrences: int
    question_ids: tuple[str, ...]
    owner_words: tuple[str, ...]
    proposed_at: str
    target: str = CFG_1_TARGET

    def to_mapping(self) -> dict[str, Any]:
        """Render the proposal (inert data; nothing is applied here)."""
        return {
            "target": self.target,
            "kind": self.kind,
            "chosen_answer": self.chosen_answer,
            "occurrences": self.occurrences,
            "question_ids": list(self.question_ids),
            "owner_words": list(self.owner_words),
            "proposed_at": self.proposed_at,
        }


def question_request(question: OwnerQuestion) -> QuestionRequest:
    """Return the immutable ask payload of ``question``."""
    return question.request


def is_due(question: OwnerQuestion, as_of: date) -> bool:
    """Return True when a deferred question has reached its resurface date."""
    if question.status is not QuestionStatus.DEFERRED or question.defer_until is None:
        return False
    return date.fromisoformat(question.defer_until) <= as_of


def question_view(question: OwnerQuestion, as_of: date | None = None) -> dict[str, Any]:
    """Render one question for the CLI or the web API."""
    due = as_of is not None and is_due(question, as_of)
    return {
        "question_id": question.question_id,
        "key": question.key,
        "kind": question.kind,
        "subject": question.request.subject,
        "decision_class": question.request.decision_class.value,
        "answerable_by": question.answerable_by.value,
        "raised_by_role": question.request.raised_by_role.value,
        "raised_by": question.request.raised_by,
        "raised_at": question.raised_at,
        "scope": question.request.scope,
        "run_id": question.request.run_id,
        "blocks": list(question.request.blocks),
        "options": list(question.request.options),
        "default_option": question.request.default_option,
        "supersedes": question.request.supersedes,
        "status": question.status.value,
        "closed_by": question.closed_by,
        "defer_until": question.defer_until,
        "due": due,
        "resurfaced": due,
        "escalation": question.request.escalation.to_mapping(),
        "answer": None if question.answer is None else question.answer.to_mapping(),
        "moot_evidence": question.moot_evidence,
        "divergence_count": len(question.divergent_records),
        "history": [event.to_mapping() for event in question.history],
    }


def get_questions_store_path() -> Path:
    """Resolve the canonical owner question document path.

    ``ORCH_QUESTIONS_STORE`` pins an explicit file (tests / diagnostics).
    Otherwise the document lives in the owner state directory beside the other
    owner decision evidence.
    """
    override = os.environ.get(QUESTIONS_STORE_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    from saberops.paths import saberops_state_dir

    return saberops_state_dir() / "questions.json"


class OwnerQuestionQueue:
    """Durable owner question queue: one JSON document, one explicit verb set."""

    FILENAME = "questions.json"

    def __init__(self, path: Path | str | None = None) -> None:
        self._path = Path(path) if path is not None else get_questions_store_path()

    @property
    def path(self) -> Path:
        """The document this queue reads and writes."""
        return self._path

    # -- persistence ------------------------------------------------------

    def load(self) -> QuestionLedger:
        """Load the ledger (empty when none exists); fail closed when unreadable."""
        if not self._path.exists():
            return QuestionLedger()
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise QuestionStoreError(f"questions store unreadable: {exc}") from exc
        return QuestionLedger.from_mapping(payload)

    def save(self, ledger: QuestionLedger) -> Path:
        """Persist the ledger atomically; fail closed on write errors."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(ledger.to_mapping(), indent=2, sort_keys=True),
                encoding="utf-8",
            )
            tmp.replace(self._path)
        except OSError as exc:
            raise QuestionStoreError(f"questions store unwritable: {exc}") from exc
        return self._path

    # -- verbs ------------------------------------------------------------

    def ask(
        self, request: QuestionRequest, *, now: str | None = None
    ) -> OwnerQuestion:
        """Create (or idempotently return) one keyed question.

        The same key with the same payload returns the existing question
        unchanged.  The same key with a *different* payload fails closed: a
        conflicting re-ask is never silently merged into the original.
        """
        ledger = self.load()
        existing = ledger.get(request.key)
        if existing is not None:
            if question_request(existing) != request:
                raise QuestionError(
                    f"key {request.key!r} already holds a different question "
                    f"({existing.question_id}); ask a new key or supersede it"
                )
            return existing
        if request.supersedes is not None and ledger.get(request.supersedes) is None:
            raise QuestionError(
                f"supersedes references an unknown question: {request.supersedes!r}"
            )
        raised_at = _require_text(now if now is not None else _now_iso(), "raised_at")
        question = OwnerQuestion(
            request=request,
            raised_at=raised_at,
            status=QuestionStatus.OPEN,
            history=(
                QuestionEvent(
                    at=raised_at,
                    verb=ASK_EVENT_VERB,
                    actor=request.raised_by,
                    role=request.raised_by_role,
                    detail=f"asked {request.kind}",
                ),
            ),
        )
        self.save(ledger.with_question(question))
        return question

    def answer(
        self,
        ref: str,
        *,
        words: str,
        authority: AnswerAuthority,
        answered_by: str,
        answered_by_role: AskRole,
        chosen_option: str | None = None,
        evidence: str | None = None,
        rejected_alternative: str | None = None,
        now: str | None = None,
    ) -> OwnerQuestion:
        """Record an answer, storing the words exactly as given.

        A conflicting later answer is recorded as a divergent record and the
        question's own outcome is left untouched: divergence is reported, never
        auto-closed.
        """
        question = self._require(ref)
        self._authorize_answer(question, authority=authority, answered_by=answered_by)
        answer = OwnerAnswer(
            question_id=question.question_id,
            words=words,
            authority=authority,
            answered_by=answered_by,
            answered_by_role=answered_by_role,
            answered_at=_require_text(now if now is not None else _now_iso(), "answered_at"),
            chosen_option=chosen_option,
            evidence=evidence,
            rejected_alternative=rejected_alternative,
        )
        if answer.chosen_option is not None and (
            answer.chosen_option not in question.request.options
        ):
            raise QuestionError(
                f"chosen_option {answer.chosen_option!r} is not one of the question's options"
            )
        if question.status in (QuestionStatus.ANSWERED, QuestionStatus.MOOT):
            return self._record_conflict(question, answer)
        updated = replace(
            question,
            status=QuestionStatus.ANSWERED,
            answer=answer,
            defer_until=None,
            history=(
                *question.history,
                QuestionEvent(
                    at=answer.answered_at,
                    verb="answer",
                    actor=answered_by,
                    role=answered_by_role,
                    detail=f"{authority.value.lower()} answer",
                ),
            ),
        )
        return self._persist(updated)

    def defer(
        self,
        ref: str,
        *,
        until: str | date,
        deferred_by: str,
        deferred_by_role: AskRole,
        note: str = "",
        now: str | None = None,
    ) -> OwnerQuestion:
        """Defer a question until a date; it resurfaces on that date."""
        question = self._require(ref)
        if deferred_by_role not in (AskRole.OWNER, AskRole.ORCHESTRATOR):
            raise AnswerAuthorityError(
                "only the owner or the Orchestrator may defer a question"
            )
        if question.status in (QuestionStatus.ANSWERED, QuestionStatus.MOOT):
            raise QuestionStateError(
                f"cannot defer a {question.status.value} question; "
                "raise a new keyed question that supersedes it"
            )
        when = _iso_date(until, "until")
        at = _require_text(now if now is not None else _now_iso(), "deferred_at")
        updated = replace(
            question,
            status=QuestionStatus.DEFERRED,
            defer_until=when,
            history=(
                *question.history,
                QuestionEvent(
                    at=at,
                    verb="defer",
                    actor=deferred_by,
                    role=deferred_by_role,
                    detail=f"deferred until {when}" + (f": {note}" if note else ""),
                ),
            ),
        )
        return self._persist(updated)

    def mark_moot(
        self,
        ref: str,
        *,
        evidence: str,
        closed_by: str,
        closed_by_role: AskRole,
        now: str | None = None,
    ) -> OwnerQuestion:
        """Close a question as moot on explicit evidence.

        Evidence is mandatory: a moot close without evidence is refused, so a
        question can never disappear merely because a task ended or storage was
        cleaned up.
        """
        question = self._require(ref)
        if closed_by_role not in (AskRole.OWNER, AskRole.ORCHESTRATOR, AskRole.ENGINE):
            raise AnswerAuthorityError(
                "only the owner, the Orchestrator or the deterministic engine "
                "may close a question as moot"
            )
        closing_evidence = _require_text(evidence, "evidence")
        at = _require_text(now if now is not None else _now_iso(), "mooted_at")
        if question.status is QuestionStatus.MOOT:
            if question.moot_evidence == closing_evidence:
                return question
            return self.record_divergence(
                question.key,
                reason=DivergenceReason.DIFFERENT_EVIDENCE,
                words=closing_evidence,
                recorded_by=closed_by,
                recorded_by_role=closed_by_role,
                note="a second moot close with different evidence",
                now=at,
            )
        if question.status is QuestionStatus.ANSWERED:
            return self.record_divergence(
                question.key,
                reason=DivergenceReason.MOOT_AFTER_ANSWER,
                words=closing_evidence,
                recorded_by=closed_by,
                recorded_by_role=closed_by_role,
                note="a moot close after an owner answer; not auto-closed",
                now=at,
            )
        updated = replace(
            question,
            status=QuestionStatus.MOOT,
            moot_evidence=closing_evidence,
            defer_until=None,
            history=(
                *question.history,
                QuestionEvent(
                    at=at,
                    verb="mark_moot",
                    actor=closed_by,
                    role=closed_by_role,
                    detail="closed as moot on evidence",
                ),
            ),
        )
        return self._persist(updated)

    def record_divergence(
        self,
        ref: str,
        *,
        reason: DivergenceReason,
        words: str,
        recorded_by: str,
        recorded_by_role: AskRole,
        chosen_option: str | None = None,
        note: str = "",
        now: str | None = None,
    ) -> OwnerQuestion:
        """Record a conflicting later record without changing the outcome."""
        question = self._require(ref)
        at = _require_text(now if now is not None else _now_iso(), "recorded_at")
        record = DivergentRecord(
            question_id=question.question_id,
            reason=reason,
            words=words,
            recorded_by=recorded_by,
            recorded_by_role=recorded_by_role,
            recorded_at=at,
            chosen_option=chosen_option,
            note=note,
        )
        updated = replace(
            question,
            divergent_records=(*question.divergent_records, record),
            history=(
                *question.history,
                QuestionEvent(
                    at=at,
                    verb="record_divergence",
                    actor=recorded_by,
                    role=recorded_by_role,
                    detail=reason.value,
                ),
            ),
        )
        return self._persist(updated)

    # -- reads ------------------------------------------------------------

    def get(self, ref: str) -> OwnerQuestion | None:
        """Return one question by key or id, or None."""
        return self.load().get(ref)

    def questions(
        self,
        *,
        status: QuestionStatus | str | None = None,
        run_id: str | None = None,
    ) -> tuple[OwnerQuestion, ...]:
        """List questions in stable creation order, optionally filtered."""
        ledger = self.load()
        wanted: QuestionStatus | None = None
        if status is not None:
            wanted = status if isinstance(status, QuestionStatus) else _status(status, "status")
        selected = [
            question
            for question in ledger.questions
            if (wanted is None or question.status is wanted)
            and (run_id is None or question.request.run_id == run_id)
        ]
        return tuple(sorted(selected, key=lambda item: (item.raised_at, item.question_id)))

    def pending(self, as_of: date) -> tuple[OwnerQuestion, ...]:
        """Return open questions plus deferred questions that are due again."""
        return tuple(
            question
            for question in self.questions()
            if question.status is QuestionStatus.OPEN or is_due(question, as_of)
        )

    def questions_for_run(self, run_id: str) -> tuple[OwnerQuestion, ...]:
        """Return every question raised against one run."""
        return self.questions(run_id=_require_text(run_id, "run_id"))

    def divergences(self, ref: str | None = None) -> tuple[DivergenceReport, ...]:
        """Report divergent records (optionally for one question)."""
        ledger = self.load()
        selected = ledger.questions
        if ref is not None:
            question = self._require(ref)
            selected = (question,)
        reports = [
            DivergenceReport(
                question_id=question.question_id,
                key=question.key,
                kind=question.kind,
                status=question.status,
                record=record,
            )
            for question in selected
            for record in question.divergent_records
        ]
        return tuple(
            sorted(reports, key=lambda item: (item.record.recorded_at, item.question_id))
        )

    def orchestrator_alone_decisions(self) -> tuple[OrchestratorAloneDecision, ...]:
        """Return the audit list of every Orchestrator-alone decision."""
        decisions = [
            OrchestratorAloneDecision(
                question_id=question.question_id,
                key=question.key,
                kind=question.kind,
                decision_class=question.request.decision_class,
                scope=question.request.scope,
                requirement=question.request.escalation.requirement,
                recommendation=question.request.escalation.recommendation,
                words=question.answer.words,
                evidence=question.answer.evidence or "",
                rejected_alternative=question.answer.rejected_alternative or "",
                answered_by=question.answer.answered_by,
                answered_at=question.answer.answered_at,
            )
            for question in self.load().questions
            if question.answer is not None
            and question.answer.authority is AnswerAuthority.ORCHESTRATOR
        ]
        return tuple(sorted(decisions, key=lambda item: (item.answered_at, item.question_id)))

    def standing_rule_proposals(
        self, threshold: int = STANDING_RULE_THRESHOLD
    ) -> tuple[StandingRuleProposal, ...]:
        """Propose a standing rule for CFG-1 when the owner answered the same
        way, for the same question kind, at least ``threshold`` times.

        Proposals are inert data.  This module writes no configuration.
        """
        if not isinstance(threshold, int) or isinstance(threshold, bool) or threshold < 2:
            raise QuestionError("standing-rule threshold must be an integer >= 2")
        groups: dict[tuple[str, str], list[OwnerQuestion]] = {}
        for question in self.load().questions:
            answer = question.answer
            if answer is None or answer.authority is not AnswerAuthority.OWNER:
                continue
            chosen = answer.chosen_option if answer.chosen_option is not None else answer.words
            groups.setdefault((question.kind, chosen), []).append(question)
        proposals: list[StandingRuleProposal] = []
        for (kind, chosen), members in groups.items():
            if len(members) < threshold:
                continue
            ordered = sorted(members, key=lambda item: (item.raised_at, item.question_id))
            answers = [item.answer for item in ordered]
            proposals.append(
                StandingRuleProposal(
                    kind=kind,
                    chosen_answer=chosen,
                    occurrences=len(ordered),
                    question_ids=tuple(item.question_id for item in ordered),
                    owner_words=tuple(
                        answer.words for answer in answers if answer is not None
                    ),
                    proposed_at=max(
                        answer.answered_at for answer in answers if answer is not None
                    ),
                )
            )
        return tuple(sorted(proposals, key=lambda item: (item.kind, item.chosen_answer)))

    # -- internals --------------------------------------------------------

    def _require(self, ref: str) -> OwnerQuestion:
        question = self.load().get(ref)
        if question is None:
            raise QuestionStoreError(f"unknown question: {ref!r}")
        return question

    def _persist(self, question: OwnerQuestion) -> OwnerQuestion:
        self.save(self.load().with_question(question))
        return question

    def _authorize_answer(
        self,
        question: OwnerQuestion,
        *,
        authority: AnswerAuthority,
        answered_by: str,
    ) -> None:
        if authority is AnswerAuthority.OWNER:
            return
        if question.answerable_by is not AnswerAuthority.ORCHESTRATOR:
            raise AnswerAuthorityError(
                f"decision class {question.request.decision_class.value} is owner-only; "
                "only the owner may answer this question"
            )
        if (
            question.request.raised_by_role is AskRole.ORCHESTRATOR
            or answered_by == question.request.raised_by
        ):
            raise AnswerAuthorityError(
                "an Orchestrator may never answer its own finding"
            )

    def _record_conflict(
        self, question: OwnerQuestion, answer: OwnerAnswer
    ) -> OwnerQuestion:
        """Record a conflicting answer; never overwrite, never auto-close."""
        if question.status is QuestionStatus.MOOT:
            return self.record_divergence(
                question.key,
                reason=DivergenceReason.ANSWER_AFTER_MOOT,
                words=answer.words,
                recorded_by=answer.answered_by,
                recorded_by_role=answer.answered_by_role,
                chosen_option=answer.chosen_option,
                note="an answer after an evidence-backed moot close",
                now=answer.answered_at,
            )
        current = question.answer
        if current is None:  # pragma: no cover - ANSWERED always carries an answer
            raise QuestionStateError("ANSWERED question carries no primary answer")
        if current.words == answer.words and current.chosen_option == answer.chosen_option:
            confirmed = replace(
                question,
                history=(
                    *question.history,
                    QuestionEvent(
                        at=answer.answered_at,
                        verb="answer",
                        actor=answer.answered_by,
                        role=answer.answered_by_role,
                        detail="repeated identical answer (confirmed)",
                    ),
                ),
            )
            return self._persist(confirmed)
        reason = (
            DivergenceReason.DIFFERENT_OPTION
            if current.chosen_option != answer.chosen_option
            else DivergenceReason.DIFFERENT_WORDS
        )
        return self.record_divergence(
            question.key,
            reason=reason,
            words=answer.words,
            recorded_by=answer.answered_by,
            recorded_by_role=answer.answered_by_role,
            chosen_option=answer.chosen_option,
            note="a later conflicting answer; the earlier answer stands",
            now=answer.answered_at,
        )


def _payload(raw: object, label: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise QuestionError(f"{label} must be a JSON object")
    return {str(key): value for key, value in raw.items()}


def _escalation_from_payload(payload: Mapping[str, Any]) -> Escalation:
    escalation_raw = payload.get("escalation")
    if escalation_raw is None:
        escalation_raw = {
            part: payload[f"escalation_{part}"]
            for part in ESCALATION_PARTS
            if f"escalation_{part}" in payload
        }
    return Escalation.from_mapping(escalation_raw)


def request_from_payload(payload: Mapping[str, Any]) -> QuestionRequest:
    """Build one :class:`QuestionRequest` from a flat JSON/CLI payload."""
    return QuestionRequest(
        key=_require_text(payload.get("key"), "key"),
        subject=_require_text(payload.get("subject"), "subject"),
        kind=_require_text(payload.get("kind"), "kind"),
        decision_class=_decision_class(payload.get("decision_class"), "decision_class"),
        raised_by_role=_ask_role(payload.get("raised_by_role"), "raised_by_role"),
        raised_by=_require_text(payload.get("raised_by"), "raised_by"),
        escalation=_escalation_from_payload(payload),
        scope=payload.get("scope"),
        run_id=payload.get("run_id"),
        blocks=_text_tuple(payload.get("blocks"), "blocks"),
        options=_text_tuple(payload.get("options"), "options"),
        default_option=payload.get("default_option"),
        supersedes=payload.get("supersedes"),
    )


def _router_client_allowed(request: Request) -> bool:
    """Return True only for a loopback web client (local-only surface)."""
    client = request.client
    if client is None:
        return False
    host = (client.host or "").strip()
    return host in {"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"}


def build_questions_router(
    store_path: Path | str | None = None,
) -> APIRouter:
    """Return the local web entry point for the owner question queue.

    Every route calls exactly one :class:`OwnerQuestionQueue` verb; the router
    adds no authority of its own.  The mounting application must keep its
    existing local-only origin guard; this router additionally refuses any
    non-loopback client.
    """
    router = APIRouter(prefix=QUESTIONS_ROUTER_PREFIX, tags=["questions"])
    queue = OwnerQuestionQueue(store_path)

    def _guard(request: Request) -> None:
        if not _router_client_allowed(request):
            raise HTTPException(status_code=403, detail="the question queue is local-only")

    async def _body(request: Request) -> dict[str, Any]:
        try:
            raw: Any = await request.json()
        except Exception as exc:  # noqa: BLE001 - any parse failure is a 400
            raise HTTPException(status_code=400, detail="body must be valid JSON") from exc
        return _payload(raw, "body")

    def _ref(payload: Mapping[str, Any]) -> str:
        return _require_text(payload.get("ref"), "ref")

    def _question_response(question: OwnerQuestion, as_of: date | None) -> JSONResponse:
        return JSONResponse(question_view(question, as_of))

    @router.get("")
    async def list_questions(request: Request) -> JSONResponse:
        _guard(request)
        as_of_raw = request.query_params.get("as_of")
        as_of = date.fromisoformat(_iso_date(as_of_raw, "as_of")) if as_of_raw else None
        status = request.query_params.get("status")
        run_id = request.query_params.get("run_id")
        try:
            questions = queue.questions(status=status, run_id=run_id or None)
            divergences = queue.divergences()
            audit = queue.orchestrator_alone_decisions()
        except QuestionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(
            {
                "questions": [question_view(item, as_of) for item in questions],
                "divergences": [item.to_mapping() for item in divergences],
                "orchestrator_alone": [item.to_mapping() for item in audit],
            }
        )

    @router.get("/pending")
    async def list_pending(request: Request) -> JSONResponse:
        _guard(request)
        as_of_raw = request.query_params.get("as_of")
        as_of = (
            date.fromisoformat(_iso_date(as_of_raw, "as_of"))
            if as_of_raw
            else datetime.now(UTC).date()
        )
        try:
            pending = queue.pending(as_of)
        except QuestionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(
            {
                "as_of": as_of.isoformat(),
                "questions": [question_view(item, as_of) for item in pending],
            }
        )

    @router.get("/divergences")
    async def list_divergences(request: Request) -> JSONResponse:
        _guard(request)
        ref = request.query_params.get("ref")
        try:
            reports = queue.divergences(ref)
        except QuestionError as exc:
            status = 404 if "unknown question" in str(exc) else 400
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        return JSONResponse({"divergences": [item.to_mapping() for item in reports]})

    @router.get("/standing-rules")
    async def list_standing_rules(request: Request) -> JSONResponse:
        _guard(request)
        raw_threshold = request.query_params.get("threshold")
        try:
            threshold = int(raw_threshold) if raw_threshold else STANDING_RULE_THRESHOLD
            proposals = queue.standing_rule_proposals(threshold)
        except (ValueError, QuestionError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(
            {
                "target": CFG_1_TARGET,
                "threshold": threshold,
                "proposals": [item.to_mapping() for item in proposals],
            }
        )

    @router.post("/ask")
    async def ask(request: Request) -> JSONResponse:
        _guard(request)
        payload = await _body(request)
        try:
            question = queue.ask(request_from_payload(payload))
        except QuestionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(question_view(question), status_code=201)

    @router.get("/show")
    async def show(request: Request) -> JSONResponse:
        _guard(request)
        ref = request.query_params.get("ref") or ""
        as_of_raw = request.query_params.get("as_of")
        as_of = date.fromisoformat(_iso_date(as_of_raw, "as_of")) if as_of_raw else None
        question = queue.get(ref)
        if question is None:
            raise HTTPException(status_code=404, detail=f"unknown question: {ref!r}")
        return _question_response(question, as_of)

    @router.post("/answer")
    async def answer(request: Request) -> JSONResponse:
        _guard(request)
        payload = await _body(request)
        try:
            question = queue.answer(
                _ref(payload),
                words=_require_verbatim(payload.get("words"), "words"),
                authority=_authority(payload.get("authority"), "authority"),
                answered_by=_require_text(payload.get("answered_by"), "answered_by"),
                answered_by_role=_ask_role(
                    payload.get("answered_by_role"), "answered_by_role"
                ),
                chosen_option=payload.get("chosen_option"),
                evidence=payload.get("evidence"),
                rejected_alternative=payload.get("rejected_alternative"),
            )
        except QuestionError as exc:
            status = 404 if "unknown question" in str(exc) else 400
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        return _question_response(question, None)

    @router.post("/defer")
    async def defer(request: Request) -> JSONResponse:
        _guard(request)
        payload = await _body(request)
        try:
            question = queue.defer(
                _ref(payload),
                until=_iso_date(payload.get("until"), "until"),
                deferred_by=_require_text(payload.get("deferred_by"), "deferred_by"),
                deferred_by_role=_ask_role(
                    payload.get("deferred_by_role"), "deferred_by_role"
                ),
                note=str(payload.get("note") or ""),
            )
        except QuestionError as exc:
            status = 404 if "unknown question" in str(exc) else 400
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        return _question_response(question, None)

    @router.post("/moot")
    async def moot(request: Request) -> JSONResponse:
        _guard(request)
        payload = await _body(request)
        try:
            question = queue.mark_moot(
                _ref(payload),
                evidence=_require_text(payload.get("evidence"), "evidence"),
                closed_by=_require_text(payload.get("closed_by"), "closed_by"),
                closed_by_role=_ask_role(
                    payload.get("closed_by_role"), "closed_by_role"
                ),
            )
        except QuestionError as exc:
            status = 404 if "unknown question" in str(exc) else 400
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        return _question_response(question, None)

    @router.post("/divergence")
    async def divergence(request: Request) -> JSONResponse:
        _guard(request)
        payload = await _body(request)
        try:
            question = queue.record_divergence(
                _ref(payload),
                reason=_divergence_reason(payload.get("reason"), "reason"),
                words=_require_verbatim(payload.get("words"), "words"),
                recorded_by=_require_text(payload.get("recorded_by"), "recorded_by"),
                recorded_by_role=_ask_role(
                    payload.get("recorded_by_role"), "recorded_by_role"
                ),
                chosen_option=payload.get("chosen_option"),
                note=str(payload.get("note") or ""),
            )
        except QuestionError as exc:
            status = 404 if "unknown question" in str(exc) else 400
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        return _question_response(question, None)

    return router
