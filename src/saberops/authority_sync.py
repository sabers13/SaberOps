"""Safe, marker-scoped synchronization of the OpenCode ORX agent file.

The agent Markdown is an adapter surface generated from
:mod:`saberops.authority`.  Synchronization rewrites only the two
explicitly delimited managed regions and byte-preserves everything else, so
owner-authored instructions are never touched.

Editing is strictly marker-scoped: no fuzzy Markdown matching, no heuristic
section detection.  Missing, duplicated, or inverted markers fail closed
without writing anything.

The owner's installed agent uses the OpenCode **V1** frontmatter key
``permission:``.  This module deliberately preserves that spelling; migrating
to the V2 ``permissions:`` key is a separate concern.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

from saberops.authority import (
    GOVERNED_PUBLISH_COMMAND,
    AuthorityMode,
    AuthorityPolicy,
    HostAccessMode,
    label_for,
    load_authority_settings,
    policy_for,
)

PERMISSIONS_BEGIN: str = "ORCH_AUTHORITY_PERMISSIONS_BEGIN"
PERMISSIONS_END: str = "ORCH_AUTHORITY_PERMISSIONS_END"
PROMPT_BEGIN: str = "ORCH_AUTHORITY_PROMPT_BEGIN"
PROMPT_END: str = "ORCH_AUTHORITY_PROMPT_END"

DEFAULT_AGENT_PATH: str = "~/.config/opencode/agents/orchestrator.md"

# Orch subcommand names that would constitute publication/release.  They are
# denied outright unless a governed publication capability exists *and* the
# active policy grants `auto_publish`.
PUBLICATION_SUBCOMMANDS: tuple[str, ...] = ("publish", "push", "release")


class AgentSyncError(RuntimeError):
    """Fail-closed error raised before any agent file is modified."""


def default_orch_executable() -> Path:
    """Return the `orch` console script of the active environment.

    ``sys.prefix`` is used rather than a resolved ``sys.executable`` so a
    virtualenv whose ``bin/python`` is a symlink to the system interpreter
    still yields the venv's own ``orch``, which is the exact path ORX is
    granted.
    """
    return Path(sys.prefix) / "bin" / "orch"


def default_allowed_directories(orch_executable: Path) -> tuple[str, ...]:
    """Derive the external-directory allowlist from the Orch installation.

    ``<workspace>/<repo>/.venv/bin/orch`` yields the workspace root that holds
    the Orch repository, which is the tree ORX legitimately inspects, plus
    ``/tmp`` for scratch evidence.  Callers may override this entirely.
    """
    parents = orch_executable.parents
    if len(parents) < 4:
        return ("/tmp/**",)
    workspace_root = parents[3]
    return (f"{workspace_root}/**", "/tmp/**")


def _bool_yaml(value: bool) -> str:
    return "true" if value else "false"


def _shell_decision(policy: AuthorityPolicy) -> str:
    """Whether ORX may run Orch itself without a per-invocation prompt."""
    return "allow" if policy.auto_start else "ask"


def _question_decision(policy: AuthorityPolicy) -> str:
    return "allow" if policy.routine_questions else "deny"


def _doom_loop_decision(policy: AuthorityPolicy) -> str:
    """Repeating the same governed Orch command is not itself a new decision."""
    return "ask" if policy.routine_questions else "allow"


def render_permissions_region(
    mode: AuthorityMode,
    *,
    orch_executable: Path,
    allowed_directories: tuple[str, ...],
    governed_publish_command: str | None = GOVERNED_PUBLISH_COMMAND,
    host_access: HostAccessMode = HostAccessMode.GOVERNED,
) -> str:
    """Render the managed OpenCode ``permission:`` block for one mode."""
    policy = policy_for(mode)
    orch = str(orch_executable)
    shell = _shell_decision(policy)
    publish_governed = bool(policy.auto_publish and governed_publish_command)

    lines: list[str] = [
        f"# authority_mode: {mode.value}",
        f"# authority_label: {label_for(mode)}",
        f"# auto_accept: {_bool_yaml(policy.auto_accept)}",
        f"# auto_publish: {_bool_yaml(policy.auto_publish)}",
        f"# routine_questions: {_bool_yaml(policy.routine_questions)}",
        f"# governed_publish_command: {governed_publish_command or '(none)'}",
        "#",
        "# This block is machine-managed by `orch authority sync-opencode`.",
        "# Change the authority mode instead of editing individual rules:",
        "#",
        "#     orch authority set <mode>",
        "",
        "permission:",
        "  read:",
        '    "*": allow',
        '    "*.env": deny',
        '    "*.env.*": deny',
        '    "*.env.example": allow',
        "",
        "  glob: allow",
        "  grep: allow",
        "  list: allow",
        "",
    ]
    if host_access == HostAccessMode.UNRESTRICTED:
        lines += [
            "  # Technical host access is owner-governed separately from authority.",
            "  edit: allow",
            "  task: allow",
            "",
        ]
    else:
        lines += [
            "  # ORX is the controller, never the implementer: project mutation",
            "  # belongs to workers launched through Orch.",
            "  edit: deny",
            "",
            "  # Do not let ORX bypass Orch by spawning OpenCode subagents directly.",
            "  task: deny",
            "",
        ]
    lines += [
        "  skill: allow",
        "  webfetch: allow",
        "  websearch: allow",
        "",
        "  external_directory:",
        f'    "*": {"allow" if host_access == HostAccessMode.UNRESTRICTED else "deny"}',
    ]
    if host_access == HostAccessMode.UNRESTRICTED:
        lines += [
            "",
            "  # Unrestricted means technical capability only; publication remains",
            "  # governed by the authority policy below.",
        ]
    else:
        lines += [
            "",
        ]
    for directory in allowed_directories:
        lines.append(f'    "{directory}": allow')
    lines += [
        "",
        "  # Orch is the only executable ORX may run.  Full authority over Orch",
        "  # is never arbitrary host-shell authority.",
        "  bash:",
        f'    "*": {"allow" if host_access == HostAccessMode.UNRESTRICTED else "deny"}',
        "",
        f'    "{orch}": {shell}',
        f'    "{orch} *": {shell}',
        "",
    ]
    if host_access == HostAccessMode.UNRESTRICTED:
        lines += [
            "    # Raw shell/Git/deployment access is technically unrestricted.",
            "    # Autonomous publication remains governed by the prompt contract.",
            "",
        ]
    if publish_governed:
        assert governed_publish_command is not None
        lines += [
            "    # Publication is authorized only through this governed Orch",
            "    # command; raw git push/merge/tag is never the mechanism.",
            f'    "{orch} {governed_publish_command}": allow',
            f'    "{orch} {governed_publish_command} *": allow',
        ]
    else:
        reason = (
            "auto_publish is granted but Orch exposes no governed publication"
            if policy.auto_publish
            else "this mode does not grant publication authority"
        )
        lines += [
            f"    # Publication stays denied: {reason}.",
            "    # Stop at the strongest supported accepted state instead.",
        ]
        for subcommand in PUBLICATION_SUBCOMMANDS:
            lines.append(f'    "{orch} {subcommand}": deny')
            lines.append(f'    "{orch} {subcommand} *": deny')
    lines += [
        "",
        f"  question: {_question_decision(policy)}",
        f"  doom_loop: {_doom_loop_decision(policy)}",
    ]
    return "\n".join(lines) + "\n"


def _authorized_actions(policy: AuthorityPolicy) -> list[tuple[str, bool]]:
    return [
        ("start Orch runs", policy.auto_start),
        ("retry a recoverable attempt", policy.auto_retry),
        ("repair a failed attempt", policy.auto_repair),
        (
            "escalate capability tier when Orch policy justifies it",
            policy.auto_escalate_when_policy_justifies,
        ),
        ("run the authoritative deterministic gate", policy.auto_gate),
        ("obtain independent review", policy.auto_review),
        ("accept a candidate once every fail-closed requirement passes", policy.auto_accept),
        ("perform ordinary safe Orch-managed cleanup", policy.auto_safe_cleanup),
        ("publish through a governed Orch capability", policy.auto_publish),
    ]


def render_prompt_region(
    mode: AuthorityMode,
    *,
    governed_publish_command: str | None = GOVERNED_PUBLISH_COMMAND,
    host_access: HostAccessMode = HostAccessMode.GOVERNED,
) -> str:
    """Render the managed authority instruction block for one mode."""
    policy = policy_for(mode)
    granted = [text for text, allowed in _authorized_actions(policy) if allowed]
    withheld = [text for text, allowed in _authorized_actions(policy) if not allowed]

    lines: list[str] = [
        "======================================================================",
        "",
        f"CURRENT_AUTHORITY_MODE: {mode.value}",
        f"CURRENT_AUTHORITY_LABEL: {label_for(mode)}",
        "",
        f"TECHNICAL_HOST_ACCESS: {host_access.value}",
        "Host access is technical capability; it does not grant publication authority.",
        "",
        "This block is machine-managed by `orch authority sync-opencode`.",
        "",
        "Read-only repository inspection is always authorized.",
        "",
    ]
    if granted:
        lines.append("Without asking the owner, you are authorized to:")
        lines.append("")
        lines += [f"- {text};" for text in granted]
    else:
        lines.append("No mutating or cost-bearing action is pre-authorized.")
    lines.append("")
    if withheld:
        lines.append("You must obtain explicit owner approval before you:")
        lines.append("")
        lines += [f"- {text};" for text in withheld]
        lines.append("")
    if policy.routine_questions:
        lines += [
            "Routine confirmation questions are permitted in this mode.",
            "",
        ]
    else:
        lines += [
            "Do not ask routine confirmation questions such as:",
            "",
            '    "Should I start?"',
            '    "Should I retry?"',
            '    "Should I repair?"',
            '    "Should I run the gate?"',
            '    "Should I switch provider?"',
            "",
            "Choose a safe default and continue.  Stop only for an",
            "OWNER_ATTENTION condition that cannot be resolved safely and",
            "correctly without the owner.",
            "",
        ]
    if policy.auto_publish and not governed_publish_command:
        lines += [
            "Publication authority is granted by policy, but Orch exposes no",
            "governed publication capability yet.  Stop at the strongest",
            "supported accepted state.  Never substitute raw `git push`,",
            "`git merge`, `git tag`, or a deployment command.",
            "",
        ]
    elif policy.auto_publish:
        lines += [
            f"Publish only through the governed Orch command `{governed_publish_command}`,",
            "and only after acceptance succeeds.  Never substitute raw",
            "`git push`, `git merge`, `git tag`, or a deployment command.",
            "",
        ]
    else:
        lines += [
            "Automatic publication is NOT authorized.  Never run `git push`,",
            "`git merge`, `git tag`, a release/deployment command, or an Orch",
            "publication operation on your own initiative.",
            "",
        ]
    lines += [
        "Authority governs approval only.  It never changes routing order,",
        "tier semantics, training policy, quota policy, or review",
        "independence.",
        "",
        "======================================================================",
    ]
    return "\n".join(lines) + "\n"


def _marker_token(line: str) -> str:
    """Reduce a line to its bare marker token, tolerating a YAML comment prefix."""
    text = line.strip()
    while text.startswith("#"):
        text = text[1:].strip()
    return text


def _locate_marker(lines: list[str], token: str, source_label: str) -> int:
    matches = [index for index, line in enumerate(lines) if _marker_token(line) == token]
    if not matches:
        raise AgentSyncError(f"Agent file {source_label}: required marker {token} is missing")
    if len(matches) > 1:
        found = ", ".join(str(index + 1) for index in matches)
        raise AgentSyncError(
            f"Agent file {source_label}: marker {token} appears {len(matches)} times "
            f"(lines {found}); exactly one occurrence is required"
        )
    return matches[0]


@dataclass(frozen=True)
class _Region:
    begin: int
    end: int


def _locate_region(lines: list[str], begin_token: str, end_token: str, label: str) -> _Region:
    begin = _locate_marker(lines, begin_token, label)
    end = _locate_marker(lines, end_token, label)
    if begin >= end:
        raise AgentSyncError(
            f"Agent file {label}: marker {end_token} (line {end + 1}) must follow "
            f"{begin_token} (line {begin + 1})"
        )
    return _Region(begin=begin, end=end)


@dataclass(frozen=True)
class AgentSyncResult:
    """Outcome of one agent synchronization."""

    path: Path
    mode: AuthorityMode
    changed: bool
    backup_path: Path | None

    def as_dict(self) -> dict[str, object]:
        """Serialize the result for machine-readable CLI output."""
        return {
            "path": str(self.path),
            "mode": self.mode.value,
            "label": label_for(self.mode),
            "changed": self.changed,
            "backup_path": str(self.backup_path) if self.backup_path else None,
        }


def render_agent_text(
    text: str,
    mode: AuthorityMode,
    *,
    source_label: str,
    orch_executable: Path,
    allowed_directories: tuple[str, ...],
    governed_publish_command: str | None = GOVERNED_PUBLISH_COMMAND,
    host_access: HostAccessMode = HostAccessMode.GOVERNED,
) -> str:
    """Return the agent text with only the two managed regions regenerated.

    Raises :class:`AgentSyncError` before producing any output when the
    markers are missing, duplicated, inverted, or overlapping.
    """
    lines = text.splitlines(keepends=True)
    permissions = _locate_region(lines, PERMISSIONS_BEGIN, PERMISSIONS_END, source_label)
    prompt = _locate_region(lines, PROMPT_BEGIN, PROMPT_END, source_label)
    if permissions.begin <= prompt.begin <= permissions.end:
        raise AgentSyncError(
            f"Agent file {source_label}: the managed authority regions overlap; refusing to edit"
        )
    if prompt.begin <= permissions.begin <= prompt.end:
        raise AgentSyncError(
            f"Agent file {source_label}: the managed authority regions overlap; refusing to edit"
        )

    permissions_body = render_permissions_region(
        mode,
        orch_executable=orch_executable,
        allowed_directories=allowed_directories,
        governed_publish_command=governed_publish_command,
        host_access=host_access,
    ).splitlines(keepends=True)
    prompt_body = render_prompt_region(
        mode,
        governed_publish_command=governed_publish_command,
        host_access=host_access,
    ).splitlines(keepends=True)

    first, second = sorted(
        ((permissions, permissions_body), (prompt, prompt_body)),
        key=lambda item: item[0].begin,
    )
    rebuilt: list[str] = []
    rebuilt += lines[: first[0].begin + 1]
    rebuilt += first[1]
    rebuilt += lines[first[0].end : second[0].begin + 1]
    rebuilt += second[1]
    rebuilt += lines[second[0].end :]
    return "".join(rebuilt)


def _backup_path_for(path: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    candidate = path.with_name(f"{path.name}.authority-bak-{stamp}")
    counter = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.authority-bak-{stamp}-{counter}")
        counter += 1
    return candidate


def _atomic_write(path: Path, text: str) -> None:
    tmp_path: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def sync_agent_file(
    path: Path,
    mode: AuthorityMode,
    *,
    orch_executable: Path | None = None,
    allowed_directories: tuple[str, ...] | None = None,
    governed_publish_command: str | None = GOVERNED_PUBLISH_COMMAND,
    host_access: HostAccessMode | None = None,
) -> AgentSyncResult:
    """Synchronize the managed authority regions of an OpenCode agent file.

    An evidence-preserving timestamped backup is taken before the first
    modifying write, and the replacement itself is atomic.  Re-syncing the
    same mode produces identical bytes, so it is a no-op that neither writes
    nor creates a backup.
    """
    executable = orch_executable or default_orch_executable()
    directories = (
        allowed_directories
        if allowed_directories is not None
        else default_allowed_directories(executable)
    )
    effective_host_access = host_access
    if effective_host_access is None:
        effective_host_access = load_authority_settings().host_access
    try:
        original = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AgentSyncError(f"Agent file {path}: cannot read: {exc}") from exc

    updated = render_agent_text(
        original,
        mode,
        source_label=str(path),
        orch_executable=executable,
        allowed_directories=directories,
        governed_publish_command=governed_publish_command,
        host_access=effective_host_access,
    )
    if updated == original:
        return AgentSyncResult(path=path, mode=mode, changed=False, backup_path=None)

    backup = _backup_path_for(path)
    backup.write_text(original, encoding="utf-8")
    _atomic_write(path, updated)
    return AgentSyncResult(path=path, mode=mode, changed=True, backup_path=backup)
