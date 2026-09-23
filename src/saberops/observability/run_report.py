"""C16-B1 canonical run report and live monitoring projection.

Product-owned, strictly read-only projection over already-persisted run
evidence.  This module performs no model calls, no Git reads, no network
I/O and no database writes: every field is derived from durable rows
(``runs``, ``attempts``, ``dispatch_evidence``, ``model_call_usage``,
``checks``, ``reviews``, ``events``, ``worker_checkpoints``) plus one
explicitly labelled point-in-time filesystem observation (attempt
worktree existence).

Evidence semantics (fail closed, never inferred):

* ``UNKNOWN`` (see :mod:`saberops.replay.contracts`) stays ``UNKNOWN``.
  Missing scalars are never rendered as zero, empty, or ``False``.
* Requested provider/model (the frozen owner routing intent from the
  run ``config_json``) is never presented as actual provider/model.
* Provider usage events (``model_call_usage`` rows) are never conflated
  with model dispatch counts (``dispatch_evidence`` rows); both are
  reported side by side.
* Retry dispatches (``DispatchStage.RETRY``) are never conflated with
  repair dispatches (``DispatchStage.REPAIR``); repair rounds are not
  derived from retry counts.
* Acceptance is read exclusively from the durable ``run_accepted``
  event.  The current target HEAD is never read and never consulted, so
  a later owner commit cannot rewrite historical acceptance, and owner
  intervention is never inferred.
* The current stage prefers explicit persisted lifecycle events over
  heuristics; when no explicit signal exists the stage is ``UNKNOWN``.

The canonical JSON rendering (:func:`canonical_report_json`) is
deterministic: the same persisted state always yields byte-identical
output, so the report is suitable as the SaberOps-derived portion of a
future append-only benchmark record.  Wall-clock "now" never enters the
canonical report; the live monitor computes elapsed time itself at
render time (outside the canonical payload).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from saberops.db import Database
from saberops.models import (
    TERMINAL_RUN_STATUSES,
    Attempt,
    DispatchStage,
    Run,
)
from saberops.observability._core import canonical_snapshot_json

#: Canonical schema marker for the C16-B1 run report projection.
REPORT_SCHEMA: str = "saberops.run_report/v1"
#: Integer schema version carried inside the payload for forward migration.
REPORT_SCHEMA_VERSION: int = 1

#: Sentinel string denoting an unavailable or unmeasured metric value.
#: Mirrors ``saberops.replay.contracts.UNKNOWN`` by value (pinned equal
#: by test) without importing the replay module: observability must not
#: depend on replay, so the normal-run path stays unwired from it.
UNKNOWN: str = "UNKNOWN"

#: Lifecycle event types that carry an explicit pipeline-stage signal, in
#: the order they normally occur.  Event types not listed here never move
#: the reported stage, so newly added diagnostic events cannot silently
#: rewrite the live view.
_STAGE_BY_EVENT: dict[str, str] = {
    "run_created": "created",
    "run_ready": "created",
    "run_started": "worker",
    "attempt_started": "worker",
    "worker_started": "worker",
    "worker_checkpoint_created": "worker",
    "worker_interrupted_checkpoint_created": "worker",
    "worker_completed": "worker_done",
    "attempt_completed": "worker_done",
    "attempt_succeeded": "worker_done",
    "candidate_lineage_advanced": "worker_done",
    "gate_started": "gate",
    "gate_completed": "gate_done",
    "gate_passed": "gate_done",
    "gate_failed": "gate_done",
    "gate_receipt": "gate_done",
    "review_dispatch_started": "review",
    "review_started": "review",
    "final_review_started": "review",
    "review_completed": "review_done",
    "repair_started": "repair",
    "repair_completed": "repair_done",
    "attempt_recovered_cold_takeover": "recovery",
    "worker_resumed": "recovery",
    "run_completed": "terminal:COMPLETED",
    "run_failed": "terminal:FAILED",
    "run_cancelled": "terminal:CANCELLED",
    "run_accepted": "accepted",
}

#: Dispatch outcome that marks a provider/runtime infrastructure failure
#: rather than capability-bearing evidence.
_PROVIDER_FAILURE_OUTCOME: str = "PROVIDER_OR_RUNTIME_FAILURE"

#: Worker-side dispatch stages (implementation work, including corrective
#: re-dispatches).  Reviewer dispatches are reported separately and never
#: folded into these counts.
_WORKER_STAGES: frozenset[str] = frozenset(
    {
        DispatchStage.IMPLEMENTATION.value,
        DispatchStage.RETRY.value,
        DispatchStage.REPAIR.value,
    }
)


def saberops_version() -> str:
    """Return the installed SaberOps version, or ``UNKNOWN``.

    The installed distribution metadata is authoritative for "which
    SaberOps produced this report" because it identifies the running
    code.  It is read-only package metadata, never execution state.
    """
    try:
        import importlib.metadata as _metadata

        version = _metadata.version("saberops")
    except Exception:
        version = ""
    if version:
        return version
    try:
        from saberops import __version__ as fallback

        if isinstance(fallback, str) and fallback:
            return fallback
    except Exception:
        pass
    return UNKNOWN


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _wall_seconds(start: str | None, end: str | None) -> float | str:
    """Return rounded wall seconds between two ISO timestamps, or UNKNOWN."""
    begin = _parse_iso(start)
    finish = _parse_iso(end)
    if begin is None or finish is None:
        return UNKNOWN
    delta = (finish - begin).total_seconds()
    if delta < 0:
        return UNKNOWN
    return round(delta, 3)


def _requested_identity(run: Run, gaps: list[str]) -> dict[str, str]:
    """Project the frozen owner routing intent; never actual execution."""
    requested_provider: str = UNKNOWN
    requested_model: str = UNKNOWN
    if run.config_json:
        try:
            parsed = json.loads(run.config_json)
        except (TypeError, ValueError, json.JSONDecodeError):
            gaps.append("run config_json is malformed; requested provider/model UNKNOWN")
            return {
                "requested_provider": requested_provider,
                "requested_model": requested_model,
            }
        if isinstance(parsed, dict):
            provider = parsed.get("provider_override")
            model = parsed.get("model_override")
            if isinstance(provider, str) and provider:
                requested_provider = provider
            if isinstance(model, str) and model:
                requested_model = model
    return {
        "requested_provider": requested_provider,
        "requested_model": requested_model,
    }


def _sorted_dispatches(db: Database, run_id: str) -> list[Any]:
    """Return dispatch evidence rows in deterministic chronological order."""
    rows = db.get_dispatch_evidence_for_run(run_id)
    return sorted(rows, key=lambda row: (row.started_at or "", row.id))


def _actual_identity(
    dispatches: list[Any], gaps: list[str]
) -> dict[str, Any]:
    """Project the provider-reported execution identity.

    The top-level actuals come from the latest completed worker-side
    dispatch that carries provider-reported identity.  When no worker
    dispatch carries one, both fields stay ``UNKNOWN``.  ``MIXED``
    signals that distinct worker dispatches reported distinct
    identities, so the top-level value is the latest one and the full
    list below must be consulted.
    """
    identities: list[dict[str, str]] = []
    for row in dispatches:
        if row.stage.value not in _WORKER_STAGES:
            continue
        identities.append(
            {
                "dispatch_id": row.id,
                "stage": row.stage.value,
                "requested": f"{row.requested_provider}/{row.requested_model}",
                "actual_provider": row.actual_provider or UNKNOWN,
                "actual_model": row.actual_model or UNKNOWN,
                "outcome": row.dispatch_outcome or UNKNOWN,
            }
        )
    known = [
        (entry["actual_provider"], entry["actual_model"])
        for entry in identities
        if entry["actual_provider"] != UNKNOWN and entry["actual_model"] != UNKNOWN
    ]
    if not known:
        if identities:
            gaps.append(
                "no worker dispatch carries provider-reported identity; "
                "actual provider/model UNKNOWN"
            )
        else:
            gaps.append("no worker dispatches observed; actual provider/model UNKNOWN")
        return {
            "actual_provider": UNKNOWN,
            "actual_model": UNKNOWN,
            "actual_identity_status": UNKNOWN,
            "dispatch_identities": identities,
        }
    latest = known[-1]
    status = "KNOWN"
    if len(set(known)) > 1:
        status = "MIXED"
        gaps.append(
            "worker dispatches report distinct actual identities; "
            "top-level actuals are the latest observed"
        )
    return {
        "actual_provider": latest[0],
        "actual_model": latest[1],
        "actual_identity_status": status,
        "dispatch_identities": identities,
    }


def _candidate_sha(
    run: Run, attempts: list[Attempt], gaps: list[str]
) -> dict[str, str]:
    """Project the candidate SHA with an explicit durable source.

    Precedence is fully durable: the run's frozen ``final_attempt_id``
    first, then the latest successful attempt, then the latest attempt
    carrying any SHA.  ``UNKNOWN`` when nothing durable names one.
    """
    by_id = {attempt.id: attempt for attempt in attempts}
    final = by_id.get(run.final_attempt_id or "")
    if final is not None and final.commit_sha:
        return {"candidate_sha": final.commit_sha, "candidate_source": "final_attempt"}
    successful = [a for a in attempts if a.commit_sha and a.status.value == "SUCCESS"]
    if successful:
        latest = max(successful, key=lambda a: (a.attempt_number, a.id))
        return {"candidate_sha": str(latest.commit_sha), "candidate_source": "latest_success"}
    with_sha = [a for a in attempts if a.commit_sha]
    if with_sha:
        latest = max(with_sha, key=lambda a: (a.attempt_number, a.id))
        return {"candidate_sha": str(latest.commit_sha), "candidate_source": "latest_with_sha"}
    gaps.append("no durable attempt carries a candidate SHA")
    return {"candidate_sha": UNKNOWN, "candidate_source": "none"}


def _current_stage(run: Run, db: Database, run_id: str, gaps: list[str]) -> dict[str, str]:
    """Derive the live pipeline stage from explicit persisted signals.

    Terminal run status is monotonic and always wins.  Otherwise the
    latest stage-bearing lifecycle event wins.  Anything else is
    ``UNKNOWN`` with an explicit gap; heuristics are never consulted.
    """
    if run.status in TERMINAL_RUN_STATUSES:
        return {
            "current_stage": f"terminal:{run.status.value}",
            "current_stage_source": "run_row:status",
        }
    try:
        events = db.get_events(run_id, limit=10000)
    except Exception:
        gaps.append("event log unreadable; current stage UNKNOWN")
        return {"current_stage": UNKNOWN, "current_stage_source": "none"}
    for event in reversed(events):
        stage = _STAGE_BY_EVENT.get(event.event_type)
        if stage is not None and not stage.startswith("terminal:"):
            return {
                "current_stage": stage,
                "current_stage_source": f"{event.event_type}#{event.id}",
            }
    gaps.append("no stage-bearing lifecycle event persisted; current stage UNKNOWN")
    return {"current_stage": UNKNOWN, "current_stage_source": "none"}


def _review_section(
    run: Run, db: Database, run_id: str, gaps: list[str]
) -> dict[str, Any]:
    """Project review necessity, rounds, and verdicts from durable rows."""
    required: bool | str = UNKNOWN
    requirement_source: str = UNKNOWN
    skip_reason: str = ""
    reason: str = UNKNOWN
    decision_json = db.get_review_risk_decision_json(run_id)
    if decision_json is not None:
        try:
            parsed = json.loads(decision_json)
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, dict):
            raw_required = parsed.get("independent_review_required")
            if isinstance(raw_required, bool):
                required = raw_required
                source = parsed.get("review_requirement_source")
                requirement_source = source if isinstance(source, str) and source else UNKNOWN
                raw_skip = parsed.get("review_skip_reason")
                skip_reason = raw_skip if isinstance(raw_skip, str) else ""
                classification = parsed.get("classification")
                if isinstance(classification, dict):
                    raw_reason = classification.get("reason")
                    reason = raw_reason if isinstance(raw_reason, str) and raw_reason else UNKNOWN
                    if required is False and not skip_reason:
                        risk = classification.get("risk_level")
                        risk_text = risk if isinstance(risk, str) and risk else UNKNOWN
                        skip_reason = f"{risk_text} ({reason})" if reason != UNKNOWN else risk_text
            else:
                # Malformed persisted decision fails closed, mirroring the
                # accept engine: an unreadable authority marker must never
                # become a silent skip.
                required = True
                requirement_source = "malformed_persisted_decision_fail_closed"
                gaps.append(
                    "persisted review decision is malformed; "
                    "treating review as required (fail closed)"
                )
        else:
            required = True
            requirement_source = "malformed_persisted_decision_fail_closed"
            gaps.append(
                "persisted review decision is malformed; "
                "treating review as required (fail closed)"
            )
    else:
        raw_config = run.config_json
        if raw_config:
            try:
                parsed_config = json.loads(raw_config)
            except (TypeError, ValueError, json.JSONDecodeError):
                parsed_config = None
            if isinstance(parsed_config, dict) and isinstance(
                parsed_config.get("review_enabled"), bool
            ):
                required = bool(parsed_config["review_enabled"])
                requirement_source = "legacy_review_enabled_config"
                if required is False:
                    skip_reason = "legacy review_enabled=false"
                    reason = "legacy review_enabled=false"
            else:
                gaps.append(
                    "no frozen review decision and no legacy review_enabled flag; "
                    "review necessity UNKNOWN"
                )
        else:
            gaps.append(
                "no frozen review decision and no persisted run config; "
                "review necessity UNKNOWN"
            )
    reviews = db.get_reviews_for_run(run_id)
    rounds = [
        {
            "id": review.id,
            "provider": review.provider,
            "model": review.model,
            "verdict": review.verdict.value,
            "created_at": review.created_at,
        }
        for review in reviews
    ]
    verdicts = [review.verdict.value for review in reviews]
    if required is True and not reviews:
        gaps.append("independent review is required but no review row is persisted")
    return {
        "review_required": required,
        "review_requirement_source": requirement_source,
        "review_reason": reason,
        "review_skip_reason": skip_reason,
        "rounds": len(rounds),
        "verdicts": verdicts,
        "round_detail": rounds,
    }


def _gate_section(
    run: Run, db: Database, run_id: str, gaps: list[str]
) -> dict[str, Any]:
    """Project gate evidence from check rows and gate-receipt events.

    Per-attempt check rows carry pass/fail but no duration; durations
    are observable only through durable ``gate_receipt`` events.  The
    two sources are reported side by side and never joined by
    heuristics.
    """
    checks = db.get_checks_for_run(run_id)
    check_rows = [
        {
            "id": check.id,
            "attempt_id": check.attempt_id,
            "gate_command": check.gate_command,
            "exit_code": check.exit_code,
            "passed": check.passed,
            "verdict": "PASS" if check.passed else "FAIL",
            "created_at": check.created_at,
            "duration_seconds": UNKNOWN,
        }
        for check in checks
    ]
    try:
        events = db.get_events(run_id, limit=10000)
    except Exception:
        events = []
    receipts: list[dict[str, Any]] = []
    for event in events:
        if event.event_type != "gate_receipt":
            continue
        payload = event.payload if isinstance(event.payload, dict) else {}
        duration = payload.get("duration_seconds")
        receipts.append(
            {
                "event_id": event.id,
                "candidate_sha": payload.get("candidate_sha") or UNKNOWN,
                "gate_command": payload.get("gate_command") or UNKNOWN,
                "exit_code": payload.get("exit_code", UNKNOWN),
                "verdict": "PASS"
                if payload.get("exit_code") == 0
                and not payload.get("timed_out", False)
                and not payload.get("launch_failed", False)
                and not payload.get("termination_incomplete", False)
                and not payload.get("evidence_persistence_failed", False)
                else "FAIL",
                "duration_seconds": duration if isinstance(duration, (int, float)) else UNKNOWN,
                "created_at": event.created_at,
            }
        )
    if checks and not receipts:
        gaps.append("gate durations UNKNOWN: no durable gate_receipt event persisted")
    if not checks and run.status in TERMINAL_RUN_STATUSES:
        gaps.append("no gate check rows persisted for a terminal run")
    passed = sum(1 for row in check_rows if row["passed"])
    return {
        "gate_command": run.gate_command,
        "checks": check_rows,
        "receipts": receipts,
        "summary": {
            "check_total": len(check_rows),
            "check_passed": passed,
            "check_failed": len(check_rows) - passed,
            "receipt_total": len(receipts),
        },
    }


def _usage_section(db: Database, run_id: str, gaps: list[str]) -> dict[str, Any]:
    """Aggregate provider usage events with explicit unknown semantics.

    Token totals sum only provider-reported known values; a counter with
    no known observation stays ``UNKNOWN`` (never zero).  The event
    count is reported separately from dispatch counts and the two are
    never combined.
    """
    rows = db.get_model_call_usage(run_id)

    def _sum_known(field: str) -> int | str:
        values = [getattr(row, field) for row in rows if getattr(row, field) is not None]
        return sum(values) if values else UNKNOWN

    known = [row for row in rows if row.input_tokens is not None or row.output_tokens is not None]
    unknown = [row for row in rows if row.input_tokens is None and row.output_tokens is None]
    by_stage: dict[str, int] = {}
    for row in rows:
        key = row.stage.value
        by_stage[key] = by_stage.get(key, 0) + 1
    if unknown:
        gaps.append(
            f"{len(unknown)} provider usage event(s) carry no token counters; "
            "totals cover known values only"
        )
    if not rows:
        gaps.append("no provider usage events persisted")
    return {
        "provider_usage_event_count": len(rows),
        "input_tokens": _sum_known("input_tokens"),
        "output_tokens": _sum_known("output_tokens"),
        "cache_read_tokens": _sum_known("cache_read_tokens"),
        "cache_write_tokens": _sum_known("cache_write_tokens"),
        "reasoning_tokens": _sum_known("reasoning_tokens"),
        "total_tokens": _sum_known("total_tokens"),
        "calls_with_known_usage": len(known),
        "calls_with_unknown_usage": len(unknown),
        "events_by_stage": dict(sorted(by_stage.items())),
    }


def _acceptance_section(db: Database, run_id: str, gaps: list[str]) -> dict[str, Any]:
    """Project acceptance exclusively from the durable run_accepted event.

    The live target HEAD is never read: historical acceptance cannot be
    rewritten by a later owner commit, and owner intervention is never
    inferred from repository state.
    """
    try:
        accepted_events = db.get_events_by_type(run_id, "run_accepted")
    except Exception:
        gaps.append("acceptance event log unreadable; acceptance UNKNOWN")
        return {
            "acceptance_status": UNKNOWN,
            "accepted_candidate_sha": UNKNOWN,
            "acceptance_event_id": None,
            "accepted_at": UNKNOWN,
            "final_branch": UNKNOWN,
        }
    if not accepted_events:
        return {
            "acceptance_status": "NOT_ACCEPTED",
            "accepted_candidate_sha": UNKNOWN,
            "acceptance_event_id": None,
            "accepted_at": UNKNOWN,
            "final_branch": UNKNOWN,
        }
    latest = max(accepted_events, key=lambda event: event.id)
    payload = latest.payload if isinstance(latest.payload, dict) else {}
    candidate = payload.get("candidate_sha")
    if not isinstance(candidate, str) or not candidate:
        gaps.append(
            f"run_accepted event #{latest.id} carries no candidate SHA; "
            "acceptance recorded but candidate UNKNOWN"
        )
        candidate = UNKNOWN
    branch = payload.get("final_branch")
    return {
        "acceptance_status": "ACCEPTED",
        "accepted_candidate_sha": candidate,
        "acceptance_event_id": latest.id,
        "accepted_at": latest.created_at,
        "final_branch": branch if isinstance(branch, str) and branch else UNKNOWN,
    }


def _workspace_section(
    run: Run,
    db: Database,
    run_id: str,
    attempts: list[Attempt],
    gaps: list[str],
) -> dict[str, Any]:
    """Project workspace evidence from durable rows plus one labelled observation.

    Changed paths union durable ``worker_checkpoints`` rows and
    ``changed_paths`` payloads on the event log.  Worktree existence is
    a point-in-time filesystem observation (read-only), explicitly
    labelled as such: it is not durable evidence.  Cleanup has no
    durable record anywhere in the execution path, so cleanup state is
    always ``UNKNOWN``.
    """
    changed: set[str] = set()
    for checkpoint in db.get_worker_checkpoints_for_run(run_id):
        for path in checkpoint.changed_paths:
            changed.add(str(path))
    try:
        events = db.get_events(run_id, limit=10000)
    except Exception:
        events = []
    for event in events:
        payload = event.payload if isinstance(event.payload, dict) else None
        if not payload:
            continue
        raw_paths = payload.get("changed_paths")
        if isinstance(raw_paths, list):
            for path in raw_paths:
                if isinstance(path, str) and path:
                    changed.add(path)
    if changed:
        changed_paths: list[str] | str = sorted(changed)
    else:
        changed_paths = UNKNOWN
        gaps.append("no durable row names changed paths")
    worktrees: list[dict[str, Any]] = []
    for attempt in attempts:
        exists: bool | str
        try:
            exists = Path(attempt.worktree_path).exists() if attempt.worktree_path else UNKNOWN
        except (OSError, ValueError):
            exists = UNKNOWN
        worktrees.append(
            {
                "attempt_number": attempt.attempt_number,
                "worktree_path": attempt.worktree_path or UNKNOWN,
                "branch": attempt.branch_name or UNKNOWN,
                "exists_now": exists,
            }
        )
    gaps.append(
        "cleanup state is not durably recorded by the execution path; "
        "worktree existence above is a point-in-time observation only"
    )
    return {
        "changed_paths": changed_paths,
        "worktrees": worktrees,
        "worktree_observation_note": "exists_now is a point-in-time observation, not evidence",
        "cleanup_state": UNKNOWN,
    }


def _warnings(
    dispatches: list[Any],
    reviews: list[Any],
    db: Database,
    run_id: str,
) -> list[str]:
    """Collect provider/runtime failures and review execution failures."""
    warnings: list[str] = []
    for row in dispatches:
        if row.dispatch_outcome == _PROVIDER_FAILURE_OUTCOME:
            warnings.append(
                f"provider/runtime failure: {row.stage.value} dispatch "
                f"{row.id} ({row.requested_provider}/{row.requested_model})"
            )
        if row.dispatch_outcome is None and row.completed_at is not None:
            warnings.append(
                f"dispatch {row.id} completed without a recorded outcome"
            )
    for review in reviews:
        if review.verdict.value in ("REVIEW_EXECUTION_FAILED", "REVIEW_UNAVAILABLE"):
            warnings.append(
                f"review {review.id} did not produce a substantive verdict "
                f"({review.verdict.value})"
            )
    try:
        events = db.get_events(run_id, limit=10000)
    except Exception:
        return warnings
    for event in events:
        if event.event_type in (
            "gate_runner_infrastructure_error",
            "gate_runner_unexpected_exception",
            "gate_preflight_blocked",
            "no_eligible_candidate",
            "worker_failed",
            "run_exception",
        ):
            warnings.append(f"{event.event_type}#{event.id}")
    return sorted(set(warnings))


def build_run_report(db: Database, run_id: str) -> dict[str, Any]:
    """Build the canonical read-only run report for ``run_id``.

    Pure read of persisted evidence; raises ``ValueError`` for an
    unknown run.  The returned mapping serializes deterministically via
    :func:`canonical_report_json`.
    """
    run = db.get_run(run_id)
    if run is None:
        raise ValueError(f"unknown run: {run_id}")
    gaps: list[str] = []
    attempts = db.get_attempts_for_run(run_id)
    dispatches = _sorted_dispatches(db, run_id)
    reviews = db.get_reviews_for_run(run_id)

    requested = _requested_identity(run, gaps)
    actual = _actual_identity(dispatches, gaps)
    candidate = _candidate_sha(run, attempts, gaps)
    stage = _current_stage(run, db, run_id, gaps)
    review = _review_section(run, db, run_id, gaps)
    gate = _gate_section(run, db, run_id, gaps)
    usage = _usage_section(db, run_id, gaps)
    acceptance = _acceptance_section(db, run_id, gaps)
    workspace = _workspace_section(run, db, run_id, attempts, gaps)
    warnings = _warnings(dispatches, reviews, db, run_id)

    worker_dispatches = sum(1 for row in dispatches if row.stage == DispatchStage.IMPLEMENTATION)
    retry_dispatches = sum(1 for row in dispatches if row.stage == DispatchStage.RETRY)
    repair_dispatches = sum(1 for row in dispatches if row.stage == DispatchStage.REPAIR)
    reviewer_dispatches = sum(1 for row in dispatches if row.stage == DispatchStage.REVIEW)
    try:
        events = db.get_events(run_id, limit=10000)
    except Exception:
        events = []
    resume_count = sum(1 for event in events if event.event_type == "worker_resumed")

    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "schema_version": REPORT_SCHEMA_VERSION,
        "identity": {
            "run_id": run.id,
            "project_id": run.project_id or UNKNOWN,
            "saberops_version": saberops_version(),
            "target_repo": run.target_repo,
            "base_sha": run.base_commit,
            "candidate_sha": candidate["candidate_sha"],
            "candidate_source": candidate["candidate_source"],
        },
        "lifecycle": {
            "status": run.status.value,
            "current_stage": stage["current_stage"],
            "current_stage_source": stage["current_stage_source"],
            "created_at": run.created_at,
            "completed_at": run.completed_at or UNKNOWN,
            "total_wall_seconds": _wall_seconds(run.created_at, run.completed_at),
        },
        "execution": {
            "attempt_count": len(attempts),
            "worker_dispatch_count": worker_dispatches,
            "retry_dispatch_count": retry_dispatches,
            "repair_dispatch_count": repair_dispatches,
            "reviewer_dispatch_count": reviewer_dispatches,
            "resume_count": resume_count,
        },
        "worker": {
            "requested_provider": requested["requested_provider"],
            "requested_model": requested["requested_model"],
            "actual_provider": actual["actual_provider"],
            "actual_model": actual["actual_model"],
            "actual_identity_status": actual["actual_identity_status"],
            "dispatch_identities": actual["dispatch_identities"],
        },
        "review": review,
        "gate": gate,
        "usage": usage,
        "acceptance": acceptance,
        "workspace": workspace,
        "warnings": warnings,
        "evidence_gaps": sorted(set(gaps)),
    }
    return report


def canonical_report_json(report: dict[str, Any]) -> bytes:
    """Serialize a run report using the stable canonical JSON contract."""
    return canonical_snapshot_json(report)


def is_terminal_report(report: dict[str, Any]) -> bool:
    """Return True when the report's lifecycle status is terminal."""
    lifecycle = report.get("lifecycle")
    if not isinstance(lifecycle, dict):
        return False
    status = lifecycle.get("status")
    return status in {"COMPLETED", "FAILED", "ORPHANED"}


def _fmt_scalar(value: Any) -> str:
    if value is None:
        return UNKNOWN
    return str(value)


def _fmt_tokens(value: Any) -> str:
    if value == UNKNOWN or value is None:
        return UNKNOWN
    return str(value)


def render_report_text(report: dict[str, Any]) -> str:
    """Render the canonical report as human-readable text.

    Derived field-for-field from the canonical mapping, so the text view
    and the JSON view can never disagree.
    """
    identity = report["identity"]
    lifecycle = report["lifecycle"]
    execution = report["execution"]
    worker = report["worker"]
    review = report["review"]
    gate = report["gate"]
    usage = report["usage"]
    acceptance = report["acceptance"]
    workspace = report["workspace"]
    lines = [
        f"Run report  {identity['run_id']}  ({report['schema']})",
        "",
        "identity",
        f"  run_id            {_fmt_scalar(identity['run_id'])}",
        f"  project_id        {_fmt_scalar(identity['project_id'])}",
        f"  saberops_version  {_fmt_scalar(identity['saberops_version'])}",
        f"  target_repo       {_fmt_scalar(identity['target_repo'])}",
        f"  base_sha          {_fmt_scalar(identity['base_sha'])}",
        f"  candidate_sha     {_fmt_scalar(identity['candidate_sha'])} "
        f"({identity['candidate_source']})",
        "",
        "lifecycle",
        f"  status            {lifecycle['status']}",
        f"  current_stage     {lifecycle['current_stage']} "
        f"({lifecycle['current_stage_source']})",
        f"  created_at        {_fmt_scalar(lifecycle['created_at'])}",
        f"  completed_at      {_fmt_scalar(lifecycle['completed_at'])}",
        f"  total_wall_time   {_fmt_scalar(lifecycle['total_wall_seconds'])}s",
        "",
        "execution",
        f"  attempt_count            {execution['attempt_count']}",
        f"  worker_dispatch_count    {execution['worker_dispatch_count']}",
        f"  retry_dispatch_count     {execution['retry_dispatch_count']}",
        f"  repair_dispatch_count    {execution['repair_dispatch_count']}",
        f"  reviewer_dispatch_count  {execution['reviewer_dispatch_count']}",
        f"  resume_count             {execution['resume_count']}",
        "",
        "worker",
        f"  requested  {_fmt_scalar(worker['requested_provider'])}/"
        f"{_fmt_scalar(worker['requested_model'])}",
        f"  actual     {_fmt_scalar(worker['actual_provider'])}/"
        f"{_fmt_scalar(worker['actual_model'])} "
        f"({worker['actual_identity_status']})",
        "",
        "review",
        f"  required     {_fmt_scalar(review['review_required'])}",
        f"  source       {_fmt_scalar(review['review_requirement_source'])}",
        f"  reason       {_fmt_scalar(review['review_reason'])}",
        f"  skip_reason  {review['review_skip_reason'] or '(none)'}",
        f"  rounds       {review['rounds']}",
        f"  verdicts     {', '.join(review['verdicts']) if review['verdicts'] else '(none)'}",
        "",
        "gate",
        f"  command  {_fmt_scalar(gate['gate_command'])}",
        f"  checks   {gate['summary']['check_passed']} passed / "
        f"{gate['summary']['check_failed']} failed / "
        f"{gate['summary']['check_total']} total; "
        f"{gate['summary']['receipt_total']} receipt(s)",
    ]
    for check in gate["checks"]:
        lines.append(
            f"    [{check['verdict']}] exit={check['exit_code']} "
            f"attempt={check['attempt_id']} duration={_fmt_scalar(check['duration_seconds'])}"
        )
    lines += [
        "",
        "usage",
        f"  provider_usage_events  {usage['provider_usage_event_count']} "
        f"(known={usage['calls_with_known_usage']} "
        f"unknown={usage['calls_with_unknown_usage']})",
        f"  input         {_fmt_tokens(usage['input_tokens'])}",
        f"  output        {_fmt_tokens(usage['output_tokens'])}",
        f"  cache-read    {_fmt_tokens(usage['cache_read_tokens'])}",
        f"  cache-write   {_fmt_tokens(usage['cache_write_tokens'])}",
        f"  reasoning     {_fmt_tokens(usage['reasoning_tokens'])}",
        f"  total         {_fmt_tokens(usage['total_tokens'])}",
        "",
        "acceptance",
        f"  status                {acceptance['acceptance_status']}",
        f"  accepted_candidate    {_fmt_scalar(acceptance['accepted_candidate_sha'])}",
        f"  accepted_at           {_fmt_scalar(acceptance['accepted_at'])}",
        f"  branch                {_fmt_scalar(acceptance['final_branch'])}",
        "",
        "workspace",
    ]
    changed = workspace["changed_paths"]
    if isinstance(changed, list):
        lines.append(f"  changed_paths  {', '.join(changed)}")
    else:
        lines.append(f"  changed_paths  {changed}")
    for entry in workspace["worktrees"]:
        lines.append(
            f"  worktree attempt#{entry['attempt_number']} "
            f"{entry['branch']} exists_now={entry['exists_now']}"
        )
    lines.append(f"  cleanup_state  {workspace['cleanup_state']}")
    warnings = report["warnings"]
    lines += ["", f"warnings ({len(warnings)})"]
    for warning in warnings:
        lines.append(f"  ! {warning}")
    gaps = report["evidence_gaps"]
    lines += ["", f"evidence_gaps ({len(gaps)})"]
    for gap in gaps:
        lines.append(f"  ? {gap}")
    lines.append("")
    return "\n".join(lines)


def render_monitor_frame(report: dict[str, Any], *, now_iso: str | None = None) -> str:
    """Render one compact live-monitor frame from a canonical report.

    ``now_iso`` supplies the wall-clock "now" for the elapsed-time line
    only; it never enters the canonical payload, so live rendering
    cannot corrupt JSON determinism.
    """
    identity = report["identity"]
    lifecycle = report["lifecycle"]
    execution = report["execution"]
    worker = report["worker"]
    review = report["review"]
    gate = report["gate"]
    usage = report["usage"]
    acceptance = report["acceptance"]
    now = _parse_iso(now_iso) if now_iso else datetime.now(UTC)
    elapsed: Any = UNKNOWN
    created = _parse_iso(lifecycle["created_at"])
    if created is not None and now is not None:
        elapsed = f"{max(0.0, (now - created).total_seconds()):.0f}s"
    gate_state = UNKNOWN
    if gate["checks"]:
        last = gate["checks"][-1]
        gate_state = f"{last['verdict']} (exit {last['exit_code']})"
    elif gate["receipts"]:
        gate_state = gate["receipts"][-1]["verdict"]
    review_state = UNKNOWN
    if review["verdicts"]:
        review_state = review["verdicts"][-1]
    elif review["review_required"] is False:
        review_state = "skipped"
    elif review["review_required"] is True:
        review_state = "pending"
    lines = [
        f"Run {identity['run_id']}  [{lifecycle['status']}]  "
        f"stage={lifecycle['current_stage']}  elapsed={elapsed}",
        f"Worker req={worker['requested_provider']}/{worker['requested_model']} "
        f"actual={worker['actual_provider']}/{worker['actual_model']} "
        f"({worker['actual_identity_status']})  "
        f"attempts={execution['attempt_count']} "
        f"dispatch w={execution['worker_dispatch_count']} "
        f"retry={execution['retry_dispatch_count']} "
        f"repair={execution['repair_dispatch_count']} "
        f"review={execution['reviewer_dispatch_count']} "
        f"resumes={execution['resume_count']}",
        f"Usage events={usage['provider_usage_event_count']} "
        f"in={_fmt_tokens(usage['input_tokens'])} "
        f"out={_fmt_tokens(usage['output_tokens'])} "
        f"cache_r={_fmt_tokens(usage['cache_read_tokens'])} "
        f"cache_w={_fmt_tokens(usage['cache_write_tokens'])} "
        f"reason={_fmt_tokens(usage['reasoning_tokens'])} "
        f"total={_fmt_tokens(usage['total_tokens'])}",
        f"Pipeline worker={execution['worker_dispatch_count']}x "
        f"gate={gate_state} review={review_state} "
        f"repair={execution['repair_dispatch_count']}x "
        f"acceptance={acceptance['acceptance_status']}",
    ]
    warnings = report["warnings"]
    gaps = report["evidence_gaps"]
    if warnings:
        lines.append(f"Warnings ({len(warnings)}): " + "; ".join(warnings[:5]))
    if gaps:
        lines.append(f"Evidence gaps ({len(gaps)}): " + "; ".join(gaps[:5]))
    return "\n".join(lines)


__all__ = [
    "REPORT_SCHEMA",
    "REPORT_SCHEMA_VERSION",
    "UNKNOWN",
    "build_run_report",
    "canonical_report_json",
    "is_terminal_report",
    "render_monitor_frame",
    "render_report_text",
    "saberops_version",
]
