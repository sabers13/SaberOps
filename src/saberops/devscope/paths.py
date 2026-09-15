"""Repository root discovery and deterministic glob matching for devscope.

Devscope is dev-time repository intelligence.  It never imports orchestrator
runtime code, so it cannot borrow the runtime's path helpers; the two small
primitives it needs live here instead.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

#: Directory names that never contain reviewable production source.
IGNORED_DIR_NAMES: frozenset[str] = frozenset(
    {
        "__pycache__",
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "build",
        "dist",
        "node_modules",
    }
)

#: Repository-relative prefix holding the importable production package.
PACKAGE_ROOT = "src/saberops"

#: Dotted name of the importable production package.
PACKAGE_NAME = "saberops"

#: Repository-relative directory holding module contracts.
MODULES_DIR = "architecture/modules"

#: Repository-relative root agent instruction file.
ROOT_AGENTS_DOC = "AGENTS.md"


def repo_root() -> Path:
    """Return the repository root inferred from this file's location.

    ``devscope`` ships inside ``src/saberops``, so the root is three
    parents up.  Callers that analyse a different checkout pass an explicit
    root instead; nothing in devscope depends on the process CWD.
    """
    return Path(__file__).resolve().parents[3]


def is_ignored(relative_path: str) -> bool:
    """Return whether a repository-relative path lies in an ignored directory."""
    parts = relative_path.split("/")
    if any(part in IGNORED_DIR_NAMES for part in parts):
        return True
    return any(part.endswith(".egg-info") for part in parts)


def iter_files(root: Path, subdir: str, suffix: str | None = None) -> tuple[str, ...]:
    """Return sorted repository-relative paths beneath ``subdir``.

    Ignored directories are skipped.  The result is sorted so that every
    downstream artifact (repository map, context plan, affected plan) is
    byte-stable for identical inputs.
    """
    base = root / subdir
    if not base.is_dir():
        return ()
    found: list[str] = []
    for path in base.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if is_ignored(relative):
            continue
        if suffix is not None and not relative.endswith(suffix):
            continue
        found.append(relative)
    return tuple(sorted(found))


@lru_cache(maxsize=512)
def _compiled(pattern: str) -> re.Pattern[str]:
    """Compile a POSIX-style path glob into an anchored regular expression.

    ``**`` crosses directory separators, ``*`` and ``?`` do not.  A pattern
    with no wildcard that names a directory also matches everything beneath
    it, which keeps ownership declarations such as ``templates`` readable.
    """
    out: list[str] = []
    index = 0
    length = len(pattern)
    while index < length:
        char = pattern[index]
        if pattern.startswith("**/", index):
            out.append("(?:[^/]+/)*")
            index += 3
        elif pattern.startswith("**", index):
            out.append(".*")
            index += 2
        elif char == "*":
            out.append("[^/]*")
            index += 1
        elif char == "?":
            out.append("[^/]")
            index += 1
        else:
            out.append(re.escape(char))
            index += 1
    body = "".join(out)
    return re.compile(f"(?:{body})(?:/.*)?\\Z")


def match_path(pattern: str, relative_path: str) -> bool:
    """Return whether ``relative_path`` matches the glob ``pattern``."""
    return _compiled(pattern).fullmatch(relative_path) is not None


def match_any(patterns: tuple[str, ...], relative_path: str) -> bool:
    """Return whether ``relative_path`` matches at least one pattern."""
    return any(match_path(pattern, relative_path) for pattern in patterns)
