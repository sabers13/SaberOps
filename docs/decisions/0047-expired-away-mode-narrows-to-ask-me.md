# ADR-0047: An expired away session narrows to ask-me and stops pausing work

- **Status:** Accepted. Phase 1 B3 engine-fix lane.
- **Date:** 2026-11-21
- **Decider:** Saber (owner), standing order for the B3 benchmark fixes
- **Supersedes in part:** the "ran past its `until`" closure in
  [ADR-0029](0029-no-work-without-a-planning-session.md) §2.1
- **Related:** [ADR-0020](0020-owner-mode.md) (owner mode),
  [ADR-0028](0028-away-mode-standing-decisions.md) (away standing decisions)

## 1. Context

ADR-0029 made the work gate close when an `away` session ran past its `until`.
That wording conflicts with the owner-mode contract, which already says an
expired `away` behaves as `ask-me`, and with the real-client B3 run: at the end
time the gate re-paused with "the away session reached its end time" after
`orch resume`, so only `orch mode clear` let dispatch proceed even though the
owner was back at the terminal.

F24 already requires a live planning session or attended terminal in every other
mode. Keeping a separate sticky pause for the expired grant adds no safety: the
ended grant is no longer a grant, and presence still governs work.

## 2. Decision

1. `effective_owner_mode` continues to narrow an expired `away` to `ask-me`.
2. The gate evaluates the away budget only while the grant is still effective.
   An expired `away` contributes no pause reason of its own; the ordinary F24
   presence rule decides. A legacy sticky `AWAY_ENDED` or `AWAY_CAP` record is
   ignored once the grant has expired.
3. A missing planning session still pauses work in the narrowed `ask-me` mode,
   exactly as it does for a directly selected `ask-me`.
4. An *unexpired* `away` that spends its job budget, or that was never bounded
   at all, still closes the gate as ADR-0029 requires.

## 3. Consequences

- The owner no longer needs `orch mode clear` to undo a grant whose own end time
  already passed; `orch resume` is again sufficient for the presence pause.
- The gate record may still display the expired budget as context, but it never
  turns that expiry into a closure.
- No authority is widened: `ask-me` is the narrowest mode, and the budget's
  remaining jobs and the authority preset are unchanged.
