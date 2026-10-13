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

## Plans and billing posture

`orch plans show|validate` manages the plan registry; `orch posture
show|verify` manages billing posture. Plan data records which accounts
exist, which pools they hold, each pool's windows, whether exhausting a
pool could turn into spending (`overage_possible`), how many runs may
draw a pool at once (`max_concurrent`), and each route's billing
posture class (A/B/C/D/X) with policy URLs, owner verification date
and expiry TTL.

File location and overlay: the owner document is the control-plane
config (`$XDG_CONFIG_HOME/saberops/control_plane.json`); the packaged
default (`defaults/plans.json`) ships the owner's billing-posture facts
and is overlaid by the owner document field by field (the owner wins
per field). `plans show` marks each field's source (`packaged` |
`owner`). `verified_on` and `toggles_attested_on` come only from the
owner document: a packaged non-null value for either is a validation
error, because verification is the owner's attestation. `reserves` is
wholesale: the owner's list replaces the packaged list only when the
owner document carries the key. An owner document without plan
sections (version 1) loads exactly as before; writes are always version
2 and preserve every section.

Selector grammar: `provider` (e.g. `codex`), `provider/model` (e.g.
`codex/gpt-5.6-terra`), or `provider/<prefix>/*` with a trailing glob
only (e.g. `opencode/opencode-go/*`). Posture selectors may also be
`backend:DIRECT_API`. The most specific selector wins: exact
`provider/model`, then the longest `provider/<prefix>/*`, then
`provider`, then `backend:X`. Note: Claude Code's provider id is
`anthropic` (not `claude`), and there is currently no `openai` model
prefix in the static routing registry, so `opencode/openai/*` is a
forward-looking selector for ChatGPT-login models served through
OpenCode.

How to verify: `orch posture verify SELECTOR [--url URL]...
[--note TEXT] [--class A|B|C|D|X] [--date YYYY-MM-DD]` records the
owner's re-verification (default date today, local). The selector must
already exist in the effective postures unless `--class` is given (a
new selector needs its class stated); changing an existing class
requires an explicit `--class`. Verification writes only the owner
document, atomically, and prints the resulting status
(OK|EXPIRING|EXPIRED|UNVERIFIED|FORBIDDEN|ATTENDED_ONLY|METERED).

What UNVERIFIED/EXPIRED mean: posture fails closed. A route with no
record, no `verified_on`, or `verified_on + ttl_days` before today is
never admitted unattended; once admission is wired (M2-4b), such
routes stay unadmitted, so every route stays unadmitted until `orch
posture verify` has been run for it. `expires_on` is inclusive: the
record is valid through that day. Status `EXPIRING` warns 3 days
before expiry. Class X is never admitted; B is attended-only; D
(prepaid/metered keys) is never scheduled unattended.

## Jobs

A **job** is what a caller submits (requirements, not providers); a
**run** is one execution of it. A job has 1..n runs (preemption,
retry). Jobs wait in the owner DB until capacity allows: nothing
dispatches jobs until the scheduler (M2-4b) lands, so submitted jobs
stay QUEUED for now.

States: QUEUED → RUNNING → SUCCEEDED | FAILED | CANCELLED, and
RUNNING → QUEUED on preemption. QUEUED carries a waiting reason: NEW |
NO_CAPACITY | PROBE_PENDING | PREEMPTED | CONCURRENCY. Requirements no
route could ever satisfy, even with full capacity, store FAILED with
`NO_ROUTE_FOR_REQUIREMENTS` at submit, so jobs never park forever.

Requirements state what the work needs: `role` (ORCHESTRATOR, WORKER,
INTEGRATOR, SURGEON, REVIEWER; only WORKER is dispatchable until R-002
wires the role chains), optional `effort` (low | medium | high, a
reasoning-budget hint that never selects a chain), required factual
capabilities (plus `--read-only`), model-family allow/deny lists
(validated and stored; enforced at admission), and a `size_hint`
(S | M | L, shared with the capacity cost buckets). Callers never name
a provider, model, tier or billing route.

Idempotency: pass `--idempotency-key KEY` with a stable `caller_id`
(`owner` for now). The same key with the same request returns the
existing job (`created: false`, exit 0, no new row or event); the same
key with a different request is refused (exit 1, nothing written); no
key always creates a new job.

- `orch job submit --repo PATH --goal TEXT [--acceptance TEXT]
  [--base-ref REF] [--role ROLE] [--effort low|medium|high]
  [--capability NAME]... [--read-only] [--family-include F]...
  [--family-exclude F]... [--size S|M|L]
  [--priority URGENT|NORMAL|BACKGROUND] [--deadline ISO]
  [--not-before ISO] [--idempotency-key KEY] [--json]`, e.g.
  `orch job submit --repo ~/projects/foo --goal "Fix the login retry"
  --acceptance "gate passes" --size M --idempotency-key foo-1`.
  Exit 3 means stored but FAILED at submit
  (`NO_ROUTE_FOR_REQUIREMENTS`); the job is printed.
- `orch job get JOB_ID [--json]`, e.g. `orch job get <job-id> --json`.
- `orch job list [--state STATE] [--limit N] [--json]`, e.g.
  `orch job list --state QUEUED --limit 20 --json`, ordered URGENT,
  NORMAL, BACKGROUND, then oldest first.
- `orch job cancel JOB_ID [--json]`, e.g. `orch job cancel <job-id>`.
  QUEUED cancels at once; RUNNING first cancels the live run through
  the supervisor and only then marks the job CANCELLED.

## Main gate

`scripts/main-gate.sh` runs the full gate on `main` — by hand, before a wave, or
from the optional systemd timer — so it can run at any time and is not tied to
the night (ADR-0003 Q6). It also rebuilds the coverage map and keeps flaky
statistics. It is deterministic, runs no model, and is idempotent. Run it by
hand with `bash scripts/main-gate.sh` from the checkout. The timer is just one
optional way to run it: it fires daily at 03:30 (`Persistent=true`,
`RandomizedDelaySec=10m`); change the schedule in
`scripts/systemd/saberops-main-gate.timer` (`OnCalendar`) or remove it by
uninstalling the timer (below).

Slot behaviour: the gate itself runs `scripts/gate-full.sh`, which takes the
heavy slot then drains the light slots, so it waits for running lane checks
and G3; while the map rebuild runs under `gate_tools.py exclusive` (heavy +
all light slots held) new lane checks and G3 wait. No other command uses
`exclusive`.

Output: `~/.cache/saberops-gate/main-gate/<YYYY-MM-DD>-<shortsha>/` holds
`gate.txt`, `result/` (outcome.json, JUnit), `map.txt` (PASS or
quarantined-only FLAKY_PASS; never on FAIL, TIMEOUT or INFRA),
`flaky.md`, and `summary.md`. The summary is copied to
`$SABEROPS_ORCH_DIR/main-gate/latest.md` (default
`/home/saber/Projects/SaberOps-dogfood-0.3.7/.orch/main-gate/latest.md`); that
file is how the orchestrator reads the latest main gate. Output dirs older than
30 days are deleted, and only from a cache dir with a `main-gate` path component
(anything else is refused with a warning and skipped); nothing else is.
The script gates a detached `_main-gate` worktree of the repository and refuses
to touch MAIN's checkout, any attached worktree, or any other path (exit 2).
Skip rule: when `main` is unchanged since the
last green main gate entry the run logs `main unchanged since last green main
gate` and skips (no gate, no map); `SABEROPS_MAIN_GATE_REPEAT=1` forces the
repeat for flaky statistics. FAIL/INFRA never skip.

History and flaky report: every `full` gate appends one JSON line to
`<git common dir>/saberops-gate-history/full.jsonl` with `ts, head_sha,
worktree, label, outcome, reason, tests, failed, flaky, wall_time,
peak_bytes` (`SABEROPS_GATE_LABEL`, default `manual`; the main-gate script sets
`main-gate`; lane runs never write; write failures warn only). `python
scripts/gate_tools.py flaky-report [--min-streak N] [--json]` (default N=3)
lists streaks of consecutive non-INFRA full gates per test under `##
quarantine proposals` (per-test totals likewise count non-INFRA gates only;
ids already in `tests/quarantine.txt` marked) and the
newest main gate's failures under `## main-gate failures`. For compatibility a
history line labelled `nightly` (written by a timer installed before this
rename) is read exactly like a `main-gate` line. Quarantine stays
owner-approved: the report only proposes, never edits the file.

Install (Saber runs these; the lane never installs the timer):

```bash
mkdir -p ~/.config/systemd/user
cp scripts/systemd/saberops-main-gate.service scripts/systemd/saberops-main-gate.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now saberops-main-gate.timer
loginctl enable-linger  # so the timer runs while logged out
```

Uninstall: `systemctl --user disable --now saberops-main-gate.timer`, then
remove the two unit files and `systemctl --user daemon-reload`.

Run once by hand: `bash scripts/main-gate.sh` from the checkout.

## Fixed-source candidate verification
Candidate gates and independent reviews run in separate detached verification
worktrees at the exact candidate commit. Their tree hash is recorded in evidence.
Uncommitted candidate changes block snapshot creation; changes to the snapshot
or candidate during verification invalidate that result. This prevents terminal
edits in the implementation worktree from changing the code under an active
candidate gate or review. Acceptance remains tied to the verified commit and its
authoritative post-integration gate. A transient source edit cannot contaminate
a gate that runs against a separate fixed checkout.
