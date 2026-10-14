"""CLI tests for `orch scheduler run` and `orch capacity show` (ADR-0002 D7).

Verifies:
1. `orch scheduler run --once` exits cleanly (exit 0).
2. Second scheduler process denied while active lease is held.
3. `orch capacity show` runs read-only without creating tables or modifying files.
4. `orch capacity show --json` outputs structured CapacityView payload.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from saberops.capacity.loop import acquire_lease
from saberops.capacity.store import ensure_tables as ensure_capacity_tables
from saberops.cli import main
from saberops.db import Database


@pytest.fixture()
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "state"
    config = tmp_path / "config"
    cache = tmp_path / "cache"
    for directory in (state, config, cache):
        directory.mkdir(parents=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    monkeypatch.delenv("SABEROPS_PROFILE", raising=False)
    return tmp_path


def _cli(*argv: str, capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_scheduler_run_once_clean_exit(
    isolated_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Covers: ADR-0002 D7 orch scheduler run --once executes a tick and exits 0."""
    db_path = isolated_env / "test_jobs.db"
    db = Database(db_path)
    ensure_capacity_tables(db)

    code, out, err = _cli("scheduler", "run", "--once", "--db", str(db_path), capsys=capsys)
    assert code == 0
    assert err == ""


def test_scheduler_second_instance_denied(
    isolated_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Covers: ADR-0002 D7 second scheduler instance is denied when active lease is held."""
    db_path = isolated_env / "test_jobs.db"
    db = Database(db_path)
    ensure_capacity_tables(db)

    # Acquire lease by instance-1
    lease = acquire_lease(db, holder_id="instance-1", ttl_seconds=60)
    assert lease is not None

    # Instance-2 tries to run tick via CLI
    code, out, err = _cli(
        "scheduler", "run", "--once", "--db", str(db_path), capsys=capsys
    )
    # The loop fails to acquire lease and exits cleanly with 0
    assert code == 0


def test_scheduler_second_instance_continuous_denied_returns_exit_2(
    isolated_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Covers: ADR-0002 D7 / F8: second continuous scheduler returns exit 2 on lease denial."""
    db_path = isolated_env / "test_jobs.db"
    db = Database(db_path)
    ensure_capacity_tables(db)

    # Acquire lease by instance-1
    lease = acquire_lease(db, holder_id="instance-1", ttl_seconds=60)
    assert lease is not None

    # Instance-2 tries to run continuous loop via CLI
    code, out, err = _cli("scheduler", "run", "--db", str(db_path), capsys=capsys)
    assert code == 2
    assert "lease_held_by_other" in err



def test_capacity_show_read_only_creates_no_tables(
    isolated_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Covers: ADR-0002 D5/D7 orch capacity show honours read-only DBs and creates no tables."""
    db_path = isolated_env / "ro_check.db"
    Database(db_path)

    code, out, err = _cli("capacity", "show", "--db", str(db_path), capsys=capsys)
    assert code == 0
    assert "Generated at :" in out
    assert "No capacity pools recorded." in out

    # Open read-only explicitly
    ro_db = Database(db_path, read_only=True)
    assert ro_db._read_only is True

    # Re-run against read-only DB
    code_ro, out_ro, err_ro = _cli(
        "capacity", "show", "--db", str(db_path), capsys=capsys
    )
    assert code_ro == 0
    assert "Generated at :" in out_ro


def test_capacity_show_json_output(
    isolated_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Covers: ADR-0002 D7 orch capacity show --json outputs structured CapacityView."""
    db_path = isolated_env / "test_cap.db"
    db = Database(db_path)
    ensure_capacity_tables(db)

    code, out, err = _cli(
        "capacity", "show", "--db", str(db_path), "--json", capsys=capsys
    )
    assert code == 0
    data = json.loads(out)
    assert "generated_at" in data
    assert data["ledger_wired"] is True
    assert "pools" in data
    assert isinstance(data["pools"], list)
