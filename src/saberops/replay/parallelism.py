"""C12-D bounded parallelism experiments over the canonical replay seam.

C12-D is an EXPERIMENT / OBSERVATION layer.  It is NOT a scheduler, NOT a
second dependency authority, NOT a second worktree/sandbox/tool authority,
and NOT a production parallelism policy.  Every piece of real execution used
by C12-D goes through the existing canonical ReplayEngine execution seam
(``ReplayEngine.execute_replay``): DisposableWorkspace lifecycle,
SandboxPolicy construction, sandbox grant/interceptor/trace setup,
``run_sandboxed_process`` invocation, deterministic verifier, workspace
mutation sentinels, authoritative-repo non-mutation invariants, and
execution-record construction all remain owned by ReplayEngine.  This module
only declares experiment arms, gates their availability fail-closed, and
post-processes the evidence the canonical seam returns.

Three declared parallelism families, measured only where honestly available:

- SEQUENTIAL BASELINE (arm A): declared work items executed one at a time
  through the canonical seam.  Always executed when a campaign runs.
- DETERMINISTIC PARALLELISM (arm B): the SAME declared work items executed
  concurrently through the same canonical seam.  This is process/check
  concurrency of independent DETERMINISTIC operations.  It is explicitly
  typed ``deterministic_process_parallelism`` and is NEVER semantic-worker
  evidence.  The arm opens only when independence is positively established
  from the declared inputs: at least two items; explicit ESTABLISHED
  independence on every item (the default is fail-closed UNKNOWN);
   complete valid canonical scope evidence on every item; NO declared
   dependency edges on any item (any non-empty dependency_item_ids —
   self, co-scheduled, undeclared, or otherwise — refuses the parallel
   arm; C12-D does not interpret dependency graphs); empty/ambiguous
   scope evidence, or exact or ancestor-descendant overlapping declared
   scope) refuses the parallel arm and execution falls back to the
   sequential baseline.
- DEPENDENCY-SAFE SEMANTIC PARALLELISM: NOT_AVAILABLE.  C11-D owns the
  dependency-aware scheduling representation (``WorkPackage.
  dependency_package_ids`` / plan-step ``dependency_ids``) and the bounded
  Attempt loops; the accepted runtime executes those loops sequentially and
  no semantic model/Orch Attempt executor participates in this bounded
  replay environment.  C12-D builds no replacement scheduler and no
  substitute semantic executor.
- SPECULATIVE SAME-TASK PARALLELISM: NOT_AVAILABLE.  A real speculative arm
  would require valid semantic execution bindings (C11-B), authorized model
  execution (C07/C08), and a multi-candidate synthesis/integration path.
  None of these exist in the current bounded replay environment and C12-D
  creates none of them.

Honesty invariants for this campaign:

- No semantic model/Orch executor participates.  Deterministic work-item
  steps executed by the canonical seam are NOT Orch/model turns:
  ``MeasurementRecord.turns_per_attempt`` stays UNKNOWN, and
  ``tool_model_round_trips``, provider/model/backend/binding, token, cost,
  reasoning, and quota metrics stay UNKNOWN.
- A deterministic work-item check PASS (strategy/verifier outcome of the
  canonical seam) is deterministic process evidence only.  No arm claims
  semantic task success: every derived C12-D observation record carries
  ``verified_success=False`` on both the record and its measurement, and the
  underlying canonical outcomes are preserved separately under explicit
  ``baseline_execution`` evidence.
- Wall-clock and per-item durations are directly observed quantities.
  Unobserved quantities are UNKNOWN, never zero, never inferred from bytes
  or wall clock.
- Security identity: as in C12-C, the canonical seam does not expose a
  positive, non-secret security-policy identity bound to the SandboxPolicy
  it actually used.  C12-D fabricates none and admits none from
  caller-written strategy evidence, so direct cross-arm comparability
  retains the explicit fail-closed security reason.
- Comparability normalizes ONLY the declared parallelism variable
  (``c12d_execution_mode``); the accepted C12-B fail-closed comparator is
  applied UNCHANGED beneath it.
- No production winner is adopted from any result.
"""

from __future__ import annotations

import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Final

from saberops.replay.comparison import (
    _positive_security_identity,
    _record_definition_inconsistencies,
    assert_comparable,
)
from saberops.replay.contracts import (
    UNKNOWN,
    ComparabilityVerdict,
    ComparisonResult,
    DeterministicVerifier,
    MeasurementRecord,
    ReplayDefinition,
    ReplayError,
    ReplayExecutionRecord,
    digest_of,
)
from saberops.replay.corpus import ReplayTaskDefinition
from saberops.replay.engine import ReplayEngine
from saberops.replay.experiment import (
    SEMANTIC_EFFECT_UNMEASURED,
    _validate_evidence_dir,
    write_campaign_evidence,
)
from saberops.replay.toolchain import (
    VERIFIER_TOOLCHAIN_PATH,
    fingerprint_or_unavailable,
)

# ---------------------------------------------------------------------------
# Dimension / vocabulary constants
# ---------------------------------------------------------------------------

#: Experimental dimension for C12-D.
DIMENSION_PARALLELISM: Final[str] = "parallelism"

#: Declared execution modes.  This is the ONLY variable C12-D normalizes away
#: when comparing arms; everything else is a mandatory control.
EXECUTION_MODE_SEQUENTIAL: Final[str] = "sequential"
EXECUTION_MODE_DETERMINISTIC_PARALLEL: Final[str] = "deterministic_parallel"

_EXECUTION_MODES: Final[tuple[str, ...]] = (
    EXECUTION_MODE_SEQUENTIAL,
    EXECUTION_MODE_DETERMINISTIC_PARALLEL,
)

#: Strategy-params key carrying the declared execution mode (the one declared
#: experimental variable).
PARAM_EXECUTION_MODE: Final[str] = "c12d_execution_mode"
#: Strategy-params key carrying the dimension marker (never normalized away,
#: so cross-dimension comparisons fail closed).
PARAM_DIMENSION: Final[str] = "c12d_dimension"
#: Strategy-params key carrying the work-item id.
PARAM_ITEM_ID: Final[str] = "c12d_item_id"
#: Strategy-params key carrying the declared work items on an arm definition.
PARAM_ITEMS: Final[str] = "c12d_items"
#: Strategy-params key carrying declared scope ownership of a work item.
PARAM_SCOPE_PATHS: Final[str] = "c12d_scope_paths"
#: Strategy-params key carrying declared dependency edges of a work item.
PARAM_DEPENDENCY_ITEM_IDS: Final[str] = "c12d_dependency_item_ids"
#: Strategy-params key carrying the declared independence status of a work item.
PARAM_INDEPENDENCE: Final[str] = "c12d_independence"
#: CANONICAL ReplayEngine command-strategy key: the engine executes
#: ``strategy_params["command"]`` through its sandboxed seam.  The declared
#: work-item step is stored under this key so the canonical seam — and
#: nothing else — owns its execution.
CANONICAL_COMMAND_KEY: Final[str] = "command"

#: Evidence-kind marker: concurrent execution of independent DETERMINISTIC
#: process/check operations.  NEVER semantic-worker evidence.
EVIDENCE_KIND_DETERMINISTIC_PARALLELISM: Final[str] = "deterministic_process_parallelism"
#: Evidence-kind marker: one-at-a-time sequential baseline execution.
EVIDENCE_KIND_SEQUENTIAL_BASELINE: Final[str] = "sequential_baseline"

#: Record-kind marker for derived C12-D observation records.
RECORD_KIND_PARALLELISM_OBSERVATION: Final[str] = "c12d_parallelism_observation"

#: Declared independence statuses of a work item.  UNKNOWN is the honest
#: declaration when independence has NOT been positively established; it
#: always refuses the parallel arm (sequential fallback).
INDEPENDENCE_ESTABLISHED: Final[str] = "ESTABLISHED"
INDEPENDENCE_UNKNOWN: Final[str] = "UNKNOWN"

_INDEPENDENCE_VALUES: Final[tuple[str, ...]] = (
    INDEPENDENCE_ESTABLISHED,
    INDEPENDENCE_UNKNOWN,
)

#: Canonical arm identifiers.
ARM_SEQUENTIAL_ID: Final[str] = "c12d_arm_sequential"
ARM_DETERMINISTIC_PARALLEL_ID: Final[str] = "c12d_arm_deterministic_parallel"
ARM_SEMANTIC_PARALLEL_ID: Final[str] = "c12d_dependency_safe_semantic_parallel"
ARM_SPECULATIVE_ID: Final[str] = "c12d_speculative_same_task"

_ITEM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


# ---------------------------------------------------------------------------
# Declared work items
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParallelWorkItem:
    """One declared deterministic work item of a C12-D experiment arm.

    A work item is a DECLARED EXPERIMENT INPUT, not a scheduler decision:
    it carries its own process step, its declared scope paths (ownership),
    its declared dependency edges (other item ids), and its declared
    independence status.  Independence must be positively declared;
    ``UNKNOWN`` independence refuses the parallel arm.
    """

    item_id: str
    #: One deterministic process step, executed by the canonical ReplayEngine
    #: command-strategy seam.  This is a PROCESS STEP, never an Orch/model turn.
    process_step: tuple[str, ...]
    #: Declared scope/ownership paths used by the independence evaluation.
    scope_paths: tuple[str, ...] = ()
    #: Declared dependency edges: other item_ids this item depends on.
    dependency_item_ids: tuple[str, ...] = ()
    #: Positively declared independence status.  Defaults to UNKNOWN
    #: (fail-closed): a caller that omits independence evidence MUST NOT
    #: automatically obtain an available deterministic-parallel arm.
    #: Parallel admission additionally requires every other structural
    #: control to pass; explicit ESTABLISHED is necessary but NOT
    #: sufficient by itself.
    independence: str = INDEPENDENCE_UNKNOWN

    def __post_init__(self) -> None:
        if not _ITEM_ID_RE.fullmatch(self.item_id):
            raise ReplayError(f"Work item id must be a plain safe token: {self.item_id!r}")
        if not self.process_step:
            raise ReplayError(f"Work item {self.item_id!r} requires a process step")
        if self.independence not in _INDEPENDENCE_VALUES:
            raise ReplayError(
                f"Work item {self.item_id!r} has unknown independence declaration: "
                f"{self.independence!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        """Canonical dictionary representation."""
        return {
            "item_id": self.item_id,
            "process_step": list(self.process_step),
            "scope_paths": sorted(self.scope_paths),
            "dependency_item_ids": sorted(self.dependency_item_ids),
            "independence": self.independence,
        }


# ---------------------------------------------------------------------------
# Experiment definition identity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParallelismExperimentDefinition:
    """Deterministic identity of one C12-D arm definition.

    Contains NO execution IDs, timestamps, or random values.  Changing the
    declared execution mode, any work item, the verifier, or any control
    changes the digest.
    """

    task_id: str
    project_key: str
    task_definition_digest: str
    frozen_start_sha: str
    arm_id: str
    execution_mode: str
    items: tuple[ParallelWorkItem, ...]
    dimension: str = DIMENSION_PARALLELISM
    verifier: DeterministicVerifier = field(default_factory=DeterministicVerifier)

    def __post_init__(self) -> None:
        if self.dimension != DIMENSION_PARALLELISM:
            raise ReplayError(
                f"Unknown C12-D experiment dimension: {self.dimension!r} "
                "(cross-dimension comparisons must fail closed)"
            )
        if self.execution_mode not in _EXECUTION_MODES:
            raise ReplayError(f"Unknown C12-D execution mode: {self.execution_mode!r}")
        if not self.items:
            raise ReplayError("A C12-D experiment arm requires at least one work item")

    def item_strategy_params(self, item: ParallelWorkItem) -> dict[str, Any]:
        """Canonical per-item strategy params embedded into a ReplayDefinition.

        The work-item step is stored under the canonical ``command`` key so
        the ReplayEngine command-strategy seam — and nothing else — owns its
        execution.  All declared controls (scope, dependencies, independence)
        are part of the digest.
        """
        params: dict[str, Any] = {
            PARAM_DIMENSION: self.dimension,
            PARAM_EXECUTION_MODE: self.execution_mode,
            PARAM_ITEM_ID: item.item_id,
            CANONICAL_COMMAND_KEY: list(item.process_step),
            PARAM_SCOPE_PATHS: sorted(item.scope_paths),
            PARAM_DEPENDENCY_ITEM_IDS: sorted(item.dependency_item_ids),
            PARAM_INDEPENDENCE: item.independence,
        }
        return dict(sorted(params.items()))

    def arm_strategy_params(self) -> dict[str, Any]:
        """Canonical arm-level strategy params embedded into a ReplayDefinition."""
        params: dict[str, Any] = {
            PARAM_DIMENSION: self.dimension,
            PARAM_EXECUTION_MODE: self.execution_mode,
            PARAM_ITEMS: [item.to_dict() for item in self.items],
        }
        return dict(sorted(params.items()))

    def to_dict(self) -> dict[str, Any]:
        """Canonical dictionary representation."""
        return {
            "task_id": self.task_id,
            "project_key": self.project_key,
            "task_definition_digest": self.task_definition_digest,
            "frozen_start_sha": self.frozen_start_sha,
            "dimension": self.dimension,
            "arm_id": self.arm_id,
            "execution_mode": self.execution_mode,
            "items": [item.to_dict() for item in self.items],
            "verifier": self.verifier.to_dict(),
        }

    @property
    def digest(self) -> str:
        """Deterministic digest of this experiment definition (no runtime IDs)."""
        return digest_of(self.to_dict())


# ---------------------------------------------------------------------------
# Independence evaluation (fail-closed gate over DECLARED inputs)
# ---------------------------------------------------------------------------


def _canonicalize_scope_path(raw: object) -> str | None:
    """Return the deterministic canonical form of one declared scope path.

    Fail-closed experiment-layer admissibility check over bounded
    deterministic diagnostic inputs only (NOT a production dependency
    subsystem): empty paths, absolute paths, traversal (``..``), and
    other ambiguous representations that prevent deterministic
    relative-scope comparison are refused (``None``) rather than
    guessed at.  The canonical form is the stripped relative POSIX
    path with no empty, ``.``, or ``..`` segments and no trailing
    slash.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    if text.startswith("/") or text.startswith("\\"):
        return None
    if "\\" in text or "\x00" in text:
        return None
    if re.fullmatch(r"[A-Za-z]:([/\\].*)?", text):
        return None
    text = text.rstrip("/")
    if not text:
        return None
    parts = text.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            return None
    return "/".join(parts)


def _scopes_conflict(canonical_a: str, canonical_b: str) -> bool:
    """Detect exact or hierarchical (ancestor/descendant) scope conflict."""
    return (
        canonical_a == canonical_b
        or canonical_a.startswith(canonical_b + "/")
        or canonical_b.startswith(canonical_a + "/")
    )


def evaluate_independence(items: tuple[ParallelWorkItem, ...]) -> tuple[bool, tuple[str, ...]]:
    """Evaluate whether declared work items may be co-scheduled concurrently.

    This is a FAIL-CLOSED gate over the declared experiment inputs.  It is
    NOT a dependency-analysis engine and owns no production dependency
    authority: production dependency representation remains owned by C11-D
    (``WorkPackage.dependency_package_ids``).  Any uncertainty refuses the
    parallel arm and execution falls back to the sequential baseline.

    Parallel admission requires ALL of the following positive structural
    controls: at least two work items; explicit ESTABLISHED independence
    on every item (omitted/UNKNOWN evidence fails closed); complete
    valid canonical scope evidence on every item; EMPTY dependency_item_ids
    on every item (any declared dependency edge — self, co-scheduled,
    undeclared, or otherwise — refuses the parallel arm; C12-D does not
    interpret dependency graphs); no duplicate item ids; and no
    overlapping or ancestor-descendant scopes.
    """
    reasons: list[str] = []
    if not items:
        reasons.append("no work items declared")
        return False, tuple(reasons)
    if len(items) < 2:
        reasons.append(
            "deterministic parallel execution requires at least two work items: "
            "a one-item arm is NOT_AVAILABLE as executed parallelism "
            "(sequential fallback required)"
        )

    ids = [item.item_id for item in items]
    if len(set(ids)) != len(ids):
        reasons.append(f"duplicate work item ids declared: {sorted(ids)}")

    canonical_scopes: dict[str, tuple[str, ...]] = {}
    for item in items:
        if item.independence != INDEPENDENCE_ESTABLISHED:
            reasons.append(
                f"work item {item.item_id!r} declares independence "
                f"{item.independence!r} (UNKNOWN by default): independence is "
                "not positively established, dependency uncertainty must "
                "fall back to sequential execution"
            )
        if not item.scope_paths:
            reasons.append(
                f"work item {item.item_id!r} declares no scope/ownership "
                "evidence: empty scope evidence fails closed, refusing "
                "the parallel arm"
            )
            canonical_scopes[item.item_id] = ()
            continue
        canonical: list[str] = []
        for raw_path in item.scope_paths:
            parsed = _canonicalize_scope_path(raw_path)
            if parsed is None:
                reasons.append(
                    f"work item {item.item_id!r} declares ambiguous or unsafe "
                    f"scope path {raw_path!r}: scope evidence must be a "
                    "canonical relative path (no empty, absolute, traversal "
                    "'..', or ambiguous segments), refusing the parallel arm"
                )
            else:
                canonical.append(parsed)
        canonical_scopes[item.item_id] = tuple(canonical)
        if item.dependency_item_ids:
            reasons.append(
                f"work item {item.item_id!r} declares dependency edges "
                f"{sorted(item.dependency_item_ids)}: deterministic parallel "
                "admission requires no declared dependency edges; C12-D does "
                "not interpret dependency graphs (self, co-scheduled, "
                "undeclared, cyclic, or transitive); sequential fallback required"
            )

    for i, item_a in enumerate(items):
        for item_b in items[i + 1 :]:
            scopes_a = canonical_scopes.get(item_a.item_id, ())
            scopes_b = canonical_scopes.get(item_b.item_id, ())
            overlap: list[str] = sorted(
                {
                    f"{path_a} <-> {path_b}"
                    for path_a in scopes_a
                    for path_b in scopes_b
                    if _scopes_conflict(path_a, path_b)
                }
            )
            if overlap:
                reasons.append(
                    f"work items {item_a.item_id!r} and {item_b.item_id!r} declare "
                    f"overlapping scope paths {overlap}: write/ownership conflict "
                    "(exact or ancestor-descendant overlap), "
                    "concurrent scheduling refused"
                )

    return (not reasons), tuple(reasons)


# ---------------------------------------------------------------------------
# Work-item execution through the canonical seam
# ---------------------------------------------------------------------------


def execute_work_item(
    engine: ReplayEngine,
    task_def: ReplayTaskDefinition,
    frozen_state: Any,
    experiment: ParallelismExperimentDefinition,
    item: ParallelWorkItem,
    *,
    evidence_dir: Path | None = None,
) -> ReplayExecutionRecord:
    """Execute ONE declared work item through the canonical ReplayEngine seam.

    ALL real execution happens inside ``ReplayEngine.execute_replay``.
    The item's verifier is the corpus task verifier, held constant across
    arms so verifier semantics remain a mandatory control.
    """
    definition = ReplayDefinition(
        task_id=task_def.task_id,
        project_key=task_def.project_key,
        frozen_start_sha=frozen_state.starting_git_sha,
        strategy_id=f"c12d_item_{item.item_id}",
        strategy_params=experiment.item_strategy_params(item),
        verifier=task_def.verifier,
        expected_artifacts=task_def.expected_artifact_shape,
        requires_external_model=task_def.requires_external_model,
        task_definition_digest=task_def.content_digest,
    )
    return engine.execute_replay(
        definition,
        starting_state=frozen_state,
        evidence_dir=evidence_dir,
    )


# ---------------------------------------------------------------------------
# Arm observation records (post-processing only)
# ---------------------------------------------------------------------------


def _item_evidence(item: ParallelWorkItem, record: ReplayExecutionRecord) -> dict[str, Any]:
    """Extract deterministic per-item evidence from one canonical record."""
    strategy = dict(record.strategy_evidence or {})
    verifier = dict(record.verifier_evidence or {})
    measurement = record.measurement
    return {
        "item_id": item.item_id,
        "execution_id": record.execution_id,
        "deterministic_check_passed": bool(strategy.get("passed", False)),
        "deterministic_check_exit_code": strategy.get("exit_code"),
        "deterministic_check_seconds": strategy.get("duration_seconds"),
        "verifier_passed": verifier.get("passed"),
        "canonical_verified_success": bool(record.verified_success),
        "tool_execution_seconds": (measurement.tool_execution_seconds if measurement else None),
        "deterministic_validation_seconds": (
            measurement.deterministic_validation_seconds if measurement else None
        ),
        "workspace_result_digest": record.workspace_result_digest,
        "evidence_basis": (
            "Deterministic process/check outcome of the canonical "
            "ReplayEngine seam.  NOT semantic-worker evidence and NOT "
            "semantic task success."
        ),
    }


def _build_arm_observation_record(
    task_def: ReplayTaskDefinition,
    frozen_state: Any,
    experiment: ParallelismExperimentDefinition,
    item_records: tuple[tuple[ParallelWorkItem, ReplayExecutionRecord], ...],
    *,
    wall_clock_seconds: float,
    evidence_kind: str,
    independence: dict[str, Any],
) -> ReplayExecutionRecord:
    """Build the derived arm-level observation record with real frozen controls.

    Evidence integrity: no semantic executor performed the corpus task
    objective, so the derived C12-D observation record carries
    ``verified_success=False`` on the record AND its measurement.  The
    underlying canonical outcomes are preserved separately as explicit
    deterministic process evidence and never masquerade as arm success.
    """
    definition = ReplayDefinition(
        task_id=task_def.task_id,
        project_key=task_def.project_key,
        frozen_start_sha=frozen_state.starting_git_sha,
        strategy_id=experiment.arm_id,
        strategy_params=experiment.arm_strategy_params(),
        verifier=task_def.verifier,
        expected_artifacts=task_def.expected_artifact_shape,
        requires_external_model=task_def.requires_external_model,
        task_definition_digest=task_def.content_digest,
        frozen_starting_state=frozen_state,
        checkpoint_id=frozen_state.checkpoint_id,
        state_digest=frozen_state.state_digest,
    )

    tool_seconds = 0.0
    validation_seconds = 0.0
    for _, record in item_records:
        m = record.measurement
        if m is not None:
            if m.tool_execution_seconds is not None:
                tool_seconds += m.tool_execution_seconds
            if m.deterministic_validation_seconds is not None:
                validation_seconds += m.deterministic_validation_seconds

    measurement = MeasurementRecord(
        # Arm success is NEVER claimed: no semantic executor participated.
        verified_success=False,
        # Directly observed quantities only:
        total_wall_clock_seconds=round(wall_clock_seconds, 4),
        tool_execution_seconds=round(tool_seconds, 4),
        deterministic_validation_seconds=round(validation_seconds, 4),
        verifier_toolchain_fingerprint=fingerprint_or_unavailable(),
        # All semantic metrics remain UNKNOWN (None serializes to UNKNOWN):
    )

    parallel_mode = experiment.execution_mode == EXECUTION_MODE_DETERMINISTIC_PARALLEL
    observation: dict[str, Any] = {
        "record_kind": RECORD_KIND_PARALLELISM_OBSERVATION,
        "evidence_kind": evidence_kind,
        "evidence_kind_basis": (
            "Deterministic process/check evidence from the canonical "
            "ReplayEngine seam.  Concurrent deterministic checks are NOT "
            "semantic-worker parallelism and must never be relabeled."
        ),
        "semantic_effect": SEMANTIC_EFFECT_UNMEASURED,
        "semantic_effect_basis": (
            "No semantic model/Orch executor performed the task objective; "
            "deterministic check PASS results are process evidence only."
        ),
        "experiment_definition_digest": experiment.digest,
        "execution_mode": experiment.execution_mode,
        "work_item_count": len(experiment.items),
        "declared_max_concurrency": len(experiment.items) if parallel_mode else 1,
        "independence_evaluation": independence,
        "wall_clock_seconds": round(wall_clock_seconds, 4),
        "items": [_item_evidence(item, record) for item, record in item_records],
        "semantic_metrics": {
            "turns_per_attempt": UNKNOWN,
            "tool_model_round_trips": UNKNOWN,
            "provider": UNKNOWN,
            "model": UNKNOWN,
            "backend": UNKNOWN,
            "exact_binding": UNKNOWN,
            "tokens": UNKNOWN,
            "reasoning_tokens": UNKNOWN,
            "quota_consumption": UNKNOWN,
            "note": (
                "No semantic executor participated; these metrics are "
                "UNKNOWN, never zero, never inferred from wall clock or bytes."
            ),
        },
        "arm_verified_success": False,
        "arm_verified_success_basis": (
            "No semantic executor performed the corpus task objective, so "
            "the derived C12-D observation arm is verified_success=False "
            "even when every deterministic check passed."
        ),
    }
    observation["baseline_execution"] = {
        "deterministic_items_all_passed": all(
            ev["deterministic_check_passed"] and ev["canonical_verified_success"]
            for ev in observation["items"]
        ),
        "item_count": len(item_records),
        "note": (
            "Underlying canonical ReplayEngine outcomes, preserved "
            "separately as deterministic process evidence.  They are NOT "
            "the semantic success of this C12-D observation arm."
        ),
    }

    return ReplayExecutionRecord(
        execution_id=f"c12d_arm_{uuid.uuid4().hex[:12]}",
        task_id=task_def.task_id,
        project_key=task_def.project_key,
        definition_digest=definition.definition_digest,
        strategy_id=experiment.arm_id,
        start_time="",
        starting_sha=frozen_state.starting_git_sha,
        frozen_starting_state=frozen_state,
        frozen_state_digest=frozen_state.digest,
        task_definition_digest=task_def.content_digest,
        effective_definition=definition,
        measurement=measurement,
        strategy_evidence=observation,
    )


# ---------------------------------------------------------------------------
# Arm runners
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParallelismArmResult:
    """One arm's result inside the bounded C12-D campaign."""

    dimension: str
    arm_id: str
    execution_mode: str
    evidence_kind: str | None
    available: bool
    not_available_reason: str | None
    experiment_digest: str | None
    wall_clock_seconds: float | None
    record: ReplayExecutionRecord | None
    item_records: tuple[tuple[str, ReplayExecutionRecord], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Canonical dictionary representation."""
        return {
            "dimension": self.dimension,
            "arm_id": self.arm_id,
            "execution_mode": self.execution_mode,
            "evidence_kind": self.evidence_kind,
            "available": self.available,
            "not_available_reason": self.not_available_reason,
            "experiment_definition_digest": self.experiment_digest,
            "wall_clock_seconds": self.wall_clock_seconds,
            "record": self.record.to_dict() if self.record else None,
            "item_records": {item_id: record.to_dict() for item_id, record in self.item_records},
        }


def run_sequential_arm(
    engine: ReplayEngine,
    task_def: ReplayTaskDefinition,
    frozen_state: Any,
    experiment: ParallelismExperimentDefinition,
    *,
    evidence_dir: Path | None = None,
) -> ParallelismArmResult:
    """ARM A — sequential baseline: declared work items one at a time."""
    if experiment.execution_mode != EXECUTION_MODE_SEQUENTIAL:
        raise ReplayError("run_sequential_arm requires the sequential execution mode")
    t0 = time.perf_counter()
    item_records: list[tuple[ParallelWorkItem, ReplayExecutionRecord]] = []
    for item in experiment.items:
        record = execute_work_item(
            engine, task_def, frozen_state, experiment, item, evidence_dir=evidence_dir
        )
        item_records.append((item, record))
    wall = time.perf_counter() - t0

    record = _build_arm_observation_record(
        task_def,
        frozen_state,
        experiment,
        tuple(item_records),
        wall_clock_seconds=wall,
        evidence_kind=EVIDENCE_KIND_SEQUENTIAL_BASELINE,
        independence={
            "evaluated": False,
            "note": (
                "Sequential baseline: independence is not required for one-at-a-time execution."
            ),
        },
    )
    return ParallelismArmResult(
        dimension=experiment.dimension,
        arm_id=experiment.arm_id,
        execution_mode=experiment.execution_mode,
        evidence_kind=EVIDENCE_KIND_SEQUENTIAL_BASELINE,
        available=True,
        not_available_reason=None,
        experiment_digest=experiment.digest,
        wall_clock_seconds=round(wall, 4),
        record=record,
        item_records=tuple((item.item_id, rec) for item, rec in item_records),
    )


def run_deterministic_parallel_arm(
    engine: ReplayEngine,
    task_def: ReplayTaskDefinition,
    frozen_state: Any,
    experiment: ParallelismExperimentDefinition,
    *,
    evidence_dir: Path | None = None,
) -> ParallelismArmResult:
    """ARM B — deterministic parallel: the SAME items executed concurrently.

    This is process/check concurrency of independent DETERMINISTIC
    operations through the canonical seam.  It is explicitly typed
    ``deterministic_process_parallelism`` and is NEVER semantic-worker
    evidence.  The arm refuses to open unless independence is positively
    established from the declared inputs.
    """
    if experiment.execution_mode != EXECUTION_MODE_DETERMINISTIC_PARALLEL:
        raise ReplayError("run_deterministic_parallel_arm requires the deterministic_parallel mode")
    independent, reasons = evaluate_independence(experiment.items)
    if not independent:
        raise ReplayError(
            "Refusing deterministic parallel arm: independence not positively "
            "established (" + "; ".join(reasons) + "). Sequential fallback required."
        )

    independence_evidence = {
        "evaluated": True,
        "established": True,
        "declared_inputs_only": True,
        "basis": (
            "Independence evaluated over DECLARED experiment inputs only "
            "(item dependency edges, declared scope ownership, declared "
            "independence status).  Not a dependency-analysis engine and "
            "not a production scheduling decision."
        ),
    }

    t0 = time.perf_counter()
    item_records: list[tuple[ParallelWorkItem, ReplayExecutionRecord]] = []
    with ThreadPoolExecutor(max_workers=len(experiment.items)) as pool:
        futures = [
            (
                item,
                pool.submit(
                    execute_work_item,
                    engine,
                    task_def,
                    frozen_state,
                    experiment,
                    item,
                    evidence_dir=evidence_dir,
                ),
            )
            for item in experiment.items
        ]
        for item, future in futures:
            try:
                record = future.result()
            except Exception as exc:
                raise ReplayError(
                    f"Concurrent canonical execution of work item {item.item_id!r} failed: {exc}"
                ) from exc
            item_records.append((item, record))
    wall = time.perf_counter() - t0

    record = _build_arm_observation_record(
        task_def,
        frozen_state,
        experiment,
        tuple(item_records),
        wall_clock_seconds=wall,
        evidence_kind=EVIDENCE_KIND_DETERMINISTIC_PARALLELISM,
        independence=independence_evidence,
    )
    return ParallelismArmResult(
        dimension=experiment.dimension,
        arm_id=experiment.arm_id,
        execution_mode=experiment.execution_mode,
        evidence_kind=EVIDENCE_KIND_DETERMINISTIC_PARALLELISM,
        available=True,
        not_available_reason=None,
        experiment_digest=experiment.digest,
        wall_clock_seconds=round(wall, 4),
        record=record,
        item_records=tuple((item.item_id, rec) for item, rec in item_records),
    )


# ---------------------------------------------------------------------------
# Semantic parallelism availability (honest NOT_AVAILABLE)
# ---------------------------------------------------------------------------


def dependency_safe_semantic_parallel_status() -> tuple[bool, str]:
    """Report dependency-safe semantic parallel arm availability honestly.

    C11-D owns the dependency-aware scheduling representation and the
    bounded Attempt loops; the accepted runtime executes those loops
    sequentially and no semantic model/Orch Attempt executor participates
    in this bounded replay environment.  Concurrent Attempts are
    representable, not executable here.  C12-D builds no replacement
    scheduler and no substitute semantic executor.
    """
    return (
        False,
        (
            "No semantic model/Orch Attempt executor participates in this "
            "bounded replay environment.  C11-D owns the dependency-aware "
            "scheduling representation (WorkPackage dependency edges) and "
            "bounded Attempt loops; the accepted runtime executes them "
            "sequentially, so concurrent semantic Attempts are representable "
            "but not executable here.  C12-D builds no replacement scheduler "
            "and no substitute semantic executor; deterministic process "
            "concurrency is NOT a substitute for semantic-worker "
            "parallelism."
        ),
    )


def speculative_same_task_parallel_status() -> tuple[bool, str]:
    """Report speculative same-task arm availability honestly.

    A real speculative arm (one strong worker vs candidates + selection vs
    verifier) requires valid semantic execution bindings (C11-B),
    authorized model execution (C07/C08), and a synthesis/integration path
    with its own resource accounting.  None of these exist in the current
    bounded replay environment and C12-D creates no new provider executor,
    multi-model selection authority, or synthesis authority.
    """
    return (
        False,
        (
            "No valid semantic execution binding or semantic executor "
            "participates in this bounded replay environment.  A real "
            "speculative same-task arm would require valid C11-B execution "
            "bindings, authorized C07/C08 model execution, and a "
            "multi-candidate synthesis/integration path with real "
            "resource accounting; C12-D creates no new provider executor, "
            "no multi-model selection authority, and no synthesis "
            "authority.  Recorded NOT_AVAILABLE rather than manufactured."
        ),
    )


# ---------------------------------------------------------------------------
# Dimension-aware comparability (C12-B layer NOT weakened)
# ---------------------------------------------------------------------------


def assert_parallelism_comparable(
    record_a: ReplayExecutionRecord,
    record_b: ReplayExecutionRecord,
) -> ComparisonResult:
    """Compare two C12-D records varying ONLY the declared execution mode.

    The accepted C12-B fail-closed comparability layer is applied UNCHANGED
    to mode-normalized records.  Normalization removes ONLY
    ``c12d_execution_mode``; the dimension marker is retained so
    cross-dimension comparisons fail closed.  Security comparability
    requires POSITIVE policy identity bound to the actual canonical
    execution contract on BOTH records; the canonical seam exposes no such
    identity, so every current C12-D cross-arm comparison retains the
    explicit fail-closed security reason (NOT_DIRECTLY_COMPARABLE).
    """
    for label, rec in (("record_a", record_a), ("record_b", record_b)):
        issues = _record_definition_inconsistencies(rec)
        if issues:
            raise ReplayError(f"{label} is internally inconsistent: " + "; ".join(issues))

    extra_reasons: list[str] = []

    for label, rec in (("record_a", record_a), ("record_b", record_b)):
        eff = rec.effective_definition
        if eff is None:
            continue
        declared_dim = eff.strategy_params.get(PARAM_DIMENSION)
        if declared_dim != DIMENSION_PARALLELISM:
            extra_reasons.append(
                f"{label} declares dimension {declared_dim!r}, not "
                f"{DIMENSION_PARALLELISM!r}: cross-dimension comparison "
                "fails closed"
            )

    identity_a = _positive_security_identity(record_a)
    identity_b = _positive_security_identity(record_b)
    if identity_a is None or identity_b is None:
        missing = [
            label
            for label, identity in (
                ("record_a", identity_a),
                ("record_b", identity_b),
            )
            if identity is None
        ]
        extra_reasons.append(
            "security identity not positively established on "
            f"{', '.join(missing)}: the canonical execution seam exposes no "
            "policy identity bound to the SandboxPolicy it used, so this "
            "control fails closed (equality of absences is not equivalence)"
        )
    elif identity_a != identity_b:
        extra_reasons.append(f"security identity mismatch: {identity_a!r} vs {identity_b!r}")

    def _normalize(rec: ReplayExecutionRecord) -> ReplayExecutionRecord:
        eff = rec.effective_definition
        if eff is None:
            return rec
        params = {k: v for k, v in eff.strategy_params.items() if k != PARAM_EXECUTION_MODE}
        norm_def = replace(eff, strategy_params=params)
        return replace(rec, effective_definition=norm_def)

    base = assert_comparable(
        _normalize(record_a),
        _normalize(record_b),
        allow_different_arm=True,
    )
    reasons = [*base.reasons, *extra_reasons]
    verdict = (
        ComparabilityVerdict.COMPARABLE
        if not reasons
        else ComparabilityVerdict.NOT_DIRECTLY_COMPARABLE
    )
    return ComparisonResult(verdict=verdict, reasons=tuple(reasons))


# ---------------------------------------------------------------------------
# Declared C12-D work items per corpus task
# ---------------------------------------------------------------------------


#: Declared deterministic work items for the C12-D campaign.  Items declare
#: their own process step, scope ownership, dependency edges, and
#: independence status.  These are deterministic diagnostic checks (NOT
#: Orch/model turns), disjoint by declared scope.  The engine executes
#: strategy steps with a hermetic environment, so each step explicitly
#: resolves its dependencies from the same dedicated verifier toolchain the
#: canonical engine's verifier environment uses (never from the host
#: site-packages).
def _pytest_check_step(test_node: str) -> tuple[str, ...]:
    """Build one deterministic pytest check step over the verifier toolchain."""
    return (
        "/usr/bin/python3",
        "-c",
        (
            f"import sys; sys.path.insert(0, {VERIFIER_TOOLCHAIN_PATH!r}); "
            "sys.path.insert(0, 'src'); "
            "import pytest; "
            "raise SystemExit(pytest.main(['-q', "
            "'-o', 'cache_dir=/tmp/.pytest_cache', "
            f"'{test_node}']))"
        ),
    )


_DECLARED_WORK_ITEMS: Final[dict[str, tuple[ParallelWorkItem, ...]]] = {
    "orch-v2-task-01-bounded-change": (
        ParallelWorkItem(
            item_id="check_projects",
            process_step=_pytest_check_step(
                "tests/test_projects.py::test_valid_git_path_accepted_and_remembered"
            ),
            scope_paths=("src/saberops/projects.py", "tests/test_projects.py"),
            independence=INDEPENDENCE_ESTABLISHED,
        ),
        ParallelWorkItem(
            item_id="check_process",
            process_step=_pytest_check_step("tests/test_process.py::test_process_success"),
            scope_paths=("src/saberops/process.py", "tests/test_process.py"),
            independence=INDEPENDENCE_ESTABLISHED,
        ),
    ),
}


def declared_work_items(task_id: str) -> tuple[ParallelWorkItem, ...]:
    """Return the declared C12-D work items for one corpus task."""
    items = _DECLARED_WORK_ITEMS.get(task_id)
    if items is None:
        raise ReplayError(f"No C12-D work items declared for task {task_id!r}")
    return items


# ---------------------------------------------------------------------------
# Bounded campaign
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskParallelismEvidence:
    """All C12-D arm evidence for one corpus task."""

    task_id: str
    task_definition_digest: str
    frozen_controls: dict[str, Any]
    toolchain_fingerprint: str
    arm_results: tuple[ParallelismArmResult, ...]
    comparison_results: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        """Canonical dictionary representation."""
        return {
            "task_id": self.task_id,
            "task_definition_digest": self.task_definition_digest,
            "frozen_controls": self.frozen_controls,
            "toolchain_fingerprint": self.toolchain_fingerprint,
            "arm_results": [a.to_dict() for a in self.arm_results],
            "comparison_results": list(self.comparison_results),
        }


@dataclass(frozen=True)
class ParallelismExperimentResult:
    """Aggregate result of the bounded C12-D campaign."""

    task_evidence: tuple[TaskParallelismEvidence, ...]
    toolchain_fingerprint: str
    summary: str
    evidence_manifest: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Canonical dictionary representation."""
        return {
            "c12d_experiment_result": True,
            "toolchain_fingerprint": self.toolchain_fingerprint,
            "tasks": [t.to_dict() for t in self.task_evidence],
            "summary": self.summary,
            "evidence_manifest": self.evidence_manifest,
        }


def _comparisons_for_task(
    sequential: ParallelismArmResult,
    parallel: ParallelismArmResult | None,
) -> list[dict[str, Any]]:
    """One-variable-at-a-time comparisons (sequential vs deterministic parallel)."""
    results: list[dict[str, Any]] = []
    if parallel is None or parallel.record is None:
        return results
    assert sequential.record is not None
    arm_cmp = assert_parallelism_comparable(sequential.record, parallel.record)
    results.append(
        {
            "scope": "arm",
            "arm_a": sequential.arm_id,
            "arm_b": parallel.arm_id,
            "dimension": DIMENSION_PARALLELISM,
            "verdict": arm_cmp.verdict.value,
            "comparable": arm_cmp.comparable,
            "reasons": list(arm_cmp.reasons),
        }
    )
    seq_items = dict(sequential.item_records)
    par_items = dict(parallel.item_records)
    for item_id in seq_items:
        if item_id in par_items:
            item_cmp = assert_parallelism_comparable(seq_items[item_id], par_items[item_id])
            results.append(
                {
                    "scope": "work_item",
                    "item_id": item_id,
                    "arm_a": sequential.arm_id,
                    "arm_b": parallel.arm_id,
                    "dimension": DIMENSION_PARALLELISM,
                    "verdict": item_cmp.verdict.value,
                    "comparable": item_cmp.comparable,
                    "reasons": list(item_cmp.reasons),
                }
            )
    return results


def run_c12d_experiments(
    repo_path: Path | str,
    *,
    evidence_dir: Path | str | None = None,
    task_ids: tuple[str, ...] | None = None,
) -> ParallelismExperimentResult:
    """Run the bounded C12-D parallelism campaign on real frozen tasks.

    Campaign shape (bounded; observation only):
    - Sequential baseline: declared deterministic work items executed
      one-at-a-time through the canonical ReplayEngine seam.
    - Deterministic parallel arm: the SAME declared items executed
      concurrently through the same canonical seam, ONLY when independence
      is positively established from the declared inputs; explicitly typed
      deterministic process evidence.  On any dependency uncertainty the
      arm is NOT_AVAILABLE and the sequential baseline is the executed
      fallback.
    - Dependency-safe semantic parallel arm: NOT_AVAILABLE (no semantic
      executor participates; C11-D owns scheduling).
    - Speculative same-task arm: NOT_AVAILABLE (no valid semantic
      binding/executor; no substitute authority created).

    No external model is invoked.  No production winner is adopted.
    """
    repo = Path(repo_path).resolve()
    evidence_path: Path | None = None
    if evidence_dir is not None:
        evidence_path = Path(evidence_dir).resolve()
        _validate_evidence_dir(evidence_path, repo)

    engine = ReplayEngine(repo)
    toolchain_fp = fingerprint_or_unavailable()

    from saberops.replay.corpus import get_corpus_task

    selected_ids = task_ids or ("orch-v2-task-01-bounded-change",)
    tasks: list[ReplayTaskDefinition] = []
    for task_id in selected_ids:
        task = get_corpus_task(task_id)
        if task is None:
            raise ReplayError(f"Unknown corpus task ID: {task_id}")
        tasks.append(task)

    task_evidence: list[TaskParallelismEvidence] = []
    for task_def in tasks:
        items = declared_work_items(task_def.task_id)
        frozen_state = engine.freeze_starting_state(
            task_def.project_key,
            starting_sha=task_def.frozen_start_ref,
        )
        frozen_controls = {
            **frozen_state.to_dict(),
            "frozen_state_digest": frozen_state.digest,
        }

        sequential_def = ParallelismExperimentDefinition(
            task_id=task_def.task_id,
            project_key=task_def.project_key,
            task_definition_digest=task_def.content_digest,
            frozen_start_sha=frozen_state.starting_git_sha,
            arm_id=ARM_SEQUENTIAL_ID,
            execution_mode=EXECUTION_MODE_SEQUENTIAL,
            items=items,
            verifier=task_def.verifier,
        )
        sequential_arm = run_sequential_arm(
            engine, task_def, frozen_state, sequential_def, evidence_dir=evidence_path
        )

        independent, independence_reasons = evaluate_independence(items)
        parallel_arm: ParallelismArmResult | None = None
        if independent:
            parallel_def = ParallelismExperimentDefinition(
                task_id=task_def.task_id,
                project_key=task_def.project_key,
                task_definition_digest=task_def.content_digest,
                frozen_start_sha=frozen_state.starting_git_sha,
                arm_id=ARM_DETERMINISTIC_PARALLEL_ID,
                execution_mode=EXECUTION_MODE_DETERMINISTIC_PARALLEL,
                items=items,
                verifier=task_def.verifier,
            )
            parallel_arm = run_deterministic_parallel_arm(
                engine, task_def, frozen_state, parallel_def, evidence_dir=evidence_path
            )
        else:
            parallel_arm = ParallelismArmResult(
                dimension=DIMENSION_PARALLELISM,
                arm_id=ARM_DETERMINISTIC_PARALLEL_ID,
                execution_mode=EXECUTION_MODE_DETERMINISTIC_PARALLEL,
                evidence_kind=None,
                available=False,
                not_available_reason=(
                    "Independence not positively established from declared "
                    "inputs; sequential fallback is the executed baseline. "
                    "Reasons: " + "; ".join(independence_reasons)
                ),
                experiment_digest=None,
                wall_clock_seconds=None,
                record=None,
            )

        semantic_available, semantic_reason = dependency_safe_semantic_parallel_status()
        semantic_arm = ParallelismArmResult(
            dimension=DIMENSION_PARALLELISM,
            arm_id=ARM_SEMANTIC_PARALLEL_ID,
            execution_mode=EXECUTION_MODE_DETERMINISTIC_PARALLEL,
            evidence_kind=None,
            available=semantic_available,
            not_available_reason=semantic_reason,
            experiment_digest=None,
            wall_clock_seconds=None,
            record=None,
        )
        speculative_available, speculative_reason = speculative_same_task_parallel_status()
        speculative_arm = ParallelismArmResult(
            dimension=DIMENSION_PARALLELISM,
            arm_id=ARM_SPECULATIVE_ID,
            execution_mode=EXECUTION_MODE_DETERMINISTIC_PARALLEL,
            evidence_kind=None,
            available=speculative_available,
            not_available_reason=speculative_reason,
            experiment_digest=None,
            wall_clock_seconds=None,
            record=None,
        )

        arm_results = (
            sequential_arm,
            parallel_arm,
            semantic_arm,
            speculative_arm,
        )
        comparison_results = tuple(_comparisons_for_task(sequential_arm, parallel_arm))

        task_evidence.append(
            TaskParallelismEvidence(
                task_id=task_def.task_id,
                task_definition_digest=task_def.content_digest,
                frozen_controls=frozen_controls,
                toolchain_fingerprint=fingerprint_or_unavailable(),
                arm_results=arm_results,
                comparison_results=comparison_results,
            )
        )

    summary = _build_summary(task_evidence, toolchain_fp)
    result = ParallelismExperimentResult(
        task_evidence=tuple(task_evidence),
        toolchain_fingerprint=toolchain_fp,
        summary=summary,
    )

    if evidence_dir is not None:
        manifest = write_experiment_evidence(result, Path(evidence_dir))
        result = ParallelismExperimentResult(
            task_evidence=result.task_evidence,
            toolchain_fingerprint=result.toolchain_fingerprint,
            summary=result.summary,
            evidence_manifest=manifest,
        )
    return result


def _build_summary(
    task_evidence: list[TaskParallelismEvidence],
    toolchain_fp: str,
) -> str:
    """Human-readable observation-only campaign summary."""
    lines: list[str] = [
        "BOUNDED PARALLELISM CAMPAIGN — OBSERVATIONS ONLY",
        "=" * 54,
        "",
        f"Toolchain fingerprint: {toolchain_fp}",
        f"Tasks evaluated: {len(task_evidence)}",
        "No external model was invoked: model/token/quota/binding metrics are UNKNOWN.",
        "",
    ]

    for task_ev in task_evidence:
        lines.append(f"Task: {task_ev.task_id}")
        fc = task_ev.frozen_controls
        lines.append(f"  project_id (canonical): {fc.get('project_id', '')}")
        lines.append(f"  frozen start: {fc.get('starting_git_sha', '')[:12]}")
        lines.append(f"  frozen_state_digest: {fc.get('frozen_state_digest', '')[:16]}...")
        for arm in task_ev.arm_results:
            if not arm.available:
                lines.append(
                    f"  ARM {arm.arm_id}: NOT_AVAILABLE — {arm.not_available_reason or ''}"
                )
                continue
            assert arm.record is not None and arm.record.strategy_evidence is not None
            obs = arm.record.strategy_evidence
            lines.append(
                f"  ARM {arm.arm_id}: evidence_kind={arm.evidence_kind} "
                f"items={obs.get('work_item_count')} "
                f"max_concurrency={obs.get('declared_max_concurrency')} "
                f"wall_clock={obs.get('wall_clock_seconds')}s "
                f"semantic_effect={obs.get('semantic_effect')} "
                f"turns=UNKNOWN (no semantic executor)"
            )
        for cmp_result in task_ev.comparison_results:
            label = (
                f"item {cmp_result['item_id']}"
                if cmp_result.get("scope") == "work_item"
                else "arm-level"
            )
            lines.append(
                f"  COMPARE {label} {cmp_result['arm_a']} vs {cmp_result['arm_b']} "
                f"[{cmp_result['dimension']}]: {cmp_result['verdict']}"
            )
            if not cmp_result["comparable"] and cmp_result["reasons"]:
                lines.append(f"    reasons: {'; '.join(cmp_result['reasons'])}")
        lines.append("")

    lines.extend(
        [
            "OBSERVATIONS (no production winner installed, none recommended):",
            "- DETERMINISTIC PARALLELISM: measured only where actual independent "
            "deterministic operations ran concurrently through the canonical seam. "
            "This is deterministic process/check evidence, explicitly typed "
            "deterministic_process_parallelism; it is NOT semantic-worker "
            "parallelism.",
            "- DEPENDENCY-SAFE SEMANTIC PARALLELISM: NOT_AVAILABLE / UNMEASURED — "
            "no semantic model/Orch Attempt executor participated in this bounded "
            "replay environment; concurrent Attempts are representable by execution "
            "ownership but not executable here, and this campaign builds no replacement "
            "scheduler.",
            "- SPECULATIVE SAME-TASK PARALLELISM: NOT_AVAILABLE / UNMEASURED — no "
            "valid semantic execution binding or executor participates; no new "
            "provider executor, selection, or synthesis authority was created.",
            "- Dependency uncertainty refuses the parallel arm and falls back to "
            "the sequential baseline (fail closed).",
            "- Direct cross-arm comparability fails closed: the canonical "
            "execution seam does not expose positive security-policy identity, "
            "and equality of absences is not equivalence.",
            "- Semantic metrics (turns, round trips, tokens, bindings, quota) are "
            "UNKNOWN everywhere: unmeasured, never zero, never inferred.",
            "",
            "NO PARALLEL STRATEGY PROVEN SUPERIOR YET.",
            "NO PRODUCTION PARALLELISM WINNER ADOPTED.",
        ]
    )
    return "\n".join(lines)


def write_experiment_evidence(
    result: ParallelismExperimentResult, evidence_dir: Path
) -> dict[str, Any]:
    """Write JSON evidence, summary, and SHA-256 manifest."""
    return write_campaign_evidence(
        result.to_dict(),
        result.summary,
        evidence_dir,
        json_name="c12d_experiment_evidence.json",
        summary_name="c12d_experiment_summary.txt",
        manifest_name="c12d_manifest.json",
        manifest_key="c12d_campaign_manifest",
    )
