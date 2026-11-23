# ADR-0068: One recovery tool, `resume_job`, from scratch or from the saved candidate

- **Status:** Accepted. Phase 1.1 lane H7.
- **Date:** 2026-11-23
- **Decider:** Saber (owner), recorded answer 2026-11-22 and 2026-11-23
- **Related:** [ADR-0016](0016-steering-note-channel.md) (note delivery at the
  next attempt), [ADR-0045](0045-owner-confirmed-resume.md) (the work gate's
  `orch resume` stays the pause confirmation), [ADR-0008](0008-candidate-bound-acceptance.md)
  (one candidate per decision).

## 1. Context

Seven CLI verbs covered recovery: `retry`, `continue`, `try-worker`,
`refresh`, `recover`, `reconcile` and `handoff`. Only `refresh_candidate` was
reachable over MCP, so a planner could not retry or continue a failed job from
chat. The planner had to ask the owner to leave the session and type a command,
which is exactly the judgment the planning CLI is better placed to make.

The verbs also mixed two public decisions with engine internals. A person or
planner needs two choices: start again, or keep the work that was saved.
`recover`, `reconcile`, automatic repair, nudge, preemption and failover are
engine housekeeping and are never a caller's choice.

## 2. Decision

1. **One public recovery tool:** the MCP tool `resume_job` takes a `job_id`,
   `from: "scratch" | "last_candidate"`, an optional `note` and an optional
   `worker_binding`. `orchestrator.resume_job` is the single service
   implementation, and `orch resume-job` is the one owner verb. `orch resume`
   is unchanged and remains the work-gate confirmation (ADR-0045).
2. **It maps onto the existing paths, it does not reimplement them.**
   `scratch` calls the retry path and `last_candidate` calls the continue path
   with the job's failed run. `worker_binding` is the folded-in `try-worker`
   path and is valid only with `scratch`. `refresh` stays its own verb.
3. **It refuses with a reason when neither choice applies.** A job that is not
   failed is refused (`JOB_NOT_RESUMABLE`); a job with no dispatched run is
   refused (`JOB_HAS_NO_RUN`); a continue with no saved candidate returns the
   continuation module's own reason.
4. **The successor is linked to the job.** `FAILED -> RUNNING` with the
   successor as `current_run_id`, appended to `run_ids`, and a `resumed` job
   event carrying the source run, the choice and the note id. `get_job`,
   `wait_job`, preemption lookup and reconciliation therefore follow the new
   attempt instead of reporting the old failure. Job events are durable
   evidence; the transition CAS-guards the FAILED state and the source run.
5. **One successor per call.** A repeat while the job is RUNNING returns the
   successor it already has (`already_resumed: true`) and starts nothing. A
   continuation that already exists is returned by the continue path itself.
6. **The note is the rejection note.** A `note` passed to `resume_job` is
   recorded as an ordinary steering note (ADR-0016) with the resume, before the
   successor's first attempt is assembled, so the next attempt's prompt carries
   it. When the owner rejects a candidate with a correction, the planner passes
   that text as the resume note. The note cannot change authority: it reaches
   the worker's next attempt and nothing else.
7. **Old verbs are aliases for one release.** `orch retry`, `orch continue` and
   `orch try-worker` keep their arguments and printed result vocabulary, print
   a one-line deprecation notice naming their `orch resume-job` equivalent, and
   call the same service function. They are removed after this release.
8. **Engine internals stay internal.** `recover`, `reconcile`, automatic repair,
   nudge, quota preemption and attempt failover keep their current entry points
   and none becomes an MCP tool or owner verb. The automatic repair budget is
   unchanged.

## 3. Consequences

- A planner can retry and continue a failed job from chat, with the optional
  note and worker choice, on the same frozen task, policy and repository the
  job was created with.
- The job's state now reflects the resumed attempt, so a session that reconnects
  later sees RUNNING with the new run rather than a stale failure.
- Resuming a bare run from `orch run` still works by run id; it has no job to
  link and no note channel, and the tool says so when a note is given.
- A crash between the successor launch and the job transition can leave the job
  FAILED beside a started run. A continuation is repaired by the existing
  continuation idempotency on the next call; a fresh attempt is not, because
  starting a run and linking it cannot be one transaction across the detached
  supervisor. This is recorded as a known, bounded limitation rather than
  papered over with a second mechanism.
- Rewinding to an earlier PLAN.md lane stays out of scope until
  dependent-candidate invalidation exists.
