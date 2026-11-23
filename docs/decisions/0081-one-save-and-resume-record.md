# ADR-0081: One save-and-resume record for interrupted work

- **Status:** Accepted. Implemented in phase 1.1 lane H28.
- **Date:** 2026-11-23
- **Decider:** Saber (owner), phase 1.1 owner answers Q1-Q15 and "maximise the
  deletion" (2026-11-22/23)
- **Related:** [ADR-0013](0013-quota-failover-continuation.md) (quota failover
  continues from its checkpoint), [ADR-0068](0068-one-recovery-tool.md)
  (`resume_job`), [ADR-0011](0011-bounded-automatic-actions.md) (`catch_up`),
  [ADR-0070](0070-three-cli-views.md) (status carries the handoff)

## 1. Context

Work in progress was saved and restored through four different representations:

- the inline interruption/cold-recovery blocks in `orchestrator.py`, which
  committed the attempt's worktree and wrote a `WorkerCheckpoint` row;
- `preemption.py`, which committed/anchored the same kind of checkpoint for a
  parked job, staged a `WorkerCheckpoint` row onto the re-dispatched run and
  recorded a `ResumeFrom` pointer plus a resume capsule;
- `continuation.py`, which restored an owner/continue candidate through a
  separate `config.continuation` reference and a special `lineage_base_sha`
  path, with no checkpoint row at all;
- `handoff.py`, which persisted both a full operator handoff and the bounded
  resume capsule for the same facts.

All three triggers (quota exhausted, provider failed, owner or planner resume
through `resume_job`) preserve work, but each restored it through its own
channel. The phase 1.1 census (H0) measured the area at about 3.2K lines
(`preemption` 1,907, `execution_failover` 682, `continuation` 168,
`handoff` 213, plus inline blocks in `orchestrator.py`).

## 2. Decision

1. **The `WorkerCheckpoint` row is the one saved-work record.** A checkpoint
   commit in the attempt's own worktree, named by one row
   (`saberops.resume.record_checkpoint`), is what every trigger records.
   `saberops.resume.save_interrupted_work` is the one save path: it commits the
   dirty worktree, journals the commit when the caller needs replay safety, and
   records the row. The orchestrator's interruption and cold-recovery blocks
   both call it.
2. **One restore path stages the record onto the successor run.**
   `saberops.resume.stage_checkpoint` writes the row the ordinary checkpoint
   channel already reads, so the next attempt starts exactly at the preserved
   commit. Quota preemption (`preemption.prepare_resume`) and an owner/planner
   continuation (`supervisor.start_supervised_run` through
   `stage_continuation`) both use it. `execution_failover` consumes the same
   row when it starts a new attempt; its reconciliation and binding policy are
   untouched.
3. **`continuation.py` is gone; `saberops.resume` carries its contract.** The
   candidate verification became `verify_resume_source`, unchanged in
   substance (failed run, committed candidate that exists and descends from
   the base, no unresolved effect, no live owner/worker/session, consistent
   durable plan). `prepare_continuation` still freezes the parent's policy and
   `validate_continuation` still refuses a replaced task or policy; the saved
   work itself is restored by the one checkpoint path. The dead
   `continuation_offer` helper (H31 candidate) went with the module.
4. **One handoff artifact.** The bounded resume capsule is the only persisted
   handoff. The full operator handoff is deleted; its evidence stays readable
   from `orch report`, which already carries attempts, checks, reviews, the
   plan and the event log. Legacy `handoff_full` rows remain readable in the
   store but are never written again.
5. **No policy moves.** Quota, pacing, preemption and failover policy, the
   crash-replay decision/intent receipts, the capacity ledger, check
   protection, review evidence, acceptance and pause are unchanged.

## 3. Consequences

- Quota exhaustion, a provider failure and an owner/planner resume now leave
  the same durable record and start the successor at the same verified
  checkpoint; a resumed attempt cannot silently start from the base when work
  was preserved.
- A crash between the checkpoint commit and the row is now replayed from the
  recorded completion even when the worktree is already clean, instead of
  reporting "no changes".
- `orch status` and `orch report --context handoff` show the resume capsule
  instead of two handoff kinds. `orch handoff --capsule` stays accepted for one
  release and prints the same capsule; `--full` no longer selects a second
  artifact.
- The duplicated mechanisms are gone: the two inline orchestrator save blocks,
  the full handoff builder and the separate continuation module. The net source
  change is close to flat because `saberops.resume` also carries the one
  save/stage API (about 90 lines) that replaces the duplicated commit-and-row
  code inside the triggers.
- Drills that pin the behaviour: `tests/test_preemption.py`,
  `tests/test_contract_job_preemption.py`,
  `tests/test_contract_preemption_checkpoint_union.py`,
  `tests/test_checkpoint_ref_safety.py`, `tests/test_f4_quota_failover.py`,
  `tests/test_continue_run.py`, `tests/test_h7_resume_job.py`,
  `tests/test_cli_three_views.py` and `tests/test_h28_one_resume_path.py`.
