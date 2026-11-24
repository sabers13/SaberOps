# ADR-0088: The scope guessers are gone; the planner names the files

- **Status:** Accepted. Implemented in phase 1.1 lane H15.
- **Date:** 2026-11-24
- **Decider:** Saber (owner), phase 1.1 owner answer Q11 ("Delete both scope
  guessers") and the "maximise the deletion" answer (2026-11-22)
- **Related:** [ADR-0005](0005-mcp-first-engine-and-phase-2-board.md) (the
  planning CLI is the orchestrator), [ADR-0083](0083-splitting-is-the-planners-job.md)
  (splitting is the planner's job), [ADR-0085](0085-one-observability-projection.md)
  (the run report keeps stored scope evidence as a read-only field)

## 1. Context

Two deterministic guessers turned a task's words into a file reading list:

- `src/saberops/devscope/` (2,354 lines) resolved a query to logical modules
  in shadow mode. Nothing outside `project_scope/` imported it.
- `src/saberops/project_scope/` (minus `plan_store.py`) (2,871 lines) built a
  project graph from `.orch/` metadata or a conservative Python AST analyzer,
  resolved the task text to one bounded scope, froze it per run, expanded it
  through `orch scope expand`, and appended a `[PROJECT SCOPE]` section to
  every worker prompt. `orch scope graph|show|expand|affected` exposed the same
  guess.

Both callers were on the run path: `supervisor.start_run` froze the scope
before launching, and `Orchestrator._run_core` reconstructed or re-analyzed it,
then rendered it into the initial and retry/repair prompts. `run_report` reads
the persisted rows as evidence. The phase 1.1 census and caller check found no
other consumer: no MCP tool, scheduler path, web route, script or benchmark-rig
caller, and no test imported either package.

The phase 1.1 rule is that the actor which understands the task is the right
one to judge which files it touches — the planning CLI. The engine's job is to
carry the goal, the acceptance the planner wrote, the paths the planner named,
project notes and the check contract to the worker, and to enforce checks,
review, quota and acceptance.

## 2. Decision

1. **`devscope/` is deleted with `orch scope`.** The package, its
   `__main__`, its CLI group and the parser/dispatch entries in `cli.py` are
   removed. `orch scope <verb>` is now an argparse `invalid choice`.
2. **The non-plan-store `project_scope` modules are deleted.** That is
   `python_analyzer`, `resolver`, `expansion`, `persistence`, `graph`,
   `metadata`, `contracts`, `context`, `affected` and `cli` — the whole guess
   pipeline, including the `[PROJECT SCOPE]` renderer. `project_scope.plan_store`
   stays unchanged for PLAN.md's immutable revisions, and the package
   `__init__` is a plain docstring.
3. **The run path no longer freezes or reconstructs scope.**
   `supervisor.start_run` and `Orchestrator._run_core` lose the graph build,
   scope resolution, freeze, `project_scope_frozen`/`project_scope_failed`
   events and `_fail_run_for_project_scope_error`. `load_scope_bundle`,
   `resolve_scope`, `freeze_run_scope` and `scope_section_for_run` leave with
   the modules. A repository that moves during a run is still caught by the
   existing provenance, candidate and gate checks; scope analysis was never a
   safety check.
4. **`render_projection` loses `project_scope_text`.** The worker prompt keeps
   the goal, the acceptance the job carries, the paths the planner named in the
   task, `[PROJECT NOTES]`, the run verification contract and the validation
   policy. No `[PROJECT SCOPE]` section is emitted.
5. **Stored scope evidence stays readable.** The `run_project_scope` and
   `scope_expansions` tables and the `get_run_project_scope` /
   `list_scope_expansions` readers stay, so a pre-1.1 run keeps its
   `run_report` evidence field (ADR-0085); the writers
   (`set_run_project_scope`, `update_run_current_scope`,
   `add_scope_expansion`) are deleted because nothing can call them.
6. **Out of scope and untouched:** `gate_scope.py` (H32 wires the real check
   protection), the affected-test selection the real check uses, PLAN.md and
   `plan_store.py`. No safety property is removed: scope was advisory prompt
   material, never an enforcement point.

## 3. Consequences

- Choosing a file list is now the planner's call. A planner that names paths in
  the task or `PLAN.md` lane gets them into the worker prompt through the
  existing prompt body; a planner that omits them produces an ordinary prompt
  instead of a wrong guess.
- `orch scope` and `python -m saberops.devscope` stop existing without a
  compatibility alias: both were read-only developer surfaces, not saved owner
  settings, so the expand/migrate/contract rule for stores does not apply.
- Measured reduction (`wc -l`): source 139,645 → 133,995 (−5,650); tests
  98,651 → 98,780 (+129, two new guards and the H15 behavioural test; no
  existing test tested the guessers). The lane diff is +92 / −5,669 over 30
  files. Later `main` commits (H12) add their own lines; they are not part of
  this measurement.
- Fake-mode benchmark time to accepted (the phase 1 rig, same host):
  median 100.9 s → 90.7 s, p95 135.7 s → 114.8 s, total 139.3 s → 119.9 s,
  PASS in both runs. Real CLIs stay H14's job.
- Left for the later dead-code pass (H31), not deleted here because they are
  outside this lane's authorized area: `git.assert_repository_at_revision`
  (its only callers were the two scope freeze blocks) and the historical
  `devscope` mention in `workers/instruction_compat.py`'s reproduction
  evidence. The `run_project_scope`/`scope_expansions` tables stay as upgrade
  and report evidence.

## 4. References

- Phase 1.1 plan, lane H15 (`phase-1.1-core-plan.md`), owner answers Q11 and
  the 2026-11-22 answer
- `src/saberops/supervisor.py`, `src/saberops/orchestrator.py`
  (`_run_core`), `src/saberops/context_projection.py` (`render_projection`)
- `src/saberops/cli.py` (`orch scope` parser and dispatch)
- `src/saberops/db.py` (stored scope readers)
- `tests/test_h15_scope_guessers_removed.py`,
  `tests/test_readiness_routing.py::test_h15_worker_prompt_has_no_project_scope_section`
