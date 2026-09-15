"""C12-C bounded tool-output representation observations over the canonical replay seam.

C12-C is an EXPERIMENT / OBSERVATION layer.  It is NOT a second execution
engine and NOT a second security or effort authority.  Every piece of real
execution used by C12-C goes through the existing canonical ReplayEngine
execution seam (``ReplayEngine.execute_replay``): DisposableWorkspace
lifecycle, SandboxPolicy construction, sandbox grant/interceptor/trace
setup, ``run_sandboxed_process`` invocation, verifier sequencing, workspace
mutation sentinels, authoritative-repo mutation sentinels, and execution
record construction all remain owned by ReplayEngine.  This module only
post-processes the evidence that seam returns.

Honesty invariants for this campaign:

- No semantic model/Orch executor participates.  Deterministic command /
  process / tool steps executed by the canonical seam are NOT Orch/model
  turns: ``MeasurementRecord.turns_per_attempt`` stays UNKNOWN (never a
  command count) and ``tool_model_round_trips`` stays UNKNOWN.  The real
  turn-budget experiment is therefore recorded NOT_AVAILABLE.
- A baseline verifier PASS is baseline evidence only.  No arm claims
  semantic task success: ``semantic_effect`` stays UNMEASURED unless a real
  semantic executor performs the task objective, and the derived C12-C
  observation arm records ``verified_success=False`` on both the record and
  its measurement.  The underlying canonical execution outcome is preserved
  separately as explicit baseline/process evidence (never as arm success).
  Deterministic process steps are named as process steps in strategy
  evidence; they never populate or alias the canonical turn metric.
- Byte counts are measured over the UTF-8 rendering of the captured
  stdout/stderr TEXT returned by the canonical seam (``ProcessResult``
  exposes strings, not raw subprocess bytes).  Raw-byte subprocess capture
  is never claimed.
- Security identity: the canonical seam does not yet expose a positive,
  non-secret security-policy identity bound to the SandboxPolicy it
  actually used.  C12-C fabricates none and admits none from ordinary
  caller-written strategy evidence.  Security identity is therefore
  UNESTABLISHED for every current C12-C execution record, and direct
  comparability always fails closed on that control.
- EFFORT: the real campaign has no configured ExecutionBinding, so the
  effort arm is NOT_AVAILABLE.  C11-B owns canonical effort resolution;
  C12-C recreates none of it and accepts no substitute resolver.
- No production winner is adopted from any result.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from orchestrator_mvp.replay.comparison import (
    _positive_security_identity,
    _record_definition_inconsistencies,
    assert_comparable,
)
from orchestrator_mvp.replay.contracts import (
    DeterministicVerifier,
    MeasurementRecord,
    ReplayDefinition,
    ReplayError,
    ReplayExecutionRecord,
    ReplaySafetyViolationError,
    digest_of,
)
from orchestrator_mvp.replay.corpus import ReplayTaskDefinition
from orchestrator_mvp.replay.engine import ReplayEngine
from orchestrator_mvp.replay.experiment import (
    SEMANTIC_EFFECT_UNMEASURED,
    _validate_evidence_dir,
    write_campaign_evidence,
)
from orchestrator_mvp.replay.toolchain import fingerprint_or_unavailable

# ---------------------------------------------------------------------------
# Dimension / vocabulary constants
# ---------------------------------------------------------------------------

#: Experimental dimension: tool-output representation.
DIMENSION_TOOL_OUTPUT = "tool_output"
#: Declared experimental dimension whose real campaign arm is NOT_AVAILABLE:
#: no semantic model/Orch turn executor participates in this campaign.
DIMENSION_TURN_BUDGET = "turn_budget"
#: Declared experimental dimension whose real campaign arm is NOT_AVAILABLE:
#: no execution binding / model execution is configured in this campaign.
DIMENSION_EFFORT = "reasoning_effort"
#: Composite-tool pseudo-dimension — status-only, never executed.
DIMENSION_COMPOSITE_TOOL = "composite_tool"

_KNOWN_DIMENSIONS = (
    DIMENSION_TURN_BUDGET,
    DIMENSION_TOOL_OUTPUT,
    DIMENSION_EFFORT,
)

#: Strategy-params key carrying the dimension itself (never normalized away,
#: so cross-dimension comparisons fail closed).
PARAM_DIMENSION = "c12c_dimension"
#: Strategy-params key for the tool-output representation.
PARAM_TOOL_REPRESENTATION = "c12c_tool_representation"
#: Strategy-params key for the held-constant context strategy.
PARAM_CONTEXT_STRATEGY = "c12c_context_strategy"
#: CANONICAL ReplayEngine command-strategy key: the engine executes
#: ``strategy_params["command"]`` through its sandboxed seam.  The declared
#: process step is stored under this key so the canonical seam — and nothing
#: else — owns its execution.
CANONICAL_COMMAND_KEY = "command"

#: Strategy-evidence key under which a positively bound security-policy
#: identity WOULD appear if a future accepted canonical seam supplied one.
#: The current canonical ReplayEngine seam exposes no trustworthy actual
#: policy identity, so C12-C never writes this key, never reads it as
#: positive evidence, and security comparability always fails closed.
#: Caller-written values under this key (including hand-written digests)
#: are ignored: equality of unproven digests is never equivalence.
SECURITY_IDENTITY_EVIDENCE_KEY = "c12c_security_identity"

#: Tool-output representations.
TOOL_REPRESENTATION_RAW = "raw"
TOOL_REPRESENTATION_ARTIFACT_INDIRECTED = "artifact_indirected"

#: Context strategy held constant: the frozen source is presented as-is
#: (C12-B owns context-strategy arms).
CONTEXT_STRATEGY_BASELINE = "baseline"

#: Bounded excerpt length for artifact-indirected tool output (bytes).
TOOL_EXCERPT_BYTES = 256

#: Composite-tool arm id: see :func:`composite_tool_arm_status`.
COMPOSITE_TOOL_ARM_ID = "composite_tool"

_ARTIFACT_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_ARTIFACT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*\.bin$")


# ---------------------------------------------------------------------------
# Experiment definition identity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolOutputExperimentDefinition:
    """Deterministic identity of one C12-C observation arm definition.

    Contains NO execution IDs, timestamps, or random values: the digest is
    purely a function of the declared experiment content.  Changing the
    experimental parameter (tool representation) changes the digest; every
    non-experimental control is part of the digest as well.
    """

    task_id: str
    project_key: str
    task_definition_digest: str
    frozen_start_sha: str
    dimension: str
    arm_id: str
    #: One deterministic process/tool step command, executed by the
    #: canonical ReplayEngine command-strategy seam.  This is a PROCESS
    #: STEP, never an Orch/model turn.
    process_step: tuple[str, ...] = ()
    #: Tool-output representation under test.
    tool_representation: str = TOOL_REPRESENTATION_RAW
    #: Held-constant context strategy (C12-B baseline; not varied here).
    context_strategy: str = CONTEXT_STRATEGY_BASELINE
    verifier: DeterministicVerifier = field(default_factory=DeterministicVerifier)

    def __post_init__(self) -> None:
        if self.dimension not in _KNOWN_DIMENSIONS:
            raise ReplayError(f"Unknown C12-C experiment dimension: {self.dimension!r}")
        if self.tool_representation not in (
            TOOL_REPRESENTATION_RAW,
            TOOL_REPRESENTATION_ARTIFACT_INDIRECTED,
        ):
            raise ReplayError(f"Unknown tool representation: {self.tool_representation!r}")
        if not self.process_step:
            raise ReplayError("A C12-C executed observation arm requires one process step")

    def strategy_params(self) -> dict[str, Any]:
        """Canonical strategy params embedded into the ReplayDefinition.

        The declared process step is stored under the canonical
        ``command`` key so the ReplayEngine command-strategy seam — and
        nothing else — owns its execution.  These keys are part of the
        ReplayDefinition digest, so changing the experimental
        representation changes the experiment identity deterministically.
        No security descriptor is embedded: C12-C does not fabricate
        security identity the canonical seam does not provide.
        """
        params: dict[str, Any] = {
            PARAM_DIMENSION: self.dimension,
            CANONICAL_COMMAND_KEY: list(self.process_step),
            PARAM_TOOL_REPRESENTATION: self.tool_representation,
            PARAM_CONTEXT_STRATEGY: self.context_strategy,
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
            "process_step": list(self.process_step),
            "tool_representation": self.tool_representation,
            "context_strategy": self.context_strategy,
            "verifier": self.verifier.to_dict(),
        }

    @property
    def digest(self) -> str:
        """Deterministic digest of this experiment definition (no runtime IDs)."""
        return digest_of(self.to_dict())


# ---------------------------------------------------------------------------
# Tool-output artifact indirection (observation-only post-processing)
# ---------------------------------------------------------------------------


def _confined_artifact_path(artifacts_dir: Path, name: str) -> Path:
    """Resolve an artifact name, refusing anything outside ``artifacts_dir``."""
    if not _ARTIFACT_NAME_RE.fullmatch(name):
        raise ReplaySafetyViolationError(
            f"Artifact name may not traverse or escape the evidence directory: {name!r}"
        )
    base = artifacts_dir.resolve()
    path = (base / name).resolve()
    if path.parent != base:
        raise ReplaySafetyViolationError(
            f"Refusing artifact path outside the evidence directory: {name!r}"
        )
    return path


def indirect_tool_result(
    content: bytes,
    artifacts_dir: Path,
    label: str,
) -> dict[str, Any]:
    """Content-address a tool result, returning bounded presentation evidence.

    Observation-only: the content is the captured tool output already
    returned by the canonical execution seam.  The full exact bytes are
    stored once, OUTSIDE Project authority, under a digest-derived,
    deterministic file name; the digest binds the exact bytes measured.
    The returned record carries an explicit ``byte_count``, a bounded
    excerpt, and an explicit ``truncated`` flag; it never masquerades as
    the full content.  Deterministic retrieval of the exact referenced
    evidence remains possible through the recorded artifact file and byte
    range, and retrieval re-verifies the digest.
    """
    if not _ARTIFACT_LABEL_RE.fullmatch(label):
        raise ReplaySafetyViolationError(f"Artifact label must be a plain safe token: {label!r}")
    digest = hashlib.sha256(content).hexdigest()
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    artifact_name = f"{digest}_{label}.bin"
    artifact_path = _confined_artifact_path(artifacts_dir, artifact_name)
    artifact_path.write_bytes(content)
    if artifact_path.read_bytes() != content:
        raise ReplaySafetyViolationError(
            f"Artifact indirection digest binding failed for {artifact_name}"
        )
    excerpt_raw = content[:TOOL_EXCERPT_BYTES]
    # The excerpt is a best-effort UTF-8 rendering; ``excerpt_bytes`` is the
    # authoritative bounded byte count of the excerpt window.
    excerpt_text = excerpt_raw.decode("utf-8", errors="ignore")
    return {
        "digest": digest,
        "byte_count": len(content),
        "excerpt": excerpt_text,
        "excerpt_bytes": len(excerpt_raw),
        "truncated": len(content) > TOOL_EXCERPT_BYTES,
        "retrieval": {
            "artifact_file": artifact_name,
            "byte_range": [0, len(content)],
        },
        "note": (
            "Content-addressed artifact; digest binds exact measured bytes; "
            "truncated=true means content continues beyond the excerpt."
        ),
    }


def retrieve_indirected_result(
    record: dict[str, Any],
    artifacts_dir: Path,
) -> bytes:
    """Deterministically retrieve exact indirected content by its digest."""
    name = record["retrieval"]["artifact_file"]
    data: bytes = _confined_artifact_path(artifacts_dir, name).read_bytes()
    if hashlib.sha256(data).hexdigest() != record["digest"]:
        raise ReplaySafetyViolationError(f"Artifact content does not match recorded digest: {name}")
    return data


# ---------------------------------------------------------------------------
# Composite-tool arm status
# ---------------------------------------------------------------------------


def composite_tool_arm_status() -> tuple[bool, str]:
    """Report composite-tool arm availability honestly.

    The existing C08/C11-C tooling primitives authorize ONE logical tool per
    frozen contract (single invocation, single side-effect class).  A bounded
    composite operation (search+context, patch+verify, tests+failure
    extraction) expressed as ONE composite invocation would require a new
    load-bearing composite-contract subsystem with its own authorization and
    trace composition.  C12-C does NOT expand scope to build one: the
    composite arm is recorded NOT_AVAILABLE.
    """
    return (
        False,
        (
            "Existing C08/C11-C primitives authorize single logical tools per "
            "frozen contract; a bounded composite invocation would require a "
            "new load-bearing composite-contract subsystem, which C12-C does "
            "not build. Atomic sequences remain available and comparable."
        ),
    )


# ---------------------------------------------------------------------------
# Tool-output representation observation over the canonical execution seam
# ---------------------------------------------------------------------------


def execute_tool_output_observation(
    engine: ReplayEngine,
    task_def: ReplayTaskDefinition,
    frozen_state: Any,
    experiment: ToolOutputExperimentDefinition,
    *,
    artifacts_dir: Path | None = None,
    evidence_dir: Path | None = None,
) -> ReplayExecutionRecord:
    """Execute ONE tool-output observation arm through the canonical seam.

    ALL real execution happens inside ``ReplayEngine.execute_replay``:
    verified Project identity and frozen state, DisposableWorkspace
    lifecycle, SandboxPolicy construction, sandbox grant/interceptor/trace
    setup, sandboxed process invocation, deterministic verifier, workspace
    mutation sentinels, and authoritative-repo non-mutation invariants.
    This function only POST-PROCESSES the returned evidence into a
    tool-output representation observation.
    """
    if experiment.dimension != DIMENSION_TOOL_OUTPUT:
        raise ReplayError(
            f"Dimension {experiment.dimension!r} is not an executed "
            "observation dimension; its campaign arm is NOT_AVAILABLE"
        )
    # Safety first: refuse evidence destinations inside the authoritative
    # repository or transient replay worktrees BEFORE anything else.
    if artifacts_dir is not None:
        _validate_evidence_dir(artifacts_dir, engine.repo_path)
    if evidence_dir is not None:
        _validate_evidence_dir(evidence_dir, engine.repo_path)
    if (
        experiment.tool_representation == TOOL_REPRESENTATION_ARTIFACT_INDIRECTED
        and artifacts_dir is None
    ):
        raise ReplayError(
            "Artifact-indirected arms require an artifacts_dir outside the authoritative repository"
        )

    definition = ReplayDefinition(
        task_id=task_def.task_id,
        project_key=task_def.project_key,
        frozen_start_sha=frozen_state.starting_git_sha,
        strategy_id=experiment.arm_id,
        strategy_params=experiment.strategy_params(),
        verifier=task_def.verifier,
        expected_artifacts=task_def.expected_artifact_shape,
        requires_external_model=task_def.requires_external_model,
        task_definition_digest=task_def.content_digest,
    )
    # Canonical execution: everything below this call is owned by the engine.
    engine_record = engine.execute_replay(
        definition,
        starting_state=frozen_state,
        evidence_dir=evidence_dir,
    )
    return observe_tool_representation(engine_record, experiment, artifacts_dir=artifacts_dir)


def observe_tool_representation(
    engine_record: ReplayExecutionRecord,
    experiment: ToolOutputExperimentDefinition,
    *,
    artifacts_dir: Path | None = None,
) -> ReplayExecutionRecord:
    """Post-process one canonical record into representation evidence.

    Never executes anything.  Byte counts are measured over the UTF-8
    rendering of the captured stdout/stderr TEXT the canonical seam placed
    in strategy evidence; the raw arm's record contains that captured
    content, so a raw "presented" quantity is backed by evidence actually
    contained in the record.  The canonical turn metric is never populated:
    a deterministic process step is not an Orch/model turn.

    Evidence-integrity derivation: no semantic model/Orch executor performed
    the corpus task objective, so the derived C12-C observation arm records
    ``verified_success=False`` on both the record and its measurement even
    when the underlying canonical execution succeeded.  The underlying
    canonical outcome is preserved separately under
    ``c12c_observation.baseline_execution`` and never masquerades as arm
    semantic success.  ReplayEngine semantics are untouched: this is
    observation-layer derivation only.
    """
    engine_evidence = dict(engine_record.strategy_evidence or {})
    stdout_bytes = str(engine_evidence.get("stdout", "")).encode("utf-8")
    stderr_bytes = str(engine_evidence.get("stderr", "")).encode("utf-8")
    rendered_bytes = len(stdout_bytes) + len(stderr_bytes)
    artifact_indirected_bytes = 0

    measurement = engine_record.measurement
    if measurement is not None and measurement.turns_per_attempt is not None:
        raise ReplayError(
            "C12-C must not populate the canonical turn metric from "
            "deterministic process steps; turns_per_attempt stays UNKNOWN"
        )

    # Preserve the underlying canonical execution outcome as separate,
    # explicit baseline/process evidence (never as arm semantic success).
    engine_verified_success = bool(engine_record.verified_success)
    verifier_passed = (
        engine_record.verifier_evidence.get("passed") if engine_record.verifier_evidence else None
    )
    process_step_passed = engine_evidence.get("passed")

    observation: dict[str, Any] = {
        "record_kind": "c12c_tool_output_observation",
        "semantic_effect": SEMANTIC_EFFECT_UNMEASURED,
        "semantic_effect_basis": (
            "No semantic model/Orch executor performed the task objective; "
            "the baseline verifier PASS in this record is baseline evidence "
            "only and never attributes task success to this arm."
        ),
        "experiment_definition_digest": experiment.digest,
        "dimension": experiment.dimension,
        "tool_representation": experiment.tool_representation,
        "process_step": list(experiment.process_step),
        "process_step_note": (
            "Deterministic process/tool step executed by the canonical "
            "ReplayEngine command-strategy seam.  A process step is NOT an "
            "Orch/model turn; turns_per_attempt stays UNKNOWN."
        ),
        "byte_counting_basis": (
            "UTF-8 rendering bytes of the captured stdout/stderr TEXT "
            "returned by the canonical seam; NOT a claim of original "
            "subprocess byte-for-byte capture."
        ),
        "rendered_representation_bytes": rendered_bytes,
        "baseline_verification": {
            "verifier_passed": verifier_passed,
            "note": (
                "Baseline verifier PASS is baseline evidence only; it does "
                "not make this arm's semantic effect MEASURED."
            ),
        },
        "baseline_execution": {
            "process_step_passed": process_step_passed,
            "verifier_passed": verifier_passed,
            "engine_verified_success": engine_verified_success,
            "note": (
                "Underlying canonical ReplayEngine execution outcome, "
                "preserved separately.  It is baseline/process evidence "
                "only and never the semantic success of this C12-C "
                "observation arm (arm verified_success stays False)."
            ),
        },
        "arm_verified_success": False,
        "arm_verified_success_basis": (
            "No semantic executor performed the corpus task objective, so "
            "the derived C12-C observation arm is verified_success=False "
            "even when the underlying canonical execution succeeded."
        ),
    }

    if experiment.tool_representation == TOOL_REPRESENTATION_RAW:
        observation["raw_representation"] = {
            "stdout_utf8_bytes": len(stdout_bytes),
            "stderr_utf8_bytes": len(stderr_bytes),
            "content_contained_in_record": True,
            "content_basis": (
                "The canonical strategy evidence of this record carries the "
                "full captured stdout/stderr text these byte counts measure."
            ),
        }
    else:
        if artifacts_dir is None:
            raise ReplayError("Artifact-indirected representation requires artifacts_dir")
        artifacts: dict[str, Any] = {}
        for stream_name, raw in (("stdout", stdout_bytes), ("stderr", stderr_bytes)):
            if raw:
                indirect = indirect_tool_result(raw, artifacts_dir, f"step_{stream_name}")
                artifacts[f"{stream_name}_artifact"] = indirect
                artifact_indirected_bytes += indirect["byte_count"]
            else:
                artifacts[f"{stream_name}_artifact"] = None
        observation["artifact_indirected_representation"] = artifacts

    derived_measurement: MeasurementRecord | None
    if measurement is not None:
        derived_measurement = replace(
            measurement,
            verified_success=False,
            tool_result_bytes=rendered_bytes,
            artifact_indirected_bytes=artifact_indirected_bytes,
        )
    else:
        derived_measurement = MeasurementRecord(
            verified_success=False,
            tool_result_bytes=rendered_bytes,
            artifact_indirected_bytes=artifact_indirected_bytes,
        )

    return replace(
        engine_record,
        verified_success=False,
        measurement=derived_measurement,
        strategy_evidence={**engine_evidence, "c12c_observation": observation},
    )


# ---------------------------------------------------------------------------
# Dimension-aware comparability (C12-B layer NOT weakened)
# ---------------------------------------------------------------------------


def _dimension_param_key(dimension: str) -> str:
    """Return the strategy-params key normalized away for ``dimension``.

    Only executed dimensions have an experimental variable to normalize.
    Turn/effort arms are NOT_AVAILABLE in this campaign and produce no
    records, so they have nothing to normalize.
    """
    if dimension == DIMENSION_TOOL_OUTPUT:
        return PARAM_TOOL_REPRESENTATION
    raise ReplayError(
        f"Dimension {dimension!r} has no executed experimental variable in "
        "this C12-C campaign (its arm is NOT_AVAILABLE); it cannot normalize "
        "comparisons"
    )


def assert_efficiency_comparable(
    record_a: ReplayExecutionRecord,
    record_b: ReplayExecutionRecord,
    *,
    dimension: str,
) -> Any:
    """Compare two C12-C records varying ONLY the declared dimension.

    The accepted C12-B fail-closed comparability layer is applied UNCHANGED
    to dimension-normalized records.  Before normalization, records whose
    record-level identity contradicts their embedded effective_definition
    are rejected outright.  Normalization removes ONLY the declared
    dimension's own strategy-params key; the dimension marker itself is
    retained so cross-dimension comparisons fail closed.  Security
    comparability requires POSITIVE policy identity bound to the actual
    canonical execution contract on BOTH records; the current canonical
    seam exposes no such identity and caller-written digests are never
    admitted, so every current C12-C executed-arm comparison retains the
    explicit fail-closed security reason (NOT_DIRECTLY_COMPARABLE).
    """
    if dimension not in _KNOWN_DIMENSIONS:
        raise ReplayError(f"Unknown C12-C experiment dimension: {dimension!r}")
    key = _dimension_param_key(dimension)

    for label, rec in (("record_a", record_a), ("record_b", record_b)):
        issues = _record_definition_inconsistencies(rec)
        if issues:
            raise ReplayError(f"{label} is internally inconsistent: " + "; ".join(issues))

    identity_a = _positive_security_identity(record_a)
    identity_b = _positive_security_identity(record_b)
    security_reasons: list[str] = []
    if identity_a is None or identity_b is None:
        missing = [
            label
            for label, identity in (
                ("record_a", identity_a),
                ("record_b", identity_b),
            )
            if identity is None
        ]
        security_reasons.append(
            "security identity not positively established on "
            f"{', '.join(missing)}: the canonical execution seam exposes no "
            "policy identity bound to the SandboxPolicy it used, so this "
            "control fails closed (equality of absences is not equivalence)"
        )
    elif identity_a != identity_b:
        security_reasons.append(f"security identity mismatch: {identity_a!r} vs {identity_b!r}")

    def _normalize(rec: ReplayExecutionRecord) -> ReplayExecutionRecord:
        eff = rec.effective_definition
        if eff is None:
            return rec
        params = {k: v for k, v in eff.strategy_params.items() if k != key}
        norm_def = replace(eff, strategy_params=params)
        return replace(rec, effective_definition=norm_def)

    base = assert_comparable(
        _normalize(record_a),
        _normalize(record_b),
        allow_different_arm=True,
    )
    reasons = [*base.reasons, *security_reasons]
    from orchestrator_mvp.replay.contracts import (
        ComparabilityVerdict,
        ComparisonResult,
    )

    verdict = (
        ComparabilityVerdict.COMPARABLE
        if not reasons
        else ComparabilityVerdict.NOT_DIRECTLY_COMPARABLE
    )
    return ComparisonResult(verdict=verdict, reasons=tuple(reasons))


# ---------------------------------------------------------------------------
# Bounded campaign
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EfficiencyArmResult:
    """One arm's result inside the bounded campaign."""

    dimension: str
    arm_id: str
    available: bool
    not_available_reason: str | None
    experiment_digest: str | None
    record: ReplayExecutionRecord | None

    def to_dict(self) -> dict[str, Any]:
        """Canonical dictionary representation."""
        observation: dict[str, Any] = {}
        if self.record is not None and self.record.strategy_evidence:
            observation = self.record.strategy_evidence.get("c12c_observation", {})
        return {
            "dimension": self.dimension,
            "arm_id": self.arm_id,
            "available": self.available,
            "not_available_reason": self.not_available_reason,
            "experiment_definition_digest": self.experiment_digest,
            "semantic_effect": observation.get("semantic_effect", SEMANTIC_EFFECT_UNMEASURED),
            "record": self.record.to_dict() if self.record else None,
        }


@dataclass(frozen=True)
class TaskEfficiencyEvidence:
    """All C12-C arm evidence for one corpus task."""

    task_id: str
    task_definition_digest: str
    frozen_controls: dict[str, Any]
    toolchain_fingerprint: str
    arm_results: tuple[EfficiencyArmResult, ...]
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
class EfficiencyExperimentResult:
    """Aggregate result of the bounded C12-C campaign."""

    task_evidence: tuple[TaskEfficiencyEvidence, ...]
    toolchain_fingerprint: str
    summary: str
    evidence_manifest: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Canonical dictionary representation."""
        return {
            "c12c_experiment_result": True,
            "toolchain_fingerprint": self.toolchain_fingerprint,
            "tasks": [t.to_dict() for t in self.task_evidence],
            "summary": self.summary,
            "evidence_manifest": self.evidence_manifest,
        }


#: Diagnostic PROCESS STEPS per corpus task.  These are deterministic
#: process/tool invocations (named as such in evidence), executed by the
#: canonical ReplayEngine command-strategy seam with its own hermetic
#: environment.  They are NOT Orch/model turns and never populate the
#: canonical turn metric.
_DIAGNOSTIC_PROCESS_STEPS: dict[str, tuple[str, ...]] = {
    "orch-v2-task-01-bounded-change": (
        "/usr/bin/python3",
        "-c",
        (
            "import sys; sys.path.insert(0, 'src'); "
            "import pytest; "
            "raise SystemExit(pytest.main(['-q', "
            "'-o', 'cache_dir=/tmp/.pytest_cache', "
            "'tests/test_projects.py::"
            "test_valid_git_path_accepted_and_remembered']))"
        ),
    ),
    "orch-v2-task-05-scope-affected": (
        "/usr/bin/python3",
        "-c",
        (
            "import sys; sys.path.insert(0, 'src'); "
            "from orchestrator_mvp.devscope.cli import main; "
            "raise SystemExit(main(['scope', '--json']))"
        ),
    ),
}


def _diagnostic_process_step(task_id: str) -> tuple[str, ...]:
    """Return the declared diagnostic process step for one corpus task."""
    step = _DIAGNOSTIC_PROCESS_STEPS.get(task_id)
    if step is None:
        raise ReplayError(f"No diagnostic process step defined for task {task_id!r}")
    return step


_TURN_ARM_NOT_AVAILABLE_REASON = (
    "No semantic model/Orch turn executor participated in this campaign. "
    "Deterministic process/tool steps are not Orch/model turns, so no honest "
    "turn-budget measurement exists; the canonical turn metric stays UNKNOWN."
)

_EFFORT_ARM_NOT_AVAILABLE_REASON = (
    "No execution binding is configured in this campaign environment. "
    "Canonical C11-B effort resolution owns that authority; C12-C adds no "
    "substitute resolver and records no speculative effort evidence."
)


def _tool_observation_arms() -> tuple[tuple[str, str], ...]:
    """Raw + artifact-indirected tool-output representation arm specs."""
    return (
        ("c12c_tool_raw", TOOL_REPRESENTATION_RAW),
        ("c12c_tool_indirect", TOOL_REPRESENTATION_ARTIFACT_INDIRECTED),
    )


def _not_available_arm(
    dimension: str,
    arm_id: str,
    reason: str,
) -> EfficiencyArmResult:
    return EfficiencyArmResult(
        dimension=dimension,
        arm_id=arm_id,
        available=False,
        not_available_reason=reason,
        experiment_digest=None,
        record=None,
    )


def _run_observation_arm(
    engine: ReplayEngine,
    task_def: ReplayTaskDefinition,
    frozen_state: Any,
    arm_id: str,
    representation: str,
    artifacts_dir: Path | None,
) -> EfficiencyArmResult:
    process_step = _diagnostic_process_step(task_def.task_id)
    experiment = ToolOutputExperimentDefinition(
        task_id=task_def.task_id,
        project_key=task_def.project_key,
        task_definition_digest=task_def.content_digest,
        frozen_start_sha=frozen_state.starting_git_sha,
        dimension=DIMENSION_TOOL_OUTPUT,
        arm_id=arm_id,
        process_step=process_step,
        tool_representation=representation,
        verifier=task_def.verifier,
    )
    record = execute_tool_output_observation(
        engine,
        task_def,
        frozen_state,
        experiment,
        artifacts_dir=artifacts_dir,
    )
    return EfficiencyArmResult(
        dimension=DIMENSION_TOOL_OUTPUT,
        arm_id=arm_id,
        available=True,
        not_available_reason=None,
        experiment_digest=experiment.digest,
        record=record,
    )


def _comparisons_for_task(
    records_by_arm: dict[str, ReplayExecutionRecord],
    dimensions_by_arm: dict[str, str],
) -> list[dict[str, Any]]:
    """One-variable-at-a-time comparisons, recorded honestly per dimension."""
    results: list[dict[str, Any]] = []
    arm_ids = list(records_by_arm)
    for i, arm_a in enumerate(arm_ids):
        for arm_b in arm_ids[i + 1 :]:
            dim_a = dimensions_by_arm[arm_a]
            dim_b = dimensions_by_arm[arm_b]
            comparison = assert_efficiency_comparable(
                records_by_arm[arm_a],
                records_by_arm[arm_b],
                dimension=dim_a,
            )
            results.append(
                {
                    "arm_a": arm_a,
                    "arm_b": arm_b,
                    "dimension": dim_a,
                    "cross_dimension": dim_a != dim_b,
                    "verdict": comparison.verdict.value,
                    "comparable": comparison.comparable,
                    "reasons": list(comparison.reasons),
                }
            )
    return results


def run_c12c_experiments(
    repo_path: Path | str,
    *,
    evidence_dir: Path | str | None = None,
    task_ids: tuple[str, ...] | None = None,
) -> EfficiencyExperimentResult:
    """Run the bounded C12-C observation campaign on real frozen tasks.

    Campaign shape (bounded; observation only):
    - Tool-output representation: raw vs artifact-indirected observation of
      ONE deterministic diagnostic process step per task, both arms executed
      by the canonical ReplayEngine seam; semantic effect UNMEASURED.
    - Turn-budget arm: NOT_AVAILABLE — no semantic model/Orch turn executor
      participates; process steps are not turns.
    - Effort arm: NOT_AVAILABLE — no execution binding is configured;
      C11-B owns canonical effort resolution and C12-C adds no substitute.
    - Composite arm: NOT_AVAILABLE.

    No external model is invoked.  No production winner is adopted.
    """
    repo = Path(repo_path).resolve()
    if evidence_dir is not None:
        _validate_evidence_dir(Path(evidence_dir), repo)

    engine = ReplayEngine(repo)
    toolchain_fp = fingerprint_or_unavailable()

    from orchestrator_mvp.replay.corpus import get_corpus_task

    selected_ids = task_ids or (
        "orch-v2-task-01-bounded-change",
        "orch-v2-task-05-scope-affected",
    )
    tasks: list[ReplayTaskDefinition] = []
    for task_id in selected_ids:
        task = get_corpus_task(task_id)
        if task is None:
            raise ReplayError(f"Unknown corpus task ID: {task_id}")
        tasks.append(task)

    artifacts_root = Path(evidence_dir) / "artifacts" if evidence_dir is not None else None

    task_evidence: list[TaskEfficiencyEvidence] = []
    for task_def in tasks:
        frozen_state = engine.freeze_starting_state(
            task_def.project_key,
            starting_sha=task_def.frozen_start_ref,
        )
        frozen_controls = {
            **frozen_state.to_dict(),
            "frozen_state_digest": frozen_state.digest,
        }

        arm_results: list[EfficiencyArmResult] = []
        for arm_id, representation in _tool_observation_arms():
            arm_results.append(
                _run_observation_arm(
                    engine,
                    task_def,
                    frozen_state,
                    arm_id,
                    representation,
                    artifacts_dir=artifacts_root,
                )
            )

        # Turn-budget arm: honestly NOT_AVAILABLE (see module docstring).
        arm_results.append(
            _not_available_arm(
                DIMENSION_TURN_BUDGET,
                "c12c_turn_budget",
                _TURN_ARM_NOT_AVAILABLE_REASON,
            )
        )
        # Effort arm: honestly NOT_AVAILABLE (no configured binding).
        arm_results.append(
            _not_available_arm(
                DIMENSION_EFFORT,
                "c12c_effort_ladder",
                _EFFORT_ARM_NOT_AVAILABLE_REASON,
            )
        )
        # Composite arm: NOT_AVAILABLE (composite_tool_arm_status()).
        composite_available, composite_reason = composite_tool_arm_status()
        arm_results.append(
            EfficiencyArmResult(
                dimension=DIMENSION_COMPOSITE_TOOL,
                arm_id=COMPOSITE_TOOL_ARM_ID,
                available=composite_available,
                not_available_reason=composite_reason,
                experiment_digest=None,
                record=None,
            )
        )

        records_by_arm = {a.arm_id: a.record for a in arm_results if a.record is not None}
        dimensions_by_arm = {a.arm_id: a.dimension for a in arm_results if a.record is not None}
        comparison_results = _comparisons_for_task(records_by_arm, dimensions_by_arm)

        task_evidence.append(
            TaskEfficiencyEvidence(
                task_id=task_def.task_id,
                task_definition_digest=task_def.content_digest,
                frozen_controls=frozen_controls,
                toolchain_fingerprint=fingerprint_or_unavailable(),
                arm_results=tuple(arm_results),
                comparison_results=tuple(comparison_results),
            )
        )

    summary = _build_summary(task_evidence, toolchain_fp)
    result = EfficiencyExperimentResult(
        task_evidence=tuple(task_evidence),
        toolchain_fingerprint=toolchain_fp,
        summary=summary,
    )

    if evidence_dir is not None:
        manifest = write_experiment_evidence(result, Path(evidence_dir))
        result = EfficiencyExperimentResult(
            task_evidence=result.task_evidence,
            toolchain_fingerprint=result.toolchain_fingerprint,
            summary=result.summary,
            evidence_manifest=manifest,
        )
    return result


def _build_summary(
    task_evidence: list[TaskEfficiencyEvidence],
    toolchain_fp: str,
) -> str:
    """Human-readable observation-only campaign summary."""
    lines: list[str] = [
        "C12-C BOUNDED TOOL-OUTPUT OBSERVATION CAMPAIGN — OBSERVATIONS ONLY",
        "=" * 64,
        "",
        f"Toolchain fingerprint: {toolchain_fp}",
        f"Tasks evaluated: {len(task_evidence)}",
        "No external model was invoked: model/token/reasoning metrics are UNKNOWN.",
        "",
    ]
    tool_observations: list[str] = []

    for task_ev in task_evidence:
        lines.append(f"Task: {task_ev.task_id}")
        fc = task_ev.frozen_controls
        lines.append(f"  project_id (canonical): {fc.get('project_id', '')}")
        lines.append(f"  frozen start: {fc.get('starting_git_sha', '')[:12]}")
        lines.append(f"  frozen_state_digest: {fc.get('frozen_state_digest', '')[:16]}...")
        for arm in task_ev.arm_results:
            if not arm.available:
                lines.append(
                    f"  ARM {arm.arm_id} ({arm.dimension}): NOT_AVAILABLE — "
                    f"{arm.not_available_reason or ''}"
                )
                continue
            rec = arm.record
            assert rec is not None and rec.strategy_evidence is not None
            m = rec.measurement
            assert m is not None
            observation = rec.strategy_evidence.get("c12c_observation", {})
            lines.append(
                f"  ARM {arm.arm_id} ({arm.dimension}): "
                f"representation={observation.get('tool_representation')} "
                f"rendered_bytes={observation.get('rendered_representation_bytes')} "
                f"tool_result_bytes={m.tool_result_bytes} "
                f"indirected_bytes={m.artifact_indirected_bytes} "
                f"semantic_effect={observation.get('semantic_effect')} "
                f"turns=UNKNOWN (no semantic executor)"
            )
            if arm.dimension == DIMENSION_TOOL_OUTPUT:
                tool_observations.append(
                    f"{task_ev.task_id}/{arm.arm_id}: "
                    f"rendered_bytes={observation.get('rendered_representation_bytes')} "
                    f"indirected_bytes={m.artifact_indirected_bytes} "
                    f"semantic_effect=UNMEASURED"
                )
        for cmp_result in task_ev.comparison_results:
            lines.append(
                f"  COMPARE {cmp_result['arm_a']} vs {cmp_result['arm_b']} "
                f"[{cmp_result['dimension']}"
                f"{', cross-dimension' if cmp_result['cross_dimension'] else ''}]: "
                f"{cmp_result['verdict']}"
            )
            if not cmp_result["comparable"] and cmp_result["reasons"]:
                lines.append(f"    reasons: {'; '.join(cmp_result['reasons'])}")
        lines.append("")

    lines.append("OBSERVATIONS (no production winner installed, none recommended):")
    lines.append(
        "- TURN: NOT_AVAILABLE / UNMEASURED — no semantic model/Orch turn "
        "executor participated in this campaign; deterministic process steps "
        "are not Orch/model turns, so no turn-ceiling result exists and none "
        "is claimed."
    )
    for obs in tool_observations:
        lines.append(f"- TOOL: {obs}")
    lines.append(
        "- TOOL: semantic effect UNMEASURED — no model consumed any "
        "representation in this campaign."
    )
    lines.extend(
        [
            "",
            "EFFORT: NOT_AVAILABLE — no execution binding is configured in this "
            "campaign environment; canonical C11-B effort resolution owns that "
            "authority and C12-C adds no substitute resolver.",
            "",
            "COMPOSITE: NOT_AVAILABLE — existing C08/C11-C primitives authorize "
            "single logical tools per frozen contract; a bounded composite arm "
            "would require a new load-bearing composite-contract subsystem, which "
            "C12-C does not build.",
            "",
            "Direct comparability of executed arms fails closed: the canonical "
            "execution seam does not expose positive security-policy identity, "
            "and equality of absences is not equivalence.",
            "",
            "NO TURN / TOOL / EFFORT STRATEGY PROVEN SUPERIOR YET.",
            "NO PRODUCTION WINNER ADOPTED.",
        ]
    )
    return "\n".join(lines)


def write_experiment_evidence(
    result: EfficiencyExperimentResult, evidence_dir: Path
) -> dict[str, Any]:
    """Write JSON evidence, summary, artifact digests, and SHA-256 manifest."""
    extra_files: dict[str, str] = {}
    artifacts_dir = evidence_dir / "artifacts"
    if artifacts_dir.is_dir():
        for artifact in sorted(artifacts_dir.iterdir()):
            if artifact.is_file():
                extra_files[f"artifacts/{artifact.name}"] = hashlib.sha256(
                    artifact.read_bytes()
                ).hexdigest()
    return write_campaign_evidence(
        result.to_dict(),
        result.summary,
        evidence_dir,
        json_name="c12c_experiment_evidence.json",
        summary_name="c12c_experiment_summary.txt",
        manifest_name="c12c_manifest.json",
        manifest_key="c12c_campaign_manifest",
        extra_files=extra_files,
    )
