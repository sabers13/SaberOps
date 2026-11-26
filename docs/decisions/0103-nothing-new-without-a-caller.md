# ADR-0103: Nothing new without a caller, and the reviewer is told

- **Status:** Accepted. Implemented in phase 1.1 lane H43.
- **Date:** 2026-11-26
- **Decider:** Saber (owner): "is saberops act like a software engineer now? how
  can we prevent code slop in saberops is it a good idea to add a instruction to
  prevent worker and also add to reviewer to remove it and optimize each
  workers code?" (2026-11-26), followed by "yes add H43 and H46, start them now".
  Applied to lane H43 as firstmate proposed it: the engine lists new code
  nothing calls and passes it to the reviewer, the reviewer gets a rule for
  unneeded code, and workers get one new instruction line.
- **Related:** [ADR-0055](0055-review-evidence-guarantee.md) (engine-verified
  review evidence), [ADR-0060](0060-gate-protection-on-the-real-paths.md) and
  [ADR-0097](0097-gate-config-files-and-switch-offs.md) (the added-line
  evidence path this reuses), [ADR-0094](0094-second-dead-code-pass.md) (the
  no-caller scan and its exclusion classes), the phase 1.1 plan lane H43, and
  `data/so-gatemeasure/report.md` §3.1 and §4.

## 1. Context

A measurement of the 46 worker commits that landed phase 1.1
(`data/so-gatemeasure/report.md`) found exactly one anti-slop rule with a
defensible signal: **a newly introduced production definition that nothing
calls**. Five such definitions were introduced; four were later deleted as dead
by H31 and the H22 follow-up, and the survivor is a test-only drill API
(`storage_cleanup.Inventory.by_class`). Applied as a blocking rule it would have
changed the verdict on 3 of 46 commits, all true positives.

Every other candidate was measured and rejected as noise: extra ruff families
(306 findings on added lines, 1 true), vulture (3/31), deptry (0/13 without a
first-party map), aislop, AI-SLOP-Detector and the near-duplicate/parameter/
comment rules. The missing discipline is not another linter. It is a cheap
caller search plus a reviewer that says so.

The reviewer cannot run that search itself: it reads a fixed snapshot. The
engine already builds trusted evidence from the candidate tree
(`build_review_evidence`, ADR-0055) and already scans added lines for
suppression directives and deleted assertions (ADR-0097). This lane adds one
list to that block.

## 2. Decision

1. **The engine computes the list.** `src/saberops/caller_evidence.py` parses
   every Python definition the candidate added under `src/`/`scripts/` (a
   definition counts only when its `def`/`class` line is in an added line
   range, so an unchanged neighbour in a touched file is not new), then runs
   `git grep -n -I -w` for its name over code files (`.py`, `.pyi`, `.html`,
   `.js`, `.mjs`, `.json`; prose and Markdown are not callers). A definition is
   **uncalled** when nothing outside `tests/` and outside its own definition
   line refers to it. A definition referenced only from `tests/` is reported
   **separately**, never as uncalled.
2. **Moves and renames are not new.** A move keeps its name, so the caller
   search finds the moved callers and the definition is simply called. A
   rename keeps its new name and the same applies. The test-only channel drops
   a move whose old location was live production code (a relocation, not a new
   test-only addition); a move of code that was already dead stays visible
   because the candidate still adds a definition nothing calls. This is what
   makes the H18 merge invisible while H24's `_routing_digest` (a live move
   whose only caller was deleted in the same commit) and H25's dead relocations
   are reported.
3. **Dynamic registration is excluded**, exactly as in H31: web routes and
   HTTP handler methods, MCP tool registrations, CLI commands, pytest fixtures,
   validators, properties and other registering decorators are called by a
   framework, not by a textual reference. Methods of a genuinely new class are
   not reported separately; the class itself is the addition.
4. **The list is bounded.** At most 20 uncalled and 10 test-only entries, each
   as `path:line qualname` plus the test reference locations for the test-only
   channel. The block is appended to the existing added-line evidence section,
   so it travels through the same `build_review_evidence` path as deleted
   assertions. No second evidence mechanism, no reviewer edit.
5. **The reviewer gets the criterion.** `FINDINGS_PROMPT` now says an addition
   with no production caller is a **B1 unneeded addition**: cite the task's
   smallest-complete-change rule in `contract_reference`, quote the engine's
   caller search in `evidence`, and name the existing alternative (helper,
   standard library or installed dependency) in `minimum_correction`. Use
   **N1** instead when it is a thin wrapper, alias, test-only drill API or a
   documented serialization contract. Findings go back to the same worker, who
   performs any deletion; the reviewer stays read-only.
6. **The worker gets one line.** Worker instruction 7 is: "Add nothing without
   a caller: no unused function, parameter, option or alias; reuse the existing
   helper and name it in your report." It fits the existing 16-line cap.

## 3. Consequences

- A candidate that adds a dead function, class, parameter, option or alias now
  reaches the reviewer with the engine's own search attached, instead of
  relying on the reviewer's reading of the diff.
- The rule is evidence, not a block: an ordinary fix that adds a helper called
  from production is silent. A new definition whose only consumer is a test is
  shown as test-only, so a drill API is a visible N1 rather than a surprise
  dead-code sweep later.
- The scan is deliberately name-based, like H31. A reference reached only
  through `getattr`, a string, or an external package is not visible; the
  reviewer sees the matched locations and decides.
- A pure file rename produces no added lines and therefore no candidate, which
  is correct: the definitions moved, they were not introduced.
- The line-based candidate rule keeps the scan off unchanged definitions in a
  touched file, so a large refactor reports only what it actually added.
- No new lint tool, rule family, dependency, suppression allowlist or reviewer
  edit loop is introduced. Suppression directives remain H41's owner question.

## 4. Validation

- `tests/test_h43_uncalled_definitions.py`: the replay of H25's `c01335d`
  (uncalled `GcPreview.candidate_for`, `_null_audit`; test-only
  `Inventory.by_class`), H24's `89916b02` (uncalled `_routing_digest`) and the
  H18 merge `b37047f` (no uncalled entry, and none of its four moves in the
  test-only list); one case per exclusion class; live and dead moves; the
  test-only channel; the bound; the worker line and reviewer criterion; and the
  evidence block reaching `build_review_evidence`.
- Full gate green on the lane branch.
