"""Read-only CLI usage evidence. Never sends a prompt or reads credentials.

Account limits come only from native CLI metadata interfaces. Local activity is a
separate, best-effort projection of numeric session fields; it is not billing
or automatic routing authority. Unknown values stay unknown.
"""

from __future__ import annotations

import asyncio
import json
import math
import tempfile
from datetime import UTC, datetime
from typing import Any

from saberops.native_usage_panel import read_panel
from saberops.opencode_go_usage import read_go_limits

CLIS = (
    ("codex", "codex", "Codex"),
    ("anthropic", "claude", "Claude Code"),
    ("opencode", "opencode", "OpenCode"),
    ("antigravity", "agy", "Antigravity"),
    ("cline", "cline", "Cline"),
    ("gemini", "gemini", "Gemini CLI"),
)
MAX_LOG_BYTES = 32 * 1024 * 1024
MAX_LOG_FILES = 300
RPC_TIMEOUT = 10.0
LIMIT_PROVIDERS = ("codex", "anthropic", "antigravity", "opencode")


def numeric(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value) if math.isfinite(value) and value >= 0 else None
    except OverflowError:
        return None


def percent(value: Any) -> float | None:
    result = numeric(value)
    return result if result is not None and result <= 100 else None


def parse_limits(provider: str, payload: Any) -> list[dict[str, Any]]:
    """Allowlist supported wire fields; never return the raw CLI payload."""
    if not isinstance(payload, dict):
        return []
    rows: list[dict[str, Any]] = []
    if provider == "codex":
        buckets = payload.get("rateLimitsByLimitId")
        if not isinstance(buckets, dict) or not buckets:
            buckets = {"codex": payload.get("rateLimits")}
        for bucket_id, bucket in buckets.items():
            if not isinstance(bucket, dict):
                continue
            for name in ("primary", "secondary"):
                window = bucket.get(name)
                if not isinstance(window, dict):
                    continue
                used = percent(window.get("usedPercent"))
                duration = numeric(window.get("windowDurationMins"))
                reset = numeric(window.get("resetsAt"))
                reset_at = None
                if reset is not None:
                    try:
                        reset_at = datetime.fromtimestamp(reset, UTC).isoformat()
                    except (OverflowError, OSError, ValueError):
                        pass
                if used is not None:
                    rows.append(
                        {
                            "label": f"{bucket_id} · {name}",
                            "remaining": 100 - used,
                            "duration": duration,
                            "reset_at": reset_at,
                            "unlimited": False,
                        }
                    )
    return rows


async def _native_limits(executable: str, provider: str, cwd: str) -> list[dict[str, Any]]:
    """Use each CLI's own auth via metadata-only stdio; no inference session."""
    from saberops.workers.env import hermetic_env

    argv = [executable, "app-server", "--listen", "stdio://"]
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=cwd,
        limit=1_000_000,
        env=hermetic_env("codex", None),
    )
    assert process.stdin is not None and process.stdout is not None
    stdin, stdout = process.stdin, process.stdout

    async def send(message: dict[str, Any]) -> None:
        body = json.dumps(message).encode() + b"\n"
        stdin.write(body)
        await stdin.drain()

    async def reply(wanted: int) -> Any:
        size = 0
        while size < 1_000_000:
            raw = await stdout.readline()
            if not raw:
                raise ValueError("CLI metadata connection closed")
            size += len(raw)
            data = json.loads(raw)
            if isinstance(data, dict) and data.get("id") == wanted:
                if "error" in data:
                    raise ValueError("CLI quota query unavailable")
                return data.get("result")
        raise ValueError("CLI metadata exceeds size limit")

    try:
        await send(
            {
                "id": 1,
                "method": "initialize",
                "params": {"clientInfo": {"name": "saberops_usage", "version": "1.0"}},
            }
        )
        await reply(1)
        await send({"method": "initialized", "params": {}})
        await send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "account/rateLimits/read",
                "params": {},
            }
        )
        return parse_limits(provider, await reply(2))
    finally:
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


def read_limits(executable: str, provider: str) -> list[dict[str, Any]]:
    if provider == "opencode":
        return read_go_limits(executable)
    if provider in ("anthropic", "antigravity"):
        return read_panel(executable, provider)
    with tempfile.TemporaryDirectory(prefix="saberops-usage-") as cwd:
        return asyncio.run(asyncio.wait_for(_native_limits(executable, provider, cwd), RPC_TIMEOUT))
