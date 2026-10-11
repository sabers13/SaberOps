"""Read-only CLI usage evidence. Never sends a prompt or reads credentials.

Account limits come only from native CLI metadata interfaces. Local activity is a
separate, best-effort projection of numeric session fields; it is not billing
or automatic routing authority. Unknown values stay unknown.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import shutil
import sqlite3
import tempfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from saberops.native_usage_panel import read_panel
from saberops.opencode_go_usage import GoUsageUnavailable, read_go_limits

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


def _sql_activity(path: Path, provider: str) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
        db.execute("PRAGMA query_only=ON")
        if provider == "opencode":
            row = db.execute(
                "SELECT count(*), sum(tokens_input), sum(tokens_output), "
                "sum(tokens_cache_read) FROM session"
            ).fetchone()
        else:
            # AggregateUsage already includes child agents; never sum both fields.
            row = db.execute(
                "SELECT count(*), "
                "sum(json_extract(metadata_json,'$.aggregateUsage.inputTokens')), "
                "sum(json_extract(metadata_json,'$.aggregateUsage.outputTokens')), "
                "sum(json_extract(metadata_json,'$.aggregateUsage.cacheReadTokens')) "
                "FROM sessions"
            ).fetchone()
    if row is None or not row[0]:
        return None
    return {
        "sessions": row[0],
        "input_tokens": numeric(row[1]),
        "output_tokens": numeric(row[2]),
        "cached_tokens": numeric(row[3]),
        "source": "Local CLI session database",
        "coverage": "Saved sessions on this computer",
    }


def _log_activity(root: Path, provider: str) -> dict[str, Any] | None:
    if not root.is_dir():
        return None
    # Bounded samples: no prompts, tool content or credential files leave this function.
    files = sorted(root.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    partial = len(files) > MAX_LOG_FILES
    totals = [0.0, 0.0, 0.0]
    found = False
    cache_known = True
    size = 0
    sessions = 0
    seen: dict[str, tuple[float, float, float]] = {}
    for path in files[:MAX_LOG_FILES]:
        file_size = path.stat().st_size
        if size + file_size > MAX_LOG_BYTES:
            partial = True
            continue
        size += file_size
        last: tuple[float, float, float] | None = None
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    data = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(data, dict):
                    continue
                if provider == "anthropic":
                    message = data.get("message")
                    if data.get("type") != "assistant" or not isinstance(message, dict):
                        continue
                    usage = message.get("usage")
                    if not isinstance(usage, dict):
                        continue
                    values = [
                        numeric(usage.get(k))
                        for k in ("input_tokens", "output_tokens", "cache_read_input_tokens")
                    ]
                    if values[0] is None or values[1] is None:
                        continue
                    # Streaming revisions of one message replace earlier totals.
                    key = message.get("id") or data.get("uuid")
                    if not isinstance(key, str):
                        continue
                    if values[2] is None:
                        cache_known = False
                    seen[key] = (values[0], values[1], values[2] or 0)
                    last = seen[key]
                else:
                    payload = data.get("payload")
                    if not isinstance(payload, dict) or payload.get("type") != "token_count":
                        continue
                    info = payload.get("info")
                    usage = info.get("total_token_usage") if isinstance(info, dict) else None
                    if not isinstance(usage, dict):
                        continue
                    values = [
                        numeric(usage.get(k))
                        for k in ("input_tokens", "output_tokens", "cached_input_tokens")
                    ]
                    if values[0] is not None and values[1] is not None:
                        if values[2] is None:
                            cache_known = False
                        last = (values[0], values[1], values[2] or 0)
        if last is not None:
            sessions += 1
            found = True
            if provider == "codex":
                totals = [a + b for a, b in zip(totals, last, strict=True)]
    if provider == "anthropic":
        for values_tuple in seen.values():
            totals = [a + b for a, b in zip(totals, values_tuple, strict=True)]
    if not found:
        return None
    return {
        "sessions": sessions,
        "input_tokens": totals[0],
        "output_tokens": totals[1],
        "cached_tokens": totals[2] if cache_known else None,
        "source": "Local CLI session history",
        "coverage": "Recent history sample (scan limit reached)"
        if partial
        else "Saved sessions on this computer",
    }


class CliUsageService:
    """TTL-cached observations; refresh failures retain explicitly older evidence."""

    def __init__(
        self,
        *,
        home: Path | None = None,
        which: Callable[[str], str | None] = shutil.which,
        limits: Callable[[str, str], list[dict[str, Any]]] = read_limits,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.home = home if home is not None else Path.home()
        self.which = which
        self.limits = limits
        self.clock = clock
        self._cards: list[dict[str, Any]] = []
        self._last_read = 0.0
        self._last_limits = 0.0
        self._lock = threading.Lock()

    def _activity(self, provider: str) -> dict[str, Any] | None:
        try:
            if provider == "codex":
                root = Path(os.environ.get("CODEX_HOME", str(self.home / ".codex")))
                return _log_activity(root / "sessions", provider)
            if provider == "anthropic":
                root = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(self.home / ".claude")))
                return _log_activity(root / "projects", provider)
            paths = {
                "opencode": Path(os.environ.get("XDG_DATA_HOME", str(self.home / ".local/share")))
                / "opencode/opencode.db",
                "cline": self.home / ".cline/data/db/sessions.db",
            }
            return _sql_activity(paths[provider], provider) if provider in paths else None
        except (OSError, sqlite3.Error, ValueError):
            return None

    def snapshot(self, *, refresh: bool = False, force: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            moment = self.clock()
            query_limits = refresh and (force or moment - self._last_limits >= 60)
            if self._cards and not query_limits and not force and moment - self._last_read < 60:
                return self._cards
            previous = {card["provider"]: card for card in self._cards}
            executables = {provider: self.which(exe) for provider, exe, _ in CLIS}
            results: dict[str, list[dict[str, Any]] | Exception] = {}
            if query_limits:
                # Native metadata readers are independently bounded. One slow CLI
                # must not delay starting the other account queries.
                with ThreadPoolExecutor(max_workers=len(LIMIT_PROVIDERS)) as pool:
                    futures = {
                        provider: pool.submit(self.limits, executable, provider)
                        for provider, executable in executables.items()
                        if executable and provider in LIMIT_PROVIDERS
                    }
                    for provider, future in futures.items():
                        try:
                            results[provider] = future.result()
                        except (
                            OSError,
                            ValueError,
                            TimeoutError,
                            asyncio.IncompleteReadError,
                            asyncio.LimitOverrunError,
                            KeyError,
                        ) as exc:
                            results[provider] = exc
            cards: list[dict[str, Any]] = []
            observed = datetime.fromtimestamp(moment, UTC).isoformat()
            for provider, _exe, name in CLIS:
                executable = executables[provider]
                if provider == "gemini" and not executable:
                    continue
                old = previous.get(provider, {})
                windows = old.get("windows", []) if executable else []
                checked_at = old.get("checked_at")
                error = old.get("limits_error", "")
                if executable and provider in LIMIT_PROVIDERS and query_limits:
                    try:
                        result = results[provider]
                        if isinstance(result, Exception):
                            raise result
                        updated_windows = result
                        if updated_windows:
                            windows = updated_windows
                            checked_at = observed
                            error = ""
                        else:
                            error = "CLI returned no account limit evidence."
                    except GoUsageUnavailable as exc:
                        error = str(exc)
                    except (
                        OSError,
                        ValueError,
                        TimeoutError,
                        asyncio.IncompleteReadError,
                        asyncio.LimitOverrunError,
                        KeyError,
                    ):
                        error = "Could not refresh account limits. Check CLI login and version."
                hint = {
                    "anthropic": "Plan limits are shown by /usage inside Claude Code.",
                    "opencode": "Refresh to read OpenCode Go limits through your OpenCode CLI.",
                    "antigravity": "Model group limits are shown by /usage inside Antigravity CLI.",
                    "cline": "Account limits depend on the provider configured in Cline.",
                    "gemini": "Check /stats inside Gemini CLI.",
                }.get(provider, "Refresh to read account limits through the CLI.")
                cards.append(
                    {
                        "provider": provider,
                        "name": name,
                        "installed": bool(executable),
                        "windows": windows,
                        "checked_at": checked_at,
                        "limits_error": error,
                        "hint": hint,
                        "source": {
                            "anthropic": "Native CLI /usage panel",
                            "antigravity": "Native CLI /usage panel",
                            "opencode": "OpenCode Go account · via OpenCode CLI",
                        }.get(provider, "Native CLI account query"),
                        "activity": self._activity(provider) if executable else None,
                        "activity_checked_at": observed,
                    }
                )
            self._cards = cards
            self._last_read = moment
            if query_limits:
                self._last_limits = moment
            return cards
