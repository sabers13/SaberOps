"""Job queue types (M2-3 section A).

Cross-lane contracts (identical text in the M2-3 and M2-4a prompts):

* C1: ``size_hint`` literal values are exactly ``"S"``, ``"M"``,
  ``"L"``. This module derives ``SIZE_HINTS`` from the shared
  ``saberops.capacity.cost_model.SizeBucket`` enum.
* C3: one role vocabulary lives here (``JobRole``); only
  ``DISPATCHABLE_ROLES = {WORKER}`` is dispatchable until R-002.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from saberops.capacity.cost_model import SizeBucket


class JobRole(StrEnum):
    """One role vocabulary for jobs (C3). Values equal names."""

    ORCHESTRATOR = "ORCHESTRATOR"
    WORKER = "WORKER"
    INTEGRATOR = "INTEGRATOR"
    SURGEON = "SURGEON"
    REVIEWER = "REVIEWER"


DISPATCHABLE_ROLES: frozenset[JobRole] = frozenset({JobRole.WORKER})

SIZE_HINTS: tuple[str, ...] = tuple(b.value for b in SizeBucket)


class JobState(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_JOB_STATES: frozenset[JobState] = frozenset(
    {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED}
)


class WaitingReason(StrEnum):
    NEW = "NEW"
    NO_CAPACITY = "NO_CAPACITY"
    PROBE_PENDING = "PROBE_PENDING"
    PREEMPTED = "PREEMPTED"
    CONCURRENCY = "CONCURRENCY"


class Priority(StrEnum):
    URGENT = "URGENT"
    NORMAL = "NORMAL"
    BACKGROUND = "BACKGROUND"


PRIORITY_ORDER: dict[Priority, int] = {
    Priority.URGENT: 0,
    Priority.NORMAL: 1,
    Priority.BACKGROUND: 2,
}


class Effort(StrEnum):
    low = "low"
    medium = "medium"
    high = "high"


NO_ROUTE_FOR_REQUIREMENTS = "NO_ROUTE_FOR_REQUIREMENTS"
RUN_FAILED = "RUN_FAILED"
RUN_MISSING = "RUN_MISSING"
# ADR-0002 D8 (M2-5): the owning job exhausted its bounded preemption budget
# (default 5).  Recorded on the JOB as the terminal failure reason when the
# limit is reached instead of a silent 6th preemption.
PREEMPTION_LIMIT = "PREEMPTION_LIMIT"

FAILURE_REASONS: frozenset[str] = frozenset(
    {NO_ROUTE_FOR_REQUIREMENTS, RUN_FAILED, RUN_MISSING, PREEMPTION_LIMIT}
)


def _coerce_role(value: JobRole | str) -> JobRole:
    if isinstance(value, JobRole):
        return value
    try:
        return JobRole(str(value))
    except ValueError as exc:
        known = ", ".join(r.value for r in JobRole)
        raise ValueError(f"role must be one of {known}; got {value!r}") from exc


def _coerce_effort(value: Effort | str | None) -> Effort | None:
    if value is None:
        return None
    if isinstance(value, Effort):
        return value
    try:
        return Effort(str(value).strip().lower())
    except ValueError as exc:
        known = ", ".join(e.value for e in Effort)
        raise ValueError(f"effort must be one of {known}; got {value!r}") from exc


def _coerce_priority(value: Priority | str) -> Priority:
    if isinstance(value, Priority):
        return value
    try:
        return Priority(str(value).strip().upper())
    except ValueError as exc:
        known = ", ".join(p.value for p in Priority)
        raise ValueError(f"priority must be one of {known}; got {value!r}") from exc


def _clean_family_list(values: tuple[str, ...] | list[str], *, field: str) -> tuple[str, ...]:
    cleaned: list[str] = []
    for raw in values:
        if not isinstance(raw, str):
            raise ValueError(f"{field} entries must be strings; got {raw!r}")
        entry = raw.strip()
        if not entry:
            raise ValueError(f"{field} entries must be non-empty strings")
        if entry != entry.lower():
            raise ValueError(f"{field} entries must be lowercase; got {raw!r}")
        cleaned.append(entry)
    return tuple(cleaned)


@dataclass(frozen=True)
class JobRequirements:
    """Caller-stated requirements (amended D6/section 11: no ``min_tier``)."""

    role: JobRole = JobRole.WORKER
    effort: Effort | None = None
    required_capabilities: tuple[str, ...] = ()
    read_only: bool = False
    model_family_include: tuple[str, ...] = ()
    model_family_exclude: tuple[str, ...] = ()
    size_hint: str | None = None

    def __post_init__(self) -> None:
        from saberops.control_plane.capabilities import normalize_required_capabilities

        role = _coerce_role(self.role)
        object.__setattr__(self, "role", role)
        effort = _coerce_effort(self.effort)
        object.__setattr__(self, "effort", effort)
        caps = tuple(self.required_capabilities) if self.required_capabilities else ()
        # Unknown capability names are a validation error (ValueError),
        # never a FAILED job.
        normalized = normalize_required_capabilities(caps)
        object.__setattr__(self, "required_capabilities", normalized)
        object.__setattr__(self, "read_only", bool(self.read_only))
        include = _clean_family_list(
            tuple(self.model_family_include) if self.model_family_include else (),
            field="model_family_include",
        )
        exclude = _clean_family_list(
            tuple(self.model_family_exclude) if self.model_family_exclude else (),
            field="model_family_exclude",
        )
        overlap = set(include) & set(exclude)
        if overlap:
            raise ValueError(
                "model_family_include and model_family_exclude must not overlap: "
                f"{sorted(overlap)}"
            )
        object.__setattr__(self, "model_family_include", include)
        object.__setattr__(self, "model_family_exclude", exclude)
        size = self.size_hint
        if size is not None and size not in SIZE_HINTS:
            raise ValueError(f"size_hint must be one of {list(SIZE_HINTS)}; got {size!r}")
        if size is not None:
            object.__setattr__(self, "size_hint", str(size))

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role.value,
            "effort": self.effort.value if self.effort is not None else None,
            "required_capabilities": list(self.required_capabilities),
            "read_only": self.read_only,
            "model_family_include": list(self.model_family_include),
            "model_family_exclude": list(self.model_family_exclude),
            "size_hint": self.size_hint,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> JobRequirements:
        return cls(
            role=_coerce_role(str(data.get("role", JobRole.WORKER.value))),
            effort=_coerce_effort(data.get("effort")),
            required_capabilities=tuple(data.get("required_capabilities", ())),
            read_only=bool(data.get("read_only", False)),
            model_family_include=tuple(data.get("model_family_include", ())),
            model_family_exclude=tuple(data.get("model_family_exclude", ())),
            size_hint=data.get("size_hint"),
        )


@dataclass(frozen=True)
class ResumeFrom:
    run_id: str
    checkpoint_sha: str
    handoff_id: str

    def __post_init__(self) -> None:
        for field in ("run_id", "checkpoint_sha", "handoff_id"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"resume_from.{field} must be a non-empty string")

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "checkpoint_sha": self.checkpoint_sha,
            "handoff_id": self.handoff_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ResumeFrom:
        return cls(
            run_id=str(data["run_id"]),
            checkpoint_sha=str(data["checkpoint_sha"]),
            handoff_id=str(data["handoff_id"]),
        )


@dataclass(frozen=True)
class Job:
    """One durable job. ``base_sha`` travels beside ``base_ref`` (section H):

    ``RunRequest`` has no field for it today, so the dispatch spec
    carries both and M2-4b honours them at dispatch.
    """

    job_id: str
    idempotency_key: str | None
    caller_id: str
    repo: str
    base_ref: str
    base_sha: str
    goal: str
    acceptance: str | None
    requirements: JobRequirements
    priority: Priority = Priority.NORMAL
    deadline: str | None = None
    not_before: str | None = None
    state: JobState = JobState.QUEUED
    waiting_reason: WaitingReason | None = None
    failure_reason: str | None = None
    eta: str | None = None
    eta_confidence: str | None = None
    deadline_at_risk: bool = False
    current_run_id: str | None = None
    run_ids: tuple[str, ...] = ()
    resume_from: ResumeFrom | None = None
    preemptions: int = 0
    created_at: str = ""
    updated_at: str = ""
    request_digest: str = ""

    def __post_init__(self) -> None:
        priority = _coerce_priority(self.priority)
        object.__setattr__(self, "priority", priority)
        if isinstance(self.state, JobState):
            state = self.state
        else:
            state = JobState(str(self.state))
            object.__setattr__(self, "state", state)
        waiting = self.waiting_reason
        if waiting is not None and not isinstance(waiting, WaitingReason):
            waiting = WaitingReason(str(waiting))
            object.__setattr__(self, "waiting_reason", waiting)
        # Waiting reasons exist only while QUEUED.
        if state == JobState.QUEUED:
            if waiting is None:
                raise ValueError("QUEUED jobs must carry a waiting_reason")
        elif waiting is not None:
            raise ValueError(f"{state.value} jobs must not carry a waiting_reason")
        # Failure reasons exist only on FAILED.
        if state == JobState.FAILED:
            if not self.failure_reason:
                raise ValueError("FAILED jobs must carry a failure_reason")
            if self.failure_reason not in FAILURE_REASONS:
                raise ValueError(f"unknown failure_reason: {self.failure_reason!r}")
        elif self.failure_reason is not None:
            raise ValueError(f"{state.value} jobs must not carry a failure_reason")
        # Run linkage: QUEUED never holds a current run; RUNNING always does.
        if state == JobState.QUEUED and self.current_run_id is not None:
            raise ValueError("QUEUED jobs must not carry a current_run_id")
        if state == JobState.RUNNING and not self.current_run_id:
            raise ValueError("RUNNING jobs must carry a current_run_id")
        object.__setattr__(self, "run_ids", tuple(self.run_ids))
        if self.preemptions < 0:
            raise ValueError("preemptions must be >= 0")

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "idempotency_key": self.idempotency_key,
            "caller_id": self.caller_id,
            "repo": self.repo,
            "base_ref": self.base_ref,
            "base_sha": self.base_sha,
            "goal": self.goal,
            "acceptance": self.acceptance,
            "requirements": self.requirements.to_dict(),
            "priority": self.priority.value,
            "deadline": self.deadline,
            "not_before": self.not_before,
            "state": self.state.value,
            "waiting_reason": self.waiting_reason.value if self.waiting_reason else None,
            "failure_reason": self.failure_reason,
            "eta": self.eta,
            "eta_confidence": self.eta_confidence,
            "deadline_at_risk": self.deadline_at_risk,
            "current_run_id": self.current_run_id,
            "run_ids": list(self.run_ids),
            "resume_from": self.resume_from.to_dict() if self.resume_from else None,
            "preemptions": self.preemptions,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "request_digest": self.request_digest,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Job:
        waiting_raw = data.get("waiting_reason")
        resume_raw = data.get("resume_from")
        return cls(
            job_id=str(data["job_id"]),
            idempotency_key=data.get("idempotency_key"),
            caller_id=str(data["caller_id"]),
            repo=str(data["repo"]),
            base_ref=str(data["base_ref"]),
            base_sha=str(data["base_sha"]),
            goal=str(data["goal"]),
            acceptance=data.get("acceptance"),
            requirements=JobRequirements.from_dict(dict(data["requirements"])),
            priority=_coerce_priority(str(data.get("priority", Priority.NORMAL.value))),
            deadline=data.get("deadline"),
            not_before=data.get("not_before"),
            state=JobState(str(data["state"])),
            waiting_reason=WaitingReason(str(waiting_raw)) if waiting_raw else None,
            failure_reason=data.get("failure_reason"),
            eta=data.get("eta"),
            eta_confidence=data.get("eta_confidence"),
            deadline_at_risk=bool(data.get("deadline_at_risk", False)),
            current_run_id=data.get("current_run_id"),
            run_ids=tuple(data.get("run_ids", ())),
            resume_from=ResumeFrom.from_dict(dict(resume_raw)) if resume_raw else None,
            preemptions=int(data.get("preemptions", 0)),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
            request_digest=str(data.get("request_digest", "")),
        )


__all__ = [
    "DISPATCHABLE_ROLES",
    "FAILURE_REASONS",
    "NO_ROUTE_FOR_REQUIREMENTS",
    "PREEMPTION_LIMIT",
    "PRIORITY_ORDER",
    "RUN_FAILED",
    "RUN_MISSING",
    "SIZE_HINTS",
    "TERMINAL_JOB_STATES",
    "Effort",
    "Job",
    "JobRequirements",
    "JobRole",
    "JobState",
    "Priority",
    "ResumeFrom",
    "WaitingReason",
]
