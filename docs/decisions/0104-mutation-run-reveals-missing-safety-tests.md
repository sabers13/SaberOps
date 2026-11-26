# ADR-0104: The mutation run's safety gaps become kept tests

- **Status:** Accepted. Implemented in phase 1.1 lane H46.
- **Date:** 2026-11-26
- **Decider:** Saber (owner): "add the missing safety tests the mutation run
  found, plus a mutation rerun" (2026-11-26 lane H46 approval).
- **Related:** `data/so-gatemeasure/report.md` §1, §5 and Appendix C (the
  measured gap list, equivalent-mutant classes and reproduction commands);
  phase 1.1 plan lane H46; `data/so-h46/notes.md` (the rerun numbers and the
  caller/line inventory).

## 1. Context

The anti-slop scout measured the safety modules with cosmic-ray 8.9.0. After
removing mutants on type-annotation lines and mutants that keep the same
behaviour, about 40 real test gaps remained, in places no lint, type check or
review found: the review-evidence validators, the gate failure paths, the
owner-mode parsers, the away preflight journal and the doctor's repair loop.
The kept suite asserted the happy path and stopped at the first wrong answer,
so a boundary or inverted-branch defect could change a safety decision without
turning a test red.

The gaps are not style or message wording. Each one is a branch that decides
whether evidence is accepted, whether a gate refuses, whether an away run may
spend budget, who may answer a question, or whether a repair is reported as
applied.

## 2. Decision

1. **One focused test per real gap, beside the existing safety tests.** Tests
   are added to `tests/test_review_findings.py`, `tests/test_gate_scope.py`,
   `tests/test_owner_mode.py`, `tests/test_away_policy.py`,
   `tests/test_f11_away.py` and `tests/test_doctor_h39.py`, so the same test
   selection mutation testing runs already includes them. No new test runner,
   fixture package or gate step is introduced.
2. **Each test states the boundary it protects.** A boundary test uses the
   exact edge value (the timestamp equal to `set_at`, exactly `MAX_SCOPES`,
   exactly the critical idle multiple, 100 % spend, a blank instead of a missing
   citation) or drives the one branch the survivor disabled (a raising repair,
   a duplicate check id, a non-blocking first question before a blocking one).
3. **Equivalent mutants are named, not chased.** Mutants on annotation lines,
   enum `is` vs `==`, message-only text, `0/1` return-code ordering and the
   ask-me ceiling that narrows every widened legacy bit back to false cannot be
   killed by a behavioural test; the notes list them as equivalents.
4. **The rerun is evidence, not a gate.** cosmic-ray is installed only into the
   lane's worktree `.venv`, is never added to `pyproject.toml`, and does not
   become a CI or merge check. Session databases live in the worktree and are
   deleted after their numbers are recorded.
5. **A real defect the new test exposes is fixed in this lane with that test.**
   No safety behaviour is relaxed to make a mutant killable.

## 3. Consequences

- The safety properties that the mutation run found unprotected now have a
  failing test when the branch is weakened. The change is test-only unless a
  test exposes a real defect (recorded in the lane notes).
- The suite grows by focused, fast unit tests at the boundaries the run
  identified; it does not restate message wording.
- Mutation testing stays a periodic maintenance check, as the scout
  recommended: rerun it after a safety change or before a release and read the
  survivor list, instead of paying its runtime on every commit.
- The recorded size budget (`scripts/size_budget.json`, ADR-0095) moves up with
  the added tests and carries a one-line reason; the budget remains a measured
  record, not a target.
- `auto_accept.py` remains unmeasured for the acceptance path while the host is
  below the gate disk floor; this lane does not pretend otherwise.
