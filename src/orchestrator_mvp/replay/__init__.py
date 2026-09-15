"""C12-A / C12-B Real-project replay, measurement, and context-experiment package."""

from __future__ import annotations

from orchestrator_mvp.replay.arms import (
    ARM_A_ID,
    ARM_B_ID,
    ARM_C_ID,
    ARM_D_ID,
    ARM_D_NOT_AVAILABLE,
    ArmResult,
    build_all_arms,
    build_arm_a,
    build_arm_b,
    build_arm_c,
    build_arm_d,
)
from orchestrator_mvp.replay.comparison import assert_comparable
from orchestrator_mvp.replay.context_pack import (
    ContextClass,
    ContextPack,
    ContextSection,
    build_context_pack,
    render_context,
)
from orchestrator_mvp.replay.contracts import (
    UNKNOWN,
    ComparabilityVerdict,
    ComparisonResult,
    DeterministicVerifier,
    FailureClassification,
    FrozenStartingState,
    MeasurementRecord,
    ReplayDefinition,
    ReplayError,
    ReplayExecutionRecord,
    ReplayProjectMismatchError,
    ReplaySafetyViolationError,
    ReplayStateMismatchError,
    ReplayVerificationError,
    ToolchainComparabilityError,
    is_unknown,
)
from orchestrator_mvp.replay.corpus import (
    ReplayTaskDefinition,
    get_initial_corpus,
)
from orchestrator_mvp.replay.engine import ReplayEngine
from orchestrator_mvp.replay.experiment import (
    SEMANTIC_EFFECT_MEASURED,
    SEMANTIC_EFFECT_UNMEASURED,
    ExperimentResult,
    TaskExperimentEvidence,
    run_c12b_experiments,
    write_experiment_evidence,
)
from orchestrator_mvp.replay.registry import (
    RealProjectRegistry,
    RegisteredProject,
    verify_project_repository,
)
from orchestrator_mvp.replay.toolchain import (
    TOOLCHAIN_UNAVAILABLE,
    VERIFIER_TOOLCHAIN_PATH,
    PackageInfo,
    ToolchainFingerprint,
    ToolchainFingerprintError,
    compute_toolchain_fingerprint,
    fingerprint_is_unavailable,
    fingerprint_or_unavailable,
    toolchain_fingerprints_match,
)
from orchestrator_mvp.replay.workspace import (
    DisposableWorkspace,
    compute_workspace_result_digest,
)

__all__ = [
    # Arms
    "ARM_A_ID",
    "ARM_B_ID",
    "ARM_C_ID",
    "ARM_D_ID",
    "ARM_D_NOT_AVAILABLE",
    "ArmResult",
    "build_all_arms",
    "build_arm_a",
    "build_arm_b",
    "build_arm_c",
    "build_arm_d",
    # Comparison
    "assert_comparable",
    # Context pack
    "ContextClass",
    "ContextPack",
    "ContextSection",
    "build_context_pack",
    "render_context",
    # Contracts
    "ComparabilityVerdict",
    "ComparisonResult",
    "DeterministicVerifier",
    "DisposableWorkspace",
    "FailureClassification",
    "FrozenStartingState",
    "MeasurementRecord",
    "RealProjectRegistry",
    "RegisteredProject",
    "ReplayDefinition",
    "ReplayEngine",
    "ReplayError",
    "ReplayExecutionRecord",
    "ReplayProjectMismatchError",
    "ReplaySafetyViolationError",
    "ReplayStateMismatchError",
    "ReplayTaskDefinition",
    "ReplayVerificationError",
    "ToolchainComparabilityError",
    "UNKNOWN",
    "compute_workspace_result_digest",
    "get_initial_corpus",
    "is_unknown",
    "verify_project_repository",
    # Experiment
    "SEMANTIC_EFFECT_MEASURED",
    "SEMANTIC_EFFECT_UNMEASURED",
    "ExperimentResult",
    "TaskExperimentEvidence",
    "run_c12b_experiments",
    "write_experiment_evidence",
    # Toolchain
    "PackageInfo",
    "TOOLCHAIN_UNAVAILABLE",
    "VERIFIER_TOOLCHAIN_PATH",
    "ToolchainFingerprint",
    "ToolchainFingerprintError",
    "compute_toolchain_fingerprint",
    "fingerprint_is_unavailable",
    "fingerprint_or_unavailable",
    "toolchain_fingerprints_match",
]
