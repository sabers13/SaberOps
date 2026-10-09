"""Historical training evidence and compatibility helpers.

Training-based admission restrictions have been retired. All classifications
are permitted; stored requirement evidence remains readable for old snapshots.
Provider, binding, readiness, capability and quota checks remain independent.
"""

from __future__ import annotations

from typing import Any, Literal

TrainingRequirement = Literal["TRUE", "FALSE", "UNKNOWN"]

REQUIREMENT_TRUE: TrainingRequirement = "TRUE"
REQUIREMENT_FALSE: TrainingRequirement = "FALSE"
REQUIREMENT_UNKNOWN: TrainingRequirement = "UNKNOWN"

_REQUIREMENTS: frozenset[str] = frozenset({"TRUE", "FALSE", "UNKNOWN"})


def normalize_training_requirement(value: str | None) -> TrainingRequirement | None:
    """Normalize raw tri-state evidence to ``TRUE``/``FALSE``/``UNKNOWN``.

    Returns ``None`` for missing or unrecognized input so callers fall
    through to the next evidence rank instead of inventing meaning.
    Reuses the existing ``DynamicCandidate.training_required``
    vocabulary; no second tri-state type is introduced.
    """
    if value is None:
        return None
    cleaned = value.strip().upper()
    if cleaned in _REQUIREMENTS:
        return cleaned  # type: ignore[return-value]
    return None


def candidate_id_of(candidate: Any) -> str:
    """Return the canonical ``provider/model`` identity for a candidate."""
    provider = str(getattr(candidate, "provider", "")).strip()
    model = str(getattr(candidate, "model", "")).strip()
    if provider and "/" not in provider:
        return f"{provider}/{model}"
    if provider:
        return f"{provider}/{model}"
    return model


def approved_training_requirement(
    snapshot: Any | None, candidate_id: str
) -> TrainingRequirement | None:
    """Return the frozen owner-approved requirement for ``candidate_id``.

    Reads only the frozen routing snapshot's ``approved_candidates``
    records (never mutable live discovery/catalog/routing state).
    Returns ``None`` when the snapshot carries no record for the id.
    """
    if snapshot is None:
        return None
    approved = getattr(snapshot, "approved_candidates", None)
    if not approved:
        return None
    wanted = candidate_id.strip()
    for entry in approved:
        if getattr(entry, "candidate_id", None) != wanted:
            continue
        return normalize_training_requirement(str(getattr(entry, "training_required", "UNKNOWN")))
    return None


def static_training_requirement(candidate: Any) -> TrainingRequirement:
    """Resolve the frozen static v1 classification for a candidate.

    Known training-required (``TRAINING_REQUIRED_CANDIDATES`` plus the
    ``"muse"`` heuristic, on either the full id or the bare model, as
    the historical call sites checked) is ``TRUE``; members of the
    closed ``ALLOWED_CANDIDATES`` registry are ``FALSE``; anything else
    (custom/unknown ids with no evidence) is ``UNKNOWN`` so Denied
    fails closed instead of inferring safety.
    """
    from saberops.routing_config import (
        ALLOWED_CANDIDATES,
        requires_training_permission,
    )

    cid = candidate_id_of(candidate)
    model = str(getattr(candidate, "model", "")).strip()
    if requires_training_permission(cid) or (model and requires_training_permission(model)):
        return REQUIREMENT_TRUE
    if cid.strip().lower() in ALLOWED_CANDIDATES:
        return REQUIREMENT_FALSE
    return REQUIREMENT_UNKNOWN


def training_requirement_for(
    candidate: Any,
    *,
    snapshot: Any | None = None,
    training_required: str | None = None,
) -> TrainingRequirement:
    """Return the canonical frozen training requirement for a candidate.

    R2-B.1 precedence: a known static v1 classification (``TRUE`` or
    ``FALSE``) is authoritative and returned exactly before any
    dynamic/carried value that could contradict it.  Frozen dynamic
    evidence (explicit requirement, carried
    :attr:`WorkerCandidate.training_required`, snapshot approval) is
    consulted only when the identity has no known static
    classification; otherwise ``UNKNOWN`` (fail closed under Denied).
    """
    static = static_training_requirement(candidate)
    if static != REQUIREMENT_UNKNOWN:
        return static
    explicit = normalize_training_requirement(training_required)
    if explicit is not None:
        return explicit
    carried = normalize_training_requirement(getattr(candidate, "training_required", None))
    if carried is not None:
        return carried
    approved = approved_training_requirement(snapshot, candidate_id_of(candidate))
    if approved is not None:
        return approved
    return REQUIREMENT_UNKNOWN


def is_training_permitted(
    candidate: Any | TrainingRequirement | str | None,
    training_allowed: bool,
    *,
    snapshot: Any | None = None,
    training_required: str | None = None,
) -> bool:
    """Always permit: legacy training flags no longer restrict execution."""
    # Compatibility arguments retain historical evidence; this filter is retired.
    return True


def training_excluded(
    candidate: Any | TrainingRequirement | str | None,
    training_allowed: bool,
    *,
    snapshot: Any | None = None,
    training_required: str | None = None,
) -> bool:
    """Inverse of :func:`is_training_permitted` for filter call sites."""
    return not is_training_permitted(
        candidate, training_allowed, snapshot=snapshot, training_required=training_required
    )


__all__ = [
    "REQUIREMENT_FALSE",
    "REQUIREMENT_TRUE",
    "REQUIREMENT_UNKNOWN",
    "TrainingRequirement",
    "approved_training_requirement",
    "candidate_id_of",
    "is_training_permitted",
    "normalize_training_requirement",
    "static_training_requirement",
    "training_excluded",
    "training_requirement_for",
]
