# ADR-0059: Structured finding ingest repairs duplicates and preserves incomplete findings

- **Status:** Accepted. Phase 1 confirming-run-4 cleanup lane.
- **Date:** 2026-11-22
- **Decider:** Saber (owner), standing order for the confirming real-run fixes
- **Related:** [ADR-0053](0053-acceptance-clause-review-gate.md) (per-clause
  reviews), [ADR-0055](0055-review-evidence-guarantee.md) (engine-verified
  review evidence)

## 1. Context

The fourth confirming run
(`/home/saber/fm-test/firstmate/data/so-confirm4/report.md`, D6) passed every
honest task and blocked every must-fail row, but two Codex-batch must-fail
reviews lost their durable finding detail while keeping the correct BLOCK:

- one reviewer emitted two findings both labelled `B1`, and the strict parser
  discarded the whole block with `duplicate finding id: B1`;
- one finding omitted `repair_scope`, and the strict parser discarded the block
  with `finding #0 missing required field: repair_scope`.

The all-or-nothing ingest was described as a guarantee in the policy module:
every finding must parse or nothing is trusted. The BLOCK stood in both cases
because the line verdict is combined with the computed disposition, but the
owner could no longer see what the reviewer found.

## 2. Decision

1. **Duplicate finding ids are renumbered deterministically on ingest.** The
   first occurrence keeps its id; later occurrences become `B1-2`, `B1-3`, and
   so on, skipping ids already seen. Both findings are stored and both take part
   in the computed disposition.
2. **A finding missing a required field is preserved as unstructured text.**
   The review detail records the reviewer's finding verbatim plus the validation
   reason (for example `missing required field: repair_scope`). It is not a
   structured finding and does not enter the computed disposition.
3. **A missing field is never invented.** In particular, no default
   `repair_scope` is supplied: guessing one could widen what repair may do.
   Findings with other protocol defects (unknown classification, invalid
   severity, non-object entries, failed evidence validation) still reject the
   whole block exactly as before.
4. **Verdict strictness is unchanged.** The result remains the stricter of the
   reviewer's explicit `VERDICT:` line and the disposition computed from the
   structured findings. The two recorded Codex-batch reviews replay as BLOCK,
   and a line verdict can never be relaxed by an ingest repair.

## 3. Consequences

- Duplicate ids and one missing field no longer erase the reviewer's finding
  detail; the durable review record keeps both what could be structured and
  what could not.
- The all-or-nothing sentence in the policy documentation now describes the
  two bounded ingest repairs instead of being silently contradicted by them.
- Authority does not widen: unstructured findings are evidence for the owner,
  not a basis for an automatic repair scope, and the block stands.
