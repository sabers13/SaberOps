"""Run-scoped project scope persistence and reconstruction.

At run creation the initial project graph and scope evidence are frozen.  An
existing run reconstructs exactly the same initial project scope even if the
owner's metadata changes later; a new run sees the updated repository base.
Later derived evidence (expansions, the current scope) is persisted separately
and never mutates the frozen initial record.  No secrets are persisted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from saberops.db import Database
from saberops.project_scope.context import render_scope_section
from saberops.project_scope.contracts import (
    ProjectGraph,
    ProjectScope,
    digest_of,
)


class ProjectScopeIntegrityError(Exception):
    """Raised when a run's persisted C09 scope evidence fails to verify.

    The frozen graph, initial scope, current scope and expansion chain must
    reconstruct byte-for-byte to the digests recorded at freeze/expansion
    time.  Malformed JSON, a corrupted digest, a scope that references a
    different graph, or a broken expansion chain all fail closed here --
    never repaired or silently rebuilt from live repository/owner state.
    """


@dataclass(frozen=True)
class ExpansionRecord:
    """One durable, deterministic scope-expansion event."""

    attempt_id: str | None
    path: str | None
    symbol: str | None
    reason: str
    before_digest: str
    after_digest: str

    def summary(self) -> tuple[str, str]:
        """Human/prompt-facing ``(added, reason)`` pair."""
        added = self.path if self.path is not None else f"symbol:{self.symbol}"
        return added, self.reason


@dataclass(frozen=True)
class ScopeBundle:
    """The full reconstructable project-scope state of one run."""

    run_id: str
    repo_root: str
    graph: ProjectGraph
    initial_scope: ProjectScope
    current_scope: ProjectScope
    expansions: tuple[ExpansionRecord, ...]

    @property
    def section(self) -> str:
        """Render the bounded worker scope section from current state."""
        return render_scope_section(
            self.graph,
            self.current_scope,
            tuple(expansion.summary() for expansion in self.expansions),
        )


def freeze_run_scope(
    db: Database,
    run_id: str,
    *,
    repo_root: str,
    graph: ProjectGraph,
    scope: ProjectScope,
) -> None:
    """Freeze the initial graph and scope evidence for ``run_id`` (idempotent)."""
    db.set_run_project_scope(
        run_id=run_id,
        repo_root=repo_root,
        graph_json=json.dumps(graph.to_dict(), sort_keys=True, separators=(",", ":")),
        graph_digest=graph.digest,
        initial_scope_json=json.dumps(
            scope.to_dict(), sort_keys=True, separators=(",", ":")
        ),
        initial_scope_digest=scope.digest,
        current_scope_json=json.dumps(scope.to_dict(), sort_keys=True, separators=(",", ":")),
        current_scope_digest=scope.digest,
        analyzer=graph.analyzer,
        graph_source=graph.graph_source.value,
        confidence=graph.confidence.value,
        base_revision=graph.base_revision,
    )


def _load_json_object(row: dict[str, Any], column: str, run_id: str) -> dict[str, Any]:
    try:
        payload = json.loads(str(row[column]))
    except json.JSONDecodeError as exc:
        raise ProjectScopeIntegrityError(
            f"run '{run_id}': {column} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise ProjectScopeIntegrityError(f"run '{run_id}': {column} must be a JSON object")
    return payload


def load_scope_bundle(db: Database, run_id: str) -> ScopeBundle | None:
    """Reconstruct the run's project scope state; ``None`` when never frozen.

    Every reconstructed digest is verified against what was stored at freeze/
    expansion time, the initial and current scope must reference the same
    frozen graph, the graph's base revision must match the persisted record
    (and the run's authoritative ``base_commit`` when the run is available),
    and the expansion chain must link digest-to-digest from the initial scope
    through to the current one.  Any mismatch fails closed with
    :class:`ProjectScopeIntegrityError`; nothing here repairs or rebuilds from
    live repository or owner state.
    """
    row = db.get_run_project_scope(run_id)
    if row is None:
        return None

    graph_payload = _load_json_object(row, "project_graph_json", run_id)
    initial_payload = _load_json_object(row, "initial_scope_json", run_id)
    current_payload = _load_json_object(row, "current_scope_json", run_id)

    try:
        graph = ProjectGraph.from_dict(graph_payload)
        initial = ProjectScope.from_dict(initial_payload)
        current = ProjectScope.from_dict(current_payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise ProjectScopeIntegrityError(
            f"run '{run_id}': stored project-scope evidence is malformed: {exc}"
        ) from exc

    stored_graph_digest = str(row["project_graph_digest"])
    if graph.digest != stored_graph_digest:
        raise ProjectScopeIntegrityError(
            f"run '{run_id}': reconstructed graph digest {graph.digest} does not "
            f"match stored digest {stored_graph_digest}"
        )
    stored_initial_digest = str(row["initial_scope_digest"])
    if initial.digest != stored_initial_digest:
        raise ProjectScopeIntegrityError(
            f"run '{run_id}': reconstructed initial-scope digest {initial.digest} "
            f"does not match stored digest {stored_initial_digest}"
        )
    stored_current_digest = str(row["current_scope_digest"])
    if current.digest != stored_current_digest:
        raise ProjectScopeIntegrityError(
            f"run '{run_id}': reconstructed current-scope digest {current.digest} "
            f"does not match stored digest {stored_current_digest}"
        )
    if initial.graph_digest != graph.digest:
        raise ProjectScopeIntegrityError(
            f"run '{run_id}': initial scope references graph digest "
            f"{initial.graph_digest}, frozen graph is {graph.digest}"
        )
    if current.graph_digest != graph.digest:
        raise ProjectScopeIntegrityError(
            f"run '{run_id}': current scope references graph digest "
            f"{current.graph_digest}, frozen graph is {graph.digest}"
        )
    stored_base_revision = str(row["base_revision"]) if row["base_revision"] is not None else ""
    if graph.base_revision != stored_base_revision:
        raise ProjectScopeIntegrityError(
            f"run '{run_id}': reconstructed graph base_revision "
            f"{graph.base_revision!r} does not match persisted base_revision "
            f"{stored_base_revision!r}"
        )
    run = db.get_run(run_id)
    if run is not None and run.base_commit and graph.base_revision != run.base_commit:
        raise ProjectScopeIntegrityError(
            f"run '{run_id}': frozen graph base_revision {graph.base_revision!r} "
            f"does not match the run's authoritative base_commit {run.base_commit!r}"
        )

    expansions = tuple(
        ExpansionRecord(
            attempt_id=expansion.get("attempt_id"),
            path=expansion.get("path"),
            symbol=expansion.get("symbol"),
            reason=str(expansion.get("reason", "")),
            before_digest=str(expansion.get("before_digest", "")),
            after_digest=str(expansion.get("after_digest", "")),
        )
        for expansion in db.list_scope_expansions(run_id)
    )
    if not expansions and current.digest != initial.digest:
        # No expansion evidence exists that could legitimately have advanced
        # the current scope past the frozen initial scope -- a mismatch here
        # is corruption (a directly tampered current_scope column), never a
        # legitimate state, and fails closed rather than being silently
        # accepted or repaired.
        raise ProjectScopeIntegrityError(
            f"run '{run_id}': current scope digest {current.digest} differs from "
            f"the initial scope digest {initial.digest} with zero recorded "
            "expansions"
        )
    if expansions:
        if expansions[0].before_digest != initial.digest:
            raise ProjectScopeIntegrityError(
                f"run '{run_id}': first expansion before_digest "
                f"{expansions[0].before_digest} does not match the frozen initial "
                f"scope digest {initial.digest}"
            )
        for previous, following in zip(expansions, expansions[1:], strict=False):
            if following.before_digest != previous.after_digest:
                raise ProjectScopeIntegrityError(
                    f"run '{run_id}': expansion chain is broken between "
                    f"{previous.after_digest} and {following.before_digest}"
                )
        if expansions[-1].after_digest != current.digest:
            raise ProjectScopeIntegrityError(
                f"run '{run_id}': last expansion after_digest "
                f"{expansions[-1].after_digest} does not match the current scope "
                f"digest {current.digest}"
            )

    return ScopeBundle(
        run_id=run_id,
        repo_root=str(row["repo_root"]),
        graph=graph,
        initial_scope=initial,
        current_scope=current,
        expansions=expansions,
    )


def scope_section_for_run(
    db: Database, run_id: str, package_scope_json: str | None = None
) -> str | None:
    """Bounded worker scope section for a run, or ``None`` when unavailable.

    Read-only.  Always reflects the durable current scope, so continuation and
    replacement workers inherit recorded expansions.  When
    ``package_scope_json`` is supplied (a persisted work-package scope), that
    package-specific scope is rendered against the run's frozen graph instead.
    """
    bundle = load_scope_bundle(db, run_id)
    if bundle is None:
        return None
    if package_scope_json:
        from saberops.project_scope.contracts import ProjectScope

        try:
            package_scope = ProjectScope.from_dict(json.loads(package_scope_json))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise ProjectScopeIntegrityError(
                f"run '{run_id}': persisted package scope is malformed: {exc}"
            ) from exc
        if package_scope.graph_digest != bundle.graph.digest:
            # A package scope resolved against a different graph must never
            # be rendered as if it described this run's frozen graph.
            raise ProjectScopeIntegrityError(
                f"run '{run_id}': package scope references graph digest "
                f"{package_scope.graph_digest}, frozen graph is {bundle.graph.digest}"
            )
        return render_scope_section(
            bundle.graph,
            package_scope,
            tuple(expansion.summary() for expansion in bundle.expansions),
        )
    return bundle.section


def record_expansion(
    db: Database,
    run_id: str,
    *,
    updated_scope: ProjectScope,
    attempt_id: str | None,
    path: str | None,
    symbol: str | None,
    reason: str,
    before_digest: str,
) -> None:
    """Persist one expansion and promote the current scope deterministically."""
    db.add_scope_expansion(
        run_id=run_id,
        attempt_id=attempt_id,
        path=path,
        symbol=symbol,
        reason=reason,
        before_digest=before_digest,
        after_digest=updated_scope.digest,
    )
    db.update_run_current_scope(
        run_id=run_id,
        scope_json=json.dumps(updated_scope.to_dict(), sort_keys=True, separators=(",", ":")),
        scope_digest=updated_scope.digest,
    )
    db.record_event(
        run_id,
        "scope_expanded",
        attempt_id=attempt_id,
        payload={
            "path": path,
            "symbol": symbol,
            "reason": reason,
            "before_digest": before_digest,
            "after_digest": updated_scope.digest,
        },
    )


def scope_digest_of(scope: ProjectScope) -> str:
    """Public alias so callers do not reach into contracts for digests."""
    return digest_of(scope.to_dict())


def repo_root_of(bundle: ScopeBundle) -> Path:
    """The repository root the bundle's graph was built against."""
    return Path(bundle.repo_root)
