# ADR-0034: Advisor checkpoints consult a stronger model without letting it build

- **Status:** Accepted. Implemented in Phase 1 lane F26 (Wave 3).
- **Date:** 2026-11-19
- **Decider:** Saber (owner), after reviewing a "build cheap, advise strong, scout
  cheapest" workflow: "yes add it".
- **Related:** [ADR-0016](0016-steering-note-channel.md) (the delivery channel),
  [ADR-0008](0008-candidate-bound-acceptance.md) (independent review and
  owner-only acceptance), [ADR-0029](0029-no-work-without-a-planning-session.md)
  (the work gate) and [ADR-0026](0026-scout-jobs.md) (read-only work).

## 1. Context

A strong model that reads an attempt and says what is wrong costs far less than
building with it, and it catches a wrong plan or a repeated failure earlier than
a cheap worker's retries do. The risk is that an "advisor" quietly becomes a
second builder: another process that edits files, produces commits, spends
conversation with the worker, or becomes the reviewer of its own advice.

## 2. Decision

1. **Opt-in per project.** An advisor route is off unless the owner sets one:
   `orch project advisor set <provider> <model> [--calls N]` writes
   `<project state>/advisor.json`; `off` removes it. The route names one
   provider/model and a per-job call budget (default 3, maximum 20).
2. **Plan subscriptions only.** The provider must be one the CLI bills against
   the owner's existing plan (`codex`, `opencode`, `anthropic`, `claude`), must
   not be disabled by the owner's provider policy, and must not be a provider
   the owner enabled for API-key billing. An unavailable route is recorded and
   the job continues without advice; it is never a reason to stop the job.
3. **Read-only by construction.** The advisor runs in a disposable detached
   worktree of the run's base, not in the run's worktree, always with
   `read_only=True`, and the scratch worktree is removed afterwards. Whatever
   the CLI may have written is discarded with the worktree, so an advisor call
   can never produce a candidate commit or move a ref.
4. **Three checkpoints, each once.**
   - `before_plan`: before the first worker attempt of the run.
   - `repeat_failure`: when the two most recent attempts failed identically
     (same terminal status, interrupting reason and bounded error text). One
     call per failure signature, never one per retry.
   - `before_review`: after the gate passed and before independent review is
     dispatched, only when review is required.
   Routine retries produce no call.
5. **Budget and availability are enforced before launch.** Every call is
   counted durably on the run; once the budget is spent no further call is
   made. A closed work gate (owner pause, no planning session, away budget)
   refuses the call and the run continues.
6. **Advice is a steering note.** Advice is bounded text recorded on the run and
   attached to the owning job as a steering note with `source=advisor`
   (ADR-0016). It reaches a worker only inside the next attempt's prompt, is
   delivered once, and stays visibly pending when no further attempt starts.
   `get_job`, `tail_job`, `catch_up` and the return summary read the same
   projection.
7. **Usage is recorded.** Each call is persisted as a `DispatchStage.ADVISOR`
   model-call row, so per-job usage (F2) includes advisor calls on the advisor
   route next to worker and reviewer calls.
8. **The advisor is not the reviewer.** Independent review stays the existing
   different-vendor check on the exact candidate, and acceptance stays
   owner-only.

## 3. Consequences

- The owner can keep a cheap build route and still get a stronger model's read
  at the moments that matter, with a hard cost ceiling per job.
- Advisor advice can never widen authority: it cannot change the task, the
  route, billing consent, the gate result or the review verdict, and it cannot
  approve anything.
- A project without an advisor route behaves exactly as before; the feature
  adds no events, notes, calls or usage rows when it is off.
- Losing the advisor (unavailable CLI, paused store, spent budget) never stalls
  work; the refusal is visible in the job record instead.
