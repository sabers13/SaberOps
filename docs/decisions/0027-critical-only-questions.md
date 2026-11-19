# ADR-0027: Critical-only question delegation

- **Status:** Accepted. Implemented in Phase 1 lane F10 (Wave 3).
- **Date:** 2026-11-19
- **Decider:** Saber (owner), Phase 1 plan lane F10
- **Related:** [ADR-0004](0004-mcp-authority-and-evidence.md) (D6 question
  authority), [ADR-0011](0011-bounded-automatic-actions.md) (no silent work),
  [ADR-0020](0020-owner-mode.md) (one owner mode)

## 1. Context

The owner asked for a mode in which the orchestrator answers routine questions
itself and asks only what it judges critical. The question queue already derived
answer authority from the decision class (ADR-0004 D6) and stored an
orchestrator-alone answer with evidence and a rejected alternative, but nothing
marked a question as critical, and `answer_question` accepted a planning-session
answer in every mode. The owner-only split covered the most dangerous classes,
yet a routine class could still be escalated on judgment and had no durable tag.

## 2. Decision

1. **Every question carries a derived `critical` tag.** It is the union of: an
   owner-only decision class; the acceptance class (acceptance has no delegable
   form); a never-delegable question kind (the ADR-0004 D14 list: test gate,
   data/training policy, roadmap scope, budget cap, credentials, destructive
   operations, publishing); or an explicit orchestrator escalation. Orchestrator
   judgment is additive only: no field, caller or mode can remove a reason. The
   tag and its reasons appear in `list_questions`, `catch_up`, the CLI and the
   Questions page.
2. **The effective mode decides who may self-answer.** `ask-me` leaves every
   question to the owner. With a live planning session, `critical-only` and
   `away` let that session answer a routine (`critical: false`) question through
   `answer_question` with PLAN authority, carrying evidence and a rejected
   alternative. A critical question is refused to the planning session in every
   mode; it notifies the owner and waits.
3. **No planner means no self-answer.** With no live planning session, a routine
   question waits; the engine never starts a background planner and the question
   stays durable. The close-the-CLI work pause itself belongs to the separate
   F17 lane ([ADR-0029](0029-no-work-without-a-planning-session.md)), not a
   second mechanism here.
4. **Every self-answer is recorded and shown.** The existing durable answer
   record holds the words, class, evidence, rejected alternative, actor and
   time. `catch_up` returns the job's self-answered decisions, and
   `question_return_summary` is the reusable projection for the return summary.
   Critical questions produce one critical notice through the existing notifier.

## 3. Consequences

- An owner-only class is never made answerable by a mode, and acceptance stays
  owner-only. The tag can only add criticality, so a planner cannot talk its way
  into an owner decision.
- `ask-me` keeps the manual answer path: the owner answers through the CLI or the
  local web surface, never through the planner.
- A routine answer is visible evidence, not a silent decision: the return
  summary and `catch_up` list the words, evidence and rejected alternative.
- Auto-accept, standing decisions and the work pause remain separate lanes
  (F15, F11 and F17) and build on the same mode store and question records.
