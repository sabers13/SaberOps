"""Durable job queue: jobs package, state machine, service surface.

Nothing here dispatches. The scheduler calls ``job_dispatch_spec``,
``mark_dispatched``, ``reconcile_jobs`` and ``requeue``; until it does, a
submitted job stays QUEUED.
"""

from saberops.jobs.models import (
    DISPATCHABLE_ROLES,
    NO_ROUTE_FOR_REQUIREMENTS,
    PREEMPTION_LIMIT,
    RUN_FAILED,
    RUN_MISSING,
    SIZE_HINTS,
    TERMINAL_JOB_STATES,
    Effort,
    Job,
    JobKind,
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
    "PREEMPTION_LIMIT",
    "RUN_FAILED",
    "RUN_MISSING",
    "SIZE_HINTS",
    "TERMINAL_JOB_STATES",
    "Effort",
    "Job",
    "JobKind",
    "JobRequirements",
    "JobRole",
    "JobState",
    "Priority",
    "ResumeFrom",
    "WaitingReason",
]
