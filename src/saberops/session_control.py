"""Durable single-writer manual control for captured CLI invocations.

The execution owner terminates its own child and confirms extinction before
acknowledging MANUAL. Web/terminal clients enqueue messages, never signal PIDs
or execute arbitrary commands. A dead owner cannot be resumed automatically.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from saberops.ownership import Liveness, ProcessIdentity, probe_liveness


class ManualControlStopped(RuntimeError):
    """Manual control ended unsafely; terminalize instead of automatic failover."""


class ControlConflict(ValueError):
    pass


class SessionControls:
    def __init__(self, root: Path) -> None:
        self.path = root.resolve() / "session-control.db"

    @contextmanager
    def connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            conn = sqlite3.connect(self.path, timeout=5)
            self.path.chmod(0o600)
        else:
            conn = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def register(self, sid: str, owner: str, identity: str | None, role: str) -> None:
        with self.connect(write=True) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS controls (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, identity TEXT, role TEXT NOT NULL,
                    mode TEXT NOT NULL, native_id TEXT, chat_path TEXT, chat_id TEXT,
                    interrupt_turn INTEGER DEFAULT 0, updated REAL NOT NULL, error TEXT);
                CREATE TABLE IF NOT EXISTS control_messages (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, text TEXT NOT NULL,
                    state TEXT NOT NULL, response TEXT, created REAL NOT NULL);
            """)
            conn.execute(
                "INSERT INTO controls (id,owner,identity,role,mode,updated) VALUES (?,?,?,?,?,?)",
                (sid, owner, identity, role, "ACTIVE", time.time()),
            )

    def get(self, sid: str, *, messages: bool = False) -> dict[str, Any] | None:
        if not self.path.is_file():
            return None
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM controls WHERE id=?", (sid,)).fetchone()
            if row is None:
                return None
            data = dict(row)
            if data["mode"] in ("ACTIVE", "REQUESTED", "MANUAL", "RESUMING"):
                live = probe_liveness(ProcessIdentity.from_json(data["identity"]))
                if live != Liveness.LIVE:
                    data["mode"] = "INTERRUPTED" if live == Liveness.DEAD else "UNKNOWN"
            if messages:
                data["messages"] = [
                    dict(m)
                    for m in conn.execute(
                        "SELECT * FROM control_messages WHERE session_id=? "
                        "ORDER BY rowid DESC LIMIT 20",
                        (sid,),
                    )
                ][::-1]

            return data

    def _require_live(self, sid: str, modes: tuple[str, ...]) -> dict[str, Any]:
        data = self.get(sid)
        if data is None or data["mode"] not in modes:
            raise ControlConflict("This session is not available for that control action.")
        return data

    def native_id(self, sid: str, native_id: str) -> None:
        with self.connect(write=True) as conn:
            conn.execute(
                "UPDATE controls SET native_id=?,updated=? WHERE id=?",
                (native_id, time.time(), sid),
            )

    def takeover(self, sid: str, *, chat_path: Path, chat_id: str) -> None:
        data = self._require_live(sid, ("ACTIVE",))
        if not data["native_id"]:
            raise ControlConflict("Waiting for the provider to report a resumable session.")
        with self.connect(write=True) as conn:
            count = conn.execute(
                "UPDATE controls SET mode=?,chat_path=?,chat_id=?,updated=? WHERE id=? AND mode=?",
                ("REQUESTED", str(chat_path), chat_id, time.time(), sid, "ACTIVE"),
            )
            if count.rowcount != 1:
                raise ControlConflict(
                    "Control changed while requesting takeover; refresh and retry."
                )

    def mode(self, sid: str) -> str:
        with self.connect() as conn:
            row = conn.execute("SELECT mode FROM controls WHERE id=?", (sid,)).fetchone()
            return str(row["mode"]) if row else "FAILED"

    def complete_if_active(self, sid: str) -> bool:
        with self.connect(write=True) as conn:
            row = conn.execute(
                "UPDATE controls SET mode=?,updated=? WHERE id=? AND mode=?",
                ("DONE", time.time(), sid, "ACTIVE"),
            )
            return row.rowcount == 1

    def hold(self, sid: str, reason: str) -> None:
        """Unexpected provider input fences automation before another turn."""
        self._require_live(sid, ("ACTIVE", "REQUESTED", "MANUAL"))
        with self.connect(write=True) as conn:
            changed = conn.execute(
                "UPDATE controls SET mode=CASE WHEN mode='ACTIVE' THEN 'REQUESTED' "
                "ELSE mode END,interrupt_turn=CASE WHEN mode='MANUAL' THEN 1 "
                "ELSE interrupt_turn END,error=?,updated=? WHERE id=? "
                "AND mode IN ('ACTIVE','REQUESTED','MANUAL')", (reason, time.time(), sid))
            if changed.rowcount != 1:
                raise ControlConflict("Control changed while pausing the native session.")

    def finish(self, sid: str, mode: str, error: str = "") -> None:
        cancelled: list[dict[str, Any]] = []
        with self.connect(write=True) as conn:
            conn.execute(
                "UPDATE controls SET mode=?,error=?,updated=? WHERE id=?",
                (mode, error, time.time(), sid),
            )
            if mode in ("DONE", "FAILED", "EXPIRED"):
                cancelled = [
                    dict(m)
                    for m in conn.execute(
                        "SELECT * FROM control_messages WHERE session_id=? AND state IN (?,?)",
                        (sid, "QUEUED", "RUNNING"),
                    )
                ]
                conn.execute(
                    "UPDATE control_messages SET state=?,response=? "
                    "WHERE session_id=? AND state IN (?,?)",
                    (
                        "FAILED",
                        error or "Session finished before delivery.",
                        sid,
                        "QUEUED",
                        "RUNNING",
                    ),
                )

        for message in cancelled:
            self.sync_chat(message, error or "Session finished before delivery.")

    def submit(self, sid: str, message: str, request_id: str) -> None:
        if not message.strip() or len(message) > 32000 or not request_id or len(request_id) > 128:
            raise ControlConflict("Enter a message of 1–32,000 characters.")
        self._require_live(sid, ("MANUAL",))
        with self.connect(write=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute(
                "SELECT * FROM control_messages WHERE id=?", (sid + ":" + request_id,)
            ).fetchone()
            if previous:
                if previous["text"] != message.strip():
                    raise ControlConflict("The request identifier belongs to a different message.")
                return
            if (
                conn.execute(
                    "SELECT COUNT(*) FROM control_messages WHERE session_id=? AND state IN (?,?)",
                    (sid, "QUEUED", "RUNNING"),
                ).fetchone()[0]
                >= 8
            ):
                raise ControlConflict("The manual message queue is full; wait for a reply.")
            if (
                conn.execute("SELECT mode FROM controls WHERE id=?", (sid,)).fetchone()[0]
                != "MANUAL"
            ):
                raise ControlConflict("The session has returned to automation.")
            conn.execute(
                "INSERT INTO control_messages VALUES (?,?,?,?,NULL,?)",
                (sid + ":" + request_id, sid, message.strip(), "QUEUED", time.time()),
            )

        self.sync_chat({"session_id": sid, "id": sid + ":" + request_id, "text": message.strip()})

    def link_chat(self, sid: str, *, chat_path: Path, chat_id: str, saved: bool = False) -> None:
        if not saved:
            self._require_live(sid, ("ACTIVE", "REQUESTED", "MANUAL", "RESUMING"))
        with self.connect(write=True) as conn:
            conn.execute("UPDATE controls SET chat_path=?,chat_id=? WHERE id=?",
                         (str(chat_path), chat_id, sid))

    def submit_native(self, sid: str, message: str, request_id: str) -> None:
        """An unexpected terminal prompt requests takeover, never a second writer."""
        if not message.strip() or len(message) > 32000 or not request_id or len(request_id) > 128:
            raise ControlConflict("Enter a message of 1–32,000 characters.")
        self._require_live(sid, ("ACTIVE", "REQUESTED", "MANUAL"))
        with self.connect(write=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute("SELECT text FROM control_messages WHERE id=?",
                                    (sid + ":" + request_id,)).fetchone()
            if previous:
                if previous["text"] != message.strip():
                    raise ControlConflict("The request identifier belongs to a different message.")
                return
            row = conn.execute("SELECT * FROM controls WHERE id=?", (sid,)).fetchone()
            if row["mode"] not in ("ACTIVE", "REQUESTED", "MANUAL") or not row["native_id"]:
                raise ControlConflict("The session cannot accept native input now")
            if not row["chat_path"] or not row["chat_id"]:
                raise ControlConflict(
                    "Open this native session through SaberOps to synchronize chat")
            if conn.execute("SELECT COUNT(*) FROM control_messages WHERE session_id=? "
                            "AND state IN (?,?)", (sid, "QUEUED", "RUNNING")).fetchone()[0] >= 8:
                raise ControlConflict("The manual message queue is full; wait for a reply.")
            if row["mode"] == "ACTIVE":
                conn.execute("UPDATE controls SET mode=?,updated=? WHERE id=?",
                             ("REQUESTED", time.time(), sid))
            conn.execute("INSERT INTO control_messages VALUES (?,?,?,?,NULL,?)",
                         (sid + ":" + request_id, sid, message.strip(), "QUEUED", time.time()))
        self.sync_chat({"session_id": sid, "id": sid + ":" + request_id, "text": message.strip()})

    def next_message(self, sid: str) -> dict[str, Any] | None:
        with self.connect(write=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM control_messages WHERE session_id=? AND state=? "
                "ORDER BY rowid LIMIT 1",
                (sid, "QUEUED"),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE control_messages SET state=? WHERE id=?", ("RUNNING", row["id"])
                )
                conn.execute("UPDATE controls SET interrupt_turn=0 WHERE id=?", (sid,))
                return dict(row)
            return None

    def reply(self, message: dict[str, Any], response: str, *, failed: bool = False) -> None:
        with self.connect(write=True) as conn:
            conn.execute(
                "UPDATE control_messages SET state=?,response=? WHERE id=?",
                ("FAILED" if failed else "DONE", response[:32000], message["id"]),
            )
        self.sync_chat(message, response[:32000])

    def sync_chat(self, message: dict[str, Any], response: str | None = None) -> None:
        # The chat frontend was removed (L-A step 5): there is no conversation
        # store to mirror manual messages into.  The session journal remains
        # the durable record; this is a no-op so engine callers keep working.
        return

    def interrupt(self, sid: str) -> None:
        self._require_live(sid, ("MANUAL",))
        with self.connect(write=True) as conn:
            cancelled = [
                dict(m)
                for m in conn.execute(
                    "SELECT * FROM control_messages WHERE session_id=? AND state=?", (sid, "QUEUED")
                )
            ]
            conn.execute("UPDATE controls SET interrupt_turn=1 WHERE id=?", (sid,))
            conn.execute(
                "UPDATE control_messages SET state=?,response=? WHERE session_id=? AND state=?",
                ("FAILED", "Interrupted before delivery.", sid, "QUEUED"),
            )

        for message in cancelled:
            self.sync_chat(message, "Interrupted before delivery.")

    def should_interrupt(self, sid: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT mode,interrupt_turn FROM controls WHERE id=?", (sid,)
            ).fetchone()
            return bool(row and (row["mode"] == "REQUESTED" or row["interrupt_turn"]))

    def resume(self, sid: str) -> None:
        self._require_live(sid, ("MANUAL",))
        with self.connect(write=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute(
                "SELECT COUNT(*) FROM control_messages WHERE session_id=? AND state IN (?,?)",
                (sid, "QUEUED", "RUNNING"),
            ).fetchone()[0]:
                raise ControlConflict("Wait for the manual reply, or interrupt it before resuming.")
            count = conn.execute(
                "UPDATE controls SET mode=?,interrupt_turn=0,updated=? WHERE id=? AND mode=?",
                ("RESUMING", time.time(), sid, "MANUAL"),
            )
            if count.rowcount != 1:
                raise ControlConflict("The session is no longer under manual control.")

    @staticmethod
    def public(data: dict[str, Any] | None) -> dict[str, Any] | None:
        if data is None:
            return None
        return {k: v for k, v in data.items() if k not in ("identity", "chat_path")}
