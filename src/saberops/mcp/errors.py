"""Typed refusals for the SaberOps MCP interface (ADR-0004 D7, Step 014 / M3).

Every refusal is a value, not a traceback: the stdio server maps an
:class:`McpError` onto a JSON-RPC 2.0 error object with a stable code, so a
client can distinguish "the server does not offer that operation"
(:class:`ForbiddenOperationError`) from "this argument is wrong"
(:class:`InvalidParamsError`) or "you are the read-only client"
(:class:`LeaseReadOnlyError`).

Authority is deterministic: the MCP layer refuses operations that ADR-0004
D7 marks **never exposed** (approving proposals, moving ``main``, pushing,
quarantine approval, posture attestation, gate execution, tracked-source
edits, deletions outside ``.orch``) before any handler runs.
"""

from __future__ import annotations

from typing import Any

# -- JSON-RPC 2.0 standard error codes ---------------------------------------
JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603

# -- MCP/server-defined error codes -----------------------------------------
MCP_TOOL_ERROR = -32000
MCP_FORBIDDEN_OPERATION = -32010
MCP_LEASE_READ_ONLY = -32020
MCP_PATH_REFUSED = -32030
MCP_NOT_FOUND = -32040

__all__ = [
    "JSONRPC_INTERNAL_ERROR",
    "JSONRPC_INVALID_PARAMS",
    "JSONRPC_INVALID_REQUEST",
    "JSONRPC_METHOD_NOT_FOUND",
    "JSONRPC_PARSE_ERROR",
    "MCP_FORBIDDEN_OPERATION",
    "MCP_LEASE_READ_ONLY",
    "MCP_NOT_FOUND",
    "MCP_PATH_REFUSED",
    "MCP_TOOL_ERROR",
    "ForbiddenOperationError",
    "InvalidParamsError",
    "JsonRpcError",
    "LeaseReadOnlyError",
    "McpError",
    "MethodNotFoundError",
    "NotFoundError",
    "ParseError",
    "PathRefusedError",
    "InvalidRequestError",
    "SnapshotVersionError",
    "ToolError",
]


class McpError(Exception):
    """Base class for every typed refusal of the MCP interface."""

    code: int = JSONRPC_INTERNAL_ERROR

    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = str(message)
        if code is not None:
            self.code = code
        self.data: dict[str, Any] = dict(data) if data is not None else {}

    def to_error(self) -> dict[str, Any]:
        """Render the JSON-RPC error object for this refusal."""
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data:
            payload["data"] = self.data
        return payload


class JsonRpcError(McpError):
    """A protocol-level failure (bad envelope, unknown method, bad params)."""


class ParseError(JsonRpcError):
    """The incoming line was not a JSON object."""

    code = JSONRPC_PARSE_ERROR


class InvalidRequestError(JsonRpcError):
    """The JSON-RPC envelope is malformed (wrong version, missing method)."""

    code = JSONRPC_INVALID_REQUEST


class MethodNotFoundError(JsonRpcError):
    """The requested JSON-RPC method is not part of the MCP surface."""

    code = JSONRPC_METHOD_NOT_FOUND


class InvalidParamsError(JsonRpcError):
    """The method/tool arguments do not match the declared schema."""

    code = JSONRPC_INVALID_PARAMS


class ToolError(McpError):
    """A tool call failed validation, lookup or execution."""

    code = MCP_TOOL_ERROR


class ForbiddenOperationError(ToolError):
    """The requested operation is on the never-exposed list (ADR-0004 D7).

    This is not "unknown tool": the operation is a known, permanently
    prohibited authority.  It is refused before any handler runs.
    """

    code = MCP_FORBIDDEN_OPERATION


class LeaseReadOnlyError(ToolError):
    """A mutating call arrived while this client is the read-only client."""

    code = MCP_LEASE_READ_ONLY


class PathRefusedError(ToolError):
    """An evidence path escaped the allowed roots or filename allowlist."""

    code = MCP_PATH_REFUSED


class NotFoundError(ToolError):
    """A requested record (job, question, wave) does not exist."""

    code = MCP_NOT_FOUND


class SnapshotVersionError(ToolError):
    """The client asked for an unsupported read-snapshot version."""

    code = MCP_TOOL_ERROR
