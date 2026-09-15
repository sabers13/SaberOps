"""Compact deterministic developer/operator scope surface.

``orch scope graph``       build/inspect a project graph (``--repo`` for an
                           external repository; read-only, JSON mode).
``orch scope show``        resolve and render a bounded scope for a query.
``orch scope expand``      run-scoped deterministic expansion (resolves
                           ORCH_RUN_ID / ORCH_DB_PATH like C08 tooling).
``orch scope affected``    shadow affected-verification planning.

All inspection commands are read-only: zero DB mutation, zero model calls,
zero network calls, and no source content is printed by default.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from saberops.db import Database
from saberops.devscope.affected import changed_paths_from_git
from saberops.project_scope.affected import plan_affected
from saberops.project_scope.expansion import ScopeExpansionError, expand_run_scope
from saberops.project_scope.graph import build_project_graph
from saberops.project_scope.metadata import ProjectMetadataError
from saberops.project_scope.persistence import (
    ProjectScopeIntegrityError,
    scope_section_for_run,
)
from saberops.project_scope.resolver import resolve_scope


def _print(payload: object, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, sort_keys=True, indent=2, default=str))
    else:
        print(payload)


def _repo(args: argparse.Namespace) -> Path:
    return Path(str(args.repo)).resolve() if getattr(args, "repo", None) else Path.cwd()


def handle_scope(args: argparse.Namespace) -> int:
    """Route the ``orch scope`` command group."""
    command = str(getattr(args, "scope_command", ""))
    try:
        if command == "graph":
            return _run_graph(args)
        if command == "show":
            return _run_show(args)
        if command == "expand":
            return _run_expand(args)
        if command == "affected":
            return _run_affected(args)
    except ProjectMetadataError as exc:
        print(f"[!] project metadata rejected (fail closed): {exc}", file=sys.stderr)
        return 2
    except ProjectScopeIntegrityError as exc:
        print(f"[!] project scope integrity check failed (fail closed): {exc}", file=sys.stderr)
        return 3
    except ScopeExpansionError as exc:
        print(f"[!] scope expansion rejected: {exc}", file=sys.stderr)
        return 1
    print(f"[!] unknown scope command: {command}", file=sys.stderr)
    return 1


def _run_graph(args: argparse.Namespace) -> int:
    root = _repo(args)
    graph = build_project_graph(root)
    payload: dict[str, Any] = {
        "project_id": graph.project_id,
        "graph_digest": graph.digest,
        "graph_source": graph.graph_source.value,
        "analyzer": graph.analyzer,
        "confidence": graph.confidence.value,
        "modules": graph.module_ids,
        "module_count": len(graph.modules),
        "files": len(graph.files),
        "unowned_files": len(graph.unowned_files),
        "dependency_edges": len(graph.edges),
        "entry_points": list(graph.entry_points),
        "reasons": list(graph.reasons),
        "files_scanned": graph.files_scanned,
        "python_files_parsed": graph.python_files_parsed,
    }
    if args.json:
        print(json.dumps(payload, sort_keys=True, indent=2))
    else:
        print(f"graph:        {payload['graph_digest']}")
        print(f"source:       {payload['graph_source']}  analyzer: {payload['analyzer']}")
        print(f"confidence:   {payload['confidence']}")
        print(
            f"modules:      {', '.join(payload['modules']) if payload['modules'] else '(none)'}"
        )
        print(
            f"files:        {payload['files']} production, {payload['unowned_files']} unowned, "
            f"{payload['dependency_edges']} module edges"
        )
        print("reasons:")
        for reason in payload["reasons"]:
            print(f"  - {reason}")
    return 0


def _run_show(args: argparse.Namespace) -> int:
    root = _repo(args)
    graph = build_project_graph(root)
    scope = resolve_scope(graph, str(args.query))
    if args.json:
        print(json.dumps(scope.to_dict(), sort_keys=True, indent=2))
    else:
        print(f"primary:        {scope.primary_module or '(none — ambiguous/unknown)'}")
        print(f"included:       {', '.join(scope.included_modules) or '(none)'}")
        print(
            f"confidence:     {scope.confidence}  closure_complete: {scope.closure_complete}  "
            f"truncated: {scope.truncated}"
        )
        print(f"verification:   {scope.verification_level}")
        print("source_paths:")
        for path in scope.source_paths:
            print(f"  {path}")
        print("tests:")
        for path in scope.test_paths:
            print(f"  {path}")
        print("reasons:")
        for reason in scope.reasons:
            print(f"  - {reason}")
    return 0


def _run_expand(args: argparse.Namespace) -> int:
    db_path = getattr(args, "db", None) or os.environ.get("ORCH_DB_PATH", "").strip() or None
    run_id = getattr(args, "run_id", None) or os.environ.get("ORCH_RUN_ID", "").strip() or None
    attempt_id = os.environ.get("ORCH_ATTEMPT_ID", "").strip() or None
    if db_path is None or run_id is None:
        print(
            "[!] scope expansion requires --db/ORCH_DB_PATH and --run-id/ORCH_RUN_ID",
            file=sys.stderr,
        )
        return 1
    db = Database(db_path)
    outcome = expand_run_scope(
        db,
        run_id,
        path=args.path,
        symbol=args.symbol,
        reason=str(args.reason),
        attempt_id=attempt_id,
    )
    _print(outcome, as_json=bool(args.json))
    return 0


def _run_affected(args: argparse.Namespace) -> int:
    root = _repo(args)
    graph = build_project_graph(root)
    if getattr(args, "base", None):
        changed = changed_paths_from_git(root, str(args.base))
    else:
        changed = tuple(str(item) for item in (getattr(args, "paths", None) or ()))
    plan = plan_affected(graph, changed)
    if args.json:
        print(json.dumps(plan.to_dict(), sort_keys=True, indent=2))
    else:
        payload: dict[str, Any] = plan.to_dict()
        print(f"level:            {payload['verification_level']}")
        print(f"changed_modules:  {payload['changed_modules']}")
        print(f"affected_modules: {payload['affected_modules']}")
        print(f"unknown_paths:    {payload['unknown_paths']}")
        print(f"tests:            {len(payload['tests'])}")
        print("reasons:")
        for reason in payload["reasons"]:
            print(f"  - {reason}")
    return 0


def show_run_scope(db_path: str, run_id: str, as_json: bool = False) -> int:
    """Read-only reconstruction of a run's persisted scope section."""
    db = Database(db_path, read_only=True)
    try:
        section = scope_section_for_run(db, run_id)
    except ProjectScopeIntegrityError as exc:
        print(f"[!] project scope integrity check failed (fail closed): {exc}", file=sys.stderr)
        return 3
    if section is None:
        print(f"[!] run '{run_id}' has no frozen project scope")
        return 1
    if as_json:
        print(json.dumps({"section": section}))
    else:
        print(section, end="")
    return 0
