"""Deterministic Python repository analysis and conservative module inference.

The analyzer reads files, it never imports or executes them.  Evidence is
restricted to cheap, deterministic sources: ``pyproject.toml`` metadata,
source layouts, package directories, ``__init__.py`` files and AST imports.
No network, no model, no arbitrary project code.

Module inference follows one documented, tested rule set based on package
boundaries plus imports.  It never treats every file as its own module, never
collapses an obviously modular repository into one giant module when stable
package boundaries exist, and caps pathological fan-out by collapsing to
coarser (safer) module boundaries.
"""

from __future__ import annotations

import ast
import tomllib
from dataclasses import dataclass
from pathlib import Path

from orchestrator_mvp.devscope.paths import iter_files

#: Above this many inferred modules the inference collapses to coarser
#: boundaries rather than producing an unusable micro-module swarm.
MAX_INFERRED_MODULES = 16


@dataclass(frozen=True)
class PythonAnalysis:
    """Raw deterministic facts about one Python repository."""

    files: tuple[str, ...]
    production_files: tuple[str, ...]
    test_files: tuple[str, ...]
    imports: dict[str, tuple[str, ...]]
    dotted_index: dict[str, str]
    entry_points: tuple[str, ...]
    files_scanned: int
    python_files_parsed: int
    parse_failures: tuple[str, ...] = ()
    package_roots: tuple[tuple[str, str], ...] = ()


def is_test_path(relative_path: str) -> bool:
    """Return whether a repository-relative path looks like test coverage."""
    parts = relative_path.split("/")
    name = parts[-1]
    return (
        any(part in {"tests", "test", "testing"} for part in parts[:-1])
        or name.startswith("test_")
        or name.endswith("_test.py")
    )


def _package_roots_from_pyproject(root: Path) -> list[tuple[str, str]]:
    """Read cheap package declarations from ``pyproject.toml`` when present.

    A malformed or exotic build file is not authoritative architecture; it is
    simply ignored so the heuristic detector decides instead.
    """
    path = root / "pyproject.toml"
    if not path.is_file():
        return []
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError):
        return []
    roots: list[tuple[str, str]] = []
    poetry = data.get("tool", {}).get("poetry", {}).get("packages", [])
    if isinstance(poetry, list):
        for item in poetry:
            if isinstance(item, dict) and isinstance(item.get("include"), str):
                include = item["include"].strip("/")
                roots.append((include, include.rsplit("/", 1)[-1]))
    find = data.get("tool", {}).get("setuptools", {}).get("packages", {}).get("find", {})
    if isinstance(find, dict):
        where = find.get("where", ["."])
        names = find.get("include") or find.get("names") or []
        if isinstance(where, list) and isinstance(names, list):
            for base in where:
                for name in names:
                    if isinstance(name, str) and not name.endswith("*"):
                        prefix = name.replace(".", "/")
                        if base and base != ".":
                            prefix = f"{str(base).strip('/')}/{prefix}"
                        roots.append((prefix, name.rsplit(".", 1)[-1]))
    return roots


def _detect_package_roots(root: Path, files: tuple[str, ...]) -> list[tuple[str, str]]:
    """Return ``(directory_prefix, package_name)`` pairs, prefix-sorted.

    Falls back to a structural heuristic: directories at the repository root
    or directly under ``src/`` that contain an ``__init__.py``.
    """
    declared = _package_roots_from_pyproject(root)
    valid = [
        (prefix, name)
        for prefix, name in declared
        if (root / prefix / "__init__.py").is_file()
        or any(file.startswith(f"{prefix}/") for file in files)
    ]
    if valid:
        return sorted(set(valid))

    detected: set[tuple[str, str]] = set()
    for file in files:
        parts = file.split("/")
        if len(parts) < 2:
            continue
        if parts[-1] != "__init__.py":
            continue
        if len(parts) == 2:
            detected.add((parts[0], parts[0]))
        elif len(parts) == 3 and parts[0] == "src":
            detected.add((f"src/{parts[1]}", parts[1]))
    return sorted(detected)


def _dotted_for_file(
    relative_path: str,
    package_roots: tuple[tuple[str, str], ...],
) -> str | None:
    """Map a repository-relative file to its dotted module name."""
    for prefix, name in package_roots:
        if relative_path.startswith(f"{prefix}/"):
            without = relative_path[len(prefix) + 1 :]
            parts = without[: -len(".py")].split("/")
            if parts[-1] == "__init__":
                parts = parts[:-1]
            return ".".join([name, *parts]) if parts else name
    if relative_path.endswith(".py") and "/" not in relative_path:
        return relative_path[: -len(".py")]
    return None


def _package_of(dotted: str, is_package: bool) -> str:
    """Return the dotted package a file's relative imports resolve against."""
    if is_package:
        return dotted
    head, _, _ = dotted.rpartition(".")
    return head


def _resolve_relative(
    dotted: str, is_package: bool, level: int, module: str | None
) -> str | None:
    """Resolve ``from ..x import y`` to an absolute dotted name."""
    base = _package_of(dotted, is_package)
    for _ in range(level - 1):
        base, _, _ = base.rpartition(".")
        if not base:
            return None
    if not base:
        return None
    return f"{base}.{module}" if module else base


def analyze_python(root: Path) -> PythonAnalysis:
    """Analyse every Python file beneath ``root`` (read-only, deterministic)."""
    files = tuple(file for file in iter_files(root, ".") if file.endswith(".py"))
    files_scanned = len(iter_files(root, "."))
    package_roots = tuple(_detect_package_roots(root, files))
    dotted_index: dict[str, str] = {}
    for file in files:
        dotted = _dotted_for_file(file, package_roots)
        if dotted is not None:
            dotted_index.setdefault(dotted, file)
    top_names = {
        dotted.split(".")[0]
        for dotted in dotted_index
    }

    imports: dict[str, tuple[str, ...]] = {}
    entry_points: list[str] = []
    parse_failures: list[str] = []
    parsed = 0
    for file in files:
        dotted = _dotted_for_file(file, package_roots)
        is_package = file.endswith("/__init__.py")
        collected: set[str] = set()
        try:
            tree = ast.parse((root / file).read_text(encoding="utf-8"), filename=file)
        except (SyntaxError, OSError, UnicodeDecodeError):
            parse_failures.append(file)
            imports[file] = ()
            continue
        parsed += 1
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in top_names:
                        collected.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                resolved: str | None
                if node.level:
                    if dotted is None:
                        resolved = None
                    else:
                        resolved = _resolve_relative(
                            dotted, is_package, node.level, node.module
                        )
                elif node.module and node.module.split(".")[0] in top_names:
                    resolved = node.module
                else:
                    resolved = None
                if resolved is None:
                    continue
                if resolved in dotted_index or resolved in top_names:
                    collected.add(resolved)
                # ``from pkg import submodule`` also imports the submodule.
                for alias in node.names:
                    child = f"{resolved}.{alias.name}"
                    if child in dotted_index:
                        collected.add(child)
        imports[file] = tuple(sorted(collected))
        if file.endswith("__main__.py"):
            entry_points.append(dotted or file)
        if any(
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == "main"
            for node in tree.body
        ):
            entry_points.append(f"{dotted or file}:main")

    production = tuple(file for file in files if not is_test_path(file))
    tests = tuple(file for file in files if is_test_path(file))
    return PythonAnalysis(
        files=files,
        production_files=production,
        test_files=tests,
        imports=imports,
        dotted_index=dotted_index,
        entry_points=tuple(sorted(set(entry_points))),
        files_scanned=files_scanned,
        python_files_parsed=parsed,
        parse_failures=tuple(sorted(parse_failures)),
        package_roots=package_roots,
    )


# ---------------------------------------------------------------------------
# Conservative module inference from package boundaries plus imports.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InferredLayout:
    """Deterministic inferred module layout for one Python repository."""

    modules: tuple[tuple[str, str, tuple[str, ...]], ...]  # (id, directory, files)
    unowned_files: tuple[str, ...]
    test_ownership: dict[str, str]
    dependency_evidence: tuple[tuple[str, str, str], ...]  # (src_mod, dst_mod, file)
    collapsed: bool


def _selected_dirs(
    prod_files: tuple[str, ...], pkg_root_dirs: tuple[str, ...]
) -> set[str]:
    """Choose which directories become modules, top-down, deterministically.

    A directory is selected when it carries real content (two or more own
    files), or is a leaf, or is a package root with content and selected
    children.  ``__init__.py`` files never count toward promotion.
    """
    own: dict[str, list[str]] = {}
    all_dirs: set[str] = {""}
    for file in prod_files:
        directory = file.rsplit("/", 1)[0] if "/" in file else ""
        all_dirs.add(directory)
        if file.rsplit("/", 1)[-1] != "__init__.py":
            own.setdefault(directory, []).append(file)

    children: dict[str, set[str]] = {}
    for directory in sorted(all_dirs):
        if directory:
            parent = directory.rsplit("/", 1)[0] if "/" in directory else ""
            children.setdefault(parent, set()).add(directory)

    selected: set[str] = set()

    def visit(directory: str, is_pkg_root: bool) -> bool:
        own_count = len(own.get(directory, ()))
        kids = sorted(children.get(directory, ()))
        selected_children = [kid for kid in kids if visit(kid, False)]
        is_selected = (
            own_count >= 2
            or (own_count >= 1 and not kids)
            or (own_count >= 1 and is_pkg_root)
            or (own_count >= 1 and bool(selected_children))
        )
        if is_selected:
            selected.add(directory)
            return True
        return False

    roots = sorted(set(pkg_root_dirs) | {d for d in all_dirs if "/" not in d})
    for start in roots:
        visit(start, start in pkg_root_dirs)
    return selected


def _nearest_owner(file: str, selected: set[str]) -> str | None:
    """Return the nearest selected ancestor directory of ``file``."""
    directory = file.rsplit("/", 1)[0] if "/" in file else ""
    while True:
        if directory in selected:
            return directory
        if not directory:
            return None
        directory = directory.rsplit("/", 1)[0] if "/" in directory else ""


def infer_module_layout(analysis: PythonAnalysis) -> InferredLayout:
    """Infer stable logical modules from package boundaries plus imports.

    Returns module ``(id, directory, files)`` triples plus deterministic
    unowned files, test ownership and cross-module dependency evidence.
    Deterministic: identical analysis input always yields the identical
    layout.
    """
    prod = analysis.production_files
    pkg_root_dirs = tuple(sorted({prefix for prefix, _ in analysis.package_roots}))
    selected = _selected_dirs(prod, pkg_root_dirs)

    def module_id_for(directory: str) -> str:
        name = directory.rsplit("/", 1)[-1] if directory else "repository"
        if directory.startswith("src/"):
            name = directory[len("src/") :].rsplit("/", 1)[-1]
        return name

    collapsed = False
    modules = tuple(sorted(selected))
    if len(modules) > MAX_INFERRED_MODULES:
        # Collapse to the coarser top-level package boundaries (or one
        # repository-wide module) rather than emit a micro-module swarm.
        collapsed = True
        if pkg_root_dirs:
            modules = tuple(
                sorted(
                    root_dir
                    for root_dir in pkg_root_dirs
                    if any(file.startswith(f"{root_dir}/") for file in prod)
                )
            )
        else:
            modules = ("",)
        if len(modules) > MAX_INFERRED_MODULES:
            modules = ("",)
        selected = set(modules)

    file_to_dir: dict[str, str] = {}
    for file in prod:
        owner_dir = _nearest_owner(file, selected)
        if owner_dir is None:
            continue
        file_to_dir[file] = owner_dir
    unowned = tuple(sorted(file for file in prod if file not in file_to_dir))

    used_ids: set[str] = set()
    modules_out: list[tuple[str, str, tuple[str, ...]]] = []
    for directory in modules:
        files = tuple(sorted(f for f, d in file_to_dir.items() if d == directory))
        if not files:
            continue
        module_id = module_id_for(directory)
        if module_id in used_ids:
            module_id = directory.replace("/", "_") or "repository"
        used_ids.add(module_id)
        modules_out.append((module_id, directory, files))

    dir_to_id = {directory: module_id for module_id, directory, _ in modules_out}
    owner_of_file = {file: dir_to_id[ddd] for file, ddd in file_to_dir.items()}

    # Dependency evidence: file-level imports collapsed into module edges.
    evidence: set[tuple[str, str, str]] = set()
    for file in sorted(owner_of_file):
        src_module = owner_of_file[file]
        for dotted in analysis.imports.get(file, ()):
            target_file = _resolve_import_target(dotted, analysis.dotted_index)
            if target_file is None:
                continue
            dst_module = owner_of_file.get(target_file)
            if dst_module is None or dst_module == src_module:
                continue
            evidence.add((src_module, dst_module, file))

    # Test ownership: a test file belongs to the module(s) it imports.
    test_ownership: dict[str, str] = {}
    for test in analysis.test_files:
        for dotted in analysis.imports.get(test, ()):
            target_file = _resolve_import_target(dotted, analysis.dotted_index)
            if target_file is None:
                continue
            owner_id = owner_of_file.get(target_file)
            if owner_id is not None:
                test_ownership[test] = owner_id
                break

    return InferredLayout(
        modules=tuple(modules_out),
        unowned_files=unowned,
        test_ownership=test_ownership,
        dependency_evidence=tuple(sorted(evidence)),
        collapsed=collapsed,
    )


def _resolve_import_target(
    dotted: str, dotted_index: dict[str, str]
) -> str | None:
    """Resolve one imported dotted name to a repo file, walking up parents."""
    candidate = dotted
    while candidate:
        if candidate in dotted_index:
            return dotted_index[candidate]
        candidate, _, _ = candidate.rpartition(".")
    return None
