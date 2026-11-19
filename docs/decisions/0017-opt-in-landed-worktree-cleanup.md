# ADR-0017: Opt-in cleanup of clean, verified-landed worktrees

- **Status:** Accepted. The opt-in `orch cleanup --landed` sweep is implemented.
- **Date:** 2026-11-19
- **Decider:** Saber (owner), Phase 1 plan lane F6
- **Related:** [ADR-0001](0001-saberops-engine-mcp-first.md),
  [ADR-0005](0005-mcp-first-engine-and-phase-2-board.md),
  [ADR-0011](0011-bounded-automatic-actions.md)

## 1. Context

Finished attempts leave per-run Git worktrees behind. The guarded storage
lifecycle already retires one run's clean worktrees on explicit request and
under a standing GC policy, but it treats every terminal candidate class alike.
That is broader than the owner asked for: the phase plan calls for retiring only
copies whose work is provably integrated, while failed, dirty and unlanded work
is preserved for inspection, retry or repair.

## 2. Decision

1. **Nothing runs automatically.** Cleanup of landed copies is an explicit,
   opt-in command: `orch cleanup --landed [--repo PATH]`. It is not wired into
   acceptance, the scheduler or the storage-maintenance tick.
2. **Landed is proven, not inferred from status.** A run qualifies only when its
   candidate projection reports `ACCEPTED` and the accepted candidate SHA is
   still an ancestor of the target branch named by its latest `run_accepted`
   event. A missing target head fails closed.
3. **Only the landed candidate's own worktree is a candidate.** Attempts whose
   commit is a different SHA (earlier, failed or repair attempts) are not
   removed by this path.
4. **The existing guards stay in force.** Each removal reuses the guarded
   lifecycle checks: an inactive terminal run, an inside-base resolved path, no
   symlink, no unique ignored content, a clean working tree, and non-forcing
   native `git worktree remove`. There is no repository-wide prune, no forced
   removal, and no branch or database deletion.
5. **Everything else is kept and reported.** Failed, active, unaccepted,
   unlanded, dirty and protected copies are reported with a machine-readable
   reason and their age against a bounded retention window. The report names the
   explicit per-run route for owner-inspected removal; the sweep itself never
   deletes them.

## 3. Consequences

- The owner gains a low-risk cleanup step that can only remove work already
  accepted into the target branch, and every kept copy remains inspectable.
- Landed worktrees still need an explicit invocation; the engine never reclaims
  them on its own, even after acceptance.
- The retention window is informational for kept copies. Deleting failed or
  unlanded work remains a separate, explicit owner decision.
