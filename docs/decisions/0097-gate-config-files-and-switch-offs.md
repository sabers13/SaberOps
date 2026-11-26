# ADR-0097: Gate config overrides and added switch-offs are owner questions

- **Status:** Accepted. Phase 1.1 lane H41. Amends decision 5 of
  [ADR-0060](0060-gate-protection-on-the-real-paths.md) for added directives.
- **Date:** 2026-11-26
- **Decider:** Saber (owner), 2026-11-23 Q15: wire the check protection, do not
  delete it; owner approved H41 as proposed on 2026-11-26.
- **Related:** [ADR-0060](0060-gate-protection-on-the-real-paths.md) (gate
  protection on the real paths), [ADR-0055](0055-review-evidence-guarantee.md)
  (engine-verified review evidence), [ADR-0053](0053-acceptance-clause-review-gate.md)
  (acceptance clause review)

## 1. Context

H32 (ADR-0060) wired gate protection into the job check and acceptance. The
protected definitions were `scripts/`, every `conftest.py`, `pyproject.toml`
and `.github/workflows/ci.yml`. The check failed closed when a candidate
changed one, because a gate cannot run under a definition the candidate
edited.

That list left two ways to change the verdict without touching a definition:

1. **An override config file.** pytest prefers `pytest.ini` over
   `pyproject.toml` *even when `pytest.ini` is empty*, and `tox.ini`,
   `setup.cfg`, `ruff.toml`/`.ruff.toml` and `mypy.ini`/`.mypy.ini` override
   the matching `pyproject.toml` sections. A workflow other than `ci.yml` was
   also unprotected.
2. **An added switch-off.** `# noqa`, `# type: ignore`, a lint disable, a
   `pytest.mark.skip`/`xfail` marker or `pytest.skip(` on an added line changes
   what the gate checks without changing any file the engine treated as a
   definition. H32 sent these to the reviewer as evidence only.

A rig reproduction on the base commit confirmed both. Three candidates passed
the job check and acceptance:

- an **empty** `pytest.ini` that made pytest ignore the trusted
  `pyproject.toml` `-W error` warning filter;
- a `pytest.ini` whose `addopts = --deselect ...` removed the failing test;
- `@pytest.mark.skip` on the failing test.

## 2. Decision

1. **Every override config file is a gate definition.** `pytest.ini`,
   `.pytest.ini`, `tox.ini`, `setup.cfg`, `ruff.toml`, `.ruff.toml`,
   `mypy.ini` and `.mypy.ini` are protected by basename in every directory
   (pytest searches upward from the collected path; ruff discovers the nearest
   config, including a nested `pyproject.toml`), and `.github/workflows/` is
   protected as a whole directory so every workflow, including one added later,
   is covered. They are loaded from the base SHA and compared byte-for-byte
   exactly as the root `pyproject.toml` is.
2. **An added switch-off is the same owner question.** The engine scans the
   lines the candidate added in Python files (`git diff base`) for `# noqa`,
   `# type: ignore`, other lint disables, and skip/xfail markers or calls. The
   first match on a line is reported once with its kind. A finding blocks the
   gate with reason code `GATE_WEAKENING_DIRECTIVE_ADDED` and an owner question
   that lists every directive as `path: line [kind]`.
3. **One mechanical path.** `check_gate_protection` accepts `added_lines` and
   `verify_gate_definitions` feeds it the candidate diff. The existing
   `GateProtectionBlockedError`, job-check failure and acceptance rollback
   therefore carry the new verdict unchanged; no second blocking mechanism was
   added. A successful receipt records `directives` beside the path verdict.
4. **The owner decision is still "accept into the base".** There is no
   self-authorization and no new allowlist file. If the owner accepts a
   definition or directive change, it lands in the base SHA through the owner
   merge; the next gate compares against that base, where the directive is no
   longer an added line, and the change is allowed.
5. **Deleted assertions stay review evidence.** A removed `assert` line is not
   a directive and does not block; `build_review_evidence` now reports the
   count and each `path: line`, next to the existing deleted-test, rewrite,
   skip, suppression and catch-all evidence. Removing an existing suppression
   is an improvement, not a directive (only added lines are scanned).
6. **The line scan is deliberately textual.** A string literal whose text
   looks like a directive can appear; the owner question shows the exact line,
   so the owner can accept it. This is the same "the owner sees the fact"
   posture as the write-scope and retirement evidence, and it is the reason the
   scan is limited to Python files.

## 3. Consequences

- A candidate can no longer buy a passing verdict by adding an empty or
  filtered `pytest.ini`, another tool config, a new workflow, or a suppression
  or skip on an added line.
- The rule is noisy by construction. In phase 1.1's own lane commits between
  `7384d36` and `3423d09` (44 commits), 14 commits added 44 directive lines:
  25 `# noqa`, 7 `# type: ignore`, 8 other lint disables and 4 skip markers.
  Roughly one deliberate suppression per 3 commits; each would have been one
  owner question. That is the known cost of making an added switch-off visible
  rather than leaving it to the reviewer.
- Ordinary test fixes and ordinary assertions remain allowed: only protected
  paths, added directives, deleted or rewritten tests (evidence), write scope
  (evidence) and deleted assertions (evidence) are reported.
- The phase 2 per-target-repository protection list is out of scope; this lane
  only closes the gap for the current repository's gate definitions.

## 4. Tests

`tests/test_h41_gate_configs_and_directives.py`:

- the reproduction: an empty `pytest.ini` is blocked before the gate runs, and
  an `addopts` deselect is blocked on acceptance;
- one case per protected override config type (including a non-`ci.yml`
  workflow);
- one case per directive kind (noqa, type-ignore, lint-disable, skip call,
  skip marker, xfail);
- explanatory prose about a directive and an ordinary added assertion are not
  directives;
- accepting the change into the base SHA makes the next gate allow it;
- `tests/test_review_evidence.py` covers deleted assertions as engine evidence.
