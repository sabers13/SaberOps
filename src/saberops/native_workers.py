"""Native subagent workers from the orchestrating CLI (ADR-0030, F16).

The orchestrating CLI's own subagents can do some of the work natively while
other jobs run as ordinary SaberOps workers, and one orchestrator controls both
through the same job, run, gate, review and acceptance records.  The native
child cooperates through the SaberOps MCP server that is already available in
its session:

* it claims a queued job, which makes SaberOps prepare a worktree pinned at the
  job's base commit;
* it heartbeats while it works, and every call returns any queued owner
  steering note and any stop request;
* it reports the candidate commit, transcript and times when it is done.

Control is **cooperative, not enforced**.  SaberOps cannot kill the subagent's
process, observe it once it stops calling, measure its quota independently or
prove it stayed in its worktree.  What it can do, and does, is fail closed on
the evidence: a report is accepted only when the candidate descends from the
pinned base, the branch and worktree are exactly the ones SaberOps created, the
canonical checkout and target branch did not move, the project gate passes on
the exact commit, and an external reviewer of a different family passes it.
Silence past the heartbeat deadline marks the job needs-attention and posts a
stop request for the orchestrator to act on with its own native control.

Everything is off by default: with :data:`NATIVE_WORKERS_ENV` unset the MCP
tools are not registered at all.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from saberops.db import current_iso_timestamp
from saberops.git import GitManager
from saberops.jobs import store as job_store
from saberops.jobs.models import RUN_FAILED, Job, JobState
from saberops.models import Attempt, AttemptStatus, CheckResult, Run, RunStatus

if TYPE_CHECKING:
    from saberops.db import Database

__all__ = [
    "NATIVE_ATTEMPT_EVENT",
    "NATIVE_CLAIMED_EVENT",
    "NATIVE_HEARTBEAT_EVENT",
    "NATIVE_HEARTBEAT_SECONDS_ENV",
    "NATIVE_MAX_CONCURRENT_ENV",
    "NATIVE_QUOTA_SAMPLE_ENV",
    "NATIVE_STOP_EVENT",
    "NATIVE_TOOL_NAMES",
    "NATIVE_VENDORS",
    "NativeWorkerError",
    "claim_native_job",
    "native_controls",
    "native_enabled",
    "native_heartbeat",
    "native_run_marker",
    "native_status",
    "record_native_attempt",
    "request_native_stop",
]

#: Off-by-default owner flag.  With it unset the native tools do not exist on
#: the MCP server at all ("turning the flag off removes the tool").
NATIVE_WORKERS_ENV = "ORCH_NATIVE_WORKERS"

#: Seconds of silence before a native child is marked needs-attention and a
#: stop is posted for the orchestrator.  The worker definition asks for a
#: heartbeat at least every few minutes.
NATIVE_HEARTBEAT_SECONDS_ENV = "ORCH_NATIVE_HEARTBEAT_SECONDS"
DEFAULT_HEARTBEAT_SECONDS = 600.0

#: Concurrent native attempts allowed per host.  One setting, default one.
NATIVE_MAX_CONCURRENT_ENV = "ORCH_NATIVE_MAX_CONCURRENT"
DEFAULT_MAX_CONCURRENT = 1

#: Optional metadata-only quota sampling through the existing safe readers
#: (cli_usage.read_limits).  Off by default so tests and CI never launch a
#: vendor CLI; when off, usage is reported as ``unknown``/``estimated``.
NATIVE_QUOTA_SAMPLE_ENV = "ORCH_NATIVE_QUOTA_SAMPLE"

NATIVE_CLAIMED_EVENT = "native_claimed"
NATIVE_HEARTBEAT_EVENT = "native_heartbeat"
NATIVE_STOP_EVENT = "native_stop_requested"
NATIVE_ATTEMPT_EVENT = "native_attempt"

#: A host's own vendor may run natively.  Antigravity is never an automated
#: worker (owner decision): it may only be the orchestrator with its own agents.
NATIVE_VENDORS: tuple[str, ...] = ("claude", "codex", "opencode")

#: The four MCP tools that exist only while the flag is on.
NATIVE_TOOL_NAMES: tuple[str, ...] = (
    "native_claim",
    "native_heartbeat",
    "native_controls",
    "record_native_attempt",
)

_TRUTHY = frozenset({"1", "true", "yes", "on"})

#: Upper bound on any transcript or progress text kept in an event payload.
MAX_TRANSCRIPT_CHARS = 8000
MAX_PROGRESS_CHARS = 500


class NativeWorkerError(RuntimeError):
    """A native-worker contract violation; every one fails closed."""

    def __init__(self, code: str, message: str, *, data: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.data: dict[str, Any] = dict(data or {})


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


def native_enabled(env: Mapping[str, str] | None = None) -> bool:
    """True only for an explicit truthy flag; absence and junk mean off."""
    source = os.environ if env is None else env
    return str(source.get(NATIVE_WORKERS_ENV, "")).strip().lower() in _TRUTHY


def _heartbeat_seconds() -> float:
    raw = os.environ.get(NATIVE_HEARTBEAT_SECONDS_ENV, "").strip()
    try:
        value = float(raw) if raw else DEFAULT_HEARTBEAT_SECONDS
    except ValueError:
        return DEFAULT_HEARTBEAT_SECONDS
    return value if value > 0 else DEFAULT_HEARTBEAT_SECONDS


def _max_concurrent() -> int:
    raw = os.environ.get(NATIVE_MAX_CONCURRENT_ENV, "").strip()
    try:
        value = int(raw) if raw else DEFAULT_MAX_CONCURRENT
    except ValueError:
        return DEFAULT_MAX_CONCURRENT
    return value if value > 0 else DEFAULT_MAX_CONCURRENT


def _quota_sampling_enabled() -> bool:
    return str(os.environ.get(NATIVE_QUOTA_SAMPLE_ENV, "")).strip().lower() in _TRUTHY


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _now(now: datetime | None) -> datetime:
    moment = now if now is not None else datetime.now(UTC)
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _text(value: object, what: str, *, limit: int | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NativeWorkerError("NATIVE_INVALID_ARGUMENT", f"{what} is required")
    clean = value.strip()
    if limit is not None and len(clean) > limit:
        clean = clean[:limit]
    return clean


def _clean_vendor(value: object) -> str:
    vendor = str(value or "").strip().lower()
    if vendor == "antigravity" or vendor == "agy":
        raise NativeWorkerError(
            "NATIVE_VENDOR_UNSUPPORTED",
            "Antigravity is never an automated native worker; it may only be the "
            "orchestrator with its own agents",
            data={"vendor": vendor},
        )
    if vendor not in NATIVE_VENDORS:
        raise NativeWorkerError(
            "NATIVE_VENDOR_UNSUPPORTED",
            f"native workers support {', '.join(NATIVE_VENDORS)}; got {vendor or 'nothing'}",
            data={"vendor": vendor},
        )
    return vendor


def _require_work_gate(db: Database, *, action: str) -> None:
    """Refuse a new start while the F17 work gate is closed (ADR-0029)."""
    from saberops.runtime_gate import evaluate_gate

    try:
        state = evaluate_gate(db=db)
    except Exception as exc:  # fail closed: an unreadable gate is closed
        raise NativeWorkerError(
            "NATIVE_WORK_GATE_CLOSED", f"cannot start {action}: the work gate is unreadable: {exc}"
        ) from exc
    if state.closed:
        raise NativeWorkerError(
            "NATIVE_WORK_GATE_CLOSED",
            f"cannot {action}: work is paused ({state.label})",
            data={"reason": state.reason, "detail": state.detail, "resume": "orch resume"},
        )


def _require_sha(value: object, what: str) -> str:
    text = _text(value, what).lower()
    if len(text) != 40 or any(ch not in "0123456789abcdef" for ch in text):
        raise NativeWorkerError(
            "NATIVE_INVALID_SHA", f"{what} must be a full 40-character commit SHA"
        )
    return text


def _family_for_vendor(vendor: str) -> str:
    return {"claude": "anthropic", "codex": "codex", "opencode": "opencode"}[vendor]


def _reaches_manifest(path: Path) -> bool:
    try:
        return path.is_dir() and (path / ".git").exists()
    except OSError:
        return False


def _event_payloads(db: Database, run_id: str, event_type: str) -> list[dict[str, Any]]:
    try:
        events = db.get_events_by_type(run_id, event_type)
    except Exception:
        return []
    return [dict(event.payload or {}) for event in events]


def native_run_marker(db: Database, run_id: str) -> dict[str, Any] | None:
    """Return the durable native claim for ``run_id``, or ``None``."""
    payloads = _event_payloads(db, run_id, NATIVE_CLAIMED_EVENT)
    return payloads[-1] if payloads else None


def _require_claim(db: Database, job_id: str) -> tuple[Job, dict[str, Any]]:
    job = job_store.get_job(db, job_id)
    if job is None:
        raise NativeWorkerError("NATIVE_JOB_UNKNOWN", f"unknown job: {job_id}")
    if not job.current_run_id:
        raise NativeWorkerError(
            "NATIVE_NOT_CLAIMED", f"job {job_id} has no run; claim it first"
        )
    marker = native_run_marker(db, job.current_run_id)
    if marker is None:
        raise NativeWorkerError(
            "NATIVE_NOT_CLAIMED",
            f"job {job_id} was not claimed as a native attempt",
            data={"job_id": job_id, "run_id": job.current_run_id},
        )
    return job, marker


def _latest_heartbeat(db: Database, run_id: str) -> dict[str, Any]:
    payloads = _event_payloads(db, run_id, NATIVE_HEARTBEAT_EVENT)
    return payloads[-1] if payloads else {}


def _stop_payload(db: Database, run_id: str) -> dict[str, Any]:
    payloads = _event_payloads(db, run_id, NATIVE_STOP_EVENT)
    return payloads[-1] if payloads else {}


def _active_native_count(db: Database) -> int:
    count = 0
    for job in job_store.list_jobs_raw(db, state=JobState.RUNNING, limit=1000):
        if job.current_run_id and native_run_marker(db, job.current_run_id) is not None:
            count += 1
    return count


# --------------------------------------------------------------------------- #
# Quota evidence (never blocks a claim or a report)
# --------------------------------------------------------------------------- #

#: (executable, provider) per native vendor for the existing safe quota readers.
_QUOTA_READER: dict[str, tuple[str, str]] = {
    "claude": ("claude", "anthropic"),
    "codex": ("codex", "codex"),
    "opencode": ("opencode", "opencode"),
}


def _sample_quota(vendor: str) -> dict[str, Any] | None:
    """One metadata-only host quota read, or ``None`` when unavailable.

    It never sends a prompt: :func:`saberops.cli_usage.read_limits` uses each
    CLI's own non-inference metadata surface.  Sampling is opt-in
    (:data:`NATIVE_QUOTA_SAMPLE_ENV`) and a missing or failing CLI is simply
    "unknown", never a claim failure.
    """
    if not _quota_sampling_enabled():
        return None
    vendor_reader = _QUOTA_READER.get(vendor)
    if vendor_reader is None:
        return None
    executable, provider = vendor_reader
    if shutil.which(executable) is None:
        return None
    try:
        from saberops.cli_usage import read_limits

        rows = read_limits(executable, provider)
    except Exception:
        return None
    limits = [
        {"label": row.get("label"), "remaining": row.get("remaining")}
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("remaining"), (int, float))
    ]
    return {"vendor": vendor, "at": current_iso_timestamp(), "limits": limits} if limits else None


def _quota_delta(before: Mapping[str, Any] | None, after: Mapping[str, Any] | None) -> float | None:
    if not before or not after:
        return None
    before_rows = {row["label"]: row["remaining"] for row in before.get("limits", [])}
    used = 0.0
    matched = False
    for row in after.get("limits", []):
        label = row.get("label")
        if label in before_rows:
            matched = True
            used += max(0.0, float(before_rows[label]) - float(row["remaining"]))
    return used if matched else None


def _learned_estimate(db: Database, vendor: str) -> float | None:
    """Mean measured usage of earlier native attempts on this vendor."""
    values: list[float] = []
    try:
        runs = db.list_runs(limit=50)
    except Exception:
        return None
    for run in runs:
        for payload in _event_payloads(db, run.id, NATIVE_ATTEMPT_EVENT):
            usage = payload.get("usage")
            if not isinstance(usage, dict) or usage.get("vendor") != vendor:
                continue
            if usage.get("label") != "measured":
                continue
            percent = usage.get("percent_used")
            if isinstance(percent, (int, float)):
                values.append(float(percent))
    if not values:
        return None
    return sum(values) / len(values)


# --------------------------------------------------------------------------- #
# Claim
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _ClaimFields:
    run_id: str
    attempt_id: str
    branch: str
    worktree: Path


def _new_run(db: Database, job: Job, *, vendor: str, model: str) -> Run:
    """Create the run row a native attempt lives in (no supervisor)."""
    from saberops.authority import load_authority_settings
    from saberops.config import freeze_owner_binding_registry_payload
    from saberops.control_plane import freeze_control_plane_policy, load_control_plane_policy
    from saberops.provenance import mint_run_provenance
    from saberops.routing_config import (
        load_effective_routing,
        snapshot_references_exact_bindings,
        snapshot_to_payload,
    )
    from saberops.run_creation import RunRequest, resolve_effective_run_config
    from saberops.tooling import freeze_tool_contract

    repo = Path(job.repo).resolve()
    config = resolve_effective_run_config(
        RunRequest(
            task=job.goal,
            repo_path=repo,
            provider_override=vendor,
            model_override=model,
            review_enabled=True,
        )
    )
    snapshot = load_effective_routing()
    if config.worker_binding_registry is None and snapshot_references_exact_bindings(snapshot):
        config = replace(config, worker_binding_registry=freeze_owner_binding_registry_payload())
    frozen_policy = freeze_control_plane_policy(
        load_control_plane_policy(),
        required_capabilities=config.required_capabilities,
        reserve_override=config.reserve_override,
        host_access=load_authority_settings().host_access.value,
    )
    from saberops.supervisor import RunSupervisor

    frozen_tools = freeze_tool_contract(config.tool_refs, RunSupervisor(db=db).tool_registry)
    provenance = mint_run_provenance(repo, job.goal)
    created_at = current_iso_timestamp()
    run = Run(
        id=provenance.run_id,
        task=job.goal,
        target_repo=str(repo),
        base_commit=job.base_sha,
        tier=config.initial_tier,
        training_allowed=config.training_allowed,
        gate_command=config.gate_command,
        status=RunStatus.RUNNING,
        created_at=created_at,
        config_json=json.dumps(config.as_dict()),
        routing_snapshot_json=json.dumps(snapshot_to_payload(snapshot, created_at)),
        control_plane_snapshot_json=frozen_policy.as_json(),
        control_plane_snapshot_digest=frozen_policy.digest,
        tool_contract_json=frozen_tools.to_json(),
        tool_contract_digest=frozen_tools.digest,
        project_id=provenance.project_id,
        project_identity_json=provenance.identity.to_json(),
        objective_id=provenance.objective_id,
    )
    db.create_run(run)
    return run


def claim_native_job(
    db: Database,
    job_id: str,
    *,
    vendor: object,
    model: object,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Claim one queued job for a native child and prepare its worktree."""
    if not native_enabled():
        raise NativeWorkerError(
            "NATIVE_WORKERS_DISABLED",
            f"native workers are off; set {NATIVE_WORKERS_ENV}=1 to enable them",
        )
    clean_vendor = _clean_vendor(vendor)
    clean_model = _text(model, "model", limit=200)
    job = job_store.get_job(db, job_id)
    if job is None:
        raise NativeWorkerError("NATIVE_JOB_UNKNOWN", f"unknown job: {job_id}")
    _require_work_gate(db, action="claim a native job")
    if job.state is not JobState.QUEUED:
        raise NativeWorkerError(
            "NATIVE_JOB_NOT_QUEUED",
            f"job {job_id} is {job.state.value}; only a QUEUED job can be claimed",
            data={"job_id": job_id, "state": job.state.value},
        )
    repo = Path(job.repo).resolve()
    if not job.base_sha:
        raise NativeWorkerError("NATIVE_BASE_MISSING", f"job {job_id} has no pinned base")
    if not GitManager.is_clean(repo):
        raise NativeWorkerError(
            "NATIVE_REPO_DIRTY", "the canonical checkout has uncommitted changes"
        )
    if not GitManager.is_ancestor(repo, job.base_sha, GitManager.get_head_commit(repo)):
        raise NativeWorkerError(
            "NATIVE_BASE_UNKNOWN",
            "the job base is not a commit of the canonical checkout",
            data={"base_sha": job.base_sha},
        )
    if _active_native_count(db) >= _max_concurrent():
        raise NativeWorkerError(
            "NATIVE_CONCURRENCY_LIMIT",
            f"{_max_concurrent()} native attempt(s) already running on this host",
        )
    canonical_head = GitManager.get_head_commit(repo)
    canonical_branch = GitManager.get_current_branch(repo)
    moment = _now(now)
    try:
        run = _new_run(db, job, vendor=clean_vendor, model=clean_model)
    except Exception as exc:
        raise NativeWorkerError(
            "NATIVE_POLICY_UNAVAILABLE",
            f"cannot freeze the native run policy (gate, routing, provenance): {exc}",
        ) from exc
    run_id = run.id
    branch = f"orch/{run_id}/a1"
    worktree = GitManager.get_default_worktree_base(repo) / run_id / "a1"
    try:
        GitManager.create_worktree(repo, branch, worktree, start_point=job.base_sha)
    except Exception as exc:
        db.update_run(run_id, RunStatus.FAILED, current_iso_timestamp(), result_summary=str(exc))
        raise NativeWorkerError(
            "NATIVE_WORKTREE_FAILED", f"could not prepare the worktree: {exc}"
        ) from exc
    attempt_id = f"{run_id}_a1"
    db.create_attempt(
        Attempt(
            id=attempt_id,
            run_id=run_id,
            attempt_number=1,
            provider=clean_vendor,
            model=clean_model,
            worktree_path=str(worktree),
            branch_name=branch,
            status=AttemptStatus.RUNNING,
            created_at=current_iso_timestamp(),
            base_sha=job.base_sha,
        )
    )
    try:
        job_store.mark_dispatched(db, job_id, run_id, now=moment)
    except Exception as exc:
        db.update_run(
            run_id,
            RunStatus.FAILED,
            current_iso_timestamp(),
            result_summary="native claim lost the job before dispatch",
        )
        raise NativeWorkerError(
            "NATIVE_JOB_MOVED",
            f"job {job_id} moved while it was being claimed; nothing was dispatched",
            data={"job_id": job_id},
        ) from exc
    quota_before = _sample_quota(clean_vendor)
    deadline = moment + timedelta(seconds=_heartbeat_seconds())
    db.record_event(
        run_id,
        NATIVE_CLAIMED_EVENT,
        attempt_id=attempt_id,
        payload={
            "job_id": job_id,
            "vendor": clean_vendor,
            "model": clean_model,
            "base_sha": job.base_sha,
            "branch": branch,
            "worktree": str(worktree),
            "canonical_head": canonical_head,
            "canonical_branch": canonical_branch,
            "claimed_at": _iso(moment),
            "heartbeat_seconds": _heartbeat_seconds(),
            "quota_before": quota_before,
            "supervision": "host",
            "quota": "unknown",
        },
    )
    return {
        "job_id": job_id,
        "run_id": run_id,
        "attempt_id": attempt_id,
        "vendor": clean_vendor,
        "model": clean_model,
        "base_sha": job.base_sha,
        "branch": branch,
        "worktree": str(worktree),
        "claimed_at": _iso(moment),
        "deadline_at": _iso(deadline),
        "heartbeat_seconds": _heartbeat_seconds(),
        "supervision": "host",
        "quota": "unknown",
        "guidance": (
            "Work only inside `worktree`; commit on `branch`. Call native_heartbeat at "
            "least every few minutes, read the steering notes and stop_requested it "
            "returns, then call record_native_attempt with candidate_sha."
        ),
    }


# --------------------------------------------------------------------------- #
# Status, heartbeat and stop requests
# --------------------------------------------------------------------------- #


def _status_from(
    db: Database,
    job: Job,
    marker: Mapping[str, Any],
    *,
    moment: datetime,
) -> dict[str, Any]:
    run_id = str(marker.get("run_id") or job.current_run_id or "")
    heartbeat = _latest_heartbeat(db, run_id)
    last_at = _parse_iso(heartbeat.get("at")) or _parse_iso(marker.get("claimed_at")) or moment
    interval = float(marker.get("heartbeat_seconds") or _heartbeat_seconds())
    deadline = last_at + timedelta(seconds=interval)
    stop = _stop_payload(db, run_id)
    terminal = job.state is not JobState.RUNNING
    expired = not terminal and moment > deadline
    return {
        "job_id": job.job_id,
        "run_id": run_id,
        "native": True,
        "vendor": marker.get("vendor"),
        "model": marker.get("model"),
        "supervision": "host",
        "quota": "unknown",
        "base_sha": marker.get("base_sha"),
        "branch": marker.get("branch"),
        "worktree": marker.get("worktree"),
        "claimed_at": marker.get("claimed_at"),
        "last_heartbeat_at": _iso(last_at),
        "heartbeat_seconds": interval,
        "deadline_at": _iso(deadline),
        "seconds_to_deadline": max(0, int((deadline - moment).total_seconds())),
        "progress": heartbeat.get("progress"),
        "stop_requested": bool(stop),
        "stop_reason": stop.get("reason"),
        "needs_attention": bool(stop) or expired or terminal,
        "job_state": job.state.value,
    }


def native_status(
    db: Database, job: Job, *, now: datetime | None = None
) -> dict[str, Any] | None:
    """Read-only native view for a job; ``None`` when it is not native."""
    if not job.current_run_id:
        return None
    marker = native_run_marker(db, job.current_run_id)
    if marker is None:
        return None
    return _status_from(db, job, marker, moment=_now(now))


def request_native_stop(
    db: Database, job: Job, *, reason: str, detail: str | None = None
) -> dict[str, Any]:
    """Post one durable stop request for the orchestrator to act on (idempotent)."""
    run_id = job.current_run_id
    if not run_id:
        raise NativeWorkerError("NATIVE_NOT_CLAIMED", f"job {job.job_id} has no run")
    existing = _stop_payload(db, run_id)
    if existing:
        return existing
    payload = {
        "job_id": job.job_id,
        "reason": reason,
        "detail": detail,
        "at": current_iso_timestamp(),
        "action": (
            "Stop this child with your own native control (subagent stop/task stop). "
            "SaberOps cannot kill it."
        ),
    }
    db.record_event(run_id, NATIVE_STOP_EVENT, payload=payload)
    return payload


def native_heartbeat(
    db: Database,
    job_id: str,
    *,
    progress: object = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record liveness, deliver pending steering, and return the stop state."""
    job, marker = _require_claim(db, job_id)
    if job.state is not JobState.RUNNING:
        raise NativeWorkerError(
            "NATIVE_JOB_NOT_RUNNING",
            f"job {job_id} is {job.state.value}; a stop request is already posted",
            data={"job_id": job_id, "state": job.state.value},
        )
    moment = _now(now)
    run_id = job.current_run_id
    assert run_id is not None  # _require_claim guarantees this
    clean_progress = None
    if isinstance(progress, str) and progress.strip():
        clean_progress = progress.strip()[:MAX_PROGRESS_CHARS]
    db.record_event(
        run_id,
        NATIVE_HEARTBEAT_EVENT,
        attempt_id=str(marker.get("attempt_id") or "") or None,
        payload={"at": _iso(moment), "progress": clean_progress},
    )
    from saberops.jobs.store import pending_steering_notes_for_run
    from saberops.steering import record_steering_delivery

    notes = tuple(pending_steering_notes_for_run(db, run_id))
    if notes:
        record_steering_delivery(
            db,
            notes,
            run_id=run_id,
            attempt_id=str(marker.get("attempt_id") or f"{run_id}_a1"),
        )
    status = _status_from(db, job, marker, moment=moment)
    status["steering_notes"] = [note.to_dict() for note in notes]
    return status


def native_controls(
    db: Database, job_id: str, *, now: datetime | None = None
) -> dict[str, Any]:
    """Read native control state and post a stop when the deadline has passed."""
    job, marker = _require_claim(db, job_id)
    moment = _now(now)
    status = _status_from(db, job, marker, moment=moment)
    if status["needs_attention"] and not status["stop_requested"]:
        stop = request_native_stop(
            db,
            job,
            reason="heartbeat_deadline",
            detail=(
                f"no heartbeat for more than {status['heartbeat_seconds']}s; "
                "the job is needs-attention"
            ),
        )
        status["stop_requested"] = True
        status["stop_reason"] = stop.get("reason")
    return status


# --------------------------------------------------------------------------- #
# Report and verification
# --------------------------------------------------------------------------- #


def _changed_paths(repo: Path, base: str, candidate: str) -> list[str]:
    return GitManager.get_changed_paths_between(repo, base, candidate)


def _worktree_toplevel(worktree: Path) -> Path | None:
    from saberops.process import run_process

    result = run_process(["git", "rev-parse", "--show-toplevel"], cwd=worktree, timeout=30)
    if not result.passed or not result.stdout.strip():
        return None
    return Path(result.stdout.strip()).resolve()


def _verify_worktree(marker: Mapping[str, Any], worktree: Path, candidate: str) -> None:
    if Path(str(marker.get("worktree") or "")).resolve() != worktree:
        raise NativeWorkerError(
            "NATIVE_WORKTREE_MISMATCH",
            "the reported worktree is not the one SaberOps prepared",
            data={"expected": marker.get("worktree"), "reported": str(worktree)},
        )
    if not _reaches_manifest(worktree):
        raise NativeWorkerError("NATIVE_WORKTREE_MISSING", f"{worktree} is not a worktree")
    top = _worktree_toplevel(worktree)
    if top is not None and top != worktree:
        raise NativeWorkerError(
            "NATIVE_WORKTREE_MISMATCH", f"{worktree} resolves to {top}, not the prepared worktree"
        )
    if not GitManager.is_clean(worktree):
        raise NativeWorkerError(
            "NATIVE_WORKTREE_DIRTY", "the worktree has uncommitted changes; commit them first"
        )
    if GitManager.get_head_commit(worktree) != candidate:
        raise NativeWorkerError(
            "NATIVE_CANDIDATE_NOT_CHECKED_OUT",
            "the worktree HEAD is not the reported candidate commit",
            data={"candidate_sha": candidate, "head": GitManager.get_head_commit(worktree)},
        )


def _verify_lineage(repo: Path, marker: Mapping[str, Any], base: str, candidate: str) -> None:
    if base != str(marker.get("base_sha") or ""):
        raise NativeWorkerError(
            "NATIVE_BASE_MISMATCH",
            "the reported base is not the pinned base this attempt was claimed for",
            data={"pinned": marker.get("base_sha"), "reported": base},
        )
    if candidate == base:
        raise NativeWorkerError(
            "NATIVE_EMPTY_CANDIDATE", "the candidate commit is the pinned base; nothing changed"
        )
    if not GitManager.is_ancestor(repo, base, candidate):
        raise NativeWorkerError(
            "NATIVE_LINEAGE_BROKEN",
            "the candidate does not descend from the pinned base",
            data={"base_sha": base, "candidate_sha": candidate},
        )
    branch = str(marker.get("branch") or "")
    if not branch or GitManager.get_ref_commit(repo, branch) != candidate:
        raise NativeWorkerError(
            "NATIVE_CANDIDATE_NOT_ON_BRANCH",
            "the candidate is not the tip of the branch SaberOps created",
            data={"branch": branch, "candidate_sha": candidate},
        )


def _verify_canonical(repo: Path, marker: Mapping[str, Any]) -> None:
    if GitManager.get_head_commit(repo) != str(marker.get("canonical_head") or ""):
        raise NativeWorkerError(
            "NATIVE_CANONICAL_MOVED", "the canonical checkout's HEAD moved during the attempt"
        )
    if GitManager.get_current_branch(repo) != marker.get("canonical_branch"):
        raise NativeWorkerError(
            "NATIVE_CANONICAL_MOVED", "the canonical checkout's branch changed during the attempt"
        )
    if not GitManager.is_clean(repo):
        raise NativeWorkerError(
            "NATIVE_CANONICAL_MOVED", "the canonical checkout has uncommitted changes"
        )
    if GitManager.get_current_branch(repo) == marker.get("branch"):
        raise NativeWorkerError(
            "NATIVE_CANONICAL_MOVED", "the candidate branch is checked out in the canonical repo"
        )


def _verify_diff_paths(paths: list[str], worktree: Path) -> None:
    if not paths:
        raise NativeWorkerError(
            "NATIVE_EMPTY_CANDIDATE", "the candidate diff is empty; nothing to review"
        )
    for raw in paths:
        parsed = Path(raw)
        if parsed.is_absolute() or ".." in parsed.parts or ".git" in parsed.parts:
            raise NativeWorkerError(
                "NATIVE_DIFF_ESCAPES_WORKTREE",
                f"the candidate diff touches a path outside the worktree: {raw}",
                data={"path": raw, "worktree": str(worktree)},
            )


def _run_gate(config: Any, worktree: Path) -> Any:
    from saberops.orchestrator import _run_configured_gate
    from saberops.process import split_command

    args = split_command(config.gate_command)
    if not args:
        raise NativeWorkerError(
            "NATIVE_GATE_MISSING", "a native attempt requires a configured project gate"
        )
    return _run_configured_gate(config, args, cwd=worktree, timeout=config.gate_timeout)


def _default_reviewer(db: Database, run_id: str, timeout: float) -> Any:
    from saberops.service import OrchestratorService

    return OrchestratorService(db=db).review_run(run_id, timeout=timeout)


def _review_is_different_family(review: Any, vendor: str) -> bool:
    from saberops.capacity.admission import matches_model_family
    from saberops.models import WorkerCandidate

    candidate = WorkerCandidate(provider=str(review.provider), model=str(review.model))
    return not matches_model_family(candidate, _family_for_vendor(vendor))


def _usage_record(
    db: Database,
    vendor: str,
    marker: Mapping[str, Any],
    *,
    usage: Mapping[str, Any] | None,
) -> dict[str, Any]:
    before = marker.get("quota_before")
    after = _sample_quota(vendor)
    measured = _quota_delta(before, after)
    if measured is not None:
        return {
            "vendor": vendor,
            "label": "measured",
            "percent_used": round(measured, 4),
            "source": "host quota reader",
            "before": before,
            "after": after,
            "reported": dict(usage or {}),
        }
    estimate = _learned_estimate(db, vendor)
    if estimate is not None:
        return {
            "vendor": vendor,
            "label": "estimated",
            "percent_used": round(estimate, 4),
            "source": "learned per-job estimate from earlier native attempts",
            "reported": dict(usage or {}),
        }
    return {
        "vendor": vendor,
        "label": "unknown",
        "percent_used": None,
        "source": "host quota not readable",
        "reported": dict(usage or {}),
    }


def record_native_attempt(
    db: Database,
    job_id: str,
    *,
    vendor: object,
    model: object,
    base_sha: object,
    candidate_sha: object,
    worktree: object,
    transcript: object = "",
    started_at: object = None,
    finished_at: object = None,
    usage: Mapping[str, Any] | None = None,
    gate_runner: Callable[[Any, Path], Any] | None = None,
    reviewer: Callable[[Database, str, float], Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Verify one native report, gate and review it, then record the attempt.

    Every mismatch fails closed and leaves the job unfinished; a passing run
    becomes an ordinary completed candidate that only owner acceptance can
    land.
    """
    if not native_enabled():
        raise NativeWorkerError(
            "NATIVE_WORKERS_DISABLED",
            f"native workers are off; set {NATIVE_WORKERS_ENV}=1 to enable them",
        )
    clean_vendor = _clean_vendor(vendor)
    clean_model = _text(model, "model", limit=200)
    base = _require_sha(base_sha, "base_sha")
    candidate = _require_sha(candidate_sha, "candidate_sha")
    worktree_text = _text(worktree, "worktree")
    job, marker = _require_claim(db, job_id)
    if job.state is not JobState.RUNNING:
        raise NativeWorkerError(
            "NATIVE_JOB_NOT_RUNNING",
            f"job {job_id} is {job.state.value}; its result can no longer be recorded",
            data={"job_id": job_id, "state": job.state.value},
        )
    # F17: a report runs the project gate and a fresh reviewer, which is new
    # work.  While the work gate is closed nothing new starts, so the report is
    # refused before any durable mutation and can be retried after `orch
    # resume`; the child's committed worktree is preserved meanwhile.
    _require_work_gate(db, action="record a native attempt")
    if marker.get("vendor") != clean_vendor or marker.get("model") != clean_model:
        raise NativeWorkerError(
            "NATIVE_IDENTITY_MISMATCH",
            "the reported vendor/model is not the one this attempt was claimed with",
        )
    attempt_id = str(marker.get("attempt_id") or f"{job.current_run_id}_a1")
    run_id = str(job.current_run_id)
    run = db.get_run(run_id)
    if run is not None and run.status is RunStatus.COMPLETED:
        raise NativeWorkerError(
            "NATIVE_ATTEMPT_ALREADY_RECORDED", "this native attempt was already recorded"
        )
    repo = Path(job.repo).resolve()
    reported_worktree = Path(worktree_text).resolve()
    _verify_worktree(marker, reported_worktree, candidate)
    _verify_lineage(repo, marker, base, candidate)
    _verify_canonical(repo, marker)
    paths = _changed_paths(repo, base, candidate)
    _verify_diff_paths(paths, reported_worktree)
    transcript_text = str(transcript or "").strip()[:MAX_TRANSCRIPT_CHARS]
    clean_started = _parse_iso(started_at) or _parse_iso(marker.get("claimed_at"))
    clean_finished = _parse_iso(finished_at) or _now(now)
    if run is None or not run.config_json:
        raise NativeWorkerError("NATIVE_RUN_MISSING", "the native run record is missing its policy")
    from saberops.config import reconstruct_run_config

    config = reconstruct_run_config(json.loads(run.config_json))
    if not config.gate_enabled or not str(config.gate_command or "").strip():
        raise NativeWorkerError(
            "NATIVE_GATE_MISSING",
            "a native attempt reaches acceptance only through an enabled project gate",
        )
    # The candidate must exist as a successful attempt before the reviewer can
    # read it (the ordinary engine works on a completed candidate).  A failure
    # below flips this attempt to FAILED, so it can never be accepted.
    db.update_attempt(
        attempt_id,
        AttemptStatus.SUCCESS,
        worker_stdout=transcript_text,
        commit_sha=candidate,
        completed_at=current_iso_timestamp(),
    )
    runner = gate_runner or _run_gate
    try:
        gate = runner(config, reported_worktree)
    except Exception as exc:
        _fail_native(db, attempt_id, run_id, job_id, reason="gate_unavailable", error=str(exc))
        raise NativeWorkerError(
            "NATIVE_GATE_UNAVAILABLE", f"the project gate could not run: {exc}"
        ) from exc
    db.create_check(
        CheckResult(
            id=f"check_{uuid.uuid4().hex[:10]}",
            attempt_id=attempt_id,
            gate_command=config.gate_command,
            exit_code=int(gate.exit_code),
            stdout=str(gate.stdout or ""),
            stderr=str(gate.stderr or ""),
            passed=bool(gate.passed),
            skipped=bool(getattr(gate, "skipped", False)),
            created_at=current_iso_timestamp(),
        )
    )
    from saberops.gate_receipt import record_receipt

    record_receipt(
        db,
        run_id=run_id,
        attempt_id=attempt_id,
        config=config,
        base_sha=base,
        candidate_sha=candidate,
        started_at=_iso(clean_started) if clean_started else current_iso_timestamp(),
        finished_at=_iso(clean_finished),
        passed=bool(gate.passed),
    )
    db.record_event(
        run_id,
        "gate_completed",
        attempt_id=attempt_id,
        payload={"candidate_sha": candidate, "exit_code": int(gate.exit_code)},
    )
    if not gate.passed or bool(getattr(gate, "skipped", False)):
        db.update_attempt(
            attempt_id,
            AttemptStatus.FAILED,
            worker_stdout=transcript_text,
            worker_error="project gate failed",
            completed_at=current_iso_timestamp(),
        )
        db.update_run(
            run_id,
            RunStatus.FAILED,
            current_iso_timestamp(),
            result_summary="native candidate failed the project gate",
        )
        job_store.reconcile_transition(db, job_id, to=JobState.FAILED, failure_reason=RUN_FAILED)
        raise NativeWorkerError(
            "NATIVE_GATE_FAILED",
            "the candidate failed the project gate; it cannot reach acceptance",
            data={"exit_code": int(gate.exit_code), "candidate_sha": candidate},
        )
    review_call = reviewer or _default_reviewer
    try:
        review = review_call(db, run_id, float(config.review_timeout))
    except NativeWorkerError:
        raise
    except Exception as exc:
        _fail_native(db, attempt_id, run_id, job_id, reason="review_unavailable", error=str(exc))
        raise NativeWorkerError(
            "NATIVE_REVIEW_UNAVAILABLE", f"the independent review could not run: {exc}"
        ) from exc
    from saberops.models import ReviewVerdict

    if review.verdict not in (ReviewVerdict.PASS, ReviewVerdict.PASS_WITH_BACKLOG):
        _fail_native(
            db,
            attempt_id,
            run_id,
            job_id,
            reason="review_rejected",
            error=str(review.verdict),
        )
        raise NativeWorkerError(
            "NATIVE_REVIEW_REJECTED",
            "the independent review did not pass; the candidate cannot reach acceptance",
            data={"verdict": str(review.verdict), "review_id": getattr(review, "id", None)},
        )
    if not _review_is_different_family(review, clean_vendor):
        _fail_native(
            db,
            attempt_id,
            run_id,
            job_id,
            reason="review_same_vendor",
            error=f"{review.provider}/{review.model}",
        )
        raise NativeWorkerError(
            "NATIVE_REVIEW_SAME_VENDOR",
            "the review ran on the host's own vendor; an external different-family "
            "reviewer is required",
            data={"reviewer": f"{review.provider}/{review.model}", "vendor": clean_vendor},
        )
    worktree_still_valid = (
        GitManager.is_clean(reported_worktree)
        and GitManager.get_head_commit(reported_worktree) == candidate
    )
    if not worktree_still_valid:
        _fail_native(
            db, attempt_id, run_id, job_id, reason="worktree_changed", error="after review"
        )
        raise NativeWorkerError(
            "NATIVE_WORKTREE_CHANGED", "the worktree changed during review"
        )
    _verify_canonical(repo, marker)
    usage_record = _usage_record(db, clean_vendor, marker, usage=usage)
    db.update_run(
        run_id,
        RunStatus.COMPLETED,
        current_iso_timestamp(),
        final_attempt_id=attempt_id,
        result_summary="native candidate is ready for separate owner acceptance",
    )
    db.record_event(
        run_id,
        NATIVE_ATTEMPT_EVENT,
        attempt_id=attempt_id,
        payload={
            "job_id": job_id,
            "vendor": clean_vendor,
            "model": clean_model,
            "base_sha": base,
            "candidate_sha": candidate,
            "worktree": str(reported_worktree),
            "branch": marker.get("branch"),
            "changed_paths": paths,
            "started_at": _iso(clean_started) if clean_started else None,
            "finished_at": _iso(clean_finished),
            "transcript_chars": len(transcript_text),
            "usage": usage_record,
            "review": {
                "id": getattr(review, "id", None),
                "provider": review.provider,
                "model": review.model,
                "verdict": str(review.verdict),
            },
            "supervision": "host",
            "quota": "estimated" if usage_record["label"] != "unknown" else "unknown",
        },
    )
    job_store.reconcile_transition(db, job_id, to=JobState.SUCCEEDED)
    return {
        "job_id": job_id,
        "run_id": run_id,
        "attempt_id": attempt_id,
        "state": "SUCCEEDED",
        "candidate_sha": candidate,
        "base_sha": base,
        "worktree": str(reported_worktree),
        "branch": marker.get("branch"),
        "supervision": "host",
        "quota": usage_record["label"],
        "usage": usage_record,
        "review": {
            "provider": review.provider,
            "model": review.model,
            "verdict": str(review.verdict),
        },
        "owner_action": f"orch accept {run_id}",
        "guidance": (
            "The candidate is gated and independently reviewed. Report the run id and the "
            "exact owner action; only the owner accepts. Never claim acceptance, push or merge."
        ),
    }


def _fail_native(
    db: Database,
    attempt_id: str,
    run_id: str,
    job_id: str,
    *,
    reason: str,
    error: str,
) -> None:
    """Best-effort fail-closed bookkeeping for a rejected native report."""
    try:
        db.update_attempt(
            attempt_id,
            AttemptStatus.FAILED,
            worker_error=f"{reason}: {error}",
            completed_at=current_iso_timestamp(),
        )
    except Exception:
        pass
    try:
        db.update_run(
            run_id,
            RunStatus.FAILED,
            current_iso_timestamp(),
            result_summary=f"native attempt rejected ({reason})",
        )
    except Exception:
        pass
    try:
        db.record_event(
            run_id,
            "native_attempt_rejected",
            attempt_id=attempt_id,
            payload={"reason": reason, "error": error, "job_id": job_id},
        )
    except Exception:
        pass
    try:
        job_store.reconcile_transition(db, job_id, to=JobState.FAILED, failure_reason=RUN_FAILED)
    except Exception:
        pass
