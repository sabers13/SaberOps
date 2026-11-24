"""Durable observability and deterministic run reconstruction.

This package contains no provider or model execution.  Dispatch evidence is
created by the callers at the real adapter boundary; snapshot functions only
read persisted state.  The one reader for run facts is
:func:`saberops.observability.run_report.build_run_report`; ``orch status``,
``orch report``, ``orch watch``, ``catch_up`` and ``tail_job`` all consume it.
The trace recorder (:mod:`saberops.observability.trace`) provides
structured-execution-trace evidence and never becomes a parallel
reconstruction authority.  The phase recorder
(:mod:`saberops.observability.phase_telemetry`) persists production phase
telemetry and never becomes an authority for routing, effort,
scheduling, lifecycle, review, or validation.
"""

from saberops.observability._core import (
    new_dispatch_evidence,
    normalize_remaining_percent,
    prompt_evidence,
    prompt_sections,
    update_dispatch_from_result,
)
from saberops.observability.phase_telemetry import (
    PhaseRecorder,
    mark_phase_not_applicable,
    mark_phase_unknown,
    measure_phase,
    record_phase_observation,
)
from saberops.observability.run_report import (
    REPORT_SCHEMA,
    REPORT_SCHEMA_VERSION,
    UNKNOWN,
    build_roles_view,
    build_run_report,
    canonical_report_json,
    is_terminal_report,
    render_monitor_frame,
    render_report_text,
    run_explanation,
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
    "build_roles_view",
    "build_run_report",
    "canonical_report_json",
    "classify_failure",
    "is_autonomous_eligible",
    "is_terminal_report",
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
    "run_explanation",
    "saberops_version",
    "update_dispatch_from_result",
]
