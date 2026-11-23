# ADR-0085: One observability projection for every run view

- **Status:** Accepted. Phase 1.1 lane H24.
- **Date:** 2026-11-24
- **Decider:** Saber (owner), recorded answers 2026-11-22 (`Q8` yes, "maximise
  the deletion") and 2026-11-23 (update the plan in the phase-1 parallel style)
- **Related:** [ADR-0070](0070-three-cli-views.md) (the three kept views this
  projection serves), [ADR-0053](0053-acceptance-clause-review-gate.md) and
  [ADR-0055](0055-review-evidence-guarantee.md) (the evidence the projection
  carries), [ADR-0025](0025-read-only-tmux-watch.md) (`orch watch`)

## 1. Context

H9 kept three CLI views and pointed them at the same durable rows, but the
reading code was still spread over four modules:

- `observability/_core.py` carried `build_run_observability_snapshot`, a second
  full projection of one run (attempts, dispatches, usage, gate checks, reviews,
  findings, project scope, continuation rows, quota observations, control-plane
  evidence, frozen contracts and a reconstruction-completeness block). After H9
  no kept view called it: `orch observability` had already become an alias for
  `orch report --json`. Only one test read it, so the legacy gateway evidence it
  surfaced had become unreachable from every product surface.
- `observability/run_report.py` built the canonical report by reading the event
  log six separate times and derived gate/review facts without the commit each
  check or review assessed.
- `mcp/tools.py` (`catch_up`) derived the latest check, the latest review bound
  to the current candidate and the quota-handoff switch from its own queries,
  duplicating the report's gate/review/continuation semantics.
- `observability/phase_telemetry.py` exported read helpers
  (`list_phase_observations`, `summarise_observations`, `assert_canonical_phase`)
  with no caller, and `db.py` carried five trace/phase getters nothing called:
  `get_attempt_trace`, `get_attempt_traces_for_run`, `get_tool_effects_for_run`,
  `get_worker_activations_for_run`, `get_phase_observation`.
- The plan also named `monitor.py`, but that module is the process-tree
  lifecycle monitor (stall confirmation, extinction); it reads `/proc`, not run
  evidence, and is the safety drill behind an early worker cut. It is not a run
  report reader and stays untouched.

## 2. Decision

1. **`build_run_report` is the one projection.** It answers every run question
   the kept views ask: lifecycle and stage, candidate and its durable source,
   requested versus actual identity, gate checks, reviews, usage, acceptance,
   workspace and warnings.
2. **The retired snapshot's facts move into it.** A new `evidence` section
   carries candidate lineage, dispatch rows, usage rows and aggregate, review
   evidence and findings, project scope, continuation (checkpoints, handoffs,
   recovery dispatches), quota observations, control-plane and frozen-contract
   evidence, phase telemetry rows with a per-phase coverage count, and the
   reconstruction-completeness block. `build_run_observability_snapshot` and its
   private readers are deleted; the legacy-gateway test now proves those rows
   still surface through the report.
3. **Derived facts are computed once.** Gate check rows report the commit each
   check assessed (from the producing attempt) and a bounded 400-character
   output excerpt. Review rounds report the commit each review assessed (from
   its REVIEW dispatch row, never "the latest commit") and a bounded summary
   excerpt. `catch_up` maps those fields instead of querying again, and its
   quota-handoff switch selects from the report's lineage, continuation and
   workspace observations.
4. **The event log is read once per report** and passed to every section that
   projects from it; each section keeps its own explicit gap when the log is
   unreadable.
5. **Terminal state comes from the canonical set.** `is_terminal_report` reads
   `TERMINAL_RUN_STATUSES` instead of its own three-name list, so the
   `report --follow` loop no longer keeps a parallel terminal-status list and a
   PREEMPTED run stops the live view through the same projection.
6. **`status`, `report`, `watch`, `catch_up` and `tail_job` consume it.**
   `status` still prints raw per-attempt worker text (which the canonical report
   deliberately does not copy) but takes its check and review facts from the
   report. `tail_job` keeps its incremental event/transcript cursor and adds the
   report's lifecycle, candidate, gate, review and acceptance facts.
7. **Unused readers are deleted.** The three phase read helpers and the five db
   getters above have no caller in `src`, `tests`, `scripts` or the benchmark
   rig. The phase recorder that writes at runtime boundaries stays.
8. **No state distinction moves.** Reviewed versus accepted, requested versus
   actual identity, dispatch versus usage counts, retry versus repair and the
   durable `run_accepted` acceptance rule keep their existing semantics; no
   outside job model is imported.

## 3. Consequences

- One module owns run reading. A fact fixed there is fixed in every view, and
  `report --json` once again carries the evidence the old `observability` alias
  promised.
- The canonical JSON grows by the evidence section (dispatch and usage rows,
  findings, phase rows, frozen contracts). It stays deterministic: rows are
  read in stable database order and serialized with the canonical serializer.
- `catch_up` and `tail_job` build the report per call. Both are read-only MCP
  reads, not per-byte loops; `tail_job` keeps its bounded cursor payload.
- `monitor.py` is not merged. Treating it as a report reader would have touched
  the stall/extinction safety path for no reader of run evidence.
- Deleted with their code: the snapshot function, the five db getters, the three
  phase read helpers, the `catch_up` check/review/switch queries and the
  duplicated event reads.
