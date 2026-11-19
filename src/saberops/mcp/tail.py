"""Read-only incremental progress tail for one job (Phase 1 Wave 3 F3).

``tail_job`` lets a planner in a second session read a running worker's
progress without touching it.  Two durable records the engine already writes
are exposed through one cursor:

* the run's authoritative **event log** (``db.get_events`` -> :class:`OrchEvent`),
  the same stream the owner's web page follows; and
* the run's **PTY transcript** (``transcripts/<run_id>.ansi``), which the
  detached execution owner writes live -- worker stdout/stderr lines are
  mirrored there by ``Orchestrator._worker_activity_hooks``.

Nothing here writes, waits, or takes planning ownership: the cursor is
computed from what is already on disk, so a read-only client can follow a job
while another session holds the planning lease.

Cursor
------

``after_sequence`` is the opaque token a previous call returned as
``next_sequence``: ````<run_id>:<event_id>:<transcript_offset>``.  A plain
integer is also accepted and means "event ``N``, transcript offset 0", which
is what a fresh client sends.  The run id travels inside the cursor so a retry
that creates a new run resets both positions instead of silently skipping the
new run's early events (event ids are global to the store, transcripts are
per run).

Presentation only
-----------------

Transcript bytes are returned as text with terminal control sequences removed
so a model can read them.  The file itself is opened read-only and never
modified; ``size`` and ``offset`` always refer to raw file bytes, so a client
can resume from ``next_sequence`` even after ANSI stripping.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_TAIL_BYTES",
    "MAX_TAIL_BYTES",
    "MAX_TAIL_EVENTS",
    "MIN_TAIL_BYTES",
    "TailCursor",
    "parse_cursor",
    "strip_terminal_control",
    "tail_run",
]

#: Default response budget for the ``tail_job`` content (events + transcript).
DEFAULT_TAIL_BYTES = 65_536

#: Smallest accepted ``max_bytes`` (keeps one useful chunk).
MIN_TAIL_BYTES = 512

#: Largest accepted ``max_bytes``; mirrors the evidence read cap (1 MiB).
MAX_TAIL_BYTES = 1_048_576

#: Events examined per call before the caller must continue from the cursor.
MAX_TAIL_EVENTS = 200

#: CSI sequences (colors, cursor movement), OSC sequences (titles, links) and
#: the remaining short two-byte escapes.
_ANSI_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ANSI_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_ANSI_OTHER = re.compile(r"\x1b[@-Z\\-_]")
#: C0 controls except tab and newline, plus DEL.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


@dataclass(frozen=True)
class TailCursor:
    """One position in a run's event log and transcript."""

    run_id: str | None = None
    event_id: int = 0
    transcript_offset: int = 0

    def encode(self) -> str:
        return f"{self.run_id or ''}:{self.event_id}:{self.transcript_offset}"


def parse_cursor(raw: object) -> TailCursor:
    """Parse ``after_sequence``; a missing/empty value is the start position.

    Accepts the opaque token from a previous call and a plain event id.  A
    malformed token is refused rather than guessed: silently restarting the
    tail would replay old output as if it were new.
    """
    from saberops.mcp.errors import InvalidParamsError

    if raw is None or raw == "":
        return TailCursor()
    if isinstance(raw, bool):
        raise InvalidParamsError("after_sequence must be an integer or a cursor string")
    if isinstance(raw, int):
        if raw < 0:
            raise InvalidParamsError("after_sequence must not be negative")
        return TailCursor(event_id=raw)
    if not isinstance(raw, str):
        raise InvalidParamsError("after_sequence must be an integer or a cursor string")
    head, sep, offset_text = raw.rpartition(":")
    run_id, sep2, event_text = head.rpartition(":")
    if not sep or not sep2:
        raise InvalidParamsError(
            "after_sequence must be an event id or a cursor returned as next_sequence"
        )
    try:
        event_id = int(event_text)
        transcript_offset = int(offset_text)
    except ValueError as exc:
        raise InvalidParamsError(
            "after_sequence cursor must carry integer event and offset positions"
        ) from exc
    if event_id < 0 or transcript_offset < 0:
        raise InvalidParamsError("after_sequence cursor positions must not be negative")
    return TailCursor(
        run_id=run_id or None,
        event_id=event_id,
        transcript_offset=transcript_offset,
    )


def strip_terminal_control(text: str) -> str:
    """Remove terminal escape/control sequences for human and model reading."""
    text = _ANSI_OSC.sub("", text)
    text = _ANSI_CSI.sub("", text)
    text = _ANSI_OTHER.sub("", text)
    return _CONTROL.sub("", text)


def _split_utf8(data: bytes) -> tuple[bytes, int]:
    """Return ``(complete, held_back)`` with no partial UTF-8 character split."""
    for back in range(1, min(4, len(data)) + 1):
        lead = data[-back]
        if lead & 0xC0 == 0x80:
            continue  # continuation byte: keep looking for the lead byte
        if lead & 0x80 == 0:
            width = 1
        elif lead & 0xE0 == 0xC0:
            width = 2
        elif lead & 0xF0 == 0xE0:
            width = 3
        elif lead & 0xF8 == 0xF0:
            width = 4
        else:
            return data[:-back], back
        if width == back:
            return data, 0
        return data[:-back], back
    return data, 0


def _transcript_size(path: Path) -> int | None:
    """Raw transcript size, or ``None`` when the run has no transcript yet."""
    try:
        return int(path.stat().st_size)
    except OSError:
        return None


def _read_transcript(path: Path, *, offset: int, budget: int) -> dict[str, Any]:
    """Read at most ``budget`` bytes from ``offset`` without changing anything."""
    chunk: dict[str, Any] = {
        "path": str(path),
        "exists": False,
        "offset": offset,
        "next_offset": offset,
        "size": 0,
        "text": "",
        "truncated": False,
        "reset": False,
        "partial_line": False,
        "updated_at": None,
    }
    try:
        stat = path.stat()
    except OSError:
        return chunk
    chunk["exists"] = True
    size = int(stat.st_size)
    chunk["size"] = size
    chunk["updated_at"] = datetime.fromtimestamp(stat.st_mtime, UTC).isoformat()
    if offset > size:
        # The file was rotated or replaced: restart rather than silently
        # returning nothing from a position that no longer exists.
        offset = 0
        chunk["offset"] = 0
        chunk["reset"] = True
    if budget <= 0 or offset >= size:
        chunk["next_offset"] = offset
        return chunk
    try:
        with open(path, "rb") as handle:
            handle.seek(offset)
            raw = handle.read(budget + 1)
    except OSError:
        return chunk
    more = len(raw) > budget
    raw = raw[:budget]
    if more or offset + len(raw) < size:
        # Prefer whole lines; a single overlong line is still returned so the
        # cursor always advances.
        newline = raw.rfind(b"\n")
        if newline >= 0:
            raw = raw[: newline + 1]
        else:
            raw, _held = _split_utf8(raw)
            chunk["partial_line"] = True
    chunk["next_offset"] = offset + len(raw)
    chunk["text"] = strip_terminal_control(raw.decode("utf-8", errors="replace"))
    chunk["truncated"] = chunk["next_offset"] < size
    return chunk


def tail_run(
    db: Any,
    *,
    run_id: str,
    cursor: TailCursor,
    max_bytes: int,
    max_events: int = MAX_TAIL_EVENTS,
) -> dict[str, Any]:
    """Collect the tail after ``cursor`` for one run, bounded by ``max_bytes``.

    The event log is read first (structural progress: attempts, worker start,
    activity, stalls, reviews) and the transcript fills the remainder, so a
    small budget still yields the newest terminal output after the events
    caught up.
    """
    from saberops.terminal import get_transcript_path

    run_changed = cursor.run_id is not None and cursor.run_id != run_id
    start = TailCursor(run_id=run_id) if run_changed else cursor

    max_bytes = max(MIN_TAIL_BYTES, min(int(max_bytes), MAX_TAIL_BYTES))
    transcript_path = get_transcript_path(db, run_id)
    # Live output keeps a guaranteed share of the budget while it is pending so
    # one large event payload can never starve the tail the planner is reading.
    size = _transcript_size(transcript_path)
    pending_output = size is not None and start.transcript_offset < size
    transcript_budget = max_bytes // 2 if pending_output else max_bytes
    transcript = _read_transcript(
        transcript_path,
        offset=start.transcript_offset,
        budget=transcript_budget,
    )
    event_budget = max(0, max_bytes - len(transcript["text"].encode("utf-8")))

    events: list[dict[str, Any]] = []
    event_bytes = 0
    next_event_id = start.event_id
    events_exhausted = True
    for event in db.get_events(run_id, after_id=start.event_id, limit=max_events + 1):
        if len(events) >= max_events:
            events_exhausted = False
            break
        rendered = {
            "id": event.id,
            "type": event.event_type,
            "attempt_id": event.attempt_id,
            "at": event.created_at,
            "payload": event.payload,
        }
        cost = len(json.dumps(rendered, sort_keys=True, default=str).encode("utf-8"))
        # Always return the first pending event so the cursor always advances.
        if events and event_bytes + cost > event_budget:
            events_exhausted = False
            break
        events.append(rendered)
        event_bytes += cost
        next_event_id = event.id

    unread_transcript = int(transcript["next_offset"]) < int(transcript["size"])
    has_more = (not events_exhausted) or unread_transcript
    return {
        "run_id": run_id,
        "run_changed": run_changed,
        "next_sequence": TailCursor(
            run_id=run_id,
            event_id=next_event_id,
            transcript_offset=int(transcript["next_offset"]),
        ).encode(),
        "events": events,
        "transcript": transcript,
        "has_more": has_more,
        "max_bytes": max_bytes,
    }
