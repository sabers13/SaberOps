"""Unit tests for the OpenCode config/home isolation helper (HV-2 fixup 1).

Every case runs offline against stub filesystems: the vendor CLI is never
executed here.  Isolation *behaviour* (the real binary honouring these keys)
is covered by the owner-authorized bounded probe recorded in
``LANE/probe-isolation/``, never by these tests.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from saberops.workers.opencode_isolation import (
    OPENCODE_AMBIENT_CONFIG_SELECTORS,
    OPENCODE_DISCOVERY_DISABLE_ENV,
    OpenCodeIsolationUnavailable,
    opencode_isolation,
    opencode_isolation_policies,
    opencode_system_config_dir,
)


def _base(home: Path) -> dict[str, str]:
    return {
        "PATH": "/usr/bin",
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "OPENCODE_CONFIG_CONTENT": '{"enabled_providers": ["evil"]}',
        "OPENCODE_CONFIG": "/tmp/hostile.json",
        "OPENCODE_CONFIG_DIR": "/tmp/hostile-dir",
        "OPENCODE_MODELS_PATH": "/tmp/hostile-models",
        "OPENCODE_DISABLE_PROJECT_CONFIG": "0",
        "OPENCODE_CONFIG_PROJECT_DISABLE": "0",
        "OPENCODE_CLI_CONFIG_CONTENT": "{}",
    }


def test_isolation_redirects_config_and_home_and_pins_data_home(
    tmp_path: Path,
) -> None:
    """Covers: HV-2 isolation env (owned root, pinned data home, discovery off)."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".local" / "share").mkdir(parents=True)
    isolation = opencode_isolation(_base(home))
    try:
        root = isolation.root
        assert root.is_dir()
        env = isolation.env_for(
            _base(home), config=isolation.config({"enabled_providers": ["x"]})
        )
        assert env["HOME"] == str(root / "home")
        assert env["XDG_CONFIG_HOME"] == str(root / "config")
        assert env["XDG_DATA_HOME"] == str(home / ".local" / "share")
        assert env[OPENCODE_DISCOVERY_DISABLE_ENV] == "1"
        assert (root / "home").is_dir() and (root / "config").is_dir()
        assert (root / "cache").is_dir() and (root / "state").is_dir()
        # Pinned last: a hostile ambient inline document never wins.
        pinned = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        assert pinned["enabled_providers"] == ["x"]
        # Ambient selectors are removed even when the overlay carried them.
        for selector in OPENCODE_AMBIENT_CONFIG_SELECTORS:
            assert selector not in env or selector in (
                "OPENCODE_CONFIG_CONTENT",
                OPENCODE_DISCOVERY_DISABLE_ENV,
            )
        policies = pinned["policies"]
        resources = {
            (entry["action"], entry["resource"], entry["effect"]) for entry in policies
        }
        assert ("integration.use", "plugin:*", "deny") in resources
        assert ("integration.use", "mcp:*", "deny") in resources
        assert ("integration.use", "skill:*", "deny") in resources
    finally:
        isolation.close()


def test_isolation_derives_data_home_when_unset(tmp_path: Path) -> None:
    """Covers: HV-2 auth/session data path (fallback before HOME redirect)."""
    home = tmp_path / "home"
    home.mkdir()
    base = _base(home)
    base.pop("XDG_DATA_HOME", None)
    isolation = opencode_isolation(base)
    try:
        assert isolation.overrides["XDG_DATA_HOME"] == str(
            home / ".local" / "share"
        )
    finally:
        isolation.close()


def test_isolation_refuses_when_data_home_unresolvable(tmp_path: Path) -> None:
    """Covers: HV-2 refusal before launch (no account/session path)."""
    with pytest.raises(OpenCodeIsolationUnavailable):
        opencode_isolation({"PATH": "/usr/bin"})


def test_isolation_refuses_present_system_config_dir(tmp_path: Path) -> None:
    """Covers: HV-2 refusal before launch (system config cannot be excluded)."""
    system = tmp_path / "etc-opencode"
    system.mkdir()
    with pytest.raises(OpenCodeIsolationUnavailable, match="system configuration"):
        opencode_isolation(_base(tmp_path), system_config_dir=system)


def test_isolation_root_is_unique_and_cleaned_up(tmp_path: Path) -> None:
    """Covers: HV-2 root lifecycle (unique per launch, removed on close)."""
    home = tmp_path / "home"
    home.mkdir()
    first = opencode_isolation(_base(home))
    second = opencode_isolation(_base(home))
    assert first.root != second.root
    first.close()
    assert not first.root.exists()
    assert second.root.is_dir()
    second.close()
    assert not second.root.exists()
    # Double close is safe and closing only ever removes the owned root.
    second.close()
    assert home.is_dir()


def test_isolation_context_manager_cleans_up_on_exception(tmp_path: Path) -> None:
    """Covers: HV-2 cleanup on error (root removed even when launch fails)."""
    home = tmp_path / "home"
    home.mkdir()
    with pytest.raises(RuntimeError, match="boom"):
        with opencode_isolation(_base(home)) as isolation:
            root = isolation.root
            raise RuntimeError("boom")
    assert not root.exists()


def test_opencode_system_config_dir_resolution() -> None:
    """Covers: HV-2 system config discovery paths per platform."""
    assert opencode_system_config_dir("linux") == Path("/etc/opencode")
    assert opencode_system_config_dir("darwin") == Path(
        "/Library/Application Support/opencode"
    )
    assert opencode_system_config_dir(
        "win32", {"ProgramData": "D:\\Data"}
    ) == Path("D:\\Data") / "opencode"


def test_isolation_policies_deny_integrations() -> None:
    """Covers: HV-2 inline deny policies (plugin/MCP/skill)."""
    assert opencode_isolation_policies() == [
        {"action": "integration.use", "resource": "plugin:*", "effect": "deny"},
        {"action": "integration.use", "resource": "mcp:*", "effect": "deny"},
        {"action": "integration.use", "resource": "skill:*", "effect": "deny"},
    ]


def test_poisoned_parent_environment_never_reaches_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: HV-2 poisoned ambient env (selectors stripped, pins win)."""
    home = tmp_path / "home"
    home.mkdir()
    real = str(tmp_path / "real-data")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "real-config"))
    monkeypatch.setenv("XDG_DATA_HOME", real)
    monkeypatch.setenv("OPENCODE_CONFIG", "/tmp/hostile.json")
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", "/tmp/hostile-dir")
    monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", '{"enabled_providers": ["evil"]}')
    monkeypatch.setenv("OPENCODE_CLI_CONFIG_CONTENT", '{"x": 1}')
    monkeypatch.setenv("OPENCODE_MODELS_URL", "https://hostile.example/models")
    monkeypatch.setenv("OPENCODE_DISABLE_PROJECT_CONFIG", "0")
    from saberops.workers.env import hermetic_env

    base = hermetic_env("opencode", {}, None)
    # hermetic_env drops every OPENCODE_* selector except the owned pins.
    assert "OPENCODE_CONFIG" not in base
    assert "OPENCODE_CONFIG_DIR" not in base
    assert "OPENCODE_CONFIG_CONTENT" not in base
    assert "OPENCODE_CLI_CONFIG_CONTENT" not in base
    assert "OPENCODE_MODELS_URL" not in base
    isolation = opencode_isolation(base)
    try:
        env = isolation.env_for(base, config=isolation.config({}))
        assert env["XDG_DATA_HOME"] == real
        assert real in env["XDG_DATA_HOME"] and "real-config" not in str(
            env["XDG_CONFIG_HOME"]
        )
        assert env["HOME"] != str(home)
        assert env[OPENCODE_DISCOVERY_DISABLE_ENV] == "1"
        assert json.loads(env["OPENCODE_CONFIG_CONTENT"])["policies"]
    finally:
        isolation.close()


def test_child_process_sees_only_the_isolated_environment(
    tmp_path: Path,
) -> None:
    """Covers: HV-2 resolved child env (recorded by a stub child process)."""
    import subprocess
    import sys

    home = tmp_path / "home"
    home.mkdir()
    probe = tmp_path / "probe"
    probe.mkdir()
    probe_script = tmp_path / "record.py"
    probe_script.write_text(
        "import json, os\n"
        "record = dict(os.environ)\n"
        f"open({str(probe / 'env.json')!r}, 'w').write(json.dumps(record, sort_keys=True))\n"
    )
    isolation = opencode_isolation(_base(home))
    try:
        env = isolation.env_for(
            _base(home), config=isolation.config({"enabled_providers": ["x"]})
        )
        subprocess.run(
            [sys.executable, str(probe_script)],
            env=env,
            capture_output=True,
            check=True,
            timeout=30,
        )
        child = json.loads((probe / "env.json").read_text())
        assert child["HOME"] == str(isolation.root / "home")
        assert child["XDG_CONFIG_HOME"] == str(isolation.root / "config")
        assert child[OPENCODE_DISCOVERY_DISABLE_ENV] == "1"
        assert "OPENCODE_CONFIG" not in child
        assert Path(child["XDG_CONFIG_HOME"]).is_dir()
    finally:
        isolation.close()


def test_no_credential_values_enter_the_owned_root(tmp_path: Path) -> None:
    """Covers: HV-2 credential hygiene (roots carry paths, never secrets)."""
    home = tmp_path / "home"
    home.mkdir()
    isolation = opencode_isolation(_base(home))
    try:
        material = " ".join(
            path.read_text(errors="replace")
            for path in isolation.root.rglob("*")
            if path.is_file()
        )
        for path in isolation.root.rglob("*"):
            assert "sk-" not in str(path) and "token" not in str(path).lower()
        assert material == ""
    finally:
        isolation.close()
