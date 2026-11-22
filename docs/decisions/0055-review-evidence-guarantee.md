# ADR-0055: Reviews receive engine-verified evidence; no-diff repairs are structural

- **Status:** Accepted. Phase 1 review-evidence and N15 fix lane.
- **Date:** 2026-11-22
- **Decider:** Saber (owner), standing order for the confirming real-run fixes
- **Related:** [ADR-0053](0053-acceptance-clause-review-gate.md) (per-clause
  reviews and the bounded no-diff re-review), [ADR-0008](0008-candidate-bound-acceptance.md)
  (candidate-bound review evidence)

## 1. Context

The confirming run (`/home/saber/fm-test/firstmate/data/so-confirm2/report.md`,
N14/N15) showed the clause gate working on real violations while rejecting
honest work. Five accepted-task candidates were blocked because the acceptance
protocol said the diff and task text were the only evidence.

- `fix-failing-test` (three CLIs): the candidate changed only `test_app.py`;
  the reviewer could not see that `app.py` returns `"hello Ada"`.
- `rename` (two CLIs): the candidate renamed every reference, but the reviewer
  could not run a repository-wide search and treated the absence as unproven.

The same run showed N15 still broken. The no-diff re-review was triggered by a
hand-written phrase list, so six ordinary repair reports never reached it. The
two that did were re-blocked because the repair report was labelled "a claim,
not proof" while the protocol demanded proof the diff could not contain. A
final review also answered `CLAUSE 1: PASS` and still returned `FINAL_BLOCK`
without the engine saying which outcome governed.

## 2. Decision

1. **The engine supplies trusted evidence for clause reviews.** When a task
   declares acceptance clauses, the review prompt carries an
   engine-verified block built from the candidate commit: the full content of
   every touched file, the in-repository modules those files import, the
   recorded gate/test output, and - for a repository-wide clause - the engine's
   own `git grep` result against the candidate tree. The block is bounded and
   is never assembled from worker or reviewer prose.
2. **The protocol distinguishes evidence classes.** A reviewer verifies clauses
   against the diff and the engine-verified evidence. The diff shows what
   changed; engine-produced file content, gate/test output and searches are
   trusted. Repair reports and other prose remain claims. "A clause you cannot
   verify from the provided material is FAIL" is unchanged.
3. **A successful repair that changes nothing is structurally
   `ALREADY_SATISFIED`.** The engine records the structured outcome and triggers
   the one bounded re-review from that field, never from the report's wording.
   The repair report rides along as a labelled claim; the re-review's decisive
   material is the engine-verified evidence.
4. **The stricter outcome wins and the conflict is durable.** If every clause
   is answered PASS but the reviewer returns `BLOCK`/`FINAL_BLOCK`, the block
   stands and the engine records `review_clause_verdict_conflict` and states the
   conflict in the review summary and details. If a PASS hides a failed or
   missing clause, the existing `review_acceptance_clause_block` still fails
   closed.
5. **Tasks without acceptance clauses keep the historical contract.** No
   engine-evidence block is added, and no new review requirement applies.

## 3. Consequences

- Honest work whose proof lives outside the diff can pass: an unchanged
  implementation file, a gate/test result, and a repository-wide absence are
  visible to the reviewer as trusted data.
- The guard is not weakened. A violating candidate still fails its clause
  against the same trusted evidence, and the two recorded Claude-batch
  violations still block.
- A no-diff repair costs at most one extra bounded review regardless of its
  report wording. A silent or unrelated no-diff repair keeps failing closed
  after that review.
- The engine can only add evidence and record conflicts; it never turns a
  reviewer block into an approval. No authority is widened.
