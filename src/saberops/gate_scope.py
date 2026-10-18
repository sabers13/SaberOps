"""Deterministic write-scope, test-retirement and gate-protection checks (ADR-0004 D3).

Three pre-G1 checks the engine raises itself, all pure and fail-closed:

* **Write scope** -- every changed path must be inside the lane's ``Touches``
  and outside every ``must-not-touch`` path.  A violation is a review finding
  produced by the engine, never a model opinion.
* **Test retirement** -- a test that disappeared or whose body changed must be
  authorized by the lane's retirement table.  Unauthorized deletion or a
  weakened (edited) test blocks: tests are the contract.
* **Gate protection** -- the files that *define* a gate (gate scripts, every
  ``conftest.py``, the pytest/ruff/mypy configuration in ``pyproject.toml`` and
  the CI workflow) may never be changed by the candidate whose gate is being
  run.  A candidate that edits them could change its own verdict, so the check
  fails closed and raises an owner question.  The engine loads the same
  definitions from the base SHA and compares content, which also catches
  uncommitted edits that a branch diff does not see.

Path matching is deliberately conservative: a ``touch``/``must-not-touch``
entry matches itself and everything under it when written as a directory
(either ``dir/`` or a bare ``dir`` that exists as a directory in the tree).
"""

from __future__ import annotations

import ast
import hashlib
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Final


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


# ---------------------------------------------------------------------------
# Gate protection: the definitions that decide a gate's verdict
# ---------------------------------------------------------------------------

#: Stable reason code for a candidate that mutated a protected gate definition.
GATE_PROTECTED_PATH_MUTATED: Final[str] = "GATE_PROTECTED_PATH_MUTATED"

#: The gate definitions the roadmap names explicitly (GT-1).  Matching is done
#: by :data:`PROTECTED_GATE_DIRECTORIES`, which contains this whole directory;
#: the tuple is kept as the named policy list so the intent stays readable.
PROTECTED_GATE_SCRIPT_PATHS: Final[tuple[str, ...]] = (
    "scripts/gate-full.sh",
    "scripts/gate-ui-fast.sh",
    "scripts/gate-affected.sh",
    "scripts/gate_tools.py",
    "scripts/build_test_map.py",
    "scripts/select_tests.py",
    "scripts/test_hygiene.py",
)

#: Whole directories that only ever hold developer/CI gate tooling.  The
#: directory rule is what closes the gap the named list leaves: a candidate
#: that weakened ``scripts/lane-check.sh`` (the G1 executor itself) or added a
#: new gate helper would otherwise change its own verdict.
PROTECTED_GATE_DIRECTORIES: Final[tuple[str, ...]] = ("scripts",)

#: Gate configuration files: the pytest/ruff/mypy sections of ``pyproject.toml``
#: and the pinned CI workflow.  ``pyproject.toml`` is protected as a whole file
#: because the dev-dependency pins decide which tools the gate runs, so a
#: section-level rule would be trivially bypassed.
PROTECTED_GATE_CONFIG_PATHS: Final[tuple[str, ...]] = (
    "pyproject.toml",
    ".github/workflows/ci.yml",
)

#: Any file with this basename is test infrastructure: pytest loads every
#: ``conftest.py`` on the path to a collected test, root and subdirectories alike.
PROTECTED_GATE_BASENAMES: Final[tuple[str, ...]] = ("conftest.py",)

#: Gate-relevant ``pyproject.toml`` tables, reported as evidence only.
_PYPROJECT_GATE_TOOL_TABLES: Final[tuple[str, ...]] = ("mypy", "pytest.ini_options", "ruff")
GATE_CONFIG_SECTIONS: Final[tuple[str, ...]] = tuple(
    f"tool.{table}" for table in _PYPROJECT_GATE_TOOL_TABLES
)


def is_protected_gate_path(path: str) -> bool:
    """True when ``path`` is a gate definition that must load from the base SHA."""
    candidate = normalize_path(path)
    if any(_matches(candidate, directory) for directory in PROTECTED_GATE_DIRECTORIES):
        return True
    if candidate in PROTECTED_GATE_CONFIG_PATHS:
        return True
    return PurePosixPath(candidate).name in PROTECTED_GATE_BASENAMES


@dataclass(frozen=True)
class GateProtectionResult:
    """Outcome of the gate-protection check for one candidate.

    ``is_protected`` is the verdict the engine acts on: when it is True the
    gate does not run and the change becomes an owner question.  There is no
    self-authorization path -- a lane that *declared* the edit in its
    ``Touches`` is still blocked; ``declared_paths`` only records that the
    declaration existed, so the owner sees who asked for the change.
    """

    is_protected: bool
    tampered_paths: tuple[str, ...] = ()
    declared_paths: tuple[str, ...] = ()
    reason_code: str = ""
    reason: str = ""
    owner_question: str = ""
    base_sha: str = ""
    base_verified: bool = False
    detected_by: tuple[str, ...] = ()
    digests: tuple[tuple[str, str], ...] = ()

    @property
    def allowed(self) -> bool:
        """True only when the candidate left every gate definition untouched."""
        return not self.is_protected

    def as_dict(self) -> dict[str, object]:
        return {
            "is_protected": self.is_protected,
            "allowed": self.allowed,
            "tampered_paths": list(self.tampered_paths),
            "declared_paths": list(self.declared_paths),
            "reason_code": self.reason_code,
            "reason": self.reason,
            "owner_question": self.owner_question,
            "base_sha": self.base_sha,
            "base_verified": self.base_verified,
            "detected_by": list(self.detected_by),
            "digests": [[path, digest] for path, digest in self.digests],
        }


def check_gate_protection(
    changed_paths: Iterable[str],
    touches: Iterable[str] | None = None,
) -> GateProtectionResult:
    """Refuse a candidate diff that edits the gate definitions it runs under.

    ``touches`` is the lane's declared ``Touches`` list and is *evidence only*:
    it tells the owner whether the change was declared, and never authorizes
    it.  Authorization requires the owner to accept the change into the base
    SHA, after which a new gate compares against the new base.
    """
    paths = sorted({normalize_path(path) for path in changed_paths})
    tampered = tuple(path for path in paths if is_protected_gate_path(path))
    if not tampered:
        return GateProtectionResult(is_protected=False)
    declared = {normalize_path(entry) for entry in (touches or ())}
    declared_paths = tuple(path for path in tampered if path in declared)
    reason = (
        f"{GATE_PROTECTED_PATH_MUTATED}: the candidate changed gate "
        f"definition(s) {', '.join(tampered)}"
    )
    question = (
        "Owner decision required: the candidate changed gate definition(s) "
        f"{', '.join(tampered)}. The gate cannot run under definitions the "
        "candidate itself edited. Accept the definition change into the base "
        "SHA (then re-run the gate against the new base) or reject the change."
    )
    if declared_paths:
        question += (
            " The lane declared "
            f"{', '.join(declared_paths)} in its Touches; a declaration is not "
            "an authorization."
        )
    return GateProtectionResult(
        is_protected=True,
        tampered_paths=tampered,
        declared_paths=declared_paths,
        reason_code=GATE_PROTECTED_PATH_MUTATED,
        reason=reason,
        owner_question=question,
        detected_by=("branch-diff",),
    )


def definition_digest(text: str) -> str:
    """Return the stable content digest recorded for one gate definition."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def gate_definition_mismatches(
    base_definitions: Mapping[str, str],
    candidate_definitions: Mapping[str, str],
) -> tuple[str, ...]:
    """Return gate definitions whose bytes differ from the base SHA.

    Added, removed and edited definitions all count: each one is a definition
    the gate would not have loaded from its base.  Keys are normalized, so a
    caller may pass paths in any repo-relative spelling.
    """
    base = {normalize_path(path): text for path, text in base_definitions.items()}
    candidate = {normalize_path(path): text for path, text in candidate_definitions.items()}
    return tuple(
        sorted(
            path
            for path in set(base) | set(candidate)
            if base.get(path) != candidate.get(path)
        )
    )


def gate_config_sections(text: str) -> dict[str, object] | None:
    """Return the gate-relevant ``pyproject.toml`` tables, ``None`` if unparsable.

    Used as evidence for the owner question, never to weaken the whole-file
    protection rule.
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None
    tool = data.get("tool")
    if not isinstance(tool, dict):
        return {}
    return {
        f"tool.{table}": tool[table] for table in _PYPROJECT_GATE_TOOL_TABLES if table in tool
    }


def changed_gate_config_sections(base_text: str, head_text: str) -> tuple[str, ...]:
    """Return the gate-relevant sections that differ between two pyproject documents.

    An unparsable document reports every gate section as changed: the engine
    cannot show that the gate configuration survived, so it fails closed.
    """
    base = gate_config_sections(base_text)
    head = gate_config_sections(head_text)
    if base is None or head is None:
        return GATE_CONFIG_SECTIONS
    return tuple(
        name for name in sorted(set(base) | set(head)) if base.get(name) != head.get(name)
    )


__all__ = [
    "GATE_CONFIG_SECTIONS",
    "GATE_PROTECTED_PATH_MUTATED",
    "PROTECTED_GATE_BASENAMES",
    "PROTECTED_GATE_CONFIG_PATHS",
    "PROTECTED_GATE_DIRECTORIES",
    "PROTECTED_GATE_SCRIPT_PATHS",
    "GateProtectionResult",
    "GateScopeError",
    "RetirementAction",
    "RetirementCheck",
    "RetirementEntry",
    "ScopeCheck",
    "changed_gate_config_sections",
    "check_gate_protection",
    "check_test_retirement",
    "check_write_scope",
    "collect_tests",
    "collect_tests_from_sources",
    "definition_digest",
    "gate_config_sections",
    "gate_definition_mismatches",
    "git_changed_files",
    "is_protected_gate_path",
    "normalize_path",
    "parse_retirement_table",
]
