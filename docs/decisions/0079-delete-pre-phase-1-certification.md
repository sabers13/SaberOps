# ADR-0079: Delete the pre-phase-1 certification engine

- **Status:** Accepted. Phase 1.1 lane H16.
- **Date:** 2026-11-23
- **Decider:** Saber (owner), Q12 (delete `orch certify` after its unique drills
  move into the benchmark) and Q15 (wire check protection, do not delete it)
- **Related:** [ADR-0005](0005-mcp-first-engine-and-phase-2-board.md) (MCP-first
  engine), [ADR-0060](0060-gate-protection-on-the-real-paths.md) (gate
  protection on the real paths)

## 1. Context

`certification.py` (828 lines) and `orch certify check|run|report|approve`
evaluated Q4 dogfooding criteria, the safe-closure contract and nine resilience
scenarios. Every verifier was a pure predicate over booleans supplied by a JSON
evidence file; nothing in the module observed the engine. A regression in the
real path could not make a certification check fail unless the evidence author
also changed the file.

The phase 1 benchmark rig now runs the real job, scheduler, worker, gate,
review and accept path in fake mode, and the subsystems the nine scenarios
described have their own live tests. H32 landed the gate-protection drill on
the real job and acceptance paths (`9e8ecd9`, ADR-0060), so the one scenario
that protected a real check no longer needs its own path list.

Caller census before deletion (MCP, scheduler/capacity, CLI, web, scripts, the
benchmark rig and all tests): the module was imported only by `cli.py` (one
`TYPE_CHECKING` import plus four lazy handler imports), `tests/test_end_certification.py`
and its documentation. No MCP tool, scheduler, capacity, web route, script or
benchmark-rig path used it. The CLI verb was the only entry point.

## 2. Decision

1. Remove `src/saberops/certification.py`, the `certify` subcommand and its
   four handlers, `tests/test_end_certification.py` and
   `docs/end-certification.md`. README no longer lists the feature.
2. Keep every live safety property where it is enforced and tested. The table
   below is the lane's "home or dropped" record for each certification item.
3. Guard the cut with `tests/test_h16_certification_removed.py`: the module is
   not importable and `orch certify` is not an accepted verb. The test failed
   on the base commit and passes after the deletion.

## 3. Disposition of the certification items

| Item | Home after deletion |
| --- | --- |
| S1 mid-turn kill repeats nothing | **Dropped as a certification scenario.** Its `TurnJournal` belonged to the in-engine orchestrator removed by ADR-0005. The live no-duplicate property is drilled on the real path: `tests/test_benchmark_rig.py::test_combined_recovery_drill` (one terminal event and one notice after restart) and the duplicate-submission drill inside `tests/test_benchmark_rig.py::test_failure_drills_reach_visible_state`. |
| S2 context overflow latches | **Dropped.** The 50% model-window latch and handoff capsule belonged to the in-engine orchestrator prompt loop removed by ADR-0005; no live code path implements or needs it. |
| S3 second session read-only | **Dropped as a lock scenario.** There is no concurrent in-engine orchestrator lock. The live second-session guarantee is the read-only MCP tail: `tests/test_f3_tail.py::test_tail_job_is_a_read_only_tool` and the server's `read_only` mode in `tests/test_f3_tail.py`. |
| S4 dead worker reuses its own worktree | **Home:** `tests/test_benchmark_rig.py::test_combined_recovery_drill` kills the worker, restarts the scheduler, and reads the preserved work file from the same worktree path; cold recovery in `orchestrator.py` keeps the crashed attempt's worktree. |
| S5 gate protection | **Home (H32):** `tests/test_benchmark_rig.py::test_gate_protection_must_fail_drill` runs `run_gate_protection_drill` against the real job check and acceptance, verifying the protected definition before any candidate check runs. |
| S6 standing decisions scope | **Home:** `tests/test_unattended.py::test_exact_scope_match_applies_the_standing_decision` (exact scope, no analogy) and `tests/test_f11_away.py::test_scheduler_parks_only_blocked_task_and_keeps_audit_across_restart` (only the blocked task parks). |
| S7 morning report | **Home:** `tests/test_f11_away.py::test_return_summary_matches_records_and_catch_up` and `tests/test_f11_away.py::test_return_summary_lists_candidates_awaiting_acceptance`; the summary is rebuilt from the away journal in `away.py`. |
| S8 stall alarm | **Home:** `tests/test_unattended.py::test_idle_at_the_threshold_raises_an_alarm`, `tests/test_unattended.py::test_extreme_idle_raises_a_critical_alarm` and `tests/test_unattended.py::test_stalled_scopes_raise_an_alarm_even_when_fresh` against `detect_stall`. |
| S9 mid-wave idea intake | **Dropped.** RC-1 idea intake edited a running wave; waves are gone and PLAN.md lanes are edited through the plan path that H19 owns. No intake entry point exists to drill. |
| Declarative evidence files (Q4, `load_evidence_file`) | **Dropped.** Q4-A..D are release/dogfooding criteria, not runtime safety; phase 1.1 H14's confirming benchmark is the replacement evidence. |
| CLOSE-1..CLOSE-5 safe closure | **Home:** the live contract is `storage_cleanup.py` and `docs/storage-lifecycle.md`; tests include `tests/test_storage_gc.py::test_candidate_outside_worktree_base_is_refused`, `tests/test_storage_gc.py::test_audit_failure_surfaces_uncertainty_and_stops`, `tests/test_storage_gc.py::test_unknown_owner_is_never_a_candidate`, `tests/test_storage_gc.py::test_paths_outside_owned_roots_are_never_candidates` and `tests/test_storage_gc.py::test_scratch_needs_a_provably_dead_owner_inside_the_roots`. |
| Owner approval record | **Dropped.** The approval record authorized self-dogfooding at a point in time; owner acceptance and the away/question records carry live decisions now. |

No safety drill is deleted: S5 moved to the real path first (H32), and the
remaining live properties stay with the code that enforces them. Only the
evidence-file predicates that could not detect a regression are removed.

## 4. Consequences

- `orch certify` is gone; `orch --help` no longer lists it. Historical release
  notes keep their record of what shipped at that release.
- The engine keeps one drill home (the benchmark rig) and one protected-check
  path (H32), instead of a second path list that never ran against the engine.
- A later change that reintroduces a certification engine or the verb fails
  `tests/test_h16_certification_removed.py`.
