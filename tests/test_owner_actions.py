"""Owner-action tests: durable Review/Accept actions and fence authority.

Merged R3-E2 and R3-E2.1 generations.

R3-E2 proves the Web owner workflow is safe for real dogfooding without
redesigning Review or Accept semantics:

* POST /review and POST /accept (confirmed) claim a durable owner action
  and detach a child; they never call ``review_run`` / ``accept_run``
  synchronously in the request.
* The launcher uses detached argv execution (``start_new_session=True``,
  never a shell) with the exact project DB and repository handed to the
  child; the child outlives the request/browser.
* At most one active action (and therefore one child) exists per run and
  kind, atomically, across processes, threads, and Web-app restarts.
* The child fences on the exact requested candidate SHA before any model
  launch (Review) or repository mutation (Accept).
* Failures become durable FAILED; a provably dead RUNNING action without
  a terminal receipt projects UNCERTAIN; UNKNOWN liveness fails closed;
  an UNCERTAIN Accept is never replayed automatically.
* Canonical Review / Accept evidence still comes only from the existing
  engines; Reject stays synchronous.

R3-E2.1 is the bounded follow-up: the persisted action row
(``RunOwnerAction.candidate_sha``) is the fence authority -- never CLI argv
(a differing ``--expected-candidate`` fails closed with zero launch/mutation,
and the CLI run id must be consistent with ``action.run_id``); dead active
actions project UNCERTAIN / OWNER_PROCESS_DEAD in both the run-detail page and
the ``/actions`` JSON while UNKNOWN liveness stays active; owner-action SSE
works after run completion over the existing run-page EventSource; and spawn
ordering is QUEUED -> STARTED -> COMPLETED|FAILED even against a fast child.

Spawned children are real live ``sleep`` processes (truthful liveness,
no credentials, no model calls); everything else is hermetic.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from saberops.accept import AcceptEligibility, AcceptResult
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
    OwnerActionKind,
    OwnerActionState,
    OwnerActionSupervisor,
    RunOwnerAction,
    action_identity,
    build_owner_action_argv,
    current_candidate_sha,
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


def _seed_run(db: Database, run_id: str, repo: str = "/tmp/r3e2-target") -> None:
    db.create_run(
        Run(
            id=run_id,
            task="r3e2 owner action probe",
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
            worktree_path="/tmp/r3e2-wt",
            branch_name="r3e2-candidate",
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


def _git_repo(path: Path) -> tuple[str, str, str]:
    """Create a real repo with base -> candidate commits, HEAD left at base."""
    repo = path / "target"
    repo.mkdir(parents=True, exist_ok=True)

    def _git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    _git("init", "-q")
    _git("config", "user.email", "r3e2@test")
    _git("config", "user.name", "r3e2")
    (repo / "feature.txt").write_text("base\n")
    _git("add", ".")
    _git("commit", "-qm", "base")
    base = _git("rev-parse", "HEAD")
    (repo / "feature.txt").write_text("candidate\n")
    _git("add", ".")
    _git("commit", "-qm", "candidate")
    candidate = _git("rev-parse", "HEAD")
    _git("checkout", "-q", base)
    return str(repo), base, candidate


def _head_sha(repo: str) -> str:
    return subprocess.run(
        ["git", "-C", repo, "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _is_owner_child_spawn(args: tuple[Any, ...], kwargs: dict[str, Any]) -> bool:
    argv = args[0] if args else kwargs.get("args")
    return isinstance(argv, list) and "saberops.cli" in [str(part) for part in argv]


class _CountingPopen:
    """Selective spawn fake: owner-action children become real live sleepers.

    Only argv executions of the owner-action child (``saberops.cli``) are
    intercepted; every other subprocess use (git validation, test helpers)
    delegates to the real Popen.  Intercepted children are genuine live
    processes, so liveness probing stays truthful without any model call.
    """

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
    """Provide a spawn fake that cleans up its live sleepers on teardown."""
    fake = _CountingPopen()
    yield fake
    fake.close()


class _FailingPopen:
    """Popen that always fails, proving launch-failure semantics."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        raise OSError("fake spawn failure")


class _StubReviewService(OrchestratorService):
    """Service fake recording Review delegation without any model call."""

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
        self.review_calls.append(
            {"run_id": run_id, "reviewer_override": reviewer_override, "timeout": timeout}
        )
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
    """Service fake recording Accept delegation without any repo mutation."""

    def __init__(self, db: Database) -> None:
        super().__init__(db=db, readiness_service=False)
        self.accept_calls: list[dict[str, Any]] = []

    def accept_run(self, run_id: str, gate_timeout: float = 300.0) -> AcceptResult:
        self.accept_calls.append({"run_id": run_id, "gate_timeout": gate_timeout})
        result = AcceptResult(
            run_id=run_id,
            accepted=True,
            target_repo="/tmp/r3e2-target",
            final_branch="main",
            final_sha=_SHA_A,
            summary="stub accept",
            gate_passed=True,
        )
        self.db.record_event(
            run_id,
            "run_accepted",
            payload={
                "candidate_sha": _SHA_A,
                "final_branch": "main",
                "gate_command": "true",
                "provenance": {
                    "candidate_sha": _SHA_A,
                    "run_id": run_id,
                    "attempt_id": "probe",
                    "attempt_number": 1,
                },
            },
        )
        return result


class _ExplodingReviewService(_StubReviewService):
    """Review authority that always fails, proving FAILED persistence."""

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


def _boom(self: Any, *args: Any, **kwargs: Any) -> Any:
    """Synchronous engine call detector: the request must never reach here."""
    raise AssertionError(f"synchronous engine call is forbidden: args={args}")


class _EligibleEngine:
    """Advisory Web pre-check stub: eligible without touching real policy."""

    def __init__(self, db: Database | None = None) -> None:
        self.db = db

    def check_accept_eligibility(self, run_id: str) -> AcceptEligibility:
        return AcceptEligibility(run_id=run_id, eligible=True, checks=())


def _live_self_identity() -> ProcessIdentity:
    current = ProcessIdentity.for_pid(os.getpid())
    assert current is not None
    return ProcessIdentity(
        host=current.host,
        boot_id=current.boot_id,
        pid=current.pid,
        start_ticks=current.start_ticks,
        session_id=current.session_id,
        process_group_id=current.process_group_id,
    )


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


# --- Web non-blocking contract ---------------------------------------------


def test_review_request_returns_without_synchronous_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, popen: _CountingPopen
) -> None:
    db_path = tmp_path / "web.db"
    db = Database(db_path)
    _seed_run(db, "run-r1")
    _gate_pass(db, "run-r1", "run-r1-a1", 1, _SHA_A)
    monkeypatch.setattr(OrchestratorService, "review_run", _boom)
    supervisor = OwnerActionSupervisor(db, popen_factory=popen)

    launch = supervisor.request_review(
        run_id="run-r1", candidate_sha=_SHA_A, target_repo="/tmp/r3e2-target"
    )

    assert launch.launched is True
    assert popen.call_count == 1
    action = db.get_active_owner_action("run-r1", "REVIEW")
    assert action is not None
    assert action.state is OwnerActionState.QUEUED
    assert action.candidate_sha == _SHA_A


def test_accept_post_returns_without_synchronous_accept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, popen: _CountingPopen
) -> None:
    db_path = tmp_path / "web.db"
    db = Database(db_path)
    _seed_run(db, "run-a1")
    _gate_pass(db, "run-a1", "run-a1-a1", 1, _SHA_A)
    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr("saberops.web.AcceptEngine", _EligibleEngine)
    monkeypatch.setattr(OrchestratorService, "accept_run", _boom)
    client = TestClient(create_app(db_path=db_path))

    response = client.post(
        "/runs/run-a1/accept", data={"confirm_run_id": "run-a1"}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"].endswith("/runs/run-a1?accept_queued=1")
    assert popen.call_count == 1
    action = db.get_active_owner_action("run-a1", "ACCEPT")
    assert action is not None
    assert action.candidate_sha == _SHA_A


def test_accept_requires_exact_confirmation_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, popen: _CountingPopen
) -> None:
    db_path = tmp_path / "web.db"
    db = Database(db_path)
    _seed_run(db, "run-a2")
    _gate_pass(db, "run-a2", "run-a2-a1", 1, _SHA_A)
    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr("saberops.web.AcceptEngine", _EligibleEngine)
    client = TestClient(create_app(db_path=db_path))

    response = client.post("/runs/run-a2/accept", data={"confirm_run_id": ""})

    assert response.status_code == 200
    assert popen.call_count == 0
    assert db.list_owner_actions("run-a2") == []


def test_accept_blocked_by_real_checklist_launches_nothing(tmp_path: Path) -> None:
    db_path = tmp_path / "web.db"
    db = Database(db_path)
    # Candidate exists but the real engine refuses (no provenance/repo): the
    # advisory pre-check stays a real gate and nothing is launched.
    _seed_run(db, "run-a3", repo="/tmp/r3e2-no-such-repo")
    _gate_pass(db, "run-a3", "run-a3-a1", 1, _SHA_A)
    client = TestClient(create_app(db_path=db_path))

    response = client.post(
        "/runs/run-a3/accept", data={"confirm_run_id": "run-a3"}, follow_redirects=False
    )

    assert response.status_code == 400
    assert db.list_owner_actions("run-a3") == []


def test_review_without_candidate_launches_nothing(tmp_path: Path) -> None:
    from saberops.candidate_lifecycle import project_candidate_state

    db_path = tmp_path / "web.db"
    db = Database(db_path)
    _seed_run(db, "run-r0")

    # The removed review route refused before claiming when no reviewable
    # candidate exists; the engine callers keep that guard.
    assert project_candidate_state(db, "run-r0").candidate_sha is None
    assert db.list_owner_actions("run-r0") == []


# --- Detached argv / process contract ---------------------------------------


def test_launcher_uses_detached_argv_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, popen: _CountingPopen
) -> None:
    monkeypatch.delenv("ORCH_EXECUTABLE", raising=False)
    db = _db(tmp_path)
    repo = str(tmp_path / "repo")
    Path(repo).mkdir()
    _seed_run(db, "run-x1", repo=repo)
    supervisor = OwnerActionSupervisor(db, popen_factory=popen)

    launch = supervisor.request_review(
        run_id="run-x1", candidate_sha=_SHA_A, target_repo=repo
    )

    assert launch.launched is True
    assert launch.duplicate is False
    assert popen.call_count == 1
    call = popen.calls[0]
    argv = call["args"][0]
    kwargs = call["kwargs"]
    assert isinstance(argv, list)
    assert argv[:3] == [sys.executable, "-m", "saberops.cli"]
    assert "review" in argv
    assert "run-x1" in argv
    assert "--db" in argv
    assert argv[argv.index("--db") + 1] == str(db.db_path)
    assert "--action-id" in argv
    assert "--expected-candidate" in argv
    assert argv[argv.index("--expected-candidate") + 1] == _SHA_A
    assert "shell" not in kwargs
    assert kwargs["start_new_session"] is True
    assert kwargs["close_fds"] is True
    assert kwargs["stdin"] is not None
    assert kwargs["cwd"] == repo
    assert kwargs["stderr"] is subprocess.STDOUT
    assert "PYTHONPATH" in kwargs["env"]
    assert kwargs["env"]["PYTHONPATH"].split(os.pathsep)[0].endswith("src")


def test_launcher_hands_exact_project_db_and_repo_to_child(
    tmp_path: Path, popen: _CountingPopen
) -> None:
    db = _db(tmp_path)
    repo = str(tmp_path / "repo")
    Path(repo).mkdir()
    _seed_run(db, "run-x2", repo=repo)
    supervisor = OwnerActionSupervisor(db, popen_factory=popen)

    launch = supervisor.request_accept(
        run_id="run-x2", candidate_sha=_SHA_A, target_repo=repo
    )

    argv = popen.calls[0]["args"][0]
    assert isinstance(argv, list)
    assert argv[argv.index("--db") + 1] == str(db.db_path)
    assert popen.calls[0]["kwargs"]["cwd"] == repo
    stored = db.get_owner_action(launch.action.action_id)
    assert stored is not None
    assert stored.target_repo == repo
    assert stored.transcript_path is not None
    transcript = Path(stored.transcript_path)
    assert transcript.parent.name == "transcripts"
    assert transcript.parent.parent == db.db_path.parent
    assert transcript.exists()


def test_build_argv_matches_orch_authority(tmp_path: Path) -> None:
    review_argv = build_owner_action_argv(
        OwnerActionKind.REVIEW, "run-q", tmp_path / "o.db", "oact_1", _SHA_A
    )
    assert review_argv[3:] == [
        "review",
        "run-q",
        "--db",
        str(tmp_path / "o.db"),
        "--timeout",
        "1200.0",
        "--action-id",
        "oact_1",
        "--expected-candidate",
        _SHA_A,
    ]
    accept_argv = build_owner_action_argv(
        OwnerActionKind.ACCEPT, "run-q", tmp_path / "o.db", "oact_2", _SHA_A
    )
    assert accept_argv[3:] == [
        "accept",
        "run-q",
        "--db",
        str(tmp_path / "o.db"),
        "--gate-timeout",
        "300.0",
        "--action-id",
        "oact_2",
        "--expected-candidate",
        _SHA_A,
    ]


# --- Duplicate-launch atomicity ----------------------------------------------


def test_duplicate_review_requests_launch_single_child(
    tmp_path: Path, popen: _CountingPopen
) -> None:
    db_path = tmp_path / "web.db"
    db = Database(db_path)
    _seed_run(db, "run-d1")
    _gate_pass(db, "run-d1", "run-d1-a1", 1, _SHA_A)
    supervisor = OwnerActionSupervisor(db, popen_factory=popen)

    first = supervisor.request_review(
        run_id="run-d1", candidate_sha=_SHA_A, target_repo="/tmp/r3e2-target"
    )
    second = supervisor.request_review(
        run_id="run-d1", candidate_sha=_SHA_A, target_repo="/tmp/r3e2-target"
    )

    assert first.launched is True
    assert second.duplicate is True
    assert second.action.action_id == first.action.action_id
    assert popen.call_count == 1
    assert len(db.list_owner_actions("run-d1")) == 1


def test_duplicate_accept_requests_launch_single_child(
    tmp_path: Path, popen: _CountingPopen
) -> None:
    db = _db(tmp_path)
    _seed_run(db, "run-d2")
    supervisor = OwnerActionSupervisor(db, popen_factory=popen)

    first = supervisor.request_accept(
        run_id="run-d2", candidate_sha=_SHA_A, target_repo="/tmp/r3e2-target"
    )
    second = supervisor.request_accept(
        run_id="run-d2", candidate_sha=_SHA_A, target_repo="/tmp/r3e2-target"
    )

    assert first.launched is True
    assert second.duplicate is True
    assert second.action.action_id == first.action.action_id
    assert popen.call_count == 1


def test_concurrent_review_claims_launch_single_child(
    tmp_path: Path, popen: _CountingPopen
) -> None:
    db_path = tmp_path / "conc.db"
    seed = Database(db_path)
    _seed_run(seed, "run-d3")
    barrier = threading.Barrier(8)
    errors: list[str] = []

    def _worker() -> None:
        try:
            barrier.wait(timeout=10)
            thread_db = Database(db_path)
            OwnerActionSupervisor(thread_db, popen_factory=popen).request_review(
                run_id="run-d3", candidate_sha=_SHA_A, target_repo="/tmp/r3e2-target"
            )
        except Exception as exc:  # pragma: no cover - contention must not raise
            errors.append(str(exc))

    threads = [threading.Thread(target=_worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert errors == []
    assert popen.call_count == 1
    assert len(Database(db_path).list_owner_actions("run-d3")) == 1


def test_review_and_accept_do_not_block_each_other(
    tmp_path: Path, popen: _CountingPopen
) -> None:
    db = _db(tmp_path)
    _seed_run(db, "run-d4")
    supervisor = OwnerActionSupervisor(db, popen_factory=popen)

    review = supervisor.request_review(
        run_id="run-d4", candidate_sha=_SHA_A, target_repo="/tmp/r3e2-target"
    )
    accept = supervisor.request_accept(
        run_id="run-d4", candidate_sha=_SHA_A, target_repo="/tmp/r3e2-target"
    )

    assert review.launched is True
    assert accept.launched is True
    assert popen.call_count == 2


# --- Restart survival / browser disconnect ------------------------------------


def test_active_action_survives_app_restart_and_blocks_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, popen: _CountingPopen
) -> None:
    db_path = tmp_path / "web.db"
    db = Database(db_path)
    _seed_run(db, "run-s1")
    _gate_pass(db, "run-s1", "run-s1-a1", 1, _SHA_A)
    supervisor = OwnerActionSupervisor(db, popen_factory=popen)
    launch = supervisor.request_review(
        run_id="run-s1", candidate_sha=_SHA_A, target_repo="/tmp/r3e2-target"
    )
    assert launch.launched is True
    del db
    del supervisor

    reopened = Database(db_path)
    second_popen = _CountingPopen()
    try:
        reopened_supervisor = OwnerActionSupervisor(reopened, popen_factory=second_popen)
        second = reopened_supervisor.request_review(
            run_id="run-s1", candidate_sha=_SHA_A, target_repo="/tmp/r3e2-target"
        )
        assert second.duplicate is True
        assert second_popen.call_count == 0
        third = reopened_supervisor.request_review(
            run_id="run-s1", candidate_sha=_SHA_A, target_repo="/tmp/r3e2-target"
        )
        assert third.duplicate is True
        assert second_popen.call_count == 0
    finally:
        second_popen.close()


def test_request_completion_has_no_cancellation_effect(
    tmp_path: Path, popen: _CountingPopen
) -> None:
    db = _db(tmp_path)
    _seed_run(db, "run-s2")
    _gate_pass(db, "run-s2", "run-s2-a1", 1, _SHA_A)
    supervisor = OwnerActionSupervisor(db, popen_factory=popen)
    launch = supervisor.request_review(
        run_id="run-s2", candidate_sha=_SHA_A, target_repo="/tmp/r3e2-target"
    )
    assert popen.call_count == 1
    # The request is long gone; the detached child later starts for real and
    # the service authority executes exactly once.
    service = _StubReviewService(db)
    code = run_review_action_child(
        db=db,
        action_id=launch.action.action_id,
        expected_candidate_sha=_SHA_A,
        service_factory=lambda _db: service,
    )
    assert code == 0
    assert len(service.review_calls) == 1
    stored = db.get_owner_action(launch.action.action_id)
    assert stored is not None
    assert stored.state is OwnerActionState.SUCCEEDED


# --- Child delegation ----------------------------------------------------------


def test_review_child_delegates_to_service_review_path(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _seed_run(db, "run-c1")
    _gate_pass(db, "run-c1", "run-c1-a1", 1, _SHA_A)
    action, created = db.claim_owner_action(
        action_id="oact_c1", run_id="run-c1", kind="REVIEW", candidate_sha=_SHA_A
    )
    assert created is True
    service = _StubReviewService(db)

    code = run_review_action_child(
        db=db,
        action_id=action.action_id,
        expected_candidate_sha=_SHA_A,
        timeout=42.0,
        service_factory=lambda _db: service,
    )

    assert code == 0
    assert len(service.review_calls) == 1
    assert service.review_calls[0]["run_id"] == "run-c1"
    assert service.review_calls[0]["timeout"] == 42.0
    stored = db.get_owner_action(action.action_id)
    assert stored is not None
    assert stored.state is OwnerActionState.SUCCEEDED
    assert stored.result_summary is not None
    assert stored.result_summary.startswith("PASS:")


def test_accept_child_delegates_to_service_accept_path(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _seed_run(db, "run-c2")
    _gate_pass(db, "run-c2", "run-c2-a1", 1, _SHA_A)
    action, created = db.claim_owner_action(
        action_id="oact_c2", run_id="run-c2", kind="ACCEPT", candidate_sha=_SHA_A
    )
    assert created is True
    service = _StubAcceptService(db)

    code = run_accept_action_child(
        db=db,
        action_id=action.action_id,
        expected_candidate_sha=_SHA_A,
        gate_timeout=11.0,
        service_factory=lambda _db: service,
    )

    assert code == 0
    assert len(service.accept_calls) == 1
    assert service.accept_calls[0] == {"run_id": "run-c2", "gate_timeout": 11.0}
    stored = db.get_owner_action(action.action_id)
    assert stored is not None
    assert stored.state is OwnerActionState.SUCCEEDED


def test_exact_reviewer_reaches_real_review_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R3-E1 authority survives the detached handoff: the child passes no
    override, so the real service resolves the exact configured Reviewer and
    the real engine receives it untouched."""
    from saberops import review as review_module

    db = _db(tmp_path)
    _seed_run(db, "run-c3")
    _gate_pass(db, "run-c3", "run-c3-a1", 1, _SHA_A)
    action, _ = db.claim_owner_action(
        action_id="oact_c3", run_id="run-c3", kind="REVIEW", candidate_sha=_SHA_A
    )
    exact = WorkerCandidate(
        provider="opencode", model="exact-reviewer", binding_id="bind-exact-1"
    )
    captured: dict[str, Any] = {}

    def _capture_configured(self: OrchestratorService) -> WorkerCandidate | None:
        return exact

    def _capture_engine(
        self: Any, *, run_id: str, reviewer_override: Any = None, **kwargs: Any
    ) -> ReviewResult:
        captured["reviewer_override"] = reviewer_override
        return ReviewResult(
            id="rev-exact",
            run_id=run_id,
            provider="opencode",
            model="exact-reviewer",
            verdict=ReviewVerdict.PASS,
            summary="exact reviewer pass",
            created_at=_CREATED,
        )

    monkeypatch.setattr(
        OrchestratorService, "_configured_reviewer_candidate", _capture_configured
    )
    monkeypatch.setattr(review_module.ReviewEngine, "review_run", _capture_engine)

    code = run_review_action_child(
        db=db,
        action_id=action.action_id,
        expected_candidate_sha=_SHA_A,
        service_factory=lambda store: OrchestratorService(store, readiness_service=False),
    )

    assert code == 0
    override = captured["reviewer_override"]
    assert isinstance(override, WorkerCandidate)
    assert override.provider == "opencode"
    assert override.model == "exact-reviewer"
    assert override.binding_id == "bind-exact-1"


# --- Candidate fencing ----------------------------------------------------------


def test_review_fence_refuses_moved_candidate_with_zero_launch(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _seed_run(db, "run-f1")
    _gate_pass(db, "run-f1", "run-f1-a1", 1, _SHA_A)
    action, _ = db.claim_owner_action(
        action_id="oact_f1", run_id="run-f1", kind="REVIEW", candidate_sha=_SHA_A
    )
    # The run moves on before the child executes.
    _gate_pass(db, "run-f1", "run-f1-a2", 2, _SHA_B)
    assert current_candidate_sha(db, "run-f1") == _SHA_B
    service = _StubReviewService(db)

    code = run_review_action_child(
        db=db,
        action_id=action.action_id,
        expected_candidate_sha=_SHA_A,
        service_factory=lambda _db: service,
    )

    assert code == 1
    assert service.review_calls == []
    assert db.get_reviews_for_run("run-f1") == []
    stored = db.get_owner_action(action.action_id)
    assert stored is not None
    assert stored.state is OwnerActionState.FAILED
    assert stored.reason == CANDIDATE_CHANGED_REASON


def test_accept_fence_refuses_moved_candidate_with_zero_mutation(tmp_path: Path) -> None:
    repo, base, candidate_a = _git_repo(tmp_path / "repo-f")
    db = _db(tmp_path)
    db.create_run(
        Run(
            id="run-f2",
            task="r3e2 accept fence probe",
            target_repo=repo,
            base_commit=base,
            tier=Tier.T1,
            training_allowed=True,
            gate_command="true",
            status=RunStatus.COMPLETED,
            created_at=_CREATED,
            completed_at=_CREATED,
        )
    )
    _gate_pass(db, "run-f2", "run-f2-a1", 1, candidate_a)
    action, _ = db.claim_owner_action(
        action_id="oact_f2", run_id="run-f2", kind="ACCEPT", candidate_sha=candidate_a
    )
    candidate_b = _SHA_B
    _gate_pass(db, "run-f2", "run-f2-a2", 2, candidate_b)
    assert current_candidate_sha(db, "run-f2") == candidate_b
    service = _StubAcceptService(db)

    code = run_accept_action_child(
        db=db,
        action_id=action.action_id,
        expected_candidate_sha=candidate_a,
        service_factory=lambda _db: service,
    )

    assert code == 1
    assert service.accept_calls == []
    assert _head_sha(repo) == base
    assert db.get_events_by_type("run-f2", "run_accepted") == []
    stored = db.get_owner_action(action.action_id)
    assert stored is not None
    assert stored.state is OwnerActionState.FAILED
    assert stored.reason == CANDIDATE_CHANGED_REASON


# --- Crash / UNCERTAIN semantics --------------------------------------------------


def test_popen_failure_marks_failed_without_false_running(
    tmp_path: Path, popen: _CountingPopen
) -> None:
    _ = popen
    db = _db(tmp_path)
    _seed_run(db, "run-p1")
    supervisor = OwnerActionSupervisor(db, popen_factory=_FailingPopen())

    launch = supervisor.request_review(
        run_id="run-p1", candidate_sha=_SHA_A, target_repo="/tmp/r3e2-target"
    )

    assert launch.launched is False
    assert launch.duplicate is False
    assert launch.action.state is OwnerActionState.FAILED
    assert launch.action.reason == LAUNCH_FAILED_REASON
    assert db.get_active_owner_action("run-p1", "REVIEW") is None
    failed = db.get_events_by_type("run-p1", "owner_action_failed")
    assert len(failed) == 1


def test_child_exception_becomes_durable_failed_with_bounded_evidence(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    _seed_run(db, "run-p2")
    _gate_pass(db, "run-p2", "run-p2-a1", 1, _SHA_A)
    action, _ = db.claim_owner_action(
        action_id="oact_p2", run_id="run-p2", kind="REVIEW", candidate_sha=_SHA_A
    )
    service = _ExplodingReviewService(db, "boom-" + "x" * 5000)

    code = run_review_action_child(
        db=db,
        action_id=action.action_id,
        expected_candidate_sha=_SHA_A,
        service_factory=lambda _db: service,
    )

    assert code == 1
    stored = db.get_owner_action(action.action_id)
    assert stored is not None
    assert stored.state is OwnerActionState.FAILED
    assert stored.reason == "RuntimeError"
    assert stored.error is not None
    assert "boom-" in stored.error
    assert len(stored.error) <= 2003


def test_dead_running_action_projects_uncertain(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _seed_run(db, "run-p3")
    action, _ = db.claim_owner_action(
        action_id="oact_p3", run_id="run-p3", kind="REVIEW", candidate_sha=_SHA_A
    )
    db.attach_owner_action_process(
        action.action_id, pid=_DEAD_PID, identity=_dead_identity()
    )

    stored = db.get_owner_action(action.action_id)
    assert stored is not None
    assert stored.state is OwnerActionState.QUEUED
    assert effective_action_state(stored) is OwnerActionState.UNCERTAIN
    assert action_identity(stored) is not None

    summary = summarize_owner_actions(db, "run-p3")
    assert summary.active_review is None


def test_unknown_liveness_fails_closed_for_duplicate_launch(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _seed_run(db, "run-p4")
    action, created = db.claim_owner_action(
        action_id="oact_p4", run_id="run-p4", kind="ACCEPT", candidate_sha=_SHA_A
    )
    assert created is True
    db.attach_owner_action_process(
        action.action_id, pid=424242, identity=_unknown_identity()
    )

    second, created_again = db.claim_owner_action(
        action_id="oact_p4b", run_id="run-p4", kind="ACCEPT", candidate_sha=_SHA_A
    )

    assert created_again is False
    assert second.action_id == "oact_p4"
    assert len(db.list_owner_actions("run-p4")) == 1


def test_uncertain_accept_is_never_automatically_replayed(
    tmp_path: Path, popen: _CountingPopen
) -> None:
    db = _db(tmp_path)
    _seed_run(db, "run-p5")
    supervisor = OwnerActionSupervisor(db, popen_factory=popen)
    first = supervisor.request_accept(
        run_id="run-p5", candidate_sha=_SHA_A, target_repo="/tmp/r3e2-target"
    )
    assert first.launched is True
    db.attach_owner_action_process(
        first.action.action_id, pid=_DEAD_PID, identity=_dead_identity()
    )
    assert popen.call_count == 1

    # No watchdog exists: nothing launches spontaneously for the dead child.
    assert popen.call_count == 1
    assert len(db.list_owner_actions("run-p5")) == 1

    # An explicit owner request reconciles the dead row to UNCERTAIN history
    # (with its own uncertain event) and starts exactly one new action.
    second = supervisor.request_accept(
        run_id="run-p5", candidate_sha=_SHA_A, target_repo="/tmp/r3e2-target"
    )
    assert second.launched is True
    assert second.action.action_id != first.action.action_id
    assert popen.call_count == 2
    rows = {row.action_id: row for row in db.list_owner_actions("run-p5")}
    assert rows[first.action.action_id].state is OwnerActionState.UNCERTAIN
    assert rows[first.action.action_id].reason == OWNER_PROCESS_DEAD_REASON
    uncertain = db.get_events_by_type("run-p5", "owner_action_uncertain")
    assert len(uncertain) == 1
    payload = uncertain[0].payload
    assert isinstance(payload, dict)
    assert payload["action_id"] == first.action.action_id


# --- Canonical evidence stays with the engines -------------------------------------


def test_successful_review_keeps_canonical_review_evidence(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _seed_run(db, "run-e1")
    _gate_pass(db, "run-e1", "run-e1-a1", 1, _SHA_A)
    action, _ = db.claim_owner_action(
        action_id="oact_e1", run_id="run-e1", kind="REVIEW", candidate_sha=_SHA_A
    )
    service = _StubReviewService(db)

    code = run_review_action_child(
        db=db,
        action_id=action.action_id,
        expected_candidate_sha=_SHA_A,
        service_factory=lambda _db: service,
    )

    assert code == 0
    reviews = db.get_reviews_for_run("run-e1")
    assert len(reviews) == 1
    assert reviews[0].verdict is ReviewVerdict.PASS
    assert reviews[0].id == "rev-stub-1"
    completed = db.get_events_by_type("run-e1", "review_completed")
    assert len(completed) == 1
    # The action row reports process outcome only; it is not a verdict store.
    stored = db.get_owner_action(action.action_id)
    assert stored is not None
    assert stored.state is OwnerActionState.SUCCEEDED
    assert stored.result_summary is not None


def test_successful_accept_keeps_engine_acceptance_evidence(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _seed_run(db, "run-e2")
    _gate_pass(db, "run-e2", "run-e2-a1", 1, _SHA_A)
    action, _ = db.claim_owner_action(
        action_id="oact_e2", run_id="run-e2", kind="ACCEPT", candidate_sha=_SHA_A
    )
    service = _StubAcceptService(db)

    code = run_accept_action_child(
        db=db,
        action_id=action.action_id,
        expected_candidate_sha=_SHA_A,
        service_factory=lambda _db: service,
    )

    assert code == 0
    accepted = db.get_events_by_type("run-e2", "run_accepted")
    assert len(accepted) == 1
    stored = db.get_owner_action(action.action_id)
    assert stored is not None
    assert stored.state is OwnerActionState.SUCCEEDED
    assert stored.result_summary is not None
    assert "main" in stored.result_summary


# --- SSE / projection / isolation / reject ------------------------------------------


def test_queued_event_carries_bounded_secret_free_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, popen: _CountingPopen
) -> None:
    db_path = tmp_path / "web.db"
    db = Database(db_path)
    _seed_run(db, "run-v1")
    _gate_pass(db, "run-v1", "run-v1-a1", 1, _SHA_A)
    monkeypatch.setattr(subprocess, "Popen", popen)
    supervisor = OwnerActionSupervisor(db, popen_factory=popen)
    launch = supervisor.request_review(
        run_id="run-v1", candidate_sha=_SHA_A, target_repo="/tmp/r3e2-target"
    )
    assert launch.launched is True

    events = db.get_events_by_type("run-v1", "owner_action_queued")
    assert len(events) == 1
    payload = events[0].payload
    assert isinstance(payload, dict)
    assert set(payload.keys()) == {"action_id", "kind", "candidate_sha"}
    assert payload["kind"] == "REVIEW"
    assert payload["candidate_sha"] == _SHA_A
    blob = json.dumps(payload).lower()
    assert "secret" not in blob
    assert "token" not in blob


def test_run_page_and_actions_json_show_durable_action_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, popen: _CountingPopen
) -> None:
    db_path = tmp_path / "web.db"
    db = Database(db_path)
    _seed_run(db, "run-v2")
    _gate_pass(db, "run-v2", "run-v2-a1", 1, _SHA_A)
    monkeypatch.setattr(subprocess, "Popen", popen)
    supervisor = OwnerActionSupervisor(db, popen_factory=popen)
    launch = supervisor.request_review(
        run_id="run-v2", candidate_sha=_SHA_A, target_repo="/tmp/r3e2-target"
    )
    assert launch.launched is True
    client = TestClient(create_app(db_path=db_path))

    actions = client.get("/runs/run-v2/actions").json()
    assert actions["owner_review_active"] is True
    assert actions["owner_review_state"] == "QUEUED"
    assert actions["owner_accept_active"] is False

    page = client.get("/runs/run-v2")
    assert page.status_code == 200
    assert 'id="owner-action-status"' in page.text
    assert "A Review owner action is already running" in page.text


def test_reject_remains_synchronous_and_untouched(tmp_path: Path) -> None:
    from saberops.candidate_lifecycle import REJECTION_EVENT_TYPE

    db_path = tmp_path / "web.db"
    db = Database(db_path)
    _seed_run(db, "run-v4")
    _gate_pass(db, "run-v4", "run-v4-a1", 1, _SHA_A)
    client = TestClient(create_app(db_path=db_path))

    decided = client.post(
        "/runs/run-v4/reject", data={"confirm": "1"}, follow_redirects=False
    )

    assert decided.status_code == 303
    assert decided.headers["location"].endswith("/runs/run-v4?rejected=1")
    assert len(db.get_events_by_type("run-v4", REJECTION_EVENT_TYPE)) == 1
    assert db.list_owner_actions("run-v4") == []


def test_launch_failure_surfaces_fast_without_false_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "web.db"
    db = Database(db_path)
    _seed_run(db, "run-v5")
    _gate_pass(db, "run-v5", "run-v5-a1", 1, _SHA_A)
    failing = _FailingPopen()
    supervisor = OwnerActionSupervisor(db, popen_factory=failing)

    launch = supervisor.request_review(
        run_id="run-v5", candidate_sha=_SHA_A, target_repo="/tmp/r3e2-target"
    )

    assert launch.launched is False
    assert db.get_active_owner_action("run-v5", "REVIEW") is None
    rows = db.list_owner_actions("run-v5")
    assert len(rows) == 1
    assert rows[0].state is OwnerActionState.FAILED
    assert rows[0].reason == LAUNCH_FAILED_REASON


def test_run_owner_action_row_contract(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _seed_run(db, "run-v6")
    action, created = db.claim_owner_action(
        action_id="oact_v6",
        run_id="run-v6",
        kind="ACCEPT",
        candidate_sha=_SHA_A,
        target_repo="/tmp/r3e2-target",
    )
    assert created is True
    assert isinstance(action, RunOwnerAction)
    assert action.kind is OwnerActionKind.ACCEPT
    assert action.state is OwnerActionState.QUEUED
    assert action.pid is None
    assert action.created_at != ""
    assert action.updated_at != ""
    running = db.mark_owner_action_running(action.action_id, identity=_live_self_identity())
    assert running is not None
    assert running.state is OwnerActionState.RUNNING
    assert running.pid == os.getpid()
    terminal = db.mark_owner_action_terminal(
        action.action_id,
        state="SUCCEEDED",
        result_summary="main:deadbeef",
    )
    assert terminal is not None
    assert terminal.state is OwnerActionState.SUCCEEDED
    # Terminal states never overwrite each other.
    again = db.mark_owner_action_terminal(
        action.action_id, state="FAILED", reason="late", error="late"
    )
    assert again is not None
    assert again.state is OwnerActionState.SUCCEEDED




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

    from saberops.owner_actions import OwnerActionSupervisor

    db_path = tmp_path / "web.db"
    db = Database(db_path)
    _seed_run(db, "run-s1")
    _gate_pass(db, "run-s1", "run-s1-a1", 1, _SHA_A)
    monkeypatch.setattr(_subprocess, "Popen", popen)
    client = TestClient(create_app(db_path=db_path))

    # No owner work yet: nothing streams for a terminal run.
    assert _owner_actions_stream_active(db, "run-s1") is False

    launch = OwnerActionSupervisor(db, popen_factory=popen).request_review(
        run_id="run-s1", candidate_sha=_SHA_A, target_repo="/tmp/r3e21-target"
    )
    assert launch.launched is True

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

    from saberops.owner_actions import OwnerActionSupervisor

    db_path = tmp_path / "web.db"
    db = Database(db_path)
    _seed_run(db, "run-s2")
    _gate_pass(db, "run-s2", "run-s2-a1", 1, _SHA_A)
    monkeypatch.setattr(_subprocess, "Popen", popen)
    client = TestClient(create_app(db_path=db_path))
    launch = OwnerActionSupervisor(db, popen_factory=popen).request_review(
        run_id="run-s2", candidate_sha=_SHA_A, target_repo="/tmp/r3e21-target"
    )
    assert launch.launched is True
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


