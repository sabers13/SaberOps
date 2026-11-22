#!/usr/bin/env python3
"""SaberOps phase 1 benchmark (lane B2).

Fake mode runs the ten fixed tasks through the lifecycle rig's fake worker and
reviewer and prints the results table.  It never starts a vendor CLI.

Real mode drives real Codex, Claude and OpenCode Go CLIs.  It is run by hand
with the owner present (lane B3), refuses without ``--yes-real-cli``, and
refuses to reuse the owner's real SaberOps store.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from saberops.benchmark import (  # noqa: E402
    DEFAULT_CLAUDE_MODEL,
    DEFAULT_CODEX_MODEL,
    DEFAULT_OPENCODE_MODEL,
    DEFAULT_SUBMIT_CONCURRENCY,
    DEFAULT_SUBMIT_MODE,
    REAL_MODE_FLAG,
    REAL_SUBSCRIPTION_ORDER,
    BenchmarkTask,
    RealModeRefused,
    SubmitPacer,
    fake_mode_environment,
    parse_per_cli_selector,
    real_batches,
    real_model_for,
    select_benchmark_tasks,
    select_benchmark_tasks_by_cli,
    validate_real_mode,
)


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected an integer, got {text!r}") from exc
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def _submission_pacer(args: argparse.Namespace) -> SubmitPacer:
    """Validate the submission flags once, before any work starts."""
    try:
        return SubmitPacer(mode=args.submit, concurrency=args.concurrency)
    except ValueError as exc:
        raise RealModeRefused(str(exc)) from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the SaberOps phase 1 benchmark.",
    )
    parser.add_argument(
        "--mode",
        choices=("fake", "real"),
        default="fake",
        help="fake uses the lifecycle rig; real drives real CLIs (lane B3)",
    )
    parser.add_argument(
        "--store",
        type=Path,
        help="real mode only: an empty isolated store directory",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        help="real mode only: where the toy repositories are created",
    )
    parser.add_argument(
        "--codex-model",
        default=DEFAULT_CODEX_MODEL,
        help=f"real mode Codex model (default {DEFAULT_CODEX_MODEL})",
    )
    parser.add_argument(
        "--opencode-model",
        default=DEFAULT_OPENCODE_MODEL,
        help=f"real mode OpenCode Go model (default {DEFAULT_OPENCODE_MODEL})",
    )
    parser.add_argument(
        "--claude-model",
        default=DEFAULT_CLAUDE_MODEL,
        help=f"real mode Claude model (default {DEFAULT_CLAUDE_MODEL})",
    )
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--tasks",
        metavar="ID[,ID...]",
        help=(
            "real mode only: run these catalogue task ids in every batch "
            "(a documented reduced confirmation run); skipped rows are printed"
        ),
    )
    selection.add_argument(
        "--per-cli",
        type=str,
        metavar="N|CLI=N|CLI=TASK[,TASK...]",
        help=(
            "real mode only: run the first N catalogue tasks in every batch, "
            "set a count per subscription (e.g. opencode=10,codex=2,claude=2), "
            "or name task ids per subscription (e.g. "
            "codex=rename,review-block-a,opencode=10); skipped rows are printed "
            "per batch"
        ),
    )
    parser.add_argument(
        REAL_MODE_FLAG,
        dest="real_acknowledged",
        action="store_true",
        help="acknowledge that real mode starts real vendor CLIs and spends quota",
    )
    parser.add_argument(
        "--submit",
        choices=("paced", "burst"),
        default=DEFAULT_SUBMIT_MODE,
        help=(
            "paced (default) submits the next task only when a worker slot is "
            "free, so the targets measure an unqueued job; burst submits every "
            "task at once and prints a labelled stress section that is not "
            "scored"
        ),
    )
    parser.add_argument(
        "--concurrency",
        type=_positive_int,
        default=DEFAULT_SUBMIT_CONCURRENCY,
        metavar="N",
        help=(
            "paced submission only: worker slots to fill, i.e. the host's "
            f"concurrently dispatching workers (default {DEFAULT_SUBMIT_CONCURRENCY})"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the real-mode plan without starting anything",
    )
    return parser


def _run_fake(pacer: SubmitPacer) -> int:
    from _pytest.monkeypatch import MonkeyPatch

    tests_dir = REPO_ROOT / "tests"
    sys.path.insert(0, str(tests_dir))
    import benchmark_rig
    from lifecycle_rig import build_lifecycle_rig

    scratch = Path(tempfile.mkdtemp(prefix="saberops-benchmark-", dir="/var/tmp"))
    os.environ.update(fake_mode_environment())
    monkeypatch = MonkeyPatch()
    try:
        rig = build_lifecycle_rig(scratch, monkeypatch)
        report = benchmark_rig.run_fake_benchmark(
            rig,
            monkeypatch,
            submission_mode=pacer.mode,
            submit_concurrency=pacer.concurrency,
        )
    finally:
        monkeypatch.undo()
        shutil.rmtree(scratch, ignore_errors=True)
    print(report.table())
    return 0 if report.passed else 1


def _print_plan(
    store: Path,
    repo_root: Path,
    models: dict[str, str],
    pacer: SubmitPacer,
    tasks: (
        tuple[BenchmarkTask, ...]
        | dict[str, tuple[BenchmarkTask, ...]]
        | None
    ) = None,
) -> None:
    print("SaberOps phase 1 benchmark: real mode plan (nothing started)")
    print(f"  store:     {store}")
    print(f"  toy repos: {repo_root}")
    print(
        f"  submission: {pacer.mode}"
        + (
            f" (next task when a worker slot is free; concurrency "
            f"{pacer.concurrency})"
            if pacer.paced
            else " (every task at once; stress only, not scored)"
        )
    )
    print("  batches (different-vendor reviewer):")
    for batch in real_batches(tasks):
        worker = f"{batch.subscription}/{real_model_for(batch.subscription, models)}"
        reviewer = (
            f"{batch.reviewer_subscription}/"
            f"{real_model_for(batch.reviewer_subscription, models)}"
        )
        print(f"    worker {worker:<52} reviewer {reviewer}")
        print("      tasks: " + ", ".join(task.task_id for task in batch.tasks))
    print()
    print("Real mode discovers the three account CLIs in the isolated store, confirms")
    print("the packaged plan-only billing posture, records owner quota for every pool")
    print("the run dispatches through, and materializes the different-vendor reviewers")
    print("before it submits the first task.  It refuses if a default model id is not")
    print("discovered on this machine or a vendor CLI is missing; the refusal prints")
    print("the remedy.")


def _run_real(args: argparse.Namespace, pacer: SubmitPacer) -> int:
    if args.store is None:
        raise RealModeRefused("real mode needs --store pointing at an isolated store")
    store = validate_real_mode(
        acknowledged=bool(args.real_acknowledged), store=args.store
    )
    repo_root = args.repo_root or (store / "repos")
    models = {
        "codex": args.codex_model,
        "opencode": args.opencode_model,
        "claude": args.claude_model,
    }
    task_ids = (
        [part for part in str(args.tasks).split(",") if part.strip()]
        if args.tasks
        else None
    )
    per_cli = parse_per_cli_selector(args.per_cli) if args.per_cli else None
    selected: tuple[BenchmarkTask, ...] | dict[str, tuple[BenchmarkTask, ...]]
    if isinstance(per_cli, dict):
        selected_by_cli, skipped_by_cli = select_benchmark_tasks_by_cli(per_cli)
        for subscription in REAL_SUBSCRIPTION_ORDER:
            skipped = skipped_by_cli[subscription]
            print(
                f"benchmark: reduced catalogue [{subscription} batch]: running "
                f"{len(selected_by_cli[subscription])} task(s); skipping "
                f"{len(skipped)}: "
                + (", ".join(task.task_id for task in skipped) if skipped else "-")
            )
        selected = selected_by_cli
    else:
        selected_sequence, skipped = select_benchmark_tasks(
            task_ids, per_cli=per_cli if isinstance(per_cli, int) else None
        )
        if skipped:
            print(
                f"benchmark: reduced catalogue: running {len(selected_sequence)} "
                f"task(s) per CLI; skipping {len(skipped)}: "
                + ", ".join(task.task_id for task in skipped)
            )
        selected = selected_sequence
    if args.dry_run:
        _print_plan(store, repo_root, models, pacer, selected)
        return 0

    from saberops.benchmark import apply_isolated_environment, run_real_benchmark

    # Replace the ambient environment: an update alone would leave a stripped
    # metered key behind in this process (N6).
    apply_isolated_environment(store)
    report = run_real_benchmark(
        store=store,
        repo_root=repo_root,
        models=models,
        tasks=selected,
        submission_mode=pacer.mode,
        submit_concurrency=pacer.concurrency,
    )
    print(report.table())
    return 0 if report.passed else 1


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        pacer = _submission_pacer(args)
        if args.mode == "fake":
            return _run_fake(pacer)
        return _run_real(args, pacer)
    except RealModeRefused as exc:
        print(f"benchmark: refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
