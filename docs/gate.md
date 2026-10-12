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
  --lane G-001=orch/G-001 --lane G-002=orch/G-002 \
  --failed /path/to/result/failed.txt --junit /path/to/result/full.xml \
  --gate-log /path/to/gate.txt --out /path/to/failures.md
```

Attribution imports the coverage selector with its static fallback, evaluates each
lane's exact tree in a private temporary clone, and never executes pytest. Signals
are selection, a changed file in the traceback, and ownership of the test file.
Two or more signals from one lane means high confidence; one means low confidence;
multiple lanes means interaction; no signals means unattributed. Lint/type diagnostics
are assigned by changed file. Route a single-lane failure to its existing worker
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
| SABEROPS_GATE_MIN_FREE_GB | 10 full / 5 lane | Minimum free GiB; 10% filesystem floor also applies |
| SABEROPS_GATE_KEEP_FAILED_GB | 2 | Total retained failed scratch cap in GiB |
| SABEROPS_GATE_KEEP | unset | `1` bypasses exit cleanup |
| SABEROPS_GATE_SLOTS | 1 | Concurrent full gates in this repository's git common directory |
| SABEROPS_GATE_SLOT_WAIT | 3600 | Slot wait deadline in seconds |
| SABEROPS_GATE_WORKERS | auto | xdist worker count in full parallel pass |
| SABEROPS_LANE_TIMEOUT | 1200 | Whole lane pytest budget in seconds |
| MYPY_CACHE_DIR | `<git-common-dir>/saberops-mypy-cache` | Shared incremental type cache |

A slot uses an OS flock, automatically released when its owner dies. Waiting prints
`waiting for full-gate slot:` with the holder's pid, worktree and start time immediately
and once per minute. Lane checks wait for existing full gates but do not hold a slot
while checking. More than 150 lane target arguments use four xdist workers.

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

## Local trial measurement (2026-10-12)

On this eight-core machine, the exact `54878ce` snapshot's old gate took 496.37 s
(855 passed, one session-control race failed), with 132,638,719 bytes of sampled
pytest scratch. The hardened gate took 225.03 s (875 passed, no flakes), with
5,241,732 bytes of sampled private scratch. Its tiers ran 836 tests in parallel
and 39 serially. Sampling was every 0.5 s; these are observations, not a bound on
future run sizes or durations. The old snapshot's complete duration list and both
runs' disk-heavy directory lists are in the G-002 orchestration evidence.
