# ADR-0084: Modes stop at consequential candidates

- **Status:** Accepted. Implemented by Phase 1.1 lane H20.
- **Date:** 2026-11-23
- **Decider:** Saber (owner). Phase 1.1 plan, owner's words 2026-11-22,
  recorded answers Q1-Q15
- **Related:** [ADR-0008](0008-candidate-bound-acceptance.md) (auto-accept),
  [ADR-0020](0020-owner-mode.md) (the mode narrows authority),
  [ADR-0027](0027-critical-only-questions.md) (never-delegable questions),
  [ADR-0047](0047-expired-away-mode-narrows-to-ask-me.md) (expiry),
  [ADR-0060](0060-gate-protection-on-the-real-paths.md) (protected check
  files),
  [ADR-0078](0078-one-question-record.md) (one self-answer record, with its
  cost if wrong),
  [ADR-0082](0082-owner-mode-carries-authority.md) (the mode ceiling)

## 1. Context

The owner's intent, verbatim:

> i feel like some features are over engineered here and some should just
> simpely hand to orchestrator and get a better result and simpler codebase

The mode lane's intent in the phase 1.1 plan: ask-me is manual; critical-only
stops at critical decisions; away stops only at critical steps, such as editing
a database or any unrecoverable, consequential step.

Before this lane, away and critical-only parked a fixed never-delegable list
(ADR-0027), but only when a *question* was raised with one of those kinds. A
green, independently reviewed candidate was auto-accepted however
consequential its change was: a candidate that added a data migration, rewrote
the schema or deleted a large part of the tree landed as a local commit during
`away`. The fixed list was real, but no candidate ever reached it.

## 2. Decision

1. **The planner can mark a candidate consequential.** `submit_job` accepts
   `requirements.consequential: true`, and a `PLAN.md` lane machine block
   accepts `"consequential": true`. The marker is frozen on the run
   (`RunConfig.consequential`), travels through the job dispatch spec, and is
   the planner's judgment call: data migrations, deleting files or data, auth
   and security, irreversible operations, effects outside the work copy. A
   missing marker means ordinary, so nothing the planner omits can loosen a
   guarantee.
2. **The engine adds a fixed floor it checks itself.** `consequential_floor`
   reads the committed candidate diff (`base_commit..candidate`) and reports a
   hit when:
   - a changed path is under a `migration/` or `migrations/` directory, or is
     `alembic.ini` (an applied migration is not undone by reverting code); or
   - the candidate deletes more than 20 files (`_DELETED_FILES_FLOOR`), which
     is a mass deletion rather than a routine refactor.
   The floor is computed from the diff only, never read from the frozen
   configuration, so `consequential: false` cannot remove it. An unreadable
   diff is unverifiable and counts as a hit (fail closed: the owner is asked).
   The protected check paths ADR-0060 already blocks are deliberately not
   repeated here: H32 refuses those on both the real job check and acceptance,
   so a weakened `conftest.py`, gate script or `pyproject.toml` never produces
   an acceptable candidate in the first place.
3. **Either signal parks the candidate; independent work continues.** When
   every existing auto-accept precondition is green and the candidate is
   consequential, `evaluate_auto_accept` refuses it with
   `park_for_owner=True` and the joined reason. `auto_accept_candidate` writes
   one durable `auto_accept_parked` event per candidate and sends one notice
   ("parked a consequential candidate", with the reason and the accept
   command). The scheduler's auto-accept pass then continues with other
   completed runs, so a parked candidate never stalls unrelated work. The
   candidate stays unaccepted; `orch accept <run-id>` remains the owner's
   command, and the return summary's `awaiting_acceptance` section already
   lists it.
4. **Nothing widens.** Ask-me is unchanged (every accept is manual, and the
   mode gate short-circuits before the floor). The floor can only add a park;
   the planner's marker can only add one. Auto-accept still never pushes,
   publishes or opens a pull request, and the parked candidate goes through no
   path other than the mode gate that already refused it.
5. **Self-answered decisions keep their cost.** The record ADR-0078 kept
   already carries `cost_if_wrong`, and the return summary already renders it;
   this lane adds no second log. The modes skill (H12) uses this ADR's criteria
   list and this park behaviour as its contract.

## 3. Why the floor is mechanical and the rest is judgment

The phase's rule is "LLMs advise; deterministic software decides", applied to
*enforcement*, not to judgment. A diff predicate can prove "this path is under
a migration directory" or "this candidate deleted 25 files". It cannot decide
"this change is auth" or "this effect is outside the work copy" without
keyword guessing, which is exactly the class of guesser this phase removes.
Those stay with the planner, who sees the goal and the candidate; the floor is
the backstop for what the planner forgets.

## 4. Alternatives considered

- **Delete the never-delegable list and let away accept everything.** Rejected:
  it would weaken owner acceptance and contradict Q15's direction (wire
  protection, do not delete it).
- **A keyword classifier for auth/security paths.** Rejected as a deterministic
  guesser; the planner marks those, and the engine's floor stays provable.
- **Carry the marker only in the run config and not on jobs or plans.**
  Rejected: the planner submits through `submit_job` and PLAN.md lanes, and a
  marker that cannot reach the run from those entries does nothing.
- **Park by refusing the whole auto-accept pass.** Rejected: one consequential
  candidate would stop every other candidate; the plan requires independent
  work to continue.

## 5. Consequences

- A migration candidate in away mode now parks and notifies instead of landing
  as a local commit; an ordinary green candidate still auto-accepts.
- The owner sees one notice and one durable event per parked candidate; the
  candidate remains on its branch for a manual `orch accept`.
- The planner's marker and the engine's floor are additive; neither can remove
  the other, and neither grants any permission.
- The floor constants are code, so widening or narrowing it is a code change
  with a test, not an owner setting.

## 6. Verification

`tests/test_h20_consequential_candidates.py` (fake CLIs and a toy repository):

- a migration candidate parks and notifies in away mode, the target branch does
  not move, and the event names the reason;
- an ordinary green candidate still auto-accepts;
- a planner-marked candidate parks without a floor hit;
- a recorded `consequential: false` cannot remove a migration floor hit;
- one deleted file is ordinary, a mass deletion is a floor hit;
- `submit_job` carries the marker to the job, the dispatch spec and the frozen
  run configuration, and a pre-H20 stored job reads as ordinary;
- a PLAN.md lane carries the marker through the plan store.

The pre-H20 reproduction auto-accepted the migration candidate
(`decision.allowed=True`); after this lane the same fixture is refused and
parked.
