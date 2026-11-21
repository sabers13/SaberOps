"""Stop, preserve and resume work when the owner is not there (F17).

The gate (:mod:`saberops.runtime_gate`) says *may work start*.  This module is
the part that acts on a closed gate:

* every RUNNING job is stopped at a safe point through the existing fenced
  termination path and parked with a verified checkpoint and resume capsule, so
  the worktree, the work and the evidence survive;
* nothing is resumed implicitly -- :func:`resume_work` is the one call the
  owner makes through ``orch resume``, and no agent can clear a pause;
* the engine records one event and sends one notice per pause.

No job is ever discarded here.  A stopped run is ``PREEMPTED`` (a terminal run
state that is not a failure), its attempt is closed, and the job returns to
``QUEUED`` with ``waiting_reason=PAUSED`` and ``resume_from`` pointing at the
checkpoint.  When the gate opens, the ordinary scheduler re-dispatches it and
the ordinary launcher restores the checkpoint through ``prepare_resume``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from saberops.runtime_gate import (
    GateState,
    PauseReason,
    clear_pause,
    evaluate_gate,
    record_pause_event,
    refresh_gate,
    register_lane_submit_gate,
    set_pause,
)

__all__ = [
    "pause_now",
    "pause_running_jobs",
    "resume_work",
    "work_allowed",
]


def work_allowed(db: Any) -> tuple[bool, GateState]:
    """Open gate answer for a start path, refreshed so a pause becomes durable."""
    state = refresh_gate(db)
    return (not state.closed), state


def pause_running_jobs(db: Any, *, reason: str, detail: str = "") -> list[str]:
    """Stop every RUNNING job's worker and park the job, preserving its work.

    Returns the job ids that are (now) parked.  Idempotent: a job already
    parked by an earlier pass is skipped, and any failure is recorded on the run
    rather than raised into the scheduler tick.  A worker that cannot be proven
    stopped is left alone and retried next tick, never force-failed.
    """
    from saberops.jobs import store
    from saberops.jobs.models import JobState, WaitingReason
    from saberops.preemption import complete_preemption
    from saberops.supervisor import RunSupervisor

    supervisor = RunSupervisor(db=db)
    parked: list[str] = []
    try:
        jobs, _ = store.list_jobs_tolerant(db, state=JobState.RUNNING, limit=1_000)
    except Exception:  # pragma: no cover - a read error must not break the tick
        logging.getLogger(__name__).exception("could not list running jobs to pause")
        return parked

    for job in jobs:
        run_id = job.current_run_id
        if not run_id:
            continue
        from saberops.native_workers import native_run_marker, request_native_stop

        if native_run_marker(db, run_id) is not None:
            # F16/F17: SaberOps cannot kill a native child and there is no
            # supervised worker to terminate or checkpoint.  Post the stop the
            # orchestrator acts on and keep the job RUNNING so its committed
            # worktree is preserved; the closed gate refuses any new native
            # claim and any report until `orch resume`.  It is deliberately not
            # reported as parked: the child may still be alive.
            try:
                request_native_stop(
                    db,
                    job,
                    reason=reason,
                    detail=(
                        detail
                        or "the work gate is closed; stop this native child and keep "
                        "its worktree"
                    ),
                )
            except Exception as exc:  # pragma: no cover - defensive
                logging.getLogger(__name__).warning(
                    "native pause stop for %s failed: %s", run_id, exc
                )
            continue
        try:
            stopped = supervisor.pause(run_id, reason=reason)
        except Exception as exc:  # pragma: no cover - defensive
            logging.getLogger(__name__).warning("pause of %s failed: %s", run_id, exc)
            stopped = False
        if stopped:
            try:
                complete_preemption(
                    db,
                    run_id=run_id,
                    repo_path=job.repo,
                    job_id=job.job_id,
                    decision_ref=f"pause:{reason}",
                    interruption="pause",
                )
            except Exception as exc:  # pragma: no cover - recovery retries it
                logging.getLogger(__name__).warning(
                    "pause park of %s failed: %s", run_id, exc
                )
        try:
            current = store.get_job(db, job.job_id)
        except Exception:
            current = None
        if current is not None and (
            current.waiting_reason == WaitingReason.PAUSED
            or current.state is not JobState.RUNNING
        ):
            parked.append(current.job_id)
        elif not stopped:
            # The worker is not provably stopped yet: keep it visible so the
            # next tick retries, and report the gate reason on the run.
            try:
                db.record_event(
                    run_id,
                    "pause_pending",
                    payload={"reason": reason, "detail": detail},
                )
            except Exception:
                pass
    return parked


def pause_now(
    db: Any,
    *,
    reason: PauseReason | str = PauseReason.OWNER,
    detail: str = "",
    by: str = "owner",
    now: Any = None,
) -> GateState:
    """Close the gate immediately and park every running worker.

    Used by ``orch pause`` and the web toggle, so an owner stop does not wait
    for the next scheduler tick.  The sticky record is written first: if the
    process dies mid-way, the scheduler still sees a closed gate.
    """
    resolved = reason if isinstance(reason, PauseReason) else PauseReason(str(reason))
    register_lane_submit_gate()
    set_pause(
        resolved,
        detail or resolved.value,
        by=by,
        now=now,
    )
    parked = pause_running_jobs(db, reason=resolved.value, detail=detail)
    state = set_pause(
        resolved,
        detail or resolved.value,
        by=by,
        paused_jobs=tuple(parked),
        now=now,
    )
    record_pause_event(state)
    return state


def resume_work(db: Any, *, by: str = "owner", now: Any = None) -> GateState:
    """Clear the pause and hand parked work back to the ordinary scheduler.

    This is the owner's explicit confirmation (``orch resume``); it is not
    exposed as an MCP tool and no agent session can call it.  Before opening
    the gate, any run that the engine already stopped but could not finish
    parking is parked now, so the next tick never mistakes a paused run for an
    abandoned one and orphans it.  Runs are never relaunched here: the job rows
    are the dispatch input.
    """
    from saberops.preemption import complete_preemption, find_job_for_run

    try:
        state = evaluate_gate(db=db)
        for run_id in state.paused_runs:
            job = find_job_for_run(db, run_id)
            if job is None:
                continue
            try:
                complete_preemption(
                    db,
                    run_id=run_id,
                    repo_path=job.repo,
                    job_id=job.job_id,
                    decision_ref="pause:resume_park",
                    interruption="pause",
                )
            except Exception:
                continue
    except Exception:  # pragma: no cover - resume must still clear the gate
        pass
    cleared = clear_pause(by=by, now=now)
    try:
        from saberops.capacity.loop import wake_scheduler
        from saberops.jobs import store

        store.ensure_tables(db)
        wake_scheduler()
    except Exception:
        pass
    return cleared


def paused_run_paths(db: Any) -> list[str]:
    """Worktree paths of paused runs, for a return summary that shows the work."""
    from saberops.jobs import store
    from saberops.jobs.models import JobState, WaitingReason

    paths: list[str] = []
    try:
        jobs, _ = store.list_jobs_tolerant(db, state=JobState.QUEUED, limit=10_000)
    except Exception:
        return paths
    for job in jobs:
        if job.waiting_reason is not WaitingReason.PAUSED:
            continue
        resume = job.resume_from
        if resume is not None:
            paths.append(str(resume.checkpoint_sha))
        elif job.repo:
            paths.append(str(Path(job.repo)))
    return paths
