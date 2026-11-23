# ADR-0062: A captured pre-1.1 store, a cross-surface MCP contract check and a fresh-install check

- **Status:** Accepted. Phase 1.1 wave 0 (lane H36).
- **Date:** 2026-11-23
- **Decider:** Saber (owner), phase 1.1 plan and owner decisions 2026-11-22,
  2026-11-23 (`/home/saber/fm-test/firstmate/data/saberops-plan/phase-1.1-core-plan.md`)
- **Related:** [ADR-0005](0005-mcp-first-engine-and-phase-2-board.md) (the
  planning CLI is the orchestrator), ADR-0016 (steering), ADR-0034 (advisor),
  ADR-0042 (MCP tool list and client registration)

## 1. Context

Phase 1.1 removes or renames things an existing install already carries: the
orchestrator binding (H3), review knobs (H4), recovery verbs (H7), CLI view
names (H9), the Cline route (H17) and the authority presets (H22). The phase's
exit test requires that a store from before the phase starts on the new code,
prints one clear notice for each removed item, and still runs a job to
acceptance; and that a fresh install reaches a first accepted job from
`preflight` alone.

None of that is proven today. The MCP contract is also spread across several
surfaces written by different lanes: the registry and `TOOL_NAMES`, the module
docstring, the built-in guide, the installed skill, the generated proxy
definitions and `orch mcp tools`. Drift between them is invisible to the
engine; two drifts already existed (`promote_scout`, `refresh_candidate` and
`get_pause` missing from the docstring list, `list_proposals` missing from the
guide).

## 2. Decision

1. **The upgrade fixture is a captured store, not a builder.** The artifacts
   under `tests/fixtures/pre_1_1_store/` are copied from a fake-mode store built
   on pre-1.1 `main` (`7384d36`). A builder that calls today's public
   functions would stop working exactly when the lanes delete those functions,
   so only `capture.py` records how the store was made and is not part of the
   check. The capture never touches the owner's real store.
2. **Restore rewrites paths, never owner settings.** `tests/pre_1_1_store.py`
   installs the fixture into an isolated store, rewrites the capture machine's
   paths (including `{python}` so the gate command stays portable), recomputes
   the path-derived project directory, and refreshes only runtime evidence with
   a short freshness window (CLI readiness, quota). Every captured owner
   setting is read back unchanged by the check.
3. **The MCP contract is one set checked across every surface.** The registry
   must equal `TOOL_NAMES`, the module docstring's exposed list, the tools named
   in the guide and the skill, and `orch mcp tools`; every description must be
   real; no surface may name a removed tool. Removed tools and one-release
   aliases are table rows in `tests/test_h36_contract.py`, added by the lane
   that removes or aliases the tool and deleted when the alias window ends.
4. **The fresh-install check follows the first-run walkthrough.** It starts with
   an empty store and a clean repository, runs only the commands
   `start_walkthrough` renders from `preflight`, supplies only the two values
   the walkthrough marks as the owner's (the project check command and a quota
   reading), and ends at the first job submitted through the MCP `submit_job`
   tool being accepted after review. The one test-host adjustment is the disk
   floor, which belongs to the host rather than the setup flow. "Install" is
   the checkout's editable install that the gate already runs: creating a fresh
   virtualenv would test packaging and the offline wheel cache, not the setup
   flow, and cannot be made deterministic inside the gate.
5. **Upgrade notices are rows, not guesses.** `tests/test_h36_upgrade.py`
   carries an empty `NOTICE_ROWS` table and a `current_code_notices` probe
   point. Each removal lane adds its row (surface, lane, expected notice) and
   extends the probe; the check asserts each notice appears exactly once, and
   the store still runs a job to acceptance.
6. **The two existing drifts are corrected** (the docstring's exposed list and
   the guide's `list_proposals` sentence), because a contract check that is red
   on landing proves nothing about later lanes.

This ADR adds checks only; it changes no runtime behavior, authority, readiness
or acceptance rule.

## 3. Consequences

- A removed tool, view, verb or setting cannot land without a contract or
  upgrade row, because the surfaces no longer agree until the row is added and
  the notice implemented.
- The fixture is a durable snapshot of the pre-1.1 file formats; a later schema
  migration is exercised by opening the captured database, not by rebuilding it.
- The capture script will stop importing after its lane's deletions. That is
  expected; it is documentation of a one-time act, and the artifacts are what
  the check uses.
- The checks cost about 30 seconds combined in the full gate and run in fake
  mode with no vendor prompt, no network and no owner store.
- The memory budget requirement in H36's done-when (phase 1 A8) is measured
  later with the rest of the full gate; wave 0 records the timing it adds.
