"""The Orchestrator contract tools exposed over MCP.

The following tools are exposed::

    preflight start_walkthrough submit_job submit_plan promote_scout refresh_candidate
    resume_job redirect_job get_job catch_up plan_status tail_job wait_job list_jobs
    cancel_job note_job discard_note capacity read_evidence list_questions
    answer_question propose_config_change list_proposals get_mode propose_mode_change
    get_pause

With ``ORCH_NATIVE_WORKERS`` enabled, four more tools exist for the hybrid
native-worker path (ADR-0030): ``native_claim``, ``native_heartbeat``,
``native_controls`` and ``record_native_attempt``.  With the flag unset they are
not registered at all.

With an advisor route set for the session's project (ADR-0034), ``ask_advisor``
is registered as well; without a route it is absent, so "off" means no tool.

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

import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field, replace
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
from saberops.mcp.lease import OrchestratorLease, pid_is_alive
from saberops.mcp.ledger import SNAPSHOT_SCHEMA_VERSION, EventLedger
from saberops.mcp.schema import validate_instance

__all__ = [
    "FORBIDDEN_OPERATIONS",
    "MUTATING_TOOLS",
    "NATIVE_MUTATING_TOOLS",
    "READ_TOOLS",
    "TOOL_NAMES",
    "ServerContext",
    "ToolRegistry",
    "ToolSpec",
    "build_default_registry",
]

#: Operations permanently refuses over MCP, with the invariant at
#: stake.  Aliases are listed so a client cannot reach a prohibited authority
#: through a synonym.
FORBIDDEN_OPERATIONS: dict[str, str] = {
    "approve_config_proposal": "config approval is owner-only",
    "approve_proposal": "config approval is owner-only",
    "config_approve": "config approval is owner-only",
    "record_config_decision": "config approval is owner-only",
    "merge_main": "moving main is owner-only",
    "move_main": "moving main is owner-only",
    "fast_forward_main": "moving main is owner-only",
    "merge_ff_only": "moving main is owner-only",
    "push": "pushing is owner-only",
    "git_push": "pushing is owner-only",
    "push_remote": "pushing is owner-only",
    "approve_quarantine": "quarantine approval is owner-only",
    "quarantine_approve": "quarantine approval is owner-only",
    "attest_posture": "posture attestation is owner-only",
    "posture_attest": "posture attestation is owner-only",
    "run_gate": "gate execution is engine/owner-only",
    "gate_run": "gate execution is engine/owner-only",
    "gate_full": "gate execution is engine/owner-only",
    "run_full_gate": "gate execution is engine/owner-only",
    "edit_source": "no direct tracked-source edits over MCP",
    "write_file": "no direct tracked-source edits over MCP",
    "apply_patch": "no direct tracked-source edits over MCP",
    "delete_path": "deletions outside .orch are owner-only",
    "delete_outside_orch": "deletions outside .orch are owner-only",
    "remove_path": "deletions outside .orch are owner-only",
    "read_credentials": "read tools never return credentials",
    "get_credentials": "read tools never return credentials",
    "read_secret": "read tools never return credentials",
    "read_token": "read tools never return credentials",
    "resume_work": "resuming paused work is owner-only; the owner runs `orch resume`",
}

#: Tools that mutate state; these require the single-writer lease.
MUTATING_TOOLS: frozenset[str] = frozenset(
    {
        "submit_job",
        "submit_plan",
        "promote_scout",
        "refresh_candidate",
        "resume_job",
        "redirect_job",
        "cancel_job",
        "note_job",
        "discard_note",
        "answer_question",
        "propose_config_change",
        "propose_mode_change",
    }
)

#: F16 native-worker tools that mutate durable evidence when the owner flag is
#: on (``controls`` posts the stop once the heartbeat deadline has passed).
#: They are kept apart from :data:`MUTATING_TOOLS` because they are only
#: registered while the flag is enabled.
NATIVE_MUTATING_TOOLS: frozenset[str] = frozenset(
    {
        "native_claim",
        "native_heartbeat",
        "native_controls",
        "record_native_attempt",
    }
)

#: Tools that only read; these stay available to a read-only client.
READ_TOOLS: frozenset[str] = frozenset(
    {
        "get_job",
        "catch_up",
        "plan_status",
        "tail_job",
        "wait_job",
        "preflight",
        "start_walkthrough",
        "list_jobs",
        "capacity",
        "read_evidence",
        "list_questions",
        "list_proposals",
        "get_mode",
        "get_pause",
    }
)

TOOL_NAMES: tuple[str, ...] = (
    "preflight",
    "start_walkthrough",
    "submit_job",
    "submit_plan",
    "promote_scout",
    "refresh_candidate",
    "resume_job",
    "redirect_job",
    "get_job",
    "catch_up",
    "plan_status",
    "tail_job",
    "wait_job",
    "list_jobs",
    "cancel_job",
    "note_job",
    "discard_note",
    "capacity",
    "read_evidence",
    "list_questions",
    "answer_question",
    "propose_config_change",
    "list_proposals",
    "get_mode",
    "propose_mode_change",
    "get_pause",
)

_JOB_STATES = ["QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"]
_PRIORITIES = ["URGENT", "NORMAL", "BACKGROUND"]
_JOB_ROLES = ["PLAN", "BUILD", "CHECK"]
_EFFORTS = ["low", "medium", "high"]
_DIFFICULTIES = ["routine", "hard"]
_QUESTION_STATUSES = ["OPEN", "DEFERRED", "ANSWERED", "MOOT"]
_ANSWER_AUTHORITIES = ["OWNER", "PLAN"]
_ANSWER_ROLES = ["OWNER", "PLAN"]
_OWNER_MODE_VALUES = ["ask-me", "critical-only", "away"]

#: A cross-CLI proxy's self-asserted provenance label (D13).  The narrow
#: create-only exception below accepts only this shape, never a free-form
#: caller string, so it cannot become a general write grant for read-only
#: clients.
_PROXY_CALLER_ID = re.compile(r"^proxy:[a-z0-9][a-z0-9._-]{0,63}$")

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
    #: Project directory this session serves, when known.  ``ask_advisor`` is
    #: offered only when this project has an advisor route (off means no tool).
    repo: Path | None = None
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

    def submit_session_id(self) -> str:
        """The session that owns a submitted job.

        Normally this session.  A proxy submits from its own read-only server
        while the live planning lease belongs to the parent session, so the
        job is owned by that live planner: its running work keeps protecting
        the lease instead of disappearing behind a short-lived child process.
        """
        record = self.lease.read()
        if record is not None and record.session_id and pid_is_alive(record.pid):
            return record.session_id
        return self.lease.session_id


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

    def _proxy_submit_owner(self, name: str, arguments: Mapping[str, Any]) -> str | None:
        """Return the live planning session allowed to accept a proxy submit.

        D13: a host CLI may give each custom-agent child its own MCP server
        instance, so the child is read-only even though the parent session is
        still planning.  The exception is deliberately narrow: only
        ``submit_job``, only for a ``caller_id`` shaped like ``proxy:<name>``,
        and only while the recorded planning lease names a live process with a
        session id.  Every other mutating tool, and every non-proxy caller,
        stays refused; a dead or missing lease grants nothing.
        """
        if name != "submit_job" or self._lease is None:
            return None
        caller = arguments.get("caller_id")
        if not isinstance(caller, str) or _PROXY_CALLER_ID.fullmatch(caller.strip()) is None:
            return None
        record = self._lease.read()
        if record is None or not record.session_id or not pid_is_alive(record.pid):
            return None
        return record.session_id

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
        proxy_submit = None
        if spec.mutating and self._lease is not None and not self._lease.writer:
            proxy_submit = self._proxy_submit_owner(name, arguments)
            if proxy_submit is None:
                status = self._lease.status()
                raise LeaseReadOnlyError(
                    f"{name!r} is read-only: {status['holder']} "
                    f"(pid {status['holder_pid']}) holds planning ownership; ask the "
                    "owner to run orch mcp release --holder <pid-or-session> for a "
                    "targeted handoff",
                    data={"tool": name, "operation": "mutating", "lease_writer": False},
                )
        errors = validate_instance(spec.input_schema, dict(arguments))
        if errors:
            raise InvalidParamsError(
                f"{name}: invalid arguments: {'; '.join(errors)}", data={"errors": errors}
            )
        if proxy_submit is not None:
            # Create-only and outside the writer's flock: the planner keeps the
            # lease, and this call only inserts a new queued job.
            return spec.handler(arguments)
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
    from saberops.jobs.models import (
        DIFFICULTIES,
        Effort,
        JobRequirements,
        Priority,
        coerce_difficulty,
        coerce_job_role,
    )
    from saberops.jobs.service import IdempotencyConflictError, submit_job

    _deny_requirement_fields(arguments)
    raw_requirements = arguments.get("requirements")
    requirements = JobRequirements()
    if isinstance(raw_requirements, Mapping):
        difficulty = raw_requirements.get("difficulty")
        if difficulty is not None and str(difficulty).strip().lower() not in DIFFICULTIES:
            raise InvalidParamsError(
                f"invalid requirements: difficulty must be one of {list(DIFFICULTIES)}"
            )
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
                difficulty=coerce_difficulty(difficulty),
            )
        except ValueError as exc:
            raise InvalidParamsError(f"invalid requirements: {exc}") from exc
    raw_focus = arguments.get("review_focus")
    if raw_focus is not None:
        if not isinstance(raw_focus, str):
            raise InvalidParamsError("review_focus must be text")
        requirements = replace(requirements, review_focus=raw_focus.strip() or None)
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
            kind=str(arguments.get("kind") or "BUILD"),
            deadline=arguments.get("deadline"),
            not_before=arguments.get("not_before"),
            depends_on=arguments.get("depends_on"),
            idempotency_key=arguments.get("idempotency_key"),
            caller_id=str(arguments.get("caller_id") or "orchestrator"),
            mcp_session_id=context.submit_session_id(),
        )
    except IdempotencyConflictError as exc:
        raise ToolError(f"idempotency conflict: {exc}") from exc
    except ValueError as exc:
        raise InvalidParamsError(f"invalid job: {exc}") from exc
    payload = {
        "v": SNAPSHOT_SCHEMA_VERSION,
        "result": _job_result(db, result.job),
        "guidance": _job_guidance(result.job, db),
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


def _submit_plan(context: ServerContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Activate a repository ``PLAN.md`` and submit its ready lanes (P2).

    Path-only: the handler reads the file from inside the caller's repository,
    never raw bytes over the wire, and returns a parsed summary.  Each ready
    lane becomes an ordinary job; dependent lanes wait for acceptance and
    ``run: "owner"`` lanes are never dispatched.
    """
    from saberops.plan_runner import PlanRunnerError, submit_plan_file
    from saberops.runtime_gate import register_lane_submit_gate

    register_lane_submit_gate()
    repo = _require_text(arguments, "repo", what="repo")
    plan_path = _require_text(arguments, "plan_path", what="plan_path")
    base_ref = arguments.get("base_ref")
    db = context.database(read_only=False)
    assert db is not None
    try:
        submission = submit_plan_file(
            db,
            repo=repo,
            plan_path=plan_path,
            base_ref=str(base_ref).strip() if base_ref else None,
            owner_initiated=True,
        )
    except PlanRunnerError as exc:
        raise InvalidParamsError(f"invalid plan: {exc}") from exc
    lanes = [
        {
            "id": view.lane_id,
            "state": view.state,
            "status": view.status,
            "reasons": list(view.reasons),
            "job_id": view.job_id,
            "owner": view.owner,
        }
        for view in submission.lanes
    ]
    payload: dict[str, Any] = {
        "v": SNAPSHOT_SCHEMA_VERSION,
        "plan_id": submission.plan.plan_id,
        "project_id": submission.plan.project_id,
        "version": submission.revision.version,
        "sha256": submission.revision.sha256,
        "submitted": list(submission.submitted),
        "paused": submission.paused,
        "error": submission.error,
        "lanes": lanes,
    }
    context.ledger.append(
        "plan_submitted",
        {
            "plan_id": submission.plan.plan_id,
            "project_id": submission.plan.project_id,
            "version": submission.revision.version,
            "sha256": submission.revision.sha256,
            "lane_ids": [view.lane_id for view in submission.lanes],
        },
        key=f"plan:{submission.plan.plan_id}:{submission.revision.sha256}",
    )
    for view in submission.lanes:
        if view.job_id is None:
            continue
        context.ledger.append(
            "plan_lane_submitted",
            {
                "plan_id": submission.plan.plan_id,
                "lane_id": view.lane_id,
                "job_id": view.job_id,
                "version": submission.revision.version,
            },
            key=(
                f"plan:{submission.plan.plan_id}:v{submission.revision.version}:"
                f"lane:{view.lane_id}"
            ),
        )
    return payload


def _job_guidance(job: Any, db: Any | None = None) -> str | None:
    from saberops.jobs.models import WaitingReason

    if job.failure_reason == "NO_ROUTE_FOR_REQUIREMENTS":
        if job.requirements.role.value != "BUILD":
            return "Only BUILD jobs dispatch; set requirements.role to BUILD."
        kind = getattr(job, "kind", None)
        if getattr(kind, "value", kind) == "SCOUT":
            return (
                "No build route offers enforced read-only execution, which a scout "
                "requires. Add a provider whose CLI enforces it (Codex today) to the "
                "build route with `orch routing add build <provider/model>`."
            )
        include = tuple(getattr(job.requirements, "model_family_include", ()) or ())
        if include:
            from saberops.mcp.guide import FAMILY_TOKEN_GUIDANCE

            return (
                f"{FAMILY_TOKEN_GUIDANCE} Add a matching build route with "
                "`orch routing add build <provider/model>`; inspect it with "
                "`orch routing show`."
            )
        return (
            "No build route satisfies the requirements. Configure one with "
            "`orch routing add build <provider/model>`; inspect with `orch routing show` "
            "and adjust capability requirements if needed. Only BUILD jobs dispatch."
        )
    waiting = getattr(job, "waiting_reason", None)
    depends_on = tuple(getattr(job, "depends_on", ()) or ())
    if waiting == WaitingReason.DEPENDENCY:
        return (
            "Waiting until the owner accepts these dependency jobs: "
            f"{', '.join(depends_on)}. It starts by itself after that; no action needed."
        )
    if waiting == WaitingReason.DEPENDENCY_FAILED:
        from saberops.jobs.dependencies import DEPENDENCY_FAILED_GUIDANCE

        return DEPENDENCY_FAILED_GUIDANCE
    if waiting == WaitingReason.AWAY_PARKED:
        return (
            "Away mode parked this task on its own branch. Answer the blocking "
            "owner question (or set the away preflight policy) and the job "
            "starts again; independent jobs were not stopped."
        )
    if waiting == WaitingReason.REDIRECTED:
        return (
            "This job was redirected: the stopped attempt's work is preserved as a "
            "checkpoint and the pending note travels in the continuation attempt's "
            "prompt. It starts on the next scheduler tick; no action is needed."
        )
    if getattr(job, "state", None) is not None and job.state.value == "FAILED":
        return (
            "The job failed. Resume it from chat with resume_job: from='scratch' "
            "for a fresh attempt, or from='last_candidate' to continue from the "
            "saved candidate. A note you pass reaches the next attempt."
        )
    if waiting == WaitingReason.QUOTA_EVIDENCE_REQUIRED:
        remedy = _latest_waiting_details(db, job).get("remedy")
        command = (
            str(remedy)
            if remedy
            else "orch quota set <provider> <pool> --remaining <percent>"
        )
        return (
            "This route needs quota evidence the engine cannot collect safely on "
            f"its own (for example Anthropic/Claude usage). Only the owner can set it: "
            f"run `{command}`. The job starts by itself on the next scheduler tick "
            "after the evidence is recorded."
        )
    return None


def _latest_waiting_details(db: Any, job: Any) -> dict[str, Any]:
    """The most recent durable waiting details for a job (F8/F9F evidence)."""
    if db is None:
        return {}
    try:
        from saberops.jobs import store

        for event in reversed(store.list_events(db, job.job_id)):
            if event.get("kind") != "waiting":
                continue
            payload = json.loads(str(event.get("payload_json") or "{}"))
            details = payload.get("details")
            if isinstance(details, dict):
                return details
    except Exception:  # noqa: BLE001 - guidance never breaks a status read
        return {}
    return {}


def _chosen_worker(db: Any, job: Any) -> dict[str, Any] | None:
    """The provider/model the build route actually dispatched for ``job``."""
    for run_id in reversed(job.run_ids):
        attempts = db.get_attempts_for_run(run_id)
        if attempts:
            attempt = attempts[-1]
            return {
                "provider": attempt.provider,
                "model": attempt.model,
                "route": f"{attempt.provider}/{attempt.model}",
            }
    return None


def _job_result(db: Any, job: Any) -> dict[str, Any]:
    """Read-only result projection, reusable by completion tools."""
    from saberops.jobs.service import job_usage_view, owner_action_view
    from saberops.scout import latest_report

    report: dict[str, Any] | None = None
    try:
        report = latest_report(db, job.job_id)
    except Exception:  # noqa: BLE001 - a report read never breaks a result view
        report = None
    owner_view = owner_action_view(db, job)
    result: dict[str, Any] = {
        "run_id": owner_view["run_id"],
        "candidate_sha": owner_view["candidate_sha"],
        "candidate_state": owner_view["candidate_state"],
        "diff": None,
        "owner_action": owner_view["owner_action"],
        "worker": _chosen_worker(db, job),
        "report": report,
        "usage": job_usage_view(db, job),
    }
    if owner_view["candidate_sha"] is not None:
        result["diff"] = {
            "repo": job.repo,
            "base_sha": job.base_sha,
            "candidate_sha": owner_view["candidate_sha"],
        }
    return result


def _get_job(context: ServerContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    from saberops.jobs.service import get_job, job_status_view
    from saberops.runtime_gate import gate_facts

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
        "guidance": _job_guidance(job, db),
        # D2: a paused store is a fact about the job's progress, so every
        # reader sees why nothing moves and the one call that resumes it --
        # before it waits out a timeout and reports no owner action.
        "pause": gate_facts(db=db),
    }


def _promote_scout(context: ServerContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Start a build job carrying a scout report as its starting context (F19)."""
    from saberops.jobs.service import (
        IdempotencyConflictError,
        JobNotFoundError,
        promote_scout,
    )

    scout_job_id = _require_text(arguments, "job_id", what="job_id")
    db = context.database(read_only=False)
    try:
        result = promote_scout(
            db,
            scout_job_id=scout_job_id,
            goal=arguments.get("goal"),
            acceptance=(
                "\n".join(arguments["acceptance"])
                if isinstance(arguments.get("acceptance"), list)
                else arguments.get("acceptance")
            ),
            base_ref=str(arguments.get("base_ref") or "HEAD"),
            idempotency_key=arguments.get("idempotency_key"),
            caller_id=str(arguments.get("caller_id") or "orchestrator"),
        )
    except JobNotFoundError as exc:
        raise NotFoundError(str(exc), data={"job_id": scout_job_id}) from exc
    except IdempotencyConflictError as exc:
        raise ToolError(f"idempotency conflict: {exc}") from exc
    except ValueError as exc:
        raise InvalidParamsError(f"cannot promote scout: {exc}") from exc
    payload = {
        "v": SNAPSHOT_SCHEMA_VERSION,
        "result": _job_result(db, result.job),
        "guidance": _job_guidance(result.job, db),
        "job_id": result.job.job_id,
        "state": result.job.state.value,
        "created": result.created,
        "job": result.job.to_dict(),
    }
    context.ledger.append(
        "scout_promoted",
        {"scout_job_id": scout_job_id, "job_id": result.job.job_id},
        key=f"scout-promoted:{scout_job_id}:{result.job.job_id}",
    )
    return payload


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
        # F26: advice is visible while the job runs, from the same record
        # catch_up and get_job read.
        "advisor": view.get("advisor"),
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


def _catch_up_switch(db: Any, run_id: str, attempts: list[Any]) -> dict[str, Any] | None:
    """The latest handoff from one attempt to its replacement, for ``catch_up``.

    F4: when a subscription runs out, the run continues from the interrupted
    attempt's checkpoint on a different binding.  That switch is durable
    evidence (attempt rows, worker checkpoints, dispatch evidence); this
    projection only reads it, so the owner sees the handoff without opening
    the run page.
    """
    from saberops.models import AttemptStatus

    if len(attempts) < 2:
        return None
    checkpoints = {
        checkpoint.source_attempt_id: checkpoint
        for checkpoint in db.get_worker_checkpoints_for_run(run_id)
    }
    dispatches = {
        dispatch.attempt_id: dispatch
        for dispatch in db.get_dispatch_evidence_for_run(run_id)
        if dispatch.attempt_id is not None
    }
    # The latest resource interruption is the handoff the owner cares about;
    # later repair attempts on the same binding must not hide it.
    pair: tuple[Any, Any] | None = None
    for index in range(len(attempts) - 1, 0, -1):
        previous, following = attempts[index - 1], attempts[index]
        if (
            previous.status in (AttemptStatus.FAILED, AttemptStatus.TIMEOUT)
            and previous.interruption_reason
        ):
            pair = (previous, following)
            break
    if pair is None:
        return None
    from_attempt, to_attempt = pair
    checkpoint = checkpoints.get(from_attempt.id)
    dispatch = dispatches.get(to_attempt.id)
    inherited = getattr(dispatch, "inherited_checkpoint_sha", None)
    from_binding = getattr(dispatches.get(from_attempt.id), "binding_id", None)
    to_binding = getattr(dispatch, "binding_id", None)
    if from_binding and to_binding:
        different_binding: bool | None = from_binding != to_binding
    else:
        different_binding = None
    try:
        preserved = bool(from_attempt.worktree_path) and Path(
            from_attempt.worktree_path
        ).is_dir()
    except OSError:
        preserved = False
    return {
        "reason": from_attempt.interruption_reason or "attempt_failed",
        "from": {
            "attempt_id": from_attempt.id,
            "provider": from_attempt.provider,
            "model": from_attempt.model,
            "binding_id": from_binding,
        },
        "to": {
            "attempt_id": to_attempt.id,
            "provider": to_attempt.provider,
            "model": to_attempt.model,
            "binding_id": to_binding,
        },
        "different_provider": (from_attempt.provider, from_attempt.model)
        != (to_attempt.provider, to_attempt.model),
        "different_binding": different_binding,
        "checkpoint_sha": checkpoint.checkpoint_sha if checkpoint is not None else None,
        "inherited_checkpoint_sha": inherited,
        "continued_from_checkpoint": (
            checkpoint is not None
            and inherited is not None
            and inherited == checkpoint.checkpoint_sha
        ),
        "work_location": from_attempt.worktree_path,
        "work_location_preserved": preserved,
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
                "critical": view["critical"],
                "critical_reasons": view["critical_reasons"],
                "run_id": view["run_id"],
                "blocks": view["blocks"],
                "options": view["options"],
                "recommendation": request.escalation.recommendation,
            }
        )
    return matched


def _catch_up_self_answered(context: ServerContext, job: Any) -> list[dict[str, Any]]:
    """Routine decisions the planning session took for this job (read-only)."""
    from saberops.questions import question_return_summary

    summary = question_return_summary(
        context.question_queue(),
        run_ids=tuple(job.run_ids),
        blocks=(job.job_id,),
    )
    return [item.to_mapping() for item in summary.self_answered]


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
        if question["answerable_by"] == "PLAN" and not question.get("critical", False):
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
                f"Question {question['question_id']} is critical and only the owner "
                "can answer it; it notifies and waits."
                if question.get("critical", False)
                else (
                    f"Question {question['question_id']} blocks this job and only the "
                    "owner can answer it."
                )
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
    # H6 (ADR-0016): a note that never reached a worker blocks acceptance, so
    # the one next action names both ways out instead of an accept the engine
    # will refuse.  A running job may also redirect to deliver it now.
    from saberops.steering import note_resolution_view

    pending_notes = [
        note for note in view.get("steering_notes") or [] if note.get("pending")
    ]
    pending_ids = [int(note["note_id"]) for note in pending_notes]
    if pending_notes and job.state.value == "RUNNING":
        resolution = note_resolution_view(job.job_id, pending_ids)
        return {
            "action": "redirect_or_wait",
            "owner_only": False,
            "detail": (
                f"{len(pending_notes)} steering note(s) are pending: they reach the "
                "worker only at the next attempt, and a candidate cannot be accepted "
                "while they are undelivered. Call redirect_job to stop the current "
                "attempt, keep its work and deliver the note now; otherwise let the "
                "note ride the next attempt."
            ),
            "command": None,
            "redirect": resolution["redirect"],
        }
    if pending_notes:
        resolution = note_resolution_view(job.job_id, pending_ids)
        return {
            "action": "resolve_steering_note",
            "owner_only": False,
            "detail": str(resolution["detail"]),
            "command": str(resolution["discard"]["cli"] or ""),
            "redirect": resolution["redirect"],
            "discard": resolution["discard"],
        }
    report = result.get("report")
    if isinstance(report, dict) and report.get("report_path"):
        return {
            "action": "read_scout_report",
            "owner_only": False,
            "detail": (
                f"The scout report is at {report['report_path']} "
                f"({report.get('summary') or 'no summary'}). Promote it into a build "
                "job when the owner wants the work done."
            ),
            "command": f"orch promote-scout {job.job_id}",
            "report_path": report["report_path"],
            "promote_command": f"orch promote-scout {job.job_id}",
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
            "action": "resume_job",
            "owner_only": False,
            "detail": (
                f"The job failed ({view.get('reason') or job.failure_reason}). "
                f"Inspect the preserved work at {work}, then call resume_job: "
                "from='scratch' for a fresh attempt, or from='last_candidate' when "
                "the run saved a candidate. Pass note=... to carry the rejection "
                "or correction into the next attempt."
            ),
            "command": f"orch resume-job {job.job_id} --from scratch",
            "resume": {
                "tool": "resume_job",
                "job_id": job.job_id,
                "from": ["scratch", "last_candidate"],
                "cli": f"orch resume-job {job.job_id} --from scratch",
            },
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


def _catch_up_return_summary(
    context: ServerContext, *, advisor: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """The one durable "while you were away" summary, shared with the notifier.

    Read-only: the journal part is append-only away state, and the ``plans``
    section reuses the projection behind ``STATE.md``.  A killed scheduler and a
    fresh CLI session therefore read exactly the same records, and closing the
    planner never loses the plan's next action.  When the caller passes the
    job's advisor projection, the same record is attached here too (F26), so the
    return summary never disagrees with `catch_up`.
    """
    try:
        from saberops.away import build_return_summary
        from saberops.plan_state import plan_return_summaries

        db = context.database(read_only=True)
        plans = plan_return_summaries(db) if db is not None else ()
        payload = build_return_summary(plans=plans, db=db).as_dict()
        if advisor is not None:
            payload["advisor"] = dict(advisor)
        return payload
    except Exception as exc:
        return {"error": f"{exc.__class__.__name__}: {exc}"}


def _catch_up(context: ServerContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Read-only "catch me up" lookup: saved request, candidate and next action.

    Reuses :func:`_job_result` (C5) so the owner action has exactly one
    projection.  Nothing is written: no job/run/event/question mutation, no
    notice, and no planning lease.  A session that does not hold the writer
    lease can call it.
    """
    from saberops.candidate_lifecycle import project_candidate_state
    from saberops.jobs.service import get_job, job_status_view
    from saberops.runtime_gate import gate_facts

    plan_id = arguments.get("plan_id")
    if plan_id is not None:
        return _catch_up_plan(
            context, _require_text(arguments, "plan_id", what="plan_id")
        )
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
    switch = _catch_up_switch(db, run_id, attempts) if run_id is not None else None

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
        "switch": switch,
        # F17: what the gate is holding, so a returning session sees the pause
        # and the one call that resumes it before it assumes work is running.
        # Stable facts only: the reader's own presence would make two sessions
        # disagree about the same store (ADR-0011 §5); `get_pause` has it.
        "pause": gate_facts(db=db),
        "open_questions": questions,
        "self_answered": _catch_up_self_answered(context, job),
        "steering_notes": _catch_up_steering_notes(db, job),
        # F26: advisor route, calls and advice for this job.
        "advisor": view.get("advisor"),
        "return_summary": _catch_up_return_summary(
            context, advisor=view.get("advisor")
        ),
        "failure": failure,
        "report": result.get("report"),
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
        pause = result.get("pause") or {}
        if pause.get("closed"):
            # D2: a closed work gate means this job cannot move, however long
            # the caller waits.  Report the pause and how to resume instead of
            # a timeout that reads as "no owner action is pending".
            since = pause.get("since")
            detail = pause.get("detail")
            return {
                **result,
                "timed_out": False,
                "outcome": "paused",
                "reason": (
                    f"{pause.get('label') or 'the work gate is closed'}"
                    f"{f' since {since}' if since else ''}"
                    f"{f' ({detail})' if detail else ''}"
                ),
                "resume": {
                    "command": pause.get("resume_command", "orch resume"),
                    "tool": pause.get("resume_tool"),
                },
            }
        if job.get("waiting_reason") == "QUOTA_EVIDENCE_REQUIRED":
            # D9: waiting cannot clear this on its own; the owner must record
            # the quota evidence (the guidance names the exact command).
            return {
                **result,
                "timed_out": False,
                "outcome": "needs_attention",
                "reason": result.get("guidance")
                or "This job needs quota evidence only the owner can set.",
            }
        if job.get("waiting_reason") == "DEPENDENCY_FAILED":
            # The job can never start, so waiting cannot change anything.
            # Return immediately instead of burning the caller's timeout.
            from saberops.jobs.dependencies import DEPENDENCY_FAILED_GUIDANCE

            return {
                **result,
                "timed_out": False,
                "outcome": "needs_attention",
                "reason": DEPENDENCY_FAILED_GUIDANCE,
            }
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


def _redirect_job(context: ServerContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Stop the running attempt, keep its work and continue with the note (H6)."""
    from saberops.jobs.service import JobNotFoundError, redirect_job

    job_id = _require_text(arguments, "job_id", what="job_id")
    text = _require_text(arguments, "text", what="text")
    db = context.database(read_only=False)
    try:
        result = redirect_job(db, job_id, text)
    except JobNotFoundError as exc:
        raise NotFoundError(f"unknown job: {job_id}", data={"job_id": job_id}) from exc
    except ValueError as exc:
        raise InvalidParamsError(f"cannot redirect job: {exc}") from exc
    context.ledger.append(
        "job_redirected",
        {
            "job_id": job_id,
            "note_id": result.note.note_id,
            "stopped_run_id": result.stopped_run_id,
            "checkpoint_sha": result.checkpoint_sha,
        },
        key=f"job-redirected:{job_id}:{result.note.note_id}",
    )
    return {
        "v": SNAPSHOT_SCHEMA_VERSION,
        "job_id": job_id,
        "note_id": result.note.note_id,
        "note": result.note.to_dict(),
        "stopped_run_id": result.stopped_run_id,
        "checkpoint_sha": result.checkpoint_sha,
        "handoff_id": result.handoff_id,
        "state": result.job.state.value,
        "waiting_reason": (
            result.job.waiting_reason.value if result.job.waiting_reason is not None else None
        ),
        "delivery": (
            "the stopped attempt's work is preserved as a checkpoint; the next "
            "scheduled attempt restores it and carries the note in its prompt"
        ),
    }


def _discard_note(context: ServerContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Deliberately drop one undelivered note so it stops blocking acceptance."""
    from saberops.jobs.service import discard_note

    note_id = int(arguments.get("note_id", 0))
    db = context.database(read_only=False)
    try:
        note = discard_note(db, note_id)
    except ValueError as exc:
        raise InvalidParamsError(f"cannot discard note: {exc}") from exc
    context.ledger.append(
        "job_note_discarded",
        {"job_id": note.job_id, "note_id": note.note_id},
        key=f"job-note-discarded:{note.note_id}",
    )
    return {
        "v": SNAPSHOT_SCHEMA_VERSION,
        "job_id": note.job_id,
        "note_id": note.note_id,
        "note": note.to_dict(),
    }


def _start_walkthrough(context: ServerContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    from saberops.walkthrough import build_walkthrough

    repo = _require_text(arguments, "repo", what="repo")
    payload = build_walkthrough(Path(repo), context.db_path).as_dict()
    payload["v"] = SNAPSHOT_SCHEMA_VERSION
    return payload


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
        "critical_count": sum(1 for question in questions if question.critical),
        "questions": [question_view(question) for question in questions],
    }


def _answer_question(context: ServerContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    from saberops.owner_mode import OwnerModeError, decide_question_answer
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
            "through the CLI or the local web surface",
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
    if (
        question.request.raised_by_role is AskRole.PLAN
        or answered_by == question.request.raised_by
    ):
        raise ToolError(
            "an Orchestrator may never answer its own finding",
            data={"ref": ref},
        )
    try:
        decision = decide_question_answer(question, planner_present=True)
    except (OwnerModeError, ValueError) as exc:
        raise ToolError(f"owner mode unavailable: {exc}") from exc
    if not decision.allowed:
        raise ForbiddenOperationError(
            f"the planning session may not answer question {question.question_id}: "
            f"{decision.reason}",
            data={
                "ref": ref,
                "critical": decision.critical,
                "disposition": decision.disposition.value,
                "mode": decision.mode.value,
            },
        )
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
            "critical": decision.critical,
            "mode": decision.mode.value,
        },
    )
    return {
        "v": SNAPSHOT_SCHEMA_VERSION,
        "question": question_view(updated),
        "answer_policy": decision.as_dict(),
    }


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


def _resume_job(context: ServerContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Resume one failed job: from scratch or from its saved candidate (H7)."""
    from saberops.service import OrchestratorService, RunOperationError

    job_id = _require_text(arguments, "job_id", what="job_id")
    db = context.database(read_only=False)
    try:
        result = OrchestratorService(db=db).resume_job(
            job_id,
            from_=str(arguments.get("from") or "scratch"),
            note=(
                str(arguments["note"])
                if isinstance(arguments.get("note"), str)
                else None
            ),
            worker_binding=(
                str(arguments["worker_binding"])
                if isinstance(arguments.get("worker_binding"), str)
                else None
            ),
        )
    except RunOperationError as exc:
        raise ToolError(str(exc)) from exc
    context.ledger.append(
        "job_resumed",
        {
            "job_id": job_id,
            "run_id": result.get("run_id"),
            "source_run_id": result.get("source_run_id"),
            "from": result.get("from"),
        },
        key=f"job-resumed:{job_id}:{result.get('run_id')}",
    )
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


def _get_mode(context: ServerContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Read the effective owner mode; never changes it (F1)."""
    from saberops.owner_mode import OwnerModeError, mode_view

    try:
        view = mode_view()
    except (OwnerModeError, ValueError) as exc:
        raise ToolError(f"owner mode unavailable: {exc}") from exc
    return {"v": SNAPSHOT_SCHEMA_VERSION, **view}


def _get_pause(context: ServerContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Read the F17 work gate: closed, why, what is paused, how to resume."""
    from saberops.runtime_gate import gate_view

    db = context.database(read_only=True)
    return {"v": SNAPSHOT_SCHEMA_VERSION, **gate_view(db=db)}


def _ask_advisor(context: ServerContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Ask the project's configured advisor one bounded question (ADR-0034).

    The tool exists only while the session's project has an advisor route; an
    off route, a paused store or a spent per-job budget is a typed refusal.
    """
    from saberops.advisor import AdvisorRefused, ask_advisor
    from saberops.jobs.service import get_job
    from saberops.workers.registry import AdapterRegistry

    job_id = _require_text(arguments, "job_id", what="job_id")
    question = _require_text(arguments, "question", what="question")
    db = context.database(read_only=False)
    if db is None:  # pragma: no cover - a writable context always has a database
        raise ToolError("owner database is unavailable")
    job = get_job(db, job_id)
    if job is None:
        raise NotFoundError(f"unknown job: {job_id}", data={"job_id": job_id})
    run_ids = [run_id for run_id in getattr(job, "run_ids", ()) if run_id]
    run_id = job.current_run_id or (run_ids[-1] if run_ids else None)
    if not run_id:
        raise ToolError(
            f"job {job_id} has no run yet; the advisor can only answer about a "
            "started run",
            data={"job_id": job_id},
        )
    run = db.get_run(run_id)
    if run is None:
        raise NotFoundError(f"unknown run for job: {run_id}", data={"run_id": run_id})
    attempts = db.get_attempts_for_run(run_id)
    attempt = attempts[-1] if attempts else None
    try:
        advice = ask_advisor(
            db,
            repo=job.repo,
            run_id=run_id,
            registry=AdapterRegistry.default(),
            attempt_id=attempt.id if attempt is not None else None,
            task=run.task,
            question=question,
            attempts=attempts,
            base_sha=(
                (attempt.base_sha if attempt is not None else None) or run.base_commit
            ),
            prior_worktree=attempt.worktree_path if attempt is not None else None,
        )
    except AdvisorRefused as exc:
        raise ToolError(f"{exc.code}: {exc}", data={"code": exc.code}) from exc
    context.ledger.append(
        "advisor_advice",
        {
            "job_id": job_id,
            "run_id": run_id,
            "call_id": advice.call_id,
            "provider": advice.provider,
            "model": advice.model,
        },
        key=f"advisor:{run_id}:{advice.call_id}",
    )
    return {
        "v": SNAPSHOT_SCHEMA_VERSION,
        "job_id": job_id,
        "run_id": run_id,
        "call_id": advice.call_id,
        "provider": advice.provider,
        "model": advice.model,
        "advice": advice.advice,
    }


def _propose_mode_change(
    context: ServerContext, arguments: Mapping[str, Any]
) -> dict[str, Any]:
    """Raise an owner question for a mode change; never flips the mode (F1)."""
    from saberops.owner_mode import (
        OWNER_MODE_ORDER,
        OwnerMode,
        OwnerModeError,
        mode_view,
        normalize_until,
        parse_owner_mode,
    )
    from saberops.questions import (
        AskRole,
        DecisionClass,
        Escalation,
        QuestionError,
        QuestionRequest,
        question_view,
    )

    try:
        mode = parse_owner_mode(arguments.get("mode"))
        until = normalize_until(arguments.get("until"))
    except OwnerModeError as exc:
        raise InvalidParamsError(str(exc)) from exc
    if until is not None and mode is not OwnerMode.AWAY:
        raise InvalidParamsError("until is only valid with away")
    rationale = _require_text(arguments, "rationale", what="rationale")
    proposed_by = _require_text(arguments, "proposed_by", what="proposed_by")
    proposed = f"set the owner mode to {mode.value}"
    if until is not None:
        proposed += f" until {until}"
    request = QuestionRequest(
        key=f"owner-mode-change:{mode.value}",
        subject=f"Change the owner mode to {mode.value}",
        kind="owner_mode_change",
        decision_class=DecisionClass.OTHER,
        raised_by_role=AskRole.PLAN,
        raised_by=proposed_by,
        escalation=Escalation(
            requirement=(
                "Only the owner switches the SaberOps owner mode; the planning "
                "session may propose but never set it."
            ),
            proposed_expansion=proposed,
            smallest_compliant="Keep the current mode until the owner decides.",
            consequences=(
                "Nothing changes from this proposal: work continues under the "
                "current mode, and the owner may still decline."
            ),
            recommendation=rationale,
        ),
        options=tuple(item.value for item in OWNER_MODE_ORDER) + ("keep-current",),
        default_option="keep-current",
    )
    try:
        question = context.question_queue().ask(request)
    except QuestionError as exc:
        raise ToolError(f"mode-change question was not recorded: {exc}") from exc
    context.ledger.append(
        "mode_change_proposed",
        {
            "proposed_mode": mode.value,
            "until": until,
            "proposed_by": proposed_by,
            "question_id": question.question_id,
            "applied": False,
        },
    )
    return {
        "v": SNAPSHOT_SCHEMA_VERSION,
        "applied": False,
        "proposed_mode": mode.value,
        "until": until,
        "current": mode_view(),
        "question": question_view(question),
        "note": (
            "a proposal changes nothing; only the owner sets the mode with "
            "`orch mode set` or the Questions page"
        ),
    }


def _plan_status(
    context: ServerContext, arguments: Mapping[str, Any]
) -> dict[str, Any]:
    """Read-only plan ledger: the exact projection behind ``STATE.md``.

    Works from a read-only client, takes no lease, writes nothing (the canonical
    file is the scheduler tick's job) and never acts on the view.
    """
    from saberops.plan_state import PlanStateError, find_plan, project_plan_state

    plan_id = arguments.get("plan_id")
    repo = arguments.get("repo")
    if plan_id is None and repo is None:
        raise InvalidParamsError("plan_id or repo is required")
    db = context.database(read_only=True)
    if db is None:
        raise NotFoundError("no SaberOps store yet", data={})
    try:
        plan = find_plan(
            db,
            plan_id=_require_text(arguments, "plan_id", what="plan_id")
            if plan_id is not None
            else None,
            repo=repo.strip() if isinstance(repo, str) and repo.strip() else None,
        )
    except PlanStateError as exc:
        raise InvalidParamsError(str(exc)) from exc
    if plan is None:
        raise NotFoundError(
            f"no active plan for {plan_id or repo}", data={"plan_id": plan_id, "repo": repo}
        )
    payload = project_plan_state(db, plan).to_dict()
    payload["v"] = SNAPSHOT_SCHEMA_VERSION
    return payload


def _catch_up_plan(context: ServerContext, plan_id: str) -> dict[str, Any]:
    """Read-only plan handoff: header, lane table, open questions, next action."""
    from saberops.jobs import store as job_store
    from saberops.plan_state import find_plan, project_plan_state

    db = context.database(read_only=True)
    if db is None:
        raise NotFoundError(f"unknown plan: {plan_id}", data={"plan_id": plan_id})
    plan = find_plan(db, plan_id=plan_id)
    if plan is None:
        raise NotFoundError(f"unknown plan: {plan_id}", data={"plan_id": plan_id})
    projection = project_plan_state(db, plan)
    state = projection.to_dict()
    questions: list[dict[str, Any]] = []
    for lane in projection.lanes:
        if lane.job_id is None:
            continue
        job = job_store.get_job(db, lane.job_id)
        if job is None:
            continue
        matched = _catch_up_questions(context, job)
        if matched:
            questions.append(
                {"lane_id": lane.lane_id, "job_id": job.job_id, "questions": matched}
            )
    return {
        "v": SNAPSHOT_SCHEMA_VERSION,
        "plan_id": plan.plan_id,
        "project_id": plan.project_id,
        "revision": plan.active_version,
        "state": state,
        "lanes": state["lanes"],
        "open_questions": questions,
        "next_action": projection.next_action,
    }


# --------------------------------------------------------------------------- #
# Registry construction
# --------------------------------------------------------------------------- #


def _native_error(exc: Exception) -> ToolError:
    from saberops.native_workers import NativeWorkerError

    if isinstance(exc, NativeWorkerError):
        return ToolError(str(exc), data={"code": exc.code, **exc.data})
    return ToolError(str(exc))


def _native_claim(context: ServerContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Claim a queued job for a native child of the orchestrating CLI (F16)."""
    from saberops.native_workers import claim_native_job

    db = context.database(read_only=False)
    assert db is not None
    job_id = _require_text(arguments, "job_id", what="job_id")
    try:
        result = claim_native_job(
            db,
            job_id,
            vendor=arguments.get("vendor"),
            model=arguments.get("model"),
        )
    except Exception as exc:
        raise _native_error(exc) from exc
    context.ledger.append(
        "native_claimed",
        {
            "job_id": job_id,
            "run_id": result["run_id"],
            "vendor": result["vendor"],
            "model": result["model"],
        },
    )
    return {"v": SNAPSHOT_SCHEMA_VERSION, **result}


def _native_heartbeat(context: ServerContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Record native liveness and deliver steering/stop state (F16)."""
    from saberops.native_workers import native_heartbeat

    db = context.database(read_only=False)
    assert db is not None
    try:
        return {
            "v": SNAPSHOT_SCHEMA_VERSION,
            **native_heartbeat(
                db,
                _require_text(arguments, "job_id", what="job_id"),
                progress=arguments.get("progress"),
            ),
        }
    except Exception as exc:
        raise _native_error(exc) from exc


def _native_controls(context: ServerContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Read native control state; posts a stop when the deadline passed (F16)."""
    from saberops.native_workers import native_controls

    db = context.database(read_only=False)
    assert db is not None
    try:
        return {
            "v": SNAPSHOT_SCHEMA_VERSION,
            **native_controls(db, _require_text(arguments, "job_id", what="job_id")),
        }
    except Exception as exc:
        raise _native_error(exc) from exc


def _record_native_attempt(context: ServerContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Verify, gate and independently review one native report (F16)."""
    from saberops.native_workers import record_native_attempt

    db = context.database(read_only=False)
    assert db is not None
    try:
        result = record_native_attempt(
            db,
            _require_text(arguments, "job_id", what="job_id"),
            vendor=arguments.get("vendor"),
            model=arguments.get("model"),
            base_sha=arguments.get("base_sha"),
            candidate_sha=arguments.get("candidate_sha"),
            worktree=arguments.get("worktree"),
            transcript=arguments.get("transcript"),
            started_at=arguments.get("started_at"),
            finished_at=arguments.get("finished_at"),
            usage=arguments.get("usage") if isinstance(arguments.get("usage"), Mapping) else None,
        )
    except Exception as exc:
        raise _native_error(exc) from exc
    context.ledger.append(
        "native_attempt_recorded",
        {
            "job_id": result["job_id"],
            "run_id": result["run_id"],
            "candidate_sha": result["candidate_sha"],
        },
    )
    return {"v": SNAPSHOT_SCHEMA_VERSION, **result}


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
                "kind": {"type": "string", "enum": ["BUILD", "SCOUT"]},
                "deadline": {"type": "string"},
                "not_before": {"type": "string"},
                "depends_on": {"type": "array", "items": {"type": "string", "minLength": 1}},
                "idempotency_key": {"type": "string"},
                "caller_id": {"type": "string"},
                "review_focus": {
                    "type": "string",
                    "description": (
                        "Optional focus for the independent reviewer (for "
                        "example new suppressions or broad handlers). It can "
                        "add attention, never turn review off."
                    ),
                },
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
                        "difficulty": {"type": "string", "enum": _DIFFICULTIES},
                    }
                ),
            },
            required=("repo", "goal"),
        ),
        _submit_job,
        mutating=True,
    )
    register(
        "submit_plan",
        "Activate a repository PLAN.md (path-only) and submit every lane whose "
        "dependencies are accepted; owner-held lanes are never dispatched.",
        _schema(
            {
                "repo": {"type": "string", "minLength": 1},
                "plan_path": {"type": "string", "minLength": 1},
                "base_ref": {"type": "string", "minLength": 1},
            },
            required=("repo", "plan_path"),
        ),
        _submit_plan,
        mutating=True,
    )
    register(
        "promote_scout",
        "Start a build job whose starting context is a completed scout's report; "
        "records the lineage on both jobs.",
        _schema(
            {
                "job_id": {"type": "string", "minLength": 1},
                "goal": {"type": "string"},
                "acceptance": {"type": ["string", "array"], "items": {"type": "string"}},
                "base_ref": {"type": "string", "minLength": 1},
                "idempotency_key": {"type": "string"},
                "caller_id": {"type": "string"},
            },
            required=("job_id",),
        ),
        _promote_scout,
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
        "resume_job",
        "Resume one failed job: from scratch (a fresh attempt of its frozen task) "
        "or from last_candidate (continue from its saved candidate). Optional note "
        "reaches the next attempt; an exact blocked worker binding is the folded-in "
        "try-worker option. One successor per call; a running job returns its successor.",
        _schema(
            {
                "job_id": {"type": "string", "minLength": 1},
                "from": {
                    "type": "string",
                    "enum": ["scratch", "last_candidate"],
                },
                "note": {"type": "string", "minLength": 1, "maxLength": 4000},
                "worker_binding": {"type": "string", "minLength": 1},
            },
            required=("job_id",),
        ),
        _resume_job,
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
        "question and the next allowed action for one job. Pass plan_id instead for the "
        "plan header, lane table, per-lane open questions and next owner action.",
        _schema(
            {
                "job_id": {"type": "string", "minLength": 1},
                "plan_id": {"type": "string", "minLength": 1},
            }
        ),
        _catch_up,
        mutating=False,
    )
    register(
        "plan_status",
        "Read-only plan ledger: the exact projection rendered into STATE.md (lane "
        "status, worker, branch, commit, evidence and next owner action). Writes nothing.",
        _schema(
            {
                "plan_id": {"type": "string", "minLength": 1},
                "repo": {"type": "string", "minLength": 1},
            }
        ),
        _plan_status,
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
        "redirect_job",
        "Stop a RUNNING job's current attempt, preserve its work and continue with "
        "the note: the next attempt restores that work and carries the note in its "
        "prompt. Use note_job when the next attempt is fine; a candidate with an "
        "undelivered note cannot be accepted.",
        _schema(
            {
                "job_id": {"type": "string", "minLength": 1},
                "text": {"type": "string", "minLength": 1, "maxLength": 4000},
            },
            required=("job_id", "text"),
        ),
        _redirect_job,
        mutating=True,
    )
    register(
        "discard_note",
        "Deliberately drop one undelivered steering note so it no longer blocks "
        "acceptance. A delivered note is evidence and cannot be discarded.",
        _schema(
            {"note_id": {"type": "integer", "minimum": 1}},
            required=("note_id",),
        ),
        _discard_note,
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
        "start_walkthrough",
        "Plain-language first-run walkthrough: a short introduction, then one setup "
        "step at a time from preflight, with check marks. Resume by calling it again.",
        _schema({"repo": {"type": "string", "minLength": 1}}, required=("repo",)),
        _start_walkthrough,
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
    register(
        "get_mode",
        "Read the effective owner mode, its permissions and every decision it permits.",
        _schema({}),
        _get_mode,
        mutating=False,
    )
    register(
        "propose_mode_change",
        "Propose an owner mode change as an owner question; never changes the mode.",
        _schema(
            {
                "mode": {
                    "type": "string",
                    "enum": _OWNER_MODE_VALUES,
                },
                "until": {"type": "string"},
                "rationale": {"type": "string", "minLength": 1},
                "proposed_by": {"type": "string", "minLength": 1},
            },
            required=("mode", "rationale", "proposed_by"),
        ),
        _propose_mode_change,
        mutating=True,
    )
    register(
        "get_pause",
        "Read the work gate: whether work is paused, why, what is parked and how to resume.",
        _schema({}),
        _get_pause,
        mutating=False,
    )
    from saberops.advisor import advisor_route_or_none

    # F26 (ADR-0034): the advisor is optional and off by default.  "Off"
    # removes the tool entirely for this project session rather than offering
    # a call that can only refuse.
    if context.repo is not None and advisor_route_or_none(context.repo) is not None:
        register(
            "ask_advisor",
            "Ask the project's configured advisor one short question about a job's "
            "current run; it reads a disposable worktree and returns advice only. "
            "Off unless the owner set an advisor route; bounded per job.",
            _schema(
                {
                    "job_id": {"type": "string", "minLength": 1},
                    "question": {"type": "string", "minLength": 1, "maxLength": 2000},
                },
                required=("job_id", "question"),
            ),
            _ask_advisor,
            mutating=False,
        )

    from saberops.native_workers import NATIVE_TOOL_NAMES, native_enabled

    if native_enabled():
        # F16: the hybrid native-worker surface exists only while the owner
        # flag is on.  Turning it off removes the tools entirely.
        register(
            "native_claim",
            "Claim a queued job for a native subagent of this CLI: SaberOps prepares "
            "the worktree at the job's pinned base and returns it.",
            _schema(
                {
                    "job_id": {"type": "string", "minLength": 1},
                    "vendor": {"type": "string", "enum": ["claude", "codex", "opencode"]},
                    "model": {"type": "string", "minLength": 1, "maxLength": 200},
                },
                required=("job_id", "vendor", "model"),
            ),
            _native_claim,
            mutating=True,
        )
        register(
            "native_heartbeat",
            "Native liveness: record progress, deliver queued steering notes once, and "
            "return any stop request and the heartbeat deadline.",
            _schema(
                {
                    "job_id": {"type": "string", "minLength": 1},
                    "progress": {"type": "string", "maxLength": 500},
                },
                required=("job_id",),
            ),
            _native_heartbeat,
            mutating=True,
        )
        register(
            "native_controls",
            "Read native control state while waiting; a missed heartbeat deadline marks "
            "the job needs-attention and posts a stop for the orchestrator to act on.",
            _schema({"job_id": {"type": "string", "minLength": 1}}, required=("job_id",)),
            _native_controls,
            mutating=True,
        )
        register(
            "record_native_attempt",
            "Report a native candidate (vendor, model, base and candidate SHA, worktree). "
            "SaberOps verifies lineage, worktree and canonical isolation, runs the project "
            "gate and a different-family review, and records an attempt that can reach "
            "owner acceptance. Missing or mismatched evidence fails closed.",
            _schema(
                {
                    "job_id": {"type": "string", "minLength": 1},
                    "vendor": {"type": "string", "enum": ["claude", "codex", "opencode"]},
                    "model": {"type": "string", "minLength": 1, "maxLength": 200},
                    "base_sha": {"type": "string", "minLength": 40, "maxLength": 40},
                    "candidate_sha": {"type": "string", "minLength": 40, "maxLength": 40},
                    "worktree": {"type": "string", "minLength": 1},
                    "transcript": {"type": "string", "maxLength": 8000},
                    "started_at": {"type": "string"},
                    "finished_at": {"type": "string"},
                    "usage": {"type": "object"},
                },
                required=(
                    "job_id",
                    "vendor",
                    "model",
                    "base_sha",
                    "candidate_sha",
                    "worktree",
                ),
            ),
            _record_native_attempt,
            mutating=True,
        )

    registered = set(registry.names())
    expected = set(TOOL_NAMES) | (set(NATIVE_TOOL_NAMES) if native_enabled() else set())
    if "ask_advisor" in registered:
        expected.add("ask_advisor")
    if registered != expected:
        raise ToolError(
            "registry does not expose exactly the D7 tool set: "
            f"missing={sorted(expected - registered)} "
            f"extra={sorted(registered - expected)}"
        )
    if registered & set(FORBIDDEN_OPERATIONS):
        raise ToolError("a never-exposed operation was registered")
    return registry
