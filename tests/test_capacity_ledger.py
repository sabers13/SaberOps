"""M2-4a ledger tests: delegation, keys, estimates, reservations, learning."""

from __future__ import annotations

import dataclasses
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from saberops.capacity.cost_model import SizeBucket
from saberops.capacity.ledger import (
    PoolDraw,
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


_T0 = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


def _kinds(*pairs: tuple[str, str, str]) -> dict[tuple[str, str], str]:
    return {(pool, window): kind for pool, window, kind in pairs}


# L5: estimate arithmetic over reservations.
def test_l5_estimate_arithmetic(tmp_path: Path) -> None:
    db = _db(tmp_path)
    t0, t1, t2, t3 = (_T0 + timedelta(minutes=m) for m in (0, 10, 20, 30))
    # A reservation created before the observation never counts.
    reserve(db, run_id="run-early", route="opencode/gpt-5",
            draws=[PoolDraw("opencode", "5h")],
            size_bucket=SizeBucket.S, now=t0)
    record_owner_quota(db, "opencode", "5h", 60.0, None,
                       source="OWNER", now=t1)
    kinds = _kinds(("opencode", "5h", "UNKNOWN"))
    assert estimate(db, "opencode", "5h", now=t1,
                    window_kinds=kinds).remaining_est == 60.0
    reserve(db, run_id="run-a", route="opencode/gpt-5",
            draws=[PoolDraw("opencode", "5h", 1.0)],
            size_bucket=SizeBucket.S, now=t2)
    reserve(db, run_id="run-b", route="opencode/gpt-5",
            draws=[PoolDraw("opencode", "5h", 2.0)],
            size_bucket=SizeBucket.S, now=t2)
    # Prior 5h/S = 3.0, spread 1.5: costs 3.0 and 6.0, spreads 1.5 and 3.0.
    est = estimate(db, "opencode", "5h", now=t3, window_kinds=kinds)
    assert est.remaining_est == 51.0
    assert est.uncertainty == 4.5
    assert est.observed_at == t1.isoformat()
    assert est.source == "OWNER"
    assert est.stale is False
    assert est.excluded_until is None
    # RELEASED rows never count.
    assert release(db, "run-b", now=t3) == 1
    est2 = estimate(db, "opencode", "5h", now=t3, window_kinds=kinds)
    assert est2.remaining_est == 57.0
    assert est2.uncertainty == 1.5


def test_l5_clamp_at_zero(tmp_path: Path) -> None:
    db = _db(tmp_path)
    record_owner_quota(db, "opencode", "5h", 5.0, None,
                       source="OWNER", now=_T0)
    reserve(db, run_id="run-big", route="opencode/gpt-5",
            draws=[PoolDraw("opencode", "5h", 4.0)],
            size_bucket=SizeBucket.L, now=_T0 + timedelta(minutes=1))
    # Prior 5h/L = 15.0 x 4 = 60 > 5 -> clamped.
    est = estimate(db, "opencode", "5h", now=_T0 + timedelta(minutes=2),
                   window_kinds=_kinds(("opencode", "5h", "UNKNOWN")))
    assert est.remaining_est == 0.0


# L6: window roll behaviour.
def test_l6_window_roll(tmp_path: Path) -> None:
    reset = _T0 + timedelta(hours=1)
    after = reset + timedelta(minutes=5)
    for kind, stale in [("CALENDAR", False), ("ROLLING", True), ("UNKNOWN", True)]:
        db = Database(tmp_path / f"roll-{kind}.db")
        record_owner_quota(db, "codex", "weekly", 12.0, reset.isoformat(),
                           source="OWNER", now=_T0)
        est = estimate(db, "codex", "weekly", now=after,
                       window_kinds={( "codex", "weekly"): kind})
        assert est.remaining_est == 100.0
        assert est.stale is stale


def test_l6_pre_reset_reservations_stop_counting(tmp_path: Path) -> None:
    db = _db(tmp_path)
    reset = _T0 + timedelta(hours=1)
    after = reset + timedelta(minutes=5)
    record_owner_quota(db, "codex", "weekly", 12.0, reset.isoformat(),
                       source="OWNER", now=_T0)
    reserve(db, run_id="run-old", route="codex/gpt-5",
            draws=[PoolDraw("codex", "weekly")],
            size_bucket=SizeBucket.M,
            now=_T0 + timedelta(minutes=30))
    est = estimate(db, "codex", "weekly", now=after,
                   window_kinds={("codex", "weekly"): "CALENDAR"})
    assert est.remaining_est == 100.0
    assert est.stale is False


# L7: active exclusions deny; expired ones do not.
def test_l7_exclusion(tmp_path: Path) -> None:
    from saberops.capacity.store import upsert_exclusion

    db = _db(tmp_path)
    record_owner_quota(db, "codex", "weekly", 80.0, None,
                       source="OWNER", now=_T0)
    until = _T0 + timedelta(hours=2)
    upsert_exclusion(db, pool_id="codex", window_id="weekly",
                     reason="QUOTA_EXHAUSTED", until=until,
                     until_confidence="EXACT", source="test", now=_T0)
    est = estimate(db, "codex", "weekly", now=_T0 + timedelta(minutes=1),
                   window_kinds=_kinds(("codex", "weekly", "UNKNOWN")))
    assert est.remaining_est == 0.0
    assert est.source == "EXHAUSTION"
    assert est.excluded_until == until.isoformat()
    # ALL-window exclusion also denies the window.
    db2 = Database(tmp_path / "exc-all.db")
    record_owner_quota(db2, "codex", "weekly", 80.0, None,
                       source="OWNER", now=_T0)
    upsert_exclusion(db2, pool_id="codex", window_id="ALL",
                     reason="QUOTA_EXHAUSTED", until=until,
                     until_confidence="EXACT", source="test", now=_T0)
    est_all = estimate(db2, "codex", "weekly", now=_T0 + timedelta(minutes=1),
                       window_kinds=_kinds(("codex", "weekly", "UNKNOWN")))
    assert est_all.remaining_est == 0.0
    assert est_all.source == "EXHAUSTION"
    # An expired exclusion does not deny.
    db3 = Database(tmp_path / "exc-exp.db")
    record_owner_quota(db3, "codex", "weekly", 80.0, None,
                       source="OWNER", now=_T0)
    upsert_exclusion(db3, pool_id="codex", window_id="weekly",
                     reason="QUOTA_EXHAUSTED",
                     until=_T0 - timedelta(hours=1),
                     until_confidence="EXACT", source="test", now=_T0)
    est_exp = estimate(db3, "codex", "weekly", now=_T0,
                       window_kinds=_kinds(("codex", "weekly", "UNKNOWN")))
    assert est_exp.remaining_est == 80.0
    assert est_exp.source == "OWNER"


# L8: per-pool ALL fallback; neither -> None + stale.
def test_l8_fallback_to_all(tmp_path: Path) -> None:
    db = _db(tmp_path)
    record_owner_quota(db, "x", "my-pool", 44.0, None,
                       source="OWNER", now=_T0)
    kinds = _kinds(("my-pool", "5h", "UNKNOWN"))
    est = estimate(db, "my-pool", "5h", now=_T0 + timedelta(minutes=1),
                   window_kinds=kinds)
    assert est.remaining_est == 44.0
    assert est.source == "OWNER"
    missing = estimate(db, "other-pool", "5h", now=_T0 + timedelta(minutes=1),
                       window_kinds={("other-pool", "5h"): "UNKNOWN"})
    assert missing.remaining_est is None
    assert missing.stale is True
    assert missing.source is None
    assert missing.observed_at is None


# L9: reservation lifecycle.
def test_l9_reservations(tmp_path: Path) -> None:
    db = _db(tmp_path)
    first = reserve(db, run_id="run-1", route="codex/gpt-5",
                    draws=[PoolDraw("codex", "weekly"),
                           PoolDraw("codex", "daily", 2.0)],
                    size_bucket=SizeBucket.M, now=_T0)
    assert len(first) == 2
    again = reserve(db, run_id="run-1", route="codex/gpt-5",
                    draws=[PoolDraw("codex", "weekly"),
                           PoolDraw("codex", "daily", 2.0)],
                    size_bucket=SizeBucket.M, now=_T0)
    assert [r.id for r in again] == [r.id for r in first]
    assert settle(db, "run-1", total_tokens=500, now=_T0) == 2
    assert settle(db, "run-1", total_tokens=500, now=_T0) == 0
    assert settle(db, "nope", total_tokens=None, now=_T0) == 0
    assert release(db, "nope", now=_T0) == 0
    reserve(db, run_id="run-2", route="codex/gpt-5",
            draws=[PoolDraw("codex", "weekly")],
            size_bucket=SizeBucket.S, now=_T0)
    assert release(db, "run-2", now=_T0) == 1
    assert release(db, "run-2", now=_T0) == 0


def test_l9_job_id_opaque_round_trip(tmp_path: Path) -> None:
    db = _db(tmp_path)
    rows = reserve(db, run_id="run-j1", job_id=None, route="codex/gpt-5",
                   draws=[PoolDraw("codex", "weekly")],
                   size_bucket=SizeBucket.S, now=_T0)
    assert rows[0].job_id is None
    rows2 = reserve(db, run_id="run-j2", job_id="job-abc-123::opaque",
                    route="codex/gpt-5",
                    draws=[PoolDraw("codex", "weekly")],
                    size_bucket=SizeBucket.S, now=_T0)
    assert rows2[0].job_id == "job-abc-123::opaque"


# L11: snapshot shape and LedgerEstimate contract.
def test_l11_snapshot(tmp_path: Path) -> None:
    db = _db(tmp_path)
    record_owner_quota(db, "codex", "weekly", 70.0, None,
                       source="OWNER", now=_T0)
    reserve(db, run_id="run-s", route="opencode/gpt-5",
            draws=[PoolDraw("go-pool", "monthly")],
            size_bucket=SizeBucket.S, now=_T0)
    from saberops.capacity.store import upsert_exclusion

    upsert_exclusion(db, pool_id="exc-pool", window_id="ALL",
                     reason="QUOTA_EXHAUSTED",
                     until=_T0 + timedelta(hours=1),
                     until_confidence="EXACT", source="test", now=_T0)
    got = {(e.pool_id, e.window_id) for e in snapshot(db, _T0)}
    assert got == {("codex", "weekly"), ("go-pool", "monthly"),
                   ("exc-pool", "ALL")}
    fields = dataclasses.fields(snapshot(db, _T0)[0].__class__)
    assert [f.name for f in fields] == [
        "pool_id", "window_id", "remaining_est", "uncertainty",
        "observed_at", "source", "stale", "excluded_until",
    ]
    assert [f.type for f in fields] == [
        "str", "str", "float | None", "float",
        "str | None", "str | None", "bool", "str | None",
    ]


def _cost_rows(db: Database) -> dict[tuple[str, str, str, str], tuple[int, float | None]]:
    owner = db.owner_db
    with owner._get_connection() as conn:
        rows = conn.execute(
            "SELECT route, pool_id, window_id, size_bucket, samples, ewma_pct"
            " FROM capacity_cost_model"
        ).fetchall()
        return {
            (str(r["route"]), str(r["pool_id"]), str(r["window_id"]),
             str(r["size_bucket"])): (int(r["samples"]),
                                     float(r["ewma_pct"]) if r["ewma_pct"] is not None else None)
            for r in rows
        }


# L10: probe-to-probe deltas train per-route cost states.
def test_l10_learning_token_split(tmp_path: Path) -> None:
    db = _db(tmp_path)
    t0, t1, t2, t3 = (_T0 + timedelta(minutes=m) for m in (0, 10, 20, 30))
    record_owner_quota(db, "opencode", "5h", 70.0, None,
                       source="PROBE", now=t0)
    reserve(db, run_id="run-a", route="route-a/model",
            draws=[PoolDraw("opencode", "5h")],
            size_bucket=SizeBucket.S, now=t1)
    reserve(db, run_id="run-b", route="route-b/model",
            draws=[PoolDraw("opencode", "5h")],
            size_bucket=SizeBucket.S, now=t1)
    assert settle(db, "run-a", total_tokens=3000, now=t2) == 1
    assert settle(db, "run-b", total_tokens=1000, now=t2) == 1
    record_owner_quota(db, "opencode", "5h", 58.0, None,
                       source="PROBE", now=t3)
    rows = _cost_rows(db)
    assert rows[("route-a/model", "opencode", "5h", "S")] == (1, 9.0)
    assert rows[("route-b/model", "opencode", "5h", "S")] == (1, 3.0)


def test_l10_learning_equal_split_and_no_sample_cases(tmp_path: Path) -> None:
    db = _db(tmp_path)
    t0, t1, t2 = (_T0 + timedelta(minutes=m) for m in (0, 10, 20))
    record_owner_quota(db, "opencode", "5h", 58.0, None,
                       source="PROBE", now=t0)
    reserve(db, run_id="run-c", route="route-c/model",
            draws=[PoolDraw("opencode", "5h")],
            size_bucket=SizeBucket.S, now=t1)
    reserve(db, run_id="run-d", route="route-d/model",
            draws=[PoolDraw("opencode", "5h")],
            size_bucket=SizeBucket.S, now=t1)
    assert settle(db, "run-c", total_tokens=None, now=t1) == 1
    assert settle(db, "run-d", total_tokens=200, now=t1) == 1
    # Delta 8 split equally: a missing token count forces equal shares.
    record_owner_quota(db, "opencode", "5h", 50.0, None,
                       source="PROBE", now=t2)
    rows = _cost_rows(db)
    assert rows[("route-c/model", "opencode", "5h", "S")] == (1, 4.0)
    assert rows[("route-d/model", "opencode", "5h", "S")] == (1, 4.0)
    # No sample when the delta is not positive.
    t3 = t2 + timedelta(minutes=10)
    record_owner_quota(db, "opencode", "5h", 55.0, None,
                       source="PROBE", now=t3)
    assert _cost_rows(db) == rows
    # No sample when a reset lies between the two observations.
    t4 = t3 + timedelta(minutes=10)
    t5 = t4 + timedelta(minutes=10)
    reserve(db, run_id="run-e", route="route-e/model",
            draws=[PoolDraw("opencode", "5h")],
            size_bucket=SizeBucket.S, now=t4)
    assert settle(db, "run-e", total_tokens=100, now=t4) == 1
    record_owner_quota(db, "opencode", "5h", 40.0, t4.isoformat(),
                       source="PROBE", now=t5)
    assert ("route-e/model", "opencode", "5h", "S") not in _cost_rows(db)


def test_l10_no_samples_for_all_keys_or_owner(tmp_path: Path) -> None:
    db = _db(tmp_path)
    t0, t1, t2 = (_T0 + timedelta(minutes=m) for m in (0, 10, 20))
    # ALL-keyed PROBE observations never produce samples.
    record_owner_quota(db, "x", "my-pool", 70.0, None,
                       source="PROBE", now=t0)
    reserve(db, run_id="run-all", route="route-a/model",
            draws=[PoolDraw("my-pool", "ALL")],
            size_bucket=SizeBucket.S, now=t1)
    assert settle(db, "run-all", total_tokens=100, now=t1) == 1
    record_owner_quota(db, "x", "my-pool", 58.0, None,
                       source="PROBE", now=t2)
    assert _cost_rows(db) == {}
    # OWNER observations never produce samples either.
    db2 = Database(tmp_path / "owner-src.db")
    record_owner_quota(db2, "opencode", "5h", 70.0, None,
                       source="OWNER", now=t0)
    reserve(db2, run_id="run-o", route="route-a/model",
            draws=[PoolDraw("opencode", "5h")],
            size_bucket=SizeBucket.S, now=t1)
    assert settle(db2, "run-o", total_tokens=100, now=t1) == 1
    record_owner_quota(db2, "opencode", "5h", 58.0, None,
                       source="OWNER", now=t2)
    assert _cost_rows(db2) == {}


def test_l10_learn_from_probe_per_window_and_row_id(tmp_path: Path) -> None:
    """Covers: ADR-0002 D5/W2 #9 and #10 per-window probe observation and row ID skip."""
    from saberops.capacity.ledger import record_probe_window_observation

    db = _db(tmp_path)
    t0 = _T0
    t1 = t0 + timedelta(minutes=10)
    t2 = t0 + timedelta(minutes=20)
    t3 = t0 + timedelta(minutes=30)

    obs1 = record_probe_window_observation(
        db,
        pool_id="chatgpt-plus",
        window_id="5h",
        remaining_pct=70.0,
        reset_at=None,
        harness="codex",
        now=t0,
    )
    assert obs1.startswith("obs_")

    obs2 = record_probe_window_observation(
        db,
        pool_id="chatgpt-plus",
        window_id="5h",
        remaining_pct=70.0,
        reset_at=None,
        harness="codex",
        now=t1,
    )
    assert obs2 != obs1

    reserve(
        db,
        run_id="run-p1",
        route="openai/gpt-5",
        draws=[PoolDraw("chatgpt-plus", "5h")],
        size_bucket=SizeBucket.M,
        now=t2,
    )
    assert settle(db, "run-p1", total_tokens=2000, now=t2) == 1

    obs3 = record_probe_window_observation(
        db,
        pool_id="chatgpt-plus",
        window_id="5h",
        remaining_pct=65.0,
        reset_at=None,
        harness="codex",
        now=t3,
    )
    assert obs3 != obs2
    rows = _cost_rows(db)
    assert rows[("openai/gpt-5", "chatgpt-plus", "5h", "M")] == (1, 5.0)


# L13: the legacy policy surface is gone; the vocabulary stays.
def test_l13_legacy_policy_deleted() -> None:
    import saberops.quota as quota

    for name in ("apply_quota_policy", "derive_routing_state",
                 "QUOTA_THRESHOLD_PERCENT", "CODEX_PROVIDER",
                 "CODEX_WEEKLY_POOL"):
        assert not hasattr(quota, name), name
    assert callable(quota.normalize_quota_observation)
