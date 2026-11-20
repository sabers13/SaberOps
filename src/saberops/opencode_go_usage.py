"""Go account quotas, queried by an ephemeral plugin inside native OpenCode.

SaberOps never reads OpenCode auth files or receives an API key. The shipped
OpenCode 2 plugin asks OpenCode's own integration API for the active
credential, performs the single read-only usage request inside OpenCode, and
hands back only percentages and reset dates. The reader only starts a private
OpenCode server, asks it to load that plugin, and reads the plugin's file.

OpenCode's command-line surface changed in the 2.x line: ``models`` no longer
loads provider auth hooks, and the 1.x plugin auth hook shape is ignored.
This reader therefore drives the 2.x local API (``serve`` plus a read-only
``api`` call) and refuses a 1.x executable with a plain message instead of
launching a command that cannot work.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import signal
import socket
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

GO_TIMEOUT = 20.0
_VERSION_TIMEOUT = 5.0
_SERVE_TIMEOUT = 8.0
_CLIENT_TIMEOUT = 10.0

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")

_ERROR_MESSAGES: dict[str, str] = {
    "HTTP_401_EXPIRED": (
        "The saved OpenCode Go API key has expired. Create a new key in OpenCode"
        " Console and connect it again in OpenCode; SaberOps cannot renew it."
    ),
    "HTTP_401": (
        "OpenCode Go rejected the saved key. Reconnect OpenCode Go in the"
        " OpenCode CLI, then try again."
    ),
    "HTTP_403": (
        "The OpenCode account in use has no OpenCode Go subscription. Switch to"
        " the account that owns Go in OpenCode (opencode auth switch), then try"
        " again."
    ),
    "HTTP_404": "OpenCode's Go usage endpoint is unavailable right now. Try again later.",
    "HTTP_429": "OpenCode is rate-limiting its usage check. Try again in a few minutes.",
    "GO_NOT_CONNECTED": (
        "OpenCode has no saved OpenCode Go account. Connect OpenCode Go in the"
        " OpenCode CLI, then try again."
    ),
    "QUERY_UNAVAILABLE": (
        "OpenCode could not answer its own usage check. Open OpenCode, confirm"
        " it can use an OpenCode Go model, then try again."
    ),
}


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


def _require_opencode_two(executable: str) -> None:
    """Refuse a CLI whose usage surface this reader cannot drive."""
    try:
        proc = subprocess.run(
            [executable, "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_VERSION_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GoUsageUnavailable(
            "OpenCode could not be started to read Go limits."
        ) from exc
    match = _VERSION_RE.search(proc.stdout.decode("utf-8", "replace"))
    if match is None:
        raise GoUsageUnavailable(
            "OpenCode did not report its version, so its Go limits cannot be"
            " read. Update OpenCode, then try again."
        )
    if int(match.group(1)) < 2:
        raise GoUsageUnavailable(
            "SaberOps reads Go limits through OpenCode 2. Update OpenCode, then"
            " try again."
        )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_until_listening(port: int, process: subprocess.Popen[bytes], deadline: float) -> None:
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise GoUsageUnavailable(
                "OpenCode's local usage service stopped before it was ready. Try"
                " again."
            )
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.2)
            try:
                probe.connect(("127.0.0.1", port))
                return
            except OSError:
                time.sleep(0.05)
    raise GoUsageUnavailable("OpenCode's local usage service did not start in time.")


def _stop(process: subprocess.Popen[bytes]) -> None:
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


def read_go_limits(executable: str) -> list[dict[str, Any]]:
    from saberops.workers.env import hermetic_env
    from saberops.workers.opencode_isolation import (
        OpenCodeIsolationUnavailable,
        opencode_isolation,
        opencode_isolation_policies,
    )

    _require_opencode_two(executable)
    plugin = Path(__file__).parent / "defaults/opencode_go_usage"
    with tempfile.TemporaryDirectory(prefix="saberops-go-usage-") as cwd:
        output = Path(cwd) / "usage.json"
        child_base = hermetic_env("opencode-go", None)
        # Config/plugin isolation for the quota reader: a private config/home
        # root with project/ancestor discovery disabled (inline deny policies
        # block every plugin, MCP server and skill), except SaberOps' own
        # shipped usage plugin, which is allowed by its exact resolved path
        # as the last policy so the loader still runs it.  Refuse before
        # launch when isolation cannot be established.
        try:
            isolation = opencode_isolation(child_base)
        except OpenCodeIsolationUnavailable as exc:
            raise GoUsageUnavailable(
                "OpenCode account usage cannot be isolated on this host."
            ) from exc
        with isolation:
            plugin_path = str(plugin.resolve())
            base_config = {
                "plugins": [plugin_path],
                "enabled_providers": ["opencode-go"],
            }
            inline = isolation.config(base_config, deny_integrations=False)
            inline["policies"] = [
                *opencode_isolation_policies(),
                {
                    "action": "integration.use",
                    "resource": f"plugin:{plugin_path}",
                    "effect": "allow",
                },
            ]
            env = isolation.env_for(child_base, config=inline)
            env["SABEROPS_GO_USAGE_OUTPUT"] = str(output)
            env["OPENCODE_PASSWORD"] = secrets.token_urlsafe(32)
            port = _free_port()
            url = f"http://127.0.0.1:{port}"
            # The 2.x CLI loads a location's plugins only when a request boots
            # that location, so a private server is started and asked for the
            # plugin list.  Popen is asynchronous: the owned root must stay
            # alive through spawn, wait, timeout terminate/kill and final
            # reap, and is only removed after the child is reaped.
            server = subprocess.Popen(
                [executable, "serve", "--hostname", "127.0.0.1", "--port", str(port)],
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                _wait_until_listening(
                    port, server, time.monotonic() + min(_SERVE_TIMEOUT, GO_TIMEOUT)
                )
                try:
                    client = subprocess.run(
                        [executable, "api", "--server", url, "GET", "/api/plugin"],
                        cwd=cwd,
                        env=env,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=min(_CLIENT_TIMEOUT, GO_TIMEOUT),
                    )
                except subprocess.TimeoutExpired as exc:
                    raise GoUsageUnavailable(
                        "OpenCode Go quota query timed out."
                    ) from exc
                if client.returncode != 0:
                    raise GoUsageUnavailable(
                        "OpenCode could not load its usage plugin. Restart"
                        " OpenCode, then try again."
                    )
                deadline = time.monotonic() + GO_TIMEOUT
                while not output.is_file() and time.monotonic() < deadline:
                    time.sleep(0.05)
                if not output.is_file():
                    raise GoUsageUnavailable(
                        "OpenCode did not finish its usage check in time. Try again."
                    )
            finally:
                _stop(server)
        if not output.is_file():
            raise GoUsageUnavailable(
                "OpenCode did not run its usage plugin. Restart OpenCode, then try"
                " again."
            )
        if output.stat().st_size > 65536:
            raise GoUsageUnavailable("OpenCode Go returned an invalid quota reply.")
        try:
            payload = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise GoUsageUnavailable(
                "OpenCode Go returned an invalid quota reply."
            ) from exc
        error = payload.get("error") if isinstance(payload, dict) else "QUERY_UNAVAILABLE"
        if error and not isinstance(error, str):
            error = "QUERY_UNAVAILABLE"
        if error:
            raise GoUsageUnavailable(
                _ERROR_MESSAGES.get(
                    error, "Could not read OpenCode Go account limits. Try again."
                )
            )
        return parse_go_limits(payload)
