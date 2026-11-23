# ADR-0073: Retire the Cline adapter and the direct-API worker

- **Status:** Accepted. Phase 1.1 lane H17.
- **Date:** 2026-11-23
- **Decider:** Saber (owner), Q13: delete the Cline adapter and the direct API
  worker path
- **Related:** [ADR-0001](0001-saberops-engine-mcp-first.md) (worker adapters
  are official vendor CLIs; this ADR narrows that list),
  [ADR-0005](0005-mcp-first-engine-and-phase-2-board.md) (the planning CLI is the
  orchestrator)

## 1. Context

SaberOps supported five worker adapters: OpenCode, Codex, Claude Code,
Antigravity (attended-only) and Cline. It also carried a second execution
transport, a direct OpenAI-compatible API worker: `workers/direct_api.py`
launched `workers/api_agent.py` as an isolated child process, which ran a
bounded HTTP tool loop against a stored API connection. A separate
`model_access/api_verification.py` verified those connections, the access store
could add new ones (`orch access add`), the web setup and discovery paths could
enumerate and probe their models, and routing could bind them through the
`DIRECT_API` execution kind.

None of that is one of the owner's four CLIs. The Cline adapter was a fifth CLI
and the direct-API loop a fifth transport, both with their own routing entries,
readiness checks, billing-posture entries, web verification path and tests. The
owner's Q13 answer deletes both.

## 2. Decision

1. **Four worker adapters remain:** OpenCode, Codex, Claude Code and Antigravity
   (attended-only, ADR-0001 Amendment 1). `AdapterRegistry.default()` no longer
   registers Cline; the `cline` capability profile, provider policy default,
   doctor row, usage row, environment entry, structured-trace mapping, session
   provider and routing prefix are removed with it.
2. **The direct-API transport is removed:** `workers/direct_api.py`,
   `workers/api_agent.py` and `model_access/api_verification.py` are deleted.
   The registry resolves no API adapter, the binding resolver fails closed with
   `UNSUPPORTED_BACKEND` for a binding that names it, and discovery/probing no
   longer sends a request for an API connection.
3. **A stored API connection is inert, never deleted.** Old
   `access-connections.json` rows still load (the account declarations share the
   store) and are reported `NOT_READY / PROTOCOL_UNSUPPORTED`; nothing is sent
   anywhere. `orch access add` and the API presets are removed, so no new API
   connection can be created. `BindingKind.DIRECT_API` and the legacy
   `backend:DIRECT_API` posture selector stay parseable so a pre-upgrade
   document still loads; no candidate can match the selector because no
   API-backed execution exists.
4. **A saved routing file that names Cline keeps working.** The loader strips
   retired Cline route ids from the *effective* document, recomputes the
   snapshot over the remaining routes, and reports one notice:
   `Cline worker adapter was removed (H17); retired route(s) dropped: …`.
   `orch routing show`, `orch models` and `orch routing validate` print it.
   A persisted pre-upgrade run snapshot is *not* rewritten: its retired ids are
   still accepted during reconstruction so its stored digest keeps verifying;
   those candidates simply have no available adapter and cannot dispatch.
5. **Packaged plans lose the Cline account, pool and posture.** The ClinePass
   entries are deleted; the packaged `backend:DIRECT_API` posture is retained as
   class `X` with the note `removed (H17)`, matching the existing retired
   Copilot row. Ambient `OMNIROUTE_API_KEY` scrubbing stays: a removed route is
   no reason for an ambient key to reach a worker child.
6. **Tests move with the code.** The Cline adapter, classifier, parser and
   routing tests, the direct-API contract tests and the OmniRoute provider tests
   are deleted. Tests that guarded a wider property (catalog serialisation,
   storage audit, hermetic environment, vendor parser contracts, the H36
   pre-1.1 store upgrade) are kept and adjusted; the H36 fixture keeps its Cline
   route and now asserts the one notice.

## 3. Consequences

- One fewer vendor adapter and one fewer execution transport to keep current
  with CLI drift, billing posture, quota classification, tracing and tests.
- A pre-upgrade routing file naming Cline is a supported input; an upgrade
  produces one notice instead of a hard validation failure.
- An owner who had API connections loses their execution. That is the Q13
  decision; their stored rows are left readable, and no data is deleted.
- The deleted `api_agent` tool loop enforced its own worktree scoping and
  credential scrubbing. Removing it removes that attack surface; it was never
  a safety property for the four remaining CLI adapters, whose containment and
  hermetic environment are unchanged.
