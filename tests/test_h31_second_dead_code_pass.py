"""H31: the second dead-code pass stays deleted, and the safety surfaces stay.

Every removed symbol below had no caller on local ``main`` in source, scripts,
tests or the benchmark rig (web routes and HTTP handler methods were excluded).
The second half of each check asserts that a surface this pass deliberately
kept is still present: check protection, gate admission, review evidence,
acceptance, steering and test teardown were never treated as dead code.
"""

from __future__ import annotations

import importlib

import pytest


def _absent(module_name: str, *names: str) -> None:
    module = importlib.import_module(module_name)
    for name in names:
        assert not hasattr(module, name), f"{module_name}.{name} came back"


def _method_absent(module_name: str, owner: str, *names: str) -> None:
    module = importlib.import_module(module_name)
    cls = getattr(module, owner)
    for name in names:
        assert not hasattr(cls, name), f"{module_name}.{owner}.{name} came back"


def test_removed_plan_and_slicing_surface_is_absent() -> None:
    """Covers: in-run plan/package writers and the deleted projections are gone."""
    for module in ("saberops.candidate_diff", "saberops.gate_outcome_projection"):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(module)
    _method_absent(
        "saberops.db",
        "Database",
        "inherit_continuation_plan",
        "list_run_plans",
        "update_run_plan",
        "create_run_plan",
        "create_plan_step",
        "update_plan_step",
        "create_work_package",
        "set_work_package_scope",
        "get_work_package",
        "update_work_package_candidate",
    )
    _absent(
        "saberops.models",
        "IndependentWorkDecision",
        "DeterministicIntegration",
        "PostIntegrationValidation",
        "IndependenceState",
        "IntegrationDisposition",
        "PostIntegrationValidationState",
        "IndependentWorkBranch",
        "IntegrationParticipant",
    )


def test_removed_routing_and_dispatch_helpers_are_absent() -> None:
    """Covers: dead routing, effort, quota and projection helpers stay deleted."""
    _absent("saberops.dispatch", "refresh_quota_evidence")
    _absent("saberops.control_plane.policy", "effective_global_slot_limit")
    _absent(
        "saberops.control_plane.bindings",
        "_indexed",
        "_RESERVED_PROFILE_OVERLAY_KEYS",
    )
    _method_absent("saberops.control_plane.bindings", "ExecutionBinding", "supports_role")
    _method_absent(
        "saberops.control_plane.effort", "OrchestratorExecutionConfig", "bounds_for"
    )
    _absent("saberops.ready_frontier", "_STRUCTURAL_REASONS")
    _absent("saberops.routing_config", "_is_muse_id")
    _method_absent("saberops.routing_config", "ValidatedRouting", "role_groups")
    _absent("saberops.models", "GATE_SCRATCH_MODE_ORDER")


def test_removed_observability_and_scheduling_helpers_are_absent() -> None:
    """Covers: unused trace, report, supervisor and runtime readers stay deleted."""
    _method_absent(
        "saberops.observability.trace",
        "AttemptTraceRecorder",
        "record_failure",
        "ingest_native_event",
    )
    _absent("saberops.observability.trace", "_summary_digest")
    _absent("saberops.observability.run_report", "_routing_digest")
    _method_absent(
        "saberops.supervisor",
        "RunSupervisor",
        "get_transcript_bytes",
        "is_execution_live",
        "get_process",
    )
    _method_absent(
        "saberops.runtime",
        "ProjectRuntimeManager",
        "for_active",
        "for_repo",
        "for_run",
        "for_identity",
    )
    _absent("saberops.runtime_gate", "_digest_seconds")
    _absent("saberops.pause_work", "paused_run_paths")
    _absent("saberops.project", "project_db_path")


def test_removed_acceptance_and_store_helpers_are_absent() -> None:
    """Covers: dead acceptance markers, listeners and usage readers stay deleted."""
    _absent(
        "saberops.autonomous_runtime",
        "_accept_marker",
        "_parse_accept_marker",
        "ACCEPT_EVIDENCE_PREFIX",
    )
    _method_absent(
        "saberops.autonomous_runtime", "AutonomousSupervisor", "_policy_bindings"
    )
    _method_absent("saberops.orchestrator", "Orchestrator", "_autonomous_pre_accept_valid")
    _method_absent(
        "saberops.db",
        "Database",
        "add_event_listener",
        "remove_event_listener",
        "aggregate_provider_usage",
        "get_latest_review",
        "get_worker_checkpoint_for_attempt",
    )
    _absent("saberops.validation_policy", "with_validation_policy")
    _absent("saberops.owner_actions", "now_iso_timestamp")


def test_removed_web_and_adapter_leftovers_are_absent() -> None:
    """Covers: web-only constants and dead adapter/usage helpers stay deleted."""
    _absent(
        "saberops.web",
        "_PROVIDERS",
        "_TIERS",
        "_DEFAULT_GATE_COMMAND",
        "TERMINAL_EVENT_TYPES",
        "OWNER_ACTION_SSE_EVENT_TYPES",
        "_ACCOUNT_LABELS",
        "_UPSTREAM_LABELS",
    )
    _absent(
        "saberops.cli_usage",
        "CLIS",
        "MAX_LOG_BYTES",
        "MAX_LOG_FILES",
        "LIMIT_PROVIDERS",
    )
    _absent("saberops.capacity.probes", "ProbeWindow")
    _method_absent("saberops.workers.registry", "AdapterRegistry", "get_available_providers")
    _absent("saberops.native_workers", "_ClaimFields")
    _absent("saberops.attendance", "heartbeat_age")
    _absent("saberops.auto_accept", "_latest_event_id")
    _absent("saberops.run_titles", "chat_title")
    _absent("saberops.control_plane.binding_store", "_BINDING_STORE_DIR")
    _absent("saberops.control_plane.quota_policy", "_candidate_ids")
    _method_absent(
        "saberops.session_journal", "SessionJournal", "last_turn"
    )


def test_kept_safety_and_evidence_surfaces_are_present() -> None:
    """Covers: check protection, admission, review/acceptance and teardown stay."""
    from saberops import (
        gate_runner,
        gate_scope,
        job_evidence,
        paths,
        questions,
        routing_config,
        session_control,
        session_runtime,
    )
    from saberops.capacity import loop
    from saberops.control_plane import policy
    from saberops.db import Database
    from saberops.gate_profiles import GateProfile
    from saberops.jobs import service as jobs_service
    from saberops.owner_actions import OwnerActionSupervisor
    from saberops.sandbox.isolation import WorkerIsolationPolicy
    from saberops.storage_budget import StorageLedger
    from saberops.storage_cleanup import Inventory
    from saberops.validation_policy import MAX_WORKER_INSTRUCTION_LINES
    from saberops.workers.registry import AdapterRegistry

    # Check protection stays first-class, not test-only.
    assert callable(gate_scope.check_gate_protection)
    assert gate_scope.PROTECTED_GATE_SCRIPT_PATHS
    # Gate admission and its drill surface stay.
    assert callable(gate_runner.host_disk_admission)
    assert hasattr(gate_runner, "GateSlotPoolProtocol")
    assert hasattr(gate_runner.GateSlotPool, "current_lease")
    assert hasattr(GateProfile, "render_commands")
    # Review and acceptance evidence stay reachable.
    assert hasattr(Database, "get_findings_for_review")
    assert hasattr(Database, "get_findings_for_run")
    assert hasattr(OwnerActionSupervisor, "request_review")
    assert hasattr(OwnerActionSupervisor, "request_accept")
    assert hasattr(Database, "get_active_owner_action")
    # Steering and its undelivered-note guard stay.
    assert callable(jobs_service.undelivered_steering_notes)
    # Worker instruction budget, storage drills, question and session reads stay.
    assert MAX_WORKER_INSTRUCTION_LINES > 0
    assert hasattr(StorageLedger, "active_reservations")
    assert hasattr(Inventory, "by_class")
    assert hasattr(questions.OwnerQuestionQueue, "questions_for_run")
    assert hasattr(paths, "is_legacy_path")
    assert hasattr(paths, "get_projects_registry_path")
    assert hasattr(AdapterRegistry, "get_registered_providers")
    assert hasattr(session_control.SessionControls, "link_chat")
    assert session_runtime.REQUIRED_SERVER_ROUTES
    assert callable(job_evidence.read_attempt_manifest)
    assert routing_config.ROUTED_ROLES
    assert hasattr(Database, "set_run_title")
    assert hasattr(Database, "journal_record_started")
    # The live isolation check (not the unused convenience predicate) stays.
    assert hasattr(WorkerIsolationPolicy, "location_of")
    # Test teardown for schedulers started by tests stays.
    assert callable(loop.stop_scheduler)
    # Policy load/parsing stays even though its dead helper went.
    assert callable(policy.load_control_plane_policy)
