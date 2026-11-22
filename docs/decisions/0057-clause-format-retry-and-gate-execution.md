# ADR-0057: Clause answers tolerate a status token; one bounded format re-ask; the toy gate executes tests

- **Status:** Accepted. Phase 1 confirming-run-3 fix lane.
- **Date:** 2026-11-22
- **Decider:** Saber (owner), standing order for the confirming real-run fixes
- **Related:** [ADR-0053](0053-acceptance-clause-review-gate.md) (per-clause
  PASS gate), [ADR-0055](0055-review-evidence-guarantee.md) (engine-verified
  review evidence)

## 1. Context

The third confirming run
(`/home/saber/fm-test/firstmate/data/so-confirm3/report.md`, D1/D2) confirmed
the must-fail rows, the structured no-diff re-review, the `python3` gate and
the notice backoff, but still lost three honest candidates:

- Codex `fix-failing-test` (and one Codex `rename` round) returned an overall
  `VERDICT: PASS` with a clause line in prose:
  `CLAUSE 1: The failing test passes. — …`. The deterministic parser accepted
  only `CLAUSE <n>: PASS|FAIL`, recorded "missing answers for clause 1", and
  turned the honest PASS into a BLOCK.
- OpenCode `fix-failing-test` and `cli-flag` were blocked because the engine
  evidence block was labelled "Gate and test output" while the only command
  run was `<python> -B -c 'import app'`. Both reviewers correctly said they
  could not verify "the failing test passes" / "the CLI accepts `--name`".

Neither loss was a wrong candidate. Both came from the review contract being
narrower than the guarantee it stated.

## 2. Decision

1. **A status token is accepted anywhere on the clause line after the
   marker.** After `CLAUSE <n>:` the parser reads an uppercase `PASS` or `FAIL`
   token in the remaining text and still requires non-empty evidence. A line
   with both tokens is contradictory and fails closed. Lowercase English prose
   such as "the test passes" is not a status token.
2. **A missing or unparseable answer earns one same-reviewer format re-ask.**
   Only when the reviewer's overall verdict is `PASS`/`PASS_WITH_BACKLOG` and
   the coverage result has missing answers and no failed answer, the engine
   asks that same reviewer once for the exact one-line form and records
   `review_clause_format_retry`. The re-asked answer is appended and the
   unchanged coverage check is applied to the combined output.
3. **The re-ask cannot approve anything.** A `FAIL` token, a contradiction, or
   a second missing answer still maps the verdict to `BLOCK` (or
   `FINAL_BLOCK`). The re-ask is capped at one per review, uses the same
   reviewer/model and candidate snapshot, and is recorded with its own dispatch
   evidence and usage.
4. **The benchmark's toy gate exercises the acceptance.** Every toy task's
   project gate is `<interpreter> -B -m pytest -q`; the `cli-flag` gate also
   invokes the built CLI with `--name Ada`. Both are bounded by the existing
   project gate timeout (300s) and the evidence block's character limit. The
   evidence label now says "Gate command output (engine-run; exact command
   below)" so it names the command actually run instead of promising an
   unspecified test run.
5. **A product-level verification command is out of scope here.** A separate
   project acceptance command (distinct from the build gate) would be a
   product change with its own policy; this ADR only makes the benchmark rig's
   gate meaningful and does not add such a command.

## 3. Consequences

- The recorded Claude Haiku `fix-failing-test` prose now gets one format
  re-ask; if the reviewer repeats the prose it still blocks. The recorded
  `rename` answer (`CLAUSE 1: PASS — …`) is accepted directly.
- The reviewer can verify behavioural clauses from real pytest/CLI output
  produced by the engine, while source inspection remains available.
- The guard is not weakened: the re-ask is blind to the candidate, cannot
  accept a `FAIL`, and adds at most one bounded review call on malformed
  output.
- `review_acceptance_clause_block` now records whether a format re-ask was
  used, so the owner can tell a wording problem from a substantive block.
