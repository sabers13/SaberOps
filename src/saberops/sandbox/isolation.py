"""Worker execution isolation: the host-side boundary around one worker.

M4 ("Isolate Workers & Protect Owner Actions") needs a deterministic
answer to a question the sandbox *policy evidence* alone cannot answer:
*may this worker execution touch this host path, run this git command, and
carry this session authority?*

This module is that answer.  It is the enforcement half of worker
isolation and it is deliberately narrow:

* **Worktree containment.**  A worker effect -- a command's ``cwd``, a
  requested write, a path argument -- must resolve (``os.path.realpath``,
  so a symlink such as ``worktree/link -> /etc`` can never smuggle an
  effect out) inside the worker's assigned worktree or inside one of the
  explicitly authorised scratch roots.  Everything else, including the
  owner's home configuration (``~/.config``, ``~/.bashrc``,
  ``~/.gitconfig``, ``~/.ssh``) and ``/etc``, fails closed with a typed
  reason before anything runs.
* **Git control-surface protection.**  A worker may commit, check out and
  inspect, but it may not rewrite the repository control surface: local or
  global ``git config`` writes, ``git remote`` mutations, ``git hook``,
  ``core.hooksPath`` overrides (``git -c``), ``--git-dir``/``--work-tree``
  escapes and path arguments outside the worktree all refuse.  The
  kernel-level counterpart lives in
  :func:`saberops.sandbox.enforcement.bwrap_mount_args_for_policy`, which
  masks ``.git/config`` and ``.git/hooks`` read-only inside the containment
  namespace; this module is the deterministic pre-launch guard for hosts
  (or call sites) without containment and for non-argv effects.
* **Session authority.**  A worker session carries an explicit,
  unescalatable session authority marker (:data:`SESSION_AUTHORITY_ENV`).
  The marker is consumed by :mod:`saberops.owner_actions`, which requires
  owner authority and fails closed when the marker says ``worker``.  A
  worker may never *claim* owner authority, and an unknown marker value
  fails closed rather than defaulting to owner.
* **No ambient host configuration.**  :func:`worker_session_env` builds the
  child environment a worker must run with: the worker marker, a scratch
  ``HOME``, and ``GIT_CONFIG_GLOBAL`` / ``GIT_CONFIG_SYSTEM`` redirected to
  ``/dev/null`` plus ``GIT_CONFIG_NOSYSTEM=1`` so host git configuration is
  neither read nor written even outside containment.

Like the rest of the sandbox package this module never contacts the
provider/model surface, never spawns a process, and never persists durable
state.  Its only host interaction is read-only path resolution
(``os.path.realpath`` / ``os.path.isdir``) so the answers describe the real
host; :func:`preflight_worker_command` is a fail-closed *decision*, and the
launch seam that calls it owns the actual spawn.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, Final

from saberops.sandbox.enforcement import SandboxEnforcementError
from saberops.sandbox.policy import GIT_INTEGRITY_DENY

__all__ = [
    "GIT_INTEGRITY_DENY",
    "HOST_CONFIG_ROOTS",
    "LOOPBACK_HOST_NAMES",
    "OWNER_SESSION_VALUE",
    "SESSION_AUTHORITY_ENV",
    "SYNTHETIC_TEST_HOST",
    "WORKER_SESSION_VALUE",
    "GitInvocationAnalysis",
    "SessionAuthority",
    "WorkerIsolationEvidence",
    "WorkerIsolationPolicy",
    "WorkerIsolationReason",
    "WorkerIsolationViolation",
    "assert_git_command_allowed",
    "assert_worker_write",
    "git_invocation_analysis",
    "is_host_config_path",
    "is_worker_session",
    "loopback_host_allowed",
    "preflight_worker_command",
    "protected_git_paths",
    "resolve_session_authority",
    "resolve_worker_scope",
    "worker_session_env",
]

# ---------------------------------------------------------------------------
# Session authority vocabulary (shared with ``saberops.owner_actions``).
# ---------------------------------------------------------------------------

#: Environment variable that carries the session authority marker.
#:
#: Absent or empty means "the local owner's own process": the Web UI, the
#: ``orch`` CLI and the tests all run as the owner by default, so existing
#: behaviour is preserved.  The worker seam sets
#: :data:`WORKER_SESSION_VALUE` for every worker child it launches, and an
#: *unknown* value never falls back to owner (see
#: :func:`resolve_session_authority`).
SESSION_AUTHORITY_ENV: Final[str] = "SABEROPS_SESSION_AUTHORITY"

#: Marker value for an automated worker session.
WORKER_SESSION_VALUE: Final[str] = "worker"
#: Marker value for an owner (human) session.
OWNER_SESSION_VALUE: Final[str] = "owner"


class SessionAuthority(StrEnum):
    """Who a session claims to be, in the two-valued M4 vocabulary."""

    OWNER = OWNER_SESSION_VALUE
    WORKER = WORKER_SESSION_VALUE


# ---------------------------------------------------------------------------
# Typed refusals.
# ---------------------------------------------------------------------------


class WorkerIsolationReason(StrEnum):
    """The typed reason a worker isolation check refused an effect."""

    #: The path is outside the worker worktree and every scratch root.
    OUTSIDE_WORKTREE = "OUTSIDE_WORKTREE"
    #: A declared scratch root itself is unusable (not absolute, etc.).
    OUTSIDE_SCRATCH_ROOT = "OUTSIDE_SCRATCH_ROOT"
    #: The path is a host/owner configuration path a worker may never touch.
    HOST_CONFIG_MUTATION = "HOST_CONFIG_MUTATION"
    #: The path targets the repository git control surface (config/hooks).
    REPO_GIT_INTEGRITY_MUTATION = "REPO_GIT_INTEGRITY_MUTATION"
    #: The git invocation itself is forbidden (config/hook/remote/escape).
    FORBIDDEN_GIT_INVOCATION = "FORBIDDEN_GIT_INVOCATION"
    #: The requested ``cwd`` is outside the worktree and scratch roots.
    CWD_OUTSIDE_WORKTREE = "CWD_OUTSIDE_WORKTREE"
    #: The session authority marker is present but not a known value.
    UNKNOWN_SESSION_AUTHORITY = "UNKNOWN_SESSION_AUTHORITY"
    #: A worker execution tried to claim owner session authority.
    WORKER_AUTHORITY_ESCALATION = "WORKER_AUTHORITY_ESCALATION"


class WorkerIsolationViolation(SandboxEnforcementError):
    """Fail-closed refusal to perform an effect outside worker isolation.

    Subclasses :class:`saberops.sandbox.enforcement.SandboxEnforcementError`
    so every existing fail-closed handler keeps working, and carries a typed
    :attr:`reason` so callers never parse prose.
    """

    def __init__(self, reason: WorkerIsolationReason, detail: str) -> None:
        super().__init__(f"{reason.value}: {detail}")
        self.reason = reason
        self.detail = detail


# ---------------------------------------------------------------------------
# Owner-path vocabulary.
# ---------------------------------------------------------------------------

#: Home-relative owner configuration a worker may never read or write.
HOST_CONFIG_ROOTS: Final[tuple[str, ...]] = (
    ".config",
    ".bashrc",
    ".bash_profile",
    ".bash_login",
    ".profile",
    ".gitconfig",
    ".git-credentials",
    ".ssh",
    ".gnupg",
    ".netrc",
    ".aws",
    ".docker",
    ".kube",
    ".npmrc",
    ".pypirc",
)

#: Absolute host roots that are always refused regardless of ``$HOME``.
HOST_ABSOLUTE_CONFIG_ROOTS: Final[tuple[str, ...]] = ("/etc", "/root")


def is_host_config_path(path: str | os.PathLike[str], *, home: str | None = None) -> bool:
    """Return True iff ``path`` is owner/host configuration a worker owns.

    The check is realpath-anchored so it never follows a symlink into an
    allowed area to authorise the link's own location.  ``home`` defaults
    to the real ``$HOME`` resolved at call time, so the answer describes
    this host.
    """
    text = os.fspath(path)
    if not text:
        return False
    resolved = os.path.realpath(text)
    for root in HOST_ABSOLUTE_CONFIG_ROOTS:
        if resolved == root or resolved.startswith(root + os.sep):
            return True
    home_text = os.path.realpath(home) if home else os.path.realpath(os.path.expanduser("~"))
    for relative in HOST_CONFIG_ROOTS:
        candidate = os.path.join(home_text, relative)
        if resolved == candidate or resolved.startswith(candidate + os.sep):
            return True
    return False


# ---------------------------------------------------------------------------
# Loopback transport vocabulary (the semantic counterpart of the Web guard).
# ---------------------------------------------------------------------------

#: Host names that name this loopback server directly.  Mirrors the
#: transport allowlist added to ``saberops.web`` by the early Step 020
#: commit so the durable owner-action layer can assert the same
#: loopback-only transport invariant.  ``saberops.web`` remains the
#: transport *enforcement* authority (it is outside this lane's write
#: scope); this module only decides whether a claimed origin is loopback.
LOOPBACK_HOST_NAMES: Final[frozenset[str]] = frozenset({"127.0.0.1", "localhost", "::1"})
#: The synthetic ASGI test server name, accepted only in a synthetic scope.
SYNTHETIC_TEST_HOST: Final[str] = "testserver"


def _split_host_header(raw: str) -> tuple[str, int | None] | None:
    """Split a Host header value into ``(hostname, port)`` or ``None``.

    Host grammar only (optional ``[v6]`` brackets, at most one numeric
    port in 1..65535, no userinfo, no whitespace, no comma list); the
    caller compares the hostname.
    """
    value = raw.strip()
    if not value or "@" in value or " " in value or "\t" in value or "," in value:
        return None
    if value.startswith("["):
        end = value.find("]")
        if end <= 1:
            return None
        hostname = value[1:end].lower()
        rest = value[end + 1 :]
        if not rest:
            return hostname, None
        if not rest.startswith(":") or len(rest) < 2:
            return None
        port_text = rest[1:]
        if not port_text.isdigit():
            return None
        port = int(port_text)
        if not 1 <= port <= 65535:
            return None
        return hostname, port
    if value.count(":") > 1:
        return None
    if ":" in value:
        hostname, _, port_text = value.partition(":")
        if not hostname or not port_text or not port_text.isdigit():
            return None
        port = int(port_text)
        if not 1 <= port <= 65535:
            return None
        return hostname.lower(), port
    return value.lower(), None


def loopback_host_allowed(
    raw_host: str,
    *,
    server_port: int | None = None,
    synthetic_scope: bool = False,
) -> bool:
    """Return True iff ``raw_host`` is an acceptable local-only transport.

    Trusted values name this loopback server directly
    (``127.0.0.1`` / ``localhost`` / ``[::1]``, optional port).  The
    synthetic ``testserver`` name is accepted only when the caller states
    it is inside a synthetic test scope; a malformed, foreign, unbracketed
    IPv6 or comma-listed Host is refused.  ``X-Forwarded-Host`` and similar
    proxy headers are never consulted.
    """
    parsed = _split_host_header(raw_host)
    if parsed is None:
        return False
    hostname, port = parsed
    if hostname == SYNTHETIC_TEST_HOST:
        if not synthetic_scope:
            return False
    elif hostname not in LOOPBACK_HOST_NAMES:
        return False
    if port is not None and server_port is not None and port != server_port:
        return False
    return True


# ---------------------------------------------------------------------------
# Session authority resolution.
# ---------------------------------------------------------------------------


def resolve_session_authority(env: Mapping[str, str] | None = None) -> SessionAuthority:
    """Resolve the effective session authority, failing closed.

    * unset / empty -> :attr:`SessionAuthority.OWNER` (the local owner's own
      Web UI, CLI or test process: unchanged behaviour);
    * ``worker`` -> :attr:`SessionAuthority.WORKER`;
    * ``owner`` -> :attr:`SessionAuthority.OWNER`;
    * anything else -> :class:`WorkerIsolationViolation` with
      :attr:`WorkerIsolationReason.UNKNOWN_SESSION_AUTHORITY`.  An unknown
      marker is never interpreted as owner.
    """
    source = env if env is not None else os.environ
    raw = (source.get(SESSION_AUTHORITY_ENV) or "").strip().lower()
    if not raw:
        return SessionAuthority.OWNER
    try:
        return SessionAuthority(raw)
    except ValueError as exc:
        raise WorkerIsolationViolation(
            WorkerIsolationReason.UNKNOWN_SESSION_AUTHORITY,
            f"{SESSION_AUTHORITY_ENV}={raw!r} is not a known session authority",
        ) from exc


def is_worker_session(env: Mapping[str, str] | None = None) -> bool:
    """Return True iff the effective session authority is a worker."""
    return resolve_session_authority(env) is SessionAuthority.WORKER


# ---------------------------------------------------------------------------
# Worker isolation policy.
# ---------------------------------------------------------------------------

#: Repository-relative control-surface paths a worker may never mutate.
_WORKER_GIT_INTEGRITY_DENY: Final[tuple[str, ...]] = GIT_INTEGRITY_DENY


def _has_glob(pattern: str) -> bool:
    return any(char in pattern for char in "*?[{")


@dataclass(frozen=True)
class WorkerIsolationPolicy:
    """The deterministic isolation contract for one worker execution.

    ``worktree_root`` is the worker's assigned worktree (absolute).  Every
    worker effect must land inside it or inside one of ``scratch_roots``.
    ``scratch_roots`` are the only other writable roots a worker may use
    (temporary build output, a pytest ``--basetemp``, and the like); they
    never expose the worktree's git control surface and host/owner
    configuration paths are refused even inside them.
    """

    worktree_root: str
    scratch_roots: tuple[str, ...] = ()
    protect_git_integrity: bool = True

    def __post_init__(self) -> None:
        if not self.worktree_root or not isinstance(self.worktree_root, str):
            raise SandboxEnforcementError(
                "WorkerIsolationPolicy.worktree_root must be a non-empty string"
            )
        if not os.path.isabs(self.worktree_root):
            raise SandboxEnforcementError(
                f"WorkerIsolationPolicy.worktree_root must be absolute: {self.worktree_root!r}"
            )
        normalised: list[str] = []
        for root in self.scratch_roots:
            if not root or not isinstance(root, str) or not os.path.isabs(root):
                raise SandboxEnforcementError(
                    f"WorkerIsolationPolicy scratch root must be absolute: {root!r}"
                )
            normalised.append(os.path.realpath(root))
        object.__setattr__(self, "scratch_roots", tuple(sorted(set(normalised))))
        object.__setattr__(self, "worktree_root", os.path.realpath(self.worktree_root))

    @property
    def real_worktree_root(self) -> str:
        """The realpath-anchored worktree root."""
        return os.path.realpath(self.worktree_root)

    def location_of(self, resolved: str) -> WorkerIsolationReason | None:
        """Return ``None`` when ``resolved`` is in scope, else the refusal reason."""
        root = self.real_worktree_root
        if resolved == root or resolved.startswith(root + os.sep):
            return None
        for scratch in self.scratch_roots:
            if resolved == scratch or resolved.startswith(scratch + os.sep):
                return None
        return WorkerIsolationReason.OUTSIDE_WORKTREE

    def authorize_path(self, path: str | os.PathLike[str]) -> bool:
        """Return True iff ``path`` is inside the worktree or a scratch root."""
        return self.location_of(os.path.realpath(os.fspath(path))) is None

    def protected_git_paths(self) -> tuple[str, ...]:
        """Absolute ``.git`` control-surface paths protected for this worktree.

        Only literal paths (never globs) are returned.  The bwrap seam
        masks them read-only when ``.git`` is a real directory, and the
        argv guard refuses the corresponding commands on every host.
        """
        if not self.protect_git_integrity:
            return ()
        paths: list[str] = []
        for pattern in _WORKER_GIT_INTEGRITY_DENY:
            if pattern.endswith("/**"):
                paths.append(os.path.join(self.real_worktree_root, pattern[: -len("/**")]))
            elif not _has_glob(pattern):
                paths.append(os.path.join(self.real_worktree_root, pattern))
        return tuple(sorted(set(paths)))

    def as_dict(self) -> dict[str, Any]:
        """Serialise the contract without any secret material."""
        return {
            "worktree_root": self.worktree_root,
            "scratch_roots": list(self.scratch_roots),
            "protect_git_integrity": self.protect_git_integrity,
        }

    @property
    def digest(self) -> str:
        """Deterministic digest of the contract for evidence."""
        payload = json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def protected_git_paths(policy: WorkerIsolationPolicy) -> tuple[str, ...]:
    """Module-level accessor for :meth:`WorkerIsolationPolicy.protected_git_paths`."""
    return policy.protected_git_paths()


# ---------------------------------------------------------------------------
# Path containment enforcement.
# ---------------------------------------------------------------------------


def _contains_parent_traversal(path: str) -> bool:
    return any(part == ".." for part in PurePosixPath(path.replace("\\", "/")).parts)


def _candidate_path(policy: WorkerIsolationPolicy, path: str | os.PathLike[str]) -> str:
    """Return the realpath-resolved candidate for ``path`` (no scope test)."""
    raw = os.fspath(path)
    if not raw:
        raise WorkerIsolationViolation(
            WorkerIsolationReason.OUTSIDE_WORKTREE, "empty path is not a worker path"
        )
    if _contains_parent_traversal(raw):
        raise WorkerIsolationViolation(
            WorkerIsolationReason.OUTSIDE_WORKTREE,
            f"path {raw!r} contains parent traversal",
        )
    candidate = raw if os.path.isabs(raw) else os.path.join(policy.real_worktree_root, raw)
    return os.path.realpath(candidate)


def resolve_worker_scope(
    policy: WorkerIsolationPolicy,
    path: str | os.PathLike[str],
) -> str:
    """Resolve ``path`` and prove it stays inside the worker scope.

    Relative paths resolve against the worktree root; absolute paths must
    already be inside the worktree or a scratch root.  Symlinks are
    resolved before the containment test, so ``worktree/link -> /etc``
    resolves outside and refuses.  Raises
    :class:`WorkerIsolationViolation` otherwise.
    """
    raw = os.fspath(path)
    resolved = _candidate_path(policy, path)
    reason = policy.location_of(resolved)
    if reason is not None:
        raise WorkerIsolationViolation(
            reason,
            f"path {raw!r} resolves to {resolved!r}, outside worktree "
            f"{policy.real_worktree_root!r} and its scratch roots",
        )
    return resolved


def assert_worker_write(
    policy: WorkerIsolationPolicy,
    path: str | os.PathLike[str],
) -> str:
    """Fail closed unless a worker may write the resolved ``path``.

    Owner/host configuration is checked first, so a request for
    ``/etc/gitconfig`` or ``~/.bashrc`` reports the specific
    :attr:`WorkerIsolationReason.HOST_CONFIG_MUTATION` reason instead of
    the generic containment refusal.  The repository git control surface
    then refuses with
    :attr:`WorkerIsolationReason.REPO_GIT_INTEGRITY_MUTATION`.  Returns
    the resolved absolute path on success.
    """
    resolved = _candidate_path(policy, path)
    if is_host_config_path(resolved):
        raise WorkerIsolationViolation(
            WorkerIsolationReason.HOST_CONFIG_MUTATION,
            f"worker may not write host/owner configuration: {resolved!r}",
        )
    reason = policy.location_of(resolved)
    if reason is not None:
        raise WorkerIsolationViolation(
            reason,
            f"path {os.fspath(path)!r} resolves to {resolved!r}, outside worktree "
            f"{policy.real_worktree_root!r} and its scratch roots",
        )
    for protected in policy.protected_git_paths():
        if resolved == protected or resolved.startswith(protected + os.sep):
            raise WorkerIsolationViolation(
                WorkerIsolationReason.REPO_GIT_INTEGRITY_MUTATION,
                f"worker may not mutate the repository git control surface: {resolved!r}",
            )
    return resolved


# ---------------------------------------------------------------------------
# Git command guard.
# ---------------------------------------------------------------------------

#: Git global options whose value names a path and can move the command
#: outside the worker's assigned scope.
_GIT_SCOPING_OPTIONS: Final[frozenset[str]] = frozenset({"-C", "--git-dir", "--work-tree"})
#: Git global options that name an executable or a remote command endpoint.
_GIT_FORBIDDEN_GLOBAL_OPTIONS: Final[frozenset[str]] = frozenset(
    {"--exec-path", "--upload-pack", "--receive-pack"}
)
#: ``git config`` read-only verbs; anything else is a write or an escape.
_GIT_CONFIG_READ_FLAGS: Final[frozenset[str]] = frozenset(
    {"--get", "--get-all", "--get-regexp", "--get-urlmatch", "--get-color",
     "--get-colorbool", "--list", "-l"}
)
#: ``git config`` flags that target config outside the worker's repository.
_GIT_CONFIG_FORBIDDEN_FLAGS: Final[frozenset[str]] = frozenset(
    {"--global", "--system", "--file", "-f", "--blob", "--edit", "-e"}
)
#: ``git config`` mutation verbs.
_GIT_CONFIG_WRITE_FLAGS: Final[frozenset[str]] = frozenset(
    {"--add", "--unset", "--unset-all", "--replace-all", "--remove-section",
     "--rename-section"}
)
#: Subcommands that rewrite the control surface, run hooks, or expose
#: credentials.
_GIT_FORBIDDEN_SUBCOMMANDS: Final[frozenset[str]] = frozenset(
    {"hook", "filter-branch", "filter-repo", "credential", "credential-cache",
     "credential-store", "daemon", "instaweb", "web--browse", "difftool",
     "mergetool", "worktree", "update-server-info", "send-email", "imap-send"}
)
#: ``git remote`` verbs that mutate ``.git/config`` (reads stay allowed).
_GIT_REMOTE_WRITE_VERBS: Final[frozenset[str]] = frozenset(
    {"add", "remove", "rm", "rename", "set-url", "set-head", "set-branches",
     "prune", "update"}
)
#: ``git submodule`` verbs that write ``submodule.*`` entries into
#: ``.git/config`` or mutate a submodule working tree (reads stay allowed).
_GIT_SUBMODULE_WRITE_VERBS: Final[frozenset[str]] = frozenset(
    {"add", "init", "update", "deinit", "sync", "set-url", "set-branch",
     "absorbgitdirs"}
)
#: Options that write branch upstream configuration into ``.git/config``.
_GIT_UPSTREAM_CONFIG_OPTIONS: Final[frozenset[str]] = frozenset(
    {"--set-upstream", "--set-upstream-to", "--unset-upstream"}
)
#: Subcommands where the short ``-u`` form sets the upstream.
_GIT_UPSTREAM_SHORT_SUBCOMMANDS: Final[frozenset[str]] = frozenset({"branch", "push"})
#: Subcommands that accept ``--template`` / ``--config`` and could install hooks.
_GIT_TEMPLATE_SUBCOMMANDS: Final[frozenset[str]] = frozenset({"init", "clone"})
#: Options whose following bare token is data, not a path (commit messages,
#: grep patterns, formatting directives).  Used to avoid false positives in
#: the path-argument scan.
_GIT_INLINE_DATA_OPTIONS: Final[frozenset[str]] = frozenset(
    {"-m", "--message", "--author", "--committer", "--grep",
     "--grep-reflog", "--format", "--pretty", "--date", "-S", "-n", "-o",
     "--output", "--subject", "--cleanup", "--reflog-message"}
)


#: Inherited environment variables that inject git configuration for the
#: current process only; a worker must not inherit them (they can override
#: ``core.hooksPath`` without touching any file).
_GIT_CONFIG_INJECTION_ENV: Final[frozenset[str]] = frozenset(
    {"GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS"}
)


@dataclass(frozen=True)
class GitInvocationAnalysis:
    """Deterministic analysis of one git argv against worker isolation."""

    is_git: bool
    subcommand: str | None
    allowed: bool
    reason: WorkerIsolationReason | None = None
    detail: str = ""


def _is_git_binary(token: str) -> bool:
    return PurePosixPath(token.replace("\\", "/")).name == "git"


def _refuse_git(
    detail: str, subcommand: str | None = None
) -> GitInvocationAnalysis:
    return GitInvocationAnalysis(
        is_git=True,
        subcommand=subcommand,
        allowed=False,
        reason=WorkerIsolationReason.FORBIDDEN_GIT_INVOCATION,
        detail=detail,
    )


def _scan_git_global_options(
    argv: list[str], policy: WorkerIsolationPolicy
) -> tuple[int, GitInvocationAnalysis | None]:
    """Walk git global options; return the subcommand index or a refusal."""
    index = 1
    while index < len(argv):
        token = argv[index]
        if token == "--":
            index += 1
            continue
        if not token.startswith("-") or token == "-":
            return index, None
        base = token.split("=", 1)[0]
        if base in ("-c", "--config-env"):
            return index, _refuse_git(
                f"inline config override {token!r} is not permitted for a worker "
                "(it can redirect core.hooksPath / core.pager)"
            )
        if base in _GIT_FORBIDDEN_GLOBAL_OPTIONS:
            return index, _refuse_git(
                f"git global option {token!r} is not permitted for a worker"
            )
        if base in _GIT_SCOPING_OPTIONS:
            value = token.partition("=")[2] or None
            if value is None:
                index += 1
                if index >= len(argv):
                    return index, _refuse_git(
                        f"git global option {base!r} requires a value"
                    )
                value = argv[index]
            try:
                # Naming a path inside the worker scope (including the
                # worktree's own git dir) is not an escape; anything else
                # would move the command outside its assigned scope.
                resolve_worker_scope(policy, value)
            except WorkerIsolationViolation as exc:
                return index, _refuse_git(
                    f"git global option {base!r} escapes the worker scope: {exc.detail}"
                )
        index += 1
    return index, None


def _scan_git_path_arguments(
    argv: list[str], policy: WorkerIsolationPolicy
) -> GitInvocationAnalysis | None:
    """Refuse a git argument that names a path escaping the worker scope.

    The scan is left-to-right and skips the operand of known data-taking
    options (``-m``, ``--grep`` ...) so a commit message is never mistaken
    for a path.  A bare token is only treated as a path when it is absolute
    or follows an explicit ``--`` separator.
    """
    skip_next = False
    after_separator = False
    for token in argv[1:]:
        if skip_next:
            skip_next = False
            continue
        if token == "--":
            after_separator = True
            continue
        if token.startswith("-"):
            base = token.split("=", 1)[0]
            if base in _GIT_INLINE_DATA_OPTIONS and "=" not in token:
                skip_next = True
            continue
        candidate: str | None = None
        if os.path.isabs(token):
            candidate = token
        elif after_separator:
            candidate = token
        elif token.startswith(".."):
            candidate = token
        if candidate is None:
            continue
        if _contains_parent_traversal(candidate):
            return _refuse_git(
                f"git path argument {token!r} traverses outside the worker scope"
            )
        try:
            resolve_worker_scope(policy, candidate)
        except WorkerIsolationViolation as exc:
            if os.path.isabs(candidate) or after_separator:
                return _refuse_git(
                    f"git path argument {token!r} escapes the worker scope: {exc.detail}"
                )
    return None


def _analyse_git_config(subcommand: str, trailing: list[str]) -> GitInvocationAnalysis:
    """``git config`` may only *read* config, and never config outside the repo."""
    for token in trailing:
        base = token.split("=", 1)[0]
        if base in _GIT_CONFIG_FORBIDDEN_FLAGS:
            return _refuse_git(
                f"git config {base} targets configuration outside the worker's repository",
                subcommand,
            )
        if base in _GIT_CONFIG_WRITE_FLAGS:
            return _refuse_git(f"git config {base} mutates configuration", subcommand)
    reads = any(token.split("=", 1)[0] in _GIT_CONFIG_READ_FLAGS for token in trailing)
    if not reads:
        positional = [token for token in trailing if not token.startswith("-")]
        return _refuse_git(
            "git config may only read in a worker session "
            f"(positional args: {positional!r})",
            subcommand,
        )
    return GitInvocationAnalysis(is_git=True, subcommand=subcommand, allowed=True)


def git_invocation_analysis(
    argv: list[str] | tuple[str, ...],
    policy: WorkerIsolationPolicy,
) -> GitInvocationAnalysis:
    """Analyse ``argv`` and decide whether worker isolation permits it.

    Non-git argv is always allowed here: this guard governs git's control
    surface only.  Other commands are governed by path containment, the
    hardened worker environment and the effective ``SandboxPolicy``.
    """
    tokens = list(argv)
    if not tokens or not _is_git_binary(tokens[0]):
        return GitInvocationAnalysis(is_git=False, subcommand=None, allowed=True)
    index, refusal = _scan_git_global_options(tokens, policy)
    if refusal is not None:
        return refusal
    if index >= len(tokens):
        # ``git --version`` / ``git --help`` and friends: read-only.
        return GitInvocationAnalysis(is_git=True, subcommand=None, allowed=True)
    subcommand = tokens[index]
    trailing = tokens[index + 1 :]
    for token in trailing:
        if token.split("=", 1)[0] in _GIT_UPSTREAM_CONFIG_OPTIONS:
            return _refuse_git(
                f"git {subcommand} {token.split('=', 1)[0]} writes branch upstream "
                "configuration into .git/config",
                subcommand,
            )
    if subcommand in _GIT_UPSTREAM_SHORT_SUBCOMMANDS and "-u" in trailing:
        return _refuse_git(
            f"git {subcommand} -u writes branch upstream configuration into .git/config",
            subcommand,
        )
    if subcommand in _GIT_FORBIDDEN_SUBCOMMANDS:
        return _refuse_git(f"git {subcommand} is not permitted for a worker", subcommand)
    if subcommand == "config":
        return _analyse_git_config(subcommand, trailing)
    if subcommand == "remote":
        verb = next((token for token in trailing if not token.startswith("-")), None)
        if verb in _GIT_REMOTE_WRITE_VERBS:
            return _refuse_git(
                f"git remote {verb} rewrites the repository control surface", subcommand
            )
    if subcommand == "submodule":
        verb = next((token for token in trailing if not token.startswith("-")), None)
        if verb in _GIT_SUBMODULE_WRITE_VERBS:
            return _refuse_git(
                f"git submodule {verb} writes submodule configuration into .git/config",
                subcommand,
            )
    if subcommand in _GIT_TEMPLATE_SUBCOMMANDS:
        for token in trailing:
            base = token.split("=", 1)[0]
            if base == "--template":
                return _refuse_git(
                    f"git {subcommand} {base} could install hooks", subcommand
                )
            if base in ("-c", "--config"):
                return _refuse_git(
                    f"git {subcommand} {base} is not permitted for a worker", subcommand
                )
    escape = _scan_git_path_arguments(tokens[index:], policy)
    if escape is not None:
        return escape
    return GitInvocationAnalysis(is_git=True, subcommand=subcommand, allowed=True)


def assert_git_command_allowed(
    argv: list[str] | tuple[str, ...],
    policy: WorkerIsolationPolicy,
) -> GitInvocationAnalysis:
    """Fail closed unless worker isolation permits the git invocation."""
    analysis = git_invocation_analysis(argv, policy)
    if not analysis.allowed:
        raise WorkerIsolationViolation(
            analysis.reason or WorkerIsolationReason.FORBIDDEN_GIT_INVOCATION,
            analysis.detail,
        )
    return analysis


# ---------------------------------------------------------------------------
# Worker session environment.
# ---------------------------------------------------------------------------


def worker_session_env(
    base_env: Mapping[str, str] | None = None,
    *,
    scratch_root: str | None = None,
) -> dict[str, str]:
    """Build the environment a worker execution must run with.

    * sets :data:`SESSION_AUTHORITY_ENV` to ``worker`` (never owner);
    * refuses when ``base_env`` explicitly claims owner authority: a worker
      context may never be escalated into an owner context;
    * redirects host git configuration so ``~/.gitconfig`` /
      ``/etc/gitconfig`` can be neither read nor written
      (``GIT_CONFIG_GLOBAL`` / ``GIT_CONFIG_SYSTEM`` to ``/dev/null`` plus
      ``GIT_CONFIG_NOSYSTEM=1``);
    * points ``HOME`` and ``XDG_CONFIG_HOME`` at ``scratch_root`` when
      supplied, so tools that reach for ``~/.config`` land in the worker's
      own scratch area rather than the owner's home.
    """
    source: dict[str, str] = dict(base_env) if base_env is not None else dict(os.environ)
    declared = (source.get(SESSION_AUTHORITY_ENV) or "").strip().lower()
    if declared == OWNER_SESSION_VALUE:
        raise WorkerIsolationViolation(
            WorkerIsolationReason.WORKER_AUTHORITY_ESCALATION,
            f"a worker session may not claim {SESSION_AUTHORITY_ENV}={OWNER_SESSION_VALUE!r}",
        )
    if declared and declared != WORKER_SESSION_VALUE:
        raise WorkerIsolationViolation(
            WorkerIsolationReason.UNKNOWN_SESSION_AUTHORITY,
            f"{SESSION_AUTHORITY_ENV}={declared!r} is not a known session authority",
        )
    # Drop inherited per-process git config injection (``GIT_CONFIG_COUNT`` /
    # ``GIT_CONFIG_KEY_n`` / ``GIT_CONFIG_VALUE_n`` / ``GIT_CONFIG_PARAMETERS``).
    # ``GIT_CONFIG_GLOBAL`` / ``GIT_CONFIG_SYSTEM`` are re-set below.
    for key in list(source):
        if key in _GIT_CONFIG_INJECTION_ENV or (
            key.startswith("GIT_CONFIG_KEY_") or key.startswith("GIT_CONFIG_VALUE_")
        ):
            del source[key]
    if scratch_root is not None:
        if not os.path.isabs(scratch_root):
            raise WorkerIsolationViolation(
                WorkerIsolationReason.OUTSIDE_SCRATCH_ROOT,
                f"scratch root must be absolute: {scratch_root!r}",
            )
        real_scratch = os.path.realpath(scratch_root)
        source["HOME"] = real_scratch
        source["XDG_CONFIG_HOME"] = os.path.join(real_scratch, ".config")
    source[SESSION_AUTHORITY_ENV] = WORKER_SESSION_VALUE
    source["GIT_CONFIG_GLOBAL"] = os.devnull
    source["GIT_CONFIG_SYSTEM"] = os.devnull
    source["GIT_CONFIG_NOSYSTEM"] = "1"
    source["GIT_TERMINAL_PROMPT"] = "0"
    return source


# ---------------------------------------------------------------------------
# The worker execution seam preflight.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkerIsolationEvidence:
    """Deterministic evidence that one worker execution passed isolation."""

    worktree_root: str
    scratch_roots: tuple[str, ...]
    session_authority: SessionAuthority
    argv0_basename: str
    git_subcommand: str | None
    protected_git_paths: tuple[str, ...]
    policy_digest: str

    def as_dict(self) -> dict[str, Any]:
        """Serialise without secret material."""
        return {
            "worktree_root": self.worktree_root,
            "scratch_roots": list(self.scratch_roots),
            "session_authority": self.session_authority.value,
            "argv0_basename": self.argv0_basename,
            "git_subcommand": self.git_subcommand,
            "protected_git_paths": list(self.protected_git_paths),
            "policy_digest": self.policy_digest,
        }


def preflight_worker_command(
    policy: WorkerIsolationPolicy,
    *,
    argv: list[str] | tuple[str, ...],
    cwd: str | os.PathLike[str] | None = None,
    requested_paths: tuple[str | os.PathLike[str], ...] = (),
    env: Mapping[str, str] | None = None,
) -> WorkerIsolationEvidence:
    """Fail-closed preflight for one worker execution.

    Order of checks (all before any child process exists):

    1. **Session authority.**  An ``env`` that explicitly claims owner
       authority refuses with
       :attr:`WorkerIsolationReason.WORKER_AUTHORITY_ESCALATION`; an
       unknown marker refuses with
       :attr:`WorkerIsolationReason.UNKNOWN_SESSION_AUTHORITY`.  A worker
       command always runs as a worker.
    2. **Working directory.**  ``cwd`` must resolve inside the worktree or
       a scratch root.
    3. **Requested paths.**  Every requested path must pass
       :func:`assert_worker_write` (containment + host config + git
       control surface).
    4. **Git invocation.**  When ``argv`` runs git,
       :func:`assert_git_command_allowed` decides.

    Returns :class:`WorkerIsolationEvidence` on success.
    """
    source = env if env is not None else os.environ
    declared = (source.get(SESSION_AUTHORITY_ENV) or "").strip().lower()
    if declared == OWNER_SESSION_VALUE:
        raise WorkerIsolationViolation(
            WorkerIsolationReason.WORKER_AUTHORITY_ESCALATION,
            f"worker execution may not run with {SESSION_AUTHORITY_ENV}={OWNER_SESSION_VALUE!r}",
        )
    if declared and declared != WORKER_SESSION_VALUE:
        raise WorkerIsolationViolation(
            WorkerIsolationReason.UNKNOWN_SESSION_AUTHORITY,
            f"{SESSION_AUTHORITY_ENV}={declared!r} is not a known session authority",
        )
    if cwd is not None:
        try:
            resolve_worker_scope(policy, cwd)
        except WorkerIsolationViolation as exc:
            if exc.reason is WorkerIsolationReason.OUTSIDE_WORKTREE:
                raise WorkerIsolationViolation(
                    WorkerIsolationReason.CWD_OUTSIDE_WORKTREE, exc.detail
                ) from exc
            raise
    for requested in requested_paths:
        assert_worker_write(policy, requested)
    tokens = list(argv)
    if not tokens:
        raise WorkerIsolationViolation(
            WorkerIsolationReason.FORBIDDEN_GIT_INVOCATION,
            "worker preflight requires a non-empty argv",
        )
    git_analysis = assert_git_command_allowed(tokens, policy)
    argv0_basename = PurePosixPath(tokens[0].replace("\\", "/")).name
    return WorkerIsolationEvidence(
        worktree_root=policy.worktree_root,
        scratch_roots=policy.scratch_roots,
        session_authority=SessionAuthority.WORKER,
        argv0_basename=argv0_basename,
        git_subcommand=git_analysis.subcommand if git_analysis.is_git else None,
        protected_git_paths=policy.protected_git_paths(),
        policy_digest=policy.digest,
    )
