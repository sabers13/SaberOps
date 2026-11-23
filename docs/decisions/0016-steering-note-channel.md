# ADR-0016: Owner steering notes reach a worker only at an attempt boundary

- **Status:** Accepted
- **Date:** 2026-11-19
- **Amended:** 2026-11-23 (phase 1.1 lane H6): redirect, and no acceptance past
  an undelivered note
- **Decider:** Saber (owner), via the Phase 1 plan lane F13 and the phase 1.1
  lane H6
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
   record keeps it, `catch_up` reports that it is undelivered, and a candidate
   with an undelivered note on its job **cannot be accepted** (see 8).
5. `catch_up` and the job record show every note with its text, creation time
   and delivery state, so a session that did not send the note can see it.
6. A note cannot change authority: it cannot widen billing consent, bypass the
   gate, the independent review, acceptance or isolation, and it cannot change
   the frozen task, routing or control-plane contract of the run. It is
   corrective text for the worker's next attempt only.
7. **Redirect** (`redirect_job` over MCP, `orch job redirect`) is the answer
   when the next attempt is not soon enough. It records the note first, then
   stops the running attempt at the supervision safe point (the same fenced
   termination a pause uses, never an input into the process), captures the
   attempt's work as a checkpoint commit plus resume capsule, and requeues the
   job with `waiting_reason=REDIRECTED`. The next scheduled attempt restores
   that checkpoint and carries the note in its prompt through 2-3. A durable
   `redirect_requested` marker lets crash recovery finish the same redirect;
   a redirect is a deliberate correction, so it never spends the preemption
   budget, and the checkpoint's recorded reason is the interruption that
   actually happened rather than quota exhaustion. Native children cannot be
   stopped at a safe point, so redirect refuses for them and `note_job` stays
   the path (their heartbeat already delivers notes).
8. **Acceptance is blocked while a note is undelivered.** The accept engine,
   its read-only eligibility checklist and the away-mode auto-accept pass use
   one predicate: a note on the run's job with no `delivered_at` and no
   `discarded_at`. The refusal names the note ids and the two ways out, and
   `catch_up` offers the same instruction instead of an accept command:
   `redirect_job` to deliver the note, or `discard_note` to drop a note that no
   longer applies.
9. **Discard is explicit.** `discard_note` records `discarded_at` on a note
   that never reached a worker and keeps the row visible; a delivered note is
   evidence of what the worker was told and is never discarded. Starting a new
   job is not a way to forget a note: the old job keeps it and its candidate
   stays unaccepted until it is redirected or discarded.

## 3. Consequences

- Mid-run corrections are safe and auditable instead of depending on a live
  terminal or a restart.
- A job that finishes before another attempt keeps its note visibly pending
  rather than silently dropping it, and its candidate stays unaccepted until
  the planner redirects or discards the note. The owner is told which note and
  which two actions, so "why can't I accept" never needs a log dig.
- A redirect costs one safe-point stop and one restored checkpoint, not lost
  work: the continuation attempt starts from the preserved state, under the
  ordinary admission, gate, review and acceptance rules.
- The worst case of an undelivered note is a blocked acceptance, never a
  published change the owner had already asked to correct. A note that is no
  longer relevant is dropped on purpose, not forgotten.
- The owner or planner can send a note through the MCP `note_job` tool or
  `orch job note`; both are ordinary writes to durable job state. `redirect_job`
  and `discard_note` (and their `orch job redirect` / `orch job note-discard`
  forms) are the two corrections to a pending note.
- Automatic nudging of an idle worker uses this same channel (implemented in
  lane F14 behind `ORCH_NUDGE_IDLE_WORKERS`): it records one bounded continue
  note rather than typing into the process. The nudge is spent once per job, and
  a second stall raises attention without another note.
- Advisor checkpoints (ADR-0034, lane F26) record their advice through the same
  channel with `source=advisor`. Advice is not owner authority and is labelled
  as advice in the prompt and in every note view; delivery semantics are
  identical.

## 4. H6 evidence

The lane's rig tests (`tests/test_h6_redirect.py`) run on the real lifecycle rig
and the acceptance fixture: a note recorded while a job runs, followed by a
successful attempt, refuses acceptance and away-mode auto-accept with the note
ids and both actions; a redirect stops the attempt, keeps its marker file in
the checkpoint, requeues the job without spending the preemption budget, and the
continuation attempt's prompt contains the note exactly once; discarding the
note unblocks a real fast-forward acceptance; a queued job and a delivered note
are both refused by `discard_note`/`redirect_job` with clear reasons.
