"""The built-in working guide every connected agent receives.

Delivered three ways so any MCP client picks it up: the ``instructions`` field
of ``initialize`` (most clients add it to the agent's context), the
``saberops-guide`` prompt, and the ``saberops://guide`` resource. It explains
the workflow in the owner's terms; it grants nothing — every boundary it
mentions is enforced by the server and the engine, not by the text.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "AMBIGUOUS_FAMILY_TOKENS",
    "FAMILY_TOKENS",
    "GUIDE",
    "GUIDE_PROMPT",
    "GUIDE_URI",
    "PAUSED_INSTRUCTIONS",
    "PREFIX",
    "SABEROPS_OFF_TOOL",
    "SABEROPS_ON_TOOL",
    "SESSION_OFF_INSTRUCTIONS",
    "SESSION_OFF_MESSAGE",
    "SHORT_INSTRUCTIONS",
]

GUIDE_URI = "saberops://guide"
GUIDE_PROMPT = "saberops-guide"

#: The family tokens a cross-CLI proxy may request. Each names exactly one
#: worker family (see ``matches_model_family``).
FAMILY_TOKENS = ("anthropic", "codex", "opencode-go")

#: Tokens that look natural but are not supported: ``claude`` also matches a
#: Go-hosted Claude model, and ``opencode`` matches every OpenCode model
#: including ChatGPT-plan ones.
AMBIGUOUS_FAMILY_TOKENS = ("claude", "opencode")

FAMILY_TOKEN_GUIDANCE = (
    "Supported family tokens for a cross-CLI proxy request are `anthropic` "
    "(Claude Code worker), `codex` (Codex worker) and `opencode-go` (OpenCode "
    "worker on the Go subscription). `claude` and `opencode` are ambiguous and "
    "are not supported tokens: `claude` also matches a Go-hosted Claude model, "
    "and `opencode` matches every OpenCode model including ChatGPT-plan ones. "
    "A `model_family_include` no build route can satisfy is refused at submit "
    "with NO_ROUTE_FOR_REQUIREMENTS."
)

#: Every message that comes from SaberOps starts with this, so the user can
#: tell SaberOps apart from the agent's own words.
PREFIX = "Orchestrator:"

_NO_ARGS: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}

SABEROPS_ON_TOOL: dict[str, Any] = {
    "name": "saberops_on",
    "description": (
        "Turn SaberOps on for this session. Call this ONLY when the user explicitly asks to "
        "turn on / start / use SaberOps (for example \"turn on saberops\"). Never call it on "
        "your own initiative."
    ),
    "inputSchema": _NO_ARGS,
}

SABEROPS_OFF_TOOL: dict[str, Any] = {
    "name": "saberops_off",
    "description": (
        "Turn SaberOps off for this session and go back to being a normal coding agent. Call "
        "this when the user asks to turn off / stop / leave SaberOps."
    ),
    "inputSchema": _NO_ARGS,
}

SESSION_OFF_INSTRUCTIONS = (
    "SaberOps is installed but OFF in this session: work as a normal coding agent and do not "
    "mention SaberOps. Its tools are listed so every client sees them, but they refuse until "
    "SaberOps is on. Only if the user explicitly asks to turn SaberOps on (for example "
    "\"turn on saberops\"), call the saberops_on tool; its result explains how to work with it."
)

SESSION_OFF_MESSAGE = (
    f"{PREFIX} SaberOps is off in this session. Ask the user whether to turn it on; only "
    "call saberops_on if they say so."
)

SHORT_INSTRUCTIONS = (
    f"Start every message that relays SaberOps to the user with \"{PREFIX}\". "
    "When the user asks to turn SaberOps off, call saberops_off. "
    "SaberOps runs coding work for you on the owner's AI subscriptions and hands back "
    "checked, isolated changes for the owner to accept. You plan; SaberOps decides which "
    "model does each job. Submit work with submit_job (one job per independent task), give a "
    "goal and testable acceptance criteria, never a model or provider, then call wait_job. "
    "Use get_job / list_jobs as needed. If the owner asks to \"catch me up\" about a job, "
    "call catch_up. Owner-only decisions "
    "(accepting changes, approving config, billing, "
    "merging, pushing) are never available here: tell the owner to use the SaberOps page. "
    "Use these MCP tools, never a shell `orch mcp ...` command or `orch resume`; "
    "the owner runs those. "
    "Read the full guide in the saberops-guide prompt or the saberops://guide resource before "
    "your first submission."
)

PAUSED_INSTRUCTIONS = (
    f"{PREFIX} SaberOps is switched off by the owner right now, so it offers no tools. Work as "
    "you normally would. The owner turns it back on with `orch mcp on` or on the SaberOps Setup "
    "page; never run that command yourself."
)

GUIDE = """\
# Working with SaberOps

You are the planner. SaberOps has exactly three roles:

- **plan** — you, the CLI session the owner sits in. You submit and follow work.
- **build** — one worker per task, including its own repair retries. It runs in
  an isolated copy of the project with the models the owner chose with
  `orch routing`. A stronger-model start or escalation is a setting on the
  build route, not another role.
- **check** — an independent reviewer (ideally another vendor) that reads the
  built candidate before the owner sees it.

Gates, landing, the memory and disk guard and cleanup are deterministic engine
steps, not model roles. There is one owner mode, described next.

## Owner mode
SaberOps runs in one owner mode: `ask-me`, `critical-only` or `away`. It is the
only authority setting: each mode carries a fixed permission ceiling, a saved
store may narrow it, and nothing can widen it. Only the owner switches it, from
the CLI (`orch mode set`) or the Questions page.

- Call `get_mode` to read the effective mode and what it permits. Never claim a
  mode you have not read.
- `tighten_mode` switches to `ask-me`; it only removes permission, so it is
  always allowed from the planning session. Any looser mode is the owner's.
- `propose_mode_change` raises an owner question. It never changes the mode, so
  do not tell the user the mode changed. With the owner-prompt hook installed
  the owner types `/saberops-away <duration>`, `/saberops-critical-only` or
  `/saberops-ask-me` in chat; without it, the owner runs `orch mode request` and
  `orch mode confirm` in a terminal, where the code is shown.
- In `ask-me`, every question waits for the owner. In `critical-only`, a live
  planning session may answer a **routine** question (`critical: false`) with
  `answer_question`, following the question's stored recommendation and
  supplying evidence and a rejected alternative; add `cost_if_wrong` when you
  can name what a wrong answer would cost. Every self-answer is logged and
  appears in `catch_up`.
  Never answer a `critical: true` question: it notifies the owner and waits.
  `away` follows the same question rule while a planner is connected. Closing
  the planning session pauses work in every mode and is separate work; do not
  start a background planner or claim a question was answered without the tool.
  Critical means an owner-only or acceptance class, a never-delegable kind, or
  anything you escalated; you can add criticality, never remove it.
- Call `list_questions` before answering. `question_view.critical` and
  `critical_reasons` say why a question is the owner's.
- Closing the planning CLI pauses everything, in every mode. No new job, plan
  lane, retry, repair or refresh starts, and every running worker is stopped at
  a safe point with its worktree, checkpoint and evidence preserved. Nothing
  resumes on its own: call `get_pause` to see what is parked and why, and tell
  the owner to run `orch resume`; only the owner can confirm a resume. Never
  call a resume tool and never run `orch resume` yourself. `orch pause` is the
  owner's own stop. The turn-on reply states a
  paused gate, and while it is closed `get_job` carries `pause` facts and
  `wait_job` returns `outcome: "paused"` immediately with the resume action:
  tell the owner work is paused instead of waiting or claiming nothing is
  pending. While away or critical-only, a periodic digest notice lists running
  jobs, the subscriptions in use and the usage so far.
- A job waiting with `QUOTA_EVIDENCE_REQUIRED` needs evidence the engine cannot
  collect safely itself (for example Anthropic/Claude usage). `wait_job`
  returns `outcome: "needs_attention"` and the guidance carries the exact
  `orch quota set <provider> <pool> --remaining <percent>` command the owner
  must run; the job starts by itself on the next tick after that.

## Tools, not the shell
- Use the SaberOps MCP tools offered in this session. When a tool covers an
  action, call the tool; do not run the equivalent `orch` command in a shell.
- The switch is `saberops_on` / `saberops_off`: call `saberops_on` when the
  owner asks to turn it on, and call `saberops_off` when they ask to stop.
  Never run `orch mcp on`,
  `orch mcp off`, `orch mcp release` or `orch mcp serve` in a shell from inside
  a session: `orch mcp release` is the owner's targeted handoff, and a second
  server started with `orch mcp serve` does not share this session's lease.
- `orch resume` is the owner's explicit confirmation. Ask the owner to run it;
  do not run it yourself.

## Talking to the user
- Start every message that relays SaberOps (a submission, a status, a result, a
  question, an error) with "Orchestrator:" so the user knows it comes from
  SaberOps. Your own words without SaberOps content need no prefix.
- When the user asks to turn SaberOps off, call `saberops_off` and carry on as a
  normal coding agent. Turn it back on only when they ask (`saberops_on`).

## First time: the walkthrough
- When the owner says "walk me through saberops", asks how to start, or the
  setup is unfinished (a fresh store, `preflight` not green, or no job yet),
  call `start_walkthrough` with the project `repo` before anything else.
- Open with the short introduction it returns, in plain words: what SaberOps
  does, what it never does, and how long setup takes. Avoid jargon; if a word
  cannot be avoided, define it in one line (the result carries a small glossary
  for job, review and accept).
- Then one step at a time. Explain the step in one or two sentences, ask, and
  either run its command after a clear yes (`kind: agent`) or give the exact
  command the owner types (`kind: owner`). Never run an owner step: confirming a
  plan, signing in to a CLI, allowing or blocking a provider, freeing disk
  space, updating a CLI. For `kind: answer`, ask for the value (a test command,
  a remaining amount) and never invent one, above all a remaining plan amount;
  then run the step's command with it.
- After each step, call `start_walkthrough` again. Steps already done come back
  with `done: true` and `next_step` is the first missing one, so an interrupted
  conversation resumes at the right place with no stored cursor. Never tell the
  owner a step is done without that result.
- When `ready` is true, submit `sample_first_job` as one `submit_job`, call
  `wait_job`, and present the exact `orch accept RUN_ID` when it finishes. The
  owner accepts; you cannot. Then show `what_next` as one short screen.
- If the owner writes in another language, say the walkthrough in that language
  and keep every command exactly as written.

## When to use it
- Use SaberOps for changes worth isolating and checking: features, fixes,
  refactors, anything that touches several files or must pass the project's checks.
- Do it yourself, without SaberOps, for questions, reading code, tiny edits you
  can verify at a glance, and anything the owner asks you to do directly.
- Every candidate that can be accepted is independently reviewed, including a
  docs-only change. Review focus can be added on `submit_job`; it cannot turn
  review off. Only the owner's direct manual `orch run --no-review` switch
  skips review, and it never changes acceptance rules.

## Which worker for which job
Choose by what the work needs, not by what is closest to hand.

- **Your own subagent, untracked** — use a native subagent in this CLI for
  read-only exploration and drafting: finding code, reading a stack trace,
  summarising a design, drafting a patch for the owner to review. It is cheap
  and appears in this CLI's own subagent list, but it leaves no SaberOps record,
  gate or independent review, so its output is never accepted as finished work.
  Anything the owner should accept goes through SaberOps.
- **`saberops-worker`** — when native workers are enabled and the task belongs
  on this CLI's own vendor, hand the claimed job to the generated
  `saberops-worker` definition. SaberOps prepares the worktree and still runs
  the project gate, an independent different-family review and the owner's
  acceptance.
- **A proxy (`claude-worker`, `codex-worker`, `opencode-go-worker`)** — when
  the work belongs on another vendor's subscription. The proxy only submits one
  job with that family token; it never edits, reviews or accepts.
- **A scout job (`kind: SCOUT`)** — when the deliverable is an investigation or
  a report rather than a change. A scout is read-only and has no gate,
  independent review or acceptance; promote its report with `promote_scout`
  when the owner wants the work built.

No CLI hosts another vendor's CLI as a native subagent, so a native child always
runs on this CLI's own vendor and subscription. In particular Claude cannot be
an OpenCode subagent on the Claude subscription: Anthropic does not allow a
third-party app to offer Claude.ai sign-in, so the permitted path is the
unmodified Claude Code binary signed in with the owner's own account. Reach
Claude from OpenCode through the `claude-worker` proxy instead.

OpenCode's "Connect other coding agents" runs the other direction: it points
Claude Code or Codex at an OpenCode Console workspace's models and policies (or
keeps their own provider with `--keep-provider`). It does not make OpenCode host
Claude Code or Codex as subagents, and it is not a SaberOps proxy.

Antigravity may orchestrate with its own native agents; that is a normal
orchestrator session. It is never an automated SaberOps worker, so there is no
`agy-worker`: automating `agy` is outside its consumer terms. Antigravity work
stays attended.

## Submitting work
1. Call `capacity` to see whether work can start now.
2. One task: `submit_job` with
   - `repo`: absolute path of the repository,
   - `goal`: one plain sentence of what must change,
   - `acceptance`: checks a reviewer can verify ("pytest tests/test_webhook.py passes",
     "retries 3 times with backoff", "no change to the public API"),
   - `idempotency_key`: a stable key, so a retry never submits twice.
   Optional `base_ref`, `priority`, `kind`, `deadline`, `not_before`,
   `depends_on`, `caller_id` and `review_focus` select the base, scheduling,
   job kind, dependency, caller and reviewer attention. `review_focus` adds
   what the independent reviewer should look at (for example new
   suppressions or broad handlers); it can never turn review off.
   `requirements` field `consequential` (send `true`) records that the candidate is
   consequential (a data migration, deleting files or data, auth and
   security, an irreversible operation, effects outside the work copy), so
   away and critical-only park it for the owner instead of auto-accepting;
   the engine also checks its own fixed floor from the diff, so the flag can
   only add, never remove, a park. A
   dependant starts only after every job it names in `depends_on` has been
   accepted by the owner; a dependency that fails or is cancelled parks the
   dependant for good. A completed scout dependency satisfies its dependants.
   `kind` is `BUILD` (default) or `SCOUT`: a scout investigates read-only and
   returns a Markdown report instead of a change, with no gate, independent
   review or owner acceptance; promote its report into a build job with
   `promote_scout`. A job with `effort: low` prefers the cheapest plan model
   of each subscription on the build route, and a scout with no stated effort
   defaults to `low` (F27/H8); a project can set `scout_route` to `cheap`,
   `build` or an exact `provider/model` for scouts, and `effort: high` or
   `size_hint: "L"` keeps the ordinary build route.
   `acceptance` is a string or list of strings.
   `requirements` may include `role`, `effort`, `size_hint`, `required_capabilities`,
   `read_only`, `model_family_include`, `model_family_exclude`, and `difficulty`.
   The role
   values are PLAN, BUILD and CHECK; only BUILD is dispatchable, and every
   submitted job builds (reasoning and any repair happen on the build route).
   A failed check is repaired by the same build route. `effort` is low, medium or high;
   `size_hint` is S, M or L and informs capacity estimates. `difficulty` is
   `routine` (the default, and what a missing value means) or `hard`: a hard
   task starts on the build route's strong-start chain, still under quota and
   the owner's automatic routing ceiling; it is a judgement about the task,
   never a model, tier or route. Family inclusion and
   exclusion filter candidates at admission. Supported family tokens for a
   cross-CLI proxy request are `anthropic` (Claude Code worker), `codex` (Codex
   worker) and `opencode-go` (OpenCode worker on the Go subscription); `claude`
   and `opencode` are ambiguous and are not supported tokens. A
   `model_family_include` no build route can satisfy is refused at submit with
   NO_ROUTE_FOR_REQUIREMENTS.
   Never send `provider`, `model`, `tier`, `billing`, `route`, `quota` or `account`:
   they are refused. SaberOps picks the model on the build route. Say how hard
   the task is with `difficulty`; the engine maps that judgement to a route.
3. Several independent tasks: submit one `submit_job` per independent task, each
   with its own distinct `idempotency_key` and non-overlapping scope. The
   scheduler dispatches admitted jobs in parallel across available providers.
4. Keep tasks small: one reviewable change each. A task that needs another
   task's result names it in `depends_on`: the dependent job waits in the queue
   until the owner accepts the earlier job, and you do not have to stay in the
   chat to submit it afterwards. If you submitted it without `depends_on` and the
   first job is accepted, the second reports a stale base with its work and
   evidence preserved; request `refresh_candidate` with the stale run ID. The
   refreshed candidate receives fresh checks and review; the owner accepts it
   separately.

## Plans
- When the owner works from a tracked `PLAN.md`, call `submit_plan` with the
  repository and the plan file path instead of one `submit_job` per task. The
  file is read inside the checkout; you do not paste its contents.
- Each lane without unsettled dependencies becomes one ordinary job. A lane
  starts automatically only after every dependency is accepted; independent
  lanes run in parallel. Do not queue a dependent lane by hand; its accepted
  dependency starts it.
- A ready lane starts on the scheduler tick while a live planning session holds
  the planning lease, and only when the owner mode allows a start. Ask-me
  still allows it: starting an activated lane is not an owner judgment. Away
  and Critical only keep starting unblocked lanes. In those modes a lane
  candidate that passed its gate and an independent review of that exact commit
  is auto-accepted as a local commit (never pushed), and `STATE.md` shows the
  undo command; in Ask me, or in a store whose saved permissions do not grant
  auto-accept, the plan stops at the first candidate and `STATE.md` says which
  accept it is waiting for. Critical-only notifies a stalled lane instead of
  answering it.
- `run: "owner"` lanes are never dispatched. They are completed only by the
  owner with `orch plan-file done <lane> --plan <id> --evidence "<what happened>"`.
- `plan_status` returns the same record-derived table as `STATE.md`: lane
  status, worker/model, branch, commit, evidence and the next owner action. It
  is read-only, so a session that is not the planning owner can read it.
- Lanes pause when the owner closes the planning CLI; they resume when a
  planning session is open again. A plan stall or completion sends one notice.

## Following it
- Call `wait_job` after each submission; it returns on a final outcome, blocking
  question, or timeout. Call again after a timeout if needed.
- `get_job` / `list_jobs` to inspect jobs.
- A scout (`kind: SCOUT`) finishes with a report, not a candidate. `get_job`,
  `wait_job` and `catch_up` show its `report` block: the report path, a short
  summary, its findings and any open owner decisions (which also become owner
  questions). Do not expect `orch accept` for a scout; when the owner wants the
  work done, call `promote_scout` with the scout's job id to start a build job
  whose starting context is that report.
- `tail_job` to read a running job's progress and terminal output without
  waiting: pass the `next_sequence` it returned last time to get only what is
  new. It is read-only, so it also works in a session that is not the planner.
- `read_evidence` for check output and review findings when something fails.
- Report back to the owner in plain words: what finished, what failed and why,
  and what needs their decision. Link them to the run on the SaberOps page.
- `cancel_job` only for work you submitted by mistake or that the owner stopped.
- When the owner corrects a running job, call `redirect_job` with their
  correction when it must land now: the current attempt stops at a safe point,
  its work is kept as a checkpoint and the next attempt restores it with the
  note in its prompt. Use `note_job` when the next attempt is soon enough.
  Neither is ever typed into a running worker; `catch_up` lists every note with
  whether it was delivered and to which attempt.
- A candidate cannot be accepted while one of its job's notes is undelivered,
  and away mode never auto-accepts past one. `catch_up` then offers
  `redirect_job` to deliver the note or `discard_note` to drop one that no
  longer applies.

## Resuming a failed job
- When a job fails, call `resume_job` with its `job_id`. `from: "scratch"`
  starts a fresh attempt of the same frozen task and policy; `from:
  "last_candidate"` continues from the candidate the failed run saved, so the
  work is kept. The call refuses with a reason when the job is not failed or
  the chosen path has nothing to resume.
- `note` travels into the next attempt exactly like `note_job`.

## Rejecting a candidate
- When the owner rejects a succeeded candidate with a correction, call
  `reject_candidate` with the job's `job_id` and that text as `note`. It records
  the rejection (that candidate can never be accepted) and starts the corrected
  attempt from the saved candidate with the note as its resume note.
- A rejection without a note is refused; a note already pending on the job
  travels with it. An already accepted candidate is refused because acceptance
  is terminal; use `note_job` or `redirect_job` while a job runs, and
  `resume_job` when it failed.
- One call starts at most one successor. Calling again while the job is running
  returns the successor it already has instead of starting another.
- `worker_binding` is the folded-in try-worker option: the exact blocked
  `UNKNOWN` worker binding named in the failure evidence, fresh attempt only.

## Catching up in a new session
- When the owner says "catch me up" (or you have no history of a job), call
  `catch_up` with its `job_id`; use `list_jobs` first when they did not give one.
  It is read-only and works even when this session is not the planning owner.
- For a plan, call `catch_up` with its `plan_id` (or `plan_status` with the
  repository). It returns the plan header, the lane table, per-lane open
  questions and the one next action, so a fresh CLI can take over a plan without
  the planner that submitted it.
- Tell the owner: the original request, the current candidate commit, the latest
  check and review with the commit each assessed, any open question, and the next
  action. When the result offers `orch accept RUN_ID`, show that exact command;
  `orch report RUN_ID` shows the owner the full check and review evidence.
- `usage` lists each attempt's subscription and token usage; `switch` describes
  the latest handoff (for example a quota-exhausted attempt continued on another
  subscription from its checkpoint). Report both plainly; they are durable
  evidence, not guesses.

## Native workers (hybrid)
When the owner has enabled native workers (`ORCH_NATIVE_WORKERS=1`), part of the
work may run on your own subagents while other jobs run as ordinary SaberOps
workers. The tools below exist only while that flag is on. This is the
generated `saberops-worker`, not a free-form subagent you invent: an ad-hoc
subagent stays read-only exploration and drafting, and its work is never
accepted on its own.

- Launch a native worker only from the generated `saberops-worker` definition
  that `orch connect --skill` installed. Never do SaberOps work in your main
  session and never claim a job you did not hand to that subagent.
- Give it one queued job id. It calls `native_claim`, works only in the returned
  worktree, calls `native_heartbeat` at least every few minutes, and calls
  `record_native_attempt` when the work is committed.
- While it works, call `native_controls` with its job id instead of waiting
  blindly. It returns `stop_requested`, the steering state and the heartbeat
  deadline. When SaberOps posts a stop (the owner cancelled the job, or a missed
  heartbeat made it needs-attention), stop the child with your own native
  control (subagent stop / task stop): SaberOps cannot kill it.
- Silence past the deadline marks the job needs-attention. Never re-launch a
  stopped or silent native child, and never treat its silence as success.
- A closed work gate (the owner paused, or this CLI closed) refuses new native
  claims and native reports, and posts a stop on running native jobs. Stop the
  child yourself; a report refused while paused can be retried after the owner
  resumes.
- A native result reaches the owner only through the project gate and an
  independent different-family review; missing or mismatched evidence fails
  closed. Never accept, push or merge.
- Native children stop when this CLI closes, so a closed CLI cannot leave one
  running. Ordinary SaberOps jobs run outside this CLI and are not stopped by
  it: check their state before restarting or duplicating anything.

## Questions and decisions
- `list_questions` shows what is waiting. You may answer only planner-level
  questions (`answer_question` with authority PLAN).
- `get_mode` reads the owner mode; `propose_mode_change` raises an owner
  question about it. `tighten_mode` may switch it to `ask-me` (it only
  tightens). Nothing switches it to a looser mode from a chat without the
  owner's typed command or terminal confirmation code.
- Owner decisions are never yours: accepting or rejecting changes, approving
  configuration, billing, merging, pushing. Tell the owner what to decide and where.
- To suggest a setting, use `propose_config_change` with a short rationale. It
  changes nothing until the owner approves it. `list_proposals` reads the
  decision ledger: what the owner approved or rejected, and why.
- `doctor` reads the self-healing report (each finding is ok, skip, fixable or
  human). `doctor_fix` repairs only fixable SaberOps-owned findings and then
  rechecks; ask-me asks the owner first and away/critical-only apply safe
  repairs. Never use it for vendor logins, API keys, owner settings or the
  user's repositories.

## If the tools disappear
The user turned SaberOps off, or the owner switched it off for every agent.
Carry on as a normal coding agent and do not try to reach SaberOps another way.
"""
