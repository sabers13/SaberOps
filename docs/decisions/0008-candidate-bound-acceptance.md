# ADR-0008: Acceptance and review evidence bind to the candidate commit

- **Status:** Accepted; implemented by Phase 1 lane C10
- **Date:** 2026-11-19
- **Decider:** Saber (owner), Phase 1 plan lane C10
- **Related:** [ADR-0001](0001-saberops-engine-mcp-first.md) invariant 3,
  [ADR-0005](0005-mcp-first-engine-and-phase-2-board.md) §5,
  [ADR-0006](0006-stale-candidate-refresh.md),
  [ADR-0010](0010-receipt-bound-gate-skip.md) (gate receipts)

## 1. Context

A gate-passed replacement candidate could reach the integration step using the
passing run-wide review earned by the commit it replaced, even though the
replacement itself had no verdict. A review that assessed one commit was
therefore able to qualify a different tree for the owner's acceptance.

## 2. Decision

1. **Both eligibility and owner acceptance use candidate-linked review
   evidence.** The review must assess the exact candidate commit being offered.
2. **A passing review for commit A never qualifies a replacement candidate B.**
   B qualifies only after its own passing review. An unrelated verdict for A
   leaves B's eligibility unchanged.
3. **A review that names no commit is historical evidence only.** Where policy
   requires a review of the current commit, it does not satisfy that
   requirement.
4. **A refresh produces a new candidate.** The refreshed work gets fresh gate
   evidence and a fresh review (see [ADR-0006](0006-stale-candidate-refresh.md));
   the old review never carries over.
5. **The existing acceptance checks stay:** the target-base check, the final
   compare-and-swap, owner-only acceptance and the recorded evidence.

## 3. Consequences

- Replacing a reviewed candidate costs a fresh review before it can be
  accepted; that is the intended price of a correct guarantee.
- The board, MCP and the CLI all show the same eligibility because they read the
  same candidate-linked record.
- Acceptance cannot silently transfer evidence from one tree to another.
