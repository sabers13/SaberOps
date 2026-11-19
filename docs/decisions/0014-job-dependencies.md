# ADR-0014: Job dependencies wait for acceptance, not completion

- **Status:** Accepted. Implemented in Phase 1 lane F8 (Wave 3).
- **Date:** 2026-11-19
- **Decider:** Saber (owner), Phase 1 plan lane F8
- **Related:** [ADR-0001](0001-saberops-engine-mcp-first.md) (pacing scheduler),
  [ADR-0006](0006-stale-candidate-refresh.md) (candidate refresh),
  [ADR-0008](0008-candidate-bound-acceptance.md) (candidate-bound acceptance),
  [ADR-0011](0011-bounded-automatic-actions.md) (no silent stall)

## 1. Context

A planner had to stay in its chat session to submit the next step of a
multi-step task after the earlier step was accepted, because a job had only a
`not_before` time gate and no way to say "start after that job lands". The
scheduler had no dependency concept at job level, so a caller that submitted
everything up front either ran steps out of order or hand-sequenced them.

The engine already records what a dependency needs to mean: a completed
candidate is not the same as accepted work. A run is COMPLETED when its worker,
gate and independent review finished; only `AcceptEngine` writes the durable
`run_accepted` event that integrates the candidate into the target branch.
Treating "the run finished" as done would let a dependant build on a candidate
the owner may still reject.

## 2. Decision

1. **`depends_on` names jobs, not runs.** A job may list other job ids
   (`depends_on`, `--depends-on` on `orch job submit`, `depends_on` on the MCP
   `submit_job` tool). The list is part of the request identity: the same
   idempotency key with a different dependency list is a different request, not
   a replay.
2. **Accepted means accepted.** A dependency satisfies a dependant only when
   its job is `SUCCEEDED` **and** one of its runs carries the durable
   `run_accepted` event. A completed-but-unaccepted candidate keeps the
   dependant parked. The run's own store is consulted when runtime state is
   project-local; anything unresolvable fails closed as not accepted.
3. **Parking is the scheduler's decision, and it is visible.** A blocked
   dependant stays `QUEUED` with the `DEPENDENCY` waiting reason and the
   outstanding dependency ids recorded on its `waiting` event. It is never
   routed, admitted or dispatched while parked. A dependency that ends `FAILED`
   or `CANCELLED` can never be accepted, so the dependant parks with
   `DEPENDENCY_FAILED` instead: it never starts, and `wait_job` returns it as
   needs-attention at once rather than waiting out its timeout.
4. **Independent work is unaffected.** A job with no dependencies, or whose
   dependencies are all accepted, takes the ordinary pacing path. Dependencies
   order related work; they never serialise unrelated jobs.
5. **Cycles and unknown ids are refused before a row exists.** `jobs.store`
   validates the graph at the single durable choke point, so every writer —
   `submit_job`, the CLI, the MCP tool and any future dependency-edit surface —
   refuses an edge that closes a cycle or names a missing job. Since a stored
   job's dependency list is immutable today, the check is a standing invariant
   rather than a user-facing error path.
6. **No wave executor.** Dependencies are a job-level precondition. The deleted
   wave executor and its slot policy stay deleted; there is no plan-level
   scheduler here.

## 3. Consequences

- A planner can submit a chain or a fan-in in one session and leave; the queue
  advances as the owner accepts.
- A dependant ships from the base its dependency landed on only if it is
  dispatched after that acceptance — the base is still resolved at submit
  time. Refreshing a candidate onto a moved target stays the existing
  `refresh_candidate` path (ADR-0006); F12's landing queue is separate work.
- A failed dependency strands its dependants in a visible parked state that
  only owner action clears (cancel, or resubmit against a replacement). The
  engine does not guess a replacement dependency.
- Stores created before this change gain the `depends_on_json` column in place
  on the next writable open; earlier rows read as having no dependencies.
