"""The Orchestrator contract tools exposed over MCP (ADR-0004 D7, M3).

The following tools are exposed::

    preflight submit_job get_job catch_up tail_job wait_job list_jobs cancel_job
    note_job capacity read_evidence list_questions answer_question
    propose_config_change list_proposals

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

import time
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
        "refresh_candidate",
        "cancel_job",
        "note_job",
        "answer_question",
        "propose_config_change",
    }
)

#: Tools that only read; these stay available to a read-only client.
READ_TOOLS: frozenset[str] = frozenset(
    {
        "get_job",
        "catch_up",
        "tail_job",
        "wait_job",
        "preflight",
        "list_jobs",
        "capacity",
        "read_evidence",
        "list_questions",
        "list_proposals",
    }
)

TOOL_NAMES: tuple[str, ...] = (
    "preflight",
    "submit_job",
    "refresh_candidate",
    "get_job",
    "catch_up",
    "tail_job",
    "wait_job",
    "list_jobs",
    "cancel_job",
    "note_job",
    "capacity",
    "read_evidence",
    "list_questions",
    "answer_question",
    "propose_config_change",
    "list_proposals",
)

_JOB_STATES = ["QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"]
_PRIORITIES = ["URGENT", "NORMAL", "BACKGROUND"]
_JOB_ROLES = ["PLAN", "BUILD", "CHECK"]
_EFFORTS = ["low", "medium", "high"]
_QUESTION_STATUSES = ["OPEN", "DEFERRED", "ANSWERED", "MOOT"]
_ANSWER_AUTHORITIES = ["OWNER", "PLAN"]
_ANSWER_ROLES = ["OWNER", "PLAN"]

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
            raise ToolError(f"refusing to register the never-exposed operation {spec.name!r}")
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
            status = self._lease.status()
            raise LeaseReadOnlyError(
                f"{name!r} is read-only: {status['holder']} (pid {status['holder_pid']}) "
                "holds planning ownership; use orch mcp release --holder <pid-or-session> "
                "for a targeted handoff",
                data={"tool": name, "operation": "mutating", "lease_writer": False},
            )
        errors = validate_instance(spec.input_schema, dict(arguments))
        if errors:
            raise InvalidParamsError(
                f"{name}: invalid arguments: {'; '.join(errors)}", data={"errors": errors}
            )
        if self._lease is None or name == "wait_job":
            return spec.handler(arguments)
        with self._lease.guarded_call(mutating=spec.mutating) as owned:
            if spec.mutating and not owned:
                status = self._lease.status()
                raise LeaseReadOnlyError(
                    f"planning ownership changed to {status['holder']} "
                    f"(pid {status['holder_pid']}); use orch mcp release for handoff",
                    data={"tool": name, "lease_writer": False},
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
    from saberops.jobs.models import Effort, JobRequirements, Priority, coerce_job_role
    from saberops.jobs.service import IdempotencyConflictError, submit_job

    _deny_requirement_fields(arguments)
    raw_requirements = arguments.get("requirements")
    requirements = JobRequirements()
    if isinstance(raw_requirements, Mapping):
        try:
            requirements = JobRequirements(
                role=coerce_job_role(str(raw_requirements.get("role", "BUILD"))),
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
            acceptance=(
                "\n".join(arguments["acceptance"])
                if isinstance(arguments.get("acceptance"), list)
                else arguments.get("acceptance")
            ),
            base_ref=str(arguments.get("base_ref") or "HEAD"),
            requirements=requirements,
            priority=Priority(str(arguments.get("priority") or Priority.NORMAL.value)),
            deadline=arguments.get("deadline"),
            not_before=arguments.get("not_before"),
            idempotency_key=arguments.get("idempotency_key"),
            caller_id=str(arguments.get("caller_id") or "orchestrator"),
            mcp_session_id=context.lease.session_id,
        )
    except IdempotencyConflictError as exc:
        raise ToolError(f"idempotency conflict: {exc}") from exc
    except ValueError as exc:
        raise InvalidParamsError(f"invalid job: {exc}") from exc
    payload = {
        "v": SNAPSHOT_SCHEMA_VERSION,
        "result": _job_result(db, result.job),
        "guidance": _job_guidance(result.job),
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


def _job_guidance(job: Any) -> str | None:
    if job.failure_reason == "NO_ROUTE_FOR_REQUIREMENTS":
        if job.requirements.role.value != "BUILD":
            return "Only BUILD jobs dispatch; set requirements.role to BUILD."
        return (
            "No build route satisfies the requirements. Configure one with "
            "`orch routing add build <provider/model>`; inspect with `orch routing show` "
            "and adjust capability requirements if needed. Only BUILD jobs dispatch."
        )
    return None


def _job_result(db: Any, job: Any) -> dict[str, Any]:
    """Read-only result projection, reusable by completion tools."""
    from saberops.accept import AcceptEngine
    from saberops.candidate_lifecycle import project_candidate_state
    from saberops.jobs.service import job_usage_view

    result: dict[str, Any] = {
        "run_id": None,
        "candidate_sha": None,
        "diff": None,
        "owner_action": None,
        "usage": job_usage_view(db, job),
    }
    for run_id in reversed(job.run_ids):
        projection = project_candidate_state(db, run_id)
        if projection.candidate_sha is None:
            continue
        eligible = AcceptEngine(db).check_accept_eligibility(run_id).eligible
        if result["candidate_sha"] is not None and not eligible:
            continue
        result.update(
            run_id=run_id,
            candidate_sha=projection.candidate_sha,
            diff={
                "repo": job.repo,
                "base_sha": job.base_sha,
                "candidate_sha": projection.candidate_sha,
            },
        )
        if eligible:
            result["owner_action"] = f"orch accept {run_id}"
            break
    return result


def _get_job(context: ServerContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    from saberops.jobs.service import get_job, job_status_view

    job_id = _require_text(arguments, "job_id", what="job_id")
    db = context.database(read_only=True)
    job = None if db is None else get_job(db, job_id)
    if job is None:
        raise NotFoundError(f"unknown job: {job_id}", data={"job_id": job_id})
    result = _job_result(db, job)
    return {
        "v": SNAPSHOT_SCHEMA_VERSION,
        "job": job_status_view(db, job),
        "result": result,
        "usage": result["usage"],
        "guidance": _job_guidance(job),
    }


def _tail_job(context: ServerContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Read a running job's live progress without touching it (Wave 3 F3)."""
    from saberops.jobs.service import get_job, job_status_view
    from saberops.mcp.tail import DEFAULT_TAIL_BYTES, parse_cursor, tail_run

    job_id = _require_text(arguments, "job_id", what="job_id")
    db = context.database(read_only=True)
    job = None if db is None else get_job(db, job_id)
    if job is None:
        raise NotFoundError(f"unknown job: {job_id}", data={"job_id": job_id})
    view = job_status_view(db, job)
    cursor = parse_cursor(arguments.get("after_sequence"))
    max_bytes = int(arguments.get("max_bytes", DEFAULT_TAIL_BYTES))
    run_id = job.current_run_id or (job.run_ids[-1] if job.run_ids else None)
    payload: dict[str, Any] = {
        "v": SNAPSHOT_SCHEMA_VERSION,
        "job_id": job.job_id,
        "state": view["state"],
        "activity": view["activity"],
        "reason": view["reason"],
        "run_id": run_id,
    }
    if run_id is None:
        payload.update(
            next_sequence=cursor.encode(),
            events=[],
            transcript=None,
            has_more=False,
        )
        return payload
    payload.update(
        tail_run(db, run_id=run_id, cursor=cursor, max_bytes=max_bytes)
    )
    return payload


def _excerpt(text: str | None, *, limit: int = 400, tail: bool = False) -> str | None:
    """Return a short, single-block excerpt; full evidence stays in the records."""
    if not text:
        return None
    stripped = text.strip()
    if not stripped:
        return None
    if len(stripped) <= limit:
        return stripped
    return "…" + (stripped[-limit:] if tail else stripped[:limit])


def _catch_up_check(
    db: Any, run_id: str, attempts: list[Any], candidate_sha: str | None
) -> dict[str, Any] | None:
    """Latest gate check for the run, with the commit that check assessed."""
    checks = db.get_checks_for_run(run_id)
    if not checks:
        return None
    latest = checks[-1]
    assessed = next(
        (attempt.commit_sha for attempt in attempts if attempt.id == latest.attempt_id),
        None,
    )
    return {
        "check_id": latest.id,
        "attempt_id": latest.attempt_id,
        "assessed_commit": assessed,
        "assessed_current_candidate": (
            candidate_sha is not None and assessed == candidate_sha
        ),
        "passed": latest.passed,
        "skipped": latest.skipped,
        "gate_command": latest.gate_command,
        "exit_code": latest.exit_code,
        "excerpt": _excerpt(
            latest.stdout if latest.passed else latest.stderr, tail=not latest.passed
        ),
    }


def _catch_up_review(
    db: Any, run_id: str, candidate_sha: str | None
) -> dict[str, Any] | None:
    """Latest review, preferring one bound to the current candidate.

    The commit a review assessed lives in the REVIEW dispatch evidence row;
    a review row itself carries no SHA.  Never infer the assessed commit from
    "the latest commit" — a review of candidate A must not be attributed to a
    later candidate B.
    """
    from saberops.models import DispatchStage

    reviews = db.get_reviews_for_run(run_id)
    if not reviews:
        return None
    assessed: dict[str, str | None] = {}
    for dispatch in db.get_dispatch_evidence_for_run(run_id):
        if dispatch.stage != DispatchStage.REVIEW:
            continue
        for key in (dispatch.association_id, dispatch.review_id):
            if key:
                assessed.setdefault(key, dispatch.candidate_sha)
    relevant = [
        review
        for review in reviews
        if candidate_sha is not None and assessed.get(review.id) == candidate_sha
    ]
    chosen = (relevant or reviews)[-1]
    assessed_sha = assessed.get(chosen.id)
    return {
        "review_id": chosen.id,
        "assessed_commit": assessed_sha,
        "assessed_current_candidate": (
            candidate_sha is not None and assessed_sha == candidate_sha
        ),
        "verdict": chosen.verdict.value,
        "provider": chosen.provider,
        "model": chosen.model,
        "created_at": chosen.created_at,
        "excerpt": _excerpt(chosen.summary),
    }


def _catch_up_questions(context: ServerContext, job: Any) -> list[dict[str, Any]]:
    """Open questions that name this job or one of its runs."""
    from saberops.questions import QuestionStatus, question_view

    questions = context.question_queue().questions(status=QuestionStatus.OPEN)
    matched: list[dict[str, Any]] = []
    for question in questions:
        request = question.request
        if request.run_id not in job.run_ids and job.job_id not in request.blocks:
            continue
        view = question_view(question)
        matched.append(
            {
                "question_id": view["question_id"],
                "key": view["key"],
                "subject": view["subject"],
                "decision_class": view["decision_class"],
                "answerable_by": view["answerable_by"],
                "run_id": view["run_id"],
                "blocks": view["blocks"],
                "options": view["options"],
                "recommendation": request.escalation.recommendation,
            }
        )
    return matched


def _catch_up_steering_notes(db: Any, job: Any) -> list[dict[str, Any]]:
    """Owner steering notes with their delivery state (read-only)."""
    from saberops.jobs.service import steering_notes

    return [note.to_dict() for note in steering_notes(db, job.job_id)]


def _catch_up_next_action(
    job: Any,
    view: Mapping[str, Any],
    result: Mapping[str, Any],
    projection: Any,
    questions: list[dict[str, Any]],
) -> dict[str, Any]:
    """The one action the engine allows next, derived only from saved records."""
    from saberops.jobs.models import JobState
    from saberops.models import CandidateState

    run_id = result.get("run_id")
    if questions:
        question = questions[0]
        if question["answerable_by"] == "PLAN":
            return {
                "action": "answer_question",
                "owner_only": False,
                "detail": (
                    f"Question {question['question_id']} blocks this job; answer it with "
                    "the answer_question tool (authority PLAN)."
                ),
                "command": None,
                "question_id": question["question_id"],
            }
        return {
            "action": "answer_question",
            "owner_only": True,
            "detail": (
                f"Question {question['question_id']} blocks this job and only the owner "
                "can answer it."
            ),
            "command": f'orch questions answer {question["question_id"]} --words "<answer>"',
            "question_id": question["question_id"],
        }
    if projection is not None and projection.state is CandidateState.ACCEPTED:
        return {
            "action": "none",
            "owner_only": False,
            "detail": f"Candidate {projection.candidate_sha[:8]} is already accepted.",
            "command": None,
        }
    if result.get("owner_action"):
        short = str(result.get("candidate_sha") or "")[:8]
        return {
            "action": "owner_accept",
            "owner_only": True,
            "detail": f"Candidate {short} is checked and ready; the owner accepts it.",
            "command": result["owner_action"],
            "reject": {
                "surface": "web",
                "path": f"/runs/{run_id}",
                "detail": "The owner can reject this candidate from the run page.",
            },
        }
    if job.state is JobState.FAILED:
        work = view.get("work_location") or "the preserved work location"
        return {
            "action": "inspect_and_resubmit",
            "owner_only": False,
            "detail": (
                f"The job failed ({view.get('reason') or job.failure_reason}). "
                f"Inspect the preserved work at {work} and resubmit with a fresh "
                "idempotency_key once the cause is fixed."
            ),
            "command": None,
        }
    if job.state is JobState.RUNNING:
        reason = view.get("reason")
        return {
            "action": "wait",
            "owner_only": False,
            "detail": (
                f"The job is running ({view.get('activity') or 'running'}); call wait_job"
                + (f": {reason}" if reason else ".")
            ),
            "command": None,
        }
    if job.state is JobState.QUEUED:
        reason = job.waiting_reason.value if job.waiting_reason is not None else "QUEUED"
        return {
            "action": "wait",
            "owner_only": False,
            "detail": f"The job is queued ({reason}); call wait_job.",
            "command": None,
        }
    if job.state is JobState.CANCELLED:
        return {
            "action": "none",
            "owner_only": False,
            "detail": "The job was cancelled; there is no owner action.",
            "command": None,
        }
    if projection is not None and projection.state is CandidateState.REJECTED:
        return {
            "action": "none",
            "owner_only": False,
            "detail": "The owner rejected the current candidate; resubmit if needed.",
            "command": None,
        }
    reason = projection.reason if projection is not None else "no candidate yet"
    return {
        "action": "inspect",
        "owner_only": False,
        "detail": (
            f"Nothing is acceptable yet ({reason}); read the run page for the full "
            "check and review evidence."
        ),
        "command": None,
    }


def _catch_up(context: ServerContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Read-only "catch me up" lookup: saved request, candidate and next action.

    Reuses :func:`_job_result` (C5) so the owner action has exactly one
    projection.  Nothing is written: no job/run/event/question mutation, no
    notice, and no planning lease.  A session that does not hold the writer
    lease can call it.
    """
    from saberops.candidate_lifecycle import project_candidate_state
    from saberops.jobs.service import get_job, job_status_view

    job_id = _require_text(arguments, "job_id", what="job_id")
    db = context.database(read_only=True)
    job = None if db is None else get_job(db, job_id)
    if job is None:
        raise NotFoundError(f"unknown job: {job_id}", data={"job_id": job_id})
    assert db is not None

    view = job_status_view(db, job)
    result = _job_result(db, job)
    run_id = result.get("run_id") or job.current_run_id
    if run_id is None and job.run_ids:
        run_id = job.run_ids[-1]
    projection = project_candidate_state(db, run_id) if run_id is not None else None
    run = db.get_run(run_id) if run_id is not None else None

    attempts = db.get_attempts_for_run(run_id) if run_id is not None else []
    check = (
        _catch_up_check(
            db,
            run_id,
            attempts,
            projection.candidate_sha if projection is not None else None,
        )
        if run_id is not None
        else None
    )
    review = (
        _catch_up_review(db, run_id, projection.candidate_sha if projection else None)
        if run_id is not None
        else None
    )
    questions = _catch_up_questions(context, job)

    failure = None
    if job.state.value == "FAILED" or view.get("last_failed_attempt"):
        failed_attempt = view.get("last_failed_attempt") or {}
        failure = {
            "reason": failed_attempt.get("reason") or view.get("reason") or job.failure_reason,
            "work_location": view.get("work_location")
            or failed_attempt.get("work_location"),
            "attempt_id": failed_attempt.get("attempt_id"),
        }

    return {
        "v": SNAPSHOT_SCHEMA_VERSION,
        "job_id": job.job_id,
        "request": {
            "goal": job.goal,
            "acceptance": job.acceptance,
            "repo": job.repo,
            "base_ref": job.base_ref,
            "base_sha": job.base_sha,
        },
        "state": job.state.value,
        "activity": view.get("activity"),
        "reason": view.get("reason"),
        "run_id": run_id,
        "run_status": run.status.value if run is not None else None,
        "candidate": (
            {
                "run_id": projection.run_id,
                "candidate_sha": projection.candidate_sha,
                "base_sha": run.base_commit if run is not None else None,
                "state": (
                    projection.state.value if projection.state is not None else None
                ),
                "verdict": (
                    projection.verdict.value if projection.verdict is not None else None
                ),
                "reason": projection.reason,
                "review_required": projection.review_required,
                "evidence": list(projection.evidence),
            }
            if projection is not None
            else None
        ),
        "check": check,
        "review": review,
        "open_questions": questions,
        "steering_notes": _catch_up_steering_notes(db, job),
        "failure": failure,
        "next_action": _catch_up_next_action(job, view, result, projection, questions),
        "usage": result["usage"],
        "result": result,
        "evidence": {
            "run_page": f"/runs/{run_id}" if run_id is not None else None,
            "check": f"check:{check['check_id']}" if check is not None else None,
            "review": f"review:{review['review_id']}" if review is not None else None,
        },
    }


def _wait_job(context: ServerContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    from saberops.questions import QuestionStatus, question_view

    deadline = time.monotonic() + int(arguments.get("timeout_seconds", 60))
    while True:
        result = _get_job(context, arguments)
        job = result["job"]
        if job["state"] in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            return {**result, "timed_out": False, "outcome": "final"}
        for question in context.question_queue().questions(status=QuestionStatus.OPEN):
            request = question.request
            if job["job_id"] in request.blocks or (
                request.run_id is not None and request.run_id in job["run_ids"]
            ):
                return {
                    **result,
                    "timed_out": False,
                    "outcome": "needs_attention",
                    "question": question_view(question),
                }
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {**result, "timed_out": True, "outcome": "timeout"}
        time.sleep(min(0.5, remaining))


def _list_jobs(context: ServerContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    from saberops.jobs.service import job_status_view, list_jobs

    limit = int(arguments.get("limit", 50))
    state = arguments.get("state")
    db = context.database(read_only=True)
    jobs = [] if db is None else list_jobs(db, state=state, limit=limit)
    return {
        "v": SNAPSHOT_SCHEMA_VERSION,
        "count": len(jobs),
        "jobs": [job_status_view(db, job) for job in jobs],
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


def _note_job(context: ServerContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Record one durable steering note; delivery happens at the next attempt."""
    from saberops.jobs.service import JobNotFoundError, note_job

    job_id = _require_text(arguments, "job_id", what="job_id")
    text = _require_text(arguments, "text", what="text")
    db = context.database(read_only=False)
    try:
        note = note_job(db, job_id, text)
    except JobNotFoundError as exc:
        raise NotFoundError(f"unknown job: {job_id}", data={"job_id": job_id}) from exc
    except ValueError as exc:
        raise InvalidParamsError(f"invalid steering note: {exc}") from exc
    context.ledger.append(
        "job_note_recorded",
        {"job_id": job_id, "note_id": note.note_id},
        key=f"job:{job_id}",
    )
    return {
        "v": SNAPSHOT_SCHEMA_VERSION,
        "job_id": job_id,
        "note_id": note.note_id,
        "note": note.to_dict(),
        "delivery": (
            "pending: the note reaches the worker in the prompt of the job's "
            "next attempt, never mid-tool"
        ),
    }


def _preflight(context: ServerContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    from saberops.preflight import collect_preflight

    repo = _require_text(arguments, "repo", what="repo")
    findings = collect_preflight(Path(repo), context.db_path)
    return {"v": SNAPSHOT_SCHEMA_VERSION, "ready": all(f.severity != "FAIL" for f in findings),
            "findings": [asdict(f) for f in findings]}


def _capacity(context: ServerContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    from saberops.capacity.loop import scheduler_alive
    from saberops.jobs.service import CapacityView, capacity, serialize_capacity_view

    db = context.database(read_only=True)
    if db is None:
        empty_view = CapacityView(generated_at=_now_iso(), ledger_wired=False, pools=())
        return {
            "v": SNAPSHOT_SCHEMA_VERSION,
            "capacity": serialize_capacity_view(empty_view, scheduler_alive=False),
        }
    return {
        "v": SNAPSHOT_SCHEMA_VERSION,
        "capacity": serialize_capacity_view(capacity(db), scheduler_alive=scheduler_alive(db)),
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


def _answer_question(context: ServerContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    from saberops.questions import (
        AnswerAuthority,
        AskRole,
        QuestionError,
        coerce_answer_authority,
        coerce_ask_role,
        is_orchestrator_answerable,
        question_view,
    )

    ref = _require_text(arguments, "question_id_or_key", what="question_id_or_key")
    words = _require_text(arguments, "answer_text", what="answer_text")
    try:
        authority = coerce_answer_authority(str(arguments.get("authority") or ""))
    except ValueError as exc:
        raise InvalidParamsError(str(exc)) from exc
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
            "the planning session may not answer it over MCP",
            data={
                "ref": ref,
                "decision_class": question.request.decision_class.value,
            },
        )
    answered_by = str(arguments.get("answered_by") or "orchestrator")
    try:
        role = coerce_ask_role(str(arguments.get("answered_by_role") or AskRole.PLAN.value))
    except ValueError as exc:
        raise InvalidParamsError(str(exc)) from exc
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


def _propose_config_change(context: ServerContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
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


def _run_database(context: ServerContext, run_id: str) -> Any:
    """Return the database that owns ``run_id``.

    Jobs and capacity live in the owner database, while run state may live in
    the owning project's store.  Resolution uses the run ID's own project
    segment (never a current selection); an unresolvable project-scoped run
    fails closed in :func:`saberops.runtime.db_path_for_run`.
    """
    from saberops.db import Database
    from saberops.runtime import db_path_for_run

    owner = context.database(read_only=False)
    assert owner is not None
    if owner.get_run(run_id) is not None:
        return owner
    resolved = db_path_for_run(run_id)
    if resolved is None:
        return owner
    return Database(resolved, owner_db=owner)


def _refresh_candidate(context: ServerContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    from saberops.service import OrchestratorService, RunOperationError

    run_id = _require_text(arguments, "run_id", what="run_id")
    db = _run_database(context, run_id)
    try:
        result = OrchestratorService(db=db).refresh_run(run_id)
    except RunOperationError as exc:
        raise ToolError(str(exc)) from exc
    return {"v": SNAPSHOT_SCHEMA_VERSION, **result}


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
    """Return the current tool registry bound to ``context``."""
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
                "acceptance": {"type": ["string", "array"], "items": {"type": "string"}},
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
        "refresh_candidate",
        "Refresh a stale run candidate with fresh checks and review; acceptance remains separate.",
        _schema({"run_id": {"type": "string", "minLength": 1}}, required=("run_id",)),
        _refresh_candidate,
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
        "catch_up",
        "Read-only handoff: original request, candidate, latest check and review, open "
        "question and the next allowed action for one job.",
        _schema({"job_id": {"type": "string", "minLength": 1}}, required=("job_id",)),
        _catch_up,
        mutating=False,
    )
    register(
        "tail_job",
        "Read-only live tail: new events and terminal output for one job after a cursor. "
        "Follows a running worker without waiting and without changing anything.",
        _schema(
            {
                "job_id": {"type": "string", "minLength": 1},
                "after_sequence": {
                    "type": ["integer", "string"],
                    "description": (
                        "Cursor returned as next_sequence by the previous call; "
                        "a plain event id or omitted for the first read."
                    ),
                },
                "max_bytes": {"type": "integer", "minimum": 512, "maximum": 1048576},
            },
            required=("job_id",),
        ),
        _tail_job,
        mutating=False,
    )
    register(
        "wait_job",
        "Wait for a job outcome or a blocking question, up to the timeout.",
        _schema(
            {
                "job_id": {"type": "string", "minLength": 1},
                "timeout_seconds": {"type": "integer", "minimum": 0, "maximum": 3600},
            },
            required=("job_id",),
        ),
        _wait_job,
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
        "note_job",
        "Record one owner steering note on a job; it is delivered once at the "
        "next worker attempt (never typed into a running worker) and shown by catch_up.",
        _schema(
            {
                "job_id": {"type": "string", "minLength": 1},
                "text": {"type": "string", "minLength": 1, "maxLength": 4000},
            },
            required=("job_id", "text"),
        ),
        _note_job,
        mutating=True,
    )
    register(
        "preflight",
        "Check project readiness and report setup commands without model prompts.",
        _schema({"repo": {"type": "string", "minLength": 1}}, required=("repo",)),
        _preflight,
        mutating=False,
    )
    register(
        "capacity",
        "Return the pool capacity read model (spare, reset deadlines).",
        _schema({}),
        _capacity,
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
