# MCP interface (Step 014 / M3)

SaberOps exposes the **Orchestrator contract** as a Model
Context Protocol (MCP) server speaking JSON-RPC 2.0 over standard I/O. The
implementation is `saberops.mcp`; it is a thin adapter: every tool validates its
arguments, then calls an existing service function, and holds no authority of
its own. "LLMs advise; deterministic software decides."

```bash
# Serve over stdio (the default action):
orch mcp serve
orch mcp                      # same as `serve`
orch mcp --transport stdio    # same, explicit transport
orch mcp tools                # print the exposed tool names
orch mcp tools --json         # print names + JSON input schemas
```

| Option | Meaning |
| --- | --- |
| `--db PATH` | Owner database path (default: the canonical SaberOps state DB). |
| `--lease PATH` | Orchestrator lease file (default: `<state>/mcp-orchestrator.lock`). |
| `--ledger PATH` | Append-only event ledger (default: `<state>/mcp-events.jsonl`). |
| `--questions-store PATH` | Owner question document (default: `<state>/questions.json`). |
| `--config-ledger PATH` | Configuration decision ledger (default: `<state>/config-ledger.jsonl`). |
| `--cwd PATH` | Base directory used to discover the `.orch` / `.saberops` evidence roots. |

Server identity: `name = "saberops"`, `version = "0.4.0"`. The newest protocol
revision implemented is `2025-06-18`; `2025-03-26` and `2024-11-05` are also
served, and an unknown requested revision falls back to `2025-06-18`.

## Lifecycle

| Method | Kind | Notes |
| --- | --- | --- |
| `initialize` | request | Returns `protocolVersion`, `capabilities` (`tools`, `resources`), `serverInfo`, `snapshotSchemaVersion: 1`, `readOnly` and the lease status. |
| `notifications/initialized` | notification | Completes the handshake; no response is sent. |
| `ping` | request | Health check; allowed before or after the handshake. |
| `tools/list` | request | The currently exposed tools with their JSON input schemas. |
| `tools/call` | request | Invoke one tool by name with an arguments object. |
| `resources/list` | request | Synthetic resources plus discovered evidence files. |
| `resources/read` | request | Read one resource by URI. |

`tools/*` and `resources/*` are refused with JSON-RPC `-32600` until
`initialize` + `notifications/initialized` have completed.

## First run, in plain words

On a fresh store, turning SaberOps on opens with a short introduction and then
one setup step at a time, each derived from `preflight`. `orch welcome` prints
the same text in a terminal (`--json` for the structured form), and the MCP
`start_walkthrough` tool returns it to the agent. The agent may run a routine
step (`kind: agent`) after the owner agrees. Steps marked `owner` — confirming a
plan, signing in to a CLI, allowing or blocking a provider, freeing disk space,
updating a CLI — are handed to the owner with the exact command and never run by
the agent. A step marked `answer` needs one value from the owner (a check
command, a remaining plan amount) and the agent never invents it. Progress is
derived from the live preflight, so an interrupted walkthrough resumes at the
first missing step with no stored cursor.

## The job and administration tools

| Tool | Mutating | Key arguments | Calls |
| --- | --- | --- | --- |
| `submit_job` | yes | `repo`, `goal`, `acceptance`, `base_ref`, `priority`, `kind`, `deadline`, `not_before`, `depends_on`, `idempotency_key`, `requirements` | `saberops.jobs.service.submit_job` |
| `submit_plan` | yes | `repo`, `plan_path`, `base_ref` | `saberops.plan_runner.submit_plan_file` (path-only, repository-confined) |
| `promote_scout` | yes | `job_id`, `goal`, `acceptance`, `base_ref`, `idempotency_key` | `saberops.jobs.service.promote_scout` |
| `get_job` | no | `job_id` | `saberops.jobs.service.get_job` |
| `catch_up` | no | `job_id` or `plan_id` | read-only handoff over saved job, run, check, review and question records; with `plan_id`, the plan header, lane table and per-lane open questions |
| `preflight` | no | `repo` | `saberops.preflight.collect_preflight` |
| `start_walkthrough` | no | `repo` | `saberops.walkthrough.build_walkthrough`: the plain-language introduction and one setup step at a time, with check marks |
| `list_jobs` | no | `state`, `limit` | `saberops.jobs.service.list_jobs` |
| `plan_status` | no | `plan_id` or `repo` | `saberops.plan_state.project_plan_state` (the exact `STATE.md` projection) |
| `cancel_job` | yes | `job_id`, `reason` | `saberops.jobs.service.cancel_job` |
| `note_job` | yes | `job_id`, `text` | `saberops.jobs.service.note_job` |
| `capacity` | no | — | `saberops.jobs.service.capacity` |
| `read_evidence` | no | `path` | guarded evidence reader |
| `list_questions` | no | `status`, `run_id` | `saberops.questions.OwnerQuestionQueue.questions` |
| `answer_question` | yes | `question_id_or_key`, `answer_text`, `authority` | `saberops.questions.OwnerQuestionQueue.answer` |
| `propose_config_change` | yes | `changes`, `rationale`, `proposed_by` | `saberops.config.propose_config_change` |
| `list_proposals` | no | — | `saberops.config.ConfigLedger.entries` |
| `get_mode` | no | — | `saberops.owner_mode.mode_view` |
| `propose_mode_change` | yes | `mode`, `until`, `rationale`, `proposed_by` | raises an owner question; never changes the mode |
| `get_pause` | no | — | `saberops.runtime_gate.gate_view` |
| `resume_work` | yes | — | `saberops.pause_work.resume_work`; the one confirmation that clears a pause |

`submit_job` accepts **requirements only**. `provider`, `model`, `tier`,
`billing`, `route`, `quota` and `account` are rejected at the top level and
inside `requirements` (ADR-0001 invariant 3), and the tool schema sets
`additionalProperties: false`, so an unknown field is never silently ignored.

### Asking for a worker family

A job selects a worker only through `requirements.model_family_include` (and
`model_family_exclude`). Three family tokens are supported for cross-CLI proxy
requests: `anthropic`, `codex` and `opencode-go` (ADR-0024).

| Token | Selects | Use |
| --- | --- | --- |
| `anthropic` | the Claude Code worker, and nothing else | **supported** |
| `codex` | the Codex worker, and nothing else | **supported** |
| `opencode-go` | OpenCode workers whose model is a Go model | **supported** |
| `claude` | the Claude Code worker **and** a Go-hosted Claude model such as `opencode-go/claude-haiku-5-5` | ambiguous, not supported |
| `opencode` | every OpenCode worker, including ChatGPT-plan models | ambiguous, not supported |

`claude` and `opencode` are not supported tokens: they silently match more than
one subscription. Only `anthropic`, `codex` and `opencode-go` are.

A non-empty `model_family_include` is checked at submit against the build route
with the same matcher admission uses. When no build-route candidate matches, the
job is refused immediately as `FAILED` / `NO_ROUTE_FOR_REQUIREMENTS` (with the
three supported tokens in `guidance`) instead of being accepted and parking with
no eligible candidate. A family request never adds a candidate: the owner's
build route must already contain that family.

The result projection that `get_job`, `wait_job`, `submit_job` and `catch_up`
return carries `result.worker` with the chosen `provider`, `model` and
`provider/model` route once a worker attempt exists, so a proxy can report
"ran on `anthropic/<model>`" rather than just "succeeded".

For parallel work, submit one `submit_job` per independent task, with a
distinct `idempotency_key` and non-overlapping scope. If an accepted sibling
makes another job stale, request `refresh_candidate` with its run ID (or use
`orch refresh <run-id>`). The owner accepts the newly checked and reviewed
candidate separately.

A job that needs an earlier job's result names it in `depends_on` instead of
being submitted later by hand: it stays QUEUED with the `DEPENDENCY` reason and
starts only after the owner accepts every dependency. A dependency that fails
or is cancelled parks the dependant with `DEPENDENCY_FAILED`, which `wait_job`
returns as needs-attention; cancel or resubmit it.

`submit_plan` is the plan-file form of the same submission. It reads a tracked
`PLAN.md` from inside the repository (the path is resolved, symlink-escaped and
size-capped; the bytes are never sent over the wire) and submits one ordinary
job per lane whose dependencies are already accepted. Independent lanes run in
parallel; a `run: "owner"` lane is never dispatched and waits for the owner's
`orch plan-file done`. A ready lane starts only while a live planning session
holds the planning lease **and** the typed owner-mode decision
(`owner_mode.decide_owner_action(DISPATCH)`) allows a start; a mode can only
narrow the authority preset, never widen it. Ask-me still allows a start (it is
not an owner judgment); `ask_before_actions` refuses it with the reason shown in
`STATE.md`. Closing the planning CLI pauses lane submission. In `away` or
`critical-only` with a full-autonomy preset, a lane candidate that passed its
gate and an independent review of that exact commit is auto-accepted as a local
commit (never pushed) and `STATE.md` shows the undo command; in ask-me, or with
a preset that does not grant auto-accept, `away` parks at the first candidate
and the next owner action names that accept instead of looking stalled.

### Scout jobs (F19)

`kind: SCOUT` submits a read-only investigation instead of a change. Roles stay
PLAN / BUILD / CHECK: a scout is a kind on the build route, and the engine
enforces read-only rather than trusting `requirements.read_only`. The worker
runs with the CLI's own read-only permission rules on the run's scratch copy;
any diff is discarded and recorded as a violation on the job. The deliverable
is a Markdown report in the job's E4 folder (`jobs/<run_id>/scout-report.md`),
indexed on the job record.

A scout completes when a non-empty report exists. It has no gate, no independent
review and no owner acceptance, so `orch accept` never applies. `get_job`,
`wait_job` and `catch_up` return a `report` block (path, short summary, findings,
and any violations); each decision in the report's `## Owner decisions` section
becomes an owner question. `promote_scout` starts an ordinary build job whose
goal carries the report as starting context and records the lineage on both
jobs. A plan lane may also set `kind: "scout"`: its report becomes starting
context for the lanes that depend on it.

By default a scout prefers the cheapest plan model of each subscription already
on the build route (Codex `gpt-*-luna`, Claude Haiku, OpenCode Go
`deepseek-v4*-flash`); every other candidate stays a fallback, and read-only
admission is unchanged (F27, ADR-0035). Override it per project with
`orch project set-scout-route <cheap|build|provider/model>` (and
`clear-scout-route`), or per job with `size_hint: "L"` or `effort: high` /
`xhigh` to keep the ordinary build route. The chosen route and per-job usage are
shown by `get_job` and `catch_up` like any other job.

`plan_status` returns the record-derived plan ledger: one row per lane with its
computed status (`planned`, `owner`, `ready`, `queued`, `running`,
`needs-attention`, `candidate`, `stale`, `refresh-failed`, `accepted`, `failed`,
`blocked`, `cancelled`, `superseded`), the final attempt's worker and model,
branch, commit and evidence identifiers, plus the ready set, held lanes,
deterministic landing queue and the next owner action. It is exactly the
projection rendered into
`STATE.md`, so the file and the tool cannot disagree. The tool is read-only: it
takes no lease, writes no file and acts on nothing. The canonical file lives in
the owner state directory and the checkout's `STATE.md` is a git-ignored
symlink created by `orch plan-file adopt`; it is never committed and never
created in a worker worktree, so `git status --porcelain` stays empty and
acceptance keeps working.

The landing queue is ordered by wave, then lane id. Accepting one sibling makes
the others stale; the queue names each stale candidate's exact
`orch refresh <run>` command. The owner may turn on bounded refresh with
`orch plan-file auto-refresh on` (an owner-only CLI verb, never MCP): at most
one stale sibling per scheduler tick is rebuilt through the same isolated
refresh, re-gated and independently re-reviewed, and recorded as
`plan_lane_refreshed`; it is never accepted and the target is never pushed. A
failed refresh shows as `refresh-failed` with its error and stops the queue
until the pair changes, rather than retrying every tick. With auto-refresh off,
`STATE.md` and the next action give the exact manual command.

`catch_up` is the read-only handoff for a new planning session. It returns the
original request, the current candidate commit, the latest gate check and
review **with the commit each assessed**, any open question, the preserved work
location on failure, C5's `result` projection (including the exact
`orch accept RUN_ID` when eligible), the durable away-mode `return_summary`
(rebuilt from the away journal, the same object the notifier sends, with a
`plans` section: accepted, running, parked and waiting-on-owner lanes plus the
next action) and the one next action the engine allows. Pass `plan_id` instead
of `job_id` to receive the plan header, lane table and per-lane open questions,
so a fresh CLI can take over a plan without the planner that submitted it.
It changes nothing, sends no notice, takes no lease, and is safe from a
read-only second client. Owners reject a candidate from the run page
(`/runs/<run_id>`).

The scheduler tick sends three plan-level notices, each once per key, from the
same projection as `STATE.md`: `plan:<id>:complete` when every lane is accepted,
done or removed; `plan:<id>:stalled:<lane>` when a lane failed or needs
attention; and, in `away` or `critical-only`, `plan:<id>:needs-decision:<lane>`
when no lane can move and a candidate (or owner lane) waits for the owner. A
lane start sends no notice; per-job terminal notices already cover it.

## Steering a running job

`note_job` (or `orch job note`) records one durable owner correction on a job
(ADR-0016). It is **not** typed into a running worker: the note is delivered
once, inside the prompt of the job's next worker attempt — a retry, repair or
continuation — and the attempt, run and time are recorded on the note. A job
that finishes before another attempt keeps the note pending. `catch_up` and
`orch job notes` list every note with its delivery state.

An optional advisor (ADR-0034) records its checkpoint advice through the same
channel with `source: "advisor"`; the prompt and the note views label it as
advice rather than owner authority. Set it per project with
`orch project advisor set <provider> <model> [--calls N]` and turn it off with
`orch project advisor off`; it is off by default. Its route, call budget, calls
and advice appear in the `advisor` block of `get_job`, `tail_job` and
`catch_up`, and its calls are counted in per-job usage.

`answer_question` accepts only `authority: "PLAN"` (legacy `ORCHESTRATOR` is
accepted and mapped). Owner authority is not exercisable over MCP:
`authority: "OWNER"` is refused with `-32010`, and a question whose decision
class is owner-only is refused the same way. A planning session can never answer
its own finding, and answers are idempotent --
repeating an identical answer records a confirmation and changes nothing.

`list_questions` returns a derived `critical` tag and its reasons for every
question. Critical is the union of an owner-only or acceptance class, a
never-delegable kind, and an explicit orchestrator escalation; it can only be
added. In `ask-me`, every question waits for the owner. In `critical-only` or
`away`, a live planning session may answer a routine (`critical: false`)
question with PLAN authority, evidence and a rejected alternative; the answer
is logged and appears in `catch_up`. A critical question is refused to the
planning session in every mode, notifies the owner and waits. With no live
planning session no self-answer is possible and no background planner starts
(ADR-0027).

`propose_config_change` validates the change and returns an approval-ready
proposal. It **applies nothing**: only the owner approving through
`orch config approve <id>` or the local web surface writes the config ledger.

`get_mode` is read-only: it returns the effective owner mode, the authority
preset it intersects, and every decision the mode permits or parks. A mode only
narrows the authority preset. `propose_mode_change` raises an owner question and
**never flips the mode**; only the owner sets it with `orch mode set` or the
Questions page (ADR-0020).

`get_pause` is read-only: it reports the work gate -- running, or paused with
its reason, the plan and worker state, the paused jobs and the one call that
resumes. Closing the planning CLI pauses all work in every mode (ADR-0029).
`resume_work` is the only confirmation that clears a pause; it is mutating, so a
read-only client cannot call it. Nothing resumes by itself: a returning session
sees the pause in `catch_up` and must confirm with `resume_work` or the owner
must run `orch resume`.

## Never exposed

The MCP contract and REHEARSAL §3 pin a list of authorities that MCP must never
reach. The registry refuses each one *before* any handler runs, with JSON-RPC
error code `-32010` and a message naming the invariant:

| Refused operation (and aliases) | Why |
| --- | --- |
| `approve_config_proposal`, `config_approve`, `record_config_decision` | Approving a config proposal is owner-only. |
| `merge_main`, `move_main`, `fast_forward_main`, `merge_ff_only` | Moving `main` is owner-only. |
| `push`, `git_push`, `push_remote` | Pushing is owner-only. |
| `approve_quarantine`, `quarantine_approve` | Quarantine approval is owner-only. |
| `attest_posture`, `posture_attest` | Posture attestation is owner-only. |
| `run_gate`, `gate_run`, `gate_full`, `run_full_gate` | Gate execution belongs to the deterministic engine scripts, not to an agent. |
| `edit_source`, `write_file`, `apply_patch` | No direct tracked-source edits over MCP. |
| `delete_path`, `delete_outside_orch`, `remove_path` | Deletions outside `.orch`/`.saberops` are owner-only. |
| `read_credentials`, `get_credentials`, `read_secret`, `read_token` | Read tools never return credentials. |

Refusals are JSON-RPC **errors**; an ordinary tool execution failure is an
`isError: true` result whose `structuredContent.error` carries the code and
detail. A client can therefore always distinguish "this authority is not
offered" from "this call failed".

### Read-boundary redaction

Every value a client receives -- tool results, resources, the snapshot -- passes
through `saberops.mcp.redaction.redact_secrets`. A mapping key whose normalized
name contains a secret marker (`token`, `secret`, `api_key`, `credential`,
`authorization`, `password`, `privatekey`, ...) has its value replaced with
`[REDACTED]`, and strings lose embedded credential shapes (`sk-…`, `ghp_…`,
`AIza…`, `Bearer …`, PEM private-key headers, ...). No read tool can echo a
credential or a secret reference's value.

## Single-writer Orchestrator lease

Exactly one MCP client may be the Orchestrator **writer** at a time; every other
live client is **read-only**. The lease is a small JSON record guarded by an
exclusive `flock` during each read-modify-write.

* The first client to acquire becomes the writer; the lease is acquired during
  `initialize` and released at EOF.
* A second live client (a different, live `pid` holds the record) is read-only:
  calls to `submit_job`, `submit_plan`, `promote_scout`, `cancel_job`,
  `note_job`, `answer_question`, `propose_config_change`, `propose_mode_change`
  and `resume_work` are refused with JSON-RPC error `-32020`. Its read tools
  (`get_job`, `catch_up`, `list_jobs`, `capacity`, `read_evidence`,
  `list_questions`, `list_proposals`, `get_mode`, `get_pause`, `start_walkthrough`)
  keep working.
* **Stale self-healing:** a record whose `pid` is dead is stale and is stolen by
  the next client (`os.kill(pid, 0)` is the liveness probe), so a crashed client
  never locks out future writers. A non-empty but unreadable/malformed record is
  treated as stale for the same reason -- a corrupt lock file must not
  permanently disable the writer role.
* Dead-PID detection is deliberately optimistic: a *live* PID is respected. The
  failure mode of respecting a recycled PID is a read-only session (safe); the
  failure mode of stealing a live lease is two writers.

The authority refusal happens **before** argument validation, so a read-only
client always gets the explicit lease error rather than a schema error.

## Versioned read snapshot and event ledger

Read payloads carry `{"v": 1, ...}` so a client can pin the shape it parses.
The `saberops://snapshot` resource assembles the lease, the ledger digest and
the bounded read sections (`list_jobs`, `capacity`, `list_questions`,
`list_proposals`) under one versioned document.

The append-only event ledger (`saberops.mcp.ledger`) records the events the
MCP server appends (`job_submitted`, `plan_submitted`, `plan_lane_submitted`,
`scout_promoted`, `job_cancelled`, `job_note_recorded`, `question_answered`,
`config_proposed`, `work_resumed`, `mode_change_proposed`) as a SHA-256 hash
chain. Appends are idempotent on the event key (replaying the same event
returns the existing entry; the same key with a different payload is a typed
conflict), `replay()` folds the log into state deterministically, and a
tampered chain fails closed.

## Resources

| URI | Content |
| --- | --- |
| `saberops://snapshot` | Versioned read snapshot (`v: 1`). |
| `saberops://lease` | Current lease status. |
| `saberops://ledger` | Ledger digest, last sequence and per-type event counts. |
| `file://<abs path>` | One allowed evidence file (see below). |

Evidence reads are restricted in two independent ways: the fully resolved path
(symlinks followed) must live under `.orch/` or `.saberops/`, and the file name
must match `prompt.md`, `report.md`, `outcome.json`, `failures.md`, `gate.txt`,
`hygiene.md` or `review*.md`. Path traversal, an absolute path outside the
roots, a symlink that escapes, and a disallowed file name are all refused with
`-32030`. Reads are capped at 1 MiB and report `truncated` when the cap is hit.

## Connecting an agent

The easy way registers the server in each agent CLI for you (it uses the
agent's own `mcp add` command where one exists, and edits a JSON config only
when it is plain JSON, after a backup):

```bash
orch connect                  # list agents on this computer and whether each is connected
orch connect claude codex     # connect specific agents (or: orch connect all)
orch connect claude --project # only for the current repository (.mcp.json)
orch connect --remove codex   # undo (also removes the instruction skill)
orch connect codex --dry-run  # show what would change
orch connect codex --skill    # also install the "turn on saberops" instruction file
orch connect --agents         # install proxy subagents for every supported CLI
orch connect codex --agents   # install only that CLI's proxy subagents
orch disconnect --agents      # remove only the generated proxy subagents
```

`--skill` installs one small `SKILL.md` per agent where the CLI supports local
skills, so "turn on saberops" is discoverable and the planner knows the
preflight, submit, wait and owner-accept loop. The MCP server stays the only
engine connection. Paths: `~/.claude/skills/saberops/`, `~/.codex/skills/saberops/`,
`~/.config/opencode/skills/saberops/` and `~/.gemini/config/skills/saberops/`.
Cursor has no local skill format, so it reports **unsupported**. `--dry-run`
prints the exact file and path; a file SaberOps did not write is never
overwritten or removed.

### Proxy subagents

A **proxy subagent** makes another vendor's subscription reachable from the CLI
you are sitting in. The host CLI shows the delegation in its own subagent list,
the proxy runs on the host's cheapest model, and its only action is to hand one
job to SaberOps with a model-family requirement. SaberOps then runs the work on
the target vendor's own CLI and subscription, and keeps the job record, the
gate, the independent review and the owner's acceptance.

`orch connect --agents` writes one marker-guarded definition per proxy (with no
agent named, for every supported CLI). `--dry-run` prints the exact path and
full content and writes nothing; `orch disconnect --agents` removes only the
generated files and keeps any sibling the owner wrote. Re-running `connect
--agents` is idempotent, and a file without the `saberops-managed-agent`
marker is never overwritten or deleted. Definitions are generated from the live
MCP tool list, so a renamed tool cannot leave a stale proxy behind.

| Host CLI | Proxy subagents | Cheapest host model | Target family token |
| --- | --- | --- | --- |
| Claude Code | `codex-worker`, `opencode-go-worker` | `haiku` | `codex`, `opencode-go` |
| Codex | `claude-worker`, `opencode-go-worker` | `gpt-6-luna` | `anthropic`, `opencode-go` |
| OpenCode | `claude-worker`, `codex-worker` | `opencode-go/deepseek-v4.1-flash` | `anthropic`, `codex` |
| Antigravity | `claude-worker`, `codex-worker`, `opencode-go-worker` | `flash` | `anthropic`, `codex`, `opencode-go` |

Paths: `~/.claude/agents/`, `~/.codex/agents/`, `~/.config/opencode/agents/`
and `~/.gemini/config/agents/`. Cursor has no subagent format, so it reports
**unsupported**. The first Claude install creates `~/.claude/agents/`; Claude
Code only watches that folder after a restart, so the command says so.

Two rules keep a proxy honest:

* It **references the session's existing `saberops` MCP server** and never
  starts a second one. A second server process does not share the session
  lease, so its `submit_job` would be refused as read-only.
* It sends one of the three unambiguous family tokens (`anthropic`, `codex`,
  `opencode-go`) and, when SaberOps reports no eligible candidate for that
  family, stops and reports the reason. It never retries another family, so a
  request to use one subscription cannot silently become another.

Antigravity can host the proxies, but there is never an `agy-worker` proxy:
SaberOps does not launch `agy` as an automated worker. A host never gets a
proxy for its own subscription.

### Native workers (hybrid, off by default)

A **native worker** does the work itself on the orchestrating CLI's own
subscription, cooperating through the `saberops` MCP server. Set
`ORCH_NATIVE_WORKERS=1` to expose the four tools; with the variable unset they
are not registered at all.

| Tool | Key arguments | What it does |
| --- | --- | --- |
| `native_claim` | `job_id`, `vendor`, `model` | Claims a QUEUED job, creates the run, the worktree at the pinned base and a RUNNING attempt; returns the worktree, branch and heartbeat deadline |
| `native_heartbeat` | `job_id`, `progress` | Records liveness, delivers pending steering notes once, and returns any stop request |
| `native_controls` | `job_id` | Read-only control state; a missed heartbeat deadline marks the job needs-attention and posts one stop request |
| `record_native_attempt` | `job_id`, `vendor`, `model`, `base_sha`, `candidate_sha`, `worktree`, `transcript`, times, `usage` | Verifies lineage, worktree and canonical isolation, runs the project gate and a different-family review, and completes an attempt that can reach owner acceptance |

`vendor` is the host's own vendor: `claude`, `codex` or `opencode`.
Antigravity is refused as an automated worker. Missing or mismatched evidence
fails closed; the job stays unfinished and the rejection is recorded. Review is
always an external SaberOps reviewer of a different family, never the host's own
subagent, and only the owner accepts.

Control is cooperative: SaberOps cannot kill a native child, see it once it
stops calling, or measure its quota independently. A native child stops when
its host CLI closes (SaberOps-side jobs are separate); do not restart one from a
new session without checking its record.

A closed F17 work gate (owner pause, no planning session, or an away session
that ran out) refuses `native_claim` and `record_native_attempt`, and posts a
stop on every running native job. Its committed worktree is preserved and the
orchestrator stops the child with its own native control; after `orch resume`
the child may report again. Owner cancellation posts a
stop request and cancels the job (a later report is refused); the orchestrator
stops the child with its own native control. Each attempt is labelled
`supervision: host` and `quota: measured|estimated|unknown`.
`ORCH_NATIVE_HEARTBEAT_SECONDS` (default 600) sets the silence deadline and
`ORCH_NATIVE_MAX_CONCURRENT` (default 1) the per-host cap.

`orch connect --skill` also installs a `saberops-worker` definition per host
subagent format (Claude Code, Codex, OpenCode) with worktree-confined
permissions where the CLI has them. The planner guide tells the orchestrating
CLI to launch native workers only from that definition, to call
`native_controls` while waiting, and to stop the child itself when SaberOps
posts a stop. Design: ADR-0030.

### Which worker for which job

| The work is | Use | What SaberOps still does |
| --- | --- | --- |
| Reading code, tracing a failure, summarising or drafting for the owner to review | A native subagent of the host CLI, untracked | Nothing: there is no job, gate, review or acceptance, so its output is never accepted as finished work |
| A change on the host CLI's own vendor | The generated `saberops-worker`, with native workers enabled | Prepares the worktree, runs the project gate and a different-family review, and leaves acceptance to the owner |
| A change on another vendor's subscription | A proxy (`claude-worker`, `codex-worker`, `opencode-go-worker`) | Runs the job on that vendor's own CLI, with the full record, gate, review and acceptance |
| An investigation whose deliverable is a report | A scout job (`kind: SCOUT`) | Records the report; there is no gate, review or acceptance until `promote_scout` starts a build job |

No CLI hosts another vendor's CLI as a native subagent, so a native child always
runs on the host's own vendor and subscription. In particular Claude cannot be
an OpenCode subagent on the Claude subscription: Anthropic does not allow a
third-party app to offer Claude.ai sign-in, so the permitted path is the
unmodified Claude Code binary signed in with the owner's own account. From
OpenCode, reach Claude through the `claude-worker` proxy instead.

OpenCode's "Connect other coding agents" runs the other direction: it points
Claude Code or Codex at an OpenCode Console workspace's models and policies (or
keeps their own provider with `--keep-provider`). It does not make OpenCode host
Claude Code or Codex as subagents, and it is not a SaberOps proxy.

Antigravity may orchestrate with its own native agents; that is a normal
orchestrator session. It is never an automated SaberOps worker, so there is no
`agy-worker`: automating `agy` is outside its consumer terms. Antigravity work
stays attended.

The Setup page offers the same as one **Connect** button per agent. Supported:
Claude Code, Codex, Antigravity, OpenCode and Cursor; any other MCP client can
use the snippet under "Client configuration" below.

## Turning SaberOps on and off

* **Per session, in the agent.** Every agent session starts with SaberOps
  **off**: the agent sees a single `saberops_on` tool, holds no planner lease and
  works as a normal coding agent. Say "turn on saberops" and the agent calls
  `saberops_on`; the full tools appear (the server sends
  `notifications/tools/list_changed`) and that session becomes the planner. Say
  "turn off saberops" and it calls `saberops_off`, releases the lease and is a
  normal agent again. `orch mcp serve --active` starts a session already on.
* **For every agent, by the owner.** `orch mcp off` (or Setup → Turn off for all
  agents) keeps every agent registered but offers no tools at all, not even
  `saberops_on`. `orch mcp on` restores it; `orch mcp status` shows the state and
  the planning agent. The switch is per profile.

`orch mcp status` separates shared access from chat activation (which the CLI
cannot observe) and shows the planning holder, PID, session, epoch, last
activity, idle time, active job count and lease-file scope. The in-session
`saberops://lease` resource reports ownership for that session.

An idle session can be replaced after 15 minutes without a tool call; set
`SABEROPS_MCP_IDLE_SECONDS` to a positive number of seconds to change that
bound. The previous session is fenced from later writes. Jobs submitted by the
holder protect its lease while they are queued or running. To hand off sooner,
run `orch mcp release --holder <session-id-or-pid>` using the holder shown by
status. This targets only that holder and refuses if its work is active or its
work state cannot be checked. A live legacy owner without fencing must be closed
before its lease can be released. The release does not turn off shared access.

Every tool result starts with `Orchestrator:`, and the guide asks the agent to
start every message that relays SaberOps with `Orchestrator:` too, so the user
can always tell SaberOps apart from the agent's own words.

## The built-in guide

How to plan and submit work (submitting independent jobs, writing acceptance
criteria, what stays with the owner) is delivered three ways so any client sees
it: the `initialize` instructions, the `saberops-guide` prompt and the
`saberops://guide` resource; `saberops_on` also returns it.

## Client configuration

The server is a local stdio process; there is no network listener and no token.
Use the absolute path to the `orch` executable if it is not on `PATH`.

### Claude Code

```bash
claude mcp add saberops -- orch mcp serve
```

or, in `.mcp.json` at the project root:

```json
{
  "mcpServers": {
    "saberops": { "command": "orch", "args": ["mcp", "serve"] }
  }
}
```

### Cursor

`.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "saberops": { "command": "orch", "args": ["mcp", "serve"] }
  }
}
```

### Codex

`~/.codex/config.toml`:

```toml
[mcp_servers.saberops]
command = "orch"
args = ["mcp", "serve"]
```

Checkout development (an installed package must not shadow the edited source):

```bash
PYTHONPATH=src .venv/bin/python -m saberops.cli mcp serve
```

## Tests

`tests/test_mcp_server.py` covers the transport, lease, ledger, redaction and
evidence guard; `tests/test_mcp_contracts.py` covers the tool contracts and the
adversarial never-exposed invariants; `tests/test_cli_mcp.py` covers the CLI
subcommand.
