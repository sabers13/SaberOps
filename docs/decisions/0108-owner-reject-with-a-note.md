# ADR-0108: The owner rejects a candidate with a note, and that note resumes the work

- **Status:** Accepted. Phase 1.1 lane H50.
- **Date:** 2026-11-27
- **Decider:** Saber (owner), recorded answer 2026-11-27 ("do it", H37 D5)
- **Related:** [ADR-0068](0068-one-recovery-tool.md) (one recovery tool and the
  rejection note), [ADR-0016](0016-steering-note-channel.md) (notes reach a
  worker only at an attempt boundary), [ADR-0008](0008-candidate-bound-acceptance.md)
  (candidate-bound decisions), [ADR-0005](0005-mcp-first-engine-and-phase-2-board.md)
  (MCP-first planner actions)

## 1. Context

ADR-0068 promised that a rejection note becomes the resume note, and the MCP
guide tells the planner to "reject it and pass that text as the resume note".
No caller existed. `orch` had no `reject` verb, there was no MCP tool, and
`reject_current_candidate` had no production caller after the web reject route
was retired. `resume_job` accepted only `FAILED` jobs, so a planner facing a
succeeded candidate could only record a `note_job` note, which then blocked
acceptance without ever reaching a worker. The H37 real-session check recorded
this as D5.

## 2. Decision

1. **One rejection flow, two entry points.** `reject_candidate` is the MCP tool
   and `orch reject` the owner-facing verb. Both call
   `OrchestratorService.reject_job`; that method calls
   `reject_current_candidate`, the single authority that may write
   `candidate_rejected`, and writes no second rejection definition.
2. **A rejection requires a note.** The note is recorded as an ordinary
   steering note (ADR-0016) with `allow_terminal=True` before the rejection is
   written, because the resumed attempt is started in the same call. A call with
   neither a new note nor an already pending note is refused
   (`REJECT_NOTE_REQUIRED`) before anything is written. A note already pending
   on the job (for example one sent while the attempt ran) is delivered with the
   rejection rather than lost.
3. **The rejection ends that candidate, never the accepted state.** The
   canonical event makes the exact candidate unacceptable: the AcceptEngine
   refuses it before any Git mutation, and the lifecycle projection reports
   `REJECTED`. Acceptance remains terminal and outranks rejection, so an
   already accepted candidate refuses with `REJECT_ACCEPTED` and a rejection
   can never undo integration.
4. **The corrected attempt continues from the saved candidate.** Rejecting is a
   correction, not a failure, so the flow reuses the H47 linked-resume path with
   `from: last_candidate`: the successor run is linked to the job
   (`SUCCEEDED -> RUNNING`), the pending note becomes the section its first
   attempt renders, and the job follows the successor to a normal terminal
   state. `resume_job` therefore accepts a `SUCCEEDED` job only when its current
   candidate is proven owner-rejected; every other succeeded job still refuses.
5. **The continue path verifies the same evidence.** `verify_resume_source`
   accepts a `COMPLETED` run only when a matching `candidate_rejected` event
   names its candidate. A completed run that was not rejected still refuses
   with the existing reason, so neither the CLI nor the planner can continue
   arbitrary finished work.
6. **Only a settled succeeded candidate can be rejected.** A running job is
   steered with `note_job` or `redirect_job`; a failed job is recovered with
   `resume_job`. The reject flow refuses both instead of becoming a third
   steering mechanism.

## 3. Consequences

- A planner can carry out the owner's rejection from chat and the owner can do
  it from the terminal, with the same durable evidence and the same successor.
- The rejected candidate stays in the record and in the repository history, is
  reported as `REJECTED`, and cannot be accepted later; the corrected attempt
  produces a new candidate.
- A crash between the rejection write and the successor launch leaves a
  rejected candidate and a pending note, never an accepted one. The owner or
  planner can call `reject_candidate` again: the rejection is idempotent and the
  resume continues from the same evidence.
- No review policy, quota, gate, isolation or billing rule changes. The note is
  corrective text for the next attempt only (ADR-0016 §6).

## 4. Alternatives rejected

- **Mark the job `FAILED` first and reuse `resume_job` unchanged.** It would
  record a failure that did not happen and would let an ordinary failure-resume
  path start from a rejection decision without naming it. The narrow
  `SUCCEEDED` guard keeps the reason explicit in the transition.
- **Start from scratch on rejection.** It discards the saved candidate the owner
  is correcting and contradicts the H7 contract that the rejection note is the
  resume note for the continued work.
- **Record the rejection without resuming.** That is the pre-H50 state: the note
  blocks acceptance and no worker ever receives it.
