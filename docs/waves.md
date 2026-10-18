# Waves, lanes and the wave executor (Step 010 / WX-1)

Deterministic wave execution for the SaberOps engine: the wave and lane models,
fail-closed admission, isolated worktrees, sequential pinned-SHA merges, the
wave/lane state machines, the 3 projects x 5 child-task concurrency policy and
the accepted-wave cleanup event consumed by Step 008 storage GC. It implements
ADR-0004 §3 D2 and D4, §4 invariants 2, 3, 4 and 10, Amendment 3 (3x5), and
Amendment 4 (cleanup eligibility).

The module split mirrors the decision split: the engine does everything
deterministic, models only supply lane code, a conflict resolution or named
glue.

| Module | Responsibility |
|---|---|
| `saberops.wave_models` | `Wave`, `Lane`, `WaveState`, `LaneState`, the two transition tables and (de)serialization |
| `saberops.wave_admission` | the pure, fail-closed `admit_wave` decision |
| `saberops.wave_executor` | worktrees, merges, one G3, state machine driving, cleanup emissions and the 3x5 wiring |
| `saberops.orchestrator` | `admit_wave` / `execute_wave` entry points |

## The wave model

A **wave** is `base_sha`, `lanes`, `merge_order`, `named_glue`, `budgets` and
`target_branch`. A **lane** is a `prompt`, its allowed paths (`touches`), its
`must_not_touch` paths, the `contracts` it participates in, a
`retirement_table`, the pinned `worker_route` and `reviewer_route` and its
`effort`.

Both models are immutable and validate on construction. A malformed wave (an
empty lane set, a duplicate lane id, a `merge_order` that is not a permutation
of the lane ids, a repeated or non-JSON budget value, a scope pattern that
escapes the repository) raises `WaveModelError` before anything is planned --
never a half-built wave.

`Wave.as_dict` / `from_dict` round-trip through JSON, and
`Wave.as_sqlite_row` / `from_sqlite_row` produce one row of primitive or
JSON-text columns. Every parse path validates fail-closed.

### Write-scope disjointness

Lane `touches` are path-prefix scopes: `src/saberops` owns that file and every
path below it. Two lanes may not overlap, and a lane may not list a path in both
`touches` and `must_not_touch`. Overlap is what would let two Workers edit the
same file in parallel, so it is rejected at admission rather than discovered at
merge time.

## Admission

`admit_wave(wave, target_head, posture_validator, *, repo=None,
ancestry_checker=None)` returns a `WaveAdmissionDecision(admitted, reasons)`.
Every independent check runs and every failure is reported as a stable
`code:detail` reason; `admitted` is true exactly when `reasons` is empty.

1. **Structural validity** of the wave (`invalid_wave:*`).
2. **`target_head` shape** -- a full lowercase hex commit SHA
   (`invalid_target_head`).
3. **Disjoint lane Touches**, pairwise and in declared order
   (`overlapping_touches:left:right:left_pattern:right_pattern`).
4. **No lane self-conflict** between its own Touches and must-not-touch
   (`lane_self_overlap:lane`).
5. **Route posture** for every lane's worker and reviewer route
   (`invalid_worker_route` / `invalid_reviewer_route`).
6. **Ancestry**: `base_sha` must be an ancestor of `target_head`
   (`base_not_ancestor`), checked with real `git merge-base` in `repo`, or by
   an injected `ancestry_checker`. With neither, ancestry is unproven and the
   wave is refused (`ancestry_unverified`): the engine never assumes a base
   still applies.

Nothing is spawned, no ref moves and no worktree is created by admission.

## State machines

```
wave:  PLANNED -> RUNNING -> INTEGRATING -> GATED -> AWAITING_ACCEPT -> ACCEPTED
                    \_____________/            \____________/
                          BLOCKED                  CANCELLED
lane:  PLANNED -> RUNNING -> REVIEW -> FIX_UP_K -> RUNNING -> REVIEW
                                     -> REVIEWED_PROVISIONAL -> ACCEPTED
```

`ACCEPTED` and `CANCELLED` are terminal for a wave; `ACCEPTED` is terminal for a
lane. `BLOCKED` is a transient state that resumes into `RUNNING` (a cleared
prerequisite, a resolved conflict, a fixed environment). The transitions live in
`WAVE_ALLOWED_TRANSITIONS` / `LANE_ALLOWED_TRANSITIONS` and are applied only
through `transition_wave` / `transition_lane`, which raise on any illegal move.
The executor therefore cannot skip `GATED`, accept an ungated wave, or leave a
terminal state by accident.

A blocked lane does not block the wave by itself: one lane failing is routed,
never a wave failure (ADR-0004 invariant 4).

## Integration

`WaveExecutor.integrate()` merges the pinned lane head SHAs **sequentially in
`merge_order`**, each with `git merge --no-ff`, into an isolated integration
worktree created from `base_sha`. Before each merge the engine re-resolves the
lane's branch tip and refuses to merge when it no longer equals the pinned
reviewed SHA: a moved lane head blocks the merge, exactly as the manual
workflow's pinned-base rule requires.

* A **clean** run merges every lane and the wave stays `INTEGRATING` with no
  outstanding work; `integration_head` is the integrated SHA.
* A **conflict** stops the sequence at the conflicted lane, keeps the wave
  `INTEGRATING`, and returns `requires_integrator=True` with
  `integrator_role="INTEGRATE"`, the conflicted paths and the remaining lanes.
  The engine never auto-resolves a conflict.
* **Named glue** items likewise require an `INTEGRATE` dispatch before G3. The
  Integrator records the resolution (`record_integrator_resolution`) and the
  written glue (`record_glue_written`); the engine never marks them done itself.
* A **moved head** or a **missing pinned SHA** blocks the wave (`BLOCKED`).
  `WaveExecutor.resume()` clears the block once the cause is fixed.

Budgets are enforced fail-closed and are never raised silently: more than
`fix_rounds` (default 2) fix rounds in one lane, or more than `g3_runs`
(default 4) G3 runs in one wave, raises `WaveBudgetExceeded` so the
Orchestrator raises an owner question instead of running again.

## One gate per wave

After every lane is merged and any named glue is attested, `run_g3()` runs
**exactly one** G3 on the exact integrated head through the GS-2
`EngineGateExecutor` under a heavy gate slot. A pass moves the wave to `GATED`;
a failure moves it to `BLOCKED` with the gate's outcome recorded. An
unaccepted integration branch is never reported as closed.

Once gated, `await_acceptance()` moves the wave to `AWAITING_ACCEPT` and reports
it to the Orchestrator. The engine never moves `main` and never pushes.

## Concurrency: 3 projects x 5 child tasks

The owner-approved limits are enforced by `saberops.project_slot_policy` (at
most 3 concurrently active projects and at most 5 executing child tasks per
project, two independent constraints -- the derived ceiling of 15 is
observability only, never the admission test).

The executor wires the wave model into that policy:

* the wave itself is a `WAVE_WRAPPER` record and never occupies a child slot;
* each lane is one `CHILD_TASK` record whose occupancy follows the lane state:
  `RUNNING` and `FIX_UP_K` occupy a slot; `PLANNED`, `REVIEW`,
  `REVIEWED_PROVISIONAL` and `BLOCKED` do not; `ACCEPTED` is terminal;
* `admit_child_lane` presents the lane as the `RESERVED` slot it would claim and
  uses the wave's own records (minus the candidate) as the snapshot, so a wave
  parent plus five running children leaves a sixth child over the limit
  (`1 x 6`), a fourth active project is refused (`4 x 1`), and the exact `3 x 5`
  boundary refuses one more child with `project_limit_reached` or
  `project_slot_limit_reached`.

These are **ceilings, never targets** (ADR-0004 Amendment 7). Reaching 3x5 is
not a goal; a wave that needs one Worker runs one Worker.

The Step 015 project registry is an optional seam. `resolve_project_id` prefers
a configured registry, then an explicit id, then the repository's derived
project identity, and fails closed when none is available. No configuration
error falls back to unlimited capacity.

The decision is a pure projection. It is not launch authority on its own: a real
launch must wrap it in the transactional caller described in
`saberops.project_slot_policy` (owner-DB lease, completeness/freshness proof,
atomic reservation). Until that caller exists this module reports intent.

## Accepted-wave cleanup eligibility

After independent review, a G3 pass, Orchestrator acceptance and the
owner-authorized fast-forward of the target branch, `accept(accepted_sha)`:

* requires `accepted_sha` to equal the exact gated wave head -- the engine never
  reports closure for a SHA it did not gate;
* moves the wave to `ACCEPTED`;
* emits `wave_integrated_and_closed`.

The event payload carries the accepted SHA, the exact child head SHAs, evidence
locations, any retained recovery refs, and the list of engine-owned local Git
objects (lane worktrees and branches, the integration worktree and branch) that
may be *considered* for cleanup, each with a reason.

The executor **does not delete anything**. A child wave or branch still
referenced by a live plan (`live_plan_refs`) is held back: it is reported as
`BLOCKED` with a `held_by_live_plan:` reason and excluded from the eligible
objects, so the overall status becomes `BLOCKED`; otherwise it is
`ELIGIBLE_CLEANUP` (or `RETAINED` when nothing is offered). Acceptance keeps the
wave `ACCEPTED` regardless of the cleanup outcome.

Step 008 owns the idempotent eligibility plan, the safe worktree/branch
retirement and storage GC; Step 013 governs persistent policy and audit; Step 019
exposes dry-run inventory and approval. A deleted local branch is never a deleted
commit, evidence or remote branch.

## Orchestrator seam

`Orchestrator.admit_wave(wave, target_head, posture_validator)` delegates to the
pure admission decision, and `Orchestrator.execute_wave(wave, repo=...)` builds a
`WaveExecutor` bound to the orchestrator's worktree base and its GS-2 engine gate
executor, so a wave's single G3 runs through the same heavy-slot machinery as
every other engine gate. Construction allocates nothing; `WaveExecutor.start()`
is the first mutating call.

## What this step does not do

* It does not run a model. Routing rules, attribution, budgets as owner
  questions and same-session repair resume are WX-2.
* It does not delete worktrees, branches or objects; it emits eligibility for
  Step 008.
* It does not move `main`, push, or create the plan/registry objects owned by
  PL-1 and Step 015.
* It does not supersede `parallel_work.execute_team` at runtime yet; the ADR
  retirement table and the caller switch belong to the integration that adopts
  the wave executor.
