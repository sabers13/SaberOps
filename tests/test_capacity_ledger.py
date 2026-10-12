"""M2-4a ledger tests: delegation, keys, estimates, reservations, learning."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from saberops.capacity.ledger import (
    PoolDraw,
    SizeBucket,
    estimate,
    get_owner_quota,
    list_owner_quota,
    record_owner_quota,
    release,
    reserve,
    settle,
    snapshot,
)
from saberops.capacity.snapshot import _legacy_pool_and_window
from saberops.db import Database
from saberops.models import QuotaState, RoutingState


def _db(tmp_path: Path, name: str = "ledger.db") -> Database:
    return Database(tmp_path / name)


def _obs_key(db: Database) -> tuple[str, str, str]:
    owner = db.owner_db
    with owner._get_connection() as conn:
        row = conn.execute(
            "SELECT pool_id, window_id, source FROM capacity_observations"
            " ORDER BY observed_at DESC, id DESC LIMIT 1"
        ).fetchone()
        assert row is not None
        return str(row["pool_id"]), str(row["window_id"]), str(row["source"])


# L1: ledger keys agree with the routing seam's mapping.
def test_l1_key_agreement(tmp_path: Path) -> None:
    db = _db(tmp_path)
    for provider, pool in [
        ("codex", "weekly"), ("opencode", "5h"),
        ("claude", "ALL"), ("x", "opencode-go"),
    ]:
        db.set_quota(provider, pool, 50.0)
        expected = _legacy_pool_and_window(
            SimpleNamespace(provider=provider, pool=pool))
        assert _obs_key(db)[:2] == expected
    # A row carrying pool_id maps pool-scoped through the same function.
    row = QuotaState(
        provider="codex", pool="weekly", remaining_percent=50.0,
        reset_at=None, routing_state=RoutingState.NORMAL,
        updated_at="2026-10-12T00:00:00+00:00", pool_id="my-pool",
    )
    assert _legacy_pool_and_window(row) == ("my-pool", "weekly")


# L2: delegation round-trip matches 7cac956 row semantics, NORMAL state.
def test_l2_delegation_round_trip(tmp_path: Path) -> None:
    db = _db(tmp_path)
    state = db.set_quota("Codex", "Weekly", 70.0, "2026-10-19T00:00:00+00:00")
    assert state.provider == "codex"
    assert state.pool == "weekly"
    assert state.remaining_percent == 70.0
    assert state.reset_at == "2026-10-19T00:00:00+00:00"
    assert state.routing_state is RoutingState.NORMAL
    assert isinstance(state.updated_at, str) and state.updated_at
    listed = db.list_quota()
    assert len(listed) == 1
    assert listed[0] == state
    assert db.get_quota("codex", "weekly") == state
    assert list_owner_quota(db) == [state]
    assert get_owner_quota(db, "CODEX", "WEEKLY") == state
    assert _obs_key(db) == ("codex", "weekly", "OWNER")


def test_l2_project_db_writes_only_owner_file(tmp_path: Path) -> None:
    owner = Database(tmp_path / "owner.db")
    project = Database(tmp_path / "proj.db", owner_db=owner)
    project.set_quota("codex", "weekly", 42.0)
    assert project.list_quota()[0].remaining_percent == 42.0
    with sqlite3.connect(str(tmp_path / "proj.db")) as conn:
        count = conn.execute("SELECT COUNT(*) FROM quota_state").fetchone()[0]
        assert count == 0
    with sqlite3.connect(str(tmp_path / "owner.db")) as conn:
        count = conn.execute("SELECT COUNT(*) FROM quota_state").fetchone()[0]
        assert count == 1
        obs = conn.execute("SELECT COUNT(*) FROM capacity_observations").fetchone()[0]
        assert obs == 1


# L3: no legacy BLOCKED/DEMOTED states are ever produced.
def test_l3_no_legacy_states(tmp_path: Path) -> None:
    db = _db(tmp_path)
    for provider, pool in [("codex", "weekly"), ("opencode", "5h")]:
        for pct in (0.0, 10.0, 24.9, 25.0, 100.0):
            state = db.set_quota(provider, pool, pct)
            assert state.routing_state is RoutingState.NORMAL
    missing = db.enable_quota("codex", "weekly")
    assert missing.routing_state is RoutingState.NORMAL
    assert missing.remaining_percent == 100.0


# L4: read-only databases read and create nothing.
def test_l4_read_only(tmp_path: Path) -> None:
    path = tmp_path / "ro.db"
    db = _db(tmp_path, "ro.db")
    db.set_quota("codex", "weekly", 33.0)
    assert path.is_file()

    def _entries() -> set[str]:
        return {
            p.name for p in tmp_path.iterdir()
            if not p.name.endswith(("-wal", "-shm", "-journal"))
        }

    before = _entries()
    ro = Database(path, read_only=True)
    rows = ro.list_quota()
    assert len(rows) == 1 and rows[0].remaining_percent == 33.0
    assert ro.get_quota("codex", "weekly") == rows[0]
    assert ro.get_quota("codex", "missing") is None
    assert _entries() == before


# L12: probe red-line rows are recorded with source PROBE.
def test_l12_probe_source(tmp_path: Path) -> None:
    from saberops.capacity.probes import handle_probe_result
    from saberops.control_plane.quota_policy import QuotaPoolPolicy

    db = _db(tmp_path)
    policies = {"codex-pool": QuotaPoolPolicy(pool_id="codex-pool", members=("codex",))}
    handle_probe_result(
        db, harness="codex", windows=[("daily", 50.0, None, "EXACT")],
        now=datetime(2026, 10, 12, 12, 0, tzinfo=UTC),
        pool_policies=policies,
    )
    row = db.get_quota("codex", "codex-pool")
    assert row is not None
    assert row.remaining_percent == 50.0
    assert row.routing_state is RoutingState.NORMAL
    assert _obs_key(db) == ("codex-pool", "ALL", "PROBE")
