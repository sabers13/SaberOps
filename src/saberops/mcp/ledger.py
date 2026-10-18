"""Append-only event ledger and the versioned read snapshot (M3).

Two client-synchronisation primitives:

* **Read snapshot, schema ``v: 1``** -- every read payload the server emits
  carries ``{"v": 1, ...}`` so a client can pin the shape it parses and detect
  a future incompatible change instead of guessing.
* **Append-only event ledger** -- one JSONL file whose entries form a SHA-256
  hash chain (``prev_digest`` -> ``digest``).  ``append`` is idempotent on
  ``(type, key)``: replaying the same logical event returns the existing entry,
  a *different* payload for an already-used key is a typed conflict, and
  :meth:`EventLedger.replay` folds the log into state deterministically (same
  log, same state; a tampered chain fails closed).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from saberops.mcp.errors import McpError, ToolError

__all__ = [
    "LEDGER_ENV",
    "LEDGER_SCHEMA_VERSION",
    "SNAPSHOT_SCHEMA_VERSION",
    "EventLedger",
    "LedgerConflictError",
    "LedgerCorruptError",
    "LedgerEvent",
    "LedgerState",
    "default_ledger_path",
]

#: Schema version of the ledger document / chain.
LEDGER_SCHEMA_VERSION = 1

#: The read-snapshot schema version exposed to clients (``v: 1``).
SNAPSHOT_SCHEMA_VERSION = 1

#: Test/diagnostics override for the ledger file path.
LEDGER_ENV = "SABEROPS_MCP_LEDGER"

LEDGER_FILENAME = "mcp-events.jsonl"


def default_ledger_path() -> Path:
    """Resolve the ledger path (``SABEROPS_MCP_LEDGER`` overrides)."""
    override = os.environ.get(LEDGER_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    from saberops.paths import saberops_state_dir

    return saberops_state_dir() / LEDGER_FILENAME


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _canonical(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _digest(payload: object) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


class LedgerConflictError(ToolError):
    """A different payload was recorded for an already-used idempotency key."""


class LedgerCorruptError(McpError):
    """The on-disk ledger is malformed or fails its hash-chain check."""


@dataclass(frozen=True)
class LedgerEvent:
    """One immutable, hash-chained ledger entry."""

    seq: int
    at: str
    type: str
    key: str
    payload: dict[str, Any]
    digest: str = ""
    prev_digest: str = ""

    def body(self) -> dict[str, Any]:
        """The signed body (everything except the digest itself)."""
        return {
            "index": LEDGER_SCHEMA_VERSION,
            "seq": self.seq,
            "at": self.at,
            "type": self.type,
            "key": self.key,
            "payload": self.payload,
            "prev_digest": self.prev_digest,
        }

    def expected_digest(self) -> str:
        return _digest(self.body())

    def with_digest(self) -> LedgerEvent:
        from dataclasses import replace

        return replace(self, digest=self.expected_digest())

    def as_dict(self) -> dict[str, Any]:
        payload = self.body()
        payload["digest"] = self.digest
        return payload

    @classmethod
    def from_mapping(cls, raw: object) -> LedgerEvent:
        """Rebuild one entry, fail-closed on a malformed row."""
        if not isinstance(raw, Mapping):
            raise LedgerCorruptError("ledger entry must be a JSON object")
        index = raw.get("index")
        if index != LEDGER_SCHEMA_VERSION:
            raise LedgerCorruptError(f"unsupported ledger schema index: {index!r}")
        seq = raw.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
            raise LedgerCorruptError(f"ledger seq must be a positive integer: {seq!r}")
        at = raw.get("at")
        type_name = raw.get("type")
        key = raw.get("key")
        digest = raw.get("digest")
        prev_digest = raw.get("prev_digest")
        payload = raw.get("payload")
        for label, value in (
            ("at", at),
            ("type", type_name),
            ("key", key),
            ("digest", digest),
            ("prev_digest", prev_digest),
        ):
            if not isinstance(value, str):
                raise LedgerCorruptError(f"ledger {label} must be a string")
        if not isinstance(payload, Mapping):
            raise LedgerCorruptError("ledger payload must be an object")
        return cls(
            seq=seq,
            at=str(at),
            type=str(type_name),
            key=str(key),
            payload={str(k): v for k, v in payload.items()},
            digest=str(digest),
            prev_digest=str(prev_digest),
        )


@dataclass(frozen=True)
class LedgerState:
    """Deterministic fold of the ledger, produced by :meth:`EventLedger.replay`."""

    last_seq: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    waves: dict[str, dict[str, Any]] = field(default_factory=dict)

    def as_mapping(self) -> dict[str, Any]:
        return {
            "v": SNAPSHOT_SCHEMA_VERSION,
            "last_seq": self.last_seq,
            "counts": dict(sorted(self.counts.items())),
            "waves": [self.waves[key] for key in sorted(self.waves)],
        }


class EventLedger:
    """Append-only, hash-chained JSONL ledger with flock-serialised appends."""

    def __init__(self, path: Path | str | None = None) -> None:
        self._path = Path(path) if path is not None else default_ledger_path()

    @property
    def path(self) -> Path:
        return self._path

    def events(self) -> tuple[LedgerEvent, ...]:
        """Read and verify the whole chain (missing file is an empty ledger)."""
        if not self._path.exists():
            return ()
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            raise LedgerCorruptError(f"cannot read ledger {self._path}: {exc}") from exc
        entries: list[LedgerEvent] = []
        prev = ""
        expected_seq = 1
        for line_number, line in enumerate(text.splitlines(), start=1):
            raw = line.strip()
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except ValueError as exc:
                raise LedgerCorruptError(
                    f"ledger line {line_number} is not JSON: {exc}"
                ) from exc
            event = LedgerEvent.from_mapping(payload)
            if event.seq != expected_seq:
                raise LedgerCorruptError(
                    f"ledger line {line_number} has seq {event.seq}, expected {expected_seq}"
                )
            if event.prev_digest != prev:
                raise LedgerCorruptError(
                    f"ledger line {line_number} breaks the hash chain"
                )
            if event.digest != event.expected_digest():
                raise LedgerCorruptError(
                    f"ledger line {line_number} fails its content-hash check"
                )
            entries.append(event)
            prev = event.digest
            expected_seq += 1
        return tuple(entries)

    def append(
        self,
        type: str,
        payload: Mapping[str, Any],
        *,
        key: str | None = None,
        now: str | None = None,
    ) -> LedgerEvent:
        """Append one event, idempotently on ``(type, key)``.

        A repeated append with the same key and the same payload returns the
        existing event unchanged (replay is a no-op).  The same key with a
        different payload is a typed :class:`LedgerConflictError`.
        """
        if not isinstance(type, str) or not type.strip():
            raise LedgerConflictError("ledger event type is required")
        type_name = type.strip()
        body = {str(k): v for k, v in payload.items()}
        event_key = (
            str(key).strip()
            if isinstance(key, str) and key.strip()
            else _digest(body)
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                handle.seek(0)
                existing = self._parse_text(handle.read())
                for event in existing:
                    if event.type == type_name and event.key == event_key:
                        if event.payload == body:
                            return event
                        raise LedgerConflictError(
                            f"ledger key {event_key!r} for type {type_name!r} already "
                            "holds a different payload"
                        )
                prev = existing[-1].digest if existing else ""
                seq = existing[-1].seq + 1 if existing else 1
                candidate = LedgerEvent(
                    seq=seq,
                    at=now or _now_iso(),
                    type=type_name,
                    key=event_key,
                    payload=body,
                    prev_digest=prev,
                ).with_digest()
                handle.seek(0, os.SEEK_END)
                handle.write(_canonical(candidate.as_dict()) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
                return candidate
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def replay(self) -> LedgerState:
        """Fold the log into state deterministically and idempotently."""
        counts: dict[str, int] = {}
        waves: dict[str, dict[str, Any]] = {}
        last_seq = 0
        for event in self.events():
            last_seq = event.seq
            counts[event.type] = counts.get(event.type, 0) + 1
            if event.type == "wave_submitted":
                wave_id = event.payload.get("wave_id")
                if isinstance(wave_id, str) and wave_id:
                    waves[wave_id] = event.payload
        return LedgerState(last_seq=last_seq, counts=counts, waves=waves)

    def snapshot(self) -> dict[str, Any]:
        """Render the ledger's own ``v: 1`` read snapshot."""
        state = self.replay()
        return {
            "v": SNAPSHOT_SCHEMA_VERSION,
            "generated_at": _now_iso(),
            "path": str(self._path),
            "last_seq": state.last_seq,
            "counts": state.counts,
            "waves": state.as_mapping()["waves"],
        }

    @staticmethod
    def _parse_text(text: str) -> list[LedgerEvent]:
        """Parse an in-handle text body without re-reading the file."""
        entries: list[LedgerEvent] = []
        prev = ""
        expected_seq = 1
        for line_number, line in enumerate(text.splitlines(), start=1):
            raw = line.strip()
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except ValueError as exc:
                raise LedgerCorruptError(
                    f"ledger line {line_number} is not JSON: {exc}"
                ) from exc
            event = LedgerEvent.from_mapping(payload)
            if event.seq != expected_seq:
                raise LedgerCorruptError(
                    f"ledger line {line_number} has seq {event.seq}, expected {expected_seq}"
                )
            if event.prev_digest != prev:
                raise LedgerCorruptError(f"ledger line {line_number} breaks the hash chain")
            if event.digest != event.expected_digest():
                raise LedgerCorruptError(
                    f"ledger line {line_number} fails its content-hash check"
                )
            entries.append(event)
            prev = event.digest
            expected_seq += 1
        return entries
