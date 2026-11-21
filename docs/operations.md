# Operating notes

Details on routing, usage limits, and fixed-source candidate verification.
The [README](../README.md) covers installation and the overall model.
Run status is visible on the minimal status page (`orch ui`); owner
configuration lives in the `orch` CLI. Every command below supports
`--json` for machine-readable output (agents and scripts); human output
goes to stdout and refusals to stderr with a nonzero exit. Nothing is
persisted on a refusal.

## Owner binding selection (check / plan)

`orch reviewer show|select|clear` manages the owner's exact **check** binding;
`orch orchestrator show|select|clear` manages the **plan** binding policy. The
persisted choice is always a binding id, never free-floating provider/model
text. Only a role-qualified binding that exists in the canonical registry may
be selected: a `BUILD` (formerly `WORKER`) or `PLAN` (formerly `ORCHESTRATOR`)
binding submitted as a check binding (and vice versa) is refused without
mutation, as is an unknown id. Reviewer selection additionally
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

## Presence and the work gate

SaberOps starts work only while someone is present. Presence is an AI CLI
planning session connected over MCP, or an attended terminal session: `orch
watch` while a terminal is attached to its tmux session, or `orch run
--attended` while that command runs. With neither open, the scheduler dispatches
nothing -- jobs, plan lanes, retries, repairs and candidate refreshes all wait --
and running workers are parked at a safe point with their work preserved.
Closing the terminal or Ctrl-C ends presence within the heartbeat bound.

A fresh store is closed but not paused: `orch job submit` queues, and work
starts as soon as a session opens. Once a session has been seen and then lost,
the pause is sticky: work resumes only after the owner runs `orch resume`. No
MCP tool clears a pause. A plain `orch run` refuses to start while the gate is
closed; `orch run --attended` is the terminal form that holds presence itself.
See [ADR-0029](decisions/0029-no-work-without-a-planning-session.md).

## Run operations (retry / continue / refresh / try-worker)

`orch retry RUN_ID` starts a fresh attempt of a run's frozen task and
policy against the repository the run was created for (validated against
its persisted project identity, never a current selection).
`orch continue RUN_ID` continues a failed run from its immutable saved
candidate; a run that already has a continuation returns the existing id
without launching anything. `orch refresh RUN_ID` applies a stale
candidate's changes in a fresh isolated worktree based on the current
target commit, runs the gate and the required independent review for the
new SHA, and leaves the result for separate owner acceptance. It never
moves the target branch; the original candidate and its evidence stay
intact for repair if the refresh conflicts. `orch try-worker RUN_ID --binding ID`
explicitly retries a failed run with one blocked `UNKNOWN` worker: the
binding must name the exact skipped candidate from the run's termination
evidence and still be present in the current routing chain with the same
provider/model, and the frozen validation and acceptance policy are kept.
Retry, continue and try-worker launch through the durable supervisor like
`orch run`; refresh runs in the foreground and returns the new run id when
its checks and review finish. The new run id is printed with `orch wait` /
`orch status` hints.

`orch list [--repo PATH] [--limit N]` lists recent runs. Without `--db` it
reads the project's own store **and** the owner store for that project's runs,
because `orch scheduler run` and the MCP server write to the owner store; both
store paths are printed so an empty project store is never mistaken for "the
run is missing".

## Build routing policy

SaberOps has three roles (ADR-0005 §3): **plan** (the external CLI session the
owner sits in), **build** and **check**. One ordered **build** chain selects the
worker; each chain is a list of fallback groups. The build route also carries an
optional **`strong_start`** setting, used when a manual high-tier run
(`--tier T3`) or an escalation asks to start on a stronger model. It is a route
setting, not a role. Resolution evaluates
fallback groups in order: the first fallback group containing an admitted
candidate wins, and candidates within that winning group are prioritized
by their intra-group binding-window pacing score:
`pace = (remaining_pct - red_line_pct - reserve_pct - cost_estimate_pct) / max(hours_to_reset, 0.25)`.
Ties break deterministically by original chain order, then by lower estimated
cost. The classifier still labels the run tier (a risk/telemetry label), but
it no longer selects routes. Provider readiness, quota, capabilities, exact
bindings and owner model enrollment still apply. Schema 3 routing files hold `roles.BUILD` plus a top-level `strong_start`, each a
list of `{"routes": [...]}` groups. The check binding keeps its ordered list in
`orch reviewer show`, and the plan role is the connected agent: see `orch
orchestrator show` (`orch routing show` points there too).

### Scout route (F27)

Scout jobs (`kind: SCOUT`, ADR-0026) prefer the cheapest plan model of each
subscription already on the build route (ADR-0035): Codex `gpt-*-luna`, Claude
Haiku, and OpenCode Go `deepseek-v4*-flash`. It only reorders the owner's
candidates; every one remains a fallback and read-only admission is unchanged.
Override per project with `orch project set-scout-route <cheap|build|provider/model>`
(`orch project clear-scout-route` returns to `cheap`); a `size_hint: "L"` or
`effort: high|xhigh` job keeps the ordinary build route.

### Model-family tokens (cross-CLI proxy requests)

A job asks for one worker family with `requirements.model_family_include`;
`provider`/`model` stay refused (ADR-0001). Three tokens are supported
(ADR-0024): `anthropic` (the Claude Code worker, and nothing else), `codex`
(the Codex worker, and nothing else) and `opencode-go` (OpenCode workers whose
model is a Go model, `opencode-go/...`).

`claude` and `opencode` are ambiguous and are **not** supported tokens: `claude`
also matches a Go-hosted Claude model such as `opencode-go/claude-haiku-5-5`,
and `opencode` matches every OpenCode worker including ChatGPT-plan models.

Feasibility is checked at submit against the same build-route candidates with
the same matcher admission uses. An include no candidate satisfies is refused
immediately as `NO_ROUTE_FOR_REQUIREMENTS`, with the three supported tokens in
the guidance; it never parks as "no eligible candidate" later. A family request
does not add a candidate, so the build route must already contain that family.
`model_family_exclude` stays an admission filter. Once a worker attempt exists,
the `get_job` / `catch_up` result carries `result.worker` with the chosen
provider/model.

## Pacing scheduler (`orch scheduler run`)

`orch scheduler run [--once] [--interval-seconds INT] [--max-concurrent N]`
runs the deterministic pacing scheduler loop in the foreground.
`orch scheduler start` starts the same loop detached (one scheduler per owner
store) and returns; it is the command `preflight` and the first-run walkthrough
give an owner, so setup never has to occupy a terminal. Both start paths take
the shared scheduler lease, so a second call reports the running helper instead
of starting a second one. Queued jobs are
scheduled in strict priority order: `URGENT` > `NORMAL` > `BACKGROUND`, with
earliest deadline first, then FIFO by creation time. `BACKGROUND` jobs require
binding-window pace >= 4.0 %/h.

### Global aggregate concurrency ceiling (interim bridge)

The scheduler enforces a ceiling on concurrently **RUNNING top-level jobs**.
It counts durable RUNNING job state (including jobs with no
`capacity_reservations` row) plus same-tick simulated starts, so two jobs from
distinct pools both count against the same ceiling. Per-model turns inside an
already-running job are *not* separate jobs and are never counted.

The effective ceiling is the owner setting `concurrency.max_global` in the
control-plane config, or the deterministic interim default **15** when the
owner has not set it. The value is a positive integer; an invalid value (0,
negative, boolean, string, or a `concurrency` block without `max_global`) fails
closed at config load — it never degrades to unbounded.

`--max-concurrent N` can only **lower** the effective ceiling for that run; it
cannot raise it above the owner-configured value. Raising the ceiling is an
owner config/UI change.

**Scope limitation (honest):** this is aggregate admission only. It does **not**
enforce the owner's project-aware policy of *3 concurrently active projects ×
5 executing tasks per project*, because M2-4b has no durable registered project
identity or wave-child task identity yet. The status is:
`GLOBAL AGGREGATE IMPLEMENTED / PROJECT-AWARE DIMENSIONS DEFERRED`. This
ceiling is independent of, and does not replace, provider-pool maxima, quota
red lines, billing posture, gate-slot or host limits.

The scheduler uses a single-instance owner-DB lease (`capacity_scheduler_lease`)
with fencing tokens to ensure exactly one scheduler instance dispatches at a time.
Dispatches atomically move jobs `QUEUED` → `RUNNING`, reserve ledger capacity,
and reconcile terminal runs upon completion or failure. Run `--once` executes
a single evaluation tick and exits cleanly.

## Storage lifecycle (`orch gc`)

`orch gc --dry-run [--class <c>] [--json] [--repo PATH] [--db PATH]` inventories
the owned storage artifacts for a repository (attempt worktrees, engine local
branches, checkpoint refs, gate scratch, worktree venvs, evidence and the owner
database) and prints the exact scope a validated standing policy would make
eligible together with the exclusions and reasons. A dry run is a true no-op:
no database, WAL file, schema, directory or policy file is created and no write
lock is taken. Ownership comes from real persisted run/attempt/candidate/
execution-owner/job records; anything unexplained is kept as `UNKNOWN`.

`orch gc --apply` executes only the exact previewed scope, revalidating every
identity immediately before each action and refusing on any race (ownership
change, symlink/root change, moved branch tip, live consumer, pending
acceptance, dirty or uniquely-ignored content). `--apply` requires a standing
owner policy at `$XDG_CONFIG_HOME/saberops/storage_gc.json`; without one it is
refused. Retirement uses native `git worktree remove` (no force) and then
`git branch -d` (no force) only — never `git worktree prune`, `git branch -D`,
recursive deletion of an unverified path or a remote operation. Every action is
audited in `storage_cleanup_events` (intent/result/failure with exact artifact
identity, scoped policy, reason and estimated versus actual bytes).

`orch cleanup <run-id>` is the compatibility route and delegates to the same
guarded implementation, so it has the same protections and no prune/force
escape. `orch cleanup --landed [--repo PATH]` is the opt-in sweep for finished
work: it removes only clean attempt worktrees whose candidate is accepted and
still present in the target branch history, using the same non-forcing native
retirement. Failed, dirty, active, unaccepted and unlanded worktrees are kept
and reported with their age against a 14-day retention bound; that step never
runs automatically and never deletes those copies. The scheduler runs bounded storage maintenance on startup (before any
tick) and hourly, consults a per-tick launch guard, sweeps only provably
dead-owned gate scratch, runs the owner-DB `VACUUM` at most weekly above 20 %
free pages, and — on a below-floor or unknown reading — runs permitted GC then
rechecks the actual filesystem; if space is still low or unknown it parks as
typed INFRA and starts no worker or gate. The fixed `max(10 GB, 10 %)` floor is
enforced before every dispatch and on the gate path; adaptive admission is not
activated. Full details:
[docs/storage-lifecycle.md](storage-lifecycle.md).

## Capacity ledger inspection (`orch capacity show`)

`orch capacity show [--json]` provides read-only inspection of the capacity
ledger snapshot across configured provider quota pools and binding windows.
It reports remaining capacity estimates, active uncertainties, evidence sources
(`OWNER`, `PROBE`, or `EXHAUSTION`), staleness flags, and any active exclusion
reset times without creating or writing tables on read.

Migration: schema 1 merges exactly as before, then migrates 2 → 3 with the
build route as the stable union of T2, T1 and T3 (T2 order first) and the T3
order as `strong_start`. A document that still names the retired roles also
migrates on load: `WORKER` becomes `BUILD`, `SURGEON` becomes `strong_start`,
and `INTEGRATOR` is dropped because landing is a deterministic engine stage,
not a model role. A stored user file is never rewritten by a load; review the
migration with `orch routing migrate` (dry run by default, `--write` persists
with a timestamped backup), which prints the before/after mapping and what it
migrated. Role editors (`orch routing add|remove|move|reorder ROLE ...`, roles
case-insensitive) refuse schema-1/2 files until migrated and accept a retired
role name as `build`. `orch run --tier T1|T2` runs the build route without
escalation and `--tier T3` starts on its `strong_start` setting; `--max-tier
T1|T2` disables escalation (default T3 allows it). Each explicit flag prints
one deprecation line.

Escalation moves the build route to its `strong_start` setting, only on
explicit typed `CAPABILITY_FAILURE` evidence (gate failure never escalates),
only with `max_auto_tier T3`, auto routing and no model override. On escalation
the run's tier label becomes T3. A repair after a failed check stays on the
ordinary build route. One build-role worker deadline: 1200s (explicit
`--worker-timeout` wins). Frozen snapshots of runs from before this change keep
their tier provenance, role keys and digest exactly as before; adopted snapshots
re-emit byte-identically.

## Usage and account limits

`orch quota set|list|enable` manages provider-pool quota state; `orch models`
and `orch doctor` report local readiness. Account limits use the CLI's own
supported metadata interface where available. Local token activity
comes from numeric session records on this machine. Local activity is not a subscription bill
or an account-wide allowance. Missing counters remain unknown; large histories
are explicitly labeled as samples. Readiness refresh uses no prompts, reads no
credential files, and does not change saved routing quotas. An expired
per-window probe reading does not by itself refuse a route: a still-valid owner
reading for the same pool (24-hour freshness) is used until fresh evidence
arrives, and the refusal names stale evidence when no valid reading remains.

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

Expiry: confirmations are one-time by default (owner decision,
20 October 2026): a record without `ttl_days` never expires and holds
until the owner changes it. `orch posture verify SELECTOR --ttl-days N`
(1-90) opts a route back into expiry. The Accounts page confirms every
account at once ("Confirm all").

What UNVERIFIED/EXPIRED mean: posture fails closed. A route with no
record, no `verified_on`, or (when it has a `ttl_days`) `verified_on +
ttl_days` before today is never admitted unattended; once admission is wired (M2-4b), such
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
NO_CAPACITY | PROBE_PENDING | QUOTA_EVIDENCE_REQUIRED | PREEMPTED |
CONCURRENCY | MEMORY_LOW | DEPENDENCY | DEPENDENCY_FAILED. Requirements no
route could ever satisfy, even with full capacity, store FAILED with
`NO_ROUTE_FOR_REQUIREMENTS` at submit, so jobs never park forever.

`PROBE_PENDING` means a probe the engine runs by itself is in flight. A pool
whose usage can only be read interactively (Anthropic/Claude today) never uses
it: the job waits with `QUOTA_EVIDENCE_REQUIRED` instead, the `waiting` event
carries the exact `orch quota set <provider> <pool> --remaining <percent>`
command, and `wait_job` returns it as needs-attention immediately. The job
starts on the next scheduler tick after the evidence is recorded.

Dependencies: `depends_on` (CLI `--depends-on JOB_ID`, repeatable; MCP
`submit_job` `depends_on`) names jobs that must be **accepted** first, not
merely finished. The scheduler parks a dependant with `DEPENDENCY`, naming the
outstanding dependencies on its `waiting` event, until every one carries a
durable `run_accepted` event from the owner's acceptance. A dependant's run is
anchored on the newest accepted dependency's landing commit, not the base
recorded at submit time, so the worker starts from the tree that contains the
work it builds on. A dependency that
ends FAILED or CANCELLED parks the dependant with `DEPENDENCY_FAILED` for good:
it never starts, and `wait_job` returns it as needs-attention instead of
waiting out its timeout. Unknown dependencies and any dependency edge that
would close a cycle are refused at submit. Independent jobs keep dispatching in
parallel beside a parked dependant.

Requirements state what the work needs: `role` (`PLAN`, `BUILD` or `CHECK`;
only `BUILD` is dispatchable, and retired names `WORKER`/`SURGEON`/
`INTEGRATOR`/`REVIEWER` are accepted and migrated), optional `effort` (low |
medium | high, a reasoning-budget hint that never selects a chain), required
factual capabilities (plus `--read-only`), model-family allow/deny lists
(validated and stored; enforced at admission), and a `size_hint` (S | M | L,
shared with the capacity cost buckets). Callers never name a provider, model,
tier or billing route.

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
  The view names the newest candidate (`Candidate: <sha> (<run>)`) and, when
  that run is eligible now, the exact owner action (`Owner action: orch accept
  <run>`). It is the same projection `wait_job` and `get_job` return as
  `result.owner_action`, so a CLI reader and an agent cannot disagree.
- `orch job list [--state STATE] [--limit N] [--json]`, e.g.
  `orch job list --state QUEUED --limit 20 --json`, ordered URGENT,
  NORMAL, BACKGROUND, then oldest first.
- `orch job cancel JOB_ID [--json]`, e.g. `orch job cancel <job-id>`.
  QUEUED cancels at once; RUNNING first cancels the live run through
  the supervisor and only then marks the job CANCELLED.
- `orch cancel RUN_ID|JOB_ID [--json]` stops the execution owner directly.
  Both the run id and the job id printed by `orch job list` are accepted; a
  job reference resolves to its current (or newest recorded) run.

## Per-job worker evidence and project notes
Each run keeps a plain folder beside its store:
`<state dir>/jobs/<run_id>/` holds `request.md`, each attempt's exact
`attempt-N.prompt.md`, the project note that attempt received
(`attempt-N.note.md`), its `attempt-N.result.md`, and an `attempt-N.json`
manifest. The manifest carries the note's SHA-256 digest, the dispatch
evidence id, and the dispatch report's requested and provider-reported model
fields. A provider that supplied no identity reads **`unreported`**; the
requested model is never copied into the reported field.

Each project also has one short note handed to every worker attempt in a
`[PROJECT NOTES]` section:

```bash
orch project notes set --repo ~/projects/foo --file notes.md
orch project notes show --repo ~/projects/foo
orch project notes clear --repo ~/projects/foo
```

The note lives in the project state directory, outside the repository, so it
never enters a reviewed diff. Editing it changes the next attempt only; earlier
attempts keep their own snapshot and digest. See
[ADR-0021](decisions/0021-worker-evidence-and-project-notes.md).

## Watching running jobs (`orch watch`)

`orch watch [--repo PATH] [--job JOB_ID] [--session NAME] [--interval SECONDS]`
opens, or attaches to, a SaberOps-owned tmux session with one window per
RUNNING job and one summary window. Each job window follows the same read-only
tail as `tail_job` (event log plus terminal transcript) and prints route,
subscription, attempt and elapsed time. A job that starts later gets a window
on the summary window's next tick; a job that finishes keeps its window with a
final state and the exact next owner action, renamed with an `-ok`, `-failed`,
`-cancelled` or `-queued` suffix. The summary lists every job and the owner
pause state.

The view is read-only: the store is opened read-only and no window has a
worker input path, so keystrokes cannot reach a worker. Steering is unchanged
(`orch job note JOB_ID --text ...`). The session is marked with the tmux user
option `@saberops_watch=1`; a default name that belongs to another session is
skipped with a numeric suffix, and an explicit `--session` name that already
exists without the marker is refused rather than touched. Use `--once` to
print the summary without opening tmux. Without a usable tmux binary the
command falls back to `orch monitor` for one selected running job.

`orch wait RUN_ID` still blocks until a persisted state change; `watch` is a
separate command, not an alias.

## Vendor CLI updates and `orch self-update` (ADR-0031)

A vendor CLI update is detected on the next scheduler tick (also periodically,
default every 10 minutes). While the new version is unverified, no new job is
started on that vendor: queued work routes to another admitted candidate or
waits with `VENDOR_CLI_UNVERIFIED` naming the update. Running jobs are not
touched.

The automatic check runs the allowlisted `--help` guard from ADR-0018, then one
tiny smoke prompt through the vendor's own isolated worker adapter. Both pass:
the new version is recorded as verified and routing resumes with one notice. A
removed flag or a failed smoke parks the vendor and files exactly one repair job
on the SaberOps repository with the evidence; that job goes through the normal
gate and review, and the owner accepts it explicitly.

```bash
orch vendor status                 # verdict, versions and the smoke setting
orch vendor status --json
orch vendor check [--vendor opencode] [--force]
orch vendor smoke on|off           # automatic smoke prompt (on by default)
orch vendor pin codex "codex-cli 0.170.0"   # warn instead of adapting
orch vendor unpin codex
orch vendor smoke-model opencode deepseek-v4-flash
```

After an accepted repair (or any accepted engine change), reinstall the daily
build from that exact commit:

```bash
orch self-update --target main                 # tip of main
orch self-update --commit <sha> --json
```

`self-update` refuses a commit that is not on the target branch, a commit with
no passing `full` gate receipt for the exact SHA, a dirty checkout, or a
checkout that is not at that commit. It never pushes. Running workers are
parked through the same safe pause path as `orch pause` and resumed after the
reinstall, so no worktree, checkpoint or job is discarded; the scheduler
re-execs into the new build, a running MCP server finishes its current call and
exits, and the command reports the old and new `orch --version`.

## Phase 1 benchmark (`scripts/benchmark.py`)

Ten fixed small tasks measure the full job, scheduler, worker, gate, review and
acceptance path and print one results table: final state, accepted commit,
submit-to-start, time to the accepted commit, completion-notice delay, owner
actions and the subscription used.

- **Fake mode** (default) runs the lifecycle rig's fake worker and reviewer
  inside the test suite: `scripts/benchmark.py --mode fake`. It also covers five
  failure drills (killed worker, killed scheduler, exhausted quota, unroutable
  job, duplicate submission) and a combined recovery drill in which the planner
  closes, a fake worker dies, the scheduler restarts and a fresh MCP session's
  `catch_up` reports the failed job, its reason and the preserved work location.
- **Real mode** drives real Codex, Claude and OpenCode Go CLIs and is run by
  hand with the owner present (lane B3). It runs the ten-task catalogue once per
  CLI — three sequential batches of ten, each on freshly seeded toy
  repositories — and selects a reviewer from a different vendor for every batch
  (`--dry-run` prints the worker/reviewer routes). It refuses without
  `--yes-real-cli` and an empty isolated `--store`, never reuses the owner's
  real SaberOps state or config, drops pay-per-token credentials from the child
  environment, and refuses to submit until the isolated store's preflight is
  green. It holds an attended-terminal session for the whole run and disables
  the detached auto-started scheduler, so exactly one scheduler driver ticks;
  a sticky pause is cleared only when it is the engine's own
  `no_planning_session` pause, never an owner pause. `XDG_DATA_HOME` is left
  alone because OpenCode keeps its sign-in there. The toy seed carries
  `greet(name)`, a CLI entry point and a `.gitignore` (so the gate's import
  leaves the worktree clean); only `fix-failing-test` starts with a failing
  test. Models are configurable; the defaults are OpenCode Go
  `opencode-go/deepseek-v4.1-flash`, Claude `claude-haiku-4-5-20251001` and
  Codex `gpt-6-luna`. Use `--dry-run` to print the plan without starting
  anything.

The critical-only drill raises the catalogue's `routine-question` task (its goal
asks the worker for one routine choice), so F10 self-answering is measured from
the catalogue rather than from an ad-hoc injected question. The benchmark tests
are marked `slow`: the full gate runs them; the per-change gate may skip that
marker.

## Main gate

`scripts/main-gate.sh` runs the full gate on `main` — by hand, before a wave, or
from the optional systemd timer — so it can run at any time and is not tied to
the night. It also rebuilds the coverage map and keeps flaky
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

## Engine-run gates (G1 / G2 / G3) and the history ledger

The engine is the only producer of authoritative gate evidence.
`Orchestrator.run_engine_gate(...)` (module seam `saberops.orchestrator.
run_engine_gate`) runs one profile against one repository and records typed
evidence:

- **Profiles are project data** (`saberops.gate_profiles`). A project may ship
  `gate-profiles.json` in its state directory to override commands and budgets:
  `fast` (lint + types + selected tests, 10 min), `acceptance` (`fast` + rerun
  rule + test-map freshness, 20 min) and `full` (lint + types + whole suite,
  30 min). A caller may request a *stronger* profile than the lifecycle kind
  requires but never a weaker one; asking for `fast` on a G3 raises
  `GateProfileError` and the gate does not run. An optional
  `selector_command` is a project selector plugin whose stdout node IDs fill the
  `{selector}` placeholder.
- **Slots** come from the `host-gates` local capacity pool (`GateSlotPool`):
  the exclusive heavy slot for G3/main-gate runs (which also drain all light
  slots) and one of two light slots for G1/G2, using the same flock files as
  `scripts/gate_tools.py`, so engine and shell gates never run past each other.
- **Write-scope and retirement preflight** (`saberops.gate_scope`) runs before a
  G1/G2 command: changed files must be inside the lane Touches and outside
  must-not-touch, and no test may be removed or rewritten unless authorized by
  the lane's retirement table. Both fail closed as engine-raised findings
  (`WRITE_SCOPE_VIOLATION`, `TEST_RETIREMENT_VIOLATION`) with no command run.
- **Disk admission** enforces the shared free-space floor and,
  when the Step 008 storage ledger is available, the gate's declared estimate
  through `host-disk` admission.
- **Evidence** is written to the run's evidence directory as `engine-outcome.json`
  and appended, once, to `<git-common-dir>/saberops-gate-history/full.jsonl`
  with `kind` (`G1|G2|G3|main-gate|manual`) and `producer: engine`. Model-run
  commands are G0: `project_gate_outcome` rejects any explicit non-engine
  producer, and `gate_history.require_engine_evidence` refuses to treat it as
  evidence.

`gate_tools.py` still writes `outcome.json` for human/worker runs (producer
`developer`), and its `kind`/`producer`/`profile`/`checks_ok`/`selected` fields
are what the engine reads when a profile command is one of the repository gate
scripts. When the engine invokes a script it sets `SABEROPS_GATE_EXTERNAL_SLOT=1`
(no nested slot acquisition) and `SABEROPS_GATE_NO_HISTORY=1` (the engine writes
the single history line).

## Fixed-source candidate verification
Candidate gates and independent reviews run in separate detached verification
worktrees at the exact candidate commit. Their tree hash is recorded in evidence.
Uncommitted candidate changes block snapshot creation; changes to the snapshot
or candidate during verification invalidate that result. This prevents terminal
edits in the implementation worktree from changing the code under an active
candidate gate or review. Acceptance remains tied to the verified commit and its
authoritative post-integration gate. A transient source edit cannot contaminate
a gate that runs against a separate fixed checkout.
