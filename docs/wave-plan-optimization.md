# Wave-plan scoring and optimization (Step 027 / WP-1)

At plan initiation the Orchestrator proposes a task graph — dependencies,
file scopes, S/M/L size, checklist counts — and a wave grouping. This module
scores that proposal and revises it deterministically, so wave numbers and
concurrency decisions come from software instead of hand-written labels.

`saberops.wave_plan_optimizer` is pure: no database, repository, network,
clock, provider or model call. It never launches, admits or merges anything;
it produces a scored plan and its revision history for the Orchestrator (and,
later, the owner) to confirm.

| Concern | Where |
|---|---|
| Graph model, limits, results | `PlanNode`, `TaskGraph`, `OptimizationLimits`, `PlanScore`, `OptimizationResult` |
| Scoring | `score_plan`, `compute_critical_path`, `detect_scope_overlaps` |
| Wave computation | `derive_waves` |
| Iterative revision | `optimize_plan`, `remove_redundant_dependencies` |
| Wave-boundary re-planning | `wave_boundary_replan` |
| Integration with PL-1 / WX-1 | `graph_from_projects`, `graph_from_revisions`, `to_scheduled_waves`, `from_scheduled_waves` |

## The planning model

A `PlanNode` is one proposed task with a stable identity `(project_id,
task_id)`:

- `size` — `S`, `M` or `L`; sized by the capacity cost model.
- `start_dependencies` — hard START edges; every target must exist in the graph.
- `integration_prerequisites` — deferred edges that never block a start.
- `owns` — declared write scopes (repo-relative paths; a trailing `/**` is a
  subtree). Unsafe scopes (absolute, `..`, backslash, embedded glob) fail
  closed.
- `barrier` — a synchronization task (see below).
- `requires_heavy_gate` — consumes one heavy-gate slot while it runs.
- `checklist_items` — the task's checklist size, carried for the plan report.
- `duration` / `integration_duration` — optional explicit overrides.

`TaskGraph` is immutable. `validate_graph` canonicalizes and validates it
fail-closed: duplicate or blank identities, unknown START targets, self-edges,
cycles, unsafe scopes, bad sizes and negative counters all raise
`WavePlanError`.

`OptimizationLimits` defaults mirror the owner policy: 3 active projects x 5
workers per project, 2 heavy-gate slots (Step 013 / CFG-1 default), a 5-hour
cost window, a fixed per-task overhead of 1.0, a barrier-integration default of
1.0, at most 3 revision rounds and a 10 % stop threshold.

## Scoring formulas

All durations are abstract planning units, not wall-clock minutes. One unit per
cost percent is the default (`units_per_cost_pct = 1.0`), so a task's work is
exactly the capacity cost model's prior:

```
duration(task) = prior_pct(window_id, size) * units_per_cost_pct
```

`prior_pct` is `saberops.capacity.cost_model.prior_pct`, whose packaged 5-hour
priors are `S = 3`, `M = 8`, `L = 15`. An explicit `duration` override wins.

**Critical path** — the longest dependency chain by task weight, computed by
topological DP:

```
weight(t)  = duration(t) + integration_duration(t)
cp(t)      = weight(t) + max(cp(dep) for dep in t.start_dependencies, default 0)
critical   = max(cp(t))
```

**Total work** — `sum(duration(t))`.

**Parallelism** — `total_work / critical` (0.0 when the critical path is zero).
`1.0` means the plan is a single chain; higher means independent work exists.

**Makespan** — the finish time of the resource-constrained list schedule
described under *Makespan estimation*.

**Scope overlap** — every pair of tasks placed in the same wave whose declared
write scopes overlap (exact match, or one path inside the other's `/**`
subtree). Two scopes overlap when either path is owned by the other:

```
path_owned(path, scope) = path == scope
                          or (scope.endswith("/**") and path.startswith(scope[:-2]))
```

**Resource demand** — from a sweep over the scheduled intervals:

- `peak_workers` — maximum concurrently running tasks across all projects;
- `peak_workers_per_project` — the same per project (bounded by the cap);
- `peak_heavy_gate_slots` — maximum concurrent heavy-gate tasks;
- `peak_provider_pct` — the largest sum of concurrently running tasks'
  provider percents;
- `total_provider_pct` — the plan's cumulative provider demand.

**Fixed cost per task** — `fixed_cost_per_task * task_count`, plus
`total_checklist_items`, reported alongside the timing score.

## Makespan estimation

`score_plan` builds a schedule with a deterministic parallel list scheduler:

1. Every task is scheduled only after all of its `start_dependencies` are
   assigned; its earliest start is the latest dependency finish.
2. A task occupies one worker slot of its own project for its whole duration.
   A project has `workers_per_project` slots; the earliest free slot is used.
3. When `requires_heavy_gate` is set and gate enforcement is on, the task also
   occupies one of the `heavy_gate_slots` gate slots.
4. When scope-conflict serialization is on, a task is pushed past any already
   scheduled task whose interval it would overlap and whose scopes conflict.
5. Ready tasks are ordered by earliest start, then longest duration first, then
   stable identity, so results are deterministic.

`chained=True` models today's chained-wave baseline (mirroring
`ready_frontier.schedule_chained_waves`): every task in wave *n* also waits for
the whole of its predecessor wave. `chained=False` is dependency-driven start —
a task begins as soon as its own dependencies finish, even while unrelated
tasks from an earlier wave are still running.

Barriers and pending dependencies are always honored in both modes.

## Barriers

A **barrier** is a task marked `barrier=True` that fences the plan: every task
that is *not* one of the barrier's transitive dependencies (and is not the
barrier itself) is placed in a strictly later wave and may not start until the
barrier is fully integrated. A barrier's own prerequisites run before it and
are never held.

Wave derivation enforces this at a monotone fixpoint: it packs waves, derives
each barrier's fence floor for all non-ancestor tasks, re-packs with those
floors as minima, and repeats until no floor rises (bounded by
`len(index) + 2`). Held tasks start no earlier than the barrier's finish plus
its integration duration, which defaults to `barrier_integration_units` (1.0)
and can be overridden per task. Non-barrier tasks charge no integration time.

A barrier with no dependencies therefore fences every other task — it behaves
as a release gate. Declare the work that must precede a barrier as its
`start_dependencies`.

## Optimization heuristics

`optimize_plan` scores the proposal as a chained-wave baseline, then revises it
in bounded rounds:

1. **Eliminate redundant transitive dependencies** —
   `remove_redundant_dependencies` drops a direct edge `t -> d` when another
   direct edge `t -> d2` already reaches `d` transitively. Reachability,
   start times and the critical path are unchanged.
2. **Dependency-driven reflow** — re-plan from chained waves to per-task
   dependency-driven start, which is how execution actually begins each task
   when its own dependencies finish.
3. **Balance concurrency** — pack tasks into the earliest wave that respects
   dependencies, per-project worker caps, the active-project cap and scope
   conflicts.
4. **Merge underfilled waves** — coalesce adjacent waves when caps, scopes and
   dependencies permit and neither wave holds a barrier.

Each round re-scores the best candidate and stops when the fractional makespan
gain `(previous - current) / previous` falls below `min_gain` (default 0.10) or
after `max_rounds` (default 3). `OptimizationResult` records the initial and
final scores, the per-round reports, the removed dependencies, the baseline
waves and the computed waves, plus `stopped_reason`
(`min_gain` / `max_rounds` / `no_tasks`).

### Computed wave numbers

Wave numbers are never read from the proposal. `derive_waves` produces integer
indices `0..n-1` such that:

- every task's wave is strictly greater than each START dependency's wave;
- a barrier's non-ancestor tasks are strictly later than the barrier;
- no wave holds two same-project tasks with overlapping scopes;
- each wave holds at most `workers_per_project` tasks per project and at most
  `active_projects` distinct projects.

`OptimizationResult.computed_waves` is the authoritative display grouping;
`baseline_waves` preserves the proposal for comparison.

### Execution invariants

- **Dependency-driven start** — inside an active wave, a task is dispatch-ready
  the moment its own START dependencies complete; it does not wait for the rest
  of its wave. `dispatch_order` returns the schedule in earliest-start order.
- **Barrier hold** — no task after a barrier starts before the barrier is
  integrated.
- **Wave-boundary re-planning** — `wave_boundary_replan` treats tasks at or
  before a boundary wave as started, drops their edges from the remaining
  subgraph, re-optimizes the unstarted tasks and numbers the resulting waves
  continuing after the boundary.

## Integration with the plan store and ready frontier

The engine is consumable without changing either authority:

- `graph_from_projects(projects, metadata=...)` adapts
  `ready_frontier.ProjectPlan` rows; coordination records (`plan_wrapper`,
  `wave_wrapper`, `orchestrator_turn`) never become planning nodes.
- `graph_from_revisions(revisions, metadata=...)` adapts
  `project_scope.plan_store.PlanRevision` tasks, resolving `other-project/task`
  edges cross-project.
- `to_scheduled_waves(graph, result)` renders computed waves as
  `ready_frontier.ScheduledWave` objects; `from_scheduled_waves` converts them
  back.

`NodeMetadata` carries size, barrier, gate, checklist and duration overrides,
because the plan store does not persist them yet — persisting that metadata in
plan revisions is follow-up work, not part of this step.

## Testing

`tests/test_wave_plan_optimizer.py` is hermetic and covers graph scoring,
makespan bounds, per-project worker and gate-slot limits, barrier enforcement,
scope-overlap detection and serialization, redundant-edge removal, the
three-round convergence cap, deterministic wave computation and the plan-store /
ready-frontier adapters. Every test carries a `Covers:` docstring tag.
