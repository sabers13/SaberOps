"""I-004 cross-lane capacity read-model and size-bucket contracts."""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from saberops.capacity import ledger
from saberops.capacity.cost_model import SizeBucket
from saberops.capacity.store import upsert_exclusion
from saberops.db import Database
from saberops.jobs.service import capacity


@pytest.fixture(autouse=True)
def isolated_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))


def test_capacity_maps_ledger_entries(tmp_path: Path) -> None:
    db = Database(tmp_path / "owner.db")
    observation = db.set_quota("codex", "weekly", 75.0, source="OWNER")
    now = datetime.fromisoformat(observation.updated_at) + timedelta(seconds=1)
    ledger.reserve(
        db, run_id="run-glue", job_id="opaque-job", route="codex/model",
        draws=[ledger.PoolDraw("codex", "weekly")], size_bucket=SizeBucket.M, now=now,
    )
    until = now + timedelta(hours=1)
    upsert_exclusion(
        db, pool_id="aaa-excluded", window_id="5h", reason="EXHAUSTED",
        until=until, until_confidence="EXACT", source="EXHAUSTION", now=now,
    )
    expected = ledger.snapshot(db, now)
    assert len(expected) == 2
    view = capacity(db, now=now)
    assert view.generated_at == now.isoformat()
    assert view.ledger_wired is True
    assert [asdict(pool) for pool in view.pools] == [
        asdict(entry) for entry in sorted(expected, key=lambda entry: (entry.pool_id, entry.window_id))
    ]
    assert [(pool.pool_id, pool.window_id) for pool in view.pools] == [
        ("aaa-excluded", "5h"), ("codex", "weekly"),
    ]
    assert view.pools[0].remaining_est == 0.0
    assert view.pools[0].excluded_until == until.isoformat()
    assert view.pools[1].source == "OWNER"
    assert view.pools[1].remaining_est is not None
    assert view.pools[1].remaining_est < 75.0


def test_capacity_default_now_is_utc_datetime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    moments: list[datetime] = []

    def capture(db: Database, now: datetime) -> list[ledger.LedgerEstimate]:
        moments.append(now)
        return []

    monkeypatch.setattr(ledger, "snapshot", capture)
    view = capacity(Database(tmp_path / "empty.db"))
    assert len(moments) == 1
    assert moments[0].tzinfo is UTC
    assert view.generated_at == moments[0].isoformat()
    assert view.ledger_wired is True
    assert view.pools == ()
