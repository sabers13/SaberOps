# ADR-0012: The phase 1 benchmark has a fake mode and a hand-run real mode

- **Status:** Accepted
- **Date:** 2026-11-19
- **Decider:** Saber (owner), via the Phase 1 plan lane B2
- **Related:** [ADR-0005](0005-mcp-first-engine-and-phase-2-board.md) §1 (the
  benchmark run is a Phase 2 precondition), [ADR-0001](0001-saberops-engine-mcp-first.md)
  invariants (isolation, deterministic gates, independent review, owner
  acceptance).

## 1. Context

Phase 1 is proven by ten fixed small tasks that all reach a final state, by
injected failures becoming visible within 120 seconds, and by recovery after the
planning CLI closes. The same rig has to be cheap enough to run per change and
honest enough to run against real subscriptions once, with the owner present.

## 2. Decision

One catalogue of ten measured tasks (plus a drill-only `routine-question` task)
lives in `saberops/benchmark.py` and is shared by both modes. The table records, per task, the final state, the accepted
commit, submit-to-start, time to the accepted commit, completion-notice delay,
owner actions and the subscription actually used.

**Fake mode** runs the catalogue through the lifecycle test rig's fake worker
and fake reviewer inside the test suite. It uses real job, scheduler, worker,
gate, review and acceptance paths but no vendor CLI and no network. It also runs
five failure drills — killed worker, killed scheduler, exhausted quota, an
unroutable job and a duplicate submission — and a combined recovery drill: the
planner session closes, a fake worker is killed, the scheduler is restarted, and
a fresh MCP session's `catch_up` must report the failed job, its reason and the
preserved work location within C2's 60-second limit. Terminal events and desktop
notices are counted across a second scheduler tick and must each occur once.

Fake mode also carries one drill per Wave 3 feature, added in lane B2b and driven
through the feature's own durable records rather than a test-only shortcut:
owner modes (ask-me parks an accept a permissive preset would allow), routine and
critical questions in critical-only, away parking one branch while an independent
branch finishes and the return summary names it, a plan file running independent
lanes in parallel and a dependent lane only after acceptance with `state.md`
matching the records at each step, a mid-run plan revision, the three-sibling
landing queue, quota exhaustion continuing on a second fake subscription from the
preserved checkpoint, a steering note delivered exactly once, one automatic nudge,
per-subscription usage in the results table, the CLI update guard, vendor
self-healing, landed worktree cleanup, auto-accept with undo (ask-me and a stale
candidate never accept), an imported native attempt eligible only after its gate
and a fresh review of its SHA, pause and presence (a closed session or terminal
pauses dispatch and parks running work, and nothing resumes without one
confirmation), a cheap read-only scout report, proxy routing by family token, the
walkthrough to a green preflight, and the three advisor checkpoints. Every drill
group is named by its feature id in the table.

**Results table.** Both modes print the same table: per task the final state, the
accepted commit, submit-to-start, time to the accepted commit, completion-notice
delay, owner actions and the subscription used; below it the failure and feature
drills with their feature group; and a usage section that aggregates input,
output and total tokens per subscription from the durable records `get_job` and
`catch_up` read. A counter a provider did not report is printed as `unknown`,
never inferred.

**Real mode** drives real Codex, Claude and OpenCode Go CLIs and is run by hand
with the owner present. It is not part of the test suite. It runs the ten-task
catalogue once per CLI as three sequential batches on freshly seeded toy
repositories, and each batch selects a reviewer from a different vendor. It
refuses to start without an explicit acknowledgement flag and an isolated, empty
store, and it never reuses the owner's real SaberOps state or config
directories. It removes pay-per-token credentials from the child environment so
a subscription run cannot silently fall back to API billing, leaves
`XDG_DATA_HOME` alone so OpenCode keeps its sign-in, and disables the detached
auto-started scheduler while it holds an attended terminal for its own ticks. Its
default routes are configurable: OpenCode Go `opencode-go/deepseek-v4.1-flash`,
Claude `claude-haiku-4-5-20251001` and Codex `gpt-6-luna`. It refuses to submit
until the isolated store's preflight is green, and prints the owner command that
fixes each missing item.

Fake-mode tests are marked `slow`: the full gate runs them, and the per-change
gate may skip that marker (lane E2 owns the selection change). The Wave 3 drills
follow the same rule, so a per-change gate stays fast while the full gate proves
every mode and comfort feature.

## 3. Consequences

- The benchmark cannot accidentally spend real subscription quota: real mode is
  opt-in twice (flag and isolated store) and preflight-gated.
- Fake mode exercises the same durable records the owner reads, so the results
  table is produced from engine evidence rather than test assertions.
- The rig never weakens billing consent, owner acceptance, independent review or
  isolation; it only supplies fake providers, a fake reviewer and a stub
  notifier.
- Engine defects the rig exposes are filed in the lane notes; an expected-failure
  marker may name a defect but never flakiness.
