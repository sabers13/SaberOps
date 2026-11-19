"""F8 job dependencies: the graph, its validation, and the acceptance gate.

A job may name other jobs it must not start before (``Job.depends_on``).  The
scheduler starts a dependant only after **every** dependency is *accepted*:
the dependency job reached ``SUCCEEDED`` and its run carries the durable
``run_accepted`` event the ``AcceptEngine`` writes when it integrates the
candidate.  A completed-but-unaccepted candidate is not enough — the owner's
acceptance is the event that makes a dependency's work real.

This module is the only place that answers that question.  ``jobs/store.py``
validates the graph before a job row is written; ``capacity/loop.py`` asks for
the blockers of every queued job on each tick and hands them to the pure
scheduler, which parks the job with a :class:`~saberops.jobs.models.WaitingReason`
instead of evaluating routes.  A job with no dependencies keeps the plain
dispatch path.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from saberops.jobs import store
from saberops.jobs.models import (
    DependencyBlocker,
    Job,
    JobState,
    WaitingReason,
)

#: Shown wherever a parked dependant is explained.  A failed dependency never
#: clears, so the only ways forward are owner actions.
DEPENDENCY_FAILED_GUIDANCE = (
    "A dependency failed, was cancelled, or is no longer readable, so this job "
    "can never start. Cancel it, or resubmit it against a replacement job."
)

#: Job states a dependency can end in after which it can still be accepted.
_ACCEPTABLE_JOB_STATES = frozenset({JobState.SUCCEEDED})


class DependencyCycleError(ValueError):
    """A ``depends_on`` edge would close a cycle in the job graph."""


class UnknownDependencyError(ValueError):
    """``depends_on`` names a job id that does not exist."""


def normalize_depends_on(values: Iterable[str] | None) -> tuple[str, ...]:
    """Clean a caller-supplied dependency list: trimmed, de-duplicated, ordered."""
    if not values:
        return ()
    if isinstance(values, str):
        raise ValueError("depends_on must be a list of job ids, not a string")
    try:
        items = list(values)
    except TypeError as exc:
        raise ValueError("depends_on must be a list of job ids") from exc
    cleaned: list[str] = []
    for raw in items:
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("depends_on entries must be non-empty job ids")
        entry = raw.strip()
        if entry not in cleaned:
            cleaned.append(entry)
    return tuple(cleaned)


def validate_job_dependencies(
    db: Any, *, job_id: str, depends_on: Sequence[str]
) -> None:
    """Refuse unknown dependencies and any edge that closes a cycle.

    ``job_id`` is the id the job will be stored under.  Every dependency must
    already exist, and walking its transitive dependencies must never reach
    ``job_id``.  Because dependencies are immutable once a job is stored, the
    current submit surface cannot produce a cycle; this check is the durable
    invariant every writer shares, including a future dependency-edit surface.
    """
    if not depends_on:
        return
    if job_id in depends_on:
        raise DependencyCycleError(f"job {job_id} cannot depend on itself")
    for dependency_id in depends_on:
        if store.get_job(db, dependency_id) is None:
            raise UnknownDependencyError(f"unknown dependency job: {dependency_id}")
        _walk(dependency_id, job_id, db, seen=set())


def _walk(start: str, target: str, db: Any, *, seen: set[str]) -> None:
    pending = [start]
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        job = store.get_job(db, current)
        if job is None:
            continue
        for dependency_id in job.depends_on:
            if dependency_id == target:
                raise DependencyCycleError(
                    f"dependency {start} transitively depends on {target}"
                )
            pending.append(dependency_id)


def run_is_accepted(db: Any, run_id: str) -> bool:
    """True iff the run carries an ``AcceptEngine`` ``run_accepted`` event.

    Runtime state is project-local while jobs live in the owner DB, so the
    event may live in the run's own project store rather than in ``db``.  The
    scheduler's store is always checked first; anything unresolvable is *not*
    accepted, so a dependant stays parked instead of starting early on missing
    evidence.
    """
    for candidate in _run_databases(db, run_id):
        try:
            events = candidate.get_events_by_type(run_id, "run_accepted")
        except Exception:
            continue
        if events:
            return True
    return False


def _run_databases(db: Any, run_id: str) -> list[Any]:
    """Stores whose events may hold ``run_id``'s acceptance: ``db``, then its project."""
    databases: list[Any] = [db]
    try:
        from saberops.db import Database
        from saberops.runtime import db_path_for_run

        path = db_path_for_run(run_id)
        if path is None:
            return databases
        db_path = getattr(db, "db_path", None)
        if db_path is not None and Path(db_path).resolve() == Path(path).resolve():
            return databases
        databases.append(Database(path))
    except Exception:
        return databases
    return databases


def job_is_accepted(db: Any, job: Job) -> bool:
    """True iff the job succeeded and one of its runs was accepted."""
    if job.state not in _ACCEPTABLE_JOB_STATES:
        return False
    return any(run_is_accepted(db, run_id) for run_id in job.run_ids)


def dependency_blockers(
    db: Any, jobs: Sequence[Job]
) -> dict[str, DependencyBlocker]:
    """Return the parked reason for every queued job whose dependencies block it.

    Jobs with no dependencies, or whose dependencies are all accepted, are
    absent from the result and take the ordinary scheduling path.  Reads are
    memoized per call so a fan-in of five jobs costs one read per dependency.
    """
    accepted: dict[str, bool] = {}
    blockers: dict[str, DependencyBlocker] = {}
    for job in jobs:
        if not job.depends_on:
            continue
        pending: list[str] = []
        failed: list[str] = []
        for dependency_id in job.depends_on:
            try:
                dependency = store.get_job(db, dependency_id)
            except (ValueError, TypeError, KeyError, AttributeError):
                # An unreadable dependency row is never proof the work landed;
                # park the dependant instead of failing the whole tick.
                dependency = None
            if dependency is None or dependency.state in (
                JobState.FAILED,
                JobState.CANCELLED,
            ):
                failed.append(dependency_id)
                continue
            if dependency_id not in accepted:
                accepted[dependency_id] = job_is_accepted(db, dependency)
            if not accepted[dependency_id]:
                pending.append(dependency_id)
        if failed:
            blockers[job.job_id] = DependencyBlocker(
                reason=WaitingReason.DEPENDENCY_FAILED,
                blocked_by=tuple(sorted(failed)),
                detail=DEPENDENCY_FAILED_GUIDANCE,
            )
        elif pending:
            blockers[job.job_id] = DependencyBlocker(
                reason=WaitingReason.DEPENDENCY,
                blocked_by=tuple(sorted(pending)),
                detail="Waiting for these jobs to be accepted.",
            )
    return blockers


__all__ = [
    "DEPENDENCY_FAILED_GUIDANCE",
    "DependencyCycleError",
    "UnknownDependencyError",
    "dependency_blockers",
    "job_is_accepted",
    "normalize_depends_on",
    "run_is_accepted",
    "validate_job_dependencies",
]
