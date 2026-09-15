"""Compact command-line surface for devscope.

Deliberately separate from ``saberops.cli``: devscope is dev-time
repository intelligence with no runtime dependencies, and mixing it into the
operator CLI would couple the two.  Output is written for an LLM reader --
a few lines on success, exact diagnostics on failure, complete structure
under ``--json``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from saberops.devscope import affected as affected_mod
from saberops.devscope import context as context_mod
from saberops.devscope.architecture import check_architecture, report_to_dict
from saberops.devscope.manifest import ManifestError
from saberops.devscope.paths import repo_root
from saberops.devscope.repomap import build_repo_map, render_repo_map, repo_map_json
from saberops.devscope.scope import resolve_scope


def _emit_json(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def build_parser() -> argparse.ArgumentParser:
    """Build the devscope argument parser."""
    parser = argparse.ArgumentParser(
        prog="python -m saberops.devscope",
        description="Deterministic repository scoping for bounded agent context.",
    )
    parser.add_argument(
        "--root", type=Path, default=None, help="repository root (default: inferred)"
    )
    parser.add_argument("--json", action="store_true", help="emit complete structured JSON")

    # The same two flags are accepted after the subcommand.  ``SUPPRESS`` keeps
    # the subparser from overwriting a value already given before it.
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--root", type=Path, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    shared.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS
    )

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("modules", help="list logical modules and their ownership", parents=[shared])
    sub.add_parser(
        "check", help="validate module contracts against actual imports", parents=[shared]
    )
    sub.add_parser("map", help="print the compact repository/symbol map", parents=[shared])

    scope_parser = sub.add_parser(
        "scope", help="resolve a task to logical modules", parents=[shared]
    )
    scope_parser.add_argument("--query", default="", help="free-text task description")
    scope_parser.add_argument("--path", action="append", default=[], help="explicit seed path")
    scope_parser.add_argument("--symbol", action="append", default=[], help="explicit seed symbol")

    context_parser = sub.add_parser(
        "context", help="plan a bounded context pack", parents=[shared]
    )
    context_parser.add_argument("--query", default="", help="free-text task description")
    context_parser.add_argument("--path", action="append", default=[], help="explicit seed path")
    context_parser.add_argument(
        "--symbol", action="append", default=[], help="explicit seed symbol"
    )
    context_parser.add_argument(
        "--max-sources", type=int, default=context_mod.DEFAULT_SOURCE_BUDGET
    )
    context_parser.add_argument("--max-tests", type=int, default=context_mod.DEFAULT_TEST_BUDGET)
    context_parser.add_argument(
        "--depth", type=int, default=context_mod.DEFAULT_DEPENDENCY_DEPTH
    )

    affected_parser = sub.add_parser(
        "affected",
        help="shadow-mode affected verification scope (never replaces `make gate`)",
        parents=[shared],
    )
    affected_parser.add_argument("--path", action="append", default=[], help="changed path")
    affected_parser.add_argument("--base", default=None, help="git base ref/SHA to diff against")
    return parser


def _run_modules(root: Path, as_json: bool) -> int:
    report = check_architecture(root)
    if as_json:
        _emit_json(report_to_dict(report))
        return 0
    for manifest in report.manifests:
        owned = sum(
            1 for owner in report.ownership.owner_by_path.values() if owner == manifest.name
        )
        sys.stdout.write(
            f"{manifest.name:<14} files={owned:<3} risk={manifest.risk:<6} "
            f"allowed={len(manifest.allowed_dependencies)} "
            f"transitional={len(manifest.transitional_dependencies)}\n"
        )
    return 0


def _run_check(root: Path, as_json: bool) -> int:
    report = check_architecture(root)
    if as_json:
        _emit_json(report_to_dict(report))
        return 0 if report.status == "PASS" else 1
    allowed = report.edges_in_state("allowed")
    transitional = report.edges_in_state("transitional")
    forbidden = report.edges_in_state("forbidden")
    sys.stdout.write(f"status: {report.status}\n")
    sys.stdout.write(f"modules: {len(report.manifests)}\n")
    sys.stdout.write(
        f"production_paths: {len(report.ownership.production_paths)} "
        f"owned: {len(report.ownership.owner_by_path)} "
        f"unowned: {len(report.ownership.unowned)} "
        f"overlapping: {len(report.ownership.overlapping)}\n"
    )
    sys.stdout.write(
        f"dependency_edges: allowed={len(allowed)} transitional={len(transitional)} "
        f"forbidden={len(forbidden)}\n"
    )
    if transitional:
        sys.stdout.write("transitional (recorded architectural debt):\n")
        for edge in transitional:
            sys.stdout.write(f"  {edge.source} -> {edge.target} ({len(edge.evidence)} import(s))\n")
    if report.findings:
        sys.stdout.write("findings:\n")
        for finding in report.findings:
            sys.stdout.write(f"  {finding.render()}\n")
        return 1
    return 0


def _run_map(root: Path, as_json: bool) -> int:
    repo_map = build_repo_map(root)
    sys.stdout.write(repo_map_json(repo_map) if as_json else render_repo_map(repo_map))
    return 0


def _run_scope(root: Path, args: argparse.Namespace) -> int:
    result = resolve_scope(
        root,
        query=args.query,
        seeds=tuple(args.path),
        symbols=tuple(args.symbol),
    )
    if args.json:
        _emit_json(
            {
                "query": result.query,
                "seeds": list(result.seeds),
                "symbols": list(result.symbols),
                "primary_modules": list(result.primary_modules),
                "confidence": result.confidence,
                "ambiguous": result.ambiguous,
                "reasons": list(result.reasons),
                "source_paths": list(result.source_paths),
                "test_paths": list(result.test_paths),
                "ranked": [
                    {
                        "module": score.module,
                        "score": score.score,
                        "matched_tokens": list(score.matched_tokens),
                        "evidence": list(score.evidence),
                    }
                    for score in result.ranked
                ],
            }
        )
        return 0
    sys.stdout.write(f"modules: [{', '.join(result.primary_modules)}]\n")
    sys.stdout.write(f"confidence: {result.confidence}  ambiguous: {result.ambiguous}\n")
    sys.stdout.write("ranked:\n")
    for score in result.ranked:
        if score.score:
            sys.stdout.write(
                f"  {score.module:<14} {score.score:>3}  {', '.join(score.matched_tokens)}\n"
            )
    sys.stdout.write("reasons:\n")
    for reason in result.reasons:
        sys.stdout.write(f"  - {reason}\n")
    return 0


def _run_context(root: Path, args: argparse.Namespace) -> int:
    budget = context_mod.ContextBudget(
        max_source_files=args.max_sources,
        max_test_files=args.max_tests,
        dependency_depth=args.depth,
    )
    plan = context_mod.plan_context(
        root,
        query=args.query,
        seeds=tuple(args.path),
        symbols=tuple(args.symbol),
        budget=budget,
    )
    if args.json:
        _emit_json(context_mod.plan_to_dict(plan))
        return 0
    sys.stdout.write(context_mod.render_plan(plan))
    return 0


def _run_affected(root: Path, args: argparse.Namespace) -> int:
    plan = affected_mod.plan_affected(root, changed_paths=tuple(args.path), base=args.base)
    if args.json:
        _emit_json(affected_mod.plan_to_dict(plan))
        return 0
    sys.stdout.write(affected_mod.render_plan(plan))
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m saberops.devscope``."""
    args = build_parser().parse_args(argv)
    root = (args.root or repo_root()).resolve()
    try:
        if args.command == "modules":
            return _run_modules(root, args.json)
        if args.command == "check":
            return _run_check(root, args.json)
        if args.command == "map":
            return _run_map(root, args.json)
        if args.command == "scope":
            return _run_scope(root, args)
        if args.command == "context":
            return _run_context(root, args)
        return _run_affected(root, args)
    except ManifestError as exc:
        sys.stderr.write(f"status: FAIL\nmanifest_error: {exc}\n")
        return 1
