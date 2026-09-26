"""Canonical training/data-policy decision (R2-B single authority).

Worker routing (:mod:`saberops.routing`), C07 preflight
(:mod:`saberops.control_plane.preflight`) and reviewer dispatch
(:mod:`saberops.review`, via :mod:`saberops.dispatch`) must reach the
*same* candidate-level data-policy decision.  This module owns it.

Semantics (over the Run's already-frozen ``training_allowed`` bit and the
best frozen candidate training-requirement evidence):

* ``training_allowed == True``: data policy excludes nothing.  Other
  readiness/quota/capability/provider policies still apply normally.
* ``training_allowed == False``: ``FALSE`` is permitted; ``TRUE`` and
  ``UNKNOWN`` are denied.  ``UNKNOWN`` is never interpreted as safe
  (fail closed) under Denied.

Evidence resolution for one candidate (R2-B.1 static authority):

0. the static v1 compatibility knowledge (``TRAINING_REQUIRED_CANDIDATES``
   plus the ``"muse"`` heuristic, then the closed
   ``ALLOWED_CANDIDATES`` registry): known training-required is
   ``TRUE``, known static is ``FALSE``.  A known static classification
   is authoritative and returned exactly: frozen approvals, carried
   :attr:`WorkerCandidate.training_required` values, explicit
   requirements, and any other dynamic evidence never change it.
   Static approvals still carry ``binding_id`` and other frozen
   evidence; they are not an alternate training-policy authority.

For identities with no known static classification (non-static
owner-approved/discovered ids and unknown custom ids), first hit wins:

1. an explicitly passed, normalized ``TRUE``/``FALSE``/``UNKNOWN``
   requirement (callers that already resolved frozen evidence);
2. the frozen classification carried on the candidate itself
   (:attr:`WorkerCandidate.training_required`, attached at chain
   resolution from the Run's frozen routing snapshot);
3. the frozen routing snapshot's owner-approved record
   (``DynamicCandidate.training_required``) for this
   ``provider/model`` identity;
4. anything else is ``UNKNOWN`` (fail closed under Denied).

The static tables stay authoritative for v1 behind this function (R6
owns their retirement); no call site reimplements them.  Mutable live
discovery/catalog/routing state is never consulted: only the frozen
candidate, the frozen snapshot, and the frozen static tables.

The denial reason vocabulary is unchanged: data-policy denial stays
``TRAINING_PERMISSION_DENIED`` at the C07 boundary and projects to the
compatibility skip ``training_policy_excludes_model``
(``SKIP_TRAINING_POLICY``).  Denial is never a provider failure, a
worker failure, or capability-escalation evidence.
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
        return normalize_training_requirement(
            str(getattr(entry, "training_required", "UNKNOWN"))
        )
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
    if requires_training_permission(cid) or (
        model and requires_training_permission(model)
    ):
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
    carried = normalize_training_requirement(
        getattr(candidate, "training_required", None)
    )
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
    """Return True iff data policy permits the candidate under the Run bit.

    When ``training_allowed`` is True, data policy never excludes.
    Under Denied only ``FALSE`` is permitted; ``TRUE`` and ``UNKNOWN``
    are denied.  ``candidate`` may be a :class:`WorkerCandidate` or an
    already-resolved requirement string.
    """
    if training_allowed:
        return True
    if isinstance(candidate, str) and candidate.strip().upper() in _REQUIREMENTS:
        requirement = normalize_training_requirement(candidate)
    else:
        requirement = training_requirement_for(
            candidate, snapshot=snapshot, training_required=training_required
        )
    return requirement == REQUIREMENT_FALSE


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
