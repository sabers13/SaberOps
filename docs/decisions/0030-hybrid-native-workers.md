# ADR-0030: Hybrid native subagent workers under two-layer control

- **Status:** Accepted. Implemented in Phase 1 lane F16 (Wave 3).
- **Date:** 2026-11-19
- **Decider:** Saber (owner), Phase 1 plan lane F16
- **Related:** [ADR-0004](0004-mcp-authority-and-evidence.md) (MCP authority and
  evidence), [ADR-0008](0008-candidate-bound-acceptance.md) (candidate-bound
  acceptance), [ADR-0016](0016-steering-note-channel.md) (steering notes),
  [ADR-0020](0020-owner-mode.md) (owner mode),
  [ADR-0023](0023-proxy-subagents-per-cli.md) (proxy subagents),
  [ADR-0024](0024-model-family-tokens.md) (family tokens)

## 1. Context

The owner asked for a hybrid: when all workers run inside one CLI, use that
CLI's own subagent mechanism, and mix those native workers with ordinary
SaberOps workers that run outside it. They also asked how to combine the
orchestrator's instructions with MCP control inside the subagent, and required
that closing the CLI pauses the work "no matter what" because a leaking native
child would be serious.

A native subagent has real advantages: it is cheap to launch in the session the
owner is already in, it appears in the CLI's own subagent list, and it has no
second setup. It also has hard limits. SaberOps cannot kill its process, see it
once it stops calling, measure its quota independently, or prove it stayed in a
worktree. A subagent that ignores its instructions is invisible to SaberOps
until it speaks again.

The honest design is therefore **cooperative control with enforced evidence**:
the child agrees to report through MCP, and SaberOps accepts nothing it cannot
verify. The orchestrator's own native control is the enforcement of last
resort for stops, because only the host CLI can stop its child.

## 2. Decision

1. **Off by default, and off removes the surface.** Native workers exist only
   while `ORCH_NATIVE_WORKERS` is truthy. With it unset the four MCP tools
   (`native_claim`, `native_heartbeat`, `native_controls`,
   `record_native_attempt`) are not registered at all, so no client can reach
   the path.
2. **One vendor at a time, host vendor only.** The supported native vendors are
   `claude`, `codex` and `opencode`, and a native child may only run on its
   host's own vendor. Antigravity is refused as an automated worker and remains
   an orchestrator with its own agents only.
3. **SaberOps prepares the work.** `native_claim` requires a QUEUED job, creates
   the run row, creates the worktree at the job's pinned `base_sha`, creates a
   RUNNING attempt, and only then marks the job dispatched. It returns the run,
   attempt, worktree, branch, pinned base and the heartbeat deadline. The child
   never chooses the worktree.
4. **Cooperative liveness.** The child heartbeats through
   `native_heartbeat`, which also delivers pending steering notes once (the
   attempt boundary of ADR-0016) and returns any stop request. Silence past
   `ORCH_NATIVE_HEARTBEAT_SECONDS` (default 600 s) is computed from durable
   events: `native_controls` (and the job status view) mark the job
   needs-attention and post one durable stop request. A stop never clears.
5. **Stop is a request, not a kill.** Owner cancellation of a native job posts a
   `native_stop_requested` record and moves the job to CANCELLED; the child's
   later report is refused because the job is no longer RUNNING. The
   orchestrator stops the child with its own native control. SaberOps never
   claims the process is gone.
6. **Reports fail closed on evidence.** `record_native_attempt` requires a
   full-SHA candidate and refuses unless: the vendor/model/base match the claim;
   the candidate descends from the pinned base; the candidate is the tip of the
   branch SaberOps created; the reported worktree is the prepared one with a
   clean HEAD at the candidate; every changed path is a plain relative path
   inside it; and the canonical checkout's HEAD, branch and cleanliness are
   exactly what they were at claim time. Missing or mismatched evidence leaves
   the job unfinished and records the rejection.
7. **Acceptance is unchanged and still needs the gate and a different family.**
   A verified report runs the frozen project gate on the exact candidate and
   persists its receipt, then runs the ordinary SaberOps reviewer. A reviewer
   whose family matches the host vendor fails the attempt
   (`NATIVE_REVIEW_SAME_VENDOR`); the host's own subagent is never the reviewer.
   Only then does the run complete and the job succeed, and only the owner can
   accept through the existing candidate-bound path (ADR-0008), which re-checks
   the gate itself.
8. **Quota is labelled honestly.** Before the claim and after the report,
   SaberOps may read the host subscription through the existing safe,
   metadata-only quota readers (`ORCH_NATIVE_QUOTA_SAMPLE` opt-in). The
   difference is recorded as `measured`; otherwise the mean of earlier measured
   attempts is recorded as `estimated`; otherwise `unknown`. Every native
   attempt carries `supervision: host` and a quota label; it is never presented
   as a SaberOps measurement.
9. **Two-layer control is generated, not remembered.** `orch connect --skill`
   installs a `saberops-worker` definition beside the skill in each host's own
   subagent format: Claude Code agent markdown (`tools`, `permissionMode`,
   `model: inherit`), Codex agent TOML (`sandbox_mode = "workspace-write"`,
   inherited model), OpenCode agent markdown with per-agent permissions that
   allow only read/edit/bash and the native MCP tools. Each file is
   marker-guarded, atomic and reversible. Instructions require: claim, work only
   in the returned worktree, commit on the returned branch, heartbeat with
   progress, read steering and stop requests on every call, report evidence,
   and never accept, push, merge or start a second SaberOps server. The planner
   guide tells the orchestrator to launch native workers only from that
   definition, to call `native_controls` while waiting, and to stop the child
   with its own native control when SaberOps posts a stop.
10. **Concurrency is capped per host.** `ORCH_NATIVE_MAX_CONCURRENT` (default 1)
    bounds concurrent native attempts; a further claim is refused with
    `NATIVE_CONCURRENCY_LIMIT`.

## 3. Consequences

- Native work becomes a first-class job record with a real candidate, gate
  receipt, independent review and owner acceptance, not a side channel.
- The owner keeps the hybrid they asked for: native children and ordinary
  SaberOps workers share one job list, one candidate policy and one acceptance
  action.
- SaberOps cannot promise kill, stall detection or quota accuracy for a native
  child. It promises that a silent child becomes needs-attention, that a stop is
  posted for the orchestrator, and that nothing it cannot verify reaches
  acceptance. Those limits are labelled on every attempt.
- Closing the host CLI stops its native children by construction, so a closed
  CLI cannot leave a native worker running. SaberOps-side jobs run outside the
  CLI and are not stopped by it; pausing SaberOps-side dispatch when the
  planning CLI closes is a separate guarantee (lane F17). This lane never
  restarts native work on its own.
- The generated worker definition depends on the native tools existing. With the
  flag off the definition says so; it never falls back to editing files outside
  SaberOps.
