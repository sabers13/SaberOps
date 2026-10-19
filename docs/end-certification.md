# END Production Certification (Step 028)

How to certify SaberOps for self-dogfooding under ADR-0004 §8 Q4.

## Q4 qualification criteria and evidence standard

| ID | Rule | Evidence |
| --- | --- | --- |
| Q4-A | ≥ 3 green waves on distinct independent/fixture repositories | `WaveRecord` per wave: wave id, repo, green flag |
| Q4-B | ≥ 1 real G3 failure correctly routed to repair/surgeon with durable attribution | wave with `g3_failure_routed_with_attribution=true` (a genuine G3 failure, never a drill) |
| Q4-C | Zero INFRA failures permitted | summed `infra_failures` must be 0 |
| Q4-D | Zero owner-only decisions taken by an autonomous model without explicit owner confirmation | summed `owner_only_autonomous_decisions` must be 0 |

`e_verdict` is CERTIFIED only when every check (Q4 + closure + all 9
scenarios) passes. Any single failure yields NOT_CERTIFIED.

## The 9 resilience scenarios

1. **Mid-turn kill** — TurnJournal replay applies each effect at most once;
   killing the Orchestrator mid-turn repeats nothing.
2. **Context overflow** — crossing 50% of the model window latches a trigger
   and re-issues the decision point with a handoff capsule.
3. **Second session** — a concurrent Orchestrator lock acquisition forces the
   second session into read-only posture.
4. **Dead worker relaunch** — a dead worker restarts inside its own existing
   worktree; no second worktree is allocated.
5. **Gate protection** — any candidate diff touching `scripts/`,
   `conftest.py`, or the gate runner is blocked fail-closed.
6. **Standing decisions scope** — an overnight run with standing decisions
   parks only dependent waves; independent waves continue.
7. **Morning report** — the report lists every Orchestrator-alone decision
   with reject rationale and evidence.
8. **Stall alarm** — an unattended heartbeat/progress timeout raises a loud
   `StallAlarm`.
9. **Mid-wave idea intake** — submitting an idea via RC-1 while a wave runs
   leaves the running wave's plan and lanes byte-identical.

## Safe wave closure and bounded storage cleanup

- Worktrees: `git worktree remove <exact-path>`; never `--force`, never a
  repo-wide prune.
- Branches: `git branch -d` after verification; never `-D`.
- Owner branches, unmerged/recovery refs, and remote refs are never deleted.
- Every deletion target must resolve inside an owned root.
- The durable audit trail and recovery checkpoints survive cleanup.

## Running `orch certify`

```bash
# Read-only checklist + Q4 evaluation (exit 1 while evidence is missing)
orch certify check --json
orch certify check --evidence evidence.json

# Execute the suite and scenario verifications
orch certify run --json
orch certify run --scenario stall-alarm --evidence evidence.json

# Markdown report (stdout or file)
orch certify report --out cert-report.md --evidence evidence.json

# Owner-only grant (requires CERTIFIED verdict + explicit token phrase)
SABEROPS_OWNER_TOKEN="... APPROVE SELF-DOGFOODING ..." \
  orch certify approve --evidence evidence.json --record approval.jsonl
```

Evidence file schema:

```json
{
  "waves": [
    {"wave_id": "w1", "repo": "fixture-a", "green": true,
     "infra_failures": 0, "g3_failure_routed_with_attribution": true,
     "owner_only_autonomous_decisions": 0, "evidence": "gate-log ref"}
  ],
  "cleanup": {
    "candidates": [{"reference": "wt-1", "kind": "worktree"}],
    "audit_trail_preserved": true,
    "recovery_checkpoints_preserved": true
  },
  "scenarios": {
    "s1_effects": ["e1", "e2"],
    "s2_usage_ratio": 0.62, "s2_latch": true,
    "s2_reissued": true, "s2_handoff": true,
    "s3_second_mode": "read-only",
    "s4_allocated": 1, "s4_reused": true,
    "s5_changed": ["src/saberops/cli.py"], "s5_blocked": false,
    "s6_dependent_parked": true, "s6_independent_continued": true,
    "s7_decisions": [{"decision_id": "d1", "reject_rationale": "r",
                      "evidence_ref": "e"}],
    "s7_reported": ["d1"],
    "s8_heartbeat_age_s": 400, "s8_progress_age_s": 10,
    "s8_timeout_s": 300, "s8_alarm": true,
    "s9_plan_before": "p", "s9_plan_after": "p",
    "s9_lanes_before": ["a"], "s9_lanes_after": ["a"]
  }
}
```

Approval never persists the token — only its SHA-256 digest — and appends
one JSON line per grant to the record file for an immutable audit history.
