# ADR-0107: The advisor has its own call bound

- **Status:** Accepted. Implemented in phase 1.1 lane H49.
- **Date:** 2026-11-27
- **Decider:** Saber (owner), phase 1.1 plan lane H49, written from the H37
  client check. The lane was added after the on-demand advisor timed out on a
  real succeeded job in two sessions.
- **Related:** [ADR-0034](0034-advisor-checkpoints.md) (the on-demand advisor),
  phase 1.1 plan lane H49, the H37 report §8 D3, and `data/so-h49/notes.md`
  (reproduction, caller lists, line counts).

## 1. Context

`ask_advisor` passed a hard-coded `DEFAULT_ADVISOR_TIMEOUT_SECONDS = 120.0` to
the provider adapter as the subprocess bound for one advisor turn. H37's Codex
and OpenCode sessions asked the advisor (route `codex/gpt-6-luna`) on a
succeeded job and got `ADVISOR_UNAVAILABLE: advisor call returned TIMEOUT`,
twice. The worker's own terminal bound is not involved: a build dispatch gets
1200 s (`ROLE_WORKER_TIMEOUTS`), so the advisor did not inherit it. The advisor's
own 120 s constant was simply shorter than a real model turn over the task, the
attempt record and a bounded diff.

Nothing is broken about refusing after a bound: a hung advisor must still end.
What was wrong is the value and whose knob it is. 120 s is not a safe ceiling
for a read-only model turn, and the owner had no way to set a different one
without editing code.

## 2. Decision

1. **The bound is the route's, not a constant in the call path.**
   `AdvisorRoute` carries `timeout_seconds` next to `call_budget`, written by
   `orch project advisor set --timeout SECONDS` and read from `advisor.json`.
   `ask_advisor` passes it to the adapter.
2. **The stated default is 600 s.** One advisor turn reads a bounded prompt
   (question, task, plan lines, recent output, failing checks, at most 3000
   characters of diff), so it is a normal model turn, not a review of a whole
   codebase. H37 saw one outlive 120 s. 600 s is five times that failure point
   and half the reviewer's 1200 s bound: an advisor that hangs ends long before
   it holds a planner's MCP call for as long as a full review, while a real
   answer in the 2-10 minute range is not cut off.
3. **A bound still exists, and it is bounded.** `timeout_seconds` must be
   between 1 s and 3600 s; a value outside that range is a config error, not a
   silent default. So no caller can turn the advisor into an unbounded call.
4. **A timeout names the route and the bound.** The refusal reason is
   `advisor route <provider>/<model> timed out after <n>s`, and its
   `advisor_unavailable` event carries `route`, `timeout_seconds` and the
   observed `duration_seconds` next to the provider and model it already
   recorded. The adapter's own `detail` (for example
   `Worker timed out after 600s`) is kept.
5. **Old documents keep working (expand, no migration).** A schema-v1
   `advisor.json` without `timeout_seconds` reads on the default; only the next
   owner write records the field. No reader is removed.

Out of scope, as the plan states: the worker's terminal bound, and which route
advises.

## 3. Consequences

- A slow but healthy advisor answers. A hung advisor ends, and the refusal
  tells the owner which route and which bound to change.
- The owner can shorten the bound (for a cheap fast advisor) or lengthen it (up
  to 3600 s) per project without a code change.
- The number of advisor calls per job is unchanged (`call_budget`, default 3);
  this ADR only bounds the time each call may take.
- A headless worker that asks blocks its own turn for up to the bound; its own
  terminal bound (1200 s) remains the outer limit, unchanged.
- Evidence: `tests/test_f26_advisor.py` has a 121 s fake advisor that must get
  an answer (`@pytest.mark.slow`), a hung fake advisor whose refusal must name
  the route and the 2 s bound it was given, and a document/CLI test for the
  setting and the pre-ADR-0107 default. `tests/fixtures/fake_worker_cli.py`
  gained `advisor_delay_seconds`, which applies only to an `[ADVISOR REQUEST]`
  prompt, and the lifecycle rig passes it through.
