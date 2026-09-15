"""CLI entry point for inspecting and running C12-A real-project replays.

Keeps the surface small:
- Devtool inspection only, no dashboard.
- Commands: list-projects, list-corpus, inspect <task_id>.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from orchestrator_mvp.replay.corpus import get_corpus_task, get_initial_corpus
from orchestrator_mvp.replay.experiment import run_c12b_experiments
from orchestrator_mvp.replay.registry import RealProjectRegistry


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser for replay commands."""
    parser = argparse.ArgumentParser(
        prog="python -m orchestrator_mvp.replay",
        description="C12-A Real-project replay and measurement devtool.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # list-projects
    subparsers.add_parser("list-projects", help="List confirmed real projects in registry.")

    # list-corpus
    subparsers.add_parser("list-corpus", help="List frozen replay tasks in corpus.")

    # inspect
    inspect_parser = subparsers.add_parser("inspect", help="Inspect a specific replay task.")
    inspect_parser.add_argument("task_id", help="ID of the task to inspect.")

    # run-experiments
    run_parser = subparsers.add_parser(
        "run-experiments",
        help="Run the bounded C12-B context/cache campaign on real frozen corpus tasks.",
    )
    run_parser.add_argument(
        "--repo",
        default=".",
        help="Path to the authoritative repository (default: current directory).",
    )
    run_parser.add_argument(
        "--evidence-dir",
        required=True,
        help="Evidence destination; must be outside the repository and replay worktrees.",
    )
    run_parser.add_argument(
        "--tasks",
        default=None,
        help="Comma-separated corpus task IDs (default: representative subset).",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    """Execute replay CLI command."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "list-projects":
        registry = RealProjectRegistry()
        for project in registry.list_projects():
            print(
                f"- {project.project_key} ({project.remote_identity}) "
                f"[root: {project.root_commit[:8]}]"
            )
        return 0

    if args.command == "list-corpus":
        corpus = get_initial_corpus()
        for task in corpus:
            print(f"- {task.task_id}: {task.description} [start: {task.frozen_start_ref[:8]}]")
        return 0

    if args.command == "inspect":
        target_task = get_corpus_task(args.task_id)
        if target_task is None:
            print(f"Error: task '{args.task_id}' not found in corpus.", file=sys.stderr)
            return 1
        print(json.dumps(target_task.to_dict(), indent=2))
        return 0

    if args.command == "run-experiments":
        task_ids = (
            tuple(t.strip() for t in args.tasks.split(",") if t.strip())
            if args.tasks
            else None
        )
        result = run_c12b_experiments(
            Path(args.repo).resolve(),
            evidence_dir=Path(args.evidence_dir),
            task_ids=task_ids,
        )
        print(result.summary)
        if result.evidence_manifest:
            print("")
            print("Evidence manifest (path + SHA-256):")
            print(f"  {Path(args.evidence_dir).resolve()}")
            for name, digest in sorted(result.evidence_manifest["files"].items()):
                print(f"  {digest}  {name}")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
