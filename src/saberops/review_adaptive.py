"""Mandatory independent review decisions.

Review is required for every candidate that can be accepted.  There is no
risk classifier and no project review policy: a scheduler job, a plan lane
and a planner submission all produce the same requirement.  The only way a
run is frozen without review is the owner's explicit switch on a direct
``orch run`` (``--no-review``), recorded once on the run config as
``review_disabled``.

The persisted :class:`ReviewDecision` is still the single audit record of
"was review required for this candidate, and why?", consulted by acceptance
and supervisor reconstruction.  A malformed or legacy decision never turns
into a skip: readers fail closed as review-required.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

REVIEW_ADAPTIVE_VERSION: Final[str] = "review-adaptive/2"


class ReviewPolicyError(ValueError):
    """Fail-closed error for an invalid review decision."""


class ReviewRequirementSource(StrEnum):
    """Why a candidate's review requirement has the value it has.

    Only two answers survive: review is mandatory, or the owner explicitly
    turned it off for a direct manual run.  The persisted source keeps the
    two auditable.
    """

    MANDATORY = "MANDATORY"
    OWNER_DISABLED = "OWNER_DISABLED"


@dataclass(frozen=True)
class ReviewDecision:
    """Frozen, JSON-serializable review requirement for one candidate.

    Reconstruction with malformed JSON fails closed (treat as
    ``independent_review_required=True``).
    """

    decision_id: str
    independent_review_required: bool
    review_requirement_source: ReviewRequirementSource
    review_skip_reason: str
    schema_version: str

    def to_json(self) -> str:
        """Serialize deterministically for durable persistence."""
        payload = {
            "schema_version": self.schema_version,
            "decision_id": self.decision_id,
            "independent_review_required": self.independent_review_required,
            "review_requirement_source": self.review_requirement_source.value,
            "review_skip_reason": self.review_skip_reason,
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str) -> ReviewDecision:
        """Reconstruct a decision, accepting the pre-1.1 persisted shape.

        A legacy decision that recorded a skip for any reason other than the
        owner switch is rejected (:class:`ReviewPolicyError`) so its reader
        falls back to the conservative review-required decision instead of
        honouring a classifier skip that no longer exists.
        """
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ReviewPolicyError(f"malformed review decision JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ReviewPolicyError("review decision payload must be a JSON object")
        decision_id = data.get("decision_id")
        required = data.get("independent_review_required")
        skip_reason = data.get("review_skip_reason", "")
        if not isinstance(decision_id, str) or not decision_id:
            raise ReviewPolicyError("review decision decision_id must be a non-empty string")
        if not isinstance(required, bool):
            raise ReviewPolicyError(
                "review decision independent_review_required must be a boolean"
            )
        if not isinstance(skip_reason, str):
            raise ReviewPolicyError("review_skip_reason must be a string")
        source_token = str(data.get("review_requirement_source", "")).strip().upper()
        if required:
            source = ReviewRequirementSource.MANDATORY
        elif source_token == ReviewRequirementSource.OWNER_DISABLED.value and (
            skip_reason == "owner-disabled"
        ):
            source = ReviewRequirementSource.OWNER_DISABLED
        else:
            raise ReviewPolicyError(
                "review decision records a skip without the owner switch: "
                f"{source_token or '<missing source>'}"
            )
        schema_version = str(data.get("schema_version") or REVIEW_ADAPTIVE_VERSION)
        return cls(
            decision_id=decision_id,
            independent_review_required=required,
            review_requirement_source=source,
            review_skip_reason=skip_reason,
            schema_version=schema_version,
        )


def mandatory_review_decision(*, decision_id: str, owner_disabled: bool) -> ReviewDecision:
    """Return the frozen decision for a candidate-ready run.

    ``owner_disabled`` is only ever true for a direct manual ``orch run``
    that carried ``--no-review``; every other entry point leaves it false.
    """
    if owner_disabled:
        return ReviewDecision(
            decision_id=decision_id,
            independent_review_required=False,
            review_requirement_source=ReviewRequirementSource.OWNER_DISABLED,
            review_skip_reason="owner-disabled",
            schema_version=REVIEW_ADAPTIVE_VERSION,
        )
    return ReviewDecision(
        decision_id=decision_id,
        independent_review_required=True,
        review_requirement_source=ReviewRequirementSource.MANDATORY,
        review_skip_reason="",
        schema_version=REVIEW_ADAPTIVE_VERSION,
    )


def conservative_legacy_decision() -> ReviewDecision:
    """Return the fail-closed decision for a run with no usable record."""
    return ReviewDecision(
        decision_id="legacy-conservative",
        independent_review_required=True,
        review_requirement_source=ReviewRequirementSource.MANDATORY,
        review_skip_reason="",
        schema_version=REVIEW_ADAPTIVE_VERSION,
    )


def validate_review_decision_invariants(decision: ReviewDecision) -> None:
    """Fail closed if a decision weakens the mandatory-review contract.

    The owner switch is the only recorded exception, and it must say so.
    Raises :class:`ReviewPolicyError` on violation.
    """
    if decision.review_requirement_source is ReviewRequirementSource.OWNER_DISABLED:
        if (
            decision.independent_review_required
            or decision.review_skip_reason != "owner-disabled"
        ):
            raise ReviewPolicyError("Invalid owner-disabled review decision")
        return
    if not decision.independent_review_required:
        raise ReviewPolicyError("independent review is mandatory for a reviewable candidate")


__all__ = [
    "REVIEW_ADAPTIVE_VERSION",
    "ReviewDecision",
    "ReviewPolicyError",
    "ReviewRequirementSource",
    "conservative_legacy_decision",
    "mandatory_review_decision",
    "validate_review_decision_invariants",
]
