# ADR-0005: MCP-first engine stays; board and setup page are Phase 2

- **Status:** Accepted. The UI, role and cut decisions stand, and the role
  retirement has landed in Phase 1 lane E1. The Telegram, replay, roadmap and
  in-engine orchestrator-memory deletions have landed; the wave executor, its
  slot policy and `execute_wave` were removed in lane D3.
- **Date:** 2026-11-19
- **Decider:** Saber (owner)
- **Supersedes in part:** [ADR-0001](0001-saberops-engine-mcp-first.md) (UI scope, role vocabulary, cut list). ADR-0001's billing, isolation, gate and independent-review invariants still stand.

## 1. Decision

SaberOps stays an **MCP-first execution engine**, driven from the owner's chosen
CLI session — currently Codex, Claude Code, OpenCode and Antigravity — while the
engine itself runs headlessly. The MCP server is the primary integration
surface; the `orch` CLI and the local service API remain, at parity with it.
Per-CLI instruction files installed by `orch connect` are discoverability aids
only: the MCP server stays the only engine connection. A UI is now **in scope**,
bounded to a **board plus setup page**, and sequenced as **Phase 2**.

Phase 2 starts only once **all** of these entry criteria pass:

1. Phase 1's exit test has passed, including the benchmark run (every task
   reaches a final state on each tested CLI) and the owner's week of daily use
   without a blocking defect.
2. Job and run records are trustworthy: no job is left showing as running after
   its worker dies (Phase 1 lanes C1 and C2).
3. Acceptance evidence is bound to the current candidate commit (lane C10; see
   [ADR-0008](0008-candidate-bound-acceptance.md)).
4. The owner has approved this board-plus-setup scope and the design record
   that bounds it (this record).

The backend is made solid first; the page is a second client of the same engine
records, built afterwards.

## 2. The UI is bounded

The board shows **only what the engine already records**. Its columns and each
card's reason are derived from job state, worker health, review and check
results, and open questions; the page holds no independent state. A card's
column is computed, never set by hand, and there is no drag and drop.

- **Read-only first.** The board ships and is used read-only before any action
  button exists.
- **Accept, reject, answer, cancel and retry** on the page produce the same
  result as the same action over MCP or the CLI.
- **No task submission or planning from the page.** Tasks are submitted through
  MCP or the CLI.
- **No multi-user or remote access.** Local, single owner, as before.
- **No charts or analytics.** This is a work board, not a dashboard.
- **Built on the existing local web app** and its live-event stream: the same
  server and templates, with no new front-end framework, desktop shell,
  separate background service, cloud part or phone app.

The exact layout is the owner's choice from lane U0's mock-ups before any UI
code is written.

If the board needs a fact the engine does not record, the fix is a small engine
change, not page logic that guesses.

The **setup page** covers first-run setup only: connecting a CLI, choosing
accounts and showing where billing must be confirmed before the first job. It
exposes no engine policy of its own; every field it writes is one the CLI and
MCP already write. It offers a button only where the fix is safe from the page
(run discovery, accept a suggested route, save a check command) and the exact
command where the owner must act themselves (sign in, confirm billing). It
**never confirms billing on the owner's behalf**.

## 3. Roles

Three roles the owner can see and choose:

| Role | Meaning |
| --- | --- |
| **plan** | The external CLI session the owner sits in and submits tasks from |
| **build** | One accountable worker per task, including its own repair retries |
| **check** | An independent reviewer, ideally a different vendor family |

**SURGEON and INTEGRATOR are retired.** Landing, gates, the memory and disk guard
and cleanup are **deterministic engine stages**, not model roles. Starting a task
on a stronger model, or escalating along the build chain, is the build route's
`strong_start` setting, not a role. Independent review, provenance and
permission boundaries stay.

Implemented in lane E1:

- One role vocabulary (`PLAN`, `BUILD`, `CHECK`) replaces the overlapping job,
  dispatch, binding, question and routing role enums. Persisted legacy values
  (`ORCHESTRATOR`, `WORKER`, `SURGEON`, `INTEGRATOR`, `REVIEWER`) load through
  explicit mappings; new writes never emit them.
- Routing schema 3 holds `roles.BUILD` and a top-level `strong_start`. A stored
  document naming `WORKER`/`SURGEON`/`INTEGRATOR` migrates on load — `WORKER`
  becomes `BUILD`, `SURGEON` becomes `strong_start`, `INTEGRATOR` is dropped —
  and `orch routing migrate` reports the before/after mapping. A persisted run
  snapshot keeps its original role keys and digest so adoption stays
  byte-stable.
- A fresh setup needs one build route and one check binding. A repair after a
  failed check runs on the build route. A run whose planner records
  `difficulty: hard` starts on the build route's `strong_start` setting;
  `routine` or a missing difficulty starts on the ordinary build chain. Both
  stay under quota and the owner's automatic routing ceiling, and a manual
  high-tier run keeps starting on `strong_start`. A run still escalates only on
  typed capability-failure evidence. The keyword tier classifier is gone
  (ADR-0063): the planner states the judgement, the engine maps it.
- Every run and job record shows who planned, who built and who checked; an
  unreported identity stays `UNKNOWN`.
- A docs-only review skip under the existing risk-adaptive policy is unchanged;
  a separate owner switch for it was not added. Follow-up: consider one only if
  it does not duplicate the risk-adaptive classifier.

## 4. What was cut, and why

| Cut | Why |
| --- | --- |
| Telegram bridge | Unreachable from the CLI, the web app and the MCP server |
| Replay | Same; no caller path exercised it |
| Roadmap change requests | Same |
| In-engine orchestrator memory and turn policy | The planning role is the CLI session the owner sits in; duplicating it in the engine added a second, unexercised planner |
| Wave executor, its slot policy, `execute_wave` and the MCP wave tools | Parallel work is several ordinary jobs; the wave layer was unreachable from the three interfaces and cost gate time |

These are **removed**, not hidden: they remain in git history. Removing them,
and the tests only they used, is the largest maintenance and gate-time saving in
Phase 1. Lanes D1, D2 and D3 have landed, so the wave-executor deletion is
complete. **Antigravity stays attended-only as a worker**; driving SaberOps from
an Antigravity session is supported, automating Antigravity is not.

## 5. What still stands from ADR-0001

Subscription-only billing through the vendors' official CLIs, hermetic
isolation, per-attempt worktrees, deterministic gates, independent review,
owner acceptance, and "LLMs advise; deterministic software decides". See
[ADR-0001](0001-saberops-engine-mcp-first.md) §3.4 and §4.

## 6. Consequences

- The engine's value is proven without a UI first; the benchmark and daily use
  are the gate to any front-end investment.
- The board cannot invent state, so a wrong card is an engine-record bug, found
  and fixed in one place.
- The board ships read-only before it can act, so a wrong column is found before
  any action can be taken from the page.
- Fewer subsystems, fewer tests, faster gates.
- The owner still needs a supported CLI to plan and submit work in Phase 1.

## 7. Related records

- [ADR-0007](0007-dynamic-memory-budget.md) — concurrency follows live memory,
  not a fixed worker or slot count.
- [ADR-0008](0008-candidate-bound-acceptance.md) — review and acceptance
  evidence bind to the candidate commit.
- [ADR-0009](0009-gate-and-test-policy.md) — per-change gate, one full gate per
  candidate, evidence-based pruning.
- [ADR-0010](0010-receipt-bound-gate-skip.md) — the receipt-bound fast-forward
  skip of the duplicate post-integration gate.
- [ADR-0011](0011-bounded-automatic-actions.md) — what the engine may do
  without an owner request.
