# ADR-0028: Away mode applies owner standing decisions and keeps a durable audit

- **Status:** Accepted. Implemented in Phase 1 lane F11 (Wave 3).
- **Date:** 2026-11-19
- **Decider:** Saber (owner), Phase 1 plan lane F11
- **Related:** [ADR-0011](0011-bounded-automatic-actions.md) (no silent work),
  [ADR-0020](0020-owner-mode.md) (one owner mode that only narrows)

## 1. Context

`saberops.unattended` encoded the whole overnight policy — exact-scope standing
decisions, the immutable never-delegable list, the deterministic preflight, the
three-consecutive-failure circuit breaker, the plan-impact projection and the
morning report — but nothing in production called it.  The questions page showed
standing permissions it never consulted, and away mode from ADR-0020 could
narrow authority without ever applying an owner's pre-authored answer.

## 2. Decision

1. **Away is the only mode where the unattended policy acts.** `ask-me` and
   `critical-only` keep their own question paths.  The scheduler reads the
   effective owner mode once per tick; a corrupt mode document fails the whole
   tick closed (`owner_mode_unreadable`) instead of silently treating away as
   attended.
2. **Standing decisions are owner-authored, exact and bounded.** They live in
   `unattended-decisions.json` beside the authority config, are written only by
   `orch away grant` / `orch away revoke` (no MCP writer exists), and are
   validated by `StandingDecision`/`DecisionSheet`: exact
   `(project, task, paths)` + decision-key match, no prefix, analogy, glob or
   wildcard, at least one expiry bound, and `answered_by: owner`.  An expired
   decision is never applied.
3. **The away policy is explicit, and away fails closed without it.** The owner
   records the preflight facts with `orch away policy set` (budget units, wall
   clock, worker cap, authorized paths).  Without a policy the existing
   `run_preflight` sees every limit unset and refuses, so away dispatch parks
   instead of inventing a safe-looking default.  F17 builds the end time, the
   usage cap and the pause switch on this same surface.
4. **Dispatch order is breaker, preflight, then questions.** For every queued
   job the scheduler checks the durable circuit breaker, runs the deterministic
   preflight, and resolves every open question that blocks that job:
   - a live exact standing decision answers the question with the owner's
     **verbatim** words (recorded with owner authority because the words are
     the owner's own), and the job may dispatch;
   - a never-delegable kind or any unhandled decision parks **only that job**
     (`AWAY_PARKED`) and records the park; independent jobs keep dispatching.
   The lane does not activate the autonomous accept class (F15).
5. **Every disposition is durable before the next step.** The away journal is
   append-only and `fsync`ed on each record (`away-journal.jsonl`).  It holds
   resolutions with their morning-report fragment, preflight results, plan
   impact and per-project outcomes.  The return summary is rebuilt from those
   records, never from process memory, and is reused by `catch_up` and the
   desktop notifier (one notice per journal revision).  A scheduler killed after
   a decision resumes with the same audit.
6. **The circuit breaker is terminal and loud.** Three consecutive failed tasks
   in one project open the breaker; the scheduler parks further dispatch with a
   readable reason, writes a `halt` record and sends one notice.  Only the owner
   clears it with `orch away reset-breaker`.
7. **Nothing here can push or move the target branch.** The summary reports
   local-only work and the never-delegable list still parks publishing.

## 3. Consequences

- An away run has an auditable answer for "why did this start or park?" from
  durable records, in the CLI (`orch away show|summary`), in `catch_up` and in
  the journal itself.
- Away cannot quietly drain usage without owner-authored limits: no policy,
  exhausted budget, passed wall clock or full worker cap all park instead of
  dispatching.
- Parked work does not stall independent work, and a parked task keeps its
  question open for the owner.
- Auto-accept (F15) and close-the-CLI pausing (F17) remain separate lanes; this
  record does not widen acceptance or resume anything silently.
