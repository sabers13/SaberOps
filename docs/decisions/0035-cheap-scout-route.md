# ADR-0035: Scout jobs prefer each subscription's cheapest plan model

- **Status:** Accepted. Implemented in Phase 1 lane F27 (Wave 3); amended by
  Phase 1.1 lane H8 (2026-11-23) so the cheap default is the ordinary
  `effort: low` rule rather than a scout-only route.
- **Date:** 2026-11-20 (amended 2026-11-23)
- **Decider:** Saber (owner), Phase 1 plan lane F27
- **Related:** [ADR-0026](0026-scout-jobs.md) (scout kind),
  [ADR-0024](0024-model-family-tokens.md) (family tokens),
  [ADR-0005](0005-mcp-first-engine-and-phase-2-board.md) (three roles and the
  one build route)

## 1. Context

Scout jobs (F19) are read-only investigations: search, grep, audit and other
questions with no code change.  They suit the cheapest plan model of whichever
subscription runs them, the way a cheap subagent swarm is used beside a strong
building model.  The build route already names every allowed subscription and
model; what was missing was a default that chooses the cheap model of each
subscription instead of the first (usually stronger) route.

The owner approved the cheap default on 2026-11-19 ("yes add it") after seeing a
workflow that builds with one model and hands search and audit to a cheap one.

## 2. Decision

1. **A scout keeps the build route; the default only reorders it.**  No second
   routing document and no new role is introduced.  Among the candidates the
   owner's build route already allows, the cheapest plan model of each
   subscription is preferred: Codex `gpt-*-luna`, Claude Haiku
   (`claude-haiku-*`), and OpenCode Go `deepseek-v4*-flash`.  Matching is by
   family, so a catalog rename (`gpt-5.6-luna` to `gpt-6-luna`) still matches.
2. **Every candidate remains a fallback.**  Admission is unchanged.  A cheap
   model that is unavailable, quota-blocked or not read-only-capable never
   removes the rest of the route: groups containing a cheap candidate are tried
   first and cheap candidates are preferred after admission, but the ordinary
   build route still runs a scout when no cheap model can be admitted.  A scout
   never invents a route outside the owner's build route.
3. **Per-project override.**  A new project setting `scout_route` accepts
   `cheap` (default), `build` (the ordinary build route, no cheap preference),
   or one exact `provider/model` already on the build route.  An explicit
   candidate narrows the scout's view to that candidate.  It is persisted in
   the existing per-project settings document, exposed as
   `orch project set-scout-route` / `clear-scout-route`, and shown by
   `orch project show`.
4. **Per-job effort.**  `effort: low` on any job prefers each subscription's
   cheapest plan model, and a scout with no stated effort is normalised to
   `effort: low` when it is created (H8).  `size_hint: "L"` or
   `effort: high|xhigh` keeps the ordinary build route; that is the job-level
   escape hatch for a large or deep investigation.
5. **Read-only and admission authority are unchanged.**  A scout still adds
   the `read_only` requirement before feasibility, so only providers whose CLI
   enforces read-only can serve it.  Quota admission, the memory budget, the
   planning-session pause and every billing invariant are the existing ones;
   only the order among admitted candidates changes.
6. **Usage stays per job.**  The chosen `provider/model` and per-attempt usage
   are already recorded on the job record (F2), so a cheap scout reports what
   it spent like any other job.

## 3. Consequences

- Search, grep and audit jobs cost the cheapest plan model of the subscription
  that runs them, while the owner's route order still decides which
  subscriptions are allowed.
- A provider whose CLI cannot enforce read-only still makes a scout infeasible
  at submit; the cheap default does not widen the read-only boundary.
- The `scout_route` setting is a selection over the build route, never a second
  source of routes.  Removing a candidate from the build route also removes it
  from the scout default; a saved explicit candidate that is no longer routed
  falls back to the cheap default instead of parking the job forever.
- A project that wants the strong model for every scout sets `build`; a single
  large investigation sets `size_hint: "L"` on that job only.
- A build job that states `effort: low` gets the same cheap preference as a
  scout.  A job that states any other effort (or none, for a build job) keeps
  the owner's ordinary route order.

## 4. Amendment by H8 (2026-11-23): the cheap default is `effort: low`

H8's verification found that the F27 reordering was keyed on the job kind, not
on the requirement the planner already states.  That duplicated `effort: low`:
the same ordering produces the same result from either input.  The amendment
keeps the ordering functions (`scout_candidate_view`,
`order_candidates_for_scout`, the family matcher and `scout_route`'s
narrowing/build override) and changes what turns the cheap preference on:

1. A scout with no stated effort records `effort: low` at job creation and at
   direct-run creation, beside its read-only requirement (`scout_requirements`
   in `jobs/models.py`, `resolve_effective_run_config` in `run_creation.py`).
2. `effort: low` on any job — scheduler dispatch or a direct `orch run` —
   prefers each subscription's cheapest plan model.  A missing effort for a
   build job and every higher effort keep the ordinary build route.
3. The scout-specific kind gates on the scheduler and direct-run paths are
   removed; only the scout `scout_route` setting (project override and
   `build`/explicit selection) still applies to scouts alone.

What the verification showed the ordering still adds beyond `effort: low`, and
so kept: the `scout_route` overrides, the `size_hint: "L"` / high-effort escape
hatch, and the group-level order that keeps a subscription with a cheap model
ahead of one without while every candidate stays a fallback.  Recorded cost
change: a scout that explicitly states `effort: medium` now runs the ordinary
build route (F27 only excluded `high|xhigh`/`L`).  No effort-stating scout ever
spends more than the model the owner's route names, and billing consent,
admission and read-only enforcement are untouched.
