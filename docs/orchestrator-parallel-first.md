# Orchestrator parallel-first policy (repository-tracked)

Status: owner-authorized policy for **authorized repository Orchestrator
sessions**. Effective 2026-10-16 this policy is **benefit-first with optional
concurrency**: optimize end-to-end accepted integrated wall time with the
fewest coherent tasks; "parallel-first" is the retained title/file path for
compatibility, not a requirement to maximize concurrency, refill slots, or
split work. This is the tracked, discoverable startup policy for a fresh
Orchestrator session that has access to this repository. It records
owner-approved working rules *and* the product behaviour SaberOps must
eventually implement.

Reading this file does **not** activate any scheduler. It instructs an agent;
deterministic software still decides. Section 2 states precisely what is
implemented today versus what is only a target.

## 1. Authority and precedence

1. Explicit owner instructions for the current task.
2. Existing repository safety, architecture, security, engineering, CI and
   release rules ([README.md](../README.md), [pyproject.toml](../pyproject.toml),
   `.github/workflows/ci.yml`, [DESIGN.md](../src/saberops/DESIGN.md)).
3. This policy.
4. Local, Git-ignored working notes under `.orch/`, when present. Those notes
   are operational memory, never the canonical source, and never a substitute
   for a tracked rule.

The Orchestrator plans, writes bounded prompts and reviews evidence. It does
not implement source, run gates, merge, push, move the target branch, delete
resources, or trigger paid/provider calls unless the owner explicitly
authorizes that posture for a specific task.

**LLMs advise; deterministic software decides.** A model proposal never becomes
owner policy, nor does it change accepted scheduling, without the deterministic
validation and owner approval path.

## 2. Current capability versus target (no overclaiming)

**Implemented today (repository code).**

- `saberops.parallel_work.execute_team` runs **one** team per parent run: two to
  six coherent tasks, two to four local workers, one integration worktree,
  dependency waves, ordered merge into the integration branch, one combined
  gate and review. It is a single-run team executor, **not** a cross-project,
  continuously refilled scheduler.
- `saberops.planning.represent_independent_ready_work` and `ready_dependents`
  are the persisted *within-run* plan/package dependency authority; they perform
  dependency-state transitions against the run database.
- `saberops.orchestrator_consumer.fetch_initial_orchestrator_guidance` is a
  bounded (roughly 600-character) *initial-objective advisor* for the first
  worker. It is **not** a planner and must not be treated as scheduling
  enforcement.
- Early pure slices may exist on unmerged branches (for example a 3x5
  project-child slot-admission projection, and a cross-phase ready-frontier
  readiness projection such as `saberops.ready_frontier`). These are pure
  **projections**. They perform no dispatch, hold no lease and are not launch
  authority on their own.

**Target — not yet live.** A deterministic, owner-approved plan with stable
project/task IDs and a **continuously refilled cross-phase ready frontier**;
hard START versus deferred INTEGRATE edges; enforced **3 active projects x 5
executing child tasks** as two independent owner-adjustable limits; a shared
per-host storage reservation budget; exclusive worktree scopes; real G1,
independent-review, exclusive-G3 and owner-acceptance gates; exact-SHA
provenance and lease fencing; and a UI that shows real ready/blocked/held
reasons. This is implemented by existing steps (project/plan registration,
durable settings, wave admission, session management, first-run UI, security,
certification), **not** by the fact that this document exists. Do not label any
of it operational until those steps pass their gates, independent review and
owner acceptance.

## 3. Fresh Orchestrator startup read sequence

Read, in this order, before drafting any plan or status report:

1. `README.md` and `AGENTS.md` (root).
2. `docs/parallel-work.md` (current parallel-team behaviour).
3. This file.
4. `pyproject.toml` and `src/saberops/DESIGN.md` (engineering/design contract).
5. The ADR set that governs the work in scope.
6. Local ignored working notes under `.orch/`, if the session has them.

Then **verify live state against Git before trusting any note**: branches,
worktrees, and the most recent gate receipts. A dated note is not evidence.

## 4. Ready-frontier recompute triggers

Recompute the **eligible** set across **all** named phases — never only the lowest
displayed step or the current phase — on every one of these events:

- a new or revised owner-approved plan;
- a task completion;
- a task failure;
- an owner decision that lifts or imposes a hold;
- a capacity release (worker, gate slot or storage reservation);
- an accepted revision.

Recompute is deterministic. It must not require a model/provider call, and it
must never manufacture work to fill capacity.

Eligibility is not dispatch. A recomputed eligible task runs only when the
owner-approved plan admits it, exactly one accountable Worker holds its
task-ID lease, interfaces are independent, and conservative expected net
benefit outweighs coordination/review/G1/G3 overhead. A ready task may
legitimately wait; sequential, one-task, and zero-wave outcomes are valid.

## 5. Dependency edges: hard START versus deferred INTEGRATE

- A **hard START** prerequisite must be satisfied before a dependent may begin.
- A **deferred INTEGRATE** prerequisite does not block starting; it marks work
  that can only be accepted/merged after the prerequisite lands.
- A blocked or failed dependency freezes **only its own dependents**. Unrelated
  lanes in any phase continue.
- Phase names and three-digit display numbers are **never** dependency edges.
- Integration-ready is not implementation-startable, and
  implementation-startable is not integrated.

## 6. Task sizing and decomposition

Decompose into the **fewest coherent, meaningful, independently testable tasks**
with stable identity and non-overlapping write scopes. The initial Orch
documents per-task size, estimated critical-path duration, and the
coordination/review/G1/G3 cost tradeoff, and compares the proposed grouping
against a simpler/sequential alternative. Prefer a bounded, pure component with a real
contract over speculative stubs. Split or parallelize only on conservative
credible net benefit; when unclear, stay sequential. Do **not** split a task merely to occupy a
slot, infer interfaces that do not exist, duplicate an existing or code-complete
lane, or double-count a parent coordinator as a child task. Reject trivial extra
tasks. Material task split/merge/re-scope needs an owner-approved plan revision
that preserves task IDs, history, and evidence; prefer bounded same-task repair.
**ONE implementation Worker owns each task**, including its fixes.

## 7. Exclusive write scopes and frozen contracts

Every worker gets a separate branch/worktree, an exact pinned base, a narrow
exclusive `Touches`/test scope, an explicit `Must-not-touch` list, and frozen
input/output contracts. Analyze integration collisions and shared types
*before* launching. No worker edits another worker's source, the target branch,
or unrelated files. Overlapping scopes run in separate waves, never
concurrently.

## 8. Gates and review independence

- Each lane runs its own cheap **G0** (lint + strict types on changed files) and
  its targeting **G1** lane check on the committed head.
- Independent review uses a **genuinely new, cold session with a different
  underlying model**, selected *before* the session is opened, and checks the
  exact pinned SHA and the real diff. Semantic review is for **implementation
  Worker** candidates (including substantive same-task fixes and Surgeon
  corrective code routed into that task) — never automatic for pure Integrator
  merges, bridge-test-only commits, or a post-G3 final look.
- A pure Integrator only assembles already-reviewed implementation commits,
  resolves safe mechanical merges, records integration-only evidence, and runs
  the deterministic integration gate. **No new semantic Reviewer session for a
  pure merge.** Orch checks merge parents, exact SHA, evidence, contracts, and
  gates; the owner moves the target branch.
- Material production behavior or glue written during integration goes **back to
  one accountable implementation Worker** under the same task scope for G1 and
  task-scoped review — no bypass. Mechanical conflict resolutions with no
  semantic change and hermetic bridge-test-only additions stay Integrator
  evidence. When in doubt whether glue is substantive, fail closed to the Worker
  path. Never globally disable risk-adaptive review on ordinary Worker runs to
  skip integration reviews.
- There is **one exclusive heavy integration gate (G3)** per wave, on the
  integration branch, with ancestry/SHA evidence.
- The owner accepts and is the only role that moves the target branch.
- INFRA failures are not code failures; a blocked lane needs its G1
  continuation, not a reimplementation.
- Running jobs are never killed merely because the owner lowers a limit; block
  new starts until occupancy falls under the limit.

## 9. Owner limits and host bounds

- Default owner limits: **3 concurrently active projects** and **5 concurrently
  executing child tasks per active project**. These are two **independent
  upper-bound constraints** and owner-adjustable maxima — not a quota, not an
  occupancy target, and not a single global cap of 15. The runtime may run only
  1 of 15 allowed tasks. The derived ceiling is reported for observability only.
  Never auto-create, split, or dispatch work to fill free slots.
- Parent plan/wave coordinators, Orchestrator turns, and waiting/review/terminal
  items do **not** consume an executing child slot.
- Actual launch also respects provider/billing posture, disjoint source
  ownership, host gate slots (at most a small number of simultaneous light
  gates; the heavy gate is exclusive), and a shared per-filesystem storage
  reservation budget that sums the remaining-growth reservations of every
  concurrently admitted writer plus a recovery margin. Unknown estimates fail
  closed. No hidden or automatic paid/provider spend.

## 10. Numbering: three-digit display versus immutable identity

Roadmap steps use one contiguous global **three-digit display sequence**
(`001..NNN`) with no resets, letters, fractions or unexplained gaps, organised
into named narrative phases. The three-digit number is a **display/position
label, not identity**. Stable task IDs, dependency references, branches,
worktrees, commits and evidence cite the immutable identity. Phase groupings
imply no execution order. Renumbering preserves identity, the dependency graph,
evidence SHAs, approval history and a versioned old-number mapping; it never
rewrites completed evidence or an in-flight lane. Coordinate new artifact paths
as `NNN-short-lane` with phase-matched folders, without renaming running
branches or invalidating prior reviews.

## 11. Idempotency and blocked-work continuation

Scheduling and accounting are idempotent: recomputing the frontier, re-reading a
snapshot, or replaying a launch attempt must not duplicate a task, double-count
a reservation, or falsely free capacity. Duplicate identical records are one
alias; conflicting identities fail closed. A **wave is optional** — zero, one,
or several tasks are all legitimate. When one dependency is blocked, the
Orchestrator **continues unrelated ready work across other phases where
beneficial** instead of serialising the plan behind the blocked subtree — but a
merely eligible task is never obligated to launch, and sequential or idle
outcomes remain valid when cost, risk, or the owner plan says so.

## 12. New-session bootstrap (paste)

> Act as the Orchestrator for this repository. Before proposing any plan,
> read `README.md`, `AGENTS.md`, `docs/parallel-work.md` and
> `docs/orchestrator-parallel-first.md` in full, then verify live Git state
> (branches, worktrees, recent receipts) before trusting any note. Optimize
> expected elapsed time to owner-accepted integrated output with the fewest
> coherent tasks: one accountable implementation Worker per task (fixes return
> to the same Worker), optional waves (sequential, one-task, and zero-wave are
> legitimate), and 3x5 only as upper-bound limits — never invent or split work
> to fill slots. Treat phase names and three-digit display numbers as
> labels, never dependencies; separate hard START from deferred INTEGRATE
> edges; recompute eligibility across all phases after every plan change,
> completion, failure, owner decision, or capacity release, remembering that
> eligible does not mean obligated to dispatch. Require G1 plus one
> task-scoped independent model-distinct review on implementation Worker
> candidates; pure Integrator merges and bridge-test-only commits get no
> automatic semantic review, and substantive integration glue returns to a
> Worker for G1 and review. Require one exclusive integration gate with exact
> ancestry/SHA evidence. Never implement source, run
> gates, move the target branch, push, or spend without owner authorization.
> Report a ready-now matrix with explicit blocked/held reasons.

Primary discoverability is the tracked `AGENTS.md` anchor, which points every
new repository Orchestrator here. A session that has **no** repository access
(for example a plain external chat) cannot see this file automatically and must
be given repository access and this read instruction by the owner.
