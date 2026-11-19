# ADR-0018: A vendor CLI version change is checked against the allowlisted help, never guessed

- **Status:** Accepted
- **Date:** 2026-11-19
- **Decider:** Saber (owner), via the Phase 1 plan lane F5
- **Related:** [ADR-0001](0001-saberops-engine-mcp-first.md) (pinned CLI
  configuration and launch-time invariants) and
  [ADR-0011](0011-bounded-automatic-actions.md) (automatic vendor interaction
  must not spend a prompt or change vendor settings).

## 1. Context

SaberOps drives the vendors' official CLIs and pins the flags its adapters
pass. The checked-in vendor fixtures record the exact `--version` and the
allowlisted `--help` text that justified those pins. Preflight already compared
the installed version with the fixtures and printed a drift warning.

After the owner updated OpenCode to 2.3.2, all three fixture sets were stale
(Claude 2.1.301 vs 2.1.328, Codex 0.162.1 vs 0.170.0, OpenCode 2.0.29 vs
2.3.2). A stale fixture is not by itself a defect: a new version can keep every
pinned flag. But a warning alone cannot distinguish "new version, same
interface" from "new version, flag removed", and the owner needs a clear,
actionable state without sending a model prompt.

At the same time, any probe of an arbitrary binary is dangerous. A wrapper or
test shim answering `--version` is not a vendor update, and an unreachable
help page must never be reported as a removed flag: dispatch must not be
blocked on a guess.

## 2. Decision

1. **Drift stays a warning.** Preflight keeps the existing
   `"<provider> CLI version differs from recorded fixtures"` warning.
2. **The guard runs only on a version change.** When the installed version
   differs from every recorded fixture version for that CLI,
   `check_version_support` runs exactly the vendor's allowlisted `--help`
   invocations (the same list the owner-run `capture` uses). It never sends a
   prompt, never reads a credential and never changes a vendor setting.
3. **Three states, one of which is a clear failure.**
   - `SUPPORTED`: a complete allowlisted capture still contains every
     adapter-pinned flag.
   - `UNSUPPORTED`: a complete capture omits one or more pinned flags. This is
     the clear unsupported-version state; preflight reports it as `FAIL` with
     the missing flags and a fixture-refresh remedy.
   - `UNVERIFIED`: the CLI is missing, a help invocation failed or produced no
     text, no pinned flags are recorded, or the installed version string does
     not match the recorded vendor version format. Preflight reports this as a
     `WARN`.
4. **No decision on a guess.** `UNSUPPORTED` requires positive evidence from a
   complete capture. Anything unreadable is `UNVERIFIED`. The guard does not
   admit, deny or delay dispatch at all: it is diagnostic evidence for the
   owner, not a new gate, and it cannot widen billing consent, change owner
   acceptance, review or isolation.
5. **Foreign version shapes are never probed.** A version string that does not
   have the shape of the recorded vendor version (for example a shim printing
   `codex 0.0.0-fake`) short-circuits to `UNVERIFIED` before the executable is
   resolved. This keeps the fake-CLI test rig from ever treating `--help` as a
   worker prompt.
6. **Capture is file-backed, not piped.** Claude Code 2.1.328 emits only a
   pipe-buffer prefix of its `--help` text when stdout is a pipe. Both the
   guard and the owner-run capture write vendor output to a temporary regular
   file, so an incomplete read cannot masquerade as a removed flag.
7. **Fixtures refresh from token-free captures only.** The checked-in fixtures
   are refreshed to the installed versions using only `--version` and the
   allowlisted `--help` invocations. Usage and exhaustion captures remain
   `fake-only` under the owner's earlier decision; refreshing an interface
   fixture does not create a usage fixture.

## 3. Consequences

- An owner who updates a vendor CLI sees either "flags still documented" or a
  named unsupported-version failure with the missing flags, instead of a bare
  drift warning.
- A broken wrapper, a missing binary or a failed help page degrades to
  `UNVERIFIED`; it never blocks dispatch or reports a false removal.
- The guard costs the allowlisted help invocations only when a version change
  is observed; a machine whose fixtures match makes no extra process calls.
- The fixtures now match the versions installed on the owner host, so the
  committed contract tests exercise the current help text. Refreshing them is
  still an explicit, token-free `capture` step rather than an assumption.
- Lane F18 ([ADR-0031](0031-vendor-cli-self-healing.md)) acts on this guard:
  a drifted CLI parks its providers, the guard plus one smoke prompt decide
  whether to re-verify, and a failed check files one repair job. The guard's
  own rule 4 (never decide on a guess) is unchanged there.
