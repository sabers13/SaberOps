# ADR-0013: A quota-exhausted run continues on another admitted binding

- **Status:** Accepted
- **Date:** 2026-11-19
- **Decider:** Saber (owner), Phase 1 plan lane F4
- **Related:** [ADR-0008](0008-candidate-bound-acceptance.md) (candidate-bound
  acceptance), [ADR-0011](0011-bounded-automatic-actions.md) §5 (`catch_up`),
  [ADR-0005](0005-mcp-first-engine-and-phase-2-board.md)

## 1. Context

When one subscription runs out mid-run, the work must not be lost and the owner
must not have to move the job by hand. A scheduler-dispatched job starts from the
route the scheduler admitted, which reaches the run as a provider/model pin. That
pin collapsed the candidate ladder to a single entry: after a quota-exhausted
attempt the engine re-dispatched the same exhausted provider until the run parked
without touching the other admitted routes.

Even when the run did continue on a second provider, the replacement attempt
started at the interrupted attempt's checkpoint, and acceptance refused that base
as not being the run's own — so preserved work could never become an accepted
candidate.

## 2. Decision

1. **A quota-exhausted attempt continues from its checkpoint.** The interrupted
   attempt is closed, its evidence is kept, its uncommitted work becomes a
   worker checkpoint on its own branch, and the next attempt starts from that
   checkpoint.
2. **A blocked route does not re-dispatch itself.** Once a provider is blocked in
   the current chain cycle (quota, binding or transport failure), the pinned
   route no longer short-circuits candidate selection: the engine re-resolves the
   role's routing chain and continues on the next admitted candidate. The
   scheduler's pin is a preference, not a re-dispatch command.
3. **Admission is unchanged.** Every fallback candidate passes the same
   control-plane, quota, readiness and capability checks before launch. This
   decision adds no route that could not already be dispatched, and it never
   weakens billing consent, independent review or owner acceptance.
4. **The checkpoint base is authorized by verified evidence.** A continuation
   whose attempt starts at a checkpoint rather than the run base records
   `failover_start_authorized` with the source attempt, anchor, checkpoint and
   start SHA. Acceptance and publication authorize that start only when the
   record re-verifies live: the source attempt's durable base equals the anchor,
   a worker checkpoint of this run exists for that source with exactly that
   checkpoint SHA, and the start is the anchor or a proven descendant of it. Any
   gap grants nothing and the candidate stays unacceptable.
5. **The handoff is visible without the run page.** `catch_up` returns the latest
   handoff (`switch`: reason, from/to provider, model and binding when recorded,
   checkpoint and its inheritance, and whether the interrupted work location is
   preserved). Per-attempt subscription and token usage come from the existing
   `usage` projection, so this adds no second source of truth. The lookup stays
   read-only.
6. **No admitted fallback is a visible parking, not a loop.** When every route
   is exhausted, the run parks through the existing preemption path with its work
   anchored and requeued, and `catch_up` reports the reason and preserved
   location.

## 3. Consequences

- A worker interrupted by quota keeps its partial work and its job keeps moving
  on the next admitted subscription without owner intervention.
- Exact owner-pinned runs keep their explicit fail-fast for an ineligible model;
  this decision changes only what happens after a genuine mid-run failure.
- Acceptance still refuses any base it cannot prove belongs to the run, so the
  continuation door is not a provenance hole.
- If no fallback route is admitted, the owner sees a parked run with the work
  preserved rather than a silent retry of an exhausted subscription.
