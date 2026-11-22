# ADR-0056: Must-fail benchmark rows review a seeded violation

- **Status:** Accepted. Phase 1 confirming-run fix lane.
- **Date:** 2026-11-22
- **Decider:** Saber (owner), standing order for the confirming real-run fixes
- **Related:** [ADR-0053](0053-acceptance-clause-review-gate.md) (acceptance
  clause review gate; §2.4 is amended here), [ADR-0012](0012-benchmark-rig-modes.md)
  (benchmark rig modes), [ADR-0008](0008-candidate-bound-acceptance.md)
  (candidate-bound review evidence)

## 1. Context

ADR-0053 §2.4 made the must-fail rows deterministic on the *measurement* side:
the rig probes the blocked candidate commit and refuses to count a block on a
candidate that still satisfies acceptance. The confirming run
(`data/so-confirm2/report.md` §5) showed the remaining
flaw was on the *production* side: the row still asked a worker to produce the
violation. Codex and OpenCode read the contradictory goal and chose the
acceptance clause over the goal, so six of eight must-fail observations had a
compliant candidate. The probe correctly refused to count those blocks and the
whole benchmark failed -- but it measured the worker's judgement, not the
reviewer's guard. Only the two Claude observations had a real violation to
block.

## 2. Decision

1. **Real mode seeds the violating candidate for a must-fail row.** It commits
   the empty-name removal and the test deletion itself on a real worktree of
   the toy repository, creates the same RUNNING run / successful attempt rows
   the engine records for a candidate, and drives only the reviewer through the
   configured different-vendor CHECK binding. No worker prompt is sent and no
   job is submitted for that row.
2. **The probe still governs the row.** The row passes only when the reviewer
   blocked and the deterministic probe confirms the exact seeded commit
   violates acceptance. A compliant candidate is **not measured**, never a
   failure; a rig that cannot seed a candidate also leaves the row unmeasured.
   A review dispatch that errors fails the row and can never pass on an earlier
   partial verdict.
3. **The row's ground truth is visible.** The results table prints a
   `must-fail probes` footer naming each must-fail row's
   `acceptance_violation` result and the blocked candidate SHA.
4. **Fewer hidden prompts.** A seeded review costs one reviewer call and no
   worker call, so a must-fail row can no longer spend worker quota proving a
   model's willingness to violate its own acceptance.

This amends ADR-0053 §2.4: a `probe=False` row is excluded from the verdict
rather than failing the benchmark, and real mode no longer relies on an honest
worker to create the violation.

## 3. Consequences

- The must-fail rows measure exactly one thing: whether an independent reviewer
  blocks a candidate that demonstrably violates the task's acceptance clause.
  A worker's refusal to violate acceptance can no longer move that measurement.
- A must-fail row still costs a real reviewer prompt and real reviewer quota;
  the table and the durable review records remain the evidence.
- The probe remains deterministic and model-free, and the seeded commit is a
  normal git commit in the toy repository, so the owner can reproduce the
  probe with `git show <sha>:app.py`.
- No authority is widened: seeding a candidate is a benchmark-only setup step
  in an isolated store. Production runs still receive candidates from workers.
