"""ToolExposurePlan: deterministic capability-to-transport mapping.

Two exposure entries for the same capability must agree on mechanism;
``ToolExposurePlan`` is therefore keyed by capability name.  The plan's
digest is canonical and ordering-independent so a worker's ToolGrant
cannot silently gain authority through a more convenient transport.

This module is intentionally narrow:

* it carries no provider, model, or orchestration logic;
* it never contacts the file system at import time;
* it produces stable, deterministic digests so an attempt's exposure
  evidence is reproducible and comparable.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from saberops.sandbox.grants import SandboxContractError


class ExposureMechanism(StrEnum):
    """Deterministic exposure mechanism for one capability.

    * ``NATIVE_MCP``            -- harness supports the capability as a
      native MCP tool; no Orch wrapper required.
    * ``ORCH_CLI``              -- the canonical Orch CLI subprocess
      drives the capability end-to-end.
    * ``MCP_TO_CLI``            -- an MCP source is rendered to an Orch
      CLI artifact by CLIHub (optional bridge; see C12 measurement).
    * ``NATIVE_BACKEND_TOOL``   -- the backend itself owns the capability
      as a built-in tool (e.g. a backend-native ``Bash`` or ``Edit``).
    """

    NATIVE_MCP = "NATIVE_MCP"
    ORCH_CLI = "ORCH_CLI"
    MCP_TO_CLI = "MCP_TO_CLI"
    NATIVE_BACKEND_TOOL = "NATIVE_BACKEND_TOOL"


@dataclass(frozen=True)
class ExposureEntry:
    """One (capability, mechanism) entry inside a :class:`ToolExposurePlan`."""

    capability: str
    mechanism: ExposureMechanism
    manifest_digest: str | None = None
    artifact_path: str | None = None
    artifact_sha256: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.capability, str)
            or not self.capability.strip()
            or "\x00" in self.capability
        ):
            raise SandboxContractError("ExposureEntry.capability must be a non-empty string")
        if self.manifest_digest is not None and (
            not isinstance(self.manifest_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", self.manifest_digest) is None
        ):
            raise SandboxContractError("manifest_digest must be a SHA-256 hex digest")
        if self.artifact_path is not None and not isinstance(self.artifact_path, str):
            raise SandboxContractError("artifact_path must be a string or null")
        if self.artifact_sha256 is not None and (
            not isinstance(self.artifact_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", self.artifact_sha256) is None
        ):
            raise SandboxContractError("artifact_sha256 must be a SHA-256 hex digest")

    def as_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "mechanism": self.mechanism.value,
            "manifest_digest": self.manifest_digest,
            "artifact_path": self.artifact_path,
            "artifact_sha256": self.artifact_sha256,
        }


@dataclass(frozen=True)
class ToolExposurePlan:
    """The deterministic mapping from a capability to a transport mechanism.

    Two exposure entries for the same capability must agree on mechanism;
    ``ToolExposurePlan`` is therefore keyed by capability name.  The plan's
    digest is canonical and ordering-independent so a worker's ToolGrant
    cannot silently gain authority through a more convenient transport.
    """

    entries: tuple[ExposureEntry, ...]
    digest: str = ""

    def __post_init__(self) -> None:
        by_capability: dict[str, ExposureEntry] = {}
        for entry in self.entries:
            if entry.capability in by_capability:
                raise SandboxContractError(
                    f"duplicate capability in ToolExposurePlan: {entry.capability!r}"
                )
            by_capability[entry.capability] = entry
        normalised = tuple(
            entry for _, entry in sorted(by_capability.items(), key=lambda item: item[0])
        )
        object.__setattr__(self, "entries", normalised)
        calculated = self._calculate_digest()
        if not self.digest:
            object.__setattr__(self, "digest", calculated)
        elif self.digest != calculated:
            raise SandboxContractError("ToolExposurePlan digest mismatch")

    def _calculate_digest(self) -> str:
        payload = [entry.as_dict() for entry in self.entries]
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def mechanism_for(self, capability: str) -> ExposureMechanism | None:
        for entry in self.entries:
            if entry.capability == capability:
                return entry.mechanism
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "entries": [entry.as_dict() for entry in self.entries],
            "digest": self.digest,
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> ToolExposurePlan:
        if not isinstance(payload, Mapping):
            raise SandboxContractError("ToolExposurePlan mapping must be a dict")
        schema_version = payload.get("schema_version", 1)
        if schema_version != 1:
            raise SandboxContractError("unsupported ToolExposurePlan schema_version")
        raw_entries = payload.get("entries")
        if not isinstance(raw_entries, list):
            raise SandboxContractError("ToolExposurePlan mapping.entries must be a list")
        entries: list[ExposureEntry] = []
        for item in raw_entries:
            if not isinstance(item, Mapping):
                raise SandboxContractError("ToolExposurePlan entry must be a dict")
            capability = item.get("capability")
            if not isinstance(capability, str) or not capability.strip():
                raise SandboxContractError(
                    "ToolExposurePlan entry capability must be a non-empty string"
                )
            raw_mechanism = item.get("mechanism")
            if not isinstance(raw_mechanism, str):
                raise SandboxContractError("ToolExposurePlan entry mechanism must be a string")
            try:
                mechanism = ExposureMechanism(raw_mechanism)
            except ValueError as exc:
                raise SandboxContractError(
                    f"unknown exposure mechanism: {item.get('mechanism')!r}"
                ) from exc
            entries.append(
                ExposureEntry(
                    capability=capability,
                    mechanism=mechanism,
                    manifest_digest=(
                        item.get("manifest_digest")
                        if item.get("manifest_digest") is not None
                        else None
                    ),
                    artifact_path=(
                        item.get("artifact_path")
                        if item.get("artifact_path") is not None
                        else None
                    ),
                    artifact_sha256=(
                        item.get("artifact_sha256")
                        if item.get("artifact_sha256") is not None
                        else None
                    ),
                )
            )
        serialized_digest = payload.get("digest")
        if "digest" in payload and (
            not isinstance(serialized_digest, str) or not serialized_digest
        ):
            raise SandboxContractError("ToolExposurePlan digest must be a non-empty string")
        return cls(
            entries=tuple(entries),
            digest=serialized_digest if isinstance(serialized_digest, str) else "",
        )


__all__ = [
    "ExposureEntry",
    "ExposureMechanism",
    "SandboxContractError",
    "ToolExposurePlan",
]
