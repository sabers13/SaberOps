# ADR-0094: The second dead-code pass removes uncalled definitions only

- **Status:** Accepted. Implemented in phase 1.1 lane H31.
- **Date:** 2026-11-24
- **Decider:** Saber (owner): the 2026-11-22 answer ("do your suggestion and
  maximise the deletion i feel like there are a lot of spam code is in repo, add
  all of this to the new phase 1.1 plan") and the 2026-11-23 answer ("update the
  plan, make the plan in style of phase 1 parallel favor"), applied to lane H31
  ("second dead-code pass").
- **Related:** phase 1.1 plan lane H31; phase 1 "Rules for every lane";
  [ADR-0075](0075-dead-code-sweep.md) (the first sweep),
  [ADR-0083](0083-splitting-is-the-planners-job.md) (in-run plans, slicing and
  the team mode are gone),
  [ADR-0086](0086-one-routing-configuration.md) (routing source of truth),
  [ADR-0092](0092-test-suite-admits-without-the-host-guard.md) (check
  protection on the real path, H32), [ADR-0093](0093-docstrings-and-comments-keep-contracts.md)
  (the preceding lane on local `main`).

## 1. Context

H31 reruns the no-caller scan after H30 and H32. Its authorized area is
definitions in `src/` whose name appears nowhere else in source, scripts or
tests, with web routes and HTTP handler methods excepted, plus the test-only
leftovers earlier lanes reported for this lane. The first sweep (H21) removed
508 source lines; the larger phase 1.1 deletion lanes removed whole features,
leaving behind helpers, readers and model classes those features used.

The scan used here parses every `src/`, `tests/` and `scripts/` Python file with
`ast`, collects every module/class/function/method definition and module-level
constant, and counts word-boundary references of each name in the tracked
source tree. Definition decorators that register code dynamically (Click/Typer
commands, MCP tools, FastAPI routes, pytest fixtures, validators, properties and
similar) are excluded from the dead report. The full before/after report and
the caller lists are in `data/so-h31/notes.md`.

**Verified before deleting.** Each name below was checked with `git grep -w`
over every tracked file (source, tests, scripts, docs, templates, static assets,
JSON defaults) in addition to the AST scan. No MCP tool, scheduler, CLI, web,
script, benchmark-rig or test path referred to any removed name. The only
remaining no-reference definition is `NoRedirect.redirect_request`, which is an
override invoked implicitly by the standard library's `urllib` redirect
handler; it is not dead and stays.

## 2. Decision

1. **Delete only definitions with no caller.** No behaviour, contract or safety
   property is removed. Where a dead writer was the only seed for a retained
   evidence reader, the test seeds the retained row directly instead of keeping
   a production writer alive.
2. **Two whole web-only projections are gone, with their tests:**
   `candidate_diff.py` (the run page's bounded diff projection) and
   `gate_outcome_projection.py` (the removed gate-outcome view). Neither module
   had a caller outside its own test; the durable candidate, gate receipt and
   gate history evidence they read stays.
3. **The dead leftovers of removed features go with them:** the in-run plan and
   work-package writers, the independent-work integration models, the
   acceptance-marker helpers, the attempt-trace failure/native-event writers,
   the web-only constants, the plan/scope model helpers and the dead runtime
   resolution methods.
4. **Cascades are removed too.** Deleting a writer made its private helpers,
   constants and models dead (`_summary_digest`, `ACCEPT_EVIDENCE_PREFIX`,
   `DEFAULT_DIGEST_SECONDS`, `IndependenceState` and friends); those went in the
   same pass rather than surviving as new orphans.
5. **The tests that only tested removed code are deleted.** Tests that used a
   removed helper as setup were rewritten to the retained read path
   (`get_worker_checkpoints_for_run`, a direct row seed) so their safety
   assertion still runs.
6. **Deliberately kept, reported not hidden:**
   - **Check protection as H32 left it:** `gate_scope.py` and the protected
     path constants stay untouched.
   - **Gate admission surface:** `GateSlotPool`, `GateSlotPoolProtocol`,
     `GateSlotPool.current_lease`, `host_disk_admission` and `gate_profiles.py`
     have no `src/` caller after the engine executor removal (H32 noted this for
     H31), but `tests/test_gate_engine_execution.py` is one of the admission
     safety drills H29 protects. Deleting the pool would delete that drill, so
     the surface stays. Its now-unused `DEFAULT_SLOT_WAIT_SECONDS` alias went.
   - **Review and acceptance evidence:** `Database.get_findings_for_review`,
     `OwnerActionSupervisor.request_review` / `request_accept` and
     `Database.get_active_owner_action` are used only by the owner-action and
     review-evidence drills. They are the acceptance/review surface H23
     documented as waiting for phase 2 U4, not dead code.
   - **Steering safety:** `jobs.service.undelivered_steering_notes` (used only
     by H6's redirect drill) is the undelivered-note guard; kept.
   - **Data-loss guard:** `Database.journal_record_started` is the only writer of
     the STARTED side-effect state that continuation refuses to resume across;
     the drill that proves that refusal stays.
   - **Test teardown:** `capacity.loop.stop_scheduler` is how tests stop the
     scheduler they started; deleting it would leak processes between tests.
   - **Worker instruction budget:** `validation_policy.MAX_WORKER_INSTRUCTION_LINES`
     is a documented H12 bound asserted by the skills test.
   - **Contract fixtures and evidence reads:** `REQUIRED_SERVER_ROUTES`,
     `ROUTED_ROLES`, storage inventory/ledger reads, the install-hygiene path
     helpers, `web._csrf_token` / `_model_catalog_store`, and several small
     question/frontier/session readers are only touched by tests that assert a
     retained contract; removing them would rewrite those assertions for no
     product gain.
   - **HTTP handler methods and the `urllib` override** are excluded by the
     scan's own rule.
7. **No record, guard or test was weakened to make the pass green.**

## 3. Consequences

- Measured reduction (`wc -l` on tracked files): `src/` 131,426 → 129,574
  (**−1,852**); `tests/` 97,793 → 96,187 (**−1,606**). Diff: `src` +9/−1,861,
  `tests` +32/−1,638 (the additions are the new guard test and the direct
  evidence-row seed).
- The no-caller scan after the pass reports no `src/` definition without a
  caller except the documented implicit-caller override; the remaining
  test-only definitions are the deliberately kept safety, evidence, teardown,
  security and contract surfaces listed above.
- The public CLI and MCP surfaces are unchanged. No store schema, setting,
  routing document or persisted evidence format changed; old rows keep their
  readers.
- `tests/test_h31_second_dead_code_pass.py` fails on the base tree (5 of 6
  tests) and passes on this branch. It also asserts every kept safety surface
  above is still present, so a future sweep cannot quietly remove it.

## 4. References

- Phase 1.1 plan, lane H31 and "Rules for every lane"
- Phase 1 plan, "Rules for every lane"
- Branch base `9e2dae3` (local `main`); ADR-0075 (first sweep), ADR-0083,
  ADR-0086, ADR-0092, ADR-0093
- `data/so-h31/notes.md` (scan before/after, caller lists, kept list, gate
  result)
