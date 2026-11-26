# ADR-0060: Gate protection runs on the real job and acceptance paths

- **Status:** Accepted. Phase 1.1 lane H32.
- **Date:** 2026-11-23
- **Decider:** Saber (owner), Q15: wire the check protection, do not delete it
- **Related:** [ADR-0053](0053-acceptance-clause-review-gate.md) (acceptance
  clause review), [ADR-0055](0055-review-evidence-guarantee.md) (engine-verified
  review evidence)

## 1. Context

The engine already had three deterministic checks in `gate_scope.py`: protected
gate definitions loaded from the base commit, write scope against a lane's
declared area, and test-retirement detection. They ran only inside
`EngineGateExecutor.execute`, reached only through
`Orchestrator.run_engine_gate`. Nothing in `src/` called that seam; only two
test files did. The real job check `_run_configured_gate` ran the candidate's
own check files in a snapshot and detected only mutation during the run. The
acceptance `GateRunner` had no protection at all.

A rig reproduction on the base commit confirmed the fault: a candidate that
edited `conftest.py` to skip its failing test passed both the job gate and
acceptance, and its own `conftest.py` executed. Q15 decided to wire the
protection rather than delete it, because a check list that excludes self-
tampering is not a check.

## 2. Decision

1. **One execution path.** `EngineGateExecutor`, `EngineGateRequest`,
   `EngineGateResult`, the module-level `run_engine_gate` seam and
   `Orchestrator.run_engine_gate` are removed. They were reachable only from
   tests. The real paths are now the job check (`_run_configured_gate`) and
   acceptance (`GateRunner`).
2. **The job check verifies before it runs.** `_run_configured_gate` receives
   the candidate's trusted base revision and calls
   `verify_gate_definitions`. Every production caller passes it: the main,
   repair and backlog-descendant paths in `orchestrator.py`, `refresh.py` and
   the native-worker finish path. An unreadable base fails closed with
   `GATE_PROTECTION_BLOCKED: GATE_BASE_UNVERIFIABLE`. A changed protected
   definition returns a failed `ProcessResult` whose stderr carries the reason
   and the owner question. The gate command never launches.
3. **Acceptance verifies before it runs.** `GateRunnerRequest` carries
   `base_sha`, `changed_files` and the lane `touches` (declaration evidence
   only). `GateRunner.run` calls the same `verify_gate_definitions` before any
   backend choice, scratch creation or subprocess. A block raises
   `GateProtectionBlockedError` (a typed
   `GateRunnerInfrastructureError`), so the existing single post-CAS failure
   boundary rolls the target back to the recorded base and records the owner
   question. The candidate's own definition never runs. A successful receipt
   records the protection verdict under `gate_protection`.
4. **The block is an owner question, never a self-authorization.** The lane's
   declared `Touches` is recorded in `declared_paths` and reported to the
   owner; it never allows the edit. Authorization requires the owner to accept
   the definition change into the base SHA.
5. **Review evidence, not new blocks.** `build_review_evidence` now always
   includes engine-computed evidence for:
   - write scope against the lane's declared `owns` (an empty list or a legacy
     plan reports "no write scope" and never blocks);
   - test retirement (deleted tests, rewritten bodies, new skip/xfail markers);
   - new suppression comments (`# noqa`, `# type: ignore`, lint disables);
   - new catch-all exception handlers added by the candidate.
   Ordinary fixes that edit tests therefore remain reviewable work; the
   reviewer sees the facts and decides. Only changed gate definitions block.
   *(H41 / [ADR-0097](0097-gate-config-files-and-switch-offs.md) later made an
   added suppression or skip directive an owner question rather than evidence
   only; the evidence is still produced for the reviewer.)*
6. **The benchmark has a must-fail row for this.** The fake-mode rig adds a
   `gate protection: weakened conftest` drill: a fake worker commits a
   `conftest.py` that would run and skip the tests; the job must fail with
   `GATE_PROTECTED_PATH_MUTATED`, no acceptance, and no execution of the
   candidate's file.

## 3. Consequences

- A candidate can no longer change the scripts, `conftest.py`,
  `pyproject.toml` or CI workflow that decide its own verdict, on either the
  job path or acceptance. Both paths compare bytes with the base revision,
  including uncommitted and untracked definitions.
- The old engine-executor API disappears with its tests; the retained gate
  slot pool, disk admission and history guards keep their tests.
- Write scope and test retirement inform the reviewer instead of failing a
  candidate. This preserves ordinary test fixes while making weakening
  visible in the review evidence.
- An owner who genuinely wants a gate-definition change must land it in the
  base first; the next candidate is then judged under the new definitions.
