# ADR-0105: A resumed job carries its job, and a terminal run never holds the store

- **Status:** Accepted. Implemented in phase 1.1 lane H47.
- **Date:** 2026-11-27
- **Decider:** Saber (owner), phase 1.1 plan lane H47, written from the H37
  client check. The lane was added after real Codex, Claude and OpenCode
  sessions lost every recovery verb on one stuck job.
- **Related:** [ADR-0068](0068-one-recovery-tool.md) (`resume_job`),
  [ADR-0081](0081-one-save-and-resume-record.md) (the one save-and-resume
  record), [ADR-0102](0102-commit-provenance-in-git.md) (the job id on the run
  row), [ADR-0011](0011-bounded-automatic-actions.md) (dispatch-window
  recovery), phase 1.1 plan lane H47, and the H37 report §8 D1.

## 1. Context

`resume_job` maps a failed job onto the existing retry and continue paths. Those
paths start the successor run directly through `RunSupervisor`, which is
deliberately outside the scheduler: no dispatch decision is made, so the
successor never gets a capacity reservation, and the run row was created without
the job id the plan's H44 had just added. Two observable failures followed:

1. When the successor run ended, the job stayed `RUNNING`. The scheduler's
   fail-closed RUNNING guard classified the job as
   `legacy_running_without_reservation` (a RUNNING job with no `RESERVED` row)
   and returned before any reconciliation. The whole store then dispatched
   nothing, `wait_job` never returned and no owner action was offered.
2. `orch job cancel`, `orch cancel` and `orch reconcile` all refused. For a
   PENDING run that never produced an attempt, its execution owner had already
   released itself; the cancellation path tried to fence a released generation
   and failed, so the job could not be closed either. The only way out was to
   edit SQLite by hand.

The guard itself is correct for a *live* unreserved run: per-pool occupancy and
debt are read from `RESERVED` rows, so treating an unreserved live run as zero
occupancy could over-admit an already-drained pool. The defect was applying a
liveness guard to a job that had no live run at all.

## 2. Decision

1. **Every resume path stamps the dispatching job on the successor run.**
   `RunSupervisor.start_supervised_run` takes an optional `job_id` and writes it
   to `runs.job_id`. `OrchestratorService.retry_run`, `continue_run` and
   `try_worker` pass the source run's `job_id`, falling back to the job store's
   run chain for rows written before H44. Refresh already copies the source run
   and so inherits the column; native jobs already set it. A plain `orch run`
   still has no job and stays `NULL`.
2. **A terminal current run is not live occupancy.** In the scheduler's RUNNING
   guard, an unreserved job whose current run is already terminal
   (`COMPLETED`, `FAILED`, `ORPHANED`, `PREEMPTED`) is not added to the
   store-wide hold. The tick continues, and the existing `reconcile_jobs`
   advances the job from its run's status (`COMPLETED` -> `SUCCEEDED`, `FAILED`
   -> `FAILED`). Settlement and release stay in the existing reconciliation
   branch and are no-ops when no reservation exists. One stuck job therefore
   never stops dispatch for other jobs.
3. **A live unreserved run still fails closed.** The lane does not redesign
   capacity reservations, so a resumed run that is still `PENDING`/`RUNNING`
   without a `RESERVED` row keeps holding the tick exactly as before. The
   scheduler loop treats that reason as a resumable hold and keeps evaluating
   it; once the run is terminal the new branch above lets the tick proceed.
   Counting an unreserved live run as occupancy would invent a pool it never
   reserved against.
4. **A released owner is not fenced again.** `RunSupervisor.cancel` treats an
   execution owner that already released itself as absent: there is no live
   claim to fence and no process to signal, so the run is terminalized without a
   cancellation claim and the job can be cancelled. A PENDING run with no
   attempt row and a released owner is exactly the shape H37 could not cancel.
   Unknown owner liveness still refuses, unchanged.
5. **No new verb.** Recovery stays `resume_job` plus the existing `orch job
   cancel`; the tick's completion reconciliation is the automatic half. The
   lane adds no second recovery path.

## 3. Consequences

- A job resumed from `scratch` or `last_candidate` reaches its terminal job
  state on the next scheduler tick after its run ends, with or without a
  reservation, and the scheduler dispatches other queued work in the same tick.
- `git log`-level provenance (ADR-0102) also covers resumed successors, because
  the job id is now on every run a job owns.
- While a resumed run is live, other jobs wait on the resumable hold. That is
  the truthful capacity answer under the current reservation design; a future
  lane that reserves on the resume path can remove it, and must keep the
  "every RUNNING job holds a live reservation" invariant.
- A released generation is terminalized without a CANCELLING claim. This does
  not weaken the fence for a live or unknown owner: only a generation that
  already released itself is treated as absent.
