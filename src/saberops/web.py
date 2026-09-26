"""Local web dashboard for SaberOps.

This module wires the FastAPI application served by ``orch ui``. It uses
OrchestratorService as a shared facade for reading state and dispatching
runs, reviews, accept, cleanup, and live SSE event streaming.
"""
# ruff: noqa: E501

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import AsyncGenerator, Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, Form, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from saberops.accept import AcceptEngine
from saberops.config import (
    reconstruct_run_config,
    resolve_run_config,
    timeout_defaults,
)
from saberops.control_plane import (
    BindingRole,
    BindingStoreError,
    OrchBindingPolicy,
    OrchSelectionStatus,
    OwnerBindings,
    OwnerBindingStore,
    ReviewerSelectionStatus,
    capability_profile_for,
    get_binding_store_path,
    materialize_discovered_binding,
    save_owner_bindings,
    select_orch_binding,
    select_reviewer_binding,
    upsert_owner_binding,
)
from saberops.control_plane.bindings import (
    BindingResolutionError,
    resolve_execution_binding,
)
from saberops.db import Database
from saberops.gate_runner import (
    GATE_SCRATCH_LABELS,
    GateScratchPolicyError,
)
from saberops.model_access import (
    ACCOUNT_BACKENDS,
    DEFAULT_READINESS_MAX_AGE,
    AccessProtocol,
    AccessStore,
    AccessStoreError,
    AuthMode,
    ConnectionTestReason,
    ExecutionSupport,
    ModelCatalog,
    ModelCatalogStore,
    ModelCatalogStoreError,
    ModelDiscoveryResult,
    ModelListingError,
    ModelSource,
    ProviderConnection,
    ReadinessCatalog,
    ReadinessService,
    ReadinessStore,
    ReadinessStoreError,
    TriState,
    apply_discovery_result,
    build_access_registry,
    default_account_connections,
    discover_connection_models,
    list_presets,
    preset_connection,
)
from saberops.models import (
    TERMINAL_RUN_STATUSES,
    CheckResult,
    HealthState,
    OrchBindingMode,
    ProviderReadiness,
    QuotaState,
    ReviewVerdict,
    RoutingState,
    Run,
    RunStatus,
    WorkerCandidate,
)
from saberops.observability.run_report import build_run_report, saberops_version
from saberops.owner_actions import (
    LAUNCH_FAILED_REASON,
    OwnerActionSupervisor,
    summarize_owner_actions,
)
from saberops.project import owner_state_dir
from saberops.projects import ProjectRegistry, ProjectValidation
from saberops.provenance import assert_run_repository
from saberops.routing import is_peak_hours
from saberops.routing_config import (
    ALLOWED_CANDIDATES,
    CONTEXT_KEYS,
    DynamicCandidate,
    RoutingConfigError,
    add_dynamic_candidate_to_chain,
    format_candidate_id,
    load_effective_routing,
    load_effective_routing_payload,
    move_candidate_in_chain,
    remove_candidate_from_chain,
    requires_training_permission,
    save_user_routing,
)
from saberops.run_creation import (
    RunCreationError,
    RunRequest,
    effective_policy_summary,
    resolve_effective_run_config,
)
from saberops.runtime import ProjectRuntime, ProjectRuntimeManager
from saberops.service import OrchestratorService
from saberops.wake import subscribe_run
from saberops.workers.registry import AdapterRegistry

_PACKAGE_DIR = Path(__file__).resolve().parent
_TEMPLATES_DIR = _PACKAGE_DIR / "templates"
_STATIC_DIR = _PACKAGE_DIR / "static"

_SOURCE_SHA_UNKNOWN = "UNKNOWN"
_SOURCE_TREE_CLEAN = "CLEAN"
_SOURCE_TREE_DIRTY = "DIRTY"
_SOURCE_TREE_NOT_GIT = "NOT_GIT"
_SOURCE_TREE_UNKNOWN = "UNKNOWN"


def _resolve_source_identity(package_dir: Path) -> tuple[str, str]:
    """Resolve fail-closed source identity for the executing package.

    Rules:
      - Installed package outside a Git checkout -> ("UNKNOWN", "NOT_GIT").
      - Inside a checkout and the executing ``src/saberops`` tree is clean
        relative to HEAD -> (exact HEAD SHA, "CLEAN").
      - Inside a checkout but the executing package tree differs from HEAD
        (modified, staged, deleted, or relevant untracked files) ->
        ("UNKNOWN", "DIRTY").

    Only the executing package tree (``src/saberops``) is considered, so a
    dirty unrelated README or planning document does not invalidate the SHA.
    Any git failure degrades to ("UNKNOWN", "UNKNOWN").
    """
    try:
        source_root = package_dir.parents[1]
    except IndexError:
        return (_SOURCE_SHA_UNKNOWN, _SOURCE_TREE_NOT_GIT)
    if package_dir != source_root / "src" / "saberops":
        return (_SOURCE_SHA_UNKNOWN, _SOURCE_TREE_NOT_GIT)
    if not (source_root / ".git").exists():
        return (_SOURCE_SHA_UNKNOWN, _SOURCE_TREE_NOT_GIT)
    try:
        from saberops.process import run_process

        head = run_process(["git", "rev-parse", "HEAD"], cwd=source_root, timeout=5)
        if not head.passed:
            return (_SOURCE_SHA_UNKNOWN, _SOURCE_TREE_UNKNOWN)
        sha = head.stdout.strip()
        if re.fullmatch(r"[0-9a-f]{40}", sha) is None:
            return (_SOURCE_SHA_UNKNOWN, _SOURCE_TREE_UNKNOWN)
        status = run_process(
            ["git", "status", "--porcelain=v1", "--", "src/saberops"],
            cwd=source_root,
            timeout=5,
        )
        if not status.passed:
            return (_SOURCE_SHA_UNKNOWN, _SOURCE_TREE_UNKNOWN)
        if status.stdout.strip():
            return (_SOURCE_SHA_UNKNOWN, _SOURCE_TREE_DIRTY)
        return (sha, _SOURCE_TREE_CLEAN)
    except Exception:
        return (_SOURCE_SHA_UNKNOWN, _SOURCE_TREE_UNKNOWN)

_BIN_ATTR: dict[str, str] = {
    "opencode": "opencode_bin",
    "codex": "codex_bin",
    "antigravity": "agy_bin",
}

_PROVIDERS = ("opencode", "codex", "antigravity")

_TIERS = ("T1", "T2", "T3")

_DEFAULT_GATE_COMMAND = "make gate"

TERMINAL_EVENT_TYPES: frozenset[str] = frozenset({"run_completed", "run_failed", "run_exception"})

#: Owner-action event types that must reach the run-page EventSource even
#: after the run itself is terminal (manual Review / Accept happens on an
#: already COMPLETED run).  Kept as a literal tuple so the client bundle
#: and the stream liveness check share one visible contract.
OWNER_ACTION_SSE_EVENT_TYPES: tuple[str, ...] = (
    "owner_action_queued",
    "owner_action_started",
    "owner_action_completed",
    "owner_action_failed",
    "owner_action_uncertain",
)

templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _owner_actions_stream_active(db: Any, run_id: str) -> bool:
    """True while a Review or Accept owner action is effectively active.

    Read-only liveness projection: never mutates storage.  Used by the
    SSE stream so a terminal run with live post-run owner work keeps a
    usable event stream until the work settles.
    """
    try:
        summary = summarize_owner_actions(db, run_id)
    except Exception:
        return False
    return summary.active_review is not None or summary.active_accept is not None


def format_timestamp(value: str | None) -> str:
    """Render a stored timestamp in canonical format: YYYY-MM-DD HH:MM:SS UTC."""
    if value is None:
        return "N/A"
    raw = value.strip()
    if raw == "":
        return "N/A"
    normalized = raw
    if raw.endswith("Z") or raw.endswith("z"):
        normalized = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return raw
    if parsed.tzinfo is not None and parsed.utcoffset() is not None:
        parsed = parsed.astimezone(UTC)
    return parsed.strftime("%Y-%m-%d %H:%M:%S UTC")


def _status_class(status: str) -> str:
    """Normalize a status string into a CSS-friendly class token."""
    return status.strip().lower().replace(" ", "-").replace("_", "-")


def _short_summary(text: str | None, limit: int = 80) -> str:
    """Return a trimmed single-line summary for table display."""
    if text is None:
        return ""
    stripped = " ".join(text.split())
    if len(stripped) <= limit:
        return stripped
    return stripped[:limit].rstrip() + "…"


def _provider_availability(
    registry: AdapterRegistry, quota_states: list[QuotaState]
) -> list[dict[str, str]]:
    """Compute display rows for provider availability."""
    blocked_providers = {
        q.provider for q in quota_states if q.routing_state == RoutingState.BLOCKED
    }
    rows: list[dict[str, str]] = []
    for provider in _PROVIDERS:
        adapter = registry.get(provider)
        status = "NOT INSTALLED"
        detail = ""
        bin_attr = _BIN_ATTR.get(provider)
        configured = getattr(adapter, bin_attr, None) if bin_attr and adapter is not None else None
        if provider == "antigravity":
            configured = os.environ.get("ORCH_AGY_BIN") or configured or "agy"
        resolved = shutil.which(configured) if configured else None
        if resolved is not None:
            if provider in blocked_providers:
                status = "BLOCKED BY QUOTA"
                detail = resolved
            else:
                status = "AVAILABLE"
                detail = resolved
        rows.append(
            {
                "name": provider,
                "status": status,
                "status_class": _status_class(status),
                "detail": detail,
            }
        )
    return rows


def _candidate_rows(
    registry: AdapterRegistry, chain: list[WorkerCandidate]
) -> list[dict[str, Any]]:
    """Convert a candidate chain into template-friendly rows."""
    return [
        {
            "provider": candidate.provider,
            "model": candidate.model,
            "available": registry.is_available(candidate.provider),
        }
        for candidate in chain
    ]


def _build_tiers(registry: AdapterRegistry) -> list[dict[str, Any]]:
    """Build the configured tier/model matrix for the dashboard."""
    snap = load_effective_routing()
    return [
        {
            "name": "T1",
            "title": "Standard / Fast Tier",
            "groups": [
                {"label": "Default", "candidates": _candidate_rows(registry, snap.t1_default)},
            ],
        },
        {
            "name": "T2",
            "title": "Advanced Tier",
            "groups": [
                {
                    "label": "Training Allowed",
                    "candidates": _candidate_rows(registry, snap.t2_training_allowed),
                },
                {
                    "label": "Training Denied (Off-Peak)",
                    "candidates": _candidate_rows(registry, snap.t2_training_denied_off_peak),
                },
                {
                    "label": "Training Denied (Peak)",
                    "candidates": _candidate_rows(registry, snap.t2_training_denied_peak),
                },
            ],
        },
        {
            "name": "T3",
            "title": "High Performance Tier",
            "groups": [
                {
                    "label": "Off-Peak",
                    "candidates": _candidate_rows(registry, snap.t3_off_peak),
                },
                {
                    "label": "Peak",
                    "candidates": _candidate_rows(registry, snap.t3_peak),
                },
            ],
        },
    ]


def _gate_result(checks: list[CheckResult]) -> str | None:
    """Derive an overall gate result from a list of checks."""
    if not checks:
        return None
    return "PASS" if all(check.passed for check in checks) else "FAIL"


def _run_rows(runs: list[Run]) -> list[dict[str, Any]]:
    """Convert runs into dashboard table rows."""
    return [
        {
            "id": run.id,
            "repository": run.target_repo,
            "tier": run.tier.value,
            "status": run.status.value,
            "status_class": _status_class(run.status.value),
            "created_at": run.created_at,
            "completed_at": run.completed_at,
            "summary": _short_summary(run.task),
        }
        for run in runs
    ]


def _attempt_rows(db: Database, run: Run) -> list[dict[str, Any]]:
    """Convert attempts (plus their gate results) into template rows."""
    rows: list[dict[str, Any]] = []
    dispatches = {
        event.attempt_id: event.payload
        for event in db.get_events(run.id, limit=1000)
        if event.event_type == "worker_started"
        and event.attempt_id
        and isinstance(event.payload, dict)
    }
    for attempt in db.get_attempts_for_run(run.id):
        checks = db.get_checks_for_attempt(attempt.id)
        last_check = checks[-1] if checks else None
        rows.append(
            {
                "attempt_number": attempt.attempt_number,
                "provider": attempt.provider,
                "model": attempt.model,
                "status": attempt.status.value,
                "status_class": _status_class(attempt.status.value),
                "commit_sha": attempt.commit_sha,
                "worktree_path": attempt.worktree_path,
                "worker_stdout": attempt.worker_stdout,
                "worker_stderr": attempt.worker_stderr,
                "worker_error": attempt.worker_error,
                "gate_result": _gate_result(checks),
                "gate_stdout": last_check.stdout if last_check else "",
                "gate_stderr": last_check.stderr if last_check else "",
                "gate_exit_code": last_check.exit_code if last_check else None,
                "created_at": attempt.created_at,
                "completed_at": attempt.completed_at,
                "stage": (dispatches.get(attempt.id) or {}).get("stage", "Unknown"),
                "tier": (dispatches.get(attempt.id) or {}).get("tier", "Unknown"),
            }
        )
    return rows


def _review_rows(db: Database, run: Run) -> list[dict[str, Any]]:
    """Convert reviews into detailed template rows for the Review UX."""
    raw_reviews = db.get_reviews_for_run(run.id)
    if not raw_reviews:
        return []

    config_data: dict[str, Any] = {}
    if run.config_json:
        try:
            config_data = json.loads(run.config_json)
        except Exception:
            config_data = {}

    review_mode = str(config_data.get("review_mode") or "bounded").lower()
    review_limit = int(config_data.get("review_limit") or 3)

    # Gather attempts and repair events to track candidate SHA and related repair SHA
    attempts = db.get_attempts_for_run(run.id)
    events = db.get_events(run.id)
    repair_events = [e for e in events if e.event_type == "repair_started"]

    # Initial candidate SHA (commit of the attempt before reviews started)
    initial_candidate_sha: str | None = None
    for att in attempts:
        if att.commit_sha:
            initial_candidate_sha = att.commit_sha
            break

    # Attempts that are repairs (attempt_number >= 2 or matching repair events)
    repair_attempts = [att for att in attempts if att.attempt_number > 1]

    rows: list[dict[str, Any]] = []
    for idx, review in enumerate(raw_reviews):
        review_number = idx + 1

        # Review progress label and final flag
        if review_mode == "max":
            progress_label = f"Review {review_number} / MAX"
            is_final = False
        else:
            progress_label = f"Review {review_number} / {review_limit}"
            is_final = review_number == review_limit

        # Execution status vs substantive verdict
        if review.verdict == ReviewVerdict.REVIEW_EXECUTION_FAILED:
            execution_status = "FAILED"
        elif review.verdict == ReviewVerdict.REVIEW_UNAVAILABLE:
            execution_status = "UNAVAILABLE"
        else:
            execution_status = "COMPLETED"

        # Candidate SHA reviewed: resolve from lineage
        candidate_sha: str | None
        if idx == 0:
            candidate_sha = initial_candidate_sha
        elif (idx - 1) < len(repair_attempts):
            sha = repair_attempts[idx - 1].commit_sha
            candidate_sha = sha if sha is not None else initial_candidate_sha
        else:
            candidate_sha = None
            if idx < len(repair_events):
                payload = repair_events[idx].payload
                if isinstance(payload, dict):
                    raw = payload.get("from_sha")
                    if isinstance(raw, str) and raw:
                        candidate_sha = raw
            if candidate_sha is None and repair_attempts:
                last = repair_attempts[-1].commit_sha
                candidate_sha = last if last is not None else initial_candidate_sha
            if candidate_sha is None:
                candidate_sha = initial_candidate_sha

        # Related repair SHA produced as a result of this review
        repair_sha: str | None = None
        if idx < len(repair_attempts):
            repair_sha = repair_attempts[idx].commit_sha

        rows.append(
            {
                "id": review.id,
                "review_number": review_number,
                "progress_label": progress_label,
                "is_final": is_final,
                "provider": review.provider,
                "model": review.model,
                "execution_status": execution_status,
                "verdict": review.verdict.value,
                "status_class": _status_class(review.verdict.value),
                "candidate_sha": candidate_sha,
                "repair_sha": repair_sha,
                "summary": review.summary,
                "details": review.details,
                "created_at": review.created_at,
            }
        )
    return rows


def _quota_rows(quota_states: list[QuotaState]) -> list[dict[str, Any]]:
    """Convert quota states into template rows."""
    return [
        {
            "provider": quota.provider,
            "pool": quota.pool,
            "remaining_percent": quota.remaining_percent,
            "routing_state": quota.routing_state.value,
            "status_class": _status_class(quota.routing_state.value),
            "reset_at": quota.reset_at,
            "updated_at": quota.updated_at,
        }
        for quota in quota_states
    ]


def _default_start_form() -> dict[str, Any]:
    """Return the start-run form defaults.

    The gate box defaults to EMPTY (follow the project gate setting);
    the shared run-creation authority fails creation with an actionable
    message when the project has no usable gate.  The training default
    is overlaid from the active project's effective data policy in
    :func:`_dashboard_context` so a fresh private project offers Denied.
    The ``Independent Review`` checkbox defaults to UNCHECKED: an
    ordinary Web run lets the C13-F risk-adaptive policy decide.  The
    user can opt in to ``review_enabled=True`` per-run via the existing
    checkbox; that signals an explicit owner force and forces review
    regardless of LOW classification.
    """
    return {
        "task": "",
        "routing_mode": "auto",
        "manual_tier": "",
        "max_auto_tier": "T3",
        "training_allowed": True,
        "provider_override": "",
        "model_override": "",
        "gate_command": "",
        "worker_timeout": "",
        "gate_timeout": "",
        "review_timeout": "",
        "review_enabled": False,
        "review_mode": "bounded",
        "review_limit": "3",
    }


def _format_timeout_seconds(raw: str, default: float) -> str:
    """Render a timeout input as seconds, falling back to the canonical default."""
    value = raw.strip()
    if not value:
        return f"{default:g}s"
    try:
        return f"{float(value):g}s"
    except ValueError:
        return value


def _effective_timeout_display(form: dict[str, Any]) -> dict[str, str]:
    """Render the effective worker/gate/review timeouts for the current form.

    All numbers are pulled from :func:`timeout_defaults` (the same shared
    table used by ``resolve_run_config``), so the display can never drift from
    the resolver.

    Tier values (T1/T2/T3) are the DEFAULT HARD DEADLINES (C16-C0): every
    worker dispatch is bounded, and exceeding the effective timeout
    terminates the worker through the fenced process-group mechanism with
    a TIMEOUT classification.  An explicit ``--worker-timeout`` overrides
    the tier default; without it, the tier default still bounds the run
    alongside monitor-driven (confirmed stall) termination.

    The raw defaults are also embedded as JSON so the browser can recompute
    the panel live without a form submission.
    """
    defaults = timeout_defaults()
    worker_override = str(form.get("worker_timeout", "")).strip()
    if worker_override:
        try:
            hard_budget = f"{float(worker_override):g}s"
        except ValueError:
            hard_budget = worker_override
        # Explicit runtime budget supplied by the owner.
        worker_text = hard_budget
        has_hard_budget = True
    else:
        # No explicit budget: the tier default is the effective hard deadline.
        routing_mode = str(form.get("routing_mode", "auto"))
        manual_tier = str(form.get("manual_tier", "")).strip()
        if routing_mode == "manual" and manual_tier in _TIERS:
            guidance = f"{defaults[manual_tier]:g}s"
        else:
            max_auto_tier = str(form.get("max_auto_tier", "T3")).strip()
            ceiling_index = _TIERS.index(max_auto_tier) if max_auto_tier in _TIERS else len(_TIERS)
            guidance = " / ".join(f"{t} {defaults[t]:g}s" for t in _TIERS[: ceiling_index + 1])
        worker_text = guidance
        has_hard_budget = False
    gate_text = _format_timeout_seconds(str(form.get("gate_timeout", "")), defaults["gate"])
    review_text = _format_timeout_seconds(str(form.get("review_timeout", "")), defaults["review"])
    return {
        "worker": worker_text,
        "gate": gate_text,
        "review": review_text,
        "defaults_json": json.dumps(defaults, sort_keys=True),
        # Semantic markers for the template: distinguish an explicit owner
        # budget from the tier-default deadline.  Every run is bounded
        # (C16-C0); the monitor complements both modes.
        "has_hard_budget": "true" if has_hard_budget else "false",
        "termination_mode": (
            "explicit_budget_plus_monitor"
            if has_hard_budget
            else "tier_default_plus_monitor"
        ),
    }


def resolve_owner_argv_prefix() -> list[str]:
    """Resolve the argv prefix for a detached/supervised execution owner.

    Single authority for execution-owner identity.  By default the
    continuation re-executes the **same** Python environment/code identity
    that launched it via ``[sys.executable, "-m", "saberops.cli"]`` -- this
    covers an installed venv, an editable install, and a source/dev
    execution whose checkout arrives via the inherited ``PYTHONPATH``
    (see ``RunSupervisor._spawn_owner``).  No ``orch`` entry point is
    discovered from ``$PATH`` on the default path, so a stale global
    ``orch`` can never become the continuation authority.

    ``ORCH_EXECUTABLE`` remains an explicit owner/developer override and,
    when set, yields the single-element prefix ``[override]``.
    """
    override = os.environ.get("ORCH_EXECUTABLE", "").strip()
    if override:
        return [override]
    return [sys.executable, "-m", "saberops.cli"]


def resolve_orch_executable() -> str:
    """Resolve the orchestrator executable identity for display/diagnostics.

    Thin display wrapper over :func:`resolve_owner_argv_prefix` (the single
    launch authority): the override when set, else the current interpreter
    identity.  Never falls back to ``shutil.which("orch")`` or any other
    ``$PATH`` discovery, so diagnostics cannot mask a stale global ``orch``.
    """
    return " ".join(resolve_owner_argv_prefix())


def _dashboard_context(
    service: OrchestratorService,
    projects: ProjectRegistry,
    *,
    error: str | None = None,
    notice: str | None = None,
    form: dict[str, Any] | None = None,
    validation_cache: ProjectValidationCache | None = None,
) -> dict[str, Any]:
    """Build the dashboard template context."""
    peak = is_peak_hours()
    quota_states = service.list_quota()
    active_repo = projects.active()
    active_path_str = str(active_repo) if active_repo else ""
    if active_repo is None:
        val = None
    elif validation_cache is not None:
        val = validation_cache.get(active_repo)
    else:
        val = projects.validate_project(active_repo)
    repo_ready = val.ready if val else False
    repo_note = val.note if val else "No project selected"

    def _recent_validation(path: str) -> ProjectValidation:
        if validation_cache is not None:
            return validation_cache.get(path)
        return projects.validate_project(path)

    recent_projects = [
        {"path": p, "validation": _recent_validation(p)} for p in projects.list_projects()
    ]
    resolved_form = form if form is not None else _default_start_form()
    project_gate_scratch: dict[str, Any] | None = None
    project_settings: dict[str, Any] | None = None
    if active_repo is not None:
        try:
            payload = OrchestratorService.get_project_gate_scratch_policy(active_repo)
            options = [
                {"value": m.value, "label": lbl}
                for m, lbl in GATE_SCRATCH_LABELS.items()
            ]
            project_gate_scratch = {
                "current_mode": payload["mode"],
                "current_label": payload["label"],
                "is_default": payload["is_default"],
                "options": options,
            }
        except GateScratchPolicyError:
            project_gate_scratch = None
        # R2-A: owner-facing effective project settings (gate, data
        # policy, review policy, target branch, gate storage).  When
        # rendering a fresh form, overlay the training default and the
        # configured gate from effective policy so the default
        # submission freezes the safe project values.
        summary = effective_policy_summary(active_repo)
        if "error" not in summary:
            project_settings = {
                "gate_command": summary["gate_command"],
                "gate_configured": summary["gate_configured"],
                "gate_detected": summary["gate_detected"],
                "gate_effective": summary["gate_effective"],
                "data_policy": summary["data_policy"],
                "data_policy_label": summary["data_policy_label"],
                "data_policy_explicit": summary["data_policy_explicit"],
                "training_allowed": summary["training_allowed"],
                "review_policy_mode": summary["review_policy_mode"],
                "review_policy_explicit": summary["review_policy_explicit"],
                "gate_scratch_mode": summary["gate_scratch_mode"],
                "accept_target_branch": summary["accept_target_branch"],
                "accept_target_branch_explicit": summary[
                    "accept_target_branch_explicit"
                ],
                "accept_target_branch_effective": summary[
                    "accept_target_branch_effective"
                ],
            }
            if form is None:
                resolved_form["training_allowed"] = bool(summary["training_allowed"])
                if summary["gate_configured"]:
                    resolved_form["gate_command"] = str(summary["gate_command"] or "")
    return {
        "schedule_state": "PEAK" if peak else "OFF-PEAK",
        "schedule_class": _status_class("PEAK" if peak else "OFF-PEAK"),
        "providers": _provider_availability(service.registry, quota_states),
        "tiers": _build_tiers(service.registry),
        "quota_states": _quota_rows(quota_states),
        "runs": _run_rows(service.list_runs(limit=50)),
        "repo_path": active_path_str,
        "repo_ready": repo_ready,
        "repo_note": repo_note,
        "recent_projects": recent_projects,
        "active_project": active_path_str,
        "tier_options": _TIERS,
        "form": resolved_form,
        "effective_timeouts": _effective_timeout_display(resolved_form),
        "project_gate_scratch": project_gate_scratch,
        "project_settings": project_settings,
        "error": error,
        "notice": notice,
    }


def _run_detail_context(
    db: Database,
    run: Run,
    *,
    confirming_cleanup: bool = False,
    confirming_accept: bool = False,
    confirming_reject: bool = False,
    accept_result: dict[str, str] | None = None,
    error: str | None = None,
    notice: str | None = None,
) -> dict[str, Any]:
    """Build the run-detail template context."""
    # Acceptance authority lives in AcceptEngine.  The Web renders the
    # engine-derived read-only projection as a checklist and gates the
    # Accept button on it; it never recreates acceptance policy.
    eligibility = AcceptEngine(db=db).check_accept_eligibility(run.id)
    can_accept = eligibility.eligible
    accept_checklist = eligibility.as_dict()["checks"]
    # R3-C: rejectability derives from the same service/projection
    # authority as the Reject action (never from RunStatus alone), and
    # the Candidate State shown is the R3-A projection including
    # REJECTED.  Reads here never record rejection events.
    from saberops.candidate_lifecycle import can_reject_candidate, project_candidate_state

    try:
        _projection = project_candidate_state(db, run.id)
        candidate_state: str | None = (
            _projection.state.value if _projection.state is not None else None
        )
    except Exception:
        candidate_state = None
    try:
        can_reject = can_reject_candidate(db, run.id)
    except Exception:
        can_reject = False
    attempts = db.get_attempts_for_run(run.id)
    events = db.get_events(run.id, limit=1000)
    review_rows = db.get_reviews_for_run(run.id)
    review_count = len(review_rows)
    # Persisted review config (additive)
    review_mode = "bounded"
    review_limit = 3
    if run.config_json:
        try:
            cfg = json.loads(run.config_json)
            review_mode = str(cfg.get("review_mode", "bounded"))
            review_limit = int(cfg.get("review_limit", 3))
        except Exception:
            pass

    candidate_sha: str | None = None
    changed_files: list[str] = []
    diff_text = ""
    diff_truncated = False
    diff_available = False
    diff_unavailable_reason: str | None = None
    failure_reason: str | None = run.result_summary

    for att in attempts:
        if att.worker_error:
            failure_reason = att.worker_error

    # R3-B: the Changes view is the same read-only candidate-diff
    # projection as GET /runs/{run_id}/diff -- frozen base_commit ..
    # R3-A current candidate SHA from the run's persisted repository.
    # Event changed_paths and latest-attempt SHAs are never consulted.
    try:
        from saberops.candidate_diff import project_candidate_diff

        diff = project_candidate_diff(db, run.id)
        candidate_sha = diff.candidate_sha
        changed_files = list(diff.changed_paths)
        diff_text = diff.diff_text
        diff_truncated = diff.truncated
        diff_available = diff.available
        diff_unavailable_reason = diff.unavailable_reason
    except Exception:
        candidate_sha = None
        changed_files = []
        diff_text = ""
        diff_truncated = False
        diff_available = False
        diff_unavailable_reason = "diff_failed"

    current_dispatch: dict[str, Any] | None = None
    for ev in reversed(events):
        if ev.event_type in ("worker_started", "review_dispatch_started") and isinstance(
            ev.payload, dict
        ):
            current_dispatch = {
                "stage": ev.payload.get("stage") or "Unknown",
                "tier": ev.payload.get("tier") or "Unknown",
                "tier_reason": ev.payload.get("tier_reason") or "Unknown",
                "provider": ev.payload.get("provider") or "Unknown",
                "model": ev.payload.get("model") or "Unknown",
            }
            break
    usage = db.aggregate_model_call_usage(run.id)
    try:
        run_report: dict[str, Any] | None = build_run_report(db, run.id)
    except Exception:
        # The monitoring projection must never break the run page: a
        # report build failure degrades to "unavailable" in the UI
        # while the canonical CLI/API surfaces still fail closed.
        run_report = None
    # R3-E2: durable owner-action projection.  This says whether the
    # detached process executing Review/Accept authority is active or how
    # the last one settled -- it is never a second review verdict or
    # acceptance-policy authority (those stay in the reviews table and
    # the AcceptEngine evidence).
    try:
        owner_summary = summarize_owner_actions(db, run.id)
    except Exception:
        owner_summary = None
    if owner_summary is not None and owner_summary.active_review is not None:
        owner_review_active = True
        owner_review_state: str | None = owner_summary.active_review.state.value
        owner_review_action: str | None = owner_summary.active_review.action_id
    else:
        owner_review_active = False
        owner_review_state = None
        owner_review_action = None
    if owner_summary is not None and owner_summary.active_accept is not None:
        owner_accept_active = True
        owner_accept_state: str | None = owner_summary.active_accept.state.value
        owner_accept_action: str | None = owner_summary.active_accept.action_id
    else:
        owner_accept_active = False
        owner_accept_state = None
        owner_accept_action = None
    if owner_summary is not None and owner_summary.last_review is not None:
        owner_review_last_error: str | None = (
            owner_summary.last_review.reason
            or owner_summary.last_review.error
            or owner_summary.last_review.state.value
        )
    else:
        owner_review_last_error = None
    if owner_summary is not None and owner_summary.last_accept is not None:
        owner_accept_last_error: str | None = (
            owner_summary.last_accept.reason
            or owner_summary.last_accept.error
            or owner_summary.last_accept.state.value
        )
    else:
        owner_accept_last_error = None

    return {
        "missing": False,
        "run_id": run.id,
        "task": run.task,
        "target_repo": run.target_repo,
        "base_commit": run.base_commit,
        "tier": run.tier.value,
        "training_policy": "Allowed" if run.training_allowed else "Denied",
        "status": run.status.value,
        "status_class": _status_class(run.status.value),
        "gate_command": run.gate_command,
        "summary": run.result_summary,
        "created_at": run.created_at,
        "completed_at": run.completed_at,
        "attempts": _attempt_rows(db, run),
        "reviews": _review_rows(db, run),
        "review_count": review_count,
        "review_mode": review_mode,
        "review_limit": review_limit,
        "review_attempt_counter": f"{review_count}/{review_limit} ({review_mode})",
        "events": events,
        "candidate_sha": candidate_sha,
        "changed_files": changed_files,
        "diff_text": diff_text,
        "diff_truncated": diff_truncated,
        "diff_available": diff_available,
        "diff_unavailable_reason": diff_unavailable_reason,
        "failure_reason": failure_reason,
        "can_review": run.status == RunStatus.COMPLETED and candidate_state != "REJECTED",
        "can_cleanup": run.status in (RunStatus.COMPLETED, RunStatus.FAILED),
        "can_accept": can_accept,
        "accept_checklist": accept_checklist,
        "candidate_state": candidate_state,
        "can_reject": can_reject,
        "can_retry": run.status in (RunStatus.COMPLETED, RunStatus.FAILED),
        "can_cancel": run.status not in TERMINAL_RUN_STATUSES,
        "owner_review_active": owner_review_active,
        "owner_review_state": owner_review_state,
        "owner_review_action": owner_review_action,
        "owner_accept_active": owner_accept_active,
        "owner_accept_state": owner_accept_state,
        "owner_accept_action": owner_accept_action,
        "owner_review_last_error": owner_review_last_error,
        "owner_accept_last_error": owner_accept_last_error,
        "confirming_cleanup": confirming_cleanup,
        "confirming_accept": confirming_accept,
        "confirming_reject": confirming_reject,
        "accept_result": accept_result,
        "error": error,
        "notice": notice,
        "plan_projection": _plan_projection(db, run.id),
        "current_dispatch": current_dispatch,
        "usage": usage,
        "run_report": run_report,
    }


def _plan_projection(db: Database, run_id: str) -> dict[str, Any]:
    """Stable API/UI projection sourced only from normalized persisted state."""
    plan = db.get_run_plan(run_id)
    supervisor = db.get_supervision(run_id)
    packages = {package.id: package for package in db.get_work_packages(run_id)}
    steps = [
        {
            "id": step.id,
            "title": step.title,
            "goal": step.goal,
            "status": step.status.value,
            "ordering": step.ordering,
            "dependency_ids": list(step.dependency_ids),
            "work_package_id": step.work_package_id,
            "attempt_id": step.attempt_id,
            "long_running": packages[step.work_package_id].long_running
            if step.work_package_id in packages
            else False,
        }
        for step in db.get_plan_steps(run_id)
    ]
    latest_review = db.get_latest_review(run_id)
    started_at = supervisor.started_at if supervisor else None
    last_activity_at = supervisor.last_activity_at if supervisor else None
    elapsed_seconds: float | None = None
    if last_activity_at is not None:
        try:
            elapsed_seconds = max(
                0.0, (datetime.now() - datetime.fromisoformat(last_activity_at)).total_seconds()
            )
        except (ValueError, TypeError):
            elapsed_seconds = None
    stalled = supervisor is not None and supervisor.health_state == HealthState.STALLED
    return {
        "run_id": run_id,
        "plan": None
        if plan is None
        else {"id": plan.id, "status": plan.status.value, "version": plan.version},
        "steps": steps,
        "current_provider": supervisor.provider if supervisor else None,
        "current_model": supervisor.model if supervisor else None,
        "health": supervisor.health_state.value if supervisor else "UNKNOWN",
        "last_activity_at": last_activity_at,
        "supervisor_state": supervisor.supervisor_state if supervisor else "UNKNOWN",
        "manager_state": supervisor.manager_state.value if supervisor else "UNKNOWN",
        "review_state": latest_review.verdict.value if latest_review else "UNKNOWN",
        "current_attempt_id": supervisor.current_attempt_id if supervisor else None,
        "worker_pid": supervisor.worker_pid if supervisor else None,
        "started_at": started_at,
        "deadline_at": supervisor.deadline_at if supervisor else None,
        # C16-C0: every supervised dispatch carries a hard deadline
        # (explicit owner value, else the tier default), so a present
        # deadline means the run is bounded -- never unbounded.
        "has_explicit_budget": bool(supervisor and supervisor.deadline_at is not None),
        "elapsed_since_activity_seconds": (
            round(elapsed_seconds, 1) if elapsed_seconds is not None else None
        ),
        "stalled": stalled,
        "transcript_path": supervisor.transcript_path if supervisor else None,
    }


def _quota_context(
    db: Database,
    *,
    error: str | None = None,
    notice: str | None = None,
    form: dict[str, Any] | None = None,
    enable_form: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the quota-management template context."""
    empty = {"provider": "", "pool": "", "remaining_percent": "", "reset_at": ""}
    return {
        "quota_states": _quota_rows(db.list_quota()),
        "form": form if form is not None else dict(empty),
        "enable_form": enable_form if enable_form is not None else {"provider": "", "pool": ""},
        "error": error,
        "notice": notice,
    }


def _missing_run_context(run_id: str) -> dict[str, Any]:
    """Build the no-such-run context."""
    return {"missing": True, "run_id": run_id}


def _inbox_context(snapshot: Any) -> dict[str, Any]:
    """Build the Inbox template context from one read-only snapshot."""
    rows: list[dict[str, Any]] = []
    for item in snapshot.items:
        payload = item.as_dict()
        project_path = payload["project_path"] or ""
        rows.append(
            {
                **payload,
                "project_short": (
                    Path(project_path).name if project_path else ""
                ),
                "task_summary": _short_summary(payload["task"]),
                "attention_class": _status_class(payload["attention_label"]),
                "short_sha": item.short_sha,
                "updated_at": item.updated_at,
            }
        )
    return {
        "items": rows,
        "count": len(rows),
        "unreadable_projects": snapshot.unreadable_projects,
    }


class ProjectValidationCache:
    """Small bounded cache over expensive project Git validation.

    Shell and dashboard rendering validate the active project plus every
    known recent project (each a ``git status``-class inspection).  This
    cache bounds that cost per request burst without becoming a general
    caching framework:

    * entries expire after ``ttl_seconds`` so active project state never
      becomes dangerously stale;
    * :meth:`invalidate` clears one path (or everything) and is called
      by every route that mutates or selects projects;
    * size is bounded (oldest entry evicted first);
    * ``now`` is injectable so behavior is deterministic and testable.

    Correctness beats micro-optimization: on any doubt the caller
    invalidates and the next read revalidates live.
    """

    def __init__(
        self,
        registry: ProjectRegistry,
        *,
        ttl_seconds: float = 2.0,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._registry = registry
        self._ttl = ttl_seconds
        self._now = now
        self._entries: dict[str, tuple[float, ProjectValidation]] = {}

    def get(self, path: Path | str) -> ProjectValidation:
        """Return the cached validation for ``path``, revalidating when stale."""
        key = str(Path(path).resolve())
        moment = self._now()
        entry = self._entries.get(key)
        if entry is not None:
            stored_at, validation = entry
            if moment - stored_at <= self._ttl:
                return validation
        validation = self._registry.validate_project(key)
        if len(self._entries) >= 16:
            oldest = min(self._entries, key=lambda k: self._entries[k][0])
            del self._entries[oldest]
        self._entries[key] = (moment, validation)
        return validation

    def invalidate(self, path: Path | str | None = None) -> None:
        """Drop the cached entry for ``path``, or everything when None."""
        if path is None:
            self._entries.clear()
            return
        self._entries.pop(str(Path(path).resolve()), None)


def _empty_shell() -> dict[str, Any]:
    """Degraded shell context: the console chrome renders with empty panes."""
    return {
        "active_project": "",
        "active_project_short": "SaberOps",
        "repo_ready": False,
        "repo_note": "Shell unavailable",
        "recent_projects": [],
        "recent_runs": [],
        "terminal_runs": [],
        "run_count": 0,
        "active_workers": 0,
        "status_verdict": "SaberOps",
        "saberops_version": saberops_version(),
        "inbox_count": None,
    }


def _shell_context(
    service: OrchestratorService,
    projects: ProjectRegistry,
    validation_cache: ProjectValidationCache | None = None,
    runtimes: ProjectRuntimeManager | None = None,
) -> dict[str, Any]:
    """Build the console-shell context rendered on every page.

    The approved OpenDesign shell (activity rail panes, status bar)
    needs live project/run data independently of the page body, so a
    small read-only projection is attached per request by middleware.
    It never mutates state and degrades to :func:`_empty_shell` when
    the backing store is unreachable.

    Project Git validation flows through ``validation_cache`` when one
    is supplied so repeated requests do not re-run ``git status`` for
    every known project on every page view.

    The Inbox badge is deliberately NOT computed here: the Inbox
    projection is a cross-project, per-run lifecycle scan and must
    never run as a universal page-request cost.  The shell carries a
    plain Inbox navigation link (``inbox_count`` stays ``None``); only
    ``GET /inbox`` and ``GET /inbox.json`` build their own single
    snapshot.
    """
    try:
        active_repo = projects.active()
        active_path_str = str(active_repo) if active_repo else ""

        def _cached_validation(path: Path | str) -> ProjectValidation:
            if validation_cache is not None:
                return validation_cache.get(path)
            return projects.validate_project(path)

        val = _cached_validation(active_repo) if active_repo else None
        runs = _run_rows(service.list_runs(limit=50))
        terminal_runs = [row for row in runs if row["status"] in TERMINAL_RUN_STATUSES]
        live_runs = [row for row in runs if row["status"] not in TERMINAL_RUN_STATUSES]
        active_short = (
            Path(active_path_str).name if active_path_str else "SaberOps"
        )
        if live_runs:
            verdict = f"{len(live_runs)} active · {len(runs)} recent"
        elif runs:
            verdict = f"{len(runs)} runs · all terminal"
        else:
            verdict = "No runs yet"
        inbox_count: int | None = None
        # R3-D.1: no universal Inbox scan.  ``runtimes`` is accepted for
        # signature compatibility only and is never scanned here; the
        # Inbox count badge stays unset so ordinary pages never pay for
        # a cross-project lifecycle projection.
        _ = runtimes
        return {
            "active_project": active_path_str,
            "active_project_short": active_short,
            "repo_ready": val.ready if val else False,
            "repo_note": val.note if val else "No project selected",
            "recent_projects": [
                {
                    "path": p,
                    "ready": _cached_validation(p).ready,
                }
                for p in projects.list_projects()
            ],
            "recent_runs": runs,
            "terminal_runs": terminal_runs,
            "run_count": len(runs),
            "active_workers": len(live_runs),
            "status_verdict": verdict,
            "saberops_version": saberops_version(),
            "inbox_count": inbox_count,
        }
    except Exception:
        return _empty_shell()


def _access_store() -> AccessStore:
    """Return the user-connection store (overridable for tests).

    Production path is ``<owner_state_dir>/access-connections.json``;
    ``ORCH_ACCESS_STORE`` pins an explicit file (tests, diagnostics).
    Only credential *references* are ever persisted -- never values.
    """
    override = os.environ.get("ORCH_ACCESS_STORE", "").strip()
    if override:
        return AccessStore(Path(override).expanduser())
    return AccessStore(owner_state_dir() / AccessStore.FILENAME)


def _model_catalog_store() -> ModelCatalogStore:
    """Return the discovered-model catalog store (overridable for tests).

    Production path is ``<owner_state_dir>/access-models.json``;
    ``ORCH_ACCESS_MODELS_STORE`` pins an explicit file.  The catalog is
    non-secret evidence and is written only by discovery refresh, never
    by connection save/probe, so a discovery failure can never erase
    durable connection configuration.
    """
    override = os.environ.get("ORCH_ACCESS_MODELS_STORE", "").strip()
    if override:
        return ModelCatalogStore(Path(override).expanduser())
    return ModelCatalogStore(owner_state_dir() / ModelCatalogStore.FILENAME)


def _load_catalog_safely() -> ModelCatalog:
    """Load the last-observed catalog, degrading to empty on corrupt state."""
    try:
        return _model_catalog_store().load()
    except ModelCatalogStoreError:
        return ModelCatalog()


def _binding_store() -> OwnerBindingStore:
    """Return the owner binding store (overridable for tests).

    Production path is ``$XDG_CONFIG_HOME/saberops/bindings.json``;
    ``ORCH_BINDING_STORE`` pins an explicit file.  The document is C11-B
    owner identity only -- never a credential value, never a second routing
    configuration.
    """
    return OwnerBindingStore(get_binding_store_path())


def _load_owner_bindings_safely() -> OwnerBindings:
    """Load owner bindings, degrading to an empty document on corrupt state."""
    try:
        return _binding_store().load()
    except BindingStoreError:
        return OwnerBindings()


def _exact_binding_for(
    owner: OwnerBindings,
    *,
    provider: str,
    model: str,
    connection_id: str | None = None,
    role: BindingRole | None = None,
) -> Any | None:
    """Return the exact registry binding for the requested identity, if any.

    A raw provider/model string never synthesizes a binding: the lookup only
    returns an entry that already exists in the canonical registry.

    C15-XC-03: identity must include the connection (the real account the
    model is reachable through).  Two connections that expose the same
    provider/model pair register distinct bindings; the Web / catalog seam
    must not silently collapse them by returning the first registry
    declaration.  When ``connection_id`` is supplied the lookup derives the
    expected binding id through the canonical
    ``connection/model/role`` materialization scheme
    (``discovered-binding:<role>:<connection>:<model>``) and requires that
    exact id to exist in :class:`BindingRegistry`.  When ``connection_id``
    is absent the lookup falls back to the declaration-order match (the
    historical behavior for non-discovered bindings) but still filters by
    ``role`` when one is supplied.
    """
    wanted_provider = provider.strip().lower()
    wanted_model = model.strip()
    if not wanted_provider or not wanted_model:
        return None
    if connection_id:
        cleaned_connection = connection_id.strip()
        if cleaned_connection:
            role_slug = (
                role.value.lower() if role is not None else BindingRole.WORKER.value.lower()
            )
            expected_id = (
                f"discovered-binding:{role_slug}:{cleaned_connection}:{wanted_model}"
            )
            return owner.registry.try_get(expected_id)
    for binding in owner.registry.list_bindings():
        if binding.provider != wanted_provider or binding.model != wanted_model:
            continue
        if role is not None and getattr(binding, "binding_role", None) is not role:
            continue
        return binding
    return None


def _resolve_exact_binding(
    owner: OwnerBindings,
    registry: AdapterRegistry,
    *,
    provider: str,
    model: str,
    connection_id: str | None = None,
    role: BindingRole | None = None,
) -> Any | None:
    """Resolve the exact binding for a model through the real C11-B resolver.

    Returns the binding only when an exact registry entry exists AND
    :func:`resolve_execution_binding` succeeds against the real adapter;
    otherwise ``None`` (no provider/model fallback, no synthesized identity).

    C15-XC-03: the lookup accepts an explicit ``connection_id`` and
    ``role`` so the same provider/model pair reached through two
    different connections (or a WORKER-role / ORCHESTRATOR-role split)
    resolves to its own exact binding rather than the first registry
    declaration.
    """
    binding = _exact_binding_for(
        owner,
        provider=provider,
        model=model,
        connection_id=connection_id,
        role=role,
    )
    if binding is None:
        return None
    adapter = registry.get(binding.provider)
    if adapter is None:
        return None
    try:
        resolve_execution_binding(
            registry=owner.registry,
            binding_id=binding.binding_id,
            adapter=adapter,
        )
    except BindingResolutionError:
        return None
    return binding


def _exact_execution_support(
    owner: OwnerBindings,
    registry: AdapterRegistry,
    connection: ProviderConnection,
    model_id: str,
) -> ExecutionSupport:
    """Positive execution support requires an exact resolvable binding.

    An account-backed connection with a registered adapter is necessary but
    not sufficient: without a matching exact :class:`ExecutionBinding` the
    result stays ``UNKNOWN``.  API/BYOK connections have no execution
    transport and stay ``UNSUPPORTED``.

    C15-XC-03: the binding is resolved against the catalog row's own
    ``connection_id`` so a model reached through two different
    connections still resolves to *its* binding rather than the first
    registry declaration.
    """
    if not connection.is_account_backed:
        return ExecutionSupport.UNSUPPORTED
    if registry.get(connection.provider) is None:
        return ExecutionSupport.UNSUPPORTED
    if _resolve_exact_binding(
        owner,
        registry,
        provider=connection.provider,
        model=model_id,
        connection_id=connection.connection_id,
    ) is None:
        return ExecutionSupport.UNKNOWN
    return ExecutionSupport.SUPPORTED


def _http_get(
    url: str,
    headers: dict[str, str],
    opener: Callable[[str, dict[str, str]], bytes] | None,
) -> bytes:
    """Perform one bounded GET at the narrow transport boundary.

    ``opener`` is injectable so a credential-bearing request can be observed
    in tests without a live network; the production path uses urllib with a
    short timeout.  No request header is ever logged.
    """
    if opener is not None:
        return opener(url, headers)
    from urllib.request import Request as UrlRequest
    from urllib.request import urlopen

    request = UrlRequest(url, method="GET", headers=headers)
    with urlopen(request, timeout=5) as response:  # noqa: S310
        return bytes(response.read(1_000_000))


def _fetch_openai_compatible_models(
    endpoint: str,
    *,
    auth_headers: Callable[[], dict[str, str]] | None = None,
    opener: Callable[[str, dict[str, str]], bytes] | None = None,
) -> list[str]:
    """Enumerate models from an OpenAI-compatible ``/models`` endpoint.

    ``auth_headers`` is the caller-owned *narrow boundary*: when supplied, the
    caller has already resolved the configured credential through the existing
    security authority and returns only the header(s) needed here.  This
    function never resolves a credential itself and never persists the value.
    When ``auth_headers`` is ``None`` the request is deliberately
    unauthenticated, so a 401/403 means *authentication required* (never
    *configured credential rejected*).  Outcomes are classified, not collapsed
    to a boolean, and never echoed back as raw secret-bearing text.
    """
    from urllib.error import HTTPError, URLError

    if not endpoint:
        raise ModelListingError(ConnectionTestReason.CONFIG_INVALID, "endpoint not configured")
    url = endpoint.rstrip("/") + "/models"
    headers: dict[str, str] = {"Accept": "application/json"}
    credential_transmitted = False
    if auth_headers is not None:
        supplied = dict(auth_headers())
        if supplied:
            headers.update(supplied)
            credential_transmitted = True
    try:
        raw = _http_get(url, headers, opener)
    except HTTPError as exc:
        if exc.code in (401, 403):
            if credential_transmitted:
                raise ModelListingError(
                    ConnectionTestReason.CREDENTIAL_REJECTED,
                    f"model list rejected the configured credential (HTTP {exc.code})",
                ) from exc
            raise ModelListingError(
                ConnectionTestReason.NOT_AUTHENTICATED,
                f"model list requires authentication (HTTP {exc.code}); "
                "credential NOT transmitted",
            ) from exc
        if exc.code == 404:
            raise ModelListingError(
                ConnectionTestReason.ENDPOINT_UNREACHABLE,
                "model list endpoint not found (HTTP 404)",
            ) from exc
        raise ModelListingError(
            ConnectionTestReason.ENDPOINT_UNREACHABLE,
            f"model list request failed (HTTP {exc.code})",
        ) from exc
    except (URLError, OSError, ValueError) as exc:
        raise ModelListingError(
            ConnectionTestReason.ENDPOINT_UNREACHABLE,
            f"model list endpoint unreachable: {exc.__class__.__name__}",
        ) from exc
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError as exc:
        raise ModelListingError(
            ConnectionTestReason.PROTOCOL_UNSUPPORTED, "model list response was not JSON"
        ) from exc
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise ModelListingError(
            ConnectionTestReason.PROTOCOL_UNSUPPORTED,
            "model list response missing 'data' array",
        )
    models = [
        str(item.get("id", "")).strip()
        for item in data
        if isinstance(item, dict)
    ]
    return [model_id for model_id in models if model_id]


#: Bounded wall-clock budget for one backend model-list subprocess.
_BACKEND_LIST_TIMEOUT_SECONDS = 15.0


def _run_backend_listing(argv: list[str]) -> str:
    """Run one backend's supported model-list command (bounded, no secrets).

    The backend reads its own account session; Orchestrator passes no
    credential and captures only stdout.  A non-zero exit is classified and
    its stderr is never surfaced (it may carry session material).
    """
    import subprocess

    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_BACKEND_LIST_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ModelListingError(
            ConnectionTestReason.ENDPOINT_UNREACHABLE,
            f"backend model listing failed: {exc.__class__.__name__}",
        ) from exc
    if completed.returncode != 0:
        raise ModelListingError(
            ConnectionTestReason.ENDPOINT_UNREACHABLE,
            f"backend model listing exited {completed.returncode}",
        )
    return completed.stdout or ""


def _normalized_backend_model_id(raw: str) -> str:
    """Return one backend-enumerated model id verbatim (whitespace-trimmed).

    A backend's enumerated model identity is provider-native and may carry
    an upstream namespace that is part of the identity the adapter must
    invoke.  ``opencode models`` lists the OpenCode-native upstream as
    ``opencode/<model>`` and its sibling upstreams as ``<upstream>/<model>``;
    the SaberOps connection ``provider`` (for example ``opencode``) names the
    CLI that carries the request, not the upstream namespace.  Stripping a
    leading ``<provider>/`` would therefore collapse ``opencode/<model>``
    into a bare name and silently merge it with an unrelated same-named
    upstream (for example ``google/<model>``).  The raw listing is the
    authoritative executable identity, so no prefix is ever removed here.
    """
    return raw.strip()


def _model_ids_from_sequence(items: list[Any]) -> list[str]:
    ids: list[str] = []
    for item in items:
        if isinstance(item, str):
            ids.append(_normalized_backend_model_id(item))
        elif isinstance(item, dict):
            for key in ("id", "model", "slug", "name"):
                candidate = item.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    ids.append(_normalized_backend_model_id(candidate.strip()))
                    break
    return [model_id for model_id in ids if model_id]


def _parse_backend_model_list(stdout: str) -> list[str]:
    """Parse a backend's supported model-list output conservatively.

    Recognises a JSON catalog (``codex debug models``) and the
    ``provider/model`` line format (``opencode models``).  Model ids are
    preserved verbatim so provider-native upstream namespaces survive
    discovery (see :func:`_normalized_backend_model_id`).  Anything that
    cannot be parsed with confidence yields no ids rather than invented ones.
    """
    text = stdout.strip()
    if not text:
        return []
    try:
        payload: Any = json.loads(text)
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        for key in ("models", "data", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return _model_ids_from_sequence(value)
        return []
    if isinstance(payload, list):
        return _model_ids_from_sequence(payload)
    ids: list[str] = []
    for line in text.splitlines():
        cleaned = line.strip()
        if not cleaned or " " in cleaned:
            continue
        ids.append(_normalized_backend_model_id(cleaned))
    return [model_id for model_id in ids if model_id]


def _list_account_models(connection: ProviderConnection) -> list[str]:
    """Enumerate models through a backend's own supported listing command."""
    descriptor = next(
        (
            entry
            for entry in ACCOUNT_BACKENDS
            if entry.connection_id == connection.connection_id
        ),
        None,
    )
    if descriptor is None or not descriptor.model_list_argv:
        raise ModelListingError(
            ConnectionTestReason.CAPABILITY_UNKNOWN,
            "backend exposes no supported model enumeration",
        )
    executable = shutil.which(descriptor.executable)
    if executable is None:
        raise ModelListingError(
            ConnectionTestReason.BACKEND_NOT_INSTALLED,
            f"backend executable '{descriptor.executable}' not found",
        )
    stdout = _run_backend_listing([executable, *descriptor.model_list_argv])
    return _parse_backend_model_list(stdout)


def _materialize_discovered_binding(
    connection: ProviderConnection,
    model_id: str,
    registry: AdapterRegistry,
) -> tuple[OwnerBindings, Any] | None:
    """Translate validated discovery evidence into exact C11-B bindings.

    Discovery creates the *role-qualified* binding identities that share
    the same account / profile and connection-scoped quota-pool
    resource:

    * WORKER-role binding: eligible for the worker-routing ladder.
    * REVIEWER-role binding: eligible for the owner-selected reviewer slot.
    * ORCHESTRATOR-role binding: eligible for the Orchestrator slot.

    All three share the same ``profile_id`` (the real connection /
    account) and ``quota_pool_id`` (the real connection-scoped
    scarcity resource); only their ``binding_id`` carries the semantic
    role so /routing/add, /reviewer/select, and /orchestrator/select
    each see their own exact binding and C07 remains authoritative
    over the real quota pool.  Two distinct account connections to the
    same provider register distinct ``profile_id`` and distinct
    ``quota_pool_id`` -- provider identity is not quota-resource
    identity, and semantic role never duplicates either resource.

    The exact bindings are persisted only when the WORKER and REVIEWER
    bindings prove through the real C11-B resolver with the real
    adapter, so a provider/model name can never become executable
    without an exact binding that actually resolves.  Returns
    ``(owner, worker_binding)`` or ``None``.
    """
    adapter = registry.get(connection.provider)
    if adapter is None:
        return None
    profile = capability_profile_for(adapter)
    worker_binding, execution_profile, pool = materialize_discovered_binding(
        connection_id=connection.connection_id,
        provider=connection.provider,
        model=model_id,
        provider_transport=profile.transport,
        harness_capabilities=profile.capabilities,
        binding_role=BindingRole.WORKER,
    )
    reviewer_binding, _reviewer_profile, _reviewer_pool = materialize_discovered_binding(
        connection_id=connection.connection_id,
        provider=connection.provider,
        model=model_id,
        provider_transport=profile.transport,
        harness_capabilities=profile.capabilities,
        binding_role=BindingRole.REVIEWER,
    )
    orch_binding, _orch_profile, _orch_pool = materialize_discovered_binding(
        connection_id=connection.connection_id,
        provider=connection.provider,
        model=model_id,
        provider_transport=profile.transport,
        harness_capabilities=profile.capabilities,
        binding_role=BindingRole.ORCHESTRATOR,
    )
    owner = _load_owner_bindings_safely()
    candidate = upsert_owner_binding(
        owner, binding=worker_binding, profile=execution_profile, pool=pool
    )
    candidate = upsert_owner_binding(
        candidate, binding=reviewer_binding, profile=execution_profile, pool=pool
    )
    candidate = upsert_owner_binding(
        candidate, binding=orch_binding, profile=execution_profile, pool=pool
    )
    try:
        resolve_execution_binding(
            registry=candidate.registry,
            binding_id=worker_binding.binding_id,
            adapter=adapter,
        )
        resolve_execution_binding(
            registry=candidate.registry,
            binding_id=reviewer_binding.binding_id,
            adapter=adapter,
        )
    except BindingResolutionError:
        return None
    if not _save_owner_bindings(candidate):
        return None
    return candidate, worker_binding


def _save_owner_bindings(owner: OwnerBindings) -> bool:
    """Persist owner bindings; return ``True`` only when durable."""
    try:
        save_owner_bindings(owner)
    except BindingStoreError:
        return False
    return True


def _apply_exact_execution_bindings(
    connection: ProviderConnection,
    result: ModelDiscoveryResult,
    registry: AdapterRegistry,
) -> ModelDiscoveryResult:
    """Replace inferred execution support with exact-binding evidence.

    A successful enumeration on an account-backed connection with a real
    adapter materializes one exact binding per discovered model and proves it
    through the canonical resolver.  API/BYOK models and models with no
    resolvable exact binding stay ``UNSUPPORTED`` / ``UNKNOWN``.  A failed or
    ``UNKNOWN`` refresh never creates a binding.
    """
    if not result.ok:
        return result
    owner = _load_owner_bindings_safely()
    support: dict[tuple[str, str, str, str], ExecutionSupport] = {}
    for model in result.models:
        if model.source is not ModelSource.DISCOVERED or model.is_stale:
            support[model.identity] = model.execution_support
            continue
        if not connection.is_account_backed:
            support[model.identity] = ExecutionSupport.UNSUPPORTED
            continue
        if registry.get(connection.provider) is None:
            support[model.identity] = ExecutionSupport.UNSUPPORTED
            continue
        materialized = _materialize_discovered_binding(
            connection, model.model_id, registry
        )
        if materialized is None:
            support[model.identity] = ExecutionSupport.UNKNOWN
            continue
        owner, _binding = materialized
        support[model.identity] = ExecutionSupport.SUPPORTED
    return replace(
        result,
        models=tuple(
            replace(model, execution_support=support.get(model.identity, model.execution_support))
            for model in result.models
        ),
    )


def _discover_connection(
    connection: ProviderConnection,
    registry: AdapterRegistry,
) -> ModelDiscoveryResult:
    """Run bounded discovery for one connection.

    Account-backed backends use their own supported listing boundary where
    one exists (``codex debug models``, ``opencode models``); backends with no
    supported enumeration stay truthfully ``UNKNOWN`` rather than inventing a
    catalog.  API connections attempt an unauthenticated OpenAI-compatible
    ``/models`` listing: there is currently no canonical read-only credential
    resolution seam suitable for this call, so an auth-requiring endpoint is
    reported as NOT_AUTHENTICATED rather than as a rejected credential.

    Execution support is proven *after* enumeration by materializing an exact
    C11-B binding and resolving it with the real adapter -- never inferred
    from the mere presence of a registered adapter.
    """
    discovered_at = datetime.now(UTC).isoformat()
    if connection.is_account_backed:
        descriptor = next(
            (
                entry
                for entry in ACCOUNT_BACKENDS
                if entry.connection_id == connection.connection_id
            ),
            None,
        )
        if descriptor is not None and descriptor.model_list_argv:
            result = discover_connection_models(
                connection,
                list_models=lambda: _list_account_models(connection),
                discovered_at=discovered_at,
            )
        else:
            result = discover_connection_models(
                connection,
                discovered_at=discovered_at,
            )
    else:
        result = discover_connection_models(
            connection,
            list_models=lambda: _fetch_openai_compatible_models(connection.endpoint),
            discovered_at=discovered_at,
        )
    return _apply_exact_execution_bindings(connection, result, registry)


def _model_display_row(
    model: Any,
    *,
    eligibility: dict[str, str] | None = None,
) -> dict[str, str]:
    """Render one catalog entry for the access page (evidence only)."""
    capabilities = ", ".join(
        f"{name}={state.value}" for name, state in model.capabilities
    )
    row = {
        "model_id": model.model_id,
        "display_name": model.display_name or model.model_id,
        "source": model.source.value,
        "discovery_status": model.discovery_status.value,
        "execution_support": model.execution_support.value,
        "discovered_at": model.discovered_at or "UNKNOWN",
        "capabilities": capabilities or "UNKNOWN",
        "evidence": "STALE" if model.is_stale else "FRESH",
        "routing_eligible": "TRUE" if model.is_execution_supported else "FALSE",
    }
    if eligibility is not None:
        row["routing_eligible"] = eligibility.get("routing_eligible", "FALSE")
        row["ineligible_reason"] = eligibility.get("ineligible_reason", "")
    else:
        row["ineligible_reason"] = ""
    return row


def _readiness_store() -> ReadinessStore:
    """Return the readiness-evidence store (overridable for tests).

    Production path is ``<owner_state_dir>/access-readiness.json``;
    ``ORCH_READINESS_STORE`` pins an explicit file.  The store carries
    only non-secret observations; the Verify action writes through this
    same seam while every other read path shows existing evidence only.
    """
    override = os.environ.get("ORCH_READINESS_STORE", "").strip()
    if override:
        return ReadinessStore(Path(override).expanduser())
    return ReadinessStore(owner_state_dir() / ReadinessStore.FILENAME)


def _readiness_age_display(observed_at: str) -> str:
    """Render the age of a readiness observation from its timestamp.

    The timestamp is the evidence; the age is derived from it, never
    fabricated.  An unreadable timestamp degrades to ``unknown age``.
    """
    try:
        observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        if observed.tzinfo is None:
            raise ValueError("naive timestamp")
        seconds = max(
            0.0, (datetime.now(UTC) - observed.astimezone(UTC)).total_seconds()
        )
    except (ValueError, TypeError, OverflowError):
        return "unknown age"
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def _readiness_presentation(
    connection_id: str,
    catalog: ReadinessCatalog | None,
) -> dict[str, Any]:
    """Project the persisted readiness evidence for one connection.

    Display only: shows the actual stored state/reason/observation time
    and whether the existing freshness policy still considers it fresh.
    No probing, no refresh, no invented readiness.  When no evidence
    exists the row says ``Not verified yet`` instead of looking like an
    execution failure.  A binary/CLI being installed is reported
    separately by the caller and is never presented as proven model
    execution readiness.
    """
    if catalog is None:
        return {
            "display": "Readiness store unreadable",
            "state": ProviderReadiness.UNKNOWN.value,
            "reason": "READINESS_AUTHORITY_UNAVAILABLE",
            "observed_at": "",
            "age": "unknown age",
            "fresh": False,
        }
    evidence = catalog.try_get(connection_id)
    if evidence is None:
        return {
            "display": "Not verified yet",
            "state": ProviderReadiness.UNKNOWN.value,
            "reason": "INSUFFICIENT_EVIDENCE",
            "observed_at": "",
            "age": "not observed",
            "fresh": False,
        }
    projected = evidence.freshness(
        as_of=datetime.now(UTC), max_age=DEFAULT_READINESS_MAX_AGE
    )
    return {
        "display": projected.state.value,
        "state": projected.state.value,
        "reason": projected.reason,
        "observed_at": evidence.observed_at,
        "age": _readiness_age_display(evidence.observed_at),
        "fresh": projected.state == evidence.state,
    }


def _access_context(
    *,
    error: str | None = None,
    notice: str | None = None,
    form: dict[str, Any] | None = None,
    probe: dict[str, Any] | None = None,
    discovery: list[dict[str, Any]] | None = None,
    owner: OwnerBindings | None = None,
    registry: AdapterRegistry | None = None,
) -> dict[str, Any]:
    """Build the model-access template context (evidence only, no secrets)."""
    try:
        access_registry = _access_store().load()
    except AccessStoreError:
        access_registry = build_access_registry(default_account_connections())
    catalog = _load_catalog_safely()
    resolved_owner = owner if owner is not None else _load_owner_bindings_safely()
    resolved_registry = registry if registry is not None else AdapterRegistry.default()

    def rows_for(connection_id: str) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        for model in catalog.for_connection(connection_id):
            eligibility = _routing_chooser_row(
                model, owner=resolved_owner, registry=resolved_registry
            )
            rows.append(_model_display_row(model, eligibility=eligibility))
        return rows

    account_rows: list[dict[str, Any]] = []
    try:
        readiness_catalog = _readiness_store().load()
    except ReadinessStoreError:
        readiness_catalog = None
    for descriptor in ACCOUNT_BACKENDS:
        connection = access_registry.try_get(descriptor.connection_id)
        resolved = shutil.which(descriptor.executable)
        account_rows.append(
            {
                "connection_id": descriptor.connection_id,
                "provider": descriptor.provider,
                "backend": descriptor.backend,
                "auth_mode": descriptor.auth_mode.value,
                "installed": "TRUE" if resolved is not None else "FALSE",
                "installed_class": _status_class(
                    "available" if resolved is not None else "not-installed"
                ),
                "installed_detail": resolved or "not on PATH",
                # Authentication is owned by the backend itself; the
                # dashboard never extracts or inspects session material,
                # so live auth state stays UNKNOWN until a supported
                # backend status probe is wired per backend.
                "authenticated": TriState.UNKNOWN.value,
                # Persisted model-execution readiness evidence (display
                # only; never refreshed or probed here).  A CLI being
                # installed is availability, not proven readiness.
                "readiness": _readiness_presentation(
                    descriptor.connection_id, readiness_catalog
                ),
                "present": "TRUE" if connection is not None else "FALSE",
                "models": rows_for(descriptor.connection_id),
            }
        )
    account_ids = {descriptor.connection_id for descriptor in ACCOUNT_BACKENDS}
    api_rows = [
        {
            "connection_id": connection.connection_id,
            "provider": connection.provider,
            "protocol": connection.protocol.value,
            "backend": connection.backend,
            "auth_mode": connection.auth_mode.value,
            "endpoint": connection.endpoint,
            "credential": (
                f"{connection.auth_header_name} ← {connection.credential_display}"
                if connection.auth_header_name
                else connection.credential_display
            ),
            "model": connection.model,
            "enabled": "TRUE" if connection.enabled else "FALSE",
            "models": rows_for(connection.connection_id),
        }
        for connection in access_registry.list_connections()
        if connection.connection_id not in account_ids
    ]
    empty = {
        "connection_id": "",
        "preset": "custom-openai-compatible",
        "protocol": AccessProtocol.OPENAI_COMPATIBLE.value,
        "endpoint": "",
        "credential_ref": "",
        "auth_header_name": "",
        "model": "",
        "public_headers": "",
    }
    return {
        "account_rows": account_rows,
        "api_rows": api_rows,
        "presets": list_presets(),
        "protocols": [protocol.value for protocol in AccessProtocol],
        "form": form if form is not None else dict(empty),
        "probe": probe,
        "discovery": discovery or [],
        "error": error,
        "notice": notice,
    }


def _routing_context_keys() -> tuple[tuple[str, str], ...]:
    """The canonical ``(tier, context)`` pairs owned by routing config."""
    return tuple(CONTEXT_KEYS)


def _orchestrator_context(
    *,
    error: str | None = None,
    notice: str | None = None,
    registry: AdapterRegistry | None = None,
) -> dict[str, Any]:
    """Build the Orchestrator-model selection context.

    The page lists only canonical ``ORCHESTRATOR`` bindings whose profile is
    enabled and whose exact identity resolves through the real adapter.  The
    persisted choice is an :class:`OrchBindingPolicy` over binding ids --
    never a free-floating provider/model setting.

    C15-XC-03: each candidate is validated against ITS OWN exact binding
    identity (the row's own binding_id) instead of "any other binding
    with the same provider/model" so the visible roster can never drift
    away from the bindings the registry actually names.
    """
    owner = _load_owner_bindings_safely()
    resolved_registry = registry if registry is not None else AdapterRegistry.default()
    rows: list[dict[str, str]] = []
    for binding in owner.registry.list_bindings():
        if not binding.is_orchestrator_capable:
            continue
        profile = owner.registry.profile_of(binding)
        # Validate using THIS binding's own identity, not a provider/model
        # match: an ORCHESTRATOR-role binding with no live adapter (or a
        # stale profile / pool) is correctly refused.
        adapter = resolved_registry.get(binding.provider)
        resolvable = False
        if adapter is not None and profile.enabled:
            try:
                resolve_execution_binding(
                    registry=owner.registry,
                    binding_id=binding.binding_id,
                    adapter=adapter,
                )
                resolvable = True
            except BindingResolutionError:
                resolvable = False
        if not resolvable:
            continue
        rows.append(
            {
                "binding_id": binding.binding_id,
                "provider": binding.provider,
                "model": binding.model,
                "backend": binding.backend.value,
                "profile_id": binding.profile_id,
                "quota_pool_id": binding.quota_pool_id,
                "profile_enabled": "TRUE" if profile.enabled else "FALSE",
            }
        )
    current: dict[str, Any] | None = None
    selection_status = "NOT CONFIGURED"
    if owner.policy is not None:
        current = {
            "mode": owner.policy.mode.value,
            "pinned_binding_id": owner.policy.pinned_binding_id or "",
            "preferred": list(owner.policy.preferred_binding_ids),
        }
        selection = select_orch_binding(owner.policy, owner.registry)
        selection_status = selection.status.value
        if selection.reason:
            selection_status = f"{selection.status.value} ({selection.reason})"
    return {
        "error": error,
        "notice": notice,
        "bindings": rows,
        "current": current,
        "selection_status": selection_status,
        "pinned_value": "" if current is None else str(current["pinned_binding_id"]),
        "auto_selected": (
            current is not None and current["mode"] == OrchBindingMode.AUTO.value
        ),
    }


def _reviewer_context(
    *,
    error: str | None = None,
    notice: str | None = None,
    registry: AdapterRegistry | None = None,
) -> dict[str, Any]:
    """Build the Reviewer-model selection context.

    The page lists only canonical ``REVIEWER`` bindings whose profile is
    enabled and whose exact identity resolves through the real adapter.
    The persisted choice is one exact binding id (never a free-floating
    provider/model setting, never a pool, never an ordered list).

    C15-XC-03: each candidate is validated against ITS OWN exact binding
    identity (the row's own binding_id) instead of "any other binding
    with the same provider/model" so the visible roster can never drift
    away from the bindings the registry actually names.
    """
    owner = _load_owner_bindings_safely()
    resolved_registry = registry if registry is not None else AdapterRegistry.default()
    rows: list[dict[str, str]] = []
    for binding in owner.registry.list_bindings():
        if binding.binding_role is not BindingRole.REVIEWER:
            continue
        profile = owner.registry.profile_of(binding)
        # Validate using THIS binding's own identity, not a provider/model
        # match: a REVIEWER-role binding with no live adapter (or a
        # stale profile / pool) is correctly refused.
        adapter = resolved_registry.get(binding.provider)
        resolvable = False
        if adapter is not None and profile.enabled:
            try:
                resolve_execution_binding(
                    registry=owner.registry,
                    binding_id=binding.binding_id,
                    adapter=adapter,
                )
                resolvable = True
            except BindingResolutionError:
                resolvable = False
        if not resolvable:
            continue
        rows.append(
            {
                "binding_id": binding.binding_id,
                "provider": binding.provider,
                "model": binding.model,
                "backend": binding.backend.value,
                "profile_id": binding.profile_id,
                "quota_pool_id": binding.quota_pool_id,
                "profile_enabled": "TRUE" if profile.enabled else "FALSE",
            }
        )
    current = owner.reviewer_binding_id
    selection = select_reviewer_binding(owner, adapter_registry=resolved_registry)
    if selection.status is ReviewerSelectionStatus.NOT_CONFIGURED:
        selection_status = "NOT CONFIGURED"
    elif selection.status is ReviewerSelectionStatus.RESOLVED:
        selection_status = "RESOLVED"
    else:
        selection_status = f"UNRESOLVED ({selection.reason})"
    return {
        "error": error,
        "notice": notice,
        "bindings": rows,
        "current": current,
        "selection_status": selection_status,
        "selected_value": "" if current is None else str(current),
    }


def _routing_chooser_row(
    model: Any,
    *,
    owner: OwnerBindings,
    registry: AdapterRegistry,
) -> dict[str, str]:
    """Render one catalog entry for the routing chooser (evidence only).

    Eligibility means *admissible to the owner's add action*, not automatic
    enrollment.  A model must be discovery-verified (not merely owner-typed),
    fresh, have a valid canonical identity, and have an **exact C11-B
    binding** that actually resolves through the real adapter.  A registered
    adapter alone is never enough: without a resolvable exact binding the
    entry is disabled with a truthful reason.

    C15-XC-03: the exact binding is resolved against the catalog row's
    own ``connection_id`` (the real account) so two connections that
    expose the same provider/model pair never collapse: registry
    declaration order cannot select a sibling binding the owner did not
    click.
    """
    provider = model.provider
    try:
        candidate_id = format_candidate_id(provider, model.model_id)
    except RoutingConfigError:
        candidate_id = ""
    binding = _exact_binding_for(
        owner,
        provider=provider,
        model=model.model_id,
        connection_id=model.connection_id,
        role=BindingRole.WORKER,
    )
    binding_id = "" if binding is None else binding.binding_id
    if not candidate_id:
        eligible = False
        reason = "invalid candidate identity"
    elif not model.is_discovery_verified:
        eligible = False
        reason = "model is owner-declared, not discovery-verified"
    elif model.is_stale:
        eligible = False
        reason = "discovery evidence is stale; refresh models first"
    elif binding is None:
        eligible = False
        reason = "No executable binding configured"
    elif _resolve_exact_binding(
        owner,
        registry,
        provider=provider,
        model=model.model_id,
        connection_id=model.connection_id,
        role=BindingRole.WORKER,
    ) is None:
        eligible = False
        reason = "exact binding does not resolve for this adapter"
    elif binding.binding_role is not BindingRole.WORKER:
        # C15-XB-03: worker routing requires a WORKER-role binding.
        # An ORCHESTRATOR-only binding cannot authorize WORKER execution.
        eligible = False
        reason = (
            "exact binding is ORCHESTRATOR-role, not eligible for worker routing"
        )
    else:
        eligible = True
        reason = ""
    return {
        "connection_id": model.connection_id,
        "provider": provider,
        "backend": model.backend,
        "model_id": model.model_id,
        "candidate_id": candidate_id,
        "binding_id": binding_id,
        "discovery_status": model.discovery_status.value,
        "execution_support": model.execution_support.value,
        "stale": "TRUE" if model.is_stale else "FALSE",
        "routing_eligible": "TRUE" if eligible else "FALSE",
        "ineligible_reason": reason,
    }


def _routing_context(
    *,
    error: str | None = None,
    notice: str | None = None,
    owner: OwnerBindings | None = None,
    registry: AdapterRegistry | None = None,
) -> dict[str, Any]:
    """Build the routing-selection context.

    Routing configuration stays canonical: the page renders the *loaded*
    chains and offers discovered candidates that have an exact resolvable
    C11-B binding.  The user decides what enters routing; nothing is enrolled
    automatically.  Ineligible entries are shown disabled with a truthful
    reason.
    """
    resolved_owner = owner if owner is not None else _load_owner_bindings_safely()
    resolved_registry = registry if registry is not None else AdapterRegistry.default()
    catalog = _load_catalog_safely()
    chains: list[dict[str, Any]] = []
    try:
        payload = load_effective_routing_payload()
    except RoutingConfigError as exc:
        return {
            "error": error or f"Routing config unreadable: {exc}",
            "notice": notice,
            "chains": [],
            "chooser": [],
            "contexts": [f"{top}.{sub}" for top, sub in CONTEXT_KEYS],
        }
    for top, sub in CONTEXT_KEYS:
        section = payload.get(top)
        chain = section.get(sub) if isinstance(section, dict) else None
        chains.append(
            {
                "tier": top,
                "context": sub,
                "context_key": f"{top}.{sub}",
                "candidates": [str(cid) for cid in chain] if isinstance(chain, list) else [],
            }
        )
    chooser = [
        _routing_chooser_row(model, owner=resolved_owner, registry=resolved_registry)
        for model in catalog.models
    ]
    return {
        "error": error,
        "notice": notice,
        "chains": chains,
        "chooser": chooser,
        "contexts": [f"{top}.{sub}" for top, sub in CONTEXT_KEYS],
    }


def _setup_context(
    projects: ProjectRegistry,
    *,
    validation_cache: ProjectValidationCache,
    registry: AdapterRegistry,
) -> dict[str, Any]:
    """Build one UI-only first-user setup checklist from canonical stores."""
    from saberops.paths import active_profile

    access = _access_context(registry=registry)
    routing = _routing_context(registry=registry)
    orch = _orchestrator_context(registry=registry)
    reviewer = _reviewer_context(registry=registry)
    active_repo = projects.active()
    validation = validation_cache.get(active_repo) if active_repo is not None else None
    settings: dict[str, object] | None = None
    if active_repo is not None:
        summary = effective_policy_summary(active_repo)
        if "error" not in summary:
            settings = summary

    discovered = sum(
        len(row.get("models", ()))
        for row in (*access["account_rows"], *access["api_rows"])
    )
    eligible = sum(
        1 for row in routing["chooser"] if row.get("routing_eligible") == "TRUE"
    )
    routed = sum(len(chain["candidates"]) for chain in routing["chains"])
    orch_ready = bool(
        orch["current"] is not None
        and str(orch["selection_status"]).startswith("RESOLVED")
    )
    reviewer_ready = bool(str(reviewer["selection_status"]).startswith("RESOLVED"))
    project_ready = bool(validation and validation.ready)
    project_policy_ready = bool(
        settings
        and settings.get("gate_effective")
        and settings.get("accept_target_branch_effective")
    )
    return {
        "profile": active_profile() or "default",
        "account_rows": access["account_rows"],
        "discovered_model_count": discovered,
        "routing_eligible_count": eligible,
        "routed_model_count": routed,
        "routing_chains": routing["chains"],
        "orch_current": orch["current"],
        "orch_status": orch["selection_status"],
        "orch_ready": orch_ready,
        "reviewer_current": reviewer["current"],
        "reviewer_status": reviewer["selection_status"],
        "reviewer_ready": reviewer_ready,
        "active_project": str(active_repo) if active_repo is not None else "",
        "project_ready": project_ready,
        "project_note": validation.note if validation is not None else "No project selected",
        "project_settings": settings,
        "project_policy_ready": project_policy_ready,
        "ready_for_dogfood": bool(
            project_ready
            and project_policy_ready
            and routed > 0
            and orch_ready
            and reviewer_ready
        ),
    }


#: Raw text fields the ``/access`` connection form re-renders verbatim.
#: A rejected submission is untrusted as a whole, so these are blanked
#: before an error re-render rather than replayed.  ``preset`` and
#: ``protocol`` are deliberately absent: the template renders them by
#: comparison against known options, never as submitted text, so they
#: cannot reflect input.
_ACCESS_REJECTED_TEXT_FIELDS = (
    "connection_id",
    "endpoint",
    "credential_ref",
    "auth_header_name",
    "model",
    "public_headers",
)


def _rejected_access_form(form: dict[str, Any]) -> dict[str, Any]:
    """Return an error-form copy that replays no raw submitted text.

    Rejected potentially sensitive free-text must not be replayed into
    the error response merely to preserve form state.  Blanking is
    structural, not heuristic: it makes no attempt to judge whether a
    value is a secret, it simply never echoes untrusted free text back.
    """
    sanitized = dict(form)
    for field_name in _ACCESS_REJECTED_TEXT_FIELDS:
        sanitized[field_name] = ""
    return sanitized


def _parse_public_headers(raw: str) -> dict[str, str]:
    """Parse literal ``Name: value`` public-header lines (fail closed).

    Parse failures are content-free by contract: a malformed line is
    identified by its 1-based line number and the structural defect
    only.  Submitted line text is never echoed, so a rejected line
    that happens to carry secret-shaped material (for example a
    pasted ``Authorization Bearer ...`` line missing its colon) can
    never be reflected into the rendered error page.  Credential-like
    header *names* are rejected by the connection contract at
    construction; this parser only enforces line shape here.
    """
    headers: dict[str, str] = {}
    for number, line in enumerate(raw.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        name, separator, value = stripped.partition(":")
        if not separator:
            raise ValueError(f"invalid public header line {number}: missing ':' separator")
        if not name.strip():
            raise ValueError(f"invalid public header line {number}: missing header name")
        if not value.strip():
            raise ValueError(f"invalid public header line {number}: missing header value")
        headers[name.strip()] = value.strip()
    return headers


def _probe_api_connection(*, endpoint: str) -> dict[str, str]:
    """Safely probe an API connection without transmitting any secret.

    The probe performs an unauthenticated GET against the configured
    endpoint with a short timeout: any HTTP response proves
    reachability (authentication itself is reported as NOT VERIFIED --
    the stored credential value is never read, let alone sent).
    """
    from urllib.error import HTTPError, URLError
    from urllib.request import Request as UrlRequest
    from urllib.request import urlopen

    if not endpoint:
        return {"ok": "FALSE", "reason": "CONFIG_INVALID", "detail": "endpoint not configured"}
    request = UrlRequest(endpoint, method="GET")
    try:
        with urlopen(request, timeout=5) as response:  # noqa: S310
            status = int(getattr(response, "status", 200))
    except HTTPError as exc:
        # An HTTP answer -- even 401/403 -- proves the endpoint is
        # reachable; auth acceptance stays NOT VERIFIED by design.
        return {
            "ok": "TRUE",
            "reason": ConnectionTestReason.OK.value,
            "detail": (
                f"endpoint reachable (HTTP {exc.code}); "
                "authentication NOT VERIFIED (credential never transmitted)"
            ),
        }
    except (URLError, OSError, ValueError) as exc:
        return {
            "ok": "FALSE",
            "reason": ConnectionTestReason.ENDPOINT_UNREACHABLE.value,
            "detail": f"endpoint unreachable: {exc.__class__.__name__}",
        }
    return {
        "ok": "TRUE",
        "reason": ConnectionTestReason.OK.value,
        "detail": (
            f"endpoint reachable (HTTP {status}); "
            "authentication NOT VERIFIED (credential never transmitted)"
        ),
    }


def _reviewed_query_value(verdict: ReviewVerdict) -> str:
    """Map an authoritative review verdict to a redirect query token.

    Substantive reviewer outcomes (a reviewer actually completed a review)
    keep the historical ``1`` token.  Infrastructure states that launched no
    usable reviewer get their own token so the rendered notice stays
    truthful.  Any other non-substantive verdict falls through to
    ``failed``: it must never claim successful completion.
    """
    if verdict == ReviewVerdict.REVIEW_UNAVAILABLE:
        return "unavailable"
    if verdict in (
        ReviewVerdict.PASS,
        ReviewVerdict.BLOCK,
        ReviewVerdict.PASS_WITH_BACKLOG,
        ReviewVerdict.FINAL_BLOCK,
    ):
        return "1"
    return "failed"


def _notice_from_query(request: Request) -> str | None:
    """Translate a success-redirect flag into a human notice."""
    reviewed = request.query_params.get("reviewed")
    if reviewed == "1":
        return "Review completed."
    if reviewed == "unavailable":
        return "Review unavailable."
    if reviewed == "failed":
        return "Review failed."
    if request.query_params.get("cleaned") == "1":
        return "Cleanup completed."
    if request.query_params.get("rejected") == "1":
        return "Candidate rejected (durable owner decision)."
    if request.query_params.get("review_queued") == "1":
        return "Review queued: a detached owner action is running; this page updates via live events."
    if request.query_params.get("accept_queued") == "1":
        return "Accept queued: a detached owner action is running; this page updates via live events."
    if request.query_params.get("action_active") == "review":
        return "A Review owner action is already running for this candidate."
    if request.query_params.get("action_active") == "accept":
        return "An Accept owner action is already running for this candidate."
    if request.query_params.get("updated") == "1":
        return "Quota updated."
    if request.query_params.get("enabled") == "1":
        return "Quota enabled."
    if request.query_params.get("connection_saved") == "1":
        auto = request.query_params.get("auto_discovered")
        if auto is not None:
            try:
                count = int(auto)
            except ValueError:
                count = -1
            if count >= 0:
                return (
                    "Connection saved (credential reference only; no secret stored). "
                    f"Bounded discovery ran automatically and found {count} model(s) "
                    "below; adding a provider only makes models available, it does "
                    "not place them into routing."
                )
        if request.query_params.get("auto_discover_failed") == "1":
            return (
                "Connection saved (credential reference only; no secret stored). "
                "Automatic discovery could not enumerate models; use "
                "'Refresh models' to retry. Nothing was invented."
            )
        return "Connection saved (credential reference only; no secret stored)."
    if request.query_params.get("connection_tested") == "1":
        return "Connection probed (see result below)."
    if request.query_params.get("routing_model_added") == "1":
        return "Model added to the routing chain (owner decision)."
    if request.query_params.get("routing_model_removed") == "1":
        return "Model removed from the routing chain."
    if request.query_params.get("routing_model_moved") == "1":
        return "Routing chain reordered (canonical order saved)."
    if request.query_params.get("selected") == "1":
        return "Orchestrator model selection saved (exact binding identity)."
    if request.query_params.get("project_selected") == "1":
        return "Target project updated."
    return None


def create_app(
    db_path: str | Path | None = None,
    repo_path: str | Path | None = None,
    registry: AdapterRegistry | None = None,
    worktree_base: Path | None = None,
    projects: ProjectRegistry | None = None,
    state_root: Path | str | None = None,
    seed_cwd_if_empty: bool = True,
) -> FastAPI:
    """Build the FastAPI dashboard application.

    Runtime state is resolved per project: the dashboard shows the currently
    selected project's runs, while every action on an *existing* run resolves
    back to that run's own project through :class:`ProjectRuntimeManager`.
    Passing an explicit ``db_path`` pins the whole app to that single store.
    """
    resolved_registry = registry if registry is not None else AdapterRegistry.default()
    if projects is None:
        storage_path = (
            Path(db_path).resolve().parent / "ui-projects.json" if db_path is not None else None
        )
        project_registry = ProjectRegistry(path=storage_path)
    else:
        project_registry = projects

    runtimes = ProjectRuntimeManager(
        registry=resolved_registry,
        worktree_base=worktree_base,
        pinned_db_path=db_path,
        state_root=state_root,
    )

    # An explicit --repo seeds/selects a project.  Existing direct callers
    # retain the historical CWD seed by default; the real first-user CLI passes
    # seed_cwd_if_empty=False so a fresh profile stays genuinely empty until
    # the owner selects a repository in UI.
    if repo_path is not None:
        resolved_repo = Path(repo_path).resolve()
        project_registry.add_project(resolved_repo)
        project_registry.select(resolved_repo)
    elif seed_cwd_if_empty and not project_registry.list_projects():
        project_registry.add_project(Path.cwd().resolve())

    def active_runtime() -> ProjectRuntime:
        """Runtime backing what the dashboard currently displays."""
        return runtimes.for_active(project_registry.active())

    def runtime_for_run(run_id: str) -> ProjectRuntime | None:
        """Runtime owning an existing run, resolved from the run itself."""
        return runtimes.for_run(run_id)

    startup_runtime = active_runtime()
    db = startup_runtime.db
    service = startup_runtime.service
    supervisor = startup_runtime.supervisor

    app = FastAPI(title="SaberOps Dashboard")
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
    templates.env.globals["fmt"] = format_timestamp

    # Bounded projection over expensive project Git validation (R0-B §6).
    # Shared by the shell middleware and the dashboard so repeated
    # requests do not re-run `git status` for every known project.
    validation_cache = ProjectValidationCache(project_registry)
    app.state.validation_cache = validation_cache

    @app.middleware("http")
    async def _attach_shell_context(request: Request, call_next: Callable[..., Any]) -> Response:
        """Attach the read-only console-shell projection to every page request.

        The approved shell renders run/project panes on all pages, so the
        data is resolved once per request here instead of in each handler.
        ``/static/*`` and ``/health`` never trigger project Git
        validation: they render no shell and carry no project state.
        A failure degrades to an empty shell rather than failing the page.
        """
        path = request.url.path
        if path == "/health" or path.startswith("/static"):
            request.state.shell = _empty_shell()
            response: Response = await call_next(request)
            return response
        try:
            request.state.shell = _shell_context(
                active_runtime().service,
                project_registry,
                validation_cache,
                runtimes,
            )
        except Exception:
            request.state.shell = _empty_shell()
        response = await call_next(request)
        return response
    app.state.db = db
    app.state.service = service
    app.state.supervisor = supervisor
    app.state.project_registry = project_registry
    app.state.runtimes = runtimes

    @app.get("/", response_class=HTMLResponse, name="dashboard")
    def dashboard(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            _dashboard_context(
                active_runtime().service,
                project_registry,
                validation_cache=validation_cache,
                notice=_notice_from_query(request),
            ),
        )

    @app.get("/setup", response_class=HTMLResponse, name="first_user_setup")
    def first_user_setup(request: Request) -> HTMLResponse:
        """Render the canonical UI-only path from empty profile to first run."""
        return templates.TemplateResponse(
            request,
            "setup.html",
            _setup_context(
                project_registry,
                validation_cache=validation_cache,
                registry=resolved_registry,
            ),
        )

    @app.post("/projects/select", name="select_project")
    def select_project(
        request: Request,
        path: Annotated[str, Form()] = "",
    ) -> Response:
        target_path = path.strip()
        if not target_path:
            return templates.TemplateResponse(
                request,
                "dashboard.html",
                _dashboard_context(
                    active_runtime().service,
                    project_registry,
                    validation_cache=validation_cache,
                    error="Project path is required.",
                ),
                status_code=400,
            )
        ok, note = project_registry.select(target_path)
        if not ok:
            return templates.TemplateResponse(
                request,
                "dashboard.html",
                _dashboard_context(
                    active_runtime().service,
                    project_registry,
                    validation_cache=validation_cache,
                    error=f"Cannot select project: {note}",
                ),
                status_code=400,
            )
        # Selecting a project changes which cached validation is active and
        # reorders recents: drop the cached entry so the next render is live.
        validation_cache.invalidate(target_path)
        return RedirectResponse(url="/?project_selected=1", status_code=303)

    @app.get(
        "/projects/settings/gate-scratch",
        name="get_project_gate_scratch_policy",
    )
    def get_project_gate_scratch_policy_endpoint(
        request: Request,
    ) -> Response:
        """Return the active project's persisted gate-scratch policy."""
        active_repo = project_registry.active()
        if not active_repo:
            return JSONResponse({"error": "no active project"}, status_code=400)
        try:
            payload = OrchestratorService.get_project_gate_scratch_policy(active_repo)
        except GateScratchPolicyError as exc:
            return JSONResponse(
                {"error": f"project gate-scratch policy is invalid: {exc}"},
                status_code=400,
            )
        return JSONResponse(payload)

    @app.post(
        "/projects/settings/gate-scratch",
        name="set_project_gate_scratch_policy",
    )
    async def set_project_gate_scratch_policy_endpoint(
        request: Request,
        mode: Annotated[str, Form()] = "",
    ) -> Response:
        """Persist the active project's gate-scratch policy.

        Changing the policy affects FUTURE runs only; already-created
        runs inherit the value that was frozen at their creation time.
        """
        active_repo = project_registry.active()
        if not active_repo:
            return JSONResponse({"error": "no active project"}, status_code=400)
        cleaned_mode = mode.strip()
        if not cleaned_mode:
            return JSONResponse({"error": "mode is required"}, status_code=400)
        try:
            OrchestratorService.set_project_gate_scratch_policy(active_repo, cleaned_mode)
        except GateScratchPolicyError as exc:
            return JSONResponse(
                {"error": f"project gate-scratch policy is invalid: {exc}"},
                status_code=400,
            )
        return RedirectResponse(url="/?gate_scratch_updated=1", status_code=303)

    @app.get(
        "/projects/settings",
        name="get_project_settings",
    )
    def get_project_settings_endpoint(
        request: Request,
    ) -> Response:
        """Return the active project's effective owner-facing settings."""
        active_repo = project_registry.active()
        if not active_repo:
            return JSONResponse({"error": "no active project"}, status_code=400)
        summary = effective_policy_summary(active_repo)
        if "error" in summary:
            return JSONResponse({"error": str(summary["error"])}, status_code=400)
        return JSONResponse(summary)

    @app.post(
        "/projects/settings/gate",
        name="set_project_gate",
    )
    async def set_project_gate_endpoint(
        request: Request,
        command: Annotated[str, Form()] = "",
    ) -> Response:
        """Persist (or, when empty, clear) the active project's gate command.

        Changing the gate affects FUTURE runs only; already-created
        runs keep the gate command frozen at their creation time.
        """
        from saberops.project_settings import (
            ProjectSettingsError,
            clear_project_gate_command,
            set_project_gate_command,
        )

        active_repo = project_registry.active()
        if not active_repo:
            return JSONResponse({"error": "no active project"}, status_code=400)
        try:
            if command.strip():
                set_project_gate_command(active_repo, command.strip())
            else:
                clear_project_gate_command(active_repo)
        except ProjectSettingsError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return RedirectResponse(url="/?project_settings_updated=1", status_code=303)

    @app.post(
        "/projects/settings/data-policy",
        name="set_project_data_policy",
    )
    async def set_project_data_policy_endpoint(
        request: Request,
        policy: Annotated[str, Form()] = "",
    ) -> Response:
        """Persist the active project's training/data-use policy."""
        from saberops.project_settings import ProjectSettingsError, set_project_data_policy

        active_repo = project_registry.active()
        if not active_repo:
            return JSONResponse({"error": "no active project"}, status_code=400)
        cleaned = policy.strip().lower()
        if cleaned not in ("allowed", "denied"):
            return JSONResponse(
                {"error": "policy must be 'allowed' or 'denied'"},
                status_code=400,
            )
        try:
            set_project_data_policy(active_repo, cleaned)
        except ProjectSettingsError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return RedirectResponse(url="/?project_settings_updated=1", status_code=303)

    @app.post(
        "/projects/settings/review-policy",
        name="set_project_review_policy",
    )
    async def set_project_review_policy_endpoint(
        request: Request,
        policy: Annotated[str, Form()] = "",
    ) -> Response:
        """Persist the active project's review policy via the canonical store."""
        from saberops.models import ReviewPolicyMode as _ReviewPolicyMode
        from saberops.project_settings import (
            ProjectSettingsError,
            set_project_review_policy,
        )
        from saberops.review_adaptive import ReviewPolicyError as _ReviewPolicyError

        active_repo = project_registry.active()
        if not active_repo:
            return JSONResponse({"error": "no active project"}, status_code=400)
        cleaned = policy.strip().lower()
        if cleaned not in ("risk-adaptive", "always-required"):
            return JSONResponse(
                {"error": "policy must be 'risk-adaptive' or 'always-required'"},
                status_code=400,
            )
        mode = (
            _ReviewPolicyMode.ALWAYS_REQUIRED
            if cleaned == "always-required"
            else _ReviewPolicyMode.RISK_ADAPTIVE
        )
        try:
            set_project_review_policy(active_repo, mode)
        except (ProjectSettingsError, _ReviewPolicyError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return RedirectResponse(url="/?project_settings_updated=1", status_code=303)

    @app.post(
        "/projects/settings/target-branch",
        name="set_project_target_branch",
    )
    async def set_project_target_branch_endpoint(
        request: Request,
        branch: Annotated[str, Form()] = "",
    ) -> Response:
        """Persist (or, when empty, clear) the accept target branch."""
        from saberops.project_settings import (
            ProjectSettingsError,
            clear_project_accept_target_branch,
            set_project_accept_target_branch,
        )

        active_repo = project_registry.active()
        if not active_repo:
            return JSONResponse({"error": "no active project"}, status_code=400)
        try:
            if branch.strip():
                set_project_accept_target_branch(active_repo, branch.strip())
            else:
                clear_project_accept_target_branch(active_repo)
        except ProjectSettingsError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return RedirectResponse(url="/?project_settings_updated=1", status_code=303)

    @app.post("/runs", name="start_run")
    async def start_run(
        request: Request,
        task: Annotated[str, Form()] = "",
        tier: Annotated[str, Form()] = "",
        routing_mode: Annotated[str, Form()] = "auto",
        manual_tier: Annotated[str, Form()] = "",
        max_auto_tier: Annotated[str, Form()] = "T3",
        training_allowed: Annotated[str, Form()] = "",
        provider_override: Annotated[str, Form()] = "",
        model_override: Annotated[str, Form()] = "",
        gate_command: Annotated[str, Form()] = "",
        worker_timeout: Annotated[str, Form()] = "",
        gate_timeout: Annotated[str, Form()] = "",
        review_timeout: Annotated[str, Form()] = "",
        review_enabled: Annotated[str, Form()] = "",
        review_mode: Annotated[str, Form()] = "bounded",
        review_limit: Annotated[str, Form()] = "3",
    ) -> Response:
        form = _default_start_form()
        form["task"] = task
        form["routing_mode"] = routing_mode
        form["manual_tier"] = manual_tier or tier
        form["max_auto_tier"] = max_auto_tier
        form["training_allowed"] = training_allowed == "1"
        form["provider_override"] = provider_override
        form["model_override"] = model_override
        # R2-A: an empty gate box means "follow the project gate setting"
        # (None).  The shared run-creation authority fails creation with
        # an actionable message when the project has neither an explicit
        # gate nor a positively detected one -- Web never silently falls
        # back to a default gate command.
        form["gate_command"] = gate_command.strip()
        form["worker_timeout"] = worker_timeout
        form["gate_timeout"] = gate_timeout
        form["review_timeout"] = review_timeout
        form["review_enabled"] = review_enabled == "1"
        form["review_mode"] = review_mode.strip() or "bounded"
        form["review_limit"] = review_limit.strip() or "3"

        active_repo = project_registry.active()
        if active_repo is None:
            return templates.TemplateResponse(
                request,
                "dashboard.html",
                _dashboard_context(
                    active_runtime().service,
                    project_registry,
                    validation_cache=validation_cache,
                    error="Select a project in the UI before starting a run.",
                    form=form,
                ),
                status_code=400,
            )

        if not task.strip():
            return templates.TemplateResponse(
                request,
                "dashboard.html",
                _dashboard_context(
                    active_runtime().service,
                    project_registry,
                    validation_cache=validation_cache,
                    error="Task is required.",
                    form=form,
                ),
                status_code=400,
            )
        if (
            routing_mode not in ("auto", "manual")
            or (tier and tier not in _TIERS)
            or (manual_tier and manual_tier not in _TIERS)
            or max_auto_tier not in _TIERS
        ):
            return templates.TemplateResponse(
                request,
                "dashboard.html",
                _dashboard_context(
                    active_runtime().service,
                    project_registry,
                    validation_cache=validation_cache,
                    error="Select a valid tier (T1, T2, or T3).",
                    form=form,
                ),
                status_code=400,
            )

        if form["review_mode"] not in ("bounded", "max"):
            return templates.TemplateResponse(
                request,
                "dashboard.html",
                _dashboard_context(
                    active_runtime().service,
                    project_registry,
                    validation_cache=validation_cache,
                    error="Select a valid review mode (bounded or max).",
                    form=form,
                ),
                status_code=400,
            )

        def _optional_int(raw: str) -> int | None:
            value = raw.strip()
            if not value:
                return None
            try:
                return int(value)
            except ValueError:
                return None

        parsed_review_limit = _optional_int(review_limit)
        if parsed_review_limit is None or parsed_review_limit < 1:
            return templates.TemplateResponse(
                request,
                "dashboard.html",
                _dashboard_context(
                    active_runtime().service,
                    project_registry,
                    validation_cache=validation_cache,
                    error="Review limit must be a whole number of at least 1.",
                    form=form,
                ),
                status_code=400,
            )

        try:

            def optional_float(raw: str) -> float | None:
                return float(raw) if raw.strip() else None

            # R2-A: Web resolves run policy exclusively through the shared
            # run-creation authority -- the same precedence the CLI uses.
            # The effective gate, gate storage, review policy,
            # data/training policy, and accept target branch freeze onto
            # the RunConfig here; later project-settings changes affect
            # future runs only.
            config = resolve_effective_run_config(
                RunRequest(
                    task=task,
                    repo_path=active_repo,
                    gate_command=form["gate_command"] or None,
                    training_allowed=form["training_allowed"],
                    review_policy=None,
                    routing_mode="manual" if tier else routing_mode,
                    manual_tier=manual_tier or tier or None,
                    max_auto_tier=max_auto_tier,
                    provider_override=provider_override,
                    model_override=model_override,
                    worker_timeout=optional_float(worker_timeout),
                    gate_timeout=optional_float(gate_timeout),
                    review_enabled=form["review_enabled"],
                    review_mode=form["review_mode"],
                    review_limit=parsed_review_limit,
                    review_timeout=optional_float(review_timeout),
                )
            )
            result = runtimes.for_repo(config.target_repo).supervisor.start_supervised_run(
                config=config
            )
        except RunCreationError as exc:
            return templates.TemplateResponse(
                request,
                "dashboard.html",
                _dashboard_context(
                    active_runtime().service,
                    project_registry,
                    validation_cache=validation_cache,
                    error=str(exc),
                    form=form,
                ),
                status_code=400,
            )
        except Exception as exc:
            return templates.TemplateResponse(
                request,
                "dashboard.html",
                _dashboard_context(
                    active_runtime().service,
                    project_registry,
                    validation_cache=validation_cache,
                    error=f"Run failed to start: {exc}",
                    form=form,
                ),
                status_code=400,
            )

        return RedirectResponse(url=f"/runs/{result.run_id}", status_code=303)

    @app.get("/runs/{run_id}", response_class=HTMLResponse, name="run_detail")
    def run_detail(request: Request, run_id: str) -> HTMLResponse:
        runtime = runtime_for_run(run_id)
        run = runtime.db.get_run(run_id) if runtime is not None else None
        if runtime is None or run is None:
            return templates.TemplateResponse(
                request, "run_detail.html", _missing_run_context(run_id), status_code=404
            )
        return templates.TemplateResponse(
            request,
            "run_detail.html",
            _run_detail_context(runtime.db, run, notice=_notice_from_query(request)),
        )

    @app.get("/runs/{run_id}/plan")
    def run_plan(run_id: str) -> JSONResponse:
        runtime = runtime_for_run(run_id)
        if runtime is None or runtime.db.get_run(run_id) is None:
            return JSONResponse({"error": "run not found"}, status_code=404)
        return JSONResponse(_plan_projection(runtime.db, run_id))

    @app.get("/runs/{run_id}/diff")
    def run_diff(run_id: str) -> JSONResponse:
        """Read-only bounded candidate diff as JSON.

        Same projection the Changes tab renders: frozen
        ``base_commit .. R3-A current candidate SHA`` from the run's
        persisted repository.  Unknown run is a real not-found
        result; a known run with no trustworthy current candidate
        returns a truthful unavailable projection.
        """
        from saberops.candidate_diff import project_candidate_diff

        runtime = runtime_for_run(run_id)
        if runtime is None or runtime.db.get_run(run_id) is None:
            return JSONResponse({"error": "run not found"}, status_code=404)
        return JSONResponse(project_candidate_diff(runtime.db, run_id).as_dict())

    @app.get("/runs/{run_id}/report")
    def run_report_json(run_id: str) -> JSONResponse:
        """Read-only canonical C16-B1 run report as JSON.

        Same projection as ``orch report RUN_ID --json``; the payload
        is deterministic for a given persisted state.
        """
        runtime = runtime_for_run(run_id)
        if runtime is None or runtime.db.get_run(run_id) is None:
            return JSONResponse({"error": "run not found"}, status_code=404)
        try:
            return JSONResponse(build_run_report(runtime.db, run_id))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)

    @app.get("/runs/{run_id}/actions")
    def run_actions(run_id: str) -> JSONResponse:
        """Authoritative run-action state for live button refresh.

        The run page fetches this after lifecycle SSE events so Accept /
        Review / Cancel / Cleanup controls refresh from server state
        instead of remaining stale from the initial page load.  The
        acceptance portion is the same engine-derived projection the
        page renders and the POST gates on.
        """
        runtime = runtime_for_run(run_id)
        run = runtime.db.get_run(run_id) if runtime is not None else None
        if runtime is None or run is None:
            return JSONResponse({"error": "run not found"}, status_code=404)
        eligibility = AcceptEngine(db=runtime.db).check_accept_eligibility(run_id)
        from saberops.candidate_lifecycle import can_reject_candidate, project_candidate_state

        try:
            _projection = project_candidate_state(runtime.db, run_id)
            _candidate_state: str | None = (
                _projection.state.value if _projection.state is not None else None
            )
        except Exception:
            _candidate_state = None
        try:
            _can_reject = can_reject_candidate(runtime.db, run_id)
        except Exception:
            _can_reject = False
        try:
            _owner_summary = summarize_owner_actions(runtime.db, run_id)
            _owner_review_active = _owner_summary.active_review is not None
            _owner_accept_active = _owner_summary.active_accept is not None
            _owner_review_state = (
                _owner_summary.active_review.state.value
                if _owner_summary.active_review is not None
                else None
            )
            _owner_accept_state = (
                _owner_summary.active_accept.state.value
                if _owner_summary.active_accept is not None
                else None
            )
            # Projected terminal history (including a provably dead active
            # action read-only-projected as UNCERTAIN / OWNER_PROCESS_DEAD).
            _owner_review_last_state = (
                _owner_summary.last_review.state.value
                if _owner_summary.last_review is not None
                else None
            )
            _owner_review_last_reason = (
                _owner_summary.last_review.reason
                if _owner_summary.last_review is not None
                else None
            )
            _owner_accept_last_state = (
                _owner_summary.last_accept.state.value
                if _owner_summary.last_accept is not None
                else None
            )
            _owner_accept_last_reason = (
                _owner_summary.last_accept.reason
                if _owner_summary.last_accept is not None
                else None
            )
        except Exception:
            _owner_review_active = False
            _owner_accept_active = False
            _owner_review_state = None
            _owner_accept_state = None
            _owner_review_last_state = None
            _owner_review_last_reason = None
            _owner_accept_last_state = None
            _owner_accept_last_reason = None
        return JSONResponse(
            {
                "run_id": run_id,
                "status": run.status.value,
                "can_accept": eligibility.eligible,
                "can_review": run.status == RunStatus.COMPLETED
                and _candidate_state != "REJECTED",
                "can_cleanup": run.status in (RunStatus.COMPLETED, RunStatus.FAILED),
                "can_retry": run.status in (RunStatus.COMPLETED, RunStatus.FAILED),
                "can_cancel": run.status not in TERMINAL_RUN_STATUSES,
                "candidate_state": _candidate_state,
                "can_reject": _can_reject,
                "accept_checklist": eligibility.as_dict()["checks"],
                "owner_review_active": _owner_review_active,
                "owner_review_state": _owner_review_state,
                "owner_accept_active": _owner_accept_active,
                "owner_accept_state": _owner_accept_state,
                "owner_review_last_state": _owner_review_last_state,
                "owner_review_last_reason": _owner_review_last_reason,
                "owner_accept_last_state": _owner_accept_last_state,
                "owner_accept_last_reason": _owner_accept_last_reason,
            }
        )

    @app.get("/runs/{run_id}/events/stream")
    async def stream_events(request: Request, run_id: str, after: int = 0) -> Response:
        runtime = runtime_for_run(run_id)
        db = runtime.db if runtime is not None else startup_runtime.db
        last_event_id_header = request.headers.get("last-event-id")
        after_id = after
        if after_id == 0 and last_event_id_header:
            try:
                after_id = int(last_event_id_header)
            except ValueError:
                pass

        persisted = db.get_events(run_id, after_id=after_id, limit=200)
        persisted_run = db.get_run(run_id)
        terminal_persisted = (
            persisted_run is not None
            and persisted_run.status in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.ORPHANED)
        ) or any(event.event_type in TERMINAL_EVENT_TYPES for event in persisted)
        # R3-E2.1: a terminal run with an active post-run owner action
        # (Review / Accept) keeps a usable live stream so queued/started/
        # completed/failed/uncertain events reach the run page.  The
        # stream may close once the run is terminal AND no owner action
        # remains active.
        if terminal_persisted and not _owner_actions_stream_active(db, run_id):
            payload = "".join(
                f"id: {event.id}\nevent: {event.event_type}\ndata: {
                    json.dumps(
                        {
                            'id': event.id,
                            'run_id': event.run_id,
                            'attempt_id': event.attempt_id,
                            'event_type': event.event_type,
                            'payload': event.payload,
                            'created_at': event.created_at,
                        }
                    )
                }\n\n"
                for event in persisted
            )
            return Response(
                content=payload,
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        async def event_generator() -> AsyncGenerator[str, None]:
            nonlocal after_id
            # A terminal persisted run never needs a live listener.  This is
            # also important for reconnects after a process restart.
            initial_events = db.get_events(run_id, after_id=after_id, limit=200)
            initial_run = db.get_run(run_id)
            terminal_initial = (
                initial_run is not None
                and initial_run.status
                in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.ORPHANED)
            ) or any(event.event_type in TERMINAL_EVENT_TYPES for event in initial_events)
            if terminal_initial and not _owner_actions_stream_active(db, run_id):
                for ev in initial_events:
                    after_id = max(after_id, ev.id)
                    data = {
                        "id": ev.id,
                        "run_id": ev.run_id,
                        "attempt_id": ev.attempt_id,
                        "event_type": ev.event_type,
                        "payload": ev.payload,
                        "created_at": ev.created_at,
                    }
                    yield f"id: {ev.id}\nevent: {ev.event_type}\ndata: {json.dumps(data)}\n\n"
                return
            q: asyncio.Queue[tuple[str, int]] = asyncio.Queue()
            loop = asyncio.get_running_loop()

            def on_event(event_run_id: str, event_id: int) -> None:
                if event_run_id == run_id:
                    loop.call_soon_threadsafe(q.put_nowait, (event_run_id, event_id))

            db.add_event_listener(on_event)
            # Events are written by the detached execution owner in its own
            # process, where the in-process listener above cannot see them.
            wake = subscribe_run(db.db_path, run_id).open()
            stop_bridge = threading.Event()

            def _bridge() -> None:
                while not stop_bridge.is_set():
                    if wake.wait(0.5):
                        loop.call_soon_threadsafe(q.put_nowait, (run_id, 0))

            bridge = threading.Thread(target=_bridge, daemon=True, name=f"wake-bridge-{run_id}")
            bridge.start()

            try:
                while True:
                    if await request.is_disconnected():
                        break

                    events = db.get_events(run_id, after_id=after_id, limit=200)
                    for ev in events:
                        after_id = max(after_id, ev.id)
                        data = {
                            "id": ev.id,
                            "run_id": ev.run_id,
                            "attempt_id": ev.attempt_id,
                            "event_type": ev.event_type,
                            "payload": ev.payload,
                            "created_at": ev.created_at,
                        }
                        payload_json = json.dumps(data)
                        yield f"id: {ev.id}\nevent: {ev.event_type}\ndata: {payload_json}\n\n"

                    run = db.get_run(run_id)
                    is_terminal = (
                        run is not None
                        and run.status
                        in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.ORPHANED)
                    ) or any(ev.event_type in TERMINAL_EVENT_TYPES for ev in db.get_events(run_id))
                    if is_terminal and not _owner_actions_stream_active(db, run_id):
                        # Terminal events are durable before this check.  End immediately
                        # after a final fetch so TestClient and browsers do not retain a
                        # completed connection solely for a grace-period timer.
                        leftover = db.get_events(run_id, after_id=after_id, limit=200)
                        for ev in leftover:
                            after_id = max(after_id, ev.id)
                            data = {
                                "id": ev.id,
                                "run_id": ev.run_id,
                                "attempt_id": ev.attempt_id,
                                "event_type": ev.event_type,
                                "payload": ev.payload,
                                "created_at": ev.created_at,
                            }
                            payload_json = json.dumps(data)
                            yield f"id: {ev.id}\nevent: {ev.event_type}\ndata: {payload_json}\n\n"
                        break
                    try:
                        await asyncio.wait_for(q.get(), timeout=1.0)
                    except TimeoutError:
                        pass
            finally:
                stop_bridge.set()
                db.remove_event_listener(on_event)
                wake.close()

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.websocket("/runs/{run_id}/terminal/ws")
    async def terminal_ws(websocket: WebSocket, run_id: str) -> None:
        """WebSocket for PTY byte stream: replay transcript, then live bytes.

        Controlled PTY only: argv list, never shell. No input channel.
        Unknown run_id fails closed (4404). Reconnect replays transcript
        and never starts a second process (supervisor single-owner guarantee).
        """
        runtime = runtime_for_run(run_id)
        run = runtime.db.get_run(run_id) if runtime is not None else None
        if runtime is None or run is None:
            await websocket.close(code=4404)
            return
        supervisor = runtime.supervisor
        await websocket.accept()
        # ATTACH-ONLY: never launch a process via WS. The WS attaches to an
        # existing supervised PTY stream and/or replays the persisted transcript.
        # Process launching happens ONLY via the explicit start-run action path.
        transcript_path = supervisor.get_transcript_path(run_id)
        offset = 0
        # Replay existing transcript first (ANSI bytes, including colors)
        try:
            if transcript_path.exists():
                data = transcript_path.read_bytes()
                if data:
                    chunk_size = 8192
                    for i in range(0, len(data), chunk_size):
                        chunk = data[i : i + chunk_size]
                        try:
                            await websocket.send_bytes(chunk)
                        except Exception:
                            return
                    offset = len(data)
        except OSError:
            offset = 0
        # If no live process exists, inform the client with a terminal-status notice
        # after replay. The client still receives the full transcript identity.
        try:
            # A detached owner from a previous server lifetime is still live
            # execution even though this process holds no handle for it.
            has_process = supervisor.is_execution_live(run_id)
        except Exception:
            has_process = False
        if not has_process:
            try:
                status_payload = {
                    "type": "terminal-status",
                    "run_id": run_id,
                    "status": run.status.value,
                    "has_process": False,
                }
                await websocket.send_text(json.dumps(status_payload))
            except Exception:
                pass
        # Live streaming: poll file growth and forward raw PTY bytes
        try:
            while True:
                await asyncio.sleep(0.05)
                try:
                    cur_bytes = transcript_path.read_bytes() if transcript_path.exists() else b""
                except OSError:
                    cur_bytes = b""
                if len(cur_bytes) > offset:
                    new_chunk = cur_bytes[offset:]
                    offset = len(cur_bytes)
                    chunk_size = 8192
                    for i in range(0, len(new_chunk), chunk_size):
                        part = new_chunk[i : i + chunk_size]
                        try:
                            await websocket.send_bytes(part)
                        except Exception:
                            return
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            try:
                await websocket.close()
            except Exception:
                pass

    @app.post("/runs/{run_id}/retry", name="retry_run")
    def retry_run(request: Request, run_id: str) -> Response:
        runtime = runtime_for_run(run_id)
        run = runtime.db.get_run(run_id) if runtime is not None else None
        if runtime is None or run is None:
            return templates.TemplateResponse(
                request, "run_detail.html", _missing_run_context(run_id), status_code=404
            )
        try:
            # The retry target is the repository this run was created for,
            # validated against its persisted project identity.  The UI's
            # active-project selection is deliberately not consulted: changing
            # it must never retarget an existing run.
            target = assert_run_repository(run)
            if run.config_json:
                persisted = json.loads(run.config_json)
                config = replace(reconstruct_run_config(persisted), target_repo=str(target))
            else:
                # Reconstruct minimal config for legacy rows
                config = resolve_run_config(
                    task=run.task,
                    target_repo=target,
                    routing_mode="manual",
                    manual_tier=run.tier,
                    training_allowed=run.training_allowed,
                    gate_command=run.gate_command,
                )
            result = runtimes.for_repo(target).supervisor.start_supervised_run(config=config)
        except Exception as exc:
            return templates.TemplateResponse(
                request,
                "run_detail.html",
                _run_detail_context(runtime.db, run, error=f"Retry failed to start: {exc}"),
                status_code=400,
            )
        return RedirectResponse(url=f"/runs/{result.run_id}", status_code=303)

    @app.post("/runs/{run_id}/cancel", name="cancel_run")
    def cancel_run(request: Request, run_id: str) -> Response:
        """Explicitly cancel a run. Browser/UI disconnect never reaches here."""
        runtime = runtime_for_run(run_id)
        run = runtime.db.get_run(run_id) if runtime is not None else None
        if runtime is None or run is None:
            return HTMLResponse(
                templates.get_template("run_detail.html").render(
                    request=request, **_missing_run_context(run_id)
                ),
                status_code=404,
            )
        cancelled = runtime.supervisor.cancel(run_id)
        notice = "cancelled" if cancelled else "cancel_not_applicable"
        return RedirectResponse(url=f"/runs/{run_id}?notice={notice}", status_code=303)

    @app.post("/runs/{run_id}/review", name="request_review")
    def request_review(request: Request, run_id: str) -> Response:
        runtime = runtime_for_run(run_id)
        run = runtime.db.get_run(run_id) if runtime is not None else None
        if runtime is None or run is None:
            return templates.TemplateResponse(
                request, "run_detail.html", _missing_run_context(run_id), status_code=404
            )
        # R3-E2: Request Review claims a durable owner action and detaches
        # the Review child immediately.  Nothing long-running executes in
        # this request: closing the browser can never stop the child, and
        # a duplicate click collapses onto the same active action.
        from saberops.candidate_lifecycle import project_candidate_state

        try:
            candidate_sha = project_candidate_state(runtime.db, run.id).candidate_sha
        except Exception:
            candidate_sha = None
        if not candidate_sha:
            return templates.TemplateResponse(
                request,
                "run_detail.html",
                _run_detail_context(
                    runtime.db, run, error="Review is unavailable: no reviewable candidate."
                ),
                status_code=400,
            )
        try:
            launch = OwnerActionSupervisor(runtime.db).request_review(
                run_id=run.id,
                candidate_sha=candidate_sha,
                target_repo=run.target_repo,
            )
        except Exception as exc:
            return templates.TemplateResponse(
                request,
                "run_detail.html",
                _run_detail_context(runtime.db, run, error=f"Review launch failed: {exc}"),
                status_code=400,
            )
        if launch.duplicate:
            return RedirectResponse(
                url=f"/runs/{run_id}?action_active=review",
                status_code=303,
            )
        if not launch.launched:
            reason = launch.action.reason or LAUNCH_FAILED_REASON
            return templates.TemplateResponse(
                request,
                "run_detail.html",
                _run_detail_context(
                    runtime.db, run, error=f"Review launch failed: {reason}"
                ),
                status_code=400,
            )
        return RedirectResponse(
            url=f"/runs/{run_id}?review_queued=1",
            status_code=303,
        )

    @app.post("/runs/{run_id}/cleanup", name="request_cleanup")
    def request_cleanup(
        request: Request,
        run_id: str,
        confirm: Annotated[str, Form()] = "",
    ) -> Response:
        runtime = runtime_for_run(run_id)
        run = runtime.db.get_run(run_id) if runtime is not None else None
        if runtime is None or run is None:
            return templates.TemplateResponse(
                request, "run_detail.html", _missing_run_context(run_id), status_code=404
            )
        if confirm != "1":
            return templates.TemplateResponse(
                request,
                "run_detail.html",
                _run_detail_context(runtime.db, run, confirming_cleanup=True),
                status_code=200,
            )
        try:
            runtime.service.cleanup_run(run_id)
        except Exception as exc:
            return templates.TemplateResponse(
                request,
                "run_detail.html",
                _run_detail_context(runtime.db, run, error=f"Cleanup failed: {exc}"),
                status_code=400,
            )
        return RedirectResponse(url=f"/runs/{run_id}?cleaned=1", status_code=303)

    @app.post("/runs/{run_id}/accept", name="request_accept")
    def request_accept(
        request: Request,
        run_id: str,
        confirm_run_id: Annotated[str, Form()] = "",
    ) -> Response:
        runtime = runtime_for_run(run_id)
        run = runtime.db.get_run(run_id) if runtime is not None else None
        if runtime is None or run is None:
            return templates.TemplateResponse(
                request, "run_detail.html", _missing_run_context(run_id), status_code=404
            )
        db = runtime.db
        # Gate on the same engine-derived eligibility the page renders.
        # The POST never recreates acceptance policy; the engine remains
        # the authority when accept_run executes.
        eligibility = AcceptEngine(db=db).check_accept_eligibility(run.id)
        if not eligibility.eligible:
            blocker = next(
                (check for check in eligibility.checks if not check.passed()),
                None,
            )
            detail = (
                f" {blocker.label}: {blocker.reason}" if blocker is not None else ""
            )
            return templates.TemplateResponse(
                request,
                "run_detail.html",
                _run_detail_context(
                    db,
                    run,
                    error=f"Accept is blocked by the acceptance checklist.{detail}",
                ),
                status_code=400,
            )
        if confirm_run_id == "":
            return templates.TemplateResponse(
                request,
                "run_detail.html",
                _run_detail_context(db, run, confirming_accept=True),
                status_code=200,
            )
        if confirm_run_id != run_id:
            return templates.TemplateResponse(
                request,
                "run_detail.html",
                _run_detail_context(
                    db,
                    run,
                    confirming_accept=True,
                    error="Type the exact run id to confirm acceptance.",
                ),
                status_code=400,
            )
        # R3-E2: confirmed Accept claims a durable owner action and detaches
        # the Accept child immediately.  The checklist above stays advisory:
        # the detached child (AcceptEngine) re-checks every canonical
        # condition immediately before mutation.  The redirect returns at
        # once; browser close can never stop the child, and duplicate
        # confirms collapse onto the same active action.
        from saberops.candidate_lifecycle import project_candidate_state as _accept_candidate_state

        try:
            accept_candidate = _accept_candidate_state(db, run.id).candidate_sha
        except Exception:
            accept_candidate = None
        if not accept_candidate:
            return templates.TemplateResponse(
                request,
                "run_detail.html",
                _run_detail_context(
                    db, run, error="Accept is unavailable: no trustworthy candidate."
                ),
                status_code=400,
            )
        try:
            accept_launch = OwnerActionSupervisor(db).request_accept(
                run_id=run.id,
                candidate_sha=accept_candidate,
                target_repo=run.target_repo,
            )
        except Exception as exc:
            return templates.TemplateResponse(
                request,
                "run_detail.html",
                _run_detail_context(db, run, error=f"Accept launch failed: {exc}"),
                status_code=400,
            )
        if accept_launch.duplicate:
            return RedirectResponse(
                url=f"/runs/{run_id}?action_active=accept",
                status_code=303,
            )
        if not accept_launch.launched:
            reason = accept_launch.action.reason or LAUNCH_FAILED_REASON
            return templates.TemplateResponse(
                request,
                "run_detail.html",
                _run_detail_context(db, run, error=f"Accept launch failed: {reason}"),
                status_code=400,
            )
        return RedirectResponse(
            url=f"/runs/{run_id}?accept_queued=1",
            status_code=303,
        )

    @app.post("/runs/{run_id}/reject", name="request_reject")
    def request_reject(
        request: Request,
        run_id: str,
        confirm: Annotated[str, Form()] = "",
    ) -> Response:
        """Two-step owner Reject: confirmation render, then durable decision.

        Reject records a durable owner decision for the exact current
        candidate; it never mutates the target repository, deletes the
        candidate, or cleans worktrees.  The POST delegates to the
        service seam -- the Web never writes ``candidate_rejected``
        events directly -- and gates the confirmation step on the same
        ``can_reject`` projection the page renders.
        """
        runtime = runtime_for_run(run_id)
        run = runtime.db.get_run(run_id) if runtime is not None else None
        if runtime is None or run is None:
            return templates.TemplateResponse(
                request, "run_detail.html", _missing_run_context(run_id), status_code=404
            )
        db = runtime.db
        service = runtime.service
        if confirm != "1":
            if not service.can_reject_candidate(run.id):
                return templates.TemplateResponse(
                    request,
                    "run_detail.html",
                    _run_detail_context(
                        db, run, error="Reject is not available for this candidate."
                    ),
                    status_code=400,
                )
            return templates.TemplateResponse(
                request,
                "run_detail.html",
                _run_detail_context(db, run, confirming_reject=True),
                status_code=200,
            )
        try:
            service.reject_candidate(run_id)
        except Exception as exc:
            return templates.TemplateResponse(
                request,
                "run_detail.html",
                _run_detail_context(db, run, error=f"Reject failed: {exc}"),
                status_code=400,
            )
        return RedirectResponse(url=f"/runs/{run_id}?rejected=1", status_code=303)

    @app.get("/inbox", response_class=HTMLResponse, name="inbox")
    def inbox_page(request: Request) -> HTMLResponse:
        """Render the cross-project owner Inbox (read-only projection)."""
        from saberops.inbox import InboxSnapshot, build_inbox

        try:
            snapshot = build_inbox(runtimes)
        except Exception:
            snapshot = InboxSnapshot(items=())
        return templates.TemplateResponse(
            request, "inbox.html", _inbox_context(snapshot)
        )

    @app.get("/inbox.json", name="inbox_json")
    def inbox_json() -> JSONResponse:
        """Return the same Inbox projection as JSON (read-only)."""
        from saberops.inbox import InboxSnapshot, build_inbox

        try:
            snapshot = build_inbox(runtimes)
        except Exception:
            snapshot = InboxSnapshot(items=())
        return JSONResponse(snapshot.as_dict())

    @app.get("/quota", response_class=HTMLResponse, name="quota")
    def quota(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "quota.html",
            _quota_context(runtimes.owner_db, notice=_notice_from_query(request)),
        )

    @app.post("/quota/set", name="set_quota")
    def quota_set(
        request: Request,
        provider: Annotated[str, Form()] = "",
        pool: Annotated[str, Form()] = "",
        remaining_percent: Annotated[str, Form()] = "",
        reset_at: Annotated[str, Form()] = "",
    ) -> Response:
        # Provider quota is owner-wide configuration, not project state.
        db = runtimes.owner_db
        service = startup_runtime.service
        form = {
            "provider": provider,
            "pool": pool,
            "remaining_percent": remaining_percent,
            "reset_at": reset_at,
        }
        if not provider.strip() or not pool.strip():
            return templates.TemplateResponse(
                request,
                "quota.html",
                _quota_context(
                    db,
                    error="Provider and pool are required.",
                    form=form,
                ),
                status_code=400,
            )
        try:
            remaining = float(remaining_percent)
        except ValueError:
            return templates.TemplateResponse(
                request,
                "quota.html",
                _quota_context(
                    db,
                    error="Remaining percentage must be a number.",
                    form=form,
                ),
                status_code=400,
            )
        reset_value = reset_at.strip() or None
        try:
            service.set_quota(
                provider=provider,
                pool=pool,
                remaining_percent=remaining,
                reset_at=reset_value,
            )
        except ValueError as exc:
            return templates.TemplateResponse(
                request,
                "quota.html",
                _quota_context(db, error=f"Quota update failed: {exc}", form=form),
                status_code=400,
            )
        return RedirectResponse(url="/quota?updated=1", status_code=303)

    @app.post("/quota/enable", name="enable_quota")
    def quota_enable(
        request: Request,
        provider: Annotated[str, Form()] = "",
        pool: Annotated[str, Form()] = "",
    ) -> Response:
        db = runtimes.owner_db
        service = startup_runtime.service
        enable_form = {"provider": provider, "pool": pool}
        if not provider.strip() or not pool.strip():
            return templates.TemplateResponse(
                request,
                "quota.html",
                _quota_context(
                    db,
                    error="Provider and pool are required.",
                    enable_form=enable_form,
                ),
                status_code=400,
            )
        service.enable_quota(provider=provider, pool=pool)
        return RedirectResponse(url="/quota?enabled=1", status_code=303)

    @app.get("/access", response_class=HTMLResponse, name="model_access")
    def model_access(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "access.html",
            _access_context(notice=_notice_from_query(request)),
        )

    @app.post("/access/test", name="test_access_connection")
    def access_test(
        request: Request,
        connection_id: Annotated[str, Form()] = "",
    ) -> Response:
        """Verify one connection and render the classified result.

        Account-backed connections perform the canonical readiness
        refresh through :class:`ReadinessService` over the exact
        already-loaded :class:`ProviderConnection`, persisted through
        the canonical :class:`ReadinessStore`: the rendered
        state/reason/observation timestamp is the persisted truth that
        subsequent automatic routing consumes.  Only the declared
        non-inference status command runs; no model call occurs and
        ``UNKNOWN`` stays ``UNKNOWN`` (a present CLI is availability,
        never proven readiness).  API connections keep the safe
        unauthenticated reachability probe -- there is no
        truth-preserving readiness authority for them, so they are
        never classified as ``BACKEND_NOT_INSTALLED``: the stored
        credential value is never read or transmitted.
        """
        wanted = connection_id.strip()
        if not wanted:
            return templates.TemplateResponse(
                request,
                "access.html",
                _access_context(error="A connection id is required."),
                status_code=400,
            )
        try:
            registry = _access_store().load()
        except AccessStoreError as exc:
            return templates.TemplateResponse(
                request,
                "access.html",
                _access_context(error=f"Access store unreadable: {exc}"),
                status_code=500,
            )
        connection = registry.try_get(wanted)
        if connection is None:
            return templates.TemplateResponse(
                request,
                "access.html",
                _access_context(error=f"Unknown connection: {wanted}"),
                status_code=404,
            )
        if connection.is_account_backed:
            # One authoritative verification path: the canonical
            # readiness refresh (non-inference status command only)
            # over the exact registry-loaded connection, composed with
            # the existing store seam so XDG/test overrides and
            # connection identity cannot diverge.  The executable
            # lookup and status runner are resolved at call time so
            # the same production functions are used without
            # early-bound defaults.
            try:
                service = ReadinessService(
                    registry,
                    _readiness_store(),
                    which=shutil.which,
                    run=subprocess.run,
                )
                evidence = service.refresh(connection)
            except ReadinessStoreError as exc:
                return templates.TemplateResponse(
                    request,
                    "access.html",
                    _access_context(error=f"Readiness store unwritable: {exc}"),
                    status_code=500,
                )
            probe = {
                "connection_id": connection.connection_id,
                "ok": (
                    "TRUE"
                    if evidence.state is ProviderReadiness.READY
                    else "FALSE"
                ),
                "reason": evidence.reason,
                "detail": (
                    "verified readiness persisted; "
                    f"state={evidence.state.value} "
                    f"observed_at={evidence.observed_at}"
                ),
                "readiness_state": evidence.state.value,
                "readiness_reason": evidence.reason,
                "observed_at": evidence.observed_at,
            }
        else:
            outcome = _probe_api_connection(
                endpoint=connection.endpoint,
            )
            probe = {"connection_id": connection.connection_id, **outcome}
        return templates.TemplateResponse(request, "access.html", _access_context(probe=probe))

    @app.post("/access/discover", name="discover_access_models")
    def access_discover(
        request: Request,
        connection_id: Annotated[str, Form()] = "",
    ) -> Response:
        """Discover the models one connection's provider/backend enumerates.

        Discovery is evidence only.  It writes the non-secret catalog
        store and can never rewrite or erase durable connection
        configuration; a failed discovery keeps prior evidence and
        reports a classified reason.
        """
        wanted = connection_id.strip()
        if not wanted:
            return templates.TemplateResponse(
                request,
                "access.html",
                _access_context(error="A connection id is required."),
                status_code=400,
            )
        try:
            access = _access_store().load()
        except AccessStoreError as exc:
            return templates.TemplateResponse(
                request,
                "access.html",
                _access_context(error=f"Access store unreadable: {exc}"),
                status_code=500,
            )
        connection = access.try_get(wanted)
        if connection is None:
            return templates.TemplateResponse(
                request,
                "access.html",
                _access_context(error=f"Unknown connection: {wanted}"),
                status_code=404,
            )
        result = _discover_connection(connection, resolved_registry)
        store = _model_catalog_store()
        try:
            catalog = store.load()
        except ModelCatalogStoreError:
            catalog = ModelCatalog()
        # Successful enumeration replaces this connection's evidence (a
        # truthful zero-model result clears models the provider proved
        # absent); a failed/UNKNOWN refresh retains last-known evidence but
        # marks it stale so it can never masquerade as fresh.
        catalog = apply_discovery_result(catalog, result)
        try:
            store.save(catalog)
        except ModelCatalogStoreError as exc:
            return templates.TemplateResponse(
                request,
                "access.html",
                _access_context(error=f"Model catalog not saved: {exc}"),
                status_code=500,
            )
        return templates.TemplateResponse(
            request,
            "access.html",
            _access_context(discovery=[result.to_dict()]),
        )

    @app.post("/access/connections", name="add_access_connection")
    def access_add(
        request: Request,
        connection_id: Annotated[str, Form()] = "",
        preset: Annotated[str, Form()] = "",
        protocol: Annotated[str, Form()] = "",
        endpoint: Annotated[str, Form()] = "",
        credential_ref: Annotated[str, Form()] = "",
        auth_header_name: Annotated[str, Form()] = "",
        model: Annotated[str, Form()] = "",
        public_headers: Annotated[str, Form()] = "",
    ) -> Response:
        """Save a user-owned API connection (references only, no secrets).

        The form carries a credential *reference* (``env:`` /
        ``profile:`` / ``store:`` handle) plus, optionally, the *name*
        of the credential-bearing header it binds to.  There is no
        field for a raw secret value anywhere in this form.
        """
        form = {
            "connection_id": connection_id,
            "preset": preset or "custom-openai-compatible",
            "protocol": protocol or AccessProtocol.OPENAI_COMPATIBLE.value,
            "endpoint": endpoint,
            "credential_ref": credential_ref,
            "auth_header_name": auth_header_name,
            "model": model,
            "public_headers": public_headers,
        }
        if not connection_id.strip():
            return templates.TemplateResponse(
                request,
                "access.html",
                _access_context(
                    error="Connection name is required.",
                    form=_rejected_access_form(form),
                ),
                status_code=400,
            )
        try:
            headers = _parse_public_headers(public_headers)
        except ValueError as exc:
            return templates.TemplateResponse(
                request,
                "access.html",
                _access_context(
                    error=f"Invalid public headers: {exc}",
                    form=_rejected_access_form(form),
                ),
                status_code=400,
            )
        try:
            store = _access_store()
            registry = store.load()
            if registry.try_get(connection_id.strip()) is not None:
                raise ValueError(f"connection '{connection_id.strip()}' already exists")
            if preset.strip() and preset.strip().lower() != "custom-openai-compatible":
                connection = preset_connection(
                    preset.strip(),
                    credential_ref=credential_ref,
                    endpoint=endpoint,
                    model=model,
                    connection_id=connection_id.strip(),
                )
                if auth_header_name.strip() or headers:
                    connection = replace(
                        connection,
                        auth_header_name=auth_header_name.strip(),
                        public_headers=tuple(sorted(headers.items())),
                    )
            else:
                try:
                    access_protocol = AccessProtocol(protocol.strip())
                except ValueError as exc:
                    raise ValueError(f"unknown protocol {protocol!r}") from exc
                connection = ProviderConnection(
                    connection_id=connection_id.strip(),
                    provider="custom",
                    protocol=access_protocol,
                    backend="openai-compatible-api"
                    if access_protocol is AccessProtocol.OPENAI_COMPATIBLE
                    else "custom-api",
                    auth_mode=AuthMode.API_KEY,
                    endpoint=endpoint,
                    credential_ref=credential_ref,
                    public_headers=tuple(sorted(headers.items())),
                    auth_header_name=auth_header_name.strip(),
                    model=model,
                )
            store.save(build_access_registry((*registry.list_connections(), connection)))
        except (ValueError, AccessStoreError) as exc:
            # Never reflect potential secret material: every raw text
            # field is blanked from the re-rendered form (not just the
            # credential_ref/public-headers pair) and the submitted
            # reference is redacted from the error text (validation
            # messages may otherwise quote the offending value).
            message = str(exc)
            submitted_ref = credential_ref.strip()
            if submitted_ref and submitted_ref in message:
                message = message.replace(submitted_ref, "[redacted]")
            return templates.TemplateResponse(
                request,
                "access.html",
                _access_context(
                    error=f"Connection not saved: {message}",
                    form=_rejected_access_form(form),
                ),
                status_code=400,
            )
        saved_connection_id = connection.connection_id
        # C16-B2: adding a provider makes its models available for
        # selection, so run one bounded discovery immediately after a
        # successful save.  This never fails the save itself and never
        # erases durable state: a failed/UNKNOWN refresh keeps the last
        # valid catalog evidence (marked stale by the canonical helper).
        discovery_suffix = ""
        try:
            saved_registry = _access_store().load()
            saved_connection = saved_registry.try_get(saved_connection_id)
            if saved_connection is not None:
                auto_result = _discover_connection(
                    saved_connection, resolved_registry
                )
                auto_store = _model_catalog_store()
                try:
                    auto_catalog = auto_store.load()
                except ModelCatalogStoreError:
                    auto_catalog = ModelCatalog()
                auto_catalog = apply_discovery_result(auto_catalog, auto_result)
                try:
                    auto_store.save(auto_catalog)
                except ModelCatalogStoreError:
                    auto_result = replace(auto_result, ok=False)
                if auto_result.ok:
                    discovery_suffix = (
                        f"&auto_discovered={len(auto_result.models)}"
                    )
                else:
                    discovery_suffix = "&auto_discover_failed=1"
        except (AccessStoreError, ValueError):
            discovery_suffix = "&auto_discover_failed=1"
        return RedirectResponse(
            url=f"/access?connection_saved=1{discovery_suffix}",
            status_code=303,
        )

    @app.get("/routing", response_class=HTMLResponse, name="routing_selection")
    def routing_selection(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "routing.html",
            _routing_context(
                notice=_notice_from_query(request),
                owner=_load_owner_bindings_safely(),
                registry=resolved_registry,
            ),
        )

    @app.post("/routing/add", name="add_routing_model")
    def routing_add(
        request: Request,
        connection_id: Annotated[str, Form()] = "",
        model_id: Annotated[str, Form()] = "",
        context: Annotated[str, Form()] = "",
        binding_id: Annotated[str, Form()] = "",
    ) -> Response:
        """Add one owner-chosen model with an exact resolvable C11-B binding.

        This is the user's explicit decision, persisted through the
        canonical routing writer.  It never auto-enrolls a model: the model
        must be discovery-verified, fresh, and have an **exact C11-B
        binding** that resolves through the real adapter.  A registered
        adapter alone is never sufficient.  The exact ``binding_id`` is
        recorded through the canonical dynamic candidate contract in the
        *same* routing document -- for static-registry models too, with
        the authoritative static training semantics preserved exactly.
        C07 still decides context eligibility (unknown training
        requirement fails closed in training-denied contexts).
        """
        owner = _load_owner_bindings_safely()
        try:
            access = _access_store().load()
        except AccessStoreError as exc:
            return templates.TemplateResponse(
                request,
                "routing.html",
                _routing_context(
                    error=f"Access store unreadable: {exc}",
                    owner=owner,
                    registry=resolved_registry,
                ),
                status_code=500,
            )
        connection = access.try_get(connection_id.strip())
        if connection is None:
            return templates.TemplateResponse(
                request,
                "routing.html",
                _routing_context(
                    error=f"Unknown connection: {connection_id.strip()}",
                    owner=owner,
                    registry=resolved_registry,
                ),
                status_code=404,
            )
        catalog = _load_catalog_safely()
        entry = catalog.try_get(
            connection_id=connection.connection_id,
            provider=connection.provider,
            backend=connection.backend,
            model_id=model_id.strip(),
        )
        if entry is None or not entry.is_discovery_verified:
            return templates.TemplateResponse(
                request,
                "routing.html",
                _routing_context(
                    error=(
                        "Model cannot enter routing: it is not a discovery-verified "
                        "provider model for this connection."
                    ),
                    owner=owner,
                    registry=resolved_registry,
                ),
                status_code=400,
            )
        if entry.is_stale:
            return templates.TemplateResponse(
                request,
                "routing.html",
                _routing_context(
                    error=(
                        "Model cannot enter routing: discovery evidence is stale; "
                        "refresh models first."
                    ),
                    owner=owner,
                    registry=resolved_registry,
                ),
                status_code=400,
            )
        exact_binding = _resolve_exact_binding(
            owner,
            resolved_registry,
            provider=entry.provider,
            model=entry.model_id,
            connection_id=connection.connection_id,
            role=BindingRole.WORKER,
        )
        if exact_binding is None:
            return templates.TemplateResponse(
                request,
                "routing.html",
                _routing_context(
                    error=(
                        "Model cannot enter routing: no exact executable "
                        "binding resolves for this connection/model."
                    ),
                    owner=owner,
                    registry=resolved_registry,
                ),
                status_code=400,
            )
        # C15-XB-03: worker routing requires an exact binding whose
        # role is BindingRole.WORKER.  An ORCHESTRATOR-only binding
        # cannot authorize WORKER execution; reusing it would be a
        # role violation.
        if exact_binding.binding_role is not BindingRole.WORKER:
            return templates.TemplateResponse(
                request,
                "routing.html",
                _routing_context(
                    error=(
                        "Model cannot enter worker routing: exact binding "
                        f"role is {exact_binding.binding_role.value!r}, "
                        "not BindingRole.WORKER."
                    ),
                    owner=owner,
                    registry=resolved_registry,
                ),
                status_code=400,
            )
        submitted_binding_id = binding_id.strip()
        if submitted_binding_id and submitted_binding_id != exact_binding.binding_id:
            return templates.TemplateResponse(
                request,
                "routing.html",
                _routing_context(
                    error=(
                        "Model cannot enter routing: submitted binding identity "
                        "does not match the canonical registry binding."
                    ),
                    owner=owner,
                    registry=resolved_registry,
                ),
                status_code=400,
            )
        try:
            candidate_id = format_candidate_id(entry.provider, entry.model_id)
        except RoutingConfigError as exc:
            return templates.TemplateResponse(
                request,
                "routing.html",
                _routing_context(
                    error=f"Invalid candidate: {exc}",
                    owner=owner,
                    registry=resolved_registry,
                ),
                status_code=400,
            )
        top, _, sub = context.strip().partition(".")
        if not top or not sub or (top, sub) not in CONTEXT_KEYS:
            return templates.TemplateResponse(
                request,
                "routing.html",
                _routing_context(
                    error=f"Unknown routing context: {context!r}",
                    owner=owner,
                    registry=resolved_registry,
                ),
                status_code=400,
            )
        try:
            payload = load_effective_routing_payload()
            if candidate_id in ALLOWED_CANDIDATES:
                # A static candidate still needs its exact owner-selected
                # WORKER binding to survive restart/freeze: record the
                # binding-bearing approval in the same routing document.
                # The static training semantics are authoritative, so the
                # approval preserves them exactly (TRUE/FALSE from the
                # closed static registry -- never UNKNOWN, which would
                # fail closed and change training-denied behavior).
                static_training_required = (
                    "TRUE"
                    if requires_training_permission(candidate_id)
                    else "FALSE"
                )
                static_approval = DynamicCandidate(
                    candidate_id=candidate_id,
                    provider=entry.provider,
                    model=entry.model_id,
                    connection_id=connection.connection_id,
                    backend=connection.backend,
                    binding_id=exact_binding.binding_id,
                    discovery_status=entry.discovery_status.value,
                    execution_support=entry.execution_support.value,
                    training_required=static_training_required,
                    capabilities=tuple(
                        (name, state.value) for name, state in entry.capabilities
                    ),
                )
                updated = add_dynamic_candidate_to_chain(
                    payload, top=top, sub=sub, candidate=static_approval
                )
            else:
                approval = DynamicCandidate(
                    candidate_id=candidate_id,
                    provider=entry.provider,
                    model=entry.model_id,
                    connection_id=connection.connection_id,
                    backend=connection.backend,
                    binding_id=exact_binding.binding_id,
                    discovery_status=entry.discovery_status.value,
                    execution_support=entry.execution_support.value,
                    # No provider exposes a training-permission classification
                    # for a newly discovered model, so it stays UNKNOWN and
                    # fails closed in training-denied contexts.
                    training_required="UNKNOWN",
                    capabilities=tuple(
                        (name, state.value) for name, state in entry.capabilities
                    ),
                )
                updated = add_dynamic_candidate_to_chain(
                    payload, top=top, sub=sub, candidate=approval
                )
            save_user_routing(updated)
        except RoutingConfigError as exc:
            return templates.TemplateResponse(
                request,
                "routing.html",
                _routing_context(
                    error=f"Routing not saved: {exc}",
                    owner=owner,
                    registry=resolved_registry,
                ),
                status_code=400,
            )
        return RedirectResponse(url="/routing?routing_model_added=1", status_code=303)

    @app.post("/routing/remove", name="remove_routing_model")
    def routing_remove(
        request: Request,
        context: Annotated[str, Form()] = "",
        candidate_id: Annotated[str, Form()] = "",
    ) -> Response:
        """Remove one candidate from a routing chain (canonical writer)."""
        top, _, sub = context.strip().partition(".")
        if not top or not sub or (top, sub) not in CONTEXT_KEYS:
            return templates.TemplateResponse(
                request,
                "routing.html",
                _routing_context(
                    error=f"Unknown routing context: {context!r}",
                    owner=_load_owner_bindings_safely(),
                    registry=resolved_registry,
                ),
                status_code=400,
            )
        try:
            payload = load_effective_routing_payload()
            updated = remove_candidate_from_chain(
                payload, top=top, sub=sub, candidate_id=candidate_id.strip()
            )
            save_user_routing(updated)
        except RoutingConfigError as exc:
            return templates.TemplateResponse(
                request,
                "routing.html",
                _routing_context(
                    error=f"Routing not saved: {exc}",
                    owner=_load_owner_bindings_safely(),
                    registry=resolved_registry,
                ),
                status_code=400,
            )
        return RedirectResponse(url="/routing?routing_model_removed=1", status_code=303)

    @app.post("/routing/move", name="move_routing_model")
    def routing_move(
        request: Request,
        context: Annotated[str, Form()] = "",
        candidate_id: Annotated[str, Form()] = "",
        direction: Annotated[str, Form()] = "",
    ) -> Response:
        """Reorder one candidate within a routing chain (canonical writer).

        Only the relative order changes; membership is untouched.  The
        resulting canonical chain is rendered via the redirect notice on
        the routing page.
        """
        top, _, sub = context.strip().partition(".")
        if not top or not sub or (top, sub) not in CONTEXT_KEYS:
            return templates.TemplateResponse(
                request,
                "routing.html",
                _routing_context(
                    error=f"Unknown routing context: {context!r}",
                    owner=_load_owner_bindings_safely(),
                    registry=resolved_registry,
                ),
                status_code=400,
            )
        try:
            payload = load_effective_routing_payload()
            updated = move_candidate_in_chain(
                payload,
                top=top,
                sub=sub,
                candidate_id=candidate_id.strip(),
                direction=direction.strip(),
            )
            save_user_routing(updated)
        except RoutingConfigError as exc:
            return templates.TemplateResponse(
                request,
                "routing.html",
                _routing_context(
                    error=f"Routing not saved: {exc}",
                    owner=_load_owner_bindings_safely(),
                    registry=resolved_registry,
                ),
                status_code=400,
            )
        return RedirectResponse(url="/routing?routing_model_moved=1", status_code=303)

    @app.get("/orchestrator", response_class=HTMLResponse, name="orchestrator_selection")
    def orchestrator_selection(request: Request) -> HTMLResponse:
        """Show the canonical eligible bindings and the owner's Orch policy."""
        return templates.TemplateResponse(
            request,
            "orchestrator.html",
            _orchestrator_context(notice=_notice_from_query(request)),
        )

    @app.post("/orchestrator/select", name="select_orchestrator_model")
    def orchestrator_select(
        request: Request,
        binding_id: Annotated[str, Form()] = "",
        mode: Annotated[str, Form()] = OrchBindingMode.PINNED.value,
    ) -> Response:
        """Persist an OrchBindingPolicy over exact binding identity.

        The persisted choice is a binding id (never a free-floating
        provider/model setting).  PINNED is the primary mode; AUTO is also
        offered and resolves through the existing ``select_orch_binding``
        authority.  A selection that cannot resolve is refused.
        """
        owner = _load_owner_bindings_safely()
        try:
            parsed_mode = OrchBindingMode(mode.strip().upper())
        except ValueError:
            return templates.TemplateResponse(
                request,
                "orchestrator.html",
                _orchestrator_context(error=f"Unknown Orchestrator mode: {mode!r}"),
                status_code=400,
            )
        if parsed_mode is OrchBindingMode.PREFERRED_WITH_FALLBACK:
            # Not exposed in this bounded UI; never silently reinterpret it.
            return templates.TemplateResponse(
                request,
                "orchestrator.html",
                _orchestrator_context(
                    error="PREFERRED_WITH_FALLBACK is not available in this view."
                ),
                status_code=400,
            )
        if parsed_mode is OrchBindingMode.PINNED:
            wanted = binding_id.strip()
            binding = owner.registry.try_get(wanted)
            if binding is None:
                return templates.TemplateResponse(
                    request,
                    "orchestrator.html",
                    _orchestrator_context(
                        error=(
                            "No such canonical binding: "
                            f"{wanted or '(empty)'}"
                        )
                    ),
                    status_code=400,
                )
            # C15-XB-03: only ORCHESTRATOR-role bindings may be selected
            # for the Orchestrator-model slot.  A WORKER-role binding
            # is not qualified for the Orchestrator role -- selecting
            # it would be a role violation (C11-B BindingRole).
            if binding.binding_role is not BindingRole.ORCHESTRATOR:
                return templates.TemplateResponse(
                    request,
                    "orchestrator.html",
                    _orchestrator_context(
                        error=(
                            "Binding is not qualified for the Orchestrator "
                            "slot: role is "
                            f"{binding.binding_role.value!r}"
                        )
                    ),
                    status_code=400,
                )
            policy = OrchBindingPolicy(
                mode=OrchBindingMode.PINNED, pinned_binding_id=wanted
            )
        else:
            policy = OrchBindingPolicy(mode=OrchBindingMode.AUTO)
        selection = select_orch_binding(policy, owner.registry)
        if selection.status is not OrchSelectionStatus.RESOLVED:
            return templates.TemplateResponse(
                request,
                "orchestrator.html",
                _orchestrator_context(
                    error=(
                        "Orchestrator selection refused: "
                        f"{selection.reason or 'no eligible binding'}"
                    )
                ),
                status_code=400,
            )
        new_owner = replace(owner, policy=policy)
        try:
            save_owner_bindings(new_owner)
        except BindingStoreError as exc:
            return templates.TemplateResponse(
                request,
                "orchestrator.html",
                _orchestrator_context(error=f"Orchestrator policy not saved: {exc}"),
                status_code=500,
            )
        return RedirectResponse(url="/orchestrator?selected=1", status_code=303)

    @app.get("/reviewer", response_class=HTMLResponse, name="reviewer_selection")
    def reviewer_selection(request: Request) -> HTMLResponse:
        """Show the canonical eligible REVIEWER bindings and the selection."""
        return templates.TemplateResponse(
            request,
            "reviewer.html",
            _reviewer_context(
                notice=_notice_from_query(request),
                registry=resolved_registry,
            ),
        )

    @app.post("/reviewer/select", name="select_reviewer_model")
    def reviewer_select(
        request: Request,
        binding_id: Annotated[str, Form()] = "",
    ) -> Response:
        """Persist the owner's exact Reviewer binding choice.

        Only a ``REVIEWER``-role binding that exists in the canonical
        registry and resolves through the real C11-B resolver with the
        real adapter may be selected.  A ``WORKER``/``ORCHESTRATOR``
        binding, an unknown id, or an unresolvable binding is refused;
        nothing is persisted on refusal.
        """
        owner = _load_owner_bindings_safely()
        wanted = binding_id.strip()
        binding = owner.registry.try_get(wanted)
        if binding is None:
            return templates.TemplateResponse(
                request,
                "reviewer.html",
                _reviewer_context(
                    error=(
                        "No such canonical binding: "
                        f"{wanted or '(empty)'}"
                    ),
                    registry=resolved_registry,
                ),
                status_code=400,
            )
        # C11-B BindingRole: only REVIEWER-role bindings may be selected
        # for the reviewer slot.  A WORKER/ORCHESTRATOR-role binding is
        # not qualified for review -- selecting it would be a role
        # violation even when provider/model text matches.
        if binding.binding_role is not BindingRole.REVIEWER:
            return templates.TemplateResponse(
                request,
                "reviewer.html",
                _reviewer_context(
                    error=(
                        "Binding is not qualified for the Reviewer slot: "
                        f"role is {binding.binding_role.value!r}"
                    ),
                    registry=resolved_registry,
                ),
                status_code=400,
            )
        adapter = resolved_registry.get(binding.provider)
        try:
            if adapter is None:
                raise BindingResolutionError("NO_ADAPTER")
            resolve_execution_binding(
                registry=owner.registry,
                binding_id=binding.binding_id,
                adapter=adapter,
            )
        except BindingResolutionError:
            return templates.TemplateResponse(
                request,
                "reviewer.html",
                _reviewer_context(
                    error=(
                        "Reviewer selection refused: exact binding does not "
                        "resolve for this adapter"
                    ),
                    registry=resolved_registry,
                ),
                status_code=400,
            )
        new_owner = replace(owner, reviewer_binding_id=wanted)
        try:
            save_owner_bindings(new_owner)
        except BindingStoreError as exc:
            return templates.TemplateResponse(
                request,
                "reviewer.html",
                _reviewer_context(
                    error=f"Reviewer selection not saved: {exc}",
                    registry=resolved_registry,
                ),
                status_code=500,
            )
        return RedirectResponse(url="/reviewer?selected=1", status_code=303)

    @app.get("/health")
    def health() -> JSONResponse:
        """Expose non-secret runtime identity for stale-install diagnostics."""
        source_root = _PACKAGE_DIR.parents[1]
        sha, tree_state = _resolve_source_identity(_PACKAGE_DIR)
        return JSONResponse(
            {
                "product": "SaberOps",
                "version": saberops_version(),
                "package_path": str(_PACKAGE_DIR),
                "source_path": str(source_root),
                "source_git_sha": sha,
                "source_tree_state": tree_state,
                "database_path": str(active_runtime().db.db_path),
                "server_pid": os.getpid(),
                "orch_executable": resolve_orch_executable(),
            }
        )

    @app.on_event("shutdown")
    def _shutdown_pty() -> None:
        """Server shutdown must not orphan PTY child or corrupt transcript."""
        try:
            runtimes.shutdown()
        except Exception:
            pass

    return app
