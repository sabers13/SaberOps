"""Adversarial worker-isolation tests (Step 020 / M4).

The sandbox -- not the worker prompt -- is the enforcement boundary.  These
tests attack that boundary from a worker's point of view and require every
attempt to fail closed with a *typed* reason before anything runs:

* a write, a ``cwd`` or a path argument resolving outside the assigned
  worktree (including through a symlink or ``..`` traversal);
* a write to owner/host configuration (``~/.config``, ``~/.bashrc``,
  ``~/.gitconfig``, ``/etc``);
* a write to the repository git control surface (``.git/config``,
  ``.git/hooks``);
* a git invocation that would rewrite that control surface
  (``git config`` writes, ``git remote`` mutations, ``git hook``,
  ``git -c``, ``--git-dir`` escapes, path arguments outside the worktree);
* a worker session claiming owner authority, or an unknown session
  authority marker silently defaulting to owner.

The kernel-level half is also covered: the bubblewrap mount computation
masks ``.git/config`` and ``.git/hooks`` read-only, a real contained child
cannot write them, and the mask computation skips targets that can never
exist (a ``.git`` *file* in a linked worktree) instead of breaking launch.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from saberops.sandbox.enforcement import (
    SandboxEnforcementError,
    bwrap_available,
    bwrap_mount_args_for_policy,
    empty_mask_dir,
    empty_mask_file,
    git_integrity_mask_targets,
)
from saberops.sandbox.isolation import (
    GIT_INTEGRITY_DENY,
    HOST_CONFIG_ROOTS,
    SESSION_AUTHORITY_ENV,
    WORKER_SESSION_VALUE,
    SessionAuthority,
    WorkerIsolationPolicy,
    WorkerIsolationReason,
    WorkerIsolationViolation,
    assert_git_command_allowed,
    assert_worker_write,
    git_invocation_analysis,
    is_host_config_path,
    is_worker_session,
    loopback_host_allowed,
    preflight_worker_command,
    resolve_session_authority,
    resolve_worker_scope,
    worker_session_env,
)
from saberops.sandbox.policy import SandboxPolicy

# ---------------------------------------------------------------------------
# Fixtures / helpers.
# ---------------------------------------------------------------------------


def _worktree(tmp_path: Path, name: str = "wt") -> Path:
    """Build a worktree-shaped directory with a real ``.git`` directory."""
    root = tmp_path / name
    (root / ".git" / "hooks").mkdir(parents=True)
    (root / ".git" / "config").write_text("[core]\n\trepositoryformatversion = 0\n")
    (root / ".git" / "refs" / "heads").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("print('ok')\n")
    return root


def _policy(worktree: Path, *scratch: Path) -> WorkerIsolationPolicy:
    return WorkerIsolationPolicy(
        worktree_root=str(worktree),
        scratch_roots=tuple(str(path) for path in scratch),
    )


def _reason_of(
    call: Callable[..., object], *args: object, **kwargs: object
) -> WorkerIsolationReason:
    """Run ``call`` expecting a typed refusal and return its reason."""
    with pytest.raises(WorkerIsolationViolation) as info:
        call(*args, **kwargs)
    return info.value.reason


# ---------------------------------------------------------------------------
# 1. Worktree containment.
# ---------------------------------------------------------------------------


def test_write_inside_worktree_is_allowed(tmp_path: Path) -> None:
    """Covers: M4 invariant 'worker writes stay inside the assigned worktree'."""
    worktree = _worktree(tmp_path)
    resolved = assert_worker_write(_policy(worktree), "src/new_module.py")
    assert resolved == str(worktree / "src" / "new_module.py")


def test_write_outside_worktree_is_refused(tmp_path: Path) -> None:
    """Covers: M4 invariant 'cannot touch files outside their worktree'."""
    worktree = _worktree(tmp_path)
    outside = tmp_path / "other-repo" / "file.txt"
    reason = _reason_of(assert_worker_write, _policy(worktree), outside)
    assert reason is WorkerIsolationReason.OUTSIDE_WORKTREE


def test_parent_traversal_is_refused(tmp_path: Path) -> None:
    """Covers: threat-model path traversal escape from the worktree."""
    worktree = _worktree(tmp_path)
    for candidate in ("../escape.txt", "src/../../escape.txt", ".."):
        reason = _reason_of(assert_worker_write, _policy(worktree), candidate)
        assert reason is WorkerIsolationReason.OUTSIDE_WORKTREE


def test_symlink_escape_is_refused(tmp_path: Path) -> None:
    """Covers: symlink cannot launder an out-of-worktree write."""
    worktree = _worktree(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (worktree / "link").symlink_to(outside, target_is_directory=True)
    reason = _reason_of(assert_worker_write, _policy(worktree), "link/leak.txt")
    assert reason is WorkerIsolationReason.OUTSIDE_WORKTREE
    assert not (outside / "leak.txt").exists()


def test_scratch_root_is_allowed(tmp_path: Path) -> None:
    """Covers: prompt section 'authorised temporary scratch roots'."""
    worktree = _worktree(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    policy = _policy(worktree, scratch)
    resolved = assert_worker_write(policy, str(scratch / "artifacts" / "out.bin"))
    assert resolved.startswith(str(scratch))
    # A sibling that is not declared stays out of scope.
    reason = _reason_of(assert_worker_write, policy, tmp_path / "elsewhere" / "x")
    assert reason is WorkerIsolationReason.OUTSIDE_WORKTREE


def test_relative_path_resolves_against_worktree(tmp_path: Path) -> None:
    """Covers: M4 invariant 'relative worker paths are worktree-relative'."""
    worktree = _worktree(tmp_path)
    policy = _policy(worktree)
    assert resolve_worker_scope(policy, ".") == policy.real_worktree_root
    assert resolve_worker_scope(policy, "src/app.py") == str(worktree / "src" / "app.py")


def test_empty_path_is_refused(tmp_path: Path) -> None:
    """Covers: fail-closed on a missing path in the worker seam."""
    policy = _policy(_worktree(tmp_path))
    assert _reason_of(resolve_worker_scope, policy, "") is WorkerIsolationReason.OUTSIDE_WORKTREE


def test_worker_isolation_policy_rejects_invalid_roots(tmp_path: Path) -> None:
    """Covers: contract validation for the isolation contract itself."""
    with pytest.raises(SandboxEnforcementError):
        WorkerIsolationPolicy(worktree_root="relative/path")
    with pytest.raises(SandboxEnforcementError):
        WorkerIsolationPolicy(worktree_root=str(tmp_path), scratch_roots=("relative",))


# ---------------------------------------------------------------------------
# 2. Owner / host configuration.
# ---------------------------------------------------------------------------


def test_host_config_paths_are_recognised(tmp_path: Path) -> None:
    """Covers: prompt section 'alter global user/host configuration'."""
    home = tmp_path / "home"
    for relative in HOST_CONFIG_ROOTS:
        assert is_host_config_path(home / relative, home=str(home))
        assert is_host_config_path(home / relative / "nested" / "file", home=str(home))
    assert not is_host_config_path(tmp_path / "project" / "src.py", home=str(home))
    assert is_host_config_path("/etc/shadow")
    assert is_host_config_path("/etc/gitconfig")
    assert is_host_config_path("/root/.bashrc")


def test_host_config_write_is_refused_with_typed_reason(tmp_path: Path) -> None:
    """Covers: M4 invariant 'cannot alter global user configuration'."""
    worktree = _worktree(tmp_path)
    policy = _policy(worktree, tmp_path)
    # ``/etc`` is refused regardless of scope declarations.
    assert _reason_of(
        assert_worker_write, policy, "/etc/passwd"
    ) is WorkerIsolationReason.HOST_CONFIG_MUTATION


def test_scratch_root_cannot_expose_host_config(tmp_path: Path) -> None:
    """Covers: threat-model scratch root used to reach host config."""
    worktree = _worktree(tmp_path)
    home = Path(os.path.expanduser("~"))
    policy = WorkerIsolationPolicy(
        worktree_root=str(worktree), scratch_roots=(str(home),)
    )
    reason = _reason_of(assert_worker_write, policy, home / ".bashrc")
    assert reason is WorkerIsolationReason.HOST_CONFIG_MUTATION
    assert is_host_config_path(home / ".gitconfig")


# ---------------------------------------------------------------------------
# 3. Repository git control surface (path level).
# ---------------------------------------------------------------------------


def test_git_control_surface_writes_are_refused(tmp_path: Path) -> None:
    """Covers: M4 invariant 'cannot mutate .git/config or repository hooks'."""
    worktree = _worktree(tmp_path)
    policy = _policy(worktree)
    for target in (
        ".git/config",
        ".git/config.worktree",
        ".git/hooks/pre-commit",
        ".git/hooks",
    ):
        reason = _reason_of(assert_worker_write, policy, target)
        assert reason is WorkerIsolationReason.REPO_GIT_INTEGRITY_MUTATION, target
    # Ordinary git writes stay inside the granted scope.
    assert assert_worker_write(policy, ".git/refs/heads/main").endswith("refs/heads/main")
    assert assert_worker_write(policy, ".git/index").endswith(".git/index")


def test_git_integrity_deny_patterns_are_file_and_dir_shaped() -> None:
    """Covers: deny patterns stay maskable (no glob-widening)."""
    assert ".git/config" in GIT_INTEGRITY_DENY
    assert ".git/hooks/**" in GIT_INTEGRITY_DENY
    assert GIT_INTEGRITY_DENY[0] == ".git/config"


def test_worker_sandbox_policy_denies_git_control_surface(tmp_path: Path) -> None:
    """Covers: M4 deny evidence when a worker SandboxPolicy is built."""
    worktree = _worktree(tmp_path)
    policy = SandboxPolicy(
        repo_root=str(worktree),
        readable_paths=("**",),
        writable_paths=("**",),
        deny_paths=GIT_INTEGRITY_DENY,
    )
    assert not policy.authorize_write(".git/config")
    assert not policy.authorize_write(".git/hooks/pre-commit")
    assert policy.authorize_write(".git/refs/heads/main")
    assert policy.authorize_write("src/app.py")


# ---------------------------------------------------------------------------
# 4. Git command guard.
# ---------------------------------------------------------------------------


def test_git_read_commands_are_allowed(tmp_path: Path) -> None:
    """Covers: M4 'worker may commit, check out and inspect'."""
    policy = _policy(_worktree(tmp_path))
    for argv in (
        ["git", "status", "--short"],
        ["git", "log", "--oneline", "-n", "5"],
        ["git", "diff", "--name-only"],
        ["git", "add", "src/app.py"],
        ["git", "commit", "-m", "Fix: adjust the parser"],
        ["git", "checkout", "-b", "worker/topic"],
        ["git", "config", "--get", "core.repositoryformatversion"],
        ["git", "config", "--list"],
        ["git", "remote", "-v"],
        ["git", "rev-parse", "HEAD"],
    ):
        analysis = git_invocation_analysis(argv, policy)
        assert analysis.allowed, (argv, analysis.detail)
        assert analysis.is_git


def test_git_config_writes_are_refused(tmp_path: Path) -> None:
    """Covers: M4 invariant 'cannot mutate .git/config'."""
    policy = _policy(_worktree(tmp_path))
    for argv in (
        ["git", "config", "user.email", "attacker@example.com"],
        ["git", "config", "--local", "core.hooksPath", "/tmp/evil"],
        ["git", "config", "--global", "user.email", "attacker@example.com"],
        ["git", "config", "--system", "core.hooksPath", "/tmp/evil"],
        ["git", "config", "--file", "/etc/gitconfig", "a", "b"],
        ["git", "config", "--unset", "core.hooksPath"],
        ["git", "config", "--edit"],
    ):
        reason = _reason_of(assert_git_command_allowed, argv, policy)
        assert reason is WorkerIsolationReason.FORBIDDEN_GIT_INVOCATION, argv


def test_git_hook_and_hookpath_are_refused(tmp_path: Path) -> None:
    """Covers: M4 invariant 'cannot tamper with repository hooks'."""
    policy = _policy(_worktree(tmp_path))
    for argv in (
        ["git", "hook", "run", "pre-commit"],
        ["git", "-c", "core.hooksPath=/tmp/evil", "commit", "-m", "x"],
        ["git", "--config-env=core.hooksPath=EVIL", "commit", "-m", "x"],
        ["git", "filter-branch", "--all"],
    ):
        reason = _reason_of(assert_git_command_allowed, argv, policy)
        assert reason is WorkerIsolationReason.FORBIDDEN_GIT_INVOCATION, argv


def test_git_remote_mutations_are_refused(tmp_path: Path) -> None:
    """Covers: M4 invariant 'cannot mutate .git/config' via git remote."""
    policy = _policy(_worktree(tmp_path))
    for argv in (
        ["git", "remote", "add", "origin", "https://evil.test/x.git"],
        ["git", "remote", "set-url", "origin", "https://evil.test/x.git"],
        ["git", "remote", "remove", "origin"],
    ):
        reason = _reason_of(assert_git_command_allowed, argv, policy)
        assert reason is WorkerIsolationReason.FORBIDDEN_GIT_INVOCATION, argv


def test_git_scope_escapes_are_refused(tmp_path: Path) -> None:
    """Covers: threat-model --git-dir / -C / path argument escape."""
    worktree = _worktree(tmp_path)
    policy = _policy(worktree)
    other = tmp_path / "other-repo"
    other.mkdir()
    for argv in (
        ["git", "--git-dir=/etc/git-repo", "log"],
        ["git", "--git-dir", str(other / ".git"), "log"],
        ["git", "-C", str(other), "status"],
        ["git", "--work-tree", str(other), "status"],
        ["git", "add", "/etc/passwd"],
        ["git", "checkout", "--", str(other / "file.txt")],
        ["git", "commit", "-F", "/etc/passwd"],
    ):
        reason = _reason_of(assert_git_command_allowed, argv, policy)
        assert reason is WorkerIsolationReason.FORBIDDEN_GIT_INVOCATION, argv


def test_non_git_argv_is_not_this_guard_s_business(tmp_path: Path) -> None:
    """Covers: guard scope -- only git control-surface commands."""
    policy = _policy(_worktree(tmp_path))
    analysis = git_invocation_analysis(["python", "-m", "pytest"], policy)
    assert analysis.allowed and analysis.is_git is False


# ---------------------------------------------------------------------------
# 5. Session authority.
# ---------------------------------------------------------------------------


def test_session_authority_defaults_to_owner_and_fails_closed_on_unknown() -> None:
    """Covers: M4 'fail closed when called from an automated worker session'."""
    assert resolve_session_authority({}) is SessionAuthority.OWNER
    assert resolve_session_authority({SESSION_AUTHORITY_ENV: ""}) is SessionAuthority.OWNER
    assert resolve_session_authority({SESSION_AUTHORITY_ENV: "owner"}) is SessionAuthority.OWNER
    assert resolve_session_authority({SESSION_AUTHORITY_ENV: "worker"}) is SessionAuthority.WORKER
    assert is_worker_session({SESSION_AUTHORITY_ENV: "WORKER"})
    # An unknown marker is never interpreted as owner.
    with pytest.raises(WorkerIsolationViolation) as info:
        resolve_session_authority({SESSION_AUTHORITY_ENV: "root"})
    assert info.value.reason is WorkerIsolationReason.UNKNOWN_SESSION_AUTHORITY


def test_worker_session_env_marks_and_hardens() -> None:
    """Covers: M4 'worker sessions run isolated from host configuration'."""
    scratch = os.path.realpath(os.path.sep + "tmp")
    env = worker_session_env({}, scratch_root=scratch)
    assert env[SESSION_AUTHORITY_ENV] == WORKER_SESSION_VALUE
    assert env["HOME"] == scratch
    assert env["XDG_CONFIG_HOME"] == os.path.join(scratch, ".config")
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["GIT_CONFIG_SYSTEM"] == os.devnull
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_worker_session_env_refuses_owner_escalation() -> None:
    """Covers: threat-model worker claiming owner session authority."""
    reason = _reason_of(
        worker_session_env, {SESSION_AUTHORITY_ENV: "owner"}
    )
    assert reason is WorkerIsolationReason.WORKER_AUTHORITY_ESCALATION
    reason = _reason_of(worker_session_env, {SESSION_AUTHORITY_ENV: "superuser"})
    assert reason is WorkerIsolationReason.UNKNOWN_SESSION_AUTHORITY


# ---------------------------------------------------------------------------
# 6. The worker execution seam preflight.
# ---------------------------------------------------------------------------


def test_preflight_accepts_a_normal_worker_git_command(tmp_path: Path) -> None:
    """Covers: prompt section 'wire enforcement into the worker execution seam'."""
    worktree = _worktree(tmp_path)
    policy = _policy(worktree)
    evidence = preflight_worker_command(
        policy,
        argv=["git", "commit", "-m", "checkpoint"],
        cwd=str(worktree / "src"),
        env={SESSION_AUTHORITY_ENV: "worker"},
    )
    assert evidence.session_authority is SessionAuthority.WORKER
    assert evidence.git_subcommand == "commit"
    assert evidence.argv0_basename == "git"
    assert evidence.worktree_root == str(worktree)
    assert evidence.protected_git_paths == (
        str(worktree / ".git" / "config"),
        str(worktree / ".git" / "config.worktree"),
        str(worktree / ".git" / "hooks"),
    )
    serialised = evidence.as_dict()
    assert serialised["session_authority"] == "worker"
    assert serialised["git_subcommand"] == "commit"


def test_preflight_refuses_owner_authority_in_a_worker_run(tmp_path: Path) -> None:
    """Covers: M4 'worker cannot claim owner authority'."""
    policy = _policy(_worktree(tmp_path))
    reason = _reason_of(
        preflight_worker_command,
        policy,
        argv=["git", "status"],
        env={SESSION_AUTHORITY_ENV: "owner"},
    )
    assert reason is WorkerIsolationReason.WORKER_AUTHORITY_ESCALATION
    reason = _reason_of(
        preflight_worker_command,
        policy,
        argv=["git", "status"],
        env={SESSION_AUTHORITY_ENV: "omnipotent"},
    )
    assert reason is WorkerIsolationReason.UNKNOWN_SESSION_AUTHORITY


def test_preflight_refuses_cwd_outside_worktree(tmp_path: Path) -> None:
    """Covers: M4 invariant 'worker cannot run outside its worktree'."""
    worktree = _worktree(tmp_path)
    policy = _policy(worktree)
    reason = _reason_of(
        preflight_worker_command,
        policy,
        argv=["git", "status"],
        cwd=str(tmp_path / "elsewhere"),
    )
    assert reason is WorkerIsolationReason.CWD_OUTSIDE_WORKTREE


def test_preflight_refuses_protected_requested_paths(tmp_path: Path) -> None:
    """Covers: M4 requested-write guard for the git control surface."""
    worktree = _worktree(tmp_path)
    policy = _policy(worktree)
    reason = _reason_of(
        preflight_worker_command,
        policy,
        argv=["python", "-c", "pass"],
        requested_paths=(".git/hooks/pre-commit",),
    )
    assert reason is WorkerIsolationReason.REPO_GIT_INTEGRITY_MUTATION
    reason = _reason_of(
        preflight_worker_command,
        policy,
        argv=["python", "-c", "pass"],
        requested_paths=("/etc/gitconfig",),
    )
    assert reason is WorkerIsolationReason.HOST_CONFIG_MUTATION


def test_preflight_requires_argv(tmp_path: Path) -> None:
    """Covers: fail-closed on an empty worker command."""
    policy = _policy(_worktree(tmp_path))
    reason = _reason_of(preflight_worker_command, policy, argv=[])
    assert reason is WorkerIsolationReason.FORBIDDEN_GIT_INVOCATION


# ---------------------------------------------------------------------------
# 7. Loopback transport (semantic counterpart of the Web Host guard).
# ---------------------------------------------------------------------------


def test_loopback_host_allowlist_matches_the_web_guard() -> None:
    """Covers: 'retain and extend the loopback Host allowlist guard'."""
    assert loopback_host_allowed("127.0.0.1")
    assert loopback_host_allowed("127.0.0.1:8765", server_port=8765)
    assert loopback_host_allowed("localhost:8765", server_port=8765)
    assert loopback_host_allowed("[::1]:8765", server_port=8765)
    assert loopback_host_allowed("LOCALHOST")
    for hostile in (
        "evil.test",
        "evil.test:8765",
        "192.168.1.5:8765",
        "127.0.0.1:99999",
        "127.0.0.1:notaport",
        "user@127.0.0.1",
        "127.0.0.1:8765, evil.test",
        "::1",
        "[::1",
        "",
        "bad host",
    ):
        assert not loopback_host_allowed(hostile), hostile
    # A port mismatch and the synthetic name outside a synthetic scope refuse.
    assert not loopback_host_allowed("127.0.0.1:1", server_port=8765)
    assert not loopback_host_allowed("testserver")
    assert loopback_host_allowed("testserver", synthetic_scope=True)


# ---------------------------------------------------------------------------
# 8. Kernel-level masks (the bwrap containment seam).
# ---------------------------------------------------------------------------


def _worker_kernel_policy(worktree: Path) -> SandboxPolicy:
    return SandboxPolicy(
        repo_root=str(worktree),
        readable_paths=("**",),
        writable_paths=("**",),
        deny_paths=GIT_INTEGRITY_DENY,
    )


def test_git_integrity_mask_targets_cover_config_and_hooks(tmp_path: Path) -> None:
    """Covers: M4 kernel-level 'cannot mutate .git/config or hooks'."""
    worktree = _worktree(tmp_path)
    targets = git_integrity_mask_targets(str(worktree))
    assert targets == (
        str(worktree / ".git" / "config"),
        str(worktree / ".git" / "config.worktree"),
        str(worktree / ".git" / "hooks"),
    )


def test_mount_args_mask_a_denied_file_with_an_empty_file(tmp_path: Path) -> None:
    """Covers: file-shaped deny targets get a file mask, not a directory."""
    worktree = _worktree(tmp_path)
    args = bwrap_mount_args_for_policy(_worker_kernel_policy(worktree))
    config = str(worktree / ".git" / "config")
    index = args.index(config)
    assert args[index - 2] == "--ro-bind"
    assert args[index - 1] == empty_mask_file()
    assert os.path.isfile(empty_mask_file())
    hooks = str(worktree / ".git" / "hooks")
    hooks_index = args.index(hooks)
    assert args[hooks_index - 1] == empty_mask_dir()


def test_linked_worktree_git_file_is_not_masked(tmp_path: Path) -> None:
    """Covers: a linked worktree (.git file) must not break the launch."""
    root = tmp_path / "linked"
    root.mkdir()
    (root / ".git").write_text("gitdir: /somewhere/else/.git/worktrees/linked\n")
    assert git_integrity_mask_targets(str(root)) == ()
    args = bwrap_mount_args_for_policy(
        SandboxPolicy(repo_root=str(root), readable_paths=("**",), writable_paths=("**",))
    )
    assert str(root / ".git" / "config") not in args


def test_outer_git_deny_is_not_remasked_inside(tmp_path: Path) -> None:
    """Covers: deny minimality -- a whole-.git deny is not re-masked inside it."""
    worktree = _worktree(tmp_path)
    policy = SandboxPolicy(
        repo_root=str(worktree),
        readable_paths=("**",),
        writable_paths=("**",),
        deny_paths=(".git/**", ".github/**"),
    )
    args = bwrap_mount_args_for_policy(policy)
    masked = [args[index + 2] for index, token in enumerate(args) if token == "--ro-bind"]
    assert str(worktree / ".git") in masked
    assert str(worktree / ".git" / "config") not in masked
    assert str(worktree / ".git" / "hooks") not in masked


def test_unmountable_deny_target_is_skipped(tmp_path: Path) -> None:
    """Covers: a deny target under a *file* ancestor is impossible, not fatal."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "notadir").write_text("i am a file\n")
    policy = SandboxPolicy(
        repo_root=str(root),
        readable_paths=("**",),
        writable_paths=("**",),
        deny_paths=("notadir/deeper/**",),
    )
    args = bwrap_mount_args_for_policy(policy)
    assert str(root / "notadir" / "deeper") not in args
    assert (root / "notadir").is_file()


@pytest.mark.skipif(not bwrap_available(), reason="bwrap not available on this host")
def test_real_containment_blocks_git_config_write(tmp_path: Path) -> None:
    """Covers: M4 'cannot tamper with .git/config' enforced by the kernel."""
    worktree = _worktree(tmp_path)
    config = worktree / ".git" / "config"
    original = config.read_text()
    args = bwrap_mount_args_for_policy(_worker_kernel_policy(worktree))
    argv = [
        "bwrap",
        *args,
        "--",
        "/bin/sh",
        "-c",
        f"echo pwned >> {config}; exit 0",
    ]
    result = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    assert config.read_text() == original
    assert "pwned" not in config.read_text()
    assert "Read-only file system" in result.stderr
