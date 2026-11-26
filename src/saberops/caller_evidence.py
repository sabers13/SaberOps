"""Engine evidence for H43: new production definitions that nothing calls.

The reviewer sees a read-only snapshot of the candidate and cannot search the
repository for callers.  The engine can, deterministically: it parses the
Python definitions the candidate added under ``src/``/``scripts/`` and asks
``git grep -w`` whether anything outside ``tests/`` refers to them.  A move is
still located by its own name, so a moved definition that production still
calls is simply a called definition here; the test-only channel additionally
drops a move whose old location was live production code, because that is a
relocation rather than a new addition.  The bounded result is passed to the
reviewer as engine evidence; the reviewer decides B1/N1 and the finding goes
back to the same worker (ADR-0103).

Dynamic-registration definitions are excluded exactly as in H31: web routes,
HTTP handler methods, MCP tool registrations, CLI commands, pytest fixtures,
validators, properties and other registering decorators are called by a
framework, not by a textual reference.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path, PurePosixPath

from saberops.git import GitManager
from saberops.process import run_process

#: Per-review bounds: the evidence is a pointer for the reviewer, never a dump.
MAX_UNCALLED = 20
MAX_TEST_ONLY = 10

#: Files whose definitions count as production.  Tests are handled by the
#: separate test-only channel; docs and assets are not definitions.
_PRODUCTION_ROOTS: tuple[str, ...] = ("src/", "scripts/")

#: Extensions a caller may live in.  Restricting the grep keeps a prose mention
#: in a Markdown document from masquerading as a production caller.
_REFERENCE_PATHSPECS: tuple[str, ...] = (
    "*.py",
    "*.pyi",
    "*.html",
    "*.js",
    "*.mjs",
    "*.json",
)

#: Decorators that register their definition with a framework, so the
#: framework calls it without a textual reference (H31's exclusion list).
_REGISTERING_DECORATOR_RE = re.compile(
    r"(?:^|\.)(?:app|router|bp|blueprint|api|mcp|server)\."
    r"(?:get|post|put|patch|delete|route|websocket|middleware|on_event|tool)\b"
    r"|(?:^|\.)(?:fixture|command|field_validator|model_validator|root_validator"
    r"|validator|property|cached_property|setter|getter|deleter|overload)\b"
    r"|register\w*|listener|receiver|subscribe",
    re.IGNORECASE,
)

#: Methods a standard-library HTTP handler is called through.
_HTTP_HANDLER_BASE = "BaseHTTPRequestHandler"


@dataclass(frozen=True)
class UncalledDefinition:
    """One new production definition with no reference outside the tests."""

    path: str
    line: int
    qualname: str
    test_references: tuple[str, ...] = ()

    def as_text(self, *, test_only: bool = False) -> str:
        where = f"{self.path}:{self.line} {self.qualname}"
        if test_only:
            shown = ", ".join(self.test_references[:3])
            more = (
                f" and {len(self.test_references) - 3} more"
                if len(self.test_references) > 3
                else ""
            )
            return f"{where} (referenced only from tests: {shown}{more})"
        return f"{where} (no reference outside tests/)"


@dataclass(frozen=True)
class CallerEvidence:
    """Bounded result of the caller scan for one candidate."""

    uncalled: tuple[UncalledDefinition, ...] = ()
    test_only: tuple[UncalledDefinition, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.uncalled and not self.test_only


@dataclass(frozen=True)
class _Definition:
    path: str
    line: int
    qualname: str
    name: str
    is_method: bool
    is_class: bool
    excluded: bool
    inside_new_class: bool = False


@dataclass(frozen=True)
class _BaseIndex:
    """Base definitions of the changed production files, by qualified name."""

    locations: dict[str, tuple[tuple[str, int], ...]]


def _is_production_python(path: str) -> bool:
    return path.endswith(".py") and path.startswith(_PRODUCTION_ROOTS)


def _is_test_path(path: str) -> bool:
    name = PurePosixPath(path).name
    return (
        path.startswith("tests/")
        or name.startswith("test_")
        or name.endswith("_test.py")
    )


def _decorator_text(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> str:
    return " ".join(ast.unparse(item) for item in node.decorator_list)


def _class_bases(node: ast.ClassDef) -> str:
    return " ".join(
        ast.unparse(base)
        for base in node.bases
        if isinstance(base, (ast.Name, ast.Attribute, ast.Subscript))
    )


def _is_http_handler_method(
    node: ast.FunctionDef | ast.AsyncFunctionDef, bases: str
) -> bool:
    if _HTTP_HANDLER_BASE not in bases:
        return False
    return node.name.startswith("do_") or node.name == "log_message"


def _is_excluded(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
    *,
    bases: str = "",
) -> bool:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        if _is_http_handler_method(node, bases):
            return True
    return bool(_REGISTERING_DECORATOR_RE.search(_decorator_text(node)))


def _definitions(source: str, path: str) -> list[_Definition]:
    """Collect module-level and class-level definitions from one file."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    found: list[_Definition] = []

    def walk(
        body: Iterable[ast.stmt],
        prefix: tuple[str, ...],
        bases: str,
    ) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualname = ".".join((*prefix, node.name))
                found.append(
                    _Definition(
                        path=path,
                        line=node.lineno,
                        qualname=qualname,
                        name=node.name,
                        is_method=bool(prefix),
                        is_class=False,
                        excluded=_is_excluded(node, bases=bases),
                    )
                )
            elif isinstance(node, ast.ClassDef):
                qualname = ".".join((*prefix, node.name))
                found.append(
                    _Definition(
                        path=path,
                        line=node.lineno,
                        qualname=qualname,
                        name=node.name,
                        is_method=bool(prefix),
                        is_class=True,
                        excluded=_is_excluded(node),
                    )
                )
                walk(node.body, (*prefix, node.name), _class_bases(node))

    walk(tree.body, (), "")
    return found


def _git_show(worktree: Path, revision: str, path: str) -> str | None:
    result = run_process(
        ["git", "show", f"{revision}:{path}"], cwd=worktree, timeout=10.0
    )
    if not result.passed or "\x00" in result.stdout:
        return None
    return result.stdout


@lru_cache(maxsize=16)
def _base_index(
    worktree: str, base_commit: str, paths: tuple[str, ...]
) -> _BaseIndex:
    """Definition locations for the base versions of the changed production files.

    Only changed files need to be indexed: a definition can only leave a file
    that the candidate changed (modified or deleted), so a move or a same-name
    predecessor is always found there.
    """
    repo = Path(worktree)
    locations: dict[str, list[tuple[str, int]]] = {}
    for path in paths:
        if not _is_production_python(path):
            continue
        source = _git_show(repo, base_commit, path)
        if source is None:
            continue
        for definition in _definitions(source, path):
            locations.setdefault(definition.qualname, []).append(
                (path, definition.line)
            )
    return _BaseIndex(
        {name: tuple(where) for name, where in locations.items()},
    )


#: Pathspecs for the base liveness check: a moved definition is "live" when
#: production code (not tests, not prose) referred to it before the move.
_PRODUCTION_REFERENCE_PATHSPECS: tuple[str, ...] = (
    "src/",
    "scripts/",
    "*.html",
    "*.json",
)


def _references(
    worktree: Path,
    revision: str,
    name: str,
    pathspecs: tuple[str, ...] = _REFERENCE_PATHSPECS,
) -> tuple[tuple[str, int], ...]:
    """Word-boundary reference locations for ``name`` at ``revision``."""
    command = [
        "git",
        "grep",
        "-n",
        "-I",
        "-w",
        "-e",
        name,
        revision,
        "--",
        *pathspecs,
    ]
    result = run_process(command, cwd=worktree, timeout=20.0)
    if not result.passed or not result.stdout.strip():
        return ()
    found: list[tuple[str, int]] = []
    prefix = f"{revision}:"
    for line in result.stdout.splitlines():
        if not line.startswith(prefix):
            continue
        rest = line[len(prefix) :]
        path, _, remainder = rest.partition(":")
        number, _, _text = remainder.partition(":")
        if not path or not number.isdigit():
            continue
        found.append((path, int(number)))
    return tuple(found)


@lru_cache(maxsize=64)
def _is_live_at_base(
    worktree: str,
    base_commit: str,
    qualname: str,
    paths: tuple[str, ...],
) -> bool:
    """True when production code referred to this definition at the base commit.

    A same-named definition that was already dead at the base is not a live move:
    relocating dead code still introduces a definition nothing calls, which is
    what the reviewer has to see.  A live move is not new code and is skipped.
    """
    index = _base_index(worktree, base_commit, paths)
    own = set(index.locations.get(qualname, ()))
    for match in _references(
        Path(worktree),
        base_commit,
        qualname.rsplit(".", 1)[-1],
        _PRODUCTION_REFERENCE_PATHSPECS,
    ):
        if match not in own:
            return True
    return False


def _is_own_definition(
    match: tuple[str, int], definition: _Definition
) -> bool:
    return match == (definition.path, definition.line)


_HUNK_RE = re.compile(
    r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@"
)


def _added_line_ranges(
    worktree: Path, base_commit: str, candidate_commit: str
) -> dict[str, list[tuple[int, int]]]:
    """Line ranges each changed file added, from a zero-context diff."""
    result = run_process(
        [
            "git",
            "diff",
            "--unified=0",
            f"{base_commit}..{candidate_commit}",
            "--",
            "*.py",
        ],
        cwd=worktree,
        timeout=30.0,
    )
    if not result.passed:
        return {}
    ranges: dict[str, list[tuple[int, int]]] = {}
    current = ""
    for line in result.stdout.splitlines():
        if line.startswith("+++ "):
            current = line[6:] if line.startswith("+++ b/") else ""
            continue
        if not current or not line.startswith("@@"):
            continue
        match = _HUNK_RE.match(line)
        if match is None:
            continue
        start = int(match.group("start"))
        count = int(match.group("count") or 1)
        if count:
            ranges.setdefault(current, []).append((start, start + count - 1))
    return ranges


def _in_added_range(line: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start <= line <= end for start, end in ranges)


def _new_definitions(
    worktree: Path,
    base_commit: str,
    candidate_commit: str,
    changed: tuple[str, ...],
) -> list[_Definition]:
    added_ranges = _added_line_ranges(worktree, base_commit, candidate_commit)
    base = _base_index(str(worktree), base_commit, tuple(sorted(changed)))
    definitions: list[_Definition] = []
    for path in changed:
        if not _is_production_python(path):
            continue
        ranges = added_ranges.get(path)
        if not ranges:
            continue
        source = _git_show(worktree, candidate_commit, path)
        if source is None:
            continue
        definitions.extend(
            definition
            for definition in _definitions(source, path)
            if _in_added_range(definition.line, ranges)
        )
    new_classes = {
        item.qualname
        for item in definitions
        if item.is_class and item.qualname not in base.locations
    }
    return [
        replace(
            definition,
            inside_new_class=any(
                part in new_classes
                for part in definition.qualname.split(".")[:-1]
            ),
        )
        for definition in definitions
    ]


def find_uncalled_definitions(
    worktree: Path,
    base_commit: str,
    candidate_commit: str = "HEAD",
) -> CallerEvidence:
    """List new production definitions nothing outside ``tests/`` references."""
    changed = tuple(
        GitManager.get_changed_paths_between(
            worktree, base_commit, candidate_commit
        )
    )
    base = _base_index(str(worktree), base_commit, tuple(sorted(changed)))
    uncalled: list[UncalledDefinition] = []
    test_only: list[UncalledDefinition] = []
    for definition in _new_definitions(
        worktree, base_commit, candidate_commit, changed
    ):
        if definition.excluded or definition.inside_new_class:
            continue
        production_refs = 0
        tests: list[str] = []
        for match in _references(worktree, candidate_commit, definition.name):
            if _is_own_definition(match, definition):
                continue
            if _is_test_path(match[0]):
                tests.append(f"{match[0]}:{match[1]}")
            else:
                production_refs += 1
        if production_refs:
            continue
        if tests and definition.qualname in base.locations and _is_live_at_base(
            str(worktree),
            base_commit,
            definition.qualname,
            tuple(sorted(changed)),
        ):
            # A move whose old location was live production code: only the
            # tests mention the new location, but this is a relocation, not a
            # new test-only definition.
            continue
        item = UncalledDefinition(
            path=definition.path,
            line=definition.line,
            qualname=definition.qualname,
            test_references=tuple(tests),
        )
        if tests:
            test_only.append(item)
        else:
            uncalled.append(item)
    uncalled.sort(key=lambda item: (item.path, item.line))
    test_only.sort(key=lambda item: (item.path, item.line))
    return CallerEvidence(
        uncalled=tuple(uncalled[:MAX_UNCALLED]),
        test_only=tuple(test_only[:MAX_TEST_ONLY]),
    )


def render_caller_evidence(worktree: Path, base_commit: str) -> str:
    """Render the bounded evidence block, or ``""`` when there is nothing new."""
    try:
        evidence = find_uncalled_definitions(worktree, base_commit)
    except Exception as exc:  # noqa: BLE001 - evidence must not block a review
        return (
            "New production definitions with no caller: the engine could not "
            f"run the caller scan ({type(exc).__name__}: {exc})."
        )
    if evidence.empty:
        return "New production definitions with no caller: none."
    parts = [
        "New production definitions with no caller (engine git grep; a live "
        "move or rename is not new):"
    ]
    if evidence.uncalled:
        parts.append(
            "- uncalled: "
            + "; ".join(item.as_text() for item in evidence.uncalled)
        )
    else:
        parts.append("- uncalled: none")
    if evidence.test_only:
        parts.append(
            "- test-only, reported separately: "
            + "; ".join(item.as_text(test_only=True) for item in evidence.test_only)
        )
    return "\n".join(parts)


__all__ = [
    "MAX_TEST_ONLY",
    "MAX_UNCALLED",
    "CallerEvidence",
    "UncalledDefinition",
    "find_uncalled_definitions",
    "render_caller_evidence",
]
