"""The owner's on/off switch for agent access over MCP.

``orch mcp off`` (or the Setup page) lets the owner keep SaberOps registered in
every agent CLI while it offers nothing: the server lists no tools, refuses
calls with a plain message and never takes the planner lease, so the agent
works exactly as without SaberOps. ``orch mcp on`` restores it; running
sessions pick the change up on their next request.

The switch file sits next to the writer lease, so each profile has its own
switch and a test that points the lease at a temporary path is isolated.
Absent or unreadable means on.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

__all__ = ["SWITCH_FILENAME", "mcp_enabled", "set_mcp_enabled", "switch_path"]

SWITCH_FILENAME = "mcp-switch.json"


def switch_path(lease_path: Path | None = None) -> Path:
    if lease_path is None:
        from saberops.mcp.lease import default_lease_path

        lease_path = default_lease_path()
    return lease_path.parent / SWITCH_FILENAME


def mcp_enabled(lease_path: Path | None = None) -> bool:
    try:
        data = json.loads(switch_path(lease_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    return not (isinstance(data, dict) and data.get("enabled") is False)


def set_mcp_enabled(enabled: bool, lease_path: Path | None = None) -> Path:
    path = switch_path(lease_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(
        json.dumps({"enabled": bool(enabled), "changed_at": datetime.now(UTC).isoformat()})
        + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)
    return path
