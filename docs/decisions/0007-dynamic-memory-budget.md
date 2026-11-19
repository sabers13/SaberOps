# ADR-0007: A dynamic host-wide memory budget replaces fixed worker and slot counts

- **Status:** Accepted; implemented by Phase 1 lanes A8 and C1
- **Date:** 2026-11-19
- **Decider:** Saber (owner), Phase 1 plan lanes A8 and C1
- **Related:** [ADR-0001](0001-saberops-engine-mcp-first.md) (engine invariants),
  [ADR-0009](0009-gate-and-test-policy.md) (gate policy)

## 1. Context

The engine sized work with configured counts: a fixed number of pytest workers
per gate, a fixed global worker concurrency of 15, fixed per-unit memory
ceilings, and a per-change gate that could start one pytest worker per core with
no memory check. Running several gates while worker CLIs were active exceeded
the machine's 15 GB, and the owner reported crashes during tests and gates.

The owner's direction on 2026-11-19: **no fixed worker count**. Concurrency
follows live memory, so many small tasks can run at once and heavy ones only a
few. Test runs, type checks and builds — not a worker CLI waiting on its model —
are where the spikes come from.

## 2. Decision

1. **One host-wide budget.** Grantable memory is available memory minus a
   reserve that protects the desktop and other applications. The reserve comes
   from the machine's measured idle baseline, not from an owner-tuned count.
2. **Every heavy step reserves before it runs.** Heavy steps are the per-change
   gate, the full gate, the engine's in-run gate, the post-integration gate on
   accept, the strict type check and the coverage-map rebuild. A step whose
   predicted peak the budget cannot cover waits.
3. **Predictions come from learned peaks.** A step's predicted peak is the
   measured peak (cgroup `memory.peak`) of earlier runs of the same step and
   selection size. Before history exists, assume the largest kind seen so far,
   then learn. A worker CLI reserves its own learned footprint separately from
   the test runs it starts.
4. **pytest workers follow the grant:** at least one, never more than the cores.
5. **Live pressure feedback.** While kernel memory pressure is present, grant
   nothing new. If pressure keeps rising, freeze the most recently started gate
   and thaw it when pressure clears. A test run stopped this way is re-run and
   never reported as failed.
6. **Running workers are never killed for memory, and no work is discarded.**
7. **One ledger.** A flock-protected record beside the existing gate slots is
   shared by every SaberOps copy and process; a reservation is released when its
   holder exits or dies.
8. **Per-step ceiling.** Each step runs in its own systemd user scope with
   `MemoryHigh` set to its grant, replacing the fixed per-unit ceilings.
9. **Worker admission.** A launch the budget cannot cover parks with a
   `MEMORY_LOW` reason and resumes when memory frees. The fixed global
   concurrency of 15 stops being the limiter; per-provider limits that reflect
   subscription rules stay.
10. **Scratch.** When `/tmp` is a RAM-backed tmpfs, gate scratch and verification
    snapshots default to a disk path, and a passing gate removes its scratch.
11. **Visibility.** Each gate records its grant, measured peak and any wait or
    freeze, so estimates and stalls can be inspected.

## 3. Consequences

- Concurrency tracks what steps actually use instead of a configured number; a
  heavy gate can wait, and that wait is visible rather than a silent stall.
- Learned peaks replace the cold-start estimate after real runs.
- No configured worker or slot count remains in the gate code. The budget has
  exactly one implementation, in the existing gate tooling; a second one would
  reintroduce the crash path it replaced.
- A wrong or stale estimate costs a wait or a re-run, never a killed worker or
  discarded work.
