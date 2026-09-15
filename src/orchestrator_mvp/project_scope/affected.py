"""Affected verification planning over a project graph (shadow mode).

Given a base project graph and an actual candidate diff, this maps
change -> owning module -> reverse affected modules -> relevant tests -> a
recommended verification level.  It always fails broad: unknown ownership,
ambiguous ownership, architecture metadata changes, build/config changes, new
module boundaries, public-interface changes with uncertain reverse closure,
unsupported language impact and invalid graphs all escalate to ``G4``.

The recommendation is advisory.  It never substitutes for the run's
configured authoritative gate.
"""

from __future__ import annotations

from dataclasses import dataclass

from orchestrator_mvp.devscope.paths import match_any
from orchestrator_mvp.project_scope.contracts import (
    Confidence,
    GraphSource,
    ProjectGraph,
)
from orchestrator_mvp.project_scope.graph import FULL_GATE_PATTERNS

LEVELS = ("G0", "G1", "G2", "G3", "G4")


def escalate(current: str, candidate: str) -> str:
    """Return the stronger of two verification levels."""
    return candidate if LEVELS.index(candidate) > LEVELS.index(current) else current


@dataclass(frozen=True)
class AffectedPlan:
    """The recommended — never authoritative — verification scope."""

    changed_paths: tuple[str, ...]
    changed_modules: tuple[str, ...]
    unknown_paths: tuple[str, ...]
    direct_dependent_modules: tuple[str, ...]
    transitive_dependent_modules: tuple[str, ...]
    affected_modules: tuple[str, ...]
    tests: tuple[str, ...]
    level: str
    reasons: tuple[str, ...]
    closure_confident: bool
    shadow_mode: bool = True
    final_gate_required: bool = True

    def to_dict(self) -> dict[str, object]:
        """Deterministic JSON-ready rendering."""
        return {
            "changed_paths": list(self.changed_paths),
            "changed_modules": list(self.changed_modules),
            "unknown_paths": list(self.unknown_paths),
            "direct_dependent_modules": list(self.direct_dependent_modules),
            "transitive_dependent_modules": list(self.transitive_dependent_modules),
            "affected_modules": list(self.affected_modules),
            "tests": list(self.tests),
            "verification_level": self.level,
            "reasons": list(self.reasons),
            "closure_confident": self.closure_confident,
            "shadow_mode": self.shadow_mode,
            "final_gate_required": self.final_gate_required,
        }


def plan_affected(graph: ProjectGraph, changed_paths: tuple[str, ...]) -> AffectedPlan:
    """Plan affected verification for ``changed_paths`` against ``graph``."""
    level = "G0"
    reasons: list[str] = []
    changed_modules: set[str] = set()
    unknown: list[str] = []

    if graph.graph_source == GraphSource.FALLBACK or graph.confidence in (
        Confidence.UNKNOWN,
        Confidence.LOW,
    ):
        level = escalate(level, "G4")
        reasons.append(
            "project graph is a conservative fallback with unknown confidence; "
            "dependency closure cannot be claimed"
        )

    for path in changed_paths:
        owners = graph.owners_of(path)
        if len(owners) == 1:
            changed_modules.add(owners[0])
            continue
        if len(owners) > 1:
            unknown.append(path)
            level = escalate(level, "G4")
            reasons.append(f"ambiguous ownership for {path}: {', '.join(owners)}")
            continue
        unknown.append(path)
        level = escalate(level, "G4")
        reasons.append(
            f"unknown/unowned path {path}: not represented in the frozen project "
            "graph; ownership is not guessed from filename similarity"
        )

    if any(match_any((".orch/**", "architecture/**"), path) for path in changed_paths):
        level = escalate(level, "G4")
        reasons.append("project architecture metadata changed; every narrower scope is invalid")
    if any(match_any(FULL_GATE_PATTERNS, path) for path in changed_paths):
        level = escalate(level, "G4")
        reasons.append("build/gate configuration changed")

    depths = graph.transitive_dependents(tuple(sorted(changed_modules)))
    direct: set[str] = set()
    transitive: set[str] = set()
    for name, depth in sorted(depths.items()):
        if depth == 0:
            continue
        if depth == 1:
            direct.add(name)
        else:
            transitive.add(name)
    if direct:
        level = escalate(level, "G2")
        reasons.append(f"direct reverse-dependent modules: {', '.join(sorted(direct))}")
    if transitive:
        level = escalate(level, "G3")
        reasons.append(f"transitive reverse-dependent modules: {', '.join(sorted(transitive))}")

    if level == "G4":
        affected = graph.module_ids
    elif level == "G3":
        affected = tuple(sorted(changed_modules | direct | transitive))
    elif level == "G2":
        affected = tuple(sorted(changed_modules | direct))
    else:
        affected = tuple(sorted(changed_modules))

    tests: list[str] = []
    for module_id in affected:
        module = graph.module_by_id(module_id)
        if module is not None:
            tests.extend(module.test_files)
    tests = sorted(set(tests))

    if not changed_paths:
        reasons.append("no changed paths supplied; nothing to verify beyond the edit loop")
    reasons.append(
        "shadow mode: this recommendation never replaces the run's authoritative gate"
    )

    return AffectedPlan(
        changed_paths=tuple(sorted(changed_paths)),
        changed_modules=tuple(sorted(changed_modules)),
        unknown_paths=tuple(sorted(unknown)),
        direct_dependent_modules=tuple(sorted(direct)),
        transitive_dependent_modules=tuple(sorted(transitive)),
        affected_modules=affected,
        tests=tuple(tests),
        level=level,
        reasons=tuple(reasons),
        closure_confident=graph.confidence == Confidence.HIGH and graph.graph_source
        in (GraphSource.EXPLICIT, GraphSource.F01, GraphSource.INFERRED),
    )
