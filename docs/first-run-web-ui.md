# First-run web UI (Step 019 / UI-1)

This document describes the complete first-run web surface served by
`orch ui`: the onboarding wizard, the routing and concurrency controls, the
question queue, the unattended/standing-decision view, and the storage/Git
lifecycle dry-run with owner-confirmed cleanup.

The surface is **localhost-only** and **read-mostly**: every projection is a
read of authoritative backend state, and every mutation is gated on a
process-local CSRF token plus (where required) explicit owner confirmation.

## Routes

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/` | Runs; renders Setup (as "First-run setup") instead when the store is empty (no runs, bindings, projects or discovered models). |
| GET | `/setup` | System check, provider sign-in, model discovery and billing confirmation. |
| GET | `/routing` | Model roles, fallback order and capacity (concurrency). |
| GET | `/projects` | Repository check and entry modes, registration, plans and waves, storage cleanup (`?repo=`, `?wave_project=`). |
| GET | `/questions` | Owner question queue, standing permissions and never-delegable decisions. |
| GET | `/first-run` | Legacy URL: `303` redirect to `/setup`, keeping the query string. |
| POST | `/first-run/concurrency` | Persist the owner-confirmed 3-project × 5-task concurrency limits. |
| POST | `/first-run/discover` | Run bounded model discovery for one connection (evidence only). |
| POST | `/first-run/posture/verify` | Record the owner's billing-posture attestation for one selector. |
| POST | `/first-run/project/register` | Register a project identity (explicit owner values). |
| POST | `/first-run/storage/apply` | Execute the previewed storage/Git cleanup under typed owner confirmation. |
| POST | `/questions/{id}/answer` | Record the owner's answer to one queued question, verbatim. |
| POST | `/questions/{id}/defer` | Defer one question until an explicit ISO date. |
| GET | `/runs/{run_id}/events/stream` | Server-sent-event live progress (existing run page). |
| GET | `/health` | Non-secret install identity (unchanged). |

`{id}` in the question routes may be either the deterministic question id or
its stable key; the queue resolves a key first.

The POST paths are unchanged from the original single onboarding page. Each one
now redirects on success to the page that owns it (`/setup`, `/routing`,
`/projects` or `/questions`) and re-renders that page with the error on refusal.

## Setup (`/setup`)

1. **Empty-state navigation.** A fresh install has no runs, bindings, projects
   or discovered models, so `GET /` renders the wizard directly instead of an
   empty run table. Once any of those exist, `/` returns the run list and
   Setup stays reachable from the sidebar.
2. **Readiness & diagnostics.** Python runtime, git executable, the bundled
   SQLite library, and the disk free floor (`max(10 GB, 10 %)`) are computed
   locally. A red row names the exact fix.
3. **Official vendor logins.** `codex login` / `claude login` (and the other
   account backends) always run in the owner's own terminal on the vendor's own
   surface. The page has no field for a password, token, or secret; it only
   shows installed/readiness evidence from `saberops.model_access`.
4. **Providers & model discovery.** Connections and the discovered catalog are
   shown with `source` and exact `execution_support` evidence. `Refresh models`
   calls `saberops.access_ops.discover_access_models`, which writes non-secret
   catalog evidence only and never invents model ids.
5. **Billing posture (fail-closed).** Each route's class, verification date,
   expiry and status come from `saberops.plans_ops.show_postures`. Only routes
   whose status is `OK`/`EXPIRING` are marked auto-routing-eligible; missing or
   expired verification excludes the route everywhere.

## Routing (`/routing`) and Projects (`/projects`)

- **Per-role assignment.** Owner bindings are grouped by semantic role
  (Worker / Reviewer / Orchestrator) with their exact `binding_id`; the
  effective Worker/Integrator/Surgeon fallback chains come from the routing
  snapshot. The Orchestrator choice is explicit and is never engine-dispatched.
- **Concurrency.** The owner ceiling is at most 3 active projects × 5 executing
  child slots per project. Changes are recorded as an immutable owner-approved
  decision in the configuration ledger (`saberops.config`); lowering a limit
  parks new work and never kills running work.
- **Project entry modes.** Given a repository path, the page observes the
  entry-mode facts, recommends the most specific qualified mode
  (`saberops.project_mode`), and lists the exact preflight blockers with their
  fix commands. Registration writes only the explicit owner-supplied identity.
- **Plans & waves.** The effective plan registry (accounts, quota pools,
  postures, reserves) is shown, and a stored active plan revision is adapted
  through `graph_from_revisions` and scored with
  `saberops.wave_plan_optimizer.optimize_plan` to show waves, critical path,
  makespan, parallelism and scope overlaps.

## Runs (`/`), Questions (`/questions`) and storage (`/projects`)

- **Run monitoring.** The run list links to each run page, whose live
  progress streams over server-sent events at
  `/runs/{run_id}/events/stream`.
- **Question queue.** Open and due questions are listed with their decision
  class, options and blocks. The owner answers with `OWNER` authority (the local
  web surface is the owner surface; the MCP server explicitly refuses owner
  authority), and answers are stored verbatim.
- **Unattended & standing decisions.** The authority mode, the never-delegable
  decision kinds, and any durable standing decisions are shown with their live/
  expired windows. Standing decisions are read from
  `<state>/unattended-decisions.json` (override: `ORCH_UNATTENDED_STORE`) and
  parsed into `saberops.unattended.StandingDecision`, so expiry is computed by
  the canonical `is_live`/`is_expired` logic.
- **Storage & Git lifecycle.** For a repository path the page builds the
  read-only inventory (`saberops.storage_inventory`) and the deterministic GC
  preview (`saberops.storage_gc.preview_gc`): eligible targets with their exact
  reference and estimated bytes, and retained/blocked artifacts with their
  reasons. Applying is fail-closed — it requires an approved standing owner
  policy, the typed preview id, and a CSRF token — and every action is written
  to the durable storage audit log.

## Security architecture

- **Localhost only.** The server binds `127.0.0.1` (ADR-0004 §8 Q3).
- **Host & Origin protection.** A middleware refuses any request whose `Host`
  is not loopback (or the synthetic `testserver` scope) with `403`, before any
  owner data is returned. Every `POST` is treated as a state mutation: a
  cross-site `Sec-Fetch-Site` or a foreign `Origin` is refused with `403`.
- **CSRF.** A process-local token (`secrets.token_urlsafe`) is rendered as a
  hidden `csrf` field in every form and verified in constant time
  (`hmac.compare_digest`) before any mutation.
- **Owner confirmation.** Privileged mutations additionally require an explicit
  `owner_confirmed=yes` checkbox (concurrency, project registration) or a typed
  confirmation equal to the freshly re-computed GC preview id (cleanup).
- **No credential capture.** No password/token/secret input exists; credentials
  are references only and are never read or transmitted by these routes.
- **Fail-closed reads.** Every projection degrades to an honest empty state and
  never raises into the request path, so an unreadable store cannot 500 the
  page.

## Operational workflows

- **Fresh install.** Open the UI, follow the four Setup steps top to bottom
  (connect a vendor in your terminal, discover models, confirm billing), then
  configure roles and capacity on Routing.
- **Answer a question.** Questions lists pending items (the sidebar shows the
  count); type the answer
  (and choose an option when the question offers one) and submit. The answer is
  recorded verbatim with owner authority.
- **Plan a cleanup.** On Projects, check the repository path, review the dry-run targets and
  their estimated bytes, then type the preview id and apply. If no approved
  standing policy exists, apply stays refused.
- **Inspect a plan.** Enter a project id under Plans and waves to preview the dependency
  DAG and the optimizer's wave scores for that project's active plan revision.

## Development notes

- The owner pages are `setup.html`, `routing.html`, `projects.html` and
  `questions.html` in `src/saberops/templates/`, extending the shared
  `base.html` shell and `_ui.html` macros, styled only by
  `src/saberops/static/css/app.css` (see `src/saberops/DESIGN.md` §Basalt).
- Tests: `tests/test_web.py` (routes, states, persistence, security) and
  `tests/test_contract_first_run_ui_assets.py` (asset contracts). They run
  hermetically with isolated XDG roots and no network.
