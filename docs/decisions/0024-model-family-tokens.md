# ADR-0024: Closed family vocabulary and submit-time family feasibility

- **Status:** Accepted. Implemented in Phase 1 lane F21 (Wave 3).
- **Date:** 2026-11-19
- **Decider:** Saber (owner), Phase 1 plan lane F21
- **Related:** [ADR-0001](0001-saberops-engine-mcp-first.md) (callers state
  model-family constraints, not providers), [ADR-0005](0005-mcp-first-engine-and-phase-2-board.md)
  (three roles, one record of truth), [ADR-0011](0011-bounded-automatic-actions.md)
  (bounded automatic actions)

## 1. Context

A cross-CLI proxy subagent asks SaberOps for a specific worker family: "run this
on Claude", "run this on Codex", "run this on OpenCode Go". The MCP surface
already exposes `requirements.model_family_include`, and admission already
applies it with `matches_model_family`. Two things were missing.

1. **No documented vocabulary.** The tokens are implicit in the matcher. Two
   natural-sounding tokens are wrong: `claude` also matches a Go-hosted Claude
   model (`opencode-go/claude-haiku-5-5`), and `opencode` matches every OpenCode
   worker including ChatGPT-plan models. A proxy that sends `claude` can land on
   the wrong subscription without any error.
2. **No submit-time refusal.** `jobs/feasibility.py` checked only role and
   capabilities, so a family request nothing in the build route could satisfy
   was accepted and then parked or failed later at dispatch. A proxy needs that
   impossibility at submit so it can report it honestly instead of retrying with
   a different family.

## 2. Decision

1. **Three supported family tokens for cross-CLI proxy requests:** `anthropic`
   (the Claude Code worker), `codex` (the Codex worker) and `opencode-go`
   (OpenCode workers on the Go subscription). They are documented in
   `src/saberops/mcp/guide.py`, `docs/mcp.md` and `docs/operations.md`.
2. **`claude` and `opencode` are documented as ambiguous, never as supported
   tokens.** The matcher keeps its current semantics — this is a vocabulary and
   validator change, not a redesign. `claude` remains usable as a general
   family filter for a caller who really means both Claude sources; it is not a
   supported proxy token because it cannot express one subscription.
3. **Feasibility checks a non-empty `model_family_include` at submit** against
   the build-route candidates with the same `matches_model_family` admission
   uses. When no candidate satisfies the include *and* the other capabilities,
   `submit_job` returns the existing `NO_ROUTE_FOR_REQUIREMENTS` failed job
   instead of accepting it. The guidance names the three supported tokens.
   `model_family_exclude` and the matcher stay admission filters/semantics.
4. **A family request never adds a candidate.** The owner's build route must
   already contain that family; feasibility says so and the guidance names
   `orch routing add build`.
5. **The result names the chosen worker.** `get_job`, `wait_job`,
   `submit_job` and `catch_up` return `result.worker` with the dispatched
   attempt's `provider`, `model` and `provider/model` route, so a proxy can say
   "ran on `anthropic/<model>`".
6. **`provider` and `model` stay refused** at the top level and inside
   `requirements` (ADR-0001 invariant 3). No `adapter_include` or provider
   requirement is introduced; that remains an owner decision.

## 3. Consequences

- An impossible include fails visibly at submit with a reason a proxy can
  report, instead of an accepted job that never dispatches.
- The supported vocabulary is testable: a documentation test asserts the three
  supported tokens and that `claude`/`opencode` are named only as ambiguous.
- Admission remains the authority for family exclusion, provider posture and
  every other dispatch decision; this change deliberately leaves it untouched.
- Callers still cannot pin a provider or model, so the one-subscription
  guarantee continues to rest on the family token, not on a new field.
