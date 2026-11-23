# ADR-0058: The benchmark submits paced by default and scores the final targets from submit

- **Status:** Accepted. Phase 1 confirming-run follow-up.
- **Date:** 2026-11-22
- **Decider:** Saber (owner), accepting the "proposed one to be safe" target
  definition
- **Related:** [ADR-0012](0012-benchmark-rig-modes.md) (benchmark fake/real
  modes), [ADR-0056](0056-benchmark-must-fail-seeded-review.md) (seeded
  must-fail rows), [ADR-0057](0057-clause-format-retry-and-gate-execution.md)
  (toy gate executes tests)

## 1. Context

The owner's final targets are defined from submit, because submit is when the
owner actually hands work over:

- time to accepted (submit → the accept actually performed): median ≤ 90 s,
  p95 ≤ 240 s;
- submit to start p95 ≤ 10 s.

Real confirming runs missed the from-submit median (117–129 s) even though the
latency from the worker start was 39–44 s, and a task submitted to an idle queue
started in about 2 s. The cause was the measurement, not the engine:
`scripts/benchmark.py` real mode submitted all ~10 tasks of a batch at once on a
host with two worker slots, so every row after the first two carried queue wait
that no daily use would produce. The figure was honest for that submission
pattern but answered a different question than the target.

## 2. Decision

1. **Paced submission is the default in both modes.** `saberops.benchmark`
   gains a shared `SubmitPacer`; the real batch driver and the fake rig submit
   the next measured task only when fewer than the configured concurrency
   (default 2, the count the confirming runs dispatched) submitted tasks are
   non-terminal. For a real batch, that slot count is
   `min(--concurrency, plan.max_concurrent)` for the batch route's pool plan, so
   Claude's one-slot plan gets one and Codex/OpenCode Go get two. A measured
   task therefore enters a free worker slot, and submit-to-start and
   time-to-accepted describe an unqueued job. The setting is explicit
   (`--concurrency N`) so it can match the host's real worker count.
2. **Burst submission is kept as an explicit stress option.** `--submit burst`
   reproduces the original all-at-once submission. Its report is labelled as a
   stress measurement and is never scored against the targets, because queue
   wait is exactly what it measures.
3. **The report scores the final targets from submit under pacing.** A paced
   real run prints time-to-accepted median, time-to-accepted p95 and
   submit-to-start p95 against their limits with a target verdict, and a missed
   target fails the run. Percentiles are nearest-rank, so every printed figure
   is one task's measured latency and no value is interpolated. Must-fail rows
   have no accept or worker start by design and never enter a timing target.
   Fake mode prints the same rows as information only: its fake worker is rig
   evidence, not a vendor latency measurement.
4. **From-start figures stay visible.** The results table adds a `from start`
   column (worker start → accept) beside `to accepted` (submit → accept) so the
   owner can see engine latency and total owner latency together.
5. **The fake/rig benchmark proves the pacing.** The ten-task fake run asserts
   that the default is paced and that submit-to-start p95 stays within the 10 s
   target; on the pre-pacing code the same catalogue submitted at once and later
   rows measured 14–28 s of queue wait, so the assertion failed. A fast unit
   test drives the shared driver with a stub two-slot engine and shows paced
   submission never exceeds the configured depth while burst submits every task
   before the first tick.

## 3. Consequences

- Target figures now answer "how long does submitting one task take in daily
  use", which is the question the owner set.
- The old all-at-once figure remains reproducible and clearly labelled, so the
  measurement change cannot hide a real regression: a paced run that misses a
  target is a genuine failure, and a burst run's higher submit-to-start is
  expected and unscored.
- Real-mode wall time grows slightly because later tasks wait for a slot instead
  of being queued; that is the realistic pattern and it spends no extra quota.
- The engine scheduler is unchanged. Pacing is a benchmark submission policy,
  not engine behaviour, and the targets themselves are not redefined here.

## 4. Amendment: paced slots follow the route's pool capacity (2026-11-23)

The phase 1 confirming run on local `main` `7384d36` passed every honest and
must-fail row but missed submit-to-start p95 (54.2 s against 10 s). The cause was
this ADR's global pacer: it kept two Claude tasks submitted while the packaged
Claude plan allows one concurrent job, so the second task waited 27–52 s in
`QUEUED` with `waiting_reason=CONCURRENCY`.

Each real batch now resolves its worker route's plan through the same packaged
plus owner plan view admission uses and drives that batch with
`min(--concurrency, plan.max_concurrent)` slots. A fast rig test overrides a pool
to one slot while the global default stays two and asserts that the second task
is never submitted while the first is non-terminal and that the batch records no
`CONCURRENCY` wait. The report prints each batch's slot count beside the deepest
queue. Its old "queue wait is not measured" sentence was wrong: submit-to-start
*is* submit-to-dispatch plus the engine's queue wait, so the line now says the
wait is included. The engine scheduler, plan values and targets are unchanged.
