"""A compact structural map of the repository, small enough to read in full.

The map exists so an agent can understand the shape of the repository without
opening it: logical modules, the files they own, top-level declarations, entry
points, and the dependency edges that reveal the architectural hubs.  Function
bodies are never included -- the map describes structure, not implementation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from orchestrator_mvp.devscope.architecture import ArchitectureReport, check_architecture
from orchestrator_mvp.devscope.graph import SourceGraph

#: Fan-in at or above which a source file is treated as an architectural hub.
HUB_FAN_IN_THRESHOLD = 10


def hub_files(
    graph: SourceGraph, threshold: int = HUB_FAN_IN_THRESHOLD
) -> tuple[tuple[str, int], ...]:
    """Return production files whose direct consumer count reaches ``threshold``.

    Hubs are derived from evidence, never from a hard-coded name list: a file
    every module imports is broad-impact whatever it happens to be called.
    """
    scored = [(path, graph.fan_in(path)) for path in graph.paths]
    hubs = [(path, count) for path, count in scored if count >= threshold]
    return tuple(sorted(hubs, key=lambda item: (-item[1], item[0])))


def top_fan_in_files(graph: SourceGraph, limit: int = 10) -> tuple[tuple[str, int], ...]:
    """Return the ``limit`` most depended-upon production files."""
    scored = sorted(
        ((path, graph.fan_in(path)) for path in graph.paths),
        key=lambda item: (-item[1], item[0]),
    )
    return tuple(scored[:limit])


def _file_entry(graph: SourceGraph, path: str) -> dict[str, Any]:
    source = graph.files[path]
    return {
        "path": source.path,
        "module": source.dotted,
        "classes": [symbol.name for symbol in source.classes if symbol.is_public],
        "functions": [symbol.name for symbol in source.functions if symbol.is_public],
        "enums": list(source.enums),
        "constants": list(source.constants),
        "exports": list(source.exports),
        "entry_points": list(source.entry_points),
        "fan_in": graph.fan_in(path),
        "fan_out": graph.fan_out(path),
    }


def build_repo_map(root: Path, report: ArchitectureReport | None = None) -> dict[str, Any]:
    """Build the canonical, JSON-ready repository map.

    Every collection is sorted, so serialising the same tree twice produces
    byte-identical output.
    """
    report = report if report is not None else check_architecture(root)
    graph = report.graph
    ownership = report.ownership

    modules: list[dict[str, Any]] = []
    for manifest in report.manifests:
        owned = tuple(
            path for path, owner in ownership.owner_by_path.items() if owner == manifest.name
        )
        outgoing = sorted({edge.target for edge in report.edges if edge.source == manifest.name})
        incoming = sorted({edge.source for edge in report.edges if edge.target == manifest.name})
        modules.append(
            {
                "name": manifest.name,
                "description": manifest.description,
                "risk": manifest.risk,
                "file_count": len(owned),
                "files": [_file_entry(graph, path) for path in owned],
                "public_interface": list(manifest.interface_names),
                "entry_points": sorted(
                    {point for path in owned for point in graph.files[path].entry_points}
                ),
                "depends_on": outgoing,
                "depended_on_by": incoming,
                "fan_out": len(outgoing),
                "fan_in": len(incoming),
                "tests": list(manifest.test_paths),
                "context_docs": list(manifest.context_docs),
            }
        )

    return {
        "schema": "devscope/repo-map/1",
        "modules": modules,
        "module_edges": [
            {"source": edge.source, "target": edge.target, "state": edge.state}
            for edge in report.edges
        ],
        "hubs": [{"path": path, "fan_in": count} for path, count in hub_files(graph)],
        "top_fan_in_files": [
            {"path": path, "fan_in": count} for path, count in top_fan_in_files(graph)
        ],
        "totals": {
            "modules": len(modules),
            "production_files": len(ownership.production_paths),
            "file_edges": len(graph.edges),
            "module_edges": len(report.edges),
        },
    }


def repo_map_json(repo_map: dict[str, Any]) -> str:
    """Serialise a repository map canonically."""
    return json.dumps(repo_map, indent=2, sort_keys=True) + "\n"


def render_repo_map(repo_map: dict[str, Any]) -> str:
    """Render the compact human/LLM-readable form of the repository map."""
    lines: list[str] = []
    totals = repo_map["totals"]
    lines.append(
        f"modules: {totals['modules']}  files: {totals['production_files']}  "
        f"module_edges: {totals['module_edges']}"
    )
    for module in repo_map["modules"]:
        lines.append("")
        lines.append(f"[{module['name']}] risk={module['risk']} files={module['file_count']}")
        lines.append(f"  depends_on: {', '.join(module['depends_on']) or '-'}")
        lines.append(f"  used_by: {', '.join(module['depended_on_by']) or '-'}")
        lines.append(f"  interface: {', '.join(module['public_interface']) or '-'}")
        for entry in module["files"]:
            symbols = [*entry["classes"], *entry["functions"]]
            head = ", ".join(symbols[:8])
            more = f" (+{len(symbols) - 8})" if len(symbols) > 8 else ""
            lines.append(
                f"  {entry['path']}  in={entry['fan_in']} out={entry['fan_out']}  {head}{more}"
            )
    if repo_map["hubs"]:
        lines.append("")
        lines.append("hubs:")
        for hub in repo_map["hubs"]:
            lines.append(f"  {hub['path']}  fan_in={hub['fan_in']}")
    return "\n".join(lines) + "\n"
