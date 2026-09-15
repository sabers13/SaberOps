"""Persistence-facing plan construction and deterministic state transitions."""
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import cast

from saberops.db import Database, current_iso_timestamp
from saberops.models import (
    DeterministicIntegration,
    IndependenceState,
    IndependentWorkBranch,
    IndependentWorkDecision,
    IntegrationDisposition,
    IntegrationParticipant,
    PlanStatus,
    PlanStep,
    PlanStepStatus,
    PostIntegrationValidation,
    PostIntegrationValidationState,
    RunPlan,
    WorkPackage,
)
from saberops.project_scope import graph as project_scope_graph
from saberops.project_scope.contracts import ProjectGraph, ProjectModule, ProjectScope
from saberops.project_scope.persistence import (
    ProjectScopeIntegrityError,
    load_scope_bundle,
)
from saberops.slicing import ProposedPackage, TaskSizingAssessment


@dataclass(frozen=True)
class PersistedPlan:
    """The created plan plus its package rows."""

    plan: RunPlan
    steps: tuple[PlanStep, ...]
    packages: tuple[WorkPackage, ...]


def build_context_capsule(
    package: WorkPackage, completed_summaries: tuple[str, ...] = ()
) -> dict[str, object]:
    """Return bounded, source-context-free worker context for a package.

    C04 projection: the raw full ``parent_objective`` is intentionally NOT
    exposed in the model-visible package capsule.  The full parent objective
    remains durably persisted on the :class:`WorkPackage` row for audit /
    reconstruction, but the package worker sees only the bounded
    ``parent_slug`` (deterministic, ``<= 120`` chars) and the bounded
    objective digest (~<= 2KB).  This prevents historical package-1 from
    receiving enough parent context to implement later packages.
    """
    from saberops.context_projection import (
        objective_digest,
        objective_slug,
    )

    parent_slug = objective_slug(package.parent_objective)
    parent_digest = objective_digest(package.parent_objective)
    return {
        "parent_slug": parent_slug,
        "parent_digest": parent_digest,
        "package_goal": package.goal,
        "acceptance_criteria": list(package.acceptance_criteria),
        "base_candidate_sha": package.base_candidate_sha or "UNKNOWN",
        "constraints": list(package.constraints),
        "completed_prerequisites": list(completed_summaries or package.prerequisite_summaries),
        "next_responsibility": package.goal,
        "gate_required": package.gate_required,
    }


def persist_plan(
    db: Database,
    run_id: str,
    parent_objective: str,
    assessment: TaskSizingAssessment,
    proposals: list[ProposedPackage],
    base_candidate_sha: str,
) -> PersistedPlan:
    """Create validated normalized plan/package rows and corresponding events."""
    now = current_iso_timestamp()
    plan = RunPlan(
        id=f"plan_{run_id}_{uuid.uuid4().hex[:8]}",
        run_id=run_id,
        status=PlanStatus.ACTIVE,
        version=1,
        created_at=now,
        updated_at=now,
    )
    db.create_run_plan(plan)
    db.record_event(
        run_id,
        "plan_created",
        payload={
            "plan_id": plan.id,
            "version": plan.version,
            "assessment_score": assessment.score,
            "assessment_signals": list(assessment.signals),
            "package_count": len(proposals),
        },
    )
    steps: list[PlanStep] = []
    packages: list[WorkPackage] = []
    package_ids = [
        f"pkg_{run_id}_{index + 1}_{uuid.uuid4().hex[:6]}" for index in range(len(proposals))
    ]
    step_ids = [
        f"step_{run_id}_{index + 1}_{uuid.uuid4().hex[:6]}" for index in range(len(proposals))
    ]
    for index, proposal in enumerate(proposals):
        dependency_steps = tuple(step_ids[item] for item in proposal.dependency_indexes)
        dependency_packages = tuple(package_ids[item] for item in proposal.dependency_indexes)
        initial = PlanStepStatus.READY if not dependency_steps else PlanStepStatus.PENDING
        package = WorkPackage(
            id=package_ids[index],
            run_id=run_id,
            plan_step_id=step_ids[index],
            parent_objective=parent_objective,
            goal=proposal.goal,
            acceptance_criteria=proposal.acceptance_criteria,
            dependency_package_ids=dependency_packages,
            base_candidate_sha=base_candidate_sha,
            relevant_paths=proposal.relevant_paths,
            long_running_hint=proposal.long_running_hint,
            long_running=bool(proposal.long_running_hint),
            long_running_reason="planner_hint"
            if proposal.long_running_hint
            else "short_or_unhinted",
            created_at=now,
            updated_at=now,
        )
        package = WorkPackage(
            **{
                **package.__dict__,
                "context_capsule_json": json.dumps(build_context_capsule(package)),
            }
        )
        step = PlanStep(
            id=step_ids[index],
            plan_id=plan.id,
            run_id=run_id,
            kind="work_package",
            title=proposal.title,
            goal=proposal.goal,
            status=initial,
            ordering=index + 1,
            dependency_ids=dependency_steps,
            work_package_id=package.id,
            created_at=now,
            updated_at=now,
        )
        db.create_plan_step(step)
        db.create_work_package(package)
        db.record_event(
            run_id,
            "plan_step_added",
            payload={
                "plan_id": plan.id,
                "step_id": step.id,
                "package_id": package.id,
                "ordering": step.ordering,
            },
        )
        db.record_event(
            run_id,
            "work_package_ready",
            payload={"plan_id": plan.id, "step_id": step.id, "package_id": package.id},
        )
        if initial == PlanStepStatus.READY:
            db.record_event(
                run_id,
                "plan_step_ready",
                payload={"plan_id": plan.id, "step_id": step.id, "package_id": package.id},
            )
        steps.append(step)
        packages.append(package)
    return PersistedPlan(plan=plan, steps=tuple(steps), packages=tuple(packages))


_C14_SCHEMA = "c14-a-independent-work-v2"
_C14_MAX_REASONS = 12
_C14_MAX_EVIDENCE = 24
_C14_MAX_ITEM_CHARS = 240

# Concurrent-mode security deny paths the represented contract commits to.
# These are the existing C11-C default sandbox deny paths
# (see ``saberops.sandbox.policy.GIT_INTERNAL_DENY`` /
# ``GITHUB_DENY``) -- the C09 ``FULL_GATE_PATTERNS`` vocabulary is a
# verification-level escalation surface and is NOT reinterpreted here as
# filesystem security denies.
_C14_CONCURRENT_DENY_PATHS: tuple[str, ...] = (".git/**", ".github/**")

# C09 graph construction already binds the repository's canonical devscope
# path-pattern matcher.  Workflow depends on project_scope (not devscope), so
# consume that exact existing binding without creating a second matcher or a
# new workflow -> devscope architecture edge.
_C09_ROOT_MATCH_ANY = cast(
    Callable[[tuple[str, ...], str], bool],
    project_scope_graph.match_any,  # type: ignore[attr-defined]  # existing C09 binding
)


def _canonical_digest(payload: object) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _bounded_items(items: Sequence[str], *, limit: int) -> tuple[str, ...]:
    normalized = sorted({item[:_C14_MAX_ITEM_CHARS] for item in items if item})
    if len(normalized) <= limit:
        return tuple(normalized)
    omitted = normalized[limit - 1 :]
    summary = f"OMITTED:{len(omitted)}:{_canonical_digest(omitted)}"
    return tuple(normalized[: limit - 1] + [summary])


def _valid_sha(value: str | None) -> bool:
    if value is None or len(value) != 40:
        return False
    return all(char in "0123456789abcdefABCDEF" for char in value)


def _decision_digest(
    *,
    run_id: str,
    state: IndependenceState,
    branches: Sequence[IndependentWorkBranch],
    integration_base_sha: str | None,
    sequential_fallback_required: bool,
    reasons: Sequence[str],
    evidence: Sequence[str],
) -> str:
    return _canonical_digest(
        {
            "schema": _C14_SCHEMA,
            "run_id": run_id,
            "state": state.value,
            "integration_base_sha": integration_base_sha,
            "sequential_fallback_required": sequential_fallback_required,
            "reasons": list(reasons),
            "evidence": list(evidence),
            "branches": [
                {
                    "branch_id": branch.branch_id,
                    "plan_step_id": branch.plan_step_id,
                    "work_package_id": branch.work_package_id,
                    "attempt_slot_id": branch.attempt_slot_id,
                    "attempt_id": branch.attempt_id,
                    "integration_base_sha": branch.integration_base_sha,
                    "isolation_requirement_id": branch.isolation_requirement_id,
                    "context_capsule_digest": branch.context_capsule_digest,
                    "owner_module_id": branch.owner_module_id,
                    "writable_existing_paths": list(branch.writable_existing_paths),
                    "writable_new_subtrees": list(branch.writable_new_subtrees),
                    "deny_paths": list(branch.deny_paths),
                    "write_contract_digest": branch.write_contract_digest,
                    "ordering_key": branch.ordering_key,
                    "candidate_slot_id": branch.candidate_slot_id,
                    "candidate_sha": branch.candidate_sha,
                }
                for branch in branches
            ],
        }
    )


def _not_established(
    run_id: str, reasons: Sequence[str], evidence: Sequence[str] = ()
) -> IndependentWorkDecision:
    bounded_reasons = _bounded_items(reasons, limit=_C14_MAX_REASONS)
    bounded_evidence = _bounded_items(evidence, limit=_C14_MAX_EVIDENCE)
    return IndependentWorkDecision(
        run_id=run_id,
        state=IndependenceState.NOT_ESTABLISHED,
        branches=(),
        integration_base_sha=None,
        sequential_fallback_required=True,
        reasons=bounded_reasons,
        evidence=bounded_evidence,
        decision_digest=_decision_digest(
            run_id=run_id,
            state=IndependenceState.NOT_ESTABLISHED,
            branches=(),
            integration_base_sha=None,
            sequential_fallback_required=True,
            reasons=bounded_reasons,
            evidence=bounded_evidence,
        ),
    )


def _dependency_reaches(
    start_id: str, target_id: str, dependencies: Mapping[str, set[str]]
) -> bool:
    pending = list(dependencies.get(start_id, ()))
    seen: set[str] = set()
    while pending:
        current = pending.pop()
        if current == target_id:
            return True
        if current in seen:
            continue
        seen.add(current)
        pending.extend(dependencies.get(current, ()))
    return False


def _normalized_paths(paths: Sequence[str]) -> tuple[str, ...] | None:
    if not paths:
        return None
    normalized: list[str] = []
    for raw in paths:
        if not raw or raw.startswith("/") or "\\" in raw:
            return None
        path = PurePosixPath(raw)
        if ".." in path.parts or path.as_posix() != raw:
            return None
        normalized.append(raw)
    if len(set(normalized)) != len(normalized):
        return None
    return tuple(sorted(normalized))


def _validate_declared_category(paths: Sequence[str]) -> tuple[str, ...] | None:
    """Validate one declared category (owned/interface/test) independently.

    Returns the canonical sorted tuple, the empty tuple when the category is
    empty, or ``None`` when the category is malformed.  Malformed evidence
    means: a non-empty raw string that is absolute, backslash-bearing,
    traversal-bearing, or otherwise not canonically equal to its
    ``PurePosixPath`` rendering; OR an intra-category duplicate entry.

    Cross-category role overlap (e.g. the same literal path appearing in
    ``owned_files`` AND ``interface_files``) is intentionally NOT detected
    here -- the two roles carry distinct C09 semantics and may legitimately
    coexist.  Cross-category overlap is preserved, then reconciled by the
    envelope union after both categories pass category-level validation.
    """
    normalized: list[str] = []
    for raw in paths:
        if not raw or raw.startswith("/") or "\\" in raw:
            return None
        path = PurePosixPath(raw)
        if ".." in path.parts or path.as_posix() != raw:
            return None
        normalized.append(raw)
    if len(set(normalized)) != len(normalized):
        return None
    return tuple(sorted(normalized))


def _authoritative_write_envelope(
    graph: ProjectGraph, owner_module_id: str
) -> tuple[str, ...] | None:
    """Return one complete graph-authoritative module write envelope or fail closed.

    C14-A: the envelope is the union ``owned_files ∪ interface_files ∪
    test_files`` (per section B).  ``interface_files``/``test_files`` are NOT
    C09 ownership claims; the section-B structural checks keep their distinct
    meanings.

    Each declared category is validated independently BEFORE the union, so an
    intra-category duplicate entry fails closed (malformed declared evidence
    is not silently coalesced by set conversion).  Cross-category role
    overlap remains legitimate and is preserved through the union.
    """
    module = graph.module_by_id(owner_module_id)
    if module is None:
        return None

    valid_owned = _validate_declared_category(module.owned_files)
    valid_interface = _validate_declared_category(module.interface_files)
    valid_test = _validate_declared_category(module.test_files)
    if valid_owned is None or valid_interface is None or valid_test is None:
        return None

    known_files = set(graph.files)
    known_tests = set(graph.test_files)

    # Section B rule 1+2+3: owned files must be canonical, present in graph.files,
    # and solely owned by this module.
    for path in module.owned_files:
        if path not in known_files:
            return None
        if graph.owners_of(path) != (owner_module_id,):
            return None

    # Section B rule 4: test files must be canonical and present in
    # graph.test_files.
    for path in module.test_files:
        if path not in known_tests:
            return None

    # Section B rule 5+6: interface files must be present in graph.files; if a
    # path is a production path owned solely by another module, do not grant
    # this branch write authority over it -- fail closed for THIS branch.
    for path in module.interface_files:
        if path not in known_files:
            return None
        owners = graph.owners_of(path)
        if owners and owners != (owner_module_id,):
            return None

    # ProjectModule.roots are C09 path-pattern contracts, not literal
    # directories.  Reuse the exact matcher already used by C09 graph
    # construction so wildcard roots cannot hide ambiguous/unowned paths.
    for path, owners in graph.ambiguous_files:
        if owner_module_id in owners or _C09_ROOT_MATCH_ANY(module.roots, path):
            return None
    for path in graph.unowned_files:
        if _C09_ROOT_MATCH_ANY(module.roots, path):
            return None

    # writable_existing_paths = owned ∪ interface ∪ test, each category already
    # canonical; sort the union once for determinism independent of category
    # enumeration order.
    combined = sorted(set(valid_owned) | set(valid_interface) | set(valid_test))
    return tuple(combined)


def _literal_subtree_for_root(root: str) -> str | None:
    """Return one literal ``<canonical-dir>/**`` subtree for ``root`` or None.

    Section C: a root is admissible in shape iff it is a non-empty,
    repository-relative, glob-metacharacter-free, traversal-free canonical
    prefix that ends in exactly ``/**``.
    """
    if not root:
        return None
    if root.startswith("/") or "\\" in root:
        return None
    if not root.endswith("/**"):
        return None
    prefix = root[:-3]
    if not prefix:
        return None
    if ".." in PurePosixPath(prefix).parts:
        return None
    if PurePosixPath(prefix).as_posix() != prefix:
        return None
    for char in "*?[]{}!":
        if char in prefix:
            return None
    return f"{prefix}/**"


def _literal_new_subtrees(module: ProjectModule) -> tuple[str, ...] | None:
    """Return canonical ``<dir>/**`` subtrees from ``module.roots``.

    Every root must first pass the exact literal-subtree admission rule
    (section C).  After validation the returned tuple is the canonical
    ``tuple(sorted(unique_valid_subtrees))`` form, so two ``ProjectModule``
    instances that represent the same set of literal subtrees in different
    ``roots`` enumeration orders produce identical ``writable_new_subtrees``
    output -- and therefore identical ``write_contract_digest``,
    ``branch_id``, ``decision_digest``, ``integration_id``,
    ``integration_digest`` and ``validation_id`` downstream.

    Returns ``None`` if any selected root fails the literal-subtree filter.
    """
    subtrees: set[str] = set()
    for root in module.roots:
        subtree = _literal_subtree_for_root(root)
        if subtree is None:
            return None
        subtrees.add(subtree)
    return tuple(sorted(subtrees))


def _write_contract_digest(
    *,
    existing: tuple[str, ...],
    subtrees: tuple[str, ...],
    deny: tuple[str, ...],
) -> str:
    """Stable digest over all three contract components in canonical order.

    All three component inputs are set-like: their enumeration order carries
    no semantic meaning.  The digest therefore hashes the canonical
    ``sorted(unique)`` rendering of each component, so two callers passing
    equivalent set-like inputs in different enumeration orders produce an
    identical digest.  A genuine membership change in any component still
    rotates the digest and every transitively bound identity.
    """
    return _canonical_digest(
        {
            "schema": _C14_SCHEMA,
            "existing_paths": sorted(set(existing)),
            "new_subtrees": sorted(set(subtrees)),
            "deny_paths": sorted(set(deny)),
        }
    )


def _subtree_prefix_parts(subtree: str) -> tuple[str, ...]:
    assert subtree.endswith("/**")
    return PurePosixPath(subtree[:-3]).parts


def _path_inside_subtree(path: str, subtree: str) -> bool:
    prefix = _subtree_prefix_parts(subtree)
    parts = PurePosixPath(path).parts
    return len(parts) >= len(prefix) and parts[: len(prefix)] == prefix


def _subtree_pair_conflict(a: str, b: str) -> bool:
    if a == b:
        return True
    a_parts = _subtree_prefix_parts(a)
    b_parts = _subtree_prefix_parts(b)
    n = min(len(a_parts), len(b_parts))
    return a_parts[:n] == b_parts[:n]


def _write_contracts_conflict(
    left_existing: Sequence[str],
    left_subtrees: Sequence[str],
    right_existing: Sequence[str],
    right_subtrees: Sequence[str],
) -> bool:
    """Pairwise write-contract conflict predicate (section E).

    Conflicting iff:
    1. any existing literal path is shared between the two contracts;
    2. any existing path of A lies inside any new subtree of B, or vice versa;
    3. any two new subtrees are identical or one is the ancestor of the other.
    """
    left_existing_set = set(left_existing)
    right_existing_set = set(right_existing)
    if left_existing_set & right_existing_set:
        return True
    for path in left_existing:
        for subtree in right_subtrees:
            if _path_inside_subtree(path, subtree):
                return True
    for path in right_existing:
        for subtree in left_subtrees:
            if _path_inside_subtree(path, subtree):
                return True
    for left_subtree in left_subtrees:
        for right_subtree in right_subtrees:
            if _subtree_pair_conflict(left_subtree, right_subtree):
                return True
    return False


def _parse_context_capsule_digest(package: WorkPackage) -> str | None:
    if package.context_capsule_json is None:
        return None
    try:
        payload = json.loads(package.context_capsule_json)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or not payload:
        return None
    return _canonical_digest(payload)


def _parse_package_scope(package: WorkPackage) -> ProjectScope | None:
    if package.scope_json is None:
        return None
    try:
        payload = json.loads(package.scope_json)
        if not isinstance(payload, dict):
            return None
        return ProjectScope.from_dict(payload)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def represent_independent_ready_work(
    db: Database,
    run_id: str,
    *,
    plan_step_ids: Sequence[str] | None = None,
) -> IndependentWorkDecision:
    """Represent dependency-safe independent READY work without dispatching it.

    Admission is deliberately fail-closed.  The existing persisted PlanStep /
    WorkPackage dependency graph is the scheduling authority, while the frozen
    C09 project graph is the ownership/write-envelope authority.  Advisory
    ``WorkPackage.relevant_paths`` may be recorded as bounded evidence, but it
    never defines or narrows the authoritative potential-write envelope.
    """
    all_steps = db.get_plan_steps(run_id)
    step_by_id = {step.id: step for step in all_steps}
    all_packages = db.get_work_packages(run_id)
    package_by_id = {package.id: package for package in all_packages}

    if plan_step_ids is None:
        selected = [step for step in all_steps if step.status == PlanStepStatus.READY]
    else:
        if len(set(plan_step_ids)) != len(tuple(plan_step_ids)):
            return _not_established(run_id, ("DUPLICATE_STEP_IDENTITY",))
        missing = sorted(set(plan_step_ids) - set(step_by_id))
        if missing:
            return _not_established(run_id, (f"UNKNOWN_PLAN_STEP:{missing[0]}",))
        selected = [step_by_id[step_id] for step_id in plan_step_ids]

    selected = sorted(selected, key=lambda step: (step.ordering, step.id))
    if len(selected) < 2:
        return _not_established(run_id, ("FEWER_THAN_TWO_READY_BRANCHES",))
    if any(step.status != PlanStepStatus.READY for step in selected):
        return _not_established(run_id, ("NON_READY_PLAN_STEP",))
    if len({step.id for step in selected}) != len(selected):
        return _not_established(run_id, ("DUPLICATE_STEP_IDENTITY",))
    if len({step.ordering for step in selected}) != len(selected):
        return _not_established(run_id, ("AMBIGUOUS_PLAN_ORDERING",))

    selected_packages: list[WorkPackage] = []
    for step in selected:
        if step.run_id != run_id or step.work_package_id is None:
            return _not_established(run_id, (f"MALFORMED_STEP_BINDING:{step.id}",))
        package = package_by_id.get(step.work_package_id)
        if package is None or package.run_id != run_id or package.plan_step_id != step.id:
            return _not_established(run_id, (f"MALFORMED_PACKAGE_BINDING:{step.id}",))
        selected_packages.append(package)
    if len({package.id for package in selected_packages}) != len(selected_packages):
        return _not_established(run_id, ("DUPLICATE_PACKAGE_IDENTITY",))

    step_dependencies = {step.id: set(step.dependency_ids) for step in all_steps}
    package_dependencies = {
        package.id: set(package.dependency_package_ids) for package in all_packages
    }
    for step in all_steps:
        if any(dep not in step_by_id for dep in step.dependency_ids):
            return _not_established(run_id, (f"MALFORMED_STEP_DEPENDENCY:{step.id}",))
    for package in all_packages:
        if any(dep not in package_by_id for dep in package.dependency_package_ids):
            return _not_established(run_id, (f"MALFORMED_PACKAGE_DEPENDENCY:{package.id}",))
    for step, package in zip(selected, selected_packages, strict=True):
        translated = {
            step_by_id[dep].work_package_id
            for dep in step.dependency_ids
            if step_by_id[dep].work_package_id is not None
        }
        if translated != set(package.dependency_package_ids):
            return _not_established(run_id, (f"DEPENDENCY_GRAPH_MISMATCH:{step.id}",))

    for left_index, left_step in enumerate(selected):
        left_package = selected_packages[left_index]
        for right_index in range(left_index + 1, len(selected)):
            right_step = selected[right_index]
            right_package = selected_packages[right_index]
            if _dependency_reaches(
                left_step.id, right_step.id, step_dependencies
            ) or _dependency_reaches(right_step.id, left_step.id, step_dependencies):
                return _not_established(
                    run_id,
                    (f"DEPENDENCY_EDGE:{left_step.id}:{right_step.id}",),
                )
            if _dependency_reaches(
                left_package.id, right_package.id, package_dependencies
            ) or _dependency_reaches(right_package.id, left_package.id, package_dependencies):
                return _not_established(
                    run_id,
                    (f"DEPENDENCY_EDGE:{left_package.id}:{right_package.id}",),
                )

    bases = {package.base_candidate_sha for package in selected_packages}
    if len(bases) != 1:
        return _not_established(run_id, ("INTEGRATION_BASE_MISMATCH",))
    integration_base_sha = next(iter(bases))
    if not _valid_sha(integration_base_sha):
        return _not_established(run_id, ("MALFORMED_INTEGRATION_BASE_SHA",))
    assert integration_base_sha is not None

    try:
        scope_bundle = load_scope_bundle(db, run_id)
    except ProjectScopeIntegrityError:
        return _not_established(run_id, ("PROJECT_SCOPE_INTEGRITY_FAILURE",))
    if scope_bundle is None:
        return _not_established(run_id, ("PROJECT_SCOPE_MISSING",))

    owners: list[str] = []
    write_envelopes: list[tuple[str, ...]] = []
    new_subtree_lists: list[tuple[str, ...]] = []
    envelope_digests: list[str] = []
    capsule_digests: list[str] = []
    evidence: list[str] = [f"PROJECT_GRAPH:{scope_bundle.graph.digest}"]
    for package in selected_packages:
        scope = _parse_package_scope(package)
        if scope is None:
            return _not_established(run_id, (f"PACKAGE_SCOPE_MISSING_OR_MALFORMED:{package.id}",))
        if (
            scope.graph_digest != scope_bundle.graph.digest
            or not scope.closure_complete
            or scope.truncated
            or scope.primary_module is None
            or bool(scope.ambiguous_modules)
            or scope.confidence != "high"
        ):
            return _not_established(
                run_id, (f"PACKAGE_SCOPE_INCOMPLETE_OR_AMBIGUOUS:{package.id}",)
            )

        owner = scope.primary_module
        module = scope_bundle.graph.module_by_id(owner)
        if module is None:
            return _not_established(
                run_id, (f"AUTHORITATIVE_WRITE_ENVELOPE_INCOMPLETE:{package.id}:{owner}",)
            )
        envelope = _authoritative_write_envelope(scope_bundle.graph, owner)
        if envelope is None:
            return _not_established(
                run_id, (f"AUTHORITATIVE_WRITE_ENVELOPE_INCOMPLETE:{package.id}:{owner}",)
            )
        new_subtrees = _literal_new_subtrees(module)
        if new_subtrees is None:
            return _not_established(
                run_id,
                (f"UNENFORCEABLE_CONCURRENT_ROOT:{package.id}:{owner}",),
            )
        contract_digest = _write_contract_digest(
            existing=envelope,
            subtrees=new_subtrees,
            deny=_C14_CONCURRENT_DENY_PATHS,
        )
        advisory_paths = _normalized_paths(package.relevant_paths)
        advisory_evidence = (
            _canonical_digest(list(advisory_paths))
            if advisory_paths is not None
            else "MISSING_OR_MALFORMED"
        )
        capsule_digest = _parse_context_capsule_digest(package)
        if capsule_digest is None:
            return _not_established(run_id, (f"CONTEXT_CAPSULE_MISSING_OR_MALFORMED:{package.id}",))
        owners.append(owner)
        write_envelopes.append(envelope)
        new_subtree_lists.append(new_subtrees)
        envelope_digests.append(contract_digest)
        capsule_digests.append(capsule_digest)
        evidence.extend(
            (
                f"SCOPE:{package.id}:{scope.digest}",
                f"OWNER:{package.id}:{owner}",
                f"WRITE_CONTRACT:{package.id}:{owner}:{contract_digest}"
                f":{len(envelope)}:{len(new_subtrees)}",
                f"ADVISORY_RELEVANT_PATHS:{package.id}:{advisory_evidence}",
                f"CONTEXT:{package.id}:{capsule_digest}",
            )
        )

    for left_index, left_owner in enumerate(owners):
        left_module = selected_packages[left_index]
        for right_index in range(left_index + 1, len(owners)):
            right_module = selected_packages[right_index]
            if left_owner == owners[right_index]:
                return _not_established(
                    run_id,
                    (f"OWNERSHIP_CONFLICT:{left_owner}",),
                    evidence,
                )
            if _write_contracts_conflict(
                write_envelopes[left_index],
                new_subtree_lists[left_index],
                write_envelopes[right_index],
                new_subtree_lists[right_index],
            ):
                return _not_established(
                    run_id,
                    (f"WRITE_CONTRACT_CONFLICT:{left_module.id}:{right_module.id}",),
                    evidence,
                )

    branches: list[IndependentWorkBranch] = []
    for step, package, owner, envelope, new_subtrees, contract_digest, capsule_digest in zip(
        selected,
        selected_packages,
        owners,
        write_envelopes,
        new_subtree_lists,
        envelope_digests,
        capsule_digests,
        strict=True,
    ):
        ordering_key = f"{step.ordering:08d}:{step.id}:{package.id}"
        branch_seed = {
            "schema": _C14_SCHEMA,
            "run_id": run_id,
            "plan_step_id": step.id,
            "work_package_id": package.id,
            "integration_base_sha": integration_base_sha,
            "context_capsule_digest": capsule_digest,
            "owner_module_id": owner,
            "write_contract_digest": contract_digest,
            "ordering_key": ordering_key,
        }
        branch_id = f"branch_{_canonical_digest(branch_seed)[:24]}"
        branches.append(
            IndependentWorkBranch(
                branch_id=branch_id,
                plan_step_id=step.id,
                work_package_id=package.id,
                attempt_slot_id=f"attempt_slot:{step.id}",
                attempt_id=step.attempt_id,
                integration_base_sha=integration_base_sha,
                isolation_requirement_id=f"isolated_worktree:{branch_id}",
                context_capsule_digest=capsule_digest,
                owner_module_id=owner,
                writable_existing_paths=envelope,
                writable_new_subtrees=new_subtrees,
                deny_paths=_C14_CONCURRENT_DENY_PATHS,
                write_contract_digest=contract_digest,
                ordering_key=ordering_key,
                candidate_slot_id=f"candidate_slot:{branch_id}",
                candidate_sha=None,
            )
        )
    branches.sort(key=lambda branch: branch.ordering_key)
    bounded_evidence = _bounded_items(evidence, limit=_C14_MAX_EVIDENCE)
    decision_digest = _decision_digest(
        run_id=run_id,
        state=IndependenceState.ESTABLISHED,
        branches=branches,
        integration_base_sha=integration_base_sha,
        sequential_fallback_required=False,
        reasons=(),
        evidence=bounded_evidence,
    )
    return IndependentWorkDecision(
        run_id=run_id,
        state=IndependenceState.ESTABLISHED,
        branches=tuple(branches),
        integration_base_sha=integration_base_sha,
        sequential_fallback_required=False,
        reasons=(),
        evidence=bounded_evidence,
        decision_digest=decision_digest,
    )


def represent_deterministic_integration(
    decision: IndependentWorkDecision,
    *,
    candidate_shas: Mapping[str, str | None] | None = None,
    integration_result_sha: str | None = None,
    failure_reason: str | None = None,
) -> DeterministicIntegration:
    """Represent deterministic integration for an ESTABLISHED branch set only."""
    if decision.state != IndependenceState.ESTABLISHED or decision.sequential_fallback_required:
        raise ValueError("independent integration is not admitted; sequential fallback is required")
    if decision.integration_base_sha is None or not decision.branches:
        raise ValueError("established independent-work decision is incomplete")
    overrides = dict(candidate_shas or {})
    branch_ids = {branch.branch_id for branch in decision.branches}
    unknown = sorted(set(overrides) - branch_ids)
    if unknown:
        raise ValueError(f"unknown branch candidate slot: {unknown[0]}")

    participants: list[IntegrationParticipant] = []
    for branch in sorted(decision.branches, key=lambda item: item.ordering_key):
        candidate_sha = overrides.get(branch.branch_id, branch.candidate_sha)
        if candidate_sha is not None and not _valid_sha(candidate_sha):
            raise ValueError(f"malformed candidate SHA for {branch.branch_id}")
        participants.append(
            IntegrationParticipant(
                branch_id=branch.branch_id,
                plan_step_id=branch.plan_step_id,
                work_package_id=branch.work_package_id,
                attempt_slot_id=branch.attempt_slot_id,
                candidate_slot_id=branch.candidate_slot_id,
                candidate_sha=candidate_sha,
            )
        )
    if integration_result_sha is not None and not _valid_sha(integration_result_sha):
        raise ValueError("malformed integration result SHA")
    if failure_reason is not None and integration_result_sha is not None:
        raise ValueError("failed integration cannot also carry an integration result SHA")
    if failure_reason is not None:
        disposition = IntegrationDisposition.FAILED
    elif integration_result_sha is not None:
        if any(item.candidate_sha is None for item in participants):
            raise ValueError("integration result requires every candidate SHA slot to be filled")
        disposition = IntegrationDisposition.INTEGRATED
    elif all(item.candidate_sha is not None for item in participants):
        disposition = IntegrationDisposition.READY_FOR_INTEGRATION
    else:
        disposition = IntegrationDisposition.PENDING_CANDIDATES

    static_payload = {
        "schema": _C14_SCHEMA,
        "integration_base_sha": decision.integration_base_sha,
        "participants": [
            {
                "branch_id": item.branch_id,
                "plan_step_id": item.plan_step_id,
                "work_package_id": item.work_package_id,
                "attempt_slot_id": item.attempt_slot_id,
                "candidate_slot_id": item.candidate_slot_id,
            }
            for item in participants
        ],
        "deterministic_order": [item.branch_id for item in participants],
    }
    integration_id = f"integration_{_canonical_digest(static_payload)[:24]}"
    bounded_failure = failure_reason[:_C14_MAX_ITEM_CHARS] if failure_reason else None
    full_payload = {
        **static_payload,
        "integration_id": integration_id,
        "candidate_results": [item.candidate_sha for item in participants],
        "disposition": disposition.value,
        "integration_result_sha": integration_result_sha,
        "failure_reason": bounded_failure,
        "post_integration_validation_required": True,
    }
    integration_digest = _canonical_digest(full_payload)
    return DeterministicIntegration(
        integration_id=integration_id,
        integration_digest=integration_digest,
        integration_base_sha=decision.integration_base_sha,
        participants=tuple(participants),
        deterministic_order=tuple(item.branch_id for item in participants),
        disposition=disposition,
        integration_result_sha=integration_result_sha,
        failure_reason=bounded_failure,
        post_integration_validation_required=True,
    )


def represent_post_integration_validation(
    integration: DeterministicIntegration,
    *,
    state: PostIntegrationValidationState = PostIntegrationValidationState.REQUIRED_PENDING,
    reason: str | None = None,
) -> PostIntegrationValidation:
    """Bind validation state to the exact deterministic integration digest."""
    if state in (
        PostIntegrationValidationState.PASSED,
        PostIntegrationValidationState.FAILED,
    ) and (
        integration.disposition != IntegrationDisposition.INTEGRATED
        or integration.integration_result_sha is None
    ):
        raise ValueError("terminal validation requires a represented integration result")
    if state == PostIntegrationValidationState.FAILED and not reason:
        raise ValueError("failed validation requires a bounded reason")
    bounded_reason = reason[:_C14_MAX_ITEM_CHARS] if reason else None
    binding_payload = {
        "schema": _C14_SCHEMA,
        "integration_id": integration.integration_id,
        "integration_digest": integration.integration_digest,
        "integration_result_sha": integration.integration_result_sha,
    }
    validation_id = f"validation_{_canonical_digest(binding_payload)[:24]}"
    return PostIntegrationValidation(
        validation_id=validation_id,
        integration_id=integration.integration_id,
        integration_digest=integration.integration_digest,
        integration_result_sha=integration.integration_result_sha,
        state=state,
        reason=bounded_reason,
    )


def mark_step(
    db: Database,
    step: PlanStep,
    status: PlanStepStatus,
    *,
    blocker: str | None = None,
    error: str | None = None,
    attempt_id: str | None = None,
) -> None:
    """Persist a state transition and a stable event vocabulary."""
    db.update_plan_step(step.id, status, blocker=blocker, error=error, attempt_id=attempt_id)
    suffix = {
        PlanStepStatus.READY: "ready",
        PlanStepStatus.RUNNING: "started",
        PlanStepStatus.COMPLETED: "completed",
        PlanStepStatus.BLOCKED: "blocked",
        PlanStepStatus.FAILED: "failed",
        PlanStepStatus.SKIPPED: "blocked",
        PlanStepStatus.PENDING: "updated",
    }[status]
    payload = {
        "plan_id": step.plan_id,
        "step_id": step.id,
        "package_id": step.work_package_id,
        "status": status.value,
    }
    if blocker is not None:
        payload["blocker"] = blocker
    if error is not None:
        payload["error"] = error
    db.record_event(step.run_id, f"plan_step_{suffix}", attempt_id=attempt_id, payload=payload)


def ready_dependents(db: Database, run_id: str) -> list[PlanStep]:
    """Move dependency-satisfied pending steps to READY in stored plan order."""
    steps = db.get_plan_steps(run_id)
    done = {step.id for step in steps if step.status == PlanStepStatus.COMPLETED}
    changed: list[PlanStep] = []
    for step in steps:
        if step.status == PlanStepStatus.PENDING and set(step.dependency_ids) <= done:
            mark_step(db, step, PlanStepStatus.READY)
            changed.append(step)
    return changed
