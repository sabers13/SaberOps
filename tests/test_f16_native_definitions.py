"""F16: the generated ``saberops-worker`` native subagent definition.

`orch connect --skill` installs a host-vendor worker definition beside the
skill: Claude Code agent file, Codex agent TOML, OpenCode agent markdown.  Each
one carries the MCP server, the host's own vendor, worktree-confined permissions
where the CLI has them, and the claim/heartbeat/report loop.

Every test uses a temporary home directory: the owner's real ``~/.claude``,
``~/.codex`` and ``~/.config/opencode`` are never read or written.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Callable
from pathlib import Path

import pytest

from saberops import agent_connect
from saberops.agent_connect import (
    AGENT_MARKER_TOML,
    SERVER_NAME,
    WORKER_NAME,
    WORKER_TOOLS,
    WORKER_VENDOR,
    install_skill,
    install_worker,
    remove_skill,
    remove_worker,
    worker_document,
    worker_file_path,
)
from saberops.mcp.tools import TOOL_NAMES
from saberops.native_workers import NATIVE_TOOL_NAMES

HOSTS = {
    "claude": ".claude/agents",
    "codex": ".codex/agents",
    "opencode": ".config/opencode/agents",
}


@pytest.fixture()
def home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_vendor_cli: Callable[[str], Path],
) -> Path:
    base = tmp_path / "home"
    base.mkdir()
    monkeypatch.setenv("HOME", str(base))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(base / ".config"))
    monkeypatch.setattr(agent_connect, "_home", lambda _home=None: base)
    # `orch connect claude` runs the host CLI's own `mcp` command in the
    # installed branch.  Supply a stub instead of depending on this machine.
    fake_vendor_cli("claude")
    return base


def _path(home: Path, slug: str) -> Path:
    extension = "toml" if slug == "codex" else "md"
    return home / HOSTS[slug] / f"{WORKER_NAME}.{extension}"


@pytest.mark.parametrize("slug", sorted(HOSTS))
def test_worker_definition_per_cli_format(slug: str) -> None:
    document = worker_document(slug)
    vendor = WORKER_VENDOR[slug]
    # It names the host's own vendor and never another one as its model source.
    assert f'("{vendor}")' in document
    # The full claim/heartbeat/report loop, including the stop rule.
    assert "`native_claim`" in document
    assert "`native_heartbeat`" in document
    assert "`record_native_attempt`" in document
    assert "stop_requested" in document
    assert "Never accept, merge, push" in document
    # It references the session's server instead of starting a second one.
    assert "second SaberOps server" in " ".join(document.split())
    assert "http://" not in document and "https://" not in document
    # Only live tool names are taught.
    loop = document.split("## The loop", 1)[1]
    mentioned = set(re.findall(r"`([a-z][a-z0-9_]+)`", loop))
    parameters = {
        "worktree",
        "branch",
        "progress",
        "steering_notes",
        "stop_requested",
        "record_native_attempt",
        "native_claim",
        "native_heartbeat",
        "native_controls",
        "candidate_sha",
        "base_sha",
        "job_id",
        "run_id",
        "candidate",
        "error",
        "vendor",
        "model",
        "native",
        "workers",
        "ORCH_NATIVE_WORKERS",
    }
    known = set(TOOL_NAMES) | set(NATIVE_TOOL_NAMES) | parameters
    assert mentioned - known == set(), (slug, mentioned - known)
    assert set(WORKER_TOOLS) <= set(TOOL_NAMES) | set(NATIVE_TOOL_NAMES)


def test_claude_definition_confines_tools() -> None:
    document = worker_document("claude")
    assert document.startswith("---\nname: saberops-worker\n")
    assert "mcpServers:\n  - saberops" in document
    assert "model: inherit" in document
    assert "permissionMode: acceptEdits" in document
    tools = re.search(r"^tools: (.+)$", document, re.M)
    assert tools is not None
    for name in ("Read", "Edit", "Write", "Bash", "Glob", "Grep"):
        assert name in tools.group(1)
    for name in WORKER_TOOLS:
        assert f"mcp__{SERVER_NAME}__{name}" in tools.group(1)
    # No web/network tool is granted to a worktree-confined worker.
    assert "WebFetch" not in document and "WebSearch" not in document


def test_codex_definition_uses_the_workspace_sandbox() -> None:
    document = worker_document("codex")
    assert document.startswith(AGENT_MARKER_TOML)
    parsed = tomllib.loads(document)
    assert parsed["name"] == WORKER_NAME
    assert parsed["sandbox_mode"] == "workspace-write"
    assert parsed["features"]["apps"] is False
    assert "native_claim" in parsed["developer_instructions"]
    assert "model" not in parsed  # inherits the host session's own model


def test_opencode_definition_grants_only_read_edit_bash_and_mcp() -> None:
    document = worker_document("opencode")
    frontmatter = document.split("---", 2)[1]
    assert "mode: subagent" in frontmatter
    assert 'action: "*", resource: "*", effect: deny' in frontmatter
    for action in ("read", "edit", "bash"):
        assert f'action: "{action}", resource: "*", effect: allow' in frontmatter
    for tool in WORKER_TOOLS:
        assert f'action: "saberops_{tool}"' in frontmatter


def test_antigravity_never_gets_an_automated_worker() -> None:
    with pytest.raises(ValueError):
        worker_document("antigravity")
    agent = agent_connect.agent_by_slug("antigravity")
    assert agent is not None
    assert worker_file_path(agent) is None
    assert install_worker("antigravity").agent_action == "unsupported"


def test_dry_run_writes_nothing(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from saberops.cli import main

    assert main(["connect", "claude", "--skill", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert WORKER_NAME in out
    assert not (home / ".claude").exists()


def test_skill_install_also_installs_and_removes_the_worker(home: Path) -> None:
    path = _path(home, "codex")
    result = install_skill("codex", home=home)
    assert result.ok, result.message
    assert path.is_file()
    assert AGENT_MARKER_TOML in path.read_text(encoding="utf-8")
    assert [file.target for file in result.agent_files] == [WORKER_NAME]
    assert result.agent_action == "installed"
    # Reinstalling is idempotent.
    again = install_skill("codex", home=home)
    assert again.agent_action == "unchanged"
    removed = remove_skill("codex", home=home)
    assert removed.ok and not path.exists()


def test_a_foreign_worker_file_is_never_overwritten_or_removed(home: Path) -> None:
    path = _path(home, "opencode")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# the owner's own worker\n", encoding="utf-8")
    installed = install_worker("opencode", home=home)
    assert installed.ok is False and installed.agent_action == "refused"
    assert path.read_text(encoding="utf-8") == "# the owner's own worker\n"
    removed = remove_worker("opencode", home=home)
    assert removed.ok is False and path.exists()


def test_install_and_remove_is_one_command(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from saberops.cli import main

    path = _path(home, "claude")
    assert main(["connect", "claude", "--skill"]) == 0
    assert path.is_file()
    capsys.readouterr()
    assert main(["connect", "claude", "--remove"]) == 0
    out = capsys.readouterr().out
    assert "Removed the SaberOps native worker" in out
    assert not path.exists()


def test_worker_lists_only_tools_the_server_can_expose() -> None:
    for name in WORKER_TOOLS:
        assert name in NATIVE_TOOL_NAMES or name in TOOL_NAMES
