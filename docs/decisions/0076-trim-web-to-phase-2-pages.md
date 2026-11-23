# ADR-0076: Trim the web app to the pages phase 2 reuses

- **Status:** Accepted. Phase 1.1 lane H23.
- **Date:** 2026-11-23
- **Decider:** Saber (owner), Q9 (delete the web-only wave and project views
  before phase 2) and the recorded 2026-11-22 answer: do the suggested
  deletions and "maximise the deletion" without overriding "verify before
  deleting"
- **Related:** [ADR-0005](0005-mcp-first-engine-and-phase-2-board.md) (the
  planning CLI is the orchestrator; the bounded UI is phase 2's board plus
  setup), [ADR-0060](0060-gate-protection-on-the-real-paths.md) (check
  protection stays on the engine path and is untouched here)

## 1. Context

The web app carried five owner pages, a run-monitoring surface and several
read models that existed only to serve them:

- **Run monitoring:** `GET /`, `GET /runs/{run_id}`, its report JSON, the
  action-state JSON, the SSE stream, the two-step accept POST and the reject
  POST, with `dashboard.html`, `run_detail.html` and the run-page controller
  `static/js/app.js`.
- **Plan and wave scoring:** the Projects page's plan registry and
  `wave_plan_optimizer` projection, and the project entry-mode view.
- **The routing editor:** `GET /routing`, its add/remove/move POSTs, the
  concurrency-limit POST and the routing option picker.
- **The storage cleanup page:** the Projects page's dry-run/apply surface and
  `POST /first-run/storage/apply`, plus project registration.

Phase 2's plan (U0) inventories those pages and says each is kept, folded into
the board, or retired. Its kept reuse is Setup (U6), Accounts (U5's data
source) and Questions (U4's question cards); the board page itself (U2), the
worker detail (U3) and board actions (U4) are new pages built on the same
server, not the retired run page. Phase 1.1 therefore keeps those three owner
pages and retires the rest, so phase 2 designs against a small truthful
surface instead of inheriting layered views.

The operations themselves are not web-only: submission, monitoring, reports,
review, acceptance, resume and cleanup live in the engine, the `orch` CLI and
MCP. `orch routing`, `orch accept`, `orch report` and `orch gc` keep every
removed page's action reachable. The one exception is owner **reject**, whose
only surface was the run page; that is recorded in §3.

## 2. Decision

1. **Three owner pages remain.** `OWNER_PAGES` is `setup`, `accounts` and
   `questions`. `GET /` is a `303` redirect to `/setup` (the app entry until
   phase 2's board page), and `/first-run` keeps its legacy redirect. The
   navigation rail lists Questions, Accounts and Setup only.
2. **Run monitoring is retired with its route set:** the dashboard and run
   page, `GET /runs/{run_id}/report`, `GET /runs/{run_id}/actions`,
   `GET /runs/{run_id}/events/stream`, `POST /runs/{run_id}/accept`,
   `POST /runs/{run_id}/reject`, `dashboard.html`, `run_detail.html` and
   `static/js/app.js`. The acceptance checklist, the accept engine, the owner
   action supervisor, run reports, candidate diff/reject and SSE wake
   infrastructure all stay in the engine with their tests.
3. **Plan, wave and storage read models are retired with their pages:**
   `projects.html`, `routing.html`, the plan registry view, the wave optimizer
   view, the project entry-mode view, the concurrency view, the routing view
   and the storage dry-run view, with `POST /routing/add|remove|move`,
   `POST /first-run/concurrency`, `POST /first-run/project/register` and
   `POST /first-run/storage/apply`. `wave_plan_optimizer.py` and
   `project_mode.py` themselves are H10's to delete; this lane only removes
   their web callers.
4. **Model discovery stays.** Setup's `POST /first-run/discover` and the
   `access_ops.discover_access_models` seam it calls are kept (the CLI and the
   startup refresh use them). Only the routing-page `return_to`/`job` branch is
   removed. The billing-posture POST stays for Setup and Accounts.
5. **MCP stops advertising retired pages.** `catch_up`'s next action and its
   evidence map no longer link `/runs/{run_id}` or claim the owner rejects the
   candidate "from the run page"; the engine action command it already names
   (`orch accept RUN_ID`) is the whole instruction.
6. **Tests move with the code.** The run-page, run-live, routing-page,
   concurrency, project-registration and storage-page tests are deleted. Tests
   that guarded a wider property are kept and narrowed to the engine seam: run
   visibility (CLI), the acceptance checklist, candidate diff/reject,
   owner-action durability and ordering, questions/mode/pause, the host guard
   and the theme owner. A new test asserts every retired route is gone and that
   `/` lands on Setup.
7. **No safety property is deleted.** Check protection, review evidence, owner
   acceptance, pause, billing consent, the CSRF/Host/Origin guard and the
   never-delegable list are untouched.

## 3. Consequences

- The web app shrinks from 3,716 lines of `web.py` plus 4 templates and a
  controller to the kept owner pages. Measured reduction is recorded with the
  lane's notes.
- Between phase 1.1 and phase 2's board, the owner has **no web reject
  button**. Acceptance stays available (`orch accept`), rejection decisions stay
  in the engine (`reject_candidate`) and phase 2's U4 puts reject back on the
  board with the recommended-answer question cards and the rejection note. This
  is a deliberate consequence of retiring run monitoring, not a weakening of a
  safety guarantee.
- Reading runs during that window uses `orch status`, `orch watch` and
  `orch report`; MCP `catch_up`/`get_job`/`wait_job` return the same facts.
- A `/routing` or `/projects` bookmark now 404s; saved routing, plan and
  concurrency settings are untouched and remain readable from the CLI.
- Closing the routing editor and the project page removes their read
  projections (`_role_binding_view`, `_role_chain_rows`, `_plans_view`,
  `_wave_view`, `_storage_view`). Unrelated dead code this lane did not own
  (for example `plan_state.LaneEvidence`'s `/runs/{run_id}` identifier) is
  reported for H31 rather than deleted here.
