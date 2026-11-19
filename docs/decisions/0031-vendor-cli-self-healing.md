# ADR-0031: Self-healing after a vendor CLI update

- **Status:** Accepted. Implemented in Phase 1 lane F18 (Wave 3).
- **Date:** 2026-11-19
- **Decider:** Saber (owner), via the Phase 1 plan lane F18
- **Related:** [ADR-0018](0018-vendor-cli-update-guard.md) (the no-prompt
  version guard this record acts on), [ADR-0002](0002-routing-admission-and-quota.md)
  (routing and admission), [ADR-0011](0011-bounded-automatic-actions.md)
  (bounded automatic actions), [ADR-0024](0024-model-family-tokens.md) (family
  tokens used to keep a repair off the broken vendor),
  [ADR-0029](0029-no-work-without-a-planning-session.md) (the work gate that
  bounds the smoke prompt)

## 1. Context

SaberOps drives the vendors' official CLIs and pins the flags its adapters
pass. ADR-0018 made a version change visible: the allowlisted `--help` guard
reports `SUPPORTED`, `UNSUPPORTED` or `UNVERIFIED` without sending a prompt.
That is still only evidence for the owner. On 2026-11-19 an OpenCode
2.2.6 -> 2.3.2 update needed a manual verification run and a separate fix lane
before the owner could trust the worker again.

The owner asked (2026-11-19): "can we put a self healing mechanisim so saberops
check after providers cli update and if something changed that effect saberops
it update itself for new cli updates to adopt", and approved the automatic
one-prompt smoke check. The engine must therefore adapt by itself after a
vendor update, and may reinstall its own daily build only after the owner has
accepted the repair.

## 2. Decision

1. **Detect without a prompt.** On every scheduler start (the first tick) and
   periodically (`SABEROPS_VENDOR_CHECK_INTERVAL`, default 600 s), the engine
   compares each installed CLI's `--version` with the version last verified for
   it. The first pass seeds that record from the checked-in ADR-0018 fixture
   manifest. A durable per-vendor record in the owner DB holds
   `installed_version`, `verified_version`, the state, the evidence and the
   repair job id, so the scheduler, the CLI and the MCP server see the same
   verdict.

2. **Contain.** While a vendor is `unverified`, `failed` or `pin_mismatch`, none
   of its providers is admitted for new work: the pacing scheduler skips those
   candidates and the queued job falls through to another admitted route or
   waits with `diagnostic: VENDOR_CLI_UNVERIFIED` naming the vendor. Running
   jobs are never touched. A vendor CLI that is simply not installed does not
   park anything; readiness already covers it. Detection sends exactly one
   notice per new version.

3. **Check the interface, then one smoke prompt.** The check process runs the
   ADR-0018 allowlisted `--help` capture and compares every adapter-pinned flag.
   `UNSUPPORTED` is a failure; `UNVERIFIED` parks the vendor but is never
   treated as a removed flag and never files a repair. When the interface
   passes, the engine sends **one** tiny prompt ("Reply with the single word
   OK.") through the vendor's own worker adapter -- the normal hermetic,
   isolated path -- in a throwaway git repository, and requires a successful
   exit with at least one parsed usage row carrying token counts. Identity is
   recorded but may legitimately be `unreported` (OpenCode 2.3.2). The smoke:
   - is **on** by default and has one owner setting (`orch vendor smoke on|off`);
   - runs **at most once per installed version**, including a failed attempt;
   - is skipped while the ADR-0029 work gate is closed (the vendor stays parked
     and the check is retried later);
   - uses the vendor's least-preferred routed plan model (the owner's route
     order, where the last candidate is the cheapest admitted one), which the
     owner may override with `orch vendor smoke-model`.

   The check runs as a deterministic engine step in a detached
   `orch vendor check` process, not as an F19 scout job: a scout would spend a
   full model turn on evidence the engine collects itself, and the lane's bound
   is one smoke prompt per version. The repair job (which does need model work)
   is an ordinary build job.

4. **Heal when nothing broke.** A passing interface check and smoke record the
   new version as verified, refresh the runtime verification record and resume
   routing, with one notice naming the old and new version. "Refreshing the
   fixtures" here means the durable runtime record, never a rewrite of the
   checked-in repository fixtures: ADR-0018 rule 7 keeps those an explicit,
   token-free `capture` step, and the engine never commits to its own
   repository without acceptance.

5. **Heal when something broke.** A failed check parks the vendor and files
   **exactly one** repair job on the SaberOps repository, keyed by
   `(vendor, version)`, with the version, the missing flags or the smoke
   output, and a `model_family_exclude` for the broken vendor's family so the
   work runs elsewhere. The job carries a durable `self_heal_repair` event
   stating `requires_owner_accept: true`. It goes through the normal gate and
   independent review; the owner accepts it explicitly and nothing
   auto-accepts it, in any owner mode. Until a later check passes, the vendor
   stays parked. The next check re-verifies the interface only (the smoke
   budget for that version is already spent) and resumes routing. The job's
   success alone never resumes the vendor.

6. **Pin instead of adapting (owner setting).** `orch vendor pin <vendor>
   <version>` makes an exact CLI version the requirement. A mismatch parks the
   vendor with a warning notice and never starts the adaptation path (no help
   diff, no smoke, no repair job); `orch vendor unpin` returns to adapting.

7. **Self-update only from an accepted, green commit.** `orch self-update`
   reinstalls the engine from a commit that is (a) already on the protected
   target branch (`--target`, default `main`) -- the owner's acceptance -- and
   (b) covered by a passing `full` gate entry for the exact SHA (`G3`,
   `main-gate` or `manual`) in the shared engine gate ledger. The checkout must
   be at that commit and clean, so the reinstall can only build committed code.
   It never pushes and never moves the target branch. Before the reinstall
   every RUNNING worker is parked at a safe point through the ADR-0029/F17 pause
   path and released afterwards, so no worktree, checkpoint or job is
   discarded; if the workers cannot be parked, nothing is installed. The engine
   records the new revision in a durable restart marker, wakes the scheduler
   (which re-execs into the new build) and lets a running MCP server finish its
   current call and exit so the client reconnects to the new code. It reports
   the old and new `orch --version` and re-runs the vendor checks.

## 3. Consequences

- An owner who updates a vendor CLI sees the engine either resume by itself or
  park that vendor, route around it and hand over one evidence-carrying repair
  job, instead of discovering the breakage in a failed run.
- The automatic path spends at most one tiny plan-limited prompt per CLI
  version, never a repair prompt on the broken vendor itself, and nothing at all
  while SaberOps is paused.
- The engine can adopt its own fixes, but only after an owner acceptance and a
  full-gate receipt for the exact commit; an unaccepted or red commit is
  refused before anything is reinstalled or paused.
- Detection still never decides on a guess: unreadable help output parks the
  vendor for a retry and files no repair.
- Repair jobs are visible like any other job, with the same gate, review and
  acceptance rules; nothing about them widens billing consent, acceptance,
  review or isolation.
- The first scheduler start after this change seeds the manifest versions, so a
  store that already drifted (for example OpenCode 2.2.6 -> 2.3.2) parks that
  vendor on the first pass and runs the check immediately.
