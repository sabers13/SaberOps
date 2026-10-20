"""Connect SaberOps to coding-agent CLIs as an MCP server (``orch connect``).

Every supported agent gets the same stdio server, ``<orch> mcp serve``, with
``SABEROPS_PROFILE`` passed through when a profile is active. Where the agent
ships its own registration command it is used (Claude Code, Codex,
Antigravity), so its config stays in the format it owns. Agents configured by
a JSON file (OpenCode, Cursor) are edited only when the file is plain JSON,
after a timestamped backup; a file with comments is never rewritten, the
snippet is returned for the owner to paste instead.

Nothing here talks to a model or a network. Command execution and the home
directory are injectable so tests never touch the owner's real agents.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = [
    "AGENTS",
    "Agent",
    "ConnectResult",
    "agent_by_slug",
    "agent_rows",
    "connect",
    "disconnect",
    "is_connected",
    "orch_command",
    "server_snippet",
]

SERVER_NAME = "saberops"

Runner = Callable[[Sequence[str], Path | None], "subprocess.CompletedProcess[str]"]


def _run(argv: Sequence[str], cwd: Path | None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv), cwd=cwd, capture_output=True, text=True, timeout=60, check=False
    )


@dataclass(frozen=True)
class Agent:
    slug: str
    name: str
    executable: str
    method: str  # "cli" (own mcp add/remove) or "json" (config file)
    project_scope: bool = False
    config_files: tuple[str, ...] = field(default_factory=tuple)  # json agents, user scope
    project_file: str = ""


AGENTS: tuple[Agent, ...] = (
    Agent("claude", "Claude Code", "claude", "cli", project_scope=True),
    Agent("codex", "Codex", "codex", "cli"),
    Agent("antigravity", "Antigravity", "agy", "cli"),
    Agent(
        "opencode",
        "OpenCode",
        "opencode",
        "json",
        project_scope=True,
        config_files=(".config/opencode/opencode.json", ".config/opencode/opencode.jsonc"),
        project_file="opencode.json",
    ),
    Agent(
        "cursor",
        "Cursor",
        "cursor",
        "json",
        project_scope=True,
        config_files=(".cursor/mcp.json",),
        project_file=".cursor/mcp.json",
    ),
)


def agent_by_slug(slug: str) -> Agent | None:
    return next((agent for agent in AGENTS if agent.slug == slug.strip().lower()), None)


@dataclass(frozen=True)
class ConnectResult:
    agent: str
    ok: bool
    message: str
    commands: tuple[str, ...] = ()
    snippet: str = ""


def orch_command() -> str:
    """Absolute path of the ``orch`` launcher, so agent PATH quirks cannot break it."""
    found = shutil.which("orch")
    return str(Path(found).resolve()) if found else "orch"


def _env() -> dict[str, str]:
    profile = os.environ.get("SABEROPS_PROFILE", "").strip()
    return {"SABEROPS_PROFILE": profile} if profile else {}


def _home(home: Path | None) -> Path:
    return home if home is not None else Path.home()


def server_snippet(agent: Agent) -> str:
    """The exact config an owner can paste by hand."""
    orch = orch_command()
    env = _env()
    if agent.slug == "opencode":
        entry: dict[str, Any] = {
            "type": "local",
            "command": [orch, "mcp", "serve"],
            "enabled": True,
        }
        if env:
            entry["environment"] = env
        return json.dumps({"mcp": {SERVER_NAME: entry}}, indent=2)
    if agent.slug == "codex":
        lines = [f"[mcp_servers.{SERVER_NAME}]", f'command = "{orch}"', 'args = ["mcp", "serve"]']
        if env:
            lines += [f"[mcp_servers.{SERVER_NAME}.env]"] + [f'{k} = "{v}"' for k, v in env.items()]
        return "\n".join(lines)
    server: dict[str, Any] = {"command": orch, "args": ["mcp", "serve"]}
    if env:
        server["env"] = env
    return json.dumps({"mcpServers": {SERVER_NAME: server}}, indent=2)


def _cli_add(agent: Agent, project: bool) -> list[str]:
    orch = orch_command()
    env = _env()
    if agent.slug == "claude":
        argv = ["claude", "mcp", "add", "--scope", "project" if project else "user", SERVER_NAME]
        for key, value in env.items():
            argv += ["-e", f"{key}={value}"]
        return [*argv, "--", orch, "mcp", "serve"]
    if agent.slug == "codex":
        argv = ["codex", "mcp", "add", SERVER_NAME]
        for key, value in env.items():
            argv += ["--env", f"{key}={value}"]
        return [*argv, "--", orch, "mcp", "serve"]
    argv = ["agy", "mcp", "add"]
    for key, value in env.items():
        argv += ["-e", f"{key}={value}"]
    return [*argv, SERVER_NAME, orch, "mcp", "serve"]


def _cli_remove(agent: Agent, project: bool) -> list[str]:
    if agent.slug == "claude":
        return ["claude", "mcp", "remove", "--scope", "project" if project else "user", SERVER_NAME]
    return [agent.executable, "mcp", "remove", SERVER_NAME]


def _json_path(agent: Agent, *, project: Path | None, home: Path | None) -> Path:
    if project is not None:
        return project / agent.project_file
    base = _home(home)
    for name in agent.config_files:
        if (base / name).is_file():
            return base / name
    return base / agent.config_files[0]


def _json_edit(path: Path, agent: Agent, *, remove: bool) -> ConnectResult:
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return ConnectResult(
                agent.slug,
                False,
                f"{path} has comments or is not plain JSON, so it was left untouched. "
                "Paste the snippet into it by hand.",
                snippet=server_snippet(agent),
            )
        if not isinstance(loaded, dict):
            return ConnectResult(agent.slug, False, f"{path} is not a JSON object; left untouched.")
        data = loaded
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup = path.with_name(f"{path.name}.bak-saberops-{stamp}")
        backup.write_bytes(path.read_bytes())
    key = "mcp" if agent.slug == "opencode" else "mcpServers"
    servers = data.get(key) if isinstance(data.get(key), dict) else {}
    assert isinstance(servers, dict)
    if remove:
        if SERVER_NAME not in servers:
            return ConnectResult(agent.slug, True, f"{agent.name} was not connected.")
        servers.pop(SERVER_NAME)
    else:
        servers[SERVER_NAME] = json.loads(server_snippet(agent))[key][SERVER_NAME]
    data[key] = servers
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    verb = "Disconnected" if remove else "Connected"
    return ConnectResult(agent.slug, True, f"{verb} {agent.name} ({path}).")


def connect(
    slug: str,
    *,
    project: Path | None = None,
    dry_run: bool = False,
    runner: Runner | None = None,
    home: Path | None = None,
) -> ConnectResult:
    """Register SaberOps with one agent (user scope, or ``project`` folder)."""
    agent = agent_by_slug(slug)
    if agent is None:
        return ConnectResult(slug, False, f"Unknown agent {slug!r}.")
    if project is not None and not agent.project_scope:
        return ConnectResult(slug, False, f"{agent.name} has no per-project MCP setting.")
    if agent.method == "json":
        path = _json_path(agent, project=project, home=home)
        if dry_run:
            return ConnectResult(
                slug, True, f"Would add SaberOps to {path}.", snippet=server_snippet(agent)
            )
        return _json_edit(path, agent, remove=False)
    if shutil.which(agent.executable) is None and runner is None:
        return ConnectResult(
            slug, False, f"{agent.name} is not installed.", snippet=server_snippet(agent)
        )
    run = runner or _run
    commands = []
    if not dry_run:
        # Re-adding replaces a stale entry (an old orch path or profile).
        run(_cli_remove(agent, project is not None), project)
    argv = _cli_add(agent, project is not None)
    commands.append(" ".join(argv))
    if dry_run:
        return ConnectResult(slug, True, "Would run:", commands=tuple(commands))
    result = run(argv, project)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        return ConnectResult(
            slug,
            False,
            f"{agent.name} refused: {detail[-1] if detail else 'exit ' + str(result.returncode)}",
            commands=tuple(commands),
            snippet=server_snippet(agent),
        )
    return ConnectResult(slug, True, f"Connected {agent.name}.", commands=tuple(commands))


def disconnect(
    slug: str,
    *,
    project: Path | None = None,
    runner: Runner | None = None,
    home: Path | None = None,
) -> ConnectResult:
    agent = agent_by_slug(slug)
    if agent is None:
        return ConnectResult(slug, False, f"Unknown agent {slug!r}.")
    if agent.method == "json":
        return _json_edit(_json_path(agent, project=project, home=home), agent, remove=True)
    if shutil.which(agent.executable) is None and runner is None:
        return ConnectResult(slug, False, f"{agent.name} is not installed.")
    argv = _cli_remove(agent, project is not None)
    result = (runner or _run)(argv, project)
    if result.returncode != 0:
        return ConnectResult(
            slug, True, f"{agent.name} was not connected.", commands=(" ".join(argv),)
        )
    return ConnectResult(slug, True, f"Disconnected {agent.name}.", commands=(" ".join(argv),))


def is_connected(agent: Agent, *, home: Path | None = None, runner: Runner | None = None) -> bool:
    """Cheap user-scope check: read the agent's own registry, never start it."""
    base = _home(home)
    try:
        if agent.slug == "claude":
            data = json.loads((base / ".claude.json").read_text(encoding="utf-8"))
            return SERVER_NAME in (data.get("mcpServers") or {})
        if agent.slug == "codex":
            text = (base / ".codex" / "config.toml").read_text(encoding="utf-8")
            return re.search(rf"^\[mcp_servers\.{SERVER_NAME}\]", text, re.M) is not None
        if agent.slug == "antigravity":
            # Its registry format is private; its own list command is fast (~0.1 s).
            if runner is None and (
                shutil.which(agent.executable) is None
                or os.environ.get("PYTEST_CURRENT_TEST") is not None
            ):
                return False
            listed = (runner or _run)([agent.executable, "mcp", "list"], None)
            return (
                listed.returncode == 0
                and re.search(rf"\b{SERVER_NAME}\b", listed.stdout or "") is not None
            )
        path = _json_path(agent, project=None, home=home)
        data = json.loads(path.read_text(encoding="utf-8"))
        key = "mcp" if agent.slug == "opencode" else "mcpServers"
        return SERVER_NAME in (data.get(key) or {})
    except (OSError, ValueError, AttributeError):
        return False


def agent_rows(*, home: Path | None = None) -> list[dict[str, Any]]:
    """Every supported agent: installed, connected, and a paste-able snippet."""
    rows = []
    for agent in AGENTS:
        rows.append(
            {
                "slug": agent.slug,
                "name": agent.name,
                "installed": shutil.which(agent.executable) is not None,
                "connected": is_connected(agent, home=home),
                "snippet": server_snippet(agent),
            }
        )
    return rows
