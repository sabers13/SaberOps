"""Away mode: owner-authored standing decisions wired into the scheduler.

`unattended.py` owns the policy projection but had no production
caller.  This module is the caller.  It adds the three durable pieces a run
path needs to act on that policy while the owner is away:

* **The standing-decision store** (``unattended-decisions.json``): the owner's
  bounded, exact-scope answers, written only by ``orch away grant`` and read by
  the scheduler.  Validation is delegated to :class:`unattended.StandingDecision`
  and :class:`unattended.DecisionSheet`, so wildcards, prefixes and unbounded
  grants cannot even be constructed.
* **The away journal** (``away-journal.jsonl``): append-only, ``fsync``-durable
  records of every disposition, plan impact, preflight result and outcome.  A
  scheduler killed after a decision resumes and still knows what it did; the
  return summary is rebuilt from these records, never from memory.
* **The return summary**: "while you were away", assembled from the journal and
  reused by ``catch_up`` and the desktop notifier.

Only ``away`` mode takes autonomous decisions here, and only the existing
authority preset's actions do anything; a standing decision never widens it and
nothing in this module can push or move the target branch.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from saberops.paths import owner_state_dir
from saberops.questions import (
    AnswerAuthority,
    AskRole,
    DecisionClass,
    OwnerQuestion,
    OwnerQuestionQueue,
    QuestionStatus,
)
from saberops.unattended import (
    AuthorityMode,
    Decision,
    DecisionDisposition,
    DecisionResolution,
    DecisionScope,
    DecisionSheet,
    NeverDelegableKind,
    NotificationChannel,
    PlanTaskState,
    PreflightRequest,
    PreflightResult,
    RunProgress,
    StandingDecision,
    build_morning_report,
    compute_plan_impact,
    resolve_decision,
    run_preflight,
)

__all__ = [
    "AWAY_JOURNAL_ENV",
    "AWAY_POLICY_ENV",
    "AWAY_STORE_ENV",
    "AwayError",
    "AwayJournal",
    "AwayPolicy",
    "DispatchGate",
    "ReturnSummary",
    "StandingDecisionStore",
    "answer_question_under_standing_decision",
    "build_return_summary",
    "decision_key_for_question",
    "deliver_return_summary",
    "evaluate_dispatch",
    "get_away_journal_path",
    "get_away_policy_path",
    "get_standing_decisions_path",
    "never_delegable_for_question",
    "project_scope_id",
    "question_scope",
    "record_job_outcome",
    "record_plan_impact",
    "record_preflight",
    "render_standing_decisions",
]

#: Test/diagnostics override for the standing-decision document.
AWAY_STORE_ENV = "ORCH_UNATTENDED_STORE"
#: Test/diagnostics override for the append-only away journal.
AWAY_JOURNAL_ENV = "ORCH_AWAY_JOURNAL"
#: Test/diagnostics override for the owner-authored away policy.
AWAY_POLICY_ENV = "ORCH_AWAY_POLICY"

STORE_VERSION: Final[int] = 1
JOURNAL_VERSION: Final[int] = 1
POLICY_VERSION: Final[int] = 1

#: The decision key a dispatch-time question is stored under.  A question key is
#: free text, while an unattended decision key is a bounded identifier, so the
#: deterministic digest is the bridge the owner and the engine both compute.
_QUESTION_KEY_PREFIX = "question."
_QUESTION_KEY_DIGEST = 24

#: Question classes that map exactly onto one never-delegable kind.  A class
#: without an exact correspondence is still owner-only or planner-answerable as
#: the question queue already decides it; it is never widened here.
_QUESTION_NEVER_DELEGABLE: Final[dict[DecisionClass, NeverDelegableKind]] = {
    DecisionClass.PUSH: NeverDelegableKind.PUBLISHING,
    DecisionClass.BILLING_OR_POSTURE: NeverDelegableKind.BUDGET_CAP,
    DecisionClass.TEST_CHANGE_AGAINST_ADR: NeverDelegableKind.TEST_GATE,
    DecisionClass.DELETE_OUTSIDE_ENGINE_FOLDERS: NeverDelegableKind.DESTRUCTIVE_OPERATIONS,
}


class AwayError(ValueError):
    """A durable away document is malformed or the request is invalid."""


def get_standing_decisions_path() -> Path:
    """Resolve the standing-decision document (web and scheduler share it)."""
    override = os.environ.get(AWAY_STORE_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return owner_state_dir() / "unattended-decisions.json"


def get_away_journal_path() -> Path:
    """Resolve the append-only away journal."""
    override = os.environ.get(AWAY_JOURNAL_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return owner_state_dir() / "away-journal.jsonl"


def get_away_policy_path() -> Path:
    """Resolve the owner-authored away preflight policy."""
    override = os.environ.get(AWAY_POLICY_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return owner_state_dir() / "away-policy.json"


def project_scope_id(repo: str | Path) -> str:
    """Return a stable, identifier-safe scope id for one repository path.

    The scope identity is local to away mode.  It is derived from the
    canonical path so a grant written for ``--repo`` matches the dispatch of a
    job on the same repository, and two different checkouts never share it.
    """
    raw = os.path.realpath(Path(repo).expanduser())
    slug = re.sub(r"[^a-z0-9]+", "-", Path(raw).name.lower()).strip("-") or "repo"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return f"{slug}-{digest}"


def decision_key_for_question(question_key: str) -> str:
    """Map a free-text question key onto a bounded unattended decision key."""
    if not isinstance(question_key, str) or not question_key.strip():
        raise AwayError("a question key is required")
    digest = hashlib.sha256(question_key.strip().encode("utf-8")).hexdigest()
    return f"{_QUESTION_KEY_PREFIX}{digest[:_QUESTION_KEY_DIGEST]}"


def never_delegable_for_question(
    decision_class: DecisionClass,
) -> NeverDelegableKind | None:
    """Return the exact never-delegable kind for a question class, if any."""
    return _QUESTION_NEVER_DELEGABLE.get(decision_class)


def question_scope(question: OwnerQuestion, job_id: str, repo: str | Path) -> DecisionScope:
    """Build the exact scope a dispatch-time question belongs to."""
    del question  # the scope is the job's, never a wildcard from question text
    return DecisionScope(
        project_id=project_scope_id(repo),
        task_id=job_id,
    )


def _now_iso(now: datetime | None = None) -> str:
    moment = now if now is not None else datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat()


def _atomic_write(path: Path, payload: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temp.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return path


class StandingDecisionStore:
    """The owner-authored standing decisions the scheduler reads."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path if path is not None else get_standing_decisions_path()

    def _load_payload(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"version": STORE_VERSION, "standing_decisions": []}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AwayError(f"standing decisions at {self.path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise AwayError(f"standing decisions at {self.path}: top level must be an object")
        if payload.get("version") != STORE_VERSION:
            raise AwayError(
                f"standing decisions at {self.path}: version must be {STORE_VERSION}"
            )
        raw = payload.get("standing_decisions")
        if not isinstance(raw, list):
            raise AwayError(f"standing decisions at {self.path}: missing standing_decisions list")
        return payload

    def decisions(self) -> tuple[StandingDecision, ...]:
        """Load and validate every standing decision, failing closed."""
        payload = self._load_payload()
        decisions: list[StandingDecision] = []
        for index, entry in enumerate(payload["standing_decisions"]):
            if not isinstance(entry, dict):
                raise AwayError(
                    f"standing decisions at {self.path}: entry {index} must be an object"
                )
            scope_raw = entry.get("scope")
            if not isinstance(scope_raw, dict):
                raise AwayError(
                    f"standing decisions at {self.path}: entry {index} needs a scope object"
                )
            paths = scope_raw.get("paths") or ()
            if not isinstance(paths, (list, tuple)):
                raise AwayError(
                    f"standing decisions at {self.path}: entry {index} paths must be a list"
                )
            try:
                decisions.append(
                    StandingDecision(
                        standing_decision_id=str(entry.get("standing_decision_id") or ""),
                        scope=DecisionScope(
                            project_id=str(scope_raw.get("project_id") or ""),
                            task_id=str(scope_raw.get("task_id") or ""),
                            paths=tuple(str(item) for item in paths),
                        ),
                        decision_key=str(entry.get("decision_key") or ""),
                        owner_words=str(entry.get("owner_words") or ""),
                        answered_by=str(entry.get("answered_by") or ""),
                        granted_at_turn=entry.get("granted_at_turn"),
                        ttl_turns=entry.get("ttl_turns"),
                        expires_at=entry.get("expires_at"),
                    )
                )
            except ValueError as exc:
                raise AwayError(
                    f"standing decisions at {self.path}: entry {index}: {exc}"
                ) from exc
        return tuple(decisions)

    def sheet(self, run_id: str, plan_reference: str = "away dispatch") -> DecisionSheet:
        """Freeze the current decisions into one run's decision sheet."""
        return DecisionSheet(
            run_id=run_id,
            plan_reference=plan_reference or "away dispatch",
            standing_decisions=self.decisions(),
        )

    def add(self, decision: StandingDecision) -> Path:
        """Append one owner decision, refusing a duplicate id or scope/key pair."""
        if not isinstance(decision, StandingDecision):
            raise AwayError("add() requires a StandingDecision")
        decisions = list(self.decisions())
        if any(item.standing_decision_id == decision.standing_decision_id for item in decisions):
            raise AwayError(
                f"standing decision {decision.standing_decision_id!r} already exists"
            )
        for item in decisions:
            if (
                item.decision_key == decision.decision_key
                and item.scope.matches_exactly(decision.scope)
            ):
                raise AwayError(
                    "a standing decision already answers this exact scope and decision key; "
                    "revoke it first"
                )
        decisions.append(decision)
        # DecisionSheet re-validates the whole set (limits, duplicates, bounds).
        DecisionSheet(
            run_id="owner",
            plan_reference="owner standing decisions",
            standing_decisions=tuple(decisions),
        )
        payload = {
            "version": STORE_VERSION,
            "standing_decisions": [item.as_dict() for item in decisions],
        }
        return _atomic_write(self.path, json.dumps(payload, indent=2) + "\n")

    def remove(self, standing_decision_id: str) -> Path:
        """Revoke one standing decision; the owner is the only writer."""
        existing = self.decisions()
        decisions = [
            item for item in existing if item.standing_decision_id != standing_decision_id
        ]
        if len(decisions) == len(existing):
            raise AwayError(f"unknown standing decision {standing_decision_id!r}")
        payload = {
            "version": STORE_VERSION,
            "standing_decisions": [item.as_dict() for item in decisions],
        }
        return _atomic_write(self.path, json.dumps(payload, indent=2) + "\n")


def render_standing_decisions(
    store: StandingDecisionStore | None = None,
    *,
    now: datetime | None = None,
    current_turn: int = 0,
) -> list[dict[str, Any]]:
    """Render every decision with its live/expired state for CLI and web."""
    resolved_store = store if store is not None else StandingDecisionStore()
    instant = _now_iso(now)
    rows: list[dict[str, Any]] = []
    for decision in resolved_store.decisions():
        rows.append(
            {
                **decision.as_dict(),
                "live": decision.is_live(now=instant, current_turn=current_turn),
            }
        )
    return rows


@dataclass(frozen=True)
class AwayPolicy:
    """The owner-authored preflight facts an away run must satisfy."""

    budget_units: int
    wall_clock_seconds: int
    worker_cap: int
    paths: tuple[str, ...]
    set_at: str
    set_by: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": POLICY_VERSION,
            "budget_units": self.budget_units,
            "wall_clock_seconds": self.wall_clock_seconds,
            "worker_cap": self.worker_cap,
            "paths": list(self.paths),
            "set_at": self.set_at,
            "set_by": self.set_by,
        }


def _parse_aware_instant(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise AwayError(f"{label} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AwayError(f"{label} is not a valid ISO-8601 timestamp: {exc}") from exc
    if parsed.tzinfo is None:
        raise AwayError(f"{label} must include a timezone offset or Z")
    return parsed.astimezone(UTC)


def load_away_policy(path: Path | None = None) -> AwayPolicy | None:
    """Load the owner-authored away policy, failing closed on corruption."""
    target = path if path is not None else get_away_policy_path()
    if not target.is_file():
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AwayError(f"away policy at {target}: {exc}") from exc
    if not isinstance(data, dict):
        raise AwayError(f"away policy at {target}: top level must be an object")
    if data.get("version") != POLICY_VERSION:
        raise AwayError(f"away policy at {target}: version must be {POLICY_VERSION}")
    raw_budget = data.get("budget_units")
    raw_wall = data.get("wall_clock_seconds")
    raw_cap = data.get("worker_cap")
    if (
        not isinstance(raw_budget, int)
        or isinstance(raw_budget, bool)
        or not isinstance(raw_wall, int)
        or isinstance(raw_wall, bool)
        or not isinstance(raw_cap, int)
        or isinstance(raw_cap, bool)
    ):
        raise AwayError(f"away policy at {target}: numeric limits are required")
    budget: int = raw_budget
    wall: int = raw_wall
    cap: int = raw_cap
    if budget <= 0 or wall <= 0 or cap <= 0:
        raise AwayError(f"away policy at {target}: every limit must be positive")
    raw_paths = data.get("paths")
    if not isinstance(raw_paths, list) or not raw_paths:
        raise AwayError(f"away policy at {target}: at least one authorized path is required")
    try:
        # DecisionScope validates canonical, wildcard-free repository paths.
        paths = DecisionScope(
            project_id="policy",
            task_id="policy",
            paths=tuple(str(item) for item in raw_paths),
        ).paths
    except ValueError as exc:
        raise AwayError(f"away policy at {target}: {exc}") from exc
    set_by = data.get("set_by")
    if set_by != "owner":
        raise AwayError(f"away policy at {target}: only the owner authors an away policy")
    return AwayPolicy(
        budget_units=budget,
        wall_clock_seconds=wall,
        worker_cap=cap,
        paths=paths,
        set_at=_parse_aware_instant(data.get("set_at"), "away policy set_at").isoformat(),
        set_by="owner",
    )


def save_away_policy(
    policy: AwayPolicy,
    *,
    path: Path | None = None,
) -> Path:
    """Persist one validated owner away policy."""
    if not isinstance(policy, AwayPolicy):
        raise AwayError("save_away_policy requires an AwayPolicy")
    if policy.set_by != "owner":
        raise AwayError("only the owner authors an away policy")
    _parse_aware_instant(policy.set_at, "away policy set_at")
    if not policy.paths:
        raise AwayError("at least one authorized path is required")
    target = path if path is not None else get_away_policy_path()
    return _atomic_write(target, json.dumps(policy.as_dict(), indent=2) + "\n")


class AwayJournal:
    """Append-only, fsync-durable record of what away mode did and why."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path if path is not None else get_away_journal_path()

    def records(self) -> list[dict[str, Any]]:
        """Read every record in durable order; a torn tail is ignored."""
        if not self.path.is_file():
            return []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise AwayError(f"away journal at {self.path}: {exc}") from exc
        records: list[dict[str, Any]] = []
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                # A process killed mid-append can leave one torn final line.
                if index == len(lines) - 1:
                    break
                raise AwayError(f"away journal at {self.path}: invalid record {index}") from None
            if not isinstance(record, dict):
                raise AwayError(f"away journal at {self.path}: record {index} is not an object")
            records.append(record)
        return records

    def append(self, record: dict[str, Any]) -> dict[str, Any]:
        """Durably append one record and return it (with its sequence number)."""
        prior = self.records()
        payload = {
            "version": JOURNAL_VERSION,
            "sequence": len(prior) + 1,
            "recorded_at": _now_iso(),
            **record,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
        descriptor = os.open(
            self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600
        )
        try:
            os.write(descriptor, line.encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return payload

    def record_resolution(
        self,
        *,
        run_id: str,
        question_id: str | None,
        question_key: str | None,
        decision: Decision,
        resolution: DecisionResolution,
        progress: RunProgress | None = None,
        job_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist one disposition together with its morning-report fragment."""
        report = build_morning_report(
            run_id=run_id,
            resolutions=(resolution,),
            decisions=(decision,),
            progress=progress,
        )
        return self.append(
            {
                "kind": "resolution",
                "run_id": run_id,
                "job_id": job_id,
                "question_id": question_id,
                "question_key": question_key,
                "decision": decision.as_dict(),
                "resolution": resolution.as_dict(),
                "report": report.as_dict(),
            }
        )

    def record_preflight(self, result: PreflightResult) -> dict[str, Any] | None:
        """Persist a preflight result, skipping an identical consecutive one."""
        prior = self.records()
        if prior:
            last = prior[-1]
            if last.get("kind") == "preflight" and last.get("result") == result.as_dict():
                return None
        return self.append({"kind": "preflight", "result": result.as_dict()})

    def record_plan_impact(
        self,
        *,
        project_id: str,
        tasks: Sequence[DecisionScope],
        parked: Sequence[DecisionScope],
        escalated: Sequence[DecisionScope],
    ) -> dict[str, Any]:
        """Persist the deterministic impact of this tick's parks."""
        impact = compute_plan_impact(
            [PlanTaskState(scope=scope) for scope in tasks],
            parked_scopes=parked,
            escalated_scopes=escalated,
        )
        return self.append(
            {
                "kind": "plan_impact",
                "project_id": project_id,
                "impact": impact.as_dict(),
            }
        )

    def _progress_records(self, project_id: str) -> list[dict[str, Any]]:
        return [
            record
            for record in self.records()
            if record.get("project_id") == project_id
            and record.get("kind") in {"outcome", "breaker_reset"}
        ]

    def progress(self, project_id: str) -> RunProgress:
        """Rebuild the consecutive-failure breaker state from the journal."""
        progress = RunProgress()
        for record in self._progress_records(project_id):
            if record.get("kind") == "breaker_reset":
                progress = RunProgress()
            elif record.get("passed") is True:
                progress = progress.after_task(passed=True)
            elif record.get("passed") is False:
                progress = progress.after_task(passed=False)
        return progress

    def record_outcome(
        self,
        *,
        project_id: str,
        task_id: str,
        passed: bool,
    ) -> dict[str, Any]:
        """Fold one finished task into the breaker; a halt is loud and durable."""
        before = self.progress(project_id)
        after = before.after_task(passed=passed)
        record = self.append(
            {
                "kind": "outcome",
                "project_id": project_id,
                "task_id": task_id,
                "passed": passed,
                "progress": after.as_dict(),
            }
        )
        if after.halted and not before.halted:
            self.append(
                {
                    "kind": "halt",
                    "project_id": project_id,
                    "task_id": task_id,
                    "reason": (
                        "Circuit breaker halted away work: three consecutive task failures. "
                        "Only the owner clears it with `orch away reset-breaker`."
                    ),
                    "progress": after.as_dict(),
                }
            )
        return record

    def reset_breaker(self, project_id: str, *, set_by: str = "owner") -> dict[str, Any]:
        """Owner-only clear of a halted breaker."""
        if set_by != "owner":
            raise AwayError("only the owner resets the away circuit breaker")
        return self.append({"kind": "breaker_reset", "project_id": project_id, "set_by": set_by})

    def halted(self, project_id: str) -> bool:
        """True while the durable breaker is open for this project."""
        progress = self.progress(project_id)
        return progress.halted or progress.circuit_open


@dataclass(frozen=True)
class ReturnSummary:
    """While you were away: everything readable from the durable journal."""

    journal_path: str
    records: int
    revision: int | None
    last_recorded_at: str | None
    standing_decision_actions: tuple[dict[str, Any], ...]
    accepted_candidates: tuple[dict[str, Any], ...]
    parked_branches: tuple[dict[str, Any], ...]
    escalations: tuple[dict[str, Any], ...]
    audit_entries: tuple[dict[str, Any], ...]
    stall_alarms: tuple[dict[str, Any], ...]
    halts: tuple[dict[str, Any], ...]
    plan_impact: dict[str, Any] | None
    preflight: dict[str, Any] | None
    plans: tuple[dict[str, Any], ...] = ()

    @property
    def has_items(self) -> bool:
        return bool(
            self.standing_decision_actions
            or self.accepted_candidates
            or self.parked_branches
            or self.escalations
            or self.audit_entries
            or self.stall_alarms
            or self.halts
            or any(
                plan.get("waiting_for_owner") or plan.get("parked")
                for plan in self.plans
            )
        )

    def one_line(self) -> str:
        """A bounded desktop-notice body derived only from the records."""
        parts = [
            f"{len(self.standing_decision_actions)} standing decision(s) applied",
            f"{len(self.parked_branches)} parked",
            f"{len(self.escalations)} escalated",
            f"{len(self.audit_entries)} audited",
        ]
        waiting = sum(
            len(plan.get("waiting_for_owner") or ()) for plan in self.plans
        )
        if waiting:
            parts.append(f"{waiting} plan lane(s) waiting on you")
        if self.halts:
            parts.append("CIRCUIT BREAKER HALTED: 3 consecutive failures")
        return "; ".join(parts)

    def as_dict(self) -> dict[str, Any]:
        return {
            "v": JOURNAL_VERSION,
            "journal_path": self.journal_path,
            "records": self.records,
            "revision": self.revision,
            "last_recorded_at": self.last_recorded_at,
            "has_items": self.has_items,
            "standing_decision_actions": list(self.standing_decision_actions),
            "accepted_candidates": list(self.accepted_candidates),
            "parked_branches": list(self.parked_branches),
            "escalations": list(self.escalations),
            "audit_entries": list(self.audit_entries),
            "stall_alarms": list(self.stall_alarms),
            "halts": list(self.halts),
            "plan_impact": self.plan_impact,
            "preflight": self.preflight,
            "plans": list(self.plans),
        }


def build_return_summary(
    journal: AwayJournal | None = None,
    *,
    plans: Sequence[dict[str, Any]] = (),
) -> ReturnSummary:
    """Assemble the return summary from the durable journal only.

    ``plans`` is the read-only plan section of the same summary (the projection
    behind ``STATE.md``): a plan is not a journal decision, but it is durable
    engine state a fresh session must be able to read after the planner closed.
    """
    resolved = journal if journal is not None else AwayJournal()
    records = resolved.records()
    standing: list[dict[str, Any]] = []
    accepted: list[dict[str, Any]] = []
    parked: list[dict[str, Any]] = []
    escalations: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []
    alarms: list[dict[str, Any]] = []
    halts: list[dict[str, Any]] = []
    plan_impact: dict[str, Any] | None = None
    preflight: dict[str, Any] | None = None
    seen: set[tuple[str, str]] = set()
    for record in records:
        kind = record.get("kind")
        if kind == "resolution":
            report = record.get("report")
            if not isinstance(report, dict):
                continue
            for key, bucket in (
                ("standing_decision_actions", standing),
                ("accepted_candidates", accepted),
                ("parked_branches", parked),
                ("escalations", escalations),
                ("audit_entries", audit),
                ("stall_alarms", alarms),
            ):
                for entry in report.get(key) or ():
                    marker = (key, json.dumps(entry, sort_keys=True))
                    if marker in seen:
                        continue
                    seen.add(marker)
                    bucket.append(entry)
        elif kind == "halt":
            halts.append(record)
        elif kind == "plan_impact":
            plan_impact = record.get("impact") or plan_impact
        elif kind == "preflight":
            preflight = record.get("result") or preflight
    last = records[-1] if records else None
    return ReturnSummary(
        journal_path=str(resolved.path),
        records=len(records),
        revision=int(last["sequence"]) if last is not None else None,
        last_recorded_at=str(last.get("recorded_at")) if last is not None else None,
        standing_decision_actions=tuple(standing),
        accepted_candidates=tuple(accepted),
        parked_branches=tuple(parked),
        escalations=tuple(escalations),
        audit_entries=tuple(audit),
        stall_alarms=tuple(alarms),
        halts=tuple(halts),
        plan_impact=plan_impact,
        preflight=preflight,
        plans=tuple(dict(plan) for plan in plans),
    )


def deliver_return_summary(summary: ReturnSummary, ledger: Path) -> bool:
    """Send the return summary once per changed digest; idempotent.

    Keying on the rendered one-line digest rather than the journal revision
    keeps a busy tick (a new preflight or plan-impact record each time) from
    sending an identical notice over and over.
    """
    if not summary.has_items:
        return False
    from saberops.notifications import notify_once

    notify_once(
        ledger,
        f"away-summary:{summary.one_line()}",
        "SaberOps: while you were away",
        summary.one_line(),
    )
    return True


def _preflight_for(
    *,
    policy: AwayPolicy | None,
    sheet: DecisionSheet,
    run_id: str,
    active_workers: int,
    journal: AwayJournal,
    now: datetime,
) -> PreflightResult:
    """Build and run the deterministic preflight from owner-authored facts."""
    if policy is None:
        # No owner-authored policy: every limit is unset, so the existing
        # preflight refuses rather than inventing a safe-looking default.
        request = PreflightRequest(
            run_id=run_id,
            plan_reference="away dispatch",
            decision_sheet=sheet,
            budget_limit_units=0,
            budget_spent_units=0,
            wall_clock_limit_seconds=0,
            wall_clock_elapsed_seconds=0,
            authorized_paths=(),
            worker_cap=0,
            active_workers=active_workers,
            notification_channels=(),
        )
        return run_preflight(request)
    set_at = _parse_aware_instant(policy.set_at, "away policy set_at")
    elapsed = max(0, int((now - set_at).total_seconds()))
    # One autonomous decision is one budget unit.  Only decisions taken since
    # this policy was set count, so a fresh policy really starts a fresh window.
    spent = 0
    for record in journal.records():
        if record.get("kind") != "resolution":
            continue
        try:
            recorded = _parse_aware_instant(record.get("recorded_at"), "journal recorded_at")
        except AwayError:
            continue
        if recorded >= set_at:
            spent += 1
    request = PreflightRequest(
        run_id=run_id,
        plan_reference="away dispatch",
        decision_sheet=sheet,
        budget_limit_units=policy.budget_units,
        budget_spent_units=spent,
        wall_clock_limit_seconds=policy.wall_clock_seconds,
        wall_clock_elapsed_seconds=elapsed,
        authorized_paths=policy.paths,
        worker_cap=policy.worker_cap,
        active_workers=active_workers,
        notification_channels=(NotificationChannel.LOG, NotificationChannel.WEB),
    )
    return run_preflight(request)


def record_preflight(
    result: PreflightResult,
    journal: AwayJournal | None = None,
) -> dict[str, Any] | None:
    """Durably record one preflight result (public scheduler hook)."""
    return (journal if journal is not None else AwayJournal()).record_preflight(result)


def record_plan_impact(
    *,
    project_id: str,
    tasks: Sequence[DecisionScope],
    parked: Sequence[DecisionScope],
    escalated: Sequence[DecisionScope] = (),
    journal: AwayJournal | None = None,
) -> dict[str, Any]:
    """Durably record the plan impact of this tick's parks."""
    return (journal if journal is not None else AwayJournal()).record_plan_impact(
        project_id=project_id,
        tasks=tasks,
        parked=parked,
        escalated=escalated,
    )


def record_job_outcome(
    *,
    project_id: str,
    task_id: str,
    passed: bool,
    journal: AwayJournal | None = None,
) -> dict[str, Any]:
    """Fold one finished task into the durable away circuit breaker."""
    return (journal if journal is not None else AwayJournal()).record_outcome(
        project_id=project_id,
        task_id=task_id,
        passed=passed,
    )


def answer_question_under_standing_decision(
    *,
    queue: OwnerQuestionQueue,
    question: OwnerQuestion,
    standing_decision_id: str,
    owner_words: str,
    now: str,
    evidence: str | None = None,
) -> None:
    """Record the owner's pre-authored words as the question's answer.

    The answer authority is the owner because the words are the owner's own,
    written verbatim in the standing decision.  The engine contributes no text.
    """
    queue.answer(
        question.question_id,
        words=owner_words,
        authority=AnswerAuthority.OWNER,
        answered_by=f"standing-decision:{standing_decision_id}",
        answered_by_role=AskRole.OWNER,
        evidence=evidence,
        now=now,
    )


@dataclass(frozen=True)
class DispatchGate:
    """One dispatch-time away decision: allowed, or exactly why it is parked."""

    allowed: bool
    code: str | None
    reason: str | None
    details: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "code": self.code,
            "reason": self.reason,
            "details": self.details,
        }


def _blocked(code: str, reason: str, **details: Any) -> DispatchGate:
    return DispatchGate(allowed=False, code=code, reason=reason, details=details)


def evaluate_dispatch(
    *,
    job_id: str,
    repo: str | Path,
    project_id: str | None = None,
    plan_tasks: Sequence[DecisionScope] = (),
    active_workers: int = 0,
    now: datetime | None = None,
    queue: OwnerQuestionQueue | None = None,
    store: StandingDecisionStore | None = None,
    journal: AwayJournal | None = None,
    policy: AwayPolicy | None | object = ...,
    current_turn: int | None = None,
) -> DispatchGate:
    """Decide whether away mode may dispatch one job right now.

    Order, each step durable before the next: the circuit breaker, the
    deterministic preflight, then every open question blocking this job.  A
    live exact standing decision answers its question (the owner's words,
    verbatim); a never-delegable or unhandled decision parks **only this job**
    and is journalled, so independent work continues.
    """
    moment = now if now is not None else datetime.now(UTC)
    instant = _now_iso(moment)
    resolved_store = store if store is not None else StandingDecisionStore()
    resolved_journal = journal if journal is not None else AwayJournal()
    resolved_queue = queue if queue is not None else OwnerQuestionQueue()
    resolved_project = project_id or project_scope_id(repo)
    resolved_policy = load_away_policy() if policy is ... else policy

    if resolved_journal.halted(resolved_project):
        return _blocked(
            "circuit_open",
            "the away circuit breaker halted this project after three consecutive "
            "task failures; only the owner clears it with `orch away reset-breaker`",
            project_id=resolved_project,
        )

    sheet = resolved_store.sheet(job_id)
    preflight = _preflight_for(
        policy=resolved_policy if isinstance(resolved_policy, AwayPolicy) else None,
        sheet=sheet,
        run_id=job_id,
        active_workers=active_workers,
        journal=resolved_journal,
        now=moment,
    )
    record_preflight(preflight, resolved_journal)
    if not preflight.ready:
        return _blocked(
            "preflight",
            "the deterministic away preflight refused the start",
            blockers=[blocker.as_dict() for blocker in preflight.blockers],
            warnings=list(preflight.warnings),
        )

    turn = current_turn
    if turn is None:
        turn = sum(1 for record in resolved_journal.records() if record.get("kind") == "resolution")
    for question in resolved_queue.questions(status=QuestionStatus.OPEN):
        request = question.request
        if job_id not in request.blocks:
            continue
        decision = Decision(
            decision_id=question.question_id,
            scope=question_scope(question, job_id, repo),
            decision_key=decision_key_for_question(request.key),
            question=request.subject,
            never_delegable=never_delegable_for_question(request.decision_class),
        )
        # Away applies the decision sheet to questions.  Candidate acceptance is
        # a separate engine stage (F15 auto-accept) and never flows through this
        # question resolver, so an unanswered question still parks its branch.
        resolution = resolve_decision(
            sheet,
            decision,
            now=instant,
            current_turn=turn,
            authority_mode=AuthorityMode.SUPERVISED,
        )
        resolved_journal.record_resolution(
            run_id=job_id,
            job_id=job_id,
            question_id=question.question_id,
            question_key=request.key,
            decision=decision,
            resolution=resolution,
        )
        if resolution.disposition is DecisionDisposition.APPLY_STANDING_DECISION:
            if (
                resolution.owner_words is None
                or resolution.applied_standing_decision_id is None
            ):
                raise AwayError(
                    "a standing-decision resolution must carry the owner words and the id"
                )
            answer_question_under_standing_decision(
                queue=resolved_queue,
                question=question,
                standing_decision_id=resolution.applied_standing_decision_id,
                owner_words=resolution.owner_words,
                now=instant,
                evidence=(
                    f"standing decision {resolution.applied_standing_decision_id} "
                    f"for question {question.question_id}"
                ),
            )
            turn += 1
            continue
        # Never-delegable, expired or unhandled: park only this job.
        if plan_tasks:
            record_plan_impact(
                project_id=resolved_project,
                tasks=plan_tasks,
                parked=(decision.scope,),
                journal=resolved_journal,
            )
        return _blocked(
            "parked"
            if resolution.disposition is DecisionDisposition.PARK_BRANCH
            else "escalated",
            resolution.explanation[-1] if resolution.explanation else "decision needs the owner",
            project_id=resolved_project,
            question_id=question.question_id,
            decision_key=decision.decision_key,
            disposition=resolution.disposition.value,
            never_delegable=decision.never_delegable.value
            if decision.never_delegable is not None
            else None,
        )
    return DispatchGate(
        allowed=True,
        code=None,
        reason=None,
        details={"project_id": resolved_project},
    )
