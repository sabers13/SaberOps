"""Project graph construction: explicit metadata outranks inference.

Precedence (strongest first):

1. valid explicit ``.orch`` metadata (fail-closed: overlaps, stale roots and
   forbidden dependencies are deterministic errors, never silently repaired);
2. an F01-style ``architecture/modules`` layout (adapter, no duplicate truth);
3. deterministic Python-analyzer inference (read-only, never mutates the
   repository);
4. a conservative fallback that claims nothing about dependency closure.

The builder is read-only.  It never creates ``.orch`` metadata, moves files,
renames packages or rewrites imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from orchestrator_mvp.devscope.paths import match_any
from orchestrator_mvp.project_scope.contracts import (
    Confidence,
    GraphSource,
    ProjectGraph,
    ProjectModule,
)
from orchestrator_mvp.project_scope.metadata import (
    PROJECT_FILE,
    ExplicitModule,
    ProjectMetadata,
    ProjectMetadataError,
    load_project_metadata,
)
from orchestrator_mvp.project_scope.python_analyzer import (
    PythonAnalysis,
    _resolve_import_target,
    analyze_python,
    infer_module_layout,
)

#: Patterns whose change invalidates any narrower verification scope.
FULL_GATE_PATTERNS: tuple[str, ...] = (
    ".github/**",
    "Makefile",
    ".orch/**",
    "architecture/**",
    "pyproject.toml",
    "setup.cfg",
    "setup.py",
    "tox.ini",
)


@dataclass(frozen=True)
class GraphBuildStats:
    """Deterministic performance evidence for one graph build."""

    files_scanned: int
    python_files_parsed: int
    build_duration_ms: int
    scope_resolution_duration_ms: int = 0


def _resolve_explicit_modules(
    metadata: ProjectMetadata,
    analysis: PythonAnalysis,
) -> tuple[ProjectModule, ...]:
    """Resolve explicit module contracts against the actual file list.

    Fail-closed checks: a root pattern matching no file is stale, overlapping
    ownership is contradictory, and actual imports that violate a declared
    ``allowed_dependencies`` policy are forbidden.
    """
    modules: list[ExplicitModule] = list(metadata.modules)

    owner_by_file: dict[str, list[str]] = {}
    for module in modules:
        matched = tuple(
            file for file in analysis.production_files if match_any(module.roots, file)
        )
        if not matched:
            raise ProjectMetadataError(
                f"{module.source_path}: stale roots: no production file matches any of "
                f"{', '.join(module.roots)}"
            )
        for file in matched:
            owner_by_file.setdefault(file, []).append(module.id)

    overlaps = sorted(
        (file, tuple(sorted(owners)))
        for file, owners in owner_by_file.items()
        if len(owners) > 1
    )
    if overlaps:
        file, owners = overlaps[0]
        raise ProjectMetadataError(
            f"{PROJECT_FILE}: overlapping module ownership: {file} claimed by "
            f"{', '.join(owners)}"
        )

    # A declared interface or test contract is architectural evidence, not a
    # best-effort hint: a stale declaration that matches nothing on disk is a
    # contract error and fails closed rather than being silently dropped.
    known_files = set(analysis.files)
    for module in modules:
        for interface in module.interfaces:
            if interface not in known_files:
                raise ProjectMetadataError(
                    f"{module.source_path}: stale interface: declared interface "
                    f"{interface} does not exist"
                )
        for pattern in module.tests:
            if not any(match_any((pattern,), test) for test in analysis.test_files):
                raise ProjectMetadataError(
                    f"{module.source_path}: stale test declaration: pattern "
                    f"{pattern!r} matches no test file"
                )

    # Dependency evidence between explicitly declared modules.
    owner_of = {file: owners[0] for file, owners in owner_by_file.items()}
    actual_edges: dict[tuple[str, str], str] = {}
    for file in sorted(owner_of):
        for dotted in analysis.imports.get(file, ()):
            target = _resolve_import_target(dotted, analysis.dotted_index)
            if target is None:
                continue
            dst = owner_of.get(target)
            if dst is not None and dst != owner_of[file]:
                actual_edges.setdefault((owner_of[file], dst), file)

    resolved: list[ProjectModule] = []
    for module in modules:
        if module.allowed_dependencies:
            forbidden = sorted(
                dst for (src, dst) in actual_edges if src == module.id
            )
            violations = sorted(set(forbidden) - set(module.allowed_dependencies))
            if violations:
                edge_file = next(
                    file for (src, dst), file in actual_edges.items() if src == module.id
                )
                raise ProjectMetadataError(
                    f"{module.source_path}: forbidden dependency {module.id} -> "
                    f"{', '.join(violations)} (e.g. {edge_file}); declared allowed: "
                    f"{', '.join(module.allowed_dependencies)}"
                )
        owned = tuple(
            sorted(file for file, owner in owner_of.items() if owner == module.id)
        )
        interfaces = tuple(
            sorted(path for path in module.interfaces if path in set(analysis.files))
        )
        resolved.append(
            ProjectModule(
                id=module.id,
                roots=module.roots,
                owned_files=owned,
                interface_files=interfaces,
                test_files=tuple(
                    sorted(
                        test
                        for test in analysis.test_files
                        if match_any(module.tests, test)
                    )
                ),
                direct_dependencies=tuple(
                    sorted({dst for (src, dst) in actual_edges if src == module.id})
                ),
                allowed_dependencies=module.allowed_dependencies,
                entry_points=(),
                origin="explicit",
            )
        )
    return tuple(resolved)


def _load_f01_metadata(root: Path) -> ProjectMetadata | None:
    """Adapt an existing F01 ``architecture/modules`` layout, if present.

    Orchestrator-v2 itself already has one authoritative F01 architecture; this
    adapter consumes it rather than creating a competing ``.orch`` duplicate.
    Dependency policy stays unrestricted here because F01's own architecture
    checker owns edge policy for that layout.
    """
    modules_dir = root / "architecture" / "modules"
    if not modules_dir.is_dir():
        return None
    from orchestrator_mvp.devscope.manifest import ManifestError, load_manifests

    try:
        manifests = load_manifests(root)
    except ManifestError as exc:
        # architecture/modules/ is already authoritative for this repository;
        # a broken manifest is a contract error, never a silent invitation for
        # Python inference to replace it with a second, competing truth.
        raise ProjectMetadataError(
            f"architecture/modules/: authoritative F01 architecture failed to load: {exc}"
        ) from exc
    modules = tuple(
        ExplicitModule(
            id=manifest.name,
            description=manifest.description,
            roots=manifest.owned_paths,
            interfaces=manifest.interface_sources,
            tests=manifest.test_paths,
            allowed_dependencies=(),
            source_path=manifest.source,
        )
        for manifest in manifests
    )
    return ProjectMetadata(project_id=root.name, analyzer="python", modules=modules)


def _build_fallback_graph(
    root: Path, analysis: PythonAnalysis | None, base_revision: str = ""
) -> ProjectGraph:
    """A conservative graph that claims nothing about dependency closure."""
    return ProjectGraph(
        project_id=root.name,
        base_revision=base_revision,
        analyzer="none",
        graph_source=GraphSource.FALLBACK,
        confidence=Confidence.UNKNOWN,
        modules=(),
        files=(),
        unowned_files=(),
        test_files=(),
        entry_points=(),
        reasons=(
            "no supported analyzer evidence (no Python packages, no explicit "
            "metadata); dependency closure is unknown",
        ),
        files_scanned=analysis.files_scanned if analysis else 0,
        python_files_parsed=analysis.python_files_parsed if analysis else 0,
    )


def _sole_owner(modules: tuple[ProjectModule, ...], file: str) -> str | None:
    owners = tuple(sorted(m.id for m in modules if file in m.owned_files))
    return owners[0] if len(owners) == 1 else None


def build_project_graph(
    root: Path, *, base_revision: str = "", analysis: PythonAnalysis | None = None
) -> ProjectGraph:
    """Build the deterministic project graph for ``root`` (read-only).

    Precedence: valid explicit ``.orch`` metadata, then an F01
    ``architecture/modules`` layout, then deterministic Python inference, then
    a conservative fallback.  Raises :class:`ProjectMetadataError` for
    malformed or contradictory explicit metadata (fail closed).
    """
    metadata = load_project_metadata(root)
    source = GraphSource.EXPLICIT if metadata is not None else None
    if metadata is None:
        metadata = _load_f01_metadata(root)
        source = GraphSource.F01 if metadata is not None else None

    if analysis is None:
        analysis = analyze_python(root)

    if metadata is not None and analysis is not None:
        assert source is not None
        modules = _resolve_explicit_modules(metadata, analysis)
        unowned = tuple(
            file
            for file in analysis.production_files
            if _sole_owner(modules, file) is None
        )
        edges = tuple(
            sorted({(m.id, dep) for m in modules for dep in m.direct_dependencies})
        )
        confidence = Confidence.HIGH if not unowned else Confidence.MEDIUM
        reasons = [f"module architecture loaded from explicit metadata ({source.value})"]
        if unowned:
            reasons.append(
                f"{len(unowned)} production file(s) outside every declared module; "
                "affected verification widens for them"
            )
        return ProjectGraph(
            project_id=metadata.project_id,
            base_revision=base_revision,
            analyzer=metadata.analyzer if source == GraphSource.EXPLICIT else "python",
            graph_source=source,
            confidence=confidence,
            modules=modules,
            files=analysis.production_files,
            unowned_files=unowned,
            edges=edges,
            edge_evidence=(),
            test_files=analysis.test_files,
            entry_points=analysis.entry_points,
            reasons=tuple(reasons),
            files_scanned=analysis.files_scanned,
            python_files_parsed=analysis.python_files_parsed,
        )

    if analysis is not None and analysis.production_files:
        return _build_inferred_graph(root, analysis, base_revision)

    return _build_fallback_graph(root, analysis, base_revision)


def _build_inferred_graph(
    root: Path, analysis: PythonAnalysis, base_revision: str
) -> ProjectGraph:
    """Infer conservative modules from Python evidence (read-only)."""
    layout = infer_module_layout(analysis)
    modules = tuple(
        ProjectModule(
            id=module_id,
            roots=((f"{directory}/**",) if directory else ("**",)),
            owned_files=files,
            interface_files=(),
            test_files=tuple(
                sorted(
                    test
                    for test, owner in layout.test_ownership.items()
                    if owner == module_id
                )
            ),
            direct_dependencies=tuple(
                sorted(
                    {dst for src, dst, _ in layout.dependency_evidence if src == module_id}
                )
            ),
            allowed_dependencies=(),
            entry_points=(),
            origin="inferred",
        )
        for module_id, directory, files in layout.modules
    )
    edges = tuple(sorted({(src, dst) for src, dst, _ in layout.dependency_evidence}))
    reasons = [
        "no explicit .orch metadata; modules inferred deterministically from "
        "Python package boundaries and AST import evidence",
    ]
    if layout.collapsed:
        reasons.append(
            "module count exceeded the inference cap; layout collapsed to coarser "
            "package boundaries"
        )
    if layout.unowned_files:
        reasons.append(
            f"{len(layout.unowned_files)} production file(s) outside every inferred "
            "module boundary"
        )
    if analysis.parse_failures:
        reasons.append(
            f"{len(analysis.parse_failures)} Python file(s) could not be parsed and "
            "contribute no import evidence"
        )
    confidence = (
        Confidence.HIGH if len(modules) >= 2 and not layout.collapsed else Confidence.MEDIUM
    )
    return ProjectGraph(
        project_id=root.name,
        base_revision=base_revision,
        analyzer="python",
        graph_source=GraphSource.INFERRED,
        confidence=confidence,
        modules=modules,
        files=analysis.production_files,
        unowned_files=layout.unowned_files,
        edges=edges,
        edge_evidence=tuple(layout.dependency_evidence),
        test_files=analysis.test_files,
        entry_points=analysis.entry_points,
        reasons=tuple(reasons),
        files_scanned=analysis.files_scanned,
        python_files_parsed=analysis.python_files_parsed,
    )
