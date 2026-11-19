# ADR-0006: Stale candidates refresh in isolation

- **Status:** Accepted
- **Date:** 2026-11-19
- **Decider:** Saber (owner), via the Phase 1 plan lane E6
- **Related:** [ADR-0001](0001-saberops-engine-mcp-first.md) invariants (isolation,
  deterministic gates, independent review, owner acceptance) and
  [ADR-0005](0005-mcp-first-engine-and-phase-2-board.md) §5.

## 1. Context

Two jobs can start from the same repository base. Accepting the first moves the
target, so the second becomes stale: its candidate still descends from the old
base. The previous workaround was to resubmit the stale work from the current
base with a fresh idempotency key, which throws away the original candidate and
its evidence and pays for another worker run.

## 2. Decision

A stale candidate is refreshed **only on explicit request** through the
`orch refresh RUN_ID` command or the `refresh_candidate` MCP tool. Refresh never
starts automatically.

The refresh preserves the source run, candidate commit, worktree, checks and
reviews. It applies the source candidate's changes to a **fresh isolated
worktree pinned at the target's current commit**, not to the target branch.
Conflicts fail the refresh and leave the original work and evidence intact for
repair.

The refreshed work is a **new run** with fresh gate evidence and a fresh
independent review bound to the new candidate SHA. The old review never
qualifies the refreshed SHA. Refresh never moves the target branch; the owner
accepts the refreshed candidate separately through the existing acceptance path,
whose target-base check and compare-and-swap are unchanged.

## 3. Consequences

- Stale work is recoverable without re-running the worker or losing evidence.
- A conflict is reported as a failed refresh with the original candidate kept.
- A target that moves during refresh makes the refresh fail closed; both the
  owner's new target revision and the refreshed candidate are preserved.
- Refresh costs one gate and one review. Isolation, deterministic verification,
  independent review and owner acceptance keep their existing guarantees.
