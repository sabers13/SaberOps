"""Bounded Reviewer Authority: pure review-policy core (Slice 3C Stage 1).

This module owns the structured review protocol: the finding classification
taxonomy, strict JSON parsing with fail-closed errors, per-classification
evidence validation, disposition computation, deterministic risk and
design-readiness assessment, and the bounded review-round machine. It is a
pure module: no I/O, no adapters, no clock, no randomness. Every decision is
computed from structured inputs so contradictions between reviewer prose and
enforced disposition are impossible by construction. Persistence-facing
integration into the review engine and orchestrator loop lands in later
stages.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, NamedTuple

REVIEW_POLICY_VERSION = "review-policy/1"


class ReviewProtocolError(Exception):
    """Raised when reviewer output violates the structured review protocol."""


class FindingClassification(StrEnum):
    """Reviewer finding taxonomy for bounded reviewer authority.

    ``B1``/``B2``/``B3`` are blocking classifications; ``D1`` returns the work
    to design; ``N1`` is an explicitly non-blocking improvement note.
    """

    CONTRACT_VIOLATION = "B1"
    CORRECTNESS_OR_SAFETY_DEFECT = "B2"
    MISSING_REQUIRED_EVIDENCE = "B3"
    DESIGN_QUESTION = "D1"
    NON_BLOCKING_IMPROVEMENT = "N1"


BLOCKING_CLASSIFICATIONS: frozenset[FindingClassification] = frozenset(
    {
        FindingClassification.CONTRACT_VIOLATION,
        FindingClassification.CORRECTNESS_OR_SAFETY_DEFECT,
        FindingClassification.MISSING_REQUIRED_EVIDENCE,
    }
)


class RepairScope(StrEnum):
    """Scope of correction a finding requires."""

    LOCAL_MECHANICAL = "LOCAL_MECHANICAL"
    DESIGN_AFFECTING = "DESIGN_AFFECTING"


class ReviewPolicyVerdict(StrEnum):
    """Disposition verdict enforced by policy, never trusted from prose."""

    PASS = "PASS"
    BLOCK = "BLOCK"
    DESIGN_RETURN = "DESIGN_RETURN"
    PASS_WITH_BACKLOG = "PASS_WITH_BACKLOG"
    FINAL_BLOCK = "FINAL_BLOCK"


SEVERITY_VALUES: tuple[str, ...] = ("critical", "high", "medium", "low")


@dataclass(frozen=True)
class StructuredFinding:
    """One structured reviewer finding in the strict protocol schema."""

    id: str
    classification: FindingClassification
    title: str
    severity: str
    contract_reference: str | None
    invariant_or_requirement: str
    code_location: str | None
    failure_scenario: str
    evidence: str
    minimum_correction: str
    repair_scope: RepairScope

    @property
    def blocking(self) -> bool:
        """True iff the classification is B1, B2, or B3."""
        return self.classification in BLOCKING_CLASSIFICATIONS


class ParsedStructuredReview(NamedTuple):
    """Result of :func:`parse_structured_review`; unpacks as a tuple."""

    verdict: ReviewPolicyVerdict
    summary: str
    findings: tuple[StructuredFinding, ...]
    policy_version: str


_CLASSIFICATION_ALIASES: dict[str, FindingClassification] = {
    token.upper(): member
    for member in FindingClassification
    for token in (member.value, member.name)
}
_REPAIR_SCOPE_ALIASES: dict[str, RepairScope] = {
    token.upper(): member for member in RepairScope for token in (member.value, member.name)
}
_POLICY_VERDICT_ALIASES: dict[str, ReviewPolicyVerdict] = {
    token.upper(): member for member in ReviewPolicyVerdict for token in (member.value, member.name)
}

_FENCED_JSON_PATTERN = re.compile(
    r"```(?:[A-Za-z0-9_+-]+)?[ \t]*\r?\n?(?P<body>.*?)\r?\n?[ \t]*```",
    re.DOTALL,
)


def _strip_code_fence(raw_text: str) -> str:
    """Return the JSON body, tolerating one fenced code block amid prose.

    The fence may carry any language tag (``json``, ``JSON``, ...); when the
    whole payload is wrapped in prose, the first fenced block is extracted.
    Text without a fence is returned unchanged so malformed payloads still
    fail closed.
    """
    text = raw_text.strip()
    match = _FENCED_JSON_PATTERN.search(text)
    return match.group("body") if match is not None else text


def _coerce_verdict(raw: object) -> ReviewPolicyVerdict:
    token = str(raw).strip().upper()
    member = _POLICY_VERDICT_ALIASES.get(token)
    if member is None:
        raise ReviewProtocolError(f"unknown verdict: {raw!r}")
    return member


def _coerce_classification(raw: object) -> FindingClassification:
    token = str(raw).strip().upper()
    member = _CLASSIFICATION_ALIASES.get(token)
    if member is None:
        raise ReviewProtocolError(f"unknown classification: {raw!r}")
    return member


def _coerce_repair_scope(raw: object) -> RepairScope:
    token = str(raw).strip().upper()
    member = _REPAIR_SCOPE_ALIASES.get(token)
    if member is None:
        raise ReviewProtocolError(f"unknown repair_scope: {raw!r}")
    return member


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ReviewProtocolError("optional string field must be a string or null")
    stripped = value.strip()
    return stripped if stripped else None


def _require_str(item: dict[str, Any], key: str, label: str) -> str:
    value = item[key]
    if not isinstance(value, str):
        raise ReviewProtocolError(f"{label} field '{key}' must be a string")
    return value


def _parse_finding(item: object, index: int) -> StructuredFinding:
    if not isinstance(item, dict):
        raise ReviewProtocolError(f"finding #{index} must be a JSON object")
    required = (
        "id",
        "classification",
        "title",
        "severity",
        "invariant_or_requirement",
        "failure_scenario",
        "evidence",
        "minimum_correction",
        "repair_scope",
    )
    for key in required:
        if key not in item:
            raise ReviewProtocolError(f"finding #{index} missing required field: {key}")
    raw_id = item["id"]
    if not isinstance(raw_id, str) or not raw_id.strip():
        raise ReviewProtocolError(f"finding #{index} field 'id' must be a non-empty string")
    title = _require_str(item, "title", f"finding '{raw_id}'")
    if not title.strip():
        raise ReviewProtocolError(f"finding '{raw_id}' field 'title' must be non-empty")
    classification = _coerce_classification(item["classification"])
    severity_raw = item["severity"]
    if not isinstance(severity_raw, str) or severity_raw.strip().lower() not in SEVERITY_VALUES:
        raise ReviewProtocolError(
            f"finding '{raw_id}' has invalid severity: {severity_raw!r}; "
            f"expected one of {SEVERITY_VALUES}"
        )
    return StructuredFinding(
        id=raw_id.strip(),
        classification=classification,
        title=title,
        severity=severity_raw.strip().lower(),
        contract_reference=_optional_string(item.get("contract_reference")),
        invariant_or_requirement=_require_str(
            item, "invariant_or_requirement", f"finding '{raw_id}'"
        ),
        code_location=_optional_string(item.get("code_location")),
        failure_scenario=_require_str(item, "failure_scenario", f"finding '{raw_id}'"),
        evidence=_require_str(item, "evidence", f"finding '{raw_id}'"),
        minimum_correction=_require_str(item, "minimum_correction", f"finding '{raw_id}'"),
        repair_scope=_coerce_repair_scope(item["repair_scope"]),
    )


def parse_structured_review(
    raw_text: str, *, is_final_round: bool = False
) -> ParsedStructuredReview:
    """Parse strict structured reviewer output into policy objects.

    Accepts a strict JSON object with keys ``verdict``, ``summary``, and
    ``findings`` (each finding carrying id, classification, title, severity,
    contract_reference, invariant_or_requirement, code_location,
    failure_scenario, evidence, minimum_correction, repair_scope). One fenced
    code block with an optional language tag is tolerated, including when it
    is embedded in surrounding prose. Anything else raises
    :class:`ReviewProtocolError`. Every parsed finding must additionally pass
    per-classification evidence validation (:func:`validate_finding_evidence`);
    any reported violation raises :class:`ReviewProtocolError` naming it
    before any disposition is computed, so structurally-valid-but-evidence-
    invalid findings fail closed at this protocol boundary. The disposition
    is always recomputed here; a raw verdict field that contradicts the
    computed disposition fails closed.
    """
    candidate = _strip_code_fence(raw_text)
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ReviewProtocolError(f"malformed JSON in structured review: {exc}") from exc
    if not isinstance(data, dict):
        raise ReviewProtocolError("structured review payload must be a JSON object")
    for key in ("verdict", "summary", "findings"):
        if key not in data:
            raise ReviewProtocolError(f"structured review missing required field: {key}")
    verdict = _coerce_verdict(data["verdict"])
    summary = data["summary"]
    if not isinstance(summary, str):
        raise ReviewProtocolError("field 'summary' must be a string")
    raw_findings = data["findings"]
    if not isinstance(raw_findings, list):
        raise ReviewProtocolError("field 'findings' must be a list")
    findings: list[StructuredFinding] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw_findings):
        finding = _parse_finding(item, index)
        if finding.id in seen_ids:
            raise ReviewProtocolError(f"duplicate finding id: {finding.id}")
        seen_ids.add(finding.id)
        findings.append(finding)
    evidence_failures = [
        f"'{finding.id}': {', '.join(violations)}"
        for finding in findings
        if (violations := validate_finding_evidence(finding))
    ]
    if evidence_failures:
        raise ReviewProtocolError(
            f"finding evidence validation failed: {'; '.join(evidence_failures)}"
        )
    disposition = compute_disposition(findings, is_final_round=is_final_round)
    if verdict != disposition:
        raise ReviewProtocolError(
            f"contradictory review verdict: raw={verdict.value} computed={disposition.value}"
        )
    return ParsedStructuredReview(disposition, summary, tuple(findings), REVIEW_POLICY_VERSION)


def compute_disposition(
    findings: Sequence[StructuredFinding], *, is_final_round: bool
) -> ReviewPolicyVerdict:
    """Compute the enforced disposition from findings alone.

    No findings => ``PASS``; only N1 findings => ``PASS_WITH_BACKLOG``; any D1
    => ``DESIGN_RETURN``; any blocking finding without D1 => ``BLOCK``, mapped
    to ``FINAL_BLOCK`` on the final permitted round. Disposition is computed,
    never parsed from model prose, so contradictions are impossible by
    construction.
    """
    has_design_question = any(
        finding.classification is FindingClassification.DESIGN_QUESTION for finding in findings
    )
    if has_design_question:
        return ReviewPolicyVerdict.DESIGN_RETURN
    if not findings:
        return ReviewPolicyVerdict.PASS
    if not any(finding.blocking for finding in findings):
        return ReviewPolicyVerdict.PASS_WITH_BACKLOG
    return ReviewPolicyVerdict.FINAL_BLOCK if is_final_round else ReviewPolicyVerdict.BLOCK


_CONCURRENCY_TERMS: tuple[str, ...] = (
    "race",
    "races",
    "concurrent",
    "concurrently",
    "concurrency",
    "thread",
    "threads",
    "threaded",
    "threading",
    "multithreaded",
    "mutex",
    "deadlock",
    "livelock",
    "parallel",
    "parallelism",
    "interleave",
    "interleaves",
    "interleaved",
    "interleaving",
    "async",
    "asynchronous",
    "atomicity",
)

_ORDERING_MARKERS: tuple[str, ...] = (
    "while",
    "during",
    "then",
    "before",
    "after",
    "meanwhile",
    "simultaneous",
    "simultaneously",
    "sequentially",
    "subsequently",
)

_B3_MISSING_MARKERS: tuple[str, ...] = (
    "missing",
    "absent",
    "not provided",
    "not included",
    "unavailable",
)

_D1_DECISION_MARKERS: tuple[str, ...] = (
    "decision",
    "decide",
    "choose",
    "choice",
    "unresolved",
    "open question",
    "option",
    "alternative",
    "trade-off",
    "tradeoff",
    "either",
)

_N1_NON_BLOCKING_MARKERS: tuple[str, ...] = (
    "non-blocking",
    "non blocking",
    "non-blockingness",
    "not blocking",
    "does not block",
    "doesn't block",
    "backlog",
    "cosmetic",
    "stylistic",
    "optional",
    "minor",
    "deferred",
    "follow-up",
)


def _contains_any(text: str, markers: Sequence[str]) -> bool:
    normalized = text.lower()
    return any(re.search(rf"\b{re.escape(marker)}\b", normalized) for marker in markers)


def mentions_concurrency(title: str) -> bool:
    """True iff the title names concurrency terms."""
    return _contains_any(title, _CONCURRENCY_TERMS)


def failure_scenario_has_ordering_marker(failure_scenario: str) -> bool:
    """True iff the scenario carries at least one concrete ordering marker.

    Vague concurrency wording ("it might race under load") without a concrete
    shape - a thread/actor interleaving or a deterministic defeat description
    using tokens like while/during/then/before-after - fails this check.
    """
    return _contains_any(failure_scenario, _ORDERING_MARKERS)


def validate_finding_evidence(finding: StructuredFinding) -> list[str]:
    """Return machine-readable violations of per-classification evidence rules.

    * B1 requires a non-empty ``contract_reference``.
    * B2 requires non-empty ``invariant_or_requirement``, ``failure_scenario``,
      and ``evidence``; a concurrency-flavored title additionally requires at
      least one concrete ordering marker in ``failure_scenario``.
    * B3 requires evidence naming the explicitly required missing artifact.
    * D1 requires ``invariant_or_requirement`` naming the unresolved decision.
    * N1 requires evidence explaining why the finding is non-blocking.
    """
    violations: list[str] = []
    classification = finding.classification
    if classification is FindingClassification.CONTRACT_VIOLATION:
        if finding.contract_reference is None or not finding.contract_reference.strip():
            violations.append("b1_contract_reference_missing")
    elif classification is FindingClassification.CORRECTNESS_OR_SAFETY_DEFECT:
        for field_name, value in (
            ("invariant_or_requirement", finding.invariant_or_requirement),
            ("failure_scenario", finding.failure_scenario),
            ("evidence", finding.evidence),
        ):
            if not value.strip():
                violations.append(f"b2_{field_name}_missing")
        if mentions_concurrency(finding.title) and not failure_scenario_has_ordering_marker(
            finding.failure_scenario
        ):
            violations.append("b2_vague_concurrency_scenario_without_concrete_interleaving")
    elif classification is FindingClassification.MISSING_REQUIRED_EVIDENCE:
        if not _contains_any(finding.evidence, _B3_MISSING_MARKERS):
            violations.append("b3_evidence_must_name_required_missing_artifact")
    elif classification is FindingClassification.DESIGN_QUESTION:
        if not _contains_any(finding.invariant_or_requirement, _D1_DECISION_MARKERS):
            violations.append("d1_must_name_unresolved_decision")
    else:  # NON_BLOCKING_IMPROVEMENT
        if not _contains_any(finding.evidence, _N1_NON_BLOCKING_MARKERS):
            violations.append("n1_evidence_must_explain_non_blocking_rationale")
    return violations


class ReviewRiskLevel(StrEnum):
    """Coarse review-effort level derived deterministically from task text."""

    LIGHT = "LIGHT"
    STANDARD = "STANDARD"
    HIGH = "HIGH"


@dataclass(frozen=True)
class ReviewRiskAssessment:
    """Transparent result of :func:`classify_review_risk`."""

    level: ReviewRiskLevel
    score: int
    signals: tuple[str, ...]
    reason: str


_HIGH_RISK_TERMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "concurrency",
        (
            "race",
            "races",
            "concurrent",
            "concurrently",
            "concurrency",
            "thread",
            "threads",
            "threading",
            "multithreaded",
            "mutex",
            "deadlock",
            "livelock",
            "parallel",
            "parallelism",
            "interleaving",
            "atomicity",
        ),
    ),
    (
        "transaction",
        ("transaction", "transactions", "transactional", "rollback", "atomic commit"),
    ),
    ("migration", ("migration", "migrations", "migrate", "migrating", "backfill")),
    (
        "security_boundary",
        (
            "security",
            "auth",
            "authn",
            "authz",
            "authentication",
            "authorization",
            "credential",
            "credentials",
            "sandbox",
            "privilege",
            "permissions",
        ),
    ),
    (
        "crash_recovery",
        ("crash", "crashes", "recovery", "orphan", "orphaned", "watchdog", "restart", "resuming"),
    ),
    (
        "cross_process_state",
        (
            "ipc",
            "cross-process",
            "inter-process",
            "interprocess",
            "socket",
            "sockets",
            "pid",
            "daemon",
            "shared-memory",
        ),
    ),
    (
        "resource_lifetime",
        (
            "leak",
            "leaks",
            "ownership",
            "lifetime",
            "lifecycle",
            "cleanup",
            "unclosed",
            "exhaustion",
        ),
    ),
    (
        "data_loss_destructive",
        (
            "destructive",
            "irreversible",
            "truncate",
            "truncation",
            "wipe",
            "purge",
            "corruption",
            "corrupting",
            "unrecoverable",
            "data loss",
            "data-loss",
        ),
    ),
    (
        "critical_public_api",
        ("public api", "api contract", "breaking change", "breaking-change", "signature change"),
    ),
)

_LIGHT_TERMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("docs", ("docs", "documentation", "readme", "changelog")),
    ("formatting", ("formatting", "whitespace", "style-only")),
    ("rename", ("rename", "renamed", "renaming")),
    ("comment_only", ("comment", "comments", "comment-only")),
    ("glue", ("glue", "typo", "typos", "cosmetic")),
)

_DESIGN_READINESS_CATEGORIES: frozenset[str] = frozenset(
    {
        "concurrency",
        "transaction",
        "migration",
        "security_boundary",
        "crash_recovery",
        "cross_process_state",
        "resource_lifetime",
    }
)


def _matching_groups(
    text: str, table: tuple[tuple[str, tuple[str, ...]], ...], prefix: str
) -> list[str]:
    matched: list[str] = []
    for group, terms in table:
        if any(re.search(rf"\b{re.escape(term)}\b", text) for term in terms):
            matched.append(f"{prefix}:{group}")
    return matched


def classify_review_risk(task_text: str, changed_paths: Sequence[str] = ()) -> ReviewRiskAssessment:
    """Classify review effort with deterministic additive scoring.

    In the transparent style of :mod:`orchestrator_mvp.classifier`: each
    distinct high-risk category present adds +3 (signals ``high:<category>``),
    each distinct light category subtracts 2 (signals ``light:<category>``).
    Score >= 3 is HIGH, score <= -2 is LIGHT, otherwise STANDARD. Changed
    paths are scanned alongside the task text so e.g. a migrations directory
    counts. Persisted callers record level + signals + reason.
    """
    normalized = "\n".join((task_text, *changed_paths)).strip().lower()
    high_signals = _matching_groups(normalized, _HIGH_RISK_TERMS, "high")
    light_signals = _matching_groups(normalized, _LIGHT_TERMS, "light")
    score = 3 * len(high_signals) - 2 * len(light_signals)
    if score >= 3:
        level = ReviewRiskLevel.HIGH
    elif score <= -2:
        level = ReviewRiskLevel.LIGHT
    else:
        level = ReviewRiskLevel.STANDARD
    signals = (*high_signals, *light_signals)
    joined = ",".join(signals) if signals else "none"
    reason = f"{level.value} (score={score}; signals={joined})"
    return ReviewRiskAssessment(level=level, score=score, signals=signals, reason=reason)


@dataclass(frozen=True)
class DesignReadiness:
    """Result of :func:`assess_design_readiness`."""

    triggered: bool
    categories: tuple[str, ...]
    reason: str


def assess_design_readiness(task_text: str, *, enabled: bool) -> DesignReadiness:
    """Assess whether a design-return review pass is warranted for a task.

    Triggered ONLY when review is enabled by the caller AND at least two
    materially distinct high-risk categories among concurrency, transaction
    ordering, resource ownership/lifetime, security boundary, crash recovery,
    cross-process state, and migration are detected. Pure function; the
    enabled flag is a parameter, never global state.
    """
    if not enabled:
        return DesignReadiness(False, (), "review_disabled_by_caller")
    normalized = task_text.strip().lower()
    categories: list[str] = []
    for group, terms in _HIGH_RISK_TERMS:
        if group in _DESIGN_READINESS_CATEGORIES and any(
            re.search(rf"\b{re.escape(term)}\b", normalized) for term in terms
        ):
            categories.append(group)
    if len(categories) < 2:
        return DesignReadiness(
            False,
            tuple(categories),
            f"insufficient_distinct_high_risk_categories ({len(categories)}/2)",
        )
    return DesignReadiness(
        True,
        tuple(categories),
        f"triggered_by_distinct_high_risk_categories ({', '.join(categories)})",
    )


@dataclass(frozen=True)
class ReviewRoundState:
    """Persistable state of the bounded review-round machine.

    ``substantive_round`` counts completed substantive reviews (0 before the
    first). ``last_disposition`` stores the computed verdict value of the most
    recent review. ``remaining_blockers_scopes`` holds repair_scope values of
    outstanding blockers. The machine guarantees: after REVIEW #1 BLOCK
    exactly one repair then REVIEW #2; after REVIEW #2 BLOCK only when every
    remaining blocker is LOCAL_MECHANICAL may one FINAL REPAIR happen followed
    by REVIEW #3; REVIEW #3 is terminal; a fourth substantive round is
    structurally unreachable; any DESIGN_AFFECTING residual after round 2
    forces DESIGN_RETURN with no further repair.
    """

    substantive_round: int
    call_budget_used: int
    last_disposition: str | None
    remaining_blockers_scopes: tuple[str, ...]
    final_repair_used: bool
    terminal: bool


_ROUND_STATE_FIELDS: tuple[str, ...] = (
    "substantive_round",
    "call_budget_used",
    "last_disposition",
    "remaining_blockers_scopes",
    "final_repair_used",
    "terminal",
)


def initial_review_round_state() -> ReviewRoundState:
    """Return the state before any substantive review has run."""
    return ReviewRoundState(
        substantive_round=0,
        call_budget_used=0,
        last_disposition=None,
        remaining_blockers_scopes=(),
        final_repair_used=False,
        terminal=False,
    )


def next_review_round(state: ReviewRoundState) -> ReviewRoundState:
    """Advance to the next permitted substantive review round.

    Raises :class:`ReviewProtocolError` when the transition is structurally
    unreachable: terminal states, concluded dispositions, missing final
    repair, design-affecting residuals after round 2, or any fourth round.
    """
    if state.terminal:
        raise ReviewProtocolError("review round state is terminal; no further rounds exist")
    if state.substantive_round >= 3:
        raise ReviewProtocolError("fourth substantive review round is structurally unreachable")
    if state.substantive_round == 0:
        return replace(state, substantive_round=1)
    if state.substantive_round == 1:
        if state.last_disposition not in (None, ReviewPolicyVerdict.BLOCK.value):
            raise ReviewProtocolError(
                f"no further review after disposition {state.last_disposition}"
            )
        return replace(state, substantive_round=2)
    # substantive_round == 2: REVIEW #3 requires exactly the one final repair
    # of only local-mechanical blockers to have happened.
    if state.last_disposition != ReviewPolicyVerdict.BLOCK.value:
        raise ReviewProtocolError(
            f"review #3 unreachable after disposition {state.last_disposition}"
        )
    if any(
        scope != RepairScope.LOCAL_MECHANICAL.value for scope in state.remaining_blockers_scopes
    ):
        raise ReviewProtocolError(
            "design-affecting residual after review #2 forces DESIGN_RETURN; "
            "no further repair or review"
        )
    if not state.final_repair_used:
        raise ReviewProtocolError(
            "review #3 requires the single final repair of local-mechanical blockers first"
        )
    return replace(state, substantive_round=3)


def final_repair_allowed(state: ReviewRoundState) -> bool:
    """True iff the one FINAL REPAIR after REVIEW #2 is currently permitted.

    Permitted only when review #2 just blocked, the final repair has not been
    used, and EVERY remaining blocker scope is LOCAL_MECHANICAL. Any
    DESIGN_AFFECTING residual forbids it (DESIGN_RETURN instead).
    """
    return bool(
        state.substantive_round == 2
        and state.last_disposition == ReviewPolicyVerdict.BLOCK.value
        and not state.final_repair_used
        and state.remaining_blockers_scopes
        and all(
            scope == RepairScope.LOCAL_MECHANICAL.value for scope in state.remaining_blockers_scopes
        )
    )


def review_round_state_to_json(state: ReviewRoundState) -> str:
    """Serialize the round state for durable persistence."""
    return json.dumps(
        {
            "substantive_round": state.substantive_round,
            "call_budget_used": state.call_budget_used,
            "last_disposition": state.last_disposition,
            "remaining_blockers_scopes": list(state.remaining_blockers_scopes),
            "final_repair_used": state.final_repair_used,
            "terminal": state.terminal,
        }
    )


def review_round_state_from_json(raw: str) -> ReviewRoundState:
    """Rebuild round state from JSON, failing closed on any deviation."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReviewProtocolError(f"malformed review round state JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ReviewProtocolError("review round state must be a JSON object")
    for key in _ROUND_STATE_FIELDS:
        if key not in data:
            raise ReviewProtocolError(f"review round state missing required field: {key}")
    substantive_round = data["substantive_round"]
    call_budget_used = data["call_budget_used"]
    for name, value in (
        ("substantive_round", substantive_round),
        ("call_budget_used", call_budget_used),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ReviewProtocolError(f"review round state field '{name}' must be a int >= 0")
    last_disposition_raw = data["last_disposition"]
    if last_disposition_raw is None:
        last_disposition: str | None = None
    else:
        coerced = _POLICY_VERDICT_ALIASES.get(str(last_disposition_raw).strip().upper())
        if coerced is None:
            raise ReviewProtocolError(f"unknown last_disposition: {last_disposition_raw!r}")
        last_disposition = coerced.value
    raw_scopes = data["remaining_blockers_scopes"]
    if not isinstance(raw_scopes, list):
        raise ReviewProtocolError("field 'remaining_blockers_scopes' must be a list")
    scopes: list[str] = []
    for scope_raw in raw_scopes:
        scope = _REPAIR_SCOPE_ALIASES.get(str(scope_raw).strip().upper())
        if scope is None:
            raise ReviewProtocolError(f"unknown remaining blocker scope: {scope_raw!r}")
        scopes.append(scope.value)
    for name in ("final_repair_used", "terminal"):
        if not isinstance(data[name], bool):
            raise ReviewProtocolError(f"review round state field '{name}' must be a boolean")
    return ReviewRoundState(
        substantive_round=substantive_round,
        call_budget_used=call_budget_used,
        last_disposition=last_disposition,
        remaining_blockers_scopes=tuple(scopes),
        final_repair_used=data["final_repair_used"],
        terminal=data["terminal"],
    )
