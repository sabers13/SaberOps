# Operating notes

Details on routing, usage limits, the chat and agent consoles, and candidate
verification. The [README](../README.md) covers installation and the overall model.

## Worker routing policy

Worker selection uses one ordered chain per tier: T1, T2 and T3. Training
classifications and peak-hour scheduling no longer restrict workers or reviewers.
Existing training flags are accepted for compatibility and do not change admission.
Provider readiness, quota, capabilities, exact bindings and owner model enrollment
still apply. Schema 2 routing files contain `T1.default`, `T2.default` and
`T3.default`. Legacy schema 1 chains merge by stable union: retain primary order
and append unique models from the other scopes. Historical run snapshots keep
their original serialized provenance and hash checks.

## Usage and account limits

The Usage & quota page detects Codex, Claude Code, GitHub Copilot, OpenCode,
Antigravity and Cline; Gemini CLI appears when installed. Account limits use the
CLI's own supported metadata interface where available. Local token activity
comes from numeric session records on this machine and is shown separately from
SaberOps's cross-project recorded calls. Local activity is not a subscription bill
or an account-wide allowance. Missing counters remain unknown; large histories
are explicitly labeled as samples. Refresh uses no prompts, reads no credential
files, and does not change saved routing quotas.

Native limit interfaces: [Codex app server](https://developers.openai.com/codex/app-server)
and [Copilot usage and billing metrics](https://docs.github.com/en/copilot/how-tos/copilot-sdk/features/usage-and-billing).
On POSIX systems, Claude Code and Antigravity account limits are read through
their native [/usage](https://code.claude.com/docs/en/costs)
[panels](https://antigravity.google/docs/cli/commands/usage). Each bounded query
uses an isolated empty workspace and submits only `/usage`, without inference.
`pyte` reconstructs the terminal screen; only quota percentages and displayed
reset labels enter the dashboard. CLI layout changes or login dialogs can make
the query unavailable; the dashboard retains any last-known observation with
its original timestamp. Cline and OpenCode providers other than Go still need
their own account quota interface; local token totals stay inside Activity details.

OpenCode Go rolling, weekly and monthly account percentages are queried through
an ephemeral OpenCode auth plugin while running `opencode models opencode-go`.
The CLI supplies its saved Go authentication to the plugin; SaberOps receives
only percentages and reset dates. The read-only endpoint is implemented in
[OpenCode's own source](https://github.com/anomalyco/opencode/blob/dev/packages/console/app/src/routes/zen/go/v1/usage.ts).
No inference runs or permanent plugin/configuration changes are made. Connect
Go in OpenCode first; missing connections, rejected keys, and missing Go
subscriptions are reported separately.

## Chat and agent consoles

The interface follows [Apple-guided UI design principles](ui-design-principles.md)
within SaberOps' own web design. These principles are part of AGENTS.md and
DESIGN.md and guide every frontend change and browser verification.

Long replies expand on demand. Follow live tracks active calls, and session
metadata stays under Session details. Console excerpts remain bounded
(4 MiB per call, recent excerpts, at most 24 calls shown). The separate SQLite
session journal retains complete output and structured events, control transitions,
and one immutable outcome per invocation, independently of those display limits.
Session details includes Download full session history for newly recorded calls. Older calls
show recorded results; polling never dispatches work.

The Chat view contains conversation only. The right **Agents** sidebar lists
orchestrator, worker and reviewer calls, including finished calls. Expand a card
for output, final response, controls and session details; **All consoles** expands
the sidebar. The run view selector keeps every tab reachable when the sidebar is
open. **Pop out console** opens a separate browser view; **View log in terminal**
is a read-only transcript viewer. **Chat in terminal** is the app's synchronized
text conversation, separate from a provider-native CLI.

Native access uses explicit provider-reported session IDs. OpenCode's live
**Open native CLI** attaches through the execution owner's guarded server;
provider messages join the same manual queue. Other CLI providers use **Take over
in CLI**: the existing owner stops its invocation before the terminal resumes
that exact saved session. Codex, Claude Code, Antigravity, Cline and GitHub Copilot
have provider-specific launch recipes. Missing IDs, missing installations,
unconfirmed shutdown and unsupported isolated/backend configurations show a reason
instead of opening a new session. **Open all in tmux** opens each unique available
session as a pane in the shared `saberops-clients` workspace and attaches a visible
desktop terminal. **Add to tmux** adds or selects one agent in that workspace;
**View tmux** shows it without starting another provider session. Existing clients
are reused, and unavailable calls are reported individually. If the desktop
terminal cannot open, the message explains why and offers the manual fallback
`tmux attach -t saberops-clients`.
Terminal clients never own a SaberOps run. App turns and return to automation are
blocked while a managed resumed native CLI owns the session.

**Reopen saved CLI** remains available after a confirmed finished invocation.
Removed reviewer worktrees are restored from the recorded Git tree; finished
access uses an inspection copy and read-only provider settings. Reopening does
not restart the run, overwrite its completion record, or replace gate/review
results. Recent native terminal output is shown separately; downloaded history
includes its complete captured output. Saved provider history may have been
removed by the provider or user, in which case the provider reports the failure.
Console output can be resized vertically; **Expand console** opens a larger
reading view that follows live output and closes with its Close action or Escape.
The shared tmux terminal opens maximized. In tmux, press **Ctrl+B, then Z** to
expand the selected agent pane; repeat to return to all panes.
Mouse support is enabled for this workspace. For terminal history, press
**Ctrl+B, then [**, scroll with Page Up/Down, and press **Q** to return to input.
Each provider uses its own native interface, with colors and terminal resizing
preserved; the launchers remove inherited headless display flags. Reopen existing
CLI clients to apply changed display settings. Historical orchestrator JSON stays
in the provider history; resumed Claude orchestrator chat is guided to use natural
replies for new messages. These display settings do not change run authority,
provider permissions or subscription authentication.

Native Claude/Codex/Copilot user/assistant messages are mirrored when the local
provider history exposes supported structured records. Cline and Antigravity
native TUI output is captured, but this does not claim structured chat-message
mirroring for those providers. Use **Chat in terminal** for guaranteed message
synchronization. Unmanaged clients opened outside SaberOps remain outside its
ownership guard. Native settings retain subscription login for Claude and
Antigravity, exclude API billing overrides, and disable Claude fast mode. Provider
account billing preferences still belong to the provider. See
[session architecture](session-architecture.md) for the capability boundary.

A compatible Claude orchestrator CLI can stay open while you send app messages
through its preview Channels feature. Reopen an existing client once to connect
the local bridge; the composer then says **shared native CLI**. Both interfaces
use that same provider session, and work requests go through SaberOps' existing
worker workflow. A disconnected bridge never silently launches a second agent or
repeats an uncertain request. This capability is provider-specific: other native
clients keep their handoff rules. The bridge uses native subscription login and
adds no API billing route. Account-backed message delivery remains subject to
the installed CLI's preview support and requires live validation.

For a live supported call with a reported native session, Take control requests
interruption by its existing execution owner. Manual input is enabled only after
the process group is confirmed stopped or the shared provider session confirms idle.
Automation stays held before returning
to gates, review, commits or further dispatch. App and synchronized CLI manual
messages share a durable queue and conversation; worker/reviewer messages are
labelled separately from orchestrator instructions. The adapter's frozen account,
model, worktree and permissions remain in force. No client signals an arbitrary
PID or launches a second writer.

Return to automation refuses a pending manual reply and verifies the worktree
revision and branch. Dirty file edits are allowed and the original assignment
must inspect and validate them. Manual reviewer replies never become formal
verdicts; returning starts a fresh formal review. The original invocation deadline
continues during manual control. Failure, expiry or a dead execution owner does
not automatically resume it. Closing a manual terminal keeps automation held;
`/resume` returns control, and `/interrupt` or Ctrl+C while waiting interrupts a
manual reply. Older calls without captured native IDs cannot be taken over.

Changes includes a Total changes card above per-file cards, with exact full Git
addition/deletion totals and the combined expandable red/green numbered patch.
Binary files are excluded from line counts and identified. Bounded patches are
labelled; totals remain exact when patch output is truncated. Renames appear as
deletion/addition. External diff/textconv programs are disabled. Acceptance and
canonical candidate selection are unchanged.

## Fixed-source candidate verification

Candidate gates and independent reviews run in separate detached verification
worktrees at the exact candidate commit. Their tree hash is recorded in evidence.
Uncommitted candidate changes block snapshot creation; changes to the snapshot
or candidate during verification invalidate that result. This prevents terminal
edits in the implementation worktree from changing the code under an active
candidate gate or review. Acceptance remains tied to the verified commit and its
authoritative post-integration gate. A transient source edit cannot contaminate
a gate that runs against a separate fixed checkout.
