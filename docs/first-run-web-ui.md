# Owner web pages (Step 019 / UI-1, trimmed by phase 1.1 H23)

This document describes the local web surface served by `orch ui` after phase
1.1 lane H23 (ADR-0076): first-run Setup, Accounts and the question queue. The
lane retired run monitoring, run reports and web accept/reject, plan and wave
scoring, the routing editor and the storage cleanup page; the engine, the
`orch` CLI and MCP keep those operations, and phase 2 rebuilds the board on
this same server.

The surface is **localhost-only** and **read-mostly**: every projection is a
read of authoritative backend state, and every mutation is gated on a
process-local CSRF token plus (where required) explicit owner confirmation.

## Routes

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/` | `303` redirect to `/setup` (the app entry until phase 2's board page). |
| GET | `/setup` | System check, provider sign-in, model discovery, billing confirmation and agent connection. |
| GET | `/accounts` | What is left on each subscription and how it is billed. |
| GET | `/questions` | Owner question queue, the owner mode, the work gate, advisory standing permissions and never-delegable decisions. |
| GET | `/first-run` | Legacy URL: `303` redirect to `/setup`, keeping the query string. |
| POST | `/first-run/discover` | Run bounded model discovery for one connection (evidence only). |
| POST | `/first-run/posture/verify` | Record the owner's billing-posture attestation for one selector. |
| POST | `/setup/agents` | Connect or remove SaberOps in one agent CLI. |
| POST | `/setup/mcp-switch` | The master switch for every connected agent. |
| POST | `/accounts/confirm-all` | Re-confirm every recorded account's billing in one click. |
| POST | `/accounts/check` | Owner-initiated, zero-inference usage reading for one account. |
| POST | `/questions/{id}/answer` | Record the owner's answer to one queued question, verbatim. |
| POST | `/questions/{id}/defer` | Defer one question until an explicit ISO date. |
| POST | `/questions/mode` | Owner-only switch of the effective owner mode (`ask-me`, `critical-only`, `away`). |
| POST | `/questions/pause` | Owner-only confirmed pause of all work. |
| POST | `/questions/resume` | Owner-only confirmed resume of all work. |
| GET | `/health` | Non-secret install identity (unchanged). |

`{id}` in the question routes may be either the deterministic question id or
its stable key; the queue resolves a key first.

Each POST redirects on success to the page that owns it (`/setup`, `/accounts`
or `/questions`) and re-renders that page with the error on refusal.

## Setup (`/setup`)

1. **Empty-state navigation.** `GET /` always lands on Setup. A fresh store
   greets the owner ("Welcome to SaberOps"); a configured store shows the
   ordinary Setup heading.
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
6. **Agent connection.** `orch connect` stays the CLI path; the page connects
   or removes one agent and owns the master MCP switch.

Choosing which model does each job is a CLI operation (`orch routing`) until
phase 2's setup page returns that control; the Setup page says so.

## Accounts (`/accounts`) and Questions (`/questions`)

- **Accounts.** What is left on each subscription, the source of the figure,
  its age and how it is billed. Usage is read from each CLI's own metadata (no
  prompt, no quota spent); an unreadable reading is shown as unknown.
- **Question queue.** Open and due questions are listed with their decision
  class, options and blocks. The owner answers with `OWNER` authority (the local
  web surface is the owner surface; the MCP server explicitly refuses owner
  authority), and answers are stored verbatim.
- **Owner mode and work gate.** The page shows the one owner mode, each
  decision it permits, and the F17 gate: paused or running, why, and what is
  parked. Away needs a bound (an end time or a job cap).
- **Unattended & standing decisions.** The never-delegable decision kinds and
  any durable standing decisions are shown with their live/expired windows.
  Standing decisions are read from `<state>/unattended-decisions.json`
  (override: `ORCH_UNATTENDED_STORE`) and parsed into
  `saberops.away.StandingDecision`, so expiry is computed by the canonical
  `is_live`/`is_expired` logic.

## Security architecture

- **Localhost only.** The server binds `127.0.0.1`.
- **Host & Origin protection.** A middleware refuses any request whose `Host`
  is not loopback (or the synthetic `testserver` scope) with `403`, before any
  owner data is returned. Every `POST` is treated as a state mutation: a
  cross-site `Sec-Fetch-Site` or a foreign `Origin` is refused with `403`.
- **CSRF.** A process-local token (`secrets.token_urlsafe`) is rendered as a
  hidden `csrf` field in every form and verified in constant time
  (`hmac.compare_digest`) before any mutation.
- **Owner confirmation.** Pausing, resuming and away-mode switches carry an
  explicit confirmation (`confirm=yes` or a required bound).
- **No credential capture.** No password/token/secret input exists; credentials
  are references only and are never read or transmitted by these routes.
- **Fail-closed reads.** Every projection degrades to an honest empty state and
  never raises into the request path, so an unreadable store cannot 500 the
  page.

## Operational workflows

- **Fresh install.** Open the UI, follow the Setup steps top to bottom (connect
  a vendor in your terminal, discover models, confirm billing, connect your
  agent), then run `orch routing` to choose which model does each job.
- **Answer a question.** Questions lists pending items (the sidebar shows the
  count); type the answer (and choose an option when the question offers one)
  and submit. The answer is recorded verbatim with owner authority.
- **Stop and resume.** Questions carries the owner-only pause and resume with
  their confirmation, and the page names why work is stopped.
- **Run work and read results.** Submitting, monitoring, reporting, reviewing,
  accepting and cleaning up stay in the planning CLI, the `orch` CLI and MCP
  until phase 2's board.

## Development notes

- The owner pages are `setup.html`, `accounts.html` and `questions.html` in
  `src/saberops/templates/`, extending the shared `base.html` shell and
  `_ui.html` macros, styled only by `src/saberops/static/css/app.css` (see
  `src/saberops/DESIGN.md`).
- Tests: `tests/test_web.py` (routes, states, persistence, security),
  `tests/test_accounts_view.py` (account readings and billing confirmation) and
  `tests/test_contract_first_run_ui_assets.py` (asset contracts). They run
  hermetically with isolated XDG roots and no network.
