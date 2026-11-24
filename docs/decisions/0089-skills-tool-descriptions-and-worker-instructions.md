# ADR-0089: Skills for judgment, tool descriptions for tools, engine instructions for workers

- **Status:** Accepted. Implemented in phase 1.1 lane H12.
- **Date:** 2026-11-24
- **Decider:** Saber (owner). Phase 1.1 plan, owner's words 2026-11-22
  ("shouldnt we add skills to saberops like firstmate?") and the recorded
  answers Q1-Q15
- **Related:** [ADR-0005](0005-mcp-first-engine-and-phase-2-board.md) (the MCP
  surface and the planning CLI), [ADR-0016](0016-steering-note-channel.md)
  (steering), [ADR-0034](0034-advisor-checkpoints.md) (the advisor),
  [ADR-0053](0053-acceptance-clause-review-gate.md), [ADR-0055](0055-review-evidence-guarantee.md), [ADR-0057](0057-clause-format-retry-and-gate-execution.md) (reviewer and
  advisor instructions stay engine-generated and fixed),
  [ADR-0063](0063-planner-recorded-difficulty.md) (difficulty is the planner's),
  [ADR-0068](0068-one-recovery-tool.md) (`resume_job`),
  [ADR-0082](0082-owner-mode-carries-authority.md) and
  [ADR-0084](0084-modes-stop-at-consequential-candidates.md) (owner modes and
  consequential candidates)

## 1. Context

`orch connect --skill` installed one `saberops` skill. It carried the loop
*and* the whole ~350-line working guide, so every planner load paid for text it
needed only some of the time. The guide also held short rules for individual
tools (how to write a job, which steering verb to use, when to resume, when to
propose a setting) that belong beside those tools.

The owner asked for SaberOps skills in the style of firstmate. The phase 1.1
rule is narrower than "add many skills":

> a skill only when the guidance is judgment, is long, and is needed only some
> of the time. Short rules for using one tool go in that tool's own MCP
> description.

Nothing here grants authority. The engine still enforces acceptance, review,
quota, isolation and pause; a skill that disagrees with the engine loses.

## 2. Decision

1. **One short main skill.** `saberops` teaches the turn-on loop, the hard
   rules and when to load the other three. It does not inline the working
   guide; it points at the `saberops://guide` resource and the
   `saberops-guide` prompt. `tests/test_agent_skill.py` bounds its size.
2. **Three on-demand skills.**
   - `saberops-setup`: the first-run walkthrough, reading `preflight`,
     connecting CLIs and accounts, quota and billing consent, and what to do
     when a vendor CLI update breaks a worker.
   - `saberops-planning`: look facts up and ask the owner only for decisions;
     short question rounds with a recommended answer; difficulty; vertical
     slices that fit one worker; wide changes as expand, migrate, contract;
     how lanes connect, written in the lane text; and choosing native subagent
     vs SaberOps job vs proxy worker vs scout.
   - `saberops-modes`: helping the owner choose and run ask-me, critical-only
     or away; raising a question and self-answering a routine one with
     evidence and "cost if wrong"; drafting standing decisions before the owner
     leaves; and the strict-pause reminder. Enforcement stays in the engine.
3. **Per-CLI paths grow by one folder per skill.** Each supported CLI keeps its
   existing skill root and gets `saberops/`, `saberops-setup/`,
   `saberops-planning/` and `saberops-modes/`, each with one `SKILL.md`.
   Install and removal stay one command; a file without the ownership marker
   is never overwritten or removed; Cursor stays unsupported.
4. **Tool descriptions carry the short rules.** `submit_job` says how to write
   a goal and testable acceptance, including at least one check tied to the
   requested behaviour that the worker does not write, and names
   `review_focus`. `note_job`, `redirect_job` and `resume_job` say when to use
   each. `get_job` and `wait_job` say to show the exact accept command.
   `catch_up` says when a new session needs it. `propose_config_change` says
   when to propose and that it applies nothing.
5. **The engine generates the worker instructions, build role only.** Every
   build attempt gets a bounded `[HOW TO WORK]` section (about 15 lines): the
   smallest complete change ladder, the "never cut" line (validation at trust
   boundaries, data-loss handling, security, accessibility), surgical edits
   that mention unrelated dead code instead of deleting it, the optional
   `shortcut: <limit>, <when to upgrade>` comment, checking each repair finding
   against the code before changing anything with any disagreement in the
   report, and a final report that ends with what was skipped, what was not
   checked, and the risks. Scouts keep their own section and do not receive it.
   The generated `saberops-worker` definition carries the same build rules and
   the heartbeat rules.
6. **Reviewer and advisor instructions stay fixed** (ADR-0053, ADR-0055,
   ADR-0057). The reviewer already receives the worker's bounded final report;
   the worker instructions make its last lines the risk evidence, so H4 passes
   it through unchanged.

## 3. Authoring

The text is written for agents: short imperatives, front-loaded trigger words
in each frontmatter description, one meaning per sentence, no duplicated
guide. Automated trigger tests are deliberately absent; H37 pressure-tests each
skill by hand on the four CLIs. `tests/test_agent_skill.py` checks the
mechanical contract (install, remove, ownership, size, named tools) and
`tests/test_h12_skills.py` checks the worker section and tool descriptions.

## 4. Consequences

- The main skill no longer names every tool; the H36 contract checks the guide
  and the skill set together, and every tool a skill names must exist.
- The full guide remains the single source of depth, so a skill edit cannot
  fork the workflow.
- A missing worker section, a scout receiving it, or a skill naming a removed
  tool fails a test rather than silently shipping.
