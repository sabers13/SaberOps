# ADR-0015: Plan files are authored intent; plan state is a rendered projection

- **Status:** Accepted. The file format, parser, activation, format-2 revision
  and `PLAN.md` renderer are implemented in Phase 1 lane P1 (Wave 3). Lane
  submission, the owner-database plan mapping and the scheduler reconciler are
  implemented in lane P2. The rendered `STATE.md`, its projection, the
  git-ignored checkout link and the read-only `plan_status`/plan `catch_up`
  paths are implemented in lane P3. Mid-run plan edits, the revision diff and
  the edit matrix — including the promise that a running lane is never
  signalled — are implemented in lane P4. Lane P5 implemented the typed
  owner-mode start decision for auto-advance, plan complete/stalled/
  needs-decision notices, the plan section of the away return summary, and the
  planner/skill and MCP documentation.
- **Date:** 2026-11-19
- **Decider:** Saber (owner), Phase 1 plan lane P1 after the owner approved
  every recommended choice for the plan-file design
- **Related:** [ADR-0005](0005-mcp-first-engine-and-phase-2-board.md) (one
  record of truth; no wave executor), [ADR-0006](0006-stale-candidate-refresh.md)
  (candidate refresh), [ADR-0008](0008-candidate-bound-acceptance.md)
  (candidate-bound acceptance), [ADR-0011](0011-bounded-automatic-actions.md)
  (bounded automatic actions), [ADR-0014](0014-job-dependencies.md) (a
  dependency waits for acceptance)

## 1. Context

The owner asked for the parallel planning structure used in this project to be
available in the product: a human-readable plan with waves and lanes, and a
progress file that shows what each lane is doing. Before this decision a plan
was stored JSON holding task ids, phases and dependencies only. Lane prose —
why the lane exists, what to do, what is out of scope, when it is done — had
nowhere to live, so a worker could not receive the brief the owner wrote.

Two separate jobs must not collapse into one file:

- **Intent** is authored. A human or a planning CLI writes the lanes, the
  ordering and the acceptance criteria.
- **Progress** is observed. It is derived from job, run, attempt, gate, review
  and `run_accepted` records. Hand-editing it would create a second, drifting
  truth.

The existing plan store already supplies immutable, hash-verified revisions,
one active revision per project and fail-closed DAG validation. Reusing it keeps
the repository's one-record-of-truth rule and avoids a second plan database.

## 2. Decision

1. **`PLAN.md` is tracked at the repository root.** One active plan per
   project, matching the one-active-revision rule of the plan store. The file
   is a reviewed specification: it survives clones and its changes are commits.
   `ROADMAP.md` stays the long-range scope export; `PLAN.md` is the dispatchable
   execution plan, and each file names the other's role.
2. **The authoring format is Markdown plus one fenced `plan` block per lane.**
   The header block carries `format`, `plan_id` and an optional `base`. Each
   `### <id>. <title>` lane carries `Why`, `Do`, `Out of scope`, `Done when`
   and `Needs` prose plus exactly one machine block holding structure only:
   `id`, `wave`, `depends_on`, `owns`, `size`, `effort`, model-family
   include/exclude, `priority`, `run` and `kind`. Unknown machine fields are
   refused so a typo cannot silently do nothing. Waves are presentation labels;
   only `depends_on` is an edge. `kind: "scout"` (F19, ADR-0026) makes a lane a
   read-only report lane; its report becomes the starting context of the lanes
   that depend on it. An owner-held lane cannot be a scout because it never
   dispatches.
3. **A lane may state bounded routing preferences, never a route.** `size`,
   `effort` and model-family include/exclude map onto the existing
   `JobRequirements` shape. Provider, model, tier, billing, route, quota and
   account stay refused, preserving ADR-0001's invariant.
4. **Owner-held lanes are first-class.** `run: "owner"` marks work the engine
   must never dispatch — a decision, a manual check, a real-vendor run that
   needs the captain present. Such a lane may not declare job-only fields. It
   is completed only by an explicit owner command, never by a model and never by
   editing the progress file.
5. **The plan store remains the record of truth, with a version-aware hash.**
   New revisions are format 2 and carry plan identity and lane detail.
   `verify()` hashes each revision in its own format, so a stored format-1
   revision keeps verifying after the extension. A changed plan is a new
   revision; revisions are immutable. Activation is idempotent on semantic
   content.
6. **Activation refuses a dirty tree.** Acceptance already requires a clean
   target tree and a target HEAD equal to the run base. Activating an edited
   plan while the tree is dirty would leave every candidate unaccepted, so
   activation fails closed and names the exact remedy
   (`git add PLAN.md && git commit -m "plan: activate <plan_id>"`).
7. **Rendering is deterministic and round-trips.** `render_plan_markdown`
   renders a stored revision to the authored format, and parsing that output
   reproduces the same lanes. `orch plan-file init`, `validate`, `activate` and
   `export` are deterministic CLI verbs.
8. **Progress is a rendered projection, never authored.** `STATE.md` is
   generated from the active revision and engine records. Its canonical file
   lives in the owner state directory; the repository view is a git-ignored
   symlink in the canonical checkout, never committed and never created in
   worker worktrees. A committed or merely untracked file would either move the
   target HEAD on every render or make `git status --porcelain` dirty and block
   acceptance. The projection distinguishes `candidate` from `accepted` and
   `owner` from `queued`; `SUCCEEDED` never renders as done.
9. **The plan layer starts lanes; the scheduler decides capacity.** The
   reconciler runs inside the scheduler tick so an activated plan can advance
   while the owner works in another window. A lane is ready only when every
   dependency is accepted (`run_accepted`), not merely completed. It submits
   one ordinary job per ready lane; parallelism, memory, quota, routes, retries
   and worktree lifecycle stay in the existing scheduler. There is no wave
   executor and no second scheduler. Every lane submission passes through the
   single function `submit_lane_job`, which consults the bounded pause gate
   (`LANE_SUBMIT_GATE`) and, for scheduler-driven submission, refuses when no
   live planning session holds the MCP planning lease. The scheduler lease is
   not a planning session: closing the planning CLI pauses plan lanes, as the
   owner required for lane F17. F17 extends that same pause to all other work
   ([ADR-0029](0029-no-work-without-a-planning-session.md)); the plan never
   outranks an owner pause. An owner-invoked activation or
   `orch plan-file done` is a present owner action and is exempt only from the
   planning-session check, never from the pause gate.
10. **Automation is bounded and owner-controlled.** Auto-advance submits jobs
    an activated plan already allows. Auto-refresh may refresh stale
    candidate-ready siblings in deterministic landing order (wave, then lane
    id), but it never accepts and never pushes. Pause, resume, abandon,
    auto-advance, auto-refresh, owner-lane completion and acceptance remain
    owner-only.
11. **Running work is never cut off.** A lane edited or removed mid-run that is
    already running finishes under its original brief. A changed lane with a
    candidate-ready result becomes an owner decision, never an automatic
    discard. A lane that is already accepted, or already completed by the
    owner, cannot be rewritten: a revision that changes its brief is refused as
    a whole and the previous revision stays active, so accepted work remains a
    fact; replacing it means adding a new lane.
12. **Plan completion is acceptance, not activity.** A plan is complete only
    when every lane is accepted, explicitly marked done, or explicitly removed.
    A failed lane keeps the plan blocked until the owner replaces or removes it.
13. **The cost of a parallel wave stays visible.** Acceptance is still per
    lane. Landing one sibling moves the target, so other candidate-ready
    siblings become stale and need a refresh and a fresh accept. The progress
    projection must state that plainly rather than imply one-click landing.

## 3. Consequences

- A worker receives the owner's actual lane brief instead of a reconstructed
  task id, and an independent reviewer can read Why and Out of scope.
- An invalid plan (cycle, unknown dependency, missing Done when, duplicate id,
  malformed machine block) is refused as a whole; the previously active
  revision stays active.
- The stored format is migration-sensitive in exactly one place: the hash. The
  format-1 compatibility test is the guard, and a tampered revision still
  fails closed in both formats.
- `STATE.md` is a convenience mirror, not authority. `orch plan-file status`
  and the MCP read path, built in later lanes, must return the same projection
  when the file is missing or the scheduler is stopped.
- The plan file is tracked, so editing and activating it moves HEAD. Batch plan
  edits at wave boundaries; the dirty-tree refusal makes the consequence
  explicit instead of surprising.
- No automatic retry of a failed lane is introduced. Attempt retries remain the
  engine's, and replacing a failed lane is a deliberate new revision.
