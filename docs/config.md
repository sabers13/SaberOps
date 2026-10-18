# Central configuration and audit

Step 013 (`CFG-1`) centralizes the workflow settings that used to live as
scattered implicit constants, and records every change as an owner-approved
decision in an append-only ledger. The implementation is
`saberops.config`; it is standard-library only and deterministic.

## Workflow settings

`WorkflowSettings` (in `saberops.config`) is the validated central policy:

| Setting | Default | Meaning |
| --- | --- | --- |
| `max_concurrent_tasks` | `3` | Concurrently active tasks (3x5 ceiling) |
| `max_worker_sessions_per_project` | `5` | Worker sessions per project (3x5 ceiling) |
| `gate_heavy_slots` | `1` | Owner-editable heavy gate-slot ceiling |
| `gate_light_slots_max` | `8` | Owner-editable light gate-slot ceiling (`MAX_LIGHT_SLOTS`) |
| `evidence_accepted_retention_days` | `90` | Accepted-run evidence retention |
| `evidence_other_retention_days` | `30` | Other evidence retention |
| `checkpoint_retention_days` | `7` | Checkpoint retention after terminal state |
| `owner_db_vacuum_interval_days` | `7` | Owner DB vacuum cadence |
| `failed_scratch_cap_gib` | `2.0` | Retained failed gate scratch cap |

`WorkflowSettings(...)` validates itself on construction, so an invalid policy
can never be persisted. The rules are fail-closed:

- `1 <= max_concurrent_tasks <= 3` and `1 <= max_worker_sessions_per_project <= 5`
  (the 3x5 ceilings are maximums, never utilization targets);
- `gate_heavy_slots >= 1` and `gate_light_slots_max >= max_concurrent_tasks`
  (the gate-slot budget is validated against the configured worker concurrency);
- retention windows are positive and `evidence_other_retention_days` never
  exceeds `evidence_accepted_retention_days`;
- `failed_scratch_cap_gib >= 0`.

`as_dict()` renders a versioned snapshot; `from_dict()` rejects unknown keys,
non-integer values and out-of-range values; `frozen_hash()` is the sha256 of the
canonical snapshot. The frozen hash is what makes a decision *compatible*: a
snapshot that no longer hashes to the value recorded next to it fails closed.

## Proposal, approval and the audit ledger

A change is a two-step decision:

1. `propose_config_change(base, changes, rationale=..., proposed_by=...)`
   validates the requested change against the current settings and returns a
   `ConfigProposal` (the base is untouched; nothing is persisted).
2. `approve_config_proposal(proposal, ledger=..., approved_by=..., created_at=...)`
   records the owner approval as an immutable `ConfigDecision`.

The ledger is a JSONL file at
`<project state dir>/config-decisions.jsonl`. Each line is a `ConfigDecision`
whose `decision_id` is the content hash of its body and whose `supersedes` links
it to the decision it replaces. The chain for a `kind` is linear:

- a replayed decision (same id) is refused;
- a second root for a `kind` (a fork) is refused;
- superseding a decision that is not the chain tip is refused;
- a line whose body no longer matches its `decision_id` is refused.

`current_workflow_settings(ledger)` returns the tip's settings, verifying the
frozen hash; with no decision it returns the canonical default.

`record_decision(...)` is the generic entry point for other recorded decisions
(for example standing-rule proposals and owner overrides). Git/storage cleanup
policies go through `record_cleanup_decision(...)`, which validates the policy
shape (`kind` in `git`/`storage`, non-negative `ttl_days`, non-empty `subjects`)
before recording it with `kind="cleanup"`.

## Dynamic gate capacity (D-0076)

The gate-slot and worker counts are derived from live memory at run time, not
pinned constants. `saberops.gate_runner` (product pool) and
`scripts/gate_tools.py` (developer gate) both read `/proc/meminfo`
(`MemAvailable`, `MemTotal`) with an `os.sysconf` fallback.

- Safety floor `2.0 GiB` for the OS/UI/shells; light slot `1.0 GiB`; heavy slot
  `3.0 GiB`.
- Light slots: `max(1, min(MAX_LIGHT_SLOTS, (MemAvailable - 2 GiB) // 1 GiB,
  cpu_cores // 2))`, `MAX_LIGHT_SLOTS = 8`.
- Heavy slots: `1`, doubled to `2` only when `MemTotal >= 32 GiB` and
  `MemAvailable >= 8 GiB` and `cpu_cores >= 12`.
- Workers: `max(1, min(cpu_cores, MemAvailable // 0.6 GiB))`.

Explicit knobs (`SABEROPS_GATE_SLOTS`, `SABEROPS_LANE_SLOTS`,
`SABEROPS_GATE_WORKERS`) remain authoritative. Before a heavy gate starts, a
pre-admission memory guard queues while available RAM is below the floor (default
`5 GiB`, override `SABEROPS_GATE_MIN_AVAILABLE_GB`, disable
`SABEROPS_GATE_MEMORY_GUARD=0`), bounded by `SABEROPS_GATE_SLOT_WAIT`. The heavy
gate drains every potential light index `0..MAX_LIGHT_SLOTS`. A `<index>.json`
whose recorded pid is dead is unlinked immediately and never displayed as a
holder. See [gate.md](gate.md#dynamic-ram-capacity-and-self-healing-slots-d-0076).

## Frozen-hash compatibility

A `ConfigDecision` carries both the settings snapshot (`value`) and its
`frozen_hash`. Reconstruction re-derives the settings from the snapshot and
re-checks the hash, so a hand-edited `config-decisions.jsonl` cannot silently
change an accepted configuration; it raises `ConfigError` instead.
