"""C13-F risk-adaptive review policy.

Pure policy module for the decision surface (``classify_change``,
``decide_review``, ``ReviewDecision`` JSON shape, invariants):
no I/O, no clock, no randomness, no model calls. Every classification
input is deterministic; every decision is JSON-serializable; the
persisted ``ReviewDecision`` is the single authoritative source of truth
that downstream acceptance / supervision / reconstruction code consults
to decide whether an independent semantic review is required for a Run.

The project-policy persistence seam (``load_project_review_policy`` /
``set_project_review_policy`` / ``remove_project_review_policy``) does
perform atomic file I/O against the canonical per-project state
directory (mirroring the C13-B gate-scratch policy pattern).  Those
helpers are the only I/O surface in this module and they are the
single seam CLI / Web / supervisor use to resolve the project policy
that becomes frozen on the run row at Run creation.

Independent semantic review is a **risk control**, not a ritual.
Review intensity follows:

* material risk of the candidate change;
* authority impact (security / governance / acceptance / publication);
* actual blast radius (ownership boundary crossings, deterministic
  coverage, autonomous execution).

It MUST NOT follow merely:

* tranche existence;
* file count;
* candidate SHA change;
* reviewer availability;
* previous-tranche precedent;
* tradition.

The doctrine and the precedence hierarchy are preserved exactly:
``review_adaptive`` decides whether a semantic reviewer MUST be launched.
It MUST NOT decide acceptance, MUST NOT run the authoritative full gate,
MUST NOT change candidate SHA, and MUST NOT silently override any other
authority (project / routing / security / gate / acceptance / publication).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

from saberops.models import ReviewPolicyMode, RiskLevel, Tier

REVIEW_ADAPTIVE_VERSION: Final[str] = "review-adaptive/1"

# Stable on-disk filename inside the project state directory.  Deliberately
# distinct from the immutable identity marker ``project.json``: this file
# holds mutable per-project review-policy preference, mirroring the C13-B
# gate-scratch policy file pattern.
_REVIEW_POLICY_FILENAME: Final[str] = "review-policy.json"
_REVIEW_POLICY_TMP_PREFIX: Final[str] = ".review-policy-"

# ---------------------------------------------------------------------------
# Project policy file schema version.  Bump only when the persisted document
# shape changes incompatibly.
# ---------------------------------------------------------------------------

REVIEW_POLICY_SCHEMA_VERSION: Final[int] = 1


class ReviewPolicyError(ValueError):
    """Fail-closed error for an invalid project review policy or decision."""


# Re-export the canonical enum so callers that already import
# ``ReviewPolicyMode`` from :mod:`saberops.review_adaptive`
# continue to work after the enum moved to :mod:`saberops.models`.
# This is the canonical seam: do not duplicate the enum.


_PROJECT_POLICY_MODE_ALIASES: dict[str, ReviewPolicyMode] = {
    token.upper(): member
    for member in ReviewPolicyMode
    for token in (member.value, member.name)
}


class ChangeClass(StrEnum):
    """Coarse change taxonomy used by the deterministic classifier.

    Distinct from review-effort levels in :mod:`saberops.review_policy`:
    this taxonomy classifies the **change**, not the required reviewer
    effort, and is consumed only by the C13-F decision pipeline.
    """

    DOCS_ONLY = "DOCS_ONLY"
    PLANNING_DOCS = "PLANNING_DOCS"
    ORDINARY_PRODUCTION = "ORDINARY_PRODUCTION"
    LOAD_BEARING_AUTHORITY = "LOAD_BEARING_AUTHORITY"
    HIGH_T3 = "HIGH_T3"


class ReviewRequirementSource(StrEnum):
    """Authoritative source of the ``independent_review_required`` answer.

    Persisted on the frozen decision so audits can answer "why was
    review required / skipped for this run?" deterministically.  The
    taxonomy deliberately excludes any field that would let a later
    caller mutate the decision silently -- the source is decided
    exactly once at Run-candidate-ready time.

    ``CLASSIFIER_AUTO_TIER_UNRESOLVABLE`` is the single truthful
    label for the fail-closed seam added at C13-F correction: when an
    auto-routed candidate's durable tier cannot be read (database
    raised, the run row is missing, or ``run.tier`` is null), the
    classifier may still emit a LOW-skip answer on diff patterns
    alone, but the candidate may have been produced at T3 with that
    signal invisible to the audit.  The corrected orchestrator
    overrides only the boolean answer and stamps this source; the
    classifier's actual ``change_class``, ``risk_level``,
    ``changed_paths`` and ``signals`` remain unchanged so the audit
    does not falsely claim the tier was positively observed as T3.
    """

    OWNER_EXPLICIT = "OWNER_EXPLICIT"
    POLICY_ALWAYS_REQUIRED = "POLICY_ALWAYS_REQUIRED"
    AUTONOMOUS_SAFETY = "AUTONOMOUS_SAFETY"
    CLASSIFIER_HIGH_T3 = "CLASSIFIER_HIGH_T3"
    CLASSIFIER_MEDIUM = "CLASSIFIER_MEDIUM"
    CLASSIFIER_LOAD_BEARING = "CLASSIFIER_LOAD_BEARING"
    CLASSIFIER_DOCS_ESCALATED = "CLASSIFIER_DOCS_ESCALATED"
    CLASSIFIER_LOW_SKIPPED = "CLASSIFIER_LOW_SKIPPED"
    CLASSIFIER_AUTO_TIER_UNRESOLVABLE = "CLASSIFIER_AUTO_TIER_UNRESOLVABLE"


# ---------------------------------------------------------------------------
# Path pattern groups.  Deterministic, no I/O: pure string matching.
# ---------------------------------------------------------------------------

_DOCS_PATH_PATTERNS: Final[tuple[str, ...]] = (
    r"\.md$",
    r"\.rst$",
    r"\.txt$",
    r"^docs?/",
    r"^ROADMAP\.md$",
    r"^STATE\.md$",
    r"^BACKLOG\.md$",
    r"^NOTES\.md$",
    r"^MANUAL\.md$",
    r"^README\.md$",
    r"^C13_E_MEASUREMENT\.md$",
    r"^CHANGELOG\.md$",
)

_HARD_GOVERNANCE_PATH_PATTERNS: Final[tuple[str, ...]] = (
    r"^architecture/modules/.*\.toml$",
    r"^architecture/context/.*\.md$",
    r"^architecture/context/.*\.py$",
    r"^architecture/.*\.py$",
    r"^AGENTS\.md$",
    r"^AGY_INTERFACE\.md$",
    r"^AGY_TOGGLE_VERIFICATION\.md$",
    r"^VALIDATION_POLICY\.md$",
)

_SECURITY_PATH_PATTERNS: Final[tuple[str, ...]] = (
    r"^saberops/sandbox/",
    r"^saberops/control_plane/",
    r"^saberops/authority\.py$",
    r"^saberops/publishing\.py$",
    r"^saberops/execution_failover\.py$",
    r"^saberops/ownership\.py$",
    r"^saberops/provenance\.py$",
)

_ACCEPTANCE_OR_GATE_PATH_PATTERNS: Final[tuple[str, ...]] = (
    r"^saberops/accept\.py$",
    r"^saberops/gate_runner\.py$",
    r"^saberops/quota\.py$",
    r"^saberops/routing\.py$",
    r"^saberops/routing_decision\.py$",
    r"^saberops/routing_config\.py$",
    r"^saberops/process\.py$",
)

_PUBLICATION_PATH_PATTERNS: Final[tuple[str, ...]] = (
    r"^saberops/publishing\.py$",
    r"^saberops/gateway/",
    r"^saberops/vcs/",
)

_RECOVERY_PATH_PATTERNS: Final[tuple[str, ...]] = (
    r"^saberops/replay/",
    r"^saberops/persistence/",
    r"^saberops/db\.py$",
    r"^saberops/execution_failover\.py$",
)

_PLANNING_DOC_PATH_PATTERNS: Final[tuple[str, ...]] = (
    r"^saberops/planning\.py$",
    r"^saberops/slicing\.py$",
    r"^saberops/project_scope/",
)

# Exact module / production-path patterns that count as "ordinary production"
# for the LOW skip path.  Conservative: only well-understood orchestrator
# helper / refactor surfaces qualify.  Anything that crosses an ownership
# boundary, lives in a hard-governance file, or affects authority / acceptance
# / publication MUST be classified MEDIUM or higher regardless of pattern.
_ORDINARY_PRODUCTION_PATH_PATTERNS: Final[tuple[str, ...]] = (
    r"^saberops/classifier\.py$",
    r"^saberops/terminal\.py$",
    r"^saberops/templates/",
    r"^saberops/static/",
    r"^saberops/observability/",
    r"^saberops/telemetry\.py$",
)


@dataclass(frozen=True)
class ChangeClassification:
    """Deterministic output of :func:`classify_change`.

    Persisted on the frozen :class:`ReviewDecision` so audits can answer
    "what did the classifier see?" without re-running the classifier.
    """

    change_class: ChangeClass
    risk_level: RiskLevel
    changed_paths: tuple[str, ...]
    crosses_ownership_boundary: bool
    changes_hard_governance: bool
    changes_security_boundary: bool
    changes_acceptance_or_gate_authority: bool
    changes_publication_authority: bool
    changes_recovery_or_exactly_once: bool
    deterministic_coverage_available: bool
    signals: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class ReviewDecision:
    """Frozen, JSON-serializable decision: is independent review required?

    Persisted on the Run row at candidate-ready time so every later
    consumer (AcceptEngine, supervisor recovery, handoff, retry) consults
    one authoritative value.  Reconstruction with malformed JSON fails
    closed (treat as ``independent_review_required=True``).

    Field provenance is exactly:

    * ``independent_review_required`` -- the single boolean answer;
    * ``review_requirement_source`` -- why it is that answer;
    * ``review_skip_reason`` -- non-empty iff review was skipped,
      deterministically explaining the skip;
    * remaining fields -- the deterministic classifier inputs/outputs.
    """

    decision_id: str
    classification: ChangeClassification
    review_policy_mode: ReviewPolicyMode
    owner_explicit_review: bool
    is_autonomous: bool
    is_high_t3: bool
    independent_review_required: bool
    review_requirement_source: ReviewRequirementSource
    review_skip_reason: str
    schema_version: str

    def to_json(self) -> str:
        """Serialize deterministically for durable persistence."""
        payload = {
            "schema_version": self.schema_version,
            "decision_id": self.decision_id,
            "review_policy_mode": self.review_policy_mode.value,
            "owner_explicit_review": self.owner_explicit_review,
            "is_autonomous": self.is_autonomous,
            "is_high_t3": self.is_high_t3,
            "independent_review_required": self.independent_review_required,
            "review_requirement_source": self.review_requirement_source.value,
            "review_skip_reason": self.review_skip_reason,
            "classification": {
                "change_class": self.classification.change_class.value,
                "risk_level": self.classification.risk_level.value,
                "changed_paths": list(self.classification.changed_paths),
                "crosses_ownership_boundary": self.classification.crosses_ownership_boundary,
                "changes_hard_governance": self.classification.changes_hard_governance,
                "changes_security_boundary": self.classification.changes_security_boundary,
                "changes_acceptance_or_gate_authority": (
                    self.classification.changes_acceptance_or_gate_authority
                ),
                "changes_publication_authority": (
                    self.classification.changes_publication_authority
                ),
                "changes_recovery_or_exactly_once": (
                    self.classification.changes_recovery_or_exactly_once
                ),
                "deterministic_coverage_available": (
                    self.classification.deterministic_coverage_available
                ),
                "signals": list(self.classification.signals),
                "reason": self.classification.reason,
            },
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str) -> ReviewDecision:
        """Reconstruct from persisted JSON, failing closed on any deviation."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ReviewPolicyError(f"malformed review decision JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ReviewPolicyError("review decision payload must be a JSON object")
        for key in (
            "schema_version",
            "decision_id",
            "review_policy_mode",
            "owner_explicit_review",
            "is_autonomous",
            "is_high_t3",
            "independent_review_required",
            "review_requirement_source",
            "review_skip_reason",
            "classification",
        ):
            if key not in data:
                raise ReviewPolicyError(
                    f"review decision missing required field: {key}"
                )
        schema_version = data["schema_version"]
        if not isinstance(schema_version, str) or not schema_version:
            raise ReviewPolicyError("review decision schema_version must be a non-empty string")
        mode_token = str(data["review_policy_mode"]).strip().upper()
        mode = _PROJECT_POLICY_MODE_ALIASES.get(mode_token)
        if mode is None:
            raise ReviewPolicyError(f"unknown review policy mode: {data['review_policy_mode']!r}")
        source_token = str(data["review_requirement_source"]).strip().upper()
        if source_token not in {member.value for member in ReviewRequirementSource}:
            raise ReviewPolicyError(
                f"unknown review requirement source: {data['review_requirement_source']!r}"
            )
        for bool_field in (
            "owner_explicit_review",
            "is_autonomous",
            "is_high_t3",
            "independent_review_required",
        ):
            if not isinstance(data[bool_field], bool):
                raise ReviewPolicyError(
                    f"review decision field '{bool_field}' must be a boolean"
                )
        skip_reason = data["review_skip_reason"]
        if not isinstance(skip_reason, str):
            raise ReviewPolicyError("review_skip_reason must be a string")
        cls_payload = data["classification"]
        if not isinstance(cls_payload, dict):
            raise ReviewPolicyError("classification field must be a JSON object")
        for key in (
            "change_class",
            "risk_level",
            "changed_paths",
            "crosses_ownership_boundary",
            "changes_hard_governance",
            "changes_security_boundary",
            "changes_acceptance_or_gate_authority",
            "changes_publication_authority",
            "changes_recovery_or_exactly_once",
            "deterministic_coverage_available",
            "signals",
            "reason",
        ):
            if key not in cls_payload:
                raise ReviewPolicyError(f"classification missing required field: {key}")
        change_class_token = str(cls_payload["change_class"]).strip().upper()
        if change_class_token not in {member.value for member in ChangeClass}:
            raise ReviewPolicyError(f"unknown change_class: {cls_payload['change_class']!r}")
        risk_level_token = str(cls_payload["risk_level"]).strip().upper()
        if risk_level_token not in {member.value for member in RiskLevel}:
            raise ReviewPolicyError(f"unknown risk_level: {cls_payload['risk_level']!r}")
        changed_paths_raw = cls_payload["changed_paths"]
        if not isinstance(changed_paths_raw, list):
            raise ReviewPolicyError("classification.changed_paths must be a list")
        signals_raw = cls_payload["signals"]
        if not isinstance(signals_raw, list):
            raise ReviewPolicyError("classification.signals must be a list")
        for bool_field in (
            "crosses_ownership_boundary",
            "changes_hard_governance",
            "changes_security_boundary",
            "changes_acceptance_or_gate_authority",
            "changes_publication_authority",
            "changes_recovery_or_exactly_once",
            "deterministic_coverage_available",
        ):
            if not isinstance(cls_payload[bool_field], bool):
                raise ReviewPolicyError(
                    f"classification.{bool_field} must be a boolean"
                )
        reason_text = cls_payload["reason"]
        if not isinstance(reason_text, str):
            raise ReviewPolicyError("classification.reason must be a string")
        classification = ChangeClassification(
            change_class=ChangeClass(change_class_token),
            risk_level=RiskLevel(risk_level_token),
            changed_paths=tuple(str(p) for p in changed_paths_raw),
            crosses_ownership_boundary=cls_payload["crosses_ownership_boundary"],
            changes_hard_governance=cls_payload["changes_hard_governance"],
            changes_security_boundary=cls_payload["changes_security_boundary"],
            changes_acceptance_or_gate_authority=cls_payload[
                "changes_acceptance_or_gate_authority"
            ],
            changes_publication_authority=cls_payload["changes_publication_authority"],
            changes_recovery_or_exactly_once=cls_payload["changes_recovery_or_exactly_once"],
            deterministic_coverage_available=cls_payload["deterministic_coverage_available"],
            signals=tuple(str(s) for s in signals_raw),
            reason=reason_text,
        )
        return cls(
            decision_id=str(data["decision_id"]),
            classification=classification,
            review_policy_mode=mode,
            owner_explicit_review=data["owner_explicit_review"],
            is_autonomous=data["is_autonomous"],
            is_high_t3=data["is_high_t3"],
            independent_review_required=data["independent_review_required"],
            review_requirement_source=ReviewRequirementSource(source_token),
            review_skip_reason=skip_reason,
            schema_version=schema_version,
        )


def _matches_any(path: str, patterns: Iterable[str]) -> bool:
    return any(re.search(pattern, path) for pattern in patterns)


def _paths_match_group(
    paths: Iterable[str], patterns: tuple[str, ...]
) -> tuple[str, ...]:
    matched: list[str] = []
    for path in paths:
        if _matches_any(path, patterns):
            matched.append(path)
    return tuple(matched)


def _detect_ownership_boundary(changed_paths: tuple[str, ...]) -> bool:
    """Return True iff the delta touches paths owned by more than one module.

    Heuristic: a change that touches both ``saberops/<area>/`` and
    ``saberops/<other_area>/`` crosses an ownership boundary.  The
    same is true when the change touches ``architecture/*`` plus any
    production surface, or when the change touches the ``tests/`` root
    in combination with anything else.  A pure single-module change does
    not cross.
    """
    if not changed_paths:
        return False
    module_dirs: set[str] = set()
    has_architecture = False
    has_tests_root = False
    has_outside_pkg = False
    for path in changed_paths:
        if path.startswith("architecture/"):
            has_architecture = True
            continue
        if path.startswith("tests/"):
            has_tests_root = True
        if path.startswith("saberops/"):
            parts = path[len("saberops/") :].split("/", 1)
            module_dirs.add(parts[0])
        elif path.startswith("src/"):
            module_dirs.add(path)
        else:
            has_outside_pkg = True
    if len(module_dirs) > 1:
        return True
    if has_architecture and (module_dirs or has_outside_pkg):
        return True
    if has_tests_root and module_dirs:
        return True
    return False


def _has_deterministic_coverage(change_class: ChangeClass) -> bool:
    """Conservative estimate: deterministic affected verification available?

    The classifier returns True except for the cases where runtime
    coverage cannot be safely established from the changed paths alone
    (HIGH/T3 or any hard-authority / security / publication change).
    """
    if change_class is ChangeClass.HIGH_T3:
        return False
    return True


def classify_change(
    *,
    task_text: str,
    changed_paths: tuple[str, ...],
    tier: Tier | None,
    autonomous: bool,
) -> ChangeClassification:
    """Classify the change into the C13-F taxonomy using deterministic facts.

    Inputs are all structured: task text, normalized repo-relative changed
    paths, the resolved routing tier, and whether the run is autonomous.
    The classifier NEVER consults model state, session state, reviewer
    availability, or file counts beyond what is needed to detect
    authority-impacting files.

    Conservative by construction: when any structural evidence cannot
    safely establish LOW, the classifier escalates rather than guessing.
    """
    signals: list[str] = []

    # 1. Doc-only detection: every path matches a docs pattern AND nothing
    #    in the changed paths matches a hard-authority / security /
    #    acceptance / publication / recovery pattern.
    all_paths = tuple(p for p in changed_paths if p)
    doc_paths = _paths_match_group(all_paths, _DOCS_PATH_PATTERNS)
    governance_paths = _paths_match_group(all_paths, _HARD_GOVERNANCE_PATH_PATTERNS)
    security_paths = _paths_match_group(all_paths, _SECURITY_PATH_PATTERNS)
    acceptance_paths = _paths_match_group(all_paths, _ACCEPTANCE_OR_GATE_PATH_PATTERNS)
    publication_paths = _paths_match_group(all_paths, _PUBLICATION_PATH_PATTERNS)
    recovery_paths = _paths_match_group(all_paths, _RECOVERY_PATH_PATTERNS)
    planning_paths = _paths_match_group(all_paths, _PLANNING_DOC_PATH_PATTERNS)

    changes_hard_governance = bool(governance_paths)
    changes_security_boundary = bool(security_paths)
    changes_acceptance_or_gate_authority = bool(acceptance_paths)
    changes_publication_authority = bool(publication_paths)
    changes_recovery_or_exactly_once = bool(recovery_paths)

    if governance_paths:
        signals.append(f"hard_governance_paths:{len(governance_paths)}")
    if security_paths:
        signals.append(f"security_boundary_paths:{len(security_paths)}")
    if acceptance_paths:
        signals.append(f"acceptance_gate_authority_paths:{len(acceptance_paths)}")
    if publication_paths:
        signals.append(f"publication_authority_paths:{len(publication_paths)}")
    if recovery_paths:
        signals.append(f"recovery_or_exactly_once_paths:{len(recovery_paths)}")

    crosses = _detect_ownership_boundary(all_paths)
    if crosses:
        signals.append("ownership_boundary_crossed")

    # HIGH/T3 path: routing tier is HIGH, OR autonomous execution flagged.
    if tier is Tier.T3:
        signals.append("tier_t3")
    if autonomous:
        signals.append("autonomous_execution")

    # Doc-only escalation: documentation changes that touch hard authority
    # / security / acceptance / publication are NOT LOW.  They are
    # escalated to LOAD_BEARING_AUTHORITY.
    docs_only = bool(all_paths) and (
        len(doc_paths) == len(all_paths) and not (
            changes_hard_governance
            or changes_security_boundary
            or changes_acceptance_or_gate_authority
            or changes_publication_authority
            or changes_recovery_or_exactly_once
        )
    )
    if docs_only:
        signals.append("docs_only_paths")

    # Planning-doc detection: paths live in planning / slicing / project_scope
    # and nothing else.  Treated as PLANNING_DOCS (LOW by default unless
    # load-bearing metadata is touched).
    planning_only = bool(planning_paths) and (
        len(planning_paths) == len(all_paths)
        and not (
            changes_hard_governance
            or changes_security_boundary
            or changes_acceptance_or_gate_authority
            or changes_publication_authority
            or changes_recovery_or_exactly_once
            or crosses
        )
    )
    if planning_only:
        signals.append("planning_only_paths")

    # Determine change_class + risk_level.
    if (
        tier is Tier.T3
        or autonomous
        or changes_security_boundary
        or changes_acceptance_or_gate_authority
        or changes_publication_authority
        or changes_recovery_or_exactly_once
    ):
        change_class = ChangeClass.HIGH_T3
        risk_level = RiskLevel.HIGH
    elif changes_hard_governance or crosses:
        change_class = ChangeClass.LOAD_BEARING_AUTHORITY
        risk_level = RiskLevel.MEDIUM
    elif docs_only:
        # Pure documentation delta with no authority impact.
        change_class = ChangeClass.DOCS_ONLY
        risk_level = RiskLevel.LOW
    elif planning_only:
        change_class = ChangeClass.PLANNING_DOCS
        risk_level = RiskLevel.LOW
    elif any(
        _matches_any(p, _ORDINARY_PRODUCTION_PATH_PATTERNS) for p in all_paths
    ):
        # Pure ordinary production code change inside one ordinary surface,
        # no hard-authority / ownership crossings.
        change_class = ChangeClass.ORDINARY_PRODUCTION
        risk_level = RiskLevel.LOW
    else:
        # Anything else with no explicit LOW signal is MEDIUM.
        change_class = ChangeClass.ORDINARY_PRODUCTION
        risk_level = RiskLevel.MEDIUM
        signals.append("no_low_signal_default_medium")

    deterministic_coverage_available = _has_deterministic_coverage(change_class)
    if not deterministic_coverage_available:
        signals.append("deterministic_coverage_unavailable")

    reason = (
        f"{change_class.value} ({risk_level.value}; "
        f"signals={','.join(signals) if signals else 'none'})"
    )
    return ChangeClassification(
        change_class=change_class,
        risk_level=risk_level,
        changed_paths=all_paths,
        crosses_ownership_boundary=crosses,
        changes_hard_governance=changes_hard_governance,
        changes_security_boundary=changes_security_boundary,
        changes_acceptance_or_gate_authority=changes_acceptance_or_gate_authority,
        changes_publication_authority=changes_publication_authority,
        changes_recovery_or_exactly_once=changes_recovery_or_exactly_once,
        deterministic_coverage_available=deterministic_coverage_available,
        signals=tuple(signals),
        reason=reason,
    )


def decide_review(
    *,
    classification: ChangeClassification,
    review_policy_mode: ReviewPolicyMode,
    owner_explicit_review: bool,
    is_autonomous: bool,
    decision_id: str,
) -> ReviewDecision:
    """Compute the frozen, JSON-serializable review decision.

    Precedence (highest first):

    1. Autonomous execution -- ``independent_review_required=True`` always.
    2. Owner explicit ``--review`` -- ``independent_review_required=True``.
    3. Project policy ``ALWAYS_REQUIRED`` -- ``independent_review_required=True``.
    4. HIGH/T3 classifier -- ``independent_review_required=True``.
    5. MEDIUM / LOAD_BEARING -- ``independent_review_required=True``.
    6. DOCS_ESCALATED (docs touching hard authority) -- ``True``.
    7. LOW (no escalation) -- ``False``, with deterministic skip reason.

    Hard minimums are not parameters: there is no caller path that can
    weaken them. The precedence above is the entire invariant.
    """
    is_high_t3 = classification.change_class is ChangeClass.HIGH_T3

    if is_autonomous:
        return ReviewDecision(
            decision_id=decision_id,
            classification=classification,
            review_policy_mode=review_policy_mode,
            owner_explicit_review=owner_explicit_review,
            is_autonomous=True,
            is_high_t3=is_high_t3,
            independent_review_required=True,
            review_requirement_source=ReviewRequirementSource.AUTONOMOUS_SAFETY,
            review_skip_reason="",
            schema_version=REVIEW_ADAPTIVE_VERSION,
        )
    if owner_explicit_review:
        return ReviewDecision(
            decision_id=decision_id,
            classification=classification,
            review_policy_mode=review_policy_mode,
            owner_explicit_review=True,
            is_autonomous=False,
            is_high_t3=is_high_t3,
            independent_review_required=True,
            review_requirement_source=ReviewRequirementSource.OWNER_EXPLICIT,
            review_skip_reason="",
            schema_version=REVIEW_ADAPTIVE_VERSION,
        )
    if review_policy_mode is ReviewPolicyMode.ALWAYS_REQUIRED:
        return ReviewDecision(
            decision_id=decision_id,
            classification=classification,
            review_policy_mode=review_policy_mode,
            owner_explicit_review=False,
            is_autonomous=False,
            is_high_t3=is_high_t3,
            independent_review_required=True,
            review_requirement_source=ReviewRequirementSource.POLICY_ALWAYS_REQUIRED,
            review_skip_reason="",
            schema_version=REVIEW_ADAPTIVE_VERSION,
        )
    if classification.change_class is ChangeClass.HIGH_T3:
        return ReviewDecision(
            decision_id=decision_id,
            classification=classification,
            review_policy_mode=review_policy_mode,
            owner_explicit_review=False,
            is_autonomous=False,
            is_high_t3=True,
            independent_review_required=True,
            review_requirement_source=ReviewRequirementSource.CLASSIFIER_HIGH_T3,
            review_skip_reason="",
            schema_version=REVIEW_ADAPTIVE_VERSION,
        )
    if (
        classification.change_class is ChangeClass.LOAD_BEARING_AUTHORITY
        or classification.risk_level is RiskLevel.MEDIUM
    ):
        source = ReviewRequirementSource.CLASSIFIER_LOAD_BEARING
        return ReviewDecision(
            decision_id=decision_id,
            classification=classification,
            review_policy_mode=review_policy_mode,
            owner_explicit_review=False,
            is_autonomous=False,
            is_high_t3=is_high_t3,
            independent_review_required=True,
            review_requirement_source=source,
            review_skip_reason="",
            schema_version=REVIEW_ADAPTIVE_VERSION,
        )
    # Docs that change a hard-authority file are an explicit escalation,
    # even though the path patterns say "docs only".  Catches the case
    # where, e.g., a Markdown file under architecture/context/ defines a
    # new authority boundary.
    if classification.changes_hard_governance:
        return ReviewDecision(
            decision_id=decision_id,
            classification=classification,
            review_policy_mode=review_policy_mode,
            owner_explicit_review=False,
            is_autonomous=False,
            is_high_t3=False,
            independent_review_required=True,
            review_requirement_source=ReviewRequirementSource.CLASSIFIER_DOCS_ESCALATED,
            review_skip_reason="",
            schema_version=REVIEW_ADAPTIVE_VERSION,
        )

    skip_reason = (
        f"{classification.change_class.value} delta with risk_level="
        f"{classification.risk_level.value} ({classification.reason})"
    )
    return ReviewDecision(
        decision_id=decision_id,
        classification=classification,
        review_policy_mode=review_policy_mode,
        owner_explicit_review=False,
        is_autonomous=False,
        is_high_t3=False,
        independent_review_required=False,
        review_requirement_source=ReviewRequirementSource.CLASSIFIER_LOW_SKIPPED,
        review_skip_reason=skip_reason,
        schema_version=REVIEW_ADAPTIVE_VERSION,
    )


def conservative_legacy_decision(
    *, owner_explicit_review: bool, review_policy_mode: ReviewPolicyMode
) -> ReviewDecision:
    """Return a conservative decision for a Run with no C13-F fields.

    Used by Accept and supervisor reconstruction paths when the Run row
    has no persisted C13-F decision.  Never invents a skip: when
    ``review_enabled`` was effectively true for the legacy run, this
    preserves the legacy semantic of "review required".  Never downgrades
    HIGH/T3, autonomous, or owner-explicit review.
    """
    classification = ChangeClassification(
        change_class=ChangeClass.LOAD_BEARING_AUTHORITY,
        risk_level=RiskLevel.MEDIUM,
        changed_paths=(),
        crosses_ownership_boundary=False,
        changes_hard_governance=False,
        changes_security_boundary=False,
        changes_acceptance_or_gate_authority=False,
        changes_publication_authority=False,
        changes_recovery_or_exactly_once=False,
        deterministic_coverage_available=True,
        signals=("legacy_conservative_no_decision",),
        reason="legacy_conservative_no_decision",
    )
    if owner_explicit_review:
        source = ReviewRequirementSource.OWNER_EXPLICIT
        required = True
    elif review_policy_mode is ReviewPolicyMode.ALWAYS_REQUIRED:
        source = ReviewRequirementSource.POLICY_ALWAYS_REQUIRED
        required = True
    else:
        source = ReviewRequirementSource.CLASSIFIER_LOAD_BEARING
        required = True
    return ReviewDecision(
        decision_id="legacy-conservative",
        classification=classification,
        review_policy_mode=review_policy_mode,
        owner_explicit_review=owner_explicit_review,
        is_autonomous=False,
        is_high_t3=False,
        independent_review_required=required,
        review_requirement_source=source,
        review_skip_reason="",
        schema_version=REVIEW_ADAPTIVE_VERSION,
    )


# ---------------------------------------------------------------------------
# Project policy persistence
# ---------------------------------------------------------------------------


def project_review_policy_path(state_dir: Path | str) -> Path:
    """Return the on-disk path of the per-project review-policy file."""
    return Path(state_dir) / _REVIEW_POLICY_FILENAME


def _coerce_mode(value: object, source: str) -> ReviewPolicyMode:
    if isinstance(value, ReviewPolicyMode):
        return value
    token = str(value).strip().upper()
    member = _PROJECT_POLICY_MODE_ALIASES.get(token)
    if member is None:
        raise ReviewPolicyError(
            f"review policy at {source}: unknown mode {value!r}; "
            f"expected one of {[m.value for m in ReviewPolicyMode]}"
        )
    return member


def _read_review_policy_file(path: Path) -> ReviewPolicyMode | None:
    """Read and validate a project policy document.

    Returns ``None`` when the file does not exist.  An existing file that
    is unreadable or malformed raises :class:`ReviewPolicyError` so the
    caller can fail closed rather than silently revert to RISK_ADAPTIVE.
    """
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReviewPolicyError(
            f"review policy at {path}: cannot read: {exc}"
        ) from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReviewPolicyError(
            f"review policy at {path}: invalid JSON: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise ReviewPolicyError(f"review policy at {path}: payload must be a JSON object")
    version = data.get("version")
    if version != REVIEW_POLICY_SCHEMA_VERSION:
        raise ReviewPolicyError(
            f"review policy at {path}: unknown schema_version {version!r}; "
            f"expected {REVIEW_POLICY_SCHEMA_VERSION}"
        )
    if "mode" not in data:
        raise ReviewPolicyError(f"review policy at {path}: missing 'mode'")
    return _coerce_mode(data["mode"], str(path))


def load_project_review_policy_for_state_dir(
    state_dir: Path | str,
) -> ReviewPolicyMode:
    """Return the persisted review-policy mode for ``state_dir``.

    A missing file is the canonical ``RISK_ADAPTIVE`` default.  A
    present-but-corrupt file fails closed with :class:`ReviewPolicyError`
    because corrupt project authority is a fail-closed condition (mirrors
    the C13-B gate-scratch policy loader).
    """
    path = project_review_policy_path(state_dir)
    loaded = _read_review_policy_file(path)
    return (
        ReviewPolicyMode.RISK_ADAPTIVE
        if loaded is None
        else loaded
    )


def load_project_review_policy(repo_path: Path | str) -> ReviewPolicyMode:
    """Return the persisted review-policy mode for the project at ``repo_path``.

    Resolves the canonical project identity (the same seam
    :func:`saberops.gate_runner.load_project_gate_scratch_policy`
    uses) and reads the per-project ``review-policy.json`` from the bound
    state directory.  A missing file is the canonical ``RISK_ADAPTIVE``
    default; a present-but-corrupt file fails closed with
    :class:`ReviewPolicyError` because corrupt project authority is a
    fail-closed condition.

    CLI / Web run-creation paths call this exactly once before freezing
    the effective mode on the ``RunConfig``.  Detached supervisor
    children, retry, repair, and handoff inherit the frozen mode from
    the run row and MUST NOT re-read mutable project policy for an
    already-created Run.
    """
    from saberops.project import (
        ProjectIdentityError,
        bind_project_state_dir,
        compute_project_identity,
    )

    repo = Path(repo_path).expanduser()
    try:
        identity = compute_project_identity(repo)
        state_dir = bind_project_state_dir(identity)
    except ProjectIdentityError as exc:
        raise ReviewPolicyError(
            f"cannot resolve project state directory for "
            f"{Path(repo_path).expanduser()}: {exc}"
        ) from exc
    return load_project_review_policy_for_state_dir(state_dir)


def resolve_effective_review_policy_mode(
    *,
    project_mode: ReviewPolicyMode | None,
    explicit_run_mode: ReviewPolicyMode | None,
) -> ReviewPolicyMode:
    """Compute the effective review-policy mode for a new Run.

    Tightening-only merge: a run-level preference MUST NOT silently
    weaken a project-level ``ALWAYS_REQUIRED`` setting.  Precedence
    (high wins):

    * ``None`` from both -> canonical ``RISK_ADAPTIVE`` default;
    * ``ALWAYS_REQUIRED`` from either -> ``ALWAYS_REQUIRED``;
    * otherwise -> ``RISK_ADAPTIVE``.

    The resolved mode is FROZEN on the ``RunConfig`` at run creation and
    is never re-resolved for an already-created Run (the detached
    supervisor-launched owner must forward the exact value via
    ``--review-policy``).
    """
    if explicit_run_mode is ReviewPolicyMode.ALWAYS_REQUIRED:
        return ReviewPolicyMode.ALWAYS_REQUIRED
    if project_mode is ReviewPolicyMode.ALWAYS_REQUIRED:
        return ReviewPolicyMode.ALWAYS_REQUIRED
    return ReviewPolicyMode.RISK_ADAPTIVE


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    """Write ``payload`` deterministically and atomically.

    Mirrors the C13-B gate-scratch policy atomic-write discipline.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    tmp_path: Path | None = None
    fd: int | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix=_REVIEW_POLICY_TMP_PREFIX,
            suffix=".tmp",
            dir=str(path.parent),
        )
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        fd = None
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def set_project_review_policy_for_state_dir(
    state_dir: Path | str, mode: ReviewPolicyMode | str
) -> Path:
    """Validate and atomically persist the project's review-policy mode.

    Refuses any value other than a recognized :class:`ReviewPolicyMode`
    so a tightening-only invariant cannot be silently widened to a
    weaken-the-minimum mode.  The previous authoritative config is
    preserved byte-for-byte on failure (the temp file is removed and
    never replaces the destination).
    """
    resolved = _coerce_mode(mode, "policy document")
    path = project_review_policy_path(state_dir)
    payload: dict[str, object] = {
        "version": REVIEW_POLICY_SCHEMA_VERSION,
        "mode": resolved.value,
    }
    _atomic_write_json(path, payload)
    return path


# ---------------------------------------------------------------------------
# Diff helpers (deterministic, no model inference)
# ---------------------------------------------------------------------------


def diff_changed_paths(
    repo_path: Path | str, base_sha: str, candidate_sha: str
) -> tuple[str, ...]:
    """Return the deterministic set of changed paths in ``candidate_sha``.

    Uses ``git diff --name-only base..candidate``.  Raises
    :class:`ReviewPolicyError` on git failure so the caller can fail
    closed instead of guessing.  The classifier MUST NOT receive an
    empty tuple and silently treat that as LOW.
    """
    import subprocess

    repo = Path(repo_path)
    try:
        completed = subprocess.run(
            ["git", "diff", "--name-only", f"{base_sha}", f"{candidate_sha}"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReviewPolicyError(
            f"cannot compute changed paths for {repo}: {exc}"
        ) from exc
    if completed.returncode != 0:
        raise ReviewPolicyError(
            f"git diff {base_sha}..{candidate_sha} failed: {completed.stderr.strip()}"
        )
    paths: list[str] = []
    for line in completed.stdout.splitlines():
        candidate = line.strip()
        if not candidate or candidate.startswith(":"):
            continue
        paths.append(candidate)
    return tuple(paths)


# ---------------------------------------------------------------------------
# Optional helpers
# ---------------------------------------------------------------------------


def ensure_project_state_dir(state_dir: Path | str) -> Path:
    """Ensure ``state_dir`` exists for project-policy persistence."""
    path = Path(state_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def remove_project_review_policy_for_state_dir(state_dir: Path | str) -> bool:
    """Remove the persisted project review-policy file, if any.

    Used by tests; never silently discards on absence.  Returns True when
    a file was removed, False when none was present.
    """
    path = project_review_policy_path(state_dir)
    if not path.exists():
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


def validate_project_review_policy_invariants(
    decision: ReviewDecision,
) -> None:
    """Fail closed if a decision attempts to weaken a hard minimum.

    Public seam for tests and external callers that want to assert a
    freshly-built decision does not contradict C13-F hard minimums.
    Raises :class:`ReviewPolicyError` on violation.
    """
    if decision.is_autonomous and not decision.independent_review_required:
        raise ReviewPolicyError(
            "autonomous execution must require independent review"
        )
    if decision.owner_explicit_review and not decision.independent_review_required:
        raise ReviewPolicyError(
            "owner-explicit review must require independent review"
        )
    if (
        decision.review_policy_mode is ReviewPolicyMode.ALWAYS_REQUIRED
        and not decision.independent_review_required
    ):
        raise ReviewPolicyError(
            "ALWAYS_REQUIRED project policy must require independent review"
        )
    if (
        decision.classification.change_class is ChangeClass.HIGH_T3
        and not decision.independent_review_required
    ):
        raise ReviewPolicyError(
            "HIGH/T3 change class must require independent review"
        )
    if (
        decision.classification.risk_level is RiskLevel.HIGH
        and not decision.independent_review_required
    ):
        raise ReviewPolicyError(
            "HIGH risk_level must require independent review"
        )
    if decision.classification.changes_security_boundary and not (
        decision.independent_review_required
    ):
        raise ReviewPolicyError(
            "security-boundary change must require independent review"
        )
    if (
        decision.classification.changes_acceptance_or_gate_authority
        and not decision.independent_review_required
    ):
        raise ReviewPolicyError(
            "acceptance/gate-authority change must require independent review"
        )
    if (
        decision.classification.changes_publication_authority
        and not decision.independent_review_required
    ):
        raise ReviewPolicyError(
            "publication-authority change must require independent review"
        )
    if (
        decision.classification.changes_recovery_or_exactly_once
        and not decision.independent_review_required
    ):
        raise ReviewPolicyError(
            "recovery / exactly-once change must require independent review"
        )


__all__ = [
    "REVIEW_ADAPTIVE_VERSION",
    "REVIEW_POLICY_SCHEMA_VERSION",
    "ChangeClass",
    "ChangeClassification",
    "ReviewDecision",
    "ReviewPolicyError",
    "ReviewPolicyMode",
    "ReviewRequirementSource",
    "classify_change",
    "conservative_legacy_decision",
    "decide_review",
    "diff_changed_paths",
    "ensure_project_state_dir",
    "load_project_review_policy",
    "load_project_review_policy_for_state_dir",
    "project_review_policy_path",
    "remove_project_review_policy_for_state_dir",
    "resolve_effective_review_policy_mode",
    "set_project_review_policy_for_state_dir",
    "validate_project_review_policy_invariants",
]
