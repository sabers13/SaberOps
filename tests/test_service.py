"""OrchestratorService quota persistence (ported from the console cut).

Ported from test_c16c2_quota_set_round_trip: backend state, not HTML.
"""

from __future__ import annotations

from pathlib import Path

from saberops.db import Database


def test_ported_quota_set_round_trip(isolated: Path) -> None:
    """Ported from test_c16c2_quota_set_round_trip (backend state, not HTML)."""
    from saberops.service import OrchestratorService

    service = OrchestratorService(db=Database(isolated / "quota.db"))
    state = service.set_quota(
        provider="codex", pool="weekly", remaining_percent=42.5, reset_at=None
    )
    assert state.remaining_percent == 42.5
    fetched = service.get_quota(provider="codex", pool="weekly")
    assert fetched is not None
    assert fetched.remaining_percent == 42.5
    assert fetched.provider == "codex" and fetched.pool == "weekly"
