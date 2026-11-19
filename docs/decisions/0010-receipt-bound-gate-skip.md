# ADR-0010: Skip the duplicate post-integration gate only for a receipt-bound fast-forward

- **Status:** Accepted (owner approval 2026-11-19). Implementation pending in
  Phase 1 lane E2; until then the gate runs as it does today.
- **Date:** 2026-11-19
- **Decider:** Saber (owner)
- **Related:** [ADR-0001](0001-saberops-engine-mcp-first.md) (acceptance),
  [ADR-0008](0008-candidate-bound-acceptance.md),
  [ADR-0009](0009-gate-and-test-policy.md)

## 1. Context

Accepting a change runs the full gate twice: once per attempt in the detached
verification snapshot, and again after acceptance fast-forwards the target —
even when the target moves to the exact commit that already passed. The second
run is the integration gate that confirms the landed tree.

Skipping it saves gate time but changes an acceptance guarantee, so the owner
approved the exception with explicit conditions rather than leaving it to an
implementation choice.

## 2. Decision

The post-integration gate may be skipped **only when all of these hold**:

1. The integration is an **exact fast-forward** of the target to the candidate
   SHA: no merge commit, no rebase, no new tree.
2. The candidate SHA holds a **passing full-gate receipt**.
3. The receipt was produced for the **same base SHA and the same candidate
   SHA**.
4. The receipt is **bound to the exact candidate SHA, base SHA, gate command
   and gate configuration**.
5. The receipt is **less than 24 hours old** at acceptance.

If any of these fail, the full gate re-runs. In particular, any merge, any
refresh, a changed gate command, a changed gate configuration, or an expired
receipt re-runs the full gate.

Everything else is unchanged: the target-base check, the final
compare-and-swap, owner-only acceptance and the recorded evidence. The receipt
used and the fact that the gate was skipped are recorded with the acceptance.

## 3. Consequences

- A clean accept of an already-gated tree saves one full gate; a mismatched or
  stale receipt costs a gate, never an ungated landing.
- The guarantee becomes precise: **the exact tree that passed the full gate is
  the exact tree that lands.**
- The exception is bound to a receipt, so it cannot be widened by a caller or
  by an implementation drifting from these conditions.
