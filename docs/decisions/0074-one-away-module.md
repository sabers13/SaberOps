# ADR-0074: One away module, one standing-decision vocabulary

- **Status:** Accepted. Implemented in phase 1.1 lane H18.
- **Date:** 2026-11-23
- **Decider:** Saber (owner), phase 1.1 owner answer Q14
- **Related:** [ADR-0028](0028-away-mode-standing-decisions.md) (away mode
  applies owner standing decisions and keeps a durable audit),
  [ADR-0020](0020-owner-mode.md) (one owner mode that only narrows)

## 1. Context

One concept lived in two modules. `saberops.unattended` (1,925 lines) held the
pure overnight policy: the frozen decision sheet of exact-scope standing
decisions, the immutable never-delegable list, the three autonomous decision
classes, the deterministic preflight, the circuit breaker, the stall alarm, the
plan-impact projection and the morning report. `saberops.away` (1,166 lines) held
the durability the scheduler actually needs: the standing-decision store, the
away journal, the away policy document, the return summary and the dispatch
gate. `away.py` did not merely call `unattended.py`; it was the only production
caller, and the split existed only because the policy arrived as a standalone
pure module (ADR-0028).

The split cost more than it saved:

- two module docstrings and two import blocks for one concept;
- two error roots (`UnattendedError`, `AwayError`) for one caller that catches
  and prints away failures;
- two ISO-8601 instant parsers with different messages and normalisation for
  the same durable bounds;
- and, once the two halves sit in one file, a name collision:
  `unattended.DecisionClass` (the autonomy class: gated accept, recommended,
  never-delegable) against `questions.DecisionClass` (the question class: push,
  billing, test change). One of them had to be named honestly.

## 2. Decision

1. **`saberops.away` is the one away module.** The policy half and the durable
   half live in that single module, in that order: constants, one error root,
   the policy, then the store, journal, return summary and dispatch gate.
   `saberops.unattended` is deleted; every importer (`cli.py`, `web.py`,
   `questions.py`, `capacity/loop.py`, tests) imports from `saberops.away`.
2. **One vocabulary.** The module keeps the standing-decision names
   (`DecisionScope`, `StandingDecision`, `DecisionSheet`, `NeverDelegableKind`
   and `NEVER_DELEGABLE_KINDS`, `Decision`, `DecisionResolution`, `RunProgress`,
   the preflight and the report). The autonomy enum is renamed to
   `AutonomyClass`, matching the `autonomy_class` field it already fills; the
   question-queue class keeps its own `DecisionClass` name in `questions.py`.
3. **One store, unchanged.** The store path (`unattended-decisions.json`,
   `ORCH_UNATTENDED_STORE`), the journal (`away-journal.jsonl`) and the policy
   document (`away-policy.json`) keep their names and formats, so an existing
   owner store is read as before and no migration or notice is needed. The
   never-delegable list is byte-for-byte unchanged.
4. **One error root.** `AwayError` is the single `ValueError` root;
   `DecisionSheetError`, `PreflightError` and `StallSupervisionError` subclass
   it. A caller that fails closed catches one type. `UnattendedError` is gone.
5. **One instant parser.** The policy's strict parser (length bound, mandatory
   timezone, explicit refusal of a naive instant) is the only one, and it
   normalises to UTC. The away document loader and the journal reader use it.
6. **The dependency stays one-directional.** `questions.py` imports
   `NEVER_DELEGABLE_KINDS` from `saberops.away`; `saberops.away` therefore takes
   its question-queue imports lazily (`TYPE_CHECKING` for annotations, a local
   import where a class object is needed) instead of at module import, so the
   merge introduces no import cycle.
7. **Nothing away may do changes.** The never-delegable list, the exact-scope
   match, the autonomous classes, the preflight blockers, the breaker, the
   alarm thresholds and the dispatch order are untouched. No test that guards a
   safety property is deleted; the policy drills move to
   `tests/test_away_policy.py` unchanged and the wiring drills stay in
   `tests/test_f11_away.py`.

## 3. Verification

- `tests/test_away_policy.py::test_one_away_module_holds_the_policy_and_its_store`
  fails on the pre-merge tree (the old module imports and `away` has no policy
  surface) and passes after it: the module is gone, the policy and the store are
  one import, the error root is one, and the never-delegable list is the seven
  frozen kinds in order.
- All 151 away, critical-only and auto-accept drills in
  `tests/test_away_policy.py` and `tests/test_f11_away.py` pass with unchanged
  assertions.
- Measured size: source 3,091 → 3,027 lines (`wc -l` of `unattended.py` +
  `away.py` before, `away.py` after), a reduction of 64 lines; the test files
  went 1,976 → 1,997 because the one-module guard test is new. The merge removes
  duplication, not behaviour, so the reduction is small by design.

## 4. Consequences

- An owner or reviewer reads one file to see what away may decide, where the
  answer is stored, what was journalled and what the return summary says.
- `import saberops.unattended` fails; there is no compatibility shim, because a
  shim would restore the second module this lane exists to remove. Nothing
  outside the repository imported it (the documented surface was the CLI and
  the scheduler).
- The 3K-line module is the largest single file in the engine. If it grows
  further, the next split should follow a real seam (policy versus durable
  wiring is now an internal section boundary), not a historical one.
