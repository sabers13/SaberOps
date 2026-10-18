"""Hermetic contract tests for bounded Orchestrator memory and restart (OM-1).

Every test states what it protects with a ``Covers:`` docstring line. The whole
file is hermetic: no provider, CLI, login, network, database, model launch or
sleep. Every filesystem write is confined to ``tmp_path`` and every process
liveness answer is injected, so no real PID or host state is trusted.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from saberops.authority import AuthorityMode
from saberops.orchestrator_memory import (
    ABSENT_MARKER,
    DIGEST_MAX_CHARS,
    DIGEST_MAX_TOKENS,
    DIGEST_SECTION_ORDER,
    MAX_REMAINING_RATIO,
    NOTEBOOK_MAX_ENTRY_CHARS,
    AcceptedRun,
    BacklogItem,
    ChecklistProgress,
    ContextTrigger,
    DigestBudgetError,
    DigestContextStatus,
    DigestFacts,
    DigestSection,
    DigestSessionState,
    FailedRun,
    HandoffConflictError,
    NotebookArchive,
    NotebookBudgetError,
    NotebookEntry,
    NotebookStore,
    NotebookStoreError,
    NotebookTier,
    OpenQuestion,
    OrchestratorHandoff,
    OrchestratorMemory,
    OrchestratorMemoryError,
    OrchestratorSession,
    ReadonlySession,
    ReadonlySessionError,
    RoadmapItem,
    RotationAction,
    SessionLockedError,
    SessionLockUnsafeError,
    SessionMode,
    TurnConflictError,
    TurnJournal,
    TurnJournalError,
    TurnState,
    build_startup_digest,
    decide_rotation,
    effective_priority,
    estimate_tokens,
    handoff_id_for,
    open_session,
)
from saberops.orchestrator_turn_policy import ContextUsage, idempotency_key
from saberops.ownership import Liveness, ProcessIdentity

FIXED_NOW = "2026-10-18T00:00:00+00:00"
SHA_A = "a" * 40
SHA_B = "b" * 40
TASK = "task-om-1"
RUN = "run-7"
WAVE = "wave-2"
POINT = "dp-0001"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _identity(pid: int = 4242) -> ProcessIdentity:
    return ProcessIdentity(
        host="test-host",
        boot_id="boot-1",
        pid=pid,
        start_ticks=100,
        session_id=pid,
        process_group_id=pid,
    )


def _probe(liveness: Liveness) -> Callable[[ProcessIdentity | None], Liveness]:
    def probe(identity: ProcessIdentity | None) -> Liveness:
        del identity
        return liveness

    return probe


def _memory(tmp_path: Path, *, liveness: Liveness = Liveness.LIVE) -> OrchestratorMemory:
    return OrchestratorMemory(
        tmp_path / "memory", probe=_probe(liveness), now=lambda: FIXED_NOW
    )


def _facts(**overrides: Any) -> DigestFacts:
    base: dict[str, Any] = {
        "project_id": "proj-alpha",
        "plan_reference": "plan-7",
        "base_sha": SHA_A,
        "last_session_at": "2026-10-17T22:00:00+00:00",
        "status_since_last_session": ("wave 4 accepted",),
        "recent_accepted_runs": (
            AcceptedRun(run_id="run-1", accepted_sha=SHA_B, accepted_at="2026-10-17T21:00:00Z"),
        ),
        "open_backlog": (BacklogItem(item_id="OM-2", title="Next item", state="open"),),
        "failed_runs": (
            FailedRun(
                run_id="run-2",
                reason="G1 failed on the lane",
                retry_command="orch run resume run-2",
            ),
        ),
        "next_roadmap_item": RoadmapItem(item_id="OM-1", title="Bound memory", state="open"),
        "open_questions": (OpenQuestion(question_id="q-1", question="Ship the wave?"),),
        "notebook": (
            NotebookEntry(key="rule-1", tier=NotebookTier.PINNED, text="never move main"),
        ),
        "session": DigestSessionState(
            session_id="orch-a", mode=SessionMode.WRITER, lock_holder_session="orch-a"
        ),
        "context": DigestContextStatus(
            usage=ContextUsage(used_tokens=10, total_tokens=100), rotation_latched=False
        ),
    }
    base.update(overrides)
    return DigestFacts(**base)


def _notebook(
    tmp_path: Path, *, max_chars: int = 60, max_entries: int = 8
) -> NotebookStore:
    return NotebookStore(
        tmp_path / "notebook.json",
        archive=NotebookArchive(tmp_path / "notebook-archive.jsonl"),
        now=lambda: FIXED_NOW,
        max_chars=max_chars,
        max_entries=max_entries,
    )


def _entry(key: str, tier: NotebookTier, *, size: int = 10, age: int = 0) -> NotebookEntry:
    return NotebookEntry(key=key, tier=tier, text=key[0].upper() * size, age=age)


def _usage(used: int, total: int = 100) -> ContextUsage:
    return ContextUsage(used_tokens=used, total_tokens=total)


def _latched(used: int = 60) -> ContextTrigger:
    return ContextTrigger.initial().observe(_usage(used))


def _rotation(**overrides: Any) -> Any:
    base: dict[str, Any] = {
        "trigger": _latched(),
        "progress": ChecklistProgress(total_items=10, open_items=8),
        "mode": AuthorityMode.SUPERVISED,
        "task_id": TASK,
        "run_id": RUN,
        "wave_id": WAVE,
        "decision_point_id": POINT,
        "session_id": "orch-a",
        "open_item_ids": ("item-1", "item-2"),
    }
    base.update(overrides)
    return decide_rotation(**base)


# --------------------------------------------------------------------------- #
# Startup digest
# --------------------------------------------------------------------------- #


def test_digest_renders_every_section_in_fixed_order() -> None:
    """Covers: OM-1 startup digest fixed section ordering."""
    digest = build_startup_digest(_facts())
    assert digest.sections == DIGEST_SECTION_ORDER
    positions = [digest.body.index(f"## {section.value}") for section in DIGEST_SECTION_ORDER]
    assert positions == sorted(positions)
    assert len(positions) == len(DIGEST_SECTION_ORDER)


def test_digest_is_deterministic_for_identical_facts() -> None:
    """Covers: OM-1 deterministic startup digest."""
    first = build_startup_digest(_facts())
    second = build_startup_digest(_facts())
    assert first.body == second.body
    assert first.canonical_json() == second.canonical_json()
    assert first.char_cost == len(first.body)
    assert first.token_cost == estimate_tokens(first.body)


def test_digest_marks_every_missing_section_as_absent() -> None:
    """Covers: OM-1 explicit [ABSENT] markers for missing state."""
    digest = build_startup_digest(
        _facts(
            project_id=None,
            plan_reference=None,
            base_sha=None,
            last_session_at=None,
            status_since_last_session=(),
            recent_accepted_runs=(),
            open_backlog=(),
            failed_runs=(),
            next_roadmap_item=None,
            open_questions=(),
            notebook=(),
            session=None,
            context=None,
        )
    )
    for section in DIGEST_SECTION_ORDER:
        assert f"## {section.value}" in digest.body
    assert ABSENT_MARKER in digest.body
    absent_count = digest.body.count(ABSENT_MARKER)
    assert absent_count >= 15
    assert "## failed_runs\n[ABSENT]" in digest.body
    assert "last session: [ABSENT]" in digest.body


def test_digest_carries_exact_retry_commands() -> None:
    """Covers: OM-1 failed runs with exact retry commands."""
    command = "orch run resume run-2 --profile dogfood --from G1"
    digest = build_startup_digest(
        _facts(failed_runs=(FailedRun(run_id="run-2", reason="G1 failed", retry_command=command),))
    )
    assert f"  exact retry: {command}" in digest.body


def test_digest_doubles_as_continue_summary() -> None:
    """Covers: OM-1 startup digest doubles as the project-level Continue summary."""
    digest = build_startup_digest(_facts())
    assert "run-1" in digest.body
    assert SHA_B in digest.body
    assert "OM-2" in digest.body
    assert "orch run resume run-2" in digest.body
    assert "item: OM-1" in digest.body


def test_digest_stays_within_its_character_and_token_budget() -> None:
    """Covers: OM-1 bounded startup digest budget."""
    statuses = tuple(f"status-{index}-" + ("x" * 400) for index in range(200))
    digest = build_startup_digest(_facts(status_since_last_session=statuses))
    assert digest.char_cost <= DIGEST_MAX_CHARS
    assert digest.token_cost <= DIGEST_MAX_TOKENS
    assert DigestSection.SINCE_LAST_SESSION.value in digest.truncations


def test_digest_discloses_truncation_without_claiming_absence() -> None:
    """Covers: OM-1 truncation is disclosed and never rendered as [ABSENT]."""
    statuses = tuple(f"status-{index}-" + ("x" * 400) for index in range(200))
    digest = build_startup_digest(_facts(status_since_last_session=statuses))
    section = digest.body.split("## since_last_session\n", 1)[1].split("\n## ", 1)[0]
    assert "[TRUNCATED" in section
    assert ABSENT_MARKER not in section


def test_digest_never_truncates_an_exact_retry_command() -> None:
    """Covers: OM-1 exact retry commands survive budget reduction verbatim."""
    command = "orch run resume run-2 " + ("--flag value " * 30)
    command = command[:500]
    digest = build_startup_digest(
        _facts(
            failed_runs=(FailedRun(run_id="run-2", reason="failed", retry_command=command),),
            status_since_last_session=tuple(
                f"status-{index}-" + ("y" * 400) for index in range(100)
            ),
        )
    )
    assert digest.char_cost <= DIGEST_MAX_CHARS
    assert command in digest.body


def test_digest_refuses_an_irreducible_over_budget_digest() -> None:
    """Covers: OM-1 digest fails closed instead of dropping mandatory content."""
    runs = tuple(
        FailedRun(
            run_id=f"run-{index}",
            reason="failed",
            retry_command=("orch run resume " + ("z" * 400)),
        )
        for index in range(40)
    )
    with pytest.raises(DigestBudgetError):
        build_startup_digest(_facts(failed_runs=runs))


def test_digest_never_reduces_a_pinned_notebook_entry() -> None:
    """Covers: OM-1 pinned notebook entries are permanent in the digest."""
    pinned = tuple(
        NotebookEntry(key=f"rule-{index}", tier=NotebookTier.PINNED, text="p" * 300)
        for index in range(10)
    )
    perishable = tuple(
        NotebookEntry(key=f"note-{index}", tier=NotebookTier.PERISHABLE, text="n" * 300)
        for index in range(10)
    )
    digest = build_startup_digest(
        _facts(
            notebook=pinned + perishable,
            status_since_last_session=tuple("s" * 400 for _ in range(50)),
        )
    )
    for entry in pinned:
        assert entry.key in digest.body


def test_digest_rejects_malformed_facts() -> None:
    """Covers: OM-1 digest input validation fails closed."""
    with pytest.raises(OrchestratorMemoryError):
        _facts(base_sha="abc")
    with pytest.raises(OrchestratorMemoryError):
        _facts(status_since_last_session=("multi\nline",))
    with pytest.raises(OrchestratorMemoryError):
        _facts(project_id="bad id with spaces")
    with pytest.raises(OrchestratorMemoryError):
        build_startup_digest(object())  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Notebook budgeting
# --------------------------------------------------------------------------- #


def test_notebook_priority_orders_pinned_above_aging_above_perishable() -> None:
    """Covers: OM-1 pinned/aging/perishable typed notebook entries."""
    pinned = _entry("pin", NotebookTier.PINNED)
    aging = _entry("age", NotebookTier.AGING)
    perishable = _entry("per", NotebookTier.PERISHABLE)
    assert effective_priority(pinned) > effective_priority(aging)
    assert effective_priority(aging) > effective_priority(perishable)


def test_aging_priority_decays_with_age() -> None:
    """Covers: OM-1 aging notebook entries decay in priority."""
    fresh = _entry("age", NotebookTier.AGING, age=0)
    stale = _entry("age", NotebookTier.AGING, age=10)
    assert effective_priority(stale) < effective_priority(fresh)
    assert effective_priority(stale) < effective_priority(_entry("per", NotebookTier.PERISHABLE))


def test_notebook_reads_pinned_first(tmp_path: Path) -> None:
    """Covers: OM-1 deterministic notebook read order."""
    store = _notebook(tmp_path)
    store.put(_entry("per", NotebookTier.PERISHABLE))
    store.put(_entry("age", NotebookTier.AGING))
    store.put(_entry("pin", NotebookTier.PINNED))
    assert [entry.key for entry in store.entries()] == ["pin", "age", "per"]


def test_notebook_evicts_lowest_priority_and_archives_it(tmp_path: Path) -> None:
    """Covers: OM-1 eviction moves entries to the append-only archive."""
    store = _notebook(tmp_path, max_chars=30)
    store.put(_entry("pin", NotebookTier.PINNED))
    store.put(_entry("age", NotebookTier.AGING))
    store.put(_entry("per", NotebookTier.PERISHABLE))
    result = store.put(_entry("new", NotebookTier.PERISHABLE))
    assert [entry.key for entry in result.evicted] == ["per"]
    assert store.get("per") is None
    assert store.get("pin") is not None
    assert store.total_chars() <= 30
    archive = store.archive.read()
    assert archive[-1]["reason"] == "budget_eviction"
    assert archive[-1]["entry"]["key"] == "per"


def test_notebook_evicts_a_decayed_aging_entry_before_a_fresh_perishable(tmp_path: Path) -> None:
    """Covers: OM-1 aging decay decides eviction order."""
    store = _notebook(tmp_path, max_chars=30)
    store.put(_entry("pin", NotebookTier.PINNED))
    store.put(_entry("age", NotebookTier.AGING, age=10))
    store.put(_entry("per", NotebookTier.PERISHABLE))
    result = store.put(_entry("new", NotebookTier.PERISHABLE))
    assert [entry.key for entry in result.evicted] == ["age"]
    assert store.get("per") is not None


def test_notebook_never_evicts_a_pinned_entry(tmp_path: Path) -> None:
    """Covers: OM-1 pinned entries are permanent across restarts."""
    store = _notebook(tmp_path, max_chars=10)
    store.put(_entry("pin", NotebookTier.PINNED))
    before = store.total_chars()
    with pytest.raises(NotebookBudgetError):
        store.put(_entry("per", NotebookTier.PERISHABLE))
    assert store.get("pin") is not None
    assert store.total_chars() == before


def test_notebook_refusal_does_not_partially_mutate(tmp_path: Path) -> None:
    """Covers: OM-1 a refused admission leaves no partially applied eviction."""
    store = _notebook(tmp_path, max_chars=20, max_entries=2)
    store.put(_entry("pin", NotebookTier.PINNED, size=20))
    with pytest.raises(NotebookBudgetError):
        store.put(_entry("per", NotebookTier.PERISHABLE))
    assert store.entry_count() == 1
    assert store.total_chars() == 20


def test_notebook_caps_entry_count_as_well_as_characters(tmp_path: Path) -> None:
    """Covers: OM-1 strictly bounded notebook total size."""
    store = _notebook(tmp_path, max_chars=1_000, max_entries=2)
    store.put(_entry("pin", NotebookTier.PINNED, size=1))
    store.put(_entry("per", NotebookTier.PERISHABLE, size=1))
    result = store.put(_entry("age", NotebookTier.AGING, size=1))
    assert store.entry_count() == 2
    assert [entry.key for entry in result.evicted] == ["per"]
    assert effective_priority(_entry("pin", NotebookTier.PINNED)) > 0
    for entry in store.entries():
        assert len(entry.text) <= NOTEBOOK_MAX_ENTRY_CHARS


def test_notebook_clears_perishable_at_a_boundary(tmp_path: Path) -> None:
    """Covers: OM-1 perishable entries are cleared at the wave/turn boundary."""
    store = _notebook(tmp_path)
    store.put(_entry("pin", NotebookTier.PINNED))
    store.put(_entry("per", NotebookTier.PERISHABLE))
    cleared = store.clear_perishable()
    assert [entry.key for entry in cleared] == ["per"]
    assert store.get("pin") is not None
    assert store.get("per") is None
    assert store.archive.read()[-1]["reason"] == "boundary_cleared"


def test_notebook_age_advances_only_aging_entries(tmp_path: Path) -> None:
    """Covers: OM-1 aging entries advance one boundary at a time."""
    store = _notebook(tmp_path)
    store.put(_entry("pin", NotebookTier.PINNED))
    store.put(_entry("age", NotebookTier.AGING))
    aged = store.advance_age()
    assert [entry.key for entry in aged] == ["age"]
    assert aged[0].age == 1
    reloaded = _notebook(tmp_path)
    assert reloaded.get("age") is not None
    assert reloaded.get("age").age == 1  # type: ignore[union-attr]


def test_notebook_persists_across_restarts(tmp_path: Path) -> None:
    """Covers: OM-1 pinned notebook state survives a restart."""
    store = _notebook(tmp_path)
    store.put(_entry("pin", NotebookTier.PINNED, size=5))
    reloaded = _notebook(tmp_path)
    assert [entry.key for entry in reloaded.entries()] == ["pin"]


def test_notebook_archive_is_append_only(tmp_path: Path) -> None:
    """Covers: OM-1 the archive is append-only and never rewritten."""
    store = _notebook(tmp_path, max_chars=20)
    store.put(_entry("pin", NotebookTier.PINNED))
    store.put(_entry("per", NotebookTier.PERISHABLE))
    store.put(_entry("new", NotebookTier.PERISHABLE))
    archive_path = store.archive.path
    recorded = archive_path.read_bytes()
    store.put(_entry("last", NotebookTier.PERISHABLE))
    extended = archive_path.read_bytes()
    assert extended.startswith(recorded)
    assert len(extended) > len(recorded)


def test_notebook_corruption_fails_closed(tmp_path: Path) -> None:
    """Covers: OM-1 a corrupt notebook is never silently emptied."""
    path = tmp_path / "notebook.json"
    path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(NotebookStoreError):
        _notebook(tmp_path)


def test_notebook_rejects_malformed_entries() -> None:
    """Covers: OM-1 notebook entry validation fails closed."""
    with pytest.raises(OrchestratorMemoryError):
        NotebookEntry(key="ok", tier=NotebookTier.PINNED, text="bad\ntext")
    with pytest.raises(OrchestratorMemoryError):
        NotebookEntry(key="ok", tier=NotebookTier.PINNED, text="x" * (NOTEBOOK_MAX_ENTRY_CHARS + 1))
    with pytest.raises(OrchestratorMemoryError):
        NotebookEntry(key="ok", tier="not-a-tier", text="text")  # type: ignore[arg-type]


def test_estimate_tokens_is_a_deterministic_ceiling() -> None:
    """Covers: OM-1 deterministic token budget accounting."""
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2


# --------------------------------------------------------------------------- #
# Session lock
# --------------------------------------------------------------------------- #


def test_lock_file_is_private_and_records_the_holder(tmp_path: Path) -> None:
    """Covers: OM-1 single-writer lock records a verified process identity."""
    memory = _memory(tmp_path)
    acquisition = memory.lock.acquire(session_id="orch-a", identity=_identity())
    assert acquisition.reason == "acquired"
    assert not acquisition.reclaimed
    assert memory.lock.path.is_file()
    assert memory.lock.path.stat().st_mode & 0o777 == 0o600
    holder = memory.lock.read_holder()
    assert holder is not None
    assert holder.session_id == "orch-a"
    assert holder.identity is not None
    assert holder.identity.start_ticks == 100
    assert holder.pid == 4242


def test_second_live_session_is_forced_read_only(tmp_path: Path) -> None:
    """Covers: OM-1 a second session is strictly forced into read-only mode."""
    memory = _memory(tmp_path)
    first = open_session(memory, session_id="orch-a", identity=_identity(4242))
    second = open_session(memory, session_id="orch-b", identity=_identity(4243))
    assert isinstance(first, OrchestratorSession)
    assert isinstance(second, ReadonlySession)
    assert second.mode is SessionMode.READONLY
    assert second.blocked_by.session_id == "orch-a"
    assert second.release() is False


def test_readonly_session_refuses_every_mutation(tmp_path: Path) -> None:
    """Covers: OM-1 the read-only session cannot mutate durable state."""
    memory = _memory(tmp_path)
    open_session(memory, session_id="orch-a", identity=_identity(4242))
    second = open_session(memory, session_id="orch-b", identity=_identity(4243))
    assert isinstance(second, ReadonlySession)
    entry = _entry("pin", NotebookTier.PINNED)
    with pytest.raises(ReadonlySessionError):
        second.put_notebook_entry(entry)
    with pytest.raises(ReadonlySessionError):
        second.clear_perishable()
    with pytest.raises(ReadonlySessionError):
        second.advance_age()
    with pytest.raises(ReadonlySessionError):
        second.begin_turn(POINT, RUN, WAVE)
    with pytest.raises(ReadonlySessionError):
        second.record_handoff(
            OrchestratorHandoff(
                handoff_id="handoff-1",
                task_id=TASK,
                run_id=RUN,
                wave_id=WAVE,
                decision_point_id=POINT,
                from_session_id="orch-a",
                total_items=2,
                open_items=2,
                open_item_ids=("a", "b"),
                remaining_ratio=1.0,
                used_tokens=60,
                total_tokens=100,
                follow_up_task_ids=(),
                reason="context",
            )
        )


def test_readonly_session_can_still_read_the_digest(tmp_path: Path) -> None:
    """Covers: OM-1 the read-only session still builds the startup digest."""
    memory = _memory(tmp_path)
    open_session(memory, session_id="orch-a", identity=_identity(4242))
    second = open_session(memory, session_id="orch-b", identity=_identity(4243))
    assert isinstance(second, ReadonlySession)
    digest = second.build_digest(_facts())
    assert digest.char_cost == len(digest.body)
    assert second.notebook_entries() == ()


def test_stale_lock_is_reclaimed_after_pid_verification(tmp_path: Path) -> None:
    """Covers: OM-1 stale lock detection reclaims a provably dead holder."""
    memory = _memory(tmp_path)
    memory.lock.acquire(session_id="orch-a", identity=_identity(4242))
    stale = OrchestratorMemory(
        tmp_path / "memory", probe=_probe(Liveness.DEAD), now=lambda: FIXED_NOW
    )
    acquisition = stale.lock.acquire(session_id="orch-b", identity=_identity(4243))
    assert acquisition.reclaimed
    assert acquisition.reclaimed_from == "orch-a"
    holder = stale.lock.read_holder()
    assert holder is not None
    assert holder.session_id == "orch-b"


def test_unknown_liveness_never_reclaims_the_lock(tmp_path: Path) -> None:
    """Covers: OM-1 UNKNOWN process liveness fails closed and is never reclaimed."""
    memory = _memory(tmp_path)
    memory.lock.acquire(session_id="orch-a", identity=_identity(4242))
    unknown = OrchestratorMemory(
        tmp_path / "memory", probe=_probe(Liveness.UNKNOWN), now=lambda: FIXED_NOW
    )
    with pytest.raises(SessionLockUnsafeError):
        unknown.lock.acquire(session_id="orch-b", identity=_identity(4243))
    with pytest.raises(SessionLockUnsafeError):
        open_session(unknown, session_id="orch-b", identity=_identity(4243))
    holder = unknown.lock.read_holder()
    assert holder is not None
    assert holder.session_id == "orch-a"


def test_live_holder_raises_session_locked(tmp_path: Path) -> None:
    """Covers: OM-1 a live holder is never displaced."""
    memory = _memory(tmp_path)
    memory.lock.acquire(session_id="orch-a", identity=_identity(4242))
    with pytest.raises(SessionLockedError):
        memory.lock.acquire(session_id="orch-b", identity=_identity(4243))


def test_same_session_reacquire_is_idempotent(tmp_path: Path) -> None:
    """Covers: OM-1 re-acquiring as the current holder is a no-op."""
    memory = _memory(tmp_path)
    first = memory.lock.acquire(session_id="orch-a", identity=_identity(4242))
    second = memory.lock.acquire(session_id="orch-a", identity=_identity(4242))
    assert second.reason == "already_held_by_self"
    assert second.holder.acquired_at == first.holder.acquired_at


def test_only_the_holder_may_release_the_lock(tmp_path: Path) -> None:
    """Covers: OM-1 a session never releases another session's lock."""
    memory = _memory(tmp_path)
    memory.lock.acquire(session_id="orch-a", identity=_identity(4242))
    assert memory.lock.release(session_id="orch-b") is False
    assert memory.lock.path.is_file()
    assert memory.lock.release(session_id="orch-a") is True
    assert not memory.lock.path.exists()
    writer = open_session(memory, session_id="orch-b", identity=_identity(4243))
    assert isinstance(writer, OrchestratorSession)


def test_corrupt_lock_file_fails_closed(tmp_path: Path) -> None:
    """Covers: OM-1 a corrupt lock file is unsafe, not absent."""
    root = tmp_path / "memory"
    root.mkdir(parents=True)
    (root / "orchestrator-session.lock").write_text("{broken", encoding="utf-8")
    memory = OrchestratorMemory(root, probe=_probe(Liveness.DEAD), now=lambda: FIXED_NOW)
    with pytest.raises(SessionLockUnsafeError):
        memory.lock.acquire(session_id="orch-a", identity=_identity())


def test_empty_lock_file_fails_closed(tmp_path: Path) -> None:
    """Covers: OM-1 an empty lock file is unsafe, not absent."""
    root = tmp_path / "memory"
    root.mkdir(parents=True)
    (root / "orchestrator-session.lock").write_text("\n", encoding="utf-8")
    memory = OrchestratorMemory(root, probe=_probe(Liveness.DEAD), now=lambda: FIXED_NOW)
    with pytest.raises(SessionLockUnsafeError):
        memory.lock.acquire(session_id="orch-a", identity=_identity())


# --------------------------------------------------------------------------- #
# Context trigger and rotation
# --------------------------------------------------------------------------- #


def test_trigger_is_not_latched_at_exactly_fifty_percent() -> None:
    """Covers: OM-1 rotation only strictly above 50% of the window."""
    trigger = ContextTrigger.initial().observe(_usage(50))
    assert not trigger.latched


def test_trigger_latches_strictly_above_fifty_percent() -> None:
    """Covers: OM-1 latched trigger when context crosses 50%."""
    trigger = ContextTrigger.initial().observe(_usage(51))
    assert trigger.latched
    assert trigger.peak_usage is not None
    assert trigger.peak_usage.used_tokens == 51


def test_trigger_latch_survives_a_later_compaction() -> None:
    """Covers: OM-1 the latch survives compaction and is re-checked at intervals."""
    trigger = _latched(60).observe(_usage(20))
    assert trigger.latched
    assert trigger.peak_usage is not None
    assert trigger.peak_usage.used_tokens == 60


def test_trigger_reset_clears_the_latch_only_after_rotation() -> None:
    """Covers: OM-1 only a real rotation unlatches the trigger."""
    trigger = _latched()
    assert trigger.reset().latched is False
    assert trigger.reset().peak_usage is None


def test_trigger_ignores_an_unknown_observation() -> None:
    """Covers: OM-1 an unknown usage observation never fabricates a trigger."""
    trigger = ContextTrigger.initial()
    assert trigger.observe(None) is trigger


def test_unlatched_trigger_continues_in_the_current_session() -> None:
    """Covers: OM-1 no rotation without a latched trigger."""
    decision = _rotation(trigger=ContextTrigger.initial())
    assert decision.action is RotationAction.CONTINUE_CURRENT_SESSION
    assert decision.handoff is None
    assert not decision.owner_question_required


def test_at_most_a_quarter_left_continues_in_the_current_session() -> None:
    """Covers: OM-1 at most 25% remaining finishes in the current session."""
    decision = _rotation(
        progress=ChecklistProgress(total_items=8, open_items=2),
    )
    assert decision.action is RotationAction.CONTINUE_CURRENT_SESSION
    assert decision.remaining_ratio == pytest.approx(MAX_REMAINING_RATIO)
    assert decision.handoff is None


def test_more_than_a_quarter_left_rotates_with_a_handoff() -> None:
    """Covers: OM-1 over 25% remaining rotates with a durable handoff."""
    decision = _rotation(progress=ChecklistProgress(total_items=10, open_items=3))
    assert decision.action is RotationAction.ROTATE_SESSION
    assert decision.handoff is not None
    assert decision.handoff.handoff_id == handoff_id_for(RUN, WAVE, POINT)
    assert decision.handoff.open_items == 3
    assert decision.handoff.total_items == 10
    assert decision.handoff.from_session_id == "orch-a"
    assert decision.owner_question_required
    assert not decision.self_split
    assert decision.follow_up_task_ids == ()


def test_unknown_open_count_is_treated_as_more_than_the_threshold() -> None:
    """Covers: OM-1 an unknown open count counts as over 25% remaining."""
    progress = ChecklistProgress(total_items=10, open_items=None)
    assert progress.remaining_ratio is None
    assert progress.remaining_above_threshold
    decision = _rotation(progress=progress)
    assert decision.action is RotationAction.ROTATE_SESSION
    assert decision.remaining_known is False
    assert decision.owner_question_required


def test_full_autonomy_self_splits_into_handoff_started_follow_ups() -> None:
    """Covers: OM-1 full autonomy self-splits the task instead of asking."""
    decision = _rotation(
        mode=AuthorityMode.FULL_AUTONOMY,
        progress=ChecklistProgress(total_items=10, open_items=6),
        open_item_ids=tuple(f"item-{index}" for index in range(6)),
    )
    assert decision.action is RotationAction.ROTATE_SESSION
    assert decision.self_split
    assert not decision.owner_question_required
    assert decision.follow_up_task_ids == (f"{TASK}-h1",)
    assert decision.handoff is not None
    assert decision.handoff.follow_up_task_ids == (f"{TASK}-h1",)


def test_self_split_chunks_follow_ups_deterministically() -> None:
    """Covers: OM-1 follow-up task ids are deterministic and bounded."""
    decision = _rotation(
        mode=AuthorityMode.FULL_AUTONOMY,
        progress=ChecklistProgress(total_items=10, open_items=10),
        open_item_ids=tuple(f"item-{index}" for index in range(10)),
        max_items_per_follow_up=4,
    )
    assert decision.follow_up_task_ids == (f"{TASK}-h1", f"{TASK}-h2", f"{TASK}-h3")


def test_autonomous_mode_still_raises_an_owner_question() -> None:
    """Covers: OM-1 every mode except full autonomy raises an owner question."""
    for mode in (
        AuthorityMode.ASK_BEFORE_ACTIONS,
        AuthorityMode.SUPERVISED,
        AuthorityMode.AUTONOMOUS,
    ):
        decision = _rotation(mode=mode)
        assert decision.owner_question_required
        assert not decision.self_split
        assert decision.follow_up_task_ids == ()


def test_rotation_decision_is_deterministic() -> None:
    """Covers: OM-1 deterministic rotation advice."""
    first = _rotation()
    second = _rotation()
    assert first.canonical_json() == second.canonical_json()


def test_rotation_rejects_malformed_progress() -> None:
    """Covers: OM-1 rotation input validation fails closed."""
    with pytest.raises(OrchestratorMemoryError):
        ChecklistProgress(total_items=0, open_items=0)
    with pytest.raises(OrchestratorMemoryError):
        ChecklistProgress(total_items=3, open_items=4)
    with pytest.raises(OrchestratorMemoryError):
        ChecklistProgress(total_items=3, open_items=-1)
    with pytest.raises(OrchestratorMemoryError):
        _rotation(mode="not-a-mode")


# --------------------------------------------------------------------------- #
# Handoffs
# --------------------------------------------------------------------------- #


def _handoff(**overrides: Any) -> OrchestratorHandoff:
    base: dict[str, Any] = {
        "handoff_id": handoff_id_for(RUN, WAVE, POINT),
        "task_id": TASK,
        "run_id": RUN,
        "wave_id": WAVE,
        "decision_point_id": POINT,
        "from_session_id": "orch-a",
        "total_items": 10,
        "open_items": 6,
        "open_item_ids": ("item-1", "item-2"),
        "remaining_ratio": 0.6,
        "used_tokens": 60,
        "total_tokens": 100,
        "follow_up_task_ids": (f"{TASK}-h1",),
        "reason": "context_over_half_window",
    }
    base.update(overrides)
    return OrchestratorHandoff(**base)


def test_handoff_is_recorded_durably_and_idempotently(tmp_path: Path) -> None:
    """Covers: OM-1 handoffs are durable and idempotent across replays."""
    memory = _memory(tmp_path)
    handoff = _handoff()
    first = memory.handoffs.record(handoff)
    second = memory.handoffs.record(handoff)
    assert first == second
    assert first.is_file()
    assert memory.handoffs.list_ids() == (handoff.handoff_id,)
    loaded = memory.handoffs.load(handoff.handoff_id)
    assert loaded.canonical_json() == handoff.canonical_json()


def test_handoff_record_is_never_silently_overwritten(tmp_path: Path) -> None:
    """Covers: OM-1 a durable handoff record is never overwritten."""
    memory = _memory(tmp_path)
    memory.handoffs.record(_handoff())
    with pytest.raises(HandoffConflictError):
        memory.handoffs.record(_handoff(open_items=5))


def test_session_records_the_rotation_handoff(tmp_path: Path) -> None:
    """Covers: OM-1 rotation records its handoff through the writer session."""
    memory = _memory(tmp_path)
    session = open_session(memory, session_id="orch-a", identity=_identity())
    assert isinstance(session, OrchestratorSession)
    decision = session.decide_rotation(
        trigger=_latched(),
        progress=ChecklistProgress(total_items=10, open_items=8),
        mode=AuthorityMode.SUPERVISED,
        task_id=TASK,
        run_id=RUN,
        wave_id=WAVE,
        decision_point_id=POINT,
        open_item_ids=("item-1", "item-2"),
    )
    assert decision.handoff is not None
    recorded = memory.latest_handoff(TASK)
    assert recorded is not None
    assert recorded.canonical_json() == decision.handoff.canonical_json()


def test_handoff_validation_fails_closed() -> None:
    """Covers: OM-1 handoff validation fails closed."""
    with pytest.raises(OrchestratorMemoryError):
        _handoff(total_items=0)
    with pytest.raises(OrchestratorMemoryError):
        _handoff(open_item_ids=("bad item",))


# --------------------------------------------------------------------------- #
# Transactional turns and crash recovery
# --------------------------------------------------------------------------- #


def _journal(tmp_path: Path) -> TurnJournal:
    return TurnJournal(tmp_path / "journal.jsonl", now=lambda: FIXED_NOW)


def test_turn_is_open_until_it_is_handled(tmp_path: Path) -> None:
    """Covers: OM-1 a decision point is acknowledged only after full handling."""
    journal = _journal(tmp_path)
    opened = journal.begin(POINT, RUN, WAVE, pending_actions=("apply-plan",))
    assert opened.record.state is TurnState.OPEN
    assert not opened.already_handled
    handled = journal.commit(opened.record.decision_key, ("apply-plan",))
    assert handled.state is TurnState.HANDLED
    assert handled.committed_actions == ("apply-plan",)


def test_handled_decision_point_is_never_reopened(tmp_path: Path) -> None:
    """Covers: OM-1 a handled decision point repeats nothing."""
    journal = _journal(tmp_path)
    opened = journal.begin(POINT, RUN, WAVE)
    journal.commit(opened.record.decision_key, ("apply-plan",))
    history_length = len(journal.history())
    replay = journal.begin(POINT, RUN, WAVE)
    assert replay.already_handled
    assert len(journal.history()) == history_length


def test_commit_is_idempotent_but_never_rewrites_committed_actions(tmp_path: Path) -> None:
    """Covers: OM-1 idempotent acknowledgement never alters a committed action."""
    journal = _journal(tmp_path)
    key = journal.begin(POINT, RUN, WAVE).record.decision_key
    journal.commit(key, ("apply-plan",))
    history_length = len(journal.history())
    journal.commit(key, ("apply-plan",))
    assert len(journal.history()) == history_length
    with pytest.raises(TurnConflictError):
        journal.commit(key, ("different-plan",))


def test_mid_turn_kill_repeats_nothing_and_rolls_back(tmp_path: Path) -> None:
    """Covers: OM-1 a mid-turn death repeats nothing and rolls back uncommitted actions."""
    journal = _journal(tmp_path)
    opened = journal.begin(POINT, RUN, WAVE, pending_actions=("apply-plan", "notify"))
    assert opened.record.decision_key == idempotency_key(POINT, RUN, WAVE)
    recovered = journal.recover()
    assert len(recovered) == 1
    plan = recovered[0]
    assert plan.decision_key == idempotency_key(POINT, RUN, WAVE)
    assert plan.decision_point_id == POINT
    assert plan.rolled_back_actions == ("apply-plan", "notify")
    assert plan.reissue_decision_point
    assert plan.session_action == "new_session"
    assert plan.brief_mode == "summary"
    assert not plan.owner_question_required
    assert journal.latest(plan.decision_key).state is TurnState.ROLLED_BACK  # type: ignore[union-attr]
    assert journal.recover() == ()


def test_committed_turn_survives_recovery_without_replay(tmp_path: Path) -> None:
    """Covers: OM-1 crash recovery keeps what the turn already committed."""
    journal = _journal(tmp_path)
    key = journal.begin(POINT, RUN, WAVE, pending_actions=("apply-plan",)).record.decision_key
    journal.commit(key, ("apply-plan",))
    assert journal.recover() == ()
    replay = journal.begin(POINT, RUN, WAVE)
    assert replay.already_handled
    assert replay.record.committed_actions == ("apply-plan",)


def test_repeated_failures_escalate_to_an_owner_question(tmp_path: Path) -> None:
    """Covers: OM-1 two failed attempts escalate to an owner question."""
    journal = _journal(tmp_path)
    journal.begin(POINT, RUN, WAVE)
    first = journal.recover()[0]
    assert not first.owner_question_required
    journal.begin(POINT, RUN, WAVE)
    second = journal.recover()[0]
    assert not second.owner_question_required
    journal.begin(POINT, RUN, WAVE)
    third = journal.recover()[0]
    assert third.owner_question_required


def test_context_overflow_reissues_the_exact_decision_point(tmp_path: Path) -> None:
    """Covers: OM-1 context overflow re-issues the exact decision point in a clean session."""
    journal = _journal(tmp_path)
    key = journal.begin(POINT, RUN, WAVE).record.decision_key
    plan = journal.recover()[0]
    assert plan.decision_key == key
    assert plan.session_action == "new_session"
    assert plan.brief_mode == "summary"
    assert plan.reissue_decision_point


def test_committed_turn_cannot_be_rolled_back(tmp_path: Path) -> None:
    """Covers: OM-1 a committed action is never rolled back."""
    journal = _journal(tmp_path)
    key = journal.begin(POINT, RUN, WAVE).record.decision_key
    journal.commit(key, ("apply-plan",))
    with pytest.raises(TurnJournalError):
        journal.rollback(key, reason="too-late")


def test_journal_rejects_unknown_decision_keys(tmp_path: Path) -> None:
    """Covers: OM-1 the journal never invents a turn."""
    journal = _journal(tmp_path)
    with pytest.raises(TurnJournalError):
        journal.commit("missing-key", ("apply-plan",))
    with pytest.raises(TurnJournalError):
        journal.rollback("missing-key", reason="nope")


def test_journal_reports_corruption(tmp_path: Path) -> None:
    """Covers: OM-1 a corrupt turn journal fails closed."""
    path = tmp_path / "journal.jsonl"
    path.write_text("{broken\n", encoding="utf-8")
    with pytest.raises(TurnJournalError):
        _journal(tmp_path).history()


# --------------------------------------------------------------------------- #
# Memory facade
# --------------------------------------------------------------------------- #


def test_memory_facade_exposes_its_durable_parts(tmp_path: Path) -> None:
    """Covers: OM-1 the memory root owns notebook, archive, lock, handoffs and journal."""
    memory = _memory(tmp_path)
    assert memory.notebook.path.name == "notebook.json"
    assert memory.archive.path.name == "notebook-archive.jsonl"
    assert memory.lock.path.name == "orchestrator-session.lock"
    assert memory.handoffs.root.name == "handoffs"
    assert memory.journal.path.name == "turn-journal.jsonl"


def test_writer_session_builds_a_digest_and_mutates_notebook(tmp_path: Path) -> None:
    """Covers: OM-1 the writer session owns notebook mutation and digest building."""
    memory = _memory(tmp_path)
    session = open_session(memory, session_id="orch-a", identity=_identity())
    assert isinstance(session, OrchestratorSession)
    session.put_notebook_entry(_entry("pin", NotebookTier.PINNED))
    assert [entry.key for entry in session.notebook_entries()] == ["pin"]
    assert session.build_digest(_facts()).sections == DIGEST_SECTION_ORDER
    assert session.release() is True


def test_writer_session_runs_a_transactional_turn(tmp_path: Path) -> None:
    """Covers: OM-1 the writer session owns the transactional turn lifecycle."""
    memory = _memory(tmp_path)
    session = open_session(memory, session_id="orch-a", identity=_identity())
    assert isinstance(session, OrchestratorSession)
    opened = session.begin_turn(POINT, RUN, WAVE, pending_actions=("apply-plan",))
    assert opened.record.state is TurnState.OPEN
    handled = session.commit_turn(opened.record.decision_key, ("apply-plan",))
    assert handled.state is TurnState.HANDLED
    assert session.recover_turns() == ()


def test_memory_rejects_malformed_root_arguments(tmp_path: Path) -> None:
    """Covers: OM-1 session identifiers are validated before any write."""
    memory = _memory(tmp_path)
    with pytest.raises(OrchestratorMemoryError):
        open_session(memory, session_id="bad session id")
    with pytest.raises(OrchestratorMemoryError):
        open_session(object(), session_id="orch-a")  # type: ignore[arg-type]


def test_status_log_and_handoff_files_are_human_readable(tmp_path: Path) -> None:
    """Covers: OM-1 durable records are plain versioned JSON on disk."""
    memory = _memory(tmp_path)
    session = open_session(memory, session_id="orch-a", identity=_identity())
    assert isinstance(session, OrchestratorSession)
    decision = session.decide_rotation(
        trigger=_latched(),
        progress=ChecklistProgress(total_items=4, open_items=3),
        mode=AuthorityMode.FULL_AUTONOMY,
        task_id=TASK,
        run_id=RUN,
        wave_id=WAVE,
        decision_point_id=POINT,
        open_item_ids=("item-1", "item-2", "item-3"),
    )
    assert decision.handoff is not None
    path = memory.handoffs.path_for(decision.handoff.handoff_id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == "orchestrator-memory/1"
    assert payload["handoff_id"] == decision.handoff.handoff_id
