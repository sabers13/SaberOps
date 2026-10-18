# Vendor CLI live check (owner-run, outside pytest)

HV-1 (Step 023).  This is the dated, token-free check the owner runs by hand.
It never sends a model prompt, never reads credentials, and never writes into
the repository unless the owner explicitly copies an approved file in.

Repository rule 3 still holds: pytest never runs a real vendor CLI.  The tests
only replay the committed fixtures through the pure parsers, and the drift
detector and capture command are exercised against stub binaries on a
temporary `PATH`.

## 1. Drift check (read-only)

```bash
PYTHONPATH=src .venv/bin/python -m saberops.vendor_cli_fixtures check
```

It runs `<cli> --version` for every CLI that has a versioned fixture and prints
a `DRIFT` line per stale CLI; the exit code is non-zero when any fixture's
recorded version differs from the installed one.  Owner-supplied logs whose
exact version was not recorded (`cli_version = "unknown"`) are skipped: drift
cannot be assessed for them.

The manifest is validated first (`validate_manifest`).  An unknown kind or
source, a missing or unredacted fixture file, a duplicate path or a bad date
prints an `INVALID manifest` block and exits `2` **without** running the drift
comparison: a version comparison over an unverified manifest could otherwise
print a misleading `no drift`.

## 2. Capture (owner-run, token-free)

```bash
PYTHONPATH=src .venv/bin/python -m saberops.vendor_cli_fixtures capture \
  --vendor all --out ~/vendor-captures-2026-10-16
```

* `--out` must be **outside the repository**; the command refuses an in-repo
  path.  Nothing is ever written into the checkout.
* Only three kinds of process run: `<cli> --version`, the allowlisted
  `<cli> --help` (and subcommand help) needed by the adapters' pinned flags,
  and the existing account readers (`native_usage_panel.read_panel` /
  `cli_usage.read_limits`), which submit no inference prompt and read no
  credentials.
* For each vendor it writes `raw/`, `redacted/` and `capture_log.txt`.
  `redact()` replaces emails, tokens, long hex/base64 secrets, UUID-like and
  `org-`/`ses_` ids, and the owner's home path/username with stable
  placeholders (`<EMAIL>`, `<TOKEN>`, `<ACCOUNT_ID>`, …) while leaving numbers,
  reset labels and layout byte-for-byte.

To refresh a committed fixture: run the capture, review `redacted/`, and copy
the approved file over `tests/fixtures/vendor_cli/<vendor>/<cli-version>/<…>`
with the owner's agreement (the prompt's `needs-decision key=PUBLISH` gate).
Each committed real fixture carries a `provenance` string in
`tests/fixtures/vendor_cli/manifest.json`.

## 3. Exhaustion logs (owner-supplied, optional)

Plan-exhaustion output cannot be produced token-free.  To make a classifier
assumption real, supply a redacted real snippet from a past CLI log and add a
`kind: "exhaustion"`, `source: "owner-supplied-log"` manifest entry naming the
classifier in `consumer`.  Until then the gap stays visible in the manifest's
`fake_only` list.

## 4. Dated live-check record

| Date | CLI versions | Drift result | Fixtures refreshed |
| --- | --- | --- | --- |
| 2026-10-16 | codex `codex-cli 0.162.1`; claude `2.1.301 (Claude Code)`; opencode `opencode v2.0.29` | no drift (`check` exit 0; 3 `config_help` fixtures match) | y — `config_help` captured and owner-kept (D-0015); usage panels/JSON and exhaustion **not** refreshed: owner-declined 2026-10-16 (D-0016), recorded under `fake_only` |
| 2026-10-17 | opencode `opencode v2.0.29` | not run (fixture edit only) | y — `opencode/opencode-v2.0.29/config_help.txt` re-captured token-free with `opencode serve --help` appended (Step 024/HV-2, authorized by the step prompt); the first three sections reproduced byte-for-byte |
| 2026-10-17 | opencode `opencode v2.0.29` (sha256 `7e23471f…`, probe evidence in `LANE/probe-isolation/`) | `check` exit 0 on the committed manifest | fixup 1 (D-0040): five-site config/plugin isolation repair — `workers/opencode_isolation.py` (owned root + `OPENCODE_DISABLE_PROJECT_CONFIG=1` + inline `integration.use` denies), applied to the direct worker, the session server, first-use verification, the Go usage reader and the web model listing; refusals before launch use existing typed forms |

## 5. Findings

**F1 — OpenCode flag pinning drift — FIXED in Step 024 (HV-2).**  HV-1 recorded that the
adapters pinned flags the installed `opencode v2.0.29` help does not document:
`src/saberops/workers/opencode.py` passed `--dir`/`--pure`,
`src/saberops/session_runtime.py` passed `serve --pure`, and
`src/saberops/model_access/model_verification.py` passed `--dir`/`--pure`.  Step 024 removed
every one of them — no undocumented flag remains.  The worktree now travels as the child
process `cwd` (the documented directory mechanism: the top-level help's
`opencode <subcommand> [flags] [<directory>]` argument), and isolation is preserved by the
hermetic child environment plus the pinned inline `OPENCODE_CONFIG_CONTENT`, which outranks
project and global config.  The fixture capture was extended token-free with
`opencode serve --help` so the serve site's flags are covered too, and the former strict
`xfail` on
`tests/test_contract_vendor_cli_parsers.py::test_opencode_adapter_flags_are_all_documented`
is now a plain pass.

Help lines relied on (installed `opencode v2.0.29`, exit 0), in
`tests/fixtures/vendor_cli/opencode/opencode-v2.0.29/config_help.txt`:

* `$ opencode run --help` — `opencode run [flags] [<message...>]`; flags `--model, -m`,
  `--agent`, `--format`, `--session, -s`, `--fork`, `--continue, -c`, `--auto`, …; **no
  `--pure`, no `--dir`**.
* `$ opencode serve --help` — `opencode serve [flags]`; flags `--hostname`, `--port`
  (and `--cors`, `--service`, `--stdio`); **no `--pure`**.
* `$ opencode --help` — top-level usage `opencode <subcommand> [flags] [<directory>]` with
  a `directory` argument; subcommands `run`, `serve`, `models`.
