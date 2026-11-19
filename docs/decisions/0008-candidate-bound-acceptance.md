# ADR-0008: Acceptance and review evidence bind to the candidate commit

- **Status:** Accepted; implemented by Phase 1 lanes C10 and F15
- **Date:** 2026-11-19
- **Decider:** Saber (owner), Phase 1 plan lanes C10 and F15
- **Related:** [ADR-0001](0001-saberops-engine-mcp-first.md) invariant 3,
  [ADR-0005](0005-mcp-first-engine-and-phase-2-board.md) §5,
  [ADR-0006](0006-stale-candidate-refresh.md),
  [ADR-0010](0010-receipt-bound-gate-skip.md) (gate receipts),
  [ADR-0020](0020-owner-mode.md) (owner mode)

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

## 4. Owner answer: automatic acceptance of a local commit (2026-11-19)

The lane asked whether automatic acceptance of a local commit should exist. The
owner answered:

> "2. do it in afk and Critical-only mode, leave it for manual mode so user
> approve it there"

This records "afk" as the `away` mode. Automatic acceptance is therefore a
mode-scoped extension of this decision, implemented by Phase 1 lane F15 in
`saberops.auto_accept`:

1. **Only `away` and `critical-only` may auto-accept, never `ask-me`.** The
   effective owner mode must be one of those two *and* the authority preset
   must still grant `auto_accept` (`full_autonomy` or
   `full_autonomy_publish`); the mode can only narrow that grant. An expired
   `away` is `ask-me` and accepts nothing. `ask-me` keeps the owner's manual
   accept for every candidate.
2. **The evidence is this record's evidence, for the exact candidate.** The
   candidate must be the run's final successful attempt and must hold its own
   candidate-linked independent review with a substantive approval
   (`PASS`/`PASS_WITH_BACKLOG`). A run-wide review, or the review of a replaced
   candidate, never qualifies. The read-only acceptance checklist must also be
   green, and the run must carry durable passing gate evidence for that
   attempt; a disabled gate does not qualify.
3. **The work gate must be open.** Automatic acceptance is work, so closing the
   planning CLI (ADR-0029) pauses it, and it never runs while `orch pause` is
   in force.
4. **The action is a local commit only.** It uses the existing accept engine,
   so the target-base check and compare-and-swap still run and the
   post-integration gate keeps the ADR-0010 receipt rules: a missing or stale
   receipt never skips the gate, it re-runs it. Nothing in the path pushes,
   publishes a branch or opens a pull request.
5. **It is auditable and reversible.** One `auto_accept_completed` event and
   one notice record the mode, candidate, base, review and branch.
   `orch accept <run-id> --undo` restores the target branch to the candidate's
   base with a compare-and-swap, keeps the candidate branch, attempt and
   evidence intact, and records `accept_undone`. Undo refuses when later work
   moved the target, when the tree is dirty, or when the candidate branch no
   longer names the candidate.
6. **It never re-accepts an undone candidate.** An acceptance record for the
   current candidate blocks a second automatic accept; the owner may accept it
   manually.

## 3. Consequences

- Replacing a reviewed candidate costs a fresh review before it can be
  accepted; that is the intended price of a correct guarantee.
- The board, MCP and the CLI all show the same eligibility because they read the
  same candidate-linked record.
- Acceptance cannot silently transfer evidence from one tree to another.
