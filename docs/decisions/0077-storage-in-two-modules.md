# ADR-0077: Storage is two modules — budget/admission and inventory/cleanup

- **Status:** Accepted. Implemented in phase 1.1 lane H25.
- **Date:** 2026-11-23
- **Decider:** Saber (owner), phase 1.1 owner answers 2026-11-22 and 2026-11-23
  ("maximise the deletion"), with "verify before deleting" and the safety list
  unchanged
- **Related:** [ADR-0017](0017-opt-in-landed-worktree-cleanup.md) (opt-in landed
  cleanup), [ADR-0011](0011-bounded-automatic-actions.md) (bounded automatic
  actions), `docs/storage-lifecycle.md` (the owner-facing contract)

## 1. Context

Five modules carried the storage lifecycle: `storage_budget.py` (2,210 lines,
the adaptive reservation ledger), `storage_inventory.py` (1,173, the read-only
inventory), `storage_lifecycle.py` (721, a pure eligibility planner over
caller-supplied dataclasses), `storage_gc.py` (1,603, the guarded preview and
executor) and `storage_maintenance.py` (467, the fixed disk floor, the bounded
maintenance pass and the audit sink). 6,174 source lines for one concept split
by the order in which it was delivered.

The split cost more than it saved. The lifecycle planner's dataclasses
(`GateScratch`, `AttemptWorktree`, `CheckpointRef`, `EvidenceBundle`,
`WorktreeVenv`, `OwnerDatabase`), its `ArtifactKind` enum (a subset of the
inventory's `ArtifactClass`) and its `PlanDecision`/`GcPlan`/`DeletionEvent`
output shapes were a second representation of objects the inventory already
described. `storage_gc` converted every `InventoryItem` into that second shape,
called the planner, and then re-checked its verdict against policy, ownership
and `protected` flags before producing a candidate. The planner's result was
reduced to a boolean plus a reason string. The planner's gate-scratch retention
rules (succeeded / older-failed / newest-failed cap) were never reached by any
production path at all: the real newest-failed cap lives in
`scripts/gate_tools.keep_failed`, and the real scratch sweep keys on a provably
dead owner.

`storage_maintenance` and `storage_gc` also duplicated the owner-DB vacuum
constants, and the merged module previously needed three import styles for one
concept.

## 2. Decision

1. **Two modules, one seam each.**
   - `saberops.storage_budget` stays the budget/admission module, unchanged. It
     is still not activated; the fixed free-space floor on every launch path
     stays binding.
   - `saberops.storage_cleanup` is the single inventory/cleanup module:
     inventory, the retention policy, the guarded preview/apply service, the
     opt-in landed sweep and the bounded maintenance pass with its audit sink,
     in that order. `storage_inventory.py`, `storage_lifecycle.py`,
     `storage_gc.py` and `storage_maintenance.py` are deleted; every importer
     (`cli.py`, `web.py`, `orchestrator.py`, `service.py`,
     `capacity/loop.py`, `preflight.py`, `gate_runner.py`, tests and the
     benchmark rig) imports from `saberops.storage_cleanup`.
2. **One representation.** Retention eligibility is decided directly from
   `InventoryItem` and its `detail` mapping; the second dataclass shape, the
   planner's action/plan vocabulary and the conversion layer are gone. The
   checks themselves are unchanged and keep their own section ("Retention
   policy"): path/ref confinement, fail-closed ownership, terminal-only
   worktrees, candidate-pending and preempted-job keeps, checkpoint retention,
   evidence compression and deletion windows, venvs following their worktree.
3. **Behaviour is unchanged, and the refusal set is the contract.** Dry-run is
   still a true no-op; nothing is forced; unlanded, dirty, uniquely-ignored,
   live-owned and unknown-owned artifacts are refused exactly as before; the
   per-action revalidation and intent-first audit are untouched. The only new
   validation is that a naive `now` passed to `preview_gc` is rejected, matching
   the deleted planner's rule.
4. **Deleted with their tests.** The planner module and its 914-line test file
   are gone. The safety cases that matter (retention windows, pending/preempted
   keeps, job-bound checkpoints, evidence hold, venvs, path escapes, unknown
   ownership, owner-DB vacuum candidate, dead-owner scratch) are re-expressed
   against `preview_gc` in `tests/test_storage_gc.py`. The planner's
   never-reached gate-scratch rules and their unit tests are dropped with the
   planner; the live cap remains `scripts/gate_tools.keep_failed`.
5. **The owner policy file name does not change.** `storage_gc.json`, its
   schema and its refusal of malformed or contradictory documents are untouched,
   so an existing owner store needs no migration.

## 3. Verification

- New retention drills in `tests/test_storage_gc.py` fail on the pre-merge tree
  (they import the merged module) and pass after it; they assert the same
  candidate/keep outcomes the planner tests asserted.
- The storage drills (`test_storage_gc`, `test_storage_inventory`,
  `test_storage_maintenance`, `test_storage_integration`) pass unchanged in
  their deletion and refusal assertions, and `test_storage_budget` plus the A8
  `test_memory_budget` stay green.
- Measured size (`wc -l`): the five storage modules 6,174 → 5,425 lines
  (`storage_budget` 2,210 unchanged, `storage_cleanup` 3,215), a reduction of
  749 source lines; the storage test files 5,737 → 5,024, a reduction of 713
  test lines, while the ported safety drills add 195 lines to
  `tests/test_storage_gc.py`.

## 4. Consequences

- A reviewer reads one file to see what SaberOps owns, what a standing policy
  may retire, how it is retired and how the scheduler's bounded pass behaves.
- `import saberops.storage_gc` (and the other three) now fails; there is no
  compatibility shim, because a shim restores the split this lane removes.
  Nothing outside the repository imported them.
- The merged module is about 3.2K lines. If it grows further, the next split
  should follow the section boundaries already in it (inventory, retention
  policy, executor, maintenance), not a historical delivery order.
