# ADR-0096: Every XDG base is test-scoped, whatever the process environment holds

- **Status:** Accepted. Implemented in phase 1.1 lane H40.
- **Date:** 2026-11-26
- **Decider:** Saber (owner): the 2026-11-22 and 2026-11-23 plan answers, applied
  to lane H40 ("the next push of `main` is green on GitHub").
- **Related:** [ADR-0082](0082-owner-mode-carries-authority.md) (the owner store
  a test must never reach), [ADR-0092](0092-test-suite-admits-without-the-host-guard.md)
  (the one isolation fixture and the test-suite host rules), the phase 1.1 plan
  lane H40, and `data/so-h22/notes.md` (the 2026-11-23 real-store incident).

## 1. Context

H22 added the autouse `_isolated_owner_store` fixture to `tests/conftest.py`: no
test may resolve the owner's real XDG store, because ADR-0082's owner-mode
reader migrates a pre-1.1 document on its first read and would therefore *write*
the owner's store. The fixture filled only the XDG bases the process environment
left unset, then asserted that the resolved owner config directory is inside the
test's scratch tree.

That guard was correct on this development host, which leaves the four XDG
variables unset, and wrong on GitHub's Ubuntu runner, which exports
`XDG_CONFIG_HOME=/home/runner/.config`. CI run for `3423d09` (`sabers13/SaberOps`,
workflow CI) reported `32 passed, 1 warning, 3162 errors`; every error was the
H22 assertion `test resolved the owner's real config at
/home/runner/.config/saberops`. The suite was green locally only because the
inherited environment differed, not because the isolation was sound.

An inherited value is exactly the case the guard exists for: "the process
environment already points at a real home" is what CI and a developer shell both
look like. Preserving it also made the assertion unreachable in the one
direction it was written to protect.

## 2. Decision

1. **The autouse fixture replaces an inherited base; a fixture-set one wins.**
   It captures the four XDG values once, before pytest starts any fixture
   (``_INHERITED_XDG``), and per test sets each base that is unset or still
   equal to that captured value to a directory under the test's scratch tree.
   That is the CI case: the runner exports a real home, the value is inherited,
   and it is replaced. A test module or session fixture that deliberately
   points a base at its own seeded routing/control-plane tree changes the value
   after the capture, so it is no longer inherited and is preserved as before.
   The update uses ``monkeypatch.setenv``, so it is restored after the test and
   no test can leak a base into the next.
2. **The H22 assertion stays.** The resolved owner config directory must remain
   inside the pytest scratch tree; the guard still fails instead of writing a
   real home, and it now also fails if a future change reintroduces a bypass.
3. **A regression test names the CI condition.**
   `test_conftest_guard_replaces_every_exported_xdg_base`
   (`tests/test_owner_mode.py`) asserts each of the four environment values
   resolves inside the test's scratch tree. Run with the variables exported to a
   scratch "real home", it fails on the pre-H40 fixture and passes after it.
   The H22 test that proves the owner config path is not `~/.config/saberops`
   stays.
4. **Nothing outside test isolation changes.** No product XDG resolution, no
   `paths.py`, no CI workflow edit, no other check. The local gates and the
   suite's own host rules (ADR-0092) are untouched; the suite must now pass both
   with the four variables exported to a scratch home and with them unset.

## 3. Consequences

- The same suite passes on a host that exports XDG bases and on one that leaves
  them unset, so the next push of `main` can be green for a reason the lane
  verified locally rather than a host difference.
- A developer who exports a real `XDG_CONFIG_HOME` to run the suite gets test
  isolation instead of a write to their store; the fixture is the authoritative
  boundary, not the ambient environment.
- The guard still only protects tests. A CLI call or a gate outside pytest must
  still export test-scoped XDG bases itself; that is the existing lane rule and
  the H22 postmortem's lesson, and this ADR does not weaken it.
