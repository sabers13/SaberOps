"""Shared test setup.

Since 20 October 2026 the packaged default routing is empty: a fresh install
routes nothing until the owner chooses models on the Routing page. Engine
tests that never write a user routing file were built on the v0.4.0 packaged
chains, so they keep reading that exact document from
``tests/fixtures/routing_v040_default.json``. Tests about the real packaged
file opt out with ``@pytest.mark.packaged_routing_default``.
"""

from __future__ import annotations

import os
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
def _isolated_owner_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """No test may resolve the owner's real XDG store.

    ADR-0082's owner-mode reader migrates a pre-1.1 document on the first read,
    so a test that reached ``~/.config/saberops`` would *write* the owner's real
    store.  That happened once (H22 postmortem, 2026-11-23): this host leaves
    ``XDG_CONFIG_HOME`` unset, so the loader fell back to the real home.  Fill in
    only the XDG bases a test has not set itself, then prove the owner config
    path resolves inside this test's scratch tree; a test that would reach the
    real home fails instead of writing it.
    """
    scratch = tmp_path.parent / f"{tmp_path.name}-owner-xdg"
    for variable, name in (
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_STATE_HOME", "state"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_DATA_HOME", "data"),
    ):
        if os.environ.get(variable):
            continue
        directory = scratch / name
        directory.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(variable, str(directory))
    from saberops.paths import saberops_config_dir

    resolved = saberops_config_dir().resolve()
    assert tmp_path.parent.resolve() in resolved.parents, (
        "test resolved the owner's real config at "
        f"{resolved}; point XDG_CONFIG_HOME at a test-scoped directory"
    )
    yield


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


@pytest.fixture(autouse=True)
def _isolated_memory_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Keep every test off the real per-user shared gate memory ledger.

    A gate that freezes step cgroups records them in a host-wide ledger.  A
    test that launches its own scope (the memory-budget tests do) must never
    register there, or a concurrent lane's gate can freeze the test's launch
    and leave it stuck.  The ledger lives beside ``tmp_path`` so it is disposed
    with the test and shared only by that test's own subprocesses.
    """
    monkeypatch.setenv(
        "SABEROPS_MEMORY_LEDGER", str(tmp_path.parent / f"{tmp_path.name}-memory-ledger")
    )
    # Real host PSI must not freeze a test's own scoped launch: tests that care
    # about pressure inject ``pressure_fn`` instead.
    monkeypatch.setenv(
        "SABEROPS_PROC_PRESSURE_MEMORY", str(tmp_path.parent / f"{tmp_path.name}-no-psi")
    )
    yield
