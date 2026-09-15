"""Actual internal dependency and symbol facts, derived from the Python AST.

Nothing here reads metadata.  The graph is the *evidence* against which module
contracts are checked, so it is built only from source text with :mod:`ast`:
no imports are executed, no provider is contacted, and the result is fully
sorted so identical inputs always produce identical output.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

from orchestrator_mvp.devscope.paths import PACKAGE_NAME, PACKAGE_ROOT, iter_files

#: Base classes that mark a class definition as an enumeration.
_ENUM_BASES = frozenset({"Enum", "StrEnum", "IntEnum", "IntFlag", "Flag", "ReprEnum"})


@dataclass(frozen=True)
class Symbol:
    """One top-level declaration, recorded without its body."""

    name: str
    kind: str
    lineno: int

    @property
    def is_public(self) -> bool:
        """Whether the symbol participates in a module's public surface."""
        return not self.name.startswith("_")


@dataclass(frozen=True)
class SourceFile:
    """Structural facts about a single production Python file."""

    path: str
    dotted: str
    is_package: bool
    classes: tuple[Symbol, ...]
    functions: tuple[Symbol, ...]
    enums: tuple[str, ...]
    constants: tuple[str, ...]
    exports: tuple[str, ...]
    imports: tuple[str, ...]
    entry_points: tuple[str, ...]

    @property
    def public_symbols(self) -> tuple[str, ...]:
        """Public class and function names, sorted."""
        names = [s.name for s in (*self.classes, *self.functions) if s.is_public]
        return tuple(sorted(names))


@dataclass(frozen=True)
class SourceGraph:
    """The complete internal dependency graph for the production package."""

    files: dict[str, SourceFile]
    dotted_index: dict[str, str]
    edges: tuple[tuple[str, str], ...]
    dependencies: dict[str, tuple[str, ...]] = field(default_factory=dict)
    dependents: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def paths(self) -> tuple[str, ...]:
        """Every analysed source path, sorted."""
        return tuple(sorted(self.files))

    def fan_in(self, path: str) -> int:
        """Number of files that import ``path`` directly."""
        return len(self.dependents.get(path, ()))

    def fan_out(self, path: str) -> int:
        """Number of internal files ``path`` imports directly."""
        return len(self.dependencies.get(path, ()))

    def reverse_closure(self, seeds: tuple[str, ...]) -> dict[str, int]:
        """Return every transitive consumer of ``seeds`` mapped to its depth.

        Seeds themselves are depth ``0``.  Breadth-first order guarantees the
        recorded depth is the shortest consumer distance, which is what the
        verification planner uses to separate G2 from G3.
        """
        depths: dict[str, int] = {seed: 0 for seed in seeds if seed in self.files}
        frontier = sorted(depths)
        depth = 0
        while frontier:
            depth += 1
            nxt: list[str] = []
            for path in frontier:
                for consumer in self.dependents.get(path, ()):
                    if consumer not in depths:
                        depths[consumer] = depth
                        nxt.append(consumer)
            frontier = sorted(nxt)
        return depths


def _dotted_name(relative_path: str) -> tuple[str, bool]:
    """Map a repository-relative path to its dotted module name."""
    without_suffix = relative_path[: -len(".py")]
    parts = without_suffix.split("/")[1:]  # drop the leading "src"
    is_package = parts[-1] == "__init__"
    if is_package:
        parts = parts[:-1]
    return ".".join(parts), is_package


def _package_of(dotted: str, is_package: bool) -> str:
    """Return the package a module's relative imports resolve against."""
    if is_package:
        return dotted
    head, _, _ = dotted.rpartition(".")
    return head


def _resolve_relative(dotted: str, is_package: bool, level: int, module: str | None) -> str | None:
    """Resolve ``from ..x import y`` to an absolute dotted name."""
    base = _package_of(dotted, is_package)
    for _ in range(level - 1):
        base, _, _ = base.rpartition(".")
        if not base:
            return None
    if not base:
        return None
    return f"{base}.{module}" if module else base


def _is_enum(node: ast.ClassDef) -> bool:
    for base in node.bases:
        if isinstance(base, ast.Name) and base.id in _ENUM_BASES:
            return True
        if isinstance(base, ast.Attribute) and base.attr in _ENUM_BASES:
            return True
    return False


def _string_tuple(node: ast.AST) -> tuple[str, ...]:
    if not isinstance(node, ast.List | ast.Tuple):
        return ()
    return tuple(
        element.value
        for element in node.elts
        if isinstance(element, ast.Constant) and isinstance(element.value, str)
    )


def _analyse(relative_path: str, text: str) -> SourceFile:
    """Extract structural facts from one file's syntax tree."""
    dotted, is_package = _dotted_name(relative_path)
    tree = ast.parse(text, filename=relative_path)

    classes: list[Symbol] = []
    functions: list[Symbol] = []
    enums: list[str] = []
    constants: list[str] = []
    exports: tuple[str, ...] = ()

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            classes.append(Symbol(node.name, "class", node.lineno))
            if _is_enum(node):
                enums.append(node.name)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            functions.append(Symbol(node.name, "function", node.lineno))
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    if target.id == "__all__":
                        exports = _string_tuple(node.value)
                    elif target.id.isupper():
                        constants.append(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == "__all__" and node.value is not None:
                exports = _string_tuple(node.value)
            elif node.target.id.isupper():
                constants.append(node.target.id)

    imports: set[str] = set()
    for imported in ast.walk(tree):
        if isinstance(imported, ast.Import):
            for alias in imported.names:
                if alias.name == PACKAGE_NAME or alias.name.startswith(f"{PACKAGE_NAME}."):
                    imports.add(alias.name)
        elif isinstance(imported, ast.ImportFrom):
            resolved: str | None
            if imported.level:
                resolved = _resolve_relative(
                    dotted, is_package, imported.level, imported.module
                )
            elif imported.module and (
                imported.module == PACKAGE_NAME
                or imported.module.startswith(f"{PACKAGE_NAME}.")
            ):
                resolved = imported.module
            else:
                resolved = None
            if resolved is None:
                continue
            imports.add(resolved)
            # ``from pkg import submodule`` also imports the submodule itself.
            for alias in imported.names:
                imports.add(f"{resolved}.{alias.name}")

    entry_points: list[str] = []
    if relative_path.endswith("__main__.py"):
        entry_points.append(dotted)
    if any(symbol.name == "main" for symbol in functions):
        entry_points.append(f"{dotted}:main")

    return SourceFile(
        path=relative_path,
        dotted=dotted,
        is_package=is_package,
        classes=tuple(sorted(classes, key=lambda s: (s.name, s.lineno))),
        functions=tuple(sorted(functions, key=lambda s: (s.name, s.lineno))),
        enums=tuple(sorted(enums)),
        constants=tuple(sorted(set(constants))),
        exports=tuple(sorted(exports)),
        imports=tuple(sorted(imports)),
        entry_points=tuple(sorted(set(entry_points))),
    )


def build_source_graph(root: Path) -> SourceGraph:
    """Build the internal dependency graph for the production package."""
    files: dict[str, SourceFile] = {}
    for relative in iter_files(root, PACKAGE_ROOT, suffix=".py"):
        analysed = _analyse(relative, (root / relative).read_text(encoding="utf-8"))
        files[relative] = analysed

    dotted_index = {source.dotted: source.path for source in files.values()}

    edge_set: set[tuple[str, str]] = set()
    for source in files.values():
        for imported in source.imports:
            target = dotted_index.get(imported)
            if target is None or target == source.path:
                continue
            edge_set.add((source.path, target))

    edges = tuple(sorted(edge_set))
    dependencies: dict[str, tuple[str, ...]] = {}
    dependents: dict[str, tuple[str, ...]] = {}
    for path in files:
        dependencies[path] = tuple(sorted(dst for src, dst in edges if src == path))
        dependents[path] = tuple(sorted(src for src, dst in edges if dst == path))

    return SourceGraph(
        files=dict(sorted(files.items())),
        dotted_index=dict(sorted(dotted_index.items())),
        edges=edges,
        dependencies=dependencies,
        dependents=dependents,
    )
