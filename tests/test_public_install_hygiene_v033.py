"""v0.3.3 public-install hygiene regression tests (F1 + F2).

Hermetic: every test isolates XDG base directories into tmp state so the
owner's actual home state is never touched.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import sqlite3
from pathlib import Path

import pytest

from saberops import paths as saberops_paths
from saberops.authority import get_authority_config_path
from saberops.cli import (
    _check_core_imports,
    _check_dev_imports,
    _check_python_requires,
    _check_venv_tools,
    _is_source_checkout,
)
from saberops.control_plane.binding_store import get_binding_store_path
from saberops.control_plane.policy import get_control_plane_config_path
from saberops.db import (
    DB_SCHEMA_VERSION,
    Database,
    DatabaseSchemaError,
    get_db_schema_version,
    get_default_db_path,
)
from saberops.project import owner_state_dir, projects_state_root
from saberops.projects import get_default_projects_path
from saberops.routing_config import get_user_routing_path
from saberops.tooling.registry import default_registry_path


@pytest.fixture
def isolated_xdg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Path]:
    """Point all XDG bases at tmp dirs; return the bases."""
    bases = {
        "XDG_STATE_HOME": tmp_path / "state",
        "XDG_CONFIG_HOME": tmp_path / "config",
        "XDG_CACHE_HOME": tmp_path / "cache",
    }
    for key, path in bases.items():
        path.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(key, str(path))
    # Keep binding-store / registry / telemetry overrides out of the way.
    for key in (
        "ORCH_BINDING_STORE",
        "ORCH_TOOL_REGISTRY",
        "ORCH_TOOL_TELEMETRY_SINK",
        "ORCH_ACCESS_STORE",
        "ORCH_ACCESS_MODELS_STORE",
    ):
        monkeypatch.delenv(key, raising=False)
    return bases


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# F1 — doctor installed-package vs development-checkout semantics
# ---------------------------------------------------------------------------


def test_installed_package_skips_source_checkout_artifacts(tmp_path: Path) -> None:
    """A non-checkout root must not require pyproject/.venv/dev tooling."""
    plain = tmp_path / "site-packages-shim"
    plain.mkdir()
    assert _is_source_checkout(plain) is False
    venv_finding = _check_venv_tools(plain)
    assert venv_finding.severity == "OK"
    dev_finding = _check_dev_imports(plain)
    assert dev_finding.severity == "OK"


def test_source_checkout_detection(tmp_path: Path) -> None:
    """A real checkout layout is still recognised as a checkout."""
    root = tmp_path / "checkout"
    (root / "src" / "saberops").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    assert _is_source_checkout(root) is True


def test_missing_runtime_module_still_fails_doctor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing genuine runtime requirements must still fail doctor."""
    real_find_spec = importlib.util.find_spec

    def _fake_find_spec(name: str, package: str | None = None) -> object:
        if name == "fastapi":
            return None
        return real_find_spec(name, package)

    monkeypatch.setattr(importlib.util, "find_spec", _fake_find_spec)
    finding = _check_core_imports()
    assert finding.severity == "FAIL"
    assert "fastapi" in finding.message


def test_dev_only_tooling_never_blocks_installed_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absent dev tooling is not a runtime readiness blocker when installed."""
    plain = tmp_path / "installed"
    plain.mkdir()
    real_find_spec = importlib.util.find_spec

    def _fake_find_spec(name: str, package: str | None = None) -> object:
        if name in ("httpx", "pytest"):
            return None
        return real_find_spec(name, package)

    monkeypatch.setattr(importlib.util, "find_spec", _fake_find_spec)
    # Runtime check ignores dev-only modules.
    assert _check_core_imports().severity == "OK"
    # Dev import diagnostic never FAILs.
    assert _check_dev_imports(plain).severity == "OK"
    # Venv-tool diagnostic never FAILs outside a checkout.
    assert _check_venv_tools(plain).severity == "OK"


def test_python_check_uses_installed_metadata_when_present() -> None:
    """Installed metadata drives the python-requires verdict when available."""
    finding = _check_python_requires(Path("/nonexistent"))
    # Python 3.12+ satisfies the published >=3.12 constraint either via
    # installed metadata or via WARN fallback; it must never FAIL here.
    assert finding.severity in ("OK", "WARN")


def test_python_check_fails_on_unsatisfied_constraint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A genuinely unsatisfied requires-python still fails doctor."""
    import saberops.cli as cli

    monkeypatch.setattr(
        cli, "_installed_package_metadata", lambda: ("0.3.3", ">=99.0")
    )
    finding = _check_python_requires(tmp_path)
    assert finding.severity == "FAIL"


# ---------------------------------------------------------------------------
# F2 — canonical SaberOps namespace
# ---------------------------------------------------------------------------


def test_default_state_path_uses_saberops_namespace(
    isolated_xdg: dict[str, Path],
) -> None:
    db_path = get_default_db_path()
    assert db_path == isolated_xdg["XDG_STATE_HOME"] / "saberops" / "orchestrator.db"
    assert "orchestrator-mvp" not in str(db_path)
    assert "orchestrator-v2" not in str(db_path)
    assert owner_state_dir() == isolated_xdg["XDG_STATE_HOME"] / "saberops"
    assert projects_state_root() == isolated_xdg["XDG_STATE_HOME"] / "saberops" / "projects"


def test_default_config_paths_use_saberops_namespace(
    isolated_xdg: dict[str, Path],
) -> None:
    expected_dir = isolated_xdg["XDG_CONFIG_HOME"] / "saberops"
    assert get_user_routing_path() == expected_dir / "routing.json"
    assert get_authority_config_path() == expected_dir / "authority.json"
    assert get_control_plane_config_path() == expected_dir / "control_plane.json"
    assert get_binding_store_path() == expected_dir / "bindings.json"
    assert default_registry_path() == expected_dir / "tools.json"
    for path in (
        get_user_routing_path(),
        get_authority_config_path(),
        get_control_plane_config_path(),
        get_binding_store_path(),
        default_registry_path(),
    ):
        assert "orchestrator-mvp" not in str(path)
        assert "orchestrator-v2" not in str(path)


def test_binding_routing_authority_share_one_config_namespace(
    isolated_xdg: dict[str, Path],
) -> None:
    """All owner config resolves consistently under the one namespace."""
    parents = {
        get_user_routing_path().parent,
        get_authority_config_path().parent,
        get_control_plane_config_path().parent,
        get_binding_store_path().parent,
        default_registry_path().parent,
    }
    assert parents == {isolated_xdg["XDG_CONFIG_HOME"] / "saberops"}
    assert get_default_projects_path().parent == isolated_xdg["XDG_STATE_HOME"] / "saberops"


def test_xdg_overrides_remain_authoritative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit XDG_* bases change the base; the saberops segment stays fixed."""
    state_base = tmp_path / "custom-state"
    config_base = tmp_path / "custom-config"
    cache_base = tmp_path / "custom-cache"
    for path in (state_base, config_base, cache_base):
        path.mkdir(parents=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state_base))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_base))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_base))
    assert get_default_db_path() == state_base / "saberops" / "orchestrator.db"
    assert get_user_routing_path() == config_base / "saberops" / "routing.json"
    assert saberops_paths.saberops_cache_dir() == cache_base / "saberops"


def test_cache_namespace_and_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cache namespace follows XDG_CACHE_HOME with the platform fallback."""
    cache_base = tmp_path / "c"
    cache_base.mkdir()
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_base))
    assert saberops_paths.saberops_cache_dir() == cache_base / "saberops"
    monkeypatch.delenv("XDG_CACHE_HOME")
    # Unset + HOME fallback: only assert the tail, never the real home content.
    monkeypatch.setenv("HOME", str(tmp_path))
    assert saberops_paths.cache_home() == tmp_path / ".cache"
    assert saberops_paths.saberops_cache_dir() == tmp_path / ".cache" / "saberops"


def test_legacy_state_not_selected_for_fresh_install(
    isolated_xdg: dict[str, Path],
) -> None:
    """Legacy dirs exist but defaults never point at them."""
    legacy_state = isolated_xdg["XDG_STATE_HOME"] / "orchestrator-mvp"
    legacy_config = isolated_xdg["XDG_CONFIG_HOME"] / "orchestrator-v2"
    legacy_state.mkdir(parents=True, exist_ok=True)
    legacy_config.mkdir(parents=True, exist_ok=True)
    assert "orchestrator-mvp" not in str(get_default_db_path())
    assert "orchestrator-v2" not in str(get_user_routing_path())
    assert saberops_paths.is_legacy_path(legacy_state / "orchestrator.db") is True
    assert saberops_paths.is_legacy_path(get_default_db_path()) is False


def test_existing_legacy_db_not_modified_by_starting_saberops(
    isolated_xdg: dict[str, Path],
) -> None:
    """Simply starting SaberOps (default paths) leaves legacy state untouched."""
    legacy_db = isolated_xdg["XDG_STATE_HOME"] / "orchestrator-mvp" / "orchestrator.db"
    legacy_db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(legacy_db))
    try:
        conn.execute("CREATE TABLE runs (id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO runs (id) VALUES ('run_legacy')")
        conn.commit()
    finally:
        conn.close()
    # user_version stays 0: genuine legacy marker.
    assert get_db_schema_version(legacy_db) == 0
    before_hash = _file_hash(legacy_db)
    before_mtime = legacy_db.stat().st_mtime_ns

    # Starting SaberOps at defaults creates/use the NEW namespace only.
    fresh_db = Database(get_default_db_path())
    assert fresh_db.db_path != legacy_db.resolve()
    assert get_db_schema_version(fresh_db.db_path) == DB_SCHEMA_VERSION
    fresh_db2 = Database()
    assert fresh_db2.db_path == get_default_db_path().resolve()

    assert _file_hash(legacy_db) == before_hash
    assert legacy_db.stat().st_mtime_ns == before_mtime
    assert get_db_schema_version(legacy_db) == 0


def test_legacy_config_not_modified_by_starting_saberops(
    isolated_xdg: dict[str, Path],
) -> None:
    """Default config resolution never reads or writes legacy documents."""
    legacy_routing = isolated_xdg["XDG_CONFIG_HOME"] / "orchestrator-v2" / "routing.json"
    legacy_routing.parent.mkdir(parents=True, exist_ok=True)
    legacy_routing.write_text('{"legacy": true}', encoding="utf-8")
    before_hash = _file_hash(legacy_routing)
    before_mtime = legacy_routing.stat().st_mtime_ns

    assert get_user_routing_path() != legacy_routing
    assert not get_user_routing_path().exists()
    assert get_authority_config_path() != (
        isolated_xdg["XDG_CONFIG_HOME"] / "orchestrator-v2" / "authority.json"
    )

    assert _file_hash(legacy_routing) == before_hash
    assert legacy_routing.stat().st_mtime_ns == before_mtime


def test_database_schema_identity_deterministic(tmp_path: Path) -> None:
    """Fresh databases are stamped deterministically with the schema version."""
    db_path = tmp_path / "fresh.db"
    Database(db_path)
    assert get_db_schema_version(db_path) == DB_SCHEMA_VERSION == 1
    # Reopening the stamped database is stable and unmodified.
    before_hash = _file_hash(db_path)
    Database(db_path)
    assert get_db_schema_version(db_path) == DB_SCHEMA_VERSION
    assert _file_hash(db_path) == before_hash


def test_database_legacy_schema_fails_closed(tmp_path: Path) -> None:
    """A legacy (user_version 0) database fails closed on writable open."""
    legacy = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(legacy))
    try:
        conn.execute("CREATE TABLE runs (id TEXT PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()
    assert get_db_schema_version(legacy) == 0
    before_hash = _file_hash(legacy)
    with pytest.raises(DatabaseSchemaError):
        Database(legacy)
    # Fail-closed means no mutation.
    assert _file_hash(legacy) == before_hash
    assert get_db_schema_version(legacy) == 0


def test_database_future_schema_fails_closed(tmp_path: Path) -> None:
    """A future/unknown schema version fails closed without mutation."""
    future = tmp_path / "future.db"
    Database(future)
    conn = sqlite3.connect(str(future))
    try:
        conn.execute(f"PRAGMA user_version = {DB_SCHEMA_VERSION + 1}")
        conn.commit()
    finally:
        conn.close()
    assert get_db_schema_version(future) == DB_SCHEMA_VERSION + 1
    before_hash = _file_hash(future)
    with pytest.raises(DatabaseSchemaError):
        Database(future)
    assert _file_hash(future) == before_hash


def test_doctor_database_check_fails_on_legacy_schema(tmp_path: Path) -> None:
    """Doctor truthfully reports legacy/unsupported database schemas."""
    from saberops.cli import _check_database

    legacy = tmp_path / "legacy-doctor.db"
    conn = sqlite3.connect(str(legacy))
    try:
        conn.execute("CREATE TABLE runs (id TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE quota_state (provider TEXT, pool TEXT)")
        conn.commit()
    finally:
        conn.close()
    finding, _ = _check_database(legacy)
    assert finding.severity == "FAIL"
    assert "user_version" in finding.message


def test_no_default_path_carries_legacy_segments(
    isolated_xdg: dict[str, Path],
) -> None:
    """One coherent namespace: no default carries orchestrator-mvp/v2."""
    assert os.environ.get("XDG_STATE_HOME") == str(isolated_xdg["XDG_STATE_HOME"])
    candidates = [
        get_default_db_path(),
        get_user_routing_path(),
        get_authority_config_path(),
        get_control_plane_config_path(),
        get_binding_store_path(),
        default_registry_path(),
        get_default_projects_path(),
        owner_state_dir(),
        projects_state_root(),
        saberops_paths.saberops_cache_dir(),
    ]
    for path in candidates:
        assert "orchestrator-mvp" not in str(path), str(path)
        assert "orchestrator-v2" not in str(path), str(path)
