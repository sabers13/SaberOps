# ADR-0001: SaberOps becomes a plan-aware, verified execution engine (MCP-first)

- **Status:** Accepted
- **Date:** 2026-10-11
- **Decider:** Saber (owner)
- **Supersedes:** the v0.3.x direction "local control plane / multi-model workspace with web console"

## 1. Decision in one paragraph

SaberOps stops being a broad multi-agent workspace application and becomes a **headless execution engine**: you (or any of your own agents) submit coding tasks; SaberOps runs them through the vendors' official CLIs **within each subscription's plan limits**, isolates every attempt in a git worktree, verifies results with deterministic gates and an independent review, and returns **accepted commits with evidence**. The engine is protocol-agnostic and is exposed through a local service API, the `orch` CLI and an **MCP server** (primary integration surface). Other front-ends (OpenMausBot, Hermes, OpenClaw, Claude Code, Codex, scripts) connect through those interfaces; SaberOps is **not** built as a plugin of any of them.

## 2. Context

### Goal (unchanged)
Use every AI subscription Saber pays for **through plan limits, not pay-per-token API billing**, with one architecture, any model in any role (orchestrator, worker, reviewer), and **no bans and no extra charges** as the top priority.

### What changed our minds
1. **The useful core is small.** On 2026-10-10/11 a working multi-agent pipeline ran with almost no SaberOps code: task prompts in `.orch/`, one git worktree per task, workers committing on their own branches, a gate, independent review, a task board. Real defects were caught (an environment leak of API keys through `request.env`; a coverage-attribution bug found by mutation testing). The value lived in ~5 concepts, not in ~95k lines.
2. **The broad space is taken.** OpenMausBot (inspected at source level, 2026-10-11: ~2,400 TS files, 4.3k stars, Apache-2.0 except `enterprise/`) already delivers the "team of agents in a chat app" product: drivers for the official `claude`/`codex`/`grok` CLIs, 500+ app integrations, voice, computer use, desktop builds. Vendors (Codex app, Claude Code background agents, OpenCode) also cover the workspace/UI layer.
3. **Nobody does the engine part.** OpenMausBot verified gaps:
   - plan usage is **display only** (`server/plan-usage.ts` -> one admin route); no pacing, deferral or failover; a quota error just ends the turn (`drivers/retry.ts`);
   - no worktree isolation (shadow-git checkpoints for diffing; bots edit the real folder);
   - no deterministic gate / acceptance;
   - weaker billing hardening for Claude: loads `--setting-sources project` (repo settings can set `env`/`apiKeyHelper`), no auth-method enforcement before turns, fast mode not disabled in the driver;
   - reads Claude/Codex OAuth tokens from disk to call internal usage endpoints, and refreshes Antigravity's OAuth token to look up identity — patterns that Anthropic's terms forbid ("may not collect … Claude.ai credentials or session tokens") and Google has banned accounts for ("piggybacking on the CLI's OAuth").
4. **Policy facts (verified 2026-10-10, see `provider-billing-posture-2026-10-10.md`):** Anthropic's June 15 programmatic-credit split is **paused**; `claude -p` and the Agent SDK currently draw on subscription limits; OpenAI officially allows plan usage in third-party apps (same pool as Codex); Antigravity terms prohibit third-party access and enforcement is automated and cross-product; Copilot Student is ~200 credits/month with Auto-only model selection.

## 3. Decision details

### 3.1 What SaberOps is
"The engine that turns my subscriptions into verified commits — including while I sleep."

### 3.2 Architecture

```
 Hermes · OpenClaw · OpenMausBot · Claude Code · Codex · scripts · Saber
        |                 |                       |
   MCP server         orch CLI          (later) A2A / ACP adapters
   (stdio + HTTP)     (any agent with a shell)
        \_________________|_______________________/
                  local service API (FastAPI)
                          |
  SaberOps engine
   ├─ Job queue          tasks state requirements (role, capability, read-only,
   │                     model-family constraints, priority, deadline), not providers
   ├─ Plan registry      accounts -> pools -> windows (5h/weekly/monthly); billing
   │                     posture per route with policy URL + verified-on + TTL
   ├─ Capacity ledger    usage-panel observations + vendor errors + learned job cost
   ├─ Scheduler          admit (fail-closed posture) -> score (pacing: spare/hour
   │                     to reset, binding window) -> dispatch; ETA = earliest reset
   ├─ Launchers          official CLIs only; hermetic env; pinned config;
   │                     per-attempt billing attestation
   ├─ Preemption         plan exhausted -> commit WIP + resume capsule -> re-queue
   ├─ Worktrees + gates  isolated attempt; coverage-mapped affected gate; full gate
   └─ Review + accept    independent model family; deterministic acceptance
```

### 3.3 Interfaces
- **Service API** (exists, FastAPI) is the internal contract; every adapter is thin.
- **`orch` CLI** stays (works for any agent with a shell).
- **MCP server (primary integration surface)** — few, coarse, asynchronous tools:
  `submit_task(repo, goal, acceptance, priority, deadline) -> job_id + ETA`,
  `get_job`, `get_result` (commit SHA, diff summary, gate evidence, review verdict),
  `cancel_job`, `list_jobs`, `capacity()`. Logs/evidence as MCP resources. Polling must always work; progress notifications are optional.
- **Later, as separate adapters:** ACP (SaberOps appears as an agent/engine in editors and chat apps, e.g. an OpenMausBot custom engine) and A2A (agent-to-agent delegation).
- **Not a plugin of any host app.** Reasons: host APIs move fast; a host that launches the CLIs itself would void SaberOps' billing guarantees; language mismatch (TS vs Python); a standalone engine is the stronger portfolio artifact.

### 3.4 Non-negotiable invariants
1. Providers are reached **only through their official CLIs/binaries** with the owner's own login. No subscription credentials behind any gateway or proxy; no reading of OAuth tokens or credential files; no multi-account pooling.
2. **Billing safety is a vendor-side + launch-time invariant, not a quota estimate:** overage/usage-credits/auto-reload off at every vendor; hermetic allowlisted child environments; pinned CLI config; refuse unless the login is the intended subscription.
3. **Policy stays inside SaberOps.** Callers (MCP/CLI/other agents) may request; they can never choose a billing route, skip a gate, override posture or accept their own result. "LLMs advise; deterministic software decides."
4. **Per-caller budgets and authentication** for every external client (stdio = launcher identity; HTTP = localhost + per-client token).
5. **Personal use only.** Many agents of Saber's, not many users: running other people's tasks on consumer subscriptions is using the plan on their behalf, which consumer terms forbid.
6. Submitted task text is untrusted input: it reaches worker prompts, never policy decisions.

## 4. Scope

| Area | Decision |
|---|---|
| Worker adapters (Claude Code, Codex, OpenCode, Cline), worktrees, gates, review, acceptance, journal, recovery | **Keep** (engine core) |
| Hermetic env (T-001), config pinning (T-003), quota semantics (T-005) | **Keep**, fold into plan registry/scheduler |
| Coverage-mapped affected gate (T-002/T-002b) | **Keep** (developer tooling and engine gate) |
| Job queue, capacity scheduler, preemption, MCP server, per-caller budgets | **New** (the main build) |
| FastAPI service layer | **Keep**, cleaned into a stable internal API |
| Web console / Jinja UI | **Optional front-end** (minimal status/approval view at most; no further investment until engine milestones are met) |
| Channels bridge, native shared chat, terminal/tmux handoff, synced console | **Cut** candidates (host apps do this better) |
| OmniRoute gateway (~5.8k lines) | **Cut** candidate; API-key-only freeze (T-006) only if kept |
| Copilot adapter | **Cut** (Student plan: 200 credits, Auto-only) |
| Antigravity adapter | **Attended/manual only** until Google answers in writing; candidate for cut from automation |
| Exact keep/cut list | Decided by the scoping inventory (S-001), not by this ADR |

## 5. Consequences

**Positive**
- Focus on the one thing no competitor does: plan-limit scheduling + verified results.
- Much smaller codebase to secure and maintain; clearer CV story with measurable claims (e.g. 45× faster gate with mutation-verified recall; zero-leak hermetic launchers).
- Any front-end, including OpenMausBot, becomes a consumer rather than a competitor.

**Negative / costs**
- Deleting features that took effort (console, Channels, terminal handoff, gateway).
- No polished UI of its own; usability depends on MCP/CLI clients.
- MCP is request/response; long-running jobs need an async design and polling.

**Risks**
- Vendor policy volatility (Anthropic changed course three times in 2026) — mitigated by posture-as-data with verification TTL.
- MCP clients' support for progress/notifications varies — mitigated by polling contract.
- Scope creep back toward a workspace app — mitigated by this ADR's scope table; any new UI work needs a superseding ADR.

## 6. Roadmap (milestones)

| Milestone | Content | Status |
|---|---|---|
| M0 Hardening | T-001 hermetic env (8f0a5d7), T-002/T-002b affected gate (94eeadd, 8d6e77e), T-003 config pinning, R-001 exhaustion signals | in progress |
| M1 Scoping & cut | S-001 keep/cut/extract inventory; cut tasks in dependency order | next |
| M2 Engine core | job queue; plan registry + routes + posture TTL; capacity ledger; pacing scheduler; exhaustion -> preemption (T-004) | planned |
| M3 Interfaces | service API cleanup; MCP server (submit/status/result/cancel/capacity); `orch` CLI parity | planned |
| M4 Safety for clients | per-caller budgets, auth, audit log | planned |
| M5 Adapters | ACP (e.g. OpenMausBot custom engine), A2A | later |

## 7. Open questions
1. Gateway: delete entirely, or keep API-key-only (T-006)? Default: delete unless S-001 finds a real dependency.
2. Web console: keep a minimal read-only status page or remove?
3. Antigravity: keep attended-only adapter or remove from automation entirely?
4. Hermes Agent / OpenClaw: verify each one's MCP client support before promising integrations.

## 8. References
- `provider-billing-posture-2026-10-10.md` (saberops project) — vendor policy status, account toggles, sources
- OpenMausBot source, inspected 2026-10-11: `server/plan-usage.ts`, `server/drivers/{claude,retry,claude-login-auth,chatgpt-plan-auth,antigravity-account}.ts`, `server/checkpoints.ts`
- Anthropic: https://code.claude.com/docs/en/legal-and-compliance · https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan
- OpenAI: https://help.openai.com/en/articles/20001542-using-your-chatgpt-plan-in-other-apps-and-sites
- Google: https://antigravity.google/terms/ · https://github.com/google-gemini/gemini-cli/discussions/20632
