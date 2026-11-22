# ADR-0053: A review PASS must answer every acceptance clause

- **Status:** Accepted. Phase 1 N14/N15 fix lane.
- **Date:** 2026-11-22
- **Decider:** Saber (owner), standing order for the confirming real-run fixes
- **Related:** [ADR-0008](0008-candidate-bound-acceptance.md) (candidate-bound
  review evidence), [ADR-0011](0011-bounded-automatic-actions.md) (bounded
  automatic repair)

## 1. Context

The confirming real run (`/var/tmp/so-confirm/report.md`, N14) recorded a
reviewer that approved a change contradicting the task's acceptance verbatim.
`run_01df15906364` (`review-block-b`) asked for the empty-name check to be
removed while its acceptance said `greet('')` must still raise `ValueError`.
The worker said exactly that in its report, and the Claude reviewer answered
`PASS` ("Code changes approved by reviewer"). The review prompt embedded the
task text but never isolated the acceptance clauses or required an answer to
each one, so a reviewer could treat the goal as authoritative and ignore the
contradicted clause.

The same run exposed the mirror problem in the repair loop (N15).
`run_1617597fd2c5` (`rename`) had a complete candidate. The reviewer blocked
because the diff did not establish the repository-wide acceptance condition.
The repair worker exited successfully without editing anything and reported
that the finding was already satisfied. The engine counted that as a failed
attempt and ended `CONVERGENCE_STALLED`, losing a finished task.

## 2. Decision

1. **Acceptance clauses are quoted into the review prompt.** When the task
   declares an `Acceptance:` section, the review prompt lists each clause with
   a stable `CLAUSE <n>:` number and requires one explicit
   `CLAUSE <n>: PASS|FAIL — evidence` answer per clause, verified against the
   provided diff. An unverifiable clause is `FAIL`.
2. **A PASS is deterministically gated on clause coverage.** The engine
   accepts `PASS` / `PASS_WITH_BACKLOG` only when every clause has exactly a
   `PASS` answer. Missing answers, an explicit `FAIL`, or contradictory
   answers for one clause map the verdict to `BLOCK` (or `FINAL_BLOCK` on the
   final review) and record `review_acceptance_clause_block`. This is computed
   from the reviewer's output, never inferred from prose.
3. **Tasks without an acceptance section keep the historical contract.** No
   clauses, no new requirement.
4. **The benchmark rig checks the must-fail rows deterministically.** For
   `review-block-a` / `review-block-b` the rig probes the candidate commit's
   `app.py`: a reviewer block on a candidate whose acceptance still holds is
   not evidence of a working must-fail guard, so the row fails. **Amended by
   [ADR-0056](0056-benchmark-must-fail-seeded-review.md):** real mode now seeds
   the violating candidate and drives only the reviewer, and a row with no
   violation to catch is *not measured* instead of failing the benchmark.
5. **A no-diff repair that claims the finding is satisfied gets one bounded
   re-review.** The inherited candidate is reviewed again with the repair
   report embedded as non-authoritative evidence. The re-review is allowed
   once; a second `BLOCK` remains terminal (`CONVERGENCE_STALLED`).

## 3. Consequences

- A reviewer can no longer approve a change that violates a stated clause
  without the engine failing closed, even if the reviewer prose argues the
  goal was met.
- Review output for tasks with acceptance clauses must carry the per-clause
  answers. The fake test reviewer emits a compliant answer; recorded or
  scripted reviewers that do not are blocked.
- A correct candidate is no longer thrown away because its repair needed no
  edit, but the extra chance is bounded and still ends `FAILED` if the
  reviewer blocks again.
- No authority is widened: the clause gate can only turn an approval into a
  block, never the reverse, and the re-review consumes the existing bounded
  review budget.
