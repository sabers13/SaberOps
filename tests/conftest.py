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
    config.addinivalue_line(
        "markers",
        "no_attendance: do not hold a test attended terminal (presence tests)",
    )


@pytest.fixture(autouse=True)
def _attended_terminal(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Run every test as if the owner had a terminal open (F24).

    Presence is required from the start: a store with no planning session and
    no attended terminal starts nothing.  Ordinary engine tests are not about
    absence, so they get one attended-terminal record for the test's lifetime.
    Presence tests opt out with ``@pytest.mark.no_attendance`` and drive the
    gate themselves.  The record lives under the test's own ``tmp_path`` and
    never touches the owner's real state directory.
    """
    if request.node.get_closest_marker("no_attendance") is not None:
        yield
        return
    from saberops.attendance import record_attendance

    # Keep the record beside the test's temp folder, never inside it: some tests
    # assert the exact contents of their own directory.
    folder = tmp_path.parent / f"{tmp_path.name}-attendance"
    monkeypatch.setenv("SABEROPS_ATTENDANCE_DIR", str(folder))
    monkeypatch.setenv("SABEROPS_PLANNING_HEARTBEAT_TIMEOUT", "21600")
    record_attendance("watch", label="pytest attended terminal", directory=folder)
    yield


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
