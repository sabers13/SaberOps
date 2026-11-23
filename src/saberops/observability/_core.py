"""Dispatch evidence and prompt capture at the adapter boundary.

This module contains no provider or model execution.  Dispatch evidence is
created by the callers at the real adapter boundary.  Reading that evidence
back is the job of the one run-report projection
(:mod:`saberops.observability.run_report`); nothing here reconstructs a run.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any

from saberops.db import current_iso_timestamp
from saberops.models import (
    DispatchEvidence,
    DispatchReason,
    DispatchRole,
    DispatchStage,
    QuotaRawSemantics,
    Tier,
    WorkerResult,
)
from saberops.quota import normalize_quota_observation

_SECTION_RE = re.compile(r"^\[([^\]\n]+)\]$", re.MULTILINE)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def prompt_sections(prompt: str) -> list[dict[str, Any]]:
    """Return a bounded, deterministic manifest of the exact prompt sections."""
    matches = list(_SECTION_RE.finditer(prompt))
    spans: list[tuple[str, int, int]] = []
    if matches:
        if matches[0].start() > 0:
            spans.append(("CONTRACT", 0, matches[0].start()))
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(prompt)
            spans.append((match.group(1), match.start(), end))
    else:
        spans.append(("CONTRACT", 0, len(prompt)))
    result: list[dict[str, Any]] = []
    for index, (name, start, end) in enumerate(spans):
        content = prompt[start:end]
        result.append(
            {
                "index": index,
                "name": name,
                "byte_count": len(content.encode("utf-8")),
                "sha256": _sha256_text(content),
            }
        )
    return result


def prompt_evidence(prompt: str, validation_marker: str = "VALIDATION POLICY:") -> dict[str, Any]:
    """Describe the exact model-visible prompt without storing a second copy."""
    return {
        "prompt_bytes": len(prompt.encode("utf-8")),
        "prompt_sha256": _sha256_text(prompt),
        "prompt_sections": prompt_sections(prompt),
        "validation_policy_occurrence_count": prompt.count(validation_marker),
    }


def new_dispatch_evidence(
    *,
    run_id: str,
    association_id: str,
    stage: DispatchStage,
    role: DispatchRole,
    tier: Tier,
    dispatch_reason: DispatchReason | str,
    requested_provider: str,
    requested_model: str,
    task: str,
    attempt_id: str | None = None,
    review_id: str | None = None,
    base_sha: str | None = None,
    inherited_checkpoint_sha: str | None = None,
    context_mode: str | None = None,
    raw_parent_included: bool | None = None,
    prompt_transport: str | None = None,
    package_id: str | None = None,
    plan_step_id: str | None = None,
    authority_mode: str | None = None,
    routing_snapshot_digest: str | None = None,
    control_plane_digest: str | None = None,
    prior_failure_refs: list[str] | None = None,
    handoff_id: str | None = None,
    binding_id: str | None = None,
    profile_id: str | None = None,
    quota_pool_id: str | None = None,
    reasoning_effort: str | None = None,
) -> DispatchEvidence:
    """Build evidence from the exact ``WorkerRequest.task`` about to be run."""
    evidence = prompt_evidence(task)
    return DispatchEvidence(
        id=f"dispatch_{uuid.uuid4().hex}",
        run_id=run_id,
        association_id=association_id,
        attempt_id=attempt_id,
        review_id=review_id,
        stage=stage,
        role=role,
        tier=tier,
        dispatch_reason=(
            dispatch_reason.value
            if isinstance(dispatch_reason, DispatchReason)
            else dispatch_reason
        ),
        requested_provider=requested_provider,
        requested_model=requested_model,
        context_mode=context_mode,
        base_sha=base_sha,
        inherited_checkpoint_sha=inherited_checkpoint_sha,
        package_id=package_id,
        plan_step_id=plan_step_id,
        authority_mode=authority_mode,
        routing_snapshot_digest=routing_snapshot_digest,
        control_plane_digest=control_plane_digest,
        selected_candidate=f"{requested_provider}/{requested_model}",
        prior_failure_refs_json=(
            json.dumps(sorted(prior_failure_refs), separators=(",", ":"))
            if prior_failure_refs
            else None
        ),
        handoff_id=handoff_id,
        prompt_bytes=int(evidence["prompt_bytes"]),
        prompt_sha256=str(evidence["prompt_sha256"]),
        prompt_sections_json=json.dumps(evidence["prompt_sections"], separators=(",", ":")),
        raw_parent_included=raw_parent_included,
        validation_policy_occurrence_count=int(evidence["validation_policy_occurrence_count"]),
        prompt_transport=prompt_transport,
        binding_id=binding_id,
        profile_id=profile_id,
        quota_pool_id=quota_pool_id,
        reasoning_effort=reasoning_effort,
        started_at=current_iso_timestamp(),
    )


def update_dispatch_from_result(evidence: DispatchEvidence, result: WorkerResult) -> None:
    """Apply provider result fields while retaining requested identity verbatim."""
    evidence.actual_provider = result.actual_provider
    evidence.actual_model = result.actual_model
    evidence.candidate_sha = result.commit_sha
    evidence.dispatch_outcome = (
        result.outcome.value if result.outcome is not None else result.status.value
    )
    evidence.interruption_reason = (
        result.interruption_reason.value if result.interruption_reason is not None else None
    )
    evidence.interruption_timestamp = (
        current_iso_timestamp() if result.interruption_reason is not None else None
    )
    evidence.completed_at = current_iso_timestamp()
    evidence.prompt_transport = result.prompt_transport_used or evidence.prompt_transport


def normalize_remaining_percent(
    raw_value: object,
    semantics: QuotaRawSemantics | str,
    raw_limit: object = None,
) -> float | None:
    """Normalize one explicitly tagged raw quota observation.

    The canonical implementation is shared with the database persistence
    boundary; no routing policy is applied here.
    """
    return normalize_quota_observation(raw_value, semantics, raw_limit)


__all__ = [
    "new_dispatch_evidence",
    "normalize_remaining_percent",
    "prompt_evidence",
    "prompt_sections",
    "update_dispatch_from_result",
]
