#!/usr/bin/env python3
"""Build the coverage-based test map used by scripts/select_tests.py.

The static import graph over-selects on hub modules (orchestrator, web, db, ...):
almost every test transitively imports them, so any change there selects ~60/62
test files. This script records *executed-code* reachability instead: it runs the
full suite once under coverage.py with ``dynamic_context = test_function`` and
exports, per source file, which test node IDs executed each line.

The map is a directory at ``$(git rev-parse --git-common-dir)/saberops-test-map/``
(shared by all worktrees, never committed):

* ``meta.json``: version, build commit sha, timestamp, test count, coverage
  version, the node-ID table, the changed-test lookup (test file -> node IDs)
  and the blind-spot list.
* ``files/<repo-relative src path>.json``: one shard per measured src file,
  ``{line: [node indices into the meta table]}``. A line present with an empty
  list executed only outside any test (import/collection time). Shards keep
  selection cheap: only changed files' shards are loaded.

Selectors treat a map whose build commit is not an ancestor of HEAD as stale and
fall back to the static rule. Coverage data is read from the combined SQLite
data file (no multi-gigabyte JSON export); only the compact shards are written.

Subprocess capture: tests that spawn Python children inherit ``COVERAGE_PROCESS_START``
plus a ``sitecustomize`` found via a temporary ``PYTHONPATH`` entry, so child
interpreters report into the same parallel-mode data files. Both live outside the
repository: no repo file, venv file or global coverage setting is touched (the
rcfile is temporary; the repo's own coverage settings are left alone).

Usage:
    python scripts/build_test_map.py [--repo PATH] [--map DIR] [--no-subprocesses]
        [--no-slot]
    python scripts/build_test_map.py --from-data PATH/TO/.coverage --build-seconds S

The full suite runs once under coverage.py with pytest-xdist
(``-n <SABEROPS_MAP_WORKERS|SABEROPS_GATE_WORKERS|4> --dist worksteal``);
``parallel = true`` plus ``coverage combine`` merge the per-worker shard
contexts.  The run holds the exclusive heavy-gate slot (via
``scripts/gate_tools.py exclusive``) so it never competes with another gate;
``--no-slot`` skips that for recovery/seed runs only.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path, PurePosixPath

MAP_DIRNAME = "saberops-test-map"
MAP_META = "meta.json"
MAP_FILES = "files"
MAP_VERSION = 2

# D-0061: a conservative default cap keeps coverage-map peak RAM in the
# owner-declared ~1.8-2.4 GiB band instead of the uncapped serial/8-worker run.
DEFAULT_MAP_WORKERS = 4
SABEROPS_MAP_WORKERS_ENV = "SABEROPS_MAP_WORKERS"
SABEROPS_GATE_WORKERS_ENV = "SABEROPS_GATE_WORKERS"


def positive_int_env(name: str, default: int) -> int:
    """Return a positive int from ``name``; malformed values keep the cap."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        print(f"warning: {name}={raw!r} is not an integer; using {default}", flush=True)
        return default
    if value < 1:
        print(f"warning: {name}={raw!r} must be >= 1; using {default}", flush=True)
        return default
    return value


def map_workers() -> int:
    """Worker count for the coverage suite: map knob, then gate knob, then cap."""
    if os.environ.get(SABEROPS_MAP_WORKERS_ENV, "").strip():
        return positive_int_env(SABEROPS_MAP_WORKERS_ENV, DEFAULT_MAP_WORKERS)
    return positive_int_env(SABEROPS_GATE_WORKERS_ENV, DEFAULT_MAP_WORKERS)


def suite_command(python: str, rcfile: Path, plugin_module: str, workers: int) -> list[str]:
    """The parallel coverage suite argv (xdist worksteal, shared contexts).

    ``parallel = true`` plus ``coverage combine`` merge the per-worker shard
    contexts, so a single xdist run replaces the old single-process serial run.
    """
    return [
        python,
        "-m",
        "coverage",
        "run",
        f"--rcfile={rcfile}",
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "-p",
        plugin_module,
        "-n",
        str(workers),
        "--dist",
        "worksteal",
    ]


def exclusive_command(python: str, command: list[str]) -> list[str]:
    """Wrap ``command`` in the canonical exclusive heavy-gate slot.

    Reuses ``scripts/gate_tools.py exclusive`` so the map build takes exactly
    the full gate's heavy slot (and drains all light slots) instead of running
    the suite while another gate is live (D-0061 item 3).
    """
    tools = Path(__file__).resolve().parent / "gate_tools.py"
    return [python, str(tools), "exclusive", "--", *command]


# Imported automatically by every Python child that inherits our PYTHONPATH entry.
# process_startup() is a no-op unless COVERAGE_PROCESS_START points at an rcfile,
# so this only ever activates for children of the instrumented suite run.
SITECUSTOMIZE = (
    "try:\n"
    "    import coverage\n"
    "    coverage.process_startup()\n"
    "except Exception:\n"
    "    pass\n"
)

# Pytest plugin (written into a temp PYTHONPATH dir, loaded with `-p`) that switches
# the coverage context to the upcoming test during setup. coverage.py's own
# test_function switcher fires only when a `test*` frame is entered, i.e. in the
# call phase: lines executed by fixtures (setup) would otherwise be attributed to
# whichever test ran before on that worker (recall defect: a fixture-only covering
# test is never selected). The label format matches coverage's own dotted
# module.qualname labels so setup and call attributions merge. Teardown needs no
# hook: the context still names the test that just ran.
SETUP_CONTEXT_PLUGIN = (
    "def pytest_runtest_setup(item):\n"
    "    try:\n"
    "        from coverage import Coverage\n"
    "    except ImportError:\n"
    "        return\n"
    "    cov = Coverage.current()\n"
    "    if cov is None:\n"
    "        return\n"
    "    try:\n"
    "        module = getattr(getattr(item, 'module', None), '__name__', None)\n"
    "        obj = getattr(item, 'obj', None)\n"
    "        qualname = getattr(obj, '__qualname__', None)\n"
    "        if not qualname:\n"
    "            qualname = str(getattr(item, 'name', 'unknown')).split('[')[0]\n"
    "        if not module:\n"
    "            return\n"
    "        label = f\"{module}.{qualname}\"\n"
    "    except Exception:\n"
    "        return\n"
    "    try:\n"
    "        cov.switch_context(label)\n"
    "    except Exception:\n"
    "        pass\n"
)

PLUGIN_MODULE = "saberops_testmap_context"

RC_TEMPLATE = (
    "[run]\n"
    "dynamic_context = test_function\n"
    "parallel = true\n"
    "data_file = {data_file}\n"
    "source = {source}\n"
)


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {completed.stderr.strip()}")
    return completed.stdout.strip()


def default_map_dir(repo: Path) -> Path:
    common = _git(repo, "rev-parse", "--git-common-dir")
    common_dir = (repo / common).resolve() if not Path(common).is_absolute() else Path(common)
    return common_dir / MAP_DIRNAME


def collect_node_ids(python: str, repo: Path) -> list[str]:
    """All test node IDs in collection order (no coverage, no cache)."""
    completed = subprocess.run(
        [python, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    node_ids = [
        line.strip()
        for line in completed.stdout.splitlines()
        if "::" in line and not line.startswith(" ")
    ]
    # Fall back to looser matching: collect-only prints one node ID per line and a
    # trailing summary ("N tests collected ...") which never contains "::".
    if not node_ids:
        node_ids = [line.strip() for line in completed.stdout.splitlines() if "::" in line]
    if completed.returncode != 0 and not node_ids:
        raise RuntimeError(f"pytest --collect-only failed:\n{completed.stdout}\n{completed.stderr}")
    return node_ids


def context_labels_for(node_id: str, observed: set[str]) -> list[str]:
    """Map a node ID to the coverage dynamic-context label(s) it produces.

    With ``dynamic_context = test_function`` coverage names a context after the
    dotted module plus qualname (``test_m.test_add``, ``test_x.TestC.test_m``),
    without parameters, while node IDs carry the file path plus ``::`` parts with
    parameters (``tests/test_m.py::test_add``). Observed labels are ground truth
    for how each test module was imported, so a node matches a label on equality
    or on a package-prefix suffix match; parametrized siblings share one label.
    """
    file_part, sep, rest = node_id.partition("::")
    stem = PurePosixPath(file_part).stem
    quals = [part.split("[")[0] for part in rest.split("::")] if sep else []
    dotted = ".".join([stem, *quals])
    return sorted(
        label for label in observed if label == dotted or label.endswith("." + dotted)
    )


def build_shards(
    repo: Path,
    data_path: Path,
    node_ids: list[str],
    commit: str,
    duration_s: float,
    pytest_exit: int,
    with_subprocesses: bool,
    out_dir: Path,
) -> dict:
    """Read the combined coverage SQLite data and write meta.json + file shards."""
    from coverage.numbits import numbits_to_nums

    repo = repo.resolve()
    src_root = repo / "src"
    con = sqlite3.connect(f"file:{data_path.as_posix()}?mode=ro", uri=True)
    observed = {
        row[0] for row in con.execute("select context from context") if row[0]
    }

    node_for_label: dict[str, set[str]] = {}
    unmatched_nodes = 0
    for node_id in node_ids:
        labels = context_labels_for(node_id, observed)
        if not labels:
            unmatched_nodes += 1
        for label in labels:
            node_for_label.setdefault(label, set()).add(node_id)
    nodes = sorted({node for group in node_for_label.values() for node in group})
    index = {node: position for position, node in enumerate(nodes)}
    label_indices = {
        label: sorted(index[node] for node in group)
        for label, group in node_for_label.items()
    }
    label_for_id = {
        row[0]: row[1] for row in con.execute("select id, context from context")
    }

    files_dir = out_dir / MAP_FILES
    files_dir.mkdir(parents=True, exist_ok=True)
    # Stale shards from an older layout must not survive: clear the shard tree.
    for stale in sorted(files_dir.rglob("*.json")):
        stale.unlink()

    shard_count = 0
    covered_any: set[str] = set()
    file_rows = con.execute("select id, path from file").fetchall()
    for file_id, abs_path in sorted(file_rows, key=lambda row: row[1]):
        try:
            rel = Path(abs_path).resolve().relative_to(repo).as_posix()
        except ValueError:
            continue
        if not rel.startswith("src/"):
            continue
        try:
            Path(repo / rel).resolve().relative_to(src_root.resolve())
        except ValueError:  # pragma: no cover - defensive; rel already under src/
            continue
        line_sets: dict[int, set[int]] = {}
        rows = con.execute(
            "select context_id, numbits from line_bits where file_id = ?", (file_id,)
        )
        for context_id, numbits in rows:
            for line in numbits_to_nums(numbits):
                line_sets.setdefault(line, set()).update(
                    label_indices.get(label_for_id.get(context_id, ""), ())
                )
        shard = {str(line): sorted(line_sets[line]) for line in sorted(line_sets)}
        shard_path = files_dir / f"{rel}.json"
        shard_path.parent.mkdir(parents=True, exist_ok=True)
        shard_path.write_text(json.dumps(shard, separators=(",", ":")), encoding="utf-8")
        shard_count += 1
        if any(line_sets.values()):
            covered_any.add(rel)
    con.close()

    # Blind spots: src modules with zero covering test contexts. Files that never
    # imported (no coverage row at all) count too: nothing executed them.
    blind = sorted(
        path.relative_to(repo).as_posix()
        for path in sorted(src_root.rglob("*.py"))
        if path.relative_to(repo).as_posix() not in covered_any
    )

    by_test: dict[str, list[int]] = {}
    for position, node_id in enumerate(nodes):
        by_test.setdefault(node_id.split("::")[0], []).append(position)

    try:
        coverage_version = package_version("coverage")
    except PackageNotFoundError:
        coverage_version = "unknown"
    meta = {
        "version": MAP_VERSION,
        "commit": commit,
        "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "test_count": len(node_ids),
        "context_count": len(node_for_label),
        "unmatched_nodes": unmatched_nodes,
        "coverage_version": coverage_version,
        "build_seconds": round(duration_s, 1),
        "pytest_exit": pytest_exit,
        "subprocess_capture": with_subprocesses,
        "nodes": nodes,
        "by_test": by_test,
        "blind": blind,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    staged = out_dir / f"{MAP_META}.tmp"
    staged.write_text(json.dumps(meta, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(staged, out_dir / MAP_META)
    meta["shard_count"] = shard_count
    return meta


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the coverage test map.")
    parser.add_argument("--repo", default=".", help="repository root")
    parser.add_argument("--map", default=None, help="map directory")
    parser.add_argument(
        "--no-subprocesses",
        action="store_true",
        help="do not set COVERAGE_PROCESS_START/sitecustomize for test children",
    )
    parser.add_argument(
        "--no-slot",
        action="store_true",
        help="skip the exclusive heavy-gate slot (recovery/seeding only)",
    )
    parser.add_argument(
        "--from-data",
        default=None,
        help=("reuse an existing combined coverage data file instead of running "
              "the suite (recovery/debugging; records given timings)"),
    )
    parser.add_argument(
        "--pytest-exit",
        type=int,
        default=0,
        help="pytest exit code to record when --from-data is used",
    )
    parser.add_argument(
        "--build-seconds",
        type=float,
        default=None,
        help="suite runtime to record when --from-data is used",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo = Path(args.repo).resolve()
    python = sys.executable
    commit = _git(repo, "rev-parse", "HEAD")
    map_dir = Path(args.map) if args.map else default_map_dir(repo)
    with_subprocesses = not args.no_subprocesses

    if args.from_data:
        if args.build_seconds is None:
            print("--from-data needs --build-seconds (the reused suite runtime)")
            return 2
        print(f"repository: {repo}")
        print(f"commit: {commit}")
        print(f"map: {map_dir}")
        print("--- pytest --collect-only ---")
        node_ids = collect_node_ids(python, repo)
        print(f"collected {len(node_ids)} test node IDs")
        data_path = Path(args.from_data)
        duration_s = args.build_seconds
        pytest_exit = args.pytest_exit
        print(f"reused {data_path} (suite {duration_s:.1f}s, pytest exit {pytest_exit})")
    else:
        tmpdir = Path(tempfile.mkdtemp(prefix="saberops-test-map-"))
        rcfile = tmpdir / "coveragerc"
        rcfile.write_text(
            RC_TEMPLATE.format(
                data_file=(tmpdir / ".coverage").as_posix(),
                source=(repo / "src").as_posix(),
            ),
            encoding="utf-8",
        )
        env = dict(os.environ)
        plugindir = tmpdir / "plugins"
        plugindir.mkdir()
        (plugindir / f"{PLUGIN_MODULE}.py").write_text(SETUP_CONTEXT_PLUGIN, encoding="utf-8")
        env["PYTHONPATH"] = str(plugindir) + os.pathsep + env.get("PYTHONPATH", "")
        if with_subprocesses:
            sitedir = tmpdir / "site"
            sitedir.mkdir()
            (sitedir / "sitecustomize.py").write_text(SITECUSTOMIZE, encoding="utf-8")
            env["COVERAGE_PROCESS_START"] = str(rcfile)
            env["PYTHONPATH"] = str(sitedir) + os.pathsep + env["PYTHONPATH"]

        print(f"repository: {repo}")
        print(f"commit: {commit}")
        print(f"map: {map_dir}")
        print(f"subprocess capture: {'on' if with_subprocesses else 'off'}")
        print(f"setup-context plugin: {PLUGIN_MODULE} (attributes fixture lines)")

        print("--- pytest --collect-only ---")
        node_ids = collect_node_ids(python, repo)
        print(f"collected {len(node_ids)} test node IDs")

        workers = map_workers()
        print(
            f"--- full suite under coverage (parallel, -n {workers} --dist worksteal; "
            "xdist shards merged by coverage combine) ---"
        )
        started = time.monotonic()
        suite_cmd = suite_command(python, rcfile, PLUGIN_MODULE, workers)
        if args.no_slot:
            print("exclusive heavy-gate slot: disabled by --no-slot")
        else:
            suite_cmd = exclusive_command(sys.executable, suite_cmd)
        suite = subprocess.run(
            suite_cmd,
            cwd=repo,
            check=False,
            env=env,
        )
        duration_s = time.monotonic() - started
        print(f"pytest exit: {suite.returncode} in {duration_s:.1f}s")
        if suite.returncode != 0:
            print("warning: suite failed; the map still records what executed", flush=True)
        pytest_exit = suite.returncode

        subprocess.run(
            [python, "-m", "coverage", "combine", f"--rcfile={rcfile}"],
            cwd=repo,
            check=False,
            capture_output=True,
        )
        data_path = tmpdir / ".coverage"
        if not data_path.exists():
            print("coverage combine produced no data file")
            return 2

    print("--- building map ---")
    meta = build_shards(
        repo, data_path, node_ids, commit, duration_s, pytest_exit,
        with_subprocesses, map_dir,
    )

    size = sum(path.stat().st_size for path in map_dir.rglob("*") if path.is_file())
    print(f"wrote {map_dir} ({size} bytes in {meta['shard_count'] + 1} files)")
    print(f"build time: {duration_s:.1f}s, tests: {meta['test_count']}, "
          f"contexts: {meta['context_count']}, "
          f"unmatched nodes: {meta['unmatched_nodes']}, "
          f"src files in map: {meta['shard_count']}")
    print(f"blind spots ({len(meta['blind'])} src modules with zero covering contexts):")
    for rel in meta["blind"]:
        print(f"  {rel}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
