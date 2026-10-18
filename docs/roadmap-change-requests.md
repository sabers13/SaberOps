# Roadmap change requests (Step 025 / RC-1)

`ROADMAP/CHANGE-WORKFLOW.md` describes how an idea becomes a plan change without
disturbing running work, and it is the manual contract this step implements. The
manual procedure is carried out by hand: the owner captures an idea with
`orch idea`, the Orchestrator triages and proposes, the owner decides, and the
apply step edits `ROADMAP/steps.json` under `ROADMAP/.revision.lock` with
`orch roadmap check --prev` as the validator.

`saberops.roadmap_changes` is the deterministic part of that procedure: idea
intake, triage routes A-E, the frozen-step guard, in-flight collision detection,
the renumber calculator, cost and makespan impact, and the conversion of an
owner-approved proposal into one immutable versioned revision in PL-1's plan
store. It is pure, except for three explicit file operations (write the idea
file, write the plan-store revision, create/remove the revision lock). No model,
provider, network or scheduler call; the only clock reads are the capture and
apply defaults, both injectable.

## Stage mapping

| CHANGE-WORKFLOW stage | Software |
|---|---|
| 0 Capture | `RoadmapIdea`, `capture_idea`, `write_idea`, `read_idea`, `idea_path`, `load_ideas` |
| 1 Triage (routes A-E) | `TriageRoute`, `TriageContext`, `RoadmapIdea.route`, `triage_idea`, `triage_batch`, `TriageDecision` |
| 2 Impact check | `impact_check`, `ImpactReport`, `check_plan_change`, `detect_collisions`, `renumber_preview`, `CostImpact`, `MakespanImpact` |
| 3 Placement | `choose_placement`, `evaluate_placement`, `PlacementDecision`, `insert_step`, `expand_step`, `remove_step` |
| 4 Proposal | `build_proposal`, `RevisionProposal.as_markdown` |
| 5 Decision | `RoadmapIdea.decision` / `RevisionProposal.owner_decision` + `approved` |
| 6 Apply | `apply_revision`, `RevisionLock`, `default_revision_lock_path`, `to_plan_task_defs` |
| 7 Activate | out of scope here: the revision is a plan, and the normal pre-run scan decides when to launch |
| Validator parity | `check_plan_change`, `assert_frozen_guard`, `RenumberPreview.summary_lines` |

Manual counterparts kept as the single source of the procedure:
`bin/orch idea --slug <s> "<words>"`, `bin/orch roadmap check --prev <steps.json>`,
`bin/orch roadmap table --prev <steps.json>`.

## Guarantees encoded

1. **Frozen steps keep their scope.** `check_plan_change` refuses a changed
   `title`, `phase`, `outcome`, `exit`, `needs`, `integration_needs` or
   `optional`, a sealed step returning to `open`, an unjustified sealed
   renumber, and any removal of a `frozen`/`done` step. An `open` step may only
   be removed with a reason in `removed`. The new plan must also validate, so a
   dependency on a removed step fails.
2. **Running lanes are untouched.** `running_lane_conflicts` refuses a revision
   that changes or removes a step with an in-flight lane, `detect_collisions`
   reports file-scope overlap with a lane's `Touches`, and `apply_revision`
   re-verifies both before writing. Nothing in this module edits a lane prompt,
   worktree, branch or gate.
3. **A revision launches nothing.** `build_proposal` and `apply_revision`
   produce a plan version only.
4. **One revision at a time.** `RevisionLock` creates
   `ROADMAP/.revision.lock` exclusively (`O_CREAT|O_EXCL`), refuses a second
   revision while it exists, and always releases on exit.
5. **Owner words survive.** `RoadmapIdea.words` is stored verbatim; the proposal
   quotes it with `json.dumps`.
6. **Only an owner decision authorises a revision.**
   `apply_revision` raises `RevisionNotApprovedError` without
   `approved=True` and a recorded `owner_decision`.

## Idea intake

```python
from saberops.roadmap_changes import capture_idea, read_idea

idea = capture_idea(ideas_dir, slug="add telemetry digest", words="owner words, verbatim")
read_idea(ideas_dir / f"{idea.idea_id}.md").words == idea.words  # True
```

The file is `ROADMAP/ideas/I-<YYYYmmdd-HHMMSS>-<slug>.md`, the same shape
`orch idea` writes: `status: captured`, `title`, `captured`, `by`, then the
`## Owner words (verbatim)` body and a `## Triage` section. Triage fields
(`route`, `target_step`, `covered_by`, `change_text`, `needs`, `touch_paths`,
`justification`, `until`, `reason`, `decision`, and a `new_step_*` block) are
written as single-line front matter by `to_markdown` and parsed back by
`from_markdown`, so a captured-and-triaged idea round-trips. Capture is
append-only: `write_idea` refuses to overwrite unless `overwrite=True` (used to
record triage).

## Triage routes A-E

| Route | Meaning | What triage requires |
|---|---|---|
| `A` | Expand an open step | `target_step` exists, is `open`, has no in-flight lane, and `change_text` (or a `needs` addition) says what it gains |
| `B` | New step that consumes a sealed step | `target_step` is `frozen`/`done` and is listed in the new step's `needs` |
| `C` | New independent step | a `NewStepSpec` with known dependencies |
| `D` | Process only | no plan change; no step is created |
| `E1` | Already covered | `covered_by` names an existing step |
| `E2` | Conflicts with in-flight work | a real overlap with a lane (or a lane on the target step) and a stated reason |
| `E3` | Reject | a reason |
| `E4` | Later | `until: <YYYY-MM-DD>` |

`triage_idea(plan, idea, TriageContext(lanes=...))` validates the idea's own
declared route against live state and returns a `TriageDecision`. It refuses a
contradictory route instead of reinterpreting it: expanding a sealed step (use
B), route B on an open step (use A), an unknown target or dependency, an
uncovered file-scope collision (declare that lane's step as an INTEGRATE
dependency, or hold the idea), or a placement that would move a sealed step
without an R1/R2/R3 justification. `triage_batch` triages a turn boundary's
ideas in one pass, threading step-creating decisions through a working plan so
one batch produces one numbering and at most one renumber.

## Impact check

`impact_check(prev, new, ...)` answers every CHANGE-WORKFLOW section 2 row; the
report is refused wholesale by `require_ok` when anything blocks it.

| Row | Source |
|---|---|
| Steps touched, and their state | `steps_touched` (content, not display number) |
| Frozen-step guard | `check_plan_change` violations and plan errors |
| Overlap with in-flight lanes | `detect_collisions` against lane `Touches`, minus INTEGRATE-resolved steps |
| Dependencies | added `needs` / `integration_needs` pairs |
| Conflicts with owner decisions / ADRs | caller-declared `adr_notes` |
| Renumber impact | `RenumberPreview.summary_lines()` (`shifted:` / `added:`) |
| Cost | `CostImpact`: new steps 1 Worker + 1 Reviewer + 1 Integrator + 1 gate; an expansion 1 Worker + 1 Reviewer + 1 gate; 0 owner decisions for "do nothing" |
| Makespan impact | `MakespanImpact`: WP-1 `optimize_plan` on the remaining (not `done`) plan, before and after |

`roadmap_graph` adapts the plan into WP-1's `TaskGraph` (dropping `done` steps
and external prerequisites, which are satisfied or not nodes) and
`roadmap_makespan` scores it. A renumber that moves only open steps therefore
reports a zero makespan delta and no task change.

## Placement and renumbering

`choose_placement(plan, phase=...)` prefers the end of the idea's natural phase
**when that shifts only open steps**; otherwise it falls back to directly before
the terminal certification gate, which is the position that keeps numbering of
evidence-holding steps stable. An explicit `after_step` is used as given.

`insert_step` renumbers contiguously and records
`renumbered[<stable id>] = "<old> -> <new>: <reason>"` for every sealed step that
had to move; without a reason that starts with `R1` (reading order), `R2` (phase
coherence) or `R3` (consolidation) it raises `FrozenStepError`. `RenumberPreview`
carries the minimal `shifted` / `added` / `removed` sets and both the
`(id, old, new)` mapping and `old_to_new()`. Display numbers are presentation
only: `steps.json` text and every dependency reference use stable IDs.

## Proposal and revision application

```python
proposal = build_proposal(plan, [idea], date="2026-10-19", lanes=lanes)
proposal.as_markdown()          # ROADMAP/proposals/_template.md shape
applied = apply_revision(
    PlanStore(root),
    replace(proposal, approved=True, owner_decision=owner_words),
    lock_path=roadmap_dir / ".revision.lock",
    created_at=stamp,
    activate=True,
)
applied.revision.version        # "v5", hash-verified by the plan store
```

`build_proposal` triages the batch, applies route A expansions and route B/C
insertions, bumps the version, records the proposal note, and refuses the result
wholesale if the impact check (including running-lane conflicts) is not clean. A
batch with no plan change (only routes D/E) raises instead of writing a no-op
revision. `apply_revision` converts the approved plan into
`PlanStore.save_revision(...)` tasks via `to_plan_task_defs` (needs -> hard START
dependencies, `integration_needs` -> deferred INTEGRATE edges, external
prerequisites dropped or mapped to `<external_project>/<need>`), under the
exclusive revision lock.

## Relation to the manual apply step

Stages 6-7 of `ROADMAP/CHANGE-WORKFLOW.md` still write the remaining manual
artifacts: `ROADMAP/versions/v<N>-<date>/`, the generated `draft.md` section 2
and 8 tables, phase README folders, `ROADMAP/CHANGELOG.md`, the
`state/DECISIONS.md` row and the ideas' `status: applied`. This module produces
the plan and the plan-store revision those artifacts are derived from; it does
not edit them, and it deliberately does not reimplement `orch roadmap table`.

Explicitly out of scope for this step: CLI or web wiring, MCP tool exposure
(`propose_config_change` belongs to M3), persisting size/`owns` metadata in
`steps.json`, and starting any new step (stage 7 is the normal pre-run scan).

## Testing

`tests/test_roadmap_changes.py` is hermetic and covers the plan model and its
validator, idea capture/round-trip with verbatim words, all eight triage routes,
batch numbering, collision detection and fail-closed scope handling, every
frozen-step refusal, justification requirements and the old-number mapping,
placement and renumber previews, every impact row plus cost and makespan, the
revision lock, owner-decision enforcement, immutable versioned apply, and the
"running lanes are untouched" invariant by byte-comparing a lane tree before and
after an apply. Every test carries a `Covers:` docstring tag.

The frozen-step guard was also cross-checked against the manual validator on the
live `ROADMAP/steps.json` (v4, 28 steps): the `shifted:` / `added:` lines and the
frozen/done refusals produced by `check_plan_change` are identical to
`bin/orch roadmap check --prev`, and a plan this module accepted (a justified
insertion at the position before `END`) passes that validator.
