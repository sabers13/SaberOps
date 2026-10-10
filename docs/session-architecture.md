# Managed provider sessions

SaberOps owns execution and workflow state. Provider-native interfaces are
clients; terminal windows and tmux never become execution owners.

## Current capability boundary

| Provider | Execution transport | Native terminal access | Intervention |
| --- | --- | --- | --- |
| OpenCode | Owner-hosted `serve`, structured HTTP/SSE | Guarded native `attach`, desktop or tmux | Native prompts or app takeover request pause; one durable queue |
| Codex | Persistent native `exec --json` | Exact `codex resume ID` after controlled handoff; saved inspection | Exclusive native client lease |
| Claude Code | Native print/stream CLI, subscription login | Exact `claude --resume ID`; saved inspection; preview Channels bridge for orchestrator chat | One native client; app messages enter its channel when connected; API overrides removed |
| Antigravity | Native print/stream CLI, account login | Exact `agy --conversation ID`; saved inspection | Exclusive lease; account route only |
| Cline | Native JSON CLI | Exact `cline --tui --id ID` when a verified ID is captured | Exclusive lease; plan settings for read-only roles |
| GitHub Copilot | Existing native adapter | Exact `copilot --resume=ID` when captured | Exclusive lease; selected profile environment |
| API-only / unsupported backend | Existing adapter | Transcript and synchronized app console | No unrelated native session substituted |

Live native attachment remains provider-specific. Codex app-server and Claude
background-native attachments are not enabled. The other provider recipes resume
saved history after the existing invocation is stopped; they do not attach to a
running headless process. No TUI key scraping is used to infer chat messages.

Autonomous sandbox dispatches retain their existing per-invocation transport;
shared OpenCode servers are disabled there until the persistent-server trace
and reconnection contract is validated.

## Execution ownership and input

The detached run owner retains dispatch, deadlines, worktrees, recovery and
cancellation. OpenCode's server is launched through the existing worker process
seam, including its containment context. A localhost, authenticated guard exposes
only scoped reads and text input to native clients. Its private provider-server
password is distinct from the client password; neither is a provider credential.
All prompts are executed by the owner with the frozen provider/model/agent and
worktree. Client-supplied model and agent changes are not forwarded. Shell,
configuration, credential, session-fork and arbitrary mutation endpoints are refused.
The bridge accepts text prompts; attachment/file prompts and native slash-command
execution are intentionally unavailable until separately governed.

Native input during automation atomically requests takeover and queues the input.
The owner acknowledges MANUAL only after the prior turn is cancelled and provider
idle is confirmed (or a headless process group is stopped and reaped). The owner
then serially executes queued manual turns. Return refuses a queued/running turn,
checks the branch/revision, records the current source hash, and resumes the
original assignment. Manual reviewer chat never substitutes for a formal verdict.
The original deadline continues. Closing a client does not return authority or
stop execution. A dead owner or uncertain termination cannot auto-resume.

A native CLI may show progress while attached during automation. Sending input
requests takeover. Closing tmux closes clients only. Independently launched
clients/shells under the same OS user are outside this guard; this is not an OS
permission boundary against the account owner. Verification uses fixed checkouts
rather than assuming all external edits can be detected while they happen.

## Evidence and verification

`session-journal.db` contains uncapped ordered output/structured events, process
identity, turn outcomes, control transitions and one immutable invocation outcome.
Console excerpts stay bounded. Invocation outcomes preserve native session ID,
provider terminal status, exit status when available, source tree and confirmed
quiescence when available; unknown evidence remains null. The existing attempt
record remains the canonical attempt-level workflow outcome.

OpenCode completion is confirmed from its message API and idle status. The owner
publishes a complete message snapshot followed by idle, seals those messages
against delayed deltas, and allows connected streams a bounded drain before
shutdown. A newly attached stream starts at the current journal cursor; history
comes from the provider message API rather than replaying obsolete busy events.
The native client launcher independently watches the durable invocation outcome,
stops only its attached client, and displays the final response and agent result.
It states that execution stopped only when quiescence is confirmed. The app also
uses the durable outcome ahead of stale display metadata. An agent finishing
successfully does not imply that the run's gate or review passed.

Only execution/recovery owners write completion evidence. UI reads do not mutate
state. If an owner is killed, recovery records interrupted or uncertain evidence;
it cannot fabricate successful completion. Surviving/unknown workers still block
a second execution owner under the existing fencing contract.

Candidate gates and reviews use disposable detached worktrees at the submitted
commit and record its Git tree hash. Source hashing includes untracked submitted
files and deletions using a temporary Git index; the worker's index is unchanged.
Ignored runtime scratch is outside submitted source. Dirty candidate source blocks
verification. Mutating a verification checkout invalidates its result. Frozen run
policy and gate command remain part of existing authoritative evidence. Acceptance
retains provenance, exact-commit and post-integration-gate requirements.

Every engine implementation and repair prompt names the frozen gate command and
its project-root entrypoint requirement. For `make gate`, workers must preserve
or create a root Makefile gate target; another test script cannot silently replace
the configured command. Workers retain focused-check ownership. Chat-created
implementation runs request independent engine review and show their resolved
verification policy in the reply. An explicit project policy disabling review
or the gate remains authoritative. Worker-internal subagent reviews do not count
as engine reviewer dispatches. These defaults apply to newly created runs;
historical run configurations and outcomes are preserved.

## Authentication and rollout validation

The official provider binary owns authentication. SaberOps does not copy OAuth
tokens, impersonate clients or route subscription credentials through a custom API.
Claude API/cloud/gateway overrides and separately billed Fast Mode remain disabled
for the native subscription connection. Antigravity API-key mode is refused; native
account terms and provider-side extra-credit preferences remain the user's provider
settings. There is no automatic switch to an API billing route.

Automated tests use simulated provider results and local HTTP clients, not paid
model calls. OpenCode 1.18.34 was checked locally for server startup, session
creation, read-only permissions, native TUI attachment with the assigned model
and agent, client closure and confirmed server shutdown. No model prompt was
submitted. Account-backed execution, cancellation and native TUI message delivery
still need live validation on the selected provider versions.

## Native client lifecycle and finished sessions

Native resumes have a separate durable lease keyed by provider, binding and
native session ID. Starting or active clients fence duplicate launches, manual
app turns and return to automation. The terminal-side supervisor publishes its
process identity and its child's identity before releasing the child to execute.
It captures PTY output, watches live manual control/deadline expiry and stops only
its own provider process group. A lost owner or unknown/live descendant retains
the fence; it never implies successful completion. A launch error is visible on
the agent card and leaves any already requested takeover held.

The original execution owner waits for terminal-client quiescence before recording
its invocation outcome. Returning from live handoff rechecks the worktree's branch
and revision and validates manual changes through the original workflow; a manual
reviewer's chat response is not a formal verdict. An open saved orchestrator CLI
fences later app provider invocations that would reuse its session. A connected
Claude channel is the exception: app messages are sent to that existing process,
without launching a second provider invocation.

Finished access leaves the original completion, gate and review evidence unchanged.
An inspection copy is reconstructed from its recorded source tree, including when
the ephemeral verification directory was removed. Finished native clients use
read-only provider settings and have a separate client state/output in the sidebar.
They are neither run continuation nor acceptance. Provider-side deleted histories,
older calls without captured IDs and unsupported backend profiles remain explicitly
unavailable. These controls depend on each provider's installed CLI and history
format; no paid provider turn is part of the offline regression suite.

Claude, Codex and Copilot structured local history readers mirror new messages with
stable deduplication keys. Cline/Antigravity TUI output is available as terminal
output; full native message-level chat mirroring is not claimed for them. The
SaberOps synchronized terminal remains the portable, fully synchronized chat path.

## Shared terminal workspace

### Shared app and native orchestrator chat

For a compatible Claude Code CLI, the launcher enables an invocation-local
[Claude channel](https://code.claude.com/docs/en/channels). Channels are a provider
research preview. SaberOps inspects the installed CLI before enabling the
custom development-channel flag; an unsupported CLI still opens with ordinary
exclusive handoff. Registration must actually complete before the app displays
shared native chat as connected. Existing clients must be reopened once.
Claude shows its own development-channel confirmation on startup. Accept it only
for the local SaberOps bridge to connect; it is separate from folder trust.

The only configured MCP server is the local SaberOps channel, started over stdio
by Claude. Its descriptor is private and scoped to the native client reservation,
recorded session and linked owner chat. The development-channel flag enables this
custom channel; it does not disable file permissions. Subscription authentication,
disabled Fast Mode and read-only orchestrator settings remain enforced. The
channel adds no API billing route; normal provider subscription limits still apply.
Claude shared chat uses `default` permission mode with the exact local tool list
`Read,Glob,Grep` plus `mcp__saberops__reply` and `mcp__saberops__request_work`.
Only those two channel tools are preapproved. Ordinary read-only native resume
uses `plan` mode and no MCP servers. Neither recipe enables local write or shell
tools or skips permission checks; work requests still enter governed dispatch.

App messages have stable identifiers and a durable bounded inbox. One app turn is
delivered at a time. The native agent replies through the channel tool, and local
structured history mirrors terminal owner messages and readable assistant replies
back into app chat. Repeated requests/replies are deduplicated. Native UI presentation
of channel events is provider-controlled; a notification is not guaranteed to look
exactly like a typed terminal message. SaberOps never scrapes rendered keys to
infer a work request. The provider serializes terminal input and channel events in
its own conversation; an active native turn may delay an app message.
Native permission prompts can also pause a reply until the owner answers them
in the terminal. The bridge never disables those prompts to force progress.

Work advice is bound to its recorded owner request. SaberOps applies the existing
run creation, project policy, gate, review and acceptance path; the native
orchestrator cannot implement or accept changes itself. App-generated dispatch
and outcome reports enter the native channel as context, not owner instructions.
Native reviewer/worker chat remains separate from formal verification evidence.

Lost channels and replies unanswered for 30 minutes become explicitly uncertain,
with no automatic resend. A UI restart preserves pending channel messages if the
native process survives. A lost UI process during dispatch marks that request
uncertain and does not repeat work; inspect Runs for any run already created.
Restarting never relaxes the native lease or proves successful cancellation.

OpenCode live workers already share their managed server. This does not by itself
make a finished OpenCode orchestrator TUI a persistent shared app chat. Codex's
queue/app-server, Cline's background hub and Copilot ACP need separate adapters
and validation. Antigravity's headless streaming interface does not establish a
native TUI attachment contract. Those providers retain explicit handoff and the
synchronized SaberOps terminal fallback; universal simultaneous native/app chat
is not claimed. Offline tests cover routing, deduplication, scope, channel protocol,
uncertain delivery and dispatch recovery. Account-backed model responses still
require live validation of the selected Claude version and account.
Claude Code 2.1.292 was also checked locally with a new empty test session:
its channel confirmation was accepted, the MCP connection completed and Claude
logged channel notification registration. No model prompt was submitted, and
existing user sessions were left open.

The inspector has one Open all in tmux action, plus View tmux. Each agent card has
Add to tmux. The batch uses the existing scoped native launcher and deduplicates
provider/binding/session identity, so two orchestrator dispatches sharing a saved
conversation open once. Existing leased clients in tmux are selected and grouped;
an existing desktop client must be closed before it can be added. Old separate
client windows can be joined into the shared tiled agents window without replacing
their processes. These operations affect clients, not execution owners.

New clients are added as named panes in the shared saberops-clients session. A batch
attaches one desktop terminal; adding sessions reuses an attached workspace.
View tmux explicitly opens a terminal window without creating provider clients. Without a usable desktop terminal, feedback
reports the background-only state and manual attach command. Batch failures remain
visible per session; a partial batch never claims every provider opened.

## Native display and scrolling

All interactive provider launchers, including the live OpenCode attach path, use
the provider's own native interface. Headless flags inherited from the UI server
(`NO_COLOR`, color-forcing overrides, `CI` and `CLAUDE_CODE_SIMPLE`) are removed
only for those clients. A real terminal type is preserved; missing or `dumb`
types fall back to `xterm-256color`. Automated execution environments, subscription
authentication controls, session identity and permissions remain unchanged.

Resumed clients receive their PTY dimensions before execution is released, and
later resizes propagate through the terminal supervisor. ANSI output and keyboard
input pass through unchanged. tmux mouse support is enabled only for the shared
SaberOps client session, allowing pane selection and scrolling where supported by
the native CLI. For terminal history, Ctrl+B then [ enters copy mode, Page Up/Down
scrolls and Q returns to input. Ctrl+B then Z expands or restores the current pane.
Full-screen providers may keep their own history instead of tmux scrollback.

Claude orchestrator resumes include direct-chat guidance to use readable replies
rather than the automated reply/action JSON envelope. This affects future native
replies only; original provider history and SaberOps' structured dispatch parser
remain intact. Each provider still has its own interface and trust prompts; native
display access does not guarantee that every provider supports live attachment.
Already-running clients must be closed and reopened to receive updated environment
and startup settings. No provider prompt is needed for this display change.
