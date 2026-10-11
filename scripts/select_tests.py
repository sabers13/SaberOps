#!/usr/bin/env python3
"""Pick the pytest targets that a diff actually affects.

`scripts/gate-full.sh` is the authoritative gate (ruff + mypy --strict + the whole
pytest suite), but pytest is ~95% of its runtime. This script answers the narrower
per-task question: *which tests does the current diff touch?*

    python scripts/select_tests.py [--base REF] [--strategy {auto,coverage,static}]
        [--map PATH] [--resolve] [--explain] [--json]

Two strategies pick graph-rule tests (every FULL/UI/docs/changed-test rule below
applies to both):

* ``coverage`` (default when a fresh map exists): executed-code reachability from
  the sharded map built by ``scripts/build_test_map.py``. Changed lines covered
  by a test select that test's node ID; added/deleted lines select the tests
  covering the nearest covered line in the same function (``ast`` spans).
  Changed module-level lines (class bodies, field definitions, constants,
  decorators, imports) execute at import time, outside any test context, so
  plain reachability cannot attribute them: the changed top-level symbol is
  identified with ``ast`` (class/function/constant/attribute name) and its
  usages in src/ and tests/ (``ast`` Name/Attribute references; string
  references such as getattr names are not tracked) select the tests covering
  the usage lines plus test files referencing the symbol directly. A changed src
  file that is new, has no covering context (blind spot), or whose map is stale
  (its build commit is not an ancestor of HEAD) falls back to the static rule
  below, as does a module-level line whose symbol cannot be resolved (the
  reason is shown with ``--explain``). Deleted symbols are resolved against the
  BASE tree, which is where their usages live.
* ``static``: the legacy transitive import graph (stdlib-only ``ast`` scan).

The static fallback inside coverage mode is deliberately narrower than the legacy
rule: only tests that import the changed module *directly* (depth 1). A hub
module that every test transitively imports would otherwise re-select the suite.

Changed paths are the committed diff versus `--base REF` plus staged, unstaged and
untracked files, so a working tree that has not been committed yet is selected
correctly. Changed *lines* come from the same three diffs with `-U0`.

stdout carries the selection only -- one pytest node ID (coverage) or test path
(static) per line, or a single line `FULL` when the whole suite is required.
Diagnostics (the `--explain` reasons, and always the FULL trigger) go to stderr,
so stdout stays machine-readable:

    selection=$(python scripts/select_tests.py --base "$base" --explain 2>reasons.txt)

Selection is conservative: when a rule cannot decide, the whole suite is selected.
Nothing here passes or fails anything, except one guard: with `--resolve` the
selected node IDs are checked against the current tree (`pytest --collect-only`
on the selected test files) and IDs that no longer exist -- ghosts from a map
built before a test was deleted or renamed, including whole deleted test files
-- are dropped and reported as "ghost IDs dropped (N)" on stderr. The caller
(`scripts/gate-affected.sh`) fails the gate when the pre-resolution selection
was non-empty but nothing resolvable remains. Standard library only, no new
runtime or dev dependencies.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

SOURCE_ROOT = "src"
TEST_ROOT = "tests"

# Directories scanned for the static import graph, as (repo-relative root, drop the
# root component from the module name). "src" uses a src layout, so
# src/saberops/workers/env.py is the module "saberops.workers.env"; tests keep their
# own prefix, so tests/test_x.py is "tests.test_x".
GRAPH_ROOTS: tuple[tuple[str, bool], ...] = ((SOURCE_ROOT, True), (TEST_ROOT, False))

# ---------------------------------------------------------------------------
# Selection rules.
#
# Evaluated in the order below; the first rule that matches a changed path decides
# what that path selects.
#
# | #  | Changed path                                             | Selection
# |----|----------------------------------------------------------|------------------------------
# | 1  | any conftest.py (here: tests/conftest.py)                | FULL suite
# | 2  | pyproject.toml and the other setup/build files           | FULL suite
# | 3  | scripts/gate-*.sh, scripts/select_tests.py                | FULL suite
# | 4  | .github/workflows/*                                       | FULL suite
# | 5  | the top-level package init src/saberops/__init__.py       | FULL suite
# | 6  | packaged presentation, src/saberops/templates/** and       | the gate-ui-fast node IDs,
# |    | src/saberops/static/**                                     | plus tests naming the file
# | 7  | docs/**, *.md, .orch/**                                   | no tests
# | 8  | *.py under a graph root, src/** and tests/**              | every test importing it,
# |    |                                                          | directly or transitively
# | 9  | any other *.py, no module identity                        | FULL suite (fail safe)
# | 10 | any other changed file, data/config/unknown               | FULL suite (fail safe)
#
# Rule 6 emits node IDs rather than files because that is what gate-ui-fast.sh runs,
# and a template/asset change is only checked by those rendering tests.
# "Directly or transitively" means the static import graph below: real imports plus
# dotted paths written as strings (monkeypatch / importlib targets). Imports inside
# `if TYPE_CHECKING:` never execute and are therefore not edges.
# ---------------------------------------------------------------------------
SETUP_FILES = frozenset(
    {
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "tox.ini",
        "requirements.txt",
        "requirements-dev.txt",
        "Pipfile",
        "Pipfile.lock",
    }
)
GATE_SCRIPT_PREFIX = "scripts/gate-"
SELF_PATH = "scripts/select_tests.py"
WORKFLOW_PREFIX = ".github/workflows/"
PRESENTATION_DIRS = frozenset({"templates", "static"})

# The exact node IDs run by scripts/gate-ui-fast.sh. Keep in sync with that script;
# editing either one selects the FULL suite by rule 3 anyway.
UI_NODE_IDS: tuple[str, ...] = (
    "tests/test_c16c2_console.py::test_c16c2_dashboard_lists_runs_with_kept_tokens",
    "tests/test_c16c2_console.py::test_c16c2_run_detail_renders_status_evidence_and_actions",
    "tests/test_c16c2_console.py::test_c16c2_missing_run_keeps_minimal_page",
    "tests/test_c16c2_console.py::test_c16c2_report_and_health_stay_json",
    "tests/test_c16c2_console.py::test_c16c2_console_assets_are_served",
)

RULE_FULL = "FULL"
RULE_NONE = "NONE"
RULE_GRAPH = "GRAPH"
RULE_PRESENTATION = "PRESENTATION"

STRATEGY_AUTO = "auto"
STRATEGY_COVERAGE = "coverage"
STRATEGY_STATIC = "static"

MAP_DIRNAME = "saberops-test-map"
MAP_META = "meta.json"
MAP_FILES = "files"
MAP_VERSION = 2

# Dotted path inside a string literal, e.g. a monkeypatch/importlib target.
_DOTTED_TARGET = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+)\b")

_SKIPPED_PARTS = frozenset({"__pycache__", "node_modules", ".git", ".venv"})


class SelectorError(RuntimeError):
    """A precondition failed (bad base ref, not a git repository, ...)."""


@dataclass(frozen=True)
class ModuleNode:
    """One scanned .py file and the module name it defines."""

    name: str
    path: str
    is_test: bool
    is_package: bool


@dataclass
class ImportGraph:
    """modules by dotted name, plus module -> imported module -> edge kind."""

    modules: dict[str, ModuleNode] = field(default_factory=dict)
    edges: dict[str, dict[str, str]] = field(default_factory=dict)

    def test_paths(self) -> list[str]:
        return sorted(node.path for node in self.modules.values() if node.is_test)


@dataclass(frozen=True)
class SelectedTest:
    """One selected pytest target plus the reason it was selected."""

    target: str
    via: str
    chain: tuple[str, ...] = ()
    detail: str = ""


@dataclass
class Selection:
    base: str
    changed: list[str]
    tests: list[SelectedTest]
    full: bool = False
    full_reason: str = ""
    ignored: list[str] = field(default_factory=list)
    strategy: str = STRATEGY_STATIC
    map_commit: str = ""
    map_built_at: str = ""
    map_stale: bool = False
    map_missing: bool = False
    ghosts: list[str] = field(default_factory=list)
    map_age_commits: int = -1
    unmapped_lines: int = 0
    changed_line_total: int = 0

    @property
    def unmapped_share(self) -> float:
        if not self.changed_line_total:
            return 0.0
        return self.unmapped_lines / self.changed_line_total


@dataclass(frozen=True)
class CoverageMap:
    """Executed-code reachability from scripts/build_test_map.py (sharded).

    `nodes` is the shared node-ID table; each ``files/<src path>.json`` shard
    maps a line number to node indices. A line present with an empty list
    executed only outside any test (import/collection time): it is executable,
    but no test context covers it. `blind` holds src modules with zero covering
    contexts anywhere. Shards load lazily so a gate run only reads the files the
    diff touches.
    """

    path: str
    commit: str
    built_at: str
    test_count: int
    coverage_version: str
    nodes: tuple[str, ...]
    by_test: dict[str, list[str]]
    blind: frozenset[str]

    @classmethod
    def load(cls, path: Path) -> CoverageMap | None:
        """Read a map directory (or its meta.json); None when unusable."""
        meta_path = path / MAP_META if path.is_dir() else path
        try:
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict) or payload.get("version") != MAP_VERSION:
            return None
        try:
            nodes = tuple(map(str, payload["nodes"]))
            by_test = {
                str(test): [nodes[i] for i in indices]
                for test, indices in payload["by_test"].items()
            }
            return cls(
                path=str(path),
                commit=str(payload.get("commit", "")),
                built_at=str(payload.get("built_at", "")),
                test_count=int(payload.get("test_count", 0)),
                coverage_version=str(payload.get("coverage_version", "")),
                nodes=nodes,
                by_test=by_test,
                blind=frozenset(map(str, payload.get("blind", []))),
            )
        except (AttributeError, TypeError, ValueError, IndexError, KeyError):
            return None

    def lines_for(self, rel: str) -> dict[int, list[str]] | None:
        """Line -> covering node IDs for one src file; None when not in the map."""
        cache = _SHARD_CACHE.get(self.path)
        if cache is None:
            cache = {}
            _SHARD_CACHE[self.path] = cache
        if rel not in cache:
            base = Path(self.path)
            shard_dir = base / MAP_FILES if base.is_dir() else base.parent / MAP_FILES
            try:
                payload = json.loads((shard_dir / f"{rel}.json").read_text(encoding="utf-8"))
                cache[rel] = {
                    int(line): [self.nodes[i] for i in indices]
                    for line, indices in payload.items()
                }
            except (OSError, ValueError, IndexError, AttributeError):
                cache[rel] = None
        return cache[rel]


_SHARD_CACHE: dict[str, dict[str, dict[int, list[str]] | None]] = {}


# ---------------------------------------------------------------------------
# git helpers
# ---------------------------------------------------------------------------


def run_git(repo: Path, *args: str) -> list[str]:
    completed = subprocess.run(
        ("git", "-c", "core.quotePath=false", *args),
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise SelectorError(f"git {' '.join(args)} failed: {completed.stderr.strip()}")
    return [line for line in completed.stdout.splitlines() if line.strip()]


def try_git(repo: Path, *args: str) -> list[str]:
    completed = subprocess.run(
        ("git", "-c", "core.quotePath=false", *args),
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return []
    return [line for line in completed.stdout.splitlines() if line.strip()]


def default_base(repo: Path) -> str:
    """Merge-base with the branch upstream when there is one, else HEAD~1."""
    upstream = try_git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    if upstream:
        merged = try_git(repo, "merge-base", "HEAD", upstream[0])
        if merged:
            return merged[0]
    parent = try_git(repo, "rev-parse", "--verify", "--quiet", "HEAD~1")
    if parent:
        return parent[0]
    raise SelectorError("cannot choose a default base: no upstream and no HEAD~1")


def changed_paths(repo: Path, base: str) -> list[str]:
    """Committed diff versus base, plus staged, unstaged and untracked paths."""
    run_git(repo, "rev-parse", "--verify", "--quiet", base)
    paths: set[str] = set()
    paths.update(run_git(repo, "diff", "--name-only", base, "HEAD"))
    paths.update(run_git(repo, "diff", "--name-only", "--cached"))
    paths.update(run_git(repo, "diff", "--name-only"))
    paths.update(run_git(repo, "ls-files", "--others", "--exclude-standard"))
    return sorted(paths)


# ---------------------------------------------------------------------------
# coverage map
# ---------------------------------------------------------------------------


def default_map_path(repo: Path) -> Path:
    """Shared map directory: git-common-dir, so all worktrees see one map."""
    lines = run_git(repo, "rev-parse", "--git-common-dir")
    if not lines:
        raise SelectorError("cannot locate the git common dir")
    common = lines[0]
    common_dir = (repo / common).resolve() if not Path(common).is_absolute() else Path(common)
    return common_dir / MAP_DIRNAME


def load_map(path: Path) -> CoverageMap | None:
    """Read a map directory (or its meta.json); None when unusable."""
    return CoverageMap.load(path)


def map_is_fresh(repo: Path, covmap: CoverageMap) -> bool:
    """True when the map's build commit is an ancestor of HEAD (or HEAD itself)."""
    if not re.fullmatch(r"[0-9a-f]{4,64}", covmap.commit):
        return False
    completed = subprocess.run(
        ("git", "merge-base", "--is-ancestor", covmap.commit, "HEAD"),
        cwd=repo,
        check=False,
        capture_output=True,
    )
    return completed.returncode == 0


# ---------------------------------------------------------------------------
# changed lines (-U0 hunks from the committed, staged and unstaged diffs)
# ---------------------------------------------------------------------------

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _new_side_lines(diff_text: str, path: str) -> set[int]:
    """New-side line numbers of `path` in one `git diff -U0` output."""
    lines: set[int] = set()
    current: str | None = None
    for text in diff_text.splitlines():
        if text.startswith("+++ "):
            name = text[4:].strip()
            if name.startswith('"') and name.endswith('"'):
                name = name[1:-1]
            if name.startswith("b/"):
                name = name[2:]
            current = None if name == "/dev/null" else name
        elif text.startswith("@@ ") and current == path:
            match = _HUNK.match(text)
            if match is None:
                continue
            start = int(match.group(1))
            count = int(match.group(2)) if match.group(2) is not None else 1
            if count == 0:
                # Pure deletion: anchor the line the removal happened at so the
                # neighbour search below has a position to work from.
                lines.add(max(start, 1))
            else:
                lines.update(range(start, start + count))
    return lines


def _zero_context_diffs(repo: Path, base: str) -> list[str]:
    """The three `-U0` diffs (committed, staged, unstaged) as text, no renames."""
    run_git(repo, "rev-parse", "--verify", "--quiet", base)
    return [
        "\n".join(run_git(repo, "diff", "-U0", "--no-renames", base, "HEAD")),
        "\n".join(run_git(repo, "diff", "-U0", "--no-renames", "--cached")),
        "\n".join(run_git(repo, "diff", "-U0", "--no-renames")),
    ]


def _removed_old_lines(diff_text: str, path: str) -> set[int]:
    """Old-side line numbers removed from `path` in one `git diff -U0` output."""
    lines: set[int] = set()
    current: str | None = None
    old_line = 0
    for text in diff_text.splitlines():
        if text.startswith("+++ "):
            name = text[4:].strip()
            if name.startswith('"') and name.endswith('"'):
                name = name[1:-1]
            if name.startswith("b/"):
                name = name[2:]
            current = None if name == "/dev/null" else name
        elif text.startswith("--- "):
            continue
        elif text.startswith("@@ ") and current == path:
            match = _HUNK.match(text)
            if match is None:
                continue
            old_match = re.match(r"^@@ -(\d+)(?:,(\d+))? \+\d+", text)
            old_line = int(old_match.group(1)) if old_match else 0
        elif current == path and old_line:
            if text.startswith("-"):
                lines.add(old_line)
                old_line += 1
            elif text.startswith("\\"):
                continue
            elif text.startswith("+"):
                continue
    return lines


def removed_lines(repo: Path, base: str) -> dict[str, set[int]]:
    """Per changed path: old-side line numbers deleted by this diff.

    Only paths that still have a diff entry are keys; new files map to the
    empty set. Used to resolve deleted top-level symbols against the BASE tree.
    """
    diffs = _zero_context_diffs(repo, base)
    paths = changed_paths(repo, base)
    result: dict[str, set[int]] = {}
    for path in paths:
        merged: set[int] = set()
        for diff_text in diffs:
            merged.update(_removed_old_lines(diff_text, path))
        if merged:
            result[path] = merged
    return result


def show_file_at(repo: Path, base: str, path: str) -> str | None:
    """Content of `path` at `base`, or None when it did not exist there."""
    completed = subprocess.run(
        ("git", "show", f"{base}:{path}"),
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return None
    return completed.stdout


def map_age_commits(repo: Path, map_commit: str) -> int:
    """Commits between the map build and HEAD, or -1 when unknowable."""
    if not re.fullmatch(r"[0-9a-f]{4,64}", map_commit):
        return -1
    lines = try_git(repo, "rev-list", "--count", f"{map_commit}..HEAD")
    if not lines:
        return -1
    try:
        return int(lines[0])
    except ValueError:
        return -1


def changed_lines(repo: Path, base: str) -> dict[str, set[int] | None]:
    """Per changed path: new-side line numbers, or None for new/deleted files.

    New files (added or untracked) and deleted files cannot be line-mapped, so
    callers apply the static fallback to them. `--no-renames` keeps a rename a
    delete plus an add, which is the safe reading.
    """
    run_git(repo, "rev-parse", "--verify", "--quiet", base)
    untracked = set(run_git(repo, "ls-files", "--others", "--exclude-standard"))
    diffs = _zero_context_diffs(repo, base)
    paths = changed_paths(repo, base)
    result: dict[str, set[int] | None] = {}
    for path in paths:
        if path in untracked or not (repo / path).exists():
            result[path] = None
            continue
        merged: set[int] = set()
        for diff_text in diffs:
            merged.update(_new_side_lines(diff_text, path))
        result[path] = merged
    # A path added in any diff (committed, staged or unstaged) is new even when it
    # exists on disk: its lines have no baseline, so fall back to static.
    statuses = _change_status(repo, base)
    for path, status in statuses.items():
        if status == "A" and path in result and (repo / path).exists():
            result[path] = None
    return result


def _change_status(repo: Path, base: str) -> dict[str, str]:
    """Committed/staged/unstaged name statuses (A/M/D/...), renames disabled."""
    statuses: dict[str, str] = {}
    for args in (
        ("diff", "--no-renames", "--name-status", base, "HEAD"),
        ("diff", "--no-renames", "--name-status", "--cached"),
        ("diff", "--no-renames", "--name-status"),
    ):
        for line in run_git(repo, *args):
            parts = line.split("\t")
            if len(parts) >= 2:
                statuses[parts[1]] = parts[0][:1]
    return statuses


# ---------------------------------------------------------------------------
# function spans and nearest-covered-line search
# ---------------------------------------------------------------------------


def function_spans(tree: ast.Module) -> list[tuple[int, int, str]]:
    """(start, end, qualified name) for every function, nested included."""
    spans: list[tuple[int, int, str]] = []

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = f"{prefix}{child.name}"
                spans.append((child.lineno, child.end_lineno or child.lineno, name))
                visit(child, prefix=f"{name}.")
            elif isinstance(child, ast.ClassDef):
                visit(child, prefix=f"{prefix}{child.name}.")
            elif isinstance(child, (ast.Module, ast.If, ast.For, ast.While, ast.With,
                                     ast.Try, ast.ExceptHandler)):
                visit(child, prefix=prefix)

    visit(tree, prefix="")
    return spans


def nearest_covered(
    line: int, spans: list[tuple[int, int, str]], covered: set[int]
) -> tuple[int, str] | None:
    """Nearest covered line in the innermost function containing `line`."""
    candidates = [span for span in spans if span[0] <= line <= span[1]]
    if not candidates:
        return None
    start, end, name = min(candidates, key=lambda span: (span[1] - span[0], span[0]))
    best: tuple[int, str] | None = None
    for covered_line in sorted(covered):
        if start <= covered_line <= end and covered_line != line:
            distance = abs(covered_line - line)
            if best is None or distance < abs(best[0] - line):
                best = (covered_line, name)
    return best


# ---------------------------------------------------------------------------
# top-level symbols and their usages (module-level change attribution)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TopSymbol:
    """One module-level definition: class, function, constant or attribute."""

    name: str
    start: int
    end: int
    kind: str  # "class" | "method" | "function" | "variable" | "classattr"
    owner: str = ""  # enclosing class for "method"/"classattr", else ""


def _assign_targets(target: ast.expr) -> list[str]:
    """Plain names bound by an assignment target (`a, b.c = ...` -> ["a"])."""
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        found: list[str] = []
        for element in target.elts:
            found.extend(_assign_targets(element))
        return found
    if isinstance(target, ast.Starred):
        return _assign_targets(target.value)
    return []


def _def_start(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> int:
    """First line of a definition, including its decorators."""
    starts = [node.lineno]
    starts.extend(getattr(dec, "lineno", node.lineno) for dec in node.decorator_list)
    return min(starts)


def top_level_symbols(tree: ast.Module) -> list[TopSymbol]:
    """Every module-level definition with its line range.

    Records class names (whole class range), method names, function names,
    module constants (plain assignment targets) and class attributes/fields
    (class-body-level assignment targets, including annotated ones).
    Import statements bind names too, but an import-line change is left
    unresolvable: naming every importer of the bound module would be as wide
    as the static fallback, so the caller falls back with that reason.
    """
    symbols: list[TopSymbol] = []
    for node in tree.body:
        end = node.end_lineno or node.lineno
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start = _def_start(node)
            symbols.append(TopSymbol(node.name, start, end, "function"))
        elif isinstance(node, ast.ClassDef):
            start = _def_start(node)
            symbols.append(TopSymbol(node.name, start, end, "class"))
            for sub in node.body:
                sub_end = sub.end_lineno or sub.lineno
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    symbols.append(
                        TopSymbol(sub.name, sub.lineno, sub_end, "method", node.name)
                    )
                elif isinstance(sub, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                    targets = (
                        [sub.target]
                        if isinstance(sub, (ast.AnnAssign, ast.AugAssign))
                        else list(sub.targets)
                    )
                    for target in targets:
                        for name in _assign_targets(target):
                            symbols.append(
                                TopSymbol(name, sub.lineno, sub_end, "classattr", node.name)
                            )
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = (
                [node.target]
                if isinstance(node, (ast.AnnAssign, ast.AugAssign))
                else list(node.targets)
            )
            for target in targets:
                for name in _assign_targets(target):
                    symbols.append(TopSymbol(name, node.lineno, end, "variable"))
    return symbols


def enclosing_symbol(symbols: list[TopSymbol], line: int) -> TopSymbol | None:
    """Innermost (smallest-range) top-level symbol containing `line`."""
    candidates = [sym for sym in symbols if sym.start <= line <= sym.end]
    if not candidates:
        return None
    return min(candidates, key=lambda sym: (sym.end - sym.start, sym.start))


def _import_bindings(tree: ast.Module, path: str) -> tuple[dict[str, str], set[str]]:
    """Local names bound by imports: (name -> dotted path, star-imported modules).

    `from m import f` binds `f` to `m.f` (the symbol itself);
    `import a.b` binds `a`; qualifier chains resolve through these.
    """
    bindings: dict[str, str] = {}
    stars: set[str] = set()
    mod = module_name_for(PurePosixPath(path))
    is_pkg = path.endswith("__init__.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".")[0]
                bindings.setdefault(local, alias.name)
        elif isinstance(node, ast.ImportFrom):
            if mod is None:
                continue
            base = _resolve_base(node, mod, is_pkg)
            if not base:
                continue
            for alias in node.names:
                if alias.name == "*":
                    stars.add(base)
                    continue
                bindings.setdefault(alias.asname or alias.name, f"{base}.{alias.name}")
    return bindings, stars


def _qualifier_path(
    value: ast.expr, bindings: dict[str, str]
) -> str | None:
    """Dotted path a call qualifier denotes (`reg.x` -> `m.reg...x`)."""
    parts: list[str] = []
    current = value
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    root = bindings.get(current.id)
    if root is None:
        return None
    parts.append(root)
    return ".".join(reversed(parts))


def _alias_from_changed(
    parent: ast.AST | None, path: str, changed_module: str
) -> bool:
    """True when an import alias imports from the changed module."""
    if isinstance(parent, ast.ImportFrom):
        mod = module_name_for(PurePosixPath(path))
        if mod is None:
            return False
        return _resolve_base(parent, mod, path.endswith("__init__.py")) == changed_module
    return False


def _symbol_matches(
    tree: ast.Module,
    symbol: TopSymbol,
    skip_lines: set[int],
    changed_module: str,
    bindings: dict[str, str] | None,
    stars: set[str],
    loose: bool,
    path: str = "",
) -> tuple[list[int], bool]:
    """Lines referencing `symbol`: (usage lines, class-name co-occurrence).

    Matches ``ast`` Name/Attribute references only; string references such as
    getattr names or patch-target strings are not tracked (documented
    limitation). ``skip_lines`` excludes occurrences on the changed lines
    themselves (the definition being edited is not its own usage).
    Class attributes and methods are matched as attribute references and only
    count when the owning class name also appears in the file: a bare
    ``.name`` occurs on unrelated objects far too often to attribute alone.
    Lowercase function names get the same protection through import
    resolution: a bare call counts only when the name is imported from the
    changed module (or star-imported), and an attribute counts only when its
    qualifier resolves to the changed module -- so ``AdapterRegistry.default``
    never attributes to ``registry.default``. Field defaults are observed by
    construction: a call building the owner class without passing the field
    explicitly (no matching keyword; ``**kwargs`` counts as maybe-passing)
    is a usage of a classattr symbol, as is ``__init__`` for a method symbol
    by the same token. The changed file itself is matched loosely (`loose`).
    """
    name = symbol.name
    usages: list[int] = []
    owner_seen = symbol.kind not in ("method", "classattr")
    if symbol.kind == "function" and not loose:
        assert bindings is not None
        # A bare `default()` call means this symbol only when the name is
        # imported from the changed module (or star-imported); anything else
        # would be a NameError -- or another module's own `default`.
        interesting = {
            local for local, dotted in bindings.items() if dotted == f"{changed_module}.{name}"
        }
        if bindings.get(name) == f"{changed_module}.{name}" or changed_module in stars:
            interesting.add(name)
        stack: list[tuple[ast.AST, ast.AST | None]] = [(tree, None)]
        while stack:
            node, parent = stack.pop()
            lineno = getattr(node, "lineno", None)
            if isinstance(node, ast.alias) and node.name == name and lineno is not None:
                # `from m import f`: importing the symbol is referencing it,
                # but only when m is the changed module -- another module's
                # same-named symbol is unrelated. (In src files the import
                # line itself almost never has direct cover, so this mainly
                # selects referencing test files.)
                if lineno not in skip_lines and _alias_from_changed(
                    parent, path, changed_module
                ):
                    usages.append(lineno)
            elif isinstance(node, ast.Name) and node.id in interesting and lineno is not None:
                if lineno not in skip_lines and isinstance(parent, ast.Call):
                    usages.append(lineno)
            elif isinstance(node, ast.Attribute) and node.attr == name and lineno is not None:
                if lineno not in skip_lines:
                    qualified = _qualifier_path(node.value, bindings)
                    if qualified == changed_module:
                        usages.append(lineno)
            for child in ast.iter_child_nodes(node):
                stack.append((child, node))
        return sorted(set(usages)), True
    stack2: list[tuple[ast.AST, ast.AST | None]] = [(tree, None)]
    watches_construction = (
        symbol.kind == "classattr"
        or (symbol.kind == "method" and symbol.name == "__init__")
    )
    while stack2:
        node, parent = stack2.pop()
        lineno = getattr(node, "lineno", None)
        if lineno is None or lineno in skip_lines:
            pass
        elif isinstance(node, ast.alias):
            if node.name == name and _alias_from_changed(parent, path, changed_module):
                usages.append(lineno)
        elif isinstance(node, ast.Call) and watches_construction:
            if _builds_owner(node, symbol, skip_lines):
                usages.append(lineno)
                owner_seen = True
        elif isinstance(node, ast.Name):
            if node.id == name and symbol.kind in ("class", "function", "variable"):
                usages.append(lineno)
            if node.id == symbol.owner and symbol.owner:
                owner_seen = True
        elif isinstance(node, ast.Attribute):
            if node.attr == name and symbol.kind in (
                "class",
                "function",
                "variable",
                "method",
                "classattr",
            ):
                usages.append(lineno)
            if node.attr == symbol.owner and symbol.owner:
                owner_seen = True
        for child in ast.iter_child_nodes(node):
            stack2.append((child, node))
    if symbol.kind in ("method", "classattr") and not owner_seen:
        return [], False
    return sorted(set(usages)), True


def _builds_owner(node: ast.Call, symbol: TopSymbol, skip_lines: set[int]) -> bool:
    """True when a call builds the symbol's owner without passing the field.

    `RunConfig(...)` without a `gate_enabled=` keyword observes the field
    default; an explicit keyword (or anything else) does not. A `**kwargs`
    spread counts as maybe-passing (conservative). Applies to classattr
    symbols (field defaults) and `__init__` methods.
    """
    func = node.func
    if isinstance(func, ast.Name):
        if func.id != symbol.owner:
            return False
    elif isinstance(func, ast.Attribute):
        if func.attr != symbol.owner:
            return False
    else:
        return False
    if getattr(node, "lineno", None) in skip_lines:
        return False
    if symbol.kind == "method":
        return True
    for keyword in node.keywords:
        if keyword.arg is None:
            return True
        if keyword.arg == symbol.name:
            return False
    return True


def _parse_text(text: str) -> ast.Module | None:
    try:
        return ast.parse(text)
    except (SyntaxError, ValueError):
        return None


# ---------------------------------------------------------------------------
# module identity
# ---------------------------------------------------------------------------


def _scanned_root(rel: PurePosixPath) -> tuple[str, bool] | None:
    for root, drop in GRAPH_ROOTS:
        if len(rel.parts) > 1 and rel.parts[0] == root:
            return root, drop
    return None


def module_name_for(rel: PurePosixPath) -> str | None:
    """Dotted module name for a path under a graph root, else None."""
    found = _scanned_root(rel)
    if found is None:
        return None
    parts = list(rel.parts)
    if found[1]:
        parts = parts[1:]
    if len(parts) < 2:
        return None
    stem = parts[-1]
    if stem == "__init__.py":
        parts = parts[:-1]
    elif stem.endswith(".py"):
        parts[-1] = stem[: -len(".py")]
    else:
        return None
    return ".".join(parts)


def is_test_path(path: str) -> bool:
    return PurePosixPath(path).parts[0] == TEST_ROOT


# ---------------------------------------------------------------------------
# static import graph
# ---------------------------------------------------------------------------


def _skipped(rel: PurePosixPath) -> bool:
    return any(part in _SKIPPED_PARTS or part.endswith(".egg-info") for part in rel.parts)


def _parse(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, ValueError):
        return None


def _longest_known(name: str, known: set[str]) -> str | None:
    """Longest prefix of `name` that is a scanned module (None for third party)."""
    parts = name.split(".")
    for size in range(len(parts), 0, -1):
        candidate = ".".join(parts[:size])
        if candidate in known:
            return candidate
    return None


def _resolve_base(node: ast.ImportFrom, module: str, is_package: bool) -> str | None:
    """Absolute module name that a possibly-relative ``from`` starts from."""
    if not node.level:
        return node.module or ""
    parts = module.split(".")
    if not is_package:
        parts = parts[:-1]
    keep = len(parts) - (node.level - 1)
    if keep < 0:
        return None
    base = parts[:keep]
    if node.module:
        base = base + node.module.split(".")
    return ".".join(part for part in base if part)


def _is_type_checking_guard(node: ast.If) -> bool:
    test = node.test
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    return False


def _iter_import_nodes(tree: ast.Module) -> Iterator[ast.Import | ast.ImportFrom]:
    """Import nodes of a module, skipping ``if TYPE_CHECKING:`` bodies.

    A type-checking-only import never executes, so it cannot change runtime test
    behaviour; keeping it would widen the selection to almost every test (this repo
    type-imports session_runtime through models, which nearly every test imports).
    """
    stack: list[ast.AST] = [tree]
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            yield node
            continue
        if isinstance(node, ast.If) and _is_type_checking_guard(node):
            continue
        stack.extend(ast.iter_child_nodes(node))


def _string_targets(tree: ast.Module, known: set[str]) -> dict[str, str]:
    """Dotted module paths written as strings, e.g. monkeypatch/importlib targets."""
    targets: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        literal = node.value
        for match in _DOTTED_TARGET.finditer(literal):
            target = _longest_known(match.group(1), known)
            if target is not None:
                targets[target] = literal
    return targets


def _import_edges(
    tree: ast.Module,
    module: str,
    is_package: bool,
    known: set[str],
) -> dict[str, str]:
    edges: dict[str, str] = {}
    for node in _iter_import_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                target = _longest_known(alias.name, known)
                if target is not None:
                    edges[target] = "import"
        else:
            base = _resolve_base(node, module, is_package)
            if base is None or base == "":
                continue
            candidates = [base]
            candidates.extend(f"{base}.{alias.name}" for alias in node.names)
            for candidate in candidates:
                target = _longest_known(candidate, known)
                if target is not None:
                    edges[target] = "import"
    return edges


def build_graph(repo: Path, extra_modules: dict[str, str] | None = None) -> ImportGraph:
    """Scan src/** and tests/**, then map every import to a scanned module.

    `extra_modules` names modules that no longer exist on disk (deleted in the diff);
    registering them keeps imports of a deleted module resolvable so the tests that
    imported it are still selected.
    """
    graph = ImportGraph()
    for root, _drop in GRAPH_ROOTS:
        directory = repo / root
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.py")):
            rel = PurePosixPath(path.relative_to(repo).as_posix())
            if _skipped(rel):
                continue
            name = module_name_for(rel)
            if name is None:
                continue
            graph.modules[name] = ModuleNode(
                name=name,
                path=rel.as_posix(),
                is_test=is_test_path(rel.as_posix()),
                is_package=rel.parts[-1] == "__init__.py",
            )
    for name, path in (extra_modules or {}).items():
        graph.modules.setdefault(
            name,
            ModuleNode(
                name=name,
                path=path,
                is_test=is_test_path(path),
                is_package=path.endswith("__init__.py"),
            ),
        )

    known = set(graph.modules)
    for name, node in graph.modules.items():
        tree = _parse(repo / node.path)
        if tree is None:
            graph.edges[name] = {}
            continue
        edges = _import_edges(tree, name, node.is_package, known)
        if node.is_test:
            for target in _string_targets(tree, known):
                edges.setdefault(target, "string")
        graph.edges[name] = edges
    return graph


def shortest_chain(graph: ImportGraph, start: str, goal: str) -> tuple[str, ...]:
    """Shortest import chain from `start` to `goal` (BFS over import edges)."""
    if start == goal:
        return (start,)
    seen = {start}
    queue: deque[tuple[str, tuple[str, ...]]] = deque([(start, (start,))])
    while queue:
        current, chain = queue.popleft()
        for target in sorted(graph.edges.get(current, {})):
            if target in seen:
                continue
            extended = (*chain, target)
            if target == goal:
                return extended
            seen.add(target)
            queue.append((target, extended))
    return ()


# ---------------------------------------------------------------------------
# rules
# ---------------------------------------------------------------------------


def _is_top_package_init(rel: PurePosixPath) -> bool:
    parts = rel.parts
    return len(parts) == 3 and parts[0] == SOURCE_ROOT and parts[2] == "__init__.py"


def _is_presentation(rel: PurePosixPath) -> bool:
    parts = rel.parts
    return len(parts) >= 4 and parts[0] == SOURCE_ROOT and parts[2] in PRESENTATION_DIRS


def classify(path: str) -> str:
    """Apply the rule table above to one changed path."""
    rel = PurePosixPath(path)
    if rel.name == "conftest.py":  # rule 1
        return RULE_FULL
    if path in SETUP_FILES:  # rule 2
        return RULE_FULL
    if path.startswith(GATE_SCRIPT_PREFIX) and path.endswith(".sh"):  # rule 3
        return RULE_FULL
    if path == SELF_PATH:  # rule 3
        return RULE_FULL
    if path.startswith(WORKFLOW_PREFIX):  # rule 4
        return RULE_FULL
    if _is_top_package_init(rel):  # rule 5
        return RULE_FULL
    if _is_presentation(rel):  # rule 6
        return RULE_PRESENTATION
    if rel.parts[0] == "docs" or rel.suffix == ".md" or rel.parts[0] == ".orch":  # rule 7
        return RULE_NONE
    if rel.suffix == ".py":  # rules 8 and 9
        return RULE_GRAPH if module_name_for(rel) is not None else RULE_FULL
    return RULE_FULL  # rule 10


def _referencing_tests(repo: Path, graph: ImportGraph, path: str) -> list[str]:
    """Test files whose source names the changed template/asset."""
    rel = PurePosixPath(path)
    needles = (rel.name, path, f"/{path}")
    hits: list[str] = []
    for test_path in graph.test_paths():
        try:
            text = (repo / test_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if any(needle in text for needle in needles):
            hits.append(test_path)
    return hits


def _direct_importing_tests(
    graph: ImportGraph, module_name: str, changed_path: str, reason: str
) -> dict[str, SelectedTest]:
    """Tests with an import edge straight to `module_name` (depth 1, no hops)."""
    targets: dict[str, SelectedTest] = {}
    for node_name, node in graph.modules.items():
        if not node.is_test:
            continue
        chain = shortest_chain(graph, node_name, module_name)
        if len(chain) == 2:
            targets[node.path] = SelectedTest(
                target=node.path,
                via=changed_path,
                chain=chain,
                detail=f"{reason}: direct importer",
            )
    return targets


# ---------------------------------------------------------------------------
# ghost node-ID resolution (Defect 1: map holds IDs of deleted/renamed tests)
# ---------------------------------------------------------------------------


def collect_existing_nodes(
    repo: Path, python: str, test_files: list[str]
) -> tuple[set[str], bool]:
    """Node IDs pytest currently collects from `test_files`.

    Runs `pytest --collect-only -q` on the files that exist (a deleted test
    file's IDs are ghosts without needing pytest). Returns the collected IDs
    and whether collection succeeded (exit 0). A failed collection returns
    whatever IDs were parsed so partitioning stays conservative.
    """
    if not test_files:
        return set(), True
    completed = subprocess.run(
        [python, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
         *test_files],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    collected = {
        line.strip()
        for line in completed.stdout.splitlines()
        if "::" in line.strip()
    }
    return collected, completed.returncode == 0


def partition_targets(
    repo: Path, targets: list[str], collected: set[str], collect_ok: bool
) -> tuple[list[str], list[str]]:
    """Split selected targets into (existing, ghosts).

    A node ID whose test file no longer exists is a ghost (covers deleted
    test files); otherwise it must have been collected. A file target whose
    file is gone is a ghost; an existing file target is kept as-is -- if it
    collects zero tests, pytest exits 5 and the gate fails explicitly rather
    than passing silently. When collection itself failed, only file-missing
    targets are dropped and everything else is kept (conservative: a broken
    collect must not empty a non-empty selection).
    """
    existing: list[str] = []
    ghosts: list[str] = []
    for target in targets:
        file_part = target.split("::")[0]
        if not (repo / file_part).exists():
            ghosts.append(target)
        elif "::" not in target:
            existing.append(target)
        elif target in collected:
            existing.append(target)
        elif not collect_ok:
            existing.append(target)
        else:
            ghosts.append(target)
    return existing, ghosts


def resolve_targets(
    repo: Path, targets: list[str], python: str
) -> tuple[list[str], list[str]]:
    """Drop ghost node IDs from `targets`; returns (existing, ghosts)."""
    files = sorted({target.split("::")[0] for target in targets})
    present = [path for path in files if (repo / path).exists()]
    collected, collect_ok = collect_existing_nodes(repo, python, present)
    return partition_targets(repo, targets, collected, collect_ok)


class SymbolSearch:
    """Usage index for top-level symbols changed at module level.

    Trees are parsed lazily and cached for one selection run: current trees
    for modified symbols, BASE-tree contents for changed files when resolving
    deleted symbols (usages of a deleted symbol only exist before the diff).
    Only ``ast`` Name/Attribute references are tracked; string references
    such as getattr names or patch-target strings are ignored (callers say so
    in --explain when that is why a symbol falls back).
    """

    def __init__(
        self,
        repo: Path,
        base: str,
        covmap: CoverageMap,
        graph: ImportGraph,
        changed_module: str,
        scan_paths: list[str],
        changed: set[str],
    ) -> None:
        self.repo = repo
        self.base = base
        self.covmap = covmap
        self.graph = graph
        self.changed_module = changed_module
        self.scan_paths = scan_paths
        self.changed = changed
        self._current: dict[str, ast.Module | None] = {}
        self._base_text: dict[str, str | None] = {}
        self._reachable: dict[str, bool] = {}
        self._bindings: dict[tuple[str, bool], tuple[dict[str, str], set[str]]] = {}

    def _reaches(self, upath: str, changed_path: str) -> bool:
        """True when `upath` can reach the changed module (import chain).

        A bare Name match alone is weak evidence for common-word symbols
        (a local variable named `default` is not `registry.default()`): the
        file must also import the changed module, directly or transitively.
        The changed file itself trivially reaches. Indirect test users that
        never name the symbol are still caught through src usage lines.
        """
        if upath == changed_path:
            return True
        if upath not in self._reachable:
            name = module_name_for(PurePosixPath(upath))
            if name is None or name not in self.graph.modules:
                self._reachable[upath] = False
            else:
                self._reachable[upath] = bool(
                    shortest_chain(self.graph, name, self.changed_module)
                )
        return self._reachable[upath]

    def current_tree(self, path: str) -> ast.Module | None:
        if path not in self._current:
            self._current[path] = _parse(self.repo / path)
        return self._current[path]

    def _text_for(self, path: str, for_deleted: bool) -> str | None:
        """File text to scan: BASE content for changed files when deleted."""
        if for_deleted and path in self.changed:
            if path not in self._base_text:
                self._base_text[path] = show_file_at(self.repo, self.base, path)
            return self._base_text[path]
        try:
            return (self.repo / path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    def _usage_targets(
        self,
        changed_path: str,
        symbol: TopSymbol,
        skip: dict[str, set[int]],
        for_deleted: bool,
    ) -> dict[str, SelectedTest]:
        """Tests covering `symbol` usage lines + test files naming it."""
        label = f"deleted symbol {symbol.name}" if for_deleted else f"symbol {symbol.name}"
        targets: dict[str, SelectedTest] = {}
        for upath in self.scan_paths:
            text = self._text_for(upath, for_deleted)
            if text is None:
                continue
            tree = _parse_text(text)
            if tree is None:
                continue
            loose = upath == changed_path
            bindings: dict[str, str] | None = None
            stars: set[str] = set()
            if symbol.kind == "function" and not loose:
                key = (upath, for_deleted)
                cached = self._bindings.get(key)
                if cached is None:
                    cached = _import_bindings(tree, upath)
                    self._bindings[key] = cached
                bindings, stars = cached
            usages, _ok = _symbol_matches(
                tree, symbol, skip.get(upath, set()),
                self.changed_module, bindings, stars, loose, upath,
            )
            if not usages or not self._reaches(upath, changed_path):
                continue
            if is_test_path(upath):
                nodes = self.covmap.by_test.get(upath, [])
                if nodes:
                    for node_id in sorted(nodes):
                        targets.setdefault(
                            node_id,
                            SelectedTest(
                                target=node_id,
                                via=changed_path,
                                detail=f"{label}: referenced by this test file",
                            ),
                        )
                else:
                    targets.setdefault(
                        upath,
                        SelectedTest(
                            target=upath,
                            via=changed_path,
                            detail=f"{label}: referenced by this test file",
                        ),
                    )
                continue
            shard = self.covmap.lines_for(upath)
            if shard is None:
                continue
            for usage in usages:
                group = shard.get(usage)
                if not group:
                    continue
                for node_id in group:
                    targets.setdefault(
                        node_id,
                        SelectedTest(
                            target=node_id,
                            via=changed_path,
                            detail=(f"{label}: {upath} line {usage} "
                                    "executed by this test"),
                        ),
                    )
        return targets

    def attribute_file(
        self,
        path: str,
        tree: ast.Module | None,
        symbol_lines: list[int],
        removed: set[int],
        targets: dict[str, SelectedTest],
    ) -> list[str]:
        """Attribute uncovered executable lines of one changed file.

        Groups new-side lines by enclosing top-level symbol and removed
        old-side lines by BASE-tree symbol; modified symbols resolve usages
        in the current tree, deleted symbols in the BASE tree. Mutates
        `targets`. Returns why-strings for lines no symbol could attribute
        (no enclosing symbol, or a modified symbol with no covering usage);
        a deleted symbol with no BASE-tree usages selects nothing silently.
        """
        whys: list[str] = []
        if not symbol_lines and not removed:
            return whys
        new_symbols = top_level_symbols(tree) if tree is not None else []
        new_names = {sym.name for sym in new_symbols}
        done: set[str] = set()
        skip = {path: set(symbol_lines)}

        if tree is None and symbol_lines:
            for line in symbol_lines:
                whys.append(f"no top-level symbol at line {line} (unparseable file)")
        for line in symbol_lines:
            if tree is None:
                continue
            symbol = enclosing_symbol(new_symbols, line)
            if symbol is None:
                whys.append(f"no top-level symbol at line {line}")
                continue
            if symbol.name in done:
                continue
            done.add(symbol.name)
            found = self._usage_targets(path, symbol, skip, for_deleted=False)
            if found:
                for target, item in found.items():
                    targets.setdefault(target, item)
            else:
                whys.append(f"symbol {symbol.name}: no covering usage found")

        if removed:
            base_text = show_file_at(self.repo, self.base, path)
            base_tree = _parse_text(base_text) if base_text is not None else None
            if base_tree is None:
                whys.append("removed lines present but BASE content unavailable")
                return whys
            old_symbols = top_level_symbols(base_tree)
            base_skip = {path: set(removed)}
            for line in sorted(removed):
                symbol = enclosing_symbol(old_symbols, line)
                if symbol is None or symbol.name in done:
                    continue
                done.add(symbol.name)
                if symbol.name in new_names:
                    found = self._usage_targets(path, symbol, skip, for_deleted=False)
                    if found:
                        for target, item in found.items():
                            targets.setdefault(target, item)
                    else:
                        whys.append(f"symbol {symbol.name}: no covering usage found")
                else:
                    found = self._usage_targets(path, symbol, base_skip, for_deleted=True)
                    for target, item in found.items():
                        targets.setdefault(target, item)
        return whys


def _coverage_targets_for_file(
    repo: Path,
    base: str,
    path: str,
    lines: set[int],
    line_map: dict[int, list[str]],
    covmap: CoverageMap,
    search: SymbolSearch,
    removed: set[int],
) -> tuple[dict[str, SelectedTest], bool, list[str]]:
    """Node IDs covering `lines` of `path`, plus symbol-level attribution.

    Returns (targets, touched_executable, fallback_whys). A changed line
    covered by tests selects those tests; a changed line with no direct cover
    selects the tests covering the nearest covered line in the same function.
    A changed line that is executable but has no covering test (import-time
    only: module level, decorators, defaults) is attributed through its
    top-level symbol instead of falling back to all direct importers: the
    tests covering the symbol's usage lines are selected, plus test files
    referencing the symbol directly. Deleted lines resolve their symbol
    against the BASE tree. `fallback_whys` explains every touched line no
    symbol could attribute; the caller falls back to static direct importers
    when targets are empty but executable code was touched.
    """
    covered = {line for line, nodes in line_map.items() if nodes}
    tree = _parse(repo / path)
    spans = function_spans(tree) if tree is not None else []
    targets: dict[str, SelectedTest] = {}
    touched_executable = False
    symbol_lines: list[int] = []
    for line in sorted(lines):
        nodes = line_map.get(line)
        if nodes:
            touched_executable = True
            for node_id in nodes:
                targets.setdefault(
                    node_id,
                    SelectedTest(
                        target=node_id,
                        via=path,
                        detail=f"line {line} executed by this test",
                    ),
                )
            continue
        if line in line_map:
            # Executable, but only import/collection-time contexts: no test owns it.
            touched_executable = True
            symbol_lines.append(line)
            continue
        found = nearest_covered(line, spans, covered)
        if found is not None:
            near, func = found
            for node_id in line_map[near]:
                targets.setdefault(
                    node_id,
                    SelectedTest(
                        target=node_id,
                        via=path,
                        detail=(f"line {near} executed by this test "
                                f"(nearest covered line in {func}() "
                                f"for changed line {line})"),
                    ),
                )
    whys = search.attribute_file(path, tree, symbol_lines, removed, targets)
    return targets, touched_executable, whys


def _changed_test_nodes(
    path: str, covmap: CoverageMap | None
) -> list[SelectedTest]:
    """Changed test files are always selected; node IDs when the map knows them."""
    if covmap is not None:
        nodes = covmap.by_test.get(path, [])
        if nodes:
            return [
                SelectedTest(target=node, via=path, detail="changed test file")
                for node in sorted(nodes)
            ]
    return [SelectedTest(target=path, via=path, detail="changed test file")]


def _resolve_strategy(
    repo: Path, strategy: str, map_path: Path | str | None
) -> tuple[str, CoverageMap | None, bool, bool]:
    """Requested strategy -> (effective strategy, map or None, stale, missing)."""
    if strategy not in (STRATEGY_AUTO, STRATEGY_COVERAGE, STRATEGY_STATIC):
        raise SelectorError(f"unknown strategy {strategy!r}")
    if strategy == STRATEGY_STATIC:
        return STRATEGY_STATIC, None, False, False
    resolved = default_map_path(repo) if map_path is None else Path(map_path)
    covmap = load_map(resolved)
    if covmap is None:
        return STRATEGY_STATIC, None, False, True
    if not map_is_fresh(repo, covmap):
        return STRATEGY_STATIC, covmap, True, False
    return STRATEGY_COVERAGE, covmap, False, False


def coverage_gap(
    repo: Path,
    covmap: CoverageMap,
    lines_by_path: dict[str, set[int] | None],
    candidates: list[str],
) -> tuple[int, int]:
    """(unmapped, total) changed src lines without coverage data.

    A changed line counts as unmapped when its file has no shard in the map
    (drifted or never measured) or the line itself is absent from the shard.
    New/deleted files (no line baseline) count their whole current (or BASE,
    for deleted) content as unmapped: nothing about them can be attributed.
    """
    unmapped = 0
    total = 0
    for path in candidates:
        lines = lines_by_path.get(path)
        shard = covmap.lines_for(path)
        if lines is None:
            try:
                text = (repo / path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = None
            if text is None:
                continue
            count = len(text.splitlines())
            unmapped += count
            total += count
        elif shard is None:
            unmapped += len(lines)
            total += len(lines)
        else:
            total += len(lines)
            unmapped += sum(1 for line in lines if line not in shard)
    return unmapped, total


def select(
    repo: Path,
    base: str,
    strategy: str = STRATEGY_AUTO,
    map_path: Path | str | None = None,
    resolve: bool = False,
    python: str | None = None,
) -> Selection:
    """changed paths -> the tests that must run for this diff."""
    changed = changed_paths(repo, base)
    rules = {path: classify(path) for path in changed}
    effective, covmap, stale, missing = _resolve_strategy(repo, strategy, map_path)
    age = map_age_commits(repo, covmap.commit) if covmap is not None else -1

    full_paths = [path for path, rule in rules.items() if rule == RULE_FULL]
    if full_paths:
        return Selection(
            base=base,
            changed=changed,
            tests=[],
            full=True,
            full_reason=full_paths[0],
            strategy=effective,
            map_commit=covmap.commit if covmap is not None else "",
            map_built_at=covmap.built_at if covmap is not None else "",
            map_stale=stale,
            map_missing=missing,
            map_age_commits=age,
        )

    ignored = [path for path, rule in rules.items() if rule == RULE_NONE]
    graph_paths = {path for path, rule in rules.items() if rule == RULE_GRAPH}
    presentation_paths = [path for path, rule in rules.items() if rule == RULE_PRESENTATION]

    extra: dict[str, str] = {}
    for path in sorted(graph_paths):
        name = module_name_for(PurePosixPath(path))
        if name is not None and not (repo / path).exists():
            extra[name] = path

    graph = build_graph(repo, extra)
    changed_modules: dict[str, str] = {}
    for path in sorted(graph_paths):
        name = module_name_for(PurePosixPath(path))
        if name is not None:
            changed_modules[name] = path
    changed_tests = sorted(path for path in graph_paths if is_test_path(path))

    targets: dict[str, SelectedTest] = {}
    unmapped = 0
    line_total = 0
    if effective == STRATEGY_COVERAGE:
        assert covmap is not None
        lines_by_path = changed_lines(repo, base)
        for path in changed_tests:
            for item in _changed_test_nodes(path, covmap):
                targets.setdefault(item.target, item)
        search_by_module: dict[str, SymbolSearch] = {}

        def search_for(module_name: str) -> SymbolSearch:
            cached = search_by_module.get(module_name)
            if cached is None:
                scan = [
                    node.path
                    for node in graph.modules.values()
                    if node.path.endswith(".py")
                    and not (node.is_test and node.path in changed_tests)
                ]
                cached = SymbolSearch(
                    repo, base, covmap, graph, module_name, sorted(scan), set(changed)
                )
                search_by_module[module_name] = cached
            return cached

        removed_by_path: dict[str, set[int]] | None = None

        def removed_for(path: str) -> set[int]:
            nonlocal removed_by_path
            if removed_by_path is None:
                removed_by_path = removed_lines(repo, base)
            return removed_by_path.get(path, set())

        for module_name, changed_path in sorted(changed_modules.items()):
            if is_test_path(changed_path):
                continue
            lines = lines_by_path.get(changed_path)
            line_map = covmap.lines_for(changed_path)
            if lines is None or changed_path in covmap.blind or line_map is None:
                if lines is None:
                    reason = "new or deleted file"
                elif changed_path in covmap.blind:
                    reason = "blind spot (no covering contexts)"
                else:
                    reason = "not in map"
                detail = f"static fallback ({reason})"
                for target, item in _direct_importing_tests(
                    graph, module_name, changed_path, detail
                ).items():
                    targets.setdefault(target, item)
                continue
            file_targets, touched, whys = _coverage_targets_for_file(
                repo,
                base,
                changed_path,
                lines,
                line_map,
                covmap,
                search_for(module_name),
                removed_for(changed_path),
            )
            for target, item in file_targets.items():
                targets.setdefault(target, item)
            if not file_targets and touched:
                detail = "static fallback (executable code, no covering test"
                if whys:
                    detail += "; " + "; ".join(whys)
                detail += ")"
                for target, item in _direct_importing_tests(
                    graph, module_name, changed_path, detail
                ).items():
                    targets.setdefault(target, item)
        src_candidates = [
            path for path in changed_modules.values() if not is_test_path(path)
        ]
        unmapped, line_total = coverage_gap(repo, covmap, lines_by_path, src_candidates)
    else:
        for path in changed_tests:
            targets[path] = SelectedTest(target=path, via=path, detail="changed test file")

        if stale and covmap is not None and strategy != STRATEGY_STATIC:
            # Stale map: executed-code reachability is untrustworthy, but the
            # current import graph is not, so every changed src module selects
            # its direct importers (depth 1) rather than the legacy transitive
            # closure. Missing map or explicit --strategy static keeps legacy.
            for module_name, changed_path in sorted(changed_modules.items()):
                if is_test_path(changed_path):
                    continue
                for target, item in _direct_importing_tests(
                    graph, module_name, changed_path, "static fallback (stale map)"
                ).items():
                    targets.setdefault(target, item)
        else:
            for node_name, node in graph.modules.items():
                if not node.is_test:
                    continue
                best: SelectedTest | None = None
                for module_name, changed_path in sorted(changed_modules.items()):
                    chain = shortest_chain(graph, node_name, module_name)
                    if not chain:
                        continue
                    candidate = SelectedTest(
                        target=node.path,
                        via=changed_path,
                        chain=chain,
                        detail=_chain_detail(chain, graph),
                    )
                    if best is None or len(candidate.chain) < len(best.chain):
                        best = candidate
                if best is not None:
                    targets.setdefault(best.target, best)

    for path in presentation_paths:
        for node_id in UI_NODE_IDS:
            targets.setdefault(
                node_id,
                SelectedTest(target=node_id, via=path, detail="presentation node ID"),
            )
        for test_path in _referencing_tests(repo, graph, path):
            targets.setdefault(
                test_path,
                SelectedTest(target=test_path, via=path, detail="test names the changed file"),
            )

    ghosts: list[str] = []
    ordered = sorted(targets.values(), key=lambda item: item.target)
    if resolve:
        kept, ghosts = resolve_targets(
            repo, [item.target for item in ordered], python or sys.executable
        )
        keep = set(kept)
        ordered = [item for item in ordered if item.target in keep]

    return Selection(
        base=base,
        changed=changed,
        tests=ordered,
        ignored=ignored,
        strategy=effective,
        map_commit=covmap.commit if covmap is not None else "",
        map_built_at=covmap.built_at if covmap is not None else "",
        map_stale=stale,
        map_missing=missing,
        ghosts=sorted(ghosts),
        map_age_commits=age,
        unmapped_lines=unmapped,
        changed_line_total=line_total,
    )


def _chain_detail(chain: tuple[str, ...], graph: ImportGraph) -> str:
    """Why the last hop of an import chain holds."""
    if len(chain) == 1:
        return "changed test file"
    kind = graph.edges.get(chain[-2], {}).get(chain[-1], "import")
    if kind == "string":
        return f'patch target "{chain[-2]}" -> {chain[-1]}'
    return "direct import" if len(chain) == 2 else "transitive import"


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------


def format_explain(selection: Selection) -> str:
    lines = [f"base: {selection.base}"]
    lines.append(f"strategy: {selection.strategy}")
    if selection.strategy == STRATEGY_COVERAGE:
        lines.append(f"map commit: {selection.map_commit} (built {selection.map_built_at})")
        if selection.map_age_commits >= 0:
            lines.append(f"map age: {selection.map_age_commits} commits")
        if selection.changed_line_total:
            share = 100.0 * selection.unmapped_share
            lines.append(
                f"changed lines without coverage data: {selection.unmapped_lines}/"
                f"{selection.changed_line_total} ({share:.0f}%)"
            )
            if selection.unmapped_share > 0.5:
                lines.append("map stale for this diff, consider rebuild")
    elif selection.map_stale:
        lines.append(
            f"map commit {selection.map_commit} is not an ancestor of HEAD: "
            "stale map, static selection"
        )
    elif selection.map_missing:
        lines.append("no coverage map: static selection")
    lines.append(f"changed files ({len(selection.changed)}):")
    lines.extend(f"  {path}" for path in selection.changed)
    if selection.full:
        lines.append(f"FULL suite required: {selection.full_reason}")
        return "\n".join(lines)
    if selection.ghosts:
        lines.append(f"ghost IDs dropped ({len(selection.ghosts)}):")
        lines.extend(f"  {target}" for target in selection.ghosts)
    if selection.ignored:
        lines.append(f"no tests for ({len(selection.ignored)}):")
        lines.extend(f"  {path}" for path in selection.ignored)
    lines.append(f"selected tests ({len(selection.tests)}):")
    for item in selection.tests:
        lines.append(f"  {item.target}")
        lines.append(f"    via: {item.via} ({item.detail})")
        if item.chain:
            lines.append(f"    chain: {' -> '.join(item.chain)}")
    return "\n".join(lines)


def selection_json(selection: Selection) -> str:
    return json.dumps(
        {
            "base": selection.base,
            "full": selection.full,
            "full_reason": selection.full_reason,
            "changed": selection.changed,
            "ignored": selection.ignored,
            "strategy": selection.strategy,
            "map": {
                "commit": selection.map_commit,
                "built_at": selection.map_built_at,
                "stale": selection.map_stale,
                "missing": selection.map_missing,
                "age_commits": selection.map_age_commits,
                "unmapped_lines": selection.unmapped_lines,
                "changed_line_total": selection.changed_line_total,
            },
            "ghosts": selection.ghosts,
            "tests": [
                {
                    "target": item.target,
                    "via": item.via,
                    "detail": item.detail,
                    "chain": list(item.chain),
                }
                for item in selection.tests
            ],
        },
        indent=2,
        sort_keys=True,
    )


def render(selection: Selection) -> str:
    if selection.full:
        return "FULL"
    return "\n".join(item.target for item in selection.tests)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Select the pytest targets affected by the current diff.",
    )
    parser.add_argument(
        "--base",
        default=None,
        help="git ref to diff against (default: merge-base with upstream, else HEAD~1)",
    )
    parser.add_argument(
        "--repo",
        default=".",
        help="repository root (default: current directory)",
    )
    parser.add_argument(
        "--strategy",
        default=STRATEGY_AUTO,
        choices=(STRATEGY_AUTO, STRATEGY_COVERAGE, STRATEGY_STATIC),
        help="test selection strategy (default: coverage when a fresh map exists)",
    )
    parser.add_argument(
        "--map",
        default=None,
        help="coverage map directory (default: git-common-dir/saberops-test-map)",
    )
    parser.add_argument(
        "--explain",
        action="store_true",
        help="print, per selected test, the changed path and import chain (stderr)",
    )
    parser.add_argument(
        "--resolve",
        action="store_true",
        help=("check selected node IDs against the current tree "
              "(pytest --collect-only) and drop ghost IDs"),
    )
    parser.add_argument("--json", action="store_true", help="emit JSON on stdout")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo = Path(args.repo).resolve()
    try:
        base = args.base or default_base(repo)
        selection = select(
            repo, base, strategy=args.strategy, map_path=args.map,
            resolve=args.resolve,
        )
    except SelectorError as error:
        print(f"select_tests: {error}", file=sys.stderr)
        return 2
    if args.json:
        print(selection_json(selection))
    else:
        rendered = render(selection)
        if rendered:
            print(rendered)
    if selection.full:
        print(f"FULL suite required: {selection.full_reason}", file=sys.stderr)
    if selection.ghosts:
        print(f"ghost IDs dropped ({len(selection.ghosts)})", file=sys.stderr)
    if args.explain:
        print(format_explain(selection), file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
