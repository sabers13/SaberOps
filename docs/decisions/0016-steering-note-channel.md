# ADR-0016: Owner steering notes reach a worker only at an attempt boundary

- **Status:** Accepted
- **Date:** 2026-11-19
- **Decider:** Saber (owner), via the Phase 1 plan lane F13
- **Related:** [ADR-0008](0008-candidate-bound-acceptance.md) (candidate evidence)
  and [ADR-0001](0001-saberops-engine-mcp-first.md) invariants (owner authority,
  one accountable worker).

## 1. Context

Course corrections arrive while work is already running: the owner or the
planner sees a worker taking the wrong route and wants to redirect it. Typing
into a live worker is unsafe: the process may be mid-tool-call, its stdin
contract belongs to the vendor CLI, and a partially read instruction can corrupt
a turn. Ending and restarting the work throws away the in-flight attempt.

The engine already has bounded retries, repair attempts and continuation
attempts. Every one of them builds a fresh prompt before the worker process
starts. That boundary is the safe point at which new owner input can be added.

## 2. Decision

1. A steering note is **durable state on the job**, not a live process input.
   It is stored with its creation time, its source (`owner` or `advisor`) and
   its delivery record in the owner database, and appears in the job event
   history.
2. A note is delivered **only inside the prompt of the next worker attempt**
   that starts for the job's current run — the first attempt for a queued job,
   otherwise the next retry, repair or continuation attempt. Nothing is ever
   written into a running worker's stdin or session mid-tool.
3. Delivery is **once**. The attempt that carries a note records
   `delivered_at`, the run ID and the attempt ID, and a later attempt does not
   repeat it. At most five pending notes travel in one prompt; further notes
   wait for the following attempt boundary.
4. A note is **pending, not lost**, when no further attempt starts: the job's
   record keeps it, `catch_up` reports that it is undelivered, and the owner can
   decide whether to resubmit. Notes for terminal jobs are refused.
5. `catch_up` and the job record show every note with its text, creation time
   and delivery state, so a session that did not send the note can see it.
6. A note cannot change authority: it cannot widen billing consent, bypass the
   gate, the independent review, acceptance or isolation, and it cannot change
   the frozen task, routing or control-plane contract of the run. It is
   corrective text for the worker's next attempt only.

## 3. Consequences

- Mid-run corrections are safe and auditable instead of depending on a live
  terminal or a restart.
- A job that finishes before another attempt keeps its note visibly pending
  rather than silently dropping it.
- The owner or planner can send a note through the MCP `note_job` tool or
  `orch job note`; both are ordinary writes to durable job state.
- Automatic nudging of an idle worker uses this same channel (implemented in
  lane F14 behind `ORCH_NUDGE_IDLE_WORKERS`): it records one bounded continue
  note rather than typing into the process. The nudge is spent once per job, and
  a second stall raises attention without another note.
- Advisor checkpoints (ADR-0034, lane F26) record their advice through the same
  channel with `source=advisor`. Advice is not owner authority and is labelled
  as advice in the prompt and in every note view; delivery semantics are
  identical.
