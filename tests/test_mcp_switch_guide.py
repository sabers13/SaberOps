"""The owner's MCP on/off switch and the built-in agent guide.

Hermetic: every path is temp state; no agent CLI, model or network is used.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from saberops.mcp.evidence import EvidenceRoots
from saberops.mcp.guide import GUIDE, GUIDE_PROMPT, GUIDE_URI, PAUSED_INSTRUCTIONS
from saberops.mcp.lease import OrchestratorLease
from saberops.mcp.ledger import EventLedger
from saberops.mcp.protocol import McpServer
from saberops.mcp.switch import mcp_enabled, set_mcp_enabled
from saberops.mcp.tools import ServerContext


def _server(tmp_path: Path) -> McpServer:
    orch = tmp_path / ".orch"
    orch.mkdir(parents=True, exist_ok=True)
    context = ServerContext(
        db_path=tmp_path / "mcp.db",
        questions_store=tmp_path / "questions.json",
        config_ledger_path=tmp_path / "config.jsonl",
        evidence=EvidenceRoots(roots=(orch,)),
        ledger=EventLedger(tmp_path / "events.jsonl"),
        lease=OrchestratorLease(tmp_path / "lease.json"),
    )
    return McpServer(context)


def _call(server: McpServer, method: str, params: dict[str, Any] | None = None) -> Any:
    response = server.handle({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
    assert response is not None
    return response


def _ready(server: McpServer, client: str = "claude-code") -> dict[str, Any]:
    init = _call(server, "initialize", {"clientInfo": {"name": client}})["result"]
    server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert isinstance(init, dict)
    return init


def test_switch_defaults_on_and_round_trips(tmp_path: Path) -> None:
    lease = tmp_path / "lease.json"
    assert mcp_enabled(lease) is True
    set_mcp_enabled(False, lease)
    assert mcp_enabled(lease) is False
    set_mcp_enabled(True, lease)
    assert mcp_enabled(lease) is True
    (tmp_path / "mcp-switch.json").write_text("not json", encoding="utf-8")
    assert mcp_enabled(lease) is True


def test_switched_off_server_offers_nothing_and_holds_no_lease(tmp_path: Path) -> None:
    set_mcp_enabled(False, tmp_path / "lease.json")
    server = _server(tmp_path)
    init = _ready(server)
    assert init["enabled"] is False
    assert init["instructions"] == PAUSED_INSTRUCTIONS
    assert _call(server, "tools/list")["result"] == {"tools": []}
    refused = _call(server, "tools/call", {"name": "list_jobs", "arguments": {}})["result"]
    assert refused["isError"] is True and "switched off" in refused["content"][0]["text"]
    assert OrchestratorLease(tmp_path / "lease.json").read() is None


def test_switch_flips_take_effect_mid_session(tmp_path: Path) -> None:
    server = _server(tmp_path)
    _ready(server)
    assert _call(server, "tools/list")["result"]["tools"]
    set_mcp_enabled(False, tmp_path / "lease.json")
    assert _call(server, "tools/list")["result"]["tools"] == []
    assert OrchestratorLease(tmp_path / "lease.json").read() is None  # planner role released
    set_mcp_enabled(True, tmp_path / "lease.json")
    assert _call(server, "tools/list")["result"]["tools"]
    record = OrchestratorLease(tmp_path / "lease.json").read()
    assert record is not None and record.client == "claude-code"


def test_guide_is_delivered_as_instructions_prompt_and_resource(tmp_path: Path) -> None:
    server = _server(tmp_path)
    init = _ready(server)
    assert init["enabled"] is True
    assert "submit_job" in init["instructions"] and GUIDE_PROMPT in init["instructions"]
    assert "prompts" in init["capabilities"]
    prompts = _call(server, "prompts/list")["result"]["prompts"]
    assert [p["name"] for p in prompts] == [GUIDE_PROMPT]
    got = _call(server, "prompts/get", {"name": GUIDE_PROMPT})["result"]
    assert got["messages"][0]["content"]["text"] == GUIDE
    assert "error" in _call(server, "prompts/get", {"name": "other"})
    listed = [r["uri"] for r in _call(server, "resources/list")["result"]["resources"]]
    assert GUIDE_URI in listed
    read = _call(server, "resources/read", {"uri": GUIDE_URI})["result"]
    assert read["contents"][0]["text"] == GUIDE


def test_guide_names_the_owner_only_boundaries() -> None:
    for phrase in ("Never send `provider`", "submit_wave", "acceptance", "propose_config_change"):
        assert phrase in GUIDE or phrase in GUIDE.replace("\n", " "), phrase
    for owner_only in ("accepting or rejecting changes", "merging, pushing"):
        assert owner_only in GUIDE.replace("\n  ", " "), owner_only


def _cli_server(tmp_path: Path) -> McpServer:
    """The way `orch mcp serve` builds it: the session starts off."""
    server = _server(tmp_path)
    return McpServer(server.context, start_active=False)


def test_agent_sessions_start_off_and_the_user_turns_them_on_and_off(tmp_path: Path) -> None:
    server = _cli_server(tmp_path)
    init = _ready(server)
    assert init["active"] is False and "OFF in this session" in init["instructions"]
    assert [t["name"] for t in _call(server, "tools/list")["result"]["tools"]] == ["saberops_on"]
    assert OrchestratorLease(tmp_path / "lease.json").read() is None  # not the planner
    refused = _call(server, "tools/call", {"name": "list_jobs", "arguments": {}})["result"]
    assert refused["isError"] and refused["content"][0]["text"].startswith("Orchestrator:")

    on = _call(server, "tools/call", {"name": "saberops_on", "arguments": {}})["result"]
    assert on["content"][0]["text"].startswith("Orchestrator: SaberOps is on.")
    assert server.drain_notifications() == [
        {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}
    ]
    names = [t["name"] for t in _call(server, "tools/list")["result"]["tools"]]
    assert "submit_job" in names and "saberops_off" in names and "saberops_on" not in names
    record = OrchestratorLease(tmp_path / "lease.json").read()
    assert record is not None and record.client == "claude-code"
    listed = _call(server, "tools/call", {"name": "list_jobs", "arguments": {}})["result"]
    assert listed["content"][0]["text"].startswith("Orchestrator: list_jobs result")

    off = _call(server, "tools/call", {"name": "saberops_off", "arguments": {}})["result"]
    assert off["content"][0]["text"].startswith("Orchestrator: SaberOps is off")
    assert server.drain_notifications()
    assert [t["name"] for t in _call(server, "tools/list")["result"]["tools"]] == ["saberops_on"]
    assert OrchestratorLease(tmp_path / "lease.json").read() is None


def test_owner_master_switch_hides_even_the_turn_on_tool(tmp_path: Path) -> None:
    set_mcp_enabled(False, tmp_path / "lease.json")
    server = _cli_server(tmp_path)
    _ready(server)
    assert _call(server, "tools/list")["result"]["tools"] == []
    on = _call(server, "tools/call", {"name": "saberops_on", "arguments": {}})["result"]
    assert on["isError"] and "switched off by the owner" in on["content"][0]["text"]


def test_stdio_writes_the_tools_changed_notification(tmp_path: Path) -> None:
    import io
    import json

    from saberops.mcp.stdio import serve_stdio

    server = _cli_server(tmp_path)
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "saberops_on"}},
    ]
    out = io.StringIO()
    serve_stdio(server, stdin=[json.dumps(m) + "\n" for m in lines], stdout=out)
    messages = [json.loads(line) for line in out.getvalue().splitlines()]
    assert messages[-1] == {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}
