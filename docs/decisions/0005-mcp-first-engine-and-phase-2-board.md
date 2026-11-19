# ADR-0005: MCP-first engine stays; board and setup page are Phase 2

- **Status:** Accepted
- **Date:** 2026-11-19
- **Decider:** Saber (owner)
- **Supersedes in part:** [ADR-0001](0001-saberops-engine-mcp-first.md) (UI scope, role vocabulary, cut list). ADR-0001's billing, isolation, gate and independent-review invariants still stand.

## 1. Decision

SaberOps stays an **MCP-first execution engine**, driven headlessly from the
owner's chosen CLI. The MCP server is the primary integration surface; the
`orch` CLI and the local service API remain, at parity with it. A UI is now
**in scope**, bounded to a **board plus setup page**, and sequenced as
**Phase 2**.

Phase 2 starts only after Phase 1's MCP core is **complete and tested**: the
benchmark run has passed and the owner has used it daily for a week without a
blocking defect. The backend is made solid first; the page is a second client of
the same engine records, built afterwards.

## 2. The UI is bounded

The board shows **only what the engine already records**. Its columns and each
card's reason are derived from job state, worker health, review and check
results, and open questions; the page holds no independent state.

- **Accept, reject, answer and cancel** on the page produce the same result as
  the same action over MCP or the CLI.
- **No task submission or planning from the page.** Tasks are submitted through
  MCP or the CLI.
- **No multi-user or remote access.** Local, single owner, as before.
- **No charts or analytics.** This is a work board, not a dashboard.

If the board needs a fact the engine does not record, the fix is a small engine
change, not page logic that guesses.

The **setup page** covers first-run setup only: connecting a CLI, choosing
accounts and confirming billing posture before the first job. It exposes no
engine policy of its own; every field it writes is one the CLI and MCP already
write.

## 3. Roles

Three roles the owner can see and choose:

| Role | Meaning |
| --- | --- |
| **plan** | The external CLI session the owner sits in and submits tasks from |
| **build** | One accountable worker per task, including its own repair retries |
| **check** | An independent reviewer, ideally a different vendor family |

**SURGEON and INTEGRATOR retire.** Landing, gates, the memory and disk guard and
cleanup are **deterministic engine stages**, not model roles. Starting a task on
a stronger model, or escalating along the worker route chain, is a route
setting, not a role. Independent review, provenance and permission boundaries
stay.

## 4. What was cut, and why

| Cut | Why |
| --- | --- |
| Telegram bridge | Unreachable from the CLI, the web app and the MCP server |
| Replay | Same; no caller path exercised it |
| Roadmap change requests | Same |
| In-engine orchestrator memory and turn policy | The planning role is the CLI session the owner sits in; duplicating it in the engine added a second, unexercised planner |
| Wave executor and MCP wave tools | Parallel work is several ordinary jobs; the wave layer was unreachable from the three interfaces and cost gate time |

These are **removed**, not hidden: they remain in git history. Deleting them,
and the tests only they used, is the largest maintenance and gate-time saving in
Phase 1. **Antigravity stays attended-only as a worker**; driving SaberOps from
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
- Fewer subsystems, fewer tests, faster gates.
- The owner still needs a supported CLI to plan and submit work in Phase 1.
