# ADR-0090: Switching the owner mode from chat

- **Status:** Accepted. Implemented by Phase 1.1 lane H13.
- **Date:** 2026-11-24
- **Decider:** Saber (owner). Phase 1.1 plan, owner's words 2026-11-22
  ("can it be on in the chat for example user prompts enable afk mode"),
  recorded answer Q10
- **Related:** [ADR-0020](0020-owner-mode.md) (the owner mode),
  [ADR-0047](0047-expired-away-mode-narrows-to-ask-me.md) (expiry),
  [ADR-0019](0019-gemini-and-stored-key-billing.md) (Codex hooks stay off),
  [ADR-0082](0082-owner-mode-carries-authority.md) (the mode ceiling),
  [ADR-0084](0084-modes-stop-at-consequential-candidates.md) (what away parks),
  [ADR-0089](0089-skills-tool-descriptions-and-worker-instructions.md) (the
  modes skill)

## 1. Context

The owner asked to switch the mode from the chat, for example by prompting
"enable afk mode". The plan's lane H13 requires:

> a typed `/saberops-away <duration>` style command handled by a hook on the
> owner's typed message where the CLI supports one (Claude Code first; Codex,
> OpenCode and Antigravity per what the lane finds), which runs `orch mode set`;
> switching to a safer mode (`ask-me`) is allowed directly from the planner;
> anything looser without the hook needs a short confirmation code shown outside
> the chat. The mode record notes how it was switched.

Done when: "text the AI reads (a file, tool output) cannot switch to away; a
typed command can; tightening works from chat; each path is logged."

The safety problem is the source of the text. A planning session can read files
and tool output, and an `away` mode is a large grant. A mode switch must
therefore be attributable to the human at the keyboard, not to text that
happened to reach the model.

## 2. Decision

### 2.1 Three switch surfaces, and only these

1. **A typed command through the CLI's owner-prompt hook.** The command is one
   of `/saberops-away <duration> [--max-jobs N]`, `/saberops-critical-only` or
   `/saberops-ask-me`. `saberops.mode_switch.run_hook` handles it and calls the
   same `owner_mode.set_owner_mode` the CLI uses.
2. **The owner's terminal confirmation code** for a looser change where no
   owner-prompt hook exists. `orch mode request <mode> ...` writes a pending
   change and prints a six-character code; `orch mode confirm <code>` applies it
   once.
3. **The owner's own command and page.** `orch mode set` and the Questions page
   are unchanged.

`orch mode show` prints the recorded path (`set_by`), and every applied,
refused, pending or unsupported attempt appends one JSON line to
`mode-switches.jsonl` under the owner state directory. The log never stores the
prompt text, so a pasted secret is not copied into the audit trail.

### 2.2 What makes the hook path attributable to the owner

`run_hook` accepts a payload only when all of these hold, and refuses (logging
the refusal) otherwise:

- the CLI names its own **owner prompt** event in `hook_event_name`; a
  `PostToolUse`, `PreToolUse`, notification or any other event is refused, so
  tool output and file text cannot switch the mode;
- the prompt field is present and is a string;
- the command is **the whole prompt and one line**. A pasted document or a
  sentence that merely contains the command is refused rather than applied.

A refusal exits zero and changes nothing, so a hook can never break the owner's
prompt.

### 2.3 Which CLIs have an owner-prompt hook

Recorded as this repository can document it; none was verified on a live CLI in
this lane (H37 does the real-session check, owner present, and fills the gaps):

| CLI | Owner-prompt hook | Finding |
| --- | --- | --- |
| Claude Code | `UserPromptSubmit` | Supported: the hook receives the owner's typed prompt as `prompt`. Documented by the vendor's hooks reference (not vendored here); unverified live. |
| Codex | none | Unsupported: SaberOps launches Codex with `-c features.hooks=false` (ADR-0019), so an installed hook would never run. |
| OpenCode | none documented here | Unsupported: the vendor's plugin system exposes a message event, but no vendored reference or live evidence exists in this repository. H37 decides. |
| Antigravity | none | Unsupported: no owner-prompt hook is documented. |

`orch mode hook install --cli claude` merges one `UserPromptSubmit` entry into
`~/.claude/settings.json`, preserving every other key and hook entry, backing up
the file, and refusing a file that is not plain JSON. `orch mode hook remove`
removes only SaberOps's own command. No lane installs the hook into a real CLI
config; tests use a fake home.

### 2.4 The planner may tighten, never loosen

`tighten_mode` (MCP) switches to `ask-me` and nothing else. It removes
permission only, so it is always safe and is the one mode change the planning
session may make directly. `ask-me` carries the narrowest ceiling; `critical-only`
and `away` share the unattended ceiling today, so a switch between them cannot be
proven narrower and is treated as a loosening.

Every looser change stays an owner decision: `propose_mode_change` keeps raising
an owner question, and no MCP tool sets `critical-only` or `away`.

### 2.5 The confirmation code is shown only outside the chat

`request_mode_change` writes a pending change with a six-character code from an
unambiguous alphabet, a 15-minute expiry, mode `0600`, and a single use.
`orch mode request` refuses to reveal the code when stdin is not a terminal, so
an agent-run shell (whose output returns to the model) never receives it. The
code is the proof that a human acted at a terminal; a wrong, expired, missing or
already-used code changes nothing.

This is defence in depth, not a replacement for the hook: a normal typed command
on a supported CLI does not need a code, and a looser change proposed from chat
on an unsupported CLI is exactly the case the code exists for.

## 3. Consequences

- What each mode permits is unchanged: this lane moves the switch surface and
  records it, not the permission table (ADR-0082).
- Every switch is attributable: the mode document's `set_by`, the switch log,
  and the MCP ledger (`mode_change_proposed`, `mode_change_applied`) name the
  path.
- A CLI without a verified owner-prompt hook keeps the safer default: the owner
  types the command at a terminal, and a chat proposal becomes a two-step
  terminal confirmation.
- H37 must verify the Claude Code payload and re-check the other three CLIs;
  until then the support table, not the code, carries the "unverified" status.
