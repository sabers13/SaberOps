#!/usr/bin/env python3
"""Pick the pytest targets that a diff actually affects.

`scripts/gate-full.sh` is the authoritative gate (ruff + mypy --strict + the whole
pytest suite), but pytest is ~95% of its runtime. This script answers the narrower
per-task question: *which test files does the current diff touch?*

    python scripts/select_tests.py [--base REF] [--explain] [--json]

Changed paths are the committed diff versus `--base REF` plus staged, unstaged and
untracked files, so a working tree that has not been committed yet is selected
correctly.

stdout carries the selection only -- one test path or pytest node ID per line, or a
single line `FULL` when the whole suite is required. Diagnostics (the `--explain`
reasons, and always the FULL trigger) go to stderr, so stdout stays machine-readable:

    selection=$(python scripts/select_tests.py --base "$base" --explain 2>reasons.txt)

Selection is conservative: when a rule cannot decide, the whole suite is selected.
Nothing here passes or fails anything; `scripts/gate-affected.sh` owns that. Standard
library only, no new runtime or dev dependencies.
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
    "tests/test_c16c2_console.py::test_c16c2_dashboard_renders_approved_shell",
    "tests/test_c16c2_console.py::test_c16c2_run_detail_renders_inspector_and_actions",
    "tests/test_c16c2_console.py::test_c16c2_missing_run_keeps_shell",
    "tests/test_c16c2_console.py::test_c16c2_console_assets_are_served",
)

RULE_FULL = "FULL"
RULE_NONE = "NONE"
RULE_GRAPH = "GRAPH"
RULE_PRESENTATION = "PRESENTATION"

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


def select(repo: Path, base: str) -> Selection:
    """changed paths -> the tests that must run for this diff."""
    changed = changed_paths(repo, base)
    rules = {path: classify(path) for path in changed}

    full_paths = [path for path, rule in rules.items() if rule == RULE_FULL]
    if full_paths:
        return Selection(
            base=base,
            changed=changed,
            tests=[],
            full=True,
            full_reason=full_paths[0],
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
    for path in changed_tests:
        targets[path] = SelectedTest(target=path, via=path, detail="changed test file")

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

    return Selection(
        base=base,
        changed=changed,
        tests=sorted(targets.values(), key=lambda item: item.target),
        ignored=ignored,
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
    lines.append(f"changed files ({len(selection.changed)}):")
    lines.extend(f"  {path}" for path in selection.changed)
    if selection.full:
        lines.append(f"FULL suite required: {selection.full_reason}")
        return "\n".join(lines)
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
        "--explain",
        action="store_true",
        help="print, per selected test, the changed path and import chain (stderr)",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON on stdout")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo = Path(args.repo).resolve()
    try:
        base = args.base or default_base(repo)
        selection = select(repo, base)
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
    if args.explain:
        print(format_explain(selection), file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
