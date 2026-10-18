"""The Orchestrator contract tools exposed over MCP (ADR-0004 D7, M3).

Twelve tools are exposed, exactly the D7 set::

    submit_job  get_job  list_jobs  cancel_job  capacity
    submit_wave wave_status read_evidence
    list_questions answer_question propose_config_change list_proposals

Everything else is either not modelled or on the **never-exposed** list, which
this module pins as data (:data:`FORBIDDEN_OPERATIONS`) and the registry
refuses before any handler runs:

* approving config proposals (owner-only),
* moving ``main`` (``git merge --ff-only``),
* pushing to a remote,
* quarantine approval,
* billing / provider posture attestation,
* direct gate execution (``scripts/gate-full.sh`` and friends),
* direct edits to tracked repository source files,
* arbitrary deletions outside ``.orch``/``.saberops``,
* reading credentials or secret-reference values.

Each :class:`ToolSpec` carries a JSON schema; :meth:`ToolRegistry.call`
validates arguments against it fail-closed, then applies the single-writer
lease rule: a mutating tool is refused with :class:`LeaseReadOnlyError` while
this client is not the writer.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from saberops.mcp.errors import (
    ForbiddenOperationError,
    InvalidParamsError,
    LeaseReadOnlyError,
    NotFoundError,
    ToolError,
)
from saberops.mcp.evidence import EvidenceRoots
from saberops.mcp.lease import OrchestratorLease
from saberops.mcp.ledger import SNAPSHOT_SCHEMA_VERSION, EventLedger
from saberops.mcp.schema import validate_instance

__all__ = [
    "FORBIDDEN_OPERATIONS",
    "MUTATING_TOOLS",
    "READ_TOOLS",
    "TOOL_NAMES",
    "ServerContext",
    "ToolRegistry",
    "ToolSpec",
    "build_default_registry",
]

#: Operations ADR-0004 D7 permanently refuses over MCP, with the invariant at
#: stake.  Aliases are listed so a client cannot reach a prohibited authority
#: through a synonym.
FORBIDDEN_OPERATIONS: dict[str, str] = {
    "approve_config_proposal": "config approval is owner-only (ADR-0004 D7/D8)",
    "approve_proposal": "config approval is owner-only (ADR-0004 D7/D8)",
    "config_approve": "config approval is owner-only (ADR-0004 D7/D8)",
    "record_config_decision": "config approval is owner-only (ADR-0004 D7/D8)",
    "merge_main": "moving main is owner-only (ADR-0004 D7)",
    "move_main": "moving main is owner-only (ADR-0004 D7)",
    "fast_forward_main": "moving main is owner-only (ADR-0004 D7)",
    "merge_ff_only": "moving main is owner-only (ADR-0004 D7)",
    "push": "pushing is owner-only (ADR-0004 D7)",
    "git_push": "pushing is owner-only (ADR-0004 D7)",
    "push_remote": "pushing is owner-only (ADR-0004 D7)",
    "approve_quarantine": "quarantine approval is owner-only (ADR-0004 D7)",
    "quarantine_approve": "quarantine approval is owner-only (ADR-0004 D7)",
    "attest_posture": "posture attestation is owner-only (ADR-0004 D7)",
    "posture_attest": "posture attestation is owner-only (ADR-0004 D7)",
    "run_gate": "gate execution is engine/owner-only (ADR-0004 D7)",
    "gate_run": "gate execution is engine/owner-only (ADR-0004 D7)",
    "gate_full": "gate execution is engine/owner-only (ADR-0004 D7)",
    "run_full_gate": "gate execution is engine/owner-only (ADR-0004 D7)",
    "edit_source": "no direct tracked-source edits over MCP (ADR-0004 D7)",
    "write_file": "no direct tracked-source edits over MCP (ADR-0004 D7)",
    "apply_patch": "no direct tracked-source edits over MCP (ADR-0004 D7)",
    "delete_path": "deletions outside .orch are owner-only (ADR-0004 D7)",
    "delete_outside_orch": "deletions outside .orch are owner-only (ADR-0004 D7)",
    "remove_path": "deletions outside .orch are owner-only (ADR-0004 D7)",
    "read_credentials": "read tools never return credentials (ADR-0004 D7)",
    "get_credentials": "read tools never return credentials (ADR-0004 D7)",
    "read_secret": "read tools never return credentials (ADR-0004 D7)",
    "read_token": "read tools never return credentials (ADR-0004 D7)",
}

#: Tools that mutate state; these require the single-writer lease.
MUTATING_TOOLS: frozenset[str] = frozenset(
    {
        "submit_job",
        "cancel_job",
        "submit_wave",
        "answer_question",
        "propose_config_change",
    }
)

#: Tools that only read; these stay available to a read-only client.
READ_TOOLS: frozenset[str] = frozenset(
    {
        "get_job",
        "list_jobs",
        "capacity",
        "wave_status",
        "read_evidence",
        "list_questions",
        "list_proposals",
    }
)

TOOL_NAMES: tuple[str, ...] = (
    "submit_job",
    "get_job",
    "list_jobs",
    "cancel_job",
    "capacity",
    "submit_wave",
    "wave_status",
    "read_evidence",
    "list_questions",
    "answer_question",
    "propose_config_change",
    "list_proposals",
)

_JOB_STATES = ["QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"]
_PRIORITIES = ["URGENT", "NORMAL", "BACKGROUND"]
_JOB_ROLES = ["ORCHESTRATOR", "WORKER", "INTEGRATOR", "SURGEON", "REVIEWER"]
_EFFORTS = ["low", "medium", "high"]
_QUESTION_STATUSES = ["OPEN", "DEFERRED", "ANSWERED", "MOOT"]
_ANSWER_AUTHORITIES = ["OWNER", "ORCHESTRATOR"]
_ANSWER_ROLES = ["OWNER", "ORCHESTRATOR"]

#: Field names that would let a caller choose a billing/model route
#: (ADR-0001 invariant 3: callers state requirements only).
_REQUIREMENT_FIELD_DENYLIST: tuple[str, ...] = (
    "provider",
    "model",
    "tier",
    "billing",
    "route",
    "quota",
    "account",
)


def _schema(
    properties: dict[str, Any],
    *,
    required: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(required),
        "properties": properties,
    }


@dataclass
class ServerContext:
    """Resolved collaborators one server instance operates on."""

    db_path: Path | None = None
    questions_store: Path | None = None
    config_ledger_path: Path | None = None
    evidence: EvidenceRoots = field(default_factory=EvidenceRoots.discover)
    ledger: EventLedger = field(default_factory=EventLedger)
    lease: OrchestratorLease = field(default_factory=OrchestratorLease)

    def control_ledger(self) -> Any:
        """Return the config-ledger handle this context reads and writes."""
        from saberops.config import ConfigLedger, config_ledger_path

        if self.config_ledger_path is not None:
            return ConfigLedger(self.config_ledger_path)
        from saberops.paths import saberops_state_dir

        return ConfigLedger(config_ledger_path(saberops_state_dir()))

    def question_queue(self) -> Any:
        """Return the owner question queue this context reads and writes."""
        from saberops.questions import OwnerQuestionQueue

        return OwnerQuestionQueue(self.questions_store)

    def database(self, *, read_only: bool) -> Any | None:
        """Open the owner database, or ``None`` for a missing read-only store.

        A read tool over an absent database returns an empty result instead of
        creating a file; a mutating tool is free to create it.
        """
        from saberops.db import Database, get_default_db_path

        path = Path(self.db_path) if self.db_path is not None else get_default_db_path()
        if not path.exists():
            if read_only:
                return None
            return Database(path, read_only=False)
        return Database(path, read_only=read_only)


@dataclass(frozen=True)
class ToolSpec:
    """One exposed tool: name, JSON schema, mutability and handler."""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[Mapping[str, Any]], dict[str, Any]]
    mutating: bool

    def descriptor(self) -> dict[str, Any]:
        """Render the MCP ``tools/list`` descriptor for this tool."""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


class ToolRegistry:
    """Name -> :class:`ToolSpec` registry with schema and lease enforcement."""

    def __init__(self, *, lease: OrchestratorLease | None = None) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._lease = lease

    def register(self, spec: ToolSpec) -> None:
        if spec.name in FORBIDDEN_OPERATIONS:
            raise ToolError(
                f"refusing to register the never-exposed operation {spec.name!r}"
            )
        if spec.name in self._tools:
            raise ToolError(f"tool already registered: {spec.name!r}")
        self._tools[spec.name] = spec

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def descriptors(self) -> list[dict[str, Any]]:
        """Exposed tool descriptors, ordered by name (deterministic)."""
        return [self._tools[name].descriptor() for name in self.names()]

    def call(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Validate and dispatch one tool call.

        Raises :class:`ForbiddenOperationError` for the never-exposed list,
        :class:`InvalidParamsError` for schema violations,
        :class:`LeaseReadOnlyError` for a mutating call without the writer
        lease, and :class:`ToolError` for handler failures.
        """
        if name in FORBIDDEN_OPERATIONS:
            raise ForbiddenOperationError(
                f"{name!r} is never exposed over MCP: {FORBIDDEN_OPERATIONS[name]}",
                data={"operation": name, "invariant": FORBIDDEN_OPERATIONS[name]},
            )
        spec = self._tools.get(name)
        if spec is None:
            raise ToolError(
                f"unknown tool: {name!r}",
                data={"tool": name, "available": list(self.names())},
            )
        if not isinstance(arguments, Mapping):
            raise InvalidParamsError(f"{name}: arguments must be a JSON object")
        if spec.mutating and self._lease is not None and not self._lease.writer:
            raise LeaseReadOnlyError(
                f"{name!r} is a mutating tool and this client holds only the "
                "read-only lease; another Orchestrator is the writer",
                data={"tool": name, "operation": "mutating", "lease_writer": False},
            )
        errors = validate_instance(spec.input_schema, dict(arguments))
        if errors:
            raise InvalidParamsError(
                f"{name}: invalid arguments", data={"errors": errors}
            )
        return spec.handler(arguments)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _require_text(arguments: Mapping[str, Any], key: str, *, what: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InvalidParamsError(f"{what} is required and must be non-empty text")
    return value.strip()


def _deny_requirement_fields(arguments: Mapping[str, Any]) -> None:
    """Refuse provider/model/tier/billing fields anywhere in the arguments."""
    found: set[str] = set()

    def scan(payload: Mapping[str, Any]) -> None:
        for name in payload:
            if str(name).strip().lower() in _REQUIREMENT_FIELD_DENYLIST:
                found.add(str(name))

    scan(arguments)
    requirements = arguments.get("requirements")
    if isinstance(requirements, Mapping):
        scan(requirements)
    if found:
        raise InvalidParamsError(
            "callers state requirements only; provider/model/tier/billing fields "
            f"are not accepted: {sorted(found)}",
            data={"rejected_fields": sorted(found)},
        )


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #


def _submit_job(context: ServerContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    from saberops.jobs.models import Effort, JobRequirements, JobRole, Priority
    from saberops.jobs.service import IdempotencyConflictError, submit_job

    _deny_requirement_fields(arguments)
    raw_requirements = arguments.get("requirements")
    requirements = JobRequirements()
    if isinstance(raw_requirements, Mapping):
        try:
            requirements = JobRequirements(
                role=JobRole(str(raw_requirements.get("role", JobRole.WORKER.value))),
                effort=(
                    Effort(str(raw_requirements["effort"]).lower())
                    if raw_requirements.get("effort")
                    else None
                ),
                required_capabilities=tuple(
                    str(item) for item in raw_requirements.get("required_capabilities", ())
                ),
                read_only=bool(raw_requirements.get("read_only", False)),
                model_family_include=tuple(
                    str(item) for item in raw_requirements.get("model_family_include", ())
                ),
                model_family_exclude=tuple(
                    str(item) for item in raw_requirements.get("model_family_exclude", ())
                ),
                size_hint=(
                    str(raw_requirements["size_hint"])
                    if raw_requirements.get("size_hint")
                    else None
                ),
            )
        except ValueError as exc:
            raise InvalidParamsError(f"invalid requirements: {exc}") from exc
    db = context.database(read_only=False)
    try:
        result = submit_job(
            db,
            repo=_require_text(arguments, "repo", what="repo"),
            goal=_require_text(arguments, "goal", what="goal"),
            acceptance=arguments.get("acceptance"),
            base_ref=str(arguments.get("base_ref") or "HEAD"),
            requirements=requirements,
            priority=Priority(str(arguments.get("priority") or Priority.NORMAL.value)),
            deadline=arguments.get("deadline"),
            not_before=arguments.get("not_before"),
            idempotency_key=arguments.get("idempotency_key"),
            caller_id=str(arguments.get("caller_id") or "orchestrator"),
        )
    except IdempotencyConflictError as exc:
        raise ToolError(f"idempotency conflict: {exc}") from exc
    except ValueError as exc:
        raise InvalidParamsError(f"invalid job: {exc}") from exc
    payload = {
        "v": SNAPSHOT_SCHEMA_VERSION,
        "job_id": result.job.job_id,
        "state": result.job.state.value,
        "created": result.created,
        "job": result.job.to_dict(),
    }
    context.ledger.append(
        "job_submitted",
        {"job_id": result.job.job_id, "state": result.job.state.value},
        key=f"job:{result.job.job_id}",
    )
    return payload


def _get_job(context: ServerContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    from saberops.jobs.service import get_job

    job_id = _require_text(arguments, "job_id", what="job_id")
    db = context.database(read_only=True)
    job = None if db is None else get_job(db, job_id)
    if job is None:
        raise NotFoundError(f"unknown job: {job_id}", data={"job_id": job_id})
    return {"v": SNAPSHOT_SCHEMA_VERSION, "job": job.to_dict()}


def _list_jobs(context: ServerContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    from saberops.jobs.service import list_jobs

    limit = int(arguments.get("limit", 50))
    state = arguments.get("state")
    db = context.database(read_only=True)
    jobs = [] if db is None else list_jobs(db, state=state, limit=limit)
    return {
        "v": SNAPSHOT_SCHEMA_VERSION,
        "count": len(jobs),
        "jobs": [job.to_dict() for job in jobs],
    }


def _run_canceller(run_id: str) -> bool:
    from saberops.db import Database, get_default_db_path
    from saberops.runtime import db_path_for_run
    from saberops.supervisor import RunSupervisor

    resolved = db_path_for_run(run_id)
    if resolved is None:
        db = Database(get_default_db_path())
    else:
        db = Database(resolved, owner_db=Database(get_default_db_path()))
    return bool(RunSupervisor(db=db).cancel(run_id))


def _cancel_job(context: ServerContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    from saberops.jobs.service import JobNotFoundError, cancel_job

    job_id = _require_text(arguments, "job_id", what="job_id")
    db = context.database(read_only=False)
    try:
        result = cancel_job(db, job_id, run_canceller=_run_canceller)
    except JobNotFoundError as exc:
        raise NotFoundError(f"unknown job: {job_id}", data={"job_id": job_id}) from exc
    except ValueError as exc:
        raise InvalidParamsError(f"invalid job cancel: {exc}") from exc
    payload = {
        "v": SNAPSHOT_SCHEMA_VERSION,
        "job_id": result.job.job_id,
        "state": result.job.state.value,
        "cancelled": result.cancelled,
        "reason": arguments.get("reason"),
        "job": result.job.to_dict(),
    }
    context.ledger.append(
        "job_cancelled",
        {
            "job_id": result.job.job_id,
            "cancelled": result.cancelled,
            "state": result.job.state.value,
        },
    )
    return payload


def _capacity(context: ServerContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    from saberops.jobs.service import capacity

    db = context.database(read_only=True)
    if db is None:
        return {
            "v": SNAPSHOT_SCHEMA_VERSION,
            "capacity": {"generated_at": _now_iso(), "ledger_wired": False, "pools": []},
        }
    return {"v": SNAPSHOT_SCHEMA_VERSION, "capacity": asdict(capacity(db))}


def _lane_overlap_reasons(wave: Any) -> list[str]:
    from saberops.wave_admission import lane_touch_overlaps, scopes_overlap

    reasons: list[str] = []
    lanes = list(wave.lanes)
    for index, left in enumerate(lanes):
        for right in lanes[index + 1 :]:
            for a, b in lane_touch_overlaps(left, right):
                reasons.append(f"OVERLAPPING_TOUCHES:{left.lane_id}:{right.lane_id}:{a}:{b}")
    for lane in lanes:
        if scopes_overlap(lane.touches, lane.must_not_touch):
            reasons.append(f"LANE_SELF_OVERLAP:{lane.lane_id}")
    return reasons


def _submit_wave(context: ServerContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    from saberops.wave_models import Wave, WaveModelError

    raw_wave = arguments.get("wave")
    if not isinstance(raw_wave, Mapping):
        raise InvalidParamsError("wave must be an object")
    try:
        wave = Wave.from_dict(dict(raw_wave))
    except WaveModelError as exc:
        raise InvalidParamsError(f"invalid wave: {exc}") from exc
    reasons = _lane_overlap_reasons(wave)
    if reasons:
        raise ToolError(
            "wave refused at submit: overlapping lane scopes are never admitted",
            data={"wave_id": wave.wave_id, "reasons": reasons},
        )
    # Deferred to the engine: live route-posture and Git ancestry admission
    # need the repository and the current posture snapshot, which are not part
    # of a pure wave description.  Recording the validated submission is the
    # MCP surface's whole authority here; nothing is spawned and no ref moves.
    context.ledger.append(
        "wave_submitted",
        {"wave_id": wave.wave_id, "wave": wave.as_dict()},
        key=f"wave:{wave.wave_id}",
    )
    return {
        "v": SNAPSHOT_SCHEMA_VERSION,
        "wave_id": wave.wave_id,
        "admitted": True,
        "state": "PLANNED",
        "wave": wave.as_dict(),
        "reasons": [],
    }


def _wave_status(context: ServerContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    from saberops.wave_models import LaneState, Wave, WaveModelError, WaveState

    raw_wave = arguments.get("wave")
    wave_id = arguments.get("wave_id")
    if isinstance(raw_wave, Mapping):
        try:
            wave = Wave.from_dict(dict(raw_wave))
        except WaveModelError as exc:
            raise InvalidParamsError(f"invalid wave: {exc}") from exc
        submitted = None
    elif isinstance(wave_id, str) and wave_id.strip():
        submitted = context.ledger.replay().waves.get(wave_id.strip())
        if submitted is None:
            raise NotFoundError(
                f"unknown wave: {wave_id}", data={"wave_id": wave_id}
            )
        try:
            wave = Wave.from_dict(submitted.get("wave"))
        except WaveModelError as exc:  # pragma: no cover - ledger holds validated waves
            raise ToolError(f"stored wave is unreadable: {exc}") from exc
    else:
        raise InvalidParamsError("provide either wave or wave_id")

    from saberops.wave_executor import LANE_STATE_TO_SLOT_STATE

    planned = LaneState.PLANNED
    slot = LANE_STATE_TO_SLOT_STATE[planned]
    lanes = [
        {
            "lane_id": lane.lane_id,
            "task_id": lane.task_id,
            "state": planned.value,
            "slot_state": slot.value,
            "branch": f"orch/{wave.wave_id}/{lane.lane_id}",
            "worktree": None,
            "head_sha": None,
            "fix_rounds": 0,
        }
        for lane in wave.lanes
    ]
    return {
        "v": SNAPSHOT_SCHEMA_VERSION,
        "wave_id": wave.wave_id,
        "state": WaveState.PLANNED.value,
        "terminal": False,
        "lanes": lanes,
        "merge_order": list(wave.merge_order),
        "named_glue": list(wave.named_glue),
        "barriers": {
            "merge_order": list(wave.merge_order),
            "g3_budget": wave.budget("g3_runs", 1),
            "fix_round_budget": wave.budget("fix_rounds", 2),
            "lanes_accepted": False,
        },
        "slot_states": {slot.value: 0},
    }


def _read_evidence(context: ServerContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    requested = _require_text(arguments, "path", what="path")
    resolved, text, truncated = context.evidence.read(requested)
    return {
        "v": SNAPSHOT_SCHEMA_VERSION,
        "name": resolved.name,
        "path": str(resolved),
        "content": text,
        "truncated": truncated,
    }


def _list_questions(context: ServerContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    from saberops.questions import QuestionError, question_view

    queue = context.question_queue()
    try:
        questions = queue.questions(
            status=arguments.get("status"),
            run_id=arguments.get("run_id"),
        )
    except QuestionError as exc:
        raise InvalidParamsError(f"invalid question query: {exc}") from exc
    return {
        "v": SNAPSHOT_SCHEMA_VERSION,
        "count": len(questions),
        "questions": [question_view(question) for question in questions],
    }


def _answer_question(
    context: ServerContext, arguments: Mapping[str, Any]
) -> dict[str, Any]:
    from saberops.questions import (
        AnswerAuthority,
        AskRole,
        QuestionError,
        is_orchestrator_answerable,
        question_view,
    )

    ref = _require_text(arguments, "question_id_or_key", what="question_id_or_key")
    words = _require_text(arguments, "answer_text", what="answer_text")
    authority = AnswerAuthority(str(arguments.get("authority") or "").strip().upper())
    if authority is AnswerAuthority.OWNER:
        raise ForbiddenOperationError(
            "owner authority is not exercisable over MCP; the owner answers "
            "through the CLI or the local web surface (ADR-0004 D7)",
            data={"operation": "answer_question", "authority": "OWNER"},
        )
    queue = context.question_queue()
    question = queue.get(ref)
    if question is None:
        raise NotFoundError(f"unknown question: {ref}", data={"ref": ref})
    if not is_orchestrator_answerable(question.request.decision_class):
        raise ForbiddenOperationError(
            f"decision class {question.request.decision_class.value} is owner-only; "
            "the Orchestrator may not answer it over MCP",
            data={
                "ref": ref,
                "decision_class": question.request.decision_class.value,
            },
        )
    answered_by = str(arguments.get("answered_by") or "orchestrator")
    role = AskRole(str(arguments.get("answered_by_role") or AskRole.ORCHESTRATOR.value))
    try:
        updated = queue.answer(
            ref,
            words=words,
            authority=authority,
            answered_by=answered_by,
            answered_by_role=role,
            chosen_option=arguments.get("chosen_option"),
            evidence=arguments.get("evidence"),
            rejected_alternative=arguments.get("rejected_alternative"),
        )
    except QuestionError as exc:
        raise ToolError(str(exc)) from exc
    context.ledger.append(
        "question_answered",
        {
            "question_id": updated.question_id,
            "authority": authority.value,
            "status": updated.status.value,
            "words": words,
            "chosen_option": arguments.get("chosen_option"),
        },
    )
    return {"v": SNAPSHOT_SCHEMA_VERSION, "question": question_view(updated)}


def _propose_config_change(
    context: ServerContext, arguments: Mapping[str, Any]
) -> dict[str, Any]:
    from saberops.config import (
        ConfigError,
        current_workflow_settings,
        propose_config_change,
    )

    changes = arguments.get("changes")
    if not isinstance(changes, Mapping):
        raise InvalidParamsError("changes must be an object of workflow settings")
    rationale = _require_text(arguments, "rationale", what="rationale")
    proposed_by = _require_text(arguments, "proposed_by", what="proposed_by")
    ledger = context.control_ledger()
    try:
        base = current_workflow_settings(ledger)
        proposal = propose_config_change(
            base, changes, rationale=rationale, proposed_by=proposed_by
        )
    except ConfigError as exc:
        raise InvalidParamsError(f"invalid config proposal: {exc}") from exc
    payload = {
        "v": SNAPSHOT_SCHEMA_VERSION,
        "proposal": proposal.as_dict(),
        "applied": False,
        "note": "a proposal changes nothing; only the owner approves via CLI or web",
    }
    context.ledger.append(
        "config_proposed",
        {
            "changed": list(proposal.changed),
            "proposed_by": proposed_by,
            "frozen_hash": proposal.frozen_hash,
        },
    )
    return payload


def _list_proposals(context: ServerContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    from saberops.config import ConfigError

    ledger = context.control_ledger()
    try:
        entries = ledger.entries()
    except ConfigError as exc:
        raise ToolError(f"config ledger unreadable: {exc}") from exc
    return {
        "v": SNAPSHOT_SCHEMA_VERSION,
        "count": len(entries),
        "proposals": [entry.as_dict() for entry in entries],
    }


# --------------------------------------------------------------------------- #
# Registry construction
# --------------------------------------------------------------------------- #


def build_default_registry(context: ServerContext) -> ToolRegistry:
    """Return the twelve-tool registry bound to ``context``."""
    registry = ToolRegistry(lease=context.lease)

    def register(
        name: str,
        description: str,
        schema: dict[str, Any],
        handler: Callable[[ServerContext, Mapping[str, Any]], dict[str, Any]],
        *,
        mutating: bool,
    ) -> None:
        registry.register(
            ToolSpec(
                name=name,
                description=description,
                input_schema=schema,
                handler=lambda arguments: handler(context, arguments),
                mutating=mutating,
            )
        )

    register(
        "submit_job",
        "Submit one job from stated requirements (no provider/model fields).",
        _schema(
            {
                "repo": {"type": "string", "minLength": 1},
                "goal": {"type": "string", "minLength": 1},
                "acceptance": {"type": "string"},
                "base_ref": {"type": "string", "minLength": 1},
                "priority": {"type": "string", "enum": _PRIORITIES},
                "deadline": {"type": "string"},
                "not_before": {"type": "string"},
                "idempotency_key": {"type": "string"},
                "caller_id": {"type": "string"},
                "requirements": _schema(
                    {
                        "role": {"type": "string", "enum": _JOB_ROLES},
                        "effort": {"type": "string", "enum": _EFFORTS},
                        "required_capabilities": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "read_only": {"type": "boolean"},
                        "model_family_include": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "model_family_exclude": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "size_hint": {"type": "string"},
                    }
                ),
            },
            required=("repo", "goal"),
        ),
        _submit_job,
        mutating=True,
    )
    register(
        "get_job",
        "Return one job's record, state and timing.",
        _schema({"job_id": {"type": "string", "minLength": 1}}, required=("job_id",)),
        _get_job,
        mutating=False,
    )
    register(
        "list_jobs",
        "List jobs, optionally filtered by state.",
        _schema(
            {
                "state": {"type": "string", "enum": _JOB_STATES},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            }
        ),
        _list_jobs,
        mutating=False,
    )
    register(
        "cancel_job",
        "Cancel a job (RUNNING cancels the live run first).",
        _schema(
            {
                "job_id": {"type": "string", "minLength": 1},
                "reason": {"type": "string"},
            },
            required=("job_id",),
        ),
        _cancel_job,
        mutating=True,
    )
    register(
        "capacity",
        "Return the pool capacity read model (spare, reset deadlines).",
        _schema({}),
        _capacity,
        mutating=False,
    )
    register(
        "submit_wave",
        "Validate and record a wave submission for the engine dispatcher.",
        _schema({"wave": {"type": "object"}}, required=("wave",)),
        _submit_wave,
        mutating=True,
    )
    register(
        "wave_status",
        "Return wave state, lane statuses and barrier conditions.",
        _schema(
            {
                "wave": {"type": "object"},
                "wave_id": {"type": "string"},
            }
        ),
        _wave_status,
        mutating=False,
    )
    register(
        "read_evidence",
        "Read one allowed evidence file under .orch or .saberops.",
        _schema({"path": {"type": "string", "minLength": 1}}, required=("path",)),
        _read_evidence,
        mutating=False,
    )
    register(
        "list_questions",
        "List owner questions, optionally filtered by status or run.",
        _schema(
            {
                "status": {"type": "string", "enum": _QUESTION_STATUSES},
                "run_id": {"type": "string"},
            }
        ),
        _list_questions,
        mutating=False,
    )
    register(
        "answer_question",
        "Answer an Orchestrator-answerable question (owner-only classes refuse).",
        _schema(
            {
                "question_id_or_key": {"type": "string", "minLength": 1},
                "answer_text": {"type": "string", "minLength": 1},
                "authority": {"type": "string", "enum": _ANSWER_AUTHORITIES},
                "answered_by": {"type": "string"},
                "answered_by_role": {"type": "string", "enum": _ANSWER_ROLES},
                "chosen_option": {"type": "string"},
                "evidence": {"type": "string"},
                "rejected_alternative": {"type": "string"},
            },
            required=("question_id_or_key", "answer_text", "authority"),
        ),
        _answer_question,
        mutating=True,
    )
    register(
        "propose_config_change",
        "Validate and return a config proposal (nothing is applied).",
        _schema(
            {
                "changes": {"type": "object"},
                "rationale": {"type": "string", "minLength": 1},
                "proposed_by": {"type": "string", "minLength": 1},
            },
            required=("changes", "rationale", "proposed_by"),
        ),
        _propose_config_change,
        mutating=True,
    )
    register(
        "list_proposals",
        "List the append-only config decision/proposal ledger.",
        _schema({}),
        _list_proposals,
        mutating=False,
    )

    registered = set(registry.names())
    if registered != set(TOOL_NAMES):
        raise ToolError(
            "registry does not expose exactly the D7 tool set: "
            f"missing={sorted(set(TOOL_NAMES) - registered)} "
            f"extra={sorted(registered - set(TOOL_NAMES))}"
        )
    if registered & set(FORBIDDEN_OPERATIONS):
        raise ToolError("a never-exposed operation was registered")
    return registry
