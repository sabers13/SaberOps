"""Devscope: deterministic, dependency-free repository intelligence.

Devscope answers two questions that decide how safely an agent can work in a
large repository:

* what is the smallest dependency-closed *context* needed to make a change;
* what is the smallest dependency-closed *verification scope* that would still
  catch a mistake in that change.

It is stdlib-only (``ast``, ``tomllib``, ``pathlib``), imports no orchestrator
runtime module, and contacts no model, provider, network or database.  It runs
in shadow mode: it recommends scope, and never replaces the authoritative full
repository gate.
"""

from __future__ import annotations

from saberops.devscope.affected import AffectedPlan, plan_affected
from saberops.devscope.architecture import (
    ArchitectureReport,
    Finding,
    ModuleEdge,
    check_architecture,
)
from saberops.devscope.context import ContextBudget, ContextPlan, plan_context
from saberops.devscope.graph import SourceFile, SourceGraph, Symbol, build_source_graph
from saberops.devscope.manifest import ManifestError, ModuleManifest, load_manifests
from saberops.devscope.owners import OwnershipReport, resolve_ownership
from saberops.devscope.repomap import build_repo_map, render_repo_map, repo_map_json
from saberops.devscope.scope import ScopeResult, resolve_scope

__all__ = [
    "AffectedPlan",
    "ArchitectureReport",
    "ContextBudget",
    "ContextPlan",
    "Finding",
    "ManifestError",
    "ModuleEdge",
    "ModuleManifest",
    "OwnershipReport",
    "ScopeResult",
    "SourceFile",
    "SourceGraph",
    "Symbol",
    "build_repo_map",
    "build_source_graph",
    "check_architecture",
    "load_manifests",
    "plan_affected",
    "plan_context",
    "render_repo_map",
    "repo_map_json",
    "resolve_ownership",
    "resolve_scope",
]
