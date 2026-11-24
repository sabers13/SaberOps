# ADR-0093: Docstrings and comments keep contracts, not history

- **Status:** Accepted. Implemented in phase 1.1 lane H30.
- **Date:** 2026-11-24
- **Decider:** Saber (owner): the 2026-11-22 answer ("do your suggestion and
  maximise the deletion ... add all of this to the new phase 1.1 plan") and the
  2026-11-23 answer ("update the plan, make the plan in style of phase 1
  parallel favor"), applied to lane H30 ("keep each one's contract ... remove
  history ... no code change in this lane").
- **Related:** phase 1.1 plan lane H30; phase 1 "Rules for every lane";
  [ADR-0005](0005-mcp-first-engine-and-phase-2-board.md) (the engine's module
  boundaries), [ADR-0092](0092-test-suite-admits-without-the-host-guard.md)
  (the preceding lane on local `main`).

## 1. Context

H30 runs after every code-changing phase 1.1 lane except H31 and H34. Its
authorized area is history text inside docstrings and comments in `src/`:
roadmap step numbers, lane narratives, "M2-3 section" labels, and descriptions
of previous behaviour. Contracts (what a thing does, what it refuses, its
invariants) stay, and so does every `shortcut:` marker. Comments and docstrings
have no code callers; the lane's own verification is therefore structural.

Measurement at the branch base (`e77950b`): `src/` is 131,445 lines, of which
15,280 are docstring lines and 5,847 are comment lines (21,127 together). The
history is mostly inline: a parenthetical lane id, a leading step label, or one
sentence of "was removed" or "will land", rather than whole paragraphs.

**Caller verification.** No product path reads a docstring or comment. Two
readers exist:

- `scripts/attribute_failures.py` parses its own `__doc__` for `--help`; it is a
  script, not `src/`.
- `tests/test_h36_contract.py::_documented_tool_list` parses the `exposed::`
  literal block in `src/saberops/mcp/tools.py`. That block is a contract and is
  untouched; the diff to `tools.py` changes other prose only.
- `scripts/test_hygiene.py` uses `ast.get_docstring` only to look for a
  `Covers:` line in **new test functions**; no test changed here.

Nothing else outside `src/` needs the removed text. The MCP, scheduler, CLI,
web, scripts, tests and benchmark-rig paths read code, schema strings and
evidence, never this prose.

## 2. Decision

1. **Only docstring and comment text changed.** A line-region check maps every
   one of the 1,373 changed lines in the diff to a docstring `STRING` token or
   a `COMMENT` token in the old or new file: 0 lines fall outside.
2. **The program is unchanged.** For all 106 changed files, `ast.dump` of the
   module with docstrings stripped is identical before and after:
   106 checked, 0 differ.
3. **History classes removed:**
   - phase/lane step ids: `H*`, `F*`, `P*`, `E*`, `A*`, `Q*`, `M2-*`, `M3`,
     `M4`, `D2`-`D17`, `R1`/`R2`, `N2`-`N17`;
   - ticket and step tags: `GS-1`/`GS-2`, `HV-1`/`HV-2`, `CFG-1`, `T-004`/
     `T-005`, `I-003`, `R-001`/`R-002`, `R3`/`R3-M1`, `GT-1`, `RD-1`, `L-A`,
     `Step NNN`;
   - section labels: "M2-3 section A", "sections C + D", "(amended /section
     11)";
   - narrative: owner-approval dates, "the removed gateway seam formerly ...",
     "The walkthrough used to ...", "will register its pause", "has not landed
     yet", "three checkpoints" for the advisor, "This lane" / "in this lane"
     where the module was meant, and "moved from ..." provenance sentences.
4. **Kept contracts.** Every behavioural statement stays: what each module,
   class and function does, what it refuses, its invariants, ordering and
   fail-closed rules. In place, the following are deliberately not treated as
   history:
   - review dispositions `B1`/`B2`/`B3`, `D1`, `N1`;
   - model tiers `T1`/`T2`/`T3`;
   - `C0` ASCII control codes;
   - legacy persisted-data and migration contracts (`LEGACY_ROLE_ALIASES`,
     retired role names, old routing documents, legacy gate documents);
   - ADR cross-references, including `ADR-0008 section 4`;
   - the `exposed::` tool list and every `shortcut:` marker (there is one
     instruction line in `validation_policy.py`; it is a code string and was
     not touched).
5. **In-place rewrites where deletion would break grammar.** Removing `D8`
   from the preemption module, for example, would have left subjectless
   sentences; those became "preemption ...". `M3`/`P6`/`M2-1` possessives became
   "the return-summary ledger", "the landing order" and "the capacity matcher".
6. **No code line, code string, test or design record changed.** The lane's
   scope is `src/` docstrings and comments; `docs/` and ADRs are untouched.

## 3. Consequences

- Measured reduction (`wc -l`): source 131,445 → 131,426 (−19); docstring lines
  15,280 → 15,265 (−15); comment lines 5,847 → 5,843 (−4); tests 97,566 →
  97,566 (unchanged). The commit changes 106 files, `+677 / −696`.
- The line reduction is modest because the history was inline identifiers and
  clauses rather than redundant paragraphs. The reading cost removed is the
  stale narrative itself: there is no remaining roadmap, lane, ticket, section
  or "previous behaviour" reference in `src/` docstrings or comments, so a
  reader no longer needs the phase plans to interpret a module.
- **Deliberately left alone** (reported, not hidden): owner-facing code strings
  that still name a retired lane or step (`H17` in an access refusal, "Step
  015" in CLI help, `ROADMAP §11` in an error message, `CFG-1` in a standing
  rule target), SQL comments inside DDL string literals, and the sample lane
  text in `plan_file.py`'s template. Those are code, not comments/docstrings;
  changing them belongs to a code lane, and the lane's diff must stay
  comment-only. `docs/` and the ADRs keep their own history for the same
  reason.
- Ruff and `mypy --strict` pass unchanged. The full gate passed
  (`GATE OUTCOME: PASS`, 3,286 passed, 0 failed, 717.57 s); the run is recorded
  in `data/so-h30/notes.md`. H31 and H34 run after this lane.
- No safety property is affected: the lane changes no executable statement,
  no string constant except docstrings, no test, no check, no acceptance,
  review, isolation or billing path.

## 4. References

- Phase 1.1 plan, lane H30 and "Rules for every lane"
- Phase 1 plan, "Rules for every lane"
- Branch base `e77950b`; `src/` at 131,445 lines
- `src/saberops/mcp/tools.py` (`exposed::` contract block, untouched)
- `tests/test_h36_contract.py`, `scripts/test_hygiene.py`,
  `scripts/attribute_failures.py` (the only docstring readers inspected)
- `data/so-h30/notes.md` (caller lists, counts, proofs and gate result)
