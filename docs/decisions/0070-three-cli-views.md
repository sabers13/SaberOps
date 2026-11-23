# ADR-0070: Three read-only CLI views — `status`, `watch`, `report`

- **Status:** Accepted. Phase 1.1 lane H9.
- **Date:** 2026-11-23
- **Decider:** Saber (owner), recorded answers 2026-11-22 (`Q8` yes, "maximise
  the deletion") and 2026-11-23
- **Related:** [ADR-0025](0025-read-only-tmux-watch.md) (`orch watch`, the live
  multi-job view), [ADR-0053](0053-acceptance-clause-review-gate.md) and
  [ADR-0055](0055-review-evidence-guarantee.md) (the evidence the report
  carries), [ADR-0068](0068-one-recovery-tool.md) (the same alias-for-one-release
  rule for verbs)

## 1. Context

Nine read-only CLI views covered one run each: `status`, `events`, `handoff`,
`plan`, `observability`, `report`, `orch-context`, `monitor`, plus `watch` for
the live multi-job session. Several were projections of the same durable rows:

- `observability` emitted the canonical snapshot JSON; `report` renders the
  same evidence and already shared the canonical serializer.
- `monitor` re-rendered that same report every few seconds; it differed from
  `report` only by the frame renderer and the polling loop.
- `orch-context` read six bounded evidence refs from the same store; `report`
  did not expose them.
- `events`, `handoff` and `plan` each printed one section of what a single run
  view already had (`status` was reading events for candidate-publication
  evidence and showed attempts, checks and reviews).

Nine names over three facts — run detail, live view, full evidence — is a
memory and documentation cost with no new authority: every one of them opened
the store read-only and none could mutate a run.

## 2. Decision

1. **Three kept views, each with one job.**
   - `orch status <run-id>` — one run or job: identity, supervision, attempts,
     checks, reviews, plus the persisted plan, the latest handoff and the last
     20 events (bounded payload excerpt).
   - `orch watch` — unchanged live view: tmux windows plus summary, read-only
     store; with no tmux it falls back to `orch report <run-id> --follow`.
   - `orch report <run-id>` — the full evidence surface over one run:
     human-readable text by default, `--json` for the canonical report JSON
     (the former `observability` output), `--context [REF]` for one bounded
     durable evidence ref or the ref list (the former `orch-context`), and
     `--follow [--once] [--interval] [--timeout]` for the live frames (the
     former `monitor`).
2. **One implementation per fact.** `status`, `report` and the aliases call
   shared helpers: `_run_plan_payload`, `_latest_handoff`, `_event_tail`,
   `_open_read_only_report_db`, `_follow_report`. No alias re-implements its
   old view; each prints one deprecation line and calls the kept path. The
   bounded context reader in `tooling/context_cli.py` stays as the reader
   `report --context` uses.
3. **Old names are aliases for one release, with a notice.** `events`,
   `handoff`, `plan`, `observability`, `orch-context` and `monitor` keep their
   existing arguments and print
   `[!] 'orch <name>' is a deprecated alias of '<replacement>'; the alias is
   kept for one release.` on stderr. Removing them is a later release's change.
4. **The plan store is not a view.** `orch plan show|validate` addresses the
   Step 015 plan store (`plan-store`); the `plan show|validate` prefix rewrite
   is unchanged. Only `orch plan <run-id>` (the run-plan view) becomes an
   alias.
5. **No authority moves.** Every path here opens the store read-only; the
   aliases add no argument that could mutate a run. `status` and `report`
   remain usable without a planning CLI or a vendor CLI.

## 3. Consequences

- A run's plan, handoff and recent events are reachable from `orch status`;
  its full evidence, bounded refs and live frames from `orch report`.
- Output grows for `status` on a busy run (bounded to 20 events with payload
  excerpts); the full payloads remain available through `report --context`.
- The live fallback text that pointed at `orch monitor` now points at
  `orch report --follow`, so an owner who follows it does not hit a
  deprecation notice from the kept view's own internals.
- One release later the six alias names are deleted without any further
  information moving; H24's single observability projection then reads the
  same kept surfaces.
- A script that parsed `orch monitor`'s frame still works through the alias
  this release; the alias notice is on stderr, so stdout is unchanged.
