# ADR-0086: One owner-facing routing source per role

- **Status:** Accepted. Implemented in phase 1.1 lane H26.
- **Date:** 2026-11-23
- **Decider:** Saber (owner), phase 1.1 plan H26 and the 2026-11-22 "maximise
  the deletion" answer
- **Related:** [ADR-0001](0001-saberops-engine-mcp-first.md) (invariants),
  [ADR-0005](0005-mcp-first-engine-and-phase-2-board.md) (the planning CLI is
  the orchestrator; schema 3 build route and `strong_start`),
  [ADR-0035](0035-cheap-scout-route.md) (the cheap route lives on the build
  chain), [ADR-0064](0064-delete-in-engine-orchestrator-model.md) (no engine
  plan binding)

## 1. Context

Four layers can answer part of "which model may run this role":

| Layer | What it actually owns |
| --- | --- |
| `routing_config.py` | The **owner's build selection**: the schema-3 `roles.BUILD` fallback groups, the `strong_start` setting, the closed static candidate registry, and the discovery-evidence approval records that let a dynamic candidate enter a chain. Every owner edit goes through `save_user_routing`. |
| `routing.py` | Resolution of one role chain from a routing snapshot, attended-only and Gemini-family refusals, explicit owner overrides. |
| `control_plane/bindings.py`, `binding_store.py` | The **exact execution identity** (`binding_id`/profile/quota pool/backend/effort) and the owner's **check selection order**. The store is the one owner document for the check role. |
| `control_plane/plans.py` | The quota/account/posture registry: pool membership, windows, billing postures, reserves. It supplies admission facts and owns no route. |
| `model_access/` | **How** an already-selected binding connects: connections, discovery evidence, readiness. It never selects, ranks or filters. |

The static routing audit for this lane found no second owner for the same
fact, but it did find the same fact encoded twice inside the areas:

- The closed build-candidate registry lived twice: `routing_config.
  ALLOWED_CANDIDATES` (the validated registry) and 19 `CANDIDATE_*` constants in
  `routing.py`, of which 13 had no caller and only six are the reviewer
  ladder's fixed policy.
- `routing.py` still carried retired compatibility readers: `is_peak_hours`
  (always `False`; routing has no peak mode), `is_muse_model` (no caller),
  `_with_binding_id` (an alias with no caller) and `_role_for_tier_alias`
  (always `"BUILD"`).
- `routing_config` read the same effective document twice (once for the
  validated snapshot, once for the raw payload) and `suggest_default_routes`
  re-implemented the plan overlay by hand instead of reading the plan registry
  through `load_effective_plan_set`.
- `plans.effective_quota_pools` re-implemented `effective_pool_plans` inline, so
  `resolve_binding_quota_pools` parsed the packaged plan document twice per
  admission.
- `bindings.resolve_worker_candidate_binding` and
  `resolve_reviewer_candidate_binding` were two copies of one exact-binding law
  that differed only by role.
- `OwnerBindings` stored the check selection twice: the legacy
  `reviewer_binding_id` primary beside the ordered `reviewer_binding_ids`
  chain. Both were written by every selection path, but only the chain is the
  real reader; the primary survived as a compatibility mirror.

## 2. Decision

1. **One owner-facing source per role, one editor.** The owner document for
   the build role is `routing.json`; the owner document for the check role is
   `bindings.json` (the ordered reviewer chain). The Routing page and
   `routing_ops` remain the only owner edit seam over both. `plans.json` and
   the access/discovery stores stay evidence-and-policy readers and own no
   route.
2. **The review ladder is not a route registry.** `routing.py` keeps only the
   six candidates the fixed reviewer ladder uses; the closed build-candidate
   registry has exactly one encoding, `routing_config.ALLOWED_CANDIDATES`.
3. **The check selection has one representation.** `OwnerBindings` keeps the
   ordered `reviewer_binding_ids` chain only; the primary is its first entry.
   A pre-ADR-0086 store that carries only `reviewer_binding_id` is migrated
   into the chain on load with one notice per store per process, and the next
   settings write drops the old field. A document that carries both keys and
   they disagree still fails closed.
4. **One reader per document and one law per decision.** The effective routing
   document is read once (`_read_package_document`/`_read_effective_document`)
   for the snapshot, the payload and the packaged default; the plan registry is
   read through `load_effective_plan_set`; quota pools are projected from one
   resolved plan set; both candidate roles resolve through one
   `_resolve_candidate_binding(role=...)`.
5. **What does not move.** `approved_candidates` stays as the discovery
   evidence that turns a route id into an exact binding; it is the only
   disambiguator when two connections expose the same provider/model, and
   persisted snapshots digest it. The legacy reviewer ladder stays as the
   engine fallback when the owner selected nothing. Quota, failover and review
   policy are untouched.

## 3. Consequences

- An owner sees the same routes before and after: `orch routing show`,
  `orch models`, the Routing page and the reviewer commands read the same
  documents with the same order.
- The check selection can no longer be set through one field and read through
  another; an ambiguous store never silently prefers one. A migrated legacy
  selection also counts as a configured chain when review runs without an
  explicit override, so the owner's choice is honored there instead of being
  silently replaced by the legacy ladder.
- A further consolidation (one document holding both the build chain and the
  check order, or deriving exact bindings from the registry instead of the
  approval record) is deliberately **not** taken here. It would change the
  frozen-snapshot digest and the connection disambiguation contract, which is
  an owner decision, not a deletion.
- Dead code found in the same files but outside this lane's overlap (for
  example `ExecutionBinding.verification_state`/`verification_summary`,
  `build_binding_registry`, `model_access.connections.probe_account_backend`)
  is reported for H31, not deleted here.

## 4. Verification

- `tests/test_routing_config.py`, `tests/test_routing_reorder.py`,
  `tests/test_routing_ops.py`: build-chain load, edit, reorder, migration.
- `tests/test_reviewer_selection.py` (new migration and ambiguity cases),
  `tests/test_reviewer_dispatch.py`, `tests/test_review.py`,
  `tests/test_cli_parity.py`, `tests/test_control_plane_bindings.py`: check
  selection, persistence, dispatch and refusal reasons.
- `tests/test_admission.py`, `tests/test_capacity_probes.py`,
  `tests/test_selector_grammar_agreement.py`: quota admission unchanged.
- `tests/test_f4_quota_failover.py`: failover drills unchanged.
- The legacy-primary migration test fails on the pre-ADR code (no notice) and
  passes after; `reviewer_binding_id` no longer appears in a saved document.
