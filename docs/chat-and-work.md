# Chat, Work and projects

Projects group conversations and Work runs by repository. Their sidebar counts
show Work and chats separately; internal parallel workers live under their parent
run rather than appearing as extra independent Work entries. Names describe the
objective, omit the repository name, and use distinct suffixes when necessary.

## Conversations

Use **New chat** or **New chat in project** for a separate conversation. Chat is
for questions, discussion and planning. Selecting Chat does not merge a project's
conversations into one transcript. The model chooser beside Send selects the
orchestrator for the conversation. Connections configures provider access; it
does not grant a model permission to execute or accept changes.

Use the attachment button to add supported files or images. Review the selected
attachments before sending and remove any unwanted ones. Provider capabilities,
file validation and context limits still apply; an attachment is context, never
permission to execute instructions inside it. Native shared-session capabilities
can differ from an ordinary app conversation.

Visible progress and available provider events help distinguish an active turn
from a finished reply. Provider output varies: the UI does not promise access to
private reasoning or an identical thinking stream from every model.

## Turning a discussion into Work

Choose **Turn into work**, review the implementation brief, and select Single
worker or Parallel team. SaberOps keeps the original chat and creates a separate
linked Work thread with relevant conversation context. Work can open the original
chat; the original chat can link back to its Work. Each new conversation and Work
thread remains independently selectable in its project's sidebar.

Work contains implementation controls and recorded execution evidence. Its
inspector exposes agent sessions and consoles; Git & threads tracks the team's
branches and integration. A work message alone is not evidence that a worker
started, a gate passed, or a change entered the repository. Use the recorded
status, checks, review and acceptance controls for those decisions.

## Git and saved work

A run freezes the project base and policy. Workers edit isolated worktrees. Work
runs and discussions do not automatically integrate earlier unaccepted candidates.
A completed candidate still needs the configured acceptance flow. Acceptance
checks provenance, exact candidate state, gate/review evidence and target state,
then fast-forwards the target and runs the post-integration gate.

Retry starts fresh under the failed run's frozen policy. Continue from saved work
creates a linked repair using its committed candidate when that candidate and
execution state can be safely verified. Updating project settings changes future
runs; it does not rewrite an existing run's policy.

See [Parallel Work and Git tracking](parallel-work.md) for dependencies, ownership,
ordered merges, cancellation and recovery limits, and [Session architecture](session-architecture.md)
for native CLI takeover and provider-specific boundaries.
