"""One durable work gate: SaberOps runs only while the owner is there.

The owner's rule (2026-11-19) is blunt: *closing the planning CLI pauses the
work, no matter what*.  Nothing may start and no worker may keep spending usage
once the last planning session that turned SaberOps on is gone, in every owner
mode including ``away``.

This module is the single place that answers "may the engine start work now?".
It has three independent inputs and **any** of them closes the gate:

1. an explicit owner pause (``orch pause`` / the web toggle);
2. no live planning session (the MCP planning lease is gone, its process is
   dead, or its heartbeat stopped -- a crashed CLI or a lost connection);
3. an ``away`` session that ran out of its end time or job budget, or that was
   never given one at all.

Closing is **sticky**.  A returning session does not silently resume anything:
it sees what was paused and the owner (or that session) clears the pause with one
command or tool call.  That is why the gate is a small durable document rather
than a pure function of liveness.

Presence is only enforced once a planning session has actually registered
(:attr:`GateDocument.presence_known`).  A store that has only ever been driven
by direct CLI commands keeps its previous behaviour; the moment an MCP planning
session turns SaberOps on, this gate arms and every later run requires one.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Final

from saberops.owner_mode import (
    OwnerMode,
    OwnerModeError,
    OwnerModeSettings,
    effective_owner_mode,
    load_owner_mode_settings,
)
from saberops.paths import get_runtime_gate_path

__all__ = [
    "AwayBudget",
    "GateDocument",
    "GateState",
    "PauseReason",
    "Presence",
    "RuntimeGateError",
    "away_jobs_started",
    "clear_pause",
    "evaluate_gate",
    "gate_path",
    "gate_view",
    "mark_presence_known",
    "maybe_send_digest",
    "presence",
    "record_pause_event",
    "read_gate",
    "refresh_gate",
    "register_lane_submit_gate",
    "set_pause",
    "summary_line",
]

SCHEMA_VERSION: Final[int] = 1

#: A heartbeat this stale means the CLI crashed or lost its connection.
DEFAULT_HEARTBEAT_TIMEOUT_SECONDS: Final[float] = 60.0

#: Default cadence of the unattended digest notice.
DEFAULT_DIGEST_SECONDS: Final[float] = 3600.0

HEARTBEAT_TIMEOUT_ENV: Final[str] = "SABEROPS_PLANNING_HEARTBEAT_TIMEOUT"
DIGEST_SECONDS_ENV: Final[str] = "SABEROPS_DIGEST_SECONDS"


class RuntimeGateError(ValueError):
    """Fail-closed error for an unreadable or malformed gate document."""


class PauseReason(StrEnum):
    """Why the gate is closed.  Stable ids for records, notices and the CLI."""

    #: The owner ran ``orch pause`` or pressed Pause on the web page.
    OWNER = "owner_pause"
    #: The last planning session went away (closed, died, or lost heartbeat).
    NO_PLANNING_SESSION = "no_planning_session"
    #: The away session's ``until`` end time passed.
    AWAY_ENDED = "away_ended"
    #: The away session spent its job budget.
    AWAY_CAP = "away_cap"
    #: Away mode was selected without an end time or a job budget.
    AWAY_UNBOUNDED = "away_unbounded"
    #: The gate document exists but cannot be read; fail closed, never open.
    GATE_UNREADABLE = "gate_unreadable"


#: Human-facing explanations.  A notice or CLI line never invents its own words.
PAUSE_REASON_LABELS: Final[dict[str, str]] = {
    PauseReason.OWNER.value: "paused by the owner",
    PauseReason.NO_PLANNING_SESSION.value: "paused: no planning session",
    PauseReason.AWAY_ENDED.value: "paused: the away session reached its end time",
    PauseReason.AWAY_CAP.value: "paused: the away session spent its job budget",
    PauseReason.AWAY_UNBOUNDED.value: (
        "paused: away mode needs an end time or a job budget"
    ),
    PauseReason.GATE_UNREADABLE.value: "paused: the work gate record is unreadable",
}


def label_for(reason: str | None) -> str:
    """Stable human label for a pause reason id."""
    if not reason:
        return "running"
    return PAUSE_REASON_LABELS.get(reason, reason)


@dataclass(frozen=True)
class Presence:
    """The live planning-session answer, derived from the MCP planning lease."""

    live: bool
    pid: int | None
    session_id: str | None
    client: str | None
    last_activity: float | None
    age_seconds: float | None
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "live": self.live,
            "pid": self.pid,
            "session_id": self.session_id,
            "client": self.client,
            "last_activity": self.last_activity,
            "age_seconds": self.age_seconds,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class PauseRecord:
    """One sticky pause: the reason, the evidence and when it started."""

    reason: str
    detail: str
    since: str
    by: str
    paused_jobs: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "label": label_for(self.reason),
            "detail": self.detail,
            "since": self.since,
            "by": self.by,
            "paused_jobs": list(self.paused_jobs),
        }

    @classmethod
    def from_mapping(cls, payload: object) -> PauseRecord | None:
        if not isinstance(payload, dict):
            return None
        reason = payload.get("reason")
        since = payload.get("since")
        if not isinstance(reason, str) or not reason:
            return None
        if not isinstance(since, str) or not since:
            return None
        detail = payload.get("detail")
        by = payload.get("by")
        jobs = payload.get("paused_jobs")
        return cls(
            reason=reason,
            detail=detail if isinstance(detail, str) else "",
            since=since,
            by=by if isinstance(by, str) else "engine",
            paused_jobs=tuple(
                str(job) for job in jobs if isinstance(job, str)
            )
            if isinstance(jobs, list)
            else (),
        )


@dataclass
class GateDocument:
    """The durable gate document (``<state>/runtime-gate.json``)."""

    presence_known: bool = False
    pause: PauseRecord | None = None
    last_resume: dict[str, str] | None = None
    #: Run ids the engine stopped for a pause and must relaunch on resume.
    paused_runs: tuple[str, ...] = ()
    schema_version: int = SCHEMA_VERSION
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "version": self.schema_version,
            "presence_known": self.presence_known,
            "pause": self.pause.as_dict() if self.pause is not None else None,
            "last_resume": self.last_resume,
            "paused_runs": list(self.paused_runs),
        }
        body.update(self.extra)
        return body


@dataclass(frozen=True)
class AwayBudget:
    """How much of the current away session has been spent."""

    until: str | None
    max_jobs: int | None
    jobs_started: int
    expired: bool
    exhausted: bool
    unbounded: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "until": self.until,
            "max_jobs": self.max_jobs,
            "jobs_started": self.jobs_started,
            "expired": self.expired,
            "exhausted": self.exhausted,
            "unbounded": self.unbounded,
        }


@dataclass(frozen=True)
class GateState:
    """The one answer every start path uses."""

    closed: bool
    reason: str | None
    detail: str
    since: str | None
    mode: OwnerMode
    presence: Presence
    presence_required: bool
    away: AwayBudget | None
    paused_runs: tuple[str, ...]
    paused_jobs: tuple[str, ...]
    resume_requires: str = "confirmation"

    @property
    def label(self) -> str:
        return label_for(self.reason)

    def as_dict(self) -> dict[str, Any]:
        return {
            "v": SCHEMA_VERSION,
            "closed": self.closed,
            "reason": self.reason,
            "label": self.label,
            "detail": self.detail,
            "since": self.since,
            "mode": self.mode.value,
            "presence_required": self.presence_required,
            "presence": self.presence.as_dict(),
            "away": self.away.as_dict() if self.away is not None else None,
            "paused_runs": list(self.paused_runs),
            "paused_jobs": list(self.paused_jobs),
            "resume_requires": self.resume_requires,
            "resume_command": "orch resume",
            "resume_tool": "resume_work",
        }


def gate_path() -> Path:
    """The durable gate document for the active profile."""
    override = os.environ.get("SABEROPS_RUNTIME_GATE", "").strip()
    if override:
        return Path(override).expanduser()
    return get_runtime_gate_path()


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _heartbeat_timeout() -> float:
    try:
        value = float(os.environ.get(HEARTBEAT_TIMEOUT_ENV, "").strip() or "0")
    except ValueError:
        return DEFAULT_HEARTBEAT_TIMEOUT_SECONDS
    return value if value > 0 else DEFAULT_HEARTBEAT_TIMEOUT_SECONDS


def _digest_seconds() -> float:
    try:
        value = float(os.environ.get(DIGEST_SECONDS_ENV, "").strip() or "0")
    except ValueError:
        return DEFAULT_DIGEST_SECONDS
    return value if value > 0 else DEFAULT_DIGEST_SECONDS


# --------------------------------------------------------------------------- #
# Document read/write
# --------------------------------------------------------------------------- #


def read_gate(path: Path | None = None) -> GateDocument:
    """Read the gate document; a missing file is a fresh, unarmed gate.

    A file that exists but cannot be parsed raises: an unreadable gate must
    never be read as "open".
    """
    target = path or gate_path()
    if not target.is_file():
        return GateDocument()
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeGateError(f"cannot read work gate {target}: {exc}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeGateError(f"work gate {target} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeGateError(f"work gate {target}: top-level must be an object")
    version = payload.get("version")
    if type(version) is not int or version != SCHEMA_VERSION:
        raise RuntimeGateError(
            f"work gate {target}: version must be integer {SCHEMA_VERSION}"
        )
    known = {"version", "presence_known", "pause", "last_resume", "paused_runs"}
    pause_raw = payload.get("pause")
    pause = None if pause_raw is None else PauseRecord.from_mapping(pause_raw)
    if pause_raw is not None and pause is None:
        raise RuntimeGateError(f"work gate {target}: malformed pause record")
    resume_raw = payload.get("last_resume")
    last_resume: dict[str, str] | None = None
    if isinstance(resume_raw, dict):
        last_resume = {
            str(key): str(value) for key, value in resume_raw.items()
        }
    runs = payload.get("paused_runs")
    return GateDocument(
        presence_known=payload.get("presence_known") is True,
        pause=pause,
        last_resume=last_resume,
        paused_runs=tuple(str(run) for run in runs)
        if isinstance(runs, list)
        else (),
        extra={k: v for k, v in payload.items() if k not in known},
    )


def _atomic_write(target: Path, document: GateDocument) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=".runtime-gate-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            handle.write(json.dumps(document.as_dict(), indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
        tmp_path = None
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def write_gate(document: GateDocument, path: Path | None = None) -> Path:
    """Persist ``document`` atomically (last writer wins; see :func:`set_pause`)."""
    target = path or gate_path()
    _atomic_write(target, document)
    return target


# --------------------------------------------------------------------------- #
# Presence
# --------------------------------------------------------------------------- #


def presence(*, now: datetime | None = None) -> Presence:
    """Return whether a planning session is connected and heartbeating."""
    from saberops.mcp.lease import OrchestratorLease, pid_is_alive

    moment = now or _now()
    try:
        record = OrchestratorLease().read()
    except Exception as exc:  # pragma: no cover - defensive
        return Presence(
            live=False,
            pid=None,
            session_id=None,
            client=None,
            last_activity=None,
            age_seconds=None,
            detail=f"planning lease unreadable: {type(exc).__name__}",
        )
    if record is None:
        return Presence(
            live=False,
            pid=None,
            session_id=None,
            client=None,
            last_activity=None,
            age_seconds=None,
            detail="no planning session holds the lease",
        )
    if not pid_is_alive(record.pid):
        return Presence(
            live=False,
            pid=record.pid,
            session_id=record.session_id or None,
            client=record.client or None,
            last_activity=record.last_activity or None,
            age_seconds=None,
            detail=f"planning session pid {record.pid} is gone",
        )
    age = (
        max(0.0, moment.timestamp() - (record.heartbeat_at or record.last_activity))
        if (record.heartbeat_at or record.last_activity)
        else None
    )
    if age is not None and age > _heartbeat_timeout():
        return Presence(
            live=False,
            pid=record.pid,
            session_id=record.session_id or None,
            client=record.client or None,
            last_activity=record.heartbeat_at or record.last_activity or None,
            age_seconds=age,
            detail=(
                f"planning session heartbeat is {age:.0f}s old "
                f"(limit {_heartbeat_timeout():.0f}s)"
            ),
        )
    return Presence(
        live=True,
        pid=record.pid,
        session_id=record.session_id or None,
        client=record.client or None,
        last_activity=record.heartbeat_at or record.last_activity or None,
        age_seconds=age,
        detail="planning session is connected",
    )


def mark_presence_known(path: Path | None = None) -> bool:
    """Arm the presence requirement once a planning session registers.

    Called by the MCP server when it takes the planning lease.  Idempotent and
    cheap: it only writes when the flag is not already set.
    """
    try:
        document = read_gate(path)
    except RuntimeGateError:
        # Never overwrite a corrupt document; the gate fails closed on its own.
        return False
    if document.presence_known:
        return False
    document.presence_known = True
    write_gate(document, path)
    return True


# --------------------------------------------------------------------------- #
# Pauses
# --------------------------------------------------------------------------- #


def set_pause(
    reason: PauseReason | str,
    detail: str,
    *,
    by: str = "engine",
    paused_jobs: tuple[str, ...] = (),
    paused_runs: tuple[str, ...] = (),
    now: datetime | None = None,
    path: Path | None = None,
) -> GateState:
    """Record one sticky pause (idempotent while the same reason is active)."""
    resolved = reason if isinstance(reason, PauseReason) else PauseReason(str(reason))
    moment = now or _now()
    try:
        document = read_gate(path)
    except RuntimeGateError:
        document = GateDocument(presence_known=True)
    existing = document.pause
    if existing is None or existing.reason != resolved.value:
        document.pause = PauseRecord(
            reason=resolved.value,
            detail=detail,
            since=moment.isoformat(),
            by=by,
            paused_jobs=tuple(dict.fromkeys(paused_jobs)),
        )
        document.paused_runs = tuple(dict.fromkeys(paused_runs))
    else:
        merged_jobs = tuple(dict.fromkeys((*existing.paused_jobs, *paused_jobs)))
        merged_runs = tuple(dict.fromkeys((*document.paused_runs, *paused_runs)))
        document.pause = PauseRecord(
            reason=existing.reason,
            detail=existing.detail or detail,
            since=existing.since,
            by=existing.by,
            paused_jobs=merged_jobs,
        )
        document.paused_runs = merged_runs
    write_gate(document, path)
    return _state_from_document(
        document,
        mode=_safe_mode(),
        presence_value=presence(now=moment),
    )


def clear_pause(
    *,
    by: str = "owner",
    now: datetime | None = None,
    path: Path | None = None,
) -> GateState:
    """Clear the sticky pause.  The next scheduler tick dispatches again."""
    moment = now or _now()
    document = read_gate(path)
    document.pause = None
    document.paused_runs = ()
    document.last_resume = {"at": moment.isoformat(), "by": by}
    write_gate(document, path)
    return _state_from_document(
        document,
        mode=_safe_mode(),
        presence_value=presence(now=moment),
    )


def _safe_mode() -> OwnerMode:
    try:
        return load_owner_mode_settings().mode
    except OwnerModeError:
        # An unreadable mode document must never read as a wider mode.
        return OwnerMode.ASK_ME


# --------------------------------------------------------------------------- #
# Away budget
# --------------------------------------------------------------------------- #


def away_jobs_started(
    db: Any, settings: OwnerModeSettings, *, now: datetime | None = None
) -> int:
    """Jobs submitted during the current away session (never guessed from gaps)."""
    if settings.mode is not OwnerMode.AWAY or not settings.set_at:
        return 0
    started = _parse_iso(settings.set_at)
    if started is None:
        return 0
    try:
        from saberops.jobs import store

        jobs, _ = store.list_jobs_tolerant(db, limit=100_000)
    except Exception:  # pragma: no cover - a read error is not evidence of zero
        return 0
    count = 0
    for job in jobs:
        created = _parse_iso(getattr(job, "created_at", None))
        if created is not None and created >= started:
            count += 1
    return count


def _away_budget(
    settings: OwnerModeSettings,
    *,
    now: datetime,
    jobs_started: int | None,
) -> AwayBudget | None:
    if settings.mode is not OwnerMode.AWAY:
        return None
    until = _parse_iso(settings.until)
    expired = until is not None and until <= now
    started = 0 if jobs_started is None else int(jobs_started)
    exhausted = settings.max_jobs is not None and started >= settings.max_jobs
    return AwayBudget(
        until=settings.until,
        max_jobs=settings.max_jobs,
        jobs_started=started,
        expired=expired,
        exhausted=exhausted,
        unbounded=not settings.away_bounded,
    )


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #


def _state_from_document(
    document: GateDocument,
    *,
    mode: OwnerMode,
    presence_value: Presence,
    away: AwayBudget | None = None,
    reason: str | None = None,
    detail: str = "",
) -> GateState:
    pause = document.pause
    if pause is not None:
        return GateState(
            closed=True,
            reason=pause.reason,
            detail=pause.detail,
            since=pause.since,
            mode=mode,
            presence=presence_value,
            presence_required=document.presence_known,
            away=away,
            paused_runs=document.paused_runs,
            paused_jobs=pause.paused_jobs,
        )
    if reason is not None:
        return GateState(
            closed=True,
            reason=reason,
            detail=detail,
            since=None,
            mode=mode,
            presence=presence_value,
            presence_required=document.presence_known,
            away=away,
            paused_runs=document.paused_runs,
            paused_jobs=(),
        )
    return GateState(
        closed=False,
        reason=None,
        detail="",
        since=None,
        mode=mode,
        presence=presence_value,
        presence_required=document.presence_known,
        away=away,
        paused_runs=document.paused_runs,
        paused_jobs=(),
    )


def evaluate_gate(
    *,
    db: Any | None = None,
    mode_settings: OwnerModeSettings | None = None,
    now: datetime | None = None,
    path: Path | None = None,
    jobs_started: int | None = None,
) -> GateState:
    """Return the current gate answer without changing anything.

    ``closed`` is True when the engine must not start work.  Live conditions
    (no session, away budget) are reported here; :func:`refresh_gate` turns them
    into the sticky record and notice.
    """
    moment = now or _now()
    try:
        settings = mode_settings or load_owner_mode_settings()
    except OwnerModeError as exc:
        return GateState(
            closed=True,
            reason=PauseReason.GATE_UNREADABLE.value,
            detail=f"owner mode document is unreadable: {exc}",
            since=None,
            mode=OwnerMode.ASK_ME,
            presence=Presence(
                live=False,
                pid=None,
                session_id=None,
                client=None,
                last_activity=None,
                age_seconds=None,
                detail="unknown",
            ),
            presence_required=True,
            away=None,
            paused_runs=(),
            paused_jobs=(),
        )
    mode, _expired = effective_owner_mode(settings, now=moment)
    try:
        document = read_gate(path)
    except RuntimeGateError as exc:
        return GateState(
            closed=True,
            reason=PauseReason.GATE_UNREADABLE.value,
            detail=str(exc),
            since=None,
            mode=mode,
            presence=Presence(
                live=False,
                pid=None,
                session_id=None,
                client=None,
                last_activity=None,
                age_seconds=None,
                detail="unknown",
            ),
            presence_required=True,
            away=None,
            paused_runs=(),
            paused_jobs=(),
        )
    current = presence(now=moment)
    started = jobs_started
    if started is None and db is not None and settings.mode is OwnerMode.AWAY:
        started = away_jobs_started(db, settings, now=moment)
    budget = _away_budget(settings, now=moment, jobs_started=started)
    if budget is not None and budget.unbounded:
        return _state_from_document(
            document,
            mode=mode,
            presence_value=current,
            away=budget,
            reason=PauseReason.AWAY_UNBOUNDED.value,
            detail=(
                "away mode needs an end time or a job budget; set it with "
                "orch mode set away --until ... --max-jobs ..."
            ),
        )
    if budget is not None and budget.expired:
        return _state_from_document(
            document,
            mode=mode,
            presence_value=current,
            away=budget,
            reason=PauseReason.AWAY_ENDED.value,
            detail=f"the away session ended at {budget.until}",
        )
    if budget is not None and budget.exhausted:
        return _state_from_document(
            document,
            mode=mode,
            presence_value=current,
            away=budget,
            reason=PauseReason.AWAY_CAP.value,
            detail=(
                f"the away session used its job budget "
                f"({budget.jobs_started}/{budget.max_jobs})"
            ),
        )
    if document.presence_known and not current.live:
        return _state_from_document(
            document,
            mode=mode,
            presence_value=current,
            away=budget,
            reason=PauseReason.NO_PLANNING_SESSION.value,
            detail=current.detail,
        )
    return _state_from_document(
        document, mode=mode, presence_value=current, away=budget
    )


def refresh_gate(
    db: Any | None = None,
    *,
    now: datetime | None = None,
    path: Path | None = None,
) -> GateState:
    """Evaluate the gate and make a live closure sticky (one event, one notice).

    This is the scheduler's entry point.  Calling it repeatedly while the same
    condition holds changes nothing: the pause record keeps its original
    ``since`` and the notice key is derived from it, so the owner sees exactly
    one notice per pause.
    """
    moment = now or _now()
    state = evaluate_gate(db=db, now=moment, path=path)
    if not state.closed or state.reason in (None, PauseReason.OWNER.value):
        # Owner pauses are written by the pause command itself; nothing to add.
        return state
    if state.since is not None:
        return state
    paused = set_pause(
        state.reason or PauseReason.GATE_UNREADABLE.value,
        state.detail,
        by="engine",
        now=moment,
        path=path,
    )
    _record_pause_once(paused)
    _notify_pause_once(db, paused)
    return paused


def record_pause_event(state: GateState) -> None:
    """Public form of the one-event-per-pause ledger append."""
    _record_pause_once(state)


def _record_pause_once(state: GateState) -> None:
    """Append one hash-chained ledger event per pause (idempotent on its key)."""
    if not state.closed or state.reason is None:
        return
    try:
        from saberops.mcp.ledger import EventLedger

        EventLedger().append(
            "work_paused",
            {
                "reason": state.reason,
                "label": state.label,
                "detail": state.detail,
                "since": state.since,
                "mode": state.mode.value,
                "paused_runs": list(state.paused_runs),
                "paused_jobs": list(state.paused_jobs),
            },
            key=f"work_paused:{state.since or state.reason}",
        )
    except Exception as exc:  # pragma: no cover - the notice path still runs
        logging.getLogger(__name__).warning("work_paused ledger append failed: %s", exc)


def _notifications_ledger(db: Any) -> Path:
    owner = getattr(db, "owner_db", db)
    db_path = Path(owner.db_path)
    return db_path.with_name(db_path.name + ".notifications.sqlite3")


def _notify_pause_once(db: Any | None, state: GateState) -> None:
    if db is None or not state.closed or state.reason is None:
        return
    key = f"pause:{state.since or state.reason}"
    job_note = (
        f" {len(state.paused_jobs)} job(s) paused."
        if state.paused_jobs
        else ""
    )
    try:
        from saberops.notifications import notify_once

        notify_once(
            _notifications_ledger(db),
            key,
            "SaberOps paused",
            f"{state.label}.{job_note} Nothing resumes until you confirm.",
        )
    except Exception as exc:  # pragma: no cover - a notice must never break a tick
        logging.getLogger(__name__).warning("pause notice failed: %s", exc)


# --------------------------------------------------------------------------- #
# Digest
# --------------------------------------------------------------------------- #


def maybe_send_digest(
    db: Any,
    state: GateState,
    *,
    now: datetime | None = None,
) -> str | None:
    """Send the hourly unattended digest while away or critical-only.

    Returns the notice key when a digest was attempted.  The notice lists the
    running jobs, the subscriptions in use and the usage recorded so far; a
    missing counter stays ``unknown`` rather than becoming a false zero.
    """
    if state.mode not in (OwnerMode.AWAY, OwnerMode.CRITICAL_ONLY):
        return None
    if state.closed:
        return None
    moment = now or _now()
    bucket = moment.astimezone(UTC).strftime("%Y-%m-%dT%H")
    key = f"digest:{state.mode.value}:{bucket}"
    running = _running_usage_rows(db)
    if not running:
        body = "No running jobs."
    else:
        lines = [
            f"{row['job_id']} [{row['subscription']}] "
            f"{row['total_tokens']} tokens"
            for row in running
        ]
        body = f"{len(running)} running: " + "; ".join(lines)
    try:
        from saberops.notifications import notify_once

        notify_once(
            _notifications_ledger(db),
            key,
            f"SaberOps {state.mode.value} digest",
            body,
        )
    except Exception as exc:  # pragma: no cover - a notice must never break a tick
        logging.getLogger(__name__).warning("digest notice failed: %s", exc)
    return key


def _running_usage_rows(db: Any) -> list[dict[str, Any]]:
    """Running job ids, subscription and known usage; read-only and tolerant."""
    rows: list[dict[str, Any]] = []
    try:
        from saberops.jobs import store
        from saberops.jobs.models import JobState
        from saberops.jobs.service import job_usage_view

        jobs, _ = store.list_jobs_tolerant(db, state=JobState.RUNNING, limit=1_000)
    except Exception:
        return rows
    for job in jobs:
        subscriptions: set[str] = set()
        total: int = 0
        try:
            view = job_usage_view(db, job)
        except Exception:
            view = {}
        for entry in view.get("subscriptions", []) if isinstance(view, dict) else []:
            slug = entry.get("subscription")
            if isinstance(slug, str) and slug:
                subscriptions.add(slug)
            usage = entry.get("usage")
            if isinstance(usage, dict):
                value = usage.get("total_tokens")
                if isinstance(value, int):
                    total += value
        rows.append(
            {
                "job_id": job.job_id,
                "subscription": ",".join(sorted(subscriptions)) or "unknown",
                "total_tokens": total,
            }
        )
    return rows


# --------------------------------------------------------------------------- #
# Plan-lane seam (F17 + P2)
# --------------------------------------------------------------------------- #


def lane_submit_gate(db: Any) -> str | None:
    """Plan-lane gate callback: a short pause reason, or ``None`` to allow."""
    state = evaluate_gate(db=db)
    if not state.closed:
        return None
    return state.reason or PauseReason.NO_PLANNING_SESSION.value


def register_lane_submit_gate() -> None:
    """Install :func:`lane_submit_gate` with the plan runner (idempotent)."""
    from saberops import plan_runner

    plan_runner.set_lane_submit_gate(lane_submit_gate)


# --------------------------------------------------------------------------- #
# Views
# --------------------------------------------------------------------------- #


def summary_line(state: GateState) -> str:
    """One owner-facing line for status output and catch-up."""
    if not state.closed:
        presence_note = (
            "planning session connected"
            if state.presence.live
            else "planning presence not required"
        )
        return f"running ({presence_note})"
    return f"{state.label} since {state.since or 'now'}"


def gate_view(
    *,
    db: Any | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Render the gate for CLI, web and MCP readers."""
    state = evaluate_gate(db=db, now=now)
    return state.as_dict()
