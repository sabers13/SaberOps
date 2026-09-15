"""Shadow-mode affected verification planning.

Given a set of changed paths, this maps change -> owning module -> consumers ->
relevant tests -> a recommended verification level.  It is *measurement*: F01
prints what a narrower verification scope would have been, and nothing more.

The authoritative final gate is unchanged and unconditional.  Every plan says
so, and every uncertain state -- an unowned production path, ambiguous
ownership, a change to architecture metadata or build/gate configuration, a
change to a high-fan-out hub -- escalates to ``G4`` rather than guessing.  The
failure mode this planner refuses to have is a confidently narrow answer.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestrator_mvp.devscope.architecture import ArchitectureReport, check_architecture
from orchestrator_mvp.devscope.owners import owner_of_any_path
from orchestrator_mvp.devscope.paths import (
    MODULES_DIR,
    PACKAGE_ROOT,
    ROOT_AGENTS_DOC,
    iter_files,
    match_any,
    match_path,
)
from orchestrator_mvp.devscope.repomap import HUB_FAN_IN_THRESHOLD, hub_files

#: Verification levels, weakest first.  The index is the escalation order.
LEVELS = ("G0", "G1", "G2", "G3", "G4")

LEVEL_MEANING = {
    "G0": "changed-file/edit-loop checks only",
    "G1": "owner-module checks and tests",
    "G2": "owner module plus its direct dependents",
    "G3": "transitive affected modules plus relevant integration coverage",
    "G4": "authoritative full repository certification",
}

#: Paths whose change invalidates every narrower scope this planner can compute.
FULL_GATE_PATTERNS: tuple[str, ...] = (
    ".github/**",
    "Makefile",
    "architecture/**",
    "mypy.ini",
    "pyproject.toml",
    "ruff.toml",
    "setup.cfg",
    "setup.py",
    "tests/__init__.py",
    "tests/conftest.py",
    "tox.ini",
    ROOT_AGENTS_DOC,
)


def escalate(current: str, candidate: str) -> str:
    """Return the stronger of two verification levels."""
    return candidate if LEVELS.index(candidate) > LEVELS.index(current) else current


@dataclass(frozen=True)
class ChangedPath:
    """One changed path, classified against the architecture."""

    path: str
    kind: str
    owners: tuple[str, ...]
    note: str


@dataclass(frozen=True)
class AffectedPlan:
    """The recommended -- never authoritative -- verification scope."""

    changed_paths: tuple[str, ...]
    classified: tuple[ChangedPath, ...]
    changed_modules: tuple[str, ...]
    direct_consumer_modules: tuple[str, ...]
    transitive_modules: tuple[str, ...]
    affected_modules: tuple[str, ...]
    level: str
    reasons: tuple[str, ...]
    tests: tuple[str, ...]

    #: F01 never replaces the final gate; this is always true.
    shadow_mode: bool = True
    final_gate_required: bool = True


def changed_paths_from_git(root: Path, base: str) -> tuple[str, ...]:
    """Return repository-relative paths changed since ``base``.

    Read-only: a single ``git diff --name-only``.  No index or worktree state
    is modified.
    """
    result = subprocess.run(
        ["git", "diff", "--name-only", base, "--"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return tuple(sorted({line.strip() for line in result.stdout.splitlines() if line.strip()}))


def _classify(root: Path, report: ArchitectureReport, path: str) -> ChangedPath:
    if match_any(FULL_GATE_PATTERNS, path):
        kind = "architecture" if path.startswith((MODULES_DIR, "architecture/")) else "build"
        if path == ROOT_AGENTS_DOC:
            kind = "architecture"
        return ChangedPath(path, kind, (), f"{kind} configuration governs every narrower scope")

    for manifest in report.manifests:
        if match_any(manifest.full_gate_triggers, path):
            return ChangedPath(
                path,
                "declared_trigger",
                (manifest.name,),
                f"{manifest.source} declares this path a full-gate trigger",
            )

    if path in report.ownership.owner_by_path:
        return ChangedPath(path, "production", (report.ownership.owner_by_path[path],), "")

    for overlap_path, owners in report.ownership.overlapping:
        if overlap_path == path:
            return ChangedPath(
                path,
                "ambiguous",
                owners,
                f"claimed by {len(owners)} modules: {', '.join(owners)}",
            )

    owners = owner_of_any_path(path, report.manifests)
    if path.startswith("tests/"):
        if owners:
            return ChangedPath(path, "test", owners, "")
        return ChangedPath(path, "unowned_test", (), "no module declares this test path")

    if path.startswith(f"{PACKAGE_ROOT}/") or path.startswith("src/"):
        if owners:
            return ChangedPath(path, "asset", owners, "")
        return ChangedPath(path, "unknown", (), "production path has no declared owner")

    if (root / path).is_file() or path.endswith((".md", ".txt", ".png", ".json")):
        if owners:
            return ChangedPath(path, "asset", owners, "")
        return ChangedPath(path, "doc", (), "non-production path outside every module")

    return ChangedPath(path, "unknown", (), "path could not be classified against the architecture")


def _resolve_tests(
    root: Path, report: ArchitectureReport, modules: tuple[str, ...]
) -> tuple[str, ...]:
    found: set[str] = set()
    existing: tuple[str, ...] | None = None
    for manifest in report.manifests:
        if manifest.name not in modules:
            continue
        for pattern in manifest.test_paths:
            if (root / pattern).is_file():
                found.add(pattern)
                continue
            # A glob rather than a literal path: resolve it against real files
            # so the count reported is always of tests that exist.
            if existing is None:
                existing = iter_files(root, "tests", suffix=".py")
            found.update(candidate for candidate in existing if match_path(pattern, candidate))
    return tuple(sorted(found))


def plan_affected(
    root: Path,
    changed_paths: tuple[str, ...] = (),
    base: str | None = None,
    report: ArchitectureReport | None = None,
) -> AffectedPlan:
    """Map changed paths to the smallest verification scope that is still safe."""
    report = report if report is not None else check_architecture(root)
    paths = tuple(sorted({*changed_paths, *(changed_paths_from_git(root, base) if base else ())}))

    level = "G0"
    reasons: list[str] = []

    if report.findings:
        level = escalate(level, "G4")
        reasons.append(
            f"architecture validation is failing with {len(report.findings)} finding(s); "
            f"dependency analysis state is unknown"
        )

    hubs = dict(hub_files(report.graph))
    classified = tuple(_classify(root, report, path) for path in paths)

    changed_modules: set[str] = set()
    changed_production: list[str] = []
    for entry in classified:
        if entry.kind in {"architecture", "build"}:
            level = escalate(level, "G4")
            reasons.append(f"{entry.kind} change {entry.path}: {entry.note}")
        elif entry.kind == "declared_trigger":
            level = escalate(level, "G4")
            reasons.append(f"full-gate trigger {entry.path}: {entry.note}")
            changed_modules.update(entry.owners)
        elif entry.kind == "ambiguous":
            level = escalate(level, "G4")
            reasons.append(f"ownership ambiguity for {entry.path}: {entry.note}")
            changed_modules.update(entry.owners)
        elif entry.kind == "unknown":
            level = escalate(level, "G4")
            reasons.append(f"unowned production path {entry.path}: {entry.note}")
        elif entry.kind == "unowned_test":
            level = escalate(level, "G3")
            reasons.append(f"unowned test path {entry.path}: {entry.note}")
        elif entry.kind == "production":
            level = escalate(level, "G1")
            changed_modules.update(entry.owners)
            changed_production.append(entry.path)
            fan_in = hubs.get(entry.path)
            if fan_in is not None:
                level = escalate(level, "G4")
                reasons.append(
                    f"high-fan-out hub {entry.path} has {fan_in} direct consumers "
                    f"(threshold {HUB_FAN_IN_THRESHOLD}); its blast radius is repository-wide"
                )
        elif entry.kind in {"test", "asset"}:
            level = escalate(level, "G1")
            changed_modules.update(entry.owners)
        else:
            reasons.append(f"documentation-only change {entry.path}: no verification impact")

    depths = report.graph.reverse_closure(tuple(sorted(changed_production)))
    direct_consumers: set[str] = set()
    transitive: set[str] = set()
    for path, depth in sorted(depths.items()):
        owner = report.ownership.owner_of(path)
        if owner is None or depth == 0:
            continue
        if depth == 1:
            direct_consumers.add(owner)
        else:
            transitive.add(owner)

    new_direct = direct_consumers - changed_modules
    if new_direct:
        level = escalate(level, "G2")
        reasons.append(
            f"direct consumers outside the changed module(s): {', '.join(sorted(new_direct))}"
        )
    new_transitive = transitive - changed_modules - direct_consumers
    if new_transitive:
        level = escalate(level, "G3")
        reasons.append(
            f"transitive consumers reached beyond depth 1: {', '.join(sorted(new_transitive))}"
        )

    if level == "G4":
        affected = tuple(report.module_names)
    elif level == "G3":
        affected = tuple(sorted(changed_modules | direct_consumers | transitive))
    elif level == "G2":
        affected = tuple(sorted(changed_modules | direct_consumers))
    else:
        affected = tuple(sorted(changed_modules))

    if not paths:
        reasons.append("no changed paths supplied; nothing to verify beyond the edit loop")

    reasons.append(
        "shadow mode: this recommendation never replaces the authoritative "
        "`make gate` run owned by the orchestrator"
    )

    return AffectedPlan(
        changed_paths=paths,
        classified=classified,
        changed_modules=tuple(sorted(changed_modules)),
        direct_consumer_modules=tuple(sorted(direct_consumers)),
        transitive_modules=tuple(sorted(transitive)),
        affected_modules=affected,
        level=level,
        reasons=tuple(reasons),
        tests=_resolve_tests(root, report, affected),
    )


def plan_to_dict(plan: AffectedPlan) -> dict[str, Any]:
    """Render an affected plan as deterministic JSON-ready data."""
    return {
        "status": "PASS",
        "shadow_mode": plan.shadow_mode,
        "final_gate_required": plan.final_gate_required,
        "changed_paths": list(plan.changed_paths),
        "classified": [
            {
                "path": entry.path,
                "kind": entry.kind,
                "owners": list(entry.owners),
                "note": entry.note,
            }
            for entry in plan.classified
        ],
        "changed_modules": list(plan.changed_modules),
        "direct_consumer_modules": list(plan.direct_consumer_modules),
        "transitive_modules": list(plan.transitive_modules),
        "affected_modules": list(plan.affected_modules),
        "verification_level": plan.level,
        "verification_level_meaning": LEVEL_MEANING[plan.level],
        "tests": list(plan.tests),
        "test_count": len(plan.tests),
        "reasons": list(plan.reasons),
    }


def render_plan(plan: AffectedPlan) -> str:
    """Compact human/LLM-readable rendering of an affected plan."""
    lines = [
        "status: PASS",
        f"changed_paths: {len(plan.changed_paths)}",
        f"changed_modules: [{', '.join(plan.changed_modules)}]",
        f"affected_modules: [{', '.join(plan.affected_modules)}]",
        f"verification_level: {plan.level}  ({LEVEL_MEANING[plan.level]})",
        f"tests: {len(plan.tests)}",
        "reasons:",
    ]
    lines.extend(f"  - {reason}" for reason in plan.reasons)
    return "\n".join(lines) + "\n"
