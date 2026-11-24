"""Canonical run report and live monitoring projection.

Product-owned, strictly read-only projection over already-persisted run
evidence.  This is the one reader for run facts: ``orch status``,
``orch report``, ``orch watch``, ``catch_up`` and ``tail_job`` all consume
it instead of deriving their own view.  This module performs no model
calls, no Git reads, no network I/O and no database writes: every field is
derived from durable rows (``runs``, ``attempts``, ``dispatch_evidence``,
``model_call_usage``, ``checks``, ``reviews``, ``review_findings``,
``events``, ``worker_checkpoints``, ``handoffs``, ``quota_observations``,
``phase_observations``, the frozen tool/gateway/control-plane contracts and
the project-scope rows) plus one explicitly labelled point-in-time
filesystem observation (attempt worktree existence).

Evidence semantics (fail closed, never inferred):

* ``UNKNOWN`` stays ``UNKNOWN``.
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

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from saberops.db import Database
from saberops.models import (
    TERMINAL_RUN_STATUSES,
    Attempt,
    DispatchReason,
    DispatchRole,
    DispatchStage,
    ReviewVerdict,
    Run,
)

#: Canonical schema marker for the run report projection.
REPORT_SCHEMA: str = "saberops.run_report/v1"
#: Integer schema version carried inside the payload for forward migration.
REPORT_SCHEMA_VERSION: int = 1

#: Sentinel string denoting an unavailable or unmeasured metric value.
UNKNOWN: str = "UNKNOWN"

#: Bounded excerpt length for the reviewer/check evidence a planner reads
#: without opening the store; the full row stays in the database.
_EXCERPT_LIMIT: int = 400

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
    """Return the product version of the code being executed, or ``UNKNOWN``.

    The imported ``saberops`` package is authoritative because it
    identifies the running code.  Installed distribution metadata is
    consulted only when the imported package carries no version, so a
    stale unrelated install can never shadow the source checkout.
    It is read-only package metadata, never execution state.
    """
    try:
        from saberops import __version__ as code_version

        if isinstance(code_version, str) and code_version:
            return code_version
    except Exception:
        pass
    try:
        import importlib.metadata as _metadata

        version = _metadata.version("saberops")
    except Exception:
        version = ""
    if version:
        return version
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


def _excerpt(text: str | None, *, tail: bool = False) -> str | None:
    """Return a bounded single-block excerpt; the full row stays durable."""
    if not text:
        return None
    stripped = text.strip()
    if not stripped:
        return None
    if len(stripped) <= _EXCERPT_LIMIT:
        return stripped
    return "…" + (stripped[-_EXCERPT_LIMIT:] if tail else stripped[:_EXCERPT_LIMIT])


def _load_events(db: Database, run_id: str) -> tuple[list[Any], bool]:
    """Read the run's event log once for every section that projects from it.

    ``False`` means the log could not be read; sections then report the
    same explicit gap they reported when each read its own copy.
    """
    try:
        return db.get_events(run_id, limit=10000), True
    except Exception:
        return [], False


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


def _current_stage(
    run: Run, events: list[Any], events_ok: bool, gaps: list[str]
) -> dict[str, str]:
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
    if not events_ok:
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


def _termination_section(
    run: Run, events: list[Any], events_ok: bool, gaps: list[str]
) -> dict[str, Any]:
    """Project the terminal explanation from the persisted terminal event.

    Read-only projection over the same authority the engine wrote: the
    latest ``run_failed`` (or ``run_completed``) event payload is
    reported verbatim -- summary, outcome, routing context and skipped
    candidates -- never re-derived, so the report cannot contradict the
    run result.  A run with no terminal event reports ``UNKNOWN``.
    """
    if not events_ok:
        gaps.append("event log unreadable; termination UNKNOWN")
        return {
            "terminal_event": UNKNOWN,
            "summary": UNKNOWN,
            "outcome": UNKNOWN,
            "routing_context": UNKNOWN,
            "skipped_candidates": [],
        }
    terminal = next(
        (
            event
            for event in reversed(events)
            if event.event_type in ("run_failed", "run_completed", "run_exception")
        ),
        None,
    )
    if terminal is None:
        return {
            "terminal_event": UNKNOWN,
            "summary": run.result_summary if run.result_summary else UNKNOWN,
            "outcome": UNKNOWN,
            "routing_context": UNKNOWN,
            "skipped_candidates": [],
        }
    payload = terminal.payload if isinstance(terminal.payload, dict) else {}
    summary = payload.get("summary")
    if not isinstance(summary, str) or not summary:
        summary = run.result_summary if run.result_summary else UNKNOWN
    outcome = payload.get("outcome")
    routing_context = payload.get("routing_context")
    skipped = payload.get("skipped_candidates")
    return {
        "terminal_event": f"{terminal.event_type}#{terminal.id}",
        "summary": summary,
        "outcome": outcome if isinstance(outcome, str) and outcome else UNKNOWN,
        "routing_context": (
            routing_context if isinstance(routing_context, str) and routing_context else UNKNOWN
        ),
        "skipped_candidates": skipped if isinstance(skipped, list) else [],
    }


def _review_section(
    run: Run,
    db: Database,
    run_id: str,
    reviews: list[Any],
    candidate_sha: str,
    dispatches: list[Any],
    gaps: list[str],
) -> dict[str, Any]:
    """Project review necessity, rounds, and verdicts from durable rows."""
    required: bool | str = UNKNOWN
    requirement_source: str = UNKNOWN
    skip_reason: str = ""
    reason: str = UNKNOWN
    decision_json = db.get_review_risk_decision_json(run_id)
    if decision_json is not None:
        from saberops.review_adaptive import (
            ReviewDecision,
            ReviewPolicyError,
            validate_review_decision_invariants,
        )

        try:
            decision = ReviewDecision.from_json(decision_json)
            validate_review_decision_invariants(decision)
        except ReviewPolicyError:
            # Mirror the accept engine: an unreadable authority marker must
            # never become a silent skip.
            required = True
            requirement_source = "malformed_persisted_decision_fail_closed"
            gaps.append(
                "persisted review decision is malformed; "
                "treating review as required (fail closed)"
            )
        else:
            required = decision.independent_review_required
            requirement_source = decision.review_requirement_source.value
            skip_reason = decision.review_skip_reason
            reason = (
                skip_reason
                if skip_reason
                else (
                    "independent review is mandatory"
                    if required
                    else "owner-disabled for this run"
                )
            )
    else:
        raw_config = run.config_json
        parsed_config: object = None
        if raw_config:
            try:
                parsed_config = json.loads(raw_config)
            except (TypeError, ValueError, json.JSONDecodeError):
                parsed_config = None
        if isinstance(parsed_config, dict) and isinstance(
            parsed_config.get("review_disabled"), bool
        ):
            # The owner's one manual switch, the same field acceptance reads.
            required = not bool(parsed_config["review_disabled"])
            requirement_source = "frozen_owner_review_switch"
            if required:
                reason = "independent review is mandatory"
            else:
                skip_reason = "owner-disabled"
                reason = "owner-disabled for this run"
        else:
            required = True
            requirement_source = "no_decision_review_required_fail_closed"
            reason = "no frozen review decision; treating review as required"
            gaps.append(
                "no frozen review decision; treating review as required (fail closed)"
            )
    # The commit a review assessed lives in its REVIEW dispatch evidence row;
    #: a review row itself carries no SHA, and "the latest commit" is never
    #: substituted for it, so a review of candidate A cannot be attributed to
    #: a later candidate B.
    assessed: dict[str, str | None] = {}
    for dispatch in dispatches:
        if dispatch.stage != DispatchStage.REVIEW:
            continue
        for key in (dispatch.association_id, dispatch.review_id):
            if key:
                assessed.setdefault(key, dispatch.candidate_sha)
    rounds = [
        {
            "id": review.id,
            "provider": review.provider,
            "model": review.model,
            "verdict": review.verdict.value,
            "created_at": review.created_at,
            "assessed_commit": assessed.get(review.id),
            "assessed_current_candidate": (
                candidate_sha != UNKNOWN and assessed.get(review.id) == candidate_sha
            ),
            "summary_excerpt": _excerpt(review.summary),
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
    run: Run,
    db: Database,
    run_id: str,
    attempts: list[Attempt],
    candidate_sha: str,
    events: list[Any],
    gaps: list[str],
) -> dict[str, Any]:
    """Project gate evidence from check rows and gate-receipt events.

    Check rows have no duration. Full-gate receipts record timestamps and
    outcome; acceptance gate-runner receipts record a separate execution.
    These sources are reported side by side without joining them heuristically.
    The commit each check assessed is the producing attempt's commit, and a
    bounded output excerpt names why it failed without copying the full log.
    """
    checks = db.get_checks_for_run(run_id)
    attempt_commits = {attempt.id: attempt.commit_sha for attempt in attempts}
    check_rows = [
        {
            "id": check.id,
            "attempt_id": check.attempt_id,
            "assessed_commit": attempt_commits.get(check.attempt_id),
            "assessed_current_candidate": (
                candidate_sha != UNKNOWN
                and attempt_commits.get(check.attempt_id) == candidate_sha
            ),
            "gate_command": check.gate_command,
            "exit_code": check.exit_code,
            "passed": check.passed,
            "verdict": "SKIPPED" if check.skipped else "PASS" if check.passed else "FAIL",
            "skipped": check.skipped,
            "created_at": check.created_at,
            "duration_seconds": UNKNOWN,
            "output_excerpt": _excerpt(
                check.stdout if check.passed else check.stderr, tail=not check.passed
            ),
        }
        for check in checks
    ]
    receipts: list[dict[str, Any]] = []
    for event in events:
        if event.event_type not in {"gate_receipt", "full_gate_receipt"}:
            continue
        payload = event.payload if isinstance(event.payload, dict) else {}
        duration: Any
        if event.event_type == "full_gate_receipt":
            duration = _wall_seconds(payload.get("started_at"), payload.get("finished_at"))
            passed = payload.get("passed")
            verdict = "PASS" if passed is True else "FAIL" if passed is False else UNKNOWN
            exit_code = UNKNOWN
        else:
            duration = payload.get("duration_seconds")
            exit_code = payload.get("exit_code", UNKNOWN)
            verdict = (
                "PASS" if exit_code == 0
                and not payload.get("timed_out", False)
                and not payload.get("launch_failed", False)
                and not payload.get("termination_incomplete", False)
                and not payload.get("evidence_persistence_failed", False)
                else "FAIL"
            )
        receipts.append(
            {
                "event_id": event.id,
                "event_type": event.event_type,
                "candidate_sha": payload.get("candidate_sha") or UNKNOWN,
                "gate_command": payload.get("gate_command") or UNKNOWN,
                "exit_code": exit_code,
                "verdict": verdict,
                "duration_seconds": duration if isinstance(duration, (int, float)) else UNKNOWN,
                "created_at": event.created_at,
            }
        )
    if any(not check.skipped for check in checks) and not any(
        receipt["duration_seconds"] != UNKNOWN for receipt in receipts
    ):
        gaps.append("gate durations UNKNOWN: no usable duration in durable gate receipts")
    if not checks and run.status in TERMINAL_RUN_STATUSES:
        gaps.append("no gate check rows persisted for a terminal run")
    passed = sum(1 for row in check_rows if row["passed"])
    skipped = sum(1 for row in check_rows if row["skipped"])
    return {
        "gate_command": run.gate_command,
        "disabled": bool(check_rows and skipped == len(check_rows)),
        "checks": check_rows,
        "receipts": receipts,
        "summary": {
            "check_total": len(check_rows),
            "check_passed": passed,
            "check_failed": len(check_rows) - passed - skipped,
            "check_skipped": skipped,
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
        "rows": [
            dict(row.__dict__, stage=row.stage.value, tier=row.tier.value) for row in rows
        ],
        "aggregate": db.aggregate_model_call_usage(run_id),
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
    checkpoints: list[Any],
    events: list[Any],
    gaps: list[str],
) -> dict[str, Any]:
    """Project workspace evidence from durable rows plus one labelled observation.

    Changed paths union durable ``worker_checkpoints`` rows and
    ``changed_paths`` payloads on the event log.  Worktree existence is
    a point-in-time filesystem observation (read-only), explicitly
    labelled as such: it is not durable evidence.  Cleanup comes from
    durable ``storage_cleanup_events`` rows; when those are absent, the
    observed worktree presence is named as unrecorded rather than
    claimed as a durable cleanup state.
    """
    changed: set[str] = set()
    for checkpoint in checkpoints:
        for path in checkpoint.changed_paths:
            changed.add(str(path))
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
    try:
        cleanup_events = db.list_storage_events(run_id=run_id, limit=1000)
    except Exception:
        cleanup_events = []
    removals = [
        event
        for event in cleanup_events
        if str(event.get("action") or "") == "remove-worktree"
    ]
    if removals:
        cleanup_state: str = "CLEANED"
    elif not worktrees or all(
        entry["worktree_path"] == UNKNOWN for entry in worktrees
    ):
        cleanup_state = UNKNOWN
        gaps.append(
            "cleanup state is not durably recorded by the execution path; "
            "worktree existence above is a point-in-time observation only"
        )
    elif any(entry["exists_now"] is True for entry in worktrees):
        cleanup_state = "NOT_CLEANED"
        gaps.append(
            "no durable cleanup record exists; 'NOT_CLEANED' is a "
            "point-in-time observation, not a durable state"
        )
    else:
        cleanup_state = "CLEANED_UNRECORDED"
        gaps.append(
            "no durable cleanup record exists; worktrees are absent now, but "
            "their removal was not recorded"
        )
    return {
        "changed_paths": changed_paths,
        "worktrees": worktrees,
        "worktree_observation_note": "exists_now is a point-in-time observation, not evidence",
        "cleanup_state": cleanup_state,
    }


def _warnings(
    dispatches: list[Any],
    reviews: list[Any],
    events: list[Any],
    events_ok: bool,
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
    if not events_ok:
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


def _roles_section(
    db: Database,
    run: Run,
    dispatches: list[Any],
    reviews: list[Any],
) -> dict[str, Any]:
    """Show who planned, who built and who checked this run (ADR-0005 §3).

    Read-only projection over durable evidence.  An unreported identity stays
    ``UNKNOWN``: the plan role is the external CLI session recorded in the
    live planning lease at creation time, the build role is the worker
    dispatch evidence, and the check role is the review row.
    """
    plan: dict[str, Any] = {
        "role": "plan",
        "identity": UNKNOWN,
        "source": "no creation evidence",
    }
    try:
        events = db.get_events(run.id, limit=10000)
    except Exception:
        events = []
    for event in events:
        if event.event_type != "run_created":
            continue
        payload = event.payload if isinstance(event.payload, dict) else {}
        planned_by = payload.get("planned_by")
        if isinstance(planned_by, dict):
            plan = {
                "role": "plan",
                "identity": planned_by.get("identity") or UNKNOWN,
                "client": planned_by.get("client") or UNKNOWN,
                "session_id": planned_by.get("session_id") or UNKNOWN,
                "source": planned_by.get("source") or "creation evidence",
            }
        break
    build: dict[str, Any] = {
        "role": "build",
        "provider": UNKNOWN,
        "model": UNKNOWN,
        "identity_status": UNKNOWN,
        "dispatch_id": UNKNOWN,
    }
    build_dispatches = [
        row for row in dispatches if row.role is DispatchRole.BUILD
    ]
    if build_dispatches:
        latest = build_dispatches[-1]
        provider = latest.actual_provider or latest.requested_provider
        model = latest.actual_model or latest.requested_model
        build = {
            "role": "build",
            "provider": provider or UNKNOWN,
            "model": model or UNKNOWN,
            "identity_status": (
                "reported" if latest.actual_provider and latest.actual_model
                else "requested_only"
            ),
            "stage": latest.stage.value,
            "dispatch_id": latest.id,
        }
    check: dict[str, Any] = {
        "role": "check",
        "provider": UNKNOWN,
        "model": UNKNOWN,
        "verdict": UNKNOWN,
        "review_id": UNKNOWN,
    }
    if reviews:
        latest_review = reviews[-1]
        check = {
            "role": "check",
            "provider": latest_review.provider or UNKNOWN,
            "model": latest_review.model or UNKNOWN,
            "verdict": latest_review.verdict.value
            if isinstance(latest_review.verdict, ReviewVerdict)
            else str(latest_review.verdict),
            "review_id": latest_review.id,
        }
    return {"plan": plan, "build": build, "check": check}


def _frozen_tool_contract(run: Run) -> dict[str, Any] | None:
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


def _frozen_gateway_contract(run: Run) -> dict[str, Any] | None:
    """Validate persisted gateway evidence without invoking the gateway.

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


def _gateway_evidence_row(row: Any) -> dict[str, Any]:
    """Project one ``gateway_evidence_json`` row into a report field."""
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


def _project_scope_snapshot(db: Database, run_id: str) -> dict[str, Any] | None:
    """Reconstruct project-scope evidence from durable, plain JSON rows.

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


def _routing_digest(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = raw
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _attempt_row(attempt: Attempt) -> dict[str, Any]:
    """Project one attempt row into the candidate-lineage evidence list."""
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


def _dispatch_row(row: Any) -> dict[str, Any]:
    """Project one dispatch row, decoding its two JSON columns in place."""
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


def _continuation_section(
    db: Database,
    run_id: str,
    checkpoints: list[Any],
    dispatches: list[Any],
) -> dict[str, Any]:
    """Project continuation evidence: checkpoints, handoffs and recovery counts."""
    handoffs = db.get_handoffs_for_run(run_id)
    return {
        "checkpoints": [dict(checkpoint.__dict__) for checkpoint in checkpoints],
        "handoffs": [
            {
                "id": handoff.id,
                "kind": handoff.kind,
                "version": handoff.version,
                "sha256": hashlib.sha256(handoff.content.encode("utf-8")).hexdigest(),
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
    }


def _phase_section(db: Database, run_id: str) -> dict[str, Any]:
    """Project phase telemetry rows and a coverage count per phase."""
    rows = db.list_phase_observations_for_run(run_id)
    summary: dict[str, dict[str, int]] = {}
    for row in rows:
        bucket = summary.setdefault(
            row.phase, {"MEASURED": 0, "UNKNOWN": 0, "NOT_APPLICABLE": 0}
        )
        bucket[row.status] = bucket.get(row.status, 0) + 1
    return {
        "rows": [dict(row.__dict__) for row in rows],
        "summary": summary,
    }


def _evidence_section(
    db: Database,
    run: Run,
    run_id: str,
    dispatches: list[Any],
    attempts: list[Attempt],
    reviews: list[Any],
    checkpoints: list[Any],
    usage: dict[str, Any],
    events: list[Any],
) -> dict[str, Any]:
    """Project the durable evidence behind the report's derived facts.

    One projection answers every evidence question: the frozen contracts,
    dispatch and usage rows, findings, project scope, continuation rows,
    quota observations, phase telemetry and reconstruction completeness.
    """
    findings = db.get_findings_for_run(run_id)
    observations = db.get_quota_observations_for_run(run_id)
    control_plane_events = [
        {
            "event_type": event.event_type,
            "payload": event.payload,
            "created_at": event.created_at,
        }
        for event in events
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
    frozen_tool_contract = _frozen_tool_contract(run)
    frozen_gateway = _frozen_gateway_contract(run)
    gateway_evidence = [
        _gateway_evidence_row(row) for row in dispatches if row.gateway_evidence_json
    ]
    usage_associations = {str(row["association_id"]) for row in usage["rows"]}
    dispatch_contract_complete = all(
        row.prompt_bytes is not None and row.prompt_sha256 is not None for row in dispatches
    )
    dispatch_outcome_complete = all(row.dispatch_outcome is not None for row in dispatches)
    usage_association_complete = all(
        row.association_id in usage_associations for row in dispatches
    )
    usage_coverage_complete = bool(usage["aggregate"]["coverage_complete"])
    reconstruction_complete = (
        dispatch_contract_complete and dispatch_outcome_complete and usage_association_complete
    )
    return {
        "candidate_lineage": [_attempt_row(attempt) for attempt in attempts],
        "dispatches": [_dispatch_row(row) for row in dispatches],
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
        "project_scope": _project_scope_snapshot(db, run_id),
        "continuation": _continuation_section(db, run_id, checkpoints, dispatches),
        "quota_observations": [dict(observation.__dict__) for observation in observations],
        "control_plane": control_plane_events,
        "frozen_control_plane": frozen_control_plane,
        "frozen_tool_contract": frozen_tool_contract,
        "frozen_gateway": frozen_gateway,
        "gateway_evidence": gateway_evidence,
        "phases": _phase_section(db, run_id),
        "completeness": {
            "dispatch_count": len(dispatches),
            "usage_row_count": len(usage["rows"]),
            "dispatches_with_prompt_hash": sum(
                1 for row in dispatches if row.prompt_sha256
            ),
            "dispatches_with_unknown_outcome": sum(
                1 for row in dispatches if row.dispatch_outcome is None
            ),
            "dispatch_contract_complete": dispatch_contract_complete,
            "dispatch_outcome_complete": dispatch_outcome_complete,
            "usage_association_complete": usage_association_complete,
            "usage_coverage_complete": usage_coverage_complete,
            "reconstruction_complete": reconstruction_complete,
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
    checkpoints = db.get_worker_checkpoints_for_run(run_id)
    events, events_ok = _load_events(db, run_id)

    requested = _requested_identity(run, gaps)
    actual = _actual_identity(dispatches, gaps)
    candidate = _candidate_sha(run, attempts, gaps)
    stage = _current_stage(run, events, events_ok, gaps)
    termination = _termination_section(run, events, events_ok, gaps)
    usage = _usage_section(db, run_id, gaps)
    review = _review_section(
        run, db, run_id, reviews, candidate["candidate_sha"], dispatches, gaps
    )
    gate = _gate_section(
        run, db, run_id, attempts, candidate["candidate_sha"], events, gaps
    )
    acceptance = _acceptance_section(db, run_id, gaps)
    workspace = _workspace_section(
        run, db, run_id, attempts, checkpoints, events, gaps
    )
    warnings = _warnings(dispatches, reviews, events, events_ok)
    evidence = _evidence_section(
        db, run, run_id, dispatches, attempts, reviews, checkpoints, usage, events
    )

    worker_dispatches = sum(1 for row in dispatches if row.stage == DispatchStage.IMPLEMENTATION)
    retry_dispatches = sum(1 for row in dispatches if row.stage == DispatchStage.RETRY)
    repair_dispatches = sum(1 for row in dispatches if row.stage == DispatchStage.REPAIR)
    reviewer_dispatches = sum(1 for row in dispatches if row.stage == DispatchStage.REVIEW)
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
        "termination": termination,
        "gate": gate,
        "usage": usage,
        "acceptance": acceptance,
        "workspace": workspace,
        "warnings": warnings,
        "evidence": evidence,
        "evidence_gaps": sorted(set(gaps)),
    }
    report["roles"] = _roles_section(db, run, dispatches, reviews)
    return report


def build_roles_view(
    db: Database, run_id: str, *, plan_identity: str | None = None
) -> dict[str, Any] | None:
    """Return the plan/build/check role view for one run (read-only).

    The job record reuses this so a job and its run never disagree about who
    planned, built and checked.  ``plan_identity`` (the job's caller) is shown
    when the run's creation evidence did not record a live planning session.
    """
    run = db.get_run(run_id)
    if run is None:
        return None
    dispatches = _sorted_dispatches(db, run_id)
    reviews = db.get_reviews_for_run(run_id)
    roles = _roles_section(db, run, dispatches, reviews)
    identity = (plan_identity or "").strip()
    if identity and roles["plan"]["identity"] == UNKNOWN:
        roles["plan"]["identity"] = identity
        roles["plan"]["source"] = "job caller"
    return roles


def canonical_report_json(report: dict[str, Any]) -> bytes:
    """Serialize a run report using the stable canonical JSON contract."""
    return json.dumps(
        report, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def is_terminal_report(report: dict[str, Any]) -> bool:
    """Return True when the report's lifecycle status is terminal.

    The canonical ``TERMINAL_RUN_STATUSES`` set is authoritative, so the live
    follow view and the report projection never disagree about whether a run
    has stopped.
    """
    lifecycle = report.get("lifecycle")
    if not isinstance(lifecycle, dict):
        return False
    status = lifecycle.get("status")
    return isinstance(status, str) and status in {
        value.value for value in TERMINAL_RUN_STATUSES
    }


def _fmt_scalar(value: Any) -> str:
    if value is None:
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
    roles = report["roles"]
    termination = report.get("termination", {})
    review = report["review"]
    gate = report["gate"]
    usage = report["usage"]
    acceptance = report["acceptance"]
    workspace = report["workspace"]
    termination_lines = [
        "",
        "termination",
        f"  event            {_fmt_scalar(termination.get('terminal_event'))}",
        f"  outcome          {_fmt_scalar(termination.get('outcome'))}",
        f"  context          {_fmt_scalar(termination.get('routing_context'))}",
        f"  summary          {_fmt_scalar(termination.get('summary'))}",
    ]
    for skipped in termination.get("skipped_candidates", []) or []:
        if isinstance(skipped, dict):
            skipped_provider = skipped.get("provider", "?")
            skipped_model = skipped.get("model", "?")
            skipped_reason = skipped.get("reason", "?")
            skipped_readiness = skipped.get("readiness_state")
            skipped_readiness_reason = skipped.get("readiness_reason")
            if skipped_readiness and skipped_readiness_reason:
                termination_lines.append(
                    f"    - {skipped_provider}/{skipped_model}: "
                    f"readiness {skipped_readiness} ({skipped_readiness_reason})"
                )
            else:
                termination_lines.append(
                    f"    - {skipped_provider}/{skipped_model}: {skipped_reason}"
                )
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
        *termination_lines,
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
        "roles",
        f"  plan   {_fmt_scalar(roles['plan']['identity'])} "
        f"({_fmt_scalar(roles['plan'].get('source'))})",
        f"  build  {_fmt_scalar(roles['build']['provider'])}/"
        f"{_fmt_scalar(roles['build']['model'])} "
        f"({_fmt_scalar(roles['build']['identity_status'])})",
        f"  check  {_fmt_scalar(roles['check']['provider'])}/"
        f"{_fmt_scalar(roles['check']['model'])} "
        f"({_fmt_scalar(roles['check']['verdict'])})",
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
        f"{gate['summary']['check_skipped']} skipped / "
        f"{gate['summary']['check_total']} total; "
        f"{gate['summary']['receipt_total']} receipt(s)",
    ]
    for check in gate["checks"]:
        lines.append(
            f"    [{check['verdict']}] exit={check['exit_code']} "
            f"attempt={check['attempt_id']} duration={_fmt_scalar(check['duration_seconds'])}"
        )
    for receipt in gate["receipts"]:
        lines.append(
            f"    [{receipt['verdict']}] {receipt['event_type']} "
            f"candidate={receipt['candidate_sha']} command={receipt['gate_command']} "
            f"duration={_fmt_scalar(receipt['duration_seconds'])}"
        )
    lines += [
        "",
        "usage",
        f"  provider_usage_events  {usage['provider_usage_event_count']} "
        f"(known={usage['calls_with_known_usage']} "
        f"unknown={usage['calls_with_unknown_usage']})",
        f"  input         {_fmt_scalar(usage['input_tokens'])}",
        f"  output        {_fmt_scalar(usage['output_tokens'])}",
        f"  cache-read    {_fmt_scalar(usage['cache_read_tokens'])}",
        f"  cache-write   {_fmt_scalar(usage['cache_write_tokens'])}",
        f"  reasoning     {_fmt_scalar(usage['reasoning_tokens'])}",
        f"  total         {_fmt_scalar(usage['total_tokens'])}",
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
        f"in={_fmt_scalar(usage['input_tokens'])} "
        f"out={_fmt_scalar(usage['output_tokens'])} "
        f"cache_r={_fmt_scalar(usage['cache_read_tokens'])} "
        f"cache_w={_fmt_scalar(usage['cache_write_tokens'])} "
        f"reason={_fmt_scalar(usage['reasoning_tokens'])} "
        f"total={_fmt_scalar(usage['total_tokens'])}",
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


def run_explanation(report: dict[str, Any]) -> str:
    """One-paragraph human-readable summary of a canonical run report.

    The run-detail status page renders this summary.  Pure report
    projection; never dispatches work.
    """
    termination = report["termination"]
    execution = report["execution"]
    dispatched = sum(execution.get(key, 0) for key in (
        "worker_dispatch_count", "retry_dispatch_count", "repair_dispatch_count",
    ))
    if termination.get("outcome") == "NO_ELIGIBLE_CANDIDATE" and dispatched == 0:
        skipped = termination.get("skipped_candidates", [])
        names = ", ".join(str(row.get("model", "Unknown model")) for row in skipped)
        return (
            f"I couldn't start implementation. No worker was dispatched. The configured "
            f"worker choices ({names}) didn't meet routing admission requirements. "
            "Open Providers to verify their readiness, or choose a verified worker in Routing, "
            "then ask me to try again.\n\n" + str(termination.get("summary", ""))
        )
    status = report["lifecycle"]["status"]
    summary = termination.get("summary") or "No result summary was recorded."
    return f"The run is {status.lower()}. {summary}"


__all__ = [
    "REPORT_SCHEMA",
    "REPORT_SCHEMA_VERSION",
    "UNKNOWN",
    "build_run_report",
    "canonical_report_json",
    "is_terminal_report",
    "render_monitor_frame",
    "render_report_text",
    "run_explanation",
    "saberops_version",
]
