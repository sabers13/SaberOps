# Development gate workflow

Workers run `bash scripts/lane-check.sh --base REF [--also PATH_OR_NODE ...]
[--out FILE]`. It prints committed and local changed files, checks the whole
repository with ruff and strict mypy, and runs changed test files plus matching
`tests/test_<module>.py` files and explicit targets. Deleted test files are omitted.
Zero targets skips pytest, while lint and types still run. This evidence makes a
lane provisional; it does not certify integration.

The integrator merges all lanes and runs **one** `bash scripts/gate-full.sh
[--result-dir DIR]`. The full gate holds a shared slot during lint, types, both
pytest passes, reruns and cleanup. The first pass runs `not serial` with xdist
worksteal; the second runs `serial` without xdist. Both retain `--durations=20`.
Every failure identified in JUnit is rerun once without xdist, unless infrastructure
or collection errors prevent a meaningful rerun. No tests are excluded by quarantine.

On failures, run:

```bash
python scripts/attribute_failures.py --base REF \
  --lane G-001=orch/G-001 --lane FIX=orch/I-003-fix-1@<fork> \
  --head HEAD --glue \
  --failed /path/to/result/failed.txt --junit /path/to/result/full.xml \
  --gate-log /path/to/gate.txt --out /path/to/failures.md
```

Attribution imports the coverage selector with its static fallback, evaluates each
lane's exact tree in a private temporary clone, and never executes pytest. Signals
are selection, a changed file in the traceback, and ownership of the test file.
Two or more signals from one lane means high confidence; one means low confidence;
multiple lanes means interaction; no signals means unattributed. A lane may be
passed as `NAME=BRANCH@FORK`: changed files and test selection are then computed
from `FORK..BRANCH` instead of `base..BRANCH`. `FORK` must be an ancestor of
`BRANCH`, otherwise the command exits 2 naming the lane. `--head REF` (default
`HEAD`) is the integration head being attributed. With `--glue`, a pseudo-lane
named `GLUE` covers integration commits in `base..head` reachable from no lane tip
(`git rev-list <base>..<head> --not <lane tips>`). Its changed files are the union
of `git diff-tree` per commit (`--cc` for merges, so clean merges contribute only
conflict resolutions). GLUE signals are traceback and owns-test only; it has no
selects signal and no test selection is computed for it. GLUE buckets like any
lane: alone it is `GLUE (high|low confidence)`, with another lane it is
interaction. Lint/type diagnostics are parsed by gate-log sections: a section starts
at a `+ ` command echo and only `ruff check` or `mypy` sections are scanned
(`path.py:<line>: error:` for mypy; concise and `--> path.py:l:c` forms for ruff).
Pytest traceback lines are never lint/type entries. A log with no ruff/mypy
sections yields no lint/type entries plus a `## notes` line. Lint/type entries are
assigned by changed file, with GLUE taking part as a lane. Route a single-lane failure to its existing worker
session; interaction, unattributed, closed-session or second-attempt failures go to
a Fixer. Infrastructure problems require environment repair, not product edits.

## Outcomes and evidence

The final line is `GATE OUTCOME: WORD` or `LANE CHECK OUTCOME: WORD`, with a reason
when applicable. The preceding line reports test/failure/flaky counts and wall time.

| Outcome | Exit | Meaning |
| --- | --- | --- |
| PASS | 0 | Checks and initial tests passed |
| FLAKY_PASS | 0 | Every first-pass failure passed once serially and is quarantined |
| FAIL | 1 | Tests, code collection, lint/types, or unquarantined flakes failed |
| TIMEOUT | 1 | Wall budget or a still-failing test timeout |
| INFRA | 3 | DISK_LOW, SLOT_WAIT_TIMEOUT, ENOSPC, PYTEST_INTERNAL, INTERRUPTED |
| FAIL (SELECTED_BUT_ZERO_RAN) | 5 | Nonempty selection ran no tests |

Code collection errors (for example ImportError) are FAIL. Pytest exit 3 or
INTERNALERROR is PYTEST_INTERNAL. ENOSPC anywhere in pytest output takes precedence.
Infrastructure failures are never rerun automatically.

Full-gate results contain `full.xml` (all passes and rerun), individual pass XML/logs,
`failed.txt`, `flaky.txt`, and `outcome.json` (counts, wall time, HEAD SHA, peak scratch
size and largest test directories). They are copied to the git common directory's
`saberops-gate-last/` before cleanup. An explicit result directory survives cleanup;
the default lives inside private scratch and is available in the copied evidence.

## Resource knobs

| Variable | Default | Effect |
| --- | --- | --- |
| SABEROPS_GATE_TMP | `${TMPDIR:-/tmp}/saberops-gate-$USER` | Owned scratch root |
| SABEROPS_GATE_MIN_FREE_GB | 10 | Minimum free GiB; 10% filesystem floor also applies |
| SABEROPS_GATE_KEEP_FAILED_GB | 2 | Total retained failed scratch cap in GiB |
| SABEROPS_GATE_KEEP | unset | `1` bypasses exit cleanup |
| SABEROPS_GATE_SLOTS | 1 | Concurrent full gates in this repository's git common directory |
| SABEROPS_LANE_SLOTS | 2 | Concurrent lane/fix checks holding light slots |
| SABEROPS_GATE_SLOT_WAIT | 3600 | Slot wait deadline in seconds |
| SABEROPS_GATE_WORKERS | auto | xdist worker count in full parallel pass |
| SABEROPS_LANE_TIMEOUT | 1200 | Whole lane pytest budget in seconds |
| MYPY_CACHE_DIR | `<git-common-dir>/saberops-mypy-cache` | Shared incremental type cache |

A slot uses an OS flock, automatically released when its owner dies. The heavy pool
(`saberops-gate-slots`, `SABEROPS_GATE_SLOTS`, default 1) is exclusive: a full gate
holds the heavy slot and then drains all light slots, holding them for the whole
run. The light pool (`saberops-gate-light-slots`, `SABEROPS_LANE_SLOTS`, default 2,
must be >= 1) is for lane checks (G1) and fix checks (G2). A lane first waits while
a full gate holds the heavy slot, then acquires one light slot and holds it for the
whole run (lint, types, tests). Waiting prints `waiting for full-gate slot:`,
`waiting for lane slot:`, or `waiting for running lane checks:` with holder pid,
worktree and start time immediately and once per minute; exceeding
`SABEROPS_GATE_SLOT_WAIT` is `INFRA SLOT_WAIT_TIMEOUT`. Because the full gate
always takes heavy before light and a lane never waits for heavy while holding
light, new lanes queue behind a draining full gate and the order cannot deadlock.
More than 150 lane target arguments use four xdist workers.

Each run creates `<worktree>-<UTC timestamp>-<pid>/owner.json`, and each pytest pass
owns a separate basetemp under it. Startup sweeps owned runs with dead pids or age over
24 hours and legacy `/tmp/pytest-of-$USER/pytest-*` directories older than 24 hours,
logging paths and sizes. Free space is checked on both scratch and repo filesystems:
`max(minimum GiB, 10% of total filesystem size)`. The exit report includes scratch
size, sampled peak size and ten largest test directories at peak. PASS, FLAKY_PASS
and INFRA scratch is deleted; failures keep at most the newest failed run per
worktree, dropping oldest runs until the total cap is met. A single oversized failed
run may therefore be dropped. SIGINT/TERM/HUP stops the child process group and
reports INTERRUPTED. SIGKILL leaves an owned orphan for the next sweep.

## Test policies

`pytest-timeout` enforces 180 seconds per test. Longer timeouts need a measured
reason next to an explicit `@pytest.mark.timeout(N)`. Markers `serial` and `slow`
are registered and strict marker validation is enabled. Timing/process-control
modules `test_session_control.py` and `test_parallel_work.py` run serially because
ADR-0003 records contention failures and successful serial reruns. Additional marks
require evidence; no test logic is changed. Plain pytest retains only failed tmp_path
directories, with a retention count of one.

`tests/quarantine.txt` contains exact node IDs, one per line; blank lines and comments
are ignored. The initial file is empty. Only owner-approved entries belong there.
A serially passing unquarantined flake still fails the full gate and is listed in
`flaky.txt` and the reason. Do not auto-add flakes or weaken tests to make a gate pass.

## Test hygiene report

After every G3 PASS and map rebuild, the Integrator runs the read-only
`python scripts/test_hygiene.py [--repo PATH] [--map DIR] [--junit PATH]
[--base REF] [--out PATH] [--json PATH]`, which executes no tests and imports
no `src` module (AST and file reads only), writing only `--out`/`--json`
(markdown to stdout when `--out` is absent). Exit 0 means a report was
produced (candidates are not errors); exit 2 means unusable input (a path
that was given but is unreadable). By hand, point `--junit` at a full-gate
`result/full.xml` and `--base` at the previous wave base.

When the map's build commit is not the `--repo` HEAD, the header says
`map is stale (<map sha> vs <head sha>)` (or `no coverage map` when there is
no map) and the coverage-based sections B and C are skipped; the others still
run. The report lists candidates only, never deletions: (A) top-level
`def`/`class` symbols in `src/saberops/**` referenced only from tests (with a
dynamic-dispatch false-positive caveat), (B) `--junit` node ids covering no
`src` lines, grouped by file (`script test` marks files loading `scripts/`
modules; the map table holds only tests that executed `src` code, so without
`--junit` B prints a needs-junit line with the unmatched count instead), (C)
identical-coverage groups spanning at least two test files (same-file
parametrizations counted, not listed), (D) task-named test files
(`TASK_NAMED_RE` in the script), (E) the slowest 20 tests from `--junit`
times, and (F) with `--base`, new tests whose docstring has no `Covers:` line.
The footer records that retirement happens only through a prompt's "Tests
retired / rewritten" table.

## Local trial measurement (2026-10-12)

On this eight-core machine, the exact `54878ce` snapshot's old gate took 496.37 s
(855 passed, one session-control race failed), with 132,638,719 bytes of sampled
pytest scratch. The hardened gate took 225.03 s (875 passed, no flakes), with
5,241,732 bytes of sampled private scratch. Its tiers ran 836 tests in parallel
and 39 serially. Sampling was every 0.5 s; these are observations, not a bound on
future run sizes or durations. The old snapshot's complete duration list and both
runs' disk-heavy directory lists are in the G-002 orchestration evidence.
