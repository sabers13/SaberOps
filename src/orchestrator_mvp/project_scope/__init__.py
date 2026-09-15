"""C09 project scope: generic, deterministic, module-aware project understanding.

This package turns F01's repository-specific scoping foundation into a normal
Orch runtime capability that works on any managed repository:

* a language-independent, serializable :class:`~contracts.ProjectGraph`;
* explicit project metadata under ``.orch/`` (highest precedence, fail-closed);
* an F01 adapter that reuses an existing ``architecture/modules`` layout;
* a deterministic Python analyzer that infers conservative modules from
  package boundaries and AST import evidence;
* a conservative fallback for unknown or mixed-language repositories;
* task-to-module scope resolution with bounded, dependency-closed context;
* deterministic worker context expansion;
* affected verification planning that always fails broad.

Zero model, provider or network calls.  Discovery is read-only: it never
creates ``.orch`` metadata, moves files or rewrites anything in the target
repository.
"""

from __future__ import annotations

from orchestrator_mvp.project_scope.affected import AffectedPlan, plan_affected
from orchestrator_mvp.project_scope.context import render_scope_section
from orchestrator_mvp.project_scope.contracts import (
    ANALYZER_VERSION,
    GRAPH_SCHEMA_VERSION,
    Confidence,
    GraphSource,
    ProjectGraph,
    ProjectModule,
    ScopeBudget,
    canonical_json,
    digest_of,
)
from orchestrator_mvp.project_scope.expansion import ScopeExpansionError, expand_run_scope
from orchestrator_mvp.project_scope.graph import GraphBuildStats, build_project_graph
from orchestrator_mvp.project_scope.metadata import ProjectMetadataError
from orchestrator_mvp.project_scope.persistence import (
    ProjectScopeIntegrityError,
    ScopeBundle,
    freeze_run_scope,
    load_scope_bundle,
    scope_section_for_run,
)
from orchestrator_mvp.project_scope.resolver import resolve_scope

__all__ = [
    "ANALYZER_VERSION",
    "AffectedPlan",
    "Confidence",
    "GRAPH_SCHEMA_VERSION",
    "GraphBuildStats",
    "GraphSource",
    "ProjectGraph",
    "ProjectMetadataError",
    "ProjectModule",
    "ProjectScopeIntegrityError",
    "ScopeBudget",
    "ScopeBundle",
    "ScopeExpansionError",
    "build_project_graph",
    "canonical_json",
    "digest_of",
    "expand_run_scope",
    "freeze_run_scope",
    "load_scope_bundle",
    "plan_affected",
    "render_scope_section",
    "resolve_scope",
    "scope_section_for_run",
]
