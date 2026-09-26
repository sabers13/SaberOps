"""R3-E2.1: durable owner-action fence + live-projection corrections.

Bounded follow-up to R3-E2 without redesigning the owner-action system:

1. The persisted action row (``RunOwnerAction.candidate_sha``) is the
   fence authority -- never CLI argv.  A differing internal
   ``--expected-candidate`` value fails closed with zero Review model
   launch / zero Accept repository mutation, and the CLI run id must be
   consistent with ``action.run_id``.
2. A provably dead active action with no terminal receipt projects as
   UNCERTAIN / OWNER_PROCESS_DEAD (read-only) in both the run-detail
   page and the ``/actions`` JSON, for Review and Accept.  UNKNOWN
   liveness stays active and duplicate-blocking.
3. Owner-action SSE works after run completion over the existing
   run-page EventSource (no second polling loop): queued/started/
   completed/failed/uncertain reach the client and drive an
   authoritative ``/actions`` refresh.
4. Spawn ordering is QUEUED -> STARTED -> COMPLETED|FAILED (QUEUED ->
   FAILED on Popen failure) even against a fast child that transitions
   before the parent's post-Popen bookkeeping, and the durable
   transcript destination survives that race without regressing a
   terminal row.
"""

from __future__ import annotations

import socket
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from saberops.accept import AcceptResult
from saberops.db import Database
from saberops.models import (
    Attempt,
    AttemptStatus,
    CheckResult,
    ReviewResult,
    ReviewVerdict,
    Run,
    RunStatus,
    Tier,
    WorkerCandidate,
)
from saberops.owner_actions import (
    ACTION_CANDIDATE_MISMATCH_REASON,
    ACTION_RUN_MISMATCH_REASON,
    CANDIDATE_CHANGED_REASON,
    LAUNCH_FAILED_REASON,
    OWNER_ACTION_COMPLETED_EVENT,
    OWNER_ACTION_FAILED_EVENT,
    OWNER_ACTION_QUEUED_EVENT,
    OWNER_ACTION_STARTED_EVENT,
    OWNER_PROCESS_DEAD_REASON,
    OwnerActionState,
    OwnerActionSupervisor,
    effective_action_state,
    run_accept_action_child,
    run_review_action_child,
    summarize_owner_actions,
)
from saberops.ownership import ProcessIdentity, current_boot_id
from saberops.service import OrchestratorService
from saberops.web import _owner_actions_stream_active, create_app

_CREATED = "2026-09-24T10:00:00+00:00"
_SHA_A = "a" * 40
_SHA_B = "b" * 40
_DEAD_PID = 2147483647

_REAL_POPEN = subprocess.Popen


def _db(path: Path) -> Database:
    return Database(path / "orch.db")


def _seed_run(db: Database, run_id: str, repo: str = "/tmp/r3e21-target") -> None:
    db.create_run(
        Run(
            id=run_id,
            task="r3e2.1 owner action probe",
            target_repo=repo,
            base_commit="c" * 40,
            tier=Tier.T1,
            training_allowed=True,
            gate_command="true",
            status=RunStatus.COMPLETED,
            created_at=_CREATED,
            completed_at=_CREATED,
        )
    )


def _gate_pass(db: Database, run_id: str, attempt_id: str, num: int, sha: str) -> None:
    db.create_attempt(
        Attempt(
            id=attempt_id,
            run_id=run_id,
            attempt_number=num,
            provider="opencode",
            model="test-model",
            worktree_path="/tmp/r3e21-wt",
            branch_name="r3e21-candidate",
            status=AttemptStatus.SUCCESS,
            commit_sha=sha,
            created_at=_CREATED,
            base_sha="c" * 40,
        )
    )
    db.create_check(
        CheckResult(
            id=f"check-{attempt_id}",
            attempt_id=attempt_id,
            gate_command="true",
            exit_code=0,
            stdout="ok",
            stderr="",
            passed=True,
            created_at=_CREATED,
        )
    )


def _is_owner_child_spawn(args: tuple[Any, ...], kwargs: dict[str, Any]) -> bool:
    argv = args[0] if args else kwargs.get("args")
    return isinstance(argv, list) and "saberops.cli" in [str(part) for part in argv]


class _CountingPopen:
    """Owner-action children become real live sleepers; all else is real."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._sleepers: list[subprocess.Popen[bytes]] = []
        self._lock = threading.Lock()

    @property
    def call_count(self) -> int:
        with self._lock:
            return len(self.calls)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if not _is_owner_child_spawn(args, kwargs):
            return _REAL_POPEN(*args, **kwargs)
        sleeper = _REAL_POPEN(["sleep", "120"])
        with self._lock:
            self.calls.append({"args": args, "kwargs": kwargs})
            self._sleepers.append(sleeper)
        return sleeper

    def close(self) -> None:
        with self._lock:
            sleepers = list(self._sleepers)
            self._sleepers.clear()
        for sleeper in sleepers:
            try:
                sleeper.kill()
                sleeper.wait(timeout=10)
            except Exception:
                pass


@pytest.fixture
def popen() -> Iterator[_CountingPopen]:
    fake = _CountingPopen()
    yield fake
    fake.close()


class _StubReviewService(OrchestratorService):
    def __init__(self, db: Database) -> None:
        super().__init__(db=db, readiness_service=False)
        self.review_calls: list[dict[str, Any]] = []

    def review_run(
        self,
        run_id: str,
        reviewer_override: WorkerCandidate | None = None,
        timeout: float = 1200.0,
        review_limit: int | None = None,
    ) -> ReviewResult:
        self.review_calls.append({"run_id": run_id, "timeout": timeout})
        result = ReviewResult(
            id="rev-stub-1",
            run_id=run_id,
            provider="opencode",
            model="stub-reviewer",
            verdict=ReviewVerdict.PASS,
            summary="stub pass",
            details="stub details",
            created_at=_CREATED,
        )
        self.db.create_review(result)
        self.db.record_event(run_id, "review_completed", payload={"review_id": result.id})
        return result


class _StubAcceptService(OrchestratorService):
    def __init__(self, db: Database) -> None:
        super().__init__(db=db, readiness_service=False)
        self.accept_calls: list[dict[str, Any]] = []

    def accept_run(self, run_id: str, gate_timeout: float = 300.0) -> AcceptResult:
        self.accept_calls.append({"run_id": run_id, "gate_timeout": gate_timeout})
        result = AcceptResult(
            run_id=run_id,
            accepted=True,
            target_repo="/tmp/r3e21-target",
            final_branch="main",
            final_sha=_SHA_A,
            summary="stub accept",
            gate_passed=True,
        )
        self.db.record_event(run_id, "run_accepted", payload={"candidate_sha": _SHA_A})
        return result


class _ExplodingReviewService(_StubReviewService):
    def __init__(self, db: Database, message: str) -> None:
        super().__init__(db)
        self._message = message

    def review_run(
        self,
        run_id: str,
        reviewer_override: WorkerCandidate | None = None,
        timeout: float = 1200.0,
        review_limit: int | None = None,
    ) -> ReviewResult:
        self.review_calls.append({"run_id": run_id})
        raise RuntimeError(self._message)


def _dead_identity() -> ProcessIdentity:
    return ProcessIdentity(
        host=socket.gethostname(),
        boot_id=current_boot_id(),
        pid=_DEAD_PID,
        start_ticks=1,
        session_id=_DEAD_PID,
        process_group_id=_DEAD_PID,
    )


def _unknown_identity() -> ProcessIdentity:
    return ProcessIdentity(
        host="foreign-host.invalid",
        boot_id=None,
        pid=424242,
        start_ticks=None,
        session_id=None,
        process_group_id=None,
    )


def _event_ids(db: Database, run_id: str, event_type: str) -> list[int]:
    return [event.id for event in db.get_events_by_type(run_id, event_type)]


# --- 1. Durable candidate SHA is the fence authority ---------------------------


def test_review_argv_mismatch_fails_closed_with_zero_launch(tmp_path: Path) -> None:
    """Row = A, CLI expected = B, current = B -> FAILED before authority."""
    db = _db(tmp_path)
    _seed_run(db, "run-m1")
    _gate_pass(db, "run-m1", "run-m1-a1", 1, _SHA_A)
    # Run moves to B; the persisted action row still pins A.
    _gate_pass(db, "run-m1", "run-m1-a2", 2, _SHA_B)
    action, _ = db.claim_owner_action(
        action_id="oact_m1", run_id="run-m1", kind="REVIEW", candidate_sha=_SHA_A
    )
    service = _StubReviewService(db)

    code = run_review_action_child(
        db=db,
        action_id=action.action_id,
        expected_candidate_sha=_SHA_B,
        run_id="run-m1",
        service_factory=lambda _db: service,
    )

    assert code == 1
    # Zero Review model launch: argv must not retarget the persisted row.
    assert service.review_calls == []
    assert db.get_reviews_for_run("run-m1") == []
    stored = db.get_owner_action(action.action_id)
    assert stored is not None
    assert stored.state is OwnerActionState.FAILED
    assert stored.reason == ACTION_CANDIDATE_MISMATCH_REASON


def test_accept_argv_mismatch_fails_closed_with_zero_mutation(tmp_path: Path) -> None:
    """Row = A, CLI expected = B, current = B -> FAILED before authority."""
    db = _db(tmp_path)
    _seed_run(db, "run-m2")
    _gate_pass(db, "run-m2", "run-m2-a1", 1, _SHA_A)
    _gate_pass(db, "run-m2", "run-m2-a2", 2, _SHA_B)
    action, _ = db.claim_owner_action(
        action_id="oact_m2", run_id="run-m2", kind="ACCEPT", candidate_sha=_SHA_A
    )
    service = _StubAcceptService(db)

    code = run_accept_action_child(
        db=db,
        action_id=action.action_id,
        expected_candidate_sha=_SHA_B,
        run_id="run-m2",
        service_factory=lambda _db: service,
    )

    assert code == 1
    assert service.accept_calls == []
    assert db.get_events_by_type("run-m2", "run_accepted") == []
    stored = db.get_owner_action(action.action_id)
    assert stored is not None
    assert stored.state is OwnerActionState.FAILED
    assert stored.reason == ACTION_CANDIDATE_MISMATCH_REASON


def test_review_run_id_mismatch_fails_closed(tmp_path: Path) -> None:
    """A CLI run id inconsistent with action.run_id fails closed."""
    db = _db(tmp_path)
    _seed_run(db, "run-m3")
    _seed_run(db, "run-m3-other")
    _gate_pass(db, "run-m3", "run-m3-a1", 1, _SHA_A)
    action, _ = db.claim_owner_action(
        action_id="oact_m3", run_id="run-m3", kind="REVIEW", candidate_sha=_SHA_A
    )
    service = _StubReviewService(db)

    code = run_review_action_child(
        db=db,
        action_id=action.action_id,
        expected_candidate_sha=_SHA_A,
        run_id="run-m3-other",
        service_factory=lambda _db: service,
    )

    assert code == 1
    assert service.review_calls == []
    stored = db.get_owner_action(action.action_id)
    assert stored is not None
    assert stored.state is OwnerActionState.FAILED
    assert stored.reason == ACTION_RUN_MISMATCH_REASON


def test_accept_run_id_mismatch_fails_closed(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _seed_run(db, "run-m4")
    _seed_run(db, "run-m4-other")
    _gate_pass(db, "run-m4", "run-m4-a1", 1, _SHA_A)
    action, _ = db.claim_owner_action(
        action_id="oact_m4", run_id="run-m4", kind="ACCEPT", candidate_sha=_SHA_A
    )
    service = _StubAcceptService(db)

    code = run_accept_action_child(
        db=db,
        action_id=action.action_id,
        expected_candidate_sha=_SHA_A,
        run_id="run-m4-other",
        service_factory=lambda _db: service,
    )

    assert code == 1
    assert service.accept_calls == []
    stored = db.get_owner_action(action.action_id)
    assert stored is not None
    assert stored.state is OwnerActionState.FAILED
    assert stored.reason == ACTION_RUN_MISMATCH_REASON


def test_matching_argv_still_fences_moved_candidate(tmp_path: Path) -> None:
    """Row = A, CLI = A, current = B -> CANDIDATE_CHANGED, zero work."""
    db = _db(tmp_path)
    _seed_run(db, "run-m5")
    _gate_pass(db, "run-m5", "run-m5-a1", 1, _SHA_A)
    action, _ = db.claim_owner_action(
        action_id="oact_m5", run_id="run-m5", kind="REVIEW", candidate_sha=_SHA_A
    )
    _gate_pass(db, "run-m5", "run-m5-a2", 2, _SHA_B)
    service = _StubReviewService(db)

    code = run_review_action_child(
        db=db,
        action_id=action.action_id,
        expected_candidate_sha=_SHA_A,
        run_id="run-m5",
        service_factory=lambda _db: service,
    )

    assert code == 1
    assert service.review_calls == []
    assert db.get_reviews_for_run("run-m5") == []
    stored = db.get_owner_action(action.action_id)
    assert stored is not None
    assert stored.state is OwnerActionState.FAILED
    assert stored.reason == CANDIDATE_CHANGED_REASON


def test_accept_matching_argv_still_fences_moved_candidate(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _seed_run(db, "run-m6")
    _gate_pass(db, "run-m6", "run-m6-a1", 1, _SHA_A)
    action, _ = db.claim_owner_action(
        action_id="oact_m6", run_id="run-m6", kind="ACCEPT", candidate_sha=_SHA_A
    )
    _gate_pass(db, "run-m6", "run-m6-a2", 2, _SHA_B)
    service = _StubAcceptService(db)

    code = run_accept_action_child(
        db=db,
        action_id=action.action_id,
        expected_candidate_sha=_SHA_A,
        run_id="run-m6",
        service_factory=lambda _db: service,
    )

    assert code == 1
    assert service.accept_calls == []
    stored = db.get_owner_action(action.action_id)
    assert stored is not None
    assert stored.state is OwnerActionState.FAILED
    assert stored.reason == CANDIDATE_CHANGED_REASON


def test_matching_row_argv_and_candidate_succeeds(tmp_path: Path) -> None:
    """The honest path (row = CLI = current) still executes authority once."""
    db = _db(tmp_path)
    _seed_run(db, "run-m7")
    _gate_pass(db, "run-m7", "run-m7-a1", 1, _SHA_A)
    action, _ = db.claim_owner_action(
        action_id="oact_m7", run_id="run-m7", kind="REVIEW", candidate_sha=_SHA_A
    )
    service = _StubReviewService(db)

    code = run_review_action_child(
        db=db,
        action_id=action.action_id,
        expected_candidate_sha=_SHA_A,
        run_id="run-m7",
        service_factory=lambda _db: service,
    )

    assert code == 0
    assert len(service.review_calls) == 1
    stored = db.get_owner_action(action.action_id)
    assert stored is not None
    assert stored.state is OwnerActionState.SUCCEEDED


# --- 2. Dead action surfaces as UNCERTAIN --------------------------------------


def _claim_dead_running(
    db: Database, *, action_id: str, run_id: str, kind: str
) -> None:
    action, created = db.claim_owner_action(
        action_id=action_id, run_id=run_id, kind=kind, candidate_sha=_SHA_A
    )
    assert created is True
    running = db.mark_owner_action_running(action.action_id, identity=_dead_identity())
    assert running is not None
    assert running.state is OwnerActionState.RUNNING


def test_dead_running_review_projects_uncertain_everywhere(tmp_path: Path) -> None:
    db_path = tmp_path / "web.db"
    db = Database(db_path)
    _seed_run(db, "run-u1")
    _claim_dead_running(db, action_id="oact_u1", run_id="run-u1", kind="REVIEW")

    stored = db.get_owner_action("oact_u1")
    assert stored is not None
    assert stored.state is OwnerActionState.RUNNING
    assert effective_action_state(stored) is OwnerActionState.UNCERTAIN

    summary = summarize_owner_actions(db, "run-u1")
    assert summary.active_review is None
    assert summary.last_review is not None
    assert summary.last_review.state is OwnerActionState.UNCERTAIN
    assert summary.last_review.reason == OWNER_PROCESS_DEAD_REASON

    # Read-only projection: storage itself is untouched by the GET path.
    assert db.get_owner_action("oact_u1") is not None
    assert db.get_owner_action("oact_u1") is not None
    assert db.get_owner_action("oact_u1").state is OwnerActionState.RUNNING  # type: ignore[union-attr]

    client = TestClient(create_app(db_path=db_path))
    actions = client.get("/runs/run-u1/actions").json()
    assert actions["owner_review_active"] is False
    assert actions["owner_review_last_state"] == "UNCERTAIN"
    assert actions["owner_review_last_reason"] == OWNER_PROCESS_DEAD_REASON

    page = client.get("/runs/run-u1")
    assert page.status_code == 200
    assert 'id="owner-action-status"' in page.text
    assert OWNER_PROCESS_DEAD_REASON in page.text


def test_dead_running_accept_projects_uncertain_everywhere(tmp_path: Path) -> None:
    db_path = tmp_path / "web.db"
    db = Database(db_path)
    _seed_run(db, "run-u2")
    _claim_dead_running(db, action_id="oact_u2", run_id="run-u2", kind="ACCEPT")

    summary = summarize_owner_actions(db, "run-u2")
    assert summary.active_accept is None
    assert summary.last_accept is not None
    assert summary.last_accept.state is OwnerActionState.UNCERTAIN
    assert summary.last_accept.reason == OWNER_PROCESS_DEAD_REASON

    assert db.get_owner_action("oact_u2").state is OwnerActionState.RUNNING  # type: ignore[union-attr]

    client = TestClient(create_app(db_path=db_path))
    actions = client.get("/runs/run-u2/actions").json()
    assert actions["owner_accept_active"] is False
    assert actions["owner_accept_last_state"] == "UNCERTAIN"
    assert actions["owner_accept_last_reason"] == OWNER_PROCESS_DEAD_REASON

    page = client.get("/runs/run-u2")
    assert page.status_code == 200
    assert 'id="owner-action-status"' in page.text
    assert OWNER_PROCESS_DEAD_REASON in page.text


def test_unknown_liveness_remains_active_and_duplicate_blocking(
    tmp_path: Path, popen: _CountingPopen
) -> None:
    db = _db(tmp_path)
    _seed_run(db, "run-u3")
    action, created = db.claim_owner_action(
        action_id="oact_u3", run_id="run-u3", kind="REVIEW", candidate_sha=_SHA_A
    )
    assert created is True
    db.attach_owner_action_process(
        action.action_id, pid=424242, identity=_unknown_identity()
    )

    stored = db.get_owner_action("oact_u3")
    assert stored is not None
    assert effective_action_state(stored) is OwnerActionState.QUEUED

    summary = summarize_owner_actions(db, "run-u3")
    assert summary.active_review is not None
    assert summary.last_review is None

    supervisor = OwnerActionSupervisor(db, popen_factory=popen)
    second = supervisor.request_review(
        run_id="run-u3", candidate_sha=_SHA_A, target_repo="/tmp/r3e21-target"
    )
    assert second.duplicate is True
    assert second.action.action_id == "oact_u3"
    assert popen.call_count == 0


# --- 3. Owner-action SSE after run completion ----------------------------------


def test_completed_run_keeps_stream_contract_for_active_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, popen: _CountingPopen
) -> None:
    """Completed run -> Review -> queued/started/completed observable in order.

    The run is already terminal; the owner-action event sequence is durable
    in chronology and the ``/actions`` projection flips from active to
    terminal through successive reads (no page reload involved).
    """
    import subprocess as _subprocess

    db_path = tmp_path / "web.db"
    db = Database(db_path)
    _seed_run(db, "run-s1")
    _gate_pass(db, "run-s1", "run-s1-a1", 1, _SHA_A)
    monkeypatch.setattr(_subprocess, "Popen", popen)
    client = TestClient(create_app(db_path=db_path))

    # No owner work yet: nothing streams for a terminal run.
    assert _owner_actions_stream_active(db, "run-s1") is False

    response = client.post("/runs/run-s1/review", follow_redirects=False)
    assert response.status_code == 303

    # The stream must now be usable for the terminal run.
    assert _owner_actions_stream_active(db, "run-s1") is True
    before = client.get("/runs/run-s1/actions").json()
    assert before["owner_review_active"] is True

    action = db.get_active_owner_action("run-s1", "REVIEW")
    assert action is not None
    service = _StubReviewService(db)
    code = run_review_action_child(
        db=db,
        action_id=action.action_id,
        expected_candidate_sha=_SHA_A,
        run_id="run-s1",
        service_factory=lambda _db: service,
    )
    assert code == 0

    queued = _event_ids(db, "run-s1", OWNER_ACTION_QUEUED_EVENT)
    started = _event_ids(db, "run-s1", OWNER_ACTION_STARTED_EVENT)
    completed = _event_ids(db, "run-s1", OWNER_ACTION_COMPLETED_EVENT)
    assert len(queued) == 1
    assert len(started) == 1
    assert len(completed) == 1
    assert queued[0] < started[0] < completed[0]

    after = client.get("/runs/run-s1/actions").json()
    assert after["owner_review_active"] is False
    assert _owner_actions_stream_active(db, "run-s1") is False


def test_completed_run_failure_sequence_is_observable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, popen: _CountingPopen
) -> None:
    import subprocess as _subprocess

    db_path = tmp_path / "web.db"
    db = Database(db_path)
    _seed_run(db, "run-s2")
    _gate_pass(db, "run-s2", "run-s2-a1", 1, _SHA_A)
    monkeypatch.setattr(_subprocess, "Popen", popen)
    client = TestClient(create_app(db_path=db_path))
    assert client.post("/runs/run-s2/review", follow_redirects=False).status_code == 303
    assert _owner_actions_stream_active(db, "run-s2") is True

    action = db.get_active_owner_action("run-s2", "REVIEW")
    assert action is not None
    service = _ExplodingReviewService(db, "sse-boom")
    code = run_review_action_child(
        db=db,
        action_id=action.action_id,
        expected_candidate_sha=_SHA_A,
        run_id="run-s2",
        service_factory=lambda _db: service,
    )
    assert code == 1

    queued = _event_ids(db, "run-s2", OWNER_ACTION_QUEUED_EVENT)
    started = _event_ids(db, "run-s2", OWNER_ACTION_STARTED_EVENT)
    failed = _event_ids(db, "run-s2", OWNER_ACTION_FAILED_EVENT)
    assert len(queued) == 1
    assert len(started) == 1
    assert len(failed) == 1
    assert queued[0] < started[0] < failed[0]

    after = client.get("/runs/run-s2/actions").json()
    assert after["owner_review_active"] is False
    assert after["owner_review_last_state"] == "FAILED"
    assert _owner_actions_stream_active(db, "run-s2") is False


def test_run_page_subscribes_to_owner_action_events_without_polling() -> None:
    """The existing EventSource (no second loop) handles owner_action_*."""
    app_js = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "saberops"
        / "static"
        / "js"
        / "app.js"
    ).read_text()
    for event_type in (
        "owner_action_queued",
        "owner_action_started",
        "owner_action_completed",
        "owner_action_failed",
        "owner_action_uncertain",
    ):
        assert event_type in app_js
    # Owner-action events drive an authoritative /actions refresh.
    assert app_js.count("/runs/") >= 2
    assert "refreshActionState" in app_js
    # No periodic polling loop was added: the only timer is the elapsed clock.
    assert app_js.count("setInterval") == 1


# --- 4. Spawn event/metadata race ----------------------------------------------


class _FastChildPopen:
    """Deterministic fast child: transitions before post-Popen bookkeeping.

    On spawn, synchronously move the just-claimed QUEUED action to RUNNING
    (emitting STARTED) like a fast detached child would, then return a
    stub proc handle.  The parent's post-Popen attach must neither lose
    the transcript nor regress the row.
    """

    def __init__(self, db: Database, *, to_terminal: bool = False) -> None:
        self.db = db
        self.to_terminal = to_terminal
        self.calls = 0

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        actions = self.db.list_owner_actions("run-q1")
        assert len(actions) == 1
        pending = actions[0]
        # The QUEUED event must already be durable at this point.
        assert _event_ids(self.db, "run-q1", OWNER_ACTION_QUEUED_EVENT) != []
        live = ProcessIdentity.for_pid(__import__("os").getpid())
        assert live is not None
        self.db.mark_owner_action_running(pending.action_id, identity=live)
        self.db.record_event(
            pending.run_id,
            OWNER_ACTION_STARTED_EVENT,
            payload={"action_id": pending.action_id, "kind": pending.kind.value},
        )
        if self.to_terminal:
            self.db.mark_owner_action_terminal(
                pending.action_id,
                state=OwnerActionState.SUCCEEDED.value,
                result_summary="main:fastchild",
            )
            self.db.record_event(
                pending.run_id,
                OWNER_ACTION_COMPLETED_EVENT,
                payload={"action_id": pending.action_id, "kind": pending.kind.value},
            )
        return SimpleNamespace(pid=424243)


def test_fast_child_cannot_reorder_queued_behind_started(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _seed_run(db, "run-q1")
    supervisor = OwnerActionSupervisor(db, popen_factory=_FastChildPopen(db))

    launch = supervisor.request_review(
        run_id="run-q1", candidate_sha=_SHA_A, target_repo=str(tmp_path)
    )

    assert launch.launched is True
    queued = _event_ids(db, "run-q1", OWNER_ACTION_QUEUED_EVENT)
    started = _event_ids(db, "run-q1", OWNER_ACTION_STARTED_EVENT)
    assert len(queued) == 1
    assert len(started) == 1
    assert queued[0] < started[0]
    # The row advanced to RUNNING (child won the race); the parent did not
    # regress it, and the transcript destination survived.
    stored = db.get_owner_action(launch.action.action_id)
    assert stored is not None
    assert stored.state is OwnerActionState.RUNNING
    assert stored.transcript_path is not None
    assert Path(stored.transcript_path).exists()


def test_fast_terminal_child_is_never_regressed_by_parent(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _seed_run(db, "run-q1")
    supervisor = OwnerActionSupervisor(
        db, popen_factory=_FastChildPopen(db, to_terminal=True)
    )

    launch = supervisor.request_accept(
        run_id="run-q1", candidate_sha=_SHA_A, target_repo=str(tmp_path)
    )

    assert launch.launched is True
    stored = db.get_owner_action(launch.action.action_id)
    assert stored is not None
    # Terminal receipt wins: parent bookkeeping left it untouched.
    assert stored.state is OwnerActionState.SUCCEEDED
    assert stored.result_summary == "main:fastchild"
    assert stored.transcript_path is not None
    assert Path(stored.transcript_path).exists()
    queued = _event_ids(db, "run-q1", OWNER_ACTION_QUEUED_EVENT)
    started = _event_ids(db, "run-q1", OWNER_ACTION_STARTED_EVENT)
    completed = _event_ids(db, "run-q1", OWNER_ACTION_COMPLETED_EVENT)
    assert queued[0] < started[0] < completed[0]


def test_popen_failure_still_orders_queued_before_failed(tmp_path: Path) -> None:
    from saberops.owner_actions import OWNER_ACTION_FAILED_EVENT

    class _FailingPopen:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, *args: Any, **kwargs: Any) -> Any:
            self.calls += 1
            raise OSError("fake spawn failure")

    db = _db(tmp_path)
    _seed_run(db, "run-q2")
    supervisor = OwnerActionSupervisor(db, popen_factory=_FailingPopen())

    launch = supervisor.request_review(
        run_id="run-q2", candidate_sha=_SHA_A, target_repo=str(tmp_path)
    )

    assert launch.launched is False
    assert launch.action.state is OwnerActionState.FAILED
    assert launch.action.reason == LAUNCH_FAILED_REASON
    queued = _event_ids(db, "run-q2", OWNER_ACTION_QUEUED_EVENT)
    failed = _event_ids(db, "run-q2", OWNER_ACTION_FAILED_EVENT)
    assert len(queued) == 1
    assert len(failed) == 1
    assert queued[0] < failed[0]


def test_transcript_persisted_before_child_exists(tmp_path: Path) -> None:
    """Spawn preparation persists the transcript path even if Popen fails."""
    from saberops.owner_actions import owner_action_transcript_path

    class _FailingPopen:
        def __call__(self, *args: Any, **kwargs: Any) -> Any:
            raise OSError("nope")

    db = _db(tmp_path)
    _seed_run(db, "run-q3")
    supervisor = OwnerActionSupervisor(db, popen_factory=_FailingPopen())
    launch = supervisor.request_review(
        run_id="run-q3", candidate_sha=_SHA_A, target_repo=str(tmp_path)
    )
    expected = owner_action_transcript_path(db.db_path, launch.action.action_id)
    assert Path(expected).exists()
    stored = db.get_owner_action(launch.action.action_id)
    assert stored is not None
    assert stored.transcript_path == str(expected)


# --- 5. Fail-closed transcript persistence (R3-E2.2) ---------------------------


def test_transcript_persistence_failure_launches_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """set_owner_action_transcript() fails, rest of DB usable -> zero launch.

    Fail-closed contract: no Popen, not reported as launched, FAILED
    settlement with the bounded launch-failure reason, zero Review
    service work / zero Accept mutation, and no QUEUED / STARTED /
    COMPLETED event -- only the FAILED receipt.
    """
    import sqlite3

    from saberops.owner_actions import OWNER_ACTION_QUEUED_EVENT

    def _boom(self: Database, action_id: str, *, transcript_path: str) -> Any:
        raise sqlite3.OperationalError("fake transcript write failure")

    monkeypatch.setattr(Database, "set_owner_action_transcript", _boom)

    popen_calls: list[Any] = []

    class _NeverPopen:
        def __call__(self, *args: Any, **kwargs: Any) -> Any:
            popen_calls.append((args, kwargs))
            raise AssertionError(
                "Popen must not be called when transcript persistence fails"
            )

    db = _db(tmp_path)
    _seed_run(db, "run-tf1")
    _gate_pass(db, "run-tf1", "run-tf1-a1", 1, _SHA_A)
    supervisor = OwnerActionSupervisor(db, popen_factory=_NeverPopen())

    review_launch = supervisor.request_review(
        run_id="run-tf1", candidate_sha=_SHA_A, target_repo=str(tmp_path)
    )
    accept_launch = supervisor.request_accept(
        run_id="run-tf1", candidate_sha=_SHA_A, target_repo=str(tmp_path)
    )

    assert popen_calls == []
    for launch in (review_launch, accept_launch):
        assert launch.launched is False
        assert launch.duplicate is False
        assert launch.action.state is OwnerActionState.FAILED
        assert launch.action.reason == LAUNCH_FAILED_REASON
        stored = db.get_owner_action(launch.action.action_id)
        assert stored is not None
        assert stored.state is OwnerActionState.FAILED
        assert stored.reason == LAUNCH_FAILED_REASON
        # The unverified destination is never recorded on the row.
        assert stored.transcript_path is None

    # Zero authority work: no Review service outcome, no Accept mutation.
    assert db.get_reviews_for_run("run-tf1") == []
    assert db.get_events_by_type("run-tf1", "run_accepted") == []
    # No launch-sequence event; exactly one FAILED receipt per action.
    assert _event_ids(db, "run-tf1", OWNER_ACTION_QUEUED_EVENT) == []
    assert _event_ids(db, "run-tf1", OWNER_ACTION_STARTED_EVENT) == []
    assert _event_ids(db, "run-tf1", OWNER_ACTION_COMPLETED_EVENT) == []
    assert len(_event_ids(db, "run-tf1", OWNER_ACTION_FAILED_EVENT)) == 2


def test_launcher_preserves_detached_argv_contract(
    tmp_path: Path, popen: _CountingPopen
) -> None:
    import os
    import sys

    db = _db(tmp_path)
    repo = str(tmp_path / "repo")
    Path(repo).mkdir()
    _seed_run(db, "run-q4", repo=repo)
    supervisor = OwnerActionSupervisor(db, popen_factory=popen)
    launch = supervisor.request_review(
        run_id="run-q4", candidate_sha=_SHA_A, target_repo=repo
    )
    assert launch.launched is True
    assert popen.call_count == 1
    call = popen.calls[0]
    argv = call["args"][0]
    kwargs = call["kwargs"]
    assert isinstance(argv, list)
    assert argv[:3] == [sys.executable, "-m", "saberops.cli"]
    assert kwargs["start_new_session"] is True
    assert kwargs["close_fds"] is True
    assert kwargs["stdin"] is not None
    assert kwargs["cwd"] == repo
    assert "shell" not in kwargs
    assert kwargs["stderr"] is subprocess.STDOUT
    assert "PYTHONPATH" in kwargs["env"]
    assert kwargs["env"]["PYTHONPATH"].split(os.pathsep)[0].endswith("src")
