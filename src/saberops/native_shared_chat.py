"""Durable inbox for a provider-owned native channel, without a second model loop."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from saberops.ownership import Liveness, ProcessIdentity, probe_liveness
from saberops.session_control import ControlConflict, SessionControls

REPLY_TIMEOUT = 30 * 60


class NativeSharedChat:
    def __init__(self, controls: SessionControls) -> None:
        self.controls = controls

    def _schema(self, conn: Any) -> None:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS native_chat_channels (
                key TEXT PRIMARY KEY, token TEXT NOT NULL, session_id TEXT NOT NULL,
                identity TEXT NOT NULL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS native_chat_inbox (
                id TEXT PRIMARY KEY, key TEXT NOT NULL, token TEXT NOT NULL,
                session_id TEXT NOT NULL, text TEXT NOT NULL, state TEXT NOT NULL,
                reply TEXT, action TEXT, created REAL NOT NULL, updated REAL NOT NULL,
                kind TEXT NOT NULL DEFAULT 'owner');
        """)
        if "kind" not in [row[1] for row in conn.execute("PRAGMA table_info(native_chat_inbox)")]:
            conn.execute(
                "ALTER TABLE native_chat_inbox ADD COLUMN kind TEXT NOT NULL DEFAULT 'owner'"
            )
        if "processor" not in [
            row[1] for row in conn.execute("PRAGMA table_info(native_chat_inbox)")
        ]:
            conn.execute("ALTER TABLE native_chat_inbox ADD COLUMN processor TEXT")

    def ready(self, data: dict[str, Any], identity: ProcessIdentity) -> None:
        with self.controls.connect(write=True) as conn:
            self._schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            self._lease(conn, data)
            conn.execute(
                "INSERT OR REPLACE INTO native_chat_channels VALUES (?,?,?,?,?)",
                (
                    data["key"],
                    data["token"],
                    data["id"],
                    json.dumps(identity.to_dict()),
                    time.time(),
                ),
            )

    @staticmethod
    def _lease(conn: Any, data: dict[str, Any]) -> None:
        row = conn.execute("SELECT * FROM native_clients WHERE key=?", (data["key"],)).fetchone()
        if not row or row["id"] != data["token"] or row["state"] != "ACTIVE":
            raise ControlConflict("This native chat channel no longer owns the session.")
        control = conn.execute("SELECT * FROM controls WHERE id=?", (data["id"],)).fetchone()
        if (
            row["session_id"] != data["id"]
            or not control
            or control["role"] != "orchestrator"
            or not control["chat_id"]
        ):
            raise ControlConflict("This channel does not belong to an orchestrator conversation.")
        if probe_liveness(ProcessIdentity.from_json(row["identity"])) != Liveness.LIVE:
            raise ControlConflict("The native terminal owner is unavailable.")

    def channel(self, sid: str) -> dict[str, Any] | None:
        if not self.controls.path.is_file():
            return None
        with self.controls.connect() as conn:
            if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='native_chat_channels'"
            ).fetchone():
                return None
            rows = conn.execute(
                "SELECT * FROM native_chat_channels WHERE session_id=?", (sid,)
            ).fetchall()
            for row in rows:
                data = dict(row, id=sid)
                try:
                    self._lease(conn, data)
                except ControlConflict:
                    continue
                if probe_liveness(ProcessIdentity.from_json(row["identity"])) != Liveness.LIVE:
                    continue
                data["pending"] = conn.execute(
                    "SELECT COUNT(*) FROM native_chat_inbox WHERE key=? AND token=? "
                    "AND state IN ('QUEUED','DELIVERED','REPLIED','PROCESSING')",
                    (row["key"], row["token"]),
                ).fetchone()[0]
                return data
        return None

    def submit(self, sid: str, text: str, request_id: str, *, chat_id: str) -> None:
        if not text.strip() or len(text) > 32000 or not request_id or len(request_id) > 128:
            raise ControlConflict("Enter a message of 1–32,000 characters.")
        control = self.controls.get(sid) or {}
        if control.get("chat_id") != chat_id or control.get("role") != "orchestrator":
            raise ControlConflict("This channel does not belong to your orchestrator conversation.")
        channel = self.channel(sid)
        if not channel:
            raise ControlConflict(
                "Shared native chat is not connected. Reopen the CLI or use synchronized chat."
            )
        message_id = sid + ":shared:" + request_id
        with self.controls.connect(write=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._lease(conn, channel)
            old = conn.execute(
                "SELECT text,state FROM native_chat_inbox WHERE id=?", (message_id,)
            ).fetchone()
            if old:
                if old["text"] != text.strip():
                    raise ControlConflict("This request identifier belongs to a different message.")
                if old["state"] == "FAILED":
                    raise ControlConflict(
                        "That message has an uncertain outcome. It was not resent."
                    )
            elif (
                conn.execute(
                    "SELECT COUNT(*) FROM native_chat_inbox WHERE key=? AND token=? "
                    "AND state IN ('QUEUED','DELIVERED','REPLIED','PROCESSING')",
                    (channel["key"], channel["token"]),
                ).fetchone()[0]
                >= 8
            ):
                raise ControlConflict("The native message queue is full. Wait for a reply.")
            if not old:
                conn.execute(
                    "INSERT INTO native_chat_inbox "
                    "(id,key,token,session_id,text,state,reply,action,created,updated) "
                    "VALUES (?,?,?,?,?,'QUEUED',NULL,NULL,?,?)",
                    (
                        message_id,
                        channel["key"],
                        channel["token"],
                        sid,
                        text.strip(),
                        time.time(),
                        time.time(),
                    ),
                )
        self.controls.sync_chat({"session_id": sid, "id": message_id, "text": text.strip()})

    def next(self, data: dict[str, Any]) -> dict[str, Any] | None:
        with self.controls.connect(write=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._lease(conn, data)
            if not conn.execute(
                "SELECT 1 FROM native_chat_channels WHERE key=? AND token=?",
                (data["key"], data["token"]),
            ).fetchone():
                raise ControlConflict("Shared chat was disconnected. Reopen the native CLI.")
            # Exactly one channel turn is delivered until it explicitly replies.
            if conn.execute(
                "SELECT 1 FROM native_chat_inbox WHERE key=? AND token=? "
                "AND state IN ('DELIVERED','REPLIED','PROCESSING')",
                (data["key"], data["token"]),
            ).fetchone():
                return None
            row = conn.execute(
                "SELECT * FROM native_chat_inbox WHERE key=? AND token=? "
                "AND state='QUEUED' ORDER BY rowid LIMIT 1",
                (data["key"], data["token"]),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE native_chat_inbox SET state=?,updated=? WHERE id=?",
                    ("DONE" if row["kind"] == "context" else "DELIVERED", time.time(), row["id"]),
                )
                return dict(row)
        return None

    def reply(
        self, data: dict[str, Any], message_id: str, text: str, action: str = "respond"
    ) -> None:
        if (
            not isinstance(text, str)
            or not text.strip()
            or len(text) > 32000
            or action not in ("respond", "work")
        ):
            raise ControlConflict("A shared reply must contain readable text and a valid action.")
        with self.controls.connect(write=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._lease(conn, data)
            row = conn.execute(
                "SELECT * FROM native_chat_inbox WHERE id=? AND key=? AND token=?",
                (message_id, data["key"], data["token"]),
            ).fetchone()
            if (
                not row
                or row["kind"] != "owner"
                or row["state"] not in ("DELIVERED", "REPLIED", "PROCESSING", "DONE")
            ):
                raise ControlConflict("The reply does not belong to a delivered app message.")
            if row["state"] != "DELIVERED":
                if row["reply"] != text.strip() or row["action"] != action:
                    raise ControlConflict("This message already has a different reply.")
            else:
                conn.execute(
                    "UPDATE native_chat_inbox SET state='REPLIED',reply=?,action=?,updated=? "
                    "WHERE id=?",
                    (text.strip(), action, time.time(), message_id),
                )
        self.controls.sync_chat(dict(row), text.strip())

    def notify(self, data: dict[str, Any], event_id: str, text: str) -> None:
        """Show app-generated dispatch/results in the native session as context only."""
        with self.controls.connect(write=True) as conn:
            self._lease(conn, data)
            conn.execute(
                "INSERT OR IGNORE INTO native_chat_inbox "
                "(id,key,token,session_id,text,state,created,updated,kind) "
                "VALUES (?,?,?,?,?,'QUEUED',?,?,'context')",
                (
                    "context:" + event_id,
                    data["key"],
                    data["token"],
                    data["id"],
                    text[:32000],
                    time.time(),
                    time.time(),
                ),
            )

    def track_terminal(self, data: dict[str, Any], event_id: str, text: str) -> None:
        with self.controls.connect(write=True) as conn:
            self._schema(conn)
            self._lease(conn, data)
            conn.execute(
                "INSERT OR IGNORE INTO native_chat_inbox "
                "(id,key,token,session_id,text,state,created,updated,kind) "
                "VALUES (?,?,?,?,?,'DONE',?,?,'terminal')",
                (
                    event_id,
                    data["key"],
                    data["token"],
                    data["id"],
                    text.strip()[:32000],
                    time.time(),
                    time.time(),
                ),
            )

    def request_work(self, data: dict[str, Any], owner_text: str, guidance: str) -> None:
        if (
            not isinstance(owner_text, str)
            or not isinstance(guidance, str)
            or not owner_text.strip()
            or not guidance.strip()
            or len(guidance) > 32000
        ):
            raise ControlConflict(
                "A work request needs the original owner text and readable guidance."
            )
        with self.controls.connect(write=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._lease(conn, data)
            row = conn.execute(
                "SELECT * FROM native_chat_inbox WHERE key=? AND token=? "
                "AND kind='terminal' ORDER BY rowid DESC LIMIT 1",
                (data["key"], data["token"]),
            ).fetchone()
            if not row or row["text"] != owner_text.strip():
                raise ControlConflict(
                    "Work advice must match the latest recorded terminal owner message."
                )
            if row["action"]:
                if row["action"] == "work" and row["reply"] == guidance.strip():
                    return
                raise ControlConflict("That terminal message already has a work request.")
            conn.execute(
                "UPDATE native_chat_inbox SET state='REPLIED',reply=?,action='work',updated=? "
                "WHERE id=?",
                (guidance.strip(), time.time(), row["id"]),
            )
        self.controls.sync_chat(dict(row), guidance.strip())

    def replies(self) -> list[dict[str, Any]]:
        with self.controls.connect() as conn:
            if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='native_chat_inbox'"
            ).fetchone():
                return []
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM native_chat_inbox WHERE state='REPLIED' ORDER BY rowid LIMIT 8"
                )
            ]

    def finish(self, message_id: str) -> None:
        with self.controls.connect(write=True) as conn:
            conn.execute(
                "UPDATE native_chat_inbox SET state='DONE',updated=? WHERE id=? "
                "AND state IN ('REPLIED','PROCESSING')",
                (time.time(), message_id),
            )

    def claim_reply(self, message_id: str) -> bool:
        with self.controls.connect(write=True) as conn:
            return (
                conn.execute(
                    "UPDATE native_chat_inbox SET state='PROCESSING',updated=?,processor=? "
                    "WHERE id=? AND state='REPLIED'",
                    (time.time(), json.dumps(ProcessIdentity.for_self().to_dict()), message_id),
                ).rowcount
                == 1
            )

    def recover(self) -> None:
        """Resolve lost channel/dispatch owners without replaying uncertain requests."""
        with self.controls.connect(write=True) as conn:
            self._schema(conn)
            channels = [
                dict(row, id=row["session_id"])
                for row in conn.execute("SELECT * FROM native_chat_channels")
            ]
            processing = [
                dict(row)
                for row in conn.execute("SELECT * FROM native_chat_inbox WHERE state='PROCESSING'")
            ]
        for channel in channels:
            live = probe_liveness(ProcessIdentity.from_json(channel["identity"]))
            with self.controls.connect() as conn:
                expired = conn.execute(
                    "SELECT 1 FROM native_chat_inbox WHERE key=? AND token=? "
                    "AND state='DELIVERED' AND updated<?",
                    (channel["key"], channel["token"], time.time() - REPLY_TIMEOUT),
                ).fetchone()
            if live == Liveness.DEAD or expired:
                self.closed(
                    channel,
                    "The native channel disconnected or its reply timed out. "
                    "Delivery is uncertain; your message was not automatically resent.",
                )
        for message in processing:
            if probe_liveness(ProcessIdentity.from_json(message["processor"])) != Liveness.DEAD:
                continue
            reason = "SaberOps restarted during this work request. Check Runs for any work "
            reason += "already queued. The request was not automatically repeated."
            control = self.controls.get(message["session_id"]) or {}
            if control.get("chat_id") and control.get("chat_path"):
                from saberops.chat import ChatStore

                ChatStore(Path(control["chat_path"])).record_native_reply(
                    control["chat_id"], "interrupted:" + message["id"], reason, role="orchestrator"
                )
            self.finish(message["id"])

    def closed(self, data: dict[str, Any], reason: str) -> None:
        with self.controls.connect(write=True) as conn:
            self._schema(conn)
            pending = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM native_chat_inbox WHERE key=? AND token=? "
                    "AND state IN ('QUEUED','DELIVERED')",
                    (data["key"], data["token"]),
                )
            ]
            conn.execute(
                "DELETE FROM native_chat_channels WHERE key=? AND token=?",
                (data["key"], data["token"]),
            )
            conn.execute(
                "UPDATE native_chat_inbox SET state='FAILED',reply=?,updated=? WHERE key=? "
                "AND token=? AND state IN ('QUEUED','DELIVERED')",
                (reason, time.time(), data["key"], data["token"]),
            )
        for message in pending:
            self.controls.sync_chat(message, reason)
