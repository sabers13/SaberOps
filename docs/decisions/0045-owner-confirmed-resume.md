# ADR-0045: Resuming paused work is an owner-confirmed action

- **Status:** Accepted. Amends decision 3 of
  [ADR-0029](0029-no-work-without-a-planning-session.md).
- **Date:** 2026-11-21
- **Decider:** Saber (owner), Phase 1 B1 real-client recheck (D16)
- **Related:** [ADR-0029](0029-no-work-without-a-planning-session.md) (no work
  without a planning session), [ADR-0011](0011-bounded-automatic-actions.md)
  (no silent work), [ADR-0023](0023-proxy-subagents-per-cli.md) (proxy
  subagents)

## 1. Context

ADR-0029 made the work gate sticky and named two confirmations that clear it:
`orch resume` from the owner and the mutating MCP `resume_work` call from the
returning planning session. The B1 real-client recheck on 2026-11-21 exposed the
difference between the two. In both recheck sessions the planner model called
`resume_work` without the owner saying "resume", and the engine accepted it, so
work started after a pause the owner never confirmed (defect D16).

The text already said the returning session must not resume on its own, but
text cannot grant or withhold authority. The MCP surface still exposed a
mutating tool that any writer client could call, and a model under pressure to
make progress will call an available tool. Keeping the call means the pause
depends on model judgment rather than on the owner's decision, which is the
opposite of ADR-0029's rule and ADR-0011's no-silent-work boundary.

## 2. Decision

1. **Only the owner resumes.** A paused work gate is cleared only by the
   owner's `orch resume` (CLI) or the equivalent owner control on the local web
   surface. The mutating MCP `resume_work` tool is removed from the tool list
   and added to the never-exposed operations, so a client that calls it by name
   receives the explicit owner-only refusal (`-32010`) rather than a handler.
2. **No implicit substitute.** `get_pause`, `catch_up`, `get_job`, `wait_job`
   and the `saberops_on` reply keep reporting the pause and the exact owner
   command. They report no resume tool, because none exists. An absent session
   that comes back, a successful submit, switching SaberOps on, or starting a
   scheduler still never clears a pause.
3. **The guide says what to do.** The built-in guide, the installed skill and
   the proxy instructions tell the planner to ask the owner to run
   `orch resume`, never to run it itself and never to call a resume tool. They
   also say to use the SaberOps MCP tools rather than shelling `orch mcp on`,
   `orch mcp off`, `orch mcp release` or `orch mcp serve` from inside a
   session.
4. **The owner path is unchanged.** `orch resume` still clears the durable gate,
   parks any interrupted run, wakes the scheduler and restores the ordinary
   dispatch path. Jobs, checkpoints, worktrees and evidence remain untouched by
   the authority change.

## 3. Consequences

- The pause guarantee no longer depends on a model's interpretation of the
  conversation. A planner that wants to continue has one honest move: ask the
  owner to run `orch resume`.
- A returning session can still submit and wait while paused; nothing dispatches
  until the owner confirms. This keeps the owner's review loop usable without
  handing resume authority to the agent.
- Clients that cached the old tool list see `resume_work` fail with the
  never-exposed refusal and the guide's owner instruction. That is a deliberate
  visible break, not a silent fallback.
- The authority change is covered by regression tests for the tool list, the
  refusal code, the gate projection, `wait_job`, the turn-on reply and the
  guide/skill text.
