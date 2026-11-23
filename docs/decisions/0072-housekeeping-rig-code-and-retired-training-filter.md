# ADR-0072: Housekeeping — the rig ships outside the package; the retired training filter is gone

- **Status:** Accepted. Phase 1.1 lane H11.
- **Date:** 2026-11-23
- **Decider:** Saber (owner)
- **Related:** [ADR-0012](0012-benchmark-rig-modes.md),
  [ADR-0061](0061-drill-results-table-and-baseline.md)

## 1. Context

Phase 1.1's housekeeping lane removes retired compatibility code that its own
caller check confirms is dead, and moves test-rig code out of the installed
package. The starting point was `main` at `f2976a4` (source 156,126 lines,
tests 104,164 lines).

Two candidates existed:

1. `src/saberops/benchmark.py` (2,801 lines) is the shared phase-1 benchmark
   rig. No module under `src/` imports it: only `scripts/benchmark.py` (the
   benchmark entry point) and test modules do.
2. `src/saberops/training_policy.py` (130 lines) is a retired compatibility
   module whose admission decision (`is_training_permitted`) always returns
   `True`; `training_excluded` is therefore always `False`. Its helper
   functions only read historical evidence. `preflight.evaluate_control_plane`
   and `capacity.admission.admit` still called the filter and kept a
   `TRAINING_PERMISSION_DENIED` branch that could never run.
   `project_settings.is_project_positively_public`,
   `project_settings.resolve_effective_data_policy`,
   `project_settings.resolve_effective_training_allowed`, and
   `run_creation.resolve_effective_training_allowed` (plus the private
   `_resolve_training_from_settings`) were always-constant compatibility
   seams over the same retired policy.

`src/saberops/vendor_cli_fixtures.py` was checked and **not** moved: production
code imports it (`vendor_health.py`, `cli.py`, `preflight.py`,
`self_update.py`), so the lane's "only tests and the rig use it" condition does
not hold.

## 2. Decision

1. **The rig core moves out of the package.** `src/saberops/benchmark.py`
   becomes `scripts/benchmark_core.py`. `scripts/benchmark.py` and the test
   modules import `benchmark_core`; `pyproject.toml` already puts `scripts` on
   pytest's `pythonpath` and mypy's `mypy_path`, and already installs only the
   explicitly listed `gate_tools` top-level module from that directory. The
   setuptools package discovery (`where = ["src"]`) therefore no longer ships
   the rig. The fake-mode benchmark behaviour is unchanged.
2. **The retired training filter is deleted, not re-implemented.** Delete
   `training_policy.py` and the dead `TRAINING_PERMISSION_DENIED` branch in
   `preflight.evaluate_control_plane`, `capacity.admission.admit`, the
   `ControlPlaneDecision.training_permitted` field and its three copies in
   `dispatch.control_plane_decision`, the `SKIP_TRAINING_POLICY` mapping, and
   the always-constant resolver seams in `project_settings`/`run_creation`
   (the orchestrator-binding translation branch went with H3's deletion of
   `control_plane/orch_binding.py`, which landed in parallel).
3. **The persisted evidence stays.** A Run's frozen `training_allowed` bit and
   a candidate's `training_required` classification remain stored and carried
   for old snapshots, and the owner-facing project data-policy setting is
   unchanged (expand, migrate, contract). No admission path consults them any
   more; a legacy Denied flag cannot restrict a candidate.
4. **The guarantee does not weaken.** Removing an always-permit filter changes
   no admission outcome, and the safety list (quota, billing consent,
   isolation, checks, independent review, owner acceptance, pause) is not
   touched. Nothing in the check-protection path is involved.

## 3. Consequences

- The installed package no longer ships the benchmark rig; a regression guard
  (`test_rig_core_ships_outside_the_installed_package`) fails if a
  `saberops.benchmark` module returns under `src/`.
- The training/data-policy vocabulary remains readable as history, but there
  is no code left that could re-enable it by accident without a new decision.
- Measured reduction for this change: 2,801 lines of rig code leave the
  installed package into `scripts/benchmark_core.py`, and the source deletions
  below that move remove 235 net lines from `src/saberops` (130 of them
  `training_policy.py`); the test suite shrinks by 803 net lines (826 of them
  `tests/test_training_policy.py`). Remaining dead-data-policy plumbing
  (`training_allowed` parameters and the run column) is historical evidence,
  not a filter, and is left for the phase-1.1 dead-code pass (H21/H31).
- The fake-mode benchmark still runs end to end (`scripts/benchmark.py
  --mode fake`): ten tasks, PASS.

## 4. References

- Phase 1.1 plan, lane H11 (`phase-1.1-core-plan.md`)
- `scripts/benchmark_core.py`, `scripts/benchmark.py`
- `src/saberops/control_plane/preflight.py` (`evaluate_control_plane`)
- `src/saberops/capacity/admission.py` (`admit`)
- `src/saberops/project_settings.py`, `src/saberops/run_creation.py`
- `tests/test_benchmark_rig.py`, `tests/test_routing_config.py`
