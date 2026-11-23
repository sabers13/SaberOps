# ADR-0082: The owner mode carries the authority presets

- **Status:** Accepted. Implemented by Phase 1.1 lane H22.
- **Date:** 2026-11-23
- **Decider:** Saber (owner), Phase 1.1 plan, recorded answers 2026-11-22 and
  2026-11-23
- **Related:** [ADR-0008](0008-candidate-bound-acceptance.md) (auto-accept),
  [ADR-0020](0020-owner-mode.md) (the mode narrows authority),
  [ADR-0027](0027-critical-only-questions.md) (question delegation),
  [ADR-0029](0029-no-work-without-a-planning-session.md) (the work gate),
  [ADR-0047](0047-expired-away-mode-narrows-to-ask-me.md) (expiry)

## 1. Context

SaberOps had two owner knobs over the same question. The **owner mode**
(`ask-me`, `critical-only`, `away`) decided presence, and five **authority
presets** decided which autonomous actions were allowed; the effective
permission was their intersection. Of the five presets only three had distinct
live behaviour, and two dimensions (publication) no mode granted by default.

The owner asked for a simpler core and maximum deletion:

> do your suggestion and maximise the deletion i feel like there are a lot of
> spam code is in repo

The plan then made the fold a lane: *replace the five authority presets and the
"preset ∩ mode" rule with the three modes plus the per-project settings that
remain; nothing any mode permits today may widen.*

## 2. Decision

1. **One persisted owner document.** `mode.json` (schema 2) holds the mode,
   `until`/`max_jobs`/`set_at`/`set_by`, the technical `host_access` setting,
   and an optional stored permission grant. The `authority.json` document and
   its reader are removed; `orch authority show|list|set` is removed and
   `set-host-access` moves to `orch mode set-host-access`.
2. **Each mode has a fixed permission ceiling.** `ask-me` keeps the attended
   start/gate/review path and no autonomous recovery or acceptance;
   `critical-only` and `away` converge on a gated, independently reviewed
   candidate and accept it as a local commit. No mode ever publishes.
   Effective permission is `stored grant ∩ mode ceiling`, so a document can
   only narrow the ceiling. An expired `away` still narrows to `ask-me`.
3. **The stored grant exists only to keep an upgrade honest.** `set_owner_mode`
   writes the selected mode's ceiling; a document migrated from the presets
   writes the *exact effective permission* the old reader returned. A fresh
   store therefore gets the mode's product behaviour, and an upgraded store
   gets exactly what it had until the owner switches mode on purpose.
4. **Migration, once, with a notice.** The first read after upgrade reads
   `mode.json` (v1) and `authority.json` (v1), computes the old effective
   permission with a frozen copy of the five preset rows and the old `ask-me`
   clearing rule, writes schema 2, and renames `authority.json` to
   `authority.json.migrated`. One notice (`notice` in `mode_view`, printed by
   `orch mode show`) names the fold and says nothing widened. A corrupt legacy
   document still fails closed; it is never guessed at.
5. **No widening, proven in three ways.**
   - For every pre-1.1 `preset × mode` pair (15), the migrated grant is equal
     or narrower, field by field (`tests/test_owner_mode.py`). The expected
     table was frozen against the old reader before it was deleted.
   - Every mode ceiling is at or below the widest permission that mode could
     carry under the old presets.
   - The old `test_*` done-whens (ask-me parks recovery and acceptance,
     away/critical-only converge and accept, expired away is ask-me, an
     explicit submission still dispatches) are re-run on the new reader.
6. **Preset mapping.** `ask_before_actions` becomes a narrowed `ask-me` grant
   (a migrated store keeps dispatch parked); `supervised` is the `ask-me`
   ceiling; `autonomous` and `full_autonomy` are represented by the
   `critical-only`/`away` ceilings; `full_autonomy_publish`'s `auto_publish`
   grant is dropped, because no mode may publish. Candidate-branch publication
   stays available as the explicit per-run `--publish-candidate` flag.

## 3. Consequences

- One knob, one document, one reader. An operator reads `orch mode show` and
  sees the mode, the host access and every decision; there is no second
  setting to reconcile.
- Fresh `away` and `critical-only` stores carry the acceptance/repair ceiling
  the owner recorded in ADR-0008 ("do it in afk and Critical-only mode"). A
  fresh `ask-me` store behaves as the old `supervised + ask-me` default.
- A migrated store can be narrower than its mode: `ask_before_actions` keeps
  dispatch parked even in `away`, and a store saved without an acceptance
  grant never auto-accepts. Switching mode explicitly applies that mode's
  ceiling; that is an owner action, not a migration.
- Publication is never a mode grant. A migrated `autonomous`/
  `full_autonomy_publish` store loses its stored candidate-publication grant;
  the per-run flag remains. This is the one deliberate narrowing.
- The three modes remain the only place new unattended permission can be
  added, so a future lane changes one table instead of a preset × mode matrix.
- Removed: `authority.py` (presets, `AuthorityMode`, `AuthoritySettings`,
  `AuthorityConfigError`, the intersection rule), the `authority.json` reader,
  `orch authority show|list|set`, the web authority view and its unused effects
  map. Tests of the deleted reader were deleted with it; every safety drill was
  rewritten against the new reader, not dropped.

## 4. Alternatives rejected

- **Three mode defaults with no stored grant.** Simplest code, but a store
  saved with `ask_before_actions` (or `full_autonomy + ask-me`) would gain
  permissions after upgrade. Rejected: it widens.
- **Keep the preset as a per-store ceiling.** That is the old intersection rule
  under a new name: two knobs, the same code.
- **Store the preset name.** Re-creates the vocabulary the lane deletes.
