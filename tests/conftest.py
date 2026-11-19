"""Shared test setup.

Since 20 October 2026 the packaged default routing is empty: a fresh install
routes nothing until the owner chooses models on the Routing page. Engine
tests that never write a user routing file were built on the v0.4.0 packaged
chains, so they keep reading that exact document from
``tests/fixtures/routing_v040_default.json``. Tests about the real packaged
file opt out with ``@pytest.mark.packaged_routing_default``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from saberops import routing_config

_V040_DEFAULT = Path(__file__).resolve().parent / "fixtures" / "routing_v040_default.json"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "packaged_routing_default: read the real (empty) packaged routing default"
    )


@pytest.fixture(autouse=True)
def _v040_routing_default(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    if request.node.get_closest_marker("packaged_routing_default") is None:
        monkeypatch.setattr(routing_config, "_packaged_default_path", lambda: _V040_DEFAULT)
    monkeypatch.setenv("SABEROPS_DISABLE_AUTO_SCHEDULER", "1")
    # F18: never probe a real vendor CLI or start a detached check process from
    # an ordinary test.  tests/test_f18_vendor_self_healing.py drives detection
    # and the checks directly with fake versions.
    monkeypatch.setenv("SABEROPS_VENDOR_CHECKS", "0")
    yield


@pytest.fixture(autouse=True)
def _disable_desktop_notifications(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never send a real desktop notice from an ordinary test.

    Tests that need delivery point ``PATH`` at a stub ``notify-send`` and opt
    back in with ``SABEROPS_DESKTOP_NOTIFICATIONS=1``.
    """
    monkeypatch.setenv("SABEROPS_DESKTOP_NOTIFICATIONS", "0")
