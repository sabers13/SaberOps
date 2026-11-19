"""The rendered plan state (``STATE.md``) for Phase 1 lane P3.

What this module is
-------------------
One pure projection of an activated plan and one Markdown renderer.  The same
projection serves three callers so they can never disagree:

* the canonical state file ``<owner state dir>/plans/<project>/<plan>/state.md``
  that the scheduler tick renders, and the git-ignored ``STATE.md`` symlink the
  owner reads in the checkout;
* ``orch plan-file status``;
* the read-only MCP ``plan_status`` tool and the plan form of ``catch_up``.

Every value comes from durable records: the immutable plan revision, the
``plans``/``plan_lanes`` rows, jobs, runs, attempts, checks, reviews and the
``run_accepted`` events.  Nothing here invents progress, and nothing here acts
on the view.  Acceptance, dispatch and repair stay exactly where they are.

Placement matters (ADR-0015).  The canonical file lives outside the repository
so a render never moves the target ``HEAD``; the in-repo view is a symlink
listed in ``.git/info/exclude`` so ``git status --porcelain`` stays empty and
acceptance keeps working.  The link is created only by :func:`adopt_state_file`
and only in the canonical checkout, never in a worker worktree.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from saberops.git import GitManager
from saberops.jobs import store
from saberops.jobs.dependencies import run_is_accepted
from saberops.jobs.models import Job, JobState
from saberops.models import (
    Attempt,
    AttemptStatus,
    CheckResult,
    ReviewResult,
    Run,
    RunStatus,
)
from saberops.paths import owner_state_dir
from saberops.plan_runner import (
    LaneView,
    PlanRecord,
    PlanRunnerError,
    get_plan,
    list_active_plans,
    plan_lane_rows,
    project_plan,
)
from saberops.project import ProjectRegistry, canonical_repo_root
from saberops.project_scope.plan_store import PlanRevision, PlanTaskDef

__all__ = [
    "LaneState",
    "PlanState",
    "PlanStateError",
    "adopt_state_file",
    "find_plan",
    "plan_state_path",
    "project_plan_state",
    "render_active_plan_states",
    "render_plan_state",
    "render_plan_state_markdown",
    "write_plan_state",
]

STATE_FILE_NAME = "STATE.md"

_DIGEST_RE = re.compile(
    r"<!--\s*saberops-state-digest:\s*([0-9a-f]{64})\s*-->"
)

#: Lane states that mean "a candidate exists and the owner has not accepted it".
_CANDIDATE_STATES = frozenset({"candidate", "stale"})

#: Lane states shown under ``Held``: work the owner or a dependency must move.
_HELD_STATES = frozenset(
    {"held", "blocked", "owner", "failed", "cancelled", "superseded"}
)


class PlanStateError(ValueError):
    """Refusal or failure while rendering or adopting the plan state."""


@dataclass(frozen=True)
class LaneState:
    """One lane's record-derived row in the progress ledger."""

    lane_id: str
    title: str
    wave: str
    owner: bool
    state: str
    status: str
    needs: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    job_id: str | None = None
    waiting_reason: str | None = None
    failure_reason: str | None = None
    worker: str | None = None
    model: str | None = None
    branch: str | None = None
    commit: str | None = None
    run_id: str | None = None
    check_id: str | None = None
    review_id: str | None = None
    owner_evidence: str | None = None

    @property
    def evidence(self) -> tuple[str, ...]:
        """Stable identifiers only: run, check, review and the run page."""
        parts: list[str] = []
        if self.run_id:
            parts.append(f"run:{self.run_id}")
        if self.check_id:
            parts.append(f"check:{self.check_id}")
        if self.review_id:
            parts.append(f"review:{self.review_id}")
        if self.run_id:
            parts.append(f"/runs/{self.run_id}")
        if self.owner_evidence:
            parts.append(f"owner:{self.owner_evidence}")
        return tuple(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.lane_id,
            "title": self.title,
            "wave": self.wave,
            "owner": self.owner,
            "state": self.state,
            "status": self.status,
            "needs": list(self.needs),
            "reasons": list(self.reasons),
            "job_id": self.job_id,
            "waiting_reason": self.waiting_reason,
            "failure_reason": self.failure_reason,
            "worker": self.worker,
            "model": self.model,
            "branch": self.branch,
            "commit": self.commit,
            "run_id": self.run_id,
            "check_id": self.check_id,
            "review_id": self.review_id,
            "owner_evidence": self.owner_evidence,
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True)
class PlanState:
    """The whole ledger: plan identity, lanes and the owner-facing summary."""

    plan_id: str
    project_id: str
    repo: str
    base_ref: str
    target_head: str | None
    revision: str
    sha256: str
    auto_advance: bool
    auto_refresh: bool
    generated_at: str
    digest: str
    lanes: tuple[LaneState, ...]
    ready: tuple[str, ...]
    held: tuple[str, ...]
    landing: tuple[str, ...]
    next_action: str

    def lane(self, lane_id: str) -> LaneState | None:
        for lane in self.lanes:
            if lane.lane_id == lane_id:
                return lane
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "project_id": self.project_id,
            "repo": self.repo,
            "base_ref": self.base_ref,
            "target_head": self.target_head,
            "revision": self.revision,
            "sha256": self.sha256,
            "auto_advance": self.auto_advance,
            "auto_refresh": self.auto_refresh,
            "generated_at": self.generated_at,
            "digest": self.digest,
            "lanes": [lane.to_dict() for lane in self.lanes],
            "ready": list(self.ready),
            "held": list(self.held),
            "landing": list(self.landing),
            "next_action": self.next_action,
        }


# ---------------------------------------------------------------------------
# Plan lookup (read-only safe)
# ---------------------------------------------------------------------------


def find_plan(
    db: Any, *, plan_id: str | None = None, repo: str | None = None
) -> PlanRecord | None:
    """Resolve one plan by id, or by a repository's active plan.

    A read-only store that predates the plan tables returns ``None`` instead of
    attempting DDL, so a read-only client never writes.
    """
    if plan_id is not None:
        return _get_plan_readonly(db, plan_id)
    if repo is None:
        raise PlanStateError("pass plan_id or repo")
    root = canonical_repo_root(repo)
    if root is None:
        raise PlanStateError(f"{repo} is not a git repository")
    project_id = _project_id_for(root)
    from saberops.plan_runner import active_plan_for_project

    try:
        return active_plan_for_project(db, project_id)
    except Exception as exc:  # sqlite3.OperationalError on a pre-plan store
        if _is_missing_plan_store(exc):
            return None
        raise


def _get_plan_readonly(db: Any, plan_id: str) -> PlanRecord | None:
    try:
        return get_plan(db, plan_id)
    except Exception as exc:
        if _is_missing_plan_store(exc):
            return None
        raise


def _is_missing_plan_store(exc: Exception) -> bool:
    text = str(exc).lower()
    return ("no such table" in text and "plan" in text) or "readonly" in text


def _project_id_for(root: Path) -> str:
    """Resolve the plan-store project id without requiring registration."""
    try:
        for project in ProjectRegistry().list_projects():
            try:
                if Path(project.repository_root).resolve() == root.resolve():
                    return project.project_id
            except OSError:
                continue
    except Exception:  # pragma: no cover - an unreadable registry falls back
        pass
    from saberops.project import compute_project_identity

    return compute_project_identity(root).project_id


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------


def _wave_sort_key(wave: str) -> tuple[int, int, str]:
    text = (wave or "").strip()
    if text.isdigit():
        return (0, int(text), "")
    return (1, 0, text.lower())


def _target_head(plan: PlanRecord) -> str | None:
    """Current target commit, or ``None`` when the checkout is unreadable."""
    try:
        root = canonical_repo_root(plan.repo)
        if root is None:
            return None
        return GitManager.get_head_commit(root)
    except Exception:
        return None


def _run_database(db: Any, run_id: str) -> Any:
    """The store that owns ``run_id``: the caller's, else its project store.

    Jobs live in the owner database while run state may live in the owning
    project's store.  Resolution follows the run id's own project segment and
    fails back to the caller's store when the run is unknown, so a read-only
    client never writes and a missing project store never hides a job row.
    """
    try:
        if db.get_run(run_id) is not None:
            return db
    except Exception:  # pragma: no cover - unreadable run row falls back
        return db
    try:
        from saberops.db import Database
        from saberops.runtime import db_path_for_run

        path = db_path_for_run(run_id)
        if path is None:
            return db
        caller_path = getattr(db, "db_path", None)
        if caller_path is not None and Path(caller_path).resolve() == Path(path).resolve():
            return db
        read_only = bool(getattr(db, "_read_only", False))
        owner = getattr(db, "owner_db", db)
        return Database(path, owner_db=owner, read_only=read_only)
    except Exception:
        return db


def _final_attempt(db: Any, run: Run | None) -> Attempt | None:
    if run is None:
        return None
    attempts = cast(list[Attempt], db.get_attempts_for_run(run.id))
    if not attempts:
        return None
    if run.final_attempt_id:
        for attempt in attempts:
            if attempt.id == run.final_attempt_id:
                return attempt
    successful = [
        attempt
        for attempt in attempts
        if attempt.status is AttemptStatus.SUCCESS and attempt.commit_sha
    ]
    if successful:
        return successful[-1]
    return attempts[-1]


def _lane_run(db: Any, job: Job | None, accepted: bool) -> tuple[Run | None, Any]:
    """Follow the job's run chain, including refreshed runs linked to it."""
    if job is None or not job.run_ids:
        return None, db
    found: list[tuple[Run, Any]] = []
    for run_id in job.run_ids:
        run_db = _run_database(db, run_id)
        run = cast(Run | None, run_db.get_run(run_id))
        if run is not None:
            found.append((run, run_db))
    completed = [item for item in found if item[0].status is RunStatus.COMPLETED]
    if accepted:
        accepted_runs = [item for item in completed if run_is_accepted(db, item[0].id)]
        if accepted_runs:
            return accepted_runs[-1]
    if completed:
        return completed[-1]
    if found:
        return found[-1]
    return None, db


def _last_check_id(db: Any, attempt: Attempt | None, run: Run | None) -> str | None:
    if attempt is not None:
        checks = cast(list[CheckResult], db.get_checks_for_attempt(attempt.id))
        if checks:
            return checks[-1].id
    if run is not None:
        checks = cast(list[CheckResult], db.get_checks_for_run(run.id))
        if checks:
            return checks[-1].id
    return None


def _last_review(db: Any, run: Run | None) -> ReviewResult | None:
    if run is None:
        return None
    reviews = cast(list[ReviewResult], db.get_reviews_for_run(run.id))
    return reviews[-1] if reviews else None


def _job_detail(job: Job | None, state: str) -> tuple[str | None, str | None]:
    """Return ``(waiting_reason, failure_reason)`` for the lane row."""
    if job is None:
        return None, None
    waiting = job.waiting_reason.value if job.waiting_reason is not None else None
    failure = job.failure_reason if job.state is JobState.FAILED else None
    return waiting, failure


def _refine_state(db: Any, view: LaneView, job: Job | None, target_head: str | None) -> str:
    """Extend the frontier state with the owner-facing status vocabulary."""
    state = view.state
    if state == "running" and job is not None:
        try:
            from saberops.jobs.service import job_status_view

            if job_status_view(db, job).get("activity") == "needs_attention":
                return "needs-attention"
        except Exception:  # pragma: no cover - activity is advisory, never authoritative
            return state
    if state == "candidate" and target_head:
        run, _ = _lane_run(db, job, False)
        if run is not None and run.base_commit != target_head:
            return "stale"
    return state


def _lane_state(
    db: Any,
    task: PlanTaskDef,
    view: LaneView,
    *,
    target_head: str | None,
    owner_evidence: str | None = None,
) -> LaneState:
    job = store.get_job(db, view.job_id) if view.job_id else None
    state = _refine_state(db, view, job, target_head)
    waiting, failure = _job_detail(job, state)
    accepted = state == "accepted"
    run, run_db = _lane_run(db, job, accepted)
    attempt = _final_attempt(run_db, run)
    check_id = _last_check_id(run_db, attempt, run)
    review = _last_review(run_db, run)
    return LaneState(
        lane_id=view.lane_id,
        title=view.title or task.title,
        wave=view.wave or task.wave or task.phase,
        owner=view.owner,
        state=state,
        status=view.status,
        needs=tuple(task.start_dependencies),
        reasons=tuple(view.reasons),
        job_id=view.job_id,
        waiting_reason=waiting,
        failure_reason=failure,
        worker=attempt.provider if attempt is not None else None,
        model=attempt.model if attempt is not None else None,
        branch=attempt.branch_name if attempt is not None else None,
        commit=attempt.commit_sha if attempt is not None else None,
        run_id=run.id if run is not None else None,
        check_id=check_id,
        review_id=review.id if review is not None else None,
        owner_evidence=owner_evidence,
    )


def _next_action(lanes: tuple[LaneState, ...], plan: PlanRecord) -> str:
    """One deterministic owner-facing sentence, derived only from the rows."""
    if plan.status != "ACTIVE":
        return f"Plan {plan.plan_id} is {plan.status.lower()}; nothing is dispatched."

    ordered = _in_landing_order(list(lanes))

    def first(*states: str) -> LaneState | None:
        return next((lane for lane in ordered if lane.state in states), None)

    lane = first("stale")
    if lane is not None:
        command = f"orch refresh {lane.run_id}" if lane.run_id else "orch refresh RUN_ID"
        return (
            f"Refresh lane {lane.lane_id}'s stale candidate, then accept it: `{command}`."
        )

    lane = first("candidate")
    if lane is not None:
        commit = f" ({lane.commit[:12]})" if lane.commit else ""
        run_id = lane.run_id or "RUN_ID"
        return f"Accept lane {lane.lane_id}'s candidate{commit}: `orch accept {run_id}`."

    lane = first("owner")
    if lane is not None:
        return (
            f"Complete owner lane {lane.lane_id} with `orch plan-file done "
            f"{lane.lane_id} --plan {plan.plan_id} --evidence <text>`."
        )

    lane = first("failed")
    if lane is not None:
        return (
            f"Replace or remove failed lane {lane.lane_id} "
            f"({lane.failure_reason or 'job failed'}); its dependants stay held."
        )

    lane = next(
        (
            item
            for item in ordered
            if item.state in {"blocked", "held"}
            and "dependency_blocked" in item.reasons
        ),
        None,
    )
    if lane is not None:
        return f"Lane {lane.lane_id} is blocked by a failed dependency; replace the dependency."

    moving = [
        item
        for item in ordered
        if item.state in {"running", "needs-attention", "queued", "ready"}
    ]
    if moving:
        names = ", ".join(item.lane_id for item in moving)
        return f"No owner action; lanes {names} are running or queued."

    return "Nothing to accept; every lane is accepted, done or removed."


def project_plan_state(
    db: Any,
    plan: PlanRecord,
    *,
    now: datetime | None = None,
    target_head: str | None = None,
) -> PlanState:
    """Build the ledger from durable records only (no writes)."""
    projection = project_plan(db, plan)
    revision: PlanRevision = projection.revision
    head = _target_head(plan) if target_head is None else target_head
    rows = plan_lane_rows(db, plan.plan_id)
    lanes: list[LaneState] = []
    for task in revision.tasks:
        view = projection.view(task.task_id)
        if view is None:  # pragma: no cover - every task has a view
            continue
        row = rows.get(task.task_id)
        lanes.append(
            _lane_state(
                db,
                task,
                view,
                target_head=head,
                owner_evidence=row.owner_evidence if row is not None else None,
            )
        )

    ready = tuple(lane.lane_id for lane in lanes if lane.state == "ready")
    held = tuple(lane.lane_id for lane in lanes if lane.state in _HELD_STATES)
    landing = tuple(
        lane.lane_id for lane in _in_landing_order(lanes) if lane.state in _CANDIDATE_STATES
    )
    generated = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
    base = PlanState(
        plan_id=plan.plan_id,
        project_id=plan.project_id,
        repo=plan.repo,
        base_ref=plan.base_ref,
        target_head=head,
        revision=revision.version,
        sha256=revision.sha256,
        auto_advance=plan.auto_advance,
        auto_refresh=plan.auto_refresh,
        generated_at=generated,
        digest="",
        lanes=tuple(lanes),
        ready=ready,
        held=held,
        landing=landing,
        next_action=_next_action(tuple(lanes), plan),
    )
    return replace(base, digest=_projection_digest(base))


def _in_landing_order(lanes: list[LaneState]) -> list[LaneState]:
    return sorted(lanes, key=lambda lane: (*_wave_sort_key(lane.wave), lane.lane_id))


def _projection_digest(state: PlanState) -> str:
    """Hash the fields a render shows, excluding the wall-clock stamp."""
    payload = state.to_dict()
    payload.pop("generated_at", None)
    payload.pop("digest", None)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def _cell(value: str | None) -> str:
    if not value:
        return "-"
    return value.replace("|", "\\|").replace("\n", " ").strip()


def _status_cell(lane: LaneState) -> str:
    if lane.state == "queued" and lane.waiting_reason:
        return f"queued ({lane.waiting_reason})"
    if lane.state == "failed" and lane.failure_reason:
        return f"failed ({lane.failure_reason})"
    return lane.state


def _worker_cell(lane: LaneState) -> str:
    if lane.worker and lane.model:
        return f"{lane.worker}/{lane.model}"
    return lane.worker or lane.model or "-"


def render_plan_state_markdown(state: PlanState) -> str:
    """Render one projection as the owner-readable ``STATE.md``."""
    target = state.target_head[:12] if state.target_head else "unknown"
    lines = [
        f"# Plan state: {state.plan_id}",
        "",
        f"<!-- saberops-state-digest: {state.digest} -->",
        f"Generated from SaberOps engine records at {state.generated_at}. Do not edit.",
        (
            f"Plan `{state.plan_id}` - project `{state.project_id}` - revision "
            f"`{state.revision}` (sha256 {state.sha256[:12]}...) - target "
            f"`{state.base_ref}` @ {target}"
        ),
        (
            f"Auto-advance: {'on' if state.auto_advance else 'off'} - "
            f"Auto-refresh: {'on' if state.auto_refresh else 'off'}"
        ),
        "",
        "| Lane | Wave | Status | Needs | Worker | Branch | Commit | Evidence |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for lane in state.lanes:
        needs = ", ".join(lane.needs) if lane.needs else "none"
        evidence = ", ".join(lane.evidence) if lane.evidence else "-"
        lines.append(
            "| "
            + " | ".join(
                (
                    _cell(lane.lane_id),
                    _cell(lane.wave or "-"),
                    _cell(_status_cell(lane)),
                    _cell(needs),
                    _cell(_worker_cell(lane)),
                    _cell(lane.branch),
                    _cell(lane.commit),
                    _cell(evidence),
                )
            )
            + " |"
        )

    lines.extend(["", "## Ready now"])
    ready = [lane for lane in state.lanes if lane.state == "ready"]
    if ready:
        lines.extend(f"- {lane.lane_id} - submitted on the next tick" for lane in ready)
    else:
        lines.append("- (none)")

    lines.extend(["", "## Held"])
    held = [lane for lane in state.lanes if lane.state in _HELD_STATES]
    if not held:
        lines.append("- (none)")
    else:
        for lane in held:
            if lane.state == "failed" and lane.failure_reason:
                detail = f"failed ({lane.failure_reason})"
            else:
                detail = lane.state
                if lane.reasons:
                    detail += f" ({', '.join(lane.reasons)})"
                if lane.failure_reason:
                    detail += f" ({lane.failure_reason})"
            if lane.owner_evidence:
                detail += f" - owner: {lane.owner_evidence}"
            lines.append(f"- {lane.lane_id} - {detail}")

    lines.extend(["", "## Landing queue (deterministic: wave, then lane id)"])
    landing = [
        lane
        for lane in _in_landing_order(list(state.lanes))
        if lane.state in _CANDIDATE_STATES
    ]
    if not landing:
        lines.append("- (empty)")
    else:
        for index, lane in enumerate(landing, start=1):
            if lane.state == "stale" and lane.run_id:
                lines.append(
                    f"{index}. {lane.lane_id} - stale; `orch refresh {lane.run_id}`"
                )
            else:
                commit = f" at {lane.commit[:12]}" if lane.commit else ""
                lines.append(f"{index}. {lane.lane_id} - candidate ready for accept{commit}")

    lines.extend(["", "## Next owner action", state.next_action, ""])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def plan_state_path(plan: PlanRecord) -> Path:
    """Canonical state file, outside the repository (ADR-0015)."""
    return (
        owner_state_dir()
        / "plans"
        / plan.project_id
        / plan.plan_id
        / "state.md"
    )


def _existing_digest(path: Path) -> str | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            head = handle.read(512)
    except OSError:
        return None
    match = _DIGEST_RE.search(head)
    return match.group(1) if match is not None else None


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temp.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def write_plan_state(
    state: PlanState, path: Path, *, force: bool = False
) -> bool:
    """Write the ledger atomically; return ``True`` only when it changed."""
    text = render_plan_state_markdown(state)
    if not force and _existing_digest(path) == state.digest:
        return False
    _atomic_write(path, text)
    return True


def render_plan_state(
    db: Any,
    plan: PlanRecord,
    *,
    now: datetime | None = None,
    path: Path | None = None,
    force: bool = False,
) -> bool:
    """Render one plan to its canonical file (or ``path``)."""
    state = project_plan_state(db, plan, now=now)
    return write_plan_state(state, path or plan_state_path(plan), force=force)


def render_active_plan_states(
    db: Any, *, now: datetime | None = None
) -> tuple[str, ...]:
    """Render every active plan; return the plan ids whose file changed."""
    written: list[str] = []
    for plan in list_active_plans(db):
        try:
            if render_plan_state(db, plan, now=now):
                written.append(plan.plan_id)
        except Exception:
            # A broken plan row must never stop the scheduler or other plans.
            logging.getLogger(__name__).exception(
                "plan state render failed for plan %s", plan.plan_id
            )
    return tuple(written)


def _ensure_excluded(repo: Path, entry: str) -> None:
    """Append ``entry`` to ``.git/info/exclude`` once (never a tracked file)."""
    info = repo / ".git" / "info"
    info.mkdir(parents=True, exist_ok=True)
    exclude = info / "exclude"
    try:
        text = exclude.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    existing = {line.strip() for line in text.splitlines()}
    if entry in existing:
        return
    suffix = "" if not text or text.endswith("\n") else "\n"
    exclude.write_text(f"{text}{suffix}{entry}\n", encoding="utf-8")


def adopt_state_file(db: Any, plan: PlanRecord) -> dict[str, Any]:
    """Create the git-ignored ``STATE.md`` symlink in the canonical checkout.

    Idempotent.  Refuses a real file, a link that points elsewhere, and any
    checkout that is not the repository's main working tree, so a worker
    worktree never receives the link.  The canonical file is rendered first.
    """
    repo = Path(plan.repo).expanduser().resolve()
    if not repo.is_dir():
        raise PlanStateError(f"plan {plan.plan_id!r} checkout {repo} does not exist")
    if not (repo / ".git").is_dir():
        raise PlanStateError(
            f"refusing to create {STATE_FILE_NAME} in {repo}: it is not the "
            "canonical checkout (worker worktrees never receive the state link)"
        )
    canonical = plan_state_path(plan)
    render_plan_state(db, plan, path=canonical, force=True)
    link = repo / STATE_FILE_NAME
    if link.is_symlink():
        target = Path(os.path.realpath(link))
        if target != Path(os.path.realpath(canonical)):
            raise PlanStateError(
                f"refusing to replace {link}: it already points to {target}"
            )
    elif link.exists():
        raise PlanStateError(
            f"refusing to replace the real file {link}; move it aside and re-run adopt"
        )
    else:
        _ensure_excluded(repo, STATE_FILE_NAME)
        link.symlink_to(canonical)
    _ensure_excluded(repo, STATE_FILE_NAME)
    return {
        "plan_id": plan.plan_id,
        "canonical": str(canonical),
        "link": str(link),
        "target": str(Path(os.path.realpath(link))),
        "clean": GitManager.is_clean(repo),
    }


# ---------------------------------------------------------------------------
# Lookup helper used by the CLI
# ---------------------------------------------------------------------------


def resolve_plan(
    db: Any,
    *,
    plan_id: str | None = None,
    project_id: str | None = None,
) -> PlanRecord:
    """Resolve one active plan for a CLI verb, refusing ambiguity."""
    from saberops.plan_runner import active_plan_for_project

    if plan_id:
        plan = get_plan(db, plan_id)
        if plan is None:
            raise PlanRunnerError(f"unknown plan {plan_id!r}")
        return plan
    if project_id:
        plan = active_plan_for_project(db, project_id)
        if plan is None:
            raise PlanRunnerError(
                f"project {project_id!r} has no active plan"
            )
        return plan
    plans = list_active_plans(db)
    if not plans:
        raise PlanRunnerError("no active plan; activate one with `orch plan-file submit`")
    if len(plans) > 1:
        names = ", ".join(plan.plan_id for plan in plans)
        raise PlanRunnerError(f"several active plans ({names}); pass --plan")
    return plans[0]
