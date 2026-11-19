# ADR-0011: What the engine may do without an owner request

- **Status:** Accepted. Scheduler presence, dead-run reconciliation, dispatch
  window recovery, the wait tool, desktop delivery, read-only quota probing and
  the `catch_up` lookup are implemented.
- **Date:** 2026-11-19
- **Decider:** Saber (owner), Phase 1 plan lanes C1, C2, C2B, C3, A5 and E5
- **Related:** [ADR-0001](0001-saberops-engine-mcp-first.md) §3.3–§3.4,
  [ADR-0005](0005-mcp-first-engine-and-phase-2-board.md),
  [ADR-0007](0007-dynamic-memory-budget.md)

## 1. Context

SaberOps runs long work after the owner has handed it off. A stall that only a
model turn could surface was the failure the owner wanted removed: a dead
worker or a stopped scheduler could leave a job looking alive, and a caller had
to ask again to learn anything. The same autonomy question applies to evidence
gathering: an automatic vendor interaction must not spend a prompt or change
vendor settings on the owner's behalf.

## 2. Decision

1. **The scheduler is always there.** Turning SaberOps on over MCP and starting
   the web app ensure exactly one scheduler runs for the store: start one if
   none is running, never start a second. A submission from another process is
   picked up promptly instead of waiting for the next 60-second tick. The loop
   never exits 0 on an error condition, and one unreadable job row is
   quarantined and reported rather than stopping all dispatch.
2. **Dead work becomes a visible state, and nothing is discarded.** Runs
   reconcile on every scheduler tick and at start. An orphaned run ends its job
   as failed with a reason and the preserved work location; a worker silent past
   its limit is surfaced as needing attention. No work is discarded. Injected
   failures become visible and actionable within 120 seconds, and a killed
   worker reaches a final state within 60 seconds, including across a scheduler
   restart.
   **Dispatch window.** A scheduler killed after the dispatch compare-and-swap
   (job RUNNING) but before its run row leaves a reserved capacity row with
   nothing running. That residue is repaired from durable records, never
   guessed: the dispatch event carries the dispatching lease generation and the
   store the run would have been written to, so a later tick acts only when the
   run row is provably absent from that store, the lease generation is
   superseded, and the dispatch is older than a short grace bound. The job is
   requeued exactly once with a recorded reason (it never started, so nothing
   ran), and failed with that reason if it loses a second dispatch the same way.
   A dispatch that recorded no such identity, or whose run is visible in
   another store, is held for owner resolution instead. One such row never
   blocks the rest of the tick, and the continuous scheduler keeps polling on
   that hold instead of exiting.
3. **Outcome delivery has no model in the loop.** An MCP wait tool returns a
   job's final or needs-attention outcome, or a timeout. A notifier sends a
   desktop notice on finished, failed, needs attention and question asked; it is
   on by default with one option to turn it off. MCP progress or log notices may
   be added as a bonus, never as the only channel.
4. **Automatic quota reads are passive.** When admission refuses for missing
   quota evidence, the engine may try its own quota reading only if that reading
   sends no prompt and changes no vendor setting. Otherwise the refusal names
   the exact command that supplies the evidence.
5. **Recovery from another CLI is read-only.** The `catch_up` lookup returns the
   original request, the current candidate, the latest check and review with the
   commit each assessed, any open question and the next action the engine
   allows, plus the reason and preserved work location on failure. It changes
   nothing, sends no notice and takes no lease, so a session without the writer
   lease can call it. The planner guide teaches the phrase "catch me up". It also
   reports each attempt's provider, model and binding and the latest handoff
   between attempts (for example a quota-exhausted attempt continued on another
   subscription from its checkpoint; see
   [ADR-0013](0013-quota-failover-continuation.md)).

## 3. Consequences

- Delivery does not depend on a live chat session, a model turn or a client
  polling at the right moment; polling still always works.
- Automatic actions cannot spend tokens or mutate vendor settings, and an
  automatic read is never the only way to obtain evidence.
- A missing scheduler, a dead worker or a missing quota reading is a reported
  state with a remedy, not a silent stall.
- A kill inside the dispatch window costs one re-dispatch, not a stranded job;
  an unprovable residue stays visible and actionable instead of being
  terminalized on a guess.
- The 60-second and 120-second bounds are the phase's acceptance targets; the
  benchmark lanes prove them on real clients, so this record does not itself
  certify them.
