# ADR-0034: The advisor is an on-demand read-only call, never a builder

- **Status:** Accepted. Implemented in Phase 1 lane F26 (Wave 3) and amended
  in phase 1.1 lane H5 (2026-11-23).
- **Date:** 2026-11-19; amended 2026-11-23.
- **Decider:** Saber (owner), after reviewing a "build cheap, advise strong,
  scout cheapest" workflow: "yes add it". Phase 1.1 owner words: "Advisor
  checkpoints make it optional exactly like claude code".
- **Related:** [ADR-0016](0016-steering-note-channel.md) (owner steering notes),
  [ADR-0008](0008-candidate-bound-acceptance.md) (independent review and
  owner-only acceptance), [ADR-0029](0029-no-work-without-a-planning-session.md)
  (the work gate) and [ADR-0026](0026-scout-jobs.md) (read-only work).

## 1. Context

A strong model that reads an attempt and says what is wrong costs far less than
building with it, and it catches a wrong plan or a repeated failure earlier than
a cheap worker's retries do. The risk is that an "advisor" quietly becomes a
second builder: another process that edits files, produces commits, spends
conversation with the worker, or becomes the reviewer of its own advice.

Phase 1 first implemented the advisor at three fixed checkpoints: before the
first attempt, after the same failure twice, and before review. The owner then
asked for an advisor that is optional in the Claude-Code sense: available when
the worker or the planner wants it, not on a fixed schedule. H0's census of the
B3 evidence (2026-11-23, `data/so-h0/report.md` §6) found the advisor route off
for every B3 project, zero checkpoint firings, and only one run that even met
the repeat-failure precondition. Q5 therefore settled the question: replace the
fixed checkpoints with an on-demand call and **remove the automatic
"same failure twice" trigger** rather than keep a special case with no positive
evidence.

## 2. Decision

1. **Opt-in per project.** An advisor route is off unless the owner sets one:
   `orch project advisor set <provider> <model> [--calls N]` writes
   `<project state>/advisor.json`; `off` removes it. The route names one
   provider/model and a per-job call budget (default 3, maximum 20).
2. **Plan subscriptions only.** The provider must be one the CLI bills against
   the owner's existing plan (`codex`, `opencode`, `anthropic`, `claude`), must
   not be disabled by the owner's provider policy, and must not be a provider
   the owner enabled for API-key billing. A refused route is recorded and the
   work continues without advice; it is never a reason to stop a job.
3. **Read-only by construction.** The advisor runs in a disposable detached
   worktree of the run's base, not in the run's worktree, always with
   `read_only=True`, and the scratch worktree is removed afterwards. Whatever
   the CLI may have written is discarded with the worktree, so an advisor call
   can never produce a candidate commit or move a ref.
4. **On demand, from the work itself.**
   - A headless worker runs `orch advisor ask --question "..."` from its own
     worktree. The engine already passes `ORCH_RUN_ID`, `ORCH_ATTEMPT_ID` and
     `ORCH_DB_PATH`; the command refuses unless the caller's working directory
     is inside that attempt's worktree, so a worker can only ask about its own
     run.
   - A native worker or the planner asks over MCP with `ask_advisor`
     (`job_id`, `question`). Native MCP registration is unchanged.
   - While the route is on, the worker prompt names the exact command and the
     remaining budget. With the route off the prompt has no advisor section.
5. **Off means no tool.** `ask_advisor` is registered on the MCP server only
   when the session's project has an advisor route. The worker command refuses
   with "the advisor is off" and no advisor event is written.
6. **Budget and availability are enforced before launch.** Every call is
   counted durably across the job's runs; once the budget is spent, the next
   call is refused with the used and allowed counts. A closed work gate (owner
   pause, no planning session, away budget) refuses the call and the run
   continues.
7. **Advice is returned to the asker and recorded on the run.** The call
   records `advisor_call_started`, the question and the bounded advice on the
   run, and the answer is printed to the worker (or returned in the MCP
   result). Advice is no longer converted into a steering note: the asker has
   it immediately, and a planner that wants it applied to a running job uses
   the ordinary owner/planner note channel. Advice recorded before phase 1.1
   keeps its `source=advisor` note label. `get_job`, `tail_job`, `catch_up` and
   the return summary read the same projection.
8. **Usage is recorded.** Each call is persisted as a `DispatchStage.ADVISOR`
   model-call row, so per-job usage (F2) includes advisor calls on the advisor
   route next to worker and reviewer calls.
9. **The advisor is not the reviewer.** Independent review stays the existing
   different-vendor check on the exact candidate, and acceptance stays
   owner-only.

### Removed by this amendment

- The three fixed checkpoints (`before_plan`, `repeat_failure`,
  `before_review`) and their per-run "fire once" bookkeeping.
- The automatic "same failure twice" trigger and its failure-signature
  matching (Q5; H0 showed no useful firing in B3).
- Advisor advice as an automatically recorded steering note.

## 3. Consequences

- The owner can keep a cheap build route and still get a stronger model's read
  when the worker or planner identifies a question, with a hard cost ceiling
  per job.
- Advisor advice can never widen authority: it cannot change the task, the
  route, billing consent, the gate result or the review verdict, and it cannot
  approve anything.
- A project without an advisor route behaves exactly as before: no tool, no
  prompt section, no events, notes, calls or usage rows.
- Losing the advisor (off, unavailable CLI, paused store, spent budget) never
  stalls work; the refusal is visible to the asker and in the job record.
- An advisor call is synchronous with the worker's current attempt. A headless
  worker that asks blocks its own turn for the bounded advisor timeout; that is
  the price of advice at the moment it is needed.
