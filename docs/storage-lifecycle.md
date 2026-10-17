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

`--apply` executes only the **exact previewed scope**, re-resolved immediately
before each action:

1. Determine the run/attempt/owner facts again from the database.
2. Re-resolve every path; a symlink swap, root change or outside-root move is
   refused, and a path equal to an owned root is protected.
3. Refuse a dirty worktree, one holding unique ignored content (`.venv/` is the
   only ignored entry proven disposable), a live execution owner, a locked
   worktree, a moved branch tip or a not-accepted-reachable branch.
4. Retire with native Git only: `git worktree remove` (no force) and then
   `git branch -d` (no force), in that order. No `git worktree prune`, no
   `git branch -D`, no `rm -rf` of an unverified path and no remote operation.
5. Audit intent, result and failure for every action.

`--apply` requires a validated standing owner policy. Without one it is
refused. A preview cannot be widened: a candidate that is not exactly in the
previewed set is refused, and a stale preview is caught by the per-action
revalidation.

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
  "allow_evidence_deletion": false,
  "allow_evidence_compression": false,
  "allow_vacuum": false
}
```

A malformed, contradictory or unknown-scope policy is refused, never silently
adopted. `allow_evidence_deletion` additionally requires an explicit
`evidence_anchor` (`"terminal"` or `"accepted"`); while the retention anchor
remains unsettled, evidence deletion is refused even if the flag is set.

## Scheduled maintenance

`orch scheduler run` invokes bounded storage maintenance on startup and at most
once per hour, and the same service is used on the DISK_LOW path. It:

* sweeps gate scratch whose owner PID is provably dead or that is older than
  24 h, only inside known scratch roots;
* runs permitted worktree/branch retirement under a standing policy;
* runs the owner-DB `VACUUM` at most weekly and only when the free-page ratio
  exceeds 20 %;
* rechecks the **actual** filesystem after a DISK_LOW pass. If space is still
  below the floor or unknown, the result is a typed `DISK_LOW` infrastructure
  park: no worker and no gate is launched, no LLM repair is dispatched and no
  PASS is fabricated.

Maintenance respects the scheduler lease (`lease_valid=False` makes it
report-only), keeps its own wall-clock budget so it cannot monopolise a tick,
and never raises into the scheduler loop.

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
owning scheduler/run boundary, never a crash and never a duplicate launch. The
main database helper and `capacity.store` share the same timeout.

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
