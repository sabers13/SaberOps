"""C06 durable observability and deterministic run reconstruction.

This module contains no provider or model execution.  Dispatch evidence is
created by the callers at the real adapter boundary; snapshot functions only
read persisted state.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any

from orchestrator_mvp.db import Database, current_iso_timestamp
from orchestrator_mvp.models import (
    DispatchEvidence,
    DispatchReason,
    DispatchRole,
    DispatchStage,
    QuotaRawSemantics,
    Tier,
    WorkerResult,
)
from orchestrator_mvp.quota import normalize_quota_observation

_SECTION_RE = re.compile(r"^\[([^\]\n]+)\]$", re.MULTILINE)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _load_frozen_tool_contract(run: Any) -> dict[str, Any] | None:
    """Validate persisted tool evidence without consulting the owner registry."""
    if run.tool_contract_json is None and run.tool_contract_digest is None:
        return None
    if not run.tool_contract_json or not run.tool_contract_digest:
        raise ValueError(f"run '{run.id}' has an incomplete frozen tool contract")
    try:
        raw = json.loads(run.tool_contract_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"run '{run.id}' has malformed frozen tool contract JSON") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError(f"run '{run.id}' has unsupported frozen tool contract schema")
    bindings = raw.get("bindings")
    digest = raw.get("digest")
    refs = (
        [str(binding.get("ref")) for binding in bindings]
        if isinstance(bindings, list) and all(isinstance(binding, dict) for binding in bindings)
        else []
    )
    if (
        not isinstance(bindings, list)
        or any(not isinstance(binding, dict) for binding in bindings)
        or len(refs) != len(set(refs))
        or not isinstance(digest, str)
        or not digest
        or digest != hashlib.sha256(
            json.dumps(
                sorted(bindings, key=lambda item: str(item.get("ref", ""))),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        or run.tool_contract_digest != digest
    ):
        raise ValueError(f"run '{run.id}' has a frozen tool contract digest mismatch")
    return raw


def _load_frozen_gateway_contract(run: Any) -> dict[str, Any] | None:
    """Validate persisted C10 gateway evidence without invoking the gateway.

    Reconstruction must be a pure read of the persisted JSON; no network
    calls and no provider/model invocations. Legacy runs without a frozen
    gateway contract remain readable and return ``None``.
    """
    raw_json = getattr(run, "gateway_snapshot_json", None)
    raw_digest = getattr(run, "gateway_snapshot_digest", None)
    if raw_json is None and raw_digest is None:
        return None
    if not raw_json or not raw_digest:
        raise ValueError(f"run '{run.id}' has an incomplete frozen gateway contract")
    try:
        raw = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"run '{run.id}' has malformed frozen gateway contract JSON") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError(f"run '{run.id}' has unsupported frozen gateway schema")
    digest = raw.get("digest")
    if not isinstance(digest, str) or not digest or digest != raw_digest:
        raise ValueError(f"run '{run.id}' has a frozen gateway digest mismatch")
    return raw


def _as_gateway_evidence(row: Any) -> dict[str, Any]:
    """Project one ``gateway_evidence_json`` row into a snapshot field."""
    raw = getattr(row, "gateway_evidence_json", None)
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return {"_invalid_json": True}
    if not isinstance(decoded, dict):
        return {"_invalid_json": True}
    return decoded


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


def canonical_snapshot_json(snapshot: dict[str, Any]) -> bytes:
    """Serialize a snapshot using the stable C06 canonical JSON contract."""
    return json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _as_attempt(attempt: Any) -> dict[str, Any]:
    return {
        "id": attempt.id,
        "attempt_number": attempt.attempt_number,
        "provider": attempt.provider,
        "model": attempt.model,
        "status": attempt.status.value,
        "base_sha": attempt.base_sha,
        "commit_sha": attempt.commit_sha,
        "interruption_reason": attempt.interruption_reason,
        "created_at": attempt.created_at,
        "completed_at": attempt.completed_at,
    }


def _as_dispatch(row: DispatchEvidence) -> dict[str, Any]:
    values = dict(row.__dict__)
    values["stage"] = row.stage.value
    values["role"] = row.role.value
    values["prior_failure_refs"] = (
        json.loads(row.prior_failure_refs_json) if row.prior_failure_refs_json else []
    )
    values.pop("prior_failure_refs_json", None)
    values["prompt_sections"] = (
        json.loads(row.prompt_sections_json) if row.prompt_sections_json else []
    )
    values.pop("prompt_sections_json", None)
    return values


def _project_scope_snapshot(db: Database, run_id: str) -> dict[str, Any] | None:
    """Reconstruct C09 project-scope evidence from durable, plain JSON rows.

    This reads generic persisted JSON through the persistence layer only; it
    never imports project-scope runtime code, so the observability dependency
    surface stays unchanged.
    """
    row = db.get_run_project_scope(run_id)
    if row is None:
        return None
    try:
        scope = json.loads(str(row["current_scope_json"]))
    except json.JSONDecodeError:
        scope = {}
    try:
        initial = json.loads(str(row["initial_scope_json"]))
    except json.JSONDecodeError:
        initial = {}
    expansions = [
        {
            "attempt_id": expansion.get("attempt_id"),
            "path": expansion.get("path"),
            "symbol": expansion.get("symbol"),
            "reason": expansion.get("reason"),
            "before_digest": expansion.get("before_digest"),
            "after_digest": expansion.get("after_digest"),
            "created_at": expansion.get("created_at"),
        }
        for expansion in db.list_scope_expansions(run_id)
    ]
    return {
        "graph_digest": row["project_graph_digest"],
        "graph_source": row["graph_source"],
        "analyzer": row["analyzer"],
        "confidence": row["confidence"],
        "base_revision": row["base_revision"],
        "primary_module": (
            initial.get("primary_module") if isinstance(initial, dict) else None
        ),
        "included_modules": (
            initial.get("included_modules") if isinstance(initial, dict) else []
        ),
        "initial_source_paths": (
            initial.get("source_paths") if isinstance(initial, dict) else []
        ),
        "dependency_interface_paths": (
            initial.get("dependency_interface_paths") if isinstance(initial, dict) else []
        ),
        "relevant_tests": initial.get("test_paths") if isinstance(initial, dict) else [],
        "closure_complete": (
            initial.get("closure_complete") if isinstance(initial, dict) else False
        ),
        "truncation": initial.get("truncated") if isinstance(initial, dict) else False,
        "scope_reasons": initial.get("reasons") if isinstance(initial, dict) else [],
        "initial_scope_digest": row["initial_scope_digest"],
        "current_scope_digest": row["current_scope_digest"],
        "expansions": expansions,
        "expansion_count": len(expansions),
        "current_primary_module": (
            scope.get("primary_module") if isinstance(scope, dict) else None
        ),
        "current_source_paths": (
            scope.get("source_paths") if isinstance(scope, dict) else []
        ),
    }


def build_run_observability_snapshot(db: Database, run_id: str) -> dict[str, Any]:
    """Read and aggregate all durable C06 evidence for ``run_id``."""
    run = db.get_run(run_id)
    if run is None:
        raise ValueError(f"unknown run: {run_id}")
    dispatches = db.get_dispatch_evidence_for_run(run_id)
    usage = db.get_model_call_usage(run_id)
    attempts = db.get_attempts_for_run(run_id)
    checks = db.get_checks_for_run(run_id)
    reviews = db.get_reviews_for_run(run_id)
    findings = db.get_findings_for_run(run_id)
    checkpoints = db.get_worker_checkpoints_for_run(run_id)
    observations = db.get_quota_observations_for_run(run_id)
    handoffs = db.get_handoffs_for_run(run_id)
    control_plane_events = [
        {
            "event_type": event.event_type,
            "payload": event.payload,
            "created_at": event.created_at,
        }
        for event in db.get_events(run_id, limit=10000)
        if event.event_type in {"control_plane_snapshot", "control_plane_decision"}
    ]
    frozen_control_plane: dict[str, Any] | None = None
    if run.control_plane_snapshot_json is not None:
        try:
            raw_frozen = json.loads(run.control_plane_snapshot_json)
            if isinstance(raw_frozen, dict):
                frozen_control_plane = raw_frozen
        except json.JSONDecodeError:
            frozen_control_plane = None
    frozen_tool_contract = _load_frozen_tool_contract(run)
    usage_aggregate = db.aggregate_model_call_usage(run_id)
    frozen_gateway = _load_frozen_gateway_contract(run)
    gateway_evidence = [
        _as_gateway_evidence(row) for row in dispatches if row.gateway_evidence_json
    ]
    scope_snapshot = _project_scope_snapshot(db, run_id)
    usage_associations = {row.association_id for row in usage}
    dispatch_contract_complete = all(
        row.prompt_bytes is not None and row.prompt_sha256 is not None for row in dispatches
    )
    dispatch_outcome_complete = all(row.dispatch_outcome is not None for row in dispatches)
    usage_association_complete = all(row.association_id in usage_associations for row in dispatches)
    usage_coverage_complete = bool(usage_aggregate["coverage_complete"])
    reconstruction_complete = (
        dispatch_contract_complete and dispatch_outcome_complete and usage_association_complete
    )
    snapshot: dict[str, Any] = {
        "schema_version": 1,
        "run": {
            "id": run.id,
            "target_repo": run.target_repo,
            "base_commit": run.base_commit,
            "tier": run.tier.value,
            "status": run.status.value,
            "created_at": run.created_at,
            "completed_at": run.completed_at,
            "final_attempt_id": run.final_attempt_id,
            "objective_id": run.objective_id,
            "project_id": run.project_id,
            "routing_snapshot_digest": _routing_digest(run.routing_snapshot_json),
            "tool_contract_digest": run.tool_contract_digest,
            "tool_refs": (
                frozen_tool_contract.get("bindings", [])
                if frozen_tool_contract is not None
                else []
            ),
        },
        "candidate_lineage": [_as_attempt(attempt) for attempt in attempts],
        "dispatches": [_as_dispatch(row) for row in dispatches],
        "usage": {
            "rows": [
                dict(row.__dict__, stage=row.stage.value, tier=row.tier.value) for row in usage
            ],
            "aggregate": usage_aggregate,
        },
        "gate_evidence": [
            {
                "id": check.id,
                "attempt_id": check.attempt_id,
                "exit_code": check.exit_code,
                "passed": check.passed,
                "gate_command": check.gate_command,
                "created_at": check.created_at,
            }
            for check in checks
        ],
        "review_evidence": [
            {
                "id": review.id,
                "provider": review.provider,
                "model": review.model,
                "verdict": review.verdict.value,
                "summary": review.summary,
                "details": review.details,
                "created_at": review.created_at,
            }
            for review in reviews
        ],
        "review_findings": [dict(finding.__dict__) for finding in findings],
        "project_scope": scope_snapshot,
        "continuation": {
            "checkpoints": [dict(checkpoint.__dict__) for checkpoint in checkpoints],
            "handoffs": [
                {
                    "id": handoff.id,
                    "kind": handoff.kind,
                    "version": handoff.version,
                    "sha256": _sha256_text(handoff.content),
                    "byte_count": len(handoff.content.encode("utf-8")),
                    "created_at": handoff.created_at,
                }
                for handoff in handoffs
            ],
            "checkpoint_count": len(checkpoints),
            "recovery_dispatch_count": sum(
                1
                for row in dispatches
                if row.dispatch_reason
                in {
                    reason.value
                    for reason in (
                        DispatchReason.CONTINUATION_AFTER_QUOTA,
                        DispatchReason.CONTINUATION_AFTER_CONTEXT,
                        DispatchReason.CONTINUATION_AFTER_GRACEFUL_HANDOFF,
                        DispatchReason.COLD_TAKEOVER,
                    )
                }
            ),
        },
        "quota_observations": [dict(observation.__dict__) for observation in observations],
        "control_plane": control_plane_events,
        "frozen_control_plane": frozen_control_plane,
        "frozen_tool_contract": frozen_tool_contract,
        "frozen_gateway": frozen_gateway,
        "gateway_evidence": gateway_evidence,
        "reconstruction": {
            "dispatch_count": len(dispatches),
            "usage_row_count": len(usage),
            "dispatches_with_prompt_hash": sum(1 for row in dispatches if row.prompt_sha256),
            "dispatches_with_unknown_outcome": sum(
                1 for row in dispatches if row.dispatch_outcome is None
            ),
            "dispatch_contract_complete": dispatch_contract_complete,
            "dispatch_outcome_complete": dispatch_outcome_complete,
            "usage_association_complete": usage_association_complete,
            "usage_coverage_complete": usage_coverage_complete,
            "reconstruction_complete": reconstruction_complete,
            # Backward-compatible alias.  It intentionally means durable
            # graph reconstruction, not provider token coverage.
            "complete": reconstruction_complete,
            "unknown_fields": sorted(
                {
                    field
                    for row in dispatches
                    for field, value in row.__dict__.items()
                    if value is None
                    and field
                    in {
                        "actual_provider",
                        "actual_model",
                        "handoff_id",
                        "tool_schema_bytes",
                        "tool_description_bytes",
                        "tool_discovery_bytes",
                        "tool_result_bytes",
                        "tools_exposed_count",
                        "tools_actually_used_count",
                    }
                }
            ),
        },
    }
    return snapshot


def _routing_digest(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = raw
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = [
    "build_run_observability_snapshot",
    "canonical_snapshot_json",
    "new_dispatch_evidence",
    "normalize_remaining_percent",
    "prompt_evidence",
    "prompt_sections",
    "update_dispatch_from_result",
]
