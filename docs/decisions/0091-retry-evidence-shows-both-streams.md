# ADR-0091: A retry shows both gate streams

- **Status:** Accepted. Implemented in phase 1.1 lane H33.
- **Date:** 2026-11-24
- **Decider:** Saber (owner), phase 1.1 plan (2026-11-22, "maximum deletion"
  and the retry-evidence row of "What the code shows")
- **Related:** [ADR-0005](0005-mcp-first-engine-and-phase-2-board.md) (the
  planning CLI is the orchestrator), [ADR-0083](0083-splitting-is-the-planners-job.md)
  (the ordinary build chain is one job), [ADR-0088](0088-the-scope-guessers-are-gone.md)
  (the prompt carries judgment from the planner, not engine guesses)

## 1. Context

`build_retry_projection` in `src/saberops/context_projection.py` built the
failure evidence for an ordinary retry after a failed check. It read the
recorded check's `stderr` first and used `elif` for `stdout`, so whenever
stderr held any text at all the stdout block was dropped.

Check tools write warnings to stderr routinely — a missing coverage threshold,
a deprecation notice, a git or environment warning — while pytest writes the
failure summary ("FAILED tests/...::test_...", the traceback, the
`1 failed, 2 passed` line) to stdout. A single warning therefore hid the only
evidence that said which test failed, and the next attempt re-read the code to
rediscover it. The engine kept both streams in the database
(`CheckResult.stdout` / `CheckResult.stderr`); only the projection discarded
one.

The plan's diagnosis was confirmed by fixture before any change: a check script
that warns on stderr and runs a failing pytest produced a retry projection
carrying `Gate stderr excerpt:` and no `Gate stdout excerpt:` at all
(`/var/tmp/h33-before`, source at `HEAD` before this lane).

## 2. Decision

1. **Both streams are projected, each labelled.** The retry evidence carries
   `Gate stderr excerpt:` and `Gate stdout excerpt:` when the stream has text.
   An absent or whitespace-only stream produces no block, as before.
2. **The present streams share one evidence block.** Each stream is pruned head
   and tail. With both streams present each gets `EVIDENCE_BLOCK_MAX_CHARS // 2`
   (~1 KB); a lone stream keeps the whole `EVIDENCE_BLOCK_MAX_CHARS` block,
   exactly as before this lane. `prune_evidence` still inserts the truncation
   marker and the builder still appends the `[truncated]` notice per stream.
   Measured worst case: 4,197 characters of failure excerpt for one long
   stream before this lane, 4,249 with both long streams after it — the same
   4,096 characters of kept stream text plus a second label, marker and
   notice.
3. **Order and shape are unchanged.** The gate command and exit code line come
   first, then the streams in the previous order (stderr, then stdout), then a
   `[truncated]` notice where the prune cut text. The rest of the projection —
   the bounded objective digest, inherited candidate, responsibility and
   validation policy — is untouched.
4. **Out of scope and untouched:** the acceptance receipt format,
   `SurgicalCapsule` and the surgical repair path (H31), the continuation
   projection, and `prune_evidence`'s own contract. No safety property is
   involved: this changes only which recorded evidence a retry shows.

## 3. Consequences

- A retry after a check that both warns and fails now shows the warning and the
  failing test summary, so the worker fixes the failure instead of
  rediscovering it. When both streams are long the prompt grows by 52 bytes
  (a second label, marker and notice) because each stream keeps half the
  block; a lone stream's evidence is unchanged in size.
- The unused constants `STDERR_BLOCK_MAX_CHARS` and `STDOUT_BLOCK_MAX_CHARS`
  stay as they are; this lane does not revive them or delete them (a later
  dead-code pass may, if nothing else adopts them).
- Measured reduction: none. The lane replaces an `if`/`elif` with a shared
  budget over the present streams and adds `tests/test_h33_retry_evidence.py`
  (~230 lines including the fixture).
- Deliberately left alone: `SurgicalCapsule` still takes optional
  `bounded_stdout_excerpt` / `bounded_stderr_excerpt` fields (H31 owns that
  surface); the continuation projection's single `failure_excerpt` from the
  run result summary is a different evidence path and was not widened.

## 4. References

- Phase 1.1 plan, lane H33 (`phase-1.1-core-plan.md`), "What the code shows"
  retry-evidence row
- `src/saberops/context_projection.py` (`build_retry_projection`,
  `prune_evidence`, `EVIDENCE_BLOCK_MAX_CHARS`)
- `src/saberops/orchestrator.py` (`_run_core` retry dispatch,
  `_run_configured_gate`)
- `src/saberops/db.py` (check rows keep both streams)
- `tests/test_h33_retry_evidence.py`
