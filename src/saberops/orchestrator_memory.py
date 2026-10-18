"""Bounded Orchestrator memory, session locking and restart recovery (OM-1, Step 022).

This module owns exactly one bounded concern per ADR-0004 **D11** (and the
roadmap v2 row ``OM-1``): the Orchestrator's context window is the scarcest
resource of a run, so its *memory* must be small, explicit and crash-safe:

* a **startup digest** with a fixed section order, an explicit character/token
  budget, and an explicit ``[ABSENT]`` marker for every piece of missing state
  (absence is never confused with a truncated section);
* a **notebook** whose entries are typed ``pinned`` / ``aging`` / ``perishable``
  and whose total size is strictly bounded, with every eviction moved to an
  append-only on-disk archive instead of being lost;
* a **single-writer session lock** so a second Orchestrator session is strictly
  forced into read-only mode, with stale-lock detection that verifies the
  recorded process incarnation before reclaiming;
* the **context trigger** when cumulative Orchestrator context crosses 50 % of
  the model window (latched, so a later compaction cannot hide it), the
  work-remaining heuristic that decides whether to finish in the current session
  or rotate to a fresh one with a durable handoff, and the full-autonomy
  self-split rule;
* a **transactional turn journal** so a decision point is acknowledged only
  after full handling, a mid-turn death repeats nothing, and an overflow
  re-issues the exact decision point in a clean session.

Boundaries (deliberately fixed):

* This module consumes ``saberops.orchestrator_turn_policy`` (OT-1) and never
  changes it: :data:`ContextUsage` and the ``> 50 %`` rule stay there.
* Nothing here launches, resumes or terminates a model session, reads a diff or
  a log, or writes anywhere outside the state root handed to it.
* The file lock is advisory-with-verification, not a kernel lock, and it is
  never treated as strong evidence of liveness on its own.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from saberops.authority import AuthorityMode
from saberops.orchestrator_turn_policy import (
    CONTEXT_ROTATION_THRESHOLD_PERCENT,
    MAX_FAILED_ATTEMPTS_BEFORE_OWNER_QUESTION,
    ContextUsage,
    idempotency_key,
)
from saberops.ownership import Liveness, ProcessIdentity, probe_liveness

ORCHESTRATOR_MEMORY_VERSION: Final[str] = "orchestrator-memory/1"

#: Explicit marker for state that is genuinely missing (never omit, never invent).
ABSENT_MARKER: Final[str] = "[ABSENT]"

#: A section that had state but lost part of it to the budget is *truncated*,
#: never rendered as absent.
TRUNCATED_TEMPLATE: Final[str] = "[TRUNCATED {count} more]"

#: Work may finish in the current session when at most this share is left.
MAX_REMAINING_RATIO: Final[float] = 0.25
#: The same threshold as a percentage, used for exact integer arithmetic.
CONTINUE_REMAINING_PERCENT: Final[int] = 25

# --- Hard input bounds: exceeding these is malformed or unbounded, fail closed ---
MAX_IDENTIFIER_LENGTH: Final[int] = 128
MAX_DIGEST_ITEMS: Final[int] = 512
MAX_TEXT_LENGTH: Final[int] = 4_000
MAX_COMMAND_LENGTH: Final[int] = 512

# --- Startup digest budget (characters are primary; tokens are derived) ---
DIGEST_MAX_CHARS: Final[int] = 12_000
DIGEST_MAX_TOKENS: Final[int] = 3_000
DIGEST_CHARS_PER_TOKEN: Final[int] = 4
DIGEST_MAX_ITEMS_PER_SECTION: Final[int] = 32
DIGEST_MAX_ITEM_CHARS: Final[int] = 320

# --- Notebook budget and tier priorities ---
NOTEBOOK_MAX_ENTRIES: Final[int] = 512
NOTEBOOK_MAX_CHARS: Final[int] = 8_000
NOTEBOOK_MAX_ENTRY_CHARS: Final[int] = 500
PINNED_PRIORITY: Final[int] = 1_000_000
AGING_BASE_PRIORITY: Final[int] = 10_000
AGING_DECAY_PER_AGE: Final[int] = 1_000
PERISHABLE_PRIORITY: Final[int] = 5_000

# --- On-disk layout below the Orchestrator memory root ---
LOCK_FILENAME: Final[str] = "orchestrator-session.lock"
NOTEBOOK_FILENAME: Final[str] = "notebook.json"
ARCHIVE_FILENAME: Final[str] = "notebook-archive.jsonl"
JOURNAL_FILENAME: Final[str] = "turn-journal.jsonl"
HANDOFFS_DIRNAME: Final[str] = "handoffs"

_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}\Z")
_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
_CONTROL_CHARS = ("\n", "\r", "\x00")

Clock = Callable[[], str]
LivenessProbe = Callable[[ProcessIdentity | None], Liveness]


def _default_clock() -> str:
    """Return the current UTC instant as a stable ISO-8601 string."""
    return datetime.now(UTC).isoformat(timespec="seconds")


class OrchestratorMemoryError(ValueError):
    """A caller-supplied fact is malformed or unbounded; the module fails closed."""


class DigestBudgetError(OrchestratorMemoryError):
    """The irreducible startup digest exceeds its budget; the digest is refused."""


class NotebookBudgetError(OrchestratorMemoryError):
    """A notebook entry cannot be admitted without breaking the size bound."""


class NotebookStoreError(OrchestratorMemoryError):
    """The persisted notebook is unreadable or corrupt; never silently emptied."""


class SessionLockedError(OrchestratorMemoryError):
    """Another live Orchestrator session holds the single-writer lock."""

    def __init__(self, holder: LockHolder) -> None:
        super().__init__(
            f"orchestrator session {holder.session_id!r} holds the writer lock "
            f"(pid={holder.pid}); this session is read-only"
        )
        self.holder = holder


class SessionLockUnsafeError(OrchestratorMemoryError):
    """The lock state cannot be decided; never reclaimed, never bypassed."""


class HandoffConflictError(OrchestratorMemoryError):
    """A durable handoff record already exists with different content."""


class TurnJournalError(OrchestratorMemoryError):
    """A turn journal operation violates the transactional turn contract."""


class TurnConflictError(TurnJournalError):
    """A committed turn cannot be re-committed with different actions."""


class ReadonlySessionError(RuntimeError):
    """A read-only Orchestrator session attempted to mutate durable state."""


class NotebookTier(StrEnum):
    """The three bounded notebook entry classes (ADR-0004 D11)."""

    PINNED = "pinned"
    AGING = "aging"
    PERISHABLE = "perishable"


class SessionMode(StrEnum):
    """How an Orchestrator session may use the shared memory root."""

    WRITER = "writer"
    READONLY = "readonly"


class RotationAction(StrEnum):
    """Deterministic advice for a latched context trigger."""

    CONTINUE_CURRENT_SESSION = "continue_current_session"
    ROTATE_SESSION = "rotate_session"


class TurnState(StrEnum):
    """Lifecycle of one Orchestrator decision point."""

    OPEN = "open"
    HANDLED = "handled"
    ROLLED_BACK = "rolled_back"


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #


def _require_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise OrchestratorMemoryError(f"{label} must be an integer")
    return value


def _require_bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise OrchestratorMemoryError(f"{label} must be a boolean")
    return value


def _positive_int(value: object, label: str) -> int:
    number = _require_int(value, label)
    if number <= 0:
        raise OrchestratorMemoryError(f"{label} must be strictly positive")
    return number


def _validate_identifier(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise OrchestratorMemoryError(f"{label} must be a string")
    if len(value) > MAX_IDENTIFIER_LENGTH:
        raise OrchestratorMemoryError(
            f"{label} exceeds the maximum length of {MAX_IDENTIFIER_LENGTH} characters"
        )
    if _IDENTIFIER_RE.fullmatch(value) is None:
        raise OrchestratorMemoryError(
            f"{label} is malformed: expected an alphanumeric-leading token of "
            f"[A-Za-z0-9._/-], got {value!r}"
        )
    return value


def _optional_identifier(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _validate_identifier(value, label)


def _validate_sha(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA_RE.fullmatch(value) is None:
        raise OrchestratorMemoryError(
            f"{label} must be an exact 40-character lowercase hex SHA (no abbreviation), "
            f"got {value!r}"
        )
    return value


def _validate_optional_sha(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _validate_sha(value, label)


def _validate_line(value: object, label: str, max_length: int) -> str:
    """Validate one single-line, control-character-free text value."""
    if not isinstance(value, str):
        raise OrchestratorMemoryError(f"{label} must be a string")
    if value.strip() == "":
        raise OrchestratorMemoryError(f"{label} must not be empty")
    if len(value) > max_length:
        raise OrchestratorMemoryError(f"{label} exceeds the maximum length of {max_length}")
    if any(marker in value for marker in _CONTROL_CHARS):
        raise OrchestratorMemoryError(
            f"{label} must be a single line (newlines, carriage returns and NUL are refused)"
        )
    return value


def _optional_line(value: object, label: str, max_length: int) -> str | None:
    if value is None:
        return None
    return _validate_line(value, label, max_length)


def _require_sequence(value: object, label: str, max_items: int) -> tuple[Any, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise OrchestratorMemoryError(f"{label} must be an ordered sequence")
    items = tuple(value)
    if len(items) > max_items:
        raise OrchestratorMemoryError(
            f"{label} has {len(items)} items; the hard limit is {max_items} "
            "(unbounded input refused)"
        )
    return items


def _require_items[ItemT](
    value: object, label: str, max_items: int, item_type: type[ItemT]
) -> tuple[ItemT, ...]:
    items = _require_sequence(value, label, max_items)
    collected: list[ItemT] = []
    for index, item in enumerate(items):
        if not isinstance(item, item_type):
            raise OrchestratorMemoryError(
                f"{label}[{index}] must be a {item_type.__name__} instance"
            )
        collected.append(item)
    return tuple(collected)


def _validate_lines(value: object, label: str, max_items: int) -> tuple[str, ...]:
    items = _require_sequence(value, label, max_items)
    return tuple(
        _validate_line(item, f"{label}[{index}]", MAX_TEXT_LENGTH)
        for index, item in enumerate(items)
    )


def _coerce_enum[EnumT: StrEnum](enum_type: type[EnumT], value: object, label: str) -> EnumT:
    if isinstance(value, enum_type):
        return value
    if isinstance(value, str):
        try:
            return enum_type(value)
        except ValueError:
            try:
                return enum_type[value]
            except KeyError:
                pass
    expected = ", ".join(member.value for member in enum_type)
    raise OrchestratorMemoryError(f"{label} must be one of: {expected}")


def estimate_tokens(text: str) -> int:
    """Return a deterministic character-proportional token estimate (ceil(chars / 4))."""
    return (len(text) + DIGEST_CHARS_PER_TOKEN - 1) // DIGEST_CHARS_PER_TOKEN


def _compact_json(payload: object) -> str:
    """Return deterministic compact JSON (no ASCII escaping, insertion key order)."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=False, separators=(",", ":"))


def _append_disclosure(truncations: list[str], section: str) -> None:
    if section not in truncations:
        truncations.append(section)


def _cap_item(text: str, limit: int, section: str, truncations: list[str]) -> str:
    if len(text) <= limit:
        return text
    _append_disclosure(truncations, section)
    return text[:limit] + "…"


def _cap_item_list(
    items: Sequence[str], limit: int, section: str, truncations: list[str]
) -> tuple[list[str], int]:
    """Cap a rendered item list; return the kept items and the number omitted."""
    omitted = 0
    if len(items) > limit:
        omitted = len(items) - limit
        items = items[:limit]
        _append_disclosure(truncations, section)
    return [_cap_item(item, DIGEST_MAX_ITEM_CHARS, section, truncations) for item in items], omitted


# --------------------------------------------------------------------------- #
# Notebook: typed entries, bounded size, append-only archive
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class NotebookEntry:
    """One immutable, single-line notebook entry.

    ``age`` counts wave/turn boundaries elapsed since the entry was written; it
    drives the deterministic decay of an ``aging`` entry's priority.
    """

    key: str
    tier: NotebookTier
    text: str
    age: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "key", _validate_identifier(self.key, "notebook.key"))
        object.__setattr__(self, "tier", _coerce_enum(NotebookTier, self.tier, "notebook.tier"))
        object.__setattr__(
            self,
            "text",
            _validate_line(self.text, "notebook.text", NOTEBOOK_MAX_ENTRY_CHARS),
        )
        age = _require_int(self.age, "notebook.age")
        if age < 0:
            raise OrchestratorMemoryError("notebook.age must not be negative")
        object.__setattr__(self, "age", age)

    def as_dict(self) -> dict[str, Any]:
        """Return an ordered, JSON-ready mapping."""
        return {"key": self.key, "tier": self.tier.value, "text": self.text, "age": self.age}


def effective_priority(entry: NotebookEntry) -> int:
    """Return the deterministic eviction priority of a notebook entry.

    ``pinned`` entries are permanent and always rank highest.  ``aging`` entries
    decay linearly with age, so a sufficiently old ``aging`` entry ranks below a
    fresh ``perishable`` one and is evicted first.  ``perishable`` entries are
    cleared at the wave/turn boundary regardless of size.
    """
    if entry.tier is NotebookTier.PINNED:
        return PINNED_PRIORITY
    if entry.tier is NotebookTier.AGING:
        return max(0, AGING_BASE_PRIORITY - AGING_DECAY_PER_AGE * entry.age)
    return PERISHABLE_PRIORITY


@dataclass(frozen=True)
class NotebookWrite:
    """The outcome of admitting one entry into the notebook."""

    admitted: NotebookEntry
    evicted: tuple[NotebookEntry, ...]
    total_chars: int
    entry_count: int


class NotebookArchive:
    """Append-only on-disk archive for exhausted notebook entries.

    The archive is never rewritten, never truncated and never read at startup
    for admission decisions: it exists so an evicted entry is preserved rather
    than lost.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def append(self, entry: NotebookEntry, *, reason: str, at: str) -> None:
        """Append one archived entry as a single JSON line and fsync it."""
        record = {
            "version": ORCHESTRATOR_MEMORY_VERSION,
            "reason": _validate_identifier(reason, "archive.reason"),
            "at": _validate_line(at, "archive.at", MAX_TEXT_LENGTH),
            "entry": entry.as_dict(),
        }
        payload = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(payload + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def read(self) -> tuple[dict[str, Any], ...]:
        """Return every archived record, in append order."""
        if not self.path.is_file():
            return ()
        records: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            parsed = json.loads(line)
            if not isinstance(parsed, dict):
                raise NotebookStoreError("archive record is not a JSON object")
            records.append(parsed)
        return tuple(records)

    def count(self) -> int:
        """Return the number of archived records."""
        return len(self.read())


class NotebookStore:
    """A strictly size-bounded notebook persisted under the memory root."""

    def __init__(
        self,
        path: Path,
        *,
        archive: NotebookArchive,
        now: Clock = _default_clock,
        max_chars: int = NOTEBOOK_MAX_CHARS,
        max_entries: int = NOTEBOOK_MAX_ENTRIES,
    ) -> None:
        self.path = Path(path)
        self.archive = archive
        self._now = now
        self.max_chars = _positive_int(max_chars, "notebook.max_chars")
        self.max_entries = _positive_int(max_entries, "notebook.max_entries")
        self._entries: dict[str, NotebookEntry] = {}
        self._order: list[str] = []
        self._load()

    # -- persistence ------------------------------------------------------- #

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise NotebookStoreError(f"notebook at {self.path} is unreadable: {exc}") from exc
        if not isinstance(payload, dict):
            raise NotebookStoreError(f"notebook at {self.path} is not a JSON object")
        raw_entries = payload.get("entries")
        if not isinstance(raw_entries, list):
            raise NotebookStoreError(f"notebook at {self.path} has no entries list")
        for raw in raw_entries:
            if not isinstance(raw, dict):
                raise NotebookStoreError(f"notebook at {self.path} has a non-object entry")
            try:
                entry = NotebookEntry(
                    key=raw["key"],
                    tier=raw["tier"],
                    text=raw["text"],
                    age=raw.get("age", 0),
                )
            except (KeyError, OrchestratorMemoryError) as exc:
                raise NotebookStoreError(
                    f"notebook at {self.path} has a malformed entry: {exc}"
                ) from exc
            if entry.key in self._entries:
                raise NotebookStoreError(f"notebook at {self.path} repeats key {entry.key!r}")
            self._entries[entry.key] = entry
            self._order.append(entry.key)
        if self.total_chars() > self.max_chars or len(self._entries) > self.max_entries:
            raise NotebookStoreError(
                f"notebook at {self.path} exceeds its configured bound and will not be "
                "silently truncated"
            )

    def _persist(self) -> None:
        payload = {
            "version": ORCHESTRATOR_MEMORY_VERSION,
            "entries": [self._entries[key].as_dict() for key in self._order],
        }
        _atomic_write_json(self.path, payload)

    # -- reads ------------------------------------------------------------- #

    def entries(self) -> tuple[NotebookEntry, ...]:
        """Return entries in deterministic read order (priority, then age, then insertion)."""
        rank = {key: index for index, key in enumerate(self._order)}
        ranked = sorted(
            self._entries.values(),
            key=lambda entry: (-effective_priority(entry), entry.age, rank[entry.key]),
        )
        return tuple(ranked)

    def get(self, key: str) -> NotebookEntry | None:
        """Return one entry by key, or ``None``."""
        return self._entries.get(key)

    def total_chars(self) -> int:
        """Return the total rendered character size of the notebook."""
        return sum(len(entry.text) for entry in self._entries.values())

    def entry_count(self) -> int:
        """Return the number of live entries."""
        return len(self._entries)

    def _over_budget(self, entries: Mapping[str, NotebookEntry]) -> bool:
        chars = sum(len(entry.text) for entry in entries.values())
        return chars > self.max_chars or len(entries) > self.max_entries

    @staticmethod
    def _lowest_priority(
        entries: Mapping[str, NotebookEntry], order: Sequence[str]
    ) -> NotebookEntry | None:
        """Return the lowest-priority candidate, deterministically."""
        if not entries:
            return None
        rank = {key: index for index, key in enumerate(order)}
        return min(
            entries.values(),
            key=lambda entry: (effective_priority(entry), -entry.age, rank[entry.key]),
        )

    def _archive(self, entry: NotebookEntry, reason: str) -> None:
        self.archive.append(entry, reason=reason, at=self._now())

    # -- writes ------------------------------------------------------------ #

    def put(self, entry: NotebookEntry) -> NotebookWrite:
        """Admit one entry, evicting lower-priority entries to stay within budget.

        ``pinned`` entries are never evicted.  If the incoming entry is itself
        the lowest-priority candidate, it is refused instead of displacing
        higher-priority state.  Evictions are planned on a copy first, so a
        refused write never leaves the notebook partially mutated.
        """
        if not isinstance(entry, NotebookEntry):
            raise OrchestratorMemoryError("put requires a NotebookEntry")
        candidates = dict(self._entries)
        order = [key for key in self._order if key != entry.key]
        candidates[entry.key] = entry
        order.append(entry.key)

        evicted: list[NotebookEntry] = []
        while self._over_budget(candidates):
            victim = self._lowest_priority(candidates, order)
            if victim is None or victim.key == entry.key:
                raise NotebookBudgetError(
                    f"entry {entry.key!r} cannot be admitted: it is the lowest-priority "
                    f"resident and the notebook is at its bound ({self.max_chars} chars, "
                    f"{self.max_entries} entries)"
                )
            del candidates[victim.key]
            order.remove(victim.key)
            evicted.append(victim)

        self._entries = candidates
        self._order = order
        self._persist()
        for victim in evicted:
            self._archive(victim, "budget_eviction")
        return NotebookWrite(
            admitted=entry,
            evicted=tuple(evicted),
            total_chars=self.total_chars(),
            entry_count=len(self._entries),
        )

    def clear_perishable(self) -> tuple[NotebookEntry, ...]:
        """Evict every perishable entry at a wave/turn boundary, archiving each."""
        doomed = [
            entry
            for entry in self._entries.values()
            if entry.tier is NotebookTier.PERISHABLE
        ]
        for entry in doomed:
            self._entries.pop(entry.key)
            self._order.remove(entry.key)
        if doomed:
            self._persist()
            for entry in doomed:
                self._archive(entry, "boundary_cleared")
        return tuple(doomed)

    def advance_age(self) -> tuple[NotebookEntry, ...]:
        """Age every ``aging`` entry by one boundary and persist the result."""
        aged: list[NotebookEntry] = []
        changed = False
        for key in self._order:
            entry = self._entries[key]
            if entry.tier is NotebookTier.AGING:
                updated = NotebookEntry(
                    key=entry.key, tier=entry.tier, text=entry.text, age=entry.age + 1
                )
                self._entries[key] = updated
                aged.append(updated)
                changed = True
        if changed:
            self._persist()
        return tuple(aged)


# --------------------------------------------------------------------------- #
# Startup digest
# --------------------------------------------------------------------------- #


class DigestSection(StrEnum):
    """The fixed digest section order; every section always renders."""

    PROJECT = "project"
    SINCE_LAST_SESSION = "since_last_session"
    RECENT_ACCEPTED_RUNS = "recent_accepted_runs"
    OPEN_BACKLOG = "open_backlog"
    FAILED_RUNS = "failed_runs"
    NEXT_ROADMAP_ITEM = "next_roadmap_item"
    OPEN_QUESTIONS = "open_questions"
    NOTEBOOK = "notebook"
    SESSION_STATE = "session_state"
    CONTEXT_STATUS = "context_status"


DIGEST_SECTION_ORDER: Final[tuple[DigestSection, ...]] = (
    DigestSection.PROJECT,
    DigestSection.SINCE_LAST_SESSION,
    DigestSection.RECENT_ACCEPTED_RUNS,
    DigestSection.OPEN_BACKLOG,
    DigestSection.FAILED_RUNS,
    DigestSection.NEXT_ROADMAP_ITEM,
    DigestSection.OPEN_QUESTIONS,
    DigestSection.NOTEBOOK,
    DigestSection.SESSION_STATE,
    DigestSection.CONTEXT_STATUS,
)

#: Deterministic reduction order once the digest exceeds its budget: least
#: operationally critical state is dropped first, and never an exact retry
#: command, a SHA, a section heading or an ``[ABSENT]`` marker.
DIGEST_REDUCTION_ORDER: Final[tuple[DigestSection, ...]] = (
    DigestSection.SINCE_LAST_SESSION,
    DigestSection.NOTEBOOK,
    DigestSection.OPEN_QUESTIONS,
    DigestSection.RECENT_ACCEPTED_RUNS,
    DigestSection.OPEN_BACKLOG,
)


@dataclass(frozen=True)
class AcceptedRun:
    """One recently accepted run, pinned to its accepted SHA."""

    run_id: str
    accepted_sha: str
    accepted_at: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _validate_identifier(self.run_id, "accepted_run.run_id"))
        object.__setattr__(
            self, "accepted_sha", _validate_sha(self.accepted_sha, "accepted_run.accepted_sha")
        )
        object.__setattr__(
            self,
            "accepted_at",
            _validate_line(self.accepted_at, "accepted_run.accepted_at", MAX_TEXT_LENGTH),
        )


@dataclass(frozen=True)
class BacklogItem:
    """One open backlog item."""

    item_id: str
    title: str
    state: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "item_id", _validate_identifier(self.item_id, "backlog.item_id"))
        object.__setattr__(
            self, "title", _validate_line(self.title, "backlog.title", MAX_TEXT_LENGTH)
        )
        object.__setattr__(
            self, "state", _validate_line(self.state, "backlog.state", MAX_TEXT_LENGTH)
        )


@dataclass(frozen=True)
class FailedRun:
    """One failed run with the exact command that retries or continues it."""

    run_id: str
    reason: str
    retry_command: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _validate_identifier(self.run_id, "failed_run.run_id"))
        object.__setattr__(
            self, "reason", _validate_line(self.reason, "failed_run.reason", MAX_TEXT_LENGTH)
        )
        object.__setattr__(
            self,
            "retry_command",
            _validate_line(self.retry_command, "failed_run.retry_command", MAX_COMMAND_LENGTH),
        )


@dataclass(frozen=True)
class RoadmapItem:
    """The next roadmap item, pinned to its state."""

    item_id: str
    title: str
    state: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "item_id", _validate_identifier(self.item_id, "roadmap.item_id"))
        object.__setattr__(
            self, "title", _validate_line(self.title, "roadmap.title", MAX_TEXT_LENGTH)
        )
        object.__setattr__(
            self, "state", _validate_line(self.state, "roadmap.state", MAX_TEXT_LENGTH)
        )


@dataclass(frozen=True)
class OpenQuestion:
    """One open owner question blocking a dependent branch of work."""

    question_id: str
    question: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "question_id", _validate_identifier(self.question_id, "question.question_id")
        )
        object.__setattr__(
            self, "question", _validate_line(self.question, "question.question", MAX_TEXT_LENGTH)
        )


@dataclass(frozen=True)
class DigestSessionState:
    """The session view the digest reports (writer or forced read-only)."""

    session_id: str | None
    mode: SessionMode | None
    lock_holder_session: str | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "session_id", _optional_identifier(self.session_id, "session_state.session_id")
        )
        if self.mode is not None:
            object.__setattr__(
                self, "mode", _coerce_enum(SessionMode, self.mode, "session_state.mode")
            )
        object.__setattr__(
            self,
            "lock_holder_session",
            _optional_identifier(self.lock_holder_session, "session_state.lock_holder_session"),
        )


@dataclass(frozen=True)
class DigestContextStatus:
    """Measured Orchestrator context and whether the rotation trigger is latched."""

    usage: ContextUsage | None
    rotation_latched: bool

    def __post_init__(self) -> None:
        if self.usage is not None and not isinstance(self.usage, ContextUsage):
            raise OrchestratorMemoryError("context_status.usage must be a ContextUsage or None")
        object.__setattr__(
            self,
            "rotation_latched",
            _require_bool(self.rotation_latched, "context_status.rotation_latched"),
        )


@dataclass(frozen=True)
class DigestFacts:
    """Every explicit, typed fact the startup digest may render.

    Missing state is passed as ``None`` or an empty sequence and renders as an
    explicit ``[ABSENT]`` marker; it is never omitted and never fabricated.
    """

    project_id: str | None
    plan_reference: str | None
    base_sha: str | None
    last_session_at: str | None
    status_since_last_session: Sequence[str] = ()
    recent_accepted_runs: Sequence[AcceptedRun] = ()
    open_backlog: Sequence[BacklogItem] = ()
    failed_runs: Sequence[FailedRun] = ()
    next_roadmap_item: RoadmapItem | None = None
    open_questions: Sequence[OpenQuestion] = ()
    notebook: Sequence[NotebookEntry] = ()
    session: DigestSessionState | None = None
    context: DigestContextStatus | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "project_id", _optional_identifier(self.project_id, "facts.project_id")
        )
        object.__setattr__(
            self,
            "plan_reference",
            _optional_line(self.plan_reference, "facts.plan_reference", MAX_TEXT_LENGTH),
        )
        object.__setattr__(
            self, "base_sha", _validate_optional_sha(self.base_sha, "facts.base_sha")
        )
        object.__setattr__(
            self,
            "last_session_at",
            _optional_line(self.last_session_at, "facts.last_session_at", MAX_TEXT_LENGTH),
        )
        object.__setattr__(
            self,
            "status_since_last_session",
            _validate_lines(self.status_since_last_session, "facts.status", MAX_DIGEST_ITEMS),
        )
        object.__setattr__(
            self,
            "recent_accepted_runs",
            _require_items(
                self.recent_accepted_runs,
                "facts.recent_accepted_runs",
                MAX_DIGEST_ITEMS,
                AcceptedRun,
            ),
        )
        object.__setattr__(
            self,
            "open_backlog",
            _require_items(self.open_backlog, "facts.open_backlog", MAX_DIGEST_ITEMS, BacklogItem),
        )
        object.__setattr__(
            self,
            "failed_runs",
            _require_items(self.failed_runs, "facts.failed_runs", MAX_DIGEST_ITEMS, FailedRun),
        )
        if self.next_roadmap_item is not None and not isinstance(
            self.next_roadmap_item, RoadmapItem
        ):
            raise OrchestratorMemoryError("facts.next_roadmap_item must be a RoadmapItem or None")
        object.__setattr__(
            self,
            "open_questions",
            _require_items(
                self.open_questions,
                "facts.open_questions",
                MAX_DIGEST_ITEMS,
                OpenQuestion,
            ),
        )
        object.__setattr__(
            self,
            "notebook",
            _require_items(self.notebook, "facts.notebook", MAX_DIGEST_ITEMS, NotebookEntry),
        )
        if self.session is not None and not isinstance(self.session, DigestSessionState):
            raise OrchestratorMemoryError("facts.session must be a DigestSessionState or None")
        if self.context is not None and not isinstance(self.context, DigestContextStatus):
            raise OrchestratorMemoryError("facts.context must be a DigestContextStatus or None")


@dataclass(frozen=True)
class StartupDigest:
    """A deterministic, budgeted, fact-only startup digest."""

    sections: tuple[DigestSection, ...]
    body: str
    char_cost: int
    token_cost: int
    truncations: tuple[str, ...]
    version: str = ORCHESTRATOR_MEMORY_VERSION

    def as_dict(self) -> dict[str, Any]:
        """Return an ordered, JSON-ready projection with a fixed key order."""
        return {
            "sections": [section.value for section in self.sections],
            "body": self.body,
            "char_cost": self.char_cost,
            "token_cost": self.token_cost,
            "truncations": list(self.truncations),
            "version": self.version,
        }

    def canonical_json(self) -> str:
        """Return the digest as deterministic compact JSON."""
        return _compact_json(self.as_dict())


@dataclass
class _DigestDraft:
    """Mutable assembly state for :func:`build_startup_digest`."""

    lines: dict[DigestSection, list[str]]
    reducible: dict[DigestSection, list[bool]]
    omitted: dict[DigestSection, int]

    def add(self, section: DigestSection, text: str, *, red: bool = False) -> None:
        self.lines[section].append(text)
        self.reducible[section].append(red)

    def render(self) -> str:
        chunks = [f"# Orchestrator startup digest ({ORCHESTRATOR_MEMORY_VERSION})"]
        for section in DIGEST_SECTION_ORDER:
            chunks.append(f"## {section.value}")
            chunks.extend(self.lines[section])
            if self.omitted[section]:
                chunks.append(TRUNCATED_TEMPLATE.format(count=self.omitted[section]))
        return "\n".join(chunks)

    def reduce_once(self, truncations: list[str]) -> bool:
        for section in DIGEST_REDUCTION_ORDER:
            section_lines = self.lines[section]
            flags = self.reducible[section]
            for index in range(len(section_lines) - 1, -1, -1):
                if flags[index]:
                    del section_lines[index]
                    del flags[index]
                    self.omitted[section] += 1
                    _append_disclosure(truncations, section.value)
                    return True
        return False


def build_startup_digest(facts: DigestFacts) -> StartupDigest:
    """Build the deterministic startup digest within its character/token budget.

    Every section in :data:`DIGEST_SECTION_ORDER` always renders.  A section
    with no state renders :data:`ABSENT_MARKER`; a section whose state was
    reduced to fit the budget renders a ``[TRUNCATED n more]`` marker instead,
    so absence and truncation are never confused.  Exact retry commands, SHAs
    and section headings are never truncated or dropped: if the irreducible
    digest cannot fit, the call raises :class:`DigestBudgetError`.
    """
    if not isinstance(facts, DigestFacts):
        raise OrchestratorMemoryError("build_startup_digest requires DigestFacts")

    truncations: list[str] = []
    draft = _DigestDraft(
        lines={section: [] for section in DIGEST_SECTION_ORDER},
        reducible={section: [] for section in DIGEST_SECTION_ORDER},
        omitted=dict.fromkeys(DIGEST_SECTION_ORDER, 0),
    )

    def cap(text: str, section: DigestSection) -> str:
        return _cap_item(text, DIGEST_MAX_ITEM_CHARS, section.value, truncations)

    def cap_list(
        items: Sequence[str], section: DigestSection
    ) -> tuple[list[str], int]:
        return _cap_item_list(
            items, DIGEST_MAX_ITEMS_PER_SECTION, section.value, truncations
        )

    # project -- always present, never dropped.
    draft.add(DigestSection.PROJECT, f"project: {facts.project_id or ABSENT_MARKER}")
    draft.add(DigestSection.PROJECT, f"plan: {facts.plan_reference or ABSENT_MARKER}")
    draft.add(DigestSection.PROJECT, f"base: {facts.base_sha or ABSENT_MARKER}")

    # since last session -- the status summary that doubles as the resume summary.
    draft.add(
        DigestSection.SINCE_LAST_SESSION,
        f"last session: {facts.last_session_at or ABSENT_MARKER}",
    )
    if facts.status_since_last_session:
        draft.add(DigestSection.SINCE_LAST_SESSION, "status since the last session:", red=False)
        statuses, omitted = cap_list(
            facts.status_since_last_session, DigestSection.SINCE_LAST_SESSION
        )
        draft.omitted[DigestSection.SINCE_LAST_SESSION] += omitted
        for status in statuses:
            draft.add(DigestSection.SINCE_LAST_SESSION, f"- {status}", red=True)
    else:
        draft.add(
            DigestSection.SINCE_LAST_SESSION,
            f"{ABSENT_MARKER} status since the last session",
        )

    # recent accepted runs.
    if facts.recent_accepted_runs:
        rendered = [
            f"- run {run.run_id} accepted {run.accepted_sha} at {run.accepted_at}"
            for run in facts.recent_accepted_runs
        ]
        runs, omitted = cap_list(rendered, DigestSection.RECENT_ACCEPTED_RUNS)
        draft.omitted[DigestSection.RECENT_ACCEPTED_RUNS] += omitted
        for line in runs:
            draft.add(DigestSection.RECENT_ACCEPTED_RUNS, line, red=True)
    else:
        draft.add(DigestSection.RECENT_ACCEPTED_RUNS, ABSENT_MARKER)

    # open backlog.
    if facts.open_backlog:
        rendered = [
            f"- {item.item_id} [{cap(item.state, DigestSection.OPEN_BACKLOG)}] "
            f"{cap(item.title, DigestSection.OPEN_BACKLOG)}"
            for item in facts.open_backlog
        ]
        backlog, omitted = cap_list(rendered, DigestSection.OPEN_BACKLOG)
        draft.omitted[DigestSection.OPEN_BACKLOG] += omitted
        for line in backlog:
            draft.add(DigestSection.OPEN_BACKLOG, line, red=True)
    else:
        draft.add(DigestSection.OPEN_BACKLOG, ABSENT_MARKER)

    # failed runs -- the exact retry command is mandatory and never truncated.
    if facts.failed_runs:
        for run in facts.failed_runs:
            draft.add(
                DigestSection.FAILED_RUNS,
                f"- run {run.run_id} failed: {cap(run.reason, DigestSection.FAILED_RUNS)}",
            )
            draft.add(DigestSection.FAILED_RUNS, f"  exact retry: {run.retry_command}")
    else:
        draft.add(DigestSection.FAILED_RUNS, ABSENT_MARKER)

    # next roadmap item -- mandated resume content, never dropped.
    item = facts.next_roadmap_item
    draft.add(DigestSection.NEXT_ROADMAP_ITEM, f"item: {item.item_id if item else ABSENT_MARKER}")
    draft.add(
        DigestSection.NEXT_ROADMAP_ITEM,
        f"title: {cap(item.title, DigestSection.NEXT_ROADMAP_ITEM) if item else ABSENT_MARKER}",
    )
    draft.add(
        DigestSection.NEXT_ROADMAP_ITEM,
        f"state: {cap(item.state, DigestSection.NEXT_ROADMAP_ITEM) if item else ABSENT_MARKER}",
    )

    # open questions.
    if facts.open_questions:
        rendered = [
            f"- {question.question_id} {cap(question.question, DigestSection.OPEN_QUESTIONS)}"
            for question in facts.open_questions
        ]
        questions, omitted = cap_list(rendered, DigestSection.OPEN_QUESTIONS)
        draft.omitted[DigestSection.OPEN_QUESTIONS] += omitted
        for line in questions:
            draft.add(DigestSection.OPEN_QUESTIONS, line, red=True)
    else:
        draft.add(DigestSection.OPEN_QUESTIONS, ABSENT_MARKER)

    # notebook -- pinned entries are never reduced away.
    if facts.notebook:
        for entry in facts.notebook:
            draft.add(
                DigestSection.NOTEBOOK,
                f"- [{entry.tier.value}] {entry.key}: {cap(entry.text, DigestSection.NOTEBOOK)}",
                red=entry.tier is not NotebookTier.PINNED,
            )
    else:
        draft.add(DigestSection.NOTEBOOK, ABSENT_MARKER)

    # session state.
    session = facts.session
    draft.add(
        DigestSection.SESSION_STATE,
        f"mode: {session.mode.value if session and session.mode else ABSENT_MARKER}",
    )
    draft.add(
        DigestSection.SESSION_STATE,
        f"session: {(session.session_id if session else None) or ABSENT_MARKER}",
    )
    draft.add(
        DigestSection.SESSION_STATE,
        f"lock holder: {(session.lock_holder_session if session else None) or ABSENT_MARKER}",
    )

    # context status -- the latched rotation trigger is always reported.
    context = facts.context
    if context is not None and context.usage is not None:
        usage = context.usage
        percent = usage.used_tokens * 100 // usage.total_tokens
        draft.add(
            DigestSection.CONTEXT_STATUS,
            f"usage: {usage.used_tokens}/{usage.total_tokens} ({percent}%)",
        )
        draft.add(
            DigestSection.CONTEXT_STATUS,
            f"above {CONTEXT_ROTATION_THRESHOLD_PERCENT}%: "
            f"{usage.exceeds_percent(CONTEXT_ROTATION_THRESHOLD_PERCENT)}",
        )
    else:
        draft.add(DigestSection.CONTEXT_STATUS, f"usage: {ABSENT_MARKER}")
    draft.add(
        DigestSection.CONTEXT_STATUS,
        f"rotation latched: {context.rotation_latched if context is not None else ABSENT_MARKER}",
    )

    while True:
        body = draft.render()
        if len(body) <= DIGEST_MAX_CHARS and estimate_tokens(body) <= DIGEST_MAX_TOKENS:
            break
        if not draft.reduce_once(truncations):
            raise DigestBudgetError(
                "the irreducible startup digest exceeds its budget of "
                f"{DIGEST_MAX_CHARS} characters / {DIGEST_MAX_TOKENS} tokens"
            )

    return StartupDigest(
        sections=DIGEST_SECTION_ORDER,
        body=body,
        char_cost=len(body),
        token_cost=estimate_tokens(body),
        truncations=tuple(truncations),
    )


# --------------------------------------------------------------------------- #
# Session lock
# --------------------------------------------------------------------------- #


def _atomic_write_json(path: Path, payload: object) -> None:
    """Write ``payload`` as compact JSON to ``path`` atomically and privately."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    text = json.dumps(payload, ensure_ascii=False, sort_keys=False, separators=(",", ":"))
    temp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    descriptor = os.open(temp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    os.replace(temp, path)


@dataclass(frozen=True)
class LockHolder:
    """The persisted identity of the single-writer lock holder."""

    session_id: str
    identity: ProcessIdentity | None
    acquired_at: str
    version: str = ORCHESTRATOR_MEMORY_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "session_id", _validate_identifier(self.session_id, "lock.session_id")
        )
        if self.identity is not None and not isinstance(self.identity, ProcessIdentity):
            raise OrchestratorMemoryError("lock.identity must be a ProcessIdentity or None")
        object.__setattr__(
            self,
            "acquired_at",
            _validate_line(self.acquired_at, "lock.acquired_at", MAX_TEXT_LENGTH),
        )

    @property
    def pid(self) -> int:
        """Return the recorded process id, or 0 when no identity was recorded."""
        return self.identity.pid if self.identity is not None else 0

    def as_dict(self) -> dict[str, Any]:
        """Return an ordered, JSON-ready mapping."""
        return {
            "version": self.version,
            "session_id": self.session_id,
            "acquired_at": self.acquired_at,
            "identity": None if self.identity is None else self.identity.to_dict(),
        }

    def to_json(self) -> str:
        """Return the holder as compact JSON."""
        return _compact_json(self.as_dict())

    @classmethod
    def from_json(cls, raw: str) -> LockHolder:
        """Rebuild a holder from :meth:`to_json` output, failing closed."""
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SessionLockUnsafeError(f"lock file is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise SessionLockUnsafeError("lock file is not a JSON object")
        raw_identity = payload.get("identity")
        identity: ProcessIdentity | None = None
        if payload.get("identity") is not None:
            if not isinstance(raw_identity, dict):
                raise SessionLockUnsafeError("lock file identity is not an object")
            identity = ProcessIdentity.from_dict(raw_identity)
            if identity is None:
                raise SessionLockUnsafeError("lock file identity is unparseable")
        try:
            return cls(
                session_id=payload["session_id"],
                identity=identity,
                acquired_at=payload["acquired_at"],
                version=str(payload.get("version", ORCHESTRATOR_MEMORY_VERSION)),
            )
        except (KeyError, OrchestratorMemoryError) as exc:
            raise SessionLockUnsafeError(f"lock file is malformed: {exc}") from exc


@dataclass(frozen=True)
class LockAcquisition:
    """The result of acquiring the single-writer lock."""

    holder: LockHolder
    reason: str
    reclaimed_from: str | None = None

    @property
    def reclaimed(self) -> bool:
        """True when this acquisition displaced a provably dead holder."""
        return self.reclaimed_from is not None


class OrchestratorSessionLock:
    """A single-writer advisory lock with verified stale-lock reclamation."""

    def __init__(
        self,
        path: Path,
        *,
        probe: LivenessProbe = probe_liveness,
        now: Clock = _default_clock,
    ) -> None:
        self.path = Path(path)
        self._probe = probe
        self._now = now

    def read_holder(self) -> LockHolder | None:
        """Return the current holder, or ``None`` when no lock file exists.

        A present but unreadable lock file raises :class:`SessionLockUnsafeError`
        rather than being treated as absent.
        """
        if not self.path.is_file():
            return None
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SessionLockUnsafeError(f"lock file at {self.path} is unreadable: {exc}") from exc
        if raw.strip() == "":
            raise SessionLockUnsafeError(f"lock file at {self.path} is empty")
        return LockHolder.from_json(raw)

    def holder_liveness(self) -> Liveness:
        """Return the verified liveness of the recorded holder incarnation."""
        holder = self.read_holder()
        if holder is None:
            return Liveness.DEAD
        return self._probe(holder.identity)

    def acquire(
        self, *, session_id: str, identity: ProcessIdentity | None = None
    ) -> LockAcquisition:
        """Acquire the writer lock, reclaiming only a provably dead holder.

        * no lock file -> acquire;
        * the same session already holding it -> idempotent re-acquire;
        * a live holder -> :class:`SessionLockedError` (the caller is read-only);
        * an undecidable holder -> :class:`SessionLockUnsafeError` (never reclaimed);
        * a provably dead holder -> reclaim and record what was displaced.
        """
        session = _validate_identifier(session_id, "lock.session_id")
        own_identity = identity if identity is not None else ProcessIdentity.for_self()
        holder = LockHolder(session_id=session, identity=own_identity, acquired_at=self._now())

        existing = self.read_holder()
        reclaimed_from: str | None = None
        if existing is not None:
            if existing.session_id == session:
                return LockAcquisition(holder=existing, reason="already_held_by_self")
            liveness = self._probe(existing.identity)
            if liveness is Liveness.LIVE:
                raise SessionLockedError(existing)
            if liveness is Liveness.UNKNOWN:
                raise SessionLockUnsafeError(
                    f"lock holder {existing.session_id!r} (pid={existing.pid}) cannot be "
                    "verified as dead; the lock is never reclaimed on UNKNOWN liveness"
                )
            reclaimed_from = existing.session_id

        self._write_exclusive(holder)
        return LockAcquisition(
            holder=holder,
            reason="acquired_after_stale_reclaim" if reclaimed_from else "acquired",
            reclaimed_from=reclaimed_from,
        )

    def _write_exclusive(self, holder: LockHolder) -> None:
        """Publish ``holder`` as the lock content, replacing a stale file atomically."""
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = holder.to_json() + "\n"
        temp = self.path.with_name(f".{self.path.name}.tmp-{os.getpid()}")
        descriptor = os.open(temp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
        os.replace(temp, self.path)
        os.chmod(self.path, 0o600)

    def release(self, *, session_id: str) -> bool:
        """Release the lock, but only when ``session_id`` is the recorded holder."""
        session = _validate_identifier(session_id, "lock.session_id")
        existing = self.read_holder()
        if existing is None:
            return False
        if existing.session_id != session:
            return False
        self.path.unlink()
        return True


# --------------------------------------------------------------------------- #
# Rotation trigger, handoff and crash recovery
# --------------------------------------------------------------------------- #


def is_full_autonomy(mode: AuthorityMode) -> bool:
    """Return True for the authority presets that grant full autonomy.

    ``full_autonomy`` and ``full_autonomy_publish`` differ only by the
    publication grant (see ``saberops.authority``), so both may self-split.
    """
    if not isinstance(mode, AuthorityMode):
        raise OrchestratorMemoryError("mode must be an AuthorityMode")
    return mode in (AuthorityMode.FULL_AUTONOMY, AuthorityMode.FULL_AUTONOMY_PUBLISH)


@dataclass(frozen=True)
class ChecklistProgress:
    """Work remaining, from the task's *starting* checklist.

    ``open_items`` may be ``None``: an unknown count is treated as *more than*
    the continue threshold, never as finished work.
    """

    total_items: int
    open_items: int | None

    def __post_init__(self) -> None:
        total = _require_int(self.total_items, "progress.total_items")
        if total <= 0:
            raise OrchestratorMemoryError("progress.total_items must be strictly positive")
        object.__setattr__(self, "total_items", total)
        if self.open_items is not None:
            open_items = _require_int(self.open_items, "progress.open_items")
            if open_items < 0:
                raise OrchestratorMemoryError("progress.open_items must not be negative")
            if open_items > total:
                raise OrchestratorMemoryError("progress.open_items must not exceed total_items")
            object.__setattr__(self, "open_items", open_items)

    @property
    def remaining_ratio(self) -> float | None:
        """Return ``open_items / total_items``, or ``None`` when unknown."""
        if self.open_items is None:
            return None
        return self.open_items / self.total_items

    @property
    def remaining_above_threshold(self) -> bool:
        """True when work remains above the continue threshold, unknown included."""
        if self.open_items is None:
            return True
        return self.open_items * 100 > self.total_items * CONTINUE_REMAINING_PERCENT


@dataclass(frozen=True)
class ContextTrigger:
    """A latched ``> 50 %`` context trigger that compaction cannot hide.

    Once latched it stays latched, even if a later observation measures *less*
    context, until :meth:`reset` is called after an actual rotation.
    """

    latched: bool = False
    peak_used_tokens: int | None = None
    peak_total_tokens: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "latched", _require_bool(self.latched, "trigger.latched"))
        used = self.peak_used_tokens
        total = self.peak_total_tokens
        if used is None and total is None:
            return
        if used is None or total is None:
            raise OrchestratorMemoryError(
                "trigger peak usage needs both peak_used_tokens and peak_total_tokens"
            )
        ContextUsage(used_tokens=used, total_tokens=total)
        object.__setattr__(self, "peak_used_tokens", used)
        object.__setattr__(self, "peak_total_tokens", total)

    @classmethod
    def initial(cls) -> ContextTrigger:
        """Return an unlatched trigger with no recorded peak."""
        return cls()

    @property
    def peak_usage(self) -> ContextUsage | None:
        """Return the recorded peak observation, or ``None``."""
        if self.peak_used_tokens is None or self.peak_total_tokens is None:
            return None
        return ContextUsage(
            used_tokens=self.peak_used_tokens, total_tokens=self.peak_total_tokens
        )

    def observe(self, usage: ContextUsage | None) -> ContextTrigger:
        """Fold one measured observation into the trigger (monotone latch)."""
        if usage is None:
            return self
        if not isinstance(usage, ContextUsage):
            raise OrchestratorMemoryError("observe requires a ContextUsage or None")
        latched = self.latched or usage.exceeds_percent(CONTEXT_ROTATION_THRESHOLD_PERCENT)
        peak = self.peak_usage
        more_than_peak = (
            peak is not None
            and usage.used_tokens * peak.total_tokens > peak.used_tokens * usage.total_tokens
        )
        if peak is None or more_than_peak:
            peak = usage
        return ContextTrigger(
            latched=latched,
            peak_used_tokens=peak.used_tokens,
            peak_total_tokens=peak.total_tokens,
        )

    def reset(self) -> ContextTrigger:
        """Clear the latch after a real rotation; the only way to unlatch."""
        return ContextTrigger.initial()

    def as_dict(self) -> dict[str, Any]:
        """Return an ordered, JSON-ready mapping."""
        return {
            "latched": self.latched,
            "peak_used_tokens": self.peak_used_tokens,
            "peak_total_tokens": self.peak_total_tokens,
        }


@dataclass(frozen=True)
class OrchestratorHandoff:
    """A durable, deterministic handoff record for a rotated Orchestrator session."""

    handoff_id: str
    task_id: str
    run_id: str
    wave_id: str
    decision_point_id: str
    from_session_id: str
    total_items: int
    open_items: int | None
    open_item_ids: tuple[str, ...]
    remaining_ratio: float | None
    used_tokens: int | None
    total_tokens: int | None
    follow_up_task_ids: tuple[str, ...]
    reason: str
    version: str = ORCHESTRATOR_MEMORY_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "handoff_id", _validate_identifier(self.handoff_id, "handoff.handoff_id")
        )
        object.__setattr__(self, "task_id", _validate_identifier(self.task_id, "handoff.task_id"))
        object.__setattr__(self, "run_id", _validate_identifier(self.run_id, "handoff.run_id"))
        object.__setattr__(self, "wave_id", _validate_identifier(self.wave_id, "handoff.wave_id"))
        object.__setattr__(
            self,
            "decision_point_id",
            _validate_identifier(self.decision_point_id, "handoff.decision_point_id"),
        )
        object.__setattr__(
            self,
            "from_session_id",
            _validate_identifier(self.from_session_id, "handoff.from_session_id"),
        )
        ChecklistProgress(total_items=self.total_items, open_items=self.open_items)
        object.__setattr__(
            self,
            "open_item_ids",
            tuple(
                _validate_identifier(item_id, f"handoff.open_item_ids[{index}]")
                for index, item_id in enumerate(
                    _require_sequence(self.open_item_ids, "handoff.open_item_ids", MAX_DIGEST_ITEMS)
                )
            ),
        )
        object.__setattr__(
            self,
            "follow_up_task_ids",
            tuple(
                _validate_identifier(item_id, f"handoff.follow_up_task_ids[{index}]")
                for index, item_id in enumerate(
                    _require_sequence(
                        self.follow_up_task_ids, "handoff.follow_up_task_ids", MAX_DIGEST_ITEMS
                    )
                )
            ),
        )
        object.__setattr__(
            self, "reason", _validate_line(self.reason, "handoff.reason", MAX_TEXT_LENGTH)
        )

    def as_dict(self) -> dict[str, Any]:
        """Return an ordered, JSON-ready mapping with a fixed key order."""
        return {
            "version": self.version,
            "handoff_id": self.handoff_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "wave_id": self.wave_id,
            "decision_point_id": self.decision_point_id,
            "from_session_id": self.from_session_id,
            "total_items": self.total_items,
            "open_items": self.open_items,
            "open_item_ids": list(self.open_item_ids),
            "remaining_ratio": self.remaining_ratio,
            "used_tokens": self.used_tokens,
            "total_tokens": self.total_tokens,
            "follow_up_task_ids": list(self.follow_up_task_ids),
            "reason": self.reason,
        }

    def canonical_json(self) -> str:
        """Return the handoff as deterministic compact JSON."""
        return _compact_json(self.as_dict())


@dataclass(frozen=True)
class RotationDecision:
    """Deterministic advice for a latched context trigger at a turn boundary."""

    action: RotationAction
    latched: bool
    remaining_known: bool
    remaining_ratio: float | None
    self_split: bool
    owner_question_required: bool
    follow_up_task_ids: tuple[str, ...]
    handoff: OrchestratorHandoff | None
    reasons: tuple[str, ...]
    version: str = ORCHESTRATOR_MEMORY_VERSION

    def as_dict(self) -> dict[str, Any]:
        """Return an ordered, JSON-ready projection with a fixed key order."""
        return {
            "action": self.action.value,
            "latched": self.latched,
            "remaining_known": self.remaining_known,
            "remaining_ratio": self.remaining_ratio,
            "self_split": self.self_split,
            "owner_question_required": self.owner_question_required,
            "follow_up_task_ids": list(self.follow_up_task_ids),
            "handoff": None if self.handoff is None else self.handoff.as_dict(),
            "reasons": list(self.reasons),
            "version": self.version,
        }

    def canonical_json(self) -> str:
        """Return the decision as deterministic compact JSON."""
        return _compact_json(self.as_dict())


def handoff_id_for(run_id: str, wave_id: str, decision_point_id: str) -> str:
    """Return the stable handoff id for one rotated decision point."""
    return (
        f"handoff-{_validate_identifier(run_id, 'run_id')}"
        f"-{_validate_identifier(wave_id, 'wave_id')}"
        f"-{_validate_identifier(decision_point_id, 'decision_point_id')}"
    )


def _split_open_items(
    task_id: str, open_item_ids: Sequence[str], *, max_items_per_follow_up: int
) -> tuple[str, ...]:
    """Split open checklist items into bounded, handoff-started follow-up task ids."""
    limit = _positive_int(max_items_per_follow_up, "max_items_per_follow_up")
    if not open_item_ids:
        return ()
    return tuple(
        f"{task_id}-h{index // limit + 1}"
        for index in range(0, len(open_item_ids), limit)
    )


def decide_rotation(
    *,
    trigger: ContextTrigger,
    progress: ChecklistProgress,
    mode: AuthorityMode,
    task_id: str,
    run_id: str,
    wave_id: str,
    decision_point_id: str,
    session_id: str,
    open_item_ids: Sequence[str] = (),
    reason: str = "context_over_half_window",
    max_items_per_follow_up: int = 8,
) -> RotationDecision:
    """Decide whether to finish in the current session or rotate with a handoff.

    * an unlatched trigger always continues in the current session;
    * with at most :data:`MAX_REMAINING_RATIO` of the checklist left, the same
      session finishes the work (a rotation would waste more than it saves);
    * otherwise the session rotates and a durable handoff is produced.  In the
      full-autonomy presets the Orchestrator self-splits the task into
      handoff-started follow-ups; in every other mode it raises an owner
      question instead.
    """
    if not isinstance(trigger, ContextTrigger):
        raise OrchestratorMemoryError("decide_rotation requires a ContextTrigger")
    if not isinstance(progress, ChecklistProgress):
        raise OrchestratorMemoryError("decide_rotation requires ChecklistProgress")
    if not isinstance(mode, AuthorityMode):
        mode = _coerce_enum(AuthorityMode, mode, "mode")
    task = _validate_identifier(task_id, "task_id")
    run = _validate_identifier(run_id, "run_id")
    wave = _validate_identifier(wave_id, "wave_id")
    point = _validate_identifier(decision_point_id, "decision_point_id")
    session = _validate_identifier(session_id, "session_id")
    line = _validate_line(reason, "reason", MAX_TEXT_LENGTH)
    items = tuple(
        _validate_identifier(item_id, f"open_item_ids[{index}]")
        for index, item_id in enumerate(
            _require_sequence(open_item_ids, "open_item_ids", MAX_DIGEST_ITEMS)
        )
    )

    if not trigger.latched:
        return RotationDecision(
            action=RotationAction.CONTINUE_CURRENT_SESSION,
            latched=False,
            remaining_known=progress.open_items is not None,
            remaining_ratio=progress.remaining_ratio,
            self_split=False,
            owner_question_required=False,
            follow_up_task_ids=(),
            handoff=None,
            reasons=(
                f"context trigger is below {CONTEXT_ROTATION_THRESHOLD_PERCENT}%; keep the session",
            ),
        )

    if not progress.remaining_above_threshold:
        return RotationDecision(
            action=RotationAction.CONTINUE_CURRENT_SESSION,
            latched=True,
            remaining_known=True,
            remaining_ratio=progress.remaining_ratio,
            self_split=False,
            owner_question_required=False,
            follow_up_task_ids=(),
            handoff=None,
            reasons=(
                f"only {progress.open_items}/{progress.total_items} checklist items remain "
                f"(at most {int(MAX_REMAINING_RATIO * 100)}%); finish in the current session",
            ),
        )

    self_split = is_full_autonomy(mode)
    follow_ups = (
        _split_open_items(task, items, max_items_per_follow_up=max_items_per_follow_up)
        if self_split
        else ()
    )
    peak = trigger.peak_usage
    ratio = progress.remaining_ratio
    handoff = OrchestratorHandoff(
        handoff_id=handoff_id_for(run, wave, point),
        task_id=task,
        run_id=run,
        wave_id=wave,
        decision_point_id=point,
        from_session_id=session,
        total_items=progress.total_items,
        open_items=progress.open_items,
        open_item_ids=items,
        remaining_ratio=ratio,
        used_tokens=peak.used_tokens if peak is not None else None,
        total_tokens=peak.total_tokens if peak is not None else None,
        follow_up_task_ids=follow_ups,
        reason=line,
    )
    remaining_text = (
        f"{progress.open_items}/{progress.total_items}"
        if progress.open_items is not None
        else "unknown (treated as more than the continue threshold)"
    )
    if self_split:
        reasons = (
            f"context trigger is latched above {CONTEXT_ROTATION_THRESHOLD_PERCENT}% and "
            f"checklist work remains ({remaining_text}); rotate with a handoff",
            "full autonomy: the Orchestrator self-splits the task into handoff-started follow-ups",
        )
    else:
        reasons = (
            f"context trigger is latched above {CONTEXT_ROTATION_THRESHOLD_PERCENT}% and "
            f"checklist work remains ({remaining_text}); rotate with a handoff",
            f"mode {mode.value} is not full autonomy: raise an owner question before splitting",
        )
    return RotationDecision(
        action=RotationAction.ROTATE_SESSION,
        latched=True,
        remaining_known=progress.open_items is not None,
        remaining_ratio=ratio,
        self_split=self_split,
        owner_question_required=not self_split,
        follow_up_task_ids=follow_ups,
        handoff=handoff,
        reasons=reasons,
    )


class HandoffStore:
    """Durable, idempotent storage for Orchestrator handoff records."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def path_for(self, handoff_id: str) -> Path:
        """Return the on-disk path of one handoff record."""
        return self.root / f"{_validate_identifier(handoff_id, 'handoff_id')}.json"

    def record(self, handoff: OrchestratorHandoff) -> Path:
        """Persist ``handoff`` exactly once and return its path.

        Re-recording identical content is a no-op (idempotent replay after a
        crash).  Re-recording *different* content under the same id is refused:
        a durable handoff record is never silently overwritten.
        """
        if not isinstance(handoff, OrchestratorHandoff):
            raise OrchestratorMemoryError("record requires an OrchestratorHandoff")
        path = self.path_for(handoff.handoff_id)
        if path.is_file():
            existing = self.load(handoff.handoff_id)
            if existing.canonical_json() == handoff.canonical_json():
                return path
            raise HandoffConflictError(
                f"handoff {handoff.handoff_id!r} already exists with different content"
            )
        _atomic_write_json(path, handoff.as_dict())
        return path

    def load(self, handoff_id: str) -> OrchestratorHandoff:
        """Load one persisted handoff record, failing closed on corruption."""
        path = self.path_for(handoff_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise OrchestratorMemoryError(f"handoff {handoff_id!r} is unreadable: {exc}") from exc
        if not isinstance(payload, dict):
            raise OrchestratorMemoryError(f"handoff {handoff_id!r} is not a JSON object")
        try:
            return OrchestratorHandoff(
                handoff_id=payload["handoff_id"],
                task_id=payload["task_id"],
                run_id=payload["run_id"],
                wave_id=payload["wave_id"],
                decision_point_id=payload["decision_point_id"],
                from_session_id=payload["from_session_id"],
                total_items=payload["total_items"],
                open_items=payload.get("open_items"),
                open_item_ids=tuple(payload.get("open_item_ids", ())),
                remaining_ratio=payload.get("remaining_ratio"),
                used_tokens=payload.get("used_tokens"),
                total_tokens=payload.get("total_tokens"),
                follow_up_task_ids=tuple(payload.get("follow_up_task_ids", ())),
                reason=payload["reason"],
            )
        except (KeyError, TypeError, OrchestratorMemoryError) as exc:
            raise OrchestratorMemoryError(f"handoff {handoff_id!r} is malformed: {exc}") from exc

    def list_ids(self) -> tuple[str, ...]:
        """Return every persisted handoff id in deterministic order."""
        if not self.root.is_dir():
            return ()
        return tuple(sorted(path.stem for path in self.root.glob("*.json")))

    def load_all(self) -> tuple[OrchestratorHandoff, ...]:
        """Return every persisted handoff record in deterministic order."""
        return tuple(self.load(handoff_id) for handoff_id in self.list_ids())

    def latest_for_task(self, task_id: str) -> OrchestratorHandoff | None:
        """Return the most recently named handoff for ``task_id``, or ``None``."""
        task = _validate_identifier(task_id, "task_id")
        matches = [handoff for handoff in self.load_all() if handoff.task_id == task]
        return matches[-1] if matches else None


@dataclass(frozen=True)
class TurnRecord:
    """One persisted state of an Orchestrator decision point."""

    decision_key: str
    decision_point_id: str
    run_id: str
    wave_id: str
    state: TurnState
    attempt: int
    pending_actions: tuple[str, ...]
    committed_actions: tuple[str, ...]
    opened_at: str
    closed_at: str | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "decision_key",
            _validate_line(self.decision_key, "turn.decision_key", MAX_TEXT_LENGTH),
        )
        object.__setattr__(
            self,
            "decision_point_id",
            _validate_identifier(self.decision_point_id, "turn.decision_point_id"),
        )
        object.__setattr__(self, "run_id", _validate_identifier(self.run_id, "turn.run_id"))
        object.__setattr__(self, "wave_id", _validate_identifier(self.wave_id, "turn.wave_id"))
        object.__setattr__(
            self, "opened_at", _validate_line(self.opened_at, "turn.opened_at", MAX_TEXT_LENGTH)
        )
        object.__setattr__(
            self,
            "closed_at",
            _optional_line(self.closed_at, "turn.closed_at", MAX_TEXT_LENGTH),
        )
        object.__setattr__(self, "note", _optional_line(self.note, "turn.note", MAX_TEXT_LENGTH))
        object.__setattr__(
            self, "state", _coerce_enum(TurnState, self.state, "turn.state")
        )
        attempt = _require_int(self.attempt, "turn.attempt")
        if attempt < 0:
            raise OrchestratorMemoryError("turn.attempt must not be negative")
        object.__setattr__(self, "attempt", attempt)
        object.__setattr__(
            self,
            "pending_actions",
            tuple(
                _validate_line(action, f"turn.pending_actions[{index}]", MAX_TEXT_LENGTH)
                for index, action in enumerate(
                    _require_sequence(
                        self.pending_actions, "turn.pending_actions", MAX_DIGEST_ITEMS
                    )
                )
            ),
        )
        object.__setattr__(
            self,
            "committed_actions",
            tuple(
                _validate_line(action, f"turn.committed_actions[{index}]", MAX_TEXT_LENGTH)
                for index, action in enumerate(
                    _require_sequence(
                        self.committed_actions, "turn.committed_actions", MAX_DIGEST_ITEMS
                    )
                )
            ),
        )

    def as_dict(self) -> dict[str, Any]:
        """Return an ordered, JSON-ready mapping with a fixed key order."""
        return {
            "decision_key": self.decision_key,
            "decision_point_id": self.decision_point_id,
            "run_id": self.run_id,
            "wave_id": self.wave_id,
            "state": self.state.value,
            "attempt": self.attempt,
            "pending_actions": list(self.pending_actions),
            "committed_actions": list(self.committed_actions),
            "opened_at": self.opened_at,
            "closed_at": self.closed_at,
            "note": self.note,
        }


@dataclass(frozen=True)
class TurnBegin:
    """The result of opening (or finding) a decision point's turn."""

    record: TurnRecord
    already_handled: bool


@dataclass(frozen=True)
class TurnRecovery:
    """The recovery plan for one decision point interrupted mid-turn."""

    decision_key: str
    decision_point_id: str
    run_id: str
    wave_id: str
    attempt: int
    rolled_back_actions: tuple[str, ...]
    reissue_decision_point: bool
    session_action: str
    brief_mode: str
    owner_question_required: bool
    reason: str
    version: str = ORCHESTRATOR_MEMORY_VERSION

    def as_dict(self) -> dict[str, Any]:
        """Return an ordered, JSON-ready projection with a fixed key order."""
        return {
            "version": self.version,
            "decision_key": self.decision_key,
            "decision_point_id": self.decision_point_id,
            "run_id": self.run_id,
            "wave_id": self.wave_id,
            "attempt": self.attempt,
            "rolled_back_actions": list(self.rolled_back_actions),
            "reissue_decision_point": self.reissue_decision_point,
            "session_action": self.session_action,
            "brief_mode": self.brief_mode,
            "owner_question_required": self.owner_question_required,
            "reason": self.reason,
        }


class TurnJournal:
    """An append-only, transactional journal of Orchestrator decision points.

    A turn is *acknowledged* (``HANDLED``) only after its actions are fully
    handled.  A turn that dies mid-flight leaves an ``OPEN`` record; recovery
    rolls its uncommitted intents back and re-issues the exact same decision
    point, so a restart never repeats a committed action.
    """

    def __init__(self, path: Path, *, now: Clock = _default_clock) -> None:
        self.path = Path(path)
        self._now = now

    def history(self) -> tuple[TurnRecord, ...]:
        """Return every persisted turn record, in append order."""
        if not self.path.is_file():
            return ()
        try:
            raw_lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise TurnJournalError(f"turn journal at {self.path} is unreadable: {exc}") from exc
        records: list[TurnRecord] = []
        for line in raw_lines:
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise TurnJournalError(f"turn journal at {self.path} is corrupt: {exc}") from exc
            if not isinstance(payload, dict):
                raise TurnJournalError(f"turn journal at {self.path} has a non-object record")
            try:
                records.append(
                    TurnRecord(
                        decision_key=payload["decision_key"],
                        decision_point_id=payload["decision_point_id"],
                        run_id=payload["run_id"],
                        wave_id=payload["wave_id"],
                        state=payload["state"],
                        attempt=payload["attempt"],
                        pending_actions=tuple(payload.get("pending_actions", ())),
                        committed_actions=tuple(payload.get("committed_actions", ())),
                        opened_at=payload["opened_at"],
                        closed_at=payload.get("closed_at"),
                        note=payload.get("note"),
                    )
                )
            except (KeyError, TypeError, OrchestratorMemoryError) as exc:
                raise TurnJournalError(f"turn journal at {self.path} is malformed: {exc}") from exc
        return tuple(records)

    def latest(self, decision_key: str) -> TurnRecord | None:
        """Return the most recent record for ``decision_key``, or ``None``."""
        found: TurnRecord | None = None
        for record in self.history():
            if record.decision_key == decision_key:
                found = record
        return found

    def _append(self, record: TurnRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = json.dumps(record.as_dict(), ensure_ascii=False, separators=(",", ":"))
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(payload + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def begin(
        self,
        decision_point_id: str,
        run_id: str,
        wave_id: str,
        *,
        pending_actions: Sequence[str] = (),
    ) -> TurnBegin:
        """Open the turn for a decision point, or return its handled record.

        The decision key is the OT-1 idempotency key, so a replayed turn is the
        *same* turn.  A handled decision point is never reopened: the caller
        learns ``already_handled`` and repeats nothing.
        """
        point = _validate_identifier(decision_point_id, "decision_point_id")
        run = _validate_identifier(run_id, "run_id")
        wave = _validate_identifier(wave_id, "wave_id")
        pending = tuple(
            _validate_line(action, f"pending_actions[{index}]", MAX_TEXT_LENGTH)
            for index, action in enumerate(
                _require_sequence(pending_actions, "pending_actions", MAX_DIGEST_ITEMS)
            )
        )
        key = idempotency_key(point, run, wave)
        prior = [record for record in self.history() if record.decision_key == key]
        if prior and prior[-1].state is TurnState.HANDLED:
            return TurnBegin(record=prior[-1], already_handled=True)
        attempt = sum(1 for record in prior if record.state is TurnState.OPEN)
        record = TurnRecord(
            decision_key=key,
            decision_point_id=point,
            run_id=run,
            wave_id=wave,
            state=TurnState.OPEN,
            attempt=attempt,
            pending_actions=pending,
            committed_actions=(),
            opened_at=self._now(),
        )
        self._append(record)
        return TurnBegin(record=record, already_handled=False)

    def commit(self, decision_key: str, actions: Sequence[str]) -> TurnRecord:
        """Acknowledge a decision point after its actions are fully handled."""
        key = _validate_line(decision_key, "decision_key", MAX_TEXT_LENGTH)
        committed = tuple(
            _validate_line(action, f"actions[{index}]", MAX_TEXT_LENGTH)
            for index, action in enumerate(
                _require_sequence(actions, "actions", MAX_DIGEST_ITEMS)
            )
        )
        current = self.latest(key)
        if current is None:
            raise TurnJournalError(f"cannot commit unknown decision key {key!r}")
        if current.state is TurnState.HANDLED:
            if current.committed_actions == committed:
                return current
            raise TurnConflictError(
                f"decision key {key!r} is already handled with different committed actions"
            )
        if current.state is TurnState.ROLLED_BACK:
            raise TurnJournalError(f"decision key {key!r} was rolled back and cannot be committed")
        record = TurnRecord(
            decision_key=current.decision_key,
            decision_point_id=current.decision_point_id,
            run_id=current.run_id,
            wave_id=current.wave_id,
            state=TurnState.HANDLED,
            attempt=current.attempt,
            pending_actions=current.pending_actions,
            committed_actions=committed,
            opened_at=current.opened_at,
            closed_at=self._now(),
            note="handled",
        )
        self._append(record)
        return record

    def rollback(self, decision_key: str, *, reason: str) -> TurnRecord:
        """Roll one *open* turn back, discarding its uncommitted actions."""
        key = _validate_line(decision_key, "decision_key", MAX_TEXT_LENGTH)
        note = _validate_line(reason, "reason", MAX_TEXT_LENGTH)
        current = self.latest(key)
        if current is None:
            raise TurnJournalError(f"cannot roll back unknown decision key {key!r}")
        if current.state is TurnState.HANDLED:
            raise TurnJournalError(
                f"decision key {key!r} already committed its actions and cannot be rolled back"
            )
        if current.state is TurnState.ROLLED_BACK:
            return current
        record = TurnRecord(
            decision_key=current.decision_key,
            decision_point_id=current.decision_point_id,
            run_id=current.run_id,
            wave_id=current.wave_id,
            state=TurnState.ROLLED_BACK,
            attempt=current.attempt,
            pending_actions=current.pending_actions,
            committed_actions=(),
            opened_at=current.opened_at,
            closed_at=self._now(),
            note=note,
        )
        self._append(record)
        return record

    def recover(self) -> tuple[TurnRecovery, ...]:
        """Roll back every interrupted turn and return its re-issue plan.

        Re-issuing uses the OT-1 idempotency key for the *exact same* decision
        point, a clean session, and a smaller (summary) brief, so nothing
        already committed is repeated.  At
        :data:`MAX_FAILED_ATTEMPTS_BEFORE_OWNER_QUESTION` the decision escalates
        to an owner question instead of a further automatic attempt.
        """
        pending_keys: list[str] = []
        for record in self.history():
            if record.state is TurnState.OPEN and record.decision_key not in pending_keys:
                pending_keys.append(record.decision_key)
        plans: list[TurnRecovery] = []
        for key in pending_keys:
            interrupted = self.latest(key)
            if interrupted is None or interrupted.state is not TurnState.OPEN:
                continue
            rolled = self.rollback(key, reason="restart_recovery")
            plans.append(
                TurnRecovery(
                    decision_key=rolled.decision_key,
                    decision_point_id=rolled.decision_point_id,
                    run_id=rolled.run_id,
                    wave_id=rolled.wave_id,
                    attempt=rolled.attempt,
                    rolled_back_actions=rolled.pending_actions,
                    reissue_decision_point=True,
                    session_action="new_session",
                    brief_mode="summary",
                    owner_question_required=(
                        rolled.attempt >= MAX_FAILED_ATTEMPTS_BEFORE_OWNER_QUESTION
                    ),
                    reason=(
                        "turn was killed mid-flight; repeat nothing and re-issue "
                        "the decision point"
                    ),
                )
            )
        return tuple(plans)


# --------------------------------------------------------------------------- #
# Memory facade and sessions
# --------------------------------------------------------------------------- #


class OrchestratorMemory:
    """The durable Orchestrator memory root: notebook, archive, lock, handoffs, journal."""

    def __init__(
        self,
        root: Path,
        *,
        probe: LivenessProbe = probe_liveness,
        now: Clock = _default_clock,
    ) -> None:
        self.root = Path(root)
        self.archive = NotebookArchive(self.root / ARCHIVE_FILENAME)
        self.notebook = NotebookStore(self.root / NOTEBOOK_FILENAME, archive=self.archive, now=now)
        self.lock = OrchestratorSessionLock(self.root / LOCK_FILENAME, probe=probe, now=now)
        self.handoffs = HandoffStore(self.root / HANDOFFS_DIRNAME)
        self.journal = TurnJournal(self.root / JOURNAL_FILENAME, now=now)
        self._now = now

    def latest_handoff(self, task_id: str) -> OrchestratorHandoff | None:
        """Return the most recent handoff recorded for ``task_id``, or ``None``."""
        return self.handoffs.latest_for_task(task_id)


class OrchestratorSession:
    """The single writer session holding the Orchestrator writer lock."""

    mode: Final[SessionMode] = SessionMode.WRITER

    def __init__(self, memory: OrchestratorMemory, session_id: str) -> None:
        self.memory = memory
        self.session_id = _validate_identifier(session_id, "session_id")

    def build_digest(self, facts: DigestFacts) -> StartupDigest:
        """Build the deterministic startup digest for this session."""
        return build_startup_digest(facts)

    def notebook_entries(self) -> tuple[NotebookEntry, ...]:
        """Return the notebook entries in deterministic read order."""
        return self.memory.notebook.entries()

    def put_notebook_entry(self, entry: NotebookEntry) -> NotebookWrite:
        """Admit one notebook entry, archiving whatever it evicts."""
        return self.memory.notebook.put(entry)

    def clear_perishable(self) -> tuple[NotebookEntry, ...]:
        """Clear perishable entries at a wave/turn boundary."""
        return self.memory.notebook.clear_perishable()

    def advance_age(self) -> tuple[NotebookEntry, ...]:
        """Age the notebook by one wave/turn boundary."""
        return self.memory.notebook.advance_age()

    def decide_rotation(
        self,
        *,
        trigger: ContextTrigger,
        progress: ChecklistProgress,
        mode: AuthorityMode,
        task_id: str,
        run_id: str,
        wave_id: str,
        decision_point_id: str,
        open_item_ids: Sequence[str] = (),
        reason: str = "context_over_half_window",
        max_items_per_follow_up: int = 8,
    ) -> RotationDecision:
        """Decide rotation for this session and record its durable handoff."""
        decision = decide_rotation(
            trigger=trigger,
            progress=progress,
            mode=mode,
            task_id=task_id,
            run_id=run_id,
            wave_id=wave_id,
            decision_point_id=decision_point_id,
            session_id=self.session_id,
            open_item_ids=open_item_ids,
            reason=reason,
            max_items_per_follow_up=max_items_per_follow_up,
        )
        if decision.handoff is not None:
            self.memory.handoffs.record(decision.handoff)
        return decision

    def record_handoff(self, handoff: OrchestratorHandoff) -> Path:
        """Persist a handoff record exactly once."""
        return self.memory.handoffs.record(handoff)

    def begin_turn(
        self,
        decision_point_id: str,
        run_id: str,
        wave_id: str,
        *,
        pending_actions: Sequence[str] = (),
    ) -> TurnBegin:
        """Open a decision point's turn (idempotent by OT-1 decision key)."""
        return self.memory.journal.begin(
            decision_point_id, run_id, wave_id, pending_actions=pending_actions
        )

    def commit_turn(self, decision_key: str, actions: Sequence[str]) -> TurnRecord:
        """Acknowledge a decision point after full handling."""
        return self.memory.journal.commit(decision_key, actions)

    def rollback_turn(self, decision_key: str, *, reason: str) -> TurnRecord:
        """Roll back an uncommitted turn."""
        return self.memory.journal.rollback(decision_key, reason=reason)

    def recover_turns(self) -> tuple[TurnRecovery, ...]:
        """Recover every decision point interrupted mid-turn."""
        return self.memory.journal.recover()

    def release(self) -> bool:
        """Release the writer lock, if this session still holds it."""
        return self.memory.lock.release(session_id=self.session_id)


class ReadonlySession:
    """A second Orchestrator session, strictly forced into read-only mode."""

    mode: Final[SessionMode] = SessionMode.READONLY

    def __init__(self, memory: OrchestratorMemory, session_id: str, blocked_by: LockHolder) -> None:
        self.memory = memory
        self.session_id = _validate_identifier(session_id, "session_id")
        self.blocked_by = blocked_by

    # -- reads are allowed -------------------------------------------------- #

    def build_digest(self, facts: DigestFacts) -> StartupDigest:
        """Build the deterministic startup digest for this read-only session."""
        return build_startup_digest(facts)

    def notebook_entries(self) -> tuple[NotebookEntry, ...]:
        """Return the notebook entries in deterministic read order."""
        return self.memory.notebook.entries()

    def latest_handoff(self, task_id: str) -> OrchestratorHandoff | None:
        """Return the most recent handoff recorded for ``task_id``, or ``None``."""
        return self.memory.handoffs.latest_for_task(task_id)

    # -- writes are refused, strictly --------------------------------------- #

    def put_notebook_entry(self, entry: NotebookEntry) -> NotebookWrite:
        """Refuse: a read-only session never mutates the notebook."""
        raise ReadonlySessionError(
            f"session {self.session_id!r} is read-only: writer is {self.blocked_by.session_id!r}"
        )

    def clear_perishable(self) -> tuple[NotebookEntry, ...]:
        """Refuse: a read-only session never mutates the notebook."""
        raise ReadonlySessionError(
            f"session {self.session_id!r} is read-only: writer is {self.blocked_by.session_id!r}"
        )

    def advance_age(self) -> tuple[NotebookEntry, ...]:
        """Refuse: a read-only session never mutates the notebook."""
        raise ReadonlySessionError(
            f"session {self.session_id!r} is read-only: writer is {self.blocked_by.session_id!r}"
        )

    def record_handoff(self, handoff: OrchestratorHandoff) -> Path:
        """Refuse: a read-only session never writes durable state."""
        raise ReadonlySessionError(
            f"session {self.session_id!r} is read-only: writer is {self.blocked_by.session_id!r}"
        )

    def begin_turn(
        self,
        decision_point_id: str,
        run_id: str,
        wave_id: str,
        *,
        pending_actions: Sequence[str] = (),
    ) -> TurnBegin:
        """Refuse: a read-only session never opens a turn."""
        raise ReadonlySessionError(
            f"session {self.session_id!r} is read-only: writer is {self.blocked_by.session_id!r}"
        )

    def release(self) -> bool:
        """Refuse: a read-only session never releases another session's lock."""
        return False


def open_session(
    memory: OrchestratorMemory,
    *,
    session_id: str,
    identity: ProcessIdentity | None = None,
) -> OrchestratorSession | ReadonlySession:
    """Open an Orchestrator session, forcing a second one into read-only mode.

    * lock acquired (or reclaimed from a provably dead holder) -> writer session;
    * a live holder -> :class:`ReadonlySession` bound to that holder;
    * an undecidable lock -> :class:`SessionLockUnsafeError`, never a session.
    """
    if not isinstance(memory, OrchestratorMemory):
        raise OrchestratorMemoryError("open_session requires an OrchestratorMemory root")
    try:
        memory.lock.acquire(session_id=session_id, identity=identity)
    except SessionLockedError as exc:
        return ReadonlySession(memory, session_id, exc.holder)
    return OrchestratorSession(memory, session_id)


__all__ = [
    "ABSENT_MARKER",
    "AGING_BASE_PRIORITY",
    "AGING_DECAY_PER_AGE",
    "ARCHIVE_FILENAME",
    "CONTEXT_ROTATION_THRESHOLD_PERCENT",
    "CONTINUE_REMAINING_PERCENT",
    "DIGEST_MAX_CHARS",
    "DIGEST_MAX_ITEM_CHARS",
    "DIGEST_MAX_ITEMS_PER_SECTION",
    "DIGEST_MAX_TOKENS",
    "DIGEST_REDUCTION_ORDER",
    "DIGEST_SECTION_ORDER",
    "HANDOFFS_DIRNAME",
    "JOURNAL_FILENAME",
    "LOCK_FILENAME",
    "MAX_REMAINING_RATIO",
    "NOTEBOOK_FILENAME",
    "NOTEBOOK_MAX_CHARS",
    "NOTEBOOK_MAX_ENTRIES",
    "NOTEBOOK_MAX_ENTRY_CHARS",
    "ORCHESTRATOR_MEMORY_VERSION",
    "PERISHABLE_PRIORITY",
    "PINNED_PRIORITY",
    "TRUNCATED_TEMPLATE",
    "AcceptedRun",
    "BacklogItem",
    "ChecklistProgress",
    "ContextTrigger",
    "DigestBudgetError",
    "DigestContextStatus",
    "DigestFacts",
    "DigestSection",
    "DigestSessionState",
    "FailedRun",
    "HandoffConflictError",
    "HandoffStore",
    "LockAcquisition",
    "LockHolder",
    "NotebookArchive",
    "NotebookBudgetError",
    "NotebookEntry",
    "NotebookStore",
    "NotebookStoreError",
    "NotebookTier",
    "NotebookWrite",
    "OpenQuestion",
    "OrchestratorHandoff",
    "OrchestratorMemory",
    "OrchestratorMemoryError",
    "OrchestratorSession",
    "OrchestratorSessionLock",
    "ReadonlySession",
    "ReadonlySessionError",
    "RoadmapItem",
    "RotationAction",
    "RotationDecision",
    "SessionLockUnsafeError",
    "SessionLockedError",
    "SessionMode",
    "StartupDigest",
    "TurnBegin",
    "TurnConflictError",
    "TurnJournal",
    "TurnJournalError",
    "TurnRecord",
    "TurnRecovery",
    "TurnState",
    "build_startup_digest",
    "decide_rotation",
    "effective_priority",
    "estimate_tokens",
    "handoff_id_for",
    "is_full_autonomy",
    "open_session",
]
