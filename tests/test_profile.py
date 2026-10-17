"""R3 first-user profile checkpoint.

Web setup surfaces were removed in L-A step 5; the owner profile
namespace isolation assertions below stay because `orch ui --profile`
and all state paths still depend on them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from saberops.paths import (
    active_profile,
    saberops_config_dir,
    saberops_state_dir,
)


@pytest.fixture()
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "state"
    config = tmp_path / "config"
    cache = tmp_path / "cache"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    monkeypatch.delenv("SABEROPS_PROFILE", raising=False)
    return tmp_path


def test_profile_isolates_all_owner_namespaces(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    default_state = saberops_state_dir()
    default_config = saberops_config_dir()

    monkeypatch.setenv("SABEROPS_PROFILE", "dogfood-zero")
    assert active_profile() == "dogfood-zero"
    assert saberops_state_dir() == default_state / "profiles" / "dogfood-zero"
    assert saberops_config_dir() == default_config / "profiles" / "dogfood-zero"


@pytest.mark.parametrize("bad", ["../escape", "/tmp/nope", "has space", ""])
def test_profile_validation_fails_closed_for_invalid_nonempty_values(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    monkeypatch.setenv("SABEROPS_PROFILE", bad)
    if not bad:
        assert active_profile() == ""
    else:
        with pytest.raises(ValueError):
            active_profile()
