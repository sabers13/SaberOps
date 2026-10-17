# Storage lifecycle (Step 008 / GS-1)

Owner-aware inventory, retention and guarded garbage collection for the local
artifacts SaberOps creates on one host: attempt worktrees, engine local
branches, checkpoint refs, gate scratch, worktree venvs, run evidence and the
owner database. It implements ADR-0003 D4/D6/D7 and Amendment 1, and consumes
the SB-1 reservation ledger (Amendment 2) without activating adaptive
admission.

Nothing here runs automatically against unowned data. A missing standing owner
policy means **inventory and report only**.

## What is owned

Ownership comes from real persisted provenance, never from a branch name, an
age, a PID or an SHA that merely happens to be an ancestor of `main`:

| Evidence | Meaning |
|---|---|
| `runs` / `attempts` rows | the run and attempt that created a worktree or branch |
| candidate lifecycle projection | `ACCEPTED` / `REJECTED` / `STALE` / pending review |
| `execution_owners` lease | a live or unknown-liveness owner still using the artifact |
| `jobs` table | an open job waiting on the artifact (including a preempted one) |
| candidate commit SHA | links a `gate-evidence/candidates/<sha>` bundle to its run |
| run id / terminal time | links evidence and checkpoint refs to their terminal run/job |

Inventoried classes: attempt worktrees, engine local branches, checkpoint refs,
gate scratch, worktree venvs, run evidence and the owner database. Gate scratch
is adopted only from engine-owned scratch roots (the configured scratch root,
the state `gate-tmp`, `/dev/shm`) and only when it carries an owner record; a
directory is never adopted from its name or its age. Evidence is adopted only
when its name is an exact run id or an exact persisted candidate SHA.

Anything the durable records cannot explain is `UNKNOWN` and is **kept** with a
reason. A missing registration or a missing path is never justification to
prune.

## Preview (`orch gc --dry-run`)

```bash
orch gc --dry-run                 # inventory + eligible scope for the current repo
orch gc --dry-run --json          # machine-readable
orch gc --dry-run --class attempt-worktree --class local-branch
orch gc --dry-run --repo /path/to/repo --db /path/to/orch.db
```

A dry run is a true no-op: it creates no database, no WAL file, no schema, no
directory and no policy file, and it takes no write lock. It prints, for every
artifact, its class, provenance ownership, reason and byte estimate, then the
exact candidates the current policy would allow together with the exclusions
and their reasons. Missing state is reported `UNKNOWN`, never adopted.

## Applying (`orch gc --apply`)

`--apply` executes only the **exact canonical previewed scope**. A selection is
matched against the preview by artifact class, reference, identity, **action**
and a digest of every authority-relevant attribute; anything else is refused as
`selection-widens-preview`, a repeated target is refused as
`duplicate-selection`, and the executed object is always the immutable previewed
candidate, never the caller's instance. Immediately before each action the
service rechecks policy approval/scope and re-resolves all authority:

1. Determine the run/attempt/owner facts again from the database.
2. Re-resolve every path; a symlink swap, root change or outside-root move is
   refused, and a path equal to an owned root is protected.
3. Refuse a dirty worktree, one holding unique ignored content (`.venv/` is the
   only ignored entry proven disposable), a live execution owner, a locked
   worktree, a moved branch tip or a not-accepted-reachable branch.
4. Retire with native Git only: `git worktree remove` (no force) and then
   `git branch -d` (no force), in that order. A checkpoint ref is deleted with a
   compare-and-swap on its exact previewed SHA. No `git worktree prune`, no
   `git branch -D`, no `rm -rf` of an unverified path and no remote operation.
5. Audit intent, result and failure for every action. **Intent is written before
   anything destructive**: if the intent cannot be persisted, nothing is
   executed and the candidate is refused (`audit-intent-failed`). If the action
   succeeds but the result audit cannot be persisted, the action is reported as
   `audit-uncertain` (never silently retried against another target) and both
   text and `--json` modes exit non-zero. A refusal whose audit write fails is
   still a refusal, never relabelled applied.

`--apply` requires a validated standing owner policy. Without one it is
refused. A preview cannot be widened, and a stale preview is caught by the
per-action revalidation.

## Standing policy

The policy file lives at `$XDG_CONFIG_HOME/saberops/storage_gc.json`
(the unprofiled owner settings path). Absent or `{"approved": false}` means
report-only.

```json
{
  "approved": true,
  "version": "1",
  "scope_classes": ["attempt-worktree", "local-branch", "worktree-venv"],
  "approved_target_ref": "refs/heads/main",
  "allow_worktree_retirement": true,
  "allow_branch_retirement": true,
  "allow_checkpoint_deletion": false,
  "allow_scratch_deletion": false,
  "allow_evidence_deletion": false,
  "allow_evidence_compression": false,
  "allow_vacuum": false
}
```

A malformed, contradictory or unknown-scope policy is refused, never silently
adopted. `allow_evidence_deletion` additionally requires an explicit
`evidence_anchor` (`"terminal"` or `"accepted"`); while the retention anchor
remains unsettled, evidence deletion is refused even if the flag is set.
`allow_evidence_compression` is non-destructive and does not need the anchor.

## Evidence compression

When `allow_evidence_compression` is set, evidence older than the 7-day
compress threshold is archived in place: a directory becomes `d.tar.gz` and a
file becomes `f.gz`, written to a temp file, fsynced and atomically renamed
before the source is removed. A crash before the final remove leaves both the
archive and the source, and a repeat run reports `already-compressed` without
duplicating work. Readable content and structure round-trip; deletion of
evidence remains held.

## Scheduled maintenance and launch guards

`orch scheduler run` runs bounded storage maintenance on startup (before any
tick, in both `--once` and continuous modes) and at most once per hour, and
every tick additionally consults the launch guard before any dispatch,
reservation or claim transition. A below-floor or unknown reading therefore
parks with **zero** jobs moved out of QUEUED, zero ledger rows booked and zero
launches — startup, periodic and per-tick are all covered, and construction
failure of the guard degrades to a typed reason rather than silently disabling
the check. The same guard is applied on the gate path
(`GateRunner.run` raises a typed `DISK_LOW` block before any backend choice,
scratch creation or subprocess).

Maintenance:

* sweeps gate scratch only when the recorded owner is **provably dead** — a
  live owner, an unknown/unrecorded owner, a symlinked entry or a path that
  resolves outside the scratch root is never touched, and age/name is never
  authority; the pass is bounded by the tick budget;
* runs permitted worktree/branch/scratch retirement under a standing policy;
* runs the owner-DB `VACUUM` at most weekly and only when the free-page ratio
  exceeds 20 %;
* rechecks the **actual** filesystem after a below-floor pass. If space is still
  below the floor or unknown, the result is a typed `DISK_LOW`/`DISK_UNKNOWN`
  infrastructure park: no worker and no gate is launched, no LLM repair is
  dispatched and no PASS is fabricated.

Maintenance respects the scheduler lease (`lease_valid=False` makes it
report-only) and never raises into the scheduler loop.

## Current floor versus adaptive admission

The fixed free-space floor stays effective on every production path. It is
implemented once in `saberops.storage_maintenance.current_disk_floor_bytes`
(`max(min_gb, 10 %)`) and consumed by both the developer gate and the storage
service, so the two can never disagree.

Adaptive admission (per-filesystem reservations from `storage_budget.py`) is
**not activated** by this service. It is labelled `inactive` in telemetry and
the shadow comparison remains shadow-only. Replacing the floor requires every
gate and engine launch path to participate, owner-approved calibration and the
G1/review/G3/activation proof of ADR-0003 Amendment 2.

## D7 SQLite robustness

`saberops.sqlite_policy` is the single implementation of the D7 write policy:
WAL, a 30 s `busy_timeout`, and `BEGIN IMMEDIATE` with bounded retry/backoff
under one shared 30 s deadline covering acquire, statements and commit. Reads
open genuinely read-only (`mode=ro`) and never take a write lock. A lock that
still escapes is a typed `SqliteBusyError` (`reason_code SQLITE_BUSY`) for the
owning scheduler/run boundary, never a crash and never a duplicate launch.

The same policy is used by the **existing** mutators, not only the new storage
audit writes: `Database.write_transaction()` backs `create_run`, `update_run`,
`create_attempt`, `update_attempt`, `create_check`, `record_event`, the
execution-owner claim/terminalize/cancel fenced helpers, the owner-action claim
and the candidate-rejection CAS; `capacity/store.owner_write_connection()`
backs the capacity exclusions and probe writers. `SqliteBusyError` is also a
`sqlite3.OperationalError`, so existing `except OperationalError` handlers keep
working while the new typed handlers park cleanly.

## Audit

Every action is recorded in `storage_cleanup_events` with timestamp, phase
(`intent` / `result` / `failure`), action, artifact class, exact reference and
identity, run id, project id, SHA, scoped policy reference, reason, and
estimated versus actual attributable bytes. A crash between the worktree and
branch steps is recoverable: re-running the same preview reports the worktree
step as `already-missing` and completes the branch step. If a removal succeeds
but the audit write fails, the result surfaces `audit_uncertain` and stops
instead of touching another target.

## Compatibility route

`orch cleanup <run-id>` delegates to the same guarded implementation as
`orch gc`: the same dirty / ignored-unique / symlink / live-owner / outside-root
protections, native `git worktree remove` without force, and no repository-wide
prune. Candidate branches and database records are preserved.
