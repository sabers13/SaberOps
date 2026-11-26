"""Shared test setup.

Since 20 October 2026 the packaged default routing is empty: a fresh install
routes nothing until the owner chooses models with `orch routing`. Engine
tests that never write a user routing file were built on the v0.4.0 packaged
chains, so they keep reading that exact document from
``tests/fixtures/routing_v040_default.json``. Tests about the real packaged
file opt out with ``@pytest.mark.packaged_routing_default``.

H29 (ADR-0092) also keeps here the two owner-store isolation fixtures the suite
had copied thirteen times, and turns the gate's *host* memory guard off for
every test unless the test asks for it with ``@pytest.mark.memory_guard``.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from saberops import routing_config

_V040_DEFAULT = Path(__file__).resolve().parent / "fixtures" / "routing_v040_default.json"

#: The XDG bases every test must keep off a real home, and their subdirectory.
_XDG_BASES = (
    ("XDG_CONFIG_HOME", "config"),
    ("XDG_STATE_HOME", "state"),
    ("XDG_CACHE_HOME", "cache"),
    ("XDG_DATA_HOME", "data"),
)

#: Base values captured before pytest started any fixture.  A test-run value
#: equal to one of these is *inherited* (GitHub's runner exports
#: ``XDG_CONFIG_HOME=/home/runner/.config``) and must be replaced; any other
#: value was set deliberately by a test's own module or session fixture (a
#: seeded routing/control-plane tree) and must win.
_INHERITED_XDG = {variable: os.environ.get(variable) for variable, _ in _XDG_BASES}


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "packaged_routing_default: read the real (empty) packaged routing default"
    )
    config.addinivalue_line(
        "markers",
        "no_attendance: do not hold a test attended terminal (presence tests)",
    )
    config.addinivalue_line(
        "markers",
        "memory_guard: hold the host gate memory guard on (guard contract tests)",
    )


@pytest.fixture(autouse=True)
def _isolated_owner_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """No test may resolve the owner's real XDG store.

    ADR-0082's owner-mode reader migrates a pre-1.1 document on the first read,
    so a test that reached ``~/.config/saberops`` would *write* the owner's real
    store.  That happened once (H22 postmortem, 2026-11-23): this host leaves
    ``XDG_CONFIG_HOME`` unset, so the loader fell back to the real home.  H40
    (ADR-0096) closes the other half: GitHub's Ubuntu runner *exports*
    ``XDG_CONFIG_HOME=/home/runner/.config``, so filling only unset bases let
    every test resolve the runner's real home.  Replace a base that is unset or
    still carries the pre-pytest value; a value a test's own module or session
    fixture set (a seeded routing/control-plane tree) wins as before.  Then
    prove the owner config path resolves inside the pytest tree.
    """
    scratch = tmp_path.parent / f"{tmp_path.name}-owner-xdg"
    for variable, name in _XDG_BASES:
        if os.environ.get(variable) not in (None, _INHERITED_XDG[variable]):
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


@pytest.fixture()
def fake_vendor_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[str], Path]:
    """Put a stub vendor CLI at the front of ``PATH`` for this test.

    A few tests drive the CLI's *installed* branch (``orch connect claude``,
    ``orch doctor``).  They must supply that fact themselves: otherwise the
    result depends on whether the machine running the suite happens to have
    Claude Code, Codex, OpenCode or Antigravity installed.  The stub exits 0
    without doing anything, so no real vendor CLI or network is touched.
    """
    bin_dir = tmp_path / "vendor-bin"
    bin_dir.mkdir()
    monkeypatch.setenv(
        "PATH", os.pathsep.join((str(bin_dir), os.environ.get("PATH", "")))
    )

    def install(name: str) -> Path:
        path = bin_dir / name
        path.write_text(f"#!{sys.executable}\nraise SystemExit(0)\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    return install


@pytest.fixture(autouse=True)
def _disable_desktop_notifications(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never send a real desktop notice from an ordinary test.

    Tests that need delivery point ``PATH`` at a stub ``notify-send`` and opt
    back in with ``SABEROPS_DESKTOP_NOTIFICATIONS=1``.
    """
    monkeypatch.setenv("SABEROPS_DESKTOP_NOTIFICATIONS", "0")


@pytest.fixture(autouse=True)
def _isolated_memory_ledger(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Keep every test off the real per-user shared gate memory ledger.

    A gate that freezes step cgroups records them in a host-wide ledger.  A
    test that launches its own scope (the memory-budget tests do) must never
    register there, or a concurrent lane's gate can freeze the test's launch
    and leave it stuck.  The ledger lives beside ``tmp_path`` so it is disposed
    with the test and shared only by that test's own subprocesses.

    Admission is still a *host* rule: it reads the real ``MemAvailable`` and
    waits for a budget sibling lanes' gates already hold, so an ordinary test
    that admits against the host turns another lane's load into a
    pytest-timeout victim (``data/so-testprof/report.md`` §4.3b: 22 tests hit
    the 180 s cap inside ``MemoryLedger.acquire``).  Ordinary tests therefore
    admit without the guard; its own contract tests carry
    ``@pytest.mark.memory_guard`` and get it back.  The predicted peak, the
    grant, the learned sizes and the worker count are unchanged --- only the
    wait/refusal that reads live host memory is skipped.
    """
    monkeypatch.setenv(
        "SABEROPS_GATE_MEMORY_GUARD",
        "1" if request.node.get_closest_marker("memory_guard") is not None else "0",
    )
    monkeypatch.setenv(
        "SABEROPS_MEMORY_LEDGER", str(tmp_path.parent / f"{tmp_path.name}-memory-ledger")
    )
    # Real host PSI must not freeze a test's own scoped launch: tests that care
    # about pressure inject ``pressure_fn`` instead.
    monkeypatch.setenv(
        "SABEROPS_PROC_PRESSURE_MEMORY", str(tmp_path.parent / f"{tmp_path.name}-no-psi")
    )
    yield


@pytest.fixture()
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """One owner store, cache and binding/access/readiness store per test.

    H29 merged the eight byte-identical copies of this fixture (accept, CLI
    parity, control-plane bindings, H4 review, H7 resume, readiness, review,
    service).  A file that needs a different shape keeps its own local
    ``isolated``, which wins over this one.
    """
    state = tmp_path / "state"
    config = tmp_path / "config"
    cache = tmp_path / "cache"
    for directory in (state, config, cache):
        directory.mkdir(parents=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    monkeypatch.delenv("SABEROPS_PROFILE", raising=False)
    monkeypatch.setenv("ORCH_BINDING_STORE", str(tmp_path / "bindings.json"))
    monkeypatch.setenv("ORCH_ACCESS_STORE", str(tmp_path / "access.json"))
    monkeypatch.setenv("ORCH_ACCESS_MODELS_STORE", str(tmp_path / "models.json"))
    monkeypatch.setenv("ORCH_READINESS_STORE", str(tmp_path / "readiness.json"))
    return tmp_path


@pytest.fixture()
def isolated_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One owner state/config tree per test, with no inherited binding stores.

    H29 merged the five near-identical copies (accept, control-plane bindings,
    web, web identity, web shell).  The shared body deletes all four ``ORCH_*``
    store overrides; a test that sets one afterwards still wins.
    """
    state = tmp_path / "state"
    config = tmp_path / "config"
    state.mkdir(parents=True)
    config.mkdir(parents=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    for variable in (
        "ORCH_ACCESS_STORE",
        "ORCH_ACCESS_MODELS_STORE",
        "ORCH_BINDING_STORE",
        "ORCH_READINESS_STORE",
    ):
        monkeypatch.delenv(variable, raising=False)
