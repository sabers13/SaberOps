# ADR-0065: Review is mandatory, focused, and reads the candidate snapshot

- **Status:** Accepted. Phase 1.1 lane H4.
- **Date:** 2026-11-23
- **Decider:** Saber (owner), phase 1.1 decision Q4
- **Related:** [ADR-0053](0053-acceptance-clause-review-gate.md) (review contract),
  [ADR-0055](0055-review-evidence-guarantee.md) (engine-verified evidence),
  [ADR-0057](0057-clause-format-retry-and-gate-execution.md) (format retry and
  real gate), ADR-0008 (candidate-bound acceptance)

## 1. Context

Phase 1.1 H0 traced a scheduler-dispatched job at runtime (`data/so-h0/report.md`
§5). `jobs/service.py:job_dispatch_spec` built a `RunRequest` with no review
field, and no production entry point passed `autonomous=`; a LOW docs-only
candidate under the default `RISK_ADAPTIVE` policy was accepted with
`review_skipped_by_risk_adaptive_policy` and no review rows. The confirming
benchmark had hidden this by setting `ALWAYS_REQUIRED` on every project.

The same review surface carried a large deterministic keyword classifier
(`review_adaptive.classify_change`), a project review-policy file, workflow
presets, and eight owner knobs. The reviewer also could not read the candidate
tree (prompt: "NO filesystem access"), received an uncapped diff, and a prompt
above the transport limit skipped the reviewer instead of blocking.

## 2. Decision

1. **Review is mandatory for every candidate that can be accepted.** The
   scheduler projection states `review_enabled=True`, run creation freezes the
   guarantee, and the candidate-ready decision is always
   `independent_review_required=True`. Acceptance still requires a review bound
   to the current candidate, and now also requires review when a build run has
   no usable decision row at all (fail closed). A scout never produces an
   acceptability candidate.
2. **One owner switch, manual only.** `orch run --no-review` freezes
   `review_disabled` on that run and is the only recorded review exception. No
   scheduler job, plan lane, web form or MCP submission can populate it.
3. **The classifier and its knobs are deleted.** `classify_change`,
   `decide_review`'s precedence, `ReviewPolicyMode`, the project
   `review-policy.json`, the project review mode/limit settings, the workflow
   presets and the per-run `--review-policy` / `--review-mode` /
   `--review-limit` / `--review` flags are gone. The gate switch survives as
   `orch project set-gate-state`. A stored `review-policy.json` or old review
   fields in `project-settings.json` are ignored with one notice.
4. **The planner may add focus, never disable review.** `submit_job`, plan
   lanes and `orch run` accept `review_focus`; it is frozen on the run and
   placed in the reviewer prompt as additional attention.
5. **The reviewer reads the candidate snapshot.** The reviewer runs in the
   existing fixed snapshot of the exact candidate commit; the prompt now says
   so and allows reading files (callers, omitted files). Write protection is
   per adapter: Claude runs `--permission-mode plan` with a Read/Glob/Grep
   allowlist, Codex uses `--sandbox read-only`, Antigravity uses `--mode plan`.
   The OpenCode CLI exposes no such switch; the engine's existing snapshot
   tree comparison detects any write and records a BLOCK. A write attempt is
   therefore either prevented or detected, never a passing review.
6. **The diff is bounded, the manifest is complete.** The diff was the one
   unbounded section of the prompt; every other section is already bounded
   (engine evidence, prior finding, worker report, focus). The changed-path
   manifest always lists every changed file. The diff body stops at a file
   boundary inside `REVIEW_DIFF_BUDGET_BYTES` (60 KB); every omitted file is
   marked in the manifest and the prompt tells the reviewer to read it from
   the snapshot. The omission is recorded as `review_diff_truncated`. A
   failed `git diff`/manifest read is unavailable evidence and blocks the
   review instead of reviewing an empty prompt.
7. **The worker's own report reaches the reviewer.** The candidate attempt's
   final output is included, bounded, as a labelled claim ("skipped, not
   checked, risks"), separate from engine-verified evidence. H12 defines the
   exact worker instruction text.
8. **The review contract is unchanged.** B1-B3, D1 and N1 dispositions, the
   findings protocol, reviewer selection and the evidence rules of ADR-0053,
   ADR-0055 and ADR-0057 stay as they are; no second severity axis is added.

## 3. Consequences

- A scheduler job under the default policy can no longer be accepted
  unreviewed. A missing candidate-linked review blocks acceptance.
- The keyword classifier no longer decides anything. Review happens for docs
  changes too; the cost is bounded by the existing review limit.
- A large candidate is reviewed against a complete manifest and a readable
  snapshot rather than a truncated prompt, and an omitted file cannot hide a
  blocking change: the reviewer is told to read it, and a reviewer that
  cannot verify it must block.
- Legacy persisted decisions are read conservatively: a pre-1.1 classifier
  skip no longer parses, so it falls back to review-required. Older stores
  keep working; their review knobs print one notice and are ignored.
- No authority is widened: review cannot be disabled by a planner, a project
  setting or a worker report.
