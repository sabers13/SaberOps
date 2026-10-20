"""MCP JSON-RPC 2.0 server over the contract tools (ADR-0004 D7, M3).

Implements the standard lifecycle (``initialize`` ->
``notifications/initialized`` -> reads), ``tools/list`` / ``tools/call``,
``resources/list`` / ``resources/read`` and ``ping``.

Two boundary rules live here:

* every value a client receives is passed through
  :func:`saberops.mcp.redaction.redact_secrets`, so no read tool -- jobs,
  questions, evidence, proposals -- can echo a credential or secret value;
* the never-exposed authority refusals
  (:class:`~saberops.mcp.errors.ForbiddenOperationError`) and the single-writer
  lease refusals (:class:`~saberops.mcp.errors.LeaseReadOnlyError`) are
  returned as JSON-RPC **errors**, while an ordinary tool execution failure is
  returned as an ``isError`` result with machine-readable detail.  A client can
  therefore always tell "this authority is not offered" apart from "this call
  failed".
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from saberops.mcp.errors import (
    ForbiddenOperationError,
    InvalidParamsError,
    InvalidRequestError,
    LeaseReadOnlyError,
    McpError,
    MethodNotFoundError,
    ParseError,
    ToolError,
)
from saberops.mcp.guide import (
    GUIDE,
    GUIDE_PROMPT,
    GUIDE_URI,
    PAUSED_INSTRUCTIONS,
    PREFIX,
    SABEROPS_OFF_TOOL,
    SABEROPS_ON_TOOL,
    SESSION_OFF_INSTRUCTIONS,
    SESSION_OFF_MESSAGE,
    SHORT_INSTRUCTIONS,
)
from saberops.mcp.ledger import SNAPSHOT_SCHEMA_VERSION
from saberops.mcp.redaction import redact_secrets
from saberops.mcp.switch import mcp_enabled
from saberops.mcp.tools import ServerContext, ToolRegistry, build_default_registry

__all__ = [
    "PROTOCOL_VERSION",
    "SERVER_NAME",
    "SERVER_VERSION",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "McpServer",
]

#: Server identity advertised in ``initialize`` (ADR-0004 D7 / Step 014).
SERVER_NAME = "saberops"
SERVER_VERSION = "0.5.0"

#: The newest MCP protocol revision this server implements.
PROTOCOL_VERSION = "2025-06-18"

#: Revisions this server can also serve.
SUPPORTED_PROTOCOL_VERSIONS: tuple[str, ...] = (
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)

#: Synthetic resource URIs exposing engine state.
SNAPSHOT_URI = "saberops://snapshot"
LEASE_URI = "saberops://lease"
LEDGER_URI = "saberops://ledger"

_JSON_MIME = "application/json"
_TEXT_MIME = "text/plain"


def _mime_for(name: str) -> str:
    lowered = name.lower()
    if lowered.endswith(".json"):
        return _JSON_MIME
    return _TEXT_MIME


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class McpServer:
    """A single MCP session over one :class:`ServerContext`."""

    def __init__(
        self,
        context: ServerContext | None = None,
        *,
        name: str = SERVER_NAME,
        version: str = SERVER_VERSION,
        acquire_lease: bool = True,
        start_active: bool = True,
    ) -> None:
        self._context = context if context is not None else ServerContext()
        self._name = name
        self._version = version
        self._registry: ToolRegistry = build_default_registry(self._context)
        self._want_lease = acquire_lease
        # A session is "on" only when the owner's master switch allows it and
        # the user turned SaberOps on in this agent (``saberops_on``). Agent
        # CLIs start sessions off (``orch mcp serve``), so opening a CLI is a
        # normal agent until the user asks for SaberOps.
        self._active = start_active
        self._outbox: list[dict[str, Any]] = []
        if acquire_lease and self.enabled and self._active:
            self._context.lease.acquire()
        self._initialized = False
        self._client_info: dict[str, Any] = {}

    @property
    def context(self) -> ServerContext:
        return self._context

    @property
    def registry(self) -> ToolRegistry:
        return self._registry

    @property
    def enabled(self) -> bool:
        """The owner's on/off switch (``orch mcp on|off``), read on every request."""
        return mcp_enabled(self._context.lease.path)

    @property
    def active(self) -> bool:
        """SaberOps is on in this session (master switch on and turned on here)."""
        return self.enabled and self._active

    def drain_notifications(self) -> list[dict[str, Any]]:
        """Server-to-client notifications queued since the last call."""
        queued, self._outbox = self._outbox, []
        return queued

    def _tools_changed(self) -> None:
        self._outbox.append({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})

    def _sync_switch(self) -> bool:
        """Follow a switch flipped mid-session; return whether tools are offered."""
        on = self.active
        lease = self._context.lease
        if not on and lease.writer:
            lease.release()
        elif on and self._want_lease and not lease.writer:
            lease.acquire()
            name = self._client_info.get("title") or self._client_info.get("name")
            if isinstance(name, str):
                lease.record_client(name)
        return on

    @property
    def read_only(self) -> bool:
        """True when a live other client holds the writer lease."""
        return not self._context.lease.writer

    @property
    def initialized(self) -> bool:
        return self._initialized

    # -- JSON-RPC envelope -------------------------------------------------

    def handle(self, message: Any) -> dict[str, Any] | None:
        """Handle one decoded JSON-RPC message; ``None`` for a notification."""
        if not isinstance(message, Mapping):
            return self._error_response(None, ParseError("message must be a JSON object"))
        if message.get("jsonrpc") != "2.0":
            return self._error_response(
                message.get("id") if isinstance(message.get("id"), (str, int)) else None,
                InvalidRequestError("jsonrpc must be '2.0'"),
            )
        raw_id = message.get("id")
        message_id = raw_id if isinstance(raw_id, (str, int)) else None
        method = message.get("method")
        if not isinstance(method, str) or not method.strip():
            return self._error_response(message_id, InvalidRequestError("method is required"))
        params = message.get("params", {})
        if not isinstance(params, Mapping):
            return self._error_response(
                message_id, InvalidParamsError("params must be a JSON object")
            )
        is_notification = "id" not in message
        if method.startswith("notifications/"):
            self._handle_notification(method, params)
            return None
        try:
            result = self._dispatch(method, params)
        except McpError as exc:
            return self._error_response(message_id, exc)
        except Exception as exc:  # noqa: BLE001 - never leak a traceback to a client
            return self._error_response(
                message_id,
                McpError(
                    "internal server error",
                    data={"type": type(exc).__name__},
                ),
            )
        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": raw_id, "result": result}

    @staticmethod
    def _error_response(message_id: Any, error: McpError) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": message_id, "error": error.to_error()}

    def _handle_notification(self, method: str, params: Mapping[str, Any]) -> None:
        if method == "notifications/initialized":
            self._initialized = True

    def _dispatch(self, method: str, params: Mapping[str, Any]) -> Any:
        if method == "initialize":
            return self._initialize(params)
        if method == "ping":
            return {}
        if method in (
            "tools/list", "tools/call", "resources/list", "resources/read",
            "prompts/list", "prompts/get",
        ):
            if not self._initialized:
                raise InvalidRequestError(
                    "initialize and notifications/initialized are required first"
                )
        if method == "tools/list":
            on = self._sync_switch()
            if not self.enabled:
                return {"tools": []}
            if not on:
                return {"tools": [SABEROPS_ON_TOOL]}
            return {"tools": [*self._registry.descriptors(), SABEROPS_OFF_TOOL]}
        if method == "tools/call":
            name = params.get("name")
            if name in (SABEROPS_ON_TOOL["name"], SABEROPS_OFF_TOOL["name"]):
                return self._session_switch(turn_on=name == SABEROPS_ON_TOOL["name"])
            if not self.enabled:
                return self._tool_error_result(ToolError(PAUSED_INSTRUCTIONS))
            if not self._sync_switch():
                return self._tool_error_result(ToolError(SESSION_OFF_MESSAGE))
            return self._tools_call(params)
        if method == "prompts/list":
            return {
                "prompts": [
                    {
                        "name": GUIDE_PROMPT,
                        "description": "How to plan and submit work through SaberOps.",
                    }
                ]
            }
        if method == "prompts/get":
            if params.get("name") != GUIDE_PROMPT:
                raise InvalidParamsError(f"unknown prompt: {params.get('name')!r}")
            return {
                "description": "How to plan and submit work through SaberOps.",
                "messages": [{"role": "user", "content": {"type": "text", "text": GUIDE}}],
            }
        if method == "resources/list":
            return self._resources_list()
        if method == "resources/read":
            return self._resources_read(params)
        raise MethodNotFoundError(f"unknown method: {method}")

    # -- lifecycle ---------------------------------------------------------

    def _initialize(self, params: Mapping[str, Any]) -> dict[str, Any]:
        requested = params.get("protocolVersion")
        negotiated = (
            requested
            if isinstance(requested, str) and requested in SUPPORTED_PROTOCOL_VERSIONS
            else PROTOCOL_VERSION
        )
        client = params.get("clientInfo")
        if isinstance(client, Mapping):
            self._client_info = {str(k): v for k, v in client.items()}
            name = self._client_info.get("title") or self._client_info.get("name")
            if isinstance(name, str):
                self._context.lease.record_client(name)
        enabled = self.enabled
        if not enabled:
            instructions = PAUSED_INSTRUCTIONS
        elif not self._active:
            instructions = SESSION_OFF_INSTRUCTIONS
        else:
            instructions = (
                SHORT_INSTRUCTIONS
                + " Approving proposals, moving main, pushing, quarantine approval, "
                "posture attestation, gate execution, tracked-source edits and "
                "deletions outside .orch are never available here."
            )
        return {
            "protocolVersion": negotiated,
            "capabilities": {
                "tools": {"listChanged": True},
                "resources": {"subscribe": False, "listChanged": False},
                "prompts": {"listChanged": False},
            },
            "serverInfo": {
                "name": self._name,
                "version": self._version,
                "title": "SaberOps Orchestrator engine",
            },
            "snapshotSchemaVersion": SNAPSHOT_SCHEMA_VERSION,
            "readOnly": self.read_only,
            "lease": self._context.lease.status(),
            "enabled": enabled,
            "active": enabled and self._active,
            "instructions": instructions,
        }

    # -- tools -------------------------------------------------------------

    def _tools_call(self, params: Mapping[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str) or not name.strip():
            raise InvalidParamsError("tools/call requires a tool name")
        arguments = params.get("arguments", {})
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, Mapping):
            raise InvalidParamsError("tools/call arguments must be an object")
        try:
            payload = self._registry.call(name.strip(), dict(arguments))
        except (ForbiddenOperationError, LeaseReadOnlyError):
            # Authority refusals are protocol-level: the client must see a
            # JSON-RPC error, never an isError result it could retry around.
            raise
        except McpError as exc:
            return self._tool_error_result(exc)
        except Exception as exc:  # noqa: BLE001 - a tool failure is data, not a crash
            return self._tool_error_result(
                ToolError(f"tool execution failed: {type(exc).__name__}: {exc}")
            )
        redacted = redact_secrets(payload)
        return {
            "content": [
                {
                    "type": "text",
                    "text": PREFIX + f" {name.strip()} result\n"
                    + json.dumps(redacted, sort_keys=True, indent=2),
                }
            ],
            "isError": False,
            "structuredContent": redacted,
        }

    @staticmethod
    def _tool_error_result(error: McpError) -> dict[str, Any]:
        text = error.message if error.message.startswith(PREFIX) else f"{PREFIX} {error.message}"
        return {
            "content": [{"type": "text", "text": text}],
            "isError": True,
            "structuredContent": {
                "v": SNAPSHOT_SCHEMA_VERSION,
                "error": error.to_error(),
            },
        }

    def _session_switch(self, *, turn_on: bool) -> dict[str, Any]:
        """``saberops_on`` / ``saberops_off``: the user's in-agent switch."""
        if turn_on and not self.enabled:
            return self._tool_error_result(ToolError(PAUSED_INSTRUCTIONS))
        changed = self._active != turn_on
        self._active = turn_on
        self._sync_switch()
        if changed:
            self._tools_changed()
        if turn_on:
            role = (
                "This session plans the work."
                if not self.read_only
                else "Another agent session is already planning; this one can only read."
            )
            text = (
                f"{PREFIX} SaberOps is on. {role} Start every message that relays SaberOps "
                f"to the user with \"{PREFIX}\". Follow this guide:\n\n{GUIDE}"
            )
        else:
            text = (
                f"{PREFIX} SaberOps is off for this session. You are a normal coding agent "
                "again; do not use SaberOps until the user turns it back on."
            )
        return {
            "content": [{"type": "text", "text": text}],
            "isError": False,
            "structuredContent": {"v": SNAPSHOT_SCHEMA_VERSION, "active": self.active},
        }

    # -- resources ---------------------------------------------------------

    def _resources_list(self) -> dict[str, Any]:
        resources: list[dict[str, Any]] = [
            {
                "uri": SNAPSHOT_URI,
                "name": "SaberOps read snapshot (v1)",
                "mimeType": _JSON_MIME,
            },
            {"uri": LEASE_URI, "name": "Orchestrator lease", "mimeType": _JSON_MIME},
            {"uri": LEDGER_URI, "name": "Append-only event ledger", "mimeType": _JSON_MIME},
            {"uri": GUIDE_URI, "name": "How to work with SaberOps", "mimeType": "text/markdown"},
        ]
        for path in self._context.evidence.list_files():
            resources.append(
                {
                    "uri": path.as_uri(),
                    "name": path.name,
                    "mimeType": _mime_for(path.name),
                    "description": "engine evidence file",
                }
            )
        return {"resources": resources}

    def _resources_read(self, params: Mapping[str, Any]) -> dict[str, Any]:
        uri = params.get("uri")
        if not isinstance(uri, str) or not uri.strip():
            raise InvalidParamsError("resources/read requires a uri")
        target = uri.strip()
        if target == SNAPSHOT_URI:
            return self._contents(target, self._snapshot(), _JSON_MIME)
        if target == LEASE_URI:
            return self._contents(target, self._context.lease.status(), _JSON_MIME)
        if target == LEDGER_URI:
            return self._contents(target, self._context.ledger.snapshot(), _JSON_MIME)
        if target == GUIDE_URI:
            return self._contents(target, GUIDE, "text/markdown")
        if target.startswith("file://"):
            parsed = urlparse(target)
            path = Path(unquote(parsed.path))
            resolved, text, truncated = self._context.evidence.read(path)
            payload = {
                "v": SNAPSHOT_SCHEMA_VERSION,
                "name": resolved.name,
                "path": str(resolved),
                "content": text,
                "truncated": truncated,
            }
            return self._contents(target, payload, _mime_for(resolved.name))
        raise InvalidParamsError(
            f"unsupported resource uri: {target!r}",
            data={"supported": [SNAPSHOT_URI, LEASE_URI, LEDGER_URI, GUIDE_URI, "file://..."]},
        )

    def _contents(self, uri: str, payload: Any, mime: str) -> dict[str, Any]:
        redacted = redact_secrets(payload)
        text = redacted if isinstance(redacted, str) else json.dumps(redacted, sort_keys=True)
        return {"contents": [{"uri": uri, "mimeType": mime, "text": text}]}

    def _snapshot(self) -> dict[str, Any]:
        """Assemble the ``v: 1`` read snapshot from the registry's read tools."""
        snapshot: dict[str, Any] = {
            "v": SNAPSHOT_SCHEMA_VERSION,
            "generated_at": _now_iso(),
            "lease": self._context.lease.status(),
            "ledger": self._context.ledger.snapshot(),
        }
        for tool, arguments in (
            ("list_jobs", {"limit": 20}),
            ("capacity", {}),
            ("list_questions", {}),
            ("list_proposals", {}),
        ):
            try:
                snapshot[tool] = self._registry.call(tool, dict(arguments))
            except McpError as exc:
                snapshot[tool] = {
                    "v": SNAPSHOT_SCHEMA_VERSION,
                    "error": exc.to_error(),
                }
            except Exception as exc:  # noqa: BLE001 - a snapshot must always render
                snapshot[tool] = {
                    "v": SNAPSHOT_SCHEMA_VERSION,
                    "error": {
                        "code": -32603,
                        "message": f"snapshot section unavailable: {type(exc).__name__}",
                    },
                }
        return snapshot

    # -- lifecycle teardown ------------------------------------------------

    def close(self) -> bool:
        """Release the writer lease when this session holds it."""
        return self._context.lease.release()


# Kept for callers that only need a parse-error response without a session.
def parse_error_response(message: str) -> dict[str, Any]:
    """Render the JSON-RPC parse-error envelope for an unreadable line."""
    return {"jsonrpc": "2.0", "id": None, "error": ParseError(message).to_error()}


# Re-exported for convenience in tests and the CLI.
__all__ += ["ToolError", "parse_error_response"]
