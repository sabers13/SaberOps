"""F3: read-only live progress tail over MCP.

``tail_job`` must let a planner in a second session read a running rig worker's
progress incrementally without changing anything: the run's event log and its
PTY transcript, addressed by one cursor, available while another session holds
the planning lease.

Hermetic: fresh empty store, toy repository, fake worker CLI, no vendor prompt
and no network.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest
from lifecycle_rig import RigEnvironment, build_lifecycle_rig

from saberops.config import RunConfig
from saberops.db import Database
from saberops.jobs.service import get_job, submit_job
from saberops.mcp.errors import MCP_NOT_FOUND, InvalidParamsError
from saberops.mcp.evidence import EvidenceRoots
from saberops.mcp.lease import OrchestratorLease
from saberops.mcp.ledger import EventLedger
from saberops.mcp.protocol import McpServer
from saberops.mcp.tail import (
    MAX_TAIL_BYTES,
    TailCursor,
    parse_cursor,
    strip_terminal_control,
    tail_run,
)
from saberops.mcp.tools import READ_TOOLS, TOOL_NAMES, ServerContext
from saberops.models import Run, RunStatus
from saberops.run_creation import RunRequest, resolve_effective_run_config
from saberops.supervisor import RunSupervisor

pytestmark = pytest.mark.packaged_routing_default


@pytest.fixture()
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RigEnvironment:
    return build_lifecycle_rig(tmp_path, monkeypatch)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _second_session(rig: RigEnvironment) -> McpServer:
    """A second MCP session: another client holds the planning lease."""
    orch = rig.root / ".orch"
    orch.mkdir(parents=True, exist_ok=True)
    lease_path = rig.root / "second-session-lease.json"
    OrchestratorLease(lease_path, holder="primary-planner", pid=1).acquire()
    server = McpServer(
        ServerContext(
            db_path=Path(rig.db().db_path),
            questions_store=rig.root / "second-questions.json",
            config_ledger_path=rig.root / "second-config.jsonl",
            evidence=EvidenceRoots(roots=(orch,)),
            ledger=EventLedger(rig.root / "second-session-ledger.jsonl"),
            lease=OrchestratorLease(lease_path, holder="second", pid=os.getpid()),
        )
    )
    server.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
    server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    return server


def _call(server: McpServer, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    response = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    )
    assert response is not None
    return response


def _payload(server: McpServer, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    response = _call(server, name, arguments)
    assert "result" in response, response
    assert response["result"]["isError"] is False, response
    structured: dict[str, Any] = response["result"]["structuredContent"]
    return structured


def _tail(server: McpServer, job_id: str, **arguments: Any) -> dict[str, Any]:
    return _payload(server, "tail_job", {"job_id": job_id, **arguments})


def _start_supervised(
    rig: RigEnvironment, *, task: str, worker_timeout: float = 60.0
) -> str:
    db = rig.db()
    config: RunConfig = resolve_effective_run_config(
        RunRequest(
            task=task,
            repo_path=rig.repo,
            max_attempts=1,
            worker_timeout=worker_timeout,
            gate_timeout=30.0,
            review_enabled=True,
            review_timeout=30.0,
        )
    )
    started = RunSupervisor(db=db).start_supervised_run(config)
    return str(started.run_id)


def _run_to_completion(rig: RigEnvironment, *, task: str) -> Run:
    run_id = _start_supervised(rig, task=task)
    return rig.wait_for_run(run_id, timeout=45.0)


def _wait_until(predicate: Any, *, timeout: float, interval: float = 0.2) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError("condition not met within the timeout")


def _job_for_run(db: Database, run_id: str) -> str:
    """Bind a dispatched run to a real job so ``tail_job`` has its job id."""
    from saberops.jobs.models import Job, JobRequirements, JobState
    from saberops.jobs.store import insert_job

    run = db.get_run(run_id)
    assert run is not None
    state = JobState.RUNNING
    if run.status == RunStatus.COMPLETED:
        state = JobState.SUCCEEDED
    elif run.status == RunStatus.FAILED:
        state = JobState.FAILED
    job_id = f"job-{run_id}"
    now = "2026-11-19T00:00:00+00:00"
    insert_job(
        db,
        Job(
            job_id=job_id,
            idempotency_key=None,
            caller_id="f3-test",
            repo=run.target_repo,
            base_ref=run.base_commit,
            base_sha=run.base_commit,
            goal="tail probe",
            acceptance=None,
            requirements=JobRequirements(),
            state=state,
            current_run_id=run_id,
            run_ids=(run_id,),
            created_at=now,
            updated_at=now,
        ),
    )
    return job_id


# --------------------------------------------------------------------------- #
# Contract surface
# --------------------------------------------------------------------------- #


def test_tail_job_is_a_read_only_tool() -> None:
    assert "tail_job" in TOOL_NAMES
    assert "tail_job" in READ_TOOLS


def test_tail_job_is_listed_with_a_cursor_schema(tmp_path: Path) -> None:
    from saberops.mcp.tools import build_default_registry

    registry = build_default_registry(ServerContext(db_path=tmp_path / "mcp.db"))
    descriptor = next(
        tool for tool in registry.descriptors() if tool["name"] == "tail_job"
    )
    properties = descriptor["inputSchema"]["properties"]
    assert set(properties) == {"job_id", "after_sequence", "max_bytes"}
    assert descriptor["inputSchema"]["required"] == ["job_id"]


# --------------------------------------------------------------------------- #
# The lane's done-when: a running rig worker
# --------------------------------------------------------------------------- #


def test_tail_job_follows_a_running_worker_from_a_second_session(
    rig: RigEnvironment,
) -> None:
    """Read a live rig worker's output incrementally without changing anything."""
    rig.set_worker_mode("live", live_lines=60, live_interval=0.15)
    run_id = _start_supervised(rig, task="Tail live worker output")
    job_id = _job_for_run(rig.db(), run_id)
    server = _second_session(rig)
    assert server.read_only is True

    first: dict[str, Any] = {}

    def _has_live_output() -> bool:
        payload = _tail(server, job_id, max_bytes=4096)
        if payload.get("transcript", {}).get("text"):
            first.update(payload)
            return True
        return False

    _wait_until(_has_live_output, timeout=30.0)
    assert first["run_id"] == run_id
    assert first["state"] == "RUNNING"
    assert first["activity"] == "running"
    assert "fake codex live line" in first["transcript"]["text"]
    assert first["events"], "a running worker must expose its structured progress"
    assert first["transcript"]["offset"] == 0
    assert first["transcript"]["next_offset"] > 0

    # The next call returns only what is new: no repeated lines, a moved cursor.
    second = _tail(server, job_id, after_sequence=first["next_sequence"], max_bytes=4096)
    assert second["transcript"]["offset"] == first["transcript"]["next_offset"]
    assert not set(_lines(second["transcript"]["text"])) & set(
        _lines(first["transcript"]["text"])
    )
    assert parse_cursor(second["next_sequence"]).transcript_offset >= (
        parse_cursor(first["next_sequence"]).transcript_offset
    )

    # Follow the same cursor while the worker is alive: every further call may
    # add new output, and the cursor only ever moves forward.
    collected = list(_lines(first["transcript"]["text"]))
    collected.extend(_lines(second["transcript"]["text"]))
    cursor = second["next_sequence"]

    def _grew() -> bool:
        nonlocal cursor
        payload = _tail(server, job_id, after_sequence=cursor, max_bytes=4096)
        collected.extend(_lines(payload["transcript"]["text"]))
        assert parse_cursor(payload["next_sequence"]).transcript_offset >= (
            parse_cursor(cursor).transcript_offset
        )
        cursor = payload["next_sequence"]
        return bool(payload["transcript"]["text"])

    _wait_until(_grew, timeout=30.0)

    # Drain to the end and account for every line exactly once.
    run = rig.wait_for_run(run_id, timeout=60.0)
    assert run.status == RunStatus.COMPLETED, run.result_summary
    for _ in range(200):
        payload = _tail(server, job_id, after_sequence=cursor, max_bytes=8192)
        collected.extend(_lines(payload["transcript"]["text"]))
        payload_events = [event["id"] for event in payload["events"]]
        assert payload_events == sorted(set(payload_events))
        cursor = payload["next_sequence"]
        if not payload["has_more"] and not payload["transcript"]["text"]:
            break
    worker_lines = [line for line in collected if "fake codex live line" in line]
    assert len(worker_lines) == len(set(worker_lines)) == 60


def _lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip()]


# --------------------------------------------------------------------------- #
# Read-only guarantee
# --------------------------------------------------------------------------- #


def test_tail_job_changes_nothing(rig: RigEnvironment) -> None:
    run = _run_to_completion(rig, task="Tail a finished run")
    assert run.status == RunStatus.COMPLETED, run.result_summary
    db = rig.db()
    job_id = _job_for_run(db, run.id)
    server = _second_session(rig)

    events_before = db.get_events(run.id, after_id=0, limit=10_000)
    transcript = Path(
        Path(db.db_path).resolve().parent / "transcripts" / f"{run.id}.ansi"
    )
    assert transcript.is_file()
    raw_before = transcript.read_bytes()
    mtime_before = transcript.stat().st_mtime_ns
    job_before = get_job(db, job_id)
    assert job_before is not None
    updated_before = job_before.updated_at
    ledger = rig.root / "second-session-ledger.jsonl"

    cursor: Any = None
    for _ in range(50):
        payload = _tail(server, job_id, **({"after_sequence": cursor} if cursor else {}))
        cursor = payload["next_sequence"]
        if not payload["has_more"]:
            break

    assert db.get_events(run.id, after_id=0, limit=10_000) == events_before
    assert transcript.read_bytes() == raw_before
    assert transcript.stat().st_mtime_ns == mtime_before
    job_after = get_job(db, job_id)
    assert job_after is not None and job_after.updated_at == updated_before
    assert not ledger.exists(), "a read tool must never append to the ledger"


def test_tail_job_works_while_another_session_holds_the_lease(
    rig: RigEnvironment,
) -> None:
    run = _run_to_completion(rig, task="Tail under a foreign lease")
    job_id = _job_for_run(rig.db(), run.id)
    server = _second_session(rig)
    assert server.read_only is True
    payload = _tail(server, job_id)
    assert payload["run_id"] == run.id
    # A mutating call in the same session is still refused.
    refused = _call(server, "cancel_job", {"job_id": job_id, "reason": "nope"})
    assert refused["error"]["code"] == -32020


# --------------------------------------------------------------------------- #
# Bounded, resumable reads
# --------------------------------------------------------------------------- #


def test_tail_job_pages_events_and_transcript(rig: RigEnvironment) -> None:
    run = _run_to_completion(rig, task="Tail in small pages")
    db = rig.db()
    job_id = _job_for_run(db, run.id)
    server = _second_session(rig)

    cursor: Any = None
    texts: list[str] = []
    event_ids: list[int] = []
    for _ in range(400):
        arguments: dict[str, Any] = {"max_bytes": 512}
        if cursor is not None:
            arguments["after_sequence"] = cursor
        payload = _tail(server, job_id, **arguments)
        texts.append(payload["transcript"]["text"])
        event_ids.extend(event["id"] for event in payload["events"])
        cursor = payload["next_sequence"]
        if not payload["has_more"]:
            break
    expected_events = [event.id for event in db.get_events(run.id, after_id=0, limit=10_000)]
    assert event_ids == expected_events, "every event exactly once, in order"

    transcript_path = (
        Path(db.db_path).resolve().parent / "transcripts" / f"{run.id}.ansi"
    )
    expected_text = strip_terminal_control(
        transcript_path.read_bytes().decode("utf-8", errors="replace")
    )
    assert "".join(texts) == expected_text

    # A second read from the final cursor is empty and stable.
    final = _tail(server, job_id, after_sequence=cursor)
    assert final["events"] == []
    assert final["transcript"]["text"] == ""
    assert final["has_more"] is False


def test_tail_job_returns_an_empty_tail_before_dispatch(rig: RigEnvironment) -> None:
    db = rig.db()
    submitted = submit_job(db, repo=rig.repo, goal="Never dispatched")
    server = _second_session(rig)
    payload = _tail(server, submitted.job.job_id)
    assert payload["state"] == "QUEUED"
    assert payload["run_id"] is None
    assert payload["events"] == []
    assert payload["transcript"] is None
    assert payload["has_more"] is False
    assert payload["next_sequence"] == ":0:0"


def test_tail_job_reports_the_same_run_facts_as_the_report(rig: RigEnvironment) -> None:
    """H24: the live tail's run facts are the one run-report projection."""
    from saberops.observability.run_report import build_run_report

    run = _run_to_completion(rig, task="Tail a finished run's report facts")
    db = rig.db()
    job_id = _job_for_run(db, run.id)
    report = build_run_report(db, run.id)
    payload = _tail(_second_session(rig), job_id)
    assert payload["run"] == {
        "status": report["lifecycle"]["status"],
        "current_stage": report["lifecycle"]["current_stage"],
        "candidate_sha": report["identity"]["candidate_sha"],
        "candidate_source": report["identity"]["candidate_source"],
        "gate": report["gate"]["summary"],
        "review_verdicts": report["review"]["verdicts"],
        "acceptance_status": report["acceptance"]["acceptance_status"],
    }


def test_tail_job_refuses_an_unknown_job(rig: RigEnvironment) -> None:
    server = _second_session(rig)
    response = _call(server, "tail_job", {"job_id": "job-missing"})
    assert response["result"]["isError"] is True
    assert response["result"]["structuredContent"]["error"]["code"] == MCP_NOT_FOUND


def test_tail_job_refuses_a_malformed_cursor(rig: RigEnvironment) -> None:
    db = rig.db()
    submitted = submit_job(db, repo=rig.repo, goal="Malformed cursor")
    server = _second_session(rig)
    response = _call(
        server,
        "tail_job",
        {"job_id": submitted.job.job_id, "after_sequence": "not-a-cursor"},
    )
    assert response["result"]["isError"] is True
    assert "after_sequence" in response["result"]["structuredContent"]["error"]["message"]


def test_tail_run_restarts_when_the_job_moves_to_another_run(rig: RigEnvironment) -> None:
    run = _run_to_completion(rig, task="First run")
    db = rig.db()
    stale = TailCursor(run_id="run_previous", event_id=9_999, transcript_offset=9_999)
    payload = tail_run(db, run_id=run.id, cursor=stale, max_bytes=4096)
    assert payload["run_changed"] is True
    assert parse_cursor(payload["next_sequence"]).transcript_offset >= 0
    # Restarting means the run's own early events are returned, not skipped.
    first_ids = [event["id"] for event in payload["events"]]
    assert first_ids and first_ids[0] == db.get_events(run.id, after_id=0, limit=1)[0].id


# --------------------------------------------------------------------------- #
# Cursor and transcript units
# --------------------------------------------------------------------------- #


def test_cursor_round_trips_and_rejects_garbage() -> None:
    cursor = TailCursor(run_id="run_abc", event_id=12, transcript_offset=345)
    assert parse_cursor(cursor.encode()) == cursor
    assert parse_cursor(12) == TailCursor(event_id=12)
    assert parse_cursor(None) == TailCursor()
    for bad in ("garbage", "run:1", "run:x:1", "run:1:x"):
        with pytest.raises(InvalidParamsError):
            parse_cursor(bad)
    with pytest.raises(InvalidParamsError):
        parse_cursor(-1)


def test_strip_terminal_control_removes_escapes_and_keeps_text() -> None:
    raw = "\x1b[31mred\x1b[0m\r\nprogress 1\rprogress 2\x07\n\ttab\n\x1b]0;title\x07after"
    stripped = strip_terminal_control(raw)
    assert "\x1b" not in stripped and "\x07" not in stripped and "\r" not in stripped
    assert "red" in stripped and "progress 1" in stripped and "after" in stripped


def test_transcript_read_is_bounded_line_aligned_and_utf8_safe(tmp_path: Path) -> None:
    from saberops.mcp.tail import _read_transcript

    path = tmp_path / "run.ansi"
    path.write_text("one\ntwö\nthree\n", encoding="utf-8")

    first = _read_transcript(path, offset=0, budget=6)
    assert first["text"] == "one\n"
    assert first["next_offset"] == 4
    # Cut mid-line is completed on the next call, never split mid-character.
    second = _read_transcript(path, offset=first["next_offset"], budget=7)
    assert second["text"].startswith("twö\n")
    assert "�" not in second["text"]

    # A cursor past the end means the file was replaced: restart cleanly.
    rotated = _read_transcript(path, offset=10_000, budget=64)
    assert rotated["reset"] is True
    assert rotated["offset"] == 0
    assert rotated["text"] == "one\ntwö\nthree\n"


def test_transcript_read_returns_an_overlong_line_without_stalling(tmp_path: Path) -> None:
    from saberops.mcp.tail import _read_transcript

    path = tmp_path / "long.ansi"
    path.write_bytes(b"x" * 5000)
    chunk = _read_transcript(path, offset=0, budget=512)
    assert chunk["partial_line"] is True
    assert chunk["next_offset"] == 512
    assert len(chunk["text"]) == 512
    assert chunk["truncated"] is True


def test_transcript_read_with_no_budget_leaves_the_position_untouched(
    tmp_path: Path,
) -> None:
    """A spent budget must never look like end-of-file to a follow-up call."""
    from saberops.mcp.tail import _read_transcript

    path = tmp_path / "pending.ansi"
    path.write_bytes(b"still running\n")
    chunk = _read_transcript(path, offset=0, budget=0)
    assert chunk["text"] == ""
    assert chunk["next_offset"] == 0
    assert chunk["size"] == len(b"still running\n")


def test_max_bytes_is_clamped(rig: RigEnvironment) -> None:
    run = _run_to_completion(rig, task="Clamp the budget")
    db = rig.db()
    payload = tail_run(
        db, run_id=run.id, cursor=TailCursor(), max_bytes=MAX_TAIL_BYTES * 10
    )
    assert payload["max_bytes"] == MAX_TAIL_BYTES


def test_tail_job_response_is_json_serialisable(rig: RigEnvironment) -> None:
    run = _run_to_completion(rig, task="Serialise the tail")
    job_id = _job_for_run(rig.db(), run.id)
    server = _second_session(rig)
    payload = _tail(server, job_id)
    json.dumps(payload)  # no non-JSON values leak into the read snapshot
    assert payload["v"] == 1
