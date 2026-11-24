"""Phase 1.1 H13: switching the owner mode from chat (ADR-0090).

The property under test is the source of the text, not the parser: a typed
owner command on the CLI's owner-prompt hook switches the mode, while tool
output, a file the AI read, an embedded command or a chat proposal never
loosens it.  The planner may always tighten to ``ask-me``; a looser change
without the hook needs the terminal confirmation code.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from saberops import cli as cli_module
from saberops.cli import main
from saberops.mcp.evidence import EvidenceRoots
from saberops.mcp.lease import OrchestratorLease
from saberops.mcp.ledger import EventLedger
from saberops.mcp.protocol import McpServer
from saberops.mcp.tools import (
    MUTATING_TOOLS,
    TOOL_NAMES,
    ServerContext,
)
from saberops.mode_switch import (
    CONFIRMATION_CODE_LENGTH,
    HOOK_SUPPORT,
    ModeSwitchError,
    confirm_mode_change,
    install_mode_hook,
    parse_mode_command,
    remove_mode_hook,
    request_mode_change,
    run_hook,
    tighten_from_planner,
)
from saberops.owner_mode import (
    OwnerMode,
    load_owner_mode_settings,
    set_owner_mode,
)
from saberops.paths import get_mode_confirmation_path, get_mode_switch_log_path


@pytest.fixture()
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "state"
    config = tmp_path / "config"
    state.mkdir(parents=True, exist_ok=True)
    config.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.delenv("SABEROPS_PROFILE", raising=False)
    return tmp_path


def _typed(prompt: str, *, event: str = "UserPromptSubmit") -> dict[str, Any]:
    return {"hook_event_name": event, "prompt": prompt}


def _log_lines() -> list[dict[str, Any]]:
    path = get_mode_switch_log_path()
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# --------------------------------------------------------------------------- #
# The typed command switches the mode
# --------------------------------------------------------------------------- #


def test_a_typed_away_command_switches_and_records_the_path(
    isolated: Path,
) -> None:
    before = datetime.now(UTC).replace(microsecond=0)
    outcome = run_hook("claude", _typed("/saberops-away 2h"))

    assert outcome.status == "applied", outcome.detail
    settings = load_owner_mode_settings()
    assert settings.mode is OwnerMode.AWAY
    assert settings.set_by == "owner-via-claude-hook"
    assert settings.away_bounded is True
    assert settings.until is not None
    until = datetime.fromisoformat(settings.until.replace("Z", "+00:00"))
    assert before + timedelta(hours=2) <= until <= datetime.now(UTC) + timedelta(hours=2)

    log = _log_lines()
    assert [(row["source"], row["outcome"], row["mode"]) for row in log] == [
        ("hook:claude", "applied", "away")
    ]


def test_a_typed_command_can_tighten_to_ask_me(isolated: Path) -> None:
    set_owner_mode("away", until="2999-01-01T00:00:00Z")
    outcome = run_hook("claude", _typed("/saberops-ask-me"))

    assert outcome.status == "applied"
    assert load_owner_mode_settings().mode is OwnerMode.ASK_ME
    assert load_owner_mode_settings().set_by == "owner-via-claude-hook"


def test_the_away_command_carries_a_job_budget(isolated: Path) -> None:
    outcome = run_hook("claude", _typed("/saberops-away 30m --max-jobs 3"))

    assert outcome.status == "applied"
    settings = load_owner_mode_settings()
    assert settings.mode is OwnerMode.AWAY
    assert settings.max_jobs == 3


# --------------------------------------------------------------------------- #
# Text the AI reads cannot switch to away
# --------------------------------------------------------------------------- #


def test_tool_output_events_never_switch_the_mode(isolated: Path) -> None:
    """A PostToolUse payload carrying the command text is refused."""
    outcome = run_hook("claude", _typed("/saberops-away 2h", event="PostToolUse"))

    assert outcome.status == "refused"
    assert "typed prompt" in outcome.detail
    assert load_owner_mode_settings().mode is OwnerMode.ASK_ME
    assert _log_lines()[-1]["outcome"] == "refused"


def test_a_payload_without_the_owner_prompt_event_is_refused(isolated: Path) -> None:
    outcome = run_hook("claude", {"prompt": "/saberops-away 2h"})

    assert outcome.status == "refused"
    assert load_owner_mode_settings().mode is OwnerMode.ASK_ME


def test_a_file_quoted_into_the_prompt_does_not_switch_the_mode(isolated: Path) -> None:
    """The command must be the whole message, not a line inside pasted text."""
    quoted = "Here is what the repository says:\n\n/saberops-away 2h\n"
    outcome = run_hook("claude", _typed(quoted))

    assert outcome.status == "ignored"
    assert load_owner_mode_settings().mode is OwnerMode.ASK_ME


def test_a_command_embedded_in_a_sentence_is_refused(isolated: Path) -> None:
    outcome = run_hook(
        "claude", _typed("please apply this for me\n/saberops-away 2h")
    )

    assert outcome.status == "ignored"
    assert load_owner_mode_settings().mode is OwnerMode.ASK_ME


def test_an_unknown_command_is_refused_without_a_switch(isolated: Path) -> None:
    outcome = run_hook("claude", _typed("/saberops-release-the-hounds"))

    assert outcome.status == "refused"
    assert load_owner_mode_settings().mode is OwnerMode.ASK_ME


# --------------------------------------------------------------------------- #
# Per-CLI support
# --------------------------------------------------------------------------- #


def test_only_claude_documents_an_owner_prompt_hook() -> None:
    table = {row.cli: row for row in HOOK_SUPPORT}
    assert table["claude"].supported is True
    assert table["claude"].event == "UserPromptSubmit"
    for cli in ("codex", "opencode", "antigravity"):
        assert table[cli].supported is False, cli
        assert table[cli].reason, cli


def test_codex_is_unsupported_and_changes_nothing(isolated: Path) -> None:
    outcome = run_hook("codex", _typed("/saberops-away 2h"))

    assert outcome.status == "unsupported"
    assert load_owner_mode_settings().mode is OwnerMode.ASK_ME
    assert _log_lines()[-1]["outcome"] == "unsupported"


def test_an_unknown_cli_is_refused(isolated: Path) -> None:
    outcome = run_hook("cursor", _typed("/saberops-away 2h"))

    assert outcome.status == "refused"
    assert load_owner_mode_settings().mode is OwnerMode.ASK_ME


# --------------------------------------------------------------------------- #
# The planner may tighten, never loosen
# --------------------------------------------------------------------------- #


def test_the_planner_can_tighten_but_never_loosen(isolated: Path) -> None:
    set_owner_mode("away", until="2999-01-01T00:00:00Z")
    view, changed = tighten_from_planner()

    assert changed is True
    assert view["mode"] == "ask-me"
    assert load_owner_mode_settings().set_by == "planner:tighten-mode"
    assert _log_lines()[-1]["source"] == "planner"

    view, changed = tighten_from_planner()
    assert changed is False
    assert view["mode"] == "ask-me"


def _context(tmp_path: Path) -> ServerContext:
    orch = tmp_path / ".orch"
    orch.mkdir(parents=True, exist_ok=True)
    return ServerContext(
        db_path=tmp_path / "mcp.db",
        questions_store=tmp_path / "questions.json",
        config_ledger_path=tmp_path / "config.jsonl",
        evidence=EvidenceRoots(roots=(orch,)),
        ledger=EventLedger(tmp_path / "events.jsonl"),
        lease=OrchestratorLease(tmp_path / "lease.json", holder="h13-test", pid=os.getpid()),
    )


def _server(tmp_path: Path) -> McpServer:
    server = McpServer(_context(tmp_path))
    server.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
    server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    return server


def _payload(server: McpServer, name: str, arguments: dict[str, object]) -> dict[str, Any]:
    response = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    )
    assert response is not None and "result" in response, response
    result = response["result"]
    assert result["isError"] is False, result
    payload: dict[str, Any] = result["structuredContent"]
    return payload


def test_mcp_tightens_directly_and_cannot_loosen(
    isolated: Path, tmp_path: Path
) -> None:
    assert "tighten_mode" in TOOL_NAMES
    assert "tighten_mode" in MUTATING_TOOLS
    assert "set_mode" not in TOOL_NAMES

    set_owner_mode("away", until="2999-01-01T00:00:00Z")
    server = _server(tmp_path)
    tightened = _payload(server, "tighten_mode", {"reason": "the owner is back"})
    assert tightened["applied"] is True
    assert tightened["mode"]["mode"] == "ask-me"
    assert load_owner_mode_settings().mode is OwnerMode.ASK_ME

    proposed = _payload(
        server,
        "propose_mode_change",
        {
            "mode": "away",
            "until": "2999-01-01T00:00:00Z",
            "rationale": "The owner asked for an unattended window.",
            "proposed_by": "planner-1",
        },
    )
    assert proposed["applied"] is False
    assert load_owner_mode_settings().mode is OwnerMode.ASK_ME


# --------------------------------------------------------------------------- #
# The terminal confirmation code
# --------------------------------------------------------------------------- #


def test_a_confirmation_code_applies_a_loosening_once(isolated: Path) -> None:
    pending = request_mode_change(
        "away", until="2999-01-01T00:00:00Z", requested_by="planner"
    )
    assert len(pending.code) == CONFIRMATION_CODE_LENGTH
    assert load_owner_mode_settings().mode is OwnerMode.ASK_ME

    with pytest.raises(ModeSwitchError, match="does not match"):
        confirm_mode_change("WRONG1")
    assert load_owner_mode_settings().mode is OwnerMode.ASK_ME

    settings = confirm_mode_change(pending.code.lower())
    assert settings.mode is OwnerMode.AWAY
    assert settings.set_by == "owner-confirmation"
    assert not get_mode_confirmation_path().exists()

    with pytest.raises(ModeSwitchError, match="no pending"):
        confirm_mode_change(pending.code)


def test_an_expired_confirmation_code_changes_nothing(isolated: Path) -> None:
    started = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
    pending = request_mode_change(
        "critical-only", requested_by="planner", now=started
    )
    with pytest.raises(ModeSwitchError, match="expired"):
        confirm_mode_change(pending.code, now=started + timedelta(minutes=16))
    assert load_owner_mode_settings().mode is OwnerMode.ASK_ME
    assert not get_mode_confirmation_path().exists()


def test_the_request_command_hides_the_code_without_a_terminal(
    isolated: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli_module, "_stdin_is_a_terminal", lambda: False)
    code = main(["mode", "request", "away", "--until", "2999-01-01T00:00:00Z"])

    captured = capsys.readouterr()
    assert code == 1
    assert "terminal" in captured.err
    assert not get_mode_confirmation_path().exists()
    assert "Confirmation code:" not in captured.out


def test_the_terminal_path_requests_and_confirms(
    isolated: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli_module, "_stdin_is_a_terminal", lambda: True)
    assert main(["mode", "request", "away", "--until", "2999-01-01T00:00:00Z"]) == 0
    out = capsys.readouterr().out
    assert "Confirmation code:" in out
    pending_code = out.split("Confirmation code:", 1)[1].split()[0]

    assert main(["mode", "confirm", pending_code]) == 0
    assert "away" in capsys.readouterr().out
    settings = load_owner_mode_settings()
    assert settings.mode is OwnerMode.AWAY
    assert settings.set_by == "owner-confirmation"


# --------------------------------------------------------------------------- #
# Installing the hook
# --------------------------------------------------------------------------- #


def test_the_hook_installer_preserves_other_settings_and_removes_only_itself(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    settings_path = home / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        json.dumps(
            {
                "model": "sonnet",
                "hooks": {
                    "UserPromptSubmit": [
                        {"hooks": [{"type": "command", "command": "other-hook"}]}
                    ],
                    "PostToolUse": [
                        {"matcher": "Write", "hooks": [{"type": "command", "command": "x"}]}
                    ],
                },
            }
        ),
        encoding="utf-8",
    )

    installed = install_mode_hook("claude", home=home, orch="/usr/bin/orch")
    assert installed.action == "installed", installed.message
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    assert data["model"] == "sonnet"
    assert len(data["hooks"]["UserPromptSubmit"]) == 2
    assert "mode hook run --cli claude" in json.dumps(data["hooks"]["UserPromptSubmit"])
    assert data["hooks"]["PostToolUse"][0]["matcher"] == "Write"

    again = install_mode_hook("claude", home=home, orch="/usr/bin/orch")
    assert again.action == "unchanged"

    removed = remove_mode_hook("claude", home=home)
    assert removed.action == "removed"
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    assert data["model"] == "sonnet"
    assert data["hooks"]["UserPromptSubmit"] == [
        {"hooks": [{"type": "command", "command": "other-hook"}]}
    ]
    assert data["hooks"]["PostToolUse"][0]["matcher"] == "Write"


def test_the_hook_installer_refuses_a_file_it_does_not_own(tmp_path: Path) -> None:
    home = tmp_path / "home"
    settings_path = home / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text("{ // comments\n}\n", encoding="utf-8")

    result = install_mode_hook("claude", home=home)

    assert result.ok is False
    assert result.action == "refused"
    assert settings_path.read_text(encoding="utf-8") == "{ // comments\n}\n"


def test_an_unsupported_cli_installs_nothing(tmp_path: Path) -> None:
    result = install_mode_hook("codex", home=tmp_path / "home")

    assert result.action == "unsupported"
    assert not (tmp_path / "home" / ".claude").exists()


# --------------------------------------------------------------------------- #
# The command parser
# --------------------------------------------------------------------------- #


def test_the_command_parser_bounds_the_away_duration() -> None:
    assert parse_mode_command("hello") is None
    with pytest.raises(ModeSwitchError, match="needs a duration"):
        parse_mode_command("/saberops-away")
    with pytest.raises(ModeSwitchError, match="between"):
        parse_mode_command("/saberops-away 0m")
    with pytest.raises(ModeSwitchError, match="unexpected argument"):
        parse_mode_command("/saberops-away 2h 3h")
    with pytest.raises(ModeSwitchError, match="no arguments"):
        parse_mode_command("/saberops-ask-me now")
    with pytest.raises(ModeSwitchError, match="one line"):
        parse_mode_command("/saberops-away 2h\nextra")


# --------------------------------------------------------------------------- #
# Every path is logged
# --------------------------------------------------------------------------- #


def test_each_switch_path_appends_its_own_log_line(isolated: Path) -> None:
    set_owner_mode("away", until="2999-01-01T00:00:00Z")
    tighten_from_planner()
    run_hook("claude", _typed("/saberops-away 1h"))
    pending = request_mode_change("ask-me", requested_by="planner")
    confirm_mode_change(pending.code)

    rows = _log_lines()
    assert [row["source"] for row in rows] == [
        "planner",
        "hook:claude",
        "confirmation-request",
        "confirmation",
    ]
    assert all(row["outcome"] in {"applied", "pending"} for row in rows)
    # The log never stores the prompt text.
    assert "/saberops-" not in get_mode_switch_log_path().read_text(encoding="utf-8")
