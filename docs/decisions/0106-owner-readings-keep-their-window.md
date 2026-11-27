# ADR-0106: An owner reading keeps the window it measures

- **Status:** Accepted. Implemented in phase 1.1 lane H48.
- **Date:** 2026-11-27
- **Decider:** Saber (owner): "Q15 = wire the check protection, don't delete it";
  phase 1.1 lane H48 approved with the plan's recommendations (2026-11-22 and
  2026-11-23 recorded answers).
- **Related:** `data/so-h37/report.md` §8 D2 (the real-session evidence and the
  job/run ids); phase 1.1 plan lane H48; `data/so-h48/notes.md` (reproduction,
  caller lists and line counts).

## 1. Context

H37 ran the real Claude reviewer against the owner's measured subscription
usage and every review was `REVIEW_UNAVAILABLE`. The evidence was
`review_candidate_skipped reason: quota_blocked`, `normalized_remaining_pct
20.0`, `red_line_pct 15.0`, `mode STRICT`, `projected_cost_pct 8.0`, for both
`anthropic/default` and `anthropic/claude-haiku-4-5-20251001`.

Two facts combined:

1. `orch quota set anthropic <pool> --remaining 20` records the owner's reading
   against the pool, not a window. The pool name is not a window name, so the
   reading is stored with window `ALL` and matched every window the pool plan
   declares.
2. The pool plan declares a 5-hour and a weekly window. The 5-hour window
   projected the 8 % medium-job 5-hour cost prior against the *weekly* figure
   (20 % − 8 % = 12 % < 15 %), so admission refused the review. The weekly
   window would have projected 2.5 % (17.5 % ≥ 15 %) and admitted it.

The reading was truthful and high enough; the engine measured it in the wrong
window. The same reading also could not be recorded with its window, so the
only alternatives were to lower the red line or to keep failing closed.

The second, smaller defect: the cheapest Codex model cannot review when it is
also the build route. Preflight reported "reviewer is not independent of the
build route" with the remedy `orch reviewer select '<independent-binding-id>'`,
so the owner had to guess the cheapest eligible independent route.

## 2. Decision

1. **A reading records the window it measures.** `orch quota set` gains
   `--window` and stores it on the observation (`capacity_observations.window_id`)
   and in the state that reaches admission. A window-named pool keeps its old
   behaviour; `--window` covers pools whose name is not a window (the H37
   subscription pool). Nothing is guessed: without `--window` the reading stays
   a pool-wide reading.
2. **Cost is projected only against the window a reading describes.** A
   window-specific reading is evaluated with that window's cost prior and
   red-line reason. Every other declared window still applies its red line to
   the same reading, but never inherits a cost prior for a window the reading
   does not describe.
3. **Probe evidence stays strict.** A probe measures one window at a time, so a
   missing probed window on an overage pool still refuses with
   `QUOTA_EVIDENCE_REQUIRED` (the packaged weekly/monthly rule is unchanged).
   A pool with probe evidence is never covered by an owner reading's fallback.
4. **The red line is unchanged.** 15 % remains the STRICT red line and a
   genuinely low reading still refuses: a weekly 5 % reading fails
   `RED_LINE_WEEKLY` (and the same reading fails the 5-hour red line).
5. **A reviewer refusal names routes, never a placeholder.** When the selected
   reviewer matches the build route, preflight names the cheapest independent
   CHECK binding and the printed command is the one that fixes it. When no
   independent binding exists, the refusal lists the configured routes and says
   every one matches the build route. `REVIEW_UNAVAILABLE` details list every
   route tried with its refusal reason.
6. **Cheapest is the existing cheap-plan-model rule**, not a price table: a
   cheap plan model sorts before other bindings, the owner's declaration order
   breaks ties, and no new price data enters the engine.

Out of scope, unchanged: the red-line percentage, paid or API-key billing, the
advisor timeout (H37 D3), and any new quota policy vocabulary.

## 3. Consequences

- The owner records the weekly figure once (`--window weekly`) and a cheap
  review runs at 20 %; the same 20 % recorded window-less stays a conservative
  5-hour projection, exactly as before.
- A recorded window is honest evidence, not an escape hatch: the red line,
  mode, overage freshness rule and probe strictness are unchanged.
- Preflight's reviewer remedy is now a runnable command with no placeholder to
  guess, matching the D11 rule for printed remedies.
- A planner reading the review record can say which reviewer routes were tried
  and why none launched, instead of "No reviewer available".
- The change is additive on stored data: existing rows keep window `ALL` and
  their old conservative behaviour; no store migration is needed.
