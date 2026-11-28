"""H52 (ADR-0110): a connect's skill and agent files follow the MCP entry's scope.

A project connect used to write the skills and ``saberops-worker`` under
``~/.claude`` while the MCP entry lived in the project.  The user-level worker
named a server that exists only for one project, so ``claude doctor`` reported
it broken.  Every test uses a temporary home and a temporary project directory:
the owner's real ``~/.claude``, ``~/.codex`` and XDG roots are never touched.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from saberops import agent_connect
from saberops.agent_connect import (
    SKILL_NAMES,
    WORKER_NAME,
    connect,
    disconnect,
    install_agents,
    install_skill,
    remove_agents,
    remove_skill,
)

PROJECT = {
    "claude": (".claude/skills", ".claude/agents"),
    "opencode": (".opencode/skills", ".opencode/agents"),
}
USER = {
    "claude": (".claude/skills", ".claude/agents"),
    "opencode": (".config/opencode/skills", ".config/opencode/agents"),
}


class FakeRunner:
    """Answer the vendor CLI's ``mcp add|remove`` and write its own registry."""

    def __init__(self, project: Path | None = None, home: Path | None = None) -> None:
        self.project = project
        self.home = home
        self.calls: list[tuple[list[str], Path | None]] = []

    def __call__(self, argv: Sequence[str], cwd: Path | None) -> subprocess.CompletedProcess[str]:
        argv = list(argv)
        self.calls.append((argv, cwd))
        if argv[:3] == ["claude", "mcp", "add"] and argv[3:5] == ["--scope", "project"]:
            assert self.project is not None
            (self.project / ".mcp.json").write_text(
                json.dumps({"mcpServers": {"saberops": {"command": "/opt/orch"}}}),
                encoding="utf-8",
            )
        if argv[:3] == ["claude", "mcp", "remove"] and argv[3:5] == ["--scope", "project"]:
            assert self.project is not None
            (self.project / ".mcp.json").unlink(missing_ok=True)
        if argv[:3] == ["claude", "mcp", "add"] and argv[3:5] == ["--scope", "user"]:
            assert self.home is not None
            (self.home / ".claude.json").write_text(
                json.dumps({"mcpServers": {"saberops": {"command": "/opt/orch"}}}),
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(argv, 0, "", "")


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = tmp_path / "home"
    base.mkdir()
    monkeypatch.setenv("HOME", str(base))
    monkeypatch.setattr(agent_connect, "_home", lambda _home=None: base)
    return base


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    folder = tmp_path / "project"
    folder.mkdir()
    return folder


def _files(root: Path) -> list[str]:
    return sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file())


def _skill(name: str = "saberops") -> str:
    return f"{name}/SKILL.md"


@pytest.mark.parametrize("slug", sorted(PROJECT))
def test_project_connect_writes_nothing_under_home(
    home: Path, project: Path, slug: str
) -> None:
    skill_dir, agent_dir = PROJECT[slug]
    runner = FakeRunner(project=project, home=home)
    result = connect(slug, project=project, home=home, runner=runner, skill=True)
    assert result.ok, result.message
    assert result.skill_action == "installed"
    assert _files(home) == []
    written = _files(project)
    for name in SKILL_NAMES:
        assert f"{skill_dir}/{_skill(name)}" in written
    extension = "toml" if slug == "codex" else "md"
    assert f"{agent_dir}/{WORKER_NAME}.{extension}" in written
    # The worker names the server that the same scope registers.
    worker = (project / agent_dir / f"{WORKER_NAME}.{extension}").read_text(encoding="utf-8")
    assert "saberops" in worker


def test_claude_project_connect_registers_and_installs_at_the_same_scope(
    home: Path, project: Path
) -> None:
    result = connect(
        "claude", project=project, home=home, runner=FakeRunner(project, home), skill=True
    )
    assert result.ok
    registration = json.loads((project / ".mcp.json").read_text(encoding="utf-8"))
    assert "saberops" in registration["mcpServers"]
    worker = (project / ".claude" / "agents" / f"{WORKER_NAME}.md").read_text(encoding="utf-8")
    assert "mcpServers:\n  - saberops" in worker
    assert not (home / ".claude").exists()


def test_user_connect_writes_nothing_under_the_project(
    home: Path, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(project)
    result = connect("claude", home=home, runner=FakeRunner(project, home), skill=True)
    assert result.ok and result.skill_action == "installed"
    assert _files(project) == []
    written = _files(home)
    assert ".claude.json" in written
    for name in SKILL_NAMES:
        assert f"{USER['claude'][0]}/{_skill(name)}" in written
    assert f"{USER['claude'][1]}/{WORKER_NAME}.md" in written


@pytest.mark.parametrize("slug", ["codex", "antigravity"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_project_skill_is_refused_without_project_scope(
    home: Path, project: Path, slug: str, dry_run: bool
) -> None:
    result = connect(
        slug,
        project=project,
        home=home,
        runner=FakeRunner(project, home),
        skill=True,
        dry_run=dry_run,
    )
    assert not result.ok
    assert "no per-project" in result.message
    assert "user scope" in result.message
    assert _files(home) == [] and _files(project) == []


def test_cli_refuses_project_skill_for_codex(
    home: Path, project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from saberops.cli import main

    assert main(["connect", "codex", "--project", str(project), "--skill"]) == 1
    assert "no per-project" in capsys.readouterr().out
    assert _files(home) == [] and _files(project) == []


@pytest.mark.parametrize("slug", sorted(PROJECT))
def test_project_removal_cleans_exactly_what_connect_wrote(
    home: Path, project: Path, slug: str
) -> None:
    skill_dir, agent_dir = PROJECT[slug]
    runner = FakeRunner(project, home)
    assert connect(slug, project=project, home=home, runner=runner, skill=True).ok
    written = _files(project)
    assert all(f"{skill_dir}/{_skill(name)}" in written for name in SKILL_NAMES)
    assert f"{agent_dir}/{WORKER_NAME}.md" in written

    gone = disconnect(slug, project=project, home=home, runner=runner)
    assert gone.ok and gone.skill_action == "removed"
    assert _files(home) == []
    remaining = _files(project)
    for name in SKILL_NAMES:
        assert f"{skill_dir}/{_skill(name)}" not in remaining
    assert f"{agent_dir}/{WORKER_NAME}.md" not in remaining


def test_project_removal_leaves_a_user_install_alone(home: Path, project: Path) -> None:
    assert install_skill("claude", home=home).ok
    user_files = _files(home)
    runner = FakeRunner(project, home)
    assert connect("claude", project=project, home=home, runner=runner, skill=True).ok

    assert disconnect("claude", project=project, home=home, runner=runner).ok
    assert _files(home) == user_files
    assert all((home / name).is_file() for name in user_files)


def test_project_dry_run_names_project_paths_and_writes_nothing(
    home: Path, project: Path
) -> None:
    result = connect(
        "claude", project=project, home=home, runner=FakeRunner(project, home),
        skill=True, dry_run=True,
    )
    assert result.ok and result.skill_action == "would-install"
    for item in result.skill_files:
        assert item.path.startswith(str(project))
    assert result.agent_files[0].path == str(project / ".claude" / "agents" / f"{WORKER_NAME}.md")
    assert _files(home) == [] and _files(project) == []


@pytest.mark.parametrize("slug", sorted(PROJECT))
def test_project_proxy_definitions_follow_the_scope(
    home: Path, project: Path, slug: str
) -> None:
    _, agent_dir = PROJECT[slug]
    result = install_agents(slug, home=home, project=project)
    assert result.ok and result.agent_action == "installed"
    assert _files(home) == []
    assert all(item.path.startswith(str(project / agent_dir)) for item in result.agent_files)

    removed = remove_agents(slug, home=home, project=project)
    assert removed.ok and removed.agent_action == "removed"
    assert not (project / agent_dir).exists()


def test_project_agents_are_refused_without_a_project_format(
    home: Path, project: Path
) -> None:
    refused = install_agents("codex", home=home, project=project)
    assert not refused.ok and refused.agent_action == "refused"
    assert "no project-level subagent format" in refused.message
    assert _files(home) == [] and _files(project) == []


def test_direct_project_skill_for_codex_does_not_fall_back_to_home(
    home: Path, project: Path
) -> None:
    refused = install_skill("codex", home=home, project=project)
    assert not refused.ok and refused.skill_action == "refused"
    assert _files(home) == [] and _files(project) == []
    assert not remove_skill("codex", home=home, project=project).ok


def test_project_disconnect_for_codex_keeps_the_user_entry(home: Path, project: Path) -> None:
    config = home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('[mcp_servers.saberops]\ncommand = "/opt/orch"\n', encoding="utf-8")

    refused = disconnect("codex", project=project, home=home)
    assert not refused.ok and "no project-level" in refused.message
    assert "saberops" in config.read_text(encoding="utf-8")
