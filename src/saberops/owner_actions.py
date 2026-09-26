"""Durable non-blocking owner actions (R3-E2): detached Review and Accept.

The Web dashboard must never execute a long-running Review or Accept inside
the HTTP request: the browser may close, the owner may double-click, and the
Web server itself may restart.  This module provides the smallest durable
primitive that makes both operations safe for real dogfooding:

* :class:`RunOwnerAction` -- one bounded row per requested REVIEW or ACCEPT,
  carrying the exact candidate SHA the owner acted on, a QUEUED / RUNNING /
  SUCCEEDED / FAILED / UNCERTAIN state, the detached process identity, and
  bounded result/error evidence.
* Atomic claiming -- concurrent Review (or Accept) POSTs for the same run
  collapse onto at most one active action and therefore at most one detached
  child.  History (SUCCEEDED / FAILED / UNCERTAIN) never blocks a later
  explicit owner request; only QUEUED / RUNNING work does.
* Candidate fencing -- the detached child re-resolves the canonical
  candidate before doing anything and refuses (typed reason, zero model
  launch / zero repository mutation) when the run moved on.
* Detached argv execution -- the same process-safety properties as
  :class:`saberops.supervisor.RunSupervisor` (argv only, explicit project
  DB, target repository as cwd, detached stdin, durable transcript,
  ``close_fds=True``, ``start_new_session=True``), so closing the browser
  or finishing the request can never cancel the child.
* Crash semantics -- Popen failure records FAILED (never a false RUNNING);
  a provably dead RUNNING action without a terminal receipt projects
  UNCERTAIN; UNKNOWN liveness fails closed for duplicate launch; an
  UNCERTAIN Accept is never replayed automatically.

Actual Review and Accept authority stays in the existing service/engine
paths: the child delegates to ``OrchestratorService.review_run`` /
``accept_run`` (which re-check every canonical condition), and the action
row is never a second review verdict or acceptance-policy authority.
"""

from __future__ import annotations

import os
import re
import subprocess
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from saberops.ownership import Liveness, ProcessIdentity, probe_liveness

if TYPE_CHECKING:
    from saberops.db import Database
    from saberops.service import OrchestratorService

__all__ = [
    "ACCEPT_CHILD_TIMEOUT_DEFAULT",
    "ACTION_CANDIDATE_MISMATCH_REASON",
    "ACTION_ID_PREFIX",
    "ACTION_RUN_MISMATCH_REASON",
    "CANDIDATE_CHANGED_REASON",
    "KIND_MISMATCH_REASON",
    "LAUNCH_FAILED_REASON",
    "OWNER_ACTION_COMPLETED_EVENT",
    "OWNER_ACTION_FAILED_EVENT",
    "OWNER_ACTION_QUEUED_EVENT",
    "OWNER_ACTION_STARTED_EVENT",
    "OWNER_ACTION_UNCERTAIN_EVENT",
    "OWNER_PROCESS_DEAD_REASON",
    "REVIEW_CHILD_TIMEOUT_DEFAULT",
    "ACTIVE_OWNER_ACTION_STATES",
    "OwnerActionKind",
    "OwnerActionLaunch",
    "OwnerActionState",
    "OwnerActionSummary",
    "OwnerActionSupervisor",
    "RunOwnerAction",
    "action_identity",
    "bound_action_text",
    "build_owner_action_argv",
    "current_candidate_sha",
    "effective_action_state",
    "is_action_effectively_active",
    "new_owner_action_id",
    "owner_action_transcript_path",
    "run_accept_action_child",
    "run_review_action_child",
    "summarize_owner_actions",
]

#: Timeout the detached Review child passes to the service authority.
REVIEW_CHILD_TIMEOUT_DEFAULT: Final[float] = 1200.0
#: Timeout the detached Accept child passes to the service authority.
ACCEPT_CHILD_TIMEOUT_DEFAULT: Final[float] = 300.0
#: Prefix for durable owner-action identifiers.
ACTION_ID_PREFIX: Final[str] = "oact_"

#: Typed reason when the candidate moved between request and child execution.
CANDIDATE_CHANGED_REASON: Final[str] = "CANDIDATE_CHANGED"
#: Typed reason when the launcher could not spawn the detached child.
LAUNCH_FAILED_REASON: Final[str] = "LAUNCH_FAILED"
#: Typed reason when a provably dead active action is settled to UNCERTAIN.
OWNER_PROCESS_DEAD_REASON: Final[str] = "OWNER_PROCESS_DEAD"
#: Typed reason when a child runs for the wrong action kind.
KIND_MISMATCH_REASON: Final[str] = "ACTION_KIND_MISMATCH"
#: Typed reason when the CLI run id does not match the persisted action row.
ACTION_RUN_MISMATCH_REASON: Final[str] = "ACTION_RUN_MISMATCH"
#: Typed reason when the internal --expected-candidate value disagrees with
#: the persisted action row (argv must never retarget a persisted action).
ACTION_CANDIDATE_MISMATCH_REASON: Final[str] = "ACTION_CANDIDATE_MISMATCH"

#: SSE-visible durable event types (surfaced by the existing run event stream).
OWNER_ACTION_QUEUED_EVENT: Final[str] = "owner_action_queued"
OWNER_ACTION_STARTED_EVENT: Final[str] = "owner_action_started"
OWNER_ACTION_COMPLETED_EVENT: Final[str] = "owner_action_completed"
OWNER_ACTION_FAILED_EVENT: Final[str] = "owner_action_failed"
OWNER_ACTION_UNCERTAIN_EVENT: Final[str] = "owner_action_uncertain"

#: Upper bound for persisted result/error evidence (bounded, secret-free).
_ACTION_TEXT_BOUND: Final[int] = 2000


class OwnerActionKind(StrEnum):
    """The two owner operations that may run detached."""

    REVIEW = "REVIEW"
    ACCEPT = "ACCEPT"


class OwnerActionState(StrEnum):
    """Lifecycle of one durable owner action."""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNCERTAIN = "UNCERTAIN"


#: States in which a row still asserts active work (blocks a duplicate).
ACTIVE_OWNER_ACTION_STATES: Final[tuple[OwnerActionState, OwnerActionState]] = (
    OwnerActionState.QUEUED,
    OwnerActionState.RUNNING,
)


@dataclass(frozen=True)
class RunOwnerAction:
    """One durable owner-action request for one run and one candidate."""

    action_id: str
    run_id: str
    kind: OwnerActionKind
    candidate_sha: str
    state: OwnerActionState
    pid: int | None = None
    identity_json: str | None = None
    target_repo: str | None = None
    transcript_path: str | None = None
    reason: str | None = None
    error: str | None = None
    result_summary: str | None = None
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class OwnerActionLaunch:
    """Outcome of a Web-request owner-action claim + launch attempt."""

    action: RunOwnerAction
    launched: bool
    duplicate: bool


@dataclass(frozen=True)
class OwnerActionSummary:
    """Durable per-run projection for the run page and live actions JSON."""

    active_review: RunOwnerAction | None
    active_accept: RunOwnerAction | None
    last_review: RunOwnerAction | None
    last_accept: RunOwnerAction | None


def now_iso_timestamp() -> str:
    """Return the current UTC timestamp in ISO 8601 format."""
    return datetime.now(UTC).isoformat()


def bound_action_text(raw: str | None) -> str | None:
    """Truncate persisted action evidence to the bounded display limit."""
    if raw is None:
        return None
    text = str(raw)
    if len(text) <= _ACTION_TEXT_BOUND:
        return text
    return text[:_ACTION_TEXT_BOUND] + "..."


def new_owner_action_id() -> str:
    """Mint a fresh durable owner-action identifier."""
    return f"{ACTION_ID_PREFIX}{uuid.uuid4().hex[:16]}"


def current_candidate_sha(db: Database, run_id: str) -> str | None:
    """Resolve the canonical current candidate SHA via lifecycle authority.

    Single seam: the R3-A projection is the only definition of "which
    candidate the run refers to".  Returns ``None`` when no trustworthy
    candidate exists.
    """
    from saberops.candidate_lifecycle import project_candidate_state

    try:
        projection = project_candidate_state(db, run_id)
    except Exception:
        return None
    return projection.candidate_sha


def action_identity(action: RunOwnerAction) -> ProcessIdentity | None:
    """Decode the persisted detached-process identity, if usable."""
    return ProcessIdentity.from_json(action.identity_json)


def effective_action_state(action: RunOwnerAction) -> OwnerActionState:
    """Project the effective state of ``action`` without mutating storage.

    A persisted QUEUED / RUNNING action whose exact process is provably
    dead without a recorded terminal result projects UNCERTAIN.  UNKNOWN
    liveness (or no recorded identity) keeps the stored state so callers
    fail closed for duplicate launch.
    """
    if action.state not in ACTIVE_OWNER_ACTION_STATES:
        return action.state
    identity = action_identity(action)
    if probe_liveness(identity) is Liveness.DEAD:
        return OwnerActionState.UNCERTAIN
    return action.state


def is_action_effectively_active(action: RunOwnerAction) -> bool:
    """True while ``action`` still asserts live work (blocks a duplicate)."""
    return effective_action_state(action) in ACTIVE_OWNER_ACTION_STATES


def summarize_owner_actions(db: Database, run_id: str) -> OwnerActionSummary:
    """Build the durable per-run owner-action projection for the UI.

    Active work is determined by effective (liveness-projected) state, so
    a provably dead child surfaces as UNCERTAIN history rather than a
    stuck active row.  ``last_*`` surfaces the newest terminal
    FAILED / UNCERTAIN row per kind so the page can show the reason.
    """
    active_review: RunOwnerAction | None = None
    active_accept: RunOwnerAction | None = None
    last_review: RunOwnerAction | None = None
    last_accept: RunOwnerAction | None = None
    try:
        rows = db.list_owner_actions(run_id)
    except Exception:
        return OwnerActionSummary(None, None, None, None)
    for action in rows:
        effective = effective_action_state(action)
        if effective in ACTIVE_OWNER_ACTION_STATES:
            if action.kind is OwnerActionKind.REVIEW and active_review is None:
                active_review = action
            elif action.kind is OwnerActionKind.ACCEPT and active_accept is None:
                active_accept = action
            continue
        if effective not in (OwnerActionState.FAILED, OwnerActionState.UNCERTAIN):
            continue
        # Read-only projection: a persisted QUEUED / RUNNING row whose exact
        # child is provably DEAD surfaces here as UNCERTAIN history with the
        # canonical OWNER_PROCESS_DEAD reason.  Storage is never mutated by
        # this projection; the next explicit owner request still persists
        # the reconciliation through the atomic claim transaction.
        projected = action
        if (
            action.state in ACTIVE_OWNER_ACTION_STATES
            and effective is OwnerActionState.UNCERTAIN
        ):
            projected = replace(
                action,
                state=OwnerActionState.UNCERTAIN,
                reason=OWNER_PROCESS_DEAD_REASON,
            )
        if action.kind is OwnerActionKind.REVIEW and last_review is None:
            last_review = projected
        elif action.kind is OwnerActionKind.ACCEPT and last_accept is None:
            last_accept = projected
    return OwnerActionSummary(
        active_review=active_review,
        active_accept=active_accept,
        last_review=last_review,
        last_accept=last_accept,
    )


def owner_action_transcript_path(db_path: Path | str, action_id: str) -> Path:
    """Return the durable transcript destination for an owner action."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", action_id)
    return Path(db_path).parent / "transcripts" / f"owner-action-{safe}.ansi"


def build_owner_action_argv(
    kind: OwnerActionKind,
    run_id: str,
    db_path: Path | str,
    action_id: str,
    candidate_sha: str,
) -> list[str]:
    """Build the detached argv for an owner-action child.

    Single authority for the CLI-prefix seam (same seam the execution
    supervisor uses): argv only, never a shell.  The child re-executes
    the existing ``orch review`` / ``orch accept`` authority with the
    internal action identity and the expected candidate for fencing.
    """
    from saberops.web import resolve_owner_argv_prefix

    prefix = resolve_owner_argv_prefix()
    if kind is OwnerActionKind.REVIEW:
        return [
            *prefix,
            "review",
            run_id,
            "--db",
            str(db_path),
            "--timeout",
            str(REVIEW_CHILD_TIMEOUT_DEFAULT),
            "--action-id",
            action_id,
            "--expected-candidate",
            candidate_sha,
        ]
    return [
        *prefix,
        "accept",
        run_id,
        "--db",
        str(db_path),
        "--gate-timeout",
        str(ACCEPT_CHILD_TIMEOUT_DEFAULT),
        "--action-id",
        action_id,
        "--expected-candidate",
        candidate_sha,
    ]


def _queued_event_payload(action: RunOwnerAction) -> dict[str, Any]:
    """Secret-free SSE payload identifying an owner-action transition."""
    return {
        "action_id": action.action_id,
        "kind": action.kind.value,
        "candidate_sha": action.candidate_sha,
    }


class OwnerActionSupervisor:
    """Claim and detach owner Review / Accept work for one project store.

    The supervisor is a thin stateless launcher over the durable
    ``owner_actions`` table: claiming is atomic in SQLite (a single
    ``BEGIN IMMEDIATE`` transaction plus a partial unique index on active
    work), and the detached child outlives every caller by construction.
    """

    def __init__(
        self,
        db: Database,
        popen_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.db = db
        self._popen_factory: Callable[..., Any] = (
            popen_factory if popen_factory is not None else subprocess.Popen
        )

    def request_review(
        self, *, run_id: str, candidate_sha: str, target_repo: str
    ) -> OwnerActionLaunch:
        """Claim + detach a Review action; never executes review inline."""
        return self._request(
            kind=OwnerActionKind.REVIEW,
            run_id=run_id,
            candidate_sha=candidate_sha,
            target_repo=target_repo,
        )

    def request_accept(
        self, *, run_id: str, candidate_sha: str, target_repo: str
    ) -> OwnerActionLaunch:
        """Claim + detach an Accept action; never executes accept inline."""
        return self._request(
            kind=OwnerActionKind.ACCEPT,
            run_id=run_id,
            candidate_sha=candidate_sha,
            target_repo=target_repo,
        )

    def _request(
        self, *, kind: OwnerActionKind, run_id: str, candidate_sha: str, target_repo: str
    ) -> OwnerActionLaunch:
        action, created = self.db.claim_owner_action(
            action_id=new_owner_action_id(),
            run_id=run_id,
            kind=kind.value,
            candidate_sha=candidate_sha,
            target_repo=target_repo,
        )
        if not created:
            # An active action (LIVE or UNKNOWN liveness) already owns this
            # work: refuse the duplicate without launching anything.
            return OwnerActionLaunch(action=action, launched=False, duplicate=True)
        return self._spawn(action=action, kind=kind)

    def _fail_spawn_closed(self, *, action: RunOwnerAction, error: str) -> OwnerActionLaunch:
        """Settle a claimed-but-never-launched action to FAILED (best effort).

        Pre-Popen fail-closed path: no child exists, so no Popen, no
        STARTED, and no authority work ever happened.  Where storage
        remains writable the action settles FAILED with the bounded
        launch-failure reason and the normal FAILED evidence is emitted;
        where storage itself is unwritable the action is still never
        reported as launched.  The two evidence writes below are
        best-effort receipts, not the authority gate: the authority gate
        is the verified transcript persistence in :meth:`_spawn`, which
        is never swallowed.
        """
        current = action
        try:
            settled = self.db.mark_owner_action_terminal(
                action.action_id,
                state=OwnerActionState.FAILED.value,
                reason=LAUNCH_FAILED_REASON,
                error=bound_action_text(error),
            )
            if settled is not None:
                current = settled
        except Exception:
            current = action
        try:
            self.db.record_event(
                action.run_id,
                OWNER_ACTION_FAILED_EVENT,
                payload={
                    **_queued_event_payload(current),
                    "reason": LAUNCH_FAILED_REASON,
                },
            )
        except Exception:
            pass
        return OwnerActionLaunch(action=current, launched=False, duplicate=False)

    def _spawn(self, *, action: RunOwnerAction, kind: OwnerActionKind) -> OwnerActionLaunch:
        db_path = self.db.db_path
        target_repo = action.target_repo or ""
        argv = build_owner_action_argv(
            kind, action.run_id, db_path, action.action_id, action.candidate_sha
        )
        transcript = owner_action_transcript_path(db_path, action.action_id)
        expected_transcript = str(transcript)
        try:
            transcript.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return self._fail_spawn_closed(
                action=action,
                error=f"owner-action transcript directory unavailable: {exc}",
            )
        # Fail-closed transcript persistence: the deterministic transcript
        # destination is persisted on the QUEUED row and read back equal
        # before the child exists, so a fast child that transitions out of
        # QUEUED before post-Popen bookkeeping cannot lose it.  That
        # bookkeeping is QUEUED-fenced and therefore cannot repair a
        # missing path -- launching without this proof would violate the
        # durability contract.  This authority-bearing write is never
        # swallowed: persistence or verification failure fails closed with
        # zero child launch.
        try:
            self.db.set_owner_action_transcript(
                action.action_id, transcript_path=expected_transcript
            )
        except Exception as exc:
            return self._fail_spawn_closed(
                action=action,
                error=(
                    "owner-action transcript persistence failed: "
                    f"{type(exc).__name__}: {exc}"
                ),
            )
        try:
            verified = self.db.get_owner_action(action.action_id)
        except Exception as exc:
            return self._fail_spawn_closed(
                action=action,
                error=(
                    "owner-action transcript verification unreadable: "
                    f"{type(exc).__name__}: {exc}"
                ),
            )
        if verified is None or verified.transcript_path != expected_transcript:
            stored_path = verified.transcript_path if verified is not None else None
            return self._fail_spawn_closed(
                action=action,
                error=(
                    "owner-action transcript verification mismatch: expected "
                    f"{expected_transcript!r}, stored {stored_path!r}"
                ),
            )
        try:
            with open(transcript, "ab"):
                pass
        except OSError as exc:
            return self._fail_spawn_closed(
                action=action,
                error=f"owner-action transcript file creation failed: {exc}",
            )
        # Durable event chronology: QUEUED is recorded before Popen so a
        # fast detached child can never emit STARTED / COMPLETED before
        # QUEUED.  Required ordering for a claimed action is
        # QUEUED -> STARTED -> COMPLETED|FAILED (QUEUED -> FAILED on
        # Popen failure).
        queued_snapshot = self.db.get_owner_action(action.action_id)
        self.db.record_event(
            action.run_id,
            OWNER_ACTION_QUEUED_EVENT,
            payload=_queued_event_payload(
                queued_snapshot if queued_snapshot is not None else action
            ),
        )
        env = dict(os.environ)
        src_path = str(Path(__file__).resolve().parent.parent)
        current_pp = env.get("PYTHONPATH", "")
        if src_path not in current_pp.split(os.pathsep):
            env["PYTHONPATH"] = (src_path + os.pathsep + current_pp).rstrip(os.pathsep)
        proc: Any | None = None
        failure: Exception | None = None
        log_fh: Any | None = None
        devnull_fh: Any | None = None
        try:
            log_fh = open(transcript, "ab", buffering=0)
            devnull_fh = open(os.devnull, "rb")
            # argv only, never a shell.  ``start_new_session`` detaches the
            # child into its own session/process group so browser disconnect,
            # request completion, or Web-server restart can never cancel it.
            proc = self._popen_factory(
                argv,
                stdin=devnull_fh,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                cwd=target_repo,
                close_fds=True,
                start_new_session=True,
                env=env,
            )
        except Exception as exc:
            failure = exc
        finally:
            for handle in (log_fh, devnull_fh):
                try:
                    if handle is not None:
                        handle.close()
                except OSError:
                    pass
        if failure is not None or proc is None:
            failed = self.db.mark_owner_action_terminal(
                action.action_id,
                state=OwnerActionState.FAILED.value,
                reason=LAUNCH_FAILED_REASON,
                error=bound_action_text(str(failure) if failure is not None else "spawn failed"),
            )
            current = failed if failed is not None else action
            self.db.record_event(
                action.run_id,
                OWNER_ACTION_FAILED_EVENT,
                payload={
                    **_queued_event_payload(current),
                    "reason": LAUNCH_FAILED_REASON,
                },
            )
            return OwnerActionLaunch(action=current, launched=False, duplicate=False)
        pid = getattr(proc, "pid", None)
        identity: ProcessIdentity | None = None
        if isinstance(pid, int):
            identity = ProcessIdentity.for_pid(pid) or ProcessIdentity(
                host=ProcessIdentity.for_self().host,
                boot_id=ProcessIdentity.for_self().boot_id,
                pid=pid,
                start_ticks=None,
                session_id=pid,
                process_group_id=pid,
            )
            # Post-spawn bookkeeping only: QUEUED-fenced and
            # transcript-preserving, so a fast child that already reached
            # RUNNING or terminal is never regressed and never loses the
            # pre-persisted transcript destination.
            self.db.attach_owner_action_process(
                action.action_id,
                pid=pid,
                identity=identity,
                transcript_path=str(transcript),
            )
        refreshed = self.db.get_owner_action(action.action_id)
        current = refreshed if refreshed is not None else action
        # The caller returns immediately: no wait, no communicate, no join.
        # The detached child is fully independent from here on.
        return OwnerActionLaunch(action=current, launched=True, duplicate=False)


def _default_service_factory(db: Database) -> OrchestratorService:
    """Build the real production service for a detached action child."""
    from saberops.service import OrchestratorService

    return OrchestratorService(db=db)


def _fail_child_closed(
    db: Database, *, action: RunOwnerAction, reason: str, error: str
) -> tuple[RunOwnerAction, str]:
    """Persist a fail-closed child refusal with zero authority work."""
    db.mark_owner_action_terminal(
        action.action_id,
        state=OwnerActionState.FAILED.value,
        reason=reason,
        error=bound_action_text(error),
    )
    db.record_event(
        action.run_id,
        OWNER_ACTION_FAILED_EVENT,
        payload={**_queued_event_payload(action), "reason": reason},
    )
    return (action, reason)


def _check_child_action(
    db: Database,
    *,
    action_id: str,
    expected_kind: OwnerActionKind,
    run_id: str | None = None,
    expected_candidate_sha: str | None = None,
) -> tuple[RunOwnerAction | None, str | None]:
    """Load the exact action row and prove the invocation matches it.

    The persisted action row is the fence authority: the CLI ``run_id``
    (when supplied as part of the internal invocation) must equal
    ``action.run_id``, and any internal ``--expected-candidate`` value
    must equal ``action.candidate_sha``.  A mismatch fails closed with
    zero authority work so argv can never retarget a persisted action.

    Returns ``(action, None)`` on success, ``(action_or_None, exit_reason)``
    when the child must refuse before doing any authority work.
    """
    try:
        action = db.get_owner_action(action_id)
    except Exception:
        return (None, "ACTION_STORE_UNREADABLE")
    if action is None:
        return (None, "ACTION_UNKNOWN")
    if action.kind is not expected_kind:
        _fail_child_closed(
            db,
            action=action,
            reason=KIND_MISMATCH_REASON,
            error=(
                f"child kind {expected_kind.value} does not match "
                f"action kind {action.kind.value}"
            ),
        )
        return (action, KIND_MISMATCH_REASON)
    if run_id is not None and run_id != action.run_id:
        _fail_child_closed(
            db,
            action=action,
            reason=ACTION_RUN_MISMATCH_REASON,
            error=(
                f"child run {run_id!r} does not match action {action.action_id!r} "
                f"run {action.run_id!r}; refusing to retarget a persisted action"
            ),
        )
        return (action, ACTION_RUN_MISMATCH_REASON)
    if expected_candidate_sha:
        if expected_candidate_sha != action.candidate_sha:
            _fail_child_closed(
                db,
                action=action,
                reason=ACTION_CANDIDATE_MISMATCH_REASON,
                error=(
                    f"child expected-candidate {expected_candidate_sha!r} does not match "
                    f"persisted action {action.action_id!r} candidate "
                    f"{action.candidate_sha!r}; argv must not retarget a persisted action"
                ),
            )
            return (action, ACTION_CANDIDATE_MISMATCH_REASON)
    return (action, None)


def _fence_child_candidate(db: Database, *, action: RunOwnerAction) -> str | None:
    """Prove the run still refers to the persisted action candidate.

    The durable ``action.candidate_sha`` is the fence authority -- never
    argv.  Returns ``None`` when fenced (match); otherwise marks the
    action FAILED with the typed reason and returns it.  The caller must
    perform zero model launch (Review) or zero repository mutation
    (Accept) on a mismatch.
    """
    current = current_candidate_sha(db, action.run_id)
    if current is not None and current == action.candidate_sha:
        return None
    db.mark_owner_action_terminal(
        action.action_id,
        state=OwnerActionState.FAILED.value,
        reason=CANDIDATE_CHANGED_REASON,
        error=bound_action_text(
            f"run {action.run_id} candidate moved: expected {action.candidate_sha}, "
            f"current {current}; refusing to act on a newer candidate"
        ),
    )
    db.record_event(
        action.run_id,
        OWNER_ACTION_FAILED_EVENT,
        payload={**_queued_event_payload(action), "reason": CANDIDATE_CHANGED_REASON},
    )
    return CANDIDATE_CHANGED_REASON


def run_review_action_child(
    *,
    db: Database,
    action_id: str,
    expected_candidate_sha: str,
    run_id: str | None = None,
    timeout: float = REVIEW_CHILD_TIMEOUT_DEFAULT,
    service_factory: Callable[[Database], OrchestratorService] | None = None,
) -> int:
    """Detached Review child contract: fence, run authority, persist state.

    Action-row authority -> candidate fence -> mark RUNNING -> existing
    ``SaberOpsService.review_run()`` (with the R3-E1 exact Reviewer
    authority resolved inside the service) -> persist terminal state.
    Returns a process exit code (0 on SUCCEEDED).
    """
    action, refusal = _check_child_action(
        db,
        action_id=action_id,
        expected_kind=OwnerActionKind.REVIEW,
        run_id=run_id,
        expected_candidate_sha=expected_candidate_sha,
    )
    if refusal is not None or action is None:
        return 1
    fenced = _fence_child_candidate(db, action=action)
    if fenced is not None:
        return 1
    db.mark_owner_action_running(action.action_id, identity=ProcessIdentity.for_self())
    db.record_event(
        action.run_id, OWNER_ACTION_STARTED_EVENT, payload=_queued_event_payload(action)
    )
    factory = service_factory if service_factory is not None else _default_service_factory
    try:
        result = factory(db).review_run(run_id=action.run_id, timeout=timeout)
    except Exception as exc:
        db.mark_owner_action_terminal(
            action.action_id,
            state=OwnerActionState.FAILED.value,
            reason=type(exc).__name__,
            error=bound_action_text(f"{type(exc).__name__}: {exc}"),
        )
        db.record_event(
            action.run_id,
            OWNER_ACTION_FAILED_EVENT,
            payload={
                **_queued_event_payload(action),
                "reason": type(exc).__name__,
            },
        )
        return 1
    verdict = getattr(result, "verdict", None)
    verdict_value = getattr(verdict, "value", str(verdict))
    db.mark_owner_action_terminal(
        action.action_id,
        state=OwnerActionState.SUCCEEDED.value,
        result_summary=bound_action_text(
            f"{verdict_value}: {getattr(result, 'summary', '')}"
        ),
    )
    db.record_event(
        action.run_id,
        OWNER_ACTION_COMPLETED_EVENT,
        payload={**_queued_event_payload(action), "verdict": verdict_value},
    )
    return 0


def run_accept_action_child(
    *,
    db: Database,
    action_id: str,
    expected_candidate_sha: str,
    run_id: str | None = None,
    gate_timeout: float = ACCEPT_CHILD_TIMEOUT_DEFAULT,
    service_factory: Callable[[Database], OrchestratorService] | None = None,
) -> int:
    """Detached Accept child contract: fence, run authority, persist state.

    Action-row authority -> candidate fence -> mark RUNNING -> existing
    ``SaberOpsService.accept_run()`` (the AcceptEngine re-checks every
    canonical eligibility and gate authority immediately before mutation)
    -> persist terminal state.  Returns a process exit code (0 on SUCCEEDED).
    """
    action, refusal = _check_child_action(
        db,
        action_id=action_id,
        expected_kind=OwnerActionKind.ACCEPT,
        run_id=run_id,
        expected_candidate_sha=expected_candidate_sha,
    )
    if refusal is not None or action is None:
        return 1
    fenced = _fence_child_candidate(db, action=action)
    if fenced is not None:
        return 1
    db.mark_owner_action_running(action.action_id, identity=ProcessIdentity.for_self())
    db.record_event(
        action.run_id, OWNER_ACTION_STARTED_EVENT, payload=_queued_event_payload(action)
    )
    factory = service_factory if service_factory is not None else _default_service_factory
    try:
        result = factory(db).accept_run(run_id=action.run_id, gate_timeout=gate_timeout)
    except Exception as exc:
        db.mark_owner_action_terminal(
            action.action_id,
            state=OwnerActionState.FAILED.value,
            reason=type(exc).__name__,
            error=bound_action_text(f"{type(exc).__name__}: {exc}"),
        )
        db.record_event(
            action.run_id,
            OWNER_ACTION_FAILED_EVENT,
            payload={
                **_queued_event_payload(action),
                "reason": type(exc).__name__,
            },
        )
        return 1
    db.mark_owner_action_terminal(
        action.action_id,
        state=OwnerActionState.SUCCEEDED.value,
        result_summary=bound_action_text(
            f"{getattr(result, 'final_branch', '')}:{getattr(result, 'final_sha', '')}"
        ),
    )
    db.record_event(
        action.run_id,
        OWNER_ACTION_COMPLETED_EVENT,
        payload=_queued_event_payload(action),
    )
    return 0
