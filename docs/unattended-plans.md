# Unattended plan execution (RD-1, Step 017)

This document describes `saberops.unattended`, the pure policy module that answers
the questions an overnight run must answer without the owner awake:

* may the run start at all (deterministic preflight);
* who may decide a given decision (the frozen decision sheet, an autonomous
  class, or the owner);
* what can never be decided autonomously (the never-delegable list);
* when the plan must stop (the circuit breaker after 3 consecutive failures);
* when the run must shout (the no-silent-stall alarm);
* what the owner reads in the morning (the morning report).

It encodes ADR-0004 **D14** (overnight readiness) and the roadmap's **RD-1**
acceptance criteria. It is an **advice-and-record projection only**: the runtime
still owns the plan, the wave executor, the scheduler, the notification
transport, the owner-question queue, the audit store and the candidate's git
operation. Nothing in this module reads or writes the filesystem, network,
clock, environment, database or a model provider, and nothing here can move
`main` or push.

| Module surface | Responsibility |
|---|---|
| `unattended.DecisionScope` | the exact project/task/path scope a decision belongs to |
| `unattended.StandingDecision` | one bounded, scoped, expiring owner answer |
| `unattended.DecisionSheet` | the owner-frozen set of standing decisions for one run |
| `unattended.NeverDelegableKind` / `NEVER_DELEGABLE_KINDS` | the immutable never-delegable list |
| `unattended.resolve_decision` | the deterministic disposition for one decision |
| `unattended.build_audit_entries` | the Orchestrator-alone audit list |
| `unattended.run_preflight` | budget / wall-clock / scope / cap / channel readiness |
| `unattended.RunProgress` | the 3-consecutive-failure circuit breaker |
| `unattended.detect_stall` | the loud no-silent-stall alarm |
| `unattended.compute_plan_impact` | what a park holds and what keeps running |
| `unattended.build_morning_report` | the owner's morning summary |
| `tests/test_unattended.py` | hermetic invariant and adversarial contract tests |

## Scope boundary

In scope:

* exact-scope, exact-key standing decisions with absolute and turn-based expiry;
* the immutable never-delegable list, enforced in software;
* the three autonomous decision classes and their strict precedence;
* the deterministic preflight and its blockers and warnings;
* the consecutive-failure circuit breaker;
* the loud stall alarm with configurable channels;
* the plan-impact projection for a park or an escalation;
* the morning report and the Orchestrator-alone audit list.

Deliberately out of scope (owned elsewhere):

* the durable plan store, plan registration and resume briefing (PL-1);
* wave execution, admission, merges and gate scheduling (WX-1/GS-2);
* the durable question queue and its owner answers (Q-1);
* the actual Telegram/log transport (TG-1) and the audit store;
* the Orchestrator turn and brief machinery (`orchestrator_turn_policy`, OT-1);
* the git operation that performs a local commit, and any push (owner-only);
* wiring into run creation and the run-all loop.

A passing unit test here proves the policy contract. It does not prove that a
real CLI session resumes, that a real alarm arrives, or that a real plan ran
overnight.

## Decision sheet and standing decisions

A run freezes a `DecisionSheet`: the run id, a plan reference, and a bounded
list of `StandingDecision` answers. Each answer is:

| Field | Contract |
|---|---|
| `standing_decision_id` | unique id for the answer |
| `scope` | the exact `DecisionScope` it applies to |
| `decision_key` | the exact predictable decision it answers |
| `owner_words` | the owner's **exact words**, recorded verbatim, never summarised |
| `answered_by` | must be `"owner"`; a model or Orchestrator answer is refused |
| `expires_at` | optional absolute, timezone-aware ISO-8601 instant |
| `granted_at_turn` / `ttl_turns` | optional turn window; `ttl_turns >= 1` |

### Exact-scope matching, never by analogy

`DecisionScope` is exactly `(project_id, task_id, paths)`. It matches **only**
field-for-field. There is no prefix match, no parent-directory match, no glob and
no wildcard: wildcard and expansion characters (`* ? [ ] { } ! $`) are rejected
wherever they appear, so a scope that could match by pattern cannot even be
constructed. `"src"` and `"src/a"` are different scopes, and `"src/a.py"` never
matches `"src/a/b.py"`. Path order and duplicates are canonicalised (sorted,
de-duplicated) because order carries no meaning; that is canonicalisation of
representation, not a relaxation of the match. A standing decision also has to
match the decision **key**, so two predictable decisions in one scope can hold
two different answers without ambiguity; a duplicate `(scope, key)` pair is
refused.

### Expiration and lifecycle

Every standing decision is bounded. At least one of `expires_at` or
`ttl_turns` is mandatory, so no standing permission is unbounded. The absolute
bound is live **strictly before** its instant and expires **at** it. The turn
bound is live for `ttl_turns` turns starting at `granted_at_turn` and expires
once `current_turn >= granted_at_turn + ttl_turns`. `ttl_turns` without
`granted_at_turn`, a zero `ttl_turns`, and a naive (timezone-less) instant are
all refused, because none of them can be evaluated exactly.

`resolve_decision` evaluates expiry with the caller's explicit `now` and
`current_turn`; it never reads a clock. An expired answer is not applied: the
decision falls through to the autonomous classes or to the owner, and the
expiry is recorded in the explanation.

## Never-delegable policy

The list is immutable software data, not a comment: `NEVER_DELEGABLE_KINDS` is a
tuple and `NEVER_DELEGABLE_POLICY` is a read-only mapping proxy.

| Kind | Never delegable because it would… |
|---|---|
| `test_gate` | disable or drop the test gate, or loosen its evidence |
| `data_training_policy` | change data or training policies, or their licenses |
| `roadmap_scope` | change roadmap scope or step boundaries |
| `budget_cap` | raise resource or financial budgets, or concurrency caps |
| `credentials` | manage credentials or secrets |
| `destructive_operations` | force push, delete repositories or drop databases |
| `publishing` | publish or release externally |

When a never-delegable decision arises, `resolve_decision` returns
`PARK_BRANCH` **before it consults the sheet, the acceptance facts or any
recommendation**, so neither a standing decision nor a green candidate nor a
recommended option can widen the list. The park applies to that decision's own
scope only; `compute_plan_impact` then holds that scope and its transitive
dependents while every unrelated task stays runnable. A single park never stalls
a plan.

## The three autonomous decision classes

`resolve_decision` uses a fixed precedence order, so the outcome never depends on
input order:

| # | Condition | Disposition | Class |
|---|---|---|---|
| 1 | the decision is never-delegable | `PARK_BRANCH` | 3 |
| 2 | a live standing decision matches the exact scope and key | `APPLY_STANDING_DECISION` | — |
| 3 | gate passed **and** cold review passed **and** checklist green **and** scope authorized **and** evidence exists | `AUTO_ACCEPT_LOCAL_COMMIT` | 1 |
| 4 | a recommended option exists with ≥ 1 rejected alternative **and** evidence | `TAKE_RECOMMENDED_OPTION` | 2 |
| 5 | anything else | `ESCALATE_OWNER` | 3 |

**Class 1 — gated acceptance.** Auto-acceptance is allowed only when the gate
passed, the cold review passed, the checklist is green, the candidate's scope
was authorized, and at least one evidence path was supplied. The only
acceptance action the module can express is a **local commit**;
`DecisionResolution.push_allowed` is always `false` and constructing a
resolution with `push_allowed=True` raises. There is no push disposition at all,
so an unattended run can never move a remote.

**Class 2 — the recommended option.** A decision that the sheet does not answer
and that is not never-delegable is settled by the Orchestrator's recommended
option, but only when the recommendation carries evidence **and** at least one
rejected alternative. Both are recorded in the Orchestrator-alone audit list.
A recommendation without an alternative or without evidence is not taken
alone; it escalates.

**Class 3 — park or escalate.** Never-delegable decisions park their own branch
(resolved at rule 1); every other unhandled decision escalates to the owner
(rule 5). The explanation names exactly why: no matching standing decision, an
expired one, unmet Class-1 conditions, an ungrounded recommendation, or a
non-autonomous authority mode.

### Authority modes

`FULL_AUTONOMY` applies rules 1–5. `SUPERVISED` applies only rule 1 (the
never-delegable list) and rule 2 (an explicit owner answer); every other
decision reaches the owner. No other mode delegates authority to the engine.

## The Orchestrator-alone audit list

`build_audit_entries(resolutions)` collects the resolutions that required an
audit — the Class-1 acceptances and the Class-2 recommendations — in the
caller's order. Each `AutonomousAuditEntry` records the decision, its scope, the
autonomy class, the chosen option, the rejected alternatives, the evidence paths
and the explanation. The caller persists the list; the module never writes it.

## Preflight

`run_preflight(request)` is the deterministic readiness check (no model) that
must pass before the run starts. A blocker prevents the start; a warning does
not.

| Blocker code | Raised when |
|---|---|
| `run_id_mismatch` | the frozen decision sheet belongs to another run |
| `budget_not_set` | the budget limit is not positive |
| `budget_exceeded` | spent units exceed the limit |
| `wall_clock_not_set` | the wall-clock limit is not positive |
| `wall_clock_exceeded` | elapsed seconds reach the limit |
| `no_authorized_scope` | fewer than one authorized file scope |
| `worker_cap_not_set` | the worker cap is not positive |
| `worker_cap_exceeded` | active workers exceed the cap |
| `no_stall_channel` | no alarm channel is configured |

Warnings fire at or above `PREFLIGHT_WARN_PERCENT` (80 %) of the budget or the
wall clock, and when the worker cap is already fully occupied. An open or
wildcard scope never reaches the preflight: `PreflightRequest` refuses it at
construction, so the refusal is fail-closed even earlier.

`PreflightResult.ready` is exactly "no blockers", and its `as_dict()` projection
is the readiness report feed.

## Circuit breaker

`RunProgress` tracks consecutive task failures. A pass resets the counter. The
counter reaching `CONSECUTIVE_FAILURE_LIMIT` (3) opens the circuit and **halts**
the plan, and halting is terminal: a later pass cannot silently resume a plan the
breaker stopped. `after_task(passed=...)` is a pure fold, so the transition is
reproducible from a persisted counter.

## No-silent-stall supervision

`StallObservation` carries the explicit supervisory facts: the last heartbeat,
`now`, the stall threshold, any stalled scopes, the configured channels and
whether the supervisor is alive. `channels` must be non-empty — a run that could
raise no alarm is a silent stall, and is refused at construction.

`detect_stall` returns `None` for a healthy run, otherwise a `StallAlarm`:

* an alarm (`ALARM`) when idle time reaches the threshold, when the supervisor
  is dead, or when any scope is stalled;
* a critical alarm (`CRITICAL`) when the supervisor is dead or idle time reaches
  `STALL_CRITICAL_MULTIPLIER` (2) times the threshold.

The alarm message is a bounded, single line (`MAX_ALARM_MESSAGE_LENGTH`) so it
can travel any channel — log, Telegram or the local web surface — without
carrying file content or credentials. The channels are always carried on the
alarm so the transport can fan out deterministically.

## Plan impact of a park

`compute_plan_impact(tasks, parked_scopes=..., escalated_scopes=...)` walks the
one-project task graph and returns which scopes are `parked`, which are
`escalated`, which are `held` (those, plus every transitive dependent) and which
stay `runnable`. `plan_stalled` is true only when something is held **and**
nothing remains runnable, so one never-delegable park is never a plan stall.
Duplicate tasks, unknown scopes, unknown dependencies, self-dependencies, cycles
and mixed projects all fail closed instead of guessing.

## Morning report

`build_morning_report` summarises the night for the owner:

| Section | Content |
|---|---|
| `standing_decision_actions` | each action taken under a standing decision, with the owner's verbatim words |
| `accepted_candidates` | each Class-1 acceptance, with its candidate, exact head SHA, `local_commit_only=True` and `pushed=False` |
| `parked_branches` | each never-delegable park, with its scope and kind |
| `escalations` | each unhandled decision that reached the owner, with the reason |
| `audit_entries` | the Orchestrator-alone audit list |
| `stall_alarms` | every alarm raised during the run |
| `consecutive_failures` / `circuit_open` / `halted` | the breaker state at the end |

`canonical_json()` is byte-stable for identical input, so a run report can be
hashed and diffed.

## Integration guide for the caller (run creation, the plan loop)

The caller is the engine, never a model. A conforming caller:

1. **Freezes the sheet with the owner.** Collect the owner's answers once, with
   their exact words, scoped to project/task/paths and a decision key, each
   bounded by `expires_at` and/or `ttl_turns`.
2. **Preflights before starting.** Supply the real budget, wall clock,
   authorized scopes, worker occupancy and channels; refuse the start on any
   blocker. This directly satisfies the invariant "an unattended plan run never
   starts without a readiness report the owner started".
3. **Resolves each decision as it arises.** Pass the explicit `now` and
   `current_turn` from engine state. Never derive them from a wall clock inside
   this module.
4. **Applies the disposition under its own fence.** `PARK_BRANCH` parks the
   scope and holds dependents via `compute_plan_impact`; `ESCALATE_OWNER` creates
   an owner question; `AUTO_ACCEPT_LOCAL_COMMIT` performs a **local commit
   only**; `TAKE_RECOMMENDED_OPTION` applies the option and persists the audit
   entry; `APPLY_STANDING_DECISION` applies the owner's answer and records the
   words.
5. **Never pushes.** Pushing and moving `main` remain owner-only. The module
   cannot express either.
6. **Feeds the breaker.** Call `after_task(passed=...)` after every task and stop
   the plan when `halted` is true.
7. **Watches the heartbeat.** Observe the run on an interval and raise the alarm
   through every configured channel; a stalled scope or a dead supervisor is
   never silent.
8. **Writes the morning report.** Feed the resolutions, their decisions, the
   final `RunProgress` and the alarms into `build_morning_report`.

Recommended call shape:

```python
from saberops.unattended import (
    AuthorityMode,
    DecisionSheet,
    build_morning_report,
    compute_plan_impact,
    detect_stall,
    resolve_decision,
    run_preflight,
)

preflight = run_preflight(request)
if not preflight.ready:
    raise SystemExit(f"preflight refused the start: {preflight.as_dict()}")

resolution = resolve_decision(sheet, decision, now=engine_now, current_turn=turn)
if resolution.disposition.value == "park_branch":
    impact = compute_plan_impact(tasks, parked_scopes=(resolution.scope,))
    if impact.plan_stalled:
        write_report(build_morning_report(run_id=run_id, resolutions=resolutions))
elif resolution.disposition.value == "auto_accept_local_commit":
    local_commit_only(resolution)  # never push
```

## Validation and failure modes

All failures are explicit exceptions: `UnattendedError` for malformed or
unbounded input, `DecisionSheetError` for an invalid sheet or answer,
`PreflightError` for malformed preflight facts, and `StallSupervisionError` for
supervision that would be silent or impossible.

| Rejected input | Why |
|---|---|
| wildcard or expansion characters in an identifier, scope path or evidence path | a decision must never match by pattern |
| non-canonical scope path (`./`, `..`, trailing `/`, absolute, backslash) | ambiguous scope identity |
| naive or malformed ISO-8601 instant | an ambiguous bound cannot be evaluated exactly |
| a standing decision with no `expires_at` and no `ttl_turns` | unbounded standing permission |
| `ttl_turns` without `granted_at_turn`, or `ttl_turns <= 0` | no exact turn window |
| `answered_by` other than `"owner"` | only the owner authors a standing decision |
| duplicate `standing_decision_id`, or duplicate `(scope, key)` | ambiguous matching |
| a decision with alternatives or a rationale but no recommendation | rejected alternatives belong to a recommendation |
| an acceptance, resolution or report record with `push_allowed`/`pushed` true | pushing is owner-only |
| a stall observation with no channel, a duplicate channel, a future heartbeat or a non-positive threshold | a silent or impossible supervision |
| a negative budget, wall clock, or worker count | corrupt engine facts |
| a plan graph with a duplicate task, unknown scope, unknown dependency, self-dependency, cycle, or mixed projects | the impact would be a guess |
| a held scope that is not part of the plan | the runner never invents work |

## Testing

`tests/test_unattended.py` is hermetic: no providers, CLIs, login, network,
database, filesystem mutation, sleeping or engine launch. Every test carries a
`Covers: RD-1 …` docstring line naming the invariant it protects. The suite
covers the never-delegable list and its immutability, exact-scope matching
(including prefix and wildcard refusals), absolute and turn expiry, owner-word
verbatim recording, sheet integrity, all three decision classes and their
precedence, the authority modes, the audit list, the circuit breaker, the stall
alarms, the preflight blockers and warnings, the plan-impact projection, the
morning report, determinism, immutability and the absence of input side effects.

Run it from a worktree:

```bash
.venv/bin/python -m pytest tests/test_unattended.py -q
```

The lane gate additionally runs ruff and strict mypy across the repository
(`bash scripts/lane-check.sh --base <base> --also tests/test_unattended.py`).

## Deferred work

The following remain for later steps and are not claimed by this module:

* the durable plan store, plan registration and the resume briefing (PL-1);
* run creation wiring and the run-all autonomy loop that calls this policy;
* the owner-question queue that consumes `ESCALATE_OWNER` (Q-1);
* the Telegram and local-web transports that fan out a `StallAlarm` (TG-1);
* the durable audit store and the Orchestrator-alone audit list persistence;
* the git operation that performs the Class-1 local commit;
* the readiness-report UI that shows the preflight result to the owner (UI-1).

Until that wiring is reviewed and integrated, this document describes a pure,
tested policy projection — not a working overnight runner.
