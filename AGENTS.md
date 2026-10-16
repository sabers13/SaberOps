# SaberOps repository instructions

The product, authority, security, and engineering rules in [README.md](README.md),
[pyproject.toml](pyproject.toml), and [CI](.github/workflows/ci.yml) remain authoritative.
LLMs advise; deterministic software decides. Workflow adoption does not change
execution authority, provenance, isolation, acceptance, or release requirements.

## Orchestrator benefit-first policy (repository-tracked)

This entry applies to any **Orchestrator** session (planning, reviewing
evidence, routing work) with access to this repository. It is an
Orchestrator-specific instruction; it does not change the frontend, security, or
engineering rules below, and it never overwrites safety instructions. It does
not authorize an autonomous engine: LLMs advise; deterministic software decides.

1. Before drafting any plan, read the tracked policy
   [docs/orchestrator-parallel-first.md](docs/orchestrator-parallel-first.md),
   then [docs/parallel-work.md](docs/parallel-work.md). Optimize expected
   elapsed time to **owner-accepted integrated output**, not concurrent Worker
   count: account for critical path, setup/context, coordination, review/rework,
   G1/G3, and merge overhead before declaring parallelism worthwhile.
2. Plan the **fewest coherent tasks** reasonably needed — each one meaningful,
   independently finishable deliverable with clear acceptance criteria. Compare a
   simple/sequential plan against any parallel split; parallelize only when
   conservative expected net time saved outweighs overhead and risk. A ready task
   is **eligible**, never automatically obligated to dispatch.
3. **ONE implementation Worker owns each task**, including its fixes. Review
   findings return to that same Worker/session/branch. A failed Worker is
   replaced by one fenced single-owner handoff, never duplicated in parallel.
   Material task split/merge needs an owner-approved plan revision.
4. **Waves are optional**: zero, one, or several tasks are all legitimate.
   Sequential, one-task, and zero-wave plans are valid when that is the
   credible fastest/safest path. Never invent work to fill free slots.
5. Honor **3 active projects x 5 executing child tasks as owner-adjustable
   upper-bound limits** (not a quota or occupancy target). The runtime may run
   1 of 15 allowed tasks. Eligibility never equals obligation to dispatch.
6. Worker implementation candidates receive G0/G1 plus one task-scoped
   independent review. **Pure Integrator merges, bridge-test-only commits, and
   post-G3 looks get no automatic semantic review**; material production
   glue/behavior changes found at integration go back to an accountable Worker
   for G1 and task-scoped review. The exclusive integration gate (G3) and owner
   acceptance stay binding. Do not globally weaken Worker risk-adaptive review.

This entry defers to owner-authorized policies and to deterministic software
decisions. It grants the Orchestrator no authority to implement source, dispatch
workers, merge, push, or move the target branch, and reading it does **not**
activate a scheduler — see "Current capability versus target" in the policy
document.

## Frontend workflow

### Precedence and skills

1. Explicit user instructions for the current task.
2. Existing repository safety, architecture, security, engineering, CI, and release rules.
3. [src/saberops/DESIGN.md](src/saberops/DESIGN.md) and the existing design values in
   [console-v2.css](src/saberops/static/css/console-v2.css).
4. This frontend workflow section.
5. The globally installed `frontend-ui-workflow` skill for detailed guidance.

Use the global `frontend-ui-workflow` skill for frontend work and workflow adoption.
It is discoverable at `~/.agents/skills/frontend-ui-workflow/SKILL.md`; the local
canonical kit is `/home/saber/projects/frontend-workflow-kit` and its adoption
instructions are in `ADOPT.md`. Do not vendor these skills into the repository.
Apply Apple Human Interface Guidelines principles to every frontend change using
[UI design principles](docs/ui-design-principles.md) and DESIGN.md. They are part
of the implementation contract, including routine UI BUILD and UI TWEAK work.
Use the global `apple-design` skill for explicit requests and relevant UI INIT /
UI REVIEW work; routine changes can use the documented principles without loading
the entire skill. Read the relevant reference before attributing a rule to Apple.
For this web application, translate HIG into hierarchy, accessibility, familiar
controls, adaptability and clear feedback using the existing tokens and HTML
semantics. Platform-specific native chrome is not a requirement.

### Current frontend boundary

This is an existing FastAPI/Jinja application, not a React application.
Templates are in `src/saberops/templates/`, application styles in
`src/saberops/static/css/`, and application JavaScript in `src/saberops/static/js/`.
`web.py` constructs the server-side contexts and mounts `/static`.
Keep existing template variables, conditions, form fields/actions, IDs, `data-*`
hooks, and request contracts intact during visual iteration.

`console-v2.css` is the verbatim approved design extraction: its header says not
to hand-edit it. Reuse its custom properties and put integration style additions
in `saberops-console.css`. Do not replace tokens or introduce a second theme
system during adoption. Token consolidation or missing token categories belong
in a separately scoped UI INIT; changing shared theme architecture requires the
normal engineering workflow. Preserve vendored xterm assets and their notices.

### Five modes

If no mode is named, select the lightest applicable mode and state it. Workflow
adoption itself does not authorize UI INIT, visual edits, or a frontend migration.

| Mode | Scope | Validation |
| --- | --- | --- |
| UI INIT | Establish or materially change the visual language. Read the global intake/design references; resolve project choices in DESIGN.md before implementing an accepted reference screen. | UI fast gate for visual-only work; escalate other changes. |
| UI BUILD | Build a substantial screen/component in the existing language, within the existing frontend boundary. Read DESIGN.md, tokens, and related templates/assets. | UI fast gate and related rendering checks; escalate new contracts. |
| UI TWEAK | A bounded visual iteration: spacing, alignment, sizing, typography, density, borders, colour, or rearrangement of existing presentation. | Rendered result, UI fast gate, affected-page checks, then STOP. |
| UI REVIEW | Review a coherent screen for hierarchy, consistency, accessibility, responsiveness, states/feedback, and visual coherence. Return bounded findings with severity, location, and suggested fixes. | No automatic gate or redesign. Fix accepted findings through TWEAK/BUILD. |
| UI FINALIZE | Accepted visual work explicitly entering finalization, or requested merge/release preparation. | Complete the authoritative full gate and affected integration/release checks. |

### UI TWEAK eligibility and escalation

Before editing, identify the intended change batch and inspect the existing diff.
Keep unrelated local changes separate; a passing UI fast gate does not certify a
mixed backend/frontend diff for merge. Do not overwrite or fix unrelated work.

Eligible tweaks change presentation in templates or non-vendored CSS while
preserving rendered data, semantics, and behavior. JavaScript is eligible only
for a narrowly scoped presentation adjustment with existing event/data contracts
unchanged; syntax success alone cannot establish that eligibility.

Escalate out of UI TWEAK to the normal SaberOps engineering workflow if a change
affects any of the following, even when it is located in a template/static file:

- Python or backend behavior, including `web.py`.
- Template variables, server-side data contracts, expressions/conditions that
  determine capabilities or state, form names/actions, or request/response data.
- API behavior, authentication/security, escaping or secret handling.
- Persistence, business logic, orchestration/state machines, or data semantics.
- Shared architecture, dependencies, CI, packaging, or release behavior.

Explain the boundary being crossed, expand context only as far as needed, and
use the required affected checks. Do not reinterpret a contract change as styling.

The routine loop is:

`rendered UI → narrow template/asset context → batched edit → inspect result → fast UI gate → STOP`

Read DESIGN.md, the target template/style, direct parents/includes, and relevant
tokens/hooks only. Do not broadly analyze orchestration internals. Refresh the
browser after edits; the current CLI has no hot-reload option. Do not automatically
run the full suite, perform architecture/release review, claim merge readiness,
commit, or switch to FINALIZE after a tweak batch.

### Gate commands

Run from the repository root:

```bash
bash scripts/gate-ui-fast.sh
```

Both wrappers use `.venv/bin/python` when present, otherwise `python` from the
active development environment. Direct interpreter invocation also works when
a moved checkout has stale virtual-environment activation paths or tool shebangs.
The fast wrapper uses installed Jinja2/pytest and the available Node.js executable;
it installs nothing. It compiles all current templates without importing the
backend, runs `node --check` on the three application JavaScript files, and runs
only these existing test node IDs from `tests/test_c16c2_console.py`:

- `test_c16c2_dashboard_renders_approved_shell`: renders the real dashboard and
  checks shell hooks, CSS/JS links, navigation, and absence of prototype controls.
- `test_c16c2_run_detail_renders_inspector_and_actions`: renders seeded running and
  completed runs; checks tabs and action links without executing those actions.
- `test_c16c2_missing_run_keeps_shell`: verifies the 404 page keeps the shared shell.
- `test_c16c2_console_assets_are_served`: verifies all application CSS/JS assets
  return HTTP 200 with nonempty content.

These are small in-process GET/render checks using existing temporary test state,
not whole backend suites. The wrapper additionally isolates inherited state/config/
cache paths and store overrides in a disposable directory. No provider/model calls,
new fixtures, live service, or browser automation installation are required.

The baseline is credible only for the eligibility rules above **plus inspection
of the affected rendered page**. For templates outside dashboard/run-detail, add
the smallest existing affected GET/render checks by exact node ID, e.g.:

```bash
bash scripts/gate-ui-fast.sh \
  tests/test_c16c2_console.py::test_c16c2_owner_pages_keep_forms
```

That existing test seeds routing test state and GET-renders access, routing, and
orchestrator forms. `test_c16c2_settings_modal_marks_unavailable_sections` can be
added for settings changes. Inspect any other candidate test's actual assertions
and setup before selecting it; filenames containing "UI" are not selection criteria.
Do not append a whole test file/suite. If no suitable related render test exists,
record that limitation and inspect the real affected page; contract changes escalate.

There is no configured CSS/HTML linter, frontend typechecker, or frontend build.
Do not invent those checks. Jinja compilation does not validate runtime data
contracts or HTML structure; Node syntax checks do not prove interaction behavior;
asset serving does not validate CSS syntax, accessibility, or layout. Inspect the
rendered result, including relevant narrow/wide widths, focus and interactive states,
and both existing themes for shared style changes. Report unavailable browser
inspection as unverified, never as visual acceptance.

For engineering (non-presentation) work there is a per-task gate that runs only the
tests the diff affects:

```bash
bash scripts/gate-affected.sh [--base REF] [--strategy {auto,coverage,static}]
```

`scripts/select_tests.py` diffs against the base ref (default: merge-base with the
branch upstream, else `HEAD~1`) plus staged, unstaged and untracked files, then
selects the tests that executed the changed lines. That reachability comes from a
coverage map built once per commit (`python scripts/build_test_map.py`, full suite
under coverage.py with `dynamic_context = test_function`, stored as sharded JSON
under `$(git rev-parse --git-common-dir)/saberops-test-map/`, shared by all
worktrees and never committed). Added/deleted lines select the tests of the nearest
covered line in the same function (`ast` spans); changed module-level lines
(class bodies, field defaults, constants, decorators) resolve through their
top-level symbol (`ast` Name/Attribute usages in `src/`/`tests/`, string
references such as getattr names not tracked; deleted symbols resolve against
the base tree) and fall back to direct importers only when the symbol cannot be
resolved. A changed file that is new, has no
covering context, or whose map predates the change (build commit not an ancestor of
`HEAD`) falls back to direct importers only (depth 1) from the static `ast` graph
over `src/**` and `tests/**` (imports plus dotted patch targets written as strings;
`if TYPE_CHECKING:` imports are not edges because they never execute). With no map
at all, or `--strategy static`, the legacy transitive closure applies. Selected
node IDs are resolved against the current tree (`pytest --collect-only`):
map node IDs of deleted/renamed tests are dropped as ghosts, and the gate fails
instead of passing when the selection was non-empty but nothing runs (also on
pytest exit 5). The wrapper
prints the map commit/age (in commits) plus the share of changed lines without
coverage data ("map stale for this diff, consider rebuild" past 50%), the base ref, the changed files and the reason for each
selected test first, so saved gate output is self-auditing. It keeps
`ruff check .` and `mypy --strict src tests` over the whole repository (mypy is
incremental and type changes ripple; its cache is shared across worktrees under
`$(git rev-parse --git-common-dir)/saberops-mypy-cache/`, override with
`MYPY_CACHE_DIR`) and runs pytest on the selection only, with
`-n auto` (pytest-xdist) when more than ~50 tests are selected. A change
that cannot be reasoned about safely (any conftest, `pyproject.toml` and other setup
files, gate/table scripts, CI workflows, the top-level package init, a `.py` outside the
scanned roots, any unknown file type) selects the FULL suite and the wrapper runs
`pytest -q`; a docs-only or `.md`/`.orch` diff selects no test and pytest is skipped.

The per-task loop is:

`change → affected gate → inspect → repeat`

`bash scripts/gate-full.sh` stays the authoritative milestone/merge gate: ruff, mypy
and the complete pytest suite with nothing filtered out. Using the affected gate does
not certify a change for merge, does not replace the full gate, and does not relax
the UI FINALIZE rule that the full wrapper is what finalization requires. Both
wrappers use `.venv/bin/python` when present, otherwise `python` from the active
development environment, and neither installs anything.

For UI FINALIZE:

```bash
bash scripts/gate-full.sh
```

The full wrapper preserves the README/CI sequence exactly in scope:
`ruff check .`, `mypy --strict src tests`, `pytest -q` (via the selected Python
interpreter's `-m`). No required test is filtered out. It does not invoke the
fast wrapper. Complete any additional affected integration tests and existing
release requirements; never substitute the fast gate for final assurance.
Do not add a React build/E2E/certification step that this repository does not require.

The finalization loop is:

`accepted UI → complete existing required validation → affected integration checks → full quality gates → merge/release readiness`

### Shared browser workflow

Dev server, using the existing CLI:

```bash
PYTHONPATH=src .venv/bin/python -m saberops.cli ui --host 127.0.0.1 --port 8765 --profile ui-workflow
```

This calls the existing `orch ui` CLI; an installed/active environment may use
`orch ui` directly. Keep `PYTHONPATH=src` for checkout development so an installed
package cannot shadow the edited source/templates. Open `http://127.0.0.1:8765`.
The named profile isolates owner setup/state; use an
already configured dogfood profile when that is needed to inspect real data.
Do not start model runs or mutate owner policy merely to inspect a visual tweak.
Refresh after template/static edits; restart the server only when required.

- **Primary:** ChatGPT/Codex shared local browser when available. Batch annotations,
  edit, refresh, inspect, run the fast gate, and stop.
- **Fallback:** Shared browser plus batched textual UI TWEAK instructions when
  annotation handoff is unavailable.
- **Backup/model-independent:** OpenCode with globally configured Playwright MCP.
  Browser tools belong in user configuration, not this repository.

Use structured browser snapshots for structure and screenshots for visual judgment.
Global MCP availability does not justify project-level Playwright dependencies,
tests, screenshots/baselines, or MCP configuration. Do not install Storybook, mock
systems, fixtures, design SaaS, or shared component packages during adoption.

### Deferred migration and handoff

The future direction is frontend → typed API client → JSON API → backend services,
with boundaries planned before an incremental React migration. Record current
coupling and unresolved contracts in DESIGN.md. This is intent only: do not create
React/Vite code, endpoints, client contracts, mocks, or a `web.py` refactor now.

No HANDOFF.md is needed for the bounded adoption. Create a short frontend handoff
when actual multi-session UI INIT/migration work begins and keep it current then.
