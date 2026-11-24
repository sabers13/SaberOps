# ADR-0087: A self-healing doctor that repairs only what it owns

- **Status:** Accepted. Implemented in phase 1.1 lane H39.
- **Date:** 2026-11-23
- **Decider:** Saber (owner); phase 1.1 plan H39, recorded 2026-11-23
  ("New phase 1.1 lane now")
- **Related:** [ADR-0020](0020-owner-mode.md) and
  [ADR-0082](0082-owner-mode-carries-authority.md) (owner mode governs
  unattended actions), [ADR-0005](0005-mcp-first-engine-and-phase-2-board.md)
  (the MCP surface and the planning CLI),
  [ADR-0001](0001-saberops-engine-mcp-first.md) (invariants; deterministic
  software decides)

## 1. Context

`orch doctor` was a read-only list of readiness checks shared only with the
CLI. It could say *what* was wrong but not repair the faults SaberOps owns, so
an owner (or a planner over MCP) had to translate a finding into shell steps by
hand. Firstmate's remote doctor had already established the useful shape:

1. check;
2. repair only what is labelled fixable;
3. recheck, and let the recheck alone decide;
4. print exact steps for what needs the human.

Firstmate's known flaw is worth recording: a background service started
without detaching stdin kept an SSH session open and hung the repair. SaberOps
already starts its own scheduler with `stdin=DEVNULL` and
`start_new_session=True`; the doctor must use that exact path, never its own
`subprocess` call.

The owner asked whether a full doctor could make SaberOps self-healing as an MCP
server and work on any PC:

> can we do full doctor for self healing for saberops functioning as mcp and
> would it work on any pc for other users? how firstmate doctor works

and then chose the lane: "New phase 1.1 lane now".

## 2. Decision

1. **Four labels, one meaning each.** Every finding is `ok`, `skip` (does not
   apply on this OS), `fixable` (SaberOps can repair its own state) or `human`
   (needs the owner; the finding carries the exact steps). Severity keeps the
   old `OK`/`WARN`/`FAIL` vocabulary for compatibility, but repair decisions
   read only the label.
2. **Check → repair → recheck.** `run_doctor(fix=True)` records each attempted
   repair, runs every check again, and reports the *recheck*. A repair that
   returns `applied=True` but does not clear its finding stays reported as
   `fixable`. `orch doctor` with no flag stays strictly read-only: no write, no
   migration, no process start, no deletion.
3. **The doctor repairs only SaberOps-owned state.**
   - missing XDG state/config/cache directories;
   - additive database migrations and index rebuilds (a legacy or corrupt
     schema stays `human`);
   - a planning-lease record whose process is provably dead (only after
     checking that it holds no running job);
   - runs whose execution owner is provably dead, through
     `RunSupervisor.reconcile`, never by the doctor writing run rows itself;
   - a scheduler that stopped while jobs are queued, started fully detached
     through `ensure_scheduler_running`;
   - legacy private-development namespaces (preview first; deletion requires an
     explicit `--delete-legacy`).
4. **Never repaired automatically:** vendor CLI logins and API keys, owner
   settings (mode, authority), the user's repositories, and system package
   installs. These are `human` and print exact steps, including a reinstall
   command pinned to the installed SaberOps version.
5. **One report for every surface.** `saberops.doctor.collect_all` composes the
   original read-only checks in `cli.collect_doctor_findings` with the
   repair-labelled checks. The CLI, `GET /doctor` (and its owner-confirmed
   repair form), and the MCP tools `doctor` / `doctor_fix` all render that one
   report; a test pins the equality of `(check_id, state)` across all three.
6. **Owner mode governs the MCP repair.** `doctor` is read-only and needs no
   lease. `doctor_fix` is a mutating tool: `ask-me` records an owner question
   and applies nothing; `critical-only` and `away` apply the safe repairs.
   The web form is an explicit owner click behind the local CSRF and origin
   guard, so it applies directly.
7. **Portable and bounded.** No hard-coded home or app path; XDG resolution goes
   through `saberops.paths`. Linux, macOS and WSL are distinguished (WSL from
   the kernel banner); other systems report `skip`. Every check runs under a
   10-second bound and reports `human` if it hangs. Every repair attempt is
   appended to `$XDG_CACHE_HOME/saberops/doctor-actions.jsonl` and to the MCP
   event ledger.

## 3. Consequences

- A new install reaches a healthy state with `orch doctor --fix`; a broken
  install that needs a human still gets the exact step.
- The repair surface is deliberately small. Adding a repair requires naming the
  SaberOps-owned object it touches and landing a test that proves the recheck
  still reports the fault when the repair does not work.
- The legacy-state deletion is the one destructive repair. It is preview-only
  until the owner passes `--delete-legacy` (or the MCP/web equivalent), matching
  the storage-cleanup rule that nothing is deleted before it is previewed.
- The doctor does not replace preflight, readiness or the work gate; it repairs
  the local substrate those systems run on.
