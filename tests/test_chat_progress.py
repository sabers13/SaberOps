"""Live previews stay factual, bounded and separate from dispatch authority."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from saberops.agent_sessions import observe_invocation
from saberops.chat_progress import chat_progress, reply_preview
from saberops.models import AttemptStatus, WorkerRequest, WorkerResult
from saberops.session_journal import SessionJournal


@pytest.mark.parametrize(("raw", "expected"), [
    ('{"reply":"Hello', 'Hello'),
    ('{"reply":"Hello\\nworld","action":"work"}', 'Hello\nworld'),
    ('{"reply":"An escaped \\"quote\\"', 'An escaped "quote"'),
    ('{"reply":"Incomplete unicode \\u00', 'Incomplete unicode '),
    ('{"action":"work"', ''),
    ('Plain response', 'Plain response'),
])
def test_json_envelope_never_leaks_into_partial_reply(raw: str, expected: str) -> None:
    assert reply_preview(raw) == expected


def test_progress_reads_only_current_turn_provider_events(tmp_path: Path) -> None:
    journal = SessionJournal(tmp_path)
    started = time.time()
    journal.register("session", "chat-1", "orchestrator", "chat-1", None)
    journal.append("session", "assignment.started", {"provider": "claude", "model": "exact"})
    journal.append("session", "provider.structured", {"type": "stream_event", "event": {
        "type": "content_block_delta", "delta": {"type": "thinking_delta",
        "thinking": "Reported reasoning summary"}
    }})
    for text in ('{"reply":"Hello ', 'world'):
        journal.append("session", "provider.structured", {"type": "stream_event", "event": {
            "type": "content_block_delta", "delta": {"type": "text_delta", "text": text}
        }})
    journal.append("session", "output.stderr", {"text": "Private transport credential"})
    journal.register("other", "different-chat", "orchestrator", "different-chat", None)
    journal.append("other", "provider.structured", {"type": "text", "text": "Other owner text"})
    before = journal.latest_sequence("session")
    chat: dict[str, Any] = {"id": "chat-1", "busy": True, "started": started}
    progress = chat_progress(tmp_path, chat)
    assert progress["text"] == "Hello world"
    assert progress["thinking"] == "Reported reasoning summary"
    assert progress["model"] == "claude / exact"
    assert "credential" not in json.dumps(progress) and "Other owner" not in json.dumps(progress)
    assert journal.latest_sequence("session") == before
    chat["started"] = time.time() + 1
    assert chat_progress(tmp_path, chat)["text"] == ""
    chat["busy"] = False
    assert chat_progress(tmp_path, chat)["thinking"] == ""


def test_stripped_json_lines_are_streamed_before_invocation_completes(tmp_path: Path) -> None:
    started = time.time()
    request = WorkerRequest(task="hello", worktree_path=tmp_path, model="exact",
                            timeout_seconds=10, read_only=True)
    observed: list[str] = []

    def invoke(current: WorkerRequest) -> WorkerResult:
        assert current.on_stdout is not None
        current.on_stdout("Provider startup notice")
        current.on_stdout(json.dumps({"type": "item.updated", "item": {
            "type": "agent_message", "text": '{"reply":"Already visible'
        }}))
        # Process callbacks omit the newline. The preview must appear before return.
        preview = chat_progress(tmp_path / "sessions", {
            "id": "chat-1", "busy": True, "started": started,
        })
        observed.append(preview["text"])
        return WorkerResult(provider="fake", model="exact", status=AttemptStatus.SUCCESS,
                            summary="done", stderr="",
                            stdout='{"reply":"Already visible","action":"respond"}')

    observe_invocation(invoke, request, root=tmp_path / "sessions", owner="chat-1",
                       role="orchestrator", provider="fake", association="chat-1")
    assert observed == ["Already visible"]
