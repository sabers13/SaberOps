# ADR-0078: One self-answer record; divergent records are deleted

- **Status:** Accepted. Phase 1.1 lane H27.
- **Date:** 2026-11-23
- **Decider:** Saber (owner), recorded answer 2026-11-22 ("do your suggestion
  and maximise the deletion"; plan row "Decision records") and the phase 1.1
  plan lane H27
- **Related:** [ADR-0027](0027-critical-only-questions.md) (self-answered
  critical-only questions), [ADR-0011](0011-bounded-automatic-actions.md) (no
  silent work), `docs/questions.md`, `docs/unattended-plans.md`

## 1. Context

Self-answered decisions were kept twice. The question ledger's audit list
(`OwnerQuestionQueue.orchestrator_alone_decisions()`) held every planning-session
answer with its evidence and rejected alternative. The away journal separately
built an `AutonomousAuditEntry` list from Class-1 and Class-2 resolutions and the
return summary showed that list as `audit_entries`. Both described the same fact
in two vocabularies, and neither said what a wrong answer would cost.

The queue also kept a third record of a conflict: a later answer, a moot close
after an answer, or an answer after a moot close was stored as a
`DivergentRecord` and reported by `orch questions divergences` and the web API.
The primary answer was already never overwritten, so the divergent record added
a second status-like trail without changing any outcome.

## 2. Decision

1. **The question audit list is the one self-answer record.** `OwnerAnswer` gains
   an optional `cost_if_wrong`; `orchestrator_alone_decisions()`,
   `question_view`, the CLI, `answer_question` over MCP and `catch_up` carry it.
   It is optional and a self-answer with no cost stays valid and unchanged.
2. **The return summary reads that record.** `away.build_return_summary()` adds
   `ReturnSummary.self_answered` from
   `orchestrator_alone_decisions()`. `ReturnSummary.one_line()` reports the
   count as "self-answered". An unreadable question store raises `AwayError`
   rather than silently reporting no self-answers.
3. **The away journal's duplicate audit list is deleted.**
   `AutonomousAuditEntry`, `build_audit_entries`, the `audit_entries` field of
   `MorningReport` and `ReturnSummary`, its builder branch and its public
   exports are removed. Journal resolution records stay: they still feed the
   budget window, standing-decision actions, accepted candidates, parked
   branches, escalations and stall alarms.
4. **Divergent records are deleted.** `DivergentRecord`, `DivergenceReason`,
   `DivergenceReport`, `OwnerQuestionQueue.record_divergence()`,
   `OwnerQuestionQueue.divergences()`, `divergence_count`, the CLI verbs
   `questions diverge` and `questions divergences`, the web routes
   `GET /divergences` and `POST /divergence`, and the list key `divergences` are
   removed. A later answer that conflicts with the recorded outcome, an answer
   after a moot close, a moot close after an answer and a second moot close on
   different evidence are refused with `QuestionStateError`; the first answer
   and the status stand. A repeated identical answer is still recorded as a
   confirmation, so the idempotency contract is unchanged.
5. **Everything else in the queue stays.** Ask, answer, defer, moot, the
   five-part escalation, the derived authority and critical tag, the
   never-closed-by-lifecycle invariant and the standing-rule proposals are
   unchanged.

Changing an owner decision was already, and remains, a new keyed question that
`supersedes` the old one. This ADR only removes the second record of the
conflict; it does not add a way to overwrite an answer.

## 3. Consequences

- One durable record answers "what did the planner decide alone, and what could
  a wrong call cost": the question ledger. The return summary, `catch_up`, the
  CLI `questions audit` and the Questions page read the same list.
- A conflicting later answer is rejected at the boundary instead of accumulating
  a report. A caller that wants a different outcome must raise a superseding
  question, which is the path ADR-0027 already assumed.
- Class-2 `TAKE_RECOMMENDED_OPTION` resolutions no longer appear in a separate
  audit section; their chosen option, rejected alternatives and evidence remain
  in the journal's resolution record, and the disposition itself is visible
  through the morning report's other sections.
- The away journal keeps exactly the sections that are not question answers, so
  budget accounting, the circuit breaker and every never-delegable park are
  unchanged.
- Measured deletion and the caller census are in
  `/home/saber/fm-test/firstmate/data/so-h27/notes.md`.
