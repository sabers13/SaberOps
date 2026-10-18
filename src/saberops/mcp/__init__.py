"""SaberOps MCP interface (ADR-0004 D7, Step 014 / M3).

Public surface:

* :class:`~saberops.mcp.protocol.McpServer` -- the JSON-RPC/MCP server;
* :func:`~saberops.mcp.tools.build_default_registry` -- the twelve D7 tools;
* :class:`~saberops.mcp.lease.OrchestratorLease` -- single-writer lease;
* :class:`~saberops.mcp.ledger.EventLedger` -- append-only event ledger;
* :func:`~saberops.mcp.redaction.redact_secrets` -- the read-boundary guard.
"""

from __future__ import annotations

from saberops.mcp.errors import (
    ForbiddenOperationError,
    InvalidParamsError,
    InvalidRequestError,
    JsonRpcError,
    LeaseReadOnlyError,
    McpError,
    MethodNotFoundError,
    NotFoundError,
    ParseError,
    PathRefusedError,
    SnapshotVersionError,
    ToolError,
)
from saberops.mcp.evidence import EvidenceRoots
from saberops.mcp.lease import LeaseRecord, OrchestratorLease, default_lease_path
from saberops.mcp.ledger import (
    SNAPSHOT_SCHEMA_VERSION,
    EventLedger,
    LedgerConflictError,
    LedgerCorruptError,
    LedgerEvent,
    LedgerState,
)
from saberops.mcp.protocol import (
    PROTOCOL_VERSION,
    SERVER_NAME,
    SERVER_VERSION,
    McpServer,
)
from saberops.mcp.redaction import REDACTED, redact_secrets
from saberops.mcp.stdio import build_server_context, run_stdio, serve_stdio
from saberops.mcp.tools import (
    FORBIDDEN_OPERATIONS,
    MUTATING_TOOLS,
    READ_TOOLS,
    TOOL_NAMES,
    ServerContext,
    ToolRegistry,
    ToolSpec,
    build_default_registry,
)

__all__ = [
    "FORBIDDEN_OPERATIONS",
    "MUTATING_TOOLS",
    "PROTOCOL_VERSION",
    "READ_TOOLS",
    "REDACTED",
    "SERVER_NAME",
    "SERVER_VERSION",
    "SNAPSHOT_SCHEMA_VERSION",
    "TOOL_NAMES",
    "EvidenceRoots",
    "EventLedger",
    "ForbiddenOperationError",
    "InvalidParamsError",
    "InvalidRequestError",
    "JsonRpcError",
    "LeaseReadOnlyError",
    "LeaseRecord",
    "LedgerConflictError",
    "LedgerCorruptError",
    "LedgerEvent",
    "LedgerState",
    "McpError",
    "McpServer",
    "MethodNotFoundError",
    "NotFoundError",
    "OrchestratorLease",
    "ParseError",
    "PathRefusedError",
    "ServerContext",
    "SnapshotVersionError",
    "ToolError",
    "ToolRegistry",
    "ToolSpec",
    "build_default_registry",
    "build_server_context",
    "default_lease_path",
    "redact_secrets",
    "run_stdio",
    "serve_stdio",
]
