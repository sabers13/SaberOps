"""Architecture validation: declared contracts versus the AST evidence.

The checker answers one question deterministically -- does the repository's
actual internal dependency graph stay inside what the module contracts permit?
Three dependency states are distinguished:

``allowed``
    the intended long-term architecture.

``transitional``
    real, currently required, and explicitly recorded as debt.  Reported apart
    from allowed edges so debt cannot quietly become the target architecture.

``forbidden``
    an actual edge no contract declares.  This fails validation; devscope never
    invents an owner or a permission to make a failure disappear.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestrator_mvp.devscope.graph import SourceGraph, build_source_graph
from orchestrator_mvp.devscope.manifest import ModuleManifest, load_manifests
from orchestrator_mvp.devscope.owners import OwnershipReport, resolve_ownership
from orchestrator_mvp.devscope.paths import ROOT_AGENTS_DOC

#: Finding codes, ordered by report precedence.
FINDING_CODES = (
    "unowned_production_path",
    "multiple_owners",
    "forbidden_dependency",
    "missing_context_doc",
    "unresolved_interface_source",
    "unresolved_interface_name",
    "missing_root_agents_doc",
    "undeclared_transitional_dependency",
)


@dataclass(frozen=True)
class Finding:
    """One deterministic architecture violation."""

    code: str
    subject: str
    message: str

    def render(self) -> str:
        """Compact single-line rendering for CLI output."""
        return f"{self.code}: {self.subject}: {self.message}"


@dataclass(frozen=True)
class ModuleEdge:
    """One logical-module dependency edge backed by concrete source evidence."""

    source: str
    target: str
    state: str
    evidence: tuple[tuple[str, str], ...]

    @property
    def key(self) -> tuple[str, str]:
        """Sortable identity of the edge."""
        return (self.source, self.target)


@dataclass(frozen=True)
class ArchitectureReport:
    """The full outcome of one architecture check."""

    manifests: tuple[ModuleManifest, ...]
    ownership: OwnershipReport
    graph: SourceGraph
    edges: tuple[ModuleEdge, ...]
    findings: tuple[Finding, ...]

    @property
    def status(self) -> str:
        """``PASS`` when no finding was produced."""
        return "PASS" if not self.findings else "FAIL"

    @property
    def module_names(self) -> tuple[str, ...]:
        """Every declared logical module, sorted."""
        return tuple(manifest.name for manifest in self.manifests)

    def edges_in_state(self, state: str) -> tuple[ModuleEdge, ...]:
        """Every module edge currently classified as ``state``."""
        return tuple(edge for edge in self.edges if edge.state == state)


def module_edges(
    graph: SourceGraph,
    ownership: OwnershipReport,
    manifests: tuple[ModuleManifest, ...],
) -> tuple[ModuleEdge, ...]:
    """Collapse file-level imports into classified logical-module edges."""
    by_name = {manifest.name: manifest for manifest in manifests}
    grouped: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for src_path, dst_path in graph.edges:
        src_module = ownership.owner_of(src_path)
        dst_module = ownership.owner_of(dst_path)
        if src_module is None or dst_module is None or src_module == dst_module:
            continue
        grouped.setdefault((src_module, dst_module), []).append((src_path, dst_path))

    edges: list[ModuleEdge] = []
    for (src_module, dst_module), evidence in sorted(grouped.items()):
        manifest = by_name[src_module]
        if dst_module in manifest.allowed_dependencies:
            state = "allowed"
        elif dst_module in manifest.transitional_dependencies:
            state = "transitional"
        else:
            state = "forbidden"
        edges.append(
            ModuleEdge(
                source=src_module,
                target=dst_module,
                state=state,
                evidence=tuple(sorted(evidence)),
            )
        )
    return tuple(edges)


def _interface_findings(
    root: Path, manifest: ModuleManifest, graph: SourceGraph
) -> list[Finding]:
    findings: list[Finding] = []
    declared_names: set[str] = set()
    for source in manifest.interface_sources:
        analysed = graph.files.get(source)
        if analysed is None:
            if not (root / source).exists():
                findings.append(
                    Finding(
                        "unresolved_interface_source",
                        manifest.source,
                        f"declared public-interface source does not exist: {source}",
                    )
                )
            continue
        declared_names.update(analysed.public_symbols)
        declared_names.update(analysed.constants)
        declared_names.update(analysed.exports)

    if manifest.interface_sources and declared_names:
        for name in manifest.interface_names:
            if name not in declared_names:
                findings.append(
                    Finding(
                        "unresolved_interface_name",
                        manifest.source,
                        f"declared public interface '{name}' is not defined in "
                        f"any declared interface source",
                    )
                )
    return findings


def check_architecture(root: Path) -> ArchitectureReport:
    """Validate declared module contracts against actual repository evidence.

    Manifest-level corruption raises :class:`~.manifest.ManifestError` before
    any analysis runs; everything else is reported as a finding so a single
    invocation surfaces the complete picture.
    """
    manifests = load_manifests(root)
    graph = build_source_graph(root)
    ownership = resolve_ownership(root, manifests)
    edges = module_edges(graph, ownership, manifests)

    findings: list[Finding] = []
    for path in ownership.unowned:
        findings.append(
            Finding(
                "unowned_production_path",
                path,
                "no module contract declares this production path",
            )
        )
    for path, owners in ownership.overlapping:
        findings.append(
            Finding(
                "multiple_owners",
                path,
                f"claimed by {len(owners)} modules: {', '.join(owners)}",
            )
        )
    for edge in edges:
        if edge.state == "forbidden":
            example = edge.evidence[0]
            findings.append(
                Finding(
                    "forbidden_dependency",
                    f"{edge.source} -> {edge.target}",
                    f"actual dependency is neither allowed nor transitional "
                    f"(e.g. {example[0]} imports {example[1]})",
                )
            )

    if not (root / ROOT_AGENTS_DOC).is_file():
        findings.append(
            Finding(
                "missing_root_agents_doc",
                ROOT_AGENTS_DOC,
                "root agent instructions are missing",
            )
        )

    declared_states = {(edge.source, edge.target): edge.state for edge in edges}
    for manifest in manifests:
        for doc in manifest.context_docs:
            if not (root / doc).is_file():
                findings.append(
                    Finding(
                        "missing_context_doc",
                        manifest.source,
                        f"referenced context document does not exist: {doc}",
                    )
                )
        findings.extend(_interface_findings(root, manifest, graph))
        for target in manifest.transitional_dependencies:
            if declared_states.get((manifest.name, target)) is None:
                findings.append(
                    Finding(
                        "undeclared_transitional_dependency",
                        manifest.source,
                        f"transitional dependency on '{target}' no longer exists in "
                        f"the source graph and should be removed",
                    )
                )

    order = {code: index for index, code in enumerate(FINDING_CODES)}
    findings.sort(key=lambda item: (order.get(item.code, len(order)), item.subject, item.message))
    return ArchitectureReport(
        manifests=manifests,
        ownership=ownership,
        graph=graph,
        edges=edges,
        findings=tuple(findings),
    )


def report_to_dict(report: ArchitectureReport) -> dict[str, Any]:
    """Render an architecture report as deterministic JSON-ready data."""
    return {
        "status": report.status,
        "modules": list(report.module_names),
        "production_paths": len(report.ownership.production_paths),
        "owned_paths": len(report.ownership.owner_by_path),
        "unowned": list(report.ownership.unowned),
        "overlapping": [
            {"path": path, "modules": list(owners)} for path, owners in report.ownership.overlapping
        ],
        "dependency_edges": {
            state: [
                {
                    "source": edge.source,
                    "target": edge.target,
                    "evidence": [list(item) for item in edge.evidence],
                }
                for edge in report.edges_in_state(state)
            ]
            for state in ("allowed", "transitional", "forbidden")
        },
        "findings": [
            {"code": finding.code, "subject": finding.subject, "message": finding.message}
            for finding in report.findings
        ],
    }
