"""Bounded, read-only chat previews from provider events; no fabricated thoughts."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from saberops.session_journal import SessionJournal

MAX_PREVIEW = 32000


def reply_preview(text: str) -> str:
    """Remove the orchestrator JSON envelope, including incomplete escaped strings."""
    match = re.search(r'"reply"\s*:\s*"', text)
    if match:
        raw = text[match.end():]
        # Decode only the reply string. Never expose action fields as user prose.
        for end in range(len(raw), max(-1, len(raw) - 8), -1):
            try:
                value, _ = json.JSONDecoder().raw_decode('"' + raw[:end] + '"')
                return str(value)[:MAX_PREVIEW]
            except ValueError:
                continue
        return ""
    stripped = text.lstrip()
    if stripped.startswith(("{", "```json")):
        return ""
    return text[:MAX_PREVIEW]


def chat_progress(root: Path, chat: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"text": "", "thinking": "", "activity": [],
                              "elapsed": 0, "model": "", "session_id": None}
    if not chat.get("busy") or not chat.get("started"):
        return result
    import time

    result["elapsed"] = max(0, int(time.time() - float(chat["started"])))
    journal = SessionJournal(root)
    if not journal.path.is_file():
        return result
    try:
        with journal.connect() as conn:
            session = conn.execute(
                "SELECT e.session_id FROM events e JOIN sessions s ON e.session_id=s.id "
                "WHERE s.owner=? AND e.created>=? ORDER BY e.sequence DESC LIMIT 1",
                (chat["id"], chat["started"]),
            ).fetchone()
            rows = list(conn.execute(
                "SELECT kind,payload,session_id FROM events WHERE session_id=? AND created>=? "
                "ORDER BY sequence LIMIT 8000",
                (session[0], chat["started"]),
            )) if session else []
    except sqlite3.Error:
        return result
    if not rows:
        return result
    sid = rows[0]["session_id"]
    result["session_id"] = sid
    text = ""
    thinking = ""
    activity: list[str] = []

    def note(label: str) -> None:
        if label and label not in activity:
            activity.append(label[:240])

    for row in rows:
        if row["session_id"] != sid:
            continue
        try:
            payload = json.loads(row["payload"])
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        kind = row["kind"]
        if kind == "assignment.started":
            result["model"] = str(payload.get("provider", "")) + " / " + str(
                payload.get("model", "")
            )
            note("Model selected: " + result["model"])
        elif kind == "process.started":
            note("Provider connected")
        # Whole-line callbacks are recorded once as structured events. Raw stdout/stderr
        # is deliberately never copied into the conversation.
        elif kind == "provider.structured":
            event_type = payload.get("type", "")
            if event_type == "stream_event":
                event = payload.get("event", {})
                if not isinstance(event, dict):
                    continue
                if event.get("type") == "content_block_start":
                    block = event.get("content_block", {})
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        note("Using " + str(block.get("name", "tool")))
                elif event.get("type") == "content_block_delta":
                    delta = event.get("delta", {})
                    if not isinstance(delta, dict):
                        continue
                    if delta.get("type") == "text_delta":
                        text = (text + str(delta.get("text", "")))[:MAX_PREVIEW * 2]
                    elif delta.get("type") == "thinking_delta":
                        thinking = (thinking + str(delta.get("thinking", "")))[:MAX_PREVIEW]
            elif event_type == "assistant":
                message = payload.get("message", {})
                if not isinstance(message, dict):
                    continue
                content = message.get("content", [])
                if not isinstance(content, list):
                    continue
                content = [block for block in content if isinstance(block, dict)]
                full = "".join(str(b.get("text", "")) for b in content
                               if b.get("type") == "text")
                if full:
                    text = full[:MAX_PREVIEW * 2]
                for block in content:
                    if block.get("type") == "tool_use":
                        note("Using " + str(block.get("name", "tool")))
                    elif block.get("type") == "thinking":
                        thinking = str(block.get("thinking", ""))[:MAX_PREVIEW]
            elif event_type in ("item.started", "item.updated", "item.completed"):
                item = payload.get("item", {})
                if not isinstance(item, dict):
                    continue
                item_type = item.get("type", "")
                if item_type == "agent_message":
                    text = str(item.get("text", ""))[:MAX_PREVIEW * 2]
                elif item_type == "reasoning":
                    thinking = str(item.get("text", ""))[:MAX_PREVIEW]
                elif item_type == "command_execution":
                    note("Command: " + str(item.get("command", "")))
                elif item_type == "mcp_tool_call":
                    note("Using " + str(item.get("tool", "tool")))
            elif event_type == "text":  # Direct API adapter's reported response.
                text = str(payload.get("text", ""))[:MAX_PREVIEW * 2]
            elif event_type == "tool":
                note("Using " + str(payload.get("name", "tool")))
            elif event_type == "result" and payload.get("success") is True:
                text = str(payload.get("text", ""))[:MAX_PREVIEW * 2]
            elif event_type in ("thread.started", "message_start", "system"):
                note("Provider session started")
    result.update(text=reply_preview(text), thinking=thinking, activity=activity[-12:])
    return result
