"""Language-independent project graph and scope contracts.

The graph is the durable, serializable evidence layer every other answer in
this package rests on.  It must be deterministic: identical repository state
plus identical analyzer configuration produces an identical canonical JSON
rendering and therefore an identical digest, regardless of insertion or
directory-walk ordering.  Nothing here reads secrets or contacts a model,
provider or network.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

#: Schema version of the canonical project graph JSON rendering.
GRAPH_SCHEMA_VERSION = 1

#: Version of the deterministic analyzer fleet (bumped when inference changes).
ANALYZER_VERSION = 1


def canonical_json(payload: object) -> str:
    """Render ``payload`` as stable canonical JSON (sorted keys, compact)."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest_of(payload: object) -> str:
    """Return the sha256 hex digest of the canonical JSON rendering."""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


class GraphSource(StrEnum):
    """Where a project graph came from, strongest evidence first."""

    EXPLICIT = "explicit"
    F01 = "f01"
    INFERRED = "inferred"
    FALLBACK = "fallback"


class Confidence(StrEnum):
    """How much the graph evidence can be trusted for narrowing decisions."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProjectModule:
    """One logical module with enough information for runtime scoping."""

    id: str
    roots: tuple[str, ...]
    owned_files: tuple[str, ...]
    interface_files: tuple[str, ...] = ()
    test_files: tuple[str, ...] = ()
    direct_dependencies: tuple[str, ...] = ()
    allowed_dependencies: tuple[str, ...] = ()
    entry_points: tuple[str, ...] = ()
    origin: str = "inferred"

    def to_dict(self) -> dict[str, object]:
        """JSON-ready rendering with sorted, deterministic collections.

        Every collection here is set-like: insertion/discovery order carries
        no semantic meaning, so the canonical rendering sorts each one.  This
        is what makes the digest genuinely order-independent, not merely
        JSON-round-trip-independent.
        """
        return {
            "id": self.id,
            "roots": sorted(self.roots),
            "owned_files": sorted(self.owned_files),
            "interface_files": sorted(self.interface_files),
            "test_files": sorted(self.test_files),
            "direct_dependencies": sorted(self.direct_dependencies),
            "allowed_dependencies": sorted(self.allowed_dependencies),
            "entry_points": sorted(self.entry_points),
            "origin": self.origin,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ProjectModule:
        """Reconstruct a module from its canonical rendering."""
        return cls(
            id=str(payload["id"]),
            roots=tuple(str(item) for item in payload.get("roots", ())),
            owned_files=tuple(str(item) for item in payload.get("owned_files", ())),
            interface_files=tuple(str(item) for item in payload.get("interface_files", ())),
            test_files=tuple(str(item) for item in payload.get("test_files", ())),
            direct_dependencies=tuple(
                str(item) for item in payload.get("direct_dependencies", ())
            ),
            allowed_dependencies=tuple(
                str(item) for item in payload.get("allowed_dependencies", ())
            ),
            entry_points=tuple(str(item) for item in payload.get("entry_points", ())),
            origin=str(payload.get("origin", "inferred")),
        )



@dataclass(frozen=True)
class ProjectGraph:
    """A deterministic, serializable graph of a managed project."""

    project_id: str
    base_revision: str
    analyzer: str
    graph_source: GraphSource
    confidence: Confidence
    modules: tuple[ProjectModule, ...]
    files: tuple[str, ...]
    unowned_files: tuple[str, ...]
    ambiguous_files: tuple[tuple[str, tuple[str, ...]], ...] = ()
    edges: tuple[tuple[str, str], ...] = ()
    edge_evidence: tuple[tuple[str, str, str], ...] = ()
    test_files: tuple[str, ...] = ()
    entry_points: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    files_scanned: int = 0
    python_files_parsed: int = 0
    _digest: str | None = field(default=None, repr=False, compare=False)

    @property
    def module_ids(self) -> tuple[str, ...]:
        """Every module id, sorted."""
        return tuple(sorted(module.id for module in self.modules))

    def module_by_id(self, module_id: str) -> ProjectModule | None:
        """Return the module with ``module_id``, or ``None``."""
        return next((module for module in self.modules if module.id == module_id), None)

    def owner_of(self, relative_path: str) -> str | None:
        """Return the single owning module of ``relative_path``, or ``None``."""
        owners = self.owners_of(relative_path)
        return owners[0] if len(owners) == 1 else None

    def owners_of(self, relative_path: str) -> tuple[str, ...]:
        """Return every module claiming ``relative_path`` (sorted)."""
        return tuple(
            sorted(
                module.id for module in self.modules if relative_path in module.owned_files
            )
        )

    def dependents_of(self, module_id: str) -> tuple[str, ...]:
        """Modules that directly depend on ``module_id`` (reverse edges), sorted."""
        return tuple(sorted({src for src, dst in self.edges if dst == module_id}))

    def dependencies_of(self, module_id: str) -> tuple[str, ...]:
        """Modules that ``module_id`` directly depends on, sorted."""
        module = self.module_by_id(module_id)
        if module is not None:
            return module.direct_dependencies
        return tuple(sorted({dst for src, dst in self.edges if src == module_id}))

    def transitive_dependents(self, seeds: tuple[str, ...]) -> dict[str, int]:
        """Every transitive consumer of ``seeds`` mapped to shortest depth."""
        depths: dict[str, int] = dict.fromkeys(seeds, 0)
        frontier = sorted(seeds)
        depth = 0
        while frontier:
            depth += 1
            nxt: list[str] = []
            for name in frontier:
                for consumer in self.dependents_of(name):
                    if consumer not in depths:
                        depths[consumer] = depth
                        nxt.append(consumer)
            frontier = sorted(nxt)
        return depths

    def to_dict(self) -> dict[str, object]:
        """Canonical JSON-ready rendering.  Ordering never affects the digest.

        Every set-like collection (``modules``, ``files``, ``unowned_files``,
        ``ambiguous_files``, ``edges``, ``edge_evidence``, ``test_files``,
        ``entry_points``) is sorted at render time, independent of insertion
        or directory-walk order.  ``edges``/``edge_evidence`` sort the outer
        collection only -- the (src, dst[, evidence]) order *within* one edge
        is semantic and is left untouched.  ``reasons`` is an ordered
        narrative, not a set, and is intentionally left as authored.
        """
        return {
            "schema_version": GRAPH_SCHEMA_VERSION,
            "project_id": self.project_id,
            "base_revision": self.base_revision,
            "analyzer": self.analyzer,
            "analyzer_version": ANALYZER_VERSION,
            "graph_source": self.graph_source.value,
            "confidence": self.confidence.value,
            "modules": [module.to_dict() for module in sorted(self.modules, key=lambda m: m.id)],
            "files": sorted(self.files),
            "unowned_files": sorted(self.unowned_files),
            "ambiguous_files": [
                [path, sorted(owners)]
                for path, owners in sorted(self.ambiguous_files, key=lambda item: item[0])
            ],
            "edges": [list(edge) for edge in sorted(self.edges)],
            "edge_evidence": [list(item) for item in sorted(self.edge_evidence)],
            "test_files": sorted(self.test_files),
            "entry_points": sorted(self.entry_points),
            "reasons": list(self.reasons),
            "files_scanned": self.files_scanned,
            "python_files_parsed": self.python_files_parsed,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ProjectGraph:
        """Reconstruct a graph from its canonical rendering."""
        return cls(
            project_id=str(payload["project_id"]),
            base_revision=str(payload.get("base_revision", "")),
            analyzer=str(payload["analyzer"]),
            graph_source=GraphSource(str(payload["graph_source"])),
            confidence=Confidence(str(payload["confidence"])),
            modules=tuple(
                ProjectModule.from_dict(item)
                for item in payload.get("modules", ())
                if isinstance(item, dict)
            ),
            files=tuple(str(item) for item in payload.get("files", ())),
            unowned_files=tuple(str(item) for item in payload.get("unowned_files", ())),
            ambiguous_files=tuple(
                (str(item[0]), tuple(str(owner) for owner in item[1]))
                for item in payload.get("ambiguous_files", ())
                if isinstance(item, list) and len(item) == 2
            ),
            edges=tuple(
                (str(edge[0]), str(edge[1]))
                for edge in payload.get("edges", ())
                if isinstance(edge, list) and len(edge) == 2
            ),
            edge_evidence=tuple(
                (str(item[0]), str(item[1]), str(item[2]))
                for item in payload.get("edge_evidence", ())
                if isinstance(item, list) and len(item) == 3
            ),
            test_files=tuple(str(item) for item in payload.get("test_files", ())),
            entry_points=tuple(str(item) for item in payload.get("entry_points", ())),
            reasons=tuple(str(item) for item in payload.get("reasons", ())),
            files_scanned=int(payload.get("files_scanned", 0)),
            python_files_parsed=int(payload.get("python_files_parsed", 0)),
        )

    @property
    def digest(self) -> str:
        """Stable digest of the canonical graph rendering."""
        return digest_of(self.to_dict())


@dataclass(frozen=True)
class ScopeBudget:
    """Deterministic limits on a single bounded scope pack (F01 philosophy)."""

    max_source_files: int = 12
    max_test_files: int = 6
    dependency_depth: int = 1

    def as_dict(self) -> dict[str, int]:
        """JSON-ready view of the budget."""
        return {
            "max_source_files": self.max_source_files,
            "max_test_files": self.max_test_files,
            "dependency_depth": self.dependency_depth,
        }


@dataclass(frozen=True)
class ProjectScope:
    """The resolved scope of one task against one frozen project graph."""

    graph_digest: str
    graph_source: str
    analyzer: str
    task_digest: str
    task_slug: str
    primary_module: str | None
    included_modules: tuple[str, ...]
    confidence: str
    source_paths: tuple[str, ...]
    dependency_interface_paths: tuple[str, ...]
    test_paths: tuple[str, ...]
    closure_complete: bool
    truncated: bool
    reasons: tuple[str, ...]
    verification_level: str
    ambiguous_modules: tuple[str, ...] = ()
    budget: ScopeBudget = field(default_factory=ScopeBudget)

    def to_dict(self) -> dict[str, object]:
        """Canonical JSON-ready rendering.

        ``included_modules``, ``source_paths``, ``dependency_interface_paths``,
        ``test_paths`` and ``ambiguous_modules`` are set-like: they are sorted
        here so the digest never depends on resolution/insertion order.
        ``reasons`` is an ordered narrative and is left as authored.
        """
        return {
            "schema_version": GRAPH_SCHEMA_VERSION,
            "graph_digest": self.graph_digest,
            "graph_source": self.graph_source,
            "analyzer": self.analyzer,
            "task_digest": self.task_digest,
            "task_slug": self.task_slug,
            "primary_module": self.primary_module,
            "included_modules": sorted(self.included_modules),
            "confidence": self.confidence,
            "source_paths": sorted(self.source_paths),
            "dependency_interface_paths": sorted(self.dependency_interface_paths),
            "test_paths": sorted(self.test_paths),
            "closure_complete": self.closure_complete,
            "truncated": self.truncated,
            "reasons": list(self.reasons),
            "verification_level": self.verification_level,
            "ambiguous_modules": sorted(self.ambiguous_modules),
            "budget": self.budget.as_dict(),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ProjectScope:
        """Reconstruct a scope from its canonical rendering."""
        budget_raw = payload.get("budget", {})
        budget = (
            ScopeBudget(
                max_source_files=int(budget_raw.get("max_source_files", 12)),
                max_test_files=int(budget_raw.get("max_test_files", 6)),
                dependency_depth=int(budget_raw.get("dependency_depth", 1)),
            )
            if isinstance(budget_raw, dict)
            else ScopeBudget()
        )
        primary = payload.get("primary_module")
        return cls(
            graph_digest=str(payload["graph_digest"]),
            graph_source=str(payload["graph_source"]),
            analyzer=str(payload["analyzer"]),
            task_digest=str(payload["task_digest"]),
            task_slug=str(payload["task_slug"]),
            primary_module=str(primary) if primary is not None else None,
            included_modules=tuple(str(item) for item in payload.get("included_modules", ())),
            confidence=str(payload["confidence"]),
            source_paths=tuple(str(item) for item in payload.get("source_paths", ())),
            dependency_interface_paths=tuple(
                str(item) for item in payload.get("dependency_interface_paths", ())
            ),
            test_paths=tuple(str(item) for item in payload.get("test_paths", ())),
            closure_complete=bool(payload.get("closure_complete", False)),
            truncated=bool(payload.get("truncated", False)),
            reasons=tuple(str(item) for item in payload.get("reasons", ())),
            verification_level=str(payload.get("verification_level", "G4")),
            ambiguous_modules=tuple(str(item) for item in payload.get("ambiguous_modules", ())),
            budget=budget,
        )

    @property
    def digest(self) -> str:
        """Stable digest of the canonical scope rendering."""
        return digest_of(self.to_dict())
