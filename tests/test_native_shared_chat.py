"""One native provider loop accepts app messages without releasing its session lease."""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from saberops.chat import ChatBusy, ChatCoordinator, ChatStore
from saberops.native_access import (
    claim_client,
    native_argv,
    reserve,
    session_key,
    supports_shared_chat,
    update_client,
)
from saberops.native_handoff import HistoryMirror
from saberops.native_shared_chat import NativeSharedChat
from saberops.ownership import ProcessIdentity
from saberops.project_bootstrap import prepare_project
from saberops.session_control import ControlConflict, SessionControls
from saberops.web import create_app


def native(
    temporary: Path, store: ChatStore | None = None
) -> tuple[NativeSharedChat, dict[str, Any], ChatStore, str]:
    store = store or ChatStore(temporary / "chat.db")
    chat_id = store.create(str(temporary), "Owner chat")
    controls = SessionControls(store.path.parent / "agent-sessions")
    sid = "b" * 32
    identity = ProcessIdentity.for_self()
    controls.register(sid, chat_id, json.dumps(identity.to_dict()), "orchestrator")
    controls.native_id(sid, "native_session123")
    controls.finish(sid, "DONE")
    controls.link_chat(sid, chat_path=store.path, chat_id=chat_id, saved=True)
    data = {
        "id": sid,
        "provider": "anthropic",
        "role": "orchestrator",
        "native_id": "native_session123",
        "model": "sonnet",
        "directory": str(temporary),
        "read_only": True,
        "shared_chat": True,
        "descriptor": str(temporary / "session.handoff"),
    }
    data.update(
        key=session_key(data),
        token=reserve(controls, data),
        controls_root=str(controls.path.parent),
    )
    claim_client(controls, data, identity)
    update_client(controls, data, "ACTIVE", child=identity)
    bridge = NativeSharedChat(controls)
    bridge.ready(data, identity)
    return bridge, data, store, chat_id


def test_queue_serializes_turns_deduplicates_and_preserves_native_lease(tmp_path: Path) -> None:
    bridge, data, store, chat_id = native(tmp_path)
    bridge.submit(data["id"], "first question", "one", chat_id=chat_id)
    bridge.submit(data["id"], "first question", "one", chat_id=chat_id)
    bridge.submit(data["id"], "second question", "two", chat_id=chat_id)
    with pytest.raises(ControlConflict, match="different message"):
        bridge.submit(data["id"], "changed", "one", chat_id=chat_id)
    first = bridge.next(data)
    assert first and first["text"] == "first question"
    assert bridge.next(data) is None
    bridge.reply(data, first["id"], "First answer")
    bridge.reply(data, first["id"], "First answer")
    assert bridge.next(data) is None
    bridge.finish(first["id"])
    second = bridge.next(data)
    assert second and second["text"] == "second question"
    bridge.reply(data, second["id"], "Second answer")
    bridge.finish(second["id"])
    assert [m["text"] for m in store.get(chat_id)["messages"]] == [  # type: ignore[index]
        "first question",
        "second question",
        "First answer",
        "Second answer",
    ]
    with pytest.raises(ControlConflict, match="already"):
        reserve(bridge.controls, data)


def test_channel_scope_stale_owner_pending_close_and_no_automatic_resend(tmp_path: Path) -> None:
    bridge, data, store, chat_id = native(tmp_path)
    with pytest.raises(ControlConflict, match="belong"):
        bridge.submit(data["id"], "hello", "wrong", chat_id="different-chat")
    bridge.submit(data["id"], "a saved question", "saved", chat_id=chat_id)
    message = bridge.next(data)
    assert message
    with pytest.raises(ControlConflict, match="delivered"):
        bridge.reply(data, "foreign-id", "wrong")
    bridge.closed(data, "Disconnected; delivery was not confirmed.")
    assert bridge.channel(data["id"]) is None
    chat = store.get(chat_id)
    assert chat and "Disconnected" in chat["messages"][-1]["text"]
    bridge.ready(data, ProcessIdentity.for_self())
    assert bridge.next(data) is None, "A reconnected channel never replays an uncertain turn"
    stale = dict(data, token="stale-reservation")
    with pytest.raises(ControlConflict, match="owns"):
        bridge.ready(stale, ProcessIdentity.for_self())


def test_concurrent_queue_has_bounded_admission(tmp_path: Path) -> None:
    bridge, data, _, chat_id = native(tmp_path)
    rejected: list[str] = []

    def submit(number: int) -> None:
        try:
            bridge.submit(data["id"], "question " + str(number), str(number), chat_id=chat_id)
        except ControlConflict as exc:
            rejected.append(str(exc))

    threads = [threading.Thread(target=submit, args=(n,)) for n in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(rejected) == 4 and all("full" in reason for reason in rejected)
    channel = bridge.channel(data["id"])
    assert channel and channel["pending"] == 8


def test_timed_out_delivery_is_visible_and_never_replayed(tmp_path: Path) -> None:
    bridge, data, store, chat_id = native(tmp_path)
    bridge.submit(data["id"], "A slow question", "slow", chat_id=chat_id)
    item = bridge.next(data)
    assert item
    with bridge.controls.connect(write=True) as conn:
        conn.execute("UPDATE native_chat_inbox SET updated=0 WHERE id=?", (item["id"],))
    bridge.recover()
    assert bridge.channel(data["id"]) is None
    assert "uncertain" in store.get(chat_id)["messages"][-1]["text"]  # type: ignore[index]
    with pytest.raises(ControlConflict, match="disconnected"):
        bridge.next(data)
    bridge.ready(data, ProcessIdentity.for_self())
    assert bridge.next(data) is None
    with pytest.raises(ControlConflict, match="uncertain outcome"):
        bridge.submit(data["id"], "A slow question", "slow", chat_id=chat_id)


def test_ui_crash_after_work_claim_reports_uncertainty_without_redispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from saberops.ownership import Liveness

    bridge, data, store, chat_id = native(tmp_path)
    bridge.submit(data["id"], "Implement a feature", "work", chat_id=chat_id)
    item = bridge.next(data)
    assert item
    bridge.reply(data, item["id"], "Worker advice", "work")
    assert bridge.claim_reply(item["id"])
    bridge.reply(data, item["id"], "Worker advice", "work")
    bridge.recover()
    assert bridge.channel(data["id"])["pending"] == 1  # type: ignore[index]
    monkeypatch.setattr("saberops.native_shared_chat.probe_liveness", lambda _: Liveness.DEAD)
    bridge.recover()
    bridge.recover()
    assert bridge.replies() == []
    assert not bridge.claim_reply(item["id"])
    messages = store.get(chat_id)["messages"]  # type: ignore[index]
    assert sum("restarted during" in row["text"] for row in messages) == 1


def test_shared_manual_submission_repairs_mirror_after_transient_storage_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge, data, store, chat_id = native(tmp_path)
    original = bridge.controls.sync_chat
    with monkeypatch.context() as patch:
        patch.setattr(bridge.controls, "sync_chat", lambda *args: (_ for _ in ()).throw(OSError()))
        with pytest.raises(OSError):
            bridge.submit(data["id"], "Preserved message", "mirror", chat_id=chat_id)
    bridge.controls.submit(data["id"], "Preserved message", "mirror")
    assert len(store.get(chat_id)["messages"]) == 1  # type: ignore[index]
    item = bridge.next(data)
    assert item
    bridge.reply(data, item["id"], "Preserved reply")
    original(item, "Preserved reply")
    bridge.finish(item["id"])
    assert len(store.get(chat_id)["messages"]) == 2  # type: ignore[index]


def test_native_channel_recipe_keeps_subscription_readonly_tools_and_exact_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, data, _, _ = native(tmp_path)
    monkeypatch.setattr("saberops.native_access.shutil.which", lambda name: "/bin/" + name)
    argv = native_argv(data)
    config = json.loads(argv[argv.index("--mcp-config") + 1])
    assert list(config["mcpServers"]) == ["saberops"]
    assert config["mcpServers"]["saberops"]["args"][-1] == data["descriptor"]
    assert argv[argv.index("--resume") + 1] == data["native_id"]
    assert argv[argv.index("--permission-mode") + 1] == "plan"
    assert "--dangerously-skip-permissions" not in argv
    assert "Write" not in argv[argv.index("--tools") + 1]
    assert json.loads(argv[argv.index("--settings") + 1])["forceLoginMethod"] == "claudeai"


@pytest.mark.parametrize("supported", [True, False])
def test_hidden_channel_flag_is_detected_without_running_a_provider_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    supported: bool,
) -> None:
    binary = tmp_path / "claude"
    binary.write_bytes(
        b"installed CLI" + (b"--dangerously-load-development-channels" if supported else b"")
    )
    monkeypatch.setattr("saberops.native_access.shutil.which", lambda name: str(binary))
    calls: list[list[str]] = []

    def help_only(argv: list[str], **kwargs: Any) -> Any:
        assert argv == [str(binary), "--help"]
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "Hidden flags are omitted", "")

    monkeypatch.setattr("saberops.native_access.subprocess.run", help_only)
    assert supports_shared_chat("anthropic") is supported
    assert calls
    assert not supports_shared_chat("antigravity")


@pytest.mark.parametrize("role", ["worker", "reviewer", "orchestrator"])
def test_only_an_unshared_orchestrator_client_blocks_app_chat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
) -> None:
    store = ChatStore(tmp_path / "chat.db")
    chat_id = store.create(str(tmp_path), "Chat")
    chats = ChatCoordinator(store, cast(Any, object()), cast(Any, object()))
    monkeypatch.setattr(
        "saberops.agent_sessions.read_sessions",
        lambda *args, **kwargs: [{"role": role, "native_client": {"state": "ACTIVE"}}],
    )
    monkeypatch.setattr(
        chats, "_ask", lambda *args, **kwargs: ("A normal reply", "respond", "test")
    )
    if role == "orchestrator":
        with pytest.raises(ChatBusy, match="no connected shared chat channel"):
            chats.submit(chat_id, "What happened?", "request")
    else:
        chats.submit(chat_id, "What happened?", "request")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            chat = store.get(chat_id)
            if chat and not chat["busy"]:
                assert chat["messages"][-1]["text"] == "A normal reply"
                break
            time.sleep(0.01)
        else:
            pytest.fail("Independent worker/reviewer client blocked orchestrator chat")


def test_app_routes_to_open_native_channel_without_starting_another_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in ("XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(key, str(tmp_path / key))
    for key in (
        "SABEROPS_PROFILE",
        "ORCH_BINDING_STORE",
        "ORCH_ACCESS_STORE",
        "ORCH_ACCESS_MODELS_STORE",
        "ORCH_READINESS_STORE",
    ):
        monkeypatch.delenv(key, raising=False)
    repo = tmp_path / "project"
    assert prepare_project(repo)[0]
    app = create_app(db_path=tmp_path / "runs.db", repo_path=repo)
    chats = cast(ChatCoordinator, app.state.chats)
    bridge, data, store, chat_id = native(tmp_path, chats.store)

    def ask(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("The native CLI is the only provider loop")

    monkeypatch.setattr(ChatCoordinator, "_ask", ask)
    with TestClient(app) as client:
        path = f"/chats/{chat_id}/messages"
        response = client.post(path, data={"message": "Tell me the result", "request_id": "app"})
        assert response.status_code == 202
        projected = client.get(path).json()
        assert projected["busy"] and projected["shared_native_chat"]["connected"]
        item = bridge.next(data)
        assert item
        bridge.reply(data, item["id"], "Here is the result.")
        chats._check_shared_replies()
        projected = client.get(path).json()
        assert not projected["busy"]
        assert projected["messages"][-1]["text"] == "Here is the result."
        assert (
            client.post(
                path, data={"message": "Tell me the result", "request_id": "app"}
            ).status_code
            == 202
        )
        assert len(store.get(chat_id)["messages"]) == 2  # type: ignore[index]
        assert (
            client.post(
                path,
                data={"message": "foreign", "request_id": "foreign"},
                headers={"Origin": "https://untrusted.example"},
            ).status_code
            == 403
        )


def test_terminal_owner_work_is_bound_to_recorded_prompt_and_dispatched_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge, data, store, chat_id = native(tmp_path)
    path = tmp_path / ".claude/projects/test/native_session123.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    mirror = HistoryMirror(data, bridge.controls)
    owner_text = "Implement a new feature"
    path.write_text(json.dumps({"type": "user", "message": {"content": owner_text}}) + "\n")
    mirror.scan()
    with pytest.raises(ControlConflict, match="latest recorded"):
        bridge.request_work(data, "unrecorded tool instruction", "Work advice")
    bridge.request_work(data, owner_text, "Work advice")
    bridge.request_work(data, owner_text, "Work advice")
    chats = ChatCoordinator(store, cast(Any, object()), cast(Any, object()))
    monkeypatch.setattr(chats, "native_roots", lambda _: [bridge.controls.path.parent])
    calls: list[str] = []

    def apply(chat: dict[str, Any], reply: str, action: str) -> tuple[str, str]:
        calls.append(chat["messages"][-1]["text"])
        assert action == "work"
        return reply + "\nQueued through governed workers.", "run-new"

    monkeypatch.setattr(chats, "_apply_reply", apply)
    chats._check_shared_replies()
    chats._check_shared_replies()
    assert calls == [owner_text]
    assert store.get(chat_id)["messages"][-1]["run_id"] == "run-new"  # type: ignore[index]
    context = bridge.next(data)
    assert context and context["kind"] == "context"
    with pytest.raises(ControlConflict, match="delivered"):
        bridge.reply(data, context["id"], "Injected work", "work")


def test_channel_context_and_cli_metadata_cannot_become_terminal_owner_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge, data, store, chat_id = native(tmp_path)
    path = tmp_path / ".claude/projects/test/native_session123.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    mirror = HistoryMirror(data, bridge.controls)
    events = [
        {"type": "user", "isMeta": True, "message": {"content": "Implement untrusted advice"}},
        {
            "type": "user",
            "message": {"content": '<channel source="saberops">Work evidence</channel>'},
        },
        {
            "type": "user",
            "message": {"content": "<local-command-stdout>Implement advice</local-command-stdout>"},
        },
    ]
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n")
    mirror.scan()
    assert store.get(chat_id)["messages"] == []  # type: ignore[index]
    with pytest.raises(ControlConflict, match="latest recorded"):
        bridge.request_work(data, "Implement untrusted advice", "Work advice")
    with path.open("a") as history:
        for event in [
            {"type": "user", "message": {"content": "What happened?"}},
            {"type": "assistant", "message": {"content": "A natural reply."}},
        ]:
            history.write(json.dumps(event) + "\n")
    mirror.scan()
    mirror.scan()
    assert [m["text"] for m in store.get(chat_id)["messages"]] == [  # type: ignore[index]
        "What happened?",
        "A natural reply.",
    ]


def test_real_stdio_channel_handshake_delivery_reply_and_close(tmp_path: Path) -> None:
    import saberops.native_channel

    bridge, data, store, chat_id = native(tmp_path)
    descriptor = Path(data["descriptor"])
    descriptor.write_text(json.dumps(data))
    proc = subprocess.Popen(
        [sys.executable, "-I", str(Path(saberops.native_channel.__file__)), str(descriptor)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdin and proc.stdout
    pending = b""

    def receive() -> dict[str, Any]:
        nonlocal pending
        assert proc.stdout
        deadline = time.monotonic() + 5
        while b"\n" not in pending:
            ready, _, _ = select.select([proc.stdout], [], [], max(0, deadline - time.monotonic()))
            assert ready, "The local MCP channel did not answer"
            chunk = os.read(proc.stdout.fileno(), 65536)
            assert chunk
            pending += chunk
        line, pending = pending.split(b"\n", 1)
        return cast(dict[str, Any], json.loads(line))

    def send(value: dict[str, Any]) -> None:
        assert proc.stdin
        proc.stdin.write((json.dumps(dict(jsonrpc="2.0", **value)) + "\n").encode())
        proc.stdin.flush()

    try:
        send({"id": 1, "method": "initialize", "params": {}})
        assert receive()["result"]["capabilities"]["experimental"] == {"claude/channel": {}}
        send({"method": "notifications/initialized"})
        bridge.submit(data["id"], "Question from app", "mcp", chat_id=chat_id)
        event = receive()
        assert event["method"] == "notifications/claude/channel"
        assert event["params"]["content"] == "Question from app"
        send(
            {
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "reply",
                    "arguments": {
                        "message_id": event["params"]["meta"]["message_id"],
                        "text": "Answer from native CLI",
                    },
                },
            }
        )
        assert not receive()["result"].get("isError")
        assert store.get(chat_id)["messages"][-1]["text"] == "Answer from native CLI"  # type: ignore[index]
        proc.stdin.close()
        assert proc.wait(timeout=5) == 0
        assert bridge.channel(data["id"]) is None
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)


def test_shared_chat_mode_uses_original_turn_permission_without_empty_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from saberops.runtime import ProjectRuntimeManager
    from saberops.workers.registry import AdapterRegistry

    bridge, data, store, chat_id = native(tmp_path)
    chats = ChatCoordinator(store, cast(ProjectRuntimeManager, None), AdapterRegistry.default())

    def apply(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Discussion must not enter worker dispatch")

    monkeypatch.setattr(chats, "_apply_reply", apply)
    chats.submit(chat_id, "Implement an app", "discuss", mode="chat")
    item = bridge.next(data)
    assert item
    # A later queued Work turn must not promote this earlier Chat turn.
    chats.submit(chat_id, "Implement the approved brief", "implement", mode="work")
    bridge.reply(data, item["id"], "Here is the proposed implementation.", "work")
    chats._check_shared_replies()
    rows = store.get(chat_id)["messages"]  # type: ignore[index]
    assert len(rows) == 3  # Two saved user messages and one native response.
    assert rows[-1]["text"] == "Here is the proposed implementation."
    assert all(row["text"] for row in rows)
    assert store.pending_runs() == []


def test_native_attachments_preserve_owner_prose_and_chat_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge, data, store, chat_id = native(tmp_path)
    chats = ChatCoordinator(store, cast(Any, None), cast(Any, None))
    monkeypatch.setattr(chats, "shared_native_chat", lambda _: (bridge, data))
    monkeypatch.setattr(chats, "native_roots", lambda _: [bridge.controls.path.parent])
    uploaded = store.attachments.save("brief.txt", b"Reference from owner")
    chats.submit(chat_id, "Discuss this file", "file-turn", mode="chat",
                 attachment_ids=(uploaded["id"],))
    item = bridge.next(data)
    assert item and "Reference from owner" in item["text"]
    assert str(store.attachments.path(uploaded["id"])) in item["text"]
    messages = store.get(chat_id)["messages"]  # type: ignore[index]
    assert messages[0]["text"] == "Discuss this file"
    assert messages[0]["attachments"] == [uploaded]
    bridge.reply(data, item["id"], "Let's discuss the file.", "work")
    chats._check_shared_replies()
    assert store.pending_runs() == []
    assert store.get(chat_id)["messages"][0]["attachments"] == [uploaded]  # type: ignore[index]


def test_rejected_native_attachment_turn_keeps_files_removable(tmp_path: Path) -> None:
    bridge, data, store, chat_id = native(tmp_path)
    chats = ChatCoordinator(store, cast(Any, None), cast(Any, None))
    uploaded = store.attachments.save("brief.txt", b"Reference")
    with pytest.raises(ValueError, match="Shorten"):
        chats.submit(chat_id, "x" * 31999, "oversized", mode="chat",
                     attachment_ids=(uploaded["id"],))
    assert store.attachments.remove(uploaded["id"])
    for n in range(8):
        bridge.submit(data["id"], "Queued question", "queued-" + str(n), chat_id=chat_id)
    another = store.attachments.save("other.txt", b"Still a draft")
    with pytest.raises(ChatBusy, match="full"):
        chats.submit(chat_id, "Read file", "queue-full", mode="chat",
                     attachment_ids=(another["id"],))
    assert store.attachments.remove(another["id"])
