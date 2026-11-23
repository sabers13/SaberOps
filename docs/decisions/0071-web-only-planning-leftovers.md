# ADR-0071: Delete the web-only planning leftovers (wave optimizer, project modes)

- **Status:** Accepted. Phase 1.1 lane H10.
- **Date:** 2026-11-23
- **Decider:** Saber (owner), recorded answers 2026-11-22 (`Q9` yes, "maximise
  the deletion") and 2026-11-23 (keep the phase 1 parallel style; deletions
  that cannot be confirmed drop out)
- **Related:** [ADR-0005](0005-mcp-first-engine-and-phase-2-board.md) (the
  planning CLI is the orchestrator), [ADR-0080](0080-plan-scheduling-and-tool-registry.md)
  (left the three wave records here for this lane), [ADR-0082](0082-owner-mode-carries-authority.md)
  (owner modes replaced the project entry modes), [ADR-0076](0076-trim-web-to-phase-2-pages.md)
  (H23 retired the `/projects` page this lane's modules fed)

## 1. Context

Phase 1 deleted the wave executor. Four modules survived because a web page or
the executor's admission seam still imported them:

- `wave_plan_optimizer.py` (1,519 lines) — pure DAG scoring that turned a stored
  plan revision into rounds, a critical path, a makespan and a parallelism
  figure for the `/projects` "Plan preview" panel.
- `project_mode.py` (1,618 lines) — the six project *entry* modes (new project,
  inspect, task, develop existing, continue, resume), their fact extraction,
  recommendation, preflight blockers and roadmap export, shown on the same page.
- `wave_models.py` (751 lines) and `wave_admission.py` (234 lines) — wave/lane
  records and the deterministic admission decision behind
  `Orchestrator.admit_wave`.

The caller check on local `main` (`7384d36` or a descendant) found:

- **Web:** `web.py` imported both big modules for the `/projects` panels only.
  H23 (ADR-0076) later retired the whole page; this lane started before or
  beside it and removes the modules either way.
- **Scheduler/CLI/MCP:** no caller. `Orchestrator.admit_wave` existed but no
  module under `src/` called it; only `tests/test_wave_admission.py` did.
- **`ready_frontier.py`:** `ScheduledTask`, `ScheduledWave` and
  `WaveScheduleError` had no remaining consumer except the optimizer and its
  test (H19 deleted the scheduler; ADR-0080 left the three records for H10).
- **Scripts and the benchmark rig:** no reference.
- **Tests:** the five modules' own tests plus two UI assertions on `/projects`.

Owner mode (ADR-0082) already replaced the authority presets, and the phase 2
plan (`phase-2-ui-plan.md`) names no use for entry modes or wave scoring: its
board reads plan lanes and `STATE.md`, not the optimizer. So the "unless phase 2
names a use" condition does not save anything here.

## 2. Decision

1. **Delete the four modules and their tests.** `wave_plan_optimizer.py`,
   `project_mode.py`, `wave_models.py`, `wave_admission.py` go together with
   `tests/test_wave_plan_optimizer.py`, `tests/test_project_mode.py`,
   `tests/test_wave_models.py`, `tests/test_wave_admission.py` and
   `tests/test_wave_recovery.py` (the last exercised only the wave model's
   `LanePreemption` record; the live `preemption.py` has its own tests).
2. **Delete the seam and the leftovers.** Remove `Orchestrator.admit_wave` and
   its imports, and the now-uncalled `ScheduledTask`, `ScheduledWave` and
   `WaveScheduleError` in `ready_frontier.py`.
3. **The web panels go with their page.** During the lane the `/projects`
   entry-mode recommendation and "Plan preview" sections, their context keys and
   the `wave_project` query parameter were removed. H23 (ADR-0076) then retired
   the whole page in parallel, so the rebased branch carries H23's removal; no
   route, template, context key or query parameter referring to either module
   remains in `web.py`.
4. **Docs follow the code.** Delete `docs/project-modes.md` and
   `docs/wave-plan-optimization.md`, drop their README links. H23's rewrite of
   `docs/first-run-web-ui.md` carries the page-side description.
5. **No guarantee moves.** Nothing here is on the safety list (quota, billing
   consent, isolation, checks, independent review, owner acceptance, pause).
   No protected check file, review evidence or safety drill is involved.

## 3. Consequences

- The four module bodies remove 4,122 source lines and their five suites remove
  2,458 test lines; with the orchestrator seam, the three ready-frontier records
  and the new guard test, the branch nets **4,186 source** and **2,421 test**
  lines removed. On the rebased tree (`main` `ae365c8`) that leaves
  `src/**/*.py` at 138,565 lines and `tests/**/*.py` at 98,603.
- No planner, owner or scheduler path loses a capability: the deleted panels
  were read-only web views, and the admission seam had no live caller.
- H23's `test_h23_retired_web_routes_are_gone` now guards the retired route set,
  so re-adding a plan/wave or entry-mode page without a new decision fails the
  web suite.

## 4. References

- Phase 1.1 plan, lane H10 (`phase-1.1-core-plan.md`), owner decision Q9
- `src/saberops/orchestrator.py`, `src/saberops/ready_frontier.py`
- `tests/test_ready_frontier.py`
- `docs/first-run-web-ui.md`, `README.md`, ADR-0076
