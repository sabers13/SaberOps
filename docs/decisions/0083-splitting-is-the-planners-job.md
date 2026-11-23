# ADR-0083: Splitting is the planner's job; the in-run team and run plan leave the core

- **Status:** Accepted. Implemented in phase 1.1 lane H2.
- **Date:** 2026-11-23
- **Decider:** Saber (owner), phase 1.1 owner answers Q7 and "maximise the
  deletion" (2026-11-22), and the 2026-11-23 restructuring answer
- **Related:** [ADR-0005](0005-mcp-first-engine-and-phase-2-board.md) (the
  planning CLI is the orchestrator), [ADR-0063](0063-planner-recorded-difficulty.md)
  (difficulty is the planner's call), [ADR-0080](0080-plan-scheduling-and-tool-registry.md)
  (PLAN.md scheduling stays)

## 1. Context

Phase 1.1's census (H0, section 7) traced a real scheduler-dispatched job and
confirmed that no owner, planner, CLI, MCP or `PLAN.md` entry can set
`execution_mode`:

- `resolve_work_packages` ran on every run with no semantic planner, so
  `deterministic_plan` always returned **one** package. A keyword score still
  decided `should_slice`, and a broad task persisted a durable run plan, work
  package and PACKAGE-mode worker projection for that single package.
- The legacy in-run team mode (`parallel_work.py`, `execution_mode="parallel"`,
  `max_parallel_workers`, `parallel_plan`) had no producer. All 59 traced B3
  runs were `execution_mode=single`.
- `planning.py`'s two integration record builders and
  `represent_independent_ready_work` had no in-repo caller at all.

Parallel work already moved to separate jobs and `PLAN.md` lanes (C4, P1-P6).
The phase 1.1 rule is that splitting is a judgment for the planner, not a
keyword score in the engine, and that code no measured path reaches is deleted
while every safety property stays.

## 2. Decision

1. **In-run sizing and slicing are deleted**: `slicing.py`
   (`assess_task_sizing`, `deterministic_plan`, `SemanticPlanner`,
   `validate_proposal`, `resolve_work_packages`, `ProposedPackage`,
   `TaskSizingAssessment`), with its tests.
2. **Ordinary runs persist no plan.** `orchestrator._run_core` always dispatches
   one worker with the full objective. `planning.py` (plan/step/package
   persistence, `build_context_capsule`, `mark_step`, `ready_dependents`, the
   two integration record builders) is deleted, and the dispatch loop carries
   no `current_plan_step` / `current_package` state.
3. **The legacy team mode is deleted**: `parallel_work.py`, the
   `execution_mode` / `max_parallel_workers` / `parallel_plan` knobs, team
   execution and team cascade pause/cancel, and the team section of the run
   report. A stored legacy parent/child marker is still read by the acceptance,
   continuation, refresh and claim guards, so an old internal team worker can
   never become an owner-acceptable candidate; the marker fields remain on
   `RunConfig` for that read-only purpose.
4. **Package-only context modes are deleted** from `context_projection.py`:
   `DispatchContextMode.PACKAGE`, `MODES_WITHOUT_FULL_TASK`'s PACKAGE member,
   `build_package_initial_projection`, `build_package_retry_projection`, and the
   projection `package_id` field. Retry, repair, final-repair, continuation and
   initial projections stay, and INITIAL remains the only mode that shows the
   full task.
5. **Chain-boundary recovery keeps only the tier boundary.**
   `_reconstruct_chain_attempt_start` still reconstructs the current tier chain
   from `tier_escalated` plus complete `attempt_started` markers, and fails
   closed on ambiguous evidence. Its package-linkage fallback is removed with
   the transitions that produced it.
6. **Continuation no longer validates a persisted run plan.** The parent's
   candidate, repository revision, worktree cleanliness and stopped-owner
   evidence are still proven; the plan-linkage check was reachable only for a
   legacy run-plan shape that no code creates now.
7. **Parallel work is several jobs or `PLAN.md` lanes.** `PLAN.md`, the plan
   store, `plan_runner`, job dependencies, continuation and handoff capsules
   are unchanged.

## 3. Consequences

- The core is smaller by **3,053 source lines** (`slicing.py` 263,
  `parallel_work.py` 572, `planning.py` 1007, and 1,211 lines from
  `orchestrator.py`, `context_projection.py`, `run_report.py`,
  `supervisor.py`, `continuation.py`, `provenance.py`, `config.py`,
  `routing_decision.py` and `ready_frontier.py`) and tests shrink by **2,850
  lines** net, including the 768-line `test_parallel_work.py`.
- A large multi-part task submitted as one job is one worker with the full
  objective and no durable plan rows or plan events; the same work written as
  `PLAN.md` lanes still runs as lanes. A new rig test covers the one-job case.
- A legacy store that still has a run plan, a team marker or the old knobs
  starts normally: the unknown knobs are ignored, the guards keep refusing a
  team child for acceptance, and the plan rows stay readable evidence.
- `RunPlan`, `PlanStep`, `WorkPackage`, their tables and the `db.py`
  persistence/inheritance helpers, the CLI run-plan view and the handoff
  capsule readers remain for stored evidence and are candidates for the later
  dead-code pass (H31), not this lane.
- `DispatchReason.PACKAGE_*` values and the nullable `package_id` /
  `plan_step_id` evidence fields remain for stored events and telemetry; no new
  run writes a non-null package value.
