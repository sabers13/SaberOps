"""Deterministic, inspectable semantic work-package planning.

Transport constraints are deliberately absent here.  Dispatch eligibility is a
separate concern owned by :mod:`orchestrator_mvp.dispatch`; a large coherent
prompt must route to a compatible transport, not be split for transport.
"""
# ruff: noqa: E501

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class TaskSizingAssessment:
    """Transparent broadness score used before optional semantic planning."""

    score: int
    signals: tuple[str, ...]
    requirement_count: int
    should_slice: bool


@dataclass(frozen=True)
class ProposedPackage:
    """Untrusted package proposal; validator turns it into engine state."""

    title: str
    goal: str
    acceptance_criteria: tuple[str, ...]
    dependency_indexes: tuple[int, ...] = ()
    relevant_paths: tuple[str, ...] = ()
    long_running_hint: bool | None = None


class SemanticPlanner(Protocol):
    """Narrow optional planner contract; implementations may be local or remote."""

    provider: str
    model: str

    def plan(self, task: str, assessment: TaskSizingAssessment) -> list[ProposedPackage]:
        """Return a proposal which is always validated by the engine."""


_COMPLEX = (
    "migrate",
    "refactor",
    "architecture",
    "schema",
    "database",
    "supervisor",
    "durable",
    "recovery",
    "frontend",
    "api",
    "review",
    "integration",
    "security",
)
_DEPENDENCY = ("then", "after", "before", "depends", "followed by", "first")


def assess_task_sizing(task: str) -> TaskSizingAssessment:
    """Score task breadth without examining model adapters or prompt transports."""
    normalized = task.strip().lower()
    score = 0
    signals: list[str] = []
    length = len(normalized)
    if length >= 1000:
        score += 4
        signals.append("length:1000+")
    elif length >= 400:
        score += 2
        signals.append("length:400-999")
    elif length >= 180:
        score += 1
        signals.append("length:180-399")

    lines = [line.strip() for line in task.splitlines() if line.strip()]
    numbered = [line for line in lines if re.match(r"(?:\d+[.)]|[-*])\s+", line)]
    requirements = len(numbered) if len(numbered) >= 2 else len(lines)
    if requirements >= 7:
        score += 4
        signals.append("requirements:7+")
    elif requirements >= 4:
        score += 2
        signals.append("requirements:4-6")

    found = 0
    for keyword in _COMPLEX:
        if re.search(rf"\b{re.escape(keyword)}\b", normalized):
            found += 1
    if found:
        score += min(5, found)
        signals.append(f"complexity:{found}")
    if any(needle in normalized for needle in _DEPENDENCY):
        score += 1
        signals.append("explicit-dependencies")
    # At least two independent areas named by common module separators is a
    # useful deterministic breadth signal, not a repository inspection.
    areas = len(
        re.findall(r"\b(?:api|web|ui|cli|database|schema|tests?|docs?|supervisor)\b", normalized)
    )
    if areas >= 4:
        score += 2
        signals.append("subsystem-breadth")
    return TaskSizingAssessment(
        score=score,
        signals=tuple(signals),
        requirement_count=requirements,
        should_slice=score >= 8 and requirements >= 3,
    )


def deterministic_plan(task: str, assessment: TaskSizingAssessment) -> list[ProposedPackage]:
    """Return a single coherent unsliced package.

    C04: raw line-position slicing is no longer the default mechanism.  A
    line position is not a semantic subsystem boundary and historically
    produced packages LARGER than the parent task and large enough to
    implement later packages.  When no semantic planner is available, the
    deterministic fallback executes the task as one coherent unsliced
    package and persists / emits a ``slicing_not_applied`` reason.
    """
    return [
        ProposedPackage(
            "Implement task",
            task.strip(),
            ("Complete the requested task.",),
            long_running_hint=assessment.score >= 12,
        )
    ]


def validate_proposal(proposal: list[ProposedPackage]) -> list[ProposedPackage]:
    """Fail closed for empty, microtask-heavy, duplicate, or cyclic proposals."""
    if not proposal:
        raise ValueError("invalid work plan: no packages")
    if len(proposal) > 6:
        raise ValueError("invalid work plan: more than six packages is microtask-heavy")
    normalized: list[ProposedPackage] = []
    seen: set[str] = set()
    for index, package in enumerate(proposal):
        title = package.title.strip()
        goal = package.goal.strip()
        criteria = tuple(item.strip() for item in package.acceptance_criteria if item.strip())
        key = " ".join(goal.lower().split())
        if not title or not goal or not criteria:
            raise ValueError(f"invalid work plan: package {index + 1} is incomplete")
        if key in seen:
            raise ValueError(f"invalid work plan: duplicate package {index + 1}")
        seen.add(key)
        for dep in package.dependency_indexes:
            if dep < 0 or dep >= len(proposal) or dep == index:
                raise ValueError(f"invalid work plan: package {index + 1} has invalid dependency")
        normalized.append(
            ProposedPackage(
                title,
                goal,
                criteria,
                package.dependency_indexes,
                package.relevant_paths,
                package.long_running_hint,
            )
        )
    # Kahn's algorithm validates future DAG-shaped plans while the 3B scheduler
    # still executes in stable order.
    remaining = {index: set(item.dependency_indexes) for index, item in enumerate(normalized)}
    completed: set[int] = set()
    while remaining:
        ready = sorted(index for index, deps in remaining.items() if deps <= completed)
        if not ready:
            raise ValueError("invalid work plan: cyclic dependencies")
        for index in ready:
            completed.add(index)
            del remaining[index]
    return normalized


def resolve_work_packages(
    task: str, planner: SemanticPlanner | None = None
) -> tuple[TaskSizingAssessment, list[ProposedPackage]]:
    """Assess then plan, always validating an optional semantic planner result.

    C04: when no semantic planner is available, never raw-line split.  When
    a planner IS available, its proposal is untrusted; we apply a small
    safety/efficiency guard before accepting it.  Harmful proposals (empty,
    duplicate, cyclic, missing acceptance criteria, parent-copy, or whose
    projected dynamic context would exceed the original parent task) cause
    slicing to be rejected and one coherent unsliced package to be
    executed.
    """
    assessment = assess_task_sizing(task)
    if not assessment.should_slice:
        return assessment, deterministic_plan(task, assessment)
    if planner is None:
        # No semantic planner: do not invent arbitrary line-group packages.
        return assessment, deterministic_plan(task, assessment)
    try:
        proposal = planner.plan(task, assessment)
    except Exception:
        # Planner failure must not crash the run; fall back to unsliced.
        return assessment, deterministic_plan(task, assessment)
    proposal = validate_proposal(proposal)
    safe = _planner_proposal_is_safe(proposal, parent_task=task)
    if not safe:
        return assessment, deterministic_plan(task, assessment)
    return assessment, proposal


def _planner_proposal_is_safe(
    proposal: list[ProposedPackage], *, parent_task: str
) -> bool:
    """Deterministically reject harmful planner proposals.

    The simple safety invariant: SLICING MUST NEVER CREATE AN INDIVIDUAL
    PACKAGE CONTEXT LARGER THAN THE LARGE PARENT OBJECTIVE IT WAS INTENDED
    TO REDUCE.

    A package is rejected when ANY of these hold:

    * the package's goal essentially reproduces the parent objective;
    * the projected dynamic context (goal + acceptance criteria +
      dependencies + paths) exceeds the original parent-task size for a
      genuinely large task.

    The function is intentionally cheap and deterministic; complex ratio
    tuning is deferred to C09.
    """
    parent_size = len(parent_task)
    large_parent_threshold = 1000
    if parent_size < large_parent_threshold:
        # Small parent objectives are not at risk of oversized slicing.
        return True
    for package in proposal:
        if _is_parent_copy(package, parent_task):
            return False
        projected_size = (
            len(package.goal)
            + sum(len(item) for item in package.acceptance_criteria)
            + sum(len(item) for item in package.relevant_paths)
        )
        if projected_size >= parent_size:
            return False
    return True


def _is_parent_copy(package: ProposedPackage, parent_task: str) -> bool:
    """Return True when the package essentially reproduces the parent objective."""
    parent_norm = " ".join(parent_task.lower().split())
    package_norm = " ".join(package.goal.lower().split())
    if not parent_norm or not package_norm:
        return False
    if parent_norm == package_norm:
        return True
    # If the package goal is >= 80% of the parent length and shares a clear
    # substring of the parent, treat as parent copy.
    if len(package_norm) >= int(0.8 * len(parent_norm)) and package_norm in parent_norm:
        return True
    return False
