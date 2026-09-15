"""SQLite state persistence for runs, attempts, gate checks, and events."""
# ruff: noqa: E501

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orchestrator_mvp.models import (
    TERMINAL_RUN_STATUSES,
    Attempt,
    AttemptStatus,
    AttemptTrace,
    CheckResult,
    DispatchEvidence,
    DispatchRole,
    DispatchStage,
    EffectStatus,
    Handoff,
    HealthState,
    ManagerState,
    ModelCallUsage,
    OrchEvent,
    PhaseObservation,
    PlanStatus,
    PlanStep,
    PlanStepStatus,
    QuotaObservation,
    QuotaState,
    ReviewFinding,
    ReviewResult,
    ReviewVerdict,
    RoutingState,
    Run,
    RunPlan,
    RunStatus,
    SideEffectOperation,
    SideEffectRecord,
    SideEffectState,
    SupervisionRecord,
    Tier,
    ToolEffect,
    TraceCapability,
    WorkerActivation,
    WorkerActivationState,
    WorkerCheckpoint,
    WorkPackage,
)
from orchestrator_mvp.ownership import (
    ExecutionOwner,
    Liveness,
    OwnershipConflictError,
    OwnershipError,
    OwnershipStoreError,
    ProcessIdentity,
    probe_liveness,
)
from orchestrator_mvp.quota import derive_routing_state, normalize_quota_observation
from orchestrator_mvp.wake import notify_run


def get_default_db_path() -> Path:
    """Return the default SQLite database path according to XDG state specification.

    If $XDG_STATE_HOME is set: $XDG_STATE_HOME/orchestrator-mvp/orchestrator.db
    Otherwise: ~/.local/state/orchestrator-mvp/orchestrator.db
    """
    xdg_state_home = os.environ.get("XDG_STATE_HOME")
    if xdg_state_home and xdg_state_home.strip():
        base_dir = Path(xdg_state_home).expanduser()
    else:
        base_dir = Path.home() / ".local" / "state"
    return base_dir / "orchestrator-mvp" / "orchestrator.db"


def current_iso_timestamp() -> str:
    """Return current UTC timestamp in ISO 8601 format."""
    return datetime.now(UTC).isoformat()


def _is_missing_column(exc: sqlite3.OperationalError, columns: tuple[str, ...]) -> bool:
    """Return True when ``exc`` reports one of ``columns`` as unknown."""
    message = str(exc)
    return any(column in message for column in columns)


def _is_missing_ownership_table(exc: sqlite3.OperationalError) -> bool:
    """True only for the one legacy condition: no ``execution_owners`` table."""
    message = str(exc).lower()
    return "no such table" in message and "execution_owners" in message


def _decode_process_identity(raw: str | None) -> ProcessIdentity | None:
    """Decode a persisted process identity, tolerating legacy/blank values."""
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return ProcessIdentity.from_dict(data) if isinstance(data, dict) else None


def _optional_text(row: sqlite3.Row, name: str) -> str | None:
    """Read an additive column that legacy databases may not have at all."""
    try:
        value = row[name]
    except (KeyError, IndexError, sqlite3.OperationalError):
        return None
    return str(value) if value else None


def _drop_gateway_evidence(evidence: DispatchEvidence) -> None:
    """Clear the C10 gateway field on legacy rows that lack the column."""
    evidence.gateway_evidence_json = None


def _drop_c11d_identity(evidence: DispatchEvidence) -> None:
    """Clear the C11-D binding identity / effort fields on legacy rows."""
    evidence.binding_id = None
    evidence.profile_id = None
    evidence.quota_pool_id = None
    evidence.reasoning_effort = None


def _dispatch_values_c10(evidence: DispatchEvidence) -> tuple[object, ...]:
    """Return dispatch values without the C11-D identity/effort columns."""
    full = _dispatch_values(evidence)
    c11d_trailing = 4
    if all(v is None for v in full[-c11d_trailing:]):
        return full[:-c11d_trailing]
    # Never serialize real C11-D data into a legacy row.
    return full[:-c11d_trailing]


def _dispatch_values_legacy(evidence: DispatchEvidence) -> tuple[object, ...]:
    """Return dispatch values without the C10 gateway column."""
    full = _dispatch_values(evidence)
    c11d_trailing = 4
    gateway_idx = len(full) - c11d_trailing - 1
    if (
        full[gateway_idx] is None
        and getattr(evidence, "gateway_evidence_json", None) is None
        and all(v is None for v in full[-c11d_trailing:])
    ):
        return full[:gateway_idx]
    # The C10 field is at gateway_idx; never serialize real data into
    # a legacy row.
    return full[:gateway_idx]


def _dispatch_values(evidence: DispatchEvidence) -> tuple[object, ...]:
    """Return dispatch columns in the schema's stable order."""
    return (
        evidence.id,
        evidence.run_id,
        evidence.association_id,
        evidence.attempt_id,
        evidence.review_id,
        evidence.stage.value,
        evidence.role.value,
        evidence.tier.value,
        evidence.dispatch_reason,
        evidence.requested_provider,
        evidence.requested_model,
        evidence.actual_provider,
        evidence.actual_model,
        evidence.package_id,
        evidence.plan_step_id,
        evidence.base_sha,
        evidence.candidate_sha,
        evidence.inherited_checkpoint_sha,
        evidence.context_mode,
        evidence.handoff_id,
        evidence.prompt_bytes,
        evidence.prompt_sha256,
        evidence.prompt_sections_json,
        int(evidence.raw_parent_included) if evidence.raw_parent_included is not None else None,
        evidence.validation_policy_occurrence_count,
        evidence.prompt_transport,
        evidence.tool_transport,
        evidence.authority_mode,
        evidence.routing_snapshot_digest,
        evidence.selected_candidate,
        evidence.prior_failure_refs_json,
        evidence.started_at,
        evidence.completed_at,
        evidence.dispatch_outcome,
        evidence.interruption_reason,
        evidence.interruption_timestamp,
        evidence.handoff_bytes,
        evidence.replacement_attempt_id,
        evidence.time_to_resume_seconds,
        evidence.tool_schema_bytes,
        evidence.tool_description_bytes,
        evidence.tool_discovery_bytes,
        evidence.tool_result_bytes,
        evidence.tools_exposed_count,
        evidence.tools_actually_used_count,
        evidence.control_plane_digest,
        getattr(evidence, "gateway_evidence_json", None),
        getattr(evidence, "binding_id", None),
        getattr(evidence, "profile_id", None),
        getattr(evidence, "quota_pool_id", None),
        getattr(evidence, "reasoning_effort", None),
    )


#: Every durable Project Ledger table, with the exact columns a ledger write
#: may touch.  The Project Ledger's *public* mutation surface is the typed
#: command set in :mod:`orchestrator_mvp.project_ledger`; this allow-list is
#: the persistence-layer backstop that keeps even an internal caller from
#: reaching a column no ledger command owns.
PROJECT_LEDGER_COLUMNS: dict[str, frozenset[str]] = {
    "project_ledger": frozenset(
        {
            "project_id",
            "canonical_path",
            "remote_identity",
            "root_commit",
            "origin",
            "genesis_json",
            "project_version",
            "schema_version",
            "created_at",
            "updated_at",
        }
    ),
    "project_facts": frozenset(
        {
            "id",
            "project_id",
            "category",
            "statement",
            "reference",
            "origin",
            "status",
            "provenance_json",
            "supersedes",
            "superseded_by",
            "introduced_version",
            "introduced_at",
            "retired_version",
            "retired_at",
        }
    ),
    "project_decisions": frozenset(
        {
            "id",
            "project_id",
            "statement",
            "rationale",
            "reference",
            "origin",
            "status",
            "provenance_json",
            "supersedes",
            "superseded_by",
            "introduced_version",
            "introduced_at",
            "retired_version",
            "retired_at",
        }
    ),
    "project_roadmap_items": frozenset(
        {
            "id",
            "project_id",
            "title",
            "status",
            "ordering",
            "depends_on_json",
            "acceptance_ref",
            "result_ref",
            "run_ref",
            "origin",
            "provenance_json",
            "revision",
            "introduced_version",
            "introduced_at",
            "updated_version",
            "updated_at",
        }
    ),
    "project_open_questions": frozenset(
        {
            "id",
            "project_id",
            "question",
            "status",
            "resolution",
            "resolution_ref",
            "origin",
            "provenance_json",
            "introduced_version",
            "introduced_at",
            "resolved_version",
            "resolved_at",
        }
    ),
    "project_change_intake": frozenset(
        {
            "id",
            "project_id",
            "summary",
            "detail",
            "origin",
            "disposition",
            "linked_fact_id",
            "linked_decision_id",
            "linked_roadmap_id",
            "provenance_json",
            "recorded_version",
            "recorded_at",
            "decided_version",
            "decided_at",
        }
    ),
}


def _is_missing_project_ledger_table(exc: sqlite3.OperationalError) -> bool:
    """True only when a C11 ledger table is absent from a legacy database.

    A read-only legacy store cannot be migrated on open, so ledger *reads*
    report "nothing recorded" instead of raising.  Writes deliberately keep
    failing closed: a database that cannot hold Project truth must never look
    as though it accepted some.
    """
    message = str(exc).lower()
    return "no such table" in message and any(
        table in message for table in (*PROJECT_LEDGER_COLUMNS, "project_ledger_events",
                                       "project_checkpoints")
    )


#: The primary key column of every ledger table an update may address.
_PROJECT_LEDGER_KEYS: dict[str, str] = {
    "project_ledger": "project_id",
    "project_facts": "id",
    "project_decisions": "id",
    "project_roadmap_items": "id",
    "project_open_questions": "id",
    "project_change_intake": "id",
}

LedgerValue = str | int | None


@dataclass(frozen=True)
class ProjectLedgerRow:
    """One whitelisted durable Project Ledger row write.

    ``mode`` is ``"insert"`` or ``"update"``.  Deletes are deliberately absent:
    accepted Project history is superseded or retired, never removed.
    """

    table: str
    mode: str
    values: Mapping[str, LedgerValue]

    def validate(self) -> None:
        """Fail closed on an unknown table, column, or mode."""
        allowed = PROJECT_LEDGER_COLUMNS.get(self.table)
        if allowed is None:
            raise ValueError(f"'{self.table}' is not a Project Ledger table")
        if self.mode not in ("insert", "update"):
            raise ValueError(f"unsupported ledger write mode '{self.mode}'")
        unknown = sorted(set(self.values) - allowed)
        if unknown:
            raise ValueError(
                f"ledger write to '{self.table}' names unknown column(s): {', '.join(unknown)}"
            )
        key = _PROJECT_LEDGER_KEYS[self.table]
        if key not in self.values:
            raise ValueError(f"ledger write to '{self.table}' must carry its key '{key}'")


@dataclass(frozen=True)
class ProjectLedgerEventRow:
    """The append-only event every authoritative ledger mutation produces."""

    project_id: str
    event_type: str
    subject_kind: str
    subject_id: str
    project_version: int
    state_digest: str
    payload_json: str


class Database:
    """Durable SQLite storage for orchestrator state."""

    def __init__(
        self,
        db_path: Path | str | None = None,
        owner_db: Database | None = None,
        read_only: bool = False,
    ) -> None:
        resolved_path = get_default_db_path() if db_path is None else Path(db_path)
        self.db_path = resolved_path.resolve()
        self._read_only = read_only
        if not read_only:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._listeners: list[Callable[[str, int], None]] = []
        self._listeners_lock = threading.Lock()
        self._plan_schema_available = True
        # Run state is project-local, but provider/account quota is owner-wide
        # configuration and stays in one global store shared by every project.
        self._owner_db = (
            owner_db if owner_db is not None and owner_db.db_path != self.db_path else None
        )
        self._init_db()

    @property
    def owner_db(self) -> Database:
        """Return the database holding owner-wide (cross-project) configuration."""
        return self._owner_db if self._owner_db is not None else self

    def add_event_listener(self, callback: Callable[[str, int], None]) -> None:
        """Register an in-process callback invoked after any event is recorded."""
        with self._listeners_lock:
            if callback not in self._listeners:
                self._listeners.append(callback)

    def remove_event_listener(self, callback: Callable[[str, int], None]) -> None:
        """Unregister an in-process event callback."""
        with self._listeners_lock:
            if callback in self._listeners:
                self._listeners.remove(callback)

    @contextmanager
    def _get_connection(self) -> Iterator[sqlite3.Connection]:
        if self._read_only:
            conn = sqlite3.connect(f"{self.db_path.as_uri()}?mode=ro", uri=True, timeout=10.0)
        else:
            conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        if not self._read_only:
            conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        if self._read_only:
            return
        try:
            with self._get_connection() as conn:
                conn.executescript(
                    """
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    task TEXT NOT NULL,
                    target_repo TEXT NOT NULL,
                    base_commit TEXT NOT NULL,
                    tier TEXT NOT NULL,
                    training_allowed INTEGER NOT NULL,
                    gate_command TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    final_attempt_id TEXT,
                    result_summary TEXT,
                    config_json TEXT,
                    review_state_json TEXT,
                    -- C13-F: frozen, JSON-serializable review-necessity decision.
                    -- Missing means "Run predates C13-F"; reconstruct conservatively.
                    review_risk_decision_json TEXT,
                    routing_snapshot_json TEXT,
                    control_plane_snapshot_json TEXT,
                    control_plane_snapshot_digest TEXT,
                    tool_contract_json TEXT,
                    tool_contract_digest TEXT,
                    project_id TEXT,
                    project_identity_json TEXT,
                    objective_id TEXT,
                    gateway_snapshot_json TEXT,
                    gateway_snapshot_digest TEXT
                );

                CREATE TABLE IF NOT EXISTS attempts (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    attempt_number INTEGER NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    worktree_path TEXT NOT NULL,
                    branch_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    worker_stdout TEXT NOT NULL DEFAULT '',
                    worker_stderr TEXT NOT NULL DEFAULT '',
                    worker_error TEXT,
                    commit_sha TEXT,
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    project_id TEXT,
                    objective_id TEXT,
                    base_sha TEXT,
                    interruption_reason TEXT
                );

                CREATE TABLE IF NOT EXISTS checks (
                    id TEXT PRIMARY KEY,
                    attempt_id TEXT NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
                    gate_command TEXT NOT NULL,
                    exit_code INTEGER NOT NULL,
                    stdout TEXT NOT NULL,
                    stderr TEXT NOT NULL,
                    passed INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS reviews (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    verdict TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    details TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    attempt_id TEXT,
                    event_type TEXT NOT NULL,
                    payload_json TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS quota_state (
                    provider TEXT NOT NULL,
                    pool TEXT NOT NULL,
                    remaining_percent REAL NOT NULL,
                    reset_at TEXT,
                    routing_state TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (provider, pool)
                );

                CREATE INDEX IF NOT EXISTS idx_attempts_run_id ON attempts(run_id);
                CREATE INDEX IF NOT EXISTS idx_checks_attempt_id ON checks(attempt_id);
                CREATE INDEX IF NOT EXISTS idx_reviews_run_id ON reviews(run_id);
                CREATE INDEX IF NOT EXISTS idx_events_run_id ON events(run_id);

                CREATE TABLE IF NOT EXISTS run_plans (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS plan_steps (
                    id TEXT PRIMARY KEY,
                    plan_id TEXT NOT NULL REFERENCES run_plans(id) ON DELETE CASCADE,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    status TEXT NOT NULL,
                    ordering INTEGER NOT NULL,
                    dependency_ids_json TEXT NOT NULL DEFAULT '[]',
                    work_package_id TEXT,
                    attempt_id TEXT,
                    review_id TEXT,
                    blocker TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS work_packages (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    plan_step_id TEXT NOT NULL REFERENCES plan_steps(id) ON DELETE CASCADE,
                    parent_objective TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    acceptance_criteria_json TEXT NOT NULL DEFAULT '[]',
                    dependency_package_ids_json TEXT NOT NULL DEFAULT '[]',
                    base_candidate_sha TEXT,
                    constraints_json TEXT NOT NULL DEFAULT '[]',
                    relevant_paths_json TEXT NOT NULL DEFAULT '[]',
                    prerequisite_summaries_json TEXT NOT NULL DEFAULT '[]',
                    gate_required INTEGER NOT NULL DEFAULT 1,
                    capability_hint TEXT,
                    tier_hint TEXT,
                    long_running_hint INTEGER,
                    long_running INTEGER NOT NULL DEFAULT 0,
                    long_running_reason TEXT,
                    context_capsule_json TEXT,
                    scope_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS supervision (
                    run_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
                    supervisor_identity TEXT,
                    supervisor_state TEXT,
                    worker_pid INTEGER,
                    worker_identity TEXT,
                    provider TEXT,
                    model TEXT,
                    started_at TEXT,
                    last_activity_at TEXT,
                    health_state TEXT NOT NULL DEFAULT 'uncertain',
                    health_reason TEXT,
                    current_package_id TEXT,
                    current_attempt_id TEXT,
                    deadline_at TEXT,
                    transcript_path TEXT,
                    wake_condition TEXT,
                    manager_state TEXT NOT NULL DEFAULT 'ATTACHED',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS execution_owners (
                    run_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
                    project_id TEXT,
                    owner_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    host TEXT NOT NULL,
                    boot_id TEXT,
                    pid INTEGER NOT NULL,
                    start_ticks INTEGER,
                    session_id INTEGER,
                    process_group_id INTEGER,
                    claimed_at TEXT NOT NULL,
                    heartbeat_at TEXT,
                    released_at TEXT,
                    release_reason TEXT,
                    cancel_agent_json TEXT
                );
                CREATE TABLE IF NOT EXISTS handoffs (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(run_id, kind, version)
                );
                CREATE INDEX IF NOT EXISTS idx_plans_run_id ON run_plans(run_id);
                CREATE INDEX IF NOT EXISTS idx_plan_steps_run_order ON plan_steps(run_id, ordering);
                CREATE INDEX IF NOT EXISTS idx_packages_run_id ON work_packages(run_id);
                CREATE INDEX IF NOT EXISTS idx_handoffs_run_kind ON handoffs(run_id, kind, version);

                CREATE TABLE IF NOT EXISTS review_findings (
                    id TEXT PRIMARY KEY,
                    review_id TEXT NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    classification TEXT NOT NULL,
                    title TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    contract_reference TEXT,
                    invariant_or_requirement TEXT NOT NULL,
                    code_location TEXT,
                    failure_scenario TEXT NOT NULL,
                    evidence TEXT NOT NULL,
                    minimum_correction TEXT NOT NULL,
                    repair_scope TEXT NOT NULL,
                    blocking INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_review_findings_run_id
                    ON review_findings(run_id);
                CREATE INDEX IF NOT EXISTS idx_review_findings_review_id
                    ON review_findings(review_id);

                CREATE TABLE IF NOT EXISTS model_call_usage (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    association_id TEXT NOT NULL,
                    attempt_id TEXT,
                    review_id TEXT,
                    stage TEXT NOT NULL,
                    tier TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    prompt_bytes INTEGER NOT NULL,
                    transport TEXT NOT NULL,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    cached_tokens INTEGER,
                    total_tokens INTEGER,
                    cache_read_tokens INTEGER,
                    cache_write_tokens INTEGER,
                    reasoning_tokens INTEGER,
                    usage_source TEXT NOT NULL,
                    raw_usage_json TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_model_call_usage_run_id
                    ON model_call_usage(run_id, created_at, id);

                CREATE TABLE IF NOT EXISTS dispatch_evidence (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    association_id TEXT NOT NULL,
                    attempt_id TEXT,
                    review_id TEXT,
                    stage TEXT NOT NULL,
                    role TEXT NOT NULL,
                    tier TEXT NOT NULL,
                    dispatch_reason TEXT NOT NULL,
                    requested_provider TEXT NOT NULL,
                    requested_model TEXT NOT NULL,
                    actual_provider TEXT,
                    actual_model TEXT,
                    package_id TEXT,
                    plan_step_id TEXT,
                    base_sha TEXT,
                    candidate_sha TEXT,
                    inherited_checkpoint_sha TEXT,
                    context_mode TEXT,
                    handoff_id TEXT,
                    prompt_bytes INTEGER,
                    prompt_sha256 TEXT,
                    prompt_sections_json TEXT,
                    raw_parent_included INTEGER,
                    validation_policy_occurrence_count INTEGER,
                    prompt_transport TEXT,
                    tool_transport TEXT,
                    authority_mode TEXT,
                    routing_snapshot_digest TEXT,
                    selected_candidate TEXT,
                    prior_failure_refs_json TEXT,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    dispatch_outcome TEXT,
                    interruption_reason TEXT,
                    interruption_timestamp TEXT,
                    handoff_bytes INTEGER,
                    replacement_attempt_id TEXT,
                    time_to_resume_seconds REAL,
                    tool_schema_bytes INTEGER,
                    tool_description_bytes INTEGER,
                    tool_discovery_bytes INTEGER,
                    tool_result_bytes INTEGER,
                    tools_exposed_count INTEGER,
                    tools_actually_used_count INTEGER,
                    control_plane_digest TEXT,
                    gateway_evidence_json TEXT,
                    binding_id TEXT,
                    profile_id TEXT,
                    quota_pool_id TEXT,
                    reasoning_effort TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_dispatch_evidence_run
                    ON dispatch_evidence(run_id, started_at, id);
                CREATE INDEX IF NOT EXISTS idx_dispatch_evidence_attempt
                    ON dispatch_evidence(attempt_id);

                CREATE TABLE IF NOT EXISTS quota_observations (
                    id TEXT PRIMARY KEY,
                    run_id TEXT REFERENCES runs(id) ON DELETE CASCADE,
                    provider TEXT NOT NULL,
                    account_ref TEXT,
                    pool TEXT,
                    window_type TEXT,
                    raw_value REAL,
                    raw_limit REAL,
                    raw_semantics TEXT NOT NULL,
                    normalized_remaining_pct REAL,
                    reset_at TEXT,
                    observed_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    freshness TEXT,
                    confidence TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_quota_observations_run
                    ON quota_observations(run_id, observed_at, id);

                CREATE TABLE IF NOT EXISTS worker_checkpoints (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    source_attempt_id TEXT NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
                    source_base_sha TEXT NOT NULL,
                    checkpoint_sha TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    changed_paths_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    handoff_id TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_worker_checkpoints_run_id
                    ON worker_checkpoints(run_id);
                CREATE INDEX IF NOT EXISTS idx_worker_checkpoints_sha
                    ON worker_checkpoints(checkpoint_sha);

                CREATE TABLE IF NOT EXISTS worker_activations (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    attempt_id TEXT NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
                    generation INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    worker_identity_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(run_id, generation)
                );
                CREATE INDEX IF NOT EXISTS idx_worker_activations_run_id
                    ON worker_activations(run_id);

                CREATE TABLE IF NOT EXISTS side_effect_journal (
                    idempotency_key TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    attempt_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    state TEXT NOT NULL,
                    payload_json TEXT,
                    result_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_side_effects_run_id
                    ON side_effect_journal(run_id);

                -- C11-C: structured execution trace + tool-effect evidence.
                -- ``tool_effects`` is the bounded, secret-free row per
                -- recorded tool operation; ``attempt_traces`` is the per-
                -- Attempt summary row carrying the TraceCapability class
                -- the runtime established before semantic execution.  Both
                -- are additive evidence; no legacy column is rewritten.
                CREATE TABLE IF NOT EXISTS tool_effects (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    attempt_id TEXT NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
                    operation TEXT NOT NULL,
                    capability TEXT,
                    resource_ref TEXT,
                    side_effect_class TEXT NOT NULL,
                    status TEXT NOT NULL,
                    idempotency_key TEXT,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    result_class TEXT,
                    result_redacted TEXT,
                    reconciliation_status TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tool_effects_attempt
                    ON tool_effects(attempt_id, started_at, id);
                CREATE INDEX IF NOT EXISTS idx_tool_effects_run
                    ON tool_effects(run_id, started_at, id);

                CREATE TABLE IF NOT EXISTS attempt_traces (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    attempt_id TEXT NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
                    trace_class TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    tool_effects_count INTEGER NOT NULL DEFAULT 0,
                    summary_json TEXT,
                    failure_kind TEXT,
                    failure_reason TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_attempt_traces_attempt
                    ON attempt_traces(attempt_id, started_at, id);
                CREATE INDEX IF NOT EXISTS idx_attempt_traces_run
                    ON attempt_traces(run_id, started_at, id);

                -- C14-C: production phase telemetry (ROADMAP §11 wall-clock
                -- decomposition).  This table is purely observational: no
                -- production code path, gate policy, routing decision,
                -- review decision, lifecycle transition, or scheduler
                -- reads from it.  ``status`` is the honest measurement
                -- status (MEASURED / UNKNOWN / NOT_APPLICABLE) so an
                -- unobservable phase can never be silently coerced into a
                -- numeric duration.  ``source_kind`` records the exact
                -- boundary that produced the measurement (orchestrator
                -- core / review engine / etc.), ``boundary_kind`` records
                -- the sub-boundary (e.g. ``create_run`` for LEDGER_DB,
                -- ``gate_run`` for DETERMINISTIC_VALIDATION).  The
                -- monotonic start/end anchors are persisted so the
                -- measurement is independently re-derivable from durable
                -- state.
                CREATE TABLE IF NOT EXISTS phase_observations (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    phase TEXT NOT NULL,
                    status TEXT NOT NULL,
                    duration_seconds REAL,
                    source_kind TEXT NOT NULL,
                    boundary_kind TEXT,
                    attempt_id TEXT,
                    review_id TEXT,
                    package_id TEXT,
                    plan_step_id TEXT,
                    started_monotonic REAL,
                    ended_monotonic REAL,
                    observed_at TEXT NOT NULL,
                    notes TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_phase_observations_run
                    ON phase_observations(run_id, observed_at, id);

                -- C09: run-scoped frozen project graph and scope evidence. The
                -- initial graph/scope are immutable once written; expansions
                -- live in scope_expansions and the current scope column is the
                -- only field that changes afterwards.
                CREATE TABLE IF NOT EXISTS run_project_scope (
                    run_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
                    repo_root TEXT NOT NULL,
                    project_graph_json TEXT NOT NULL,
                    project_graph_digest TEXT NOT NULL,
                    initial_scope_json TEXT NOT NULL,
                    initial_scope_digest TEXT NOT NULL,
                    current_scope_json TEXT NOT NULL,
                    current_scope_digest TEXT NOT NULL,
                    analyzer TEXT NOT NULL,
                    graph_source TEXT NOT NULL,
                    confidence TEXT NOT NULL,
                    base_revision TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS scope_expansions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    attempt_id TEXT,
                    path TEXT,
                    symbol TEXT,
                    reason TEXT NOT NULL,
                    before_digest TEXT NOT NULL,
                    after_digest TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_scope_expansions_run_id
                    ON scope_expansions(run_id);

                -- C11: the durable Project Ledger.  Project-level truth lives
                -- here and nowhere else: Orch sessions are disposable, so no
                -- authoritative record may depend on a live process, a
                -- provider session, or a conversation transcript.  Accepted
                -- rows are never rewritten; they are superseded or retired,
                -- and every authoritative mutation appends one durable event.
                CREATE TABLE IF NOT EXISTS project_ledger (
                    project_id TEXT PRIMARY KEY,
                    canonical_path TEXT NOT NULL,
                    remote_identity TEXT NOT NULL DEFAULT '',
                    root_commit TEXT NOT NULL DEFAULT '',
                    origin TEXT NOT NULL,
                    genesis_json TEXT NOT NULL,
                    project_version INTEGER NOT NULL,
                    schema_version INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS project_facts (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL
                        REFERENCES project_ledger(project_id) ON DELETE CASCADE,
                    category TEXT NOT NULL,
                    statement TEXT NOT NULL,
                    reference TEXT NOT NULL DEFAULT '',
                    origin TEXT NOT NULL,
                    status TEXT NOT NULL,
                    provenance_json TEXT NOT NULL,
                    supersedes TEXT,
                    superseded_by TEXT,
                    introduced_version INTEGER NOT NULL,
                    introduced_at TEXT NOT NULL,
                    retired_version INTEGER,
                    retired_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_project_facts_project
                    ON project_facts(project_id);

                CREATE TABLE IF NOT EXISTS project_decisions (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL
                        REFERENCES project_ledger(project_id) ON DELETE CASCADE,
                    statement TEXT NOT NULL,
                    rationale TEXT NOT NULL DEFAULT '',
                    reference TEXT NOT NULL DEFAULT '',
                    origin TEXT NOT NULL,
                    status TEXT NOT NULL,
                    provenance_json TEXT NOT NULL,
                    supersedes TEXT,
                    superseded_by TEXT,
                    introduced_version INTEGER NOT NULL,
                    introduced_at TEXT NOT NULL,
                    retired_version INTEGER,
                    retired_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_project_decisions_project
                    ON project_decisions(project_id);

                CREATE TABLE IF NOT EXISTS project_roadmap_items (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL
                        REFERENCES project_ledger(project_id) ON DELETE CASCADE,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    ordering INTEGER NOT NULL,
                    depends_on_json TEXT NOT NULL DEFAULT '[]',
                    acceptance_ref TEXT NOT NULL DEFAULT '',
                    result_ref TEXT NOT NULL DEFAULT '',
                    run_ref TEXT,
                    origin TEXT NOT NULL,
                    provenance_json TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    introduced_version INTEGER NOT NULL,
                    introduced_at TEXT NOT NULL,
                    updated_version INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_project_roadmap_project
                    ON project_roadmap_items(project_id);

                CREATE TABLE IF NOT EXISTS project_open_questions (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL
                        REFERENCES project_ledger(project_id) ON DELETE CASCADE,
                    question TEXT NOT NULL,
                    status TEXT NOT NULL,
                    resolution TEXT NOT NULL DEFAULT '',
                    resolution_ref TEXT NOT NULL DEFAULT '',
                    origin TEXT NOT NULL,
                    provenance_json TEXT NOT NULL,
                    introduced_version INTEGER NOT NULL,
                    introduced_at TEXT NOT NULL,
                    resolved_version INTEGER,
                    resolved_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_project_open_questions_project
                    ON project_open_questions(project_id);

                CREATE TABLE IF NOT EXISTS project_change_intake (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL
                        REFERENCES project_ledger(project_id) ON DELETE CASCADE,
                    summary TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    origin TEXT NOT NULL,
                    disposition TEXT NOT NULL,
                    linked_fact_id TEXT,
                    linked_decision_id TEXT,
                    linked_roadmap_id TEXT,
                    provenance_json TEXT NOT NULL,
                    recorded_version INTEGER NOT NULL,
                    recorded_at TEXT NOT NULL,
                    decided_version INTEGER,
                    decided_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_project_change_intake_project
                    ON project_change_intake(project_id);

                CREATE TABLE IF NOT EXISTS project_ledger_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    subject_kind TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    project_version INTEGER NOT NULL,
                    state_digest TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_project_ledger_events_project
                    ON project_ledger_events(project_id, project_version);

                CREATE TABLE IF NOT EXISTS project_checkpoints (
                    checkpoint_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    parent_checkpoint_id TEXT,
                    project_version INTEGER NOT NULL,
                    state_digest TEXT NOT NULL,
                    checkpoint_digest TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    manifest_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_project_checkpoints_project
                    ON project_checkpoints(project_id, project_version);
                """
                )
                # Additive, non-destructive migration for databases created before the
                # per-run effective config column existed. New databases already include
                # it via the CREATE TABLE statement above.
                columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(runs)")}
                if "config_json" not in columns:
                    conn.execute("ALTER TABLE runs ADD COLUMN config_json TEXT")
                if "review_state_json" not in columns:
                    conn.execute("ALTER TABLE runs ADD COLUMN review_state_json TEXT")
                # C13-F additive migration: frozen review-necessity decision.
                # Existing rows keep NULL and remain readable; reconstruction
                # falls back to the conservative decision.
                if "review_risk_decision_json" not in columns:
                    conn.execute(
                        "ALTER TABLE runs ADD COLUMN review_risk_decision_json TEXT"
                    )
                if "routing_snapshot_json" not in columns:
                    conn.execute("ALTER TABLE runs ADD COLUMN routing_snapshot_json TEXT")
                if "control_plane_snapshot_json" not in columns:
                    conn.execute("ALTER TABLE runs ADD COLUMN control_plane_snapshot_json TEXT")
                if "control_plane_snapshot_digest" not in columns:
                    conn.execute("ALTER TABLE runs ADD COLUMN control_plane_snapshot_digest TEXT")
                if "tool_contract_json" not in columns:
                    conn.execute("ALTER TABLE runs ADD COLUMN tool_contract_json TEXT")
                if "tool_contract_digest" not in columns:
                    conn.execute("ALTER TABLE runs ADD COLUMN tool_contract_digest TEXT")
                # C10 additive migration: gateway snapshot columns. Existing
                # databases keep NULL and remain readable; legacy runs are
                # never re-classified by gateway semantics.
                if "gateway_snapshot_json" not in columns:
                    conn.execute("ALTER TABLE runs ADD COLUMN gateway_snapshot_json TEXT")
                if "gateway_snapshot_digest" not in columns:
                    conn.execute("ALTER TABLE runs ADD COLUMN gateway_snapshot_digest TEXT")
                dispatch_columns = {
                    str(row["name"]) for row in conn.execute("PRAGMA table_info(dispatch_evidence)")
                }
                if dispatch_columns and "control_plane_digest" not in dispatch_columns:
                    conn.execute(
                        "ALTER TABLE dispatch_evidence ADD COLUMN control_plane_digest TEXT"
                    )
                # C10 additive migration: per-dispatch gateway evidence.
                if dispatch_columns and "gateway_evidence_json" not in dispatch_columns:
                    conn.execute(
                        "ALTER TABLE dispatch_evidence ADD COLUMN gateway_evidence_json TEXT"
                    )
                # C11-D additive migration: exact binding identity and
                # resolved reasoning effort on the durable dispatch row.
                for _c11d_col in (
                    "binding_id",
                    "profile_id",
                    "quota_pool_id",
                    "reasoning_effort",
                ):
                    if dispatch_columns and _c11d_col not in dispatch_columns:
                        conn.execute(
                            f"ALTER TABLE dispatch_evidence ADD COLUMN {_c11d_col} TEXT"
                        )
                # Project/objective provenance.  Existing rows keep NULL and stay
                # readable; they are never retro-attached to a project.
                for column in ("project_id", "project_identity_json", "objective_id"):
                    if column not in columns:
                        conn.execute(f"ALTER TABLE runs ADD COLUMN {column} TEXT")
                attempt_columns = {
                    str(row["name"]) for row in conn.execute("PRAGMA table_info(attempts)")
                }
                for column in ("project_id", "objective_id", "base_sha", "interruption_reason"):
                    if column not in attempt_columns:
                        conn.execute(f"ALTER TABLE attempts ADD COLUMN {column} TEXT")
                owner_columns = {
                    str(row["name"]) for row in conn.execute("PRAGMA table_info(execution_owners)")
                }
                if owner_columns and "cancel_agent_json" not in owner_columns:
                    conn.execute("ALTER TABLE execution_owners ADD COLUMN cancel_agent_json TEXT")
                # Additive migration: supervision.worker_identity added in Correction 2.
                # Existing rows keep NULL and remain readable; no data is destroyed.
                try:
                    sup_columns = {
                        str(r["name"]) for r in conn.execute("PRAGMA table_info(supervision)")
                    }
                    if sup_columns and "worker_identity" not in sup_columns:
                        conn.execute("ALTER TABLE supervision ADD COLUMN worker_identity TEXT")
                except Exception:
                    # supervision table may not exist yet in very old databases.
                    pass
                # C09 additive migration: work_packages.scope_json. Existing
                # rows keep NULL and stay readable; no data is destroyed.
                try:
                    wp_columns = {
                        str(r["name"]) for r in conn.execute("PRAGMA table_info(work_packages)")
                    }
                    if wp_columns and "scope_json" not in wp_columns:
                        conn.execute("ALTER TABLE work_packages ADD COLUMN scope_json TEXT")
                except Exception:
                    # work_packages may not exist yet in very old databases.
                    pass
                conn.commit()
        except sqlite3.OperationalError as exc:
            if "readonly" not in str(exc).lower():
                raise
            # Read-only legacy databases remain inspectable.  New 3B writes
            # correctly fail closed if requested, but read-only commands do not
            # mutate a user's historical state merely to inspect it.
            self._plan_schema_available = False

    def create_run(self, run: Run) -> None:
        """Insert a new run together with its project/objective provenance."""
        with self._get_connection() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO runs (
                        id, task, target_repo, base_commit, tier,
                        training_allowed, gate_command, status, created_at,
                        completed_at, final_attempt_id, result_summary, config_json,
                        review_state_json, review_risk_decision_json,
                        routing_snapshot_json,
                        control_plane_snapshot_json, control_plane_snapshot_digest,
                        tool_contract_json, tool_contract_digest,
                        project_id, project_identity_json, objective_id,
                        gateway_snapshot_json, gateway_snapshot_digest
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run.id,
                        run.task,
                        run.target_repo,
                        run.base_commit,
                        run.tier.value,
                        1 if run.training_allowed else 0,
                        run.gate_command,
                        run.status.value,
                        run.created_at,
                        run.completed_at,
                        run.final_attempt_id,
                        run.result_summary,
                        run.config_json,
                        run.review_state_json,
                        getattr(run, "review_risk_decision_json", None),
                        run.routing_snapshot_json,
                        run.control_plane_snapshot_json,
                        run.control_plane_snapshot_digest,
                        run.tool_contract_json,
                        run.tool_contract_digest,
                        run.project_id,
                        run.project_identity_json,
                        run.objective_id,
                        getattr(run, "gateway_snapshot_json", None),
                        getattr(run, "gateway_snapshot_digest", None),
                    ),
                )
            except sqlite3.OperationalError as provenance_exc:
                if not _is_missing_column(
                    provenance_exc,
                    (
                        "control_plane_snapshot_json",
                        "control_plane_snapshot_digest",
                        "project_id",
                        "project_identity_json",
                        "objective_id",
                    ),
                ):
                    raise
                self._insert_run_pre_provenance(conn, run)
            conn.commit()

    def _insert_run_pre_provenance(self, conn: sqlite3.Connection, run: Run) -> None:
        """Insert into a database predating the provenance columns.

        Legacy state is preserved rather than migrated destructively, so a
        read-only or otherwise un-migratable historical database still accepts
        writes for the columns it does have.
        """
        try:
            conn.execute(
                """
                    INSERT INTO runs (
                        id, task, target_repo, base_commit, tier,
                        training_allowed, gate_command, status, created_at,
                        completed_at, final_attempt_id, result_summary, config_json,
                        review_state_json, routing_snapshot_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                (
                    run.id,
                    run.task,
                    run.target_repo,
                    run.base_commit,
                    run.tier.value,
                    1 if run.training_allowed else 0,
                    run.gate_command,
                    run.status.value,
                    run.created_at,
                    run.completed_at,
                    run.final_attempt_id,
                    run.result_summary,
                    run.config_json,
                    run.review_state_json,
                    run.routing_snapshot_json,
                ),
            )
        except sqlite3.OperationalError as exc:
            if "routing_snapshot_json" in str(exc):
                try:
                    conn.execute(
                        """
                        INSERT INTO runs (
                            id, task, target_repo, base_commit, tier,
                            training_allowed, gate_command, status, created_at,
                            completed_at, final_attempt_id, result_summary, config_json,
                            review_state_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run.id,
                            run.task,
                            run.target_repo,
                            run.base_commit,
                            run.tier.value,
                            1 if run.training_allowed else 0,
                            run.gate_command,
                            run.status.value,
                            run.created_at,
                            run.completed_at,
                            run.final_attempt_id,
                            run.result_summary,
                            run.config_json,
                            run.review_state_json,
                        ),
                    )
                except sqlite3.OperationalError as exc2:
                    if "review_state_json" in str(exc2):
                        conn.execute(
                            """
                            INSERT INTO runs (
                                id, task, target_repo, base_commit, tier,
                                training_allowed, gate_command, status, created_at,
                                completed_at, final_attempt_id, result_summary, config_json
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                run.id,
                                run.task,
                                run.target_repo,
                                run.base_commit,
                                run.tier.value,
                                1 if run.training_allowed else 0,
                                run.gate_command,
                                run.status.value,
                                run.created_at,
                                run.completed_at,
                                run.final_attempt_id,
                                run.result_summary,
                                run.config_json,
                            ),
                        )
                    else:
                        raise
            elif "review_state_json" in str(exc):
                conn.execute(
                    """
                    INSERT INTO runs (
                        id, task, target_repo, base_commit, tier,
                        training_allowed, gate_command, status, created_at,
                        completed_at, final_attempt_id, result_summary, config_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run.id,
                        run.task,
                        run.target_repo,
                        run.base_commit,
                        run.tier.value,
                        1 if run.training_allowed else 0,
                        run.gate_command,
                        run.status.value,
                        run.created_at,
                        run.completed_at,
                        run.final_attempt_id,
                        run.result_summary,
                        run.config_json,
                    ),
                )
        else:
            raise

    def get_run(self, run_id: str) -> Run | None:
        """Fetch a run by ID."""
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,))
            row = cursor.fetchone()
            if row is None:
                return None
            # Additive columns: handle legacy rows without new fields
            try:
                review_state = str(row["review_state_json"]) if row["review_state_json"] else None
            except (KeyError, IndexError, sqlite3.OperationalError):
                review_state = None
            try:
                config_val = str(row["config_json"]) if row["config_json"] else None
            except (KeyError, IndexError, sqlite3.OperationalError):
                config_val = None
            try:
                raw_routing = row["routing_snapshot_json"]
                routing_val = str(raw_routing) if raw_routing is not None else None
            except (KeyError, IndexError, sqlite3.OperationalError):
                routing_val = None
            try:
                raw_decision = row["review_risk_decision_json"]
                decision_val = str(raw_decision) if raw_decision is not None else None
            except (KeyError, IndexError, sqlite3.OperationalError):
                decision_val = None
            return Run(
                id=str(row["id"]),
                task=str(row["task"]),
                target_repo=str(row["target_repo"]),
                base_commit=str(row["base_commit"]),
                tier=Tier(str(row["tier"])),
                training_allowed=bool(row["training_allowed"]),
                gate_command=str(row["gate_command"]),
                status=RunStatus(str(row["status"])),
                created_at=str(row["created_at"]),
                completed_at=str(row["completed_at"]) if row["completed_at"] else None,
                final_attempt_id=str(row["final_attempt_id"]) if row["final_attempt_id"] else None,
                result_summary=str(row["result_summary"]) if row["result_summary"] else None,
                config_json=config_val,
                review_state_json=review_state,
                review_risk_decision_json=decision_val,
                routing_snapshot_json=routing_val,
                control_plane_snapshot_json=_optional_text(row, "control_plane_snapshot_json"),
                control_plane_snapshot_digest=_optional_text(row, "control_plane_snapshot_digest"),
                tool_contract_json=_optional_text(row, "tool_contract_json"),
                tool_contract_digest=_optional_text(row, "tool_contract_digest"),
                project_id=_optional_text(row, "project_id"),
                project_identity_json=_optional_text(row, "project_identity_json"),
                objective_id=_optional_text(row, "objective_id"),
                gateway_snapshot_json=_optional_text(row, "gateway_snapshot_json"),
                gateway_snapshot_digest=_optional_text(row, "gateway_snapshot_digest"),
            )

    def update_run(
        self,
        run_id: str,
        status: RunStatus,
        completed_at: str | None = None,
        final_attempt_id: str | None = None,
        result_summary: str | None = None,
    ) -> None:
        """Update run status and completion details."""
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE runs
                SET status = ?, completed_at = ?, final_attempt_id = ?, result_summary = ?
                WHERE id = ?
                """,
                (status.value, completed_at, final_attempt_id, result_summary, run_id),
            )
            conn.commit()

    def update_run_tier(self, run_id: str, tier: Tier) -> None:
        """Persist the concrete tier currently executing for an auto-routed run."""
        with self._get_connection() as conn:
            conn.execute("UPDATE runs SET tier = ? WHERE id = ?", (tier.value, run_id))
            conn.commit()

    def update_review_loop_state(
        self,
        run_id: str,
        current_attempt: int,
        is_final: bool,
        terminal_verdict: str | None = None,
    ) -> None:
        """Persist per-run review loop state additively.

        Stores ``current_attempt`` (1-indexed), ``is_final`` flag, and
        optional ``terminal_verdict`` when a terminal review state is reached.
        The JSON payload is durable across Database reopens and legacy rows
        without the column keep working (NULL is returned for missing state).
        """
        payload: dict[str, Any] = {
            "current_attempt": int(current_attempt),
            "is_final": bool(is_final),
            "updated_at": current_iso_timestamp(),
        }
        if terminal_verdict is not None:
            payload["terminal_verdict"] = str(terminal_verdict)
        else:
            payload["terminal_verdict"] = None
        json_str = json.dumps(payload)
        try:
            with self._get_connection() as conn:
                conn.execute(
                    "UPDATE runs SET review_state_json = ? WHERE id = ?",
                    (json_str, run_id),
                )
                conn.commit()
        except sqlite3.OperationalError as exc:
            if "review_state_json" in str(exc):
                # Auto-migrate legacy DB missing the column
                with self._get_connection() as conn:
                    cols = {str(r["name"]) for r in conn.execute("PRAGMA table_info(runs)")}
                    if "review_state_json" not in cols:
                        conn.execute("ALTER TABLE runs ADD COLUMN review_state_json TEXT")
                    conn.execute(
                        "UPDATE runs SET review_state_json = ? WHERE id = ?",
                        (json_str, run_id),
                    )
                    conn.commit()
            else:
                raise

    def get_review_loop_state(self, run_id: str) -> dict[str, Any] | None:
        """Fetch persisted review loop state, or None for legacy/uninitialized rows.

        Reconstructable via Database reads; returns the JSON payload previously
        stored by :meth:`update_review_loop_state`.
        """
        try:
            with self._get_connection() as conn:
                row = conn.execute(
                    "SELECT review_state_json FROM runs WHERE id = ?", (run_id,)
                ).fetchone()
                if row is None:
                    return None
                try:
                    raw = row["review_state_json"]
                except (KeyError, IndexError, sqlite3.OperationalError):
                    return None
                if raw is None or not str(raw).strip():
                    return None
                return json.loads(str(raw))  # type: ignore[no-any-return]
        except sqlite3.OperationalError as exc:
            if "review_state_json" in str(exc):
                return None
            raise

    def update_review_risk_decision(self, run_id: str, json_str: str | None) -> None:
        """Persist the frozen C13-F review-necessity decision for ``run_id``.

        Idempotent overwrite; an empty / ``None`` payload deletes the
        decision (which forces conservative reconstruction on the next
        read).  The column is added by an additive migration when
        missing, mirroring the existing ``review_state_json`` pattern.
        """
        value: str | None = json_str if json_str is not None else None
        try:
            with self._get_connection() as conn:
                conn.execute(
                    "UPDATE runs SET review_risk_decision_json = ? WHERE id = ?",
                    (value, run_id),
                )
                conn.commit()
        except sqlite3.OperationalError as exc:
            if "review_risk_decision_json" in str(exc):
                with self._get_connection() as conn:
                    cols = {str(r["name"]) for r in conn.execute("PRAGMA table_info(runs)")}
                    if "review_risk_decision_json" not in cols:
                        conn.execute(
                            "ALTER TABLE runs ADD COLUMN review_risk_decision_json TEXT"
                        )
                    conn.execute(
                        "UPDATE runs SET review_risk_decision_json = ? WHERE id = ?",
                        (value, run_id),
                    )
                    conn.commit()
            else:
                raise

    def get_review_risk_decision_json(self, run_id: str) -> str | None:
        """Return the persisted C13-F review-necessity decision JSON, or ``None``.

        ``None`` for legacy rows predating C13-F and for any row whose
        decision was deleted by a subsequent ``update_review_risk_decision``.
        """
        try:
            with self._get_connection() as conn:
                row = conn.execute(
                    "SELECT review_risk_decision_json FROM runs WHERE id = ?", (run_id,)
                ).fetchone()
                if row is None:
                    return None
                try:
                    raw = row["review_risk_decision_json"]
                except (KeyError, IndexError, sqlite3.OperationalError):
                    return None
                if raw is None or not str(raw).strip():
                    return None
                return str(raw)
        except sqlite3.OperationalError as exc:
            if "review_risk_decision_json" in str(exc):
                return None
            raise

    def adopt_run_if_pending(self, run_id: str) -> bool:
        """Atomically transition a run from PENDING to RUNNING if and only if currently PENDING."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                UPDATE runs
                SET status = ?
                WHERE id = ? AND status = ?
                """,
                (RunStatus.RUNNING.value, run_id, RunStatus.PENDING.value),
            )
            conn.commit()
            return cursor.rowcount > 0

    def fail_run_if_nonterminal(
        self,
        run_id: str,
        completed_at: str | None = None,
        result_summary: str | None = None,
    ) -> bool:
        """Atomically transition a run to FAILED only if currently in a nonterminal status."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                UPDATE runs
                SET status = ?, completed_at = ?, result_summary = ?
                WHERE id = ? AND status NOT IN (?, ?, ?)
                """,
                (
                    RunStatus.FAILED.value,
                    completed_at,
                    result_summary,
                    run_id,
                    RunStatus.COMPLETED.value,
                    RunStatus.FAILED.value,
                    RunStatus.ORPHANED.value,
                ),
            )
            conn.commit()
            return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # Durable execution ownership
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_execution_owner(row: sqlite3.Row) -> ExecutionOwner:
        return ExecutionOwner(
            run_id=str(row["run_id"]),
            project_id=_optional_text(row, "project_id"),
            owner_id=str(row["owner_id"]),
            generation=int(row["generation"]),
            state=str(row["state"]),
            identity=ProcessIdentity(
                host=str(row["host"]),
                boot_id=_optional_text(row, "boot_id"),
                pid=int(row["pid"]),
                start_ticks=None if row["start_ticks"] is None else int(row["start_ticks"]),
                session_id=None if row["session_id"] is None else int(row["session_id"]),
                process_group_id=(
                    None if row["process_group_id"] is None else int(row["process_group_id"])
                ),
            ),
            claimed_at=str(row["claimed_at"]),
            heartbeat_at=_optional_text(row, "heartbeat_at"),
            released_at=_optional_text(row, "released_at"),
            release_reason=_optional_text(row, "release_reason"),
            cancel_agent=_decode_process_identity(_optional_text(row, "cancel_agent_json")),
        )

    def get_execution_owner(self, run_id: str) -> ExecutionOwner | None:
        """Return the durable execution-owner record for a run, if any.

        ``None`` means "this store holds no ownership row" -- either the run was
        never claimed, or the database genuinely predates the table.  Any other
        failure (locked, corrupt, unreadable) raises
        :class:`OwnershipStoreError`: an unreadable store is never evidence that
        a run has no owner.
        """
        try:
            with self._get_connection() as conn:
                row = conn.execute(
                    "SELECT * FROM execution_owners WHERE run_id = ?", (run_id,)
                ).fetchone()
                return self._row_to_execution_owner(row) if row is not None else None
        except sqlite3.OperationalError as exc:
            if _is_missing_ownership_table(exc):
                return None
            raise OwnershipStoreError(
                f"Cannot read execution ownership for run '{run_id}': {exc}"
            ) from exc
        except sqlite3.DatabaseError as exc:
            raise OwnershipStoreError(
                f"Cannot read execution ownership for run '{run_id}': {exc}"
            ) from exc

    def claim_execution_owner(
        self,
        run_id: str,
        *,
        identity: ProcessIdentity,
        owner_id: str | None = None,
        expect_owner_id: str | None = None,
        expect_generation: int | None = None,
        project_id: str | None = None,
    ) -> ExecutionOwner:
        """Atomically become the single execution owner of ``run_id``.

        The whole decision -- terminal check, cancellation check, incumbent
        liveness, generation bump, write -- happens inside one ``BEGIN
        IMMEDIATE`` transaction, so two independent OS processes racing to own
        the same run cannot both succeed: the loser raises before it can launch
        anything.

        ``expect_owner_id`` **and** ``expect_generation`` together form the
        fencing token handed to a detached owner that was reserved on its
        behalf.  Both must match for the reservation to be adopted, so a stale
        process carrying an old generation can never adopt a newer reservation.
        A half-token adopts nothing and falls back to ordinary contention.
        """
        now = current_iso_timestamp()
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run_row = conn.execute(
                "SELECT status, project_id FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            if run_row is None:
                raise OwnershipError(f"Cannot claim execution of unknown run '{run_id}'")
            status = RunStatus(str(run_row["status"]))
            if status in TERMINAL_RUN_STATUSES:
                raise OwnershipError(
                    f"Cannot claim execution of run '{run_id}': status {status.value} is terminal"
                )
            resolved_project = project_id or _optional_text(run_row, "project_id")
            existing_row = conn.execute(
                "SELECT * FROM execution_owners WHERE run_id = ?", (run_id,)
            ).fetchone()

            claimed_at = now
            if existing_row is None:
                generation = 1
                resolved_owner_id = owner_id or uuid.uuid4().hex
            else:
                incumbent = self._row_to_execution_owner(existing_row)
                if incumbent.is_cancelling:
                    # An authoritative cancellation owns this run until it
                    # finishes or aborts.  Only a cancellation whose agent is
                    # provably gone may be superseded, so an abandoned cancel
                    # cannot wedge the run forever.
                    agent_liveness = probe_liveness(incumbent.cancel_agent)
                    if agent_liveness is not Liveness.DEAD:
                        raise OwnershipConflictError(
                            f"Run '{run_id}' is being cancelled by owner "
                            f"{incumbent.owner_id} generation {incumbent.generation} "
                            f"(agent liveness={agent_liveness.value}); refusing takeover"
                        )
                same_process = (
                    incumbent.identity.host == identity.host
                    and incumbent.identity.pid == identity.pid
                    and incumbent.identity.start_ticks == identity.start_ticks
                    and identity.start_ticks is not None
                )
                # Fencing needs both halves of the token; a matching owner id
                # with a stale generation is not a reservation for us.
                reserved_for_us = (
                    expect_owner_id is not None
                    and expect_generation is not None
                    and expect_owner_id == incumbent.owner_id
                    and expect_generation == incumbent.generation
                )
                if (
                    expect_owner_id is not None or expect_generation is not None
                ) and not reserved_for_us:
                    # Supplying a reservation changes this from an ordinary
                    # re-entrant claim into an exact fenced adoption.  Do not
                    # let a same-process launcher accidentally mask a stale,
                    # partial, or wrong-owner token.
                    raise OwnershipConflictError(
                        f"Run '{run_id}' reservation does not match active owner "
                        f"{incumbent.owner_id} generation {incumbent.generation}"
                    )
                if same_process or reserved_for_us:
                    # Re-entrant claim or adoption of our own reservation.
                    resolved_owner_id = incumbent.owner_id
                    generation = incumbent.generation
                    claimed_at = incumbent.claimed_at
                elif incumbent.is_active:
                    liveness = probe_liveness(incumbent.identity)
                    if liveness is not Liveness.DEAD:
                        raise OwnershipConflictError(
                            f"Run '{run_id}' is already owned by pid "
                            f"{incumbent.identity.pid} on {incumbent.identity.host} "
                            f"(liveness={liveness.value}); refusing a second owner"
                        )
                    generation = incumbent.generation + 1
                    resolved_owner_id = owner_id or uuid.uuid4().hex
                else:
                    generation = incumbent.generation + 1
                    resolved_owner_id = owner_id or uuid.uuid4().hex

            conn.execute(
                """
                INSERT INTO execution_owners (
                    run_id, project_id, owner_id, generation, state, host, boot_id,
                    pid, start_ticks, session_id, process_group_id,
                    claimed_at, heartbeat_at, released_at, release_reason,
                    cancel_agent_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)
                ON CONFLICT(run_id) DO UPDATE SET
                    project_id = excluded.project_id,
                    owner_id = excluded.owner_id,
                    generation = excluded.generation,
                    state = excluded.state,
                    host = excluded.host,
                    boot_id = excluded.boot_id,
                    pid = excluded.pid,
                    start_ticks = excluded.start_ticks,
                    session_id = excluded.session_id,
                    process_group_id = excluded.process_group_id,
                    claimed_at = excluded.claimed_at,
                    heartbeat_at = excluded.heartbeat_at,
                    released_at = NULL,
                    release_reason = NULL,
                    cancel_agent_json = NULL
                """,
                (
                    run_id,
                    resolved_project,
                    resolved_owner_id,
                    generation,
                    ExecutionOwner.CLAIMED,
                    identity.host,
                    identity.boot_id,
                    identity.pid,
                    identity.start_ticks,
                    identity.session_id,
                    identity.process_group_id,
                    claimed_at,
                    now,
                ),
            )
            conn.commit()

        claimed = self.get_execution_owner(run_id)
        if claimed is None:  # pragma: no cover - the row was just committed
            raise OwnershipError(f"Execution ownership for run '{run_id}' vanished after claim")
        return claimed

    def adopt_execution_owner_identity(
        self,
        run_id: str,
        *,
        owner_id: str,
        generation: int,
        identity: ProcessIdentity,
    ) -> bool:
        """Point an existing claim at the detached process that now executes it.

        Fenced by ``owner_id``/``generation`` so a superseded launcher can never
        redirect a newer owner's record.
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                UPDATE execution_owners
                SET host = ?, boot_id = ?, pid = ?, start_ticks = ?, session_id = ?,
                    process_group_id = ?, heartbeat_at = ?
                WHERE run_id = ? AND owner_id = ? AND generation = ? AND state = ?
                """,
                (
                    identity.host,
                    identity.boot_id,
                    identity.pid,
                    identity.start_ticks,
                    identity.session_id,
                    identity.process_group_id,
                    current_iso_timestamp(),
                    run_id,
                    owner_id,
                    generation,
                    ExecutionOwner.CLAIMED,
                ),
            )
            conn.commit()
            return cursor.rowcount > 0

    def release_execution_owner(
        self,
        run_id: str,
        *,
        owner_id: str,
        generation: int,
        reason: str = "released",
    ) -> bool:
        """Release one exact ownership generation, but only while ``CLAIMED``.

        Both ``owner_id`` and ``generation`` are mandatory: releasing by
        ``run_id`` alone would silently relinquish whichever generation happens
        to hold the run *now*, which may be a newer owner that the caller never
        inspected.

        The release is additionally fenced to the ``CLAIMED`` state.  A
        generation that has entered ``CANCELLING`` is owned by the cancellation
        lifecycle and may only be resolved by :meth:`terminalize_run_fenced` or
        :meth:`abort_execution_cancellation` -- a worker that finishes
        concurrently with its own cancellation therefore fails harmlessly here
        (``False``), leaving the durable ``CANCELLING`` fence intact to keep
        blocking takeover until cancellation finalizes atomically.
        """
        if not owner_id or generation is None:
            raise OwnershipError(
                f"release_execution_owner for run '{run_id}' requires both owner_id and generation"
            )
        try:
            with self._get_connection() as conn:
                cursor = conn.execute(
                    """
                    UPDATE execution_owners
                    SET state = ?, released_at = ?, release_reason = ?, cancel_agent_json = NULL
                    WHERE run_id = ? AND owner_id = ? AND generation = ? AND state = ?
                    """,
                    (
                        ExecutionOwner.RELEASED,
                        current_iso_timestamp(),
                        reason,
                        run_id,
                        owner_id,
                        generation,
                        ExecutionOwner.CLAIMED,
                    ),
                )
                conn.commit()
                return cursor.rowcount > 0
        except sqlite3.OperationalError as exc:
            if _is_missing_ownership_table(exc):
                return False
            raise OwnershipStoreError(
                f"Cannot release execution ownership for run '{run_id}': {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Fenced terminalization
    # ------------------------------------------------------------------

    def _fenced_owner_row(
        self,
        conn: sqlite3.Connection,
        run_id: str,
        owner_id: str | None,
        generation: int | None,
        expected_states: tuple[str, ...],
    ) -> bool:
        """Verify, inside the caller's transaction, that ownership is as inspected.

        ``owner_id is None`` asserts the opposite: that no row is asserting
        ownership of this run at all.
        """
        row = conn.execute(
            "SELECT owner_id, generation, state FROM execution_owners WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if owner_id is None or generation is None:
            return row is None or str(row["state"]) not in ExecutionOwner.ACTIVE_STATES
        if row is None:
            return False
        return (
            str(row["owner_id"]) == owner_id
            and int(row["generation"]) == generation
            and str(row["state"]) in expected_states
        )

    def terminalize_run_fenced(
        self,
        run_id: str,
        *,
        owner_id: str | None,
        generation: int | None,
        status: RunStatus,
        result_summary: str,
        attempt_reason: str,
        expected_owner_states: tuple[str, ...] = (ExecutionOwner.CLAIMED,),
        release_reason: str = "released",
        completed_at: str | None = None,
    ) -> dict[str, Any] | None:
        """Terminalize a run, its attempts, and one ownership generation atomically.

        The whole decision commits as a unit under ``BEGIN IMMEDIATE``: the
        inspected owner generation is re-verified, the run is confirmed still
        nonterminal, every RUNNING/PENDING attempt is closed, the run reaches
        ``status``, and exactly the inspected generation is released.

        Returns ``None`` -- having changed nothing -- when ownership moved on or
        the run already reached a terminal state while the caller was deciding.
        That is the fail-closed path: a newer generation is never orphaned,
        never has its attempts failed, and never gets released by a decision
        that was made about its predecessor.
        """
        now = current_iso_timestamp()
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run_row = conn.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
            if run_row is None:
                conn.rollback()
                return None
            if RunStatus(str(run_row["status"])) in TERMINAL_RUN_STATUSES:
                conn.rollback()
                return None
            if not self._fenced_owner_row(
                conn, run_id, owner_id, generation, expected_owner_states
            ):
                conn.rollback()
                return None

            attempt_rows = conn.execute(
                "SELECT id FROM attempts WHERE run_id = ? AND status IN (?, ?)",
                (run_id, AttemptStatus.RUNNING.value, AttemptStatus.PENDING.value),
            ).fetchall()
            failed_attempts = [str(row["id"]) for row in attempt_rows]
            if failed_attempts:
                conn.execute(
                    """
                    UPDATE attempts
                    SET status = ?, worker_error = ?, completed_at = ?
                    WHERE run_id = ? AND status IN (?, ?)
                    """,
                    (
                        AttemptStatus.FAILED.value,
                        attempt_reason,
                        now,
                        run_id,
                        AttemptStatus.RUNNING.value,
                        AttemptStatus.PENDING.value,
                    ),
                )
            conn.execute(
                "UPDATE runs SET status = ?, result_summary = ?, completed_at = ? WHERE id = ?",
                (status.value, result_summary, completed_at, run_id),
            )
            if owner_id is not None and generation is not None:
                conn.execute(
                    """
                    UPDATE execution_owners
                    SET state = ?, released_at = ?, release_reason = ?, cancel_agent_json = NULL
                    WHERE run_id = ? AND owner_id = ? AND generation = ?
                    """,
                    (
                        ExecutionOwner.RELEASED,
                        now,
                        release_reason,
                        run_id,
                        owner_id,
                        generation,
                    ),
                )
            conn.commit()
        return {
            "run_id": run_id,
            "status": status.value,
            "failed_attempts": failed_attempts,
            "owner_id": owner_id,
            "generation": generation,
        }

    def begin_execution_cancellation(
        self,
        run_id: str,
        *,
        owner_id: str,
        generation: int,
        agent: ProcessIdentity,
    ) -> bool:
        """Durably mark one ownership generation as being cancelled.

        While a run is ``CANCELLING`` no other process may claim it, so nothing
        can slip in between "the old owner died" and "the run became terminal".
        The cancelling agent's own identity is recorded so an abandoned
        cancellation can later be superseded instead of wedging the run.
        """
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run_row = conn.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
            if run_row is None or RunStatus(str(run_row["status"])) in TERMINAL_RUN_STATUSES:
                conn.rollback()
                return False
            row = conn.execute(
                "SELECT * FROM execution_owners WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                conn.rollback()
                return False
            incumbent = self._row_to_execution_owner(row)
            if incumbent.owner_id != owner_id or incumbent.generation != generation:
                conn.rollback()
                return False
            if incumbent.state not in ExecutionOwner.ACTIVE_STATES:
                conn.rollback()
                return False
            if incumbent.is_cancelling:
                # Another cancellation already holds this generation; only take
                # it over when its agent is provably gone.
                agent_liveness = probe_liveness(incumbent.cancel_agent)
                is_us = (
                    incumbent.cancel_agent is not None
                    and incumbent.cancel_agent.host == agent.host
                    and incumbent.cancel_agent.pid == agent.pid
                    and incumbent.cancel_agent.start_ticks == agent.start_ticks
                )
                if agent_liveness is not Liveness.DEAD and not is_us:
                    conn.rollback()
                    return False
            conn.execute(
                """
                UPDATE execution_owners
                SET state = ?, cancel_agent_json = ?
                WHERE run_id = ? AND owner_id = ? AND generation = ?
                """,
                (
                    ExecutionOwner.CANCELLING,
                    json.dumps(agent.to_dict()),
                    run_id,
                    owner_id,
                    generation,
                ),
            )
            conn.commit()
        return True

    def abort_execution_cancellation(
        self,
        run_id: str,
        *,
        owner_id: str,
        generation: int,
    ) -> bool:
        """Undo a cancellation that could not proceed, mutating nothing else."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                UPDATE execution_owners
                SET state = ?, cancel_agent_json = NULL
                WHERE run_id = ? AND owner_id = ? AND generation = ? AND state = ?
                """,
                (
                    ExecutionOwner.CLAIMED,
                    run_id,
                    owner_id,
                    generation,
                    ExecutionOwner.CANCELLING,
                ),
            )
            conn.commit()
            return cursor.rowcount > 0

    def list_runs(self, limit: int = 50, project_id: str | None = None) -> list[Run]:
        """List past runs ordered by creation time descending.

        ``project_id`` restricts the listing to one project.  Project-local
        databases are already isolated; the filter exists for the preserved
        global store, whose historical rows must never be presented as if they
        belonged to whichever project is currently selected.
        """
        with self._get_connection() as conn:
            if project_id is not None:
                cursor = conn.execute(
                    "SELECT * FROM runs WHERE project_id = ? ORDER BY created_at DESC LIMIT ?",
                    (project_id, limit),
                )
            else:
                cursor = conn.execute(
                    "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                )
            runs: list[Run] = []
            for row in cursor.fetchall():
                try:
                    review_state = (
                        str(row["review_state_json"]) if row["review_state_json"] else None
                    )  # noqa: E501
                except (KeyError, IndexError, sqlite3.OperationalError):
                    review_state = None
                try:
                    config_val = str(row["config_json"]) if row["config_json"] else None
                except (KeyError, IndexError, sqlite3.OperationalError):
                    config_val = None
                try:
                    raw_routing = row["routing_snapshot_json"]
                    routing_val = str(raw_routing) if raw_routing is not None else None
                except (KeyError, IndexError, sqlite3.OperationalError):
                    routing_val = None
                try:
                    raw_decision = row["review_risk_decision_json"]
                    decision_val = (
                        str(raw_decision) if raw_decision is not None else None
                    )
                except (KeyError, IndexError, sqlite3.OperationalError):
                    decision_val = None
                runs.append(
                    Run(
                        id=str(row["id"]),
                        task=str(row["task"]),
                        target_repo=str(row["target_repo"]),
                        base_commit=str(row["base_commit"]),
                        tier=Tier(str(row["tier"])),
                        training_allowed=bool(row["training_allowed"]),
                        gate_command=str(row["gate_command"]),
                        status=RunStatus(str(row["status"])),
                        created_at=str(row["created_at"]),
                        completed_at=str(row["completed_at"]) if row["completed_at"] else None,
                        final_attempt_id=str(row["final_attempt_id"])
                        if row["final_attempt_id"]
                        else None,
                        result_summary=str(row["result_summary"])
                        if row["result_summary"]
                        else None,
                        config_json=config_val,
                        review_state_json=review_state,
                        review_risk_decision_json=decision_val,
                        routing_snapshot_json=routing_val,
                        control_plane_snapshot_json=_optional_text(
                            row, "control_plane_snapshot_json"
                        ),
                        control_plane_snapshot_digest=_optional_text(
                            row, "control_plane_snapshot_digest"
                        ),
                        project_id=_optional_text(row, "project_id"),
                        project_identity_json=_optional_text(row, "project_identity_json"),
                        objective_id=_optional_text(row, "objective_id"),
                        gateway_snapshot_json=_optional_text(row, "gateway_snapshot_json"),
                        gateway_snapshot_digest=_optional_text(row, "gateway_snapshot_digest"),
                    )
                )
            return runs

    def create_attempt(self, attempt: Attempt) -> None:
        """Insert a new worker attempt, inheriting provenance from its run.

        A candidate is only ever traceable if the row that produced it records
        the project, objective, and authorized base it was created under.  Any
        provenance the caller supplied explicitly is preserved verbatim so a
        mismatch stays visible instead of being quietly repaired.
        """
        project_id = attempt.project_id
        objective_id = attempt.objective_id
        base_sha = attempt.base_sha
        if project_id is None or objective_id is None or base_sha is None:
            parent = self.get_run(attempt.run_id)
            if parent is not None:
                project_id = project_id if project_id is not None else parent.project_id
                objective_id = objective_id if objective_id is not None else parent.objective_id
                base_sha = base_sha if base_sha is not None else parent.base_commit
        attempt.project_id = project_id
        attempt.objective_id = objective_id
        attempt.base_sha = base_sha
        with self._get_connection() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO attempts (
                        id, run_id, attempt_number, provider, model,
                        worktree_path, branch_name, status, worker_stdout,
                        worker_stderr, worker_error, commit_sha, created_at, completed_at,
                        project_id, objective_id, base_sha, interruption_reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        attempt.id,
                        attempt.run_id,
                        attempt.attempt_number,
                        attempt.provider,
                        attempt.model,
                        attempt.worktree_path,
                        attempt.branch_name,
                        attempt.status.value,
                        attempt.worker_stdout,
                        attempt.worker_stderr,
                        attempt.worker_error,
                        attempt.commit_sha,
                        attempt.created_at,
                        attempt.completed_at,
                        project_id,
                        objective_id,
                        base_sha,
                        attempt.interruption_reason,
                    ),
                )
            except sqlite3.OperationalError as exc:
                if not _is_missing_column(
                    exc, ("project_id", "objective_id", "base_sha", "interruption_reason")
                ):
                    raise
                conn.execute(
                    """
                    INSERT INTO attempts (
                        id, run_id, attempt_number, provider, model,
                        worktree_path, branch_name, status, worker_stdout,
                        worker_stderr, worker_error, commit_sha, created_at, completed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        attempt.id,
                        attempt.run_id,
                        attempt.attempt_number,
                        attempt.provider,
                        attempt.model,
                        attempt.worktree_path,
                        attempt.branch_name,
                        attempt.status.value,
                        attempt.worker_stdout,
                        attempt.worker_stderr,
                        attempt.worker_error,
                        attempt.commit_sha,
                        attempt.created_at,
                        attempt.completed_at,
                    ),
                )
            conn.commit()

    def get_attempt(self, attempt_id: str) -> Attempt | None:
        """Fetch an attempt by ID."""
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,))
            row = cursor.fetchone()
            if row is None:
                return None
            return self._row_to_attempt(row)

    def get_attempts_for_run(self, run_id: str) -> list[Attempt]:
        """Fetch all attempts for a given run ordered by attempt_number."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM attempts WHERE run_id = ? ORDER BY attempt_number ASC",
                (run_id,),
            )
            return [self._row_to_attempt(row) for row in cursor.fetchall()]

    def update_attempt(
        self,
        attempt_id: str,
        status: AttemptStatus,
        worker_stdout: str = "",
        worker_stderr: str = "",
        worker_error: str | None = None,
        commit_sha: str | None = None,
        completed_at: str | None = None,
        interruption_reason: str | None = None,
    ) -> None:
        """Update attempt outcomes."""
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE attempts
                SET status = ?, worker_stdout = ?, worker_stderr = ?,
                    worker_error = ?, commit_sha = ?, completed_at = ?,
                    interruption_reason = COALESCE(?, interruption_reason)
                WHERE id = ?
                """,
                (
                    status.value,
                    worker_stdout,
                    worker_stderr,
                    worker_error,
                    commit_sha,
                    completed_at,
                    interruption_reason,
                    attempt_id,
                ),
            )
            conn.commit()

    def create_check(self, check: CheckResult) -> None:
        """Insert a gate check result."""
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO checks (
                    id, attempt_id, gate_command, exit_code, stdout, stderr, passed, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    check.id,
                    check.attempt_id,
                    check.gate_command,
                    check.exit_code,
                    check.stdout,
                    check.stderr,
                    1 if check.passed else 0,
                    check.created_at,
                ),
            )
            conn.commit()

    def get_checks_for_attempt(self, attempt_id: str) -> list[CheckResult]:
        """Fetch checks for an attempt."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM checks WHERE attempt_id = ? ORDER BY created_at ASC",
                (attempt_id,),
            )
            results: list[CheckResult] = []
            for row in cursor.fetchall():
                results.append(
                    CheckResult(
                        id=str(row["id"]),
                        attempt_id=str(row["attempt_id"]),
                        gate_command=str(row["gate_command"]),
                        exit_code=int(row["exit_code"]),
                        stdout=str(row["stdout"]),
                        stderr=str(row["stderr"]),
                        passed=bool(row["passed"]),
                        created_at=str(row["created_at"]),
                    )
                )
            return results

    def get_checks_for_run(self, run_id: str) -> list[CheckResult]:
        """Fetch all gate evidence for a run in stable order."""
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT checks.* FROM checks
                JOIN attempts ON attempts.id = checks.attempt_id
                WHERE attempts.run_id = ?
                ORDER BY checks.created_at ASC, checks.id ASC
                """,
                (run_id,),
            ).fetchall()
        return [
            CheckResult(
                id=str(row["id"]),
                attempt_id=str(row["attempt_id"]),
                gate_command=str(row["gate_command"]),
                exit_code=int(row["exit_code"]),
                stdout=str(row["stdout"]),
                stderr=str(row["stderr"]),
                passed=bool(row["passed"]),
                created_at=str(row["created_at"]),
            )
            for row in rows
        ]

    def create_review(self, review: ReviewResult) -> None:
        """Insert a review result."""
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO reviews (
                    id, run_id, provider, model, verdict, summary, details, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    review.id,
                    review.run_id,
                    review.provider,
                    review.model,
                    review.verdict.value,
                    review.summary,
                    review.details,
                    review.created_at,
                ),
            )
            conn.commit()

    def create_model_call_usage(self, usage: ModelCallUsage) -> None:
        """Persist normalized exact-or-unknown telemetry for one model call."""
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO model_call_usage (
                    id, run_id, association_id, attempt_id, review_id, stage, tier,
                    provider, model, prompt_bytes, transport, input_tokens,
                    output_tokens, cached_tokens, total_tokens, cache_read_tokens,
                    cache_write_tokens, reasoning_tokens, usage_source,
                    raw_usage_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    usage.id,
                    usage.run_id,
                    usage.association_id,
                    usage.attempt_id,
                    usage.review_id,
                    usage.stage.value,
                    usage.tier.value,
                    usage.provider,
                    usage.model,
                    usage.prompt_bytes,
                    usage.transport,
                    usage.input_tokens,
                    usage.output_tokens,
                    usage.cached_tokens,
                    usage.total_tokens,
                    usage.cache_read_tokens,
                    usage.cache_write_tokens,
                    usage.reasoning_tokens,
                    usage.usage_source,
                    usage.raw_usage_json,
                    usage.created_at,
                ),
            )
            conn.commit()

    def link_model_calls_to_review(
        self, run_id: str, association_ids: list[str], review_id: str
    ) -> None:
        """Attach actual review calls to their durable review outcome.

        ``association_id`` remains the identity of each actual model call;
        this lifecycle update only adds the outcome edge after the result row
        exists.  Several failed calls may therefore point to one final result.
        """
        if not association_ids:
            return
        placeholders = ", ".join("?" for _ in association_ids)
        with self._get_connection() as conn:
            conn.execute(
                f"UPDATE dispatch_evidence SET review_id = ? "
                f"WHERE run_id = ? AND association_id IN ({placeholders})",
                [review_id, run_id, *association_ids],
            )
            conn.execute(
                f"UPDATE model_call_usage SET review_id = ? "
                f"WHERE run_id = ? AND association_id IN ({placeholders})",
                [review_id, run_id, *association_ids],
            )
            conn.commit()

    def get_model_call_usage(self, run_id: str) -> list[ModelCallUsage]:
        """Return durable usage rows in deterministic creation/id order."""
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM model_call_usage
                WHERE run_id = ? ORDER BY created_at ASC, id ASC
                """,
                (run_id,),
            ).fetchall()
        return [
            ModelCallUsage(
                id=str(row["id"]),
                run_id=str(row["run_id"]),
                association_id=str(row["association_id"]),
                attempt_id=str(row["attempt_id"]) if row["attempt_id"] else None,
                review_id=str(row["review_id"]) if row["review_id"] else None,
                stage=DispatchStage(str(row["stage"])),
                tier=Tier(str(row["tier"])),
                provider=str(row["provider"]),
                model=str(row["model"]),
                prompt_bytes=int(row["prompt_bytes"]),
                transport=str(row["transport"]),
                input_tokens=int(row["input_tokens"]) if row["input_tokens"] is not None else None,
                output_tokens=int(row["output_tokens"])
                if row["output_tokens"] is not None
                else None,
                cached_tokens=int(row["cached_tokens"])
                if row["cached_tokens"] is not None
                else None,
                total_tokens=int(row["total_tokens"]) if row["total_tokens"] is not None else None,
                cache_read_tokens=int(row["cache_read_tokens"])
                if row["cache_read_tokens"] is not None
                else None,
                cache_write_tokens=int(row["cache_write_tokens"])
                if row["cache_write_tokens"] is not None
                else None,
                reasoning_tokens=int(row["reasoning_tokens"])
                if row["reasoning_tokens"] is not None
                else None,
                usage_source=str(row["usage_source"]),
                raw_usage_json=str(row["raw_usage_json"]) if row["raw_usage_json"] else None,
                created_at=str(row["created_at"]),
            )
            for row in rows
        ]

    def aggregate_model_call_usage(self, run_id: str) -> dict[str, object]:
        """Return deterministic run-level token totals and coverage."""
        from orchestrator_mvp.telemetry import aggregate_usage

        return aggregate_usage(self.get_model_call_usage(run_id))

    # -----------------------------------------------------------------------
    # C06: durable dispatch evidence and observation-only quota telemetry
    # -----------------------------------------------------------------------

    def create_dispatch_evidence(self, evidence: DispatchEvidence) -> None:
        """Insert one evidence row at the actual model dispatch boundary."""
        with self._get_connection() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO dispatch_evidence (
                        id, run_id, association_id, attempt_id, review_id, stage, role, tier,
                        dispatch_reason, requested_provider, requested_model, actual_provider,
                        actual_model, package_id, plan_step_id, base_sha, candidate_sha,
                        inherited_checkpoint_sha, context_mode, handoff_id, prompt_bytes,
                        prompt_sha256, prompt_sections_json, raw_parent_included,
                        validation_policy_occurrence_count, prompt_transport, tool_transport,
                        authority_mode, routing_snapshot_digest, selected_candidate,
                        prior_failure_refs_json, started_at, completed_at, dispatch_outcome,
                        interruption_reason, interruption_timestamp, handoff_bytes,
                        replacement_attempt_id, time_to_resume_seconds, tool_schema_bytes,
                        tool_description_bytes, tool_discovery_bytes, tool_result_bytes,
                        tools_exposed_count, tools_actually_used_count, control_plane_digest,
                        gateway_evidence_json, binding_id, profile_id, quota_pool_id,
                        reasoning_effort
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    _dispatch_values(evidence),
                )
            except sqlite3.OperationalError as exc:
                msg = str(exc)
                if "reasoning_effort" in msg or "binding_id" in msg:
                    # Legacy database missing the C11-D binding/effort
                    # columns.  Drop them and retry through the C10 path.
                    _drop_c11d_identity(evidence)
                    try:
                        conn.execute(
                            """
                            INSERT INTO dispatch_evidence (
                                id, run_id, association_id, attempt_id, review_id, stage, role, tier,
                                dispatch_reason, requested_provider, requested_model, actual_provider,
                                actual_model, package_id, plan_step_id, base_sha, candidate_sha,
                                inherited_checkpoint_sha, context_mode, handoff_id, prompt_bytes,
                                prompt_sha256, prompt_sections_json, raw_parent_included,
                                validation_policy_occurrence_count, prompt_transport, tool_transport,
                                authority_mode, routing_snapshot_digest, selected_candidate,
                                prior_failure_refs_json, started_at, completed_at, dispatch_outcome,
                                interruption_reason, interruption_timestamp, handoff_bytes,
                                replacement_attempt_id, time_to_resume_seconds, tool_schema_bytes,
                                tool_description_bytes, tool_discovery_bytes, tool_result_bytes,
                                tools_exposed_count, tools_actually_used_count, control_plane_digest,
                                gateway_evidence_json
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                      ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                      ?, ?, ?, ?, ?, ?)
                            """,
                            _dispatch_values_c10(evidence),
                        )
                    except sqlite3.OperationalError as exc2:
                        if "gateway_evidence_json" not in str(exc2):
                            raise
                        _drop_gateway_evidence(evidence)
                        conn.execute(
                            """
                            INSERT INTO dispatch_evidence (
                                id, run_id, association_id, attempt_id, review_id, stage, role, tier,
                                dispatch_reason, requested_provider, requested_model, actual_provider,
                                actual_model, package_id, plan_step_id, base_sha, candidate_sha,
                                inherited_checkpoint_sha, context_mode, handoff_id, prompt_bytes,
                                prompt_sha256, prompt_sections_json, raw_parent_included,
                                validation_policy_occurrence_count, prompt_transport, tool_transport,
                                authority_mode, routing_snapshot_digest, selected_candidate,
                                prior_failure_refs_json, started_at, completed_at, dispatch_outcome,
                                interruption_reason, interruption_timestamp, handoff_bytes,
                                replacement_attempt_id, time_to_resume_seconds, tool_schema_bytes,
                                tool_description_bytes, tool_discovery_bytes, tool_result_bytes,
                                tools_exposed_count, tools_actually_used_count, control_plane_digest
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                      ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                      ?, ?, ?, ?, ?)
                            """,
                            _dispatch_values_legacy(evidence),
                        )
                elif "gateway_evidence_json" in msg:
                    # Legacy database missing the C10 column. Insert without it.
                    _drop_gateway_evidence(evidence)
                    conn.execute(
                        """
                        INSERT INTO dispatch_evidence (
                            id, run_id, association_id, attempt_id, review_id, stage, role, tier,
                            dispatch_reason, requested_provider, requested_model, actual_provider,
                            actual_model, package_id, plan_step_id, base_sha, candidate_sha,
                            inherited_checkpoint_sha, context_mode, handoff_id, prompt_bytes,
                            prompt_sha256, prompt_sections_json, raw_parent_included,
                            validation_policy_occurrence_count, prompt_transport, tool_transport,
                            authority_mode, routing_snapshot_digest, selected_candidate,
                            prior_failure_refs_json, started_at, completed_at, dispatch_outcome,
                            interruption_reason, interruption_timestamp, handoff_bytes,
                            replacement_attempt_id, time_to_resume_seconds, tool_schema_bytes,
                            tool_description_bytes, tool_discovery_bytes, tool_result_bytes,
                                tools_exposed_count, tools_actually_used_count, control_plane_digest
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                  ?, ?, ?, ?, ?)
                        """,
                        _dispatch_values_legacy(evidence),
                    )
                else:
                    raise
            conn.commit()

    def update_dispatch_evidence(self, evidence: DispatchEvidence) -> None:
        """Update only lifecycle/result fields of an existing dispatch row."""
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE dispatch_evidence SET
                    actual_provider = ?, actual_model = ?, candidate_sha = ?,
                    prompt_bytes = ?, prompt_transport = ?, completed_at = ?,
                    dispatch_outcome = ?, interruption_reason = ?,
                    interruption_timestamp = ?, handoff_bytes = ?,
                    replacement_attempt_id = ?, time_to_resume_seconds = ?,
                    tool_discovery_bytes = ?, tool_result_bytes = ?,
                    tools_actually_used_count = ?,
                    gateway_evidence_json = ?
                WHERE id = ?
                """,
                (
                    evidence.actual_provider,
                    evidence.actual_model,
                    evidence.candidate_sha,
                    evidence.prompt_bytes,
                    evidence.prompt_transport,
                    evidence.completed_at,
                    evidence.dispatch_outcome,
                    evidence.interruption_reason,
                    evidence.interruption_timestamp,
                    evidence.handoff_bytes,
                    evidence.replacement_attempt_id,
                    evidence.time_to_resume_seconds,
                    evidence.tool_discovery_bytes,
                    evidence.tool_result_bytes,
                    evidence.tools_actually_used_count,
                    getattr(evidence, "gateway_evidence_json", None),
                    evidence.id,
                ),
            )
            conn.commit()

    def link_dispatch_replacement(
        self,
        run_id: str,
        source_attempt_id: str,
        replacement_attempt_id: str,
        replacement_started_at: str,
        handoff_id: str | None = None,
    ) -> int:
        """Link a persisted interrupted dispatch to its replacement attempt."""
        handoff = self.get_handoff(handoff_id) if handoff_id else None
        handoff_bytes = len(handoff.content.encode("utf-8")) if handoff else None
        try:
            replacement_started = datetime.fromisoformat(replacement_started_at)
        except ValueError:
            replacement_started = None
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT id, interruption_timestamp FROM dispatch_evidence
                WHERE run_id = ? AND attempt_id = ?
                ORDER BY started_at ASC, id ASC
                """,
                (run_id, source_attempt_id),
            ).fetchall()
            linked = 0
            resume_seconds_for_replacement: float | None = None
            for row in rows:
                resume_seconds: float | None = None
                if replacement_started is not None and row["interruption_timestamp"]:
                    try:
                        interrupted = datetime.fromisoformat(str(row["interruption_timestamp"]))
                        resume_seconds = round(
                            (replacement_started - interrupted).total_seconds(), 10
                        )
                        if resume_seconds_for_replacement is None:
                            resume_seconds_for_replacement = resume_seconds
                    except ValueError:
                        resume_seconds = None
                conn.execute(
                    """
                    UPDATE dispatch_evidence SET
                        replacement_attempt_id = ?, handoff_id = COALESCE(?, handoff_id),
                        handoff_bytes = ?, time_to_resume_seconds = ?
                    WHERE id = ?
                    """,
                    (
                        replacement_attempt_id,
                        handoff_id,
                        handoff_bytes,
                        resume_seconds,
                        str(row["id"]),
                    ),
                )
                linked += 1
            if linked:
                conn.execute(
                    """
                    UPDATE dispatch_evidence SET
                        handoff_id = COALESCE(?, handoff_id), handoff_bytes = ?,
                        time_to_resume_seconds = ?
                    WHERE run_id = ? AND attempt_id = ?
                    """,
                    (
                        handoff_id,
                        handoff_bytes,
                        resume_seconds_for_replacement,
                        run_id,
                        replacement_attempt_id,
                    ),
                )
            conn.commit()
        return linked

    def get_dispatch_evidence_for_run(self, run_id: str) -> list[DispatchEvidence]:
        """Return dispatch evidence in stable actual-dispatch order."""
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM dispatch_evidence
                WHERE run_id = ? ORDER BY started_at ASC, id ASC
                """,
                (run_id,),
            ).fetchall()
        return [self._row_to_dispatch_evidence(row) for row in rows]

    def get_dispatch_evidence(self, evidence_id: str) -> DispatchEvidence | None:
        """Fetch one dispatch evidence row by stable ID."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM dispatch_evidence WHERE id = ?", (evidence_id,)
            ).fetchone()
        return self._row_to_dispatch_evidence(row) if row is not None else None

    def create_quota_observation(
        self, observation: QuotaObservation, run_id: str | None = None
    ) -> None:
        """Persist a canonical observation without applying routing policy."""
        normalized = normalize_quota_observation(
            observation.raw_value, observation.raw_semantics, observation.raw_limit
        )
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO quota_observations (
                    id, run_id, provider, account_ref, pool, window_type, raw_value,
                    raw_limit, raw_semantics, normalized_remaining_pct, reset_at,
                    observed_at, source, freshness, confidence
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation.id,
                    run_id or observation.run_id,
                    observation.provider,
                    observation.account_ref,
                    observation.pool,
                    observation.window_type,
                    observation.raw_value,
                    observation.raw_limit,
                    observation.raw_semantics,
                    normalized,
                    observation.reset_at,
                    observation.observed_at,
                    observation.source,
                    observation.freshness,
                    observation.confidence,
                ),
            )
            conn.commit()

    def create_run_quota_observation(self, run_id: str, observation: QuotaObservation) -> None:
        """Persist an observation associated with a run."""
        normalized = normalize_quota_observation(
            observation.raw_value, observation.raw_semantics, observation.raw_limit
        )
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO quota_observations (
                    id, run_id, provider, account_ref, pool, window_type, raw_value,
                    raw_limit, raw_semantics, normalized_remaining_pct, reset_at,
                    observed_at, source, freshness, confidence
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation.id,
                    run_id,
                    observation.provider,
                    observation.account_ref,
                    observation.pool,
                    observation.window_type,
                    observation.raw_value,
                    observation.raw_limit,
                    observation.raw_semantics,
                    normalized,
                    observation.reset_at,
                    observation.observed_at,
                    observation.source,
                    observation.freshness,
                    observation.confidence,
                ),
            )
            conn.commit()

    def get_quota_observations_for_run(self, run_id: str) -> list[QuotaObservation]:
        """Fetch run-associated quota observations deterministically."""
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM quota_observations
                WHERE run_id = ? ORDER BY observed_at ASC, id ASC
                """,
                (run_id,),
            ).fetchall()
        return [self._row_to_quota_observation(row) for row in rows]

    def get_reviews_for_run(self, run_id: str) -> list[ReviewResult]:
        """Fetch all reviews for a run in creation order."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM reviews WHERE run_id = ? ORDER BY created_at ASC",
                (run_id,),
            )
            return [self._row_to_review(row) for row in cursor.fetchall()]

    def get_latest_review(self, run_id: str) -> ReviewResult | None:
        """Fetch the most recent review for a run if any."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM reviews WHERE run_id = ? ORDER BY created_at DESC LIMIT 1",
                (run_id,),
            )
            row = cursor.fetchone()
            return self._row_to_review(row) if row else None

    def count_launched_reviews(self, run_id: str) -> int:
        """Return total number of reviewer model calls actually launched for a run."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT (
                    SELECT COUNT(*) FROM events
                    WHERE run_id = ? AND event_type = 'review_candidate_unusable'
                ) + (
                    SELECT COUNT(*) FROM reviews
                    WHERE run_id = ? AND provider != ''
                ) AS cnt
                """,
                (run_id, run_id),
            )
            row = cursor.fetchone()
            return int(row["cnt"]) if row else 0

    def create_review_findings(self, batch: list[ReviewFinding]) -> None:
        """Insert a batch of structured findings for one review atomically.

        Additive Slice 3C persistence; duplicate IDs fail closed via the
        primary key. Legacy rows elsewhere are untouched.
        """
        if not batch:
            return
        with self._get_connection() as conn:
            conn.executemany(
                """
                INSERT INTO review_findings (
                    id, review_id, run_id, classification, title, severity,
                    contract_reference, invariant_or_requirement, code_location,
                    failure_scenario, evidence, minimum_correction, repair_scope,
                    blocking, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        finding.id,
                        finding.review_id,
                        finding.run_id,
                        finding.classification,
                        finding.title,
                        finding.severity,
                        finding.contract_reference,
                        finding.invariant_or_requirement,
                        finding.code_location,
                        finding.failure_scenario,
                        finding.evidence,
                        finding.minimum_correction,
                        finding.repair_scope,
                        1 if finding.blocking else 0,
                        finding.created_at,
                    )
                    for finding in batch
                ],
            )
            conn.commit()

    def get_findings_for_review(self, review_id: str) -> list[ReviewFinding]:
        """Fetch structured findings for one review in deterministic order."""
        if not self._plan_schema_available:
            return []
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM review_findings WHERE review_id = ?
                ORDER BY created_at ASC, id ASC
                """,
                (review_id,),
            ).fetchall()
            return [self._row_to_review_finding(row) for row in rows]

    def get_findings_for_run(self, run_id: str) -> list[ReviewFinding]:
        """Fetch all structured findings for a run in deterministic order."""
        if not self._plan_schema_available:
            return []
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM review_findings WHERE run_id = ?
                ORDER BY created_at ASC, id ASC
                """,
                (run_id,),
            ).fetchall()
            return [self._row_to_review_finding(row) for row in rows]

    def record_event(
        self,
        run_id: str,
        event_type: str,
        attempt_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> int:
        """Log an event and notify in-process listeners."""
        payload_str = json.dumps(payload) if payload is not None else None
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO events (run_id, attempt_id, event_type, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (run_id, attempt_id, event_type, payload_str, current_iso_timestamp()),
            )
            event_id = int(cursor.lastrowid) if cursor.lastrowid is not None else 0
            conn.commit()

        with self._listeners_lock:
            listeners = list(self._listeners)

        for listener in listeners:
            try:
                listener(run_id, event_id)
            except Exception:
                pass  # Listener exceptions are swallowed and must never affect orchestration

        # Detached execution owners write from their own process, so in-process
        # listeners alone cannot wake the UI or ``orch wait``.  The durable log
        # stays authoritative; this only says "there is something new to read".
        notify_run(self.db_path, run_id)

        return event_id

    def get_events(self, run_id: str, after_id: int = 0, limit: int = 1000) -> list[OrchEvent]:
        """Fetch events for a run with ID greater than after_id, ordered by ID ascending."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT id, run_id, attempt_id, event_type, payload_json, created_at
                FROM events
                WHERE run_id = ? AND id > ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (run_id, after_id, limit),
            )
            events: list[OrchEvent] = []
            for row in cursor.fetchall():
                payload_str = row["payload_json"]
                payload: dict[str, Any] | None = json.loads(payload_str) if payload_str else None
                events.append(
                    OrchEvent(
                        id=int(row["id"]),
                        run_id=str(row["run_id"]),
                        attempt_id=str(row["attempt_id"]) if row["attempt_id"] else None,
                        event_type=str(row["event_type"]),
                        payload=payload,
                        created_at=str(row["created_at"]),
                    )
                )
            return events

    def get_complete_events_for_run(self, run_id: str) -> list[OrchEvent]:
        """Fetch the complete authoritative event stream for one run.

        This is deliberately separate from :meth:`get_events`, whose bounded
        default is suitable for UI/reconnect windows but cannot authorize a
        provenance, recovery, or acceptance decision.
        """
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT id, run_id, attempt_id, event_type, payload_json, created_at
                FROM events
                WHERE run_id = ?
                ORDER BY id ASC
                """,
                (run_id,),
            ).fetchall()
        return [self._row_to_event(row) for row in rows]

    def get_events_by_type(self, run_id: str, event_type: str) -> list[OrchEvent]:
        """Fetch all authoritative events of one type for a run via SQL."""
        if not isinstance(event_type, str) or not event_type:
            raise ValueError("event_type must be a non-empty string")
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT id, run_id, attempt_id, event_type, payload_json, created_at
                FROM events
                WHERE run_id = ? AND event_type = ?
                ORDER BY id ASC
                """,
                (run_id, event_type),
            ).fetchall()
        return [self._row_to_event(row) for row in rows]

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> OrchEvent:
        """Convert one event row without applying a presentation limit."""
        payload_str = row["payload_json"]
        payload: dict[str, Any] | None = json.loads(payload_str) if payload_str else None
        return OrchEvent(
            id=int(row["id"]),
            run_id=str(row["run_id"]),
            attempt_id=str(row["attempt_id"]) if row["attempt_id"] else None,
            event_type=str(row["event_type"]),
            payload=payload,
            created_at=str(row["created_at"]),
        )

    # Slice 3B plan/supervision persistence.  These are normalized rows rather
    # than an opaque run blob so a reconnect can render and resume deterministically.
    def create_run_plan(self, plan: RunPlan) -> None:
        """Persist a new plan; duplicate IDs fail closed via the primary key."""
        with self._get_connection() as conn:
            conn.execute(
                "INSERT INTO run_plans (id, run_id, status, version, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    plan.id,
                    plan.run_id,
                    plan.status.value,
                    plan.version,
                    plan.created_at,
                    plan.updated_at,
                ),
            )
            conn.commit()

    def get_run_plan(self, run_id: str) -> RunPlan | None:
        """Return the newest persisted plan, or ``None`` for a legacy run."""
        if not self._plan_schema_available:
            return None
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM run_plans WHERE run_id = ? ORDER BY version DESC LIMIT 1", (run_id,)
            ).fetchone()
            return self._row_to_plan(row) if row is not None else None

    def update_run_plan(self, plan_id: str, status: PlanStatus, version: int | None = None) -> None:
        """Update plan lifecycle/version without replacing its steps."""
        now = current_iso_timestamp()
        with self._get_connection() as conn:
            if version is None:
                conn.execute(
                    "UPDATE run_plans SET status = ?, updated_at = ? WHERE id = ?",
                    (status.value, now, plan_id),
                )
            else:
                conn.execute(
                    "UPDATE run_plans SET status = ?, version = ?, updated_at = ? WHERE id = ?",
                    (status.value, version, now, plan_id),
                )
            conn.commit()

    def create_plan_step(self, step: PlanStep) -> None:
        """Persist a stable plan step."""
        with self._get_connection() as conn:
            conn.execute(
                """INSERT INTO plan_steps (id, plan_id, run_id, kind, title, goal, status, ordering,
                   dependency_ids_json, work_package_id, attempt_id, review_id, blocker, error,
                   created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    step.id,
                    step.plan_id,
                    step.run_id,
                    step.kind,
                    step.title,
                    step.goal,
                    step.status.value,
                    step.ordering,
                    json.dumps(list(step.dependency_ids)),
                    step.work_package_id,
                    step.attempt_id,
                    step.review_id,
                    step.blocker,
                    step.error,
                    step.created_at,
                    step.updated_at,
                ),
            )
            conn.commit()

    def get_plan_steps(self, run_id: str) -> list[PlanStep]:
        """Return plan steps in deterministic execution/display order."""
        if not self._plan_schema_available:
            return []
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM plan_steps WHERE run_id = ? ORDER BY ordering ASC, id ASC", (run_id,)
            ).fetchall()
            return [self._row_to_plan_step(row) for row in rows]

    def update_plan_step(
        self,
        step_id: str,
        status: PlanStepStatus,
        *,
        work_package_id: str | None = None,
        attempt_id: str | None = None,
        review_id: str | None = None,
        blocker: str | None = None,
        error: str | None = None,
    ) -> None:
        """Transition one step and retain any absent linkage values."""
        now = current_iso_timestamp()
        with self._get_connection() as conn:
            row = conn.execute("SELECT * FROM plan_steps WHERE id = ?", (step_id,)).fetchone()
            if row is None:
                raise ValueError(f"unknown plan step: {step_id}")
            conn.execute(
                """UPDATE plan_steps SET status = ?, work_package_id = ?, attempt_id = ?, review_id = ?,
                   blocker = ?, error = ?, updated_at = ? WHERE id = ?""",
                (
                    status.value,
                    work_package_id if work_package_id is not None else row["work_package_id"],
                    attempt_id if attempt_id is not None else row["attempt_id"],
                    review_id if review_id is not None else row["review_id"],
                    blocker if blocker is not None else row["blocker"],
                    error if error is not None else row["error"],
                    now,
                    step_id,
                ),
            )
            conn.commit()

    # ------------------------------------------------------------------
    # C09: run-scoped project scope evidence (frozen graph + expansions).
    # The persistence layer stores and returns plain JSON strings/dicts;
    # project-scope semantics live in the project_scope module.
    # ------------------------------------------------------------------

    def set_run_project_scope(
        self,
        *,
        run_id: str,
        repo_root: str,
        graph_json: str,
        graph_digest: str,
        initial_scope_json: str,
        initial_scope_digest: str,
        current_scope_json: str,
        current_scope_digest: str,
        analyzer: str,
        graph_source: str,
        confidence: str,
        base_revision: str,
    ) -> None:
        """Freeze (or idempotently rewrite) the run's project scope record."""
        now = current_iso_timestamp()
        with self._get_connection() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO run_project_scope (
                       run_id, repo_root, project_graph_json, project_graph_digest,
                       initial_scope_json, initial_scope_digest,
                       current_scope_json, current_scope_digest,
                       analyzer, graph_source, confidence, base_revision,
                       created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    repo_root,
                    graph_json,
                    graph_digest,
                    initial_scope_json,
                    initial_scope_digest,
                    current_scope_json,
                    current_scope_digest,
                    analyzer,
                    graph_source,
                    confidence,
                    base_revision,
                    now,
                    now,
                ),
            )
            conn.commit()

    def get_run_project_scope(self, run_id: str) -> dict[str, Any] | None:
        """Return the raw scope-evidence row for ``run_id`` as a plain dict."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM run_project_scope WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            return None
        return {key: row[key] for key in row.keys()}

    def update_run_current_scope(self, *, run_id: str, scope_json: str, scope_digest: str) -> None:
        """Promote the current scope after a recorded expansion."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                """UPDATE run_project_scope
                   SET current_scope_json = ?, current_scope_digest = ?, updated_at = ?
                   WHERE run_id = ?""",
                (scope_json, scope_digest, current_iso_timestamp(), run_id),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"run '{run_id}' has no project scope record")
            conn.commit()

    def add_scope_expansion(
        self,
        *,
        run_id: str,
        attempt_id: str | None,
        path: str | None,
        symbol: str | None,
        reason: str,
        before_digest: str,
        after_digest: str,
    ) -> None:
        """Append one durable expansion record."""
        with self._get_connection() as conn:
            conn.execute(
                """INSERT INTO scope_expansions (
                       run_id, attempt_id, path, symbol, reason,
                       before_digest, after_digest, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    attempt_id,
                    path,
                    symbol,
                    reason,
                    before_digest,
                    after_digest,
                    current_iso_timestamp(),
                ),
            )
            conn.commit()

    def list_scope_expansions(self, run_id: str) -> list[dict[str, Any]]:
        """Every recorded expansion for ``run_id`` in durable order."""
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM scope_expansions WHERE run_id = ? ORDER BY id ASC", (run_id,)
            ).fetchall()
        return [{key: row[key] for key in row.keys()} for row in rows]

    def create_work_package(self, package: WorkPackage) -> None:
        """Persist a normalized coherent work package."""
        with self._get_connection() as conn:
            conn.execute(
                """INSERT INTO work_packages (id, run_id, plan_step_id, parent_objective, goal,
                   acceptance_criteria_json, dependency_package_ids_json, base_candidate_sha,
                   constraints_json, relevant_paths_json, prerequisite_summaries_json, gate_required,
                   capability_hint, tier_hint, long_running_hint, long_running, long_running_reason,
                   context_capsule_json, scope_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    package.id,
                    package.run_id,
                    package.plan_step_id,
                    package.parent_objective,
                    package.goal,
                    json.dumps(list(package.acceptance_criteria)),
                    json.dumps(list(package.dependency_package_ids)),
                    package.base_candidate_sha,
                    json.dumps(list(package.constraints)),
                    json.dumps(list(package.relevant_paths)),
                    json.dumps(list(package.prerequisite_summaries)),
                    1 if package.gate_required else 0,
                    package.capability_hint,
                    package.tier_hint,
                    None if package.long_running_hint is None else int(package.long_running_hint),
                    int(package.long_running),
                    package.long_running_reason,
                    package.context_capsule_json,
                    package.scope_json,
                    package.created_at,
                    package.updated_at,
                ),
            )
            conn.commit()

    def set_work_package_scope(self, package_id: str, scope_json: str) -> None:
        """Attach the deterministic C09 project scope to a work package."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "UPDATE work_packages SET scope_json = ?, updated_at = ? WHERE id = ?",
                (scope_json, current_iso_timestamp(), package_id),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"unknown work package: {package_id}")
            conn.commit()

    def get_work_packages(self, run_id: str) -> list[WorkPackage]:
        """Return packages ordered by their plan-step execution order."""
        if not self._plan_schema_available:
            return []
        with self._get_connection() as conn:
            rows = conn.execute(
                """SELECT wp.* FROM work_packages wp JOIN plan_steps ps ON ps.id = wp.plan_step_id
                   WHERE wp.run_id = ? ORDER BY ps.ordering ASC, wp.id ASC""",
                (run_id,),
            ).fetchall()
            return [self._row_to_work_package(row) for row in rows]

    def get_work_package(self, package_id: str) -> WorkPackage | None:
        if not self._plan_schema_available:
            return None
        with self._get_connection() as conn:
            row = conn.execute("SELECT * FROM work_packages WHERE id = ?", (package_id,)).fetchone()
            return self._row_to_work_package(row) if row is not None else None

    def update_work_package_candidate(
        self, package_id: str, candidate_sha: str | None, capsule_json: str | None = None
    ) -> None:
        """Record the authoritative base/capsule used for the next package dispatch."""
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE work_packages SET base_candidate_sha = ?, context_capsule_json = COALESCE(?, context_capsule_json), updated_at = ? WHERE id = ?",
                (candidate_sha, capsule_json, current_iso_timestamp(), package_id),
            )
            conn.commit()

    def upsert_supervision(self, record: SupervisionRecord) -> None:
        """Durably record supervisor evidence; no secrets or process handles are stored."""
        with self._get_connection() as conn:
            conn.execute(
                """INSERT INTO supervision (run_id, supervisor_identity, supervisor_state, worker_pid,
                   worker_identity, provider, model, started_at, last_activity_at, health_state,
                   health_reason, current_package_id, current_attempt_id, deadline_at, transcript_path,
                   wake_condition, manager_state, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(run_id) DO UPDATE SET supervisor_identity=excluded.supervisor_identity,
                   supervisor_state=excluded.supervisor_state, worker_pid=excluded.worker_pid,
                   worker_identity=excluded.worker_identity, provider=excluded.provider,
                   model=excluded.model, started_at=excluded.started_at,
                   last_activity_at=excluded.last_activity_at, health_state=excluded.health_state,
                   health_reason=excluded.health_reason, current_package_id=excluded.current_package_id,
                   current_attempt_id=excluded.current_attempt_id, deadline_at=excluded.deadline_at,
                   transcript_path=excluded.transcript_path, wake_condition=excluded.wake_condition,
                   manager_state=excluded.manager_state, updated_at=excluded.updated_at""",
                (
                    record.run_id,
                    record.supervisor_identity,
                    record.supervisor_state,
                    record.worker_pid,
                    record.worker_identity,
                    record.provider,
                    record.model,
                    record.started_at,
                    record.last_activity_at,
                    record.health_state.value
                    if isinstance(record.health_state, HealthState)
                    else record.health_state,
                    record.health_reason,
                    record.current_package_id,
                    record.current_attempt_id,
                    record.deadline_at,
                    record.transcript_path,
                    record.wake_condition,
                    record.manager_state.value
                    if isinstance(record.manager_state, ManagerState)
                    else record.manager_state,
                    record.updated_at,
                ),
            )
            conn.commit()

    def set_worker_identity_fenced(
        self,
        run_id: str,
        attempt_id: str,
        worker_pid: int | None,
        worker_identity: str | None,
    ) -> bool:
        """Narrow worker identity persistence that never overwrites attempt metadata.

        Only updates worker_pid, worker_identity, and updated_at on the
        supervision row that matches run_id AND current_attempt_id == attempt_id.
        Never touches provider, model, deadline_at, transcript_path, or
        last_activity_at.

        Returns True if a row was updated, False if no matching row existed
        (attempt context changed or no row yet).  Fails harmlessly — does not
        raise on any constraint or schema issue.
        """
        if not self._plan_schema_available:
            return False
        now = current_iso_timestamp()
        try:
            with self._get_connection() as conn:
                cur = conn.execute(
                    """
                    UPDATE supervision
                    SET worker_pid = ?,
                        worker_identity = ?,
                        updated_at = ?
                    WHERE run_id = ?
                      AND (current_attempt_id = ? OR current_attempt_id IS NULL)
                    """,
                    (worker_pid, worker_identity, now, run_id, attempt_id),
                )
                conn.commit()
                return cur.rowcount > 0
        except Exception:
            return False

    def record_worker_activity(
        self,
        run_id: str,
        attempt_id: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        stream: str | None = None,
        transcript_path: str | None = None,
    ) -> None:
        """Coalesced worker-activity projection for the control plane.

        This is the bounded, normalized complement to the raw transcript: it
        touches ``supervision.last_activity_at`` (and the provider/model/
        attempt/transcript context when available) and emits a single
        ``worker_activity`` event that wakes UI/SSE/``orch wait`` listeners.
        Callers must rate-limit (coalesce) per-line output before invoking
        this; the raw high-volume source of truth remains the transcript.
        """
        now = current_iso_timestamp()
        if self._plan_schema_available:
            with self._get_connection() as conn:
                conn.execute(
                    """
                    UPDATE supervision
                    SET last_activity_at = ?,
                        current_attempt_id = COALESCE(?, current_attempt_id),
                        provider = COALESCE(?, provider),
                        model = COALESCE(?, model),
                        transcript_path = COALESCE(?, transcript_path),
                        updated_at = ?
                    WHERE run_id = ?
                    """,
                    (now, attempt_id, provider, model, transcript_path, now, run_id),
                )
                conn.commit()
        self.record_event(
            run_id,
            "worker_activity",
            attempt_id=attempt_id,
            payload={
                "provider": provider,
                "model": model,
                "stream": stream,
                "last_activity_at": now,
            },
        )

    def mark_worker_stalled(
        self,
        run_id: str,
        attempt_id: str | None = None,
        *,
        stalled: bool,
        reason: str,
    ) -> None:
        """Record a soft-inactivity stall (or its recovery) durably.

        A stall is attention evidence only: it never terminates the worker and
        never changes routing.  ``stalled=False`` records that output resumed.
        """
        if not self._plan_schema_available:
            return
        now = current_iso_timestamp()
        health = "stalled" if stalled else "healthy"
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE supervision
                SET health_state = ?,
                    health_reason = ?,
                    current_attempt_id = COALESCE(?, current_attempt_id),
                    updated_at = ?
                WHERE run_id = ?
                """,
                (health, reason, attempt_id, now, run_id),
            )
            conn.commit()
        self.record_event(
            run_id,
            "worker_resumed" if not stalled else "worker_stalled",
            attempt_id=attempt_id,
            payload={"reason": reason},
        )

    def update_supervision_health(
        self,
        run_id: str,
        *,
        health_state: HealthState,
        health_reason: str,
        attempt_id: str | None = None,
        manager_state: ManagerState | None = None,
    ) -> bool:
        """Narrow supervision health projection for monitor state transitions.

        Only updates health_state, health_reason, updated_at and — when
        provided — the attempt context and manager_state.  Never touches
        worker identity, provider/model, deadlines, or activity timestamps.

        Returns True if a row was updated.
        """
        if not self._plan_schema_available:
            return False
        now = current_iso_timestamp()
        try:
            with self._get_connection() as conn:
                cur = conn.execute(
                    """
                    UPDATE supervision
                    SET health_state = ?,
                        health_reason = ?,
                        current_attempt_id = COALESCE(?, current_attempt_id),
                        manager_state = COALESCE(?, manager_state),
                        updated_at = ?
                    WHERE run_id = ?
                    """,
                    (
                        health_state.value
                        if isinstance(health_state, HealthState)
                        else health_state,
                        health_reason,
                        attempt_id,
                        manager_state.value
                        if isinstance(manager_state, ManagerState)
                        else manager_state,
                        now,
                        run_id,
                    ),
                )
                conn.commit()
                return cur.rowcount > 0
        except Exception:
            return False

    def get_supervision(self, run_id: str) -> SupervisionRecord | None:
        if not self._plan_schema_available:
            return None
        with self._get_connection() as conn:
            row = conn.execute("SELECT * FROM supervision WHERE run_id = ?", (run_id,)).fetchone()
            return self._row_to_supervision(row) if row is not None else None

    def create_handoff(self, handoff: Handoff) -> None:
        """Append immutable handoff history; callers must allocate the next version."""
        with self._get_connection() as conn:
            conn.execute(
                "INSERT INTO handoffs (id, run_id, kind, version, content, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    handoff.id,
                    handoff.run_id,
                    handoff.kind,
                    handoff.version,
                    handoff.content,
                    handoff.created_at,
                ),
            )
            conn.commit()

    def get_latest_handoff(self, run_id: str, kind: str | None = None) -> Handoff | None:
        if not self._plan_schema_available:
            return None
        query = "SELECT * FROM handoffs WHERE run_id = ?"
        values: tuple[str, ...] = (run_id,)
        if kind is not None:
            query += " AND kind = ?"
            values = (run_id, kind)
        query += " ORDER BY version DESC, created_at DESC LIMIT 1"
        with self._get_connection() as conn:
            row = conn.execute(query, values).fetchone()
            return self._row_to_handoff(row) if row is not None else None

    def get_handoff(self, handoff_id: str) -> Handoff | None:
        """Fetch one immutable handoff by its durable identity."""
        with self._get_connection() as conn:
            row = conn.execute("SELECT * FROM handoffs WHERE id = ?", (handoff_id,)).fetchone()
        return self._row_to_handoff(row) if row is not None else None

    def get_handoffs_for_run(self, run_id: str) -> list[Handoff]:
        """Fetch immutable handoffs in stable version order."""
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM handoffs WHERE run_id = ?
                ORDER BY kind ASC, version ASC, id ASC
                """,
                (run_id,),
            ).fetchall()
        return [self._row_to_handoff(row) for row in rows]

    def next_handoff_version(self, run_id: str, kind: str) -> int:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS version FROM handoffs WHERE run_id = ? AND kind = ?",
                (run_id, kind),
            ).fetchone()
            return int(row["version"]) + 1 if row is not None else 1

    def set_quota(
        self,
        provider: str,
        pool: str,
        remaining_percent: float,
        reset_at: str | None = None,
    ) -> QuotaState:
        """Upsert quota state for a provider pool.

        Derives routing state from the remaining percentage and any existing
        state, preserving a sticky Codex BLOCK until an explicit enable.

        Quota is owner-wide configuration, so a project-local database
        delegates to the single global owner store.
        """
        if self._owner_db is not None:
            return self._owner_db.set_quota(
                provider=provider,
                pool=pool,
                remaining_percent=remaining_percent,
                reset_at=reset_at,
            )
        provider_norm = provider.strip().lower()
        pool_norm = pool.strip().lower()
        if not 0.0 <= remaining_percent <= 100.0:
            raise ValueError("remaining_percent must be between 0 and 100")

        current = self.get_quota(provider_norm, pool_norm)
        routing_state = derive_routing_state(
            provider_norm,
            pool_norm,
            remaining_percent,
            current.routing_state if current is not None else None,
        )
        now = current_iso_timestamp()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO quota_state (
                    provider, pool, remaining_percent, reset_at, routing_state, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, pool) DO UPDATE SET
                    remaining_percent = excluded.remaining_percent,
                    reset_at = excluded.reset_at,
                    routing_state = excluded.routing_state,
                    updated_at = excluded.updated_at
                """,
                (provider_norm, pool_norm, remaining_percent, reset_at, routing_state.value, now),
            )
            conn.commit()
        return QuotaState(
            provider=provider_norm,
            pool=pool_norm,
            remaining_percent=remaining_percent,
            reset_at=reset_at,
            routing_state=routing_state,
            updated_at=now,
        )

    def enable_quota(self, provider: str, pool: str) -> QuotaState:
        """Explicitly re-enable automatic routing for a provider pool.

        Sets routing state to NORMAL. A subsequent quota update below the
        threshold will re-derive BLOCKED/DEMOTED as appropriate.
        """
        if self._owner_db is not None:
            return self._owner_db.enable_quota(provider=provider, pool=pool)
        provider_norm = provider.strip().lower()
        pool_norm = pool.strip().lower()
        now = current_iso_timestamp()
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT remaining_percent, reset_at FROM quota_state "
                "WHERE provider = ? AND pool = ?",
                (provider_norm, pool_norm),
            ).fetchone()
            remaining = float(row["remaining_percent"]) if row is not None else 100.0
            reset_at = str(row["reset_at"]) if row is not None and row["reset_at"] else None
            conn.execute(
                """
                INSERT INTO quota_state (
                    provider, pool, remaining_percent, reset_at, routing_state, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, pool) DO UPDATE SET
                    routing_state = excluded.routing_state,
                    updated_at = excluded.updated_at
                """,
                (provider_norm, pool_norm, remaining, reset_at, RoutingState.NORMAL.value, now),
            )
            conn.commit()
        return QuotaState(
            provider=provider_norm,
            pool=pool_norm,
            remaining_percent=remaining,
            reset_at=reset_at,
            routing_state=RoutingState.NORMAL,
            updated_at=now,
        )

    def get_quota(self, provider: str, pool: str) -> QuotaState | None:
        """Fetch quota state for a provider pool, or None if absent."""
        if self._owner_db is not None:
            return self._owner_db.get_quota(provider=provider, pool=pool)
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM quota_state WHERE provider = ? AND pool = ?",
                (provider.strip().lower(), pool.strip().lower()),
            ).fetchone()
            return self._row_to_quota(row) if row is not None else None

    def list_quota(self) -> list[QuotaState]:
        """List all quota state rows ordered by provider then pool."""
        if self._owner_db is not None:
            return self._owner_db.list_quota()
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM quota_state ORDER BY provider ASC, pool ASC"
            ).fetchall()
            return [self._row_to_quota(row) for row in rows]

    def _row_to_quota(self, row: sqlite3.Row) -> QuotaState:
        return QuotaState(
            provider=str(row["provider"]),
            pool=str(row["pool"]),
            remaining_percent=float(row["remaining_percent"]),
            reset_at=str(row["reset_at"]) if row["reset_at"] else None,
            routing_state=RoutingState(str(row["routing_state"])),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _json_strings(raw: object) -> tuple[str, ...]:
        """Decode an additive JSON-array field fail-closed to an empty tuple."""
        try:
            value = json.loads(str(raw)) if raw is not None else []
        except (TypeError, ValueError, json.JSONDecodeError):
            return ()
        return tuple(str(item) for item in value) if isinstance(value, list) else ()

    def _row_to_plan(self, row: sqlite3.Row) -> RunPlan:
        return RunPlan(
            id=str(row["id"]),
            run_id=str(row["run_id"]),
            status=PlanStatus(str(row["status"])),
            version=int(row["version"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def _row_to_plan_step(self, row: sqlite3.Row) -> PlanStep:
        return PlanStep(
            id=str(row["id"]),
            plan_id=str(row["plan_id"]),
            run_id=str(row["run_id"]),
            kind=str(row["kind"]),
            title=str(row["title"]),
            goal=str(row["goal"]),
            status=PlanStepStatus(str(row["status"])),
            ordering=int(row["ordering"]),
            dependency_ids=self._json_strings(row["dependency_ids_json"]),
            work_package_id=str(row["work_package_id"]) if row["work_package_id"] else None,
            attempt_id=str(row["attempt_id"]) if row["attempt_id"] else None,
            review_id=str(row["review_id"]) if row["review_id"] else None,
            blocker=str(row["blocker"]) if row["blocker"] else None,
            error=str(row["error"]) if row["error"] else None,
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def _row_to_work_package(self, row: sqlite3.Row) -> WorkPackage:
        hint_raw = row["long_running_hint"]
        return WorkPackage(
            id=str(row["id"]),
            run_id=str(row["run_id"]),
            plan_step_id=str(row["plan_step_id"]),
            parent_objective=str(row["parent_objective"]),
            goal=str(row["goal"]),
            acceptance_criteria=self._json_strings(row["acceptance_criteria_json"]),
            dependency_package_ids=self._json_strings(row["dependency_package_ids_json"]),
            base_candidate_sha=str(row["base_candidate_sha"])
            if row["base_candidate_sha"]
            else None,
            constraints=self._json_strings(row["constraints_json"]),
            relevant_paths=self._json_strings(row["relevant_paths_json"]),
            prerequisite_summaries=self._json_strings(row["prerequisite_summaries_json"]),
            gate_required=bool(row["gate_required"]),
            capability_hint=str(row["capability_hint"]) if row["capability_hint"] else None,
            tier_hint=str(row["tier_hint"]) if row["tier_hint"] else None,
            long_running_hint=bool(hint_raw) if hint_raw is not None else None,
            long_running=bool(row["long_running"]),
            long_running_reason=str(row["long_running_reason"])
            if row["long_running_reason"]
            else None,
            context_capsule_json=str(row["context_capsule_json"])
            if row["context_capsule_json"]
            else None,
            scope_json=(
                str(row["scope_json"])
                if "scope_json" in row.keys() and row["scope_json"]
                else None
            ),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def _row_to_supervision(self, row: sqlite3.Row) -> SupervisionRecord:
        return SupervisionRecord(
            run_id=str(row["run_id"]),
            supervisor_identity=str(row["supervisor_identity"])
            if row["supervisor_identity"]
            else None,
            supervisor_state=str(row["supervisor_state"]) if row["supervisor_state"] else None,
            worker_pid=int(row["worker_pid"]) if row["worker_pid"] is not None else None,
            worker_identity=str(row["worker_identity"]) if row["worker_identity"] else None,
            provider=str(row["provider"]) if row["provider"] else None,
            model=str(row["model"]) if row["model"] else None,
            started_at=str(row["started_at"]) if row["started_at"] else None,
            last_activity_at=str(row["last_activity_at"]) if row["last_activity_at"] else None,
            health_state=HealthState(str(row["health_state"])),
            health_reason=str(row["health_reason"]) if row["health_reason"] else None,
            current_package_id=str(row["current_package_id"])
            if row["current_package_id"]
            else None,
            current_attempt_id=str(row["current_attempt_id"])
            if row["current_attempt_id"]
            else None,
            deadline_at=str(row["deadline_at"]) if row["deadline_at"] else None,
            transcript_path=str(row["transcript_path"]) if row["transcript_path"] else None,
            wake_condition=str(row["wake_condition"]) if row["wake_condition"] else None,
            manager_state=ManagerState(str(row["manager_state"])),
            updated_at=str(row["updated_at"]),
        )

    def _row_to_handoff(self, row: sqlite3.Row) -> Handoff:
        return Handoff(
            id=str(row["id"]),
            run_id=str(row["run_id"]),
            kind=str(row["kind"]),
            version=int(row["version"]),
            content=str(row["content"]),
            created_at=str(row["created_at"]),
        )

    def _row_to_attempt(self, row: sqlite3.Row) -> Attempt:
        return Attempt(
            id=str(row["id"]),
            run_id=str(row["run_id"]),
            attempt_number=int(row["attempt_number"]),
            provider=str(row["provider"]),
            model=str(row["model"]),
            worktree_path=str(row["worktree_path"]),
            branch_name=str(row["branch_name"]),
            status=AttemptStatus(str(row["status"])),
            worker_stdout=str(row["worker_stdout"]),
            worker_stderr=str(row["worker_stderr"]),
            worker_error=str(row["worker_error"]) if row["worker_error"] else None,
            commit_sha=str(row["commit_sha"]) if row["commit_sha"] else None,
            created_at=str(row["created_at"]),
            completed_at=str(row["completed_at"]) if row["completed_at"] else None,
            project_id=_optional_text(row, "project_id"),
            objective_id=_optional_text(row, "objective_id"),
            base_sha=_optional_text(row, "base_sha"),
            interruption_reason=_optional_text(row, "interruption_reason"),
        )

    def _row_to_review(self, row: sqlite3.Row) -> ReviewResult:
        return ReviewResult(
            id=str(row["id"]),
            run_id=str(row["run_id"]),
            provider=str(row["provider"]),
            model=str(row["model"]),
            verdict=ReviewVerdict(str(row["verdict"])),
            summary=str(row["summary"]),
            details=str(row["details"]),
            created_at=str(row["created_at"]),
        )

    def _row_to_review_finding(self, row: sqlite3.Row) -> ReviewFinding:
        return ReviewFinding(
            id=str(row["id"]),
            review_id=str(row["review_id"]),
            run_id=str(row["run_id"]),
            classification=str(row["classification"]),
            title=str(row["title"]),
            severity=str(row["severity"]),
            contract_reference=str(row["contract_reference"])
            if row["contract_reference"]
            else None,
            invariant_or_requirement=str(row["invariant_or_requirement"]),
            code_location=str(row["code_location"]) if row["code_location"] else None,
            failure_scenario=str(row["failure_scenario"]),
            evidence=str(row["evidence"]),
            minimum_correction=str(row["minimum_correction"]),
            repair_scope=str(row["repair_scope"]),
            blocking=bool(row["blocking"]),
            created_at=str(row["created_at"]),
        )

    def _row_to_dispatch_evidence(self, row: sqlite3.Row) -> DispatchEvidence:
        """Decode a C06 dispatch row, retaining NULL as unknown."""
        return DispatchEvidence(
            id=str(row["id"]),
            run_id=str(row["run_id"]),
            association_id=str(row["association_id"]),
            attempt_id=_optional_text(row, "attempt_id"),
            review_id=_optional_text(row, "review_id"),
            stage=DispatchStage(str(row["stage"])),
            role=DispatchRole(str(row["role"])),
            tier=Tier(str(row["tier"])),
            dispatch_reason=str(row["dispatch_reason"]),
            requested_provider=str(row["requested_provider"]),
            requested_model=str(row["requested_model"]),
            actual_provider=_optional_text(row, "actual_provider"),
            actual_model=_optional_text(row, "actual_model"),
            package_id=_optional_text(row, "package_id"),
            plan_step_id=_optional_text(row, "plan_step_id"),
            base_sha=_optional_text(row, "base_sha"),
            candidate_sha=_optional_text(row, "candidate_sha"),
            inherited_checkpoint_sha=_optional_text(row, "inherited_checkpoint_sha"),
            context_mode=_optional_text(row, "context_mode"),
            handoff_id=_optional_text(row, "handoff_id"),
            prompt_bytes=int(row["prompt_bytes"]) if row["prompt_bytes"] is not None else None,
            prompt_sha256=_optional_text(row, "prompt_sha256"),
            prompt_sections_json=_optional_text(row, "prompt_sections_json"),
            raw_parent_included=(
                bool(row["raw_parent_included"]) if row["raw_parent_included"] is not None else None
            ),
            validation_policy_occurrence_count=(
                int(row["validation_policy_occurrence_count"])
                if row["validation_policy_occurrence_count"] is not None
                else None
            ),
            prompt_transport=_optional_text(row, "prompt_transport"),
            tool_transport=_optional_text(row, "tool_transport"),
            authority_mode=_optional_text(row, "authority_mode"),
            routing_snapshot_digest=_optional_text(row, "routing_snapshot_digest"),
            control_plane_digest=_optional_text(row, "control_plane_digest"),
            selected_candidate=_optional_text(row, "selected_candidate"),
            prior_failure_refs_json=_optional_text(row, "prior_failure_refs_json"),
            gateway_evidence_json=_optional_text(row, "gateway_evidence_json"),
            started_at=str(row["started_at"]),
            completed_at=_optional_text(row, "completed_at"),
            dispatch_outcome=_optional_text(row, "dispatch_outcome"),
            interruption_reason=_optional_text(row, "interruption_reason"),
            interruption_timestamp=_optional_text(row, "interruption_timestamp"),
            handoff_bytes=int(row["handoff_bytes"]) if row["handoff_bytes"] is not None else None,
            replacement_attempt_id=_optional_text(row, "replacement_attempt_id"),
            time_to_resume_seconds=(
                float(row["time_to_resume_seconds"])
                if row["time_to_resume_seconds"] is not None
                else None
            ),
            tool_schema_bytes=int(row["tool_schema_bytes"])
            if row["tool_schema_bytes"] is not None
            else None,
            tool_description_bytes=int(row["tool_description_bytes"])
            if row["tool_description_bytes"] is not None
            else None,
            tool_discovery_bytes=int(row["tool_discovery_bytes"])
            if row["tool_discovery_bytes"] is not None
            else None,
            tool_result_bytes=int(row["tool_result_bytes"])
            if row["tool_result_bytes"] is not None
            else None,
            tools_exposed_count=int(row["tools_exposed_count"])
            if row["tools_exposed_count"] is not None
            else None,
            tools_actually_used_count=int(row["tools_actually_used_count"])
            if row["tools_actually_used_count"] is not None
            else None,
            binding_id=_optional_text(row, "binding_id"),
            profile_id=_optional_text(row, "profile_id"),
            quota_pool_id=_optional_text(row, "quota_pool_id"),
            reasoning_effort=_optional_text(row, "reasoning_effort"),
        )

    def _row_to_quota_observation(self, row: sqlite3.Row) -> QuotaObservation:
        return QuotaObservation(
            id=str(row["id"]),
            provider=str(row["provider"]),
            account_ref=_optional_text(row, "account_ref"),
            pool=_optional_text(row, "pool"),
            window_type=_optional_text(row, "window_type"),
            raw_value=float(row["raw_value"]) if row["raw_value"] is not None else None,
            raw_limit=float(row["raw_limit"]) if row["raw_limit"] is not None else None,
            raw_semantics=str(row["raw_semantics"]),
            normalized_remaining_pct=(
                float(row["normalized_remaining_pct"])
                if row["normalized_remaining_pct"] is not None
                else None
            ),
            reset_at=_optional_text(row, "reset_at"),
            observed_at=str(row["observed_at"]),
            source=str(row["source"]),
            freshness=_optional_text(row, "freshness"),
            confidence=_optional_text(row, "confidence"),
            run_id=_optional_text(row, "run_id"),
        )

    def _row_to_worker_checkpoint(self, row: sqlite3.Row) -> WorkerCheckpoint:
        paths = json.loads(str(row["changed_paths_json"])) if row["changed_paths_json"] else []
        return WorkerCheckpoint(
            id=str(row["id"]),
            run_id=str(row["run_id"]),
            source_attempt_id=str(row["source_attempt_id"]),
            source_base_sha=str(row["source_base_sha"]),
            checkpoint_sha=str(row["checkpoint_sha"]),
            reason=str(row["reason"]),
            changed_paths=tuple(str(p) for p in paths),
            created_at=str(row["created_at"]),
            handoff_id=_optional_text(row, "handoff_id"),
        )

    def _row_to_worker_activation(self, row: sqlite3.Row) -> WorkerActivation:
        return WorkerActivation(
            id=str(row["id"]),
            run_id=str(row["run_id"]),
            attempt_id=str(row["attempt_id"]),
            generation=int(row["generation"]),
            state=WorkerActivationState(str(row["state"])),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            worker_identity_json=_optional_text(row, "worker_identity_json"),
        )

    def _row_to_side_effect(self, row: sqlite3.Row) -> SideEffectRecord:
        return SideEffectRecord(
            idempotency_key=str(row["idempotency_key"]),
            run_id=str(row["run_id"]),
            attempt_id=str(row["attempt_id"]),
            operation=SideEffectOperation(str(row["operation"])),
            state=SideEffectState(str(row["state"])),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            payload_json=_optional_text(row, "payload_json"),
            result_json=_optional_text(row, "result_json"),
        )

    def _row_to_tool_effect(self, row: sqlite3.Row) -> ToolEffect:
        return ToolEffect(
            id=str(row["id"]),
            run_id=str(row["run_id"]),
            attempt_id=str(row["attempt_id"]),
            operation=str(row["operation"]),
            capability=_optional_text(row, "capability"),
            resource_ref=_optional_text(row, "resource_ref"),
            side_effect_class=str(row["side_effect_class"]),
            status=EffectStatus(str(row["status"])),
            idempotency_key=_optional_text(row, "idempotency_key"),
            started_at=str(row["started_at"]),
            completed_at=_optional_text(row, "completed_at"),
            result_class=_optional_text(row, "result_class"),
            result_redacted=_optional_text(row, "result_redacted"),
            reconciliation_status=str(row["reconciliation_status"]),
            error=_optional_text(row, "error"),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def _row_to_attempt_trace(self, row: sqlite3.Row) -> AttemptTrace:
        return AttemptTrace(
            id=str(row["id"]),
            run_id=str(row["run_id"]),
            attempt_id=str(row["attempt_id"]),
            trace_class=TraceCapability(str(row["trace_class"])),
            source_kind=str(row["source_kind"]),
            started_at=str(row["started_at"]),
            updated_at=str(row["updated_at"]),
            completed_at=_optional_text(row, "completed_at"),
            tool_effects_count=int(row["tool_effects_count"]),
            summary_json=_optional_text(row, "summary_json"),
            failure_kind=_optional_text(row, "failure_kind"),
            failure_reason=_optional_text(row, "failure_reason"),
        )

    # -----------------------------------------------------------------------
    # C05: Worker Checkpoints
    # -----------------------------------------------------------------------

    def create_worker_checkpoint(self, checkpoint: WorkerCheckpoint) -> None:
        """Persist a durable worker checkpoint."""
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO worker_checkpoints (
                    id, run_id, source_attempt_id, source_base_sha,
                    checkpoint_sha, reason, changed_paths_json, created_at, handoff_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint.id,
                    checkpoint.run_id,
                    checkpoint.source_attempt_id,
                    checkpoint.source_base_sha,
                    checkpoint.checkpoint_sha,
                    checkpoint.reason,
                    json.dumps(list(checkpoint.changed_paths)),
                    checkpoint.created_at,
                    checkpoint.handoff_id,
                ),
            )
            conn.commit()

    def get_worker_checkpoints_for_run(self, run_id: str) -> list[WorkerCheckpoint]:
        """Fetch all worker checkpoints for a run ordered by created_at."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM worker_checkpoints WHERE run_id = ? ORDER BY created_at ASC",
                (run_id,),
            )
            return [self._row_to_worker_checkpoint(row) for row in cursor.fetchall()]

    def get_latest_worker_checkpoint(self, run_id: str) -> WorkerCheckpoint | None:
        """Fetch the most recently created worker checkpoint for a run."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM worker_checkpoints WHERE run_id = ? ORDER BY created_at DESC LIMIT 1",
                (run_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return self._row_to_worker_checkpoint(row)

    def get_worker_checkpoint_by_sha(
        self, run_id: str, checkpoint_sha: str
    ) -> WorkerCheckpoint | None:
        """Fetch a worker checkpoint for a run by its checkpoint SHA."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM worker_checkpoints WHERE run_id = ? AND checkpoint_sha = ? LIMIT 1",
                (run_id, checkpoint_sha),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return self._row_to_worker_checkpoint(row)

    def get_worker_checkpoint_for_attempt(
        self, run_id: str, source_attempt_id: str
    ) -> WorkerCheckpoint | None:
        """Fetch a worker checkpoint by its source attempt ID."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM worker_checkpoints WHERE run_id = ? AND source_attempt_id = ? LIMIT 1",
                (run_id, source_attempt_id),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return self._row_to_worker_checkpoint(row)

    # -----------------------------------------------------------------------
    # C05: Worker Activations (Fencing & Lease)
    # -----------------------------------------------------------------------

    def create_worker_activation(self, activation: WorkerActivation) -> None:
        """Persist a worker activation record for generation fencing."""
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO worker_activations (
                    id, run_id, attempt_id, generation, state,
                    worker_identity_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    activation.id,
                    activation.run_id,
                    activation.attempt_id,
                    activation.generation,
                    activation.state.value,
                    activation.worker_identity_json,
                    activation.created_at,
                    activation.updated_at,
                ),
            )
            conn.commit()

    def update_worker_activation_state(
        self,
        run_id: str,
        generation: int,
        state: WorkerActivationState,
        worker_identity_json: str | None = None,
    ) -> None:
        """Update state and optional identity of a worker activation."""
        now = datetime.now(UTC).isoformat()
        with self._get_connection() as conn:
            if worker_identity_json is not None:
                conn.execute(
                    """
                    UPDATE worker_activations
                    SET state = ?, worker_identity_json = ?, updated_at = ?
                    WHERE run_id = ? AND generation = ?
                    """,
                    (state.value, worker_identity_json, now, run_id, generation),
                )
            else:
                conn.execute(
                    """
                    UPDATE worker_activations
                    SET state = ?, updated_at = ?
                    WHERE run_id = ? AND generation = ?
                    """,
                    (state.value, now, run_id, generation),
                )
            conn.commit()

    def get_active_worker_activation(self, run_id: str) -> WorkerActivation | None:
        """Fetch the latest active worker activation for a run."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM worker_activations WHERE run_id = ? ORDER BY generation DESC LIMIT 1",
                (run_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return self._row_to_worker_activation(row)

    def get_worker_activations_for_run(self, run_id: str) -> list[WorkerActivation]:
        """Fetch all worker activations for a run ordered by generation."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM worker_activations WHERE run_id = ? ORDER BY generation ASC",
                (run_id,),
            )
            return [self._row_to_worker_activation(row) for row in cursor.fetchall()]

    # -----------------------------------------------------------------------
    # C05: Side-Effect Journal (Crash Window Idempotency)
    # -----------------------------------------------------------------------

    def journal_record_intent(
        self,
        idempotency_key: str,
        run_id: str,
        attempt_id: str,
        operation: SideEffectOperation,
        payload: dict[str, Any] | None = None,
    ) -> SideEffectRecord:
        """Record intent to perform an external or irreversible side effect."""
        now = datetime.now(UTC).isoformat()
        payload_json = json.dumps(payload) if payload is not None else None
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO side_effect_journal (
                    idempotency_key, run_id, attempt_id, operation,
                    state, payload_json, result_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)
                ON CONFLICT(idempotency_key) DO UPDATE SET
                    updated_at = excluded.updated_at
                """,
                (
                    idempotency_key,
                    run_id,
                    attempt_id,
                    operation.value,
                    SideEffectState.INTENT.value,
                    payload_json,
                    now,
                    now,
                ),
            )
            conn.commit()
        return self.get_side_effect(idempotency_key)  # type: ignore[return-value]

    def journal_record_completed(
        self,
        idempotency_key: str,
        result: dict[str, Any] | None = None,
    ) -> None:
        """Record successful completion of a journaled side effect."""
        now = datetime.now(UTC).isoformat()
        result_json = json.dumps(result) if result is not None else None
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE side_effect_journal
                SET state = ?, result_json = ?, updated_at = ?
                WHERE idempotency_key = ?
                """,
                (SideEffectState.COMPLETED.value, result_json, now, idempotency_key),
            )
            conn.commit()

    def journal_record_started(self, idempotency_key: str) -> None:
        """Mark a journaled external operation as started before execution."""
        now = datetime.now(UTC).isoformat()
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE side_effect_journal SET state = ?, updated_at = ? "
                "WHERE idempotency_key = ?",
                (SideEffectState.STARTED.value, now, idempotency_key),
            )
            conn.commit()

    def journal_record_failed(
        self,
        idempotency_key: str,
        error: str | None = None,
    ) -> None:
        """Record failure of a journaled side effect."""
        now = datetime.now(UTC).isoformat()
        result_json = json.dumps({"error": error}) if error is not None else None
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE side_effect_journal
                SET state = ?, result_json = ?, updated_at = ?
                WHERE idempotency_key = ?
                """,
                (SideEffectState.FAILED.value, result_json, now, idempotency_key),
            )
            conn.commit()

    def journal_record_uncertain(
        self,
        idempotency_key: str,
        reason: str | None = None,
    ) -> None:
        """Record uncertain outcome of a journaled side effect."""
        now = datetime.now(UTC).isoformat()
        result_json = json.dumps({"uncertain_reason": reason}) if reason is not None else None
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE side_effect_journal
                SET state = ?, result_json = ?, updated_at = ?
                WHERE idempotency_key = ?
                """,
                (SideEffectState.UNCERTAIN.value, result_json, now, idempotency_key),
            )
            conn.commit()

    def get_side_effect(self, idempotency_key: str) -> SideEffectRecord | None:
        """Fetch a side-effect journal record by key."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM side_effect_journal WHERE idempotency_key = ?",
                (idempotency_key,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return self._row_to_side_effect(row)

    def get_side_effects_for_run(self, run_id: str) -> list[SideEffectRecord]:
        """Fetch all side-effect journal records for a run."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM side_effect_journal WHERE run_id = ? ORDER BY created_at ASC",
                (run_id,),
            )
            return [self._row_to_side_effect(row) for row in cursor.fetchall()]

    def get_side_effects_for_attempt(self, attempt_id: str) -> list[SideEffectRecord]:
        """Fetch every side-effect journal record for ``attempt_id``."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM side_effect_journal WHERE attempt_id = ? "
                "ORDER BY created_at ASC",
                (attempt_id,),
            )
            return [self._row_to_side_effect(row) for row in cursor.fetchall()]

    # ------------------------------------------------------------------
    # C11-C structured trace + tool-effect evidence.
    #
    # Additive tables; the canonical event log (``events``) is unchanged
    # and remains the authoritative append-only journal for run state.
    # These rows answer the runtime / supervisor questions about trace
    # class, side-effect reconciliation, and tool operation provenance
    # that the event log alone cannot, without becoming a parallel
    # reconstruction authority.
    # ------------------------------------------------------------------

    def create_tool_effect(self, effect: ToolEffect) -> None:
        """Insert a tool-effect row for an Attempt.

        Idempotent on ``id``: a duplicate insert replaces the row instead
        of failing, so the recorder can re-write the same id when it
        transitions from STARTED to COMPLETED/FAILED/UNCERTAIN.
        """
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO tool_effects (
                    id, run_id, attempt_id, operation, capability,
                    resource_ref, side_effect_class, status,
                    idempotency_key, started_at, completed_at,
                    result_class, result_redacted,
                    reconciliation_status, error,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    completed_at = excluded.completed_at,
                    result_class = excluded.result_class,
                    result_redacted = excluded.result_redacted,
                    reconciliation_status = excluded.reconciliation_status,
                    error = excluded.error,
                    updated_at = excluded.updated_at
                """,
                (
                    effect.id,
                    effect.run_id,
                    effect.attempt_id,
                    effect.operation,
                    effect.capability,
                    effect.resource_ref,
                    effect.side_effect_class,
                    effect.status.value,
                    effect.idempotency_key,
                    effect.started_at,
                    effect.completed_at,
                    effect.result_class,
                    effect.result_redacted,
                    effect.reconciliation_status,
                    effect.error,
                    effect.created_at,
                    effect.updated_at,
                ),
            )
            conn.commit()

    def update_tool_effect(self, effect: ToolEffect) -> None:
        """Update the mutable columns of an existing tool-effect row.

        The caller supplies the same ``ToolEffect`` after mutating the
        mutable fields in place; this preserves the row's identity.
        """
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE tool_effects SET
                    status = ?,
                    completed_at = ?,
                    result_class = ?,
                    result_redacted = ?,
                    reconciliation_status = ?,
                    error = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    effect.status.value,
                    effect.completed_at,
                    effect.result_class,
                    effect.result_redacted,
                    effect.reconciliation_status,
                    effect.error,
                    effect.updated_at,
                    effect.id,
                ),
            )
            conn.commit()

    def get_tool_effect(self, effect_id: str) -> ToolEffect | None:
        """Fetch a tool-effect row by id."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM tool_effects WHERE id = ?", (effect_id,)
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return self._row_to_tool_effect(row)

    def get_tool_effects_for_attempt(self, attempt_id: str) -> list[ToolEffect]:
        """Return every tool effect recorded against an attempt, in order."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM tool_effects WHERE attempt_id = ? "
                "ORDER BY started_at ASC, id ASC",
                (attempt_id,),
            )
            return [self._row_to_tool_effect(row) for row in cursor.fetchall()]

    def get_tool_effects_for_run(self, run_id: str) -> list[ToolEffect]:
        """Return every tool effect recorded against a run, in order."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM tool_effects WHERE run_id = ? "
                "ORDER BY started_at ASC, id ASC",
                (run_id,),
            )
            return [self._row_to_tool_effect(row) for row in cursor.fetchall()]

    def create_attempt_trace(self, trace: AttemptTrace) -> None:
        """Insert a per-Attempt trace summary row.

        Idempotent on ``id`` so the recorder can re-write the same row
        when ``completed_at`` or ``tool_effects_count`` change.
        """
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO attempt_traces (
                    id, run_id, attempt_id, trace_class, source_kind,
                    started_at, updated_at, completed_at,
                    tool_effects_count, summary_json,
                    failure_kind, failure_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    trace_class = excluded.trace_class,
                    source_kind = excluded.source_kind,
                    updated_at = excluded.updated_at,
                    completed_at = excluded.completed_at,
                    tool_effects_count = excluded.tool_effects_count,
                    summary_json = excluded.summary_json,
                    failure_kind = excluded.failure_kind,
                    failure_reason = excluded.failure_reason
                """,
                (
                    trace.id,
                    trace.run_id,
                    trace.attempt_id,
                    trace.trace_class.value,
                    trace.source_kind,
                    trace.started_at,
                    trace.updated_at,
                    trace.completed_at,
                    trace.tool_effects_count,
                    trace.summary_json,
                    trace.failure_kind,
                    trace.failure_reason,
                ),
            )
            conn.commit()

    def update_attempt_trace(self, trace: AttemptTrace) -> None:
        """Update the mutable columns of an AttemptTrace row."""
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE attempt_traces SET
                    trace_class = ?,
                    source_kind = ?,
                    updated_at = ?,
                    completed_at = ?,
                    tool_effects_count = ?,
                    summary_json = ?,
                    failure_kind = ?,
                    failure_reason = ?
                WHERE id = ?
                """,
                (
                    trace.trace_class.value,
                    trace.source_kind,
                    trace.updated_at,
                    trace.completed_at,
                    trace.tool_effects_count,
                    trace.summary_json,
                    trace.failure_kind,
                    trace.failure_reason,
                    trace.id,
                ),
            )
            conn.commit()

    def get_attempt_trace(self, trace_id: str) -> AttemptTrace | None:
        """Fetch an AttemptTrace row by id."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM attempt_traces WHERE id = ?", (trace_id,)
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return self._row_to_attempt_trace(row)

    def get_attempt_trace_for_attempt(self, attempt_id: str) -> AttemptTrace | None:
        """Return the trace summary row for an Attempt, if any."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM attempt_traces WHERE attempt_id = ? "
                "ORDER BY started_at DESC LIMIT 1",
                (attempt_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return self._row_to_attempt_trace(row)

    def get_attempt_traces_for_run(self, run_id: str) -> list[AttemptTrace]:
        """Return every AttemptTrace row recorded against a run, in order."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM attempt_traces WHERE run_id = ? "
                "ORDER BY started_at ASC, id ASC",
                (run_id,),
            )
            return [self._row_to_attempt_trace(row) for row in cursor.fetchall()]

    # ------------------------------------------------------------------
    # C14-C production phase telemetry (purely observational)
    # ------------------------------------------------------------------

    def create_phase_observation(self, observation: PhaseObservation) -> None:
        """Persist one C14-C phase observation row.

        The ``PhaseObservation`` dataclass validates itself on construction
        (canonical phase/status, finite monotonic anchors, non-negative
        duration).  This method only handles the durable INSERT and never
        inspects the payload: it does not invent durations and it does not
        coerce an unobservable phase into a numeric value.
        """
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO phase_observations (
                    id, run_id, phase, status, duration_seconds, source_kind,
                    boundary_kind, attempt_id, review_id, package_id,
                    plan_step_id, started_monotonic, ended_monotonic,
                    observed_at, notes
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    observation.id,
                    observation.run_id,
                    observation.phase,
                    observation.status,
                    observation.duration_seconds,
                    observation.source_kind,
                    observation.boundary_kind,
                    observation.attempt_id,
                    observation.review_id,
                    observation.package_id,
                    observation.plan_step_id,
                    observation.started_monotonic,
                    observation.ended_monotonic,
                    observation.observed_at,
                    observation.notes,
                ),
            )
            conn.commit()

    def list_phase_observations_for_run(self, run_id: str) -> list[PhaseObservation]:
        """Return every PhaseObservation for ``run_id`` in stable order.

        Ordering is ``observed_at`` ascending with ``id`` as a deterministic
        tiebreaker so retries and repeated occurrences surface individually
        without being silently collapsed.
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM phase_observations WHERE run_id = ? "
                "ORDER BY observed_at ASC, id ASC",
                (run_id,),
            )
            return [self._row_to_phase_observation(row) for row in cursor.fetchall()]

    def get_phase_observation(self, observation_id: str) -> PhaseObservation | None:
        """Fetch one PhaseObservation row by id."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM phase_observations WHERE id = ?", (observation_id,)
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return self._row_to_phase_observation(row)

    def _row_to_phase_observation(self, row: sqlite3.Row) -> PhaseObservation:
        """Map one ``phase_observations`` row into the typed dataclass."""
        return PhaseObservation(
            id=str(row["id"]),
            run_id=str(row["run_id"]),
            phase=str(row["phase"]),
            status=str(row["status"]),
            duration_seconds=(
                float(row["duration_seconds"])
                if row["duration_seconds"] is not None
                else None
            ),
            source_kind=str(row["source_kind"]),
            boundary_kind=(
                str(row["boundary_kind"]) if row["boundary_kind"] is not None else None
            ),
            attempt_id=(str(row["attempt_id"]) if row["attempt_id"] is not None else None),
            review_id=(str(row["review_id"]) if row["review_id"] is not None else None),
            package_id=(str(row["package_id"]) if row["package_id"] is not None else None),
            plan_step_id=(
                str(row["plan_step_id"]) if row["plan_step_id"] is not None else None
            ),
            started_monotonic=(
                float(row["started_monotonic"])
                if row["started_monotonic"] is not None
                else None
            ),
            ended_monotonic=(
                float(row["ended_monotonic"]) if row["ended_monotonic"] is not None else None
            ),
            observed_at=str(row["observed_at"]),
            notes=str(row["notes"]) if row["notes"] is not None else None,
        )

    # ------------------------------------------------------------------
    # C11 Project Ledger
    #
    # The store holds durable Project truth and applies each authoritative
    # mutation atomically: the affected rows, the project's new version, and
    # the append-only event carrying the resulting state digest all commit
    # together or not at all.  Nothing here interprets the records; meaning
    # belongs to ``orchestrator_mvp.project_ledger``.
    # ------------------------------------------------------------------

    def create_project_ledger(
        self,
        *,
        project_id: str,
        canonical_path: str,
        remote_identity: str,
        root_commit: str,
        origin: str,
        genesis_json: str,
        project_version: int,
        schema_version: int,
        created_at: str,
        event: ProjectLedgerEventRow,
    ) -> bool:
        """Create the ledger row for ``project_id``; ``False`` if it exists.

        Repeated initialization is idempotent: an existing ledger is left
        exactly as it is rather than being re-created or overwritten.
        """
        with self._get_connection() as conn:
            existing = conn.execute(
                "SELECT project_id FROM project_ledger WHERE project_id = ?", (project_id,)
            ).fetchone()
            if existing is not None:
                return False
            conn.execute(
                """INSERT INTO project_ledger (
                       project_id, canonical_path, remote_identity, root_commit,
                       origin, genesis_json, project_version, schema_version,
                       created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    project_id,
                    canonical_path,
                    remote_identity,
                    root_commit,
                    origin,
                    genesis_json,
                    project_version,
                    schema_version,
                    created_at,
                    created_at,
                ),
            )
            self._append_project_ledger_event(conn, event, created_at)
            conn.commit()
        return True

    def get_project_ledger(self, project_id: str) -> dict[str, Any] | None:
        """Return the raw ledger identity row, or ``None`` when absent."""
        try:
            with self._get_connection() as conn:
                row = conn.execute(
                    "SELECT * FROM project_ledger WHERE project_id = ?", (project_id,)
                ).fetchone()
        except sqlite3.OperationalError as exc:
            if not _is_missing_project_ledger_table(exc):
                raise
            return None
        return None if row is None else {key: row[key] for key in row.keys()}

    def list_project_ledger_ids(self) -> list[str]:
        """Every project id with a durable ledger, in stable order."""
        try:
            with self._get_connection() as conn:
                rows = conn.execute(
                    "SELECT project_id FROM project_ledger ORDER BY project_id ASC"
                ).fetchall()
        except sqlite3.OperationalError as exc:
            if not _is_missing_project_ledger_table(exc):
                raise
            return []
        return [str(row["project_id"]) for row in rows]

    def apply_project_ledger_mutation(
        self,
        *,
        project_id: str,
        project_version: int,
        rows: Sequence[ProjectLedgerRow],
        event: ProjectLedgerEventRow,
    ) -> None:
        """Atomically apply one authoritative Project mutation.

        ``project_version`` is the version the project moves *to*.  It is
        applied with an optimistic guard so two concurrent sessions can never
        both claim the same version: the update matches only while the stored
        version is still the predecessor.
        """
        for row in rows:
            row.validate()
        if event.project_version != project_version:
            raise ValueError("ledger event version must match the mutation version")
        now = current_iso_timestamp()
        with self._get_connection() as conn:
            cursor = conn.execute(
                """UPDATE project_ledger SET project_version = ?, updated_at = ?
                   WHERE project_id = ? AND project_version = ?""",
                (project_version, now, project_id, project_version - 1),
            )
            if cursor.rowcount == 0:
                raise ValueError(
                    f"project '{project_id}' is not at version {project_version - 1}; "
                    "the ledger advanced concurrently"
                )
            for row in rows:
                self._write_project_ledger_row(conn, row)
            self._append_project_ledger_event(conn, event, now)
            conn.commit()

    @staticmethod
    def _write_project_ledger_row(conn: sqlite3.Connection, row: ProjectLedgerRow) -> None:
        columns = sorted(row.values)
        if row.mode == "insert":
            placeholders = ", ".join("?" for _ in columns)
            conn.execute(
                f"INSERT INTO {row.table} ({', '.join(columns)}) VALUES ({placeholders})",
                tuple(row.values[column] for column in columns),
            )
            return
        key = _PROJECT_LEDGER_KEYS[row.table]
        assignments = [column for column in columns if column != key]
        if not assignments:
            return
        setters = ", ".join(f"{column} = ?" for column in assignments)
        cursor = conn.execute(
            f"UPDATE {row.table} SET {setters} WHERE {key} = ?",
            (*(row.values[column] for column in assignments), row.values[key]),
        )
        if cursor.rowcount == 0:
            raise ValueError(f"{row.table} has no row '{row.values[key]}' to update")

    @staticmethod
    def _append_project_ledger_event(
        conn: sqlite3.Connection, event: ProjectLedgerEventRow, now: str
    ) -> None:
        conn.execute(
            """INSERT INTO project_ledger_events (
                   project_id, event_type, subject_kind, subject_id,
                   project_version, state_digest, payload_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event.project_id,
                event.event_type,
                event.subject_kind,
                event.subject_id,
                event.project_version,
                event.state_digest,
                event.payload_json,
                now,
            ),
        )

    def list_project_ledger_rows(self, table: str, project_id: str) -> list[dict[str, Any]]:
        """Return every durable row of one ledger ``table`` for a project."""
        if table not in PROJECT_LEDGER_COLUMNS or table == "project_ledger":
            raise ValueError(f"'{table}' is not a Project Ledger record table")
        try:
            with self._get_connection() as conn:
                rows = conn.execute(
                    f"SELECT * FROM {table} WHERE project_id = ? ORDER BY id ASC", (project_id,)
                ).fetchall()
        except sqlite3.OperationalError as exc:
            if not _is_missing_project_ledger_table(exc):
                raise
            return []
        return [{key: row[key] for key in row.keys()} for row in rows]

    def list_project_ledger_events(self, project_id: str) -> list[dict[str, Any]]:
        """Every append-only ledger event for ``project_id`` in durable order."""
        try:
            with self._get_connection() as conn:
                rows = conn.execute(
                    "SELECT * FROM project_ledger_events WHERE project_id = ? ORDER BY seq ASC",
                    (project_id,),
                ).fetchall()
        except sqlite3.OperationalError as exc:
            if not _is_missing_project_ledger_table(exc):
                raise
            return []
        return [{key: row[key] for key in row.keys()} for row in rows]

    def get_project_state_digest_at(self, project_id: str, project_version: int) -> str | None:
        """The state digest durably recorded when the project reached a version."""
        try:
            with self._get_connection() as conn:
                row = conn.execute(
                    """SELECT state_digest FROM project_ledger_events
                       WHERE project_id = ? AND project_version = ?
                       ORDER BY seq DESC LIMIT 1""",
                    (project_id, project_version),
                ).fetchone()
        except sqlite3.OperationalError as exc:
            if not _is_missing_project_ledger_table(exc):
                raise
            return None
        return None if row is None else str(row["state_digest"])

    def insert_project_checkpoint(
        self,
        *,
        checkpoint_id: str,
        project_id: str,
        parent_checkpoint_id: str | None,
        project_version: int,
        state_digest: str,
        checkpoint_digest: str,
        schema_version: int,
        manifest_json: str,
        created_at: str,
    ) -> None:
        """Persist one deterministic checkpoint manifest (idempotent by id)."""
        with self._get_connection() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO project_checkpoints (
                       checkpoint_id, project_id, parent_checkpoint_id, project_version,
                       state_digest, checkpoint_digest, schema_version, manifest_json,
                       created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    checkpoint_id,
                    project_id,
                    parent_checkpoint_id,
                    project_version,
                    state_digest,
                    checkpoint_digest,
                    schema_version,
                    manifest_json,
                    created_at,
                ),
            )
            conn.commit()

    def get_project_checkpoint(self, checkpoint_id: str) -> dict[str, Any] | None:
        """Return one stored checkpoint row, or ``None`` when unknown."""
        try:
            with self._get_connection() as conn:
                row = conn.execute(
                    "SELECT * FROM project_checkpoints WHERE checkpoint_id = ?", (checkpoint_id,)
                ).fetchone()
        except sqlite3.OperationalError as exc:
            if not _is_missing_project_ledger_table(exc):
                raise
            return None
        return None if row is None else {key: row[key] for key in row.keys()}

    def list_project_checkpoints(self, project_id: str) -> list[dict[str, Any]]:
        """Every checkpoint of ``project_id``, oldest first."""
        try:
            with self._get_connection() as conn:
                rows = conn.execute(
                    """SELECT * FROM project_checkpoints WHERE project_id = ?
                       ORDER BY project_version ASC, created_at ASC, checkpoint_id ASC""",
                    (project_id,),
                ).fetchall()
        except sqlite3.OperationalError as exc:
            if not _is_missing_project_ledger_table(exc):
                raise
            return []
        return [{key: row[key] for key in row.keys()} for row in rows]

    def get_latest_project_checkpoint(self, project_id: str) -> dict[str, Any] | None:
        """The newest checkpoint of ``project_id``, or ``None`` when none exist."""
        checkpoints = self.list_project_checkpoints(project_id)
        return checkpoints[-1] if checkpoints else None
