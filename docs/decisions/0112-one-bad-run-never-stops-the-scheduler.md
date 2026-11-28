# ADR-0112: One bad run never stops the scheduler, and the owner can see it

- **Status:** Accepted. Implemented in phase 1.1 lane H54.
- **Date:** 2026-11-28
- **Decider:** Saber (owner), phase 1.1 lane H54, written from the H37 recheck
  on 2026-11-28 ("yes start those four, do option 2 for N3").
- **Related:** [ADR-0011](0011-bounded-automatic-actions.md) (dispatch-window
  recovery), [ADR-0105](0105-resumed-job-completes-and-scheduler-continues.md)
  (a terminal run never holds the store), the H37 recheck report §9 N2 and
  §6 D1.

## 1. Context

A job dispatch writes the run row and reserves capacity before it launches the
detached execution owner. When that launch fails after the run row exists, the
job stays `RUNNING` on a `PENDING` run; when the reservation is already gone
(for example a resumed successor, or a launch path that released it), the
scheduler's fail-closed RUNNING guard classified the job as
`legacy_running_without_reservation`. The continuous loop treated that tick
result as fatal and exited, leaving every other queued job stuck until an owner
ran `orch scheduler start` again. The H37 recheck saw four such exits in one
session. H47 (ADR-0105) only let a run that was *already terminal at tick time*
past the guard; a `PENDING` run whose owner was gone still stopped the store.

The owner also had no way to tell whether a scheduler was running, when it last
ticked, or why it stopped. The lease file and schedules transcript were the
only evidence, and neither is an owner-facing status.

## 2. Decision

1. **A failed launch keeps its cause on the run.** The dispatch exception path
   writes one `run_launch_failed` event with a bounded reason
   (`<ExceptionType>: <message>`, at most 500 characters) when the run row
   exists. The event is evidence only; dispatch does not decide the run's fate
   from the exception alone.
2. **The next tick settles an abandoned launch.** `settle_failed_launch` acts
   only on a job whose current run is `PENDING`, whose execution owner is
   provably `DEAD` (no claim, a released claim, or a dead claimed process),
   and which is not a native cooperative run or a cancellation in progress.
   It terminalizes the run `FAILED` with the recorded launch reason (or "the
   launch never produced a live execution owner") and the owner action to fix
   the cause and resubmit, releases the reservation idempotently, and fails
   the job with `LAUNCH_FAILED`. The failed job appears in the tick's
   reconciled changes. A `LIVE` or `UNKNOWN` owner is never acted on, so a
   launch still in progress and an unreadable ownership store keep the
   existing reconcile/hold path.
3. **The legacy-RUNNING hold is resumable, not fatal.** The continuous loop
   keeps polling on `legacy_running_without_reservation` (as it already did on
   `running_run_status_unresolved`) and logs the owner-visible job ids once per
   distinct hold. A genuine live unreserved run still holds the tick; this ADR
   does not change capacity reservation semantics. One `--once` tick still
   reports the hold as an error exit because a one-shot command has no later
   tick to keep alive.
4. **`orch scheduler status` reports the scheduler.** Liveness comes from the
   existing lease (`scheduler_alive`). The last tick, outcome and error come
   from a durable per-store status file written by `run_scheduler_loop` after
   each tick and once on exit, with `holder_id`, pid, timestamps and the
   blocked job ids. Writes are best-effort and never raise into the loop; a
   missing or corrupt file reports "never recorded" rather than failing the
   verb. `--json` returns the same shape.
5. **No second recovery path and no reservation redesign.** Settlement uses
   the existing fenced run terminalization and job compare-and-set; the
   owner's recovery verbs are unchanged. The dirty-tree cause that produced
   the failed launch is N5 and stays out of scope.

## 3. Consequences

- A launch failure that leaves a `PENDING` run fails exactly that job, names
  its cause and the owner action, releases its capacity, and the same tick can
  dispatch independent queued work. The scheduler does not need a manual
  restart.
- A live unreserved run still holds the whole tick, as ADR-0105 requires; the
  difference is that the scheduler process stays up and the hold is visible
  through `orch scheduler status` and the warning log instead of a silent exit.
- A stopped scheduler now leaves `last_tick_at`, `last_outcome` and
  `last_error` behind for the owner, including the reason codes that used to
  appear only on stderr of a process that had already exited.
- The job's `LAUNCH_FAILED` reason is a new member of `FAILURE_REASONS`; older
  rows are unaffected, and existing `RUN_FAILED` jobs keep their meaning.
- Evidence: `tests/test_h54_scheduler_resilience.py` drives a real durable
  store and tick with a stubbed launch that writes a `PENDING` run and raises,
  the exact unreserved `PENDING`-run shape from H37, a continuous loop that
  polls past a legacy hold, and the status verb reporting a stopped scheduler
  with its last error.
