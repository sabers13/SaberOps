"""CLI integration for ``orch mcp`` (Step 014 / M3).

Covers the CLI surface: ``orch mcp tools`` lists the D7 tools, ``orch mcp`` and
``orch mcp serve`` (and ``--transport stdio``) launch the stdio server, the
parse-level contract rejects an unknown transport, and a second client over the
CLI is recognised as read-only.
"""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

import pytest

from saberops.cli import main
from saberops.mcp.lease import OrchestratorLease
from saberops.mcp.tools import FORBIDDEN_OPERATIONS, TOOL_NAMES


def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
    """Return the CLI path options that keep every file inside ``tmp_path``."""
    monkeypatch.delenv("ORCH_DIR", raising=False)
    monkeypatch.delenv("SABEROPS_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    return {
        "--db": str(tmp_path / "mcp.db"),
        "--lease": str(tmp_path / "lease.json"),
        "--ledger": str(tmp_path / "events.jsonl"),
        "--questions-store": str(tmp_path / "questions.json"),
        "--config-ledger": str(tmp_path / "config.jsonl"),
        "--cwd": str(tmp_path),
    }


def _argv(options: dict[str, str], *parts: str) -> list[str]:
    argv = ["mcp", *parts]
    for key, value in options.items():
        argv += [key, value]
    return argv


def test_cli_mcp_tools_json_lists_exactly_the_d7_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Covers: prompt section 'add orch mcp CLI subcommand' (tools)."""
    options = _isolate(monkeypatch, tmp_path)
    assert main(_argv(options, "tools", "--json")) == 0
    payload = json.loads(capsys.readouterr().out)
    names = [tool["name"] for tool in payload["tools"]]
    assert sorted(names) == sorted(TOOL_NAMES)


def test_cli_mcp_tools_never_lists_a_forbidden_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Covers: ADR-0004 D7 ('Never exposed' stays invisible)."""
    options = _isolate(monkeypatch, tmp_path)
    assert main(_argv(options, "tools", "--json")) == 0
    text = capsys.readouterr().out
    for forbidden in FORBIDDEN_OPERATIONS:
        assert f'"{forbidden}"' not in text


def test_cli_mcp_tools_human_output_lists_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Covers: prompt section 'orch mcp CLI subcommand' (human output)."""
    options = _isolate(monkeypatch, tmp_path)
    assert main(_argv(options, "tools")) == 0
    out = capsys.readouterr().out
    for name in ("submit_job", "read_evidence", "answer_question"):
        assert name in out


def test_cli_mcp_tools_does_not_acquire_the_writer_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Covers: single-writer lease (inspection never steals the writer role)."""
    options = _isolate(monkeypatch, tmp_path)
    lease_path = Path(options["--lease"])
    OrchestratorLease(lease_path, holder="primary", pid=1).acquire()
    before = lease_path.read_text(encoding="utf-8")
    assert main(_argv(options, "tools", "--json")) == 0
    capsys.readouterr()
    assert lease_path.read_text(encoding="utf-8") == before


def _stdin_payload(*lines: dict[str, object]) -> str:
    return "".join(json.dumps(line) + "\n" for line in lines)


def _serve(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, options: dict[str, str], payload: str
) -> list[dict[str, object]]:
    """Run `orch mcp serve` against a captured stdin/stdout pair."""
    captured = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    monkeypatch.setattr(sys, "stdout", captured)
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    assert main(_argv(options, "serve")) == 0
    return [json.loads(line) for line in captured.getvalue().splitlines() if line]


def test_cli_mcp_serve_roundtrip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: prompt section 'launch the MCP server' (serve round trip)."""
    options = _isolate(monkeypatch, tmp_path)
    responses = _serve(
        monkeypatch,
        tmp_path,
        options,
        _stdin_payload(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ),
    )
    assert [response["id"] for response in responses] == [1, 2]
    names = [tool["name"] for tool in responses[1]["result"]["tools"]]  # type: ignore[index]
    assert sorted(names) == sorted(TOOL_NAMES)


def test_cli_mcp_default_action_serves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: prompt section 'orch mcp --transport stdio' (default serve)."""
    options = _isolate(monkeypatch, tmp_path)
    captured = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(_stdin_payload(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}},
    )))
    monkeypatch.setattr(sys, "stdout", captured)
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    argv = ["mcp", "--transport", "stdio"]
    for key, value in options.items():
        argv += [key, value]
    assert main(argv) == 0
    responses = [json.loads(line) for line in captured.getvalue().splitlines() if line]
    assert responses[1]["result"] == {}


def test_cli_mcp_rejects_an_unknown_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: prompt section 'transport stdio' (only stdio is implemented)."""
    _isolate(monkeypatch, tmp_path)
    with pytest.raises(SystemExit) as excinfo:
        main(["mcp", "serve", "--transport", "http"])
    assert excinfo.value.code == 2


def test_cli_mcp_serve_is_read_only_for_a_second_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: prompt section 'read-only second client' (over the CLI)."""
    options = _isolate(monkeypatch, tmp_path)
    OrchestratorLease(Path(options["--lease"]), holder="primary", pid=1).acquire()
    responses = _serve(
        monkeypatch,
        tmp_path,
        options,
        _stdin_payload(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "submit_job", "arguments": {"repo": "/tmp", "goal": "g"}},
            },
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "list_jobs", "arguments": {}},
            },
        ),
    )
    assert responses[0]["result"]["readOnly"] is True  # type: ignore[index]
    assert responses[2]["error"]["code"] == -32020  # type: ignore[index]
    assert responses[3]["result"]["isError"] is False  # type: ignore[index]


def test_cli_mcp_never_opens_a_credential_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: ADR-0004 D7 (no MCP subcommand reaches credentials)."""
    options = _isolate(monkeypatch, tmp_path)
    captured = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    monkeypatch.setattr(sys, "stdout", captured)
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    assert main(_argv(options, "serve")) == 0
    assert captured.getvalue() == ""
    assert os.environ.get("XDG_STATE_HOME") == str(tmp_path / "state")
