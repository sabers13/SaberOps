"""Durable job queue (M2-3): jobs package, state machine, service surface.

Nothing in this lane dispatches. The scheduler (M2-4b) will call
``job_dispatch_spec``, ``mark_dispatched``, ``reconcile_jobs`` and
``requeue``. Until then submitted jobs stay QUEUED.
"""

from saberops.jobs.models import (
    DISPATCHABLE_ROLES,
    NO_ROUTE_FOR_REQUIREMENTS,
    RUN_FAILED,
    RUN_MISSING,
    SIZE_HINTS,
    TERMINAL_JOB_STATES,
    Effort,
    Job,
    JobRequirements,
    JobRole,
    JobState,
    Priority,
    ResumeFrom,
    WaitingReason,
)

__all__ = [
    "DISPATCHABLE_ROLES",
    "NO_ROUTE_FOR_REQUIREMENTS",
    "RUN_FAILED",
    "RUN_MISSING",
    "SIZE_HINTS",
    "TERMINAL_JOB_STATES",
    "Effort",
    "Job",
    "JobRequirements",
    "JobRole",
    "JobState",
    "Priority",
    "ResumeFrom",
    "WaitingReason",
]
