"""C06 durable observability and deterministic run reconstruction.

This package contains no provider or model execution.  Dispatch evidence is
created by the callers at the real adapter boundary; snapshot functions only
read persisted state.  The trace recorder (:mod:`saberops.observability.trace`)
provides C11-C structured-execution-trace evidence and never becomes a
parallel reconstruction authority.  The phase recorder
(:mod:`saberops.observability.phase_telemetry`) provides C14-C
production phase telemetry (ROADMAP §11) and never becomes an authority for
routing, effort, scheduling, lifecycle, review, or validation.
"""

from saberops.observability._core import (
    build_run_observability_snapshot,
    canonical_snapshot_json,
    new_dispatch_evidence,
    normalize_remaining_percent,
    prompt_evidence,
    prompt_sections,
    update_dispatch_from_result,
)
from saberops.observability.phase_telemetry import (
    PhaseRecorder,
    assert_canonical_phase,
    list_phase_observations,
    mark_phase_not_applicable,
    mark_phase_unknown,
    measure_phase,
    record_phase_observation,
    summarise_observations,
)
from saberops.observability.run_report import (
    REPORT_SCHEMA,
    REPORT_SCHEMA_VERSION,
    UNKNOWN,
    build_run_report,
    canonical_report_json,
    is_terminal_report,
    render_monitor_frame,
    render_report_text,
    saberops_version,
)
from saberops.observability.trace import (
    FAILURE_KIND_MAP,
    AttemptTraceRecorder,
    classify_failure,
    is_autonomous_eligible,
    operation_for_capability,
)

__all__ = [
    "FAILURE_KIND_MAP",
    "AttemptTraceRecorder",
    "PhaseRecorder",
    "REPORT_SCHEMA",
    "REPORT_SCHEMA_VERSION",
    "UNKNOWN",
    "assert_canonical_phase",
    "build_run_observability_snapshot",
    "build_run_report",
    "canonical_report_json",
    "canonical_snapshot_json",
    "classify_failure",
    "is_autonomous_eligible",
    "is_terminal_report",
    "list_phase_observations",
    "mark_phase_not_applicable",
    "mark_phase_unknown",
    "measure_phase",
    "new_dispatch_evidence",
    "normalize_remaining_percent",
    "operation_for_capability",
    "prompt_evidence",
    "prompt_sections",
    "record_phase_observation",
    "render_monitor_frame",
    "render_report_text",
    "saberops_version",
    "summarise_observations",
    "update_dispatch_from_result",
]
