"""Owner-selected exact Reviewer bindings and ordered fallback.

Legacy single selections still resolve exactly. An explicit ordered list
restricts dispatch to its chosen bindings; missing or unusable members may
be skipped, but an exhausted or explicitly empty list refuses review.
The ReviewEngine enforces readiness, budgets, anti-self-review and read-only
execution on every attempt and stops on a substantive verdict.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from saberops.control_plane.bindings import (
    BindingResolutionError,
    BindingRole,
    ExecutionBinding,
    resolve_execution_binding,
)
from saberops.models import WorkerCandidate

#: Stable reason vocabulary for reviewer-selection refusals.  These are
#: emitted verbatim into UI context and error surfaces; new values are
#: additive only.
REVIEWER_NOT_CONFIGURED = "REVIEWER_NOT_CONFIGURED"
REVIEWER_BINDING_NOT_FOUND = "REVIEWER_BINDING_NOT_FOUND"
REVIEWER_WRONG_ROLE = "REVIEWER_WRONG_ROLE"
REVIEWER_UNRESOLVABLE = "REVIEWER_UNRESOLVABLE"


class ReviewerSelectionStatus(StrEnum):
    """Outcome of :func:`select_reviewer_binding`."""

    RESOLVED = "RESOLVED"
    UNRESOLVED = "UNRESOLVED"
    NOT_CONFIGURED = "NOT_CONFIGURED"


@dataclass(frozen=True)
class ReviewerSelection:
    """The typed result of resolving the owner's Reviewer choice.

    ``RESOLVED`` carries the exact selected binding; ``UNRESOLVED``
    carries the typed reason so surfaces can answer *what* the owner
    asked for and *why* it cannot satisfy review; ``NOT_CONFIGURED``
    means the owner never selected one and the legacy ladder applies.
    """

    status: ReviewerSelectionStatus
    binding: ExecutionBinding | None
    requested_binding_id: str | None = None
    reason: str | None = None


class ReviewerSelectionError(ValueError):
    """Fail-closed refusal: a configured reviewer cannot authorize review."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def select_reviewer_binding(
    owner: Any,
    *,
    adapter_registry: Any | None = None,
) -> ReviewerSelection:
    """Resolve the owner's exact Reviewer choice against the registry.

    ``owner`` is the canonical :class:`OwnerBindings` document;
    ``adapter_registry`` supplies the real adapters (``.get(provider)``)
    used to prove the exact binding through the canonical C11-B
    resolver.  Pure over its inputs: no provider contact, no CLI, no
    live process beyond the adapter handles the caller hands in.
    """
    requested = owner.reviewer_binding_id
    if requested is None or not str(requested).strip():
        return ReviewerSelection(
            status=ReviewerSelectionStatus.NOT_CONFIGURED,
            binding=None,
            requested_binding_id=None,
            reason=REVIEWER_NOT_CONFIGURED,
        )
    wanted = str(requested).strip()
    binding = owner.registry.try_get(wanted)
    if binding is None:
        return ReviewerSelection(
            status=ReviewerSelectionStatus.UNRESOLVED,
            binding=None,
            requested_binding_id=wanted,
            reason=REVIEWER_BINDING_NOT_FOUND,
        )
    # C11-B BindingRole: only a REVIEWER-role binding may authorize the
    # reviewer slot.  A WORKER/ORCHESTRATOR binding with identical
    # provider/model text is a role violation, never reviewer authority.
    if binding.binding_role is not BindingRole.REVIEWER:
        return ReviewerSelection(
            status=ReviewerSelectionStatus.UNRESOLVED,
            binding=binding,
            requested_binding_id=wanted,
            reason=REVIEWER_WRONG_ROLE,
        )
    adapter = adapter_registry.get(binding.provider) if adapter_registry is not None else None
    try:
        if adapter is None:
            raise BindingResolutionError("NO_ADAPTER")
        resolve_execution_binding(
            registry=owner.registry,
            binding_id=binding.binding_id,
            adapter=adapter,
        )
    except BindingResolutionError:
        return ReviewerSelection(
            status=ReviewerSelectionStatus.UNRESOLVED,
            binding=binding,
            requested_binding_id=wanted,
            reason=REVIEWER_UNRESOLVABLE,
        )
    return ReviewerSelection(
        status=ReviewerSelectionStatus.RESOLVED,
        binding=binding,
        requested_binding_id=wanted,
        reason=None,
    )


def reviewer_candidate_for_dispatch(
    owner: Any,
    *,
    adapter_registry: Any | None = None,
) -> WorkerCandidate | None:
    """Return the exact reviewer candidate, or ``None`` when unconfigured.

    A configured-but-invalid selection raises
    :class:`ReviewerSelectionError` (fail closed, never fall back to
    another reviewer).  The returned candidate carries the exact
    provider, exact model, and exact ``binding_id`` of the selected
    ``REVIEWER`` binding for the existing
    ``ReviewEngine.review_run(reviewer_override=...)`` seam.
    """
    if owner.reviewer_binding_ids is not None:
        from dataclasses import replace

        for binding_id in owner.reviewer_binding_ids:
            selected = select_reviewer_binding(
                replace(owner, reviewer_binding_id=binding_id), adapter_registry=adapter_registry
            )
            if selected.status is ReviewerSelectionStatus.RESOLVED and selected.binding is not None:
                return WorkerCandidate(
                    provider=selected.binding.provider,
                    model=selected.binding.model,
                    binding_id=binding_id,
                )
        raise ReviewerSelectionError(
            REVIEWER_UNRESOLVABLE, "No chosen reviewer can authorize review"
        )
    selection = select_reviewer_binding(owner, adapter_registry=adapter_registry)
    if selection.status is ReviewerSelectionStatus.NOT_CONFIGURED:
        return None
    if selection.status is not ReviewerSelectionStatus.RESOLVED or selection.binding is None:
        raise ReviewerSelectionError(
            selection.reason or REVIEWER_UNRESOLVABLE,
            f"configured reviewer {selection.requested_binding_id or '(empty)'} "
            "cannot authorize review; refusing rather than substituting another reviewer",
        )
    binding = selection.binding
    return WorkerCandidate(
        provider=binding.provider,
        model=binding.model,
        binding_id=binding.binding_id,
    )


__all__ = [
    "REVIEWER_BINDING_NOT_FOUND",
    "REVIEWER_NOT_CONFIGURED",
    "REVIEWER_UNRESOLVABLE",
    "REVIEWER_WRONG_ROLE",
    "ReviewerSelection",
    "ReviewerSelectionError",
    "ReviewerSelectionStatus",
    "reviewer_candidate_for_dispatch",
    "select_reviewer_binding",
]
