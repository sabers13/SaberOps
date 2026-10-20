"""Newline-delimited JSON-RPC transport over standard I/O (M3).

MCP's stdio transport is one JSON-RPC message per line on stdin and one
response per line on stdout.  Log or diagnostic output must never be written to
stdout (it would corrupt the stream), so this module writes responses only and
leaves diagnostics to stderr.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable
from typing import Any, TextIO

from saberops.mcp.lease import OrchestratorLease
from saberops.mcp.ledger import EventLedger
from saberops.mcp.protocol import McpServer, parse_error_response
from saberops.mcp.tools import ServerContext

__all__ = ["build_server_context", "run_stdio", "serve_stdio"]


def build_server_context(
    *,
    db_path: str | None = None,
    lease_path: str | None = None,
    ledger_path: str | None = None,
    questions_store: str | None = None,
    config_ledger: str | None = None,
    cwd: str | None = None,
) -> ServerContext:
    """Build a :class:`ServerContext` from explicit CLI options."""
    from pathlib import Path

    from saberops.mcp.evidence import EvidenceRoots

    return ServerContext(
        db_path=Path(db_path) if db_path else None,
        questions_store=Path(questions_store) if questions_store else None,
        config_ledger_path=Path(config_ledger) if config_ledger else None,
        evidence=EvidenceRoots.discover(Path(cwd).resolve() if cwd else None),
        ledger=EventLedger(ledger_path) if ledger_path else EventLedger(),
        lease=OrchestratorLease(lease_path) if lease_path else OrchestratorLease(),
    )


def serve_stdio(
    server: McpServer,
    *,
    stdin: Iterable[str] | None = None,
    stdout: TextIO | None = None,
) -> int:
    """Serve ``server`` until the input stream ends; returns an exit code."""
    source: Iterable[str] = sys.stdin if stdin is None else stdin
    sink: TextIO = sys.stdout if stdout is None else stdout
    try:
        for raw_line in source:
            line = raw_line.strip()
            if not line:
                continue
            try:
                decoded: Any = json.loads(line)
            except ValueError:
                response: dict[str, Any] | None = parse_error_response(
                    "input line is not valid JSON"
                )
            else:
                response = server.handle(decoded)
            if response is not None:
                sink.write(json.dumps(response, sort_keys=True) + "\n")
            for notification in server.drain_notifications():
                sink.write(json.dumps(notification, sort_keys=True) + "\n")
            sink.flush()
    finally:
        server.close()
    return 0


def run_stdio(context: ServerContext, **kwargs: Any) -> int:
    """Construct a server for ``context`` and serve stdio."""
    server = McpServer(context)
    return serve_stdio(server, **kwargs)
