"""Single-writer takeover and shared conversation contracts, without provider calls."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from saberops.agent_sessions import observe_invocation, read_sessions
from saberops.chat import ChatStore
from saberops.models import AttemptStatus, WorkerInterruptionReason, WorkerRequest, WorkerResult
from saberops.ownership import ProcessIdentity
from saberops.process import ProcessResult, run_process
from saberops.session_control import ControlConflict, SessionControls
from saberops.workers import ClaudeCodeAdapter, CodexAdapter, OpenCodeAdapter


def live_identity() -> str:
    identity = ProcessIdentity.for_pid(os.getpid())
    assert identity
    return json.dumps(identity.to_dict())


def wait_for(predicate: Any) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Timed out waiting for control transition")


def git(path: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)


def repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(
        repo,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@localhost",
        "commit",
        "--allow-empty",
        "-m",
        "base",
    )
    return repo


def result(*, interrupted: bool = False) -> WorkerResult:
    return WorkerResult(
        provider="codex",
        model="m",
        status=AttemptStatus.FAILED if interrupted else AttemptStatus.SUCCESS,
        summary="Done",
        stdout="Native reply",
        stderr="",
        interruption_reason=WorkerInterruptionReason.ABORTED if interrupted else None,
    )


def test_queue_is_idempotent_and_shared_with_chat_without_new_dispatch(tmp_path: Path) -> None:
    store = ChatStore(tmp_path / "conversations.db")
    chat_id = store.create(str(tmp_path), "Test")
    controls = SessionControls(tmp_path / "sessions")
    controls.register("sid", "run", live_identity(), "worker")
    controls.native_id("sid", "native_1234")
    controls.takeover("sid", chat_path=store.path, chat_id=chat_id)
    with pytest.raises(ControlConflict):
        controls.submit("sid", "Too early", "early")
    controls.finish("sid", "MANUAL")
    controls.submit("sid", "Inspect the file", "request1")
    controls.submit("sid", "Inspect the file", "request1")
    with pytest.raises(ControlConflict):
        controls.submit("sid", "Different text", "request1")
    with pytest.raises(ControlConflict):
        controls.resume("sid")
    message = controls.next_message("sid")
    assert message
    controls.reply(message, "Inspected")
    chat = store.get(chat_id)
    assert chat
    assert [(m["role"], m["text"]) for m in chat["messages"]] == [
        ("worker_user", "Inspect the file"),
        ("worker", "Inspected"),
    ]
    controls.submit("sid", "Never delivered", "request2")
    controls.interrupt("sid")
    chat = store.get(chat_id)
    assert chat
    assert chat["messages"][-1]["text"] == "Interrupted before delivery."
    controls.resume("sid")
    assert controls.mode("sid") == "RESUMING"


@pytest.mark.parametrize(
    "role,precommit",
    [
        ("worker", False),
        ("reviewer", False),
        ("orchestrator", False),
        ("worker", True),
    ],
)
def test_takeover_holds_caller_manual_turn_reuses_permissions_and_resume_revalidates(
    tmp_path: Path,
    role: str,
    precommit: bool,
) -> None:
    repo = repository(tmp_path)
    root = tmp_path / "sessions"
    controls = SessionControls(root)
    store = ChatStore(tmp_path / "conversations.db")
    chat = store.create(str(repo), "Original")
    calls: list[WorkerRequest] = []
    returned: list[WorkerResult] = []
    failures: list[BaseException] = []
    request = WorkerRequest(
        "Original assignment",
        repo,
        "m",
        timeout_seconds=8,
        read_only=role != "worker",
        env={"FROZEN": "value"},
    )

    def invoke(req: WorkerRequest) -> WorkerResult:
        calls.append(req)
        assert req.on_stdout and req.control_interrupt
        req.on_stdout('{"type":"thread.started","thread_id":"native_12345678"}\n')
        assert req.env == request.env and req.read_only == request.read_only
        if len(calls) == 1:
            if precommit:
                git(
                    repo,
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=t@localhost",
                    "commit",
                    "--allow-empty",
                    "-m",
                    "Worker commit before pause",
                )
            wait_for(req.control_interrupt)
            return result(interrupted=True)
        if req.task == "Manual question":
            assert req.native_resume_id == "native_12345678"
            return result()
        assert req.task.startswith("Original assignment")
        assert req.native_resume_id == (None if role == "reviewer" else "native_12345678")
        assert req.timeout_seconds < request.timeout_seconds
        return result()

    def run() -> None:
        try:
            returned.append(
                observe_invocation(
                    invoke,
                    request,
                    root=root,
                    owner="run",
                    role=role,
                    provider="codex",
                    association="attempt",
                )
            )
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        wait_for(
            lambda: read_sessions(root, "run") and read_sessions(root, "run")[0].get("can_takeover")
        )
        sid = read_sessions(root, "run")[0]["id"]
        controls.takeover(sid, chat_path=store.path, chat_id=chat)
        wait_for(lambda: controls.mode(sid) == "MANUAL")
        assert thread.is_alive() and not returned and len(calls) == 1
        controls.submit(sid, "Manual question", "human")
        wait_for(
            lambda: (
                len(calls) == 2
                and (controls.get(sid, messages=True) or {})["messages"][0]["state"] == "DONE"
            )
        )
        assert not returned
        if role == "worker":
            (repo / "manual.txt").write_text("A permitted manual edit")
        controls.resume(sid)
        thread.join(5)
        assert not thread.is_alive() and not failures
        assert returned[0].status == AttemptStatus.SUCCESS and controls.mode(sid) == "DONE"
        chat_data = store.get(chat)
        assert chat_data
        assert chat_data["messages"][-1]["text"] == "Native reply"
    finally:
        if thread.is_alive():
            controls.finish(sid, "RESUMING")
            thread.join(10)


@pytest.mark.parametrize("change", ["branch", "commit", "deadline"])
def test_revision_changes_or_expired_deadline_never_resume_automation(
    tmp_path: Path,
    change: str,
) -> None:
    repo = repository(tmp_path)
    root = tmp_path / "sessions"
    controls = SessionControls(root)
    store = ChatStore(tmp_path / "chat.db")
    chat = store.create(str(repo), "Test")
    calls: list[str] = []
    returned: list[WorkerResult] = []

    def invoke(req: WorkerRequest) -> WorkerResult:
        calls.append(req.task)
        assert req.on_stdout and req.control_interrupt
        req.on_stdout('{"type":"thread.started","thread_id":"native_12345678"}\n')
        wait_for(req.control_interrupt)
        return result(interrupted=True)

    thread = threading.Thread(
        target=lambda: returned.append(
            observe_invocation(
                invoke,
                WorkerRequest(
                    "original", repo, "m", timeout_seconds=0.4 if change == "deadline" else 5
                ),
                root=root,
                owner="run",
                role="worker",
                provider="codex",
                association="a",
            )
        )
    )
    thread.start()
    wait_for(
        lambda: read_sessions(root, "run") and read_sessions(root, "run")[0].get("can_takeover")
    )
    sid = read_sessions(root, "run")[0]["id"]
    controls.takeover(sid, chat_path=store.path, chat_id=chat)
    wait_for(lambda: controls.mode(sid) == "MANUAL")
    if change == "branch":
        git(repo, "checkout", "-b", "different")
    elif change == "commit":
        git(
            repo,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=t@localhost",
            "commit",
            "--allow-empty",
            "-m",
            "manual",
        )
    if change != "deadline":
        controls.resume(sid)
    thread.join(5)
    assert not thread.is_alive() and len(calls) == 1
    assert returned[0].status == (
        AttemptStatus.TIMEOUT if change == "deadline" else AttemptStatus.FAILED
    )


def test_dead_owner_cannot_control_or_resume_and_get_does_not_mutate(tmp_path: Path) -> None:
    controls = SessionControls(tmp_path)
    stale = json.loads(live_identity())
    stale["pid"] = 99999999
    controls.register("sid", "run", json.dumps(stale), "worker")
    controls.native_id("sid", "native_1234")
    controls.finish("sid", "MANUAL")
    assert (controls.get("sid") or {})["mode"] == "INTERRUPTED"
    assert controls.mode("sid") == "MANUAL"
    with pytest.raises(ControlConflict):
        controls.resume("sid")
    with pytest.raises(ControlConflict):
        controls.submit("sid", "hello", "id")


@pytest.mark.parametrize("broken_callback", [False, True])
def test_real_process_control_confirms_descendant_extinction_before_return(
    tmp_path: Path,
    broken_callback: bool,
) -> None:
    child = tmp_path / "child.pid"
    ready = threading.Event()
    code = (
        "import subprocess,sys,time,pathlib; "
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']); "
        "pathlib.Path(sys.argv[1]).write_text(str(p.pid)); print('ready',flush=True);time.sleep(60)"
    )

    def interrupt() -> bool:
        if broken_callback and ready.is_set():
            raise ValueError("Lost control channel")
        return ready.is_set()

    process = run_process(
        [sys.executable, "-c", code, str(child)],
        timeout=8,
        on_stdout=lambda _: ready.set(),
        control_interrupt=interrupt,
        termination_grace_seconds=0.1,
    )
    assert process.control_interrupted and not process.termination_incomplete
    assert not process.timed_out and process.exit_code != 0
    pid = int(child.read_text())
    proc_stat = Path(f"/proc/{pid}/stat")
    assert not proc_stat.exists() or proc_stat.read_text().split()[2] == "Z"


@pytest.mark.parametrize("provider", ["codex", "opencode", "claude-code"])
def test_native_resume_keeps_readonly_permissions_and_frozen_environment(
    tmp_path: Path,
    provider: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []
    monkeypatch.setattr("saberops.workers.codex.GitManager.get_changed_paths", lambda _: [])

    def runner(args: list[str], **kwargs: Any) -> ProcessResult:
        calls.append((args, kwargs))
        text = (
            json.dumps({"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty"})
            if "auth" in args
            else '{"type":"result","is_error":false,"result":"OK"}'
        )
        return ProcessResult(args, 0, text, "", 0.01)

    adapters = {
        "codex": CodexAdapter(runner=runner),
        "opencode": OpenCodeAdapter(runner=runner),
        "claude-code": ClaudeCodeAdapter(runner=runner),
    }

    def callback() -> bool:
        return False

    req = WorkerRequest(
        "Manual reply",
        tmp_path,
        "m",
        env={"ORCH_EXECUTION_BINDING_ID": "same"},
        read_only=True,
        persist_native_session=True,
        native_resume_id="native_12345678",
        control_interrupt=callback,
    )
    adapters[provider].run(req)
    args, kwargs = calls[-1]
    assert kwargs["control_interrupt"] is callback
    assert kwargs["env"]["ORCH_EXECUTION_BINDING_ID"] == "same"
    assert "native_12345678" in args and kwargs["cwd"] == tmp_path
    if provider == "codex":
        assert "--ephemeral" not in args and args[args.index("--sandbox") + 1] == "read-only"
        assert "--skip-git-repo-check" in args
    elif provider == "claude-code":
        assert "--no-session-persistence" not in args
        assert args[args.index("--tools") + 1] == "Read,Glob,Grep"
        assert args[args.index("--permission-mode") + 1] == "plan"
    else:
        assert "--pure" in args and "--session" in args


def test_only_successful_matching_orchestrator_session_is_reused(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    req = WorkerRequest(
        "one", tmp_path, "m", read_only=True, env={"ORCH_EXECUTION_BINDING_ID": "binding"}
    )
    resumes: list[str | None] = []

    def invoke(request: WorkerRequest) -> WorkerResult:
        resumes.append(request.native_resume_id)
        assert request.on_stdout
        request.on_stdout('{"type":"thread.started","thread_id":"native_12345678"}\n')
        return result()

    for request in [req, replace(req, task="two"), replace(req, model="other")]:
        observe_invocation(
            invoke,
            request,
            root=root,
            owner="chat",
            role="orchestrator",
            provider="codex",
            association="turn",
        )
    assert resumes == [None, "native_12345678", None]


def test_manual_stop_terminalizes_run_before_gate_or_automatic_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from saberops.db import Database
    from saberops.models import RunStatus
    from saberops.orchestrator import Orchestrator
    from saberops.workers.registry import AdapterRegistry

    repo = repository(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("SABEROPS_PROFILE", raising=False)
    monkeypatch.delenv("ORCH_BINDING_STORE", raising=False)
    calls: list[str] = []
    adapter = CodexAdapter(runner=lambda argv, **kwargs: ProcessResult(argv, 0, "", "", 0.01))

    def stopped(request: WorkerRequest) -> WorkerResult:
        calls.append(request.task)
        return WorkerResult(
            provider="codex",
            model="m",
            status=AttemptStatus.FAILED,
            summary="Worktree reconciliation failed",
            stdout="",
            stderr="",
            has_changes=False,
            manual_control_stopped=True,
        )

    monkeypatch.setattr(adapter, "run", stopped)
    db = Database(tmp_path / "run.db")
    orchestrator = Orchestrator(
        db=db, registry=AdapterRegistry([adapter]), worktree_base=tmp_path / "worktrees"
    )
    marker = tmp_path / "gate-ran"
    outcome = orchestrator.run(
        task="Test",
        repo_path=repo,
        provider_override="codex",
        model_override="m",
        max_attempts=3,
        gate_command=f"touch {marker}",
        review=False,
    )
    assert outcome.status == RunStatus.FAILED and len(calls) == 1
    assert not marker.exists()
    run = db.get_run(outcome.run_id)
    assert run and run.result_summary and "Manual control stopped automation" in run.result_summary
