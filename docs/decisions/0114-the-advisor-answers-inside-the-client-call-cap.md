# ADR-0114: The advisor answers inside the client's call cap

- **Status:** Accepted. Implemented in phase 1.1 lane H56.
- **Date:** 2026-11-28
- **Decider:** Saber (owner), phase 1.1 plan lane H56, written from the H37
  recheck (answer "yes start those four, do option 2 for N3" — the four are
  N1, N2, N4, N7).
- **Related:** [ADR-0034](0034-advisor-checkpoints.md) (the on-demand advisor),
  [ADR-0107](0107-the-advisor-has-its-own-call-bound.md) (the advisor's own
  600 s bound), the H37 recheck report §9 N7 and §6 D3, and
  `data/so-h56/notes.md` (reproduction, caller lists, line counts).

## 1. Context

`ask_advisor` ran the whole model turn inside the planner's MCP `tools/call`.
H37's recheck measured what that costs when the turn is slow:

- Codex gave up after 300,044 ms with `timed out awaiting tools/call after
  300s`. No advice was recorded; `model_call_usage` kept an `advisor` row with
  `total_tokens NULL`.
- Claude's call ran past 360 s and died when the turn ended.

ADR-0107 had raised the advisor's own bound to 600 s, so the engine was willing
to wait longer than any client would. The bound was right; the shape was wrong.
The tightest known client call caps are **300 s (Codex, measured)** and
**360 s (Claude turn)**. A synchronous call can produce no answer at all that
the planner can read, and the recorded evidence says the model call may have
happened while the advice was lost with the request.

## 2. Decision

1. **The model turn runs outside the planner's request.** `start_advisor_call`
   prepares the call exactly as before (route, gate, budget, availability, the
   `advisor_call_started` event, the rendered prompt) and then runs the turn on
   a daemon thread. The planner's call returns a handle
   (`status: "started"`, `call_id`, `run_id`, route, budget, bound).
2. **The same tool polls, with the `wait_job` shape.** `ask_advisor` with a
   `call_id` and `wait_seconds` waits on the durable run records — no second
   mechanism, no in-memory queue — and returns `answered` with the advice,
   `running` when the wait expires, `refused` (as a typed tool error naming
   the reason) when the turn failed, and `lost` when the engine that held the
   call ended before it answered. A start call may wait too; its default is
   zero so the handle always comes back immediately.
3. **The wait is under every known cap.** `MAX_ADVISOR_POLL_SECONDS` is 240 s
   and the poll default is 60 s. Both stay below the 300 s Codex cap and the
   360 s Claude turn, so no `tools/call` can outlast its client. The advisor's
   own route bound (ADR-0107, default 600 s) is unchanged and remains the outer
   limit of the model turn itself.
4. **A late answer is still recorded and retrievable.** Success, timeout and
   every refusal record on the run exactly as before, now carrying the
   `call_id` (and the call's `started_at` and `timeout_seconds`), so a planner
   that stopped polling still finds the advice in `catch_up`'s `advisor` block.
   The per-job budget still counts each started call once: an interrupted call
   did spend the model call.
5. **The worker path is unchanged.** `orch advisor ask` stays synchronous and
   keeps the worker's own terminal bound; that path is out of this lane's
   scope, as the plan states.

## 3. Consequences

- `ask_advisor` from Codex and Claude returns inside the client cap even when a
  real advisor turn takes several minutes; the planner polls until the answer
  or is told the call was lost.
- A one-line question costs two MCP calls instead of one when the advisor is
  slower than the planner's first wait, and still one call when it answers
  inside the requested wait.
- The advisor's evidence is unchanged in kind; `advisor_unavailable` payloads
  gain the `call_id`, and the call record gains `started_at` and
  `timeout_seconds` so a poll can tell running from lost.
- Evidence: `tests/test_f26_advisor.py` covers the handle/poll path, a refused
  call polled to its typed error, a call whose engine died (`lost`), and a
  `@pytest.mark.slow` rig test whose fake advisor takes 301 s — longer than the
  Codex cap — where every `tools/call` stays under 240 s and the answer still
  arrives.
