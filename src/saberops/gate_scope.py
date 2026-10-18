"""Deterministic write-scope and test-retirement checks (ADR-0004 D3).

Two pre-G1 checks the engine raises itself, both pure and fail-closed:

* **Write scope** -- every changed path must be inside the lane's ``Touches``
  and outside every ``must-not-touch`` path.  A violation is a review finding
  produced by the engine, never a model opinion.
* **Test retirement** -- a test that disappeared or whose body changed must be
  authorized by the lane's retirement table.  Unauthorized deletion or a
  weakened (edited) test blocks: tests are the contract.

Path matching is deliberately conservative: a ``touch``/``must-not-touch``
entry matches itself and everything under it when written as a directory
(either ``dir/`` or a bare ``dir`` that exists as a directory in the tree).
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum


class GateScopeError(ValueError):
    """Fail-closed error for malformed scope input."""


class RetirementAction(StrEnum):
    """The only two authorized retirement actions."""

    REMOVED = "removed"
    REWRITTEN = "rewritten"


@dataclass(frozen=True)
class RetirementEntry:
    """One authorized test retirement: ``node_id`` plus the allowed action."""

    node_id: str
    action: RetirementAction | str

    def __post_init__(self) -> None:
        if not self.node_id.strip():
            raise GateScopeError("retirement entry requires a node id")
        try:
            object.__setattr__(
                self, "action", RetirementAction(self.action)
            )
        except ValueError as exc:
            raise GateScopeError(
                f"retirement action {self.action!r} is not one of "
                f"{', '.join(a.value for a in RetirementAction)}"
            ) from exc


@dataclass(frozen=True)
class ScopeCheck:
    """Result of one deterministic write-scope check."""

    allowed: bool
    out_of_scope: tuple[str, ...]
    must_not_touch: tuple[str, ...]
    reason: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "out_of_scope": list(self.out_of_scope),
            "must_not_touch": list(self.must_not_touch),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class RetirementCheck:
    """Result of one deterministic test-retirement check."""

    allowed: bool
    removed: tuple[str, ...]
    rewritten: tuple[str, ...]
    unauthorized_removed: tuple[str, ...]
    unauthorized_rewritten: tuple[str, ...]
    reason: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "removed": list(self.removed),
            "rewritten": list(self.rewritten),
            "unauthorized_removed": list(self.unauthorized_removed),
            "unauthorized_rewritten": list(self.unauthorized_rewritten),
            "reason": self.reason,
        }


def normalize_path(path: str) -> str:
    """Return a canonical repo-relative POSIX path or raise."""
    text = path.strip().replace("\\", "/")
    if not text:
        raise GateScopeError("empty path in scope declaration")
    if text.startswith("/") or ":" in text.split("/")[0]:
        raise GateScopeError(f"scope paths must be repo-relative: {path!r}")
    parts: list[str] = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise GateScopeError(f"scope path escapes the repository: {path!r}")
        parts.append(part)
    if not parts:
        raise GateScopeError(f"scope path resolves to nothing: {path!r}")
    return "/".join(parts)


def _matches(path: str, pattern: str) -> bool:
    """True when ``path`` is ``pattern`` or lives under the ``pattern`` dir."""
    root = pattern.rstrip("/")
    return path == root or path.startswith(root + "/")


def _patterns(entries: Iterable[str]) -> tuple[str, ...]:
    return tuple(normalize_path(entry) for entry in entries)


def check_write_scope(
    changed: Sequence[str],
    touches: Sequence[str],
    must_not_touch: Sequence[str] = (),
) -> ScopeCheck:
    """Check changed files against a lane's Touches / must-not-touch lists.

    An out-of-scope path OR any must-not-touch hit fails closed.  The returned
    tuples are sorted so the finding is deterministic.
    """
    allowed_patterns = _patterns(touches)
    forbidden_patterns = _patterns(must_not_touch)
    paths = sorted({normalize_path(path) for path in changed})
    out_of_scope = tuple(
        path for path in paths if not any(_matches(path, pat) for pat in allowed_patterns)
    )
    forbidden = tuple(
        path for path in paths if any(_matches(path, pat) for pat in forbidden_patterns)
    )
    if out_of_scope or forbidden:
        reasons: list[str] = []
        if out_of_scope:
            reasons.append("outside Touches: " + ", ".join(out_of_scope))
        if forbidden:
            reasons.append("must-not-touch: " + ", ".join(forbidden))
        return ScopeCheck(
            allowed=False,
            out_of_scope=out_of_scope,
            must_not_touch=forbidden,
            reason="; ".join(reasons),
        )
    return ScopeCheck(allowed=True, out_of_scope=(), must_not_touch=())


def collect_tests(source: str, path: str) -> dict[str, str]:
    """Map ``path::qualname`` node ids to a normalized body digest.

    Module-level ``test_*`` functions and ``test_*`` methods of classes are
    collected.  The digest is ``ast.dump`` of the function node, so a purely
    cosmetic edit still counts as a rewrite -- the retirement table, not a
    heuristic, is what authorizes a change to an existing test.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise GateScopeError(f"cannot parse test source {path}: {exc}") from exc
    found: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
            "test_"
        ):
            found[f"{path}::{node.name}"] = ast.dump(node)
        elif isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(
                    child, (ast.FunctionDef, ast.AsyncFunctionDef)
                ) and child.name.startswith("test_"):
                    found[f"{path}::{node.name}::{child.name}"] = ast.dump(child)
    return found


def collect_tests_from_sources(sources: Mapping[str, str]) -> dict[str, str]:
    """Collect tests from a ``path -> source`` mapping."""
    collected: dict[str, str] = {}
    for path in sorted(sources):
        collected.update(collect_tests(sources[path], normalize_path(path)))
    return collected


def check_test_retirement(
    base_sources: Mapping[str, str],
    head_sources: Mapping[str, str],
    authorized: Sequence[RetirementEntry] = (),
) -> RetirementCheck:
    """Refuse unauthorized test deletions and unauthorized rewrites.

    * a node id present at base but absent at head is *removed*;
    * a node id present in both with a different body digest is *rewritten*;
    * newly added tests are always allowed.

    ``authorized`` maps node ids to the permitted action; a node removed but
    only authorized as ``rewritten`` (or vice versa) is unauthorized.
    """
    base = collect_tests_from_sources(base_sources)
    head = collect_tests_from_sources(head_sources)
    permissions: dict[str, RetirementAction] = {}
    for entry in authorized:
        resolved = entry if isinstance(entry, RetirementEntry) else None
        if resolved is None:  # pragma: no cover - defensive
            raise GateScopeError("retirement entries must be RetirementEntry instances")
        action = resolved.action
        if not isinstance(action, RetirementAction):  # pragma: no cover - defensive
            raise GateScopeError(f"retirement entry {resolved.node_id!r} has no action")
        permissions[resolved.node_id] = action

    removed = tuple(sorted(set(base) - set(head)))
    rewritten = tuple(sorted(node for node in set(base) & set(head) if base[node] != head[node]))
    unauthorized_removed = tuple(
        node for node in removed if permissions.get(node) is not RetirementAction.REMOVED
    )
    unauthorized_rewritten = tuple(
        node for node in rewritten if permissions.get(node) is not RetirementAction.REWRITTEN
    )
    if unauthorized_removed or unauthorized_rewritten:
        reasons: list[str] = []
        if unauthorized_removed:
            reasons.append("unauthorized test removal: " + ", ".join(unauthorized_removed))
        if unauthorized_rewritten:
            reasons.append(
                "unauthorized test rewrite (possible weakening): "
                + ", ".join(unauthorized_rewritten)
            )
        return RetirementCheck(
            allowed=False,
            removed=removed,
            rewritten=rewritten,
            unauthorized_removed=unauthorized_removed,
            unauthorized_rewritten=unauthorized_rewritten,
            reason="; ".join(reasons),
        )
    return RetirementCheck(
        allowed=True,
        removed=removed,
        rewritten=rewritten,
        unauthorized_removed=(),
        unauthorized_rewritten=(),
    )


def parse_retirement_table(rows: Sequence[Mapping[str, object]]) -> tuple[RetirementEntry, ...]:
    """Parse a prompt's "Tests retired / rewritten" table into entries.

    Each row must carry ``node_id`` and ``action``; unknown keys are ignored so
    the table keeps human-facing columns (reason, where).
    """
    entries: list[RetirementEntry] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise GateScopeError(f"retirement table row #{index} must be a mapping")
        node_id = row.get("node_id")
        action = row.get("action")
        if not isinstance(node_id, str) or not node_id.strip():
            raise GateScopeError(f"retirement table row #{index} needs a node_id")
        if not isinstance(action, str):
            raise GateScopeError(f"retirement table row #{index} needs an action")
        entries.append(RetirementEntry(node_id=node_id.strip(), action=action))
    return tuple(entries)


def git_changed_files(repo_path: str, base: str, head: str = "HEAD") -> tuple[str, ...]:
    """Committed changed paths for ``base..head`` via ``git diff --name-only``."""
    import subprocess

    completed = subprocess.run(
        ["git", "diff", "--name-only", f"{base}..{head}"],
        cwd=repo_path,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise GateScopeError(
            f"git diff {base}..{head} failed in {repo_path}: {completed.stderr.strip()}"
        )
    return tuple(sorted(line.strip() for line in completed.stdout.splitlines() if line.strip()))


__all__ = [
    "GateScopeError",
    "RetirementAction",
    "RetirementCheck",
    "RetirementEntry",
    "ScopeCheck",
    "check_test_retirement",
    "check_write_scope",
    "collect_tests",
    "collect_tests_from_sources",
    "git_changed_files",
    "normalize_path",
    "parse_retirement_table",
]
