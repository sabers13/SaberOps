"""Real sandbox enforcement backend for autonomous execution.

``SandboxPolicy`` / ``ToolGrant`` / ``ToolExposurePlan`` are policy
*evidence*; this module is the *enforcement* half.  It answers the
question "may THIS autonomous child perform THIS effect on THIS host"
deterministically and fails CLOSED before semantic execution whenever
the answer is not a proven yes.

Honest backend claims on this host:

* ``OS_PREFLIGHT`` -- deterministic pre-launch *authorisation* only:
  every requested write is resolved against the real filesystem
  (``os.path.realpath``, so a symlink such as ``allowed/link ->
  /outside`` can never smuggle a write past the writable scope) and
  must be authorised by BOTH the effective ``SandboxPolicy`` and the
  effective ``ToolGrant``.  Denied *requested* writes never reach a
  child process.  After execution the caller is expected to audit
  ``changed_paths`` via :func:`audit_changed_paths`.  ``OS_PREFLIGHT``
  does NOT claim ``FILESYSTEM`` enforcement: an arbitrary semantic
  child can still open a path that was never presented to
  :func:`preflight_write`, so post-hoc audit is detection, not
  prevention.  ``OS_PREFLIGHT`` therefore refuses autonomous *mutating*
  execution; it remains usable for read-only / non-mutating launches
  plus ``PROCESS`` argv containment and ``CREDENTIAL`` construction.
* ``BWRAP`` -- bubblewrap user-namespace containment, available when
  the ``bwrap`` executable is present on the host (see
  :func:`filesystem_containment_available`).  The child is launched
  with ONLY the runtime filesystem required to execute (system
  library paths such as ``/usr`` / ``/lib`` / ``/bin`` read-only --
  never the whole host root), ``/tmp`` replaced by an empty tmpfs,
  exactly the repository scope the policy authorises (writable
  directories bound read-write, readable-only scope bound read-only,
  everything else invisible), and every denied path covered by a
  bind overlaid with an empty read-only directory (reads see nothing,
  writes fail with ``EROFS``).  Forbidden mutation is prevented by
  the kernel while the child runs -- not merely detected afterwards.
  With the default ``DENY`` network policy the child is additionally
  launched with ``--unshare-net`` so it cannot reach even host
  loopback; a non-``DENY`` effective network policy fails closed
  because no backend on this host can enforce selective egress.
  Enforcement is exact: writable scopes the backend cannot represent
  precisely (a not-yet-existing exact file, a partial filename glob)
  fail closed instead of widening the containing directory.
* ``PROCESS`` -- enforced by argv allowlisting on both backends: the
  host shell is never used (``shell=False`` at the process seam), and
  when the policy names ``approved_executables`` the child argv[0]
  must match.
* ``NETWORK`` -- NOT enforceable by either backend.  Neither backend
  firewalls a child process's egress, so any autonomous binding that
  *requires* network egress is ineligible: preflight fails closed
  instead of silently running unsandboxed.
* ``CREDENTIAL`` -- enforced by construction on both backends: the
  child environment is an explicit allowlist; unrelated ambient
  credentials never enter the child.
* ``TOOL_INTERCEPTION`` -- provided when the caller installs a
  per-attempt execution boundary through the enforcement layer (see
  :func:`attach_sandbox_interceptor` /
  :func:`verify_native_trace_source`) and records tool effects
  through it (see :func:`record_intercepted_effect`).
  ``SANDBOX_INTERCEPTED`` is only ever derived from an actually
  *installed* interceptor, never from a statically constructed
  dataclass; ``NATIVE_STRUCTURED`` only from a harness source the
  runtime configuration factually verified for the adapter that owns
  THIS execution path.

When ``bwrap`` is absent from the host, ``FILESYSTEM`` enforcement is
*unavailable* and autonomous mutating semantic execution fails closed
before model execution (see
:data:`FILESYSTEM_CONTAINMENT_UNAVAILABLE_MESSAGE`).  That refusal is
preferable to falsely claiming enforcement.

This module is intentionally pure except for read-only host probes
(``bwrap`` presence, ``os.path`` existence checks for mount
computation): it never spawns a process itself (the launch seam lives
in :mod:`saberops.process`, which the execution module owns)
and never touches durable state.  It depends only on the sandbox
contracts and the contracts module, so the sandbox module's
``allowed = ["contracts"]`` dependency boundary holds.
"""

from __future__ import annotations

import os
import shutil
import threading
from dataclasses import dataclass, field
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any

from saberops.models import TraceCapability
from saberops.sandbox.exposure import ToolExposurePlan
from saberops.sandbox.grants import (
    CapabilityOperation,
    SandboxContractError,
    ToolGrant,
)
from saberops.sandbox.policy import SandboxPolicy


class SandboxEnforcementError(SandboxContractError):
    """Fail-closed error raised when autonomous execution is not permitted.

    A subclass of :class:`SandboxContractError` so existing
    fail-closed handlers keep working; the distinct type lets callers
    distinguish "policy evidence malformed" from "enforcement refused
    to launch".
    """


class SandboxEnforcementCapability(StrEnum):
    """Typed enforcement properties a backend can actually provide."""

    FILESYSTEM = "FILESYSTEM"
    PROCESS = "PROCESS"
    NETWORK = "NETWORK"
    CREDENTIAL = "CREDENTIAL"
    TOOL_INTERCEPTION = "TOOL_INTERCEPTION"


class SandboxEnforcementBackend(StrEnum):
    """The enforcement backends this host implements.

    ``OS_PREFLIGHT`` = deterministic preflight (policy + grant +
    realpath resolution) before launch, argv-allowlisted process launch
    without a host shell, explicit allowlisted child environment, and
    post-hoc changed-path audit.  It is authorisation + detection, NOT
    filesystem enforcement.

    ``BWRAP`` = the same preflight PLUS bubblewrap user-namespace
    containment at launch: the kernel prevents the child from mutating
    anything outside the policy's writable scope while it runs.
    """

    OS_PREFLIGHT = "OS_PREFLIGHT"
    BWRAP = "BWRAP"


#: Capabilities ``OS_PREFLIGHT`` actually provides.  ``FILESYSTEM`` is
#: absent on purpose: preflight authorises *requested* paths and the
#: audit detects *observed* mutations, but neither prevents an
#: arbitrary semantic child from opening an undeclared path while it
#: runs.  Claiming ``FILESYSTEM`` here would overstate the backend.
OS_PREFLIGHT_CAPABILITIES: frozenset[SandboxEnforcementCapability] = frozenset(
    {
        SandboxEnforcementCapability.PROCESS,
        SandboxEnforcementCapability.CREDENTIAL,
        SandboxEnforcementCapability.TOOL_INTERCEPTION,
    }
)

#: Capabilities the ``BWRAP`` backend actually provides.  ``NETWORK``
#: is absent on purpose: this host cannot firewall a child process's
#: egress, so a network requirement fails closed (see
#: :func:`preflight_autonomous_dispatch`).
BWRAP_CAPABILITIES: frozenset[SandboxEnforcementCapability] = frozenset(
    {
        SandboxEnforcementCapability.FILESYSTEM,
        SandboxEnforcementCapability.PROCESS,
        SandboxEnforcementCapability.CREDENTIAL,
        SandboxEnforcementCapability.TOOL_INTERCEPTION,
    }
)

#: Fail-closed refusal emitted when autonomous *mutating* execution is
#: requested but no filesystem containment backend is available on
#: this host.  Tests assert on a stable prefix of this message, not on
#: the full text.
FILESYSTEM_CONTAINMENT_UNAVAILABLE_MESSAGE: str = (
    "FILESYSTEM CONTAINMENT UNAVAILABLE \u2014 AUTONOMOUS MUTATION BLOCKED"
)

#: Fail-closed refusal emitted when autonomous *read-only* semantic
#: execution is requested but no filesystem containment backend is
#: available on this host.  Readable scope is itself a security
#: boundary, so a zero-write autonomous child may never fall through
#: to an uncontained launch merely because ``writable_paths`` is
#: empty.  Shares the stable ``FILESYSTEM CONTAINMENT UNAVAILABLE``
#: prefix with the mutating refusal; the suffix names the read-only
#: scope instead of mutation.
READONLY_CONTAINMENT_UNAVAILABLE_MESSAGE: str = (
    "FILESYSTEM CONTAINMENT UNAVAILABLE \u2014 AUTONOMOUS READ-ONLY SCOPE BLOCKED"
)


def backend_capabilities(
    backend: SandboxEnforcementBackend = SandboxEnforcementBackend.OS_PREFLIGHT,
) -> frozenset[SandboxEnforcementCapability]:
    """Return the typed capability set ``backend`` actually provides."""
    if backend is SandboxEnforcementBackend.OS_PREFLIGHT:
        return OS_PREFLIGHT_CAPABILITIES
    if backend is SandboxEnforcementBackend.BWRAP:
        return BWRAP_CAPABILITIES
    raise SandboxEnforcementError(f"unknown enforcement backend: {backend!r}")


@lru_cache(maxsize=1)
def bwrap_available() -> bool:
    """Return True iff the ``bwrap`` containment executable is on PATH."""
    return shutil.which("bwrap") is not None


def filesystem_containment_available() -> bool:
    """Return True iff real filesystem containment can be enforced.

    Today that means ``bwrap`` is present (the ``BWRAP`` backend).  When
    this is False, ``FILESYSTEM`` enforcement capability is unavailable
    and autonomous mutating semantic execution must fail closed before
    model execution -- never run unsandboxed.
    """
    return bwrap_available()


# ---------------------------------------------------------------------------
# Filesystem: realpath-anchored preflight + post-hoc audit.
# ---------------------------------------------------------------------------


def resolve_within_repo(repo_root: str, repo_relative_path: str) -> Path:
    """Resolve ``repo_relative_path`` against the REAL filesystem.

    Symlinks in every existing path component are resolved via
    ``os.path.realpath``; the resolved absolute path must stay anchored
    inside the resolved ``repo_root``.  A symlink such as
    ``allowed/link -> /outside`` therefore resolves outside the repo and
    raises :class:`SandboxEnforcementError` instead of being authorised
    as the literal text ``allowed/link/escaped.txt``.
    """
    if not repo_relative_path or repo_relative_path.startswith("/"):
        raise SandboxEnforcementError(
            f"refusing absolute/empty repo-relative path: {repo_relative_path!r}"
        )
    root_real = os.path.realpath(repo_root)
    candidate = os.path.join(root_real, repo_relative_path)
    resolved = os.path.realpath(candidate)
    if resolved != root_real and not resolved.startswith(root_real + os.sep):
        raise SandboxEnforcementError(
            f"symlink escape: {repo_relative_path!r} resolves outside the repository"
        )
    return Path(resolved)


def preflight_write(
    policy: SandboxPolicy,
    grant: ToolGrant,
    repo_relative_path: str,
) -> Path:
    """Fail-closed preflight for one autonomous file write.

    Raises :class:`SandboxEnforcementError` when the write is outside
    the repository (including via symlink escape) or is not authorised
    by BOTH the effective ``SandboxPolicy`` and the effective
    ``ToolGrant``.  Returns the resolved absolute path on success so
    the caller launches the child against exactly what was authorised.
    """
    resolved = resolve_within_repo(policy.repo_root, repo_relative_path)
    root_real = os.path.realpath(policy.repo_root)
    if resolved == Path(root_real):
        resolved_rel = "."
    else:
        resolved_rel = os.path.relpath(str(resolved), root_real)
    if not policy.authorize_write(resolved_rel):
        raise SandboxEnforcementError(
            f"SandboxPolicy denies write to {repo_relative_path!r}"
            f" (resolved: {resolved_rel!r})"
        )
    if not grant.authorize(CapabilityOperation.FILE_WRITE, resolved_rel):
        raise SandboxEnforcementError(
            f"ToolGrant denies FILE_WRITE to {repo_relative_path!r}"
            f" (resolved: {resolved_rel!r})"
        )
    return resolved


def audit_changed_paths(
    policy: SandboxPolicy,
    changed_paths: tuple[str, ...] | list[str],
) -> tuple[str, ...]:
    """Post-hoc audit: return the subset of ``changed_paths`` never authorised.

    An empty return means every observed mutation was inside the
    writable scope.  A non-empty return is a sandbox violation the
    caller must record as such -- the effects are not replay-safe.
    Detection only: this never *prevents* a mutation.
    """
    violations: list[str] = []
    for changed in changed_paths:
        try:
            resolved = resolve_within_repo(policy.repo_root, changed)
        except SandboxEnforcementError:
            violations.append(changed)
            continue
        root_real = os.path.realpath(policy.repo_root)
        resolved_rel = (
            "."
            if resolved == Path(root_real)
            else os.path.relpath(str(resolved), root_real)
        )
        if not policy.authorize_write(resolved_rel):
            violations.append(changed)
    return tuple(violations)


# ---------------------------------------------------------------------------
# BWRAP mount computation: exact-scope kernel enforcement.
#
# The backend enforces EXACTLY what the policy authorises -- never a
# convenient superset.  A writable scope the backend cannot represent
# precisely fails closed (see :func:`require_exact_writable_scope`)
# instead of widening the containing directory: ``allowed/new.txt``
# for a not-yet-existing file, or ``allowed/foo*.txt``, must never
# silently become "writable entire allowed/".
# ---------------------------------------------------------------------------


def _literal_prefix(pattern: str) -> str:
    """Return the literal path prefix of ``pattern`` before any glob char."""
    parts: list[str] = []
    for part in pattern.split("/"):
        if any(char in part for char in "*?[{"):
            break
        parts.append(part)
    return "/".join(parts)


def _pattern_has_wildcard(pattern: str) -> bool:
    return any(char in pattern for char in "*?[{")


def _is_subtree_pattern(pattern: str) -> bool:
    """Return True iff ``pattern`` names an exact directory subtree.

    Only ``<literal-dir>/**`` (and the repo-covering ``**``) qualify:
    the kernel can bind exactly that directory, nothing more.
    """
    if pattern == "**":
        return True
    if not pattern.endswith("/**"):
        return False
    prefix = pattern[: -len("/**")]
    return bool(prefix) and not _pattern_has_wildcard(prefix)


def writable_scope_violation(policy: SandboxPolicy) -> str | None:
    """Return the reason the writable scope is unrepresentable, or None.

    Representable scopes (``None`` return) are exactly:

    * the repo-covering ``**`` (bind the repo root read-write);
    * an exact directory subtree ``<literal-dir>/**`` (bind that
      directory read-write -- created at launch when missing, so the
      bind source always exists);
    * an exact file pattern whose file already exists on the host
      (bind that file read-write).

    Everything else is unrepresentable at mount granularity and must
    fail closed: a not-yet-existing exact file (binding its parent
    would make siblings writable), a partial filename glob such as
    ``allowed/foo*.txt`` or ``*.py`` (binding the containing
    directory would broaden authority), or any other glob the kernel
    cannot express exactly.
    """
    root_real = os.path.realpath(policy.repo_root)
    for pattern in policy.writable_paths:
        if _is_subtree_pattern(pattern):
            continue
        if _pattern_has_wildcard(pattern):
            return (
                f"writable pattern {pattern!r} is not exactly enforceable: "
                "binding its containing directory would broaden authority "
                "beyond the granted scope"
            )
        candidate = os.path.join(root_real, pattern)
        if os.path.isfile(candidate) and os.path.lexists(candidate):
            continue
        return (
            f"writable pattern {pattern!r} is not exactly enforceable: "
            "only an existing exact file can be bound without widening "
            "its containing directory"
        )
    return None


def require_exact_writable_scope(policy: SandboxPolicy) -> None:
    """Fail closed unless the writable scope is exactly enforceable.

    Raises :class:`SandboxEnforcementError` naming the offending
    pattern.  Narrowing-safe (deny wins) adjustments happen at mount
    time; anything that would broaden authority refuses here, before
    any semantic child exists.
    """
    violation = writable_scope_violation(policy)
    if violation is not None:
        raise SandboxEnforcementError(
            f"refusing autonomous mutating launch: {violation}"
        )


def policy_is_repo_writable(policy: SandboxPolicy) -> bool:
    """Return True iff the policy's writable scope covers the whole repo."""
    return "**" in set(policy.writable_paths)


def policy_is_mutating(policy: SandboxPolicy) -> bool:
    """Return True iff the policy authorises any filesystem mutation."""
    return bool(policy.writable_paths)


def writable_bind_dirs(policy: SandboxPolicy) -> tuple[str, ...]:
    """Return the absolute writable directories the BWRAP seam must bind.

    Exact granularity only: each writable pattern contributes either
    its subtree directory (``allowed/**`` -> ``<root>/allowed``) or an
    already-existing exact file.  A repo-covering ``**`` yields the
    repo root itself.  Raises :class:`SandboxEnforcementError` for any
    pattern :func:`writable_scope_violation` deems unrepresentable --
    the seam fails closed instead of widening a directory.  Callers
    must ``mkdir -p`` every non-root entry before launching (empty
    directories are invisible to git status); this function itself
    creates nothing.
    """
    require_exact_writable_scope(policy)
    root_real = os.path.realpath(policy.repo_root)
    if policy_is_repo_writable(policy):
        return (root_real,)
    dirs: set[str] = set()
    for pattern in policy.writable_paths:
        prefix = _literal_prefix(pattern)
        if not prefix:
            # Unreachable: root-level globs are unrepresentable and
            # were refused above.  Kept as a fail-closed belt.
            raise SandboxEnforcementError(
                f"writable pattern {pattern!r} has no literal directory to bind"
            )
        candidate = os.path.join(root_real, prefix)
        if _pattern_has_wildcard(pattern):
            # Only `<dir>/**` survives the exactness gate above.
            dirs.add(candidate)
        else:
            # Exact file pattern, known to exist (see the gate above).
            dirs.add(candidate)
    # Drop binds nested under another bind (the outer bind covers them).
    ordered = sorted(dirs)
    minimal: list[str] = []
    for entry in ordered:
        if not any(entry != outer and entry.startswith(outer + os.sep) for outer in minimal):
            minimal.append(entry)
    return tuple(minimal)


def readable_bind_paths(policy: SandboxPolicy) -> tuple[str, ...]:
    """Return the absolute repository paths bound read-only for reads.

    The child receives exactly the readable scope -- never the whole
    repository: an exact subtree ``<dir>/**`` (or repo-covering
    ``**``) contributes its directory, an existing exact file
    contributes the file.  Patterns that cannot be represented
    exactly are SKIPPED (never widened): under-exposure fails safe
    for reads (the child sees less), while
    :func:`require_exact_writable_scope` still refuses loudly for
    writes.  Missing paths contribute nothing -- there is nothing to
    expose.  Deny-covered binds are dropped by the mount computation
    (see :func:`bwrap_mount_args_for_policy`).
    """
    root_real = os.path.realpath(policy.repo_root)
    if "**" in set(policy.readable_paths):
        return (root_real,)
    bound: set[str] = set()
    for pattern in policy.readable_paths:
        if _is_subtree_pattern(pattern):
            prefix = pattern[: -len("/**")] if pattern != "**" else ""
            candidate = os.path.join(root_real, prefix) if prefix else root_real
            if os.path.lexists(candidate):
                bound.add(candidate)
            continue
        if _pattern_has_wildcard(pattern):
            # Partial globs cannot be bound exactly: skip rather than
            # expose the containing directory.
            continue
        candidate = os.path.join(root_real, pattern)
        if os.path.lexists(candidate):
            bound.add(candidate)
    ordered = sorted(bound)
    minimal: list[str] = []
    for entry in ordered:
        if not any(entry != outer and entry.startswith(outer + os.sep) for outer in minimal):
            minimal.append(entry)
    return tuple(minimal)


def _deny_literal_prefixes(policy: SandboxPolicy) -> list[str]:
    """Return the absolute literal prefixes of every deny pattern."""
    root_real = os.path.realpath(policy.repo_root)
    prefixes: list[str] = []
    for pattern in policy.deny_paths:
        if pattern in ("**", "*"):
            prefixes.append(root_real)
            continue
        prefix = _literal_prefix(pattern)
        if not prefix:
            continue
        prefixes.append(os.path.join(root_real, prefix))
    return sorted(set(prefixes))


def _is_covered(path: str, bind: str) -> bool:
    return path == bind or path.startswith(bind + os.sep)


def deny_mask_paths(policy: SandboxPolicy) -> tuple[str, ...]:
    """Return absolute denied paths the BWRAP seam must mask.

    A denied prefix is masked whenever a kept bind would otherwise
    expose it -- including prefixes that do not exist on the host
    yet: the seam overlays an empty read-only directory
    preemptively, so a child can neither read through nor create
    into a denied path under a read-write bind.  Prefixes no bind
    covers stay invisible on their own and need no mask.  A
    root-covering deny (``**`` / ``*``) refuses all exposure: no bind
    may open the repository, so there is nothing to mask.
    """
    deny_prefixes = _deny_literal_prefixes(policy)
    root_real = os.path.realpath(policy.repo_root)
    if root_real in deny_prefixes:
        # A root-covering deny refuses all mutation: no bind may open it.
        return ()
    try:
        kept_binds = set(writable_bind_dirs(policy)) | set(readable_bind_paths(policy))
    except SandboxEnforcementError:
        # Unrepresentable writable scope fails closed at the gate; no
        # mask set is meaningful without binds.
        return ()
    if policy_is_repo_writable(policy) or "**" in set(policy.readable_paths):
        kept_binds.add(root_real)
    masked = {
        prefix
        for prefix in deny_prefixes
        if any(_is_covered(prefix, bind) for bind in kept_binds)
    }
    return tuple(sorted(masked))


#: System runtime paths the BWRAP seam exposes read-only so a
#: contained child can execute.  This is the complete list: the host
#: root itself is NEVER bound, so user data outside the authorised
#: repository scope (other repositories, home files, on-disk
#: credentials) is invisible to the child.  Entries missing from a
#: given host are skipped at mount time.
RUNTIME_READONLY_BINDS: tuple[str, ...] = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc")


def runtime_readonly_binds() -> tuple[str, ...]:
    """Return the existing system runtime paths bound read-only."""
    return tuple(path for path in RUNTIME_READONLY_BINDS if os.path.lexists(path))


_empty_mask_dir: str | None = None


def empty_mask_dir() -> str:
    """Return a process-wide empty directory used for deny overlays.

    Denied paths covered by a bind are overlaid with this directory
    read-only: the child reads an empty directory and any write fails
    with ``EROFS``.  The directory lives in the system temp area --
    never inside the repository, so masking never pollutes the
    worktree -- and is created once per process.
    """
    global _empty_mask_dir
    if _empty_mask_dir is None:
        import tempfile

        candidate = tempfile.mkdtemp(prefix="orch-sandbox-mask-")
        _empty_mask_dir = candidate
    return _empty_mask_dir


def bwrap_mount_args_for_policy(policy: SandboxPolicy) -> list[str]:
    """Return the bwrap mount arguments enforcing ``policy`` at the kernel.

    Layout (later mounts overlay earlier ones):

    * ``--unshare-net`` when the effective network policy is ``DENY``
      (actually enforced network isolation: the child cannot reach
      even host loopback).  A non-``DENY`` effective network mode
      refuses here -- no backend on this host can enforce selective
      egress, so building mounts for it would pretend a policy is
      enforced that is not.
    * system runtime binds (read-only): exactly
      :data:`RUNTIME_READONLY_BINDS` entries present on the host --
      never the host root, so unrelated user data stays invisible;
    * ``--tmpfs /tmp`` -- a fresh empty ``/tmp`` hiding host temp paths;
    * ``--proc /proc --dev /dev`` -- minimal runtime plumbing;
    * repository binds: the repo root read-write when the writable
      scope covers it, else one ``--bind`` per writable directory
      from :func:`writable_bind_dirs` plus one ``--ro-bind`` per
      readable-only path from :func:`readable_bind_paths`.  Binds
      covered by a deny prefix are dropped (deny wins by narrowing,
      never by broadening); a root-covering deny drops every
      repository bind.
    * deny masks: one ``--ro-bind <empty> <denied>`` per path from
      :func:`deny_mask_paths` (deny wins, including preemptively for
      denied paths that do not exist on the host yet).
    """
    if policy.network.mode != "DENY":
        raise SandboxEnforcementError(
            "refusing to build containment mounts: effective network policy "
            f"mode {policy.network.mode!r} cannot be enforced by this backend"
        )
    root_real = os.path.realpath(policy.repo_root)
    args: list[str] = ["--unshare-net"]
    for runtime_path in runtime_readonly_binds():
        args += ["--ro-bind", runtime_path, runtime_path]
    args += [
        "--tmpfs", "/tmp",
        "--proc", "/proc",
        "--dev", "/dev",
        "--die-with-parent",
    ]
    deny_prefixes = _deny_literal_prefixes(policy)
    root_denied = root_real in deny_prefixes

    def _dropped_by_deny(bind: str) -> bool:
        return any(
            bind == prefix or bind.startswith(prefix + os.sep)
            for prefix in deny_prefixes
        ) or (
            any(
                _glob_match_host_prefix(bind, root_real, pattern)
                for pattern in policy.deny_paths
            )
        )

    repo_rw = policy_is_repo_writable(policy) and not root_denied
    # A read-write bind already implies readability: readable paths it
    # covers must NOT be overlaid read-only afterwards (a later
    # ``--ro-bind`` would shadow the writable bind and break
    # legitimate writes).
    rw_binds: set[str] = set()
    if repo_rw:
        args += ["--bind", root_real, root_real]
        rw_binds.add(root_real)
    else:
        if not root_denied:
            for bind_dir in writable_bind_dirs(policy):
                if bind_dir == root_real:
                    args += ["--bind", root_real, root_real]
                    rw_binds.add(root_real)
                elif not _dropped_by_deny(bind_dir):
                    args += ["--bind", bind_dir, bind_dir]
                    rw_binds.add(bind_dir)
            for readable_path in readable_bind_paths(policy):
                if any(_is_covered(readable_path, rw) for rw in rw_binds):
                    continue
                if readable_path == root_real:
                    args += ["--ro-bind", root_real, root_real]
                elif not _dropped_by_deny(readable_path):
                    if os.path.isdir(readable_path) and not os.path.islink(readable_path):
                        args += ["--ro-bind", readable_path, readable_path]
                    elif os.path.lexists(readable_path):
                        args += ["--ro-bind", readable_path, readable_path]
    mask_source = empty_mask_dir()
    for masked in deny_mask_paths(policy):
        args += ["--ro-bind", mask_source, masked]
    return args


def _glob_match_host_prefix(bind: str, root_real: str, pattern: str) -> bool:
    """Return True iff deny glob ``pattern`` can match inside ``bind``.

    Conservative narrowing helper for mount computation: when a deny
    glob's literal prefix is an ancestor of (or equal to) ``bind``,
    the bind is dropped rather than risk exposing denied content.
    """
    prefix = _literal_prefix(pattern)
    if not prefix:
        return True
    ancestor = os.path.join(root_real, prefix)
    return bind == ancestor or bind.startswith(ancestor + os.sep)


def ensure_sandbox_bind_dirs(policy: SandboxPolicy) -> tuple[str, ...]:
    """Create every non-root writable bind directory; return what exists.

    Empty directories are invisible to ``git status``.  Only exact
    subtree binds are created (see :func:`require_exact_writable_scope`:
    unrepresentable scopes raise before anything is created).
    Readable-only binds are never created -- a missing readable path
    simply stays unbound.  Deny-mask targets are never created.
    """
    require_exact_writable_scope(policy)
    ready: list[str] = []
    for bind_dir in writable_bind_dirs(policy):
        if bind_dir == os.path.realpath(policy.repo_root):
            ready.append(bind_dir)
            continue
        if os.path.isfile(bind_dir) and os.path.lexists(bind_dir):
            ready.append(bind_dir)
            continue
        os.makedirs(bind_dir, exist_ok=True)
        ready.append(bind_dir)
    return tuple(ready)


# ---------------------------------------------------------------------------
# Process: argv allowlist, never a host shell.
# ---------------------------------------------------------------------------


def preflight_executable(policy: SandboxPolicy, argv: list[str]) -> str:
    """Fail-closed preflight for one sandboxed child argv.

    The host shell is never authorised through this seam (there is no
    ``shell=True`` anywhere on the sandboxed path).  When the policy
    names ``approved_executables``, ``argv[0]`` (by literal or
    basename) must be listed; otherwise a child could escape into an
    unrestricted host command path merely because
    ``authorize_sandboxed_shell()`` returned True.
    """
    if not argv or not argv[0]:
        raise SandboxEnforcementError("refusing to launch an empty child argv")
    executable = argv[0]
    approved = policy.process.approved_executables
    if approved and not policy.authorize_executable(executable):
        raise SandboxEnforcementError(
            f"executable {executable!r} is not in the attempt's approved_executables"
        )
    return executable


# ---------------------------------------------------------------------------
# Trace mechanism: pre-launch verified capability vs observed evidence.
# ---------------------------------------------------------------------------


#: Harness ``source_kind`` values whose *structured event capability*
#: has been factually verified for autonomous use.  These are the
#: ``source_kind`` strings produced by adapters that carry the
#: ``structured_usage`` factual capability after observation + testing
#: (OpenCode JSONL, Cline JSONL, Codex JSONL).  Copilot / Antigravity
#: CLI paths are deliberately NOT members: mere executable presence
#: never proves a structured event stream.
NATIVE_STRUCTURED_VERIFIED_SOURCES: frozenset[str] = frozenset(
    {
        "opencode_jsonl",
        "cline_jsonl",
        "codex_jsonl",
    }
)


#: Runtime-owned mapping from adapter provider name to the harness
#: event sources factually verified for THAT adapter's execution
#: path.  A ``native_source_kind`` string alone never confers
#: ``NATIVE_STRUCTURED`` eligibility: only
#: :func:`verify_native_trace_source` -- which checks this mapping
#: and installs the mechanism on the attempt's boundary -- does.
#: Providers absent here (Copilot, Antigravity, and any test-local
#: backend) have no verified native structured trace; autonomous
#: execution for them requires an installed sandbox interceptor
#: instead.
NATIVE_STRUCTURED_ADAPTER_SOURCES: dict[str, frozenset[str]] = {
    "opencode": frozenset({"opencode_jsonl"}),
    "cline": frozenset({"cline_jsonl"}),
    "codex": frozenset({"codex_jsonl"}),
}


@dataclass
class _BoundaryInstallation:
    """One genuinely installed per-attempt execution boundary.

    The registry of these records is what separates "a mechanism is
    installed on THIS execution path" from "a caller typed a
    dataclass".  :func:`attach_sandbox_interceptor` and
    :func:`verify_native_trace_source` are the only paths that create
    records; every eligibility decision consults them.
    """

    attempt_id: str
    run_id: str
    policy_digest: str
    grant_digest: str
    has_interceptor: bool = False
    native_source_kind: str | None = None
    native_verified_by: str | None = None
    observed_effects: list[dict[str, str]] = field(default_factory=list)


_boundary_installations: dict[tuple[str, str, str, str], _BoundaryInstallation] = {}
_boundary_lock: threading.Lock = threading.Lock()


def _installation_key(
    *, attempt_id: str, run_id: str, policy: SandboxPolicy, grant: ToolGrant
) -> tuple[str, str, str, str]:
    return (attempt_id, run_id, policy.digest, grant.digest)


def _get_or_create_installation(
    *, attempt_id: str, run_id: str, policy: SandboxPolicy, grant: ToolGrant
) -> _BoundaryInstallation:
    key = _installation_key(
        attempt_id=attempt_id, run_id=run_id, policy=policy, grant=grant
    )
    with _boundary_lock:
        installation = _boundary_installations.get(key)
        if installation is None:
            installation = _BoundaryInstallation(
                attempt_id=attempt_id,
                run_id=run_id,
                policy_digest=policy.digest,
                grant_digest=grant.digest,
            )
            _boundary_installations[key] = installation
        return installation


def _find_installations(
    *, policy: SandboxPolicy, grant: ToolGrant
) -> list[_BoundaryInstallation]:
    with _boundary_lock:
        return [
            installation
            for installation in _boundary_installations.values()
            if installation.policy_digest == policy.digest
            and installation.grant_digest == grant.digest
        ]


def _interceptor_installed(
    *,
    policy: SandboxPolicy,
    grant: ToolGrant,
    attempt_id: str | None = None,
    run_id: str | None = None,
) -> bool:
    """Return True iff an interceptor is installed for this boundary.

    With ``attempt_id``/``run_id`` the installation must be pinned to
    THIS attempt.  Without them any installation for these digests
    counts (digest-level sharing across attempts with an identical
    boundary).  The unpinned form exists ONLY for the non-autonomous
    unit-level API (:meth:`VerifiedTraceMechanism.trace_class_for`);
    autonomous eligibility and launch ALWAYS pin (see
    :func:`require_installed_trace_mechanism`) and never use this
    fallback.
    """
    candidates = _find_installations(policy=policy, grant=grant)
    matching = [item for item in candidates if item.has_interceptor]
    if attempt_id is None or run_id is None:
        return bool(matching)
    return any(
        item.attempt_id == attempt_id and item.run_id == run_id for item in matching
    )


def _native_installed(
    *,
    source_kind: str,
    verified_by: str,
    policy: SandboxPolicy,
    grant: ToolGrant,
    attempt_id: str | None = None,
    run_id: str | None = None,
) -> bool:
    """Return True iff a verified native source is installed here."""
    candidates = _find_installations(policy=policy, grant=grant)
    matching = [
        item
        for item in candidates
        if item.native_source_kind == source_kind
        and item.native_verified_by == verified_by
    ]
    if attempt_id is None or run_id is None:
        return bool(matching)
    return any(
        item.attempt_id == attempt_id and item.run_id == run_id for item in matching
    )


@dataclass(frozen=True)
class SandboxEffectInterceptor:
    """Per-attempt proof that interception is attached to THIS boundary.

    A bare :class:`TraceCapability` enum value can be typed by any
    caller; this object cannot be borrowed across attempts: it pins the
    ``attempt_id`` / ``run_id`` it was attached for together with the
    exact ``policy_digest`` / ``grant_digest`` of the boundary it
    guards.  :meth:`is_armed_for` re-checks those digests against the
    effective policy + grant at preflight time, so an interceptor
    attached to attempt A can never authorise attempt B.

    A valid zero-tool-effect attempt still carries an armed
    interceptor: eligibility proves the *mechanism* exists, never that
    a fake effect occurred.
    """

    attempt_id: str
    run_id: str
    policy_digest: str
    grant_digest: str

    def is_armed_for(self, *, policy: SandboxPolicy, grant: ToolGrant) -> bool:
        """Return True iff this interceptor guards THIS policy + grant."""
        return (
            self.policy_digest == policy.digest
            and self.grant_digest == grant.digest
        )


def attach_sandbox_interceptor(
    *,
    policy: SandboxPolicy,
    grant: ToolGrant,
    attempt_id: str,
    run_id: str,
) -> SandboxEffectInterceptor:
    """Install interception on one attempt's execution boundary.

    The ONLY legitimate way to obtain a :class:`SandboxEffectInterceptor`:
    the factory binds the caller's effective policy + grant digests to
    the attempt the enforcement layer will record effects for AND
    registers that installation in the enforcement layer, so
    pre-launch eligibility can distinguish "installed on THIS
    execution path" from "a dataclass with matching text".  There is
    no path that upgrades a ``NONE`` attempt by typing an enum, and a
    directly constructed :class:`SandboxEffectInterceptor` (one that
    never passed through this factory) confers no eligibility.
    """
    if not attempt_id or not run_id:
        raise SandboxEnforcementError(
            "refusing to attach a sandbox interceptor without attempt/run identity"
        )
    interceptor = SandboxEffectInterceptor(
        attempt_id=attempt_id,
        run_id=run_id,
        policy_digest=policy.digest,
        grant_digest=grant.digest,
    )
    installation = _get_or_create_installation(
        attempt_id=attempt_id, run_id=run_id, policy=policy, grant=grant
    )
    with _boundary_lock:
        installation.has_interceptor = True
    return interceptor


def verify_native_trace_source(
    *,
    adapter_provider: str,
    source_kind: str,
    policy: SandboxPolicy,
    grant: ToolGrant,
    attempt_id: str,
    run_id: str,
) -> VerifiedTraceMechanism:
    """Install + return the verified native trace mechanism for one attempt.

    The ONLY legitimate way to claim ``NATIVE_STRUCTURED``: the
    ``adapter_provider`` must own ``source_kind`` in the
    runtime-owned :data:`NATIVE_STRUCTURED_ADAPTER_SOURCES` mapping
    (a factually verified adapter/harness execution path), and the
    installation is registered on this attempt's boundary.  An
    arbitrary caller string -- however plausible -- raises
    :class:`SandboxEnforcementError` instead of manufacturing
    eligibility.
    """
    allowed = NATIVE_STRUCTURED_ADAPTER_SOURCES.get(adapter_provider, frozenset())
    if source_kind not in NATIVE_STRUCTURED_VERIFIED_SOURCES or source_kind not in allowed:
        raise SandboxEnforcementError(
            f"refusing unverified native trace source {source_kind!r} "
            f"for provider {adapter_provider!r}: no factually verified "
            "harness execution path owns it"
        )
    if not attempt_id or not run_id:
        raise SandboxEnforcementError(
            "refusing to verify a native trace source without attempt/run identity"
        )
    installation = _get_or_create_installation(
        attempt_id=attempt_id, run_id=run_id, policy=policy, grant=grant
    )
    with _boundary_lock:
        installation.native_source_kind = source_kind
        installation.native_verified_by = adapter_provider
    return VerifiedTraceMechanism(
        native_source_kind=source_kind,
        native_verified_by=adapter_provider,
    )


@dataclass(frozen=True)
class VerifiedTraceMechanism:
    """Pre-launch evidence that a trace mechanism is available.

    This is the PRE-LAUNCH half of the trace contract: "a verified
    trace mechanism is available for this exact execution path".  The
    POST/IN-FLIGHT half -- the actual :class:`AttemptTrace` evidence
    the mechanism produced -- is recorded separately by the
    enforcement layer (see :func:`resolve_trace_capability` and
    :func:`record_intercepted_effect`).

    * ``native_source_kind`` -- the harness event source for THIS
      attempt; counts only when it names a factually-verified harness
      path (see :data:`NATIVE_STRUCTURED_VERIFIED_SOURCES`) AND
      ``native_verified_by`` names the adapter provider that owns it
      (see :data:`NATIVE_STRUCTURED_ADAPTER_SOURCES`) AND the
      installation was registered via
      :func:`verify_native_trace_source`.  A bare caller string never
      suffices.
    * ``native_verified_by`` -- the adapter provider whose verified
      harness path produced ``native_source_kind``; ``None`` means
      "nobody verified this", which derives ``NONE``.
    * ``interceptor`` -- the :class:`SandboxEffectInterceptor`
      attached to THIS attempt's boundary; counts only when
      :meth:`is_armed_for` accepts the effective policy + grant AND
      the installation was registered via
      :func:`attach_sandbox_interceptor`.  A directly constructed
      dataclass never suffices.

    ``None`` (or an object satisfying neither arm) means ``NONE``:
    autonomous execution fails closed before semantic launch.
    """

    native_source_kind: str | None = None
    native_verified_by: str | None = None
    interceptor: SandboxEffectInterceptor | None = None

    def trace_class_for(
        self, *, policy: SandboxPolicy, grant: ToolGrant
    ) -> TraceCapability:
        """Derive the trace class from the ACTUAL mechanism evidence."""
        if (
            self.native_source_kind is not None
            and self.native_verified_by is not None
            and self.native_source_kind in NATIVE_STRUCTURED_VERIFIED_SOURCES
            and self.native_source_kind
            in NATIVE_STRUCTURED_ADAPTER_SOURCES.get(self.native_verified_by, frozenset())
            and _native_installed(
                source_kind=self.native_source_kind,
                verified_by=self.native_verified_by,
                policy=policy,
                grant=grant,
            )
        ):
            return TraceCapability.NATIVE_STRUCTURED
        if (
            self.interceptor is not None
            and self.interceptor.is_armed_for(policy=policy, grant=grant)
            and _interceptor_installed(policy=policy, grant=grant)
        ):
            return TraceCapability.SANDBOX_INTERCEPTED
        return TraceCapability.NONE


# ---------------------------------------------------------------------------
# In-flight evidence: what the installed boundary actually observed.
# ---------------------------------------------------------------------------


def require_installed_trace_mechanism(
    trace_mechanism: VerifiedTraceMechanism | None,
    *,
    policy: SandboxPolicy,
    grant: ToolGrant,
    attempt_id: str | None = None,
    run_id: str | None = None,
) -> TraceCapability:
    """Return the trace class iff the mechanism is installed for THIS attempt.

    Single choke point for autonomous pre-launch trace eligibility.
    ``attempt_id`` and ``run_id`` are MANDATORY: an unpinned check --
    one where a prior legitimate installation for identical
    policy/grant digests could satisfy a later attempt -- never
    authorises autonomous execution.  A ``None`` mechanism, a mechanism
    that derives ``NONE``, a directly constructed dataclass/token that
    was never installed through :func:`attach_sandbox_interceptor` /
    :func:`verify_native_trace_source`, or an installation pinned to a
    different attempt all raise :class:`SandboxEnforcementError`
    before any semantic execution.

    The non-autonomous unit-level digest API
    (:meth:`VerifiedTraceMechanism.trace_class_for`) is deliberately
    NOT consulted here, so no "any installation with matching digests"
    fallback can authorise a new Attempt.
    """
    if not attempt_id or not run_id:
        raise SandboxEnforcementError(
            "autonomous execution ineligible: trace eligibility requires "
            "THIS run_id + attempt_id (unpinned digest matching never "
            "authorises autonomous execution)"
        )
    if trace_mechanism is None:
        raise SandboxEnforcementError(
            "autonomous execution ineligible: no verified trace mechanism "
            "for this execution path (no verified native harness events "
            "and no installed sandbox interceptor)"
        )
    if (
        trace_mechanism.native_source_kind is not None
        and trace_mechanism.native_verified_by is not None
        and trace_mechanism.native_source_kind in NATIVE_STRUCTURED_VERIFIED_SOURCES
        and trace_mechanism.native_source_kind
        in NATIVE_STRUCTURED_ADAPTER_SOURCES.get(
            trace_mechanism.native_verified_by, frozenset()
        )
        and _native_installed(
            source_kind=trace_mechanism.native_source_kind,
            verified_by=trace_mechanism.native_verified_by,
            policy=policy,
            grant=grant,
            attempt_id=attempt_id,
            run_id=run_id,
        )
    ):
        return TraceCapability.NATIVE_STRUCTURED
    if (
        trace_mechanism.interceptor is not None
        and trace_mechanism.interceptor.attempt_id == attempt_id
        and trace_mechanism.interceptor.run_id == run_id
        and trace_mechanism.interceptor.is_armed_for(policy=policy, grant=grant)
        and _interceptor_installed(
            policy=policy,
            grant=grant,
            attempt_id=attempt_id,
            run_id=run_id,
        )
    ):
        return TraceCapability.SANDBOX_INTERCEPTED
    raise SandboxEnforcementError(
        "autonomous execution ineligible: no trace mechanism is installed "
        "for this run_id + attempt_id (a directly constructed interceptor "
        "or native-source object never authorises execution)"
    )


def record_intercepted_effect(
    *,
    policy: SandboxPolicy,
    grant: ToolGrant,
    attempt_id: str,
    run_id: str,
    operation: str,
    resource_ref: str,
) -> None:
    """Record one tool effect the installed boundary actually observed.

    The POST/IN-FLIGHT half of the trace contract: post-execution
    evidence comes from what the installed mechanism observed -- never
    from a caller declaration.  A valid zero-effect attempt simply
    records nothing.  Raises :class:`SandboxEnforcementError` when no
    boundary is installed for this attempt (effects cannot be
    observed without one).
    """
    key = _installation_key(
        attempt_id=attempt_id, run_id=run_id, policy=policy, grant=grant
    )
    with _boundary_lock:
        installation = _boundary_installations.get(key)
        if installation is None or (
            not installation.has_interceptor and installation.native_source_kind is None
        ):
            raise SandboxEnforcementError(
                "refusing to record an intercepted effect: no execution "
                "boundary is installed for this attempt"
            )
        installation.observed_effects.append(
            {"operation": operation, "resource_ref": resource_ref}
        )


def retire_boundary_installation(
    *,
    policy: SandboxPolicy,
    grant: ToolGrant,
    attempt_id: str,
    run_id: str,
) -> bool:
    """Retire one Attempt's in-memory boundary installation, if present.

    Called when the Attempt's boundary lifecycle is complete (after
    durable trace/effect persistence has been attempted) so the
    registry does not grow indefinitely or leave stale eligibility
    material behind: a retired installation can never authorise a
    later Attempt, not even at digest level.  Returns True iff an
    installation was actually removed.  Never raises: retirement is
    cleanup, not a security decision.
    """
    if not attempt_id or not run_id:
        return False
    key = _installation_key(
        attempt_id=attempt_id, run_id=run_id, policy=policy, grant=grant
    )
    with _boundary_lock:
        return _boundary_installations.pop(key, None) is not None


@dataclass(frozen=True)
class BoundaryEvidence:
    """Snapshot of what one installed boundary observed.

    ``trace_class`` is the pre-launch class the installation earned;
    ``effect_refs`` are the bounded resource refs the boundary
    actually observed post-launch (possibly empty -- a zero-effect
    attempt is valid).  Produced only from enforcement-layer records,
    never from caller text.
    """

    attempt_id: str
    run_id: str
    trace_class: TraceCapability
    source_kind: str
    effect_refs: tuple[str, ...]


def boundary_evidence_for(
    *,
    attempt_id: str,
    run_id: str,
    policy: SandboxPolicy,
    grant: ToolGrant,
) -> BoundaryEvidence | None:
    """Return the installed boundary's evidence, or None if absent."""
    key = _installation_key(
        attempt_id=attempt_id, run_id=run_id, policy=policy, grant=grant
    )
    with _boundary_lock:
        installation = _boundary_installations.get(key)
        if installation is None:
            return None
        effects = tuple(
            effect.get("resource_ref", "") for effect in installation.observed_effects
        )
    if installation.native_source_kind is not None:
        trace_class = TraceCapability.NATIVE_STRUCTURED
        source_kind = installation.native_source_kind
    elif installation.has_interceptor:
        trace_class = TraceCapability.SANDBOX_INTERCEPTED
        source_kind = "sandbox_enforcer"
    else:
        return None
    return BoundaryEvidence(
        attempt_id=attempt_id,
        run_id=run_id,
        trace_class=trace_class,
        source_kind=source_kind,
        effect_refs=effects,
    )


# ---------------------------------------------------------------------------
# Dispatch preflight: policy + grant + exposure + trace in one seam.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AutonomousDispatchPreflight:
    """Deterministic evidence that autonomous dispatch was permitted.

    ``trace_class`` is the trace evidence class DERIVED from the
    verified mechanism (never declared by the caller).
    ``backend`` names the enforcement backend that performed preflight.
    """

    trace_class: TraceCapability
    backend: SandboxEnforcementBackend
    policy_digest: str
    grant_digest: str


def resolve_trace_capability(
    *,
    native_events_observed: bool,
    intercepted_effects_recorded: bool,
) -> TraceCapability:
    """Derive the trace class from ACTUAL execution-path evidence.

    * ``NATIVE_STRUCTURED`` -- only when real structured harness events
      were observed for THIS attempt.
    * ``SANDBOX_INTERCEPTED`` -- only when the enforcement layer
      actually recorded tool effects for THIS attempt.
    * ``NONE`` -- neither exists.  Autonomous execution is ineligible.
    """
    if native_events_observed:
        return TraceCapability.NATIVE_STRUCTURED
    if intercepted_effects_recorded:
        return TraceCapability.SANDBOX_INTERCEPTED
    return TraceCapability.NONE


def preflight_autonomous_dispatch(
    *,
    policy: SandboxPolicy,
    grant: ToolGrant,
    exposure: ToolExposurePlan | None,
    trace_mechanism: VerifiedTraceMechanism | None,
    requested_writes: tuple[str, ...] = (),
    requested_executable: str | None = None,
    requested_network_destinations: tuple[str, ...] = (),
    backend: SandboxEnforcementBackend = SandboxEnforcementBackend.OS_PREFLIGHT,
    requires_provider_network: bool = False,
    attempt_id: str | None = None,
    run_id: str | None = None,
) -> AutonomousDispatchPreflight:
    """Fail-closed gate before any autonomous semantic execution.

    Consults the effective ``SandboxPolicy``, ``ToolGrant``,
    ``ToolExposurePlan`` and verified trace mechanism together:

    * ``trace_mechanism`` ``None`` -- or a mechanism that derives
      ``NONE`` for THIS policy + grant (no installed native harness
      path, no installed interceptor) -- refuses.  There is no
      ``trace_class`` parameter: a caller cannot manufacture
      ``SANDBOX_INTERCEPTED`` eligibility by typing an enum or a
      dataclass; only a mechanism installed on this execution path
      (via :func:`attach_sandbox_interceptor` or
      :func:`verify_native_trace_source`) derives it.  When
      ``attempt_id``/``run_id`` are supplied the installation must be
      pinned to THIS attempt.
    * a non-``DENY`` effective network mode refuses on every backend:
      no backend provides the ``NETWORK`` capability, so selective
      egress cannot be enforced -- running anyway would pretend the
      policy is enforced.
    * ``requires_provider_network`` refuses on every backend: a CLI
      backend that inherently needs provider egress cannot be
      separated safely from worker/tool egress under containment, so
      the binding is recorded as not autonomous-eligible instead of
      running with unenforced network isolation.  Local-execution
      backends (no provider egress) pass with ``False``.
    * filesystem mutation (any ``requested_writes`` or any writable
      scope on the policy) requires the ``FILESYSTEM`` capability on
      the selected backend.  ``OS_PREFLIGHT`` does not provide it, so
      mutating dispatch must select ``BWRAP`` -- which in turn fails
      closed when ``bwrap`` is absent from the host (see
      :data:`FILESYSTEM_CONTAINMENT_UNAVAILABLE_MESSAGE`).  On
      ``BWRAP`` the writable scope must additionally be exactly
      enforceable (see :func:`require_exact_writable_scope`):
      unrepresentable scopes fail closed rather than widening a
      directory.
    * every requested write must pass :func:`preflight_write`
      (policy AND grant, symlink-anchored).
    * a requested executable must pass :func:`preflight_executable`.
    * any requested network destination is refused: no backend
      provides the ``NETWORK`` capability, so autonomous execution
      requiring egress fails closed instead of running unsandboxed.
    * ``exposure`` is carried as evidence (mechanism selection cannot
      broaden authority); a ``None`` exposure plan is accepted only
      when no capability-keyed tool dispatch is requested.

    Returns :class:`AutonomousDispatchPreflight` evidence on success.
    """
    capabilities = backend_capabilities(backend)
    trace_class = require_installed_trace_mechanism(
        trace_mechanism,
        policy=policy,
        grant=grant,
        attempt_id=attempt_id,
        run_id=run_id,
    )
    if policy.network.mode != "DENY":
        raise SandboxEnforcementError(
            f"autonomous execution with network mode {policy.network.mode!r} "
            "cannot be enforced by this backend: failing closed"
        )
    if requires_provider_network:
        raise SandboxEnforcementError(
            "autonomous execution ineligible: this binding requires provider "
            "network egress, which cannot be separated safely from worker/tool "
            "egress under this enforcement backend"
        )
    mutating = bool(requested_writes) or policy_is_mutating(policy)
    if mutating and SandboxEnforcementCapability.FILESYSTEM not in capabilities:
        if backend is SandboxEnforcementBackend.OS_PREFLIGHT and not bwrap_available():
            raise SandboxEnforcementError(
                f"{FILESYSTEM_CONTAINMENT_UNAVAILABLE_MESSAGE}: "
                "OS_PREFLIGHT authorises requested paths but cannot prevent "
                "an arbitrary semantic child from opening undeclared paths"
            )
        raise SandboxEnforcementError(
            f"autonomous mutating execution requires FILESYSTEM enforcement; "
            f"backend {backend.value} does not provide it"
        )
    if backend is SandboxEnforcementBackend.BWRAP and not bwrap_available():
        # The BWRAP backend promises kernel containment for every
        # autonomous semantic child -- mutating or read-only (readable
        # scope is itself a security boundary).  Without ``bwrap`` on
        # the host that promise cannot be kept, so dispatch fails
        # closed here, before any semantic child exists.
        if mutating:
            raise SandboxEnforcementError(FILESYSTEM_CONTAINMENT_UNAVAILABLE_MESSAGE)
        raise SandboxEnforcementError(READONLY_CONTAINMENT_UNAVAILABLE_MESSAGE)
    if backend is SandboxEnforcementBackend.BWRAP and mutating:
        require_exact_writable_scope(policy)
    for destination in requested_network_destinations:
        if SandboxEnforcementCapability.NETWORK not in capabilities:
            raise SandboxEnforcementError(
                f"autonomous network egress to {destination!r} cannot be "
                "enforced by this backend: failing closed"
            )
        if not policy.authorize_network(destination):
            raise SandboxEnforcementError(
                f"SandboxPolicy denies network egress to {destination!r}"
            )
    for relpath in requested_writes:
        preflight_write(policy, grant, relpath)
    if requested_executable is not None:
        preflight_executable(policy, [requested_executable])
    return AutonomousDispatchPreflight(
        trace_class=trace_class,
        backend=backend,
        policy_digest=policy.digest,
        grant_digest=grant.digest,
    )


def preflight_as_dict(preflight: AutonomousDispatchPreflight) -> dict[str, Any]:
    """Serialise preflight evidence without secret material."""
    return {
        "trace_class": preflight.trace_class.value,
        "backend": preflight.backend.value,
        "policy_digest": preflight.policy_digest,
        "grant_digest": preflight.grant_digest,
    }


__all__ = [
    "AutonomousDispatchPreflight",
    "BoundaryEvidence",
    "BWRAP_CAPABILITIES",
    "FILESYSTEM_CONTAINMENT_UNAVAILABLE_MESSAGE",
    "READONLY_CONTAINMENT_UNAVAILABLE_MESSAGE",
    "NATIVE_STRUCTURED_ADAPTER_SOURCES",
    "NATIVE_STRUCTURED_VERIFIED_SOURCES",
    "OS_PREFLIGHT_CAPABILITIES",
    "RUNTIME_READONLY_BINDS",
    "SandboxEffectInterceptor",
    "SandboxEnforcementBackend",
    "SandboxEnforcementCapability",
    "SandboxEnforcementError",
    "VerifiedTraceMechanism",
    "attach_sandbox_interceptor",
    "audit_changed_paths",
    "backend_capabilities",
    "boundary_evidence_for",
    "bwrap_available",
    "bwrap_mount_args_for_policy",
    "deny_mask_paths",
    "empty_mask_dir",
    "ensure_sandbox_bind_dirs",
    "filesystem_containment_available",
    "policy_is_mutating",
    "policy_is_repo_writable",
    "preflight_as_dict",
    "preflight_autonomous_dispatch",
    "preflight_executable",
    "preflight_write",
    "readable_bind_paths",
    "record_intercepted_effect",
    "require_exact_writable_scope",
    "require_installed_trace_mechanism",
    "resolve_trace_capability",
    "resolve_within_repo",
    "retire_boundary_installation",
    "runtime_readonly_binds",
    "verify_native_trace_source",
    "writable_bind_dirs",
    "writable_scope_violation",
]
