# Operating notes

Details on routing, usage limits, and fixed-source candidate verification.
The [README](../README.md) covers installation and the overall model.
Run status is visible on the minimal status page (`orch ui`); owner
configuration lives in the `orch` CLI.

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

`orch quota set|list|enable` manages provider-pool quota state; `orch models`
and `orch doctor` report local readiness. Account limits use the CLI's own
supported metadata interface where available. Local token activity
comes from numeric session records on this machine. Local activity is not a subscription bill
or an account-wide allowance. Missing counters remain unknown; large histories
are explicitly labeled as samples. Readiness refresh uses no prompts, reads no
credential files, and does not change saved routing quotas.

Native limit interfaces: [Codex app server](https://developers.openai.com/codex/app-server)
and [Copilot usage and billing metrics](https://docs.github.com/en/copilot/how-tos/copilot-sdk/features/usage-and-billing).
On POSIX systems, Claude Code and Antigravity account limits are read through
their native [/usage](https://code.claude.com/docs/en/costs)
[panels](https://antigravity.google/docs/cli/commands/usage). Each bounded query
uses an isolated empty workspace and submits only `/usage`, without inference.
`pyte` reconstructs the terminal screen; only quota percentages and displayed
reset labels enter usage evidence. CLI layout changes or login dialogs can make
the query unavailable; the last-known observation is retained with
its original timestamp. Cline and OpenCode providers other than Go still need
their own account quota interface.

OpenCode Go rolling, weekly and monthly account percentages are queried through
an ephemeral OpenCode auth plugin while running `opencode models opencode-go`.
The CLI supplies its saved Go authentication to the plugin; SaberOps receives
only percentages and reset dates. The read-only endpoint is implemented in
[OpenCode's own source](https://github.com/anomalyco/opencode/blob/dev/packages/console/app/src/routes/zen/go/v1/usage.ts).
No inference runs or permanent plugin/configuration changes are made. Connect
Go in OpenCode first; missing connections, rejected keys, and missing Go
subscriptions are reported separately.

## Fixed-source candidate verification

Candidate gates and independent reviews run in separate detached verification
worktrees at the exact candidate commit. Their tree hash is recorded in evidence.
Uncommitted candidate changes block snapshot creation; changes to the snapshot
or candidate during verification invalidate that result. This prevents terminal
edits in the implementation worktree from changing the code under an active
candidate gate or review. Acceptance remains tied to the verified commit and its
authoritative post-integration gate. A transient source edit cannot contaminate
a gate that runs against a separate fixed checkout.
