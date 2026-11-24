"""Job dependencies: the graph, its validation, and the acceptance gate.

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
    JobKind,
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


def _canonical_sha(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if len(text) != 40 or any(ch not in "0123456789abcdef" for ch in text):
        return None
    return text


def accepted_landing_sha(db: Any, dependency_id: str) -> tuple[str, int, str] | None:
    """The newest ``run_accepted`` landing SHA of one accepted dependency.

    Only :class:`~saberops.accept.AcceptEngine` writes ``run_accepted``; its
    payload carries the integrated commit as ``candidate_sha``.  Runtime state
    is project-local, so the event may live in the run's own store; the
    ``(created_at, event_id)`` pair orders acceptances even across stores.
    Returns ``None`` when no readable acceptance exists -- a dependant then
    keeps its frozen base rather than guessing a revision.
    """
    dependency = store.get_job(db, dependency_id)
    if dependency is None or dependency.state not in _ACCEPTABLE_JOB_STATES:
        return None
    best: tuple[str, int, str] | None = None
    for run_id in dependency.run_ids:
        for candidate in _run_databases(db, run_id):
            try:
                events = candidate.get_events_by_type(run_id, "run_accepted")
            except Exception:
                continue
            for event in events:
                payload = getattr(event, "payload", None)
                if not isinstance(payload, dict):
                    continue
                sha = _canonical_sha(payload.get("candidate_sha"))
                if sha is None:
                    continue
                try:
                    event_id = int(getattr(event, "id", 0) or 0)
                except (TypeError, ValueError):
                    event_id = 0
                created_at = str(getattr(event, "created_at", "") or "")
                if best is None or (created_at, event_id) > (best[0], best[1]):
                    best = (created_at, event_id, sha)
    return best


def dispatch_base_sha(db: Any, job: Job) -> str:
    """The commit a job's run should start from.

    A job without dependencies keeps its submit-time ``base_sha``: that value
    is part of the request identity.  A dependant starts from what its
    dependencies actually landed.  The scheduler dispatches it only after every
    dependency is accepted, so the newest acceptance is the revision that
    contains the work the dependant was written against; anchoring on the
    submit-time base made the worker rebuild the old tree and fail with
    ``base_revision_mismatch``.
    """
    if not job.depends_on:
        return job.base_sha
    landings = [
        landed
        for dependency_id in job.depends_on
        if (landed := accepted_landing_sha(db, dependency_id)) is not None
    ]
    if not landings:
        return job.base_sha
    landings.sort(key=lambda item: (item[0], item[1]))
    return landings[-1][2]


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
    """True iff the job succeeded and one of its runs was accepted.

    A scout job has no candidate to accept: its deliverable is a report,
    and the engine considers the job complete when the report exists.  A
    succeeded scout therefore satisfies a dependency directly.
    """
    if job.state not in _ACCEPTABLE_JOB_STATES:
        return False
    if job.kind is JobKind.SCOUT:
        return True
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
    "accepted_landing_sha",
    "dependency_blockers",
    "dispatch_base_sha",
    "job_is_accepted",
    "normalize_depends_on",
    "run_is_accepted",
    "validate_job_dependencies",
]
