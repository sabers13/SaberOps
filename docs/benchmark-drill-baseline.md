# Phase 1 drill results table and regression baseline (H35)

This is the results table for the fake-mode benchmark drills: every phase 1 B2
and B2b drill, its phase 1 threshold, and the pre-1.1 baseline measured on local
`main`. Phase 1.1 lanes add their drills to the same table as they land; a drill
leaves it only with the feature it exercises and the owner decision that
removed it (ADR-0061).

## Baseline run

- Commit: local `main` at `7384d36` (before phase 1.1). Only the catalogue and
  its diagnostics differ from that commit; no drill implementation changed.
- Command (fake mode, no vendor CLI, no network):

  ```
  TMPDIR=/var/tmp .venv/bin/python -m pytest tests/test_benchmark_rig.py -n 2 -q
  ```

  Result: 58 passed in 318.46s. The xdist run does not relay the printed tables,
  so the per-drill seconds below are from the same suite run serially
  (`-q -s`, no xdist: 58 passed in 453.96s). Timings are one run on one host and
  are informational; the **threshold** column is the pass/fail bound.
- Every drill reached `pass`. "Final state" is each drill's own terminal label
  and is informational (`RUNNING` means the drill deliberately stopped a job
  that was still live).

<!-- drill-baseline:start -->
| drill | lane | group | slow | limit (s) | baseline (s) |
| --- | --- | --- | --- | --- | --- |
| kill worker | B2 | B2 | yes | 120 | 7.2 |
| kill scheduler | B2 | B2 | yes | 120 | 7.6 |
| quota exhausted | B2 | B2 | yes | 120 | 16.8 |
| bad route | B2 | B2 | yes | 120 | 0.7 |
| duplicate submission | B2 | B2 | yes | 120 | 10.1 |
| combined recovery | B2 | B2/B3 | yes | 60 | 9.2 |
| critical-only questions | B2b | F10 | yes | 120 | 0.2 |
| away mode | B2b | F11 | yes | 120 | 11.3 |
| plan dependencies | B2b | F8 | yes | 120 | 27.8 |
| plan file edit | B2b | F9 | yes | 120 | 3.1 |
| landing queue | B2b | F12 | yes | 120 | 33.5 |
| quota failover | B2b | F4 | yes | 120 | 21.7 |
| steering note | B2b | F13 | yes | 120 | 14.0 |
| one automatic nudge | B2b | F14 | yes | 120 | 1.3 |
| usage per subscription | B2b | F2 | yes | 120 | 25.6 |
| CLI update guard | B2b | F5 | yes | 120 | 0.0 |
| vendor self-healing | B2b | F18 | yes | 120 | 0.4 |
| worktree cleanup | B2b | F6 | yes | 120 | 3.1 |
| owner modes | B2b | F1 | yes | 120 | 0.5 |
| auto-accept | B2b | F15 | yes | 120 | 0.8 |
| imported native attempt | B2b | F16 | yes | 120 | 6.2 |
| pause and confirm | B2b | F17/F24 | yes | 120 | 9.8 |
| park running work | B2b | F17/F24 | yes | 120 | 4.4 |
| scout report | B2b | F19/F27 | yes | 120 | 8.6 |
| proxy family routing | B2b | F20/F21 | yes | 120 | 33.7 |
| walkthrough | B2b | F25 | yes | 120 | 4.8 |
| advisor on demand | B2b | F26 | yes | 120 | 32.5 |
<!-- drill-baseline:end -->

Every B2 drill is a fake-mode injected failure that must become visible with
exactly one notice inside the bound; the combined recovery drill adds B3's
60-second C2 limit. The B2b drills are the phase 1 Wave 3 feature drills.

## How to extend the table

1. Make the rig emit a `DrillResult` with a stable `name` and `group`.
2. Add one row to `DRILL_ROWS` in `src/saberops/benchmark.py` (name, group,
   lane, phase 1 threshold, test function).
3. Add one row to the table above, in the same position.
4. Run the drill's test once to record the baseline seconds, then the full gate.

The fast checks in `tests/test_benchmark_drills.py` compare the catalogue, the
baseline table and the producing code, so a missing or renamed row fails before
the slow drills run. Drill tests stay `slow=True`: the per-change gate skips
them and the full gate runs them.
