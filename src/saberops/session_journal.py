"""Durable execution evidence, independent of bounded console excerpts.

Only execution owners/recovery write outcomes. Reads never change liveness.
A recovery outcome may be interrupted/uncertain, never inferred success.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from saberops.ownership import Liveness, ProcessIdentity, probe_liveness


class SessionJournal:
    def __init__(self, root: Path) -> None:
        self.path = root / "session-journal.db"

    @contextmanager
    def connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            conn = sqlite3.connect(self.path, timeout=10)
            self.path.chmod(0o600)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, role TEXT NOT NULL,
                    association TEXT NOT NULL, identity TEXT, process_identity TEXT,
                    created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                    kind TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS events_session ON events(session_id, sequence);
                CREATE TABLE IF NOT EXISTS outcomes (
                    session_id TEXT PRIMARY KEY, payload TEXT NOT NULL, created REAL NOT NULL);
            """)
        else:
            conn = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def register(
        self, sid: str, owner: str, role: str, association: str, identity: str | None
    ) -> None:
        with self.connect(write=True) as conn:
            conn.execute(
                "INSERT INTO sessions VALUES (?,?,?,?,?,NULL,?)",
                (sid, owner, role, association, identity, time.time()),
            )

    def append(self, sid: str, kind: str, payload: object) -> int:
        with self.connect(write=True) as conn:
            inserted = conn.execute(
                "INSERT INTO events(session_id,kind,payload,created) VALUES (?,?,?,?)",
                (sid, kind, json.dumps(payload, ensure_ascii=False), time.time()),
            )
            assert inserted.lastrowid is not None
            return inserted.lastrowid

    def latest_sequence(self, sid: str) -> int:
        if not self.path.is_file():
            return 0
        with self.connect() as conn:
            return int(
                conn.execute(
                    "SELECT COALESCE(MAX(sequence),0) FROM events WHERE session_id=?", (sid,)
                ).fetchone()[0]
            )

    def last_turn(self, sid: str) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        with self.connect() as conn:
            row = conn.execute(
                "SELECT payload FROM events WHERE session_id=? AND kind='turn.completed' "
                "ORDER BY sequence DESC LIMIT 1",
                (sid,),
            ).fetchone()
        return dict(json.loads(row[0])) if row else {}

    def process(self, sid: str, identity: dict[str, Any]) -> None:
        with self.connect(write=True) as conn:
            conn.execute(
                "UPDATE sessions SET process_identity=? WHERE id=?", (json.dumps(identity), sid)
            )
        self.append(sid, "process.started", identity)

    def complete(self, sid: str, payload: dict[str, Any]) -> bool:
        """Commit one immutable canonical outcome; repeated recovery is idempotent."""
        with self.connect(write=True) as conn:
            inserted = (
                conn.execute(
                    "INSERT OR IGNORE INTO outcomes VALUES (?,?,?)",
                    (sid, json.dumps(payload), time.time()),
                ).rowcount
                == 1
            )
        return inserted

    def view(self, sid: str, *, after: int = 0, limit: int = 100) -> dict[str, Any]:
        if not self.path.is_file():
            return {"events": [], "outcome": None, "cursor": after}
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE session_id=? AND sequence>? ORDER BY sequence LIMIT ?",
                (sid, after, min(200, max(1, limit))),
            )
            events = [
                {
                    "sequence": row["sequence"],
                    "kind": row["kind"],
                    "payload": json.loads(row["payload"]),
                    "created": row["created"],
                }
                for row in rows
            ]
            outcome = conn.execute(
                "SELECT payload FROM outcomes WHERE session_id=?", (sid,)
            ).fetchone()
        return {
            "events": events,
            "outcome": json.loads(outcome[0]) if outcome else None,
            "cursor": events[-1]["sequence"] if events else after,
        }

    def recover(self, owner: str) -> list[str]:
        """Called by recovery, never UI projection. Surviving workers stay fenced."""
        if not self.path.is_file():
            return []
        with self.connect() as conn:
            rows = [
                dict(row)
                for row in conn.execute(
                    "SELECT s.* FROM sessions s LEFT JOIN outcomes o ON o.session_id=s.id "
                    "WHERE s.owner=? AND o.session_id IS NULL",
                    (owner,),
                )
            ]
        recovered: list[str] = []
        for row in rows:
            owner_live = probe_liveness(ProcessIdentity.from_json(row["identity"]))
            if owner_live != Liveness.DEAD:
                continue
            worker_live = probe_liveness(ProcessIdentity.from_json(row["process_identity"]))
            state = "INTERRUPTED" if worker_live == Liveness.DEAD else "UNCERTAIN"
            if self.complete(
                row["id"],
                {
                    "status": state,
                    "source": "recovery",
                    "provider_terminal_status": None,
                    "reason": "Execution owner disappeared",
                    "worker_liveness": worker_live.value,
                },
            ):
                recovered.append(row["id"])
        return recovered

    def tail(self, sid: str, *, max_bytes: int = 16000) -> str:
        if not self.path.is_file():
            return ""
        with self.connect() as conn:
            rows = list(
                conn.execute(
                    "SELECT kind,payload FROM events WHERE session_id=? "
                    "AND kind IN ('output.stdout','output.stderr') "
                    "ORDER BY sequence DESC LIMIT 100",
                    (sid,),
                )
            )
        text = "".join(
            ("[stderr] " if row["kind"] == "output.stderr" else "")
            + json.loads(row["payload"])["text"]
            for row in reversed(rows)
        )
        return text.encode("utf-8")[-max_bytes:].decode("utf-8", errors="replace")

    def export(self, sid: str) -> Iterator[str]:
        """Stream a stable read snapshot; WAL readers do not block live capture."""
        with self.connect() as conn:
            conn.execute("BEGIN")
            for row in conn.execute(
                "SELECT * FROM events WHERE session_id=? ORDER BY sequence", (sid,)
            ):
                yield (
                    json.dumps(
                        {
                            "sequence": row["sequence"],
                            "kind": row["kind"],
                            "created": row["created"],
                            "payload": json.loads(row["payload"]),
                        }
                    )
                    + "\n"
                )
            outcome = conn.execute(
                "SELECT payload FROM outcomes WHERE session_id=?", (sid,)
            ).fetchone()
            if outcome:
                yield json.dumps({"kind": "completion", "payload": json.loads(outcome[0])}) + "\n"
