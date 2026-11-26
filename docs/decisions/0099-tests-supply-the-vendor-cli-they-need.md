# ADR-0099: A test that needs a vendor CLI supplies its own

- **Status:** Accepted. Implemented in phase 1.1 lane H40b.
- **Date:** 2026-11-26
- **Decider:** Saber (owner): the 2026-11-22 and 2026-11-23 plan answers, applied
  to lane H40b ("the suite passes with the vendor CLIs hidden from PATH and
  normally; the next push of `main` is green on GitHub").
- **Related:** [ADR-0092](0092-test-suite-admits-without-the-host-guard.md)
  (the test suite never depends on the host for admission),
  [ADR-0096](0096-every-xdg-base-is-test-scoped.md) (H40, whose CI run exposed
  these three failures), the phase 1.1 plan lane H40b, and
  `data/so-h40b/notes.md` (reproduction and validation).

## 1. Context

H40 (ADR-0096) made every test resolve a test-scoped XDG store. GitHub CI run
`45745099395` on `71fcf30` then reported three failures out of a suite that had
previously failed wholesale:

- `tests/test_doctor_h39.py::test_cli_doctor_without_fix_stays_read_only_and_fix_repairs`:
  `handle_doctor(fix=True)` returned `1` and the summary ended
  `13 ok, 0 skip, 0 fixable, 4 human - NOT READY`.
- `tests/test_f16_native_definitions.py::test_dry_run_writes_nothing` and
  `::test_install_and_remove_is_one_command`:
  `orch connect claude --skill --dry-run` returned `1` and printed
  `Claude Code is not installed.`

Both pass on a developer machine because Claude Code, Codex, OpenCode or
Antigravity happen to be installed there. `orch doctor` treats "no worker
provider is AVAILABLE" as a `FAIL`, and `orch connect claude` checks
`shutil.which("claude")` before it will run the CLI's own `mcp` command. The
tests asserted product behaviour (doctor readiness, connect's installed branch)
while silently borrowing the host's installation to reach it.

A suite that is green only where the vendor tools are installed is not evidence
that the code works, and the next push of `main` cannot be green on a clean
runner until the tests provide their own environment.

## 2. Decision

1. **A test that exercises the installed branch supplies the CLI.** A new
   `fake_vendor_cli` fixture in `tests/conftest.py` creates a test-scoped `bin`
   directory holding an executable stub that exits `0` without doing anything,
   and prepends that directory to `PATH` with `monkeypatch.setenv` (so the value
   is restored after the test). The stub is invoked only where the test's own
   assertion already reaches; no real vendor CLI, prompt or network is touched.
2. **The three failing tests use it.** `tests/test_doctor_h39.py` installs a stub
   `opencode` so at least one provider is AVAILABLE for the CLI doctor's
   readiness check. `tests/test_f16_native_definitions.py` installs a stub
   `claude` in its `home` fixture, so `orch connect claude` takes its installed
   branch and runs the stub instead of the host binary.
3. **No product behaviour changes.** `agent_connect.connect`,
   `agent_connect.disconnect`, `_check_providers` and `handle_doctor` keep their
   current detection, output and exit codes. The doctor still fails on a host
   with no provider; connect still refuses when a CLI is absent. Only the test
   environment is supplied.
4. **The suite is checked both ways.** The lane runs the three tests and the
   full suite with the vendor CLIs removed from `PATH`, and the full gate
   normally, so a future test cannot quietly reintroduce a host dependency.

## 3. Consequences

- The three tests now state the fact they depend on. A clean GitHub runner and a
  developer machine with all four vendor CLIs behave the same, and a developer
  without them no longer sees a false failure.
- The fixture is opt-in per test. It does not hide real vendor CLIs from the
  rest of the suite, so tests that deliberately drive detection with their own
  fakes (for example `test_cli_startup_discovery`) keep their existing seams.
- The stub is a test double for *presence*, not for vendor behaviour. A test
  that needs the CLI to write its config or report a version still provides
  that behaviour itself (as `test_agent_skill`'s `CodexRunner` already does).
- This record amends no isolation or safety rule. Owner-store isolation
  (ADR-0082/ADR-0096) and the no-host-admission rule (ADR-0092) are unchanged.
