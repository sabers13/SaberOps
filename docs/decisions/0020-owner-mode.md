# ADR-0020: One owner mode narrows authority; it never widens it

- **Status:** Accepted. Implemented in Phase 1 lane F1 (Wave 3).
- **Date:** 2026-11-19
- **Decider:** Saber (owner), Phase 1 plan lane F1
- **Related:** [ADR-0001](0001-saberops-engine-mcp-first.md) (plan limits),
  [ADR-0005](0005-mcp-first-engine-and-phase-2-board.md) (roles and authority),
  [ADR-0008](0008-candidate-bound-acceptance.md) (candidate-bound acceptance),
  [ADR-0011](0011-bounded-automatic-actions.md) (no silent work)

## 1. Context

SaberOps had five authority presets but no single attended/away switch. The
questions page rendered the unwired `unattended.py` projection as `LIVE`
standing permissions, and the authority table printed policy booleans that no
live code read. That was a product-truth defect: a fresh install looked as if it
had autonomous behaviour it did not have.

The owner asked for modes in which the system asks every judgment (`ask-me`),
asks only what the orchestrator judges critical (`critical-only`), or runs while
the owner is away (`away`), and required that only the owner switch modes.

## 2. Decision

1. **One persisted owner mode.** The document beside the authority config holds
   `ask-me`, `critical-only` or `away`, plus an optional `until` end time. It is
   versioned and validated fail-closed. A missing document means `ask-me`; a
   corrupt one raises rather than selecting a different mode. Only `away` may
   carry `until`, and an expired `away` narrows to `ask-me`.
2. **Effective permission = authority preset ∩ mode.** The intersection can
   only remove a permission the preset already grants. `ask-me` clears
   autonomous retry, repair, escalation, acceptance, publication and safe
   cleanup. `critical-only` and `away` keep the preset unchanged. Dispatch
   follows the preset's `auto_start` in every mode, so `away` never exceeds the
   preset and a preset without `auto_start` parks dispatch instead of launching
   it.
3. **One typed decision function.** Every engine decision point asks
   `decide_owner_action` for a `ModeDecision`: dispatch at the scheduler,
   autonomous start, retry and repair/escalation through the autonomous
   supervisor, and candidate publication at run creation. The decision carries
   the effective mode, the authority preset, the boolean result and a readable
   reason. No decision point inspects a policy boolean on its own.
4. **Only the owner switches the mode.** `orch mode show|set|clear` and the
   Questions page toggle are the write paths. MCP exposes a read-only
   `get_mode` and a `propose_mode_change` that raises an owner question and
   never flips the mode. The planner guide may describe a mode only after
   reading it with `get_mode`.
5. **Recorded intent stays advisory until wired.** A policy dimension or action
   that no live code reads is labelled advisory, never `LIVE`. This covers the
   `unattended.py` standing permissions and the authority fields
   (`auto_accept`, `auto_publish`, `auto_gate`, `auto_review`,
   `auto_safe_cleanup`, `routine_questions`) that have no reader yet.

## 3. Consequences

- A mode cannot create a second, weaker authority system. The owner's authority
  ceiling still governs every automatic action.
- `ask-me` is the default and the safest state: autonomous recovery parks and
  the owner accepts manually. `full_autonomy + ask-me` never auto-accepts.
- An expired `away` is visible as expired and behaves as `ask-me`; it never
  keeps acting on an old grant.
- This lane does not wire auto-accept, standing decisions, a return summary or
  close-the-CLI pausing. Those remain separate lanes (F15, F11 and F17) and
  build on the same store and decision function.
- The safety and truth boundary is narrow and testable: the scheduler parks an
  `away` dispatch the preset forbids, the supervisor's recovery properties
  follow the mode, and no page or CLI promises an unread permission as live.
