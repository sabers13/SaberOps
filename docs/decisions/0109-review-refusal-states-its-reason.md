# ADR-0109: A refused review states its reason and the owner's next action

- **Status:** Accepted. Implemented in phase 1.1 lane H51.
- **Date:** 2026-11-27
- **Decider:** Saber (owner), phase 1.1 plan lane H51, written from the H37
  client check ("do it", 2026-11-27: fix D3, D5 and D9 before H14).
- **Related:** [ADR-0105](0105-resumed-job-completes-and-scheduler-continues.md)
  (a terminal run no longer leaves its job stuck), [ADR-0106](0106-owner-readings-keep-their-window.md)
  (a reviewer refusal names the routes it tried), the H37 report §8 D9, phase
  1.1 plan lane H51.

## 1. Context

H37 §8 D9: `wait_job` returned `timeout` twice while the job still read
`RUNNING`, and the candidate already said `REVIEW_UNAVAILABLE` in
`runs.result_summary`. The reason a review refused to run (quota, no eligible
independent reviewer, a reviewer timeout) lived in the `reviews` row's details,
in the `review_candidate_skipped` events and in the run-report warnings. To act
on it the owner had to read the database. The job never finishing was D1, fixed
by ADR-0105.

Checked on local `main` with H47 and H48: the stuck job now reconciles to
`FAILED`, and `job_status_view` fills `reason` from `runs.result_summary`. What
still did not reach the planner was the cause: the summary named only the
verdict enum (`terminal verdict REVIEW_UNAVAILABLE`). `wait_job`'s guidance was
the generic "resume the job", which for a quota refusal is the wrong next move.
D9's planner-visible half therefore reproduced.

## 2. Decision

1. **One projection states the refusal.** `jobs.service.review_refusal_view(db,
   job)` reads the last review of the job's current run and, when its verdict is
   `REVIEW_UNAVAILABLE` or `REVIEW_EXECUTION_FAILED`, returns the verdict, a
   machine reason, the routes tried (from the durable
   `review_candidate_skipped` events) and the owner's next action. It changes
   nothing and reads only durable rows.
2. **The reasons are the plan's three, plus two honest fallbacks.** `quota`
   (`quota_blocked` / `QUOTA_EVIDENCE_REQUIRED`, or a quota summary such as
   stale evidence), `no_independent_reviewer` (build-route equality, family
   exclusion, or an empty reviewer ladder), `timeout` (an execution failure
   whose evidence says `TIMEOUT`), `unavailable` (readiness, provider or prompt
   transport refused every route) and `execution_failed` (no usable verdict for
   any other reason). Each carries its own action: record quota evidence,
   select an independent reviewer, retry on the saved candidate, check
   preflight, or retry. The action always ends in `resume_job`
   `from='last_candidate'`, the one recovery verb.
3. **Every status surface states the same fact.** `job_status_view` carries the
   projection as `review` and appends its detail to `reason`; the job result
   projection (`get_job`, `wait_job` final) carries the same block as
   `result.review`; `_job_guidance` returns the projection's
   detail and action instead of the generic resume line. A refusal is judged
   from records, never from prose matching.
4. **The fail-closed run summary keeps the evidence.** The candidate-review
   fail-closed path writes the review's details into `runs.result_summary`, so
   `orch status`, the run report and a future reader see the cause without the
   projection.
5. **When a review is unavailable is unchanged.** Selection, quota admission,
   the red line and the refusal rules stay H48's; this lane only reports the
   outcome.

## 3. Consequences

- A planner that called `wait_job` now gets `outcome: "final"`, a `reason` that
  names the cause and a `guidance` with the exact next action, from chat alone.
- The refusal block is empty (`None`) for a substantive verdict, so passing and
  rejected-but-reviewed candidates read exactly as before.
- The projection is read-only and idempotent: repeated `wait_job`/`get_job`
  calls cannot change job state, and a scheduler tick still owns
  reconciliation (ADR-0105).
- The category is a stable vocabulary a later board or report can render; it
  is not a new review verdict or disposition.
