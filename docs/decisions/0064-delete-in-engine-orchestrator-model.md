# ADR-0064: The planner is the agent; the in-engine Orchestrator model is deleted

- **Status:** Accepted. Phase 1.1 lane H3.
- **Date:** 2026-11-23
- **Decider:** Saber (owner), recorded answer 2026-11-22 ("Q3: delete the
  in-engine orchestrator model and `orch orchestrator`: yes")
- **Related:** [ADR-0005](0005-mcp-first-engine-and-phase-2-board.md) §3 (the
  plan role is the external CLI session), [ADR-0001](0001-saberops-engine-mcp-first.md)
  (the engine is not a second planner), [ADR-0053](0053-acceptance-clause-review-gate.md)
  and [ADR-0055](0055-review-evidence-guarantee.md) (review evidence).

## 1. Context

When an owner selected an "orchestrator binding", the engine called a model
once per run to "interpret the objective" and gave that text to the first
worker as an advisory section. The same call also proposed a display title.

That duplicated the planning CLI the owner already sits in. ADR-0005 §3
retired the role vocabulary to `BUILD` and `CHECK` and named the plan role as
the external CLI session; the binding slot, the consumer
(`orchestrator_consumer.py`, 1,622 lines), the selection policy
(`control_plane/orch_binding.py`, 578 lines), the store entry, `orch
orchestrator show|select|clear`, the routing page's orchestrator list and the
first-dispatch guidance cache were the residue of the earlier design. A run
made one model call the worker, reviewer and advisor did not own, and a
selection that no longer matched the vocabulary (the plan role is the agent)
was still persisted.

## 2. Decision

1. **No in-engine Orchestrator model.** A run makes model calls only as a
   worker, an independent reviewer or an on-demand advisor. A store that still
   selects an orchestrator binding is ignored: the run starts normally, no
   model is invoked for it, and no `orchestrator_guidance_result` event is
   written.
2. **One notice, then gone.** Loading an owner binding document that carries
   `orch_binding_policy` prints one line naming ADR-0064 to stderr, once per
   store path per process. The setting is not read, validated or rewritten on
   load; the next settings write drops the key. A frozen pre-1.1 run contract
   may still carry `binding_policy`; it is ignored silently because it never
   influenced execution, only diagnostics.
3. **The store keeps one engine role selection.** `OwnerBindings` holds the
   binding registry and the reviewer selection. `role_binding_ids` and
   `with_role_order` accept only `reviewer`; any other role is refused. The
   `orch orchestrator` verb is removed rather than aliased (it would have no
   effect).
4. **The `PLAN` binding role is retired but readable.** Old stores contain one
   `discovered-binding:orchestrator:...` per discovered model. Keeping the enum
   value lets those documents load unchanged; no code creates or selects one,
   and discovery materializes only `BUILD` and `CHECK` bindings. Deleting the
   value would turn every pre-1.1 store unreadable, which is a worse trade than
   one inert enum member. `_BINDING_ID_SLUGS` keeps `orchestrator` for the
   same reason.
5. **The first-dispatch guidance cache and its projection section go.** The
   `orchestrator_guidance` parameter is removed from `build_initial_projection`
   and `build_package_initial_projection`, so no worker prompt carries a
   section produced by a model that is not the planner.
6. **Run titles fall back to the deterministic label.** The guidance call also
   proposed titles; without it a run's display title is
   `fallback_title(task, repo)` (the same label the store already backfills),
   optionally upgraded by an owner or planner edit. `parse_named_guidance` and
   the guidance-cache tests are removed with it. A title is a label, not
   objective text, so the run's objective is unchanged.
7. **Routing notices point at the agent.** A routing document that names
   `PLAN` or `ORCHESTRATOR` is still refused as managed elsewhere, and the
   notice now points at `orch mcp status` (which shows the connected planning
   agent) instead of the deleted verb.

Out of scope and unchanged: the reviewer binding, worker bindings, the advisor
route, routing chains, quota, checks, review and acceptance.

## 3. Consequences

- One model call per run disappears; the run's first worker gets the objective
  and the engine's fixed contract, not a second model's paraphrase.
- The measured deletion is in the lane notes: the consumer, the selection
  policy, the CLI verb, the store entry and the cache, with the tests that
  exercised them.
- A pre-1.1 owner who pinned an orchestrator binding keeps a working store and
  sees one notice; nothing silently changes which worker or reviewer runs.
- An old run contract still loads; `binding_policy` is dropped the next time
  the contract is written.
- Run titles are less varied than a model's proposals. The owner can rename a
  run; H11's housekeeping sweep does not need to revisit this.
- `is_orchestrator_capable` and `load_owner_runtime_bindings` /
  `_adopt_owner_binding_registry` are removed with the policy they existed for.
  The two orchestrator adopters had no caller; the lane verified that before
  deleting them (see the lane notes).
