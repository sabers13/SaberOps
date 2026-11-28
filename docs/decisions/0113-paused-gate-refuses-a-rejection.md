# ADR-0113: A paused gate refuses a rejection before recording it

- **Status:** Accepted. Implemented in phase 1.1 lane H55.
- **Date:** 2026-11-28
- **Decider:** Saber (owner), phase 1.1 plan lane H55, from the H37 recheck's
  N4 on 2026-11-28 ("start those four": N1, N2, N4 and N7).
- **Related:** [ADR-0108](0108-owner-reject-with-a-note.md) (the rejection flow
  this amends), [ADR-0045](0045-owner-confirmed-resume.md) (only `orch resume`
  clears a pause), [ADR-0016](0016-steering-note-channel.md) (steering notes), the H37
  recheck report §9 N4 and §6 D5.

## 1. Context

`reject_candidate` and `orch reject` call `OrchestratorService.reject_job`. That
method recorded the note, wrote the rejection and then called `resume_job`,
which starts work and is therefore gated by `evaluate_gate`. When the work gate
was closed with a sticky pause, the write half had already landed: the call
returned `RESUME_PAUSED: Resume is paused: paused: no planning session`, the job
stayed `SUCCEEDED`, the candidate became `REJECTED`, the note was pending and no
successor existed. A retry after `orch resume` worked, but until then the owner
had a rejection with no correction, which is precisely the state ADR-0108
promised never to create.

Reproduced on the H55 branch in the lifecycle rig with the gate paused:
`tests/test_h55_reject_while_paused.py` failed with that same `RESUME_PAUSED`
message before the change.

## 2. Decision

1. **The gate is checked before the first write.** `reject_job` refuses with
   `REJECT_PAUSED` when the gate is closed and not transient, before it records
   a note, a rejection or a successor. Nothing is recorded: the note is not
   stored, the candidate keeps its state, the job stays `SUCCEEDED` and its
   current run is unchanged. The owner or planner rejects again after
   `orch resume`.
2. **The transient gate keeps its existing meaning.** A store that never had a
   live session reports a closed but transient gate; `resume_job` lets that
   through so a fresh store can be retried, and the refusal uses the same test
   (`closed and not transient`), so a rejection behaves exactly like the resume
   it would start.
3. **The refusal names what happened and what the owner does next.** The message
   carries the gate's own label (`work is paused: no planning session`,
   `paused by the owner`) and says that nothing was recorded and that the owner
   must run `orch resume` before rejecting again. The MCP guide's rejection
   section says the same to the planner.
4. **No queued correction is added.** A pause means no work starts; a rejection
   parked to auto-start later would be a new kind of deferred work with its own
   resume ordering, owner notice and test surface. Refusing cleanly keeps the
   one consistent rule: starting a correction is starting work, and work does not
   start while the gate is closed.

## 3. Consequences

- A paused gate can never leave a `REJECTED` candidate with a pending note and
  no successor; the failing outcome is a refusal that changed nothing.
- The planner's reply after a pause is a refusal it can explain to the owner,
  with the owner's exact next command. Once the owner resumes, the same call
  records the rejection and starts the corrected attempt with the note, as
  ADR-0108 specifies.
- A crash between the gate check and the successor launch can still leave a
  rejected candidate and a pending note, as ADR-0108 §3 records; that unchanged
  window needs a crash, not a deliberate pause, and a repeated call resumes it.
- `reject_current_candidate` on its own (the service seam the retired web reject
  route used) still records only the rejection; it never starts a correction and
  is out of this lane.

## 4. Alternatives rejected

- **Record the rejection with a queued correction that starts on resume.** It
  would need a durable pending-correction record, a scheduler or resume hook to
  start it, and a way to tell the owner a correction began that they did not
  request at that moment. The pause would stop meaning "no work starts".
- **Record the rejection, then roll it back when the resume is refused.** The
  note and the rejection are separate durable writes with their own events;
  rolling them back would add inverse writes to the rejection authority and
  could still fail between them.
- **Gate only the resume and keep the writes (status quo).** That is the N4
  defect: a rejection without a correction.
