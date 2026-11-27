# ADR-0100: The engine records what a job started, and reports leftovers

- **Status:** Accepted. Implemented in phase 1.1 lane H45.
- **Date:** 2026-11-27
- **Decider:** Saber (owner): the 2026-11-26 instruction to add per-job process
  and port tracking from Orca's open-ports panel ("Record what each job started
  (child processes, listening ports) and flag anything still running after the
  job ends, so the existing 'only stop processes you started' rule can be
  checked"), and "yes do 44 and 45".
- **Related:** phase 1.1 plan lane H45 and "Rules for every lane"; phase 1
  "Rules for every lane"; H44 (ADR-0102, commit provenance in git), the
  sibling lane that puts the job id on the run;
  `data/so-provscan/report.md` §2 (the evidence this lane was written from).

## 1. Context

Process identity was already strong before this lane. A worker starts in its own
session, so its PID is its process-group ID; the fenced identity (host, boot
id, pid, start ticks, session, process group) is persisted before the worker is
considered cancellable; the whole tree is read directly from `/proc`; leftover
descendants are ended at attempt end and an unconfirmed end fails the attempt;
and "only stop processes you started" is enforced by that fence, which refuses
an unknown or other-host process.

What was missing was any record of *what the job actually did*: no port code at
all, no per-job inventory of child processes beyond the top process group, and
no check after a job ends for anything still running. The owner's panel shows
what a job started so a leftover can be seen; phase 2's worker detail (T3, U3)
needs the same facts from the engine, because the board may only show facts the
engine records.

## 2. Decision

1. **Observe, never signal.** A new module, `saberops.job_processes`, reads
   only `/proc/<pid>/stat`, `/proc/<pid>/fd`, `/proc/net/tcp` and
   `/proc/net/tcp6`. It has no kill, pause or cleanup path. A leftover is
   evidence for the owner; ending a process stays with the existing fenced
   termination primitives in `monitor.py` / `ownership.py`, and the "only stop
   processes you started" rule is untouched.
2. **Key the record by the fenced identity.** Three additive tables, with
   `DB_SCHEMA_VERSION` unchanged:
   `job_processes` (run, attempt, host, boot id, pgid, pid, start ticks, comm,
   leader flag, first/last seen), `job_ports` (run, attempt, host, boot id,
   pgid, protocol, port, socket inode, address, first/last seen) and
   `job_leftovers` (run, attempt, verdict, detail). No job-id column is added
   here (H44 owns `runs.job_id`); the read accessors resolve the job from that
   column, so no caller walks the job store.
3. **Sample while it runs, then once at the end.** The worker dispatch passes a
   non-fatal `on_process_sample` callback through `WorkerRequest` and
   `run_process`; it fires on each streaming wait-loop iteration and is
   throttled to `SAMPLE_INTERVAL_SECONDS` (2 s, overridable for rigs with
   `SABEROPS_JOB_PROCESS_SAMPLE_SECONDS`). The first sample is taken as soon as
   the process-start callback supplies the fenced identity. After the worker
   returns, `finish()` takes a final sample, computes the verdict and persists
   it. Both the primary and the repair dispatch paths wire it.
4. **A port is attributed by socket inode, not by port number alone.** For each
   live member of the worker's process group, the recorder joins the socket
   inodes it holds to the LISTEN rows of `/proc/net/tcp` and `tcp6`. A port
   rebound by an unrelated process is therefore never reported as the job's.
5. **The leftovers verdict is three-valued plus "not observable".**
   `none` (nothing from the job is still running or listening), `leftovers`
   (a live group member or a recorded socket inode still in LISTEN),
   `not_observable` (a host-run native worker, which the engine never launches)
   and `unknown` (evidence unreadable, or the recording failed). The detail
   names every leftover process and listening socket and records the honest
   limits: processes that left the group, sockets of a shared service, and the
   native worker are `unknown`/`not_observable`, never zero.
6. **Engine service ports are excluded by name.** A socket whose port matches
   `ENGINE_SERVICE_PORTS` (the web server on 8765; MCP is stdio and owns no
   listening socket) is not recorded as a job port; the exclusion is named in
   the verdict detail.
7. **One verdict per attempt.** The verdict is written when the worker process
   group ends, which is the job-end observation point for the run's final
   attempt; the helper exposes the latest verdict for a run. A job with several
   attempts keeps one honest record per attempt.

## 3. Consequences

- The owner can check "only stop processes you started" against evidence: a
  descendant that daemonized out of the group and kept a listener is named
  after the job ends without the engine touching it.
- A detached process that holds no socket is not observable, and a native
  worker is never observable; both are recorded as limits rather than a clean
  result.
- Sampling writes at most one upsert per live member and per listening socket
  per interval; the reads are the same reduced `/proc` surface the monitor
  already uses.
- Phase 2's worker detail reads these tables; no board code lives here.

## 4. Not in this lane

Killing or cleaning up leftovers; ports on another host; commit provenance
(H44, ADR-0102); any board code.