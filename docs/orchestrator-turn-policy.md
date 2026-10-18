# Orchestrator turn admission and context rotation (OT-1, Step 016)

This document describes `saberops.orchestrator_turn_policy`, the pure policy
module that answers one question at an Orchestrator decision point: **should the
engine start a fresh Orchestrator session, reuse the existing one, continue the
turn that is already in progress, defer, block, or ask the owner?** It also
describes the bounded brief the engine hands to that turn.

The module encodes ADR-0004 **D11** (Orchestrator turns, brief, notebook,
session rotation, transactional turns). It is an **advice projection only**: the
runtime still owns the session-ownership fence, the idempotent turn transaction
and every side effect. Nothing in this module starts, resumes, terminates or
persists a session, and nothing reads or writes the filesystem, network, clock,
environment or a model provider.

| Module surface | Responsibility |
|---|---|
| `orchestrator_turn_policy.TurnRequest` | all explicit, typed facts for one decision point |
| `orchestrator_turn_policy.decide_turn(request)` | deterministic admission/rotation advice |
| `orchestrator_turn_policy.build_brief(request)` | bounded, fact-only brief for the turn |
| `orchestrator_turn_policy.TurnDecision` | immutable, explainable, versioned result |
| `tests/test_orchestrator_turn_policy.py` | hermetic invariant and adversarial contract tests |

## Scope boundary

In scope:

* admission advice at the four bounded D11 decision points;
* the strict `> 50 %` between-turns rotation rule and its boundary cases;
* the UNKNOWN-usage safe default;
* the mid-turn rotation prohibition;
* two-failure escalation and one stable idempotency key per decision point;
* a size-capped, disclosed, reference-only brief (plan reference, standing
  decisions, notebook, wave state, question, evidence paths).

Deliberately out of scope (owned elsewhere):

* the durable turn journal, lease and idempotent persistence;
* CLI session launch, reattachment and crash recovery;
* the engine verifier that measures context usage and persists failure counts;
* the analyst sub-session, model dispatch and provider selection;
* plan-store, registry, wave-executor or web-UI behaviour;
* any adapter or glue for Step 022 (`OM-1`); a later Integrator owns wiring once
  the upstream engine baseline is accepted.

A passing unit test here proves the policy contract, not that a real CLI session
resumes or that crash recovery works.

## Decision points

`DecisionKind` is a closed enum. ADR-0004 D11 wakes the Orchestrator only at
these points; arbitrary autonomous model calls are not decision points and are
rejected by validation.

| Kind | When it is raised |
|---|---|
| `plan_wave` | plan wave *k*: prompts, contracts, glue, retirement table |
| `triage_review` | triage review findings and route fix-ups |
| `g3_routing` | route a G3 failure the rules leave open |
| `wave_close` | close wave *k* and curate lessons |

## Actions and reason codes

`TurnAction` is the only output vocabulary. The runtime applies it; the policy
never does.

| Action | Meaning |
|---|---|
| `NEW_SESSION` | start a fresh session and deliver the brief |
| `REUSE_SESSION` | reuse the existing resumable session |
| `CONTINUE_EXACT_SESSION` | a turn is in progress; continue that exact session |
| `DEFER` | usage is unknown and the caller requires certainty; retry later |
| `BLOCKED` | the requested transition is unsafe or the brief cannot be bounded |
| `OWNER_QUESTION` | repeated failure; escalate to the owner instead of a third attempt |

Every decision carries a stable `ReasonCode` and an `explanation` tuple whose
first entry is prefixed with the decision-point id, so a journal or run report can
explain *why* the engine acted without re-deriving the policy.

| Reason code | Action |
|---|---|
| `new_session_no_session` | `NEW_SESSION` |
| `new_session_rotated_over_threshold` | `NEW_SESSION` |
| `new_session_context_unknown` | `NEW_SESSION` |
| `reuse_session_within_budget` | `REUSE_SESSION` |
| `reuse_session_at_threshold` | `REUSE_SESSION` |
| `continue_turn_in_progress` | `CONTINUE_EXACT_SESSION` |
| `blocked_rotation_mid_turn` | `BLOCKED` |
| `blocked_context_unknown` | `BLOCKED` |
| `blocked_brief_budget_exceeded` | `BLOCKED` |
| `defer_context_unknown` | `DEFER` |
| `owner_question_repeated_failure` | `OWNER_QUESTION` |

## The decision procedure

`decide_turn(request)` evaluates the rules in a fixed precedence order. The first
matching rule wins, which makes the result independent of input ordering.

1. **Repeated failure.** If `failed_attempts >= 2` for the same decision point,
   return `OWNER_QUESTION` / `owner_question_repeated_failure`. The brief is
   still built if it fits, so the owner question can carry the pinned state. This
   rule precedes every other rule, including mid-turn handling.
2. **Turn in progress.** If `at_turn_boundary` is false, a rotation request is
   refused (`BLOCKED` / `blocked_rotation_mid_turn`,
   `rotation_refused=True`); otherwise the exact session continues
   (`CONTINUE_EXACT_SESSION` / `continue_turn_in_progress`). Neither outcome is
   permission to start a fresh turn, and no brief is built mid-turn.
3. **No resumable session.** Return `NEW_SESSION` / `new_session_no_session`.
   There is nothing to rotate away from, so this precedes usage handling.
4. **Measured usage.** With a `ContextUsage(used_tokens, total_tokens)`:
   * strictly **above** 50 % → `NEW_SESSION` / `new_session_rotated_over_threshold`
     with `rotation_required=True`;
   * **exactly** 50 % → `REUSE_SESSION` / `reuse_session_at_threshold`;
   * **below** 50 % → `REUSE_SESSION` / `reuse_session_within_budget`.
5. **UNKNOWN usage.** Never assume the existing session has ample budget. The
   caller can choose the explicit policy:
   * `PREFER_NEW_SESSION` (default) → `NEW_SESSION` /
     `new_session_context_unknown`, `rotation_required=True`;
   * `DEFER_WHEN_UNKNOWN` → `DEFER` / `defer_context_unknown`;
   * `BLOCK_WHEN_UNKNOWN` → `BLOCKED` / `blocked_context_unknown`.
   No UNKNOWN policy ever returns `REUSE_SESSION`.

`rotation_requested` is meaningful only while a turn is in progress, where it is
refused. At a turn boundary the measured 50 % rule decides; a caller cannot force
rotation while the session is within budget, because ADR-0004 D11 makes rotation
an engine decision, not a caller preference.

### The 50 % boundary in integer maths

`ContextUsage.exceeds_percent(50)` is `used * 100 > total * 50`, and
`at_percent(50)` is the equality. This avoids floating-point drift and keeps
`1 / 2` exactly at the threshold (reuse), while `51 / 100` rotates. A zero
`total` is invalid telemetry, not "ample budget", and fails closed.

## Turn inputs

`TurnRequest` is frozen. Every field is validated in `__post_init__`; malformed
or unbounded input raises `TurnPolicyError` (fail closed) rather than guessing.

| Field | Contract |
|---|---|
| `decision_point_id` | stable id for this wake-up; same value across replays |
| `run_id`, `wave_id` | run/wave identity; alphanumeric-leading `[A-Za-z0-9._/-]`, max 128 chars |
| `kind` | one of the four `DecisionKind` members (value or member name coerces) |
| `at_turn_boundary` | boolean; true only between turns |
| `session_resumable` | boolean; false when no session exists or it cannot resume |
| `context_usage` | `ContextUsage` or `None` for explicitly UNKNOWN |
| `failed_attempts` | persisted attempts for **this** decision point, `0..10_000` |
| `rotation_requested` | boolean; a request made while the turn is in progress |
| `unknown_context_policy` | explicit `DEFER`/`BLOCK`/`PREFER_NEW_SESSION` choice |
| `plan_reference` | optional text label for the plan/standing decisions |
| `standing_decisions` | ordered owner-approved decisions, at most 64 input entries |
| `notebook` | curated Orchestrator notebook, at most 512 input entries |
| `wave_state` | `WaveState` with exact SHAs and an outcome tag; must match `wave_id` |
| `question` | the turn's question text |
| `evidence_paths` | **paths only**, at most 512 input entries |

The caller must supply the authoritative, persisted `failed_attempts` count. The
policy does not increment or store it, and a bool is not accepted as an integer.

### Wave state

`WaveState` pins `wave_id`, `base_sha`, `head_sha` and `outcome_tag`. SHAs must be
exact 40-character lowercase hex — abbreviations are refused. The outcome tag is
an uppercase token such as `G3_PASS` or `REVIEW_ACCEPT`. A `WaveState` whose
`wave_id` differs from the request's `wave_id` is a conflicting duplicate and
fails closed.

## Idempotency and transactional turns

`TurnRequest.idempotency_key` (and `idempotency_key(decision_point_id, run_id,
wave_id)`) returns:

```text
orchestrator-turn-policy/1:<run_id>:<wave_id>:<decision_point_id>
```

A failed turn replay uses the **same** decision point id and therefore the same
key; the key is also stable across kinds and attempts. The runtime journals the
decision under that key so a dead or overflowing turn is re-issued to a fresh
session exactly once. After two failed attempts the policy returns
`OWNER_QUESTION`; there is never a silent third attempt.

## The brief

`build_brief` produces a `Brief` with a fixed structure and deterministic
serialization. It contains facts only, in this order:

1. `plan_reference` — an optional label for the plan and standing decisions;
2. `standing_decisions` — ordered, capped text entries (stable cacheable prefix);
3. `notebook` — the curated Orchestrator notebook, capped, one single-line fact
   per entry (embedded newlines, carriage returns and NUL are refused);
4. `wave_state` — engine state: `wave_id`, exact `base_sha`/`head_sha`, outcome tag;
5. `question` — the turn's question;
6. `evidence_paths` — **references** to review/gate evidence, never content;
7. `truncations` — the sections that were reduced, in disclosure order;
8. `policy_version` — `orchestrator-turn-policy/1`.

### Caps and truncation

Two independent layers bound the brief:

| Layer | Constant | Value | Behaviour |
|---|---|---|---|
| Brief item count | `BRIEF_MAX_STANDING_DECISIONS` | 16 | over-cap items dropped, disclosed |
| Brief item count | `BRIEF_MAX_NOTEBOOK_ENTRIES` | 32 | over-cap items dropped, disclosed |
| Brief item count | `BRIEF_MAX_EVIDENCE_PATHS` | 32 | over-cap paths dropped, disclosed |
| Brief item size | `BRIEF_MAX_TEXT_ITEM_LENGTH` | 320 | item cut with `…`, disclosed |
| Brief mandatory budget | `BRIEF_MAX_MANDATORY_CHARS` | 3 000 | overflow **blocks** the turn |
| Brief total budget | `BRIEF_MAX_TOTAL_CHARS` | 12 000 | optional sections reduced, disclosed |
| Hard input count | `MAX_STANDING_DECISIONS` | 64 | rejected as unbounded |
| Hard input count | `MAX_NOTEBOOK_ENTRIES` | 512 | rejected as unbounded |
| Hard input count | `MAX_EVIDENCE_PATHS` | 512 | rejected as unbounded |
| Hard item size | `MAX_TEXT_ITEM_LENGTH` | 4 000 | rejected |
| Hard path size | `MAX_PATH_LENGTH` | 1 024 | rejected |

The **mandatory** content is the question plus the pinned wave state. If it
exceeds `BRIEF_MAX_MANDATORY_CHARS`, `build_brief` raises `BriefBudgetError` and
`decide_turn` returns `BLOCKED` / `blocked_brief_budget_exceeded` with no brief.
Mandatory content is never silently truncated or summarised.

Optional sections are first capped by count and item size, then reduced one item
at a time until the total fits `BRIEF_MAX_TOTAL_CHARS`. The reduction order is
evidence paths, then notebook, then standing decisions; every reduction adds its
section name to `truncations`. The policy never substitutes a model-written
summary for dropped facts.

### References only

`evidence_paths` are validated as paths: embedded `\n`, `\r` or `\x00` is
refused, so a caller cannot smuggle a log body, diff or transcript through a
"path" field. The policy never opens a path. Credentials, secret values,
environment dumps and arbitrary file bodies must never be supplied as brief
fields.

## Result projection

`TurnDecision.as_dict()` and `Brief.as_dict()` have fixed key order, and
`canonical_json()` uses compact separators with `ensure_ascii=False`. Identical
immutable input yields a byte-identical projection, so a run report can hash it.

`TurnDecision` also reports `context_usage_known`, `used_tokens`,
`total_tokens`, `rotation_required` and `rotation_refused` so the runtime can act
without re-parsing the explanation. The returned dataclasses are frozen; callers
cannot mutate a decision after it is journaled.

## Integration guide for caller modules (Step 022 `OM-1` and later)

The caller is the engine-side turn driver, not the model. A conforming caller
does the following:

1. **Detect the decision point.** It derives `decision_point_id`, `run_id`,
   `wave_id` and `kind` from engine state and its persisted turn journal. The id
   must be stable across replays of the same decision point.
2. **Determine the turn boundary.** `at_turn_boundary=True` only between turns;
   while a model turn is executing, pass `False`.
3. **Determine resumability.** Pass `session_resumable=True` only when an exact,
   resumable session exists for this run/wave.
4. **Measure or omit usage.** Pass `ContextUsage(used, total)` when the provider
   exposes trustworthy counts; pass `None` when usage is UNKNOWN. Never invent a
   ratio, and never clamp a corrupt measurement into range.
5. **Supply the persisted failure count.** Read `failed_attempts` for this
   decision point from the journal; do not count unrelated historical failures.
6. **Choose an UNKNOWN policy.** The default `PREFER_NEW_SESSION` is the safe
   choice. Use `DEFER_WHEN_UNKNOWN` when spending a turn before certainty is
   unacceptable, or `BLOCK_WHEN_UNKNOWN` when only a measured session may run.
7. **Build the brief from engine facts.** Supply the plan reference, ordered
   standing decisions, the curated notebook, the pinned `WaveState`, the
   question, and evidence **paths**. Do not pass file contents.
8. **Apply the advice under the ownership fence.** The runtime — not this module
   — starts/reuses/continues sessions, writes the journal, and calls providers.
   Journal both the decision and its `decision_key` before acting.
9. **Honour `BLOCKED`, `DEFER` and `OWNER_QUESTION`.** They are terminal for the
   automated path; route them to the owner-question queue or retry later rather
   than retrying blindly.
10. **Re-issue a failed turn with the same key and a smaller brief.** If the
    turn died or overflowed, replay the same decision point; after two failures
    the policy escalates.

Recommended call shape:

```python
from saberops.orchestrator_turn_policy import (
    ContextUsage,
    DecisionKind,
    TurnRequest,
    decide_turn,
)

request = TurnRequest(
    decision_point_id="dp-wave-4-close",
    run_id="run-2026-10-18",
    wave_id="wave-4",
    kind=DecisionKind.WAVE_CLOSE,
    at_turn_boundary=True,
    session_resumable=True,
    context_usage=ContextUsage(used_tokens=41_000, total_tokens=80_000),
    failed_attempts=0,
    wave_state=wave_state,
    question="Close wave 4 and curate lessons.",
    evidence_paths=("evidence/wave-4/g3.txt", "evidence/wave-4/review.md"),
)
decision = decide_turn(request)
if decision.action.value in {"BLOCKED", "DEFER", "OWNER_QUESTION"}:
    journal(decision.decision_key, decision)  # route to owner/retry
else:
    journal(decision.decision_key, decision)
    runtime.apply_turn_advice(decision)  # runtime owns the side effect
```

Do **not** import this module from the session runtime, the journal or the CLI to
bypass the runtime's fence, and do not treat `NEW_SESSION` as proof that a model
or provider was launched.

## Validation and failure modes

All failures are explicit exceptions (`TurnPolicyError`, or `BriefBudgetError`
for the mandatory brief budget); the policy never returns a guessed action for
malformed input.

| Rejected input | Why |
|---|---|
| empty, whitespace-padded, non-ASCII or over-128-char identifier | ambiguous journal keys |
| bool or negative token count | corrupt telemetry |
| zero or negative `total_tokens` | impossible ratio |
| `used_tokens > total_tokens` | impossible ratio |
| non-`ContextUsage` usage object | untrusted telemetry |
| bool, negative or > 10 000 `failed_attempts` | unbounded replay state |
| unknown `DecisionKind` / `UnknownContextPolicy` | out-of-contract wake-up |
| abbreviated, uppercase or non-hex SHA | ambiguous wave pin |
| lowercase, empty or over-32-char outcome tag | unstable wave status |
| `wave_state.wave_id != wave_id` | conflicting wave identity |
| empty text entry, over-4 000-char entry | unbounded brief input |
| over-512 entry notebook or evidence list | unbounded brief input |
| `\n`, `\r` or `\x00` in an evidence path | content smuggling |
| `\n`, `\r` or `\x00` in a notebook entry | content smuggling into the curated notebook |
| mandatory brief content over 3 000 chars | the turn cannot be briefed safely |

## Testing

`tests/test_orchestrator_turn_policy.py` is hermetic: no providers, CLIs, login,
network, database, filesystem mutation, sleeping or engine launch. Each test
carries a `Covers: ADR-0004 D11 ...` docstring line naming the invariant it
protects. The suite covers the below/equal/above threshold boundary, no-session
and UNKNOWN handling, corrupt telemetry, mid-turn rotation refusal, failure
escalation, idempotency, decision kinds, identifier/SHA/tag validation, brief
caps and disclosure, reference-only evidence, deterministic serialization,
immutability and the absence of input side effects.

Run it from a worktree:

```bash
.venv/bin/python -m pytest tests/test_orchestrator_turn_policy.py -q
```

The lane gate additionally runs ruff and strict mypy across the repository
(`bash scripts/lane-check.sh --base <base> --also tests/test_orchestrator_turn_policy.py`).

## Deferred work

The following remain for later steps and are not claimed by this module:

* the durable turn journal/lease, idempotent persistence and crash recovery;
* CLI/desktop session launch, reattachment and mid-turn overflow handling;
* the verifier that measures provider context usage;
* the read-only analyst sub-session;
* project plan lookup/resume briefing (D13);
* wiring the policy into the wave executor and the run-all autonomy loop;
* any adapter for `OM-1` (Step 022), which an Integrator owns after the upstream
  engine baseline is accepted.

Until that wiring is reviewed and integrated, this document describes a pure,
tested policy projection — not a working Orchestrator session manager.
