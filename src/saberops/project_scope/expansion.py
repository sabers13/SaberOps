"""Deterministic worker scope expansion.

A worker has a deterministic, model-free way to widen its bounded context when
concrete evidence requires it.  Expansion is an Orch workflow/context contract,
not a filesystem security sandbox: the worker can technically read the
repository through normal host access, but every scope widening is validated,
reasoned about and recorded durably.

Expansion validates that the requested path belongs to the project, rejects
traversal and outside-project paths, records the exact reason plus before/
after scope digests, and stays bounded.  It never calls a model.
"""

from __future__ import annotations

import ast
from pathlib import Path, PurePosixPath

from saberops.db import Database
from saberops.project_scope.contracts import ProjectScope
from saberops.project_scope.persistence import (
    ScopeBundle,
    load_scope_bundle,
    record_expansion,
)
from saberops.project_scope.resolver import MAX_INTERFACE_PATHS, closure_modules

#: Maximum files searched for one symbol expansion (deterministic, bounded).
MAX_SYMBOL_FILES = 4

#: Maximum paths added by one symbol expansion.
MAX_SYMBOL_PATHS = 4

#: Maximum bytes a worker-supplied expansion reason may occupy.  Bounded so a
#: worker cannot defeat bounded-context design by writing a multi-megabyte
#: reason into durable state and every future worker prompt.
MAX_REASON_BYTES = 1024


class ScopeExpansionError(Exception):
    """Raised when an expansion request is invalid, unsafe or impossible."""


def validate_relative_path(path: str) -> str:
    """Validate a repository-relative expansion path; return it normalized."""
    if not path or path.strip() != path:
        raise ScopeExpansionError("path must be a non-empty repository-relative path")
    if "\\" in path or path.startswith("/") or PurePosixPath(path).is_absolute():
        raise ScopeExpansionError(f"path must be repository-relative, got {path!r}")
    parts = PurePosixPath(path).parts
    if any(part == ".." for part in parts):
        raise ScopeExpansionError(f"path traversal rejected: {path!r}")
    if "." not in PurePosixPath(path).name:
        # Directories are not scope targets; workers expand to concrete files.
        raise ScopeExpansionError(f"path must name a concrete file, got {path!r}")
    return path


def _validate_reason(reason: str) -> str:
    if not reason or not reason.strip():
        raise ScopeExpansionError("an explicit reason is required for every expansion")
    stripped = reason.strip()
    size = len(stripped.encode("utf-8"))
    if size > MAX_REASON_BYTES:
        raise ScopeExpansionError(
            f"expansion reason is {size} UTF-8 bytes, exceeding the "
            f"{MAX_REASON_BYTES}-byte bound; shorten it"
        )
    return stripped


def _resolve_expansion_root(
    db: Database, run_id: str, bundle: ScopeBundle, attempt_id: str | None
) -> Path:
    """Resolve the filesystem root used to inspect expansion candidates.

    Worker-attempt mode (``attempt_id`` present) resolves against the
    authoritative attempt worktree recorded in the DB -- never a caller-
    supplied path -- and never silently falls back to the frozen repository
    root when the supplied attempt cannot be verified: an unknown attempt,
    an attempt belonging to a different run, or a missing worktree all fail
    closed here.  Operator mode (no ``attempt_id``) continues to use the
    frozen repository root the graph was built against.  The frozen graph
    remains the ownership/dependency authority either way; only the
    filesystem view used to inspect candidate paths/symbols changes.
    """
    if attempt_id is None:
        root = Path(bundle.repo_root)
        if not root.is_dir():
            raise ScopeExpansionError(f"project root no longer exists: {root}")
        return root
    attempt = db.get_attempt(attempt_id)
    if attempt is None:
        raise ScopeExpansionError(f"unknown worker attempt id: {attempt_id!r}")
    if attempt.run_id != run_id:
        raise ScopeExpansionError(
            f"attempt {attempt_id!r} belongs to run {attempt.run_id!r}, not the "
            f"expanding run {run_id!r}"
        )
    worktree = Path(attempt.worktree_path)
    if not worktree.is_dir():
        raise ScopeExpansionError(
            f"attempt {attempt_id!r} worktree no longer exists: {worktree}"
        )
    return worktree


def _resolve_symbol_files(
    root: Path, graph_files: tuple[str, ...], symbol: str
) -> tuple[str, ...]:
    """Find files (bounded, deterministic) that define ``symbol``.

    ``graph_files`` is the caller's search universe -- conservatively the
    frozen graph's own files plus any path already recorded in the current
    scope, so a candidate path a worker added explicitly by ``--path`` can
    later participate in symbol lookup even though the frozen graph never
    heard of it.  This is never repository-wide semantic indexing.

    Every recorded/frozen relative path is re-resolved and containment-
    checked against ``root`` before it is read -- the same protection
    ``--path`` expansion already gives its single candidate -- so a path that
    was replaced with a symlink pointing outside the active worktree/root is
    never parsed as project evidence.  Symbol expansion fails closed the
    instant such an escape is found rather than silently skipping it.
    """
    resolved_root = root.resolve()
    matches: list[str] = []
    for path in graph_files:
        candidate = (root / path).resolve()
        try:
            candidate.relative_to(resolved_root)
        except ValueError as exc:
            raise ScopeExpansionError(
                f"path {path!r} escapes the project root and cannot be used as "
                "symbol-expansion evidence"
            ) from exc
        try:
            tree = ast.parse(candidate.read_text(encoding="utf-8"), filename=path)
        except (SyntaxError, OSError, UnicodeDecodeError):
            continue
        found = any(
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
            and node.name == symbol
            for node in tree.body
        )
        if not found:
            found = any(
                isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Name) and target.id == symbol
                    for target in node.targets
                )
                for node in tree.body
            )
        if found:
            matches.append(path)
        if len(matches) >= MAX_SYMBOL_FILES:
            break
    if not matches:
        raise ScopeExpansionError(f"symbol '{symbol}' was not found in any graph file")
    return tuple(matches[:MAX_SYMBOL_PATHS])


def expand_run_scope(
    db: Database,
    run_id: str,
    *,
    path: str | None = None,
    symbol: str | None = None,
    reason: str,
    attempt_id: str | None = None,
) -> dict[str, object]:
    """Expand the run's current scope deterministically; return an outcome.

    Rejects traversal, absolute paths and outside-project targets.  Adds the
    requested evidence to the current scope, records the expansion durably and
    never mutates the frozen initial graph or initial scope.
    """
    validated_reason = _validate_reason(reason)
    if (path is None) == (symbol is None):
        raise ScopeExpansionError("expand with exactly one of --path or --symbol")
    bundle = load_scope_bundle(db, run_id)
    if bundle is None:
        raise ScopeExpansionError(f"run '{run_id}' has no frozen project scope")
    root = _resolve_expansion_root(db, run_id, bundle, attempt_id)

    before: ProjectScope = bundle.current_scope
    before_digest = before.digest
    source_paths = list(before.source_paths)

    if path is not None:
        validated = validate_relative_path(path)
        target = (root / validated).resolve()
        try:
            target.relative_to(root.resolve())
        except ValueError as exc:
            raise ScopeExpansionError(f"path escapes the project root: {path!r}") from exc
        if not target.is_file():
            raise ScopeExpansionError(f"path does not exist in the project: {validated}")
        if validated in source_paths:
            return {
                "status": "already_included",
                "path": validated,
                "scope_digest": before_digest,
            }
        added: tuple[str, ...] = (validated,)
        symbol = None
    else:
        assert symbol is not None
        # Conservative bounded search universe: the frozen graph's own files
        # plus whatever the current scope has already recorded (including a
        # candidate path a worker added explicitly by --path).  Never
        # repository-wide semantic indexing.
        search_universe = tuple(sorted({*bundle.graph.files, *source_paths}))
        added = _resolve_symbol_files(root, search_universe, symbol)
        fresh = tuple(item for item in added if item not in source_paths)
        if not fresh:
            return {
                "status": "already_included",
                "symbol": symbol,
                "scope_digest": before_digest,
            }
        added = fresh
        path = None

    # Bounded expansion capacity is an explicit, named hard cap.  Adding the
    # requested path(s) either fits inside it, or the expansion is rejected
    # outright with zero scope mutation -- never a silent alphabetical
    # eviction of requested or already-included paths while still reporting
    # "expanded".
    budget_cap = 2 * before.budget.max_source_files
    prospective_source = tuple(sorted({*source_paths, *added}))
    if len(prospective_source) > budget_cap:
        raise ScopeExpansionError(
            f"expansion would exceed the bounded capacity of {budget_cap} source "
            f"paths (currently {len(source_paths)}, adding {len(added)}); rejected "
            "with zero scope mutation"
        )
    updated_source = prospective_source

    owners_by_path = {item: bundle.graph.owners_of(item) for item in added}
    unowned_added = tuple(
        sorted(item for item, owners in owners_by_path.items() if not owners)
    )
    single_owners = {owners[0] for owners in owners_by_path.values() if len(owners) == 1}
    new_modules = sorted(single_owners - set(before.included_modules))

    included_set = set(before.included_modules) | single_owners
    dependency_interface_paths = list(before.dependency_interface_paths)
    closure_complete = before.closure_complete
    truncated = before.truncated
    verification_level = before.verification_level
    extra_reasons: list[str] = []

    if unowned_added:
        # The worker may legitimately need a newly created/unowned candidate
        # file; allow it as explicit evidence, but the frozen graph cannot
        # vouch for its ownership or dependency closure, so this expansion
        # never keeps a narrow, falsely confident verification claim.
        closure_complete = False
        verification_level = "G4"
        extra_reasons.append(
            "expanded path(s) "
            + ", ".join(unowned_added)
            + " are absent/unowned in the frozen project graph; closure cannot be "
            "claimed and verification widens to G4"
        )

    if new_modules:
        closed = closure_modules(
            bundle.graph,
            tuple(sorted({*before.included_modules, *new_modules})),
            before.budget,
        )
        additional_closure = sorted(set(closed) - included_set)
        included_set |= set(closed)
        new_interfaces: list[str] = []
        for module_id in (*new_modules, *additional_closure):
            module = bundle.graph.module_by_id(module_id)
            if module is not None:
                new_interfaces.extend(
                    module.interface_files or module.owned_files[:MAX_INTERFACE_PATHS]
                )
        merged_interfaces = sorted(set(dependency_interface_paths) | set(new_interfaces))
        extra_reasons.append(
            "expansion added file(s) owned by module(s) not previously in scope: "
            + ", ".join(new_modules)
        )
        if additional_closure:
            extra_reasons.append(
                "dependency closure for newly included module(s) also added: "
                + ", ".join(additional_closure)
            )
        if len(merged_interfaces) > MAX_INTERFACE_PATHS:
            dependency_interface_paths = merged_interfaces[:MAX_INTERFACE_PATHS]
            closure_complete = False
            truncated = True
            extra_reasons.append(
                "newly included module(s) dependency-interface evidence exceeded "
                "the bounded budget; closure marked incomplete rather than silently "
                "omitted"
            )
        else:
            dependency_interface_paths = merged_interfaces

    updated = ProjectScope(
        graph_digest=before.graph_digest,
        graph_source=before.graph_source,
        analyzer=before.analyzer,
        task_digest=before.task_digest,
        task_slug=before.task_slug,
        primary_module=before.primary_module,
        included_modules=tuple(sorted(included_set)),
        confidence=before.confidence,
        source_paths=updated_source,
        dependency_interface_paths=tuple(dependency_interface_paths),
        test_paths=before.test_paths,
        closure_complete=closure_complete,
        truncated=truncated,
        reasons=(
            *before.reasons,
            "expanded with: " + ", ".join(added) + f" (reason: {validated_reason})",
            *extra_reasons,
        ),
        verification_level=verification_level,
        ambiguous_modules=before.ambiguous_modules,
        budget=before.budget,
    )
    record_expansion(
        db,
        run_id,
        updated_scope=updated,
        attempt_id=attempt_id,
        path=path,
        symbol=symbol,
        reason=validated_reason,
        before_digest=before_digest,
    )
    return {
        "status": "expanded",
        "added_paths": list(added),
        "path": path,
        "symbol": symbol,
        "reason": validated_reason,
        "before_digest": before_digest,
        "scope_digest": updated.digest,
    }
