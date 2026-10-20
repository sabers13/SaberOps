"""MCP server unit/integration tests: transport, lease, ledger, redaction (M3).

Covers the Step 014 server from the outside: the JSON-RPC 2.0 lifecycle, the
``v: 1`` read snapshot, the append-only event ledger, the single-writer
Orchestrator lease with read-only second clients and stale self-healing, the
read-boundary secret redaction and the evidence path guard.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from saberops.mcp.evidence import EvidenceRoots
from saberops.mcp.lease import OrchestratorLease, pid_is_alive
from saberops.mcp.ledger import (
    SNAPSHOT_SCHEMA_VERSION,
    EventLedger,
    LedgerConflictError,
    LedgerCorruptError,
)
from saberops.mcp.protocol import (
    PROTOCOL_VERSION,
    SERVER_NAME,
    SERVER_VERSION,
    McpServer,
)
from saberops.mcp.redaction import REDACTED, redact_secrets
from saberops.mcp.stdio import serve_stdio
from saberops.mcp.tools import TOOL_NAMES, ServerContext


def _context(tmp_path: Path, *, lease: OrchestratorLease | None = None) -> ServerContext:
    """A context whose every path is disposable temp state."""
    orch = tmp_path / ".orch"
    orch.mkdir(parents=True, exist_ok=True)
    return ServerContext(
        db_path=tmp_path / "mcp.db",
        questions_store=tmp_path / "questions.json",
        config_ledger_path=tmp_path / "config.jsonl",
        evidence=EvidenceRoots(roots=(orch,)),
        ledger=EventLedger(tmp_path / "events.jsonl"),
        lease=lease if lease is not None else OrchestratorLease(tmp_path / "lease.json"),
    )


def _server(tmp_path: Path, *, lease: OrchestratorLease | None = None) -> McpServer:
    return McpServer(_context(tmp_path, lease=lease))


def _ready(server: McpServer) -> None:
    server.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
    server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})


def _call(server: McpServer, method: str, params: dict[str, object] | None = None,
          mid: int = 1) -> dict[str, Any]:
    response = server.handle(
        {"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}}
    )
    assert response is not None
    return response


def _tool(server: McpServer, name: str, arguments: dict[str, object]) -> dict[str, Any]:
    response = _call(server, "tools/call", {"name": name, "arguments": arguments})
    assert "result" in response, response
    result: dict[str, Any] = response["result"]
    return result


def _dead_pid() -> int:
    """Return a pid that is certainly not alive (a reaped child)."""
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #


def test_initialize_advertises_saberops_and_the_d7_capabilities(tmp_path: Path) -> None:
    """Covers: prompt section 'Protocol Transport & Handshake' (initialize)."""
    server = _server(tmp_path)
    result = _call(server, "initialize", {"protocolVersion": "2025-06-18"})["result"]
    assert isinstance(result, dict)
    assert result["serverInfo"]["name"] == SERVER_NAME == "saberops"
    assert result["serverInfo"]["version"] == SERVER_VERSION == "0.5.0"
    assert result["protocolVersion"] == "2025-06-18"
    assert set(result["capabilities"]) == {"tools", "resources", "prompts"}
    assert result["snapshotSchemaVersion"] == SNAPSHOT_SCHEMA_VERSION == 1
    assert result["readOnly"] is False


@pytest.mark.parametrize("requested", ["2025-03-26", "2024-11-05"])
def test_initialize_accepts_every_supported_revision(tmp_path: Path, requested: str) -> None:
    """Covers: prompt section 'declares protocol version' (negotiation)."""
    server = _server(tmp_path)
    result = _call(server, "initialize", {"protocolVersion": requested})["result"]
    assert result["protocolVersion"] == requested


def test_initialize_falls_back_for_an_unknown_revision(tmp_path: Path) -> None:
    """Covers: prompt section 'declares protocol version' (fail-safe default)."""
    server = _server(tmp_path)
    result = _call(server, "initialize", {"protocolVersion": "1999-01-01"})["result"]
    assert result["protocolVersion"] == PROTOCOL_VERSION


def test_notifications_initialized_produces_no_response(tmp_path: Path) -> None:
    """Covers: prompt section 'notifications/initialized' (no id, no reply)."""
    server = _server(tmp_path)
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert server.initialized is True


@pytest.mark.parametrize("method", ["tools/list", "tools/call", "resources/list", "resources/read"])
def test_reads_require_the_handshake(tmp_path: Path, method: str) -> None:
    """Covers: prompt section 'Standard MCP lifecycle methods' (ordering)."""
    server = _server(tmp_path)
    response = _call(server, method, {"uri": "saberops://lease"})
    assert response["error"]["code"] == -32600


def test_ping_works_without_initialization(tmp_path: Path) -> None:
    """Covers: prompt section 'ping (healthcheck)'."""
    server = _server(tmp_path)
    assert _call(server, "ping")["result"] == {}


def test_tools_list_exposes_exactly_the_twelve_d7_tools(tmp_path: Path) -> None:
    """Covers: ADR-0004 D7 tool set (no more, no fewer)."""
    server = _server(tmp_path)
    _ready(server)
    result = _call(server, "tools/list")["result"]
    names = [tool["name"] for tool in result["tools"]]
    # The twelve D7 tools plus the user's in-agent switch to turn SaberOps off.
    assert sorted(n for n in names if n != "saberops_off") == sorted(TOOL_NAMES)
    assert len(TOOL_NAMES) == 12 and "saberops_off" in names
    for tool in result["tools"]:
        assert tool["inputSchema"]["type"] == "object"


def test_unknown_method_is_method_not_found(tmp_path: Path) -> None:
    """Covers: JSON-RPC 2.0 (unknown method -> -32601)."""
    server = _server(tmp_path)
    response = _call(server, "server/unknown")
    assert response["error"]["code"] == -32601


def test_bad_jsonrpc_version_is_invalid_request(tmp_path: Path) -> None:
    """Covers: JSON-RPC 2.0 (envelope validation)."""
    server = _server(tmp_path)
    response = server.handle({"jsonrpc": "1.0", "id": 4, "method": "ping"})
    assert response is not None
    assert response["error"]["code"] == -32600


def test_non_object_message_is_a_parse_error(tmp_path: Path) -> None:
    """Covers: JSON-RPC 2.0 (parse error -> -32700)."""
    server = _server(tmp_path)
    response = server.handle(["not", "a", "request"])
    assert response is not None
    assert response["error"]["code"] == -32700


def test_tool_failure_never_leaks_a_traceback(tmp_path: Path) -> None:
    """Covers: prompt section 'no credentials/tracebacks on the wire'."""
    server = _server(tmp_path)
    _ready(server)
    result = _tool(server, "read_evidence", {"path": "does-not-exist.md"})
    assert result["isError"] is True
    error = result["structuredContent"]["error"]
    assert error["code"] == -32030
    assert str(tmp_path) not in error["message"]


# --------------------------------------------------------------------------- #
# Snapshot + resources
# --------------------------------------------------------------------------- #


def test_snapshot_resource_is_versioned_v1(tmp_path: Path) -> None:
    """Covers: prompt section 'Versioned Read Snapshot (v:1)'."""
    server = _server(tmp_path)
    _ready(server)
    response = _call(server, "resources/read", {"uri": "saberops://snapshot"})
    payload = json.loads(response["result"]["contents"][0]["text"])
    assert payload["v"] == 1
    assert set(payload) >= {"lease", "ledger", "list_jobs", "capacity", "list_questions"}


def test_resources_list_includes_synthetic_and_evidence(tmp_path: Path) -> None:
    """Covers: prompt section 'resources/list (logs, receipts, evidence)'."""
    orch = tmp_path / ".orch"
    orch.mkdir(parents=True, exist_ok=True)
    (orch / "report.md").write_text("lane report\n", encoding="utf-8")
    server = _server(tmp_path)
    _ready(server)
    response = _call(server, "resources/list")
    uris = [item["uri"] for item in response["result"]["resources"]]
    assert "saberops://snapshot" in uris
    assert "saberops://lease" in uris
    assert "saberops://ledger" in uris
    assert (orch / "report.md").as_uri() in uris


def test_read_evidence_reads_an_allowed_file(tmp_path: Path) -> None:
    """Covers: prompt section 'read_evidence' (allowed evidence file)."""
    orch = tmp_path / ".orch"
    orch.mkdir(parents=True, exist_ok=True)
    (orch / "gate.txt").write_text("OUTCOME: PASS\n", encoding="utf-8")
    server = _server(tmp_path)
    _ready(server)
    result = _tool(server, "read_evidence", {"path": "gate.txt"})
    assert result["isError"] is False
    payload = result["structuredContent"]
    assert payload["content"] == "OUTCOME: PASS\n"
    assert payload["truncated"] is False


@pytest.mark.parametrize(
    "attempt",
    [
        "../outside.md",
        "../../etc/passwd",
        "/etc/passwd",
        "sub/../../outside.md",
        "..",
    ],
)
def test_read_evidence_refuses_path_traversal(tmp_path: Path, attempt: str) -> None:
    """Covers: prompt section 'Path traversal must be strictly prevented'."""
    server = _server(tmp_path)
    _ready(server)
    result = _tool(server, "read_evidence", {"path": attempt})
    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == -32030


def test_read_evidence_refuses_a_disallowed_filename(tmp_path: Path) -> None:
    """Covers: prompt section 'reads allowed evidence files' (allowlist)."""
    orch = tmp_path / ".orch"
    orch.mkdir(parents=True, exist_ok=True)
    (orch / "secrets.env").write_text("TOKEN=x\n", encoding="utf-8")
    server = _server(tmp_path)
    _ready(server)
    result = _tool(server, "read_evidence", {"path": "secrets.env"})
    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == -32030


def test_read_evidence_refuses_a_symlink_that_escapes_the_root(tmp_path: Path) -> None:
    """Covers: prompt section 'Path traversal' (symlink escape)."""
    orch = tmp_path / ".orch"
    orch.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside.md"
    outside.write_text("secret\n", encoding="utf-8")
    (orch / "report.md").symlink_to(outside)
    server = _server(tmp_path)
    _ready(server)
    result = _tool(server, "read_evidence", {"path": "report.md"})
    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == -32030


def test_read_evidence_refuses_an_absolute_path_under_a_root_prefix(tmp_path: Path) -> None:
    """Covers: prompt section 'Path traversal' (prefix lookalike dir)."""
    orch = tmp_path / ".orch"
    orch.mkdir(parents=True, exist_ok=True)
    lookalike = tmp_path / ".orch-evil"
    lookalike.mkdir()
    (lookalike / "report.md").write_text("nope\n", encoding="utf-8")
    server = _server(tmp_path)
    _ready(server)
    result = _tool(server, "read_evidence", {"path": str(lookalike / "report.md")})
    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == -32030


# --------------------------------------------------------------------------- #
# Ledger
# --------------------------------------------------------------------------- #


def test_ledger_append_is_idempotent_and_replays_deterministically(tmp_path: Path) -> None:
    """Covers: prompt section 'Append-only event ledger with idempotent replay'."""
    ledger = EventLedger(tmp_path / "events.jsonl")
    first = ledger.append("wave_submitted", {"wave_id": "w1", "n": 1}, key="wave:w1")
    again = ledger.append("wave_submitted", {"wave_id": "w1", "n": 1}, key="wave:w1")
    assert first.digest == again.digest
    assert len(ledger.events()) == 1
    state_a = ledger.replay()
    state_b = EventLedger(tmp_path / "events.jsonl").replay()
    assert state_a == state_b
    assert state_a.waves["w1"]["wave_id"] == "w1"
    assert state_a.last_seq == 1


def test_ledger_conflicts_on_a_reused_key_with_a_different_payload(tmp_path: Path) -> None:
    """Covers: prompt section 'idempotent' (same key, different payload refuses)."""
    ledger = EventLedger(tmp_path / "events.jsonl")
    ledger.append("job_submitted", {"job_id": "j1", "state": "QUEUED"}, key="job:j1")
    with pytest.raises(LedgerConflictError):
        ledger.append("job_submitted", {"job_id": "j1", "state": "FAILED"}, key="job:j1")


def test_ledger_hash_chain_detects_tampering(tmp_path: Path) -> None:
    """Covers: invariant 'engine evidence is never silently rewritten'."""
    path = tmp_path / "events.jsonl"
    ledger = EventLedger(path)
    ledger.append("job_submitted", {"job_id": "j1", "state": "QUEUED"}, key="job:j1")
    tampered = path.read_text(encoding="utf-8").replace("QUEUED", "FAILED")
    path.write_text(tampered, encoding="utf-8")
    with pytest.raises(LedgerCorruptError):
        ledger.events()


def test_ledger_snapshot_is_versioned(tmp_path: Path) -> None:
    """Covers: prompt section 'Versioned Read Snapshot (v:1)'."""
    snapshot = EventLedger(tmp_path / "events.jsonl").snapshot()
    assert snapshot["v"] == 1
    assert snapshot["last_seq"] == 0


# --------------------------------------------------------------------------- #
# Lease
# --------------------------------------------------------------------------- #


def test_first_client_is_writer_and_second_is_read_only(tmp_path: Path) -> None:
    """Covers: prompt section 'Single-Writer Orchestrator Lease'."""
    path = tmp_path / "lease.json"
    primary = OrchestratorLease(path, holder="primary", pid=os.getpid())
    assert primary.acquire() is True
    assert primary.writer is True
    secondary = OrchestratorLease(path, holder="secondary", pid=1)
    assert secondary.acquire() is False
    assert secondary.writer is False
    assert secondary.status()["held_by_live_other"] is True


def test_acquire_is_idempotent_in_the_same_process(tmp_path: Path) -> None:
    """Covers: prompt section 'single-writer ... self-healing' (re-entrant)."""
    path = tmp_path / "lease.json"
    lease = OrchestratorLease(path, holder="same", pid=os.getpid())
    assert lease.acquire() is True
    assert lease.acquire() is True


def test_dead_pid_lease_self_heals(tmp_path: Path) -> None:
    """Covers: prompt section 'Stale lease self-healing (dead-PID detection)'."""
    path = tmp_path / "lease.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "holder": "ghost",
                "pid": _dead_pid(),
                "acquired_at": "2020-01-01T00:00:00+00:00",
                "host": "gone",
            }
        ),
        encoding="utf-8",
    )
    lease = OrchestratorLease(path, holder="next", pid=os.getpid())
    assert lease.acquire() is True
    assert lease.status()["stale_cleared_on_acquire"] is True
    assert lease.status()["holder"] == "next"


def test_malformed_lease_record_never_blocks_the_writer(tmp_path: Path) -> None:
    """Covers: prompt section 'self-healing' (corrupt record is stale)."""
    path = tmp_path / "lease.json"
    path.write_text("{not valid json", encoding="utf-8")
    lease = OrchestratorLease(path, holder="next", pid=os.getpid())
    assert lease.acquire() is True


def test_release_frees_the_writer_role(tmp_path: Path) -> None:
    """Covers: prompt section 'Single-Writer Orchestrator Lease' (release)."""
    path = tmp_path / "lease.json"
    first = OrchestratorLease(path, holder="first", pid=os.getpid())
    assert first.acquire() is True
    assert first.release() is True
    assert not path.read_text(encoding="utf-8").strip()
    other = OrchestratorLease(path, holder="other", pid=os.getpid())
    assert other.acquire() is True


def test_release_never_steals_another_live_holder(tmp_path: Path) -> None:
    """Covers: single-writer (a non-holder cannot free a live lease)."""
    path = tmp_path / "lease.json"
    OrchestratorLease(path, holder="primary", pid=1).acquire()
    other = OrchestratorLease(path, holder="other", pid=os.getpid())
    assert other.release() is False
    assert OrchestratorLease(path).read() is not None


def test_pid_is_alive_reports_self_and_dead(tmp_path: Path) -> None:
    """Covers: prompt section 'dead-PID detection (os.kill(pid, 0))'."""
    assert pid_is_alive(os.getpid()) is True
    assert pid_is_alive(_dead_pid()) is False
    assert pid_is_alive(0) is False
    assert pid_is_alive(-1) is False


def test_read_only_client_refuses_mutations_but_allows_reads(tmp_path: Path) -> None:
    """Covers: prompt section 'read-only second client' (mutating vs read)."""
    path = tmp_path / "lease.json"
    OrchestratorLease(path, holder="primary", pid=1).acquire()
    server = _server(tmp_path, lease=OrchestratorLease(path, holder="secondary", pid=os.getpid()))
    assert server.read_only is True
    _ready(server)
    blocked = _call(server, "tools/call", {"name": "submit_job", "arguments": {}})
    assert blocked["error"]["code"] == -32020
    read = _tool(server, "list_jobs", {})
    assert read["isError"] is False


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #


def test_redaction_masks_secret_named_keys_and_credential_values() -> None:
    """Covers: ADR-0004 D7 'Read tools never return credentials'."""
    payload = {
        "api_key": "whatever",
        "nested": {"oauth_token": "abc", "safe": "value"},
        "goal": "use sk-live-ABCDEFGH12345678 please",
        "items": ["Bearer abcdefghijklmnopqrst", {"password": "hunter2"}],
    }
    redacted = redact_secrets(payload)
    assert redacted["api_key"] == REDACTED
    assert redacted["nested"]["oauth_token"] == REDACTED
    assert redacted["nested"]["safe"] == "value"
    assert "sk-live-ABCDEFGH12345678" not in redacted["goal"]
    assert redacted["items"][1]["password"] == REDACTED
    assert "abcdefghijklmnopqrst" not in redacted["items"][0]


def test_read_evidence_redacts_credential_values(tmp_path: Path) -> None:
    """Covers: ADR-0004 D7 (no secret reference values over MCP)."""
    orch = tmp_path / ".orch"
    orch.mkdir(parents=True, exist_ok=True)
    (orch / "hygiene.md").write_text(
        "NOTE: leaked sk-ant-abcdefghijklmnopqrstuvwx here\n", encoding="utf-8"
    )
    server = _server(tmp_path)
    _ready(server)
    result = _tool(server, "read_evidence", {"path": "hygiene.md"})
    content = result["structuredContent"]["content"]
    assert "sk-ant-abcdefghijklmnopqrstuvwx" not in content
    assert REDACTED in content


def test_redact_secrets_is_total_on_odd_input() -> None:
    """Covers: redaction is non-raising and JSON-compatible."""
    assert redact_secrets(None) is None
    assert redact_secrets(7) == 7
    assert redact_secrets([1, {"token": "t"}]) == [1, {"token": REDACTED}]


# --------------------------------------------------------------------------- #
# stdio transport
# --------------------------------------------------------------------------- #


def test_stdio_serves_lines_and_skips_notifications(tmp_path: Path) -> None:
    """Covers: prompt section 'Protocol Transport (JSON-RPC 2.0 over stdio)'."""
    server = _server(tmp_path)
    payload = "\n".join(
        [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}),
            "",
        ]
    )
    out = io.StringIO()
    rc = serve_stdio(server, stdin=io.StringIO(payload), stdout=out)
    assert rc == 0
    responses = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [r["id"] for r in responses] == [1, 2]
    assert responses[1]["result"] == {}


def test_stdio_reports_a_parse_error_for_a_bad_line(tmp_path: Path) -> None:
    """Covers: prompt section 'stdio transport' (bad line is a parse error)."""
    server = _server(tmp_path)
    out = io.StringIO()
    serve_stdio(server, stdin=io.StringIO("{not json}\n"), stdout=out)
    response = json.loads(out.getvalue().splitlines()[0])
    assert response["error"]["code"] == -32700
    assert response["id"] is None


def test_stdio_releases_the_writer_lease_on_eof(tmp_path: Path) -> None:
    """Covers: prompt section 'single-writer' (session teardown)."""
    path = tmp_path / "lease.json"
    server = _server(tmp_path, lease=OrchestratorLease(path, holder="s", pid=os.getpid()))
    assert server.read_only is False
    serve_stdio(server, stdin=io.StringIO(""), stdout=io.StringIO())
    assert OrchestratorLease(path).read() is None


def test_module_entrypoint_is_importable() -> None:
    """Covers: prompt section 'MCP server in src/saberops/mcp/'."""
    assert sys.modules["saberops.mcp.protocol"].McpServer is McpServer
