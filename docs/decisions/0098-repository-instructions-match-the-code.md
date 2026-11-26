# ADR-0098: Repository instructions describe the code that exists

- **Status:** Accepted. Implemented in phase 1.1 lane H42.
- **Date:** 2026-11-26
- **Decider:** Saber (owner): the 2026-11-22 answer ("do your suggestion and
  maximise the deletion i feel like there are a lot of spam code is in repo,
  add all of this to the new phase 1.1 plan") and the 2026-11-23 answer
  ("update the plan, make the plan in style of phase 1 parallel favor"),
  applied to lane H42 ("correct only text that is now factually wrong ... Never
  add new guidance").
- **Related:** phase 1.1 plan lane H42 and "Rules for every lane"; phase 1
  "Rules for every lane"; [ADR-0060](0060-gate-protection-on-the-real-paths.md)
  (write scope informs the reviewer), [ADR-0063](0063-planner-recorded-difficulty.md)
  (difficulty replaces the keyword classifier), [ADR-0064](0064-delete-in-engine-orchestrator-model.md)
  (the orchestrator model and binding are gone), [ADR-0074](0074-one-away-module.md)
  (one away module), [ADR-0076](0076-trim-web-to-phase-2-pages.md) (the trimmed
  web surface), [ADR-0083](0083-splitting-is-the-planners-job.md) (team mode and
  in-run splitting are gone), [ADR-0093](0093-docstrings-and-comments-keep-contracts.md)
  (the docstring and comment diet).

## 1. Context

Phase 1.1 removed features, routes, roles and modules. H30 removed history from
docstrings and comments, but no lane owned the tracked Markdown instructions.
H42 is that lane: make the repository's own instructions match the code without
adding guidance and without rewriting decision records, which are history.

The lane is correction-only. It changes only text that is now factually wrong,
removes a section or paragraph that describes only deleted behaviour, and leaves
every historical record alone. No runtime behaviour changes.

**What phase 1.1 deleted.** The in-run team mode and its integration worktree
(`parallel_work.py`, `execution_mode`), in-run slicing (`slicing.py`), the
in-engine orchestrator model and binding, the pre-phase-1 certification engine
and `orch certify`, the two scope guessers (`devscope`, the non-plan-store
`project_scope` modules and `[PROJECT SCOPE]`), the keyword tier classifier, the
wave plan optimizer/admission, the orchestrator and routing web pages, and the
`.orch/WORKFLOW.md` working document.

**What still exists and must not be corrected away.** Verification before each
deletion found several look-alikes that are live:

- `.orch/` and `.saberops/` are still the MCP evidence roots
  (`saberops.mcp.evidence.EVIDENCE_ROOTS`), still the ignored runtime-state
  directory (`.gitignore`, `docs/**, *.md, .orch/**` in `scripts/select_tests.py`),
  and still where `scripts/main-gate.sh` writes the latest summary.
- Gate kinds `G1`, `G2`, `G3` and `main-gate` remain (`saberops.gate_history`);
  G3 is the full-profile integration gate.
- PLAN.md lanes still carry a `wave` presentation label; it was never a
  dependency edge (`saberops.plan_file`).
- Stored pre-1.1 runs and routing files still name `parallel_work.py` markers
  and the retired `INTEGRATOR`/`SURGEON`/`WORKER` roles as migration input, and
  say so in their notices.
- Release notes, fixture provenance and the decisions directory are records of
  what was true then, not current instructions.

## 2. Decision

1. **Remove only deleted behaviour from the operating docs.**
   `docs/parallel-work.md` loses the in-run team execution, integration-branch
   and team-failure sections; its current-versus-target section now says that
   the ready-frontier projection is on `main` and that no wave admission exists.
   Write scope is described as reviewer evidence (ADR-0060), not as an
   acceptance block.
2. **Correct false role and capability claims.**
   `AGENTS.md` no longer credits an `Integrator` role with merges: landing is a
   deterministic engine stage. `docs/operations.md` reads the run tier from the
   planner-recorded difficulty instead of a deleted classifier, and no longer
   cites a missing durable project identity as the reason project-aware
   scheduling is deferred. `docs/config.md` drops the deleted wave layer from
   the list of consumers that still read their own constants.
3. **Correct the web instructions.** `README.md` no longer says a Routing page
   chooses models; routing is `orch routing` and `orch reviewer select`, and
   landing follows the owner mode. `docs/mcp.md` no longer names a nonexistent
   `orch config approve` verb: `approve_config_proposal` has no MCP, CLI or web
   entry point today.
4. **Remove references to the deleted `WORKFLOW.md`.** The
   `scripts/test_hygiene.py` docstring and footer, `docs/questions.md`,
   `saberops.ready_frontier`'s module docstring and the affected test
   `Covers:` lines no longer cite it. `docs/gate.md` says the hygiene report is
   run after a full-gate pass rather than claiming an automated landing step.
5. **Correct docstrings and comments that point at deleted symbols.** The review
   decision field cites `mandatory_review_decision` (the surviving function),
   and the OpenCode transport evidence calls the `devscope` instruction the
   former one.

## 3. Consequences

- **No behaviour change.** The only `src/` edits are docstrings and comments in
  `models.py`, `ready_frontier.py` and `workers/instruction_compat.py`; the
  `scripts/test_hygiene.py` change is its docstring and printed footer. No
  function, signature, route, schema or CLI verb moves.
- **What a search now finds.** `.orch/` remains only where it names a live
  evidence root, an ignored runtime directory or fixture provenance.
  `WORKFLOW` remains only as code identifiers (`WORKFLOW_SETTINGS_VERSION`,
  `WORKFLOW_PREFIX`) and in historical records. The deleted module names remain
  only in decisions, release notes, migration notices/tests and H42 itself.
- **Deliberately left alone.** Decision records and release notes (history);
  fixture provenance that cites the old evidence path; the `.orch`/`.saberops`
  evidence-root contract; the `wave` label on PLAN.md lanes and the
  `--wave-barrier` name (both current); migration handling that must still
  recognise retired names; `src/saberops/DESIGN.md`'s dated design log and
  `docs/ui-design-principles.md`'s pre-v0.4 workspace text, which describe
  screens the earlier phase removed rather than anything phase 1.1 deleted —
  rewriting them would be new guidance, which this lane forbids.
- **Observed, not changed.** `context_projection.SurgicalCapsule` still has no
  caller on local `main` after H31; it is code, outside this lane's authorized
  area, and is reported for a later worker.

## 4. References

- Phase 1.1 plan, lane H42 and "Rules for every lane"; phase 1 "Rules for every
  lane"
- `AGENTS.md`, `README.md`, `docs/parallel-work.md`,
  `docs/orchestrator-parallel-first.md`, `docs/operations.md`,
  `docs/gate.md`, `docs/questions.md`, `docs/config.md`, `docs/mcp.md`
- `data/so-h42/notes.md` (search results, caller lists and gate output)
