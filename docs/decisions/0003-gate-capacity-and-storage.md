# ADR-0003: Deterministic gate evidence, host capacity admission and the owned-artifact lifecycle

- **Status:** Reconstructed index record. Not owner-accepted; no original text is
  recoverable from this repository's history.
- **Date reconstructed:** 2026-11-19, from the shipped implementation and its
  live documentation.
- **Related:** [ADR-0009](0009-gate-and-test-policy.md),
  [ADR-0007](0007-dynamic-memory-budget.md),
  [ADR-0005](0005-mcp-first-engine-and-phase-2-board.md) §4.4.

> **Reconstruction notice.** No `docs/decisions/0003-*.md` file exists now and
> none exists anywhere in the Git history of this repository, so the ADR body,
> its option analysis and its acceptance record cannot be recovered. What
> follows is a citation-resolving index built from the shipped implementation,
> docstrings and live docs pages for every identifier the repository cites as
> "ADR-0003", including Amendments 1 and 2. Nothing here records an owner decision, an approval, or a vote:
> the original decision text was never committed, and two values the
> implementation treats as deliberately *unapproved* (the evidence retention
> anchor and the adaptive-admission margin) are called out as such below rather
> than being presented as settled policy.

## Rule index

Each row states the rule as the shipped code actually enforces it, not as an
original ADR paragraph would have phrased it.

| ID | Rule as shipped | Authority (live doc / symbol) |
| --- | --- | --- |
| **D1** | A gate verdict is a closed deterministic vocabulary (`PASS`, `FLAKY_PASS`, `FAIL`, `TIMEOUT`, `INFRA`) and acceptance is fail-closed: unknown, missing, omitted or self-contradictory evidence becomes `BLOCKED`, never green. `FAIL` is a code failure that carries the exact failing node IDs and is repair-loop input; `INFRA` is never a pass and never repair input. Only `producer: engine` evidence is authoritative — a model's own run of the same commands is G0. | `saberops.gate_outcome_projection.project_gate_outcome` / `GateEvidence` / `ProjectedDecisionKind`; `docs/gate.md` "Outcomes and evidence"; `tests/test_gate_engine_execution.py::test_engine_g1_fail_is_repair_input`. |
| **D2** | Three gate profiles bound each lifecycle point: `fast` (lint + types + selected tests, 600 s), `acceptance` (same commands plus rerun rule and test-map freshness, 1200 s), `full` (lint + types + whole suite, 1800 s). Profiles are project data (`gate-profiles.json` may override commands and add a `selector_command` plugin), but the lifecycle-to-profile mapping is engine policy — `G1`/`G2` → `fast`, `G3`/`main-gate`/`manual` → `full` — and a caller may only strengthen the request, never weaken it. A malformed override, unknown key, unknown profile or weaker request raises `GateProfileError` and the gate does not run; every profile must carry a wall budget of at least 1 s. | `saberops.gate_profiles.resolve_profile` / `required_profile` / `GateProfile.__post_init__` / `default_profiles`; `docs/operations.md` "Profiles"; `tests/test_gate_profiles.py`. |
| **D3** | Gate concurrency is one host-wide reservation pool shared by every SaberOps copy, shell gate and engine gate: G3/main-gate take the exclusive heavy grant (which drains every light index), G1/G2 take one light grant, all flock-backed and released automatically when the owner dies. Waiting is bounded by `SABEROPS_GATE_SLOT_WAIT`; expiry, or a malformed wait/slot configuration, is typed `INFRA SLOT_WAIT_TIMEOUT`, never a silent downgrade or deadlock. | `saberops.gate_runner.GateSlotPool` (delegating to `scripts/gate_tools.MemoryLedger`); `docs/gate.md` "Resource knobs"; `tests/test_gate_engine_execution.py::test_engine_gate_slot_timeout_is_infra`. |
| **D4** | One free-space floor, `max(min_gb, 10 % of total)` with `min_gb` defaulting to 10 GiB, is consulted before any dispatch, reservation, claim transition, gate backend choice, scratch creation or subprocess. A below-floor reading is typed INFRA `DISK_LOW` and an unreadable capacity is `DISK_UNKNOWN`; both park with zero jobs moved out of QUEUED, zero ledger rows and zero launches, and never fabricate a PASS. Startup maintenance runs before the first tick in both `--once` and continuous modes, every tick re-checks, and a DISK_LOW pass runs bounded permitted GC then re-reads the real filesystem — still low or unknown stays a typed park. | `saberops.storage_maintenance.current_disk_floor_bytes` / `StorageMaintenance.launch_guard_reason`; `saberops.capacity.loop.run_loop`/`tick` `launch_guard`; `saberops.gate_runner.default_gate_disk_guard`; `docs/storage-lifecycle.md` "Scheduled maintenance and launch guards". |
| **D5** | Only the node IDs that failed the first pass are rerun, once, serially and without xdist; `FLAKY_PASS` means every first-pass failure passed on that rerun and no new failure appeared. Flaky outcomes are tracked in one shared per-checkout history line (with `kind`, `producer`, `profile`) and an unquarantined serially-passing flake still FAILs the full gate. G3 accepts `FLAKY_PASS` only when every flaky test is owner-quarantined; G1/G2 accept it without quarantine evidence. | `scripts/gate_tools.evaluate` (flaky/unquarantined computation) and `history_path`; `saberops.gate_outcome_projection.project_gate_outcome` G3 branch; `docs/gate.md` "Test policies"; `tests/test_gate_outcome_projection.py::test_full_evidence_flaky_pass_g3_quarantined_accepted_direct`. |
| **D6** | Owned artifacts are inventoried from persisted provenance only (run/attempt rows, candidate lifecycle, execution-owner lease, jobs) and become deletable only when ownership is proven and the path is a strict descendant of an owned root; unknown ownership, missing metadata, naive timestamps, outside-root or root-equal paths and symlink escapes all KEEP with a machine-readable reason. Retention: evidence compresses 7 d after creation, and deletion is scoped only to terminal runs (accepted 90 d, other terminal 30 d from terminal time) but stays held until a policy settles the anchor (see Q4); checkpoint refs go 7 d after the owning job is terminal and must match the owning job id exactly; gate scratch is eligible at end of gate, with only the newest failed scratch per run kept under a 2 GiB total cap, and sweeping requires an approved policy (Amendment 1 §7); attempt worktrees are eligible only when the attempt is terminal and the candidate accepted/rejected/superseded (kept while review is pending or the job is preempted but not terminal); worktree venvs die with their worktree. Owner-DB `VACUUM` is weekly and only above a 20 % free-page ratio, owned caches are reported but never deleted, and orphan reclamation is deferred until engine ownership records exist. | `saberops.storage_lifecycle.plan_gc` + frozen constants; `saberops.storage_inventory.build_inventory` / `storage_owned_roots`; `saberops.storage_gc.preview_gc`; `docs/storage-lifecycle.md` "What is owned" / "Evidence compression". |
| **D7** | Every SQLite write path shares one policy: WAL, a 30 s `busy_timeout`, and `BEGIN IMMEDIATE` with bounded retry/backoff under a **single** shared 30 s deadline covering acquire, statements and commit (never N × 30 s), with explicit commit on success and rollback on failure. Reads open genuinely read-only (`mode=ro`) and never take a write lock. Escaping contention is a typed `SqliteBusyError` (`SQLITE_BUSY`, also a `sqlite3.OperationalError`) surfaced as storage INFRA to the owning scheduler/run boundary — never a crash, never a duplicate launch — and the same helpers back the pre-existing mutators (`Database.write_transaction`, `capacity.store.owner_write_connection`). | `saberops.sqlite_policy.immediate_transaction` / `write_connection` / `read_connection` / `BUSY_TIMEOUT_MS`; `saberops.db.Database.write_transaction`; `docs/storage-lifecycle.md` "D7 SQLite robustness". |
| **Q1** | The floor question is answered by keeping the existing fixed `max(min_gb, 10 %)` guard (10 GiB default), implemented once as a read-only consumer (`current_disk_floor_bytes`) so the developer gate and the storage service can never disagree, and never lowered or reimplemented weaker. Replacing it with per-filesystem adaptive admission is the separate, owner-gated Amendment 2 answer, not part of this question's resolution. | `saberops.storage_maintenance.current_disk_floor_bytes`; `docs/storage-lifecycle.md` "Current floor versus adaptive admission"; `tests/test_storage_maintenance.py::test_current_floor_matches_the_live_guard`. |
| **Q4** | Evidence retention is: compress 7 d after creation (non-destructive, anchored at creation), delete 90 d after terminal time for accepted runs and 30 d after terminal time for other terminal runs, with an explicit timezone-aware terminal timestamp required. The terminal anchor itself is an **unapproved** conservative safety floor: the implementation records that ADR-0003 D6/Q4 does not settle the canonical anchor and refuses evidence deletion until an owner policy settles it. | `saberops.storage_lifecycle` module docstring + `EVIDENCE_COMPRESS_AFTER` / `EVIDENCE_ACCEPTED_RETENTION` / `EVIDENCE_OTHER_RETENTION`; `saberops.storage_gc.GcPolicy.evidence_anchor`; `tests/test_storage_lifecycle.py::test_evidence_accepted_90d_boundary`. |
| **Q6** | The main gate is not tied to the night: `scripts/main-gate.sh` runs the full gate on `main` on demand (by hand, before a wave, or from an optional systemd timer), is deterministic, runs no model and is idempotent — it skips when `main` is unchanged since the last green run unless explicitly repeated. It also rebuilds the coverage map and keeps flaky statistics. | `docs/operations.md` "Main gate"; `scripts/main-gate.sh` header (step 3 skip rule, optional `saberops-main-gate.timer`). |
| **§2** (bare section reference) | Section-level statement of the flake rule: contention flakes pass when the failing tests pass on a serial rerun. The two timing/process-control modules (`tests/test_parallel_work.py`, `tests/test_session_control.py`) keep `pytest.mark.serial` as documentation only — the gate has no serial tier — so the operative behaviour is D5's single serial rerun of the failing node IDs. | Comments in `tests/test_parallel_work.py` / `tests/test_session_control.py`; `docs/gate.md` "Test policies"; `scripts/gate_tools.evaluate`. |
| **Amendment 1 §1** | Inventory authority comes from real persisted records only — run/attempt rows, candidate lifecycle projection, execution-owner lease, job rows, exact run id or exact persisted candidate SHA — never from a branch name, an age, a PID, or an SHA that merely happens to be an ancestor of `main`. Anything the durable state cannot explain is `UNKNOWN` and kept with a reason; a missing registration, missing repo identity or missing path is never justification to prune, and gate scratch is adopted only from engine-owned scratch roots carrying an owner record. | `saberops.storage_inventory.build_inventory` / `OwnershipState`; module docstring "Authority rules"; `docs/storage-lifecycle.md` "What is owned"; `tests/test_storage_inventory.py`. |
| **Amendment 1 §2** | Every action revalidates immediately before it executes: a dirty worktree, one holding unique ignored content (`.venv/` is the only ignored entry proven disposable), a live or unknown-liveness execution owner, a locked worktree, pending review/acceptance, an active run, an outside-root or root-equal path, a symlink swap or a moved branch tip all refuse. Age and name are never removal authority, and a reused PID is not a live owner. | `saberops.storage_gc.apply_gc` per-action revalidation; `saberops.gate_runner.sweep_gate_scratch` dead-owner contract; `docs/storage-lifecycle.md` "Applying"; `tests/test_storage_gc.py` §2 tests. |
| **Amendment 1 §3** | Retirement uses only the exact native Git operations `git worktree remove` (no force) and then `git branch -d` (no force), in that order; checkpoint refs are deleted with a compare-and-swap on their exact previewed SHA. There is no `git worktree prune`, no `git branch -D`, no `rm -rf` of an unverified path, no remote operation, and only engine-owned refs are in scope (a non-`main` approved target retires cleanly; the legacy `orch cleanup` route keeps the same protections). | `saberops.storage_gc.apply_gc`; `saberops.git.CHECKPOINT_REF_PREFIX` / `GitManager.delete_branch` / `delete_checkpoint_ref`; `docs/storage-lifecycle.md` "Applying" step 4 and "Compatibility route". |
| **Amendment 1 §4** | Intent is persisted before anything destructive: if the intent cannot be written nothing executes (`audit-intent-failed`); a refusal whose audit write fails is still a refusal and is never relabelled applied; a successful removal whose result audit cannot be written reports `audit-uncertain` and stops instead of touching another target. Symlinks are never followed into user data, and provenance and audit rows survive cleanup. | `saberops.storage_gc.apply_gc` / `StorageAuditEvent`; `saberops.db` `storage_cleanup_events`; `docs/storage-lifecycle.md` "Applying" step 5 and "Audit". |
| **Amendment 1 §5** | Maintenance is bounded and lease-aware: startup, hourly (`MAINTENANCE_INTERVAL = 1 h`) and DISK_LOW passes take a short wall budget and recheck the remaining budget before GC, vacuum and sweep, so an expensive earlier phase cannot spill into a later one after the budget is exhausted. On DISK_LOW it runs permitted GC and then re-reads the actual filesystem; a lease-invalid pass is report-only, and hard-linked/shared bytes are never summed as definitely reclaimable. | `saberops.storage_maintenance.StorageMaintenance.run` / `MAINTENANCE_INTERVAL`; `saberops.storage_inventory.measure_path_bytes` hard-link rule; `docs/storage-lifecycle.md` "Scheduled maintenance and launch guards". |
| **Amendment 1 §6** | `--apply` executes only the exact canonical previewed scope, matched by artifact class, reference, identity, action and a digest of every authority-relevant attribute; anything else is refused as `selection-widens-preview`, a repeated target as `duplicate-selection`, and same class/reference/identity without the pinned authority data is not permission. A dry run (including on a missing store) is a true no-op: no database, WAL, schema, directory, policy file or write lock, and it adopts nothing. | `saberops.storage_gc.preview_gc` / `apply_gc` / `GcPreview.candidate_for`; `docs/storage-lifecycle.md` "Preview" and "Applying"; `tests/test_storage_gc.py` §6 tests. |
| **Amendment 1 §7** | A validated, owner-scoped standing policy is required for any destructive action. An absent, unapproved (`"approved": false`), corrupt, contradictory or unknown-scope policy means inventory-and-report only: `--apply` is refused, maintenance sweeps nothing, and no gate or worker launch permission is granted. | `saberops.storage_gc.load_gc_policy` / `GcPolicy`; `saberops.storage_maintenance.StorageMaintenance.run`; `docs/storage-lifecycle.md` "Standing policy"; `tests/test_storage_gc.py::test_no_policy_authorizes_nothing`. |
| **Amendment 1 §Acceptance** | The acceptance bar is dry-run no-op plus crash-safe repetition: a run interrupted between the worktree and branch steps is recovered by re-running the same preview (the worktree step reports `already-missing` and the branch step completes), and an interrupted or repeated cleanup is safe and idempotent. | `saberops.storage_gc.apply_gc` recovery path; `docs/storage-lifecycle.md` "Audit"; `tests/test_storage_gc.py::test_already_missing_and_repeat_invocation_are_idempotent`. |
| **Amendment 1 "retention-anchor caveat"** | Evidence deletion stays held while the retention anchor is unsettled; `allow_evidence_deletion` additionally requires an explicit `evidence_anchor` of `"terminal"` or `"accepted"`, and even then the implementation treats the anchor as an unapproved safety floor rather than an ADR decision. Compression is non-destructive and does not need the anchor. | `saberops.storage_gc.load_gc_policy` / `GcPolicy.evidence_anchor`; `saberops.storage_lifecycle` module docstring; `docs/storage-lifecycle.md` "Standing policy". |
| **Amendment 2** | Concurrent runs admit against one owner-only, host-wide SQLite reservation ledger guarded by one bounded `flock`: per filesystem, `required = authorised recovery margin + sum(active remaining growth) + candidate bound`, compared with live `statvfs` inside the same critical section, and a reservation is durably committed (`synchronous=FULL`) before any expensive work starts. Keys are idempotent per installation/session/attempt/phase and fenced by process identity plus generation, so a proven-dead owner can be taken over while a live foreign incarnation, copied token, stale generation or corrupt/foreign/tampered row cannot; settlement is idempotent and single-writer. Unknown estimates, an unapproved margin policy, misidentified or aliased filesystems, lock contention and unreadable state all fail closed with typed `DISK_BUDGET_*` and reserve nothing; observed owned bytes reduce the bound, shared/uncertain caches keep the conservative bound, and an observed overshoot pauses only new starts, never kills a running job. | `saberops.storage_budget` module docstring, `StorageLedger.try_reserve` / `renew` / `settle` / `assess` / `observe`, `compute_required_free`, `load_margin_policy`; `tests/test_storage_budget.py` (≈60 contract tests). |
| **Amendment 2 §Rollout** | This slice is deliberately **not** wired into `scripts/gate_tools.py` or the engine capacity loop: the existing live free-space floor stays binding on every production path, adaptive admission is labelled `inactive` in telemetry, and the placeholder recovery margin is shadow-only (`margin_authorised=False`) and can never authorise an admission. Activation requires an owner-approved numeric margin policy plus the G1/review/G3/activation proof and a separately reviewed integration of every launch path. | `saberops.storage_budget` docstring "Deliberate scope limits" + `SHADOW_MARGIN_BYTES`; `docs/storage-lifecycle.md` "Current floor versus adaptive admission". |
| **"Amd.1"** (alias) | Shorthand for Amendment 1 in the certification module: safe accepted-wave closure and bounded storage cleanup — native `git worktree remove` without force, `git branch -d` after verification, never `-D`, never a repo-wide prune, no deletion of owner branches/unmerged/recovery/remote refs, every target inside an owned root, and audit plus recovery checkpoints survive. | `saberops.certification` module docstring and its wave-closure section; `docs/end-certification.md` "Safe wave closure and bounded storage cleanup". |

## Identifiers that are *not* ADR-0003 content

These tokens are cited next to ADR-0003 but belong to other records; they are
listed so a reader does not mistake them for missing ADR-0003 sections.

| Token | What it actually is |
| --- | --- |
| `GS-1` | Step 008 work-item id for the storage lifecycle ("gate/storage"); `docs/storage-lifecycle.md` is titled "Storage lifecycle (Step 008 / GS-1)". Used as a parent-step reference, e.g. `saberops.sqlite_policy` ("ADR-0003 D7, GS-1"). |
| `A8` | Phase-1 lane id from ADR-0007 (dynamic memory budget), cited as "ADR-0003 D3 + A8" in `saberops.gate_runner.GateSlotPool` because the D3 slot pool delegates to the A8 memory ledger. |
| `review F5` | A review-finding id on the gate-scratch sweep contract (`saberops.gate_runner.sweep_gate_scratch`), not an ADR identifier. |

## Citations found

Literal `ADR-0003` appears on 266 lines across `*.md` and `*.py` (excluding
`.venv`). Counts below are lines containing the identifier and overlap, since
one line can cite several identifiers.

| Identifier | Lines | Example file |
| --- | --- | --- |
| `D1` | 25 | `tests/test_gate_engine_execution.py` |
| `D2` | 12 | `src/saberops/gate_profiles.py` |
| `D3` | 5 | `tests/test_gate_engine_execution.py` |
| `D4` | 31 | `src/saberops/storage_maintenance.py` |
| `D5` | 25 | `scripts/gate_tools.py` |
| `D6` | 49 | `src/saberops/storage_lifecycle.py` |
| `D7` | 25 | `src/saberops/sqlite_policy.py` |
| `Q1` | 4 | `tests/test_storage_maintenance.py` |
| `Q4` | 7 | `tests/test_storage_lifecycle.py` |
| `Q6` | 1 | `docs/operations.md` |
| `§2` (bare) | 2 | `tests/test_parallel_work.py` |
| `Amendment 1` | 58 | `src/saberops/storage_gc.py` |
| `Amendment 1 §1` | 7 | `src/saberops/storage_inventory.py` |
| `Amendment 1 §2` | 13 | `tests/test_storage_gc.py` |
| `Amendment 1 §3` | 6 | `tests/test_storage_gc.py` |
| `Amendment 1 §4` | 8 | `tests/test_direct_api.py` |
| `Amendment 1 §5` | 6 | `tests/test_storage_integration.py` |
| `Amendment 1 §6` | 6 | `tests/test_storage_gc.py` |
| `Amendment 1 §7` | 4 | `tests/test_storage_gc.py` |
| `Amendment 1 §Acceptance` | 2 | `tests/test_storage_gc.py` |
| `Amendment 1 retention-anchor caveat` | 1 | `tests/test_storage_gc.py` |
| `Amd.1` | 1 | `src/saberops/certification.py` |
| `Amendment 2` | 72 | `src/saberops/storage_budget.py` |
| `Amendment 2 §Rollout` | 1 | `src/saberops/storage_budget.py` |
| `GS-1` (cross-reference) | 1 | `src/saberops/sqlite_policy.py` |
| `A8` (cross-reference) | 1 | `src/saberops/gate_runner.py` |
| `review F5` (cross-reference) | 1 | `src/saberops/gate_runner.py` |

Highest-density files: `tests/test_storage_budget.py` (63), `tests/test_storage_gc.py`
(30), `tests/test_gate_outcome_projection.py` (23), `tests/test_storage_lifecycle.py`
(18), `tests/test_storage_maintenance.py` (17), `tests/test_storage_inventory.py`
(16), `tests/test_storage_integration.py` (16), `tests/test_sqlite_contention.py`
(14), `src/saberops/storage_lifecycle.py` (13).

## Reconstruction gaps and confidence

1. **No original text exists.** `git log --all -- 'docs/decisions/0003*'` is
   empty; the only commits that name ADR-0003 are `52a8e2d` (D6/Q4 planner),
   `adffee3` (Amendment 2 ledger), `519fdea` (GS-1 lifecycle, D7) and `c4aeba0`
   (D2/D3 gate profiles), and their messages paraphrase only the sub-decisions
   they implemented. No acceptance date, decider list, alternatives or vote is
   recoverable, and none is asserted above.
2. **D1 wording is the weakest reconstruction.** The identifier is only ever
   cited for outcome/evidence behaviour (FAIL carries failing IDs, G1
   `FLAKY_PASS` needs no quarantine, complete evidence yields PASS). The row
   therefore states the shipped decision contract of
   `gate_outcome_projection`, which is the only consistent reading; an original
   D1 may have been narrower (for example only the verdict taxonomy).
3. **Bare `§2` is ambiguous.** Only two comments cite it, both about
   "contention flakes pass when run serially". It may be a section of the ADR
   body that later became D5, or a section about deterministic reruns generally.
   Any statement beyond the rerun rule would be invention.
4. **Q2, Q3 and Q5 are never cited** anywhere in the repository. They cannot be
   reconstructed at all; if the original record had five or more questions,
   only Q1, Q4 and Q6 survive as usage.
5. **Two values are explicitly unapproved in code**, so no citation resolves to
   an authoritative owner decision for them: the evidence retention *anchor*
   (D6/Q4, held until an owner policy settles it) and the adaptive-admission
   *margin* (Amendment 2, shadow-only until owner calibration). Both are marked
   as caveats in the table rather than presented as settled ADR text.
6. **Amendment 2 is summarised, not enumerated.** Its ~60 test docstrings assert
   distinct sub-contracts (alias dedup, multi-device order, lease/PID reuse,
   replay, fencing, backpressure, profile isolation, symlink redirection,
   schema versioning). The row states the load-bearing rules; each individual
   assertion is authoritative only through `tests/test_storage_budget.py`.
7. **Aliases and pairings exist.** `Amd.1` and
   `Amendment 1` refer to the same guarded-lifecycle amendment, and `D4/Q1`
   is always cited as a pair (the floor question is already answered by D4). No identifier was
   found that could not be given a defensible shipped rule; the uncertainties
   are about original wording and scope, not about current behaviour.
8. **One live inconsistency is recorded rather than smoothed over.**
   `docs/gate.md` describes `FLAKY_PASS` as "every first-pass failure passed
   once serially and is quarantined", while
   `gate_outcome_projection.project_gate_outcome` deliberately accepts an
   unquarantined `FLAKY_PASS` for G1/G2 and rejects it only for G3. The D5 row
   states the code's behaviour (G3-only quarantine); a future ADR-0003 restore
   should reconcile the doc line or the projection, not cite the table as proof
   that they already agree.
