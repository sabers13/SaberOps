"""Bounded worker scope section projection.

The ``[PROJECT SCOPE]`` section is the only project-architecture evidence a
worker prompt carries.  It is bounded (F01 philosophy), deterministic, and it
never contains unrelated module listings, deployment material, the whole
repository tree, full module manifests, or a duplicate of the original task.
"""

from __future__ import annotations

from orchestrator_mvp.project_scope.contracts import ProjectGraph, ProjectScope

_EXPANSION_INSTRUCTIONS = (
    "Expand context only with specific evidence:\n"
    '  orch scope expand --path <repo-relative-path> --reason "<specific missing '
    'dependency/path evidence>"\n'
    '  orch scope expand --symbol <name> --reason "<specific missing symbol evidence>"'
)


def render_scope_section(
    graph: ProjectGraph,
    scope: ProjectScope,
    expansions: tuple[tuple[str, str], ...] = (),
) -> str:
    """Render the bounded ``[PROJECT SCOPE]`` section for a worker prompt."""
    lines = ["[PROJECT SCOPE]", "", f"Graph:\n  {graph.digest}", ""]
    lines.append("Primary module:")
    if scope.primary_module is not None:
        lines.append(f"  {scope.primary_module}")
    else:
        lines.append("  none (multi-module or unknown scope; see included modules)")
    included = [m for m in scope.included_modules if m != scope.primary_module]
    lines.append("")
    lines.append("Included dependency modules:")
    lines.append(f"  {', '.join(included) if included else '(none)'}")
    lines.append("")
    lines.append("Start with:")
    if scope.source_paths:
        lines.extend(f"  {path}" for path in scope.source_paths)
    else:
        lines.append("  (bounded repository map unavailable; inspect the worktree)")
    if scope.dependency_interface_paths:
        lines.append("")
        lines.append("Dependency interfaces:")
        lines.extend(f"  {path}" for path in scope.dependency_interface_paths)
    lines.append("")
    lines.append("Relevant tests:")
    if scope.test_paths:
        lines.extend(f"  {path}" for path in scope.test_paths)
    else:
        lines.append("  (none identified)")
    lines.append("")
    lines.append("Closure:")
    if scope.closure_complete:
        lines.append("  complete")
    elif scope.truncated:
        lines.append("  incomplete (truncated within budget; expand with evidence)")
    else:
        lines.append(
            "  unknown — repository architecture could not be determined; "
            "verification is broad/full"
        )
    if expansions:
        lines.append("")
        lines.append("Recorded expansions:")
        lines.extend(f"  + {path} ({reason})" for path, reason in expansions)
    lines.append("")
    lines.append(_EXPANSION_INSTRUCTIONS)
    return "\n".join(lines) + "\n"
