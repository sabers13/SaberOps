# Bounded Orchestrator memory, session locking and restart (OM-1, Step 022)

This document describes `saberops.orchestrator_memory`, the module that keeps the
Orchestrator's context window small, explicit and crash-safe across restarts. It
implements ADR-0004 **D11** and the roadmap v2 row `OM-1`:

* a deterministic **startup digest** with a fixed section order, an explicit
  character/token budget and `[ABSENT]` markers for missing state;
* a typed, strictly size-bounded **notebook** (`pinned` / `aging` /
  `perishable`) with an append-only on-disk **archive**;
* a single-writer **session lock** that forces a second session into read-only
  mode and only reclaims a lock after verifying the recorded process
  incarnation;
* the latched **context trigger** at `> 50 %`, the work-remaining heuristic, the
  rotation-with-handoff decision, and the full-autonomy self-split;
* a **transactional turn journal** that acknowledges a decision point only after
  full handling and recovers a mid-turn death without repeating anything.

The module **consumes** `saberops.orchestrator_turn_policy` (OT-1) and never
changes it: `ContextUsage`, the strict `> 50 %` rule and the decision-point
idempotency key stay there. Nothing here launches, resumes or terminates a model
session, reads a diff or a log, or writes outside the state root handed to it.

| Module surface | Responsibility |
|---|---|
| `orchestrator_memory.build_startup_digest(facts)` | deterministic, budgeted startup digest |
| `orchestrator_memory.NotebookStore` | typed notebook with bounded size and eviction |
| `orchestrator_memory.NotebookArchive` | append-only archive of evicted entries |
| `orchestrator_memory.OrchestratorSessionLock` | single-writer lock with verified stale reclamation |
| `orchestrator_memory.open_session(...)` | writer session or forced `ReadonlySession` |
| `orchestrator_memory.decide_rotation(...)` | continue / rotate-with-handoff / self-split advice |
| `orchestrator_memory.HandoffStore` | durable, idempotent handoff records |
| `orchestrator_memory.TurnJournal` | transactional decision-point journal and recovery |
| `orchestrator_memory.OrchestratorMemory` | the memory root that owns all of the above |
| `tests/test_orchestrator_memory.py` | hermetic contract and adversarial tests |

## Scope boundary

In scope:

* the digest's fixed sections, budget accounting, `[ABSENT]` markers, truncation
  disclosure and exact retry commands;
* notebook admission, deterministic eviction, archiving, boundary clearing and
  restart persistence;
* lock acquisition, read-only fallback, stale reclamation and fail-closed
  `UNKNOWN` liveness;
* the latched context trigger, the `open / total` work heuristic, rotation
  handoffs and the full-autonomy self-split;
* turn acknowledgement, rollback and restart recovery.

Deliberately out of scope (owned elsewhere):

* measuring real provider context usage, or launching and reattaching CLI
  sessions;
* the wave executor, the run loop, the plan store, the project registry and the
  web UI;
* provider/model selection, quota and capacity decisions;
* any production wiring of this module into the run loop. Until that wiring is
  reviewed and integrated, this is a tested, bounded subsystem, not a working
  session manager for a live run.

A passing unit test here proves the memory contract. It does not prove that a
real CLI session resumes, that a real provider reports usage, or that a real
crash was survived.

## On-disk layout

`OrchestratorMemory(root)` owns exactly these paths below `root`:

| Path | Content | Written how |
|---|---|---|
| `notebook.json` | the live, bounded notebook | whole-file atomic replace (0o600) |
| `notebook-archive.jsonl` | every evicted or boundary-cleared entry | append-only, fsynced |
| `orchestrator-session.lock` | the single-writer holder record | atomic replace (0o600) |
| `handoffs/<handoff-id>.json` | durable rotation handoffs | create-once, atomic replace |
| `turn-journal.jsonl` | decision-point OPEN/HANDLED/ROLLED_BACK records | append-only, fsynced |

Every file is plain versioned JSON (`"orchestrator-memory/1"`). A corrupt file is
not silently emptied or ignored: the reader raises (`NotebookStoreError`,
`SessionLockUnsafeError`, `TurnJournalError`, `OrchestratorMemoryError`) so the
owner sees the problem instead of losing state.

## Startup digest

`build_startup_digest` renders **every** section in this fixed order:

1. `project` — project id, plan reference, base SHA, each with `[ABSENT]`;
2. `since_last_session` — last session timestamp and the status summary; this is
   the project-level **Continue** summary;
3. `recent_accepted_runs` — run id, accepted SHA, timestamp;
4. `open_backlog` — item id, state, title;
5. `failed_runs` — run id, reason, and the **exact** retry/continue command;
6. `next_roadmap_item` — item id, title, state;
7. `open_questions`;
8. `notebook` — `[pinned|aging|perishable] key: text`;
9. `session_state` — writer/read-only mode, session id, lock holder;
10. `context_status` — measured usage, whether it is above 50 %, and the latch.

### Budget

| Budget | Value | Meaning |
|---|---|---|
| `DIGEST_MAX_CHARS` | 12 000 | hard character ceiling for the rendered body |
| `DIGEST_MAX_TOKENS` | 3 000 | hard token ceiling (`ceil(chars / 4)`) |
| `DIGEST_MAX_ITEMS_PER_SECTION` | 32 | per-section item cap at render time |
| `DIGEST_MAX_ITEM_CHARS` | 320 | per-item cap for free text |
| `MAX_DIGEST_ITEMS` | 512 | hard input bound; more is refused, not truncated |

`estimate_tokens` is deliberately simple and deterministic (one token per four
characters, rounded up); it is a budgeting convention, not a tokenizer.

### Absence versus truncation

The two are never conflated:

* a section with **no state** renders `[ABSENT]` (and, for the status section,
  `[ABSENT] status since the last session`);
* a section that **had state** but lost items to the budget renders the kept
  items plus `[TRUNCATED n more]`, and the section name is listed in
  `digest.truncations`.

Reduction is deterministic and ordered:
`since_last_session` → notebook (`perishable`, then `aging`) → `open_questions`
→ `recent_accepted_runs` → `open_backlog`. `pinned` notebook entries, SHAs,
section headings, exact retry commands and `[ABSENT]` markers are never reduced.
If the irreducible digest cannot fit the budget, `build_startup_digest` raises
`DigestBudgetError` instead of emitting a quietly incomplete digest.

## Notebook budgeting

`NotebookTier` has three values with a strict priority order:

| Tier | Priority | Lifetime |
|---|---|---|
| `pinned` | `1 000 000` | permanent across restarts; never evicted |
| `aging` | `10 000 - 1 000 × age` (floored at 0) | decays at every wave/turn boundary |
| `perishable` | `5 000` | cleared at the wave/turn boundary |

`advance_age()` increments every `aging` entry by one boundary, so a sufficiently
old `aging` entry ranks *below* a fresh `perishable` one and is evicted first.
`clear_perishable()` removes every perishable entry at a boundary.

Bounds: `NOTEBOOK_MAX_CHARS` (8 000 rendered characters),
`NOTEBOOK_MAX_ENTRIES` (512) and `NOTEBOOK_MAX_ENTRY_CHARS` (500 per entry).

`put(entry)` plans evictions on a copy and only then commits, so a refused write
never leaves a partially mutated notebook. Eviction order is deterministic:
lowest priority first, then oldest, then earliest inserted. If the incoming entry
is itself the lowest-priority candidate, it is refused with
`NotebookBudgetError` rather than displacing higher-priority state. Every evicted
entry (budget eviction or boundary clear) is appended to
`notebook-archive.jsonl`; the archive is never rewritten.

## Lock protocol

The lock is a single-writer advisory lock *with verification*, not a kernel
lock and not proof of liveness by itself.

1. `acquire(session_id, identity)` reads the existing holder.
   * no lock file → the caller becomes the writer;
   * the same session id → idempotent re-acquire (`already_held_by_self`);
   * a **live** holder (`Liveness.LIVE`) → `SessionLockedError`; the caller is
     offered a `ReadonlySession` instead;
   * an **undecidable** holder (`Liveness.UNKNOWN`) → `SessionLockUnsafeError`;
     the lock is never reclaimed and no session is opened;
   * a **provably dead** holder (`Liveness.DEAD`) → the lock is reclaimed and the
     displaced session id is recorded in `LockAcquisition.reclaimed_from`.
2. The holder record persists the full `ProcessIdentity` (host, boot id, pid,
   start ticks, session id, process group id), so PID reuse cannot masquerade as
   the original holder.
3. A present but unreadable or empty lock file is **unsafe**, never absent.
4. `release(session_id)` deletes the lock only when the caller is the recorded
   holder; a foreign release returns `False` and leaves the file in place.

`open_session` maps the three outcomes directly: writer session, `ReadonlySession`
bound to the live holder, or `SessionLockUnsafeError`. A `ReadonlySession` may
build the digest, read the notebook and read handoffs; **every** mutating method
(`put_notebook_entry`, `clear_perishable`, `advance_age`, `record_handoff`,
`begin_turn`) raises `ReadonlySessionError`, and `release()` returns `False`.

The lock is advisory: two processes that both observe `DEAD` at the same instant
may both reclaim it. That is acceptable because the lock protects Orchestrator
memory (which is itself atomic and idempotent), not money or gate evidence.

## Context trigger, rotation and handoffs

`ContextTrigger` is **latched**: `observe(usage)` sets the latch when the
Orchestrator's measured context is strictly above 50 % of its model window, and
the latch is never cleared by a later, smaller observation (a compaction cannot
hide the crossing). The peak observation is retained. `reset()` is the only way
to unlatch, and it is called after an actual rotation.

The trigger measures the **Orchestrator's** context only; worker context is not
part of this decision.

Rotation is decided at a turn boundary:

| Condition | Result |
|---|---|
| trigger not latched | `CONTINUE_CURRENT_SESSION` |
| latched, `open / total <= 25 %` | `CONTINUE_CURRENT_SESSION` (finish the work) |
| latched, `open / total > 25 %` or open count unknown | `ROTATE_SESSION` with a handoff |
| rotating, full-autonomy preset | `self_split=True`, handoff-started follow-ups |
| rotating, any other mode | `owner_question_required=True` |

`ChecklistProgress` is built from the task's **starting** checklist.
`open_items=None` (an unknown count) is treated as *more than* the continue
threshold, never as finished work. The threshold is integer arithmetic
(`open × 100 > total × 25`), so the boundary is exact.

### Handoff record

`OrchestratorHandoff` captures, deterministically:

* `handoff_id` = `handoff-<run>-<wave>-<decision-point>`;
* task, run, wave and decision point ids;
* the session being rotated away from;
* total and open checklist items, the open item ids and the remaining ratio;
* the peak measured context at rotation;
* the follow-up task ids (full autonomy only);
* the reason.

`HandoffStore.record` is idempotent for identical content (a crash replay is a
no-op) and raises `HandoffConflictError` when the same id is re-recorded with
different content: a durable handoff is never silently overwritten.

## Transactional turns and crash recovery

`TurnJournal` appends one JSON line per state change, keyed by the OT-1
`idempotency_key(decision_point_id, run_id, wave_id)`.

| Operation | Rule |
|---|---|
| `begin(...)` | opens `OPEN`; a `HANDLED` key returns `already_handled=True` and writes nothing |
| `commit(key, actions)` | the acknowledgement: `OPEN → HANDLED`; identical replay is a no-op; different actions raise `TurnConflictError` |
| `rollback(key, reason)` | only an `OPEN` turn; `HANDLED` raises `TurnJournalError` |
| `recover()` | every still-`OPEN` key is rolled back and returned as a recovery plan |

A decision point is acknowledged only after full handling: nothing is `HANDLED`
until `commit` runs. If the process dies mid-turn, `recover()`:

1. appends `ROLLED_BACK` for the interrupted turn;
2. reports the turn's uncommitted intents as `rolled_back_actions` (they are
   **not** re-executed);
3. re-issues the **exact** decision point (`decision_key`,
   `decision_point_id`) into a clean session (`session_action="new_session"`)
   with a smaller summary brief (`brief_mode="summary"`);
4. escalates to `owner_question_required=True` once the attempt count reaches
   `MAX_FAILED_ATTEMPTS_BEFORE_OWNER_QUESTION` (2), matching the OT-1 turn
   policy instead of a silent third attempt.

Calling `recover()` again returns `()` because the interrupted turn is no longer
`OPEN`, so a restart repeats nothing.

## Validation and fail-closed rules

All inputs are validated; malformed or unbounded input raises
`OrchestratorMemoryError` (or a subclass):

| Rejected input | Reason |
|---|---|
| identifier not matching `[A-Za-z0-9._/-]` (max 128) | path/JSON injection |
| SHA not exactly 40 lowercase hex characters | ambiguous provenance |
| newline, carriage return or NUL in text | content smuggling |
| more than 512 items in a digest/checklist/journal list | unbounded input |
| `open_items > total_items` or `total_items <= 0` | impossible progress |
| a negative token count or corrupt `ContextUsage` | corrupt telemetry |
| an unknown authority mode | never guessed |

## Testing

`tests/test_orchestrator_memory.py` is hermetic: no provider, CLI, login,
network, database, model launch or sleep. All filesystem writes go to
`tmp_path`, process liveness is always injected, and the clock is fixed. Every
test carries a `Covers: OM-1 ...` docstring line.

Run it from a worktree:

```bash
.venv/bin/python -m pytest tests/test_orchestrator_memory.py -q
```

The lane gate additionally runs `ruff` and strict `mypy` across the repository.

## Deferred work

Not claimed by this module:

* measuring real provider context usage and persisting the trigger across a real
  session restart (the trigger object is pure; wiring it to the journal is an
  Integrator concern);
* launching, resuming and terminating CLI/desktop sessions;
* wiring the digest, notebook, lock and journal into the wave executor and the
  run-all autonomy loop;
* the read-only *analyst* sub-session (D11) and project plan lookup (D13);
* the product UI for lock state, archive browsing and handoff review.
