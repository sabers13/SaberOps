# ADR-0111: A corrected candidate is acceptable

- **Status:** Accepted. Implemented in phase 1.1 lane H53.
- **Date:** 2026-11-28
- **Decider:** Saber (owner), phase 1.1 plan lane H53, from the H37 recheck on
  2026-11-28 (N1: "a corrected candidate cannot be accepted").
- **Related:** [ADR-0068](0068-one-recovery-tool.md)
  (one recovery tool, `resume_job`, from scratch or the saved candidate),
  [ADR-0105](0105-resumed-job-completes-and-scheduler-continues.md)
  (resumed jobs complete), [ADR-0108](0108-owner-reject-with-a-note.md)
  (the reject-with-a-note flow), [ADR-0008](0008-candidate-bound-acceptance.md)
  (candidate-bound acceptance), [ADR-0013](0013-quota-failover-continuation.md)
  (continuation start anchors).

## 1. Context

`orch accept <run>` refused every candidate produced by a continuation.  A
failed job resumed with `resume_job from=last_candidate`, or a succeeded job
whose candidate the owner rejected with a note, starts its next attempt at the
parent run's saved candidate.  The successor run's own `base_commit` is the
repository HEAD at continuation time, which is the *original* base, not the
saved candidate.  Acceptance's provenance check therefore compared the
attempt's real base (the saved candidate) against the successor run's base and
failed with:

    Candidate attempt '<run>_a1' was based on <candidate>, not the authorized
    base <original> of run '<run>'

after gate PASS and review PASS on all three CLIs.  Only a fresh single-attempt
run could be accepted.  The check was right to demand a proven base; it lacked
the durable record that proves a continuation's base.

## 2. Decision

1. **Prove the continuation lineage; do not skip the base check.**
   `continuation_authorized_bases` reads the frozen `continuation` reference
   from the successor's run configuration and authorizes exactly its saved
   candidate as an additional acceptable base.  Every link must re-verify from
   durable state: the successor's own base equals the reference base, the
   parent run records that base, both runs belong to the same durable job and
   repository, the referenced attempt belongs to the parent and has exactly
   the referenced candidate commit, and that candidate descends from the base
   in the repository.
2. **One shared proof for every candidate gate.**  `execution_authorized_bases`
   (publication, acceptance and refresh) unions the continuation bases with the
   existing failover start anchors, so a continuation candidate is published,
   refreshed and accepted through the same evidence instead of through a
   special case on one path.
3. **Unrelated bases stay refused.**  The assertion remains exact membership:
   an attempt whose base is not the run base, a verified lineage candidate, or
   a verified failover anchor is still rejected.  A tampered or incomplete
   continuation reference contributes nothing.
4. **Missing a base is not a loophole.**  The fallback for rows without any
   provenance is unchanged; a provenanced run still never accepts an attempt
   that lacks provenance.

## 3. Consequences

- `resume_job from=last_candidate`, `resume_job from=scratch` and the
  reject-with-a-note flow can all reach acceptance; the target fast-forwards
  to the corrected candidate.
- The lineage is re-derived on every check, so a later state change (a moved
  base, a missing candidate commit, a different job) revokes the authorization
  instead of trusting a one-time decision.
- `orch accept` continues to exit non-zero when integration fails: the
  post-integration gate failure path rolls the target back and raises, and the
  CLI reports `Integration failed` with exit code 1 (N9).
- Chained continuations work without a special case: each successor's
  reference names its immediate parent, and the ancestry check ties the
  candidate to the original base.
