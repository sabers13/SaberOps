# Parallel Work and Git tracking

Parallel work is **several jobs or PLAN.md lanes**, not an in-run team.  Each
job is one worker with one coherent objective; a plan submits one ordinary job
per ready lane.  Routing, capacity, worktrees and owner acceptance stay where
they are for every job.

Phase 1.1 removed the legacy in-run team mode (`parallel_work.py`,
`execution_mode`) and the persisted single-package run plan.  Splitting work is
the planner's job: a large task submitted as one job runs as one job, and the
same work written as PLAN.md lanes runs as lanes.  See ADR-0083.

A stored pre-1.1 run may still carry `parallel_parent_run_id` /
`parallel_task_index`; those markers are never produced now and are kept only
so an old internal team worker is still refused as an acceptance candidate.

## Current capability versus target scheduler

**CURRENT — implemented today.**  A PLAN.md file submits one ordinary job per
ready lane from the scheduler tick (`orch plan-file ...`, phase 1 lanes P1-P3).
The plan-data layer is `saberops.project.ProjectRegistry`,
`saberops.project_scope.plan_store` and the deterministic `saberops.ready_frontier`
projection, surfaced read-only via `orch project list|register|active` and
`orch plan-store show|validate`.  It is **not** a cross-project, continuously
refilled scheduler.

**TARGET — not yet implemented.** A deterministic, continuously refilled
**cross-phase ready frontier across 3 active projects x 5 executing child tasks**
(two independent, owner-adjustable **upper-bound limits**, not a quota or
occupancy target), admitted only under an **owner-approved plan version** with
stable project/task identity, **exactly one active implementation Worker lease
per task**, hard START versus deferred INTEGRATE edges, exclusive write scopes,
a shared per-filesystem storage reservation budget, lease fencing, exact-SHA evidence,
one exclusive integration gate, and a UI that shows real ready/blocked/held
reasons. A blocked early-phase dependency must not stop unrelated later-phase
implementation **where beneficial** — but eligibility never obligates dispatch:
sequential, one-task, and zero-wave outcomes are legitimate, and the runtime
must never auto-create, split, or launch work to fill free slots. A ready task
need not dispatch when cost, risk, or the owner plan says no. Semantic review
covers **implementation Worker** candidates only; pure integration merges and
bridge-test-only commits get **no automatic semantic review**, while substantive
production glue returns to an accountable Worker for G1 and task-scoped review.
That stage guard (trusted role/stage/task provenance, never prompt or
branch-name inference, no phantom pending-REVIEW for integration-only work) is
**TARGET runtime behavior, not yet wired**: the UI and status evidence must not
present a phantom review blocker for integration-only artifacts. At every
approved plan-run start and frontier-changing transition the target runtime
deterministically recomputes the ready set under the frozen plan, selects the
advantageous concurrent cohort, and starts each via atomic task-ID
ownership/worktree reservation — readiness is dispatched, not merely
displayed — without LLM per-slot decisions or invented costs. Every scan
persists an audit (trigger, plan version, eligible/active/selected/held IDs
with reasons, evidence versions, next trigger); the UI shows
READY/SELECTED/RUNNING/HELD/INTEGRATION-BLOCKED separately. Manual operation
follows the same check with owner-launched sessions: Orch states the scan
conclusion and presents all selected pinned prompts together, never
duplicating running Workers. The manual pre-run check is operative now;
runtime enforcement is TARGET, not yet proven on main. Early pure *projections* (for example
`saberops.ready_frontier`, a readiness/hold projection, and a project-child
slot-admission projection) may land on unmerged branches; they compute nothing
about live occupancy, hold no lease, dispatch nothing, and are never launch
authority by themselves. The target behaviour is delivered by the existing
project/plan, settings, wave-admission, session, UI and security
work, **not** by documentation. See
[orchestrator-parallel-first.md](orchestrator-parallel-first.md) for the
tracked Orchestrator policy and its current-versus-target section.

## Starting in an empty project

Configure an executable project gate before starting the team. If the repository
has no tests yet, make the first dependency create the shared contract and its
runnable tests; later workers add their own tests in disjoint files. Every stage
inherits the same gate, so it must be usable from that first dependency onward.
For a standard-library Python project, one suitable command is
`python3 -B -m unittest discover -s parallel_board/tests -v`. The `-B` prevents
bytecode cache writes during immutable verification; test databases and scratch
files must also live outside the repository, for example in temporary directories.
The final integration tests must require the complete application.

Gate changes affect future runs. A retry starts a new run under the failed run's
frozen policy; after correcting project settings (`orch project ...`), start a
fresh run using the corrected policy. An installed CLI alone is not verified
model access: readiness is recorded by explicit model verification, `orch
doctor` reports local readiness, and unknown readiness keeps refusing dispatch.

## Execution and Git

1. The parent freezes its objective, original project commit, routing,
   execution identity, tools, gate and review policies through normal admission.
2. An integration worktree starts at that original commit on
   `orch/<parent-run>/integration`. The project checkout stays unchanged.
3. Ready tasks with disjoint declared files run together, up to the selected
   limit. Dependencies and shared file scopes run in separate waves. Every
   worker uses the existing supervised run lifecycle, its own durable process,
   attempts, transcript and `orch/<worker-run>/a<N>` worktree/branch.
4. Workers in one wave start from the same integration commit. They inherit
   the parent's frozen execution policy; internal candidates are not published
   or independently acceptable. Their gates and configured reviews still apply.
5. Completed candidates are checked against their declared files and wave
   ancestry. Their branches merge into the integration worktree in stable task
   order, regardless of completion order. Dependencies start from the integrated
   result of earlier waves.
6. A final parent worker inspects the combined result and can fix integration
   defects. The parent's normal gate and review run against the full candidate,
   with the original project commit as the comparison base. This adds a final
   worker invocation as well as the task workers.
7. Publication and acceptance independently verify the task-to-worker links,
   candidate provenance, scoped changes and actual Git merge parents. An event
   claiming integration alone cannot authorize a foreign candidate. The normal
   acceptance checks and post-integration gate remain in effect.
8. The owner accepts the parent candidate using the existing acceptance flow.
   Only this step updates the project's target branch. Target movement still
   causes the existing preflight/CAS checks to refuse acceptance.

Work scopes describe allowed candidate changes. They are validated before
integration; they are not a new operating-system filesystem sandbox. This mode
uses the established supervised worker path and does not silently substitute it
for SaberOps' separate autonomous containment mode.

## Failures and interruptions

Large review diffs use the adapter's verified prompt transport. Antigravity
keeps `--print` for small prompts and pipes plain text without that flag above
the argument safety threshold. A live 42,421-byte prompt returned an exact
response and terminal SUCCESS with the installed CLI on 2026-10-10. This keeps
the complete review diff and existing read-only/completion checks; it does not
truncate review evidence or bypass required review. Recovery reference files
should live outside disposable `/tmp` when a workflow must survive a restart.
OpenCode's managed sessions deny external-directory access. For owner-requested
recovery from saved work in the same repository, name the source commit and
the exact owned files in the task. Workers can read each file with
`git show <commit>:<path>` and copy its bytes into their worktree without
external-directory access or importing branch history. The new scoped gate,
independent review and parent integration checks still apply to those copies.

- A worker failure stops its wave and blocks later tasks. Existing worker and
  integration branches remain available for inspection.
- A Git conflict aborts the merge, records the affected files and preserves
  worker branches. It does not ask a model to resolve conflicts automatically.
- A combined gate/review failure preserves the combined candidate without
  making it acceptable. If a failed parent has a committed final attempt, engine
  continuation can repair that candidate without rerunning the completed team.
  A retry starts a new run instead.
- Stop on the parent uses the existing fenced cancellation path (`orch cancel`)
  for its active workers before stopping the parent. Unknown ownership or
  unconfirmed worker termination refuses cancellation rather than claiming that
  execution stopped.
- Server refresh/restart does not relaunch durable owners. An interrupted team
  is preserved rather than automatically replayed or merged. A parent that dies
  abruptly can leave detached workers finishing isolated branches; inspect those
  workers through their run reports and stop them (`orch cancel`) before
  retrying. Automatic mid-team recovery and manual conflict resolution are
  separate future work.

The run-detail status page links the team's checks, review and acceptance
evidence. The engine does not force-push, reset the owner's checkout, delete
branches, or automatically accept work.

## Open-source references and attribution

These repositories were shallow-cloned into
`/tmp/saberops-parallel-references-o2TMiU` on 2026-10-10. SaberOps implements the
following patterns in its existing Python architecture. No upstream runtime,
source file, skill prompt, service or dependency was vendored or installed.

| Reference | Pinned commit | Material inspected and adopted pattern |
| --- | --- | --- |
| [Agent Orchestrator](https://github.com/OrchestratorInc/agent-orchestrator) (formerly ComposioHQ) | `c72956ae6df2a658f941afe62c2c61275ec6f20a` | `backend/internal/adapters/workspace/gitworktree/workspace.go`, `commands.go`, and workspace documentation: isolated session worktrees, durable task/workspace tracking and preservation of work. |
| [Gas Town](https://github.com/gastownhall/gastown) (formerly steveyegge) | `649b832b7672bc7a2dbef26f5983aba6198b819b` | `internal/refinery/manager.go`, `internal/templates/roles/refinery.md.tmpl`: ordered integration queue, explicit conflict state and checking the assembled result. |
| [Superpowers](https://github.com/obra/superpowers) | `8ca22dba9a94f28898bbce59f2537ff4d87c747d` | `skills/writing-plans`, `dispatching-parallel-agents`, `using-git-worktrees`, `subagent-driven-development`, `finishing-a-development-branch`: coherent task briefs, dependency/file separation, independent review and deliberate integration. |

Agent Orchestrator is Apache-2.0 licensed. Gas Town is MIT licensed, copyright
2025 Steve Yegge. Superpowers is MIT licensed, copyright 2025 Jesse Vincent.
The original licenses can be read at the pinned upstream revisions. Temporary
clones are research artifacts, not release contents. This is architectural adaptation
with original SaberOps code,
not incorporation of their implementations or license transfer.

## Validation

PLAN.md lane submission, mid-run edit, landing order and STATE.md drills run in
the full gate; the per-lane rig and benchmark cover job submission, routing,
worktrees, checks, independent review and owner acceptance.  The repository's
normal lint/type/test gates cover the application integration.

See the [release notes](releases/0.4.0.md) for repository-wide release validation.
