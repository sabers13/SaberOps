# Owner question queue and the five-part escalation protocol (Q-1, Step 012)

The engine never guesses an owner decision (ADR-0004 §3 D6, WORKFLOW §7 as
data). It raises a **keyed question** instead. This document describes the
queue that carries those questions from any role — including a worker mid-run —
to the owner's answer, and the escalation protocol that accompanies them.

The module is `saberops.questions`; the CLI surface is `orch questions ...`;
the web surface is the FastAPI router returned by `build_questions_router()`.

| Module / surface | Responsibility |
|---|---|
| `saberops.questions` | one service layer: the queue, the status transitions, the escalation contract, the divergence and audit projections, the standing-rule proposals |
| `orch questions ...` (`saberops.cli`) | thin CLI over exactly one queue verb per subcommand |
| `build_questions_router()` | thin local-only web entry point over the same verbs (mounted by the web app) |
| `$XDG_STATE_HOME/saberops/questions.json` | the durable owner document (`ORCH_QUESTIONS_STORE` overrides it) |

## The queue

A question is keyed and immutable in its ask payload:

* `key` is the idempotency key. The same key always maps to the same
  deterministic id (`q-<sha256(key)[:12]>`), so a retried ask — a resent worker
  message, a repeated web request — never creates a second question.
* The same key with a *different* payload fails closed. A conflicting re-ask is
  never silently merged; a superseding decision is a new keyed question that
  names the earlier one in `supersedes` — the reference must resolve to a known
  question, and linking to it never closes it.
* Any role may raise one: `OWNER`, `ORCHESTRATOR`, `WORKER`, `SURGEON`,
  `REVIEWER`, `INTEGRATOR`, `ENGINE`.

### Statuses and transitions

| Status | Meaning | Left by |
|---|---|---|
| `OPEN` | waiting for an answer | `answer`, `defer`, `mark_moot`, `record_divergence` |
| `DEFERRED` | postponed until `defer_until`; resurfaces on that date | `answer`, `defer` (a later date), `mark_moot` |
| `ANSWERED` | the owner (or the Orchestrator, where allowed) answered | nothing; a later conflict becomes a divergent record |
| `MOOT` | closed on explicit evidence that it no longer matters | nothing; a later conflict becomes a divergent record |

`closed_by` reports the verb that produced the current status (`answer`,
`defer`, `mark_moot`), which is what the CLI and the web API show.

### The owner's exact words

An answer's `words` are stored verbatim — leading and trailing whitespace,
line breaks and internal spacing are preserved byte for byte in the document and
on every read path. Only whitespace-only answers are refused.

### Defer-until and evidence-backed moot close

* `defer` stores an ISO `defer_until` date. `pending(as_of)` returns `OPEN`
  questions plus `DEFERRED` questions whose date has arrived; `question_view`
  marks those `due`/`resurfaced`.
* `mark_moot` requires non-empty evidence. A moot close without evidence is
  refused, so a question cannot disappear on a claim alone.

### Questions are never closed by task completion or cleanup

* `TASK_LIFECYCLE_CLOSE_AUTHORITIES` is deliberately empty, and the module
  exposes no delete, prune, purge, clear or cleanup path: questions are
  evidence.
* Only the explicit verbs above may move a question out of `OPEN`. Answering,
  deferring or closing one question leaves every sibling question — including
  questions raised by the same run — untouched.
* A later conflicting record is **reported**, never applied over the existing
  outcome (see Divergence below).

## The five-part escalation protocol

Every question carries all five parts; a question that cannot state them is not
well-formed and is refused (`EscalationError`):

1. **`requirement`** — the requirement that forces the question.
2. **`proposed_expansion`** — the expansion being proposed.
3. **`smallest_compliant`** — the smallest compliant option.
4. **`consequences`** — the concrete consequences.
5. **`recommendation`** — the Orchestrator's recommendation.

## Who may answer

The answer authority is derived from the question's decision class, never chosen
by the caller:

| Class | Authority |
|---|---|
| `MOVE_MAIN`, `PUSH`, `QUARANTINE`, `BILLING_OR_POSTURE`, `TEST_CHANGE_AGAINST_ADR`, `DELETE_OUTSIDE_ENGINE_FOLDERS`, `APPROVE_CONFIG_PROPOSAL`, `OTHER` | `OWNER` only |
| `REVIEW_FINDING_CLASSIFICATION`, `FIX_UP_CONTENT`, `G3_FAILURE_ROUTING`, `ROUTINE_QUESTION` | `ORCHESTRATOR` allowed |

Rules enforced in code:

* An **implementer role** (`WORKER`, `SURGEON`, `REVIEWER`, `INTEGRATOR`) can
  never answer a question; `OWNER` authority requires the `OWNER` role and
  `ORCHESTRATOR` authority requires the `ORCHESTRATOR` role.
* An **Orchestrator never answers its own finding**: a question raised by the
  `ORCHESTRATOR` role, or by the same actor that would answer it, is refused.
* An Orchestrator-alone answer must carry `evidence` **and** the
  `rejected_alternative` (RD-1: logged with evidence and the rejected
  alternative), which is what makes the audit list usable.
* An unknown decision class fails closed to `OWNER`.

### Divergent records

A later answer that conflicts with an answered question, a moot close after an
answer, or an answer after a moot close is recorded as a `DivergentRecord`
(`DIFFERENT_WORDS`, `DIFFERENT_OPTION`, `ANSWER_AFTER_MOOT`, `MOOT_AFTER_ANSWER`,
`DIFFERENT_EVIDENCE`). The primary answer and the status are **not** changed and
the question is **not** auto-closed. Divergences are reported by
`orch questions divergences` and in the web API, always with the question's
current status attached.

## Audit and standing rules

* `orchestrator_alone_decisions()` is the audit list of every decision the
  Orchestrator took without the owner: the question, its decision class, the
  forcing requirement, the Orchestrator's recommendation, the answer words, the
  evidence and the rejected alternative.
* `standing_rule_proposals(threshold=3)` groups **owner** answers by question
  kind and answer (the chosen option, or the words when there is no option) and
  proposes one standing rule per group that reached the threshold. A proposal
  targets `CFG-1` (Step 013) and is inert data: computing proposals writes
  nothing and applies nothing. A divergent answer simply does not join the
  group.

## Storage

One owner-level JSON document, `$XDG_STATE_HOME/saberops/questions.json`
(`ORCH_QUESTIONS_STORE` pins an explicit file for tests and diagnostics; the CLI
`--store` flag does the same). Writes are atomic (`<name>.tmp` + replace) and an
unreadable, malformed or wrongly-versioned document fails closed with
`QuestionStoreError` instead of degrading to an empty queue.

## CLI

```bash
orch questions ask --key run_abc/gate-block --subject "May the lane touch the protected test?" \
  --kind gate-block --decision-class TEST_CHANGE_AGAINST_ADR --role WORKER --by worker:run_abc \
  --run-id run_abc --blocks wave3/lane-a \
  --requirement "..." --expansion "..." --smallest "..." --consequences "..." --recommendation "..."

orch questions list [--status OPEN|DEFERRED|ANSWERED|MOOT] [--as-of 2026-11-01] [--run-id RUN] [--json]
orch questions show REF [--as-of DATE] [--json]
orch questions answer REF --words "..." [--authority owner|orchestrator] [--by ACTOR] [--role ROLE] \
  [--chosen-option OPTION] [--evidence E] [--rejected-alternative A] [--json]
orch questions defer REF --until YYYY-MM-DD [--by ACTOR] [--role OWNER|ORCHESTRATOR] [--note TEXT]
orch questions moot REF --evidence "..." [--by ACTOR] [--role OWNER|ORCHESTRATOR|ENGINE]
orch questions diverge REF --reason REASON --words "..." --by ACTOR --role ROLE [--note TEXT]
orch questions divergences [REF] [--json]
orch questions standing-rules [--threshold 3] [--json]
orch questions audit [--json]
```

Exit codes follow the rest of the CLI: `0` success, `1` a typed refusal
(`[!] ...` on stderr, no traceback), `2` an argparse usage error.

## Web entry point

`build_questions_router(store_path=None)` returns a FastAPI `APIRouter` with
prefix `/questions`: `GET ""`, `GET /pending`, `GET /show`, `GET /divergences`,
`GET /standing-rules`, `POST /ask`, `POST /answer`, `POST /defer`, `POST /moot`,
`POST /divergence`. Every route calls exactly one queue verb and holds no
authority of its own; a non-loopback client is refused with `403`, and the
mounting application keeps its own local-only origin guard.

The web application (`saberops.web`) does not mount this router yet: the web
console cut belongs to a separate step, and Step 012's authorised scope is the
service layer plus the CLI. UI-1 (Step 019) wires the answer surface into the
local UI. The MCP tools of ADR-0004 D7 (`list_questions`, `answer_question`)
map onto the same verbs when M3 lands, and inherit the same refusals.

## Boundaries

* No SQLite table: questions are one owner document, in line with the other
  owner-level documents (`bindings.json`, `control_plane.json`).
* This module proposes standing rules; it never applies configuration (CFG-1
  owns approval and audit of applied configuration).
* Worker status messages that raise decisions (WX-2) feed this queue's `ask`
  verb; the status channel's own semantics stay with WX-2.

## Verification

`tests/test_questions.py` covers the escalation contract, derived authority,
keyed idempotency, verbatim storage, the implementer/self-answer refusals,
divergence, defer/resurface, evidence-backed moot close, the
never-closed-by-lifecycle invariant, the audit list, standing-rule proposals,
the durable document (round trip, corrupt/unwritable, atomic write) and the web
router. `tests/test_cli_questions.py` drives the real `orch questions` entry
point with an isolated owner state. Both are hermetic: no provider call, no
network, no clock dependency.
