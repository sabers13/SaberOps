# ADR-0004: The MCP Orchestrator contract, owner authority and gate evidence

- **Status:** Reconstructed index record. Not owner-accepted; no original text is
  recoverable from this repository's history.
- **Date reconstructed:** 2026-11-19, from the shipped implementation and its
  live documentation.
- **Related:** [ADR-0001](0001-saberops-engine-mcp-first.md) §3.4 (the
  invariants that still stand), [ADR-0008](0008-candidate-bound-acceptance.md),
  [ADR-0011](0011-bounded-automatic-actions.md).

> **Reconstruction notice.** No original ADR-0004 text was ever committed —
> `docs/decisions/` holds ADR-0001 and ADR-0005…0014, and
> `git log --all -- docs/decisions/0004*` is empty. The record below is inferred
> only from the shipped implementation, its tests and the live docs pages that
> cite it, so it claims no owner approval, no vote and no date.
> 
> **Provenance:** ~176 citation lines across `docs/` (mcp, operations, gate,
> questions, unattended-plans, end-certification, first-run-web-ui),
> `src/saberops/` and `tests/`. Where the shipped code and a test disagree in
> wording, the code is authoritative. Identifiers cited only in git history
> (invariants 4 and Amendments 1–4, 6) are listed as gaps, not invented.

---

## Reconstructed decisions

| ID | Rule as shipped | Authority (live doc/symbol) |
| --- | --- | --- |
| **D2** | A **wave** is `base_sha`, `target_branch`, `lanes`, `merge_order`, `named_glue` and `budgets`; a **lane** is a prompt plus `touches`, `must_not_touch`, `contracts`, `retirement_table` and pinned `worker_route`/`reviewer_route`/`effort`. Both models are immutable and validate fail-closed at construction (duplicate lane id, `merge_order` that is not a permutation of the lane ids, a scope pattern that escapes the repo, a non-JSON budget value all raise `WaveModelError`). **Admission** is a deterministic, fail-closed decision whose six independent checks all run so one attempt reports every problem: structural validity, `target_head` shape, pairwise path-prefix `touches` overlap, lane self-conflict between its own `touches` and `must_not_touch`, worker/reviewer route posture, and `base_sha` ancestry (real `git merge-base`, or refusal as unproven); nothing is spawned and no ref moves. Wave and lane lifecycles are total, fail-closed transition tables — wave `PLANNED→RUNNING→INTEGRATING→GATED→AWAITING_ACCEPT→ACCEPTED` with `BLOCKED`/`CANCELLED` escapes, lane `PLANNED→RUNNING→REVIEW→FIX_UP_K`/`REVIEWED_PROVISIONAL→ACCEPTED` with `BLOCKED` and the WX-2 `PREEMPTED` park — applied only through `transition_wave`/`transition_lane`, so `GATED` cannot be skipped and a terminal state cannot be left. Serialization is one JSON round-trip plus one SQLite-compatible row, every parse path validating types. | `src/saberops/wave_models.py` (`Wave`, `Lane`, `WaveState`, `LaneState`, `WAVE_ALLOWED_TRANSITIONS`, `LANE_ALLOWED_TRANSITIONS`); `src/saberops/wave_admission.py` (`admit_wave`, `WaveAdmissionDecision`); `Orchestrator.admit_wave` in `src/saberops/orchestrator.py` |
| **D3** | The **engine, never a model, is the only producer of authoritative gate evidence**, recorded once per run as `engine-outcome.json` and one appended line in `<git-common-dir>/saberops-gate-history/full.jsonl` carrying `kind` (`G1|G2|G3|main-gate|manual`) and `producer` (`engine`). A model's own iteration run is `G0`/`model` and is never evidence: `require_engine_evidence` refuses it and `project_gate_outcome` projects it `BLOCKED`. Before any gate command runs the engine performs the deterministic pre-G1 preflight — changed paths inside `Touches` and outside `must_not_touch` (`WRITE_SCOPE_VIOLATION`), no test removed or rewritten without a matching retirement-table action (`TEST_RETIREMENT_VIOLATION`), and gate-definition tamper protection against the base SHA (`GATE_PROTECTION_BLOCKED`) — then disk admission, the slot, the profile wall budget, and a fail-closed projection of the profile's `outcome.json` (`kind`, `producer`, `profile`, `checks_ok`, `selected`). A blocked preflight yields `outcome: BLOCKED` with no command run and a real wall time. | `docs/operations.md` §"Engine-run gates (G1 / G2 / G3) and the history ledger"; `src/saberops/gate_history.py` (`GateKind`, `GateProducer`, `require_engine_evidence`); `src/saberops/gate_scope.py` (`check_write_scope`, `check_test_retirement`, `check_gate_protection`); `src/saberops/gate_runner.py` (`EngineGateExecutor.execute`); `docs/gate.md` §"Gate tamper protection (GT-1)" |
| **D4** | A wave's work is **budgeted and the budget is a hard ceiling**: at most **4 G3 runs per wave** (`MAX_G3_RUNS_PER_WAVE`) and at most **2 fix rounds per failure** (`MAX_FIX_ROUNDS_PER_FAILURE`). Declared `budgets` are validated — `g3_runs` must be a positive integer ≤ 4, `fix_rounds` a non-negative integer ≤ 2 — and any other budget value must be JSON-serializable; exceeding a ceiling raises `WaveModelError` rather than being clamped, and a budget is never raised silently (the overrun path escalates to an owner question). The budget accessor reads an integer with a default and refuses a non-integer. | `src/saberops/wave_models.py` (`MAX_G3_RUNS_PER_WAVE`, `MAX_FIX_ROUNDS_PER_FAILURE`, `Wave._budget_problems`, `Wave.budget`) |
| **D6** | The engine **never guesses an owner decision**: it raises a keyed, immutable question carrying all five escalation parts (requirement, proposed expansion, smallest compliant option, consequences, recommendation) — a question missing one is refused. Answer **authority is derived from the decision class, never chosen by the caller**: `MOVE_MAIN`, `PUSH`, `QUARANTINE`, `BILLING_OR_POSTURE`, `TEST_CHANGE_AGAINST_ADR`, `DELETE_OUTSIDE_ENGINE_FOLDERS`, `APPROVE_CONFIG_PROPOSAL` and `OTHER`/unknown are **owner-only**; `REVIEW_FINDING_CLASSIFICATION`, `FIX_UP_CONTENT`, `G3_FAILURE_ROUTING` and `ROUTINE_QUESTION` are Orchestrator-answerable. Implementer roles (`BUILD`, `CHECK`) can never answer, a planner can never answer its own finding, a planner-alone answer must carry `evidence` **and** `rejected_alternative`, answers store the owner's exact words, a question leaves `OPEN` only through an explicit verb (with evidence-backed `moot`), and task completion or cleanup has no close authority. A later conflicting record is reported as a divergence, never applied over the answer. | `docs/questions.md`; `src/saberops/questions.py` (`DecisionClass`, `OWNER_ONLY_DECISION_CLASSES`, `ORCHESTRATOR_ANSWERABLE_DECISION_CLASSES`, `resolve_answer_authority`, `OwnerQuestionQueue`) |
| **D7** | MCP exposes a **fixed Orchestrator tool set and nothing more**: `preflight`, `submit_job`, `get_job`, `catch_up`, `tail_job`, `wait_job`, `list_jobs`, `cancel_job`, `note_job`, `capacity`, `read_evidence`, `list_questions`, `answer_question`, `propose_config_change`, `list_proposals` — each schema-validating and delegating to an existing service function with no authority of its own. The **never-exposed** authorities (approving config proposals, moving `main`, pushing, quarantine approval, posture attestation, direct gate execution, tracked-source edits, deletions outside `.orch`/`.saberops`, reading credentials) are pinned as data **with aliases** and refused as JSON-RPC `-32010` errors *before* any handler runs; an ordinary failure is instead an `isError` result. **Read tools never return credentials or secret-reference values** (key-marker and value-pattern redaction on every tool result, resource and snapshot), evidence reads are confined to `.orch`/`.saberops` with a filename allowlist and symlink-escape refusal (`-32030`, 1 MiB cap), and exactly one MCP client is the writer while a second live client is read-only (`-32020` on mutating calls). `answer_question` accepts only `PLAN` authority. | `docs/mcp.md` §"The job and administration tools", §"Never exposed", §"Single-writer Orchestrator lease"; `src/saberops/mcp/tools.py` (`FORBIDDEN_OPERATIONS`, `ToolRegistry.call`); `src/saberops/mcp/redaction.py` (`redact_secrets`); `src/saberops/mcp/evidence.py` (`EvidenceRoots`, `ALLOWED_EVIDENCE_PATTERNS`); `src/saberops/mcp/errors.py` (`MCP_FORBIDDEN_OPERATION = -32010`) |
| **D8** | The **local owner surface is loopback-only and every mutation goes through one validating service layer that fails closed**. The web app binds `127.0.0.1`, a middleware refuses any non-loopback `Host` (and a cross-site `Sec-Fetch-Site`/foreign `Origin` on every POST) with `403` *before* owner data is returned, every form carries a process-local CSRF token verified with `hmac.compare_digest`, privileged mutations additionally require explicit owner confirmation, and no route has a password/token/secret field. `/health` and `/static/*` are deliberately outside the private-path guard, so health stays reachable and reports only **non-secret install identity** (product, version, paths, source SHA/tree state, DB path, pid, `orch` path). | `src/saberops/web.py` (`_is_private_web_path`, host guard, `@app.get("/health")`); `docs/first-run-web-ui.md`; `tests/test_contract_web_host_guard.py::test_health_stays_open_by_design` |
| **D8.1** | Cross-surface mutations share **one validating service layer**: `propose_config_change` validates the change against the current `WorkflowSettings` and refuses unknown settings, malformed values and no-op proposals (`ConfigError`) before anything is built, and the MCP/CLI/web adapters call that layer rather than re-implementing validation. A refusal is an ordinary tool error (`isError: true`), never a silently coerced value. | `src/saberops/config.py` (`propose_config_change`, `WorkflowSettings`); `tests/test_mcp_contracts.py::test_propose_config_change_refuses_an_unknown_setting` |
| **D8.2** | **A proposal changes nothing**: `propose_config_change` returns an approval-ready, frozen-hash `ConfigProposal` with `applied: false` and persists no entry; only `approve_config_proposal` through the owner's `orch config approve` or the local web surface appends to the immutable config-decision ledger. | `src/saberops/config.py` (`propose_config_change`, `approve_config_proposal`, `ConfigLedger`); `docs/mcp.md` §"The job and administration tools" |
| **D14** | An **unattended (overnight) run** is governed by an owner-frozen `DecisionSheet` of bounded standing decisions that apply only on an exact `(project, task, paths)` + decision-key match, with no prefix, glob or wildcard, recording the owner's exact words and at least one bound (`expires_at` and/or `ttl_turns`). An immutable **never-delegable** list (`test_gate`, `data_training_policy`, `roadmap_scope`, `budget_cap`, `credentials`, `destructive_operations`, `publishing`) is checked **first** and parks only its own branch; then a live standing decision applies; then Class 1 may auto-accept **only** when gate + cold review + green checklist + authorized scope + evidence are all present, and the only acceptance action that exists is a **local commit** (`push_allowed` is always false and cannot be constructed true); then Class 2 may take the recommended option only with evidence and ≥ 1 rejected alternative (journalled in the Orchestrator-alone audit list); everything else escalates. A deterministic preflight blocks the start on budget/wall-clock/scope/worker-cap/channel gaps, the third consecutive failure opens a terminal circuit breaker, and a run with no alarm channel is refused as a silent stall. | `src/saberops/unattended.py` (module docstring "The frozen ADR-0004 D14 rules encoded here", `NEVER_DELEGABLE_KINDS`, `resolve_decision`, `run_preflight`, `RunProgress`, `detect_stall`, `build_morning_report`); `docs/unattended-plans.md` |
| **§3** | "Decision" section heading. The concrete decisions cited from it are **D2** (wave/lane model and admission), **D6** (owner decision authority) and **D7** (MCP Orchestrator contract), consistent with every `§3 Dn` citation in the tree. | `src/saberops/wave_models.py` docstring (`§3 D2`); `src/saberops/questions.py` docstring (`§3 D6`) |
| **§4 invariant 2** | **Overlapping lane file sets are rejected at submit/admission.** `touches` are path-prefix scopes (`src/saberops` owns everything below it), two lanes may not overlap in either direction, and a lane may not list the same path in `touches` and `must_not_touch`; overlap is reported as ordered `overlapping_touches:left:right:left_pattern:right_pattern` (or `lane_self_overlap:lane`) and nothing is spawned. | `src/saberops/wave_models.py` docstring; `src/saberops/wave_admission.py` (`lane_touch_overlaps`, `scopes_overlap`, `scope_covers`) |
| **§4 invariant 3** | **Integration is a declared, deterministic order over pinned identities**: `base_sha` is pinned on the wave, lane heads are the reviewed SHAs, and `merge_order` must list every lane id exactly once (a permutation), so the merge sequence is reproducible and a lane cannot be merged out of order or twice. While the WX-1 executor existed it additionally re-resolved each branch tip and refused a moved head; that executor was deleted (ADR-0005 lane D3), so the shipped guarantee is the model contract plus admission. | `src/saberops/wave_models.py` (`Wave.problems` merge_order permutation check); `tests/test_wave_models.py::test_merge_order_must_cover_every_lane_exactly_once` |
| **§4 invariant 10** | **Budgets are bounded and fail closed**: a declared `g3_runs` > 4, `fix_rounds` > 2, a zero `g3_runs` or a boolean/non-integer value is refused, and a non-JSON budget value is refused, so a caller can never widen a budget silently. | `src/saberops/wave_models.py` (`_budget_problems`); `tests/test_wave_models.py::test_budget_bounds_are_enforced` |
| **invariant 6** | **Owner-only decisions can only ever be answered by the owner.** The class split in D6 is enforced at the answer boundary: `OWNER` authority requires the `OWNER` role and is **not exercisable over MCP** (`authority: "OWNER"` → `-32010`), and a question whose class is owner-only is refused the same way even when the caller claims `PLAN`. | `docs/mcp.md`; `src/saberops/questions.py` (`OWNER_ONLY_DECISION_CLASSES`, `resolve_answer_authority`); `tests/test_mcp_contracts.py::test_answer_question_refuses_owner_authority` |
| **invariant 7** | **No MCP tool can apply a configuration change.** `approve_config_proposal`, `config_approve`, `record_config_decision` and their aliases are refused `-32010` before any handler, the ledger stays empty, and only the owner's own surface writes it. | `docs/mcp.md`; `tests/test_mcp_contracts.py::test_no_tool_can_approve_a_config_proposal` |
| **invariant 13** | **Tools are idempotent.** A question key always maps to one deterministic id (`q-<sha256(key)[:12]>`), so a retried ask is the same question and the same key with a different payload fails closed; `submit_job` with an `idempotency_key` returns the same job with `created: false` and records one ledger event; repeating an identical answer is recorded as a confirmation, does not create a divergence, and appends one `question_answered` event; ledger appends are idempotent on the event key. | `docs/mcp.md`; `src/saberops/questions.py` (`question_id_for_key`, `OwnerQuestionQueue.answer`); `tests/test_mcp_contracts.py::test_submit_job_is_idempotent_with_a_key`, `::test_answer_question_is_idempotent` |
| **§8 Q2** | The owner's accepted answer that the **Orchestrator may answer routine questions** without waking the owner: `REVIEW_FINDING_CLASSIFICATION`, `FIX_UP_CONTENT`, `G3_FAILURE_ROUTING` (where the rules allow two targets) and `ROUTINE_QUESTION` resolve to `PLAN` authority, but only with `evidence` and a `rejected_alternative`, and every such decision appears in `orchestrator_alone_decisions()`. Owner-only classes are never Orchestrator-answerable. | `src/saberops/questions.py` (`ORCHESTRATOR_ANSWERABLE_DECISION_CLASSES` docstring "(ADR-0004 D6 / Q2)", `orchestrator_alone_decisions`) |
| **§8 Q3** | The local surface is **localhost only**: `orch ui` binds `127.0.0.1` by default and the app refuses a non-loopback `Host` with `403` before returning owner data, so the console is never reachable from the network. | `docs/first-run-web-ui.md` §"Security architecture"; `src/saberops/cli.py` (`ui --host` default `127.0.0.1`); `src/saberops/web.py` (`_LOOPBACK_HOST_NAMES`) |
| **§8 Q4** | **Self-dogfooding certification criteria**: (Q4-A) ≥ 3 green waves on distinct independent/fixture repositories, (Q4-B) ≥ 1 *real* G3 failure routed to a build repair with durable attribution, (Q4-C) zero INFRA failures, (Q4-D) zero owner-only decisions taken autonomously without explicit owner confirmation. `e_verdict` is `CERTIFIED` only when Q4 plus closure plus all nine scenarios pass; any single failure is `NOT_CERTIFIED`. | `docs/end-certification.md`; `src/saberops/certification.py` (`evaluate_q4`, `WaveRecord`, `Q4_MIN_GREEN_WAVES`) |
| **Amendment 5** | The **cross-phase ready frontier** is a pure, deterministic projection over an *authoritative and explicitly complete* plan snapshot: phases and three-digit display steps are presentation labels, never dependency edges, so later-phase work does not wait on an earlier phase. Only hard **START** dependencies gate a start (a blocked/failed dependency holds its dependents and nothing else); **integration prerequisites** never block a start. Only a `CHILD_TASK` occupies an execution lane — plan/wave wrappers and Orchestrator turns are coordination records, and identical duplicates are one alias. Write scopes are canonical, project-namespaced and exclusive: every `RUNNING` child reserves its scopes before new candidates are examined, and non-executing holders reserve nothing. The 3-active-project and 5-child-per-project maxima are independent bounds; the derived 15 is observability only and the owner bound is never silently lowered. Unknown status/kind, missing or conflicting identity, unsafe scope, missing dependency, cycle, incomplete snapshot or non-authoritative source **fail closed**, denying the whole projection. The projection is explicitly **not launch authority** (`REQUIRES_TRANSACTIONAL_VERIFICATION` is always true). | `src/saberops/ready_frontier.py` (`project_ready_frontier`, `FrontierLimits`, `TaskKind`, `FrontierReason`, `SnapshotSource`); `tests/test_ready_frontier.py` (12 `Covers: ADR-0004 Amendment 5` tests) |
| **Amendment 7** | The **3 × 5 concurrency bound is an owner-declared ceiling, never a target**: at most 3 concurrently active tasks/projects and at most 5 worker sessions per project (`MAX_CONCURRENT_TASKS = 3`, `MAX_WORKER_SESSIONS_PER_PROJECT = 5`), recorded as central owner configuration in the append-only ledger. The two bounds are independent constraints; the product (15) is observability only, reaching the ceiling is not a goal, and nothing may silently raise or lower it. | `src/saberops/config.py` (`MAX_CONCURRENT_TASKS`, `MAX_WORKER_SESSIONS_PER_PROJECT`); `src/saberops/ready_frontier.py` (`FrontierLimits`, `default_frontier_limits`) |

### Cross-references carried by the citations (not ADR-0004 identifiers)

| Identifier | What it points at as shipped | Authority |
| --- | --- | --- |
| **M3 / Step 014** | The milestone/step that built `saberops.mcp` and `orch mcp serve` — the D7 implementation work item. | `src/saberops/mcp/__init__.py`, `docs/mcp.md` title |
| **Step 017 / RD-1** | The milestone/roadmap criterion that built `saberops.unattended` — the D14 implementation work item. `RD-1` is cited 114 times across the tree, mostly in `tests/test_unattended.py` docstrings. | `docs/unattended-plans.md` title; `src/saberops/unattended.py` |
| **REHEARSAL §3** | The rehearsal document's never-verbs list, which D7 pins as data; the doc is not in this checkout, the enforcement is `FORBIDDEN_OPERATIONS`. | `docs/mcp.md` §"Never exposed"; `src/saberops/mcp/tools.py` |
| **WORKFLOW §7** | The manual workflow's escalation protocol, carried as data by the five-part `Escalation` contract. | `docs/questions.md` opening paragraph |
| **ADR-0003 D2 / D3** | Gate profiles (fast/acceptance/full as project data, never weaker than the lifecycle requires) and the host-gates slot pools — cited *alongside* ADR-0004 D3 in the engine gate seam. ADR-0003 is also uncommitted. | `src/saberops/gate_profiles.py` (`required_profile`, `resolve_profile`); `docs/operations.md` §"Engine-run gates" |
| **ADR-0001 invariant 3** | Callers state requirements (capabilities) and can never state routes, models, tiers, billing or quotas; a caller may strengthen but never weaken a required profile. | `src/saberops/mcp/tools.py`, `src/saberops/gate_profiles.py` |

---

## Citations found

Every identifier below was found by `grep -rn "ADR-0004" --include="*.md" --include="*.py" .`
(176 matching lines). Counts are occurrences of the identifier text, not lines.
"Example file" is one representative citation site.

### Identifiers on ADR-0004 itself

| Exact identifier text | Count | Example file |
| --- | --- | --- |
| `ADR-0004 D2` | 44 | `tests/test_wave_models.py` (`Covers: ADR-0004 D2 (lane/wave fields)`) |
| `ADR-0004 D3` | 27 | `src/saberops/gate_history.py` (module docstring) |
| `ADR-0004 D4` | 3 | `src/saberops/wave_models.py` (`MAX_G3_RUNS_PER_WAVE`) |
| `ADR-0004 D6` | 8 | `src/saberops/questions.py` (`DecisionClass` docstring) |
| `ADR-0004 D7` | 52 | `docs/mcp.md`; `src/saberops/mcp/tools.py` (`FORBIDDEN_OPERATIONS`) |
| `ADR-0004 D7/D8` | 4 | `src/saberops/mcp/tools.py` (config-approval aliases) |
| `ADR-0004 D8` | 1 | `tests/test_contract_web_host_guard.py:183` |
| `ADR-0004 D8.1` | 1 | `tests/test_mcp_contracts.py:426` |
| `ADR-0004 D8.2` | 2 | `tests/test_mcp_contracts.py:409`; `docs/mcp.md:109` |
| `ADR-0004 D14` (`**D14**`) | 5 | `src/saberops/unattended.py` (module docstring) |
| `ADR-0004 §3` | 4 | `src/saberops/wave_models.py:3`; `src/saberops/questions.py:5` |
| `ADR-0004 §3 D2` | 2 | `src/saberops/wave_models.py:3`; `src/saberops/wave_admission.py:1` |
| `ADR-0004 §3 D6` | 2 | `src/saberops/questions.py:5`; `docs/questions.md:3` |
| `§4 invariant 2` (with `ADR-0004 D2 +`) | 5 | `tests/test_wave_admission.py`; `src/saberops/wave_admission.py:1` |
| `§4 invariant 3` | 1 | `tests/test_wave_models.py:149` |
| `§4 invariant 10` | 1 | `tests/test_wave_models.py:158` |
| `ADR-0004 invariant 6` | 2 | `tests/test_mcp_contracts.py:323` |
| `ADR-0004 invariants 6 and 13` | 1 | `docs/mcp.md:104` |
| `ADR-0004 invariant 7` | 1 | `tests/test_mcp_contracts.py:457` |
| `ADR-0004 invariant 13` | 3 | `tests/test_mcp_contracts.py:241,384`; `src/saberops/questions.py:8` |
| `ADR-0004 D6 / Q2`, `ADR-0004 D6, accepted answer Q2` | 2 | `src/saberops/questions.py:282,305` |
| `ADR-0004 §8 Q3` | 1 | `docs/first-run-web-ui.md:106` |
| `ADR-0004 §8 Q4` | 1 | `docs/end-certification.md:3` |
| `ADR-0004 section 8 Q4` / `ADR-0004 Q4 qualification` | 4 | `src/saberops/certification.py` (`evaluate_q4`, `CertificationChecklist`) |
| `ADR-0004 section 8` | 1 | `src/saberops/certification.py:10` |
| `ADR-0004 Amendment 5` | 13 | `src/saberops/ready_frontier.py:11`; `tests/test_ready_frontier.py` (12 tests) |
| `ADR-0004 Amendment 7` | 1 | `src/saberops/config.py:565` |
| bare `ADR-0004` (line-wrapped continuation) | 1 | `src/saberops/mcp/errors.py:10-11` (`ADR-0004` / `D7 marks **never exposed**`) |

### Files carrying the citations

`docs/`: `mcp.md`, `operations.md`, `gate.md`, `questions.md`, `unattended-plans.md`,
`end-certification.md`, `first-run-web-ui.md`.
`src/saberops/`: `mcp/{__init__,errors,evidence,protocol,redaction,tools}.py`,
`orchestrator.py`, `questions.py`, `ready_frontier.py`, `unattended.py`, `config.py`,
`certification.py`, `gate_history.py`, `gate_outcome_projection.py`, `gate_profiles.py`,
`gate_runner.py`, `gate_scope.py`, `wave_admission.py`, `wave_models.py`.
`tests/`: `test_cli_mcp.py`, `test_contract_web_host_guard.py`, `test_gate_engine_execution.py`,
`test_gate_outcome_projection.py`, `test_gate_scope.py`, `test_gate_tools.py`,
`test_mcp_contracts.py`, `test_mcp_server.py`, `test_ready_frontier.py`, `test_unattended.py`,
`test_wave_admission.py`, `test_wave_models.py`.
Other: `scripts/gate_tools.py`.

---

## Gaps and reconstructions I could not verify

1. **Section numbering.** `§3` (decisions), `§4` (invariants) and `§8` (questions/answers)
   are all cited, but nothing in the tree states what `§1`, `§2`, `§5`–`§7` contained.
2. **Invariant numbering is incomplete.** Only 2, 3, 6, 7, 10 and 13 are cited. The WX-1
   commit message (`f70eba6`) says "invariants 2-4 and 10", so at least **invariant 4**
   exists (a blocked lane is routed, not a wave failure) but is no longer cited in code;
   **invariants 5, 8, 9, 11, 12** are unattested.
3. **Amendments.** Only **5** and **7** are cited in the tree. `f70eba6` also cites
   "Amendments 3, 4 and 7" (3 = 3x5, 4 = accepted-wave cleanup eligibility) and
   `docs/waves.md` (deleted in `00acb47`) named **Amendment 1** for storage/cleanup.
   The texts of Amendments 1–4 and 6 are not recoverable from the shipped code; the
   cleanup contract now lives in `src/saberops/storage_gc.py` and `saberops.certification`
   safe-closure checks.
4. **D-decisions 1, 5, 9–13 are entirely uncited.** D8's headline sentence is the weakest
   reconstruction in this table: only one test cites `D8` (health stays open) and the two
   sub-decisions `D8.1`/`D8.2` are config-proposal rules, so the umbrella statement
   ("local owner surface, one validating layer, non-secret diagnostics") is inferred from
   `docs/first-run-web-ui.md` plus `web.py`, not from a citation.
5. **D3 vs ADR-0003.** Every D3 citation that also names gate profiles or slots attributes
   those to **ADR-0003 D2/D3**, which is likewise uncommitted. A reconstructed ADR-0004
   should not absorb the profile/slot decision.
6. **Q1 is never cited.** Q2/Q3/Q4 are recoverable (routine-question authority, localhost
   binding, certification criteria); nothing shows whether a Q1 existed or what §8's full
   question list was.
7. **D10–D13 / D1 / D5 / D9** are never mentioned, so this record cannot claim the ADR was
   limited to the decisions above.
8. **Deleted corroboration.** `docs/waves.md` (WX-1/WX-2) and `saberops.wave_executor` were
   removed in `00acb47`; their rule text was used only to check my reconstruction of D2/D4
   and invariant 3, and is **not** a live authority pointer.
