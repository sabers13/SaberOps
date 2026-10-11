# Operating notes

Details on routing, usage limits, and fixed-source candidate verification.
The [README](../README.md) covers installation and the overall model.
Run status is visible on the minimal status page (`orch ui`); owner
configuration lives in the `orch` CLI. Every command below supports
`--json` for machine-readable output (agents and scripts); human output
goes to stdout and refusals to stderr with a nonzero exit. Nothing is
persisted on a refusal.

## Owner binding selection (reviewer / orchestrator)

`orch reviewer show|select|clear` manages the owner's exact reviewer
binding; `orch orchestrator show|select|clear` manages the orchestrator
binding policy. The persisted choice is always a binding id, never
free-floating provider/model text. Only a role-qualified binding that
exists in the canonical registry may be selected: a `WORKER` or
`ORCHESTRATOR` binding submitted as reviewer (and vice versa) is refused
without mutation, as is an unknown id. Reviewer selection additionally
proves the exact binding through the real adapter; orchestrator `PINNED`
proves policy-resolvability (role, enabled profile, quota) while live
executability is confirmed at dispatch, which fails closed. Orchestrator
`--auto` resolves through the existing routing authority; an
unresolvable policy is refused and nothing is saved. Clearing the
reviewer restores the legacy review ladder.

## Model-access connections

`orch access list|show|add|test|discover|verify-model` manages
model-access connections. Only credential *references* (`env:` /
`profile:` / `store:` handles) are ever persisted; there is no field for
a raw secret value, and error text redacts the submitted reference.
`test` runs only the declared non-inference status check (`UNKNOWN`
stays `UNKNOWN`; a present CLI is availability, never proven readiness)
and persists the classified evidence; unsupported transports keep the
unauthenticated reachability probe, which never reads the stored
credential, let alone transmits it. `discover` is evidence only: it
writes the non-secret model catalog and can never rewrite durable
connection configuration, and a failed refresh keeps prior evidence
marked stale instead of inventing models. Adding a connection runs one
bounded discovery immediately without ever failing the save.
`verify-model` is the explicit owner-authorized bounded model check for
a supported account connection and an exact current catalog model; it
never runs implicitly and never backs an automatic fallback.

## Workflow presets

`orch project set-workflow quick|balanced|thorough|max|custom --repo X`
persists the project's workflow preset: `quick` disables the gate and
review, `balanced` / `thorough` / `max` freeze their canonical
gate/review/mode/limit tuples. A non-quick preset on a project with no
effective gate requires `--gate-command` (or `quick`, which needs none).
`custom` persists the explicit `--gate-enabled|--gate-disabled`,
`--review`, `--mode` and `--limit` flags. Runs freeze the effective
policy at creation; changing the preset affects future runs only.

## Run operations (retry / continue / try-worker)

`orch retry RUN_ID` starts a fresh attempt of a run's frozen task and
policy against the repository the run was created for (validated against
its persisted project identity, never a current selection).
`orch continue RUN_ID` continues a failed run from its immutable saved
candidate; a run that already has a continuation returns the existing id
without launching anything. `orch try-worker RUN_ID --binding ID`
explicitly retries a failed run with one blocked `UNKNOWN` worker: the
binding must name the exact skipped candidate from the run's termination
evidence and still be present in the current routing chain with the same
provider/model, and the frozen validation and acceptance policy are kept.
All three launch through the durable supervisor like `orch run`; the new
run id is printed with `orch wait` / `orch status` hints.

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
