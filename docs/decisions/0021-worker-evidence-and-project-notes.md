# ADR-0021: Per-job evidence folders and per-project worker notes

- **Status:** Accepted; implemented in Phase 1 lane E4.
- **Date:** 2026-11-19
- **Decider:** Saber (owner), through the Phase 1 plan's E4 lane
- **Related:** [ADR-0001](0001-saberops-engine-mcp-first.md) §3.3 (MCP-first
  evidence reads), [ADR-0004](0004-mcp-authority-and-evidence.md) (dispatch
  evidence), [ADR-0011](0011-bounded-automatic-actions.md).

## 1. Decision

Every run keeps a **plain per-job folder** beside the store that owns it:

```
<state dir>/jobs/<run_id>/
    request.md               what was asked
    attempt-N.prompt.md      the exact prompt the worker received
    attempt-N.note.md        the exact project note that travelled in it
    attempt-N.result.md      what came back
    attempt-N.json           manifest linking the attempt to dispatch evidence
```

The folder is durable, append-only evidence. A later edit never rewrites an
earlier attempt's prompt or note snapshot.

Each project has one short **project notes** file at
`<project state dir>/project-notes.md`. It is owner-maintained, lives outside
the repository (so it is never part of a reviewed diff), and is handed to
every worker attempt in a `[PROJECT NOTES]` prompt section. `orch project notes
show|set|clear` reads and writes it; `--file -` reads stdin.

## 2. Worker evidence rules

- Each attempt records the project note **actually sent**, plus that snapshot's
  SHA-256 digest, so evidence stays readable after the live note changes.
- The attempt manifest copies the existing dispatch report's
  `requested_provider`/`requested_model` and its provider-reported
  `actual_provider`/`actual_model`, and names the `dispatch_evidence_id`.
- When the provider supplies no model identity, the attempt record says
  **`unreported`**. It never guesses a model, and it never promotes the
  requested model into the reported field. (The run-report projection keeps
  its own `UNKNOWN` vocabulary; both are truthful labels for the same absence.)
- A note is read from the repository's verified project state directory, not
  from whichever database happens to hold the run, so a pinned or global store
  cannot mis-deliver another project's note.
- Evidence is **not authority**. A missing folder or an unwritable state
  directory records a `job_folder_write_failed` event and never changes
  admission, gating, review or acceptance.

## 3. Consequences

- A worker's exact input and the note it received are reconstructible without
  replaying the model, including after the owner edits the note.
- The folder holds no secrets beyond what already reached the worker prompt,
  and it lives under the owner's own state directory, not in the repository.
- Runs remain one folder each; a job that runs again gets its own run folder.
