# ADR-0092: The test suite admits without the host guard and keeps one isolation fixture

- **Status:** Accepted. Implemented in phase 1.1 lane H29.
- **Date:** 2026-11-24
- **Decider:** Saber (owner): the 2026-11-22 answer ("do your suggestion and
  maximise the deletion ... add all of this to the new phase 1.1 plan") and the
  2026-11-23 answer ("update the plan, make the plan in style of phase 1
  parallel favor"), applied to lane H29 ("remove tests of deleted code, merge
  duplicate fixtures, drop tests that only re-assert another test's facts ...
  report gate time before and after").
- **Related:** [ADR-0007](0007-dynamic-memory-budget.md) (the gate's dynamic
  memory budget and its host guard), [ADR-0082](0082-owner-mode-carries-authority.md)
  (the owner store a test must never reach), `data/so-testprof/report.md` (the
  suite profile this lane works from), `data/so-h0/report.md` (the gate
  baseline).

## 1. Context

H29 runs after every phase 1.1 deletion lane, H4 and H32. Its authorized area is
tests of deleted code, duplicate fixtures, and tests that only re-assert another
test's facts.

Measurement first:

- **Nothing is left to delete with deleted code.** Every `saberops` module and
  every symbol imported by `tests/**` resolves against the post-1.1 `src/`
  tree (an AST check over 195 files found 0 missing modules and 0 missing
  names). Each deletion lane already took its own tests and left a removal
  guard (`test_h15_scope_guessers_removed.py`, `test_h16_certification_removed.py`,
  `test_h10_web_leftovers.py`, `test_h19_plan_and_tool_layers.py`,
  `test_h21_dead_code_sweep.py`, `test_orchestrator_model_removed.py`); those
  guards stay, because they fail if the removed code returns.
- **One test asset outlived its feature.** `tests/js/run_live_refresh.cjs`
  drove the run page's live-refresh controller (`data-run-refresh`,
  `saberops:run-tab`). H23 deleted that controller and its Python test; the
  script stayed behind with no caller. It is deleted here.
- **No test only re-asserts another test.** An AST body-hash scan over every
  `def test_*` found one identical pair,
  `tests/test_gate_outcome_projection.py::test_nonpositive_tests_ran_blocks_pass`
  and `::test_malformed_tests_ran_blocks_pass`; each drives a different
  malformed `tests_ran` value through the same guard, so both stay.
- **The duplicate mass is fixtures, and the gate's flakiness is fixture
  admission.** Eight byte-identical copies of the `isolated` owner-store
  fixture and five near-identical copies of `isolated_xdg` existed. The gate
  profile (`so-testprof` §4.3) shows the rest of the cost: the gate's memory
  guard is a *host* rule, it reads real `MemAvailable` and waits for a budget
  sibling lanes' gates already hold, so 22 tests per full gate earlier hit the
  180 s pytest cap *inside* `MemoryLedger.acquire`; the five
  `test_main_gate_*` tests re-entered the same host lease from inside the gate
  for up to 600 s; the rigs' in-test run budgets (10-15 s per
  worker/gate/review phase) were sized for an idle host; and the p6 refresh
  fixture expired its planning presence after 60 s, which is shorter than the
  wave that precedes the assertion on a loaded host.

## 2. Decision

1. **`tests/conftest.py` admits without the host guard, by default, for the
   test session.** `_isolated_memory_ledger` now sets
   `SABEROPS_GATE_MEMORY_GUARD=0`. Admission still computes the same predicted
   peak, grant, learned peak and worker count; only the wait/refusal that reads
   the host's live memory is skipped. A test or module that needs the guard's
   contract carries `@pytest.mark.memory_guard`, which sets it back to `1`:
   `test_memory_budget`, `test_gate_slots_dynamic`, `test_gate_engine_execution`,
   `test_c1_memory_ledger` (which also opts in per test) and the memory-ledger
   and `exclusive` tests in `test_gate_tools`. The two `exclusive` tests also
   pin zero host PSI, as `test_gate_slots_dynamic` already did, so a sibling
   lane's pressure cannot park them. The guard itself, quota, billing consent,
   isolation, checks, review, acceptance and pause are unchanged.
2. **The rigs' in-test run budgets become stall detectors, not load
   detectors.** Every test-only `RunRequest` budget below 60 s for worker, gate
   or review becomes 60 s, and the run wait that paired with it becomes at
   least 90 s, in `test_p6_plan_landing`, `test_lifecycle_rig`,
   `test_c4_parallel_jobs`, `test_e4_job_evidence`, `test_e6_refresh`,
   `test_p3_plan_state`, `test_f27_cheap_scout` and `test_f3_tail`. They still
   fail a stuck engine, well inside the 180 s pytest bound; they no longer fail
   a busy host. No product timeout and no phase 1 drill threshold
   (`DRILL_LIMIT_SECONDS`) changes.
3. **The p6 refresh fixture keeps presence live for the whole test.** It no
   longer forces a 60 s planning heartbeat; conftest's long default applies.
   The pause drill closes the session by releasing the lease and ending
   attendance, so it never depended on expiry.
4. **The benchmark rig gets a bound its own assertion can win.** The ten-task
   table's stall bound rises from 180 s (exactly the pytest bound, so a slow
   host produced an opaque `TIMEOUT` instead of a result) to 300 s, and
   `tests/test_benchmark_rig.py` carries a 480 s outer `timeout` marker. The
   phase 1 drill visible-state threshold (120 s) is untouched.
5. **One isolation fixture per shape.** `tests/conftest.py` holds `isolated`
   (owner store, cache and the four `ORCH_*` stores) and `isolated_xdg` (owner
   state/config, no inherited `ORCH_*` store). The eight identical `isolated`
   copies and the five `isolated_xdg` copies are deleted; a file that needs a
   different shape keeps its own local definition, which wins over conftest's.
   The no-op `_hermetic_memory_admission` fixture in `test_loop` goes with the
   new default.
6. **No safety test is removed or merged.** The check-protection tests
   (`test_h32_gate_protection`, `test_gate_scope`, `test_gate_tamper`), the
   drill catalogue (`test_benchmark_rig`, `test_benchmark_drills`,
   `tests/benchmark_rig.py`, `tests/benchmark_wave3.py`), H4's review evidence
   (`test_review*`, `test_n14_*`), acceptance/receipts/admission
   (`test_accept`, `test_gate_receipt`, `test_memory_budget`,
   `test_c1_memory_ledger`, `test_gate_engine_execution`), the lifecycle rig and
   the billing/consent/pause drills are untouched. `tests/quarantine.txt`
   remains owner-only and empty. `tests/conftest.py`'s real-home isolation
   guard (H22) is unchanged.
7. **A stale, git-ignored `src/saberops/devscope/` bytecode directory was
   removed from the lane's worktree.** H15's removal guard
   (`test_h15_scope_guessers_removed.py`) failed on it because `find_spec`
   still saw a namespace package; it is build residue, not source, and no
   tracked file changed with it.

## 3. Consequences

- A full gate on a shared host stops turning a sibling lane's load into a
  timeout verdict. The measured before/after wall time, node counts and failure
  set are in `data/so-h29/notes.md`; H0's own caveat still applies (its 413.6 s
  quiet reference was a smaller suite and the last clean number).
- The guard's own contract keeps a test in every place it is decided:
  `tests/test_h29_harness_contract.py` proves an ordinary test admits with no
  budget and that a `memory_guard` test refuses; the guard's modules keep their
  existing assertions.
- Test-only budgets now express "the engine is stuck" rather than "the host is
  busy". A genuinely hung fake worker still fails at 60 s and a hung run at
  90 s, inside the suite's 180 s bound, so a real defect is not hidden.
- Measured reduction (`wc -l`): source 130,323 → 130,323 (unchanged; this lane
  edits tests, fixtures and this record only). Test Python 96,929 → 96,841
  (−88) over 28 files (`+209 / −250`), plus the 170-line orphaned `.cjs`, so
  the test tree is **258 lines smaller**; the commit changes 30 files with this
  record. Collected nodes 3,257 → 3,259 (two new harness-contract tests).
- Full gate, same host, same lock, same base: **1,528.11 s with 1 failed and 5
  flaky → 876.33 s with 0 failed and 0 flaky** (0 cgroup freezes), the first
  green gate on this host since 2026-11-23 10:27. The last eleven green
  pre-1.1 gates measured 699–967 s at 3,468–3,563 nodes, so 876 s at 3,259
  nodes is inside that range; H0's 413.6 s quiet reference was a smaller suite
  (2,838 nodes) from a different machine state and its own §1.2 records that it
  could not be re-measured. The remaining measured lever (seed a pre-migrated
  store template instead of creating ~500-900 stores per gate, worth an
  estimated 60–170 s wall) is a fixture-cost follow-up, not a prune.
- What stays load-sensitive by design and is reported, not changed: phase 1's
  120 s drill visible-state threshold; the wall-clock assertions that encode a
  product bound (`test_cli_usage`, `test_sqlite_contention`,
  `test_storage_budget`, `test_supervisor`, `test_f17_pause`); the 30 s SQLite
  write budget (`sqlite_policy.WRITE_BUDGET_SECONDS`), which a cgroup freeze
  during a full gate can exhaust; the gate's own PSI freeze (`freezes` in the
  history entry), which belongs to the gate budget H35 owns; the `serial`
  marker (registered but not honoured by the gate); the module-scoped
  `project_template` in `test_continue_run` (rebuilt once per xdist worker
  under `--dist worksteal`); and fresh-store creation cost (a pre-migrated
  store template is a follow-up, not a prune).

## 4. References

- Phase 1.1 plan, lane H29 (`phase-1.1-core-plan.md`), "Order at a glance" and
  "Rules for every lane"
- `data/so-testprof/report.md` §3, §4.3, §5, §8, §10 and `data/so-h0/report.md`
  §1.2
- `tests/conftest.py`, `tests/test_h29_harness_contract.py`,
  `tests/test_memory_budget.py`, `tests/test_gate_slots_dynamic.py`,
  `tests/test_gate_engine_execution.py`, `tests/test_c1_memory_ledger.py`,
  `tests/test_gate_tools.py`
- `tests/test_p6_plan_landing.py`, `tests/test_lifecycle_rig.py`,
  `tests/test_c4_parallel_jobs.py`, `tests/test_e4_job_evidence.py`,
  `tests/test_e6_refresh.py`, `tests/test_p3_plan_state.py`,
  `tests/test_f27_cheap_scout.py`, `tests/test_f3_tail.py`,
  `tests/benchmark_rig.py`, `tests/test_benchmark_rig.py`
