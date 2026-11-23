"""Away mode: one module for the away policy, its durable records and its summary.

This module owns the whole away surface in one place:

* **The away policy**: the bounded, deterministic decisions an overnight run
  must make without the owner awake.  A run carries an owner-frozen
  :class:`DecisionSheet` of exact-scope :class:`StandingDecision` answers, the
  immutable never-delegable list, the three autonomous decision classes and
  their precedence, the deterministic preflight, the consecutive-failure
  circuit breaker, the no-silent-stall alarm, the plan-impact projection and the
  morning report.  Every instant and counter is supplied by the caller, so
  identical immutable input yields byte-identical output, and the policy half
  performs no I/O, no clock read and no provider or model launch.
* **The standing-decision store** (``unattended-decisions.json``): the owner's
  bounded, exact-scope answers, written only by ``orch away grant`` and read by
  the scheduler.  Validation lives in :class:`StandingDecision` and
  :class:`DecisionSheet`, so wildcards, prefixes and unbounded grants cannot
  even be constructed.
* **The away journal** (``away-journal.jsonl``): append-only, ``fsync``-durable
  records of every disposition, plan impact, preflight result and outcome.  A
  scheduler killed after a decision resumes and still knows what it did; the
  return summary is rebuilt from these records, never from memory.
* **The return summary**: "while you were away", assembled from the journal and
  reused by ``catch_up`` and the desktop notifier.

Only ``away`` mode takes autonomous decisions here, and only the existing
authority preset's actions do anything; a standing decision never widens it and
nothing in this module can push or move the target branch.  The policy half has
one vocabulary (standing decisions) and one store; the durable half writes only
that store, the journal and the owner authored policy document.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final

from saberops.paths import owner_state_dir

if TYPE_CHECKING:  # the question queue imports this module, so this direction is lazy
    from saberops.questions import DecisionClass, OwnerQuestion, OwnerQuestionQueue

__all__ = [
    "AWAY_JOURNAL_ENV",
    "AWAY_POLICY_ENV",
    "AWAY_STORE_ENV",
    "AcceptedCandidateRecord",
    "AlarmSeverity",
    "AuthorityMode",
    "AwayError",
    "AwayJournal",
    "AwayPolicy",
    "CONSECUTIVE_FAILURE_LIMIT",
    "CandidateAcceptance",
    "Decision",
    "DecisionClass",
    "DecisionDisposition",
    "DecisionResolution",
    "DecisionScope",
    "DecisionSheet",
    "DecisionSheetError",
    "DispatchGate",
    "EscalationRecord",
    "MAX_ALARM_MESSAGE_LENGTH",
    "MAX_ALTERNATIVES",
    "MAX_EVIDENCE_PATHS",
    "MAX_IDENTIFIER_LENGTH",
    "MAX_PATH_COUNT",
    "MAX_PATH_LENGTH",
    "MAX_SCOPES",
    "MAX_STANDING_DECISIONS",
    "MAX_TASKS",
    "MAX_TEXT_LENGTH",
    "MorningReport",
    "NEVER_DELEGABLE_KINDS",
    "NEVER_DELEGABLE_POLICY",
    "NeverDelegableKind",
    "NotificationChannel",
    "OWNER_ANSWER_SOURCE",
    "PREFLIGHT_WARN_PERCENT",
    "ParkedBranchRecord",
    "PlanImpact",
    "PlanTaskState",
    "PreflightBlocker",
    "PreflightBlockerCode",
    "PreflightError",
    "PreflightRequest",
    "PreflightResult",
    "ReturnSummary",
    "RunProgress",
    "STALL_CRITICAL_MULTIPLIER",
    "StallAlarm",
    "StallObservation",
    "StallSupervisionError",
    "StandingDecision",
    "StandingDecisionAction",
    "StandingDecisionStore",
    "UNATTENDED_PLANS_VERSION",
    "answer_question_under_standing_decision",
    "build_morning_report",
    "build_return_summary",
    "compute_plan_impact",
    "decision_key_for_question",
    "deliver_return_summary",
    "detect_stall",
    "evaluate_dispatch",
    "get_away_journal_path",
    "get_away_policy_path",
    "get_standing_decisions_path",
    "is_never_delegable",
    "never_delegable_description",
    "never_delegable_for_question",
    "project_scope_id",
    "question_scope",
    "record_job_outcome",
    "record_plan_impact",
    "record_preflight",
    "render_standing_decisions",
    "resolve_decision",
    "run_preflight",
]

# --- Policy constants and hard bounds ---------------------------------------

UNATTENDED_PLANS_VERSION = "unattended-plans/1"

#: The plan is halted automatically after this many consecutive task failures.
CONSECUTIVE_FAILURE_LIMIT = 3
#: Idle time at or above this multiple of the threshold is a critical alarm.
STALL_CRITICAL_MULTIPLIER = 2
#: Budget or wall-clock consumption at or above this share is warned, not blocked.
PREFLIGHT_WARN_PERCENT = 80

# --- Hard bounds: exceeding these is malformed/unbounded, fail closed --------
MAX_IDENTIFIER_LENGTH = 128
MAX_TEXT_LENGTH = 4_000
MAX_ALARM_MESSAGE_LENGTH = 512
MAX_PATH_COUNT = 512
MAX_PATH_LENGTH = 1_024
MAX_STANDING_DECISIONS = 64
MAX_ALTERNATIVES = 32
MAX_EVIDENCE_PATHS = 512
MAX_SCOPES = 512
MAX_TASKS = 1_000
MAX_TURNS_INPUT = 100_000_000
MAX_SECONDS_INPUT = 10_000_000_000
MAX_UNITS_INPUT = 10_000_000_000

#: Only the owner may author a standing decision; anything else is refused.
OWNER_ANSWER_SOURCE = "owner"

_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}\Z")
_PATH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,1023}\Z")
_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
_CONTENT_MARKERS = ("\n", "\r", "\x00")
#: Wildcards, alternation and shell expansion are never valid in a scope.
_WILDCARD_CHARS = "*?[]{}!$"
_NON_CANONICAL_PATH_SEGMENTS = ("", ".", "..")

# --- Durable documents the store reads and writes ---------------------------

#: Test/diagnostics override for the standing-decision document.
AWAY_STORE_ENV = "ORCH_UNATTENDED_STORE"
#: Test/diagnostics override for the append-only away journal.
AWAY_JOURNAL_ENV = "ORCH_AWAY_JOURNAL"
#: Test/diagnostics override for the owner-authored away policy.
AWAY_POLICY_ENV = "ORCH_AWAY_POLICY"

STORE_VERSION: Final[int] = 1
JOURNAL_VERSION: Final[int] = 1
POLICY_VERSION: Final[int] = 1

# --- One error root ---------------------------------------------------------

class AwayError(ValueError):
    """A durable away document or a caller-supplied fact is malformed.

    One error root for the whole module: the policy subclasses below name the
    narrower failure, and a caller that wants to fail closed catches
    :class:`AwayError`.
    """


class DecisionSheetError(AwayError):
    """The decision sheet or one of its standing decisions is invalid."""


class PreflightError(AwayError):
    """The preflight input is malformed or unbounded."""


class StallSupervisionError(AwayError):
    """A run would be left without a loud alarm channel or a live supervisor."""


# --- The away policy --------------------------------------------------------

class NeverDelegableKind(StrEnum):
    """The immutable never-delegable decision list.

    A decision of one of these kinds can never be taken autonomously, by a
    standing decision or by the Orchestrator's recommendation. It parks its own
    branch/task and reaches the owner.
    """

    TEST_GATE = "test_gate"
    DATA_TRAINING_POLICY = "data_training_policy"
    ROADMAP_SCOPE = "roadmap_scope"
    BUDGET_CAP = "budget_cap"
    CREDENTIALS = "credentials"
    DESTRUCTIVE_OPERATIONS = "destructive_operations"
    PUBLISHING = "publishing"


#: Immutable, ordered never-delegable policy. ``MappingProxyType`` refuses
#: mutation, and the tuple is derived from it, so no caller can widen the list.
NEVER_DELEGABLE_POLICY: Final[Mapping[NeverDelegableKind, str]] = MappingProxyType(
    {
        NeverDelegableKind.TEST_GATE: (
            "disabling or dropping the test gate, or loosening its evidence"
        ),
        NeverDelegableKind.DATA_TRAINING_POLICY: (
            "changing data or training policies, or their licenses"
        ),
        NeverDelegableKind.ROADMAP_SCOPE: (
            "changing roadmap scope or step boundaries"
        ),
        NeverDelegableKind.BUDGET_CAP: (
            "raising resource or financial budgets, or concurrency caps"
        ),
        NeverDelegableKind.CREDENTIALS: "managing credentials or secrets",
        NeverDelegableKind.DESTRUCTIVE_OPERATIONS: (
            "destructive operations such as force push, deleting repositories "
            "or dropping databases"
        ),
        NeverDelegableKind.PUBLISHING: "external publishing or releases",
    }
)
#: Frozen iteration order of the never-delegable list.
NEVER_DELEGABLE_KINDS: Final[tuple[NeverDelegableKind, ...]] = tuple(NEVER_DELEGABLE_POLICY)


class AutonomyClass(StrEnum):
    """The three autonomous decision classes (roadmap RD-1)."""

    CLASS_1_GATED_ACCEPT = "class_1_gated_accept"
    CLASS_2_RECOMMENDED = "class_2_recommended"
    CLASS_3_NEVER_DELEGABLE = "class_3_never_delegable"


class DecisionDisposition(StrEnum):
    """The deterministic outcome for one decision. The runtime applies it."""

    APPLY_STANDING_DECISION = "apply_standing_decision"
    AUTO_ACCEPT_LOCAL_COMMIT = "auto_accept_local_commit"
    TAKE_RECOMMENDED_OPTION = "take_recommended_option"
    PARK_BRANCH = "park_branch"
    ESCALATE_OWNER = "escalate_owner"


class AuthorityMode(StrEnum):
    """How much of a plan's discretion the engine may exercise without the owner.

    Only ``FULL_AUTONOMY`` may apply the autonomous decision classes; every other
    mode asks the owner for anything the frozen decision sheet does not answer.
    """

    FULL_AUTONOMY = "full_autonomy"
    SUPERVISED = "supervised"


class NotificationChannel(StrEnum):
    """A configurable channel a loud stall alarm may be raised through."""

    LOG = "log"
    TELEGRAM = "telegram"
    WEB = "web"


class AlarmSeverity(StrEnum):
    """How loudly a stall alarm must shout."""

    ALARM = "alarm"
    CRITICAL = "critical"


class PreflightBlockerCode(StrEnum):
    """Stable, explainable reason a readiness preflight refuses the start."""

    RUN_ID_MISMATCH = "run_id_mismatch"
    BUDGET_NOT_SET = "budget_not_set"
    BUDGET_EXCEEDED = "budget_exceeded"
    WALL_CLOCK_NOT_SET = "wall_clock_not_set"
    WALL_CLOCK_EXCEEDED = "wall_clock_exceeded"
    NO_AUTHORIZED_SCOPE = "no_authorized_scope"
    WORKER_CAP_NOT_SET = "worker_cap_not_set"
    WORKER_CAP_EXCEEDED = "worker_cap_exceeded"
    NO_STALL_CHANNEL = "no_stall_channel"


def _coerce_enum[EnumT: StrEnum](enum_type: type[EnumT], value: object, label: str) -> EnumT:
    if isinstance(value, enum_type):
        return value
    if isinstance(value, str):
        try:
            return enum_type(value)
        except ValueError:
            try:
                return enum_type[value]
            except KeyError:
                pass
    expected = ", ".join(member.value for member in enum_type)
    raise AwayError(f"{label} must be one of: {expected}")


def _require_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AwayError(f"{label} must be an integer")
    return value


def _require_bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise AwayError(f"{label} must be a boolean")
    return value


def _require_bounded_int(value: object, label: str, *, maximum: int, minimum: int = 0) -> int:
    number = _require_int(value, label)
    if number < minimum:
        raise AwayError(f"{label} must not be below {minimum}")
    if number > maximum:
        raise AwayError(f"{label} exceeds the hard limit of {maximum}")
    return number


def _validate_identifier(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise AwayError(f"{label} must be a string")
    if len(value) > MAX_IDENTIFIER_LENGTH:
        raise AwayError(
            f"{label} exceeds the maximum length of {MAX_IDENTIFIER_LENGTH} characters"
        )
    if any(character in value for character in _WILDCARD_CHARS):
        raise AwayError(
            f"{label} is malformed: wildcards and expansion characters are never "
            f"valid, got {value!r}"
        )
    if _IDENTIFIER_RE.fullmatch(value) is None:
        raise AwayError(
            f"{label} is malformed: expected an alphanumeric-leading token of "
            f"[A-Za-z0-9._/-], got {value!r}"
        )
    return value


def _validate_sha(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA_RE.fullmatch(value) is None:
        raise AwayError(
            f"{label} must be an exact 40-character lowercase hex SHA (no abbreviation), "
            f"got {value!r}"
        )
    return value


def _validate_text(value: object, label: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise AwayError(f"{label} must be a string")
    if value.strip() == "":
        raise AwayError(f"{label} must not be empty")
    if len(value) > max_length:
        raise AwayError(f"{label} exceeds the maximum length of {max_length} characters")
    if "\x00" in value or "\r" in value:
        raise AwayError(f"{label} must not contain NUL or carriage returns")
    return value


def _optional_text(value: object, label: str, max_length: int) -> str | None:
    if value is None:
        return None
    return _validate_text(value, label, max_length)


def _validate_text_sequence(
    value: object, label: str, max_items: int, max_item_length: int
) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise AwayError(f"{label} must be an ordered sequence of text entries")
    items = tuple(value)
    if len(items) > max_items:
        raise AwayError(
            f"{label} has {len(items)} entries; the hard limit is {max_items} (unbounded input "
            "refused)"
        )
    validated: list[str] = []
    for index, item in enumerate(items):
        validated.append(_validate_text(item, f"{label}[{index}]", max_item_length))
    return tuple(validated)


def _canonical_repo_path(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise AwayError(f"{label} must be a string")
    if any(character in value for character in _WILDCARD_CHARS):
        raise AwayError(
            f"{label} is malformed: a scope path is an exact repository-relative path and "
            f"never a wildcard or pattern, got {value!r}"
        )
    if any(marker in value for marker in _CONTENT_MARKERS):
        raise AwayError(
            f"{label} must be a path reference only, not file content "
            "(newlines, carriage returns and NUL are refused)"
        )
    if value.startswith("/") or "\\" in value:
        raise AwayError(f"{label} must be a repository-relative path, got {value!r}")
    if len(value) > MAX_PATH_LENGTH:
        raise AwayError(f"{label} exceeds the maximum length of {MAX_PATH_LENGTH} characters")
    if _PATH_RE.fullmatch(value) is None:
        raise AwayError(f"{label} is malformed, got {value!r}")
    if any(segment in _NON_CANONICAL_PATH_SEGMENTS for segment in value.split("/")):
        raise AwayError(
            f"{label} must be canonical: no leading './', no '.'/'..' segment and no trailing "
            f"'/', got {value!r}"
        )
    return value


def _canonical_repo_paths(value: object, label: str) -> tuple[str, ...]:
    items = _validate_text_sequence(value, label, MAX_PATH_COUNT, MAX_PATH_LENGTH)
    canonical = {
        _canonical_repo_path(item, f"{label}[{index}]") for index, item in enumerate(items)
    }
    return tuple(sorted(canonical))


def _parse_instant(value: object, label: str) -> datetime:
    """Parse one timezone-aware ISO-8601 instant, normalized to UTC.

    Every decision bound and every durable record is compared as an exact UTC
    instant; a naive instant is ambiguous and is refused.
    """
    if not isinstance(value, str):
        raise AwayError(f"{label} must be an ISO-8601 timestamp string")
    if len(value) > 64:
        raise AwayError(f"{label} exceeds the maximum length of 64 characters")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise AwayError(f"{label} is not a valid ISO-8601 timestamp: {error}") from error
    if parsed.tzinfo is None:
        raise AwayError(
            f"{label} must be timezone-aware (e.g. '2026-10-18T06:00:00+00:00'); a naive "
            "instant is ambiguous and is refused"
        )
    return parsed.astimezone(UTC)


def is_never_delegable(kind: object) -> bool:
    """True iff ``kind`` is on the immutable never-delegable list."""
    coerced = _coerce_enum(NeverDelegableKind, kind, "never_delegable kind")
    return coerced in NEVER_DELEGABLE_POLICY


def never_delegable_description(kind: object) -> str:
    """The frozen human-readable description of a never-delegable kind."""
    coerced = _coerce_enum(NeverDelegableKind, kind, "never_delegable kind")
    return NEVER_DELEGABLE_POLICY[coerced]


@dataclass(frozen=True)
class DecisionScope:
    """The exact scope a decision belongs to: project, task and repository paths.

    Scopes match **exactly**, never by analogy: there is no prefix match, no
    glob, no path parent match and no wildcard. Wildcard and expansion
    characters are rejected at construction. Paths must already be canonical
    repository-relative paths; the tuple is sorted and de-duplicated, which is
    canonicalisation of order only, never a relaxation of the match.
    """

    project_id: str
    task_id: str
    paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "project_id", _validate_identifier(self.project_id, "scope.project_id")
        )
        object.__setattr__(self, "task_id", _validate_identifier(self.task_id, "scope.task_id"))
        object.__setattr__(self, "paths", _canonical_repo_paths(self.paths, "scope.paths"))

    def matches_exactly(self, other: object) -> bool:
        """True only for a field-for-field identical scope; never an analogy."""
        if not isinstance(other, DecisionScope):
            raise AwayError("matches_exactly requires a DecisionScope")
        return (
            self.project_id == other.project_id
            and self.task_id == other.task_id
            and self.paths == other.paths
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "task_id": self.task_id,
            "paths": list(self.paths),
        }


@dataclass(frozen=True)
class StandingDecision:
    """One owner answer, frozen into the run as a bounded standing permission.

    The owner's ``owner_words`` are recorded verbatim and are never summarised or
    paraphrased. A standing decision is always bounded: it must carry an
    absolute ``expires_at`` instant and/or a ``ttl_turns`` count with the
    ``granted_at_turn`` it starts from. ``answered_by`` must be the owner, so no
    Orchestrator- or model-authored text can become a standing decision.
    """

    standing_decision_id: str
    scope: DecisionScope
    decision_key: str
    owner_words: str
    answered_by: str
    granted_at_turn: int | None = None
    ttl_turns: int | None = None
    expires_at: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "standing_decision_id",
            _validate_identifier(self.standing_decision_id, "standing_decision_id"),
        )
        if not isinstance(self.scope, DecisionScope):
            raise DecisionSheetError("standing_decision.scope must be a DecisionScope")
        object.__setattr__(
            self,
            "decision_key",
            _validate_identifier(self.decision_key, "standing_decision.decision_key"),
        )
        object.__setattr__(
            self,
            "owner_words",
            _validate_text(self.owner_words, "standing_decision.owner_words", MAX_TEXT_LENGTH),
        )
        if not isinstance(self.answered_by, str) or self.answered_by != OWNER_ANSWER_SOURCE:
            raise DecisionSheetError(
                f"standing_decision.answered_by must be {OWNER_ANSWER_SOURCE!r}; only the owner "
                "may author a standing decision"
            )
        if self.granted_at_turn is not None:
            object.__setattr__(
                self,
                "granted_at_turn",
                _require_bounded_int(
                    self.granted_at_turn,
                    "standing_decision.granted_at_turn",
                    maximum=MAX_TURNS_INPUT,
                ),
            )
        if self.ttl_turns is not None:
            object.__setattr__(
                self,
                "ttl_turns",
                _require_bounded_int(
                    self.ttl_turns,
                    "standing_decision.ttl_turns",
                    maximum=MAX_TURNS_INPUT,
                    minimum=1,
                ),
            )
            if self.granted_at_turn is None:
                raise DecisionSheetError(
                    "standing_decision.ttl_turns requires granted_at_turn so expiry is exact"
                )
        if self.expires_at is not None:
            _parse_instant(self.expires_at, "standing_decision.expires_at")
        if self.expires_at is None and self.ttl_turns is None:
            raise DecisionSheetError(
                "a standing decision must be bounded by expires_at and/or ttl_turns; an "
                "unbounded standing permission is refused"
            )

    def is_expired(self, *, now: str, current_turn: int) -> bool:
        """True when the decision is no longer live at ``now`` / ``current_turn``.

        The absolute bound expires **at** its instant (live strictly before it);
        the turn bound is live for ``ttl_turns`` turns starting at
        ``granted_at_turn`` (i.e. expired once ``current_turn`` reaches
        ``granted_at_turn + ttl_turns``).
        """
        instant = _parse_instant(now, "now")
        turn = _require_bounded_int(current_turn, "current_turn", maximum=MAX_TURNS_INPUT)
        if self.expires_at is not None and instant >= _parse_instant(
            self.expires_at, "standing_decision.expires_at"
        ):
            return True
        if self.ttl_turns is not None and self.granted_at_turn is not None:
            if turn >= self.granted_at_turn + self.ttl_turns:
                return True
        return False

    def is_live(self, *, now: str, current_turn: int) -> bool:
        """Convenience inverse of :meth:`is_expired`."""
        return not self.is_expired(now=now, current_turn=current_turn)

    def as_dict(self) -> dict[str, Any]:
        return {
            "standing_decision_id": self.standing_decision_id,
            "scope": self.scope.as_dict(),
            "decision_key": self.decision_key,
            "owner_words": self.owner_words,
            "answered_by": self.answered_by,
            "granted_at_turn": self.granted_at_turn,
            "ttl_turns": self.ttl_turns,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True)
class DecisionSheet:
    """The owner-frozen decision sheet of one unattended run.

    One standing decision per ``(scope, decision_key)`` pair, so matching is
    unambiguous and never a "first wildcard wins" guess.
    """

    run_id: str
    plan_reference: str
    standing_decisions: tuple[StandingDecision, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "run_id", _validate_identifier(self.run_id, "decision_sheet.run_id")
        )
        object.__setattr__(
            self,
            "plan_reference",
            _validate_text(self.plan_reference, "decision_sheet.plan_reference", MAX_TEXT_LENGTH),
        )
        if isinstance(self.standing_decisions, (str, bytes)) or not isinstance(
            self.standing_decisions, Sequence
        ):
            raise DecisionSheetError("standing_decisions must be an ordered sequence")
        decisions = tuple(self.standing_decisions)
        if len(decisions) > MAX_STANDING_DECISIONS:
            raise DecisionSheetError(
                f"decision sheet has {len(decisions)} standing decisions; the hard limit is "
                f"{MAX_STANDING_DECISIONS}"
            )
        ids: set[str] = set()
        pairs: set[tuple[str, str, tuple[str, ...], str]] = set()
        for index, decision in enumerate(decisions):
            if not isinstance(decision, StandingDecision):
                raise DecisionSheetError(f"standing_decisions[{index}] must be a StandingDecision")
            if decision.standing_decision_id in ids:
                raise DecisionSheetError(
                    f"duplicate standing_decision_id {decision.standing_decision_id!r}"
                )
            ids.add(decision.standing_decision_id)
            scope = decision.scope
            pair = (scope.project_id, scope.task_id, scope.paths, decision.decision_key)
            if pair in pairs:
                raise DecisionSheetError(
                    "two standing decisions share the same scope and decision_key; the match "
                    "would be ambiguous"
                )
            pairs.add(pair)
        object.__setattr__(self, "standing_decisions", decisions)

    def find(self, decision_key: str, scope: DecisionScope) -> StandingDecision | None:
        """The single decision whose scope and key match exactly, or ``None``."""
        for standing in self.standing_decisions:
            if standing.decision_key == decision_key and standing.scope.matches_exactly(scope):
                return standing
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "plan_reference": self.plan_reference,
            "standing_decisions": [item.as_dict() for item in self.standing_decisions],
        }


@dataclass(frozen=True)
class CandidateAcceptance:
    """The exact evidence a Class-1 auto-acceptance requires.

    All five facts must be true. The action is a local commit only; this module
    has no push action at all, so a run can never push.
    """

    candidate_id: str
    head_sha: str
    gate_passed: bool
    cold_review_passed: bool
    checklist_green: bool
    scope_authorized: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "candidate_id", _validate_identifier(self.candidate_id, "acceptance.candidate_id")
        )
        object.__setattr__(
            self, "head_sha", _validate_sha(self.head_sha, "acceptance.head_sha")
        )
        for field_name in (
            "gate_passed",
            "cold_review_passed",
            "checklist_green",
            "scope_authorized",
        ):
            object.__setattr__(
                self,
                field_name,
                _require_bool(getattr(self, field_name), f"acceptance.{field_name}"),
            )

    def unmet_conditions(self) -> tuple[str, ...]:
        """The Class-1 conditions that are not satisfied, in a stable order."""
        failures: list[str] = []
        if not self.gate_passed:
            failures.append("gate_passed")
        if not self.cold_review_passed:
            failures.append("cold_review_passed")
        if not self.checklist_green:
            failures.append("checklist_green")
        if not self.scope_authorized:
            failures.append("scope_authorized")
        return tuple(failures)

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "head_sha": self.head_sha,
            "gate_passed": self.gate_passed,
            "cold_review_passed": self.cold_review_passed,
            "checklist_green": self.checklist_green,
            "scope_authorized": self.scope_authorized,
        }


@dataclass(frozen=True)
class Decision:
    """One decision the unattended runner must resolve.

    ``never_delegable`` marks a decision that can never be taken autonomously.
    ``acceptance`` marks a gated acceptance candidate. ``recommended_option``
    with ``alternatives`` is the Orchestrator's Class-2 proposal; when it is
    absent the other two fields must be absent too.
    """

    decision_id: str
    scope: DecisionScope
    decision_key: str
    question: str
    never_delegable: NeverDelegableKind | None = None
    acceptance: CandidateAcceptance | None = None
    recommended_option: str | None = None
    recommended_rationale: str | None = None
    alternatives: tuple[str, ...] = ()
    evidence_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "decision_id", _validate_identifier(self.decision_id, "decision.decision_id")
        )
        if not isinstance(self.scope, DecisionScope):
            raise AwayError("decision.scope must be a DecisionScope")
        object.__setattr__(
            self, "decision_key", _validate_identifier(self.decision_key, "decision.decision_key")
        )
        object.__setattr__(
            self, "question", _validate_text(self.question, "decision.question", MAX_TEXT_LENGTH)
        )
        if self.never_delegable is not None:
            object.__setattr__(
                self,
                "never_delegable",
                _coerce_enum(
                    NeverDelegableKind, self.never_delegable, "decision.never_delegable"
                ),
            )
        if self.acceptance is not None and not isinstance(self.acceptance, CandidateAcceptance):
            raise AwayError("decision.acceptance must be a CandidateAcceptance or None")
        object.__setattr__(
            self,
            "recommended_option",
            _optional_text(self.recommended_option, "decision.recommended_option", MAX_TEXT_LENGTH),
        )
        object.__setattr__(
            self,
            "recommended_rationale",
            _optional_text(
                self.recommended_rationale, "decision.recommended_rationale", MAX_TEXT_LENGTH
            ),
        )
        object.__setattr__(
            self,
            "alternatives",
            _validate_text_sequence(
                self.alternatives, "decision.alternatives", MAX_ALTERNATIVES, MAX_TEXT_LENGTH
            ),
        )
        if len(self.alternatives) > 0 and self.recommended_option is None:
            raise AwayError(
                "decision.alternatives requires decision.recommended_option; rejected alternatives "
                "belong to a recommended option"
            )
        if self.recommended_rationale is not None and self.recommended_option is None:
            raise AwayError(
                "decision.recommended_rationale requires decision.recommended_option"
            )
        object.__setattr__(
            self,
            "evidence_paths",
            _canonical_repo_paths(self.evidence_paths, "decision.evidence_paths"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "scope": self.scope.as_dict(),
            "decision_key": self.decision_key,
            "question": self.question,
            "never_delegable": None
            if self.never_delegable is None
            else self.never_delegable.value,
            "acceptance": None if self.acceptance is None else self.acceptance.as_dict(),
            "recommended_option": self.recommended_option,
            "recommended_rationale": self.recommended_rationale,
            "alternatives": list(self.alternatives),
            "evidence_paths": list(self.evidence_paths),
        }


@dataclass(frozen=True)
class DecisionResolution:
    """The deterministic, explainable outcome for one :class:`Decision`.

    ``push_allowed`` is always false: the module has no disposition that pushes,
    so an unattended run can never move a remote. A Class-1 acceptance is a local
    commit only.
    """

    decision_id: str
    scope: DecisionScope
    disposition: DecisionDisposition
    autonomy_class: AutonomyClass | None
    decision_key: str
    chosen_option: str | None
    owner_words: str | None
    applied_standing_decision_id: str | None
    rejected_alternatives: tuple[str, ...]
    evidence_paths: tuple[str, ...]
    parks_scope: DecisionScope | None
    audit_required: bool
    explanation: tuple[str, ...]
    push_allowed: bool = False
    policy_version: str = UNATTENDED_PLANS_VERSION

    def __post_init__(self) -> None:
        if self.push_allowed is not False:
            raise AwayError(
                "an unattended resolution may never allow a push; pushing is owner-only"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "scope": self.scope.as_dict(),
            "disposition": self.disposition.value,
            "autonomy_class": None if self.autonomy_class is None else self.autonomy_class.value,
            "decision_key": self.decision_key,
            "chosen_option": self.chosen_option,
            "owner_words": self.owner_words,
            "applied_standing_decision_id": self.applied_standing_decision_id,
            "rejected_alternatives": list(self.rejected_alternatives),
            "evidence_paths": list(self.evidence_paths),
            "parks_scope": None if self.parks_scope is None else self.parks_scope.as_dict(),
            "audit_required": self.audit_required,
            "explanation": list(self.explanation),
            "push_allowed": self.push_allowed,
            "policy_version": self.policy_version,
        }

    def canonical_json(self) -> str:
        return json.dumps(
            self.as_dict(), ensure_ascii=False, sort_keys=False, separators=(",", ":")
        )


def _resolution(
    decision: Decision,
    disposition: DecisionDisposition,
    autonomy_class: AutonomyClass | None,
    *,
    chosen_option: str | None = None,
    owner_words: str | None = None,
    applied_standing_decision_id: str | None = None,
    rejected_alternatives: tuple[str, ...] = (),
    parks_scope: DecisionScope | None = None,
    audit_required: bool = False,
    explanation: tuple[str, ...],
) -> DecisionResolution:
    return DecisionResolution(
        decision_id=decision.decision_id,
        scope=decision.scope,
        disposition=disposition,
        autonomy_class=autonomy_class,
        decision_key=decision.decision_key,
        chosen_option=chosen_option,
        owner_words=owner_words,
        applied_standing_decision_id=applied_standing_decision_id,
        rejected_alternatives=rejected_alternatives,
        evidence_paths=decision.evidence_paths,
        parks_scope=parks_scope,
        audit_required=audit_required,
        explanation=(f"{decision.decision_id}: {disposition.value}", *explanation),
    )


def resolve_decision(
    sheet: DecisionSheet,
    decision: Decision,
    *,
    now: str,
    current_turn: int,
    authority_mode: AuthorityMode = AuthorityMode.FULL_AUTONOMY,
) -> DecisionResolution:
    """Resolve one decision deterministically under the frozen decision sheet.

    Precedence (first match wins, so the result never depends on input order):

    1. **Never-delegable** -> ``PARK_BRANCH`` (Class 3). This precedes the sheet
       and every autonomous class, so nothing can widen the list.
    2. **Exact standing decision** (live, exact scope, exact key) ->
       ``APPLY_STANDING_DECISION``, recording the owner's words verbatim.
    3. **Class 1** (full autonomy only) -> ``AUTO_ACCEPT_LOCAL_COMMIT`` when the
       gate passed, the cold review passed, the checklist is green, the scope is
       authorized and evidence exists. The action is a local commit; a push is
       never permitted.
    4. **Class 2** (full autonomy only) -> ``TAKE_RECOMMENDED_OPTION`` when the
       Orchestrator's recommendation exists with at least one rejected
       alternative and evidence.
    5. **Class 3** -> ``ESCALATE_OWNER`` for every other unhandled decision.

    A supervised run applies only rule 2; everything else reaches the owner.
    """
    if not isinstance(sheet, DecisionSheet):
        raise DecisionSheetError("resolve_decision requires a DecisionSheet")
    if not isinstance(decision, Decision):
        raise AwayError("resolve_decision requires a Decision")
    mode = _coerce_enum(AuthorityMode, authority_mode, "authority_mode")
    _parse_instant(now, "now")
    _require_bounded_int(current_turn, "current_turn", maximum=MAX_TURNS_INPUT)

    # 1. Never-delegable first: no sheet, class or recommendation can widen it.
    if decision.never_delegable is not None:
        kind = _coerce_enum(
            NeverDelegableKind, decision.never_delegable, "decision.never_delegable"
        )
        return _resolution(
            decision,
            DecisionDisposition.PARK_BRANCH,
            AutonomyClass.CLASS_3_NEVER_DELEGABLE,
            parks_scope=decision.scope,
            explanation=(
                f"never-delegable ({kind.value}): {NEVER_DELEGABLE_POLICY[kind]}",
                "parks only this branch/task; dependencies wait and independent work continues",
                "the owner answers this decision; the plan is never stalled by one park",
            ),
        )

    # 2. An exact, live standing decision answers it; nothing looser ever matches.
    standing = sheet.find(decision.decision_key, decision.scope)
    if standing is not None and standing.is_live(now=now, current_turn=current_turn):
        return _resolution(
            decision,
            DecisionDisposition.APPLY_STANDING_DECISION,
            None,
            owner_words=standing.owner_words,
            applied_standing_decision_id=standing.standing_decision_id,
            explanation=(
                f"exact-scope standing decision {standing.standing_decision_id} applies",
                "owner words are recorded verbatim and are never paraphrased",
            ),
        )

    if mode is AuthorityMode.FULL_AUTONOMY:
        # 3. Class 1: gated acceptance, local commit only, never a push.
        if decision.acceptance is not None:
            unmet = decision.acceptance.unmet_conditions()
            if not unmet and decision.evidence_paths:
                return _resolution(
                    decision,
                    DecisionDisposition.AUTO_ACCEPT_LOCAL_COMMIT,
                    AutonomyClass.CLASS_1_GATED_ACCEPT,
                    chosen_option=f"accept {decision.acceptance.candidate_id}",
                    audit_required=True,
                    explanation=(
                        "gate passed, cold review passed and checklist is green",
                        f"accepts candidate {decision.acceptance.candidate_id} at "
                        f"{decision.acceptance.head_sha} as a local commit",
                        "a push is never permitted for an unattended run",
                    ),
                )

        # 4. Class 2: the recommended option, with evidence and alternatives.
        if (
            decision.recommended_option is not None
            and decision.alternatives
            and decision.evidence_paths
        ):
            return _resolution(
                decision,
                DecisionDisposition.TAKE_RECOMMENDED_OPTION,
                AutonomyClass.CLASS_2_RECOMMENDED,
                chosen_option=decision.recommended_option,
                rejected_alternatives=decision.alternatives,
                audit_required=True,
                explanation=(
                    "not answered by the decision sheet and not never-delegable",
                    f"takes the Orchestrator's recommended option: {decision.recommended_option}",
                    "logged with evidence and the rejected alternatives in the audit list",
                ),
            )

    # 5. Everything else reaches the owner. Class 3 covers park and escalate.
    reasons: list[str] = []
    if mode is not AuthorityMode.FULL_AUTONOMY:
        reasons.append(f"authority mode {mode.value} asks the owner for unhandled decisions")
    if not decision.evidence_paths:
        reasons.append("no evidence path was supplied")
    if standing is not None and not standing.is_live(now=now, current_turn=current_turn):
        reasons.append("the matching standing decision has expired")
    elif standing is None:
        reasons.append("no standing decision matches this exact scope and decision key")
    if decision.acceptance is not None:
        unmet = decision.acceptance.unmet_conditions()
        if unmet:
            reasons.append(f"Class-1 acceptance conditions unmet: {', '.join(unmet)}")
    if decision.recommended_option is None:
        reasons.append("no recommended option was proposed")
    else:
        if not decision.alternatives:
            reasons.append("the recommended option has no rejected alternative to audit")
        if not decision.evidence_paths:
            reasons.append("the recommended option has no evidence to audit")
    return _resolution(
        decision,
        DecisionDisposition.ESCALATE_OWNER,
        AutonomyClass.CLASS_3_NEVER_DELEGABLE,
        explanation=tuple(reasons) or ("unhandled by the autonomous classes; ask the owner",),
    )


@dataclass(frozen=True)
class RunProgress:
    """Consecutive-failure accounting for the circuit breaker.

    A pass resets the counter. Once the counter reaches
    :data:`CONSECUTIVE_FAILURE_LIMIT` the plan is **halted**, and halting is
    terminal: a later pass cannot silently resume a plan the breaker stopped.
    """

    consecutive_failures: int = 0
    halted: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "consecutive_failures",
            _require_bounded_int(
                self.consecutive_failures,
                "progress.consecutive_failures",
                maximum=MAX_UNITS_INPUT,
            ),
        )
        object.__setattr__(self, "halted", _require_bool(self.halted, "progress.halted"))

    @property
    def circuit_open(self) -> bool:
        """True once the consecutive-failure limit has been reached."""
        return self.consecutive_failures >= CONSECUTIVE_FAILURE_LIMIT

    def after_task(self, *, passed: bool) -> RunProgress:
        """Fold one task outcome into the breaker state, deterministically."""
        if self.halted or self.circuit_open:
            return RunProgress(
                consecutive_failures=self.consecutive_failures,
                halted=True,
            )
        if passed:
            return RunProgress(consecutive_failures=0, halted=False)
        failures = self.consecutive_failures + 1
        return RunProgress(
            consecutive_failures=failures,
            halted=failures >= CONSECUTIVE_FAILURE_LIMIT,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "consecutive_failures": self.consecutive_failures,
            "circuit_open": self.circuit_open,
            "halted": self.halted,
        }


@dataclass(frozen=True)
class StallObservation:
    """Explicit supervisory facts: heartbeat, supervisor liveness, stalled work.

    ``channels`` must be non-empty: a run that could raise no alarm would be a
    silent stall, which is refused. ``last_heartbeat_at`` must not be in the
    future relative to ``now``.
    """

    run_id: str
    last_heartbeat_at: str
    now: str
    stall_threshold_seconds: int
    stalled_scopes: tuple[DecisionScope, ...] = ()
    channels: tuple[NotificationChannel, ...] = ()
    supervisor_alive: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _validate_identifier(self.run_id, "observation.run_id"))
        heartbeat = _parse_instant(self.last_heartbeat_at, "observation.last_heartbeat_at")
        instant = _parse_instant(self.now, "observation.now")
        if heartbeat > instant:
            raise StallSupervisionError(
                "observation.last_heartbeat_at must not be later than observation.now"
            )
        object.__setattr__(
            self,
            "stall_threshold_seconds",
            _require_bounded_int(
                self.stall_threshold_seconds,
                "observation.stall_threshold_seconds",
                maximum=MAX_SECONDS_INPUT,
                minimum=1,
            ),
        )
        if isinstance(self.stalled_scopes, (str, bytes)) or not isinstance(
            self.stalled_scopes, Sequence
        ):
            raise AwayError("observation.stalled_scopes must be an ordered sequence")
        scopes = tuple(self.stalled_scopes)
        if len(scopes) > MAX_SCOPES:
            raise AwayError(
                f"observation.stalled_scopes has {len(scopes)} entries; the hard limit is "
                f"{MAX_SCOPES}"
            )
        for index, scope in enumerate(scopes):
            if not isinstance(scope, DecisionScope):
                raise AwayError(
                    f"observation.stalled_scopes[{index}] must be a DecisionScope"
                )
        object.__setattr__(self, "stalled_scopes", scopes)
        if isinstance(self.channels, (str, bytes)) or not isinstance(self.channels, Sequence):
            raise AwayError("observation.channels must be an ordered sequence")
        channels = tuple(
            _coerce_enum(NotificationChannel, channel, "observation.channels")
            for channel in self.channels
        )
        if len(channels) == 0:
            raise StallSupervisionError(
                "a run must be configured with at least one alarm channel; a run that can raise "
                "no alarm is a silent stall and is refused"
            )
        if len(set(channels)) != len(channels):
            raise AwayError("observation.channels must not repeat a channel")
        object.__setattr__(self, "channels", channels)
        object.__setattr__(
            self,
            "supervisor_alive",
            _require_bool(self.supervisor_alive, "observation.supervisor_alive"),
        )

    @property
    def idle_seconds(self) -> int:
        """Whole seconds since the last heartbeat (never negative)."""
        elapsed = _parse_instant(self.now, "observation.now") - _parse_instant(
            self.last_heartbeat_at, "observation.last_heartbeat_at"
        )
        return int(elapsed.total_seconds())


@dataclass(frozen=True)
class StallAlarm:
    """A loud, single-line stall alarm for the configured channels."""

    run_id: str
    severity: AlarmSeverity
    idle_seconds: int
    stalled_scopes: tuple[DecisionScope, ...]
    channels: tuple[NotificationChannel, ...]
    reason: str
    message: str
    policy_version: str = UNATTENDED_PLANS_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "severity": self.severity.value,
            "idle_seconds": self.idle_seconds,
            "stalled_scopes": [scope.as_dict() for scope in self.stalled_scopes],
            "channels": [channel.value for channel in self.channels],
            "reason": self.reason,
            "message": self.message,
            "policy_version": self.policy_version,
        }


def detect_stall(observation: StallObservation) -> StallAlarm | None:
    """Raise a loud alarm when the run is silent, unsupervised or stalled.

    Returns ``None`` for a healthy run. The alarm is ``CRITICAL`` when the
    supervisor is dead or idleness reaches
    :data:`STALL_CRITICAL_MULTIPLIER` times the threshold; otherwise ``ALARM``.
    The message is a bounded single line so it can travel any channel safely.
    """
    if not isinstance(observation, StallObservation):
        raise StallSupervisionError("detect_stall requires a StallObservation")
    idle = observation.idle_seconds
    threshold = observation.stall_threshold_seconds
    reasons: list[str] = []
    if not observation.supervisor_alive:
        reasons.append("no live supervisor")
    if idle >= threshold:
        reasons.append(f"heartbeat idle {idle}s at or past the {threshold}s threshold")
    if observation.stalled_scopes:
        reasons.append(f"{len(observation.stalled_scopes)} stalled scope(s)")
    if not reasons:
        return None
    critical = (
        not observation.supervisor_alive or idle >= threshold * STALL_CRITICAL_MULTIPLIER
    )
    channels = ", ".join(channel.value for channel in observation.channels)
    message = (
        f"STALL run={observation.run_id} severity="
        f"{'critical' if critical else 'alarm'} idle={idle}s: {'; '.join(reasons)}; "
        f"channels: {channels}"
    )
    if len(message) > MAX_ALARM_MESSAGE_LENGTH:
        message = message[:MAX_ALARM_MESSAGE_LENGTH]
    return StallAlarm(
        run_id=observation.run_id,
        severity=AlarmSeverity.CRITICAL if critical else AlarmSeverity.ALARM,
        idle_seconds=idle,
        stalled_scopes=observation.stalled_scopes,
        channels=observation.channels,
        reason="; ".join(reasons),
        message=message,
    )


@dataclass(frozen=True)
class PreflightBlocker:
    """One reason the readiness preflight refuses to start the run."""

    code: PreflightBlockerCode
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code.value, "detail": self.detail}


@dataclass(frozen=True)
class PreflightResult:
    """The go/no-go outcome of the deterministic preflight (no model involved)."""

    run_id: str
    ready: bool
    blockers: tuple[PreflightBlocker, ...]
    warnings: tuple[str, ...]
    policy_version: str = UNATTENDED_PLANS_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "ready": self.ready,
            "blockers": [blocker.as_dict() for blocker in self.blockers],
            "warnings": list(self.warnings),
            "policy_version": self.policy_version,
        }


@dataclass(frozen=True)
class PreflightRequest:
    """All explicit facts the deterministic preflight checks before a start.

    Nothing here is measured by the module: budget, wall-clock, scope, worker
    occupancy and channels are engine facts supplied by the caller.
    """

    run_id: str
    plan_reference: str
    decision_sheet: DecisionSheet
    budget_limit_units: int
    budget_spent_units: int
    wall_clock_limit_seconds: int
    wall_clock_elapsed_seconds: int
    authorized_paths: tuple[str, ...]
    worker_cap: int
    active_workers: int
    notification_channels: tuple[NotificationChannel, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _validate_identifier(self.run_id, "preflight.run_id"))
        object.__setattr__(
            self,
            "plan_reference",
            _validate_text(self.plan_reference, "preflight.plan_reference", MAX_TEXT_LENGTH),
        )
        if not isinstance(self.decision_sheet, DecisionSheet):
            raise PreflightError("preflight.decision_sheet must be a DecisionSheet")
        object.__setattr__(
            self,
            "budget_limit_units",
            _require_bounded_int(
                self.budget_limit_units, "preflight.budget_limit_units", maximum=MAX_UNITS_INPUT
            ),
        )
        object.__setattr__(
            self,
            "budget_spent_units",
            _require_bounded_int(
                self.budget_spent_units, "preflight.budget_spent_units", maximum=MAX_UNITS_INPUT
            ),
        )
        object.__setattr__(
            self,
            "wall_clock_limit_seconds",
            _require_bounded_int(
                self.wall_clock_limit_seconds,
                "preflight.wall_clock_limit_seconds",
                maximum=MAX_SECONDS_INPUT,
            ),
        )
        object.__setattr__(
            self,
            "wall_clock_elapsed_seconds",
            _require_bounded_int(
                self.wall_clock_elapsed_seconds,
                "preflight.wall_clock_elapsed_seconds",
                maximum=MAX_SECONDS_INPUT,
            ),
        )
        object.__setattr__(
            self,
            "authorized_paths",
            _canonical_repo_paths(self.authorized_paths, "preflight.authorized_paths"),
        )
        object.__setattr__(
            self,
            "worker_cap",
            _require_bounded_int(self.worker_cap, "preflight.worker_cap", maximum=MAX_UNITS_INPUT),
        )
        object.__setattr__(
            self,
            "active_workers",
            _require_bounded_int(
                self.active_workers, "preflight.active_workers", maximum=MAX_UNITS_INPUT
            ),
        )
        if isinstance(self.notification_channels, (str, bytes)) or not isinstance(
            self.notification_channels, Sequence
        ):
            raise PreflightError("preflight.notification_channels must be an ordered sequence")
        channels = tuple(
            _coerce_enum(NotificationChannel, channel, "preflight.notification_channels")
            for channel in self.notification_channels
        )
        if len(set(channels)) != len(channels):
            raise PreflightError("preflight.notification_channels must not repeat a channel")
        object.__setattr__(self, "notification_channels", channels)


def run_preflight(request: PreflightRequest) -> PreflightResult:
    """The deterministic readiness check that must pass before a start.

    Blockers (any one refuses the start): a sheet for another run, an unset or
    exhausted budget, an unset or exceeded wall-clock limit, no authorized scope,
    an unset or exceeded worker cap, or no alarm channel. An open or wildcard
    scope is refused even earlier, when the request is constructed, so it can
    never reach the preflight at all. Warnings never block; they tell the owner
    what is already nearly spent.
    """
    if not isinstance(request, PreflightRequest):
        raise PreflightError("run_preflight requires a PreflightRequest")
    blockers: list[PreflightBlocker] = []
    warnings: list[str] = []

    if request.decision_sheet.run_id != request.run_id:
        blockers.append(
            PreflightBlocker(
                code=PreflightBlockerCode.RUN_ID_MISMATCH,
                detail=(
                    f"decision sheet is for run {request.decision_sheet.run_id!r}, not "
                    f"{request.run_id!r}"
                ),
            )
        )
    if request.budget_limit_units <= 0:
        blockers.append(
            PreflightBlocker(
                code=PreflightBlockerCode.BUDGET_NOT_SET,
                detail="the owner must set a positive budget before the run starts",
            )
        )
    elif request.budget_spent_units > request.budget_limit_units:
        blockers.append(
            PreflightBlocker(
                code=PreflightBlockerCode.BUDGET_EXCEEDED,
                detail=(
                    f"budget spent {request.budget_spent_units} exceeds the limit "
                    f"{request.budget_limit_units}"
                ),
            )
        )
    if request.wall_clock_limit_seconds <= 0:
        blockers.append(
            PreflightBlocker(
                code=PreflightBlockerCode.WALL_CLOCK_NOT_SET,
                detail="the owner must set a positive wall-clock limit before the run starts",
            )
        )
    elif request.wall_clock_elapsed_seconds >= request.wall_clock_limit_seconds:
        blockers.append(
            PreflightBlocker(
                code=PreflightBlockerCode.WALL_CLOCK_EXCEEDED,
                detail=(
                    f"wall-clock elapsed {request.wall_clock_elapsed_seconds}s reaches the limit "
                    f"{request.wall_clock_limit_seconds}s"
                ),
            )
        )
    if len(request.authorized_paths) == 0:
        blockers.append(
            PreflightBlocker(
                code=PreflightBlockerCode.NO_AUTHORIZED_SCOPE,
                detail="the owner must authorize at least one explicit file scope",
            )
        )
    if len(request.notification_channels) == 0:
        blockers.append(
            PreflightBlocker(
                code=PreflightBlockerCode.NO_STALL_CHANNEL,
                detail="no alarm channel is configured; the run would be a silent stall",
            )
        )
    if request.worker_cap <= 0:
        blockers.append(
            PreflightBlocker(
                code=PreflightBlockerCode.WORKER_CAP_NOT_SET,
                detail="the owner must set a positive worker cap before the run starts",
            )
        )
    elif request.active_workers > request.worker_cap:
        blockers.append(
            PreflightBlocker(
                code=PreflightBlockerCode.WORKER_CAP_EXCEEDED,
                detail=(
                    f"{request.active_workers} workers are active, above the cap of "
                    f"{request.worker_cap}"
                ),
            )
        )
    elif request.active_workers == request.worker_cap:
        warnings.append("the worker cap is already fully occupied")

    if request.budget_limit_units > 0 and request.budget_spent_units * 100 >= (
        request.budget_limit_units * PREFLIGHT_WARN_PERCENT
    ):
        warnings.append(
            f"budget is at or above {PREFLIGHT_WARN_PERCENT}% consumed "
            f"({request.budget_spent_units}/{request.budget_limit_units} units)"
        )
    if request.wall_clock_limit_seconds > 0 and request.wall_clock_elapsed_seconds * 100 >= (
        request.wall_clock_limit_seconds * PREFLIGHT_WARN_PERCENT
    ):
        warnings.append(
            f"wall-clock is at or above {PREFLIGHT_WARN_PERCENT}% consumed "
            f"({request.wall_clock_elapsed_seconds}/{request.wall_clock_limit_seconds}s)"
        )

    return PreflightResult(
        run_id=request.run_id,
        ready=len(blockers) == 0,
        blockers=tuple(blockers),
        warnings=tuple(warnings),
    )


@dataclass(frozen=True)
class PlanTaskState:
    """One task of the plan graph: its scope and the scopes it depends on."""

    scope: DecisionScope
    depends_on: tuple[DecisionScope, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.scope, DecisionScope):
            raise AwayError("plan task scope must be a DecisionScope")
        if isinstance(self.depends_on, (str, bytes)) or not isinstance(self.depends_on, Sequence):
            raise AwayError("plan task depends_on must be an ordered sequence")
        dependencies = tuple(self.depends_on)
        if len(dependencies) > MAX_TASKS:
            raise AwayError("plan task has too many dependencies")
        seen: set[DecisionScope] = set()
        for index, dependency in enumerate(dependencies):
            if not isinstance(dependency, DecisionScope):
                raise AwayError(f"plan task depends_on[{index}] must be a DecisionScope")
            if dependency.matches_exactly(self.scope):
                raise AwayError("a plan task must not depend on itself")
            if dependency in seen:
                raise AwayError("plan task dependencies must not repeat")
            seen.add(dependency)
        object.__setattr__(self, "depends_on", dependencies)

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope.as_dict(),
            "depends_on": [dependency.as_dict() for dependency in self.depends_on],
        }


@dataclass(frozen=True)
class PlanImpact:
    """Which tasks a park/escalation holds and which keep running.

    ``plan_stalled`` is true only when something is held **and** nothing remains
    runnable, so a single never-delegable park never stalls an independent plan.
    """

    parked: tuple[DecisionScope, ...]
    escalated: tuple[DecisionScope, ...]
    held: tuple[DecisionScope, ...]
    runnable: tuple[DecisionScope, ...]
    plan_stalled: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "parked": [scope.as_dict() for scope in self.parked],
            "escalated": [scope.as_dict() for scope in self.escalated],
            "held": [scope.as_dict() for scope in self.held],
            "runnable": [scope.as_dict() for scope in self.runnable],
            "plan_stalled": self.plan_stalled,
        }


def _validate_task_graph(tasks: tuple[PlanTaskState, ...]) -> None:
    if len(tasks) > MAX_TASKS:
        raise AwayError(f"the plan has {len(tasks)} tasks; the hard limit is {MAX_TASKS}")
    scopes: set[DecisionScope] = set()
    projects: set[str] = set()
    for task in tasks:
        if task.scope in scopes:
            raise AwayError(f"duplicate plan task scope {task.scope.as_dict()!r}")
        scopes.add(task.scope)
        projects.add(task.scope.project_id)
    if len(projects) > 1:
        raise AwayError(
            "compute_plan_impact models one project's task graph; mixed projects are refused"
        )
    for task in tasks:
        for dependency in task.depends_on:
            if dependency not in scopes:
                raise AwayError(
                    f"task {task.scope.task_id!r} depends on unknown scope "
                    f"{dependency.task_id!r}"
                )
    # Iterative DFS cycle detection with three colours.
    adjacency = {task.scope: task.depends_on for task in tasks}
    state: dict[DecisionScope, int] = {}
    for task in tasks:
        if state.get(task.scope, 0) != 0:
            continue
        stack: list[tuple[DecisionScope, int]] = [(task.scope, 0)]
        state[task.scope] = 1
        while stack:
            node, index = stack[-1]
            dependencies = adjacency[node]
            if index >= len(dependencies):
                state[node] = 2
                stack.pop()
                continue
            stack[-1] = (node, index + 1)
            neighbour = dependencies[index]
            colour = state.get(neighbour, 0)
            if colour == 1:
                raise AwayError("the plan task graph contains a dependency cycle")
            if colour == 0:
                state[neighbour] = 1
                stack.append((neighbour, 0))


def compute_plan_impact(
    tasks: Sequence[PlanTaskState],
    *,
    parked_scopes: Sequence[DecisionScope] = (),
    escalated_scopes: Sequence[DecisionScope] = (),
) -> PlanImpact:
    """Deterministically hold a park's dependents and report what still runs.

    A parked (never-delegable) or escalated scope holds itself and every
    transitive dependent; unrelated work stays runnable. Unknown scopes,
    duplicate tasks, mixed projects and cycles all fail closed instead of
    guessing.
    """
    if isinstance(tasks, (str, bytes)) or not isinstance(tasks, Sequence):
        raise AwayError("compute_plan_impact requires an ordered sequence of tasks")
    task_list: list[PlanTaskState] = []
    for index, task in enumerate(tasks):
        if not isinstance(task, PlanTaskState):
            raise AwayError(f"tasks[{index}] must be a PlanTaskState")
        task_list.append(task)
    ordered = tuple(task_list)
    _validate_task_graph(ordered)
    parked = _validate_scopes(parked_scopes, "parked_scopes")
    escalated = _validate_scopes(escalated_scopes, "escalated_scopes")
    known = {task.scope for task in ordered}
    for scope in (*parked, *escalated):
        if scope not in known:
            raise AwayError(
                f"scope {scope.task_id!r} is not part of the plan and cannot be held"
            )
    seeds = set(parked) | set(escalated)
    dependents: dict[DecisionScope, list[DecisionScope]] = {}
    for task in ordered:
        for dependency in task.depends_on:
            dependents.setdefault(dependency, []).append(task.scope)
    held = set(seeds)
    frontier = list(seeds)
    while frontier:
        current = frontier.pop()
        for dependent in dependents.get(current, []):
            if dependent not in held:
                held.add(dependent)
                frontier.append(dependent)
    runnable = tuple(task.scope for task in ordered if task.scope not in held)
    return PlanImpact(
        parked=parked,
        escalated=escalated,
        held=tuple(task.scope for task in ordered if task.scope in held),
        runnable=runnable,
        plan_stalled=len(held) > 0 and len(runnable) == 0,
    )


def _validate_scopes(value: object, label: str) -> tuple[DecisionScope, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise AwayError(f"{label} must be an ordered sequence of scopes")
    scopes: list[DecisionScope] = []
    for index, scope in enumerate(value):
        if not isinstance(scope, DecisionScope):
            raise AwayError(f"{label}[{index}] must be a DecisionScope")
        scopes.append(scope)
    if len(scopes) > MAX_SCOPES:
        raise AwayError(f"{label} has {len(scopes)} entries; the hard limit is {MAX_SCOPES}")
    if len(set(scopes)) != len(scopes):
        raise AwayError(f"{label} must not repeat a scope")
    return tuple(scopes)


@dataclass(frozen=True)
class StandingDecisionAction:
    """One action the run took under a standing decision, for the report."""

    decision_id: str
    standing_decision_id: str
    scope: DecisionScope
    owner_words: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "standing_decision_id": self.standing_decision_id,
            "scope": self.scope.as_dict(),
            "owner_words": self.owner_words,
        }


@dataclass(frozen=True)
class AcceptedCandidateRecord:
    """One candidate accepted alone as a local commit; never pushed."""

    decision_id: str
    candidate_id: str
    head_sha: str
    scope: DecisionScope
    local_commit_only: bool = True
    pushed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "candidate_id": self.candidate_id,
            "head_sha": self.head_sha,
            "scope": self.scope.as_dict(),
            "local_commit_only": self.local_commit_only,
            "pushed": self.pushed,
        }


@dataclass(frozen=True)
class ParkedBranchRecord:
    """One branch/task parked by a never-delegable decision."""

    decision_id: str
    scope: DecisionScope
    never_delegable: NeverDelegableKind
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "scope": self.scope.as_dict(),
            "never_delegable": self.never_delegable.value,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class EscalationRecord:
    """One unhandled decision escalated to the owner."""

    decision_id: str
    scope: DecisionScope
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "scope": self.scope.as_dict(),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class MorningReport:
    """What happened overnight: standing actions, accepts, parks, escalations."""

    run_id: str
    standing_decision_actions: tuple[StandingDecisionAction, ...]
    accepted_candidates: tuple[AcceptedCandidateRecord, ...]
    parked_branches: tuple[ParkedBranchRecord, ...]
    escalations: tuple[EscalationRecord, ...]
    stall_alarms: tuple[StallAlarm, ...]
    consecutive_failures: int
    circuit_open: bool
    halted: bool
    policy_version: str = UNATTENDED_PLANS_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "standing_decision_actions": [
                action.as_dict() for action in self.standing_decision_actions
            ],
            "accepted_candidates": [
                candidate.as_dict() for candidate in self.accepted_candidates
            ],
            "parked_branches": [parked.as_dict() for parked in self.parked_branches],
            "escalations": [escalation.as_dict() for escalation in self.escalations],
            "stall_alarms": [alarm.as_dict() for alarm in self.stall_alarms],
            "consecutive_failures": self.consecutive_failures,
            "circuit_open": self.circuit_open,
            "halted": self.halted,
            "policy_version": self.policy_version,
        }

    def canonical_json(self) -> str:
        return json.dumps(
            self.as_dict(), ensure_ascii=False, sort_keys=False, separators=(",", ":")
        )


def build_morning_report(
    *,
    run_id: str,
    resolutions: Sequence[DecisionResolution],
    decisions: Sequence[Decision] = (),
    progress: RunProgress | None = None,
    alarms: Sequence[StallAlarm] = (),
) -> MorningReport:
    """Summarise the run for the owner's morning read.

    ``decisions`` supplies the acceptance facts that a resolution carries only as
    an option string; when it is omitted the report still lists standing
    decisions, parks and escalations. The report never mutates a resolution and
    is byte-stable for identical input.
    """
    identifier = _validate_identifier(run_id, "report.run_id")
    if isinstance(resolutions, (str, bytes)) or not isinstance(resolutions, Sequence):
        raise AwayError("report resolutions must be an ordered sequence")
    if isinstance(decisions, (str, bytes)) or not isinstance(decisions, Sequence):
        raise AwayError("report decisions must be an ordered sequence")
    if isinstance(alarms, (str, bytes)) or not isinstance(alarms, Sequence):
        raise AwayError("report alarms must be an ordered sequence")
    by_decision: dict[str, Decision] = {}
    for index, decision in enumerate(decisions):
        if not isinstance(decision, Decision):
            raise AwayError(f"decisions[{index}] must be a Decision")
        by_decision[decision.decision_id] = decision

    standing_actions: list[StandingDecisionAction] = []
    accepted: list[AcceptedCandidateRecord] = []
    parked: list[ParkedBranchRecord] = []
    escalations: list[EscalationRecord] = []
    for index, resolved in enumerate(resolutions):
        if not isinstance(resolved, DecisionResolution):
            raise AwayError(f"resolutions[{index}] must be a DecisionResolution")
        if resolved.disposition is DecisionDisposition.APPLY_STANDING_DECISION:
            if resolved.applied_standing_decision_id is None or resolved.owner_words is None:
                raise AwayError(
                    "a standing-decision resolution must name the decision and the owner words"
                )
            standing_actions.append(
                StandingDecisionAction(
                    decision_id=resolved.decision_id,
                    standing_decision_id=resolved.applied_standing_decision_id,
                    scope=resolved.scope,
                    owner_words=resolved.owner_words,
                )
            )
        elif resolved.disposition is DecisionDisposition.AUTO_ACCEPT_LOCAL_COMMIT:
            source = by_decision.get(resolved.decision_id)
            if source is None or source.acceptance is None:
                raise AwayError(
                    "an accepted candidate needs its Decision with the CandidateAcceptance facts"
                )
            accepted.append(
                AcceptedCandidateRecord(
                    decision_id=resolved.decision_id,
                    candidate_id=source.acceptance.candidate_id,
                    head_sha=source.acceptance.head_sha,
                    scope=resolved.scope,
                )
            )
        elif resolved.disposition is DecisionDisposition.PARK_BRANCH:
            source = by_decision.get(resolved.decision_id)
            if source is None or source.never_delegable is None:
                raise AwayError(
                    "a parked branch needs its Decision with the never-delegable kind"
                )
            parked.append(
                ParkedBranchRecord(
                    decision_id=resolved.decision_id,
                    scope=resolved.scope,
                    never_delegable=_coerce_enum(
                        NeverDelegableKind, source.never_delegable, "decision.never_delegable"
                    ),
                    detail=resolved.explanation[-1] if resolved.explanation else "never-delegable",
                )
            )
        elif resolved.disposition is DecisionDisposition.ESCALATE_OWNER:
            escalations.append(
                EscalationRecord(
                    decision_id=resolved.decision_id,
                    scope=resolved.scope,
                    reason="; ".join(resolved.explanation[1:]) or "unhandled decision",
                )
            )

    alarm_list: list[StallAlarm] = []
    for index, alarm in enumerate(alarms):
        if not isinstance(alarm, StallAlarm):
            raise AwayError(f"alarms[{index}] must be a StallAlarm")
        alarm_list.append(alarm)
    state = progress if progress is not None else RunProgress()
    if not isinstance(state, RunProgress):
        raise AwayError("report progress must be a RunProgress or None")

    return MorningReport(
        run_id=identifier,
        standing_decision_actions=tuple(standing_actions),
        accepted_candidates=tuple(accepted),
        parked_branches=tuple(parked),
        escalations=tuple(escalations),
        stall_alarms=tuple(alarm_list),
        consecutive_failures=state.consecutive_failures,
        circuit_open=state.circuit_open,
        halted=state.halted,
    )


# --- The durable wiring: store, journal, return summary, dispatch gate ------

def get_standing_decisions_path() -> Path:
    """Resolve the standing-decision document (web and scheduler share it)."""
    override = os.environ.get(AWAY_STORE_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return owner_state_dir() / "unattended-decisions.json"


def get_away_journal_path() -> Path:
    """Resolve the append-only away journal."""
    override = os.environ.get(AWAY_JOURNAL_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return owner_state_dir() / "away-journal.jsonl"


def get_away_policy_path() -> Path:
    """Resolve the owner-authored away preflight policy."""
    override = os.environ.get(AWAY_POLICY_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return owner_state_dir() / "away-policy.json"


def project_scope_id(repo: str | Path) -> str:
    """Return a stable, identifier-safe scope id for one repository path.

    The scope identity is local to away mode.  It is derived from the
    canonical path so a grant written for ``--repo`` matches the dispatch of a
    job on the same repository, and two different checkouts never share it.
    """
    raw = os.path.realpath(Path(repo).expanduser())
    slug = re.sub(r"[^a-z0-9]+", "-", Path(raw).name.lower()).strip("-") or "repo"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return f"{slug}-{digest}"


#: The decision key a dispatch-time question is stored under.  A question key is
#: free text, while a standing decision key is a bounded identifier, so the
#: deterministic digest is the bridge the owner and the engine both compute.
_QUESTION_KEY_PREFIX = "question."
_QUESTION_KEY_DIGEST = 24

#: Question classes that map exactly onto one never-delegable kind.
def _question_never_delegable() -> dict[DecisionClass, NeverDelegableKind]:
    """Map each question class that has an exact never-delegable kind.

    A class without an exact correspondence is still owner-only or
    planner-answerable as the question queue already decides it; it is never
    widened here.
    """
    from saberops.questions import DecisionClass

    return {
        DecisionClass.PUSH: NeverDelegableKind.PUBLISHING,
        DecisionClass.BILLING_OR_POSTURE: NeverDelegableKind.BUDGET_CAP,
        DecisionClass.TEST_CHANGE_AGAINST_ADR: NeverDelegableKind.TEST_GATE,
        DecisionClass.DELETE_OUTSIDE_ENGINE_FOLDERS: NeverDelegableKind.DESTRUCTIVE_OPERATIONS,
    }


def decision_key_for_question(question_key: str) -> str:
    """Map a free-text question key onto a bounded unattended decision key."""
    if not isinstance(question_key, str) or not question_key.strip():
        raise AwayError("a question key is required")
    digest = hashlib.sha256(question_key.strip().encode("utf-8")).hexdigest()
    return f"{_QUESTION_KEY_PREFIX}{digest[:_QUESTION_KEY_DIGEST]}"


def never_delegable_for_question(
    decision_class: DecisionClass,
) -> NeverDelegableKind | None:
    """Return the exact never-delegable kind for a question class, if any."""
    return _question_never_delegable().get(decision_class)


def question_scope(question: OwnerQuestion, job_id: str, repo: str | Path) -> DecisionScope:
    """Build the exact scope a dispatch-time question belongs to."""
    del question  # the scope is the job's, never a wildcard from question text
    return DecisionScope(
        project_id=project_scope_id(repo),
        task_id=job_id,
    )


def _now_iso(now: datetime | None = None) -> str:
    moment = now if now is not None else datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat()


def _atomic_write(path: Path, payload: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temp.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return path


class StandingDecisionStore:
    """The owner-authored standing decisions the scheduler reads."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path if path is not None else get_standing_decisions_path()

    def _load_payload(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"version": STORE_VERSION, "standing_decisions": []}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AwayError(f"standing decisions at {self.path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise AwayError(f"standing decisions at {self.path}: top level must be an object")
        if payload.get("version") != STORE_VERSION:
            raise AwayError(
                f"standing decisions at {self.path}: version must be {STORE_VERSION}"
            )
        raw = payload.get("standing_decisions")
        if not isinstance(raw, list):
            raise AwayError(f"standing decisions at {self.path}: missing standing_decisions list")
        return payload

    def decisions(self) -> tuple[StandingDecision, ...]:
        """Load and validate every standing decision, failing closed."""
        payload = self._load_payload()
        decisions: list[StandingDecision] = []
        for index, entry in enumerate(payload["standing_decisions"]):
            if not isinstance(entry, dict):
                raise AwayError(
                    f"standing decisions at {self.path}: entry {index} must be an object"
                )
            scope_raw = entry.get("scope")
            if not isinstance(scope_raw, dict):
                raise AwayError(
                    f"standing decisions at {self.path}: entry {index} needs a scope object"
                )
            paths = scope_raw.get("paths") or ()
            if not isinstance(paths, (list, tuple)):
                raise AwayError(
                    f"standing decisions at {self.path}: entry {index} paths must be a list"
                )
            try:
                decisions.append(
                    StandingDecision(
                        standing_decision_id=str(entry.get("standing_decision_id") or ""),
                        scope=DecisionScope(
                            project_id=str(scope_raw.get("project_id") or ""),
                            task_id=str(scope_raw.get("task_id") or ""),
                            paths=tuple(str(item) for item in paths),
                        ),
                        decision_key=str(entry.get("decision_key") or ""),
                        owner_words=str(entry.get("owner_words") or ""),
                        answered_by=str(entry.get("answered_by") or ""),
                        granted_at_turn=entry.get("granted_at_turn"),
                        ttl_turns=entry.get("ttl_turns"),
                        expires_at=entry.get("expires_at"),
                    )
                )
            except ValueError as exc:
                raise AwayError(
                    f"standing decisions at {self.path}: entry {index}: {exc}"
                ) from exc
        return tuple(decisions)

    def sheet(self, run_id: str, plan_reference: str = "away dispatch") -> DecisionSheet:
        """Freeze the current decisions into one run's decision sheet."""
        return DecisionSheet(
            run_id=run_id,
            plan_reference=plan_reference or "away dispatch",
            standing_decisions=self.decisions(),
        )

    def add(self, decision: StandingDecision) -> Path:
        """Append one owner decision, refusing a duplicate id or scope/key pair."""
        if not isinstance(decision, StandingDecision):
            raise AwayError("add() requires a StandingDecision")
        decisions = list(self.decisions())
        if any(item.standing_decision_id == decision.standing_decision_id for item in decisions):
            raise AwayError(
                f"standing decision {decision.standing_decision_id!r} already exists"
            )
        for item in decisions:
            if (
                item.decision_key == decision.decision_key
                and item.scope.matches_exactly(decision.scope)
            ):
                raise AwayError(
                    "a standing decision already answers this exact scope and decision key; "
                    "revoke it first"
                )
        decisions.append(decision)
        # DecisionSheet re-validates the whole set (limits, duplicates, bounds).
        DecisionSheet(
            run_id="owner",
            plan_reference="owner standing decisions",
            standing_decisions=tuple(decisions),
        )
        payload = {
            "version": STORE_VERSION,
            "standing_decisions": [item.as_dict() for item in decisions],
        }
        return _atomic_write(self.path, json.dumps(payload, indent=2) + "\n")

    def remove(self, standing_decision_id: str) -> Path:
        """Revoke one standing decision; the owner is the only writer."""
        existing = self.decisions()
        decisions = [
            item for item in existing if item.standing_decision_id != standing_decision_id
        ]
        if len(decisions) == len(existing):
            raise AwayError(f"unknown standing decision {standing_decision_id!r}")
        payload = {
            "version": STORE_VERSION,
            "standing_decisions": [item.as_dict() for item in decisions],
        }
        return _atomic_write(self.path, json.dumps(payload, indent=2) + "\n")


def render_standing_decisions(
    store: StandingDecisionStore | None = None,
    *,
    now: datetime | None = None,
    current_turn: int = 0,
) -> list[dict[str, Any]]:
    """Render every decision with its live/expired state for CLI and web."""
    resolved_store = store if store is not None else StandingDecisionStore()
    instant = _now_iso(now)
    rows: list[dict[str, Any]] = []
    for decision in resolved_store.decisions():
        rows.append(
            {
                **decision.as_dict(),
                "live": decision.is_live(now=instant, current_turn=current_turn),
            }
        )
    return rows


@dataclass(frozen=True)
class AwayPolicy:
    """The owner-authored preflight facts an away run must satisfy."""

    budget_units: int
    wall_clock_seconds: int
    worker_cap: int
    paths: tuple[str, ...]
    set_at: str
    set_by: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": POLICY_VERSION,
            "budget_units": self.budget_units,
            "wall_clock_seconds": self.wall_clock_seconds,
            "worker_cap": self.worker_cap,
            "paths": list(self.paths),
            "set_at": self.set_at,
            "set_by": self.set_by,
        }




def _validated_policy_paths(paths: Sequence[object], *, where: str) -> tuple[str, ...]:
    """Canonicalise the authorized paths, or raise a plain, owner-actionable error.

    A policy is loaded through exactly this check, so it is enforced when the
    policy is *saved* too: a path that loads as unreadable (an absolute path,
    ``.``, a wildcard) must be refused at the moment the owner types it, never
    written and then silently unable to let any work start.
    """
    if not paths:
        raise AwayError(f"{where}: at least one authorized path is required")
    try:
        # DecisionScope validates canonical, wildcard-free repository paths.
        return DecisionScope(
            project_id="policy",
            task_id="policy",
            paths=tuple(str(item) for item in paths),
        ).paths
    except ValueError as exc:
        raise AwayError(
            f"{where}: authorized paths are repository-relative, for example 'src' "
            f"or 'README.md' (never an absolute path or '.'): {exc}"
        ) from exc


def load_away_policy(path: Path | None = None) -> AwayPolicy | None:
    """Load the owner-authored away policy, failing closed on corruption."""
    target = path if path is not None else get_away_policy_path()
    if not target.is_file():
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AwayError(f"away policy at {target}: {exc}") from exc
    if not isinstance(data, dict):
        raise AwayError(f"away policy at {target}: top level must be an object")
    if data.get("version") != POLICY_VERSION:
        raise AwayError(f"away policy at {target}: version must be {POLICY_VERSION}")
    raw_budget = data.get("budget_units")
    raw_wall = data.get("wall_clock_seconds")
    raw_cap = data.get("worker_cap")
    if (
        not isinstance(raw_budget, int)
        or isinstance(raw_budget, bool)
        or not isinstance(raw_wall, int)
        or isinstance(raw_wall, bool)
        or not isinstance(raw_cap, int)
        or isinstance(raw_cap, bool)
    ):
        raise AwayError(f"away policy at {target}: numeric limits are required")
    budget: int = raw_budget
    wall: int = raw_wall
    cap: int = raw_cap
    if budget <= 0 or wall <= 0 or cap <= 0:
        raise AwayError(f"away policy at {target}: every limit must be positive")
    raw_paths = data.get("paths")
    if not isinstance(raw_paths, list) or not raw_paths:
        raise AwayError(f"away policy at {target}: at least one authorized path is required")
    paths = _validated_policy_paths(raw_paths, where=f"away policy at {target}")
    set_by = data.get("set_by")
    if set_by != "owner":
        raise AwayError(f"away policy at {target}: only the owner authors an away policy")
    return AwayPolicy(
        budget_units=budget,
        wall_clock_seconds=wall,
        worker_cap=cap,
        paths=paths,
        set_at=_parse_instant(data.get("set_at"), "away policy set_at").isoformat(),
        set_by="owner",
    )


def save_away_policy(
    policy: AwayPolicy,
    *,
    path: Path | None = None,
) -> Path:
    """Persist one validated owner away policy."""
    if not isinstance(policy, AwayPolicy):
        raise AwayError("save_away_policy requires an AwayPolicy")
    if policy.set_by != "owner":
        raise AwayError("only the owner authors an away policy")
    _parse_instant(policy.set_at, "away policy set_at")
    # Validate exactly what ``load_away_policy`` will accept: a policy that
    # cannot be read back must never be written.
    paths = _validated_policy_paths(policy.paths, where="away policy")
    if paths != tuple(policy.paths):
        policy = replace(policy, paths=paths)
    target = path if path is not None else get_away_policy_path()
    return _atomic_write(target, json.dumps(policy.as_dict(), indent=2) + "\n")


class AwayJournal:
    """Append-only, fsync-durable record of what away mode did and why."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path if path is not None else get_away_journal_path()

    def records(self) -> list[dict[str, Any]]:
        """Read every record in durable order; a torn tail is ignored."""
        if not self.path.is_file():
            return []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise AwayError(f"away journal at {self.path}: {exc}") from exc
        records: list[dict[str, Any]] = []
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                # A process killed mid-append can leave one torn final line.
                if index == len(lines) - 1:
                    break
                raise AwayError(f"away journal at {self.path}: invalid record {index}") from None
            if not isinstance(record, dict):
                raise AwayError(f"away journal at {self.path}: record {index} is not an object")
            records.append(record)
        return records

    def append(self, record: dict[str, Any]) -> dict[str, Any]:
        """Durably append one record and return it (with its sequence number)."""
        prior = self.records()
        payload = {
            "version": JOURNAL_VERSION,
            "sequence": len(prior) + 1,
            "recorded_at": _now_iso(),
            **record,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
        descriptor = os.open(
            self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600
        )
        try:
            os.write(descriptor, line.encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return payload

    def record_resolution(
        self,
        *,
        run_id: str,
        question_id: str | None,
        question_key: str | None,
        decision: Decision,
        resolution: DecisionResolution,
        progress: RunProgress | None = None,
        job_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist one disposition together with its morning-report fragment."""
        report = build_morning_report(
            run_id=run_id,
            resolutions=(resolution,),
            decisions=(decision,),
            progress=progress,
        )
        return self.append(
            {
                "kind": "resolution",
                "run_id": run_id,
                "job_id": job_id,
                "question_id": question_id,
                "question_key": question_key,
                "decision": decision.as_dict(),
                "resolution": resolution.as_dict(),
                "report": report.as_dict(),
            }
        )

    def record_preflight(self, result: PreflightResult) -> dict[str, Any] | None:
        """Persist a preflight result, skipping an identical consecutive one."""
        prior = self.records()
        if prior:
            last = prior[-1]
            if last.get("kind") == "preflight" and last.get("result") == result.as_dict():
                return None
        return self.append({"kind": "preflight", "result": result.as_dict()})

    def record_plan_impact(
        self,
        *,
        project_id: str,
        tasks: Sequence[DecisionScope],
        parked: Sequence[DecisionScope],
        escalated: Sequence[DecisionScope],
    ) -> dict[str, Any]:
        """Persist the deterministic impact of this tick's parks."""
        impact = compute_plan_impact(
            [PlanTaskState(scope=scope) for scope in tasks],
            parked_scopes=parked,
            escalated_scopes=escalated,
        )
        return self.append(
            {
                "kind": "plan_impact",
                "project_id": project_id,
                "impact": impact.as_dict(),
            }
        )

    def _progress_records(self, project_id: str) -> list[dict[str, Any]]:
        return [
            record
            for record in self.records()
            if record.get("project_id") == project_id
            and record.get("kind") in {"outcome", "breaker_reset"}
        ]

    def progress(self, project_id: str) -> RunProgress:
        """Rebuild the consecutive-failure breaker state from the journal."""
        progress = RunProgress()
        for record in self._progress_records(project_id):
            if record.get("kind") == "breaker_reset":
                progress = RunProgress()
            elif record.get("passed") is True:
                progress = progress.after_task(passed=True)
            elif record.get("passed") is False:
                progress = progress.after_task(passed=False)
        return progress

    def record_outcome(
        self,
        *,
        project_id: str,
        task_id: str,
        passed: bool,
    ) -> dict[str, Any]:
        """Fold one finished task into the breaker; a halt is loud and durable."""
        before = self.progress(project_id)
        after = before.after_task(passed=passed)
        record = self.append(
            {
                "kind": "outcome",
                "project_id": project_id,
                "task_id": task_id,
                "passed": passed,
                "progress": after.as_dict(),
            }
        )
        if after.halted and not before.halted:
            self.append(
                {
                    "kind": "halt",
                    "project_id": project_id,
                    "task_id": task_id,
                    "reason": (
                        "Circuit breaker halted away work: three consecutive task failures. "
                        "Only the owner clears it with `orch away reset-breaker`."
                    ),
                    "progress": after.as_dict(),
                }
            )
        return record

    def reset_breaker(self, project_id: str, *, set_by: str = "owner") -> dict[str, Any]:
        """Owner-only clear of a halted breaker."""
        if set_by != "owner":
            raise AwayError("only the owner resets the away circuit breaker")
        return self.append({"kind": "breaker_reset", "project_id": project_id, "set_by": set_by})

    def halted(self, project_id: str) -> bool:
        """True while the durable breaker is open for this project."""
        progress = self.progress(project_id)
        return progress.halted or progress.circuit_open


@dataclass(frozen=True)
class ReturnSummary:
    """While you were away: everything readable from the durable journal."""

    journal_path: str
    records: int
    revision: int | None
    last_recorded_at: str | None
    standing_decision_actions: tuple[dict[str, Any], ...]
    accepted_candidates: tuple[dict[str, Any], ...]
    parked_branches: tuple[dict[str, Any], ...]
    escalations: tuple[dict[str, Any], ...]
    stall_alarms: tuple[dict[str, Any], ...]
    halts: tuple[dict[str, Any], ...]
    plan_impact: dict[str, Any] | None
    preflight: dict[str, Any] | None
    plans: tuple[dict[str, Any], ...] = ()
    awaiting_acceptance: tuple[dict[str, Any], ...] = ()
    #: Every planning-session self-answer, read from the one kept record (H27).
    self_answered: tuple[dict[str, Any], ...] = ()

    @property
    def has_items(self) -> bool:
        return bool(
            self.standing_decision_actions
            or self.accepted_candidates
            or self.parked_branches
            or self.escalations
            or self.self_answered
            or self.stall_alarms
            or self.halts
            or self.awaiting_acceptance
            or any(
                plan.get("waiting_for_owner") or plan.get("parked")
                for plan in self.plans
            )
        )

    def one_line(self) -> str:
        """A bounded desktop-notice body derived only from the records."""
        parts = [
            f"{len(self.standing_decision_actions)} standing decision(s) applied",
            f"{len(self.parked_branches)} parked",
            f"{len(self.escalations)} escalated",
            f"{len(self.self_answered)} self-answered",
        ]
        if self.awaiting_acceptance:
            parts.append(
                f"{len(self.awaiting_acceptance)} candidate(s) awaiting acceptance"
            )
        waiting = sum(
            len(plan.get("waiting_for_owner") or ()) for plan in self.plans
        )
        if waiting:
            parts.append(f"{waiting} plan lane(s) waiting on you")
        if self.halts:
            parts.append("CIRCUIT BREAKER HALTED: 3 consecutive failures")
        return "; ".join(parts)

    def as_dict(self) -> dict[str, Any]:
        return {
            "v": JOURNAL_VERSION,
            "journal_path": self.journal_path,
            "records": self.records,
            "revision": self.revision,
            "last_recorded_at": self.last_recorded_at,
            "has_items": self.has_items,
            "standing_decision_actions": list(self.standing_decision_actions),
            "accepted_candidates": list(self.accepted_candidates),
            "parked_branches": list(self.parked_branches),
            "escalations": list(self.escalations),
            "stall_alarms": list(self.stall_alarms),
            "halts": list(self.halts),
            "plan_impact": self.plan_impact,
            "preflight": self.preflight,
            "plans": list(self.plans),
            "awaiting_acceptance": list(self.awaiting_acceptance),
            "self_answered": list(self.self_answered),
        }


def awaiting_acceptance(db: Any, *, limit: int = 1000) -> tuple[dict[str, Any], ...]:
    """Every SUCCEEDED job whose candidate still waits for the owner.

    The away journal records decisions, not jobs, so a fresh return summary
    that read only the journal omitted standalone away candidates (D17).  This
    projection is read-only and reuses the same candidate lifecycle and owner
    action the job surfaces show: the command named here is the exact one the
    owner can type.
    """
    import sqlite3

    from saberops.candidate_lifecycle import project_candidate_state
    from saberops.jobs.models import JobState
    from saberops.jobs.service import owner_action_view
    from saberops.jobs.store import list_jobs_tolerant
    from saberops.models import CandidateState

    terminal = {CandidateState.ACCEPTED, CandidateState.REJECTED, CandidateState.CLEANED}
    try:
        jobs, _ = list_jobs_tolerant(
            db, state=JobState.SUCCEEDED, limit=limit, quarantine=False
        )
    except sqlite3.Error:
        # A read-only store that has never held a job has no candidate to name.
        return ()
    entries: list[dict[str, Any]] = []
    for job in jobs:
        view = owner_action_view(db, job)
        run_id = view.get("run_id")
        if not run_id or not view.get("candidate_sha"):
            continue
        projection = project_candidate_state(db, str(run_id))
        if projection.candidate_sha is None or projection.state in terminal:
            continue
        entries.append(
            {
                "job_id": job.job_id,
                "run_id": run_id,
                "candidate_sha": projection.candidate_sha,
                "repo": job.repo,
                "goal": job.goal,
                "state": (
                    projection.state.value
                    if projection.state is not None
                    else None
                ),
                "reason": projection.reason,
                "owner_action": view.get("owner_action"),
            }
        )
    return tuple(entries)


def _self_answered_for_summary() -> tuple[dict[str, Any], ...]:
    """Read every planning-session self-answer from the one kept record (H27)."""
    from saberops.questions import OwnerQuestionQueue, QuestionStoreError

    try:
        decisions = OwnerQuestionQueue().orchestrator_alone_decisions()
    except (QuestionStoreError, OSError) as exc:
        raise AwayError(f"self-answered decisions unreadable: {exc}") from exc
    return tuple(item.to_mapping() for item in decisions)


def build_return_summary(
    journal: AwayJournal | None = None,
    *,
    plans: Sequence[dict[str, Any]] = (),
    db: Any | None = None,
) -> ReturnSummary:
    """Assemble the return summary from durable engine records.

    ``plans`` is the read-only plan section of the same summary (the projection
    behind ``STATE.md``): a plan is not a journal decision, but it is durable
    engine state a fresh session must be able to read after the planner closed.
    When ``db`` is available, standalone SUCCEEDED candidates that still wait
    for acceptance are projected too, so "while you were away" names the exact
    accept command instead of reporting only plan lanes.  Self-answered
    decisions come from the question audit list, the one kept record (H27).
    """
    resolved = journal if journal is not None else AwayJournal()
    records = resolved.records()
    standing: list[dict[str, Any]] = []
    accepted: list[dict[str, Any]] = []
    parked: list[dict[str, Any]] = []
    escalations: list[dict[str, Any]] = []
    alarms: list[dict[str, Any]] = []
    halts: list[dict[str, Any]] = []
    plan_impact: dict[str, Any] | None = None
    preflight: dict[str, Any] | None = None
    seen: set[tuple[str, str]] = set()
    for record in records:
        kind = record.get("kind")
        if kind == "resolution":
            report = record.get("report")
            if not isinstance(report, dict):
                continue
            for key, bucket in (
                ("standing_decision_actions", standing),
                ("accepted_candidates", accepted),
                ("parked_branches", parked),
                ("escalations", escalations),
                ("stall_alarms", alarms),
            ):
                for entry in report.get(key) or ():
                    marker = (key, json.dumps(entry, sort_keys=True))
                    if marker in seen:
                        continue
                    seen.add(marker)
                    bucket.append(entry)
        elif kind == "halt":
            halts.append(record)
        elif kind == "plan_impact":
            plan_impact = record.get("impact") or plan_impact
        elif kind == "preflight":
            preflight = record.get("result") or preflight
    last = records[-1] if records else None
    return ReturnSummary(
        journal_path=str(resolved.path),
        records=len(records),
        revision=int(last["sequence"]) if last is not None else None,
        last_recorded_at=str(last.get("recorded_at")) if last is not None else None,
        standing_decision_actions=tuple(standing),
        accepted_candidates=tuple(accepted),
        parked_branches=tuple(parked),
        escalations=tuple(escalations),
        stall_alarms=tuple(alarms),
        halts=tuple(halts),
        plan_impact=plan_impact,
        preflight=preflight,
        plans=tuple(dict(plan) for plan in plans),
        awaiting_acceptance=(awaiting_acceptance(db) if db is not None else ()),
        self_answered=_self_answered_for_summary(),
    )


def deliver_return_summary(summary: ReturnSummary, ledger: Path) -> bool:
    """Send the return summary once per changed digest; idempotent.

    Keying on the rendered one-line digest rather than the journal revision
    keeps a busy tick (a new preflight or plan-impact record each time) from
    sending an identical notice over and over.
    """
    if not summary.has_items:
        return False
    from saberops.notifications import notify_once

    notify_once(
        ledger,
        f"away-summary:{summary.one_line()}",
        "SaberOps: while you were away",
        summary.one_line(),
    )
    return True


def _preflight_for(
    *,
    policy: AwayPolicy | None,
    sheet: DecisionSheet,
    run_id: str,
    active_workers: int,
    journal: AwayJournal,
    now: datetime,
) -> PreflightResult:
    """Build and run the deterministic preflight from owner-authored facts."""
    if policy is None:
        # No owner-authored policy: every limit is unset, so the existing
        # preflight refuses rather than inventing a safe-looking default.
        request = PreflightRequest(
            run_id=run_id,
            plan_reference="away dispatch",
            decision_sheet=sheet,
            budget_limit_units=0,
            budget_spent_units=0,
            wall_clock_limit_seconds=0,
            wall_clock_elapsed_seconds=0,
            authorized_paths=(),
            worker_cap=0,
            active_workers=active_workers,
            notification_channels=(),
        )
        return run_preflight(request)
    set_at = _parse_instant(policy.set_at, "away policy set_at")
    elapsed = max(0, int((now - set_at).total_seconds()))
    # One autonomous decision is one budget unit.  Only decisions taken since
    # this policy was set count, so a fresh policy really starts a fresh window.
    spent = 0
    for record in journal.records():
        if record.get("kind") != "resolution":
            continue
        try:
            recorded = _parse_instant(record.get("recorded_at"), "journal recorded_at")
        except AwayError:
            continue
        if recorded >= set_at:
            spent += 1
    request = PreflightRequest(
        run_id=run_id,
        plan_reference="away dispatch",
        decision_sheet=sheet,
        budget_limit_units=policy.budget_units,
        budget_spent_units=spent,
        wall_clock_limit_seconds=policy.wall_clock_seconds,
        wall_clock_elapsed_seconds=elapsed,
        authorized_paths=policy.paths,
        worker_cap=policy.worker_cap,
        active_workers=active_workers,
        notification_channels=(NotificationChannel.LOG, NotificationChannel.WEB),
    )
    return run_preflight(request)


def record_preflight(
    result: PreflightResult,
    journal: AwayJournal | None = None,
) -> dict[str, Any] | None:
    """Durably record one preflight result (public scheduler hook)."""
    return (journal if journal is not None else AwayJournal()).record_preflight(result)


def record_plan_impact(
    *,
    project_id: str,
    tasks: Sequence[DecisionScope],
    parked: Sequence[DecisionScope],
    escalated: Sequence[DecisionScope] = (),
    journal: AwayJournal | None = None,
) -> dict[str, Any]:
    """Durably record the plan impact of this tick's parks."""
    return (journal if journal is not None else AwayJournal()).record_plan_impact(
        project_id=project_id,
        tasks=tasks,
        parked=parked,
        escalated=escalated,
    )


def record_job_outcome(
    *,
    project_id: str,
    task_id: str,
    passed: bool,
    journal: AwayJournal | None = None,
) -> dict[str, Any]:
    """Fold one finished task into the durable away circuit breaker."""
    return (journal if journal is not None else AwayJournal()).record_outcome(
        project_id=project_id,
        task_id=task_id,
        passed=passed,
    )


def answer_question_under_standing_decision(
    *,
    queue: OwnerQuestionQueue,
    question: OwnerQuestion,
    standing_decision_id: str,
    owner_words: str,
    now: str,
    evidence: str | None = None,
) -> None:
    """Record the owner's pre-authored words as the question's answer.

    The answer authority is the owner because the words are the owner's own,
    written verbatim in the standing decision.  The engine contributes no text.
    """
    from saberops.questions import AnswerAuthority, AskRole

    queue.answer(
        question.question_id,
        words=owner_words,
        authority=AnswerAuthority.OWNER,
        answered_by=f"standing-decision:{standing_decision_id}",
        answered_by_role=AskRole.OWNER,
        evidence=evidence,
        now=now,
    )


@dataclass(frozen=True)
class DispatchGate:
    """One dispatch-time away decision: allowed, or exactly why it is parked."""

    allowed: bool
    code: str | None
    reason: str | None
    details: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "code": self.code,
            "reason": self.reason,
            "details": self.details,
        }


def _blocked(code: str, reason: str, **details: Any) -> DispatchGate:
    return DispatchGate(allowed=False, code=code, reason=reason, details=details)


def evaluate_dispatch(
    *,
    job_id: str,
    repo: str | Path,
    project_id: str | None = None,
    plan_tasks: Sequence[DecisionScope] = (),
    active_workers: int = 0,
    now: datetime | None = None,
    queue: OwnerQuestionQueue | None = None,
    store: StandingDecisionStore | None = None,
    journal: AwayJournal | None = None,
    policy: AwayPolicy | None | object = ...,
    current_turn: int | None = None,
) -> DispatchGate:
    """Decide whether away mode may dispatch one job right now.

    Order, each step durable before the next: the circuit breaker, the
    deterministic preflight, then every open question blocking this job.  A
    live exact standing decision answers its question (the owner's words,
    verbatim); a never-delegable or unhandled decision parks **only this job**
    and is journalled, so independent work continues.
    """
    from saberops.questions import OwnerQuestionQueue, QuestionStatus

    moment = now if now is not None else datetime.now(UTC)
    instant = _now_iso(moment)
    resolved_store = store if store is not None else StandingDecisionStore()
    resolved_journal = journal if journal is not None else AwayJournal()
    resolved_queue = queue if queue is not None else OwnerQuestionQueue()
    resolved_project = project_id or project_scope_id(repo)
    resolved_policy: AwayPolicy | None | object = policy
    if policy is ...:
        try:
            resolved_policy = load_away_policy()
        except AwayError as exc:
            # An unreadable policy is the same fact as no policy: away dispatch
            # parks every job.  Report it once here instead of raising into the
            # scheduler tick, where it would repeat as a traceback every tick.
            logging.getLogger(__name__).warning("away policy is unreadable: %s", exc)
            resolved_policy = None

    if resolved_journal.halted(resolved_project):
        return _blocked(
            "circuit_open",
            "the away circuit breaker halted this project after three consecutive "
            "task failures; only the owner clears it with `orch away reset-breaker`",
            project_id=resolved_project,
        )

    sheet = resolved_store.sheet(job_id)
    preflight = _preflight_for(
        policy=resolved_policy if isinstance(resolved_policy, AwayPolicy) else None,
        sheet=sheet,
        run_id=job_id,
        active_workers=active_workers,
        journal=resolved_journal,
        now=moment,
    )
    record_preflight(preflight, resolved_journal)
    if not preflight.ready:
        return _blocked(
            "preflight",
            "the deterministic away preflight refused the start",
            blockers=[blocker.as_dict() for blocker in preflight.blockers],
            warnings=list(preflight.warnings),
        )

    turn = current_turn
    if turn is None:
        turn = sum(1 for record in resolved_journal.records() if record.get("kind") == "resolution")
    for question in resolved_queue.questions(status=QuestionStatus.OPEN):
        request = question.request
        if job_id not in request.blocks:
            continue
        decision = Decision(
            decision_id=question.question_id,
            scope=question_scope(question, job_id, repo),
            decision_key=decision_key_for_question(request.key),
            question=request.subject,
            never_delegable=never_delegable_for_question(request.decision_class),
        )
        # Away applies the decision sheet to questions.  Candidate acceptance is
        # a separate engine stage (F15 auto-accept) and never flows through this
        # question resolver, so an unanswered question still parks its branch.
        resolution = resolve_decision(
            sheet,
            decision,
            now=instant,
            current_turn=turn,
            authority_mode=AuthorityMode.SUPERVISED,
        )
        resolved_journal.record_resolution(
            run_id=job_id,
            job_id=job_id,
            question_id=question.question_id,
            question_key=request.key,
            decision=decision,
            resolution=resolution,
        )
        if resolution.disposition is DecisionDisposition.APPLY_STANDING_DECISION:
            if (
                resolution.owner_words is None
                or resolution.applied_standing_decision_id is None
            ):
                raise AwayError(
                    "a standing-decision resolution must carry the owner words and the id"
                )
            answer_question_under_standing_decision(
                queue=resolved_queue,
                question=question,
                standing_decision_id=resolution.applied_standing_decision_id,
                owner_words=resolution.owner_words,
                now=instant,
                evidence=(
                    f"standing decision {resolution.applied_standing_decision_id} "
                    f"for question {question.question_id}"
                ),
            )
            turn += 1
            continue
        # Never-delegable, expired or unhandled: park only this job.
        if plan_tasks:
            record_plan_impact(
                project_id=resolved_project,
                tasks=plan_tasks,
                parked=(decision.scope,),
                journal=resolved_journal,
            )
        return _blocked(
            "parked"
            if resolution.disposition is DecisionDisposition.PARK_BRANCH
            else "escalated",
            resolution.explanation[-1] if resolution.explanation else "decision needs the owner",
            project_id=resolved_project,
            question_id=question.question_id,
            decision_key=decision.decision_key,
            disposition=resolution.disposition.value,
            never_delegable=decision.never_delegable.value
            if decision.never_delegable is not None
            else None,
        )
    return DispatchGate(
        allowed=True,
        code=None,
        reason=None,
        details={"project_id": resolved_project},
    )
