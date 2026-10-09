"""Bounded, metadata-only discovery through the providers' native CLI protocols.

Wire contracts follow anthropics/claude-agent-sdk-python's initialize control
request and github/copilot-sdk's Content-Length JSON-RPC models.list request.
No user message, session.create, prompt or inference request is sent.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from typing import Any

from .connections import ConnectionTestReason
from .discovery import ModelListingError

_LIST_TIMEOUT = 20.0
_MAX_BYTES = 1_000_000


def _unknown(detail: str) -> ModelListingError:
    return ModelListingError(ConnectionTestReason.CAPABILITY_UNKNOWN, detail)


def _ids(payload: Any, field: str) -> list[str]:
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        raise _unknown("native CLI returned no supported model catalog")
    result: list[str] = []
    for item in payload["models"]:
        if not isinstance(item, dict) or not isinstance(item.get(field), str):
            raise _unknown("native CLI returned an invalid model catalog")
        value = item[field].strip()
        if not value or any(char.isspace() for char in value):
            raise _unknown("native CLI returned an invalid model identifier")
        if value not in result:
            result.append(value)
    return result


async def _exchange(executable: str, protocol: str, cwd: str) -> list[str]:
    if protocol == "claude":
        argv = [executable, "--print", "--input-format", "stream-json",
                "--output-format", "stream-json", "--verbose", "--tools", "",
                "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                "--settings", '{"disableAllHooks":true}', "--no-session-persistence"]
        message = {"type": "control_request", "request_id": "saberops-models",
                   "request": {"subtype": "initialize", "hooks": None}}
        wire = (json.dumps(message) + "\n").encode()
    elif protocol == "copilot":
        argv = [executable, "--headless", "--stdio", "--no-auto-update"]
        message = {"jsonrpc": "2.0", "id": "saberops-models", "method": "models.list",
                   "params": {}}
        body = json.dumps(message).encode()
        wire = f"Content-Length: {len(body)}\r\n\r\n".encode() + body
    else:
        raise _unknown("unsupported native model-list protocol")
    process = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, cwd=cwd, limit=_MAX_BYTES,
    )
    try:
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(wire)
        await process.stdin.drain()
        total = 0
        while total <= _MAX_BYTES:
            if protocol == "copilot":
                header = await process.stdout.readuntil(b"\r\n\r\n")
                fields = dict(line.split(b":", 1) for line in header.strip().split(b"\r\n"))
                length = int(fields[b"Content-Length"].strip())
                if not 0 < length <= _MAX_BYTES - total:
                    raise _unknown("native model catalog exceeds response size limit")
                raw = await process.stdout.readexactly(length)
                total += len(header)
            else:
                raw = await process.stdout.readline()
                if not raw:
                    raise _unknown("native CLI closed before returning a model catalog")
            total += len(raw)
            reply = json.loads(raw)
            if not isinstance(reply, dict):
                continue
            if protocol == "copilot" and reply.get("id") == "saberops-models":
                if "error" in reply:
                    raise _unknown("native model-list request failed; check CLI login and version")
                return _ids(reply.get("result"), "id")
            if protocol == "claude" and reply.get("type") == "control_response":
                response = reply.get("response")
                if (not isinstance(response, dict)
                        or response.get("request_id") != "saberops-models"):
                    continue
                if response.get("subtype") != "success":
                    raise _unknown("native model-list request failed; check CLI login and version")
                return _ids(response.get("response"), "value")
        raise _unknown("native model catalog exceeds response size limit")
    finally:
        if process.stdin is not None:
            process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), timeout=1)
        except TimeoutError:
            if process.returncode is None:
                process.kill()
            await process.wait()


def list_native_models(executable: str, protocol: str) -> list[str]:
    """Ask the installed CLI for metadata using its own account session."""
    async def bounded(cwd: str) -> list[str]:
        return await asyncio.wait_for(_exchange(executable, protocol, cwd), _LIST_TIMEOUT)

    try:
        # Never initialize against the user's repository or its project hooks.
        with tempfile.TemporaryDirectory(prefix="saberops-model-list-") as cwd:
            return asyncio.run(bounded(cwd))
    except ModelListingError:
        raise
    except TimeoutError as exc:
        raise _unknown("native model-list request timed out") from exc
    except (OSError, ValueError, KeyError, asyncio.IncompleteReadError,
            asyncio.LimitOverrunError) as exc:
        raise _unknown(
            "native model-list protocol unavailable; check CLI login and version"
        ) from exc
