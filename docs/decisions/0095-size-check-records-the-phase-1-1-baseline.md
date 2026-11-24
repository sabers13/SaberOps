# ADR-0095: A local size budget records logic lines and suppression comments

- **Status:** Accepted. Implemented in phase 1.1 lane H34.
- **Date:** 2026-11-24
- **Decider:** Saber (owner): the 2026-11-22 answer ("do your suggestion and
  maximise the deletion i feel like there are a lot of spam code is in repo, add
  all of this to the new phase 1.1 plan") and the 2026-11-23 answer ("update the
  plan, make the plan in style of phase 1 parallel favor"), applied to lane H34
  ("size check").
- **Related:** phase 1.1 plan lane H34 and "Rules for every lane"; phase 1
  "Rules for every lane"; [ADR-0092](0092-test-suite-admits-without-the-host-guard.md)
  (check protection on the real path), [ADR-0093](0093-docstrings-and-comments-keep-contracts.md)
  (the docstring and comment diet), [ADR-0094](0094-second-dead-code-pass.md)
  (the second dead-code pass, the lane immediately before this one).

## 1. Context

Phase 1.1 removed whole features and their support code. H30 then removed
history from docstrings and comments, and H31 removed definitions nothing
called. The plan's exit test asks that the source shrink by a measured amount
and that every phase 1 drill stay green; H34 adds the missing guard against
quietly growing the core again.

The plan's rule is exact: record the post-phase-1.1 count of **logic lines**
(excluding docstrings, comments and blank lines) and of **suppression
comments**; a change that grows either past the recorded number fails unless it
updates the recorded number with a one-line reason. Comments have no limit. The
check lands locally, so it is not hosted CI, and it must run in under a second.

Every local gate (`gate-full.sh`, `gate-affected.sh`, `lane-check.sh`, and
`main-gate.sh` through the full gate) reaches one runner:
`Runner.run_checks` in `scripts/gate_tools.py`, which currently runs ruff, then
strict mypy, then pytest. `gate-ui-fast.sh` is presentation-only and runs no
Python check.

## 2. Decision

1. **One shared step, every local gate.** `scripts/size_budget.py --check` runs
   in `Runner.run_checks` before ruff, and its exit status joins ruff and mypy in
   `checks_ok`. No wrapper needs its own copy; hosted CI and the presentation
   wrapper are unchanged.
2. **String-and-comment stripping, stdlib only.** The measured tree is every
   first-party `*.py` under `src/`, `tests/` and `scripts/`. One regular
   expression in source order matches every string literal (single- and
   multi-line, including docstrings) and every comment; each match is replaced
   by its line breaks, and the remaining non-blank lines are the **logic
   lines**. So docstrings, every other string literal, comments and blank lines
   are free, and comments have no limit. A **suppression comment** is a
   comment match whose text starts with a check-disabling directive
   (`# noqa`, `# type: ignore`, `# pyright: ignore`, `# pragma: no cover`,
   `# nosec`, `# pylint: disable`, `# mypy: ignore`, `# ruff: noqa`,
   `# fmt: off/on`, `# isort: skip`, `# coverage: ignore`); a `#` inside a
   string is part of that string, and prose that merely mentions a directive is
   not a suppression.
3. **A recorded budget with a required reason.** `scripts/size_budget.json`
   holds `logic_lines`, `suppression_comments` and a one-line `reason`. Growth
   in either number fails with the measured and recorded values.
   `--write --reason "..."` recomputes and records the numbers; a missing,
   blank, multi-line or malformed reason is refused. A missing or unreadable
   budget fails closed with exit 2 rather than passing by accident.
4. **Shrinking is free; the budget is a ceiling.** A tree that drops below the
   recorded numbers passes without an update. The recorded number is not
   auto-tightened, so deletion lanes do not churn the budget; a later lane may
   lower it deliberately with a reason.
5. **The budget is a protected gate definition.** Both files live under
   `scripts/`, which H32's `PROTECTED_GATE_DIRECTORIES` rule treats as gate
   tooling. On the real job and acceptance paths, raising the budget is an
   owner question, not something a candidate can silently do to pass its own
   gate. The check that measures a change therefore cannot be edited by that
   change without the owner seeing it.
6. **Under a second, no state.** The check reads and strips the tree on each
   run; it keeps no cache, imports nothing beyond the standard library, and
   measured about 0.3 s wall time including interpreter startup on this host.

## 3. Consequences

- Recorded with this lane (base `fb6be7c`, the check, its tests and this record
  in place): **176,563 logic lines** and **354 suppression comments**. Both
  files are committed with the lane, so the tree passes its own check.
- `tests/test_h34_size_check.py` fails on the base tree (the script and its
  wiring do not exist) and passes here. It guards the counting rule (docstrings,
  string content, blank lines and plain comments are free; a prose mention is
  not a directive), growth failing without an update, the update plus a
  one-line reason passing, the refusal of a missing or multi-line reason, the
  current tree staying within the recorded budget, and the gate wiring. The
  sub-second target is measured on the check itself, not asserted inside a
  parallel suite that would time the xdist crowd instead.
- The full and per-change gates each pay under a second more; the lane's
  three-minute budget is unaffected.
- **Deliberately left alone:** no limit on comments; templates, static assets
  and Markdown are outside the measured tree; the budget is not auto-tightened
  after deletions; the presentation-only gate does not run it.
- A future growth is a deliberate, recorded action: update the numbers with a
  one-line reason, and on the real check path the owner accepts the protected
  definition change before the next gate loads it.

## 4. References

- Phase 1.1 plan, lane H34 and "Rules for every lane"; phase 1 "Rules for every
  lane"
- `docs/gate.md` ("Size budget (local only)"), `README.md` (Development)
- Branch base `fb6be7c` (local `main`); ADR-0092, ADR-0093, ADR-0094
- `data/so-h34/notes.md` (caller lists, counts, measured time, gate result)
