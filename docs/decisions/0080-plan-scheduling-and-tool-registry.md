# ADR-0080: Plan scheduling and the logical tool registry leave the core

- **Status:** Accepted. Implemented in phase 1.1 lane H19.
- **Date:** 2026-11-23
- **Decider:** Saber (owner), phase 1.1 owner answer Q1/Q13 and "maximise the
  deletion" (2026-11-22)
- **Related:** [ADR-0005](0005-mcp-first-engine-and-phase-2-board.md) (the
  planning CLI is the orchestrator), [ADR-0016](0016-steering-notes.md)
  (steering), [ADR-0060](0060-gate-protection-on-the-real-paths.md) (the check
  path this lane does not touch)

## 1. Context

The phase 1.1 census (H0, sections 8-9) traced a real `PLAN.md` submission
through the scheduler: `submit_plan_file`, `reconcile_plan_lanes`,
`project_ready_frontier`, `plan_state` and the plan store are all reached, and
the B3 store holds real plans with lane rows and events. Two layers beside that
live path were reached by nothing:

- **Chained wave scheduling.** `ready_frontier.schedule_chained_waves` (204
  lines) and the `PlanRunReport`/`HeldSummary`/`build_plan_run_report` block
  (118 lines) were called only by their own tests. The live plan path projects
  a ready frontier; it never sequences or reports waves.
- **The logical tool registry.** `saberops.tooling` (1,298 lines) reads
  `tools.json` from the owner config, freezes a per-run tool contract, renders
  an `[AVAILABLE TOOLS]` prompt section, journals invocations and feeds
  telemetry into dispatch evidence. No repository file, setup step, benchmark
  store or B3 run contains `tools.json`; all 59 traced runs have
  `tool_refs = []`, no tool was ever resolved or invoked, and MCP and `PLAN.md`
  cannot set a tool ref at all. The only producers are a manual
  `orch run --tool <ref>` and `orch tools exec`, both requiring the owner to
  hand-author a registry file.

Both layers were maintained, tested and described in design docs while
enforcing nothing on any measured path. The phase 1.1 rule is to delete what no
path reaches instead of keeping layered knobs for a feature nobody uses, while
every safety property (quota, consent, isolation, checks, review, acceptance,
pause) stays.

## 2. Decision

1. **The chained-wave scheduler and its run report are deleted** from
   `ready_frontier.py`, with their tests. `project_ready_frontier` remains the
   one plan-readiness projection, and `HeldTask` stays because that projection
   builds and returns it (the census entry claiming `HeldTask` was unused was
   wrong; the lane's own verification overrides it).
2. **The logical tool registry is deleted**: `tooling/contracts.py`,
   `generator.py`, `invocation.py`, `registry.py` and their tests; `orch tools`
   (`list`/`describe`/`exec`); the `--tool` flag; `tool_refs` on `RunConfig`
   and `RunRequest`; `paths.get_tool_registry_path()`; `tool_registry` on
   `Orchestrator`, `RunSupervisor` and `OrchestratorService`; the frozen tool
   contract, tool-telemetry and `[AVAILABLE TOOLS]` prompt plumbing in
   `orchestrator.py`, `supervisor.py`, `native_workers.py`,
   `capacity/loop.py` and `context_projection.py`.
3. **The continuation precondition drops the tool contract.** Continuing a
   failed run required a frozen tool contract; with the feature gone the
   requirement would refuse every new run. Routing and control-plane contracts
   remain mandatory and unchanged, so a continuation still reproduces the
   routing and authority it was admitted under.
4. **Stored evidence stays readable, not writable.** New runs write no tool
   contract. The existing nullable `runs.tool_contract_json` /
   `tool_contract_digest` columns, their migration, the handoff summary and the
   observability snapshot keep reading them, so a pre-1.1 row still reports
   what it recorded; nothing depends on a new row having one.
5. **`orch-context` keeps its module.** `tooling/context_cli.py` is the
   read-only evidence surface behind `orch-context`, which is live. It stays
   until the one-report view (H9/H24) absorbs that verb.

## 3. Consequences

- The core is smaller by roughly 1,800 source lines and 220 test lines, and no
  behaviour on a traced owner, planner or scheduler path changes: `PLAN.md`
  submit, mid-run edit, landing order and `STATE.md` drills pass unchanged.
- A run can no longer expose a manifest tool to a worker. No measured path
  could do so before, and the four vendor CLIs already bring their own tools;
  the planner's work is job and lane structure.
- `grant_for_sandbox(tool_refs=...)` and `CapabilityEntry.tool_ref` remain as
  inert sandbox policy surface; no caller passes a ref. Removing them is a
  separate sandbox-policy change, not part of this lane.
- `ScheduledTask`, `ScheduledWave` and `WaveScheduleError` stay for the
  web-only wave optimizer, which H10 owns.
