"""Phase 1 F18: ``orch self-update`` (ADR-0031).

The engine may reinstall itself only from a commit that is already accepted on
the protected target branch and holds a passing full-gate receipt for the exact
SHA.  Before the reinstall every RUNNING worker is parked through the F17 path
and released afterwards, so nothing is discarded.  Every test uses a temporary
repository and a temporary install; the owner's installed ``orch`` is never
touched.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from saberops.gate_history import GateHistoryEntry, GateKind, append_entry, history_path
from saberops.self_update import full_gate_evidence, perform_self_update, read_restart_marker

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@localhost",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@localhost",
    "PATH": "/usr/bin:/bin",
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=GIT_ENV,
    ).stdout.strip()


def _commit(repo: Path, name: str, text: str = "x\n") -> str:
    (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "engine"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "a.txt")
    return repo


def _record_gate(
    repo: Path,
    sha: str,
    *,
    outcome: str = "PASS",
    profile: str = "full",
    kind: GateKind = GateKind.G3,
) -> None:
    append_entry(
        history_path(repo / ".git"),
        GateHistoryEntry(
            kind=kind,
            producer="engine",
            head_sha=sha,
            outcome=outcome,
            profile=profile,
        ),
    )


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in ("state", "config", "cache", "data", "home"):
        (tmp_path / name).mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    yield


def _db(tmp_path: Path) -> object:
    from saberops.db import Database

    return Database(tmp_path / "owner.db")


def _probe_sequence(*versions: str) -> Callable[[Path], str]:
    seen: list[str] = list(versions)

    def probe(_repo: Path) -> str:
        if not seen:
            return versions[-1]
        return seen.pop(0)

    return probe


def _installer(installs: list[Path]) -> Callable[[Path], tuple[bool, str]]:
    def install(root: Path) -> tuple[bool, str]:
        installs.append(root)
        return True, ""

    return install


def test_reinstalls_an_accepted_green_commit_and_reports_both_versions(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    accepted = _commit(repo, "b.txt")
    _record_gate(repo, accepted)
    order: list[str] = []
    installed: list[Path] = []
    states = iter(
        [
            SimpleNamespace(paused_jobs=("job-1", "job-2")),
            SimpleNamespace(paused_jobs=()),
        ]
    )

    def pause(_db: object, **_kwargs: object) -> object:
        order.append("pause")
        return next(states)

    def resume(_db: object, **_kwargs: object) -> None:
        order.append("resume")

    def install(root: Path) -> tuple[bool, str]:
        order.append("install")
        installed.append(root)
        assert _git(root, "rev-parse", "HEAD") == accepted
        return True, "installed"

    result = perform_self_update(
        _db(tmp_path),
        repo=repo,
        install=install,
        version_probe=_probe_sequence("orch 0.5.0", "orch 0.6.0"),
        pause=pause,
        resume=resume,
    )
    assert result.ok is True
    assert result.code == "updated"
    assert result.commit == accepted
    assert result.old_version == "orch 0.5.0"
    assert result.new_version == "orch 0.6.0"
    assert result.paused_jobs == ("job-1", "job-2")
    assert order == ["pause", "install", "resume"]
    assert installed == [repo.resolve()]
    marker = read_restart_marker()
    assert marker["revision"] == accepted
    assert marker["previous_revision"] == accepted


def test_refuses_a_commit_that_is_not_on_the_target_branch(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "side")
    side = _commit(repo, "side.txt")
    _record_gate(repo, side)
    _git(repo, "checkout", "-q", "main")
    _commit(repo, "b.txt")
    installs: list[Path] = []
    result = perform_self_update(
        _db(tmp_path),
        repo=repo,
        commit=side,
        install=_installer(installs),
        pause=lambda *_a, **_k: SimpleNamespace(paused_jobs=()),
        resume=lambda *_a, **_k: None,
    )
    assert result.ok is False
    assert result.code == "commit_not_accepted"
    assert installs == []


def test_refuses_a_commit_without_a_passing_full_gate(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    accepted = _commit(repo, "b.txt")
    _record_gate(repo, accepted, outcome="VALIDATION_FAILURE")
    installs: list[Path] = []
    result = perform_self_update(
        _db(tmp_path),
        repo=repo,
        install=_installer(installs),
        pause=lambda *_a, **_k: SimpleNamespace(paused_jobs=()),
        resume=lambda *_a, **_k: None,
    )
    assert result.ok is False
    assert result.code == "no_full_gate"
    assert installs == []


@pytest.mark.parametrize(
    ("outcome", "profile", "kind"),
    [
        ("PASS", "fast", GateKind.G3),
        ("PASS", "full", GateKind.G1),
        ("TIMEOUT", "full", GateKind.MAIN_GATE),
    ],
)
def test_only_a_full_gate_pass_on_the_exact_sha_counts(
    tmp_path: Path, outcome: str, profile: str, kind: GateKind
) -> None:
    repo = _repo(tmp_path)
    accepted = _commit(repo, "b.txt")
    _record_gate(repo, accepted, outcome=outcome, profile=profile, kind=kind)
    assert full_gate_evidence(repo.resolve(), accepted) is None
    result = perform_self_update(
        _db(tmp_path),
        repo=repo,
        install=lambda root: (True, ""),
        pause=lambda *_a, **_k: SimpleNamespace(paused_jobs=()),
        resume=lambda *_a, **_k: None,
    )
    assert result.code == "no_full_gate"


def test_a_flaky_full_gate_pass_is_green(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    accepted = _commit(repo, "b.txt")
    _record_gate(repo, accepted, outcome="FLAKY_PASS")
    assert full_gate_evidence(repo.resolve(), accepted) is not None


def test_refuses_a_commit_that_is_not_checked_out(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    accepted = _commit(repo, "b.txt")
    _record_gate(repo, accepted)
    _commit(repo, "c.txt")
    installs: list[Path] = []
    result = perform_self_update(
        _db(tmp_path),
        repo=repo,
        commit=accepted,
        install=_installer(installs),
        pause=lambda *_a, **_k: SimpleNamespace(paused_jobs=()),
        resume=lambda *_a, **_k: None,
    )
    assert result.code == "checkout_mismatch"
    assert installs == []


def test_refuses_a_dirty_checkout(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    accepted = _git(repo, "rev-parse", "HEAD")
    _record_gate(repo, accepted)
    (repo / "uncommitted.txt").write_text("dirty\n", encoding="utf-8")
    installs: list[Path] = []
    result = perform_self_update(
        _db(tmp_path),
        repo=repo,
        install=_installer(installs),
        pause=lambda *_a, **_k: SimpleNamespace(paused_jobs=()),
        resume=lambda *_a, **_k: None,
    )
    assert result.code == "dirty_worktree"
    assert installs == []


def test_a_failed_install_resumes_the_gate_and_leaves_no_marker(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    accepted = _git(repo, "rev-parse", "HEAD")
    _record_gate(repo, accepted)
    order: list[str] = []

    def pause(*_args: object, **_kwargs: object) -> object:
        order.append("pause")
        return SimpleNamespace(paused_jobs=("job-1",))

    def resume(*_args: object, **_kwargs: object) -> None:
        order.append("resume")

    result = perform_self_update(
        _db(tmp_path),
        repo=repo,
        install=lambda _root: (False, "pip exploded"),
        version_probe=_probe_sequence("orch 0.5.0"),
        pause=pause,
        resume=resume,
    )
    assert result.ok is False
    assert result.code == "install_failed"
    assert order == ["pause", "resume"]
    assert result.evidence["install_output"] == "pip exploded"
    assert read_restart_marker() == {}


def test_refuses_to_install_when_running_workers_cannot_be_parked(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    accepted = _git(repo, "rev-parse", "HEAD")
    _record_gate(repo, accepted)
    installs: list[Path] = []

    def pause(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("cannot stop worker")

    result = perform_self_update(
        _db(tmp_path),
        repo=repo,
        install=_installer(installs),
        pause=pause,
        resume=lambda *_a, **_k: None,
    )
    assert result.code == "pause_failed"
    assert installs == []


def test_not_a_repository_is_refused(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    result = perform_self_update(
        _db(tmp_path),
        repo=plain,
        install=lambda _root: (True, ""),
        pause=lambda *_a, **_k: SimpleNamespace(paused_jobs=()),
        resume=lambda *_a, **_k: None,
    )
    assert result.code == "not_a_repository"


def test_cli_reports_a_refusal_and_never_runs_an_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from saberops.cli import main
    from saberops.self_update import SelfUpdateResult

    def fake(db: object, **kwargs: object) -> SelfUpdateResult:
        return SelfUpdateResult(
            ok=False, code="no_full_gate", summary="not green", commit="abc"
        )

    monkeypatch.setattr("saberops.self_update.perform_self_update", fake)
    assert main(["self-update", "--repo", str(tmp_path), "--json"]) == 1


# ---------------------------------------------------------------------------
# The restart keeps a running job (F17 pause path)
# ---------------------------------------------------------------------------

pytestmark_rig = pytest.mark.packaged_routing_default


@pytest.mark.packaged_routing_default
def test_self_update_parks_and_resumes_a_running_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    from lifecycle_rig import RigEnvironment, build_lifecycle_rig

    from saberops.capacity.loop import run_scheduler_loop
    from saberops.jobs.models import JobState, WaitingReason
    from saberops.jobs.service import get_job, submit_job
    from saberops.mcp.lease import OrchestratorLease
    from saberops.model_access.readiness_service import ReadinessService
    from saberops.models import RunStatus
    from saberops.paths import owner_state_dir
    from saberops.pause_work import resume_work
    from saberops.runtime_gate import (
        evaluate_gate,
        mark_presence_known,
    )

    rig: RigEnvironment = build_lifecycle_rig(tmp_path / "rig", monkeypatch)
    monkeypatch.delenv("SABEROPS_RUNTIME_GATE", raising=False)
    try:
        lease = OrchestratorLease()
        lease.configure_database(Path(rig.db().db_path))
        assert lease.acquire() is True
        lease.touch()
        mark_presence_known()

        def tick() -> object:
            readiness = ReadinessService.for_owner_state(owner_state_dir())
            return run_scheduler_loop(rig.db(), once=True, readiness_service=readiness)

        def wait(predicate: Callable[[], bool], timeout: float = 180.0) -> None:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if predicate():
                    return
                time.sleep(0.2)
            pytest.fail("condition did not become true")

        rig.set_worker_mode("hang", hang_seconds=180)
        db = rig.db()
        submitted = submit_job(db, repo=rig.repo, goal="survive the engine update").job

        def dispatched() -> bool:
            tick()
            current = get_job(db, submitted.job_id)
            return bool(current and current.current_run_id)

        wait(dispatched)
        job = get_job(db, submitted.job_id)
        assert job is not None and job.current_run_id
        run_id = job.current_run_id

        def worker_started() -> bool:
            from saberops.supervisor import RunSupervisor

            owner, _ = RunSupervisor(db=db).owner_evidence(run_id)
            sup = db.get_supervision(run_id)
            return bool(owner and sup and sup.worker_pid and sup.worker_identity)

        wait(worker_started)
        attempt = db.get_attempts_for_run(run_id)[-1]
        worktree = Path(attempt.worktree_path)
        marker = worktree / "unfinished-work.txt"
        marker.write_text("preserved\n", encoding="utf-8")

        # The engine repository stands in for the SaberOps checkout; the rig's
        # jobs live in their own store, so only the guarded order and the real
        # F17 pause/resume path are exercised here.
        engine = tmp_path / "engine-repo"
        engine.mkdir()
        _git(engine, "init", "-q", "-b", "main")
        accepted = _commit(engine, "engine.txt")
        _record_gate(engine, accepted)
        result = perform_self_update(
            db,
            repo=engine,
            install=lambda _root: (True, "installed"),
            version_probe=_probe_sequence("orch 0.5.0", "orch 0.6.0"),
        )
        assert result.ok is True
        assert submitted.job_id in result.paused_jobs

        parked = get_job(db, submitted.job_id)
        assert parked is not None
        assert parked.state is JobState.QUEUED
        assert parked.waiting_reason is WaitingReason.PAUSED
        assert parked.resume_from is not None
        assert parked.resume_from.run_id == run_id
        run = db.get_run(run_id)
        assert run is not None and run.status is RunStatus.PREEMPTED
        assert marker.read_text(encoding="utf-8") == "preserved\n"
        assert worktree.exists()
        # The pause was released by self-update; the work was never discarded.
        assert evaluate_gate(db=db).closed is False
        assert evaluate_gate(db=db).reason is None

        rig.set_worker_mode("commit", target_file="done.txt", content="ok\n")

        def redispatched() -> bool:
            tick()
            current = get_job(db, submitted.job_id)
            return bool(
                current
                and (
                    current.current_run_id != run_id
                    or current.state is JobState.SUCCEEDED
                )
            )

        wait(redispatched)
        current = get_job(db, submitted.job_id)
        assert current is not None
        assert current.state is JobState.SUCCEEDED or current.current_run_id != run_id
    finally:
        try:
            OrchestratorLease().release()
        except Exception:
            pass
        try:
            resume_work(rig.db(), by="test-cleanup")
        except Exception:
            pass


def test_pause_reason_is_a_named_gate_reason() -> None:
    from saberops.runtime_gate import PAUSE_REASON_LABELS, PauseReason

    assert PauseReason.SELF_UPDATE.value == "self_update"
    assert PauseReason.SELF_UPDATE.value in PAUSE_REASON_LABELS
