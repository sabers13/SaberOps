"""Generated proxy subagent definitions installed by ``orch connect --agents``.

Every test uses a temporary home directory: the owner's real ``~/.claude``,
``~/.codex``, ``~/.config/opencode`` and ``~/.gemini`` are never read or written.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest

from saberops import agent_connect
from saberops.agent_connect import (
    AGENT_MARKER,
    AGENT_MARKER_TOML,
    HOST_MODEL,
    PROXIES,
    PROXY_FAMILY,
    PROXY_TOOLS,
    agent_by_slug,
    agent_document,
    agent_file_path,
    install_agents,
    remove_agents,
)
from saberops.mcp.tools import TOOL_NAMES

HOSTS = {
    "claude": ".claude/agents",
    "codex": ".codex/agents",
    "opencode": ".config/opencode/agents",
    "antigravity": ".gemini/config/agents",
}

EXPECTED_TARGETS = {
    "claude": ("codex-worker", "opencode-go-worker"),
    "codex": ("claude-worker", "opencode-go-worker"),
    "opencode": ("claude-worker", "codex-worker"),
    "antigravity": ("claude-worker", "codex-worker", "opencode-go-worker"),
}


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = tmp_path / "home"
    base.mkdir()
    monkeypatch.setenv("HOME", str(base))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(base / ".config"))
    monkeypatch.setattr(agent_connect, "_home", lambda _home=None: base)
    return base


def _expected(home: Path, slug: str, target: str) -> Path:
    extension = "toml" if slug == "codex" else "md"
    return home / HOSTS[slug] / f"{target}.{extension}"


def _documents() -> list[tuple[str, str, str]]:
    return [
        (slug, target, agent_document(slug, target))
        for slug, targets in EXPECTED_TARGETS.items()
        for target in targets
    ]


def test_every_host_target_and_family_matches_the_design() -> None:
    assert {
        slug: tuple(proxy.name for proxy in proxies) for slug, proxies in PROXIES.items()
    } == EXPECTED_TARGETS
    assert PROXY_FAMILY == {
        "claude-worker": "anthropic",
        "codex-worker": "codex",
        "opencode-go-worker": "opencode-go",
    }
    # A host never gets a proxy for its own subscription, and Antigravity is
    # never a target.
    assert "claude-worker" not in EXPECTED_TARGETS["claude"]
    assert "codex-worker" not in EXPECTED_TARGETS["codex"]
    assert all("agy-worker" not in targets for targets in EXPECTED_TARGETS.values())


@pytest.mark.parametrize("slug", sorted(HOSTS))
def test_dry_run_shows_the_exact_file_and_path_for_each_cli(home: Path, slug: str) -> None:
    result = install_agents(slug, home=home, dry_run=True)
    assert result.ok and result.agent_action == "would-install"
    assert [item.target for item in result.agent_files] == list(EXPECTED_TARGETS[slug])
    for item in result.agent_files:
        path = _expected(home, slug, item.target)
        assert item.path == str(path)
        assert item.content == agent_document(slug, item.target)
        assert item.action == "would-install"
        assert not path.exists()


@pytest.mark.parametrize("slug, target", [(s, t) for s, ts in EXPECTED_TARGETS.items() for t in ts])
def test_exact_content_per_cli(slug: str, target: str) -> None:
    document = agent_document(slug, target)
    family = PROXY_FAMILY[target]
    model = HOST_MODEL[slug]
    assert f'caller_id="proxy:{target}"' in document
    assert f'requirements={{"model_family_include": ["{family}"]}}' in document
    assert target in document
    # An `agy` proxy is never generated; the guide only names the gap in prose.
    assert "name: agy-worker" not in document
    assert 'name = "agy-worker"' not in document
    assert "no eligible candidate" in document
    # Every definition forbids editing, accepting and merging in plain words.
    assert "Never edit files" in document and "never push and never merge" in document
    # Every definition references the session's server rather than starting one.
    assert "orch mcp serve" in document  # as a prohibition
    assert "mcp serve" not in document.replace("orch mcp serve", "")
    if slug == "claude":
        assert document.startswith("---\nname: ")
        assert f"model: {model}" in document
        assert "mcpServers:\n  - saberops" in document
        assert "mcp__saberops__submit_job" in document
        tools = re.search(r"^tools: (.+)$", document, re.M)
        assert tools is not None
        assert all(
            name.removeprefix("mcp__saberops__") in TOOL_NAMES
            for name in tools.group(1).split(", ")
        )
        assert not any(
            builtin in tools.group(1) for builtin in ("Read", "Bash", "Edit", "Write")
        )
    elif slug == "codex":
        assert document.startswith(AGENT_MARKER_TOML)
        parsed = tomllib.loads(document)
        assert parsed["name"] == target
        assert parsed["model"] == model
        assert parsed["features"]["shell_tool"] is False
        assert "mcp_servers" not in parsed
        assert family in parsed["developer_instructions"]
        assert "mcp serve" not in document.replace("orch mcp serve", "")
    elif slug == "opencode":
        assert document.startswith("---\ndescription: ")
        assert f"model: {model}" in document
        frontmatter = document.split("---", 2)[1]
        assert 'action: "*", resource: "*", effect: deny' in frontmatter
        for tool in PROXY_TOOLS:
            assert f'action: "saberops_{tool}"' in frontmatter
        assert "saberops_on" not in frontmatter and "saberops_off" not in frontmatter
    elif slug == "antigravity":
        assert document.startswith("---\nname: ")
        assert f"model: {model}" in document
        assert "subagent: true" in document
        assert "commandExecutionPolicy: sandbox" in document
        assert "\ntools:" not in document  # agy hangs on unmapped tool names


@pytest.mark.parametrize("target", EXPECTED_TARGETS["codex"])
def test_codex_proxy_is_a_valid_custom_agent_file(target: str) -> None:
    """Codex 0.170.0 loads ``~/.codex/agents/*.toml`` custom agents.

    It requires ``name``, ``description`` and ``developer_instructions``; model
    settings in the file override the parent, and an ``mcp_servers`` table is
    ignored because the child inherits the parent's servers.  The generated
    file must stay inside that schema or Codex silently skips it (D13).
    """
    parsed = tomllib.loads(agent_document("codex", target))
    assert parsed["name"] == target
    assert parsed["description"]
    assert parsed["developer_instructions"].strip()
    assert parsed["model"] == HOST_MODEL["codex"]
    assert parsed["model_reasoning_effort"] == "low"
    assert "mcp_servers" not in parsed
    assert "saberops" in parsed["developer_instructions"]


def test_only_live_tool_names_appear() -> None:
    for slug, target, document in _documents():
        loop = document.split("## Full SaberOps guide", 1)[0]
        mentioned = set(re.findall(r"`([a-z][a-z0-9_]+)`", loop))
        params = {
            "saberops",
            "repo",
            "goal",
            "acceptance",
            "idempotency_key",
            "caller_id",
            "requirements",
            "model_family_include",
            "provider",
            "model",
            "route",
            "timeout_seconds",
            "next_sequence",
            "candidate_sha",
            "owner_action",
            "job_id",
            "state",
            "get_job",
        }
        assert mentioned - set(TOOL_NAMES) - params == set(), (slug, target)
        assert set(PROXY_TOOLS) <= set(TOOL_NAMES)
        for server_action in re.findall(r"saberops_([a-z][a-z0-9_]+)", document):
            assert server_action in TOOL_NAMES or server_action in {"on", "off"}, (
                slug,
                target,
                server_action,
            )


def test_no_inline_saberops_command_or_url() -> None:
    for _slug, _target, document in _documents():
        assert "http://" not in document and "https://" not in document
        # The only mention of ``mcp serve`` is the prohibition.
        assert document.count("mcp serve") == 1
        assert "mcpServers:\n  - saberops" in document or _slug != "claude"
        assert '"command"' not in document


@pytest.mark.parametrize("slug", sorted(HOSTS))
def test_install_writes_one_owned_file_per_proxy(home: Path, slug: str) -> None:
    result = install_agents(slug, home=home)
    assert result.ok and result.agent_action == "installed"
    for item in result.agent_files:
        path = _expected(home, slug, item.target)
        assert path.is_file() and item.path == str(path)
        content = path.read_text(encoding="utf-8")
        assert content == agent_document(slug, item.target)
        assert AGENT_MARKER in content or AGENT_MARKER_TOML in content
    assert list(home.rglob("*.tmp")) == []


@pytest.mark.parametrize("slug", sorted(HOSTS))
def test_reinstall_is_idempotent(home: Path, slug: str) -> None:
    first = install_agents(slug, home=home)
    assert first.ok
    second = install_agents(slug, home=home)
    assert second.ok and second.agent_action == "unchanged"
    for item in second.agent_files:
        assert item.action == "unchanged"
    # An older SaberOps file (still carrying the marker) is refreshed.
    target = EXPECTED_TARGETS[slug][0]
    path = _expected(home, slug, target)
    path.write_text("stale", encoding="utf-8")
    path.write_text(
        (AGENT_MARKER_TOML if slug == "codex" else AGENT_MARKER) + "\nstale\n", encoding="utf-8"
    )
    refreshed = install_agents(slug, home=home)
    assert refreshed.ok and refreshed.agent_action == "installed"
    assert path.read_text(encoding="utf-8") == agent_document(slug, target)


@pytest.mark.parametrize("slug", sorted(HOSTS))
def test_a_foreign_file_is_never_overwritten_or_removed(home: Path, slug: str) -> None:
    target = EXPECTED_TARGETS[slug][0]
    path = _expected(home, slug, target)
    path.parent.mkdir(parents=True)
    original = "hand written by the owner\n"
    path.write_text(original, encoding="utf-8")

    refused = install_agents(slug, home=home)
    assert not refused.ok and refused.agent_action == "refused"
    assert path.read_text(encoding="utf-8") == original

    removed = remove_agents(slug, home=home)
    assert not removed.ok and removed.agent_action == "refused"
    assert path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("slug", sorted(HOSTS))
def test_removal_is_one_command_and_keeps_foreign_siblings(home: Path, slug: str) -> None:
    assert install_agents(slug, home=home).ok
    sibling = _expected(home, slug, EXPECTED_TARGETS[slug][0]).parent / "notes.md"
    sibling.write_text("mine", encoding="utf-8")

    removed = remove_agents(slug, home=home)
    assert removed.ok and removed.agent_action == "removed"
    for target in EXPECTED_TARGETS[slug]:
        assert not _expected(home, slug, target).exists()
    assert sibling.read_text(encoding="utf-8") == "mine"
    assert sibling.parent.is_dir()

    again = remove_agents(slug, home=home)
    assert again.ok and again.agent_action == "absent"

    sibling.unlink()
    install_agents(slug, home=home)
    clean = remove_agents(slug, home=home)
    assert clean.ok and clean.agent_action == "removed"
    assert not sibling.parent.exists()


def test_cursor_has_no_subagent_format(home: Path) -> None:
    cursor = agent_by_slug("cursor")
    assert cursor is not None and cursor.agent_dir == ""
    assert agent_file_path(cursor, "claude-worker", home=home) is None
    result = install_agents("cursor", home=home)
    assert result.ok and result.agent_action == "unsupported"
    assert "unsupported" in result.message
    assert remove_agents("cursor", home=home).agent_action == "unsupported"
    with pytest.raises(ValueError):
        agent_document("cursor", "claude-worker")
    with pytest.raises(ValueError):
        agent_document("claude", "agy-worker")


def test_claude_first_install_prints_the_restart_hint(home: Path) -> None:
    result = install_agents("claude", home=home, dry_run=True)
    assert "Restart Claude Code" in result.message
    assert not (home / ".claude" / "agents").exists()


def test_cli_dry_run_prints_the_files_and_writes_nothing(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from saberops.cli import main

    assert main(["connect", "--agents", "--dry-run"]) == 0
    out = capsys.readouterr().out
    for slug, targets in EXPECTED_TARGETS.items():
        for target in targets:
            assert f"Agent file: {_expected(home, slug, target)}" in out
    assert "saberops_submit_job" in out or "mcp__saberops__submit_job" in out
    assert not any(home.rglob("*.md")) and not any(home.rglob("*.toml"))


def test_cli_install_then_disconnect_leaves_home_clean(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from saberops.cli import main

    assert main(["connect", "--agents"]) == 0
    files = sorted(path for path in home.rglob("*") if path.is_file())
    assert len(files) == sum(len(targets) for targets in EXPECTED_TARGETS.values())
    capsys.readouterr()
    assert main(["disconnect", "--agents"]) == 0
    out = capsys.readouterr().out
    assert "Removed the SaberOps proxy" in out
    assert not [path for path in home.rglob("*") if path.is_file()]
    for slug in HOSTS:
        assert not (home / HOSTS[slug]).exists()


def test_cli_named_host_only_touches_that_host(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from saberops.cli import main

    assert main(["connect", "codex", "--agents"]) == 0
    capsys.readouterr()
    assert list((home / ".codex" / "agents").iterdir())
    assert not (home / ".claude").exists()
    assert not (home / ".config" / "opencode" / "agents").exists()
    assert not (home / ".gemini" / "config" / "agents").exists()


def test_cli_agents_mode_does_not_touch_mcp_config(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from saberops.cli import main

    config = home / ".config" / "opencode" / "opencode.json"
    config.parent.mkdir(parents=True)
    original = json.dumps({"theme": "x"})
    config.write_text(original, encoding="utf-8")

    assert main(["connect", "opencode", "--agents"]) == 0
    capsys.readouterr()
    assert config.read_text(encoding="utf-8") == original
    assert (home / ".config" / "opencode" / "agents" / "claude-worker.md").is_file()


def test_proxy_definition_stays_short_and_references_the_guide_resource() -> None:
    """D12: every proxy launch pays for its body; the guide is a resource.

    The whole working guide used to be inlined, making each definition ~21 KB.
    It stays on the server as ``saberops://guide`` and the definition only
    points at it.
    """
    from saberops.agent_connect import WORKER_VENDOR, worker_document
    from saberops.mcp.guide import GUIDE_URI

    worker_size = max(len(worker_document(slug)) for slug in WORKER_VENDOR)
    for slug, target, document in _documents():
        assert GUIDE_URI in document, (slug, target)
        # The guide's own opening heading is the marker of an inlined copy.
        assert "# Working with SaberOps" not in document, (slug, target)
        assert len(document) < worker_size + 2_000, (slug, target, len(document))
