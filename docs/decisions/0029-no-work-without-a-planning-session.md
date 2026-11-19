# ADR-0029: No work runs without a connected planning session

- **Status:** Accepted. Implemented in Phase 1 lane F17 (Wave 3).
- **Date:** 2026-11-19
- **Decider:** Saber (owner), Phase 1 plan lane F17
- **Related:** [ADR-0020](0020-owner-mode.md) (owner mode),
  [ADR-0027](0027-critical-only-questions.md) (critical-only rotation),
  [ADR-0028](0028-away-mode-standing-decisions.md) (away standing decisions),
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

## 2. Decision

1. **One durable work gate, one answer.** `runtime-gate.json` beside the owner
   state holds one sticky pause record (reason, detail, `since`, by, paused
   jobs) and the list of runs the engine stopped. `runtime_gate.evaluate_gate`
   is the only function that answers "may work start". It is closed when *any*
   of these holds:
   - the owner paused (`orch pause`, the Questions-page toggle);
   - no connected planning session, **once presence has been established**
     (the MCP stdio server took the planning lease at least once);
   - an `away` session ran past its `until` or spent its job budget;
   - an `away` session has no end time and no job budget at all;
   - the gate document or the owner-mode document cannot be read (fail closed).
2. **Presence is the planning lease plus a heartbeat.** The MCP stdio server
   writes `heartbeat_at` on the planning lease every few seconds while the
   session lives, so a long tool call or an idle chat is not mistaken for a
   disconnect. Presence is absent when the lease is missing, its pid is dead
   (the CLI was killed), or the heartbeat is older than the timeout (the CLI
   crashed or lost its connection). A clean CLI close releases the lease, and
   the scheduler notices on its next tick. Closing therefore pauses new
   dispatch within one bounded tick in every mode.
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
7. **Stores without a planning session keep their old behaviour.** Presence is
   enforced only after the first MCP planning session registers. A store driven
   solely by direct CLI commands is not retroactively gated; once the owner uses
   a planning CLI, the rule applies from then on. This keeps direct CLI
   scheduling usable without weakening the rule the owner asked for.
8. **While away or critical-only, a digest is sent periodically** (default
   hourly) listing running jobs, the subscriptions in use and the usage
   recorded so far, so unattended spend is visible without opening a session.

## 3. Consequences

- Closing the planning CLI is now the safe action the owner wanted: no new
  jobs, lanes, retries, repairs or refreshes, and every worker stops with its
  work preserved. The only way back is an explicit confirmation.
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
  bypassed here. Acceptance stays owner-only; this lane only stops spending.
