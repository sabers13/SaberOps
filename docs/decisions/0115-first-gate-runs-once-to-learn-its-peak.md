# ADR-0115: A new repository's first gate runs once to learn its real peak

- **Status:** Accepted. Implemented in phase 1.1 lane H57.
- **Date:** 2026-11-28
- **Decider:** Saber (owner), phase 1.1 plan lane H57, chosen from the H37
  recheck. The owner chose option 2: let the first check run once to learn a
  real measurement, not option 1 (fit the cold-start guess to this machine).
- **Related:** [ADR-0007](0007-dynamic-memory-budget.md) (the host-wide memory
  ledger and learned peaks), phase 1.1 plan lane H57, the H37 recheck report §9
  N3, and the gate memory code in `scripts/gate_tools.py`.

## 1. Context

A gate step with no learned sample predicts the cold-start seed
(`SEED_STEP_PEAKS_BYTES`; the effective non-worker guess is 1161 MiB). On a
repository with no `configured-gate:default` sample, a host whose grantable
budget was about 450-810 MB (15 GB machine, 2.5 GB desktop reserve) could never
admit that predicted peak. The gate printed
`waiting for memory budget … predicted=1217396736 bytes > budget=…` once a
minute until the session ended. It was not a tight machine: the real configured
gate measured about 9.9 MB in the scout's lab run, after which the gate ran in
seconds.

The seed is a conservative guess, not a measurement. Waiting forever on a guess
blocks a new repository's very first gate, which is exactly when no history
exists to replace the guess. The alternative option - shrinking the seed for
this machine - was rejected because it encodes one host's number and still
would not learn the actual step.

## 2. Decision

1. **A step with no learned sample runs once instead of waiting on an unfitting
   seed.** When the predicted peak (the cold-start guess) exceeds the remaining
   budget, and the ledger has no completed sample for that `step:size_bucket`,
   the gate acquires a one-off probe lease instead of sleeping.
2. **The probe is alone and capped by what the budget allows.** A probe is
   granted only when no other ledger holder is live; its grant is the whole
   remaining budget, so no other step can be admitted beside it. The step runs
   under a hard `MemoryMax` equal to that grant in the `systemd-run` scope
   (in addition to the usual `MemoryHigh`). The probe is only attempted when a
   scope can actually enforce that cap; otherwise the normal wait still applies.
3. **The measured peak replaces the guess.** The probe's `memory.peak` is
   recorded with the ordinary learning path, and later acquisitions use the
   learned value. A step that already has a sample is never probed and keeps the
   existing reserve and seed behaviour.
4. **A probe killed at the cap is a memory refusal, not a retry.** If the probe
   is stopped at its cap (`oom_kill` in the scope, or a scoped exit 137), the
   capped peak is recorded as a refusal, not as a completed size, and the gate
   reports `MEMORY_REFUSED` naming the step, the cap and the measurement. Later
   attempts with no learned sample at the same or a tighter budget refuse
   immediately instead of waiting for memory that will not arrive; once the
   budget grows past the refused cap a new first run is attempted. A completed
   run's measured peak always takes precedence over an older refusal.
5. **Reporting is explicit at every surface.** The shell lane runner reports
   `GATE OUTCOME: INFRA (MEMORY_REFUSED)` (lane) and the engine returns the
   refusal text on the configured-gate path. `MemoryRefusalError` derives from
   `TimeoutError`, so callers that already fail closed on a budget timeout keep
   doing so.

Out of scope, as the plan states: other gate thresholds, the desktop reserve,
and machine-wide gate scheduling beyond this first-run case.

## 3. Consequences

- A new repository's first gate runs under its actual budget and learns its real
  peak, instead of printing "waiting for memory budget" until the session ends.
- A step that genuinely does not fit is refused in seconds with a message that
  names the measurement, instead of waiting an hour to time out. The refusal is
  durable: retrying at the same budget does not run the step again.
- The probe consumes the whole remaining budget while it runs, so concurrent
  gates wait one probe length instead of running beside a first run that has no
  learned size to schedule against.
- Hosts without a usable memory scope keep the old wait; the change never runs
  an uncapped probe.
- Evidence: `tests/test_memory_budget.py` proves a first run with an empty
  ledger and a below-seed budget runs once, learns the measured peak and reuses
  it, and that a capped kill raises `MEMORY_REFUSED`, records the refusal and
  refuses again at the same budget while retrying successfully after memory
  grows. `tests/test_gate_tools.py` proves the probe scope adds `MemoryMax` at
  the grant while ordinary scopes still set only `MemoryHigh`.
