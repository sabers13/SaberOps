"""Discussion and work use durable identities without losing original messages."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from saberops.chat import ChatBusy, ChatCoordinator, ChatStore
from saberops.models import Run, RunStatus, Tier
from saberops.navigation import project_folders
from saberops.runtime import ProjectRuntimeManager


def run(repo: str, identity: str) -> Run:
    return Run(
        id=identity,
        task="Build a separate task",
        target_repo=repo,
        base_commit="0" * 40,
        status=RunStatus.COMPLETED,
        tier=Tier.T1,
        gate_command="true",
        training_allowed=False,
        created_at="2026-10-01T10:00:00+00:00",
    )


def test_promotion_snapshot_idempotency_files_and_original_authority(tmp_path: Path) -> None:
    store = ChatStore(tmp_path / "chats.db")
    source = store.create("/repo", "Discuss a task", kind="chat")
    file = store.attachments.save("brief.txt", b"Original discussion file")
    token = store.claim(
        source, "Explain the design", "initial", mode="chat", attachment_ids=(file["id"],)
    )
    assert token
    store.finish(source, token, "Design advice")
    before = store.get(source)
    work = store.promote(source, "Implement only the approved task", "fork")
    assert (
        work != source and store.promote(source, "Implement only the approved task", "fork") == work
    )
    child = store.get(work)
    assert child and child["kind"] == "work" and child["source_chat_id"] == source
    assert child["source_discussion"][0]["attachments"] == [file]
    assert child["messages"] == []
    assert store.get(source)["messages"] == before["messages"]  # type: ignore[index]
    with pytest.raises(ValueError, match="different work brief"):
        store.promote(source, "A different task", "fork")
    with pytest.raises(ValueError, match="attachment"):
        store.promote(source, "Bad file", "bad", ("file_" + "0" * 32,))
    chats = ChatCoordinator(store, cast(Any, None), cast(Any, None))
    with pytest.raises(ValueError, match="Turn into work"):
        chats.submit(source, "Implement via old thread", "wrong", mode="work")
    restarted = ChatStore(store.path).get(work)
    assert restarted and restarted["source_discussion"] == child["source_discussion"]
    assert not store.attachments.remove(file["id"])


def test_finished_runs_get_distinct_threads_with_provenance_and_no_history_loss(
    tmp_path: Path,
) -> None:
    store = ChatStore(tmp_path / "chats.db")
    source = store.create("/repo", "Original shared discussion")
    token = store.claim(source, "Original objective", "initial")
    assert token
    store.finish(source, token, "Result A", run_id="run-A")
    token = store.claim(source, "A second task", "followup")
    assert token
    store.finish(source, token, "Result B", run_id="run-B")
    store.record_manual(
        source, "native-old", "Worker terminal input", "Worker output", role="worker"
    )
    store.link_run(source, "run-A")
    store.link_run(source, "run-B")
    before = store.get(source)
    first = store.separate_run(run("/repo", "run-A"), "Saved A")
    second = store.separate_run(replace(run("/repo", "run-B"), task="Build second task"), "Saved B")
    assert first != second and source not in (first, second)
    assert store.for_run("run-A") == first and store.for_run("run-B") == second
    assert store.separate_run(run("/repo", "run-A"), "Ignored duplicate") == first
    assert store.get(source)["messages"] == before["messages"]  # type: ignore[index]
    first_chat, second_chat = store.get(first), store.get(second)
    assert first_chat and second_chat
    assert [m["text"] for m in first_chat["messages"]] == ["Build a separate task", "Result A"]
    assert [m["text"] for m in second_chat["messages"]] == ["Build second task", "Result B"]
    visible = store.presentation(store.get(source))  # type: ignore[arg-type]
    assert [m["text"] for m in visible["messages"]] == ["Original objective", "A second task"]
    with store.connect() as conn:
        assert dict(conn.execute("SELECT run_id,source_chat_id FROM run_thread_origins")) == {
            "run-A": source,
            "run-B": source,
        }
    manager = ProjectRuntimeManager(pinned_db_path=tmp_path / "runs.db")
    manager.owner_db.create_run(run("/repo", "run-A"))
    manager.owner_db.create_run(run("/repo", "run-B"))
    groups = project_folders(manager, store, ["/repo"], "/repo")
    assert len(groups[0]["chats"]) == 1 and len(groups[0]["work"]) == 2
    assert len(groups[0]["entries"]) == 3, "Linked work conversations must not duplicate run rows"
    manager.shutdown()


def test_busy_thread_is_never_split_or_replayed(tmp_path: Path) -> None:
    store = ChatStore(tmp_path / "chats.db")
    source = store.create("/repo", "Pending chat")
    store.claim(source, "Pending reply", "pending")
    store.link_run(source, "run-A")
    with pytest.raises(ChatBusy):
        store.promote(source, "Implement", "fork")
    assert store.separate_run(run("/repo", "run-A"), "Unreachable") == source
    assert store.for_run("run-A") == source and store.get(source)["busy"]  # type: ignore[index]
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 1


def test_startup_separates_finished_history_once_and_keeps_live_owners(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(name, str(tmp_path / name))
    monkeypatch.delenv("SABEROPS_PROFILE", raising=False)
    store = ChatStore(tmp_path / "chats.db")
    source = store.create("/repo", "Finished tasks")
    idle = store.create("/repo", "Standalone chat")
    active = store.create("/repo", "Native owner")
    running = store.create("/repo", "Running owner")
    manager = ProjectRuntimeManager(pinned_db_path=tmp_path / "runs.db")
    for identity, chat_id in (("run-A", source), ("run-B", source), ("run-live", active)):
        manager.owner_db.create_run(run("/repo", identity))
        store.link_run(chat_id, identity)
    manager.owner_db.create_run(run("/repo", "run-finished-sibling"))
    manager.owner_db.create_run(replace(run("/repo", "run-active"), status=RunStatus.RUNNING))
    store.link_run(running, "run-finished-sibling")
    store.link_run(running, "run-active")
    coordinator = ChatCoordinator(store, manager, cast(Any, None))
    monkeypatch.setattr(
        coordinator,
        "shared_native_chat",
        lambda identity: (object(), {}) if identity == active else None,
    )
    coordinator.separate_history()
    assert store.for_run("run-A") != source and store.for_run("run-B") != source
    assert store.for_run("run-A") != store.for_run("run-B")
    assert store.for_run("run-live") == active
    assert store.for_run("run-finished-sibling") == running
    assert store.get(running)["kind"] == "legacy"  # type: ignore[index]
    assert store.get(idle)["kind"] == "chat"  # type: ignore[index]
    with store.connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
    coordinator.separate_history()
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == count
    assert store.get(active)["kind"] == "legacy"  # type: ignore[index]
    assert store.get(source)["work_children"]  # type: ignore[index]
    manager.shutdown()
