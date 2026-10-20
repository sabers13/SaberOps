"""`orch connect`, the Setup agent step and the owner's MCP switch.

Hermetic: a fake command runner and a temporary home directory; no real agent
CLI is ever started and no real config file is touched.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from saberops import agent_connect
from saberops.agent_connect import connect, disconnect, is_connected, server_snippet
from saberops.mcp.switch import mcp_enabled
from saberops.web import _csrf_token, create_app


class FakeRunner:
    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        self.calls: list[tuple[list[str], Path | None]] = []
        self.returncode = returncode
        self.stdout = stdout

    def __call__(self, argv: Sequence[str], cwd: Path | None) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), cwd))
        return subprocess.CompletedProcess(list(argv), self.returncode, self.stdout, "")


@pytest.fixture()
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in ("state", "config", "cache", "home"):
        (tmp_path / name).mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("SABEROPS_MCP_LEASE", str(tmp_path / "state" / "lease.json"))
    monkeypatch.setenv("SABEROPS_PROFILE", "")
    monkeypatch.setattr(agent_connect, "orch_command", lambda: "/opt/orch")
    return tmp_path / "home"


def test_claude_uses_its_own_cli_and_replaces_a_stale_entry(isolated: Path) -> None:
    runner = FakeRunner()
    result = connect("claude", runner=runner)
    assert result.ok
    assert runner.calls[0][0] == ["claude", "mcp", "remove", "--scope", "user", "saberops"]
    assert runner.calls[1][0] == [
        "claude",
        "mcp",
        "add",
        "--scope",
        "user",
        "saberops",
        "--",
        "/opt/orch",
        "mcp",
        "serve",
    ]


def test_profile_is_passed_through_and_project_scope_runs_in_the_folder(
    isolated: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SABEROPS_PROFILE", "work")
    runner = FakeRunner()
    connect("claude", project=tmp_path, runner=runner)
    argv, cwd = runner.calls[-1]
    assert cwd == tmp_path and "--scope" in argv and "project" in argv
    assert ["-e", "SABEROPS_PROFILE=work"] == argv[argv.index("-e") : argv.index("-e") + 2]
    codex = FakeRunner()
    connect("codex", runner=codex)
    assert "--env" in codex.calls[-1][0] and "SABEROPS_PROFILE=work" in codex.calls[-1][0]
    assert not connect("codex", project=tmp_path, runner=codex).ok  # no per-project setting


def test_dry_run_changes_nothing(isolated: Path) -> None:
    runner = FakeRunner()
    result = connect("codex", dry_run=True, runner=runner)
    assert result.ok and runner.calls == [] and "codex mcp add saberops" in result.commands[0]


def test_a_refusing_cli_returns_the_snippet_to_paste(isolated: Path) -> None:
    result = connect("antigravity", runner=FakeRunner(returncode=1))
    assert not result.ok and "mcpServers" in result.snippet


def test_json_agent_is_edited_with_a_backup_and_removed_cleanly(isolated: Path) -> None:
    config = isolated / ".config" / "opencode" / "opencode.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"mcp": {"other": {"type": "local"}}, "theme": "x"}))
    result = connect("opencode", home=isolated)
    assert result.ok
    data = json.loads(config.read_text())
    assert data["theme"] == "x" and "other" in data["mcp"]
    assert data["mcp"]["saberops"]["command"] == ["/opt/orch", "mcp", "serve"]
    assert list(config.parent.glob("opencode.json.bak-saberops-*"))
    opencode = agent_connect.agent_by_slug("opencode")
    assert opencode is not None and is_connected(opencode, home=isolated)
    assert disconnect("opencode", home=isolated).ok
    assert "saberops" not in json.loads(config.read_text())["mcp"]


def test_a_config_with_comments_is_never_rewritten(isolated: Path) -> None:
    config = isolated / ".cursor" / "mcp.json"
    config.parent.mkdir(parents=True)
    original = '{\n  // my servers\n  "mcpServers": {}\n}\n'
    config.write_text(original)
    result = connect("cursor", home=isolated)
    assert not result.ok and "left untouched" in result.message
    assert config.read_text() == original
    assert json.loads(result.snippet)["mcpServers"]["saberops"]["args"] == ["mcp", "serve"]


def test_connected_state_reads_each_agents_own_registry(isolated: Path) -> None:
    (isolated / ".claude.json").write_text(json.dumps({"mcpServers": {"saberops": {}}}))
    (isolated / ".codex").mkdir()
    (isolated / ".codex" / "config.toml").write_text('[mcp_servers.saberops]\ncommand = "x"\n')

    def by(slug: str) -> agent_connect.Agent:
        agent = agent_connect.agent_by_slug(slug)
        assert agent is not None
        return agent

    assert is_connected(by("claude"), home=isolated)
    assert is_connected(by("codex"), home=isolated)
    assert is_connected(by("antigravity"), runner=FakeRunner(stdout="saberops  stdio  /opt/orch"))
    assert not is_connected(
        by("antigravity"), runner=FakeRunner(stdout="No MCP servers configured.")
    )
    assert "[mcp_servers.saberops]" in server_snippet(by("codex"))


def test_setup_lists_agents_and_flips_the_master_switch(isolated: Path, tmp_path: Path) -> None:
    client = TestClient(create_app(db_path=tmp_path / "web.db"))
    page = client.get("/setup").text
    for marker in ("Connect your agent", "turn on saberops", 'action="/setup/mcp-switch"'):
        assert marker in page, marker
    assert client.post("/setup/mcp-switch", data={"enabled": "off"}).status_code == 403
    done = client.post(
        "/setup/mcp-switch", data={"csrf": _csrf_token(), "enabled": "off"}, follow_redirects=False
    )
    assert done.status_code == 303 and not mcp_enabled()
    assert "SaberOps is off for every agent" in client.get("/setup?mcp=off").text
    client.post("/setup/mcp-switch", data={"csrf": _csrf_token(), "enabled": "on"})
    assert mcp_enabled()


def test_setup_connect_route_uses_the_connector(
    isolated: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    def fake_connect(slug: str, **_: object) -> agent_connect.ConnectResult:
        seen.append(slug)
        return agent_connect.ConnectResult(slug, True, "Connected.")

    monkeypatch.setattr(agent_connect, "connect", fake_connect)
    client = TestClient(create_app(db_path=tmp_path / "web.db"))
    assert client.post("/setup/agents", data={"agent": "claude"}).status_code == 403
    assert (
        client.post("/setup/agents", data={"csrf": _csrf_token(), "agent": "vim"}).status_code
        == 400
    )
    done = client.post(
        "/setup/agents", data={"csrf": _csrf_token(), "agent": "claude"}, follow_redirects=False
    )
    assert done.status_code == 303 and seen == ["claude"]
    assert "say" in client.get(done.headers["location"]).text


def test_cli_switch(tmp_path: Path) -> None:
    from saberops.cli import main

    lease = str(tmp_path / "lease.json")
    assert main(["mcp", "off", "--lease", lease]) == 0
    assert not mcp_enabled(Path(lease))
    assert main(["mcp", "on", "--lease", lease]) == 0
    assert mcp_enabled(Path(lease))
