# ADR-0029: No work runs without a connected planning session

- **Status:** Accepted. Implemented in Phase 1 lanes F17 and F24 (Wave 3).
- **Date:** 2026-11-19
- **Decider:** Saber (owner), Phase 1 plan lanes F17 and F24
- **Related:** [ADR-0020](0020-owner-mode.md) (owner mode),
  [ADR-0027](0027-critical-only-questions.md) (critical-only rotation),
  [ADR-0028](0028-away-mode-standing-decisions.md) (away standing decisions),
  [ADR-0025](0025-read-only-tmux-watch.md) (read-only watch view),
  [ADR-0015](0015-plan-files-and-rendered-plan-state.md) (plan-lane pause seam),
  [ADR-0002](0002-routing-admission-and-quota.md) (quota preemption/resume),
  [ADR-0011](0011-bounded-automatic-actions.md) (no silent work)

## 1. Context

The owner's rule, given twice on 2026-11-19:

> "I also don't want a scenario that I close a CLI and some project runs without
> anyone know and drain my usage."
>
> "I want closing CLI pause the work no matter what, this would be a serious
> leak if not controlled."

Before this lane, the scheduler was a detached process with no notion of who was
watching. `away` had no end bound, plan lanes had their own pause seam but the
rest of the engine did not, and an owner pause existed only as
`orch scheduler stop`, which leaves workers running. The engine could therefore
spend subscription usage with the planning CLI closed, which is exactly the
leak the owner forbade -- and it would also contradict the mode promise, because
`away` is supposed to mean "the owner is away from the keyboard while the
planning CLI stays open".

F17 then armed its pause the first time an MCP planning session took the
planning lease.  A store driven only by plain `orch` commands in a terminal was
never paused, so the guarantee still had the leak the owner named.  F24 closes
it: presence is required from the start for every store.  With no AI CLI
planning session connected, SaberOps never starts work, whether or not a
session was ever seen.  Plain terminal use counts only while an *attended*
terminal session is open: `orch watch` (F23) while a terminal is attached, or
`orch run --attended` while that command runs.

## 2. Decision

1. **One durable work gate, one answer.** `runtime-gate.json` beside the owner
   state holds one sticky pause record (reason, detail, `since`, by, paused
   jobs) and the list of runs the engine stopped. `runtime_gate.evaluate_gate`
   is the only function that answers "may work start". It is closed when *any*
   of these holds:
   - the owner paused (`orch pause`, the Questions-page toggle);
   - no connected planning session and no attended terminal (F24);
   - an `away` session ran past its `until` or spent its job budget;
   - an `away` session has no end time and no job budget at all;
   - the gate document or the owner-mode document cannot be read (fail closed).
2. **Presence is a heartbeat from one of two sources.** An AI CLI planning
   session writes `heartbeat_at` on the planning lease every few seconds while
   it lives; an attended terminal writes an `attendance/<session>.json` record
   and refreshes its `heartbeat_at` every few seconds (F24).  Presence is absent
   when every source is missing, its pid is dead (the CLI or terminal was
   killed), or each heartbeat is older than the shared timeout (the CLI
   crashed, lost its connection, or the terminal closed).  A clean CLI close
   releases the lease and a closed terminal removes its record; the scheduler
   notices on its next tick. Closing therefore pauses new dispatch within one
   bounded tick in every mode.
3. **A pause is sticky and never resumes silently.** The returning session sees
   the pause in `catch_up` and `get_pause`; work continues only after one
   explicit confirmation: `orch resume` (owner) or the mutating MCP
   `resume_work` (the planning session). An absent session that comes back is
   not a confirmation.
4. **Running workers are stopped at a safe point, never discarded.** A closed
   gate parks every RUNNING job through the existing fenced stop plus the D8
   checkpoint/anchor/capsule protocol: the worker tree and execution owner are
   terminated with the same identity fences as cancellation, the run ends
   `PREEMPTED`, and the job returns to `QUEUED` with `waiting_reason=PAUSED`
   and a verified `resume_from`. The worktree, the WIP checkpoint, the handoff
   capsule and every attempt stay on disk. A pause does **not** consume the
   bounded quota-preemption budget; a resumed job is re-dispatched through the
   ordinary scheduler and restored through `prepare_resume`.
5. **Every start path is gated.** Scheduler dispatch, plan-lane submission
   (the P2 seam), autonomous retry, repair and escalation, and candidate
   refresh all refuse while the gate is closed. Submitting a job is still
   allowed -- it only queues -- because a returning session may submit and then
   confirm a resume.
6. **Away is bounded.** An `away` mode selection must carry `until`, a
   `max_jobs` budget, or both; `orch mode set away` refuses otherwise. Reaching
   either bound pauses exactly like a lost session and sends one notice. This is
   the *session-level* bound: [ADR-0028](0028-away-mode-standing-decisions.md)
   adds the per-job away preflight (budget units, wall clock, worker cap,
   authorized paths), which stays the dispatch-time check inside the same
   ``away`` session. A pre-existing `away` document with no session bound loads
   but never starts work (`away_unbounded`), so an upgrade cannot silently run
   unbounded.
7. **Presence is required from the start for every store.** A fresh store, or
   one that has only ever been driven by plain `orch` commands, is closed until
   a planning session or an attended terminal is live (F24).  A store that has
   never had a live session is closed **without** a sticky pause: nothing was
   interrupted, so work queued by `orch submit` starts as soon as an attended
   session opens.  The first live session is recorded durably; losing it after
   that is the sticky F17 pause described above.  F17 documents written with
   `presence_known: false` are migrated to armed on the next evaluation, with
   one notice and one `work_gate_armed` ledger event.  `orch run --attended`
   holds presence for its own run and refuses while an explicit pause is open;
   a plain `orch run` refuses to start without a live session, because its
   detached execution would keep spending after the terminal closes.  Use
   `--attended`, open a session, or `orch submit` and let the scheduler
   dispatch.
8. **While away or critical-only, a digest is sent periodically** (default
   hourly) listing running jobs, the subscriptions in use and the usage
   recorded so far, so unattended spend is visible without opening a session.

## 3. Consequences

- Closing the planning CLI, or the attended terminal, is now the safe action the
  owner wanted: no new jobs, lanes, retries, repairs or refreshes, and every
  worker stops with its work preserved. The only way back is an explicit
  confirmation once presence has been seen.
- A fresh install cannot spend usage before the owner connects a CLI: the
  scheduler runs but starts nothing, and `orch submit` only queues.
- `away` cannot be an unattended background mode; it is an attended-but-distracted
  mode with a hard bound. This is the owner's chosen trade-off.
- The pause record is one small document and one hash-chained ledger event per
  pause; notices are keyed by that pause, so a long pause does not spam.
- `RunStatus.PREEMPTED` now also means "paused", which is why the pause uses
  `WaitingReason.PAUSED` (not `PREEMPTED`) on the job and why the preemption
  budget is untouched. The durable D8 receipt and checkpoint verification are
  unchanged, so a crash at any point in a pause still converges on recovery.
- A planning lease left behind by a dead session no longer blocks takeover once
  its jobs are parked: `active_work` counts executing work, not parked work.
- No provider, model, billing-posture or acceptance decision is made or
  bypassed here. This lane only stops spending; F15's local auto-accept is a
  separate, mode-scoped stage (ADR-0008 §4) and is paused like all other work.
