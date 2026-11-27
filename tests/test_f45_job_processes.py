"""ADR-0100 (phase 1.1 lane H45): per-job processes and listening ports.

Drills:

* a fake worker that starts a long-lived child and a listener, then exits,
  shows both while the job runs and a leftover listener after it ends;
* the leftover is reported, never killed;
* a clean job reports no leftovers;
* a host-run native worker reports ``not_observable``;
* unreadable ``/proc`` evidence reports ``unknown``, never zero;
* the engine's own service ports are excluded by name.
"""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path
from typing import Any

import pytest
from lifecycle_rig import RigEnvironment, build_lifecycle_rig

from saberops import job_processes
from saberops.config import RunConfig
from saberops.job_processes import (
    ENGINE_SERVICE_PORTS,
    JobProcessRecorder,
    read_listening_sockets,
)
from saberops.models import RunStatus
from saberops.run_creation import RunRequest, resolve_effective_run_config
from saberops.supervisor import RunSupervisor

pytestmark = pytest.mark.packaged_routing_default

WORKER_PROVIDER = "codex"
WORKER_MODEL = "gpt-5.6-terra"


@pytest.fixture()
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RigEnvironment:
    return build_lifecycle_rig(tmp_path, monkeypatch)


def _start_run(rig: RigEnvironment, task: str) -> str:
    config: RunConfig = resolve_effective_run_config(
        RunRequest(
            task=task,
            repo_path=rig.repo,
            provider_override=WORKER_PROVIDER,
            model_override=WORKER_MODEL,
            max_attempts=1,
            worker_timeout=60.0,
            gate_timeout=60.0,
            review_enabled=True,
            review_timeout=60.0,
        )
    )
    return RunSupervisor(db=rig.db()).start_supervised_run(config).run_id


def _stop(pid: int) -> None:
    """Stop one drill process by exact pid (never a pattern kill)."""
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _wait_for_inventory(
    rig: RigEnvironment, run_id: str, *, timeout: float = 30.0
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Wait until both a child process row and a listening port were recorded."""
    db = rig.db()
    deadline = time.monotonic() + timeout
    processes: list[dict[str, Any]] = []
    ports: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        processes = db.get_job_processes(run_id)
        ports = db.get_job_ports(run_id)
        if ports and any(int(row["pid"]) != int(row["pgid"]) for row in processes):
            return processes, ports
        time.sleep(0.1)
    return processes, ports


def test_child_and_listener_are_recorded_and_a_leftover_is_reported(
    rig: RigEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The running job shows its child and listener; the exit leaves evidence."""
    port_file = rig.root / "holder-port.txt"
    detach_file = rig.root / "holder-detach"
    pid_file = rig.root / "holder-pid.txt"
    monkeypatch.setenv("ORCH_FAKE_WORKER_PORT_FILE", str(port_file))
    monkeypatch.setenv("ORCH_FAKE_WORKER_DETACH_FILE", str(detach_file))
    monkeypatch.setenv("ORCH_FAKE_WORKER_HOLDER_PID_FILE", str(pid_file))
    monkeypatch.setenv("SABEROPS_JOB_PROCESS_SAMPLE_SECONDS", "0.2")
    rig.set_worker_mode("children")
    db = rig.db()
    run_id = _start_run(rig, "ADR-0100 child and listener drill")
    holder_pid: int | None = None
    try:
        processes, port_rows = _wait_for_inventory(rig, run_id)
        assert port_rows, "the job's listening port was never sampled while it ran"
        port_row = port_rows[0]
        assert int(port_row["port"]) > 0

        assert processes, "the job's process group was never sampled while it ran"
        assert any(int(row["pid"]) != int(row["pgid"]) for row in processes), processes
        attempt_ids = {str(row["attempt_id"]) for row in processes}
        assert len(attempt_ids) == 1
        assert all(
            row["first_seen_at"] <= row["last_seen_at"] for row in processes
        )

        # Let the holder leave the fenced process group before the worker exits.
        detach_file.write_text("go", encoding="utf-8")
        deadline = time.monotonic() + 20.0
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert pid_file.exists(), "the drill child never detached"
        holder_pid = int(pid_file.read_text(encoding="utf-8").strip())

        run = rig.wait_for_run(run_id, timeout=90.0)
        assert run.status is RunStatus.COMPLETED, run.result_summary

        verdict = db.get_job_leftovers(run_id)
        assert verdict is not None
        assert verdict["verdict"] == "leftovers", verdict["detail"]
        leftover_ports = verdict["detail"]["listening_ports"]
        assert any(
            int(entry["port"]) == int(port_row["port"]) for entry in leftover_ports
        ), verdict["detail"]

        # Report, never kill: the detached listener is still alive after the job.
        assert pid_file.exists()
        os.kill(holder_pid, 0)
        assert "out_of_group_processes_unknown" in verdict["detail"]["limits"]
    finally:
        if pid_file.exists():
            try:
                holder_pid = int(pid_file.read_text(encoding="utf-8").strip())
            except ValueError:
                holder_pid = None
        if holder_pid is not None:
            _stop(holder_pid)


def test_clean_job_records_no_leftovers(rig: RigEnvironment) -> None:
    rig.set_worker_mode("commit")
    run_id = _start_run(rig, "ADR-0100 clean job drill")
    run = rig.wait_for_run(run_id, timeout=90.0)
    assert run.status is RunStatus.COMPLETED, run.result_summary
    verdict = rig.db().get_job_leftovers(run_id)
    assert verdict is not None
    assert verdict["verdict"] == "none", verdict
    # A plain orch run has no job; the read path resolves it from runs.job_id.
    assert verdict["job_id"] is None


def test_inventory_reads_resolve_the_job_id_from_the_run_row(
    rig: RigEnvironment,
) -> None:
    """H44's runs.job_id replaces a job-store walk on the inventory read path."""
    from saberops.db import current_iso_timestamp
    from saberops.models import Run, Tier

    db = rig.db()
    now = current_iso_timestamp()
    run_id = "run_h45_job_link"
    db.create_run(
        Run(
            id=run_id,
            task="job-linked inventory",
            target_repo=str(rig.repo),
            base_commit=rig.base_sha,
            tier=Tier.T1,
            training_allowed=True,
            gate_command=rig.gate_command,
            status=RunStatus.COMPLETED,
            created_at=now,
            completed_at=now,
            job_id="job_h45_drill",
        )
    )
    db.record_job_leftovers(run_id=run_id, attempt_id="attempt_h45", verdict="none")
    record = db.get_job_leftovers(run_id, "attempt_h45")
    assert record is not None
    assert record["job_id"] == "job_h45_drill"


def test_native_worker_reports_not_observable(
    rig: RigEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host-run worker has no engine-owned process group to observe."""
    import subprocess

    from saberops.jobs.models import JobState
    from saberops.jobs.service import submit_job
    from saberops.jobs.store import get_job
    from saberops.models import ReviewVerdict
    from saberops.native_workers import claim_native_job, record_native_attempt

    monkeypatch.setenv("ORCH_NATIVE_WORKERS", "1")
    monkeypatch.setenv("SABEROPS_DISABLE_AUTO_SCHEDULER", "1")
    job = submit_job(
        rig.db(), repo=rig.repo, goal="Native inventory drill", acceptance="file exists"
    ).job
    claim = claim_native_job(
        rig.db(), job.job_id, vendor="codex", model="codex-native-test-model"
    )
    worktree = Path(str(claim["worktree"]))
    (worktree / "native.txt").write_text("native\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(worktree), "add", "native.txt"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(worktree), "commit", "-m", "native change"],
        check=True,
        capture_output=True,
    )
    candidate = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    class _Review:
        id = "rev_native_h45"
        provider = "anthropic"
        model = "claude-sonnet-4-6"
        verdict = ReviewVerdict.PASS
        summary = "native review"

    def _reviewer(_db: Any, _run_id: str, _timeout: float) -> Any:
        return _Review()

    record_native_attempt(
        rig.db(),
        job.job_id,
        vendor=str(claim["vendor"]),
        model=str(claim["model"]),
        base_sha=str(claim["base_sha"]),
        candidate_sha=candidate,
        worktree=str(worktree),
        transcript="native transcript",
        reviewer=_reviewer,
    )
    job_after = get_job(rig.db(), job.job_id)
    assert job_after is not None
    assert job_after.state is JobState.SUCCEEDED
    verdict = rig.db().get_job_leftovers(str(claim["run_id"]))
    assert verdict is not None
    assert verdict["verdict"] == "not_observable", verdict
    assert "native_worker_processes_not_observable" in verdict["detail"]["limits"]


def test_unreadable_proc_evidence_is_unknown_not_zero(rig: RigEnvironment) -> None:
    """A recorder that cannot read /proc/net must never claim a clean result."""
    db = rig.db()
    run_id = _start_run(rig, "ADR-0100 unreadable evidence placeholder")
    run = rig.wait_for_run(run_id, timeout=90.0)
    assert run.status == RunStatus.COMPLETED, run.result_summary
    attempt_id = db.get_attempts_for_run(run_id)[-1].id
    from saberops.ownership import ProcessIdentity

    # A synthetic identity with no live group: the verdict must still be
    # "unknown" because the socket evidence could not be read.
    identity = ProcessIdentity(
        host="test-host",
        boot_id=None,
        pid=9_999_991,
        start_ticks=1,
        session_id=None,
        process_group_id=9_999_991,
    )
    recorder = JobProcessRecorder(
        db,
        run_id=run_id,
        attempt_id=attempt_id,
        sample_interval=0.0,
        net_root=Path("/var/tmp/saberops-h45-does-not-exist"),
    )
    recorder.attach_identity(identity)
    assert recorder.finish() == "unknown"
    verdict = db.get_job_leftovers(run_id, attempt_id)
    assert verdict is not None
    assert verdict["verdict"] == "unknown"
    assert verdict["detail"]["reason"] == "proc_evidence_incomplete"


def test_engine_service_ports_are_excluded_by_name(
    rig: RigEnvironment, tmp_path: Path
) -> None:
    """A socket on the engine's own service port is not recorded as a job port."""
    import subprocess
    import sys

    fixture = Path(__file__).resolve().parent / "fixtures" / "listener_process.py"
    port_file = tmp_path / "listener-port.txt"
    release_file = tmp_path / "listener-release"
    leader = subprocess.Popen(
        [sys.executable, str(fixture), str(port_file), str(release_file)],
        start_new_session=True,
    )
    db = rig.db()
    try:
        deadline = time.monotonic() + 20.0
        while not port_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert port_file.exists()
        port = int(port_file.read_text(encoding="utf-8").strip())

        run_id = _start_run(rig, "ADR-0100 engine port placeholder")
        run = rig.wait_for_run(run_id, timeout=90.0)
        assert run.status == RunStatus.COMPLETED, run.result_summary
        attempt_id = db.get_attempts_for_run(run_id)[-1].id

        from saberops.ownership import ProcessIdentity

        identity = ProcessIdentity.for_pid(leader.pid)
        assert identity is not None
        recorder = JobProcessRecorder(
            db,
            run_id=run_id,
            attempt_id=attempt_id,
            sample_interval=0.0,
        )
        saved = dict(ENGINE_SERVICE_PORTS)
        ENGINE_SERVICE_PORTS[port] = "test-service"
        try:
            recorder.attach_identity(identity)
            release_file.write_text("go", encoding="utf-8")
            leader.wait(timeout=20.0)
            verdict = recorder.finish()
        finally:
            ENGINE_SERVICE_PORTS.clear()
            ENGINE_SERVICE_PORTS.update(saved)
    finally:
        if leader.poll() is None:
            _stop(leader.pid)
            leader.wait(timeout=10.0)

    assert db.get_job_ports(run_id) == []
    record = db.get_job_leftovers(run_id, attempt_id)
    assert record is not None
    assert record["verdict"] == verdict == "none"
    assert record["detail"]["excluded_engine_services"] == ["test-service"]


def test_proc_net_tcp_parsing_reads_only_listeners(tmp_path: Path) -> None:
    net = tmp_path / "net"
    net.mkdir()
    (net / "tcp").write_text(
        "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when "
        "retrnsmt   uid  timeout inode\n"
        "   0: 0100007F:1F90 00000000:0000 0A 00000000:00000000 00:00000000 "
        "00000000  1000        0 4242 1 0000000000000000 100 0 0 10 0\n"
        "   1: 0100007F:1F91 0100007F:9C40 01 00000000:00000000 00:00000000 "
        "00000000  1000        0 4343 1 0000000000000000 20 4 30 10 -1\n",
        encoding="utf-8",
    )
    (net / "tcp6").write_text(
        "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when "
        "retrnsmt   uid  timeout inode\n"
        "   0: 00000000000000000000000000000000:1F92 000000000000000000000000"
        "00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 "
        "4545 1 0000000000000000 100 0 0 10 0\n",
        encoding="utf-8",
    )
    result = read_listening_sockets(net)
    assert result.complete is True
    ports = {(entry.protocol, entry.port, entry.inode) for entry in result.sockets}
    assert ports == {("tcp", 8080, 4242), ("tcp6", 8082, 4545)}
    assert {entry.address for entry in result.sockets if entry.protocol == "tcp"} == {
        "127.0.0.1"
    }


def test_missing_net_tables_are_incomplete(tmp_path: Path) -> None:
    empty = tmp_path / "no-net"
    empty.mkdir()
    result = read_listening_sockets(empty)
    assert result.complete is False
    assert result.sockets == ()
    assert "saberops-web" in job_processes.ENGINE_SERVICE_PORTS.values()
