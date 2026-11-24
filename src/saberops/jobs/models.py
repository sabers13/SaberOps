"""Job queue types.

Contracts:

* ``size_hint`` literal values are exactly ``"S"``, ``"M"``,
  ``"L"``. This module derives ``SIZE_HINTS`` from the shared
  ``saberops.capacity.cost_model.SizeBucket`` enum.
* One role vocabulary lives here (``JobRole``): ``PLAN``, ``BUILD`` and
  ``CHECK`` (ADR-0005 §3).  Only ``DISPATCHABLE_ROLES = {BUILD}`` is
  dispatchable; ``PLAN`` is the external CLI session and ``CHECK`` is the
  engine's independent reviewer.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from saberops.capacity.cost_model import SizeBucket


class JobRole(StrEnum):
    """The one role vocabulary (ADR-0005 §3). Values equal names.

    * ``PLAN``  — the external CLI session the owner sits in and submits from.
    * ``BUILD`` — the one accountable worker per task, including its own
                  repair retries and any stronger-model start/escalation.
    * ``CHECK`` — the independent reviewer of the built candidate.
    """

    PLAN = "PLAN"
    BUILD = "BUILD"
    CHECK = "CHECK"


#: Legacy persisted role names, mapped onto the three-role vocabulary.  These
#: are accepted when reading durable rows (jobs, routing snapshots, dispatch
#: evidence) and when a caller uses an older name; new writes never emit them.
LEGACY_ROLE_ALIASES: dict[str, JobRole] = {
    "ORCHESTRATOR": JobRole.PLAN,
    "PLANNER": JobRole.PLAN,
    "WORKER": JobRole.BUILD,
    "SURGEON": JobRole.BUILD,
    "INTEGRATOR": JobRole.BUILD,
    "REVIEWER": JobRole.CHECK,
}


def coerce_job_role(value: JobRole | str) -> JobRole:
    """Return the three-role ``JobRole`` for ``value``, accepting legacy names."""
    if isinstance(value, JobRole):
        return value
    cleaned = str(value).strip().upper()
    try:
        return JobRole(cleaned)
    except ValueError:
        legacy = LEGACY_ROLE_ALIASES.get(cleaned)
        if legacy is not None:
            return legacy
        known = ", ".join(r.value for r in JobRole)
        raise ValueError(f"role must be one of {known}; got {value!r}") from None


DISPATCHABLE_ROLES: frozenset[JobRole] = frozenset({JobRole.BUILD})


class JobKind(StrEnum):
    """What a job delivers. Roles stay PLAN / BUILD / CHECK.

    * ``BUILD`` — the ordinary job: a checked, reviewable candidate the owner
      can accept.
    * ``SCOUT`` — a read-only investigation whose deliverable is a markdown
      report, never a commit.  A scout is a *kind* of BUILD job (same role,
      same route, same admission), not a fourth role.
    """

    BUILD = "BUILD"
    SCOUT = "SCOUT"


def coerce_job_kind(value: JobKind | str) -> JobKind:
    """Return the canonical :class:`JobKind` for ``value``."""
    if isinstance(value, JobKind):
        return value
    try:
        return JobKind(str(value).strip().upper())
    except ValueError as exc:
        known = ", ".join(k.value for k in JobKind)
        raise ValueError(f"kind must be one of {known}; got {value!r}") from exc


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
    #: The route's pool needs quota evidence the engine will not collect on
    #: its own (an interactive usage reader such as Claude).  Only the owner can
    #: supply it; guidance names the exact ``orch quota set`` command.  Unlike
    #: ``PROBE_PENDING`` no tick can ever clear it by probing.
    QUOTA_EVIDENCE_REQUIRED = "QUOTA_EVIDENCE_REQUIRED"
    PREEMPTED = "PREEMPTED"
    CONCURRENCY = "CONCURRENCY"
    MEMORY_LOW = "MEMORY_LOW"
    #: The owner mode does not permit autonomous dispatch here.  ``away``
    #: never exceeds the authority preset, so a preset without ``auto_start``
    #: parks the job instead of launching it.  Clears by itself when the mode
    #: or the preset changes.
    MODE_RESTRICTED = "MODE_RESTRICTED"
    #: At least one job named in ``Job.depends_on`` has not been accepted
    #: yet, so this job must not start.  Clears by itself once the dependency
    #: is accepted.
    DEPENDENCY = "DEPENDENCY"
    #: The owner or planner redirected a running attempt.  Its work is
    #: anchored as a checkpoint and the job waits for the next dispatch, which
    #: restores that work and carries the steering note in its prompt.  Unlike
    #: ``PREEMPTED`` this is a deliberate correction and never spends the
    #: preemption budget.
    REDIRECTED = "REDIRECTED"
    #: A dependency ended FAILED or CANCELLED, so this job can never start.
    #: It never clears by itself; the owner cancels or resubmits it.
    DEPENDENCY_FAILED = "DEPENDENCY_FAILED"
    #: Away mode parked this job on its own branch.  A never-delegable or
    #: unhandled decision waits for the owner; independent jobs keep running.
    #: Clears by itself when the blocking question is answered or the away
    #: preflight/circuit breaker recovers.
    AWAY_PARKED = "AWAY_PARKED"
    #: A scheduler died between the dispatch compare-and-swap and its run row.
    #: Nothing ran, so the job is requeued once with this reason (and failed
    #: with the same-named failure reason if it happens again).
    DISPATCH_WINDOW_LOST = "DISPATCH_WINDOW_LOST"
    #: The owner gate closed (owner pause, no planning session, or an away
    #: session that ended) while this job was running.  The engine stopped the
    #: worker, saved a verified checkpoint and parked the job here.  It clears
    #: only when the owner or the returning planning session confirms a resume;
    #: no autonomous path dispatches a PAUSED job.
    PAUSED = "PAUSED"


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
    xhigh = "xhigh"


class Difficulty(StrEnum):
    """Planner-recorded task difficulty.

    The planner understands the task; the engine maps the recorded value onto
    a route.  These are judgements, never provider, model, tier or route
    names, and a caller cannot spell one of those here (the MCP denylist still
    refuses them).  Missing means :attr:`ROUTINE`: the ordinary build chain.
    """

    ROUTINE = "routine"
    HARD = "hard"


DIFFICULTIES: tuple[str, ...] = tuple(d.value for d in Difficulty)


def coerce_difficulty(value: Difficulty | str | None) -> Difficulty:
    """Return the canonical :class:`Difficulty`; missing means routine."""
    if value is None:
        return Difficulty.ROUTINE
    if isinstance(value, Difficulty):
        return value
    try:
        return Difficulty(str(value).strip().lower())
    except ValueError as exc:
        known = ", ".join(d.value for d in Difficulty)
        raise ValueError(f"difficulty must be one of {known}; got {value!r}") from exc


NO_ROUTE_FOR_REQUIREMENTS = "NO_ROUTE_FOR_REQUIREMENTS"
RUN_FAILED = "RUN_FAILED"
RUN_MISSING = "RUN_MISSING"
CORRUPT_JOB_ROW = "CORRUPT_JOB_ROW"
# The owning job exhausted its bounded preemption budget
# (default 5).  Recorded on the JOB as the terminal failure reason when the
# limit is reached instead of a silent 6th preemption.
PREEMPTION_LIMIT = "PREEMPTION_LIMIT"
#: A RUNNING job was dispatched by a scheduler that died before it created the
#: run row.  The job holds a reserved capacity row but no run ever started; the
#: scheduler requeues it once under :attr:`WaitingReason.DISPATCH_WINDOW_LOST`
#: and records this as the terminal failure reason if it loses a second
#: dispatch the same way.
DISPATCH_WINDOW_LOST = "DISPATCH_WINDOW_LOST"

FAILURE_REASONS: frozenset[str] = frozenset(
    {
        NO_ROUTE_FOR_REQUIREMENTS,
        RUN_FAILED,
        RUN_MISSING,
        PREEMPTION_LIMIT,
        CORRUPT_JOB_ROW,
        DISPATCH_WINDOW_LOST,
    }
)


def _coerce_role(value: JobRole | str) -> JobRole:
    return coerce_job_role(value)


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


def read_only_requirements(requirements: JobRequirements) -> JobRequirements:
    """Return ``requirements`` with the read-only capability enforced.

    A scout's read-only nature is a property of the job kind, so callers cannot
    submit a writing scout by omitting the flag.  The role stays BUILD: a
    scout is a kind on the build route, never a second role.
    """
    from dataclasses import replace

    from saberops.control_plane.capabilities import Capability

    if requirements.role is not JobRole.BUILD:
        raise ValueError(
            "a scout job uses the BUILD role; scout is a job kind, not a role"
        )
    capabilities = tuple(
        sorted({*requirements.required_capabilities, Capability.READ_ONLY.value})
    )
    return replace(requirements, read_only=True, required_capabilities=capabilities)


def scout_requirements(requirements: JobRequirements) -> JobRequirements:
    """Return a scout's kind-derived requirements: read-only and ``effort: low``.

    A scout is read-only, and an unstated effort becomes ``low`` so the cheap
    model default is the same ``effort: low`` rule every job uses
    (ADR-0035) instead of a scout-only route.  The role stays BUILD: a scout
    is a kind on the build route, never a second role.
    """
    resolved = read_only_requirements(requirements)
    if resolved.effort is None:
        from dataclasses import replace as _replace

        resolved = _replace(resolved, effort=Effort.low)
    return resolved


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
    """Caller-stated requirements (no ``min_tier``)."""

    role: JobRole = JobRole.BUILD
    effort: Effort | None = None
    required_capabilities: tuple[str, ...] = ()
    read_only: bool = False
    model_family_include: tuple[str, ...] = ()
    model_family_exclude: tuple[str, ...] = ()
    size_hint: str | None = None
    #: Planner-recorded judgement, never a route name. Missing means
    #: routine: the ordinary build chain.
    difficulty: Difficulty = Difficulty.ROUTINE
    #: Planner-authored focus for the independent reviewer.  It can only add
    #: attention; nothing can use it to turn review off.
    review_focus: str | None = None
    #: Planner-recorded judgement: this job's candidate is consequential
    #: (a data migration, deleting data, auth and security, an irreversible
    #: operation, effects outside the work copy), so away and critical-only
    #: park it for the owner instead of auto-accepting.  Missing means
    #: ordinary; the engine's own diff floor applies either way.
    consequential: bool = False

    def __post_init__(self) -> None:
        from saberops.control_plane.capabilities import normalize_required_capabilities

        role = _coerce_role(self.role)
        object.__setattr__(self, "role", role)
        effort = _coerce_effort(self.effort)
        object.__setattr__(self, "effort", effort)
        object.__setattr__(self, "difficulty", coerce_difficulty(self.difficulty))
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
        focus = (
            self.review_focus.strip()
            if isinstance(self.review_focus, str)
            else self.review_focus
        )
        object.__setattr__(self, "review_focus", focus or None)
        if type(self.consequential) is not bool:
            raise ValueError("consequential must be a boolean")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "role": self.role.value,
            "effort": self.effort.value if self.effort is not None else None,
            "required_capabilities": list(self.required_capabilities),
            "read_only": self.read_only,
            "model_family_include": list(self.model_family_include),
            "model_family_exclude": list(self.model_family_exclude),
            "size_hint": self.size_hint,
            "difficulty": self.difficulty.value,
        }
        # Written only when set so a request digest stored before this field
        # existed still matches its idempotency key.
        if self.review_focus:
            payload["review_focus"] = self.review_focus
        if self.consequential:
            payload["consequential"] = True
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> JobRequirements:
        return cls(
            role=_coerce_role(str(data.get("role", JobRole.BUILD.value))),
            effort=_coerce_effort(data.get("effort")),
            required_capabilities=tuple(data.get("required_capabilities", ())),
            read_only=bool(data.get("read_only", False)),
            model_family_include=tuple(data.get("model_family_include", ())),
            model_family_exclude=tuple(data.get("model_family_exclude", ())),
            size_hint=data.get("size_hint"),
            # An older row has no difficulty; missing means routine.
            difficulty=coerce_difficulty(data.get("difficulty")),
            review_focus=data.get("review_focus"),
            consequential=bool(data.get("consequential", False)),
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
class SteeringNote:
    """One durable owner note attached to a job (ADR-0016).

    A note is never typed into a live worker.  It is recorded here and
    included in the prompt of the next worker attempt, which is the only safe
    boundary at which new owner input can reach the worker.  Delivery is
    recorded once, on the attempt that carried the note.
    """

    note_id: int
    job_id: str
    body: str
    created_at: str
    delivered_at: str | None = None
    delivered_run_id: str | None = None
    delivered_attempt_id: str | None = None
    #: Set when the owner or planner deliberately dropped the note without
    #: delivering it.  A discarded note no longer blocks acceptance; it stays
    #: in the record so the decision is visible.
    discarded_at: str | None = None
    #: Who wrote the note: ``owner`` (or the planner on the owner's behalf) or
    #: ``advisor`` (advice recorded before the on-demand advisor moved off the
    #: note channel).  Delivery semantics are identical.
    source: str = "owner"

    @property
    def delivered(self) -> bool:
        return self.delivered_at is not None

    @property
    def discarded(self) -> bool:
        return self.discarded_at is not None

    @property
    def pending(self) -> bool:
        """True when this correction never reached a worker and was not dropped."""
        return not self.delivered and not self.discarded

    def to_dict(self) -> dict[str, Any]:
        return {
            "note_id": self.note_id,
            "job_id": self.job_id,
            "text": self.body,
            "created_at": self.created_at,
            "delivered": self.delivered,
            "delivered_at": self.delivered_at,
            "delivered_run_id": self.delivered_run_id,
            "delivered_attempt_id": self.delivered_attempt_id,
            "discarded": self.discarded,
            "discarded_at": self.discarded_at,
            "pending": self.pending,
            "source": self.source,
        }


@dataclass(frozen=True)
class DependencyBlocker:
    """Why a queued job's dependencies do not let it start (pure contract).

    ``reason`` is the :class:`WaitingReason` the scheduler parks the job with;
    ``blocked_by`` names the dependencies responsible, sorted for determinism so
    the same durable state always produces the same decision.
    """

    reason: WaitingReason
    blocked_by: tuple[str, ...] = ()
    detail: str | None = None


@dataclass(frozen=True)
class Job:
    """One durable job. ``base_sha`` travels beside ``base_ref`` (section H):

    ``RunRequest`` has no field for it today, so the dispatch spec
    carries both and honours them at dispatch.

    ``kind`` is the job kind (BUILD or SCOUT). A scout is the same BUILD
    role with an enforced read-only requirement and a report instead of a
    candidate; it is never a fourth role.
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
    kind: JobKind = JobKind.BUILD
    deadline: str | None = None
    not_before: str | None = None
    depends_on: tuple[str, ...] = ()
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
        kind = coerce_job_kind(self.kind)
        object.__setattr__(self, "kind", kind)
        if kind is JobKind.SCOUT:
            # A scout can never write, whatever the caller asked for, and an
            # unstated effort is the cheap default: both are properties of the
            # job kind, not caller preferences (ADR-0035).
            object.__setattr__(
                self, "requirements", scout_requirements(self.requirements)
            )
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
        depends_on: list[str] = []
        for raw in self.depends_on:
            if not isinstance(raw, str) or not raw.strip():
                raise ValueError("depends_on entries must be non-empty job ids")
            dependency = raw.strip()
            if dependency == self.job_id:
                raise ValueError(f"job {self.job_id} cannot depend on itself")
            if dependency not in depends_on:
                depends_on.append(dependency)
        object.__setattr__(self, "depends_on", tuple(depends_on))

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
            "kind": self.kind.value,
            "deadline": self.deadline,
            "not_before": self.not_before,
            "depends_on": list(self.depends_on),
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
            kind=coerce_job_kind(str(data.get("kind", JobKind.BUILD.value))),
            deadline=data.get("deadline"),
            not_before=data.get("not_before"),
            depends_on=tuple(data.get("depends_on", ())),
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
    "LEGACY_ROLE_ALIASES",
    "coerce_job_kind",
    "read_only_requirements",
    "scout_requirements",
    "coerce_job_role",
    "DISPATCHABLE_ROLES",
    "FAILURE_REASONS",
    "NO_ROUTE_FOR_REQUIREMENTS",
    "PREEMPTION_LIMIT",
    "PRIORITY_ORDER",
    "RUN_FAILED",
    "RUN_MISSING",
    "SIZE_HINTS",
    "TERMINAL_JOB_STATES",
    "DependencyBlocker",
    "Effort",
    "Job",
    "JobKind",
    "JobRequirements",
    "JobRole",
    "JobState",
    "Priority",
    "ResumeFrom",
    "WaitingReason",
]
