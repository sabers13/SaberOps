# ADR-0033: A plain-language first-run walkthrough

- **Status:** Accepted. Implemented in Phase 1 lane F25 (Wave 3).
- **Date:** 2026-11-19
- **Decider:** Saber (owner), Phase 1 plan lane F25
- **Related:** [ADR-0005](0005-mcp-first-engine-and-phase-2-board.md) (MCP-first
  engine), [ADR-0004](0004-mcp-authority-and-evidence.md) (MCP authority),
  [ADR-0011](0011-bounded-automatic-actions.md) (no silent work)

## 1. Context

The owner asked on 2026-11-19:

> "i also want a walk through as a starting message to introduce saberops to user
> and setup with user with talk with them in it, it must be simple in a way that
> non technical can also get it and do the setup"

The readiness preflight (lane C6) already lists every missing setup item and the
one command that fixes each, but it speaks in engine vocabulary ("quota
evidence", "posture", "binding", "route") and prints the whole list at once. A
non-technical owner cannot use it as a conversation, and the MCP guide said
nothing about opening a first session.

## 2. Decision

1. **One text, four renderers.** `saberops.walkthrough` owns the introduction,
   the never-do promises, the glossary and the step wording. The MCP guide, the
   `start_walkthrough` tool, the `saberops_on` reply and `orch welcome` all render
   it, so they cannot drift apart.
2. **The walkthrough is derived, not stored.** Every call rebuilds the steps from
   `preflight.collect_preflight`; a step is a check mark when its findings are
   not `FAIL`. There is no cursor file: an interrupted conversation resumes at
   the first missing step, and a step cannot be reported done on stale evidence.
3. **One step at a time, with a declared authority kind.**
   - `agent`: the agent may run the printed command after the owner says yes
     (installing a CLI, choosing a build or reviewer route, setting the check
     command, starting the scheduler helper).
   - `answer`: the agent must ask for one value and never invent it (a test
     command, a remaining plan amount); it then runs the command with that value.
   - `owner`: the agent never runs it. Confirming a plan, signing in to a CLI,
     allowing or blocking a provider, freeing disk space and updating a CLI are
     described and handed over with the exact command to type.
4. **A fresh store opens with the introduction.** Turning SaberOps on in a CLI
   when the owner store holds no job opens with the plain introduction (what it
   does, what it never does, how long setup takes) and points at
   `start_walkthrough`. The same text is offered on demand ("walk me through
   saberops") and in a terminal through `orch welcome`.
5. **Plain writing is checked, not assumed.** `readability_problems()` checks the
   introduction and every step text for length (two sentences, bounded words) and
   for a listed jargon set; words that cannot be avoided (job, review, accept,
   Git) are defined in the glossary instead. Commands and preflight detail are
   exempt because they must stay exact.
6. **Language follows the owner.** The payload tells the agent to say the
   walkthrough in the owner's language and to keep every command exactly as
   written.
7. **Nothing new is authorized.** The walkthrough describes the existing
   preflight remedies. It cannot confirm billing, accept a change, merge, push or
   change a mode, and it never runs an `owner` step.

## 3. Consequences

- The first session has a friendly opening instead of a technical guide, and the
  same words are available from the CLI without an agent.
- Because progress is recomputed, the walkthrough cannot claim a check mark for a
  setting that has since been removed; it may re-ask a step the owner undid,
  which is the safe direction.
- The owner-only boundary of the MCP server is unchanged. The new guarantee is
  narrower: the walkthrough marks billing and sign-in as the owner's, and the
  agent is taught to hand them over rather than attempt them.
- The step set follows `preflight`; a new readiness finding with no plain
  mapping still appears, conservatively as an owner step, so nothing is silently
  dropped from setup.
