# ADR-0009: Fast per-change checks, one full gate per candidate, evidence-based pruning

- **Status:** Accepted and implemented (Phase 1 lane E2, 2026-11-19). The
  per-change/full-gate split, the shared type-check cache, the single selection
  path, the non-blocking coverage-map rebuild, the cheaper fixtures/waits and
  the local-only landing rule are in force.
- **Date:** 2026-11-19
- **Decider:** Saber (owner), Phase 1 plan lanes A2 and E2
- **Related:** [ADR-0005](0005-mcp-first-engine-and-phase-2-board.md),
  [ADR-0007](0007-dynamic-memory-budget.md),
  [ADR-0010](0010-receipt-bound-gate-skip.md)

## 1. Context

The owner reported that tests and gates take too much time and that the suite
contains tests that are no longer valuable. Measurement showed the per-change
gate still runs whole-repository lint and a strict type check, falls back to the
full suite for common changes, coexists with a second selection path, and that
the coverage-map rebuild holds the exclusive slot after every landing. Every
accepted change also runs the full gate twice.

Pruning is a safety question: removing a test removes a guarantee unless some
retained test covers the same behaviour.

## 2. Decision

1. **Two speeds.** The per-change gate is the default for task work; the full
   suite runs when landing on `main`. The strict type-check cache is shared, so
   a type check does not start cold.
2. **One full gate per candidate SHA.** Repair retries use the per-change gate.
   The full gate is the candidate-level evidence, not a per-attempt ritual.
   A passing full gate records a receipt; acceptance skips the duplicate gate
   only under the conditions in [ADR-0010](0010-receipt-bound-gate-skip.md).
3. **Selection must not fall back to the full suite for the common cases the
   measurement identified,** and the lifecycle rig's tests always run.
   Automatic selection without a coverage map uses the narrower direct-importer
   fallback; an explicit `--strategy static` keeps the legacy transitive
   closure. `pytest.mark.slow` tests are excluded from per-change selection.
4. **Prune only by evidence.** Every test proposed for removal names the
   retained test that covers the same behaviour. Tests of admission, billing,
   security, acceptance, migration and recovery are never pruned. The measured
   dead-subsystem tests were already removed with their subsystems; no further
   test met the covering-test rule, so this lane removed none.
5. **One selection path.** `lane-check.sh` and `gate-affected.sh` both run the
   `gate_tools.py` lane runner, which calls `scripts/select_tests.py`; there is
   one selection implementation. The coverage-map rebuild runs detached under
   its own non-blocking lock and a normal memory reservation, so it no longer
   holds the exclusive gate slot after every landing.
6. **Cheaper fixtures and waits.** The heaviest git-fixture files copy a
   template repository instead of initialising and committing per test, and
   sleep-based polling becomes condition, event or durable wake waits.
7. **Delivery during the benchmark.** Work lands on local `main`. Pushing and
   forge pull-request creation remain separate owner decisions; publishing is
   not part of this phase.

## 3. Consequences

- A task gets feedback from the per-change gate; the full suite's cost is paid
  once per candidate rather than twice per accept.
- Every removed test has a named owner, so pruning can be reviewed instead of
  trusted.
- The duplicate accept gate is governed separately by
  [ADR-0010](0010-receipt-bound-gate-skip.md); nothing in this record permits
  skipping a gate.
