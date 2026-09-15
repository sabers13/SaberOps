"""Per-Attempt SandboxPolicy contracts.

The sandbox -- not the worker prompt -- is the enforcement boundary.
``SandboxPolicy`` is the canonical, deterministic evidence of what was
authorised for one Attempt; runtime enforcers consult :meth:`authorize_*`
methods and call :meth:`digest` to persist the policy as Attempt evidence.

This module is intentionally narrow:

* it carries no provider, model, or orchestration logic;
* it never contacts the file system at import time;
* it never persists real secret values -- only ``credential_ref`` references
  with the canonical ``env:`` / ``profile:`` / ``store:`` prefixes;
* it produces stable, deterministic digests so an attempt's policy evidence
  is reproducible and comparable.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from orchestrator_mvp.sandbox.grants import (
    CapabilityEntry,
    CapabilityOperation,
    PathPattern,
    ResourceScope,
    SandboxContractError,
    SideEffectSemantics,
    ToolGrant,
    _glob_to_regex,
    validate_credential_ref,
)

# ---------------------------------------------------------------------------
# Network policy.
# ---------------------------------------------------------------------------


_NETWORK_MODES = ("DENY", "ALLOWLIST")


def _glob_host(host: str, pattern: str) -> bool:
    """Match ``host`` against a bounded host glob pattern.

    Supports only ``*`` (zero or more chars) and literal text.  Anything
    more elaborate must be expressed as multiple narrower patterns.
    """
    if not pattern:
        return False
    regex_parts: list[str] = []
    for ch in pattern:
        if ch == "*":
            regex_parts.append("[^.]*")
        elif ch == ".":
            regex_parts.append(r"\.")
        else:
            regex_parts.append(re.escape(ch))
    return re.fullmatch("".join(regex_parts), host) is not None


@dataclass(frozen=True)
class NetworkPolicy:
    """Network policy applied to one :class:`SandboxPolicy`.

    The default ``mode`` is ``DENY``: an autonomous Attempt has no ambient
    network and must be explicitly granted an ``ALLOWLIST`` to egress.
    Provider transport required by the harness/backend is a separate
    concern from this policy and is not broadened by an allowlist here.
    """

    mode: str = "DENY"
    allow_destinations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.mode not in _NETWORK_MODES:
            raise SandboxContractError(
                f"network policy mode must be one of {_NETWORK_MODES}, got {self.mode!r}"
            )
        for destination in self.allow_destinations:
            if not destination or not isinstance(destination, str):
                raise SandboxContractError(
                    "network allow destinations must be non-empty strings"
                )

    def authorize(self, destination: str) -> bool:
        if self.mode == "DENY":
            return False
        return any(_glob_host(destination, pattern) for pattern in self.allow_destinations)


# ---------------------------------------------------------------------------
# Process policy.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProcessPolicy:
    """Process policy applied to one :class:`SandboxPolicy`.

    ``allow_host_shell=False`` (the default) forbids unrestricted HOST shell
    execution.  ``sandboxed_shell=True`` allows ordinary ``run_process``
    invocation inside the attempt's sandbox; ``approved_executables`` are the
    exact paths/basenames whose invocation is authorised even when the
    sandbox shell is on.
    """

    allow_host_shell: bool = False
    sandboxed_shell: bool = True
    approved_executables: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for executable in self.approved_executables:
            if not executable or not isinstance(executable, str):
                raise SandboxContractError("approved_executables must be non-empty strings")
            if any(part == ".." for part in PurePosixPath(executable).parts):
                raise SandboxContractError(
                    f"approved_executable may not contain traversal: {executable!r}"
                )

    def authorize_host_shell(self) -> bool:
        return self.allow_host_shell

    def authorize_sandboxed_shell(self) -> bool:
        return self.sandboxed_shell

    def authorize_executable(self, executable: str) -> bool:
        if not executable:
            return False
        if executable in self.approved_executables:
            return True
        basename = Path(executable).name
        return any(
            candidate == executable or candidate == basename
            for candidate in self.approved_executables
        )


# ---------------------------------------------------------------------------
# Resource limits.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResourceLimits:
    """Optional deterministic resource limits for one Attempt.

    Every field is optional.  When ``None`` the runtime may either apply its
    own platform default or leave the limit unenforced -- but the runtime
    must never claim enforcement of a limit that was not actually set.  The
    sandbox policy is the *evidence* of what was authorised, not a promise
    about what every host kernel can enforce.
    """

    max_cpu_seconds: int | None = None
    max_wall_seconds: int | None = None
    max_memory_bytes: int | None = None
    max_open_files: int | None = None
    max_disk_bytes: int | None = None


# ---------------------------------------------------------------------------
# SandboxPolicy.
# ---------------------------------------------------------------------------


def _glob_match(path: str, pattern: str) -> bool:
    """Match ``path`` against a bounded glob pattern using the canonical translator."""
    text = pattern.lstrip("/")
    normalised = path.lstrip("/")
    return re.fullmatch(_glob_to_regex(text), normalised) is not None


@dataclass(frozen=True)
class SandboxPolicy:
    """The per-Attempt enforcement contract.

    The sandbox -- not the worker prompt -- is the enforcement boundary.
    Every evaluation method answers deterministically from this object alone;
    no filesystem or environment state participates.  The class is designed
    so callers may use it as the *evidence* of what was authorised (via
    :meth:`digest`) without exposing secret values or runtime handles.
    """

    repo_root: str
    readable_paths: tuple[str, ...] = ()
    writable_paths: tuple[str, ...] = ()
    deny_paths: tuple[str, ...] = ()
    network: NetworkPolicy = field(default_factory=NetworkPolicy)
    credential_refs: tuple[str, ...] = ()
    process: ProcessPolicy = field(default_factory=ProcessPolicy)
    resource_limits: ResourceLimits = field(default_factory=ResourceLimits)
    schema_version: int = 1
    digest: str = ""

    def __post_init__(self) -> None:
        if not self.repo_root or not isinstance(self.repo_root, str):
            raise SandboxContractError("SandboxPolicy.repo_root must be a non-empty string")
        if not Path(self.repo_root).is_absolute():
            raise SandboxContractError("SandboxPolicy.repo_root must be an absolute path")
        for collection in (self.readable_paths, self.writable_paths, self.deny_paths):
            for pattern_text in collection:
                if not pattern_text:
                    raise SandboxContractError("SandboxPolicy path patterns must be non-empty")
                if ".." in PurePosixPath(pattern_text).parts:
                    raise SandboxContractError(
                        f"SandboxPolicy path contains traversal: {pattern_text!r}"
                    )
        for ref in self.credential_refs:
            validate_credential_ref(ref)
        normalised_readable = tuple(sorted(self.readable_paths))
        normalised_writable = tuple(sorted(self.writable_paths))
        normalised_deny = tuple(sorted(self.deny_paths))
        normalised_refs = tuple(sorted(self.credential_refs))
        object.__setattr__(self, "readable_paths", normalised_readable)
        object.__setattr__(self, "writable_paths", normalised_writable)
        object.__setattr__(self, "deny_paths", normalised_deny)
        object.__setattr__(self, "credential_refs", normalised_refs)
        calculated = self._calculate_digest()
        if not self.digest:
            object.__setattr__(self, "digest", calculated)
        elif self.digest != calculated:
            raise SandboxContractError("SandboxPolicy digest mismatch")

    def _calculate_digest(self) -> str:
        payload = {
            "schema_version": self.schema_version,
            "repo_root": self.repo_root,
            "readable_paths": list(self.readable_paths),
            "writable_paths": list(self.writable_paths),
            "deny_paths": list(self.deny_paths),
            "network": {
                "mode": self.network.mode,
                "allow_destinations": sorted(self.network.allow_destinations),
            },
            "credential_refs": list(self.credential_refs),
            "process": {
                "allow_host_shell": self.process.allow_host_shell,
                "sandboxed_shell": self.process.sandboxed_shell,
                "approved_executables": sorted(self.process.approved_executables),
            },
            "resource_limits": {
                "max_cpu_seconds": self.resource_limits.max_cpu_seconds,
                "max_wall_seconds": self.resource_limits.max_wall_seconds,
                "max_memory_bytes": self.resource_limits.max_memory_bytes,
                "max_open_files": self.resource_limits.max_open_files,
                "max_disk_bytes": self.resource_limits.max_disk_bytes,
            },
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def authorize_read(self, repo_relative_path: str) -> bool:
        if self._is_denied(repo_relative_path):
            return False
        return any(_glob_match(repo_relative_path, pattern) for pattern in self.readable_paths)

    def authorize_write(self, repo_relative_path: str) -> bool:
        if self._is_denied(repo_relative_path):
            return False
        return any(_glob_match(repo_relative_path, pattern) for pattern in self.writable_paths)

    def authorize_network(self, destination: str) -> bool:
        return self.network.authorize(destination)

    def authorize_executable(self, executable: str) -> bool:
        return self.process.authorize_executable(executable)

    def authorize_credential_ref(self, ref: str) -> bool:
        return ref in self.credential_refs

    def authorize_host_shell(self) -> bool:
        return self.process.authorize_host_shell()

    def authorize_sandboxed_shell(self) -> bool:
        return self.process.authorize_sandboxed_shell()

    def _is_denied(self, repo_relative_path: str) -> bool:
        return any(_glob_match(repo_relative_path, pattern) for pattern in self.deny_paths)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "repo_root": self.repo_root,
            "readable_paths": list(self.readable_paths),
            "writable_paths": list(self.writable_paths),
            "deny_paths": list(self.deny_paths),
            "network": {
                "mode": self.network.mode,
                "allow_destinations": list(self.network.allow_destinations),
            },
            "credential_refs": list(self.credential_refs),
            "process": {
                "allow_host_shell": self.process.allow_host_shell,
                "sandboxed_shell": self.process.sandboxed_shell,
                "approved_executables": list(self.process.approved_executables),
            },
            "resource_limits": {
                "max_cpu_seconds": self.resource_limits.max_cpu_seconds,
                "max_wall_seconds": self.resource_limits.max_wall_seconds,
                "max_memory_bytes": self.resource_limits.max_memory_bytes,
                "max_open_files": self.resource_limits.max_open_files,
                "max_disk_bytes": self.resource_limits.max_disk_bytes,
            },
            "digest": self.digest,
        }


# ---------------------------------------------------------------------------
# Default deny lists and helper builders.
# ---------------------------------------------------------------------------


GIT_INTERNAL_DENY: tuple[str, ...] = (".git/**",)
GITHUB_DENY: tuple[str, ...] = (".github/**",)


def default_sandbox_policy(
    repo_root: str,
    *,
    writable_paths: tuple[str, ...] = (),
    network_mode: str = "DENY",
    network_allow_destinations: tuple[str, ...] = (),
    credential_refs: tuple[str, ...] = (),
    approved_executables: tuple[str, ...] = (),
    allow_host_shell: bool = False,
) -> SandboxPolicy:
    """Construct a default-deny sandbox policy for an autonomous attempt.

    The default always denies writes to ``.git/**`` and to ``.github/**``;
    an explicit additional ``writable_paths`` grant is required for a worker
    to mutate either.  The default also denies network egress and host shell.
    """
    deny_paths: tuple[str, ...] = GIT_INTERNAL_DENY + GITHUB_DENY
    return SandboxPolicy(
        repo_root=repo_root,
        readable_paths=writable_paths,
        writable_paths=writable_paths,
        deny_paths=deny_paths,
        network=NetworkPolicy(
            mode=network_mode,
            allow_destinations=tuple(sorted(network_allow_destinations)),
        ),
        credential_refs=tuple(sorted(credential_refs)),
        process=ProcessPolicy(
            allow_host_shell=allow_host_shell,
            sandboxed_shell=True,
            approved_executables=tuple(sorted(approved_executables)),
        ),
    )


def grant_for_sandbox(
    policy: SandboxPolicy,
    *,
    extra_readable: tuple[str, ...] = (),
    extra_writable: tuple[str, ...] = (),
    tool_refs: tuple[str, ...] = (),
) -> ToolGrant:
    """Build a :class:`ToolGrant` aligned to a :class:`SandboxPolicy`.

    The grant is computed from the policy's own resource surface plus any
    optional extras.  Operations the policy forbids are simply absent from
    the grant; absence is itself a denial.
    """
    readable = list(policy.readable_paths) + list(extra_readable)
    writable = list(policy.writable_paths) + list(extra_writable)
    entries: list[CapabilityEntry] = []
    if readable:
        readable_set = sorted({PathPattern(p) for p in readable}, key=lambda p: p.raw)
        entries.append(
            CapabilityEntry(
                operation=CapabilityOperation.FILE_READ,
                resource_scope=ResourceScope(
                    readable_paths=tuple(readable_set),
                ),
                side_effect=SideEffectSemantics.READ_ONLY,
                tool_ref=tool_refs[0] if tool_refs else None,
            )
        )
    if writable:
        writable_set = sorted({PathPattern(p) for p in writable}, key=lambda p: p.raw)
        entries.append(
            CapabilityEntry(
                operation=CapabilityOperation.FILE_WRITE,
                resource_scope=ResourceScope(
                    writable_paths=tuple(writable_set),
                ),
                side_effect=SideEffectSemantics.LOCAL_REPLAY_SAFE,
                tool_ref=tool_refs[0] if tool_refs else None,
            )
        )
    if policy.network.mode != "DENY":
        entries.append(
            CapabilityEntry(
                operation=CapabilityOperation.NETWORK,
                resource_scope=ResourceScope(
                    network_destinations=tuple(sorted(policy.network.allow_destinations)),
                ),
                side_effect=SideEffectSemantics.EXTERNAL_IDEMPOTENT,
                tool_ref=tool_refs[0] if tool_refs else None,
            )
        )
    if policy.credential_refs:
        entries.append(
            CapabilityEntry(
                operation=CapabilityOperation.CREDENTIAL_ACCESS,
                resource_scope=ResourceScope(
                    credential_refs=tuple(sorted(policy.credential_refs)),
                ),
                side_effect=SideEffectSemantics.EXTERNAL_UNCERTAIN,
                tool_ref=None,
            )
        )
    if not entries:
        raise SandboxContractError(
            "SandboxPolicy has no read/write/network/credential surface to grant"
        )
    return ToolGrant(entries=tuple(entries))


__all__ = [
    "GIT_INTERNAL_DENY",
    "GITHUB_DENY",
    "NetworkPolicy",
    "ProcessPolicy",
    "ResourceLimits",
    "SandboxContractError",
    "SandboxPolicy",
    "default_sandbox_policy",
    "grant_for_sandbox",
]
