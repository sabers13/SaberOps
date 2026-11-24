"""Auto-accept an eligible local candidate while the owner is away.

Automatic acceptance of a local commit runs in ``away`` and ``critical-only``
only, never in ``ask-me`` where the owner approves every accept
(ADR-0008).  This module is the one seam
that turns that answer into engine behaviour:

* The mode gate is :func:`saberops.owner_mode.decide_owner_action` for
  :attr:`~saberops.owner_mode.ModeAction.ACCEPT`.  ``ask-me`` refuses; the
  authority preset must still grant ``auto_accept`` (``full_autonomy`` and
  ``full_autonomy_publish``); the owner mode can only narrow the preset.
* The work gate must be open.  Closing the planning CLI pauses the engine
  (ADR-0029), and auto-accept is work.
* The candidate must be the run's final successful attempt, carry durable
  passing gate evidence (a passed check bound to that attempt; the gate may
  not be disabled), and hold its own candidate-linked independent review with
  a substantive approval.  A run-wide or replaced-candidate review never
  qualifies (ADR-0008).  The read-only acceptance checklist must also be green.
* Acceptance goes through the existing :class:`saberops.accept.AcceptEngine`
  path, so the target-base check and the final compare-and-swap still run, and
  the post-integration gate keeps the ADR-0010 receipt rules.  Nothing in this
  module can push, publish or open a pull request: it only asks the engine to
  fast-forward the local target branch.
* One durable audit event (``auto_accept_completed``) and one notice are
  recorded per accepted candidate.  ``orch accept <run-id> --undo`` restores
  the target branch to the candidate's base and leaves the candidate branch,
  attempt and evidence untouched, so the owner can accept it manually later.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from saberops.accept import AcceptEngine
from saberops.gate_receipt import find_valid_receipt
from saberops.git import GitManager, GitRefRaceError
from saberops.models import AttemptStatus, ReviewVerdict, RunStatus
from saberops.owner_mode import ModeAction, OwnerMode, decide_owner_action
from saberops.process import run_process
from saberops.steering import undelivered_note_block_text, undelivered_notes_for_run

if TYPE_CHECKING:
    from saberops.db import Database
    from saberops.models import Attempt, ReviewResult, Run
    from saberops.runtime_gate import GateState

__all__ = [
    "ACCEPT_UNDONE_EVENT",
    "AUTO_ACCEPT_EVENT",
    "AUTO_ACCEPT_PARKED_EVENT",
    "AutoAcceptCheck",
    "AutoAcceptDecision",
    "AutoAcceptOutcome",
    "AutoAcceptUndo",
    "ConsequentialFloor",
    "auto_accept_candidate",
    "auto_accept_next_candidate",
    "consequential_floor",
    "evaluate_auto_accept",
    "undo_auto_accept",
]

#: Durable audit event recorded once per automatically accepted candidate.
AUTO_ACCEPT_EVENT: Final[str] = "auto_accept_completed"

#: Durable audit event recorded once per candidate the engine parks for the
#: owner because it is consequential.
AUTO_ACCEPT_PARKED_EVENT: Final[str] = "auto_accept_parked"

#: Durable audit event recorded by the one-command undo.
ACCEPT_UNDONE_EVENT: Final[str] = "accept_undone"

#: Substantive independent-review verdicts (same set the acceptance engine uses).
_APPROVING_VERDICTS: Final[frozenset[ReviewVerdict]] = frozenset(
    {ReviewVerdict.PASS, ReviewVerdict.PASS_WITH_BACKLOG}
)

#: Only these effective modes may ever carry the preset's acceptance grant.
_ACCEPTING_MODES: Final[frozenset[OwnerMode]] = frozenset(
    {OwnerMode.AWAY, OwnerMode.CRITICAL_ONLY}
)

#: Directory names that hold data migrations.  An applied migration is not
#: undone by reverting the code, so the engine treats any change under one as
#: consequential no matter what the planner recorded.
_MIGRATION_DIRECTORY_NAMES: Final[frozenset[str]] = frozenset(
    {"migration", "migrations"}
)

#: Tool configuration that only exists to run migrations.
_MIGRATION_FILE_NAMES: Final[frozenset[str]] = frozenset({"alembic.ini"})

#: Deleting more files than this in one candidate is a mass deletion, which
#: the fixed floor treats as consequential.  A routine refactor deletes a few.
_DELETED_FILES_FLOOR: Final[int] = 20


@dataclass(frozen=True)
class ConsequentialFloor:
    """The engine's fixed floor: what no planner or mode may call ordinary.

    ``reasons`` is empty when nothing matched.  A caller (the auto-accept
    decision) only ever adds to this: the planner's marker can make a
    candidate consequential, never make a floor hit disappear.
    """

    reasons: tuple[str, ...] = ()

    @property
    def hit(self) -> bool:
        return bool(self.reasons)

    def as_dict(self) -> dict[str, Any]:
        return {"hit": self.hit, "reasons": list(self.reasons)}


def _is_migration_path(path: str) -> bool:
    parts = [part for part in path.split("/") if part]
    if any(part.lower() in _MIGRATION_DIRECTORY_NAMES for part in parts[:-1]):
        return True
    return bool(parts) and parts[-1].lower() in _MIGRATION_FILE_NAMES


def _candidate_diff_entries(
    repo_path: str | Path, base_sha: str, candidate_sha: str
) -> list[tuple[str, tuple[str, ...]]] | None:
    """Return ``(status, paths)`` for the committed candidate diff, or None."""
    result = run_process(
        ["git", "diff", "--name-status", "--find-renames", f"{base_sha}..{candidate_sha}"],
        cwd=repo_path,
        timeout=30.0,
    )
    if not result.passed:
        return None
    entries: list[tuple[str, tuple[str, ...]]] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        status, *paths = line.split("\t")
        entries.append((status.strip(), tuple(path for path in paths if path)))
    return entries


def consequential_floor(
    repo_path: str | Path, base_sha: str, candidate_sha: str
) -> ConsequentialFloor:
    """Check the candidate diff against the engine's fixed consequential floor.

    Two mechanical rules today: a change under a migration directory (or the
    migration tool's configuration), and a mass deletion above
    :data:`_DELETED_FILES_FLOOR`.  Auth, security and "effects outside the work
    copy" stay the planner's judgment because no diff predicate can decide them
    without guessing; the floor is what the engine can prove itself.

    An unreadable diff is unverifiable, so it counts as a floor hit (fail
    closed: park and ask the owner).
    """
    entries = _candidate_diff_entries(repo_path, base_sha, candidate_sha)
    if entries is None:
        return ConsequentialFloor(
            ("the engine could not scan the candidate diff",)
        )
    migrations = sorted(
        {
            path
            for status, paths in entries
            if not status.startswith("D")
            for path in paths
            if _is_migration_path(path)
        }
    )
    deleted = sum(1 for status, _paths in entries if status.startswith("D"))
    reasons: list[str] = []
    if migrations:
        shown = ", ".join(migrations[:5])
        more = f" (+{len(migrations) - 5} more)" if len(migrations) > 5 else ""
        reasons.append(f"it changes a data migration ({shown}{more})")
    if deleted > _DELETED_FILES_FLOOR:
        reasons.append(
            f"it deletes {deleted} files, above the floor of "
            f"{_DELETED_FILES_FLOOR}"
        )
    return ConsequentialFloor(tuple(reasons))


def _planner_marked_consequential(run: Run) -> bool:
    """Read the planner's frozen marker; missing means ordinary."""
    if not run.config_json:
        return False
    data = json.loads(run.config_json)
    if not isinstance(data, dict):
        raise ValueError("frozen run configuration must be an object")
    return bool(data.get("consequential", False))


@dataclass(frozen=True)
class AutoAcceptCheck:
    """One read-only precondition for an automatic local acceptance."""

    key: str
    passed: bool
    reason: str


@dataclass(frozen=True)
class AutoAcceptDecision:
    """Typed answer to "may this candidate be accepted without the owner?"."""

    run_id: str
    allowed: bool
    mode: str
    candidate_sha: str | None
    review_id: str | None
    base_sha: str | None
    target_repo: str | None
    reason: str
    checks: tuple[AutoAcceptCheck, ...]
    #: True when the candidate would otherwise auto-accept but the engine
    #: parks it for the owner because it is consequential. Independent
    #: work continues; the candidate stays unaccepted.
    park_for_owner: bool = False
    #: Why it is consequential; empty when it is not.
    consequence: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Serialize for the CLI, logs and tests."""
        return {
            "run_id": self.run_id,
            "allowed": self.allowed,
            "mode": self.mode,
            "candidate_sha": self.candidate_sha,
            "review_id": self.review_id,
            "base_sha": self.base_sha,
            "target_repo": self.target_repo,
            "reason": self.reason,
            "park_for_owner": self.park_for_owner,
            "consequence": self.consequence,
            "checks": [
                {"key": check.key, "passed": check.passed, "reason": check.reason}
                for check in self.checks
            ],
        }


@dataclass(frozen=True)
class AutoAcceptOutcome:
    """Result of one automatic acceptance attempt."""

    run_id: str
    applied: bool
    reason: str
    candidate_sha: str | None = None
    final_sha: str | None = None
    target_repo: str | None = None
    final_branch: str | None = None
    mode: str | None = None
    review_id: str | None = None
    audit_event_id: int | None = None
    undo_command: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "applied": self.applied,
            "reason": self.reason,
            "candidate_sha": self.candidate_sha,
            "final_sha": self.final_sha,
            "target_repo": self.target_repo,
            "final_branch": self.final_branch,
            "mode": self.mode,
            "review_id": self.review_id,
            "audit_event_id": self.audit_event_id,
            "undo_command": self.undo_command,
        }


@dataclass(frozen=True)
class AutoAcceptUndo:
    """Result of undoing one automatic acceptance."""

    run_id: str
    undone: bool
    reason: str
    candidate_sha: str | None = None
    restored_sha: str | None = None
    kept_branch: str | None = None
    target_repo: str | None = None
    audit_event_id: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "undone": self.undone,
            "reason": self.reason,
            "candidate_sha": self.candidate_sha,
            "restored_sha": self.restored_sha,
            "kept_branch": self.kept_branch,
            "target_repo": self.target_repo,
            "audit_event_id": self.audit_event_id,
        }


def _check(checks: list[AutoAcceptCheck], key: str, passed: bool, reason: str) -> bool:
    checks.append(AutoAcceptCheck(key=key, passed=passed, reason=reason))
    return passed


def _refused(
    *,
    run_id: str,
    mode: str,
    checks: list[AutoAcceptCheck],
    candidate_sha: str | None = None,
    review_id: str | None = None,
    base_sha: str | None = None,
    target_repo: str | None = None,
    park_for_owner: bool = False,
    consequence: str = "",
) -> AutoAcceptDecision:
    failures = [check.reason for check in checks if not check.passed]
    reason = failures[0] if failures else "auto-accept is not allowed"
    return AutoAcceptDecision(
        run_id=run_id,
        allowed=False,
        mode=mode,
        candidate_sha=candidate_sha,
        review_id=review_id,
        base_sha=base_sha,
        target_repo=target_repo,
        reason=reason,
        checks=tuple(checks),
        park_for_owner=park_for_owner,
        consequence=consequence,
    )


def _final_candidate_attempt(db: Database, run_id: str) -> Attempt | None:
    """Return the latest successful attempt carrying a commit SHA."""
    attempts = db.get_attempts_for_run(run_id)
    successful = [
        attempt
        for attempt in attempts
        if attempt.status == AttemptStatus.SUCCESS and attempt.commit_sha
    ]
    return successful[-1] if successful else None



def _already_accepted_candidate(db: Database, run_id: str) -> str | None:
    """Return the SHA of the most recent accepted candidate, if any."""
    from saberops.autonomous_runtime import _latest_run_accepted

    accepted = _latest_run_accepted(db, run_id)
    if accepted is None:
        return None
    payload = accepted[1]
    candidate = payload.get("candidate_sha")
    return str(candidate) if isinstance(candidate, str) and candidate else None


def _gate_evidence(
    db: Database, run: Run, attempt: Attempt, candidate_sha: str
) -> tuple[bool, str]:
    """Return ``(ok, reason)`` for durable passing gate evidence on the candidate.

    The candidate must own a passed check row from its run-time gate.  A
    validated ADR-0010 receipt for the same candidate and base is accepted as
    equivalent evidence because it records a passing full gate for that exact
    tree.  A disabled gate and a receipt for a different tree never qualify.
    """
    engine = AcceptEngine(db=db)
    try:
        gate_enabled, _review_disabled = engine._validation_policy(run.id)
    except RuntimeError as exc:
        return False, f"the frozen validation policy is unreadable: {exc}"
    if not gate_enabled:
        return False, (
            "the project gate is disabled for this run; auto-accept requires a "
            "passing gate"
        )
    checks = db.get_checks_for_run(run.id)
    if any(check.attempt_id == attempt.id and check.passed for check in checks):
        return True, f"attempt #{attempt.attempt_number} holds a passed gate check"
    try:
        receipt = find_valid_receipt(db, run, candidate_sha=candidate_sha)
    except Exception as exc:  # malformed or unreadable receipt fails closed
        return False, f"the gate receipt is unreadable: {exc}"
    if receipt is not None:
        return True, "a valid ADR-0010 gate receipt covers the exact candidate"
    return False, (
        f"candidate {candidate_sha[:8]} has no passing gate evidence "
        "(no passed check and no valid receipt)"
    )


def _review_evidence(
    engine: AcceptEngine, run_id: str, candidate_sha: str
) -> tuple[ReviewResult | None, str]:
    """Return ``(bound_review, reason)`` for candidate-linked review evidence."""
    bound, historical = engine.candidate_review_evidence(run_id, candidate_sha)
    if bound is None:
        suffix = (
            f" (older review {historical.id} is historical only)"
            if historical is not None
            else ""
        )
        return None, (
            f"candidate {candidate_sha[:8]} has no candidate-linked independent "
            f"review{suffix}"
        )
    if bound.verdict not in _APPROVING_VERDICTS:
        return None, (
            f"candidate {candidate_sha[:8]} review verdict is "
            f"{bound.verdict.value}, not a substantive approval"
        )
    return bound, (
        f"review {bound.id} approved candidate {candidate_sha[:8]} "
        f"({bound.verdict.value})"
    )


def evaluate_auto_accept(
    *,
    db: Database,
    run_id: str,
    now: datetime | None = None,
    engine: AcceptEngine | None = None,
    gate_state: GateState | None = None,
) -> AutoAcceptDecision:
    """Decide read-only whether ``run_id`` may be accepted without the owner."""
    checks: list[AutoAcceptCheck] = []

    try:
        mode_decision = decide_owner_action(ModeAction.ACCEPT, now=now)
    except Exception as exc:  # corrupt mode/authority documents fail closed
        _check(checks, "owner_mode", False, f"owner mode is unreadable: {exc}")
        return _refused(run_id=run_id, mode="unreadable", checks=checks)
    mode_value = mode_decision.mode.value

    mode_ok = mode_decision.allowed and mode_decision.mode in _ACCEPTING_MODES
    mode_reason = (
        mode_decision.reason
        if mode_decision.allowed
        else (
            f"auto-accept stays manual in {mode_value}: {mode_decision.reason}"
        )
    )
    if not _check(checks, "owner_mode", mode_ok, mode_reason):
        return _refused(run_id=run_id, mode=mode_value, checks=checks)

    if gate_state is None:
        from saberops.runtime_gate import evaluate_gate

        gate_state = evaluate_gate(db=db, now=now)
    if not _check(
        checks,
        "work_gate",
        not gate_state.closed,
        "the work gate is open"
        if not gate_state.closed
        else (
            "the work gate is closed "
            f"({gate_state.reason or 'unknown'}: {gate_state.detail}); "
            "no work starts, including auto-accept"
        ),
    ):
        return _refused(run_id=run_id, mode=mode_value, checks=checks)

    run = db.get_run(run_id)
    if run is None:
        _check(checks, "run", False, f"run '{run_id}' was not found")
        return _refused(run_id=run_id, mode=mode_value, checks=checks)
    if not _check(
        checks,
        "run_completed",
        run.status == RunStatus.COMPLETED,
        f"run status is {run.status.value}; expected completed"
        if run.status != RunStatus.COMPLETED
        else "run is completed",
    ):
        return _refused(
            run_id=run_id,
            mode=mode_value,
            checks=checks,
            base_sha=run.base_commit,
            target_repo=run.target_repo,
        )

    attempt = _final_candidate_attempt(db, run_id)
    if attempt is None or not attempt.commit_sha:
        _check(checks, "candidate", False, "the run has no successful candidate commit")
        return _refused(
            run_id=run_id,
            mode=mode_value,
            checks=checks,
            base_sha=run.base_commit,
            target_repo=run.target_repo,
        )
    candidate_sha = attempt.commit_sha
    _check(
        checks,
        "candidate",
        True,
        f"candidate is commit {candidate_sha[:8]} from attempt #{attempt.attempt_number}",
    )

    # The away-mode pass must never accept past a steering note
    # that never reached a worker.  The owner is told to redirect or discard it;
    # until then the candidate is not auto-acceptable.
    pending_notes = undelivered_notes_for_run(db, run_id)
    if not _check(
        checks,
        "steering_notes",
        not pending_notes,
        (
            undelivered_note_block_text(pending_notes[0].job_id, pending_notes)
            if pending_notes
            else "no undelivered steering note blocks this candidate"
        ),
    ):
        return _refused(
            run_id=run_id,
            mode=mode_value,
            checks=checks,
            candidate_sha=candidate_sha,
            base_sha=run.base_commit,
            target_repo=run.target_repo,
        )

    accepted_sha = _already_accepted_candidate(db, run_id)
    if not _check(
        checks,
        "not_already_accepted",
        accepted_sha != candidate_sha,
        (
            f"candidate {candidate_sha[:8]} is already accepted; an undo is not "
            "re-run automatically"
        )
        if accepted_sha == candidate_sha
        else "the candidate has no acceptance record",
    ):
        return _refused(
            run_id=run_id,
            mode=mode_value,
            checks=checks,
            candidate_sha=candidate_sha,
            base_sha=run.base_commit,
            target_repo=run.target_repo,
        )

    engine = engine or AcceptEngine(db=db)
    gate_ok, gate_reason = _gate_evidence(db, run, attempt, candidate_sha)
    if not _check(checks, "gate", gate_ok, gate_reason):
        return _refused(
            run_id=run_id,
            mode=mode_value,
            checks=checks,
            candidate_sha=candidate_sha,
            base_sha=run.base_commit,
            target_repo=run.target_repo,
        )

    review, review_reason = _review_evidence(engine, run_id, candidate_sha)
    review_id = review.id if review is not None else None
    if not _check(checks, "review", review is not None, review_reason):
        return _refused(
            run_id=run_id,
            mode=mode_value,
            checks=checks,
            candidate_sha=candidate_sha,
            base_sha=run.base_commit,
            target_repo=run.target_repo,
        )

    try:
        eligibility = engine.check_accept_eligibility(run_id)
    except Exception as exc:  # unreadable store/projection fails closed
        _check(checks, "checklist", False, f"the acceptance checklist is unreadable: {exc}")
        return _refused(
            run_id=run_id,
            mode=mode_value,
            checks=checks,
            candidate_sha=candidate_sha,
            review_id=review_id,
            base_sha=run.base_commit,
            target_repo=run.target_repo,
        )
    if not eligibility.eligible:
        failures = [
            f"{check.label}: {check.reason}"
            for check in eligibility.checks
            if not check.passed()
        ]
        _check(
            checks,
            "checklist",
            False,
            "the acceptance checklist is not green: " + "; ".join(failures),
        )
        return _refused(
            run_id=run_id,
            mode=mode_value,
            checks=checks,
            candidate_sha=candidate_sha,
            review_id=review_id,
            base_sha=run.base_commit,
            target_repo=run.target_repo,
        )
    _check(checks, "checklist", True, "every acceptance check passes")

    # Modes stop at what is actually consequential.  The candidate would
    # auto-accept, but a data migration, a mass deletion or the planner's own
    # marker parks it for the owner instead.  The planner can only add; the
    # engine's floor is computed here from the diff and never read from the
    # frozen configuration.  Independent work continues because the refusal
    # only skips this candidate.
    try:
        planner_marked = _planner_marked_consequential(run)
    except (TypeError, ValueError) as exc:
        _check(
            checks,
            "consequential",
            False,
            f"the frozen run configuration is unreadable: {exc}",
        )
        return _refused(
            run_id=run_id,
            mode=mode_value,
            checks=checks,
            candidate_sha=candidate_sha,
            review_id=review_id,
            base_sha=run.base_commit,
            target_repo=run.target_repo,
        )
    floor = (
        consequential_floor(run.target_repo, run.base_commit, candidate_sha)
        if run.base_commit
        else ConsequentialFloor(("the run did not record a base commit",))
    )
    park_reasons = list(floor.reasons)
    if planner_marked:
        park_reasons.insert(0, "the planner marked it consequential")
    consequence = "; ".join(park_reasons)
    if not _check(
        checks,
        "consequential",
        not park_reasons,
        (
            f"consequential candidate parked for the owner: {consequence}"
            if park_reasons
            else "the candidate is not consequential"
        ),
    ):
        return _refused(
            run_id=run_id,
            mode=mode_value,
            checks=checks,
            candidate_sha=candidate_sha,
            review_id=review_id,
            base_sha=run.base_commit,
            target_repo=run.target_repo,
            park_for_owner=True,
            consequence=consequence,
        )

    return AutoAcceptDecision(
        run_id=run_id,
        allowed=True,
        mode=mode_value,
        candidate_sha=candidate_sha,
        review_id=review_id,
        base_sha=run.base_commit,
        target_repo=run.target_repo,
        reason=(
            f"{mode_value} with a full-autonomy preset: gate passed and review "
            f"{review_id} approved candidate {candidate_sha[:8]}"
        ),
        checks=tuple(checks),
    )


def _notification_ledger(db: Database) -> Path:
    owner_db = getattr(db, "owner_db", db)
    path = Path(owner_db.db_path)
    return path.with_name(path.name + ".notifications.sqlite3")


def _notify(db: Database, key: str, title: str, body: str) -> None:
    try:
        from saberops.notifications import notify_once

        notify_once(_notification_ledger(db), key, title, body)
    except Exception:
        logging.getLogger(__name__).exception("auto-accept notice failed")


def _park_consequential(
    *,
    db: Database,
    decision: AutoAcceptDecision,
    notifier: Any | None,
) -> AutoAcceptOutcome:
    """Record and announce one candidate the engine parks for the owner.

    The event is written once per candidate so a scheduler tick that sees the
    same parked candidate again does not append duplicate evidence; the notice
    ledger is idempotent on its own key.
    """
    candidate_sha = decision.candidate_sha or ""
    event_id = _already_parked_event_id(db, decision.run_id, candidate_sha)
    if event_id is None:
        event_id = db.record_event(
            decision.run_id,
            AUTO_ACCEPT_PARKED_EVENT,
            payload={
                "candidate_sha": candidate_sha,
                "base_sha": decision.base_sha,
                "review_id": decision.review_id,
                "mode": decision.mode,
                "consequence": decision.consequence,
                "reason": decision.reason,
                "local_commit_only": True,
                "pushed": False,
                "accept_command": f"orch accept {decision.run_id}",
            },
        )
    suffix = f" Acknowledge with `orch accept {decision.run_id}` or discard it."
    body = (
        f"SaberOps parked candidate {(candidate_sha or '?')[:8]} for run "
        f"{decision.run_id} while the owner was {decision.mode}.\n"
        f"It is green and reviewed, but it is consequential: "
        f"{decision.consequence}.\n"
        "It was not accepted; independent work continues." + suffix
    )
    title = "SaberOps parked a consequential candidate"
    if notifier is not None:
        notifier(
            _notification_ledger(db),
            f"auto-accept-parked:{decision.run_id}:{candidate_sha}",
            title,
            body,
        )
    else:
        _notify(
            db,
            f"auto-accept-parked:{decision.run_id}:{candidate_sha}",
            title,
            body,
        )
    return AutoAcceptOutcome(
        run_id=decision.run_id,
        applied=False,
        reason=decision.reason,
        candidate_sha=decision.candidate_sha,
        target_repo=decision.target_repo,
        mode=decision.mode,
        review_id=decision.review_id,
        audit_event_id=event_id,
    )


def _already_parked_event_id(db: Database, run_id: str, candidate_sha: str) -> int | None:
    for event in db.get_events_by_type(run_id, AUTO_ACCEPT_PARKED_EVENT):
        if (event.payload or {}).get("candidate_sha") == candidate_sha:
            return int(event.id)
    return None


def auto_accept_candidate(
    *,
    db: Database,
    run_id: str,
    now: datetime | None = None,
    engine: AcceptEngine | None = None,
    gate_state: GateState | None = None,
    notifier: Any | None = None,
) -> AutoAcceptOutcome:
    """Accept one eligible candidate as a local commit, or refuse with a reason."""
    decision = evaluate_auto_accept(
        db=db, run_id=run_id, now=now, engine=engine, gate_state=gate_state
    )
    if not decision.allowed:
        if decision.park_for_owner:
            return _park_consequential(
                db=db, decision=decision, notifier=notifier
            )
        return AutoAcceptOutcome(run_id=run_id, applied=False, reason=decision.reason)
    assert decision.candidate_sha is not None
    engine = engine or AcceptEngine(db=db)
    undo_command = f"orch accept {run_id} --undo"
    automatic_audit = {
        "mode": decision.mode,
        "review_id": decision.review_id,
        "base_sha": decision.base_sha,
        "local_commit_only": True,
        "pushed": False,
        "undo_command": undo_command,
    }
    try:
        result = engine.accept_run(run_id, automatic=automatic_audit)
    except Exception as exc:
        return AutoAcceptOutcome(
            run_id=run_id,
            applied=False,
            reason=f"acceptance refused: {exc}",
            candidate_sha=decision.candidate_sha,
            mode=decision.mode,
            review_id=decision.review_id,
        )

    event_id = db.record_event(
        run_id,
        AUTO_ACCEPT_EVENT,
        payload={
            "candidate_sha": result.final_sha,
            "base_sha": decision.base_sha,
            "review_id": decision.review_id,
            "mode": decision.mode,
            "final_branch": result.final_branch,
            "target_repo": result.target_repo,
            "local_commit_only": True,
            "pushed": False,
            "undo_command": undo_command,
        },
    )
    body = (
        f"SaberOps auto-accepted candidate {result.final_sha[:8]} for run {run_id} "
        f"on '{result.final_branch}' while the owner was {decision.mode}.\n"
        "Local commit only; nothing was pushed.\n"
        f"Undo: {undo_command}"
    )
    if notifier is not None:
        notifier(
            _notification_ledger(db),
            f"auto-accept:{run_id}:{result.final_sha}",
            "SaberOps auto-accepted a local commit",
            body,
        )
    else:
        _notify(
            db,
            f"auto-accept:{run_id}:{result.final_sha}",
            "SaberOps auto-accepted a local commit",
            body,
        )
    return AutoAcceptOutcome(
        run_id=run_id,
        applied=True,
        reason=result.summary,
        candidate_sha=decision.candidate_sha,
        final_sha=result.final_sha,
        target_repo=result.target_repo,
        final_branch=result.final_branch,
        mode=decision.mode,
        review_id=decision.review_id,
        audit_event_id=event_id,
        undo_command=undo_command,
    )


def auto_accept_next_candidate(
    *,
    db: Database,
    now: datetime | None = None,
    limit: int = 25,
    notifier: Any | None = None,
) -> AutoAcceptOutcome | None:
    """Try at most one completed candidate; used by the scheduler tick.

    Refuses immediately unless the effective mode can auto-accept at all, so a
    normal attended store never scans completed runs on every tick.
    """
    try:
        mode_decision = decide_owner_action(ModeAction.ACCEPT, now=now)
    except Exception:
        logging.getLogger(__name__).exception("owner mode unavailable; no auto-accept")
        return None
    if not (mode_decision.allowed and mode_decision.mode in _ACCEPTING_MODES):
        return None
    try:
        runs = db.list_runs(limit=limit)
    except Exception:
        logging.getLogger(__name__).exception("run listing failed; no auto-accept")
        return None
    for run in runs:
        if run.status is not RunStatus.COMPLETED:
            continue
        try:
            outcome = auto_accept_candidate(
                db=db, run_id=run.id, now=now, notifier=notifier
            )
        except Exception:
            logging.getLogger(__name__).exception(
                "auto-accept evaluation failed for run %s", run.id
            )
            continue
        if outcome.applied:
            return outcome
    return None


def undo_auto_accept(
    *, db: Database, run_id: str, now: datetime | None = None
) -> AutoAcceptUndo:
    """Restore the target branch to the candidate's base, keeping the candidate.

    Only an automatic acceptance may be undone, only while the target still
    points at that exact candidate, and only when the working tree is clean.
    The candidate branch keeps naming the candidate commit, so the owner can
    accept it manually afterwards.
    """
    del now  # reserved for the audit timestamp; events carry their own time
    from saberops.autonomous_runtime import _latest_run_accepted

    accepted = _latest_run_accepted(db, run_id)
    if accepted is None:
        return AutoAcceptUndo(
            run_id=run_id,
            undone=False,
            reason="no durable acceptance record exists for this run",
        )
    accepted_id, accepted_payload = accepted
    automatic = accepted_payload.get("automatic")
    if not isinstance(automatic, dict):
        return AutoAcceptUndo(
            run_id=run_id,
            undone=False,
            reason="the latest acceptance is not an automatic acceptance; refusing",
        )
    candidate_sha = str(accepted_payload.get("candidate_sha") or "")
    base_sha = str(automatic.get("base_sha") or "")
    if not candidate_sha or not base_sha:
        return AutoAcceptUndo(
            run_id=run_id,
            undone=False,
            reason="the auto-accept audit record is incomplete; refusing to guess",
        )

    undone_ids = [
        int(event.id)
        for event in db.get_events_by_type(run_id, ACCEPT_UNDONE_EVENT)
        if (event.payload or {}).get("candidate_sha") == candidate_sha
    ]
    if undone_ids and max(undone_ids) > accepted_id:
        return AutoAcceptUndo(
            run_id=run_id,
            undone=False,
            reason=f"candidate {candidate_sha[:8]} was already undone",
            candidate_sha=candidate_sha,
        )

    run = db.get_run(run_id)
    if run is None:
        return AutoAcceptUndo(
            run_id=run_id, undone=False, reason=f"run '{run_id}' was not found"
        )
    target_repo = Path(str(accepted_payload.get("target_repo") or run.target_repo))
    final_branch = str(accepted_payload.get("final_branch") or "")
    if not final_branch:
        return AutoAcceptUndo(
            run_id=run_id,
            undone=False,
            reason="the acceptance record names no target branch",
            candidate_sha=candidate_sha,
        )

    attempts = db.get_attempts_for_run(run_id)
    candidate_branch = next(
        (a.branch_name for a in attempts if a.commit_sha == candidate_sha and a.branch_name),
        None,
    )
    if candidate_branch is None:
        return AutoAcceptUndo(
            run_id=run_id,
            undone=False,
            reason=(
                "the candidate has no branch that would keep it after the undo; "
                "refusing to move the target"
            ),
            candidate_sha=candidate_sha,
            target_repo=str(target_repo),
        )
    try:
        if not GitManager.is_clean(target_repo):
            return AutoAcceptUndo(
                run_id=run_id,
                undone=False,
                reason=(
                    f"target repository '{target_repo}' is not clean; refusing to "
                    "move the branch under uncommitted work"
                ),
                candidate_sha=candidate_sha,
                target_repo=str(target_repo),
            )
        head = GitManager.get_head_commit(target_repo)
        if head != candidate_sha:
            return AutoAcceptUndo(
                run_id=run_id,
                undone=False,
                reason=(
                    f"target HEAD is {head[:8]}, not the auto-accepted candidate "
                    f"{candidate_sha[:8]}; refusing to overwrite later work"
                ),
                candidate_sha=candidate_sha,
                target_repo=str(target_repo),
            )
        current_branch = GitManager.get_current_branch(target_repo)
        if current_branch != final_branch:
            return AutoAcceptUndo(
                run_id=run_id,
                undone=False,
                reason=(
                    f"target is on '{current_branch}', not the accepted branch "
                    f"'{final_branch}'; refusing to move an unrelated checkout"
                ),
                candidate_sha=candidate_sha,
                target_repo=str(target_repo),
            )
        branch_commit = GitManager.get_ref_commit(target_repo, candidate_branch)
        if branch_commit != candidate_sha:
            return AutoAcceptUndo(
                run_id=run_id,
                undone=False,
                reason=(
                    f"candidate branch '{candidate_branch}' no longer names "
                    f"{candidate_sha[:8]}; undo would lose the candidate"
                ),
                candidate_sha=candidate_sha,
                target_repo=str(target_repo),
            )
        GitManager.compare_and_swap_ref(
            target_repo,
            f"refs/heads/{final_branch}",
            expected_old_sha=candidate_sha,
            new_target_sha=base_sha,
        )
        GitManager.sync_index_worktree(target_repo)
        restored = GitManager.get_head_commit(target_repo)
        if restored != base_sha:
            raise GitRefRaceError(
                "target ref changed after the undo compare-and-swap"
            )
    except Exception as exc:
        return AutoAcceptUndo(
            run_id=run_id,
            undone=False,
            reason=f"undo failed: {exc}",
            candidate_sha=candidate_sha,
            target_repo=str(target_repo),
        )

    event_id = db.record_event(
        run_id,
        ACCEPT_UNDONE_EVENT,
        payload={
            "candidate_sha": candidate_sha,
            "restored_sha": base_sha,
            "final_branch": final_branch,
            "kept_branch": candidate_branch,
            "target_repo": str(target_repo),
            "manual_accept_command": f"orch accept {run_id}",
        },
    )
    return AutoAcceptUndo(
        run_id=run_id,
        undone=True,
        reason=(
            f"restored '{final_branch}' to {base_sha[:8]}; candidate "
            f"{candidate_sha[:8]} stays on '{candidate_branch or 'its branch'}'"
        ),
        candidate_sha=candidate_sha,
        restored_sha=base_sha,
        kept_branch=candidate_branch,
        target_repo=str(target_repo),
        audit_event_id=event_id,
    )
