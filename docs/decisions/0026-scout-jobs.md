# ADR-0026: Scout jobs are a read-only kind on the build route

- **Status:** Accepted. Implemented in Phase 1 lane F19 (Wave 3).
- **Date:** 2026-11-19
- **Decider:** Saber (owner), Phase 1 plan lane F19
- **Related:** [ADR-0005](0005-mcp-first-engine-and-phase-2-board.md) (three roles),
  [ADR-0014](0014-job-dependencies.md) (job dependencies),
  [ADR-0008](0008-candidate-bound-acceptance.md) (candidate-bound acceptance),
  [ADR-0021](0021-worker-evidence-and-project-notes.md) (per-job evidence folders)

## 1. Context

The owner asked whether SaberOps should add a scout worker role, and after the
recommendation to add scouts as a job type rather than a fourth role, said yes.
A planner today can only submit build jobs: a knowledge-only question still
pays for a gate, an independent review and an owner acceptance, and produces no
kept answer.  Jobs already carried a `read_only` requirement, but it only
filtered routing; nothing enforced it at execution and nothing produced,
stored or returned a report.

Five knowledge-only scouts changed what this plan built or caught defects
during planning (a wrong hybrid premise, a refresh-link bug, an unenforced
Gemini rule, a stored-key exposure).  Losing those answers in chat transcripts
is the cost this record removes.

## 2. Decision

1. **A kind, not a role.** Roles stay PLAN / BUILD / CHECK. `Job.kind` is
   `BUILD` (default) or `SCOUT`; a scout is a BUILD job with a different
   deliverable. It uses the build route, the same billing posture, quota
   admission, memory budget and pause rules as any other job.
2. **Read-only enforced, not requested.** Submitting a scout adds the
   `read_only` capability to the job's requirements before feasibility, so a
   route without enforced read-only support is refused at submit. At execution
   the frozen `RunConfig.kind` makes the adapter run with its own read-only
   rules (Codex `--sandbox read-only` today). Any diff the worker produces
   against the base is recorded as a `scout_write_violation` and discarded: the
   scratch copy is reset and cleaned, and no commit is created.
3. **The report is the deliverable.** The worker answers with Markdown; the
   engine writes `jobs/<run_id>/scout-report.md` in the E4 per-job folder and
   indexes `report_path`, summary, findings, decisions, violations and digest
   as a `scout_report` job event. A scout completes when a non-empty report
   exists; an empty answer fails the job.
4. **No gate, no review, no acceptance by default.** A report is not a
   candidate, so there is nothing to gate, review or accept. The run completes
   without the candidate-publication and acceptance spine. A report review for
   high-stakes questions stays optional follow-up work, not a default.
5. **Decisions never stay in prose.** Each bullet in the report's
   `## Owner decisions` section becomes one keyed, owner-only question
   (`OTHER`, which fails closed to the owner). Questions are idempotent for the
   same report.
6. **Report retrieval and promotion.** `get_job`, `wait_job`, `catch_up` and the
   completion notice show the report path, a short summary and the findings.
   `promote_scout` (MCP tool and `orch promote-scout`) starts an ordinary build
   job whose goal carries the bounded report text as starting context and
   records the lineage on both jobs. Promotion is explicit; nothing is
   auto-promoted.
7. **Dependencies.** A succeeded scout satisfies a dependant directly: there is
   no candidate to accept. The report is available to the dependant through the
   job record.

## 3. Consequences

- Knowledge-only work is cheap: no gate, review or acceptance cost, and the
  answer survives the planning session that asked for it.
- A scout can never land a change. A provider whose CLI cannot enforce
  read-only makes scouts infeasible at submit with an actionable reason rather
  than silently running a writable worker.
- The report is captured from the worker's final answer rather than written by
  the worker, because a read-only CLI sandbox cannot allow one writable path.
  The engine is the only writer, and only inside the job's own folder.
- Plan lanes may set `kind: "scout"` (ADR-0015 format). A scout lane's report
  is attached to the goal of the lanes that depend on it, and its path and
  digest are recorded on the dependant's submission event. The automatic
  CLI-update check is a deterministic engine step rather than a scout job
  ([ADR-0031](0031-vendor-cli-self-healing.md) decision 3); this record does
  not claim it.
- Stores created before this change gain the `jobs.kind` column in place on the
  next writable open; existing rows are `BUILD`.
