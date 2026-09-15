"""Durable contracts and measurement schema for C12-A real-project replay.

C12-A answers exactly one architectural question:
Can Orch reproducibly replay the same real Project task from the same durable
starting state and produce trustworthy comparable measurements without
mutating authoritative Project truth?

Core Invariants:
1. Replay evidence is evidence, never authoritative Project truth.
2. Deleting all replay data must leave Project state reconstructable.
3. Content identity is strictly separated from execution identity.
4. Unavailable metrics remain explicitly UNKNOWN rather than zero or fabricated.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

#: Sentinel string denoting an unavailable or unmeasured metric value.
UNKNOWN: Final[str] = "UNKNOWN"


def is_unknown(value: Any) -> bool:
    """Return True if value is None or the explicit UNKNOWN sentinel."""
    return value is None or value == UNKNOWN


def canonical_json(payload: object) -> str:
    """Render ``payload`` as stable canonical JSON (sorted keys, compact)."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest_of(payload: object) -> str:
    """Return the sha256 hex digest of the canonical JSON rendering."""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


class ReplayError(RuntimeError):
    """Base error for all replay subsystem failures."""


class ReplaySafetyViolationError(ReplayError):
    """Raised when a replay operation attempts to violate repository or ledger safety."""


class ReplayProjectMismatchError(ReplayError):
    """Raised when target repository fails project identity or history verification."""


class ReplayStateMismatchError(ReplayError):
    """Raised when starting state or git SHA does not match expectation."""


class ReplayVerificationError(ReplayError):
    """Raised when deterministic verification of a replay fails."""


class ToolchainComparabilityError(ReplayError):
    """Raised when attempting to compare runs with mismatched toolchain fingerprints."""


class FailureClassification(StrEnum):
    """Certification and replay failure classifications (Section 14)."""

    CANDIDATE_DEFECT = "CANDIDATE_DEFECT"
    PREEXISTING_TEST_DEFECT = "PREEXISTING_TEST_DEFECT"
    NONDETERMINISTIC_TEST = "NONDETERMINISTIC_TEST"
    INFRASTRUCTURE_ABORT = "INFRASTRUCTURE_ABORT"


class ComparabilityVerdict(StrEnum):
    """Verdict for a cross-run comparison (Section 13)."""

    COMPARABLE = "COMPARABLE"
    NOT_DIRECTLY_COMPARABLE = "NOT_DIRECTLY_COMPARABLE"


@dataclass(frozen=True)
class ComparisonResult:
    """Result of asserting comparability between two replay execution records.

    Two results may be directly compared only when all required controls match
    (Section 13). Mismatched controls produce NOT_DIRECTLY_COMPARABLE rather
    than silent aggregation.
    """

    verdict: ComparabilityVerdict
    reasons: tuple[str, ...] = ()

    @property
    def comparable(self) -> bool:
        """True iff the verdict is COMPARABLE."""
        return self.verdict == ComparabilityVerdict.COMPARABLE

    def to_dict(self) -> dict[str, Any]:
        """Canonical dictionary representation."""
        return {
            "verdict": self.verdict.value,
            "comparable": self.comparable,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class DeterministicVerifier:
    """Specification for deterministic verification of a replay arm."""

    verifier_type: str = "command"
    command: tuple[str, ...] = ()
    expected_exit_code: int = 0
    expected_files: tuple[str, ...] = ()
    expected_output_patterns: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Canonical dictionary representation."""
        return {
            "verifier_type": self.verifier_type,
            "command": list(self.command),
            "expected_exit_code": self.expected_exit_code,
            "expected_files": sorted(self.expected_files),
            "expected_output_patterns": list(self.expected_output_patterns),
        }


@dataclass(frozen=True)
class FrozenStartingState:
    """The immutable starting baseline for paired replay executions."""

    project_id: str
    repository_path: str
    starting_git_sha: str
    root_commit: str
    checkpoint_id: str | None = None
    state_digest: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Canonical dictionary representation."""
        return {
            "project_id": self.project_id,
            "repository_path": self.repository_path,
            "starting_git_sha": self.starting_git_sha,
            "root_commit": self.root_commit,
            "checkpoint_id": self.checkpoint_id,
            "state_digest": self.state_digest,
        }

    @property
    def digest(self) -> str:
        """Deterministic digest of the frozen starting state."""
        return digest_of(self.to_dict())


def _serialize_metric(value: Any) -> Any:
    """Format an optional metric value, rendering None as UNKNOWN."""
    if value is None:
        return UNKNOWN
    return value


@dataclass(frozen=True)
class MeasurementRecord:
    """Comprehensive measurement record capturing multi-dimensional execution metrics.

    All unmeasured/unavailable values are represented as None and serialize to
    'UNKNOWN' rather than zero or fabricated values.
    """

    # SUCCESS / WORKFLOW
    verified_success: bool = False
    failure_classification: FailureClassification | None = None
    attempt_count: int | None = None
    repair_count: int | None = None
    escalation_count: int | None = None
    human_intervention_count: int | None = None
    turns_per_attempt: tuple[int, ...] | None = None
    tool_model_round_trips: int | None = None

    # MODEL / ROUTING
    provider: str | None = None
    model: str | None = None
    backend: str | None = None
    exact_binding: str | None = None
    effort_level: str | None = None
    binding_changes: int | None = None
    provider_model_backend_changes: int | None = None

    # TOKENS / COST (None = UNKNOWN, never fabricated)
    input_tokens: int | None = None
    uncached_input_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    monetary_cost: float | None = None

    # CONTEXT
    stable_context_bytes: int | None = None
    semi_stable_context_bytes: int | None = None
    volatile_context_bytes: int | None = None
    context_pack_digest: str | None = None
    rendered_prefix_digest: str | None = None
    tool_schema_bytes: int | None = None
    tool_result_bytes: int | None = None
    artifact_indirected_bytes: int | None = None

    # TIME
    total_wall_clock_seconds: float | None = None
    deterministic_overhead_seconds: float | None = None
    model_wait_seconds: float | None = None
    tool_execution_seconds: float | None = None
    deterministic_validation_seconds: float | None = None
    review_seconds: float | None = None
    gate_seconds: float | None = None
    human_wait_seconds: float | None = None

    # QUOTA / EXECUTION
    quota_state_evidence: str | None = None
    scarce_pool_consumption: int | None = None
    cold_cache_transition: bool | None = None
    crash_restart_failover_evidence: str | None = None
    #: F-04: Deterministic verifier-toolchain fingerprint (sha256).  Stored as
    #: execution evidence only; never authoritative Project truth.
    verifier_toolchain_fingerprint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize measurement record with explicit UNKNOWN values for missing fields."""
        return {
            "success_workflow": {
                "verified_success": self.verified_success,
                "failure_classification": (
                    self.failure_classification.value if self.failure_classification else None
                ),
                "attempt_count": _serialize_metric(self.attempt_count),
                "repair_count": _serialize_metric(self.repair_count),
                "escalation_count": _serialize_metric(self.escalation_count),
                "human_intervention_count": _serialize_metric(self.human_intervention_count),
                "turns_per_attempt": (
                    list(self.turns_per_attempt)
                    if self.turns_per_attempt is not None
                    else UNKNOWN
                ),
                "tool_model_round_trips": _serialize_metric(self.tool_model_round_trips),
            },
            "model_routing": {
                "provider": _serialize_metric(self.provider),
                "model": _serialize_metric(self.model),
                "backend": _serialize_metric(self.backend),
                "exact_binding": _serialize_metric(self.exact_binding),
                "effort_level": _serialize_metric(self.effort_level),
                "binding_changes": _serialize_metric(self.binding_changes),
                "provider_model_backend_changes": _serialize_metric(
                    self.provider_model_backend_changes
                ),
            },
            "tokens_cost": {
                "input_tokens": _serialize_metric(self.input_tokens),
                "uncached_input_tokens": _serialize_metric(self.uncached_input_tokens),
                "cache_read_tokens": _serialize_metric(self.cache_read_tokens),
                "cache_write_tokens": _serialize_metric(self.cache_write_tokens),
                "output_tokens": _serialize_metric(self.output_tokens),
                "reasoning_tokens": _serialize_metric(self.reasoning_tokens),
                "monetary_cost": _serialize_metric(self.monetary_cost),
            },
            "context": {
                "stable_context_bytes": _serialize_metric(self.stable_context_bytes),
                "semi_stable_context_bytes": _serialize_metric(self.semi_stable_context_bytes),
                "volatile_context_bytes": _serialize_metric(self.volatile_context_bytes),
                "context_pack_digest": _serialize_metric(self.context_pack_digest),
                "rendered_prefix_digest": _serialize_metric(self.rendered_prefix_digest),
                "tool_schema_bytes": _serialize_metric(self.tool_schema_bytes),
                "tool_result_bytes": _serialize_metric(self.tool_result_bytes),
                "artifact_indirected_bytes": _serialize_metric(self.artifact_indirected_bytes),
            },
            "time": {
                "total_wall_clock_seconds": _serialize_metric(self.total_wall_clock_seconds),
                "deterministic_overhead_seconds": _serialize_metric(
                    self.deterministic_overhead_seconds
                ),
                "model_wait_seconds": _serialize_metric(self.model_wait_seconds),
                "tool_execution_seconds": _serialize_metric(self.tool_execution_seconds),
                "deterministic_validation_seconds": _serialize_metric(
                    self.deterministic_validation_seconds
                ),
                "review_seconds": _serialize_metric(self.review_seconds),
                "gate_seconds": _serialize_metric(self.gate_seconds),
                "human_wait_seconds": _serialize_metric(self.human_wait_seconds),
            },
            "quota_execution": {
                "quota_state_evidence": _serialize_metric(self.quota_state_evidence),
                "scarce_pool_consumption": _serialize_metric(self.scarce_pool_consumption),
                "cold_cache_transition": _serialize_metric(self.cold_cache_transition),
                "crash_restart_failover_evidence": _serialize_metric(
                    self.crash_restart_failover_evidence
                ),
                "verifier_toolchain_fingerprint": _serialize_metric(
                    self.verifier_toolchain_fingerprint
                ),
            },
        }

    @property
    def digest(self) -> str:
        """Deterministic digest of this measurement record."""
        return digest_of(self.to_dict())


@dataclass(frozen=True)
class ReplayDefinition:
    """The canonical content identity of a replay experiment definition.

    Does NOT contain execution IDs, process IDs, or timestamps so that
    content identity remains strictly reproducible.
    """

    task_id: str
    project_key: str
    frozen_start_sha: str
    strategy_id: str
    strategy_params: dict[str, Any] = field(default_factory=dict)
    verifier: DeterministicVerifier = field(default_factory=DeterministicVerifier)
    expected_artifacts: tuple[str, ...] = ()
    requires_external_model: bool = False
    #: Deterministic content identity of the corpus ReplayTaskDefinition this
    #: definition instantiates.  Same task_id with changed objective/scope/
    #: verifier/task semantics yields a different digest and must NOT compare
    #: as the same task.
    task_definition_digest: str = ""
    frozen_starting_state: FrozenStartingState | None = None
    checkpoint_id: str | None = None
    state_digest: str | None = None

    def bind_starting_state(self, frozen_starting_state: FrozenStartingState) -> ReplayDefinition:
        """Return a new ReplayDefinition bound to the verified FrozenStartingState."""
        if (
            self.frozen_start_sha
            and self.frozen_start_sha != frozen_starting_state.starting_git_sha
        ):
            raise ReplayStateMismatchError(
                f"Definition frozen_start_sha '{self.frozen_start_sha}' does not match "
                f"frozen starting state SHA '{frozen_starting_state.starting_git_sha}'."
            )
        if self.frozen_starting_state is not None:
            if self.frozen_starting_state.digest != frozen_starting_state.digest:
                raise ReplayStateMismatchError(
                    "Definition is already bound to a different FrozenStartingState."
                )
        return ReplayDefinition(
            task_id=self.task_id,
            project_key=self.project_key,
            frozen_start_sha=frozen_starting_state.starting_git_sha,
            strategy_id=self.strategy_id,
            strategy_params=dict(self.strategy_params),
            verifier=self.verifier,
            expected_artifacts=self.expected_artifacts,
            requires_external_model=self.requires_external_model,
            task_definition_digest=self.task_definition_digest,
            frozen_starting_state=frozen_starting_state,
            checkpoint_id=frozen_starting_state.checkpoint_id,
            state_digest=frozen_starting_state.state_digest,
        )

    @property
    def is_bound(self) -> bool:
        """Return True if this definition is bound to a verified FrozenStartingState."""
        return self.frozen_starting_state is not None

    def content_dict(self) -> dict[str, Any]:
        """Deterministic dictionary representation of content identity."""
        ckpt = self.checkpoint_id
        st_dig = self.state_digest
        f_state_dict: dict[str, Any] | None = None
        if self.frozen_starting_state is not None:
            f_state_dict = self.frozen_starting_state.to_dict()
            if ckpt is None:
                ckpt = self.frozen_starting_state.checkpoint_id
            if st_dig is None:
                st_dig = self.frozen_starting_state.state_digest

        return {
            "task_id": self.task_id,
            "project_key": self.project_key,
            "frozen_start_sha": self.frozen_start_sha,
            "strategy_id": self.strategy_id,
            "strategy_params": self.strategy_params,
            "verifier": self.verifier.to_dict(),
            "expected_artifacts": sorted(self.expected_artifacts),
            "requires_external_model": self.requires_external_model,
            "task_definition_digest": self.task_definition_digest,
            "frozen_starting_state": f_state_dict,
            "checkpoint_id": ckpt,
            "state_digest": st_dig,
        }

    @property
    def definition_digest(self) -> str:
        """Stable sha256 hex digest of the canonical JSON definition."""
        return digest_of(self.content_dict())


@dataclass(frozen=True)
class ReplayExecutionRecord:
    """Durable record of one specific execution of a replay definition."""

    execution_id: str
    task_id: str
    project_key: str
    definition_digest: str
    strategy_id: str
    start_time: str
    end_time: str | None = None
    starting_sha: str = ""
    candidate_sha: str | None = None
    workspace_path: str = ""
    workspace_result_digest: str = ""
    verified_success: bool = False
    failure_classification: FailureClassification | None = None
    measurement: MeasurementRecord | None = None
    frozen_starting_state: FrozenStartingState | None = None
    frozen_state_digest: str = ""
    #: Deterministic content identity of the corpus task this record executes.
    task_definition_digest: str = ""
    effective_definition: ReplayDefinition | None = None
    strategy_evidence: dict[str, Any] | None = None
    verifier_evidence: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Canonical dictionary representation."""
        return {
            "execution_id": self.execution_id,
            "task_id": self.task_id,
            "project_key": self.project_key,
            "definition_digest": self.definition_digest,
            "strategy_id": self.strategy_id,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "starting_sha": self.starting_sha,
            "candidate_sha": self.candidate_sha,
            "workspace_path": self.workspace_path,
            "workspace_result_digest": self.workspace_result_digest,
            "verified_success": self.verified_success,
            "failure_classification": (
                self.failure_classification.value if self.failure_classification else None
            ),
            "measurement": self.measurement.to_dict() if self.measurement else None,
            "frozen_starting_state": (
                self.frozen_starting_state.to_dict() if self.frozen_starting_state else None
            ),
            "frozen_state_digest": self.frozen_state_digest,
            "task_definition_digest": self.task_definition_digest,
            "effective_definition": (
                self.effective_definition.content_dict() if self.effective_definition else None
            ),
            "strategy_evidence": self.strategy_evidence,
            "verifier_evidence": self.verifier_evidence,
        }

    @property
    def digest(self) -> str:
        """Deterministic digest of the execution record."""
        return digest_of(self.to_dict())
