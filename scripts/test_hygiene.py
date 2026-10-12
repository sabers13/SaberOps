#!/usr/bin/env python3
"""Read-only test hygiene report: retirement candidates, never deletions.

After each G3 PASS and map rebuild the Integrator runs this tool (read-only:
coverage map + ``result/full.xml`` + git; it executes no tests and imports no
``src`` module) to list *candidates* for the orchestrator's retirement table.
See ``docs/gate.md`` ("Test hygiene report") and ``.orch/WORKFLOW.md`` §5.

Usage:
    scripts/test_hygiene.py [--repo PATH] [--map DIR] [--junit PATH]
        [--base REF] [--out PATH] [--json PATH]

Exit 0 when a report was produced (candidates are not errors); exit 2 for
unusable input (a path that was given but is unreadable).
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath
from typing import Any

sys.dont_write_bytecode = True

# File stems matching this are task-named (e.g. test_r3e12_..., test_c16b2_...,
# test_h3_..., test_i004_glue.py); test_quota_exclusions.py and
# test_capacity_ledger.py do not match.
TASK_NAMED_RE = re.compile(r"^test_(?:[a-z]{1,3}\d[a-z0-9]*|i\d{3})_")

FOOTER = (
    "Candidates only. Retirement happens through a prompt's "
    '"Tests retired / rewritten" table (WORKFLOW §9 rule 12).'
)

SRC_PACKAGE = PurePosixPath("src/saberops")
SRC_SCAN_ROOT = PurePosixPath("src")
TEST_SCAN_ROOT = PurePosixPath("tests")
SHARD_DIR = "files"
META_FILE = "meta.json"
SLOWEST_COUNT = 20


def _load_sibling(name: str) -> Any:
    """Import scripts/<name>.py by path (read-only reuse, no src imports)."""
    path = Path(__file__).resolve().parent / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"test_hygiene_{name}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load sibling script {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {completed.stderr.strip()}")
    return completed.stdout.strip()


def _parse_file(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, ValueError):
        return None


def _parse_text(text: str, filename: str) -> ast.Module | None:
    try:
        return ast.parse(text, filename=filename)
    except (SyntaxError, ValueError):
        return None


def _refs_in_tree(tree: ast.Module) -> dict[str, list[int]]:
    """Name references: Name ids, Attribute attrs, ImportFrom alias names.

    String entries (including ``__all__``) never count as a use.
    """
    refs: dict[str, list[int]] = {}

    def add(name: str, lineno: int) -> None:
        refs.setdefault(name, []).append(lineno)

    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            add(node.id, node.lineno)
        elif isinstance(node, ast.Attribute):
            add(node.attr, node.lineno)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name != "*":
                    add(alias.name, node.lineno)
    return refs


def _normalize_node_id(node_id: str) -> str:
    """Common key for map (pytest) and junit-derived node ids.

    Both sides already use `path::qualname[param]` form (gate_tools.junit
    mirrors it, including class segments); this canonicalises only path
    separators and `./` prefixes so parametrized ids and class segments
    compare literally on both sides.
    """
    file_part, sep, rest = node_id.partition("::")
    file_part = file_part.replace("\\", "/")
    while "//" in file_part:
        file_part = file_part.replace("//", "/")
    if file_part.startswith("./"):
        file_part = file_part[2:]
    return file_part + ("::" + rest if sep else "")


def _read_meta(map_dir: Path) -> dict[str, Any]:
    """Raw meta.json payload (for test_count/unmatched_nodes); {} when unusable."""
    try:
        payload = json.loads((map_dir / META_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


def _def_start(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> int:
    starts = [node.lineno]
    starts.extend(getattr(dec, "lineno", node.lineno) for dec in node.decorator_list)
    return min(starts)


def _all_exports(tree: ast.Module) -> set[str]:
    """String entries assigned to ``__all__`` in one module."""
    exported: set[str] = set()
    for node in tree.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
            value = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
            value = node.value
        if not any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets):
            continue
        if isinstance(value, (ast.List, ast.Tuple, ast.Set)):
            for element in value.elts:
                if isinstance(element, ast.Constant) and isinstance(element.value, str):
                    exported.add(element.value)
    return exported


def section_a(repo: Path) -> list[dict[str, Any]]:
    """Top-level src defs/classes referenced only from tests.

    Returns rows of {file, line, symbol, exported, referenced_by}.
    """
    package = repo / SRC_PACKAGE.as_posix()
    src_files = sorted((repo / SRC_SCAN_ROOT.as_posix()).rglob("*.py"))
    test_files = sorted((repo / TEST_SCAN_ROOT.as_posix()).rglob("*.py"))
    src_refs: dict[str, dict[str, list[int]]] = {}
    src_trees: dict[str, ast.Module] = {}
    for path in src_files:
        tree = _parse_file(path)
        if tree is None:
            continue
        rel = path.relative_to(repo).as_posix()
        src_refs[rel] = _refs_in_tree(tree)
        src_trees[rel] = tree
    test_refs: dict[str, set[str]] = {}
    for path in test_files:
        tree = _parse_file(path)
        if tree is None:
            continue
        rel = path.relative_to(repo).as_posix()
        test_refs[rel] = set(_refs_in_tree(tree))
    rows: list[dict[str, Any]] = []
    for path in sorted(package.rglob("*.py")):
        rel = path.relative_to(repo).as_posix()
        tree = src_trees.get(rel)
        if tree is None:
            continue
        exported = _all_exports(tree)
        for node in tree.body:
            if not isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                continue
            name = node.name
            if name == "main" or _dunder(name):
                continue
            start = _def_start(node)
            end = node.end_lineno or node.lineno
            used_in_src = False
            for other_rel, refs in src_refs.items():
                for lineno in refs.get(name, []):
                    if other_rel == rel and start <= lineno <= end:
                        continue  # the symbol's own definition
                    used_in_src = True
                    break
                if used_in_src:
                    break
            if used_in_src:
                continue
            referenced_by = sorted(
                test_rel
                for test_rel, names in test_refs.items()
                if name in names
            )
            if not referenced_by:
                continue
            rows.append(
                {
                    "file": rel,
                    "line": node.lineno,
                    "symbol": name,
                    "exported": name in exported,
                    "referenced_by": referenced_by,
                }
            )
    return rows


def _script_test_marks(repo: Path, files: list[str]) -> dict[str, bool]:
    """True for test files that load scripts/ modules (no src coverage exists)."""
    marks: dict[str, bool] = {}
    for rel in files:
        try:
            text = (repo / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            marks[rel] = False
            continue
        tree = _parse_text(text, rel)
        hit = "scripts/" in text or "scripts\\" in text
        if not hit and tree is not None:
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    if any(
                        a.name == "scripts" or a.name.startswith("scripts.")
                        for a in node.names
                    ):
                        hit = True
                        break
                elif isinstance(node, ast.ImportFrom):
                    module_name = node.module or ""
                    if module_name == "scripts" or module_name.startswith("scripts."):
                        hit = True
                        break
        marks[rel] = hit
    return marks


def read_shards(map_dir: Path) -> dict[str, dict[int, list[int]]]:
    """Shard rel path -> {line: [node indices]} for every files/*.json shard."""
    shards: dict[str, dict[int, list[int]]] = {}
    shard_root = map_dir / SHARD_DIR
    if not shard_root.is_dir():
        return shards
    for shard_path in sorted(shard_root.rglob("*.json")):
        try:
            payload = json.loads(shard_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        rel = shard_path.relative_to(shard_root).as_posix()
        if rel.endswith(".json"):
            rel = rel[: -len(".json")]
        lines: dict[int, list[int]] = {}
        for line_text, indices in payload.items():
            try:
                line = int(line_text)
                lines[line] = [int(i) for i in indices]
            except (ValueError, TypeError):
                continue
        shards[rel] = lines
    return shards


def _id_mismatch(
    nodes: list[str], junit_ids: list[str]
) -> tuple[int, int]:
    """(map-only, junit-only) counts under the common normalisation."""
    map_norm = {_normalize_node_id(node) for node in nodes}
    junit_norm = {_normalize_node_id(node_id) for node_id in junit_ids}
    return len(map_norm - junit_norm), len(junit_norm - map_norm)


def section_b(
    nodes: list[str],
    junit_ids: list[str],
    shards: dict[str, dict[int, list[int]]],
) -> tuple[dict[str, dict[str, Any]], int, int]:
    """Junit node ids covering no src line, grouped by file, plus mismatches.

    The map node table holds only tests that executed src code, so the
    population is the complete junit set: an id is uncovered when its
    normalised form covers no line in any shard (absent from the table, or
    present with no lines). Map-only/junit-only counts expose naming drift
    between the two id sources.
    """
    covered: dict[str, set[tuple[str, int]]] = {}
    for rel, lines in shards.items():
        for line, indices in lines.items():
            for index in indices:
                if 0 <= index < len(nodes):
                    key = _normalize_node_id(nodes[index])
                    covered.setdefault(key, set()).add((rel, line))
    map_only, junit_only = _id_mismatch(nodes, junit_ids)
    grouped: dict[str, list[str]] = {}
    for node_id in junit_ids:
        if not covered.get(_normalize_node_id(node_id)):
            grouped.setdefault(node_id.split("::")[0], []).append(node_id)
    files = {
        rel: {"nodes": sorted(ids), "script_test": False}
        for rel, ids in sorted(grouped.items())
    }
    return files, map_only, junit_only


def section_c(
    nodes: list[str], shards: dict[str, dict[int, list[int]]]
) -> tuple[list[dict[str, Any]], int]:
    """Identical-coverage groups spanning >= 2 files; same-file count."""
    coverage: dict[int, set[tuple[str, int]]] = {}
    for rel, lines in shards.items():
        for line, indices in lines.items():
            for index in indices:
                coverage.setdefault(index, set()).add((rel, line))
    groups: dict[frozenset[tuple[str, int]], list[int]] = {}
    for position in range(len(nodes)):
        cov = coverage.get(position)
        if cov is None or not cov:
            continue  # empty-coverage nodes never form a group
        groups.setdefault(frozenset(cov), []).append(position)
    listed: list[dict[str, Any]] = []
    same_file = 0
    for covered_set, members in groups.items():
        if len(members) < 2:
            continue
        files = {nodes[i].split("::")[0] for i in members}
        if len(files) < 2:
            same_file += 1
            continue
        listed.append(
            {"nodes": sorted(nodes[i] for i in members), "lines": len(covered_set)}
        )
    listed.sort(key=lambda group: group["nodes"][0])
    return listed, same_file


def task_named_files(repo: Path) -> list[str]:
    """Repo-relative test files whose stem matches TASK_NAMED_RE."""
    found: list[str] = []
    tests_root = repo / TEST_SCAN_ROOT.as_posix()
    if not tests_root.is_dir():
        return found
    for path in sorted(tests_root.rglob("test_*.py")):
        if TASK_NAMED_RE.match(path.stem):
            found.append(path.relative_to(repo).as_posix())
    return found


def _junit_times(junit_path: Path) -> tuple[list[tuple[str, float]], float]:
    """(node, time) per testcase in document order plus the summed time.

    The node-id derivation mirrors scripts/gate_tools.py::junit (which is
    reused for the authoritative node set); this walk only attaches the
    ``time`` attribute that Evidence does not carry.
    """
    tree = ET.parse(junit_path)
    rows: list[tuple[str, float]] = []
    total = 0.0
    for case in tree.iter("testcase"):
        if not case.get("classname") and (case.get("name") or "").endswith(".py"):
            node = case.get("name", "")
        else:
            node = ""
        filename = (case.get("file") or (case.get("classname", "").replace(".", "/") + ".py"))
        node = node or filename + "::" + (case.get("name") or "")
        if case.get("classname") and (
            ".py" not in filename or not Path(filename).name.startswith("test_")
        ):
            parts = (case.get("classname") or "").split(".")
            module = next(
                (i for i, p in enumerate(parts) if p.startswith("test_")),
                len(parts) - 1,
            )
            node = "/".join(parts[: module + 1]) + ".py::"
            node += "::".join([*parts[module + 1 :], case.get("name") or ""])
        try:
            elapsed = float(case.get("time") or 0.0)
        except ValueError:
            elapsed = 0.0
        total += elapsed
        rows.append((node, elapsed))
    return rows, total


def section_e(
    gate_tools: Any, junit_path: Path
) -> tuple[list[dict[str, Any]], float, int, list[str]]:
    """Slowest 20 tests by junit time; total time; case count; deduped ids."""
    evidence = gate_tools.junit(junit_path)
    canonical: list[str] = list(evidence.cases)
    rows, total = _junit_times(junit_path)
    if {node for node, _ in rows} != set(canonical):
        # The mirror walk must agree with the authoritative naming; a mismatch
        # means the junit layout changed, so fall back to canonical ids.
        timed = dict.fromkeys(canonical, 0.0)
    else:
        timed = {}
        for node, elapsed in rows:
            timed[node] = max(timed.get(node, 0.0), elapsed)
    slowest = sorted(timed.items(), key=lambda item: (-item[1], item[0]))
    return (
        [{"node": node, "time": elapsed} for node, elapsed in slowest[:SLOWEST_COUNT]],
        total,
        len(canonical),
        sorted(set(canonical)),
    )


def _collect_test_functions(
    tree: ast.Module,
) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """Qualname -> test function (any def whose name starts with test)."""
    found: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qual = f"{prefix}{child.name}" if prefix else child.name
                if child.name.startswith("test"):
                    found[qual] = child
                visit(child, f"{qual}.")
            elif isinstance(child, ast.ClassDef):
                qual = f"{prefix}{child.name}" if prefix else child.name
                visit(child, f"{qual}.")
            else:
                visit(child, prefix)

    visit(tree, "")
    return found


def _has_covers(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> bool:
    doc = ast.get_docstring(node) or ""
    return any(line.strip().startswith("Covers:") for line in doc.splitlines())


def section_f(repo: Path, base: str) -> list[str]:
    """New test functions at HEAD (vs --base) without a Covers: docstring line."""
    missing: list[str] = []
    tests_root = repo / TEST_SCAN_ROOT.as_posix()
    if not tests_root.is_dir():
        return missing
    for path in sorted(tests_root.rglob("*.py")):
        rel = path.relative_to(repo).as_posix()
        head_tree = _parse_file(path)
        if head_tree is None:
            continue
        head_tests = _collect_test_functions(head_tree)
        try:
            base_text = _git(repo, "show", f"{base}:{rel}")
        except RuntimeError:
            base_text = None
        if base_text is None:
            base_tests: dict[str, Any] = {}
        else:
            base_tree = _parse_text(base_text, rel)
            base_tests = (
                _collect_test_functions(base_tree) if base_tree is not None else {}
            )
        for qual, func in sorted(head_tests.items()):
            if qual in base_tests:
                continue
            if not _has_covers(func):
                missing.append(f"{rel}::{qual}")
    return missing


def _count_test_files(repo: Path) -> int:
    tests_root = repo / TEST_SCAN_ROOT.as_posix()
    if not tests_root.is_dir():
        return 0
    return len(list(tests_root.rglob("test_*.py")))


def _ast_test_count(repo: Path) -> int:
    """Fallback node count when neither map nor junit is available."""
    total = 0
    tests_root = repo / TEST_SCAN_ROOT.as_posix()
    if not tests_root.is_dir():
        return 0
    for path in tests_root.rglob("test_*.py"):
        tree = _parse_file(path)
        if tree is not None:
            total += len(_collect_test_functions(tree))
    return total


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only test hygiene report.")
    parser.add_argument("--repo", default=".", help="repository root")
    parser.add_argument("--map", default=None, help="coverage map directory")
    parser.add_argument("--junit", default=None, help="junit xml from a full gate")
    parser.add_argument("--base", default=None, help="base ref for new-test scan")
    parser.add_argument("--out", default=None, help="markdown report path")
    parser.add_argument("--json", default=None, help="json report path")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo = Path(args.repo)
    if not repo.is_dir():
        print(f"error: --repo {args.repo} is not a readable directory", file=sys.stderr)
        return 2
    repo = repo.resolve()
    try:
        head = _git(repo, "rev-parse", "HEAD")
    except RuntimeError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    gate_tools = _load_sibling("gate_tools")
    select_tests = _load_sibling("select_tests")

    if args.map:
        map_dir = Path(args.map)
        if not map_dir.is_dir():
            print(f"error: --map {args.map} is not a readable directory", file=sys.stderr)
            return 2
    else:
        try:
            map_dir = select_tests.default_map_path(repo)
        except Exception:
            map_dir = repo / ".no-such-map"

    covmap: Any = None
    try:
        covmap = select_tests.CoverageMap.load(map_dir)
    except (OSError, ValueError):
        covmap = None
    map_commit = str(covmap.commit) if covmap is not None else ""
    fresh = bool(map_commit) and map_commit == head
    nodes: list[str] = list(covmap.nodes) if covmap is not None else []
    node_source = "map" if covmap is not None else ""

    junit_path = Path(args.junit) if args.junit else None
    if junit_path is not None and not junit_path.is_file():
        print(f"error: --junit {args.junit} is not readable", file=sys.stderr)
        return 2
    slowest: list[dict[str, Any]] | None = None
    junit_total = 0.0
    junit_cases = 0
    junit_ids: list[str] | None = None
    if junit_path is not None:
        try:
            slowest, junit_total, junit_cases, junit_ids = section_e(gate_tools, junit_path)
        except ET.ParseError as error:
            print(f"error: cannot parse --junit {args.junit}: {error}", file=sys.stderr)
            return 2
        except OSError as error:
            print(f"error: cannot read --junit {args.junit}: {error}", file=sys.stderr)
            return 2

    if not nodes and junit_path is not None:
        evidence = gate_tools.junit(junit_path)
        nodes = list(evidence.cases)
        node_source = "junit"
    if nodes or junit_path is not None or covmap is not None:
        node_count = len(nodes)
    else:
        node_count = _ast_test_count(repo)
    if not nodes and node_source == "":
        node_source = "ast"

    if args.base:
        try:
            _git(repo, "rev-parse", "--verify", "--quiet", args.base)
        except RuntimeError as error:
            print(f"error: --base {args.base} is unusable: {error}", file=sys.stderr)
            return 2

    test_files = _count_test_files(repo)
    task_named = task_named_files(repo)

    # Coverage-based sections need a fresh map. B's population is the
    # complete junit set (the map table holds only tests that executed src
    # code); without --junit there is nothing to list.
    meta = _read_meta(map_dir) if covmap is not None else {}
    meta_tests = meta.get("test_count", "?")
    meta_unmatched = meta.get("unmatched_nodes", "?")
    shards: dict[str, dict[int, list[int]]] = {}
    if fresh:
        shards = read_shards(map_dir)
    rows_b: dict[str, Any] | None = None
    map_only = 0
    junit_only = 0
    mismatch_known = False
    if junit_ids is not None and covmap is not None:
        map_only, junit_only = _id_mismatch(nodes, junit_ids)
        mismatch_known = True
    groups_c: list[dict[str, Any]] | None = None
    same_file_c = 0
    if fresh:
        if junit_ids is not None:
            files_b, map_only, junit_only = section_b(nodes, junit_ids, shards)
            mismatch_known = True
            if files_b:
                marks = _script_test_marks(repo, sorted(files_b))
                for rel, is_script in marks.items():
                    files_b[rel]["script_test"] = is_script
            rows_b = files_b
        else:
            rows_b = {
                "needs_junit": (
                    "B needs --junit: the map lists only tests that executed src code "
                    f"({meta_unmatched} tests not in the map)."
                )
            }
        groups_c, same_file_c = section_c(nodes, shards)

    rows_a = section_a(repo)

    by_test: dict[str, list[str]] = {}
    if covmap is not None:
        try:
            by_test = dict(covmap.by_test)
        except (AttributeError, TypeError):
            by_test = {}
    rows_d: list[dict[str, Any]] = []
    for rel in task_named:
        count = len(by_test.get(rel, []))
        if not count:
            tree = _parse_file(repo / rel)
            count = len(_collect_test_functions(tree)) if tree is not None else 0
        rows_d.append({"file": rel, "tests": count})

    rows_f: list[str] | None = None
    if args.base:
        rows_f = section_f(repo, args.base)

    lines: list[str] = []
    lines.append("# Test hygiene report")
    lines.append("")
    lines.append(f"- HEAD: {head}")
    if covmap is None:
        lines.append("- Map: none (no coverage map; sections B and C skipped)")
    elif fresh:
        lines.append(
            f"- Map: {map_commit} (fresh), test_count {meta_tests}, "
            f"unmatched_nodes {meta_unmatched}"
        )
    else:
        lines.append(
            f"- Map: map is stale ({map_commit} vs {head}); sections B and C skipped, "
            f"test_count {meta_tests}, unmatched_nodes {meta_unmatched}"
        )
    lines.append(f"- Test node ids: {node_count} (from {node_source})")
    lines.append(f"- Test files: {test_files} ({len(task_named)} task-named)")
    if mismatch_known:
        lines.append(f"- id mismatch: map-only {map_only}, junit-only {junit_only}")
    if junit_path is not None:
        lines.append(f"- JUnit total time: {junit_total:.1f}s over {junit_cases} cases")

    lines.append("")
    lines.append("## A. Test-only src symbols")
    lines.append("")
    if rows_a:
        for row in rows_a:
            exported = " (exported)" if row["exported"] else ""
            lines.append(
                f"- {row['file']}:{row['line']} {row['symbol']}{exported}"
                f" — referenced by: {', '.join(row['referenced_by'])}"
            )
    else:
        lines.append("- none found.")
    lines.append("")
    lines.append(
        "_Precision caveat: dynamic dispatch (`getattr`, registries, CLI `set_defaults`)"
        " causes false positives._"
    )

    if rows_b is not None:
        lines.append("")
        lines.append("## B. Tests covering no src lines")
        lines.append("")
        if "needs_junit" in rows_b:
            lines.append(f"- {rows_b['needs_junit']}")
        elif rows_b:
            for rel, group in rows_b.items():
                marker = " (script test)" if group["script_test"] else ""
                lines.append(f"- {rel}{marker}:")
                for node_id in group["nodes"]:
                    lines.append(f"  - {node_id}")
        else:
            lines.append("- none found.")

    if groups_c is not None:
        lines.append("")
        lines.append("## C. Identical-coverage groups across files")
        lines.append("")
        if groups_c:
            for group in groups_c:
                lines.append(f"- {group['lines']} covered lines shared by:")
                for node_id in group["nodes"]:
                    lines.append(f"  - {node_id}")
        else:
            lines.append("- none found.")
        lines.append("")
        lines.append(
            f"- Same-file groups (usually parametrizations) counted, not listed: {same_file_c}."
        )

    lines.append("")
    lines.append("## D. Task-named test files")
    lines.append("")
    if rows_d:
        for row in rows_d:
            lines.append(f"- {row['file']}: {row['tests']} tests")
    else:
        lines.append("- none found.")

    if slowest is not None:
        lines.append("")
        lines.append("## E. Slowest 20 tests")
        lines.append("")
        if slowest:
            for entry in slowest:
                lines.append(f"- {entry['time']:.3f}s {entry['node']}")
        else:
            lines.append("- none found.")

    if rows_f is not None:
        lines.append("")
        lines.append("## F. New tests without Covers:")
        lines.append("")
        if rows_f:
            for name in rows_f:
                lines.append(f"- {name}")
        else:
            lines.append("- none found.")

    lines.append("")
    lines.append(FOOTER)
    markdown = "\n".join(lines) + "\n"

    payload: dict[str, Any] = {
        "header": {
            "head": head,
            "map_commit": map_commit,
            "map_status": "fresh" if fresh else ("stale" if covmap is not None else "missing"),
            "node_count": node_count,
            "node_source": node_source,
            "test_files": test_files,
            "task_named_count": len(task_named),
            "junit_total_time": junit_total if junit_path is not None else None,
            "junit_cases": junit_cases if junit_path is not None else None,
            "map_test_count": meta_tests if covmap is not None else None,
            "map_unmatched_nodes": meta_unmatched if covmap is not None else None,
            "id_mismatch_map_only": map_only if mismatch_known else None,
            "id_mismatch_junit_only": junit_only if mismatch_known else None,
        },
        "a": rows_a,
        "b": rows_b,
        "c": {"groups": groups_c, "same_file_groups": same_file_c}
        if groups_c is not None
        else None,
        "d": rows_d,
        "e": slowest,
        "f": rows_f,
        "footer": FOOTER,
    }
    blob = json.dumps(payload, indent=1, sort_keys=True) + "\n"

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(markdown, encoding="utf-8")
    else:
        sys.stdout.write(markdown)
    if args.json:
        json_path = Path(args.json)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(blob, encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
