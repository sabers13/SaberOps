"""Engine gate-history ledger (ADR-0004 D3).

One append-only JSONL ledger records every gate the engine produces, in the
existing G-005 ``full.jsonl`` format **extended with ``kind`` and
``producer``**:

* ``kind`` is the lifecycle point -- ``G1`` (lane check), ``G2`` (fix check),
  ``G3`` (integration gate), ``main-gate`` (periodic main gate) or ``manual``.
* ``producer`` is who ran the gate.  Only ``engine`` is authoritative
  evidence; a model's own iteration run (G0) is never accepted here.

The ledger is shared by all worktrees of one checkout (the git common dir) and
lives next to the developer gate's history so both views read the same file.
Writes are single-line appends under an exclusive ``flock`` so concurrent
gates cannot interleave a partial line.
"""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

HISTORY_DIRNAME: Final[str] = "saberops-gate-history"
HISTORY_FILENAME: Final[str] = "full.jsonl"


class GateHistoryError(ValueError):
    """Fail-closed error for an invalid history entry or producer."""


class GateKind(StrEnum):
    """Lifecycle point of one gate run."""

    G1 = "G1"
    G2 = "G2"
    G3 = "G3"
    MAIN_GATE = "main-gate"
    MANUAL = "manual"


class GateProducer(StrEnum):
    """Who produced a gate run.

    ``ENGINE`` is the authoritative producer (ADR-0004 D3).  ``DEVELOPER`` is
    the repository's own gate scripts run by a human/worker shell.  ``MODEL``
    and ``G0`` are iteration runs: they are never evidence.
    """

    ENGINE = "engine"
    DEVELOPER = "developer"
    MODEL = "model"
    G0 = "G0"


#: Producer values accepted as authoritative evidence.
ENGINE_EVIDENCE_PRODUCERS: Final[frozenset[str]] = frozenset({GateProducer.ENGINE.value})


@dataclass(frozen=True)
class GateHistoryEntry:
    """One normalized gate-history line."""

    kind: GateKind
    producer: str
    head_sha: str
    worktree: str = ""
    outcome: str = "INFRA"
    reason: str = ""
    tests: int = 0
    failed: tuple[str, ...] = ()
    flaky: tuple[str, ...] = ()
    wall_time: float = 0.0
    peak_bytes: int = 0
    profile: str = ""
    label: str = ""
    ts: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.head_sha or not self.head_sha.strip():
            raise GateHistoryError("gate history entry requires a head_sha")
        if not str(self.producer).strip():
            raise GateHistoryError("gate history entry requires a producer")
        if self.tests < 0 or self.wall_time < 0.0 or self.peak_bytes < 0:
            raise GateHistoryError("gate history counters cannot be negative")

    @property
    def engine_produced(self) -> bool:
        """True only when the entry is authoritative engine evidence."""
        return self.producer in ENGINE_EVIDENCE_PRODUCERS

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = dict(self.extra)
        payload.update(
            {
                "ts": self.ts or datetime.now(UTC).isoformat(timespec="seconds"),
                "head_sha": self.head_sha,
                "worktree": self.worktree,
                "kind": self.kind.value,
                "producer": self.producer,
                "profile": self.profile,
                "label": self.label or self.kind.value,
                "outcome": self.outcome,
                "reason": self.reason,
                "tests": self.tests,
                "failed": list(self.failed),
                "flaky": list(self.flaky),
                "wall_time": self.wall_time,
                "peak_bytes": self.peak_bytes,
            }
        )
        return payload


def history_path(common_dir: Path | str) -> Path:
    """Return the shared ledger path for a git common directory."""
    return Path(common_dir) / HISTORY_DIRNAME / HISTORY_FILENAME


def append_entry(path: Path | str, entry: GateHistoryEntry) -> None:
    """Append one JSON line under an exclusive ``flock``.

    The parent directory is created on demand; an I/O failure raises so the
    engine can fail closed rather than silently lose authoritative evidence.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry.as_dict(), sort_keys=True) + "\n"
    with target.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def read_entries(path: Path | str) -> list[dict[str, Any]]:
    """Read the ledger oldest-first; a missing file or bad line yields []."""
    target = Path(path)
    if not target.exists():
        return []
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return []
    entries: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            entries.append(parsed)
    return entries


def require_engine_evidence(entry: Mapping[str, Any]) -> None:
    """Raise unless ``entry`` is authoritative engine evidence (ADR-0004 D3).

    G0 (model) runs and human developer runs are legitimate operations but
    they are never accepted as gate evidence; this seam states that rule in
    one place so every consumer applies it identically.
    """
    producer = str(entry.get("producer", "")).strip()
    if producer not in ENGINE_EVIDENCE_PRODUCERS:
        raise GateHistoryError(
            f"gate evidence producer {producer or '<missing>'!r} is not authoritative; "
            "only engine-run gates are accepted as evidence (G0/model runs are not)"
        )


__all__ = [
    "ENGINE_EVIDENCE_PRODUCERS",
    "HISTORY_DIRNAME",
    "HISTORY_FILENAME",
    "GateHistoryEntry",
    "GateHistoryError",
    "GateKind",
    "GateProducer",
    "append_entry",
    "history_path",
    "read_entries",
    "require_engine_evidence",
]
