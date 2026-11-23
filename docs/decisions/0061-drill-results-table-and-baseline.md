# ADR-0061: One drill catalogue, the results table and the phase 1 baseline

- **Status:** Accepted. Phase 1.1 lane H35, wave 0.
- **Date:** 2026-11-23
- **Decider:** Saber (owner), via the phase 1.1 plan lane H35
- **Related:** [ADR-0012](0012-benchmark-rig-modes.md) (benchmark fake/real
  modes), [ADR-0056](0056-benchmark-must-fail-seeded-review.md) (seeded
  must-fail rows), [ADR-0058](0058-benchmark-paced-submission-targets.md)
  (paced submission and the final targets)

## 1. Context

Phase 1 section B2 built the fake-mode benchmark and B2b added the Wave 3
drills. Each drill test asserted its own group, visibility and pass state, but
nothing tied the drills together:

- no single list said which drills must exist, so a dropped or renamed drill
  was visible only by reading every test;
- the printed results table is generated from whatever the run emitted, so a
  drill that silently stopped being emitted simply disappeared from the table;
- the slow marker and the phase 1 visible-state thresholds were separate
  literals in the test harness, not facts the table could check;
- there was no pre-1.1 measurement to compare later phase 1.1 runs against.

Phase 1.1 requires the backend to be proven solid before phase 2's UI starts
(plan exit tests 2, 5, 6 and 8). H35 needs every phase 1 drill to run green in
the full gate, appear in a results table, keep phase 1's thresholds, and let a
drill leave the table only with the feature and the owner decision that removed
it.

## 2. Decision

1. **The drill catalogue is the single list.** `DRILL_CATALOGUE` in
   `scripts/benchmark_core.py` names every drill the fake-mode rig must keep: its
   exact `DrillResult.name`, its group (`B2` or the feature id), its phase 1
   lane, the phase 1 threshold in seconds, the pytest node id that runs it, and
   its slow marker. The threshold constants (`DRILL_LIMIT_SECONDS` = 120 s,
   `RECOVERY_DRILL_LIMIT_SECONDS` = 60 s) live there too; the test harness
   imports them instead of restating the numbers.
2. **Every drill run checks itself against the catalogue.** The drill helpers
   assert each emitted result is registered with the same group and slow marker
   and finishes inside its catalogue threshold. A new, renamed or regrouped
   drill fails the run until the catalogue is updated; removing a drill's code
   without removing its catalogue entry fails the fast catalogue test, which
   checks the drill name still appears in the rig source.
3. **The results table has a recorded baseline.**
   `docs/benchmark-drill-baseline.md` holds the wave-0 regression baseline: the
   pre-1.1 local `main` commit, the exact command, and one row per drill with
   its observed seconds and pass state. A fast test compares that table with
   the catalogue, so extending the rig, the catalogue and the table is one
   three-line change and cannot drift.
4. **Timings are informational; thresholds are enforced.** The recorded
   baseline seconds describe one run on one machine and are not asserted. The
   phase 1 thresholds in the catalogue are the pass/fail bound.
5. **Drills are slow by default.** Every phase 1 drill carries `slow=True`; the
   per-change gate selects only affected tests and the full gate runs them all,
   so phase 1's 3-minute per-change budget is preserved.
6. **A drill leaves the table only with its feature.** After a phase 1.1
   deletion lane lands, the phase 1 regression drills are rerun. Removing a
   drill is a recorded owner decision tied to the feature it exercised, never a
   silent test deletion (phase 1.1 "Never delete check protection, review
   evidence or a safety drill" rule).

## 3. Consequences

- Adding a phase 1.1 drill is: emit the `DrillResult`, add one
  `BenchmarkDrill` entry, add one baseline-table row, rerun the gate.
- The printed `BenchmarkReport.table()` stays the human-facing results table;
  the catalogue and baseline document are what make it complete and comparable.
- The baseline is a regression reference for H35 after each deletion lane and
  for the phase 1.1 confirming benchmark (H14), not a new pass/fail gate of its
  own.
- No new dependency, no new runner and no change to the real-mode safety gate.
