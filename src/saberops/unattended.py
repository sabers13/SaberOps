"""Unattended plan execution policy (ADR-0004 D14, Step 017 / RD-1).

This module owns one bounded, deterministic concern: given explicit typed facts
about an **unattended plan run**, it decides *who may decide what*, *what must
never be delegated*, *whether the run may start*, *when a stall must shout*, and
*what the morning report says*. It is a **pure advice projection** -- no I/O, no
clock, no randomness, no persistence, no provider or model launch, and no
imports from the session, runtime, job, database, web, MCP or CLI layers.

Every instant and counter is supplied by the caller, so identical immutable
input yields byte-identical output.

The frozen ADR-0004 D14 rules encoded here:

1. **Decision sheet.** A run carries an owner-frozen :class:`DecisionSheet` of
   bounded :class:`StandingDecision` answers. A standing decision applies **only
   on an exact scope match** (project, task, paths) plus the exact decision key.
   There is no analogy, prefix match, pattern or wildcard anywhere: identifiers
   and scopes are validated to reject wildcard characters outright. Every
   standing decision is scoped, records the owner's exact words verbatim, and is
   bounded by an absolute ``expires_at`` and/or a turn ``ttl_turns``; at least
   one bound is mandatory, so no standing permission is unbounded.
2. **Never-delegable policy.** The never-delegable list is immutable software
   data (:data:`NEVER_DELEGABLE_KINDS`). A never-delegable decision parks **only
   its own branch/task** -- its dependents wait, independent work continues, and
   the plan is never stalled by a single park.
3. **Three autonomous decision classes.** Class 1 auto-accepts a candidate only
   when the gate passed, the cold review passed and the checklist is green; the
   only acceptance action that exists is a **local commit**, and the projection
   can never permit a push. Class 2 takes the Orchestrator's recommended option
   only when it exists with evidence and at least one rejected alternative, and
   journals them in the audit list. Class 3 parks never-delegable decisions on
   their own branch and escalates every other unhandled decision to the owner.
   No other authority mode delegates: a supervised run asks the owner for
   anything the sheet does not answer.
4. **Preflight and supervision.** :func:`run_preflight` verifies budget,
   wall-clock limit, authorized file scopes, worker caps and an alarm channel
   before execution starts, and a blocker prevents the start. The circuit
   breaker halts the plan after
   :data:`CONSECUTIVE_FAILURE_LIMIT` consecutive task failures.
   :func:`detect_stall` raises a loud, single-line alarm on heartbeat
   inactivity, a dead supervisor or stalled branches, through the configured
   channels; a run without a channel is refused as a silent stall. The morning
   report collects standing-decision actions, accepted candidates, parked
   branches, escalations and audit entries.

This module deliberately does **not** own the durable plan, the wave executor,
the scheduler, the notification transport, the owner-question queue, the audit
store or the candidate git operation. It returns deterministic decisions and
records; the runtime applies them under its own fence, and only the owner moves
``main`` or pushes.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final

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


class NeverDelegableKind(StrEnum):
    """The immutable never-delegable decision list (ADR-0004 D14).

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


class DecisionClass(StrEnum):
    """The three autonomous decision classes (ADR-0004 D14, roadmap RD-1)."""

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


class UnattendedError(ValueError):
    """A caller-supplied fact is malformed or unbounded; the policy fails closed."""


class DecisionSheetError(UnattendedError):
    """The decision sheet or one of its standing decisions is invalid."""


class PreflightError(UnattendedError):
    """The preflight input is malformed or unbounded."""


class StallSupervisionError(UnattendedError):
    """A run would be left without a loud alarm channel or a live supervisor."""


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
    raise UnattendedError(f"{label} must be one of: {expected}")


def _require_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise UnattendedError(f"{label} must be an integer")
    return value


def _require_bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise UnattendedError(f"{label} must be a boolean")
    return value


def _require_bounded_int(value: object, label: str, *, maximum: int, minimum: int = 0) -> int:
    number = _require_int(value, label)
    if number < minimum:
        raise UnattendedError(f"{label} must not be below {minimum}")
    if number > maximum:
        raise UnattendedError(f"{label} exceeds the hard limit of {maximum}")
    return number


def _validate_identifier(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise UnattendedError(f"{label} must be a string")
    if len(value) > MAX_IDENTIFIER_LENGTH:
        raise UnattendedError(
            f"{label} exceeds the maximum length of {MAX_IDENTIFIER_LENGTH} characters"
        )
    if any(character in value for character in _WILDCARD_CHARS):
        raise UnattendedError(
            f"{label} is malformed: wildcards and expansion characters are never "
            f"valid, got {value!r}"
        )
    if _IDENTIFIER_RE.fullmatch(value) is None:
        raise UnattendedError(
            f"{label} is malformed: expected an alphanumeric-leading token of "
            f"[A-Za-z0-9._/-], got {value!r}"
        )
    return value


def _validate_sha(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA_RE.fullmatch(value) is None:
        raise UnattendedError(
            f"{label} must be an exact 40-character lowercase hex SHA (no abbreviation), "
            f"got {value!r}"
        )
    return value


def _validate_text(value: object, label: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise UnattendedError(f"{label} must be a string")
    if value.strip() == "":
        raise UnattendedError(f"{label} must not be empty")
    if len(value) > max_length:
        raise UnattendedError(f"{label} exceeds the maximum length of {max_length} characters")
    if "\x00" in value or "\r" in value:
        raise UnattendedError(f"{label} must not contain NUL or carriage returns")
    return value


def _optional_text(value: object, label: str, max_length: int) -> str | None:
    if value is None:
        return None
    return _validate_text(value, label, max_length)


def _validate_text_sequence(
    value: object, label: str, max_items: int, max_item_length: int
) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise UnattendedError(f"{label} must be an ordered sequence of text entries")
    items = tuple(value)
    if len(items) > max_items:
        raise UnattendedError(
            f"{label} has {len(items)} entries; the hard limit is {max_items} (unbounded input "
            "refused)"
        )
    validated: list[str] = []
    for index, item in enumerate(items):
        validated.append(_validate_text(item, f"{label}[{index}]", max_item_length))
    return tuple(validated)


def _canonical_repo_path(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise UnattendedError(f"{label} must be a string")
    if any(character in value for character in _WILDCARD_CHARS):
        raise UnattendedError(
            f"{label} is malformed: a scope path is an exact repository-relative path and "
            f"never a wildcard or pattern, got {value!r}"
        )
    if any(marker in value for marker in _CONTENT_MARKERS):
        raise UnattendedError(
            f"{label} must be a path reference only, not file content "
            "(newlines, carriage returns and NUL are refused)"
        )
    if value.startswith("/") or "\\" in value:
        raise UnattendedError(f"{label} must be a repository-relative path, got {value!r}")
    if len(value) > MAX_PATH_LENGTH:
        raise UnattendedError(f"{label} exceeds the maximum length of {MAX_PATH_LENGTH} characters")
    if _PATH_RE.fullmatch(value) is None:
        raise UnattendedError(f"{label} is malformed, got {value!r}")
    if any(segment in _NON_CANONICAL_PATH_SEGMENTS for segment in value.split("/")):
        raise UnattendedError(
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
    if not isinstance(value, str):
        raise UnattendedError(f"{label} must be an ISO-8601 timestamp string")
    if len(value) > 64:
        raise UnattendedError(f"{label} exceeds the maximum length of 64 characters")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise UnattendedError(f"{label} is not a valid ISO-8601 timestamp: {error}") from error
    if parsed.tzinfo is None:
        raise UnattendedError(
            f"{label} must be timezone-aware (e.g. '2026-10-18T06:00:00+00:00'); a naive "
            "instant is ambiguous and is refused"
        )
    return parsed


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
            raise UnattendedError("matches_exactly requires a DecisionScope")
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
            raise UnattendedError("decision.scope must be a DecisionScope")
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
            raise UnattendedError("decision.acceptance must be a CandidateAcceptance or None")
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
            raise UnattendedError(
                "decision.alternatives requires decision.recommended_option; rejected alternatives "
                "belong to a recommended option"
            )
        if self.recommended_rationale is not None and self.recommended_option is None:
            raise UnattendedError(
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
    autonomy_class: DecisionClass | None
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
            raise UnattendedError(
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
    autonomy_class: DecisionClass | None,
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
        raise UnattendedError("resolve_decision requires a Decision")
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
            DecisionClass.CLASS_3_NEVER_DELEGABLE,
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
                    DecisionClass.CLASS_1_GATED_ACCEPT,
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
                DecisionClass.CLASS_2_RECOMMENDED,
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
        DecisionClass.CLASS_3_NEVER_DELEGABLE,
        explanation=tuple(reasons) or ("unhandled by the autonomous classes; ask the owner",),
    )


@dataclass(frozen=True)
class AutonomousAuditEntry:
    """One action the engine took alone, for the Orchestrator-alone audit list."""

    decision_id: str
    scope: DecisionScope
    autonomy_class: DecisionClass
    chosen_option: str
    rejected_alternatives: tuple[str, ...]
    evidence_paths: tuple[str, ...]
    explanation: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "scope": self.scope.as_dict(),
            "autonomy_class": self.autonomy_class.value,
            "chosen_option": self.chosen_option,
            "rejected_alternatives": list(self.rejected_alternatives),
            "evidence_paths": list(self.evidence_paths),
            "explanation": list(self.explanation),
        }


def build_audit_entries(
    resolutions: Sequence[DecisionResolution],
) -> tuple[AutonomousAuditEntry, ...]:
    """Collect the actions taken alone (Class 1 and Class 2) into an audit list.

    Only resolutions that required an audit are included; the caller persists
    them. The order is the caller's order, so the list is reproducible.
    """
    if isinstance(resolutions, (str, bytes)) or not isinstance(resolutions, Sequence):
        raise UnattendedError("build_audit_entries requires an ordered sequence")
    entries: list[AutonomousAuditEntry] = []
    for index, resolved in enumerate(resolutions):
        if not isinstance(resolved, DecisionResolution):
            raise UnattendedError(f"resolutions[{index}] must be a DecisionResolution")
        if not resolved.audit_required:
            continue
        if resolved.autonomy_class is None or resolved.chosen_option is None:
            raise UnattendedError(
                "an audit-required resolution must name an autonomy class and a chosen option"
            )
        entries.append(
            AutonomousAuditEntry(
                decision_id=resolved.decision_id,
                scope=resolved.scope,
                autonomy_class=resolved.autonomy_class,
                chosen_option=resolved.chosen_option,
                rejected_alternatives=resolved.rejected_alternatives,
                evidence_paths=resolved.evidence_paths,
                explanation=resolved.explanation,
            )
        )
    return tuple(entries)


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
            raise UnattendedError("observation.stalled_scopes must be an ordered sequence")
        scopes = tuple(self.stalled_scopes)
        if len(scopes) > MAX_SCOPES:
            raise UnattendedError(
                f"observation.stalled_scopes has {len(scopes)} entries; the hard limit is "
                f"{MAX_SCOPES}"
            )
        for index, scope in enumerate(scopes):
            if not isinstance(scope, DecisionScope):
                raise UnattendedError(
                    f"observation.stalled_scopes[{index}] must be a DecisionScope"
                )
        object.__setattr__(self, "stalled_scopes", scopes)
        if isinstance(self.channels, (str, bytes)) or not isinstance(self.channels, Sequence):
            raise UnattendedError("observation.channels must be an ordered sequence")
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
            raise UnattendedError("observation.channels must not repeat a channel")
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
            raise UnattendedError("plan task scope must be a DecisionScope")
        if isinstance(self.depends_on, (str, bytes)) or not isinstance(self.depends_on, Sequence):
            raise UnattendedError("plan task depends_on must be an ordered sequence")
        dependencies = tuple(self.depends_on)
        if len(dependencies) > MAX_TASKS:
            raise UnattendedError("plan task has too many dependencies")
        seen: set[DecisionScope] = set()
        for index, dependency in enumerate(dependencies):
            if not isinstance(dependency, DecisionScope):
                raise UnattendedError(f"plan task depends_on[{index}] must be a DecisionScope")
            if dependency.matches_exactly(self.scope):
                raise UnattendedError("a plan task must not depend on itself")
            if dependency in seen:
                raise UnattendedError("plan task dependencies must not repeat")
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
        raise UnattendedError(f"the plan has {len(tasks)} tasks; the hard limit is {MAX_TASKS}")
    scopes: set[DecisionScope] = set()
    projects: set[str] = set()
    for task in tasks:
        if task.scope in scopes:
            raise UnattendedError(f"duplicate plan task scope {task.scope.as_dict()!r}")
        scopes.add(task.scope)
        projects.add(task.scope.project_id)
    if len(projects) > 1:
        raise UnattendedError(
            "compute_plan_impact models one project's task graph; mixed projects are refused"
        )
    for task in tasks:
        for dependency in task.depends_on:
            if dependency not in scopes:
                raise UnattendedError(
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
                raise UnattendedError("the plan task graph contains a dependency cycle")
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
        raise UnattendedError("compute_plan_impact requires an ordered sequence of tasks")
    task_list: list[PlanTaskState] = []
    for index, task in enumerate(tasks):
        if not isinstance(task, PlanTaskState):
            raise UnattendedError(f"tasks[{index}] must be a PlanTaskState")
        task_list.append(task)
    ordered = tuple(task_list)
    _validate_task_graph(ordered)
    parked = _validate_scopes(parked_scopes, "parked_scopes")
    escalated = _validate_scopes(escalated_scopes, "escalated_scopes")
    known = {task.scope for task in ordered}
    for scope in (*parked, *escalated):
        if scope not in known:
            raise UnattendedError(
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
        raise UnattendedError(f"{label} must be an ordered sequence of scopes")
    scopes: list[DecisionScope] = []
    for index, scope in enumerate(value):
        if not isinstance(scope, DecisionScope):
            raise UnattendedError(f"{label}[{index}] must be a DecisionScope")
        scopes.append(scope)
    if len(scopes) > MAX_SCOPES:
        raise UnattendedError(f"{label} has {len(scopes)} entries; the hard limit is {MAX_SCOPES}")
    if len(set(scopes)) != len(scopes):
        raise UnattendedError(f"{label} must not repeat a scope")
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
    audit_entries: tuple[AutonomousAuditEntry, ...]
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
            "audit_entries": [entry.as_dict() for entry in self.audit_entries],
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
        raise UnattendedError("report resolutions must be an ordered sequence")
    if isinstance(decisions, (str, bytes)) or not isinstance(decisions, Sequence):
        raise UnattendedError("report decisions must be an ordered sequence")
    if isinstance(alarms, (str, bytes)) or not isinstance(alarms, Sequence):
        raise UnattendedError("report alarms must be an ordered sequence")
    by_decision: dict[str, Decision] = {}
    for index, decision in enumerate(decisions):
        if not isinstance(decision, Decision):
            raise UnattendedError(f"decisions[{index}] must be a Decision")
        by_decision[decision.decision_id] = decision

    standing_actions: list[StandingDecisionAction] = []
    accepted: list[AcceptedCandidateRecord] = []
    parked: list[ParkedBranchRecord] = []
    escalations: list[EscalationRecord] = []
    for index, resolved in enumerate(resolutions):
        if not isinstance(resolved, DecisionResolution):
            raise UnattendedError(f"resolutions[{index}] must be a DecisionResolution")
        if resolved.disposition is DecisionDisposition.APPLY_STANDING_DECISION:
            if resolved.applied_standing_decision_id is None or resolved.owner_words is None:
                raise UnattendedError(
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
                raise UnattendedError(
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
                raise UnattendedError(
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
            raise UnattendedError(f"alarms[{index}] must be a StallAlarm")
        alarm_list.append(alarm)
    state = progress if progress is not None else RunProgress()
    if not isinstance(state, RunProgress):
        raise UnattendedError("report progress must be a RunProgress or None")

    return MorningReport(
        run_id=identifier,
        standing_decision_actions=tuple(standing_actions),
        accepted_candidates=tuple(accepted),
        parked_branches=tuple(parked),
        escalations=tuple(escalations),
        audit_entries=build_audit_entries(resolutions),
        stall_alarms=tuple(alarm_list),
        consecutive_failures=state.consecutive_failures,
        circuit_open=state.circuit_open,
        halted=state.halted,
    )


__all__ = [
    "AlarmSeverity",
    "AcceptedCandidateRecord",
    "AuthorityMode",
    "AutonomousAuditEntry",
    "CONSECUTIVE_FAILURE_LIMIT",
    "CandidateAcceptance",
    "Decision",
    "DecisionClass",
    "DecisionDisposition",
    "DecisionResolution",
    "DecisionScope",
    "DecisionSheet",
    "DecisionSheetError",
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
    "ParkedBranchRecord",
    "PlanImpact",
    "PlanTaskState",
    "PreflightBlocker",
    "PreflightBlockerCode",
    "PreflightError",
    "PreflightRequest",
    "PreflightResult",
    "PREFLIGHT_WARN_PERCENT",
    "RunProgress",
    "STALL_CRITICAL_MULTIPLIER",
    "StallAlarm",
    "StallObservation",
    "StallSupervisionError",
    "StandingDecision",
    "StandingDecisionAction",
    "UNATTENDED_PLANS_VERSION",
    "UnattendedError",
    "build_audit_entries",
    "build_morning_report",
    "compute_plan_impact",
    "detect_stall",
    "is_never_delegable",
    "never_delegable_description",
    "resolve_decision",
    "run_preflight",
]
