"""Native, metadata-only Copilot login verification; no inference session.

Wire contract: github/copilot-sdk's auth.getStatus / isAuthenticated.
Account identifiers, status messages and credential material never leave here.
"""

from __future__ import annotations

import asyncio
import json
import tempfile

_MAX_BYTES = 1_000_000


async def _copilot_status(executable: str, cwd: str) -> bool | None:
    process = await asyncio.create_subprocess_exec(
        executable, "--headless", "--stdio", "--no-auto-update",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, cwd=cwd, limit=_MAX_BYTES,
    )
    try:
        assert process.stdin is not None and process.stdout is not None
        body = json.dumps({"jsonrpc": "2.0", "id": "saberops-auth",
                           "method": "auth.getStatus", "params": {}}).encode()
        process.stdin.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        await process.stdin.drain()
        total = 0
        while total < _MAX_BYTES:
            header = await process.stdout.readuntil(b"\r\n\r\n")
            fields = dict(line.split(b":", 1) for line in header.strip().split(b"\r\n"))
            length = int(fields[b"Content-Length"].strip())
            if not 0 < length <= _MAX_BYTES - total - len(header):
                return None
            raw = await process.stdout.readexactly(length)
            total += len(header) + length
            reply = json.loads(raw)
            if not isinstance(reply, dict) or reply.get("id") != "saberops-auth":
                continue
            result = reply.get("result")
            if "error" in reply or not isinstance(result, dict):
                return None
            value = result.get("isAuthenticated")
            return value if isinstance(value, bool) else None
        return None
    finally:
        if process.stdin is not None:
            process.stdin.close()
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(process.wait(), 1)
        except TimeoutError:
            process.kill()
            await process.wait()


def native_authentication(executable: str, protocol: str, *, timeout: float = 8) -> bool | None:
    """Ask the native CLI about its existing login; unsupported versions stay unknown."""
    if protocol != "copilot":
        return None
    try:
        with tempfile.TemporaryDirectory(prefix="saberops-login-") as cwd:
            return asyncio.run(asyncio.wait_for(_copilot_status(executable, cwd), timeout))
    except (OSError, ValueError, KeyError, TimeoutError, asyncio.IncompleteReadError,
            asyncio.LimitOverrunError):
        return None
