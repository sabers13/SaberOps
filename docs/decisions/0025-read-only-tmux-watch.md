# ADR-0025: Read-only tmux watch view

- **Status:** Accepted
- **Date:** 2026-11-19
- **Decider:** Saber (owner)
- **Related:** [ADR-0001](0001-saberops-engine-mcp-first.md) (tmux handoff cut),
  [ADR-0011](0011-bounded-automatic-actions.md) (read-only evidence surface)

## 1. Decision in one paragraph

`orch watch [--repo] [--job ID] [--session NAME]` opens (or attaches to) a
SaberOps-owned tmux session with one window per RUNNING job and one summary
window. Each job window runs the read-only live tail (`tail_job`'s event log
and terminal transcript) plus a status line with route, subscription, attempt
and elapsed time. Windows are created when a job starts, renamed with its
final state when it finishes, and never closed automatically. The summary
window reconciles windows on every tick and lists every job with the owner
pause state. Nothing in the view writes to the engine or reaches a worker: the
store is opened read-only, no window carries worker input, and steering stays
in the F13 note channel (`orch job note`).

## 2. Context

Workers are headless (`claude -p`, `codex exec`, `opencode run`), so there is
no worker pane to join; ADR-0001 cut the terminal/tmux handoff for that reason.
The owner still asked (2026-11-19) for a tmux way to watch work, then accepted
a read-only view rather than an interactive one.

## 3. Details

- **Session ownership.** The session carries the tmux user option
  `@saberops_watch=1`. A default session name from another source is never
  reused: the watcher picks a free suffixed name. An explicitly requested
  name that exists without the marker is refused, so no other tmux session is
  attached to, renamed, killed or sent keys.
- **Window identity.** A window carries `@saberops_job=<job id>`; the name is
  the job id suffix plus a goal slug (the closest thing to a lane label today;
  when plan lanes expose a lane id on the job, it takes the slug's place).
  Finished jobs keep their window with an `-ok`, `-failed`, `-cancelled` or
  `-queued` suffix.
- **Observation only.** `saberops.watch` exposes no `send-keys`, `pipe-pane`
  or `respawn` operation. Every loop reopens the store with `read_only=True`.
- **Fallback.** Without a usable tmux binary the command prints the summary
  and hands one selected running job's current run to `orch monitor`.
- **Pause state.** The summary reads the owner pause record when one exists
  (`Database.get_owner_pause()`), and says "not paused (no pause record)" while
  F17 has not landed. F17's implementation supplies the record; the summary
  does not infer pause from transient process state.

## 4. Consequences

ADR-0001's cut stands: this is not a terminal handoff, not an interactive
worker pane, and cannot start, stop or steer work. It adds no authority and no
new durable state. The cost is a small, bounded tmux dependency on the
observation path only; every engine operation still works without tmux.
