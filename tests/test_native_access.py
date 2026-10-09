"""Native session access remains exclusive and never rewrites run outcomes."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from saberops.agent_sessions import _native_id
from saberops.chat import ChatStore
from saberops.native_access import (
    capability,
    claim_client,
    client_state,
    fail_launch,
    focus_tmux_client,
    interactive_environment,
    native_argv,
    native_environment,
    reserve,
    saved_directory,
    session_key,
    show_tmux_workspace,
    terminal_launch,
    update_client,
)
from saberops.native_handoff import HistoryMirror
from saberops.ownership import ProcessIdentity
from saberops.session_control import ControlConflict, SessionControls
from saberops.session_journal import SessionJournal


def setup(temporary: Path) -> tuple[SessionControls, dict[str, Any]]:
    identity = ProcessIdentity.for_pid(os.getpid())
    assert identity
    controls = SessionControls(temporary)
    controls.register("sid", "run", json.dumps(identity.to_dict()), "worker")
    controls.native_id("sid", "native_session123")
    return controls, {
        "id": "sid",
        "provider": "codex",
        "native_id": "native_session123",
        "model": "selected",
        "directory": str(temporary),
        "read_only": True,
    }


@pytest.mark.parametrize(
    "provider,flag",
    [
        ("codex", "resume"),
        ("anthropic", "--resume"),
        ("claude-code", "--resume"),
        ("opencode", "--session"),
        ("antigravity", "--conversation"),
        ("cline", "--id"),
        ("copilot", "--resume=native_session123"),
    ],
)
def test_each_provider_opens_explicit_session_and_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    flag: str,
) -> None:
    monkeypatch.setattr("saberops.native_access.shutil.which", lambda name: "/bin/" + name)
    _, data = setup(tmp_path)
    data["provider"] = provider
    argv = native_argv(data)
    assert flag in argv and "selected" in argv
    assert "native_session123" in " ".join(argv)
    assert "--continue" not in argv and "--ephemeral" not in argv
    assert "--dangerously-skip-permissions" not in argv


def test_lease_fences_duplicate_terminals_manual_messages_and_return(tmp_path: Path) -> None:
    controls, data = setup(tmp_path)
    controls.finish("sid", "MANUAL")
    token = reserve(controls, data)
    data.update(key=session_key(data), token=token)
    with pytest.raises(ControlConflict, match="already"):
        reserve(controls, data)
    with pytest.raises(ControlConflict, match="Close"):
        controls.resume("sid")
    with pytest.raises(ControlConflict, match="Close"):
        controls.submit("sid", "race", "request")
    assert controls.next_message("sid") is None
    update_client(controls, data, "DONE")
    controls.resume("sid")
    assert controls.mode("sid") == "RESUMING"
    with pytest.raises(ControlConflict, match="resuming"):
        reserve(controls, data)


def test_saved_card_cannot_launch_while_another_turn_uses_its_session(tmp_path: Path) -> None:
    controls, data = setup(tmp_path)
    controls.finish("sid", "DONE")
    identity = ProcessIdentity.for_pid(os.getpid())
    assert identity
    controls.register("new", "run", json.dumps(identity.to_dict()), "orchestrator")
    controls.native_id("new", data["native_id"])
    with pytest.raises(ControlConflict, match="another invocation"):
        reserve(controls, data)


def test_missing_identity_and_unconfirmed_shutdown_do_not_offer_native_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("saberops.native_access.shutil.which", lambda _: "/bin/provider")
    assert not capability({"provider": "codex"})["native_cli_available"]
    data = {
        "provider": "codex",
        "native_id": "native_session123",
        "completion": {"status": "SUCCESS", "quiescent": False},
    }
    assert not capability(data)["native_cli_available"]
    data["completion"]["quiescent"] = True  # type: ignore[index]
    assert capability(data)["native_cli_mode"] == "reopen"


def test_tmux_reuses_workspace_without_trying_to_attach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr("saberops.native_access.shutil.which", lambda _: "tmux")

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("saberops.native_access.subprocess.run", run)
    terminal_launch(
        [sys.executable, "/path with space/client", "literal;argument"], "tmux", "reviewer"
    )
    assert [argv[1] for argv in calls] == [
        "has-session", "set-option", "list-panes", "split-window"
    ]
    assert calls[1][-2:] == ["mouse", "on"]
    assert "'/path with space/client'" in calls[-1][-1]
    assert "'literal;argument'" in calls[-1][-1]


def test_tmux_failure_is_reported_with_actual_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("saberops.native_access.shutil.which", lambda _: "tmux")
    monkeypatch.setattr(
        "saberops.native_access.subprocess.run",
        lambda argv, **_: subprocess.CompletedProcess(argv, 1, "", "socket inaccessible"),
    )
    with pytest.raises(OSError, match="socket inaccessible"):
        terminal_launch(["provider"], "tmux", "worker")


def test_real_tmux_repeated_launches_work_without_web_server_tty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shlex

    actual = shutil.which("tmux")
    if not actual:
        pytest.skip("tmux is not installed")
    socket = "native-test-" + tmp_path.name
    wrapper = tmp_path / "tmux"
    wrapper.write_text(
        "#!/bin/sh\nexec " + shlex.join([actual, "-L", socket, "-f", "/dev/null"]) + ' "$@"\n'
    )
    wrapper.chmod(0o700)
    monkeypatch.setattr("saberops.native_access.shutil.which", lambda _: str(wrapper))
    try:
        for role in ("worker", "reviewer"):
            terminal_launch([sys.executable, "-c", "import time; time.sleep(20)"], "tmux", role)
        panes = subprocess.check_output(
            [
                actual,
                "-L",
                socket,
                "list-panes",
                "-s",
                "-t",
                "saberops-clients",
                "-F",
                "#{pane_id}\t#{@saberops_client}\t#{pane_pid}",
            ],
            text=True,
        )
        assert "worker" in panes and "reviewer" in panes
        assert subprocess.check_output(
            [actual, "-L", socket, "show-options", "-t", "saberops-clients", "mouse"],
            text=True,
        ).strip() == "mouse on"
        assert focus_tmux_client("worker")
        assert focus_tmux_client("reviewer")
        assert (
            subprocess.check_output(
                [
                    actual,
                    "-L",
                    socket,
                    "list-panes",
                    "-s",
                    "-t",
                    "saberops-clients",
                    "-F",
                    "#{pane_id}\t#{@saberops_client}\t#{pane_pid}",
                ],
                text=True,
            )
            == panes
        ), "Selecting an existing client must keep its provider process and pane"
    finally:
        subprocess.run([actual, "-L", socket, "kill-server"], capture_output=True, check=False)


def test_subscription_environment_excludes_extra_api_billing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, data = setup(tmp_path)
    data["provider"] = "anthropic"
    for key in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "CLAUDE_CODE_USE_BEDROCK"):
        monkeypatch.setenv(key, "private")
    env = native_environment(data)
    assert not any(
        key in env for key in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "CLAUDE_CODE_USE_BEDROCK")
    )
    assert env["CLAUDE_CODE_DISABLE_FAST_MODE"] == "1"


@pytest.mark.parametrize("provider", ["anthropic", "claude-code", "codex", "opencode",
                                      "antigravity", "cline", "copilot"])
def test_native_ui_ignores_headless_flags_but_preserves_terminal_and_provider_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str,
) -> None:
    _, data = setup(tmp_path)
    data["provider"] = provider
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for key in ("NO_COLOR", "FORCE_COLOR", "CLICOLOR", "CLICOLOR_FORCE", "CI",
                "CLAUDE_CODE_SIMPLE"):
        monkeypatch.setenv(key, "1")
    monkeypatch.setenv("TERM", "tmux-256color")
    monkeypatch.setenv("COLORTERM", "truecolor")
    env = native_environment(data)
    assert env["TERM"] == "tmux-256color" and env["COLORTERM"] == "truecolor"
    assert "NO_COLOR" not in env and "CI" not in env and "CLAUDE_CODE_SIMPLE" not in env
    assert os.environ["NO_COLOR"] == "1", "Automated workers keep their original environment"
    source = {"TERM": "dumb", "NO_COLOR": "1", "OPENCODE_SERVER_PASSWORD": "credential"}
    result = interactive_environment(source)
    assert result == {"TERM": "xterm-256color", "OPENCODE_SERVER_PASSWORD": "credential"}
    assert source["NO_COLOR"] == "1"
    assert interactive_environment({})["TERM"] == "xterm-256color"


def test_only_native_claude_orchestrator_gets_readable_chat_guidance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("saberops.native_access.shutil.which", lambda name: "/bin/" + name)
    _, data = setup(tmp_path)
    data.update(provider="anthropic", role="orchestrator")
    argv = native_argv(data)
    prompt = argv[argv.index("--append-system-prompt") + 1]
    assert "readable text" in prompt and "read-only" in prompt
    assert argv[argv.index("--permission-mode") + 1] == "plan"
    data["role"] = "reviewer"
    assert "--append-system-prompt" not in native_argv(data)


@pytest.mark.skipif(os.name != "posix", reason="Native terminal access uses POSIX PTYs")
def test_native_pty_has_size_at_startup_preserves_colors_input_and_resize(tmp_path: Path) -> None:
    import fcntl
    import signal
    import struct
    import termios
    import time

    import saberops.native_handoff

    controls, data = setup(tmp_path / "controls")
    controls.finish("sid", "DONE")
    report = tmp_path / "dimensions.json"
    resized = tmp_path / "resized.json"
    binary = tmp_path / "codex"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys, signal\nfrom pathlib import Path\n"
        f"report = {str(report)!r}\n"
        f"resized = Path({str(resized)!r})\n"
        "signal.signal(signal.SIGWINCH, lambda *_: "
        "resized.write_text(json.dumps(list(os.get_terminal_size()))))\n"
        "size = list(os.get_terminal_size())\n"
        "with open(report, 'w') as f: json.dump({'initial': size, 'no_color': "
        "os.environ.get('NO_COLOR')}, f)\n"
        "print('\\x1b[32mNative UI ready\\x1b[0m', flush=True)\n"
        "answer = sys.stdin.readline().strip()\n"
        "with open(report, 'w') as f: json.dump({'initial': size, 'resized': "
        "list(os.get_terminal_size()), 'input': answer}, f)\n"
    )
    binary.chmod(0o700)
    data.update(controls_root=str(controls.path.parent), directory=str(tmp_path),
                access_mode="reopen", key=session_key(data), token=reserve(controls, data),
                log=str(tmp_path / "native.log"))
    descriptor = tmp_path / "native.handoff"
    descriptor.write_text(json.dumps(data))
    master, slave = os.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 44, 120, 0, 0))
    proc = subprocess.Popen(
        [sys.executable, "-I", str(Path(saberops.native_handoff.__file__)), str(descriptor)],
        stdin=slave, stdout=slave, stderr=slave,
        env=dict(os.environ, PATH=str(tmp_path) + ":" + os.environ["PATH"], NO_COLOR="1"),
    )
    try:
        deadline = time.monotonic() + 10
        while not report.is_file() and time.monotonic() < deadline and proc.poll() is None:
            time.sleep(0.02)
        assert report.is_file(), "The native CLI must start inside the PTY"
        initial = json.loads(report.read_text())
        assert initial == {"initial": [120, 44], "no_color": None}
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 37, 99, 0, 0))
        os.kill(proc.pid, signal.SIGWINCH)
        deadline = time.monotonic() + 5
        while not resized.is_file() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert resized.is_file(), "The native TUI must receive resize notifications"
        assert json.loads(resized.read_text()) == [99, 37]
        os.write(master, b"hello native CLI\n")
        assert proc.wait(timeout=10) == 0
        assert json.loads(report.read_text()) == {
            "initial": [120, 44], "resized": [99, 37], "input": "hello native CLI"
        }
        assert b"\x1b[32mNative UI ready\x1b[0m" in Path(data["log"]).read_bytes()
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=10)
        os.close(master)
        os.close(slave)


def test_removed_review_worktree_uses_exact_recorded_git_tree(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()

    def git(*args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=repository, text=True).strip()

    git("init", "-q")
    (repository / "source.txt").write_text("reviewed content")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@local", "commit", "-qm", "base")
    tree = git("rev-parse", "HEAD^{tree}")
    (repository / "source.txt").write_text("later edits")
    folder = tmp_path / "sessions"
    folder.mkdir()
    path = saved_directory(
        {
            "id": "review",
            "worktree": str(tmp_path / "removed"),
            "repository": str(repository),
            "completion": {"source_tree": tree},
        },
        folder,
    )
    assert (path / "source.txt").read_text() == "reviewed content"
    assert (repository / "source.txt").read_text() == "later edits"


def test_native_history_mirrors_only_new_provider_messages_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controls, data = setup(tmp_path / "controls")
    chat = ChatStore(tmp_path / "chat.db")
    chat_id = chat.create(str(tmp_path), "Test")
    controls.finish("sid", "DONE")
    controls.link_chat("sid", chat_path=chat.path, chat_id=chat_id, saved=True)
    data.update(provider="anthropic", token="token")
    path = tmp_path / ".claude/projects/project/native_session123.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"type": "user", "message": {"content": "old prompt"}}) + "\n")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    mirror = HistoryMirror(data, controls)
    with path.open("a") as stream:
        for role, text in (("user", "new prompt"), ("assistant", "new response")):
            stream.write(json.dumps({"type": role, "message": {"content": text}}) + "\n")
    mirror.scan()
    mirror.scan()
    saved = chat.get(chat_id)
    assert saved and [m["text"] for m in saved["messages"]] == ["new prompt", "new response"]


@pytest.mark.parametrize("exit_code", [0, 7])
def test_native_wrapper_records_client_finish_without_changing_agent_outcome(
    tmp_path: Path,
    exit_code: int,
) -> None:
    controls, data = setup(tmp_path / "controls")
    controls.finish("sid", "DONE")
    journal = SessionJournal(controls.path.parent)
    journal.register("sid", "run", "worker", "a1", None)
    journal.complete("sid", {"status": "SUCCESS", "quiescent": True, "source_tree": "original"})
    before = journal.view("sid")["outcome"]
    binary = tmp_path / "codex"
    binary.write_text(f"#!/bin/sh\nprintf 'native test session\\n'\nexit {exit_code}\n")
    binary.chmod(0o700)
    data.update(
        controls_root=str(controls.path.parent),
        directory=str(tmp_path),
        access_mode="reopen",
        key=session_key(data),
        token=reserve(controls, data),
        log=str(tmp_path / "native.log"),
    )
    descriptor = tmp_path / "native.handoff"
    descriptor.write_text(json.dumps(data))
    import saberops.native_handoff

    result = subprocess.run(
        [sys.executable, "-I", str(Path(saberops.native_handoff.__file__)), str(descriptor)],
        env=dict(os.environ, PATH=str(tmp_path) + ":" + os.environ["PATH"]),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == exit_code, result.stderr
    assert "native test session" in result.stdout
    state = client_state(controls, data["key"])
    assert state and state["state"] == ("DONE" if exit_code == 0 else "FAILED")
    if exit_code:
        assert str(exit_code) in state["error"]
    assert journal.view("sid")["outcome"] == before
    assert any(e["kind"] == "native.client.output" for e in journal.view("sid")["events"])


@pytest.mark.parametrize(
    "provider,payload",
    [
        ("cline", {"type": "run_result", "sessionId": "native_session123"}),
        ("copilot", {"type": "session.start", "session_id": "native_session123"}),
    ],
)
def test_additional_provider_session_ids_are_captured(
    provider: str, payload: dict[str, str]
) -> None:
    assert _native_id(json.dumps(payload), provider) == "native_session123"


def test_finished_reviewer_can_reopen_through_run_endpoint_without_changing_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient

    from saberops.agent_sessions import observe_invocation, owner_directory
    from saberops.db import Database
    from saberops.models import AttemptStatus, Run, RunStatus, Tier, WorkerRequest, WorkerResult
    from saberops.web import create_app

    for name in ("XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(name, str(tmp_path / name))
    for name in (
        "SABEROPS_PROFILE",
        "ORCH_BINDING_STORE",
        "ORCH_ACCESS_STORE",
        "ORCH_ACCESS_MODELS_STORE",
    ):
        monkeypatch.delenv(name, raising=False)
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@local",
            "commit",
            "--allow-empty",
            "-qm",
            "base",
        ],
        cwd=repository,
        check=True,
    )
    db = Database(tmp_path / "web.db")
    db.create_run(
        Run(
            id="run",
            task="test",
            target_repo=str(repository),
            base_commit="base",
            tier=Tier.T1,
            training_allowed=False,
            gate_command="true",
            status=RunStatus.COMPLETED,
            created_at="2026-10-06T10:00:00+00:00",
            project_id="test",
            config_json="{}",
        )
    )
    root = tmp_path / "agent-sessions"

    def invoke(request: WorkerRequest) -> WorkerResult:
        assert request.on_stdout
        request.on_stdout('{"type":"system","session_id":"native_review123"}\n')
        return WorkerResult(
            "anthropic",
            "selected",
            AttemptStatus.SUCCESS,
            "done",
            "PASS",
            "",
            execution_quiescent=True,
        )

    observe_invocation(
        invoke,
        WorkerRequest("review", repository, "selected", read_only=True),
        root=root,
        owner="run",
        role="reviewer",
        provider="anthropic",
        association="review",
    )
    launches: list[list[str]] = []
    monkeypatch.setattr(
        "saberops.native_access.terminal_launch", lambda args, *_: launches.append(args)
    )
    shown: list[bool] = []

    def show_workspace(*, force_window: bool = False) -> dict[str, Any]:
        shown.append(True)
        return {"visible": True, "message": "Opened the shared workspace."}

    monkeypatch.setattr("saberops.native_access.show_tmux_workspace", show_workspace)
    monkeypatch.setattr("saberops.native_access.shutil.which", lambda name: "/bin/" + name)
    client = TestClient(create_app(db_path=tmp_path / "web.db", seed_cwd_if_empty=False))
    rows = client.get("/runs/run/sessions").json()["sessions"]
    sid = rows[0]["id"]
    before = SessionJournal(root).view(sid)["outcome"]
    response = client.post(f"/runs/run/sessions/{sid}/open-native-cli?backend=tmux")
    assert response.status_code == 200, response.text
    assert response.json()["mode"] == "reopen"
    assert launches and Path(launches[0][1]).name == "native_handoff.py"
    descriptor = Path(launches[0][2])
    assert descriptor.parent == owner_directory(root, "run")
    saved = json.loads(descriptor.read_text())
    assert saved["native_id"] == "native_review123" and saved["read_only"]
    assert Path(saved["directory"]) != repository
    assert client.post(f"/runs/run/sessions/{sid}/open-native-cli?backend=tmux").status_code == 409
    assert db.get_run("run").status == RunStatus.COMPLETED  # type: ignore[union-attr]
    assert SessionJournal(root).view(sid)["outcome"] == before
    update_client(SessionControls(root), saved, "DONE")

    def extra(request: WorkerRequest) -> WorkerResult:
        assert request.on_stdout
        request.on_stdout(
            json.dumps({"type": "system", "session_id": request.native_resume_id}) + "\n"
        )
        return WorkerResult(
            "anthropic",
            "selected",
            AttemptStatus.SUCCESS,
            "done",
            "done",
            "",
            execution_quiescent=True,
        )

    for role, native in (
        ("worker", "native_worker123"),
        ("orchestrator", "native_orch123"),
        ("orchestrator", "native_orch123"),
    ):
        observe_invocation(
            extra,
            WorkerRequest(role, repository, "selected", native_resume_id=native, read_only=True),
            root=root,
            owner="run",
            role=role,
            provider="anthropic",
            association=role,
        )
    launches.clear()
    shown.clear()
    bulk = client.post("/runs/run/sessions/open-tmux?all_sessions=true")
    assert bulk.status_code == 200, bulk.text
    assert len(bulk.json()["opened_sessions"]) == len(launches) == 3
    assert shown == [True], "The desktop workspace opens once for the batch"
    monkeypatch.setattr("saberops.native_access.focus_tmux_client", lambda _: True)
    assert client.post("/runs/run/sessions/open-tmux?all_sessions=true").status_code == 200
    assert len(launches) == 3, "Repeated open-all selects existing sessions"
    assert client.post("/runs/run/sessions/open-tmux").status_code == 200
    assert len(launches) == 3, "View tmux only opens the workspace terminal"
    assert client.post("/runs/missing/sessions/open-tmux").status_code == 404
    assert (
        client.post(
            "/runs/run/sessions/open-tmux", headers={"Origin": "https://foreign.example"}
        ).status_code
        == 403
    )


def test_tmux_workspace_opens_visible_terminal_and_reuses_attached_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    attached = False
    monkeypatch.setattr("saberops.native_access.shutil.which", lambda name: "/bin/" + name)

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            argv, 0, "/dev/pts/42" if attached and argv[1] == "list-clients" else "", ""
        )

    monkeypatch.setattr("saberops.native_access.subprocess.run", run)
    monkeypatch.setattr(
        "saberops.native_access.terminal_launch", lambda args, *_: calls.append(args)
    )
    assert show_tmux_workspace()["visible"]
    assert calls == [["/bin/tmux", "attach-session", "-t", "=saberops-clients"]]
    attached = True
    assert show_tmux_workspace()["reused"]
    assert len(calls) == 1
    assert show_tmux_workspace(force_window=True)["visible"]
    assert len(calls) == 2, "Explicit View always opens a visible desktop window"


def test_tmux_attach_failure_is_visible_without_claiming_a_window_opened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("saberops.native_access._ensure_tmux_workspace", lambda: None)
    monkeypatch.setattr(
        "saberops.native_access._tmux_run", lambda _: subprocess.CompletedProcess([], 0, "", "")
    )
    monkeypatch.setattr("saberops.native_access.shutil.which", lambda _: "/bin/tmux")

    def unavailable(*args: Any) -> None:
        raise ControlConflict("No desktop terminal is available")

    monkeypatch.setattr("saberops.native_access.terminal_launch", unavailable)
    result = show_tmux_workspace()
    assert not result["visible"]
    assert "No desktop terminal" in result["message"]


def test_late_or_duplicate_terminal_cannot_claim_another_launch(tmp_path: Path) -> None:
    controls, data = setup(tmp_path)
    data.update(key=session_key(data), token=reserve(controls, data))
    identity = ProcessIdentity.for_pid(os.getpid())
    assert identity
    claim_client(controls, data, identity)
    with pytest.raises(ControlConflict, match="already"):
        claim_client(controls, data, identity)
    update_client(controls, data, "DONE")
    newer = dict(data, token=reserve(controls, data))
    with pytest.raises(ControlConflict):
        claim_client(controls, data, identity)
    claim_client(controls, newer, identity)


def test_expired_launch_cannot_start_after_automation_was_released(tmp_path: Path) -> None:
    controls, data = setup(tmp_path)
    data.update(key=session_key(data), token=reserve(controls, data))
    identity = ProcessIdentity.for_pid(os.getpid())
    assert identity
    with controls.connect(write=True) as conn:
        conn.execute("UPDATE native_clients SET updated=0 WHERE key=?", (data["key"],))
    with pytest.raises(ControlConflict, match="expired"):
        claim_client(controls, data, identity)


def test_uncertain_terminal_launch_cannot_release_a_claimed_native_owner(tmp_path: Path) -> None:
    controls, data = setup(tmp_path)
    data.update(key=session_key(data), token=reserve(controls, data))
    identity = ProcessIdentity.for_pid(os.getpid())
    assert identity
    claim_client(controls, data, identity)
    fail_launch(controls, data, "Desktop launch timed out")
    assert client_state(controls, data["key"])["state"] == "STARTING"  # type: ignore[index]
    with pytest.raises(ControlConflict):
        reserve(controls, data)
