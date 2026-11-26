# ADR-0102: An accepted commit carries its job, run and attempt into Git

- **Status:** Accepted. Implemented in phase 1.1 lane H44.
- **Date:** 2026-11-27
- **Decider:** Saber (owner), from Zed's DeltaDB idea, recorded in the plan:
  "Each accepted commit links back to the job that produced it: model, base
  commit, worker transcript, check results and review verdict. Use a git note
  or commit trailer so `git log` can answer 'why does this line exist?' Don't
  use DeltaDB itself." Released for phase 1.1 on 2026-11-26 ("yes do 44 and
  45").
- **Related:** [ADR-0055](0055-review-evidence-guarantee.md) (engine-verified
  candidate-bound review), [ADR-0010](0010-receipt-bound-gate-skip.md) (the
  full-gate receipt that allows the integration gate to be skipped),
  [ADR-0021](0021-worker-evidence-and-project-notes.md) (the on-disk worker
  evidence folder), [ADR-0005](0005-mcp-first-engine-and-phase-2-board.md)
  (phase 2's board shows only facts the engine records), and the phase 1.1
  plan lane H44.

## 1. Context

The store already answers every provenance question: project, objective, run,
attempt, base and candidate SHA (`provenance.py`), provider and model per
attempt plus requested-versus-reported model (`dispatch_evidence`), the worker
transcript (`<state>/transcripts/<run_id>.ansi`), checks, the acceptance gate
receipt in the `run_accepted` payload, and the review bound to the candidate
SHA through its `REVIEW` dispatch. Git answered nothing beyond a commit
message: no trailer, no note, no job id anywhere, and `runs` had no `job_id`
column (the only job-to-run map was the job row's `run_ids_json`).

Two facts shape the design:

1. **The commit is frozen before the gate.** The candidate commit is created
   immediately before the gate runs; acceptance installs *that exact SHA*
   through a ref compare-and-swap and the review is bound to that SHA. Rewording
   it at acceptance would break both, so nothing known only after the gate can
   live in the commit message.
2. **Two commit paths exist.** When the worker leaves its work uncommitted the
   orchestrator makes the candidate commit; when the worker commits its own
   work the orchestrator adopts that HEAD. The engine must not reword a
   worker-made commit (explicitly out of scope), so the trailer is possible
   only on the engine-made path.

## 2. Decision

1. **The job id is on the run row.** `runs.job_id` is written when the job
   dispatches the run (scheduler and native paths), inherited by a refreshed
   run through `replace(source, ...)`, and NULL for a plain `orch run` or a
   legacy row. Additive migration: existing rows keep NULL and the read path
   falls back to the job's own run chain.

2. **The candidate commit carries a trailer block** written only when the
   engine makes the commit, with the facts known then — one key per fact the
   plan names:

   ```
   SaberOps-Job: <job id>          (omitted for a plain `orch run`)
   SaberOps-Run: <run id>
   SaberOps-Attempt: <attempt id>
   SaberOps-Base: <base commit>
   SaberOps-Worker: <provider / worker CLI>
   SaberOps-Model: <requested model>
   ```

   The headline keeps its historical
   `orch(<run>): candidate changes for attempt <n> (<provider>/<model>)` shape.
   `.`-free, single-token keys are deliberate: Git's trailer grammar
   (`Key: value`, last paragraph) accepts them, and one Python parser reads the
   same keys from a commit message and from a note.

3. **Acceptance adds the note, and `AcceptEngine` is its only writer.** Every
   acceptance goes through `AcceptEngine.accept_run` (service, CLI, owner
   action, auto-accept), so one place writes `refs/notes/saberops` on the
   installed SHA with what exists only afterwards: gate result and receipt
   digest, review id, verdict and finding count, transcript path plus whether it
   was present at acceptance, accepted-at and final branch — alongside run, job,
   attempt, base, candidate, worker and model. The note is a commit on the
   notes ref, never a change to the candidate.

4. **The note is best-effort and recorded.** Acceptance has already installed
   the candidate and passed the gate by the time the note is written, so a
   local notes failure must never roll that back. The outcome is recorded in the
   `run_accepted` payload (`provenance_note`: `written`, `ref`, `sha` or the
   error). Nothing is lost: the store holds every fact and the trailer already
   names the job, run and attempt. A repeat accept that finds the durable
   acceptance already recorded reports success without rewriting the note; the
   note is derived evidence, and `orch provenance` says plainly when it is
   absent.

5. **Notes are not pushed by default.** Trailers travel with the commit into
   every clone. Notes are local until the owner deliberately fetches or pushes
   the notes ref:

   ```bash
   git push origin refs/notes/saberops
   git fetch origin refs/notes/saberops:refs/notes/saberops
   ```

   Pushing every acceptance would add a second ref update to a step whose
   authority is the target branch, and notes are derived, rebuildable evidence,
   not authority. Not pushing also keeps a rejected candidate's note out of
   shared history until the owner asks for it.

6. **One read verb resolves it.** `orch provenance <rev>` (or
   `orch provenance path:line`, which blames the line first) reads the trailer
   and the note, then the run, attempt and job rows, and reports the gate and
   review facts, the transcript's *live* state, and whether the stored attempt
   commit is exactly the resolved commit. `--json` emits the same facts for
   scripts; a commit with no SaberOps provenance exits 1 with a plain message.

7. **Retention is stated, not hidden.** Run and attempt rows are never deleted,
   so a trailer or note keeps resolving after the transcript file is gone.
   Storage cleanup deletes accepted-run transcripts 90 days after acceptance;
   the read verb reports `removed` with that reason instead of failing or
   printing a stale path. The note records whether the transcript existed when
   it was written.

8. **No DeltaDB, no dependency, nothing pushed implicitly.** Only Git plumbing
   (`git notes`, `git log`, `git blame`) is used.

## 3. Consequences

- `git log` on a fresh clone of the target answers which job, run, attempt,
  base, worker and model produced an engine-made commit, with no store access.
- A worker-made commit has no trailer (the engine never rewrites it), but the
  acceptance note still links it to the same records; `orch provenance` prefers
  the note's facts and verifies the stored attempt commit matches.
- The board in phase 2 can show commit provenance from Git or the store; the
  store stays the authority and the note is its Git mirror.
- A trailer is a claim in a commit message, so the verb verifies it against the
  attempt row and reports `verified: false` when they disagree instead of
  presenting a worker-written trailer as fact.
- Existing runs and commits predate the trailers; they resolve nothing and the
  verb says so. No backfill is attempted.

## 4. Validation

- `tests/test_h44_commit_provenance.py` (four fake-worker rig drills, no real
  vendor CLI):
  1. a scheduler-dispatched job's engine-made candidate, accepted, then a
     **fresh clone**: the trailer block resolves job/run/attempt/base/worker/
     model with no store and no notes ref; after the documented notes fetch the
     note adds gate and review; a `path:line` blame resolves to the same record;
  2. the transcript file and the note are both removed: the lookup still names
     run, attempt and job and reports the transcript `removed`;
  3. a plain commit is reported as having no SaberOps provenance (exit 1);
  4. a worker that commits its own work resolves through the acceptance note
     alone, with `verified: true`.
- Full gate green on the lane branch.
