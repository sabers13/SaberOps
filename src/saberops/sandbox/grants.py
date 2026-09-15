"""ToolGrant contracts: resource-scoped capability + operation vocabulary.

C11-C widens the canonical C08 *capability* contract into a *resource-scoped
authorization contract*.  A bare capability name (e.g. ``FILE_WRITE``) is
never a sufficient security boundary; the runtime must be able to evaluate
whether an attempted operation is authorized against a concrete target.

This module is intentionally narrow:

* it carries no provider, model, or orchestration logic;
* it never contacts the file system at import time;
* it never persists real secret values -- only ``credential_ref`` references
  with the canonical ``profile:`` / ``store:`` / ``env:`` prefixes;
* it produces stable, deterministic digests so an attempt's policy evidence
  is reproducible and comparable.

The module owns:

* :class:`SideEffectSemantics` -- the four-class taxonomy used to gate retry /
  failover decisions (READ_ONLY / LOCAL_REPLAY_SAFE / EXTERNAL_IDEMPOTENT /
  EXTERNAL_UNCERTAIN).  Distinct from C08's coarser :class:`SideEffectClass`;
  the runtime layer maps one to the other.
* :class:`CapabilityOperation` -- the canonical *operation* vocabulary
  (``FILE_READ``, ``FILE_WRITE``, ``PROCESS_RUN``, ``NETWORK``,
  ``CREDENTIAL_ACCESS``).  These are operations the runtime enforces; the
  higher-level :class:`ToolSpec` logical refs from C08 are bound to one of
  these via :class:`CapabilityEntry.tool_ref`.
* :class:`PathPattern` -- bounded canonical glob/literal pattern with
  traversal / symlink safety checks at construction time.
* :class:`ResourceScope` -- the resource scope attached to a capability
  entry: readable paths, writable paths, executable paths, network
  destinations, credential references.
* :class:`CapabilityEntry` -- one (operation, resource_scope, side-effect)
  triple inside a :class:`ToolGrant`.
* :class:`ToolGrant` -- the typed, deterministic grant that an attempt's
  ToolGrant row freezes at run creation.
* :func:`c08_to_runtime_side_effect` -- mapping from C08 SideEffectClass to
  the runtime semantics vocabulary.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any


class SandboxContractError(ValueError):
    """Typed failure raised by sandbox / grant / exposure helpers."""


_ENV_CREDENTIAL_ID_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_REFERENCE_ID_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*(?:/[A-Za-z0-9_][A-Za-z0-9_.-]*)*")
_CREDENTIAL_SCHEMES = frozenset({"env", "profile", "store"})


def validate_credential_ref(ref: str) -> str:
    """Validate one non-secret credential reference and return it unchanged."""
    if not isinstance(ref, str) or not ref:
        raise SandboxContractError("credential reference must be a non-empty string")
    scheme, separator, identifier = ref.partition(":")
    if separator != ":" or scheme not in _CREDENTIAL_SCHEMES:
        raise SandboxContractError(
            f"credential_ref must use env:/profile:/store: scheme: {ref!r}"
        )
    if any(char.isspace() or ord(char) < 32 for char in ref):
        raise SandboxContractError(
            f"credential_ref contains whitespace or control characters: {ref!r}"
        )
    if scheme == "env":
        valid = _ENV_CREDENTIAL_ID_RE.fullmatch(identifier) is not None
    else:
        valid = _REFERENCE_ID_RE.fullmatch(identifier) is not None and all(
            segment not in (".", "..") for segment in identifier.split("/")
        )
    if not valid:
        raise SandboxContractError(f"credential_ref has a malformed identifier: {ref!r}")
    return ref


# ---------------------------------------------------------------------------
# Side-effect semantics: the four-class reconciliation taxonomy.
#
# Distinct from C08's coarse SideEffectClass: this is the runtime policy
# vocabulary that gates retry / failover decisions, while C08's values name
# the operational class of a logical tool.  A tool may carry both; a runtime
# mapping (``c08_to_runtime_side_effect``) coerces one to the other.
# ---------------------------------------------------------------------------


class SideEffectSemantics(StrEnum):
    """Four-class taxonomy for effect reconciliation.

    * ``READ_ONLY``              -- no external mutation; safe to replay.
    * ``LOCAL_REPLAY_SAFE``      -- ordinary retry is acceptable; no
      external system, no durable receipt required.
    * ``EXTERNAL_IDEMPOTENT``    -- external mutation but with a
      provider-side idempotency key / receipt; retries only after the
      prior receipt is verified or absent.
    * ``EXTERNAL_UNCERTAIN``     -- external mutation whose effect cannot
      be cheaply detected; reconcile before any retry.
    """

    READ_ONLY = "READ_ONLY"
    LOCAL_REPLAY_SAFE = "LOCAL_REPLAY_SAFE"
    EXTERNAL_IDEMPOTENT = "EXTERNAL_IDEMPOTENT"
    EXTERNAL_UNCERTAIN = "EXTERNAL_UNCERTAIN"


# ---------------------------------------------------------------------------
# Canonical operation vocabulary.
#
# These are the bounded operation classes the runtime evaluates against a
# resource scope.  The logical tool refs from C08 (e.g. ``github.merge``)
# bind to one of these via :attr:`CapabilityEntry.tool_ref`; the worker
# harness maps tool-invocation intent to the operation it represents, and
# the sandbox answers ``authorize``.
# ---------------------------------------------------------------------------


class CapabilityOperation(StrEnum):
    """Canonical bounded operation vocabulary the runtime enforces."""

    FILE_READ = "FILE_READ"
    FILE_WRITE = "FILE_WRITE"
    PROCESS_RUN = "PROCESS_RUN"
    NETWORK = "NETWORK"
    CREDENTIAL_ACCESS = "CREDENTIAL_ACCESS"


# ---------------------------------------------------------------------------
# Path patterns: bounded, canonical, traversal-safe.
# ---------------------------------------------------------------------------


_GLOB_SEGMENT_RE = re.compile(r"\*\*|\*|\?")
_DISALLOWED_PATH_CHARS = set("\x00")


def _validate_pattern_text(value: str) -> str:
    if not value:
        raise SandboxContractError("path pattern must not be empty")
    if any(char in _DISALLOWED_PATH_CHARS for char in value):
        raise SandboxContractError(f"path pattern contains NUL: {value!r}")
    parts = PurePosixPath(value).parts
    if ".." in parts:
        raise SandboxContractError(f"path pattern contains traversal: {value!r}")
    return value


def _glob_to_regex(pattern: str) -> str:
    """Translate the bounded glob vocabulary to a Python regular expression.

    The translator walks the pattern char-by-char and preserves glob meta
    semantics literally.  ``**`` matches across separators; ``*`` matches
    non-separator characters; ``?`` matches a single non-separator char.
    Anything outside :data:`_VALID_GLOB_CHARS` has already been rejected by
    :func:`_validate_pattern_text`.
    """
    out: list[str] = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "*":
            if i + 1 < len(pattern) and pattern[i + 1] == "*":
                out.append(".*")
                i += 2
            else:
                out.append("[^/]*")
                i += 1
            continue
        if ch == "?":
            out.append("[^/]")
            i += 1
            continue
        if ch == ".":
            out.append(r"\.")
            i += 1
            continue
        if ch in {"+", "(", ")", "{", "}", "|", "^", "$"}:
            out.append(re.escape(ch))
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


@dataclass(frozen=True)
class PathPattern:
    """Canonical bounded glob/literal pattern.

    Patterns are validated at construction: any ``..`` segment, NUL byte, or
    unsupported character raises :class:`SandboxContractError`.  The pattern
    text is preserved verbatim for digests; matching is case-sensitive and
    anchored to the repository root supplied to :meth:`matches`.
    """

    raw: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "raw", _validate_pattern_text(self.raw))

    @property
    def kind(self) -> str:
        """Return ``"glob"`` when the pattern uses glob meta-characters."""
        if _GLOB_SEGMENT_RE.search(self.raw):
            return "glob"
        return "literal"

    def matches(self, candidate: str) -> bool:
        """Return True iff ``candidate`` is authorized by this pattern."""
        text = self.raw.lstrip("/")
        normalised_candidate = candidate.lstrip("/")
        if self.kind == "literal":
            return text == normalised_candidate
        return re.fullmatch(_glob_to_regex(text), normalised_candidate) is not None


# ---------------------------------------------------------------------------
# Resource scope.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResourceScope:
    """The resource scope attached to one :class:`CapabilityEntry`.

    Paths are *repository-relative* globs/literals; the runtime resolves
    them against the attempt's repository root when evaluating
    :meth:`authorize`.  Network destinations are plain host globs
    (``*.example.com``) and credential references use the existing
    ``env:`` / ``profile:`` / ``store:`` vocabulary from C08.
    """

    readable_paths: tuple[PathPattern, ...] = ()
    writable_paths: tuple[PathPattern, ...] = ()
    executable_paths: tuple[str, ...] = ()
    network_destinations: tuple[str, ...] = ()
    credential_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for ref in self.credential_refs:
            validate_credential_ref(ref)
        for executable in self.executable_paths:
            if not executable or not isinstance(executable, str):
                raise SandboxContractError("executable_path entries must be non-empty strings")
            if any(part == ".." for part in PurePosixPath(executable).parts):
                raise SandboxContractError(
                    f"executable_path may not contain traversal: {executable!r}"
                )
        for destination in self.network_destinations:
            if not destination or not isinstance(destination, str):
                raise SandboxContractError("network destination entries must be non-empty strings")
            if any(char in destination for char in ("\x00", " ", "\n", "\r")):
                raise SandboxContractError(
                    f"network destination contains whitespace or NUL: {destination!r}"
                )

    def matches_readable(self, repo_relative_path: str) -> bool:
        return any(pattern.matches(repo_relative_path) for pattern in self.readable_paths)

    def matches_writable(self, repo_relative_path: str) -> bool:
        return any(pattern.matches(repo_relative_path) for pattern in self.writable_paths)


# ---------------------------------------------------------------------------
# Capability entry + ToolGrant.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CapabilityEntry:
    """One bounded capability inside a :class:`ToolGrant`.

    ``operation`` is the canonical operation class; ``resource_scope`` is the
    resource boundary attached to it; ``side_effect`` is the runtime
    reconciliation class; ``constraints`` carries a tuple of arbitrary
    string constraints (e.g. ``"max_bytes:1048576"``, ``"timeout:30s"``,
    ``"approved_executable:/usr/bin/python3"``).
    """

    operation: CapabilityOperation
    resource_scope: ResourceScope = field(default_factory=ResourceScope)
    constraints: tuple[str, ...] = ()
    side_effect: SideEffectSemantics = SideEffectSemantics.LOCAL_REPLAY_SAFE
    tool_ref: str | None = None

    def __post_init__(self) -> None:
        for constraint in self.constraints:
            if not isinstance(constraint, str) or not constraint.strip():
                raise SandboxContractError(
                    "capability constraints must be non-empty strings"
                )

    def authorize_read(self, repo_relative_path: str) -> bool:
        if self.operation is not CapabilityOperation.FILE_READ:
            return False
        return self.resource_scope.matches_readable(repo_relative_path)

    def authorize_write(self, repo_relative_path: str) -> bool:
        if self.operation is not CapabilityOperation.FILE_WRITE:
            return False
        return self.resource_scope.matches_writable(repo_relative_path)

    def authorize_executable(self, executable: str) -> bool:
        if self.operation is not CapabilityOperation.PROCESS_RUN:
            return False
        return executable in self.resource_scope.executable_paths

    def as_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation.value,
            "tool_ref": self.tool_ref,
            "side_effect": self.side_effect.value,
            "constraints": list(self.constraints),
            "resource_scope": {
                "readable_paths": sorted(p.raw for p in self.resource_scope.readable_paths),
                "writable_paths": sorted(p.raw for p in self.resource_scope.writable_paths),
                "executable_paths": sorted(self.resource_scope.executable_paths),
                "network_destinations": sorted(self.resource_scope.network_destinations),
                "credential_refs": sorted(self.resource_scope.credential_refs),
            },
        }


@dataclass(frozen=True)
class ToolGrant:
    """The canonical resource-scoped tool grant for one Attempt.

    A grant is a frozen tuple of :class:`CapabilityEntry` values.  The grant
    digest is deterministic: same effective policy yields the same digest,
    regardless of how the entries were assembled or which Python set ordering
    the caller happened to use.
    """

    entries: tuple[CapabilityEntry, ...]
    digest: str = ""

    def __post_init__(self) -> None:
        if not self.entries:
            raise SandboxContractError("ToolGrant must declare at least one capability entry")
        operations = [entry.operation for entry in self.entries]
        if len(set(operations)) != len(operations):
            raise SandboxContractError(
                "ToolGrant must not declare duplicate operations"
            )
        normalised_entries = tuple(
            sorted(
                self.entries,
                key=lambda entry: (
                    entry.operation.value,
                    entry.tool_ref or "",
                    entry.side_effect.value,
                ),
            )
        )
        object.__setattr__(self, "entries", normalised_entries)
        calculated = self._calculate_digest()
        if not self.digest:
            object.__setattr__(self, "digest", calculated)
        elif self.digest != calculated:
            raise SandboxContractError("ToolGrant digest mismatch")

    def _calculate_digest(self) -> str:
        payload = [entry.as_dict() for entry in self.entries]
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def find(self, operation: CapabilityOperation) -> CapabilityEntry | None:
        for entry in self.entries:
            if entry.operation is operation:
                return entry
        return None

    def authorize(
        self,
        operation: CapabilityOperation,
        repo_relative_path: str | None = None,
    ) -> bool:
        entry = self.find(operation)
        if entry is None:
            return False
        if operation is CapabilityOperation.FILE_READ:
            return repo_relative_path is not None and entry.authorize_read(repo_relative_path)
        if operation is CapabilityOperation.FILE_WRITE:
            return repo_relative_path is not None and entry.authorize_write(repo_relative_path)
        if operation is CapabilityOperation.PROCESS_RUN:
            return True  # process-run is governed by SandboxPolicy.process
        if operation is CapabilityOperation.NETWORK:
            return True  # network is governed by SandboxPolicy.network
        if operation is CapabilityOperation.CREDENTIAL_ACCESS:
            return True  # credential scope lives on SandboxPolicy
        return False

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "entries": [entry.as_dict() for entry in self.entries],
            "digest": self.digest,
        }


# ---------------------------------------------------------------------------
# Mapping helper: C08 SideEffectClass -> runtime SideEffectSemantics.
# ---------------------------------------------------------------------------


def c08_to_runtime_side_effect(value: Any) -> SideEffectSemantics:
    """Map a C08 :class:`SideEffectClass` (or its string value) to runtime semantics.

    A live C08 SideEffectClass carries ``READ_ONLY`` / ``MUTATING`` /
    ``UNKNOWN``.  ``READ_ONLY`` is an unambiguous projection.  ``MUTATING``
    cannot itself distinguish ``LOCAL_REPLAY_SAFE`` from
    ``EXTERNAL_IDEMPOTENT`` / ``EXTERNAL_UNCERTAIN``; the default projection
    is the conservative ``EXTERNAL_UNCERTAIN`` -- the runtime may refine it
    when the surrounding ToolGrant entry carries an explicit
    :class:`SideEffectSemantics` value.  ``UNKNOWN`` maps to
    ``EXTERNAL_UNCERTAIN``; the sandbox never silently classifies an unknown
    side-effect as replay-safe.
    """
    try:
        raw = getattr(value, "value", value)
    except AttributeError:
        raw = value
    if raw == "READ_ONLY":
        return SideEffectSemantics.READ_ONLY
    if raw == "MUTATING":
        return SideEffectSemantics.EXTERNAL_UNCERTAIN
    if raw == "UNKNOWN":
        return SideEffectSemantics.EXTERNAL_UNCERTAIN
    if isinstance(raw, SideEffectSemantics):
        return raw
    raise SandboxContractError(f"unrecognised side-effect value: {raw!r}")


__all__ = [
    "CapabilityEntry",
    "CapabilityOperation",
    "PathPattern",
    "ResourceScope",
    "SandboxContractError",
    "SideEffectSemantics",
    "ToolGrant",
    "c08_to_runtime_side_effect",
]
