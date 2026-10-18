# Project entry modes (Step 026 / PM-1)

How SaberOps decides what "start" means for a repository: the six owner-facing
entry modes, the repository facts that narrow them in code, the preflight that
lists every blocker with the command that clears it, the owner-confirmation
boundary, and the repository `ROADMAP.md` export generated from the plan store.

It implements the Step 026 outcome of roadmap v4 (local orchestration document
`2026-10-18-v4-modes-autonomy-waves.md`, Step 026 / PM-1) and consumes, without
changing, Step 015 (plan store and project registry), Step 016 (Orchestrator
turns), Step 021 (gate protection) and Step 022 (the startup digest).

The module is `saberops.project_mode`. It performs no I/O beyond observing the
repository and the existing owner-state files, creates nothing on its own,
executes no gate, launches no worker and calls no model.

## The six modes

| Mode | Entered from | Writes | Summary |
|---|---|---|---|
| `new_project` | A directory with no commits | git history, gate, `ROADMAP.md` | `git init`, initial commit, initial gate, owner-approved `ROADMAP.md` committed in the repository |
| `develop_existing` | An existing repository SaberOps has no active plan for | SaberOps state | Gate check plus a durable project map kept in SaberOps state per commit |
| `continue_saberops` | A registered project with an active plan revision | run/plan state | Loads the OM-1 startup digest, recent runs, open backlog and next roadmap item |
| `task` | Any committed repository | one candidate | One task with a gate, or the existing owner-only gate-disabled setting applied to that Task |
| `resume_run` | A repository with a paused/interrupted run and a clean tree | run state | Resume from the exact saved checkpoint |
| `inspect` | Anything, including a missing path | nothing | Read-only status and health report: zero candidates, no gates |

`inspect` is always available. It is never the *recommended* default: a
recommendation that can name an actionable mode must name it.

## Repository and owner-state facts

`extract_repository_facts(path)` observes, and never infers:

| Fact | Meaning |
|---|---|
| `git_repository` | `canonical_repo_root(path)` resolves to a working tree |
| `commits_present` | `git rev-parse HEAD` succeeds |
| `gate_scripts_present` | `scripts/gate-full.sh`, `scripts/gate-affected.sh`, `scripts/lane-check.sh`, or a `Makefile` with a `gate:` target exists |
| `orch_state_present` | a `.orch/` directory exists in the repository |
| `working_tree_dirty` | `git status --porcelain` is non-empty |
| `git_hooks_present` | at least one non-`.sample` file exists under the git hooks directory |
| `roadmap_present` | `ROADMAP.md` exists at the repository root |

`extract_saberops_state_facts(...)` adds the owner state that separates "a
repository SaberOps develops" from "a repository it can merely see":

| Fact | Meaning |
|---|---|
| `registered_in_saberops` | a registry entry's `repository_root` canonicalises to this repository |
| `active_plan_present` | the plan store holds an active revision for a matching project |
| `resumable_run_present` | the caller supplied at least one paused/interrupted run id |

The resumable-run inventory belongs to Step 011 (WX-2). It is a caller input
here; when the caller cannot supply it, the fact is false and `resume_run` is
narrowed out rather than guessed in.

## Qualification rules

A mode is allowed only when **all** its required facts are true and **none** of
its forbidden facts are true. This is the only place allowed modes are decided.

| Mode | Requires | Forbidden |
|---|---|---|
| `inspect` | — | — |
| `new_project` | — | `commits_present` |
| `task` | `git_repository`, `commits_present` | — |
| `resume_run` | `git_repository`, `commits_present`, `resumable_run_present` | `working_tree_dirty` |
| `develop_existing` | `git_repository`, `commits_present`, `gate_scripts_present` | `active_plan_present` |
| `continue_saberops` | `git_repository`, `commits_present`, `registered_in_saberops`, `active_plan_present` | — |

Consequences worth stating explicitly:

* a directory with no commits can only be entered as `new_project` (or
  `inspect`), because every other writing mode needs existing history;
* a repository with commit history cannot be "created" again;
* `develop_existing` needs a gate entry point — an existing repository without
  one is a `task` repository until the owner adds a gate;
* `continue_saberops` is not a synonym for "registered": without an active
  plan revision there is nothing to continue, and the workflow uses
  `develop_existing` with a targeted owner question about the missing roadmap.

## Recommendation and fail-closed validation

`recommend_mode(facts)` is the deterministic advisory: the most *specific*
allowed mode wins (the one whose qualification uses the most facts), with ties
broken toward the lighter mode and confidence reduced to `0.5`.

Modes are ranked by authority footprint, lightest first:

```text
inspect < task < resume_run < develop_existing < continue_saberops < new_project
```

`validate_recommendation(recommendation, facts)` is the authority. The
Orchestrator may pick any *allowed* mode, but its typed `ModeRecommendation`
must survive all of these refusals:

* the mode is not allowed by the observed facts;
* the evidence cites a fact the mode does not require (irrelevant/invented), a
  fact twice, or no fact at all for a mode that has requirements;
* the evidence contradicts the observation (`expected` differs from the
  observed value);
* the confidence is not a float in `[0, 1]`, or exceeds the fraction of the
  mode's required facts the evidence actually covers;
* the runner-up is the mode itself or is not allowed.

A genuine tie — equal specificity, and the evidence cites no fact that
distinguishes the requested mode from the tied lighter one — resolves to the
lighter mode instead of failing. The returned `ModeChoice` records
`requested_mode`, effective `mode`, `tie_broken` and the runner-up, so the
tie is visible in evidence rather than silent.

## Preflight

`preflight(mode, facts, gate_required=True)` returns `blockers` (each with a
machine code, a summary and an exact shell command) plus non-blocking
`warnings`. `inspect` is never blocked.

| Blocker | Raised when | Fix command |
|---|---|---|
| `not_a_git_repository` | the path is not a repository | `git init` |
| `no_commits` | no commit exists | `git add -A && git commit -m "chore: initial commit"` |
| `no_gate_scripts` | no gate entry point exists | creates `scripts/gate-full.sh` and makes it executable |
| `missing_roadmap` | `ROADMAP.md` is absent (`new_project`) | writes and commits `ROADMAP.md` |
| `repository_has_commits` | existing history blocks `new_project` | start a `task` instead |
| `not_registered` | no registry entry matches (`continue_saberops`) | `orch project register …` |
| `no_active_plan` | no active plan revision (`continue_saberops`) | plan-store bootstrap one-liner |
| `no_resumable_run` | no paused/interrupted run (`resume_run`) | `orch list --repo …` |
| `dirty_working_tree` | the tree is dirty (`resume_run`) | `git stash push --include-untracked …` |
| `active_plan_exists` | an active plan blocks `develop_existing` | `orch plan-store show …` |

`new_project` is special: entering it *creates* the facts it needs, so its
preflight lists `git init`, the initial commit, the gate entry point and the
committed roadmap in the order they must happen. Placeholders such as
`<project-id>` are values only the owner can choose; every other part of the
command is exact.

## Owner confirmation and the gate-disabled Task

`check_activation(activation, facts)` applies authority first and environment
second:

* every roadmap- or state-writing mode (`new_project`, `develop_existing`,
  `continue_saberops`, `task`, `resume_run`) requires
  `confirmed_by_owner=True`; a missing confirmation raises
  `ModeConfirmationError` and is never downgraded to a warning;
* a gate-disabled Task is owner-only: `selected_by=orchestrator` with
  `gate_policy=owner_disabled` raises `ModeAuthorityError`, and so does a
  gate-disabled non-Task;
* a gate-disabled Task suppresses only the gate preflight blocker, returns the
  `GATE_DISABLED_WARNING` in `warnings` and in `accept_checklist`, and is never
  pushed. The accept authority and the existing
  `project_settings.gate_enabled` document are untouched; this module only
  models the per-Task choice;
* `inspect` needs no confirmation and always passes.

## The per-project mode record

`saberops.project_mode` owns one small persisted document,
`project-mode.json` (schema version 1), in the project's state directory:

* `save_mode_record` / `load_mode_record` round-trip the record atomically and
  fail closed on corrupt or unsupported content;
* a state-writing mode cannot be recorded without owner confirmation;
* `reclassify_mode(state_dir, …)` is the accept-time path: it requires
  explicit owner confirmation, requires an existing record, keeps the recorded
  gate policy unless a new one is passed, and increments `revision` with
  `reclassified_by_owner=True`.

Recording the mode never starts anything. It is evidence of the owner's entry
decision, not a launch authorization.

## `ROADMAP.md` export

The repository roadmap is an export, never a second source of truth:

```python
from saberops.project_mode import export_roadmap_markdown, write_roadmap_export
from saberops.project_scope.plan_store import PlanStore

store = PlanStore()
markdown = export_roadmap_markdown(store, "my-project")  # active revision
write_roadmap_export("/path/to/repository", store.load_active("my-project"))
```

Rendering properties:

* the only timestamp is the revision's own `created_at`; the current clock is
  never read, so two renders of one revision are byte-identical;
* the revision's content hash is printed (first 12 hex characters), which makes
  a stale export visible;
* phases appear in first-appearance order, tasks keep their stored order, pipe
  characters are escaped, and dependency-free tasks are marked `—`;
* `roadmap_export_matches(destination, revision)` re-renders and compares
  byte-for-byte, so "the export matches the plan store" is a check, not a
  claim;
* `next_roadmap_item(revision)` is a display order hint for Continue mode
  (dependency-free task first, then smallest `(phase, task_id)`). Readiness
  remains `saberops.ready_frontier.project_ready_frontier`; the hint grants no
  launch authority.

## Continue summary and the startup digest

Step 022 (OM-1) owns the Orchestrator startup digest. This module declares the
structural subset it consumes, so Continue mode is implementable and testable
before OM-1 lands:

```python
StartupDigest(recent_runs=(RecentRun(run_id, status, accepted),), open_backlog=(OpenBacklogItem(title, detail),))
build_continue_summary(facts, revision, digest)   # requires Continue-qualified facts
```

`ContinueSummary.render()` prints the project, the active plan revision, the
next roadmap item, the recent runs and the open backlog. Building it refuses
facts that do not qualify `continue_saberops` and repositories claimed by more
than one registered project — the ambiguous case is a question for the owner,
not a silent pick.

## Thin borders: the targeted question

Registration and the active plan revision are separate facts, so a registered
project can exist without an active plan (for example, a repository registered
during an earlier experiment). In that state `continue_saberops` is not
allowed and `develop_existing` applies. The owner must answer one question:
create the first plan revision and its roadmap export, or develop the existing
repository from scratch. The mode record captures whichever answer the owner
confirms.

## Authority boundaries

* No mode recommendation is a launch decision. `check_activation` reports
  environment blockers; launching, gating, reviewing and accepting remain with
  their existing authorities.
* Gate protection (Step 021) is untouched. A gate-disabled Task can only be an
  explicit owner choice, it is never pushed, and it is never a quiet default.
* The plan store remains the only plan authority; the export is derived.
* The registry and project identity from `saberops.project` are consumed
  read-only.
* Nothing here writes to `main`, pushes a remote, or mutates another project's
  state.

## Verification

```bash
.venv/bin/python -m pytest tests/test_project_mode.py -q
.venv/bin/ruff check .
.venv/bin/mypy --strict src tests
```

The test module is hermetic: each case builds its own temporary repository,
plan store, registry and owner-state root, runs only the local `git`
executable, and asserts the mode transition, the refusal or the rendered
export. Every test carries a `Covers:` docstring line.
