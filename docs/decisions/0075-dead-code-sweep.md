# ADR-0075: Sweep functions no path reaches

- **Status:** Accepted. Implemented in phase 1.1 lane H21.
- **Date:** 2026-11-23
- **Decider:** Saber (owner), phase 1.1 owner answers Q1-Q15 (2026-11-22,
  2026-11-23) and "maximise the deletion" (2026-11-22)
- **Related:** [ADR-0005](0005-mcp-first-engine-and-phase-2-board.md) (the
  planning CLI is the orchestrator; MCP is the engine surface),
  [ADR-0060](0060-gate-protection-on-the-real-paths.md) (check protection, which
  this lane does not touch)

## 1. Context

The second read-only review of local `main` at `395d9ba` listed functions that
nothing in the repository calls. Phase 1.1's rule is to delete what no owner,
planner or scheduler path reaches instead of keeping layered code for a feature
nobody uses. Every safety property (quota, billing consent, isolation, checks,
independent review, owner acceptance, pause) stays where it is enforced.

Before deleting, the lane re-ran the caller census on the rebased branch (local
`main` `0e0227f`): a text scan over `src/`, `tests/`, `scripts/`, `docs/` and the
benchmark rig for each symbol name, plus the module imports of every file that
imports the owning module. Only each symbol's own definition and the code it
alone used matched; no MCP tool, scheduler, capacity, CLI handler, web route,
script, benchmark-rig path or test referred to any of them. Where a name-based
scan cannot decide, the function had no importer outside its own module, so no
dynamic dispatch could reach it.

Not in the authorized area, and left alone: the two live wording heuristics
(review verdict prose classification in `review.py`, title guessing in
`run_titles.py`) and the check-protection path (H32). The two integration record
builders in `planning.py` belong to H2.

## 2. What was deleted

| Area | Removed | Lines |
| --- | --- | --- |
| `review_policy.py` | `ReviewRoundState`, `initial_review_round_state`, `next_review_round`, `final_repair_allowed`, the JSON codec (`review_round_state_to_json`, `review_round_state_from_json`), their field list and the `_POLICY_VERDICT_ALIASES` map only the codec read | 167 |
| `runtime.py` | `ProjectRuntimeManager.list_inbox_stores`, `list_known_runtimes`, and the private support only they used (`InboxStore`, `_owner_db_resolved_path`) | 158 |
| `supervisor.py` | `RunSupervisor.observe_long_worker` | 81 |
| `db.py` | `Database.mark_worker_stalled`, `Database.update_attempt_trace` | 67 |
| `agent_sessions.py` | `legacy_session` (and the `datetime` import only it used) | 35 |
| `scripts/benchmark_core.py` | `first_blocked_candidate_sha` (the rig moved out of the package in H11) | 26 |

Total: **536 lines deleted, 2 changed** (the `review_policy` module docstring no
longer advertises the round machine). Source (`src/saberops`) shrank by 508
lines; the rig script by 26. No test exercised any of the removed symbols by
name, so no test was deleted with them; the new guard adds 56 test lines.

What stays, deliberately:

- `assess_worker_health` in `supervisor.py` is still exported in `__all__` and
  is now called by nothing; it is outside this lane's authorized area and stays
  for H31's second pass.
- `RepairScope`, `ReviewProtocolError`, `HealthState`, `ManagerState` and the
  supervision/attempt-trace readers are still used by live code.
- The `worker_stalled` / `worker_resumed` run events are still produced by
  `orchestrator.py` and consumed by `jobs/service.py` and
  `observability/run_report.py`; only the uncalled second writer is gone.

## 3. Decision

1. Delete the functions above with their private support, as listed.
2. Guard the cut with `tests/test_h21_dead_code_sweep.py`: the attributes and
   the rig function must be absent. Every one of its four checks failed on the
   base commit and passes after the deletion.
3. Correct the two statements the cut made wrong: the `review_policy` module
   docstring no longer claims a bounded review-round machine, and the
   `agent_sessions` import list no longer carries an unused module.

## 4. Consequences

- The bounded review-round state machine is not persisted anywhere, so a store
  written before this change needs no migration and none is offered.
- `ProjectRuntimeManager` now resolves runtimes and shuts them down; the
  cross-project read-only enumeration it also offered had no caller since the
  Inbox was rebuilt on `for_run`.
- A later change that reintroduces one of these names without a caller fails
  `tests/test_h21_dead_code_sweep.py`, and H31's repository-wide scan reports
  it anyway.
