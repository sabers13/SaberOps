"""Protocol authority, durable completion, and fixed-source verification contracts."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from saberops.agent_sessions import observe_invocation, owner_directory, read_sessions
from saberops.chat import ChatStore
from saberops.config import RunConfig
from saberops.control_plane.capabilities import Capability, capability_profile_for
from saberops.models import AttemptStatus, ReasoningEffort, WorkerRequest, WorkerResult
from saberops.orchestrator import _run_configured_gate
from saberops.ownership import ProcessIdentity
from saberops.process import ProcessResult
from saberops.session_control import ControlConflict, SessionControls
from saberops.session_journal import SessionJournal
from saberops.session_runtime import AgentSessionRuntime, SessionRuntimeError, permission_rules
from saberops.verification_snapshot import fixed_snapshot, source_tree
from saberops.workers.antigravity import AntigravityAdapter


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-b", "main")
    (path / "source.txt").write_text("original")
    git(path, "add", ".")
    git(path, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-m", "base")
    return path


def live_identity() -> str:
    identity = ProcessIdentity.for_pid(os.getpid())
    assert identity
    return json.dumps(identity.to_dict())


def test_journal_survives_capped_output_and_has_one_completion(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("saberops.agent_sessions.LOG_MAX_BYTES", 20)
    root = tmp_path / "sessions"
    raw = '{"type":"thread.started","thread_id":"native_12345678"}\n' + "x" * 300

    def invoke(req: WorkerRequest) -> WorkerResult:
        assert req.on_stdout
        # Native identity can arrive across process read boundaries.
        req.on_stdout(raw[:12])
        req.on_stdout(raw[12:])
        return WorkerResult("codex", "model", AttemptStatus.SUCCESS, "Done", "OK", "")

    result = observe_invocation(
        invoke,
        WorkerRequest("task", repo, "model"),
        root=root,
        owner="run",
        role="worker",
        provider="codex",
        association="a1",
    )
    assert result.status == AttemptStatus.SUCCESS
    row = read_sessions(root, "run")[0]
    assert row["truncated"] and row["native_id"] == "native_12345678"
    journal = SessionJournal(root)
    view = journal.view(row["id"])
    assert (
        "".join(
            event["payload"]["text"] for event in view["events"] if event["kind"] == "output.stdout"
        )
        == raw
    )
    assert view["outcome"]["source_tree"] == git(repo, "rev-parse", "HEAD^{tree}")
    assert not journal.complete(row["id"], {"status": "SUCCESS", "source": "other"})
    assert journal.view(row["id"])["outcome"] == view["outcome"]


def test_owner_loss_records_uncertainty_without_relaunch(tmp_path: Path) -> None:
    journal = SessionJournal(tmp_path)
    dead = json.loads(live_identity())
    dead["pid"] = 99999999
    journal.register("dead-owner", "run", "worker", "a1", json.dumps(dead))
    journal.process("dead-owner", json.loads(live_identity()))
    assert journal.recover("run") == ["dead-owner"]
    assert journal.view("dead-owner")["outcome"]["status"] == "UNCERTAIN"
    assert journal.recover("run") == []
    journal.register("live-owner", "run", "worker", "a2", live_identity())
    assert journal.recover("run") == []
    assert journal.view("live-owner")["outcome"] is None


def test_source_hash_tracks_additions_deletions_and_does_not_change_index(repo: Path) -> None:
    original = source_tree(repo)
    index = git(repo, "diff", "--cached")
    (repo / "new.txt").write_text("new")
    assert source_tree(repo) != original
    assert git(repo, "diff", "--cached") == index
    (repo / "new.txt").unlink()
    (repo / "source.txt").unlink()
    assert source_tree(repo) != original
    with pytest.raises(ValueError, match="uncommitted"):
        with fixed_snapshot(repo):
            pass


def test_snapshot_does_not_observe_live_edits_and_is_removed(repo: Path) -> None:
    with fixed_snapshot(repo) as snapshot:
        (repo / "source.txt").write_text("edited outside snapshot")
        assert (snapshot.path / "source.txt").read_text() == "original"
        assert snapshot.tree != source_tree(repo)
        path = snapshot.path
    assert not path.exists()
    assert "saberops-verification" not in git(repo, "worktree", "list")


def test_gate_mutation_invalidates_the_verified_tree(repo: Path) -> None:
    config = RunConfig(task="task", target_repo=str(repo), gate_command="gate")
    result = _run_configured_gate(
        config,
        [
            sys.executable,
            "-c",
            "from pathlib import Path; Path('source.txt').write_text('gate changed it')",
        ],
        cwd=repo,
        timeout=10,
    )
    assert result.exit_code == 0 and not result.passed and result.verification_mutated
    assert result.verification_tree == git(repo, "rev-parse", "HEAD^{tree}")
    assert (repo / "source.txt").read_text() == "original"


@pytest.mark.parametrize("make_gate", [False, True])
def test_worker_committed_revision_is_the_verified_candidate(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_gate: bool
) -> None:
    from saberops.db import Database
    from saberops.models import RunStatus
    from saberops.orchestrator import Orchestrator
    from saberops.workers.codex import CodexAdapter
    from saberops.workers.registry import AdapterRegistry

    for key in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(key, str(tmp_path / key))
    for key in ("SABEROPS_PROFILE", "ORCH_BINDING_STORE"):
        monkeypatch.delenv(key, raising=False)
    committed: list[str] = []
    adapter = CodexAdapter()
    gate = "make gate" if make_gate else f"{sys.executable} -c pass"

    def invoke(request: WorkerRequest) -> WorkerResult:
        assert "[RUN VERIFICATION]" in request.task
        assert f"Frozen authoritative gate command (from the project root): {gate}" in request.task
        if make_gate:
            assert "root must have a Makefile with a gate target" in request.task
            (request.worktree_path / "Makefile").write_text(
                f".PHONY: gate\ngate:\n\t{sys.executable} -c pass\n"
            )
        (request.worktree_path / "source.txt").write_text("worker committed changes")
        git(request.worktree_path, "add", ".")
        git(
            request.worktree_path,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@localhost",
            "commit",
            "-m",
            "worker commit",
        )
        committed.append(git(request.worktree_path, "rev-parse", "HEAD"))
        return WorkerResult(
            "codex", "m", AttemptStatus.SUCCESS, "Done", "Done", "", has_changes=True
        )

    monkeypatch.setattr(adapter, "run", invoke)
    outcome = Orchestrator(
        db=Database(tmp_path / "run.db"),
        registry=AdapterRegistry([adapter]),
        worktree_base=tmp_path / "worktrees",
    ).run(
        config=RunConfig(
            task="Test committed worker",
            target_repo=str(repo),
            provider_override="codex",
            model_override="m",
            max_attempts=1,
            review_disabled=True,
            gate_command=gate,
        ),
    )
    assert outcome.status == RunStatus.COMPLETED
    assert outcome.attempts[-1].commit_sha == committed[0]


def test_autonomous_observer_retains_per_invocation_transport(repo: Path, tmp_path: Path) -> None:
    def invoke(request: WorkerRequest) -> WorkerResult:
        assert request.session_runtime is None
        return WorkerResult("opencode", "provider/model", AttemptStatus.SUCCESS, "Done", "", "")

    observe_invocation(
        invoke,
        WorkerRequest("task", repo, "provider/model"),
        root=tmp_path / "sessions",
        owner="run",
        role="worker",
        provider="opencode",
        association="a1",
        allow_shared_runtime=False,
    )


@pytest.fixture
def bridge(tmp_path: Path) -> Any:
    controls = SessionControls(tmp_path / "controls")
    controls.register("sid", "run", live_identity(), "worker")
    controls.native_id("sid", "ses_native123456")
    chat = ChatStore(tmp_path / "chat.db")
    chat_id = chat.create(str(tmp_path), "Test")
    controls.link_chat("sid", chat_path=chat.path, chat_id=chat_id)
    journal = SessionJournal(tmp_path / "controls")
    journal.register("sid", "run", "worker", "a1", live_identity())
    runtime = AgentSessionRuntime(tmp_path, "sid", controls, journal, lambda _: None)
    runtime.native_id = "ses_native123456"
    runtime.request = WorkerRequest("task", tmp_path, "provider/model")
    runtime._start_proxy("opencode")
    assert runtime.proxy
    yield runtime, controls, chat, chat_id
    assert runtime.close()


def http(
    runtime: AgentSessionRuntime, method: str, path: str, data: object = None, *, auth: bool = True
) -> tuple[int, Any]:
    assert runtime.proxy
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = (
            "Basic " + base64.b64encode(("opencode:" + runtime.client_password).encode()).decode()
        )
    req = urllib.request.Request(
        f"http://127.0.0.1:{runtime.proxy.server_port}" + path,
        data=json.dumps(data).encode() if data is not None else None,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def test_native_prompt_holds_automation_joins_queue_and_syncs_chat(bridge: Any) -> None:
    runtime, controls, chat, chat_id = bridge
    path = "/session/ses_native123456/prompt_async"
    body = {
        "messageID": "msg_human1234",
        "agent": "unrestricted",
        "model": {"providerID": "other", "modelID": "other"},
        "parts": [{"type": "text", "text": "Inspect current work"}],
    }
    assert http(runtime, "POST", path, body, auth=False)[0] == 401
    assert controls.mode("sid") == "ACTIVE"
    assert http(runtime, "POST", path, body)[0] == 204
    assert controls.mode("sid") == "REQUESTED"
    assert not controls.complete_if_active("sid")
    # Queued prompt is never delivered by the native client or before quiescence.
    controls.finish("sid", "MANUAL")
    assert http(runtime, "POST", path, body)[0] == 204
    message = controls.next_message("sid")
    assert message and message["text"] == "Inspect current work"
    with pytest.raises(ControlConflict):
        controls.resume("sid")
    controls.reply(message, "Done")
    assert [m["text"] for m in chat.get(chat_id)["messages"]] == ["Inspect current work", "Done"]
    controls.resume("sid")
    assert http(runtime, "POST", path, {"parts": [{"type": "text", "text": "Late"}]})[0] == 409


def test_native_client_cannot_change_frozen_settings_or_access_another_session(bridge: Any) -> None:
    runtime, controls, _, _ = bridge
    assert http(runtime, "GET", "/session/ses_other1234/message")[0] == 403
    assert http(runtime, "PATCH", "/config", {"permission": "allow"})[0] == 403
    assert (
        http(runtime, "POST", "/session/ses_native123456/shell", {"command": "touch bad"})[0] == 409
    )
    assert http(runtime, "POST", "/instance/dispose", {})[0] == 409
    assert controls.mode("sid") == "ACTIVE"


def test_native_abort_requests_takeover_and_does_not_resume_automation(bridge: Any) -> None:
    runtime, controls, _, _ = bridge
    path = "/session/ses_native123456/abort"
    assert http(runtime, "POST", path, {})[0] == 200
    assert controls.mode("sid") == "REQUESTED" and controls.should_interrupt("sid")
    controls.finish("sid", "MANUAL")
    assert http(runtime, "POST", path, {})[0] == 200
    assert controls.mode("sid") == "MANUAL" and controls.should_interrupt("sid")
    controls.resume("sid")
    assert http(runtime, "POST", path, {})[0] == 409


@pytest.mark.parametrize("prior_interrupted_turn", [False, True])
def test_protocol_turn_sends_frozen_model_and_readonly_agent_and_validates_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prior_interrupted_turn: bool,
) -> None:
    controls = SessionControls(tmp_path)
    runtime = AgentSessionRuntime(
        tmp_path, "sid", controls, SessionJournal(tmp_path), lambda _: None
    )
    runtime.native_id = "ses_native1234"
    runtime.has_turn = prior_interrupted_turn
    descriptor = tmp_path / "sid.native"
    descriptor.write_text(json.dumps({"session": runtime.native_id}))
    runtime.thread = threading.current_thread()
    runtime.request = WorkerRequest(
        "task", tmp_path, "provider/model", read_only=True, reasoning_effort=ReasoningEffort.HIGH
    )
    posted: list[Any] = []
    created: list[Any] = []
    polls = 0

    def api(method: str, path: str, body: object = None, **kwargs: Any) -> Any:
        nonlocal polls
        if method == "POST" and path == "/session":
            created.append(body)
            return {"id": "ses_fresh1234"}
        if method == "POST":
            posted.append(body)
            return None
        if path.endswith("/message"):
            polls += 1
            return (
                []
                if polls == 1
                else [
                    {
                        "info": {
                            "id": "msg_reply",
                            "role": "assistant",
                            "parentID": posted[0]["messageID"],
                            "time": {"completed": 1},
                            "finish": "stop",
                            "providerID": "provider",
                            "modelID": "model",
                        },
                        "parts": [{"type": "text", "text": "Done"}],
                    }
                ]
            )
        return {}

    monkeypatch.setattr(runtime, "api", api)
    monkeypatch.setattr("saberops.session_runtime.GitManager.get_changed_paths", lambda _: [])
    import time

    result = runtime._turn(runtime.request, time.monotonic())
    assert result.status == AttemptStatus.SUCCESS and result.stdout == "Done"
    assert posted[0]["model"] == {"providerID": "provider", "modelID": "model"}
    assert posted[0]["agent"] == "saberops"
    assert posted[0]["variant"] == "high"
    assert bool(created) == prior_interrupted_turn
    if prior_interrupted_turn:
        assert created[0]["agent"] == "saberops"
        assert json.loads(descriptor.read_text())["session"] == "ses_fresh1234"
    assert permission_rules({"edit": "deny"}) == [
        {"permission": "edit", "pattern": "*", "action": "deny"}
    ]


@pytest.mark.parametrize(
    "status,denied,expected",
    [
        ("SUCCESS", False, AttemptStatus.SUCCESS),
        ("SUCCESS", True, AttemptStatus.FAILED),
        ("ERROR", False, AttemptStatus.FAILED),
        (None, False, AttemptStatus.FAILED),
    ],
)
def test_antigravity_requires_terminal_success_and_no_denied_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str | None,
    denied: bool,
    expected: AttemptStatus,
) -> None:
    monkeypatch.setattr("saberops.workers.antigravity.Path.home", lambda: tmp_path)
    monkeypatch.setattr("saberops.workers.antigravity.GitManager.get_changed_paths", lambda _: [])
    calls: list[list[str]] = []
    raw = json.dumps(
        {
            "event": "result",
            "result": {
                "status": status,
                "response": "Done",
                "conversation_id": "native_conversation1234",
            },
        }
    )
    if denied:
        raw = (
            json.dumps(
                {
                    "event": "step_update",
                    "step_update": {"tool_info": {"error": {"type": "PERMISSION_DENIED"}}},
                }
            )
            + "\n"
            + raw
        )

    def runner(args: list[str], **kwargs: Any) -> ProcessResult:
        calls.append(args)
        assert kwargs["inherit_env"] is False
        assert "GEMINI_API_KEY" not in kwargs["env"]
        return ProcessResult(args, 0, raw, "", 0.01)

    result = AntigravityAdapter(runner=runner).run(
        WorkerRequest(
            "task", tmp_path, "model", read_only=True, native_resume_id="native_conversation1234"
        )
    )
    assert result.status == expected and result.provider_exit_code == 0
    assert "stream-json" in calls[0] and "plan" in calls[0] and "--conversation" in calls[0]
    assert "--dangerously-skip-permissions" not in calls[0]


@pytest.mark.parametrize(
    ("model", "ambient", "typed", "expected"),
    [
        ("gemini-3.8-flash-high", None, None, "high"),
        ("gemini-3.8-flash-low", None, None, "low"),
        ("claude-sonnet-5-5-medium", None, None, "medium"),
        ("legacy-model", None, None, "medium"),
        ("gemini-3.8-flash-high", "low", None, "low"),
        ("gemini-3.8-flash-low", "medium", ReasoningEffort.HIGH, "high"),
    ],
)
def test_antigravity_default_effort_matches_preset_without_overriding_explicit_effort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    model: str, ambient: str | None, typed: ReasoningEffort | None, expected: str,
) -> None:
    monkeypatch.setattr("saberops.workers.antigravity.Path.home", lambda: tmp_path)
    monkeypatch.setattr("saberops.workers.antigravity.GitManager.get_changed_paths", lambda _: [])
    if ambient is None:
        monkeypatch.delenv("ORCH_AGY_EFFORT", raising=False)
    else:
        monkeypatch.setenv("ORCH_AGY_EFFORT", ambient)
    calls: list[list[str]] = []

    def runner(args: list[str], **kwargs: Any) -> ProcessResult:
        calls.append(args)
        return ProcessResult(args, 0, json.dumps({
            "event": "result", "result": {"status": "SUCCESS", "response": "Done"},
        }), "", 0.01)

    result = AntigravityAdapter(runner=runner).run(WorkerRequest(
        "task", tmp_path, model, read_only=True, reasoning_effort=typed,
    ))
    assert result.status == AttemptStatus.SUCCESS
    assert calls[0][calls[0].index("--model") + 1] == model
    assert calls[0][calls[0].index("--effort") + 1] == expected


@pytest.mark.parametrize("task", ["small", "é" * 8, "é" * 9, "x" * 200_000])
@pytest.mark.parametrize("terminal", ["SUCCESS", "ERROR", None])
def test_antigravity_prompt_transport_preserves_text_and_completion_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, task: str, terminal: str | None,
) -> None:
    monkeypatch.setenv("ORCH_PROMPT_ARG_LIMIT", "16")
    monkeypatch.setattr("saberops.workers.antigravity.Path.home", lambda: tmp_path)
    monkeypatch.setattr("saberops.workers.antigravity.GitManager.get_changed_paths", lambda _: [])
    use_stdin = len(task.encode("utf-8")) > 16
    calls: list[list[str]] = []
    def on_stdout(chunk: str) -> None:
        pass

    def runner(args: list[str], **kwargs: Any) -> ProcessResult:
        calls.append(args)
        assert args[args.index("--model") + 1] == "gemini-3.8-flash-high"
        assert args[args.index("--mode") + 1] == "plan"
        assert "--dangerously-skip-permissions" not in args
        assert args[args.index("--conversation") + 1] == "native_conversation1234"
        assert kwargs["on_stdout"] is on_stdout
        assert kwargs["inherit_env"] is False
        assert "GEMINI_API_KEY" not in kwargs["env"]
        assert "GOOGLE_API_KEY" not in kwargs["env"]
        if use_stdin:
            assert task not in args and "--print" not in args
            assert args[args.index("--input-format") + 1] == "text"
            assert kwargs["input_text"] == task
        else:
            assert args[args.index("--print") + 1] == task
            assert "input_text" not in kwargs
        raw = json.dumps({"event": "result", "result": {"status": terminal,
                                                          "response": "Done"}})
        return ProcessResult(args, 0, raw, "", 0.01)

    adapter = AntigravityAdapter(runner=runner)
    assert adapter.max_prompt_bytes() is None
    assert adapter.prompt_transport() == "stdin"
    expected_transport = "stdin" if use_stdin else "argv"
    assert adapter.prompt_transport_for(len(task.encode("utf-8"))) == expected_transport
    assert Capability.LARGE_PROMPT_STDIN in capability_profile_for(adapter).capabilities
    assert "large_prompt_stdin" in adapter.factual_capabilities
    result = adapter.run(WorkerRequest(
        task, tmp_path, "gemini-3.8-flash-high", read_only=True,
        native_resume_id="native_conversation1234", on_stdout=on_stdout,
        env={"GEMINI_API_KEY": "excluded"},
    ))
    assert len(calls) == 1
    assert result.prompt_bytes == len(task.encode("utf-8"))
    assert result.prompt_transport_used == expected_transport
    expected_status = AttemptStatus.SUCCESS if terminal == "SUCCESS" else AttemptStatus.FAILED
    assert result.status == expected_status


def test_history_export_includes_completion_and_latest_output_after_display_cap(
    tmp_path: Path,
) -> None:
    journal = SessionJournal(tmp_path)
    journal.register("sid", "run", "worker", "attempt", live_identity())
    for value in ("old output", "latest output"):
        journal.append("sid", "output.stdout", {"text": value})
    journal.complete("sid", {"status": "SUCCESS"})
    exported = [json.loads(line) for line in journal.export("sid")]
    assert [row["kind"] for row in exported] == ["output.stdout", "output.stdout", "completion"]
    assert journal.tail("sid", max_bytes=13) == "latest output"


def test_unmanaged_provider_input_holds_execution_before_any_result_is_accepted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controls = SessionControls(tmp_path)
    controls.register("sid", "run", live_identity(), "worker")
    controls.native_id("sid", "ses_native1234")
    journal = SessionJournal(tmp_path)
    journal.register("sid", "run", "worker", "a1", live_identity())
    runtime = AgentSessionRuntime(tmp_path, "sid", controls, journal, lambda _: None)
    runtime.native_id = "ses_native1234"
    runtime.thread = threading.current_thread()
    request = WorkerRequest(
        "original",
        tmp_path,
        "provider/model",
        control_interrupt=lambda: controls.should_interrupt("sid"),
    )
    runtime.request = request
    polls = 0
    aborted = False

    def api(method: str, path: str, body: object = None, **kwargs: Any) -> Any:
        nonlocal polls, aborted
        if path.endswith("/abort"):
            aborted = True
            return True
        if path.endswith("/message"):
            polls += 1
            return (
                []
                if polls == 1
                else [
                    {
                        "info": {"id": "msg_unmanaged", "role": "user"},
                        "parts": [{"type": "text", "text": "external task"}],
                    }
                ]
            )
        return {}

    monkeypatch.setattr(runtime, "api", api)
    monkeypatch.setattr("saberops.session_runtime.GitManager.get_changed_paths", lambda _: [])
    import time

    result = runtime._turn(request, time.monotonic())
    assert result.status == AttemptStatus.FAILED and aborted
    assert controls.mode("sid") == "REQUESTED"
    assert any(event["kind"] == "native.foreign_input" for event in journal.view("sid")["events"])


def test_confirmed_snapshot_seals_late_deltas_and_busy_status(tmp_path: Path) -> None:
    output: list[str] = []
    journal = SessionJournal(tmp_path)
    runtime = AgentSessionRuntime(
        tmp_path, "sid", SessionControls(tmp_path), journal, lambda _: None
    )
    runtime.native_id = "ses_one"
    runtime.request = WorkerRequest("task", tmp_path, "provider/model", on_stdout=output.append)
    runtime.capture_event(
        {"type": "message.part.delta", "properties": {"messageID": "msg_final", "delta": "partial"}}
    )
    runtime.confirm_snapshot(
        [{"info": {"id": "msg_final"}, "parts": [{"type": "text", "text": "Complete response"}]}]
    )
    runtime.capture_event(
        {"type": "message.part.delta", "properties": {"messageID": "msg_final", "delta": "late"}}
    )
    runtime.capture_event({"type": "session.status", "properties": {"status": {"type": "busy"}}})
    assert len(output) == 1
    events = journal.view("sid")["events"]
    assert events[-1]["kind"] == "provider.snapshot"
    frames = runtime.snapshot_events(events[-1]["payload"])
    assert frames[-2]["properties"]["part"]["text"] == "Complete response"
    assert frames[-1]["properties"]["status"]["type"] == "idle"


def test_unconfirmed_abort_cannot_publish_a_finished_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = SessionJournal(tmp_path)
    runtime = AgentSessionRuntime(
        tmp_path, "sid", SessionControls(tmp_path), journal, lambda _: None
    )
    runtime.native_id = "ses_one"
    request = WorkerRequest("task", tmp_path, "provider/model", control_interrupt=lambda: True)
    runtime.request = request

    def api(method: str, path: str, body: object = None, **kwargs: Any) -> Any:
        if path == "/session/status":
            return {"ses_one": {"type": "busy"}}
        return [] if path.endswith("/message") else None

    clock = iter([0, 0, 11])
    monkeypatch.setattr(runtime, "api", api)
    monkeypatch.setattr("saberops.session_runtime.time.monotonic", lambda: next(clock))
    with pytest.raises(SessionRuntimeError, match="did not confirm cancellation"):
        runtime._turn(request, 0)
    assert not runtime.idle_confirmed and runtime.final_cursor == 0


def test_durable_outcome_overrides_stale_active_metadata_without_mutating_it(
    tmp_path: Path,
) -> None:
    sid = "a" * 32
    folder = owner_directory(tmp_path, "run")
    folder.mkdir()
    path = folder / (sid + ".json")
    path.write_text(
        json.dumps(
            {
                "id": sid,
                "state": "ACTIVE",
                "native_attach_ready": True,
                "native_id": "ses_one",
                "owner_identity": live_identity(),
            }
        )
    )
    (folder / (sid + ".log")).write_text("partial output")
    controls = SessionControls(tmp_path)
    controls.register(sid, "run", live_identity(), "worker")
    controls.native_id(sid, "ses_one")
    journal = SessionJournal(tmp_path)
    journal.register(sid, "run", "worker", "a1", live_identity())
    journal.complete(sid, {"status": "SUCCESS", "quiescent": True})
    before = path.read_bytes()
    session = read_sessions(tmp_path, "run")[0]
    assert session["state"] == "SUCCESS"
    assert not session["native_attach_ready"] and not session["can_takeover"]
    assert path.read_bytes() == before


@pytest.mark.parametrize("path", ["/event", "/global/event"])
def test_live_stream_gets_final_snapshot_without_replaying_old_busy(bridge: Any, path: str) -> None:
    runtime, _, _, _ = bridge
    runtime.journal.append(
        "sid",
        "provider.structured",
        {"type": "session.status", "properties": {"status": {"type": "busy"}}},
    )
    frames: list[dict[str, Any]] = []
    failures: list[BaseException] = []

    def read() -> None:
        headers = {
            "Authorization": "Basic "
            + base64.b64encode(("opencode:" + runtime.client_password).encode()).decode()
        }
        req = urllib.request.Request(
            f"http://127.0.0.1:{runtime.proxy.server_port}" + path, headers=headers
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                for line in response:
                    if not line.startswith(b"data:"):
                        continue
                    payload = json.loads(line[5:])
                    frame = payload["payload"] if path == "/global/event" else payload
                    frames.append(frame)
                    if frame["type"] == "session.status":
                        return
        except BaseException as exc:
            failures.append(exc)

    reader = threading.Thread(target=read)
    reader.start()
    deadline = time.monotonic() + 3
    while not runtime.stream_cursors and time.monotonic() < deadline:
        time.sleep(0.01)
    assert runtime.stream_cursors
    runtime.confirm_snapshot(
        [
            {
                "info": {"id": "msg_done", "sessionID": runtime.native_id},
                "parts": [
                    {
                        "id": "prt_done",
                        "messageID": "msg_done",
                        "sessionID": runtime.native_id,
                        "type": "text",
                        "text": "All done",
                    }
                ],
            }
        ]
    )
    assert runtime.close()
    reader.join(5)
    assert not reader.is_alive() and not failures
    assert frames[-1]["properties"]["status"]["type"] == "idle"
    assert frames[-2]["properties"]["part"]["text"] == "All done"
    assert len(frames) == 3


def test_frozen_gate_contract_reaches_repairs_with_correct_prompt_metrics() -> None:
    from saberops.models import ReviewResult, ReviewVerdict
    from saberops.orchestrator import _render_final_repair_task, _render_repair_task
    from saberops.validation_policy import verification_contract

    contract = verification_contract("make gate", True, True, False)
    review = ReviewResult(
        id="review",
        run_id="run",
        provider="fake",
        model="model",
        verdict=ReviewVerdict.BLOCK,
        summary="Fix issue",
        details="Evidence",
    )
    for rendered in (
        _render_repair_task("Build app", 2, "abc", review, run_verification_text=contract),
        _render_final_repair_task("Build app", 2, "abc", [review], run_verification_text=contract),
    ):
        assert "root must have a Makefile with a gate target" in rendered.prompt
        assert "Independent SaberOps review is requested" in rendered.prompt
        assert rendered.projected_prompt_bytes == len(rendered.prompt.encode())
        assert rendered.validation_policy_marker_count == 1
