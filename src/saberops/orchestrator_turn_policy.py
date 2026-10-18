"""Pure Orchestrator turn-admission and context-rotation policy (ADR-0004 D11).

This module owns exactly one bounded concern: given explicit, typed facts about
a single Orchestrator *decision point*, it returns a deterministic, explainable
*advice projection* for that turn. It is a **pure** module -- no I/O, no clock,
no randomness, no persistence, no provider or model launch, and no imports from
the session, runtime, journal, database or CLI layers.

Scope boundary (deliberately narrow):

* It is **not** a chat/session manager, a runtime controller, a model
  dispatcher, a scheduler, a durable turn journal or an owner DB schema.
* It does **not** own the session-ownership fence or the idempotent turn
  transaction. Those stay with the runtime. The returned action is advice only.
* It never starts a model, resumes a session, terminates a session or reads
  evidence. Evidence is supplied as *paths only* and is never opened here.

The frozen ADR-0004 D11 rules encoded here:

1. Rotation happens **only between turns** and only when measured context
   consumption is strictly **> 50 %**; at exactly 50 % the session is reused.
   With no resumable session the turn starts a new one.
2. Trustworthy usage is required to reason about budget. When usage is UNKNOWN
   the policy never assumes the existing session has ample budget: it takes a
   safe new session (default) or an explicit DEFER/BLOCKED, and never reuses.
3. A rotation requested while a turn is in progress must not touch another
   session; it returns BLOCKED or CONTINUE_EXACT_SESSION, clearly distinct from
   permission to start a fresh turn.
4. A failed turn replay uses the **same** decision-point idempotency key. At two
   failed attempts the turn escalates to OWNER_QUESTION -- never a silent third.
5. The brief is bounded and deterministic: ordered standing plan decisions,
   capped notebook, wave state with pinned exact SHAs and outcome tags, the
   current question, and *references only* to review/gate evidence. Over-cap
   input is truncated with disclosure or blocked, never summarised or inlined.
6. All inputs are strictly validated; malformed identifiers, negative token
   counts, zero totals, impossible ratios, conflicting wave identity, unbounded
   notebook/path lists, embedded content markers in notebook entries or evidence
   paths, and invalid SHA/outcome tags all fail closed.

This module intentionally uses immutable dataclasses and no mutable module
state so the same immutable input always yields byte-identical output.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

ORCHESTRATOR_TURN_POLICY_VERSION = "orchestrator-turn-policy/1"

#: Rotation happens only strictly above this share of the context window.
CONTEXT_ROTATION_THRESHOLD_PERCENT = 50
#: Two failed attempts for the *same* decision point escalate to the owner.
MAX_FAILED_ATTEMPTS_BEFORE_OWNER_QUESTION = 2

# --- Hard input bounds: exceeding these is malformed/unbounded, fail closed ---
MAX_IDENTIFIER_LENGTH = 128
MAX_FAILED_ATTEMPTS_INPUT = 10_000
MAX_STANDING_DECISIONS = 64
MAX_NOTEBOOK_ENTRIES = 512
MAX_EVIDENCE_PATHS = 512
MAX_TEXT_ITEM_LENGTH = 4_000
MAX_PATH_LENGTH = 1_024

# --- Brief bounds: within the hard bounds, cap the brief and disclose ---------
BRIEF_MAX_STANDING_DECISIONS = 16
BRIEF_MAX_NOTEBOOK_ENTRIES = 32
BRIEF_MAX_EVIDENCE_PATHS = 32
BRIEF_MAX_TEXT_ITEM_LENGTH = 320
BRIEF_MAX_MANDATORY_CHARS = 3_000
BRIEF_MAX_TOTAL_CHARS = 12_000

_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}\Z")
_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
_OUTCOME_TAG_RE = re.compile(r"[A-Z][A-Z0-9_]{0,31}\Z")
_CONTENT_MARKERS = ("\n", "\r", "\x00")


class TurnPolicyError(ValueError):
    """A caller-supplied fact is malformed or unbounded; policy fails closed."""


class BriefBudgetError(TurnPolicyError):
    """The mandatory brief content exceeds its budget; the turn is blocked."""


class DecisionKind(StrEnum):
    """The bounded set of Orchestrator decision points (ADR-0004 D11).

    These are the only points at which the engine wakes the Orchestrator. They
    are deliberately *not* arbitrary autonomous model calls.
    """

    PLAN_WAVE = "plan_wave"
    TRIAGE_REVIEW = "triage_review"
    G3_ROUTING = "g3_routing"
    WAVE_CLOSE = "wave_close"


class TurnAction(StrEnum):
    """Deterministic advice for one turn. Owned by the runtime, not applied here."""

    NEW_SESSION = "NEW_SESSION"
    REUSE_SESSION = "REUSE_SESSION"
    CONTINUE_EXACT_SESSION = "CONTINUE_EXACT_SESSION"
    DEFER = "DEFER"
    BLOCKED = "BLOCKED"
    OWNER_QUESTION = "OWNER_QUESTION"


class ReasonCode(StrEnum):
    """Stable, explainable reason for a :class:`TurnDecision`."""

    NEW_SESSION_NO_SESSION = "new_session_no_session"
    NEW_SESSION_ROTATED = "new_session_rotated_over_threshold"
    NEW_SESSION_CONTEXT_UNKNOWN = "new_session_context_unknown"
    REUSE_SESSION_WITHIN_BUDGET = "reuse_session_within_budget"
    REUSE_SESSION_AT_THRESHOLD = "reuse_session_at_threshold"
    CONTINUE_TURN_IN_PROGRESS = "continue_turn_in_progress"
    BLOCKED_ROTATION_MID_TURN = "blocked_rotation_mid_turn"
    BLOCKED_CONTEXT_UNKNOWN = "blocked_context_unknown"
    BLOCKED_BRIEF_BUDGET = "blocked_brief_budget_exceeded"
    DEFER_CONTEXT_UNKNOWN = "defer_context_unknown"
    OWNER_QUESTION_REPEATED_FAILURE = "owner_question_repeated_failure"


class UnknownContextPolicy(StrEnum):
    """What to do when usage is UNKNOWN and the session is resumable.

    The default is the safe option: never reuse on unknown usage. ``DEFER`` and
    ``BLOCK_WHEN_UNKNOWN`` are explicit alternatives for a caller that needs
    certainty before spending a turn.
    """

    PREFER_NEW_SESSION = "prefer_new_session"
    DEFER_WHEN_UNKNOWN = "defer_when_unknown"
    BLOCK_WHEN_UNKNOWN = "block_when_unknown"


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
    raise TurnPolicyError(f"{label} must be one of: {expected}")


def _require_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TurnPolicyError(f"{label} must be an integer")
    return value


def _require_bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise TurnPolicyError(f"{label} must be a boolean")
    return value


def _validate_identifier(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise TurnPolicyError(f"{label} must be a string")
    if len(value) > MAX_IDENTIFIER_LENGTH:
        raise TurnPolicyError(
            f"{label} exceeds the maximum length of {MAX_IDENTIFIER_LENGTH} characters"
        )
    if _IDENTIFIER_RE.fullmatch(value) is None:
        raise TurnPolicyError(
            f"{label} is malformed: expected an alphanumeric-leading token of "
            "[A-Za-z0-9._/-], got {value!r}"
        )
    return value


def _validate_sha(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA_RE.fullmatch(value) is None:
        raise TurnPolicyError(
            f"{label} must be an exact 40-character lowercase hex SHA (no abbreviation), "
            f"got {value!r}"
        )
    return value


def _validate_outcome_tag(value: object, label: str) -> str:
    if not isinstance(value, str) or _OUTCOME_TAG_RE.fullmatch(value) is None:
        raise TurnPolicyError(
            f"{label} is malformed: expected an uppercase token like 'G3_PASS', got {value!r}"
        )
    return value


def _optional_text(value: object, label: str, max_length: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TurnPolicyError(f"{label} must be a string or None")
    if value.strip() == "":
        raise TurnPolicyError(f"{label} must not be empty when provided")
    if len(value) > max_length:
        raise TurnPolicyError(f"{label} exceeds the maximum length of {max_length} characters")
    return value


def _validate_text_sequence(
    value: object, label: str, max_items: int, max_item_length: int
) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TurnPolicyError(f"{label} must be an ordered sequence of text entries")
    items = tuple(value)
    if len(items) > max_items:
        raise TurnPolicyError(
            f"{label} has {len(items)} entries; the hard limit is {max_items} (unbounded input "
            "refused)"
        )
    validated: list[str] = []
    for index, item in enumerate(items):
        if not isinstance(item, str):
            raise TurnPolicyError(f"{label}[{index}] must be a string")
        if item.strip() == "":
            raise TurnPolicyError(f"{label}[{index}] must not be empty")
        if len(item) > max_item_length:
            raise TurnPolicyError(
                f"{label}[{index}] exceeds the maximum length of {max_item_length} characters"
            )
        validated.append(item)
    return tuple(validated)


def _reject_content_markers(items: tuple[str, ...], label: str, message: str) -> tuple[str, ...]:
    for index, item in enumerate(items):
        if any(marker in item for marker in _CONTENT_MARKERS):
            raise TurnPolicyError(f"{label}[{index}] {message}")
    return items


def _validate_path_sequence(
    value: object, label: str, max_items: int, max_item_length: int
) -> tuple[str, ...]:
    items = _validate_text_sequence(value, label, max_items, max_item_length)
    return _reject_content_markers(
        items,
        label,
        "must be a path reference only, not file content (newlines and NUL are refused)",
    )


def _validate_notebook_sequence(
    value: object, label: str, max_items: int, max_item_length: int
) -> tuple[str, ...]:
    items = _validate_text_sequence(value, label, max_items, max_item_length)
    return _reject_content_markers(
        items,
        label,
        "must be a single-line notebook entry, not embedded file content "
        "(newlines, carriage returns and NUL are refused)",
    )


@dataclass(frozen=True)
class ContextUsage:
    """Trustworthy measured context consumption, or the caller omits it for UNKNOWN.

    ``used_tokens`` must be non-negative and ``total_tokens`` strictly positive,
    with ``used_tokens <= total_tokens``. Anything else is corrupt telemetry and
    fails closed; the policy never invents a ratio.
    """

    used_tokens: int
    total_tokens: int

    def __post_init__(self) -> None:
        used = _require_int(self.used_tokens, "context_usage.used_tokens")
        total = _require_int(self.total_tokens, "context_usage.total_tokens")
        if used < 0:
            raise TurnPolicyError("context_usage.used_tokens must not be negative")
        if total <= 0:
            raise TurnPolicyError("context_usage.total_tokens must be strictly positive")
        if used > total:
            raise TurnPolicyError(
                "context_usage.used_tokens must not exceed context_usage.total_tokens"
            )

    def exceeds_percent(self, percent: int) -> bool:
        """True iff consumption is *strictly* above ``percent`` (integer maths)."""
        return self.used_tokens * 100 > self.total_tokens * percent

    def at_percent(self, percent: int) -> bool:
        """True iff consumption is exactly ``percent`` (integer maths)."""
        return self.used_tokens * 100 == self.total_tokens * percent

    def as_dict(self) -> dict[str, int]:
        return {"used_tokens": self.used_tokens, "total_tokens": self.total_tokens}


@dataclass(frozen=True)
class WaveState:
    """Current wave state: pinned exact SHAs and an outcome tag, nothing more."""

    wave_id: str
    base_sha: str
    head_sha: str
    outcome_tag: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "wave_id", _validate_identifier(self.wave_id, "wave_state.wave_id")
        )
        object.__setattr__(self, "base_sha", _validate_sha(self.base_sha, "wave_state.base_sha"))
        object.__setattr__(self, "head_sha", _validate_sha(self.head_sha, "wave_state.head_sha"))
        object.__setattr__(
            self,
            "outcome_tag",
            _validate_outcome_tag(self.outcome_tag, "wave_state.outcome_tag"),
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "wave_id": self.wave_id,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "outcome_tag": self.outcome_tag,
        }


@dataclass(frozen=True)
class Brief:
    """A bounded, deterministic, fact-only turn briefing.

    Evidence appears as ``evidence_paths`` *references only*; the policy never
    embeds file content, logs, diffs or transcripts.
    """

    plan_reference: str | None
    standing_decisions: tuple[str, ...]
    notebook: tuple[str, ...]
    wave_state: WaveState | None
    question: str | None
    evidence_paths: tuple[str, ...]
    truncations: tuple[str, ...]
    policy_version: str = ORCHESTRATOR_TURN_POLICY_VERSION

    def as_dict(self) -> dict[str, Any]:
        """Ordered, JSON-ready projection with a fixed key order."""
        return {
            "plan_reference": self.plan_reference,
            "standing_decisions": list(self.standing_decisions),
            "notebook": list(self.notebook),
            "wave_state": None if self.wave_state is None else self.wave_state.as_dict(),
            "question": self.question,
            "evidence_paths": list(self.evidence_paths),
            "truncations": list(self.truncations),
            "policy_version": self.policy_version,
        }

    def canonical_json(self) -> str:
        return json.dumps(
            self.as_dict(), ensure_ascii=False, sort_keys=False, separators=(",", ":")
        )


@dataclass(frozen=True)
class TurnDecision:
    """Deterministic advice projection. The runtime owns applying it."""

    action: TurnAction
    reason_code: ReasonCode
    decision_key: str
    rotation_required: bool
    rotation_refused: bool
    context_usage_known: bool
    used_tokens: int | None
    total_tokens: int | None
    brief: Brief | None
    explanation: tuple[str, ...]
    policy_version: str = ORCHESTRATOR_TURN_POLICY_VERSION

    def as_dict(self) -> dict[str, Any]:
        """Ordered, JSON-ready projection with a fixed key order."""
        return {
            "action": self.action.value,
            "reason_code": self.reason_code.value,
            "decision_key": self.decision_key,
            "rotation_required": self.rotation_required,
            "rotation_refused": self.rotation_refused,
            "context_usage_known": self.context_usage_known,
            "used_tokens": self.used_tokens,
            "total_tokens": self.total_tokens,
            "brief": None if self.brief is None else self.brief.as_dict(),
            "explanation": list(self.explanation),
            "policy_version": self.policy_version,
        }

    def canonical_json(self) -> str:
        return json.dumps(
            self.as_dict(), ensure_ascii=False, sort_keys=False, separators=(",", ":")
        )


@dataclass(frozen=True)
class TurnRequest:
    """All explicit facts for one decision point. Supplied by a later verifier.

    Every field is validated in :meth:`__post_init__`; malformed or unbounded
    input raises :class:`TurnPolicyError` (fail closed). No field accepts a
    credential, secret, environment value or arbitrary file body.
    """

    decision_point_id: str
    run_id: str
    wave_id: str
    kind: DecisionKind
    at_turn_boundary: bool
    session_resumable: bool
    context_usage: ContextUsage | None = None
    failed_attempts: int = 0
    rotation_requested: bool = False
    unknown_context_policy: UnknownContextPolicy = UnknownContextPolicy.PREFER_NEW_SESSION
    plan_reference: str | None = None
    standing_decisions: Sequence[str] = ()
    notebook: Sequence[str] = ()
    wave_state: WaveState | None = None
    question: str | None = None
    evidence_paths: Sequence[str] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "decision_point_id",
            _validate_identifier(self.decision_point_id, "decision_point_id"),
        )
        object.__setattr__(self, "run_id", _validate_identifier(self.run_id, "run_id"))
        object.__setattr__(self, "wave_id", _validate_identifier(self.wave_id, "wave_id"))
        object.__setattr__(self, "kind", _coerce_enum(DecisionKind, self.kind, "kind"))
        object.__setattr__(
            self, "at_turn_boundary", _require_bool(self.at_turn_boundary, "at_turn_boundary")
        )
        object.__setattr__(
            self, "session_resumable", _require_bool(self.session_resumable, "session_resumable")
        )
        object.__setattr__(
            self, "rotation_requested", _require_bool(self.rotation_requested, "rotation_requested")
        )
        object.__setattr__(
            self,
            "unknown_context_policy",
            _coerce_enum(
                UnknownContextPolicy, self.unknown_context_policy, "unknown_context_policy"
            ),
        )
        attempts = _require_int(self.failed_attempts, "failed_attempts")
        if attempts < 0:
            raise TurnPolicyError("failed_attempts must not be negative")
        if attempts > MAX_FAILED_ATTEMPTS_INPUT:
            raise TurnPolicyError(
                f"failed_attempts exceeds the hard limit of {MAX_FAILED_ATTEMPTS_INPUT}"
            )
        object.__setattr__(self, "failed_attempts", attempts)
        if self.context_usage is not None and not isinstance(self.context_usage, ContextUsage):
            raise TurnPolicyError("context_usage must be a ContextUsage or None (for UNKNOWN)")
        if self.wave_state is not None:
            if not isinstance(self.wave_state, WaveState):
                raise TurnPolicyError("wave_state must be a WaveState or None")
            if self.wave_state.wave_id != self.wave_id:
                raise TurnPolicyError(
                    "conflicting wave identity: wave_state.wave_id does not match wave_id"
                )
        object.__setattr__(
            self,
            "plan_reference",
            _optional_text(self.plan_reference, "plan_reference", MAX_TEXT_ITEM_LENGTH),
        )
        object.__setattr__(
            self, "question", _optional_text(self.question, "question", MAX_TEXT_ITEM_LENGTH)
        )
        object.__setattr__(
            self,
            "standing_decisions",
            _validate_text_sequence(
                self.standing_decisions,
                "standing_decisions",
                MAX_STANDING_DECISIONS,
                MAX_TEXT_ITEM_LENGTH,
            ),
        )
        object.__setattr__(
            self,
            "notebook",
            _validate_notebook_sequence(
                self.notebook, "notebook", MAX_NOTEBOOK_ENTRIES, MAX_TEXT_ITEM_LENGTH
            ),
        )
        object.__setattr__(
            self,
            "evidence_paths",
            _validate_path_sequence(
                self.evidence_paths, "evidence_paths", MAX_EVIDENCE_PATHS, MAX_PATH_LENGTH
            ),
        )

    @property
    def idempotency_key(self) -> str:
        """Stable key for the same decision point across replays."""
        return idempotency_key(self.decision_point_id, self.run_id, self.wave_id)


def idempotency_key(decision_point_id: str, run_id: str, wave_id: str) -> str:
    """Deterministic decision-point key; identical across failed-turn replays."""
    return f"{ORCHESTRATOR_TURN_POLICY_VERSION}:{run_id}:{wave_id}:{decision_point_id}"


def _append_disclosure(truncations: list[str], section: str) -> None:
    if section not in truncations:
        truncations.append(section)


def _cap_text_item(text: str, limit: int, section: str, truncations: list[str]) -> str:
    if len(text) <= limit:
        return text
    _append_disclosure(truncations, section)
    return text[:limit] + "…"


def _cap_sequence(
    items: Sequence[str], limit: int, section: str, truncations: list[str]
) -> list[str]:
    if len(items) > limit:
        _append_disclosure(truncations, section)
        items = items[:limit]
    return [
        _cap_text_item(item, BRIEF_MAX_TEXT_ITEM_LENGTH, section, truncations) for item in items
    ]


def _brief_size(
    plan_reference: str | None,
    standing: Sequence[str],
    notebook: Sequence[str],
    wave: WaveState | None,
    question: str | None,
    evidence: Sequence[str],
) -> int:
    total = 0
    for value in (plan_reference, question):
        total += len(value) if value is not None else 0
    if wave is not None:
        total += len(wave.wave_id) + len(wave.base_sha) + len(wave.head_sha) + len(wave.outcome_tag)
    total += sum(len(item) for item in standing)
    total += sum(len(item) for item in notebook)
    total += sum(len(item) for item in evidence)
    return total


def build_brief(request: TurnRequest) -> Brief:
    """Build a bounded, deterministic, fact-only brief for a turn.

    Mandatory content (the question and the pinned wave state) must fit
    ``BRIEF_MAX_MANDATORY_CHARS`` or the call raises :class:`BriefBudgetError`.
    Optional sections are capped by count and per-item length, then reduced
    deterministically (evidence, then notebook, then standing decisions) until
    the total fits ``BRIEF_MAX_TOTAL_CHARS``; every reduction is disclosed in
    ``truncations``. Evidence is retained as path references only.
    """
    if not isinstance(request, TurnRequest):
        raise TurnPolicyError("build_brief requires a TurnRequest")
    truncations: list[str] = []

    # The question is mandatory turn content: it is never silently truncated.
    # If it (with the wave state) exceeds the mandatory budget, the turn blocks.
    question = request.question
    plan_reference = (
        None
        if request.plan_reference is None
        else _cap_text_item(
            request.plan_reference, BRIEF_MAX_TEXT_ITEM_LENGTH, "plan_reference", truncations
        )
    )
    wave = request.wave_state
    mandatory_chars = len(question) if question is not None else 0
    if wave is not None:
        mandatory_chars += (
            len(wave.wave_id) + len(wave.base_sha) + len(wave.head_sha) + len(wave.outcome_tag)
        )
    if mandatory_chars > BRIEF_MAX_MANDATORY_CHARS:
        raise BriefBudgetError(
            "mandatory brief content (question and wave state) exceeds the budget of "
            f"{BRIEF_MAX_MANDATORY_CHARS} characters"
        )

    standing = _cap_sequence(
        request.standing_decisions, BRIEF_MAX_STANDING_DECISIONS, "standing_decisions", truncations
    )
    notebook = _cap_sequence(request.notebook, BRIEF_MAX_NOTEBOOK_ENTRIES, "notebook", truncations)
    evidence = _cap_sequence(
        request.evidence_paths, BRIEF_MAX_EVIDENCE_PATHS, "evidence_paths", truncations
    )

    # Deterministic reduction, disclosed, never a summary.
    while (
        _brief_size(plan_reference, standing, notebook, wave, question, evidence)
        > BRIEF_MAX_TOTAL_CHARS
    ):
        if evidence:
            evidence.pop()
            _append_disclosure(truncations, "evidence_paths")
        elif notebook:
            notebook.pop()
            _append_disclosure(truncations, "notebook")
        elif standing:
            standing.pop()
            _append_disclosure(truncations, "standing_decisions")
        else:  # pragma: no cover - mandatory budget already checked above
            raise BriefBudgetError("brief exceeds the total character budget")

    return Brief(
        plan_reference=plan_reference,
        standing_decisions=tuple(standing),
        notebook=tuple(notebook),
        wave_state=wave,
        question=question,
        evidence_paths=tuple(evidence),
        truncations=tuple(truncations),
    )


def _new_decision(
    request: TurnRequest,
    action: TurnAction,
    reason: ReasonCode,
    *,
    rotation_required: bool,
    rotation_refused: bool = False,
    brief: Brief | None,
    explanation: tuple[str, ...],
) -> TurnDecision:
    usage = request.context_usage
    return TurnDecision(
        action=action,
        reason_code=reason,
        decision_key=request.idempotency_key,
        rotation_required=rotation_required,
        rotation_refused=rotation_refused,
        context_usage_known=usage is not None,
        used_tokens=usage.used_tokens if usage is not None else None,
        total_tokens=usage.total_tokens if usage is not None else None,
        brief=brief,
        explanation=(f"{request.decision_point_id}: {reason.value}", *explanation),
    )


def _decision_with_brief(
    request: TurnRequest,
    action: TurnAction,
    reason: ReasonCode,
    *,
    rotation_required: bool,
    rotation_refused: bool = False,
    explanation: tuple[str, ...],
) -> TurnDecision:
    try:
        brief: Brief | None = build_brief(request)
    except BriefBudgetError as exc:
        return _new_decision(
            request,
            TurnAction.BLOCKED,
            ReasonCode.BLOCKED_BRIEF_BUDGET,
            rotation_required=False,
            brief=None,
            explanation=(str(exc),),
        )
    return _new_decision(
        request,
        action,
        reason,
        rotation_required=rotation_required,
        rotation_refused=rotation_refused,
        brief=brief,
        explanation=explanation,
    )


def decide_turn(request: TurnRequest) -> TurnDecision:
    """Return the deterministic, explainable advice for one decision point.

    This is a pure projection: it never launches a model, resumes, terminates or
    persists anything. The runtime still holds the session-ownership fence and
    the idempotent turn transaction.
    """
    if not isinstance(request, TurnRequest):
        raise TurnPolicyError("decide_turn requires a TurnRequest")

    # 4. Repeated failure for the same decision point -> owner question, never a
    #    silent third attempt. The key stays identical for the replay.
    if request.failed_attempts >= MAX_FAILED_ATTEMPTS_BEFORE_OWNER_QUESTION:
        return _decision_with_brief(
            request,
            TurnAction.OWNER_QUESTION,
            ReasonCode.OWNER_QUESTION_REPEATED_FAILURE,
            rotation_required=False,
            explanation=(
                f"decision point failed {request.failed_attempts} times; escalating to the owner",
            ),
        )

    # 3. A turn is in progress: rotation is prohibited. A requested rotation is
    #    refused; otherwise the exact session continues. Neither is ever
    #    permission to start a fresh turn.
    if not request.at_turn_boundary:
        if request.rotation_requested:
            return _new_decision(
                request,
                TurnAction.BLOCKED,
                ReasonCode.BLOCKED_ROTATION_MID_TURN,
                rotation_required=False,
                rotation_refused=True,
                brief=None,
                explanation=("a turn is in progress; rotation is only allowed between turns",),
            )
        return _new_decision(
            request,
            TurnAction.CONTINUE_EXACT_SESSION,
            ReasonCode.CONTINUE_TURN_IN_PROGRESS,
            rotation_required=False,
            brief=None,
            explanation=("a turn is in progress; continue the exact session, do not rotate",),
        )

    # 1. No resumable session -> start a new one (nothing to rotate away from).
    if not request.session_resumable:
        return _decision_with_brief(
            request,
            TurnAction.NEW_SESSION,
            ReasonCode.NEW_SESSION_NO_SESSION,
            rotation_required=False,
            explanation=("no resumable session exists for this decision point",),
        )

    usage = request.context_usage

    # 2. Trustworthy usage: rotate strictly above 50 %, reuse otherwise (exactly
    #    50 % reuses).
    if usage is not None:
        if usage.exceeds_percent(CONTEXT_ROTATION_THRESHOLD_PERCENT):
            return _decision_with_brief(
                request,
                TurnAction.NEW_SESSION,
                ReasonCode.NEW_SESSION_ROTATED,
                rotation_required=True,
                explanation=(
                    "measured context use is strictly above "
                    f"{CONTEXT_ROTATION_THRESHOLD_PERCENT}%; rotate between turns",
                ),
            )
        at_threshold = usage.at_percent(CONTEXT_ROTATION_THRESHOLD_PERCENT)
        return _decision_with_brief(
            request,
            TurnAction.REUSE_SESSION,
            ReasonCode.REUSE_SESSION_AT_THRESHOLD
            if at_threshold
            else ReasonCode.REUSE_SESSION_WITHIN_BUDGET,
            rotation_required=False,
            explanation=(
                "measured context use is at the "
                f"{CONTEXT_ROTATION_THRESHOLD_PERCENT}% threshold; reuse the session"
                if at_threshold
                else "measured context use is within budget; reuse the session",
            ),
        )

    # 2 (continued). Usage UNKNOWN: never assume the existing session has ample
    #    budget. Take a safe new session, or DEFER/BLOCKED explicitly. Never
    #    reuse.
    if request.unknown_context_policy is UnknownContextPolicy.DEFER_WHEN_UNKNOWN:
        return _new_decision(
            request,
            TurnAction.DEFER,
            ReasonCode.DEFER_CONTEXT_UNKNOWN,
            rotation_required=False,
            brief=None,
            explanation=("context usage is unknown and the caller requires certainty; defer",),
        )
    if request.unknown_context_policy is UnknownContextPolicy.BLOCK_WHEN_UNKNOWN:
        return _new_decision(
            request,
            TurnAction.BLOCKED,
            ReasonCode.BLOCKED_CONTEXT_UNKNOWN,
            rotation_required=False,
            brief=None,
            explanation=("context usage is unknown and the caller forbids a new session; blocked",),
        )
    return _decision_with_brief(
        request,
        TurnAction.NEW_SESSION,
        ReasonCode.NEW_SESSION_CONTEXT_UNKNOWN,
        rotation_required=True,
        explanation=(
            "context usage is unknown; take the safe new session rather than assume budget",
        ),
    )


__all__ = [
    "BRIEF_MAX_EVIDENCE_PATHS",
    "BRIEF_MAX_MANDATORY_CHARS",
    "BRIEF_MAX_NOTEBOOK_ENTRIES",
    "BRIEF_MAX_STANDING_DECISIONS",
    "BRIEF_MAX_TEXT_ITEM_LENGTH",
    "BRIEF_MAX_TOTAL_CHARS",
    "CONTEXT_ROTATION_THRESHOLD_PERCENT",
    "MAX_EVIDENCE_PATHS",
    "MAX_FAILED_ATTEMPTS_BEFORE_OWNER_QUESTION",
    "MAX_FAILED_ATTEMPTS_INPUT",
    "MAX_IDENTIFIER_LENGTH",
    "MAX_NOTEBOOK_ENTRIES",
    "MAX_PATH_LENGTH",
    "MAX_STANDING_DECISIONS",
    "MAX_TEXT_ITEM_LENGTH",
    "Brief",
    "BriefBudgetError",
    "ContextUsage",
    "DecisionKind",
    "ORCHESTRATOR_TURN_POLICY_VERSION",
    "ReasonCode",
    "TurnAction",
    "TurnDecision",
    "TurnPolicyError",
    "TurnRequest",
    "UnknownContextPolicy",
    "WaveState",
    "build_brief",
    "decide_turn",
    "idempotency_key",
]
