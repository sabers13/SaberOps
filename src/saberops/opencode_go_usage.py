"""Go account quotas, queried by an ephemeral plugin inside native OpenCode.

SaberOps never reads OpenCode auth files or receives an API key. The public
plugin auth loader supplies auth to OpenCode's own read-only usage hook.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

GO_TIMEOUT = 20.0


class GoUsageUnavailable(ValueError):
    """Only fixed, safe user-facing messages; never native error payloads."""


def parse_go_limits(payload: Any) -> list[dict[str, Any]]:
    from saberops.cli_usage import numeric

    if not isinstance(payload, dict) or not isinstance(payload.get("usage"), dict):
        return []
    rows = []
    for name, label, duration in (
        ("rolling", "OpenCode Go · 5 hour", 300),
        ("weekly", "OpenCode Go · Weekly", 10080),
        ("monthly", "OpenCode Go · Monthly", None),
    ):
        window = payload["usage"].get(name)
        if not isinstance(window, dict):
            continue
        used = numeric(window.get("percent"))
        if used is None:
            continue
        reset = window.get("resetsAt")
        reset_at = None
        if isinstance(reset, str) and len(reset) <= 64:
            try:
                parsed = datetime.fromisoformat(reset.replace("Z", "+00:00"))
                if parsed.tzinfo is not None:
                    reset_at = reset
            except ValueError:
                pass
        rows.append(
            {
                "label": label,
                "used": used,
                "remaining": max(0, 100 - used),
                "duration": duration,
                "reset_at": reset_at,
                "unlimited": False,
            }
        )
    return rows


def read_go_limits(executable: str) -> list[dict[str, Any]]:
    from saberops.workers.env import hermetic_env

    plugin = Path(__file__).parent / "defaults/opencode_go_usage.mjs"
    with tempfile.TemporaryDirectory(prefix="saberops-go-usage-") as cwd:
        output = Path(cwd) / "usage.json"
        child_base = hermetic_env("opencode-go", None)
        env = {
            **child_base,
            "SABEROPS_GO_USAGE_OUTPUT": str(output),
            "OPENCODE_CONFIG_CONTENT": json.dumps(
                {
                    "plugin": [plugin.resolve().as_uri()],
                    "enabled_providers": ["opencode-go"],
                }
            ),
        }
        # Model enumeration initializes provider auth hooks without an
        # inference session, prompt, or change to the user's saved config.
        process = subprocess.Popen(
            [executable, "models", "opencode-go"],
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            process.wait(timeout=GO_TIMEOUT)
        except subprocess.TimeoutExpired as exc:
            raise GoUsageUnavailable("OpenCode Go quota query timed out.") from exc
        finally:
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            elif process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                if os.name == "posix":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    process.kill()
                process.wait()
        if not output.is_file():
            raise GoUsageUnavailable(
                "Connect OpenCode Go in the OpenCode CLI to read account limits."
            )
        if output.stat().st_size > 65536:
            raise GoUsageUnavailable("OpenCode Go returned an invalid quota reply.")
        payload = json.loads(output.read_text())
        error = payload.get("error") if isinstance(payload, dict) else "QUERY_UNAVAILABLE"
        if error and not isinstance(error, str):
            error = "QUERY_UNAVAILABLE"
        if error:
            message = {
                "HTTP_401": "OpenCode Go rejected its saved key. Reconnect Go in OpenCode.",
                "HTTP_403": "No OpenCode Go subscription is available for the connected account.",
                "HTTP_404": "The OpenCode Go account usage endpoint is unavailable.",
                "GO_NOT_CONNECTED": (
                    "Connect OpenCode Go in the OpenCode CLI to read account limits."
                ),
            }.get(error, "Could not read OpenCode Go account limits. Try refreshing again.")
            raise GoUsageUnavailable(message)
        return parse_go_limits(payload)
