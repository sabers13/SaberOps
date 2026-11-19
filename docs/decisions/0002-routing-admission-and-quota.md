# ADR-0002: Routing, admission, scheduling and quota preemption

- **Status:** Reconstructed index record. Not owner-accepted; no original text is
  recoverable from this repository's history.
- **Date reconstructed:** 2026-11-19, from the shipped implementation at `ba34a92`
  and its descendants.
- **Related:** [ADR-0001](0001-saberops-engine-mcp-first.md),
  [ADR-0005](0005-mcp-first-engine-and-phase-2-board.md) §3,
  [ADR-0013](0013-quota-failover-continuation.md) (quota-exhausted continuation,
  the in-run counterpart to D8's job-level preemption)

> **Reconstruction notice.** ADR-0002 was cited in roughly 245 places across
> docs, source docstrings and tests, but its text was never committed; only
> ADR-0001 and ADR-0005 onward exist under `docs/decisions/`. This record was
> reconstructed by reading the code each citation points at, so it describes the
> shipped implementation rather than an approved decision. Sub-identifiers appear
> only where the repository already cites them, and numbering, section boundaries
> and any superseded wording are unrecoverable. No owner approval is implied;
> unresolved gaps are listed at the end and belong to the owner.

## Decisions as shipped

| ID | Rule as shipped | Authority (live doc / symbol) |
| --- | --- | --- |
| **D1** | A route selected through an explicit binding draws **its binding pool plus every configured pool that declares that route as a member** (declared shared scarcity). Binding must never hide a second pool's constraint: one exhausted sibling pool denies the route, and a bound route with no member match does **not** fall back to the synthesized provider-wide default pool. | `saberops.capacity.admission.admit` (the `binding_pool_id is not None` branch and `drawn_pools`), `saberops.control_plane.quota_policy.quota_decision` / `pools_for`; `tests/test_reviewer_admission.py::test_shared_membership_blocked_member_pool_refuses_bound_reviewer`, `tests/test_admission.py::test_in_run_two_pool_binding_does_not_discard_second_pool` |
| **D2** | Billing posture is decided per route from the **most specific** record (exact route > model prefix > provider > backend). It fails closed: no record, no `verified_on`, an expired TTL, or class **X** is never admitted; class **B** (attended-only) and class **D** (metered) are never eligible for unattended scheduler dispatch and require an explicit owner override in-run; an unknown provider (`ALLOWED_WITH_WARNING`) is likewise denied unattended. Posture is evaluated **before** the unconfigured-capacity grace, so a blank owner DB can never convert an unverified route into an admitted one. | `saberops.control_plane.posture.posture_decision`, `saberops.capacity.admission.admit` step 6 ("Billing posture") and its `POSTURE_UNVERIFIED` block; `docs/operations.md` §"Plans and billing posture" |
| **D4** | Quota-pool enforcement: mode **OFF** denies unconditionally; an active exclusion denies in every mode; a pool declared `overage_possible` requires **fresh evidence** (a `FRESH` observation with a remaining percentage) or admission returns `QUOTA_EVIDENCE_REQUIRED` with a probe request when the harness is known; admission denies when `remaining − projected_cost × member_multiplier − time_of_day_reserve < red_line_pct`; and a pool plan's `max_concurrent` refuses admission once the per-pool RUNNING count reaches it. | `saberops.capacity.admission.admit` steps 7–8, `saberops.control_plane.quota_policy.QuotaPoolPolicy` / `_red_line_reason`; `tests/test_admission.py::test_admit_pool_mode_off`, `::test_admit_active_exclusion`, `::test_admit_overage_possible_requires_fresh_evidence`, `::test_admit_red_line_and_projected_cost`, `::test_admit_concurrency_limits` |
| **D5** | Per-window accounting. **Reserves** are time-of-day percentages keyed by pool + window + weekday + local hours (with a timezone) and are subtracted from usable headroom; a `RESERVE`-mode red line is the only thing an explicit reserve override bypasses. **Probe observations** are recorded per pool *and window* and folded into the cost model one interval at a time, skipping the row just inserted (by row id) and refusing to infer a delta across a reset. **Charges** count `RESERVED` + `SETTLED` rows only after the latest observation (or after a window roll, which resets the charge to zero), so budget is never double-counted. | `saberops.capacity.admission.reserve_pct`, `saberops.capacity.ledger.record_probe_window_observation` / `_learn_from_probe`, `saberops.capacity.ledger.post_observation_charges`; `tests/test_capacity_ledger.py::test_l10_learn_from_probe_per_window_and_row_id`, `::test_post_observation_charges_preserves_cutoff_and_reset_semantics` |
| **D7** | One canonical, pure admission + scoring policy is shared by **both** dispatch points (the queued scheduler and every in-run worker / continuation / repair / review dispatch). Queued jobs are ordered `URGENT > NORMAL > BACKGROUND`, then deadline presence, earliest deadline, FIFO `created_at`, `job_id`; the **first fallback group with an admitted candidate wins**, and within it the highest binding-window pace wins with ties broken by chain order then lower estimated cost. `pace = (remaining_pct − red_line_pct − reserve_pct − cost_estimate_pct) / max(hours_to_reset, 0.25)`; `BACKGROUND` needs pace ≥ 4.0. Execution is single-instance through a fenced owner-DB lease; a tick claims `QUEUED → RUNNING` by CAS, reserves ledger capacity atomically, and compensates (requeue + release) on launch failure or a lost lease. A RUNNING job with no `current_run_id` or no live `RESERVED` row holds the whole tick with an owner-visible diagnostic rather than being counted as zero occupancy. | `saberops.capacity.admission.admit` / `score` / `order_candidates_for_role`, `saberops.capacity.scheduler.schedule`, `saberops.capacity.loop.acquire_lease` / `scheduler_tick`; `docs/operations.md` §"Pacing scheduler", `tests/test_contract_admission_dispatch.py` |
| **D8** | A **durable job-owned** run that dies of genuine `QUOTA_EXHAUSTED` with no admitted candidate left in any fallback group (a durable `no_eligible_candidate` event) is **preempted, not failed**: WIP is captured as a verified checkpoint commit (or the exact existing HEAD is anchored — never fabricated), anchored under `refs/saberops/checkpoints/<job_id>/<n>`, a factual resume capsule is persisted through the handoff contract, the run becomes `PREEMPTED` (terminal for the run), and the job returns to `QUEUED` with `waiting_reason = PREEMPTED`, a verified `resume_from`, a `not_before` only when an `EXACT`/`PARSED` reset exists, one bounded `preemptions` increment, and exactly one release of the old reservation. Bound is `max_preemptions = 5`; at the limit run and job go terminally `FAILED` with `PREEMPTION_LIMIT`. A standalone run with no job association is never preempted and keeps its truthful failure. | `saberops.preemption.complete_preemption` / `prepare_resume`, `DEFAULT_MAX_PREEMPTIONS`, `saberops.models.RunStatus.PREEMPTED`, `saberops.jobs.models.PREEMPTION_LIMIT`; `docs/operations.md` §"Jobs" |
| **D8 R1** | **Post-requeue capacity settlement.** A job that was requeued, re-dispatched or already terminated can leave its *old* run's `RESERVED` rows charged forever, because `reconcile_jobs` only scans RUNNING jobs. A paged scan starts from durable owner-storage debt and releases only runs that are provably terminal **and** carry an authenticated D8 receipt (bound decision or typed limit tail), independent of `waiting_reason` and of the job's current state. It never frees capacity for the live generation of that run, never touches another job's rows, and the release is lease-guarded inside the same `BEGIN IMMEDIATE` transaction so a stale scheduler writes nothing. | `saberops.jobs.service.settle_preemption_debts`, `saberops.capacity.ledger.list_reserved_debts` / `release_terminal_debt`; `tests/test_contract_job_preemption.py` (R1 cases) |
| **D8 R2** | **Interrupted preemption preparation.** The "D8 owns this outcome" decision is written as a durable receipt **before** any checkpoint/ref/capsule/intent side effect, and it names the exact executor owner generation that made it. Recovery finishes the transaction only for a provably **dead owner + quiescent worker**, only through the fenced path, and only when the receipt authenticates: no receipt means no invented preemption, an unbound legacy intent is not authority, a decision bound to another generation is not authority, and stale recovery cannot requeue a newer run. The whole protocol is idempotent across restarts. | `saberops.preemption.record_preparation_decision` / `authenticate_decision` / `classify_preemption_transaction` / `finish_interrupted_preemption`, `saberops.jobs.service.reconcile_preemptions`, `saberops.db.Database.terminalize_preemption_fenced`, `saberops.capacity.loop.scheduler_tick` step 1b |
| **D8 R3** | **Checkpoint refs are engine-owned and create-only.** Only `refs/saberops/checkpoints/<job_id>/<n>` is produced, with a validated `[A-Za-z0-9_-]{1,64}` job id and a bounded index (≤ 64) — never caller-supplied ref text. Anchoring is an atomic **create-only compare-and-swap** (`git update-ref --no-deref <ref> <sha> <all-zero-oid>`), so an injected direct ref is never overwritten, a **symbolic ref (including dangling) is refused and never dereferenced**, and an existing direct ref with the identical SHA is reused idempotently. Reading and listing apply the same direct-only rule, so a symlink can never be reported as a verified anchor. | `saberops.git.validate_checkpoint_ref`, `GitManager._checkpoint_ref_state`, `GitManager.anchor_checkpoint_ref`, `GitManager.read_checkpoint_ref`; `tests/test_checkpoint_ref_safety.py` |
| **D8 R4** | **Anchor resolution prefers verified newer lineage over a stale recorded anchor.** The recorded checkpoint row is a starting candidate, not an unconditional winner: dirty WIP that extends the recorded anchor is snapshotted; a clean HEAD that equals or descends from the anchor resolves to that newer commit; a clean HEAD that is an ancestor resolves to the anchor; a HEAD neither ancestor nor descendant is **ambiguous lineage and fails closed** rather than discarding or fabricating the resume point. Every returned SHA must be a commit object in the job repository. | `saberops.preemption._resolve_anchor_sha`; `tests/test_preemption.py` (R4 cases), `tests/test_contract_preemption_checkpoint_union.py` |
| **D8 R4-M1** | **Positive worktree ownership proof before any inspection or mutation.** A worktree is trusted only when it is an existing directory, is itself the worktree root (`git rev-parse --show-toplevel`), shares the job repository's canonical common Git dir, and is registered by `git worktree list --porcelain` for it; ownership is re-proved immediately before the mutating call. A foreign repository (even one sharing object SHAs), a subdirectory, a nested checkout or a symlink alias never qualifies, and its index, HEAD and refs stay untouched. Unprovable metadata, or a recorded path that can no longer be proved, declines with no stale-anchor fallback. | `saberops.preemption._verified_worktree_root` / `_git_common_dir` / `_registered_worktree_roots`; `tests/test_preemption.py` (R4-M1 cases) |
| **D8 F1** | **The preemption predicate is narrow, not a blanket rule.** Preemption needs *both* a genuine quota signal and a durable `no_eligible_candidate` event belonging to the **same** run generation; cross-generation pairing must not preempt. Any admitted non-quota failure (gate, timeout, model failure) is disqualifying, a durable `no_eligible_candidate` event alone is not enough, and a quota-only multi-candidate exhaustion still preempts. | `saberops.preemption.has_genuine_quota_signal` / `has_no_eligible_evidence` / `verify_prepared_artifacts`; `tests/test_preemption.py` (F1 cases) |
| **§11** | **Routing is role-chain based, and tiers are labels.** The only routed role is `BUILD`; `--tier` / `--max-tier` are deprecated compatibility aliases that print exactly one deprecation line and select `BUILD` (a `T3` request means "start on this route's `strong_start` setting"). The classifier still labels a run tier for risk/telemetry, but the label never selects a route. | `saberops.routing._role_for_tier_alias` / `resolve_candidate_chain`, `saberops.cli._warn_deprecated_routing_flags`, `docs/operations.md` §"Build routing policy" |
| **§11 item 2** | **Routing documents migrate on load, and the stored file is never rewritten.** Schema 1 merges its `T1.default` / `T2.*` / `T3.*` contexts per tier, then 2 → 3 maps `BUILD` to the stable union of T2, T1 and T3 (T2 order first) and `strong_start` to the T3 order; every route becomes its own fallback group. `approved_candidates` is carried verbatim, the packaged default is the migrated schema-2 document, retired role names also migrate (`WORKER` → `BUILD`, `SURGEON` → `strong_start`, `INTEGRATOR` dropped), and persisted/frozen snapshots keep their original digest and tier provenance so pre-migration runs stay adoptable. | `saberops.routing_config._merge_legacy_to_v2`, `_migrate_v2_to_v3_roles`, `canonical_routing_payload`, `load_effective_routing`, `payload_to_snapshot`; `tests/test_routing_config.py` (§11 item 2 cases) |
| **§11 item 3** | **Every dispatch resolves the `BUILD` chain; escalation changes the setting, not the role.** Escalation moves that same route to `strong_start` only on explicit typed `CAPABILITY_FAILURE` evidence, only with `max_auto_tier == T3`, auto routing and no model override, and it relabels the run `T3`. Exhaustion made only of `no_changes`, timeouts or pre-dispatch skips is not capability evidence, and a repair after a failed check stays on the ordinary build chain. | `saberops.orchestrator` escalation block in the dispatch loop (`ceiling_allows_escalation` / `may_escalate`), `saberops.routing.resolve_candidate_chain(strong_start=…)`; `tests/test_contract_routing_orchestrator.py::test_auto_run_labelled_t1_dispatches_worker_chain_head` |
| **D2 / F1** | Unverified posture fails closed at **both** seams unconditionally: no owner override and no unconfigured-capacity grace can admit it, and a job without any matching record is skipped (`POSTURE_UNVERIFIED`), never launched. This is the fix-up that made posture evaluation precede the capacity grace. | `saberops.capacity.admission.admit` (`POSTURE_UNVERIFIED` block), `saberops.dispatch.evaluate_eligibility`; `tests/test_admission.py::test_posture_first_prevents_unconfigured_fail_open` |
| **D7 / F2** | Outstanding reservations are derived from durable storage and **subtracted before the red-line check**; the queued scheduler simulates both running counts and reservation spend across a batch so one tick cannot over-admit. A failed or partial read is `QUOTA_EVIDENCE_REQUIRED`, never "zero debt"; `{}` is returned only when every required capacity table is provably absent. | `saberops.capacity.admission.admit` (`reservations` / `post_observation_charges`), `saberops.capacity.scheduler.schedule` (`simulated_reservations`), `saberops.capacity.ledger.post_observation_charges`, `saberops.capacity.admission.check_capacity_tables_exist`; `tests/test_admission.py` (F2 cases) |
| **D7 / F3** | The **global aggregate ceiling** counts RUNNING top-level jobs (including ones with no reservation row) plus same-tick simulated starts, independent of pool mapping. Effective ceiling = owner `concurrency.max_global`, else interim default **15**; `--max-concurrent N` and programmatic injection are **lowering-only** (`min`), and any invalid value (0, negative, bool, non-int, a `concurrency` block without `max_global`) fails closed instead of degrading to unbounded. Per-pool `max_concurrent` is enforced from the same running counts. | `saberops.control_plane.policy.effective_global_slot_limit`, `DEFAULT_MAX_GLOBAL_CONCURRENT`, `saberops.capacity.scheduler.schedule`, `saberops.capacity.loop.scheduler_tick`; `docs/operations.md` §"Global aggregate concurrency ceiling" |
| **D7 / F6** | A first-acquisition race on the scheduler lease is caught: an `IntegrityError` on the insert is absorbed, the row is re-read, and the loser returns a truthful denial rather than an unhandled traceback. | `saberops.capacity.loop.acquire_lease`; `tests/test_loop.py::test_simultaneous_first_acquire_race_catches_integrity_error` |
| **D7 / F8** | Work that cannot run reports a typed wait instead of dispatching: a job whose `depends_on` is not all **accepted** waits with `DEPENDENCY` (and `DEPENDENCY_FAILED` for good when a dependency failed/cancelled) before any routing or capacity evaluation; and a denied scheduler lease reports `lease_held_by_other` and exits 2. | `saberops.jobs.dependencies.dependency_blockers`, `saberops.capacity.scheduler.schedule` (blocker branch), `saberops.jobs.models.WaitingReason`, `saberops.capacity.loop` CLI lease path; `docs/operations.md` §"Jobs", `tests/test_scheduler_cli.py` |
| **D7 / F9** | A wait ETA is the **minimum reset ETA over `EXACT`/`PARSED`-confidence decisions**; estimates are never averaged or invented, and `CONSERVATIVE`/unknown resets do not produce a timestamp. | `saberops.capacity.scheduler.schedule` (`best_eta_dec` selection), `saberops.capacity.admission.AdmissionDecision.wait_confidence`; `tests/test_scheduler.py::test_wait_eta_picks_min_from_exact_or_parsed_decisions` |
| **D7 / F10** | A production tick with no injected `clock_fn` still fences on the **real** current time, so an already-expired lease aborts before any launch instead of using a stale captured `now`. | `saberops.capacity.loop.scheduler_tick` (`_current_moment` / `clock_fn`), `check_lease`; `tests/test_loop.py::test_production_shape_lease_expiry_without_clock_fn_fences_launches` |
| **D7 / F12** | A candidate whose provider executable is unavailable is denied at admission, so the tick records a wait and performs **no CAS transition and no reservation**. | `saberops.capacity.admission.admit` (`PROVIDER_EXECUTABLE_UNAVAILABLE`), `saberops.capacity.loop.scheduler_tick` readiness map; `tests/test_loop.py::test_readiness_unavailable_executable_does_not_dispatch` |
| **D7 / R-002** | A **valid empty** chain is an owner decision, not an error: routing schema 3 removed the per-tier/per-package chains, and a tick facing an empty worker chain parks the job with a wait reason instead of raising. (R-002 is a separate routing-migration decision record cited without an `ADR-0002` prefix; only this facet is asserted by the ADR-0002 citation.) | `saberops.routing.py` (schema-3 comment), `saberops.routing_config` (`ROUTED_ROLES`), `saberops.capacity.scheduler.schedule` (empty `groups` branch); `tests/test_loop.py::test_valid_empty_chain_never_crashes_tick` |
| **D7 / R3-M1** | Per-pool RUNNING occupancy is authoritative owner-DB state: if the reservation scan for **any** RUNNING job cannot be read, the whole tick aborts with `ledger_read_error` — never zero, never a partial count that admission could over-admit on. | `saberops.capacity.loop.scheduler_tick` (running-counts scan); `tests/test_loop.py` (R3-M1 cases) |
| **D1 / D7 / F2 / M-3** | `binding_pool_id` scopes *drawing*, not evidence: it must not discard a second declared member pool's constraint, so a non-binding member pool that is below its red line still denies the bound route. | `saberops.capacity.admission.admit` (`drawn_pools` loop); `tests/test_admission.py::test_in_run_two_pool_binding_does_not_discard_second_pool` |
| **D5 / W2 #9, #10** | Probe evidence is learned **per window** (never from an `ALL`-keyed minimum, which is not a per-window delta) and the just-inserted observation row is skipped by **row id** when locating the previous interval. | `saberops.capacity.ledger._learn_from_probe` (`new_row_id`, `window_id == "ALL"` guard), `record_probe_window_observation`; `tests/test_capacity_ledger.py::test_l10_learn_from_probe_per_window_and_row_id` |
| **D2 / D-0024** | Test-only allowance, not a production rule: fixtures may seed a **synthetic, setup-only** verified posture (and the packaged pool membership) so a hermetic dispatch test reaches its intended seam. No packaged default, production posture, admission or pool semantics change, and no real vendor receives an attestation. | `tests/test_session_control.py::_seed_verified_codex_posture`, `tests/test_public_capabilities.py::_seed_verified_worker_posture`, `tests/test_worker_hermetic_env.py` setup-only precedent |

### Identifier families (read this before reusing an ID)

- **D2 / F1** and **D8 / F1** are different rules. F-numbers are scoped to the
  decision they amend (the D2 series is about posture/admission, the D7 series
  about the scheduler, the D8 series about preemption). Never cite a bare `F1`.
- **R1, R2, R3, R4, R4-M1** belong to D8's crash/ref/ownership lane; **R-002** is
  a routing-schema change; **R3-M1** is a D7 occupancy fix. They are unrelated
  numbering series that happen to share a prefix letter.
- **M-3** is an amendment to the D1/D7/F2 binding-scope rule, not a standalone
  decision.
- **M2-5**, **M2-4b**, **Step 001 / Step001**, **W2**, **§11 item n** are
  milestone/step labels used as citations. They identify implementation phases,
  not additional decision clauses; only the rules they label are listed above.

## Citations found

Raw counts below are occurrences of the literal identifier inside an
`ADR-0002 …` citation phrase (a citation like `ADR-0002 §11 item 2` is counted in
both §11 and §11 item 2). 246 lines in the tracked tree contain `ADR-0002`:
244 in Python (source docstrings, test docstrings, one CLI help string) and 2 in
Markdown.

| Identifier | Raw count | Example file |
| --- | --- | --- |
| `D1` | 6 | `tests/test_reviewer_admission.py` |
| `D2` | 37 | `src/saberops/capacity/admission.py` |
| `D4` | 6 | `tests/test_admission.py` |
| `D5` | 7 | `tests/test_capacity_ledger.py` |
| `D7` | 88 | `src/saberops/capacity/loop.py` |
| `D8` | 80 | `src/saberops/preemption.py` |
| `§11` (bare) | 9 | `src/saberops/cli.py` |
| `§11 item 2` | 10 | `src/saberops/routing_config.py` |
| `§11 item 3` | 1 | `tests/test_contract_routing_orchestrator.py` |
| `D2 / F1` | 2 | `src/saberops/capacity/admission.py` |
| `D8 / F1` | 5 | `tests/test_preemption.py` |
| `D7 / F2` (also `D1/D7/F2`, `D5/D7/F2`) | 9 | `tests/test_admission.py` |
| `D7 / F3` | 25 | `tests/test_scheduler.py`, `tests/test_plans_registry.py` |
| `D7 / F6` | 1 | `tests/test_loop.py` |
| `D7 / F8` | 1 | `tests/test_scheduler_cli.py` |
| `D7 / F9` | 1 | `tests/test_scheduler.py` |
| `D7 / F10` | 1 | `tests/test_loop.py` |
| `D7 / F12` | 1 | `tests/test_loop.py` |
| `D7 / R-002` | 1 | `tests/test_loop.py` |
| `D7 / R3-M1` | 2 | `tests/test_loop.py` |
| `D1 / D7 / F2 / M-3` | 1 | `tests/test_admission.py` |
| `D8 R1` | 13 | `tests/test_contract_job_preemption.py` |
| `D8 R2` | 10 | `tests/test_contract_job_preemption.py` |
| `D8 R3` | 15 | `tests/test_checkpoint_ref_safety.py` |
| `D8 R4` | 6 | `tests/test_preemption.py` |
| `D8 R4-M1` | 8 | `tests/test_preemption.py` |
| `D5 / W2 #9 and #10` | 1 | `tests/test_capacity_ledger.py` |
| `D2 / … D-0024` | 2 | `tests/test_session_control.py` |
| `WORKFLOW §7` (cited *with* D2, not an ADR-0002 section) | 17 | `tests/test_worker_hermetic_env.py` |

## Gaps and citations that cannot be reconstructed confidently

1. **No `ADR-0002 §7` citation exists.** The `§7` that appears next to `D2` is
   always `WORKFLOW §7` (`.orch/WORKFLOW.md`, which is **not present in this
   checkout**), cited to authorise a setup-only test-fixture migration; the other
   `§7` strings in the tree belong to `ADR-0003 Amendment 1 §7` (storage tests) or
   to an unnamed contract schema. None is an ADR-0002 section and none should be
   indexed as one.
2. **`D-0024`** is an owner-decision token, not an ADR-0002 sub-ID. Its source
   record is not in the repository; only the test rationale survives
   (`tests/test_session_control.py:464`). The rule stated above is the shipped
   fixture practice, not a recovered decision text.
3. **D3 and D6 are never cited for ADR-0002.** `ADR-0003 D6` appears once in
   `src/saberops/git.py` (retention window) and is a different record. No ADR-0002
   D3/D6 rule is recoverable.
4. **The F-number series is incomplete as a set.** Only F1, F2, F3, F6, F8, F9,
   F10 and F12 are cited; F4, F5, F7, F11 and any F13+ are referenced nowhere, so
   their subject matter cannot be inferred and is deliberately omitted.
5. **Section boundaries and the original numbering are unknown.** `§11` is the
   only section number cited, items 2 and 3 are cited by number, and **item 1 is
   never cited by number** — the deprecation-flag half of §11 above was
   reconstructed from bare `§11` citations. The record therefore uses IDs rather
   than a reconstructed outline.
6. **One citation is incidental, not substantive:** in
   `tests/test_unattended.py:344` the text `ADR-0002 D5` sits inside a synthetic
   owner-words string used to prove verbatim storage. It asserts nothing about D5
   and should not be counted as decision evidence.
7. **`R-002` is an outside record, not an ADR-0002 ID.** It is cited bare
   (`R-002`) in `tests/test_cli_parity.py`, `tests/test_contract_routing_orchestrator.py`,
   `tests/test_readiness_routing.py` and `tests/test_loop.py` for the routing
   role-chain migration ("decision 4", per-package escalation, routing editors,
   the `ROLE` column, deprecated flags). Its text is not committed either; the
   only ADR-0002 claim attached to it is the valid-empty-chain behaviour listed
   above, and that is all this record asserts.
8. **No owner approval is implied.** Where the shipped code is internally
   inconsistent or surprising (for example, the bound D1 draw and the legacy
   unbound `pools_for` path do not draw the same pool set), this record describes
   what the code does and leaves the correction to a normal engineering decision.
