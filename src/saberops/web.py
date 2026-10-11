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
import threading
from collections.abc import AsyncGenerator, Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import (
    FastAPI,
    Form,
    Request,
    Response,
)
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from saberops.accept import AcceptEngine
from saberops.control_plane import (
    BindingRole,
    BindingStoreError,
    OwnerBindings,
    OwnerBindingStore,
    capability_profile_for,
    get_binding_store_path,
    materialize_discovered_binding,
    save_owner_bindings,
    upsert_owner_binding,
)
from saberops.control_plane.bindings import (
    BindingResolutionError,
    resolve_execution_binding,
)
from saberops.db import Database
from saberops.model_access import (
    ACCOUNT_BACKENDS,
    AccessStore,
    ConnectionTestReason,
    ExecutionSupport,
    ModelCatalog,
    ModelCatalogStore,
    ModelCatalogStoreError,
    ModelDiscoveryResult,
    ModelListingError,
    ModelSource,
    ProviderConnection,
    ReadinessService,
    ReadinessStore,
    discover_connection_models,
    is_virtual_route,
)
from saberops.models import (
    TERMINAL_RUN_STATUSES,
    CheckResult,
    ReviewVerdict,
    Run,
    RunStatus,
)
from saberops.observability.run_report import (
    build_run_report,
    run_explanation,
    saberops_version,
)
from saberops.owner_actions import (
    LAUNCH_FAILED_REASON,
    OwnerActionSupervisor,
    summarize_owner_actions,
)
from saberops.project import owner_state_dir
from saberops.run_titles import fallback_title
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
            "summary": run.display_title or fallback_title(run.task, run.target_repo),
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
                "findings": _finding_rows(db, review.id),
            }
        )
    return rows


def _finding_rows(db: Database, review_id: str) -> list[dict[str, Any]]:
    """Structured reviewer findings for one review, blocking ones first."""
    from saberops.review_policy import FINDING_LABELS

    try:
        findings = db.get_findings_for_review(review_id)
    except Exception:
        return []
    rows = [
        {
            "code": f.classification,
            "label": FINDING_LABELS.get(f.classification, f.classification),
            "tone": "fail" if f.blocking else (
                "warning" if f.classification == "D1" else "pass"
            ),
            "blocking": f.blocking,
            "title": f.title,
            "severity": f.severity,
            "location": f.code_location or "",
            "requirement": f.invariant_or_requirement,
            "contract": f.contract_reference or "",
            "failure": f.failure_scenario,
            "evidence": f.evidence,
            "correction": f.minimum_correction,
            "scope": f.repair_scope.replace("_", " ").lower(),
        }
        for f in findings
    ]
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    rows.sort(
        key=lambda r: (not r["blocking"], r["code"] != "D1", order.get(str(r["severity"]), 4))
    )
    return rows


def resolve_owner_argv_prefix() -> list[str]:
    """Resolve the argv prefix for a detached/supervised execution owner.

    Backward-compatible re-export of :func:`saberops.cli_environment.resolve_owner_argv_prefix`
    (the single authority lives in cli_environment so engine modules need
    not import the web console).
    """
    from saberops.cli_environment import resolve_owner_argv_prefix as _resolve

    return _resolve()


def resolve_orch_executable() -> str:
    """Resolve the orchestrator executable identity for display/diagnostics.

    Backward-compatible re-export of :func:`saberops.cli_environment.resolve_orch_executable`.
    """
    from saberops.cli_environment import resolve_orch_executable as _resolve

    return _resolve()

#: What each authority preset changes in SaberOps today. Policy fields that
#: only the unwired autonomous project runner reads are not promised here.
_AUTHORITY_EFFECTS: dict[str, str] = {
    "ask_before_actions": "Runs behave like Supervised. Your synced OpenCode agent asks before every action.",
    "supervised": "Gate and review run automatically. You accept every candidate.",
    "autonomous": "Like Supervised, and every candidate that passes the gate is pushed to the repository's origin as orch/<run>/a<n>. You still accept.",
    "full_autonomy": "Same as Autonomous today. Automatic acceptance is not wired yet.",
    "full_autonomy_publish": "Same as Autonomous today. Automatic acceptance and publishing are not wired yet.",
}


def _dashboard_context(
    service: OrchestratorService,
    *,
    error: str | None = None,
    notice: str | None = None,
) -> dict[str, Any]:
    """Build the minimal status-page context: the run list."""
    return {
        "runs": _run_rows(service.list_runs(limit=50)),
        "saberops_version": saberops_version(),
        "error": error,
        "notice": notice,
    }


def _run_detail_context(
    db: Database,
    run: Run,
    *,
    confirming_accept: bool = False,
    confirming_reject: bool = False,
    accept_result: dict[str, str] | None = None,
    error: str | None = None,
    notice: str | None = None,
) -> dict[str, Any]:
    """Build the minimal run-detail status context.

    L-A step 5: status, attempts, gate results, review verdict, and diff
    only.  Continuation offers, git worktrees, resume capsules, frozen
    tools, plans, and the review/cleanup/retry/cancel actions now live in
    the ``orch`` CLI.
    """
    # Acceptance authority lives in AcceptEngine.  The Web renders the
    # engine-derived read-only projection as a checklist and gates the
    # Accept button on it; it never recreates acceptance policy.
    eligibility = AcceptEngine(db=db).check_accept_eligibility(run.id)
    can_accept = eligibility.eligible
    accept_checklist = eligibility.as_dict()["checks"]
    # Rejectability derives from the same service/projection
    # authority as the Reject action (never from RunStatus alone), and
    # the Candidate State shown is the projection including
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

    candidate_sha: str | None = None
    changed_files: list[str] = []
    diff_text = ""
    diff_files: tuple[dict[str, object], ...] = ()
    diff_totals: dict[str, int | bool] | None = None
    diff_truncated = False
    diff_available = False
    diff_unavailable_reason: str | None = None
    failure_reason: str | None = run.result_summary

    for att in attempts:
        if att.worker_error:
            failure_reason = att.worker_error

    # The Changes view is the same read-only candidate-diff
    # projection as GET /runs/{run_id}/diff -- frozen base_commit ..
    # Current candidate SHA from the run's persisted repository.
    # Event changed_paths and latest-attempt SHAs are never consulted.
    try:
        from saberops.candidate_diff import project_candidate_diff

        diff = project_candidate_diff(db, run.id)
        candidate_sha = diff.candidate_sha
        changed_files = list(diff.changed_paths)
        diff_text = diff.diff_text
        diff_files = diff.files
        diff_totals = diff.totals
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
    # Durable owner-action projection.  This says whether the
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
        "display_title": run.display_title or fallback_title(run.task, run.target_repo),
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
        "events": events,
        "candidate_sha": candidate_sha,
        "changed_files": changed_files,
        "diff_text": diff_text,
        "diff_files": diff_files,
        "diff_totals": diff_totals,
        "diff_truncated": diff_truncated,
        "diff_available": diff_available,
        "diff_unavailable_reason": diff_unavailable_reason,
        "failure_reason": failure_reason,
        "can_accept": can_accept,
        "accept_checklist": accept_checklist,
        "candidate_state": candidate_state,
        "can_reject": can_reject,
        "owner_review_active": owner_review_active,
        "owner_review_state": owner_review_state,
        "owner_review_action": owner_review_action,
        "owner_accept_active": owner_accept_active,
        "owner_accept_state": owner_accept_state,
        "owner_accept_action": owner_accept_action,
        "owner_review_last_error": owner_review_last_error,
        "owner_accept_last_error": owner_accept_last_error,
        "confirming_accept": confirming_accept,
        "confirming_reject": confirming_reject,
        "accept_result": accept_result,
        "error": error,
        "notice": notice,
        "current_dispatch": current_dispatch,
        "usage": usage,
        "run_report": run_report,
        "chat_explanation": run_explanation(run_report) if run_report else run.result_summary,
    }


def _missing_run_context(run_id: str) -> dict[str, Any]:
    """Build the no-such-run context."""
    return {"missing": True, "run_id": run_id}


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
    ``ORCH_BINDING_STORE`` pins an explicit file.  The document is binding
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

    Identity must include the connection (the real account the
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
    """Resolve the exact binding for a model through the real resolver.

    Returns the binding only when an exact registry entry exists AND
    :func:`resolve_execution_binding` succeeds against the real adapter;
    otherwise ``None`` (no provider/model fallback, no synthesized identity).

    The lookup accepts an explicit ``connection_id`` and
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
    from urllib.request import build_opener

    from saberops.workers.api_agent import NoRedirect

    request = UrlRequest(url, method="GET", headers=headers)
    with build_opener(NoRedirect()).open(request, timeout=5) as response:
        return bytes(response.read(8_000_001))


def _model_list_url(endpoint: str) -> str:
    """Derive a read-only catalog URL without changing connection configuration."""
    from urllib.parse import urlsplit, urlunsplit

    parsed = urlsplit(endpoint)
    path = parsed.path.rstrip("/")
    # OpenRouter's model catalog is shared across its API surfaces, including
    # alpha/decisions. Never apply this host-specific rule to a custom server.
    if parsed.hostname == "openrouter.ai":
        path = "/api/v1/models"
    else:
        for suffix in ("/chat/completions", "/completions", "/responses"):
            if path.endswith(suffix):
                path = path[:-len(suffix)]
                break
        if not path.endswith("/models"):
            path += "/models"
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment))


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
    url = _model_list_url(endpoint)
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
    if len(raw) > 8_000_000:
        raise ModelListingError(
            ConnectionTestReason.PROTOCOL_UNSUPPORTED, "model list exceeds response size limit"
        )
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
    if descriptor is None or not descriptor.has_model_listing:
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
    if descriptor.model_list_protocol:
        from saberops.model_access.native_listing import list_native_models

        return list_native_models(executable, descriptor.model_list_protocol)
    stdout = _run_backend_listing([executable, *descriptor.model_list_argv])
    if descriptor.backend == "antigravity":
        # Native `agy models` emits exact ID<TAB>display name pairs.
        # Never interpret headers, errors or display names as executable IDs.
        return [
            line.split("\t", 1)[0].strip()
            for line in stdout.splitlines()
            if "\t" in line and line.split("\t", 1)[0].strip()
        ]
    return _parse_backend_model_list(stdout)


def _materialize_discovered_binding(
    connection: ProviderConnection,
    model_id: str,
    registry: AdapterRegistry,
    *,
    owner: OwnerBindings | None = None,
    persist: bool = True,
) -> tuple[OwnerBindings, Any] | None:
    """Translate validated discovery evidence into exact bindings.

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
    each see their own exact binding and quota policy remains authoritative
    over the real quota pool.  Two distinct account connections to the
    same provider register distinct ``profile_id`` and distinct
    ``quota_pool_id`` -- provider identity is not quota-resource
    identity, and semantic role never duplicates either resource.

    The exact bindings are persisted only when the WORKER and REVIEWER
    bindings prove through the real resolver with the real
    adapter, so a provider/model name can never become executable
    without an exact binding that actually resolves.  Returns
    ``(owner, worker_binding)`` or ``None``.
    """
    adapter = registry.get(connection.provider)
    if adapter is None:
        return None
    from saberops.control_plane.bindings import BindingKind
    from saberops.workers.direct_api import DirectAPIAdapter, supports_connection
    api = not connection.is_account_backed
    if api:
        if not supports_connection(connection):
            return None
        adapter = DirectAPIAdapter(connection.provider)
    profile = capability_profile_for(adapter)
    worker_binding, execution_profile, pool = materialize_discovered_binding(
        connection_id=connection.connection_id,
        provider=connection.provider,
        model=model_id,
        provider_transport=profile.transport,
        harness_capabilities=profile.capabilities,
        binding_role=BindingRole.WORKER,
        backend=BindingKind.DIRECT_API if api else BindingKind.DIRECT_CLI,
    )
    reviewer_binding, _reviewer_profile, _reviewer_pool = materialize_discovered_binding(
        connection_id=connection.connection_id,
        provider=connection.provider,
        model=model_id,
        provider_transport=profile.transport,
        harness_capabilities=profile.capabilities,
        binding_role=BindingRole.REVIEWER,
        backend=BindingKind.DIRECT_API if api else BindingKind.DIRECT_CLI,
    )
    orch_binding, _orch_profile, _orch_pool = materialize_discovered_binding(
        connection_id=connection.connection_id,
        provider=connection.provider,
        model=model_id,
        provider_transport=profile.transport,
        harness_capabilities=profile.capabilities,
        binding_role=BindingRole.ORCHESTRATOR,
        backend=BindingKind.DIRECT_API if api else BindingKind.DIRECT_CLI,
    )
    owner = owner if owner is not None else _load_owner_bindings_safely()
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
        resolve_execution_binding(
            registry=candidate.registry,
            binding_id=orch_binding.binding_id,
            adapter=adapter,
        )
    except BindingResolutionError:
        return None
    if persist and not _save_owner_bindings(candidate):
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
    adapter materializes exact role bindings per discovered model and proves them
    through the canonical resolver. Unsupported protocols and models with no
    resolvable exact binding stay ``UNSUPPORTED`` / ``UNKNOWN``. A failed or
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
            from saberops.workers.direct_api import supports_connection
            if not supports_connection(connection) or is_virtual_route(
                connection.provider, model.model_id
            ):
                support[model.identity] = ExecutionSupport.UNSUPPORTED
                continue
        if registry.get(connection.provider) is None:
            support[model.identity] = ExecutionSupport.UNSUPPORTED
            continue
        materialized = _materialize_discovered_binding(
            connection, model.model_id, registry, owner=owner, persist=False
        )
        if materialized is None:
            support[model.identity] = ExecutionSupport.UNKNOWN
            continue
        owner, _binding = materialized
        support[model.identity] = ExecutionSupport.SUPPORTED
    if ExecutionSupport.SUPPORTED in support.values() and not _save_owner_bindings(owner):
        support = {
            identity: ExecutionSupport.UNKNOWN if value is ExecutionSupport.SUPPORTED else value
            for identity, value in support.items()
        }
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
    one exists (listing commands or Claude/Copilot native control protocols); backends with no
    supported enumeration stay truthfully ``UNKNOWN`` rather than inventing a
    catalog. API connections use their configured env credential reference
    for a read-only OpenAI-compatible catalog request; credentials never
    enter durable discovery evidence. Missing credentials permit only public listing.

    Execution support is proven *after* enumeration by materializing an exact
    Binding and resolving it with the real adapter -- never inferred
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
        if descriptor is not None and descriptor.has_model_listing:
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
        from saberops.workers.direct_api import credential_value, supports_connection

        def api_headers() -> dict[str, str]:
            headers = dict(connection.public_headers)
            if supports_connection(connection):
                try:
                    credential = credential_value(connection)
                except ValueError:
                    credential = ""
                if credential:
                    name = connection.auth_header_name or "Authorization"
                    headers[name] = (
                        f"Bearer {credential}" if name.lower() == "authorization" else credential
                    )
            return headers

        result = discover_connection_models(
            connection,
            list_models=lambda: _fetch_openai_compatible_models(
                connection.endpoint, auth_headers=api_headers
            ),
            discovered_at=discovered_at,
        )
    return _apply_exact_execution_bindings(connection, result, registry)


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


def initialize_account_readiness() -> None:
    """Acquire bounded native CLI evidence at UI launch, in the selected profile."""
    ReadinessService(
        registry=_access_store().load(), store=_readiness_store()
    ).refresh_accounts_on_startup()


def _probe_api_connection(*, endpoint: str) -> dict[str, str]:
    """Safely probe an API connection without transmitting any secret.

    The probe performs an unauthenticated GET against the derived model-list
    URL with a short timeout. A 404 reports a missing catalog route; other
    HTTP answers prove reachability (authentication remains NOT VERIFIED --
    the stored credential value is never read, let alone sent).
    """
    from urllib.error import HTTPError, URLError
    from urllib.request import Request as UrlRequest
    from urllib.request import urlopen

    if not endpoint:
        return {"ok": "FALSE", "reason": "CONFIG_INVALID", "detail": "endpoint not configured"}
    request = UrlRequest(_model_list_url(endpoint), method="GET")
    try:
        with urlopen(request, timeout=5) as response:  # noqa: S310
            status = int(getattr(response, "status", 200))
    except HTTPError as exc:
        if exc.code == 404:
            return {
                "ok": "FALSE",
                "reason": ConnectionTestReason.ENDPOINT_UNREACHABLE.value,
                "detail": "server reachable, but model list endpoint not found (HTTP 404); "
                "check the API base URL. Authentication NOT VERIFIED (credential never transmitted)",
            }
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
    if request.query_params.get("authority_saved") == "1":
        return "Authority settings saved. New runs use them."
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
        return (
            "Reviewer model selection saved (exact binding identity)."
            if request.query_params.get("role") == "reviewer"
            else "Orchestrator model selection saved (exact binding identity)."
        )
    if request.query_params.get("project_selected") == "1":
        return "Target project updated."
    return None


def create_app(
    db_path: str | Path | None = None,
    repo_path: str | Path | None = None,
    registry: AdapterRegistry | None = None,
    worktree_base: Path | None = None,
    state_root: Path | str | None = None,
    seed_cwd_if_empty: bool = True,
) -> FastAPI:
    """Build the minimal FastAPI status application.

    L-A step 5 cut the console to a minimal status page: run list,
    run detail (status, attempts, gate results, review verdict, diff),
    report JSON, accept/reject actions, and health.  Owner configuration
    (routing, quota, access, reviewer, projects, chats) lives in the
    ``orch`` CLI now.  The ``projects`` parameter is gone with
    ``saberops.projects``; ``repo_path`` is accepted for launch
    compatibility but the page always shows the pinned store's runs.
    Passing an explicit ``db_path`` pins the whole app to that single store.
    """
    del repo_path, seed_cwd_if_empty  # launch compatibility only; see docstring.
    resolved_registry = registry if registry is not None else AdapterRegistry.default()

    runtimes = ProjectRuntimeManager(
        registry=resolved_registry,
        worktree_base=worktree_base,
        pinned_db_path=db_path,
        state_root=state_root,
    )

    def runtime_for_run(run_id: str) -> ProjectRuntime | None:
        """Runtime owning an existing run, resolved from the run itself."""
        return runtimes.for_run(run_id)

    pinned = runtimes.legacy()
    db = pinned.db
    service = pinned.service
    supervisor = pinned.supervisor

    app = FastAPI(title="SaberOps Status")
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
    templates.env.globals["fmt"] = format_timestamp

    @app.middleware("http")
    async def _origin_guard(request: Request, call_next: Callable[..., Any]) -> Response:
        """Refuse cross-site/foreign-origin POSTs to the accept/reject actions.

        Host/Origin headers never choose what the actions do; a foreign
        origin fails closed.  Read-only GETs pass through untouched.
        """
        path = request.url.path
        mutation = request.method == "POST" and (
            (path.startswith("/runs/") and path.endswith("/accept"))
            or (path.startswith("/runs/") and path.endswith("/reject"))
        )
        origin = request.headers.get("origin")
        if mutation and (request.headers.get("sec-fetch-site") == "cross-site"
                         or (origin is not None and origin != str(request.base_url).rstrip("/"))):
            return JSONResponse({"error": "Use the local SaberOps page for this action."}, status_code=403)
        response: Response = await call_next(request)
        return response
    app.state.db = db
    app.state.service = service
    app.state.supervisor = supervisor
    app.state.runtimes = runtimes

    @app.get("/", response_class=HTMLResponse, name="dashboard")
    def dashboard(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            _dashboard_context(service, notice=_notice_from_query(request)),
        )

    @app.get("/runs/{run_id}", response_class=HTMLResponse, name="run_detail")
    def run_detail(request: Request, run_id: str) -> HTMLResponse:
        runtime = runtime_for_run(run_id)
        run = runtime.db.get_run(run_id) if runtime is not None else None
        if runtime is None or run is None:
            return templates.TemplateResponse(
                request, "run_detail.html", _missing_run_context(run_id), status_code=404
            )
        context = _run_detail_context(runtime.db, run, notice=_notice_from_query(request))
        return templates.TemplateResponse(request, "run_detail.html", context)

    @app.get("/runs/{run_id}/report")
    def run_report_json(run_id: str) -> JSONResponse:
        """Read-only canonical run report as JSON.

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
        if runtime is None:
            return JSONResponse({"error": "run not found"}, status_code=404)
        db = runtime.db
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
        # A terminal run with an active post-run owner action
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
        # Confirmed Accept claims a durable owner action and detaches
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

    @app.get("/health")
    def health() -> JSONResponse:
        """Expose non-secret runtime identity for stale-install diagnostics."""
        source_root = _PACKAGE_DIR.parents[1]
        sha, tree_state = _resolve_source_identity(_PACKAGE_DIR)
        return JSONResponse({
            "product": "SaberOps",
            "version": saberops_version(),
            "package_path": str(_PACKAGE_DIR),
            "source_path": str(source_root),
            "source_git_sha": sha,
            "source_tree_state": tree_state,
            "database_path": str(db.db_path),
            "server_pid": os.getpid(),
            "orch_executable": resolve_orch_executable(),
        })

    @app.on_event("shutdown")
    def _shutdown() -> None:
        """Server shutdown releases cached runtimes without corrupting state."""
        try:
            runtimes.shutdown()
        except Exception:
            pass

    return app
