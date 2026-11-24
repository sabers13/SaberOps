"""The optional per-CLI instruction skill installed by ``orch connect --skill``.

Every test uses a temporary home directory: the owner's real ``~/.claude``,
``~/.codex``, ``~/.config/opencode`` and ``~/.gemini`` are never read or written.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from saberops import agent_connect
from saberops.agent_connect import (
    SKILL_MARKER,
    SKILL_NAME,
    agent_by_slug,
    connect,
    disconnect,
    install_skill,
    remove_skill,
    skill_document,
    skill_path,
)
from saberops.agent_skills import (
    MAX_MAIN_SKILL_LINES,
    SKILL_MODES_NAME,
    SKILL_NAMES,
    SKILL_PLANNING_NAME,
    SKILL_SETUP_NAME,
)
from saberops.mcp.guide import GUIDE
from saberops.mcp.tools import TOOL_NAMES

SUPPORTED = {
    "claude": ".claude/skills",
    "codex": ".codex/skills",
    "opencode": ".config/opencode/skills",
    "antigravity": ".gemini/config/skills",
}


class FakeRunner:
    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        self.calls: list[tuple[list[str], Path | None]] = []
        self.returncode = returncode
        self.stdout = stdout

    def __call__(self, argv: Sequence[str], cwd: Path | None) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), cwd))
        return subprocess.CompletedProcess(list(argv), self.returncode, self.stdout, "")


class CodexRunner(FakeRunner):
    """Writes the table ``codex mcp add`` writes, so approval can be pinned."""

    def __init__(self, home: Path) -> None:
        super().__init__()
        self.home = home

    def __call__(self, argv: Sequence[str], cwd: Path | None) -> subprocess.CompletedProcess[str]:
        result = super().__call__(argv, cwd)
        if list(argv[:3]) == ["codex", "mcp", "add"]:
            config = self.home / ".codex" / "config.toml"
            config.parent.mkdir(parents=True, exist_ok=True)
            config.write_text(
                '[mcp_servers.saberops]\ncommand = "/opt/orch"\n', encoding="utf-8"
            )
        return result


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = tmp_path / "home"
    base.mkdir()
    monkeypatch.setenv("HOME", str(base))
    monkeypatch.setattr(agent_connect, "_home", lambda _home=None: base)
    return base


def _expected(home: Path, slug: str) -> Path:
    return home / SUPPORTED[slug] / SKILL_NAME / "SKILL.md"


def _expected_skill(home: Path, slug: str, name: str) -> Path:
    return home / SUPPORTED[slug] / name / "SKILL.md"


def _expected_all(home: Path, slug: str) -> list[Path]:
    return [_expected_skill(home, slug, name) for name in SKILL_NAMES]


@pytest.mark.parametrize("slug", sorted(SUPPORTED))
def test_dry_run_shows_the_exact_file_and_path_for_each_cli(home: Path, slug: str) -> None:
    result = install_skill(slug, home=home, dry_run=True)
    assert result.ok and result.skill_action == "would-install"
    assert result.skill_path == str(_expected(home, slug))
    assert result.skill_content == skill_document()
    assert [item.name for item in result.skill_files] == list(SKILL_NAMES)
    for name, item in zip(SKILL_NAMES, result.skill_files, strict=True):
        assert item.path == str(_expected_skill(home, slug, name))
        assert item.content == skill_document(name)
        assert item.action == "would-install"
        assert not Path(item.path).exists()


@pytest.mark.parametrize("slug", sorted(SUPPORTED))
def test_connect_dry_run_includes_the_skill(home: Path, slug: str) -> None:
    result = connect(slug, dry_run=True, runner=FakeRunner(), home=home, skill=True)
    assert result.ok and result.skill_action == "would-install"
    assert result.skill_path == str(_expected(home, slug))
    assert [item.name for item in result.skill_files] == list(SKILL_NAMES)
    assert not any(path.exists() for path in _expected_all(home, slug))


def test_cursor_has_no_skill_and_reports_unsupported(home: Path) -> None:
    cursor = agent_by_slug("cursor")
    assert cursor is not None and skill_path(cursor, home=home) is None
    result = install_skill("cursor", home=home)
    assert result.ok and result.skill_action == "unsupported"
    assert "unsupported" in result.message
    assert remove_skill("cursor", home=home).skill_action == "unsupported"
    assert not (home / ".cursor").exists()


@pytest.mark.parametrize("slug", sorted(SUPPORTED))
def test_install_writes_one_owned_file_per_skill(home: Path, slug: str) -> None:
    result = install_skill(slug, home=home)
    assert result.ok and result.skill_action == "installed"
    assert result.skill_path == str(_expected(home, slug))
    for name, path in zip(SKILL_NAMES, _expected_all(home, slug), strict=True):
        assert path.is_file()
        content = path.read_text(encoding="utf-8")
        assert content == skill_document(name)
        assert SKILL_MARKER in content
        assert content.startswith(f"---\nname: {name}\ndescription: ")
    # The full guide stays on the server; the main skill only points at it.
    main = _expected(home, slug).read_text(encoding="utf-8")
    assert GUIDE not in main
    assert "saberops://guide" in main


@pytest.mark.parametrize("slug", sorted(SUPPORTED))
def test_reinstall_is_idempotent(home: Path, slug: str) -> None:
    first = install_skill(slug, home=home)
    second = install_skill(slug, home=home)
    assert first.ok and second.ok
    assert second.skill_action == "unchanged"
    for path in _expected_all(home, slug):
        assert path.is_file()
        assert list(path.parent.glob("*.tmp")) == []
    # An older SaberOps file (still carrying the marker) is refreshed, in any
    # of the four skills.
    on_demand = _expected_skill(home, slug, SKILL_MODES_NAME)
    on_demand.write_text(
        on_demand.read_text(encoding="utf-8").replace("## The strict pause", "## Old pause"),
        encoding="utf-8",
    )
    refreshed = install_skill(slug, home=home)
    assert refreshed.ok and refreshed.skill_action == "installed"
    assert on_demand.read_text(encoding="utf-8") == skill_document(SKILL_MODES_NAME)


@pytest.mark.parametrize("slug", sorted(SUPPORTED))
def test_a_foreign_file_is_never_overwritten_or_removed(home: Path, slug: str) -> None:
    path = _expected_skill(home, slug, SKILL_PLANNING_NAME)
    path.parent.mkdir(parents=True)
    original = "---\nname: mine\ndescription: hand written\n---\n"
    path.write_text(original, encoding="utf-8")

    refused = install_skill(slug, home=home)
    assert not refused.ok and refused.skill_action == "refused"
    assert "left untouched" in refused.message
    assert path.read_text(encoding="utf-8") == original
    # The other three installed; only the foreign file is protected.
    assert _expected(home, slug).is_file()
    assert _expected_skill(home, slug, SKILL_SETUP_NAME).is_file()

    removed = remove_skill(slug, home=home)
    assert not removed.ok and removed.skill_action == "refused"
    assert path.read_text(encoding="utf-8") == original
    assert not _expected(home, slug).exists()


@pytest.mark.parametrize("slug", sorted(SUPPORTED))
def test_removal_is_one_command_and_leaves_foreign_siblings(home: Path, slug: str) -> None:
    assert install_skill(slug, home=home).ok
    path = _expected(home, slug)
    sibling = path.parent / "notes.md"
    sibling.write_text("mine", encoding="utf-8")

    removed = remove_skill(slug, home=home)
    assert removed.ok and removed.skill_action == "removed"
    assert not path.exists()
    assert all(not _expected_skill(home, slug, name).exists() for name in SKILL_NAMES)
    assert sibling.read_text(encoding="utf-8") == "mine"
    assert path.parent.is_dir()  # not empty, so the folder stays

    again = remove_skill(slug, home=home)
    assert again.ok and again.skill_action == "absent"

    sibling.unlink()
    path.parent.rmdir()
    install_skill(slug, home=home)
    clean = remove_skill(slug, home=home)
    assert clean.ok and clean.skill_action == "removed"
    assert not path.parent.exists()


def test_a_directory_in_place_of_the_file_is_refused(home: Path) -> None:
    path = _expected(home, "codex")
    path.mkdir(parents=True)
    installed = install_skill("codex", home=home)
    assert not installed.ok and installed.skill_action == "refused"
    removed = remove_skill("codex", home=home)
    assert not removed.ok and removed.skill_action == "refused"
    assert path.is_dir()
    assert not _expected_skill(home, "codex", SKILL_SETUP_NAME).exists()


def test_connect_with_skill_and_one_command_disconnect(home: Path) -> None:
    runner = FakeRunner()
    result = connect("claude", runner=runner, home=home, skill=True)
    assert result.ok and result.skill_action == "installed"
    assert all(path.is_file() for path in _expected_all(home, "claude"))

    gone = disconnect("claude", runner=runner, home=home)
    assert gone.ok and gone.skill_action == "removed"
    assert not any(path.exists() for path in _expected_all(home, "claude"))
    assert runner.calls[-1][0] == ["claude", "mcp", "remove", "--scope", "user", "saberops"]


def test_removal_still_works_when_the_cli_is_gone(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert install_skill("claude", home=home).ok
    path = _expected(home, "claude")
    import shutil

    monkeypatch.setattr(shutil, "which", lambda _name: None)
    gone = disconnect("claude", home=home)
    assert gone.ok and gone.skill_action == "removed"
    assert not path.exists()
    assert not any(item.exists() for item in _expected_all(home, "claude"))


def test_connect_without_skill_touches_no_skill_file(home: Path) -> None:
    result = connect("codex", runner=CodexRunner(home), home=home)
    assert result.ok and result.skill_action == ""
    assert not any(path.exists() for path in _expected_all(home, "codex"))


def test_json_agent_connect_with_skill_then_disconnect(home: Path) -> None:
    config = home / ".config" / "opencode" / "opencode.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"theme": "x"}), encoding="utf-8")

    result = connect("opencode", home=home, skill=True)
    assert result.ok and result.skill_action == "installed"
    assert all(path.is_file() for path in _expected_all(home, "opencode"))
    assert "saberops" in json.loads(config.read_text(encoding="utf-8"))["mcp"]

    gone = disconnect("opencode", home=home)
    assert gone.ok and gone.skill_action == "removed"
    assert not any(path.exists() for path in _expected_all(home, "opencode"))
    assert "saberops" not in json.loads(config.read_text(encoding="utf-8"))["mcp"]


def test_cli_dry_run_prints_the_file_and_path(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from saberops.cli import main

    assert main(["connect", "opencode", "--skill", "--dry-run"]) == 0
    out = capsys.readouterr().out
    for name in SKILL_NAMES:
        assert f"Skill file: {_expected_skill(home, 'opencode', name)}" in out
    assert "saberops_on" in out and "submit_job" in out
    assert not any(path.exists() for path in _expected_all(home, "opencode"))


def test_cli_install_and_remove_is_one_command(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from saberops.cli import main

    assert main(["connect", "opencode", "--skill"]) == 0
    assert all(path.is_file() for path in _expected_all(home, "opencode"))
    capsys.readouterr()
    assert main(["connect", "opencode", "--remove"]) == 0
    out = capsys.readouterr().out
    assert "Removed the SaberOps skill" in out
    assert not any(path.exists() for path in _expected_all(home, "opencode"))


def test_main_skill_stays_short_and_points_at_the_guide() -> None:
    document = skill_document()
    assert document.count("\n") + 1 <= MAX_MAIN_SKILL_LINES
    assert GUIDE not in document
    for name in (SKILL_SETUP_NAME, SKILL_PLANNING_NAME, SKILL_MODES_NAME):
        assert f"`{name}`" in document, name


def test_each_skill_document_is_bounded() -> None:
    for name in SKILL_NAMES:
        document = skill_document(name)
        assert document.startswith(f"---\nname: {name}\ndescription: ")
        assert SKILL_MARKER in document
        # Every skill is a sheet the host loads on demand, not the whole guide.
        assert GUIDE not in document, name
        assert len(document.splitlines()) <= MAX_MAIN_SKILL_LINES, name


def test_planning_skill_carries_the_worker_choice_guidance() -> None:
    document = " ".join(skill_document(SKILL_PLANNING_NAME).split())
    for phrase in (
        "## Choosing the worker",
        "Read-only exploration",
        "never accepted as finished",
        "saberops-worker",
        "claude-worker",
        "codex-worker",
        "opencode-go-worker",
        "scout job",
        "Claude cannot be an OpenCode subagent",
        "Connect other coding agents",
        "Antigravity is an orchestrator only",
        # Codex only delegates when the owner or the instructions ask (D13).
        "custom agents under `~/.codex/agents`",
        "spawn_agent",
        "agent_type",
        "do not quietly do it yourself",
    ):
        assert phrase in document, phrase


def test_skill_never_shells_the_owner_commands() -> None:
    """D17/D16: the skill uses the MCP tools and leaves `orch resume` to the owner."""
    document = skill_document()
    for phrase in (
        "never a shell `orch mcp on`",
        "Call `saberops_on`",
        "Tell the owner to run `orch resume`",
    ):
        assert phrase in document, phrase
    assert "`resume_work`" not in document


def test_skill_matches_the_live_mcp_tool_list() -> None:
    document = skill_document()
    switch_tools = {"saberops_on", "saberops_off"}
    # Every tool the skill tells the planner to call must exist on the server.
    for required in (
        "saberops_on",
        "saberops_off",
        "preflight",
        "submit_job",
        "wait_job",
        "get_job",
        "list_jobs",
        "read_evidence",
    ):
        assert f"`{required}`" in document, required
        assert required in TOOL_NAMES or required in switch_tools, required
    # C5 result fields the skill tells the planner to present.
    for field in ("run_id", "candidate_sha", "diff", "owner_action"):
        assert f"`{field}`" in document, field
    # No stale tool name is taught in the loop section.
    loop = document.split("## Load the matching skill", 1)[0]
    mentioned = set(re.findall(r"`([a-z][a-z0-9_]+)`", loop))
    known = set(TOOL_NAMES) | switch_tools | {
        "repo",
        "goal",
        "acceptance",
        "idempotency_key",
        "job_id",
        "run_id",
        "candidate_sha",
        "diff",
        "owner_action",
        "difficulty",
        # Host-CLI delegation vocabulary, not SaberOps tools (D13).
        "spawn_agent",
        "agent_type",
    }
    assert mentioned - known == set()
