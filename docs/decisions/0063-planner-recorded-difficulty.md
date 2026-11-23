# ADR-0063: The planner states task difficulty; the engine maps it to a route

- **Status:** Accepted. Phase 1.1 lane H1.
- **Date:** 2026-11-23
- **Decider:** Saber (owner)
- **Amends:** [ADR-0001](0001-saberops-engine-mcp-first.md) invariant 3 and
  [ADR-0005](0005-mcp-first-engine-and-phase-2-board.md) §3 (strong start)
- **Related:** [ADR-0011](0011-bounded-automatic-actions.md)

## 1. Context

Which model starts a task was guessed in the engine: `classifier.py` scored the
task text with a fixed keyword table (`refactor` +2, `typo` -2, requirements
+1 each) into T1-T3, and `routing_decision.choose_tier` turned that score into
the run's tier. The planning CLI — the one actor that has read the task, its
repository and its context — was forbidden to say "this is hard": the MCP
requirement denylist refused `tier`, and only `effort` and `size_hint` were
accepted. A stronger-model start happened only on a manual `orch run` at T3.

That is a judgement made with the wrong tool. The owner's rule is "LLMs advise;
deterministic software decides": the planner should give the judgement, and
the engine should keep every enforcement decision (quota, the owner's ceiling,
escalation evidence) in code. The keyword scorer also produced a
transparent-looking audit trail for a decision nobody could actually inspect
in context.

## 2. Decision

1. **Difficulty is a requirement, not a route.** `submit_job` accepts
   `requirements.difficulty` with exactly `routine` and `hard`; `orch run` and
   `orch job submit` accept `--difficulty`, and a `PLAN.md` lane may set
   `"difficulty"` in its machine block. A missing value means `routine`.
2. **The engine maps it.** `hard` starts on the build route's existing
   `strong_start` chain; `routine` (or missing) starts on the ordinary build
   chain. An empty `strong_start` setting falls back to the build chain, so a
   planner omission never parks a job. Quota admission is unchanged, and the
   scheduler performs the same mapping for queued jobs.
3. **The owner's ceiling still wins.** `max_auto_tier` below T3 keeps even a
   `hard` run on the ordinary build chain. Escalation is still allowed only on
   a typed `CAPABILITY_FAILURE` recorded from a dispatched attempt; exhaustion,
   timeouts, gate failures and pre-dispatch skips never escalate.
4. **The keyword classifier is removed.** `classifier.py` and its
   `classify_task_tier` caller in `routing_decision.choose_tier` are deleted.
   The remaining consumers of a tier label (the run row, the reviewer ladder,
   the risk-adaptive review class) read the difficulty recorded on the run
   instead. The single BUILD-role worker deadline is unchanged: it is a role
   default, not a difficulty judgement.
5. **No route by another name.** `provider`, `model`, `tier`, `billing`,
   `route`, `quota` and `account` are still refused at the top level and inside
   `requirements`. `difficulty` is not an alias for any of them; the values are
   the two judgements above, validated as a closed vocabulary.
6. **Idempotency is preserved.** A default (routine) difficulty is excluded
   from the request digest, so an idempotency key stored before this change
   still replays as the same request.

## 3. Consequences

- The planner can state the one judgement it is best placed to make, and the
  operator can read that judgement directly in the job record.
- The engine keeps the whole spend-authority decision: quota, ceiling,
  escalation evidence. Nothing a planner omits loosens a guarantee.
- A missing difficulty degrades to the ordinary build chain, never to a
  stronger or unmetered route.
- The keyword scoring table and its tests are gone; the run configuration
  carries `difficulty` explicitly, and legacy payloads reconstruct as
  `routine`.
- Existing manual `--tier T3` behaviour is unchanged (it still selects the
  strong start); the deprecated tier flags are kept for compatibility.

## 4. References

- Phase 1.1 plan, lane H1 (`phase-1.1-core-plan.md`)
- `src/saberops/jobs/models.py` (`Difficulty`, `JobRequirements`)
- `src/saberops/config.py` (`RunConfig.initial_tier`,
  `RunConfig.initial_strong_start`)
- `src/saberops/capacity/scheduler.py` (queued-job chain selection)
- `src/saberops/routing_decision.py` (`choose_tier`,
  `is_escalation_evidence`)
