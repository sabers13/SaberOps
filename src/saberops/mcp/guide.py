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
    "mention SaberOps. Only if the user explicitly asks to turn SaberOps on (for example "
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
    "Read the full guide in the saberops-guide prompt or the saberops://guide resource before "
    "your first submission."
)

PAUSED_INSTRUCTIONS = (
    f"{PREFIX} SaberOps is switched off by the owner right now, so it offers no tools. Work as "
    "you normally would. The owner turns it back on with `orch mcp on` or on the SaberOps Setup "
    "page."
)

GUIDE = """\
# Working with SaberOps

You are the planner. SaberOps has exactly three roles:

- **plan** — you, the CLI session the owner sits in. You submit and follow work.
- **build** — one worker per task, including its own repair retries. It runs in
  an isolated copy of the project with the models the owner chose on the
  Routing page. A stronger-model start or escalation is a setting on the build
  route, not another role.
- **check** — an independent reviewer (ideally another vendor) that reads the
  built candidate before the owner sees it.

Gates, landing, the memory and disk guard and cleanup are deterministic engine
steps, not model roles. There are no selectable modes.

## Talking to the user
- Start every message that relays SaberOps (a submission, a status, a result, a
  question, an error) with "Orchestrator:" so the user knows it comes from
  SaberOps. Your own words without SaberOps content need no prefix.
- When the user asks to turn SaberOps off, call `saberops_off` and carry on as a
  normal coding agent. Turn it back on only when they ask (`saberops_on`).

## When to use it
- Use SaberOps for changes worth isolating and checking: features, fixes,
  refactors, anything that touches several files or must pass the project's checks.
- Do it yourself, without SaberOps, for questions, reading code, tiny edits you
  can verify at a glance, and anything the owner asks you to do directly.
- A docs-only change may skip independent review under the existing
  risk-adapted review policy; that policy keeps review for anything touching
  authority or production behaviour, and never changes acceptance.

## Submitting work
1. Call `capacity` to see whether work can start now.
2. One task: `submit_job` with
   - `repo`: absolute path of the repository,
   - `goal`: one plain sentence of what must change,
   - `acceptance`: checks a reviewer can verify ("pytest tests/test_webhook.py passes",
     "retries 3 times with backoff", "no change to the public API"),
   - `idempotency_key`: a stable key, so a retry never submits twice.
   Optional `base_ref`, `priority`, `deadline`, `not_before`, `depends_on`, and
   `caller_id` select the base, scheduling, dependency and caller. A dependant
   starts only after every job it names in `depends_on` has been accepted by the
   owner; a dependency that fails or is cancelled parks the dependant for good.
   `acceptance` is a string or list of strings.
   `requirements` may include `role`, `effort`, `size_hint`, `required_capabilities`,
   `read_only`, `model_family_include`, and `model_family_exclude`. The role
   values are PLAN, BUILD and CHECK; only BUILD is dispatchable, and every
   submitted job builds (reasoning and any repair happen on the build route).
   A failed check is repaired by the same build route. `effort` is low, medium or high;
   `size_hint` is S, M or L and informs capacity estimates. Family inclusion and
   exclusion filter candidates at admission.
   Never send `provider`, `model`, `tier`, `billing`, `route`, `quota` or `account`:
   they are refused. SaberOps picks the model on the build route.
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

## Following it
- Call `wait_job` after each submission; it returns on a final outcome, blocking
  question, or timeout. Call again after a timeout if needed.
- `get_job` / `list_jobs` to inspect jobs.
- `tail_job` to read a running job's progress and terminal output without
  waiting: pass the `next_sequence` it returned last time to get only what is
  new. It is read-only, so it also works in a session that is not the planner.
- `read_evidence` for check output and review findings when something fails.
- Report back to the owner in plain words: what finished, what failed and why,
  and what needs their decision. Link them to the run on the SaberOps page.
- `cancel_job` only for work you submitted by mistake or that the owner stopped.
- When the owner corrects a running job, call `note_job` with their correction.
  It is stored durably and reaches the worker once, in the prompt of the next
  attempt; it is never typed into a running worker, so a job that finishes
  before another attempt keeps the note pending. `catch_up` lists every note
  with whether it was delivered and to which attempt.

## Catching up in a new session
- When the owner says "catch me up" (or you have no history of a job), call
  `catch_up` with its `job_id`; use `list_jobs` first when they did not give one.
  It is read-only and works even when this session is not the planning owner.
- Tell the owner: the original request, the current candidate commit, the latest
  check and review with the commit each assessed, any open question, and the next
  action. When the result offers `orch accept RUN_ID`, show that exact command;
  the owner accepts there or rejects from the run page for full evidence.

## Questions and decisions
- `list_questions` shows what is waiting. You may answer only planner-level
  questions (`answer_question` with authority PLAN).
- Owner decisions are never yours: accepting or rejecting changes, approving
  configuration, billing, merging, pushing. Tell the owner what to decide and where.
- To suggest a setting, use `propose_config_change` with a short rationale. It
  changes nothing until the owner approves it.

## If the tools disappear
The user turned SaberOps off, or the owner switched it off for every agent.
Carry on as a normal coding agent and do not try to reach SaberOps another way.
"""
