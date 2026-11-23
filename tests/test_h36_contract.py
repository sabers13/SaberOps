"""H36: the MCP contract matches every surface the planner reads.

The planner meets the tool contract through the surfaces that must name the
same set: the server's ``tools/list`` (the registry and :data:`TOOL_NAMES`), the
built-in guide, the installed skill, the generated proxy definitions, and the
``orch mcp tools`` listing.

Later phase-1.1 lanes extend :data:`REMOVED_TOOLS` and :data:`ALIASES` as they
remove a tool or keep one as a one-release alias; wave 0 records the shape and
checks today's set.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from saberops.agent_connect import PROXIES, agent_document, skill_document
from saberops.cli import main as cli_main
from saberops.mcp.guide import GUIDE
from saberops.mcp.lease import OrchestratorLease
from saberops.mcp.tools import (
    FORBIDDEN_OPERATIONS,
    NATIVE_MUTATING_TOOLS,
    READ_TOOLS,
    TOOL_NAMES,
    ServerContext,
    build_default_registry,
)
from saberops.native_workers import NATIVE_TOOL_NAMES

pytestmark = [pytest.mark.packaged_routing_default]

#: Session switches the server adds beside the registry's tools.
SWITCH_TOOLS = frozenset({"saberops_on", "saberops_off"})


@dataclass(frozen=True)
class RemovedTool:
    """A tool a phase-1.1 lane removed; it must be absent from every surface."""

    name: str
    lane: str


@dataclass(frozen=True)
class Alias:
    """A tool kept as a one-release alias that prints its removal notice."""

    alias: str
    canonical: str
    notice: str
    arguments: Mapping[str, object] = field(default_factory=dict)


#: Empty in wave 0.  Add one row in the lane that removes a tool (H3, H7, H8,
#: H9, H17) and delete the row when the one-release window ends.
REMOVED_TOOLS: tuple[RemovedTool, ...] = ()

#: Empty in wave 0.  Add one row when a lane keeps an old name as an alias.
ALIASES: tuple[Alias, ...] = ()


def _registry() -> Any:
    # No owner store is touched: a nonexistent database and a private lease
    # path are enough to build the registry (mutating calls refuse as readers).
    return build_default_registry(
        ServerContext(
            db_path=Path("/nonexistent/orchestrator.db"),
            lease=OrchestratorLease(Path("/nonexistent/h36-lease.json")),
        )
    )


def _proxy_documents() -> list[str]:
    return [
        agent_document(slug, proxy.name)
        for slug, proxies in PROXIES.items()
        for proxy in proxies
    ]


def _surfaces() -> list[str]:
    return [GUIDE, skill_document(), *_proxy_documents()]


def _documented_tool_list() -> list[str]:
    """Parse the module docstring's ``exposed::`` literal block."""
    from saberops.mcp import tools

    after = (tools.__doc__ or "").split("exposed::", 1)[1]
    lines: list[str] = []
    for line in after.splitlines():
        if line.strip():
            lines.extend(line.split())
        elif lines:
            break
    return lines


def _notices(payload: object) -> list[str]:
    if isinstance(payload, Mapping):
        found = [str(value) for key, value in payload.items() if "notice" in str(key)]
        for value in payload.values():
            found.extend(_notices(value))
        return found
    if isinstance(payload, list):
        found = []
        for item in payload:
            found.extend(_notices(item))
        return found
    return []


def _without_notices(payload: object) -> object:
    if isinstance(payload, Mapping):
        return {
            key: _without_notices(value)
            for key, value in payload.items()
            if "notice" not in str(key)
        }
    if isinstance(payload, list):
        return [_without_notices(item) for item in payload]
    return payload


def test_registry_exposes_exactly_the_published_tool_set() -> None:
    names = set(_registry().names())
    assert names == set(TOOL_NAMES)
    assert names & set(NATIVE_TOOL_NAMES) == set(), "native tools are flag-gated"
    assert names & set(FORBIDDEN_OPERATIONS) == set()
    assert READ_TOOLS <= names
    assert NATIVE_MUTATING_TOOLS.isdisjoint(names)


def test_module_docstring_lists_every_published_tool() -> None:
    assert sorted(_documented_tool_list()) == sorted(TOOL_NAMES)


def test_guide_and_skill_name_every_published_tool() -> None:
    for surface in (GUIDE, skill_document()):
        missing = sorted(name for name in TOOL_NAMES if f"`{name}`" not in surface)
        assert missing == [], missing


def test_every_surface_uses_only_live_server_names() -> None:
    known = set(TOOL_NAMES) | set(NATIVE_TOOL_NAMES) | {"on", "off"}
    for surface in _surfaces():
        mentioned = set(re.findall(r"saberops_([a-z][a-z0-9_]*)", surface))
        assert mentioned <= known, mentioned - known


def test_every_published_tool_has_a_real_description() -> None:
    registry = _registry()
    for name in TOOL_NAMES:
        spec = registry.get(name)
        assert spec is not None, name
        assert len(spec.description.strip()) >= 20, (name, spec.description)
        assert spec.description.rstrip().endswith("."), (name, spec.description)


def test_removed_tools_are_absent_from_every_surface() -> None:
    names = set(_registry().names())
    for row in REMOVED_TOOLS:
        assert row.name not in TOOL_NAMES, row
        assert row.name not in names, row
        assert row.name not in NATIVE_TOOL_NAMES, row
        for surface in _surfaces():
            assert f"`{row.name}`" not in surface, (row.lane, row.name)
            assert f"saberops_{row.name}" not in surface, (row.lane, row.name)


def test_every_alias_prints_its_notice_and_still_works() -> None:
    registry = _registry()
    names = set(registry.names())
    for row in ALIASES:
        assert row.alias in names, row
        assert row.canonical in names, row
        alias_payload = registry.call(row.alias, dict(row.arguments))
        canonical_payload = registry.call(row.canonical, dict(row.arguments))
        assert any(row.notice in notice for notice in _notices(alias_payload)), (
            row,
            alias_payload,
        )
        assert _without_notices(alias_payload) == _without_notices(canonical_payload), row


def test_removed_and_alias_rows_are_consistent() -> None:
    removed = {row.name for row in REMOVED_TOOLS}
    aliases = {row.alias for row in ALIASES}
    canonicals = {row.canonical for row in ALIASES}
    assert removed.isdisjoint(aliases)
    assert canonicals <= set(TOOL_NAMES)
    for alias_row in ALIASES:
        assert alias_row.alias != alias_row.canonical
    for removed_row in REMOVED_TOOLS:
        assert removed_row.lane, removed_row


def test_cli_tool_list_matches_the_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for var in ("ORCH_DIR", "SABEROPS_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    argv = [
        "mcp",
        "tools",
        "--json",
        "--db",
        str(tmp_path / "mcp.db"),
        "--lease",
        str(tmp_path / "lease.json"),
        "--ledger",
        str(tmp_path / "events.jsonl"),
        "--questions-store",
        str(tmp_path / "questions.json"),
        "--config-ledger",
        str(tmp_path / "config.jsonl"),
        "--cwd",
        str(tmp_path),
    ]
    assert cli_main(argv) == 0
    payload = json.loads(capsys.readouterr().out)
    assert sorted(tool["name"] for tool in payload["tools"]) == sorted(TOOL_NAMES)
