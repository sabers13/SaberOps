"""Bounded context packing: the smallest safe reading list for one task.

The planner answers "what should an implementation agent open first", not
"what could possibly be relevant".  It returns *paths and reasons*, never
concatenated source, so the caller keeps control of how much it actually
reads.  Budgets are deterministic, and pressure against them is always
reported: a plan that silently dropped required dependency closure would be
worse than one that admits it did not fit.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestrator_mvp.devscope.architecture import ArchitectureReport, check_architecture
from orchestrator_mvp.devscope.paths import MODULES_DIR, ROOT_AGENTS_DOC
from orchestrator_mvp.devscope.scope import ScopeResult, resolve_scope

#: Default starting policy for a bounded context pack.
DEFAULT_SOURCE_BUDGET = 12
DEFAULT_TEST_BUDGET = 6
DEFAULT_DEPENDENCY_DEPTH = 1


@dataclass(frozen=True)
class ContextBudget:
    """Deterministic limits on how much a single context pack may name."""

    max_source_files: int = DEFAULT_SOURCE_BUDGET
    max_test_files: int = DEFAULT_TEST_BUDGET
    dependency_depth: int = DEFAULT_DEPENDENCY_DEPTH

    def as_dict(self) -> dict[str, int]:
        """JSON-ready view of the budget."""
        return {
            "max_source_files": self.max_source_files,
            "max_test_files": self.max_test_files,
            "dependency_depth": self.dependency_depth,
        }


@dataclass(frozen=True)
class ContextPlan:
    """What an implementation worker should inspect before editing."""

    scope: ScopeResult
    modules: tuple[str, ...]
    module_contracts: tuple[str, ...]
    context_docs: tuple[str, ...]
    root_agents_doc: str
    editable_sources: tuple[str, ...]
    dependency_interfaces: tuple[tuple[str, tuple[str, ...]], ...]
    dependency_neighborhood: tuple[tuple[str, int, str], ...]
    tests: tuple[str, ...]
    budget: ContextBudget
    truncated: bool
    closure_complete: bool
    reasons: tuple[str, ...]

    @property
    def source_count(self) -> int:
        """Total source files named by the plan, editable plus interfaces."""
        return len(self.editable_sources) + sum(
            len(paths) for _, paths in self.dependency_interfaces
        )


def _neighborhood(
    report: ArchitectureReport, modules: tuple[str, ...], depth: int
) -> tuple[tuple[str, int, str], ...]:
    """Breadth-first dependency neighbourhood of ``modules`` up to ``depth``."""
    seen: dict[str, tuple[int, str]] = dict.fromkeys(modules, (0, "primary"))
    frontier = list(modules)
    for level in range(1, depth + 1):
        nxt: list[str] = []
        for name in frontier:
            for edge in report.edges:
                if edge.source != name or edge.target in seen:
                    continue
                seen[edge.target] = (level, edge.state)
                nxt.append(edge.target)
        frontier = sorted(nxt)
        if not frontier:
            break
    return tuple(
        sorted(
            ((name, level, state) for name, (level, state) in seen.items() if level > 0),
            key=lambda item: (item[1], item[0]),
        )
    )


def _dependency_boundary(
    report: ArchitectureReport,
    selected_sources: tuple[str, ...],
    dependency_modules: tuple[str, ...],
) -> tuple[dict[str, tuple[str, ...]], tuple[tuple[str, str, str], ...]]:
    """Return dependency files actually imported by ``selected_sources``.

    Module-edge evidence is the architectural view of the same AST-derived
    file edges used by the graph.  Restricting that evidence to the selected
    source set keeps a large declared public surface from becoming a hidden
    context-budget requirement.
    """
    selected = set(selected_sources)
    allowed_targets = set(dependency_modules)
    paths_by_module: dict[str, set[str]] = {}
    evidence: list[tuple[str, str, str]] = []
    for edge in report.edges:
        if edge.target not in allowed_targets:
            continue
        for source, target in edge.evidence:
            if source not in selected:
                continue
            paths_by_module.setdefault(edge.target, set()).add(target)
            evidence.append((edge.target, source, target))
    return (
        {name: tuple(sorted(paths)) for name, paths in sorted(paths_by_module.items())},
        tuple(sorted(evidence)),
    )


def plan_context(
    root: Path,
    query: str = "",
    seeds: tuple[str, ...] = (),
    symbols: tuple[str, ...] = (),
    budget: ContextBudget | None = None,
    report: ArchitectureReport | None = None,
    scope: ScopeResult | None = None,
) -> ContextPlan:
    """Build a bounded context pack for a task, reporting any budget pressure."""
    report = report if report is not None else check_architecture(root)
    budget = budget if budget is not None else ContextBudget()
    scope = (
        scope
        if scope is not None
        else resolve_scope(root, query=query, seeds=seeds, symbols=symbols, report=report)
    )

    modules = scope.primary_modules
    by_name = {manifest.name: manifest for manifest in report.manifests}
    reasons: list[str] = list(scope.reasons)

    neighborhood = _neighborhood(report, modules, budget.dependency_depth)
    for name, level, state in neighborhood:
        if state == "transitional":
            reasons.append(
                f"dependency {name} (depth {level}) is reached through a transitional edge; "
                f"treat it as existing debt, not as target architecture"
            )

    editable_candidates = tuple(
        path for path in scope.source_paths if report.ownership.owner_of(path) in modules
    )
    truncated = False
    closure_complete = True

    # Only direct dependencies of the selected primary module participate in
    # its boundary closure.  Deeper modules remain visible in the
    # neighbourhood, but are not injected into a depth-one context pack.
    direct_dependency_modules = tuple(
        name
        for name, _, _ in neighborhood
        if any(edge.source in modules and edge.target == name for edge in report.edges)
    )

    # Select target files and reserve the boundary they actually require as a
    # single bounded unit.  This gives required dependency files priority over
    # optional additional target files without examining imports from files
    # that were not selected.
    selected_editable: list[str] = []
    selected_boundary: dict[str, tuple[str, ...]] = {}
    forced_boundary_overflow = False
    for candidate in editable_candidates:
        candidate_boundary, _ = _dependency_boundary(
            report, (candidate,), direct_dependency_modules
        )
        proposed_boundary = {
            name: tuple(
                sorted(
                    {
                        *selected_boundary.get(name, ()),
                        *candidate_boundary.get(name, ()),
                    }
                )
            )
            for name in set(selected_boundary) | set(candidate_boundary)
        }
        proposed_count = (
            len(selected_editable) + 1 + sum(len(paths) for paths in proposed_boundary.values())
        )
        if proposed_count <= budget.max_source_files:
            selected_editable.append(candidate)
            selected_boundary = proposed_boundary
            continue

        # If the first relevant target cannot fit together with its required
        # boundary, retain the target and make the omitted boundary explicit.
        # This is the useful, honest representation of an impossible closure.
        if not selected_editable and candidate_boundary:
            selected_editable.append(candidate)
            selected_boundary = candidate_boundary
            forced_boundary_overflow = True
        break

    editable = tuple(selected_editable)
    if len(editable) < len(editable_candidates):
        truncated = True
        reasons.append(
            f"source budget exhausted: {len(editable_candidates)} candidate source files "
            f"could not all fit with their required dependency boundaries in the budget of "
            f"{budget.max_source_files}; kept the most query-relevant safe subset"
        )

    remaining = budget.max_source_files - len(editable)
    selected_interfaces: list[tuple[str, tuple[str, ...]]] = []
    missing_boundary: list[str] = []
    for name in direct_dependency_modules:
        paths = selected_boundary.get(name, ())
        if not paths:
            continue
        kept = paths[:remaining]
        remaining -= len(kept)
        if len(kept) < len(paths):
            closure_complete = False
            missing_boundary.extend(paths[len(kept) :])
        selected_interfaces.append((name, kept))
    selected_interfaces = [entry for entry in selected_interfaces if entry[1]]

    if not closure_complete:
        truncated = True
        detail = "; ".join(missing_boundary) if missing_boundary else "unknown required files"
        reasons.append(
            f"dependency closure incomplete: {len(missing_boundary)} required "
            f"direct-dependency boundary file(s) did not fit the source budget of "
            f"{budget.max_source_files} ({detail}); raise --max-sources or narrow the "
            f"scope before relying on this pack"
        )
    if forced_boundary_overflow:
        reasons.append(
            "dependency closure incomplete: the first relevant target's required boundary "
            f"exceeds the source budget of {budget.max_source_files}"
        )

    _, boundary_evidence = _dependency_boundary(report, editable, direct_dependency_modules)
    for dependency, source, target in boundary_evidence:
        manifest = by_name.get(dependency)
        if manifest is not None and target not in manifest.interface_sources:
            reasons.append(
                f"undeclared/internal dependency surface: {source} reaches {target} "
                f"through {dependency}; included from AST evidence"
            )

    test_candidates = scope.test_paths
    tests = test_candidates
    if len(tests) > budget.max_test_files:
        tests = tests[: budget.max_test_files]
        truncated = True
        reasons.append(
            f"test budget exhausted: {len(test_candidates)} relevant test files exceed the "
            f"budget of {budget.max_test_files}; the remainder belongs to the owning "
            f"module's fuller check"
        )

    contracts = tuple(f"{MODULES_DIR}/{name}.toml" for name in modules if name in by_name)
    docs = tuple(
        sorted({doc for name in modules for doc in by_name[name].context_docs if name in by_name})
    )

    return ContextPlan(
        scope=scope,
        modules=modules,
        module_contracts=contracts,
        context_docs=docs,
        root_agents_doc=ROOT_AGENTS_DOC,
        editable_sources=tuple(editable),
        dependency_interfaces=tuple(selected_interfaces),
        dependency_neighborhood=neighborhood,
        tests=tuple(tests),
        budget=budget,
        truncated=truncated,
        closure_complete=closure_complete,
        reasons=tuple(reasons),
    )


def plan_to_dict(plan: ContextPlan) -> dict[str, Any]:
    """Render a context plan as deterministic JSON-ready data."""
    return {
        "query": plan.scope.query,
        "seeds": list(plan.scope.seeds),
        "symbols": list(plan.scope.symbols),
        "modules": list(plan.modules),
        "confidence": plan.scope.confidence,
        "ambiguous": plan.scope.ambiguous,
        "root_agents_doc": plan.root_agents_doc,
        "module_contracts": list(plan.module_contracts),
        "context_docs": list(plan.context_docs),
        "editable_sources": list(plan.editable_sources),
        "dependency_interfaces": [
            {"module": name, "sources": list(paths)} for name, paths in plan.dependency_interfaces
        ],
        "dependency_neighborhood": [
            {"module": name, "depth": depth, "edge_state": state}
            for name, depth, state in plan.dependency_neighborhood
        ],
        "tests": list(plan.tests),
        "budget": plan.budget.as_dict(),
        "source_count": plan.source_count,
        "test_count": len(plan.tests),
        "truncated": plan.truncated,
        "closure_complete": plan.closure_complete,
        "reasons": list(plan.reasons),
        "ranked_modules": [
            {"module": score.module, "score": score.score, "matched": list(score.matched_tokens)}
            for score in plan.scope.ranked
            if score.score > 0
        ],
    }


def render_plan(plan: ContextPlan) -> str:
    """Compact human/LLM-readable rendering of a context plan."""
    lines = [
        f"modules: {', '.join(plan.modules)}",
        f"confidence: {plan.scope.confidence}  ambiguous: {plan.scope.ambiguous}",
        f"sources: {plan.source_count}/{plan.budget.max_source_files}  "
        f"tests: {len(plan.tests)}/{plan.budget.max_test_files}  "
        f"depth: {plan.budget.dependency_depth}",
        f"truncated: {plan.truncated}  closure_complete: {plan.closure_complete}",
        f"read_first: {plan.root_agents_doc}, {', '.join(plan.module_contracts)}, "
        f"{', '.join(plan.context_docs)}",
    ]
    lines.append("editable_sources:")
    lines.extend(f"  {path}" for path in plan.editable_sources)
    if plan.dependency_interfaces:
        lines.append("dependency_interfaces:")
        for name, paths in plan.dependency_interfaces:
            lines.append(f"  {name}: {', '.join(paths)}")
    lines.append("tests:")
    lines.extend(f"  {path}" for path in plan.tests)
    lines.append("reasons:")
    lines.extend(f"  {reason}" for reason in plan.reasons)
    return "\n".join(lines) + "\n"
