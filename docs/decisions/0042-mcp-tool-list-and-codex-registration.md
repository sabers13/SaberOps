# ADR-0042: Publish the MCP tool list always and register Codex with approval

- **Status:** Accepted. Implemented in the B1 follow-up lane (21 November 2026).
- **Date:** 2026-11-21
- **Decider:** Saber (owner), Phase 1 B1 real-client check
- **Related:** [ADR-0023](0023-proxy-subagents-per-cli.md) (proxy subagents),
  [ADR-0024](0024-model-family-tokens.md) (family tokens)

## 1. Context

The B1 real-client check found Codex 0.170.0 unusable after `orch connect`:

- `codex mcp add` writes only `command`, `args` and `env`. Codex then refuses
  every MCP tool call with `MCP tool call requires approval, but approval
  policy is never`. The captain's older hand-written entry carried
  `default_tools_approval_mode = "approve"`; no SaberOps code wrote it.
- Codex 0.170.0 does not re-list MCP tools after
  `notifications/tools/list_changed`. Because a new session started "off" and
  advertised only `saberops_on`, the rest of the tools never appeared, even
  after the user turned SaberOps on. `orch mcp serve --active` worked only
  because the full list existed at the first `tools/list`.
- `orch connect --profile` was parsed but ignored, and the entry carried only
  `SABEROPS_PROFILE`. Codex forwards only the environment written in its entry,
  so the child lost the owner's `XDG_*` bases and wrote to
  `~/.local/state/saberops/profiles/...` instead of the isolated store.

The same check left the Codex proxy subagents unverified: the generated
`~/.codex/agents/*.toml` files load in 0.170.0, but a session kept the job
instead of spawning `claude-worker`.

## 2. Decision

1. **The session switch is enforced in `tools/call`, not by shrinking
   `tools/list`.** When the owner's master switch is on, the server always
   publishes the registry plus `saberops_on` and `saberops_off`, whether or not
   the session has been turned on. Every tool except `saberops_on` refuses with
   the existing "off in this session" message until the user turns SaberOps on.
   `notifications/tools/list_changed` is still sent for clients that follow it.
   With the owner's master switch off, `tools/list` is still empty: that switch
   means "offer nothing at all".
2. **`orch connect` pins Codex's approval mode.** After `codex mcp add`
   succeeds, the entry gets `default_tools_approval_mode = "approve"`. The key
   is validated with `tomllib` before the file is replaced and re-pinned on
   every re-add, because `codex mcp add` rewrites the table. If the pin cannot
   be written, connect fails and prints the exact line to add by hand. The
   approval covers only this server; SaberOps' never-exposed boundaries are
   still enforced inside the process.
3. **`--profile` is honoured and the entry pins resolved store paths.**
   `orch connect --profile NAME` validates the name with the same rule as
   `SABEROPS_PROFILE`, registers `SABEROPS_PROFILE=NAME`, and always writes the
   resolved `XDG_STATE_HOME`, `XDG_CONFIG_HOME` and `XDG_CACHE_HOME` bases.
   This keeps a profile isolated even when the CLI filters the child
   environment.
4. **Codex proxies are documented and prompted as custom agents.** The files
   already match the 0.170.0 custom-agent schema (`name`, `description`,
   `developer_instructions`, plus model settings; no `mcp_servers` override).
   Codex only delegates when the owner or a skill explicitly asks, so the
   installed SaberOps skill names the proxy and tells the planner to spawn it
   with `spawn_agent`/`agent_type` instead of doing the work itself.

## 3. Consequences

- Codex sees every tool at session start and works after one `saberops_on`.
- The tool names and descriptions are visible while SaberOps is off. This is
  deliberate: the server, not the list, is the boundary. The initialize
  instructions still say not to mention SaberOps until it is turned on.
- Every connection entry now contains absolute store paths. Moving or renaming
  the store requires re-running `orch connect`, which was already true for the
  `orch` path.
- A Codex connect can now fail after the CLI registration succeeded when the
  approval key cannot be pinned. The message says what to add; the entry is not
  silently left broken.
- Other clients keep their behavior: Claude Code and OpenCode refresh on the
  notification, and the pinned environment is harmless to them.

## 4. Alternatives considered

- **Keep the short list and add `--active` to the registered args.** Rejected:
  it turns SaberOps on for every new session, removes the explicit owner
  request, and still would not cover a client that starts off then turns on.
- **Write `config.toml` directly instead of using `codex mcp add`.** Rejected:
  Codex owns the format and the command; appending one validated key to the
  table it wrote is smaller and keeps comments.
- **Per-tool `[mcp_servers.saberops.tools.<tool>] approval_mode` blocks.**
  Rejected as the default: 25 blocks must be regenerated whenever the tool
  list changes. The server-level key has the same effect for this server.
- **Rename Codex proxies to underscore names.** Not needed: 0.170.0's custom
  agent loader accepts the hyphenated names (verified with the local loader),
  and the name must stay aligned with the other hosts.
