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

One catalogue of ten fixed tasks lives in `saberops/benchmark.py` and is shared
by both modes. The table records, per task, the final state, the accepted
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

**Real mode** drives real Codex, Claude and OpenCode Go CLIs and is run by hand
with the owner present. It is not part of the test suite. It refuses to start
without an explicit acknowledgement flag and an isolated, empty store, and it
never reuses the owner's real SaberOps state or config directories. It removes
pay-per-token credentials from the child environment so a subscription run
cannot silently fall back to API billing. Its default routes are configurable:
OpenCode Go `opencode-go/deepseek-v4.1-flash`, Claude `claude-haiku-5-5` and
Codex `gpt-6-luna`. It refuses to submit until the isolated store's preflight is
green, and prints the owner command that fixes each missing item.

Fake-mode tests are marked `slow`: the full gate runs them, and the per-change
gate may skip that marker (lane E2 owns the selection change).

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
